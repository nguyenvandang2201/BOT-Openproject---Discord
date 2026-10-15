from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest

from op_discord.openproject import APIError, OpenProject, ProtocolError


def collection(ids, total=None, next_href=None):
    return {
        "_type": "Collection",
        "total": len(ids) if total is None else total,
        "_embedded": {"elements": [{"_type": "Notification", "id": n} for n in ids]},
        "_links": {"nextByOffset": {"href": next_href}} if next_href else {},
    }


async def test_actual_hal_pagination_and_user_token(config):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer user-one-token"
        assert request.url.path == "/api/v3/notifications"
        if request.url.params.get("offset") == "2":
            return httpx.Response(200, json=collection([2], 2))
        assert request.url.params["sortBy"] == '[["id","asc"]]'
        return httpx.Response(200, json=collection([1], 2, "/api/v3/notifications?offset=2"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await OpenProject(config, http).notifications("user-one-token") == [1, 2]
    assert len(calls) == 2


@pytest.mark.parametrize(
    "next_href",
    [
        "https://evil.example/steal",
        "//evil.example/steal",
        "/api/v3/users/me",
        "/api/v3/notifications#fragment",
    ],
)
async def test_no_credentials_to_untrusted_pagination(config, next_href):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=collection([1], 2, next_href))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ProtocolError):
            await OpenProject(config, http).notifications("secret")
    assert len(calls) == 1


async def test_page_cap_and_incomplete_collection(config):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=collection([1], 2, "/api/v3/notifications?offset=2"))
        )
    ) as http:
        with pytest.raises(ProtocolError, match="page limit"):
            await OpenProject(replace(config, max_pages=1), http).notifications("secret")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=collection([1], 2)))
    ) as http:
        with pytest.raises(ProtocolError, match="Incomplete"):
            await OpenProject(config, http).notifications("secret")


async def test_redirect_not_followed(config):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(APIError) as error:
            await OpenProject(config, http).me("secret")
        assert error.value.status == 302
    assert len(calls) == 1


async def test_rate_limit_shared_and_retry_after_honored(config):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        api = OpenProject(config, http)
        for token in ("one", "two"):
            with pytest.raises(APIError) as error:
                await api.me(token)
            assert error.value.retry_after > 119
    assert len(calls) == 1


async def test_refresh_token_form_and_rotation(config):
    def handler(request):
        assert request.method == "POST" and request.url.path == "/oauth/token"
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["refresh_token"]
        assert form["refresh_token"] == ["old-refresh"]
        assert form["client_secret"] == ["client-secret"]
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "token_type": "Bearer",
                "refresh_token": "new-refresh",
                "expires_in": 7200,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        tokens = await OpenProject(config, http).refresh({"refresh_token": "old-refresh"})
        assert tokens["refresh_token"] == "new-refresh"


async def test_error_does_not_expose_response_secrets(config):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(401, text="token=top-secret"))
    ) as http:
        with pytest.raises(APIError) as error:
            await OpenProject(config, http).me("top-secret")
        assert "top-secret" not in str(error.value)
