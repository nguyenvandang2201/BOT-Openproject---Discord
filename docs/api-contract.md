# Hợp đồng API đã xác minh

Đối chiếu ngày 2026-10-15. Tài liệu stable hiện tại không thay thế kiểm tra đúng phiên bản
đang self-host. Request không xác thực từ môi trường phát triển tới API root và
`https://openproject.lazy247.space/api/v3/spec.json` từng nhận **HTTP 403**; chưa xác định đó là
OpenProject hay reverse proxy/WAF. Kiểm tra lại ngày 2026-10-15 sau khi NetBird kết nối:
`GET /api/v3` và `GET /api/v3/spec.json` không xác thực đều trả **HTTP 401** dạng HAL JSON.
Chưa dùng token để đối chiếu spec của instance. Luồng liên kết thật đã gọi thành công
`/api/v3/users/me`; một tài khoản đang `active` và polling `/api/v3/notifications`
thành công. Chưa có notification mới để kiểm tra `GET /api/v3/notifications/{id}` và DM.
Không kết luận host thiếu endpoint hoặc phiên bản nào.

| Request | Mục đích | Nguồn chính thức |
|---|---|---|
| `GET /oauth/authorize` | Authorization Code, scope `api_v3`, state | [OAuth applications](https://www.openproject.org/docs/system-admin-guide/authentication/oauth-applications/) |
| `POST /oauth/token` | Đổi code hoặc refresh token; form URL encoded | [OAuth applications](https://www.openproject.org/docs/system-admin-guide/authentication/oauth-applications/), [Doorkeeper configuration của OpenProject](https://github.com/opf/openproject/blob/dev/config/initializers/doorkeeper.rb) |
| `GET /api/v3/users/me` | ID user cấp quyền; `me` là giá trị đặc biệt của `{id}` | [Users API](https://www.openproject.org/docs/api/endpoints/users/) |
| `GET /api/v3/notifications` | In-app notification, pageSize, sortBy ID | [Notifications API](https://www.openproject.org/docs/api/endpoints/notifications/) |
| `GET /api/v3/notifications/{id}` | Kiểm tra lại quyền và đọc nội dung trước gửi | [Notifications API](https://www.openproject.org/docs/api/endpoints/notifications/) |
| `GET /api/v3/spec.json` | Probe kiểm tra hợp đồng trên instance, không dùng trong polling | [API introduction](https://www.openproject.org/docs/api/introduction/) |

Collection dùng `_embedded.elements` và `_links.nextByOffset.href` theo
[Collections API](https://www.openproject.org/docs/api/collections/). Không dùng endpoint kiểu
`/users/{id}/notifications`, không tự thêm webhook notification hoặc endpoint subscribe.

MVP chỉ chuyển **in-app notification có ID mới**, tùy cấu hình thông báo của từng người trên
OpenProject. Không hứa chuyển mọi email hoặc mọi lần sửa task. Không đọc tài khoản người khác
bằng token admin. Không giả định một webhook work package xác định được người nhận cá nhân.

Luồng OAuth dùng confidential client với client secret phía server, state và cookie;
không yêu cầu PKCE trong MVP. Refresh token dùng grant chuẩn `refresh_token` trên cùng token
endpoint. Cấu hình Doorkeeper chính thức bật refresh token; khả năng/expiry thực tế cần xác nhận
trong staging ở phiên bản đang chạy. Nếu token không refresh được, bot dừng tài khoản và yêu cầu
liên kết lại, không chuyển sang tài khoản chung.

Lệnh `op-discord-check` chỉ đọc specification và hai endpoint theo token API nhập ẩn; không ghi
dữ liệu OpenProject hoặc gửi Discord. Probe chưa kiểm chứng OAuth/DM: phải chạy luồng staging
thật ở README. Token probe dùng Basic `apikey:<API token>` để tương thích cả bản cũ.

Discord dùng Gateway và API được thư viện chính thức dự án discord.py hỗ trợ:
[Client](https://discordpy.readthedocs.io/en/stable/api.html),
[Interactions](https://discordpy.readthedocs.io/en/stable/interactions/api.html).
