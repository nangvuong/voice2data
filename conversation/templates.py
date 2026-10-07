"""
Bộ Template câu hỏi và phản hồi tự nhiên cho Conversation State.
Sinh câu hỏi điền khuyết tự nhiên theo chuẩn VietGAP,
cung cấp menu lựa chọn 2 hướng cho ảnh (Ghi nhật ký vs Tư vấn kỹ thuật),
và các bản tóm tắt xác nhận lưu CSDL.
"""

from typing import Any, Dict, List, Optional, Tuple
import re

# Bảng các trường bắt buộc theo từng hoạt động (Đồng bộ với app/app.py)
ACTIVITY_REQUIRED_FIELDS: Dict[str, List[Tuple[str, str, List[str]]]] = {
    "spray": [
        ("ten_thuoc_bvtv", "Tên thuốc BVTV", ["material_name", "ten_thuoc", "ten_phan_thuoc", "pesticide_name", "pesticide", "medicine", "brand_name"]),
        ("target_pest", "Đối tượng sâu bệnh", ["sinh_vat_gay_hai", "pest_name", "doi_tuong_phong_tru", "doi_tuong_sau_benh", "pest", "target_pests"]),
        ("nong_do_va_luong_dung", "Nồng độ & liều lượng", ["concentration", "luong_nuoc_pha", "lieu_luong_thuoc", "lieu_luong", "dose_quantity", "dosage", "dose", "luong_dung"]),
        ("thoi_gian_cach_ly", "Thời gian cách ly (ngày)", ["isolation_days", "quarantine_days", "cach_ly", "preharvest_interval", "phi", "isolation"]),
    ],
    "fertilize": [
        ("ten_phan_bon", "Tên phân bón", ["material_name", "ten_phan_thuoc", "fertilizer_name", "fertilizer", "ten_phan"]),
        ("luong_phan_bon", "Lượng phân bón", ["quantity", "luong_su_dung", "so_luong", "fertilizer_amount", "amount"]),
    ],
    "purchase": [
        ("material_name", "Tên vật tư", ["ten_thuoc_bvtv", "ten_phan_bon", "ten_vat_tu", "ten_vat_tu_nhap", "input_name", "item_name", "pesticide_name", "fertilizer_name"]),
        ("quantity", "Số lượng", ["so_luong_vat_tu", "luong_su_dung", "so_luong", "qty"]),
        ("unit", "Đơn vị tính", ["don_vi"]),
    ],
    "harvest": [
        ("crop_name", "Loại nông sản", ["loai_nong_san", "ten_cay_trong", "crop"]),
        ("san_luong_thu_hoach", "Sản lượng thu hoạch", ["quantity", "harvest_quantity", "so_luong", "yield_amount", "yield"]),
    ],
    "sale": [
        ("crop_name", "Loại nông sản", ["loai_nong_san", "ten_cay_trong", "crop"]),
        ("khoi_luong_tieu_thu", "Khối lượng bán", ["khoi_luong_ban", "sale_quantity", "quantity", "so_luong"]),
    ],
    "waste": [
        ("material_name", "Tên thuốc / vật tư", ["ten_vat_tu", "ten_thuoc"]),
        ("treatment_method", "Cách xử lý & nơi lưu trữ", ["cach_xu_ly", "phuong_phap_xu_ly", "storage_location"]),
    ],
    "observation": [
        ("sinh_vat_gay_hai", "Sinh vật gây hại phát hiện", ["target_pest", "pest_name", "doi_tuong_sau_benh", "pest"]),
    ],
    "general": [
        ("cong_viec", "Nội dung công việc", ["task_name", "description", "noi_dung_cong_viec"]),
    ],
}

# Bảng các trường không bắt buộc (optional) cần hỏi thêm khi xác nhận
ACTIVITY_OPTIONAL_FIELDS: Dict[str, List[Tuple[str, str, str]]] = {
    # (slot_key, label, prompt_phrase)
    # LƯU Ý: Các trường weather_condition, growth_stage, dien_tich KHÔNG cần hỏi theo yêu cầu người dùng
    "spray": [
        ("phuong_thuc_phun", "Dụng cụ / phương thức phun", "dụng cụ phun (bình đeo vai, máy bay drone hay máy nén)"),
    ],
    "fertilize": [
        ("phuong_phap_bon", "Phương pháp bón", "phương pháp bón (bón lót, bón thúc, tưới gốc hay rải gốc)"),
    ],
    "purchase": [
        ("supplier_name", "Nơi mua / đại lý", "đại lý nơi mua"),
        ("unit_price", "Đơn giá", "giá mua"),
        ("invoice_photo", "Hóa đơn / chứng từ", "chụp ảnh hóa đơn chứng từ"),
    ],
    "harvest": [
        ("dia_diem_cach_so_che", "Lô thu hoạch & sơ chế", "lô thu hoạch hoặc cách sơ chế"),
        ("chat_luong", "Phân loại chất lượng", "chất lượng nông sản (loại 1, loại 2)"),
    ],
    "sale": [
        ("co_so_thu_mua", "Thương lái / nơi mua", "tên thương lái hoặc đơn vị thu mua"),
        ("unit_price", "Đơn giá", "giá bán"),
    ],
    "observation": [
        ("severity", "Mức độ gây hại", "mức độ gây hại (nhẹ, trung bình hay nặng)"),
        ("bien_phap_xu_ly", "Biện pháp xử lý", "hướng xử lý"),
    ],
    "waste": [
        ("storage_location", "Nơi lưu trữ vỏ bao", "nơi lưu giữ vỏ bao bì"),
    ],
    "general": [],
}


def get_missing_optional_fields(activity_type: str, slots: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """Lấy danh sách các trường không bắt buộc (optional) chưa được cung cấp."""
    canon = (activity_type or "").lower()
    opts = ACTIVITY_OPTIONAL_FIELDS.get(canon, [])
    missing_opts = []
    for key, label, prompt_phrase in opts:
        # Nếu là hóa đơn chứng từ, đã có photo_url hoặc invoice_no coi như đã cung cấp
        if key == "invoice_photo":
            if slots.get("invoice_no") or slots.get("photo_url"):
                continue
            missing_opts.append((key, label, prompt_phrase))
            continue
        val = slots.get(key)
        if val is None or str(val).strip() in ("", "None", "null"):
            missing_opts.append((key, label, prompt_phrase))
    return missing_opts


SLOT_CANONICAL_MAP: Dict[str, str] = {
    # spray
    "pesticide_name": "ten_thuoc_bvtv",
    "pesticide": "ten_thuoc_bvtv",
    "ten_thuoc": "ten_thuoc_bvtv",
    "medicine": "ten_thuoc_bvtv",
    "brand_name": "ten_thuoc_bvtv",
    "dosage": "nong_do_va_luong_dung",
    "dose": "nong_do_va_luong_dung",
    "dose_quantity": "nong_do_va_luong_dung",
    "concentration": "nong_do_va_luong_dung",
    "lieu_luong": "nong_do_va_luong_dung",
    "lieu_luong_thuoc": "nong_do_va_luong_dung",
    "luong_nuoc_pha": "nong_do_va_luong_dung",
    "preharvest_interval": "thoi_gian_cach_ly",
    "phi": "thoi_gian_cach_ly",
    "isolation_days": "thoi_gian_cach_ly",
    "quarantine_days": "thoi_gian_cach_ly",
    "cach_ly": "thoi_gian_cach_ly",
    "pest": "target_pest",
    "pest_name": "target_pest",
    "target_pests": "target_pest",
    "doi_tuong_phong_tru": "target_pest",
    "doi_tuong_sau_benh": "target_pest",
    # fertilize
    "fertilizer_name": "ten_phan_bon",
    "fertilizer": "ten_phan_bon",
    "ten_phan": "ten_phan_bon",
    "fertilizer_amount": "luong_phan_bon",
    "luong_su_dung": "luong_phan_bon",
    "application_method": "phuong_phap_bon",
    "method": "phuong_phap_bon",
    "cach_bon": "phuong_phap_bon",
    # purchase
    "input_name": "material_name",
    "ten_vat_tu": "material_name",
    "ten_vat_tu_nhap": "material_name",
    "item_name": "material_name",
    "qty": "quantity",
    "so_luong_vat_tu": "quantity",
    "don_vi": "unit",
    "supplier": "supplier_name",
    "noi_mua": "supplier_name",
    "nha_cung_cap": "supplier_name",
    "dai_ly": "supplier_name",
    "don_gia": "unit_price",
    "gia_mua": "unit_price",
    # harvest & sale
    "yield_amount": "san_luong_thu_hoach",
    "harvest_quantity": "san_luong_thu_hoach",
    "san_luong": "san_luong_thu_hoach",
    "crop": "crop_name",
    "loai_nong_san": "crop_name",
    "ten_cay_trong": "crop_name",
    "cay_trong": "crop_name",
    "sale_quantity": "khoi_luong_tieu_thu",
    "khoi_luong_ban": "khoi_luong_tieu_thu",
    "san_luong_ban": "khoi_luong_tieu_thu",
    "buyer": "co_so_thu_mua",
    "buyer_name": "co_so_thu_mua",
    "thuong_lai": "co_so_thu_mua",
    "nguoi_mua": "co_so_thu_mua",
    "gia_ban": "unit_price",
    # observation
    "ten_sau_benh": "sinh_vat_gay_hai",
    "muc_do": "severity",
    "cultural_controls": "bien_phap_xu_ly",
    "action_taken": "bien_phap_xu_ly",
    # waste
    "waste_type": "material_name",
    "loai_bao_bi": "material_name",
    "cach_xu_ly": "treatment_method",
    "phuong_phap_xu_ly": "treatment_method",
    "noi_ton_tru": "storage_location",
    "noi_luu_tru": "storage_location",
    # general
    "task_name": "cong_viec",
    "description": "cong_viec",
    "dien_tich": "dien_tich",
    "area": "dien_tich",
    "area_applied": "dien_tich",
    "dien_tich_phun": "dien_tich",
    "dien_tich_bon": "dien_tich",
    "so_goc": "dien_tich",
    "so_cay": "dien_tich",
    "dung_cu_phun": "phuong_thuc_phun",
    "thiet_bi_phun": "phuong_thuc_phun",
    "dung_cu": "phuong_thuc_phun",
    "phuong_thuc": "phuong_thuc_phun",
    "weather": "thoi_tiet",
    "thoi_tiet_phun": "thoi_tiet",
    "growth_stage": "thoi_diem_bon",
    "giai_doan": "thoi_diem_bon",
    "thoi_diem": "thoi_diem_bon",
    "gia_tien": "unit_price",
    "gia": "unit_price",
    "so_tien": "unit_price",
    "price": "unit_price",
    "so_hoa_don": "invoice_no",
    "hoa_don": "invoice_no",
    "chung_tu": "invoice_no",
    "noi_ban": "co_so_thu_mua",
    "chat_luong_nong_san": "chat_luong",
    "so_che": "dia_diem_cach_so_che",
}


def canonicalize_slots(slots: Dict[str, Any], activity_type: Optional[str] = None) -> Dict[str, Any]:
    """Quy chuẩn hóa tên các trường slot về khóa chính thống."""
    if not isinstance(slots, dict):
        return {}
    res = dict(slots)
    act = (activity_type or "").lower()

    for k, v in list(slots.items()):
        if v is None or str(v).strip() in ("", "null", "None"):
            continue
        lk = k.lower().strip()
        canon_k = SLOT_CANONICAL_MAP.get(lk)
        if canon_k and canon_k not in res:
            res[canon_k] = v

        # Xử lý trường hợp đặc biệt: material_name ở spray hoặc fertilize
        if lk == "material_name" or lk == "ten_vat_tu":
            if act == "spray" and "ten_thuoc_bvtv" not in res:
                res["ten_thuoc_bvtv"] = v
            elif act == "fertilize" and "ten_phan_bon" not in res:
                res["ten_phan_bon"] = v

        # Xử lý ngược lại: ten_thuoc_bvtv hoặc ten_phan_bon ở purchase
        if act == "purchase":
            if "material_name" not in res:
                if res.get("ten_thuoc_bvtv"):
                    res["material_name"] = res["ten_thuoc_bvtv"]
                    if "material_type" not in res:
                        res["material_type"] = "Thuốc BVTV"
                elif res.get("ten_phan_bon"):
                    res["material_name"] = res["ten_phan_bon"]
                    if "material_type" not in res:
                        res["material_type"] = "Phân bón"

        # Xử lý waste: map ten_thuoc_bvtv hoặc ten_phan_bon -> material_name
        if act == "waste":
            if "material_name" not in res:
                if res.get("ten_thuoc_bvtv"):
                    res["material_name"] = res["ten_thuoc_bvtv"]
                elif res.get("ten_phan_bon"):
                    res["material_name"] = res["ten_phan_bon"]
            if "waste_type" not in res:
                res["waste_type"] = "Vỏ chai, bao bì thuốc BVTV"
            if res.get("activity_date") and "treatment_date" not in res:
                res["treatment_date"] = res["activity_date"]

        # Xử lý trường hợp đặc biệt: quantity ở fertilize
        if lk == "quantity" and act == "fertilize" and "luong_phan_bon" not in res:
            res["luong_phan_bon"] = v

        # Xử lý observation: map target_pest / pest_name -> sinh_vat_gay_hai
        if act == "observation":
            if "sinh_vat_gay_hai" not in res:
                for k_obs in ["target_pest", "pest_name", "doi_tuong_sau_benh", "pest"]:
                    if res.get(k_obs):
                        res["sinh_vat_gay_hai"] = res[k_obs]
                        break

        # Chuẩn hóa thời gian cách ly thành số nguyên nếu có
        if canon_k == "thoi_gian_cach_ly" or lk == "thoi_gian_cach_ly":
            import re
            m = re.search(r"(\d+)", str(v))
            if m:
                try:
                    res["thoi_gian_cach_ly"] = int(m.group(1))
                except ValueError:
                    pass

    # Đồng bộ ngày thực hiện giữa activity_date và purchase_date / treatment_date
    if act == "purchase":
        if res.get("activity_date") and "purchase_date" not in res:
            res["purchase_date"] = res["activity_date"]
        elif res.get("purchase_date") and "activity_date" not in res:
            res["activity_date"] = res["purchase_date"]
    elif act == "waste":
        if res.get("activity_date") and "treatment_date" not in res:
            res["treatment_date"] = res["activity_date"]
        elif res.get("treatment_date") and "activity_date" not in res:
            res["activity_date"] = res["treatment_date"]
            res["purchase_date"] = res["activity_date"]
        elif res.get("purchase_date") and "activity_date" not in res:
            res["activity_date"] = res["purchase_date"]
    else:
        if res.get("purchase_date") and "activity_date" not in res:
            res["activity_date"] = res["purchase_date"]

    return res

TASK_NAME_DISPLAY: Dict[str, str] = {
    "spray": "Phun thuốc BVTV",
    "fertilize": "Bón phân",
    "purchase": "Mua vật tư nông nghiệp",
    "harvest": "Thu hoạch nông sản",
    "sale": "Bán nông sản",
    "waste": "Xử lý vỏ bao bì thuốc",
    "observation": "Theo dõi sâu bệnh hại",
    "general": "Hoạt động canh tác",
}

CATEGORY_NAMES_VN: Dict[str, str] = {
    "pest_disease": "Sâu bệnh hại cây trồng",
    "pesticide": "Thuốc bảo vệ thực vật",
    "fertilizer": "Phân bón nông nghiệp",
    "field_crop": "Vườn cây / Hiện trường canh tác",
    "invoice_receipt": "Hóa đơn / Chứng từ vật tư",
    "other": "Hình ảnh vật tư / Nông trại",
}


def check_missing_fields(activity_type: str, current_slots: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Kiểm tra danh sách các trường bắt buộc còn thiếu cho một loại hoạt động.
    Trả về danh sách [{'key': ..., 'label': ...}]
    """
    if not activity_type or not isinstance(activity_type, str):
        return []
    canon = activity_type.lower()
    reqs = ACTIVITY_REQUIRED_FIELDS.get(canon, [])
    missing = []

    for req_key, req_label, aliases in reqs:
        val = current_slots.get(req_key)
        if val is None or str(val).strip() in ("", "null", "None"):
            for al in aliases:
                al_val = current_slots.get(al)
                if al_val is not None and str(al_val).strip() not in ("", "null", "None"):
                    val = al_val
                    current_slots[req_key] = al_val
                    break

        # Tự động gán crop_name từ CSDL nếu chưa có hoặc đang là mặc định cũ
        if req_key == "crop_name" and (val is None or not str(val).strip() or str(val).strip() in ("Cà gai leo", "null", "None")):
            try:
                from services.database import get_db_connection, get_crop_name_for_farmer
                conn = get_db_connection()
                val = get_crop_name_for_farmer(conn)
                conn.close()
            except Exception:
                val = "Bưởi diễn"
            current_slots["crop_name"] = val or "Bưởi diễn"

        if val is None or str(val).strip() in ("", "null", "None"):
            missing.append({"key": req_key, "label": req_label})

    return missing


def build_clarification_question(
    activity_type: str,
    current_slots: Dict[str, Any],
    missing_fields: List[Dict[str, Any]],
) -> str:
    """
    Sinh câu hỏi tự nhiên bằng Template cho các trường còn thiếu.
    Hỗ trợ kết hợp nhiều trường để hỏi gộp 1 câu tự nhiên.
    """
    if not activity_type or not isinstance(activity_type, str):
        return ""
    if not missing_fields:
        return build_confirmation_message(activity_type, current_slots)

    missing_keys = {m["key"] for m in missing_fields}
    canon = activity_type.lower()

    # 1. PHUN THUỐC BVTV (spray)
    if canon == "spray":
        ten_thuoc = current_slots.get("ten_thuoc_bvtv") or current_slots.get("pesticide_name") or "thuốc"
        has_pest = "target_pest" not in missing_keys
        has_dose = "nong_do_va_luong_dung" not in missing_keys
        has_iso = "thoi_gian_cach_ly" not in missing_keys

        if "ten_thuoc_bvtv" in missing_keys:
            return "Bác cho em hỏi mình đang phun loại thuốc bảo vệ thực vật tên gì thế ạ?"

        if not has_pest and not has_dose and not has_iso:
            return f"Dạ bác phun thuốc {ten_thuoc} để trị sâu bệnh gì, liều lượng bao nhiêu và cách ly mấy ngày ạ?"
        elif not has_pest and not has_dose:
            return f"Dạ bác phun {ten_thuoc} để trị sâu bệnh gì và liều lượng pha bao nhiêu ạ?"
        elif not has_dose and not has_iso:
            return f"Dạ bác phun {ten_thuoc} với liều lượng bao nhiêu và thời gian cách ly là mấy ngày ạ?"
        elif not has_pest and not has_iso:
            return f"Dạ bác phun {ten_thuoc} trị sâu bệnh gì và thời gian cách ly là mấy ngày ạ?"
        elif not has_pest:
            return f"Dạ thuốc {ten_thuoc} này bác phun để phòng trừ đối tượng sâu bệnh hại nào ạ?"
        elif not has_dose:
            return f"Bác cho em hỏi đợt này mình phun thuốc {ten_thuoc} với liều lượng bao nhiêu (mấy bình hoặc mấy lít nước) ạ?"
        elif not has_iso:
            return f"Dạ thời gian cách ly an toàn của thuốc {ten_thuoc} này là mấy ngày trước khi thu hoạch thế bác?"

    # 2. BÓN PHÂN (fertilize)
    elif canon == "fertilize":
        ten_phan = current_slots.get("ten_phan_bon") or current_slots.get("fertilizer_name") or "phân"
        if "ten_phan_bon" in missing_keys:
            return "Bác cho em hỏi đợt này mình bón loại phân gì thế ạ (NPK, phân chuồng, hữu cơ vi sinh...)?"
        if "luong_phan_bon" in missing_keys:
            return f"Bác bón phân {ten_phan} với khối lượng bao nhiêu (mấy tạ, mấy bao hoặc mấy ký) ạ?"

    # 3. MUA VẬT TƯ (purchase)
    elif canon == "purchase":
        m_name = current_slots.get("material_name") or "vật tư"
        if "material_name" in missing_keys:
            return "Bác vừa nhập hoặc mua loại vật tư nông nghiệp nào thế ạ? Bác có thể nói tên hoặc bấm chụp ảnh vật tư để em lấy thông tin tự động nhé!"
        if "quantity" in missing_keys or "unit" in missing_keys:
            return f"Bác mua {m_name} với số lượng là bao nhiêu và đơn vị tính thế nào (bao, ký, chai, gói...) ạ?"

    # 4. THU HOẠCH (harvest)
    elif canon == "harvest":
        c_name = current_slots.get("crop_name") or "nông sản"
        if "crop_name" in missing_keys:
            return "Bác đang thu hoạch loại cây trồng hay nông sản nào thế ạ?"
        if "san_luong_thu_hoach" in missing_keys:
            return f"Đợt này bác thu hoạch {c_name} được sản lượng bao nhiêu (tạ, tấn, ký) ạ?"

    # 5. BÁN / XUẤT NÔNG SẢN (sale)
    elif canon == "sale":
        c_name = current_slots.get("crop_name") or "nông sản"
        if "khoi_luong_tieu_thu" in missing_keys:
            return f"Bác xuất bán {c_name} với khối lượng bao nhiêu (tấn, tạ hay ký) ạ?"

    # 6. XỬ LÝ BAO BÌ (waste)
    elif canon == "waste":
        if "material_name" in missing_keys:
            return "Vỏ bao bì này là của loại phân bón hay thuốc BVTV nào thế bác?"
        if "treatment_method" in missing_keys:
            return "Vỏ bao bì bác thu gom về bể chứa chuyên dụng hay đem đi tiêu hủy ở đâu ạ?"

    # 7. QUAN SÁT SÂU BỆNH (observation)
    elif canon == "observation":
        if "sinh_vat_gay_hai" in missing_keys:
            return "Bác quan sát thấy loại sâu bệnh hay sinh vật gây hại nào xuất hiện trên vườn thế ạ?"

    # 8. HOẠT ĐỘNG CHUNG (general)
    elif canon == "general":
        if "cong_viec" in missing_keys:
            return "Bác cho em hỏi cụ thể công việc canh tác hôm nay là gì (làm cỏ, xới đất, tỉa cành, tưới nước...) ạ?"

    # Fallback câu hỏi tổng quát
    labels = [m["label"] for m in missing_fields]
    return f"Dạ để hoàn thiện sổ VietGAP, bác bổ sung thêm giúp em thông tin: {', '.join(labels)} nhé!"


def build_confirmation_message(activity_type: str, slots: Dict[str, Any]) -> str:
    """Sinh bản tóm tắt xác nhận đầy đủ, chuẩn quy trình VietGAP."""
    if not activity_type or not isinstance(activity_type, str):
        return ""
    canon = activity_type.lower()
    raw_date = slots.get("activity_date") or slots.get("purchase_date") or slots.get("harvest_date") or slots.get("sale_date") or "Hôm nay"
    
    # Định dạng ngày thân thiện dd/mm/yyyy
    date_display = raw_date
    if re.match(r"^\d{4}-\d{2}-\d{2}$", str(raw_date).strip()):
        y, m, d = raw_date.strip().split("-")
        date_display = f"{d}/{m}/{y}"

    t_name = TASK_NAME_DISPLAY.get(canon, "Ghi chép canh tác")
    crop = slots.get("crop_name") or "Bưởi diễn"

    lines = [
        f"📝 **PHIẾU XÁC NHẬN: {t_name.upper()}**",
        f"📅 **Ngày thực hiện:** {date_display}",
    ]

    if canon == "spray":
        ten_thuoc = slots.get("ten_thuoc_bvtv") or slots.get("pesticide_name") or "Thuốc BVTV"
        target_pest = slots.get("target_pest") or "Sâu bệnh hại"
        dosage = slots.get("nong_do_va_luong_dung") or "Pha theo hướng dẫn"
        iso = slots.get("thoi_gian_cach_ly")
        iso_str = f"{iso} ngày" if str(iso).strip().isdigit() else (str(iso) if iso else "Theo quy định")

        lines.append(f"🌱 **Cây trồng:** {crop}")
        lines.append(f"💊 **Tên thuốc BVTV:** {ten_thuoc}")
        lines.append(f"🎯 **Đối tượng phòng trừ:** {target_pest}")
        lines.append(f"⚖️ **Nồng độ & liều lượng:** {dosage}")
        lines.append(f"⏳ **Thời gian cách ly:** {iso_str}")
        if slots.get("dien_tich"):
            lines.append(f"📐 **Diện tích/Số gốc:** {slots.get('dien_tich')}")
        if slots.get("phuong_thuc_phun"):
            lines.append(f"🚜 **Dụng cụ/Thiết bị phun:** {slots.get('phuong_thuc_phun')}")
        if slots.get("thoi_tiet"):
            lines.append(f"⛅ **Thời tiết:** {slots.get('thoi_tiet')}")

    elif canon == "fertilize":
        ten_phan = slots.get("ten_phan_bon") or slots.get("fertilizer_name") or "Phân bón"
        luong_phan = slots.get("luong_phan_bon") or "Đủ liều lượng"
        lines.append(f"🌱 **Cây trồng:** {crop}")
        lines.append(f"🌾 **Tên phân bón:** {ten_phan}")
        lines.append(f"⚖️ **Khối lượng:** {luong_phan}")
        if slots.get("phuong_phap_bon"):
            lines.append(f"🚜 **Phương pháp bón:** {slots.get('phuong_phap_bon')}")
        if slots.get("dien_tich"):
            lines.append(f"📐 **Diện tích/Số cây:** {slots.get('dien_tich')}")
        if slots.get("thoi_diem_bon"):
            lines.append(f"🌿 **Giai đoạn/Thời điểm:** {slots.get('thoi_diem_bon')}")

    elif canon == "purchase":
        m_name = slots.get("material_name") or "Vật tư nông nghiệp"
        qty = slots.get("quantity") or 1
        unit = slots.get("unit") or ""
        lines.append(f"📦 **Tên vật tư:** {m_name}")
        lines.append(f"⚖️ **Số lượng:** {qty} {unit}".strip())
        if slots.get("supplier_name"):
            lines.append(f"🏪 **Nơi mua / Đại lý:** {slots.get('supplier_name')}")
        if slots.get("unit_price"):
            lines.append(f"💰 **Đơn giá:** {slots.get('unit_price')}")
        if slots.get("invoice_no"):
            lines.append(f"🧾 **Số hóa đơn:** {slots.get('invoice_no')}")

    elif canon == "harvest":
        san_luong = slots.get("san_luong_thu_hoach") or slots.get("quantity") or "Theo thực tế"
        lines.append(f"🌱 **Cây trồng / Nông sản:** {crop}")
        lines.append(f"⚖️ **Sản lượng thu hoạch:** {san_luong}")
        if slots.get("dia_diem_cach_so_che"):
            lines.append(f"📍 **Lô thu hoạch / Sơ chế:** {slots.get('dia_diem_cach_so_che')}")
        if slots.get("chat_luong"):
            lines.append(f"✨ **Chất lượng:** {slots.get('chat_luong')}")

    elif canon == "sale":
        khoi_luong = slots.get("khoi_luong_tieu_thu") or slots.get("quantity") or "Theo thực tế"
        buyer = slots.get("co_so_thu_mua") or slots.get("buyer_name") or "Thương lái / Hợp tác xã"
        lines.append(f"🌱 **Nông sản xuất bán:** {crop}")
        lines.append(f"⚖️ **Khối lượng tiêu thụ:** {khoi_luong}")
        lines.append(f"🤝 **Cơ sở thu mua / Khách:** {buyer}")
        if slots.get("unit_price"):
            lines.append(f"💰 **Đơn giá:** {slots.get('unit_price')}")

    elif canon == "observation":
        sb = slots.get("sinh_vat_gay_hai") or slots.get("target_pest") or "Sinh vật gây hại"
        sev = slots.get("severity") or "Cần theo dõi"
        action = slots.get("bien_phap_xu_ly") or slots.get("action_taken") or ""
        lines.append(f"🌱 **Cây trồng theo dõi:** {crop}")
        lines.append(f"🐛 **Sinh vật gây hại:** {sb}")
        lines.append(f"⚠️ **Mức độ xuất hiện:** {sev}")
        if action:
            lines.append(f"🛡️ **Biện pháp xử lý:** {action}")

    elif canon == "waste":
        vt = slots.get("material_name") or "Vỏ bao bì thuốc BVTV"
        method = slots.get("treatment_method") or "Thu gom vào bể chứa chuyên dụng"
        lines.append(f"📦 **Vật tư / Vỏ bao bì:** {vt}")
        lines.append(f"♻️ **Phương pháp xử lý:** {method}")
        if slots.get("storage_location"):
            lines.append(f"📍 **Nơi lưu trữ:** {slots.get('storage_location')}")

    else:
        task = slots.get("cong_viec") or slots.get("description") or "Chăm sóc vườn cây"
        lines.append(f"🌱 **Cây trồng:** {crop}")
        lines.append(f"⚒️ **Nội dung công việc:** {task}")
        if slots.get("dien_tich"):
            lines.append(f"📐 **Khu vực/Diện tích:** {slots.get('dien_tich')}")

    missing_opts = get_missing_optional_fields(canon, slots)
    if canon == "purchase":
        extra_hints = []
        if not slots.get("supplier_name"):
            extra_hints.append("nơi mua / đại lý")
        if not slots.get("unit_price"):
            extra_hints.append("giá mua")

        hint_lines = []
        if extra_hints:
            hint_lines.append(f"💡 *Bác có muốn ghi thêm **{' hay '.join(extra_hints)}** không ạ?*")
        if not slots.get("invoice_no") and not slots.get("photo_url"):
            hint_lines.append("📸 *Bác có thể chụp ảnh bao bì vật tư hoặc hóa đơn bằng nút bên dưới để lấy thông tin tự động nhé!*")
        hint_lines.append("👉 **Bác có thể nói thêm, chụp ảnh vật tư/hóa đơn, hoặc bấm ĐỒNG Ý LƯU VÀO SỔ nếu thông tin đã đủ nhé!**")
        lines.append("\n" + "\n".join(hint_lines))
    elif missing_opts:
        hints = " hay ".join([opt[2] for opt in missing_opts[:2]])
        lines.append(f"\n💡 *Bác có muốn ghi thêm **{hints}** không ạ?*")
        lines.append("👉 **Bác có thể nói thêm, hoặc bấm ĐỒNG Ý LƯU VÀO SỔ nếu thông tin đã đủ nhé!**")
    else:
        lines.append("\n👉 **Thông tin đã rất đầy đủ! Bác kiểm tra lại rồi bấm ĐỒNG Ý LƯU VÀO SỔ nhé!**")

    return "\n".join(lines)


def build_image_choice_prompt(analysis_dict: Dict[str, Any]) -> str:
    """
    Sinh câu hỏi lựa chọn 2 hướng khi người dùng gửi ảnh:
    [1] Ghi nhật ký VietGAP
    [2] Trả lời / Tư vấn thông tin thuốc, phân, sâu bệnh
    """
    cat = analysis_dict.get("category", "other")
    cat_vn = CATEGORY_NAMES_VN.get(cat, "Vật tư nông nghiệp")
    title = analysis_dict.get("title") or "Hình ảnh nông nghiệp"

    return (
        f"📸 Em đã nhận diện được ảnh: **{title}** ({cat_vn}).\n\n"
        f"Bác muốn thực hiện theo hướng nào ạ?\n"
        f"👉 [1] **Ghi vào sổ nhật ký VietGAP** (Tự động điền biểu mẫu canh tác)\n"
        f"👉 [2] **Tư vấn / Tra cứu thông tin** (Công dụng, liều dùng, an toàn, cách phòng trừ sâu bệnh)\n\n"
        f"*(Bác có thể nói hoặc gõ: '1', 'ghi nhật ký', '2', 'tư vấn')*"
    )


def build_lead_and_sections(analysis_dict: Dict[str, Any], prompt_for_diary: bool = True) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Tạo cấu trúc tinh gọn theo yêu cầu:
    1. Lead info:
       - Nếu là thuốc BVTV: Thuốc gì (tên thuốc) + Tác dụng (đối tượng phòng trừ)
       - Nếu là sâu bệnh hại: Bệnh gì (tên bệnh) + Triệu chứng (dấu hiệu nhận biết)
       - Nếu là phân bón: Phân bón gì (tên phân) + Tác dụng (công dụng chính)
    2. Sections: Danh sách các phần tiếp theo, mỗi nút là 1 CÂU HỎI CỦA NGƯỜI DÂN và có spoken_text để đọc to.
    """
    cat = analysis_dict.get("category", "other")
    title = analysis_dict.get("title", "")
    summary = analysis_dict.get("summary", "")

    # 1. SÂU BỆNH HẠI
    if cat == "pest_disease" or analysis_dict.get("pest_disease"):
        p = analysis_dict.get("pest_disease") or {}
        pest_name = p.get("pest_name") or title or "Sâu bệnh hại"
        crop_name = p.get("crop_name") or "Cây trồng"
        symptoms = p.get("symptoms") or summary or "Xuất hiện dấu hiệu sâu bệnh hại trên cây trồng"
        severity = p.get("severity") or "Cần theo dõi"
        sci = f" ({p.get('scientific_name')})" if p.get("scientific_name") else ""

        lead_info = {
            "type": "pest_disease",
            "type_label": "Chẩn đoán Sâu Bệnh Hại",
            "item_label": "Bệnh chẩn đoán",
            "item_name": f"{pest_name}{sci}",
            "crop_name": crop_name,
            "core_label": "Triệu chứng",
            "core_val": symptoms,
            "sub_info": f"Cây trồng: {crop_name} | Mức độ: {severity}",
        }

        sections = []
        # Nút 1: Biện pháp phòng trừ
        controls = p.get("cultural_controls") or p.get("biological_controls") or "Cắt tỉa cành thông thoáng, thu gom lá bệnh và tiêu hủy sạch sẽ."
        if p.get("cultural_controls") and p.get("biological_controls"):
            controls = f"Canh tác: {p.get('cultural_controls')}\nSinh học: {p.get('biological_controls')}"
        sections.append({
            "id": "control",
            "question": "Phòng trừ sâu bệnh này như thế nào?",
            "label": "Phòng trừ sâu bệnh này như thế nào?",
            "icon": "shield",
            "title": "Biện pháp phòng trừ & Canh tác",
            "content": controls,
            "spoken_text": f"Về biện pháp phòng trừ: {controls}",
        })

        # Nút 2: Thuốc đặc trị đề xuất
        actives = p.get("recommended_actives") or []
        actives_str = ", ".join(actives) if isinstance(actives, list) else str(actives)
        matched = p.get("matched_farm_pesticides") or []
        stock_text = ""
        stock_spoken = ""
        if matched:
            names = ", ".join([f"{m.get('name')}" for m in matched[:2]])
            stock_text = f"\nCó sẵn trong kho trang trại: {names}."
            stock_spoken = f" Trong kho nhà mình đang có sẵn {names}."
        med_content = f"Hoạt chất khuyến nghị: {actives_str or 'Theo khuyến cáo kỹ thuật'}.{stock_text}"
        sections.append({
            "id": "medicines",
            "question": "Dùng thuốc gì để trị dứt điểm?",
            "label": "Dùng thuốc gì để trị dứt điểm?",
            "icon": "pill",
            "title": "Thuốc & Hoạt chất đặc trị",
            "content": med_content.strip(),
            "spoken_text": f"Để trị dứt điểm, bác có thể dùng các hoạt chất: {actives_str or 'theo danh mục kỹ thuật'}.{stock_spoken}",
        })

        # Nút 3: Mức độ gây hại
        sections.append({
            "id": "severity",
            "question": "Sâu bệnh này gây hại có nặng không?",
            "label": "Sâu bệnh này gây hại có nặng không?",
            "icon": "alert",
            "title": "Mức độ & Bộ phận bị hại",
            "content": f"Mức độ: {severity}\nBộ phận bị hại: {p.get('affected_part') or 'Lá, thân, cành'}\nĐiều kiện phát sinh: {p.get('causes_and_conditions') or 'Thời tiết ẩm độ cao, mưa ẩm kéo dài'}",
            "spoken_text": f"Sâu bệnh này ở mức độ {severity}, gây hại chủ yếu ở {p.get('affected_part') or 'lá và cành cây'}.",
        })

        # Nút 4: Ghi sổ theo dõi sâu bệnh
        if prompt_for_diary:
            sections.append({
                "id": "diary",
                "question": "Ghi vào sổ theo dõi sâu bệnh giúp tôi",
                "label": "Ghi vào sổ theo dõi sâu bệnh giúp tôi",
                "icon": "fileEdit",
                "is_action": True,
                "action_prompt": "Ghi vào sổ theo dõi sâu bệnh giúp tôi.",
            })

        return lead_info, sections

    # 2. THUỐC BVTV
    elif cat == "pesticide" or analysis_dict.get("pesticide"):
        pest = analysis_dict.get("pesticide") or {}
        trade_name = pest.get("trade_name") or pest.get("brand_name") or title or "Thuốc BVTV"
        act = pest.get("active_ingredient") or pest.get("active_ingredients") or ""

        # Kiểm tra hoạt chất cấm qua safety_checker
        from services.rag.safety_checker import safety_checker
        banned_check = safety_checker.check_banned_substance(f"{trade_name} {act}")
        is_banned = bool(pest.get("is_banned") or banned_check.is_banned)
        banned_info = pest.get("banned_info") or (banned_check.to_dict() if banned_check.is_banned else None)

        if is_banned and banned_info:
            chem_name = banned_info.get("matched_name") or act
            hazard = banned_info.get("hazard") or "Rất độc hại, tồn lưu lâu dài trong môi trường và nông sản."
            legal_source = banned_info.get("source_legal") or "Thông tư số 75/BNNMT & Luật BVTV số 41/2013"

            lead_info = {
                "type": "pesticide_banned",
                "type_label": "⛔ THUỐC CẤM SỬ DỤNG TẠI VIỆT NAM (VI PHẠM VIETGAP)",
                "item_label": "⛔ Thuốc CẤM phát hiện",
                "item_name": trade_name,
                "core_label": "Tình trạng pháp lý",
                "core_val": f"ĐÃ BỊ CẤM SỬ DỤNG theo {legal_source}",
                "sub_info": f"Hoạt chất cấm: {chem_name}",
            }

            sections = []
            # Nút 1: Mức độ nguy hại & Lý do cấm
            sections.append({
                "id": "banned_hazard",
                "question": "Tại sao thuốc này bị cấm và nguy hại ra sao?",
                "label": "Tại sao thuốc này bị cấm và nguy hại ra sao?",
                "icon": "alertTriangle",
                "title": "Mức độ nguy hại & Lý do cấm sử dụng",
                "content": f"Theo {legal_source}:\n- **Hoạt chất chính:** {chem_name}\n- **Phân nhóm:** {banned_info.get('group', 'Thuốc BVTV')}\n- **Tác hại nghiêm trọng:** {hazard}\n- **Quy chuẩn VietGAP:** Tuyệt đối cấm sử dụng, lưu trữ trong trang trại. Vi phạm sẽ bị tước chứng nhận VietGAP.",
                "spoken_text": f"Bác ơi, thuốc này đã bị cấm sử dụng tại Việt Nam vì rất độc hại. {hazard}",
            })

            # Nút 2: Chế phẩm sinh học an toàn thay thế
            alts = banned_info.get("safe_alternatives") or []
            if not alts:
                alts = safety_checker.check_banned_substance(trade_name).safe_alternatives
            alts_lines = [f"- **{a['name']}:** {a['desc']}" for a in alts[:3]]
            sections.append({
                "id": "safe_alternatives",
                "question": "Có chế phẩm sinh học nào an toàn thay thế không?",
                "label": "Có chế phẩm sinh học nào an toàn thay thế không?",
                "icon": "shieldCheck",
                "title": "Giải pháp sinh học thay thế theo chuẩn VietGAP",
                "content": "Để phòng trừ sâu bệnh đạt chuẩn VietGAP mà không dùng thuốc cấm, bác nên chuyển sang:\n" + ("\n".join(alts_lines) if alts_lines else "- Ưu tiên chế phẩm vi sinh Bt, nấm xanh nấm trắng hoặc dầu khoáng nông nghiệp."),
                "spoken_text": "Bác nên chuyển sang dùng các chế phẩm sinh học như vi sinh Bt, nấm xanh nấm trắng hoặc dầu khoáng nông nghiệp nhé!",
            })

            # Nút 3: Thông báo từ chối ghi nhật ký
            sections.append({
                "id": "banned_refusal",
                "question": "Thuốc này có ghi vào nhật ký VietGAP được không?",
                "label": "Thuốc này có ghi vào nhật ký VietGAP được không?",
                "icon": "xCircle",
                "title": "Từ chối ghi nhật ký VietGAP",
                "content": "⛔ **HỆ THỐNG TỪ CHỐI GHI NHẬT KÝ VỚI THUỐC CẤM:**\nTiêu chuẩn VietGAP yêu cầu 100% thuốc bảo vệ thực vật phải nằm trong danh mục được phép sử dụng tại Việt Nam. Sử dụng thuốc cấm sẽ gây nguy hiểm nghiêm trọng và bị đình chỉ chứng nhận VietGAP.",
                "spoken_text": "Thuốc này nằm trong danh mục cấm nên hệ thống không cho phép ghi vào sổ nhật ký VietGAP đâu bác nhé!",
            })

            return lead_info, sections

        target_pests = pest.get("target_pests") or []
        if isinstance(target_pests, list):
            target_str = ", ".join(target_pests) if target_pests else (summary or "Phòng trừ sâu bệnh hại cây trồng")
        else:
            target_str = str(target_pests)

        lead_info = {
            "type": "pesticide",
            "type_label": "Thuốc Bảo Vệ Thực Vật",
            "item_label": "Thuốc phát hiện",
            "item_name": trade_name,
            "core_label": "Tác dụng",
            "core_val": target_str,
            "sub_info": f"Hoạt chất: {act}" if act else "",
        }

        sections = []
        # Nút 1: Liều lượng & Cách dùng
        dosage = pest.get("dosage_and_usage") or pest.get("dosage_instructions") or "Pha theo hướng dẫn ghi trên bao bì."
        registered_crops = pest.get("registered_crops")
        crops_str = ""
        if registered_crops:
            crops_str = f"\nCây trồng đăng ký: {', '.join(registered_crops) if isinstance(registered_crops, list) else registered_crops}"
        sections.append({
            "id": "dosage",
            "question": "Thuốc này pha liều lượng thế nào?",
            "label": "Thuốc này pha liều lượng thế nào?",
            "icon": "scale",
            "title": "Liều lượng pha & Cách phun",
            "content": f"{dosage}{crops_str}",
            "spoken_text": f"Về liều lượng và cách dùng: {dosage}",
        })

        # Nút 2: Thời gian cách ly & An toàn
        iso = pest.get("isolation_days")
        iso_str = f"Thời gian cách ly (PHI): {iso} ngày." if iso is not None else "Thời gian cách ly: Theo hướng dẫn bao bì."
        safety = pest.get("safety_precautions") or pest.get("safety_warnings") or "Đeo khẩu trang, găng tay khi pha và phun thuốc. Không phun ngược chiều gió."
        sections.append({
            "id": "safety",
            "question": "Bao lâu thì được thu hoạch và có độc không?",
            "label": "Bao lâu thì được thu hoạch và có độc không?",
            "icon": "clock",
            "title": "Thời gian cách ly & Lưu ý an toàn",
            "content": f"{iso_str}\nAn toàn lao động: {safety}",
            "spoken_text": f"{iso_str} Bác lưu ý an toàn: {safety}",
        })

        # Nút 3: Thuốc trong kho CSDL (nếu có đối soát)
        db = pest.get("db_match")
        if db and db.get("matched"):
            sections.append({
                "id": "stock",
                "question": "Kho nhà mình có sẵn loại thuốc này không?",
                "label": "Kho nhà mình có sẵn loại thuốc này không?",
                "icon": "checkCircle",
                "title": "Thuốc có trong kho",
                "content": f"Có sẵn trong kho: {db.get('name')}\nLiều lượng chuẩn: {db.get('rag_knowledge', {}).get('lieu_luong_nong_do', 'Theo danh mục')}.",
                "spoken_text": f"Dạ trong kho trang trại có sẵn thuốc {db.get('name')}.",
            })

        # Nút 4: Ghi nhật ký Phun thuốc
        if prompt_for_diary:
            sections.append({
                "id": "diary",
                "question": "Ghi việc phun thuốc này vào nhật ký giúp tôi",
                "label": "Ghi việc phun thuốc này vào nhật ký giúp tôi",
                "icon": "fileEdit",
                "is_action": True,
                "action_prompt": "Ghi việc phun thuốc này vào nhật ký giúp tôi.",
            })

        return lead_info, sections

    # 3. PHÂN BÓN
    elif cat == "fertilizer" or analysis_dict.get("fertilizer"):
        f = analysis_dict.get("fertilizer") or {}
        trade_name = f.get("trade_name") or f.get("fertilizer_name") or title or "Phân bón nông nghiệp"
        benefits = f.get("benefits") or []
        if isinstance(benefits, list):
            ben_str = ", ".join(benefits) if benefits else (summary or "Cung cấp dinh dưỡng cho cây trồng")
        else:
            ben_str = str(benefits)
        npk = f.get("composition_npk") or f.get("nutrients") or ""

        lead_info = {
            "type": "fertilizer",
            "type_label": "Phân Bón Nông Nghiệp",
            "item_label": "Phân bón phát hiện",
            "item_name": trade_name,
            "core_label": "Tác dụng",
            "core_val": ben_str,
            "sub_info": f"NPK: {npk}" if npk else "",
        }

        sections = []
        dosage = f.get("dosage_and_usage") or f.get("usage_instructions") or "Bón theo quy trình kỹ thuật canh tác."
        sections.append({
            "id": "dosage",
            "question": "Phân này bón liều lượng thế nào?",
            "label": "Phân này bón liều lượng thế nào?",
            "icon": "scale",
            "title": "Liều lượng & Phương pháp bón",
            "content": dosage,
            "spoken_text": f"Về liều lượng và cách bón: {dosage}",
        })
        if npk or f.get("fertilizer_type"):
            sections.append({
                "id": "nutrients",
                "question": "Phân này có những chất dinh dưỡng gì?",
                "label": "Phân này có những chất dinh dưỡng gì?",
                "icon": "sparkles",
                "title": "Thành phần dinh dưỡng / NPK",
                "content": f"Thành phần: {npk or 'Xem bao bì'}\nLoại phân: {f.get('fertilizer_type', 'Vật tư canh tác')}.",
                "spoken_text": f"Thành phần dinh dưỡng chính gồm: {npk or 'các chất khoáng thiết yếu cho cây'}.",
            })
        if prompt_for_diary:
            sections.append({
                "id": "diary",
                "question": "Ghi việc bón phân này vào nhật ký giúp tôi",
                "label": "Ghi việc bón phân này vào nhật ký giúp tôi",
                "icon": "fileEdit",
                "is_action": True,
                "action_prompt": "Ghi việc bón phân này vào nhật ký giúp tôi.",
            })
        return lead_info, sections

    # 4. KHÁC / HÓA ĐƠN
    lead_info = {
        "type": "other",
        "type_label": "Hình Ảnh Canh Tác",
        "item_label": "Đối tượng phát hiện",
        "item_name": title or "Hình ảnh thực địa",
        "core_label": "Thông tin",
        "core_val": summary or "Đã ghi nhận hình ảnh thực địa.",
        "sub_info": "",
    }
    sections = []
    if prompt_for_diary:
        sections.append({
            "id": "diary",
            "question": "Ghi thông tin này vào nhật ký giúp tôi",
            "label": "Ghi thông tin này vào nhật ký giúp tôi",
            "icon": "fileEdit",
            "is_action": True,
            "action_prompt": "Ghi nhận thông tin này vào nhật ký giúp tôi.",
        })
    return lead_info, sections


def build_advisory_response(analysis_dict: Dict[str, Any], prompt_for_diary: bool = True) -> str:
    """
    Sinh nội dung trả lời súc tích, tinh gọn theo đúng yêu cầu:
    - Ngay đầu chỉ nêu: Thuốc gì + tác dụng, hoặc Bệnh gì + triệu chứng.
    - Kèm các nút chuyển hướng xem thông tin tiếp theo hoặc ghi nhật ký.
    """
    lead_info, _ = build_lead_and_sections(analysis_dict, prompt_for_diary)

    item_label = lead_info["item_label"]
    item_name = lead_info["item_name"]
    core_label = lead_info["core_label"]
    core_val = lead_info["core_val"]
    sub_info = lead_info.get("sub_info", "")

    if lead_info.get("type") == "pesticide_banned":
        lines = [
            f"⛔ **CẢNH BÁO PHÁP LÝ VIETGAP: THUỐC ĐÃ BỊ CẤM SỬ DỤNG!**",
            f"🌿 **{item_label}:** **{item_name}**" + (f" *({sub_info})*" if sub_info else ""),
            f"🚨 **{core_label}:** {core_val}",
            f"\n💡 *Bác chạm các câu hỏi bên dưới để xem chi tiết mức độ nguy hại và các giải pháp sinh học thay thế an toàn nhé!*"
        ]
        return "\n".join(lines)

    lines = [
        f"🌿 **{item_label}:** **{item_name}**" + (f" *({sub_info})*" if sub_info else ""),
        f"🎯 **{core_label}:** {core_val}",
    ]
    if prompt_for_diary:
        lines.append("\n💡 *Bác chạm các câu hỏi bên dưới để nghe hướng dẫn hoặc ghi vào sổ nhật ký VietGAP nhé!*")

    return "\n".join(lines)


def classify_material_category(material_name: str, material_type: Optional[str] = None) -> str:
    """Xác định vật tư là 'pesticide' (thuốc BVTV) hay 'fertilizer' (phân bón) hay 'other'."""
    m_type_lower = (material_type or "").lower()
    if any(w in m_type_lower for w in ["thuốc", "bvtv", "trừ sâu", "trừ bệnh", "trừ cỏ"]):
        return "pesticide"
    if any(w in m_type_lower for w in ["phân", "dinh dưỡng", "hữu cơ", "npk"]):
        return "fertilizer"

    name_lower = (material_name or "").lower()
    pesticide_keywords = [
        "thuốc", "bvtv", "trừ sâu", "trừ bệnh", "trừ cỏ", "diệt", "anvil", "antracol", "radiant",
        "ridomil", "regent", "amistar", "tilt", "score", "confidor", "aliette", "coc 85", "bordeaux",
        "booc đô", "kasumin", "validacin", "actara", "movento", "chess", "match", "proclaim", "60sc", "70wp", "sc", "wp", "wg", "ec", "sl"
    ]
    if any(k in name_lower for k in pesticide_keywords):
        return "pesticide"

    fertilizer_keywords = [
        "phân", "npk", "đạm", "lân", "kali", "ure", "urê", "dap", "đầu trâu", "vi sinh",
        "hữu cơ", "phân bò", "phân gà", "phân chuồng", "trùn quế", "humic", "vôi", "lân nung chảy"
    ]
    if any(k in name_lower for k in fertilizer_keywords):
        return "fertilizer"

    try:
        from services.vietgap.material_matcher import material_matcher
        p_res = material_matcher.fuzzy_match(material_name, category_hint="pesticide")
        f_res = material_matcher.fuzzy_match(material_name, category_hint="fertilizer")
        p_score = p_res.confidence if p_res and p_res.matched else 0.0
        f_score = f_res.confidence if f_res and f_res.matched else 0.0
        if p_score >= 0.55 and p_score >= f_score:
            return "pesticide"
        if f_score >= 0.55 and f_score > p_score:
            return "fertilizer"
    except Exception:
        pass

    return "other"


def build_success_message(
    activity_type: str,
    record_id: Any,
    slots: Optional[Dict[str, Any]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Sinh thông báo lưu sổ nhật ký thành công kèm danh sách nút hành động gợi ý chuyển tiếp luồng."""
    canon = activity_type.lower()
    s = slots or {}
    crop = s.get("crop_name") or "cây trồng"

    if canon == "purchase":
        mat_name = s.get("material_name") or "vật tư"
        qty = s.get("quantity") or ""
        unit = s.get("unit") or ""
        qty_str = f"{qty} {unit}".strip()
        mat_cat = classify_material_category(mat_name, s.get("material_type"))

        if mat_cat == "pesticide":
            text = (
                f"✅ **ĐÃ LƯU THÀNH CÔNG VÀO SỔ VẬT TƯ!**\n"
                f"Đã ghi nhận mua {qty_str + ' ' if qty_str else ''}thuốc BVTV **{mat_name}**.\n\n"
                f"💡 *Bác có muốn chuyển sang ghi việc đem thuốc này đi phun luôn không?*"
            )
            actions = [
                {
                    "id": "switch_to_spray",
                    "label": f"Phun thuốc {mat_name} luôn",
                    "prompt": f"Ghi việc phun thuốc {mat_name} luôn",
                    "icon": "spray",
                }
            ]
            return text, actions
        elif mat_cat == "fertilizer":
            text = (
                f"✅ **ĐÃ LƯU THÀNH CÔNG VÀO SỔ VẬT TƯ!**\n"
                f"Đã ghi nhận mua {qty_str + ' ' if qty_str else ''}phân bón **{mat_name}**.\n\n"
                f"💡 *Bác có muốn chuyển sang ghi việc đem phân này đi bón luôn không?*"
            )
            actions = [
                {
                    "id": "switch_to_fertilize",
                    "label": f"Bón phân {mat_name} luôn",
                    "prompt": f"Ghi việc bón phân {mat_name} luôn",
                    "icon": "fertilize",
                }
            ]
            return text, actions
        else:
            text = (
                f"✅ **ĐÃ LƯU THÀNH CÔNG VÀO SỔ VẬT TƯ!**\n"
                f"Đã ghi nhận mua {qty_str + ' ' if qty_str else ''}vật tư **{mat_name}**.\n\n"
                f"Bác có thể tiếp tục ghi chép công việc tiếp theo hoặc gửi ảnh mới ạ!"
            )
            return text, []

    elif canon == "spray":
        med_name = s.get("ten_thuoc_bvtv") or s.get("pesticide_name") or "thuốc BVTV"
        text = (
            f"✅ **ĐÃ LƯU THÀNH CÔNG VÀO SỔ PHUN THUỐC BVTV!**\n"
            f"Đã ghi nhận phun thuốc **{med_name}** cho {crop}.\n\n"
            f"💡 *Theo quy định VietGAP, sau khi phun xong bác nên thu gom vỏ bao bì vào bể chứa chuyên dụng. Bác có muốn ghi chép xử lý vỏ bao bì hay mua vật tư thuốc **{med_name}** không?*"
        )
        actions = [
            {
                "id": "switch_to_waste",
                "label": "Ghi xử lý vỏ bao bì",
                "prompt": f"Ghi xử lý vỏ bao bì thuốc {med_name}",
                "icon": "waste",
            },
            {
                "id": "switch_to_purchase",
                "label": f"Ghi mua thuốc {med_name}",
                "prompt": f"Ghi việc mua thuốc {med_name} vào sổ vật tư",
                "icon": "purchase",
            },
        ]
        return text, actions

    elif canon == "waste":
        vt = s.get("material_name") or "thuốc BVTV"
        text = (
            f"✅ **ĐÃ LƯU THÀNH CÔNG VÀO SỔ XỬ LÝ VỎ BAO BÌ!**\n"
            f"Đã ghi nhận thu gom và xử lý vỏ bao bì thuốc **{vt}** theo quy chuẩn VietGAP.\n\n"
            f"Bác có thể tiếp tục ghi chép công việc tiếp theo hoặc gửi ảnh mới ạ!"
        )
        return text, []

    elif canon == "fertilize":
        fert_name = s.get("ten_phan_bon") or s.get("fertilizer_name") or "phân bón"
        text = (
            f"✅ **ĐÃ LƯU THÀNH CÔNG VÀO SỔ BÓN PHÂN!**\n"
            f"Đã ghi nhận bón phân **{fert_name}** cho {crop}.\n\n"
            f"💡 *Bác có muốn ghi chép việc mua loại phân bón này vào sổ vật tư không?*"
        )
        actions = [
            {
                "id": "switch_to_purchase",
                "label": f"Ghi mua phân {fert_name}",
                "prompt": f"Ghi việc mua phân bón {fert_name} vào sổ vật tư",
                "icon": "purchase",
            }
        ]
        return text, actions

    else:
        t_name = TASK_NAME_DISPLAY.get(canon, "công việc")
        text = (
            f"✅ **ĐÃ LƯU THÀNH CÔNG VÀO SỔ NHẬT KÝ!**\n"
            f"Đã ghi nhận công việc {t_name}.\n"
            f"Bác có thể tiếp tục ghi chép công việc tiếp theo hoặc gửi ảnh mới ạ!"
        )
        return text, []


def build_cancel_message() -> str:
    """Sinh thông báo hủy phiên."""
    return "👌 Dạ em đã hủy phiên ghi chép này. Dữ liệu chưa lưu sẽ được bỏ qua. Khi nào cần ghi tiếp bác cứ gọi em nhé!"


def build_tts_spoken_text(
    text: str,
    analysis_dict: Optional[Dict[str, Any]] = None,
    dialog_state: Optional[str] = None,
    activity_type: Optional[str] = None,
    slots: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Tạo câu nói tự nhiên, ngắn gọn và súc tích dành riêng cho giọng đọc TTS Piper VITS.
    - Nếu đang ở trạng thái CONFIRMING: Đọc câu xác nhận tự nhiên, ấm áp để bác nông dân nghe rõ.
    - Nếu đang ở trạng thái COMMITTED: Đọc thông báo lưu thành công.
    - Nếu có analysis_dict và đang ở ADVISORY: Đọc câu mở đầu nhận diện ảnh ngắn gọn.
    - Ngược lại: Làm sạch văn bản để đọc mượt mà.
    """
    state_str = str(dialog_state or "").upper()

    # 1. Trạng thái xác nhận (CONFIRMING)
    if state_str == "CONFIRMING" or "PHIẾU XÁC NHẬN" in text or "chuẩn xác chưa" in text:
        act = (activity_type or "").lower()
        s = slots or {}
        if not act or act == "general":
            if s.get("ten_thuoc_bvtv") or s.get("pesticide_name"):
                act = "spray"
            elif s.get("ten_phan_bon") or s.get("fertilizer_name"):
                act = "fertilize"
            elif s.get("san_luong_thu_hoach"):
                act = "harvest"
            elif s.get("khoi_luong_tieu_thu"):
                act = "sale"
            elif s.get("sinh_vat_gay_hai"):
                act = "observation"
            else:
                act = "general"

        crop = s.get("crop_name") or "cây bưởi"
        missing_opts = get_missing_optional_fields(act, s)
        hint_spoken = ""
        if act == "purchase":
            if not s.get("invoice_no") and not s.get("photo_url"):
                hint_spoken = " Bác có thể chụp ảnh bao bì vật tư hoặc hóa đơn để lấy thông tin tự động, hoặc xem phiếu đã chuẩn chưa để em lưu vào sổ nhé!"
            elif missing_opts:
                spoken_labels = [opt[1].split("/")[0].strip().lower() for opt in missing_opts[:2] if opt[0] != "invoice_photo"]
                if spoken_labels:
                    hint_phrase = " hoặc ".join(spoken_labels)
                    hint_spoken = f" Bác có muốn bổ sung thêm {hint_phrase} không, hay để em lưu vào sổ luôn ạ?"
                else:
                    hint_spoken = " Bác xem thông tin đã chuẩn xác chưa để em lưu vào sổ nhật ký nhé!"
            else:
                hint_spoken = " Bác xem thông tin đã chuẩn xác chưa để em lưu vào sổ nhật ký nhé!"
        elif missing_opts:
            spoken_labels = [opt[1].split("/")[0].strip().lower() for opt in missing_opts[:2] if opt[0] != "invoice_photo"]
            if spoken_labels:
                hint_phrase = " hoặc ".join(spoken_labels)
                hint_spoken = f" Bác có muốn bổ sung thêm {hint_phrase} không, hay để em lưu vào sổ luôn ạ?"
            else:
                hint_spoken = " Bác xem thông tin đã chuẩn xác chưa để em lưu vào sổ nhật ký nhé!"
        else:
            hint_spoken = " Bác xem thông tin đã chuẩn xác chưa để em lưu vào sổ nhật ký nhé!"

        if act == "spray":
            med = s.get("ten_thuoc_bvtv") or "thuốc"
            return f"Dạ em đã tổng hợp xong phiếu phun thuốc {med} cho {crop}.{hint_spoken}"
        elif act == "fertilize":
            fert = s.get("ten_phan_bon") or "phân bón"
            return f"Dạ em đã tổng hợp xong phiếu bón phân {fert} cho {crop}.{hint_spoken}"
        elif act == "purchase":
            mat = s.get("material_name") or "vật tư"
            return f"Dạ em đã tổng hợp phiếu mua {mat}.{hint_spoken}"
        elif act == "harvest":
            qty = s.get("san_luong_thu_hoach") or "sản lượng"
            return f"Dạ em đã tổng hợp phiếu thu hoạch {crop} {qty}.{hint_spoken}"
        elif act == "sale":
            qty = s.get("khoi_luong_tieu_thu") or "khối lượng"
            return f"Dạ em đã tổng hợp phiếu xuất bán {crop} {qty}.{hint_spoken}"
        elif act == "observation":
            pest = s.get("sinh_vat_gay_hai") or "sâu bệnh"
            return f"Dạ em đã tổng hợp phiếu theo dõi sâu bệnh {pest} trên {crop}.{hint_spoken}"
        elif act == "waste":
            vt = s.get("material_name") or "vỏ bao bì thuốc"
            return f"Dạ em đã tổng hợp phiếu xử lý vỏ bao bì {vt}.{hint_spoken}"
        else:
            return f"Dạ em đã tổng hợp xong thông tin nhật ký.{hint_spoken}"

    # 2. Trạng thái đã lưu thành công (COMMITTED)
    if state_str == "COMMITTED" or "LƯU THÀNH CÔNG" in text:
        act = (activity_type or "").lower()
        s = slots or {}
        if act == "purchase":
            mat = s.get("material_name") or "vật tư"
            cat = classify_material_category(mat, s.get("material_type"))
            if cat == "pesticide":
                return f"Dạ em đã lưu thành công việc mua thuốc {mat}. Bác có muốn ghi luôn việc đem thuốc này đi phun cho vườn không ạ?"
            elif cat == "fertilizer":
                return f"Dạ em đã lưu thành công việc mua phân bón {mat}. Bác có muốn ghi luôn việc đem phân này đi bón cho vườn không ạ?"
            else:
                return f"Dạ em đã lưu thành công việc mua vật tư {mat} rồi bác nhé!"
        elif act == "spray":
            med = s.get("ten_thuoc_bvtv") or "thuốc"
            return f"Dạ em đã lưu xong phiếu phun thuốc {med}. Bác có muốn ghi chép việc xử lý vỏ bao bì thuốc luôn không ạ?"
        elif act == "waste":
            vt = s.get("material_name") or "thuốc"
            return f"Dạ em đã lưu xong phiếu xử lý vỏ bao bì thuốc {vt} theo chuẩn VietGAP rồi bác nhé!"
        elif act == "fertilize":
            fert = s.get("ten_phan_bon") or "phân bón"
            return f"Dạ em đã lưu xong phiếu bón phân {fert}. Bác có muốn ghi chép việc mua loại phân này vào sổ vật tư không ạ?"
        else:
            return "Dạ em đã lưu thành công vào sổ nhật ký rồi bác nhé!"

    # 3. Trạng thái hỏi bổ sung khi có ảnh (CLARIFYING với ảnh)
    if analysis_dict and state_str == "CLARIFYING":
        act = (activity_type or "").lower()
        s = slots or {}
        if not act or act == "general":
            if s.get("ten_thuoc_bvtv") or analysis_dict.get("pesticide"):
                act = "spray"
            elif s.get("ten_phan_bon") or analysis_dict.get("fertilizer"):
                act = "fertilize"
            elif analysis_dict.get("pest_disease"):
                act = "observation"

        if act == "purchase":
            mat = s.get("material_name") or analysis_dict.get("title") or "vật tư"
            return f"Dạ em đã nhận diện được {mat}. Bác cho em hỏi đợt này mình mua với số lượng bao nhiêu (mấy bao, mấy chai hoặc mấy ký) ạ?"
        elif act == "spray":
            p = analysis_dict.get("pesticide") or {}
            med = s.get("ten_thuoc_bvtv") or p.get("trade_name") or analysis_dict.get("title") or "thuốc"
            from services.rag.safety_checker import safety_checker
            if p.get("is_banned") or s.get("is_banned") or safety_checker.check_banned_substance(med).is_banned:
                return f"Bác ơi, thuốc {med} đã bị cấm sử dụng tại Việt Nam nên hệ thống không cho phép ghi vào sổ nhật ký VietGAP đâu ạ!"
            if not s.get("target_pest") and not p.get("target_pests"):
                return f"Dạ em đã nhận diện được thuốc {med}. Bác cho em hỏi mình phun trị sâu bệnh gì và phun bao nhiêu bình nước thế ạ?"
            return f"Dạ em đã nhận diện được thuốc {med}. Bác cho em hỏi đợt này mình phun bao nhiêu bình hoặc bao nhiêu lít nước thế ạ?"
        elif act == "fertilize":
            f = analysis_dict.get("fertilizer") or {}
            fert = s.get("ten_phan_bon") or f.get("trade_name") or analysis_dict.get("title") or "phân bón"
            return f"Dạ em đã nhận diện được phân bón {fert}. Bác cho em hỏi đợt này mình bón khối lượng bao nhiêu (mấy bao hoặc mấy ký) ạ?"
        elif act == "harvest":
            crop = s.get("crop_name") or "nông sản"
            return f"Dạ em đã ghi nhận hình ảnh thu hoạch {crop}. Bác cho em hỏi đợt này mình thu hoạch được sản lượng bao nhiêu ạ?"
        elif act == "observation":
            pd = analysis_dict.get("pest_disease") or {}
            pest = s.get("sinh_vat_gay_hai") or pd.get("pest_name") or "sâu bệnh"
            return f"Dạ em đã nhận diện sâu bệnh {pest}. Bác cho em hỏi mình quan sát thấy ở khu vực nào và đã phun thuốc gì chưa ạ?"
        else:
            return "Dạ em đã nhận diện hình ảnh. Bác cho em hỏi thêm thông tin liều lượng và khối lượng đợt này nhé!"

    # 4. Trạng thái tư vấn ảnh ban đầu (ADVISORY)
    if analysis_dict and state_str == "ADVISORY":
        cat = analysis_dict.get("category", "")
        # Sâu bệnh hại
        if cat == "pest_disease" or analysis_dict.get("pest_disease"):
            p = analysis_dict.get("pest_disease") or {}
            pest_name = p.get("pest_name") or analysis_dict.get("title", "sâu bệnh hại")
            crop = p.get("crop_name") or "cây trồng"
            symp = p.get("symptoms") or ""
            short_symp = symp.split(".")[0].split(";")[0].strip() if symp else "cần theo dõi"
            return f"Dạ trên cây {crop}, em chẩn đoán là {pest_name}, triệu chứng là {short_symp}. Bác chạm câu hỏi bên dưới để nghe hướng dẫn hoặc ghi nhật ký nhé!"

        # Thuốc BVTV
        elif cat == "pesticide" or analysis_dict.get("pesticide"):
            p = analysis_dict.get("pesticide") or {}
            name = p.get("trade_name") or analysis_dict.get("title") or "Thuốc bảo vệ thực vật"
            from services.rag.safety_checker import safety_checker
            banned_res = safety_checker.check_banned_substance(f"{name} {p.get('active_ingredient', '')}")
            if p.get("is_banned") or banned_res.is_banned:
                return f"Cảnh báo nguy hiểm! Thuốc {name} đã bị cấm sử dụng tại Việt Nam theo quy chuẩn VietGAP. Bác tuyệt đối không sử dụng thuốc này cho vườn cây nhé!"
            targets = p.get("target_pests") or []
            target_str = ", ".join(targets[:3]) if isinstance(targets, list) and targets else "phòng trừ sâu bệnh"
            return f"Dạ em nhận diện được thuốc {name}, tác dụng {target_str}. Bác chạm câu hỏi bên dưới để nghe hướng dẫn hoặc ghi nhật ký nhé!"

        # Phân bón
        elif cat == "fertilizer" or analysis_dict.get("fertilizer"):
            f = analysis_dict.get("fertilizer") or {}
            name = f.get("trade_name") or analysis_dict.get("title") or "Phân bón nông nghiệp"
            benefits = f.get("benefits") or []
            ben_str = ", ".join(benefits[:2]) if isinstance(benefits, list) and benefits else "cung cấp dinh dưỡng"
            return f"Dạ đây là phân bón {name}, tác dụng {ben_str}. Bác chạm câu hỏi bên dưới để nghe hướng dẫn hoặc ghi nhật ký nhé!"

    # 4. Trạng thái hỏi bổ sung thông tin (CLARIFYING) hoặc câu thoại chung
    clean = text
    clean = re.sub(r"[━─\-_=]{3,}", "", clean)
    clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", clean)
    clean = re.sub(r"\*([^*]+)\*", r"\1", clean)
    clean = re.sub(r"#{1,6}\s*", "", clean)
    clean = re.sub(r"^[ \t]*[•\-\*]\s*", "", clean, flags=re.MULTILINE)
    clean = re.sub(r"\[ID\s*\d+\]", "", clean)
    clean = re.sub(r"\[[\d\s,]+\]", "", clean)
    clean = re.sub(r"\(?Bảng\s*[\d\.]+[a-z]?\)?", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\bCSDL\b", "sổ nhật ký", clean, flags=re.IGNORECASE)
    clean = re.sub(r"[🌿🐛🧪🌱🌾🧾📷🔍📝📦💡⚖️⏳🎯✅⚠️✓✕🔊🔇●👉🌾🚜🏪💰📍🤝🛡️♻️⚒️⛔🚨]", "", clean)
    clean = re.sub(r"\n+", ". ", clean)
    clean = re.sub(r"\s*,\s*,", ",", clean)
    clean = re.sub(r"\s*,\s*\.", ".", clean)
    clean = re.sub(r"\s+([,.\?!])", r"\1", clean)
    clean = re.sub(r"\.\s*\.", ".", clean)
    clean = re.sub(r"\s+", " ", clean).strip()

    # Bảo toàn 100% câu trả lời để giọng đọc TTS phát âm trọn vẹn không bị ngắt cụt.
    # Chỉ ngắt tại dấu chấm câu kết thúc nếu vượt quá 700 ký tự (phòng ngừa câu quá dài bất thường).
    if len(clean) > 700:
        truncated = clean[:700]
        last_dot = max(truncated.rfind("."), truncated.rfind("?"), truncated.rfind("!"))
        if last_dot > 350:
            clean = truncated[:last_dot + 1]
        else:
            clean = truncated.strip() + "."
    return clean
