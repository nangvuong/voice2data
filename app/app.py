"""
Flask Web Application: Nông Ký - Voice to Text to Data
Entrypoint khởi tạo Flask Server và đăng ký các API Blueprints từ thư mục app/api/.
"""

import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(APP_DIR)
if APP_DIR in sys.path:
    sys.path.remove(APP_DIR)
if ROOT_DIR in sys.path:
    sys.path.remove(ROOT_DIR)
sys.path.insert(0, ROOT_DIR)

# Thiết lập đường dẫn tuyệt đối cho các model của services
os.environ["TOKENS_PATH"] = os.path.join(ROOT_DIR, "models/tokens.txt")
os.environ["ENCODER_PATH"] = os.path.join(ROOT_DIR, "models/encoder.int8.onnx")
os.environ["DECODER_PATH"] = os.path.join(ROOT_DIR, "models/decoder.onnx")
os.environ["JOINER_PATH"] = os.path.join(ROOT_DIR, "models/joiner.int8.onnx")
os.environ["VAD_MODEL_PATH"] = os.path.join(ROOT_DIR, "models/silero_vad.onnx")
os.environ["TMP_DIR"] = os.path.join(ROOT_DIR, "tmp")

import argparse
from flask import Flask
from flask_cors import CORS

import config
from app.common import (
    CERTS_DIR,
    CERT_FILE,
    KEY_FILE,
    logger,
    run_async,
    apply_normalizer,
    FIELD_KEYS,
)
from app.api import register_blueprints
from services.database import init_db_and_seed
from services.engines.llm_engine import llm_engine
from services.engines.gemini_engine import gemini_engine
from services.audio.realtime_stt import realtime_stt_engine

__all__ = [
    "app",
    "create_app",
    "run_async",
    "logger",
    "apply_normalizer",
    "FIELD_KEYS",
]


def create_app() -> Flask:
    """Khởi tạo và cấu hình Flask Application."""
    application = Flask(__name__, static_folder=None, template_folder=None)
    application.secret_key = os.getenv("FLASK_SECRET_KEY", "vietgap_nong_ky_secret_odroid_2026")
    CORS(application)
    register_blueprints(application)
    return application


app = create_app()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nông Ký Flask Server")
    parser.add_argument("--port", type=int, default=5000, help="Cổng lắng nghe (mặc định 5000)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Địa chỉ host")
    parser.add_argument("--ssl", action="store_true", default=True, help="Bật HTTPS (mặc định bật)")
    parser.add_argument("--no-ssl", dest="ssl", action="store_false", help="Tắt HTTPS, chỉ dùng HTTP")
    args = parser.parse_args()

    try:
        init_db_and_seed()
        logger.info("✅ CSDL MySQL local (farm_diary) đã sẵn sàng.")
    except Exception as e:
        logger.warning(f"⚠️ Kiểm tra MySQL local gặp cảnh báo: {e}")

    try:
        run_async(llm_engine.start())
        logger.info("✅ services.llm_engine (Local Qwen3 trên RK3588 NPU: %s) đã sẵn sàng.", config.LOCAL_AI_MODEL)
    except Exception as e:
        logger.warning(f"⚠️ Khởi động llm_engine gặp cảnh báo: {e}")

    if config.GEMINI_API_KEY:
        try:
            run_async(gemini_engine.start())
            logger.info("✅ services.gemini_engine (Google Gemini: %s) đã sẵn sàng.", gemini_engine.default_model)
        except Exception as e:
            logger.warning(f"⚠️ Khởi động gemini_engine gặp cảnh báo: {e}")

    ssl_context = None
    if args.ssl and os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        ssl_context = (CERT_FILE, KEY_FILE)
        logger.info(f"🔒 Chạy ứng dụng qua HTTPS với chứng chỉ tại {CERTS_DIR}")
        logger.info(f"   URL: https://{args.host}:{args.port}")
    else:
        logger.info(f"⚠️ Chạy ứng dụng qua HTTP tại http://{args.host}:{args.port}")

    if getattr(config, "WS_ENABLED", True):
        ws_port = getattr(config, "WS_PORT", 8765)
        ws_cert = CERT_FILE if (args.ssl and os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)) else None
        ws_key = KEY_FILE if ws_cert else None
        try:
            realtime_stt_engine.start_websocket_server_in_background(
                host=args.host,
                port=ws_port,
                ssl_cert=ws_cert,
                ssl_key=ws_key,
            )
            ws_protocol = "wss" if ws_cert else "ws"
            logger.info(f"🚀 Realtime STT WebSocket Server đã khởi động tại {ws_protocol}://{args.host}:{ws_port}")
        except Exception as e:
            logger.warning(f"⚠️ Khởi động Realtime STT WebSocket Server gặp cảnh báo: {e}")

    app.run(host=args.host, port=args.port, ssl_context=ssl_context, debug=False)
