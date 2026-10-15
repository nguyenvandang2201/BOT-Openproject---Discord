import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from op_discord.admin import AdminError, execute
from op_discord.bot import BridgeBot
from op_discord.channel_policy import (
    VIEW_CHANNEL,
    channel_is_private,
    private_overwrites,
)
from op_discord.openproject import OpenProject
from op_discord.service import DeliveryError, Service
from op_discord.store import Store


def details(config, uid=100, bot_id=999):
    return {
        "type": 0,
        "guild_id": str(config.guild_id),
        "parent_id": str(config.category_id),
        "permission_overwrites": private_overwrites(config.guild_id, uid, bot_id),
    }


def test_private_channel_policy_rejects_extra_viewer_and_wrong_parent(config):
    channel = details(config)
    assert channel_is_private(channel, config.guild_id, config.category_id, 100, 999)
    channel["permission_overwrites"].append(
        {"id": "555", "type": 0, "allow": str(VIEW_CHANNEL), "deny": "0"}
    )
    assert not channel_is_private(channel, config.guild_id, config.category_id, 100, 999)
    channel = details(config)
    channel["parent_id"] = "777"
    assert not channel_is_private(channel, config.guild_id, config.category_id, 100, 999)


def test_old_account_migrates_without_becoming_sendable(config):
    db = sqlite3.connect(config.db_path)
    db.execute(
        "CREATE TABLE accounts (discord_id TEXT PRIMARY KEY, op_id INTEGER NOT NULL UNIQUE, "
        "name TEXT NOT NULL, tokens BLOB NOT NULL, status TEXT NOT NULL DEFAULT 'active', "
        "error TEXT, failures INTEGER NOT NULL DEFAULT 0, next_poll REAL NOT NULL DEFAULT 0, "
        "last_poll REAL)"
    )
    from cryptography.fernet import Fernet

    encrypted = Fernet(config.encryption_key.encode()).encrypt(
        json.dumps({"access_token": "legacy"}).encode()
    )
    db.execute(
        "INSERT INTO accounts(discord_id,op_id,name,tokens) VALUES(?,?,?,?)",
        ("100", 10, "Alice", encrypted),
    )
    db.commit()
    db.close()
    migrated = Store(config.db_path, config.encryption_key)
    try:
        account = migrated.account("100")
        assert account["status"] == "migration_pending"
        assert account["credential_kind"] == "oauth"
        assert account["channel_id"] is None
        assert migrated.accounts() == []
        migrated.assign_channel("100", "1", "1100")
        assert migrated.account("100")["status"] == "active"
    finally:
        migrated.close()


def test_api_key_provision_is_encrypted_and_baselined(config, store):
    store.provision_api_key("100", 10, "Alice", "private-api-token", "1", "1100", [1, 2])
    account = store.account("100")
    assert account["credential_kind"] == "api_key"
    assert b"private-api-token" not in account["tokens"]
    assert store.unseal(account["tokens"])["api_key"] == "private-api-token"
    assert store.jobs("100", 10) == []
    store.discover("100", [1, 2, 3])
    assert [job["notification_id"] for job in store.jobs("100", 10)] == [3]


async def test_api_key_uses_basic_auth(config):
    def upstream(request):
        assert request.headers["Authorization"] == "Basic YXBpa2V5OnByaXZhdGU="
        return httpx.Response(200, json={"_type": "User", "id": 10})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        me = await OpenProject(config, http).me(httpx.BasicAuth("apikey", "private"))
    assert me["id"] == 10


async def test_permanent_channel_error_preserves_queue(config, store, api):
    store.provision_api_key("100", 10, "Alice", "key", "1", "1100", [])
    api.notifications.return_value = [4]
    send = AsyncMock(side_effect=DeliveryError("channel_permissions_changed", permanent=True))
    await Service(config, store, api, send).poll_account("100")
    assert store.pending_count("100") == 1
    assert store.account("100")["status"] == "needs_attention"
    assert store.accounts() == []


async def test_bot_sends_to_channel_only(config, store, monkeypatch):
    store.provision_api_key("100", 10, "Alice", "key", "1", "1100", [])
    guild = SimpleNamespace(id=config.guild_id, fetch_member=AsyncMock())
    channel = SimpleNamespace(
        guild=guild, category_id=config.category_id, overwrites={}, send=AsyncMock()
    )
    monkeypatch.setattr("op_discord.bot.discord.TextChannel", SimpleNamespace)
    monkeypatch.setattr("op_discord.bot.channel_is_private", lambda *args: True)
    async with BridgeBot(config, store, OpenProject(config, None)) as bot:
        bot._connection.user = SimpleNamespace(id=999)
        bot.get_guild = lambda guild_id: guild
        bot.fetch_channel = AsyncMock(return_value=channel)
        await bot.send_notification("100", {"id": 4, "_type": "Notification"})
    channel.send.assert_awaited_once()
    assert bot.fetch_channel.await_args.args == (1100,)


async def test_admin_link_creates_private_channel_after_baseline(config, store, monkeypatch):
    monkeypatch.setattr("op_discord.admin.getpass.getpass", lambda prompt: "private")
    created = details(config)
    created["id"] = "1100"
    calls = []

    def upstream(request):
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": "999"})
        if path == "/api/v10/guilds/1/members/100":
            return httpx.Response(200, json={"user": {"id": "100"}})
        if path == "/api/v10/channels/2":
            return httpx.Response(200, json={"id": "2", "guild_id": "1", "type": 4})
        if path == "/api/v3/users/me":
            assert request.headers["Authorization"] == "Basic YXBpa2V5OnByaXZhdGU="
            return httpx.Response(200, json={"_type": "User", "id": 10, "name": "Alice"})
        if path == "/api/v3/notifications":
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "_embedded": {"elements": [{"_type": "Notification", "id": 5}]},
                    "_links": {},
                },
            )
        if path == "/api/v10/guilds/1/channels":
            posted = json.loads(request.content)
            assert posted["permission_overwrites"] == created["permission_overwrites"]
            return httpx.Response(201, json=created)
        if path == "/api/v10/channels/1100":
            return httpx.Response(200, json=created)
        raise AssertionError(path)

    await execute(config, store, "link", 100, transport=httpx.MockTransport(upstream))
    assert store.account("100")["channel_id"] == "1100"
    assert store.account("100")["status"] == "active"
    assert store.jobs("100", 10) == []
    assert calls.index(("GET", "/api/v3/notifications")) < calls.index(
        ("POST", "/api/v10/guilds/1/channels")
    )


async def test_admin_unlink_revokes_access_before_erasing_account(config, store):
    store.provision_api_key("100", 10, "Alice", "private", "1", "1100", [])
    channel = details(config)
    channel["id"] = "1100"
    calls = []

    def upstream(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": "999"})
        if request.url.path == "/api/v10/channels/1100":
            return httpx.Response(200, json=channel)
        if request.url.path == "/api/v10/channels/1100/permissions/100":
            assert store.account("100") is not None
            return httpx.Response(204)
        raise AssertionError(request.url.path)

    await execute(config, store, "unlink", 100, transport=httpx.MockTransport(upstream))
    assert store.account("100") is None
    assert calls[-1] == ("PUT", "/api/v10/channels/1100/permissions/100")


async def test_admin_unlink_does_not_erase_account_if_revoke_fails(config, store):
    store.provision_api_key("100", 10, "Alice", "private", "1", "1100", [])
    channel = details(config)
    channel["id"] = "1100"

    def upstream(request):
        if request.url.path == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": "999"})
        if request.url.path == "/api/v10/channels/1100":
            return httpx.Response(200, json=channel)
        return httpx.Response(403)

    with pytest.raises(AdminError):
        await execute(config, store, "unlink", 100, transport=httpx.MockTransport(upstream))
    assert store.account("100") is not None
