"""
VLM Engine (Vision-Language Model Engine - services/vlm_engine.py)
------------------------------------------------------------------
Dịch vụ phân tích hình ảnh đa phương thức (VLM) chuyên sâu cho nông nghiệp VietGAP,
sử dụng mô hình Google AI Gemini 3.1 Flash Lite (mặc định: gemini-3.1-flash-lite).

Tính năng chính:
1. Tự động nhận diện và phân loại ảnh thực địa:
   - Sâu bệnh hại (Pest & Disease Diagnosis): chẩn đoán triệu chứng, tác nhân, mức độ hại,
     biện pháp sinh học/canh tác và khuyến nghị hoạt chất điều trị.
   - Thuốc BVTV (Pesticide Detection): đọc nhãn thương phẩm, hoạt chất, nồng độ,
     dạng thuốc (WG, SC, EC...), đối tượng phòng trừ, liều lượng, PHI (ngày cách ly).
   - Phân bón (Fertilizer Detection): đọc tên phân bón, loại phân (hữu cơ, NPK, vi lượng...),
     thành phần dinh dưỡng, liều lượng cách bón.
   - Cây trồng / Hiện trường / Hóa đơn chứng từ mua bán vật tư.
2. Tích hợp liên kết CSDL VietGAP và Fuzzy Match:
   - Tự động đối soát tên thuốc / phân bón với danh mục trong MySQL (qua MaterialMatcher).
   - Tự động tra cứu thuốc BVTV đang có sẵn trong CSDL trang trại phù hợp để trị loại sâu bệnh phát hiện.
3. Hỗ trợ đa dạng nguồn ảnh: file path, raw bytes, base64 data, URL ảnh nội bộ.
"""

import asyncio
import base64
from dataclasses import dataclass, field
import io
import json
import logging
import os
import re
import ssl
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple, Union

import aiohttp
from PIL import Image

import config
from services.vietgap.material_matcher import material_matcher, MaterialMatchResult

logger = logging.getLogger(__name__)

# Cấu hình SSL
try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()

# Model mặc định
DEFAULT_VLM_MODEL = getattr(config, "GEMINI_VLM_MODEL", "gemini-3.1-flash-lite")
FALLBACK_MODELS = [
    DEFAULT_VLM_MODEL,
    getattr(config, "GEMINI_MODEL", "gemini-3.5-flash-lite"),
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
]


class VLMEngineError(Exception):
    """Lỗi xử lý hình ảnh qua VLM Engine."""


# ==============================================================================
# DATA CLASSES / SCHEMAS
# ==============================================================================

@dataclass
class PestDiseaseDiagnosis:
    """Kết quả chẩn đoán sâu bệnh hại từ hình ảnh."""
    pest_name: str
    scientific_name: Optional[str] = None
    pest_type: Optional[str] = None          # Sâu hại, Nấm bệnh, Vi khuẩn, Nhện hại, Virus...
    crop_name: Optional[str] = None          # Cây trồng bị hại
    affected_part: Optional[str] = None      # Lá, Quả, Thân, Rễ, Hoa...
    symptoms: Optional[str] = None          # Mô tả triệu chứng tổn thương
    severity: Optional[str] = None           # Nhẹ, Trung bình, Nặng
    causes_and_conditions: Optional[str] = None  # Điều kiện phát sinh
    cultural_controls: Optional[str] = None  # Biện pháp canh tác
    biological_controls: Optional[str] = None # Biện pháp sinh học
    recommended_actives: List[str] = field(default_factory=list) # Hoạt chất khuyến nghị
    matched_farm_pesticides: List[Dict[str, Any]] = field(default_factory=list) # Thuốc có sẵn trong CSDL trang trại

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pest_name": self.pest_name,
            "scientific_name": self.scientific_name,
            "pest_type": self.pest_type,
            "crop_name": self.crop_name,
            "affected_part": self.affected_part,
            "symptoms": self.symptoms,
            "severity": self.severity,
            "causes_and_conditions": self.causes_and_conditions,
            "cultural_controls": self.cultural_controls,
            "biological_controls": self.biological_controls,
            "recommended_actives": self.recommended_actives,
            "matched_farm_pesticides": self.matched_farm_pesticides,
        }


@dataclass
class PesticideDetection:
    """Kết quả nhận diện thuốc bảo vệ thực vật từ hình ảnh bao bì."""
    trade_name: str
    active_ingredient: Optional[str] = None
    formulation: Optional[str] = None        # WG, SC, EC, WP, SL, OD...
    toxicity_group: Optional[str] = None     # Nhóm độc GHS/WHO
    target_pests: List[str] = field(default_factory=list) # Sâu bệnh phòng trừ
    registered_crops: List[str] = field(default_factory=list) # Cây trồng đăng ký
    dosage_and_usage: Optional[str] = None   # Liều lượng và cách dùng
    isolation_days: Optional[int] = None     # PHI (ngày)
    manufacturer: Optional[str] = None      # Nhà sản xuất / Đăng ký
    safety_precautions: Optional[str] = None # Lưu ý an toàn
    db_match: Optional[Dict[str, Any]] = None # Đối soát với CSDL MySQL trang trại
    is_banned: bool = False                  # Cờ cảnh báo hoạt chất cấm theo Thông tư 75
    banned_info: Optional[Dict[str, Any]] = None # Chi tiết quy định cấm

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_name": self.trade_name,
            "active_ingredient": self.active_ingredient,
            "formulation": self.formulation,
            "toxicity_group": self.toxicity_group,
            "target_pests": self.target_pests,
            "registered_crops": self.registered_crops,
            "dosage_and_usage": self.dosage_and_usage,
            "isolation_days": self.isolation_days,
            "manufacturer": self.manufacturer,
            "safety_precautions": self.safety_precautions,
            "db_match": self.db_match,
            "is_banned": self.is_banned,
            "banned_info": self.banned_info,
        }



@dataclass
class FertilizerDetection:
    """Kết quả nhận diện phân bón từ hình ảnh bao bì."""
    trade_name: str
    fertilizer_type: Optional[str] = None    # Hữu cơ, Vô cơ, NPK, Vi lượng, Vi sinh...
    composition_npk: Optional[str] = None    # Tỷ lệ NPK, Hữu cơ %, Vi lượng...
    benefits: List[str] = field(default_factory=list) # Công dụng chính
    dosage_and_usage: Optional[str] = None   # Liều lượng và cách bón
    suitable_crops: List[str] = field(default_factory=list) # Cây trồng phù hợp
    manufacturer: Optional[str] = None      # Nhà sản xuất / phân phối
    db_match: Optional[Dict[str, Any]] = None # Đối soát với CSDL MySQL trang trại

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_name": self.trade_name,
            "fertilizer_type": self.fertilizer_type,
            "composition_npk": self.composition_npk,
            "benefits": self.benefits,
            "dosage_and_usage": self.dosage_and_usage,
            "suitable_crops": self.suitable_crops,
            "manufacturer": self.manufacturer,
            "db_match": self.db_match,
        }


@dataclass
class InvoiceItem:
    """Một dòng mặt hàng phân bón / thuốc BVTV / vật tư trên hóa đơn mua bán."""
    material_name: str
    material_type: str = "Vật tư"            # 'Thuốc BVTV', 'Phân bón', 'Giống', 'Vật tư'
    active_ingredient: Optional[str] = None
    quantity: float = 1.0
    unit: str = "đơn vị"                     # chai, gói, bao, kg, lít...
    unit_price: Optional[Any] = None         # Đơn giá (VD: 120000 hoặc '120.000 đ')
    amount: Optional[Any] = None             # Thành tiền dòng hàng
    expiry_date: Optional[str] = None
    batch_no: Optional[str] = None
    pesticide_id: Optional[int] = None
    fertilizer_id: Optional[int] = None
    db_match: Optional[Dict[str, Any]] = None
    is_banned: bool = False
    banned_info: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "material_name": self.material_name,
            "material_type": self.material_type,
            "active_ingredient": self.active_ingredient,
            "quantity": self.quantity,
            "unit": self.unit,
            "unit_price": self.unit_price,
            "amount": self.amount,
            "expiry_date": self.expiry_date,
            "batch_no": self.batch_no,
            "pesticide_id": self.pesticide_id,
            "fertilizer_id": self.fertilizer_id,
            "db_match": self.db_match,
            "is_banned": self.is_banned,
            "banned_info": self.banned_info,
        }


@dataclass
class InvoiceReceiptDetection:
    """Kết quả trích xuất hóa đơn / phiếu mua bán phân bón, thuốc trừ sâu."""
    supplier_name: Optional[str] = None      # Tên đại lý / cửa hàng VTNN
    supplier_phone: Optional[str] = None
    supplier_address: Optional[str] = None
    invoice_no: Optional[str] = None         # Số hóa đơn / Mã phiếu
    purchase_date: Optional[str] = None      # Ngày mua (YYYY-MM-DD)
    buyer_name: Optional[str] = None
    total_amount: Optional[Any] = None       # Tổng tiền thanh toán
    payment_status: Optional[str] = None
    items: List[InvoiceItem] = field(default_factory=list)
    has_banned_items: bool = False
    banned_items: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "supplier_name": self.supplier_name,
            "supplier_phone": self.supplier_phone,
            "supplier_address": self.supplier_address,
            "invoice_no": self.invoice_no,
            "purchase_date": self.purchase_date,
            "buyer_name": self.buyer_name,
            "total_amount": self.total_amount,
            "payment_status": self.payment_status,
            "items": [it.to_dict() for it in self.items],
            "has_banned_items": self.has_banned_items,
            "banned_items": self.banned_items,
        }


@dataclass
class VLMAnalysisResult:
    """Kết quả phân tích tổng thể từ VLM Engine."""
    status: str = "success"
    category: str = "other"                  # 'pest_disease', 'pesticide', 'fertilizer', 'field_crop', 'invoice_receipt', 'other'
    title: str = ""
    summary: str = ""
    confidence_score: float = 1.0
    pest_disease: Optional[PestDiseaseDiagnosis] = None
    pesticide: Optional[PesticideDetection] = None
    fertilizer: Optional[FertilizerDetection] = None
    invoice_receipt: Optional[InvoiceReceiptDetection] = None
    execution_time_ms: float = 0.0
    model_used: str = ""
    raw_json: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    def to_diary_payload(
        self,
        intent: Optional[str] = None,
        parcel_id: Optional[Any] = None,
        crop_name: Optional[str] = None,
        activity_date: Optional[str] = None,
        extra_fields: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Chuyển đổi kết quả phân tích VLM thành Payload thêm hoạt động nhật ký canh tác chuẩn VietGAP.
        Hỗ trợ 4 loại hoạt động:
        1. 'purchase': Mua vật tư (phân bón, thuốc BVTV, hạt giống)
        2. 'spray': Phun thuốc BVTV
        3. 'fertilize': Bón phân
        4. 'observation': Quan sát sâu bệnh hại

        Nếu intent = None hoặc 'auto':
        - Ảnh sâu bệnh (pest_disease) -> tự động tạo hoạt động 'observation'
        - Ảnh thuốc BVTV (pesticide) -> tự động tạo hoạt động 'spray'
        - Ảnh phân bón (fertilizer) -> tự động tạo hoạt động 'fertilize'
        - Ảnh hóa đơn (invoice_receipt) -> tự động tạo hoạt động 'purchase'
        """
        if self.status != "success":
            return None

        today_iso = activity_date or date.today().isoformat()
        now_time = datetime.now().strftime("%H:%M:%S")

        # Tự động xác định intent nếu không chỉ định
        act_intent = (intent or "auto").lower().strip()
        if act_intent == "auto":
            if self.category == "pest_disease" or self.pest_disease:
                act_intent = "observation"
            elif self.category == "pesticide" or self.pesticide:
                act_intent = "spray"
            elif self.category == "fertilizer" or self.fertilizer:
                act_intent = "fertilize"
            elif self.category == "invoice_receipt":
                act_intent = "purchase"
            else:
                act_intent = "observation" if self.pest_disease else ("spray" if self.pesticide else ("fertilize" if self.fertilizer else "general"))

        # 1. HOẠT ĐỘNG PHUN THUỐC BVTV (spray)
        if act_intent == "spray":
            p_name = "Thuốc BVTV"
            p_id = None
            t_pests = "Sâu bệnh hại"
            dosage = "Pha theo hướng dẫn trên bao bì"
            iso_days = 7
            sup_name = None
            note_parts = []

            if self.pesticide:
                p = self.pesticide
                p_name = p.trade_name or p_name
                if p.db_match and p.db_match.get("matched"):
                    p_id = p.db_match.get("id")
                    p_name = p.db_match.get("name") or p_name
                if p.target_pests:
                    t_pests = ", ".join(p.target_pests)
                if p.dosage_and_usage:
                    dosage = p.dosage_and_usage
                if p.isolation_days:
                    iso_days = p.isolation_days
                if p.manufacturer:
                    sup_name = p.manufacturer
                if p.active_ingredient:
                    note_parts.append(f"Hoạt chất: {p.active_ingredient}")
                if p.formulation:
                    note_parts.append(f"Dạng: {p.formulation}")
            elif self.pest_disease:
                t_pests = self.pest_disease.pest_name
                if self.pest_disease.matched_farm_pesticides:
                    best_med = self.pest_disease.matched_farm_pesticides[0]
                    p_name = best_med["name"]
                    p_id = best_med["pesticide_id"]
                    if best_med.get("min_isolation_days"):
                        iso_days = best_med["min_isolation_days"]
                    if best_med.get("dosage"):
                        dosage = best_med["dosage"]
                    sup_name = best_med.get("manufacturer")
                note_parts.append(f"Phun trị sâu bệnh: {self.pest_disease.pest_name}")

            table_data = {
                "activity_date": today_iso,
                "activity_time": now_time,
                "ten_thuoc_bvtv": p_name,
                "pesticide_id": p_id,
                "target_pest": t_pests,
                "nong_do_va_luong_dung": dosage,
                "application_method": "Phun sương ướt đều tán lá",
                "thoi_gian_cach_ly": f"{iso_days} ngày",
                "isolation_days": iso_days,
                "growth_stage": "Sinh trưởng",
                "supplier_name": sup_name,
                "note": "; ".join(note_parts) or f"Phun thuốc {p_name} nhận diện từ ảnh VLM",
            }
            if crop_name:
                table_data["crop_name"] = crop_name
            if parcel_id:
                table_data["plot_id"] = parcel_id
            if extra_fields and isinstance(extra_fields, dict):
                table_data.update(extra_fields)

            payload_res = {
                "task_types": ["spray"],
                "tables": {"spray": table_data},
                "raw_text": f"Phun thuốc {p_name} phòng trừ {t_pests} (Nhận diện từ ảnh VLM)",
            }
            if parcel_id:
                payload_res["plot_id"] = parcel_id
            return payload_res

        # 2. HOẠT ĐỘNG BÓN PHÂN (fertilize)
        elif act_intent == "fertilize":
            f_name = "Phân bón"
            f_id = None
            dosage = "Bón theo hướng dẫn kỹ thuật"
            method = "Bón gốc / Tưới rãnh theo tán"
            sup_name = None
            note_parts = []

            if self.fertilizer:
                f = self.fertilizer
                f_name = f.trade_name or f_name
                if f.db_match and f.db_match.get("matched"):
                    f_id = f.db_match.get("id")
                    f_name = f.db_match.get("name") or f_name
                if f.dosage_and_usage:
                    dosage = f.dosage_and_usage
                if f.fertilizer_type and "lá" in f.fertilizer_type.lower():
                    method = "Phun qua lá"
                if f.manufacturer:
                    sup_name = f.manufacturer
                if f.composition_npk:
                    note_parts.append(f"Thành phần: {f.composition_npk}")
                if f.benefits:
                    note_parts.append(f"Công dụng: {', '.join(f.benefits)}")

            table_data = {
                "activity_date": today_iso,
                "activity_time": now_time,
                "ten_phan_bon": f_name,
                "fertilizer_id": f_id,
                "luong_phan_bon": dosage,
                "phuong_phap_bon": method,
                "growth_stage": "Sinh trưởng",
                "supplier_name": sup_name,
                "note": "; ".join(note_parts) or f"Bón phân {f_name} nhận diện từ ảnh VLM",
            }
            if crop_name:
                table_data["crop_name"] = crop_name
            if parcel_id:
                table_data["plot_id"] = parcel_id
            if extra_fields and isinstance(extra_fields, dict):
                table_data.update(extra_fields)

            payload_res = {
                "task_types": ["fertilize"],
                "tables": {"fertilize": table_data},
                "raw_text": f"Bón phân {f_name} (Nhận diện từ ảnh VLM)",
            }
            if parcel_id:
                payload_res["plot_id"] = parcel_id
            return payload_res

        # 3. HOẠT ĐỘNG MUA VẬT TƯ (purchase)
        elif act_intent == "purchase":
            m_name = self.title or "Vật tư nông nghiệp"
            m_type = "Khác"
            p_id = None
            f_id = None
            sup_name = None
            unit = "chai"
            qty = 1.0
            u_price = None
            t_amount = None
            inv_no = None
            p_date = today_iso
            exp_date = None
            invoice_items_payload: List[Dict[str, Any]] = []
            note_parts = []

            if self.invoice_receipt:
                inv = self.invoice_receipt
                sup_name = inv.supplier_name or sup_name
                inv_no = inv.invoice_no
                if inv.purchase_date:
                    p_date = inv.purchase_date
                t_amount = inv.total_amount
                if inv.items:
                    for it in inv.items:
                        it_dict = it.to_dict()
                        if sup_name and not it_dict.get("supplier_name"):
                            it_dict["supplier_name"] = sup_name
                        if inv_no and not it_dict.get("invoice_no"):
                            it_dict["invoice_no"] = inv_no
                        if p_date and not it_dict.get("purchase_date"):
                            it_dict["purchase_date"] = p_date
                        invoice_items_payload.append(it_dict)

                    first_it = inv.items[0]
                    p_id = first_it.pesticide_id
                    f_id = first_it.fertilizer_id
                    if len(inv.items) == 1:
                        m_name = first_it.material_name or m_name
                        m_type = first_it.material_type or "Vật tư"
                        qty = first_it.quantity or 1.0
                        unit = first_it.unit or "đơn vị"
                        u_price = first_it.unit_price
                        exp_date = first_it.expiry_date
                        if first_it.active_ingredient:
                            note_parts.append(f"Hoạt chất/TP: {first_it.active_ingredient}")
                    else:
                        item_names = [
                            f"{i.material_name} ({int(i.quantity) if float(i.quantity or 1).is_integer() else i.quantity} {i.unit})"
                            for i in inv.items
                        ]
                        m_name = ", ".join([i.material_name for i in inv.items])
                        types_set = {i.material_type for i in inv.items if i.material_type}
                        if "Thuốc BVTV" in types_set and "Phân bón" in types_set:
                            m_type = "Phân bón & Thuốc BVTV"
                        elif "Thuốc BVTV" in types_set:
                            m_type = "Thuốc BVTV"
                        elif "Phân bón" in types_set:
                            m_type = "Phân bón"
                        else:
                            m_type = first_it.material_type or "Vật tư"
                        qty = float(len(inv.items))
                        unit = "loại vật tư"
                        u_price = t_amount or first_it.unit_price
                        note_parts.append("Chi tiết hóa đơn: " + "; ".join(item_names))

                if inv.total_amount:
                    note_parts.append(f"Tổng tiền HĐ: {inv.total_amount}")
                if inv.supplier_phone:
                    note_parts.append(f"SĐT đại lý: {inv.supplier_phone}")

            elif self.pesticide:
                m_type = "Thuốc BVTV"
                m_name = self.pesticide.trade_name or m_name
                if self.pesticide.db_match and self.pesticide.db_match.get("matched"):
                    p_id = self.pesticide.db_match.get("id")
                    m_name = self.pesticide.db_match.get("name") or m_name
                if self.pesticide.formulation and any(k in self.pesticide.formulation.upper() for k in ["WG", "WP", "GR"]):
                    unit = "gói"
                sup_name = self.pesticide.manufacturer
                if self.pesticide.active_ingredient:
                    note_parts.append(f"Hoạt chất: {self.pesticide.active_ingredient}")
            elif self.fertilizer:
                m_type = "Phân bón"
                m_name = self.fertilizer.trade_name or m_name
                if self.fertilizer.db_match and self.fertilizer.db_match.get("matched"):
                    f_id = self.fertilizer.db_match.get("id")
                    m_name = self.fertilizer.db_match.get("name") or m_name
                if self.fertilizer.fertilizer_type and any(k in self.fertilizer.fertilizer_type.lower() for k in ["hữu cơ", "vô cơ", "npk"]):
                    unit = "bao"
                sup_name = self.fertilizer.manufacturer
                if self.fertilizer.composition_npk:
                    note_parts.append(f"Thành phần: {self.fertilizer.composition_npk}")

            table_data = {
                "purchase_date": p_date,
                "material_name": m_name,
                "material_type": m_type,
                "pesticide_id": p_id,
                "fertilizer_id": f_id,
                "quantity": qty,
                "unit": unit,
                "unit_price": u_price,
                "total_amount": t_amount,
                "invoice_no": inv_no,
                "expiry_date": exp_date,
                "supplier_name": sup_name,
                "invoice_items": invoice_items_payload if invoice_items_payload else None,
                "note": "; ".join(note_parts) or f"Mua {m_name} (Nhận diện từ ảnh VLM)",
            }
            if parcel_id:
                table_data["plot_id"] = parcel_id
            if extra_fields and isinstance(extra_fields, dict):
                table_data.update(extra_fields)

            payload_res = {
                "task_types": ["purchase"],
                "tables": {"purchase": table_data},
                "raw_text": f"Mua {m_name} ({m_type}) nhận diện từ ảnh VLM",
            }
            if parcel_id:
                payload_res["plot_id"] = parcel_id
            return payload_res

        # 4. HOẠT ĐỘNG QUAN SÁT SÂU BỆNH HẠI (observation)
        elif act_intent == "observation":
            p_name = "Sinh vật gây hại"
            p_type = "Sâu bệnh hại"
            sev = "Nhẹ"
            crop = crop_name or "Cây trồng"
            part = "Lá"
            action_desc = "Theo dõi vườn định kỳ"
            note_parts = []

            if self.pest_disease:
                pd = self.pest_disease
                p_name = pd.pest_name or p_name
                p_type = pd.pest_type or ("Sâu hại" if any(k in p_name.lower() for k in ["sâu", "rầy", "bọ", "rệp", "nhện", "ruồi"]) else "Bệnh hại")
                sev = pd.severity or sev
                crop = crop_name or pd.crop_name or crop
                part = pd.affected_part or part

                action_items = []
                if pd.cultural_controls:
                    action_items.append(f"Canh tác: {pd.cultural_controls}")
                if pd.biological_controls:
                    action_items.append(f"Sinh học: {pd.biological_controls}")
                if pd.matched_farm_pesticides:
                    med_names = ", ".join([m["name"] for m in pd.matched_farm_pesticides[:2]])
                    action_items.append(f"Thuốc sẵn có trong kho: {med_names}")
                elif pd.recommended_actives:
                    action_items.append(f"Hoạt chất khuyến nghị: {', '.join(pd.recommended_actives)}")
                if action_items:
                    action_desc = "; ".join(action_items)

                if pd.symptoms:
                    note_parts.append(f"Triệu chứng: {pd.symptoms}")
                if pd.scientific_name:
                    note_parts.append(f"Tên KH: {pd.scientific_name}")
                if pd.causes_and_conditions:
                    note_parts.append(f"ĐK phát sinh: {pd.causes_and_conditions}")

            table_data = {
                "activity_date": today_iso,
                "activity_time": now_time,
                "pest_name": p_name,
                "sinh_vat_gay_hai": p_name,
                "pest_type": p_type,
                "severity": sev,
                "muc_do": sev,
                "crop_name": crop,
                "affected_part": part,
                "action_taken": action_desc,
                "bien_phap_xu_ly": action_desc,
                "description": f"Quan sát phát hiện: {p_name} ({sev}) trên {crop}",
                "note": "; ".join(note_parts) or f"Phát hiện {p_name} từ ảnh chụp thực địa",
            }
            if parcel_id:
                table_data["plot_id"] = parcel_id
            if extra_fields and isinstance(extra_fields, dict):
                table_data.update(extra_fields)

            payload_res = {
                "task_types": ["observation"],
                "tables": {"observation": table_data},
                "raw_text": f"Quan sát phát hiện {p_name} ({p_type}, mức độ {sev}) trên {crop}",
            }
            if parcel_id:
                payload_res["plot_id"] = parcel_id
            return payload_res

        return None

    def save_to_database(
        self,
        farmer_id: Optional[int] = None,
        intent: Optional[str] = None,
        photo_url: str = "",
        parcel_id: Optional[Any] = None,
        crop_name: Optional[str] = None,
        activity_date: Optional[str] = None,
        extra_fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Lưu trực tiếp kết quả phân tích VLM vào CSDL MySQL vietgap_diary.
        Tự động chọn bảng chi tiết tương ứng (spray, fertilize, purchase, observation)
        và lưu liên kết ảnh vào activity_photos.
        """
        payload = self.to_diary_payload(
            intent=intent,
            parcel_id=parcel_id,
            crop_name=crop_name,
            activity_date=activity_date,
            extra_fields=extra_fields,
        )
        if not payload:
            return {
                "status": "error",
                "message": "Không thể tạo payload hoạt động từ kết quả VLM.",
            }

        from services.database import save_diary_to_mysql
        return save_diary_to_mysql(
            farmer_id=farmer_id,
            data=payload,
            raw_text=payload.get("raw_text", ""),
            photo_url=photo_url,
            time_process=round(self.execution_time_ms / 1000.0, 3) if self.execution_time_ms else 0.0,
            llm_meta={
                "engine": "vlm_gemini",
                "model": self.model_used,
                "category": self.category,
                "confidence_score": self.confidence_score,
                "raw_response": self.raw_json or self.summary,
                "raw_llm_output": self.raw_json or self.summary,
            },
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "category": self.category,
            "title": self.title,
            "summary": self.summary,
            "confidence_score": round(self.confidence_score, 2),
            "pest_disease": self.pest_disease.to_dict() if self.pest_disease else None,
            "pesticide": self.pesticide.to_dict() if self.pesticide else None,
            "fertilizer": self.fertilizer.to_dict() if self.fertilizer else None,
            "invoice_receipt": self.invoice_receipt.to_dict() if self.invoice_receipt else None,
            "execution_time_ms": round(self.execution_time_ms, 1),
            "model_used": self.model_used,
            "raw_json": self.raw_json,
            "raw_response": self.raw_json or self.summary,
            "raw_llm_output": self.raw_json or self.summary,
            "llm_meta": {
                "engine": "vlm_gemini",
                "model": self.model_used,
                "category": self.category,
                "confidence_score": round(self.confidence_score, 2),
                "latency_s": round(self.execution_time_ms / 1000.0, 3) if self.execution_time_ms else 0.0,
                "raw_response": self.raw_json or self.summary,
                "raw_llm_output": self.raw_json or self.summary,
            },
            "activity_payload": self.to_diary_payload(),
            "error": self.error,
        }


# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================

def prepare_image_base64(
    image_input: Union[str, bytes, io.BytesIO, Image.Image],
    max_dimension: int = 1600,
    quality: int = 85,
) -> Tuple[str, str]:
    """
    Chuẩn hóa và nén hình ảnh trước khi gửi đến Gemini API.
    Giảm dung lượng ảnh mà vẫn giữ trọn vẹn chi tiết văn bản nhãn và tổn thương sâu bệnh.
    Trả về: (base64_data_str, mime_type)
    """
    img: Optional[Image.Image] = None
    mime_type = "image/jpeg"

    if isinstance(image_input, Image.Image):
        img = image_input
    elif isinstance(image_input, bytes):
        img = Image.open(io.BytesIO(image_input))
    elif isinstance(image_input, io.BytesIO):
        img = Image.open(image_input)
    elif isinstance(image_input, str):
        # Có thể là đường dẫn file hoặc data URL
        if image_input.startswith("data:image/") and ";base64," in image_input:
            header, b64_str = image_input.split(";base64,", 1)
            raw_bytes = base64.b64decode(b64_str)
            img = Image.open(io.BytesIO(raw_bytes))
        elif os.path.exists(image_input):
            img = Image.open(image_input)
        else:
            raise FileNotFoundError(f"Không tìm thấy file ảnh: {image_input}")
    else:
        raise ValueError(f"Kiểu dữ liệu ảnh không hỗ trợ: {type(image_input)}")

    # Xử lý xoay theo EXIF orientation nếu có
    try:
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    # Chuyển đổi màu RGBA/P sang RGB
    if img.mode in ("RGBA", "LA", "P"):
        rgb_img = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "RGBA":
            rgb_img.paste(img, mask=img.split()[-1])
        else:
            rgb_img.paste(img.convert("RGB"))
        img = rgb_img
    elif img.mode != "RGB":
        img = img.convert("RGB")

    # Thu nhỏ nếu kích thước vượt quá max_dimension
    w, h = img.size
    if max(w, h) > max_dimension:
        ratio = max_dimension / float(max(w, h))
        new_w = max(1, int(w * ratio))
        new_h = max(1, int(h * ratio))
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    # Lưu sang buffer JPEG
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    buf.seek(0)
    b64_data = base64.b64encode(buf.read()).decode("utf-8")
    return b64_data, mime_type


def find_farm_pesticides_for_pest(
    pest_name: str,
    pest_type: Optional[str] = None,
    recommended_actives: Optional[List[str]] = None,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """
    Tra cứu danh mục thuốc BVTV trong MySQL trang trại để tìm các loại thuốc
    có khả năng phòng trừ sâu bệnh này theo dữ liệu RAG và đăng ký VietGAP.
    """
    if not pest_name or not pest_name.strip():
        return []

    p_query = pest_name.lower().strip()
    # Tách từ khóa quan trọng
    keywords = [w.strip() for w in re.split(r"[\s,;/\-_]+", p_query) if len(w.strip()) >= 2]
    # Lọc bỏ từ quá chung chung
    stop_words = {"bệnh", "sâu", "hại", "trên", "cây", "lá", "quả", "trái", "thân", "rễ", "con"}
    keywords = [w for w in keywords if w not in stop_words]
    if not keywords:
        keywords = [p_query]

    active_keywords = [a.lower().strip() for a in (recommended_actives or []) if a and len(a.strip()) >= 3]

    import pymysql
    from services.database import get_db_connection

    matches: List[Dict[str, Any]] = []
    seen_ids = set()

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT pesticide_id, name, active_ingredient, formulation, 
                           registration_no, min_isolation_days, max_concentration, 
                           toxicity_group, allowed_crops, manufacturer, rag_knowledge 
                    FROM pesticides;
                """)
                rows = cur.fetchall()

                for r in rows:
                    p_id = r["pesticide_id"]
                    p_name = r["name"]
                    p_active = str(r.get("active_ingredient") or "")

                    rag = r.get("rag_knowledge")
                    if isinstance(rag, str):
                        try:
                            rag = json.loads(rag)
                        except Exception:
                            rag = {}
                    elif not rag:
                        rag = {}

                    target_pests = str(rag.get("doi_tuong_gay_hai") or "") + " " + str(rag.get("phong_ngua_nhom_benh") or "")
                    target_pests_lower = target_pests.lower()
                    allowed_crops = str(r.get("allowed_crops") or "")

                    match_score = 0
                    reasons = []

                    # 1. So khớp tên sâu bệnh trực tiếp
                    if p_query in target_pests_lower:
                        match_score += 90
                        reasons.append(f"Đặc trị chính xác: '{pest_name}'")
                    else:
                        hit_count = sum(1 for kw in keywords if kw in target_pests_lower)
                        if hit_count >= 2:
                            match_score += 70
                            reasons.append("Phòng trừ nhóm dịch hại tương ứng")
                        elif hit_count == 1 and len(keywords) == 1:
                            match_score += 65
                            reasons.append("Phòng trừ nhóm dịch hại tương ứng")

                    # 2. So khớp hoạt chất khuyến nghị
                    for act in active_keywords:
                        if act in p_active.lower():
                            match_score += 40
                            reasons.append(f"Chứa hoạt chất chuẩn: {act}")
                            break

                    if match_score >= 50 and p_id not in seen_ids:
                        seen_ids.add(p_id)
                        matches.append({
                            "pesticide_id": p_id,
                            "name": p_name,
                            "active_ingredient": p_active,
                            "formulation": r.get("formulation"),
                            "min_isolation_days": r.get("min_isolation_days"),
                            "max_concentration": r.get("max_concentration"),
                            "dosage": rag.get("lieu_luong_nong_do"),
                            "manufacturer": r.get("manufacturer"),
                            "registered_pests": target_pests.strip(),
                            "match_reason": ", ".join(reasons),
                            "match_score": match_score,
                        })

    except Exception as e:
        logger.warning("Lỗi tra cứu thuốc BVTV trong CSDL cho sâu bệnh '%s': %s", pest_name, e)

    # Sắp xếp theo điểm tương thích
    matches.sort(key=lambda x: x["match_score"], reverse=True)
    return matches[:limit]


# ==============================================================================
# VLM ENGINE CLASS
# ==============================================================================

class VLMEngine:
    """
    Vision-Language Model Engine kết nối Google AI Gemini (mô hình 3.1 Flash Lite).
    Chuyên trách nhận diện sâu bệnh, tên thuốc, phân bón và hiện trường canh tác VietGAP từ hình ảnh.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: Optional[str] = None,
        timeout_sec: Optional[int] = None,
    ):
        self.api_key = api_key or config.GEMINI_API_KEY
        self.default_model = default_model or DEFAULT_VLM_MODEL
        self.timeout_sec = timeout_sec or getattr(config, "GEMINI_TIMEOUT_SEC", 30)
        self._session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(3)

    def _ensure_session(self) -> aiohttp.ClientSession:
        try:
            curr_loop = asyncio.get_running_loop()
        except RuntimeError:
            curr_loop = None

        if (
            self._session is None
            or self._session.closed
            or getattr(self._session, "_loop", None) is None
            or self._session._loop.is_closed()
            or (curr_loop and self._session._loop != curr_loop)
        ):
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_sec),
                connector=aiohttp.TCPConnector(ssl=_SSL_CONTEXT),
            )
        return self._session

    async def close(self):
        """Đóng session aiohttp."""
        if self._session and not self._session.closed:
            await self._session.close()

    # --------------------------------------------------------------------------
    # Prompt Construction
    # --------------------------------------------------------------------------

    def _build_system_instruction(self, task: str = "auto", crop_name: Optional[str] = None) -> str:
        """Xây dựng system prompt hướng dẫn Gemini nhận diện và phân tích ảnh nông nghiệp theo giống cây đang trồng."""
        if not crop_name or crop_name == "Cà gai leo":
            try:
                from services.database import get_db_connection, get_crop_name_for_farmer
                conn = get_db_connection()
                crop_name = get_crop_name_for_farmer(conn)
                conn.close()
            except Exception:
                crop_name = crop_name or "Bưởi"

        return f"""Bạn là chuyên gia VLM (Vision-Language Model) hàng đầu về Nông nghiệp nhiệt đới và tiêu chuẩn VietGAP.

BỐI CẢNH CANH TÁC CỦA TRANG TRẠI:
- Trang trại này là vùng canh tác chuyên nghiệp trồng giống cây: {crop_name} đạt chuẩn VietGAP.
- BẮT BUỘC: Khi phân tích, chẩn đoán SÂU BỆNH HẠI trên cây trồng (pest_disease), tập trung chẩn đoán trên giống cây đang trồng là "{crop_name}" (crop_name: "{crop_name}").
  TUYỆT ĐỐI KHÔNG chẩn đoán hoặc nói lan man sang các loại cây khác trừ khi trên nhãn bao bì thuốc BVTV hoặc phân bón có danh mục đăng ký riêng.

Nhiệm vụ: Phân tích kỹ lưỡng hình ảnh do nông dân hoặc kỹ sư nông nghiệp cung cấp để nhận diện chính xác 1 trong các nhóm đối tượng:
1. SÂU BỆNH HẠI (Pest & Disease - "pest_disease"): Cây trồng bị nấm, vi khuẩn, sâu, rầy, nhện hại, thán thư, đốm lá, rỉ sắt, xì mủ, vàng lá, rệp sáp... trên cây {crop_name}.
2. THUỐC BẢO VỆ THỰC VẬT (Pesticide - "pesticide"): Nhãn chai, gói, bao bì thuốc trừ sâu, trừ bệnh, diệt cỏ, diệt ốc, điều hòa sinh trưởng.
3. PHÂN BÓN (Fertilizer - "fertilizer"): Nhãn bao, chai, hũ phân bón lá, phân hữu cơ, NPK, vi lượng, vi sinh, phân chuồng, chất cải tạo đất.
4. HÓA ĐƠN MUA VẬT TƯ NÔNG NGHIỆP (Invoice / Receipt - "invoice_receipt"): Hóa đơn bán hàng, hóa đơn GTGT, biên lai, phiếu xuất kho, sổ ghi mua phân bón, thuốc trừ sâu, thuốc BVTV, giống cây, vật tư nông nghiệp (kể cả hóa đơn in hoặc viết tay). Lưu ý: Nếu là hóa đơn ngoài nông nghiệp (ví dụ: hóa đơn thuốc tây/dược phẩm người như Pharmacity, hóa đơn siêu thị, ăn uống, điện nước...), đặt category là "other" và "invoice_receipt" để null.
5. KHÁC (Other/Field - "field_crop" | "other"): Vườn cây {crop_name} tổng quan hoặc ảnh không liên quan.

BẮT BUỘC TRẢ VỀ DUY NHẤT MỘT ĐỐI TƯỢNG JSON THEO ĐÚNG CẤU TRÚC SAU (không kèm markdown ngoài JSON):
{{
  "category": "pest_disease" | "pesticide" | "fertilizer" | "invoice_receipt" | "field_crop" | "other",
  "title": "Tên tiêu đề ngắn gọn (ví dụ: Hóa đơn mua phân bón & thuốc BVTV, hoặc Bệnh thán thư trên cây {crop_name}, hoặc Thuốc trừ sâu Regent 800WG, hoặc Phân bón NPK Đầu Trâu)",
  "summary": "Tóm tắt chẩn đoán hoặc nội dung hóa đơn trong 1-2 câu ngắn gọn",
  "confidence_score": 0.95,
  "pest_disease": {{
    "pest_name": "Tên sâu bệnh hại thông dụng tiếng Việt (ví dụ: Bệnh thán thư, Bệnh lở cổ rễ, Đốm mắt cua, Rệp sáp, Nhện đỏ, Bọ trĩ...)",
    "scientific_name": "Tên khoa học nếu xác định được (ví dụ: Colletotrichum spp., Rhizoctonia solani...)",
    "pest_type": "Nấm bệnh" | "Sâu hại" | "Vi khuẩn" | "Nhện hại" | "Rầy/Rệp" | "Virus" | "Tuyến trùng" | "Rối loạn dinh dưỡng",
    "crop_name": "{crop_name}",
    "affected_part": "Lá" | "Thân/Cành" | "Rễ" | "Quả" | "Hoa" | "Toàn cây",
    "symptoms": "Mô tả ngắn gọn triệu chứng tổn thương nhìn thấy trên ảnh",
    "severity": "Nhẹ" | "Trung bình" | "Nặng",
    "causes_and_conditions": "Điều kiện thời tiết hoặc môi trường thuận lợi phát sinh",
    "cultural_controls": "Biện pháp canh tác phòng trị an toàn VietGAP (cắt tỉa cành bệnh, vệ sinh tàn dư, thoát nước...)",
    "biological_controls": "Biện pháp sinh học (dùng nấm đối kháng Trichoderma, Bacillus, bẫy dính...)",
    "recommended_actives": ["hoạt chất khuyến nghị 1", "hoạt chất 2 (ví dụ: Metalaxyl, Mancozeb, Hexaconazole, Abamectin...)"]
  }},
  "pesticide": {{
    "trade_name": "Tên thương phẩm đầy đủ trên bao bì (ví dụ: Ridomil Gold 68WG, Anvil 5SC, SIAMB 12...)",
    "active_ingredient": "Hoạt chất và hàm lượng (ví dụ: Metalaxyl M 40g/kg + Mancozeb 640g/kg)",
    "formulation": "Dạng thuốc (WG, SC, EC, WP, SL, OD, GR...)",
    "toxicity_group": "Nhóm độc (ví dụ: GHS nhóm 4, Cẩn thận, Độc trung bình...)",
    "target_pests": ["Sương mai", "Thán thư", "Chết nhanh", "Loét sọc"],
    "registered_crops": ["{crop_name}", "Cây dược liệu", "Rau màu"],
    "dosage_and_usage": "Liều lượng pha chi tiết (ví dụ: Pha 50g/bình 16-25 lít hoặc 800g-1kg/phuy 200 lít)",
    "isolation_days": 14,
    "manufacturer": "Tên công ty sản xuất hoặc đăng ký phân phối",
    "safety_precautions": "Lưu ý bảo hộ lao động và nguồn nước khi sử dụng"
  }},
  "fertilizer": {{
    "trade_name": "Tên phân bón đầy đủ trên bao bì (ví dụ: NPK Đầu Trâu 20-20-15, SIAMB 12, Canxi Bo...)",
    "fertilizer_type": "Phân bón lá" | "Vi lượng" | "Hữu cơ" | "Hữu cơ vi sinh" | "Vô cơ" | "NPK" | "Phân chuồng",
    "composition_npk": "Thành phần dinh dưỡng (ví dụ: Kẽm (Zn): 15.000ppm, N-P-K: 20-20-15...)",
    "benefits": ["Kích thích ra hoa", "Chống rụng hoa quả", "Phát triển bộ rễ dược liệu"],
    "dosage_and_usage": "Hướng dẫn liều lượng bón hoặc pha tưới gốc / phun lá",
    "suitable_crops": ["{crop_name}", "Cây dược liệu", "Rau màu"],
    "manufacturer": "Tên công ty sản xuất hoặc phân phối"
  }},
  "invoice_receipt": {{
    "supplier_name": "Tên đại lý / cửa hàng vật tư nông nghiệp / nhà cung cấp trên hóa đơn",
    "supplier_phone": "Số điện thoại đại lý (nếu có)",
    "supplier_address": "Địa chỉ đại lý / cửa hàng (nếu có)",
    "invoice_no": "Số hóa đơn / Mã phiếu / Số chứng từ (nếu có)",
    "purchase_date": "Ngày mua hàng trên hóa đơn chuẩn hóa định dạng YYYY-MM-DD (nếu nhìn thấy)",
    "buyer_name": "Tên khách hàng / người mua (nếu có)",
    "total_amount": "Tổng cộng tiền thanh toán cuối cùng của cả hóa đơn (chú ý đọc kỹ số tổng cộng cuối bảng, kể cả khi người bán viết lệch xuống dòng dưới; ví dụ: '7.585.000 đ')",
    "payment_status": "Đã thanh toán / Tiền mặt / Chuyển khoản / Ghi nợ (nếu có)",
    "items": [
      {{
        "material_name": "Ghi CHÍNH XÁC tên phân bón / thuốc BVTV / vật tư theo đúng chữ viết trên từng dòng hóa đơn (không tự ý đổi sang tên khác)",
        "material_type": "Thuốc BVTV" | "Phân bón" | "Giống" | "Vật tư",
        "active_ingredient": "Hoạt chất hoặc hàm lượng NPK nếu có ghi kèm",
        "quantity": 10,
        "unit": "Chỉ điền đơn vị tính ('bao', 'chai', 'gói', 'kg', 'lít', 'lon', 'hộp') NẾU trên hóa đơn có ghi rõ ở cột Đơn vị tính hoặc ghi kèm trong tên hàng (như 'bao 25kg'); nếu cột Đơn vị tính để trống thì để chuỗi rỗng ''",
        "unit_price": "Đơn giá của dòng hàng (ví dụ: '125.000 đ')",
        "amount": "Thành tiền của dòng hàng = Số lượng x Đơn giá (ví dụ: '500.000 đ')",
        "expiry_date": "Hạn sử dụng YYYY-MM-DD (nếu có)",
        "batch_no": "Số lô sản xuất (nếu có)"
      }}
    ]
  }}
}}

QUY TẮC BẮT BUỘC:
1. Nếu ảnh là sâu bệnh hại: Điền chi tiết "pest_disease", "crop_name" luôn là "{crop_name}", còn "pesticide", "fertilizer", "invoice_receipt" để null.
2. Nếu ảnh là thuốc BVTV: Điền chi tiết "pesticide", còn "pest_disease", "fertilizer", "invoice_receipt" để null.
3. Nếu ảnh là phân bón: Điền chi tiết "fertilizer", còn "pest_disease", "pesticide", "invoice_receipt" để null.
4. Nếu ảnh là hóa đơn, biên lai, phiếu xuất kho mua bán phân bón / thuốc trừ sâu / thuốc BVTV / giống / vật tư nông nghiệp: Category BẮT BUỘC là "invoice_receipt", điền đầy đủ khối "invoice_receipt" và danh sách "items" (tách riêng đầy đủ TẤT CẢ từng dòng mặt hàng trên hóa đơn, kể cả dòng viết ở cuối bảng hay dòng tổng cộng nếu có tên hàng + số lượng + đơn giá + thành tiền), các khối còn lại để null. Ngược lại, nếu ảnh là hóa đơn ngoài nông nghiệp (như dược phẩm y tế người, siêu thị, ăn uống...), đặt category là "other", điền rõ "title" và "summary" mô tả hóa đơn đó, và để "invoice_receipt" là null.
5. Nếu ảnh là vườn cây {crop_name} bình thường hoặc khác: Category là "field_crop" hoặc "other", 4 khối trên để null.
6. Tuyệt đối không tự bịa đặt tên thuốc hay thông số không nhìn thấy hoặc không chắc chắn.""".strip()

    # --------------------------------------------------------------------------
    # Core Analysis API Call
    # --------------------------------------------------------------------------

    async def analyze_image(
        self,
        image_input: Union[str, bytes, io.BytesIO, Image.Image],
        prompt: Optional[str] = None,
        task: str = "auto",
        model: Optional[str] = None,
        match_db: bool = True,
        crop_name: Optional[str] = None,
    ) -> VLMAnalysisResult:
        """
        Gửi hình ảnh đến Gemini 3.1 Flash Lite để nhận diện sâu bệnh, thuốc BVTV, phân bón hoặc hóa đơn vật tư.
        
        Args:
            image_input: Đường dẫn file, bytes, buffer hoặc PIL Image
            prompt: Chỉ dẫn bổ sung của người dùng (tùy chọn)
            task: 'auto', 'pest_disease', 'pesticide', 'fertilizer', 'invoice_receipt'
            model: Tên model Gemini ghi đè (mặc định gemini-3.1-flash-lite)
            match_db: Tự động đối soát với CSDL MySQL trang trại (mặc định True)
            crop_name: Tên giống cây đang trồng tại trang trại (mặc định 'Cà gai leo')
        """
        api_key = self.api_key or config.GEMINI_API_KEY
        if not api_key:
            return VLMAnalysisResult(
                status="error",
                error="Thiếu GEMINI_API_KEY trong cấu hình hệ thống.",
                title="Lỗi cấu hình",
            )

        t0 = time.time()

        # 1. Chuẩn bị ảnh base64
        try:
            b64_data, mime_type = prepare_image_base64(image_input)
        except Exception as e_prep:
            logger.error("Lỗi chuẩn bị hình ảnh cho VLM: %s", e_prep)
            return VLMAnalysisResult(
                status="error",
                error=f"Không đọc được hình ảnh: {e_prep}",
                title="Lỗi hình ảnh",
            )

        # 2. Xây dựng prompt — truyền crop_name để prompt chứa đúng tên giống cây
        system_instruction = self._build_system_instruction(task=task, crop_name=crop_name)
        user_text = prompt.strip() if prompt and prompt.strip() else "Hãy phân tích hình ảnh này cẩn thận và xác định đối tượng:"
        if task == "pest_disease":
            user_text += "\n[Yêu cầu]: Hãy tập trung chẩn đoán các triệu chứng sâu bệnh hại, tác nhân gây bệnh và biện pháp xử lý."
        elif task == "pesticide":
            user_text += "\n[Yêu cầu]: Hãy tập trung đọc chi tiết nhãn thuốc BVTV, hoạt chất, dạng thuốc, liều lượng và đối tượng phòng trừ."
        elif task == "fertilizer":
            user_text += "\n[Yêu cầu]: Hãy tập trung đọc nhãn phân bón, loại phân, thành phần NPK/dinh dưỡng và cách bón."
        elif task in ("invoice_receipt", "invoice", "purchase"):
            user_text += (
                "\n[Yêu cầu]: Đây là hóa đơn / biên lai / phiếu mua bán phân bón, thuốc trừ sâu, vật tư nông nghiệp. "
                "Hãy đặt category là 'invoice_receipt' và trích xuất đầy đủ tên đại lý, số hóa đơn, ngày mua (YYYY-MM-DD), "
                "tổng tiền và danh sách từng mặt hàng trong 'items' (tên thuốc/phân bón, phân loại 'Thuốc BVTV' hoặc 'Phân bón', "
                "số lượng, đơn vị tính, đơn giá, thành tiền)."
            )

        payload = {
            "system_instruction": {
                "parts": [{"text": system_instruction}]
            },
            "contents": [
                {
                    "parts": [
                        {"text": user_text},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": b64_data,
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {
                "response_mime_type": "application/json",
                "temperature": 0.1,
            },
        }

        # 3. Danh sách model thử nghiệm (ưu tiên gemini-3.1-flash-lite)
        target_models = [model] if model else FALLBACK_MODELS
        # Loại trùng lặp
        ordered_models = []
        for m in target_models:
            if m and m not in ordered_models:
                ordered_models.append(m)

        session = self._ensure_session()
        last_error = None

        for m_name in ordered_models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:generateContent?key={api_key}"
            try:
                async with self._semaphore:
                    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=self.timeout_sec)) as resp:
                        if resp.status != 200:
                            err_body = await resp.text()
                            logger.warning("[VLM Engine %s] HTTP %d: %s", m_name, resp.status, err_body[:200])
                            last_error = f"HTTP {resp.status} ({m_name})"
                            continue

                        data = await resp.json()
                        candidates = data.get("candidates", [])
                        if not candidates:
                            last_error = "Gemini không trả về candidate nào."
                            continue

                        parts = candidates[0].get("content", {}).get("parts", [])
                        if not parts:
                            last_error = "Nội dung phản hồi rỗng."
                            continue

                        raw_text = parts[0].get("text", "").strip()
                        raw_clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text, flags=re.MULTILINE).strip()
                        parsed = json.loads(raw_clean)

                        elapsed = (time.time() - t0) * 1000
                        return await self._process_vlm_json_output(
                            parsed=parsed,
                            execution_time_ms=elapsed,
                            model_used=m_name,
                            match_db=match_db,
                        )

            except Exception as e_call:
                logger.warning("[VLM Engine %s] Thử nghiệm thất bại: %s", m_name, e_call)
                last_error = str(e_call)

        # Fallback sang RapidOCR + LLM/Regex nếu ảnh hóa đơn gặp lỗi kết nối VLM
        try:
            fallback_res = await self._fallback_ocr_invoice(image_input, t0=t0, match_db=match_db)
            if fallback_res and fallback_res.status == "success":
                return fallback_res
        except Exception as e_fb:
            logger.warning("Fallback OCR hóa đơn thất bại: %s", e_fb)

        return VLMAnalysisResult(
            status="error",
            error=f"Tất cả các model VLM đều gặp lỗi. Lỗi cuối: {last_error}",
            title="Lỗi xử lý VLM",
            execution_time_ms=(time.time() - t0) * 1000,
        )

    async def _fallback_ocr_invoice(
        self,
        image_input: Union[str, bytes, io.BytesIO, Image.Image],
        t0: float,
        match_db: bool = True,
    ) -> Optional[VLMAnalysisResult]:
        """Fallback trích xuất hóa đơn bằng RapidOCR + LLM/MaterialMatcher khi Gemini VLM không khả dụng."""
        from services.engines.ocr_engine import ocr_engine
        ocr_res = await ocr_engine.ocr(image_input, min_score=0.45)
        if not ocr_res or not ocr_res.text or not ocr_res.text.strip():
            return None

        raw_ocr = ocr_res.text.strip()
        lower_ocr = raw_ocr.lower()
        is_invoice_like = any(
            kw in lower_ocr
            for kw in ["hóa đơn", "hoá đơn", "phiếu", "biên lai", "đại lý", "cửa hàng", "tổng tiền", "thành tiền", "đơn giá", "số lượng"]
        )
        matched_mats = await material_matcher.extract_and_match(raw_ocr, min_score=55.0)
        if not is_invoice_like and not matched_mats:
            return None

        items_raw = []
        for m in matched_mats:
            m_dict = m.to_dict()
            cat_str = m_dict.get("category") or ""
            m_type = "Thuốc BVTV" if cat_str == "pesticide" else ("Phân bón" if cat_str == "fertilizer" else "Vật tư")
            items_raw.append({
                "material_name": m_dict.get("name") or m_dict.get("query") or "Vật tư nông nghiệp",
                "material_type": m_type,
                "active_ingredient": m_dict.get("active_ingredient"),
                "quantity": 1,
                "unit": "bao" if m_type == "Phân bón" else "chai",
            })

        sup_match = re.search(r"(?:đại\s*lý|cửa\s*hàng|nhà\s*cung\s*cấp|cty|công\s*ty)[:\s\-]*([^\n\r]{3,60})", raw_ocr, flags=re.IGNORECASE)
        inv_match = re.search(r"(?:số\s*hđ|số\s*hóa\s*đơn|mã\s*phiếu|số\s*phiếu|no\.?)[:\s#\-]*([A-Za-z0-9\-_/]{2,25})", raw_ocr, flags=re.IGNORECASE)
        date_match = re.search(r"(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}|\d{4}[/\-\.]\d{1,2}[/\-\.]\d{1,2})", raw_ocr)
        total_match = re.search(r"(?:tổng\s*cộng|tổng\s*tiền|thanh\s*toán|thành\s*tiền)[:\s]*([\d\.,]+\s*(?:đ|vnđ|vnd|nghìn|triệu)?)", raw_ocr, flags=re.IGNORECASE)

        parsed_fallback = {
            "category": "invoice_receipt",
            "title": "Hóa đơn mua vật tư nông nghiệp",
            "summary": f"Trích xuất từ hóa đơn OCR ({len(items_raw)} mặt hàng).",
            "confidence_score": 0.85,
            "invoice_receipt": {
                "supplier_name": sup_match.group(1).strip() if sup_match else None,
                "invoice_no": inv_match.group(1).strip() if inv_match else None,
                "purchase_date": date_match.group(1).strip() if date_match else None,
                "total_amount": total_match.group(1).strip() if total_match else None,
                "items": items_raw,
            },
        }
        elapsed = (time.time() - t0) * 1000
        return await self._process_vlm_json_output(
            parsed=parsed_fallback,
            execution_time_ms=elapsed,
            model_used="rapidocr_fallback",
            match_db=match_db,
        )

    @staticmethod
    def _normalize_invoice_date(raw_date: Any) -> Optional[str]:
        """Chuẩn hóa ngày trên hóa đơn sang định dạng YYYY-MM-DD."""
        if not raw_date:
            return None
        s = str(raw_date).strip()
        if not s or s.lower() in ("null", "none"):
            return None
        m_iso = re.search(r"(\d{4})[/\-\.](\d{1,2})[/\-\.](\d{1,2})", s)
        if m_iso:
            y, m, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
            if 1 <= m <= 12 and 1 <= d <= 31:
                return f"{y:04d}-{m:02d}-{d:02d}"
        m_vn = re.search(r"(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{2,4})", s)
        if m_vn:
            d, m, y = int(m_vn.group(1)), int(m_vn.group(2)), int(m_vn.group(3))
            if y < 100:
                y += 2000
            if 1 <= m <= 12 and 1 <= d <= 31:
                return f"{y:04d}-{m:02d}-{d:02d}"
        m_text = re.search(r"ngày\s*(\d{1,2})\s*tháng\s*(\d{1,2})(?:\s*năm\s*(\d{4}))?", s, flags=re.IGNORECASE)
        if m_text:
            d, m = int(m_text.group(1)), int(m_text.group(2))
            y = int(m_text.group(3)) if m_text.group(3) else date.today().year
            if 1 <= m <= 12 and 1 <= d <= 31:
                return f"{y:04d}-{m:02d}-{d:02d}"
        return s

    @staticmethod
    def _parse_invoice_qty(raw_qty: Any, current_unit: Optional[str] = None, item_name: Optional[str] = None) -> Tuple[float, str]:
        """Tách số lượng và đơn vị tính từ dòng mặt hàng hóa đơn."""
        unit = (str(current_unit).strip() if current_unit and str(current_unit).strip().lower() not in ("null", "none", "", "đơn vị") else "")
        if not unit and item_name:
            m_u_name = re.search(r"\b(bao|chai|gói|lọ|can|thùng|hộp|túi|lon|xô)\b", str(item_name), flags=re.IGNORECASE)
            if m_u_name:
                unit = m_u_name.group(1).lower()
        if raw_qty is None:
            return 1.0, unit
        if isinstance(raw_qty, (int, float)):
            return float(raw_qty), unit
        s = str(raw_qty).strip()
        m = re.search(r"(\d+(?:[\.,]\d+)?)\s*([a-zA-ZÀ-ỹ]+)?", s)
        if m:
            val = float(m.group(1).replace(",", "."))
            u_found = (m.group(2) or "").strip()
            return val, (unit or u_found)
        return 1.0, unit

    # --------------------------------------------------------------------------
    # Output Processing & Database Linking
    # --------------------------------------------------------------------------

    async def _process_vlm_json_output(
        self,
        parsed: Dict[str, Any],
        execution_time_ms: float,
        model_used: str,
        match_db: bool = True,
    ) -> VLMAnalysisResult:
        """Chuyển đổi JSON từ Gemini thành VLMAnalysisResult có cấu trúc và liên kết CSDL."""
        category = parsed.get("category") or "other"
        title = parsed.get("title") or "Kết quả phân tích hình ảnh"
        summary = parsed.get("summary") or ""
        confidence = float(parsed.get("confidence_score") or 1.0)

        pest_diag: Optional[PestDiseaseDiagnosis] = None
        pest_raw = parsed.get("pest_disease")
        if isinstance(pest_raw, dict) and pest_raw.get("pest_name"):
            pest_name = pest_raw.get("pest_name")
            p_actives = pest_raw.get("recommended_actives") or []
            if isinstance(p_actives, str):
                p_actives = [p_actives]

            # Tra cứu thuốc trong CSDL trang trại phù hợp với sâu bệnh này
            matched_pesticides = []
            if match_db:
                matched_pesticides = find_farm_pesticides_for_pest(
                    pest_name=pest_name,
                    pest_type=pest_raw.get("pest_type"),
                    recommended_actives=p_actives,
                )

            pest_diag = PestDiseaseDiagnosis(
                pest_name=pest_name,
                scientific_name=pest_raw.get("scientific_name"),
                pest_type=pest_raw.get("pest_type"),
                crop_name=pest_raw.get("crop_name"),
                affected_part=pest_raw.get("affected_part"),
                symptoms=pest_raw.get("symptoms"),
                severity=pest_raw.get("severity"),
                causes_and_conditions=pest_raw.get("causes_and_conditions"),
                cultural_controls=pest_raw.get("cultural_controls"),
                biological_controls=pest_raw.get("biological_controls"),
                recommended_actives=p_actives,
                matched_farm_pesticides=matched_pesticides,
            )

        pest_det: Optional[PesticideDetection] = None
        pest_det_raw = parsed.get("pesticide")
        if isinstance(pest_det_raw, dict) and pest_det_raw.get("trade_name"):
            t_name = pest_det_raw.get("trade_name")
            db_match_dict = None
            if match_db and t_name:
                match_res = material_matcher.fuzzy_match(t_name, category_hint="pesticide")
                db_match_dict = match_res.to_dict()

            # Kiểm tra hoạt chất cấm từ cả tên thương phẩm và hoạt chất OCR được
            from services.vietgap.material_matcher import check_banned_ingredient
            ai_text = pest_det_raw.get("active_ingredient") or ""
            is_banned, banned_info = check_banned_ingredient(f"{t_name} {ai_text}")
            if not is_banned and db_match_dict and db_match_dict.get("is_banned"):
                is_banned = True
                banned_info = db_match_dict.get("banned_info")

            pest_det = PesticideDetection(
                trade_name=t_name,
                active_ingredient=pest_det_raw.get("active_ingredient"),
                formulation=pest_det_raw.get("formulation"),
                toxicity_group=pest_det_raw.get("toxicity_group"),
                target_pests=pest_det_raw.get("target_pests") or [],
                registered_crops=pest_det_raw.get("registered_crops") or [],
                dosage_and_usage=pest_det_raw.get("dosage_and_usage") or pest_det_raw.get("dosage"),
                isolation_days=pest_det_raw.get("isolation_days"),
                manufacturer=pest_det_raw.get("manufacturer"),
                safety_precautions=pest_det_raw.get("safety_precautions"),
                db_match=db_match_dict,
                is_banned=is_banned,
                banned_info=banned_info,
            )

        fert_det: Optional[FertilizerDetection] = None
        fert_det_raw = parsed.get("fertilizer")
        if isinstance(fert_det_raw, dict) and fert_det_raw.get("trade_name"):
            f_name = fert_det_raw.get("trade_name")
            db_match_dict = None
            if match_db and f_name:
                match_res = material_matcher.fuzzy_match(f_name, category_hint="fertilizer")
                db_match_dict = match_res.to_dict()

            fert_det = FertilizerDetection(
                trade_name=f_name,
                fertilizer_type=fert_det_raw.get("fertilizer_type"),
                composition_npk=fert_det_raw.get("composition_npk"),
                benefits=fert_det_raw.get("benefits") or [],
                dosage_and_usage=fert_det_raw.get("dosage_and_usage") or fert_det_raw.get("dosage"),
                suitable_crops=fert_det_raw.get("suitable_crops") or [],
                manufacturer=fert_det_raw.get("manufacturer"),
                db_match=db_match_dict,
            )

        inv_det: Optional[InvoiceReceiptDetection] = None
        inv_raw = parsed.get("invoice_receipt")
        if isinstance(inv_raw, dict) or category == "invoice_receipt":
            inv_dict = inv_raw if isinstance(inv_raw, dict) else {}
            from services.vietgap.material_matcher import check_banned_ingredient

            raw_items = inv_dict.get("items") or []
            if not isinstance(raw_items, list):
                raw_items = []

            processed_items: List[InvoiceItem] = []
            banned_items_list: List[Dict[str, Any]] = []

            for it_raw in raw_items:
                if not isinstance(it_raw, dict):
                    continue
                m_name = str(it_raw.get("material_name") or it_raw.get("trade_name") or it_raw.get("name") or "").strip()
                if not m_name:
                    continue

                raw_type = str(it_raw.get("material_type") or "").strip()
                lower_type = raw_type.lower()
                lower_name = m_name.lower()

                cat_hint = None
                has_npk_ratio = bool(re.search(r"\b\d{1,2}\s*[\-\.]\s*\d{1,2}\s*[\-\.]\s*\d{1,2}\b", lower_name))
                if any(k in lower_type or k in lower_name for k in ["thuốc", "bvtv", "trừ sâu", "trừ bệnh", "trừ cỏ", "diệt ốc", "wg", "sc", "ec", "wp", "sl", "od", "atamite", "cy rut", "cyrut", "anpho", "anvil", "comcat"]):
                    m_type_norm = "Thuốc BVTV"
                    cat_hint = "pesticide"
                elif has_npk_ratio or any(k in lower_type or k in lower_name for k in ["phân", "npk", "hữu cơ", "vi lượng", "đạm", "lân", "kali", "urea", "dap", "humic", "canxi", "combi", "manhe", "magie", "mầm", "kẽm", "bo"]):
                    m_type_norm = "Phân bón"
                    cat_hint = "fertilizer"
                elif any(k in lower_type or k in lower_name for k in ["giống", "hạt", "cây con"]):
                    m_type_norm = "Giống"
                else:
                    m_type_norm = raw_type or "Vật tư"

                db_match_dict = None
                p_id = None
                f_id = None
                act_ing = it_raw.get("active_ingredient")

                if match_db:
                    match_res = material_matcher.fuzzy_match(m_name, category_hint=cat_hint)
                    db_match_dict = match_res.to_dict()
                    if db_match_dict.get("matched"):
                        # Giữ nguyên tên thực tế ghi trên hóa đơn trừ khi khớp gần như tuyệt đối (>= 96)
                        if (db_match_dict.get("score") or 0) >= 96:
                            m_name = db_match_dict.get("name") or m_name
                        matched_cat = db_match_dict.get("category")
                        if matched_cat == "pesticide":
                            if not has_npk_ratio:
                                m_type_norm = "Thuốc BVTV"
                            p_id = db_match_dict.get("id")
                        elif matched_cat == "fertilizer":
                            m_type_norm = "Phân bón"
                            f_id = db_match_dict.get("id")
                        if not act_ing and db_match_dict.get("active_ingredient") and (db_match_dict.get("score") or 0) >= 90:
                            act_ing = db_match_dict.get("active_ingredient")

                is_banned, banned_info = check_banned_ingredient(f"{m_name} {act_ing or ''}")
                if not is_banned and db_match_dict and db_match_dict.get("is_banned"):
                    is_banned = True
                    banned_info = db_match_dict.get("banned_info")

                qty_val, unit_str = self._parse_invoice_qty(it_raw.get("quantity"), it_raw.get("unit"), item_name=m_name)
                exp_norm = self._normalize_invoice_date(it_raw.get("expiry_date"))

                inv_item = InvoiceItem(
                    material_name=m_name,
                    material_type=m_type_norm,
                    active_ingredient=act_ing,
                    quantity=qty_val,
                    unit=unit_str,
                    unit_price=it_raw.get("unit_price"),
                    amount=it_raw.get("amount") or it_raw.get("total_price"),
                    expiry_date=exp_norm,
                    batch_no=it_raw.get("batch_no"),
                    pesticide_id=p_id,
                    fertilizer_id=f_id,
                    db_match=db_match_dict,
                    is_banned=is_banned,
                    banned_info=banned_info,
                )
                processed_items.append(inv_item)
                if is_banned:
                    banned_items_list.append({
                        "material_name": m_name,
                        "active_ingredient": act_ing,
                        "banned_info": banned_info,
                    })

            if processed_items or inv_dict.get("supplier_name") or inv_dict.get("invoice_no") or category == "invoice_receipt":
                category = "invoice_receipt"
                norm_p_date = self._normalize_invoice_date(inv_dict.get("purchase_date"))
                inv_det = InvoiceReceiptDetection(
                    supplier_name=inv_dict.get("supplier_name"),
                    supplier_phone=inv_dict.get("supplier_phone"),
                    supplier_address=inv_dict.get("supplier_address"),
                    invoice_no=inv_dict.get("invoice_no"),
                    purchase_date=norm_p_date,
                    buyer_name=inv_dict.get("buyer_name"),
                    total_amount=inv_dict.get("total_amount"),
                    payment_status=inv_dict.get("payment_status"),
                    items=processed_items,
                    has_banned_items=bool(banned_items_list),
                    banned_items=banned_items_list,
                )
                if not title or title == "Kết quả phân tích hình ảnh":
                    title = "Hóa đơn mua phân bón & thuốc BVTV"

        return VLMAnalysisResult(
            status="success",
            category=category,
            title=title,
            summary=summary,
            confidence_score=confidence,
            pest_disease=pest_diag,
            pesticide=pest_det,
            fertilizer=fert_det,
            invoice_receipt=inv_det,
            execution_time_ms=execution_time_ms,
            model_used=model_used,
            raw_json=parsed,
        )

    # --------------------------------------------------------------------------
    # Synchronous wrapper
    # --------------------------------------------------------------------------

    def analyze_image_sync(
        self,
        image_input: Union[str, bytes, io.BytesIO, Image.Image],
        prompt: Optional[str] = None,
        task: str = "auto",
        model: Optional[str] = None,
        match_db: bool = True,
    ) -> VLMAnalysisResult:
        """Phiên bản đồng bộ cho CLI và synchronous Flask endpoints."""
        try:
            curr_loop = asyncio.get_running_loop()
        except RuntimeError:
            curr_loop = None

        if curr_loop and curr_loop.is_running():
            import nest_asyncio
            nest_asyncio.apply()
            return curr_loop.run_until_complete(
                self.analyze_image(image_input, prompt, task, model, match_db)
            )
        return asyncio.run(
            self.analyze_image(image_input, prompt, task, model, match_db)
        )


# Singleton instance dùng chung toàn hệ thống
vlm_engine = VLMEngine()
