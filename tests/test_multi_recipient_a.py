import dataclasses
import hashlib
import hmac
import json
import sqlite3
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from op_discord.bot import dm_card
from op_discord.openproject import APIError, OpenProject
from op_discord.store import Store
from op_discord.web import create_app
from op_discord.webhook import WebhookService, parse_event


def package(version=0, assignee="/api/v3/users/10", status=1):
    return {
        "_type": "WorkPackage", "id": 42, "lockVersion": version, "subject": "Task",
        "_links": {
            "assignee": {"href": assignee, "title": "Assignee"},
            "status": {"href": f"/api/v3/statuses/{status}", "title": f"Status {status}"},
            "project": {"href": "/api/v3/projects/1", "title": "Test"},
        },
    }


def event(action="work_package:created", version=0, assignee="/api/v3/users/10", status=1):
    return parse_event(json.dumps({
        "action": action,
        "work_package": package(version, assignee, status),
        "actor": {"name": "Manager"},
    }).encode())


def signed(value):
    body = json.dumps(value).encode()
    digest = hmac.new(b"x" * 40, body, hashlib.sha1).hexdigest()
    return body, {"X-OP-Signature": f"sha1={digest}"}


@pytest.mark.asyncio
async def test_ingress_is_durable_and_combines_changes(store, api, config):
    store.dm_link(10, "100", welcome=False)
    store.dm_link(11, "110", welcome=False)
    enabled = dataclasses.replace(config, multi_recipient_enabled=True)
    app = create_app(store, api, webhook_secret="x" * 40, multi_recipient_enabled=True)
    payload = {"action": "work_package:created", "work_package": package()}
    body, headers = signed(payload)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://bridge.example"
    ) as http:
        first = await http.post("/webhooks/openproject", content=body, headers=headers)
        second = await http.post("/webhooks/openproject", content=body, headers=headers)
    assert first.status_code == 202 and first.json()["queued"] == 1
    assert second.json()["queued"] == 0
    assert store.dm_jobs(10) == []

    store.close()
    store = Store(config.db_path, config.encryption_key)
    assert len(store.pending_events(10)) == 1
    service = WebhookService(enabled, store, None)
    await service.resolve_pending()
    assert [job["op_id"] for job in store.dm_jobs(10)] == [10]
    changed = event("work_package:updated", 1, "/api/v3/users/11", 2)
    assert store.enqueue_event(changed) == 1
    await service.resolve_pending()
    jobs = store.dm_jobs(10)
    assert len(jobs) == 2
    job = jobs[1]
    assert job["op_id"] == 11 and job["kind"] == "summary"
    assert json.loads(job["metadata"])["changes"] == ["assignment", "status"]
    card, _ = dm_card(job, config.op_url)
    assert any("Status 1 → Status 2" in field.value for field in card.fields)
    assert store.enqueue_event(changed) == 0
    store.close()


def test_multiple_recipients_dedup_and_atomic_resolution(store):
    for op_id in (10, 11):
        store.dm_link(op_id, str(op_id * 10), welcome=False)
    assert store.enqueue_event(event()) == 1
    assert store.resolve_event("wp:42:0", {10: ["assignee"], 11: ["watcher"]}) == 2
    assert store.resolve_event("wp:42:0", {10: ["assignee"]}) == 0
    assert sorted(job["op_id"] for job in store.dm_jobs(10)) == [10, 11]
    assert store.db.execute("SELECT COUNT(*) FROM delivery_claims").fetchone()[0] == 2

    next_event = event("work_package:updated", 1, status=2)
    assert store.enqueue_event(next_event) == 1
    with pytest.raises(TypeError):
        store.resolve_event("wp:42:1", {10: ["assignee"], 11: None})
    assert store.db.execute(
        "SELECT lock_version FROM work_package_state WHERE id=42"
    ).fetchone()[0] == 0
    assert store.db.execute(
        "SELECT status FROM inbound_events WHERE source_event_id='wp:42:1'"
    ).fetchone()[0] == "pending"
    assert len(store.dm_jobs(10)) == 2


def test_migration_preserves_legacy_claim_and_welcome(store, config):
    store.dm_link(10, "100", welcome=True)
    old_event = event()
    store.accept_webhook(old_event)
    store.db.execute("PRAGMA user_version=3")
    store.db.commit()
    store.close()
    migrated = Store(config.db_path, config.encryption_key)
    try:
        assert migrated.db.execute("PRAGMA user_version").fetchone()[0] == 7
        assert migrated.enqueue_event(old_event) == 0
        assert migrated.db.execute(
            "SELECT COUNT(*) FROM delivery_claims WHERE source_event_id='wp:42:0'"
        ).fetchone()[0] == 1
        assert [job["kind"] for job in migrated.dm_jobs(10)] == ["welcome", "assignment"]
    finally:
        migrated.close()


def test_migration_from_real_v3_table_layout(tmp_path, config):
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
        PRAGMA user_version=3;
        CREATE TABLE dm_deliveries (
            event_key TEXT PRIMARY KEY, op_id INTEGER NOT NULL, discord_id TEXT NOT NULL,
            kind TEXT NOT NULL, work_package_id INTEGER NOT NULL, subject TEXT NOT NULL,
            project TEXT NOT NULL, detail TEXT NOT NULL, actor TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL, error TEXT,
            metadata TEXT NOT NULL DEFAULT '{}'
        );
        INSERT INTO dm_deliveries
            (event_key,op_id,discord_id,kind,work_package_id,subject,project,detail,
             actor,created_at)
        VALUES('comment:99:comment',10,'100','comment',42,'Task','Test','Hello','A',1);
    """)
    db.close()
    migrated = Store(str(path), config.encryption_key)
    try:
        assert migrated.db.execute("PRAGMA user_version").fetchone()[0] == 7
        assert migrated.db.execute(
            "SELECT status FROM inbound_events WHERE source_event_id='comment:99'"
        ).fetchone()[0] == "resolved"
        assert migrated.db.execute(
            "SELECT op_id FROM delivery_claims WHERE source_event_id='comment:99'"
        ).fetchone()[0] == 10
        assert migrated.db.execute(
            "SELECT source_event_id FROM dm_deliveries"
        ).fetchone()[0] is None
    finally:
        migrated.close()


@pytest.mark.asyncio
async def test_group_is_parsed_but_not_silently_processed_in_phase_a(store, config):
    group_event = event(assignee="/api/v3/groups/7")
    assert group_event["work_package"]["assignee_group_id"] == 7
    assert group_event["work_package"]["assignee_id"] is None
    store.enqueue_event(group_event)
    await WebhookService(
        dataclasses.replace(config, multi_recipient_enabled=True), store, None
    ).resolve_pending()
    row = store.db.execute("SELECT status,error FROM inbound_events").fetchone()
    assert tuple(row) == ("failed", "group_resolution_not_enabled")
    assert store.db.execute("SELECT COUNT(*) FROM work_package_state").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_resolution_respects_retry_after_and_keeps_snapshot_pending(store, config):
    store.enqueue_event(event())
    service = WebhookService(dataclasses.replace(config, multi_recipient_enabled=True), store, None)
    service.resolve_recipients = AsyncMock(side_effect=APIError(429, 120))
    before = time.time()
    await service.resolve_pending()
    row = store.db.execute(
        "SELECT status,attempts,next_attempt,error FROM inbound_events"
    ).fetchone()
    assert row["status"] == "pending" and row["attempts"] == 1
    assert row["next_attempt"] >= before + 120
    assert row["error"] == "openproject_http_429"
    assert store.db.execute("SELECT COUNT(*) FROM work_package_state").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_service_get_uses_only_get_and_api_key(config):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"_type": "Collection"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        api = OpenProject(dataclasses.replace(config, service_api_key="secret"), http)
        assert await api.service_get("/api/v3/work_packages/42/watchers") == {
            "_type": "Collection"
        }
    assert seen[0].method == "GET"
    assert seen[0].url.host == "op.example"
    assert seen[0].headers["authorization"].startswith("Basic ")
