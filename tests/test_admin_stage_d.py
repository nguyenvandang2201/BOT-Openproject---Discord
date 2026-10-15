import dataclasses
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from op_discord.admin_web import COOKIE
from op_discord.openproject import OpenProject, ProtocolError
from op_discord.store import Store
from op_discord.web import create_app
from op_discord.webhook import WebhookService, parse_event


def admin_app(store, config):
    api = AsyncMock()
    api.config = dataclasses.replace(
        config, multi_recipient_enabled=True, watchers_enabled=True,
        groups_enabled=True, accountable_enabled=True,
        service_api_key="api-key", watcher_reconcile_seconds=0,
    )
    api.service_user.side_effect = lambda op_id: {
        "id": op_id, "name": f"User {op_id}", "status": "active"
    }
    bot = AsyncMock()
    bot.is_manager.return_value = True
    bot.member_summary.side_effect = lambda discord_id: {
        "id": discord_id, "name": f"Member {discord_id}"
    }
    app = create_app(store, api, admin_bot=bot, admin_url="http://192.168.1.12:8081")
    return app, api, bot


async def login(client, store):
    ticket = store.admin_ticket("123")
    response = await client.get(f"/admin/login?ticket={ticket}")
    assert response.status_code == 303
    return store.admin_csrf(client.cookies.get(COOKIE))


@pytest.mark.asyncio
async def test_service_user_lookup_and_bounded_name_search(config):
    seen = []

    def respond(request):
        seen.append(request)
        if request.url.path.endswith("/12"):
            return httpx.Response(200, json={
                "_type": "User", "id": 12, "name": "Alice", "status": "active"
            })
        return httpx.Response(200, json={
            "_type": "Collection", "_embedded": {"elements": [
                {"_type": "User", "id": 12, "name": "Alice", "status": "active"}
            ]},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        api = OpenProject(dataclasses.replace(config, service_api_key="key"), http)
        assert (await api.service_user(12))["name"] == "Alice"
        assert [user["id"] for user in await api.search_users("Ali")] == [12]
    assert all(request.method == "GET" for request in seen)
    assert "filters=" in str(seen[1].url) and "pageSize=10" in str(seen[1].url)

    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={
        "_type": "User", "id": 99,
    }))
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(ProtocolError):
            await OpenProject(dataclasses.replace(config, service_api_key="key"), http).service_user(12)


@pytest.mark.asyncio
async def test_admin_lookup_and_bulk_preview_apply_are_authorized_and_atomic(store, config):
    app, api, bot = admin_app(store, config)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://192.168.1.12:8081"
    ) as client:
        assert (await client.get("/admin/lookup?q=12")).status_code == 403
        csrf = await login(client, store)
        lookup = await client.get("/admin/lookup?q=12&discord_id=100")
        assert lookup.status_code == 200
        assert "User 12" in lookup.text and "Member 100" in lookup.text
        assert (await client.get("/admin/lookup?q=Alice")).status_code == 200
        api.search_users.assert_awaited_once_with("Alice")

        csv_data = "op_id,discord_id,name\n12,100,Alice\n13,101,Bob"
        assert (await client.post("/admin/import", data={
            "mode": "preview", "csv": csv_data,
        })).status_code == 403
        preview = await client.post("/admin/import", data={
            "mode": "preview", "csrf": csrf, "csv": csv_data,
        })
        assert preview.status_code == 200 and "Xác nhận nhập" in preview.text
        assert store.dm_links() == []
        applied = await client.post("/admin/import", data={
            "mode": "apply", "csrf": csrf, "csv": csv_data,
        }, follow_redirects=False)
        assert applied.status_code == 303
        assert [link["op_id"] for link in store.dm_links()] == [12, 13]
        assert [job["kind"] for job in store.dm_jobs(10)] == ["welcome", "welcome"]
        assert store.admin_audit_recent()[0]["actor_discord_id"] == "123"
        assert "bulk_link" in (await client.get("/admin/audit")).text

        conflict = await client.post("/admin/import", data={
            "mode": "apply", "csrf": csrf,
            "csv": "op_id,discord_id,name\n14,102,Carol\n15,100,Conflict",
        })
        assert conflict.status_code == 400
        assert "Dòng 3" in conflict.text
        assert [link["op_id"] for link in store.dm_links()] == [12, 13]
        bot.member_summary.assert_awaited()


@pytest.mark.asyncio
async def test_admin_history_single_retry_test_dm_and_failed_event_retry(store, config):
    app, _, _ = admin_app(store, config)
    store.dm_link(12, "100", welcome=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://192.168.1.12:8081"
    ) as client:
        csrf = await login(client, store)
        test = await client.post("/admin/action", data={
            "csrf": csrf, "action": "test", "op_id": "12",
        })
        assert test.status_code == 303
        job = store.dm_jobs(10)[0]
        assert job["kind"] == "test"
        assert (await client.post("/admin/action", data={
            "csrf": csrf, "action": "test", "op_id": "12",
        })).status_code == 400
        store.dm_finish(job["event_key"], "failed", "dm_forbidden")
        history = await client.get("/admin/history?op=12&status=failed")
        assert "dm_forbidden" in history.text and "Thử lại" in history.text
        retry = await client.post("/admin/action", data={
            "csrf": csrf, "action": "retry_job", "event_key": job["event_key"],
        })
        assert retry.status_code == 303
        store.dm_finish(job["event_key"], "sent")
        assert (await client.post("/admin/action", data={
            "csrf": csrf, "action": "retry_job", "event_key": job["event_key"],
        })).status_code == 400
        sent = await client.get("/admin/history?op=12&status=sent")
        assert "Gửi lúc" in sent.text and "test" in sent.text

        payload = {"action": "work_package:created", "work_package": {
            "_type": "WorkPackage", "id": 42, "lockVersion": 0, "subject": "Task",
            "_links": {"status": {"href": "/api/v3/statuses/1"},
                       "assignee": {"href": "/api/v3/users/12"}},
        }}
        store.enqueue_event(parse_event(json.dumps(payload).encode()))
        store.finish_event("wp:42:0", "failed", "openproject_http_403")
        assert (await client.get("/admin/history?task=42&status=failed")).status_code == 200
        result = await client.post("/admin/action", data={
            "csrf": csrf, "action": "retry_event", "source": "wp:42:0",
        })
        assert result.status_code == 303
        assert store.pending_events(10)[0]["source_event_id"] == "wp:42:0"


@pytest.mark.asyncio
async def test_project_rules_limit_roles_and_accountable_opt_in(store, config):
    app, api, _ = admin_app(store, config)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://192.168.1.12:8081"
    ) as client:
        csrf = await login(client, store)
        for role, enabled in (("watcher", "0"), ("group", "0"), ("accountable", "1")):
            result = await client.post("/admin/action", data={
                "csrf": csrf, "action": "rule", "project_id": "1", "role": role,
                "event": "work_package:created", "enabled": enabled,
            })
            assert result.status_code == 303
        assert len(store.recipient_rules()) == 3
        assert "accountable" in (await client.get("/admin/rules")).text

    task = parse_event(json.dumps({
        "action": "work_package:created",
        "work_package": {
            "_type": "WorkPackage", "id": 42, "lockVersion": 0, "subject": "Task",
            "_links": {
                "status": {"href": "/api/v3/statuses/1"},
                "project": {"href": "/api/v3/projects/1"},
                "assignee": {"href": "/api/v3/groups/7"},
                "responsible": {"href": "/api/v3/users/12"},
            },
        },
    }).encode())
    recipients, watcher_ids = await WebhookService(
        api.config, store, AsyncMock(), api
    ).resolve_recipients(task)
    assert recipients == {12: ["accountable"]}
    assert watcher_ids is None
    api.watchers.assert_not_awaited()
    api.group_members.assert_not_awaited()
    store.dm_link(12, "100", welcome=False)
    store.enqueue_event(task)
    await WebhookService(api.config, store, AsyncMock(), api).resolve_pending()
    jobs = store.dm_jobs(10)
    assert [(job["op_id"], job["kind"]) for job in jobs] == [(12, "created")]
    assert json.loads(jobs[0]["metadata"])["recipient_roles"] == ["accountable"]


@pytest.mark.asyncio
async def test_accountable_change_notifies_new_person_and_rechecks_current_owner(store, config):
    enabled = dataclasses.replace(
        config, multi_recipient_enabled=True, accountable_enabled=True,
        service_api_key="api-key",
    )
    for action in ("work_package:created", "work_package:updated"):
        store.set_recipient_rule(1, "accountable", action, True)
    for op_id in (10, 12, 13):
        store.dm_link(op_id, str(op_id * 10), welcome=False)
    api = AsyncMock()
    service = WebhookService(enabled, store, AsyncMock(), api)

    def payload(version, responsible):
        return {"action": "work_package:created" if version == 0 else "work_package:updated",
                "work_package": {
                    "_type": "WorkPackage", "id": 42, "lockVersion": version, "subject": "Task",
                    "_links": {"status": {"href": "/api/v3/statuses/1"},
                               "project": {"href": "/api/v3/projects/1"},
                               "assignee": {"href": "/api/v3/users/10"},
                               "responsible": {"href": f"/api/v3/users/{responsible}"}},
                }}

    store.enqueue_event(parse_event(json.dumps(payload(0, 12)).encode()))
    await service.resolve_pending()
    assert sorted(job["op_id"] for job in store.dm_jobs(10)) == [10, 12]
    old_job = next(job for job in store.dm_jobs(10) if job["op_id"] == 12)
    store.enqueue_event(parse_event(json.dumps(payload(1, 13)).encode()))
    await service.resolve_pending()
    new_job = next(job for job in store.dm_jobs(10) if job["op_id"] == 13)
    assert new_job["kind"] == "accountable_added"
    api.service_get.return_value = payload(1, 13)["work_package"]
    assert not await service.delivery_eligible(old_job, {}, {}, {})
    assert await service.delivery_eligible(new_job, {}, {}, {})


@pytest.mark.asyncio
async def test_v5_snapshot_baselines_accountable_without_false_notice(store, config):
    store.dm_link(12, "120", welcome=False)
    store.dm_seed_states([(42, 10, 1, 0)])
    store.db.execute("PRAGMA user_version=5")
    store.db.commit()
    store.close()
    migrated = Store(config.db_path, config.encryption_key)
    try:
        assert migrated.db.execute(
            "SELECT responsible_id FROM work_package_state WHERE id=42"
        ).fetchone()[0] == -1
        migrated.set_recipient_rule(1, "accountable", "work_package:updated", True)
        task = parse_event(json.dumps({
            "action": "work_package:updated",
            "work_package": {
                "_type": "WorkPackage", "id": 42, "lockVersion": 1, "subject": "Task",
                "_links": {"status": {"href": "/api/v3/statuses/1"},
                           "project": {"href": "/api/v3/projects/1"},
                           "assignee": {"href": "/api/v3/users/10"},
                           "responsible": {"href": "/api/v3/users/12"}},
            },
        }).encode())
        migrated.enqueue_event(task)
        enabled = dataclasses.replace(
            config, multi_recipient_enabled=True, accountable_enabled=True,
            service_api_key="api-key",
        )
        await WebhookService(enabled, migrated, AsyncMock(), AsyncMock()).resolve_pending()
        assert migrated.dm_jobs(10) == []
        assert migrated.db.execute(
            "SELECT responsible_id FROM work_package_state WHERE id=42"
        ).fetchone()[0] == 12
    finally:
        migrated.close()


@pytest.mark.asyncio
async def test_watcher_rule_suppresses_reconciliation_notice(store, config):
    store.dm_seed_states([(42, 10, 1, 0)])
    store.set_work_package_project_id(42, 1)
    store.dm_link(12, "120", welcome=False)
    api = AsyncMock()
    api.watchers.side_effect = [{11}, {11, 12}]
    enabled = dataclasses.replace(
        config, multi_recipient_enabled=True, watchers_enabled=True,
        watcher_reconcile_seconds=60, service_api_key="api-key",
    )
    service = WebhookService(enabled, store, AsyncMock(), api)
    await service.reconcile_due()
    store.set_recipient_rule(1, "watcher", "work_package:updated", False)
    store.db.execute("UPDATE watcher_baselines SET next_check=0 WHERE work_package_id=42")
    store.db.commit()
    await service.reconcile_due()
    assert store.known_watchers(42) == {11, 12}
    assert store.dm_jobs(10) == []
