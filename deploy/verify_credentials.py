"""Check deployment credentials without displaying their values."""

import json
import sys
from pathlib import Path

import httpx


def status_and_json(response):
    try:
        return response.status_code, response.json()
    except json.JSONDecodeError:
        return response.status_code, {}


def main():
    env_path = Path(__file__).resolve().parents[1] / ".env"
    settings = dict(
        line.partition("=")[::2]
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    required = (
        "OPENPROJECT_URL",
        "PUBLIC_URL",
        "OPENPROJECT_CLIENT_ID",
        "OPENPROJECT_CLIENT_SECRET",
        "DISCORD_BOT_TOKEN",
    )
    if any(not settings.get(key) for key in required):
        print("Deployment credentials: missing setting")
        return 1

    redirect = settings["PUBLIC_URL"].rstrip("/") + "/oauth/callback"
    try:
        op_status, op_body = status_and_json(
            httpx.post(
                settings["OPENPROJECT_URL"].rstrip("/") + "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": "invalid-deployment-check-code",
                    "redirect_uri": redirect,
                    "client_id": settings["OPENPROJECT_CLIENT_ID"],
                    "client_secret": settings["OPENPROJECT_CLIENT_SECRET"],
                },
                timeout=20,
            )
        )
    except (httpx.HTTPError, ValueError) as exc:
        print(f"OpenProject OAuth: request failed ({type(exc).__name__})")
        return 1
    op_ok = op_status == 400 and op_body.get("error") == "invalid_grant"
    print(f"OpenProject OAuth client authentication: {'OK' if op_ok else 'FAILED'}")
    if not op_ok:
        print(f"  HTTP {op_status}; error {op_body.get('error', 'unknown')}")

    try:
        discord_status, discord_body = status_and_json(
            httpx.get(
                "https://discord.com/api/v10/users/@me",
                headers={"Authorization": "Bot " + settings["DISCORD_BOT_TOKEN"]},
                timeout=20,
            )
        )
    except (httpx.HTTPError, ValueError) as exc:
        print(f"Discord bot: request failed ({type(exc).__name__})")
        return 1
    discord_ok = discord_status == 200 and str(discord_body.get("id")) == "1560096546375340163"
    print(f"Discord bot authentication: {'OK' if discord_ok else 'FAILED'}")
    if not discord_ok:
        print(f"  HTTP {discord_status}")
    return 0 if op_ok and discord_ok else 1


if __name__ == "__main__":
    sys.exit(main())
