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
