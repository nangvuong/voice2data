# TỔNG QUAN KIẾN TRÚC HỆ THỐNG & CƠ CHẾ HOẠT ĐỘNG
**Dự án:** Nông Ký (Voice to Text to Data VietGAP)  
**Nền tảng mục tiêu:** Máy tính nhúng ARM (Odroid M2 / Rockchip RK3588 NPU) & Thiết bị di động  
**Cập nhật lần cuối:** 28/09/2026  

---

## 1. TỔNG QUAN DỰ ÁN

Hệ thống được thiết kế theo mô hình **Edge AI Micro-monolith** (kết hợp dịch vụ web monolith và các engine AI biên), phục vụ mục đích ghi chép nhật ký canh tác nông nghiệp theo chuẩn VietGAP bằng giọng nói tiếng Việt.

Hệ thống có khả năng chạy **Offline 100%** trực tiếp tại trang trại (không cần Internet), đồng thời hỗ trợ chế độ kết nối Cloud linh hoạt khi cần độ chính xác mở rộng.

---

## 2. KIẾN TRÚC PHÂN TẦNG (ARCHITECTURE LAYERS)

```
┌────────────────────────────────────────────────────────────────────────┐
│ 1. TẦNG CLIENT (TRÌNH DUYỆT WEB / MOBILE)                              │
│  - Giao diện: HTML5 / CSS / Vanilla JavaScript thuần (Jinja2)          │
│  - Thu âm & Xử lý tín hiệu: Web Audio API (Downsample 16kHz PCM16)     │
│  - Giao thức: WebSocket WSS (Ưu tiên) + Fetch REST API (Dự phòng)      │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                         (WSS:8765 / HTTPS:5001)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 2. TẦNG MÁY CHỦ WEB & GATEWAY (BACKEND)                                │
│  - Flask Web Server (:5001 - HTTPS) - Quản lý qua nongky.service        │
│  - Realtime STT WebSocket Server (:8765 - WSS) - Daemon Thread         │
│  - Adminer Database Manager (:8088 - HTTP) - adminer.service           │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 3. TẦNG DỊCH VỤ XỬ LÝ LÕI (SERVICES LAYER)                            │
│  - services/realtime_stt.py  : Streaming STT Session & VAD Windowing   │
│  - services/stt_engine.py    : Sherpa-ONNX Task Queue & Batch STT      │
│  - services/ocr_engine.py    : RapidOCR Engine (Nhận diện hóa đơn/bao bì)│
│  - services/tts_engine.py    : Piper/Sherpa Text-to-Speech Tiếng Việt  │
│  - services/audio_converter.py: Chuẩn hoá audio bằng FFmpeg            │
│  - services/llm_engine.py    : Gọi Local Qwen3.5 trên NPU (:8080)     │
│  - services/gemini_engine.py : Tùy chọn Cloud LLM (Gemini 3.5 Flash)   │
│  - services/normalizer/      : Chuẩn hoá số, đơn vị, ngày, bảng CSDL   │
│  - services/database.py      : Tầng kết nối & CRUD MySQL               │
│  - services/exporters/       : Xuất báo cáo VietGAP (Excel, Word, PDF) │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 4. TẦNG TRỌNG SỐ AI & DỮ LIỆU LƯU TRỮ                                  │
│  - NPU RK3588 : Qwen3.5-2B_RK3588_w8a8.rkllm (Port 8080)               │
│  - CPU ARM    : Sherpa-ONNX Zipformer (encoder/decoder/joiner INT8)    │
│  - MySQL 8.0  : Database 'vietgap_diary' (Port 3306)                   │
│  - Storage    : /mnt/data/audio/*.wav (Bản ghi âm gốc)                 │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 3. CƠ CHẾ NHẬN DẠNG GIỌNG NÓI THỜI GIAN THỰC (REALTIME STT)

### 3.1. Pipeline xử lý âm thanh
1. **Thu âm & Resample tại Client:**
   * Browser dùng `Web Audio API` (`AudioContext`, `createMediaStreamSource`).
   * Tín hiệu được chuyển đổi mẫu (downsample) sang chuẩn **16,000 Hz, mono, PCM 16-bit** ngay tại trình duyệt.
2. **Streaming Chunk:**
   * Cứ mỗi ~200ms - 250ms (4096 mẫu), client gửi gói nhị phân (Binary Buffer) qua kết nối WebSocket `wss://<host>:8765`.
3. **Voice Activity Detection (Silero VAD):**
   * Server chia âm thanh thành từng cửa sổ nhỏ **512 mẫu (32ms)** nạp vào `sherpa_onnx.VoiceActivityDetector`.
   * Phát hiện khoảng nói (`threshold=0.5`) và khoảng lặng (`min_silence_sec=0.35s`).
4. **Phát sinh sự kiện (Events):**
   * `ready`: Khi kết nối WebSocket thành công và tạo session.
   * `partial`: Khi người dùng đang nói và đoạn âm thanh đạt chu kỳ (~250ms) -> Trả về văn bản tạm thời để hiển thị tức thì.
   * `final`: Khi người dùng dừng nói quá 0.35s (VAD Endpoint) -> Chốt câu văn bản hoàn chỉnh.
   * `complete`: Khi kết thúc ghi âm (`{ "action": "finish" }`) -> Xả toàn bộ buffer VAD và tổng hợp toàn văn.

### 3.2. Cơ chế dự phòng 3 lớp (3-Tier Fallback)
* **Tầng 1 (Ưu tiên):** WebSocket WSS (`wss://:8765`) - Độ trễ tối thiểu, truyền nhị phân nhẹ không tốn HTTP Header.
* **Tầng 2 (Dự phòng):** HTTP REST Streaming (`POST /api/stt/realtime/feed`) - Dành cho môi trường mạng chặn WebSocket.
* **Tầng 3 (Dự phòng sự cố):** MediaRecorder Upload (`POST /api/stt`) - Ghi âm toàn bộ file rồi upload xử lý trọn gói.

---

## 4. TỔ CHỨC MÃ NGUỒN FRONTEND & BACKEND

### 4.1. Phía Frontend (HTML thuần)
* **Đặc điểm:** Không dùng Framework nặng (React, Vue, Node.js), không cần build step (Webpack/Vite). Toàn bộ HTML/CSS/JS chạy trực tiếp từ Jinja2 templates:
  * `app/templates/index.html`: Dashboard chính, ghi âm realtime, đối soát AI, bảng nhật ký.
  * `app/templates/manager.html`: Quản lý trang trại, thống kê mùa vụ, lô thửa.
  * `app/templates/diary.html`: Sổ nhật ký điện tử dạng bảng chuẩn VietGAP.
  * `app/templates/add_activity.html`: Giao diện tối ưu hoá cho điện thoại di động ngoài đồng ruộng.
* **Ưu điểm:** Cực kỳ nhẹ, tiết kiệm RAM cho máy tính nhúng, sửa code có tác dụng ngay lập tức.

### 4.2. Phía Backend (Python Flask)
* Chạy thông qua dịch vụ hệ thống `nongky.service`:
  ```bash
  /root/voice_to_data/.venv/bin/python /root/voice_to_data/app/app.py --port 5001 --ssl
  ```
* Tích hợp sẵn SSL tự cấp trong `app/certs` để kích hoạt giao thức HTTPS bắt buộc cho Microphone trên trình duyệt.
* Tự động khởi chạy WebSocket Server ngầm trong tiến trình Flask, chia sẻ chung 1 mô hình `sherpa-onnx` nạp trong RAM.

---

## 5. CƠ CHẾ LƯU VẾT & KIỂM TOÁN (AUDIT TRAIL & LOGGING)

### 5.1. Lưu vết hoạt động của AI (Trong Cơ sở dữ liệu)
Hệ thống lưu lại mọi thao tác AI đã làm vào MySQL `vietgap_diary`:
1. **Bảng `ai_extraction_records`:**
   * `raw_transcript`: Văn bản thô ban đầu người dùng nói.
   * `ai_json_result`: Toàn bộ JSON mà AI đã phân tích (loại công việc, tên thuốc/phân bón, liều lượng, diện tích...).
   * `time_process`: Thời gian mô hình AI suy luận (giây).
   * `extraction_status`: Trạng thái duyệt (`APPLIED`, `PENDING`, `REJECTED`, `NEEDS_REVIEW`).
   * `reviewed_by` & `reviewed_at`: Người duyệt và thời điểm xác nhận lưu vào sổ nhật ký.
   * `activity_id` & `voice_id`: Liên kết tới hoạt động và bản ghi âm tương ứng.
2. **Bảng `activity_voice_records`:**
   * Lưu đường dẫn file âm thanh `.wav` tại `/mnt/data/audio/`.
   * Lưu thời lượng âm thanh và kết quả STT thô để đối chứng thực địa.

### 5.2. File Log Ứng dụng
* File `app/app.log`: Ghi lại chi tiết thời gian gọi API trích xuất, engine được dùng (Local RKLLM hay Gemini Cloud), cảnh báo JSON hoặc lỗi nghiệp vụ.

### 5.3. Nhật ký phát triển cùng Trợ lý AI (Gemini / Antigravity)
* Mọi phiên làm việc phát triển tính năng đều được ghi lại đầy đủ vào thư mục:
  `/root/.gemini/antigravity-cli/brain/<session-id>/.system_generated/logs/transcript.jsonl`
* Ghi lại chi tiết từng câu lệnh shell đã chạy, từng file đã chỉnh sửa cùng toàn bộ code diff trước và sau.

---

## 6. DANH MỤC DỊCH VỤ, CỔNG (PORTS) & THÔNG TIN TRUY CẬP

| Dịch vụ | Địa chỉ truy cập | Giao thức | Mô tả / Thông tin đăng nhập |
| :--- | :--- | :--- | :--- |
| **Ứng dụng Web chính** | `https://192.168.40.5:5001` | HTTPS | Giao diện ghi nhật ký canh tác bằng giọng nói |
| **Realtime STT WebSocket** | `wss://192.168.40.5:8765` | WSS | Kênh streaming âm thanh nhận diện tức thì |
| **Quản trị CSDL (Adminer)** | `http://192.168.40.5:8088` | HTTP | Quản lý bảng dữ liệu MySQL trực quan qua Web |
| **Local LLM Server (NPU)** | `http://192.168.40.5:8080` | HTTP | OpenAI-compatible API chạy model Qwen3.5 trên RK3588 |
| **Cơ sở dữ liệu MySQL** | `192.168.40.5:3306` | TCP/Socket | **User:** `farm_user` \| **Pass:** `farm_password`<br/>**Database:** `vietgap_diary` |

---

## 7. CÁC LỆNH QUẢN TRỊ DỊCH VỤ THƯỜNG DÙNG

```bash
# Quản lý Web App & WebSocket STT:
systemctl status nongky.service
systemctl restart nongky.service
journalctl -u nongky.service -f

# Quản lý dịch vụ AI Local trên NPU:
systemctl status rkllm.service
systemctl restart rkllm.service

# Quản lý giao diện Adminer:
systemctl status adminer.service
systemctl restart adminer.service

# Kiểm tra các cổng đang lắng nghe:
ss -tulpn | grep -E "5001|8765|8088|8080|3306"
```
