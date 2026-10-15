"""Small, validated projection of OpenProject webhooks for Discord delivery."""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time

from .service import DeliveryError

log = logging.getLogger(__name__)
USER_PATH = re.compile(r"^/api/v3/users/([1-9][0-9]*)$")


def valid_signature(body: bytes, header: str, secret: str) -> bool:
    expected = "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
    return len(header) == len(expected) and hmac.compare_digest(header, expected)


def positive_id(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("Invalid OpenProject ID")
    return value


def version_id(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError("Invalid work package version")
    return value


def user_link(value):
    if not isinstance(value, dict):
        return None
    match = USER_PATH.fullmatch(str(value.get("href", "")))
    return positive_id(int(match[1])) if match else None


def short(value, length=200):
    return str(value or "").strip()[:length]


def work_package(value):
    if not isinstance(value, dict) or value.get("_type") != "WorkPackage":
        raise ValueError("Missing work package")
    links = value.get("_links")
    if not isinstance(links, dict):
        raise ValueError("Missing work package links")
    status = links.get("status") or {}
    status_href = status.get("href", "") if isinstance(status, dict) else ""
    match = re.fullmatch(r"/api/v3/statuses/([1-9][0-9]*)", status_href)
    if not match:
        raise ValueError("Missing work package status")
    project = links.get("project") or {}
    if not isinstance(project, dict):
        project = {}
    assignee = links.get("assignee") or {}
    if not isinstance(assignee, dict):
        assignee = {}
    priority = links.get("priority") or {}
    if not isinstance(priority, dict):
        priority = {}
    reporter = links.get("author") or {}
    if not isinstance(reporter, dict):
        reporter = {}
    return {
        "id": positive_id(value.get("id")),
        "lock_version": version_id(value.get("lockVersion")),
        "assignee_id": user_link(links.get("assignee")),
        "assignee_name": short(assignee.get("title"), 100),
        "status_id": positive_id(int(match[1])),
        "status_title": short(status.get("title"), 100) or "Đã đổi trạng thái",
        "subject": short(value.get("subject"), 250) or "Công việc OpenProject",
        "project": short(project.get("title"), 120),
        "priority": short(priority.get("title"), 100),
        "due_date": short(value.get("dueDate"), 20),
        "reporter": short(reporter.get("title"), 100),
    }


def parse_event(body: bytes):
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("Invalid payload")
    action = payload.get("action")
    if action in ("work_package:created", "work_package:updated"):
        wp = work_package(payload.get("work_package"))
        actor = payload.get("actor") or {}
        if not isinstance(actor, dict):
            actor = {}
        return {"action": action, "work_package": wp, "actor": short(actor.get("name"), 100)}
    if action == "work_package_comment:comment":
        activity = payload.get("activity")
        if not isinstance(activity, dict) or activity.get("internal") is True:
            raise ValueError("Invalid public comment")
        embedded = activity.get("_embedded") or {}
        if not isinstance(embedded, dict):
            raise ValueError("Invalid comment resource")
        wp = work_package(embedded.get("workPackage"))
        user = embedded.get("user") or {}
        if not isinstance(user, dict):
            user = {}
        comment = activity.get("comment") or {}
        if not isinstance(comment, dict):
            comment = {}
        return {
            "action": action,
            "work_package": wp,
            "activity_id": positive_id(activity.get("id")),
            "actor": short(user.get("name"), 100),
            "comment": short(comment.get("raw"), 300),
        }
    return None


class WebhookService:
    def __init__(self, config, store, send):
        self.config, self.store, self.send = config, store, send
        self.last_tick = time.time()

    async def run(self):
        while True:
            self.last_tick = time.time()
            for job in self.store.dm_jobs(self.config.delivery_batch):
                link = self.store.dm_link_for_discord(job["discord_id"])
                if not link or not link["enabled"] or link["op_id"] != job["op_id"]:
                    self.store.dm_finish(job["event_key"], "cancelled", "link_changed")
                    continue
                try:
                    await self.send(job)
                except DeliveryError as error:
                    if error.permanent:
                        self.store.dm_finish(job["event_key"], "failed", error.code)
                    else:
                        delay = min(3600, 30 * 2 ** min(job["attempts"], 7))
                        self.store.dm_finish(job["event_key"], "pending", error.code, delay)
                    log.warning("dm_failed key=%s code=%s", job["event_key"], error.code)
                except Exception as error:
                    self.store.dm_finish(job["event_key"], "pending", "internal_error", 300)
                    log.error("dm_failed key=%s kind=%s", job["event_key"], type(error).__name__)
                else:
                    self.store.dm_finish(job["event_key"], "sent")
            self.last_tick = time.time()
            await asyncio.sleep(2)
