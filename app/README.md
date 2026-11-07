# 🌾 Nông Ký — Backend Flask (Voice to Text to Data)

Hệ thống Edge AI Backend phục vụ ghi chép nhật ký nông nghiệp VietGAP bằng giọng nói và hình ảnh, vận hành trên bo mạch **ODROID-M2 (Rockchip RK3588 ARM64)**.

---

## 📁 Cấu trúc thư mục `/app`

```
app/
├── app.py                # Entrypoint khởi chạy Flask Server & Realtime STT WebSocket
├── common.py             # Cấu hình đường dẫn (/mnt/data/...), logging, run_async, normalizer
├── run.sh                # Script khởi chạy ứng dụng
├── certs/                # Chứng chỉ SSL/TLS dùng chung cho cả Backend (5000) & Frontend (5001)
│   ├── cert.pem          # Chứng chỉ SSL X.509
│   └── key.pem           # Private key
└── api/                  # Toàn bộ các module API theo chức năng (cấu trúc phẳng)
    ├── __init__.py       # Đăng ký tập trung Blueprints & phục vụ media (/static, /uploads, /tmp, /api/health)
    ├── auth.py           # Xác thực nông hộ (/login, /logout, /api/auth/me)
    ├── conversation.py   # Trợ lý hội thoại AI & TTS (/api/conversation/*)
    ├── stt.py            # Quản lý STT, Hotwords, Records, STT Lab & Realtime (/api/stt*)
    ├── ai.py             # Nhận diện OCR, VLM, Trích xuất VietGAP & Voice-to-Data
    ├── diary.py          # Sổ nhật ký canh tác VietGAP, danh mục, lô thửa GPS
    └── export.py         # Xuất báo cáo (Excel, Word, PDF), mùa vụ & QR Truy xuất nguồn gốc
```

> **Lưu trữ dữ liệu Media**: Toàn bộ tệp âm thanh (`/mnt/data/audio`), hình ảnh thực địa (`/mnt/data/photos`) và tệp tải lên tạm thời (`/mnt/data/uploads`) được lưu trực tiếp trên phân vùng dữ liệu lớn `/mnt/data`.
