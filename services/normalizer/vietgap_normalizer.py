"""
VietGAP Normalizer (services/normalizer/vietgap_normalizer.py)
---------------------------------------------------------------
Chuẩn hóa dữ liệu chuyên sâu cho 4 biểu mẫu nhật ký VietGAP theo vietGAP.pdf & schema.sql:
- Bảng 1.5: Mua (sản xuất) vật tư (material_purchases)
- Bảng 1.6: Sử dụng phân bón, thuốc BVTV & hóa chất (material_usages)
- Bảng 1.7: Xử lý bao bì, thuốc BVTV dư thừa (waste_treatments)
- Bảng 1.8: Thu hoạch và bán sản phẩm (harvest_sales)
"""

import re
import logging
from typing import Any, Dict, Optional, Tuple, Union
from datetime import datetime, date

from services.normalizer.dates import parse_date, extract_spoken_expiry_date
from services.normalizer.activity_normalizer import (
    normalize_relative_date,
    normalize_text,
)
from services.normalizer.db_classifier import to_iso_date

logger = logging.getLogger(__name__)

# Từ điển chuẩn hóa đơn vị đo lường VietGAP
UNIT_MAPPING = {
    "kg": "kg", "ký": "kg", "kí": "kg", "kilôgam": "kg", "kilogam": "kg", "kilo": "kg",
    "g": "g", "gram": "g", "gam": "g",
    "tấn": "tấn", "tan": "tấn",
    "tạ": "tạ", "ta": "tạ",
    "yến": "yến",
    "lít": "lít", "lit": "lít", "l": "lít",
    "ml": "ml", "mililít": "ml", "mililit": "ml", "cc": "ml",
    "bao": "bao", "bịch": "bịch", "gói": "gói", "chai": "chai",
    "thùng": "thùng", "can": "can", "lọ": "lọ", "ống": "ống",
    "bình": "bình", "phuy": "phuy", "phi": "phuy",
    "ha": "ha", "hecta": "ha", "héc ta": "ha",
    "sào": "sào", "sao": "sào", "công": "công",
    "cây": "cây", "nhánh": "nhánh", "chồi": "chồi", "củ": "củ", "hạt": "hạt",
    "người": "người", "lao động": "người", "công nhân": "người", "người làm": "người",
    "cái": "cái", "bộ": "bộ", "cuộn": "cuộn",
}

MATERIAL_TYPE_MAPPING = {
    "phân": "FERTILIZER", "phan": "FERTILIZER", "fertilizer": "FERTILIZER",
    "thuốc": "PESTICIDE", "thuoc": "PESTICIDE", "pesticide": "PESTICIDE", "bvtv": "PESTICIDE",
    "hóa chất": "CHEMICAL", "hoa chat": "CHEMICAL", "chemical": "CHEMICAL", "vôi": "CHEMICAL",
    "cây giống": "SEED", "cay giong": "SEED", "hạt / cây giống": "SEED", "hat / cay giong": "SEED",
    "giống": "SEED", "giong": "SEED", "hạt": "SEED", "cây con": "SEED", "seed": "SEED", "seedling": "SEED",
    "bầu giống": "SEED", "cây": "SEED",
}

# Chuẩn hóa phương pháp bón phân / phun thuốc (Bảng 1.6)
METHOD_MAPPING = {
    "lót": "BÓN LÓT", "bon lot": "BÓN LÓT",
    "thúc": "BÓN THÚC", "bon thuc": "BÓN THÚC",
    "gốc": "BÓN GỐC", "rải gốc": "BÓN GỐC", "vãi": "BÓN GỐC",
    "lá": "PHUN QUA LÁ", "phun la": "PHUN QUA LÁ",
    "nhỏ giọt": "TƯỚI NHỎ GIỌT", "nho giot": "TƯỚI NHỎ GIỌT", "drip": "TƯỚI NHỎ GIỌT",
    "phun mưa": "TƯỚI PHUN MƯA", "béc": "TƯỚI PHUN MƯA",
    "bình máy": "PHUN BÌNH MÁY", "binh may": "PHUN BÌNH MÁY",
    "đeo vai": "PHUN BÌNH ĐEO VAI", "bình đeo": "PHUN BÌNH ĐEO VAI",
    "drone": "PHUN BẰNG DRONE", "máy bay": "PHUN BẰNG DRONE",
}

# Chuẩn hóa giai đoạn sinh trưởng (5 giai đoạn chuẩn)
GROWTH_STAGE_MAPPING = {
    # 1. Sau thu hoạch (kiểm tra trước Thu hoạch)
    "sau thu hoạch": "Sau thu hoạch",
    "phục hồi sau thu hoạch": "Sau thu hoạch",
    "phục hồi cây": "Sau thu hoạch",
    "phục hồi": "Sau thu hoạch",
    "rửa vườn": "Sau thu hoạch",
    "dọn vườn sau thu hoạch": "Sau thu hoạch",
    "dọn vườn": "Sau thu hoạch",
    "tỉa cành già": "Sau thu hoạch",
    "tỉa cành tạo tán": "Sau thu hoạch",
    "cắt tỉa sau thu hoạch": "Sau thu hoạch",
    "tỉa cành": "Sau thu hoạch",
    "vệ sinh vườn": "Sau thu hoạch",
    # 2. Ra hoa
    "ra hoa": "Ra hoa",
    "nở hoa": "Ra hoa",
    "phân hóa mầm hoa": "Ra hoa",
    "chớm hoa": "Ra hoa",
    "bung hoa": "Ra hoa",
    "đang có hoa": "Ra hoa",
    "kết nụ": "Ra hoa",
    "nhú nụ": "Ra hoa",
    "làm bông": "Ra hoa",
    "xử lý ra hoa": "Ra hoa",
    "thụ phấn": "Ra hoa",
    # 3. Quả non
    "quả non": "Quả non",
    "trái non": "Quả non",
    "nuôi trái": "Quả non",
    "nuôi quả": "Quả non",
    "đậu quả": "Quả non",
    "đậu trái": "Quả non",
    "kết trái": "Quả non",
    "lớn trái": "Quả non",
    "nuôi củ": "Quả non",
    "rụng sinh lý": "Quả non",
    "chạy trái": "Quả non",
    "vào hạt": "Quả non",
    "phun dưỡng trái": "Quả non",
    "bón nuôi trái": "Quả non",
    # 4. Sinh trưởng
    "sinh trưởng": "Sinh trưởng",
    "đang sinh trưởng": "Sinh trưởng",
    "phát triển thân lá": "Sinh trưởng",
    "nuôi cành lá": "Sinh trưởng",
    "sinh trưởng mạnh": "Sinh trưởng",
    "đẻ nhánh": "Sinh trưởng",
    "cây con": "Sinh trưởng",
    "mới trồng": "Sinh trưởng",
    "xuống giống": "Sinh trưởng",
    "bén rễ": "Sinh trưởng",
    "gieo hạt": "Sinh trưởng",
    "gieo trồng": "Sinh trưởng",
    "ra đọt non": "Sinh trưởng",
    "làm đọt": "Sinh trưởng",
    "đọt non": "Sinh trưởng",
    "đâm chồi": "Sinh trưởng",
    "ra lá non": "Sinh trưởng",
    "búp non": "Sinh trưởng",
    "phát đọt": "Sinh trưởng",
    "chuẩn bị đất": "Sinh trưởng",
    "làm đất": "Sinh trưởng",
    "làm luống": "Sinh trưởng",
    "xới đất": "Sinh trưởng",
    "lên luống": "Sinh trưởng",
    "phát triển": "Sinh trưởng",
    # 5. Thu hoạch
    "chuẩn bị thu hoạch": "Thu hoạch",
    "sắp thu hoạch": "Thu hoạch",
    "đang thu hoạch": "Thu hoạch",
    "thu hoạch": "Thu hoạch",
    "thu hái": "Thu hoạch",
    "thu bói": "Thu hoạch",
    "chín": "Thu hoạch",
    "hái trái": "Thu hoạch",
    "hái quả": "Thu hoạch",
    "cắt trái": "Thu hoạch",
}

def normalize_growth_stage(stage_str: Optional[str]) -> Optional[str]:
    """Chuẩn hóa giai đoạn mùa vụ về đúng 5 giai đoạn chuẩn: Sau thu hoạch, Ra hoa, Quả non, Sinh trưởng, Thu hoạch."""
    if not stage_str or not str(stage_str).strip():
        return None
    s = str(stage_str).strip()
    s_low = s.lower()
    for canonical in ["Sau thu hoạch", "Ra hoa", "Quả non", "Sinh trưởng", "Thu hoạch"]:
        if s_low == canonical.lower():
            return canonical
    for k, v in GROWTH_STAGE_MAPPING.items():
        if k in s_low:
            return v
    return "Sinh trưởng"

def normalize_vietnamese_title(text: Optional[str]) -> Optional[str]:
    """Chuẩn hóa viết hoa chữ cái đầu cho tên riêng, vật tư, thương lái."""
    if not text or not str(text).strip():
        return None
    s = str(text).strip()
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" ,.-:;\"'“”")
    if not s:
        return None
    return s.title()

def clean_material_name(name: Optional[str]) -> Optional[str]:
    """Làm sạch tên vật tư, loại bỏ số lượng, trạng thái, nơi mua đi kèm nếu LLM trích xuất thừa."""
    if not name:
        return None
    s = str(name).strip()
    s = re.sub(r"\b(?:tại|ở|từ)\s+.*$", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"^(?:hôm nay|hôm qua|sáng nay|chiều nay|trưa nay|tối nay|hôm trước)\s+", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"^(?:mua|nhập|lấy|xuất|bán)\s+", "", s, flags=re.IGNORECASE).strip()
    try:
        norm = normalize_text(s)
        cleaned = re.sub(r"^\d+(?:[.,]\d+)?\s+", "", norm).strip()
        cleaned = re.sub(r"^(?:bao|chai|gói|can|thùng|lọ|cuộn|bịch)\s+(phân|thuốc)", r"\1", cleaned, flags=re.IGNORECASE)
    except Exception:
        cleaned = s
    return normalize_vietnamese_title(cleaned)

def clean_work_name(cv: Optional[str]) -> Optional[str]:
    """Làm sạch tên công việc canh tác khác, loại bỏ số người hoặc câu phủ định sâu bệnh đi kèm."""
    if not cv:
        return None
    s = str(cv).strip()
    s = re.sub(r"^(?:hôm nay|hôm qua|sáng nay|chiều nay|trưa nay|tối nay|hôm trước)\s+", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"\b(?:không có|không phát hiện|không ghi nhận|sạch)\s+.*$", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"\b(?:\d+|mười|năm|hai|ba|bốn|sáu|bảy|tám|chín|một)\s+(?:đến|-)\s*.*$", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"\b(?:khoảng|tầm)?\s*\d+\s*(?:người|lao động|cây|bao)?.*$", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"\b(?:người|lao động)\b.*$", "", s, flags=re.IGNORECASE).strip()
    return normalize_vietnamese_title(s)

def normalize_quantity_and_unit(text: Optional[str]) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    """
    Tách và chuẩn hóa số lượng và đơn vị từ chuỗi:
    Vd: "hai mươi bao" -> (20.0, "bao", "20 bao")
        "5,5 kg" -> (5.5, "kg", "5.5 kg")
        "hai tấn rưỡi" -> (2.5, "tấn", "2.5 tấn")
    """
    if not text or not str(text).strip():
        return None, None, None

    raw_str = str(text).strip()
    try:
        norm_text = normalize_text(raw_str).lower()
    except Exception:
        norm_text = raw_str.lower()

    norm_text = re.sub(r"(\d+)\s+rưỡi\b", r"\1.5", norm_text)
    norm_text = re.sub(r"\bnửa\s+(\w+)", r"0.5 \1", norm_text)

    m_num = re.search(r"(\d+(?:[.,]\d+)?)", norm_text)
    if not m_num:
        return None, None, None

    num_val = float(m_num.group(1).replace(",", "."))
    unit_part = norm_text[m_num.end():].strip()
    unit_part = re.sub(r"^(?:trên|cho|mỗi|vào)\s+", "", unit_part).strip()

    matched_unit = None
    for u_key, u_val in UNIT_MAPPING.items():
        if re.search(r"\b" + re.escape(u_key) + r"\b", unit_part):
            matched_unit = u_val
            break

    if not matched_unit:
        words = unit_part.split()
        if words:
            matched_unit = words[0]

    num_display = f"{int(num_val)}" if num_val.is_integer() else f"{num_val}"
    formatted_str = f"{num_display} {matched_unit}".strip() if matched_unit else num_display

    return num_val, matched_unit, formatted_str

def normalize_isolation_days(text: Optional[Union[str, int]]) -> Tuple[Optional[int], Optional[str]]:
    """
    Chuẩn hóa thời gian cách ly:
    "bảy ngày" -> (7, "7 ngày")
    "1 tuần" -> (7, "7 ngày")
    "14 ngày" -> (14, "14 ngày")
    """
    if text is None:
        return None, None

    raw = str(text).strip().lower()
    if not raw or raw in ("null", "none", "không") or any(k in raw for k in ["không cách ly", "không cần cách ly", "không có", "không"]):
        return None, None

    if "tuần" in raw:
        m = re.search(r"(\d+)", normalize_text(raw))
        w = int(m.group(1)) if m else 1
        days = w * 7
        return days, f"{days} ngày"

    m = re.search(r"(\d+)", normalize_text(raw))
    if m:
        days = int(m.group(1))
        return days, f"{days} ngày"

    return None, raw

def normalize_solution_dosage(text: Optional[str]) -> Optional[str]:
    """
    Chuẩn hóa nồng độ & lượng dùng thuốc BVTV / phân bón:
    - Chuyển số chữ tiếng Việt → số Ả Rập trong toàn bộ chuỗi
    - Chuẩn hoá đơn vị (lít, ml, chai, gói, bao, kg...)
    - Giữ nguyên cấu trúc phức như "X chai pha Y lít nước", "X ml bình Y lít"
    Ví dụ:
      "hai chai pha bốn trăm lít nước"  → "2 chai pha 400 lít nước"
      "hai lít rưỡi"                    → "2.5 lít"
      "20ml bình 16 lít"                → "20 ml bình 16 lít"
      "một gói"                         → "1 gói"
      "năm trăm gam"                    → "500 g"
    """
    if not text or not str(text).strip():
        return None

    raw = str(text).strip()
    try:
        norm = normalize_text(raw)
    except Exception:
        norm = raw

    # Lowercase toàn bộ sau khi chuyển số chữ → số Ả Rập
    norm = norm.strip().lower()

    # Xử lý "X rưỡi" → "X.5"
    norm = re.sub(r"(\d+)\s+rưỡi\b", r"\1.5", norm, flags=re.IGNORECASE)
    norm = re.sub(r"\bnửa\s+(\w+)", r"0.5 \1", norm, flags=re.IGNORECASE)

    # Tách số dính liền đơn vị: "20ml" → "20 ml", "30g" → "30 g"
    norm = re.sub(r"(\d)(ml|lít|lit|kg|g|mg)\b", r"\1 \2", norm)

    UNIT_NORM = {
        "lít": "lít", "lit": "lít", "l": "lít",
        "mililit": "ml", "mililít": "ml", "cc": "ml", "ml": "ml",
        "gam": "g", "gram": "g",
        "kg": "kg", "ký": "kg", "kí": "kg", "kilôgam": "kg", "kilogam": "kg", "kilo": "kg",
        "tấn": "tấn", "tạ": "tạ", "yến": "yến",
        "bao": "bao", "gói": "gói", "chai": "chai",
        "can": "can", "thùng": "thùng", "lọ": "lọ", "ống": "ống",
        "viên": "viên",
    }

    def _norm_segment(segment: str) -> str:
        """Chuẩn hoá một đoạn 'số + đơn_vị' trong segment (đã lowercase)."""
        s = segment.strip()
        if not s:
            return s
        # Thay dấu phẩy thập phân
        s = re.sub(r"(\d),(\d)", r"\1.\2", s)
        # Chuẩn hoá đơn vị
        for u_raw, u_std in UNIT_NORM.items():
            s = re.sub(r"(?<!\w)" + re.escape(u_raw) + r"(?!\w)", u_std, s)
        # Đảm bảo khoảng trắng giữa số và đơn vị
        s = re.sub(r"(\d)\s*(lít|ml|g|kg|tấn|tạ|yến|bao|gói|chai|can|thùng|lọ|ống|viên)\b", r"\1 \2", s)
        # Gọn khoảng trắng
        s = re.sub(r"\s+", " ", s)
        return s.strip()

    # Tách theo từ liên kết: pha, cho, trên, mỗi, /
    # "bình" KHÔNG tách vì "bình 16 lít" là một đơn vị đo ("bình 16 lít nước")
    SPLIT_PATTERN = r"(\bpha\b|\bcho\b|\btrên\b|\bmỗi\b|/)"
    parts = re.split(SPLIT_PATTERN, norm, flags=re.IGNORECASE)
    result_parts = []
    for part in parts:
        lower_p = part.strip().lower()
        if lower_p in ("pha", "cho", "trên", "mỗi", "/"):
            result_parts.append(f" {lower_p} ")
        else:
            result_parts.append(_norm_segment(part))

    result = "".join(result_parts).strip()
    result = re.sub(r"\s{2,}", " ", result)
    return result if result else None



def normalize_vietgap_tables(tables: Dict[str, Any]) -> Dict[str, Any]:
    """
    Chuẩn hóa tự động toàn diện tất cả các trường trong 4 bảng VietGAP:
    - Bảng 1.5: b1_5_material_purchases
    - Bảng 1.6: b1_6_material_usages
    - Bảng 1.7: b1_7_waste_treatments
    - Bảng 1.8: b1_8_harvest_sales
    """
    if not isinstance(tables, dict):
        return {}

    normalized = {}

    # 1. BẢNG 1.5: MUA VẬT TƯ
    row_1_5 = tables.get("b1_5_material_purchases") or tables.get("material_purchases") or tables.get("input_purchases") or tables.get("purchase")
    if row_1_5 and isinstance(row_1_5, dict):
        item = dict(row_1_5)
        item["purchase_date"] = to_iso_date(item.get("purchase_date") or item.get("ngay_thuc_hien")) or date.today().isoformat()
        name = item.get("material_name") or item.get("ten_vat_tu") or item.get("ten_vat_tu_nhap")
        item["material_name"] = clean_material_name(name)
        qty_raw = item.get("quantity") or item.get("so_luong_vat_tu")
        unit_raw = item.get("unit") or item.get("don_vi")
        combined_qty = f"{qty_raw or ''} {unit_raw or ''}".strip()
        num_val, unit_val, formatted = normalize_quantity_and_unit(combined_qty)
        if num_val is not None:
            item["quantity"] = num_val
        if unit_val:
            item["unit"] = unit_val
        elif unit_raw:
            item["unit"] = unit_raw

        mat_type = item.get("material_type") or item.get("loai_vat_tu")
        if mat_type:
            mat_type_upper = str(mat_type).upper()
            if mat_type_upper in ("FERTILIZER", "PESTICIDE", "CHEMICAL", "SEED", "OTHER"):
                item["material_type"] = mat_type_upper
            elif mat_type_upper == "SEEDLING":
                item["material_type"] = "SEED"
            else:
                for k, v in MATERIAL_TYPE_MAPPING.items():
                    if k in str(mat_type).lower():
                        item["material_type"] = v
                        break

        # Suy luận đơn vị tính nếu câu nói chưa gắn đơn vị
        if not item.get("unit"):
            m_name = str(item.get("material_name") or "").lower()
            m_type = str(item.get("material_type") or "").upper()
            if m_type == "SEED" or "cây giống" in m_name or "cây con" in m_name:
                item["unit"] = "cây"
            elif "hạt giống" in m_name or "hạt" in m_name:
                item["unit"] = "hạt"
            elif "phân" in m_name:
                item["unit"] = "bao"
            elif "bạt" in m_name or "ống" in m_name:
                item["unit"] = "cuộn"

        final_unit = item.get("unit")
        if formatted and any(c.isalpha() for c in formatted.split()[-1:]):
            item["so_luong_vat_tu"] = formatted
        elif item.get("quantity") is not None:
            num_display = f"{int(item['quantity'])}" if isinstance(item['quantity'], float) and item['quantity'].is_integer() else f"{item['quantity']}"
            item["so_luong_vat_tu"] = f"{num_display} {final_unit}".strip() if final_unit else num_display
        else:
            item["so_luong_vat_tu"] = qty_raw

        sup = item.get("supplier_name") or item.get("noi_cung_cap") or item.get("noi_mua")
        if not sup or str(sup).strip().lower() in ("null", "none"):
            raw_text = item.get("raw_text") or tables.get("raw_text") or ""
            if raw_text:
                m_sup = re.search(r"\b(?:tại|ở|từ)\s+([A-ZÀ-Ỹa-zà-ỹ0-9\s]+?)(?:$|[,\.;]|\bvới\b|\bgiá\b)", raw_text, re.IGNORECASE)
                if m_sup:
                    cand = m_sup.group(1).strip()
                    if len(cand) >= 3 and not any(k in cand.lower() for k in ["hôm nay", "hôm qua", "vườn", "ruộng", "nhà"]):
                        sup = cand
        if sup:
            sup = re.sub(r"\b(?:hsd|hạn\s+(?:sử\s+dụng|dùng|sd)?|date).*$", "", sup, flags=re.IGNORECASE).strip()
        item["supplier_name"] = normalize_vietnamese_title(sup)
        exp = item.get("expiry_date") or item.get("han_su_dung")
        if not exp or str(exp).strip().lower() in ("null", "none"):
            raw_text = item.get("raw_text") or tables.get("raw_text") or ""
            if raw_text:
                exp = extract_spoken_expiry_date(raw_text)
        if exp:
            exp_iso = to_iso_date(exp) or exp
            item["expiry_date"] = exp_iso
            item["han_su_dung"] = exp_iso
        else:
            item["expiry_date"] = None
            item["han_su_dung"] = None
        item.pop("batch_no", None)
        item.pop("so_lo", None)

        normalized["b1_5_material_purchases"] = item
        normalized["material_purchases"] = item
        normalized["purchase"] = item

    # 2. BẢNG 1.6: SỬ DỤNG PHÂN BÓN, THUỐC BVTV & HÓA CHẤT
    # 2a. Hoạt động phun thuốc BVTV (spray / pesticide_applications)
    row_spray = tables.get("spray") or tables.get("pesticide_applications")
    if row_spray and isinstance(row_spray, dict):
        item_spray = dict(row_spray)
        item_spray["activity_date"] = to_iso_date(item_spray.get("activity_date") or item_spray.get("ngay_thuc_hien")) or date.today().isoformat()
        if item_spray.get("ten_thuoc_bvtv"):
            item_spray["ten_thuoc_bvtv"] = normalize_vietnamese_title(item_spray.get("ten_thuoc_bvtv"))

        pest = item_spray.get("sinh_vat_gay_hai") or item_spray.get("target_pest")
        if pest:
            p_str = str(pest).strip()
            if any(k in p_str.lower() for k in ["không có", "không phát hiện", "không ghi nhận", "sạch", "không"]):
                item_spray["sinh_vat_gay_hai"] = "Không phát hiện"
                item_spray["target_pest"] = "Không phát hiện"
            else:
                item_spray["sinh_vat_gay_hai"] = normalize_vietnamese_title(p_str)
                item_spray["target_pest"] = item_spray["sinh_vat_gay_hai"]

        conc = item_spray.get("nong_do_va_luong_dung") or item_spray.get("lieu_luong_thuoc")
        if conc:
            item_spray["nong_do_va_luong_dung"] = normalize_solution_dosage(conc)

        iso_days, iso_text = normalize_isolation_days(item_spray.get("thoi_gian_cach_ly"))
        if iso_text:
            item_spray["thoi_gian_cach_ly"] = iso_text
        if iso_days is not None:
            item_spray["phi_days"] = iso_days

        gst = item_spray.get("growth_stage")
        if gst:
            item_spray["growth_stage"] = normalize_growth_stage(gst)

        normalized["spray"] = item_spray
        normalized["pesticide_applications"] = item_spray

    # 2b. Hoạt động bón phân (fertilize / fertilizer_applications)
    row_fert = tables.get("fertilize") or tables.get("fertilizer_applications")
    if row_fert and isinstance(row_fert, dict):
        item_fert = dict(row_fert)
        item_fert["activity_date"] = to_iso_date(item_fert.get("activity_date") or item_fert.get("ngay_thuc_hien")) or date.today().isoformat()
        if item_fert.get("ten_phan_bon"):
            item_fert["ten_phan_bon"] = normalize_vietnamese_title(item_fert.get("ten_phan_bon"))
        f_qty = item_fert.get("luong_phan_bon")
        if f_qty:
            # Nếu là chuỗi phức (pha, bình...) → dùng normalize_solution_dosage giữ đầy đủ
            # Nếu là số đơn giản → normalize_solution_dosage vẫn xử lý tốt
            item_fert["luong_phan_bon"] = normalize_solution_dosage(f_qty) or f_qty
        m_bon = item_fert.get("phuong_phap_bon")
        if m_bon:
            m_lower = str(m_bon).lower()
            for k, v in METHOD_MAPPING.items():
                if k in m_lower:
                    item_fert["phuong_phap_bon"] = v
                    break
        gst = item_fert.get("growth_stage")
        if gst:
            item_fert["growth_stage"] = normalize_growth_stage(gst)

        normalized["fertilize"] = item_fert
        normalized["fertilizer_applications"] = item_fert

    # 2c. Bảng 1.6 tổng hợp (b1_6_material_usages / material_usages)
    row_1_6 = tables.get("b1_6_material_usages") or tables.get("material_usages")
    if not row_1_6:
        comb_1_6 = {}
        if "fertilize" in normalized:
            comb_1_6.update(normalized["fertilize"])
        if "spray" in normalized:
            comb_1_6.update(normalized["spray"])
        if comb_1_6:
            row_1_6 = comb_1_6
    elif not row_1_6 and ("cultivation_diaries" in tables):
        cd_data = tables["cultivation_diaries"]
        if isinstance(cd_data, dict) and (cd_data.get("ten_phan_bon") or cd_data.get("ten_thuoc_bvtv")):
            row_1_6 = cd_data

    if row_1_6 and isinstance(row_1_6, dict):
        item = dict(row_1_6)
        item["activity_date"] = to_iso_date(item.get("activity_date") or item.get("ngay_thuc_hien")) or date.today().isoformat()
        if item.get("ten_phan_bon"):
            item["ten_phan_bon"] = normalize_vietnamese_title(item.get("ten_phan_bon"))
        f_qty = item.get("luong_phan_bon")
        if f_qty:
            item["luong_phan_bon"] = normalize_solution_dosage(f_qty) or f_qty
        m_bon = item.get("phuong_phap_bon")
        if m_bon:
            m_lower = str(m_bon).lower()
            for k, v in METHOD_MAPPING.items():
                if k in m_lower:
                    item["phuong_phap_bon"] = v
                    break
        if item.get("ten_thuoc_bvtv"):
            item["ten_thuoc_bvtv"] = normalize_vietnamese_title(item.get("ten_thuoc_bvtv"))

        pest = item.get("sinh_vat_gay_hai") or item.get("target_pest")
        if pest:
            p_str = str(pest).strip()
            if any(k in p_str.lower() for k in ["không có", "không phát hiện", "không ghi nhận", "sạch", "không"]):
                item["sinh_vat_gay_hai"] = "Không phát hiện"
                item["target_pest"] = "Không phát hiện"
            else:
                item["sinh_vat_gay_hai"] = normalize_vietnamese_title(p_str)
                item["target_pest"] = item["sinh_vat_gay_hai"]

        conc = item.get("nong_do_va_luong_dung") or item.get("lieu_luong_thuoc")
        if conc:
            item["nong_do_va_luong_dung"] = normalize_solution_dosage(conc)
        iso_days, iso_text = normalize_isolation_days(item.get("thoi_gian_cach_ly"))
        if iso_text:
            item["thoi_gian_cach_ly"] = iso_text
        if iso_days is not None:
            item["phi_days"] = iso_days

        gst = item.get("growth_stage")
        if gst:
            item["growth_stage"] = normalize_growth_stage(gst)

        normalized["b1_6_material_usages"] = item
        normalized["material_usages"] = item
        if "spray" not in normalized and item.get("ten_thuoc_bvtv"):
            normalized["spray"] = item
            normalized["pesticide_applications"] = item
        if "fertilize" not in normalized and item.get("ten_phan_bon"):
            normalized["fertilize"] = item
            normalized["fertilizer_applications"] = item

    # 3. BẢNG 1.7: THU GOM VÀ XỬ LÝ BAO BÌ, THUỐC DƯ THỪA
    row_1_7 = tables.get("b1_7_waste_treatments") or tables.get("waste_treatments") or tables.get("waste")
    if row_1_7 and isinstance(row_1_7, dict):
        item = dict(row_1_7)
        item["treatment_date"] = to_iso_date(item.get("treatment_date") or item.get("ngay_thuc_hien")) or date.today().isoformat()

        # Dọn sạch artifact từ prompt
        for k in ["waste_type", "loai_bao_bi_thuoc_du", "storage_location", "noi_ton_tru", "treatment_method", "cach_xu_ly", "material_name"]:
            if item.get(k) and any(art in str(item[k]).lower() for art in ["cụm thời gian", "null nếu", "cách thức", "nơi lưu", "loại bao"]):
                item[k] = None

        raw_text = item.get("raw_text") or tables.get("raw_text") or ""
        w_type = item.get("waste_type") or item.get("loai_bao_bi_thuoc_du")
        if not w_type and raw_text:
            m_w = re.search(r"(?:vỏ\s+bao\s+bì(?:\s+chai\s+lọ)?|chai\s+lọ|vỏ\s+chai|bao\s+bì|rác\s+thải|túi\s+nilon)", raw_text, re.IGNORECASE)
            if m_w:
                w_type = m_w.group(0)
        item["loai_bao_bi_thuoc_du"] = normalize_vietnamese_title(w_type)
        item["waste_type"] = item["loai_bao_bi_thuoc_du"]

        storage = item.get("storage_location") or item.get("noi_ton_tru")
        if not storage and raw_text:
            m_st = re.search(r"(?:tại|ở|vào|về)\s+([a-zA-ZÀ-ỹ0-9\s]+?(?:hố|thùng|kho|bể|khu)[a-zA-ZÀ-ỹ0-9\s]*?)(?:$|[,\.;]|\bđể\b|\bvới\b)", raw_text, re.IGNORECASE)
            if m_st:
                storage = m_st.group(1).strip()
        item["noi_ton_tru"] = normalize_vietnamese_title(storage)
        item["storage_location"] = item["noi_ton_tru"]

        method = item.get("treatment_method") or item.get("cach_xu_ly")
        if not method and raw_text:
            m_m = re.search(r"(?:để|bằng\s+cách)\s+([a-zA-ZÀ-ỹ0-9\s]+?(?:tiêu\s+hủy|thu\s+gom|chôn|đốt|xử\s+lý)[a-zA-ZÀ-ỹ0-9\s]*?)(?:$|[,\.;])", raw_text, re.IGNORECASE)
            if m_m:
                method = m_m.group(1).strip()
        if method:
            item["cach_xu_ly"] = normalize_vietnamese_title(str(method).strip())
            item["treatment_method"] = item["cach_xu_ly"]

        normalized["b1_7_waste_treatments"] = item
        normalized["waste_treatments"] = item
        normalized["waste"] = item

    # 4. BẢNG 1.8: THU HOẠCH NÔNG SẢN
    row_harvest = tables.get("b1_8_harvests") or tables.get("harvests") or tables.get("harvest") or tables.get("b1_8_harvest_sales")
    if row_harvest and isinstance(row_harvest, dict):
        item_h = dict(row_harvest)
        item_h["harvest_date"] = to_iso_date(item_h.get("harvest_date") or item_h.get("ngay_thuc_hien") or item_h.get("date")) or date.today().isoformat()
        item_h["ngay_thuc_hien"] = item_h["harvest_date"]
        c_name = item_h.get("crop_name") or item_h.get("loai_nong_san")
        if c_name:
            item_h["crop_name"] = normalize_vietnamese_title(c_name)
            item_h["loai_nong_san"] = item_h["crop_name"]
        h_qty = item_h.get("san_luong_thu_hoach") or item_h.get("quantity") or item_h.get("harvest_quantity")
        if h_qty:
            _, _, h_fmt = normalize_quantity_and_unit(h_qty)
            item_h["san_luong_thu_hoach"] = h_fmt or h_qty
        proc = item_h.get("dia_diem_cach_so_che") or item_h.get("processing_method") or item_h.get("cach_so_che")
        if proc:
            item_h["dia_diem_cach_so_che"] = normalize_vietnamese_title(proc)

        # Xóa các trường bán sản phẩm khỏi bảng thu hoạch
        item_h.pop("sale_date", None)
        item_h.pop("thoi_gian_xuat_ban", None)
        item_h.pop("khoi_luong_tieu_thu", None)
        item_h.pop("khoi_luong_ban", None)
        item_h.pop("khoi_luong_ieu_thu", None)
        item_h.pop("co_so_thu_mua", None)
        item_h.pop("co_so_thu_ma", None)
        item_h.pop("buyer_name", None)

        normalized["b1_8_harvests"] = item_h
        normalized["harvests"] = item_h
        normalized["harvest"] = item_h

    # 5. BẢNG 1.9: TIÊU THỤ / BÁN SẢN PHẨM
    row_sale = tables.get("b1_9_sales") or tables.get("sales") or tables.get("sale") or tables.get("b1_8_harvest_sales")
    if row_sale and isinstance(row_sale, dict):
        item_s = dict(row_sale)
        item_s["sale_date"] = to_iso_date(item_s.get("sale_date") or item_s.get("thoi_gian_xuat_ban") or item_s.get("ngay_thuc_hien") or item_s.get("date")) or date.today().isoformat()
        item_s["thoi_gian_xuat_ban"] = item_s["sale_date"]
        item_s["ngay_thuc_hien"] = item_s["sale_date"]
        c_name = item_s.get("crop_name") or item_s.get("loai_nong_san")
        if c_name:
            item_s["crop_name"] = normalize_vietnamese_title(c_name)
            item_s["loai_nong_san"] = item_s["crop_name"]
        buyer = (
            item_s.get("co_so_thu_mua")
            or item_s.get("buyer_name")
            or item_s.get("nguoi_mua_thuong_lai")
            or item_s.get("co_so_thu_ma")
            or item_s.get("noi_mua")
        )
        if not buyer or str(buyer).strip().lower() in ("null", "none"):
            raw_text = item_s.get("raw_text") or tables.get("raw_text") or ""
            if raw_text:
                m_buyer = re.search(r"\b(?:bán cho|cho|xuất cho)\s+([A-ZÀ-Ỹa-zà-ỹ0-9\s]+?)(?:$|[,\.;]|\bvới\b|\bgiá\b)", raw_text, re.IGNORECASE)
                if m_buyer:
                    cand = m_buyer.group(1).strip()
                    if len(cand) >= 3 and not any(k in cand.lower() for k in ["hôm nay", "hôm qua", "vườn", "ruộng"]):
                        buyer = cand
        if buyer:
            item_s["co_so_thu_mua"] = normalize_vietnamese_title(buyer)
            item_s["buyer_name"] = item_s["co_so_thu_mua"]
        s_qty = (
            item_s.get("khoi_luong_tieu_thu")
            or item_s.get("khoi_luong_ban")
            or item_s.get("khoi_luong_ieu_thu")
            or item_s.get("quantity")
            or item_s.get("sale_quantity")
        )
        if s_qty:
            _, _, s_fmt = normalize_quantity_and_unit(s_qty)
            item_s["khoi_luong_tieu_thu"] = s_fmt or s_qty
            item_s["khoi_luong_ban"] = item_s["khoi_luong_tieu_thu"]

        # Xóa các trường lỗi chính tả / token artifact từ LLM
        item_s.pop("khoi_luong_ieu_thu", None)
        item_s.pop("co_so_thu_ma", None)

        # Xóa các trường thu hoạch khỏi bảng bán
        item_s.pop("harvest_date", None)
        item_s.pop("san_luong_thu_hoach", None)
        item_s.pop("dia_diem_cach_so_che", None)
        item_s.pop("processing_method", None)

        normalized["b1_9_sales"] = item_s
        normalized["sales"] = item_s
        normalized["sale"] = item_s

    # Giữ b1_8_harvest_sales gộp nếu cần tương thích ngược
    if ("b1_8_harvests" in normalized) or ("b1_9_sales" in normalized):
        hs_merged = {}
        if "b1_8_harvests" in normalized:
            hs_merged.update(normalized["b1_8_harvests"])
        if "b1_9_sales" in normalized:
            hs_merged.update(normalized["b1_9_sales"])
        normalized["b1_8_harvest_sales"] = hs_merged

    # 6. CULTIVATION_DIARIES (CÔNG VIỆC CANH TÁC KHÁC: DỌN CỎ, LÀM ĐẤT, CẮT TỈA CÀNH, TƯỚI NƯỚC, BAO TRÁI...)
    row_cd = tables.get("cultivation_diaries") or tables.get("cultivation_activities") or tables.get("general")
    if row_cd and isinstance(row_cd, dict):
        item = dict(row_cd)
        item["activity_date"] = to_iso_date(item.get("activity_date") or item.get("ngay_thuc_hien")) or date.today().isoformat()
        c_viec = item.get("cong_viec") or item.get("task_name") or item.get("description")
        item["cong_viec"] = clean_work_name(c_viec)
        item["description"] = item["cong_viec"]
        item["task_name"] = item["cong_viec"]

        # Chuẩn hoá sinh vật gây hại phát hiện (mặc định 'Không phát hiện' nếu không có hoặc nhắc tới không có sâu bệnh)
        pest = item.get("target_pest") or item.get("sinh_vat_gay_hai") or item.get("pest_name")
        if pest:
            p_str = str(pest).strip()
            if any(k in p_str.lower() for k in ["không có", "không phát hiện", "không ghi nhận", "sạch", "không"]):
                item["target_pest"] = "Không phát hiện"
                item["sinh_vat_gay_hai"] = "Không phát hiện"
            else:
                item["target_pest"] = normalize_vietnamese_title(p_str)
                item["sinh_vat_gay_hai"] = item["target_pest"]
        else:
            raw_t = f"{item.get('raw_text', '')} {c_viec or ''} {item.get('description', '')}".lower()
            if any(k in raw_t for k in ["không có sinh vật gây hại", "không phát hiện sinh vật gây hại", "không ghi nhận sinh vật gây hại", "sạch sâu bệnh"]):
                item["target_pest"] = "Không phát hiện"
                item["sinh_vat_gay_hai"] = "Không phát hiện"
        qty = item.get("so_luong") or item.get("so_luong_lieu_luong") or item.get("quantity")
        if qty:
            clean_q = str(qty).strip()
            if clean_q.lower() in ("null", "none", "") or not re.search(r"\d", clean_q):
                item["so_luong"] = None
                item["so_luong_lieu_luong"] = None
                item["quantity"] = None
            else:
                try:
                    norm_q = normalize_text(clean_q)
                    m_range = re.search(r"(\d+)\s*(?:đến|-)\s*(\d+)\s*(?:người|lao động)?", norm_q, flags=re.IGNORECASE)
                    if m_range:
                        clean_q = f"{m_range.group(1)}-{m_range.group(2)} người"
                    else:
                        _, _, q_fmt = normalize_quantity_and_unit(clean_q)
                        if q_fmt:
                            clean_q = q_fmt
                        else:
                            clean_q = norm_q.lower()
                except Exception:
                    pass
                item["so_luong"] = clean_q
                item["so_luong_lieu_luong"] = clean_q
                item["quantity"] = clean_q
        else:
            item["so_luong"] = None
            item["so_luong_lieu_luong"] = None
            item["quantity"] = None

        # Bỏ activity_type theo yêu cầu
        item.pop("activity_type", None)
        item.pop("loai_hoat_dong", None)

        gst = item.get("growth_stage") or item.get("giai_doan")
        if gst:
            item["growth_stage"] = normalize_growth_stage(gst)
            item["giai_doan"] = item.get("growth_stage")

        # Bỏ method_equipment và area_or_target theo yêu cầu
        item.pop("method_equipment", None)
        item.pop("area_or_target", None)
        item.pop("thiet_bi", None)
        item.pop("phuong_phap", None)
        item.pop("khu_vuc", None)
        item.pop("dien_tich", None)

        normalized["cultivation_diaries"] = item
        normalized["cultivation_activities"] = item
        normalized["general"] = item

    # 7. OBSERVATION (QUAN SÁT SÂU BỆNH / THEO DÕI SINH VẬT GÂY HẠI)
    row_obs = tables.get("observation") or tables.get("pest_observations") or tables.get("diary_pests")
    if row_obs and isinstance(row_obs, dict):
        item_o = dict(row_obs)
        item_o["activity_date"] = to_iso_date(item_o.get("activity_date") or item_o.get("ngay_thuc_hien") or item_o.get("observation_date")) or date.today().isoformat()
        pest = item_o.get("sinh_vat_gay_hai") or item_o.get("pest_name") or item_o.get("target_pest")
        if pest:
            p_str = str(pest).strip()
            if any(k in p_str.lower() for k in ["không có", "không phát hiện", "không ghi nhận", "sạch", "không"]):
                item_o["sinh_vat_gay_hai"] = "Không phát hiện"
                item_o["pest_name"] = "Không phát hiện"
                item_o["target_pest"] = "Không phát hiện"
            else:
                item_o["sinh_vat_gay_hai"] = normalize_vietnamese_title(p_str)
                item_o["pest_name"] = item_o["sinh_vat_gay_hai"]
                item_o["target_pest"] = item_o["sinh_vat_gay_hai"]
        else:
            item_o["sinh_vat_gay_hai"] = "Không phát hiện"
            item_o["pest_name"] = "Không phát hiện"
            item_o["target_pest"] = "Không phát hiện"

        item_o["muc_do"] = item_o.get("muc_do") or item_o.get("severity") or "Nhẹ"
        item_o["severity"] = item_o["muc_do"]
        item_o["action_taken"] = item_o.get("action_taken") or item_o.get("bien_phap_xu_ly") or item_o.get("notes") or "Theo dõi tiếp"

        gst = item_o.get("growth_stage") or item_o.get("giai_doan")
        if gst:
            item_o["growth_stage"] = normalize_growth_stage(gst)
            item_o["giai_doan"] = item_o.get("growth_stage")

        normalized["observation"] = item_o
        normalized["pest_observations"] = item_o
        normalized["diary_pests"] = item_o

    for k, v in tables.items():
        if k not in normalized and isinstance(v, dict):
            normalized[k] = v

    return normalized
