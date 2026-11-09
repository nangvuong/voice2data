"""
Routes cho các dịch vụ AI Engine: OCR, VLM, STT đơn lẻ, Trích xuất VietGAP, Chuẩn hoá và Pipeline Voice-to-Data.
"""

import os
import json
import uuid
from flask import Blueprint, request, jsonify, session

import config
from app.common import (
    logger,
    run_async,
    STATIC_PHOTOS_DIR,
    STATIC_AUDIO_DIR,
    UPLOADS_DIR,
    apply_normalizer,
)
from services.audio.audio_converter import resample_to_16k_mono_pcm16
from services.engines.stt_engine import stt_engine
from services.engines.ocr_engine import ocr_engine, OCRError
from services.vietgap.material_matcher import material_matcher
from services.engines.vlm_engine import vlm_engine
from services.engines.llm_engine import llm_engine
from services.engines.gemini_engine import gemini_engine
from services.normalizer import classify_into_tables
from services.database import (
    detect_growth_stage_from_text,
    update_season_growth_stage,
)

ai_bp = Blueprint("ai", __name__)


# ==========================================
# UPLOAD PHOTO & OCR & VLM
# ==========================================

@ai_bp.route("/api/upload-photo", methods=["POST"])
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
    do_ocr = (
        request.args.get("ocr", "").lower() in ("true", "1", "yes")
        or request.form.get("ocr", "").lower() in ("true", "1", "yes")
    )
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

    # Nếu người dùng yêu cầu chạy VLM luôn khi upload
    do_vlm = (
        request.args.get("vlm", "").lower() in ("true", "1", "yes")
        or request.form.get("vlm", "").lower() in ("true", "1", "yes")
    )
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


@ai_bp.route("/api/ocr", methods=["POST"])
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

    min_score = float(
        request.args.get("min_score")
        or (request.get_json(silent=True) or {}).get("min_score")
        or request.form.get("min_score")
        or 0.5
    )
    do_vis = (
        request.args.get("vis", "").lower() in ("true", "1", "yes")
        or (request.get_json(silent=True) or {}).get("vis") is True
    )
    do_extract = (
        request.args.get("extract", "").lower() in ("true", "1", "yes")
        or (request.get_json(silent=True) or {}).get("extract") is True
    )
    do_match = (
        request.args.get("match", "").lower() in ("true", "1", "yes")
        or (request.get_json(silent=True) or {}).get("match") is True
    )

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


@ai_bp.route("/api/match-material", methods=["POST"])
def api_match_material():
    """
    Sử dụng LLM 0.6B (Port 8081) để trích xuất tên thuốc/phân bón từ văn bản và fuzzy match với CSDL.
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


@ai_bp.route("/api/vlm-analyze", methods=["POST"])
def api_vlm_analyze():
    """
    Phân tích hình ảnh bằng VLM Engine: nhận diện sâu bệnh, tên thuốc BVTV, phân bón.
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


@ai_bp.route("/api/vlm-add-activity", methods=["POST"])
def api_vlm_add_activity():
    """
    Phân tích hình ảnh bằng VLM Engine và tự động lưu bản ghi hoạt động vào CSDL VietGAP.
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


@ai_bp.route("/api/invoice-read", methods=["POST"])
def api_invoice_read():
    """
    Chuyên dụng đọc hóa đơn mua phân bón & thuốc trừ sâu (invoice_receipt) để ghi nhật ký mua vật tư (Bảng 1.5 - input_purchases).
    - Trích xuất: Tên nhà cung cấp, số hóa đơn, ngày mua, tổng tiền, và danh sách từng mặt hàng (tên phân/thuốc, số lượng, đơn vị, đơn giá).
    - Tự động đối chiếu với CSDL `pesticides` / `fertilizers` và kiểm tra hoạt chất cấm.
    - Nếu truyền `auto_save=true`, tự động lưu toàn bộ các mặt hàng vào bảng `input_purchases` (chặn lưu nếu phát hiện thuốc cấm).
    """
    photo_file = request.files.get("photo") or request.files.get("image") or request.files.get("file")
    target_img_input = None
    photo_url = ""

    if photo_file:
        orig_ext = os.path.splitext(photo_file.filename or "")[1].lower()
        if not orig_ext or orig_ext not in [".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp"]:
            orig_ext = ".jpg"
        photo_id = uuid.uuid4().hex
        filename = f"invoice_{photo_id}{orig_ext}"
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
        return jsonify({"status": "error", "message": "Vui lòng đính kèm file ảnh hóa đơn hoặc cung cấp photo_url / base64"}), 400

    data = request.get_json(silent=True) or request.form or {}
    farmer = session.get("farmer") or {}
    farmer_id = session.get("user_id") or farmer.get("id") or request.args.get("farmer_id") or data.get("farmer_id") or 1
    crop_name = request.args.get("crop_name") or data.get("crop_name") or "Bưởi diễn"
    auto_save = str(request.args.get("auto_save") or data.get("auto_save") or "false").lower() in ("true", "1", "yes")

    try:
        res = run_async(vlm_engine.analyze_image(
            image_input=target_img_input,
            task="invoice_receipt",
            match_db=True,
            crop_name=crop_name,
        ))

        analysis_dict = res.to_dict()
        analysis_dict["photo_url"] = photo_url
        diary_payload = res.to_diary_payload(
            intent="purchase",
            photo_url=photo_url,
            crop_name=crop_name,
        )

        inv_data = analysis_dict.get("invoice_receipt") or {}
        has_banned = bool(inv_data.get("has_banned_items"))

        db_res = None
        if auto_save:
            if has_banned:
                return jsonify({
                    "status": "error",
                    "message": "Phát hiện thuốc BVTV thuộc danh mục CẤM trên hóa đơn! Hệ thống từ chối lưu vào sổ nhật ký VietGAP.",
                    "photo_url": photo_url,
                    "invoice_receipt": inv_data,
                    "diary_payload": diary_payload,
                    "analysis": analysis_dict,
                }), 400
            db_res = res.save_to_database(
                farmer_id=int(farmer_id) if farmer_id else 1,
                intent="purchase",
                photo_url=photo_url,
                crop_name=crop_name,
            )

        return jsonify({
            "status": "success",
            "photo_url": photo_url,
            "invoice_receipt": inv_data,
            "diary_payload": diary_payload,
            "db_result": db_res,
            "analysis": analysis_dict,
        })
    except Exception as e:
        logger.exception("Lỗi đọc hóa đơn vật tư: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


# ==========================================
# STT ĐƠN LẺ & TRÍCH XUẤT VIETGAP & VOICE-TO-DATA
# ==========================================

@ai_bp.route("/api/stt", methods=["POST"])
def transcribe_audio():
    """Nhận file âm thanh và nhận diện giọng nói bằng services.stt_engine, lưu audio để nghe lại."""
    audio_file = request.files.get("audio") or request.files.get("file")
    if not audio_file:
        return jsonify({"status": "error", "message": "Không tìm thấy file âm thanh"}), 400

    job_id = uuid.uuid4().hex
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
            "audio_url": audio_url,
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


@ai_bp.route("/api/extract", methods=["POST"])
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
                "auto_normalized": True,
            })

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
            "auto_normalized": True,
        })
    except Exception as e:
        logger.exception("Lỗi khi trích xuất thông tin: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@ai_bp.route("/api/normalize", methods=["POST"])
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
            "data": cleaned,
        })
    except Exception as e:
        logger.exception("Lỗi khi chuẩn hoá qua services.normalizer: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@ai_bp.route("/api/voice-to-data", methods=["POST"])
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
                "tables": None,
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
                "auto_normalized": True,
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
            "auto_normalized": True,
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
