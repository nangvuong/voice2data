import re
from services.normalizer.core.types import Token, TokenType

class NumberParser:
    @staticmethod
    def reduce_numbers(tokens: list[Token]) -> list[Token]:
        """Gộp các token số học đứng liền nhau thành một token DIGIT hoặc LITERAL duy nhất."""
        result = []
        current_num_group = []
        
        def flush_num_group():
            if not current_num_group:
                return
            
            # Kiểm tra xem có DECIMAL trong group không
            decimal_idx = -1
            for i, t in enumerate(current_num_group):
                if t.type == TokenType.DECIMAL:
                    decimal_idx = i
                    break
            
            def calc_int_part(tokens_subset):
                stack = []
                can_concat = False
                for t in tokens_subset:
                    if t.type == TokenType.DIGIT:
                        # Hỗ trợ nói tắt thiếu mươi: HAI LĂM, HAI NHĂM, HAI MỐT, HAI TƯ, BA MỐT...
                        if t.raw in ("LĂM", "NHĂM", "MỐT") or (t.raw in ("TƯ", "BỐN") and stack and 2 <= stack[-1] <= 9):
                            if stack and 1 <= stack[-1] <= 9:
                                tens = stack.pop()
                                stack.append(tens * 10 + t.value)
                                can_concat = False
                                continue
                        if can_concat and stack:
                            last = stack.pop()
                            stack.append(int(str(last) + str(t.value)))
                        else:
                            stack.append(t.value)
                            can_concat = True
                    elif t.type == TokenType.MULTIPLIER:
                        m = t.value
                        if m >= 1000:
                            val = sum(stack) if stack else 1
                            stack = [val * m]
                        elif m == 100:
                            if stack and 0 <= stack[-1] <= 9:
                                val = stack.pop()
                                stack.append(val * 100)
                            else:
                                stack.append(100)
                        elif m == 10:
                            if stack and 1 <= stack[-1] <= 9:
                                val = stack.pop()
                                stack.append(val * 10)
                            else:
                                stack.append(10)
                        can_concat = False
                return sum(stack) if stack else 0

            raw_str = " ".join(t.raw for t in current_num_group)

            if decimal_idx != -1:
                int_tokens = current_num_group[:decimal_idx]
                frac_tokens = current_num_group[decimal_idx+1:]
                
                int_val = calc_int_part(int_tokens) if int_tokens else 0
                
                frac_str = ""
                if frac_tokens:
                    is_all_digits = all(t.type == TokenType.DIGIT for t in frac_tokens)
                    if is_all_digits:
                        frac_str = "".join(str(t.value) for t in frac_tokens)
                    else:
                        frac_str = str(calc_int_part(frac_tokens))
                
                decimal_t = current_num_group[decimal_idx]
                val_str = f"{int_val}{decimal_t.value}{frac_str}"
                result.append(Token(TokenType.LITERAL, val_str, raw_str))
            else:
                total = calc_int_part(current_num_group)
                result.append(Token(TokenType.DIGIT, total, raw_str))
                
            current_num_group.clear()

        for t in tokens:
            if t.type in (TokenType.DIGIT, TokenType.MULTIPLIER, TokenType.DECIMAL):
                if current_num_group:
                    last_t = current_num_group[-1]
                    # Nếu cả 2 đều là số Ả Rập -> ngắt (VD: "1" và "5" -> không gom thành 15)
                    if last_t.raw.isdigit() and t.raw.isdigit():
                        flush_num_group()
                    # Nếu trước đó là MULTIPLIER (như MƯỜI), nhưng số tiếp theo là số Ả Rập >= 10 -> ngắt
                    elif last_t.type == TokenType.MULTIPLIER and t.raw.isdigit() and (t.value >= 10 or t.value == 0):
                        flush_num_group()
                        
                current_num_group.append(t)
            else:
                flush_num_group()
                result.append(t)
                
        flush_num_group()
        return result

    @staticmethod
    def evaluate_number_tokens(tokens: list[Token]) -> str:
        """
        Duyệt qua danh sách token, gộp các token liên quan đến số và tính toán.
        Trả về chuỗi kết quả hoàn chỉnh.
        """
        reduced_tokens = NumberParser.reduce_numbers(tokens)
        
        result_strs = []
        for t in reduced_tokens:
            if t.type == TokenType.DIGIT:
                result_strs.append(str(t.value))
            elif t.type == TokenType.UNIT:
                # Sử dụng value chuẩn hóa thay vì raw text (VD: "/NĂM" thay vì "TRÊN_NĂM")
                result_strs.append(str(t.value))
            else:
                result_strs.append(t.raw)
                
        out = " ".join(result_strs)
        out = out.replace(" . ", ".").replace(" .", ".")
        out = re.sub(r"\s+(/[A-ZÀ-Ỹ0-9²]+)", r"\1", out)
        return out.strip()

class DateParser:
    @staticmethod
    def parse_date_tokens(tokens: list[Token]) -> str:
        """
        Dựa trên mảng token (đã reduce số), tìm ra ngày, tháng, năm.
        Grammar cơ bản: [PREFIX_DAY]? [DIGIT_DAY]? [PREFIX_MONTH]? [DIGIT_MONTH]? [PREFIX_YEAR]? [DIGIT_YEAR]?
        """
        from services.normalizer.core.validator import Validator
        
        reduced = NumberParser.reduce_numbers(tokens)
        
        day = None
        month = None
        year = None
        
        # FSM đơn giản
        EXPECT_DAY = False
        EXPECT_MONTH = False
        EXPECT_YEAR = False
        
        for t in reduced:
            if t.type == TokenType.PREFIX_DAY:
                EXPECT_DAY = True
                EXPECT_MONTH = False
                EXPECT_YEAR = False
            elif t.type == TokenType.PREFIX_MONTH:
                EXPECT_MONTH = True
                EXPECT_DAY = False
                EXPECT_YEAR = False
            elif t.type == TokenType.PREFIX_YEAR:
                EXPECT_YEAR = True
                EXPECT_DAY = False
                EXPECT_MONTH = False
            elif t.type == TokenType.DIGIT:
                # Đang đợi một loại số cụ thể
                if EXPECT_DAY and day is None:
                    day = t.value
                    EXPECT_DAY = False
                elif EXPECT_MONTH and month is None:
                    month = t.value
                    EXPECT_MONTH = False
                elif EXPECT_YEAR and year is None:
                    year = t.value
                    EXPECT_YEAR = False
                else:
                    # Nếu thấy số mà không có prefix, tự suy luận theo thứ tự (ngày -> tháng -> năm)
                    # Giả định: Người đọc thường đọc ngày tháng năm theo thứ tự
                    if day is None:
                        day = t.value
                    elif month is None:
                        month = t.value
                    elif year is None:
                        year = t.value
                        
        # Kiểm duyệt tính logic
        if Validator.is_valid_date(day, month, year):
            parts = []
            if day is not None:
                parts.append(f"{day:02d}")
            if month is not None:
                parts.append(f"{month:02d}")
            if year is not None:
                parts.append(f"{year:04d}")
            if parts:
                return "/".join(parts)
                
        # Nếu LLM trả về số nhưng validation fail. 
        # Option A: Bỏ qua ngày nếu ngày sai, giữ lại tháng/năm.
        if Validator.is_valid_date(None, month, year) and month and year:
            return f"{month:02d}/{year:04d}"
            
        # Nếu không bắt được ngày tháng hợp lệ nào (hoặc chuỗi không chứa ngày tháng),
        # return lại raw string sau khi parse number (thường gặp khi output fail)
        return NumberParser.evaluate_number_tokens(tokens)
