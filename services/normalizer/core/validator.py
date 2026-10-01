class Validator:
    @staticmethod
    def is_valid_date(day: int | None, month: int | None, year: int | None) -> bool:
        """
        Kiểm tra tính hợp lệ của ngày tháng năm.
        Trả về True nếu logic hợp lệ (VD: không có ngày 31/02).
        """
        if month is not None and (month < 1 or month > 12):
            return False
            
        if day is not None:
            if day < 1 or day > 31:
                return False
            
            if month is not None:
                max_days = 31
                if month in [4, 6, 9, 11]:
                    max_days = 30
                elif month == 2:
                    if year is not None:
                        is_leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
                        max_days = 29 if is_leap else 28
                    else:
                        max_days = 29 # Cho phép 29 nếu không biết năm
                        
                if day > max_days:
                    return False
                    
        return True
