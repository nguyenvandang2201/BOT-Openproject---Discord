import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from pathlib import Path

from cryptography.fernet import Fernet


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    """Single-process SQLite storage; no transaction contains an await."""

    def __init__(self, path: str, key: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.cipher_key = key.encode()
        self.cipher = Fernet(key.encode())
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 7:
            self.db.close()
            raise ValueError("Database was created by a newer bridge version")
        self.db.executescript("""
            PRAGMA foreign_keys=ON;
            PRAGMA journal_mode=WAL;
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS accounts (
                discord_id TEXT PRIMARY KEY, op_id INTEGER NOT NULL UNIQUE,
                name TEXT NOT NULL, tokens BLOB NOT NULL,
                status TEXT NOT NULL DEFAULT 'migration_pending', error TEXT,
                failures INTEGER NOT NULL DEFAULT 0, next_poll REAL NOT NULL DEFAULT 0,
                last_poll REAL, credential_kind TEXT NOT NULL DEFAULT 'oauth',
                guild_id TEXT, channel_id TEXT
            );
            CREATE TABLE IF NOT EXISTS flows (
                discord_id TEXT PRIMARY KEY, ticket TEXT UNIQUE, state TEXT UNIQUE,
                browser TEXT, expires REAL NOT NULL,
                confirm TEXT, pending BLOB
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                discord_id TEXT NOT NULL REFERENCES accounts(discord_id) ON DELETE CASCADE,
                notification_id INTEGER NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(discord_id, notification_id)
            );
            CREATE INDEX IF NOT EXISTS delivery_due
                ON deliveries(discord_id, status, next_attempt);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(accounts)")}
        with self.db:
            for name, definition in (
                ("credential_kind", "TEXT NOT NULL DEFAULT 'oauth'"),
                ("guild_id", "TEXT"),
                ("channel_id", "TEXT"),
            ):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE accounts ADD COLUMN {name} {definition}")
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS account_channel ON accounts(channel_id)")
            self.db.execute(
                "UPDATE accounts SET status='migration_pending' "
                "WHERE channel_id IS NULL AND status IN ('active','needs_attention')"
            )
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS dm_links (
                    op_id INTEGER PRIMARY KEY, discord_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS work_package_state (
                    id INTEGER PRIMARY KEY, assignee_id INTEGER, status_id INTEGER,
                    lock_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dm_deliveries (
                    event_key TEXT PRIMARY KEY, op_id INTEGER NOT NULL,
                    discord_id TEXT NOT NULL,
                    kind TEXT NOT NULL, work_package_id INTEGER NOT NULL,
                    subject TEXT NOT NULL, project TEXT NOT NULL,
                    detail TEXT NOT NULL, actor TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, error TEXT
                );
                CREATE INDEX IF NOT EXISTS dm_due ON dm_deliveries(status,next_attempt);
                CREATE TABLE IF NOT EXISTS inbound_events (
                    source_event_id TEXT PRIMARY KEY, work_package_id INTEGER NOT NULL,
                    action TEXT NOT NULL, lock_version INTEGER,
                    projection TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    received_at REAL NOT NULL, error TEXT
                );
                CREATE INDEX IF NOT EXISTS inbound_due
                    ON inbound_events(status,next_attempt,received_at);
                CREATE TABLE IF NOT EXISTS delivery_claims (
                    source_event_id TEXT NOT NULL, op_id INTEGER NOT NULL,
                    PRIMARY KEY(source_event_id,op_id)
                );
                CREATE TABLE IF NOT EXISTS watcher_baselines (
                    work_package_id INTEGER PRIMARY KEY, observed_at REAL NOT NULL,
                    initialized INTEGER NOT NULL DEFAULT 0,
                    next_check REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS work_package_watchers (
                    work_package_id INTEGER NOT NULL, op_id INTEGER NOT NULL,
                    PRIMARY KEY(work_package_id,op_id)
                );
                CREATE TABLE IF NOT EXISTS admin_tickets (
                    ticket_hash TEXT PRIMARY KEY, discord_id TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    session_hash TEXT PRIMARY KEY, discord_id TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_audit (
                    id INTEGER PRIMARY KEY, actor_discord_id TEXT NOT NULL,
                    action TEXT NOT NULL, target TEXT NOT NULL, at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS admin_audit_recent ON admin_audit(at DESC);
                CREATE TABLE IF NOT EXISTS recipient_rules (
                    project_id INTEGER NOT NULL, role TEXT NOT NULL,
                    event TEXT NOT NULL, enabled INTEGER NOT NULL,
                    PRIMARY KEY(project_id,role,event)
                );
                CREATE TABLE IF NOT EXISTS work_package_details (
                    work_package_id INTEGER PRIMARY KEY, snapshot TEXT NOT NULL,
                    due_date TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS detail_due ON work_package_details(due_date);
                CREATE TABLE IF NOT EXISTS reminder_scans (
                    work_package_id INTEGER NOT NULL, due_date TEXT NOT NULL,
                    phase TEXT NOT NULL, scanned_at REAL NOT NULL,
                    PRIMARY KEY(work_package_id,due_date,phase)
                );
                CREATE TABLE IF NOT EXISTS dm_preferences (
                    op_id INTEGER PRIMARY KEY, mode TEXT NOT NULL,
                    quiet_start TEXT NOT NULL DEFAULT '', quiet_end TEXT NOT NULL DEFAULT '',
                    muted_events TEXT NOT NULL DEFAULT '[]'
                );
            """)
            for table, name, definition in (
                ("work_package_state", "assignee_name", "TEXT NOT NULL DEFAULT ''"),
                ("work_package_state", "status_name", "TEXT NOT NULL DEFAULT ''"),
                ("dm_deliveries", "metadata", "TEXT NOT NULL DEFAULT '{}'"),
                ("dm_deliveries", "source_event_id", "TEXT"),
                ("dm_deliveries", "delivered_at", "REAL"),
                ("work_package_state", "assignee_kind", "TEXT NOT NULL DEFAULT 'user'"),
                ("work_package_state", "project_id", "INTEGER"),
                ("work_package_state", "responsible_id", "INTEGER"),
                ("watcher_baselines", "initialized", "INTEGER NOT NULL DEFAULT 0"),
                ("watcher_baselines", "next_check", "REAL NOT NULL DEFAULT 0"),
                ("watcher_baselines", "attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("watcher_baselines", "error", "TEXT"),
            ):
                existing = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
                if name not in existing:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            if version < 6:
                # Existing snapshots never recorded Accountable. Baseline them on the
                # next event instead of announcing a historical owner as newly added.
                self.db.execute(
                    "UPDATE work_package_state SET responsible_id=-1 "
                    "WHERE responsible_id IS NULL"
                )
            if version < 2:
                # Preserve links from the previous channel deployment once.
                self.db.execute(
                    "INSERT OR IGNORE INTO dm_links(op_id,discord_id,name,enabled) "
                    "SELECT op_id,discord_id,name,1 FROM accounts WHERE status='active'"
                )
            if version < 4:
                # Legacy events must not fan out to newly linked users on webhook replay.
                for row in self.db.execute(
                    "SELECT event_key,op_id,work_package_id,created_at,kind "
                    "FROM dm_deliveries WHERE kind!='welcome'"
                ):
                    suffix = ":" + row["kind"]
                    if not row["event_key"].endswith(suffix):
                        continue
                    source = row["event_key"][:-len(suffix)]
                    self.db.execute(
                        "INSERT OR IGNORE INTO inbound_events "
                        "(source_event_id,work_package_id,action,projection,status,received_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (source, row["work_package_id"], "legacy", "{}", "resolved",
                         row["created_at"]),
                    )
                    self.db.execute(
                        "INSERT OR IGNORE INTO delivery_claims(source_event_id,op_id) VALUES(?,?)",
                        (source, row["op_id"]),
                    )
            self.db.execute("PRAGMA user_version=7")

    def close(self):
        self.db.close()

    def seal(self, value):
        return self.cipher.encrypt(json.dumps(value).encode())

    def unseal(self, value):
        return json.loads(self.cipher.decrypt(value))

    def cleanup(self):
        with self.db:
            self.db.execute("DELETE FROM flows WHERE expires < ?", (time.time(),))
            self.db.execute("DELETE FROM admin_tickets WHERE expires < ?", (time.time(),))
            self.db.execute("DELETE FROM admin_sessions WHERE expires < ?", (time.time(),))

    def admin_ticket(self, discord_id):
        ticket = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute("DELETE FROM admin_tickets WHERE discord_id=?", (discord_id,))
            self.db.execute(
                "INSERT INTO admin_tickets VALUES(?,?,?)",
                (digest(ticket), discord_id, time.time() + 300),
            )
        return ticket

    def admin_login(self, ticket):
        if len(ticket) > 128:
            return None
        session = secrets.token_urlsafe(32)
        with self.db:
            row = self.db.execute(
                "DELETE FROM admin_tickets WHERE ticket_hash=? AND expires>? RETURNING discord_id",
                (digest(ticket), time.time()),
            ).fetchone()
            if not row:
                return None
            self.db.execute(
                "INSERT INTO admin_sessions VALUES(?,?,?)",
                (digest(session), row["discord_id"], time.time() + 3600),
            )
        return session

    def admin_session(self, session):
        if not session or len(session) > 128:
            return None
        row = self.db.execute(
            "SELECT discord_id FROM admin_sessions WHERE session_hash=? AND expires>?",
            (digest(session), time.time()),
        ).fetchone()
        return row["discord_id"] if row else None

    def admin_csrf(self, session):
        return hmac.new(self.cipher_key, session.encode(), hashlib.sha256).hexdigest()

    def admin_logout(self, session):
        with self.db:
            self.db.execute("DELETE FROM admin_sessions WHERE session_hash=?", (digest(session),))

    def start_flow(self, discord_id: str) -> str:
        self.cleanup()
        ticket = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute("DELETE FROM flows WHERE discord_id=?", (discord_id,))
            self.db.execute(
                "INSERT INTO flows(discord_id,ticket,expires) VALUES(?,?,?)",
                (discord_id, digest(ticket), time.time() + 600),
            )
        return ticket

    def begin_oauth(self, ticket: str):
        state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.db:
            row = self.db.execute(
                "UPDATE flows SET ticket=NULL,state=?,browser=? "
                "WHERE ticket=? AND expires>? RETURNING discord_id",
                (digest(state), digest(browser), digest(ticket), time.time()),
            ).fetchone()
        return (state, browser) if row else None

    def consume_state(self, state: str, browser: str):
        with self.db:
            row = self.db.execute(
                "UPDATE flows SET state=NULL,browser=NULL WHERE state=? AND browser=? "
                "AND expires>? RETURNING discord_id,expires",
                (digest(state), digest(browser), time.time()),
            ).fetchone()
        return dict(row) if row else None

    def set_pending(self, discord_id: str, flow_expires: float, value: dict):
        code = secrets.token_hex(8)
        with self.db:
            # A newer /link or /unlink must invalidate a callback still in flight.
            result = self.db.execute(
                "UPDATE flows SET confirm=?,pending=? WHERE discord_id=? AND expires=? "
                "AND expires>? AND state IS NULL AND ticket IS NULL",
                (digest(code), self.seal(value), discord_id, flow_expires, time.time()),
            )
        return code if result.rowcount else None

    def pending(self, discord_id: str, code: str):
        row = self.db.execute(
            "SELECT pending FROM flows WHERE discord_id=? AND confirm=? AND expires>?",
            (discord_id, digest(code), time.time()),
        ).fetchone()
        return self.unseal(row[0]) if row and row[0] else None

    def activate(self, discord_id: str, code: str, ids: list[int]):
        with self.db:
            value = self.pending(discord_id, code)
            if not value:
                raise ValueError("Mã xác nhận hết hạn hoặc không hợp lệ. Chạy /op link lại.")
            if self.account(discord_id):
                raise ValueError("Hãy /op unlink tài khoản hiện tại trước khi liên kết lại.")
            self.db.execute(
                "INSERT INTO accounts(discord_id,op_id,name,tokens) VALUES(?,?,?,?)",
                (discord_id, value["id"], value["name"], self.seal(value["tokens"])),
            )
            self.db.executemany(
                "INSERT INTO deliveries VALUES(?,?, 'baseline',0,0)",
                [(discord_id, nid) for nid in ids],
            )
            self.db.execute("DELETE FROM flows WHERE discord_id=?", (discord_id,))

    def provision_api_key(self, discord_id, op_id, name, api_key, guild_id, channel_id, ids):
        with self.db:
            self.db.execute(
                "INSERT INTO accounts(discord_id,op_id,name,tokens,credential_kind,guild_id,"
                "channel_id,status) VALUES(?,?,?,?,?,?,?,'active')",
                (discord_id, op_id, name, self.seal({"api_key": api_key}), "api_key", guild_id, channel_id),
            )
            self.db.executemany(
                "INSERT INTO deliveries VALUES(?,?, 'baseline',0,0)",
                [(discord_id, nid) for nid in ids],
            )

    def assign_channel(self, discord_id, guild_id, channel_id):
        with self.db:
            result = self.db.execute(
                "UPDATE accounts SET guild_id=?,channel_id=?,status='active',error=NULL "
                "WHERE discord_id=? AND status='migration_pending'",
                (guild_id, channel_id, discord_id),
            )
            if result.rowcount != 1:
                raise ValueError("Account is not awaiting a channel")

    def suspend(self, discord_id, error="suspended_by_admin"):
        with self.db:
            result = self.db.execute(
                "UPDATE accounts SET status='needs_attention',error=? WHERE discord_id=?",
                (error, discord_id),
            )
            if result.rowcount != 1:
                raise ValueError("Account not found")

    def resume(self, discord_id):
        with self.db:
            result = self.db.execute(
                "UPDATE accounts SET status='active',error=NULL,failures=0,next_poll=0 "
                "WHERE discord_id=? AND channel_id IS NOT NULL",
                (discord_id,),
            )
            if result.rowcount != 1:
                raise ValueError("Account has no channel")

    def list_accounts(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM accounts ORDER BY discord_id")]

    def account(self, discord_id):
        row = self.db.execute("SELECT * FROM accounts WHERE discord_id=?", (discord_id,)).fetchone()
        return dict(row) if row else None

    def accounts(self):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM accounts WHERE status='active' AND channel_id IS NOT NULL "
                "AND next_poll<=?", (time.time(),)
            )
        ]

    def save_tokens(self, discord_id, tokens):
        with self.db:
            self.db.execute(
                "UPDATE accounts SET tokens=? WHERE discord_id=?", (self.seal(tokens), discord_id)
            )

    def unlink(self, discord_id):
        with self.db:
            self.db.execute("DELETE FROM flows WHERE discord_id=?", (discord_id,))
            self.db.execute("DELETE FROM accounts WHERE discord_id=?", (discord_id,))

    def discover(self, discord_id, ids):
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO deliveries VALUES(?,?, 'pending',0,0)",
                [(discord_id, nid) for nid in ids],
            )
            self.db.execute(
                "UPDATE accounts SET last_poll=? WHERE discord_id=?", (time.time(), discord_id)
            )

    def jobs(self, discord_id, limit):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM deliveries WHERE discord_id=? AND status='pending' "
                "AND next_attempt<=? ORDER BY notification_id LIMIT ?",
                (discord_id, time.time(), limit),
            )
        ]

    def finish(self, discord_id, notification_id, status):
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET status=? WHERE discord_id=? AND notification_id=?",
                (status, discord_id, notification_id),
            )

    def retry(self, job, delay):
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET attempts=attempts+1,next_attempt=? "
                "WHERE discord_id=? AND notification_id=?",
                (time.time() + delay, job["discord_id"], job["notification_id"]),
            )

    def account_result(self, discord_id, *, status="active", error=None, delay=0):
        with self.db:
            self.db.execute(
                "UPDATE accounts SET status=?,error=?,next_poll=?, "
                "failures=CASE WHEN ? IS NULL THEN 0 ELSE failures+1 END "
                "WHERE discord_id=?",
                (status, error, time.time() + delay, error, discord_id),
            )

    def pending_count(self, discord_id):
        return self.db.execute(
            "SELECT COUNT(*) FROM deliveries WHERE discord_id=? AND status='pending'", (discord_id,)
        ).fetchone()[0]

    def dm_link(self, op_id, discord_id, name="", *, welcome=True):
        with self.db:
            old = self.db.execute(
                "SELECT discord_id FROM dm_links WHERE op_id=?", (op_id,)
            ).fetchone()
            self.db.execute(
                "INSERT INTO dm_links(op_id,discord_id,name,enabled) VALUES(?,?,?,1) "
                "ON CONFLICT(op_id) DO UPDATE SET discord_id=excluded.discord_id,"
                "name=excluded.name,enabled=1",
                (op_id, discord_id, name),
            )
            changed = old is None or old["discord_id"] != discord_id
            if changed and welcome:
                self.db.execute(
                    "INSERT INTO dm_deliveries "
                    "(event_key,op_id,discord_id,kind,work_package_id,subject,project,"
                    "detail,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (f"welcome:{op_id}:{secrets.token_hex(12)}", op_id, discord_id,
                     "welcome", 0, "Chào mừng đến với thông báo OpenProject", "",
                     name[:200], "", time.time()),
                )
        return changed

    def dm_unlink(self, op_id):
        with self.db:
            result = self.db.execute("DELETE FROM dm_links WHERE op_id=?", (op_id,))
        return result.rowcount

    def dm_set_enabled(self, op_id, enabled):
        with self.db:
            result = self.db.execute(
                "UPDATE dm_links SET enabled=? WHERE op_id=?", (int(enabled), op_id)
            )
        return result.rowcount

    def dm_links(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM dm_links ORDER BY op_id")]

    def dm_link_for_discord(self, discord_id):
        row = self.db.execute(
            "SELECT * FROM dm_links WHERE discord_id=?", (discord_id,)
        ).fetchone()
        return dict(row) if row else None

    def current_assignee(self, work_package_id):
        row = self.db.execute(
            "SELECT assignee_id,assignee_kind FROM work_package_state WHERE id=?",
            (work_package_id,),
        ).fetchone()
        return dict(row) if row else None

    def work_package_project_id(self, work_package_id):
        row = self.db.execute(
            "SELECT project_id FROM work_package_state WHERE id=?", (work_package_id,)
        ).fetchone()
        return row[0] if row else None

    def set_work_package_project_id(self, work_package_id, project_id):
        with self.db:
            self.db.execute(
                "UPDATE work_package_state SET project_id=? WHERE id=?",
                (project_id, work_package_id),
            )

    def watchers_due(self, limit, project_ids=()):
        """Start with unobserved tasks, then revisit initialized snapshots."""
        scope = (
            " AND (wp.project_id IS NULL OR wp.project_id IN ("
            + ",".join("?" for _ in project_ids) + "))"
            if project_ids else ""
        )
        params = tuple(project_ids)
        missing = [row[0] for row in self.db.execute(
            "SELECT wp.id FROM work_package_state AS wp WHERE NOT EXISTS "
            "(SELECT 1 FROM watcher_baselines AS b WHERE b.work_package_id=wp.id) "
            + scope + " ORDER BY wp.id LIMIT ?", (*params, limit)
        )]
        remaining = limit - len(missing)
        due = [row[0] for row in self.db.execute(
            "SELECT b.work_package_id FROM watcher_baselines AS b "
            "JOIN work_package_state AS wp ON wp.id=b.work_package_id "
            "WHERE b.next_check<=?" + scope +
            " ORDER BY b.next_check,b.work_package_id LIMIT ?",
            (time.time(), *params, remaining)
        )] if remaining else []
        return missing + due

    def watcher_baseline(self, work_package_id):
        row = self.db.execute(
            "SELECT initialized,attempts FROM watcher_baselines WHERE work_package_id=?",
            (work_package_id,),
        ).fetchone()
        return dict(row) if row else None

    def watcher_errors(self, limit=20):
        return [dict(row) for row in self.db.execute(
            "SELECT work_package_id,error,attempts FROM watcher_baselines "
            "WHERE error IS NOT NULL ORDER BY next_check DESC LIMIT ?", (limit,)
        )]

    def known_watchers(self, work_package_id):
        return {row[0] for row in self.db.execute(
            "SELECT op_id FROM work_package_watchers WHERE work_package_id=?",
            (work_package_id,),
        )}

    def defer_watcher_check(self, work_package_id, error, delay):
        with self.db:
            self.db.execute(
                "INSERT INTO watcher_baselines(work_package_id,observed_at,next_check,attempts,error) "
                "VALUES(?,?,?,?,?) ON CONFLICT(work_package_id) DO UPDATE SET "
                "next_check=excluded.next_check,attempts=watcher_baselines.attempts+1,"
                "error=excluded.error",
                (work_package_id, 0, time.time() + delay, 1, error),
            )

    def reconcile_watchers(self, work_package_id, watcher_ids, wp, interval,
                           notify_added=True):
        """Update a watcher snapshot and enqueue notices for additions after baseline."""
        watcher_ids = set(watcher_ids)
        with self.db:
            baseline = self.watcher_baseline(work_package_id)
            old = self.known_watchers(work_package_id)
            added = (watcher_ids - old if baseline and baseline["initialized"]
                     and notify_added else set())
            if added and (wp is None or wp["id"] != work_package_id):
                raise ValueError("Current work package required for new watchers")
            self.db.execute(
                "DELETE FROM work_package_watchers WHERE work_package_id=?", (work_package_id,)
            )
            self.db.executemany(
                "INSERT INTO work_package_watchers(work_package_id,op_id) VALUES(?,?)",
                ((work_package_id, op_id) for op_id in sorted(watcher_ids)),
            )
            now = time.time()
            self.db.execute(
                "INSERT INTO watcher_baselines "
                "(work_package_id,observed_at,initialized,next_check) VALUES(?,?,1,?) "
                "ON CONFLICT(work_package_id) DO UPDATE SET observed_at=excluded.observed_at,"
                "initialized=1,next_check=excluded.next_check,attempts=0,error=NULL",
                (work_package_id, now, now + interval),
            )
            if not added:
                return 0
            source = f"watcher-reconcile:{work_package_id}:{time.time_ns()}"
            self.db.execute(
                "INSERT INTO inbound_events "
                "(source_event_id,work_package_id,action,lock_version,projection,status,received_at) "
                "VALUES(?,?,?,?,?,'resolved',?)",
                (source, work_package_id, "watcher_reconcile", wp["lock_version"],
                 json.dumps(wp, ensure_ascii=False), now),
            )
            count = 0
            for op_id in sorted(added):
                self.db.execute(
                    "INSERT INTO delivery_claims(source_event_id,op_id) VALUES(?,?)",
                    (source, op_id),
                )
                link = self.db.execute(
                    "SELECT discord_id FROM dm_links WHERE op_id=? AND enabled=1", (op_id,)
                ).fetchone()
                if not link:
                    continue
                metadata = {
                    "status": wp["status_title"], "priority": wp.get("priority", ""),
                    "due_date": wp.get("due_date", ""), "assignee": wp["assignee_name"],
                    "reporter": wp.get("reporter", ""), "changes": ["watcher_added"],
                    "recipient_roles": ["watcher"],
                }
                self.db.execute(
                    "INSERT INTO dm_deliveries "
                    "(event_key,source_event_id,op_id,discord_id,kind,work_package_id,"
                    "subject,project,detail,actor,created_at,metadata) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"{source}:{op_id}", source, op_id, link["discord_id"],
                     "watcher_added", work_package_id, wp["subject"], wp["project"],
                     "Bạn đã được thêm theo dõi", "", now,
                     json.dumps(metadata, ensure_ascii=False)),
                )
                count += 1
            return count

    def enqueue_event(self, event):
        """Persist a validated projection before acknowledging the webhook."""
        wp = event["work_package"]
        source = (
            f"comment:{event['activity_id']}"
            if event["action"] == "work_package_comment:comment"
            else f"wp:{wp['id']}:{wp['lock_version']}"
        )
        projection = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self.db:
            result = self.db.execute(
                "INSERT OR IGNORE INTO inbound_events "
                "(source_event_id,work_package_id,action,lock_version,projection,received_at) "
                "VALUES(?,?,?,?,?,?)",
                (source, wp["id"], event["action"], wp["lock_version"],
                 projection, time.time()),
            )
        return result.rowcount

    def pending_events(self, limit):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM inbound_events WHERE status='pending' AND next_attempt<=? "
            "ORDER BY received_at,rowid LIMIT ?", (time.time(), limit)
        )]

    def inbound_status(self):
        return {row["status"]: row["total"] for row in self.db.execute(
            "SELECT status,COUNT(*) AS total FROM inbound_events GROUP BY status"
        )}

    def inbound_failures(self, limit=50):
        return [dict(row) for row in self.db.execute(
            "SELECT source_event_id,work_package_id,error FROM inbound_events "
            "WHERE status='failed' ORDER BY received_at DESC LIMIT ?", (limit,)
        )]

    def finish_event(self, source, status, error=None, delay=0):
        with self.db:
            self.db.execute(
                "UPDATE inbound_events SET status=?,error=?,attempts=attempts+1,next_attempt=? "
                "WHERE source_event_id=? AND status='pending'",
                (status, error, time.time() + delay, source),
            )

    def resolve_event(self, source, recipients, watcher_ids=None, watcher_recheck_delay=0,
                      detail_notifications=False, attachments=None,
                      attachment_notifications=False):
        """Commit the task snapshot and all recipient jobs in one transaction.

        recipients maps OpenProject user IDs to their recipient roles. Linking later
        must not turn a previously processed event into a new notification.
        """
        with self.db:
            row = self.db.execute(
                "SELECT * FROM inbound_events WHERE source_event_id=? AND status='pending'",
                (source,),
            ).fetchone()
            if row is None:
                return 0
            event = json.loads(row["projection"])
            wp = event["work_package"]
            old = self.db.execute(
                "SELECT * FROM work_package_state WHERE id=?", (wp["id"],)
            ).fetchone()
            if event["action"] != "work_package_comment:comment":
                if old and wp["lock_version"] <= old["lock_version"]:
                    self.db.execute(
                        "UPDATE inbound_events SET status='resolved' WHERE source_event_id=?",
                        (source,),
                    )
                    return 0
                principal_id = wp.get("assignee_group_id") or wp["assignee_id"]
                principal_kind = (
                    "group" if wp.get("assignee_group_id") else
                    "user" if wp["assignee_id"] else "none"
                )
                previous_kind = old["assignee_kind"] if old else "none"
                previous_id = old["assignee_id"] if old else None
                changed_assignee = (
                    old is not None and
                    (previous_id, previous_kind) != (principal_id, principal_kind)
                )
                changes = []
                if event["action"] == "work_package:created":
                    changes.append("assignment" if principal_id else "created")
                elif changed_assignee and principal_id:
                    changes.append("assignment")
                if old and old["status_id"] != wp["status_id"]:
                    changes.append("status")
                responsible_id = wp.get("responsible_id")
                if (old and old["responsible_id"] != -1
                        and old["responsible_id"] != responsible_id and responsible_id
                        and "accountable" in recipients.get(responsible_id, ())):
                    changes.append("accountable_added")
                observed = dict(wp.get("observed_details") or {})
                if attachments is not None:
                    observed["attachments"] = attachments
                detail_row = self.db.execute(
                    "SELECT snapshot FROM work_package_details WHERE work_package_id=?",
                    (wp["id"],),
                ).fetchone()
                previous = json.loads(detail_row["snapshot"]) if detail_row else {}
                detail_changes = []
                for field, new in observed.items():
                    if field in previous and previous[field] != new:
                        before = previous[field]
                        if field == "description":
                            if before["hash"] == new["hash"]:
                                continue
                            before, new_value = before["preview"], new["preview"]
                        elif field == "attachments":
                            before_ids = {item["id"] for item in before}
                            new_ids = {item["id"] for item in new}
                            if before_ids == new_ids:
                                continue
                            added_files = [item["name"] for item in new
                                           if item["id"] not in before_ids][:3]
                            before, new_value = len(before), len(new)
                        else:
                            new_value = new
                        detail_changes.append({
                            "field": field, "before": before, "after": new_value,
                            **({"added": added_files} if field == "attachments" else {}),
                        })
                previous.update(observed)
                display_details = previous
                if observed:
                    self.db.execute(
                        "INSERT INTO work_package_details(work_package_id,snapshot,due_date) "
                        "VALUES(?,?,?) ON CONFLICT(work_package_id) DO UPDATE SET "
                        "snapshot=excluded.snapshot,due_date=excluded.due_date",
                        (wp["id"], json.dumps(previous, ensure_ascii=False),
                         previous.get("due_date") or ""),
                    )
                notified_details = [item for item in detail_changes if (
                    attachment_notifications if item["field"] == "attachments"
                    else detail_notifications
                )]
                if notified_details:
                    changes.append("details")
                self.db.execute(
                    "INSERT INTO work_package_state "
                    "(id,assignee_id,assignee_kind,status_id,lock_version,assignee_name,status_name) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "assignee_id=excluded.assignee_id,assignee_kind=excluded.assignee_kind,"
                    "status_id=excluded.status_id,lock_version=excluded.lock_version,"
                    "assignee_name=excluded.assignee_name,status_name=excluded.status_name",
                    (wp["id"], principal_id, principal_kind, wp["status_id"],
                     wp["lock_version"], wp.get("assignee_name", ""), wp["status_title"]),
                )
                self.db.execute(
                    "UPDATE work_package_state SET project_id=? WHERE id=?",
                    (wp.get("project_id"), wp["id"]),
                )
                self.db.execute(
                    "UPDATE work_package_state SET responsible_id=? WHERE id=?",
                    (wp.get("responsible_id"), wp["id"]),
                )
            else:
                changes = ["comment"]
                detail_changes = []
                notified_details = []
                detail_row = self.db.execute(
                    "SELECT snapshot FROM work_package_details WHERE work_package_id=?",
                    (wp["id"],),
                ).fetchone()
                display_details = json.loads(detail_row["snapshot"]) if detail_row else {}
            added_watchers = set()
            if watcher_ids is not None:
                watcher_ids = set(watcher_ids)
                baseline = self.db.execute(
                    "SELECT initialized FROM watcher_baselines WHERE work_package_id=?",
                    (wp["id"],)
                ).fetchone()
                previous_watchers = {
                    item[0] for item in self.db.execute(
                        "SELECT op_id FROM work_package_watchers WHERE work_package_id=?",
                        (wp["id"],),
                    )
                }
                if (baseline and baseline["initialized"]
                        and event["action"] != "work_package:created"):
                    added_watchers = watcher_ids - previous_watchers
                self.db.execute(
                    "DELETE FROM work_package_watchers WHERE work_package_id=?", (wp["id"],)
                )
                self.db.executemany(
                    "INSERT INTO work_package_watchers(work_package_id,op_id) VALUES(?,?)",
                    ((wp["id"], op_id) for op_id in sorted(watcher_ids)),
                )
                self.db.execute(
                    "INSERT INTO watcher_baselines "
                    "(work_package_id,observed_at,initialized,next_check) VALUES(?,?,1,?) "
                    "ON CONFLICT(work_package_id) DO UPDATE SET observed_at=excluded.observed_at,"
                    "initialized=1,next_check=excluded.next_check,attempts=0,error=NULL",
                    (wp["id"], time.time(), time.time() + watcher_recheck_delay),
                )
            count = 0
            for op_id, roles in recipients.items():
                recipient_changes = list(changes)
                if ("watcher" in roles and "assignee" not in roles
                        and "group_assignee" not in roles
                        and event["action"] == "work_package:created"):
                    recipient_changes = ["created"]
                if ("accountable" in roles and "assignee" not in roles
                        and "group_assignee" not in roles and "watcher" not in roles
                        and "assignment" in recipient_changes):
                    recipient_changes = [
                        ("created" if event["action"] == "work_package:created" else "summary")
                        if change == "assignment" else change
                        for change in recipient_changes
                    ]
                if op_id in added_watchers and "watcher_added" not in recipient_changes:
                    recipient_changes.append("watcher_added")
                if not recipient_changes:
                    continue
                claim = self.db.execute(
                    "INSERT OR IGNORE INTO delivery_claims(source_event_id,op_id) VALUES(?,?)",
                    (source, op_id),
                )
                if not claim.rowcount:
                    continue
                link = self.db.execute(
                    "SELECT discord_id FROM dm_links WHERE op_id=? AND enabled=1", (op_id,)
                ).fetchone()
                if not link:
                    continue
                kind = recipient_changes[0] if len(recipient_changes) == 1 else "summary"
                metadata = {
                    "status": wp["status_title"],
                    "priority": display_details.get("priority", wp.get("priority", "")),
                    "due_date": display_details.get("due_date", wp.get("due_date", "")),
                    "assignee": wp.get("assignee_name", ""),
                    "reporter": wp.get("reporter", ""),
                    "previous_assignee": (
                        old["assignee_name"] or f"ID {old['assignee_id']}"
                        if old and old["assignee_id"] is not None else ""
                    ),
                    "previous_status": (
                        old["status_name"] or f"ID {old['status_id']}" if old else ""
                    ),
                    "changes": recipient_changes,
                    "recipient_roles": sorted(set(roles)),
                    "assignee_group_id": wp.get("assignee_group_id"),
                    "responsible_id": wp.get("responsible_id"),
                    "field_changes": notified_details,
                }
                result = self.db.execute(
                    "INSERT OR IGNORE INTO dm_deliveries "
                    "(event_key,source_event_id,op_id,discord_id,kind,work_package_id,"
                    "subject,project,detail,actor,created_at,metadata) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"{source}:{op_id}", source, op_id, link["discord_id"], kind,
                     wp["id"], wp["subject"], wp["project"],
                     event.get("comment") or wp["status_title"], event["actor"],
                     time.time(), json.dumps(metadata, ensure_ascii=False)),
                )
                count += result.rowcount
            self.db.execute(
                "UPDATE inbound_events SET status='resolved',error=NULL WHERE source_event_id=?",
                (source,),
            )
            return count

    def accept_webhook(self, event):
        """Atomically update the work package snapshot and enqueue recipient jobs."""
        wp = event["work_package"]
        wp_id = wp["id"]
        with self.db:
            if event["action"] in ("work_package:created", "work_package:updated"):
                old = self.db.execute(
                    "SELECT * FROM work_package_state WHERE id=?", (wp_id,)
                ).fetchone()
                version = wp["lock_version"]
                if old and version <= old["lock_version"]:
                    return 0
                self.db.execute(
                    "INSERT INTO work_package_state "
                    "(id,assignee_id,status_id,lock_version,assignee_name,status_name) "
                    "VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET assignee_id=excluded.assignee_id,"
                    "status_id=excluded.status_id,lock_version=excluded.lock_version,"
                    "assignee_name=excluded.assignee_name,status_name=excluded.status_name",
                    (wp_id, wp["assignee_id"], wp["status_id"], version,
                     wp.get("assignee_name", ""), wp["status_title"]),
                )
                self.db.execute(
                    "UPDATE work_package_state SET project_id=? WHERE id=?",
                    (wp.get("project_id"), wp_id),
                )
                kinds = []
                if wp["assignee_id"] is not None and (
                    event["action"] == "work_package:created"
                    or (old is not None and old["assignee_id"] != wp["assignee_id"])
                ):
                    kinds.append(("assignment", "Bạn được giao công việc"))
                if old is not None and old["status_id"] != wp["status_id"]:
                    kinds.append(("status", wp["status_title"]))
                event_id = f"wp:{wp_id}:{version}"
            else:
                kinds = [("comment", event.get("comment") or "Có bình luận mới")]
                event_id = f"comment:{event['activity_id']}"
            if wp["assignee_id"] is None:
                return 0
            link = self.db.execute(
                "SELECT discord_id FROM dm_links WHERE op_id=? AND enabled=1",
                (wp["assignee_id"],),
            ).fetchone()
            if not link:
                return 0
            count = 0
            for kind, detail in kinds:
                metadata = {
                    "status": wp["status_title"],
                    "priority": wp.get("priority", ""),
                    "due_date": wp.get("due_date", ""),
                    "assignee": wp.get("assignee_name", ""),
                    "reporter": wp.get("reporter", ""),
                    "previous_assignee": (
                        old["assignee_name"] or f"ID {old['assignee_id']}"
                        if old and old["assignee_id"] is not None else ""
                    ) if event["action"] != "work_package_comment:comment" else "",
                    "previous_status": (
                        old["status_name"] or f"ID {old['status_id']}"
                        if old else ""
                    ) if event["action"] != "work_package_comment:comment" else "",
                }
                result = self.db.execute(
                    "INSERT OR IGNORE INTO dm_deliveries "
                    "(event_key,op_id,discord_id,kind,work_package_id,subject,project,detail,actor,"
                    "created_at,metadata) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (f"{event_id}:{kind}", wp["assignee_id"], link["discord_id"], kind, wp_id,
                     wp["subject"], wp["project"], detail, event["actor"], time.time(),
                     json.dumps(metadata, ensure_ascii=False)),
                )
                count += result.rowcount
            return count

    def dm_jobs(self, limit):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM dm_deliveries WHERE status='pending' AND next_attempt<=? "
            "ORDER BY CASE WHEN kind IN ('welcome','test','digest','due','overdue') "
            "THEN 0 ELSE 1 END,created_at,event_key LIMIT ?", (time.time(), limit)
        )]

    def dm_defer(self, key, until):
        with self.db:
            self.db.execute(
                "UPDATE dm_deliveries SET next_attempt=? WHERE event_key=? AND status='pending'",
                (until, key),
            )

    def reminder_candidates(self, today, yesterday, after_id=0, limit=200):
        return [dict(row) for row in self.db.execute(
            "SELECT d.work_package_id,d.due_date FROM work_package_details AS d "
            "JOIN work_package_state AS s ON s.id=d.work_package_id "
            "WHERE d.due_date IN (?,?) AND d.work_package_id>? "
            "AND NOT EXISTS(SELECT 1 FROM reminder_scans AS r WHERE "
            "r.work_package_id=d.work_package_id AND r.due_date=d.due_date AND "
            "r.phase=CASE WHEN d.due_date=? THEN 'due' ELSE 'overdue' END) "
            "ORDER BY d.work_package_id LIMIT ?",
            (today, yesterday, after_id, today, limit),
        )]

    def mark_reminder_scanned(self, wp_id, due_date, phase):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO reminder_scans VALUES(?,?,?,?)",
                (wp_id, due_date, phase, time.time()),
            )

    def enqueue_reminder(self, wp, recipients, phase, due_date):
        """One durable reminder per recipient, task, date and phase."""
        if phase not in ("due", "overdue") or not due_date:
            raise ValueError("Invalid reminder")
        now = time.time()
        count = 0
        with self.db:
            for op_id, roles in recipients.items():
                link = self.db.execute(
                    "SELECT discord_id FROM dm_links WHERE op_id=? AND enabled=1", (op_id,)
                ).fetchone()
                if not link:
                    continue
                recent = self.db.execute(
                    "SELECT 1 FROM dm_deliveries WHERE op_id=? AND work_package_id=? "
                    "AND kind NOT IN ('due','overdue','test','welcome','digest') "
                    "AND status IN ('pending','sent','batched') AND created_at>? LIMIT 1",
                    (op_id, wp["id"], now - 3600),
                ).fetchone()
                if recent:
                    continue
                metadata = {
                    "status": wp["status_title"], "priority": wp.get("priority", ""),
                    "due_date": due_date, "assignee": wp.get("assignee_name", ""),
                    "reporter": wp.get("reporter", ""), "recipient_roles": sorted(set(roles)),
                    "assignee_group_id": wp.get("assignee_group_id"),
                    "reminder_due_date": due_date,
                }
                key = f"reminder:{wp['id']}:{due_date}:{phase}:{op_id}"
                result = self.db.execute(
                    "INSERT OR IGNORE INTO dm_deliveries "
                    "(event_key,source_event_id,op_id,discord_id,kind,work_package_id,"
                    "subject,project,detail,actor,created_at,metadata) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, key, op_id, link["discord_id"], phase, wp["id"],
                     wp["subject"], wp["project"], "", "", now,
                     json.dumps(metadata, ensure_ascii=False)),
                )
                count += result.rowcount
        return count

    def dm_preference(self, op_id, defaults):
        row = self.db.execute(
            "SELECT mode,quiet_start,quiet_end,muted_events FROM dm_preferences WHERE op_id=?",
            (op_id,),
        ).fetchone()
        return (dict(row) | {"muted_events": json.loads(row["muted_events"])}) if row else {
            "mode": defaults.notification_mode, "quiet_start": defaults.quiet_start,
            "quiet_end": defaults.quiet_end, "muted_events": [],
        }

    def set_dm_preference(self, op_id, mode, quiet_start="", quiet_end="", muted_events=()):
        if mode not in ("instant", "daily") or len(muted_events) > 12:
            raise ValueError("Invalid notification preference")
        with self.db:
            self.db.execute(
                "INSERT INTO dm_preferences(op_id,mode,quiet_start,quiet_end,muted_events) "
                "VALUES(?,?,?,?,?) ON CONFLICT(op_id) DO UPDATE SET mode=excluded.mode,"
                "quiet_start=excluded.quiet_start,quiet_end=excluded.quiet_end,"
                "muted_events=excluded.muted_events",
                (op_id, mode, quiet_start, quiet_end, json.dumps(sorted(set(muted_events)))),
            )

    def enqueue_digest(self, op_id, local_day, limit=20):
        """Move pending event notices to one durable digest, at most once per day."""
        key = f"digest:{local_day}:{op_id}"
        with self.db:
            if self.db.execute(
                "SELECT 1 FROM dm_deliveries WHERE event_key=?", (key,)
            ).fetchone():
                return 0
            rows = [dict(row) for row in self.db.execute(
                "SELECT * FROM dm_deliveries WHERE op_id=? AND status='pending' "
                "AND kind NOT IN ('welcome','test','due','overdue','digest') "
                "ORDER BY created_at,event_key LIMIT ?", (op_id, limit),
            )]
            if not rows:
                return 0
            entries = [{key: row[key] for key in (
                "event_key", "op_id", "discord_id", "kind", "work_package_id",
                "subject", "project", "detail", "actor", "metadata", "source_event_id",
            )} for row in rows]
            self.db.execute(
                "UPDATE dm_deliveries SET status='batched' WHERE event_key IN (" +
                ",".join("?" for _ in rows) + ") AND status='pending'",
                tuple(row["event_key"] for row in rows),
            )
            self.db.execute(
                "INSERT INTO dm_deliveries "
                "(event_key,op_id,discord_id,kind,work_package_id,subject,project,"
                "detail,actor,created_at,metadata) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (key, op_id, rows[0]["discord_id"], "digest", 0,
                 "Tóm tắt OpenProject", "", "", "", time.time(),
                 json.dumps({"entries": entries, "local_day": local_day}, ensure_ascii=False)),
            )
        return len(rows)

    def digest_candidates(self, limit=200):
        return [row[0] for row in self.db.execute(
            "SELECT DISTINCT op_id FROM dm_deliveries WHERE status='pending' "
            "AND kind NOT IN ('welcome','test','due','overdue','digest') LIMIT ?",
            (limit,),
        )]

    def dm_finish(self, key, status, error=None, delay=0):
        with self.db:
            self.db.execute(
                "UPDATE dm_deliveries SET status=?,error=?,attempts=attempts+1,next_attempt=?,"
                "delivered_at=CASE WHEN ?='sent' THEN ? ELSE delivered_at END "
                "WHERE event_key=?", (status, error, time.time() + delay,
                                      status, time.time(), key)
            )

    def dm_pending_count(self, discord_id):
        return self.db.execute(
            "SELECT COUNT(*) FROM dm_deliveries WHERE discord_id=? AND status='pending'",
            (discord_id,),
        ).fetchone()[0]

    def dm_seed_states(self, rows):
        with self.db:
            result = self.db.executemany(
                "INSERT OR IGNORE INTO work_package_state "
                "(id,assignee_id,status_id,lock_version) VALUES(?,?,?,?)", rows
            )
        return result.rowcount

    def dm_failures(self):
        return [dict(r) for r in self.db.execute(
            "SELECT event_key,op_id,discord_id,error FROM dm_deliveries "
            "WHERE status='failed' ORDER BY created_at"
        )]

    def dm_retry_failed(self, op_id):
        with self.db:
            result = self.db.execute(
                "UPDATE dm_deliveries SET status='pending',error=NULL,next_attempt=0 "
                "WHERE status='failed' AND op_id=? AND discord_id=("
                "SELECT discord_id FROM dm_links WHERE op_id=? AND enabled=1)",
                (op_id, op_id),
            )
        return result.rowcount

    def dm_retry_one(self, event_key):
        with self.db:
            result = self.db.execute(
                "UPDATE dm_deliveries SET status='pending',error=NULL,next_attempt=0 "
                "WHERE event_key=? AND status='failed' AND EXISTS("
                "SELECT 1 FROM dm_links WHERE dm_links.op_id=dm_deliveries.op_id "
                "AND dm_links.discord_id=dm_deliveries.discord_id AND enabled=1)",
                (event_key,),
            )
        return result.rowcount == 1

    def inbound_retry_one(self, source):
        with self.db:
            result = self.db.execute(
                "UPDATE inbound_events SET status='pending',error=NULL,next_attempt=0,attempts=0 "
                "WHERE source_event_id=? AND status='failed' AND action!='legacy'",
                (source,),
            )
        return result.rowcount == 1

    def dm_test(self, op_id):
        with self.db:
            link = self.db.execute(
                "SELECT discord_id FROM dm_links WHERE op_id=? AND enabled=1", (op_id,)
            ).fetchone()
            if not link:
                return False
            pending = self.db.execute(
                "SELECT 1 FROM dm_deliveries WHERE op_id=? AND kind='test' "
                "AND status='pending' LIMIT 1", (op_id,)
            ).fetchone()
            if pending:
                return False
            self.db.execute(
                "INSERT INTO dm_deliveries "
                "(event_key,op_id,discord_id,kind,work_package_id,subject,project,"
                "detail,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"test:{op_id}:{secrets.token_hex(12)}", op_id, link["discord_id"],
                 "test", 0, "Kiểm tra thông báo", "", "", "", time.time()),
            )
        return True

    def dm_history(self, task_id=None, op_id=None, status="", limit=100):
        clauses, args = [], []
        if task_id is not None:
            clauses.append("work_package_id=?")
            args.append(task_id)
        if op_id is not None:
            clauses.append("op_id=?")
            args.append(op_id)
        if status:
            clauses.append("status=?")
            args.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return [dict(row) for row in self.db.execute(
            "SELECT event_key,source_event_id,work_package_id,kind,op_id,discord_id,"
            "status,attempts,error,created_at,delivered_at,metadata FROM dm_deliveries" + where +
            " ORDER BY created_at DESC,event_key DESC LIMIT ?", (*args, limit),
        )]

    def inbound_history(self, task_id=None, status="", limit=100):
        clauses, args = [], []
        if task_id is not None:
            clauses.append("work_package_id=?")
            args.append(task_id)
        if status:
            clauses.append("status=?")
            args.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return [dict(row) for row in self.db.execute(
            "SELECT source_event_id,work_package_id,action,status,attempts,error,received_at "
            "FROM inbound_events" + where + " ORDER BY received_at DESC LIMIT ?",
            (*args, limit),
        )]

    def audit_admin(self, actor_discord_id, action, target):
        with self.db:
            self.db.execute(
                "INSERT INTO admin_audit(actor_discord_id,action,target,at) VALUES(?,?,?,?)",
                (actor_discord_id, action, str(target)[:160], time.time()),
            )

    def admin_audit_recent(self, limit=100):
        return [dict(row) for row in self.db.execute(
            "SELECT actor_discord_id,action,target,at FROM admin_audit "
            "ORDER BY id DESC LIMIT ?", (limit,),
        )]

    def recipient_rule(self, project_id, role, event, default):
        if project_id is None:
            return default
        row = self.db.execute(
            "SELECT enabled FROM recipient_rules WHERE project_id=? AND role=? AND event=?",
            (project_id, role, event),
        ).fetchone()
        return bool(row[0]) if row else default

    def set_recipient_rule(self, project_id, role, event, enabled):
        with self.db:
            self.db.execute(
                "INSERT INTO recipient_rules(project_id,role,event,enabled) VALUES(?,?,?,?) "
                "ON CONFLICT(project_id,role,event) DO UPDATE SET enabled=excluded.enabled",
                (project_id, role, event, int(enabled)),
            )

    def recipient_rules(self):
        return [dict(row) for row in self.db.execute(
            "SELECT project_id,role,event,enabled FROM recipient_rules "
            "ORDER BY project_id,role,event"
        )]

    def bulk_link(self, rows):
        """Apply prevalidated links atomically, preserving the one-Discord-account rule."""
        with self.db:
            for op_id, discord_id, name in rows:
                old = self.db.execute(
                    "SELECT discord_id FROM dm_links WHERE op_id=?", (op_id,)
                ).fetchone()
                self.db.execute(
                    "INSERT INTO dm_links(op_id,discord_id,name,enabled) VALUES(?,?,?,1) "
                    "ON CONFLICT(op_id) DO UPDATE SET discord_id=excluded.discord_id,"
                    "name=excluded.name,enabled=1",
                    (op_id, discord_id, name[:200]),
                )
                if old is None or old["discord_id"] != discord_id:
                    self.db.execute(
                        "INSERT INTO dm_deliveries "
                        "(event_key,op_id,discord_id,kind,work_package_id,subject,"
                        "project,detail,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (f"welcome:{op_id}:{secrets.token_hex(12)}", op_id, discord_id,
                         "welcome", 0, "Chào mừng đến với thông báo OpenProject", "",
                         name[:200], "", time.time()),
                    )
