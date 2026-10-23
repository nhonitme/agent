# Build AMConnectGDT cho Windows 7, 10 và 11

Bản tương thích dùng Python 3.8.10 x64 và PyInstaller 5.13.2. Không build bản
phát hành này bằng Python 3.9 trở lên vì Python mới đã bỏ hỗ trợ Windows 7.

## Build

Chạy:

```bat
build_agent_win7_11.bat
```

Kết quả:

```text
dist\win7-win11-x64\AMConnectGDT.exe
```

Thư mục phát hành cần có:

```text
AMConnectGDT.exe
config.env
```

Sao chép `config.env.example` thành `config.env` trước khi chạy.

## Phạm vi tương thích

- Windows 7 SP1 64-bit: mục tiêu tương thích, cần kiểm tra trên máy thật hoặc VM.
- Windows 10 64-bit: đã chạy smoke test `/health`.
- Windows 11 64-bit: dùng cùng executable x64.

Máy Windows 7 32-bit không chạy được bản x64. Trường hợp đó phải cài Python
3.8.10 x86, tạo virtual environment x86 và build thêm một executable riêng.

Windows 7 nên được cài đầy đủ bản cập nhật hệ thống, Universal C Runtime, chứng
chỉ gốc và TLS 1.2 để kết nối HTTPS tới GDT.

## Thông báo nguồn bị bỏ qua cho client (v1.0.9)

- `/fetch-invoice-list` và `/fetch-all-invoice` trả thêm `skipped_sources`: mỗi phần tử có `source`, `source_name`, `type_search`, `reason_code`, `message` và `page` nếu lỗi phản hồi tại một trang. Mảng rỗng nếu không bỏ qua nguồn nào.
- Trường `message` của phản hồi tổng hợp thông báo để client hiển thị, bao gồm: “Nếu quý khách hàng không xuất loại hóa đơn này thì có thể bỏ qua”. Cảnh báo áp dụng cả nguồn bị bỏ qua do ngoại lệ khác hoặc dừng giữa chừng khi GDT trả lỗi; các trang đã lấy thành công trong trường hợp sau vẫn được giữ lại.
- Giữ nguyên quy tắc HTTP/`success` hiện tại: bỏ qua nguồn không tự đổi kết quả thành lỗi; client cần đọc `skipped_sources` để biết danh sách chưa đầy đủ. Lỗi chi tiết vẫn trả HTTP 207 như trước.

## Bỏ qua nguồn máy tính tiền lỗi kết nối (v1.0.9)

- Timeout HTTP mặc định là 10 giây. API danh sách vẫn thử tối đa 3 lần cho mỗi trang; đây không phải giới hạn tổng thời gian quét một nguồn.
- Khi nguồn máy tính tiền (`sco-query`) gặp `_GDTTransportError` sau 3 lần thử một trang, agent ghi log và bỏ qua nguồn đó để tiếp tục tiến trình.
- Áp dụng cho `/fetch-invoice-list` và bước lấy danh sách của `/fetch-all-invoice`; giữ kết quả các nguồn khác đã lấy thành công.
- Tổng hóa đơn trả về không bao gồm nguồn máy tính tiền bị bỏ qua. Nguồn thông thường (`query`) vẫn báo lỗi khi hết số lần thử kết nối.

## Sửa lỗi tải detail chậm / HTTP 429 (từ v1.0.8)

- Mặc định tải 3 detail song song (`DETAIL_WORKERS=3`) và thử mỗi detail tối đa 2 lần.
- Timeout mỗi request detail mặc định 25 giây; số lần thử này **không** áp dụng cho API danh sách/XML.
- Khi GDT trả 429, các worker dùng chung một cooldown 10 giây và tạm giảm về 1 detail đồng thời. Sau 10 detail thành công liên tiếp, agent khôi phục mức 3 worker.
- Khi gặp 4 lỗi 429 liên tiếp **hoặc** 3 detail liên tiếp lỗi kết nối, agent dừng lấy thêm và trả các hóa đơn chưa tải dưới dạng partial.
- Giới hạn tổng thời gian mỗi batch mặc định là 3600 giây; chỉnh `DETAIL_BATCH_TIMEOUT` khi cần.

**Lưu ý tích hợp AMNote:** `/fetch-invoice-details` và `/fetch-all-invoice`
vẫn trả HTTP **207** và `{"success": false, "partial": true, ...}` khi có lỗi.
Không được bỏ qua `items` chỉ vì `success=false`: các hóa đơn thành công
vẫn chứa `detail`, hóa đơn lỗi chứa `detail=null` và
`detail_error_code` (`DETAIL_FETCH_FAILED`, `GDT_RATE_LIMITED`,
`GDT_UNAVAILABLE` hoặc `DETAIL_BATCH_TIMEOUT`).
Có thể gọi lại `/fetch-invoice-details` **chỉ với các phần tử lỗi** sau khi
GDT phục hồi, tránh tải lại toàn bộ.

**Triển khai:** Cần build lại `AMConnectGDT.exe` bằng script trên và thay
file EXE đang sử dụng. Commit source không tự nâng cấp file EXE đã cài.
Không dùng EXE/version.json cũ trong thư mục dist làm bằng chứng rằng
bản 1.0.8 đã được triển khai. Nên sao lưu EXE cũ trước khi thay thế.

**Kiểm thử source:** `python -m unittest discover -s tests -p "test_*.py" -v`
(chạy từ thư mục `agent`, yêu cầu cài `aiohttp` và `curl_cffi`).
Đã bổ sung GitHub Actions kiểm tra cú pháp và logic không cần kết nối GDT
trên Python 3.8 và 3.11. Vẫn cần xác nhận thực tế với tài khoản GDT hợp lệ.
