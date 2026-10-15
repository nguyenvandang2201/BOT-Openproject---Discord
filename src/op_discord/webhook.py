"""Small, validated projection of OpenProject webhooks for Discord delivery."""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .openproject import APIError, ProtocolError
from .service import DeliveryError

log = logging.getLogger(__name__)
USER_PATH = re.compile(r"^/api/v3/users/([1-9][0-9]*)$")
GROUP_PATH = re.compile(r"^/api/v3/groups/([1-9][0-9]*)$")
PROJECT_PATH = re.compile(r"^/api/v3/projects/([1-9][0-9]*)$")
LOCAL_ZONE = ZoneInfo("Asia/Ho_Chi_Minh")


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


def group_link(value):
    if not isinstance(value, dict):
        return None
    match = GROUP_PATH.fullmatch(str(value.get("href", "")))
    return positive_id(int(match[1])) if match else None


def short(value, length=200):
    return str(value or "").strip()[:length]


def observed_details(value, links):
    """Only include properties actually present in the webhook projection."""
    result = {}
    for source, target in (("startDate", "start_date"), ("dueDate", "due_date")):
        if source in value:
            raw = value[source]
            if raw is not None:
                if not isinstance(raw, str):
                    raise ValueError(f"Invalid {source}")
                date.fromisoformat(raw)
            result[target] = raw or ""
    if "percentageDone" in value:
        raw = value["percentageDone"]
        if raw is not None and (type(raw) is not int or not 0 <= raw <= 100):
            raise ValueError("Invalid percentageDone")
        result["percentage_done"] = raw
    if "priority" in links:
        priority = links["priority"]
        if priority is not None and not isinstance(priority, dict):
            raise ValueError("Invalid priority")
        if priority is None or (not priority.get("href") and "title" not in priority):
            result["priority"] = ""
        elif "title" in priority:
            result["priority"] = short(priority["title"], 100)
    if "description" in value:
        description = value["description"]
        if description is not None and not isinstance(description, dict):
            raise ValueError("Invalid description")
        if description is None or "raw" in description:
            raw = (description or {}).get("raw") or ""
            if not isinstance(raw, str):
                raise ValueError("Invalid description")
            result["description"] = {
                "hash": hashlib.sha256(raw.encode()).hexdigest(),
                "preview": short(raw, 300),
            }
    return result


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
    project_match = PROJECT_PATH.fullmatch(str(project.get("href", "")))
    assignee = links.get("assignee") or {}
    if not isinstance(assignee, dict):
        assignee = {}
    assignee_href = assignee.get("href")
    if assignee_href and not (user_link(assignee) or group_link(assignee)):
        raise ValueError("Unsupported assignee link")
    responsible = links.get("responsible") or {}
    if not isinstance(responsible, dict):
        responsible = {}
    if responsible.get("href") and not user_link(responsible):
        raise ValueError("Unsupported accountable link")
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
        "assignee_group_id": group_link(links.get("assignee")),
        "assignee_name": short(assignee.get("title"), 100),
        "responsible_id": user_link(responsible),
        "status_id": positive_id(int(match[1])),
        "status_title": short(status.get("title"), 100) or "Đã đổi trạng thái",
        "subject": short(value.get("subject"), 250) or "Công việc OpenProject",
        "project": short(project.get("title"), 120),
        "project_id": positive_id(int(project_match[1])) if project_match else None,
        "priority": short(priority.get("title"), 100),
        "due_date": short(value.get("dueDate"), 20),
        "reporter": short(reporter.get("title"), 100),
        "observed_details": observed_details(value, links),
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
    def __init__(self, config, store, send, api=None):
        self.config, self.store, self.send, self.api = config, store, send, api
        self.last_tick = time.time()
        self.next_schedule = 0

    def local_now(self):
        return datetime.now(LOCAL_ZONE)

    @staticmethod
    def is_quiet(now, preference):
        start, end = preference["quiet_start"], preference["quiet_end"]
        if not start or not end or start == end:
            return False
        current = now.strftime("%H:%M")
        return start <= current < end if start < end else current >= start or current < end

    @staticmethod
    def next_local_hour(now, clock):
        hour, minute = (int(part) for part in clock.split(":"))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target.timestamp()

    @staticmethod
    def preferred_job(job, preference):
        if job["kind"] in ("welcome", "test", "digest"):
            return job
        muted = set(preference["muted_events"])
        metadata = json.loads(job.get("metadata") or "{}")
        changes = metadata.get("changes") or [job["kind"]]
        remaining = [change for change in changes if change not in muted]
        if not remaining:
            return None
        if remaining == changes:
            return job
        result = dict(job)
        metadata["changes"] = remaining
        result["metadata"] = json.dumps(metadata, ensure_ascii=False)
        result["kind"] = remaining[0] if len(remaining) == 1 else "summary"
        return result

    async def schedule_reminders(self, now):
        if not self.config.reminders_enabled or now.hour < self.config.reminder_hour:
            return
        today = now.date()
        statuses = {}
        after_id = 0
        while True:
            rows = self.store.reminder_candidates(
                today.isoformat(), (today - timedelta(days=1)).isoformat(), after_id
            )
            if not rows:
                break
            for row in rows:
                wp_id = row["work_package_id"]
                after_id = wp_id
                try:
                    wp = work_package(await self.api.service_get(f"/api/v3/work_packages/{wp_id}"))
                    phase = "due" if row["due_date"] == today.isoformat() else "overdue"
                    if wp["due_date"] != row["due_date"]:
                        self.store.mark_reminder_scanned(wp_id, row["due_date"], phase)
                        continue
                    status_id = wp["status_id"]
                    if status_id not in statuses:
                        status = await self.api.service_get(f"/api/v3/statuses/{status_id}")
                        if status.get("_type") != "Status" or status.get("id") != status_id or (
                            type(status.get("isClosed")) is not bool
                        ):
                            raise ProtocolError("Invalid status resource")
                        statuses[status_id] = status["isClosed"]
                    if statuses[status_id]:
                        self.store.mark_reminder_scanned(wp_id, row["due_date"], phase)
                        continue
                    recipients, _ = await self.resolve_recipients({
                        "action": "work_package:updated", "work_package": wp,
                    })
                    if len(recipients) > self.config.max_recipients_per_event:
                        raise ValueError("recipient_limit_exceeded")
                    self.store.enqueue_reminder(wp, recipients, phase, row["due_date"])
                    self.store.mark_reminder_scanned(wp_id, row["due_date"], phase)
                except (APIError, ProtocolError, ValueError, KeyError, TypeError) as error:
                    log.warning("reminder_schedule_failed wp=%s kind=%s", wp_id, type(error).__name__)
            if len(rows) < 200:
                break

    def schedule_digests(self, now):
        if now.hour < self.config.digest_hour:
            return
        for op_id in self.store.digest_candidates():
            preference = self.store.dm_preference(op_id, self.config)
            if preference["mode"] == "daily" and not self.is_quiet(now, preference):
                self.store.enqueue_digest(op_id, now.date().isoformat())

    async def resolve_recipients(self, event):
        """Resolve current assignee, Group members and watchers to user IDs."""
        wp = event["work_package"]
        assignee_id = wp.get("assignee_id")
        recipients = {assignee_id: ["assignee"]} if assignee_id else {}
        project_id = wp.get("project_id")
        action = event["action"]
        if (self.config.accountable_enabled and wp.get("responsible_id")
                and self.store.recipient_rule(project_id, "accountable", action, False)):
            recipients.setdefault(wp["responsible_id"], []).append("accountable")
        group_id = wp.get("assignee_group_id")
        if group_id:
            if not self.config.groups_enabled:
                raise ValueError("group_resolution_not_enabled")
            if self.store.recipient_rule(project_id, "group", action, True):
                projects = self.config.group_project_ids
                if projects and wp.get("project_id") is None:
                    raise ValueError("project_id_missing")
                if projects and wp["project_id"] not in projects:
                    raise ValueError("group_project_not_enabled")
                if self.api is None:
                    raise ValueError("service_api_unavailable")
                for op_id in await self.api.group_members(group_id):
                    recipients.setdefault(op_id, []).append("group_assignee")
        watcher_ids = None
        if self.config.watchers_enabled:
            projects = self.config.watcher_project_ids
            if projects and wp.get("project_id") is None:
                raise ValueError("project_id_missing")
            if ((not projects or wp["project_id"] in projects)
                    and self.store.recipient_rule(project_id, "watcher", action, True)):
                if self.api is None:
                    raise ValueError("service_api_unavailable")
                watcher_ids = await self.api.watchers(wp["id"])
                for op_id in watcher_ids:
                    recipients.setdefault(op_id, []).append("watcher")
        return recipients, watcher_ids

    async def resolve_pending(self):
        for row in self.store.pending_events(self.config.delivery_batch):
            source = row["source_event_id"]
            try:
                event = json.loads(row["projection"])
                recipients, watcher_ids = await self.resolve_recipients(event)
                if len(recipients) > self.config.max_recipients_per_event:
                    raise ValueError("recipient_limit_exceeded")
                attachments = None
                if (self.config.attachment_notifications_enabled
                        and event["action"] != "work_package_comment:comment"):
                    if self.api is None:
                        raise ValueError("service_api_unavailable")
                    try:
                        current = work_package(await self.api.service_get(
                            f"/api/v3/work_packages/{event['work_package']['id']}"
                        ))
                        if current["lock_version"] == event["work_package"]["lock_version"]:
                            attachments = await self.api.attachments(event["work_package"]["id"])
                    except (APIError, ProtocolError, ValueError) as error:
                        log.warning("attachment_snapshot_skipped wp=%s kind=%s",
                                    event["work_package"]["id"], type(error).__name__)
                self.store.resolve_event(
                    source, recipients, watcher_ids, self.config.watcher_reconcile_seconds,
                    self.config.detail_notifications_enabled, attachments,
                    self.config.attachment_notifications_enabled,
                )
            except APIError as error:
                code = f"openproject_http_{error.status}"
                if error.status in (400, 401, 403, 404):
                    self.store.finish_event(source, "failed", code)
                else:
                    self.retry_resolution(row, code, error.retry_after)
            except (ValueError, KeyError, TypeError) as error:
                self.store.finish_event(source, "failed", str(error)[:100])
                log.warning("event_resolution_failed source=%s code=%s", source, type(error).__name__)
            except ProtocolError:
                self.store.finish_event(source, "failed", "openproject_protocol_error")
            except Exception as error:
                self.retry_resolution(row, "internal_error")
                log.error("event_resolution_retry source=%s kind=%s", source, type(error).__name__)

    def retry_resolution(self, row, code, retry_after=0):
        if row["attempts"] >= 9:
            self.store.finish_event(row["source_event_id"], "failed", code)
            return
        delay = max(retry_after, min(3600, 30 * 2 ** min(row["attempts"], 7)))
        self.store.finish_event(row["source_event_id"], "pending", code, delay)

    async def reconcile_due(self):
        if not (self.config.watchers_enabled and self.config.watcher_reconcile_seconds):
            return
        if self.api is None:
            return
        for wp_id in self.store.watchers_due(1, self.config.watcher_project_ids):
            baseline = self.store.watcher_baseline(wp_id)
            try:
                scope_wp = None
                project_id = self.store.work_package_project_id(wp_id)
                if project_id is None:
                    scope_wp = work_package(
                        await self.api.service_get(f"/api/v3/work_packages/{wp_id}")
                    )
                    project_id = scope_wp["project_id"]
                    if project_id is None:
                        raise ValueError("project_id_missing")
                    self.store.set_work_package_project_id(wp_id, project_id)
                if (self.config.watcher_project_ids and
                        project_id not in self.config.watcher_project_ids):
                    continue
                notify_added = self.store.recipient_rule(
                    project_id, "watcher", "work_package:updated", True
                )
                watcher_ids = await self.api.watchers(wp_id)
                added = (
                    watcher_ids - self.store.known_watchers(wp_id)
                    if baseline and baseline["initialized"] and notify_added else set()
                )
                wp = scope_wp
                if added:
                    wp = wp or work_package(
                        await self.api.service_get(f"/api/v3/work_packages/{wp_id}")
                    )
                self.store.reconcile_watchers(
                    wp_id, watcher_ids, wp, self.config.watcher_reconcile_seconds,
                    notify_added=notify_added,
                )
            except APIError as error:
                delay = max(error.retry_after, 3600 if error.status in (401, 403, 404) else 60)
                self.store.defer_watcher_check(wp_id, f"openproject_http_{error.status}", delay)
            except (ProtocolError, ValueError, KeyError, TypeError) as error:
                self.store.defer_watcher_check(wp_id, type(error).__name__, 3600)
            except Exception as error:
                self.store.defer_watcher_check(wp_id, "internal_error", 300)
                log.error("watcher_reconcile_failed wp=%s kind=%s", wp_id, type(error).__name__)

    async def delivery_eligible(
        self, job, watcher_cache, work_package_cache=None, group_cache=None
    ):
        if work_package_cache is None:
            work_package_cache = {}
        if group_cache is None:
            group_cache = {}
        metadata = json.loads(job.get("metadata") or "{}")
        if job["kind"] == "digest":
            preference = self.store.dm_preference(job["op_id"], self.config)
            entries = []
            for entry in metadata.get("entries", []):
                preferred = self.preferred_job(entry, preference)
                if preferred and await self.delivery_eligible(
                    preferred, watcher_cache, work_package_cache, group_cache
                ):
                    entries.append(preferred)
            metadata["entries"] = entries
            job["metadata"] = json.dumps(metadata, ensure_ascii=False)
            return bool(entries)
        if not job.get("source_event_id"):
            return True  # Legacy jobs have no role snapshot.
        roles = metadata.get("recipient_roles") or []
        wp_id = job["work_package_id"]

        async def current_work_package():
            if self.api is None:
                raise ValueError("service_api_unavailable")
            if wp_id not in work_package_cache:
                work_package_cache[wp_id] = work_package(
                    await self.api.service_get(f"/api/v3/work_packages/{wp_id}")
                )
            return work_package_cache[wp_id]

        if job["kind"] in ("due", "overdue"):
            current = await current_work_package()
            if current.get("due_date") != metadata.get("reminder_due_date"):
                return False
            status = await self.api.service_get(f"/api/v3/statuses/{current['status_id']}")
            if status.get("_type") != "Status" or status.get("id") != current["status_id"] or (
                type(status.get("isClosed")) is not bool
            ):
                raise ProtocolError("Invalid status resource")
            if status["isClosed"]:
                return False

        if "assignee" in roles:
            if (self.config.watchers_enabled or self.config.groups_enabled
                    or self.config.accountable_enabled):
                current_assignee_id = (await current_work_package())["assignee_id"]
            else:
                current = self.store.current_assignee(wp_id)
                current_assignee_id = (
                    current["assignee_id"] if current and current["assignee_kind"] == "user"
                    else job["op_id"] if current is None else None
                )
            if current_assignee_id == job["op_id"]:
                return True
        if "group_assignee" in roles:
            expected_group_id = metadata.get("assignee_group_id")
            if type(expected_group_id) is int and expected_group_id > 0 and (
                await current_work_package()
            )["assignee_group_id"] == expected_group_id:
                if expected_group_id not in group_cache:
                    group_cache[expected_group_id] = await self.api.group_members(
                        expected_group_id
                    )
                if job["op_id"] in group_cache[expected_group_id]:
                    return True
        if "accountable" in roles:
            if (await current_work_package())["responsible_id"] == job["op_id"]:
                return True
        if "watcher" in roles:
            if wp_id not in watcher_cache:
                if self.api is None:
                    raise ValueError("service_api_unavailable")
                watcher_cache[wp_id] = await self.api.watchers(wp_id)
            return job["op_id"] in watcher_cache[wp_id]
        return False

    async def run(self):
        while True:
            self.last_tick = time.time()
            # Drain already accepted events even after the flag is switched off.
            await self.resolve_pending()
            now = self.local_now()
            if time.time() >= self.next_schedule:
                await self.schedule_reminders(now)
                self.schedule_digests(now)
                self.next_schedule = time.time() + 60
            watcher_cache = {}
            work_package_cache = {}
            group_cache = {}
            for job in self.store.dm_jobs(self.config.delivery_batch):
                link = self.store.dm_link_for_discord(job["discord_id"])
                if not link or not link["enabled"] or link["op_id"] != job["op_id"]:
                    self.store.dm_finish(job["event_key"], "cancelled", "link_changed")
                    continue
                try:
                    preference = self.store.dm_preference(job["op_id"], self.config)
                    if job["kind"] not in ("welcome", "test"):
                        if self.is_quiet(now, preference):
                            self.store.dm_defer(
                                job["event_key"],
                                self.next_local_hour(now, preference["quiet_end"]),
                            )
                            continue
                        if preference["mode"] == "daily" and job["kind"] not in (
                            "digest", "due", "overdue"
                        ):
                            self.store.dm_defer(
                                job["event_key"],
                                self.next_local_hour(now, f"{self.config.digest_hour:02d}:00"),
                            )
                            continue
                        if job["kind"] != "digest":
                            preferred = self.preferred_job(job, preference)
                            if preferred is None:
                                self.store.dm_finish(job["event_key"], "cancelled", "muted")
                                continue
                            job = preferred
                    if not await self.delivery_eligible(
                        job, watcher_cache, work_package_cache, group_cache
                    ):
                        self.store.dm_finish(job["event_key"], "cancelled", "recipient_removed")
                        continue
                    await self.send(job)
                except APIError as error:
                    code = f"openproject_http_{error.status}"
                    if error.status in (400, 401, 403, 404):
                        self.store.dm_finish(job["event_key"], "failed", code)
                    else:
                        delay = max(error.retry_after, min(3600, 30 * 2 ** min(job["attempts"], 7)))
                        self.store.dm_finish(job["event_key"], "pending", code, delay)
                except (ProtocolError, ValueError, KeyError, TypeError):
                    self.store.dm_finish(job["event_key"], "failed", "openproject_protocol_error")
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
            await self.reconcile_due()
            self.last_tick = time.time()
            await asyncio.sleep(2)
