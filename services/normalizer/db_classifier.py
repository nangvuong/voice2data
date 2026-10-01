"""
DB Classifier & Normalizer (services/normalizer/db_classifier.py)
------------------------------------------------------------------
Tự động phân loại các trường trích xuất từ câu nói vào các bảng CSDL chuẩn VietGAP theo schema.sql:
- Bảng 1.5: material_purchases (mua và nhập vật tư, phân bón, thuốc BVTV, giống)
- Bảng 1.6: cultivation_diaries, material_usages, diary_pests (nhật ký canh tác, sử dụng vật tư, phòng trừ sâu bệnh)
- Bảng 1.7: harvests, sales (thu hoạch và tiêu thụ nông sản)
"""

import re
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

from services.normalizer.activity_normalizer import (
    normalize_relative_date,
    normalize_text,
)
from services.normalizer.dates import resolve_spoken_date, extract_spoken_expiry_date

logger = logging.getLogger(__name__)

ACTIVITY_TYPES = {
    "SOIL_PREPARATION", "PLANTING", "FERTILIZING",
    "PESTICIDE", "IRRIGATION", "HARVEST", "OTHER"
}

FERTILIZER_TYPES = {"ORGANIC", "INORGANIC", "BIO", "FOLIAR", "OTHER"}
PEST_TYPES = {"INSECT", "DISEASE", "WEED", "OTHER"}
SEVERITY_LEVELS = {"LOW", "MEDIUM", "HIGH", "Nhẹ", "Trung bình", "Nặng"}
IRRIGATION_METHODS = {"DRIP", "SPRINKLER", "FURROW", "FLOOD", "MANUAL", "OTHER"}
INPUT_TYPES = {"SEED", "SEEDLING", "FERTILIZER", "PESTICIDE", "CHEMICAL", "OTHER"}


def to_iso_date(val: Optional[str]) -> Optional[str]:
    """
    Chuyển đổi chuỗi ngày (DD/MM/YYYY, DD-MM-YYYY, ngày chữ...) sang định dạng ISO YYYY-MM-DD chuẩn SQL.
    """
    if not val or str(val).strip().upper() in ("NULL", "NONE", ""):
        return None

    s = str(val).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s

    m_iso = re.match(r"^(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})$", s)
    if m_iso:
        y, mth, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        return f"{y:04d}-{mth:02d}-{d:02d}"

    m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$", s)
    if m:
        d, mth, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mth:02d}-{d:02d}"

    # 1. Thử bóc tách hạn sử dụng hoặc ngày nói cụ thể
    exp_iso = extract_spoken_expiry_date(s)
    if exp_iso and re.match(r"^\d{4}-\d{2}-\d{2}$", exp_iso):
        return exp_iso

    # 2. Bóc tách ngày nói tiếng Việt chính xác (chỉ nhận khi parse được ngày thực, không lấy fallback ngày hôm nay)
    res_spoken = resolve_spoken_date(s, fallback_iso="")
    if res_spoken and re.match(r"^\d{4}-\d{2}-\d{2}$", res_spoken):
        return res_spoken

    norm_date = normalize_relative_date(s) or s

    if " - " in norm_date:
        norm_date = norm_date.split(" - ")[0].strip()
    elif ", " in norm_date:
        norm_date = norm_date.split(", ")[0].strip()

    m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$", norm_date)
    if m:
        d, mth, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mth:02d}-{d:02d}"

    m_iso = re.match(r"^(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})$", norm_date)
    if m_iso:
        y, mth, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        return f"{y:04d}-{mth:02d}-{d:02d}"

    return norm_date


def parse_dosage_and_solution(text: Optional[str]) -> Tuple[Optional[float], Optional[str], Optional[float], Optional[str]]:
    """
    Tách số lượng (float), đơn vị, lượng nước pha (solution_volume) và đơn vị nước pha.
    """
    if not text or str(text).strip().upper() in ("NULL", "NONE", ""):
        return None, None, None, None

    try:
        s = normalize_text(str(text)).upper()
    except Exception:
        s = str(text).upper()

    s = re.sub(r"\s*/\s*HA\b", "/HA", s)
    s = re.sub(r"\s*/\s*SÀO\b", "/SÀO", s)
    s = re.sub(r"\bKI\s+LÔ\s+GAM\b", "KG", s)
    s = re.sub(r"\bKILOGAM\b", "KG", s)
    s = re.sub(r"\bHÉC\s+TA\b", "HA", s)
    s = re.sub(r"\bTRÊN\s+(?:1\s+)?HA\b", "/HA", s)
    s = re.sub(r"\bTRÊN\s+(?:1\s+)?SÀO\b", "/SÀO", s)

    sol_vol, sol_unit = None, None
    m_sol = re.search(r"PHA\s+(\d+(?:[.,]\d+)?)\s*(LÍT|L|ML)?(?:\s+NƯỚC)?", s)
    if m_sol:
        sol_vol = float(m_sol.group(1).replace(",", "."))
        sol_unit = m_sol.group(2) or "LÍT"
        s_main = s[:m_sol.start()] + " " + s[m_sol.end():]
    else:
        s_main = s

    m_qty = re.search(r"(\d+(?:[.,]\d+)?)", s_main)
    qty, unit = None, None
    if m_qty:
        qty = float(m_qty.group(1).replace(",", "."))
        unit = s_main[m_qty.end():].strip()
        unit = re.sub(r"^(?:TRÊN|CHO|MỖI)\s+", "", unit)
        unit = re.sub(r"\s+", " ", unit).strip()
        if not unit and "/" in s_main:
            unit = s_main.split("/")[-1].strip()

    return qty, unit or None, sol_vol, sol_unit


def infer_activity_type(cong_viec: Optional[str], description: str = "") -> str:
    """Xác định ENUM activity_type trong cultivation_diaries."""
    combined = f"{cong_viec or ''} {description or ''}".upper()

    if any(k in combined for k in ["CÀY", "BỪA", "PHAY ĐẤT", "CHUẨN BỊ ĐẤT", "DỌN CỎ", "LÀM ĐẤT", "BẠT CHỐNG CỎ", "RẢI BẠT", "BẠT PHỦ"]):
        return "SOIL_PREPARATION"
    if any(k in combined for k in ["TRỒNG", "GIEO", "XUỐNG GIỐNG", "ĐI DẶM", "DẶM CÂY"]):
        return "PLANTING"
    if any(k in combined for k in ["BÓN PHÂN", "RẢI PHÂN", "BÓN LÓT", "BÓN THÚC", "PHÂN NPK", "PHÂN CHUỒNG"]):
        return "FERTILIZING"
    if any(k in combined for k in ["PHUN THUỐC", "XỊT THUỐC", "TRỊ SÂU", "TRỊ BỆNH", "NANO ECOTECH", "TOP 1", "BVTV"]):
        return "PESTICIDE"
    if any(k in combined for k in ["TƯỚI", "TƯỚI NƯỚC", "GIỮ ẨM", "LÀM ẨM", "BÉC TƯỚI", "ỐNG NƯỚC"]):
        return "IRRIGATION"
    if any(k in combined for k in ["THU HOẠCH", "CẮT CÀ GAI", "BĂM", "SẤY KHÔ", "HÁI"]):
        return "HARVEST"
    return "OTHER"


def infer_fertilizer_type(name: Optional[str]) -> str:
    """Xác định ENUM fertilizer_type trong materials."""
    if not name:
        return "OTHER"
    u = str(name).upper()
    if any(k in u for k in ["CHUỒNG", "HỮU CƠ", "Ủ", "PHÂN BÒ", "PHÂN GÀ", "TRÙN QUẾ"]):
        return "ORGANIC"
    if any(k in u for k in ["NPK", "ĐẠM", "LÂN", "KALI", "URE", "VÔ CƠ", "HÓA HỌC"]):
        return "INORGANIC"
    if any(k in u for k in ["SINH HỌC", "VI SINH", "NANO"]):
        return "BIO"
    if any(k in u for k in ["LÁ", "DẠNG NƯỚC", "NƯỚC", "XỊT LÁ"]):
        return "FOLIAR"
    return "OTHER"


def infer_pest_type(pest_name: Optional[str]) -> str:
    """Xác định ENUM pest_type trong pests."""
    if not pest_name:
        return "OTHER"
    u = str(pest_name).upper()
    if any(k in u for k in ["SÂU", "RẦY", "BỌ", "NHỆN", "RỆP", "CHÂU CHẤU", "ỐC"]):
        return "INSECT"
    if any(k in u for k in ["NẤM", "KHẢM", "HÉO", "RỈ SẮT", "THÁN THƯ", "THỐI", "ĐỐM", "SƯƠNG MAI"]):
        return "DISEASE"
    if any(k in u for k in ["CỎ", "CỎ DẠI"]):
        return "WEED"
    return "OTHER"


def infer_input_type(item_name: Optional[str]) -> str:
    """Xác định ENUM material_type trong materials / material_purchases."""
    if not item_name:
        return "OTHER"
    u = str(item_name).upper()
    if any(k in u for k in ["CÂY GIỐNG", "CÂY CON", "HẠT GIỐNG", "GIỐNG"]):
        return "SEED"
    if any(k in u for k in ["PHÂN", "NPK", "CHUỒNG", "HỮU CƠ"]):
        return "FERTILIZER"
    if any(k in u for k in ["THUỐC", "NANO ECOTECH", "TOP 1", "BVTV"]):
        return "PESTICIDE"
    if any(k in u for k in ["HÓA CHẤT", "VÔI", "KHỬ TRÙNG"]):
        return "CHEMICAL"
    return "OTHER"


def classify_into_tables(data: Union[dict, list]) -> Dict[str, Any]:
    """
    Trích xuất và tự động phân loại các trường của câu nói vào các bảng CSDL chuẩn VietGAP:
    - Bảng 1.5: material_purchases
    - Bảng 1.6: cultivation_diaries, material_usages, diary_pests
    - Bảng 1.7: harvests, sales
    """
    if isinstance(data, list):
        data = data[0] if data and isinstance(data[0], dict) else {}
    if not isinstance(data, dict):
        return {}

    # Kiểm tra xem input đã có cấu trúc bảng chưa
    has_table_keys = any(
        k in data for k in [
            "cultivation_diaries", "material_usages", "diary_pests", "material_purchases", "harvests", "sales",
            "cultivation_activities", "fertilizer_applications", "pesticide_applications",
            "pest_observations", "irrigation_records", "input_purchases",
            "harvest_records", "sales_records"
        ]
    )
    if has_table_keys:
        return normalize_db_tables(data)

    def clean_val(v):
        if v is None:
            return None
        s = str(v).strip()
        return None if s.upper() in ("NULL", "NONE", "") else s

    # 1. Trích xuất các trường chung cho bảng CULTIVATION_DIARIES
    ngay_raw = clean_val(data.get("ngay_thuc_hien") or data.get("activity_date") or data.get("ngay"))
    iso_date = to_iso_date(ngay_raw)
    giai_doan_raw = clean_val(data.get("giai_doan") or data.get("giai_doan_sinh_truong") or data.get("growth_stage"))
    giai_doan = None
    if giai_doan_raw:
        try:
            from services.database import detect_growth_stage_from_text
            giai_doan = detect_growth_stage_from_text(giai_doan_raw) or giai_doan_raw
        except Exception:
            giai_doan = giai_doan_raw
    cong_viec = clean_val(data.get("cong_viec") or data.get("description") or data.get("action"))
    thiet_bi = clean_val(data.get("thiet_bi") or data.get("thiet_bi_dung_cu") or data.get("equipment_used"))
    loai_hd = clean_val(data.get("loai_hoat_dong") or data.get("activity_type"))

    # Các trường đặc thù
    ten_pb = clean_val(data.get("ten_phan_bon"))
    luong_pb = clean_val(data.get("luong_phan_bon"))
    pp_bon = clean_val(data.get("phuong_phap_bon"))

    ten_thuoc = clean_val(data.get("ten_thuoc_bvtv"))
    svgh = clean_val(data.get("sinh_vat_gay_hai") or data.get("pest_name") or data.get("target_pest"))
    lieu_thuoc = clean_val(data.get("lieu_luong_thuoc"))
    nuoc_pha = clean_val(data.get("luong_nuoc_pha"))
    thoi_gian_cl = clean_val(data.get("thoi_gian_cach_ly") or data.get("quarantine_days"))

    pp_tuoi = clean_val(data.get("phuong_phap_tuoi") or data.get("irrigation_method"))

    # Các trường Bảng 1.5 (Mua/SX vật tư)
    ten_vt = clean_val(data.get("ten_vat_tu_nhap") or data.get("input_name"))
    sl_vt = clean_val(data.get("so_luong_vat_tu"))
    noi_mua_vt = clean_val(data.get("noi_mua_vat_tu"))
    han_su_dung = clean_val(data.get("han_su_dung") or data.get("expiry_date"))
    so_lo = clean_val(data.get("so_lo") or data.get("batch_no"))

    # Các trường Bảng 1.7 (Thu gom, xử lý bao bì và thuốc BVTV dư thừa)
    loai_bb = clean_val(data.get("loai_bao_bi") or data.get("waste_type") or data.get("loai_bao_bi_thuoc_du"))
    noi_tt = clean_val(data.get("noi_ton_tru") or data.get("storage_location"))
    cach_xl = clean_val(data.get("cach_xu_ly") or data.get("treatment_method"))

    # Các trường Bảng 1.8 (Thu hoạch và tiêu thụ)
    sl_harvest = clean_val(data.get("san_luong_thu_hoach"))
    cach_so_che = clean_val(data.get("cach_so_che") or data.get("processing_method"))
    kl_ban = clean_val(data.get("khoi_luong_ban"))
    nguoi_mua = clean_val(data.get("nguoi_mua_thuong_lai"))

    # Ghi chú chung (Đã lược bỏ theo yêu cầu người dùng)
    ghi_chu = None

    # Fallback cho schema 8 trường cũ
    ten_pt_old = clean_val(data.get("ten_phan_thuoc"))
    luong_sd_old = clean_val(data.get("luong_su_dung"))
    noi_mua_old = clean_val(data.get("noi_mua"))

    if not ten_pb and not ten_thuoc and ten_pt_old:
        if any(k in ten_pt_old.upper() for k in ["THUỐC", "NANO ECOTECH", "TOP 1", "BVTV"]):
            ten_thuoc = ten_pt_old
        else:
            ten_pb = ten_pt_old

    # Xác định loại hoạt động
    act_type = loai_hd.upper() if (loai_hd and loai_hd.upper() in ACTIVITY_TYPES) else None
    if not act_type:
        act_type = infer_activity_type(cong_viec, f"{ten_pb or ten_thuoc or ten_pt_old or ''} {giai_doan or ''}")

    equipment_used = thiet_bi.upper() if thiet_bi else None

    # Khởi tạo dict tables rỗng - CHỈ THÊM BẢNG KHI CÓ KEY TƯƠNG ỨNG
    tables: Dict[str, Any] = {}

    # =========================================================================
    # 1. BẢNG 1.5: MUA HOẶC SẢN XUẤT VẬT TƯ (b1_5_material_purchases)
    # =========================================================================
    is_purchase = bool(
        ten_vt or sl_vt or noi_mua_vt or han_su_dung or so_lo or
        ("b1_5_material_purchases" in data) or ("material_purchases" in data) or ("input_purchases" in data) or
        (cong_viec and any(k in cong_viec.upper() for k in ["MUA", "NHẬP", "LẤY VỀ", "TỰ SẢN XUẤT", "TỰ Ủ", "ĐẠI LÝ"])) or
        (noi_mua_old and not (kl_ban or nguoi_mua or ten_pb or ten_thuoc))
    )
    if is_purchase:
        in_name = (ten_vt or ten_pb or ten_thuoc or ten_pt_old or cong_viec or "VẬT TƯ NÔNG NGHIỆP").upper()
        in_qty, in_unit, _, _ = parse_dosage_and_solution(sl_vt or luong_sd_old)
        supplier = (noi_mua_vt or noi_mua_old)
        exp_d = to_iso_date(han_su_dung) if han_su_dung else None
        item_1_5 = {
            "purchase_date": iso_date,
            "material_name": in_name,
            "material_type": infer_input_type(in_name),
            "quantity": in_qty,
            "unit": in_unit or "ĐƠN VỊ",
            "supplier_name": supplier.upper() if supplier else None,
            "batch_no": so_lo,
            "expiry_date": exp_d,
            "note": ghi_chu,
        }
        tables["b1_5_material_purchases"] = item_1_5
        tables["material_purchases"] = item_1_5
        tables["input_purchases"] = {
            "purchase_date": iso_date,
            "input_type": infer_input_type(in_name),
            "input_name": in_name,
            "quantity": in_qty,
            "unit": in_unit or "ĐƠN VỊ",
            "supplier_name": supplier.upper() if supplier else None,
        }

    # =========================================================================
    # 2. BẢNG 1.7: THU GOM, XỬ LÝ BAO BÌ VÀ THUỐC BVTV DƯ THỪA (b1_7_waste_treatments)
    # =========================================================================
    is_waste = bool(
        loai_bb or noi_tt or cach_xl or
        ("b1_7_waste_treatments" in data) or ("waste_treatments" in data) or
        (cong_viec and any(k in cong_viec.upper() for k in ["THU GOM", "XỬ LÝ BAO BÌ", "VỎ CHAI", "THÙNG CHỨA", "TIÊU HỦY", "RÁC THẢI", "SÚC RỬA", "THUỐC DƯ"]))
    )
    if is_waste:
        item_1_7 = {
            "treatment_date": iso_date,
            "waste_type": (loai_bb or "VỎ CHAI, BAO BÌ THUỐC BVTV").upper(),
            "material_name": (ten_thuoc or ten_vt or "").upper() or None,
            "storage_location": (noi_tt or "HỐ CHỨA BAO BÌ THUỐC BVTV CÓ MÁI CHE").upper(),
            "treatment_method": (cach_xl or "THU GOM, SÚC RỬA 3 LẦN, BÀN GIAO ĐƠN VỊ XỬ LÝ NGUY HẠI").upper(),
            "note": ghi_chu,
        }
        tables["b1_7_waste_treatments"] = item_1_7
        tables["waste_treatments"] = item_1_7

    # =========================================================================
    # 3. BẢNG 1.8: THU HOẠCH VÀ TIÊU THỤ SẢN PHẨM (b1_8_harvest_sales)
    # =========================================================================
    is_harvest_sale = bool(
        sl_harvest or cach_so_che or kl_ban or nguoi_mua or
        act_type == "HARVEST" or
        ("b1_8_harvest_sales" in data) or ("harvests" in data) or ("sales" in data) or
        ("harvest_records" in data) or ("sales_records" in data) or
        (cong_viec and any(k in cong_viec.upper() for k in ["THU HOẠCH", "CẮT", "HÁI", "XUẤT BÁN", "BÁN CHO", "CÂN BÁN", "THƯƠNG LÁI"]))
    )
    if is_harvest_sale:
        h_qty, h_unit, _, _ = parse_dosage_and_solution(sl_harvest or luong_sd_old)
        s_qty, s_unit, _, _ = parse_dosage_and_solution(kl_ban or luong_sd_old)
        crop_name = clean_val(data.get("loai_nong_san")) or clean_val(data.get("crop_name")) or "NÔNG SẢN"
        proc_desc = cach_so_che.upper() if cach_so_che else ("TẠI VƯỜN" if sl_harvest else None)
        buyer_desc = (nguoi_mua or noi_mua_old or "").upper() or ("THƯƠNG LÁI" if kl_ban else None)

        tables["b1_8_harvest_sales"] = {
            "harvest_date": iso_date,
            "san_luong_thu_hoach": sl_harvest or (f"{h_qty} {h_unit or 'KG'}" if h_qty else None),
            "dia_diem_cach_so_che": proc_desc,
            "sale_date": iso_date if (kl_ban or nguoi_mua) else None,
            "co_so_thu_mua": buyer_desc,
            "khoi_luong_tieu_thu": kl_ban or (f"{s_qty} {s_unit or 'KG'}" if s_qty else None),
            "crop_name": crop_name,
            "note": ghi_chu,
        }
        if sl_harvest or act_type == "HARVEST":
            tables["harvests"] = {
                "harvest_date": iso_date,
                "quantity": h_qty,
                "unit": h_unit or "KG",
                "crop_name": crop_name,
                "location": "TẠI VƯỜN",
                "processing_method": proc_desc,
                "note": ghi_chu,
            }
            tables["harvest_records"] = tables["harvests"]
        if kl_ban or nguoi_mua:
            tables["sales"] = {
                "sale_date": iso_date,
                "quantity": s_qty,
                "unit": s_unit or "KG",
                "buyer_name": buyer_desc,
                "note": ghi_chu,
            }
            tables["sales_records"] = tables["sales"]

    # =========================================================================
    # 4. BẢNG 1.6: SỬ DỤNG PHÂN BÓN VÀ THUỐC BVTV, HÓA CHẤT (b1_6_material_usages)
    # =========================================================================
    # Điều kiện kích hoạt: có thông tin phân bón, thuốc BVTV, sâu bệnh, tưới tiêu,
    # hoặc hoạt động canh tác mà không phải là mua vật tư / xử lý rác / thu hoạch riêng biệt.
    has_fert_pest = bool(ten_pb or luong_pb or pp_bon or ten_thuoc or svgh or lieu_thuoc or nuoc_pha or thoi_gian_cl or pp_tuoi)
    is_usage = bool(
        has_fert_pest or
        act_type in ("FERTILIZING", "PESTICIDE", "IRRIGATION", "SOIL_PREPARATION", "PLANTING") or
        ("b1_6_material_usages" in data) or ("material_usages" in data) or
        (cong_viec and not (is_purchase and not has_fert_pest) and not is_waste and not is_harvest_sale)
    )

    if is_usage or not tables:
        f_name = ten_pb.upper() if ten_pb else None
        f_qty, f_unit, _, _ = parse_dosage_and_solution(luong_pb or (luong_sd_old if act_type == "FERTILIZING" else None))
        f_method = pp_bon.upper() if pp_bon else ("BÓN LÓT" if (cong_viec and "LÓT" in cong_viec.upper()) else ("BÓN THÚC" if f_name else None))

        p_name = ten_thuoc.upper() if ten_thuoc else None
        p_dos, p_unit, p_sol, p_sol_unit = parse_dosage_and_solution(lieu_thuoc or (luong_sd_old if act_type == "PESTICIDE" else None))
        if nuoc_pha:
            m_np = re.search(r"(\d+(?:[.,]\d+)?)", normalize_text(nuoc_pha))
            if m_np:
                p_sol = float(m_np.group(1).replace(",", "."))
                p_sol_unit = "LÍT"

        q_days = None
        if thoi_gian_cl:
            m_cl = re.search(r"\d+", normalize_text(str(thoi_gian_cl)))
            if m_cl:
                q_days = int(m_cl.group(0))

        conc_str = f"PHA {p_sol} {p_sol_unit or 'LÍT'}" if p_sol else (nuoc_pha.upper() if nuoc_pha else None)
        if lieu_thuoc and conc_str:
            nong_do_str = f"{conc_str} ({lieu_thuoc})"
        elif conc_str:
            nong_do_str = conc_str
        elif lieu_thuoc:
            nong_do_str = lieu_thuoc
        else:
            nong_do_str = None

        method_str = pp_bon or equipment_used or pp_tuoi
        if not method_str:
            if p_name:
                method_str = "PHUN BÌNH MÁY"
            elif f_name:
                method_str = f_method or "BÓN GỐC"
            else:
                method_str = "THỰC HIỆN TRỰC TIẾP"

        tables["b1_6_material_usages"] = {
            "activity_date": iso_date,
            "ten_phan_bon": f_name,
            "luong_phan_bon": luong_pb,
            "phuong_phap_bon": f_method,
            "ten_thuoc_bvtv": p_name,
            "sinh_vat_gay_hai": svgh.upper() if svgh else None,
            "target_pest": svgh.upper() if svgh else None,
            "lieu_luong_thuoc": lieu_thuoc,
            "nong_do_va_luong_dung": nong_do_str,
            "luong_nuoc_pha": nuoc_pha,
            "thoi_gian_cach_ly": q_days,
            "application_method": method_str.upper() if method_str else None,
            "growth_stage": giai_doan if giai_doan else None,
            "description": cong_viec.upper() if cong_viec else None,
            "note": ghi_chu,
        }

        # Lưu thông tin dùng vật tư tương thích ngược
        mat_name = p_name or f_name or (ten_pt_old.upper() if ten_pt_old else None)
        mat_type = "PESTICIDE" if p_name else ("FERTILIZER" if f_name else "OTHER")
        mat_qty = p_dos if p_name else f_qty
        mat_unit = p_unit if p_name else f_unit
        tables["material_usages"] = {
            "activity_date": iso_date,
            "material_name": mat_name or "VẬT TƯ NÔNG NGHIỆP",
            "material_type": mat_type,
            "quantity": mat_qty,
            "unit": mat_unit or "KG",
            "concentration": conc_str,
            "isolation_days": q_days,
            "application_method": method_str.upper() if method_str else None,
            "supplier_name": (noi_mua_vt or noi_mua_old or "").upper() or None,
            "note": ghi_chu,
        }

        if f_name or act_type == "FERTILIZING":
            tables["fertilizer_applications"] = {
                "fertilizer_name": f_name or "PHÂN BÓN",
                "fertilizer_type": infer_fertilizer_type(f_name or ""),
                "quantity": f_qty,
                "unit": f_unit or "KG",
                "application_method": f_method,
            }

        if p_name or act_type == "PESTICIDE" or lieu_thuoc or q_days:
            tables["pesticide_applications"] = {
                "target_pest": svgh.upper() if svgh else "SÂU HẠI CÂY TRỒNG",
                "pesticide_name": p_name or "THUỐC BVTV",
                "dosage": p_dos,
                "dosage_unit": p_unit or "LÍT/HA",
                "solution_volume": p_sol,
                "solution_unit": p_sol_unit or "LÍT",
                "quarantine_days": q_days,
            }

        if svgh:
            tables["diary_pests"] = {
                "activity_date": iso_date,
                "pest_name": svgh.upper(),
                "pest_type": infer_pest_type(svgh),
                "severity": "Trung bình",
                "note": f"PHÁT HIỆN TRÊN ĐỒNG RUỘNG: {svgh.upper()}",
            }
            tables["pest_observations"] = {
                "observation_date": iso_date,
                "pest_name": svgh.upper(),
                "pest_type": infer_pest_type(svgh),
                "severity_level": "MEDIUM",
                "damage_symptoms": f"PHÁT HIỆN TRÊN ĐỒNG RUỘNG: {svgh.upper()}",
            }

        if pp_tuoi or act_type == "IRRIGATION":
            irrig_method = "SPRINKLER" if (pp_tuoi and any(k in pp_tuoi.upper() for k in ["BÉC", "MƯA"])) else "MANUAL"
            tables["irrigation_records"] = {
                "water_source": "WELL_WATER",
                "irrigation_method": irrig_method,
                "duration_minutes": None,
                "volume_m3": None,
            }

        cultivation_act = {
            "activity_date": iso_date,
            "activity_type": act_type or "OTHER",
            "growth_stage": giai_doan if giai_doan else None,
            "description": cong_viec.upper() if cong_viec else None,
            "equipment_used": equipment_used,
        }
        tables["cultivation_diaries"] = cultivation_act
        tables["cultivation_activities"] = cultivation_act

    return tables


def normalize_db_tables(tables_dict: dict) -> Dict[str, Any]:
    """
    Chuẩn hoá các bảng CSDL đã được phân loại:
    - Chuẩn hoá ngày -> ISO YYYY-MM-DD
    - Chuẩn hoá số lượng sang float/int
    - Chuyển chữ hoa chuẩn CSDL
    """
    if not isinstance(tables_dict, dict):
        return {}

    norm_tables = {}
    for table_name, row in tables_dict.items():
        if not isinstance(row, dict):
            continue

        clean_row = {}
        for col, val in row.items():
            if val is None or str(val).strip().upper() in ("NULL", "NONE", ""):
                clean_row[col] = None
                continue

            v_str = str(val).strip()
            if col.endswith("_date") or col in ("activity_date", "purchase_date", "harvest_date", "sale_date", "observation_date", "start_date", "end_date"):
                clean_row[col] = to_iso_date(v_str)
            elif col in ("quantity", "dosage", "solution_volume", "volume_m3", "duration_minutes", "isolation_days", "quarantine_days"):
                if isinstance(val, (int, float)):
                    clean_row[col] = val
                else:
                    m = re.search(r"(\d+(?:[.,]\d+)?)", normalize_text(v_str))
                    if m:
                        num = float(m.group(1).replace(",", "."))
                        clean_row[col] = int(num) if col in ("duration_minutes", "isolation_days", "quarantine_days") else num
                    else:
                        clean_row[col] = None
            elif isinstance(val, str):
                clean_row[col] = v_str
            else:
                clean_row[col] = val

        norm_tables[table_name] = clean_row

    return norm_tables


def tables_to_flat_dict(tables: dict) -> dict:
    """
    Chuyển đổi cấu trúc bảng CSDL thành cấu trúc phẳng để tương thích ngược.
    """
    if not isinstance(tables, dict):
        return {}

    act = tables.get("b1_6_material_usages") or tables.get("cultivation_diaries") or tables.get("cultivation_activities", {})
    mat_u = tables.get("b1_6_material_usages") or tables.get("material_usages") or {}
    fert = tables.get("fertilizer_applications") or {}
    pest = tables.get("pesticide_applications") or {}
    obs = tables.get("diary_pests") or tables.get("pest_observations", {})
    pur = tables.get("b1_5_material_purchases") or tables.get("material_purchases") or tables.get("input_purchases", {})
    waste = tables.get("b1_7_waste_treatments") or tables.get("waste_treatments", {})
    har_sale = tables.get("b1_8_harvest_sales") or {}
    har = tables.get("harvests") or tables.get("harvest_records", {})
    sale = tables.get("sales") or tables.get("sales_records", {})

    iso_d = (
        act.get("activity_date") or mat_u.get("activity_date") or
        pur.get("purchase_date") or waste.get("treatment_date") or
        har_sale.get("harvest_date") or har_sale.get("sale_date") or
        har.get("harvest_date") or sale.get("sale_date")
    )
    ngay_display = iso_d
    if iso_d and re.match(r"^\d{4}-\d{2}-\d{2}$", iso_d):
        y, m, d = iso_d.split("-")
        ngay_display = f"{d}/{m}/{y}"

    giai_doan = act.get("growth_stage")
    cong_viec = act.get("description")

    svgh = act.get("sinh_vat_gay_hai") or obs.get("pest_name") or pest.get("target_pest")
    if svgh == "SÂU HẠI CÂY TRỒNG":
        svgh = None

    ten_pt = (
        act.get("ten_phan_bon") or act.get("ten_thuoc_bvtv") or
        mat_u.get("material_name") or fert.get("fertilizer_name") or pest.get("pesticide_name") or
        pur.get("material_name")
    )

    luong = None
    if act.get("luong_phan_bon"):
        luong = str(act.get("luong_phan_bon"))
    elif act.get("lieu_luong_thuoc"):
        luong = str(act.get("lieu_luong_thuoc"))
    elif mat_u.get("quantity") is not None:
        luong = f"{mat_u.get('quantity')} {mat_u.get('unit') or 'KG'}".strip()
    elif pur.get("quantity") is not None:
        luong = f"{pur.get('quantity')} {pur.get('unit') or ''}".strip()
    elif har_sale.get("san_luong_thu_hoach"):
        luong = str(har_sale.get("san_luong_thu_hoach"))
    elif har_sale.get("khoi_luong_tieu_thu"):
        luong = str(har_sale.get("khoi_luong_tieu_thu"))
    elif fert.get("quantity") is not None:
        luong = f"{fert.get('quantity')} {fert.get('unit') or 'KG'}".strip()
    elif pest.get("dosage") is not None:
        luong = f"{pest.get('dosage')} {pest.get('dosage_unit') or 'LÍT/HA'}".strip()
    elif sale.get("quantity") is not None:
        luong = f"{sale.get('quantity')} {sale.get('unit') or 'KG'}".strip()
    elif har.get("quantity") is not None:
        luong = f"{har.get('quantity')} {har.get('unit') or 'KG'}".strip()

    noi_mua = (
        pur.get("supplier_name") or mat_u.get("supplier_name") or
        har_sale.get("co_so_thu_mua") or sale.get("buyer_name")
    )

    thoi_gian_cl = None
    if act.get("thoi_gian_cach_ly") is not None:
        thoi_gian_cl = f"{act.get('thoi_gian_cach_ly')} NGÀY"
    elif mat_u.get("isolation_days") is not None:
        thoi_gian_cl = f"{mat_u.get('isolation_days')} NGÀY"
    elif pest.get("quarantine_days") is not None:
        thoi_gian_cl = f"{pest.get('quarantine_days')} NGÀY"

    return {
        "ngay_thuc_hien": ngay_display,
        "giai_doan": giai_doan,
        "giai_doan_sinh_truong": giai_doan,
        "cong_viec": cong_viec,
        "sinh_vat_gay_hai": svgh,
        "ten_phan_thuoc": ten_pt,
        "luong_su_dung": luong,
        "noi_mua": noi_mua,
        "thoi_gian_cach_ly": thoi_gian_cl,
    }
