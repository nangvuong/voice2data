"""
Flask Web Application: Nông Ký - Voice to Text to Data
Sử dụng trực tiếp các Engine từ thư mục 'services':
- services.stt_engine (sherpa-onnx + Silero VAD)
- services.audio_converter (ffmpeg 16k mono pcm16)
- services.llm_engine (RKLLM local server NPU RK3588)
- services.normalizer (Tự động chuẩn hoá 100% ngày, đơn vị, chữ hoa CSDL)
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

import json
import re
import time
import uuid
import logging
import argparse
import asyncio
import threading
from datetime import datetime
from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    send_file,
    send_from_directory,
    session,
    redirect,
    url_for,
)
from flask_cors import CORS

import config
from services.audio.audio_converter import resample_to_16k_mono_pcm16, AudioConversionError
from services.engines.stt_engine import stt_engine, STTError, update_onnx_hotwords
from services.audio.hotwords_manager import hotwords_manager
from services.audio.stt_records import stt_records
from services.engines.ocr_engine import ocr_engine, OCRError
from services.vietgap.material_matcher import material_matcher
from services.engines.vlm_engine import vlm_engine, VLMEngineError
from services.audio.realtime_stt import realtime_stt_engine
from services.engines.llm_engine import llm_engine, LLMError
from services.engines.gemini_engine import gemini_engine, GeminiEngineError
from services.normalizer import (
    normalize_activity_list,
    normalize_for_webapp,
    normalize_text,
    classify_into_tables,
    normalize_db_tables,
    normalize_vietgap_tables,
)
from services.database import (
    init_db_and_seed,
    authenticate_farmer,
    get_farmer_by_id,
    get_diary_records,
    get_diary_by_tables,
    get_diary_record_by_id,
    save_diary_to_mysql,
    update_diary_record,
    delete_diary_from_mysql,
    delete_diary_record,
    get_categories,
    get_plots,
    find_nearest_plot,
    detect_growth_stage_from_text,
    update_season_growth_stage,
    GROWTH_STAGES,
)
from services.exporters import (
    export_cultivation_diary_to_bytes,
    export_cultivation_diary_to_docx_bytes,
    export_cultivation_diary_to_pdf_bytes,
    get_available_seasons,
    export_vietgap_tables_excel_bytes,
    export_vietgap_tables_docx_bytes,
    export_vietgap_tables_pdf_bytes,
)
from conversation import conversation_manager

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
UPLOADS_DIR = os.path.join(APP_DIR, "uploads")
CERTS_DIR = os.path.join(APP_DIR, "certs")
LOGS_FILE = os.path.join(DATA_DIR, "logs.json")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
APP_LOG_FILE = os.path.join(APP_DIR, "app.log")

# Cấu hình logging ghi đồng thời ra console và file app.log
from logging.handlers import RotatingFileHandler

log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(log_formatter)

file_handler = RotatingFileHandler(
    APP_LOG_FILE,
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8"
)
file_handler.setFormatter(log_formatter)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers = [console_handler, file_handler]

logger = logging.getLogger("nong-ky-flask")

werkzeug_logger = logging.getLogger("werkzeug")
werkzeug_logger.setLevel(logging.INFO)

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(CERTS_DIR, exist_ok=True)

# Khởi tạo logs file nếu chưa có
if not os.path.exists(LOGS_FILE):
    with open(LOGS_FILE, "w", encoding="utf-8") as f:
        json.dump([], f, ensure_ascii=False, indent=2)

# Quản lý Background Event Loop cho Async Services (aiohttp, ffmpeg)
_bg_loop = asyncio.new_event_loop()
_bg_thread = threading.Thread(target=_bg_loop.run_forever, daemon=True, name="services-loop")
_bg_thread.start()


def run_async(coro):
    """Chạy một coroutine an toàn trên background thread event loop."""
    future = asyncio.run_coroutine_threadsafe(coro, _bg_loop)
    return future.result()


# Danh sách 8 trường chuẩn cho UI
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

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.secret_key = os.getenv("FLASK_SECRET_KEY", "vietgap_nong_ky_secret_odroid_2026")
CORS(app)

# ── RAG Knowledge Base Blueprint ──
from services.rag.rag_service import rag_bp  # noqa: E402
app.register_blueprint(rag_bp)


def read_logs():
    try:
        with open(LOGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def write_logs(logs):
    with open(LOGS_FILE, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)


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


@app.route("/login", methods=["GET", "POST"])
def login():
    """Trang đăng nhập nông hộ kết nối CSDL MySQL local."""
    if request.method == "GET":
        if "farmer" in session:
            return redirect("/nongky")
        return render_template("login.html")

    data = request.get_json(silent=True) or request.form
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    farmer = authenticate_farmer(username, password)
    if not farmer:
        return jsonify({"status": "error", "message": "Tên đăng nhập hoặc mật khẩu không chính xác."}), 401

    session["farmer"] = farmer
    logger.info("👨‍🌾 Nông dân '%s' (%s) đã đăng nhập thành công.", farmer.get("username"), farmer.get("full_name"))
    return jsonify({
        "status": "success",
        "farmer": farmer,
        "redirect": "/nongky"
    })


_last_session_cleanup_time = 0.0


@app.route("/logout", methods=["GET", "POST"])
def logout():
    """Đăng xuất tài khoản nông hộ và dọn dẹp các file audio TTS của phiên."""
    try:
        conv_sid = session.get("conv_session_id")
        if conv_sid:
            conversation_manager.store.remove(conv_sid)

        farmer = session.get("farmer") or {}
        f_id = farmer.get("user_id") or farmer.get("id")
        if f_id:
            conversation_manager.cleanup_farmer_sessions(int(f_id))

        conversation_manager.cleanup_old_tts_files(max_age_seconds=60, keep_latest=0)
    except Exception as e:
        logger.debug("Lỗi dọn dẹp TTS khi logout: %s", e)

    session.clear()
    return redirect(url_for("login"))


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    """Lấy thông tin tài khoản đang đăng nhập."""
    farmer = session.get("farmer")
    if not farmer:
        return jsonify({"status": "unauthorized", "logged_in": False}), 401
    return jsonify({"status": "success", "logged_in": True, "farmer": farmer})


@app.before_request
def refresh_farmer_session():
    """Tự động đồng bộ thông tin nông hộ & vùng trồng mới nhất từ CSDL vào session, đồng thời dọn dẹp phiên hết hạn."""
    global _last_session_cleanup_time
    now = time.time()
    if (now - _last_session_cleanup_time) > 60:
        _last_session_cleanup_time = now
        try:
            conversation_manager.store.cleanup_expired()
            conversation_manager.cleanup_old_tts_files(max_age_seconds=180, keep_latest=0)
        except Exception as _e_clean:
            logger.debug("Lỗi dọn dẹp phiên hết hạn hook: %s", _e_clean)

    if "farmer" in session and session["farmer"]:
        u_id = session["farmer"].get("user_id") or session["farmer"].get("id")
        if u_id:
            try:
                fresh = get_farmer_by_id(u_id)
                if fresh:
                    session["farmer"] = fresh
            except Exception:
                pass


@app.route("/")
@app.route("/nongky")
@app.route("/nongky/")
def index():
    """Trang chính ghi nhật ký (yêu cầu đăng nhập)."""
    farmer = session.get("farmer")
    if not farmer:
        return redirect(url_for("login"))
    return render_template("index.html", farmer=farmer)


@app.route("/them-hoat-dong")
@app.route("/them-hoat-dong/<activity_type>")
@app.route("/them-cong-viec")
@app.route("/them-cong-viec/<activity_type>")
def add_activity(activity_type="auto"):
    """Trang riêng thêm hoạt động canh tác (hỗ trợ AI tự động phân loại hoặc theo loại người dùng chọn)."""
    farmer = session.get("farmer")
    if not farmer:
        return redirect(url_for("login"))
    valid_activities = ["spray", "fertilize", "purchase", "harvest", "sale", "waste", "general", "observation", "auto"]
    if activity_type not in valid_activities:
        activity_type = "auto"
    return render_template("add_activity.html", farmer=farmer, activity_type=activity_type)


@app.route("/diary")
@app.route("/nhat-ky")
@app.route("/so-nhat-ky")
def diary():
    """Trang Sổ nhật ký canh tác (dòng thời gian hoạt động sản xuất VietGAP)."""
    farmer = session.get("farmer")
    if not farmer:
        return redirect(url_for("login"))
    return render_template("diary.html", farmer=farmer)


@app.route("/conversation")
@app.route("/hoi-thoai")
def conversation_page():
    """Trang Trợ lý Hội thoại AI đa lượt (giao diện chat tiếng Việt)."""
    farmer = session.get("farmer")
    if not farmer:
        return redirect(url_for("login"))
    return render_template("conversation.html", farmer=farmer)


@app.route("/ghi-nhat-ky-cu")
@app.route("/nongky-cu")
def classic_diary_entry():
    """Trang ghi nhật ký theo giao diện thu âm và biểu mẫu truyền thống."""
    farmer = session.get("farmer")
    if not farmer:
        return redirect(url_for("login"))
    return render_template("index_classic.html", farmer=farmer)


@app.route("/uploads/<path:filename>")
def serve_uploads(filename):
    """Phục vụ file ảnh/âm thanh tải lên."""
    return send_from_directory(UPLOADS_DIR, filename)


@app.route("/tmp/<path:filename>")
def serve_tmp_audio(filename):
    """Phục vụ file âm thanh TTS tạm thời."""
    tmp_dir = os.path.join(ROOT_DIR, "tmp")
    return send_from_directory(tmp_dir, filename)


# ==============================================================================
# API CHO TRỢ LÝ HỘI THOẠI AI (CONVERSATION)
# ==============================================================================

@app.route("/api/conversation/message", methods=["POST"])
def api_conversation_message():
    """Nhận tin nhắn văn bản từ người dùng và phản hồi qua ConversationManager."""
    try:
        farmer = session.get("farmer")
        farmer_id = (farmer.get("id") or farmer.get("farmer_id") or 1) if farmer else 1
        data = request.get_json() or {}
        text = data.get("text", "").strip()
        session_id = data.get("session_id") or session.get("conv_session_id")
        if not session_id:
            session_id = f"web_{uuid.uuid4().hex[:8]}"
            session["conv_session_id"] = session_id
        enable_tts = bool(data.get("enable_tts", False))

        res = run_async(
            conversation_manager.handle_text(
                session_id=session_id,
                user_text=text,
                farmer_id=farmer_id,
                enable_tts=enable_tts,
            )
        )
        if res.get("audio_file"):
            res["audio_file"] = f"/tmp/{os.path.basename(res['audio_file'])}"
        return jsonify(res)
    except Exception as e:
        logger.exception("Lỗi api_conversation_message: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/conversation/image", methods=["POST"])
def api_conversation_image():
    """Nhận file ảnh tải lên, phân tích VLM và phản hồi qua ConversationManager."""
    try:
        farmer = session.get("farmer")
        farmer_id = (farmer.get("id") or farmer.get("farmer_id") or 1) if farmer else 1
        file = request.files.get("file") or request.files.get("image")
        if not file:
            return jsonify({"status": "error", "message": "Không có file ảnh"}), 400

        session_id = request.form.get("session_id") or session.get("conv_session_id")
        if not session_id:
            session_id = f"web_{uuid.uuid4().hex[:8]}"
            session["conv_session_id"] = session_id
        enable_tts = request.form.get("enable_tts") in ("true", "1", "True")

        ext = os.path.splitext(file.filename)[1] or ".jpg"
        temp_name = f"conv_img_{uuid.uuid4().hex[:10]}{ext}"
        temp_path = os.path.join(UPLOADS_DIR, temp_name)
        file.save(temp_path)

        res = run_async(
            conversation_manager.handle_image(
                session_id=session_id,
                image_path=temp_path,
                farmer_id=farmer_id,
                enable_tts=enable_tts,
            )
        )
        res["web_image_url"] = f"/uploads/{temp_name}"
        if res.get("audio_file"):
            res["audio_file"] = f"/tmp/{os.path.basename(res['audio_file'])}"
        return jsonify(res)
    except Exception as e:
        logger.exception("Lỗi api_conversation_image: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/conversation/audio", methods=["POST"])
def api_conversation_audio():
    """Nhận file âm thanh ghi âm, nhận diện STT và phản hồi qua ConversationManager."""
    try:
        farmer = session.get("farmer")
        farmer_id = (farmer.get("id") or farmer.get("farmer_id") or 1) if farmer else 1
        file = request.files.get("file") or request.files.get("audio")
        if not file:
            return jsonify({"status": "error", "message": "Không có file âm thanh"}), 400

        session_id = request.form.get("session_id") or session.get("conv_session_id")
        if not session_id:
            session_id = f"web_{uuid.uuid4().hex[:8]}"
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
            )
        )
        if res.get("audio_file"):
            res["audio_file"] = f"/tmp/{os.path.basename(res['audio_file'])}"
        return jsonify(res)
    except Exception as e:
        logger.exception("Lỗi api_conversation_audio: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/conversation/reset", methods=["POST"])
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


@app.route("/api/conversation/tts", methods=["POST"])
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


@app.route("/api/conversation/tts/cleanup", methods=["POST"])
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


@app.route("/manager")
@app.route("/admin")
@app.route("/quan-ly")
def manager():
    """Trang Cổng quản lý dành cho người quản lý xem toàn bộ dữ liệu trích xuất."""
    farmer = session.get("farmer")
    if not farmer:
        return redirect(url_for("login"))
    return render_template("manager.html", farmer=farmer)


# ==========================================
# STT LAB: trang cấu hình / test model STT
# ==========================================
STT_LAB_DEFAULTS = {
    # Tiền xử lý audio (ffmpeg)
    "highpass_hz": 0, "lowpass_hz": 0, "denoise": False, "denoise_nr": 12,
    "gain_db": 0.0, "normalize": False, "trim_silence": False,
    "start_sec": 0.0, "end_sec": 0.0, "speed": 1.0,
    # VAD
    "use_vad": True,
    "vad_threshold": config.VAD_THRESHOLD, "vad_min_silence": config.VAD_MIN_SILENCE_SEC,
    "vad_min_speech": config.VAD_MIN_SPEECH_SEC, "vad_max_speech": config.VAD_MAX_SPEECH_SEC,
    "vad_window": config.VAD_WINDOW_SIZE, "vad_padding": 0.0,
    # Recognizer
    "num_threads": config.NUM_THREADS, "decoding_method": "greedy_search", "max_active_paths": 4,
}


@app.route("/stt")
@app.route("/stt/")
@app.route("/stt/config")
@app.route("/stt-config")
@app.route("/stt-lab")
@app.route("/stt-test")
def stt_config():
    """Trang cấu hình & test STT: tham số xử lý, sửa kết quả, lưu và học từ điển (yêu cầu đăng nhập)."""
    farmer = session.get("farmer")
    if not farmer:
        return redirect(url_for("login"))
    return render_template("stt/config.html", farmer=farmer, defaults=STT_LAB_DEFAULTS, active="config")


@app.route("/stt/dashboard")
@app.route("/stt-dashboard")
def stt_dashboard():
    """Dashboard thống kê các file đã test và lưu."""
    farmer = session.get("farmer")
    if not farmer:
        return redirect(url_for("login"))
    return render_template("stt/dashboard.html", farmer=farmer, active="dashboard")


@app.route("/stt/dictionary")
@app.route("/stt-dictionary")
@app.route("/tu-dien-stt")
def stt_dictionary_page():
    """Chuyển hướng trang từ điển cũ sang dashboard."""
    return redirect(url_for("stt_dashboard_page"))


@app.route("/stt/realtime")
@app.route("/stt-realtime")
def stt_realtime_page():
    """Trang test nhận diện giọng nói thời gian thực (Streaming Realtime STT)."""
    farmer = session.get("farmer")
    if not farmer:
        return redirect(url_for("login"))
    return render_template("stt/realtime.html", farmer=farmer, active="realtime")


@app.route("/stt/hotwords")
@app.route("/stt-hotwords")
@app.route("/tu-hotwords")
def stt_hotwords_page():
    """Trang quản lý từ Hot-fix (Hotwords Contextual Biasing) cho mô hình ONNX."""
    farmer = session.get("farmer")
    if not farmer:
        return redirect(url_for("login"))
    return render_template("stt/hotwords.html", farmer=farmer, active="hotwords")


@app.route("/api/stt-hotwords", methods=["GET"])
def api_stt_hotwords_list():
    if not session.get("farmer"):
        return jsonify({"status": "error", "message": "Chưa đăng nhập"}), 401
    search = request.args.get("q")
    category = request.args.get("cat")
    words = hotwords_manager.list_words(search=search, category=category)
    return jsonify({
        "status": "success",
        "words": words,
        "categories": hotwords_manager.get_categories(),
        "stats": hotwords_manager.stats(),
    })


@app.route("/api/stt-hotwords", methods=["POST"])
def api_stt_hotwords_add():
    if not session.get("farmer"):
        return jsonify({"status": "error", "message": "Chưa đăng nhập"}), 401
    d = request.get_json(silent=True) or {}
    word = d.get("word", "")
    category = d.get("category", "Chung")
    ok, msg, item = hotwords_manager.add_word(word, category=category)
    if not ok:
        return jsonify({"status": "error", "message": msg}), 400
    return jsonify({"status": "success", "message": msg, "item": item})


@app.route("/api/stt-hotwords/batch", methods=["POST"])
def api_stt_hotwords_batch():
    if not session.get("farmer"):
        return jsonify({"status": "error", "message": "Chưa đăng nhập"}), 401
    d = request.get_json(silent=True) or {}
    words = d.get("words", "")
    category = d.get("category", "Chung")
    res = hotwords_manager.add_batch(words, category=category)
    return jsonify({"status": "success", **res})


@app.route("/api/stt-hotwords/<path:word_id>", methods=["DELETE"])
def api_stt_hotwords_delete(word_id):
    if not session.get("farmer"):
        return jsonify({"status": "error", "message": "Chưa đăng nhập"}), 401
    ok = hotwords_manager.delete_word(word_id)
    return jsonify({"status": "success" if ok else "error", "message": "" if ok else "Không tìm thấy từ khóa"})


@app.route("/api/stt-hotwords/preview", methods=["GET"])
def api_stt_hotwords_preview():
    word = request.args.get("word", "")
    tokens = hotwords_manager.encode_tokens(word)
    return jsonify({"status": "success", "word": word, "tokens": tokens})


@app.route("/api/stt-hotwords/sync", methods=["POST"])
def api_stt_hotwords_sync():
    if not session.get("farmer"):
        return jsonify({"status": "error", "message": "Chưa đăng nhập"}), 401
    count = hotwords_manager.sync_to_onnx()
    return jsonify({"status": "success", "total_tokens": count})


@app.route("/api/stt-hotwords/export", methods=["GET"])
def api_stt_hotwords_export():
    if not session.get("farmer"):
        return jsonify({"status": "error", "message": "Chưa đăng nhập"}), 401
    if os.path.exists(hotwords_manager.onnx_path):
        return send_file(hotwords_manager.onnx_path, as_attachment=True, download_name="onnx_hotwords.txt")
    return jsonify({"status": "error", "message": "Chưa có file hotwords"}), 404




def _record_audio_name(audio_url: str):
    """Chỉ cho phép file lab_* nằm trong thư mục audio."""
    name = os.path.basename((audio_url or "").split("?")[0])
    if name.startswith(("lab_", "saved_")) and name.endswith(".wav"):
        return name
    return None


@app.route("/api/stt-records", methods=["GET"])
def stt_records_list():
    if not session.get("farmer"):
        return jsonify({"status": "error", "message": "Chưa đăng nhập"}), 401
    return jsonify({"status": "success", "records": stt_records.list()})


@app.route("/api/stt-records/stats", methods=["GET"])
def stt_records_stats():
    if not session.get("farmer"):
        return jsonify({"status": "error", "message": "Chưa đăng nhập"}), 401
    return jsonify({"status": "success", "stats": stt_records.stats()})


@app.route("/api/stt-records", methods=["POST"])
def stt_records_save():
    """Lưu một lần test (kèm bản đã sửa)."""
    if not session.get("farmer"):
        return jsonify({"status": "error", "message": "Chưa đăng nhập"}), 401
    d = request.get_json(silent=True) or {}
    dict_text = (d.get("dict_text") or "").strip()
    final_text = (d.get("final_text") or "").strip()
    raw_text = (d.get("raw_text") or dict_text).strip()
    if not dict_text or not final_text:
        return jsonify({"status": "error", "message": "Thiếu văn bản gốc hoặc văn bản đã sửa"}), 400

    # Tự động trích xuất các từ sửa khác biệt và convert thành hotwords nạp vào ONNX model
    added_hotwords = hotwords_manager.learn_from_correction(dict_text, final_text, category="Tự học từ bản sửa")
    new_words = [it["word"] for it in added_hotwords]
    total_onnx = len(hotwords_manager.list_words())

    fields = {
        "filename": d.get("filename") or "(văn bản dán)",
        "raw_text": raw_text, "dict_text": dict_text, "final_text": final_text,
        "audio_duration": d.get("audio_duration"), "process_time": d.get("process_time") or 0,
        "ffmpeg_time": d.get("ffmpeg_time"), "segments": d.get("segments"),
        "params": d.get("params"),
        "changes": [], "new_vocab": new_words,
    }

    existing = stt_records.get(d["record_id"]) if d.get("record_id") else None
    if existing:
        fields["new_vocab"] = existing.get("new_vocab", []) + new_words
        fields["dict_text"] = existing.get("dict_text", dict_text)
        rec = stt_records.update(existing["id"], fields)
    else:
        src = _record_audio_name(d.get("audio_url"))
        if src and os.path.exists(os.path.join(STATIC_AUDIO_DIR, src)):
            import shutil
            saved = f"saved_{uuid.uuid4().hex[:12]}.wav"
            shutil.copyfile(os.path.join(STATIC_AUDIO_DIR, src), os.path.join(STATIC_AUDIO_DIR, saved))
            fields["audio_url"] = f"/static/audio/{saved}"
        rec = stt_records.add(fields)
    return jsonify({
        "status": "success",
        "record": rec,
        "onnx_updated": bool(added_hotwords),
        "total_onnx_hotwords": total_onnx,
        "learned_hotwords": new_words,
        "learned": {"added_entries": [], "updated_entries": [], "added_vocab": new_words}
    })


@app.route("/api/stt-records/<rec_id>", methods=["DELETE"])
def stt_records_delete(rec_id):
    if not session.get("farmer"):
        return jsonify({"status": "error", "message": "Chưa đăng nhập"}), 401
    rec = stt_records.delete(rec_id)
    if not rec:
        return jsonify({"status": "error", "message": "Không tìm thấy"}), 404
    name = _record_audio_name(rec.get("audio_url"))
    if name and name.startswith("saved_"):
        try:
            os.remove(os.path.join(STATIC_AUDIO_DIR, name))
        except OSError:
            pass
    return jsonify({"status": "success"})


@app.route("/api/stt-records/bulk-delete", methods=["POST"])
def stt_records_bulk_delete():
    if not session.get("farmer"):
        return jsonify({"status": "error", "message": "Chưa đăng nhập"}), 401
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or []
    deleted_count = 0
    for rec_id in ids:
        rec = stt_records.delete(rec_id)
        if rec:
            deleted_count += 1
            name = _record_audio_name(rec.get("audio_url"))
            if name and name.startswith("saved_"):
                try:
                    os.remove(os.path.join(STATIC_AUDIO_DIR, name))
                except OSError:
                    pass
    return jsonify({"status": "success", "deleted": deleted_count})



def _build_ffmpeg_filters(p: dict) -> list:
    f = []
    if p["highpass_hz"] > 0:
        f.append(f"highpass=f={int(p['highpass_hz'])}")
    if p["lowpass_hz"] > 0:
        f.append(f"lowpass=f={int(p['lowpass_hz'])}")
    if p["denoise"]:
        f.append(f"afftdn=nr={p['denoise_nr']}")
    if p["speed"] and abs(p["speed"] - 1.0) > 1e-3:
        sp = min(max(p["speed"], 0.5), 2.0)
        f.append(f"atempo={sp}")
    if p["trim_silence"]:
        f.append("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.2"
                 ",areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.2,areverse")
    if p["gain_db"]:
        f.append(f"volume={p['gain_db']}dB")
    if p["normalize"]:
        f.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    return f


def _parse_lab_params(form) -> dict:
    def _b(k): return str(form.get(k, "")).lower() in ("true", "1", "on", "yes")
    def _f(k, lo=None, hi=None):
        try:
            v = float(form.get(k, STT_LAB_DEFAULTS[k]))
        except (TypeError, ValueError):
            v = float(STT_LAB_DEFAULTS[k])
        if lo is not None: v = max(lo, v)
        if hi is not None: v = min(hi, v)
        return v
    method = form.get("decoding_method", "greedy_search")
    if method not in ("greedy_search", "modified_beam_search"):
        method = "greedy_search"
    window = int(_f("vad_window"))
    if window not in (256, 512, 768, 1024):
        window = 512
    return {
        "highpass_hz": _f("highpass_hz", 0, 4000), "lowpass_hz": _f("lowpass_hz", 0, 8000),
        "denoise": _b("denoise"), "denoise_nr": _f("denoise_nr", 1, 97),
        "gain_db": _f("gain_db", -30, 30), "normalize": _b("normalize"),
        "trim_silence": _b("trim_silence"),
        "start_sec": _f("start_sec", 0), "end_sec": _f("end_sec", 0),
        "speed": _f("speed", 0.5, 2.0),
        "use_vad": _b("use_vad"),
        "vad_threshold": _f("vad_threshold", 0.05, 0.95),
        "vad_min_silence": _f("vad_min_silence", 0.05, 5),
        "vad_min_speech": _f("vad_min_speech", 0.05, 5),
        "vad_max_speech": _f("vad_max_speech", 3, 60),
        "vad_window": window, "vad_padding": _f("vad_padding", 0, 1),
        "num_threads": int(_f("num_threads", 1, 16)), "decoding_method": method,
        "max_active_paths": int(_f("max_active_paths", 1, 16)),
    }


@app.route("/api/stt-lab/defaults", methods=["GET"])
def stt_lab_defaults():
    return jsonify({"status": "success", "defaults": STT_LAB_DEFAULTS})


@app.route("/api/stt-lab/transcribe", methods=["POST"])
def stt_lab_transcribe():
    """Xử lý audio bằng ffmpeg theo tham số, rồi nhận diện với VAD/recognizer tuỳ chỉnh."""
    if not session.get("farmer"):
        return jsonify({"status": "error", "message": "Chưa đăng nhập"}), 401
    audio_file = request.files.get("audio") or request.files.get("file")
    server_path = request.form.get("server_path")
    if not audio_file and not server_path:
        return jsonify({"status": "error", "message": "Không tìm thấy file âm thanh"}), 400

    p = _parse_lab_params(request.form)
    job_id = uuid.uuid4().hex
    out_name = f"lab_{job_id}.wav"
    out_path = os.path.join(STATIC_AUDIO_DIR, out_name)
    keep_raw = False
    if server_path:
        # File nằm sẵn trên server (import thư mục theo đường dẫn) - không xoá sau khi xử lý
        raw_path = _safe_import_path(server_path)
        if not raw_path or not os.path.isfile(raw_path):
            return jsonify({"status": "error", "message": "Đường dẫn không hợp lệ hoặc nằm ngoài thư mục cho phép"}), 400
        keep_raw = True
    else:
        ext = os.path.splitext(audio_file.filename or "")[1].lower()
        if ext not in AUDIO_EXTS:
            ext = ".webm" if "webm" in (audio_file.content_type or "") else ".wav"
        raw_path = os.path.join(UPLOADS_DIR, f"lab_{job_id}_raw{ext}")
        audio_file.save(raw_path)

    try:
        if os.path.getsize(raw_path) < 100:
            return jsonify({"status": "error", "message": "File âm thanh rỗng hoặc quá ngắn."}), 400

        # Dọn file lab cũ (> 6 giờ)
        now = time.time()
        for fn in os.listdir(STATIC_AUDIO_DIR):
            fp = os.path.join(STATIC_AUDIO_DIR, fn)
            if fn.startswith("lab_") and now - os.path.getmtime(fp) > 6 * 3600:
                try: os.remove(fp)
                except OSError: pass

        cmd = ["ffmpeg", "-y"]
        if p["start_sec"] > 0:
            cmd += ["-ss", str(p["start_sec"])]
        if p["end_sec"] > p["start_sec"] and p["end_sec"] > 0:
            cmd += ["-to", str(p["end_sec"])]
        cmd += ["-i", raw_path]
        filters = _build_ffmpeg_filters(p)
        if filters:
            cmd += ["-af", ",".join(filters)]
        cmd += ["-ar", str(config.TARGET_SAMPLE_RATE), "-ac", "1", "-sample_fmt", "s16",
                "-c:a", "pcm_s16le", out_path]

        async def _run_ffmpeg():
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(proc.communicate(), timeout=config.FFMPEG_TIMEOUT_SEC)
            if proc.returncode != 0:
                raise AudioConversionError("ffmpeg lỗi: " + err.decode(errors="ignore")[-300:])

        t0 = time.time()
        run_async(_run_ffmpeg())
        ffmpeg_time = round(time.time() - t0, 2)
        if not os.path.exists(out_path) or os.path.getsize(out_path) < 100:
            return jsonify({"status": "error", "message": "Audio sau xử lý rỗng (kiểm tra start/end/trim)."}), 400

        # Chạy cùng executor 1 worker của engine để tuần tự với các job khác
        result = stt_engine._executor.submit(stt_engine._transcribe_tuned_sync, out_path, p).result()
        result.pop("_spans", None)
        result.pop("_sr", None)
        raw_text = result.get("text", "")
        corrected, applied = raw_text, []

        result.update({
            "status": "success",
            "raw_text": raw_text,
            "corrected_text": raw_text,
            "dictionary_applied": [],
            "ffmpeg_time": ffmpeg_time,
            "ffmpeg_filters": filters,
            "params": p,
            "audio_url": f"/static/audio/{out_name}",
        })
        return jsonify(result)
    except Exception as e:
        logger.exception("STT Lab lỗi: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if not keep_raw:
            try: os.remove(raw_path)
            except OSError: pass


# ----- Import thư mục -----
AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".ogg", ".webm", ".aac", ".flac", ".opus", ".mp4", ".wma")
IMPORT_ROOTS = [os.path.realpath(r) for r in
                os.getenv("STT_IMPORT_ROOTS", f"/mnt/data,{os.path.dirname(APP_DIR)}").split(",") if r.strip()]


def _safe_import_path(path: str):
    """Chỉ cho phép đọc file audio nằm trong các thư mục gốc được cấu hình (STT_IMPORT_ROOTS)."""
    try:
        rp = os.path.realpath(path)
    except (TypeError, ValueError):
        return None
    if not any(rp == r or rp.startswith(r + os.sep) for r in IMPORT_ROOTS):
        return None
    return rp


@app.route("/api/stt-batch/list", methods=["GET"])
def stt_batch_list():
    """Liệt kê file audio trong một thư mục trên server (đệ quy tuỳ chọn)."""
    if not session.get("farmer"):
        return jsonify({"status": "error", "message": "Chưa đăng nhập"}), 401
    path = _safe_import_path(request.args.get("path", ""))
    if not path or not os.path.isdir(path):
        return jsonify({"status": "error", "message": "Thư mục không tồn tại hoặc không được phép. Gốc cho phép: " + ", ".join(IMPORT_ROOTS)}), 400
    recursive = request.args.get("recursive", "1") != "0"
    files = []
    for root, dirs, names in os.walk(path):
        dirs.sort()
        for n in sorted(names):
            if n.lower().endswith(AUDIO_EXTS):
                fp = os.path.join(root, n)
                files.append({"path": fp, "name": os.path.relpath(fp, path), "size": os.path.getsize(fp)})
        if not recursive:
            break
        if len(files) >= 2000:
            break
    return jsonify({"status": "success", "dir": path, "files": files, "roots": IMPORT_ROOTS})





@app.route("/download-cert")
def download_cert():
    """Tải chứng chỉ SSL để cài đặt vào máy (Trusted Root CA)."""
    if os.path.exists(CERT_FILE):
        return send_file(CERT_FILE, as_attachment=True, download_name="nongky-cert.crt")
    return jsonify({"error": "Certificate not found"}), 404


@app.route("/api/health", methods=["GET"])
def health():
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
            "ready": True
        }
    })


AUDIO_STORAGE_DIR = os.getenv("AUDIO_STORAGE_DIR", getattr(config, "AUDIO_STORAGE_DIR", "/mnt/data/audio"))
try:
    os.makedirs(AUDIO_STORAGE_DIR, exist_ok=True)
    STATIC_AUDIO_DIR = AUDIO_STORAGE_DIR
except Exception as e:
    logger.warning("Không thể tạo thư mục %s: %s, fallback về static/audio", AUDIO_STORAGE_DIR, e)
    STATIC_AUDIO_DIR = os.path.join(APP_DIR, "static", "audio")
    os.makedirs(STATIC_AUDIO_DIR, exist_ok=True)


@app.route("/static/audio/<path:filename>")
def serve_static_audio(filename):
    """Phục vụ file âm thanh trực tiếp từ thư mục lưu trữ /mnt/data/audio."""
    return send_from_directory(STATIC_AUDIO_DIR, filename)


STATIC_PHOTOS_DIR = os.path.join(APP_DIR, "static", "photos")
os.makedirs(STATIC_PHOTOS_DIR, exist_ok=True)


@app.route("/static/photos/<path:filename>")
def serve_static_photos(filename):
    """Phục vụ ảnh chụp hoạt động thực địa."""
    return send_from_directory(STATIC_PHOTOS_DIR, filename)


@app.route("/api/upload-photo", methods=["POST"])
def upload_photo():
    """Tải lên ảnh chụp thực địa hoạt động canh tác VietGAP và tùy chọn chạy OCR."""
    photo_file = request.files.get("photo") or request.files.get("image") or request.files.get("file")
    if not photo_file:
        return jsonify({"status": "error", "message": "Không tìm thấy file ảnh"}), 400

    orig_ext = os.path.splitext(photo_file.filename or "")[1].lower()
    if not orig_ext or orig_ext not in [".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp"]:
        orig_ext = ".jpg"

    photo_id = uuid.uuid4().hex
    filename = f"{photo_id}{orig_ext}"
    dest_path = os.path.join(STATIC_PHOTOS_DIR, filename)
    photo_file.save(dest_path)
    photo_url = f"/static/photos/{filename}"

    resp = {"status": "success", "photo_url": photo_url}

    # Nếu người dùng yêu cầu chạy OCR luôn khi upload
    do_ocr = request.args.get("ocr", "").lower() in ("true", "1", "yes") or request.form.get("ocr", "").lower() in ("true", "1", "yes")
    if do_ocr:
        try:
            min_score = float(request.args.get("min_score") or request.form.get("min_score") or 0.5)
            ocr_res = ocr_engine.ocr_sync(dest_path, min_score=min_score)
            resp["ocr"] = {
                "text": ocr_res.text,
                "lines": [l.to_dict() for l in ocr_res.lines],
                "total_lines": ocr_res.total_lines,
                "avg_score": ocr_res.avg_score,
                "timing": ocr_res.to_dict()["timing"],
            }
        except Exception as e:
            logger.warning("OCR khi upload ảnh thất bại: %s", e)
            resp["ocr"] = {"text": "", "error": str(e)}

    # Nếu người dùng yêu cầu chạy VLM (Gemini 3.1 Flash Lite) luôn khi upload
    do_vlm = request.args.get("vlm", "").lower() in ("true", "1", "yes") or request.form.get("vlm", "").lower() in ("true", "1", "yes")
    if do_vlm:
        try:
            task = request.args.get("task") or request.form.get("task") or "auto"
            prompt = request.args.get("prompt") or request.form.get("prompt")
            vlm_res = run_async(vlm_engine.analyze_image(dest_path, prompt=prompt, task=task))
            resp["vlm"] = vlm_res.to_dict()
        except Exception as e_vlm:
            logger.warning("VLM khi upload ảnh thất bại: %s", e_vlm)
            resp["vlm"] = {"status": "error", "error": str(e_vlm)}

    return jsonify(resp)


@app.route("/api/ocr", methods=["POST"])
def ocr_image():
    """
    Nhận diện văn bản từ hình ảnh bằng RapidOCR.
    Hỗ trợ nhận file upload (multipart/form-data) hoặc đường dẫn photo_url (JSON).
    Hỗ trợ tùy chọn vẽ ảnh trực quan (vis=true) và trích xuất cấu trúc VietGAP (extract=true).
    """
    photo_file = request.files.get("photo") or request.files.get("image") or request.files.get("file")
    photo_url = ""
    target_img_input = None

    if photo_file:
        orig_ext = os.path.splitext(photo_file.filename or "")[1].lower()
        if not orig_ext or orig_ext not in [".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp"]:
            orig_ext = ".jpg"
        photo_id = uuid.uuid4().hex
        filename = f"{photo_id}{orig_ext}"
        dest_path = os.path.join(STATIC_PHOTOS_DIR, filename)
        photo_file.save(dest_path)
        photo_url = f"/static/photos/{filename}"
        target_img_input = dest_path
    else:
        data = request.get_json(silent=True) or request.form or {}
        photo_url = data.get("photo_url") or data.get("image_url") or ""
        if photo_url:
            clean_name = os.path.basename(photo_url.split("?")[0])
            local_path = os.path.join(STATIC_PHOTOS_DIR, clean_name)
            if os.path.exists(local_path):
                target_img_input = local_path
            else:
                target_img_input = photo_url

    if not target_img_input:
        return jsonify({"status": "error", "message": "Vui lòng đính kèm file ảnh hoặc cung cấp photo_url"}), 400

    # Lấy tham số cấu hình
    min_score = float(request.args.get("min_score") or (request.get_json(silent=True) or {}).get("min_score") or request.form.get("min_score") or 0.5)
    do_vis = request.args.get("vis", "").lower() in ("true", "1", "yes") or (request.get_json(silent=True) or {}).get("vis") is True
    do_extract = request.args.get("extract", "").lower() in ("true", "1", "yes") or (request.get_json(silent=True) or {}).get("extract") is True
    do_match = request.args.get("match", "").lower() in ("true", "1", "yes") or (request.get_json(silent=True) or {}).get("match") is True

    try:
        ocr_res = ocr_engine.ocr_sync(target_img_input, min_score=min_score)

        vis_url = None
        if do_vis and ocr_res.lines and isinstance(target_img_input, str) and os.path.exists(target_img_input):
            vis_filename = f"vis_{os.path.basename(target_img_input)}"
            vis_dest = os.path.join(STATIC_PHOTOS_DIR, vis_filename)
            try:
                ocr_res.save_visualization(target_img_input, vis_dest)
                vis_url = f"/static/photos/{vis_filename}"
            except Exception as e_vis:
                logger.warning("Không tạo được ảnh visualization: %s", e_vis)

        # Trích xuất và fuzzy match tên thuốc / phân bón với CSDL (sử dụng LLM 0.6B)
        matched_materials = []
        if (do_match or do_extract) and ocr_res.text:
            try:
                match_results = run_async(material_matcher.extract_and_match(ocr_res.text))
                matched_materials = [m.to_dict() for m in match_results]
            except Exception as e_mat:
                logger.warning("Lỗi trích xuất & fuzzy match vật tư: %s", e_mat)

        vietgap_data = None
        if do_extract and ocr_res.text:
            try:
                # Gửi văn bản OCR sang pipeline trích xuất VietGAP
                from services.engines.gemini_engine import gemini_engine
                from services.engines.llm_engine import llm_engine
                from services.vietgap.regex_parser import parse_with_regex

                default_eng = getattr(config, "DEFAULT_EXTRACTION_ENGINE", "gemini").lower()
                if default_eng == "gemini" and bool(config.GEMINI_API_KEY):
                    vietgap_data = run_async(gemini_engine.extract_activities(ocr_res.text))
                elif config.LLM_ENABLED:
                    vietgap_data = run_async(llm_engine.extract_activities(ocr_res.text))
                else:
                    vietgap_data = parse_with_regex(ocr_res.text)

                if vietgap_data and isinstance(vietgap_data, dict):
                    vietgap_data = apply_normalizer(vietgap_data)
            except Exception as e_ext:
                logger.warning("Lỗi trích xuất VietGAP từ văn bản OCR: %s", e_ext)

        return jsonify({
            "status": "success",
            "photo_url": photo_url,
            "vis_url": vis_url,
            "text": ocr_res.text,
            "lines": [l.to_dict() for l in ocr_res.lines],
            "total_lines": ocr_res.total_lines,
            "avg_score": ocr_res.avg_score,
            "timing": ocr_res.to_dict()["timing"],
            "matched_materials": matched_materials,
            "vietgap_data": vietgap_data,
        })

    except OCRError as e:
        logger.exception("Lỗi nhận diện OCR: %s", e)
        return jsonify({"status": "error", "message": f"Lỗi OCR: {e}"}), 500
    except Exception as e:
        logger.exception("Lỗi xử lý OCR không mong muốn: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/match-material", methods=["POST"])
def api_match_material():
    """
    Sử dụng LLM 0.6B (Port 8081) để trích xuất tên thuốc/phân bón từ văn bản và fuzzy match với CSDL.
    Payload: {"text": "..."} hoặc {"name": "..."}
    """
    data = request.get_json(silent=True) or request.form or {}
    text = data.get("text") or data.get("name") or request.args.get("text") or request.args.get("name") or ""
    min_score = float(data.get("min_score") or request.args.get("min_score") or 60.0)

    if not text.strip():
        return jsonify({"status": "error", "message": "Nội dung văn bản (text/name) rỗng."}), 400

    try:
        results = run_async(material_matcher.extract_and_match(text, min_score=min_score))
        return jsonify({
            "status": "success",
            "input_text": text,
            "total_found": len(results),
            "matched_materials": [m.to_dict() for m in results],
        })
    except Exception as e:
        logger.exception("Lỗi khi xử lý match material: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/vlm-analyze", methods=["POST"])
def api_vlm_analyze():
    """
    Phân tích hình ảnh bằng VLM Engine (Gemini 3.1 Flash Lite):
    Nhận diện sâu bệnh hại, tên thuốc BVTV, phân bón và liên kết CSDL VietGAP.
    Hỗ trợ nhận file upload (multipart/form-data) hoặc photo_url / base64 trong JSON.
    """
    photo_file = request.files.get("photo") or request.files.get("image") or request.files.get("file")
    target_img_input = None
    photo_url = ""

    if photo_file:
        orig_ext = os.path.splitext(photo_file.filename or "")[1].lower()
        if not orig_ext or orig_ext not in [".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp"]:
            orig_ext = ".jpg"
        photo_id = uuid.uuid4().hex
        filename = f"{photo_id}{orig_ext}"
        dest_path = os.path.join(STATIC_PHOTOS_DIR, filename)
        photo_file.save(dest_path)
        photo_url = f"/static/photos/{filename}"
        target_img_input = dest_path
    else:
        data = request.get_json(silent=True) or request.form or {}
        photo_url = data.get("photo_url") or data.get("image_url") or ""
        b64_data = data.get("image_base64") or data.get("base64") or ""
        if b64_data:
            target_img_input = b64_data
        elif photo_url:
            clean_name = os.path.basename(photo_url.split("?")[0])
            local_path = os.path.join(STATIC_PHOTOS_DIR, clean_name)
            if os.path.exists(local_path):
                target_img_input = local_path
            else:
                target_img_input = photo_url

    if not target_img_input:
        return jsonify({"status": "error", "message": "Vui lòng đính kèm file ảnh hoặc cung cấp photo_url / base64"}), 400

    data = request.get_json(silent=True) or request.form or {}
    task = request.args.get("task") or data.get("task") or "auto"
    prompt = request.args.get("prompt") or data.get("prompt")
    model = request.args.get("model") or data.get("model")
    match_db = str(request.args.get("match_db") or data.get("match_db") or "true").lower() in ("true", "1", "yes")

    try:
        res = run_async(vlm_engine.analyze_image(
            image_input=target_img_input,
            prompt=prompt,
            task=task,
            model=model,
            match_db=match_db,
        ))
        resp_data = res.to_dict()
        resp_data["photo_url"] = photo_url
        return jsonify(resp_data)
    except Exception as e:
        logger.exception("Lỗi phân tích VLM: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/vlm-add-activity", methods=["POST"])
def api_vlm_add_activity():
    """
    Phân tích hình ảnh bằng VLM Engine (Gemini 3.1 Flash Lite) và tự động
    lưu bản ghi hoạt động tương ứng vào CSDL VietGAP:
    - 4 hoạt động:
      1. purchase: Mua vật tư (lưu vào input_purchases)
      2. spray: Phun thuốc BVTV (lưu vào cultivation_activities + pesticide_applications)
      3. fertilize: Bón phân (lưu vào cultivation_activities + fertilizer_applications)
      4. observation: Quan sát sâu bệnh (lưu vào cultivation_activities + pest_observations)
    Hỗ trợ nhận file upload (multipart/form-data) hoặc photo_url / base64 trong JSON.
    """
    photo_file = request.files.get("photo") or request.files.get("image") or request.files.get("file")
    target_img_input = None
    photo_url = ""

    if photo_file:
        orig_ext = os.path.splitext(photo_file.filename or "")[1].lower()
        if not orig_ext or orig_ext not in [".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp"]:
            orig_ext = ".jpg"
        photo_id = uuid.uuid4().hex
        filename = f"{photo_id}{orig_ext}"
        dest_path = os.path.join(STATIC_PHOTOS_DIR, filename)
        photo_file.save(dest_path)
        photo_url = f"/static/photos/{filename}"
        target_img_input = dest_path
    else:
        data = request.get_json(silent=True) or request.form or {}
        photo_url = data.get("photo_url") or data.get("image_url") or ""
        b64_data = data.get("image_base64") or data.get("base64") or ""
        if b64_data:
            target_img_input = b64_data
        elif photo_url:
            clean_name = os.path.basename(photo_url.split("?")[0])
            local_path = os.path.join(STATIC_PHOTOS_DIR, clean_name)
            if os.path.exists(local_path):
                target_img_input = local_path
            else:
                target_img_input = photo_url

    if not target_img_input:
        return jsonify({"status": "error", "message": "Vui lòng đính kèm file ảnh hoặc cung cấp photo_url / base64"}), 400

    data = request.get_json(silent=True) or request.form or {}
    intent = request.args.get("intent") or data.get("intent") or "auto"
    farmer_id = session.get("user_id") or request.args.get("farmer_id") or data.get("farmer_id")
    crop_name = request.args.get("crop_name") or data.get("crop_name")
    parcel_id = request.args.get("parcel_id") or request.args.get("plot_id") or data.get("parcel_id") or data.get("plot_id")
    activity_date = request.args.get("activity_date") or request.args.get("date") or data.get("activity_date") or data.get("date")
    prompt = request.args.get("prompt") or data.get("prompt")
    task = request.args.get("task") or data.get("task") or "auto"
    model = request.args.get("model") or data.get("model")
    match_db = str(request.args.get("match_db") or data.get("match_db") or "true").lower() in ("true", "1", "yes")

    extra_fields = data.get("extra_fields") if isinstance(data.get("extra_fields"), dict) else None

    try:
        res = run_async(vlm_engine.analyze_image(
            image_input=target_img_input,
            prompt=prompt,
            task=task,
            model=model,
            match_db=match_db,
        ))

        if res.status != "success":
            return jsonify({
                "status": "error",
                "message": f"Lỗi phân tích VLM: {res.error}",
                "analysis": res.to_dict(),
            }), 400

        db_res = res.save_to_database(
            farmer_id=int(farmer_id) if farmer_id else None,
            intent=intent,
            photo_url=photo_url,
            parcel_id=parcel_id,
            crop_name=crop_name,
            activity_date=activity_date,
            extra_fields=extra_fields,
        )

        analysis_dict = res.to_dict()
        analysis_dict["photo_url"] = photo_url

        if db_res.get("status") == "error":
            return jsonify({
                "status": "error",
                "message": db_res.get("message", "Lỗi lưu sổ nhật ký"),
                "db_result": db_res,
                "analysis": analysis_dict,
            }), 500

        return jsonify({
            "status": "success",
            "message": "Đã phân tích ảnh và thêm hoạt động vào nhật ký VietGAP thành công",
            "intent": intent,
            "photo_url": photo_url,
            "db_result": db_res,
            "analysis": analysis_dict,
        })

    except Exception as e:
        logger.exception("Lỗi khi thêm hoạt động từ VLM: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500




@app.route("/api/stt", methods=["POST"])
def transcribe_audio():
    """Nhận file âm thanh và nhận diện giọng nói bằng services.stt_engine, lưu audio để nghe lại."""
    audio_file = request.files.get("audio") or request.files.get("file")
    if not audio_file:
        return jsonify({"status": "error", "message": "Không tìm thấy file âm thanh"}), 400

    job_id = uuid.uuid4().hex
    
    # Giữ phần mở rộng file để ffmpeg nhận diện chính xác định dạng container
    orig_ext = os.path.splitext(audio_file.filename or "")[1].lower()
    if not orig_ext or orig_ext not in [".wav", ".mp3", ".m4a", ".ogg", ".webm", ".aac", ".flac"]:
        content_type = (audio_file.content_type or "").lower()
        if "webm" in content_type:
            orig_ext = ".webm"
        elif "ogg" in content_type:
            orig_ext = ".ogg"
        elif "mp4" in content_type or "m4a" in content_type:
            orig_ext = ".m4a"
        else:
            orig_ext = ".wav"

    raw_path = os.path.join(UPLOADS_DIR, f"{job_id}_raw{orig_ext}")
    audio_filename = f"{job_id}.wav"
    permanent_wav_path = os.path.join(STATIC_AUDIO_DIR, audio_filename)

    audio_file.save(raw_path)

    if not os.path.exists(raw_path) or os.path.getsize(raw_path) < 100:
        return jsonify({"status": "error", "message": "File âm thanh rỗng hoặc thời lượng quá ngắn, vui lòng thử lại."}), 400

    try:
        run_async(resample_to_16k_mono_pcm16(raw_path, permanent_wav_path))
        text = stt_engine._transcribe_sync(permanent_wav_path)
        audio_url = f"/static/audio/{audio_filename}"
        return jsonify({
            "status": "success",
            "text": text,
            "raw_text": text,
            "audio_url": audio_url
        })
    except Exception as e:
        logger.exception("Lỗi khi xử lý giọng nói với services: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if os.path.exists(raw_path):
            try:
                os.remove(raw_path)
            except OSError:
                pass


# ==========================================
# REALTIME STT & NORMALIZER API ENDPOINTS
# ==========================================

@app.route("/api/stt/realtime/start", methods=["POST"])
def realtime_stt_start():
    """Bắt đầu một phiên nhận diện streaming realtime mới."""
    body = request.get_json(silent=True) or {}
    custom_session_id = body.get("session_id") or request.headers.get("X-Session-ID")
    session = realtime_stt_engine.create_session(session_id=custom_session_id)
    return jsonify({
        "status": "success",
        "session_id": session.session_id,
        "sample_rate": session.sample_rate,
        "message": "Phiên streaming realtime đã sẵn sàng."
    })


@app.route("/api/stt/realtime/feed", methods=["POST"])
def realtime_stt_feed():
    """
    Nạp chunk âm thanh (PCM16 16kHz mono) vào phiên streaming.
    Hỗ trợ nạp binary trong body hoặc multipart/form-data.
    Trả về các sự kiện partial/final mới nhất kèm thực thể đã chuẩn hoá.
    """
    session_id = (
        request.headers.get("X-Session-ID")
        or request.args.get("session_id")
        or (request.form.get("session_id") if request.form else None)
    )
    if not session_id:
        return jsonify({"status": "error", "message": "Thiếu session_id (header X-Session-ID hoặc query parameter)"}), 400

    session = realtime_stt_engine.get_session(session_id)
    if not session:
        return jsonify({"status": "error", "message": f"Session {session_id} không tồn tại hoặc đã kết thúc"}), 404

    # Lấy dữ liệu audio bytes
    chunk_bytes = None
    if request.data:
        chunk_bytes = request.data
    elif "chunk" in request.files:
        chunk_bytes = request.files["chunk"].read()
    elif "file" in request.files:
        chunk_bytes = request.files["file"].read()

    if not chunk_bytes:
        return jsonify({"status": "success", "session_id": session_id, "events": []})

    preserve_case = request.args.get("preserve_case", "true").lower() in ("true", "1")
    events = session.feed_audio(chunk_bytes, preserve_case=preserve_case)

    return jsonify({
        "status": "success",
        "session_id": session_id,
        "events": [ev.to_dict() for ev in events]
    })


@app.route("/api/stt/realtime/finish", methods=["POST"])
def realtime_stt_finish():
    """
    Kết thúc phiên streaming, xả buffer VAD và trả về toàn bộ kết quả tổng hợp.
    """
    body = request.get_json(silent=True) or {}
    session_id = (
        body.get("session_id")
        or request.headers.get("X-Session-ID")
        or request.args.get("session_id")
    )
    if not session_id:
        return jsonify({"status": "error", "message": "Thiếu session_id"}), 400

    session = realtime_stt_engine.get_session(session_id)
    if not session:
        return jsonify({"status": "error", "message": f"Session {session_id} không tồn tại hoặc đã kết thúc"}), 404

    preserve_case = body.get("preserve_case", True)
    events = session.finish(preserve_case=preserve_case)
    realtime_stt_engine.close_session(session_id)

    complete_ev = next((ev for ev in events if ev.event_type == "complete"), None)
    return jsonify({
        "status": "success",
        "session_id": session_id,
        "events": [ev.to_dict() for ev in events],
        "result": complete_ev.to_dict() if complete_ev else None
    })


@app.route("/api/stt/realtime/info", methods=["GET"])
def realtime_stt_info():
    """Trả về cấu hình và URL kết nối WebSocket cho Realtime STT Engine."""
    ws_port = getattr(config, "WS_PORT", 8765)
    ws_enabled = getattr(config, "WS_ENABLED", True)
    is_ssl = (
        request.is_secure
        or request.scheme == "https"
        or request.headers.get("X-Forwarded-Proto") == "https"
    )
    protocol = "wss" if is_ssl else "ws"
    host = request.host.split(":")[0]
    return jsonify({
        "status": "success",
        "ws_enabled": ws_enabled,
        "ws_url": f"{protocol}://{host}:{ws_port}",
        "ws_port": ws_port,
        "protocol": protocol,
        "sample_rate": config.TARGET_SAMPLE_RATE,
    })





@app.route("/api/extract", methods=["POST"])
def extract_data():
    """
    Trích xuất văn bản bằng services.gemini_engine hoặc services.llm_engine và TỰ ĐỘNG chuẩn hoá theo schema CSDL VietGAP.
    """
    body = request.get_json(silent=True) or {}
    text = body.get("text", "").strip()
    job_type = body.get("job_type")
    if str(job_type or "").strip().lower() in ("auto", "all", "none", "", "null", "undefined"):
        job_type = None
    log_date = body.get("log_date") or request.args.get("log_date")
    default_engine = getattr(config, "DEFAULT_EXTRACTION_ENGINE", "gemini").lower()
    engine_param = (body.get("engine") or request.args.get("engine") or default_engine).lower()

    if not text:
        return jsonify({"status": "error", "message": "Nội dung văn bản rỗng"}), 400

    farmer = session.get("farmer")
    farmer_id = farmer.get("id") if farmer else None
    season_id = farmer.get("season_id") if farmer else None

    try:
        use_gemini = (
            engine_param == "gemini" or
            (not config.LLM_ENABLED and bool(config.GEMINI_API_KEY))
        )

        if use_gemini and config.GEMINI_API_KEY:
            try:
                llm_res = run_async(gemini_engine.extract_info(
                    text, job_type=job_type, log_date=log_date,
                    farmer_id=farmer_id, season_id=season_id, auto_update_stage=True
                ))
                model_display = f"Google Gemini ({llm_res.get('model')})"
            except Exception as gemini_err:
                logger.warning("Google Gemini gặp sự cố, tự động fallback sang Local LLM Engine: %s", gemini_err)
                llm_res = run_async(llm_engine.extract_info(text, job_type=job_type, log_date=log_date))
                model_display = f"Local LLM ({llm_res.get('model')}) [Fallback]"
        else:
            try:
                llm_res = run_async(llm_engine.extract_info(text, job_type=job_type, log_date=log_date))
                model_display = f"Local LLM ({llm_res.get('model')})"
            except Exception as llm_err:
                if config.GEMINI_API_KEY:
                    logger.warning("Local LLM gặp sự cố, tự động fallback sang Gemini Engine: %s", llm_err)
                    llm_res = run_async(gemini_engine.extract_info(
                        text, job_type=job_type, log_date=log_date,
                        farmer_id=farmer_id, season_id=season_id, auto_update_stage=True
                    ))
                    model_display = f"Google Gemini ({llm_res.get('model')}) [Fallback]"
                else:
                    raise

        logger.info("📥 [API /api/extract] Phản hồi từ Engine (%s):\n%s", model_display, json.dumps(llm_res.get("tables", {}), ensure_ascii=False, indent=2))
        classified_tables = llm_res.get("tables", {})
        final_data = llm_res.get("data", {})
        extracted_fields = llm_res.get("extracted_fields", {})
        active_tables = llm_res.get("active_tables", list(classified_tables.keys()))
        activities = llm_res.get("activities", {})
        active_activities = llm_res.get("active_activities", [])
        active_tasks = llm_res.get("active_tasks", active_activities)

        detected_stage = llm_res.get("growth_stage") or detect_growth_stage_from_text(text)
        if not detected_stage and isinstance(extracted_fields, dict):
            detected_stage = extracted_fields.get("growth_stage") or extracted_fields.get("giai_doan")

        is_stage_only = llm_res.get("is_stage_only", False)
        stage_updated = llm_res.get("stage_updated", False)

        # Fallback kiểm tra nếu engine khác chưa đánh dấu is_stage_only
        if not is_stage_only and detected_stage:
            has_real_act = False
            for a_key in active_activities:
                a_val = activities.get(a_key)
                if isinstance(a_val, dict):
                    real_vals = [
                        v for k, v in a_val.items()
                        if v and str(v).strip() not in ("", "null", "None")
                        and k not in ("activity_date", "purchase_date", "harvest_date", "sale_date", "treatment_date", "growth_stage", "giai_doan", "giai_doan_sinh_truong", "notes", "description")
                        and not (k == "cong_viec" and str(v).strip().lower() in ("thăm vườn", "thăm đồng", "kiểm tra vườn", "quan sát") and not any(w in text.lower() for w in ("thăm", "kiểm tra", "quan sát")))
                    ]
                    if real_vals:
                        has_real_act = True
                        break
            if not has_real_act:
                is_stage_only = True

        # Khi chỉ có trường giai đoạn thì TỰ ĐỘNG CẬP NHẬT giai đoạn và phản hồi ngay
        if is_stage_only and detected_stage:
            if not stage_updated:
                try:
                    update_season_growth_stage(detected_stage, farmer_id=farmer_id, season_id=season_id)
                    stage_updated = True
                except Exception as e:
                    logger.warning("Lỗi cập nhật giai đoạn vào MySQL: %s", e)

            if "farmer" in session and session["farmer"]:
                session["farmer"]["growth_stage"] = detected_stage
                session.modified = True

            return jsonify({
                "status": "success",
                "is_stage_only": True,
                "stage_updated": stage_updated,
                "growth_stage": detected_stage,
                "message": llm_res.get("message") or f"Đã tự động cập nhật giai đoạn mùa vụ thành '{detected_stage}'",
                "activities": {},
                "active_activities": [],
                "active_tasks": [],
                "tables": {},
                "data": {"growth_stage": detected_stage, "giai_doan": detected_stage},
                "extracted_fields": {"growth_stage": detected_stage, "giai_doan": detected_stage},
                "model": model_display,
                "latency_s": round(llm_res.get("latency_s", 0), 2),
                "tokens_per_sec": round(llm_res.get("tokens_per_sec", 0), 1),
                "auto_normalized": True
            })

        # Bổ sung growth_stage vào các activities nếu phát hiện được
        if detected_stage and isinstance(activities, dict):
            for a_key, a_fields in activities.items():
                if isinstance(a_fields, dict) and not a_fields.get("growth_stage") and not a_fields.get("giai_doan"):
                    a_fields["growth_stage"] = detected_stage

        raw_resp = llm_res.get("raw_response")
        return jsonify({
            "status": "success",
            "is_stage_only": False,
            "stage_updated": False,
            "activities": activities,
            "active_activities": active_activities,
            "active_tasks": active_tasks,
            "tables": classified_tables,
            "extracted_fields": extracted_fields,
            "active_tables": active_tables,
            "data": final_data,
            "growth_stage": detected_stage,
            "raw_response": raw_resp,
            "raw_llm_output": raw_resp,
            "llm_meta": {
                "model": model_display,
                "latency_s": round(llm_res.get("latency_s", 0), 2),
                "tokens_per_sec": round(llm_res.get("tokens_per_sec", 0), 1),
                "prompt_tokens": llm_res.get("prompt_tokens"),
                "completion_tokens": llm_res.get("completion_tokens"),
                "raw_response": raw_resp,
                "engine": "gemini" if "Gemini" in model_display else "local_rkllm",
            },
            "model": model_display,
            "latency_s": round(llm_res.get("latency_s", 0), 2),
            "tokens_per_sec": round(llm_res.get("tokens_per_sec", 0), 1),
            "auto_normalized": True
        })
    except Exception as e:
        logger.exception("Lỗi khi trích xuất thông tin: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/normalize", methods=["POST"])
def normalize_data():
    """API chuẩn hoá dữ liệu qua services.normalizer."""
    body = request.get_json(silent=True) or {}
    input_data = body.get("data") or body
    if not input_data or not isinstance(input_data, dict):
        return jsonify({"status": "error", "message": "Dữ liệu cần chuẩn hoá không hợp lệ"}), 400

    try:
        cleaned = apply_normalizer(input_data)
        classified_tables = classify_into_tables(input_data if "cultivation_activities" in input_data else cleaned)
        return jsonify({
            "status": "success",
            "tables": classified_tables,
            "data": cleaned
        })
    except Exception as e:
        logger.exception("Lỗi khi chuẩn hoá qua services.normalizer: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/voice-to-data", methods=["POST"])
def voice_to_data():
    """Pipeline hoàn chỉnh: Nhận diện giọng nói STT -> Trích xuất Gemini Engine -> Chuẩn hoá VietGAP."""
    audio_file = request.files.get("audio") or request.files.get("file")

    if not audio_file:
        return jsonify({"status": "error", "message": "Không tìm thấy file âm thanh"}), 400

    job_id = uuid.uuid4().hex
    raw_path = os.path.join(UPLOADS_DIR, f"{job_id}_raw")
    audio_filename = f"{job_id}.wav"
    permanent_wav_path = os.path.join(STATIC_AUDIO_DIR, audio_filename)

    audio_file.save(raw_path)

    try:
        run_async(resample_to_16k_mono_pcm16(raw_path, permanent_wav_path))
        text = stt_engine._transcribe_sync(permanent_wav_path)
        audio_url = f"/static/audio/{audio_filename}"
        if not text:
            return jsonify({
                "status": "warning",
                "text": "",
                "audio_url": audio_url,
                "message": "Không nhận diện được giọng nói trong bản ghi.",
                "data": None,
                "tables": None
            })

        default_engine = getattr(config, "DEFAULT_EXTRACTION_ENGINE", "gemini").lower()
        engine_param = (request.form.get("engine") or request.args.get("engine") or default_engine).lower()
        job_type = request.form.get("job_type") or request.args.get("job_type")
        if str(job_type or "").strip().lower() in ("auto", "all", "none", "", "null", "undefined"):
            job_type = None
        log_date = request.form.get("log_date") or request.args.get("log_date")
        use_gemini = (
            engine_param == "gemini" or
            (not config.LLM_ENABLED and bool(config.GEMINI_API_KEY))
        )

        farmer = session.get("farmer")
        farmer_id = farmer.get("id") if farmer else None
        season_id = farmer.get("season_id") if farmer else None

        if use_gemini and config.GEMINI_API_KEY:
            try:
                llm_res = run_async(gemini_engine.extract_info(
                    text, job_type=job_type, log_date=log_date,
                    farmer_id=farmer_id, season_id=season_id, auto_update_stage=True
                ))
                model_display = f"Google Gemini ({llm_res.get('model')})"
            except Exception as gemini_err:
                logger.warning("Google Gemini gặp sự cố, tự động fallback sang Local LLM Engine: %s", gemini_err)
                llm_res = run_async(llm_engine.extract_info(text, job_type=job_type, log_date=log_date))
                model_display = f"Local LLM ({llm_res.get('model')}) [Fallback]"
        else:
            try:
                llm_res = run_async(llm_engine.extract_info(text, job_type=job_type, log_date=log_date))
                model_display = f"Local LLM ({llm_res.get('model')})"
            except Exception as llm_err:
                if config.GEMINI_API_KEY:
                    logger.warning("Local LLM gặp sự cố, tự động fallback sang Gemini Engine: %s", llm_err)
                    llm_res = run_async(gemini_engine.extract_info(
                        text, job_type=job_type, log_date=log_date,
                        farmer_id=farmer_id, season_id=season_id, auto_update_stage=True
                    ))
                    model_display = f"Google Gemini ({llm_res.get('model')}) [Fallback]"
                else:
                    raise

        logger.info("📥 [API /api/voice-to-data] Phản hồi từ Engine (%s):\n%s", model_display, json.dumps(llm_res.get("tables", {}), ensure_ascii=False, indent=2))
        classified_tables = llm_res.get("tables", {})
        final_data = llm_res.get("data", {})
        extracted_fields = llm_res.get("extracted_fields", {})
        active_tables = llm_res.get("active_tables", list(classified_tables.keys()))
        activities = llm_res.get("activities", {})
        active_activities = llm_res.get("active_activities", [])
        active_tasks = llm_res.get("active_tasks", active_activities)

        detected_stage = llm_res.get("growth_stage") or detect_growth_stage_from_text(text)
        if not detected_stage and isinstance(extracted_fields, dict):
            detected_stage = extracted_fields.get("growth_stage") or extracted_fields.get("giai_doan")

        is_stage_only = llm_res.get("is_stage_only", False)
        stage_updated = llm_res.get("stage_updated", False)

        # Fallback kiểm tra nếu engine chưa đánh dấu is_stage_only
        if not is_stage_only and detected_stage:
            has_real_act = False
            for a_key in active_activities:
                a_val = activities.get(a_key)
                if isinstance(a_val, dict):
                    real_vals = [
                        v for k, v in a_val.items()
                        if v and str(v).strip() not in ("", "null", "None")
                        and k not in ("activity_date", "purchase_date", "harvest_date", "sale_date", "treatment_date", "growth_stage", "giai_doan", "giai_doan_sinh_truong", "notes", "description")
                        and not (k == "cong_viec" and str(v).strip().lower() in ("thăm vườn", "thăm đồng", "kiểm tra vườn", "quan sát") and not any(w in text.lower() for w in ("thăm", "kiểm tra", "quan sát")))
                    ]
                    if real_vals:
                        has_real_act = True
                        break
            if not has_real_act:
                is_stage_only = True

        if is_stage_only and detected_stage:
            if not stage_updated:
                try:
                    update_season_growth_stage(detected_stage, farmer_id=farmer_id, season_id=season_id)
                    stage_updated = True
                except Exception as e:
                    logger.warning("Lỗi cập nhật giai đoạn vào MySQL: %s", e)

            if "farmer" in session and session["farmer"]:
                session["farmer"]["growth_stage"] = detected_stage
                session.modified = True

            return jsonify({
                "status": "success",
                "text": text,
                "audio_url": audio_url,
                "is_stage_only": True,
                "stage_updated": stage_updated,
                "growth_stage": detected_stage,
                "message": llm_res.get("message") or f"Đã tự động cập nhật giai đoạn mùa vụ thành '{detected_stage}'",
                "activities": {},
                "active_activities": [],
                "active_tasks": [],
                "tables": {},
                "data": {"growth_stage": detected_stage, "giai_doan": detected_stage},
                "extracted_fields": {"growth_stage": detected_stage, "giai_doan": detected_stage},
                "model": model_display,
                "latency_s": round(llm_res.get("latency_s", 0), 2),
                "tokens_per_sec": round(llm_res.get("tokens_per_sec", 0), 1),
                "auto_normalized": True
            })

        raw_resp = llm_res.get("raw_response")
        return jsonify({
            "status": "success",
            "text": text,
            "audio_url": audio_url,
            "is_stage_only": False,
            "stage_updated": False,
            "activities": activities,
            "active_activities": active_activities,
            "active_tasks": active_tasks,
            "tables": classified_tables,
            "extracted_fields": extracted_fields,
            "active_tables": active_tables,
            "data": final_data,
            "growth_stage": detected_stage,
            "raw_response": raw_resp,
            "raw_llm_output": raw_resp,
            "llm_meta": {
                "model": model_display,
                "latency_s": round(llm_res.get("latency_s", 0), 2),
                "tokens_per_sec": round(llm_res.get("tokens_per_sec", 0), 1),
                "prompt_tokens": llm_res.get("prompt_tokens"),
                "completion_tokens": llm_res.get("completion_tokens"),
                "raw_response": raw_resp,
                "engine": "gemini" if "Gemini" in model_display else "local_rkllm",
            },
            "model": model_display,
            "latency_s": round(llm_res.get("latency_s", 0), 2),
            "tokens_per_sec": round(llm_res.get("tokens_per_sec", 0), 1),
            "auto_normalized": True
        })
    except Exception as e:
        logger.exception("Lỗi trong pipeline voice-to-data: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500

    finally:
        if os.path.exists(raw_path):
            try:
                os.remove(raw_path)
            except OSError:
                pass


# ==========================================
# CÁC API NHẬT KÝ CANH TÁC (VIETGAP DIARY API)
# ==========================================

@app.route("/api/logs", methods=["GET"])
@app.route("/api/diary", methods=["GET"])
def get_logs():
    """Lấy danh sách nhật ký trực tiếp từ CSDL MySQL vietgap_diary."""
    farmer = session.get("farmer")
    farmer_id = farmer.get("id") if farmer else None
    table_filter = request.args.get("table")
    limit = int(request.args.get("limit", 100))

    try:
        tables = get_diary_by_tables(farmer_id=farmer_id, limit=limit)
        flat_logs = get_diary_records(farmer_id=farmer_id, limit=limit)

        if table_filter and table_filter in tables:
            filtered_logs = tables[table_filter]
            return jsonify({
                "status": "success",
                "source": "mysql_local",
                "database": config.MYSQL_DATABASE,
                "table": table_filter,
                "count": len(filtered_logs),
                "data": filtered_logs,
                "logs": filtered_logs
            })

        return jsonify({
            "status": "success",
            "source": "mysql_local",
            "database": config.MYSQL_DATABASE,
            "tables": tables,
            "logs": flat_logs,
            "total": len(flat_logs)
        })
    except Exception as e:
        logger.exception("Lỗi khi đọc nhật ký từ MySQL: %s", e)
        logs = read_logs()
        return jsonify({
            "status": "success",
            "source": "fallback_json",
            "tables": {},
            "logs": logs
        })


ACTIVITY_REQUIRED_FIELDS = {
    "spray": [
        ("ten_thuoc_bvtv", "Tên thuốc BVTV", ["material_name", "ten_thuoc", "ten_phan_thuoc"]),
        ("target_pest", "Đối tượng sâu bệnh", ["sinh_vat_gay_hai", "pest_name"]),
        ("nong_do_va_luong_dung", "Nồng độ & liều lượng", ["concentration", "luong_nuoc_pha", "lieu_luong_thuoc", "lieu_luong"]),
        ("thoi_gian_cach_ly", "Thời gian cách ly", ["isolation_days", "quarantine_days", "cach_ly"]),
    ],
    "fertilize": [
        ("ten_phan_bon", "Tên phân bón", ["material_name", "ten_phan_thuoc"]),
        ("luong_phan_bon", "Lượng bón", ["quantity", "luong_su_dung", "so_luong"]),
    ],
    "purchase": [
        ("material_name", "Tên vật tư", ["ten_vat_tu", "ten_vat_tu_nhap", "input_name"]),
        ("quantity", "Số lượng / Khối lượng", ["so_luong_vat_tu", "luong_su_dung", "so_luong"]),
        ("unit", "Đơn vị tính", ["don_vi"]),
    ],
    "harvest": [
        ("crop_name", "Loại nông sản", ["loai_nong_san", "ten_cay_trong"]),
        ("san_luong_thu_hoach", "Sản lượng thu hoạch", ["quantity", "harvest_quantity", "so_luong", "yield_amount"]),
    ],
    "sale": [
        ("crop_name", "Loại nông sản", ["loai_nong_san", "ten_cay_trong"]),
        ("khoi_luong_tieu_thu", "Khối lượng bán", ["khoi_luong_ban", "sale_quantity", "quantity", "so_luong"]),
    ],
    "waste": [
        ("material_name", "Tên thuốc / vật tư", ["ten_vat_tu", "ten_thuoc"]),
        ("waste_type", "Loại bao bì / rác", ["loai_bao_bi", "loai_bao_bi_thuoc_du"]),
        ("treatment_method", "Cách xử lý & nơi lưu trữ", ["cach_xu_ly", "phuong_phap_xu_ly", "storage_location"]),
    ],
    "observation": [
        ("sinh_vat_gay_hai", "Sinh vật gây hại phát hiện", ["target_pest", "pest_name", "doi_tuong_sau_benh"]),
    ],
    "general": [
        ("cong_viec", "Tên / Nội dung công việc", ["task_name", "description", "noi_dung_cong_viec"]),
    ],
}

TASK_NAME_DISPLAY = {
    "spray": "Phun thuốc BVTV",
    "fertilize": "Bón phân",
    "purchase": "Mua vật tư",
    "harvest": "Thu hoạch",
    "sale": "Tiêu thụ / Bán",
    "waste": "Xử lý bao bì",
    "observation": "Quan sát",
    "general": "Hoạt động chung"
}


@app.route("/api/logs", methods=["POST"])
@app.route("/api/diary", methods=["POST"])
def save_log():
    """Lưu nhật ký canh tác tổng hợp vào CSDL vietgap_diary."""
    entry = request.get_json(silent=True) or {}
    farmer = session.get("farmer")
    farmer_id = farmer.get("id") if farmer else None

    # Kiểm tra các trường bắt buộc của từng hoạt động trước khi xử lý
    tables_data = entry.get("tables") or {}
    task_types = entry.get("task_types") or []
    if isinstance(task_types, str):
        task_types = [task_types]
    elif not isinstance(task_types, list):
        task_types = []

    if not task_types and isinstance(tables_data, dict):
        task_types = list(tables_data.keys())

    missing_errors = []
    canonical_map = {
        "b1_6_material_usages": "spray",
        "pesticide_applications": "spray",
        "fertilizer_applications": "fertilize",
        "b1_5_material_purchases": "purchase",
        "input_purchases": "purchase",
        "b1_8_harvests": "harvest",
        "harvest_records": "harvest",
        "b1_9_sales": "sale",
        "sales_records": "sale",
        "b1_7_waste_treatments": "waste",
        "waste_disposals": "waste",
        "pest_observations": "observation",
        "cultivation_diaries": "general",
        "cultivation_activities": "general",
    }

    for t_key in task_types:
        canonical_task = canonical_map.get(t_key, t_key)
        reqs = ACTIVITY_REQUIRED_FIELDS.get(canonical_task) or []
        t_data = {}
        if isinstance(tables_data, dict):
            t_data = tables_data.get(t_key) or tables_data.get(canonical_task) or {}

        for req_key, req_label, aliases in reqs:
            val = t_data.get(req_key) if isinstance(t_data, dict) else None
            if not val and isinstance(t_data, dict):
                for al in aliases:
                    val = t_data.get(al)
                    if val:
                        break
            if not val:
                val = entry.get(req_key)
            if not val:
                for al in aliases:
                    val = entry.get(al)
                    if val:
                        break

            # Lấy crop_name từ nông hộ hoặc DB nếu chưa có
            if req_key == "crop_name" and (not val or not str(val).strip()):
                crop_from_db = farmer.get("crop_name") if farmer else None
                if not crop_from_db:
                    try:
                        from services.database import get_db_connection, get_crop_name_for_farmer
                        conn = get_db_connection()
                        crop_from_db = get_crop_name_for_farmer(conn, user_id=farmer_id)
                        conn.close()
                    except Exception:
                        crop_from_db = "Bưởi diễn"
                val = crop_from_db or "Bưởi diễn"
                if isinstance(t_data, dict):
                    t_data["crop_name"] = val
                entry["crop_name"] = val

            if not val or not str(val).strip():
                t_display = TASK_NAME_DISPLAY.get(canonical_task, canonical_task)
                missing_errors.append(f"'{req_label}' của {t_display}")

    if missing_errors:
        return jsonify({
            "status": "error",
            "message": f"Thiếu thông tin bắt buộc trước khi lưu: {', '.join(missing_errors)}",
            "missing_fields": missing_errors
        }), 400

    # Tự động chuẩn hoá qua normalizer
    normalized_entry = apply_normalizer(entry)
    for k in ["id", "created_at", "raw_text", "audio_url", "photo_url", "task_types"]:
        if k in entry:
            normalized_entry[k] = entry[k]

    # Đọc metadata LLM từ frontend (model, latency_s, prompt_tokens, completion_tokens, raw_response)
    llm_meta = entry.get("llm_meta") or {}
    if not isinstance(llm_meta, dict):
        llm_meta = {}
    if "raw_response" not in llm_meta and "raw_response" in entry:
        llm_meta["raw_response"] = entry["raw_response"]
    if "raw_llm_output" not in llm_meta and "raw_llm_output" in entry:
        llm_meta["raw_llm_output"] = entry["raw_llm_output"]
    if "raw_llm_output" not in llm_meta and "raw_response" in llm_meta:
        llm_meta["raw_llm_output"] = llm_meta["raw_response"]
    time_process = float(llm_meta.get("latency_s") or 0.0)

    db_res = None
    save_error = None
    try:
        db_res = save_diary_to_mysql(
            farmer_id,
            normalized_entry,
            raw_text=entry.get("raw_text", ""),
            audio_url=entry.get("audio_url", ""),
            photo_url=entry.get("photo_url", ""),
            time_process=time_process,
            llm_meta=llm_meta,
        )
        normalized_entry["id"] = db_res.get("id", normalized_entry.get("id"))
        normalized_entry["table"] = "cultivation_activities"
    except Exception as e:
        save_error = str(e)
        logger.exception("Lỗi khi lưu nhật ký vào MySQL: %s", e)

    # Cập nhật giai đoạn mùa vụ vào session nông hộ nếu có thay đổi
    updated_stage = db_res.get("growth_stage") if db_res else None
    if updated_stage:
        if "farmer" in session and session["farmer"]:
            session["farmer"]["growth_stage"] = updated_stage
            session.modified = True

    # Đồng thời lưu bản sao backup vào logs.json
    logs = read_logs()
    logs.insert(0, normalized_entry)
    write_logs(logs)

    if save_error and not db_res:
        return jsonify({
            "status": "error",
            "message": f"Lỗi ghi sổ nhật ký: {save_error}",
            "entry": normalized_entry,
            "db": None
        }), 500

    return jsonify({
        "status": "success",
        "message": "Đã lưu sổ nhật ký thành công",
        "source": "mysql_local" if db_res else "local_json",
        "activity_id": db_res.get("id") if db_res else normalized_entry.get("id"),
        "created_records": db_res.get("created_records", {}) if db_res else {},
        "growth_stage": updated_stage,
        "entry": normalized_entry,
        "db": db_res
    }), 201 if db_res else 200


@app.route("/api/diary/<table_name>", methods=["POST"])
def save_specific_diary_table(table_name):
    """
    API chuyên biệt lưu trực tiếp một bản ghi nhật ký vào bảng cụ thể theo đúng schema vietgap_diary.
    Hỗ trợ các bảng:
    - pesticide_applications / spray: Phun thuốc BVTV
    - fertilizer_applications / fertilize: Bón phân
    - cultivation_activities / general: Công việc canh tác khác
    - harvest_records / harvest: Thu hoạch nông sản
    - sales_records / sale: Tiêu thụ / xuất bán nông sản
    - input_purchases / purchase: Mua vật tư / cây giống
    - waste_disposals / waste: Xử lý bao bì / chất thải
    - pest_observations: Ghi nhận sâu bệnh
    - irrigation_records: Tưới tiêu
    """
    body = request.get_json(silent=True) or {}
    farmer = session.get("farmer")
    farmer_id = farmer.get("id") if farmer else None

    tbl = str(table_name).strip().lower().replace("-", "_")

    task_map = {
        "pesticide_applications": "spray",
        "spray": "spray",
        "b1_6_pesticides": "spray",
        "fertilizer_applications": "fertilize",
        "fertilize": "fertilize",
        "b1_6_fertilizers": "fertilize",
        "cultivation_activities": "general",
        "cultivation_diaries": "general",
        "general": "general",
        "harvest_records": "harvest",
        "harvests": "harvest",
        "harvest": "harvest",
        "b1_8_harvests": "harvest",
        "sales_records": "sale",
        "sales": "sale",
        "sale": "sale",
        "b1_9_sales": "sale",
        "input_purchases": "purchase",
        "material_purchases": "purchase",
        "b1_5_material_purchases": "purchase",
        "purchase": "purchase",
        "waste_disposals": "waste",
        "waste_treatments": "waste",
        "b1_7_waste_treatments": "waste",
        "waste": "waste",
        "pest_observations": "observation",
        "observation": "observation",
        "quan_sat": "observation",
        "diary_pests": "observation",
        "pest_observation": "observation",
        "irrigation_records": "irrigation",
    }
    task_type = task_map.get(tbl, "general")

    payload = {
        "task_types": [task_type],
        "tables": {
            task_type: body,
            tbl: body
        },
        "raw_text": body.get("raw_text", ""),
        "audio_url": body.get("audio_url", "")
    }

    try:
        db_res = save_diary_to_mysql(
            farmer_id,
            payload,
            raw_text=body.get("raw_text", ""),
            audio_url=body.get("audio_url", "")
        )
        return jsonify({
            "status": "success",
            "message": f"Đã lưu bản ghi vào bảng {tbl} thành công",
            "table": tbl,
            "activity_id": db_res.get("id"),
            "created_records": db_res.get("created_records", {}),
            "db": db_res
        }), 201
    except Exception as e:
        logger.exception("Lỗi khi lưu bảng %s: %s", tbl, e)
        return jsonify({
            "status": "error",
            "message": f"Lỗi ghi vào bảng {tbl}: {str(e)}"
        }), 500


@app.route("/api/diary/<table_name>/<record_id>", methods=["GET"])
def get_diary_detail(table_name, record_id):
    """Lấy chi tiết một bản ghi nhật ký theo ID và tên bảng."""
    row = get_diary_record_by_id(table_name, record_id)
    if not row:
        return jsonify({"status": "error", "message": "Không tìm thấy bản ghi"}), 404
    return jsonify({
        "status": "success",
        "table": table_name,
        "record_id": record_id,
        "data": row
    })


@app.route("/api/season/growth-stage", methods=["GET", "POST"])
def api_season_growth_stage():
    """API lấy hoặc cập nhật giai đoạn mùa vụ từ lời nói hoặc văn bản người dân."""
    farmer = session.get("farmer")
    farmer_id = farmer.get("id") if farmer else None

    if request.method == "GET":
        stage = (farmer.get("growth_stage") if farmer else None) or "Sinh trưởng"
        return jsonify({"status": "success", "growth_stage": stage, "available_stages": GROWTH_STAGES})

    body = request.get_json(silent=True) or request.form or {}
    text = body.get("text") or body.get("growth_stage") or ""
    stage = detect_growth_stage_from_text(text)
    if not stage:
        t_clean = str(text).strip()
        for s in GROWTH_STAGES:
            if t_clean.lower() == s.lower():
                stage = s
                break
    if not stage:
        stage = "Sinh trưởng" if not str(text).strip() else str(text).strip()

    ok = update_season_growth_stage(stage, farmer_id=farmer_id)
    if ok and "farmer" in session and session["farmer"]:
        session["farmer"]["growth_stage"] = stage
        session.modified = True

    return jsonify({"status": "success", "growth_stage": stage, "available_stages": GROWTH_STAGES, "message": f"Đã cập nhật giai đoạn mùa vụ: {stage}"})


@app.route("/api/logs/<log_id>", methods=["PUT"])
@app.route("/api/diary/<table_name>/<record_id>", methods=["PUT"])
def update_log(log_id=None, table_name=None, record_id=None):
    """Cập nhật / chỉnh sửa thông tin bản ghi nhật ký trong CSDL vietgap_diary."""
    rec_id = record_id or log_id
    tbl = table_name or request.args.get("table") or "cultivation_activities"
    update_data = request.get_json(silent=True) or {}

    if not rec_id:
        return jsonify({"status": "error", "message": "Thiếu ID bản ghi cần sửa"}), 400

    try:
        ok = update_diary_record(rec_id, tbl, update_data)
        if ok:
            return jsonify({
                "status": "success",
                "message": f"Đã cập nhật bản ghi {rec_id} trong bảng {tbl} thành công",
                "record_id": rec_id,
                "table": tbl
            })
        else:
            return jsonify({
                "status": "error",
                "message": f"Không thể cập nhật bản ghi {rec_id}"
            }), 400
    except Exception as e:
        logger.exception("Lỗi khi cập nhật nhật ký: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/logs/<log_id>", methods=["DELETE"])
@app.route("/api/diary/<table_name>/<record_id>", methods=["DELETE"])
def delete_log(log_id=None, table_name=None, record_id=None):
    """Xóa một dòng nhật ký theo ID khỏi CSDL vietgap_diary và file backup."""
    rec_id = record_id or log_id
    tbl = table_name or request.args.get("table")
    try:
        delete_diary_record(rec_id, tbl)
    except Exception as e:
        logger.warning("Lỗi xóa từ MySQL: %s", e)

    logs = read_logs()
    updated_logs = [log for log in logs if str(log.get("id")) != str(rec_id)]
    write_logs(updated_logs)
    return jsonify({"status": "success", "deleted_id": rec_id, "table": tbl})


@app.route("/api/categories", methods=["GET"])
@app.route("/api/categories/<cat_type>", methods=["GET"])
def get_all_categories(cat_type=None):
    """Lấy danh mục dữ liệu phục vụ gợi ý tự động (autocomplete) và dropdown trên form."""
    try:
        cats = get_categories(cat_type)
        return jsonify({
            "status": "success",
            "category_type": cat_type or "all",
            "data": cats
        })
    except Exception as e:
        logger.exception("Lỗi lấy danh mục: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/plots", methods=["GET"])
def api_get_plots():
    """Lấy danh sách các lô canh tác kèm tọa độ GPS."""
    try:
        farmer = session.get("farmer")
        farm_id = farmer.get("farm_id") if farmer else None
        plots = get_plots(farm_id)
        return jsonify({"status": "success", "data": plots})
    except Exception as e:
        logger.exception("Lỗi lấy danh sách lô: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/plots/nearest", methods=["POST", "GET"])
def api_nearest_plot():
    """Tìm lô canh tác gần nhất từ tọa độ GPS."""
    try:
        data = request.get_json(silent=True) or request.values or {}
        lat = data.get("latitude") or data.get("lat")
        lng = data.get("longitude") or data.get("lng") or data.get("lon")
        if lat is None or lng is None:
            return jsonify({"status": "error", "message": "Thiếu tọa độ latitude hoặc longitude"}), 400

        farmer = session.get("farmer")
        farm_id = farmer.get("farm_id") if farmer else None
        nearest = find_nearest_plot(float(lat), float(lng), farm_id)
        plots = get_plots(farm_id)
        return jsonify({
            "status": "success",
            "nearest_plot": nearest,
            "plots": plots
        })
    except Exception as e:
        logger.exception("Lỗi tìm lô gần nhất: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/export/excel", methods=["GET", "POST"])
@app.route("/export/excel", methods=["GET"])
def export_excel():
    """Xuất sổ nhật ký canh tác theo chuẩn VietGAP ra file Excel (.xlsx)."""
    try:
        farmer = session.get("farmer")
        farmer_id = request.args.get("farmer_id")
        if not farmer_id and farmer:
            farmer_id = farmer.get("user_id") or farmer.get("farmer_id")

        season_id = request.args.get("season_id")
        template = request.args.get("template") or request.args.get("type")
        if not season_id and request.is_json:
            season_id = request.json.get("season_id")
            if not template:
                template = request.json.get("template")
        if season_id:
            try:
                season_id = int(season_id)
            except (ValueError, TypeError):
                season_id = None

        if template in ("vietgap", "tables", "pdf_template"):
            buf, filename = export_vietgap_tables_excel_bytes(season_id=season_id, farmer_id=farmer_id)
        else:
            buf, filename = export_cultivation_diary_to_bytes(season_id=season_id, farmer_id=farmer_id)

        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as e:
        logger.exception("Lỗi xuất Excel: %s", e)
        return jsonify({"status": "error", "message": f"Không thể xuất file Excel: {str(e)}"}), 500


@app.route("/api/export/docx", methods=["GET", "POST"])
@app.route("/export/docx", methods=["GET"])
def export_docx():
    """Xuất sổ nhật ký canh tác theo chuẩn VietGAP ra file Word (.docx)."""
    try:
        farmer = session.get("farmer")
        farmer_id = request.args.get("farmer_id")
        if not farmer_id and farmer:
            farmer_id = farmer.get("user_id") or farmer.get("farmer_id")

        season_id = request.args.get("season_id")
        template = request.args.get("template") or request.args.get("type")
        if not season_id and request.is_json:
            season_id = request.json.get("season_id")
            if not template:
                template = request.json.get("template")
        if season_id:
            try:
                season_id = int(season_id)
            except (ValueError, TypeError):
                season_id = None

        if template in ("vietgap", "tables", "pdf_template"):
            buf, filename = export_vietgap_tables_docx_bytes(season_id=season_id, farmer_id=farmer_id)
        else:
            buf, filename = export_cultivation_diary_to_docx_bytes(season_id=season_id, farmer_id=farmer_id)

        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as e:
        logger.exception("Lỗi xuất Word (docx): %s", e)
        return jsonify({"status": "error", "message": f"Không thể xuất file Word: {str(e)}"}), 500


@app.route("/api/export/pdf", methods=["GET", "POST"])
@app.route("/export/pdf", methods=["GET"])
def export_pdf():
    """Xuất sổ nhật ký canh tác theo chuẩn VietGAP ra file PDF (.pdf)."""
    try:
        farmer = session.get("farmer")
        farmer_id = request.args.get("farmer_id")
        if not farmer_id and farmer:
            farmer_id = farmer.get("user_id") or farmer.get("farmer_id")

        season_id = request.args.get("season_id")
        template = request.args.get("template") or request.args.get("type")
        if not season_id and request.is_json:
            season_id = request.json.get("season_id")
            if not template:
                template = request.json.get("template")
        if season_id:
            try:
                season_id = int(season_id)
            except (ValueError, TypeError):
                season_id = None

        if template in ("vietgap", "tables", "pdf_template"):
            buf, filename = export_vietgap_tables_pdf_bytes(season_id=season_id, farmer_id=farmer_id)
        else:
            buf, filename = export_cultivation_diary_to_pdf_bytes(season_id=season_id, farmer_id=farmer_id)

        return send_file(
            buf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as e:
        logger.exception("Lỗi xuất PDF: %s", e)
        return jsonify({"status": "error", "message": f"Không thể xuất file PDF: {str(e)}"}), 500


@app.route("/api/export/vietgap/<fmt>", methods=["GET", "POST"])
@app.route("/export/vietgap/<fmt>", methods=["GET"])
def export_vietgap_direct(fmt):
    """Xuất trực tiếp các biểu mẫu theo chuẩn vietGAP.pdf (excel, docx, pdf)."""
    fmt_lower = fmt.lower()
    if fmt_lower in ("excel", "xlsx"):
        return export_excel()
    elif fmt_lower in ("docx", "doc", "word"):
        return export_docx()
    elif fmt_lower == "pdf":
        return export_pdf()
    else:
        return jsonify({"status": "error", "message": f"Định dạng {fmt} không được hỗ trợ"}), 400


@app.route("/api/seasons", methods=["GET"])
def get_seasons_api():
    """Lấy danh sách các mùa vụ canh tác để chọn khi xuất báo cáo."""
    try:
        farmer = session.get("farmer")
        farmer_id = request.args.get("farmer_id") or (farmer.get("user_id") if farmer else None)
        seasons = get_available_seasons(farmer_id=farmer_id)
        return jsonify({"status": "success", "data": seasons})
    except Exception as e:
        logger.exception("Lỗi lấy danh sách mùa vụ: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nông Ký Flask Server")
    parser.add_argument("--port", type=int, default=5000, help="Cổng lắng nghe (mặc định 5000)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Địa chỉ host")
    parser.add_argument("--ssl", action="store_true", default=True, help="Bật HTTPS (mặc định bật)")
    parser.add_argument("--no-ssl", dest="ssl", action="store_false", help="Tắt HTTPS, chỉ dùng HTTP")
    args = parser.parse_args()

    # Khởi tạo CSDL MySQL local & seed dữ liệu nếu cần
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
        logger.info(f"   URL: https://{args.host}:{args.port} hoặc https://192.168.40.18:{args.port}")
    else:
        logger.info(f"⚠️ Chạy ứng dụng qua HTTP tại http://{args.host}:{args.port}")

    # Khởi động Realtime STT WebSocket Server
    if getattr(config, "WS_ENABLED", True):
        ws_port = getattr(config, "WS_PORT", 8765)
        ws_cert = CERT_FILE if (args.ssl and os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)) else None
        ws_key = KEY_FILE if ws_cert else None
        try:
            realtime_stt_engine.start_websocket_server_in_background(
                host=args.host,
                port=ws_port,
                ssl_cert=ws_cert,
                ssl_key=ws_key
            )
            ws_protocol = "wss" if ws_cert else "ws"
            logger.info(f"🚀 Realtime STT WebSocket Server đã khởi động tại {ws_protocol}://{args.host}:{ws_port}")
        except Exception as e:
            logger.warning(f"⚠️ Khởi động Realtime STT WebSocket Server gặp cảnh báo: {e}")

    app.run(host=args.host, port=args.port, ssl_context=ssl_context, debug=False)
