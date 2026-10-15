import json
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode, urljoin, urlsplit

import httpx


class APIError(Exception):
    def __init__(self, status=0, retry_after=0):
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"OpenProject request failed (HTTP {status})")


class ProtocolError(Exception):
    pass


def retry_seconds(value: str) -> float:
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 60


class OpenProject:
    def __init__(self, config, http: httpx.AsyncClient):
        self.config, self.http = config, http
        self.blocked_until = 0.0

    def safe_url(self, href: str, path: str | None = None) -> str:
        url = urljoin(self.config.op_url + "/", href)
        parsed, base = urlsplit(url), urlsplit(self.config.op_url)
        if (
            parsed.scheme != base.scheme
            or parsed.netloc != base.netloc
            or parsed.username
            or parsed.password
            or parsed.fragment
            or (path and parsed.path != path)
        ):
            raise ProtocolError("Unexpected OpenProject link")
        return url

    def authorize_url(self, state):
        return (
            self.config.op_url
            + "/oauth/authorize?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": self.config.client_id,
                    "redirect_uri": self.config.redirect_uri,
                    "scope": "api_v3",
                    "state": state,
                    "prompt": "consent",
                }
            )
        )

    async def request(self, method, href, *, token=None, data=None):
        if time.time() < self.blocked_until:
            raise APIError(429, self.blocked_until - time.time())
        headers = {"Accept": "application/hal+json, application/json"}
        auth = token if isinstance(token, httpx.BasicAuth) else None
        if token and auth is None:
            headers["Authorization"] = "Bearer " + token
        try:
            response = await self.http.request(
                method,
                self.safe_url(href),
                headers=headers,
                auth=auth,
                data=data,
                follow_redirects=False,
                timeout=20,
            )
        except httpx.RequestError:
            raise APIError() from None
        if not 200 <= response.status_code < 300:
            delay = retry_seconds(response.headers.get("Retry-After", "60"))
            if response.status_code == 429:
                self.blocked_until = time.time() + delay
            raise APIError(response.status_code, delay)
        try:
            result = response.json()
        except ValueError:
            raise ProtocolError("Expected JSON from OpenProject") from None
        if not isinstance(result, dict):
            raise ProtocolError("Expected a HAL object")
        return result

    async def service_get(self, href):
        """Read OpenProject with the bridge's service account, never a user's token."""
        if not self.config.service_api_key:
            raise APIError(401)
        return await self.request(
            "GET", href, token=httpx.BasicAuth("apikey", self.config.service_api_key)
        )

    @staticmethod
    def user_summary(item):
        user_id = item.get("id") if isinstance(item, dict) else None
        if (not isinstance(item, dict) or item.get("_type") != "User"
                or type(user_id) is not int or not 0 < user_id < 2**63):
            raise ProtocolError("Expected OpenProject User")
        return {
            "id": user_id,
            "name": str(item.get("name") or f"ID {user_id}")[:200],
            "status": str(item.get("status") or "unknown")[:30],
        }

    async def service_user(self, user_id):
        if type(user_id) is not int or not 0 < user_id < 2**63:
            raise ProtocolError("Invalid user ID")
        user = self.user_summary(await self.service_get(f"/api/v3/users/{user_id}"))
        if user["id"] != user_id:
            raise ProtocolError("OpenProject User ID mismatch")
        return user

    async def search_users(self, name):
        name = str(name).strip()
        if not 2 <= len(name) <= 80:
            raise ValueError("Nhập từ 2 đến 80 ký tự để tìm người dùng.")
        filters = json.dumps([{"name": {"operator": "=", "values": [name]}}])
        href = "/api/v3/users?" + urlencode({"pageSize": 10, "filters": filters})
        result = await self.service_get(href)
        embedded = result.get("_embedded")
        elements = embedded.get("elements") if isinstance(embedded, dict) else None
        if result.get("_type") != "Collection" or not isinstance(elements, list):
            raise ProtocolError("Expected OpenProject User collection")
        if len(elements) > 10:
            raise ProtocolError("User search exceeded page size")
        return [self.user_summary(item) for item in elements]

    async def group_members(self, group_id):
        if type(group_id) is not int or not 0 < group_id < 2**63:
            raise ProtocolError("Invalid group ID")
        path = f"/api/v3/groups/{group_id}"
        result = await self.service_get(path)
        if (result.get("_type") != "Group" or type(result.get("id")) is not int
                or result["id"] != group_id):
            raise ProtocolError("Expected Group")
        links = result.get("_links")
        embedded = result.get("_embedded")
        if not isinstance(links, dict) or not isinstance(embedded, dict):
            raise ProtocolError("Missing Group members")
        member_links = links.get("members")
        member_objects = embedded.get("members")
        if member_links is None and member_objects is None:
            raise ProtocolError("Missing Group members")
        if member_links is not None and not isinstance(member_links, list):
            raise ProtocolError("Invalid Group member links")
        if member_objects is not None and not isinstance(member_objects, list):
            raise ProtocolError("Invalid Group member resources")

        linked_ids = set()
        for link in member_links or []:
            href = link.get("href") if isinstance(link, dict) else None
            if not isinstance(href, str):
                raise ProtocolError("Invalid Group member link")
            parsed = urlsplit(href)
            if parsed.query or not parsed.path.startswith("/api/v3/users/"):
                raise ProtocolError("Invalid Group member link")
            suffix = parsed.path.removeprefix("/api/v3/users/")
            if not suffix.isascii() or not suffix.isdecimal() or suffix.startswith("0"):
                raise ProtocolError("Invalid Group member ID")
            user_id = int(suffix)
            if not 0 < user_id < 2**63:
                raise ProtocolError("Invalid Group member ID")
            self.safe_url(href, f"/api/v3/users/{user_id}")
            linked_ids.add(user_id)
            if len(linked_ids) > self.config.max_recipients_per_event:
                raise ProtocolError("Group member limit exceeded")

        embedded_ids, active_ids = set(), set()
        for item in member_objects or []:
            if not isinstance(item, dict) or item.get("_type") != "User":
                raise ProtocolError("Expected Group member User")
            user_id = item.get("id")
            if type(user_id) is not int or not 0 < user_id < 2**63:
                raise ProtocolError("Invalid Group member ID")
            embedded_ids.add(user_id)
            if len(embedded_ids) > self.config.max_recipients_per_event:
                raise ProtocolError("Group member limit exceeded")
            if item.get("status", "active") == "active":
                active_ids.add(user_id)
        if member_links is not None and member_objects is not None and linked_ids != embedded_ids:
            raise ProtocolError("Incomplete Group members")
        ids = active_ids if member_objects is not None else linked_ids
        if len(ids) > self.config.max_recipients_per_event:
            raise ProtocolError("Group member limit exceeded")
        return ids

    async def watchers(self, work_package_id):
        if type(work_package_id) is not int or not 0 < work_package_id < 2**63:
            raise ProtocolError("Invalid work package ID")
        path = f"/api/v3/work_packages/{work_package_id}/watchers"
        href, visited, ids, seen_ids = path, set(), set(), set()
        for _ in range(self.config.max_pages):
            url = self.safe_url(href, path)
            if url in visited:
                raise ProtocolError("Watcher pagination loop")
            visited.add(url)
            result = await self.service_get(url)
            embedded = result.get("_embedded")
            elements = embedded.get("elements") if isinstance(embedded, dict) else None
            total = result.get("total")
            if result.get("_type") != "Collection" or not isinstance(elements, list) or (
                type(total) is not int or total < 0
            ):
                raise ProtocolError("Expected watcher collection")
            for item in elements:
                if not isinstance(item, dict) or item.get("_type") != "User":
                    raise ProtocolError("Expected watcher User")
                user_id = item.get("id")
                if type(user_id) is not int or not 0 < user_id < 2**63:
                    raise ProtocolError("Invalid watcher ID")
                user_links = item.get("_links")
                self_link = user_links.get("self") if isinstance(user_links, dict) else None
                if not isinstance(self_link, dict) or not self_link.get("href"):
                    raise ProtocolError("Watcher ID/link mismatch")
                self.safe_url(self_link["href"], f"/api/v3/users/{user_id}")
                seen_ids.add(user_id)
                if item.get("status", "active") == "active":
                    ids.add(user_id)
            if len(ids) > self.config.max_recipients_per_event:
                raise ProtocolError("Watcher limit exceeded")
            links = result.get("_links") or {}
            if not isinstance(links, dict):
                raise ProtocolError("Invalid watcher links")
            next_link = links.get("nextByOffset") or links.get("next") or {}
            if not isinstance(next_link, dict):
                raise ProtocolError("Invalid watcher next link")
            href = next_link.get("href")
            if not href:
                if len(seen_ids) != total:
                    raise ProtocolError("Incomplete watcher collection")
                return ids
        raise ProtocolError("Watcher page limit reached")

    async def attachments(self, work_package_id):
        """Return a bounded snapshot of file IDs and names, never download files."""
        if type(work_package_id) is not int or not 0 < work_package_id < 2**63:
            raise ProtocolError("Invalid work package ID")
        path = f"/api/v3/work_packages/{work_package_id}/attachments"
        href, visited, files = path, set(), {}
        for _ in range(self.config.max_pages):
            url = self.safe_url(href, path)
            if url in visited:
                raise ProtocolError("Attachment pagination loop")
            visited.add(url)
            result = await self.service_get(url)
            embedded = result.get("_embedded")
            elements = embedded.get("elements") if isinstance(embedded, dict) else None
            total = result.get("total")
            if result.get("_type") != "Collection" or not isinstance(elements, list) or (
                type(total) is not int or not 0 <= total <= 500
            ):
                raise ProtocolError("Expected bounded attachment collection")
            for item in elements:
                if not isinstance(item, dict) or item.get("_type") != "Attachment":
                    raise ProtocolError("Expected Attachment")
                file_id = item.get("id")
                if type(file_id) is not int or not 0 < file_id < 2**63:
                    raise ProtocolError("Invalid attachment ID")
                files[file_id] = str(item.get("fileName") or item.get("title") or "Tệp")[:100]
            if len(files) > 500:
                raise ProtocolError("Attachment limit exceeded")
            links = result.get("_links") or {}
            if not isinstance(links, dict):
                raise ProtocolError("Invalid attachment links")
            next_link = links.get("nextByOffset") or links.get("next") or {}
            if not isinstance(next_link, dict):
                raise ProtocolError("Invalid attachment next link")
            href = next_link.get("href")
            if not href:
                if len(files) != total:
                    raise ProtocolError("Incomplete attachment collection")
                return [{"id": key, "name": files[key]} for key in sorted(files)]
        raise ProtocolError("Attachment page limit reached")

    async def token_request(self, **grant):
        result = await self.request(
            "POST",
            "/oauth/token",
            data={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                **grant,
            },
        )
        if not isinstance(result.get("access_token"), str) or not result["access_token"]:
            raise ProtocolError("Missing OAuth access token")
        if str(result.get("token_type", "")).lower() != "bearer":
            raise ProtocolError("Unsupported OAuth token type")
        expires_in = result.get("expires_in")
        if not isinstance(expires_in, (int, float)) or expires_in <= 0:
            raise ProtocolError("Expected OAuth token expiry")
        return {
            "access_token": result["access_token"],
            "refresh_token": result.get("refresh_token"),
            "expires_at": time.time() + expires_in,
        }

    async def exchange(self, code):
        return await self.token_request(
            grant_type="authorization_code", code=code, redirect_uri=self.config.redirect_uri
        )

    async def refresh(self, tokens):
        if not tokens.get("refresh_token"):
            raise APIError(401)
        result = await self.token_request(
            grant_type="refresh_token", refresh_token=tokens["refresh_token"]
        )
        result["refresh_token"] = result["refresh_token"] or tokens["refresh_token"]
        return result

    async def me(self, token):
        result = await self.request("GET", "/api/v3/users/me", token=token)
        if result.get("_type") != "User" or type(result.get("id")) is not int:
            raise ProtocolError("Expected authenticated User")
        return result

    async def notifications(self, token):
        path = "/api/v3/notifications"
        href = path + "?" + urlencode({"pageSize": 100, "sortBy": '[["id","asc"]]'})
        ids, visited = set(), set()
        for _ in range(self.config.max_pages):
            url = self.safe_url(href, path)
            if url in visited:
                raise ProtocolError("Pagination loop")
            visited.add(url)
            result = await self.request("GET", url, token=token)
            elements = result.get("_embedded", {}).get("elements")
            if not isinstance(elements, list) or type(result.get("total")) is not int:
                raise ProtocolError("Expected notification collection")
            for item in elements:
                if item.get("_type") != "Notification" or type(item.get("id")) is not int:
                    raise ProtocolError("Expected Notification")
                ids.add(item["id"])
            href = (result.get("_links", {}).get("nextByOffset") or {}).get("href")
            if not href:
                if len(ids) < result["total"]:
                    raise ProtocolError("Incomplete notification collection; will retry")
                return sorted(ids)
        raise ProtocolError("Notification page limit reached; increase MAX_NOTIFICATION_PAGES")

    async def notification(self, token, notification_id):
        result = await self.request("GET", f"/api/v3/notifications/{notification_id}", token=token)
        if result.get("_type") != "Notification" or result.get("id") != notification_id:
            raise ProtocolError("Expected Notification")
        return result
