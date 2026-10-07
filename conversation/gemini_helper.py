"""
Hỗ trợ gọi Google Gemini API cho module Conversation State.
Bao gồm phân loại ý định (Intent Classification), trích xuất và gộp slot đa lượt (Multi-turn Slot Extraction),
và tư vấn kỹ thuật nông nghiệp chuyên sâu.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import ssl
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import certifi

import config

logger = logging.getLogger("conversation.gemini")

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
DEFAULT_MODEL = getattr(config, "GEMINI_MODEL", "gemini-3.5-flash-lite")


class GeminiConversationHelper:
    """Wrapper gọi Gemini cho trạng thái hội thoại đa lượt."""

    def __init__(self, api_key: Optional[str] = None, model: str = DEFAULT_MODEL):
        self.api_key = api_key or config.GEMINI_API_KEY
        self.model = model
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=_SSL_CONTEXT)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _call_gemini_api(
        self,
        system_instruction: str,
        user_content: str,
        temperature: float = 0.0,
        response_schema: Optional[dict] = None,
        timeout_sec: int = 15,
    ) -> Dict[str, Any]:
        """Gọi trực tiếp Google Generative Language API với JSON payload."""
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY chưa được cấu hình trong .env!")

        session = await self._ensure_session()
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )

        gen_config: Dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": 2048,
        }
        if response_schema:
            gen_config["response_mime_type"] = "application/json"
            gen_config["response_schema"] = response_schema

        payload = {
            "system_instruction": {"parts": [{"text": system_instruction}]},
            "contents": [{"parts": [{"text": user_content}]}],
            "generationConfig": gen_config,
        }

        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        headers = {"Content-Type": "application/json"}

        async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Lỗi Gemini API ({resp.status}): {body[:300]}")

            resp_json = await resp.json()
            candidates = resp_json.get("candidates", [])
            if not candidates:
                raise RuntimeError("Gemini không trả về candidate nào.")

            parts = candidates[0].get("content", {}).get("parts", [])
            if not parts:
                raise RuntimeError("Gemini không có content parts.")

            raw_text = parts[0].get("text", "").strip()

            # Parse JSON
            try:
                return json.loads(raw_text)
            except json.JSONDecodeError:
                clean = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.MULTILINE)
                clean = re.sub(r"\s*```$", "", clean, flags=re.MULTILINE).strip()
                try:
                    return json.loads(clean)
                except json.JSONDecodeError:
                    m = re.search(r"\{.*\}", raw_text, re.DOTALL)
                    if m:
                        return json.loads(m.group(0))
                    raise RuntimeError(f"Không thể parse JSON từ Gemini: {raw_text[:200]}")

    async def classify_and_extract_slots(
        self,
        user_text: str,
        current_activity: Optional[str] = None,
        accumulated_slots: Optional[Dict[str, Any]] = None,
        dialogue_state: str = "IDLE",
        history_turns: Optional[List[Dict[str, Any]]] = None,
        crop_name: Optional[str] = None,
        last_saved_activity: Optional[str] = None,
        last_saved_slots: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Phân loại ý định (Intent) và trích xuất/gộp các trường thông tin (Slot Merging)
        dựa trên ngữ cảnh hội thoại hiện tại.
        """
        accumulated_slots = accumulated_slots or {}
        history_turns = history_turns or []
        active_crop = crop_name or accumulated_slots.get("crop_name") or "Bưởi diễn"

        # Heuristic phát hiện nhanh xác nhận / hủy bỏ / lựa chọn ảnh trước khi gọi LLM
        clean_text_lower = user_text.strip().lower()

        # Phát hiện lựa chọn hướng khi đang ở IMAGE_CHOICE
        if dialogue_state == "IMAGE_CHOICE":
            if any(k in clean_text_lower for k in ["1", "nhật ký", "ghi vào", "ghi sổ", "biểu mẫu", "lưu sổ"]):
                return {"intent": "CHOOSE_DIARY", "confidence": 1.0, "extracted_slots": {}}
            if any(k in clean_text_lower for k in ["2", "tư vấn", "tra cứu", "thông tin", "hỏi", "xem thuốc", "sâu gì"]):
                return {"intent": "CHOOSE_ADVICE", "confidence": 1.0, "extracted_slots": {}}

        # Phát hiện xác nhận khi đang ở CONFIRMING
        if dialogue_state == "CONFIRMING":
            confirm_words = ["lưu", "ok", "đồng ý", "đúng", "chuẩn", "chính xác", "ghi đi", "lưu đi", "xác nhận", "được rồi"]
            has_confirm = any(k in clean_text_lower for k in confirm_words)
            has_cancel = any(w in clean_text_lower for w in ["không", "đừng", "chưa", "hủy"])
            if has_confirm and not has_cancel:
                # Nếu chỉ là câu xác nhận thuần túy (không chứa số lượng hay từ khóa bổ sung chi tiết), trả về nhanh
                has_specs = bool(
                    re.search(r"\d+", clean_text_lower)
                    or any(w in clean_text_lower for w in ["sào", "gốc", "cây", "thúc", "lót", "tưới", "rải", "bình", "máy bay", "drone", "đại lý", "giá", "nghìn", "triệu", "đắt", "rẻ", "hóa đơn", "nắng", "mưa", "râm", "lô"])
                )
                if not has_specs:
                    return {"intent": "CONFIRM", "confidence": 1.0, "extracted_slots": {}}

        # Phát hiện đồng ý ghi nhật ký khi đang ở ADVISORY (sau khi gửi ảnh)
        if dialogue_state == "ADVISORY":
            agree_words = ["có", "ghi", "lưu", "đồng ý", "ok", "được", "tiếp tục", "vâng", "ừ", "ghi đi", "lưu đi", "nhật ký", "ghi vào"]
            if any(k in clean_text_lower for k in agree_words) and not any(w in clean_text_lower for w in ["không", "đừng", "chưa", "hủy"]):
                return {"intent": "PROVIDE_SLOT", "activity_type": current_activity, "confidence": 1.0, "extracted_slots": {}}

        # Phát hiện hủy bỏ
        cancel_phrases = ["hủy bỏ", "hủy đi", "hủy phiếu", "đừng lưu", "không lưu", "thôi không ghi", "để sau", "bỏ qua", "reset phiên"]
        is_cancel = clean_text_lower.strip() in ["hủy", "thôi", "thoát", "reset"] or any(p in clean_text_lower for p in cancel_phrases)
        if is_cancel and not any(p in clean_text_lower for p in ["bỏ vào", "bỏ trong", "bỏ thùng", "bỏ bể", "bỏ hố", "bỏ rác"]):
            return {"intent": "CANCEL", "confidence": 1.0, "extracted_slots": {}}

        system_instruction = f"""Bạn là trợ lý NLU chuyên sâu cho nhật ký nông nghiệp VietGAP.
BỐI CẢNH TRANG TRẠI:
- Cây trồng canh tác chủ lực của trang trại: {active_crop} theo tiêu chuẩn VietGAP.
- Mọi hoạt động canh tác (phun thuốc, bón phân, thu hoạch, theo dõi sâu bệnh) đều mặc định diễn ra trên cây {active_crop}.
- Khi người dùng không nhắc rõ tên cây, trường "crop_name" luôn mặc định là "{active_crop}".

Nhiệm vụ: Phân tích phát ngôn mới nhất của người dùng trong ngữ cảnh hội thoại đang diễn ra.

CÁC Ý ĐỊNH (intent):
- "START_ACTIVITY": Bắt đầu nói về một công việc canh tác mới (phun thuốc, bón phân, mua vật tư, thu hoạch, bán hàng, dọn cỏ...).
- "PROVIDE_SLOT": Trả lời câu hỏi gợi ý của trợ lý hoặc bổ sung thông tin còn thiếu (ví dụ: "trị rầy nâu", "2 lít", "14 ngày", "5 bao", "đại lý Quang Minh").
- "CORRECTION": Muốn đính chính/sửa lại thông tin đã nói trước đó (ví dụ: "nhầm, 5 bao chứ không phải 3 bao", "hôm nay chứ không phải hôm qua").
- "CONFIRM": Đồng ý lưu dữ liệu vào sổ nhật ký.
- "CANCEL": Hủy bỏ không ghi chép nữa.
- "ASK_ADVICE": Đang hỏi tư vấn kỹ thuật (thuốc này trị bệnh gì, liều lượng thế nào, sâu này là sâu gì).
- "CHITCHAT": Chào hỏi hoặc giao tiếp bình thường.

CÁC LOẠI HOẠT ĐỘNG (activity_type):
- "spray": Phun thuốc BVTV
- "fertilize": Bón phân
- "purchase": Mua / nhập vật tư
- "harvest": Thu hoạch nông sản
- "sale": Tiêu thụ / xuất bán nông sản
- "waste": Xử lý vỏ bao bì
- "observation": Theo dõi sinh vật gây hại
- "general": Hoạt động canh tác chung (làm đất, làm cỏ, tưới nước...)

QUY TẮC CHUYỂN TIẾP THÔNG MINH GIỮA MUA VẬT TƯ VÀ PHUN THUỐC / BÓN PHÂN (VÀ NGƯỢC LẠI):
1. Khi đang hoặc vừa nói về MUA VẬT TƯ (thuốc BVTV hoặc phân bón), nếu người dùng nói "mang đi phun luôn", "ghi phun thuốc này luôn", "phun luôn 2 bình", "đi phun", "bón luôn", "mang đi bón luôn 3 bao"...:
   -> Ý định là "START_ACTIVITY", activity_type là "spray" (nếu là thuốc) hoặc "fertilize" (nếu là phân bón).
   -> Nếu có nói liều lượng/số bình (vd: "2 bình 16 lít", "3 bao"), trích xuất vào "nong_do_va_luong_dung" hoặc "luong_phan_bon".
2. NGƯỢC LẠI: Khi đang hoặc vừa nói về PHUN THUỐC hoặc BÓN PHÂN, nếu người dùng nói "ghi mua thuốc này luôn", "thuốc này tôi mua 2 chai ở đại lý Tuấn", "ghi mua phân này", "mua 10 bao ở đại lý"...:
   -> Ý định là "START_ACTIVITY", activity_type là "purchase".
   -> Trích xuất quantity, unit, supplier_name nếu người dùng có nhắc.
3. CÂU KẾT HỢP CẢ HAI HOẠT ĐỘNG (ÁP DỤNG ĐẦY ĐỦ CHO CẢ PHÂN BÓN VÀ THUỐC BVTV):
   a) MUA PHÂN BÓN + ĐEM ĐI BÓN LUÔN:
      Ví dụ: "Tôi mua 5 bao phân NPK Đầu Trâu ở đại lý Thành Đạt rồi đem đi bón luôn 2 bao cho bưởi"
      -> "activity_type": "purchase", "extracted_slots": {{"material_name": "NPK Đầu Trâu", "material_type": "Phân bón", "quantity": 5, "unit": "bao", "supplier_name": "đại lý Thành Đạt"}}
      -> "secondary_activity": "fertilize", "secondary_slots": {{"ten_phan_bon": "NPK Đầu Trâu", "luong_phan_bon": "2 bao"}}
   b) BÓN PHÂN + GHI MUA PHÂN BÓN VÀO SỔ:
      Ví dụ: "Hôm nay tôi bón 3 bao phân NPK Đầu Trâu cho vườn bưởi, ghi mua 10 bao ở đại lý Thành Đạt vào sổ luôn"
      -> "activity_type": "fertilize", "extracted_slots": {{"ten_phan_bon": "NPK Đầu Trâu", "luong_phan_bon": "3 bao"}}
      -> "secondary_activity": "purchase", "secondary_slots": {{"material_name": "NPK Đầu Trâu", "material_type": "Phân bón", "quantity": 10, "unit": "bao", "supplier_name": "đại lý Thành Đạt"}}
   c) MUA THUỐC BVTV + ĐEM ĐI PHUN LUÔN:
      Ví dụ: "Tôi mua 2 chai thuốc Radiant 60SC ở đại lý Tuấn rồi mang đi phun luôn 2 bình trị bọ trĩ"
      -> "activity_type": "purchase", "extracted_slots": {{"material_name": "Radiant 60SC", "material_type": "Thuốc BVTV", "quantity": 2, "unit": "chai", "supplier_name": "đại lý Tuấn"}}
      -> "secondary_activity": "spray", "secondary_slots": {{"ten_thuoc_bvtv": "Radiant 60SC", "nong_do_va_luong_dung": "2 bình", "target_pest": "bọ trĩ"}}
   d) PHUN THUỐC + GHI MUA THUỐC VÀO SỔ:
      Ví dụ: "Sáng nay tôi phun 2 bình Radiant 60SC trị bọ trĩ, ghi mua 5 chai ở đại lý Tuấn vào sổ luôn"
      -> "activity_type": "spray", "extracted_slots": {{"ten_thuoc_bvtv": "Radiant 60SC", "nong_do_va_luong_dung": "2 bình", "target_pest": "bọ trĩ"}}
      -> "secondary_activity": "purchase", "secondary_slots": {{"material_name": "Radiant 60SC", "material_type": "Thuốc BVTV", "quantity": 5, "unit": "chai", "supplier_name": "đại lý Tuấn"}}
   e) PHUN THUỐC XONG + GHI XỬ LÝ VỎ BAO BÌ (spray -> waste):
      Khi vừa ghi xong hoặc đang nói về PHUN THUỐC BVTV, nếu người dùng nói "ghi xử lý vỏ bao bì", "thu gom vỏ chai thuốc", "vứt vỏ bao vào bể chứa", "xử lý bao bì thuốc này luôn":
      -> "intent": "START_ACTIVITY", "activity_type": "waste"
      -> Kế thừa tên thuốc vừa phun vào "material_name" (vd: "Radiant 60SC", "Anvil"), loại bao bì vào "waste_type" ("Vỏ chai, bao bì thuốc BVTV").
      -> Nếu người dùng nói phương pháp (vd: "bỏ vào hố chứa", "thu gom vào bể chứa chuyên dụng"), trích xuất vào "treatment_method".

4. KHI ĐANG Ở TRẠNG THÁI CONFIRMING (XÁC NHẬN):
   - CHÚ Ý ĐẶC BIỆT: CHỈ gán "intent": "CONFIRM" khi người dùng THỰC SỰ CÓ TỪ NGỮ ĐỒNG Ý / LƯU / XÁC NHẬN (như "lưu", "ok", "đồng ý", "lưu đi", "lưu luôn", "chuẩn rồi", "được rồi", "ghi vào sổ").
   - Nếu người dùng CHỈ CUNG CẤP THÊM THÔNG TIN (kể cả nói diện tích, dụng cụ, thời tiết, phương pháp bón, đại lý...) mà KHÔNG có từ ngữ đồng ý lưu:
     -> BẮT BUỘC gán "intent": "PROVIDE_SLOT", TUYỆT ĐỐI KHÔNG gán "CONFIRM".
   - Nếu người dùng VỪA CUNG CẤP THÔNG TIN VỪA NÓI LƯU (vd: "bón thúc rồi lưu luôn", "2 sào lưu đi", "đại lý Tuấn lưu luôn"):
     -> "intent": "CONFIRM", đồng thời trích xuất các trường thông tin đó vào "extracted_slots".

- LƯU Ý VỀ CÁC TRƯỜNG TÙY CHỌN:
  + Các trường weather_condition (thời tiết), growth_stage (giai đoạn sinh trưởng), dien_tich (diện tích/số gốc) KHÔNG CẦN hỏi thêm nếu nông dân không nhắc đến (chỉ trích xuất nếu họ tự nguyện nói ra).
  + Đối với hoạt động Mua vật tư (purchase): Nông dân có thể được gợi ý hoặc yêu cầu chụp ảnh hóa đơn / chứng từ hoặc chụp ảnh bao bì vật tư để lấy thông tin tự động.

QUY TẮC KHÓA (KEYS) CHUẨN CỦA SLOTS THEO TỪNG HOẠT ĐỘNG:
- spray (Phun thuốc BVTV):
  + ten_thuoc_bvtv: tên thuốc nguyên văn (vd: "Nano Ecotech", "Top 1", "Anvil 5SC", "Radiant 60SC")
  + target_pest: đối tượng sâu bệnh hại (vd: "rầy nâu", "sâu vẽ bùa", "nấm hồng")
  + nong_do_va_luong_dung: liều lượng pha hoặc nồng độ (vd: "2 bình 16 lít", "1 chai/ha")
  + thoi_gian_cach_ly: số ngày cách ly (vd: 7, 14 hoặc "14 ngày")
  + dien_tich: diện tích hoặc số gốc (vd: "2 sào", "50 gốc")
  + phuong_thuc_phun: dụng cụ hoặc phương thức phun (vd: "bình đeo vai", "máy bay drone", "máy nén")
  + thoi_tiet: thời tiết khi phun (vd: "trời râm", "lặng gió", "nắng ráo")
  + crop_name: tên cây trồng (mặc định: "{active_crop}")
- fertilize (Bón phân):
  + ten_phan_bon: tên phân bón (vd: "NPK", "phân chuồng", "Đầu trâu")
  + luong_phan_bon: lượng bón kèm đơn vị (vd: "5 bao", "3 tạ/ha", "50kg")
  + phuong_phap_bon: bón lót, bón thúc, tưới gốc, rải gốc...
  + dien_tich: diện tích hoặc số cây (vd: "3 sào", "100 cây")
  + thoi_diem_bon: giai đoạn sinh trưởng của cây (vd: "nuôi trái", "sau tỉa cành")
  + crop_name: tên cây trồng (mặc định: "{active_crop}")
- purchase (Mua vật tư):
  + material_name: tên giống, phân, thuốc...
  + material_type: loại vật tư ("Thuốc BVTV" hoặc "Phân bón" hoặc "Hạt giống")
  + quantity: số lượng
  + unit: đơn vị tính (bao, kg, chai, gói, cây...)
  + supplier_name: nơi mua, đại lý, công ty
  + unit_price: đơn giá / giá mua (vd: "350 nghìn/bao")
  + invoice_no: số hóa đơn / chứng từ
- harvest (Thu hoạch):
  + crop_name: tên cây trồng (mặc định: "{active_crop}")
  + san_luong_thu_hoach: sản lượng (vd: "5 tấn", "2 tạ")
  + dia_diem_cach_so_che: lô thu hoạch hoặc nơi/cách sơ chế
  + chat_luong: loại 1, loại 2...
- sale (Xuất bán):
  + crop_name: tên cây trồng (mặc định: "{active_crop}")
  + khoi_luong_tieu_thu: khối lượng bán
  + co_so_thu_mua: người mua, công ty, thương lái
  + unit_price: đơn giá bán
- observation (Theo dõi sâu bệnh):
  + sinh_vat_gay_hai: tên sâu bệnh (vd: "loét sẹo", "sâu vẽ bùa")
  + crop_name: tên cây trồng (mặc định: "{active_crop}")
  + severity: mức độ (nhẹ, trung bình, nặng)
  + bien_phap_xu_ly: biện pháp phòng ngừa/canh tác
- waste (Xử lý bao bì):
  + material_name: tên vỏ bao bì
  + treatment_method: cách xử lý
  + storage_location: nơi lưu trữ
- general (Chung):
  + cong_viec: nội dung công việc
  + dien_tich: khu vực / diện tích làm việc

Trả về ĐÚNG cấu trúc JSON, không kèm bất kỳ giải thích nào:
{{
  "intent": "START_ACTIVITY" | "PROVIDE_SLOT" | "CORRECTION" | "CONFIRM" | "CANCEL" | "ASK_ADVICE" | "CHITCHAT",
  "activity_type": "spray" | "fertilize" | "purchase" | "harvest" | "sale" | "waste" | "observation" | "general" | null,
  "extracted_slots": {{ ... }},
  "corrected_slots": {{ ... }},
  "secondary_activity": "spray" | "fertilize" | "purchase" | null,
  "secondary_slots": {{ ... }},
  "confidence": 0.95
}}"""

        # Tổng hợp ngữ cảnh đưa vào user_content
        context_str = f"TRẠNG THÁI HIỆN TẠI: {dialogue_state}\n"
        if current_activity:
            context_str += f"HOẠT ĐỘNG ĐANG XỬ LÝ: {current_activity}\n"
        if accumulated_slots:
            context_str += f"CÁC THÔNG TIN ĐÃ THU THẬP: {json.dumps(accumulated_slots, ensure_ascii=False)}\n"
        if last_saved_activity and last_saved_slots:
            context_str += f"HOẠT ĐỘNG VỪA LƯU GẦN NHẤT: {last_saved_activity} với dữ liệu: {json.dumps(last_saved_slots, ensure_ascii=False)}\n"
        if history_turns:
            recent_turns = history_turns[-3:]
            context_str += "LỊCH SỬ 3 LƯỢT GẦN NHẤT:\n"
            for t in recent_turns:
                context_str += f"- {t.get('role')}: {t.get('text')}\n"

        user_content = f"{context_str}\nCÂU NÓI MỚI CỦA NÔNG DÂN: \"{user_text}\""

        try:
            res = await self._call_gemini_api(
                system_instruction=system_instruction,
                user_content=user_content,
                temperature=0.0,
                timeout_sec=12,
            )
            return res
        except Exception as e:
            logger.warning("Gọi Gemini NLU gặp sự cố: %s. Kích hoạt fallback heuristic.", e)
            return self._heuristic_fallback(user_text, current_activity, dialogue_state, last_saved_activity, last_saved_slots)

    def _heuristic_fallback(
        self,
        user_text: str,
        current_activity: Optional[str],
        dialogue_state: str,
        last_saved_activity: Optional[str] = None,
        last_saved_slots: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Quy tắc heuristic dự phòng khi không có mạng hoặc Gemini timeout."""
        clean = user_text.lower()
        extracted: Dict[str, Any] = {}
        secondary_act = None
        secondary_slots: Dict[str, Any] = {}

        # Heuristic phát hiện chuyển đổi hoặc câu kết hợp
        is_spray_word = any(k in clean for k in ["phun", "xịt", "trị sâu", "trị bệnh", "thuốc bvtv"])
        is_fertilize_word = any(k in clean for k in ["bón", "rải phân", "phân bón", "tưới phân"])
        is_purchase_word = any(k in clean for k in ["mua", "nhập", "đại lý", "cửa hàng"])

        detected_activity = current_activity

        # Kiểm tra chuyển tiếp từ purchase -> spray/fertilize
        ref_act = current_activity or last_saved_activity
        if ref_act == "purchase":
            if any(k in clean for k in ["phun luôn", "mang đi phun", "đi phun", "xịt luôn", "phun thuốc"]):
                detected_activity = "spray"
            elif any(k in clean for k in ["bón luôn", "mang đi bón", "đi bón", "rải luôn", "bón phân"]):
                detected_activity = "fertilize"
        elif ref_act in ("spray", "fertilize"):
            if any(k in clean for k in ["ghi mua", "mua luôn", "mua thuốc", "mua phân", "mua ở", "mua đại lý"]):
                detected_activity = "purchase"
            elif ref_act == "spray" and any(k in clean for k in ["xử lý bao bì", "vỏ bao", "bao bì", "vỏ chai", "thu gom vỏ"]):
                detected_activity = "waste"

        # Nếu không có ref_act hoặc câu mới hoàn toàn
        if not detected_activity:
            if is_purchase_word and is_fertilize_word:
                # Kiểm tra từ nào xuất hiện trước
                pos_p = clean.find("mua")
                pos_f = clean.find("bón") if "bón" in clean else clean.find("rải")
                if pos_p < pos_f:
                    detected_activity = "purchase"
                    secondary_act = "fertilize"
                else:
                    detected_activity = "fertilize"
                    secondary_act = "purchase"
            elif is_purchase_word and is_spray_word:
                pos_p = clean.find("mua")
                pos_s = clean.find("phun") if "phun" in clean else clean.find("xịt")
                if pos_p < pos_s:
                    detected_activity = "purchase"
                    secondary_act = "spray"
                else:
                    detected_activity = "spray"
                    secondary_act = "purchase"
            elif is_purchase_word:
                detected_activity = "purchase"
            elif is_spray_word:
                detected_activity = "spray"
            elif is_fertilize_word:
                detected_activity = "fertilize"
            elif any(k in clean for k in ["thu hoạch", "hái", "cắt"]):
                detected_activity = "harvest"
            elif any(k in clean for k in ["bán", "xuất bán", "thương lái"]):
                detected_activity = "sale"
            elif any(k in clean for k in ["xử lý bao bì", "vỏ bao", "bao bì", "vỏ chai", "thu gom vỏ", "tiêu hủy bao"]):
                detected_activity = "waste"

        # Phát hiện ngày tháng
        if "hôm nay" in clean:
            extracted["activity_date"] = time.strftime("%Y-%m-%d")
        elif "hôm qua" in clean:
            from datetime import date, timedelta
            extracted["activity_date"] = (date.today() - timedelta(days=1)).isoformat()

        # Phát hiện cách ly
        m_iso = re.search(r"(\d+)\s*(ngày|hôm)\s*(cách\s*ly)?", clean)
        if m_iso:
            extracted["thoi_gian_cach_ly"] = int(m_iso.group(1))

        # Phát hiện liều lượng / khối lượng (hỗ trợ cả 2 lượng trong câu kết hợp)
        all_doses = list(re.finditer(r"(\d+[\.,]?\d*)\s*(lít|chai|gói|tạ|tấn|bao|ký|kg|bình)", clean))
        if all_doses:
            first_dose = all_doses[0]
            first_qty_str = f"{first_dose.group(1)} {first_dose.group(2)}"
            if detected_activity == "spray":
                extracted["nong_do_va_luong_dung"] = first_qty_str
            elif detected_activity == "fertilize":
                extracted["luong_phan_bon"] = first_qty_str
            elif detected_activity == "purchase":
                extracted["quantity"] = first_dose.group(1)
                extracted["unit"] = first_dose.group(2)

            if len(all_doses) > 1 and secondary_act:
                second_dose = all_doses[1]
                second_qty_str = f"{second_dose.group(1)} {second_dose.group(2)}"
                if secondary_act == "spray":
                    secondary_slots["nong_do_va_luong_dung"] = second_qty_str
                elif secondary_act == "fertilize":
                    secondary_slots["luong_phan_bon"] = second_qty_str
                elif secondary_act == "purchase":
                    secondary_slots["quantity"] = second_dose.group(1)
                    secondary_slots["unit"] = second_dose.group(2)

        # Phát hiện tên phân bón
        fert_keywords = [
            ("npk 16-16-8", "NPK 16-16-8"),
            ("npk đầu trâu", "NPK Đầu Trâu"),
            ("đầu trâu", "NPK Đầu Trâu"),
            ("npk", "NPK"),
            ("phân chuồng", "Phân chuồng"),
            ("hữu cơ vi sinh", "Phân hữu cơ vi sinh"),
            ("hữu cơ", "Phân hữu cơ"),
            ("đạm", "Phân đạm"),
            ("lân", "Phân lân"),
            ("kali", "Phân kali"),
            ("dap", "DAP"),
            ("urê", "Phân Urê"),
            ("ure", "Phân Urê"),
        ]
        found_fert = None
        for fk, f_name in fert_keywords:
            if fk in clean:
                found_fert = f_name
                break

        if found_fert:
            if detected_activity == "fertilize":
                extracted["ten_phan_bon"] = found_fert
            elif detected_activity == "purchase":
                extracted["material_name"] = found_fert
                extracted["material_type"] = "Phân bón"
            if secondary_act == "fertilize":
                secondary_slots["ten_phan_bon"] = found_fert
            elif secondary_act == "purchase":
                secondary_slots["material_name"] = found_fert
                secondary_slots["material_type"] = "Phân bón"

        # Phát hiện tên thuốc BVTV
        pesticide_keywords = [
            ("radiant 60sc", "Radiant 60SC"),
            ("radiant", "Radiant 60SC"),
            ("antracol 70wp", "Antracol 70WP"),
            ("antracol", "Antracol 70WP"),
            ("anvil 5sc", "Anvil 5SC"),
            ("anvil", "Anvil 5SC"),
            ("nano ecotech", "Nano Ecotech"),
            ("top 1", "Top 1"),
        ]
        found_pest = None
        for pk, p_name in pesticide_keywords:
            if pk in clean:
                found_pest = p_name
                break

        if found_pest:
            if detected_activity == "spray":
                extracted["ten_thuoc_bvtv"] = found_pest
            elif detected_activity == "purchase":
                extracted["material_name"] = found_pest
                extracted["material_type"] = "Thuốc BVTV"
            if secondary_act == "spray":
                secondary_slots["ten_thuoc_bvtv"] = found_pest
            elif secondary_act == "purchase":
                secondary_slots["material_name"] = found_pest
                secondary_slots["material_type"] = "Thuốc BVTV"

        # Phát hiện nơi mua / đại lý
        m_sup = re.search(r"(?:ở|tại)\s+(đại lý\s+[\w\s]+|cửa hàng\s+[\w\s]+|hợp tác xã\s+[\w\s]+)", clean)
        if m_sup:
            sup_name = m_sup.group(1).title().strip()
            if detected_activity == "purchase":
                extracted["supplier_name"] = sup_name
            elif secondary_act == "purchase":
                secondary_slots["supplier_name"] = sup_name

        # Phát hiện sâu bệnh
        for pest in ["rầy nâu", "sâu ăn lá", "sâu đục thân", "bọ trĩ", "nấm bệnh", "thán thư", "nhện đỏ", "sâu vẽ bùa", "loét sẹo"]:
            if pest in clean:
                if detected_activity == "spray":
                    extracted["target_pest"] = pest
                elif secondary_act == "spray":
                    secondary_slots["target_pest"] = pest
                break

        # Phát hiện phương pháp bón
        for meth in ["bón lót", "bón thúc", "tưới gốc", "rải gốc", "bón vãi", "phun qua lá"]:
            if meth in clean:
                extracted["phuong_phap_bon"] = meth
                break

        # Phát hiện phương thức / dụng cụ phun
        for p_meth in ["máy bay", "drone", "bình đeo vai", "máy nén", "bình điện", "bình bơm", "phun sương"]:
            if p_meth in clean:
                extracted["phuong_thuc_phun"] = p_meth
                break

        # Phát hiện diện tích hoặc số gốc / số cây
        m_dt = re.search(r"(\d+(?:[\.,]\d+)?\s*(?:sào|ha|hecta|gốc|cây|m2|mét vuông))", clean)
        if m_dt:
            extracted["dien_tich"] = m_dt.group(1).strip()

        # Phát hiện thời tiết
        for w_val in ["trời nắng", "trời râm", "râm mát", "lặng gió", "mát mẻ", "nắng ráo", "trời mát"]:
            if w_val in clean:
                extracted["thoi_tiet"] = w_val
                break

        # Phát hiện đơn giá / tiền
        m_pr = re.search(r"(\d+(?:[\.,]\d+)?\s*(?:nghìn|ngàn|triệu|k|đồng|vnd)(?:\s*(?:một|1)?\s*(?:bao|ký|kg|chai|gói))?)", clean)
        if m_pr and "unit_price" not in extracted:
            extracted["unit_price"] = m_pr.group(1).strip()

        # Phát hiện số hóa đơn
        m_inv = re.search(r"(?:hóa đơn|chứng từ|số)\s*(?:số)?\s*([a-zA-Z0-9\-_/]+)", clean)
        if m_inv:
            extracted["invoice_no"] = m_inv.group(1).strip()

        # Phát hiện thông tin xử lý vỏ bao bì
        if detected_activity == "waste":
            if not extracted.get("material_name"):
                if last_saved_slots and (last_saved_slots.get("ten_thuoc_bvtv") or last_saved_slots.get("pesticide_name")):
                    extracted["material_name"] = last_saved_slots.get("ten_thuoc_bvtv") or last_saved_slots.get("pesticide_name")
            if not extracted.get("waste_type"):
                extracted["waste_type"] = "Vỏ chai, bao bì thuốc BVTV"
            for w_meth in ["bể chứa", "hố chứa", "thu gom", "tiêu hủy", "chôn lấp", "bể thu gom"]:
                if w_meth in clean:
                    extracted["treatment_method"] = "Thu gom vào bể chứa chuyên dụng"
                    break

        confirm_words = ["lưu", "ok", "đồng ý", "đúng", "chuẩn", "chính xác", "ghi đi", "lưu đi", "xác nhận", "được rồi"]
        if dialogue_state == "CONFIRMING":
            if any(k in clean for k in confirm_words) and not any(w in clean for w in ["không", "đừng", "chưa", "hủy"]):
                intent = "CONFIRM"
            else:
                intent = "PROVIDE_SLOT"
        elif current_activity and dialogue_state in ("CLARIFYING", "COLLECTING") and (detected_activity == current_activity or not detected_activity):
            intent = "PROVIDE_SLOT"
        else:
            intent = "START_ACTIVITY"

        res = {
            "intent": intent,
            "activity_type": detected_activity or current_activity or "general",
            "extracted_slots": extracted,
            "corrected_slots": {},
            "confidence": 0.6,
        }
        if secondary_act:
            res["secondary_activity"] = secondary_act
            res["secondary_slots"] = secondary_slots
        return res

    async def answer_agricultural_advice(
        self,
        user_question: str,
        context_data: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Trả lời câu hỏi tư vấn nông nghiệp theo chuẩn VietGAP cho giống cây trồng trong CSDL."""
        crop_name = None
        if context_data and isinstance(context_data, dict):
            crop_name = context_data.get("crop_name")
            if not crop_name and context_data.get("image_analysis"):
                p = context_data["image_analysis"].get("pest_disease") or {}
                crop_name = p.get("crop_name")
        if not crop_name:
            try:
                from services.database import get_db_connection, get_crop_name_for_farmer
                conn = get_db_connection()
                crop_name = get_crop_name_for_farmer(conn)
                conn.close()
            except Exception:
                crop_name = "Bưởi"

        # 1. Truy xuất tài liệu kỹ thuật chuẩn VietGAP từ RAG Engine
        rag_context = ""
        try:
            from services.rag.rag_engine import rag_engine
            rag_res = await rag_engine.ask_async(
                question=user_question,
                top_k=3,
                crop_name=crop_name,
                temperature=0.2,
            )
            if rag_res:
                if rag_res.is_safety_guardrail:
                    return rag_res.answer
                if rag_res.sources:
                    rag_snippets = [f"[{s.get('rank', i+1)}] {s.get('section_title')}" for i, s in enumerate(rag_res.sources[:3])]
                    rag_context = "\nTÀI LIỆU KỸ THUẬT VIETGAP TRA CỨU ĐƯỢC:\n" + "\n".join(rag_snippets)
        except Exception as _e_rag:
            logger.debug("Không thể tra cứu RAG trong answer_agricultural_advice: %s", _e_rag)

        system_instruction = f"""Bạn là trợ lý Nông Ký AI kiêm kỹ sư nông nghiệp VietGAP chuyên nghiệp.
BỐI CẢNH CANH TÁC:
- Trang trại của bác nông dân đang trồng giống cây: {crop_name} theo tiêu chuẩn VietGAP.
- BẮT BUỘC: Mọi câu trả lời, tư vấn sâu bệnh hại, thuốc BVTV, phân bón, kỹ thuật chăm sóc, tỉa cành, tưới nước CHỈ TẬP TRUNG NÓI VỀ CÂY {crop_name.upper()}.
- QUY TẮC PHÁT NGÔN CHO GIỌNG ĐỌC (TTS):
  + Trả lời cực kỳ ngắn gọn, súc tích (chỉ từ 2 đến 3 câu).
  + Không dùng các ký hiệu trang trí như gạch đầu dòng, dấu sao markdown, bảng biểu, danh sách dài dòng.
  + Nói thẳng vào bệnh/triệu chứng trên cây {crop_name}, biện pháp xử lý an toàn VietGAP (ưu tiên sinh học, vi sinh) và thuốc được phép dùng nếu cần.
  + Xưng hô thân mật, lễ phép: 'em' và 'bác'."""

        user_content = f"CÂU HỎI CỦA BÁC NÔNG DÂN: \"{user_question}\""
        if rag_context:
            user_content = f"{rag_context}\n\n" + user_content
        if context_data:
            user_content = f"THÔNG TIN BỐI CẢNH:\n{json.dumps(context_data, ensure_ascii=False)}\n\n" + user_content

        try:
            session = await self._ensure_session()
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
            payload = {
                "system_instruction": {"parts": [{"text": system_instruction}]},
                "contents": [{"parts": [{"text": user_content}]}],
                "generationConfig": {"temperature": 0.3, "max_output_tokens": 1024},
            }
            async with session.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts:
                            return parts[0].get("text", "").strip()
        except Exception as e:
            logger.warning("Lỗi khi xin ý kiến tư vấn từ Gemini: %s", e)

        return (
            "Dạ về vấn đề này, bác chú ý thăm vườn thường xuyên, ưu tiên sử dụng các chế phẩm sinh học "
            "và tuân thủ đúng nguyên tắc 4 đúng (đúng thuốc, đúng liều lượng, đúng lúc, đúng cách) theo chuẩn VietGAP nhé bác!"
        )


# Singleton instance
gemini_conv_helper = GeminiConversationHelper()
