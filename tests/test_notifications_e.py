import dataclasses
import json
import time
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from op_discord.bot import dm_card
from op_discord.store import Store
from op_discord.webhook import WebhookService, parse_event


def event(version, *, due="2026-10-15", priority="Normal", description="First",
          percentage=0, omitted=False):
    wp = {
        "_type": "WorkPackage", "id": 42, "lockVersion": version, "subject": "Task",
        "_links": {
            "assignee": {"href": "/api/v3/users/10", "title": "Alice"},
            "status": {"href": "/api/v3/statuses/1", "title": "New"},
            "project": {"href": "/api/v3/projects/1", "title": "Test"},
            "priority": {"href": "/api/v3/priorities/1", "title": priority},
        },
        "startDate": "2026-10-01", "dueDate": due, "percentageDone": percentage,
        "description": {"raw": description},
    }
    if omitted:
        for key in ("dueDate", "startDate", "percentageDone", "description"):
            del wp[key]
        del wp["_links"]["priority"]
    return parse_event(json.dumps({
        "action": "work_package:created" if version == 0 else "work_package:updated",
        "work_package": wp, "actor": {"name": "Manager"},
    }).encode())


def resolve(store, payload, *, attachments=None):
    store.enqueue_event(payload)
    return store.resolve_event(
        f"wp:42:{payload['work_package']['lock_version']}", {10: ["assignee"]},
        detail_notifications=True, attachments=attachments,
        attachment_notifications=True,
    )


def test_optional_changes_have_baseline_and_ignore_missing_fields(store, config):
    store.dm_link(10, "100", welcome=False)
    assert resolve(store, event(0), attachments=[{"id": 1, "name": "a.txt"}]) == 1
    assert resolve(store, event(1, due="2026-10-16", priority="High",
                                description="Second", percentage=40),
                   attachments=[{"id": 1, "name": "a.txt"},
                                {"id": 2, "name": "b.txt"}]) == 1
    changed = store.dm_jobs(10)[1]
    metadata = json.loads(changed["metadata"])
    assert metadata["changes"] == ["details"]
    assert {item["field"] for item in metadata["field_changes"]} == {
        "due_date", "percentage_done", "priority", "description", "attachments",
    }
    embed, _ = dm_card(changed, config.op_url)
    assert any("2026-10-15 → 2026-10-16" in field.value for field in embed.fields)
    assert resolve(store, event(2, omitted=True)) == 0
    assert store.db.execute(
        "SELECT due_date FROM work_package_details WHERE work_package_id=42"
    ).fetchone()[0] == "2026-10-16"


@pytest.mark.asyncio
async def test_reminder_checks_live_due_and_closed_status_and_is_durable(store, config):
    store.dm_link(10, "100", welcome=False)
    today = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date().isoformat()
    assert resolve(store, event(0, due=today)) == 1
    store.db.execute(
        "UPDATE dm_deliveries SET created_at=? WHERE work_package_id=42",
        (time.time() - 7200,),
    )
    store.db.commit()
    api = AsyncMock()
    current = event(0, due=today)["work_package"]
    # Mock the raw API resource, which is parsed when the reminder is scheduled.
    raw = {
        "_type": "WorkPackage", "id": 42, "lockVersion": 0,
        "subject": "Task", "dueDate": today,
        "_links": {
            "assignee": {"href": "/api/v3/users/10", "title": "Alice"},
            "status": {"href": "/api/v3/statuses/1", "title": "New"},
            "project": {"href": "/api/v3/projects/1", "title": "Test"},
        },
    }
    api.service_get.side_effect = lambda path: (
        raw if path.endswith("/work_packages/42") else
        {"_type": "Status", "id": 1, "isClosed": False}
    )
    enabled = dataclasses.replace(config, reminders_enabled=True, reminder_hour=0)
    service = WebhookService(enabled, store, AsyncMock(), api)
    now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    await service.schedule_reminders(now)
    await service.schedule_reminders(now)
    reminders = [item for item in store.dm_jobs(10) if item["kind"] == "due"]
    assert len(reminders) == 1
    store.close()
    reopened = Store(config.db_path, config.encryption_key)
    service.store = reopened
    await service.schedule_reminders(now)
    assert len([item for item in reopened.dm_jobs(10) if item["kind"] == "due"]) == 1
    assert await service.delivery_eligible(reminders[0], {})
    raw["dueDate"] = "2026-12-31"
    assert not await service.delivery_eligible(reminders[0], {})
    raw["dueDate"] = today
    api.service_get.side_effect = lambda path: (
        raw if path.endswith("/work_packages/42") else
        {"_type": "Status", "id": 1, "isClosed": True}
    )
    assert not await service.delivery_eligible(reminders[0], {})
    assert current["due_date"] == today
    reopened.close()


def test_digest_and_preferences_survive_restart(store, config):
    store.dm_link(10, "100", welcome=False)
    assert resolve(store, event(0)) == 1
    assert resolve(store, event(1, priority="High")) == 1
    store.set_dm_preference(10, "daily", "22:00", "07:00", ["comment"])
    assert store.enqueue_digest(10, "2026-10-15") == 2
    assert store.enqueue_digest(10, "2026-10-15") == 0
    assert [job["kind"] for job in store.dm_jobs(10)] == ["digest"]
    assert store.db.execute(
        "SELECT COUNT(*) FROM dm_deliveries WHERE status='batched'"
    ).fetchone()[0] == 2
    store.close()
    reopened = Store(config.db_path, config.encryption_key)
    try:
        assert reopened.enqueue_digest(10, "2026-10-15") == 0
        assert reopened.dm_preference(10, config)["mode"] == "daily"
        assert len(reopened.dm_jobs(10)) == 1
    finally:
        reopened.close()


def test_quiet_hours_and_event_mute(config, store):
    service = WebhookService(config, store, None)
    prefs = {"quiet_start": "22:00", "quiet_end": "07:00",
             "muted_events": ["comment"]}
    assert service.is_quiet(datetime(2026, 10, 15, 23, 0), prefs)
    assert service.is_quiet(datetime(2026, 10, 16, 6, 59), prefs)
    assert not service.is_quiet(datetime(2026, 10, 16, 7, 0), prefs)
    assert service.preferred_job({"kind": "comment", "metadata": "{}"}, prefs) is None
