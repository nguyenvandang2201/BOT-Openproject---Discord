import asyncio
import dataclasses
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from op_discord.bot import dm_card
from op_discord.openproject import APIError, OpenProject, ProtocolError
from op_discord.webhook import WebhookService, parse_event


def work_package(version=0, status=1, assignee=10, project=1):
    return {
        "_type": "WorkPackage", "id": 42, "lockVersion": version,
        "subject": "Task with watchers",
        "_links": {
            "assignee": {"href": f"/api/v3/users/{assignee}", "title": "Alice"},
            "status": {"href": f"/api/v3/statuses/{status}", "title": f"Status {status}"},
            "project": {"href": f"/api/v3/projects/{project}", "title": "Test"},
        },
    }


def event(action="work_package:created", version=0, status=1, assignee=10):
    return parse_event(json.dumps({
        "action": action, "work_package": work_package(version, status, assignee),
        "actor": {"name": "Manager"},
    }).encode())


def enabled(config, **overrides):
    return dataclasses.replace(
        config, multi_recipient_enabled=True, watchers_enabled=True,
        service_api_key="api-key", **overrides,
    )


@pytest.mark.asyncio
async def test_watcher_api_pages_and_validates_members(config):
    requests = []

    def response(request):
        requests.append(request)
        if "offset=2" in str(request.url):
            elements = [{
                "_type": "User", "id": 12, "status": "active",
                "_links": {"self": {"href": "/api/v3/users/12"}},
            }]
            links = {}
        else:
            elements = [{
                "_type": "User", "id": 11, "status": "active",
                "_links": {"self": {"href": "/api/v3/users/11"}},
            }]
            links = {"nextByOffset": {
                "href": "/api/v3/work_packages/42/watchers?offset=2"
            }}
        return httpx.Response(200, json={
            "_type": "Collection", "total": 2, "count": len(elements),
            "_embedded": {"elements": elements}, "_links": links,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        api = OpenProject(enabled(config), http)
        assert await api.watchers(42) == {11, 12}
    assert len(requests) == 2
    assert all(request.method == "GET" for request in requests)


@pytest.mark.asyncio
async def test_watcher_api_rejects_incomplete_or_wrong_user(config):
    for item in (
        {"_type": "Group", "id": 11, "_links": {"self": {"href": "/api/v3/users/11"}}},
        {"_type": "User", "id": 11, "_links": {"self": {"href": "/api/v3/users/12"}}},
        {"_type": "User", "id": 11,
         "_links": {"self": {"href": "https://other.example/api/v3/users/11"}}},
    ):
        transport = httpx.MockTransport(lambda _, item=item: httpx.Response(200, json={
            "_type": "Collection", "total": 1, "_embedded": {"elements": [item]}, "_links": {},
        }))
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(ProtocolError):
                await OpenProject(enabled(config), http).watchers(42)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={
        "_type": "Collection", "total": 2, "_embedded": {"elements": []}, "_links": {},
    }))
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(ProtocolError, match="Incomplete"):
            await OpenProject(enabled(config), http).watchers(42)


@pytest.mark.asyncio
async def test_created_task_fans_out_and_deduplicates_assignee(store, config):
    store.dm_link(10, "100", welcome=False)
    store.dm_link(11, "110", welcome=False)
    api = AsyncMock()
    api.watchers.return_value = {10, 11, 12}  # ID 12 has no Discord link.
    service = WebhookService(enabled(config), store, AsyncMock(), api)
    assert store.enqueue_event(event()) == 1
    await service.resolve_pending()
    jobs = sorted(store.dm_jobs(10), key=lambda row: row["op_id"])
    assert [(job["op_id"], job["kind"]) for job in jobs] == [
        (10, "assignment"), (11, "created")
    ]
    assert json.loads(jobs[0]["metadata"])["recipient_roles"] == ["assignee", "watcher"]
    card, _ = dm_card(jobs[1], config.op_url)
    assert "Theo dõi" in card.title
    assert store.enqueue_event(event()) == 0
    assert store.db.execute("SELECT COUNT(*) FROM work_package_watchers").fetchone()[0] == 3


@pytest.mark.asyncio
async def test_public_comment_reaches_watcher(store, config):
    store.dm_link(10, "100", welcome=False)
    store.dm_link(11, "110", welcome=False)
    api = AsyncMock()
    api.watchers.return_value = {11}
    comment = parse_event(json.dumps({
        "action": "work_package_comment:comment",
        "activity": {
            "id": 900, "internal": False, "comment": {"raw": "Please review"},
            "_embedded": {"workPackage": work_package(), "user": {"name": "Manager"}},
        },
    }).encode())
    store.enqueue_event(comment)
    await WebhookService(enabled(config), store, AsyncMock(), api).resolve_pending()
    jobs = store.dm_jobs(10)
    assert sorted(job["op_id"] for job in jobs) == [10, 11]
    assert all(job["kind"] == "comment" and job["detail"] == "Please review" for job in jobs)


@pytest.mark.asyncio
async def test_added_and_removed_watcher_only_gets_new_events(store, config):
    for op_id in (10, 11, 12):
        store.dm_link(op_id, str(op_id * 10), welcome=False)
    api = AsyncMock()
    service = WebhookService(enabled(config), store, AsyncMock(), api)
    api.watchers.return_value = {11}
    store.enqueue_event(event())
    await service.resolve_pending()
    initial_count = len(store.dm_jobs(10))

    api.watchers.return_value = {11, 12}
    added = event("work_package:updated", 1)
    store.enqueue_event(added)
    await service.resolve_pending()
    added_jobs = store.dm_jobs(10)[initial_count:]
    assert [(job["op_id"], job["kind"]) for job in added_jobs] == [
        (12, "watcher_added")
    ]
    assert "thêm theo dõi" in dm_card(added_jobs[0], config.op_url)[0].title

    api.watchers.return_value = {12}
    changed = event("work_package:updated", 2, status=2)
    store.enqueue_event(changed)
    await service.resolve_pending()
    current = [job for job in store.dm_jobs(20) if job["source_event_id"] == "wp:42:2"]
    assert sorted(job["op_id"] for job in current) == [10, 12]
    assert all(job["op_id"] != 11 for job in current)


@pytest.mark.asyncio
async def test_removed_watcher_is_cancelled_before_delivery(store, config):
    store.dm_link(10, "100", welcome=False)
    store.dm_link(11, "110", welcome=False)
    api = AsyncMock()
    api.watchers.side_effect = [{11}, set()]
    service = WebhookService(enabled(config), store, AsyncMock(), api)
    store.enqueue_event(event())
    await service.resolve_pending()
    watcher_job = next(job for job in store.dm_jobs(10) if job["op_id"] == 11)
    assert not await service.delivery_eligible(watcher_job, {})


@pytest.mark.asyncio
async def test_worker_cancels_removed_watcher_without_sending(store, config):
    store.dm_link(11, "110", welcome=False)
    api = AsyncMock()
    api.watchers.side_effect = [{11}, set()]
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
async def test_reassigned_user_is_rechecked_before_delivery(store, config):
    store.dm_link(10, "100", welcome=False)
    api = AsyncMock()
    api.watchers.return_value = set()
    api.service_get.return_value = work_package(1, assignee=12)
    service = WebhookService(enabled(config), store, AsyncMock(), api)
    store.enqueue_event(event())
    await service.resolve_pending()
    job = store.dm_jobs(10)[0]
    assert not await service.delivery_eligible(job, {}, {})


@pytest.mark.asyncio
async def test_watcher_read_failure_does_not_advance_snapshot(store, config):
    api = AsyncMock()
    api.watchers.side_effect = APIError(403)
    service = WebhookService(enabled(config), store, AsyncMock(), api)
    store.enqueue_event(event())
    await service.resolve_pending()
    row = store.db.execute("SELECT status,error FROM inbound_events").fetchone()
    assert tuple(row) == ("failed", "openproject_http_403")
    assert store.db.execute("SELECT COUNT(*) FROM work_package_state").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_project_allowlist_keeps_other_projects_on_assignee_only(store, config):
    store.dm_link(10, "100", welcome=False)
    api = AsyncMock()
    service = WebhookService(
        enabled(config, watcher_project_ids=(99,)), store, AsyncMock(), api
    )
    store.enqueue_event(event())
    await service.resolve_pending()
    api.watchers.assert_not_awaited()
    assert [job["op_id"] for job in store.dm_jobs(10)] == [10]


@pytest.mark.asyncio
async def test_reconciliation_baselines_then_notifies_without_webhook(store, config):
    store.dm_link(10, "100", welcome=False)
    store.dm_link(11, "110", welcome=False)
    store.dm_link(12, "120", welcome=False)
    store.dm_seed_states([(42, 10, 1, 0)])
    store.db.execute("UPDATE work_package_state SET project_id=1 WHERE id=42")
    store.db.commit()
    api = AsyncMock()
    api.watchers.return_value = {11}
    api.service_get.return_value = work_package(1)
    service = WebhookService(
        enabled(config, watcher_reconcile_seconds=60, watcher_project_ids=(1,)),
        store, AsyncMock(), api,
    )

    await service.reconcile_due()
    assert store.known_watchers(42) == {11}
    assert store.dm_jobs(10) == []  # First observation is a baseline, not a welcome blast.
    store.db.execute("UPDATE watcher_baselines SET next_check=0 WHERE work_package_id=42")
    store.db.commit()
    api.watchers.return_value = {11, 12}
    await service.reconcile_due()
    jobs = store.dm_jobs(10)
    assert [(job["op_id"], job["kind"]) for job in jobs] == [(12, "watcher_added")]
    api.service_get.assert_awaited_once_with("/api/v3/work_packages/42")

    store.db.execute("UPDATE watcher_baselines SET next_check=0 WHERE work_package_id=42")
    store.db.commit()
    await service.reconcile_due()
    assert len(store.dm_jobs(10)) == 1

    store.db.execute("UPDATE watcher_baselines SET next_check=0 WHERE work_package_id=42")
    store.db.commit()
    api.watchers.return_value = {11}
    await service.reconcile_due()
    assert store.known_watchers(42) == {11}

    store.db.execute("UPDATE watcher_baselines SET next_check=0 WHERE work_package_id=42")
    store.db.commit()
    api.watchers.return_value = {11, 12}
    await service.reconcile_due()
    assert len(store.dm_jobs(10)) == 2  # Re-adding after removal is a new subscription.


@pytest.mark.asyncio
async def test_reconciliation_checks_project_for_legacy_task(store, config):
    store.dm_seed_states([(42, 10, 1, 0)])  # Old snapshot has no project ID.
    api = AsyncMock()
    api.service_get.return_value = work_package(project=99)
    api.watchers.return_value = {11}
    service = WebhookService(
        enabled(config, watcher_reconcile_seconds=60, watcher_project_ids=(1,)),
        store, AsyncMock(), api,
    )
    await service.reconcile_due()
    assert store.work_package_project_id(42) == 99
    api.watchers.assert_not_awaited()
    assert store.watchers_due(1, (1,)) == []
