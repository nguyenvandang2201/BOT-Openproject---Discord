import ipaddress
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from dotenv import load_dotenv


def origin(value: str) -> str:
    value = value.rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("URLs must be HTTPS origins without credentials, paths or query strings")
    return value


def admin_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or parsed.scheme not in ("http", "https")):
        raise ValueError("ADMIN_URL must be an HTTP(S) origin")
    if parsed.scheme == "http":
        try:
            private = ipaddress.ip_address(parsed.hostname).is_private
        except ValueError:
            private = parsed.hostname == "localhost"
        if not private:
            raise ValueError("HTTP ADMIN_URL must use a private IP or localhost")
    return value.rstrip("/")


@dataclass(frozen=True)
class Config:
    op_url: str
    public_url: str
    client_id: str
    client_secret: str
    discord_token: str
    encryption_key: str
    db_path: str = "data/bridge.sqlite3"
    guild_id: int | None = None
    category_id: int | None = None
    poll_interval: int = 60
    max_pages: int = 100
    delivery_batch: int = 20
    port: int = 8080
    delivery_mode: str = "channel"
    webhook_secret: str = ""
    admin_url: str = ""
    multi_recipient_enabled: bool = False
    service_api_key: str = field(default="", repr=False)
    max_recipients_per_event: int = 50
    watchers_enabled: bool = False
    watcher_project_ids: tuple[int, ...] = ()
    watcher_reconcile_seconds: int = 0
    groups_enabled: bool = False
    group_project_ids: tuple[int, ...] = ()
    accountable_enabled: bool = False
    detail_notifications_enabled: bool = False
    attachment_notifications_enabled: bool = False
    reminders_enabled: bool = False
    reminder_hour: int = 9
    notification_mode: str = "instant"
    digest_hour: int = 17
    quiet_start: str = ""
    quiet_end: str = ""
    user_preferences_enabled: bool = True

    @property
    def redirect_uri(self) -> str:
        return self.public_url + "/oauth/callback"

    @classmethod
    def from_env(cls):
        load_dotenv()

        def required(key):
            value = os.environ.get(key, "").strip()
            if not value:
                raise ValueError(f"Missing {key}")
            return value

        key = required("TOKEN_ENCRYPTION_KEY")
        Fernet(key.encode())
        mode = os.getenv("DELIVERY_MODE", "channel").strip()
        multi_recipient = os.getenv("MULTI_RECIPIENT_ENABLED", "false").strip().lower()
        if multi_recipient not in ("true", "false"):
            raise ValueError("MULTI_RECIPIENT_ENABLED must be true or false")
        watchers = os.getenv("WATCHERS_ENABLED", "false").strip().lower()
        if watchers not in ("true", "false"):
            raise ValueError("WATCHERS_ENABLED must be true or false")
        groups = os.getenv("GROUPS_ENABLED", "false").strip().lower()
        if groups not in ("true", "false"):
            raise ValueError("GROUPS_ENABLED must be true or false")
        accountable = os.getenv("ACCOUNTABLE_ENABLED", "false").strip().lower()
        if accountable not in ("true", "false"):
            raise ValueError("ACCOUNTABLE_ENABLED must be true or false")
        def flag(name, default="false"):
            value = os.getenv(name, default).strip().lower()
            if value not in ("true", "false"):
                raise ValueError(f"{name} must be true or false")
            return value == "true"

        def clock(value):
            import re
            if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
                raise ValueError("Quiet hours must use HH:MM")
            return value

        quiet_start = os.getenv("QUIET_START", "").strip()
        quiet_end = os.getenv("QUIET_END", "").strip()
        if bool(quiet_start) != bool(quiet_end):
            raise ValueError("QUIET_START and QUIET_END must both be set")
        if quiet_start:
            clock(quiet_start)
            clock(quiet_end)
        projects = os.getenv("WATCHER_PROJECT_IDS", "").strip()
        watcher_project_ids = tuple(int(value.strip()) for value in projects.split(",")) if projects else ()
        group_projects = os.getenv("GROUP_PROJECT_IDS", "").strip()
        group_project_ids = (
            tuple(int(value.strip()) for value in group_projects.split(","))
            if group_projects else ()
        )
        result = cls(
            op_url=origin(required("OPENPROJECT_URL")),
            public_url=origin(os.environ["PUBLIC_URL"]) if os.getenv("PUBLIC_URL") else "",
            client_id=os.getenv("OPENPROJECT_CLIENT_ID", "").strip(),
            client_secret=os.getenv("OPENPROJECT_CLIENT_SECRET", "").strip(),
            discord_token=required("DISCORD_BOT_TOKEN"),
            encryption_key=key,
            db_path=os.getenv("DATABASE_PATH", "data/bridge.sqlite3"),
            guild_id=int(required("DISCORD_GUILD_ID")),
            category_id=(
                int(required("DISCORD_CATEGORY_ID")) if mode == "channel"
                else int(os.getenv("DISCORD_CATEGORY_ID", "0") or "0") or None
            ),
            poll_interval=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
            max_pages=int(os.getenv("MAX_NOTIFICATION_PAGES", "100")),
            delivery_batch=int(os.getenv("DELIVERY_BATCH_SIZE", "20")),
            port=int(os.getenv("HTTP_PORT", "8080")),
            delivery_mode=mode,
            webhook_secret=os.getenv("OPENPROJECT_WEBHOOK_SECRET", "").strip(),
            admin_url=admin_origin(os.environ["ADMIN_URL"]) if os.getenv("ADMIN_URL") else "",
            multi_recipient_enabled=multi_recipient == "true",
            service_api_key=os.getenv("OPENPROJECT_SERVICE_API_KEY", "").strip(),
            max_recipients_per_event=int(os.getenv("MAX_RECIPIENTS_PER_EVENT", "50")),
            watchers_enabled=watchers == "true",
            watcher_project_ids=watcher_project_ids,
            watcher_reconcile_seconds=int(os.getenv(
                "WATCHER_RECONCILE_SECONDS", "300" if watchers == "true" else "0"
            )),
            groups_enabled=groups == "true",
            group_project_ids=group_project_ids,
            accountable_enabled=accountable == "true",
            detail_notifications_enabled=flag("DETAIL_NOTIFICATIONS_ENABLED"),
            attachment_notifications_enabled=flag("ATTACHMENT_NOTIFICATIONS_ENABLED"),
            reminders_enabled=flag("REMINDERS_ENABLED"),
            reminder_hour=int(os.getenv("REMINDER_HOUR", "9")),
            notification_mode=os.getenv("NOTIFICATION_MODE", "instant").strip(),
            digest_hour=int(os.getenv("DIGEST_HOUR", "17")),
            quiet_start=quiet_start,
            quiet_end=quiet_end,
            user_preferences_enabled=flag("USER_PREFERENCES_ENABLED", "true"),
        )
        if (
            result.poll_interval < 15
            or result.max_pages < 1
            or result.delivery_batch < 1
            or not 1 <= result.port <= 65535
            or result.guild_id < 1
            or (result.delivery_mode == "channel" and result.category_id < 1)
            or result.delivery_mode not in ("channel", "webhook_dm")
            or (result.delivery_mode == "webhook_dm" and len(result.webhook_secret) < 32)
            or bool(result.client_id) != bool(result.client_secret)
            or (result.multi_recipient_enabled and result.delivery_mode != "webhook_dm")
            or result.max_recipients_per_event < 1
            or (result.watchers_enabled and
                (not result.multi_recipient_enabled or not result.service_api_key))
            or (result.groups_enabled and
                (not result.multi_recipient_enabled or not result.service_api_key))
            or (result.accountable_enabled and
                (not result.multi_recipient_enabled or not result.service_api_key))
            or (result.detail_notifications_enabled and not result.multi_recipient_enabled)
            or any(project_id < 1 for project_id in result.watcher_project_ids)
            or any(project_id < 1 for project_id in result.group_project_ids)
            or (result.watcher_reconcile_seconds != 0 and
                (result.watcher_reconcile_seconds < 60 or not result.watchers_enabled))
            or result.notification_mode not in ("instant", "daily")
            or not 0 <= result.reminder_hour <= 23
            or not 0 <= result.digest_hour <= 23
            or ((result.attachment_notifications_enabled or result.reminders_enabled)
                and (not result.multi_recipient_enabled or not result.service_api_key))
        ):
            raise ValueError("Invalid polling, page, batch or port configuration")
        return result
