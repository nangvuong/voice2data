"""
Handler xử lý khi người dùng gửi file audio (.wav, voice message) hoặc tin nhắn văn bản vào bot Telegram.
Định dạng kết quả JSON trả về thành tóm tắt rõ ràng với biểu tượng nông nghiệp kèm code block JSON chuẩn CSDL.
"""

import html
import json
import logging
import os
import uuid

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import config
from services.audio.audio_converter import AudioConversionError, resample_to_16k_mono_pcm16
from services.engines.llm_engine import llm_engine
from services.engines.stt_engine import STTError, stt_engine
from services.engines.ocr_engine import OCRError, ocr_engine
from services.vietgap.regex_parser import parse_with_regex
from services.normalizer import normalize_activity_json, normalize_text

logger = logging.getLogger(__name__)

# Telegram giới hạn 1 tin nhắn text tối đa 4096 ký tự. Với audio dài (vd. 30
# phút), transcript rất dễ vượt mức này -> phải gửi dưới dạng file thay vì
# edit_text (nếu không sẽ lỗi khi gửi).
TELEGRAM_TEXT_LIMIT = 4096

ACTIVITY_NAMES = {
    "bon_phan": "Bón phân",
    "phun_thuoc": "Phun thuốc BVTV",
    "tuoi_nuoc": "Tưới nước",
    "tia_canh": "Tỉa cành / Tạo tán",
    "lam_co": "Làm cỏ",
    "be_qua": "Bẻ quả / Tỉa trái",
    "kiem_tra_sau_benh": "Kiểm tra sâu bệnh",
    "khac": "Hoạt động khác",
}

MATERIAL_TYPES = {
    "phan_bon": "Phân bón",
    "thuoc_bvtv": "Thuốc BVTV",
    "che_pham_sinh_hoc": "Chế phẩm sinh học",
}


def format_telegram_message(raw_text: str) -> tuple[str, str, str | None, str, str]:
    """
    Định dạng kết quả JSON trả về cho Telegram Bot:
    - Trình bày tóm tắt tiếng Việt trực quan, minh bạch với biểu tượng nông nghiệp (🌱, 🗓️, 📝, 📍, 📦, 🌤️).
    - Hiển thị khối JSON Dữ Liệu chuẩn CSDL (bảng hoat_dong_canh_tac, chi_tiet_vat_tu_su_dung, lo_dat)
      trong thẻ HTML <pre><code class="language-json">...</code></pre>.
    Trả về: (full_msg, summary_msg, parse_mode, file_content, filename)
    """
    try:
        data = json.loads(raw_text)
        
        # Xử lý format mới "trich_xuat" từ regex parser
        if "trich_xuat" in data:
            t = data["trich_xuat"]
            lines = [
                "<b>🌱 KẾT QUẢ TRÍCH XUẤT</b>",
                "━━━━━━━━━━━━━━━━━━━━━━",
                f"• 🗓️ <b>Ngày tháng:</b> {html.escape(str(t.get('ngay_thang', 'không')))}",
                f"• 🌱 <b>Giai đoạn:</b> {html.escape(str(t.get('giai_doan', t.get('giai_doan_sinh_truong', 'không'))))}",
                f"• 📝 <b>Công việc:</b> {html.escape(str(t.get('cong_viec', 'không')))}",
                f"• 🐛 <b>Sinh vật gây hại:</b> {html.escape(str(t.get('sinh_vat_gay_hai_phat_hien', 'không')))}",
                f"• 📦 <b>Phân/Thuốc:</b> {html.escape(str(t.get('ten_phan_thuoc_bvtv', 'không')))}",
                f"• ⚖️ <b>Liều lượng:</b> {html.escape(str(t.get('luong_su_dung', 'không')))}",
                f"• 🛒 <b>Nơi mua:</b> {html.escape(str(t.get('noi_mua', 'không')))}",
                f"• ⏳ <b>Cách ly:</b> {html.escape(str(t.get('thoi_gian_cach_ly', 'không')))}",
            ]
            
            json_pretty = json.dumps(data, ensure_ascii=False, indent=2)
            summary_only = "\n".join(lines).strip()
            
        else:
            # Format cũ "activities" từ LLM
            activities = data.get("activities") if isinstance(data, dict) else (data if isinstance(data, list) else None)
            if not isinstance(activities, list) or len(activities) == 0:
                return raw_text, raw_text, None, raw_text, "ket_qua.txt"
    
            lines = [
                "<b>🌱 NHẬT KÝ CANH TÁC BƯỞI EXPORT (GLOBALGAP)</b>",
                "━━━━━━━━━━━━━━━━━━━━━━",
            ]
    
            for idx, item in enumerate(activities, 1):
                if not isinstance(item, dict):
                    continue
                loai_key = str(item.get("loai_hoat_dong") or "khac").lower()
                loai_vn = ACTIVITY_NAMES.get(loai_key, "Hoạt động khác")
                ngay = item.get("ngay_thuc_hien")
                header = f"<b>{idx}. {html.escape(loai_vn)}</b>"
                if ngay:
                    header += f" — 🗓️ <i>{html.escape(str(ngay))}</i>"
                lines.append(header)
    
                mo_ta = item.get("mo_ta")
                if mo_ta:
                    lines.append(f"  • 📝 <b>Mô tả:</b> {html.escape(str(mo_ta))}")
    
                ma_lo = item.get("ma_lo")
                giong = item.get("giong_buoi")
                if ma_lo or giong:
                    lo_str = f"Lô {ma_lo}" if ma_lo else ""
                    giong_str = f"Giống: {giong}" if giong else ""
                    comb = " | ".join(filter(None, [lo_str, giong_str]))
                    lines.append(f"  • 📍 <b>Lô/Giống:</b> {html.escape(comb)}")
    
                vat_tu = item.get("ten_vat_tu")
                if vat_tu:
                    lieu_luong = item.get("lieu_luong")
                    don_vi = item.get("don_vi")
                    ll_str = (
                        f" ({lieu_luong} {don_vi})"
                        if lieu_luong is not None and don_vi
                        else (f" ({lieu_luong})" if lieu_luong is not None else "")
                    )
                    loai_vt = item.get("loai_vat_tu")
                    vt_type_str = f" [{MATERIAL_TYPES.get(loai_vt, str(loai_vt))}]" if loai_vt else ""
                    lines.append(
                        f"  • 📦 <b>Vật tư:</b> {html.escape(str(vat_tu))}{html.escape(ll_str)}{html.escape(vt_type_str)}"
                    )
    
                thoi_tiet = item.get("thoi_tiet")
                if thoi_tiet:
                    lines.append(f"  • 🌤️ <b>Thời tiết:</b> {html.escape(str(thoi_tiet))}")
    
                lines.append("")  # Dòng trống phân cách giữa các hoạt động
    
            json_pretty = json.dumps({"activities": activities}, ensure_ascii=False, indent=2)
            summary_only = "\n".join(lines).strip()
            if len(summary_only) > TELEGRAM_TEXT_LIMIT:
                summary_only = (
                    "<b>🌱 NHẬT KÝ CANH TÁC BƯỞI EXPORT</b>\n"
                    "<i>(Danh sách hoạt động quá dài, vui lòng xem chi tiết trong file JSON đính kèm bên dưới)</i>"
                )

        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("<b>📦 JSON Dữ Liệu (Chuẩn CSDL):</b>")
        lines.append(f'<pre><code class="language-json">{html.escape(json_pretty)}</code></pre>')

        full_msg = "\n".join(lines).strip()
        return full_msg, summary_only, ParseMode.HTML, json_pretty, "nhat_ky_buoi.json"
    except Exception as e:
        logger.warning("Không định dạng được JSON sang HTML (%s), trả về text thô.", e)
        return raw_text, raw_text, None, raw_text, "ket_qua.txt"


async def handle_audio_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Nhận file audio (voice message, audio, hoặc document .wav),
    resample bằng ffmpeg, chạy STT, trả kết quả text cho người dùng.
    """
    message = update.effective_message

    # Lấy file object tuỳ theo loại người dùng gửi (voice / audio / document)
    tg_file_obj = None
    if message.voice:
        tg_file_obj = message.voice
    elif message.audio:
        tg_file_obj = message.audio
    elif message.document:
        tg_file_obj = message.document

    if tg_file_obj is None:
        await message.reply_text("Vui lòng gửi một file audio (.wav, voice message...).")
        return

    # Kiểm tra kích thước file trước khi tải
    file_size = getattr(tg_file_obj, "file_size", None) or 0
    if file_size > config.MAX_FILE_SIZE_MB * 1024 * 1024:
        await message.reply_text(
            f"File quá lớn (giới hạn {config.MAX_FILE_SIZE_MB}MB). Vui lòng gửi file nhỏ hơn."
        )
        return

    os.makedirs(config.TMP_DIR, exist_ok=True)

    # Tên file duy nhất cho mỗi request, tránh đụng độ khi nhiều người dùng cùng lúc
    job_id = uuid.uuid4().hex
    raw_path = os.path.join(config.TMP_DIR, f"{job_id}_raw")
    wav_path = os.path.join(config.TMP_DIR, f"{job_id}_16k.wav")

    processing_msg = await message.reply_text("⏳ Đang xử lý audio, vui lòng chờ...")

    try:
        # 1. Tải file gốc về
        tg_file = await tg_file_obj.get_file()
        await tg_file.download_to_drive(raw_path)

        # 2. Resample về 16kHz, mono, PCM16 bằng ffmpeg
        try:
            await resample_to_16k_mono_pcm16(raw_path, wav_path)
            # Lưu bản sao audio vào AUDIO_STORAGE_DIR (/mnt/data/audio)
            audio_storage_dir = getattr(config, "AUDIO_STORAGE_DIR", "/mnt/data/audio")
            try:
                import shutil
                os.makedirs(audio_storage_dir, exist_ok=True)
                shutil.copy2(wav_path, os.path.join(audio_storage_dir, f"{job_id}.wav"))
            except Exception as save_err:
                logger.warning("Không thể lưu file audio vào %s: %s", audio_storage_dir, save_err)
        except AudioConversionError as exc:
            logger.warning("Convert audio thất bại: %s", exc)
            await processing_msg.edit_text(
                "Không thể xử lý file audio này. Vui lòng kiểm tra lại định dạng file."
            )
            return

        # 3. Đẩy vào hàng đợi STT (model đã load sẵn, xử lý tuần tự)
        try:
            text = await stt_engine.transcribe(wav_path)
            # (Bỏ qua correction engine)
            pass
                
        except STTError as exc:
            logger.error("STT thất bại: %s", exc)
            await processing_msg.edit_text(
                "Có lỗi xảy ra khi nhận diện giọng nói. Vui lòng thử lại sau."
            )
            return

        # 4. Chạy thuật toán bóc tách dữ liệu
        if text:
            norm_text = normalize_text(text)
            parsed_data_json = None
            
            # 4.1 Thử PhoBERT Slot
            # from services.phobert_slot import phobert_slot_engine
            # if phobert_slot_engine.session:
            #     try:
            #         phobert_result = phobert_slot_engine.extract_slots(norm_text)
            #         if phobert_result:
            #             parsed_data_json = phobert_result
            #     except Exception as e:
            #         logger.warning(f"Lỗi PhoBERT Slot: {e}")
            
            # # 4.2 Fallback Regex
            # if not parsed_data_json:
            #     parsed_data_json = parse_with_regex(norm_text)
                
            if parsed_data_json:
                text = normalize_activity_json(parsed_data_json)
            elif config.LLM_ENABLED:
                # Nếu PhoBERT/Regex thất bại hoặc không đủ tự tin, fallback qua LLM
                await processing_msg.edit_text("🤖 PhoBERT/Regex thất bại, đang phân tích bằng LLM...")
                text = await llm_engine.post_process_stt(text)

        # 5. Trả kết quả đã định dạng (HTML bảng biểu + JSON Code block)
        if not text:
            await processing_msg.edit_text("Không nhận diện được nội dung trong file audio.")
        elif not config.LLM_ENABLED:
            await processing_msg.edit_text(text)
        else:
            full_msg, summary_msg, parse_mode, file_content, filename = format_telegram_message(text)
            if len(full_msg) <= TELEGRAM_TEXT_LIMIT:
                await processing_msg.edit_text(full_msg, parse_mode=parse_mode)
            else:
                out_path = os.path.join(config.TMP_DIR, f"{job_id}_{filename}")
                try:
                    with open(out_path, "w", encoding="utf-8") as f:
                        f.write(file_content)
                    await processing_msg.edit_text(
                        summary_msg,
                        parse_mode=parse_mode,
                    )
                    with open(out_path, "rb") as f:
                        await message.reply_document(f, filename=filename)
                finally:
                    if os.path.exists(out_path):
                        os.remove(out_path)

    except Exception:  # noqa: BLE001
        logger.exception("Lỗi không xác định khi xử lý audio.")
        await processing_msg.edit_text("Đã có lỗi xảy ra, vui lòng thử lại.")

    finally:
        # 6. Luôn dọn dẹp file tạm dù thành công hay thất bại
        for path in (raw_path, wav_path):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                logger.warning("Không xoá được file tạm: %s", path)


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Nhận tin nhắn văn bản trực tiếp từ người dùng, chạy qua LLM bóc tách nhật ký và trả về JSON đã định dạng.
    """
    message = update.effective_message
    if not message or not message.text:
        return

    text_input = message.text.strip()
    if not text_input:
        return

    job_id = uuid.uuid4().hex
    processing_msg = await message.reply_text("🤖 Đang phân tích nhật ký canh tác...")

    try:
        text = text_input
        norm_text = normalize_text(text)
        parsed_data_json = None
        
        # 1. Thử PhoBERT Slot
        from services.phobert_slot import phobert_slot_engine
        if phobert_slot_engine.session:
            try:
                phobert_result = phobert_slot_engine.extract_slots(norm_text)
                if phobert_result:
                    parsed_data_json = phobert_result
            except Exception as e:
                logger.warning(f"Lỗi PhoBERT Slot: {e}")
                
        # 2. Fallback Regex
        if not parsed_data_json:
            parsed_data_json = parse_with_regex(norm_text)
            
        if parsed_data_json:
            text = normalize_activity_json(parsed_data_json)
        elif config.LLM_ENABLED:
            await processing_msg.edit_text("🤖 PhoBERT/Regex thất bại, đang phân tích bằng LLM...")
            text = await llm_engine.post_process_stt(text_input)
            
        # Always render telegram message if we tried to parse
        if parsed_data_json or config.LLM_ENABLED:
            full_msg, summary_msg, parse_mode, file_content, filename = format_telegram_message(text)
            if len(full_msg) <= TELEGRAM_TEXT_LIMIT:
                await processing_msg.edit_text(full_msg, parse_mode=parse_mode)
            else:
                out_path = os.path.join(config.TMP_DIR, f"{job_id}_{filename}")
                try:
                    with open(out_path, "w", encoding="utf-8") as f:
                        f.write(file_content)
                    await processing_msg.edit_text(
                        summary_msg,
                        parse_mode=parse_mode,
                    )
                    with open(out_path, "rb") as f:
                        await message.reply_document(f, filename=filename)
                finally:
                    if os.path.exists(out_path):
                        os.remove(out_path)
        else:
            await processing_msg.edit_text(text)
    except Exception:  # noqa: BLE001
        logger.exception("Lỗi khi xử lý tin nhắn văn bản.")
        await processing_msg.edit_text("Đã có lỗi xảy ra khi phân tích nhật ký, vui lòng thử lại.")


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Xử lý khi người dùng gửi ảnh (hóa đơn phân bón, bao bì thuốc BVTV, sổ tay canh tác) qua bot Telegram.
    Sử dụng RapidOCR để nhận diện chữ, sau đó trích xuất thông tin chuẩn hóa VietGAP.
    """
    message = update.message
    if not message or not message.photo:
        return

    # Lấy ảnh có độ phân giải cao nhất
    photo = message.photo[-1]
    job_id = uuid.uuid4().hex

    processing_msg = await message.reply_text("📸 Đang tải ảnh và nhận diện văn bản (RapidOCR)...")

    try:
        tg_file = await context.bot.get_file(photo.file_id)
        photo_bytes = await tg_file.download_as_bytearray()

        # Nhận diện chữ bằng RapidOCR bất đồng bộ
        ocr_res = await ocr_engine.ocr(bytes(photo_bytes))

        if not ocr_res.text:
            await processing_msg.edit_text(
                "⚠️ Không phát hiện thấy văn bản rõ ràng trong ảnh.\n"
                "Vui lòng chụp lại ảnh rõ nét, đủ ánh sáng và chụp cận cảnh hóa đơn/bao bì."
            )
            return

        ocr_summary = (
            f"📄 <b>Văn bản nhận diện ({ocr_res.total_lines} dòng, {ocr_res.avg_score:.0%}):</b>\n"
            f"<i>{html.escape(ocr_res.text[:400])}{'...' if len(ocr_res.text) > 400 else ''}</i>\n\n"
            "🤖 Đang trích xuất dữ liệu canh tác VietGAP..."
        )
        await processing_msg.edit_text(ocr_summary, parse_mode=ParseMode.HTML)

        # Chạy trích xuất qua pipeline
        text = ocr_res.text
        norm_text = normalize_text(text)
        parsed_data_json = None

        # 1. Thử PhoBERT Slot nếu có
        try:
            from services.phobert_slot import phobert_slot_engine
            if getattr(phobert_slot_engine, "session", None):
                parsed_data_json = phobert_slot_engine.extract_slots(norm_text)
        except Exception:
            pass

        # 2. Fallback Regex
        if not parsed_data_json:
            parsed_data_json = parse_with_regex(norm_text)

        if parsed_data_json:
            text = normalize_activity_json(parsed_data_json)
        elif config.LLM_ENABLED:
            text = await llm_engine.post_process_stt(ocr_res.text)

        if parsed_data_json or config.LLM_ENABLED:
            full_msg, summary_msg, parse_mode, file_content, filename = format_telegram_message(text)
            if len(full_msg) <= TELEGRAM_TEXT_LIMIT:
                await processing_msg.edit_text(full_msg, parse_mode=parse_mode)
            else:
                out_path = os.path.join(config.TMP_DIR, f"{job_id}_{filename}")
                try:
                    with open(out_path, "w", encoding="utf-8") as f:
                        f.write(file_content)
                    await processing_msg.edit_text(summary_msg, parse_mode=parse_mode)
                    with open(out_path, "rb") as f:
                        await message.reply_document(f, filename=filename)
                finally:
                    if os.path.exists(out_path):
                        os.remove(out_path)
        else:
            await processing_msg.edit_text(
                f"📄 <b>Văn bản nhận diện:</b>\n{html.escape(ocr_res.text)}",
                parse_mode=ParseMode.HTML,
            )

    except OCRError as exc:
        logger.exception("Lỗi khi xử lý OCR ảnh Telegram: %s", exc)
        await processing_msg.edit_text(f"Đã có lỗi khi nhận diện ảnh: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Lỗi không mong muốn khi xử lý ảnh Telegram: %s", exc)
        await processing_msg.edit_text("Đã có lỗi xảy ra khi xử lý ảnh, vui lòng thử lại sau.")

