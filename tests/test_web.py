import re
from urllib.parse import parse_qs, urlsplit

import httpx

from op_discord.openproject import OpenProject
from op_discord.web import COOKIE, create_app


async def test_oauth_http_flow_requires_cookie_and_discord_confirmation(config, store):
    requests = []

    def upstream(request):
        requests.append(request)
        if request.url.path == "/oauth/token":
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["authorization_code"]
            assert form["redirect_uri"] == [config.redirect_uri]
            return httpx.Response(
                200,
                json={
                    "access_token": "oauth-secret",
                    "token_type": "Bearer",
                    "refresh_token": "refresh",
                    "expires_in": 7200,
                },
            )
        assert request.url.path == "/api/v3/users/me"
        return httpx.Response(
            200, json={"_type": "User", "id": 10, "name": "<script>Alice</script>"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as op_http:
        app = create_app(store, OpenProject(config, op_http))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=config.public_url
        ) as browser:
            ticket = store.start_flow("100")
            start = await browser.get("/link", params={"ticket": ticket})
            assert start.status_code == 303
            assert "Secure" in start.headers["set-cookie"]
            assert "HttpOnly" in start.headers["set-cookie"]
            state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
            cookie = browser.cookies.get(COOKIE)
            browser.cookies.clear()
            bad = await browser.get("/oauth/callback", params={"state": state, "code": "code"})
            assert bad.status_code == 400
            assert not requests
            browser.cookies.set(COOKIE, cookie)
            success = await browser.get("/oauth/callback", params={"state": state, "code": "code"})
            assert success.status_code == 200
            assert "<script>" not in success.text
            assert "oauth-secret" not in success.text
            assert success.headers["cache-control"] == "no-store"
            confirm = re.search(r"code:([a-f0-9]{16})", success.text)[1]
            assert store.pending("100", confirm)["id"] == 10
            assert store.pending("200", confirm) is None
            assert store.account("100") is None
            replay = await browser.get("/oauth/callback", params={"state": state, "code": "code"})
            assert replay.status_code == 400
            assert len(requests) == 2


async def test_readiness_separate_from_liveness(store, api):
    app = create_app(store, api, ready=lambda: False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://bot.example"
    ) as http:
        assert (await http.get("/healthz")).status_code == 200
        assert (await http.get("/readyz")).status_code == 503
