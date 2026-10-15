import asyncio
import dataclasses
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from op_discord.bot import dm_card
from op_discord.config import Config
from op_discord.openproject import APIError, OpenProject, ProtocolError
from op_discord.webhook import WebhookService, parse_event


def package(version=0, assignee="/api/v3/groups/7", status=1):
    return {
        "_type": "WorkPackage", "id": 42, "lockVersion": version, "subject": "Task",
        "_links": {
            "assignee": {"href": assignee, "title": "Team Seven"},
            "status": {"href": f"/api/v3/statuses/{status}", "title": f"Status {status}"},
            "project": {"href": "/api/v3/projects/1", "title": "Test"},
        },
    }


def event(action="work_package:created", version=0, assignee="/api/v3/groups/7", status=1):
    return parse_event(json.dumps({
        "action": action, "work_package": package(version, assignee, status),
        "actor": {"name": "Manager"},
    }).encode())


def enabled(config, **overrides):
    return dataclasses.replace(
        config, multi_recipient_enabled=True, groups_enabled=True,
        service_api_key="api-key", **overrides,
    )


def group(members=(10, 11, 12), *, links=True, embedded=True):
    return {
        "_type": "Group", "id": 7,
        "_links": {"members": [
            {"href": f"/api/v3/users/{user_id}"} for user_id in members
        ]} if links else {},
        "_embedded": {"members": [
            {"_type": "User", "id": user_id} for user_id in members
        ]} if embedded else {},
    }


@pytest.mark.asyncio
async def test_group_api_reads_members_with_service_account(config):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=group())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        assert await OpenProject(enabled(config), http).group_members(7) == {10, 11, 12}
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/v3/groups/7"
    assert seen[0].headers["authorization"].startswith("Basic ")


@pytest.mark.asyncio
async def test_group_api_supports_one_member_representation_or_empty_group(config):
    for response in (group(links=False), group(embedded=False), group(())):
        transport = httpx.MockTransport(lambda _, response=response: httpx.Response(
            200, json=response
        ))
        async with httpx.AsyncClient(transport=transport) as http:
            expected = set() if not response["_links"].get("members") else {10, 11, 12}
            if response["_embedded"].get("members"):
                expected = {10, 11, 12}
            assert await OpenProject(enabled(config), http).group_members(7) == expected


@pytest.mark.asyncio
async def test_group_api_filters_inactive_members(config):
    response = group((10, 11))
    response["_embedded"]["members"][1]["status"] = "locked"
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    async with httpx.AsyncClient(transport=transport) as http:
        assert await OpenProject(enabled(config), http).group_members(7) == {10}


@pytest.mark.asyncio
async def test_group_api_rejects_missing_or_mismatched_members(config):
    wrong = [
        {"_type": "Group", "id": 7, "_links": {}, "_embedded": {}},
        group((10, 11)) | {"id": 8},
        group((10, 11)) | {"_embedded": {"members": [
            {"_type": "User", "id": 10}
        ]}},
        group((10,)) | {"_links": {"members": [{
            "href": "https://evil.example/api/v3/users/10"
        }]}},
        group((10,)) | {"_embedded": {"members": [
            {"_type": "Group", "id": 10}
        ]}},
    ]
    for response in wrong:
        transport = httpx.MockTransport(lambda _, response=response: httpx.Response(
            200, json=response
        ))
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(ProtocolError):
                await OpenProject(enabled(config), http).group_members(7)
    response = group((10, 11, 12))
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(ProtocolError, match="limit"):
            await OpenProject(enabled(config, max_recipients_per_event=2), http).group_members(7)


@pytest.mark.asyncio
async def test_group_fans_out_to_linked_members_and_deduplicates_watcher(store, config):
    for op_id in (10, 11):
        store.dm_link(op_id, str(op_id * 10), welcome=False)
    api = AsyncMock()
    api.group_members.return_value = {10, 11, 12}
    api.watchers.return_value = {11}
    service = WebhookService(
        enabled(config, watchers_enabled=True, watcher_reconcile_seconds=0),
        store, AsyncMock(), api,
    )
    store.enqueue_event(event())
    await service.resolve_pending()
    jobs = sorted(store.dm_jobs(10), key=lambda job: job["op_id"])
    assert [(job["op_id"], job["kind"]) for job in jobs] == [
        (10, "assignment"), (11, "assignment")
    ]
    assert json.loads(jobs[1]["metadata"])["recipient_roles"] == [
        "group_assignee", "watcher"
    ]
    assert json.loads(jobs[0]["metadata"])["assignee_group_id"] == 7
    assert "Nhóm" in dm_card(jobs[0], config.op_url)[0].title
    assert any("Đang theo dõi" in field.value for field in dm_card(
        jobs[1], config.op_url
    )[0].fields)
    assert len(store.dm_jobs(10)) == 2


@pytest.mark.asyncio
async def test_group_member_receives_status_and_comment_then_stops_after_reassignment(
    store, config
):
    store.dm_link(10, "100", welcome=False)
    api = AsyncMock()
    api.group_members.return_value = {10}
    service = WebhookService(enabled(config), store, AsyncMock(), api)
    for item in (
        event(), event("work_package:updated", 1, status=2),
        parse_event(json.dumps({
            "action": "work_package_comment:comment",
            "activity": {
                "id": 900, "internal": False, "comment": {"raw": "Please review"},
                "_embedded": {"workPackage": package(1, status=2),
                              "user": {"name": "Manager"}},
            },
        }).encode()),
    ):
        store.enqueue_event(item)
        await service.resolve_pending()
    assert [job["kind"] for job in store.dm_jobs(10)] == [
        "assignment", "status", "comment"
    ]
    store.enqueue_event(event("work_package:updated", 2, "/api/v3/users/11", 3))
    await service.resolve_pending()
    assert len(store.dm_jobs(10)) == 3
    api.group_members.assert_awaited()


@pytest.mark.asyncio
async def test_removed_member_or_reassigned_group_is_cancelled_before_delivery(store, config):
    store.dm_link(10, "100", welcome=False)
    api = AsyncMock()
    api.group_members.return_value = {10}
    service = WebhookService(enabled(config), store, AsyncMock(), api)
    store.enqueue_event(event())
    await service.resolve_pending()
    job = store.dm_jobs(10)[0]
    api.service_get.return_value = package(0)
    api.group_members.return_value = set()
    assert not await service.delivery_eligible(job, {}, {}, {})
    api.group_members.return_value = {10}
    api.service_get.return_value = package(1, "/api/v3/users/11")
    assert not await service.delivery_eligible(job, {}, {}, {})


@pytest.mark.asyncio
async def test_watcher_role_still_qualifies_after_group_removal(store, config):
    store.dm_link(10, "100", welcome=False)
    api = AsyncMock()
    api.group_members.return_value = {10}
    api.watchers.return_value = {10}
    service = WebhookService(
        enabled(config, watchers_enabled=True, watcher_reconcile_seconds=0),
        store, AsyncMock(), api,
    )
    store.enqueue_event(event())
    await service.resolve_pending()
    job = store.dm_jobs(10)[0]
    api.service_get.return_value = package(1, "/api/v3/users/11")
    api.watchers.return_value = {10}
    assert await service.delivery_eligible(job, {}, {}, {})


@pytest.mark.asyncio
async def test_group_project_allowlist_fails_explicitly(store, config):
    api = AsyncMock()
    store.enqueue_event(event())
    await WebhookService(enabled(config, group_project_ids=(99,)), store,
                         AsyncMock(), api).resolve_pending()
    api.group_members.assert_not_awaited()
    row = store.db.execute("SELECT status,error FROM inbound_events").fetchone()
    assert tuple(row) == ("failed", "group_project_not_enabled")
    assert store.db.execute("SELECT COUNT(*) FROM work_package_state").fetchone()[0] == 0


def test_group_config_requires_multi_recipient_and_service_key(config, monkeypatch):
    monkeypatch.setattr("op_discord.config.load_dotenv", lambda: None)
    for key, value in {
        "OPENPROJECT_URL": config.op_url,
        "DISCORD_BOT_TOKEN": config.discord_token,
        "TOKEN_ENCRYPTION_KEY": config.encryption_key,
        "DISCORD_GUILD_ID": "1",
        "DELIVERY_MODE": "webhook_dm",
        "OPENPROJECT_WEBHOOK_SECRET": "x" * 40,
        "GROUPS_ENABLED": "true",
        "MULTI_RECIPIENT_ENABLED": "true",
        "OPENPROJECT_SERVICE_API_KEY": "api-key",
        "GROUP_PROJECT_IDS": "1,2",
    }.items():
        monkeypatch.setenv(key, value)
    assert Config.from_env().group_project_ids == (1, 2)
    monkeypatch.setenv("OPENPROJECT_SERVICE_API_KEY", "")
    with pytest.raises(ValueError):
        Config.from_env()
    monkeypatch.setenv("OPENPROJECT_SERVICE_API_KEY", "api-key")
    monkeypatch.setenv("MULTI_RECIPIENT_ENABLED", "false")
    with pytest.raises(ValueError):
        Config.from_env()


@pytest.mark.asyncio
async def test_worker_cancels_removed_group_member_without_sending(store, config):
    store.dm_link(10, "100", welcome=False)
    api = AsyncMock()
    api.group_members.side_effect = [{10}, set()]
    api.service_get.return_value = package()
    sender = AsyncMock()
    service = WebhookService(enabled(config), store, sender, api)
    store.enqueue_event(event())
    await service.resolve_pending()
    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    sender.assert_not_awaited()
    row = store.db.execute("SELECT status,error FROM dm_deliveries").fetchone()
    assert tuple(row) == ("cancelled", "recipient_removed")


@pytest.mark.asyncio
async def test_group_api_error_and_limit_do_not_advance_snapshot(store, config):
    for error, expected in ((APIError(403), "openproject_http_403"),
                            (ProtocolError("bad response"), "openproject_protocol_error")):
        api = AsyncMock()
        api.group_members.side_effect = error
        service = WebhookService(enabled(config), store, AsyncMock(), api)
        store.enqueue_event(event(version=0 if isinstance(error, APIError) else 1))
        await service.resolve_pending()
        row = store.db.execute(
            "SELECT status,error FROM inbound_events ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        assert tuple(row) == ("failed", expected)
        assert store.db.execute("SELECT COUNT(*) FROM work_package_state").fetchone()[0] == 0
    api = AsyncMock()
    api.group_members.return_value = {10, 11}
    store.enqueue_event(event(version=2))
    await WebhookService(enabled(config, max_recipients_per_event=1), store,
                         AsyncMock(), api).resolve_pending()
    row = store.db.execute(
        "SELECT status,error FROM inbound_events ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert tuple(row) == ("failed", "recipient_limit_exceeded")
    assert store.db.execute("SELECT COUNT(*) FROM work_package_state").fetchone()[0] == 0
