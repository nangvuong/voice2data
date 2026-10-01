import re
from services.normalizer.core.types import Token, TokenType
from services.normalizer.rules.vocab import VOCAB, UNITS

# Các từ tiếng Việt có thể là thành phần của năm (VD: HAI KHÔNG HAI SÁU = 2026)
_YEAR_DIGIT_WORDS = {
    "KHÔNG", "MỘT", "MỐT", "HAI", "BA", "BỐN", "TƯ",
    "LĂM", "NHĂM", "SÁU", "BẢY", "BẨY", "TÁM", "CHÍN"
}

_DIGIT_MAP = {
    "MỘT": 1, "MỐT": 1, "HAI": 2, "BA": 3, "BỐN": 4, "TƯ": 4,
    "NĂM": 5, "LĂM": 5, "NHĂM": 5, "SÁU": 6, "BẢY": 7, "TÁM": 8, "CHÍN": 9,
}

def _parse_num_val(s: str) -> int:
    s = s.strip().upper()
    if s.isdigit():
        return int(s)
    words = s.split()
    if len(words) == 1:
        if words[0] == "MƯỜI":
            return 10
        return _DIGIT_MAP.get(words[0], 1)
    if len(words) == 2:
        if words[0] == "MƯỜI":
            return 10 + _DIGIT_MAP.get(words[1], 0)
        elif words[1] == "MƯƠI":
            return _DIGIT_MAP.get(words[0], 1) * 10
        elif words[0] in _DIGIT_MAP and words[1] in ("LĂM", "NHĂM", "MỐT", "TƯ"):
            return _DIGIT_MAP[words[0]] * 10 + _DIGIT_MAP.get(words[1], 0)
    if len(words) == 3 and words[1] == "MƯƠI":
        return _DIGIT_MAP.get(words[0], 1) * 10 + _DIGIT_MAP.get(words[2], 0)
    return _DIGIT_MAP.get(s, 1)

def preprocess_spoken_text(text: str) -> str:
    """Chuẩn hóa cách nói tắt, tiếng lóng, sai chính tả số trước khi tách từ."""
    t = text.upper().strip()
    t = re.sub(r"\bBẨY\b", "BẢY", t)
    t = re.sub(r"\bRƯỞI\b", "RƯỠI", t)

    # Khung nhận diện số / cụm số tiếng Việt
    _single_digit = r"(?:KHÔNG|MỘT|MỐT|HAI|BA|BỐN|TƯ|NĂM|LĂM|NHĂM|SÁU|BẢY|BẨY|TÁM|CHÍN)"
    _num_or_digit = rf"(?:(?:{_single_digit}\s+MƯƠI(?:\s+{_single_digit})?)|(?:MƯỜI(?:\s+{_single_digit})?)|(?:{_single_digit}\s+(?:LĂM|NHĂM|MỐT|TƯ))|{_single_digit}|\d+)"
    t = re.sub(rf"\b({_num_or_digit})\s+TRIỆU\s+RƯỠI\b", lambda m: f"{_parse_num_val(m.group(1))*1000000 + 500000}", t)
    t = re.sub(r"\bTRIỆU\s+RƯỠI\b", "1500000", t)
    t = re.sub(rf"\b({_num_or_digit})\s+(?:NGÀN|NGHÌN)\s+RƯỠI\b", lambda m: f"{_parse_num_val(m.group(1))*1000 + 500}", t)
    t = re.sub(r"\b(?:NGÀN|NGHÌN)\s+RƯỠI\b", "1500", t)
    t = re.sub(rf"\b({_num_or_digit})\s+TRĂM\s+RƯỠI\b", lambda m: f"{_parse_num_val(m.group(1))*100 + 50}", t)
    t = re.sub(r"\bTRĂM\s+RƯỠI\b", "150", t)

    # 2 ngàn 5 / hai nghìn năm -> 2500
    t = re.sub(rf"\b({_num_or_digit})\s+(?:NGÀN|NGHÌN)\s+(?:5|NĂM)(?!\s+TRĂM)\b", lambda m: f"{_parse_num_val(m.group(1))*1000 + 500}", t)

    # Phân số nói thông dụng
    t = re.sub(r"\bMỘT\s+PHẦN\s+HAI\b", "0.5", t)
    t = re.sub(r"\bMỘT\s+PHẦN\s+TƯ\b", "0.25", t)
    t = re.sub(r"\bBA\s+PHẦN\s+TƯ\b", "0.75", t)

    # Định mức / tỉ lệ thường gặp
    t = re.sub(r"\b(?:CHO\s+MỖI|TRÊN\s+MỖI|MỖI|CHO\s+MỘT|TRÊN\s+MỘT|CHO\s+1|TRÊN\s+1)\s+(?:HÉC\s+TA|HECTA|HA)\b", "/HA", t)
    t = re.sub(r"\b(?:CHO\s+MỖI|TRÊN\s+MỖI|MỖI|CHO\s+MỘT|TRÊN\s+MỘT|CHO\s+1|TRÊN\s+1)\s+SÀO\b", "/SÀO", t)
    t = re.sub(r"\b(?:CHO\s+MỖI|TRÊN\s+MỖI|MỖI|CHO\s+MỘT|TRÊN\s+MỘT|CHO\s+1|TRÊN\s+1)\s+CÔNG\b", "/CÔNG", t)
    t = re.sub(r"\b(?:CHO\s+MỖI|TRÊN\s+MỖI|MỖI|CHO\s+MỘT|TRÊN\s+MỘT|CHO\s+1|TRÊN\s+1)\s+MẪU\b", "/MẪU", t)
    t = re.sub(r"\b(?:CHO\s+MỖI|TRÊN\s+MỖI|MỖI|CHO\s+MỘT|TRÊN\s+MỘT|CHO\s+1|TRÊN\s+1)\s+BÌNH\b", "/BÌNH", t)
    t = re.sub(r"\b(?:CHO\s+MỖI|TRÊN\s+MỖI|MỖI|CHO\s+MỘT|TRÊN\s+MỘT|CHO\s+1|TRÊN\s+1)\s+GỐC\b", "/GỐC", t)
    t = re.sub(r"\b(?:CHO\s+MỖI|TRÊN\s+MỖI|MỖI|CHO\s+MỘT|TRÊN\s+MỘT|CHO\s+1|TRÊN\s+1)\s+CÂY\b", "/CÂY", t)
    t = re.sub(r"\b(?:CHO\s+MỖI|TRÊN\s+MỖI|MỖI|CHO\s+MỘT|TRÊN\s+MỘT|CHO\s+1|TRÊN\s+1)\s+VỤ\b", "/VỤ", t)

    # <số> <đơn vị> rưỡi -> <số>.5 <đơn vị> (VD: 2 lít rưỡi -> 2.5 lít, hai lít rưỡi -> 2.5 lít)
    _units_pat = r"(?:LÍT|LIT|KG|KÍ|KÝ|CÂN|KI_LÔ_GAM|KILOGAM|TẠ|TẤN|YẾN|GAM|GRAM|HA|HÉC_TA|HECTA|SÀO|MẪU|CÔNG|BAO|CHAI|GÓI|LỌ|CAN|THÙNG|BÌNH|BÌ|BỌC|SỌT|CÂY|NGÀY|THÁNG|NĂM|VỤ|NGƯỜI|CÁI|BỘ)"
    
    # Nửa <đơn vị> -> 0.5 <đơn vị> (VD: nửa lít -> 0.5 lít, nửa bao -> 0.5 bao, nửa sào -> 0.5 sào)
    t = re.sub(rf"\bNỬA\s+({_units_pat})\b", r"0.5 \1", t)
    t = re.sub(r"\bNỬA\s+TRIỆU\b", "500000", t)

    t = re.sub(
        rf"\b({_num_or_digit})\s+({_units_pat})\s+RƯỠI\b",
        lambda m: f"{_parse_num_val(m.group(1))}.5 {m.group(2)}",
        t
    )
    # <đơn vị> rưỡi đứng độc lập (VD: lít rưỡi -> 1.5 lít, bao rưỡi -> 1.5 bao)
    t = re.sub(
        rf"\b({_units_pat})\s+RƯỠI\b",
        r"1.5 \1",
        t
    )
    # <số> rưỡi <đơn vị> -> <số>.5 <đơn vị> (VD: hai rưỡi lít -> 2.5 lít)
    t = re.sub(
        rf"\b({_num_or_digit})\s+RƯỠI\s+({_units_pat})\b",
        lambda m: f"{_parse_num_val(m.group(1))}.5 {m.group(2)}",
        t
    )
    # <số> rưỡi độc lập -> <số>.5
    t = re.sub(
        rf"\b({_num_or_digit})\s+RƯỠI\b",
        lambda m: f"{_parse_num_val(m.group(1))}.5",
        t
    )

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

    # Chuỗi năm nói tắt
    t = re.sub(r"\b20\s+HAI\s+LĂM\b", "HAI NGHÌN HAI MƯƠI LĂM", t)
    t = re.sub(r"\b20\s+HAI\s+SÁU\b", "HAI NGHÌN HAI MƯƠI SÁU", t)
    t = re.sub(r"\b2\s+KHÔNG\s+25\b", "HAI KHÔNG HAI LĂM", t)
    t = re.sub(r"\bHAI\s+KHÔNG\s+25\b", "HAI KHÔNG HAI LĂM", t)
    t = re.sub(r"\b2\s+NGHÌN\s+25\b", "HAI NGHÌN HAI MƯƠI LĂM", t)
    t = re.sub(r"\b2\s+NGÀN\s+25\b", "HAI NGHÌN HAI MƯƠI LĂM", t)
    t = re.sub(r"\bHAI\s+NGHÌN\s+25\b", "HAI NGHÌN HAI MƯƠI LĂM", t)
    t = re.sub(r"\bHAI\s+NGÀN\s+25\b", "HAI NGHÌN HAI MƯƠI LĂM", t)

    t = re.sub(r"\bHAI\s+(?:NGHÌN|NGÀN)\s+HAI\s+MƯƠI\s+(?:LĂM|NĂM)\b", "HAI NGHÌN KHÔNG TRĂM HAI MƯƠI LĂM", t)
    t = re.sub(r"\bHAI\s+(?:NGHÌN|NGÀN)\s+HAI\s+MƯƠI\s+(?:SÁU)\b", "HAI NGHÌN KHÔNG TRĂM HAI MƯƠI SÁU", t)
    t = re.sub(r"\bHAI\s+(?:NGHÌN|NGÀN)\s+HAI\s+MƯƠI\s+(?:TƯ|BỐN)\b", "HAI NGHÌN KHÔNG TRĂM HAI MƯƠI TƯ", t)

    return t

class Tokenizer:
    @staticmethod
    def tokenize(text: str) -> list[Token]:
        if not text:
            return []
            
        text = preprocess_spoken_text(text)
        
        # Tiền xử lý các đơn vị nhiều từ trước (vd: TẤN TRÊN NĂM)
        # Sắp xếp theo độ dài giảm dần để cụm dài nhất được replace trước
        sorted_units = sorted(UNITS.items(), key=lambda x: len(x[0]), reverse=True)
        for u_phrase, (u_type, u_val) in sorted_units:
            if " " in u_phrase:
                safe_phrase = u_phrase.replace(" ", "_")
                pattern = rf"(?<!\w){re.escape(u_phrase)}(?!\w)"
                text = re.sub(pattern, safe_phrase, text)
                
        # Tách chữ và các ký tự đặc biệt (hỗ trợ / kèm đơn vị đo: /HA, /SÀO...)
        raw_tokens = re.findall(r'/\w+(?:_\w+)*|\w+(?:_\w+)*|[^\w\s]', text, re.UNICODE)
        
        tokens = []
        for i, raw in enumerate(raw_tokens):
            w = raw.replace("_", " ")
            
            # Ưu tiên check unit
            if w in UNITS:
                tokens.append(Token(TokenType.UNIT, UNITS[w][1], raw))
                continue
                
            # Xử lý từ NĂM (số 5 hoặc Year)
            if w == "NĂM":
                is_digit = False
                
                # --- Look-ahead ---
                if i + 1 < len(raw_tokens):
                    next_w = raw_tokens[i+1].replace("_", " ")
                    # NĂM đứng trước hệ số nhân hoặc dấu thập phân → là số 5
                    if next_w in ["MƯƠI", "MƯỜI", "TRĂM", "NGHÌN", "NGÀN", "CHẤM", "PHẨY", "RƯỠI"]:
                        is_digit = True
                    # NĂM đứng ngay trước đơn vị đo lường → là số 5
                    elif next_w in UNITS:
                        is_digit = True
                
                # --- Look-ahead sâu: đếm chữ số theo sau ---
                # Nếu sau NĂM có ≥2 chữ số/chữ tiếng Việt liên tiếp (VD: HAI KHÔNG HAI SÁU)
                # thì đây là NĂM (Year)
                if not is_digit:
                    digit_count = 0
                    for j in range(i + 1, min(i + 5, len(raw_tokens))):
                        jw = raw_tokens[j].replace("_", " ")
                        if jw in _YEAR_DIGIT_WORDS or jw.isdigit():
                            digit_count += 1
                        else:
                            break
                    if digit_count >= 2:
                        is_digit = False  # Đây là PREFIX_YEAR, giữ nguyên
                
                # --- Look-behind ---
                if i > 0:
                    prev_w = raw_tokens[i-1].replace("_", " ")
                    # Đứng sau LẺ/LINH/SỐ/KHÔNG/CHẤM/PHẨY → số 5 (VD: "LINH NĂM", "KHÔNG NĂM", "CHẤM NĂM")
                    if prev_w in ["LẺ", "LINH", "SỐ", "KHÔNG", "CHẤM", "PHẨY"]:
                        is_digit = True
                    # Sau THÁNG/NGÀY/MỒNG/MÙNG → số 5 (VD: "THÁNG NĂM" = tháng 5, "MÙNG NĂM" = mùng 5)
                    if prev_w in ["THÁNG", "NGÀY", "MỒNG", "MÙNG"]:
                        is_digit = True
                    # Sau MƯỜI/MƯƠI: NĂM chỉ là số 5 khi look-ahead không tìm thấy
                    # chuỗi năm (≥2 chữ số). VD: "MƯỜI NĂM" = 15, nhưng
                    # "MƯỜI NĂM HAI KHÔNG HAI SÁU" → MƯỜI (10) rồi NĂM (Year) 2026
                    if prev_w in ["MƯỜI", "MƯƠI"]:
                        # Kiểm tra look-ahead: có ≥2 chữ số ngay sau NĂM không?
                        digit_count_after = 0
                        for j in range(i + 1, min(i + 5, len(raw_tokens))):
                            jw = raw_tokens[j].replace("_", " ")
                            if jw in _YEAR_DIGIT_WORDS or jw.isdigit():
                                digit_count_after += 1
                            else:
                                break
                        if digit_count_after < 2:
                            # Không đủ chữ số để hình thành năm → NĂM là số 5 (VD: MƯỜI NĂM = 15)
                            is_digit = True
                        # else: NĂM là PREFIX_YEAR (VD: MƯỜI NĂM 2026 = tháng 10, năm 2026)
                        
                if is_digit:
                    tokens.append(Token(TokenType.DIGIT, 5, raw))
                else:
                    tokens.append(Token(TokenType.PREFIX_YEAR, None, raw))
                continue
                
            if w in (".", ","):
                has_digit_after = (i + 1 < len(raw_tokens) and (raw_tokens[i+1].isdigit() or (raw_tokens[i+1] in VOCAB and VOCAB[raw_tokens[i+1]][0] == TokenType.DIGIT)))
                has_digit_before = (i > 0 and (raw_tokens[i-1].isdigit() or (raw_tokens[i-1] in VOCAB and VOCAB[raw_tokens[i-1]][0] == TokenType.DIGIT)))
                if (has_digit_before and has_digit_after) or (not has_digit_before and has_digit_after):
                    tokens.append(Token(TokenType.DECIMAL, ".", raw))
                    continue
                else:
                    tokens.append(Token(TokenType.PUNCT, w, raw))
                    continue

            if w in VOCAB:
                t_type, t_val = VOCAB[w]
                tokens.append(Token(t_type, t_val, raw))
                continue
                
            if w.isdigit():
                tokens.append(Token(TokenType.DIGIT, int(w), raw))
                continue
                
            tokens.append(Token(TokenType.LITERAL, w, raw))
            
        return tokens
