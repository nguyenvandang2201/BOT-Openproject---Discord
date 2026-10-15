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
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            response = await self.http.request(
                method,
                self.safe_url(href),
                headers=headers,
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
