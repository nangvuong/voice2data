"""
Routes cho STT Engine: Hotwords, Records, STT Lab và Realtime Streaming STT.
"""

import os
import time
import uuid
import shutil
import asyncio
from flask import Blueprint, request, jsonify, session, send_file

import config
from app.common import (
    logger,
    run_async,
    STATIC_AUDIO_DIR,
    UPLOADS_DIR,
    APP_DIR,
)
from services.audio.hotwords_manager import hotwords_manager
from services.audio.stt_records import stt_records
from services.engines.stt_engine import stt_engine
from services.audio.audio_converter import AudioConversionError
from services.audio.realtime_stt import realtime_stt_engine

stt_bp = Blueprint("stt", __name__)

# ==========================================
# CẤU HÌNH & HẰNG SỐ STT LAB
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

AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".ogg", ".webm", ".aac", ".flac", ".opus", ".mp4", ".wma")
IMPORT_ROOTS = [
    os.path.realpath(r)
    for r in os.getenv("STT_IMPORT_ROOTS", f"/mnt/data,{os.path.dirname(APP_DIR)}").split(",")
    if r.strip()
]


def _record_audio_name(audio_url: str):
    """Chỉ cho phép file lab_* nằm trong thư mục audio."""
    name = os.path.basename((audio_url or "").split("?")[0])
    if name.startswith(("lab_", "saved_")) and name.endswith(".wav"):
        return name
    return None


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
        f.append(
            "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.2"
            ",areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.2,areverse"
        )
    if p["gain_db"]:
        f.append(f"volume={p['gain_db']}dB")
    if p["normalize"]:
        f.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    return f


def _parse_lab_params(form) -> dict:
    def _b(k):
        return str(form.get(k, "")).lower() in ("true", "1", "on", "yes")

    def _f(k, lo=None, hi=None):
        try:
            v = float(form.get(k, STT_LAB_DEFAULTS[k]))
        except (TypeError, ValueError):
            v = float(STT_LAB_DEFAULTS[k])
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v

    method = form.get("decoding_method", "greedy_search")
    if method not in ("greedy_search", "modified_beam_search"):
        method = "greedy_search"
    window = int(_f("vad_window"))
    if window not in (256, 512, 768, 1024):
        window = 512
    return {
        "highpass_hz": _f("highpass_hz", 0, 4000),
        "lowpass_hz": _f("lowpass_hz", 0, 8000),
        "denoise": _b("denoise"),
        "denoise_nr": _f("denoise_nr", 1, 97),
        "gain_db": _f("gain_db", -30, 30),
        "normalize": _b("normalize"),
        "trim_silence": _b("trim_silence"),
        "start_sec": _f("start_sec", 0),
        "end_sec": _f("end_sec", 0),
        "speed": _f("speed", 0.5, 2.0),
        "use_vad": _b("use_vad"),
        "vad_threshold": _f("vad_threshold", 0.05, 0.95),
        "vad_min_silence": _f("vad_min_silence", 0.05, 5),
        "vad_min_speech": _f("vad_min_speech", 0.05, 5),
        "vad_max_speech": _f("vad_max_speech", 3, 60),
        "vad_window": window,
        "vad_padding": _f("vad_padding", 0, 1),
        "num_threads": int(_f("num_threads", 1, 16)),
        "decoding_method": method,
        "max_active_paths": int(_f("max_active_paths", 1, 16)),
    }


def _safe_import_path(path: str):
    """Chỉ cho phép đọc file audio nằm trong các thư mục gốc được cấu hình (STT_IMPORT_ROOTS)."""
    try:
        rp = os.path.realpath(path)
    except (TypeError, ValueError):
        return None
    if not any(rp == r or rp.startswith(r + os.sep) for r in IMPORT_ROOTS):
        return None
    return rp


# ==========================================
# HOTWORDS API
# ==========================================

@stt_bp.route("/api/stt-hotwords", methods=["GET"])
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


@stt_bp.route("/api/stt-hotwords", methods=["POST"])
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


@stt_bp.route("/api/stt-hotwords/batch", methods=["POST"])
def api_stt_hotwords_batch():
    if not session.get("farmer"):
        return jsonify({"status": "error", "message": "Chưa đăng nhập"}), 401
    d = request.get_json(silent=True) or {}
    words = d.get("words", "")
    category = d.get("category", "Chung")
    res = hotwords_manager.add_batch(words, category=category)
    return jsonify({"status": "success", **res})


@stt_bp.route("/api/stt-hotwords/<path:word_id>", methods=["DELETE"])
def api_stt_hotwords_delete(word_id):
    if not session.get("farmer"):
        return jsonify({"status": "error", "message": "Chưa đăng nhập"}), 401
    ok = hotwords_manager.delete_word(word_id)
    return jsonify({"status": "success" if ok else "error", "message": "" if ok else "Không tìm thấy từ khóa"})


@stt_bp.route("/api/stt-hotwords/preview", methods=["GET"])
def api_stt_hotwords_preview():
    word = request.args.get("word", "")
    tokens = hotwords_manager.encode_tokens(word)
    return jsonify({"status": "success", "word": word, "tokens": tokens})


@stt_bp.route("/api/stt-hotwords/sync", methods=["POST"])
def api_stt_hotwords_sync():
    if not session.get("farmer"):
        return jsonify({"status": "error", "message": "Chưa đăng nhập"}), 401
    count = hotwords_manager.sync_to_onnx()
    return jsonify({"status": "success", "total_tokens": count})


@stt_bp.route("/api/stt-hotwords/export", methods=["GET"])
def api_stt_hotwords_export():
    if not session.get("farmer"):
        return jsonify({"status": "error", "message": "Chưa đăng nhập"}), 401
    if os.path.exists(hotwords_manager.onnx_path):
        return send_file(hotwords_manager.onnx_path, as_attachment=True, download_name="onnx_hotwords.txt")
    return jsonify({"status": "error", "message": "Chưa có file hotwords"}), 404


# ==========================================
# STT RECORDS API
# ==========================================

@stt_bp.route("/api/stt-records", methods=["GET"])
def stt_records_list():
    if not session.get("farmer"):
        return jsonify({"status": "error", "message": "Chưa đăng nhập"}), 401
    return jsonify({"status": "success", "records": stt_records.list()})


@stt_bp.route("/api/stt-records/stats", methods=["GET"])
def stt_records_stats():
    if not session.get("farmer"):
        return jsonify({"status": "error", "message": "Chưa đăng nhập"}), 401
    return jsonify({"status": "success", "stats": stt_records.stats()})


@stt_bp.route("/api/stt-records", methods=["POST"])
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

    added_hotwords = hotwords_manager.learn_from_correction(dict_text, final_text, category="Tự học từ bản sửa")
    new_words = [it["word"] for it in added_hotwords]
    total_onnx = len(hotwords_manager.list_words())

    fields = {
        "filename": d.get("filename") or "(văn bản dán)",
        "raw_text": raw_text,
        "dict_text": dict_text,
        "final_text": final_text,
        "audio_duration": d.get("audio_duration"),
        "process_time": d.get("process_time") or 0,
        "ffmpeg_time": d.get("ffmpeg_time"),
        "segments": d.get("segments"),
        "params": d.get("params"),
        "changes": [],
        "new_vocab": new_words,
    }

    existing = stt_records.get(d["record_id"]) if d.get("record_id") else None
    if existing:
        fields["new_vocab"] = existing.get("new_vocab", []) + new_words
        fields["dict_text"] = existing.get("dict_text", dict_text)
        rec = stt_records.update(existing["id"], fields)
    else:
        src = _record_audio_name(d.get("audio_url"))
        if src and os.path.exists(os.path.join(STATIC_AUDIO_DIR, src)):
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
        "learned": {"added_entries": [], "updated_entries": [], "added_vocab": new_words},
    })


@stt_bp.route("/api/stt-records/<rec_id>", methods=["DELETE"])
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


@stt_bp.route("/api/stt-records/bulk-delete", methods=["POST"])
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


# ==========================================
# STT LAB & BATCH API
# ==========================================

@stt_bp.route("/api/stt-lab/defaults", methods=["GET"])
def stt_lab_defaults():
    return jsonify({"status": "success", "defaults": STT_LAB_DEFAULTS})


@stt_bp.route("/api/stt-lab/transcribe", methods=["POST"])
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

        now = time.time()
        for fn in os.listdir(STATIC_AUDIO_DIR):
            fp = os.path.join(STATIC_AUDIO_DIR, fn)
            if fn.startswith("lab_") and now - os.path.getmtime(fp) > 6 * 3600:
                try:
                    os.remove(fp)
                except OSError:
                    pass

        cmd = ["ffmpeg", "-y"]
        if p["start_sec"] > 0:
            cmd += ["-ss", str(p["start_sec"])]
        if p["end_sec"] > p["start_sec"] and p["end_sec"] > 0:
            cmd += ["-to", str(p["end_sec"])]
        cmd += ["-i", raw_path]
        filters = _build_ffmpeg_filters(p)
        if filters:
            cmd += ["-af", ",".join(filters)]
        cmd += [
            "-ar", str(config.TARGET_SAMPLE_RATE),
            "-ac", "1",
            "-sample_fmt", "s16",
            "-c:a", "pcm_s16le",
            out_path,
        ]

        async def _run_ffmpeg():
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _, err = await asyncio.wait_for(proc.communicate(), timeout=config.FFMPEG_TIMEOUT_SEC)
            if proc.returncode != 0:
                raise AudioConversionError("ffmpeg lỗi: " + err.decode(errors="ignore")[-300:])

        t0 = time.time()
        run_async(_run_ffmpeg())
        ffmpeg_time = round(time.time() - t0, 2)
        if not os.path.exists(out_path) or os.path.getsize(out_path) < 100:
            return jsonify({"status": "error", "message": "Audio sau xử lý rỗng (kiểm tra start/end/trim)."}), 400

        result = stt_engine._executor.submit(stt_engine._transcribe_tuned_sync, out_path, p).result()
        result.pop("_spans", None)
        result.pop("_sr", None)
        raw_text = result.get("text", "")

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
            try:
                os.remove(raw_path)
            except OSError:
                pass


@stt_bp.route("/api/stt-batch/list", methods=["GET"])
def stt_batch_list():
    """Liệt kê file audio trong một thư mục trên server (đệ quy tuỳ chọn)."""
    if not session.get("farmer"):
        return jsonify({"status": "error", "message": "Chưa đăng nhập"}), 401
    path = _safe_import_path(request.args.get("path", ""))
    if not path or not os.path.isdir(path):
        return jsonify({
            "status": "error",
            "message": "Thư mục không tồn tại hoặc không được phép. Gốc cho phép: " + ", ".join(IMPORT_ROOTS),
        }), 400
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


# ==========================================
# REALTIME STT API
# ==========================================

@stt_bp.route("/api/stt/realtime/start", methods=["POST"])
def realtime_stt_start():
    """Bắt đầu một phiên nhận diện streaming realtime mới."""
    body = request.get_json(silent=True) or {}
    custom_session_id = body.get("session_id") or request.headers.get("X-Session-ID")
    sess = realtime_stt_engine.create_session(session_id=custom_session_id)
    return jsonify({
        "status": "success",
        "session_id": sess.session_id,
        "sample_rate": sess.sample_rate,
        "message": "Phiên streaming realtime đã sẵn sàng.",
    })


@stt_bp.route("/api/stt/realtime/feed", methods=["POST"])
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

    sess = realtime_stt_engine.get_session(session_id)
    if not sess:
        return jsonify({"status": "error", "message": f"Session {session_id} không tồn tại hoặc đã kết thúc"}), 404

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
    events = sess.feed_audio(chunk_bytes, preserve_case=preserve_case)

    return jsonify({
        "status": "success",
        "session_id": session_id,
        "events": [ev.to_dict() for ev in events],
    })


@stt_bp.route("/api/stt/realtime/finish", methods=["POST"])
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

    stt_sess = realtime_stt_engine.get_session(session_id)
    if not stt_sess:
        return jsonify({"status": "error", "message": f"Session {session_id} không tồn tại hoặc đã kết thúc"}), 404

    preserve_case = body.get("preserve_case", True)
    events = stt_sess.finish(preserve_case=preserve_case)

    audio_url = ""
    try:
        wav_filename = f"voice_{uuid.uuid4().hex[:12]}.wav"
        wav_dest = os.path.join(STATIC_AUDIO_DIR, wav_filename)
        if stt_sess.save_wav(wav_dest):
            audio_url = f"/static/audio/{wav_filename}"
            session["last_voice_audio_url"] = audio_url
    except Exception as e:
        logger.warning("Không lưu được file âm thanh realtime: %s", e)

    realtime_stt_engine.close_session(session_id)

    complete_ev = next((ev for ev in events if ev.event_type == "complete"), None)
    return jsonify({
        "status": "success",
        "session_id": session_id,
        "audio_url": audio_url,
        "events": [ev.to_dict() for ev in events],
        "result": complete_ev.to_dict() if complete_ev else None,
    })


@stt_bp.route("/api/stt/realtime/info", methods=["GET"])
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
