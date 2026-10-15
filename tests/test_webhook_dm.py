import asyncio
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import httpx

from op_discord.bot import BridgeBot
from op_discord.service import DeliveryError
from op_discord.web import create_app
from op_discord.webhook import WebhookService


def wp(version=0, assignee=10, status=1):
    return {
        "_type": "WorkPackage",
        "id": 42,
        "lockVersion": version,
        "subject": "Fix login",
        "_links": {
            "assignee": {"href": f"/api/v3/users/{assignee}"} if assignee else {"href": None},
            "status": {"href": f"/api/v3/statuses/{status}", "title": f"State {status}"},
            "project": {"href": "/api/v3/projects/1", "title": "Example"},
        },
    }


def signed(payload, secret="x" * 40):
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
    return body, {"X-OP-Signature": f"sha1={signature}"}


async def test_webhook_auth_and_assignment_status_idempotency(store, api):
    store.dm_link(10, "100", "Alice")
    app = create_app(store, api, webhook_secret="x" * 40)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://bot.example"
    ) as http:
        path = "/webhooks/openproject"
        first = {"action": "work_package:created", "work_package": wp()}
        body, headers = signed(first)
        assert (await http.post(path, content=body)).status_code == 401
        assert (await http.post(path, content=body, headers=headers)).json()["queued"] == 1
        assert (await http.post(path, content=body, headers=headers)).json()["queued"] == 0

        changed = {"action": "work_package:updated", "work_package": wp(1, status=2)}
        body, headers = signed(changed)
        assert (await http.post(path, content=body, headers=headers)).json()["queued"] == 1
        unrelated = {"action": "work_package:updated", "work_package": wp(2, status=2)}
        body, headers = signed(unrelated)
        assert (await http.post(path, content=body, headers=headers)).json()["queued"] == 0
        assert [job["kind"] for job in store.dm_jobs(10)] == ["assignment", "status"]


async def test_rejects_oversized_and_internal_comment_payloads(store, api):
    app = create_app(store, api, webhook_secret="x" * 40)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://bot.example"
    ) as http:
        oversized = await http.post("/webhooks/openproject", content=b"a" * 1048577)
        assert oversized.status_code == 413
        body, headers = signed({"action": "work_package_comment:internal_comment"})
        ignored = await http.post("/webhooks/openproject", content=body, headers=headers)
        assert ignored.status_code == 202
        assert ignored.json()["queued"] == 0


async def test_comment_uses_embedded_work_package_and_link_changes_cancel_pending(store, api, config):
    store.dm_link(10, "100")
    activity = {
        "id": 99,
        "internal": False,
        "comment": {"raw": "Please check this"},
        "_embedded": {"workPackage": wp(3), "user": {"name": "Bob"}},
    }
    app = create_app(store, api, webhook_secret="x" * 40)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://bot.example"
    ) as http:
        body, headers = signed({"action": "work_package_comment:comment", "activity": activity})
        response = await http.post("/webhooks/openproject", content=body, headers=headers)
        assert response.json()["queued"] == 1
        assert (await http.post("/webhooks/openproject", content=body, headers=headers)).json()[
            "queued"
        ] == 0
    job = store.dm_jobs(1)[0]
    assert job["detail"] == "Please check this"
    store.dm_link(10, "200")
    sender = AsyncMock()
    service = WebhookService(config, store, sender)
    # Run a single queue pass, then cancel the sleep.
    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    sender.assert_not_awaited()
    assert store.dm_jobs(1) == []


async def test_unseen_update_baselines_without_false_assignment(store, api):
    store.dm_link(10, "100")
    app = create_app(store, api, webhook_secret="x" * 40)
    body, headers = signed({"action": "work_package:updated", "work_package": wp(7)})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://bot.example"
    ) as http:
        response = await http.post("/webhooks/openproject", content=body, headers=headers)
    assert response.json()["queued"] == 0
    assert store.dm_jobs(10) == []


async def test_seeded_existing_work_package_detects_first_real_change(store, api):
    store.dm_link(10, "100")
    assert store.dm_seed_states([(42, 10, 1, 5)]) == 1
    app = create_app(store, api, webhook_secret="x" * 40)
    body, headers = signed({"action": "work_package:updated", "work_package": wp(6, status=2)})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://bot.example"
    ) as http:
        response = await http.post("/webhooks/openproject", content=body, headers=headers)
    assert response.json()["queued"] == 1
    assert store.dm_jobs(1)[0]["kind"] == "status"


async def test_dm_worker_retries_transient_failures(store, config):
    store.dm_link(10, "100")
    store.accept_webhook({
        "action": "work_package:created",
        "work_package": {
            "id": 42, "lock_version": 0, "assignee_id": 10, "status_id": 1,
            "status_title": "New", "subject": "Task", "project": "P",
        },
        "actor": "Manager",
    })
    sender = AsyncMock(side_effect=DeliveryError())
    service = WebhookService(config, store, sender)
    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert sender.await_count == 1
    row = store.db.execute("SELECT status,attempts FROM dm_deliveries").fetchone()
    assert tuple(row) == ("pending", 1)


async def test_discord_dm_uses_member_and_escapes_mentions(config, store, api):
    member = AsyncMock()
    guild = AsyncMock()
    guild.fetch_member.return_value = member
    job = {
        "discord_id": "100", "kind": "comment", "subject": "@everyone Fix login",
        "work_package_id": 42, "project": "Example", "detail": "@here Please check",
        "actor": "Bob",
    }
    async with BridgeBot(config, store, api) as bot:
        with patch.object(bot, "get_guild", return_value=guild):
            await bot.send_dm(job)
    member.send.assert_awaited_once()
    embed = member.send.await_args.kwargs["embed"]
    assert "@everyone" not in embed.title
    assert "@here" not in embed.fields[1].value
    assert embed.url == "https://op.example/work_packages/42"
