# OpenProject → private Discord channels

Microservice Python chuyển in-app notification của từng tài khoản OpenProject vào
**text channel riêng của người đó trong một Discord server**. Bot không gửi DM.
Người dùng thường chỉ thấy channel của mình; chủ server và thành viên có quyền
`Administrator` vẫn xem được mọi channel. Bot không sửa source hoặc database của
OpenProject.

**Trạng thái:** mã nâng cấp và test offline đã có trong repository. Phiên bản
private channel chưa được triển khai hoặc xác minh với Discord/OpenProject thật.
[Trạng thái VPS trước nâng cấp](docs/connection-status.md) là ảnh chụp của bản gửi DM cũ.

## Luồng hoạt động

1. Quản lý chạy công cụ `op-discord-admin` trên máy chủ, cung cấp Discord ID và
   personal API token của đúng tài khoản OpenProject tương ứng qua prompt ẩn.
2. Công cụ kiểm tra người đó thuộc server, xác thực token bằng `/api/v3/users/me`,
   lấy ID notification hiện tại làm baseline, tạo private channel với quyền riêng,
   rồi lưu liên kết cùng token đã mã hóa trong SQLite.
3. Worker quét notification mới theo từng tài khoản, kiểm tra lại quyền đọc bằng
   credential của tài khoản đó, kiểm tra quyền channel trước khi gửi và đăng embed
   vào channel đã lưu. ID đã gửi được ghi vào SQLite để không gửi lại sau restart.
4. `/op status` là lệnh duy nhất dành cho người dùng, chỉ hiện trong server đã cấu
   hình và trả lời riêng cho người gọi. Liên kết/gỡ liên kết do quản lý thao tác.

Bot chỉ chuyển **notification có ID mới**. Không chuyển mọi lần chỉnh sửa task hay
mọi email, và không đánh dấu notification đã đọc trên OpenProject. Nếu tiến trình
dừng ngay sau khi Discord nhận tin nhưng trước khi SQLite ghi `sent`, một tin có thể
bị gửi lặp.

## Chuẩn bị Discord và OpenProject

- Tạo một **category** trong server, ví dụ `OPENPROJECT`, và lấy ID cho
  `DISCORD_CATEGORY_ID`. Đặt `DISCORD_GUILD_ID` là ID của server.
- Bot cần `Manage Channels`, `Manage Roles` (để gỡ quyền khi unlink),
  `View Channel`, `Send Messages`, `Read Message History` và `Embed Links`.
  Không cấp `Administrator` cho bot. Bot tạo từng channel với
  `@everyone` bị từ chối xem, chỉ người nhận và bot được cấp quyền trực tiếp.
- Quản lý cần personal API token **riêng của từng tài khoản OpenProject** theo cách
  được chủ tài khoản cho phép. Discord ID và OpenProject ID không đủ để đọc
  notification cá nhân. Không nhập token vào Discord, URL hoặc tham số dòng lệnh.
- Mã và token OAuth của tài khoản đã liên kết ở bản cũ được giữ cho migration. Nếu
  còn dùng tài khoản OAuth cũ, giữ `OPENPROJECT_CLIENT_ID` và
  `OPENPROJECT_CLIENT_SECRET` để refresh token. Luồng tạo liên kết OAuth mới qua
  `/link` và `/oauth/callback` đã tắt.

Quyền `Administrator` của Discord vượt qua permission overwrites. Category chứa
tối đa 50 channel; với hơn 50 người cần thêm category và mở rộng cấu hình.

## Cấu hình và chạy

Python 3.12 hoặc Docker Compose v2. Sao chép `.env.example` thành `.env`, điền
`OPENPROJECT_URL`, `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`,
`DISCORD_CATEGORY_ID` và `TOKEN_ENCRYPTION_KEY`. Giữ khóa Fernet cũ nếu dùng DB
đã có; thay khóa sẽ làm các token đã lưu không giải mã được. Tạo khóa mới cho DB
mới bằng:

```powershell
py -3.12 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Trên VPS đã dùng `compose.server.yaml`, chạy bản mới sau khi sao lưu volume DB và
khóa Fernet:

```bash
docker compose -f compose.yaml -f compose.server.yaml build
docker compose -f compose.yaml -f compose.server.yaml up -d
docker compose -f compose.yaml -f compose.server.yaml ps
```

`/healthz` kiểm tra HTTP còn chạy; `/readyz` chỉ trả 200 khi Discord và worker
sẵn sàng. Mặc định bot poll mỗi 60 giây mỗi tài khoản, tối đa 20 notification mỗi
lượt; các tham số nằm trong `.env.example`. Chỉ chạy một replica với một volume
SQLite. Không dùng `docker compose down -v` nếu cần giữ liên kết và hàng đợi.

## Quản lý liên kết

`op-discord-admin` là công cụ ngoại tuyến: **dừng bridge trước mỗi thao tác** rồi
chạy công cụ với cùng `.env` và volume dữ liệu. Ví dụ trên VPS:

```bash
docker compose -f compose.yaml -f compose.server.yaml stop bridge
docker compose -f compose.yaml -f compose.server.yaml run --rm --no-deps bridge op-discord-admin link --discord-id 123456789012345678
docker compose -f compose.yaml -f compose.server.yaml up -d bridge
```

Lệnh `link` hỏi OpenProject API token qua prompt ẩn. Nó kiểm tra account và
baseline trước khi tạo channel. Nếu tạo channel thành công nhưng DB không ghi
được, công cụ in channel ID để quản lý kiểm tra và dọn thủ công; không tự gán
channel đó cho tài khoản khác.

Các lệnh khác, đều chạy khi bridge đã dừng:

| Lệnh | Tác dụng |
|---|---|
| `list` | Liệt kê liên kết, trạng thái, channel và số notification chờ; không in token. |
| `migrate --discord-id ID` | Tạo channel và kích hoạt tài khoản OAuth cũ đang `migration_pending`. |
| `audit-channels` | Kiểm tra thành viên còn trong server và quyền của mọi channel đã gán. |
| `suspend --discord-id ID` | Tạm dừng tài khoản, giữ token và hàng đợi. |
| `resume --discord-id ID` | Kiểm tra lại thành viên/channel rồi kích hoạt. |
| `unlink --discord-id ID` | Gỡ quyền xem channel của người dùng, xóa liên kết/token/queue; giữ channel và lịch sử. |

Khi nâng cấp DB cũ, các account đang `active` nhưng chưa có `channel_id` sẽ thành
`migration_pending`. Worker mới không gửi notification cho chúng và **không quay
lại DM**. Quản lý dùng `migrate` cho từng người; ledger và các job đang chờ được
giữ. Nếu channel bị xóa, quyền xem bị mở rộng hoặc người dùng rời server, bot
giữ job và chuyển tài khoản sang `needs_attention`, không gửi sang nơi khác.

## Phát triển và kiểm thử

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e '.[dev]'
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m ruff check src tests
```

Test dùng HTTP mock, SQLite thật và không gửi tin ra Discord. Hãy kiểm tra thực
với hai tài khoản Discord/OpenProject trước khi dùng production: mỗi người chỉ
thấy channel của mình, notification mới đến đúng channel, không có DM, restart
không gửi lại tin cũ, và bot dừng khi quyền channel bị sửa.

[Kế hoạch và điều kiện nghiệm thu chi tiết](docs/private-channel-plan.md) ·
[Hợp đồng API OpenProject](docs/api-contract.md).
