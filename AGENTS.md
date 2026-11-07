# 🌾 Nông Ký (Voice-to-Data) — Developer & Agent Guide

> **Mục tiêu**: Hướng dẫn toàn diện về kiến trúc hệ thống, các dịch vụ, quy tắc lập trình và ràng buộc tài nguyên cho các AI Agent và lập trình viên.

---

## 1. Tổng quan Kiến trúc Hệ thống

Dự án là hệ thống Edge AI Full-stack phục vụ ghi chép nhật ký nông nghiệp VietGAP bằng giọng nói và hình ảnh, vận hành trên bo mạch **ODROID-M2 (Rockchip RK3588 ARM64, 16GB RAM)**.

```
                           ┌─────────────────────────────┐
                           │   Trình duyệt / Thiết bị    │
                           │   (Laptop / Mobile / App)   │
                           └──────────────┬──────────────┘
                                          │
                  ┌───────────────────────┴───────────────────────┐
                  │ HTTPS :5001                                   │ HTTPS :5000
                  ▼                                               ▼
     ┌─────────────────────────┐                     ┌─────────────────────────┐
     │   Frontend React Native │                     │   Backend Flask App     │
     │      (Expo SDK 58)      │                     │     (Python 3.12)       │
     │  gluestack-ui + NW v5   │                     │  STT, OCR, RAG, SQLite  │
     │  Metro Reverse Proxy    │───[/api, /login]───▶│  MySQL (farm_diary)     │
     └─────────────────────────┘                     └────────────┬────────────┘
                                                                  │
                                                     ┌────────────┴────────────┐
                                                     ▼                         ▼
                                             RKLLM NPU Server          Realtime WebSocket
                                               (Port 8080)                 (Port 8765)
```

---

## 2. Thông tin Cổng Dịch vụ (Ports & Protocols)

| Dịch vụ | Cổng | Giao thức | Thư mục mã nguồn | Systemd Service |
| :--- | :--- | :--- | :--- | :--- |
| **Backend Flask** | **`5000`** | HTTPS (TLSv1.3) | `app/` | `nongky.service` |
| **Frontend React Native** | **`5001`** | HTTPS (TLSv1.3) | `frontend/` | `nongky-frontend.service` |
| **Local LLM Server (NPU)**| **`8080`** | HTTP | `rkllm_server/` | `rkllm.service` |
| **Realtime STT WebSocket**| **`8765`** | WSS (TLS/SSL) | `services/` | Tích hợp trong Flask |

---

## 3. Ràng buộc Phần cứng & Phân vùng Ổ đĩa (BẮT BUỘC TUÂN THỦ)

* **Phân vùng Root (`/`)**: Có dung lượng hạn chế (~17 GB). **KHÔNG ĐƯỢC** cài đặt các thư viện nặng, model AI hay npm cache trực tiếp lên phân vùng này.
* **Phân vùng Dữ liệu (`/mnt/data`)**: Có dung lượng lớn (**40 GB**).
  * Mã nguồn và `node_modules` của Frontend được lưu tại: `/mnt/data/voice_to_data_frontend` và tạo symlink về `/root/voice_to_data/frontend`.
  * Cache npm được ánh xạ về: `/mnt/data/.npm`.
  * Cache gluestack-ui được ánh xạ về: `/mnt/data/.gluestack`.
  * Model AI (GGUF, RKLLM, Onnx): Lưu trữ tại `/mnt/data/`.
* **Giới hạn File Watcher**: `fs.inotify.max_user_watches` phải luôn được đặt $\ge 524288$ để Metro Bundler hoạt động ổn định.

---

## 4. Reverse Proxy & Cơ chế Xác thực SSL

* Cả FE (5001) và BE (5000) đều sử dụng chung chứng chỉ SSL tại `app/certs/cert.pem` và `app/certs/key.pem`.
* **Cơ chế Reverse Proxy trong Metro**:
  * File `frontend/metro.config.js` đã được cấu hình middleware tự động chuyển tiếp các request `/api/*`, `/login`, `/logout`, `/uploads/*`, `/tmp/*` từ cổng `5001` sang Backend `https://127.0.0.1:5000`.
  * **Quy tắc cho code Frontend**: Khi gọi API từ Web, luôn dùng đường dẫn tương đối (ví dụ: `fetch('/login')` hoặc `fetch('/api/auth/me')`). Điều này giúp tránh hoàn toàn lỗi **CORS** và lỗi **Certificate Invalid** khi trình duyệt truy cập qua HTTPS.

---

## 5. Quy chuẩn Frontend (React Native + gluestack-ui + NativeWind)

* **Styling Engine**: Sử dụng **NativeWind v5 (Tailwind CSS v4)** kết hợp cùng **gluestack-ui v5**.
* **Thư viện UI**:
  * Các component nằm trong `frontend/components/ui/` (`Button`, `Badge`, `Box`, `Text`, `Input`...).
  * Thêm component mới bằng lệnh:
    ```bash
    cd /root/voice_to_data/frontend
    npx gluestack-ui add <component_name>
    ```
* **Bảng màu nhận diện VietGAP**:
  * Nền chính: Dark Forest `#141C15`
  * Nền Header: Deep Green `#1F3626`
  * Màu lá chủ đạo: `#3F6B3F` (Active: `#5B8C4A`)
  * Màu giấy thẻ: `#FBF8EF`
  * Màu vàng Gold: `#D6A339` (Chữ điểm nhấn *nông ký*)

---

## 6. Lệnh Vận hành Thường dùng

```bash
# Kiểm tra trạng thái toàn bộ dịch vụ
systemctl status nongky.service          # Backend Flask (Port 5000)
systemctl status nongky-frontend.service # Frontend React Native (Port 5001)

# Khởi động lại dịch vụ
systemctl restart nongky.service
systemctl restart nongky-frontend.service

# Xem log theo thời gian thực
journalctl -u nongky.service -f
journalctl -u nongky-frontend.service -f

# Chạy frontend ở chế độ tương tác (cho developer)
cd /root/voice_to_data/frontend
./run.sh
```
