"""
Conversation Manager - Trình điều phối trung tâm cho hội thoại đa lượt Nông Ký AI.
Tích hợp toàn diện:
- Gemini NLU (ý định, bóc tách và gộp slot)
- VLM Engine (xử lý ảnh 2 hướng: Ghi nhật ký & Trả lời thông tin thuốc, phân, sâu bệnh)
- Template System (sinh câu hỏi điền khuyết tự nhiên)
- STT & TTS Engine (nhận diện giọng nói và phát âm phản hồi)
- Normalizer & Material Matcher (chuẩn hóa dữ liệu và khớp CSDL)
- MySQL Database (lưu trữ VietGAP khép kín)
"""

from __future__ import annotations

import asyncio
from datetime import date
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import config
from conversation.gemini_helper import gemini_conv_helper
from conversation.state import (
    ConversationState,
    DialogState,
    SessionStore,
    UserIntent,
    session_store,
)
from conversation.templates import (
    ACTIVITY_REQUIRED_FIELDS,
    TASK_NAME_DISPLAY,
    build_advisory_response,
    build_cancel_message,
    build_clarification_question,
    build_confirmation_message,
    build_image_choice_prompt,
    build_lead_and_sections,
    build_success_message,
    build_tts_spoken_text,
    canonicalize_slots,
    check_missing_fields,
)
from services.audio.audio_converter import resample_to_16k_mono_pcm16
from services.database import get_crop_name_for_farmer, get_default_season_id, save_diary_to_mysql
from services.vietgap.material_matcher import material_matcher
from services.normalizer import normalize_text
from services.engines.stt_engine import stt_engine
from services.rag.rag_engine import rag_engine, RAGAnswerResult
from services.engines.tts_engine import tts_engine
from services.engines.vlm_engine import vlm_engine

logger = logging.getLogger("conversation.manager")


class ConversationManager:
    """Bộ điều phối hội thoại đa lượt thông minh cho nông nghiệp VietGAP."""

    def __init__(self, store: Optional[SessionStore] = None):
        self.store = store or session_store

    @staticmethod
    def _is_side_question(text: str) -> bool:
        """Kiểm tra câu nói có phải là câu hỏi tra cứu kỹ thuật xen ngang hay không."""
        clean = (text or "").lower().strip()
        if "?" in clean:
            return True
        question_patterns = [
            r"mấy\s+ngày",
            r"bao\s+nhiêu",
            r"thế\s+nào",
            r"ra\s+sao",
            r"như\s+thế\s+nào",
            r"trị\s+(?:bệnh\s+)?gì",
            r"chữa\s+(?:bệnh\s+)?gì",
            r"phòng\s+(?:trừ\s+)?gì",
            r"công\s+dụng\s+gì",
            r"pha\s+(?:như\s+)?thế\s+nào",
            r"pha\s+sao",
            r"bón\s+sao",
            r"bón\s+(?:như\s+)?thế\s+nào",
            r"độc\s+không",
            r"có\s+độc\s+không",
            r"bao\s+lâu",
            r"là\s+gì",
            r"tại\s+sao",
            r"thuốc\s+gì",
            r"phân\s+gì",
            r"cách\s+dùng",
            r"liều\s+dùng",
            r"liều\s+lượng",
            r"thời\s+gian\s+cách\s+ly",
            r"cách\s+ly",
            r"có\s+được\s+không",
            r"dùng\s+được\s+không",
            r"xịt\s+được\s+không",
            r"phun\s+được\s+không",
        ]
        for p in question_patterns:
            if re.search(p, clean):
                return True
        return False

    def _extract_bridge_from_rag(
        self,
        question: str,
        rag_text: str,
        sources: List[Dict[str, Any]],
        crop_name: str,
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        """
        Bóc tách thông tin từ kết quả RAG để chuẩn bị sẵn sàng cho Context Bridge
        (chuyển sang ghi nhật ký một chạm mà không cần người dùng nhập lại).
        """
        q_lower = (question or "").lower()
        ans_lower = (rag_text or "").lower()
        combined = f"{q_lower} {ans_lower}"

        # 1. Danh sách sâu bệnh phổ biến trên cây ăn quả / bưởi
        known_pests = [
            "bọ trĩ", "sâu vẽ bùa", "rầy chổng cánh", "nhện đỏ", "nhện trắng",
            "rệp sáp", "bọ xít xanh", "sâu đục quả", "ruồi đục quả", "ruồi vàng",
            "thán thư", "loét", "ghẻ sẹo", "vàng lá gân xanh", "greening",
            "thối rễ", "nấm hồng", "chảy gôm", "nứt thân chảy mủ", "bệnh sương mai",
            "thối nhũn", "đốm mắt cua", "cháy lá", "sâu cuốn lá", "sâu tơ"
        ]
        matched_pest = None
        for p in known_pests:
            if p in q_lower or p in ans_lower:
                matched_pest = p.title()
                break

        # 2. Quét danh mục vật tư từ CSDL (material_matcher)
        material_matcher.reload_catalog()
        matched_med = None
        matched_med_info = None
        matched_fert = None
        matched_fert_info = None

        source_titles = " ".join([(s.get("section_title") or s.get("title") or "") for s in (sources or [])]).lower()

        for item in (material_matcher._db_cache or []):
            i_clean = (item.get("cleaned_name") or "").lower()
            if not i_clean or len(i_clean) < 3:
                continue
            if i_clean in source_titles or i_clean in ans_lower:
                if item["category"] == "pesticide" and not matched_med:
                    matched_med = item["name"]
                    matched_med_info = item
                elif item["category"] == "fertilizer" and not matched_fert:
                    matched_fert = item["name"]
                    matched_fert_info = item

        # 3. Quét regex nếu chưa thấy thuốc trong CSDL catalog
        if not matched_med:
            med_patterns = [
                r"\b(?:thuốc|chế phẩm)\s+([A-Za-z0-9\s]{2,20}?(?:WG|WP|SC|EC|SL|ND|SE|OD|GR|BR))\b",
                r"\b([A-Z][a-zA-Z0-9]{2,15}\s+\d+[A-Z]{2})\b",
            ]
            for pat in med_patterns:
                m = re.search(pat, rag_text)
                if m:
                    cand = m.group(1).strip()
                    if cand.lower() not in ("thuốc bảo vệ thực vật", "thuốc sinh học", "thuốc hóa học"):
                        matched_med = cand
                        break

        # 4. Quét thời gian cách ly (PHI) trong văn bản RAG
        phi_days = None
        if matched_med_info and matched_med_info.get("min_isolation_days"):
            phi_days = matched_med_info["min_isolation_days"]
        if not phi_days:
            m_phi = re.search(r"(?:cách ly|PHI)[^\d]{1,30}?(\d+)\s*ngày", rag_text, re.IGNORECASE)
            if m_phi:
                try:
                    phi_days = int(m_phi.group(1))
                except Exception:
                    pass

        # 5. Xác định hoạt động phù hợp
        # A. Phun thuốc BVTV
        if matched_med or any(w in combined for w in ["phun thuốc", "xịt thuốc", "trị sâu", "trừ sâu", "trừ bệnh", "thuốc bvtv"]):
            bridge_slots = {
                "activity_date": date.today().isoformat(),
                "crop_name": crop_name or "Bưởi",
            }
            if matched_med:
                bridge_slots["ten_thuoc_bvtv"] = matched_med
                if matched_med_info and matched_med_info.get("id"):
                    bridge_slots["pesticide_id"] = matched_med_info["id"]
            if matched_pest:
                bridge_slots["target_pest"] = matched_pest
            if phi_days:
                bridge_slots["thoi_gian_cach_ly"] = phi_days
            return "spray", bridge_slots

        # B. Bón phân
        if matched_fert or any(w in combined for w in ["bón phân", "phân bón", "npk", "phân chuồng", "bón lót", "bón thúc"]):
            bridge_slots = {
                "activity_date": date.today().isoformat(),
                "crop_name": crop_name or "Bưởi",
            }
            if matched_fert:
                bridge_slots["ten_phan_bon"] = matched_fert
                if matched_fert_info and matched_fert_info.get("id"):
                    bridge_slots["fertilizer_id"] = matched_fert_info["id"]
            return "fertilize", bridge_slots

        # C. Theo dõi sâu bệnh hại (Quan sát)
        if matched_pest:
            bridge_slots = {
                "activity_date": date.today().isoformat(),
                "crop_name": crop_name or "Bưởi",
                "sinh_vat_gay_hai": matched_pest,
            }
            return "observation", bridge_slots

        return None, {}

    # =========================================================================
    # 1. TIẾP NHẬN ĐẦU VÀO VĂN BẢN (TEXT PROCESSING)
    # =========================================================================

    async def handle_text(
        self,
        session_id: str,
        user_text: str,
        farmer_id: Optional[int] = 1,
        season_id: Optional[int] = None,
        enable_tts: bool = False,
        tts_output_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Xử lý 1 lượt nói dạng văn bản từ người dùng.
        Trả về kết quả gồm: text phản hồi, trạng thái mới, slots hiện tại, file audio TTS (nếu bật).
        """
        clean_text = normalize_text(user_text)
        if not clean_text:
            return {
                "status": "warning",
                "message": "Văn bản rỗng.",
                "response_text": "Bác vui lòng nói hoặc nhập thông tin cụ thể nhé!",
                "dialog_state": DialogState.IDLE.value,
            }

        state = self.store.get_or_create(session_id, farmer_id=farmer_id, season_id=season_id)
        if not state.crop_name or state.crop_name in ("Cà gai leo", "Bưởi"):
            try:
                from services.database import get_db_connection, get_crop_name_for_farmer
                conn = get_db_connection()
                fetched = get_crop_name_for_farmer(conn, user_id=farmer_id, season_id=season_id)
                conn.close()
                if fetched:
                    state.crop_name = fetched
            except Exception as _e_crop:
                logger.debug("Không lấy được crop_name từ DB: %s", _e_crop)

        state.add_turn(role="user", text=clean_text)

        response_text = ""
        action_taken = "NONE"
        suggested_actions: List[Dict[str, Any]] = []
        rag_sources: List[Dict[str, Any]] = []
        rag_spoken_text: Optional[str] = None

        # ---------------------------------------------------------------------
        # 0. CHỐT CHẶN AN TOÀN HÓA CHẤT & CHẤT CẤM (SAFETY GUARDRAIL < 2ms)
        # ---------------------------------------------------------------------
        from services.rag.safety_checker import safety_checker
        safety_ans = safety_checker.route_and_answer_safety_query(clean_text)
        if safety_ans and safety_ans.handled:
            logger.info("🛡️ SafetyGuardrail kích hoạt: entity=%s, is_banned=%s", 
                        safety_ans.entity_name, safety_ans.is_banned)
            if safety_ans.is_banned:
                state.accumulated_slots.pop("ten_thuoc_bvtv", None)
                state.accumulated_slots.pop("material_name", None)
                state.accumulated_slots.pop("pesticide_id", None)
                if state.current_activity in ("spray", "purchase") or state.state in (DialogState.COLLECTING, DialogState.CLARIFYING, DialogState.CONFIRMING):
                    state.state = DialogState.IDLE
                    state.current_activity = None

            response_text = safety_ans.markdown_answer
            action_taken = "SAFETY_GUARDRAIL_TRIGGERED"
            state.add_turn(role="assistant", text=response_text)

            audio_result_path = None
            if enable_tts and response_text:
                tts_text = safety_ans.spoken_text or build_tts_spoken_text(response_text)
                audio_result_path = self._generate_tts_audio(tts_text, tts_output_path)
                if audio_result_path:
                    state.register_tts_audio(audio_result_path)

            return {
                "status": "success",
                "session_id": session_id,
                "dialog_state": state.state.value,
                "current_activity": state.current_activity,
                "action_taken": action_taken,
                "response_text": response_text,
                "accumulated_slots": state.accumulated_slots,
                "missing_slots": state.missing_slots,
                "analysis": None,
                "audio_file": audio_result_path,
                "suggested_actions": [],
                "safety_guardrail": safety_ans.to_dict(),
            }

        # ---------------------------------------------------------------------
        # TH 1: Đang ở trạng thái tư vấn (ADVISORY) hoặc có Context Bridge sẵn
        # Nông dân trả lời câu hỏi: "Bác có muốn ghi nhận việc này vào sổ nhật ký không?"
        # ---------------------------------------------------------------------
        has_advisory_context = (state.state == DialogState.ADVISORY and bool(state.current_activity or state.bridge_activity)) or bool(state.bridge_activity)
        if state.state == DialogState.ADVISORY and not has_advisory_context:
            state.state = DialogState.IDLE

        if has_advisory_context:
            text_lower = clean_text.lower()

            # Kiểm tra xem ảnh trước đó có phải thuốc cấm không
            is_banned_img = False
            if state.last_image_analysis:
                p_info = state.last_image_analysis.get("pesticide") or {}
                lead_i = state.last_image_analysis.get("lead_info") or {}
                is_banned_img = bool(p_info.get("is_banned") or lead_i.get("type") == "pesticide_banned")

            # Kiểm tra xem câu hỏi có trùng với một trong các nút section đã sinh trước đó không
            matched_sec = None
            if state.last_image_analysis and state.last_image_analysis.get("sections"):
                for sec in state.last_image_analysis["sections"]:
                    if sec.get("is_action") or sec.get("id") == "diary":
                        continue
                    q_sec = (sec.get("question") or "").lower()
                    s_id = (sec.get("id") or "").lower()
                    if (q_sec and (q_sec == text_lower or (len(text_lower) >= 8 and (q_sec in text_lower or text_lower in q_sec)))) or (s_id and s_id == text_lower):
                        matched_sec = sec
                        break

            question_words = [
                "không?", "thế nào", "như thế nào", "ra sao", "sao", "bao nhiêu",
                "bao lâu", "thuốc gì", "gì", "ai", "đâu", "khi nào", "tại sao", "vì sao",
                "có được không", "dùng được không", "xịt được không", "phun được không",
                "độc không", "trị được không", "là gì", "phải không", "đúng không"
            ]
            is_question = clean_text.endswith("?") or any(w in text_lower for w in question_words) or self._is_side_question(clean_text)

            decline_phrases = [
                "không cần", "không muốn", "thôi", "chưa cần", "để sau", "chỉ hỏi",
                "cảm ơn", "thôi không cần", "bỏ qua", "chưa", "không ghi", "đừng ghi",
                "không lưu", "đừng lưu"
            ]
            is_pure_decline = not is_question and any(p in text_lower for p in decline_phrases)

            explicit_diary_phrases = [
                "ghi việc", "ghi vào", "ghi sổ", "ghi nhật ký", "lưu nhật ký",
                "ghi nhận thông tin", "đồng ý lưu", "lưu vào sổ", "lưu sổ",
                "ghi đi", "lưu đi", "đồng ý ghi", "lưu giúp", "ghi giúp", "tạo phiếu",
                "ghi xử lý", "ghi chép", "ghi mua", "ghi bán", "ghi bón", "ghi phun",
                "xử lý vỏ bao bì", "xử lý bao bì", "thu gom vỏ bao bì"
            ]
            short_affirmations = [
                "có", "có chứ", "được", "được chứ", "đồng ý", "ok", "oke", "okay",
                "chuẩn", "chuẩn rồi", "lưu", "ghi", "ừ", "uh", "dạ có", "bác có",
                "lưu nhé", "ghi nhé", "lưu nha", "ghi nha", "có lưu", "có ghi",
                "ghi luôn", "lưu luôn", "có ghi luôn", "có lưu luôn"
            ]
            has_explicit_phrase = (
                any(p in text_lower for p in explicit_diary_phrases) or
                text_lower.startswith(("ghi ", "lưu "))
            ) and not any(neg in text_lower for neg in ["không", "đừng", "chưa", "thôi"])
            clean_words = re.findall(r"\w+", text_lower)
            is_short_affirmation = text_lower.strip() in short_affirmations or (
                len(clean_words) <= 4 and any(w in clean_words for w in ["có", "được", "đồng ý", "ok", "ừ", "ghi", "lưu"]) and not any(w in clean_words for w in ["không", "chưa", "gì", "sao", "hả"])
            )
            is_diary_action = not is_pure_decline and not is_question and (has_explicit_phrase or is_short_affirmation)
            dose_units = ["bình", "lít", "bao", "kg", "tạ", "tấn", "chai", "gói", "sào", "gốc"]
            has_extra_specs = bool(re.search(r"\b\d+[\.,]?\d*\s*(?:" + "|".join(dose_units) + r")\b", text_lower)) or any(k in text_lower for k in ["hôm qua", "sáng nay", "chiều nay"])

            # Người dùng từ chối ghi nhật ký
            if is_pure_decline:
                state.bridge_activity = None
                state.bridge_slots = {}
                state.reset()
                response_text = "Dạ vâng! Khi nào cần tư vấn kỹ thuật hoặc ghi chép nhật ký công việc khác, bác cứ nhắn em nhé!"
                action_taken = "DIARY_PROMPT_DECLINED"

            # Người dùng bấm hoặc nói muốn ghi nhật ký thuốc CẤM
            elif is_banned_img and is_diary_action:
                state.bridge_activity = None
                state.bridge_slots = {}
                state.reset()
                state.state = DialogState.IDLE
                response_text = (
                    "⛔ **HỆ THỐNG TỪ CHỐI GHI NHẬT KÝ VIETGAP:**\n\n"
                    "Thuốc này nằm trong danh mục **CẤM SỬ DỤNG TẠI VIỆT NAM** theo Thông tư 75/BNNMT & Luật BVTV 41/2013.\n"
                    "Quy chuẩn VietGAP tuyệt đối nghiêm cấm sử dụng và lưu trữ thuốc cấm trong trang trại. Bác vui lòng chuyển sang các chế phẩm sinh học an toàn nhé!"
                )
                action_taken = "BANNED_PESTICIDE_DIARY_REFUSED"

            # Khớp câu hỏi nhanh từ nút section
            elif matched_sec:
                response_text = matched_sec["content"]
                action_taken = f"SECTION_ANSWERED_{matched_sec.get('id', '').upper()}"

            # Người dùng đồng ý ghi nhật ký (từ nút bấm hoặc nói "ghi sổ", "có") không bổ sung thêm thông số
            # → Kế thừa từ Bridge hoặc Image, chuyển thẳng sang xác nhận/hỏi thêm
            elif is_diary_action and not has_extra_specs:
                if state.bridge_activity:
                    # Nếu câu nói của người dùng chỉ đích danh một hoạt động khác
                    explicit_act = None
                    if any(k in text_lower for k in ["mua", "nhập", "vật tư", "đại lý"]):
                        explicit_act = "purchase"
                    elif any(k in text_lower for k in ["xử lý bao bì", "vỏ bao", "bao bì", "vỏ chai", "tiêu hủy"]):
                        explicit_act = "waste"
                    elif any(k in text_lower for k in ["phun", "xịt"]):
                        explicit_act = "spray"
                    elif any(k in text_lower for k in ["bón", "rải phân"]):
                        explicit_act = "fertilize"

                    if explicit_act and explicit_act != state.bridge_activity:
                        state.bridge_activity = explicit_act
                        if explicit_act == "purchase" and state.last_saved_activity in ("spray", "fertilize"):
                            last_mat = state.last_saved_slots.get("ten_thuoc_bvtv") or state.last_saved_slots.get("ten_phan_bon")
                            state.bridge_slots = {
                                "material_name": last_mat,
                                "material_type": "Thuốc BVTV" if state.last_saved_activity == "spray" else "Phân bón",
                                "purchase_date": date.today().isoformat(),
                                "activity_date": date.today().isoformat(),
                                "crop_name": state.last_saved_slots.get("crop_name") or state.crop_name or "Bưởi diễn",
                            }
                        elif explicit_act == "waste" and state.last_saved_activity == "spray":
                            last_mat = state.last_saved_slots.get("ten_thuoc_bvtv") or state.last_saved_slots.get("pesticide_name")
                            state.bridge_slots = {
                                "material_name": last_mat,
                                "waste_type": "Vỏ chai, bao bì thuốc BVTV",
                                "treatment_date": date.today().isoformat(),
                                "activity_date": date.today().isoformat(),
                                "crop_name": state.last_saved_slots.get("crop_name") or state.crop_name or "Bưởi diễn",
                            }

                    state.current_activity = state.bridge_activity
                    if state.bridge_slots:
                        for k, v in state.bridge_slots.items():
                            if k not in state.accumulated_slots or not state.accumulated_slots[k]:
                                state.accumulated_slots[k] = v
                    state.bridge_activity = None
                    state.bridge_slots = {}

                if not state.current_activity:
                    state.state = DialogState.IDLE
                    response_text = "Dạ bác muốn ghi chép công việc gì vào sổ nhật ký thế ạ (phun thuốc, bón phân, mua vật tư...)?"
                    action_taken = "DIARY_PROMPT_ACTIVITY_UNKNOWN"
                else:
                    if not state.accumulated_slots.get("crop_name"):
                        state.accumulated_slots["crop_name"] = state.crop_name or "Bưởi diễn"
                    if not state.accumulated_slots.get("activity_date"):
                        state.accumulated_slots["activity_date"] = date.today().isoformat()

                    self._match_materials_in_slots(state)
                    state.accumulated_slots = canonicalize_slots(state.accumulated_slots, state.current_activity)
                    missing = check_missing_fields(state.current_activity, state.accumulated_slots)
                    state.missing_slots = missing
                    act_display = TASK_NAME_DISPLAY.get(state.current_activity, state.current_activity)

                    if missing:
                        state.state = DialogState.CLARIFYING
                        q = build_clarification_question(state.current_activity, state.accumulated_slots, missing)
                        response_text = f"Dạ em chuyển sang ghi sổ **{act_display}** nhé!\n{q}"
                        action_taken = "DIARY_STARTED_FROM_IMAGE_CLARIFYING"
                    else:
                        state.state = DialogState.CONFIRMING
                        response_text = build_confirmation_message(state.current_activity, state.accumulated_slots)
                        action_taken = "DIARY_STARTED_FROM_IMAGE_CONFIRMING"

            # Người dùng hỏi thông tin kỹ thuật (liều lượng, cách ly, thuốc đặc trị, phòng trừ...) hoặc không phải đồng ý ghi sổ
            elif is_question or not is_diary_action:
                rag_res = await rag_engine.ask_async(
                    question=clean_text,
                    crop_name=state.crop_name,
                )
                state.last_rag_result = rag_res.to_dict()
                response_text = rag_res.answer
                rag_sources = []
                suggested_actions = []
                rag_spoken_text = rag_res.spoken_text

                # RAG thuần trả lời kỹ thuật, không gợi ý ghi sổ (Phương án A)
                action_taken = "IMAGE_OR_ADVISORY_RAG_ANSWERED"

            # Người dùng đồng ý + cung cấp thêm thông tin cụ thể (có số lượng, liều lượng...)
            # → Gọi NLU để trích xuất các slot bổ sung từ câu nói
            else:
                if state.bridge_activity:
                    state.current_activity = state.bridge_activity
                    if state.bridge_slots:
                        for k, v in state.bridge_slots.items():
                            if k not in state.accumulated_slots or not state.accumulated_slots[k]:
                                state.accumulated_slots[k] = v
                    state.bridge_activity = None
                    state.bridge_slots = {}

                if not state.current_activity:
                    state.current_activity = "general"

                nlu_res = await gemini_conv_helper.classify_and_extract_slots(
                    user_text=clean_text,
                    current_activity=state.current_activity,
                    accumulated_slots=state.accumulated_slots,
                    dialogue_state="CLARIFYING",
                    history_turns=[t.to_dict() for t in state.history],
                    crop_name=state.crop_name,
                )
                extra_slots = nlu_res.get("extracted_slots", {})
                extra_slots = canonicalize_slots(extra_slots, state.current_activity)
                if extra_slots:
                    state.update_slots(extra_slots)

                if not state.accumulated_slots.get("crop_name"):
                    state.accumulated_slots["crop_name"] = state.crop_name or "Bưởi diễn"
                if not state.accumulated_slots.get("activity_date"):
                    state.accumulated_slots["activity_date"] = date.today().isoformat()

                self._match_materials_in_slots(state)
                state.accumulated_slots = canonicalize_slots(state.accumulated_slots, state.current_activity)

                missing = check_missing_fields(state.current_activity, state.accumulated_slots)
                state.missing_slots = missing

                act_display = TASK_NAME_DISPLAY.get(state.current_activity, state.current_activity)

                if missing:
                    state.state = DialogState.CLARIFYING
                    q = build_clarification_question(state.current_activity, state.accumulated_slots, missing)
                    response_text = f"Dạ em chuyển sang ghi sổ **{act_display}** nhé!\n{q}"
                    action_taken = "DIARY_STARTED_FROM_IMAGE_CLARIFYING"
                else:
                    state.state = DialogState.CONFIRMING
                    response_text = build_confirmation_message(state.current_activity, state.accumulated_slots)
                    action_taken = "DIARY_STARTED_FROM_IMAGE_CONFIRMING"

        # ---------------------------------------------------------------------
        # TH 1.5: Hỏi đáp kỹ thuật xen ngang khi đang điền nhật ký (In-flow Q&A)
        # Nông dân hỏi về cách ly, liều lượng, công dụng... trong khi bot đang hỏi thiếu slot
        # ---------------------------------------------------------------------
        elif state.state in (DialogState.CLARIFYING, DialogState.CONFIRMING) and state.current_activity and self._is_side_question(clean_text):
            logger.info("💡 In-flow Side-Question phát hiện: '%s' (Activity: %s)", clean_text, state.current_activity)
            rag_res = await rag_engine.ask_async(
                question=clean_text,
                crop_name=state.crop_name,
            )
            rag_sources = []
            rag_spoken_text = rag_res.spoken_text
            auto_filled = []

            # Thử tự động điền các slot còn thiếu dựa trên kết quả RAG & CSDL vật tư
            if state.current_activity == "spray":
                # 1. Thời gian cách ly (thoi_gian_cach_ly)
                if not state.accumulated_slots.get("thoi_gian_cach_ly"):
                    phi_found = None
                    med_name = state.accumulated_slots.get("ten_thuoc_bvtv")
                    if med_name:
                        m_item = material_matcher.fuzzy_match(med_name, category_hint="pesticide")
                        if m_item and m_item.min_isolation_days:
                            phi_found = m_item.min_isolation_days
                    if not phi_found:
                        m_phi = re.search(r"(?:cách ly|PHI)[^\d]{1,60}?(\d+)\s*ngày", rag_res.answer, re.IGNORECASE)
                        if m_phi:
                            try:
                                phi_found = int(m_phi.group(1))
                            except Exception:
                                pass
                    if phi_found:
                        state.accumulated_slots["thoi_gian_cach_ly"] = phi_found
                        auto_filled.append(("thời gian cách ly", f"{phi_found} ngày"))

                # 2. Đối tượng sâu bệnh (target_pest)
                if not state.accumulated_slots.get("target_pest"):
                    known_pests = [
                        "bọ trĩ", "sâu vẽ bùa", "rầy chổng cánh", "nhện đỏ", "nhện trắng",
                        "rệp sáp", "bọ xít xanh", "sâu đục quả", "ruồi đục quả", "ruồi vàng",
                        "thán thư", "loét", "ghẻ sẹo", "vàng lá gân xanh", "greening",
                        "thối rễ", "nấm hồng", "chảy gôm", "nứt thân chảy mủ", "bệnh sương mai",
                        "thối nhũn", "đốm mắt cua", "cháy lá", "sâu cuốn lá", "sâu tơ"
                    ]
                    for p in known_pests:
                        if p in clean_text.lower() or p in rag_res.answer.lower():
                            state.accumulated_slots["target_pest"] = p.title()
                            auto_filled.append(("sâu bệnh", p.title()))
                            break

            elif state.current_activity == "fertilize":
                # Tên phân bón nếu chưa có
                if not state.accumulated_slots.get("ten_phan_bon"):
                    material_matcher.reload_catalog()
                    for item in (material_matcher._db_cache or []):
                        if item["category"] == "fertilizer" and item["cleaned_name"].lower() in rag_res.answer.lower():
                            state.accumulated_slots["ten_phan_bon"] = item["name"]
                            auto_filled.append(("tên phân bón", item["name"]))
                            break

            # Tạo nội dung trả lời súc tích
            rag_display_answer = rag_res.answer.split("\n\n")[0].strip()
            if len(rag_display_answer) > 280:
                dots = [i for i, ch in enumerate(rag_display_answer[:280]) if ch in ('.', '!', '?')]
                if dots:
                    rag_display_answer = rag_display_answer[:dots[-1] + 1]

            rag_tts_answer = rag_res.spoken_text or rag_display_answer

            fill_note = ""
            if auto_filled:
                fill_str = ", ".join([f"{name}: **{val}**" for name, val in auto_filled])
                fill_note = f"\n\n✅ *Em đã tự động điền {fill_str} vào phiếu cho bác rồi ạ!*"

            # Kiểm tra các trường còn thiếu sau khi đã tự động điền
            missing = check_missing_fields(state.current_activity, state.accumulated_slots)
            state.missing_slots = missing

            if missing:
                state.state = DialogState.CLARIFYING
                clarify_q = build_clarification_question(state.current_activity, state.accumulated_slots, missing)
                response_text = f"{rag_display_answer}{fill_note}\n\n👉 Bác cho em hỏi thêm thông tin tiếp theo nhé:\n{clarify_q}"
                action_taken = "IN_FLOW_SIDE_QUESTION_CLARIFYING"
                rag_spoken_text = f"{rag_tts_answer}. Bác cho em hỏi thêm: {build_tts_spoken_text(clarify_q)}"
            else:
                state.state = DialogState.CONFIRMING
                conf_msg = build_confirmation_message(state.current_activity, state.accumulated_slots)
                response_text = f"{rag_display_answer}{fill_note}\n\n📋 Thông tin phiếu đã đầy đủ rồi ạ:\n{conf_msg}"
                action_taken = "IN_FLOW_SIDE_QUESTION_CONFIRMING"
                rag_spoken_text = f"{rag_tts_answer}. Thông tin phiếu đã đầy đủ, bác kiểm tra và xác nhận lưu giúp em nhé!"

        # ---------------------------------------------------------------------
        # TH 2: Xử lý thông thường qua Gemini NLU & Quản lý Slot
        # ---------------------------------------------------------------------
        else:
            # 1. Gọi Gemini phân loại ý định và trích xuất slot
            nlu_res = await gemini_conv_helper.classify_and_extract_slots(
                user_text=clean_text,
                current_activity=state.current_activity,
                accumulated_slots=state.accumulated_slots,
                dialogue_state=state.state.value,
                history_turns=[t.to_dict() for t in state.history],
                crop_name=state.crop_name,
                last_saved_activity=state.last_saved_activity,
                last_saved_slots=state.last_saved_slots,
            )

            intent = nlu_res.get("intent", UserIntent.UNKNOWN.value)
            activity_type = nlu_res.get("activity_type") or state.current_activity
            extracted_slots = nlu_res.get("extracted_slots", {})
            corrected_slots = nlu_res.get("corrected_slots", {})

            # Lưu hoạt động thứ 2 nếu câu nói kết hợp (vd: vừa mua vật tư vừa mang đi phun luôn)
            secondary_act = nlu_res.get("secondary_activity")
            secondary_slots = nlu_res.get("secondary_slots") or {}
            if secondary_act:
                state.meta["pending_activity"] = {
                    "activity": secondary_act,
                    "slots": secondary_slots,
                }

            # Nếu NLU trả về CONFIRM nhưng câu phát ngôn của nông dân không chứa bất kỳ từ xác nhận nào (chỉ cung cấp slot) -> chuyển thành PROVIDE_SLOT
            if intent == UserIntent.CONFIRM.value and extracted_slots:
                confirm_words = ["lưu", "ok", "đồng ý", "đúng", "chuẩn", "chính xác", "ghi đi", "lưu đi", "xác nhận", "được rồi", "lưu luôn", "có", "vâng", "ừ"]
                has_confirm_keyword = any(k in clean_text.lower() for k in confirm_words)
                if not has_confirm_keyword:
                    intent = UserIntent.PROVIDE_SLOT.value

            # 2. Xử lý theo từng loại ý định
            if intent == UserIntent.CANCEL.value:
                state.reset()
                state.state = DialogState.CANCELLED
                response_text = build_cancel_message()
                action_taken = "SESSION_CANCELLED"

            elif intent == UserIntent.CONFIRM.value:
                if state.state == DialogState.CONFIRMING and state.current_activity:
                    # Nếu câu xác nhận có kèm thông tin bổ sung (vd: "bón thúc rồi lưu luôn", "2 sào lưu đi")
                    if extracted_slots:
                        canon_slots = canonicalize_slots(extracted_slots, state.current_activity)
                        state.update_slots(canon_slots)
                        self._match_materials_in_slots(state)

                    # Người dùng đồng ý lưu -> Lưu vào MySQL
                    committed_act = state.current_activity
                    committed_slots = dict(state.accumulated_slots)

                    # Chống ghi đúp (Idempotency Guard / Race Condition Prevention):
                    # Khóa trạng thái ngay lập tức trước khi await I/O database để chặn double-click / duplicate requests
                    state.state = DialogState.COMMITTED
                    state.current_activity = None

                    save_res = await self._commit_to_database(state, committed_act, committed_slots)
                    if save_res.get("success"):
                        rec_id = save_res.get("record_id")
                        success_text, suggested_actions = build_success_message(committed_act, rec_id, slots=committed_slots)
                        state.last_saved_record = save_res
                        state.last_saved_activity = committed_act
                        state.last_saved_slots = committed_slots

                        # Kiểm tra xem có pending_activity không (từ câu kết hợp cả mua và phun/bón)
                        pending = state.meta.pop("pending_activity", None)
                        if pending and isinstance(pending, dict) and pending.get("activity"):
                            next_act = pending.get("activity")
                            next_slots = pending.get("slots") or {}
                            state.reset(keep_last_saved=True)
                            state.current_activity = next_act

                            # Kế thừa tên thuốc / tên phân từ committed_slots nếu chưa có
                            if committed_act == "purchase":
                                mat = committed_slots.get("material_name")
                                if next_act == "spray" and not next_slots.get("ten_thuoc_bvtv"):
                                    next_slots["ten_thuoc_bvtv"] = mat
                                elif next_act == "fertilize" and not next_slots.get("ten_phan_bon"):
                                    next_slots["ten_phan_bon"] = mat
                            elif committed_act in ("spray", "fertilize") and next_act == "purchase":
                                mat = committed_slots.get("ten_thuoc_bvtv") or committed_slots.get("ten_phan_bon")
                                if not next_slots.get("material_name"):
                                    next_slots["material_name"] = mat
                                    next_slots["material_type"] = "Thuốc BVTV" if committed_act == "spray" else "Phân bón"

                            if not next_slots.get("activity_date"):
                                next_slots["activity_date"] = committed_slots.get("activity_date") or date.today().isoformat()
                            if not next_slots.get("crop_name"):
                                next_slots["crop_name"] = state.crop_name or committed_slots.get("crop_name") or "Bưởi diễn"

                            next_slots = canonicalize_slots(next_slots, next_act)
                            state.accumulated_slots = next_slots
                            self._match_materials_in_slots(state)

                            missing = check_missing_fields(next_act, state.accumulated_slots)
                            state.missing_slots = missing
                            next_display = TASK_NAME_DISPLAY.get(next_act, next_act)

                            if missing:
                                state.state = DialogState.CLARIFYING
                                q = build_clarification_question(next_act, state.accumulated_slots, missing)
                                response_text = f"{success_text}\n\nTiếp tục ghi **{next_display}** nhé bác!\n{q}"
                                action_taken = "CHAINED_ACTIVITY_CLARIFYING"
                                suggested_actions = []
                            else:
                                state.state = DialogState.CONFIRMING
                                conf_msg = build_confirmation_message(next_act, state.accumulated_slots)
                                response_text = f"{success_text}\n\nTiếp tục ghi **{next_display}** luôn:\n{conf_msg}"
                                action_taken = "CHAINED_ACTIVITY_CONFIRMING"
                                suggested_actions = []
                        else:
                            state.reset(keep_last_saved=True)
                            state.state = DialogState.COMMITTED
                            response_text = success_text
                            action_taken = "DATA_COMMITTED"
                            if committed_act == "spray":
                                med_name = committed_slots.get("ten_thuoc_bvtv") or committed_slots.get("pesticide_name") or "thuốc BVTV"
                                state.bridge_activity = "waste"
                                state.bridge_slots = {
                                    "material_name": med_name,
                                    "waste_type": "Vỏ chai, bao bì thuốc BVTV",
                                    "activity_date": committed_slots.get("activity_date") or date.today().isoformat(),
                                    "treatment_date": committed_slots.get("activity_date") or date.today().isoformat(),
                                    "crop_name": committed_slots.get("crop_name") or state.crop_name or "Bưởi diễn",
                                }
                    else:
                        state.state = DialogState.CONFIRMING
                        state.current_activity = committed_act
                        state.accumulated_slots = committed_slots
                        response_text = f"❌ Gặp sự cố khi lưu vào sổ nhật ký: {save_res.get('error')}. Bác kiểm tra lại giúp em nhé!"
                else:
                    response_text = "Dạ hiện tại chưa có thông tin nhật ký nào hoàn tất để lưu ạ. Bác muốn ghi chép công việc gì thế bác?"

            elif intent == UserIntent.ASK_ADVICE.value:
                # Nông dân hỏi tư vấn kỹ thuật nông nghiệp
                rag_res = await rag_engine.ask_async(
                    question=clean_text,
                    crop_name=state.crop_name,
                )
                state.last_rag_result = rag_res.to_dict()
                state.bridge_activity = None
                state.bridge_slots = {}
                state.state = DialogState.IDLE

                response_text = rag_res.answer
                rag_sources = []
                suggested_actions = []
                rag_spoken_text = rag_res.spoken_text

                # RAG thuần trả lời kỹ thuật, không gợi ý ghi sổ (Phương án A)
                action_taken = "AGRICULTURAL_ADVICE_RAG_PROVIDED"

            else:
                # Cung cấp thông tin / Khởi tạo / Đính chính (START_ACTIVITY, PROVIDE_SLOT, CORRECTION)
                activity_driven_intents = {
                    UserIntent.START_ACTIVITY.value,
                    UserIntent.PROVIDE_SLOT.value,
                    UserIntent.CORRECTION.value,
                }

                if activity_type:
                    old_act = state.current_activity
                    new_act = activity_type

                    # 1. Chuyển đổi hoạt động giữa chừng (Mid-flow switch giữa purchase <-> spray / fertilize)
                    if old_act and old_act != new_act:
                        if old_act == "purchase" and new_act in ("spray", "fertilize"):
                            mat = state.accumulated_slots.get("material_name")
                            saved_date = state.accumulated_slots.get("activity_date") or state.accumulated_slots.get("purchase_date")
                            crop = state.accumulated_slots.get("crop_name")
                            state.accumulated_slots = {}
                            if mat:
                                if new_act == "spray":
                                    state.accumulated_slots["ten_thuoc_bvtv"] = mat
                                else:
                                    state.accumulated_slots["ten_phan_bon"] = mat
                            if saved_date:
                                state.accumulated_slots["activity_date"] = saved_date
                            if crop:
                                state.accumulated_slots["crop_name"] = crop

                        elif old_act in ("spray", "fertilize") and new_act == "purchase":
                            mat = state.accumulated_slots.get("ten_thuoc_bvtv") or state.accumulated_slots.get("ten_phan_bon")
                            saved_date = state.accumulated_slots.get("activity_date")
                            crop = state.accumulated_slots.get("crop_name")
                            state.accumulated_slots = {}
                            if mat:
                                state.accumulated_slots["material_name"] = mat
                                state.accumulated_slots["material_type"] = "Thuốc BVTV" if old_act == "spray" else "Phân bón"
                            if saved_date:
                                state.accumulated_slots["purchase_date"] = saved_date
                                state.accumulated_slots["activity_date"] = saved_date
                            if crop:
                                state.accumulated_slots["crop_name"] = crop
                        elif old_act == "spray" and new_act == "waste":
                            mat = state.accumulated_slots.get("ten_thuoc_bvtv")
                            saved_date = state.accumulated_slots.get("activity_date")
                            crop = state.accumulated_slots.get("crop_name")
                            state.accumulated_slots = {}
                            if mat:
                                state.accumulated_slots["material_name"] = mat
                                state.accumulated_slots["waste_type"] = "Vỏ chai, bao bì thuốc BVTV"
                            if saved_date:
                                state.accumulated_slots["treatment_date"] = saved_date
                                state.accumulated_slots["activity_date"] = saved_date
                            if crop:
                                state.accumulated_slots["crop_name"] = crop
                        else:
                            state.accumulated_slots = {}

                        state.current_activity = new_act

                    elif not old_act:
                        # 2. Hoạt động mới sau khi đã lưu phiên trước (Kế thừa từ last_saved_slots)
                        state.current_activity = new_act
                        if state.last_saved_activity == "purchase" and state.last_saved_slots.get("material_name"):
                            last_mat = state.last_saved_slots["material_name"]
                            if new_act == "spray" and not extracted_slots.get("ten_thuoc_bvtv"):
                                extracted_slots["ten_thuoc_bvtv"] = last_mat
                            elif new_act == "fertilize" and not extracted_slots.get("ten_phan_bon"):
                                extracted_slots["ten_phan_bon"] = last_mat
                        elif state.last_saved_activity in ("spray", "fertilize") and new_act == "purchase":
                            last_mat = state.last_saved_slots.get("ten_thuoc_bvtv") or state.last_saved_slots.get("ten_phan_bon")
                            if last_mat and not extracted_slots.get("material_name"):
                                extracted_slots["material_name"] = last_mat
                                if not extracted_slots.get("material_type"):
                                    extracted_slots["material_type"] = "Thuốc BVTV" if state.last_saved_activity == "spray" else "Phân bón"
                        elif state.last_saved_activity == "spray" and new_act == "waste":
                            last_mat = state.last_saved_slots.get("ten_thuoc_bvtv") or state.last_saved_slots.get("pesticide_name")
                            if last_mat and not extracted_slots.get("material_name"):
                                extracted_slots["material_name"] = last_mat
                            if not extracted_slots.get("waste_type"):
                                extracted_slots["waste_type"] = "Vỏ chai, bao bì thuốc BVTV"
                            if not extracted_slots.get("activity_date") and state.last_saved_slots.get("activity_date"):
                                extracted_slots["activity_date"] = state.last_saved_slots.get("activity_date")
                            if not extracted_slots.get("treatment_date") and state.last_saved_slots.get("activity_date"):
                                extracted_slots["treatment_date"] = state.last_saved_slots.get("activity_date")
                    else:
                        state.current_activity = new_act

                # Chỉ fallback về "general" khi user thật sự đang cung cấp thông tin hoạt động
                # Tránh set "general" khi user chỉ CHITCHAT hoặc UNKNOWN → sẽ làm ô nhiễm slot matching
                if not state.current_activity and intent in activity_driven_intents:
                    state.current_activity = "general"

                # Nếu vẫn chưa có activity (CHITCHAT / UNKNOWN thuần) → kiểm tra xem có phải câu hỏi tra cứu không
                if not state.current_activity:
                    if self._is_side_question(clean_text):
                        rag_res = await rag_engine.ask_async(
                            question=clean_text,
                            crop_name=state.crop_name,
                        )
                        state.last_rag_result = rag_res.to_dict()
                        state.bridge_activity = None
                        state.bridge_slots = {}
                        state.state = DialogState.IDLE
                        response_text = rag_res.answer
                        rag_sources = []
                        suggested_actions = []
                        rag_spoken_text = rag_res.spoken_text
                        # RAG thuần trả lời kỹ thuật, không gợi ý ghi sổ (Phương án A)
                        action_taken = "AGRICULTURAL_ADVICE_RAG_PROVIDED"
                    else:
                        response_text = await gemini_conv_helper.answer_agricultural_advice(
                            user_question=clean_text,
                            context_data={"crop_name": state.crop_name, "current_activity": None, "slots": {}},
                        )
                        action_taken = "CHITCHAT_OR_UNKNOWN_REPLIED"
                else:
                    # Quy chuẩn hóa tên các trường slot về khóa chính thống
                    extracted_slots = canonicalize_slots(extracted_slots, state.current_activity)
                    corrected_slots = canonicalize_slots(corrected_slots, state.current_activity)

                    # Ghi đè nếu có slot đính chính (CORRECTION)
                    if corrected_slots:
                        state.update_slots(corrected_slots)

                    # Cập nhật slot mới
                    if extracted_slots:
                        state.update_slots(extracted_slots)

                    state.accumulated_slots = canonicalize_slots(state.accumulated_slots, state.current_activity)

                    # Tự động gán ngày hôm nay nếu chưa có ngày
                    date_keys = ["activity_date", "purchase_date", "harvest_date", "sale_date", "treatment_date"]
                    has_date = any(state.accumulated_slots.get(k) for k in date_keys)
                    if not has_date:
                        state.accumulated_slots["activity_date"] = date.today().isoformat()
                    if not state.accumulated_slots.get("crop_name"):
                        state.accumulated_slots["crop_name"] = state.crop_name or "Bưởi diễn"

                    # Kiểm tra danh mục vật tư bằng MaterialMatcher
                    self._match_materials_in_slots(state)

                    # Kiểm tra hoạt chất cấm trong slot
                    if state.accumulated_slots.get("is_banned"):
                        b_info = state.accumulated_slots.get("banned_info") or {}
                        b_name = b_info.get("matched_name") or state.accumulated_slots.get("ten_thuoc_bvtv") or state.accumulated_slots.get("material_name") or "Thuốc"
                        hazard = b_info.get("hazard") or "Rất độc hại, gây tồn lưu nguy hiểm trong môi trường và nông sản."
                        state.state = DialogState.IDLE
                        state.current_activity = None
                        state.accumulated_slots = {}
                        response_text = (
                            f"⛔ **CẢNH BÁO PHÁP LÝ VIETGAP: PHÁT HIỆN THUỐC CẤM {b_name.upper()}!**\n\n"
                            f"Theo Thông tư 75/BNNMT & Luật BVTV 41/2013, hoạt chất này đã bị **CẤM SỬ DỤNG TẠI VIỆT NAM** ({hazard}).\n\n"
                            f"🚨 **Quy chuẩn VietGAP:** Hệ thống từ chối ghi nhận hoạt động này vào sổ nhật ký VietGAP.\n\n"
                            f"🌿 **Giải pháp an toàn:** Bác nên chuyển sang dùng các chế phẩm sinh học như vi sinh Bt, nấm đối kháng Trichoderma hoặc dầu khoáng nông nghiệp nhé!"
                        )
                        action_taken = "BANNED_PESTICIDE_BLOCKED"
                    else:
                        # Kiểm tra các trường bắt buộc còn thiếu bằng Template
                        missing = check_missing_fields(state.current_activity, state.accumulated_slots)
                        state.missing_slots = missing

                        if missing:
                            state.state = DialogState.CLARIFYING
                            response_text = build_clarification_question(
                                state.current_activity, state.accumulated_slots, missing
                            )
                            action_taken = "CLARIFYING_QUESTION_ASKED"
                        else:
                            state.state = DialogState.CONFIRMING
                            response_text = build_confirmation_message(
                                state.current_activity, state.accumulated_slots
                            )
                            if state.meta.get("pending_activity"):
                                p_act = state.meta["pending_activity"].get("activity")
                                p_disp = TASK_NAME_DISPLAY.get(p_act, p_act)
                                response_text += f"\n\n💡 *(Sau khi lưu phiếu này, em sẽ tự động ghi tiếp **{p_disp}** luôn nhé bác)*"
                            action_taken = "CONFIRMATION_REQUESTED"

        # Ghi nhận lượt trả lời của trợ lý vào lịch sử
        state.add_turn(role="assistant", text=response_text)

        # ---------------------------------------------------------------------
        # Sinh giọng nói TTS nếu được yêu cầu
        # ---------------------------------------------------------------------
        audio_result_path = None
        if enable_tts and response_text:
            tts_analysis = state.last_image_analysis if action_taken == "IMAGE_QUESTION_ANSWERED" else None
            if rag_spoken_text and action_taken in ("AGRICULTURAL_ADVICE_RAG_PROVIDED", "IMAGE_OR_ADVISORY_RAG_ANSWERED", "IN_FLOW_SIDE_QUESTION_CLARIFYING", "IN_FLOW_SIDE_QUESTION_CONFIRMING"):
                tts_text = rag_spoken_text
            else:
                tts_text = build_tts_spoken_text(
                    response_text,
                    analysis_dict=tts_analysis,
                    dialog_state=state.state.value,
                    activity_type=state.current_activity,
                    slots=state.accumulated_slots,
                )
            audio_result_path = self._generate_tts_audio(tts_text, tts_output_path)
            if audio_result_path:
                state.register_tts_audio(audio_result_path)

        return {
            "status": "success",
            "session_id": session_id,
            "dialog_state": state.state.value,
            "current_activity": state.current_activity,
            "action_taken": action_taken,
            "response_text": response_text,
            "accumulated_slots": state.accumulated_slots,
            "missing_slots": state.missing_slots,
            "analysis": None,
            "audio_file": audio_result_path,
            "suggested_actions": suggested_actions,
            "sources": [],
        }

    # =========================================================================
    # 2. TIẾP NHẬN ĐẦU VÀO HÌNH ẢNH (TỰ TRẢ LỜI & HỎI/LƯU NHẬT KÝ)
    # =========================================================================

    def _extract_slots_from_image(
        self,
        analysis_dict: Dict[str, Any],
        current_activity: Optional[str] = None,
        crop_name: Optional[str] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Bóc tách thông tin từ kết quả VLM thành các slot nhật ký theo chuẩn VietGAP."""
        cat = analysis_dict.get("category", "other")
        cat_to_act = {
            "pesticide": "spray",
            "fertilizer": "fertilize",
            "pest_disease": "observation",
            "invoice_receipt": "purchase",
            "field_crop": "general",
            "other": "general",
        }
        # Nếu đang trong một hoạt động cụ thể (VD: purchase, waste...), tôn trọng hoạt động đó
        if current_activity == "purchase":
            target_act = "purchase"
        elif current_activity == "waste":
            target_act = "waste"
        elif current_activity in ("spray", "fertilize", "harvest", "sale", "observation"):
            target_act = current_activity
        elif cat == "pesticide" or analysis_dict.get("pesticide"):
            target_act = "spray"
        elif cat == "fertilizer" or analysis_dict.get("fertilizer"):
            target_act = "fertilize"
        elif cat == "pest_disease" or analysis_dict.get("pest_disease"):
            target_act = "observation"
        elif cat == "invoice_receipt":
            target_act = "purchase"
        elif current_activity and current_activity != "general":
            target_act = current_activity
        else:
            target_act = cat_to_act.get(cat, current_activity or "general")

        resolved_crop = crop_name or "Bưởi diễn"
        slots: Dict[str, Any] = {
            "activity_date": date.today().isoformat(),
            "crop_name": resolved_crop,
        }

        # 1. Hoạt động MUA VẬT TƯ (purchase)
        if target_act == "purchase":
            slots["purchase_date"] = date.today().isoformat()
            if cat == "pesticide" or analysis_dict.get("pesticide"):
                p = analysis_dict.get("pesticide") or {}
                name = p.get("trade_name") or p.get("brand_name") or analysis_dict.get("title")
                db = p.get("db_match") or {}
                if db.get("matched") and db.get("name"):
                    name = db.get("name")
                    slots["pesticide_id"] = db.get("id")
                slots["material_name"] = name
                slots["material_type"] = "Thuốc BVTV"
                if p.get("manufacturer"):
                    slots["supplier_name"] = p.get("manufacturer")
            elif cat == "fertilizer" or analysis_dict.get("fertilizer"):
                f = analysis_dict.get("fertilizer") or {}
                name = f.get("trade_name") or f.get("fertilizer_name") or analysis_dict.get("title")
                db = f.get("db_match") or {}
                if db.get("matched") and db.get("name"):
                    name = db.get("name")
                    slots["fertilizer_id"] = db.get("id")
                slots["material_name"] = name
                slots["material_type"] = "Phân bón"
                if f.get("manufacturer"):
                    slots["supplier_name"] = f.get("manufacturer")
            elif cat == "invoice_receipt":
                slots["material_name"] = analysis_dict.get("title") or "Vật tư mua theo hóa đơn"
                if analysis_dict.get("summary"):
                    slots["note"] = analysis_dict.get("summary")
            else:
                slots["material_name"] = analysis_dict.get("title") or "Vật tư nông nghiệp"

        # 2. Hoạt động XỬ LÝ BAO BÌ (waste)
        elif target_act == "waste":
            if cat == "pesticide" or analysis_dict.get("pesticide"):
                p = analysis_dict.get("pesticide") or {}
                slots["material_name"] = p.get("trade_name") or p.get("brand_name") or analysis_dict.get("title")
            elif cat == "fertilizer" or analysis_dict.get("fertilizer"):
                f = analysis_dict.get("fertilizer") or {}
                slots["material_name"] = f.get("trade_name") or f.get("fertilizer_name") or analysis_dict.get("title")
            else:
                slots["material_name"] = analysis_dict.get("title") or "Vỏ bao bì"

        # 3. Hoạt động PHUN THUỐC, BÓN PHÂN HOẶC QUAN SÁT SÂU BỆNH
        elif cat == "pesticide" or analysis_dict.get("pesticide"):
            p = analysis_dict.get("pesticide") or {}
            slots["ten_thuoc_bvtv"] = p.get("trade_name") or p.get("brand_name") or analysis_dict.get("title")
            if p.get("target_pests"):
                tp = p.get("target_pests")
                slots["target_pest"] = ", ".join(tp) if isinstance(tp, list) else str(tp)
            dosage = p.get("dosage_and_usage") or p.get("dosage_instructions")
            if dosage:
                slots["huong_dan_pha"] = dosage
            if p.get("isolation_days") is not None:
                slots["thoi_gian_cach_ly"] = p.get("isolation_days")
            db = p.get("db_match") or {}
            if db.get("matched"):
                slots["pesticide_id"] = db.get("id")
                if db.get("name"):
                    slots["ten_thuoc_bvtv"] = db.get("name")
        elif cat == "fertilizer" or analysis_dict.get("fertilizer"):
            f = analysis_dict.get("fertilizer") or {}
            slots["ten_phan_bon"] = f.get("trade_name") or f.get("fertilizer_name") or analysis_dict.get("title")
            dosage = f.get("dosage_and_usage") or f.get("usage_instructions")
            if dosage:
                slots["huong_dan_bon"] = dosage
            db = f.get("db_match") or {}
            if db.get("matched"):
                slots["fertilizer_id"] = db.get("id")
                if db.get("name"):
                    slots["ten_phan_bon"] = db.get("name")
        elif cat == "pest_disease" or analysis_dict.get("pest_disease"):
            pd = analysis_dict.get("pest_disease") or {}
            slots["sinh_vat_gay_hai"] = pd.get("pest_name") or analysis_dict.get("title")
            if pd.get("severity"):
                slots["severity"] = pd.get("severity")
            if pd.get("crop_name"):
                slots["crop_name"] = pd.get("crop_name")
            controls = pd.get("cultural_controls") or pd.get("biological_controls") or "Cắt tỉa cành bệnh, thu gom tiêu hủy sạch sẽ"
            slots["bien_phap_xu_ly"] = controls
            if target_act == "spray":
                slots["target_pest"] = pd.get("pest_name") or analysis_dict.get("title")
                matched_meds = pd.get("matched_farm_pesticides") or []
                if matched_meds:
                    first_med = matched_meds[0]
                    slots["ten_thuoc_bvtv"] = first_med.get("name")
                    slots["pesticide_id"] = first_med.get("pesticide_id")
                    if first_med.get("dosage"):
                        slots["huong_dan_pha"] = first_med.get("dosage")
                    if first_med.get("min_isolation_days") is not None:
                        slots["thoi_gian_cach_ly"] = first_med.get("min_isolation_days")
            if pd.get("symptoms") or pd.get("recommended_actives"):
                rec_act = pd.get("recommended_actives", [])
                rec_act_str = ", ".join(rec_act) if isinstance(rec_act, list) else str(rec_act)
                slots["note"] = f"Triệu chứng: {pd.get('symptoms', '')}; Hoạt chất: {rec_act_str}"
        elif cat == "invoice_receipt":
            slots["material_name"] = analysis_dict.get("title") or "Vật tư mua theo hóa đơn"
            if analysis_dict.get("summary"):
                slots["note"] = analysis_dict.get("summary")

        return target_act, slots

    async def handle_image(
        self,
        session_id: str,
        image_path: str,
        farmer_id: Optional[int] = 1,
        season_id: Optional[int] = None,
        enable_tts: bool = False,
    ) -> Dict[str, Any]:
        """
        Tiếp nhận ảnh từ người dùng:
        1. Phân tích qua VLM Engine (Gemini 3.1 Flash Lite) và luôn trả lời thông tin kỹ thuật trong ảnh.
        2. Nếu đang ghi nhật ký: Tự động cập nhật dữ liệu từ ảnh vào nhật ký đang ghi -> tiếp tục hỏi thiếu hoặc xin lưu.
        3. Nếu chưa ghi nhật ký: Trả lời thông tin trong ảnh + hỏi có muốn ghi nhật ký dựa trên ảnh không -> tiếp tục hội thoại.
        """
        if not os.path.exists(image_path):
            return {
                "status": "error",
                "message": f"Không tìm thấy file ảnh: {image_path}",
                "response_text": "File ảnh không tồn tại. Bác kiểm tra lại đường dẫn giúp em nhé!",
                "dialog_state": DialogState.IDLE.value,
            }

        state = self.store.get_or_create(session_id, farmer_id=farmer_id, season_id=season_id)

        # Cập nhật tên giống cây từ DB nếu chưa có (chỉ query 1 lần, sau đó cache trong state)
        if not state.crop_name or state.crop_name == "Cà gai leo":
            try:
                from services.database import get_db_connection, get_crop_name_for_farmer
                conn = get_db_connection()
                fetched = get_crop_name_for_farmer(conn, user_id=farmer_id, season_id=season_id)
                conn.close()
                if fetched:
                    state.crop_name = fetched
            except Exception as _e_crop:
                logger.debug("Không lấy được crop_name từ DB: %s", _e_crop)

        state.last_image_path = image_path
        state.add_turn(role="user", text=f"[Gửi ảnh: {os.path.basename(image_path)}]", image_path=image_path)

        # Phân tích ảnh qua VLM Engine — truyền tên giống cây từ phiên hội thoại
        try:
            analysis = await vlm_engine.analyze_image(
                image_path,
                crop_name=state.crop_name or "Bưởi",
            )
            analysis_dict = analysis.to_dict()
            state.last_image_analysis = analysis_dict
        except Exception as e:
            logger.exception("Lỗi khi phân tích ảnh qua VLM: %s", e)
            return {
                "status": "error",
                "message": str(e),
                "response_text": "Không thể phân tích ảnh này. Bác vui lòng gửi lại ảnh rõ nét hơn nhé!",
                "dialog_state": state.state.value,
            }

        # Kiểm tra xem có ĐANG TRONG TIẾN TRÌNH GHI NHẬT KÝ không
        is_in_diary = state.state in (DialogState.COLLECTING, DialogState.CLARIFYING, DialogState.CONFIRMING) and bool(state.current_activity)

        # Luôn tạo nội dung trả lời thông tin trong ảnh (kèm nút ghi nhật ký theo yêu cầu)
        advisory_info = build_advisory_response(analysis_dict, prompt_for_diary=(not is_in_diary))
        lead_info, sections = build_lead_and_sections(analysis_dict, prompt_for_diary=(not is_in_diary))
        analysis_dict["lead_info"] = lead_info
        analysis_dict["sections"] = sections
        state.last_image_analysis = analysis_dict

        # Kiểm tra chốt chặn an toàn: Thuốc bị CẤM trong ảnh
        p_info = analysis_dict.get("pesticide") or {}
        is_banned_image = (lead_info.get("type") == "pesticide_banned") or bool(p_info.get("is_banned"))

        response_text = ""
        action_taken = ""
        clarifying_q = ""

        if is_banned_image:
            # Thuốc cấm: TUYỆT ĐỐI KHÔNG ghi nhật ký, xóa slot và chuyển về ADVISORY để cảnh báo
            state.accumulated_slots = {}
            state.missing_slots = []
            state.current_activity = None
            state.state = DialogState.ADVISORY
            response_text = advisory_info
            action_taken = "BANNED_PESTICIDE_IMAGE_ALERT"

        elif is_in_diary:
            # -----------------------------------------------------------------
            # HƯỚNG 1: ĐANG GHI NHẬT KÝ -> Đắp dữ liệu ảnh vào nhật ký & hỏi tiếp hoặc lưu
            # -----------------------------------------------------------------
            target_act, img_slots = self._extract_slots_from_image(analysis_dict, state.current_activity, crop_name=state.crop_name)
            state.current_activity = target_act
            img_slots = canonicalize_slots(img_slots, state.current_activity)
            state.update_slots(img_slots)
            self._match_materials_in_slots(state)
            state.accumulated_slots = canonicalize_slots(state.accumulated_slots, state.current_activity)

            missing = check_missing_fields(state.current_activity, state.accumulated_slots)
            state.missing_slots = missing

            act_display = TASK_NAME_DISPLAY.get(state.current_activity, state.current_activity)
            diary_status_header = (
                f"\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📝 **Em đã tự động cập nhật thông tin từ ảnh vào nhật ký {act_display} đang ghi.**\n"
            )

            if missing:
                state.state = DialogState.CLARIFYING
                clarifying_q = build_clarification_question(state.current_activity, state.accumulated_slots, missing)
                response_text = f"{advisory_info}{diary_status_header}{clarifying_q}"
                action_taken = "DIARY_UPDATED_FROM_IMAGE_CLARIFYING"
            else:
                state.state = DialogState.CONFIRMING
                confirm_msg = build_confirmation_message(state.current_activity, state.accumulated_slots)
                response_text = f"{advisory_info}{diary_status_header}{confirm_msg}"
                action_taken = "DIARY_UPDATED_FROM_IMAGE_CONFIRMING"

        else:
            # -----------------------------------------------------------------
            # HƯỚNG 2: CHƯA GHI NHẬT KÝ -> Trả lời thông tin trong ảnh + kèm nút ghi nhật ký
            # -----------------------------------------------------------------
            # Chuẩn bị sẵn hoạt động và slots từ ảnh để nếu người dùng bấm nút ghi sổ thì tiếp tục ngay
            suggested_act, pre_slots = self._extract_slots_from_image(analysis_dict, crop_name=state.crop_name)
            pre_slots = canonicalize_slots(pre_slots, suggested_act)
            state.bridge_activity = suggested_act
            state.bridge_slots = pre_slots
            state.current_activity = suggested_act
            state.accumulated_slots = pre_slots
            self._match_materials_in_slots(state)
            state.accumulated_slots = canonicalize_slots(state.accumulated_slots, state.current_activity)

            # Chuyển sang trạng thái ADVISORY (Chờ người dùng hỏi thêm hoặc bấm ghi sổ)
            state.state = DialogState.ADVISORY
            response_text = advisory_info
            action_taken = "ADVISORY_WITH_DIARY_PROMPT"

        state.add_turn(role="assistant", text=response_text)

        audio_file = None
        if enable_tts and response_text:
            tts_text = build_tts_spoken_text(
                response_text,
                analysis_dict=analysis_dict,
                dialog_state="ADVISORY" if not is_in_diary else state.state.value,
                activity_type=state.current_activity,
                slots=state.accumulated_slots,
            )
            audio_file = self._generate_tts_audio(tts_text)
            if audio_file:
                state.register_tts_audio(audio_file)

        return {
            "status": "success",
            "session_id": session_id,
            "dialog_state": state.state.value,
            "current_activity": state.current_activity,
            "category": analysis_dict.get("category"),
            "title": analysis_dict.get("title"),
            "action_taken": action_taken,
            "response_text": response_text,
            "clarifying_question": clarifying_q if is_in_diary and missing else None,
            "lead_info": lead_info,
            "sections": sections,
            "analysis": analysis_dict,
            "accumulated_slots": state.accumulated_slots,
            "missing_slots": state.missing_slots,
            "audio_file": audio_file,
        }

    # =========================================================================
    # 3. TIẾP NHẬN ĐẦU VÀO ÂM THANH (AUDIO PROCESSING)
    # =========================================================================

    async def handle_audio(
        self,
        session_id: str,
        audio_path: str,
        farmer_id: Optional[int] = 1,
        season_id: Optional[int] = None,
        enable_tts: bool = False,
    ) -> Dict[str, Any]:
        """
        Tiếp nhận file ghi âm từ người dùng:
        1. Resample về 16kHz mono PCM16 bằng FFmpeg.
        2. Chạy Offline STT bằng Sherpa-ONNX Zipformer tiếng Việt.
        3. Đẩy văn bản nhận diện được vào handle_text().
        """
        if not os.path.exists(audio_path):
            return {
                "status": "error",
                "message": f"Không tìm thấy file audio: {audio_path}",
                "response_text": "Không tìm thấy file ghi âm. Bác thử lại giúp em nhé!",
                "dialog_state": DialogState.IDLE.value,
            }

        os.makedirs(config.TMP_DIR, exist_ok=True)
        converted_wav = os.path.join(config.TMP_DIR, f"conv_{int(time.time()*1000)}_16k.wav")

        try:
            await resample_to_16k_mono_pcm16(audio_path, converted_wav)
            transcript = await stt_engine.transcribe(converted_wav)
            if not transcript or not transcript.strip():
                return {
                    "status": "warning",
                    "message": "Không nhận diện được giọng nói trong bản ghi.",
                    "response_text": "Em không nghe rõ bác nói gì trong bản ghi. Bác nói lại giúp em nhé!",
                    "dialog_state": DialogState.IDLE.value,
                }

            logger.info("🎙️ [STT Thành công]: '%s'", transcript)
            res = await self.handle_text(
                session_id=session_id,
                user_text=transcript,
                farmer_id=farmer_id,
                season_id=season_id,
                enable_tts=enable_tts,
            )
            res["stt_transcript"] = transcript
            return res

        except Exception as e:
            logger.exception("Lỗi khi xử lý audio trong ConversationManager: %s", e)
            return {
                "status": "error",
                "message": str(e),
                "response_text": "Có lỗi khi chuyển giọng nói thành văn bản. Bác thử lại nhé!",
                "dialog_state": DialogState.IDLE.value,
            }
        finally:
            if os.path.exists(converted_wav):
                try:
                    os.remove(converted_wav)
                except OSError:
                    pass

    # =========================================================================
    # 4. KHỚP DANH MỤC & LƯU CSDL
    # =========================================================================

    def _match_materials_in_slots(self, state: ConversationState) -> None:
        """Đối chiếu tên thuốc hoặc phân bón trong slot với danh mục CSDL."""
        slots = state.accumulated_slots
        act = state.current_activity or ""

        # Khớp thuốc BVTV
        if act == "spray" and slots.get("ten_thuoc_bvtv"):
            try:
                from services.rag.safety_checker import safety_checker
                med_val = str(slots["ten_thuoc_bvtv"])
                banned_res = safety_checker.check_banned_substance(med_val)
                if banned_res.is_banned:
                    slots["is_banned"] = True
                    slots["banned_info"] = banned_res.to_dict()
                else:
                    match_res = material_matcher.fuzzy_match(med_val, category_hint="pesticide")
                    if match_res:
                        if match_res.is_banned:
                            slots["is_banned"] = True
                            slots["banned_info"] = match_res.banned_info
                        elif match_res.matched:
                            slots["ten_thuoc_bvtv"] = match_res.canonical_name or slots["ten_thuoc_bvtv"]
                            slots["pesticide_id"] = match_res.item_id
                            if match_res.min_isolation_days and not slots.get("thoi_gian_cach_ly"):
                                slots["thoi_gian_cach_ly"] = match_res.min_isolation_days
            except Exception as e:
                logger.debug("Khớp danh mục thuốc không thành công: %s", e)

        # Khớp phân bón
        elif act == "fertilize" and slots.get("ten_phan_bon"):
            try:
                match_res = material_matcher.fuzzy_match(str(slots["ten_phan_bon"]), category_hint="fertilizer")
                if match_res and match_res.matched:
                    slots["ten_phan_bon"] = match_res.canonical_name or slots["ten_phan_bon"]
                    slots["fertilizer_id"] = match_res.item_id
            except Exception as e:
                logger.debug("Khớp danh mục phân bón không thành công: %s", e)

        # Khớp vật tư khi mua (Thuốc BVTV hoặc Phân bón)
        elif act == "purchase" and slots.get("material_name"):
            try:
                from conversation.templates import classify_material_category
                from services.rag.safety_checker import safety_checker
                mat_val = str(slots["material_name"])
                cat = classify_material_category(mat_val, slots.get("material_type"))
                if cat == "pesticide":
                    slots["material_type"] = "Thuốc BVTV"
                    banned_res = safety_checker.check_banned_substance(mat_val)
                    if banned_res.is_banned:
                        slots["is_banned"] = True
                        slots["banned_info"] = banned_res.to_dict()
                    else:
                        match_res = material_matcher.fuzzy_match(mat_val, category_hint="pesticide")
                        if match_res:
                            if match_res.is_banned:
                                slots["is_banned"] = True
                                slots["banned_info"] = match_res.banned_info
                            elif match_res.matched:
                                slots["material_name"] = match_res.canonical_name or slots["material_name"]
                                slots["pesticide_id"] = match_res.item_id
                elif cat == "fertilizer":
                    slots["material_type"] = "Phân bón"
                    match_res = material_matcher.fuzzy_match(mat_val, category_hint="fertilizer")
                    if match_res and match_res.matched:
                        slots["material_name"] = match_res.canonical_name or slots["material_name"]
                        slots["fertilizer_id"] = match_res.item_id
            except Exception as e:
                logger.debug("Khớp danh mục vật tư mua không thành công: %s", e)

    async def _commit_to_database(
        self,
        state: ConversationState,
        act_type: Optional[str] = None,
        slots: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Ghi dữ liệu hoàn chỉnh của phiên xuống CSDL MySQL vietgap_diary."""
        target_act = act_type or state.current_activity or "general"
        target_slots = dict(slots) if slots is not None else dict(state.accumulated_slots)
        if not target_slots.get("crop_name"):
            target_slots["crop_name"] = state.crop_name or "Bưởi diễn"
        if not target_slots.get("activity_date"):
            target_slots["activity_date"] = date.today().isoformat()

        # Chốt chặn kiểm tra an toàn VietGAP: Tuyệt đối không lưu hoạt chất cấm vào MySQL
        from services.rag.safety_checker import safety_checker
        for cand in [target_slots.get("ten_thuoc_bvtv"), target_slots.get("material_name"), target_slots.get("active_ingredient")]:
            if cand:
                banned_chk = safety_checker.check_banned_substance(str(cand))
                if banned_chk.is_banned:
                    logger.warning("⛔ Chặn ghi nhận hoạt chất cấm vào CSDL MySQL: %s (%s)", cand, banned_chk.matched_name)
                    return {
                        "status": "error",
                        "error": f"Bị từ chối lưu vào CSDL: Thuốc/hoạt chất '{cand}' đã bị cấm sử dụng theo Thông tư 75/BNNMT & Luật BVTV 41/2013 (Vi phạm tiêu chuẩn VietGAP).",
                    }

        dialogue_history = [t.to_dict() for t in state.history]
        raw_user_text = " ".join([t.text for t in state.history if t.role == "user"])

        payload = {
            "tables": {target_act: target_slots},
            "task_types": [target_act],
            "raw_text": raw_user_text,
            "photo_url": state.last_image_path or "",
            "dialogue_history": dialogue_history,
            "session_id": state.session_id,
            "crop_name": target_slots.get("crop_name"),
            "total_turns": len(dialogue_history),
        }

        # Lưu bản sao JSON cục bộ phục vụ làm tập dữ liệu huấn luyện AI sau này
        try:
            dataset_dir = os.path.join(config.TMP_DIR, "conversation_dataset")
            os.makedirs(dataset_dir, exist_ok=True)
            export_file = os.path.join(dataset_dir, f"conv_{state.session_id}_{int(time.time())}.json")
            with open(export_file, "w", encoding="utf-8") as f:
                json.dump({
                    "session_id": state.session_id,
                    "farmer_id": state.farmer_id,
                    "activity_type": target_act,
                    "crop_name": target_slots.get("crop_name"),
                    "activity_date": target_slots.get("activity_date"),
                    "accumulated_slots": target_slots,
                    "dialogue_history": dialogue_history,
                    "raw_user_text": raw_user_text,
                    "created_at": time.time(),
                }, f, ensure_ascii=False, indent=2)
            logger.info("💾 Đã lưu dữ liệu hội thoại huấn luyện vào: %s", export_file)
        except Exception as _e_dump:
            logger.debug("Không thể ghi file dataset hội thoại cục bộ: %s", _e_dump)

        try:
            loop = asyncio.get_running_loop()
            db_res = await loop.run_in_executor(
                None,
                lambda: save_diary_to_mysql(
                    farmer_id=state.farmer_id,
                    data=payload,
                    raw_text=payload["raw_text"],
                    photo_url=payload["photo_url"],
                    time_process=1.0,
                    llm_meta={
                        "engine": "gemini_conversation",
                        "model": gemini_conv_helper.model,
                        "raw_response": {
                            "accumulated_slots": target_slots,
                            "dialogue_history": dialogue_history,
                            "image_analysis": state.last_image_analysis,
                        },
                        "raw_llm_output": {
                            "accumulated_slots": target_slots,
                            "dialogue_history": dialogue_history,
                            "image_analysis": state.last_image_analysis,
                        },
                    },
                ),
            )
            return {"success": True, "record_id": db_res.get("id"), "db": db_res}
        except Exception as e:
            logger.exception("Lỗi khi ghi nhật ký vào MySQL: %s", e)
            return {"success": False, "error": str(e)}

    @classmethod
    def cleanup_old_tts_files(cls, max_age_seconds: int = 600, keep_latest: int = 5) -> int:
        """
        Dọn dẹp các file tts_*.wav mồ côi (không thuộc phiên nào đang hoạt động và đã cũ).
        Tuyệt đối không xóa file của các phiên đang hoạt động!
        """
        deleted_count = 0
        try:
            tmp_dir = getattr(config, "TMP_DIR", "/root/voice_to_data/tmp")
            if not os.path.exists(tmp_dir):
                return 0

            # Lấy tất cả các file TTS thuộc các phiên đang hoạt động
            active_filenames = set()
            try:
                from conversation.state import session_store
                active_filenames = session_store.get_all_active_tts_files()
            except Exception as _e_store:
                logger.debug("Không thể lấy active_tts_files: %s", _e_store)

            tts_files = []
            now = time.time()
            for fname in os.listdir(tmp_dir):
                if fname.startswith("tts_") and fname.endswith(".wav"):
                    # Tuyệt đối giữ lại các file thuộc phiên đang chạy
                    if fname in active_filenames:
                        continue
                    fpath = os.path.join(tmp_dir, fname)
                    if os.path.isfile(fpath):
                        mtime = os.path.getmtime(fpath)
                        tts_files.append((fpath, mtime))

            # Sắp xếp từ mới nhất đến cũ nhất
            tts_files.sort(key=lambda x: x[1], reverse=True)

            # Bỏ qua keep_latest file mới nhất
            candidates = tts_files[keep_latest:]
            for fpath, mtime in candidates:
                if (now - mtime) > max_age_seconds:
                    try:
                        os.remove(fpath)
                        deleted_count += 1
                    except Exception as err:
                        logger.debug("Không thể xóa file tts cũ %s: %s", fpath, err)
        except Exception as e:
            logger.warning("Lỗi dọn dẹp file tts: %s", e)
        return deleted_count

    @staticmethod
    def delete_tts_file(file_name_or_url: str) -> bool:
        """Xóa an toàn một file tts theo tên hoặc đường dẫn /tmp/..."""
        if not file_name_or_url:
            return False
        try:
            fname = os.path.basename(file_name_or_url.split("?")[0])
            # Chỉ cho phép xóa file tts_*.wav để đảm bảo an toàn
            if not (fname.startswith("tts_") and fname.endswith(".wav")):
                return False
            tmp_dir = getattr(config, "TMP_DIR", "/root/voice_to_data/tmp")
            fpath = os.path.join(tmp_dir, fname)
            if os.path.isfile(fpath):
                os.remove(fpath)
                logger.info("🗑️ Đã xóa file TTS: %s", fname)
                return True
        except Exception as e:
            logger.debug("Không thể xóa file tts %s: %s", file_name_or_url, e)
        return False

    def _generate_tts_audio(self, text: str, output_path: Optional[str] = None) -> Optional[str]:
        """Sinh âm thanh giọng nói TTS từ văn bản phản hồi."""
        try:
            os.makedirs(config.TMP_DIR, exist_ok=True)
            out_file = output_path or os.path.join(config.TMP_DIR, f"tts_{int(time.time()*1000)}.wav")
            audio_res = tts_engine.synthesize_sync(text, speed=1.0)
            audio_res.save(out_file)
            return out_file
        except Exception as e:
            logger.warning("Không thể sinh TTS audio: %s", e)
            return None

    def cleanup_farmer_sessions(self, farmer_id: int) -> int:
        """Dọn dẹp tất cả các phiên và file TTS thuộc về một nông dân."""
        try:
            return self.store.remove_by_farmer(int(farmer_id))
        except Exception as e:
            logger.debug("Lỗi khi dọn dẹp phiên của farmer %s: %s", farmer_id, e)
            return 0


# Singleton instance toàn cục
conversation_manager = ConversationManager()
