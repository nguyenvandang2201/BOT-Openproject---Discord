# Trạng thái triển khai — 2026-10-15

> Đây là ảnh chụp trạng thái của phiên bản gửi DM trước khi nâng cấp. Mã private
> channel hiện trong repository chưa được xác minh hoặc triển khai trên VPS.

Microservice đang chạy trên máy OpenProject `192.168.1.12` trong
`/opt/op-discord-bridge`, tách khỏi stack OpenProject tại `/opt/openproject-dev`.
Bot dùng Docker Compose với `compose.yaml` và `compose.server.yaml`, bind
`192.168.1.12:8081`, DNS AdGuard `192.168.1.50`, volume SQLite riêng.
Không sửa source code hoặc database OpenProject. Máy Caddy `192.168.1.51`
không tham gia route bot.

| Kiểm tra | Kết quả |
|---|---|
| Cloudflare Tunnel | Hostname `https://oponproject-bot.lazy247.space` tới `192.168.1.12:8081` hoạt động |
| OpenProject OAuth | App `Discord notification bridge`, confidential, scope `api_v3`, redirect `https://oponproject-bot.lazy247.space/oauth/callback` |
| Client ID/secret | `POST /oauth/token` với mã thử sai trả `invalid_grant`, chứng tỏ xác thực client được chấp nhận; không in credential |
| Discord bot | `GET /users/@me` trả 200 với app ID `1560096546375340163`; bot kết nối Gateway |
| Server Discord | Guild **QLDA** `1542104786780819476`; guild command `/op` có `link`, `confirm`, `status`, `unlink` |
| Container | `op-discord-bridge-bridge-1` đang `healthy` |
| Readiness | `http://192.168.1.12:8081/readyz` và HTTPS công khai `/readyz` đều trả 200 |
| Liên kết tài khoản | Đã có 1 tài khoản `active` qua OAuth thật; polling tiếp tục thành công, chưa có lỗi tài khoản |
| DM thông báo | Chưa có notification mới trong ledger nên chưa xác minh gửi DM |

Credential OpenProject được người dùng cập nhật trực tiếp trên VPS và đã kiểm tra
thành công; không đưa secret vào tài liệu. Theo yêu cầu của người dùng, container
đã chuyển sang **token Discord cũ** và kết nối Gateway thành công. Token này
từng bị dán vào cuộc trò chuyện và vẫn hoạt động; nếu muốn thu hồi bản đã lộ,
dùng **Reset Token** trong Discord Developer Portal, cập nhật
`/opt/op-discord-bridge/.env`, restart container và xác nhận token cũ trả 401.
Nên đổi cả mật khẩu SSH đã từng chia sẻ.

Liên kết OAuth đã được xác nhận bởi một tài khoản người dùng thực. Bước còn lại:
tạo một in-app notification mới cho tài khoản đó từ tài khoản khác, xác nhận
notification xuất hiện trong OpenProject và DM đến đúng người trong khoảng một
chu kỳ polling. Không gửi credential hoặc nội dung riêng tư vào chat.

Kiểm tra vận hành:

```bash
cd /opt/op-discord-bridge
docker compose -f compose.yaml -f compose.server.yaml ps
docker compose -f compose.yaml -f compose.server.yaml logs --tail=100 --no-color bridge
curl -fsS http://192.168.1.12:8081/readyz
```

Tài liệu API: [OpenProject OAuth applications](https://www.openproject.org/docs/system-admin-guide/authentication/oauth-applications/),
[Discord API](https://discord.com/developers/docs/intro).
