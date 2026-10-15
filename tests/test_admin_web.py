from unittest.mock import AsyncMock

import httpx

from op_discord.admin_web import COOKIE
from op_discord.bot import dm_card
from op_discord.web import create_app


def setup(store, api):
    bot = AsyncMock()
    bot.is_manager.return_value = True
    app = create_app(store, api, admin_bot=bot, admin_url="http://192.168.1.12:8081")
    return app, bot


async def test_admin_ticket_single_use_and_manager_recheck(store, api):
    app, bot = setup(store, api)
    ticket = store.admin_ticket("123")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://192.168.1.12:8081") as client:
        assert (await client.get("/admin")).status_code == 401
        wrong_host = await client.get(f"/admin/login?ticket={ticket}", headers={"Host": "public.example"})
        assert wrong_host.status_code == 404
        result = await client.get(f"/admin/login?ticket={ticket}", follow_redirects=False)
        assert result.status_code == 303
        assert "httponly" in result.headers["set-cookie"].lower()
        assert "samesite=strict" in result.headers["set-cookie"].lower()
        assert (await client.get(f"/admin/login?ticket={ticket}")).status_code == 403
        assert (await client.get("/admin")).status_code == 200
        bot.is_manager.return_value = False
        assert (await client.get("/admin")).status_code == 401


async def test_admin_links_welcome_csrf_actions_and_logout(store, api):
    app, bot = setup(store, api)
    ticket = store.admin_ticket("123")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://192.168.1.12:8081") as client:
        await client.get(f"/admin/login?ticket={ticket}")
        session = client.cookies.get(COOKIE)
        csrf = store.admin_csrf(session)
        assert (await client.post("/admin/action", data={"action": "link", "op_id": "12",
                                                          "discord_id": "456"})).status_code == 403
        values = {"csrf": csrf, "action": "link", "op_id": "12", "discord_id": "456",
                  "name": "<script>alert(1)</script>"}
        response = await client.post("/admin/action", data=values, follow_redirects=False)
        assert response.status_code == 303
        bot.verify_member.assert_awaited_once_with("456")
        jobs = store.dm_jobs(10)
        assert [job["kind"] for job in jobs] == ["welcome"]
        card, view = dm_card(jobs[0], "https://op.example")
        assert "Chào mừng" in card.title
        assert view is None
        assert "&lt;script&gt;" in (await client.get("/admin")).text
        assert "<script>alert(1)</script>" not in (await client.get("/admin")).text
        await client.post("/admin/action", data=values)
        assert len(store.dm_jobs(10)) == 1
        assert (await client.post("/admin/action", data={**values, "op_id": "13"})).status_code == 409
        suspend = await client.post("/admin/action", data={"csrf": csrf, "action": "suspend",
                                                           "op_id": "12"})
        assert suspend.status_code == 303
        assert store.dm_links()[0]["enabled"] == 0
        await client.post("/admin/action", data={"csrf": csrf, "action": "resume", "op_id": "12"})
        assert store.dm_links()[0]["enabled"] == 1
        await client.post("/admin/logout", data={"csrf": csrf})
        assert (await client.get("/admin")).status_code == 401


async def test_admin_rejects_invalid_member_and_expired_ticket(store, api):
    app, bot = setup(store, api)
    ticket = store.admin_ticket("123")
    store.db.execute("UPDATE admin_tickets SET expires=0")
    store.db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://192.168.1.12:8081") as client:
        assert (await client.get(f"/admin/login?ticket={ticket}")).status_code == 403
        ticket = store.admin_ticket("123")
        await client.get(f"/admin/login?ticket={ticket}")
        from op_discord.service import DeliveryError
        bot.verify_member.side_effect = DeliveryError("member_absent", permanent=True)
        response = await client.post("/admin/action", data={"csrf": store.admin_csrf(client.cookies.get(COOKIE)),
                                                              "action": "link", "op_id": "12",
                                                              "discord_id": "456"})
        assert response.status_code == 400
        assert store.dm_links() == []
