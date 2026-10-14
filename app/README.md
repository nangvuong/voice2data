# 🌾 Nông Ký — Voice to Text to Data (Flask App with HTTPS)

Ứng dụng ghi chép sổ nhật ký canh tác thông minh cho nông dân:
**Giọng nói (Voice) ➔ Văn bản (Text qua STT Offline) ➔ Dữ liệu có cấu trúc (Data qua Local LLM trên NPU) ➔ Chuẩn hoá CSDL (Normalizer)**

---

## 🏗️ Kiến trúc sử dụng trực tiếp các Engine từ thư mục `services/`

Ứng dụng **không tạo thêm các service con trùng lặp trong thư mục `/app`**, mà tái sử dụng 100% các engine cốt lõi của hệ thống từ thư mục [`services/`](file:///root/voice_to_data/services):
1. **`services.audio_converter` (`resample_to_16k_mono_pcm16`):**
   - Chuyển đổi file âm thanh ghi từ trình duyệt (.webm, .mp3, .wav, .m4a) sang định dạng 16kHz mono PCM16 bằng FFmpeg.
2. **`services.stt_engine` (`stt_engine`):**
   - Nhận diện giọng nói tiếng Việt bằng mô hình `sherpa-onnx` kết hợp cắt khoảng lặng `Silero VAD`.
3. **`services.llm_engine` (`llm_engine`):**
   - Kết nối tới Local RKLLM Server (trên NPU Rockchip RK3588 qua Port 8080) để bóc tách thông tin thành các trường nông nghiệp.
4. **`services.normalizer` (`normalize_activity_list`):**
   - Chuẩn hoá toàn diện:
     - Ngày tương đối (`"hôm nay"`, `"sáng nay"`, `"hôm qua"`) ➔ ngày `DD/MM/YYYY`.
     - Đơn vị đo lường (`"hai lít trên một ha"` ➔ `2 LÍT/HA`, `"năm tạ"` ➔ `5 TẠ/HA`).
     - Lọc phủ định sâu bệnh (`"không có sâu bệnh"` ➔ `null`).
     - Chuẩn hoá viết hoa danh từ vật tư nông nghiệp (`"phân n p k"` ➔ `PHÂN NPK`).

---

## 🔒 Chứng chỉ SSL / HTTPS (Bắt buộc cho Micro)

Để trình duyệt (Chrome, Safari trên điện thoại và máy tính) mở quyền **Microphone**, kết nối từ xa trong mạng LAN bắt buộc phải là ngữ cảnh an toàn (**HTTPS**).

Bộ chứng chỉ SSL đã được khởi tạo sẵn trong thư mục `certs/`:
- `certs/cert.pem` : Chứng chỉ SSL (SAN: `192.168.40.18`, `127.0.0.1`, `localhost`, `nongky.local`)
- `certs/key.pem` : Private key RSA 2048-bit

---

## 📁 Cấu trúc thư mục gọn gàng của `/app`

```
/root/voice_to_data/app/
├── app.py                # Máy chủ Flask trực tiếp gọi các engine trong services/
├── certs/                # Thư mục chứng chỉ SSL/TLS (HTTPS)
│   ├── cert.pem          # Chứng chỉ SSL X.509
│   ├── key.pem           # Private key
│   └── openssl.cnf       # Cấu hình OpenSSL SAN
├── templates/
│   └── index.html        # Giao diện Nông Ký Web UI
├── data/
│   └── logs.json         # CSDL lưu trữ các bản ghi nhật ký canh tác
├── uploads/              # Thư mục chứa các file âm thanh tạm thời
├── run.sh                # Script khởi chạy ứng dụng nhanh
└── README.md             # Tài liệu hướng dẫn
```

---

## 🚀 Hướng dẫn truy cập & sử dụng

### 1. Truy cập qua trình duyệt
```
https://192.168.40.18:5000
```
> **Thao tác khi mở lần đầu (Self-signed Cert):**
> - **Trên Chrome / Edge:** Bấm **"Nâng cao" (Advanced)** ➔ Chọn **"Tiếp tục truy cập 192.168.40.18 (không an toàn)"**.
> - **Trên Safari (iOS / Mac):** Bấm **"Hiển thị chi tiết" (Show Details)** ➔ Chọn **"Truy cập trang web này" (visit this website)**.
> - Sau đó bấm **"Cho phép" (Allow)** khi trình duyệt xin quyền Microphone.

### 2. Khởi chạy ứng dụng
```bash
cd /root/voice_to_data/app
./run.sh
```
