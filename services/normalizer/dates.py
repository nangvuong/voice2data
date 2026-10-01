import re
from typing import Any, Optional
from datetime import datetime, date, timedelta
from services.normalizer.core.tokenizer import Tokenizer
from services.normalizer.core.parser import DateParser

MONTH_MAP = {
    "GIÊNG": 1, "MỘT": 1, "MỐT": 1, "1": 1, "01": 1,
    "HAI": 2, "2": 2, "02": 2,
    "BA": 3, "3": 3, "03": 3,
    "BỐN": 4, "TƯ": 4, "4": 4, "04": 4,
    "NĂM": 5, "5": 5, "05": 5,
    "SÁU": 6, "6": 6, "06": 6,
    "BẢY": 7, "BẨY": 7, "7": 7, "07": 7,
    "TÁM": 8, "8": 8, "08": 8,
    "CHÍN": 9, "9": 9, "09": 9,
    "MƯỜI": 10, "10": 10, "CHỤC": 10,
    "MƯỜI MỘT": 11, "MƯỜI MỐT": 11, "11": 11,
    "MƯỜI HAI": 12, "CHẠP": 12, "12": 12,
}

WORD_TO_NUM = {
    "KHÔNG": 0, "MỘT": 1, "MỐT": 1, "HAI": 2, "BA": 3, "BỐN": 4, "TƯ": 4,
    "NĂM": 5, "LĂM": 5, "NHĂM": 5, "SÁU": 6, "BẢY": 7, "BẨY": 7, "TÁM": 8, "CHÍN": 9,
    "MƯỜI": 10, "MƯƠI": 10, "CHỤC": 10, "TRĂM": 100, "NGHÌN": 1000, "NGÀN": 1000,
    "TRIỆU": 1000000, "TỶ": 1000000000, "TỈ": 1000000000,
    "LẺ": 0, "LINH": 0, "HĂM": 20,
}

# Bảng tra cứu ngày 1 - 31 tiếng Việt (đầy đủ các biến thể nói tắt, tiếng lóng, sai chính tả)
DAY_WORDS: dict[str, int] = {}

# 1 - 9
for d, names in [
    (1, ["MỘT", "MỐT", "1", "01"]),
    (2, ["HAI", "2", "02"]),
    (3, ["BA", "3", "03"]),
    (4, ["BỐN", "TƯ", "4", "04"]),
    (5, ["NĂM", "5", "05"]),
    (6, ["SÁU", "6", "06"]),
    (7, ["BẢY", "BẨY", "7", "07"]),
    (8, ["TÁM", "8", "08"]),
    (9, ["CHÍN", "9", "09"]),
]:
    for n in names:
        DAY_WORDS[n] = d
        DAY_WORDS[f"MÙNG {n}"] = d
        DAY_WORDS[f"MỒNG {n}"] = d

# 10
DAY_WORDS["10"] = 10
DAY_WORDS["MƯỜI"] = 10
DAY_WORDS["CHỤC"] = 10
DAY_WORDS["MÙNG MƯỜI"] = 10
DAY_WORDS["MỒNG MƯỜI"] = 10
DAY_WORDS["MÙNG 10"] = 10
DAY_WORDS["MỒNG 10"] = 10

# 11 - 19
DAY_WORDS["11"] = 11
DAY_WORDS["MƯỜI MỘT"] = 11
DAY_WORDS["MƯỜI MỐT"] = 11
DAY_WORDS["12"] = 12
DAY_WORDS["MƯỜI HAI"] = 12
DAY_WORDS["13"] = 13
DAY_WORDS["MƯỜI BA"] = 13
DAY_WORDS["14"] = 14
DAY_WORDS["MƯỜI BỐN"] = 14
DAY_WORDS["MƯỜI TƯ"] = 14
DAY_WORDS["15"] = 15
DAY_WORDS["MƯỜI NĂM"] = 15
DAY_WORDS["MƯỜI LĂM"] = 15
DAY_WORDS["MƯỜI NHĂM"] = 15
DAY_WORDS["16"] = 16
DAY_WORDS["MƯỜI SÁU"] = 16
DAY_WORDS["17"] = 17
DAY_WORDS["MƯỜI BẢY"] = 17
DAY_WORDS["MƯỜI BẨY"] = 17
DAY_WORDS["18"] = 18
DAY_WORDS["MƯỜI TÁM"] = 18
DAY_WORDS["19"] = 19
DAY_WORDS["MƯỜI CHÍN"] = 19

# 20 - 29
DAY_WORDS["20"] = 20
DAY_WORDS["HAI MƯƠI"] = 20
DAY_WORDS["HAI CHỤC"] = 20
DAY_WORDS["HĂM"] = 20

for d, w_list in [
    (21, ["MỐT", "MỘT"]),
    (22, ["HAI"]),
    (23, ["BA"]),
    (24, ["BỐN", "TƯ"]),
    (25, ["LĂM", "NHĂM", "NĂM"]),
    (26, ["SÁU"]),
    (27, ["BẢY", "BẨY"]),
    (28, ["TÁM"]),
    (29, ["CHÍN"]),
]:
    DAY_WORDS[str(d)] = d
    for w in w_list:
        DAY_WORDS[f"HAI MƯƠI {w}"] = d
        DAY_WORDS[f"HAI {w}"] = d
        DAY_WORDS[f"HĂM {w}"] = d

# 30 - 31
DAY_WORDS["30"] = 30
DAY_WORDS["BA MƯƠI"] = 30
DAY_WORDS["BA CHỤC"] = 30
DAY_WORDS["31"] = 31
DAY_WORDS["BA MƯƠI MỐT"] = 31
DAY_WORDS["BA MƯƠI MỘT"] = 31
DAY_WORDS["BA MỐT"] = 31
DAY_WORDS["BA MỘT"] = 31


def clean_spoken_number_text(text: str) -> str:
    """Chuẩn hóa cách đọc tắt, tiếng địa phương, sai chính tả trong số."""
    t = text.upper().strip()
    t = re.sub(r"\bBẨY\b", "BẢY", t)
    t = re.sub(r"\bRƯỞI\b", "RƯỠI", t)

    # Hăm -> hai mươi
    t = re.sub(r"\bHĂM\s+MỐT\b", "HAI MƯƠI MỐT", t)
    t = re.sub(r"\bHĂM\s+LĂM\b", "HAI MƯƠI LĂM", t)
    t = re.sub(r"\bHĂM\s+NHĂM\b", "HAI MƯƠI LĂM", t)
    t = re.sub(r"\bHĂM\s+TƯ\b", "HAI MƯƠI TƯ", t)
    t = re.sub(r"\bHĂM\s+BỐN\b", "HAI MƯƠI BỐN", t)
    t = re.sub(r"\bHĂM\s+HAI\b", "HAI MƯƠI HAI", t)
    t = re.sub(r"\bHĂM\s+BA\b", "HAI MƯƠI BA", t)
    t = re.sub(r"\bHĂM\s+SÁU\b", "HAI MƯƠI SÁU", t)
    t = re.sub(r"\bHĂM\s+BẢY\b", "HAI MƯƠI BẢY", t)
    t = re.sub(r"\bHĂM\s+TÁM\b", "HAI MƯƠI TÁM", t)
    t = re.sub(r"\bHĂM\s+CHÍN\b", "HAI MƯƠI CHÍN", t)
    t = re.sub(r"\bHĂM\b", "HAI MƯƠI", t)

    # Chục
    t = re.sub(r"\bMỘT\s+CHỤC\b", "MƯỜI", t)
    t = re.sub(r"\bHAI\s+CHỤC\b", "HAI MƯƠI", t)
    t = re.sub(r"\bBA\s+CHỤC\b", "BA MƯƠI", t)
    t = re.sub(r"\bBỐN\s+CHỤC\b", "BỐN MƯƠI", t)
    t = re.sub(r"\bNĂM\s+CHỤC\b", "NĂM MƯƠI", t)
    t = re.sub(r"\bSÁU\s+CHỤC\b", "SÁU MƯƠI", t)
    t = re.sub(r"\bBẢY\s+CHỤC\b", "BẢY MƯƠI", t)
    t = re.sub(r"\bTÁM\s+CHỤC\b", "TÁM MƯƠI", t)
    t = re.sub(r"\bCHÍN\s+CHỤC\b", "CHÍN MƯƠI", t)
    t = re.sub(r"\bCHỤC\b", "MƯỜI", t)

    # Năm nói tắt (20 hai lăm, hai nghìn hai lăm...)
    t = re.sub(r"\b20\s+HAI\s+LĂM\b", "HAI NGHÌN HAI MƯƠI LĂM", t)
    t = re.sub(r"\b20\s+HAI\s+SÁU\b", "HAI NGHÌN HAI MƯƠI SÁU", t)
    t = re.sub(r"\b2\s+KHÔNG\s+25\b", "HAI KHÔNG HAI LĂM", t)
    t = re.sub(r"\bHAI\s+KHÔNG\s+25\b", "HAI KHÔNG HAI LĂM", t)
    t = re.sub(r"\b2\s+NGHÌN\s+25\b", "HAI NGHÌN HAI MƯƠI LĂM", t)
    t = re.sub(r"\b2\s+NGÀN\s+25\b", "HAI NGHÌN HAI MƯƠI LĂM", t)
    t = re.sub(r"\bHAI\s+NGHÌN\s+25\b", "HAI NGHÌN HAI MƯƠI LĂM", t)
    t = re.sub(r"\bHAI\s+NGÀN\s+25\b", "HAI NGHÌN HAI MƯƠI LĂM", t)

    t = re.sub(r"\bMƯỜI\s+NHĂM\b", "MƯỜI LĂM", t)

    t = re.sub(r"\bHAI\s+(?:NGHÌN|NGÀN)\s+HAI\s+MƯƠI\s+(?:LĂM|NĂM)\b", "HAI NGHÌN KHÔNG TRĂM HAI MƯƠI LĂM", t)
    t = re.sub(r"\bHAI\s+(?:NGHÌN|NGÀN)\s+HAI\s+MƯƠI\s+(?:SÁU)\b", "HAI NGHÌN KHÔNG TRĂM HAI MƯƠI SÁU", t)
    t = re.sub(r"\bHAI\s+(?:NGHÌN|NGÀN)\s+HAI\s+MƯƠI\s+(?:TƯ|BỐN)\b", "HAI NGHÌN KHÔNG TRĂM HAI MƯƠI TƯ", t)

    # 2 từ độc lập: HAI LĂM, HAI TƯ, BA MỐT
    words_temp = t.split()
    if len(words_temp) == 2:
        for prefix_name in ["HAI", "BA", "BỐN", "NĂM", "SÁU", "BẢY", "TÁM", "CHÍN"]:
            for u_s in ["MỐT", "HAI", "BA", "BỐN", "TƯ", "LĂM", "NHĂM", "SÁU", "BẢY", "TÁM", "CHÍN"]:
                if words_temp[0] == prefix_name and words_temp[1] == u_s:
                    t = f"{prefix_name} MƯƠI {u_s}"

    return t


def words_to_int(text: str) -> int:
    """Chuyển chuỗi số tiếng Việt (chữ hoặc số, kể cả nói tắt) sang integer."""
    text = text.upper().strip()
    if text.isdigit():
        return int(text)

    # Loại bỏ tiền tố năm / ngày nếu đứng trước biểu thức năm / ngày
    text = re.sub(r"^NĂM\s+(?=(?:20\d\d|19\d\d|HAI\s+(?:KHÔNG|NGHÌN|NGÀN)|HAI\s+MƯƠI\s+[LĂMTƯNHĂM\d]))", "", text)
    text = re.sub(r"^(?:NGÀY|MÙNG|MỒNG|THÁNG)\s+", "", text)

    # Check trực tiếp trong DAY_WORDS
    if text in DAY_WORDS:
        return DAY_WORDS[text]

    cleaned = clean_spoken_number_text(text)
    words = [w for w in cleaned.split() if w]

    # Kiểm tra chuỗi 4 chữ số liên tiếp (VD: HAI KHÔNG HAI LĂM = 2025)
    digits = []
    is_seq = True
    for w in words:
        if w in WORD_TO_NUM and WORD_TO_NUM[w] < 10:
            digits.append(str(WORD_TO_NUM[w]))
        elif w.isdigit() and len(w) == 1:
            digits.append(w)
        else:
            is_seq = False
            break
    if is_seq and len(digits) == 4:
        return int("".join(digits))

    stack = []
    can_concat = False
    for w in words:
        val = WORD_TO_NUM.get(w, None)
        if val is None:
            if w.isdigit():
                val = int(w)
            else:
                continue

        if val >= 1000:
            s = sum(stack) if stack else 1
            stack = [s * val]
            can_concat = False
        elif val == 100:
            if stack and 0 <= stack[-1] <= 9:
                s = stack.pop()
                stack.append(s * 100)
            else:
                stack.append(100)
            can_concat = False
        elif val == 10:
            if stack and 1 <= stack[-1] <= 9:
                s = stack.pop()
                stack.append(s * 10)
            else:
                stack.append(10)
            can_concat = False
        elif val == 0 and w in ("LẺ", "LINH"):
            can_concat = False
        else:
            # Hỗ trợ nói tắt thiếu mươi: HAI LĂM, HAI NHĂM, HAI MỐT, HAI TƯ, BA MỐT...
            if w in ("LĂM", "NHĂM", "MỐT") or (w in ("TƯ", "BỐN") and stack and 2 <= stack[-1] <= 9):
                if stack and 1 <= stack[-1] <= 9:
                    tens = stack.pop()
                    stack.append(tens * 10 + val)
                    can_concat = False
                    continue
            if can_concat and stack:
                last = stack.pop()
                stack.append(int(str(last) + str(val)))
            else:
                stack.append(val)
                can_concat = True

    return sum(stack) if stack else 0


def parse_days_list(text: str) -> list[int]:
    """Tách danh sách ngày trong cụm 'các ngày ...' (VD: 'hai mươi tư hai mươi bảy hai mươi chín')."""
    words = text.strip().upper().replace(",", " ").replace(" VÀ ", " ").split()
    results = []
    i = 0
    while i < len(words):
        matched = False
        for length in [3, 2, 1]:
            if i + length <= len(words):
                if length == 3 and i + 3 < len(words) and words[i + 3] in ("MƯƠI", "MƯỜI"):
                    continue
                phrase = " ".join(words[i:i + length])
                if phrase in DAY_WORDS:
                    results.append(DAY_WORDS[phrase])
                    i += length
                    matched = True
                    break
        if not matched:
            if words[i].isdigit():
                v = int(words[i])
                if 1 <= v <= 31:
                    results.append(v)
            i += 1
    return results


def extract_dmy(text: str) -> tuple[int | None, int | None, int | None]:
    """
    Trích xuất có cấu trúc (ngày, tháng, năm) từ một cụm ngày bất kỳ:
    - ngày D tháng M năm Y -> (d, m, y)
    - ngày D tháng M -> (d, m, None)
    - D/M/Y hoặc D/M -> (d, m, y)
    - ngày D (chỉ ngày) -> (d, None, None)
    """
    t = text.strip().upper()

    # 1. Format: ngày D tháng M năm Y
    m = re.search(r"(?:NGÀY|MÙNG|MỒNG)?\s*(.+?)\s+THÁNG\s+(.+?)\s+NĂM\s+(.+)$", t)
    if m:
        d = words_to_int(m.group(1))
        m_str = m.group(2).strip()
        mo = MONTH_MAP.get(m_str, words_to_int(m_str))
        y = words_to_int(m.group(3))
        if 1 <= d <= 31 and 1 <= mo <= 12 and 1900 <= y <= 2100:
            return (d, mo, y)

    # 1b. Format: ngày D tháng M Y (không có từ NĂM, vd: "ngày hai tháng bảy hai không hai lăm")
    m_no_nam = re.search(r"(?:NGÀY|MÙNG|MỒNG)?\s*(.+?)\s+THÁNG\s+(.+?)\s+((?:HAI\s+KHÔNG\s+\S+\s+\S+)|(?:\d{4})|(?:20\d{2}))(?:\s+.*)?$", t)
    if m_no_nam:
        d = words_to_int(m_no_nam.group(1))
        m_str = m_no_nam.group(2).strip()
        mo = MONTH_MAP.get(m_str, words_to_int(m_str))
        y = words_to_int(m_no_nam.group(3))
        if 1 <= d <= 31 and 1 <= mo <= 12 and 1900 <= y <= 2100:
            return (d, mo, y)

    # 2. Format: ngày D tháng M (không năm)
    m2 = re.search(r"(?:NGÀY|MÙNG|MỒNG)?\s*(.+?)\s+THÁNG\s+(.+)$", t)
    if m2:
        d = words_to_int(m2.group(1))
        m_str = m2.group(2).strip()
        mo = MONTH_MAP.get(m_str, words_to_int(m_str))
        if 1 <= d <= 31 and 1 <= mo <= 12:
            return (d, mo, None)

    # 3. Format: D/M/Y hoặc D/M
    m3 = re.search(r"\b(\d{1,2})[/\-\.](\d{1,2})(?:[/\-\.](\d{2,4}))?\b", t)
    if m3:
        d = int(m3.group(1))
        mo = int(m3.group(2))
        y_val = m3.group(3)
        y = None
        if y_val:
            y = int(y_val)
            if y < 100:
                y += 2000
        if 1 <= d <= 31 and 1 <= mo <= 12:
            return (d, mo, y)

    # 4. Format: Chỉ có ngày (VD: "mùng một", "ngày 15", "hai tư", "15")
    clean_d = re.sub(r"^(?:NGÀY|MÙNG|MỒNG)\s+", "", t).strip()
    if clean_d in DAY_WORDS:
        return (DAY_WORDS[clean_d], None, None)
    d_val = words_to_int(clean_d)
    if 1 <= d_val <= 31:
        return (d_val, None, None)

    return (None, None, None)


def parse_date(text: str) -> str:
    """
    Chuyển đổi linh hoạt chuỗi ngày/tháng/năm tiếng Việt sang định dạng chuẩn:
    - Khoảng ngày: 'từ ... đến ...' hoặc '... đến ...' -> 'DD/MM/YYYY - DD/MM/YYYY'
    - Nhiều ngày: 'ngày A và B' hoặc 'các ngày A B C tháng M năm Y' -> 'DD1/MM1/YYYY, DD2/MM2/YYYY...'
    - Ngày đơn: 'ngày D tháng M năm Y' -> 'DD/MM/YYYY'
    """
    if not text:
        return text
    raw = text.strip()
    u = raw.upper()

    # 1. Khoảng thời gian: "TỪ ... ĐẾN ...", "... ĐẾN ...", "... - ..."
    m_range = re.search(r"(?:\bTỪ\s+)?(.+?)\s+(?:ĐẾN|-)\s+(.+)", u)
    if m_range:
        part_a = m_range.group(1).strip()
        part_b = m_range.group(2).strip()
        part_a = re.sub(r"^TỪ\s+", "", part_a)

        da, ma, ya = extract_dmy(part_a)
        db, mb, yb = extract_dmy(part_b)

        if da and db:
            if ma is None and mb is not None:
                ma = mb
            if ya is None and yb is not None:
                ya = yb
            if mb is None and ma is not None:
                mb = ma
            if yb is None and ya is not None:
                yb = ya

            fmt_a = f"{da:02d}/{ma:02d}/{ya:04d}" if (ma and ya) else (f"{da:02d}/{ma:02d}" if ma else f"{da:02d}")
            fmt_b = f"{db:02d}/{mb:02d}/{yb:04d}" if (mb and yb) else (f"{db:02d}/{mb:02d}" if mb else f"{db:02d}")
            return f"{fmt_a} - {fmt_b}"

    # 2. Nhiều ngày: "CÁC NGÀY ... THÁNG ... [NĂM ...]"
    m_multi = re.search(r"\b(?:CÁC\s+NGÀY|NGÀY)\s+(.+?)\s+THÁNG\s+(.+?)(?:\s+NĂM\s+(.+))?$", u)
    if m_multi:
        days_str = m_multi.group(1).strip()
        month_str = m_multi.group(2).strip()
        year_str = m_multi.group(3).strip() if m_multi.group(3) else None

        days = parse_days_list(days_str)
        if len(days) > 1:
            mo = MONTH_MAP.get(month_str, words_to_int(month_str))
            y = words_to_int(year_str) if year_str else None
            if 1 <= mo <= 12:
                if y and 1900 <= y <= 2100:
                    return ", ".join(f"{d:02d}/{mo:02d}/{y:04d}" for d in days)
                else:
                    return ", ".join(f"{d:02d}/{mo:02d}" for d in days)

    # 3. Hai mốc ngày nối bằng "VÀ": "NGÀY ... VÀ ..."
    if " VÀ " in u:
        parts = u.split(" VÀ ")
        if len(parts) == 2:
            p1, p2 = parts[0].strip(), parts[1].strip()
            d1, m1, y1 = extract_dmy(p1)
            d2, m2, y2 = extract_dmy(p2)
            if d1 and d2:
                if m1 is None and m2 is not None:
                    m1 = m2
                if y1 is None and y2 is not None:
                    y1 = y2
                if m2 is None and m1 is not None:
                    m2 = m1
                if y2 is None and y1 is not None:
                    y2 = y1
                fmt1 = f"{d1:02d}/{m1:02d}/{y1:04d}" if (m1 and y1) else (f"{d1:02d}/{m1:02d}" if m1 else f"{d1:02d}")
                fmt2 = f"{d2:02d}/{m2:02d}/{y2:04d}" if (m2 and y2) else (f"{d2:02d}/{m2:02d}" if m2 else f"{d2:02d}")
                return f"{fmt1}, {fmt2}"

    # 4. Ngày đơn
    d, m, y = extract_dmy(u)
    if d and m and y:
        return f"{d:02d}/{m:02d}/{y:04d}"
    elif d and m:
        return f"{d:02d}/{m:02d}"

    # Fallback về Grammar Parser cổ điển nếu có
    try:
        tokens = Tokenizer.tokenize(raw)
        return DateParser.parse_date_tokens(tokens)
    except Exception:
        return raw



# ==============================================================================
# BỘ CHUẨN HÓA THỜI GIAN GIỜ / PHÚT (TIME NORMALIZER) CHO VIETGAP_DIARY
# ==============================================================================

TIME_WORDS_MAP = {
    "KHÔNG": 0, "MỘT": 1, "MỐT": 1, "HAI": 2, "BA": 3, "BỐN": 4, "TƯ": 4,
    "NĂM": 5, "SÁU": 6, "BẢY": 7, "BẨY": 7, "TÁM": 8, "CHÍN": 9,
    "MƯỜI": 10, "MƯỜI MỘT": 11, "MƯỜI HAI": 12, "MƯỜI BA": 13, "MƯỜI BỐN": 14,
    "MƯỜI LĂM": 15, "MƯỜI NĂM": 15, "MƯỜI SÁU": 16, "MƯỜI BẢY": 17, "MƯỜI TÁM": 18,
    "MƯỜI CHÍN": 19, "HAI MƯƠI": 20, "HAI MỐT": 21, "HAI HAI": 22, "HAI BA": 23,
}

def normalize_time(val: Any, default_on_period: bool = True) -> str | None:
    """
    Chuẩn hóa thời gian về định dạng HH:MM:SS chuẩn cho MySQL TIME.
    Hỗ trợ:
    - Định dạng số: '08:15', '8:15:00', '8h30', '14h', '15:00'
    - Có kèm buổi: '2h chiều' -> 14:00:00, '7h tối' -> 19:00:00, '8h sáng' -> 08:00:00
    - Từ ngữ tiếng Việt: '8 giờ sáng', '7 rưỡi sáng', '2 rưỡi chiều', 'mười bốn giờ ba mươi'
    - Từ ngữ chỉ buổi: 'sáng sớm' -> 06:00:00, 'sáng' -> 07:30:00, 'chiều' -> 15:00:00
    """
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in ("none", "null", "—", "-", "undefined", "''", '""'):
        return None

    # Nếu đã là HH:MM:SS
    m_full = re.match(r"^(\d{1,2}):(\d{2}):(\d{2})$", s)
    if m_full:
        h, m, sec = int(m_full.group(1)), int(m_full.group(2)), int(m_full.group(3))
        if 0 <= h <= 23 and 0 <= m <= 59 and 0 <= sec <= 59:
            return f"{h:02d}:{m:02d}:{sec:02d}"

    # Nếu là HH:MM
    m_hm = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if m_hm:
        h, m = int(m_hm.group(1)), int(m_hm.group(2))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}:00"

    u = s.upper().strip()

    # Nhận diện buổi
    is_pm = any(k in u for k in ["CHIỀU", "TỐI", "ĐÊM", "PM"])
    is_am = any(k in u for k in ["SÁNG", "AM"])
    is_noon = "TRƯA" in u

    # 1. Định dạng số kèm chữ 'h' hoặc 'giờ': "8H30", "8H", "14H20", "8 GIỜ 30", "8 GIỜ RƯỠI"
    m_h = re.search(r"(\d{1,2})\s*(?:H|GIỜ)\s*(?:(\d{1,2})|RƯỠI)?", u)
    if m_h:
        h = int(m_h.group(1))
        m_part = m_h.group(2)
        if "RƯỠI" in u[m_h.start():m_h.end()+6]:
            m = 30
        elif m_part and m_part.isdigit():
            m = int(m_part)
        else:
            m = 0

        # Điều chỉnh buổi chiều / tối (nếu h <= 12)
        if is_pm and 1 <= h <= 11:
            h += 12
        elif is_noon and h == 11 and is_pm:
            h = 11
        elif is_noon and h <= 2:
            h += 12
        elif is_am and h == 12:
            h = 0

        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}:00"

    # 2. Xử lý chữ số tiếng Việt: "BẢY GIỜ RƯỠI SÁNG", "HAI GIỜ CHIỀU", "MƯỜI BỐN GIỜ"
    for w_hour, h_num in sorted(TIME_WORDS_MAP.items(), key=lambda x: len(x[0]), reverse=True):
        if re.search(rf"\b{w_hour}\s+GIỜ", u):
            h = h_num
            m = 0
            if "RƯỠI" in u:
                m = 30
            elif "MƯỜI LĂM" in u or "MƯỜI NĂM" in u:
                m = 15
            elif "BỐN MƯƠI LĂM" in u or "BỐN MƯƠI NĂM" in u:
                m = 45

            if is_pm and 1 <= h <= 11:
                h += 12
            elif is_noon and h <= 2:
                h += 12

            if 0 <= h <= 23 and 0 <= m <= 59:
                return f"{h:02d}:{m:02d}:00"

    # 3. Nếu chỉ có từ chỉ buổi và cho phép gán giờ mặc định của buổi
    if default_on_period:
        if "SÁNG SỚM" in u or "TINH MƠ" in u:
            return "06:00:00"
        elif "SÁNG" in u:
            return "07:30:00"
        elif "TRƯA" in u:
            return "11:30:00"
        elif "CHIỀU" in u:
            return "15:00:00"
        elif "TỐI" in u or "ĐÊM" in u:
            return "19:00:00"

    return None


def extract_time_from_text(text: str) -> str | None:
    """Quét và trích xuất mốc thời gian giờ từ một đoạn văn bản tiếng Việt."""
    if not text or not str(text).strip():
        return None
    raw = str(text)

    # 1. Tìm mẫu giờ số: "lúc 8h30", "khoảng 7:15 sáng", "hồi 14h", "lúc 8 giờ rưỡi"
    m = re.search(r"(?:lúc|khoảng|hồi|vào|đoạn)?\s*(\d{1,2}\s*(?::|h|giờ)\s*\d{0,2}(?:\s*(?:sáng|trưa|chiều|tối))?|\d{1,2}\s*giờ\s*rưỡi(?:\s*(?:sáng|trưa|chiều|tối))?)", raw, re.IGNORECASE)
    if m:
        t = normalize_time(m.group(1))
        if t:
            return t

    # 2. Tìm mẫu giờ chữ: "tám giờ sáng", "hai giờ chiều", "bảy giờ rưỡi"
    m_words = re.search(r"((?:không|một|hai|ba|bốn|năm|sáu|bảy|tám|chín|mười|mười một|mười hai|mười bốn|mười lăm)\s+giờ(?:\s+rưỡi)?(?:\s+(?:sáng|trưa|chiều|tối))?)", raw, re.IGNORECASE)
    if m_words:
        t = normalize_time(m_words.group(1))
        if t:
            return t

    # 3. Tìm buổi
    if any(k in raw.lower() for k in ["sáng sớm"]):
        return "06:00:00"
    if any(k in raw.lower() for k in ["sáng nay", "buổi sáng"]):
        return "07:30:00"
    if any(k in raw.lower() for k in ["chiều qua", "chiều nay", "buổi chiều"]):
        return "15:00:00"
    if any(k in raw.lower() for k in ["trưa nay", "buổi trưa"]):
        return "11:30:00"

    return None


YEAR_PATTERN = r'(?:hai\s+(?:không|ngàn|nghìn)\s+\S+\s+\S+|\d{4}|20\d{2})'
DATE_SPOKEN_RE = re.compile(
    rf'(?:ngày|các ngày|mùng|mồng)\s+(.+?)\s+tháng\s+(.+?)(?:\s+năm\s+({YEAR_PATTERN}))?(?:\s+|$|[,\.;])',
    re.IGNORECASE
)


def resolve_spoken_date(text: str, fallback_iso: Optional[str] = None) -> str:
    """
    Trích xuất và giải quyết ngày chính xác từ câu nói tiếng Việt (STT).
    Ưu tiên:
    1. Ngày rõ ràng dạng số (DD/MM/YYYY hoặc DD/MM).
    2. Ngày rõ ràng dạng chữ tiếng Việt (ngày D tháng M [năm Y]).
    3. Từ chỉ thời gian tương đối:
       - 'hôm nay', 'sáng nay', 'chiều nay', 'trưa nay', 'tối nay', 'bữa nay', 'nay' -> ngày hôm nay (YYYY-MM-DD).
       - 'hôm qua', 'chiều qua', 'sáng qua', 'trưa qua', 'tối qua', 'hôm trước', 'hôm rồi' -> ngày hôm qua (YYYY-MM-DD).
       - 'hôm kia', 'hôm kìa' -> lùi 2 ngày.
       - 'ngày mai', 'sáng mai', 'chiều mai', 'mốt' -> tiến 1 ngày.
    4. Nếu trong câu hoàn toàn KHÔNG có từ chỉ ngày/thời gian nào:
       - Mặc định là ngày HÔM NAY (tránh LLM tự bịa đặt ngày tháng trong quá khứ).
    """
    today = date.today()
    if not text or not str(text).strip():
        return fallback_iso or today.isoformat()

    t = str(text).lower().strip()

    # 1. Tìm cụm ngày tháng năm rõ ràng dạng số (DD/MM/YYYY hoặc DD/MM)
    m_num = re.search(r'\b(\d{1,2})[/\-\.](\d{1,2})(?:[/\-\.](\d{2,4}))?\b', t)
    if m_num:
        d = int(m_num.group(1))
        mo = int(m_num.group(2))
        y_raw = m_num.group(3)
        y = int(y_raw) if y_raw else today.year
        if y < 100:
            y += 2000
        if 1 <= d <= 31 and 1 <= mo <= 12 and 1900 <= y <= 2100:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # 2. Tìm cụm ngày tháng năm dạng chữ tiếng Việt
    m_spoken = DATE_SPOKEN_RE.search(t)
    if not m_spoken and "tháng" in t:
        # Hỗ trợ trường hợp không có từ 'ngày' đứng đầu, vd: '20 tháng 12 năm 2026'
        m_spoken = re.search(rf'(?:^|\b)(?:ngày|mùng|mồng)?\s*(\d{{1,2}}|[a-zA-ZÀ-ỹ\s]+?)\s+tháng\s+(.+?)(?:\s+năm\s+({YEAR_PATTERN}))?(?:\s+|$|[,\.;])', t, re.IGNORECASE)
    if m_spoken:
        phrase = m_spoken.group(0).strip()
        if not phrase.lower().startswith(("ngày", "mùng", "mồng")):
            phrase = "ngày " + phrase
        d, mo, y = extract_dmy(phrase)
        if d and mo and 1 <= d <= 31 and 1 <= mo <= 12:
            y_final = y if (y and 1900 <= y <= 2100) else today.year
            return f"{y_final:04d}-{mo:02d}-{d:02d}"

    # 3. Từ chỉ thời gian tương đối
    # Hôm nay
    if re.search(r'\b(hôm nay|bữa nay|sáng nay|chiều nay|trưa nay|tối nay|nay|hôm ni|hiện tại)\b', t):
        return today.isoformat()

    # Hôm qua
    if re.search(r'\b(hôm qua|bữa qua|sáng qua|chiều qua|trưa qua|tối qua|hôm trước|hôm rồi)\b', t):
        return (today - timedelta(days=1)).isoformat()

    # Hôm kia
    if re.search(r'\b(hôm kia|hôm kìa)\b', t):
        return (today - timedelta(days=2)).isoformat()

    # Ngày mai
    if re.search(r'\b(ngày mai|sáng mai|chiều mai|ngày mốt|mốt)\b', t):
        return (today + timedelta(days=1)).isoformat()

    # 4. Nếu câu nói không nhắc đến bất kỳ mốc thời gian nào
    return fallback_iso if fallback_iso is not None else today.isoformat()


EXPIRY_PHRASE_RE = re.compile(
    r"(?:hạn\s+(?:sử\s+dụng|dùng|sd)?|hsd|date|hạn\s+đến)\s*(?:là|đến|tới|hết)?\s*:?\s*.+?(?:$|[,\.;]|\bvới\b|\bgiá\b|\btại\b|\bở\b)",
    re.IGNORECASE
)


def strip_expiry_phrase(text: str) -> str:
    """Loại bỏ cụm hạn sử dụng khỏi văn bản để tránh nhầm hạn sử dụng thành ngày thực hiện hoạt động."""
    if not text:
        return ""
    return EXPIRY_PHRASE_RE.sub(" ", text).strip()


def extract_spoken_expiry_date(text: str) -> Optional[str]:
    """
    Trích xuất chính xác hạn sử dụng (expiry_date) từ văn bản tiếng Việt.
    Hỗ trợ:
    - Ngày cụ thể: 'hạn sử dụng đến ngày 20 tháng 12 năm 2026', 'hạn dùng 20/12/2026', '20-12-2026'
    - Tháng năm: 'hạn đến tháng 12 năm 2026' -> '2026-12-31'
    - Năm: 'hsd 2027' -> '2027-12-31'
    - Khoảng năm: 'hạn sử dụng 2 năm' -> hôm nay + 2 năm
    """
    if not text:
        return None
    t = str(text).strip()

    # 1. Tìm mệnh đề hạn sử dụng
    m_exp = re.search(r"(?:hạn\s+(?:sử\s+dụng|dùng|sd)?|hsd|date|hạn\s+đến)\s*(?:là|đến|tới|hết)?\s*:?\s*(.+?)(?:$|[,\.;]|\bvới\b|\bgiá\b|\btại\b|\bở\b)", t, re.IGNORECASE)
    exp_str = m_exp.group(1).strip() if m_exp else t

    # 1a. Dạng X năm (vd: 2 năm, 3 năm)
    m_years = re.match(r"^(\d+)\s+năm", exp_str, re.IGNORECASE)
    if m_years:
        ny = int(m_years.group(1))
        today = date.today()
        try:
            return date(today.year + ny, today.month, today.day).isoformat()
        except ValueError:
            return date(today.year + ny, today.month, 28).isoformat()

    # 1b. Dạng ngày số DD/MM/YYYY hoặc DD-MM-YYYY
    m_dmy = re.search(r"\b(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{2,4})\b", exp_str)
    if m_dmy:
        d, mo, y = int(m_dmy.group(1)), int(m_dmy.group(2)), int(m_dmy.group(3))
        if y < 100:
            y += 2000
        if 1 <= d <= 31 and 1 <= mo <= 12 and 1900 <= y <= 2100:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # 1c. Dạng [ngày] D tháng M [năm Y] (chữ hoặc số)
    m_sp = DATE_SPOKEN_RE.search("ngày " + exp_str if not exp_str.lower().startswith(("ngày", "mùng", "mồng")) else exp_str)
    if not m_sp and "tháng" in exp_str:
        m_sp = re.search(rf'(?:^|\b)(?:ngày|mùng|mồng)?\s*(\d{{1,2}}|[a-zA-ZÀ-ỹ\s]+?)\s+tháng\s+(.+?)(?:\s+năm\s+({YEAR_PATTERN}))?(?:\s+|$|[,\.;])', exp_str, re.IGNORECASE)
    if m_sp:
        phr = m_sp.group(0).strip()
        if not phr.lower().startswith(("ngày", "mùng", "mồng")):
            phr = "ngày " + phr
        d, mo, y = extract_dmy(phr)
        if d and mo and 1 <= d <= 31 and 1 <= mo <= 12:
            y_final = y if (y and 1900 <= y <= 2100) else date.today().year
            return f"{y_final:04d}-{mo:02d}-{d:02d}"

    # 1d. Dạng tháng M năm Y (vd: tháng 12 năm 2026) -> lấy ngày cuối tháng
    m_my = re.search(r"tháng\s+(\d{1,2}|mười\s+hai|mười\s+một|[a-zA-ZÀ-ỹ]+)\s+năm\s+(\d{4}|[a-zA-ZÀ-ỹ0-9\s]+)", exp_str, re.IGNORECASE)
    if m_my:
        mo_raw = m_my.group(1).upper().strip()
        mo = MONTH_MAP.get(mo_raw)
        if not mo and mo_raw.isdigit():
            mo = int(mo_raw)
        _, _, y_parsed = extract_dmy("năm " + m_my.group(2))
        y_final = y_parsed if y_parsed else date.today().year
        if mo and 1 <= mo <= 12:
            import calendar
            last_day = calendar.monthrange(y_final, mo)[1]
            return f"{y_final:04d}-{mo:02d}-{last_day:02d}"

    # 1e. Dạng năm Y (vd: năm 2027 hoặc 2027) -> ngày cuối năm
    m_y = re.search(r"\b(20\d{2})\b", exp_str)
    if m_y:
        return f"{m_y.group(1)}-12-31"

    return None

