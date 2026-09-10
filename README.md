# NÔNG KÝ AI - VOICE TO DATA VIETGAP
> **Hệ thống ghi nhận và số hoá nhật ký canh tác nông nghiệp VietGAP bằng giọng nói trên thiết bị biên (Edge AI - Rockchip RK3588 NPU & Cloud Gemini)**

---

## 📖 MỤC LỤC
1. [Tổng quan dự án](#1-tổng-quan-dự-án)
2. [Kiến trúc hệ thống & Luồng dữ liệu](#2-kiến-trúc-hệ-thống--luồng-dữ-liệu)
3. [Cấu trúc thư mục dự án](#3-cấu-trúc-thư-mục-dự-án)
4. [Yêu cầu phần cứng & Cài đặt môi trường](#4-yêu-cầu-phần-cứng--cài-đặt-môi-trường)
5. [Cấu hình hệ thống (.env)](#5-cấu-hình-hệ-thống-env)
6. [Hướng dẫn khởi chạy & Vận hành](#6-hướng-dẫn-khởi-chạy--vận-hành)
   - [6.1. Khởi chạy Local LLM Server trên NPU](#61-khởi-chạy-local-llm-server-trên-npu)
   - [6.2. Khởi chạy Web App Nông Ký (HTTPS Cổng 5000)](#62-khởi-chạy-web-app-nông-ký-https-cổng-5000)
   - [6.3. Khởi chạy Telegram Bot](#63-khởi-chạy-telegram-bot)
7. [Hướng dẫn sử dụng giao diện Web](#7-hướng-dẫn-sử-dụng-giao-diện-web)
8. [Chuẩn hoá biểu mẫu VietGAP & Cơ sở dữ liệu](#8-chuẩn-hoá-biểu-mẫu-vietgap--cơ-sở-dữ-liệu)
9. [Bộ công cụ Benchmark & Đánh giá mô hình](#9-bộ-công-cụ-benchmark--đánh-giá-mô-hình)
10. [Xử lý sự cố thường gặp (Troubleshooting)](#10-xử-lý-sự-cố-thường-gặp-troubleshooting)

---

## 1. TỔNG QUAN DỰ ÁN

Việc ghi chép nhật ký canh tác thủ công bằng sổ tay của nông dân gặp nhiều trở ngại: tốn thời gian, dễ quên, chữ viết khó đọc và khó tổng hợp để phục vụ cấp chứng nhận VietGAP/GlobalGAP.

**Nông Ký AI (Voice to Data)** là giải pháp công nghệ toàn diện:
- **Nhập liệu rảnh tay**: Nông dân chỉ cần bấm nút và nói tự nhiên (hoặc gửi tin nhắn thoại qua Telegram).
- **Hoạt động Offline 100% tại hiện trường**: Tích hợp mô hình nhận dạng giọng nói tiếng Việt (ASR) và mô hình ngôn ngữ lớn (LLM) chạy trực tiếp trên chip xử lý thần kinh **NPU Rockchip RK3588** (Orange Pi 5 Plus / Odroid M2), không cần Internet vẫn bóc tách và lưu trữ dữ liệu an toàn.
- **Hỗ trợ chế độ Hybrid Cloud**: Linh hoạt chuyển đổi sang Google Gemini API (gemini-3.5-flash-lite) khi có mạng để tăng tốc độ phản hồi chỉ còn ~1 giây.
- **Tự động điền 5 biểu mẫu chuẩn VietGAP**:
  - *Bảng 1.5*: Sổ theo dõi mua (sản xuất) vật tư nông nghiệp.
  - *Bảng 1.6*: Sổ theo dõi sử dụng phân bón, thuốc BVTV & hoá chất.
  - *Bảng 1.7*: Sổ theo dõi xử lý bao bì, thuốc BVTV dư thừa.
  - *Bảng 1.8*: Sổ theo dõi thu hoạch nông sản.
  - *Bảng 1.9*: Sổ theo dõi tiêu thụ / xuất bán sản phẩm.
  - *Bảng tổng hợp*: Nhật ký công việc canh tác chung (làm cỏ, tưới nước, tỉa cành, làm đất, dặm cây...).

---

## 2. KIẾN TRÚC HỆ THỐNG & LUỒNG DỮ LIỆU

```
  ┌────────────────────────────────────────────────────────┐
  │                 ĐẦU VÀO CỦA NÔNG DÂN                   │
  │  - Giọng nói Web App (Microphone WebRTC qua HTTPS)    │
  │  - File ghi âm / Tin nhắn thoại Telegram Bot (.ogg)   │
  └──────────────────────────┬─────────────────────────────┘
                             │
                             ▼
  ┌────────────────────────────────────────────────────────┐
  │ 1. CHUẨN HOÁ ÂM THANH & CẮT ĐOẠN IM LẶNG               │
  │  - FFmpeg: Mono, 16kHz, 16-bit PCM WAV                 │
  │  - Silero VAD: Phát hiện giọng nói, cắt bỏ khoảng lặng │
  └──────────────────────────┬─────────────────────────────┘
                             │
                             ▼
  ┌────────────────────────────────────────────────────────┐
  │ 2. NHẬN DẠNG GIỌNG NÓI TIẾNG VIỆT OFFLINE (STT)        │
  │  - Engine: Sherpa-ONNX (Zipformer / Transducer)        │
  │  - Đầu ra: Văn bản thô không dấu chấm phẩy (Raw Text)  │
  └──────────────────────────┬─────────────────────────────┘
                             │
                             ▼
  ┌────────────────────────────────────────────────────────┐
  │ 3. BÓC TÁCH NGỮ NGHĨA BẰNG LLM (CHẾ ĐỘ KÉP)            │
  │  [Chế độ 1: Offline NPU RK3588 (Khuyên dùng)]          │
  │    - RKLLM Server (Cổng 8080, W8A8): Qwen3.5-2B / 0.6B │
  │  [Chế độ 2: Cloud API (Khi có Internet)]               │
  │    - Google Gemini 3.5 Flash-Lite (Structured JSON)    │
  └──────────────────────────┬─────────────────────────────┘
                             │
                             ▼
  ┌────────────────────────────────────────────────────────┐
  │ 4. BỘ CHUẨN HOÁ LUẬT HỌC VIETGAP (NORMALIZER)          │
  │  - Quy đổi ngày tháng STT: "hai không hai lăm" -> 2025 │
  │  - Quy đổi số lượng tiếng Việt: "năm mươi ngàn" -> 50000│
  │  - Chuẩn hoá danh mục: phân bón, thuốc, sâu bệnh, cây  │
  │  - Safe Guard: chống ảo giác và lộn ngược nghĩa sâu hại│
  └──────────────────────────┬─────────────────────────────┘
                             │
                             ▼
  ┌────────────────────────────────────────────────────────┐
  │ 5. LƯU TRỮ CSDL & HIỂN THỊ TRÊN GIAO DIỆN               │
  │  - MySQL Local (`vietgap_diary`): 13 bảng liên kết    │
  │  - Web App UI: Dòng thời gian, biểu mẫu VietGAP PDF/CSV│
  │  - Telegram Bot: Phản hồi thẻ thông tin nông nghiệp    │
  └────────────────────────────────────────────────────────┘
```

---

## 3. CẤU TRÚC THƯ MỤC DỰ ÁN

```
voice_to_data/
├── app/                              # Ứng dụng Web Flask giao diện nông dân & API
│   ├── app.py                        # Entrypoint Flask Server (hỗ trợ REST API, WebSocket & HTTPS)
│   ├── run.sh                        # Script khởi chạy web app cổng 5001 kèm SSL
│   ├── certs/                        # Chứng chỉ SSL tự ký cho HTTPS
│   ├── templates/                    # Giao diện Jinja2 HTML (index, conversation, diary, manager...)
│   └── static/                       # File CSS, JavaScript, ảnh, font & audio
│
├── cli/                              # Bộ công cụ dòng lệnh (Command-Line Interface)
│   ├── conversation_cli.py           # CLI hội thoại đa lượt tương tác (Conversation State)
│   ├── ocr_cli.py                    # CLI nhận diện chữ viết hóa đơn/bao bì (RapidOCR/VietOCR)
│   ├── rag_chat.py                   # CLI hỏi đáp kỹ thuật canh tác VietGAP (RAG Q&A)
│   ├── rag_eval.py                   # Script đánh giá định lượng retrieval (Recall@K, MRR)
│   ├── tts_cli.py                    # CLI đọc văn bản thành giọng nói tiếng Việt
│   └── vlm_cli.py                    # CLI thị giác máy tính nhận diện bệnh/vật tư qua ảnh
│
├── conversation/                     # Module quản lý hội thoại đa lượt (Conversation State Machine)
│   ├── state.py                      # Định nghĩa trạng thái & bộ nhớ phiên hội thoại
│   ├── manager.py                    # Quản lý luồng xử lý văn bản, ảnh, âm thanh đa lượt
│   ├── gemini_helper.py              # Xử lý NLU / gợi ý phản hồi thông minh
│   └── templates.py                  # Các mẫu kịch bản hội thoại VietGAP
│
├── data/                             # Dữ liệu & Tài liệu tham chiếu chuẩn hóa
│   ├── rag/                          # Tập dữ liệu tri thức RAG
│   │   ├── rag_retrieval.jsonl       # 428 tri thức (Quy trình bưởi, 32 sản phẩm, quy chuẩn)
│   │   └── rag_eval.jsonl            # 114 câu hỏi đánh giá benchmark retrieval
│   ├── reference/                    # Tài liệu tham chiếu vật tư & VietGAP
│   │   ├── tat_ca_san_pham.json      # 32 sản phẩm phân bón & thuốc BVTV đầy đủ
│   │   ├── bieu_mau_thong_ke_rag.csv # Bảng đối soát vật tư, liều lượng, cách dùng
│   │   ├── rag_knowledge_base.md     # Tài liệu tra cứu tri thức VietGAP chi tiết
│   │   └── insert_vietgap_diary.sql  # SQL seed dữ liệu danh mục vật tư
│   ├── conversation_dataset/         # Dữ liệu mẫu kiểm thử phiên hội thoại
│   └── schema.sql                    # Cấu trúc CSDL MySQL vietgap_diary
│
├── deploy/                           # Cấu hình triển khai hệ thống (Systemd & Startup)
│   ├── systemd/                      # File cấu hình systemd service (nongky, rkllm, tts...)
│   └── scripts/                      # Script khởi động dịch vụ (start_LLM.sh, start_tts.sh)
│
├── models/                           # Trọng số mô hình AI Offline (STT Zipformer, VietOCR, VAD)
├── rag_index/                        # Bộ nhớ đệm Vector Index (vectors.npy, metadata.json)
├── results/                          # Kết quả đo đạc benchmark & evaluation
├── rkllm_server/                     # OpenAI-compatible API server chạy NPU RK3588 (Cores 0-2)
├── services/                         # Tầng dịch vụ lõi (STT, TTS, OCR, VLM, RAG, Normalizer, DB)
├── tests/                            # Bộ kiểm thử tự động (Unit tests & Integration tests)
├── tools/                            # Công cụ quản trị & Benchmark
│   ├── adminer.php                   # Giao diện quản trị CSDL MySQL trên web (:8088)
│   └── benchmark/                    # Các kịch bản benchmark OCR, phân loại, trích xuất
│
├── config.py                         # Cấu hình tập trung từ biến môi trường (.env)
├── requirements.txt                  # Danh sách thư viện Python
└── README.md                         # Tài liệu hướng dẫn sử dụng toàn diện
```

---

## 4. YÊU CẦU PHẦN CỨNG & CÀI ĐẶT MÔI TRƯỜNG

### 4.1. Phần cứng khuyến nghị
- **Bo mạch thiết bị biên**: Orange Pi 5 Plus / Odroid M2 / Khadas Edge 2 (Chip SoC **Rockchip RK3588 / RK3588S**).
- **NPU**: 3 Cores NPU tích hợp, tổng hiệu năng 6 TOPS.
- **RAM**: Tối thiểu 8GB (khuyên dùng 16GB).
- **Bộ nhớ lưu trữ**: eMMC / NVMe SSD từ 64GB trở lên (Mount tại `/mnt/data`).
- **Hệ điều hành**: Ubuntu 22.04 LTS hoặc Ubuntu 24.04 LTS (Linux aarch64).

### 4.2. Cài đặt các gói phụ thuộc hệ thống
```bash
sudo apt update && sudo apt install -y \
    ffmpeg \
    mysql-server \
    libmysqlclient-dev \
    git \
    curl \
    psmisc
```

### 4.3. Cài đặt môi trường Python ảo
Dự án sử dụng Python 3.10+ (đã kiểm thử tương thích tốt nhất trên Python 3.12).
```bash
cd /root/voice_to_data
python3 -m venv .venv
source .venv/bin/activate

# Cài đặt các thư viện cần thiết
pip install --upgrade pip
pip install \
    flask \
    aiohttp \
    pymysql \
    python-dotenv \
    python-telegram-bot \
    numpy \
    onnxruntime \
    sherpa-onnx \
    soundfile \
    werkzeug
```

### 4.4. Cấu hình CSDL MySQL
Khởi tạo database `vietgap_diary` và tài khoản kết nối:
```sql
CREATE DATABASE IF NOT EXISTS vietgap_diary CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'farm_user'@'localhost' IDENTIFIED BY 'farm_password';
GRANT ALL PRIVILEGES ON vietgap_diary.* TO 'farm_user'@'localhost';
FLUSH PRIVILEGES;
```
*Ghi chú*: Khi ứng dụng khởi động lần đầu, hàm `init_db_and_seed()` trong `services/database.py` sẽ tự động tạo bảng và nạp dữ liệu mẫu ban đầu (nông hộ, lô đất A1/B2, mùa vụ 2026, danh mục phân bón/thuốc BVTV).

---

## 5. CẤU HÌNH HỆ THỐNG (.env)

Tạo hoặc chỉnh sửa file `.env` tại thư mục gốc `/root/voice_to_data/.env`:

```ini
# ==============================================================================
# CẤU HÌNH AGRILOG AI - VOICE TO DATA
# ==============================================================================

# 1. Telegram Bot (Lấy từ @BotFather nếu dùng Telegram)
TELEGRAM_BOT_TOKEN="your_telegram_bot_token_here"

# 2. Local AI Server (RKLLM Server trên NPU hoặc Llama.cpp trên CPU)
LOCAL_AI_BASE_URL="http://127.0.0.1:8080"
LOCAL_AI_ENDPOINT="/v1/chat/completions"
LLM_MAX_TOKENS=1024
LLM_TIMEOUT_SEC=60
LLM_ENABLED=true

# 3. Lựa chọn Engine AI mặc định ('llm' cho Local NPU, 'gemini' cho Cloud API)
DEFAULT_EXTRACTION_ENGINE="llm"

# 4. Google Gemini API (Tuỳ chọn cho chế độ Cloud)
GEMINI_API_KEY="your_gemini_api_key_here"
GEMINI_MODEL="gemini-3.5-flash-lite"
GEMINI_TIMEOUT_SEC=30

# 5. CSDL MySQL
MYSQL_HOST="localhost"
MYSQL_PORT=3306
MYSQL_USER="farm_user"
MYSQL_PASSWORD="farm_password"
MYSQL_DATABASE="vietgap_diary"
MYSQL_SOCKET="/var/run/mysqld/mysqld.sock"

# 6. Kho lưu trữ âm thanh
AUDIO_STORAGE_DIR="/mnt/data/audio"
```

---

## 6. HƯỚNG DẪN KHỞI CHẠY & VẬN HÀNH

Hệ thống hoạt động theo kiến trúc vi dịch vụ (Microservices), bao gồm:
1. **Dịch vụ AI NPU** (cổng 8080)
2. **Giao diện Web App** (cổng 5000)
3. **Telegram Bot** (tuỳ chọn)

---

### 6.1. Khởi chạy Local LLM Server trên NPU

Dự án cung cấp script điều khiển thông minh [`start_LLM.sh`](file:///root/voice_to_data/start_LLM.sh):

#### Cách 1: Chạy mô hình Qwen3.5-2B (Khuyên dùng - Cân bằng tốt nhất giữa độ chính xác & tốc độ)
```bash
cd /root/voice_to_data
./start_LLM.sh 2b
```
- **Tài nguyên**: Chiếm ~2.8 GB RAM, 3 NPU Cores (W8A8).
- **Tốc độ**: ~10–12s / câu (8.8 tok/s).
- **Độ chính xác**: **86.8%** (Tiệm cận Cloud Gemini).

#### Cách 2: Chạy mô hình Qwen3-0.6B (Tốc độ siêu nhanh, bộ nhớ cực nhẹ)
```bash
./start_LLM.sh 0.6b
```
- **Tài nguyên**: Chiếm < 1 GB RAM.
- **Tốc độ**: ~4.4s / câu (20.3 tok/s).
- **Độ chính xác**: **65.2%** (Thích hợp cho các câu đơn giản, máy cấu hình thấp).

#### Cách 3: Chạy mô hình Qwen3.5-2B trên CPU bằng Llama.cpp (KV Cache Prefix Reuse)
```bash
./start_LLM.sh cpu
# hoặc:
./start_llama.sh
```

Kiểm tra trạng thái server AI sẵn sàng:
```bash
curl -s http://127.0.0.1:8080/v1/models
# Kết quả mẫu: {"data":[{"id":"Qwen3.5-2B_RK3588_w8a8","object":"model"}],"object":"list"}
```

---

### 6.2. Khởi chạy Web App Nông Ký (HTTPS Cổng 5000)

Do trình duyệt web (Google Chrome, Safari, Edge) bắt buộc phải có kết nối an toàn (HTTPS) mới cấp quyền thu âm qua Microphone, web app đã được tích hợp sẵn chứng chỉ SSL tự ký.

Chạy lệnh:
```bash
cd /root/voice_to_data/app
./run.sh
```
Hoặc chạy chế độ nền (Background Service):
```bash
nohup /root/voice_to_data/.venv/bin/python /root/voice_to_data/app/app.py --port 5000 --ssl > /root/voice_to_data/app/app.log 2>&1 &
```

Truy cập trên điện thoại hoặc máy tính cùng mạng LAN:
- **URL**: `https://<IP_MÁY_CHỦ>:5000` (Ví dụ: `https://192.168.40.5:5000` hoặc `https://localhost:5000`)
- *Lưu ý*: Lần đầu truy cập, trình duyệt sẽ hiện cảnh báo bảo mật do chứng chỉ tự ký $\rightarrow$ Nhấn **Nâng cao (Advanced)** $\rightarrow$ Chọn **Tiếp tục truy cập (Proceed to site)**.

---

### 6.3. Khởi chạy Telegram Bot

Nếu nông dân muốn gửi ghi âm từ xa qua Telegram:
```bash
cd /root/voice_to_data
/root/voice_to_data/.venv/bin/python main.py
```

---

## 7. HƯỚNG DẪN SỬ DỤNG GIAO DIỆN WEB

### 7.1. Đăng nhập hệ thống
- **Tài khoản nông dân**: `nongdan1` / Mật khẩu: `123456`
- **Tài khoản quản trị**: `admin` / Mật khẩu: `admin123`

### 7.2. Ghi nhật ký bằng giọng nói
1. Trên thanh tác vụ, chọn loại hoạt động muốn ghi chép:
   - **Tất cả / Tự động nhận diện**
   - **Phun thuốc BVTV**
   - **Bón phân**
   - **Mua vật tư**
   - **Thu hoạch**
   - **Xuất bán**
   - **Công việc khác** (Làm cỏ, tưới nước, tỉa cành...)
2. Bấm nút **Microphone màu đỏ** để bắt đầu thu âm.
3. Đọc to rõ ràng câu nói nhật ký. Ví dụ:
   > *"Hôm nay ngày mùng năm tháng tư nhập một trăm ngàn cây giống cà gai leo từ công ty Tuấn Nông về ngâm ủ."*
   >
   > *"Sáng nay phun hai chai Nano Ecotech trên một héc ta phòng trừ sâu ăn lá và rầy nâu, thời gian cách ly mười bốn ngày."*
4. Bấm nút **Dừng thu âm**.
5. Hệ thống sẽ:
   - Chuyển giọng nói thành văn bản hiển thị lên màn hình.
   - Gọi LLM bóc tách vào các trường nghiệp vụ VietGAP.
   - Hiển thị bản xem trước (Preview) cho nông dân kiểm tra lại.
   - Tự động lưu vào CSDL MySQL.

### 7.3. Xem dòng thời gian (Timeline) & Xuất sổ VietGAP
- Tab **Dòng thời gian (Timeline)**: Hiển thị các hoạt động theo trục ngày tháng trực quan, có thể nghe lại đoạn ghi âm gốc tương ứng với mỗi hoạt động.
- Tab **Sổ VietGAP**: Tra cứu và xuất dữ liệu ra bảng tính theo chuẩn 5 biểu mẫu VietGAP (Bảng 1.5, 1.6, 1.7, 1.8, 1.9).

---

## 8. CHUẨN HOÁ BIỂU MẪU VIETGAP & CƠ SỞ DỮ LIỆU

Dữ liệu sau khi trích xuất được tự động phân bổ vào 13 bảng CSDL trong MySQL `vietgap_diary`:

| Tên bảng CSDL | Tương ứng biểu mẫu VietGAP | Dữ liệu quản lý chính |
| :--- | :--- | :--- |
| `input_purchases` | **Bảng 1.5** - Mua vật tư | Ngày mua, loại vật tư, tên vật tư/giống, số lượng, đơn vị, giá mua, hạn sử dụng, nhà cung cấp |
| `pesticide_applications` | **Bảng 1.6** - Sử dụng thuốc BVTV | Tên thuốc, loại sâu bệnh hại, nồng độ pha, liều lượng, phương pháp phun, thời gian cách ly (PHI), ngày thu hoạch an toàn |
| `fertilizer_applications` | **Bảng 1.6** - Bón phân | Tên phân bón, liều lượng sử dụng, phương pháp bón (bón lót, bón thúc, tưới gốc), giai đoạn cây |
| `waste_disposals` | **Bảng 1.7** - Xử lý bao bì/rác | Ngày xử lý, loại bao bì rác thải BVTV, vị trí lưu giữ, biện pháp xử lý (súc rửa 3 lần, hố tiêu huỷ) |
| `harvest_records` | **Bảng 1.8** - Thu hoạch | Ngày thu hoạch, loại nông sản, sản lượng thu hoạch, địa điểm và phương pháp sơ chế |
| `sales_records` | **Bảng 1.9** - Tiêu thụ sản phẩm | Ngày xuất bán, khối lượng bán, người mua/thương lái/doanh nghiệp, giá bán, thành tiền |
| `cultivation_activities` | Bảng hoạt động gốc | Nhật ký các việc canh tác chung (làm cỏ, dọn bồn, xới đất, làm giàn, tưới nước, dặm cây...) |
| `activity_voice_records` | Dữ liệu âm thanh | Đường dẫn file ghi âm `.wav` và văn bản STT thô phục vụ kiểm chứng thực địa |
| `ai_extraction_records` | Lưu vết AI | Toàn bộ prompt, JSON đầu ra, thời gian xử lý của mô hình AI để phục vụ kiểm toán |

---

## 9. BỘ CÔNG CỤ BENCHMARK & ĐÁNH GIÁ MÔ HÌNH

Dự án tích hợp sẵn các công cụ benchmark tự động giúp lập trình viên kiểm thử và đánh giá hiệu năng:

### 9.1. So sánh đối chiếu 3 mô hình (Gemini vs RKLLM 2B vs RKLLM 0.6B)
Chạy trên 10 câu thực tế tiêu biểu đủ các bảng:
```bash
cd /root/voice_to_data
# Đo Gemini
.venv/bin/python benchmark_10_sentences.py gemini

# Đo RKLLM 2B
.venv/bin/python benchmark_10_sentences.py 2b

# Đo RKLLM 0.6B
.venv/bin/python benchmark_10_sentences.py 0.6b
```

**Kết quả đo đạc thực tế trên NPU RK3588**:
- **Gemini 3.5 Flash-Lite**: Độ trễ **1.27s** | Độ chính xác **91.6%** (Cloud API)
- **RKLLM 2B (NPU RK3588)**: Độ trễ **12.25s** (8.8 tok/s) | Độ chính xác **86.8%** (Offline)
- **RKLLM 0.6B (NPU RK3588)**: Độ trễ **4.44s** (20.3 tok/s) | Độ chính xác **65.2%** (Offline)

### 9.2. Benchmark toàn bộ 102 câu nhật ký và ghi vào CSDL
```bash
cd /root/voice_to_data
.venv/bin/python benchmark_rkllm_0.6b_and_save_db.py
```
Hệ thống sẽ duyệt tuần tự 102 câu từ `sample.txt`, bóc tách thông tin, chuẩn hoá qua normalizer và lưu thành công 102 bản ghi vào MySQL, đồng thời xuất báo cáo tổng kết JSON tại `benchmark_rkllm_0.6b_summary.json`.

---

## 10. XỬ LÝ SỰ CỐ THƯỜNG GẶP (TROUBLESHOOTING)

### 1. Cổng 8080 hoặc 5000 bị báo lỗi "Address already in use"
Giải phóng tiến trình đang chiếm cổng:
```bash
fuser -k 8080/tcp 2>/dev/null || true
fuser -k 5000/tcp 2>/dev/null || true
```

### 2. Trình duyệt không mở được Microphone để ghi âm
- **Nguyên nhân**: Trình duyệt chỉ cho phép truy cập Microphone qua giao thức an toàn `https://` hoặc `localhost`.
- **Khắc phục**: 
  - Chắc chắn rằng bạn đang truy cập bằng đường dẫn `https://` thay vì `http://`.
  - Trên điện thoại Android (Chrome), nếu bị chặn chứng chỉ tự ký, vào `chrome://flags` $\rightarrow$ tìm `unsafely-treat-insecure-origin-as-secure` $\rightarrow$ nhập địa chỉ IP máy chủ (ví dụ `https://192.168.40.5:5000`) $\rightarrow$ chọn `Enabled` và Restart trình duyệt.

### 3. Khởi động RKLLM Server bị lỗi "failed to open librkllmrt.so"
- Kiểm tra file thư viện:
```bash
ls -la /root/voice_to_data/lib/librkllmrt.so
export LD_LIBRARY_PATH=/root/voice_to_data/lib:$LD_LIBRARY_PATH
```

### 4. Muốn xoá sạch nhật ký đã ghi để thử nghiệm lại từ đầu
Chạy script dọn dẹp CSDL chuyên dụng:
```bash
.venv/bin/python /root/.gemini/antigravity-cli/brain/b9c20c1d-3f0d-4248-9988-2f9ef0fdea9a/scratch/clear_activities.py
```
Script sẽ dọn sạch toàn bộ các bảng hoạt động và đưa CSDL về trạng thái dữ liệu mẫu sạch ban đầu.

---

## 👨‍💻 TÁC GIẢ & BẢN QUYỀN
- **Dự án**: Nông Ký AI - Voice to Data VietGAP
- **Nền tảng mục tiêu**: Rockchip RK3588 (Odroid M2, Orange Pi 5 Plus) & Linux aarch64
- **Tiêu chuẩn dữ liệu**: Quy chuẩn thực hành nông nghiệp tốt VietGAP (TCVN 11892-1:2017)
