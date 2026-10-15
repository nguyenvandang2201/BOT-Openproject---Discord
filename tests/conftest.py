import time
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

from op_discord.config import Config
from op_discord.store import Store


@pytest.fixture
def config(tmp_path):
    return Config(
        op_url="https://op.example",
        public_url="https://bot.example",
        client_id="client",
        client_secret="client-secret",
        discord_token="discord-secret",
        encryption_key=Fernet.generate_key().decode(),
        db_path=str(tmp_path / "db.sqlite3"),
        guild_id=1,
        category_id=2,
    )


@pytest.fixture
def store(config):
    result = Store(config.db_path, config.encryption_key)
    yield result
    result.close()


@pytest.fixture
def api():
    result = AsyncMock()
    result.me.return_value = {"_type": "User", "id": 10, "name": "Alice"}
    result.notifications.return_value = []
    result.notification.side_effect = lambda token, nid: {"_type": "Notification", "id": nid}
    return result


def pending(store, uid="100", op_id=10, expiry=None):
    ticket = store.start_flow(uid)
    state, browser = store.begin_oauth(ticket)
    flow = store.consume_state(state, browser)
    return store.set_pending(
        uid,
        flow["expires"],
        {
            "id": op_id,
            "name": "Alice",
            "tokens": {
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "expires_at": expiry if expiry is not None else time.time() + 7200,
            },
        },
    )


def activate(store, uid="100", op_id=10, ids=(), expiry=None):
    code = pending(store, uid, op_id, expiry)
    store.activate(uid, code, list(ids))
    store.assign_channel(uid, "1", str(1000 + int(uid)))
