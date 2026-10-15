import sqlite3
import time
from unittest.mock import AsyncMock

import pytest
from conftest import activate, pending

from op_discord.service import Service
from op_discord.store import Store


def test_state_bound_to_browser_and_single_use(store):
    ticket = store.start_flow("100")
    state, browser = store.begin_oauth(ticket)
    assert store.begin_oauth(ticket) is None
    assert store.consume_state(state, "wrong-browser") is None
    assert store.consume_state(state, browser)["discord_id"] == "100"
    assert store.consume_state(state, browser) is None


def test_expired_state_rejected(store):
    ticket = store.start_flow("100")
    state, browser = store.begin_oauth(ticket)
    with store.db:
        store.db.execute("UPDATE flows SET expires=0")
    assert store.consume_state(state, browser) is None


def test_replaced_flow_and_unlink_invalidate_inflight_callback(store):
    state, browser = store.begin_oauth(store.start_flow("100"))
    flow = store.consume_state(state, browser)
    store.start_flow("100")
    assert store.set_pending("100", flow["expires"], {}) is None
    store.unlink("100")
    assert store.set_pending("100", flow["expires"], {}) is None


async def test_confirm_requires_original_discord_identity(config, store, api):
    code = pending(store)
    service = Service(config, store, api, AsyncMock())
    with pytest.raises(ValueError):
        await service.confirm("200", code)
    assert store.account("100") is None
    assert await service.confirm("100", code) == "Alice"
    with pytest.raises(ValueError):
        await service.confirm("100", code)


async def test_failed_baseline_never_activates(config, store, api):
    code = pending(store)
    api.notifications.side_effect = RuntimeError("page 2 failed")
    with pytest.raises(RuntimeError):
        await Service(config, store, api, AsyncMock()).confirm("100", code)
    assert store.account("100") is None


def test_one_to_one_mapping_and_transaction_rollback(store):
    activate(store)
    code = pending(store, "200", 10)
    with pytest.raises(sqlite3.IntegrityError):
        store.activate("200", code, [9])
    assert store.account("100")["op_id"] == 10
    assert store.account("200") is None
    assert store.pending("200", code)


def test_tokens_encrypted_persist_and_unlink_cascades(config, store):
    activate(store)
    row = store.account("100")
    assert b"access-secret" not in row["tokens"]
    assert b"refresh-secret" not in row["tokens"]
    store.discover("100", [1])
    second = Store(config.db_path, config.encryption_key)
    try:
        assert second.unseal(second.account("100")["tokens"])["access_token"] == "access-secret"
        assert second.pending_count("100") == 1
    finally:
        second.close()
    store.unlink("100")
    assert store.account("100") is None
    assert store.pending_count("100") == 0


async def test_expired_pending_confirmation(config, store, api):
    code = pending(store, expiry=time.time() - 1)
    with pytest.raises(ValueError):
        await Service(config, store, api, AsyncMock()).confirm("100", code)
    assert store.account("100") is None
