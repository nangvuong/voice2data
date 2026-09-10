# HƯỚNG DẪN SỬ DỤNG HỆ THỐNG NÔNG KÝ AI (VOICE TO DATA VIETGAP)

Tài liệu này cung cấp hướng dẫn nhanh từng bước để vận hành và sử dụng toàn bộ hệ thống **Nông Ký AI** trên thiết bị biên Rockchip RK3588.

---

## ⚡ 1. Khởi động nhanh hệ thống (Quick Start)

Hệ thống bao gồm 2 dịch vụ chính cần chạy:

### Bước 1: Khởi động Server AI (NPU RK3588)
Mở cửa sổ dòng lệnh Terminal và chạy:
```bash
cd /root/voice_to_data
./start_LLM.sh 2b
```
> [!TIP]
> - Mặc định sẽ nạp mô hình **Qwen3.5-2B W8A8** chạy trên 3 lõi NPU (tốc độ ~10-12s, độ chính xác 86.8%).
> - Nếu muốn chạy mô hình siêu nhẹ và nhanh hơn, dùng: `./start_LLM.sh 0.6b` (tốc độ ~4.4s).

### Bước 2: Khởi động Giao diện Web App (HTTPS)
Mở một cửa sổ Terminal khác và chạy:
```bash
cd /root/voice_to_data/app
./run.sh
```

---

## 📱 2. Truy cập và Thu âm trên Trình duyệt

1. **Mở trình duyệt** trên điện thoại thông minh hoặc máy tính cùng mạng Wi-Fi:
   - Truy cập: `https://<IP_THIẾT_BỊ>:5000` (Ví dụ: `https://192.168.40.5:5000` hoặc `https://localhost:5000`).
   - Bấm **Nâng cao (Advanced)** $\rightarrow$ **Tiếp tục truy cập (Proceed)** để bỏ qua cảnh báo chứng chỉ SSL tự ký.

2. **Đăng nhập**:
   - Tài khoản nông dân: `nongdan1` / Mật khẩu: `123456`
   - Tài khoản quản trị: `admin` / Mật khẩu: `admin123`

3. **Ghi chép bằng giọng nói**:
   - Chọn loại công việc (hoặc để mặc định *Tự động nhận diện*).
   - Nhấn nút **Microphone màu đỏ** và cấp quyền thu âm cho trình duyệt.
   - Nói câu nhật ký rõ ràng.
   - Nhấn **Dừng thu âm**.
   - Kiểm tra kết quả bóc tách hiển thị và bấm **Xác nhận lưu vào CSDL**.

---

## 📊 3. Xem nhật ký & Xuất biểu mẫu VietGAP

- **Tab Dòng thời gian (Timeline)**: Xem lịch sử công việc theo ngày, bấm nút Play để nghe lại đoạn ghi âm thực tế của nông dân.
- **Tab Biểu mẫu VietGAP**: Tra cứu dữ liệu đã được tự động điền vào:
  - Bảng 1.5: Mua giống, phân bón, thuốc BVTV
  - Bảng 1.6: Sử dụng phân bón, thuốc BVTV
  - Bảng 1.7: Xử lý vỏ bao bì thuốc
  - Bảng 1.8: Thu hoạch nông sản
  - Bảng 1.9: Xuất bán sản phẩm

---

## 🧪 4. Công cụ Benchmark & Kiểm thử

Chạy script so sánh hiệu năng giữa Gemini, RKLLM 2B và RKLLM 0.6B trên 10 câu thực tế:
```bash
cd /root/voice_to_data
# Đo Gemini
.venv/bin/python benchmark_10_sentences.py gemini

# Đo RKLLM 2B
.venv/bin/python benchmark_10_sentences.py 2b

# Đo RKLLM 0.6B
.venv/bin/python benchmark_10_sentences.py 0.6b
```

---

## 🛠️ 5. Lệnh hữu ích khi cần bảo trì

- **Giải phóng cổng mạng khi bị treo**:
  ```bash
  fuser -k 8080/tcp 2>/dev/null || true
  fuser -k 5000/tcp 2>/dev/null || true
  ```
- **Xoá trắng dữ liệu thử nghiệm trong CSDL**:
  ```bash
  .venv/bin/python /root/.gemini/antigravity-cli/brain/b9c20c1d-3f0d-4248-9988-2f9ef0fdea9a/scratch/clear_activities.py
  ```
- **Chạy Telegram Bot (tuỳ chọn)**:
  ```bash
  .venv/bin/python /root/voice_to_data/main.py
  ```
