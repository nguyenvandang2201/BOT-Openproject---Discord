"""Read-only compatibility probe. Never sends a Discord message."""

import asyncio
import getpass
import os

import httpx
from dotenv import load_dotenv

from .config import origin


async def check(base, api_token):
    failures = 0
    async with httpx.AsyncClient(trust_env=False) as http:
        # Basic apikey auth also works on older supported OpenProject versions.
        auth = httpx.BasicAuth("apikey", api_token)
        for path in ("/api/v3/spec.json", "/api/v3/users/me", "/api/v3/notifications?pageSize=1"):
            try:
                response = await http.get(
                    base + path, auth=auth, timeout=20, follow_redirects=False
                )
                print(f"{path}: HTTP {response.status_code}")
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("Expected JSON object")
                if path.endswith("spec.json"):
                    for endpoint in (
                        "/api/v3/users/{id}",
                        "/api/v3/notifications",
                        "/api/v3/notifications/{id}",
                    ):
                        ok = "get" in data.get("paths", {}).get(endpoint, {})
                        print(f"  GET {endpoint}: {'OK' if ok else 'MISSING'}")
                        failures += not ok
                elif "users/me" in path:
                    if data.get("_type") != "User" or type(data.get("id")) is not int:
                        raise ValueError("Expected authenticated User")
                    print("  Authenticated User: OK")
                else:
                    if not isinstance(data.get("_embedded", {}).get("elements"), list):
                        raise ValueError("Expected notification collection")
                    print("  Notification collection: OK")
            except (httpx.HTTPError, ValueError, AttributeError) as error:
                print(
                    f"  FAIL ({type(error).__name__}); check authentication, API docs and proxy/WAF"
                )
                failures += 1
    return 1 if failures else 0


def main():
    load_dotenv()
    base = origin(os.getenv("OPENPROJECT_URL", "https://openproject.lazy247.space"))
    token = getpass.getpass("OpenProject personal API token (hidden, not stored): ")
    if not token:
        raise SystemExit("A token is required for the authenticated compatibility check")
    raise SystemExit(asyncio.run(check(base, token)))
