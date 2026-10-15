# OpenProject → Discord DM

Bot nhận webhook OpenProject và gửi DM cho người được giao công việc. Người tham gia chỉ cần có mặt trong Discord server; quản lý ghép **OpenProject user ID ↔ Discord user ID** một lần. Không cần token hay thao tác liên kết từ người tham gia.

## Thông báo

| Sự kiện OpenProject | Người nhận | Nội dung |
|---|---|---|
| Tạo công việc có người được giao, hoặc đổi người được giao | Người được giao mới | Tên công việc, dự án, người thực hiện và liên kết |
| Đổi trạng thái công việc | Người đang được giao | Trạng thái mới và liên kết |
| Bình luận công khai mới | Người đang được giao | Tối đa 300 ký tự đầu và liên kết |

Bot bỏ qua bình luận nội bộ. Một webhook cập nhật có thể tạo cả thông báo giao việc lẫn đổi trạng thái. `/op status` trong Discord server trả lời riêng trạng thái liên kết và số DM đang chờ. DM được gửi từ bot vào hộp thư Discord; không tạo channel cho mỗi người. DM dùng thẻ thông tin có tiêu đề, trạng thái, ưu tiên, hạn, dự án, người được giao, người báo cáo và nút mở công việc. Thuộc tính nào OpenProject không cung cấp sẽ hiện dấu gạch ngang. Khi quản lý liên kết người dùng mới, bot gửi một DM chào mừng.

OpenProject chỉ phát webhook công việc sau khi hết thời gian **gộp hoạt động**. Mặc định thời gian này là 5 phút; có thể chỉnh tại **Administration → Emails and notifications → Aggregation**. Trên server hiện tại đặt 1 phút để thông báo đến nhanh hơn. Thiết lập này cũng thay đổi cách OpenProject gộp email và thông báo trong ứng dụng; các job đã hẹn trước khi đổi thiết lập vẫn theo thời điểm cũ.

OpenProject gửi ảnh chụp công việc ở thời điểm xử lý webhook, không gửi danh sách thuộc tính vừa thay đổi. Vì vậy lần cập nhật đầu tiên của **công việc cũ chưa có snapshot** chỉ lưu trạng thái làm mốc, không suy đoán sai rằng vừa có giao việc/đổi trạng thái. Các lần sau được so sánh với mốc. Công việc mới có thể gửi giao việc ngay. Bình luận dùng thông tin công việc nhúng trong chính webhook.

## Cấu hình

Sao chép `.env.example` thành `.env`, giữ khóa `TOKEN_ENCRYPTION_KEY` cũ nếu dùng database hiện hữu, rồi đặt:

```dotenv
OPENPROJECT_URL=https://openproject.example.com
DISCORD_BOT_TOKEN=...
DISCORD_GUILD_ID=...
DELIVERY_MODE=webhook_dm
OPENPROJECT_WEBHOOK_SECRET=... # ít nhất 32 ký tự ngẫu nhiên
BRIDGE_BIND_ADDRESS=192.168.1.12
BRIDGE_PUBLISHED_PORT=8081
ADMIN_URL=http://192.168.1.12:8081
```

`DISCORD_CATEGORY_ID` không cần cho DM. Secret webhook phải giống hệt secret cấu hình trong OpenProject. Tạo secret bằng `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Giữ kín `.env`; không đưa secret vào URL hoặc Git.

Trong **OpenProject → Administration → API and webhooks → + Webhook**, đặt Payload URL là `http://192.168.1.12:8081/webhooks/openproject`, nhập cùng Signature secret, bật webhook, chọn các sự kiện **Work package created**, **Work package updated**, **Work package comment** (bình luận công khai), và chọn dự án cần áp dụng. Nếu OpenProject chặn đích mạng nội bộ, thêm IP bridge vào cấu hình `OPENPROJECT_SSRF__PROTECTION__IP__ALLOWLIST` của dịch vụ OpenProject gửi webhook rồi khởi động lại dịch vụ đó. Có thể dùng một URL HTTPS nội bộ thay cho HTTP LAN nếu hạ tầng đã có sẵn.

OpenProject ký body bằng header `X-OP-Signature: sha1=...`; bridge kiểm tra chữ ký trước khi phân tích nội dung, giới hạn body 1 MiB và trả `202` sau khi lưu hàng đợi SQLite. Chỉ chạy **một** replica bridge với một volume SQLite.

## Chạy bằng Docker Compose

```bash
docker compose -f compose.yaml -f compose.server.yaml build
docker compose -f compose.yaml -f compose.server.yaml up -d
docker compose -f compose.yaml -f compose.server.yaml ps
curl -f http://127.0.0.1:8081/readyz
```

Trên VPS đang dùng `192.168.1.12:8081`, thay địa chỉ `curl` cho đúng listener. `/healthz` kiểm tra HTTP, `/readyz` chỉ thành công khi bot Discord và worker sẵn sàng. `DELIVERY_MODE=webhook_dm` tắt hẳn worker polling/channel cũ. Không dùng `docker compose down -v` vì sẽ xóa database.

## Trang quản lý liên kết trong mạng nội bộ

Người quản lý cần quyền **Manage Server** trong Discord server. Từ máy tính nằm trong mạng nội bộ, chạy `/op admin` trong server; bot trả lời riêng một liên kết đăng nhập có hiệu lực 5 phút và chỉ dùng một lần. Mở liên kết đó trên cùng máy nội bộ để vào trang quản lý. Phiên đăng nhập hết hạn sau 1 giờ; quyền Manage Server được kiểm tra lại khi mở trang và gửi mỗi biểu mẫu.

Trong trang quản lý, nhập **OpenProject user ID** và **Discord user ID**, thêm tên để dễ nhận biết rồi bấm **Lưu liên kết**. Bot xác nhận Discord ID đang là thành viên server. Liên kết mới được xếp hàng gửi DM chào mừng; người dùng không cần thao tác gì. Trang cũng cho phép xem số DM chờ, tạm dừng/bật lại, gỡ liên kết và đưa DM lỗi trở lại hàng đợi. Nếu người dùng chặn DM, hãy xử lý cài đặt Discord rồi bấm **Gửi lại lỗi**. Cần xác nhận OpenProject user ID đúng với người đó trước khi lưu vì trang không có quyền đọc danh sách người dùng OpenProject.

`ADMIN_URL` phải là URL nội bộ trỏ đến listener của bridge; URL này cũng được dùng để giới hạn Host được phép truy cập `/admin`. Nếu cần cho quản lý truy cập ngoài LAN, hãy dùng VPN vào LAN. Không thêm đường dẫn `/admin` vào tunnel hoặc reverse proxy công khai. Nếu bot đã chạy trước khi thêm biến này, hãy build và khởi động lại bridge để lệnh `/op admin` xuất hiện.

## Lệnh quản trị dự phòng

Các lệnh `op-discord-admin` dùng cùng `.env` và volume database. Dừng bridge trước khi chạy vì CLI dùng khóa độc quyền:

```bash
docker compose -f compose.yaml -f compose.server.yaml stop bridge
docker compose -f compose.yaml -f compose.server.yaml run --rm --no-deps bridge op-discord-admin dm-link --op-id 12 --discord-id 123456789012345678 --name "Tên người dùng"
docker compose -f compose.yaml -f compose.server.yaml run --rm --no-deps bridge op-discord-admin dm-list
docker compose -f compose.yaml -f compose.server.yaml up -d bridge
```

`dm-link` kiểm tra Discord ID thuộc server. Các lệnh còn lại: `dm-suspend --op-id ID`, `dm-resume --op-id ID`, `dm-unlink --op-id ID`, `dm-errors`, `dm-retry --op-id ID`. Liên kết đang hoạt động từ bản channel cũ được nhập tự động **một lần** khi database nâng lên phiên bản mới. Nên chạy `dm-list` sau nâng cấp. Nếu người dùng chặn DM từ bot hoặc rời server, công việc gửi tương ứng được đánh dấu lỗi; sau khi xử lý nguyên nhân, quản lý dùng `dm-retry` để gửi lại.

## Kiểm tra và giới hạn

Chỉ định tuyến theo OpenProject **user ID**, không theo tên. Bot kiểm tra thành viên vẫn ở Discord server trước mỗi DM. Hàng đợi lưu loại sự kiện, tiêu đề, dự án và tối đa 300 ký tự bình luận; không lưu toàn bộ body webhook. Cùng một sự kiện được nhận lại sẽ không thêm công việc gửi trùng. Nếu Discord đã nhận DM nhưng tiến trình dừng trước khi SQLite ghi `sent`, một DM có thể được gửi lại sau khi khởi động.

Với công việc cũ, nhập snapshot hiện tại từ OpenProject trước khi bật webhook để nhận ngay lần thay đổi đầu tiên. Trên máy chủ cùng Docker với OpenProject, khi bridge đã dừng, có thể xuất `id,assigned_to_id,status_id,lock_version` từ bảng `work_packages` bằng PostgreSQL rồi truyền CSV không header qua stdin vào `op-discord-admin dm-seed` (dùng `docker compose run -T`). Lệnh chỉ thêm công việc chưa có snapshot, không ghi đè mốc mới hơn. Nếu không nhập, lần cập nhật đầu tiên của mỗi công việc cũ sẽ tạo mốc như mô tả trên. Không gửi DM thử đến người dùng khi chạy test tự động.

Mã channel/polling cũ vẫn có thể dùng bằng `DELIVERY_MODE=channel`; trong chế độ đó cần `DISCORD_CATEGORY_ID` và luồng quản lý channel/token cũ. Chế độ DM không dùng personal API token của người tham gia.
