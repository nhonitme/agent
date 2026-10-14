AGENT_PORT=8099
Cổng HTTP mà agent lắng nghe. AMNote gọi agent tại http://127.0.0.1:8099.

Mặc định: 8099
Hợp lệ: 1–65535
Đổi nếu cổng bị chiếm bởi phần mềm khác.

-----------------------------------------------------------------------------------------
AGENT_HOST=127.0.0.1
Địa chỉ bind của server. Chỉ cho phép loopback để credentials GDT không lộ ra mạng LAN.

Cho phép: 127.0.0.1, ::1, localhost
Không được đặt 0.0.0.0 hay IP LAN — agent sẽ từ chối khởi động
Request từ ngoài localhost cũng bị chặn 403

-----------------------------------------------------------------------------------------
MAX_CONCURRENT=10
Số request tới API GDT chạy đồng thời tối đa (semaphore _gdt_sem).

Mặc định: 10
Hợp lệ: 1–10
Ảnh hưởng kéo danh sách và lấy chi tiết hóa đơn song song
Nếu gặp 429 từ GDT, giảm xuống 5–8

-----------------------------------------------------------------------------------------
GDT_PAGE_SIZE=30
Số hóa đơn lấy trong mỗi trang danh sách từ GDT.

Mặc định: 30
Hợp lệ: 1–50
Agent tự giảm page size nếu GDT đóng response giữa chừng
("incomplete chunked read").

-----------------------------------------------------------------------------------------
MAX_HTTP_CONCURRENT=10
Số request HTTP từ AMNote vào agent xử lý đồng thời tối đa (semaphore _http_sem trong middleware).

Mặc định: 10
Hợp lệ: 1–100
Bao gồm cả /health, /progress, các API fetch…

-----------------------------------------------------------------------------------------
HTTP_QUEUE_TIMEOUT=5
Thời gian (giây) chờ lấy slot HTTP khi agent đang đầy (MAX_HTTP_CONCURRENT).

Mặc định: 5
Phải > 0
Hết thời gian chờ → trả 503 "Agent đang quá tải, vui lòng thử lại" + header Retry-After: 2

-----------------------------------------------------------------------------------------
MAX_DETAIL_ITEMS=5000
Số hóa đơn tối đa trong body của POST /fetch-invoice-details.

Mặc định: 5000
Hợp lệ: 1–20000
Vượt quá → 413 (items vượt giới hạn)

-----------------------------------------------------------------------------------------
MAX_REQUEST_BODY_MB=16
Kích thước body JSON tối đa (MB) mà aiohttp chấp nhận.

Mặc định: 16
Hợp lệ: 1–50
Quan trọng khi gửi mảng items lớn lên /fetch-invoice-details

-----------------------------------------------------------------------------------------
GDT_CA_BUNDLE=
Đường dẫn file CA bundle (PEM) dùng khi HTTPS tới GDT.

Để trống → dùng CA hệ thống / certifi mặc định
Điền đường dẫn khi máy dùng proxy / chứng chỉ nội bộ (ví dụ: C:\certs\company-ca.pem)
