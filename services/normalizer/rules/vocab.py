from services.normalizer.core.types import TokenType

# Từ điển ánh xạ từ vựng tiếng Việt sang token
VOCAB = {
    # Digits
    "KHÔNG": (TokenType.DIGIT, 0),
    "LẺ": (TokenType.DIGIT, 0),
    "LINH": (TokenType.DIGIT, 0),
    "MỘT": (TokenType.DIGIT, 1),
    "MỐT": (TokenType.DIGIT, 1),
    "HAI": (TokenType.DIGIT, 2),
    "BA": (TokenType.DIGIT, 3),
    "BỐN": (TokenType.DIGIT, 4),
    "TƯ": (TokenType.DIGIT, 4),
    "NĂM": (TokenType.DIGIT, 5),
    "LĂM": (TokenType.DIGIT, 5),
    "NHĂM": (TokenType.DIGIT, 5),
    "SÁU": (TokenType.DIGIT, 6),
    "BẢY": (TokenType.DIGIT, 7),
    "BẨY": (TokenType.DIGIT, 7),
    "TÁM": (TokenType.DIGIT, 8),
    "CHÍN": (TokenType.DIGIT, 9),
    
    # Multipliers
    "MƯỜI": (TokenType.MULTIPLIER, 10),
    "MƯƠI": (TokenType.MULTIPLIER, 10),
    "CHỤC": (TokenType.MULTIPLIER, 10),
    "TRĂM": (TokenType.MULTIPLIER, 100),
    "NGHÌN": (TokenType.MULTIPLIER, 1000),
    "NGÀN": (TokenType.MULTIPLIER, 1000),
    "TRIỆU": (TokenType.MULTIPLIER, 1000000),
    "TỶ": (TokenType.MULTIPLIER, 1000000000),
    "TỈ": (TokenType.MULTIPLIER, 1000000000),
    
    # Decimals
    "CHẤM": (TokenType.DECIMAL, "."),
    "PHẨY": (TokenType.DECIMAL, "."),
    ".": (TokenType.DECIMAL, "."),
    ",": (TokenType.DECIMAL, "."),
    "RƯỠI": (TokenType.DECIMAL, ".5"),
    "RƯỞI": (TokenType.DECIMAL, ".5"),
    
    # Prefixes
    "NGÀY": (TokenType.PREFIX_DAY, None),
    "MỒNG": (TokenType.PREFIX_DAY, None),
    "MÙNG": (TokenType.PREFIX_DAY, None),
    "THÁNG": (TokenType.PREFIX_MONTH, None),
    
    # Chữ "NĂM" (Year) bị trùng lặp với số 5. Tokenizer sẽ xử lý dựa trên ngữ cảnh.
    # Mặc định ta sẽ ưu tiên xét ngữ cảnh bằng parser.
    # Trong bộ vocab này ta để riêng 1 mục YEAR để tham chiếu nếu cần
    "YEAR_PREFIX": (TokenType.PREFIX_YEAR, None),
    
    # Tháng theo chữ
    "GIÊNG": (TokenType.DIGIT, 1),
    "CHẠP": (TokenType.DIGIT, 12),
}

UNITS = {
    # Diện tích
    "HÉC TA": (TokenType.UNIT, "HA"),
    "HECTA": (TokenType.UNIT, "HA"),
    "HA": (TokenType.UNIT, "HA"),
    "TRÊN MỘT HÉC TA": (TokenType.UNIT, "/HA"),
    "TRÊN HÉC TA": (TokenType.UNIT, "/HA"),
    "TRÊN 1 HÉC TA": (TokenType.UNIT, "/HA"),
    "TRÊN 1 HA": (TokenType.UNIT, "/HA"),
    "/HÉC TA": (TokenType.UNIT, "/HA"),
    "/HA": (TokenType.UNIT, "/HA"),
    "SÀO": (TokenType.UNIT, "SÀO"),
    "TRÊN SÀO": (TokenType.UNIT, "/SÀO"),
    "TRÊN 1 SÀO": (TokenType.UNIT, "/SÀO"),
    "/SÀO": (TokenType.UNIT, "/SÀO"),
    "MẪU": (TokenType.UNIT, "MẪU"),
    "TRÊN MẪU": (TokenType.UNIT, "/MẪU"),
    "TRÊN 1 MẪU": (TokenType.UNIT, "/MẪU"),
    "/MẪU": (TokenType.UNIT, "/MẪU"),
    "CÔNG": (TokenType.UNIT, "CÔNG"),
    "TRÊN CÔNG": (TokenType.UNIT, "/CÔNG"),
    "TRÊN 1 CÔNG": (TokenType.UNIT, "/CÔNG"),
    "/CÔNG": (TokenType.UNIT, "/CÔNG"),
    "MÉT VUÔNG": (TokenType.UNIT, "M²"),
    "M2": (TokenType.UNIT, "M²"),
    "M²": (TokenType.UNIT, "M²"),
    "/M2": (TokenType.UNIT, "/M²"),
    "/M²": (TokenType.UNIT, "/M²"),

    # Khối lượng & Thể tích
    "KI LÔ GAM": (TokenType.UNIT, "KG"),
    "KILOGAM": (TokenType.UNIT, "KG"),
    "KÍ LÔ GAM": (TokenType.UNIT, "KG"),
    "KÍ": (TokenType.UNIT, "KG"),
    "KÝ": (TokenType.UNIT, "KG"),
    "CÂN": (TokenType.UNIT, "KG"),
    "KG": (TokenType.UNIT, "KG"),
    "TẠ": (TokenType.UNIT, "TẠ"),
    "TẤN": (TokenType.UNIT, "TẤN"),
    "YẾN": (TokenType.UNIT, "YẾN"),
    "GAM": (TokenType.UNIT, "GAM"),
    "GRAM": (TokenType.UNIT, "GAM"),
    "LÍT": (TokenType.UNIT, "LÍT"),
    "LIT": (TokenType.UNIT, "LÍT"),
    "MILILÍT": (TokenType.UNIT, "ML"),
    "MILILIT": (TokenType.UNIT, "ML"),
    "MI LI LÍT": (TokenType.UNIT, "ML"),
    "ML": (TokenType.UNIT, "ML"),
    "CC": (TokenType.UNIT, "ML"),

    # Đơn vị cây giống, bao bì & đóng gói
    "CÂY GIỐNG": (TokenType.UNIT, "CÂY GIỐNG"),
    "CÂY": (TokenType.UNIT, "CÂY"),
    "BAO": (TokenType.UNIT, "BAO"),
    "CHAI": (TokenType.UNIT, "CHAI"),
    "GÓI": (TokenType.UNIT, "GÓI"),
    "LỌ": (TokenType.UNIT, "LỌ"),
    "CAN": (TokenType.UNIT, "CAN"),
    "THÙNG": (TokenType.UNIT, "THÙNG"),
    "BÌNH": (TokenType.UNIT, "BÌNH"),
    "BÌ": (TokenType.UNIT, "BÌ"),
    "BỌC": (TokenType.UNIT, "BỌC"),
    "VIÊN": (TokenType.UNIT, "VIÊN"),
    "VỈ": (TokenType.UNIT, "VỈ"),
    "CUỘN": (TokenType.UNIT, "CUỘN"),
    "TÉP": (TokenType.UNIT, "TÉP"),
    "SỌT": (TokenType.UNIT, "SỌT"),
    "MÉT": (TokenType.UNIT, "MÉT"),

    # Thời gian, tần suất & định mức
    "NGÀY": (TokenType.UNIT, "NGÀY"),
    "THÁNG": (TokenType.UNIT, "THÁNG"),
    "VỤ": (TokenType.UNIT, "VỤ"),
    "TRÊN NĂM": (TokenType.UNIT, "/NĂM"),
    "/NĂM": (TokenType.UNIT, "/NĂM"),
    "TRÊN VỤ": (TokenType.UNIT, "/VỤ"),
    "/VỤ": (TokenType.UNIT, "/VỤ"),
    "TRÊN BÌNH": (TokenType.UNIT, "/BÌNH"),
    "TRÊN 1 BÌNH": (TokenType.UNIT, "/BÌNH"),
    "/BÌNH": (TokenType.UNIT, "/BÌNH"),
    "TRÊN GỐC": (TokenType.UNIT, "/GỐC"),
    "TRÊN 1 GỐC": (TokenType.UNIT, "/GỐC"),
    "/GỐC": (TokenType.UNIT, "/GỐC"),
    "TRÊN CÂY": (TokenType.UNIT, "/CÂY"),
    "TRÊN 1 CÂY": (TokenType.UNIT, "/CÂY"),
    "/CÂY": (TokenType.UNIT, "/CÂY"),
    # Đơn vị người / lao động
    "NGƯỜI": (TokenType.UNIT, "NGƯỜI"),
    "NGƯỜI LÀM": (TokenType.UNIT, "NGƯỜI"),
    "LAO ĐỘNG": (TokenType.UNIT, "NGƯỜI"),
    "CÔNG NHÂN": (TokenType.UNIT, "NGƯỜI"),
    "CÁI": (TokenType.UNIT, "CÁI"),
    "BỘ": (TokenType.UNIT, "BỘ"),

    "TẤN TRÊN NĂM": (TokenType.UNIT, "TẤN/NĂM"),
    "TẤN/NĂM": (TokenType.UNIT, "TẤN/NĂM"),
}

