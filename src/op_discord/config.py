import os
from dataclasses import dataclass
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
        result = cls(
            op_url=origin(required("OPENPROJECT_URL")),
            public_url=origin(os.environ["PUBLIC_URL"]) if os.getenv("PUBLIC_URL") else "",
            client_id=os.getenv("OPENPROJECT_CLIENT_ID", "").strip(),
            client_secret=os.getenv("OPENPROJECT_CLIENT_SECRET", "").strip(),
            discord_token=required("DISCORD_BOT_TOKEN"),
            encryption_key=key,
            db_path=os.getenv("DATABASE_PATH", "data/bridge.sqlite3"),
            guild_id=int(required("DISCORD_GUILD_ID")),
            category_id=int(required("DISCORD_CATEGORY_ID")),
            poll_interval=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
            max_pages=int(os.getenv("MAX_NOTIFICATION_PAGES", "100")),
            delivery_batch=int(os.getenv("DELIVERY_BATCH_SIZE", "20")),
            port=int(os.getenv("HTTP_PORT", "8080")),
        )
        if (
            result.poll_interval < 15
            or result.max_pages < 1
            or result.delivery_batch < 1
            or not 1 <= result.port <= 65535
            or result.guild_id < 1
            or result.category_id < 1
            or bool(result.client_id) != bool(result.client_secret)
        ):
            raise ValueError("Invalid polling, page, batch or port configuration")
        return result
