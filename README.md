# OpenProject → Discord

Microservice độc lập để liên kết tài khoản OpenProject và gửi thông báo cá nhân qua Discord DM.
Không chỉnh sửa source code, plugin hoặc database của OpenProject.

**MVP đang chạy trên VPS và đã kết nối Discord Gateway.** Route HTTPS công khai và
`/readyz` trả 200, ứng dụng OAuth chấp nhận Client ID/secret, bot có lệnh `/op` trong
server QLDA. Một tài khoản đã liên kết qua OAuth thật và được polling thành công;
chưa có notification mới để xác minh DM thực tế.
API của instance trả 401 cho request không xác thực; chưa đối chiếu được spec của
phiên bản thực tế bằng tài khoản.

**Máy triển khai đã xác định:** OpenProject ở `192.168.1.12` (`/opt/openproject-dev`),
Caddy ở `192.168.1.51`, DNS AdGuard ở `192.168.1.50`. Bot có cấu hình
`compose.server.yaml` để dùng DNS AdGuard và cổng riêng `192.168.1.12:8081`.
Xem [trạng thái triển khai](docs/connection-status.md) và
[hướng dẫn Tunnel](docs/cloudflare-tunnel.md).

Tài liệu: [Kiến trúc và các giai đoạn](docs/architecture.md) ·
[API và nguồn xác minh](docs/api-contract.md).

[Kết quả kiểm tra](docs/verification.md): 32 tests đạt, lint và dependency check đạt,
image Docker build và smoke test offline đạt. Chưa kiểm tra luồng thật trên VPS.

[Trạng thái kết nối Discord hiện tại](docs/connection-status.md): bot đang chạy trong
server QLDA và route Tunnel hoạt động. Theo yêu cầu hiện tại, container dùng lại
token cũ từng xuất hiện trong cuộc trò chuyện; token này vẫn còn hiệu lực.

## Chức năng MVP

- `/op link`: bắt đầu OAuth qua liên kết riêng, không nhập API token vào Discord.
- `/op confirm code:...`: xác nhận tài khoản OpenProject vừa đăng nhập trên trình duyệt.
- `/op status`: xem liên kết, số DM đang chờ và lỗi gần nhất.
- `/op unlink`: dừng nhận DM, xóa liên kết/token/hàng đợi tại bot.
- Polling in-app notifications mặc định mỗi 60 giây/người, token riêng từng người.
- Bỏ qua lịch sử lúc liên kết; lưu ID để không gửi lại sau restart; retry có backoff.
- Refresh OAuth token, mã hóa token khi lưu; state/cookie/mã xác nhận dùng một lần.

Bot chuyển thông báo có **ID mới**, không phải mọi thay đổi của task hay mọi email.
Không đánh dấu đã đọc trên OpenProject. Một DM có thể bị lặp nếu tiến trình chết đúng lúc sau
khi Discord nhận nhưng trước khi DB ghi thành công. Chi tiết giới hạn ở tài liệu kiến trúc.

## Cấu trúc thư mục

```text
src/op_discord/
  config.py        # Kiểm tra cấu hình môi trường
  openproject.py   # OAuth + API v3, phân trang, kiểm tra URL
  store.py         # SQLite, token mã hóa, phiên liên kết và hàng đợi
  service.py       # Xác nhận liên kết, polling, retry và gửi DM
  web.py           # /link, /oauth/callback, /healthz, /readyz
  bot.py           # Slash commands và Discord DM
  main.py          # Lifecycle, single-process lock
  check.py         # Probe tương thích chỉ đọc
tests/             # API mock + SQLite thật + HTTP OAuth flow
docs/              # Kiến trúc, kế hoạch và hợp đồng API
deploy/            # Reverse proxy mẫu
Dockerfile
compose.yaml
compose.tunnel.yaml # Dùng chung network với cloudflared Docker đang có
compose.server.yaml # DNS AdGuard cho bot trên máy OpenProject hiện tại
.env.example
requirements.lock  # Phiên bản runtime đã khóa
```

## 1. Chuẩn bị DNS và HTTPS

Hostname bot hiện dùng `https://oponproject-bot.lazy247.space`. Route Cloudflare Tunnel
đã xác minh bằng HTTP server tạm trên cổng `192.168.1.12:8081`; đặt hostname này vào
`PUBLIC_URL`. OpenProject giữ nguyên `https://openproject.lazy247.space`.

Với Cloudflare Tunnel, dùng hướng dẫn chuyên biệt ở trên. Nếu dùng reverse proxy thông thường,
route hostname bot tới `127.0.0.1:8080`. Có mẫu
[Caddyfile](deploy/Caddyfile.example) dành cho Caddy chạy trực tiếp trên host. Nếu proxy chạy
trong container, nối proxy và bridge vào Docker network chung rồi dùng `bridge:8080`, không
dùng localhost của container proxy. Chỉ publish qua HTTPS. Không bật access log chứa query
string OAuth, và không đặt trang đăng nhập proxy phía trước callback.

## 2. Tạo OAuth application trong OpenProject

Trong Administration → Authentication → OAuth applications, tạo ứng dụng:

- Name: `Discord notification bridge`.
- Redirect URI: **chính xác** `https://<hostname-bot>/oauth/callback`.
- Confidential: bật.
- Scope: `api_v3`.
- Client Credentials User: để trống; mỗi người cấp quyền bằng tài khoản riêng.

Lưu Client ID và Client secret vào `.env`, không gửi qua chat hoặc đưa vào Git.
Trên server đã triển khai, nếu cần cập nhật credential, chạy
`python3 deploy/configure_secrets.py` ngay trong phiên SSH. Script hỏi ba giá trị
qua prompt ẩn và ghi `.env` với quyền `600`; nhấn Enter để giữ giá trị hiện tại.
Không dán secret vào chat. Kiểm tra credential trong container trước khi start/restart:

```bash
docker run --rm --user "$(id -u):$(id -g)" --dns 192.168.1.50 \
  --mount type=bind,source=/opt/op-discord-bridge,target=/project,readonly \
  --entrypoint python op-discord-bridge-bridge:latest \
  /project/deploy/verify_credentials.py
```

Hai dòng kiểm tra phải đều báo `OK`; script không in secret.
Tài liệu [OpenProject OAuth applications](https://www.openproject.org/docs/system-admin-guide/authentication/oauth-applications/).
Không cần bật CORS vì request API chạy server-to-server. `api_v3` là scope API tổng quát;
implementation chỉ gọi GET cho dữ liệu OpenProject, không có scope chỉ đọc riêng được giả định.

## 3. Tạo Discord application và bot

Trong [Discord Developer Portal](https://discord.com/developers/applications):

1. Tạo application/bot, lấy Bot Token vào `DISCORD_BOT_TOKEN`.
2. Cài bot vào server thử nghiệm với scopes `bot` và `applications.commands`.
3. Không cấp Administrator; không cần privileged intents hoặc Message Content Intent.
4. Đặt `DISCORD_GUILD_ID` thành ID server thử nghiệm để slash commands đăng ký nhanh.
   Để trống khi muốn đăng ký global. Khi chuyển từ guild sang global, xóa command guild cũ
   trong cấu hình tích hợp Discord nếu còn hiển thị trùng; MVP không tự xóa command cũ.
5. Người dùng cần cho phép bot gửi DM, thường qua thiết lập nhận DM từ thành viên cùng server.

Không cần điền Discord Interactions Endpoint URL vì bot dùng Gateway. HTTP callback ở đây
là callback OAuth của OpenProject, không phải callback Discord.

## 4. Điền cấu hình và chạy

Trên VPS Linux có Docker Compose v2:

```bash
cp .env.example .env
chmod 600 .env
docker compose build
docker compose run --rm --no-deps bridge python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Trên máy hiện tại, dùng `docker compose -f compose.yaml -f compose.server.yaml ...`, đặt
`BRIDGE_BIND_ADDRESS=192.168.1.12` và `BRIDGE_PUBLISHED_PORT=8081` trong `.env`.
Trong Cloudflare Zero Trust, thêm published application route cho hostname bot tới
`http://192.168.1.12:8081` trên tunnel đang phục vụ OpenProject. Máy Caddy hiện không
chạy `cloudflared`, nên không cần sửa Caddyfile cho route này.

Lưu khóa vừa sinh vào `TOKEN_ENCRYPTION_KEY` trong `.env`. Điền `PUBLIC_URL`, Client ID,
Client secret, Discord token và Guild ID. Không đổi khóa khi vẫn còn DB cũ; backup khóa riêng.
Khóa sinh ở lệnh trên chỉ hiển thị cho quản trị trên terminal, không được commit.

Kiểm tra API trên chính môi trường triển khai trước khi start bot:

```bash
docker compose run --rm --no-deps bridge op-discord-check
```

Probe yêu cầu nhập **personal API token của tài khoản thử nghiệm** ở prompt ẩn; không lưu token
này. Đây chỉ là công cụ chẩn đoán; luồng dùng bot bình thường dùng OAuth. Nếu API docs bị tắt,
bật API documentation trong Administration → API and webhooks để kiểm tra specification.
Nếu trả 403/HTML, kiểm tra reverse proxy/WAF và quyền API; không tự suy ra endpoint khác.

Sau khi probe đạt và cấu hình HTTPS xong:

```bash
docker compose up -d
docker compose ps
docker compose logs --tail=100 bridge
curl -fsS http://127.0.0.1:8080/readyz
```

Compose chạy **một replica**, non-root, filesystem chỉ đọc trừ volume dữ liệu.
Không dùng `docker compose down -v` nếu cần giữ liên kết/hàng đợi.

## 5. Kiểm tra staging thật

1. Người A chạy `/op link`, đăng nhập OpenProject A, kiểm tra tên/ID trên trang callback và
   chạy `/op confirm code:...` bằng Discord A. `/op status` phải báo `active`.
2. Làm tương tự cho người B với tài khoản riêng. Kiểm tra mỗi người chỉ nhận dữ liệu của mình.
3. Dùng người B tạo hoạt động tạo được in-app notification cho A theo cấu hình OpenProject
   của A, ví dụ mention trong work package. Xác nhận notification xuất hiện trong OpenProject
   trước; chờ khoảng một chu kỳ polling rồi kiểm tra Discord A. Không kỳ vọng self-action tạo tin.
4. Restart bridge; thông báo đã gửi không được gửi lại. Tạo notification mới và kiểm tra DM mới.
5. Chặn DM; queue phải giữ lại và `/op status` hiện `discord_dm_blocked`. Mở lại và chờ retry.
6. Chạy qua một chu kỳ hết hạn OAuth (mặc định tài liệu là khoảng 2 giờ), xác nhận bot vẫn gửi
   được nhờ refresh. Không chỉnh DB production để ép hết hạn.
7. Thu hồi quyền OAuth trong OpenProject; bot phải dừng tài khoản và hiện `needs_attention`.
   Sửa quyền rồi `/op unlink`, `/op link` lại. Liên kết lại bắt đầu baseline mới.
8. `/op unlink`; xác nhận không có DM mới sau khi lệnh hoàn tất.

Các bước này cần tài khoản thật; bộ test mock không thay thế chúng.

## Phát triển và kiểm thử

PowerShell tại workspace hiện tại:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.lock
.\.venv\Scripts\python -m pip install -e '.[dev]'
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m ruff check src tests
.\.venv\Scripts\op-discord.exe
```

Nếu môi trường hạn chế quyền thư mục temp, chỉ định `--basetemp` tới **thư mục test riêng**
trong workspace; pytest có thể xóa nội dung thư mục đó. Không dùng thư mục chứa source/dữ liệu thật.
Lệnh start cần `.env` hợp lệ và HTTPS callback dù phát triển cục bộ. Các test chạy offline,
không gửi tin hoặc gọi OpenProject/Discord thật.

## Vận hành và backup

- Giám sát `/readyz`, trạng thái Docker, log `account_poll_failed` và dung lượng volume.
  Docker healthcheck chỉ đánh dấu unhealthy; nó không tự restart container chỉ vì unhealthy.
- `/op status` hiển thị lỗi theo người dùng; không đưa token/nội dung thông báo vào log.
- Backup: dừng bridge, sao lưu toàn bộ volume `bridge-data` (gồm DB/WAL nếu còn), lưu secret
  mã hóa riêng và hạn chế quyền đọc, sau đó start bridge. Restore đúng DB với đúng khóa.
- Thử khôi phục trên môi trường cô lập, tránh chạy song song hai bot cùng tài khoản.
- Mất khóa mã hóa thì cần liên kết lại; không có cơ chế khôi phục token từ DB.
- Thay đổi khóa chưa được hỗ trợ trực tuyến. Với MVP, unlink/relink có kế hoạch hoặc bổ sung
  công cụ rotate có backup trước khi thay khóa.
- Chỉ giữ một instance; ledger ID tăng theo số notification quan sát đến khi người dùng unlink.

Các biến nâng cao: `POLL_INTERVAL_SECONDS` (>=15), `MAX_NOTIFICATION_PAGES`,
`DELIVERY_BATCH_SIZE`. Đặt giới hạn dựa trên tải OpenProject và số người dùng thực tế.
