"""
Các tiện ích, cấu hình và biến dùng chung cho ứng dụng Flask Nông Ký.
Toàn bộ dữ liệu media (audio, photos, uploads) được lưu trên phân vùng /mnt/data.
"""

import sys
import os

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Thiết lập đường dẫn tuyệt đối cho các model của services
os.environ["TOKENS_PATH"] = os.path.join(ROOT_DIR, "models/tokens.txt")
os.environ["ENCODER_PATH"] = os.path.join(ROOT_DIR, "models/encoder.int8.onnx")
os.environ["DECODER_PATH"] = os.path.join(ROOT_DIR, "models/decoder.onnx")
os.environ["JOINER_PATH"] = os.path.join(ROOT_DIR, "models/joiner.int8.onnx")
os.environ["VAD_MODEL_PATH"] = os.path.join(ROOT_DIR, "models/silero_vad.onnx")
os.environ["TMP_DIR"] = os.path.join(ROOT_DIR, "tmp")

import logging
import asyncio
import threading
from logging.handlers import RotatingFileHandler

import config
from services.normalizer import (
    normalize_activity_list,
    normalize_vietgap_tables,
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CERTS_DIR = os.path.join(APP_DIR, "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
APP_LOG_FILE = os.path.join(APP_DIR, "app.log")

# Lưu trữ media trên phân vùng dữ liệu lớn (/mnt/data)
STATIC_AUDIO_DIR = os.getenv("AUDIO_STORAGE_DIR", getattr(config, "AUDIO_STORAGE_DIR", "/mnt/data/audio"))
STATIC_PHOTOS_DIR = os.getenv("PHOTOS_STORAGE_DIR", "/mnt/data/photos")
UPLOADS_DIR = os.getenv("UPLOADS_DIR", "/mnt/data/uploads")

for _dir in (CERTS_DIR, STATIC_AUDIO_DIR, STATIC_PHOTOS_DIR, UPLOADS_DIR):
    os.makedirs(_dir, exist_ok=True)

# Cấu hình logging ghi đồng thời ra console và file app.log
log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(log_formatter)

file_handler = RotatingFileHandler(
    APP_LOG_FILE,
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
file_handler.setFormatter(log_formatter)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers = [console_handler, file_handler]

logger = logging.getLogger("nong-ky-flask")
logging.getLogger("werkzeug").setLevel(logging.INFO)

# Quản lý Background Event Loop cho Async Services (aiohttp, ffmpeg)
_bg_loop = asyncio.new_event_loop()
_bg_thread = threading.Thread(target=_bg_loop.run_forever, daemon=True, name="services-loop")
_bg_thread.start()


def run_async(coro):
    """Chạy một coroutine an toàn trên background thread event loop."""
    future = asyncio.run_coroutine_threadsafe(coro, _bg_loop)
    return future.result()


FIELD_KEYS = [
    "ngay_thuc_hien",
    "giai_doan_sinh_truong",
    "cong_viec",
    "sinh_vat_gay_hai",
    "ten_phan_thuoc",
    "luong_su_dung",
    "noi_mua",
    "thoi_gian_cach_ly",
]


def apply_normalizer(raw_dict: dict) -> dict:
    """Tự động chuẩn hoá một activity dict bằng services.normalizer, bảo tồn đầy đủ các bảng và trường VietGAP."""
    if not raw_dict:
        return {}

    tables_backup = raw_dict.get("tables")
    if tables_backup and isinstance(tables_backup, dict):
        tables_backup = normalize_vietgap_tables(tables_backup)

    item = dict(raw_dict)
    item.pop("tables", None)

    if "giai_doan_sinh_truong" in item and "giai_doan" not in item:
        item["giai_doan"] = item["giai_doan_sinh_truong"]

    try:
        norm_list = normalize_activity_list([item])
        if norm_list and isinstance(norm_list, list):
            res = dict(norm_list[0])
            for k, v in raw_dict.items():
                if k != "tables" and (k not in res or res[k] is None):
                    res[k] = v
            if "giai_doan" in res and res.get("giai_doan_sinh_truong") is None:
                res["giai_doan_sinh_truong"] = res["giai_doan"]
            if tables_backup is not None:
                res["tables"] = tables_backup
            return res
    except Exception as e:
        logger.warning(f"Lỗi chuẩn hoá trong services.normalizer: {e}")

    res = dict(raw_dict)
    if tables_backup is not None:
        res["tables"] = tables_backup
    return res
