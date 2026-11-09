"""
Routes cho trợ lý hội thoại AI: ghi nhật ký, chẩn đoán sâu bệnh, TTS.
"""

import os
import re
import uuid
import json
from flask import Blueprint, request, jsonify, session

from app.common import logger, run_async, UPLOADS_DIR
from conversation import conversation_manager

conversation_bp = Blueprint("conversation", __name__)


@conversation_bp.route("/api/conversation/message", methods=["POST"])
def api_conversation_message():
    """Nhận tin nhắn văn bản từ người dùng và phản hồi qua ConversationManager."""
    try:
        farmer = session.get("farmer")
        farmer_id = (farmer.get("id") or farmer.get("farmer_id") or 1) if farmer else 1
        data = request.get_json() or {}
        text = data.get("text", "").strip()
        flow_mode = (data.get("flow_mode") or "diary").strip().lower()
        session_id = data.get("session_id") or session.get("conv_session_id")
        if not session_id:
            session_id = f"web_{flow_mode}_{uuid.uuid4().hex[:8]}"
            session["conv_session_id"] = session_id
        enable_tts = bool(data.get("enable_tts", False))
        audio_url = data.get("audio_url") or session.pop("last_voice_audio_url", "")

        res = run_async(
            conversation_manager.handle_text(
                session_id=session_id,
                user_text=text,
                farmer_id=farmer_id,
                enable_tts=enable_tts,
                flow_mode=flow_mode,
                audio_url=audio_url,
            )
        )
        if res.get("audio_file"):
            res["audio_file"] = f"/tmp/{os.path.basename(res['audio_file'])}"
        return jsonify(res)
    except Exception as e:
        logger.exception("Lỗi api_conversation_message: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@conversation_bp.route("/api/conversation/image", methods=["POST"])
def api_conversation_image():
    """Nhận file ảnh tải lên, phân tích VLM và phản hồi qua ConversationManager."""
    try:
        farmer = session.get("farmer")
        farmer_id = (farmer.get("id") or farmer.get("farmer_id") or 1) if farmer else 1
        file = request.files.get("file") or request.files.get("image")
        if not file:
            return jsonify({"status": "error", "message": "Không có file ảnh"}), 400

        flow_mode = (request.form.get("flow_mode") or "diary").strip().lower()
        task = (request.form.get("task") or "auto").strip().lower()
        session_id = request.form.get("session_id") or session.get("conv_session_id")
        if not session_id:
            session_id = f"web_{flow_mode}_{uuid.uuid4().hex[:8]}"
            session["conv_session_id"] = session_id
        enable_tts = request.form.get("enable_tts") in ("true", "1", "True")

        ext = os.path.splitext(file.filename)[1] or ".jpg"
        temp_name = f"conv_img_{uuid.uuid4().hex[:10]}{ext}"
        temp_path = os.path.join(UPLOADS_DIR, temp_name)
        file.save(temp_path)
        web_image_url = f"/uploads/{temp_name}"

        res = run_async(
            conversation_manager.handle_image(
                session_id=session_id,
                image_path=temp_path,
                farmer_id=farmer_id,
                enable_tts=enable_tts,
                flow_mode=flow_mode,
                photo_url=web_image_url,
                task=task,
            )
        )
        res["web_image_url"] = web_image_url
        if res.get("audio_file"):
            res["audio_file"] = f"/tmp/{os.path.basename(res['audio_file'])}"
        return jsonify(res)
    except Exception as e:
        logger.exception("Lỗi api_conversation_image: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@conversation_bp.route("/api/conversation/audio", methods=["POST"])
def api_conversation_audio():
    """Nhận file âm thanh ghi âm, nhận diện STT và phản hồi qua ConversationManager."""
    try:
        farmer = session.get("farmer")
        farmer_id = (farmer.get("id") or farmer.get("farmer_id") or 1) if farmer else 1
        file = request.files.get("file") or request.files.get("audio")
        if not file:
            return jsonify({"status": "error", "message": "Không có file âm thanh"}), 400

        flow_mode = (request.form.get("flow_mode") or "diary").strip().lower()
        session_id = request.form.get("session_id") or session.get("conv_session_id")
        if not session_id:
            session_id = f"web_{flow_mode}_{uuid.uuid4().hex[:8]}"
            session["conv_session_id"] = session_id
        enable_tts = request.form.get("enable_tts") in ("true", "1", "True")

        ext = os.path.splitext(file.filename)[1] or ".wav"
        temp_name = f"conv_audio_{uuid.uuid4().hex[:10]}{ext}"
        temp_path = os.path.join(UPLOADS_DIR, temp_name)
        file.save(temp_path)

        res = run_async(
            conversation_manager.handle_audio(
                session_id=session_id,
                audio_path=temp_path,
                farmer_id=farmer_id,
                enable_tts=enable_tts,
                flow_mode=flow_mode,
            )
        )
        if res.get("audio_file"):
            res["audio_file"] = f"/tmp/{os.path.basename(res['audio_file'])}"
        return jsonify(res)
    except Exception as e:
        logger.exception("Lỗi api_conversation_audio: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@conversation_bp.route("/api/conversation/reset", methods=["POST"])
def api_conversation_reset():
    """Đặt lại trạng thái phiên hội thoại và dọn dẹp các file audio TTS."""
    try:
        session_id = None
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            session_id = data.get("session_id")
        if not session_id and request.form:
            session_id = request.form.get("session_id")
        if not session_id and request.data:
            try:
                raw_json = json.loads(request.data.decode("utf-8"))
                if isinstance(raw_json, dict):
                    session_id = raw_json.get("session_id")
            except Exception:
                pass
        if not session_id:
            session_id = session.get("conv_session_id")

        deleted_count = 0
        if session_id:
            state = conversation_manager.store.get(session_id)
            if state:
                deleted_count = state.cleanup_tts_files()
                state.reset()
            # Xóa phiên khỏi RAM để giải phóng tài nguyên
            conversation_manager.store.remove(session_id)

        # Dọn dẹp thêm các file tts mồ côi
        cleaned_old = conversation_manager.cleanup_old_tts_files(max_age_seconds=60, keep_latest=0)

        return jsonify({
            "status": "success",
            "message": "Đã đặt lại hội thoại và dọn dẹp audio",
            "deleted_tts_count": deleted_count,
            "cleaned_old": cleaned_old
        })
    except Exception as e:
        logger.exception("Lỗi api_conversation_reset: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@conversation_bp.route("/api/conversation/tts", methods=["POST"])
def api_conversation_tts():
    """Tạo audio TTS từ đoạn văn bản (đọc to thông tin khi bác nông dân bấm câu hỏi)."""
    try:
        data = request.get_json(silent=True) or {}
        text = (data.get("text") or "").strip()
        if not text:
            return jsonify({"status": "error", "message": "Nội dung văn bản trống"}), 400

        session_id = data.get("session_id") or session.get("conv_session_id")
        if session_id:
            session["conv_session_id"] = session_id

        # Làm sạch markdown để giọng đọc mượt mà
        clean_text = re.sub(r"[*#_`\[\]]", "", text)
        clean_text = re.sub(r"\n+", ". ", clean_text)
        audio_file = conversation_manager._generate_tts_audio(clean_text)
        if not audio_file or not os.path.exists(audio_file):
            return jsonify({"status": "error", "message": "Không thể tạo giọng đọc"}), 500

        if session_id:
            state = conversation_manager.store.get_or_create(session_id)
            if state:
                state.register_tts_audio(audio_file)

        return jsonify({
            "status": "success",
            "audio_file": f"/tmp/{os.path.basename(audio_file)}"
        })
    except Exception as e:
        logger.exception("Lỗi api_conversation_tts: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@conversation_bp.route("/api/conversation/tts/cleanup", methods=["POST"])
def api_conversation_tts_cleanup():
    """Dọn dẹp file âm thanh TTS (bảo vệ file của phiên đang hoạt động, chỉ dọn file mồ côi)."""
    try:
        # Không xóa file audio khi phiên còn đang hoạt động để người dùng có thể nghe lại/replay
        cleaned_count = conversation_manager.cleanup_old_tts_files(max_age_seconds=600, keep_latest=5)
        return jsonify({
            "status": "success",
            "deleted": False,
            "cleaned_count": cleaned_count,
            "message": "File audio được giữ lại trong suốt phiên để có thể nghe lại."
        })
    except Exception as e:
        logger.warning("Lỗi api_conversation_tts_cleanup: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500
