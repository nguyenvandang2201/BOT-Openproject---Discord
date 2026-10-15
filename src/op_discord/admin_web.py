"""LAN-only, Discord-authorized management of OpenProject to Discord links."""

import hmac
import html
import ipaddress
import logging
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .service import DeliveryError

log = logging.getLogger(__name__)
COOKIE = "opbridge_admin"
ID = re.compile(r"[1-9][0-9]{0,18}\Z")
CSS = """
:root{color-scheme:dark;font-family:Inter,system-ui,sans-serif;background:#111827;color:#f3f4f6}
*{box-sizing:border-box}body{margin:0}main{max-width:1100px;margin:36px auto;padding:0 20px}
h1{font-size:30px;margin:0 0 8px}h2{font-size:19px;margin:0 0 18px}p{color:#aeb8c8}
.top{display:flex;justify-content:space-between;align-items:start;gap:20px;margin-bottom:24px}
.card{background:#1c2738;border:1px solid #344256;border-radius:16px;padding:24px;margin:18px 0}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
label{display:block;font-size:13px;color:#b9c6d7}input{width:100%;padding:12px;margin-top:6px;
background:#101827;border:1px solid #475569;border-radius:8px;color:#fff;font:inherit}
button{background:#17846b;border:0;border-radius:8px;padding:10px 14px;color:white;
font:inherit;font-weight:650;cursor:pointer}button.secondary{background:#344256}
button.danger{background:#923e48}button:hover{filter:brightness(1.15)}
.actions{display:flex;gap:8px;flex-wrap:wrap}.notice{background:#154333;border:1px solid #1b7652;
padding:12px 16px;border-radius:8px}.error{background:#5a2730;border-color:#ad4855}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px;border-bottom:1px solid #344256}
td{overflow-wrap:anywhere}th{color:#aeb8c8;font-size:13px}small{color:#aeb8c8}
.tablewrap{overflow:auto}.pill{padding:4px 8px;border-radius:16px;background:#1b624f;
white-space:nowrap}.off{background:#6b4148}form.inline{display:inline}
@media(max-width:750px){.grid{grid-template-columns:1fr}.top{display:block}}
"""


def esc(value):
    return html.escape(str(value), quote=True)


def document(title, body, status=200):
    return HTMLResponse(
        '<!doctype html><html lang="vi"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)}</title><style>{CSS}</style></head>"
        f"<body><main>{body}</main></body></html>", status_code=status,
    )


def form(action, label, csrf, op_id, button_class="secondary"):
    return (
        '<form class="inline" method="post" action="/admin/action">'
        f'<input type="hidden" name="csrf" value="{csrf}">'
        f'<input type="hidden" name="action" value="{action}">'
        f'<input type="hidden" name="op_id" value="{op_id}">'
        f'<button class="{button_class}" type="submit">{label}</button></form>'
    )


def positive(value):
    if not ID.fullmatch(value) or int(value) >= 2**63:
        raise ValueError("ID phải là số nguyên dương hợp lệ.")
    return int(value)


def read_form(body):
    if len(body) > 8192:
        raise ValueError("Biểu mẫu quá lớn.")
    values = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
    if any(len(items) != 1 for items in values.values()):
        raise ValueError("Biểu mẫu không hợp lệ.")
    return {key: items[0] for key, items in values.items()}


def register_admin(app, store, bot, admin_url):
    if not admin_url:
        return
    origin = urlsplit(admin_url)
    expected_host = origin.netloc.lower()
    secure = origin.scheme == "https"

    def on_lan_host(request):
        if request.headers.get("host", "").lower() != expected_host:
            return False
        # The page is opened directly on the LAN; public reverse proxies add these headers.
        if any(header in request.headers for header in (
            "forwarded", "x-forwarded-for", "x-real-ip", "cf-connecting-ip"
        )):
            return False
        try:
            address = ipaddress.ip_address(request.client.host)
        except (ValueError, AttributeError):
            return False
        return address.is_private or address.is_loopback

    async def authorized(request):
        session = request.cookies.get(COOKIE, "")
        discord_id = store.admin_session(session)
        if not discord_id or not await bot.is_manager(discord_id):
            return None
        return session

    @app.get("/admin/login")
    async def login(request: Request, ticket: str = ""):
        if not on_lan_host(request):
            return document("Không tìm thấy", "<h1>Không tìm thấy</h1>", 404)
        if not ticket or len(ticket) > 128:
            return document("Liên kết không hợp lệ", "<h1>Liên kết không hợp lệ</h1>", 400)
        # Consume the ticket before checking permission so a failed attempt cannot be replayed.
        session = store.admin_login(ticket)
        discord_id = store.admin_session(session or "")
        if not discord_id or not await bot.is_manager(discord_id):
            if session:
                store.admin_logout(session)
            return document("Không có quyền", "<h1>Liên kết hết hạn hoặc bạn không còn quyền quản lý.</h1>", 403)
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie(COOKIE, session, max_age=3600, path="/admin", httponly=True,
                            secure=secure, samesite="strict")
        return response

    @app.get("/admin")
    async def index(request: Request, notice: str = ""):
        if not on_lan_host(request):
            return document("Không tìm thấy", "<h1>Không tìm thấy</h1>", 404)
        session = await authorized(request)
        if not session:
            return document("Cần đăng nhập", "<h1>Cần đăng nhập</h1><p>Dùng /op admin trong Discord server để lấy liên kết mới.</p>", 401)
        csrf = store.admin_csrf(session)
        messages = {
            "linked": "Đã lưu liên kết. Nếu là liên kết mới, lời chào đang chờ gửi DM.",
            "suspended": "Đã tạm dừng thông báo.",
            "resumed": "Đã bật lại thông báo.",
            "removed": "Đã gỡ liên kết.",
            "retried": "Đã đưa các DM lỗi trở lại hàng đợi.",
            "unchanged": "Liên kết đã tồn tại và không thay đổi.",
        }
        banner = f'<p class="notice">{messages[notice]}</p>' if notice in messages else ""
        links = store.dm_links()
        failures = store.dm_failures()
        rows = []
        for link in links:
            op_id = link["op_id"]
            active = bool(link["enabled"])
            actions = [form("suspend" if active else "resume", "Tạm dừng" if active else "Bật lại",
                            csrf, op_id)]
            actions.append(form("retry", "Gửi lại lỗi", csrf, op_id))
            actions.append(form("unlink", "Gỡ", csrf, op_id, "danger"))
            rows.append(
                f"<tr><td>{op_id}</td><td>{esc(link['discord_id'])}</td>"
                f"<td>{esc(link['name'])}</td>"
                f'<td><span class="pill {"" if active else "off"}">'
                f'{"Đang bật" if active else "Tạm dừng"}</span></td>'
                f"<td>{store.dm_pending_count(link['discord_id'])}</td>"
                f'<td><div class="actions">{"".join(actions)}</div></td></tr>'
            )
        failure_rows = "".join(
            f"<tr><td>{esc(item['op_id'])}</td><td>{esc(item['event_key'])}</td>"
            f"<td>{esc(item['error'] or '')}</td></tr>" for item in failures[:50]
        )
        body = (
            '<div class="top"><div><h1>Quản lý thông báo OpenProject</h1>'
            '<p>Liên kết tài khoản với Discord · Chỉ dành cho quản lý server</p></div>'
            '<form method="post" action="/admin/logout">'
            f'<input type="hidden" name="csrf" value="{csrf}">'
            '<button class="secondary">Đăng xuất</button></form></div>'
            f"{banner}<section class=card><h2>Thêm hoặc cập nhật liên kết</h2>"
            '<form method="post" action="/admin/action"><div class="grid">'
            '<label>OpenProject user ID<input name="op_id" inputmode="numeric" required></label>'
            '<label>Discord user ID<input name="discord_id" inputmode="numeric" required></label>'
            '<label>Tên hiển thị<input name="name" maxlength="200"></label></div>'
            f'<input type="hidden" name="csrf" value="{csrf}">'
            '<input type="hidden" name="action" value="link">'
            '<p><button type="submit">Lưu liên kết</button></p></form>'
            '<small>Lấy OpenProject user ID từ hồ sơ người dùng; bật Developer Mode trong Discord để sao chép user ID. '
            'Người dùng phải đang ở trong server. Liên kết mới sẽ nhận một DM chào mừng.</small></section>'
            '<section class=card><h2>Liên kết hiện có</h2><div class=tablewrap><table>'
            '<thead><tr><th>OP ID</th><th>Discord ID</th><th>Tên</th><th>Trạng thái</th>'
            '<th>Đang chờ</th><th>Thao tác</th></tr></thead><tbody>'
            f'{"".join(rows) or "<tr><td colspan=6>Chưa có liên kết.</td></tr>"}'
            '</tbody></table></div></section>'
            '<section class=card><h2>DM gửi lỗi</h2><div class=tablewrap><table>'
            '<thead><tr><th>OP ID</th><th>Sự kiện</th><th>Lý do</th></tr></thead><tbody>'
            f'{failure_rows or "<tr><td colspan=3>Không có lỗi.</td></tr>"}'
            '</tbody></table></div></section>'
        )
        return document("Quản lý OpenProject Discord", body)

    @app.post("/admin/action")
    async def action(request: Request):
        if not on_lan_host(request):
            return document("Không tìm thấy", "<h1>Không tìm thấy</h1>", 404)
        session = await authorized(request)
        if not session:
            return document("Không có quyền", "<h1>Không có quyền</h1>", 403)
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
            return document("Biểu mẫu không hợp lệ", "<h1>Biểu mẫu không hợp lệ</h1>", 400)
        try:
            values = read_form(await request.body())
            if not hmac.compare_digest(values.get("csrf", ""), store.admin_csrf(session)):
                return document("Không có quyền", "<h1>Phiên biểu mẫu không hợp lệ.</h1>", 403)
            op_id = positive(values.get("op_id", ""))
            operation = values.get("action", "")
            if operation == "link":
                discord_id = str(positive(values.get("discord_id", "")))
                await bot.verify_member(discord_id)
                changed = store.dm_link(op_id, discord_id, values.get("name", "").strip()[:200])
                notice = "linked" if changed else "unchanged"
            elif operation == "suspend":
                if not store.dm_set_enabled(op_id, False):
                    raise ValueError("Không tìm thấy liên kết.")
                notice = "suspended"
            elif operation == "resume":
                if not store.dm_set_enabled(op_id, True):
                    raise ValueError("Không tìm thấy liên kết.")
                notice = "resumed"
            elif operation == "unlink":
                if not store.dm_unlink(op_id):
                    raise ValueError("Không tìm thấy liên kết.")
                notice = "removed"
            elif operation == "retry":
                store.dm_retry_failed(op_id)
                notice = "retried"
            else:
                raise ValueError("Thao tác không hợp lệ.")
        except DeliveryError as error:
            message = "Người dùng chưa ở trong server Discord." if error.code == "member_absent" else "Không kiểm tra được thành viên Discord."
            return document("Không thể lưu", f'<h1>Không thể lưu</h1><p>{message}</p><a href="/admin">Quay lại</a>', 400)
        except sqlite3.IntegrityError:
            return document("Không thể lưu", '<h1>Discord ID đã liên kết với OpenProject ID khác.</h1><a href="/admin">Quay lại</a>', 409)
        except (ValueError, UnicodeError) as error:
            return document("Không thể lưu", f'<h1>Không thể lưu</h1><p>{esc(error)}</p><a href="/admin">Quay lại</a>', 400)
        log.info("admin_action action=%s op_id=%s", operation, op_id)
        return RedirectResponse(f"/admin?notice={notice}", status_code=303)

    @app.post("/admin/logout")
    async def logout(request: Request):
        if not on_lan_host(request):
            return document("Không tìm thấy", "<h1>Không tìm thấy</h1>", 404)
        session = await authorized(request)
        if not session:
            return document("Không có quyền", "<h1>Không có quyền</h1>", 403)
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
            return document("Biểu mẫu không hợp lệ", "<h1>Biểu mẫu không hợp lệ</h1>", 400)
        try:
            values = read_form(await request.body())
        except (ValueError, UnicodeError):
            return document("Biểu mẫu không hợp lệ", "<h1>Biểu mẫu không hợp lệ</h1>", 400)
        if not hmac.compare_digest(values.get("csrf", ""), store.admin_csrf(session)):
            return document("Không có quyền", "<h1>Phiên biểu mẫu không hợp lệ.</h1>", 403)
        store.admin_logout(session)
        response = RedirectResponse("/admin", status_code=303)
        response.delete_cookie(COOKIE, path="/admin", secure=secure, httponly=True,
                               samesite="strict")
        return response
