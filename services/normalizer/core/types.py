from enum import Enum, auto
from typing import Any, NamedTuple

class TokenType(Enum):
    DIGIT = auto()        # Các số từ 0 đến 9
    MULTIPLIER = auto()   # Các hệ số: MƯỜI (10), TRĂM (100), NGHÌN (1000)
    PREFIX_DAY = auto()   # Từ chỉ ngày: NGÀY, MỒNG
    PREFIX_MONTH = auto() # Từ chỉ tháng: THÁNG
    PREFIX_YEAR = auto()  # Từ chỉ năm: NĂM
    DECIMAL = auto()      # Các từ thập phân: CHẤM, PHẨY, RƯỠI
    PUNCT = auto()        # Dấu phân cách
    LITERAL = auto()      # Từ ngữ thông thường không mang nghĩa số học
    UNIT = auto()         # Đơn vị: HA, TẤN, TẤN/NĂM
    
class Token(NamedTuple):
    type: TokenType
    value: Any
    raw: str
