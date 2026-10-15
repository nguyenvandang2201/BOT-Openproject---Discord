import html
import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .webhook import parse_event, valid_signature

log = logging.getLogger(__name__)
COOKIE = "__Host-opbridge"
MAX_WEBHOOK_BODY = 1024 * 1024


def page(text, status=200):
    return HTMLResponse(
        '<!doctype html><html lang="vi"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Liên kết OpenProject với Discord</title><body>"
        "<h1>OpenProject ↔ Discord</h1><p>" + html.escape(text) + "</p></body></html>",
        status_code=status,
    )


def create_app(store, api, ready=lambda: True, *, allow_oauth=False, webhook_secret=""):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request, call_next):
        try:
            response = await call_next(request)
        except Exception as error:
            log.error("web_failed kind=%s", type(error).__name__)
            response = page("Không thể hoàn tất. Vui lòng chạy /op link lại.", 500)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
                "X-Content-Type-Options": "nosniff",
            }
        )
        return response

    @app.get("/healthz")
    async def health():
        return {"status": "alive"}

    @app.get("/readyz")
    async def readiness():
        ok = ready()
        return JSONResponse({"ready": ok}, status_code=200 if ok else 503)

    @app.post("/webhooks/openproject")
    async def openproject_webhook(request: Request):
        if not webhook_secret:
            return JSONResponse({"error": "disabled"}, status_code=404)
        if request.headers.get("content-length", "").isdigit() and int(
            request.headers["content-length"]
        ) > MAX_WEBHOOK_BODY:
            return JSONResponse({"error": "payload_too_large"}, status_code=413)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_WEBHOOK_BODY:
                return JSONResponse({"error": "payload_too_large"}, status_code=413)
            body.extend(chunk)
        body = bytes(body)
        if not valid_signature(body, request.headers.get("x-op-signature", ""), webhook_secret):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            event = parse_event(body)
            if event is None:
                return JSONResponse({"accepted": True, "queued": 0}, status_code=202)
            queued = store.accept_webhook(event)
        except (ValueError, TypeError, KeyError):
            return JSONResponse({"error": "invalid_payload"}, status_code=400)
        return JSONResponse({"accepted": True, "queued": queued}, status_code=202)

    @app.get("/link")
    async def link(ticket: str = ""):
        if not allow_oauth:
            return page("Liên kết do quản lý thực hiện.", 404)
        if len(ticket) > 128:
            return page("Liên kết không hợp lệ.", 400)
        flow = store.begin_oauth(ticket)
        if not flow:
            return page("Liên kết hết hạn hoặc đã dùng. Chạy /op link lại.", 400)
        state, browser = flow
        response = RedirectResponse(api.authorize_url(state), status_code=303)
        response.set_cookie(
            COOKIE, browser, max_age=600, secure=True, httponly=True, samesite="lax", path="/"
        )
        return response

    @app.get("/oauth/callback")
    async def callback(request: Request, state: str = "", code: str = "", error: str = ""):
        if not allow_oauth:
            return page("Liên kết do quản lý thực hiện.", 404)
        if len(state) > 128 or len(code) > 4096:
            return page("Phiên xác thực không hợp lệ.", 400)
        flow = store.consume_state(state, request.cookies.get(COOKIE, ""))
        if not flow:
            return page("Phiên xác thực sai, hết hạn hoặc đã dùng. Chạy /op link lại.", 400)
        if error or not code:
            return page("Bạn chưa cấp quyền. Có thể chạy /op link để thử lại.", 400)
        try:
            tokens = await api.exchange(code)
            me = await api.me(tokens["access_token"])
            confirm = store.set_pending(
                flow["discord_id"],
                flow["expires"],
                {
                    "id": me["id"],
                    "name": str(me.get("name", me["id"])),
                    "tokens": tokens,
                },
            )
            if not confirm:
                return page("Phiên đã bị hủy hoặc hết hạn. Chạy /op link lại.", 400)
        except Exception as exc:
            log.warning("oauth_failed kind=%s", type(exc).__name__)
            return page(
                "Không xác thực được với OpenProject. Chạy /op link lại hoặc báo quản trị.", 502
            )
        response = page(
            f"Tài khoản OpenProject: {me.get('name', '')} (ID {me['id']}). "
            f"Nếu đúng tài khoản của bạn, trở về chính tài khoản Discord đã chạy /op link "
            f"và nhập /op confirm code:{confirm}. "
            "Mã chỉ dùng một lần, hết hạn cùng phiên liên kết sau tối đa 10 phút. "
            "Bot chỉ bắt đầu gửi thông báo sau khi xác nhận."
        )
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        return response

    return app
