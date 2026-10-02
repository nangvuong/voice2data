import re
import json
import os

# Load file cấu hình từ vựng
KEYWORDS_FILE = os.path.join(os.path.dirname(__file__), "keywords.json")

def _load_anchor_map():
    if not os.path.exists(KEYWORDS_FILE):
        return {}
    with open(KEYWORDS_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)
        
    raw_map = {}
    for key, words in config.items():
        for w in words:
            raw_map[w.lower()] = key
            
    # Sort keys theo độ dài giảm dần để ưu tiên match từ ghép dài trước
    sorted_map = {k: raw_map[k] for k in sorted(raw_map.keys(), key=len, reverse=True)}
    return sorted_map

# Load 1 lần khi khởi động module
ANCHOR_MAP = _load_anchor_map()

def _build_anchor_pattern(anchor_map):
    terms = []
    for k in anchor_map.keys():
        if k == "thuốc": terms.append(r"(?<!phun )(?<!phun)thuốc")
        elif k == "thuốc sâu": terms.append(r"(?<!phun )(?<!phun)thuốc sâu")
        elif k == "phân": terms.append(r"(?<!bón )(?<!bón)phân")
        elif k == "phân bón": terms.append(r"(?<!bón )(?<!bón)phân bón")
        else: terms.append(re.escape(k))
    if not terms:
        return re.compile(r"^\b$")
    pattern_str = r"\b(" + "|".join(terms) + r")\b"
    return re.compile(pattern_str, re.IGNORECASE)

ANCHOR_PATTERN = _build_anchor_pattern(ANCHOR_MAP)
EMPTY_VALUE_KEYS = ["hôm nay", "hôm qua", "hôm kia", "bữa nay", "sáng nay", "chiều nay", "ngày mai", "mốt", "kia", "sáng mai", "chiều mai", "tối nay", "tối qua"]

def parse_with_regex(text: str) -> str | None:
    """
    Trích xuất text thành JSON với 8 trường chuẩn như trong loi_ke_trich_xuat_giong_noi.jsonl.
    """
    text_lower = text.lower()
    
    # Extract ngay_thang
    ngay_thang = "không"
    ngay_match = re.search(r'(hôm nay|mấy hôm nay|mấy ngày nay|từ nay đến cuối tháng|mấy hôm đầu tháng|nửa tháng nay|cuối tháng|cả tuần nay|tháng này|sang tháng này|đầu năm|cả tháng này|mấy hôm mười tám.*|hai hôm mười sáu.*|mấy hôm hai mươi bốn.*)', text_lower)
    if ngay_match:
        ngay_thang = ngay_match.group(1)
        
    # Bỏ tập regex mẫu việc cv_patterns cố định theo yêu cầu
    cong_viec = "không"
    giai_doan = "không ghi"

    # Sinh vat
    sv_list = []
    if re.search(r'sâu ăn lá', text_lower): sv_list.append("Sâu ăn lá")
    if re.search(r'rầy nâu', text_lower): sv_list.append("Rầy nâu")
    sinh_vat = ", ".join(sv_list) if sv_list else "không"
    
    # Thuoc/phan
    thuoc_list = []
    if re.search(r'phân chuồng', text_lower): thuoc_list.append("Phân chuồng")
    if re.search(r'npk', text_lower): thuoc_list.append("NPK")
    if re.search(r'nano ecotech', text_lower): thuoc_list.append("Thuốc sinh học nano ecotech")
    if re.search(r'top 1', text_lower): thuoc_list.append("Top 1")
    if re.search(r'phân gà', text_lower): thuoc_list.append("Phân gà")
    thuoc = ", ".join(thuoc_list) if thuoc_list else "không"
    
    # Luong su dung
    luong = "không"
    luong_match2 = re.search(r'((một|hai|ba|bốn|năm|sáu|bảy|tám|chín|mười|lăm|mươi|\d)+\s*(nghìn|trăm|triệu)?\s*(cây|lít|tạ|cân|kg).*?(hecta|ha)?)', text_lower)
    if luong_match2:
        luong = luong_match2.group(1).strip()
            
    # Noi mua
    noi_mua = "không"
    noi_match = re.search(r'(công ty [a-z0-9 ]+|thương lái)', text_lower)
    if noi_match:
        noi_mua = noi_match.group(1).title()
        
    # Cach ly
    cach_ly = "không"
    cach_ly_match = re.search(r'cách ly (.*?)(?= mới| )', text_lower)
    if cach_ly_match:
        cach_ly = cach_ly_match.group(1)
        
    data = {
        "ngay_thang": ngay_thang,
        "giai_doan_sinh_truong": giai_doan,
        "cong_viec": cong_viec,
        "sinh_vat_gay_hai_phat_hien": sinh_vat,
        "ten_phan_thuoc_bvtv": thuoc,
        "luong_su_dung": luong,
        "noi_mua": noi_mua,
        "thoi_gian_cach_ly": cach_ly
    }
    
    # Nếu không nhận diện được bất kỳ thông tin gì thì trả về None
    if ngay_thang == "không" and cong_viec == "không" and luong == "không" and thuoc == "không":
        return None
        
    return json.dumps({"trich_xuat": data}, ensure_ascii=False)




