"""
Quản lý tập trung toàn bộ các API Blueprints của hệ thống Nông Ký.
"""

import os
from flask import Blueprint, Flask, jsonify, redirect, request, send_file, send_from_directory

import config
from app.common import (
    ROOT_DIR,
    UPLOADS_DIR,
    STATIC_AUDIO_DIR,
    STATIC_PHOTOS_DIR,
    CERT_FILE,
    KEY_FILE,
)
from app.api.auth import auth_bp
from app.api.conversation import conversation_bp
from app.api.stt import stt_bp
from app.api.ai import ai_bp
from app.api.diary import diary_bp
from app.api.export import export_bp
from services.rag.rag_service import rag_bp

system_bp = Blueprint("system", __name__)


@system_bp.route("/")
@system_bp.route("/nongky")
@system_bp.route("/nongky/")
def index():
    """Chuyển hướng về Frontend React Native (Port 5001)."""
    host = request.host.split(":")[0]
    return redirect(f"https://{host}:5001/")


@system_bp.route("/api/health", methods=["GET"])
def health():
    """Kiểm tra trạng thái hoạt động của Backend và các AI Engines."""
    has_ssl = os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)
    return jsonify({
        "status": "online",
        "ssl": has_ssl,
        "auto_normalized": True,
        "services": {
            "stt_engine": "services.stt_engine (sherpa-onnx + silero_vad)",
            "llm_engine": f"services.llm_engine (Qwen3 on RK3588 NPU - {config.LOCAL_AI_MODEL})",
            "normalizer": "services.normalizer (dates, units, rules - ALWAYS ACTIVE)",
            "audio_converter": "services.audio_converter (ffmpeg)",
            "database": "MySQL Local (farm_diary)",
            "ready": True,
        },
    })


@system_bp.route("/download-cert")
def download_cert():
    """Tải chứng chỉ SSL để cài đặt vào thiết bị (Trusted Root CA)."""
    if os.path.exists(CERT_FILE):
        return send_file(CERT_FILE, as_attachment=True, download_name="nongky-cert.crt")
    return jsonify({"error": "Certificate not found"}), 404


@system_bp.route("/uploads/<path:filename>")
def serve_uploads(filename):
    """Phục vụ file ảnh/âm thanh tải lên tạm thời."""
    return send_from_directory(UPLOADS_DIR, filename)


@system_bp.route("/tmp/<path:filename>")
def serve_tmp_audio(filename):
    """Phục vụ file âm thanh TTS tạm thời."""
    return send_from_directory(os.path.join(ROOT_DIR, "tmp"), filename)


@system_bp.route("/static/audio/<path:filename>")
def serve_static_audio(filename):
    """Phục vụ file âm thanh từ /mnt/data/audio."""
    for folder in (STATIC_AUDIO_DIR, UPLOADS_DIR):
        if folder and os.path.isfile(os.path.join(folder, filename)):
            return send_from_directory(folder, filename)
    return send_from_directory(STATIC_AUDIO_DIR, filename)


@system_bp.route("/static/photos/<path:filename>")
def serve_static_photos(filename):
    """Phục vụ ảnh chụp hoạt động thực địa từ /mnt/data/photos."""
    for folder in (STATIC_PHOTOS_DIR, UPLOADS_DIR):
        if folder and os.path.isfile(os.path.join(folder, filename)):
            return send_from_directory(folder, filename)
    return send_from_directory(STATIC_PHOTOS_DIR, filename)


def register_blueprints(application: Flask) -> None:
    """Đăng ký toàn bộ Blueprints vào Flask application."""
    for bp in (
        system_bp,
        auth_bp,
        conversation_bp,
        stt_bp,
        ai_bp,
        diary_bp,
        export_bp,
        rag_bp,
    ):
        application.register_blueprint(bp)
