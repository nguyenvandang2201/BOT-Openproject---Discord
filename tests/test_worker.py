import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from conftest import activate

from op_discord.openproject import APIError
from op_discord.service import DeliveryError, Service
from op_discord.store import Store


async def test_baseline_dedup_and_restart(config, store, api):
    activate(store, ids=[1, 2])
    api.notifications.return_value = [1, 2, 3]
    send = AsyncMock()
    service = Service(config, store, api, send)
    await service.poll_account("100")
    await service.poll_account("100")
    send.assert_awaited_once_with("100", {"_type": "Notification", "id": 3})
    reopened = Store(config.db_path, config.encryption_key)
    try:
        await Service(config, reopened, api, send).poll_account("100")
        assert send.await_count == 1
    finally:
        reopened.close()


async def test_personal_isolation(config, store, api):
    activate(store, "100", 10)
    activate(store, "200", 20)
    store.save_tokens(
        "200",
        {
            "access_token": "second-user-token",
            "refresh_token": "second-refresh",
            "expires_at": time.time() + 7200,
        },
    )
    api.notifications.side_effect = lambda token: [1] if token == "access-secret" else [2]
    send = AsyncMock()
    service = Service(config, store, api, send)
    await service.poll_account("100")
    await service.poll_account("200")
    assert [(c.args[0], c.args[1]["id"]) for c in send.await_args_list] == [("100", 1), ("200", 2)]
    assert [c.args[0] for c in api.notification.await_args_list] == [
        "access-secret",
        "second-user-token",
    ]


async def test_blocked_dm_retries_durably(config, store, api):
    activate(store)
    api.notifications.return_value = [1, 2]
    send = AsyncMock(side_effect=DeliveryError("discord_dm_blocked"))
    await Service(config, store, api, send).poll_account("100")
    assert store.pending_count("100") == 2
    assert store.account("100")["error"] == "discord_dm_blocked"
    assert send.await_count == 1
    reopened = Store(config.db_path, config.encryption_key)
    try:
        with reopened.db:
            reopened.db.execute("UPDATE deliveries SET next_attempt=0")
        send.side_effect = None
        await Service(config, reopened, api, send).poll_account("100")
        assert reopened.pending_count("100") == 0
    finally:
        reopened.close()


async def test_revoked_visibility_not_sent(config, store, api):
    activate(store)
    api.notifications.return_value = [1]
    api.notification.side_effect = APIError(404)
    send = AsyncMock()
    await Service(config, store, api, send).poll_account("100")
    send.assert_not_awaited()
    assert store.pending_count("100") == 0


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_permission_errors_suspend_without_deleting_queue(config, store, api, status):
    activate(store)
    store.discover("100", [1])
    api.notifications.side_effect = APIError(status)
    send = AsyncMock()
    await Service(config, store, api, send).poll_account("100")
    assert store.account("100")["status"] == "needs_attention"
    assert store.pending_count("100") == 1
    assert store.accounts() == []
    send.assert_not_awaited()


async def test_refresh_persisted_before_notification_fetch(config, store, api):
    activate(store, expiry=0)
    api.refresh.return_value = {
        "access_token": "new",
        "refresh_token": "rotated",
        "expires_at": time.time() + 7200,
    }

    async def notifications(token):
        assert token == "new"
        assert store.unseal(store.account("100")["tokens"])["refresh_token"] == "rotated"
        return []

    api.notifications.side_effect = notifications
    await Service(config, store, api, AsyncMock()).poll_account("100")
    api.refresh.assert_awaited_once()


async def test_unlink_serializes_with_inflight_poll(config, store, api):
    activate(store)
    entered, release = asyncio.Event(), asyncio.Event()

    async def notifications(token):
        entered.set()
        await release.wait()
        return [1]

    api.notifications.side_effect = notifications
    send = AsyncMock()
    service = Service(config, store, api, send)
    poll = asyncio.create_task(service.poll_account("100"))
    await entered.wait()
    unlink = asyncio.create_task(service.unlink("100"))
    await asyncio.sleep(0)
    assert not unlink.done()
    release.set()
    await asyncio.gather(poll, unlink)
    count = send.await_count
    await service.poll_account("100")
    assert send.await_count == count
    assert store.account("100") is None
