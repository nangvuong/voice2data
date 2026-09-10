"""
Đọc toàn bộ cấu hình từ biến môi trường (.env).
Không hardcode token hay đường dẫn model trong code.
"""

import os
from dotenv import load_dotenv

# Nạp file .env nếu có (khi chạy local). Trên server có thể set env trực tiếp.
load_dotenv()


def _require(name: str) -> str:
    """Lấy biến môi trường bắt buộc, báo lỗi rõ ràng nếu thiếu."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Thiếu biến môi trường bắt buộc: {name}")
    return value


# ----- Telegram -----
TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")

# ----- sherpa-onnx model -----
TOKENS_PATH = os.getenv("TOKENS_PATH", "models/tokens.txt")
ENCODER_PATH = os.getenv("ENCODER_PATH", "models/encoder.int8.onnx")
DECODER_PATH = os.getenv("DECODER_PATH", "models/decoder.onnx")
JOINER_PATH = os.getenv("JOINER_PATH", "models/joiner.int8.onnx")
NUM_THREADS = int(os.getenv("NUM_THREADS", "4"))

# ----- sherpa-onnx VAD (dùng để cắt audio dài thành từng đoạn có tiếng nói
# trước khi đưa vào OfflineRecognizer, tránh decode nguyên khối audio dài) -----
VAD_MODEL_PATH = os.getenv("VAD_MODEL_PATH", "models/silero_vad.onnx")
VAD_THRESHOLD = float(os.getenv("VAD_THRESHOLD", "0.5"))
VAD_MIN_SILENCE_SEC = float(os.getenv("VAD_MIN_SILENCE_SEC", "0.5"))
VAD_MIN_SPEECH_SEC = float(os.getenv("VAD_MIN_SPEECH_SEC", "0.25"))
# Độ dài tối đa 1 đoạn (giây) dù không có khoảng lặng - chặn trường hợp nói
# liên tục không nghỉ khiến 1 đoạn quá dài, ảnh hưởng chất lượng decode.
VAD_MAX_SPEECH_SEC = float(os.getenv("VAD_MAX_SPEECH_SEC", "25"))
VAD_WINDOW_SIZE = int(os.getenv("VAD_WINDOW_SIZE", "512"))

# ----- Audio & Streaming WebSocket -----
TARGET_SAMPLE_RATE = 16000
WS_PORT = int(os.getenv("WS_PORT", "8765"))
WS_ENABLED = os.getenv("WS_ENABLED", "true").lower() in ("true", "1", "yes")

# ----- Thư mục tạm -----
TMP_DIR = os.getenv("TMP_DIR", "tmp")

# ----- Giới hạn -----
# LƯU Ý QUAN TRỌNG: Telegram Bot API bản cloud (mặc định) chỉ cho bot tải
# (getFile/download) file tối đa 20MB, BẤT KỂ giá trị MAX_FILE_SIZE_MB đặt
# ở đây. Muốn nhận file audio 30 phút (thường 50-100MB+ tuỳ định dạng gốc),
# bắt buộc phải tự host Local Bot API Server
# (https://github.com/tdlib/telegram-bot-api) - khi đó giới hạn tải file
# mới lên tới 2000MB. Giá trị dưới đây đặt theo mức trần đó.
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "2000"))
# ffmpeg convert 1 file dài (vd. 30 phút, nhiều định dạng nén) hiếm khi mất
# quá vài phút, nhưng để dư thời gian phòng server yếu / file rất dài.
FFMPEG_TIMEOUT_SEC = int(os.getenv("FFMPEG_TIMEOUT_SEC", "1800"))  # 30 phút
# STT chạy CPU, với audio ~30 phút (đã cắt VAD) có thể mất nhiều phút tuỳ
# cấu hình máy; đặt timeout rộng để không bị huỷ giữa chừng.
STT_TIMEOUT_SEC = int(os.getenv("STT_TIMEOUT_SEC", "3600"))  # 60 phút

# ----- LLM (Local AI Server trên Odroid M2) -----
# Địa chỉ IP và Port của máy chạy AI (Local llama-server / Flask server)
LOCAL_AI_BASE_URL = os.getenv("LOCAL_AI_BASE_URL", "http://127.0.0.1:8080")
# Đường dẫn API
LOCAL_AI_ENDPOINT = os.getenv("LOCAL_AI_ENDPOINT", "/v1/chat/completions")
# Tên model (tuỳ chọn, server có thể tự lấy model mặc định)
LOCAL_AI_MODEL = os.getenv("LOCAL_AI_MODEL", "Qwen3.5-2B_RK3588_w8a8")
# Địa chỉ IP và Port cho model phân loại nhanh (0.6B)
LOCAL_AI_CLASSIFIER_URL = os.getenv("LOCAL_AI_CLASSIFIER_URL", "http://127.0.0.1:8081")
LOCAL_AI_CLASSIFIER_MODEL = os.getenv("LOCAL_AI_CLASSIFIER_MODEL", "qwen3-0.6b_w8a8_rk3588")
# Số token tối đa cho mỗi request LLM (phần output JSON)
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "1024"))
# Timeout cho mỗi request LLM (giây)
LLM_TIMEOUT_SEC = int(os.getenv("LLM_TIMEOUT_SEC", "60"))
# Số lần thử lại tối đa khi gặp lỗi (timeout, lỗi server, JSON parse lỗi)
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))
# Bật/tắt LLM post-processing (đặt false nếu chỉ muốn STT thô)
LLM_ENABLED = os.getenv("LLM_ENABLED", "true").lower() in ("true", "1", "yes")
# Engine trích xuất mặc định của ứng dụng ('llm' hoặc 'gemini')
DEFAULT_EXTRACTION_ENGINE = os.getenv("DEFAULT_EXTRACTION_ENGINE", "gemini")

# ----- Google AI Gemini API -----
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_VLM_MODEL = os.getenv("GEMINI_VLM_MODEL", "gemini-3.1-flash-lite")
GEMINI_TIMEOUT_SEC = int(os.getenv("GEMINI_TIMEOUT_SEC", "30"))

# ----- MySQL Database -----
MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "farm_user")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "farm_password")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "vietgap_diary")
MYSQL_SOCKET = os.getenv("MYSQL_SOCKET", "/var/run/mysqld/mysqld.sock")

# ----- Audio Storage -----
AUDIO_STORAGE_DIR = os.getenv("AUDIO_STORAGE_DIR", "/mnt/data/audio")

# ----- TTS (Text-to-Speech) Model -----
TTS_MODEL_DIR = os.getenv("TTS_MODEL_DIR", "/mnt/data/vits-piper-vi_VN-vais1000-medium")
TTS_ENGINE = os.getenv("TTS_ENGINE", "sherpa")  # 'sherpa' hoặc 'piper'
TTS_SPEED = float(os.getenv("TTS_SPEED", "1.0"))
TTS_NUM_THREADS = int(os.getenv("TTS_NUM_THREADS", "4"))
TTS_SAMPLE_RATE = int(os.getenv("TTS_SAMPLE_RATE", "22050"))
TTS_SERVER_PORT = int(os.getenv("TTS_SERVER_PORT", "8082"))

# ----- OCR (Optical Character Recognition - RapidOCR) -----
OCR_NUM_THREADS = int(os.getenv("OCR_NUM_THREADS", "4"))
OCR_USE_DET = os.getenv("OCR_USE_DET", "true").lower() in ("true", "1", "yes")
OCR_USE_CLS = os.getenv("OCR_USE_CLS", "true").lower() in ("true", "1", "yes")
OCR_USE_REC = os.getenv("OCR_USE_REC", "true").lower() in ("true", "1", "yes")
OCR_MIN_SCORE = float(os.getenv("OCR_MIN_SCORE", "0.5"))
OCR_TIMEOUT_SEC = int(os.getenv("OCR_TIMEOUT_SEC", "60"))
OCR_DET_MODEL_PATH = os.getenv("OCR_DET_MODEL_PATH", "")
OCR_REC_MODEL_PATH = os.getenv("OCR_REC_MODEL_PATH", "")
OCR_CLS_MODEL_PATH = os.getenv("OCR_CLS_MODEL_PATH", "")
OCR_REC_KEYS_PATH = os.getenv("OCR_REC_KEYS_PATH", "")
OCR_CONFIG_PATH = os.getenv("OCR_CONFIG_PATH", "")

