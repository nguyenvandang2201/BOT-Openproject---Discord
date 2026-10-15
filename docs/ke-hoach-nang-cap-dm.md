# Kế hoạch nâng cấp OpenProject → Discord DM

Ngày lập: 15/10/2026. Phạm vi: chế độ `DELIVERY_MODE=webhook_dm` trong repository này.

Tiến độ: mã giai đoạn A–D đã được triển khai và kiểm thử cục bộ; các cờ tính năng mặc định tắt. Trang quản lý có tra cứu, CSV, lịch sử, retry, thử DM, quy tắc và audit. Chưa xác minh bằng tài khoản dịch vụ OpenProject thật, chưa bật Watchers/Group/Accountable trên server.

## 1. Mục tiêu và hiện trạng

Bot chỉ hoạt động với thành viên của **một Discord server** và gửi thông báo bằng DM. Quản lý tạo liên kết OpenProject user ID ↔ Discord user ID tại trang quản lý trong mạng nội bộ; người tham gia không cần cấu hình. Giữ DM chào mừng khi vừa liên kết, thẻ thông báo có nút mở công việc, và ba loại sự kiện hiện có: giao việc, đổi trạng thái, bình luận công khai.

Trước giai đoạn A, `webhook.py` chỉ đọc assignee dạng `/api/v3/users/{id}` và `store.py` chỉ xếp DM cho một assignee. Trường hợp task #1167 đã xác nhận: assignee nhận DM, các Watchers được liên kết không nhận DM. Mã A–D hiện hỗ trợ nhiều người nhận theo ID OpenProject và quản trị qua LAN, nhưng cần kiểm tra quyền API và chạy thử trên server thật.

**Kết quả mong muốn trước tiên:** cùng một sự kiện gửi đúng một DM cho từng người đủ điều kiện trong tập Assignee / thành viên Group được giao / Watchers. Người chưa liên kết, liên kết bị tạm dừng hoặc đã rời Discord server không nhận DM. Không tạo channel riêng và không gửi bình luận nội bộ.

## 2. Quy tắc người nhận đề xuất

| Sự kiện | Người nhận mặc định | Nội dung và lưu ý |
|---|---|---|
| Tạo task, assignee là User | Assignee; Watchers đã có khi xử lý sự kiện | Assignee thấy “Bạn được giao”; Watchers thấy “Bạn đang theo dõi”. |
| Tạo task, assignee là Group | Thành viên Group đã liên kết; Watchers | Thành viên thấy tên Group được giao. Không gửi cho toàn bộ Discord server. |
| Đổi assignee User/Group | Người được giao mới hoặc thành viên Group mới; Watchers hiện tại | Người cũ không nhận thông báo “bị gỡ” trong bản đầu. |
| Đổi trạng thái | Assignee hoặc thành viên Group hiện tại; Watchers hiện tại | Một DM/người/sự kiện, dù người đó có nhiều vai trò. |
| Bình luận công khai | Assignee hoặc thành viên Group hiện tại; Watchers hiện tại | Giữ giới hạn trích đoạn 300 ký tự. Bình luận nội bộ luôn bị bỏ qua. |
| Thêm Watcher | Watcher mới được liên kết | Một DM giới thiệu task **nếu xác minh được tín hiệu thêm Watcher**; nếu không có webhook phù hợp thì dùng đối soát định kỳ. |
| Gỡ Watcher / rời Group | Không gửi thông báo riêng | Người đó không còn nhận các sự kiện tiếp theo từ vai trò đã gỡ. |

Mặc định chưa thêm `Accountable`, `Reporter`, toàn bộ project hoặc toàn bộ Group không liên quan vào danh sách nhận. Các vai trò này chỉ bật sau khi quản lý chọn quy tắc và xác nhận quyền truy cập công việc của người nhận. Nếu người vừa là assignee vừa là watcher, gộp lý do vào một thẻ DM. Quy tắc loại trừ người tự thực hiện thay đổi để **tắt mặc định**, có thể bổ sung tùy chọn sau.

Danh sách Group và Watchers được đọc tại **thời điểm xử lý** webhook, không phải ảnh chụp lịch sử tại thời điểm OpenProject phát sự kiện. Sau khi xác định, lưu tập người nhận cho sự kiện để retry không tự sinh người nhận mới. Trước khi gửi, kiểm tra lại liên kết, tư cách thành viên Discord và điều kiện vai trò; hủy DM chờ nếu người đó đã mất quyền nhận. Việc kiểm tra lại bằng API cần có giới hạn thời gian và retry, không âm thầm đánh dấu `sent` khi không xác minh được.

## 3. Điều kiện kỹ thuật phải xác minh trước khi sửa luồng gửi

1. Tạo tài khoản dịch vụ OpenProject riêng; bridge chỉ dùng token đó cho lệnh **GET** và cấp quyền tối thiểu đáp ứng API. Thử `GET /api/v3/work_packages/{id}/watchers` cho task thử nghiệm và `GET /api/v3/groups/{id}` cho Group thử nghiệm. Tài liệu OpenProject cho thấy danh sách thành viên Group cần quyền `manage_members` trong ít nhất một project; phải kiểm tra quyền thực tế trên máy chủ trước khi chốt cách cấp quyền. Không dùng token cá nhân của người tham gia.
2. Ghi lại mẫu webhook thật khi tạo task, đổi assignee thành Group, thêm/gỡ Watcher, đổi trạng thái và bình luận. Đặc biệt xác minh **thêm/gỡ Watcher có phát `work_package:updated` hay không**. Nếu không, không thể cam kết DM ngay lúc thêm Watcher chỉ dựa vào webhook; cần job đối soát.
3. Kiểm tra đường dẫn assignee Group trong payload (`/api/v3/groups/{id}`), cấu trúc `_links.members`/`_embedded.members`, tình huống Group trống, thành viên không hoạt động và giới hạn phân trang trên phiên bản OpenProject đang chạy.
4. Chốt project thử nghiệm, hai hoặc ba tài khoản Discord thử nghiệm, các OpenProject user ID đã liên kết và ngưỡng số DM tối đa cho một sự kiện. Không gửi thử hàng loạt vào toàn bộ Group thật.

Tài liệu API: [Watchers](https://www.openproject.org/docs/api/endpoints/work-packages/), [Groups và quyền đọc members](https://www.openproject.org/docs/api/endpoints/groups/), [cấu hình webhooks](https://www.openproject.org/docs/system-admin-guide/api-and-webhooks/).

## 4. Giai đoạn triển khai

### Giai đoạn A — Nền tảng gửi cho nhiều người

**Thay đổi:**

- Mở rộng bộ phân tích webhook để nhận assignee User hoặc Group; kiểm tra nghiêm ngặt ID và URL, không dùng tên để ghép người.
- Thêm cấu hình token dịch vụ trong biến môi trường và client API chỉ đọc với cùng cơ chế kiểm tra origin, timeout, `Retry-After` như client hiện có. Không ghi token vào log, SQLite hoặc trang quản lý.
- Tách xử lý thành `nhận webhook → lưu sự kiện tối giản → tìm người nhận → tạo DM jobs → gửi`. HTTP chỉ trả `202` sau khi sự kiện đã được lưu bền vững; gọi OpenProject API trong worker để webhook không bị treo bởi API chậm.
- Thêm trạng thái sự kiện `pending_resolution`, `resolved`, `retry`, `failed`. Lỗi 429/5xx/network sẽ retry có backoff; 401/403 sẽ báo rõ trong trang quản lý. Không mất sự kiện vì API tạm lỗi.
- Nâng schema SQLite theo cách cộng thêm, giữ toàn bộ `dm_links`, `work_package_state`, DM chào mừng và lịch sử `dm_deliveries` cũ. Khóa gửi trùng mới phải gồm **sự kiện nguồn + OpenProject user ID người nhận**; một người trùng nhiều vai trò hoặc cùng lúc được giao việc và đổi trạng thái chỉ có một DM tổng hợp cho sự kiện đó. Không dùng Discord ID làm thành phần khóa vì thay liên kết không được phát lại sự kiện cũ.
- Giữ phiên bản `lockVersion` của work package để bỏ webhook cũ/đến lệch thứ tự; comment dùng `activity_id`. Lưu ảnh chụp nội dung đã rút gọn, không lưu toàn bộ body webhook.

**Hoàn thành khi:** migration từ bản DB hiện tại chạy được hai lần mà không tạo DM trùng; 1 sự kiện có thể tạo ≥2 jobs độc lập; webhook lặp lại tạo 0 jobs mới; assignee User hiện tại vẫn nhận đúng như trước.

**Dữ liệu và điểm sửa mã dự kiến:**

| Thành phần | Việc cần làm |
|---|---|
| `webhook.py` | Phân tích `_links.assignee` theo User/Group; lưu projection và event ID, không gọi API trong HTTP handler. |
| `openproject.py`, `config.py` | Client đọc Watchers/Group, xác thực token dịch vụ, giới hạn origin/timeout/rate limit. |
| `store.py` | Bảng `inbound_events` (event ID, task ID, version, projection, trạng thái/attempts/next_attempt/lỗi); bảng `delivery_claims` khóa duy nhất `(source_event_id, op_id)`; mở rộng `dm_deliveries` bằng source event ID và vai trò nhận; chỉ ghi snapshot + claims + jobs trong một transaction sau khi phân giải thành công. |
| `WebhookService` | Worker phân giải sự kiện trước, worker gửi DM sau; retry hai giai đoạn độc lập. |
| `bot.py`, `admin_web.py` | Embed gộp thay đổi, kiểm tra điều kiện trước khi gửi, hiện lỗi phân giải và lịch sử gửi. |
| `tests/test_webhook_dm.py`, `tests/test_admin_web.py` | Kiểm thử migration, phân giải, fanout, dedup, lỗi API và thao tác quản trị. |

Migration cần suy ra `source_event_id` và OpenProject user ID từ các bản ghi `dm_deliveries` cũ để tạo `delivery_claims`, đồng thời đánh dấu các sự kiện nguồn cũ đã xử lý; webhook/comment cũ phát lại không mở rộng thêm người nhận. Hai DM cũ thuộc cùng một webhook vẫn được giữ nguyên trong lịch sử, dù từ bản mới chỉ có một DM tổng hợp. Bản ghi `welcome` được giữ theo cơ chế riêng. Snapshot của task chỉ tiến lên khi sự kiện đã phân giải xong; nếu API Group/Watchers lỗi, retry không bị `lockVersion` mới chặn mất sự kiện. Giới hạn kích thước projection, số trang API và số lần thử để queue không tăng vô hạn.

### Giai đoạn B — Watchers

**Thay đổi:**

- Đọc danh sách qua `GET /api/v3/work_packages/{id}/watchers`, kiểm tra kiểu User, ID, trang kế tiếp và giới hạn số người; giao với `dm_links.enabled=1`.
- Lập một tập người nhận cho tạo task, đổi assignee, đổi trạng thái và bình luận công khai theo bảng mục 2. Gộp vai trò trùng; thẻ DM ghi rõ “Bạn được giao” hoặc “Bạn theo dõi công việc”.
- Lưu snapshot Watchers theo task để phát hiện thành viên mới. Chỉ tạo DM giới thiệu cho người mới **sau khi** kiểm chứng webhook thêm Watcher; nếu không có webhook thì thêm job đối soát có chu kỳ, mốc ban đầu không gửi lại thông báo hàng loạt.
- Khi Watcher bị gỡ trước lúc job chạy, đánh dấu job `cancelled` với lý do; khi API Watchers không đọc được, đưa sự kiện vào trạng thái cần retry/điều tra.

**Hoàn thành khi:** watcher đã liên kết nhận DM cho sự kiện mới trên task #1167 hoặc task thử nghiệm tương đương; người không liên kết không nhận; assignee đồng thời là watcher chỉ nhận một DM; watcher bị gỡ không nhận sự kiện sau đó.

### Giai đoạn C — Assignee là Group

**Thay đổi:**

- Đọc Group bằng `GET /api/v3/groups/{id}`, lấy và kiểm tra danh sách User thành viên; chỉ dùng OpenProject user ID đã liên kết, không dùng Discord role cùng tên.
- Tạo một job cho mỗi thành viên đủ điều kiện; loại người trùng với Watchers; lưu vai trò nguồn để người quản lý biết vì sao họ nhận DM.
- Đặt giới hạn số người nhận/sự kiện (đề xuất khởi đầu 50, cấu hình được). Vượt ngưỡng thì giữ sự kiện ở trạng thái cần xử lý, **không cắt bớt im lặng**.
- Thành viên Group được thêm/gỡ sẽ áp dụng cho sự kiện tương lai. Nếu muốn DM ngay khi thêm thành viên vào Group, cần một cơ chế đối soát riêng; không gộp mặc định vào bước này.

**Hoàn thành khi:** giao một task cho Group ba thành viên, trong đó hai người đã liên kết, tạo đúng hai DM; người không liên kết không nhận; một người cũng là watcher vẫn chỉ một DM; đổi assignee Group → User không làm các thành viên Group cũ nhận thông báo sau đó.

### Giai đoạn D — Trang quản lý và vận hành

**Thay đổi:**

- Giữ đăng nhập `/op admin`, quyền Manage Server, mạng nội bộ, phiên đăng nhập và CSRF hiện tại.
- Thêm tra cứu người dùng OpenProject theo ID/tên để xác nhận trước khi liên kết; tìm Discord member theo ID; hỗ trợ nhập nhiều liên kết bằng CSV có màn hình xem trước và báo dòng lỗi.
- Thêm trang lịch sử sự kiện/DM: task, loại sự kiện, vai trò nhận, OpenProject ID, Discord ID, trạng thái, số lần thử, lỗi, thời điểm nhận/gửi. Bộ lọc theo task/người/trạng thái; không hiển thị token hoặc toàn bộ nội dung bình luận.
- Thêm nút thử DM tới **một** tài khoản đã liên kết, retry từng job lỗi, xem sự kiện chưa phân giải, và nhật ký thao tác quản trị (ai, thao tác, thời điểm). Không tạo lại DM đã `sent` khi bấm retry.
- Thêm màn hình quy tắc người nhận theo project và loại sự kiện; lúc đầu chỉ có tùy chọn Watchers, Group, Accountable và tắt/bật theo project, với cấu hình mặc định theo mục 2. Chặn cấu hình có nguy cơ gửi cho cả project trước khi có giới hạn người nhận.

**Hoàn thành khi:** quản lý có thể tự xác định “vì sao người A không nhận DM” từ trang nội bộ mà không cần SSH; các thao tác thay đổi đều có kiểm tra quyền và dấu vết.

### Giai đoạn E — Thông báo phong phú và giảm nhiễu

- Bổ sung thay đổi ưu tiên, hạn, ngày bắt đầu, `% hoàn thành`, mô tả và tệp đính kèm **theo lựa chọn**. Cần lưu snapshot thuộc tính để chỉ báo trường thực sự đổi; webhook OpenProject không cung cấp trực tiếp bản diff đáng tin cho mọi trường.
- Thêm nhắc hạn/hết hạn bằng scheduler riêng, timezone `Asia/Saigon`, chỉ gửi một lần cho mỗi mốc hạn hoặc khi hạn đổi. Không dựa vào webhook cho sự kiện thời gian.
- Thêm cấu hình thông báo tức thời, bản tóm tắt hằng ngày, giờ yên lặng và tắt bớt loại sự kiện theo người dùng. Quản lý đặt mặc định; người dùng có thể điều chỉnh nếu được cho phép.
- Thêm template embed rõ loại sự kiện, thay đổi trước → sau, người tác động, project, hạn, trạng thái và nút mở task. Giới hạn độ dài theo Discord và escape dữ liệu từ OpenProject.

**Hoàn thành khi:** cùng một chuỗi cập nhật không gây spam không cần thiết, nội dung chỉ nêu thay đổi đã xác nhận, và digest/nhắc hạn không gửi lại sau restart.

### Giai đoạn F — Độ tin cậy, bảo mật và mở rộng

- Thêm số liệu `webhook_received`, `resolution_pending/failed`, `dm_pending/sent/failed/cancelled`, độ trễ và cảnh báo nếu backlog vượt ngưỡng; endpoint health/readiness phản ánh worker.
- Throttle DM theo phản hồi Discord, tôn trọng `retry_after`, giới hạn batch và độ đồng thời; phân biệt lỗi tạm thời với DM bị chặn hoặc người dùng rời server.
- Sao lưu SQLite nhất quán cùng WAL, kiểm tra khôi phục định kỳ, đặt thời hạn lưu lịch sử và cơ chế xóa dữ liệu người đã gỡ liên kết theo chính sách vận hành.
- Giữ trang quản lý chỉ trong LAN/VPN, secret ở biến môi trường, xoay token dịch vụ và webhook secret có quy trình; kiểm tra quyền truy cập OpenProject trước khi đưa nội dung nhạy cảm vào DM.
- Chỉ cân nhắc nút thao tác từ Discord (đổi trạng thái, bình luận) sau khi có xác thực OpenProject theo từng người và phân quyền rõ ràng. Giai đoạn hiện tại vẫn chỉ đọc và thông báo.

## 5. Ma trận kiểm thử bắt buộc

| Kịch bản | Kết quả cần đạt |
|---|---|
| Task mới giao User; cập nhật trạng thái; bình luận công khai | Hành vi assignee cũ không bị mất. |
| Bình luận nội bộ | Không tạo DM nào. |
| Hai Watchers liên kết, một Watcher chưa liên kết | Chỉ hai người đã liên kết nhận DM. |
| Assignee cũng là Watcher | Một DM cho cùng sự kiện/nội dung. |
| Assignee Group có thành viên được thêm/gỡ | Chỉ thành viên hiện đủ điều kiện nhận sự kiện tương lai. |
| Webhook lặp, đến sai thứ tự, worker restart | Không tạo job trùng hoặc mất sự kiện; retry tiếp tục. |
| Đổi/hủy liên kết trước khi gửi | Job cũ bị hủy; không gửi sang Discord ID mới vì job cũ. |
| API OpenProject trả 401/403/429/500 hoặc timeout | Hiện lỗi phân giải, retry khi thích hợp, không báo `sent` giả. |
| Discord từ chối DM, 429, bot/người rời server | Phân loại lỗi đúng, không chặn các người nhận khác. |
| DB từ schema hiện hành nâng lên và khởi động lại | Giữ link/DM cũ; không gửi lại toàn bộ Watchers. |
| Thêm Watcher không phát webhook | Job đối soát phát hiện trong chu kỳ đã định hoặc UI ghi rõ giới hạn nếu chưa bật đối soát. |

Kiểm thử unit dùng payload mẫu OpenProject thật đã ẩn thông tin; integration dùng HTTP API giả và SQLite thật; kiểm thử cuối dùng project `test` và tài khoản Discord thử nghiệm. Không dùng một bài test tự động để phát DM tới người dùng thật.

## 6. Triển khai và quay lui

1. Trước triển khai: xác nhận tài khoản dịch vụ và hai endpoint đọc hoạt động; chụp mẫu webhook; sao lưu SQLite nhất quán và tệp cấu hình **không đưa secret vào Git**; ghi số lượng link, pending, failed hiện có.
2. Triển khai schema + worker với cờ `MULTI_RECIPIENT_ENABLED=false`. Chạy migration và kiểm thử smoke (`/readyz`), xác minh luồng assignee cũ và xem số liệu phân giải ở chế độ quan sát.
3. Bật Watchers cho project `test` và những tài khoản thử đã liên kết. Kiểm tra task #1167 hoặc task mới, trùng vai trò, thêm/gỡ Watcher, retry. Sau đó mở rộng theo project.
4. Bật Group sau khi Watchers ổn định và tài khoản dịch vụ đọc được Group members. Thử Group nhỏ trước; giám sát số job/sự kiện, lỗi Discord và độ trễ trong 24 giờ đầu.
5. Nếu có sự cố, tắt cờ tính năng mới để trở lại tuyến assignee; giữ lịch sử `sent` và queue để không gửi lại. Chỉ quay về image cũ sau khi xác nhận schema mới tương thích hoặc đã có bản sao DB trước migration. Tuyệt đối không xóa volume SQLite.

**Thứ tự ưu tiên:** A → B → C → D; E và F triển khai dần sau khi luồng nhiều người chạy ổn. Ước lượng sơ bộ cho một người phát triển: A 2–3 ngày, B 2–3 ngày, C 1–2 ngày, D 2–4 ngày. Ước lượng phụ thuộc mạnh vào quyền API Group và việc OpenProject có phát tín hiệu khi thay Watcher hay không; E/F sẽ chốt riêng sau khi có dữ liệu sử dụng.

## 7. Quyết định cần chốt khi bắt đầu viết mã

- Kiểm chứng bằng thử nghiệm trên OpenProject hiện tại rằng thêm/gỡ Watcher có phát webhook. Đây là điều kiện quyết định giữa thông báo ngay và đối soát định kỳ.
- Xác nhận tài khoản dịch vụ được phép đọc Group members trong các project áp dụng. Nếu chính sách không cho cấp quyền đó, thay Group fanout bằng danh sách người nhận do quản lý cấu hình trong bridge.
- Chọn giới hạn người nhận tối đa/sự kiện và project thử nghiệm. Mặc định đề xuất 50 và project `test`.

Các điểm này vẫn cần được xác minh trước khi bật các vai trò mới trên server thật.
