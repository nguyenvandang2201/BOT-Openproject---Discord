# Kế hoạch chuyển bot sang private channel theo người dùng

## Trạng thái triển khai

| Phần | Trạng thái trong repository | Điều kiện còn lại |
|---|---|---|
| 1. Migration SQLite và cấu hình một guild/category | Đã viết, test offline | Sao lưu DB thật trước khi chạy migration trên VPS |
| 2. Công cụ quản lý liên kết bằng API token riêng | Đã viết, test HTTP mock | Kiểm tra quyền Discord/OpenProject với tài khoản thật |
| 3. Gửi vào private channel, chặn DM và self-link | Đã viết, test offline | Kiểm tra channel thật và notification thật |
| 4. Tài liệu và kiểm thử hồi quy | Đã viết | Build Docker và smoke test trên máy có Docker engine |
| 5. Triển khai và nghiệm thu trên VPS | Chưa thực hiện | Cấu hình category ID, backup, migrate account, thử hai người |

Mã hiện tại chưa được triển khai trên VPS. Cách vận hành chi tiết nằm trong
[README](../README.md).

## 1. Mục tiêu và phạm vi

- Bot chỉ hoạt động trong một Discord server được cấu hình bằng `DISCORD_GUILD_ID`.
- Mỗi người được liên kết có đúng một text channel riêng trong một category `OPENPROJECT`.
- Bot chỉ gửi notification của OpenProject vào channel đã gán cho đúng người, không gửi DM và không gửi vào channel chung.
- Người dùng thường chỉ xem được channel của mình. Chủ server và thành viên có quyền `Administrator` vẫn xem được mọi channel theo cơ chế quyền của Discord.
- Quản lý thực hiện liên kết và gỡ liên kết. Người dùng không tự chạy `/op link` hay `/op confirm`.
- Giữ nguyên nguyên tắc chỉ chuyển in-app notification có ID mới; không đánh dấu đã đọc trên OpenProject.

Phạm vi MVP là một server, một tiến trình bot, SQLite hiện có và số lượng người dùng đủ nhỏ để quản lý channel thủ công. Một category chứa tối đa 50 channel; khi vượt mức này cần tạo thêm category theo nhóm.

## 2. Quyết định về nguồn xác thực OpenProject

Discord ID chỉ xác định người nhận trên Discord. Để đọc notification cá nhân, bot phải có thông tin xác thực của chính tài khoản OpenProject đó. API notification trả dữ liệu theo quyền của người gọi; không dùng token của quản lý để đọc thay mọi người.

**Phương án triển khai mặc định cho liên kết mới:** quản lý nhập personal API token riêng của từng tài khoản OpenProject bằng công cụ chạy trên máy chủ. Công cụ không nhận token qua tham số dòng lệnh, slash command, chat hay URL; token được đọc từ prompt ẩn, kiểm tra bằng `GET /api/v3/users/me`, rồi mã hóa bằng khóa Fernet hiện có. Quản lý phải có cách được phép lấy token từ chủ tài khoản. Nếu tổ chức không chấp nhận việc quản lý giữ token của người dùng, đổi riêng bước cấp quyền này sang OAuth do người dùng phê duyệt và quản lý xác nhận cặp ID trước khi kích hoạt.

Liên kết OAuth đang tồn tại được giữ lại trong giai đoạn chuyển đổi: tiếp tục refresh token và polling bằng credential cũ. Chức năng tạo liên kết OAuth mới trên Discord bị tắt khi triển khai chế độ quản lý thủ công. Chỉ bỏ OAuth client secret và callback sau khi không còn tài khoản dùng OAuth.

## 3. Cấu trúc và quyền Discord

Tạo category `OPENPROJECT` và các channel tên trung tính, ví dụ `op-<discord_id>`. Không đưa tên người, tên dự án, tiêu đề task hoặc nội dung notification vào tên hay topic của channel.

Bot tạo channel với permission overwrites ngay lúc tạo:

| Đối tượng | View Channel | Read Message History | Send Messages | Embed Links |
|---|---:|---:|---:|---:|
| `@everyone` | Từ chối | Theo View Channel | Từ chối | Từ chối |
| Người nhận | Cho phép | Cho phép | Từ chối | Không cần |
| Bot | Cho phép | Cho phép | Cho phép | Cho phép |

Không thêm role chung vào danh sách cho phép. Quản lý có quyền `Administrator` sẽ xem được channel; không cấp `Administrator` cho bot. Bot cần `Manage Channels`, `Manage Roles` (để gỡ quyền khi unlink), `View Channel`, `Send Messages`, `Read Message History` và `Embed Links` tại phạm vi phù hợp. Nếu channel được tạo dưới category, bot vẫn phải đặt overwrites riêng cho từng channel, không dựa vào việc tự đồng bộ quyền category.

Trước khi kích hoạt và trước mỗi lượt gửi, bot kiểm tra channel đúng guild, đúng category, còn tồn tại và có tập quyền xem như chính sách trên. Nếu phát hiện một role hoặc người dùng khác được cấp `View Channel`, bot tạm dừng gửi và báo lỗi cho quản lý. Quyền `Administrator` của server luôn là ngoại lệ mà bot không thể che nội dung khỏi họ.

## 4. Luồng quản lý liên kết

MVP dùng công cụ quản trị chạy trên host qua SSH, trong một cửa sổ bảo trì ngắn. Dừng container bridge trước khi thay đổi liên kết để không có worker đang gửi, sau đó chạy công cụ với cùng volume SQLite và khóa mã hóa, rồi khởi động lại. Không đặt endpoint quản trị trên hostname HTTPS công khai.

### Tạo liên kết

1. Quản lý cung cấp Discord ID. Công cụ kiểm tra ID thuộc guild đã cấu hình bằng `Get Guild Member` của Discord; không dựa vào cache Gateway.
2. Công cụ hỏi OpenProject personal API token qua prompt ẩn. Gọi `/api/v3/users/me` để lấy OpenProject ID và tên hiển thị. Không cho quản lý tự gõ OpenProject ID thay cho kết quả xác thực.
3. Kiểm tra Discord ID và OpenProject ID chưa liên kết với tài khoản khác. Nếu đã có liên kết, dừng và yêu cầu thao tác chuyển/gỡ riêng; không ghi đè âm thầm.
4. Lấy toàn bộ ID notification hiện tại làm baseline. Nếu lỗi API hoặc phân trang chưa hoàn chỉnh, dừng trước khi tạo liên kết.
5. Tạo channel riêng với overwrites ở mục 3; kiểm tra lại quyền vừa tạo và lưu `channel_id` Discord trả về.
6. Trong một giao dịch SQLite, lưu tài khoản, credential đã mã hóa, `guild_id`, `channel_id`, trạng thái `active` và các ID baseline. Chỉ kích hoạt khi mọi bước đã thành công.
7. Nếu tạo channel thành công nhưng ghi DB thất bại, ghi nhận channel mồ côi để quản lý xử lý; không tự gắn nó cho người khác. Công cụ có lệnh kiểm tra và dọn channel mồ côi sau khi xác nhận.

### Gỡ hoặc tạm dừng

- `suspend`: dừng polling và gửi, giữ credential, baseline và hàng đợi để xử lý sự cố.
- `unlink`: xóa credential, liên kết và hàng đợi của tài khoản trong DB. Channel được giữ nguyên theo mặc định để quản lý quyết định có lưu lịch sử hay không; quyền xem của người dùng bị gỡ nếu chính sách là ngừng truy cập ngay.
- `delete-channel`: thao tác quản trị riêng, yêu cầu chỉ đúng channel do bot quản lý; việc xóa channel làm mất lịch sử trên Discord và không thể hoàn tác.
- Thay người nhận Discord hoặc tài khoản OpenProject phải đi qua quy trình gỡ và tạo mới, có baseline mới; không sửa ID trực tiếp trên bản ghi đang hoạt động.

## 5. Thay đổi trong mã và dữ liệu

| Thành phần | Công việc |
|---|---|
| `config.py` | Bắt buộc `DISCORD_GUILD_ID`, thêm `DISCORD_CATEGORY_ID`; kiểm tra ID và không cho chạy chế độ DM. |
| `store.py` | Migration có phiên bản cho `guild_id`, `channel_id`, loại credential (`oauth`/`api_key`) và trạng thái đích gửi. `channel_id` là duy nhất; ràng buộc một Discord ID và một OpenProject ID như hiện tại. |
| `openproject.py` | Hỗ trợ Basic auth `apikey:<personal token>` bên cạnh Bearer OAuth; giữ pagination, kiểm tra URL và xử lý rate limit hiện có. |
| `bot.py` | Thay `user.send()` bằng gửi vào channel đã lưu; không có DM fallback. Giữ định dạng embed và vô hiệu hóa mentions. Chỉ đăng ký lệnh trong guild; loại bỏ các lệnh link/confirm cũ. |
| `service.py` | Kiểm tra thành viên guild và quyền channel trước khi gửi; phân loại lỗi `member_absent`, `channel_missing`, `channel_permissions_changed`, `discord_send_failed`. Giữ queue khi lỗi đích gửi. |
| `web.py` | Giữ `/healthz` và `/readyz`. Tắt `/link` và `/oauth/callback` cho liên kết mới; chỉ giữ callback nếu chọn biến thể OAuth có quản lý phê duyệt. |
| Công cụ quản trị mới | Các lệnh `link`, `list`, `status`, `suspend`, `unlink`, `audit-channels`; nhập token ẩn và không in/log token. |
| Compose và tài liệu | Cấu hình category ID, quyền bot, quy trình bảo trì, backup và phục hồi. |

Migration phải giữ nguyên token OAuth đã mã hóa và ledger notification hiện có. Bản ghi cũ chưa có `channel_id` chuyển sang `migration_pending`; worker mới tuyệt đối không gửi DM. Quản lý tạo và gán channel cho từng bản ghi, kiểm tra quyền, rồi mới chuyển sang `active`. Notification đang chờ được giữ lại và gửi vào channel sau khi kích hoạt. Sao lưu DB và khóa Fernet trước migration; thử khôi phục trên môi trường cô lập.

## 6. Quy tắc gửi và xử lý lỗi

1. Worker lấy notification bằng credential của tài khoản tương ứng, ghi ID mới vào ledger và đọc lại từng notification trước khi gửi như hiện tại.
2. Xác minh người nhận còn trong guild ở đầu mỗi chu kỳ polling; trước mỗi lần gửi, kiểm tra channel còn tồn tại và quyền xem vẫn đúng chính sách. Nếu Discord trả lỗi khi gửi, cập nhật trạng thái và giữ job trong hàng đợi.
3. Gửi embed vào `channel_id`; chỉ ghi `sent` sau khi Discord trả thành công. Không ping `@everyone`, role hoặc người khác.
4. Khi lỗi quyền, channel bị xóa, người dùng rời guild hoặc Discord tạm lỗi: giữ job `pending`, ghi mã lỗi cụ thể, áp dụng retry/backoff phù hợp. Với lỗi cấu hình hoặc thành viên vắng mặt, tạm dừng gửi cho tài khoản tới khi quản lý sửa và kích hoạt lại; không thử vô hạn mỗi 5 giây.
5. Không tạo lại channel tự động khi channel bị xóa, để tránh tạo nhiều channel hoặc cấp quyền sai. `audit-channels` báo sai lệch và cho quản lý tạo/gán lại có kiểm soát.

Ngữ nghĩa gửi vẫn là at-least-once: tiến trình chết sau khi Discord nhận tin nhưng trước khi DB ghi `sent` có thể gây một tin lặp. Không hứa exactly-once.

## 7. Kiểm thử và điều kiện hoàn tất

### Kiểm thử tự động

- API token của A chỉ đọc notification của A; mapping trùng Discord ID hoặc OpenProject ID bị từ chối.
- Tạo channel có đúng overwrites; channel thiếu quyền hoặc có thêm người xem không được kích hoạt/gửi.
- Người A chỉ được gửi vào `channel_id` của A, người B chỉ vào channel B; không có lời gọi `user.send()` trong nhánh gửi notification.
- Baseline bỏ qua lịch sử; restart không gửi lại tin đã ghi `sent`; job đang chờ vẫn tồn tại.
- Người rời guild, channel bị xóa, mất quyền, lỗi 429/5xx và lỗi token đều giữ trạng thái phù hợp, không gửi sang DM/channel chung.
- Migration dữ liệu OAuth cũ giữ token và ledger, đưa account thiếu channel vào `migration_pending`.

### Kiểm tra trên server thử nghiệm

1. Liên kết hai tài khoản OpenProject và Discord khác nhau; đăng nhập từng Discord user để kiểm tra chỉ thấy channel của mình. Đăng nhập quản trị viên để kiểm tra thấy cả hai.
2. Tạo notification thực cho A; xác nhận nó xuất hiện ở channel A trong khoảng chu kỳ polling, không xuất hiện ở channel B và không có DM.
3. Khởi động lại bot, tạo notification mới, kiểm tra không lặp tin cũ.
4. Cho A rời server hoặc xóa channel A; xác nhận bot giữ queue, báo lỗi và không gửi sang nơi khác.
5. Sửa quyền channel để thêm người xem; xác nhận audit phát hiện và dừng gửi trước khi đẩy nội dung mới.
6. Gỡ liên kết A; xác nhận token và queue bị xóa, channel/lịch sử được xử lý theo lựa chọn của quản lý.

## 8. Thứ tự triển khai và quay lui

1. Sao lưu DB, volume và khóa Fernet; ghi nhận các account OAuth hiện có và queue đang chờ.
2. Thêm migration và hỗ trợ hai loại credential; kiểm thử offline.
3. Thêm công cụ quản trị và tạo/audit private channel; kiểm thử với Discord server thử nghiệm.
4. Đổi worker sang gửi channel, chặn DM ở mức mã và đăng ký slash command chỉ trong guild. Đồng bộ xóa lệnh cũ trên Discord.
5. Triển khai bản mới với các account cũ ở `migration_pending`; gán channel và xác minh từng account trước khi kích hoạt.
6. Chạy kiểm tra thực với hai người, theo dõi lỗi và queue trước khi đưa toàn bộ người dùng vào sử dụng.

Nếu cần quay lui trước khi kích hoạt lại, dừng bot, khôi phục DB cùng khóa Fernet và phiên bản container trước từ cùng thời điểm backup. **Không chạy song song bản cũ và mới**; bản cũ có thể tiếp tục gửi DM. Tin đã đăng trong Discord không được thu hồi bởi thao tác quay lui.

## 9. Điểm cần quyết định trước khi viết mã

1. Quản lý có được phép nhận và quản lý personal API token của từng người không? Nếu không, dùng OAuth có bước quản lý phê duyệt.
2. Khi `unlink`, giữ channel và lịch sử hay gỡ quyền người dùng ngay? Kế hoạch mặc định: giữ channel, gỡ quyền xem của người dùng.
3. Channel chỉ đọc hay cho phép người dùng trả lời trong channel? Kế hoạch mặc định: chỉ đọc.

Nguồn kỹ thuật: [Discord Create Guild Channel](https://docs.discord.com/developers/resources/guild), [Discord Permissions](https://docs.discord.com/developers/topics/permissions), [OpenProject Notifications API](https://www.openproject.org/docs/api/endpoints/notifications/).
