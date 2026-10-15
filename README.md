# OpenProject → Discord DM

Kế hoạch bổ sung Watchers, Group, trang quản lý và các giai đoạn nâng cấp: [docs/ke-hoach-nang-cap-dm.md](docs/ke-hoach-nang-cap-dm.md).

Bot nhận webhook OpenProject và gửi DM cho người được giao công việc. Người tham gia chỉ cần có mặt trong Discord server; quản lý ghép **OpenProject user ID ↔ Discord user ID** một lần. Không cần token hay thao tác liên kết từ người tham gia.

## Tiến độ nâng cấp nhiều người nhận

Giai đoạn A–E đã thêm schema SQLite v7, hàng đợi webhook bền vững, khóa chống trùng theo sự kiện và OpenProject user ID, cơ chế phân giải/retry riêng, thẻ DM gộp thay đổi, gửi DM cho Watchers và thành viên Group được giao đã liên kết. `MULTI_RECIPIENT_ENABLED=false`, `WATCHERS_ENABLED=false`, `GROUPS_ENABLED=false` và `ACCOUNTABLE_ENABLED=false` là mặc định. Khi bật thử nghiệm, bot tạo tối đa một DM cho mỗi người trong một sự kiện dù họ thuộc nhiều vai trò. `queued` trong phản hồi webhook là số **sự kiện** đã lưu, không phải số DM.

Để thử Watchers, tài khoản dịch vụ OpenProject cần quyền **View work packages** và **View work package watchers** trên project liên quan ([tài liệu API](https://www.openproject.org/docs/api/endpoints/work-packages/)). Đặt `OPENPROJECT_SERVICE_API_KEY` của tài khoản đó, `MULTI_RECIPIENT_ENABLED=true`, `WATCHERS_ENABLED=true` và tùy chọn `WATCHER_PROJECT_IDS` bằng ID project thử nghiệm. Người tham gia vẫn chỉ cần liên kết OpenProject ID ↔ Discord ID; không cần token riêng. Người là assignee đồng thời là watcher chỉ nhận một DM cho mỗi sự kiện.

Bot giữ snapshot Watchers khi xử lý webhook. Khi bật Watchers, bot mặc định đối soát mỗi task sau 300 giây để bắt cả trường hợp thêm Watcher không phát webhook hoặc không đổi `lockVersion`; đặt `WATCHER_RECONCILE_SECONDS=0` nếu đã xác minh không cần đối soát. Lần quan sát đầu chỉ tạo mốc, không gửi hàng loạt DM cũ. Một vòng worker kiểm tra tối đa một task để hạn chế tải API. Watcher bị gỡ sẽ không nhận DM đang chờ nếu kiểm tra lại API cho thấy họ không còn theo dõi task. Trang quản lý hiển thị lỗi API hoặc lỗi phân giải.

Để thử Group, đặt `GROUPS_ENABLED=true` cùng `MULTI_RECIPIENT_ENABLED=true` và `OPENPROJECT_SERVICE_API_KEY`. Có thể giới hạn bằng `GROUP_PROJECT_IDS` gồm các project ID phân cách bằng dấu phẩy. Tài khoản dịch vụ cần quyền đọc danh sách thành viên Group (`manage_members` trong ít nhất một project theo [tài liệu OpenProject](https://www.openproject.org/docs/api/endpoints/groups/)). Bot lấy thành viên hiện tại qua `GET /api/v3/groups/{id}` khi xử lý sự kiện, chỉ gửi cho người đã liên kết và kiểm tra lại Group cùng assignee trước khi gửi. Thêm thành viên vào Group tự nó không tạo DM; sự kiện công việc tiếp theo mới có thể tạo DM. Group ngoài `GROUP_PROJECT_IDS`, lỗi API hoặc vượt `MAX_RECIPIENTS_PER_EVENT` hiện lỗi phân giải và không cập nhật snapshot task.

Giai đoạn D bổ sung trang quản lý trong LAN: từ `/op admin`, dùng **Tra cứu tài khoản** để tìm OpenProject theo ID/tên và Discord member theo ID; liên kết từng người hoặc nhập CSV `op_id,discord_id,name` (tối đa 25 dòng, xem trước rồi xác nhận). **Lịch sử sự kiện và DM** lọc theo task, OpenProject ID, trạng thái; xem vai trò nhận, số lần thử, lỗi, thời điểm nhận/tạo/gửi và retry từng webhook/DM lỗi. Mỗi liên kết có nút **Thử DM**; bản ghi đã gửi không thể retry. **Nhật ký quản trị** lưu Discord ID quản lý, thao tác, đích và thời điểm. Trang vẫn yêu cầu quyền Manage Server, phiên và CSRF như trước. Tra cứu tên qua `GET /api/v3/users` có thể đòi quyền rộng hơn tra cứu ID; nếu API trả 403, quản lý vẫn thấy lỗi rõ trên trang ([tài liệu Users API](https://www.openproject.org/docs/api/endpoints/users/)).

**Quy tắc người nhận** cho phép bật/tắt Watcher, Group và Accountable theo project và loại sự kiện. Các cờ môi trường vẫn là cổng tổng: Watcher cần `WATCHERS_ENABLED`, Group cần `GROUPS_ENABLED`, Accountable cần `ACCOUNTABLE_ENABLED`; mặc định Accountable không nhận DM. `MAX_RECIPIENTS_PER_EVENT` vẫn áp dụng cho tập người nhận sau khi gộp vai trò. Giao diện không cho bật Group/Watcher ngoài project allowlist tương ứng. Quy tắc Watcher cho “Cập nhật task” cũng điều khiển DM từ job đối soát Watcher. Quy tắc mới áp dụng khi phân giải sự kiện tiếp theo; DM đã xếp hàng vẫn qua bước kiểm tra tư cách người nhận. Để thử Accountable, bật `ACCOUNTABLE_ENABLED=true` và thêm quy tắc cho project/sự kiện mong muốn; bot kiểm tra lại trường Responsible trước khi gửi DM.

Giai đoạn E theo dõi ưu tiên, hạn, ngày bắt đầu, phần trăm hoàn thành và mô tả từ webhook khi `DETAIL_NOTIFICATIONS_ENABLED=true` cùng `MULTI_RECIPIENT_ENABLED=true`. Bot lưu mốc đầu tiên, chỉ nêu trường thực sự đổi trong các phiên bản sau. `ATTACHMENT_NOTIFICATIONS_ENABLED=true` dùng API dịch vụ để so sánh ID tệp; nếu API không đọc được hoặc task đã đổi phiên bản, bot bỏ phần tệp và vẫn xử lý thông báo còn lại. Tệp chỉ hiển thị số lượng trước/sau, không tải nội dung. [API Work packages](https://www.openproject.org/docs/api/endpoints/work-packages/) và [API Attachments](https://www.openproject.org/docs/api/endpoints/attachments/) mô tả các trường và endpoint cần quyền đọc.

`REMINDERS_ENABLED=true` bật nhắc **đến hạn hôm nay** lúc `REMINDER_HOUR` và **quá hạn** ngày kế tiếp; đồng hồ dùng `Asia/Ho_Chi_Minh`. Scheduler xác nhận hạn và trạng thái chưa đóng qua API trước khi xếp DM, rồi kiểm tra lại trước lúc gửi. Mỗi task, mốc hạn, loại nhắc và người nhận có một khóa bền vững trong SQLite; khởi động lại bình thường không xếp lại. Nếu hạn đổi, mốc mới có khóa riêng. Bot chỉ quét task đã có snapshot từ webhook và chỉ xét hôm nay/hôm qua, nên task cũ chưa xuất hiện trong webhook không bị nhắc hàng loạt.

Quản lý đặt mặc định bằng `NOTIFICATION_MODE=instant|daily`, `DIGEST_HOUR`, `QUIET_START`, `QUIET_END`. Hai giờ yên lặng dùng `HH:MM` và phải đặt cùng nhau; qua nửa đêm được hỗ trợ. Chế độ `daily` gộp tối đa 20 thông báo sự kiện/người vào một DM sau `DIGEST_HOUR`; thông báo còn dư sang ngày sau. Nhắc hạn vẫn gửi riêng sau giờ yên lặng. Người đã liên kết dùng `/op notify` trong server để xem, hoặc đặt `mode:daily`, `quiet:22:00-07:00`, `mute:comment,status` (dùng `off` để xóa giờ yên lặng/danh sách tắt). Lệnh trả lời riêng trong server. Đặt `USER_PREFERENCES_ENABLED=false` để người dùng chỉ xem cấu hình do quản lý đặt.

Hãy sao lưu SQLite trước lần khởi động đầu với phiên bản này vì schema được nâng tự động. Chưa bật các cờ trên server thật trước khi xác minh quyền API, payload Group thực tế và tín hiệu thêm/gỡ Watcher. Giữ `OPENPROJECT_SERVICE_API_KEY` ngoài Git.

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
