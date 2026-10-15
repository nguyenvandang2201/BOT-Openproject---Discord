"""LAN-only, Discord-authorized management of OpenProject to Discord links."""

import csv
import hmac
import html
import io
import ipaddress
import json
import logging
import re
import sqlite3
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .openproject import APIError, ProtocolError
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
label{display:block;font-size:13px;color:#b9c6d7}input,select,textarea{width:100%;padding:12px;margin-top:6px;
background:#101827;border:1px solid #475569;border-radius:8px;color:#fff;font:inherit}
input[type=checkbox]{width:auto;margin-right:8px}
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


def read_form(body, limit=8192):
    if len(body) > limit:
        raise ValueError("Biểu mẫu quá lớn.")
    values = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
    if any(len(items) != 1 for items in values.values()):
        raise ValueError("Biểu mẫu không hợp lệ.")
    return {key: items[0] for key, items in values.items()}


async def read_request_form(request, limit=8192):
    if request.headers.get("content-length", "").isdigit() and int(
        request.headers["content-length"]
    ) > limit:
        raise ValueError("Biểu mẫu quá lớn.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise ValueError("Biểu mẫu quá lớn.")
        body.extend(chunk)
    return read_form(bytes(body), limit)


def when(value):
    return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%d %H:%M UTC")


def parse_links_csv(value):
    if len(value.encode("utf-8")) > 20000:
        raise ValueError("CSV quá lớn (tối đa 20 KB).")
    reader = csv.DictReader(io.StringIO(value.lstrip("\ufeff")))
    headers = reader.fieldnames or []
    if not {"op_id", "discord_id"}.issubset(headers) or set(headers) - {
        "op_id", "discord_id", "name"
    }:
        raise ValueError("CSV cần cột op_id,discord_id và có thể thêm name.")
    rows, op_ids, discord_ids = [], set(), set()
    for line, item in enumerate(reader, 2):
        if line > 26:
            raise ValueError("Mỗi lần nhập tối đa 25 liên kết.")
        if None in item or any(value is None for value in item.values()):
            raise ValueError(f"Dòng {line}: số cột không hợp lệ.")
        try:
            op_id = positive(item["op_id"].strip())
            discord_id = str(positive(item["discord_id"].strip()))
        except ValueError:
            raise ValueError(f"Dòng {line}: ID không hợp lệ.") from None
        if op_id in op_ids or discord_id in discord_ids:
            raise ValueError(f"Dòng {line}: ID bị lặp trong CSV.")
        op_ids.add(op_id)
        discord_ids.add(discord_id)
        rows.append((op_id, discord_id, (item.get("name") or "").strip()[:200]))
    if not rows:
        raise ValueError("CSV chưa có dữ liệu.")
    return rows


def post_button(action, label, csrf, **fields):
    hidden = "".join(
        f'<input type="hidden" name="{esc(key)}" value="{esc(value)}">'
        for key, value in fields.items()
    )
    return (
        '<form class="inline" method="post" action="/admin/action">'
        f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
        f'<input type="hidden" name="action" value="{esc(action)}">'
        f'{hidden}<button class="secondary" type="submit">{esc(label)}</button></form>'
    )


def register_admin(app, store, bot, api, admin_url):
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
        store.audit_admin(discord_id, "login", "admin")
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
            "test_queued": "Đã xếp một DM thử vào hàng đợi.",
            "job_retried": "Đã đưa một DM lỗi trở lại hàng đợi.",
            "event_retried": "Đã đưa webhook lỗi trở lại hàng đợi phân giải.",
            "rule_saved": "Đã lưu quy tắc người nhận.",
            "imported": "Đã nhập các liên kết hợp lệ và xếp lời chào mới.",
            "unchanged": "Liên kết đã tồn tại và không thay đổi.",
        }
        banner = f'<p class="notice">{messages[notice]}</p>' if notice in messages else ""
        links = store.dm_links()
        failures = store.dm_failures()
        inbound_status = store.inbound_status()
        inbound_failures = store.inbound_failures()
        watcher_errors = store.watcher_errors()
        rows = []
        for link in links:
            op_id = link["op_id"]
            active = bool(link["enabled"])
            actions = [form("suspend" if active else "resume", "Tạm dừng" if active else "Bật lại",
                            csrf, op_id)]
            actions.append(form("retry", "Gửi lại lỗi", csrf, op_id))
            actions.append(post_button("test", "Thử DM", csrf, op_id=op_id))
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
        inbound_rows = "".join(
            f"<tr><td>{esc(item['source_event_id'])}</td>"
            f"<td>{item['work_package_id']}</td><td>{esc(item['error'] or '')}</td></tr>"
            for item in inbound_failures
        )
        watcher_rows = "".join(
            f"<tr><td>{item['work_package_id']}</td><td>{esc(item['error'])}</td>"
            f"<td>{item['attempts']}</td></tr>" for item in watcher_errors
        )
        body = (
            '<div class="top"><div><h1>Quản lý thông báo OpenProject</h1>'
            '<p>Liên kết tài khoản với Discord · Chỉ dành cho quản lý server</p></div>'
            '<form method="post" action="/admin/logout">'
            f'<input type="hidden" name="csrf" value="{csrf}">'
            '<button class="secondary">Đăng xuất</button></form></div>'
            f'{banner}<p><a href="/admin/lookup">Tra cứu tài khoản</a> · '
            '<a href="/admin/history">Lịch sử sự kiện và DM</a> · '
            '<a href="/admin/rules">Quy tắc người nhận</a> · '
            '<a href="/admin/audit">Nhật ký quản trị</a></p>'
            '<section class=card><h2>Thêm hoặc cập nhật liên kết</h2>'
            '<form method="post" action="/admin/action"><div class="grid">'
            '<label>OpenProject user ID<input name="op_id" inputmode="numeric" required></label>'
            '<label>Discord user ID<input name="discord_id" inputmode="numeric" required></label>'
            '<label>Tên hiển thị<input name="name" maxlength="200"></label></div>'
            f'<input type="hidden" name="csrf" value="{csrf}">'
            '<input type="hidden" name="action" value="link">'
            '<p><button type="submit">Lưu liên kết</button></p></form>'
            '<small>Tra cứu OpenProject ID và Discord ID trước khi lưu; bật Developer Mode trong Discord để sao chép user ID. '
            'Người dùng phải đang ở trong server. Liên kết mới sẽ nhận một DM chào mừng.</small></section>'
            '<section class=card><h2>Nhập nhiều liên kết bằng CSV</h2>'
            '<p>Cột: op_id,discord_id,name. Tối đa 25 dòng; xem trước trước khi lưu.</p>'
            '<form method="post" action="/admin/import">'
            '<textarea name="csv" rows="5" maxlength="20000" style="width:100%" '
            'placeholder="op_id,discord_id,name"></textarea>'
            f'<input type="hidden" name="csrf" value="{csrf}">'
            '<input type="hidden" name="mode" value="preview">'
            '<p><button type="submit">Xem trước</button></p></form></section>'
            '<section class=card><h2>Liên kết hiện có</h2><div class=tablewrap><table>'
            '<thead><tr><th>OP ID</th><th>Discord ID</th><th>Tên</th><th>Trạng thái</th>'
            '<th>Đang chờ</th><th>Thao tác</th></tr></thead><tbody>'
            f'{"".join(rows) or "<tr><td colspan=6>Chưa có liên kết.</td></tr>"}'
            '</tbody></table></div></section>'
            '<section class=card><h2>DM gửi lỗi</h2><div class=tablewrap><table>'
            '<thead><tr><th>OP ID</th><th>Sự kiện</th><th>Lý do</th></tr></thead><tbody>'
            f'{failure_rows or "<tr><td colspan=3>Không có lỗi.</td></tr>"}'
            '</tbody></table></div></section>'
            '<section class=card><h2>Phân giải webhook</h2>'
            f'<p>Đang chờ: {inbound_status.get("pending", 0)} · '
            f'Lỗi: {inbound_status.get("failed", 0)}</p>'
            '<div class=tablewrap><table><thead><tr><th>Sự kiện</th><th>Task</th>'
            '<th>Lý do</th></tr></thead><tbody>'
            f'{inbound_rows or "<tr><td colspan=3>Không có lỗi.</td></tr>"}'
            '</tbody></table></div></section>'
            '<section class=card><h2>Đối soát Watchers</h2><div class=tablewrap><table>'
            '<thead><tr><th>Task</th><th>Lỗi</th><th>Số lần thử</th></tr></thead><tbody>'
            f'{watcher_rows or "<tr><td colspan=3>Không có lỗi.</td></tr>"}'
            '</tbody></table></div></section>'
        )
        return document("Quản lý OpenProject Discord", body)

    @app.get("/admin/lookup")
    async def lookup(request: Request, q: str = "", discord_id: str = ""):
        if not on_lan_host(request):
            return document("Không tìm thấy", "<h1>Không tìm thấy</h1>", 404)
        if not await authorized(request):
            return document("Không có quyền", "<h1>Không có quyền</h1>", 403)
        q, discord_id = q.strip(), discord_id.strip()
        if len(q) > 80 or len(discord_id) > 20:
            return document("Tra cứu", "<h1>Giá trị tìm kiếm quá dài.</h1>", 400)
        users, member, errors = [], None, []
        try:
            if q:
                users = ([await api.service_user(positive(q))] if q.isdecimal()
                         else await api.search_users(q))
        except (APIError, ProtocolError, ValueError) as error:
            errors.append(f"OpenProject: {type(error).__name__} "
                          f"{error.status if isinstance(error, APIError) else ''}")
        try:
            if discord_id:
                member = await bot.member_summary(str(positive(discord_id)))
        except (DeliveryError, ValueError):
            errors.append("Discord: không tìm thấy thành viên hoặc không kiểm tra được.")
        user_rows = "".join(
            f"<tr><td>{item['id']}</td><td>{esc(item['name'])}</td>"
            f"<td>{esc(item['status'])}</td></tr>" for item in users
        )
        member_html = (
            f'<section class="card"><h2>Discord</h2><p>{esc(member["name"])} '
            f'(ID {esc(member["id"])})</p></section>' if member else ''
        )
        body = (
            '<p><a href="/admin">← Trang quản lý</a></p><h1>Tra cứu tài khoản</h1>'
            '<form method="get" action="/admin/lookup"><div class="grid">'
            f'<label>OpenProject ID hoặc tên<input name="q" value="{esc(q)}"></label>'
            f'<label>Discord ID<input name="discord_id" value="{esc(discord_id)}"></label>'
            '</div><p><button type="submit">Tra cứu</button></p></form>'
            f'<p>{esc("; ".join(errors))}</p>'
            '<section class="card"><h2>OpenProject</h2><table><thead><tr>'
            '<th>ID</th><th>Tên</th><th>Trạng thái</th></tr></thead><tbody>'
            f'{user_rows or "<tr><td colspan=3>Chưa có kết quả.</td></tr>"}'
            '</tbody></table></section>'
            f'{member_html}'
        )
        return document("Tra cứu tài khoản", body)

    @app.post("/admin/import")
    async def import_links(request: Request):
        if not on_lan_host(request):
            return document("Không tìm thấy", "<h1>Không tìm thấy</h1>", 404)
        session = await authorized(request)
        if not session:
            return document("Không có quyền", "<h1>Không có quyền</h1>", 403)
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
            return document("Biểu mẫu không hợp lệ", "<h1>Biểu mẫu không hợp lệ</h1>", 400)
        try:
            values = await read_request_form(request, 65536)
            if not hmac.compare_digest(values.get("csrf", ""), store.admin_csrf(session)):
                return document("Không có quyền", "<h1>Phiên biểu mẫu không hợp lệ.</h1>", 403)
            mode = values.get("mode")
            if mode not in ("preview", "apply"):
                raise ValueError("Thao tác nhập không hợp lệ.")
            rows = parse_links_csv(values.get("csv", ""))
            linked_discord_ids = {link["discord_id"]: link["op_id"] for link in store.dm_links()}
            checked = []
            for line, (op_id, discord_id, name) in enumerate(rows, 2):
                if linked_discord_ids.get(discord_id, op_id) != op_id:
                    raise ValueError(f"Dòng {line}: Discord ID đã liên kết với OP ID khác.")
                try:
                    user = await api.service_user(op_id)
                except APIError as error:
                    raise ValueError(f"Dòng {line}: OpenProject API HTTP {error.status}.") from None
                except ProtocolError:
                    raise ValueError(f"Dòng {line}: OpenProject trả dữ liệu không hợp lệ.") from None
                if user["id"] != op_id or user["status"] != "active":
                    raise ValueError(f"Dòng {line}: OpenProject ID {op_id} không active.")
                try:
                    member = await bot.member_summary(discord_id)
                except DeliveryError:
                    raise ValueError(f"Dòng {line}: không kiểm tra được Discord ID.") from None
                if member["id"] != discord_id:
                    raise ValueError(f"Dòng {line}: Discord ID không khớp.")
                checked.append((op_id, discord_id, name or user["name"], member["name"]))
            if mode == "apply":
                store.bulk_link([(op_id, discord_id, name) for op_id, discord_id, name, _ in checked])
                targets = ",".join(str(op_id) for op_id, _, _, _ in checked)
                store.audit_admin(store.admin_session(session), "bulk_link",
                                  f"{len(checked)} links: {targets}")
                return RedirectResponse("/admin?notice=imported", status_code=303)
        except (ValueError, UnicodeError, sqlite3.IntegrityError, DeliveryError,
                APIError, ProtocolError) as error:
            if isinstance(error, APIError):
                message = f"OpenProject API HTTP {error.status}; kiểm tra quyền tài khoản dịch vụ."
            elif isinstance(error, DeliveryError):
                message = "Không tìm thấy một thành viên Discord hoặc không kiểm tra được."
            elif isinstance(error, (sqlite3.IntegrityError, ProtocolError)):
                message = "Dữ liệu liên kết xung đột hoặc API trả dữ liệu không hợp lệ."
            else:
                message = str(error)
            return document("Không thể nhập", f'<h1>Không thể nhập</h1><p>{esc(message)}</p>'
                            '<a href="/admin">Quay lại</a>', 400)
        preview = "".join(
            f"<tr><td>{op_id}</td><td>{esc(name)}</td><td>{esc(discord_id)}</td>"
            f"<td>{esc(member_name)}</td></tr>"
            for op_id, discord_id, name, member_name in checked
        )
        body = (
            '<p><a href="/admin">← Trang quản lý</a></p><h1>Xem trước CSV</h1>'
            '<p>Kiểm tra từng dòng trước khi lưu. Các liên kết mới sẽ nhận DM chào mừng.</p>'
            '<table><thead><tr><th>OP ID</th><th>Tên OP</th><th>Discord ID</th>'
            f'<th>Thành viên</th></tr></thead><tbody>{preview}</tbody></table>'
            '<form method="post" action="/admin/import">'
            f'<textarea name="csv" hidden>{esc(values["csv"])}</textarea>'
            f'<input type="hidden" name="csrf" value="{esc(store.admin_csrf(session))}">'
            '<input type="hidden" name="mode" value="apply">'
            '<p><button type="submit">Xác nhận nhập</button></p></form>'
        )
        return document("Xem trước CSV", body)

    @app.get("/admin/history")
    async def history(request: Request, task: str = "", op: str = "", status: str = ""):
        if not on_lan_host(request):
            return document("Không tìm thấy", "<h1>Không tìm thấy</h1>", 404)
        session = await authorized(request)
        if not session:
            return document("Không có quyền", "<h1>Không có quyền</h1>", 403)
        try:
            task_id = positive(task) if task else None
            op_id = positive(op) if op else None
            if status not in ("", "pending", "resolved", "sent", "failed", "cancelled"):
                raise ValueError("Trạng thái không hợp lệ.")
        except ValueError as error:
            return document("Bộ lọc", f"<h1>{esc(error)}</h1>", 400)
        csrf = store.admin_csrf(session)
        deliveries = store.dm_history(task_id, op_id,
                                      status if status in ("pending", "sent", "failed", "cancelled") else "")
        inbound = store.inbound_history(task_id,
                                        status if status in ("pending", "resolved", "failed") else "")
        dm_rows = "".join(
            '<tr>' + ''.join(f'<td>{esc(value)}</td>' for value in (
                row["work_package_id"] or "—",
                row["source_event_id"] or row["event_key"], row["kind"],
                ", ".join(json.loads(row["metadata"] or "{}").get("recipient_roles") or []),
                row["op_id"], row["discord_id"], row["status"], row["attempts"],
                row["error"] or "—", when(row["created_at"]),
                when(row["delivered_at"]) if row["delivered_at"] else "—",
            )) + '<td>' + (post_button("retry_job", "Thử lại", csrf, event_key=row["event_key"])
                          if row["status"] == "failed" else "") + '</td></tr>'
            for row in deliveries
        )
        inbound_rows = "".join(
            f'<tr><td>{esc(row["source_event_id"])}</td><td>{row["work_package_id"]}</td>'
            f'<td>{esc(row["action"])}</td><td>{esc(row["status"])}</td>'
            f'<td>{row["attempts"]}</td><td>{esc(row["error"] or "—")}</td>'
            f'<td>{when(row["received_at"])}</td><td>'
            f'{post_button("retry_event", "Phân giải lại", csrf, source=row["source_event_id"])
               if row["status"] == "failed" else ""}</td></tr>'
            for row in inbound
        )
        body = (
            '<p><a href="/admin">← Trang quản lý</a></p><h1>Lịch sử sự kiện và DM</h1>'
            '<form method="get" action="/admin/history"><div class="grid">'
            f'<label>Task ID<input name="task" value="{esc(task)}"></label>'
            f'<label>OpenProject ID<input name="op" value="{esc(op)}"></label>'
            f'<label>Trạng thái<input name="status" value="{esc(status)}"></label>'
            '</div><p><button type="submit">Lọc</button></p></form>'
            '<section class="card tablewrap"><h2>DM gần đây</h2><table><thead><tr>'
            '<th>Task</th><th>Sự kiện</th><th>Loại</th><th>Vai trò</th><th>OP ID</th><th>Discord ID</th>'
            '<th>Trạng thái</th><th>Lần thử</th><th>Lỗi</th><th>Tạo lúc</th><th>Gửi lúc</th><th></th>'
            f'</tr></thead><tbody>{dm_rows or "<tr><td colspan=12>Không có DM.</td></tr>"}'
            '</tbody></table></section>'
            '<section class="card tablewrap"><h2>Webhook gần đây</h2><table><thead><tr>'
            '<th>Sự kiện</th><th>Task</th><th>Loại</th><th>Trạng thái</th><th>Lần thử</th>'
            '<th>Lỗi</th><th>Nhận lúc</th><th></th></tr></thead><tbody>'
            f'{inbound_rows or "<tr><td colspan=8>Không có sự kiện.</td></tr>"}'
            '</tbody></table></section>'
        )
        return document("Lịch sử thông báo", body)

    @app.get("/admin/audit")
    async def audit(request: Request):
        if not on_lan_host(request):
            return document("Không tìm thấy", "<h1>Không tìm thấy</h1>", 404)
        if not await authorized(request):
            return document("Không có quyền", "<h1>Không có quyền</h1>", 403)
        rows = "".join(
            f'<tr><td>{when(item["at"])}</td><td>{esc(item["actor_discord_id"])}</td>'
            f'<td>{esc(item["action"])}</td><td>{esc(item["target"])}</td></tr>'
            for item in store.admin_audit_recent()
        )
        return document("Nhật ký quản trị", '<p><a href="/admin">← Trang quản lý</a></p>'
                        '<h1>Nhật ký quản trị</h1><table><thead><tr><th>Thời điểm</th>'
                        '<th>Discord quản lý</th><th>Thao tác</th><th>Đích</th>'
                        f'</tr></thead><tbody>{rows or "<tr><td colspan=4>Chưa có thao tác.</td></tr>"}'
                        '</tbody></table>')

    @app.get("/admin/rules")
    async def rules(request: Request):
        if not on_lan_host(request):
            return document("Không tìm thấy", "<h1>Không tìm thấy</h1>", 404)
        session = await authorized(request)
        if not session:
            return document("Không có quyền", "<h1>Không có quyền</h1>", 403)
        csrf = store.admin_csrf(session)
        rows = "".join(
            f'<tr><td>{item["project_id"]}</td><td>{esc(item["role"])}</td>'
            f'<td>{esc(item["event"])}</td><td>{"Bật" if item["enabled"] else "Tắt"}</td>'
            '</tr>' for item in store.recipient_rules()
        )
        body = (
            '<p><a href="/admin">← Trang quản lý</a></p><h1>Quy tắc người nhận</h1>'
            '<p>Quy tắc theo project và sự kiện. Cờ môi trường Watchers, Group, Accountable '
            'phải được bật trước; quy tắc không thể vượt giới hạn người nhận của server.</p>'
            '<form method="post" action="/admin/action"><div class="grid">'
            '<label>Project ID<input name="project_id" inputmode="numeric" required></label>'
            '<label>Vai trò<select name="role"><option value="watcher">Watcher</option>'
            '<option value="group">Group</option><option value="accountable">Accountable</option>'
            '</select></label><label>Sự kiện<select name="event">'
            '<option value="work_package:created">Tạo task</option>'
            '<option value="work_package:updated">Cập nhật task</option>'
            '<option value="work_package_comment:comment">Bình luận</option>'
            '</select></label></div><p><label><input type="checkbox" name="enabled" value="1">'
            'Bật vai trò cho sự kiện này</label></p>'
            f'<input type="hidden" name="csrf" value="{csrf}">'
            '<input type="hidden" name="action" value="rule">'
            '<button type="submit">Lưu quy tắc</button></form>'
            '<section class="card"><h2>Quy tắc đã lưu</h2><table><thead><tr>'
            '<th>Project</th><th>Vai trò</th><th>Sự kiện</th><th>Trạng thái</th>'
            f'</tr></thead><tbody>{rows or "<tr><td colspan=4>Chưa có ghi đè.</td></tr>"}'
            '</tbody></table></section>'
        )
        return document("Quy tắc người nhận", body)

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
            values = await read_request_form(request)
            if not hmac.compare_digest(values.get("csrf", ""), store.admin_csrf(session)):
                return document("Không có quyền", "<h1>Phiên biểu mẫu không hợp lệ.</h1>", 403)
            operation = values.get("action", "")
            if operation in ("link", "suspend", "resume", "unlink", "retry", "test"):
                op_id = positive(values.get("op_id", ""))
                target = str(op_id)
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
            elif operation == "test":
                if not store.dm_test(op_id):
                    raise ValueError("Liên kết chưa bật hoặc đã có DM thử đang chờ.")
                notice = "test_queued"
            elif operation == "retry_job":
                target = values.get("event_key", "")
                if not target or len(target) > 200 or not store.dm_retry_one(target):
                    raise ValueError("DM này không ở trạng thái lỗi hoặc liên kết đã thay đổi.")
                notice = "job_retried"
            elif operation == "retry_event":
                target = values.get("source", "")
                if not target or len(target) > 200 or not store.inbound_retry_one(target):
                    raise ValueError("Webhook này không ở trạng thái lỗi.")
                notice = "event_retried"
            elif operation == "rule":
                project_id = positive(values.get("project_id", ""))
                role, event = values.get("role"), values.get("event")
                if role not in ("watcher", "group", "accountable") or event not in (
                    "work_package:created", "work_package:updated",
                    "work_package_comment:comment",
                ):
                    raise ValueError("Vai trò hoặc sự kiện không hợp lệ.")
                if values.get("enabled", "0") not in ("0", "1"):
                    raise ValueError("Giá trị bật/tắt không hợp lệ.")
                enabled = values.get("enabled") == "1"
                config = getattr(api, "config", None)
                flag = getattr(config, {
                    "watcher": "watchers_enabled", "group": "groups_enabled",
                    "accountable": "accountable_enabled",
                }[role], False)
                if enabled and flag is not True:
                    raise ValueError("Cần bật cờ môi trường của vai trò trước.")
                allowlist = getattr(config, {
                    "watcher": "watcher_project_ids", "group": "group_project_ids",
                    "accountable": "group_project_ids",
                }[role], ())
                if enabled and role != "accountable" and allowlist and project_id not in allowlist:
                    raise ValueError("Project không nằm trong danh sách cho phép của vai trò.")
                store.set_recipient_rule(project_id, role, event, enabled)
                target = f"{project_id}:{role}:{event}:{int(enabled)}"
                notice = "rule_saved"
            else:
                raise ValueError("Thao tác không hợp lệ.")
            store.audit_admin(store.admin_session(session), operation, target)
        except DeliveryError as error:
            message = "Người dùng chưa ở trong server Discord." if error.code == "member_absent" else "Không kiểm tra được thành viên Discord."
            return document("Không thể lưu", f'<h1>Không thể lưu</h1><p>{message}</p><a href="/admin">Quay lại</a>', 400)
        except sqlite3.IntegrityError:
            return document("Không thể lưu", '<h1>Discord ID đã liên kết với OpenProject ID khác.</h1><a href="/admin">Quay lại</a>', 409)
        except (APIError, ProtocolError) as error:
            message = (f"OpenProject API HTTP {error.status}; kiểm tra quyền tài khoản dịch vụ."
                       if isinstance(error, APIError) else "OpenProject trả dữ liệu không hợp lệ.")
            return document("Không thể lưu", f'<h1>Không thể lưu</h1><p>{esc(message)}</p>'
                            '<a href="/admin">Quay lại</a>', 502)
        except (ValueError, UnicodeError) as error:
            return document("Không thể lưu", f'<h1>Không thể lưu</h1><p>{esc(error)}</p><a href="/admin">Quay lại</a>', 400)
        log.info("admin_action action=%s target=%s", operation, target)
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
            values = await read_request_form(request)
        except (ValueError, UnicodeError):
            return document("Biểu mẫu không hợp lệ", "<h1>Biểu mẫu không hợp lệ</h1>", 400)
        if not hmac.compare_digest(values.get("csrf", ""), store.admin_csrf(session)):
            return document("Không có quyền", "<h1>Phiên biểu mẫu không hợp lệ.</h1>", 403)
        store.audit_admin(store.admin_session(session), "logout", "admin")
        store.admin_logout(session)
        response = RedirectResponse("/admin", status_code=303)
        response.delete_cookie(COOKIE, path="/admin", secure=secure, httponly=True,
                               samesite="strict")
        return response
