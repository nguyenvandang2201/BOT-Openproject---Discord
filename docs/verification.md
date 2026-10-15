# Kết quả kiểm tra MVP — 2026-10-15

> Kết quả dưới đây thuộc phiên bản MVP gửi DM. Bộ kiểm thử private channel hiện
> có kết quả riêng trong README; chưa kiểm tra phiên bản mới trên Discord/OpenProject thật.

- **32 tests passed** trên Python 3.12.10: HTTP API mock, SQLite thực, luồng HTTP OAuth
  với cookie/state, mã xác nhận gắn Discord ID, hết hạn/replay, unique mapping, baseline,
  phân trang, chặn link khác origin, rate limit, refresh token, retry DM, restart, unlink
  khi đang polling, giới hạn embed và khai báo slash commands.
- `ruff check src tests`: đạt.
- `pip check`: không có dependency bị thiếu/xung đột.
- Docker image `openproject-discord-bridge:local`: build thành công trên Linux container.
- Smoke test image với **network none**, user UID 10001, root filesystem chỉ đọc:
  mở SQLite/WAL trong vùng ghi, tạo phiên liên kết, khởi tạo bot và kiểm tra health/readiness
  qua ASGI thành công. Test dùng dữ liệu giả, không đăng nhập Discord thật.
- `docker compose config` cho cấu hình mặc định và override tunnel: hợp lệ.

Một cảnh báo deprecation `audioop` từ discord.py trên Python 3.12 xuất hiện khi test;
không ảnh hưởng kết quả. Image hiện khóa Python 3.12, chưa kiểm tra Python 3.13+.

**Chưa xác minh:** API được xác thực trên `openproject.lazy247.space`, authorization/refresh
với OAuth application thật, Gateway login và DM thật, DNS/Cloudflare route trên VPS.
Request không xác thực tới API root/spec của host này từng trả 403; kiểm tra lại sau khi
NetBird kết nối trả 401. Không thay đổi OpenProject,
Cloudflare hoặc Discord của người dùng trong quá trình build/test.

Thực hiện các bước staging trong README sau khi điền secrets tại môi trường triển khai.
