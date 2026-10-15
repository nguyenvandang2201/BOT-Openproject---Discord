import hashlib
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
        self.cipher = Fernet(key.encode())
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
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
            self.db.execute("PRAGMA user_version=1")

    def close(self):
        self.db.close()

    def seal(self, value):
        return self.cipher.encrypt(json.dumps(value).encode())

    def unseal(self, value):
        return json.loads(self.cipher.decrypt(value))

    def cleanup(self):
        with self.db:
            self.db.execute("DELETE FROM flows WHERE expires < ?", (time.time(),))

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
