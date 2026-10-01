"""

Các trường chuẩn hóa:
  - ngay_thuc_hien: Quy đổi ngày tương đối → DD/MM/YYYY
  - loai_hoat_dong: Validate enum loai_hoat_dong
  - don_vi:         Chuẩn hóa đơn vị đo lường → ký hiệu chuẩn
  - lieu_luong:     Đảm bảo kiểu number
  - Các trường text: UPPERCASE

LƯU Ý (hợp đồng "1 câu = 1 hoạt động"):
  LLM giờ trả về ĐÚNG MỘT object hoạt động cho mỗi câu, không còn bọc
  trong mảng "activities". Các hàm normalize_* trong module này vẫn giữ
  khả năng nhận vào list (để tương thích ngược / batch xử lý nhiều câu),
  nhưng normalize_activity_json giờ còn nhận thêm trực tiếp MỘT object
  hoạt động đơn — trường hợp phổ biến nhất khi gọi 1 lần cho 1 câu.
"""

from datetime import datetime, timedelta
import json
import logging

from services.normalizer.dates import parse_date

logger = logging.getLogger(__name__)

# No Bưởi-specific enums are used for Cà Gai Leo

# Các trường hợp lệ của một object hoạt động, dùng để nhận diện object đơn
# (khi normalize_activity_json phải đoán xem dict đầu vào là 1 hoạt động
# hay là một wrapper khác).
_ACTIVITY_KEYS = {
    "ngay_thuc_hien", "giai_doan", "giai_doan_sinh_truong", "cong_viec",
    "sinh_vat_gay_hai", "ten_phan_thuoc", "luong_su_dung",
    "noi_mua", "thoi_gian_cach_ly",
}


def complete_missing_date_parts(val_str: str, now: datetime) -> str:
    """
    Bổ sung các cấp thời gian cao hơn bị thiếu:
    - Chỉ có Ngày -> thêm Tháng và Năm hiện tại.
    - Có Ngày/Tháng -> thêm Năm hiện tại.
    - Khoảng ngày hoặc danh sách ngày -> chuẩn hóa từng phần tử.
    """
    if not val_str:
        return val_str

    # 1. Trường hợp khoảng ngày: "DD/MM - DD/MM" hoặc "DD/MM/YYYY - DD/MM/YYYY"
    if " - " in val_str:
        parts = val_str.split(" - ")
        return " - ".join(complete_missing_date_parts(p.strip(), now) for p in parts)

    # 2. Trường hợp danh sách ngày: "DD/MM, DD/MM"
    if ", " in val_str:
        parts = val_str.split(", ")
        return ", ".join(complete_missing_date_parts(p.strip(), now) for p in parts)

    clean_str = val_str.replace("-", "/").replace(".", "/").strip()
    parts = [p.strip() for p in clean_str.split("/") if p.strip()]

    # Trường hợp chỉ có 1 số (chỉ có Ngày) -> thêm Tháng và Năm hiện tại
    if len(parts) == 1 and parts[0].isdigit():
        day = int(parts[0])
        if 1 <= day <= 31:
            return f"{day:02d}/{now.month:02d}/{now.year:04d}"

    # Trường hợp có 2 số (Ngày và Tháng) -> thêm Năm hiện tại
    elif len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        day = int(parts[0])
        month = int(parts[1])
        if 1 <= day <= 31 and 1 <= month <= 12:
            return f"{day:02d}/{month:02d}/{now.year:04d}"

    # Trường hợp có 3 số (Ngày, Tháng, Năm) -> chuẩn hóa format DD/MM/YYYY
    elif len(parts) == 3 and all(p.isdigit() for p in parts):
        day = int(parts[0])
        month = int(parts[1])
        year = int(parts[2])
        if year < 100:
            year += 2000
        if 1 <= day <= 31 and 1 <= month <= 12 and 1900 <= year <= 2100:
            return f"{day:02d}/{month:02d}/{year:04d}"

    return val_str


import re
from services.normalizer.core.tokenizer import Tokenizer
from services.normalizer.core.parser import NumberParser


def normalize_text(text: str) -> str:
    """Chuẩn hóa văn bản số (chuyển chữ số tiếng Việt thành số Ả Rập và chuẩn hóa đơn vị)."""
    if not text:
        return text
    tokens = Tokenizer.tokenize(text)
    return NumberParser.evaluate_number_tokens(tokens)


def normalize_relative_date(date_str: str) -> str | None:
    """
    Quy đổi từ chỉ thời gian tương đối (hôm qua, hôm nay, ngày mai...),
    chuỗi ngày tháng bằng chữ/số, khoảng ngày và nhiều mốc ngày.
    """
    if not date_str or str(date_str).strip().upper() in ("NULL", "NONE", ""):
        return None

    val = str(date_str).strip()
    u = val.upper()
    now = datetime.now()

    # Nhóm từ khóa tương đối
    if u in ("HÔM NAY", "NAY", "TODAY", "HIỆN TẠI", "SÁNG NAY", "CHIỀU NAY", "TRƯA NAY", "TỐI NAY"):
        return now.strftime("%d/%m/%Y")
    if u in ("HÔM QUA", "HOM QUA", "HÔM QUA NAY", "HÔM RỒI", "HÔM TRƯỚC", "QUA", "YESTERDAY", "CHIỀU QUA", "SÁNG QUA", "TRƯA QUA", "TỐI QUA"):
        return (now - timedelta(days=1)).strftime("%d/%m/%Y")
    if u in ("HÔM KIA", "HOM KIA", "HÔM KÌA"):
        return (now - timedelta(days=2)).strftime("%d/%m/%Y")
    if u in ("NGÀY MAI", "MAI", "TOMORROW", "SÁNG MAI", "CHIỀU MAI"):
        return (now + timedelta(days=1)).strftime("%d/%m/%Y")
    if u in ("NGÀY KIA", "NGAY KIA", "MỐT"):
        return (now + timedelta(days=2)).strftime("%d/%m/%Y")

    # Kiểm tra bổ sung tháng/năm trên chuỗi số trực tiếp (VD: "15", "15/08", "15-8")
    completed = complete_missing_date_parts(u, now)
    if completed != u:
        return completed

    # Parse qua bộ phân tích ngày
    try:
        parsed = parse_date(val)
        if parsed and parsed.strip():
            return complete_missing_date_parts(parsed.strip().upper(), now)
    except Exception:
        pass

    return complete_missing_date_parts(u, now)


def normalize_unit(unit_str: str) -> str | None:
    pass  # Đơn vị hiện được chuẩn hóa tích hợp trong luong_su_dung


def normalize_activity_list(activities: list[dict]) -> list[dict]:
    """
    Chuẩn hóa toàn diện danh sách hoạt động canh tác nông nghiệp:
    - ngay_thuc_hien: Chuyển đổi ngày chữ/số, khoảng ngày, ngày tương đối -> DD/MM/YYYY hoặc khoảng ngày.
    - giai_doan_sinh_truong: Chuẩn hóa tên giai đoạn (CHUẨN BỊ ĐẤT, PHÁT TRIỂN, SINH TRƯỞNG...).
    - cong_viec: Loại bỏ từ đệm hành động, chuẩn hóa viết hoa.
    - sinh_vat_gay_hai: Lọc bỏ các câu phủ định ("không phát hiện sinh vật gây hại" -> None), chuẩn hóa tên sâu bệnh.
    - ten_phan_thuoc: Chuẩn hóa tên thuốc BVTV, phân bón (PHÂN NPK, NANO ECOTECH, TOP 1...).
    - luong_su_dung: Quy đổi chữ số sang số, chuẩn hóa đơn vị (KG, LÍT, HA, /HA, CÂY GIỐNG...).
    - noi_mua: Chuẩn hóa tên công ty, nhà cung cấp, thương lái (CÔNG TY TNHH, QUANG MINH TNA, MIND GROUP, SANDU...).
    - thoi_gian_cach_ly: Chuyển đổi ngày cách ly sang số ("14 NGÀY", nếu "không cách ly" -> None).
    """
    if not isinstance(activities, list):
        return activities

    normalized = []
    for item in activities:
        if not isinstance(item, dict):
            normalized.append(item)
            continue

        norm_item = {}
        for k, v in item.items():
            if v is None or str(v).strip().upper() in ("NULL", "NONE", ""):
                norm_item[k] = None
                continue

            v_str = str(v).strip()

            if k == "ngay_thuc_hien":
                norm_item[k] = normalize_relative_date(v_str)

            elif k == "sinh_vat_gay_hai":
                u = v_str.upper()
                negative_phrases = [
                    "KHÔNG PHÁT HIỆN SINH VẬT GÂY HẠI",
                    "KHÔNG CÓ SINH VẬT GÂY HẠI",
                    "KHÔNG GHI NHẬN SINH VẬT GÂY HẠI",
                    "KHÔNG PHÁT HIỆN",
                    "KHÔNG GHI NHẬN",
                    "KHÔNG CÓ",
                    "KHÔNG THẤY",
                    "KHÔNG GẶP",
                    "KHÔNG BỊ",
                    "BÌNH THƯỜNG",
                    "SẠCH BỆNH",
                ]
                is_neg = any(np in u for np in negative_phrases)
                if is_neg or u == "KHÔNG":
                    norm_item[k] = None
                else:
                    u = re.sub(r"\s+VÀ\s+", ", ", u)
                    u = re.sub(r"\s*,\s*", ", ", u)
                    norm_item[k] = u.strip()

            elif k == "thoi_gian_cach_ly":
                u = v_str.upper()
                if u in ("KHÔNG CÁCH LY", "KHÔNG", "KHÔNG CÓ", "0 NGÀY", "KHÔNG CẦN CÁCH LY"):
                    norm_item[k] = None
                else:
                    try:
                        norm = normalize_text(u)
                        norm_item[k] = norm.upper().strip()
                    except Exception:
                        norm_item[k] = u.strip()

            elif k == "luong_su_dung":
                try:
                    norm = normalize_text(v_str).upper()
                    norm = re.sub(r"\s*/\s*HA\b", "/HA", norm)
                    norm = re.sub(r"\s*/\s*SÀO\b", "/SÀO", norm)
                    norm = re.sub(r"\bKI\s+LÔ\s+GAM\b", "KG", norm)
                    norm = re.sub(r"\bKILOGAM\b", "KG", norm)
                    norm = re.sub(r"\bHÉC\s+TA\b", "HA", norm)
                    norm = re.sub(r"\bTRÊN\s+(?:1\s+)?HA\b", "/HA", norm)
                    norm = re.sub(r"\s+", " ", norm).strip()
                    norm_item[k] = norm
                except Exception:
                    norm_item[k] = v_str.upper()

            elif k == "ten_phan_thuoc":
                u = v_str.upper()
                u = re.sub(r"\bPHÂN\s+N\s+P\s+K\b", "PHÂN NPK", u)
                u = re.sub(r"\bN\s+P\s+K\b", "NPK", u)
                u = re.sub(r"\bTOP\s+MỘT\b", "TOP 1", u)
                u = re.sub(r"\bTỐP\s+MỘT\b", "TOP 1", u)
                u = re.sub(r"\s+VÀ\s+TOP\s+1\b", ", TOP 1", u)
                u = re.sub(r"\s+", " ", u).strip()
                norm_item[k] = u

            elif k == "noi_mua":
                u = v_str.upper()
                u = re.sub(r"\bCÔNG TY TRÁCH NHIỆM HỮU HẠN\b", "CÔNG TY TNHH", u)
                u = re.sub(r"\bCÔNG TY TNHH QUANG MINH T\s+N\s+A\b", "CÔNG TY TNHH QUANG MINH TNA", u)
                u = re.sub(r"\bQUANG MINH T\s+N\s+A\b", "QUANG MINH TNA", u)
                u = re.sub(r"\s+", " ", u).strip()
                norm_item[k] = u

            elif k in ("giai_doan", "giai_doan_sinh_truong"):
                u = v_str.upper()
                target_key = "giai_doan" if "giai_doan" in item or "giai_doan" not in item else k
                if "KHÔNG" in u:
                    norm_item[k] = None
                else:
                    u = re.sub(r"^(?:TRONG\s+GIAI\s+ĐOẠN|GIAI\s+ĐOẠN|CÂY\s+Ở\s+GIAI\s+ĐOẠN|CÂY\s+ĐANG|CÂY)\s+", "", u)
                    norm_item[k] = re.sub(r"\s+", " ", u).strip()

            elif k == "cong_viec":
                u = v_str.upper()
                u = re.sub(r"^(?:CÔNG\s+VIỆC\s+THỰC\s+HIỆN\s+LÀ|CÔNG\s+VIỆC\s+LÀ|TIẾN\s+HÀNH|THỰC\s+HIỆN|BẮT\s+ĐẦU|TIẾP\s+TỤC\s+VỚI\s+VIỆC|TIẾP\s+TỤC)\s+", "", u)
                norm_item[k] = re.sub(r"\s+", " ", u).strip()

            else:
                norm_item[k] = v_str.upper()

        normalized.append(norm_item)

    return normalized



def normalize_activity_json(llm_output: str) -> str:
    """
    Nhận JSON string từ LLM, chuyển thành object, chuẩn hóa và trả về JSON string.

    Hỗ trợ 3 dạng đầu vào (theo thứ tự ưu tiên nhận diện):
      1. Một MẢNG các hoạt động: [{"ngay_thuc_hien": ..., ...}, ...]
         -> chuẩn hóa toàn bộ danh sách (dùng cho batch nhiều câu).
      2. Một OBJECT ĐƠN chứa đúng 1 hoạt động (không có "activities"):
         {"ngay_thuc_hien": ..., "cong_viec": ..., ...}
         -> đây là dạng phổ biến nhất kể từ khi áp dụng hợp đồng
         "1 câu = 1 hoạt động"; hàm sẽ tự bọc vào list 1 phần tử rồi
         chuẩn hóa.
      3. Dạng wrapper cũ {"activities": [...]} (để tương thích ngược với
         output cũ hoặc trường hợp model lỡ trả nhầm định dạng):
         -> chỉ lấy phần tử ĐẦU TIÊN trong "activities" (đúng hợp đồng
         1 hoạt động), log cảnh báo nếu có nhiều hơn 1 phần tử.
    """
    try:
        data = json.loads(llm_output)

        if isinstance(data, list):
            norm_list = normalize_activity_list(data)
            return json.dumps(norm_list, ensure_ascii=False, indent=2)

        if isinstance(data, dict):
            if "activities" in data and isinstance(data["activities"], list):
                acts = data["activities"]
                if not acts:
                    logger.warning("Wrapper 'activities' rỗng, không có gì để chuẩn hóa.")
                    return llm_output
                if len(acts) > 1:
                    logger.warning(
                        "Nhận được %d hoạt động trong wrapper 'activities' dù chỉ hỗ trợ 1 hoạt động/câu, "
                        "chỉ lấy phần tử đầu tiên.",
                        len(acts),
                    )
                norm_list = normalize_activity_list([acts[0]])
                return json.dumps(norm_list, ensure_ascii=False, indent=2)

            # Object đơn: 1 hoạt động trực tiếp (dạng chuẩn mới)
            norm_list = normalize_activity_list([data])
            return json.dumps(norm_list, ensure_ascii=False, indent=2)

    except Exception as e:
        logger.warning(f"Lỗi parse JSON khi chuẩn hóa hoạt động canh tác: {e}")

    return llm_output


def normalize_for_webapp(activities: list[dict]) -> list[dict]:
    """
    Trả về cấu trúc phẳng cho nhật ký canh tác cà gai leo.
    """
    return activities