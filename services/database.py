"""
Module quản lý kết nối và thao tác với CSDL MySQL (vietgap_diary).
Hỗ trợ kiến trúc thiết bị biên (Edge Device Architecture):
- 0. Users & Farm & Plots & Crop Seasons
- 1. Danh mục: suppliers, pesticides, fertilizers, materials
- 2. Bảng gốc hoạt động canh tác: cultivation_activities, activity_photos, activity_voice_records
- 3. Bảng con mở rộng 1-1: pesticide_applications, fertilizer_applications, pest_observations, irrigation_records, harvest_records
- 4. Bảng giao dịch mùa vụ / độc lập: input_purchases, sales_records, waste_disposals
- 5. Bảng AI & Đồng bộ: ai_extraction_records, sync_queue
- 6. Hỗ trợ biểu mẫu VietGAP chuẩn (Bảng 1.5, 1.6, 1.7, 1.8, 1.9) và tương thích ngược 100% với giao diện hiện tại.
"""

import os
import re
import json
import logging
from datetime import datetime, date, timedelta
from decimal import Decimal
from typing import Optional, Dict, Any, List, Union
import pymysql
from pymysql.cursors import DictCursor
from werkzeug.security import generate_password_hash, check_password_hash

import config

logger = logging.getLogger("nong-ky-db")


def _serialize_db_val(v: Any) -> Any:
    """Chuyển đổi kiểu dữ liệu MySQL sang JSON-friendly format."""
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, timedelta):
        tot = int(v.total_seconds())
        h = tot // 3600
        m = (tot % 3600) // 60
        s = tot % 60
        return f"{h:02d}:{m:02d}:{s:02d}"
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dict):
        return {k: _serialize_db_val(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_serialize_db_val(item) for item in v]
    return v


def _serialize_row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Serialize toàn bộ các trường trong dict MySQL."""
    if not row:
        return row
    return {k: _serialize_db_val(v) for k, v in row.items()}



def get_db_connection():
    """Tạo kết nối đến MySQL vietgap_diary (hỗ trợ unix_socket và host/port TCP)."""
    conn_kwargs = {
        "user": config.MYSQL_USER,
        "password": config.MYSQL_PASSWORD,
        "database": getattr(config, "MYSQL_DATABASE", "vietgap_diary"),
        "charset": "utf8mb4",
        "cursorclass": DictCursor,
        "autocommit": True,
    }

    if getattr(config, "MYSQL_SOCKET", None) and os.path.exists(config.MYSQL_SOCKET):
        conn_kwargs["unix_socket"] = config.MYSQL_SOCKET
    else:
        conn_kwargs["host"] = config.MYSQL_HOST
        conn_kwargs["port"] = config.MYSQL_PORT

    return pymysql.connect(**conn_kwargs)


def _parse_number(val: Any) -> Optional[float]:
    """Trích xuất số từ chuỗi như '50 kg', '2 lít', '5 bao', '1.5'."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", ".")
    m = re.search(r"[-+]?\d*\.?\d+", s)
    if m:
        try:
            return float(m.group(0))
        except ValueError:
            pass
    return None


def _parse_date(val: Any) -> Optional[str]:
    """Chuẩn hóa ngày về YYYY-MM-DD hợp lệ cho MySQL DATE."""
    if val is None:
        return None
    if isinstance(val, (date, datetime)):
        return val.strftime("%Y-%m-%d")
    s = str(val).strip()
    if not s or s.lower() in ("none", "null", "—", "-", "undefined", "''", '""'):
        return None
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s)
    if m:
        try:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError:
            pass
    return None


def _parse_time(val: Any) -> Optional[str]:
    """Chuẩn hóa giờ về HH:MM:SS."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.strftime("%H:%M:%S")
    s = str(val).strip()
    m = re.search(r"(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?", s)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2))
        sec = int(m.group(3)) if m.group(3) else 0
        return f"{h:02d}:{mi:02d}:{sec:02d}"
    return None


def _ensure_supplier(cur, name: Optional[str], supplier_type: Optional[str] = None) -> Optional[int]:
    """Tìm hoặc tạo mới nhà cung cấp trong bảng suppliers."""
    if not name or not str(name).strip():
        return None
    cleaned = str(name).strip()
    cur.execute("SELECT supplier_id FROM suppliers WHERE LOWER(name) = LOWER(%s) LIMIT 1;", (cleaned,))
    row = cur.fetchone()
    if row:
        return row["supplier_id"]

    cur.execute(
        "INSERT INTO suppliers (name, supplier_type) VALUES (%s, %s);",
        (cleaned, supplier_type or "Đại lý VTNN"),
    )
    return cur.lastrowid


def _ensure_pesticide(cur, name: Optional[str], target_pest: Optional[str] = None, min_iso: int = 7) -> Optional[int]:
    """Tìm hoặc tạo mới thuốc BVTV trong bảng pesticides."""
    if not name or not str(name).strip():
        return None
    cleaned = str(name).strip()
    cur.execute("SELECT pesticide_id FROM pesticides WHERE LOWER(name) = LOWER(%s) LIMIT 1;", (cleaned,))
    row = cur.fetchone()
    if row:
        return row["pesticide_id"]

    cur.execute(
        """
        INSERT INTO pesticides (name, min_isolation_days, allowed_crops)
        VALUES (%s, %s, %s);
        """,
        (cleaned, min_iso, "Cây ăn trái / Rau màu"),
    )
    return cur.lastrowid


def _ensure_fertilizer(cur, name: Optional[str], fert_type: Optional[str] = None) -> Optional[int]:
    """Tìm hoặc tạo mới phân bón trong bảng fertilizers."""
    if not name or not str(name).strip():
        return None
    cleaned = str(name).strip()
    cur.execute("SELECT fertilizer_id FROM fertilizers WHERE LOWER(name) = LOWER(%s) LIMIT 1;", (cleaned,))
    row = cur.fetchone()
    if row:
        return row["fertilizer_id"]

    f_type = fert_type or ("Hữu cơ" if "hữu cơ" in cleaned.lower() else "Vô cơ")
    cur.execute(
        "INSERT INTO fertilizers (name, fertilizer_type) VALUES (%s, %s);",
        (cleaned, f_type),
    )
    return cur.lastrowid


def _ensure_material(cur, name: Optional[str], material_type: str = "MATERIAL", unit: Optional[str] = None) -> Optional[int]:
    """Tìm hoặc tạo mới vật tư / hạt giống trong bảng materials."""
    if not name or not str(name).strip():
        return None
    cleaned = str(name).strip()
    cur.execute("SELECT material_id FROM materials WHERE LOWER(name) = LOWER(%s) LIMIT 1;", (cleaned,))
    row = cur.fetchone()
    if row:
        return row["material_id"]

    cur.execute(
        "INSERT INTO materials (name, material_type, unit) VALUES (%s, %s, %s);",
        (cleaned, material_type, unit or "cái"),
    )
    return cur.lastrowid


def get_crop_name_for_farmer(conn, user_id: Optional[Any] = None, season_id: Optional[Any] = None) -> str:
    """Lấy tên giống cây đang trồng từ crop_seasons hoặc farm cho nông dân, ưu tiên season_id nếu có."""
    try:
        with conn.cursor() as cur:
            if season_id:
                cur.execute(
                    "SELECT crop_name FROM crop_seasons WHERE season_id = %s LIMIT 1;",
                    (season_id,),
                )
                row = cur.fetchone()
                if row and row.get("crop_name"):
                    return row["crop_name"]

            if user_id:
                cur.execute(
                    """
                    SELECT cs.crop_name
                    FROM crop_seasons cs
                    JOIN plots p ON cs.plot_id = p.plot_id
                    JOIN farm f ON p.farm_id = f.farm_id
                    WHERE f.owner_user_id = %s
                    ORDER BY cs.created_at DESC LIMIT 1;
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
                if row and row.get("crop_name"):
                    return row["crop_name"]

            # Fallback 1: Lấy vụ mùa mới nhất trong crop_seasons
            cur.execute("SELECT crop_name FROM crop_seasons ORDER BY created_at DESC LIMIT 1;")
            row = cur.fetchone()
            if row and row.get("crop_name"):
                return row["crop_name"]

            # Fallback 2: Lấy từ farm
            cur.execute("SELECT crop_name FROM farm LIMIT 1;")
            row = cur.fetchone()
            if row and row.get("crop_name"):
                return row["crop_name"]
    except Exception:
        pass
    return "Bưởi"


def get_default_season_id(conn, user_id: Optional[Any] = None) -> int:
    """Lấy season_id hợp lệ từ crop_seasons hoặc tạo mới nếu chưa có."""
    with conn.cursor() as cur:
        if user_id:
            cur.execute(
                """
                SELECT cs.season_id
                FROM crop_seasons cs
                JOIN plots p ON cs.plot_id = p.plot_id
                JOIN farm f ON p.farm_id = f.farm_id
                WHERE f.owner_user_id = %s
                ORDER BY cs.season_id ASC LIMIT 1;
                """,
                (user_id,),
            )
            res = cur.fetchone()
            if res:
                return res["season_id"]

        cur.execute("SELECT season_id FROM crop_seasons ORDER BY season_id ASC LIMIT 1;")
        res = cur.fetchone()
        if res:
            return res["season_id"]

        # Nếu chưa có season nào, tự động tạo cấu trúc farm -> plot -> crop_season
        cur.execute("SELECT farm_id FROM farm LIMIT 1;")
        f_row = cur.fetchone()
        if not f_row:
            cur.execute("SELECT user_id FROM users LIMIT 1;")
            u_row = cur.fetchone()
            u_id = u_row["user_id"] if u_row else None
            cur.execute(
                """
                INSERT INTO farm (owner_user_id, name, farm_code, address, certification_body)
                VALUES (%s, 'HỢP TÁC XÃ ĐẠI ĐỒNG', 'VN-HBOR-0033', 'Xóm Độc Lập - Xã Thịnh Minh - Tỉnh Phú Thọ', 'VietGAP Trồng trọt');
                """,
                (u_id,),
            )
            farm_id = cur.lastrowid
        else:
            farm_id = f_row["farm_id"]

        cur.execute("SELECT plot_id FROM plots WHERE farm_id = %s LIMIT 1;", (farm_id,))
        p_row = cur.fetchone()
        if not p_row:
            cur.execute(
                """
                INSERT INTO plots (farm_id, name, plot_code, crop_name, variety, area_size, area_unit)
                VALUES (%s, 'Lô 1 — Vườn Bưởi diễn Trung tâm', 'VN-HBOR-0033-L01', 'Bưởi diễn', 'Bưởi', 30000.0, 'm2');
                """,
                (farm_id,),
            )
            plot_id = cur.lastrowid
        else:
            plot_id = p_row["plot_id"]

        cur.execute(
            """
            INSERT INTO crop_seasons (plot_id, season_name, crop_name, variety, start_date, expected_yield, yield_unit)
            VALUES (%s, 'Vụ mùa Bưởi diễn 2026', 'Bưởi diễn', 'Bưởi', CURRENT_DATE(), 100.0, 'tấn/năm');
            """,
            (plot_id,),
        )
        return cur.lastrowid


def init_db_and_seed():
    """Khởi tạo và nạp dữ liệu mẫu ban đầu theo schema.sql VietGAP nếu CSDL còn trống."""
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM users;")
            res = cur.fetchone()
            if res and res["total"] > 0:
                cur.execute("SELECT COUNT(*) AS total FROM cultivation_activities;")
                ca_res = cur.fetchone()
                if ca_res and ca_res["total"] > 0:
                    logger.info("✅ CSDL vietgap_diary đã có dữ liệu ban đầu.")
                    conn.close()
                    return

            logger.info("🌱 Đang khởi tạo dữ liệu mẫu cho CSDL vietgap_diary...")

            # 1. Tạo users
            cur.execute(
                """
                INSERT INTO users (username, password, full_name, phone, role)
                VALUES 
                ('nongdan1', %s, 'Vũ Xuân Anh', '0912345678', 'WORKER'),
                ('admin', %s, 'Quản trị viên Hệ thống', '0909999888', 'ADMIN')
                ON DUPLICATE KEY UPDATE full_name=VALUES(full_name);
                """,
                (generate_password_hash("123456"), generate_password_hash("admin123")),
            )

            cur.execute("SELECT user_id FROM users WHERE username = 'nongdan1' LIMIT 1;")
            u1 = cur.fetchone()
            user1_id = u1["user_id"] if u1 else 1

            # 2. Tạo Farm
            cur.execute(
                """
                INSERT INTO farm (owner_user_id, name, farm_code, address, phone, certification_body, certification_no, certification_date, certification_expiry, target_market)
                VALUES (%s, 'HỢP TÁC XÃ ĐẠI ĐỒNG', 'VN-HBOR-0033', 'Xóm Độc Lập - Xã Thịnh Minh - Tỉnh Phú Thọ', '0912345678', 'VietGAP Trồng trọt', 'VietGAP-HBOR-0033', '2025-01-01', '2027-12-31', 'Thị trường hoa quả tươi, siêu thị và thương lái trong nước')
                ON DUPLICATE KEY UPDATE name=VALUES(name);
                """,
                (user1_id,),
            )
            cur.execute("SELECT farm_id FROM farm WHERE owner_user_id = %s LIMIT 1;", (user1_id,))
            f_row = cur.fetchone()
            farm_id = f_row["farm_id"] if f_row else 1

            # 3. Tạo Plots (Lô đất canh tác)
            cur.execute(
                """
                INSERT INTO plots (farm_id, name, plot_code, crop_name, variety, area_size, area_unit, soil_type, water_source, land_use_history)
                VALUES 
                (%s, 'Lô 1 — Vườn Bưởi diễn Trung tâm', 'VN-HBOR-0033-L01', 'Bưởi diễn', 'Bưởi', 30000.0, 'm2', 'Đất đồi trung du', 'Nguồn nước tự nhiên / Giếng khoan', 'Vườn chuyên canh Bưởi diễn VietGAP tại Xã Thịnh Minh'),
                (%s, 'Lô 2 — Vườn Bưởi diễn Bãi Sông Đà', 'VN-HBOR-0033-L02', 'Bưởi diễn', 'Bưởi', 25000.0, 'm2', 'Đất phù sa ven sông', 'Nước sông Đà / Tự nhiên', 'Vườn canh tác Bưởi diễn VietGAP ven sông')
                ON DUPLICATE KEY UPDATE name=VALUES(name);
                """,
                (farm_id, farm_id),
            )
            cur.execute("SELECT plot_id FROM plots WHERE plot_code = 'VN-HBOR-0033-L01' LIMIT 1;")
            p_row = cur.fetchone()
            plot_id = p_row["plot_id"] if p_row else 1

            # 4. Tạo Crop Season (Mùa vụ)
            cur.execute(
                """
                INSERT INTO crop_seasons (plot_id, season_name, crop_name, variety, seed_source, start_date, expected_harvest_date, actual_end_date, expected_yield, actual_yield, yield_unit, target_market)
                VALUES (%s, 'Vụ mùa Bưởi diễn 2026', 'Bưởi diễn', 'Bưởi', 'Vườn ươm giống Bưởi đạt chuẩn VietGAP', '2026-01-01', '2026-11-30', '2026-11-30', 100.0, 84.0, 'tấn/năm', 'Thị trường hoa quả tươi, siêu thị và thương lái trong nước')
                ON DUPLICATE KEY UPDATE season_name=VALUES(season_name);
                """,
                (plot_id,),
            )
            cur.execute("SELECT season_id FROM crop_seasons WHERE plot_id = %s LIMIT 1;", (plot_id,))
            s_row = cur.fetchone()
            season_id = s_row["season_id"] if s_row else 1

            # 5. Tạo danh mục: Suppliers, Pesticides, Fertilizers, Materials
            suppliers_seed = [
                ("Đại lý VTNN Ba Tèo", "Ấp 3, Cẩm Mỹ, Đồng Nai", "0918111222", "Phân bón & Thuốc BVTV"),
                ("Công ty CP Phân bón Bình Điền", "KCN Tân Tạo, Bình Tân, TP.HCM", "02838123456", "Phân bón"),
                ("Trại giống Cái Mơn", "Chợ Lách, Bến Tre", "0903333444", "Cây giống"),
                ("Công ty TNHH Syngenta Việt Nam", "KCN Biên Hòa 2, Đồng Nai", "02513836555", "Thuốc BVTV"),
            ]
            for s_name, s_addr, s_phone, s_type in suppliers_seed:
                cur.execute(
                    """
                    INSERT INTO suppliers (name, address, phone, supplier_type)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE name=VALUES(name);
                    """,
                    (s_name, s_addr, s_phone, s_type),
                )

            pesticides_seed = [
                ("Anvil 5SC", "Hexaconazole", "SC", 14, "Cây ăn trái, lúa"),
                ("Chess 50WG", "Pymetrozine", "WG", 7, "Cây ăn trái, rau màu"),
                ("Ridomil Gold 68WG", "Metalaxyl M + Mancozeb", "WG", 14, "Cây ăn trái, hồ tiêu"),
                ("Confidor 200SL", "Imidacloprid", "SL", 7, "Cây có múi, xoài"),
            ]
            for p_name, p_ing, p_form, p_iso, p_crop in pesticides_seed:
                cur.execute(
                    """
                    INSERT INTO pesticides (name, active_ingredient, formulation, min_isolation_days, allowed_crops)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE name=VALUES(name);
                    """,
                    (p_name, p_ing, p_form, p_iso, p_crop),
                )

            fertilizers_seed = [
                ("NPK 16-16-8 Đầu Trâu", "Vô cơ", "16-16-8", "Công ty Bình Điền"),
                ("Phân hữu cơ vi sinh Quế Lâm", "Hữu cơ vi sinh", "Hữu cơ 15%", "Tập đoàn Quế Lâm"),
                ("DAP 18-46-0", "Vô cơ", "18-46-0", "DAP Đình Vũ"),
                ("NPK 20-20-15 TE", "Vô cơ", "20-20-15", "Đầu Trâu"),
            ]
            for f_name, f_type, f_npk, f_mfg in fertilizers_seed:
                cur.execute(
                    """
                    INSERT INTO fertilizers (name, fertilizer_type, npk_ratio, manufacturer)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE name=VALUES(name);
                    """,
                    (f_name, f_type, f_npk, f_mfg),
                )

            materials_seed = [
                ("Cây giống sầu riêng Ri6", "SEED", "cây", "Cây ghép gốc nhớt 1 năm"),
                ("Màng phủ nông nghiệp PE", "MATERIAL", "cuộn", "Màng phủ 1.2m x 400m"),
                ("Túi bao trái mít", "MATERIAL", "cái", "Túi lưới chống ruồi vàng"),
                ("Béc tưới bù áp 60L/h", "MATERIAL", "cái", "Hệ thống tưới tự động"),
            ]
            for m_name, m_type, m_unit, m_desc in materials_seed:
                cur.execute(
                    """
                    INSERT INTO materials (name, material_type, unit, description)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE name=VALUES(name);
                    """,
                    (m_name, m_type, m_unit, m_desc),
                )

            # Lấy ID của danh mục đã tạo
            cur.execute("SELECT supplier_id FROM suppliers WHERE name LIKE '%%Ba Tèo%%' LIMIT 1;")
            sup_bateo = cur.fetchone()["supplier_id"]
            cur.execute("SELECT supplier_id FROM suppliers WHERE name LIKE '%%Cái Mơn%%' LIMIT 1;")
            sup_caimon = cur.fetchone()["supplier_id"]

            cur.execute("SELECT pesticide_id FROM pesticides WHERE name LIKE '%%Chess%%' LIMIT 1;")
            pest_chess = cur.fetchone()["pesticide_id"]

            cur.execute("SELECT fertilizer_id FROM fertilizers WHERE name LIKE '%%NPK 16-16-8%%' LIMIT 1;")
            fert_npk = cur.fetchone()["fertilizer_id"]

            cur.execute("SELECT material_id FROM materials WHERE name LIKE '%%Cây giống%%' LIMIT 1;")
            mat_giong = cur.fetchone()["material_id"]

            # 6. Seed Nhật ký Mua vật tư (input_purchases) - Bảng 1.5
            purchases_seed = [
                (season_id, sup_bateo, "FERTILIZER", None, fert_npk, None, "2026-03-01", 20.0, "bao", 650000.0, "2028-03-01", "HD-0123", "Mua phân bón gốc đợt 1"),
                (season_id, sup_bateo, "PESTICIDE", pest_chess, None, None, "2026-03-01", 10.0, "gói", 45000.0, "2027-09-01", "HD-0124", "Thuốc trừ rầy rệp phòng trừ"),
                (season_id, sup_caimon, "MATERIAL", None, None, mat_giong, "2026-02-15", 50.0, "cây", 120000.0, None, "HD-0099", "Cây giống sạch bệnh có tem xuất xứ"),
            ]
            for s_id, sp_id, itype, p_id, f_id, m_id, p_date, qty, unit, price, exp, inv, note in purchases_seed:
                cur.execute(
                    """
                    INSERT INTO input_purchases (season_id, supplier_id, item_type, pesticide_id, fertilizer_id, material_id, purchase_date, quantity, unit, unit_price, expiry_date, invoice_no, note)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                    """,
                    (s_id, sp_id, itype, p_id, f_id, m_id, p_date, qty, unit, price, exp, inv, note),
                )

            # 7. Seed Hoạt động Phun thuốc (cultivation_activities + pesticide_applications + activity_voice_records)
            cur.execute(
                """
                INSERT INTO cultivation_activities (season_id, activity_type, activity_date, activity_time, growth_stage, description, weather_condition, performed_by, created_by, source, is_confirmed)
                VALUES (%s, 'PESTICIDE', '2026-03-08', '06:30:00', 'Sinh trưởng', 'Phun thuốc trừ rầy xanh hại đọt non sầu riêng', 'Nắng nhẹ, lặng gió', 'Nguyễn Văn Nông', %s, 'VOICE_AI', TRUE);
                """,
                (season_id, user1_id),
            )
            act_pest_id = cur.lastrowid
            cur.execute(
                """
                INSERT INTO activity_voice_records (activity_id, audio_url, stt_text)
                VALUES (%s, '', 'Sáng nay tôi phun 2 gói Chess 50WG phòng rầy xanh lúc ra đọt non cách ly 7 ngày');
                """,
                (act_pest_id,),
            )
            cur.execute(
                """
                INSERT INTO pesticide_applications (activity_id, pesticide_id, supplier_id, dose_quantity, dose_unit, concentration_value, concentration_unit, application_method, target_pest, isolation_days, safe_harvest_date, note)
                VALUES (%s, %s, %s, 2.0, 'gói', 0.05, '%%', 'Phun sương đều 2 mặt lá', 'Rầy xanh, rầy phấn trắng', 7, '2026-03-15', 'Phun đọt non sáng sớm');
                """,
                (act_pest_id, pest_chess, sup_bateo),
            )

            # 8. Seed Hoạt động Bón phân (cultivation_activities + fertilizer_applications)
            cur.execute(
                """
                INSERT INTO cultivation_activities (season_id, activity_type, activity_date, activity_time, growth_stage, description, weather_condition, performed_by, created_by, source, is_confirmed)
                VALUES (%s, 'FERTILIZER', '2026-03-05', '07:15:00', 'Sinh trưởng', 'Bón phân NPK 16-16-8 Đầu Trâu quanh gốc', 'Nắng tốt', 'Nguyễn Văn Nông', %s, 'VOICE_AI', TRUE);
                """,
                (season_id, user1_id),
            )
            act_fert_id = cur.lastrowid
            cur.execute(
                """
                INSERT INTO fertilizer_applications (activity_id, fertilizer_id, supplier_id, quantity, unit, application_method, note)
                VALUES (%s, %s, %s, 50.0, 'kg', 'Rải quanh tán gốc lấp đất nhẹ', 'Bón đợt 1');
                """,
                (act_fert_id, fert_npk, sup_bateo),
            )

            # 9. Seed Hoạt động Canh tác khác (cultivation_activities - OTHER: dọn cỏ, cắt tỉa cành)
            cur.execute(
                """
                INSERT INTO cultivation_activities (season_id, activity_type, activity_date, activity_time, growth_stage, description, weather_condition, performed_by, created_by, source, is_confirmed)
                VALUES (%s, 'OTHER', '2026-03-10', '08:00:00', 'Sinh trưởng', 'Dọn cỏ phát sạch quanh bồn và cắt tỉa cành tăm', 'Nắng ráo', 'Nguyễn Văn Nông', %s, 'VOICE_AI', TRUE);
                """,
                (season_id, user1_id),
            )
            act_other_id = cur.lastrowid
            cur.execute(
                """
                INSERT INTO activity_voice_records (activity_id, audio_url, stt_text)
                VALUES (%s, '', 'Hôm nay dọn cỏ quanh gốc và tỉa cành tăm, không có sinh vật gây hại');
                """,
                (act_other_id,),
            )

            # 10. Seed Thu hoạch và Bán (cultivation_activities + harvest_records + sales_records) - Bảng 1.8 & 1.9
            cur.execute(
                """
                INSERT INTO cultivation_activities (season_id, activity_type, activity_date, activity_time, growth_stage, description, weather_condition, performed_by, created_by, source, is_confirmed)
                VALUES (%s, 'HARVEST', '2026-03-12', '09:00:00', 'Thu hoạch', 'Thu hoạch đợt 1 trái sầu riêng chín cây', 'Nắng đẹp', 'Nguyễn Văn Nông', %s, 'VOICE_AI', TRUE);
                """,
                (season_id, user1_id),
            )
            act_harv_id = cur.lastrowid
            cur.execute(
                """
                INSERT INTO harvest_records (activity_id, season_id, quantity, unit, processing_location, harvester_name, note)
                VALUES (%s, %s, 1200.0, 'kg', 'Khu sơ chế đóng thùng xốp tại vườn', 'Tổ thu hoạch 4 người', 'Trái đạt chuẩn loại 1');
                """,
                (act_harv_id, season_id),
            )
            harvest_id = cur.lastrowid

            cur.execute(
                """
                INSERT INTO sales_records (harvest_id, buyer_name, buyer_contact, sale_date, quantity, unit, unit_price, total_amount, note)
                VALUES (%s, 'Thương lái Ba Tèo', '0909123456', '2026-03-12', 1200.0, 'kg', 85000.0, 102000000.0, 'Cân tại vườn giao đủ tiền');
                """,
                (harvest_id,),
            )

            # 11. Seed Xử lý bao bì (waste_disposals) - Bảng 1.7
            cur.execute(
                """
                INSERT INTO waste_disposals (season_id, disposal_date, waste_type, quantity, unit, storage_location, treatment_method, note)
                VALUES (%s, '2026-03-09', 'Vỏ gói thuốc BVTV Chess 50WG', 2.0, 'gói', 'Bể xi măng chứa bao bì có nắp đậy tại vườn', 'Súc rửa 3 lần đem bỏ hố chứa tập trung bàn giao đơn vị thu gom', 'Tuân thủ VietGAP');
                """,
                (season_id,),
            )

            conn.close()
            logger.info("✅ Đã nạp thành công bộ dữ liệu mẫu VietGAP vào vietgap_diary!")
    except Exception as e:
        logger.exception("Lỗi khi khởi tạo CSDL vietgap_diary: %s", e)


def authenticate_farmer(username: str, password: str) -> Optional[Dict[str, Any]]:
    """Xác thực đăng nhập nông dân / người dùng theo bảng users, farm và crop_seasons."""
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 
                    u.user_id, u.username, u.password, u.full_name, u.phone, u.role,
                    f.farm_id, f.name AS farm_name, f.farm_code, f.address AS farm_address,
                    f.certification_body, f.certification_no, f.target_market,
                    f.total_area, f.area_text,
                    COALESCE(
                        (SELECT cs_sub.crop_name FROM crop_seasons cs_sub JOIN plots p_sub ON cs_sub.plot_id = p_sub.plot_id WHERE p_sub.farm_id = f.farm_id ORDER BY cs_sub.created_at DESC LIMIT 1),
                        f.crop_name,
                        'Bưởi'
                    ) AS crop_name,
                    COALESCE(
                        (SELECT cs_sub.variety FROM crop_seasons cs_sub JOIN plots p_sub ON cs_sub.plot_id = p_sub.plot_id WHERE p_sub.farm_id = f.farm_id ORDER BY cs_sub.created_at DESC LIMIT 1),
                        f.variety,
                        'Bưởi'
                    ) AS variety,
                    f.plant_date, f.harvest_date, f.growth_stage, f.estimated_yield, f.actual_yield, f.buyer
                FROM users u
                LEFT JOIN farm f ON u.user_id = f.owner_user_id
                WHERE u.username = %s LIMIT 1;
                """,
                (username,),
            )
            user_row = cur.fetchone()
            conn.close()

            if not user_row:
                return None

            stored_pw = user_row.get("password") or ""
            is_valid = False
            try:
                if stored_pw.startswith("pbkdf2:") or stored_pw.startswith("scrypt:"):
                    is_valid = check_password_hash(stored_pw, password)
                else:
                    is_valid = (stored_pw == password)
            except Exception:
                is_valid = (stored_pw == password)

            if is_valid:
                user_row.pop("password", None)
                user_row["id"] = user_row.get("user_id")
                user_row["farmer_id"] = user_row.get("user_id")
                if not user_row.get("growth_stage"):
                    user_row["growth_stage"] = "Sinh trưởng"
                return user_row

    except Exception as e:
        logger.exception("Lỗi xác thực người dùng: %s", e)
    return None


def get_farmer_by_id(user_id: Any) -> Optional[Dict[str, Any]]:
    """Lấy thông tin nông dân / người dùng theo ID."""
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 
                    u.user_id, u.username, u.full_name, u.phone, u.role,
                    f.farm_id, f.name AS farm_name, f.farm_code, f.address AS farm_address,
                    f.certification_body, f.certification_no, f.target_market,
                    f.total_area, f.area_text,
                    COALESCE(
                        (SELECT cs_sub.crop_name FROM crop_seasons cs_sub JOIN plots p_sub ON cs_sub.plot_id = p_sub.plot_id WHERE p_sub.farm_id = f.farm_id ORDER BY cs_sub.created_at DESC LIMIT 1),
                        f.crop_name,
                        'Bưởi'
                    ) AS crop_name,
                    COALESCE(
                        (SELECT cs_sub.variety FROM crop_seasons cs_sub JOIN plots p_sub ON cs_sub.plot_id = p_sub.plot_id WHERE p_sub.farm_id = f.farm_id ORDER BY cs_sub.created_at DESC LIMIT 1),
                        f.variety,
                        'Bưởi'
                    ) AS variety,
                    f.plant_date, f.harvest_date, f.growth_stage, f.estimated_yield, f.actual_yield, f.buyer
                FROM users u
                LEFT JOIN farm f ON u.user_id = f.owner_user_id
                WHERE u.user_id = %s LIMIT 1;
                """,
                (user_id,),
            )
            row = cur.fetchone()
            conn.close()
            if row:
                row["id"] = row.get("user_id")
                row["farmer_id"] = row.get("user_id")
                if not row.get("growth_stage"):
                    row["growth_stage"] = "Sinh trưởng"
            return row
    except Exception as e:
        logger.exception("Lỗi get_farmer_by_id: %s", e)
        return None


GROWTH_STAGES = [
    "Sau thu hoạch",
    "Sinh trưởng",
    "Ra hoa",
    "Quả non",
    "Thu hoạch",
]

GROWTH_STAGE_ORDER = {stage: idx for idx, stage in enumerate(GROWTH_STAGES)}


def infer_pomelo_stage_from_date(date_val: Any, fallback: Optional[str] = None) -> str:
    """
    Suy luận giai đoạn sinh trưởng của cây Bưởi diễn theo tháng thực hiện trong năm
    nếu bản ghi độc lập (như Mua vật tư, Xử lý bao bì, Tiêu thụ) không lưu trực tiếp cột growth_stage.
    Trình tự sinh học chuẩn của cây Bưởi diễn:
      1. Sau thu hoạch (Tháng 1): Vệ sinh vườn, cắt tỉa cành, bón phân hữu cơ phục hồi.
      2. Sinh trưởng (Tháng 2): Bật mầm lộc xuân, phát triển cành lá, phòng sâu vẽ bùa.
      3. Ra hoa (Tháng 3 - 4): Phân hóa mầm hoa, bung hoa, thụ phấn, đậu tượng quả.
      4. Quả non (Tháng 5 - 9): Tuyển quả, bao trái, bón thúc nuôi quả lớn và vào đường.
      5. Thu hoạch (Tháng 10 - 12): Kiểm tra độ chín, thu hoạch và tiêu thụ bưởi thương phẩm.
    """
    if fallback and str(fallback).strip() in GROWTH_STAGE_ORDER:
        return str(fallback).strip()
    d_str = _parse_date(date_val)
    if d_str:
        try:
            month = int(d_str.split("-")[1])
            if month == 1:
                return "Sau thu hoạch"
            elif month == 2:
                return "Sinh trưởng"
            elif month in (3, 4):
                return "Ra hoa"
            elif 5 <= month <= 9:
                return "Quả non"
            else:
                return "Thu hoạch"
        except Exception:
            pass
    return fallback or "Sinh trưởng"


def detect_growth_stage_from_text(text: Optional[str]) -> Optional[str]:
    """
    Nhận diện giai đoạn sinh trưởng của mùa vụ dựa vào lời nói hoặc văn bản ghi chép của người dân.
    Chuẩn hóa chính xác về 5 giai đoạn chính theo quy chuẩn:
    1. 'Sau thu hoạch'
    2. 'Ra hoa'
    3. 'Quả non'
    4. 'Sinh trưởng'
    5. 'Thu hoạch'
    """
    if not text or not str(text).strip():
        return None
    t = str(text).lower().strip()

    # Kiểm tra trực tiếp các tên giai đoạn chuẩn
    for stage in GROWTH_STAGES:
        if t == stage.lower():
            return stage

    keyword_map = [
        # 1. Sau thu hoạch (kiểm tra trước Thu hoạch)
        (["sau thu hoạch", "phục hồi sau thu hoạch", "phục hồi cây", "tỉa cành già", "tỉa cành tạo tán", "rửa vườn", "dọn vườn sau thu hoạch", "cắt tỉa sau thu hoạch", "vệ sinh vườn"], "Sau thu hoạch"),
        # 2. Quả non (trái non, nuôi trái, đậu quả, đậu trái...)
        (["quả non", "trái non", "nuôi trái", "nuôi quả", "đậu quả", "đậu trái", "kết trái", "lớn trái", "nuôi củ", "rụng sinh lý", "chạy trái", "vào hạt", "phun dưỡng trái", "bón nuôi trái"], "Quả non"),
        # 3. Ra hoa
        (["ra hoa", "nở hoa", "phân hóa mầm hoa", "chớm hoa", "bung hoa", "đang có hoa", "kết nụ", "nhú nụ", "làm bông", "xử lý ra hoa", "thụ phấn"], "Ra hoa"),
        # 4. Thu hoạch
        (["chuẩn bị thu hoạch", "sắp thu hoạch", "đang thu hoạch", "thu hoạch", "thu hái", "thu bói", "chín", "hái trái", "hái quả", "cắt trái"], "Thu hoạch"),
        # 5. Sinh trưởng
        (["sinh trưởng", "đang sinh trưởng", "phát triển thân lá", "nuôi cành lá", "sinh trưởng mạnh", "đẻ nhánh", "cây con", "mới trồng", "xuống giống", "bén rễ", "gieo hạt", "gieo trồng", "ra đọt non", "làm đọt", "đọt non", "đâm chồi", "ra lá non", "búp non", "phát đọt", "chuẩn bị đất", "làm đất", "làm luống", "xới đất", "lên luống", "phát triển"], "Sinh trưởng"),
    ]

    # Kiểm tra các cấu trúc nói: "giai đoạn X", "thời kỳ X", "đợt X", "thời điểm X"
    m = re.search(r"(?:giai\s*đoạn|thời\s*kỳ|thời\s*điểm|đợt)\s+([a-zA-Zà-ỹÀ-Ỹ0-9\s/]+?)(?:,|\.|\bđể\b|\bcho\b|\bvà\b|\bnhưng\b|\bnên\b|\blúc\b|$)", t)
    if m:
        phrase = m.group(1).strip()
        for keywords, stage_name in keyword_map:
            for kw in keywords:
                if kw in phrase:
                    return stage_name
        for stage in GROWTH_STAGES:
            if stage.lower() in phrase:
                return stage

    # Kiểm tra trực tiếp trong toàn bộ câu theo thứ tự ưu tiên
    for keywords, stage_name in keyword_map:
        for kw in keywords:
            if re.search(r"\b" + re.escape(kw) + r"\b", t):
                return stage_name

    for keywords, stage_name in keyword_map:
        for kw in keywords:
            if kw in t:
                return stage_name

    return None


def update_season_growth_stage(growth_stage: str, farmer_id: Optional[Any] = None, season_id: Optional[int] = None) -> bool:
    """Cập nhật giai đoạn sinh trưởng của mùa vụ và trang trại hiện tại."""
    if not growth_stage or not str(growth_stage).strip():
        return False
    stage_clean = str(growth_stage).strip()
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            if season_id:
                cur.execute("UPDATE crop_seasons SET growth_stage = %s WHERE season_id = %s;", (stage_clean, season_id))
            else:
                cur.execute("UPDATE crop_seasons SET growth_stage = %s ORDER BY season_id DESC LIMIT 1;", (stage_clean,))

            if farmer_id:
                cur.execute("UPDATE farm SET growth_stage = %s WHERE owner_user_id = %s;", (stage_clean, farmer_id))
            else:
                cur.execute("UPDATE farm SET growth_stage = %s ORDER BY farm_id ASC LIMIT 1;", (stage_clean,))

            conn.commit()
            conn.close()
            logger.info("🌱 Đã cập nhật giai đoạn mùa vụ vào CSDL: '%s'", stage_clean)
            return True
    except Exception as e:
        logger.exception("Lỗi update_season_growth_stage: %s", e)
        return False


def get_diary_by_tables(farmer_id: Optional[Any] = None, limit: int = 100) -> Dict[str, List[Dict[str, Any]]]:
    """
    Truy vấn toàn bộ các bảng dữ liệu của schema vietgap_diary:
    - Bảng 1.5: input_purchases / b1_5_material_purchases
    - Bảng 1.6: b1_6_material_usages (gộp pesticide_applications và fertilizer_applications)
    - Phun thuốc: pesticide_applications
    - Bón phân: fertilizer_applications
    - Canh tác khác: cultivation_activities (OTHER) / cultivation_diaries
    - Bảng 1.7: waste_disposals / b1_7_waste_treatments
    - Bảng 1.8: harvest_records / b1_8_harvests
    - Bảng 1.9: sales_records / b1_9_sales
    - Sâu bệnh: pest_observations
    - Tưới nước: irrigation_records
    """
    tables_data: Dict[str, List[Dict[str, Any]]] = {
        # 5 Biểu mẫu chính thức VietGAP (vietGAP.pdf)
        "b1_5_material_purchases": [],
        "b1_6_material_usages": [],
        "b1_7_waste_treatments": [],
        "b1_8_harvests": [],
        "b1_9_sales": [],
        "b1_8_harvest_sales": [],

        # Tên bảng mới theo schema.sql vietgap_diary
        "input_purchases": [],
        "pesticide_applications": [],
        "fertilizer_applications": [],
        "pest_observations": [],
        "irrigation_records": [],
        "cultivation_activities": [],
        "harvest_records": [],
        "sales_records": [],
        "waste_disposals": [],

        # Alias cũ tương thích ngược 100%
        "material_purchases": [],
        "material_usages": [],
        "waste_treatments": [],
        "cultivation_diaries": [],
        "harvests": [],
        "sales": [],
        "diary_pests": [],
    }

    def _extract_media_from_note(note_text: Optional[str]):
        if not note_text:
            return "", "", ""
        raw_n = str(note_text)
        p_url = ""
        a_url = ""
        m_p = re.search(r"Ảnh(?:\s*chứng\s*từ)?:\s*(\S+)", raw_n, flags=re.IGNORECASE)
        if m_p:
            p_url = m_p.group(1).rstrip(";,")
        m_a = re.search(r"Ghi\s*âm:\s*(\S+)", raw_n, flags=re.IGNORECASE)
        if m_a:
            a_url = m_a.group(1).rstrip(";,")
        clean_n = re.sub(r"[;\s]*Ảnh(?:\s*chứng\s*từ)?:\s*\S+", "", raw_n, flags=re.IGNORECASE)
        clean_n = re.sub(r"[;\s]*Ghi\s*âm:\s*\S+", "", clean_n, flags=re.IGNORECASE).strip(" ;")
        return clean_n, p_url, a_url

    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            photo_map: Dict[int, str] = {}
            voice_map: Dict[int, Any] = {}
            try:
                cur.execute("SELECT activity_id, photo_url FROM activity_photos ORDER BY photo_id ASC;")
                for pr in cur.fetchall():
                    if pr.get("activity_id") and pr.get("photo_url"):
                        p_val = str(pr["photo_url"]).strip()
                        if p_val and not p_val.startswith("/") and not p_val.startswith("http"):
                            p_val = f"/uploads/{os.path.basename(p_val)}" if "uploads" in p_val else f"/static/photos/{os.path.basename(p_val)}"
                        elif p_val.startswith("/root/"):
                            p_val = f"/uploads/{os.path.basename(p_val)}" if "/uploads/" in p_val else f"/static/photos/{os.path.basename(p_val)}"
                        photo_map[int(pr["activity_id"])] = p_val
            except Exception as _ep:
                logger.debug("Không đọc được activity_photos: %s", _ep)

            try:
                cur.execute("SELECT activity_id, audio_url, stt_text FROM activity_voice_records ORDER BY voice_id ASC;")
                for vr in cur.fetchall():
                    if vr.get("activity_id"):
                        voice_map[int(vr["activity_id"])] = (
                            vr.get("audio_url") or "",
                            vr.get("stt_text") or "",
                        )
            except Exception as _ev:
                logger.debug("Không đọc được activity_voice_records: %s", _ev)

            # 1. BẢNG 1.5: MUA VẬT TƯ (input_purchases)
            cur.execute(
                """
                SELECT 
                    ip.purchase_id,
                    ip.purchase_date,
                    ip.item_type,
                    ip.quantity,
                    ip.unit,
                    ip.unit_price,
                    ip.batch_no,
                    ip.expiry_date,
                    ip.invoice_no,
                    ip.note,
                    ip.created_at,
                    COALESCE(p.name, f.name, m.name, 'Vật tư nông nghiệp') AS material_name,
                    s.name AS supplier_name,
                    s.address AS supplier_address,
                    cs.season_name
                FROM input_purchases ip
                LEFT JOIN pesticides p ON ip.pesticide_id = p.pesticide_id
                LEFT JOIN fertilizers f ON ip.fertilizer_id = f.fertilizer_id
                LEFT JOIN materials m ON ip.material_id = m.material_id
                LEFT JOIN suppliers s ON ip.supplier_id = s.supplier_id
                JOIN crop_seasons cs ON ip.season_id = cs.season_id
                ORDER BY ip.purchase_date DESC, ip.created_at DESC
                LIMIT %s;
                """,
                (limit,),
            )
            for r in cur.fetchall():
                p_qty = float(r["quantity"]) if r["quantity"] is not None else None
                q_str = f"{int(p_qty) if p_qty and p_qty.is_integer() else p_qty} {r.get('unit') or ''}".strip()
                p_date_str = str(r["purchase_date"]) if r["purchase_date"] else ""
                p_stage = infer_pomelo_stage_from_date(p_date_str)
                clean_note, note_photo_url, note_audio_url = _extract_media_from_note(r.get("note"))
                item = {
                    "id": r["purchase_id"],
                    "purchase_id": r["purchase_id"],
                    "table": "input_purchases",
                    "ngay_thuc_hien": p_date_str,
                    "purchase_date": p_date_str,
                    "activity_time": "08:00:00",
                    "growth_stage": p_stage,
                    "giai_doan": p_stage,
                    "ten_vat_tu": r.get("material_name"),
                    "material_name": r.get("material_name"),
                    "material_type": r.get("item_type"),
                    "so_luong_vat_tu": q_str,
                    "quantity": p_qty,
                    "unit": r.get("unit"),
                    "unit_price": float(r["unit_price"]) if r.get("unit_price") is not None else None,
                    "noi_cung_cap": r.get("supplier_name"),
                    "supplier_name": r.get("supplier_name"),
                    "supplier_address": r.get("supplier_address"),
                    "han_su_dung": str(r["expiry_date"]) if r["expiry_date"] else "",
                    "expiry_date": str(r["expiry_date"]) if r["expiry_date"] else "",
                    "invoice_no": r.get("invoice_no"),
                    "note": clean_note,
                    "photo_url": note_photo_url,
                    "audio_url": note_audio_url,
                    "season_name": r.get("season_name"),
                    "created_at": r["created_at"].isoformat() if r.get("created_at") else "",
                }
                tables_data["input_purchases"].append(item)
                tables_data["b1_5_material_purchases"].append(item)
                tables_data["material_purchases"].append(item)

            # 2. BẢNG PHUN THUỐC (pesticide_applications JOIN cultivation_activities)
            cur.execute(
                """
                SELECT 
                    pa.application_id,
                    pa.activity_id,
                    ca.activity_date,
                    ca.activity_time,
                    ca.growth_stage,
                    ca.weather_condition,
                    ca.description,
                    ca.performed_by,
                    ca.created_at,
                    pl.plot_code,
                    pl.name AS plot_name,
                    p.name AS pesticide_name,
                    s.name AS supplier_name,
                    pa.dose_quantity,
                    pa.dose_unit,
                    pa.concentration_value,
                    pa.concentration_unit,
                    pa.application_method,
                    pa.target_pest,
                    pa.isolation_days,
                    pa.safe_harvest_date,
                    pa.note,
                    vr.audio_url,
                    vr.stt_text
                FROM pesticide_applications pa
                JOIN cultivation_activities ca ON pa.activity_id = ca.activity_id
                LEFT JOIN plots pl ON ca.plot_id = pl.plot_id
                JOIN pesticides p ON pa.pesticide_id = p.pesticide_id
                LEFT JOIN suppliers s ON pa.supplier_id = s.supplier_id
                LEFT JOIN activity_voice_records vr ON ca.activity_id = vr.activity_id
                ORDER BY ca.activity_date DESC, ca.activity_time DESC, ca.created_at DESC
                LIMIT %s;
                """,
                (limit,),
            )
            for r in cur.fetchall():
                dose_str = ""
                if r.get("dose_quantity") is not None:
                    dq = float(r["dose_quantity"])
                    dq_fmt = f"{int(dq) if dq.is_integer() else dq}"
                    dose_str = f"{dq_fmt} {r.get('dose_unit') or ''}".strip()
                if r.get("concentration_value") is not None:
                    cv = float(r["concentration_value"])
                    cv_fmt = f"{int(cv) if cv.is_integer() else cv}"
                    c_str = f"{cv_fmt} {r.get('concentration_unit') or '%'}"
                    dose_str = f"{dose_str} (Nồng độ: {c_str})".strip() if dose_str else c_str

                act_date_str = str(r["activity_date"]) if r["activity_date"] else ""
                p_stage = infer_pomelo_stage_from_date(act_date_str, r.get("growth_stage"))
                item_pest = {
                    "id": r["activity_id"],
                    "application_id": r["application_id"],
                    "activity_id": r["activity_id"],
                    "table": "pesticide_applications",
                    "ngay_thuc_hien": act_date_str,
                    "activity_date": act_date_str,
                    "activity_time": str(r["activity_time"]) if r["activity_time"] else "",
                    "ten_thuoc_bvtv": r.get("pesticide_name"),
                    "pesticide_name": r.get("pesticide_name"),
                    "material_name": r.get("pesticide_name"),
                    "material_type": "PESTICIDE",
                    "sinh_vat_gay_hai": r.get("target_pest") or "Không phát hiện",
                    "target_pest": r.get("target_pest") or "Không phát hiện",
                    "nong_do_va_luong_dung": dose_str,
                    "dose_quantity": float(r["dose_quantity"]) if r.get("dose_quantity") is not None else None,
                    "dose_unit": r.get("dose_unit"),
                    "concentration_value": float(r["concentration_value"]) if r.get("concentration_value") is not None else None,
                    "concentration_unit": r.get("concentration_unit"),
                    "phuong_phap_bon": r.get("application_method"),
                    "application_method": r.get("application_method"),
                    "thoi_gian_cach_ly": f"{r['isolation_days']} ngày" if r.get("isolation_days") is not None else "",
                    "isolation_days": r.get("isolation_days"),
                    "safe_harvest_date": str(r["safe_harvest_date"]) if r.get("safe_harvest_date") else "",
                    "growth_stage": p_stage,
                    "giai_doan": p_stage,
                    "description": r.get("description"),
                    "plot_code": r.get("plot_code"),
                    "plot_name": r.get("plot_name"),
                    "weather_condition": r.get("weather_condition"),
                    "performed_by": r.get("performed_by"),
                    "supplier_name": r.get("supplier_name"),
                    "note": r.get("note"),
                    "raw_text": r.get("stt_text") or voice_map.get(r["activity_id"], ("", ""))[1],
                    "audio_url": r.get("audio_url") or voice_map.get(r["activity_id"], ("", ""))[0],
                    "photo_url": photo_map.get(r["activity_id"], ""),
                    "created_at": r["created_at"].isoformat() if r.get("created_at") else "",
                }
                tables_data["pesticide_applications"].append(item_pest)
                tables_data["b1_6_material_usages"].append(item_pest)
                tables_data["material_usages"].append(item_pest)

            # 3. BẢNG BÓN PHÂN (fertilizer_applications JOIN cultivation_activities)
            cur.execute(
                """
                SELECT 
                    fa.application_id,
                    fa.activity_id,
                    ca.activity_date,
                    ca.activity_time,
                    ca.growth_stage,
                    ca.weather_condition,
                    ca.description,
                    ca.performed_by,
                    ca.created_at,
                    pl.plot_code,
                    pl.name AS plot_name,
                    f.name AS fertilizer_name,
                    s.name AS supplier_name,
                    fa.quantity,
                    fa.unit,
                    fa.application_method,
                    fa.note,
                    vr.audio_url,
                    vr.stt_text
                FROM fertilizer_applications fa
                JOIN cultivation_activities ca ON fa.activity_id = ca.activity_id
                LEFT JOIN plots pl ON ca.plot_id = pl.plot_id
                JOIN fertilizers f ON fa.fertilizer_id = f.fertilizer_id
                LEFT JOIN suppliers s ON fa.supplier_id = s.supplier_id
                LEFT JOIN activity_voice_records vr ON ca.activity_id = vr.activity_id
                ORDER BY ca.activity_date DESC, ca.activity_time DESC, ca.created_at DESC
                LIMIT %s;
                """,
                (limit,),
            )
            for r in cur.fetchall():
                f_qty = float(r["quantity"]) if r["quantity"] is not None else None
                q_fmt = f"{int(f_qty) if f_qty and f_qty.is_integer() else f_qty} {r.get('unit') or ''}".strip()
                act_date_str = str(r["activity_date"]) if r["activity_date"] else ""
                f_stage = infer_pomelo_stage_from_date(act_date_str, r.get("growth_stage"))
                item_fert = {
                    "id": r["activity_id"],
                    "application_id": r["application_id"],
                    "activity_id": r["activity_id"],
                    "table": "fertilizer_applications",
                    "ngay_thuc_hien": act_date_str,
                    "activity_date": act_date_str,
                    "activity_time": str(r["activity_time"]) if r["activity_time"] else "",
                    "ten_phan_bon": r.get("fertilizer_name"),
                    "fertilizer_name": r.get("fertilizer_name"),
                    "material_name": r.get("fertilizer_name"),
                    "material_type": "FERTILIZER",
                    "luong_phan_bon": q_fmt,
                    "quantity": f_qty,
                    "unit": r.get("unit"),
                    "phuong_phap_bon": r.get("application_method"),
                    "application_method": r.get("application_method"),
                    "growth_stage": f_stage,
                    "giai_doan": f_stage,
                    "description": r.get("description"),
                    "plot_code": r.get("plot_code"),
                    "plot_name": r.get("plot_name"),
                    "weather_condition": r.get("weather_condition"),
                    "performed_by": r.get("performed_by"),
                    "supplier_name": r.get("supplier_name"),
                    "note": r.get("note"),
                    "raw_text": r.get("stt_text") or voice_map.get(r["activity_id"], ("", ""))[1],
                    "audio_url": r.get("audio_url") or voice_map.get(r["activity_id"], ("", ""))[0],
                    "photo_url": photo_map.get(r["activity_id"], ""),
                    "created_at": r["created_at"].isoformat() if r.get("created_at") else "",
                }
                tables_data["fertilizer_applications"].append(item_fert)
                tables_data["b1_6_material_usages"].append(item_fert)
                tables_data["material_usages"].append(item_fert)

            # Sắp xếp b1_6_material_usages theo ngày & giờ thực hiện giảm dần
            tables_data["b1_6_material_usages"].sort(
                key=lambda x: (x.get("ngay_thuc_hien") or "", x.get("activity_time") or "", x.get("created_at") or ""),
                reverse=True,
            )

            # 4. BẢNG THU HOẠCH (harvest_records JOIN cultivation_activities) - Bảng 1.8
            cur.execute(
                """
                SELECT 
                    hr.harvest_id,
                    hr.activity_id,
                    hr.quantity,
                    hr.unit,
                    hr.processing_location,
                    hr.harvester_name,
                    hr.note,
                    ca.activity_date,
                    ca.activity_time,
                    ca.growth_stage,
                    ca.description,
                    ca.performed_by,
                    ca.created_at,
                    pl.plot_code,
                    pl.name AS plot_name,
                    cs.crop_name,
                    cs.variety,
                    vr.audio_url,
                    vr.stt_text
                FROM harvest_records hr
                JOIN cultivation_activities ca ON hr.activity_id = ca.activity_id
                LEFT JOIN plots pl ON ca.plot_id = pl.plot_id
                JOIN crop_seasons cs ON hr.season_id = cs.season_id
                LEFT JOIN activity_voice_records vr ON ca.activity_id = vr.activity_id
                ORDER BY ca.activity_date DESC, ca.activity_time DESC, ca.created_at DESC
                LIMIT %s;
                """,
                (limit,),
            )
            for r in cur.fetchall():
                h_qty = float(r["quantity"]) if r["quantity"] is not None else None
                hq_fmt = f"{int(h_qty) if h_qty and h_qty.is_integer() else h_qty} {r.get('unit') or 'kg'}".strip()
                act_date_str = str(r["activity_date"]) if r["activity_date"] else ""
                h_stage = r.get("growth_stage") or "Thu hoạch"
                item_h = {
                    "id": r["harvest_id"],
                    "harvest_id": r["harvest_id"],
                    "activity_id": r["activity_id"],
                    "table": "harvest_records",
                    "ngay_thuc_hien": act_date_str,
                    "harvest_date": act_date_str,
                    "activity_date": act_date_str,
                    "activity_time": str(r["activity_time"]) if r["activity_time"] else "",
                    "growth_stage": h_stage,
                    "giai_doan": h_stage,
                    "description": r.get("description"),
                    "plot_code": r.get("plot_code"),
                    "plot_name": r.get("plot_name"),
                    "loai_nong_san": r.get("crop_name"),
                    "crop_name": r.get("crop_name"),
                    "variety": r.get("variety"),
                    "san_luong_thu_hoach": hq_fmt,
                    "quantity": h_qty,
                    "unit": r.get("unit") or "kg",
                    "dia_diem_cach_so_che": r.get("processing_location"),
                    "processing_location": r.get("processing_location"),
                    "harvester_name": r.get("harvester_name") or r.get("performed_by"),
                    "note": r.get("note"),
                    "raw_text": r.get("stt_text") or voice_map.get(r["activity_id"], ("", ""))[1],
                    "audio_url": r.get("audio_url") or voice_map.get(r["activity_id"], ("", ""))[0],
                    "photo_url": photo_map.get(r["activity_id"], ""),
                    "created_at": r["created_at"].isoformat() if r.get("created_at") else "",
                }
                tables_data["harvest_records"].append(item_h)
                tables_data["b1_8_harvests"].append(item_h)
                tables_data["harvests"].append(item_h)
                tables_data["b1_8_harvest_sales"].append(item_h)

            # 5. BẢNG BÁN SẢN PHẨM (sales_records JOIN harvest_records JOIN cultivation_activities) - Bảng 1.9
            cur.execute(
                """
                SELECT 
                    sr.sale_id,
                    sr.harvest_id,
                    sr.sale_date,
                    sr.buyer_name,
                    sr.buyer_contact,
                    sr.quantity,
                    sr.unit,
                    sr.unit_price,
                    sr.total_amount,
                    sr.note,
                    sr.created_at,
                    cs.crop_name
                FROM sales_records sr
                JOIN harvest_records hr ON sr.harvest_id = hr.harvest_id
                JOIN crop_seasons cs ON hr.season_id = cs.season_id
                ORDER BY sr.sale_date DESC, sr.created_at DESC
                LIMIT %s;
                """,
                (limit,),
            )
            for r in cur.fetchall():
                s_qty = float(r["quantity"]) if r["quantity"] is not None else None
                sq_fmt = f"{int(s_qty) if s_qty and s_qty.is_integer() else s_qty} {r.get('unit') or 'kg'}".strip()
                sale_date_str = str(r["sale_date"]) if r["sale_date"] else ""
                item_s = {
                    "id": r["sale_id"],
                    "sale_id": r["sale_id"],
                    "harvest_id": r["harvest_id"],
                    "table": "sales_records",
                    "ngay_thuc_hien": sale_date_str,
                    "sale_date": sale_date_str,
                    "thoi_gian_xuat_ban": sale_date_str,
                    "activity_time": "09:00:00",
                    "growth_stage": "Thu hoạch",
                    "giai_doan": "Thu hoạch",
                    "loai_nong_san": r.get("crop_name"),
                    "crop_name": r.get("crop_name"),
                    "co_so_thu_mua": r.get("buyer_name"),
                    "buyer_name": r.get("buyer_name"),
                    "buyer_contact": r.get("buyer_contact"),
                    "khoi_luong_tieu_thu": sq_fmt,
                    "quantity": s_qty,
                    "unit": r.get("unit") or "kg",
                    "unit_price": float(r["unit_price"]) if r.get("unit_price") is not None else None,
                    "total_amount": float(r["total_amount"]) if r.get("total_amount") is not None else None,
                    "note": r.get("note"),
                    "created_at": r["created_at"].isoformat() if r.get("created_at") else "",
                }
                tables_data["sales_records"].append(item_s)
                tables_data["b1_9_sales"].append(item_s)
                tables_data["sales"].append(item_s)
                tables_data["b1_8_harvest_sales"].append(item_s)

            # 6. BẢNG XỬ LÝ CHẤT THẢI / BAO BÌ (waste_disposals) - Bảng 1.7
            cur.execute(
                """
                SELECT 
                    wd.disposal_id,
                    wd.disposal_date,
                    wd.waste_type,
                    wd.quantity,
                    wd.unit,
                    wd.storage_location,
                    wd.treatment_method,
                    wd.note,
                    wd.created_at,
                    cs.season_name
                FROM waste_disposals wd
                JOIN crop_seasons cs ON wd.season_id = cs.season_id
                ORDER BY wd.disposal_date DESC, wd.created_at DESC
                LIMIT %s;
                """,
                (limit,),
            )
            for r in cur.fetchall():
                w_qty = float(r["quantity"]) if r["quantity"] is not None else None
                disp_date_str = str(r["disposal_date"]) if r["disposal_date"] else ""
                w_stage = infer_pomelo_stage_from_date(disp_date_str)
                clean_w_note, w_photo_url, w_audio_url = _extract_media_from_note(r.get("note"))
                item_w = {
                    "id": r["disposal_id"],
                    "disposal_id": r["disposal_id"],
                    "table": "waste_disposals",
                    "ngay_thuc_hien": disp_date_str,
                    "treatment_date": disp_date_str,
                    "disposal_date": disp_date_str,
                    "activity_time": "10:00:00",
                    "growth_stage": w_stage,
                    "giai_doan": w_stage,
                    "loai_bao_bi_thuoc_du": r.get("waste_type"),
                    "waste_type": r.get("waste_type"),
                    "quantity": w_qty,
                    "unit": r.get("unit"),
                    "noi_ton_tru": r.get("storage_location"),
                    "storage_location": r.get("storage_location"),
                    "cach_xu_ly": r.get("treatment_method"),
                    "treatment_method": r.get("treatment_method"),
                    "note": clean_w_note,
                    "photo_url": w_photo_url,
                    "audio_url": w_audio_url,
                    "created_at": r["created_at"].isoformat() if r.get("created_at") else "",
                }
                tables_data["waste_disposals"].append(item_w)
                tables_data["b1_7_waste_treatments"].append(item_w)
                tables_data["waste_treatments"].append(item_w)

            # 7. BẢNG HOẠT ĐỘNG CANH TÁC KHÁC, QUAN SÁT SÂU BỆNH & TƯỚI TIÊU (cultivation_activities)
            cur.execute(
                """
                SELECT 
                    ca.activity_id,
                    ca.activity_type,
                    ca.activity_date,
                    ca.activity_time,
                    ca.growth_stage,
                    ca.description,
                    ca.weather_condition,
                    ca.performed_by,
                    ca.created_at,
                    pl.plot_code,
                    pl.name AS plot_name,
                    po.pest_name,
                    po.severity AS pest_severity,
                    po.action_taken AS pest_action,
                    ir.method AS irrigation_method,
                    ir.duration_minutes,
                    vr.audio_url,
                    vr.stt_text
                FROM cultivation_activities ca
                LEFT JOIN plots pl ON ca.plot_id = pl.plot_id
                LEFT JOIN pest_observations po ON ca.activity_id = po.activity_id
                LEFT JOIN irrigation_records ir ON ca.activity_id = ir.activity_id
                LEFT JOIN activity_voice_records vr ON ca.activity_id = vr.activity_id
                WHERE ca.activity_type IN ('OTHER', 'PEST_OBSERVATION', 'IRRIGATION')
                ORDER BY ca.activity_date DESC, ca.activity_time DESC, ca.created_at DESC
                LIMIT %s;
                """,
                (limit,),
            )
            for r in cur.fetchall():
                stt_t = (r.get("stt_text") or "").lower()
                p_display = r.get("pest_name") or "Không phát hiện"
                if p_display == "Không phát hiện" and ("rầy" in stt_t or "sâu" in stt_t or "nấm" in stt_t or "bệnh" in stt_t):
                    m_p = re.search(r"(rầy\s+\w+|sâu\s+\w+|bệnh\s+\w+|nấm\s+\w+)", stt_t)
                    if m_p and not any(k in stt_t for k in ["không có", "không phát hiện", "sạch"]):
                        p_display = m_p.group(1).title()
                if r.get("pest_severity") and p_display != "Không phát hiện":
                    p_display = f"{p_display} (Mức độ: {r['pest_severity']})"

                raw_desc = r.get("description") or ""
                clean_desc = raw_desc
                qty_val = ""
                m_qty = re.search(r"\((?:Số lượng(?:/liều lượng)?|Liều lượng):\s*([^)]+)\)", raw_desc, re.IGNORECASE)
                if m_qty:
                    qty_val = m_qty.group(1).strip()
                    clean_desc = re.sub(r"\s*\((?:Số lượng(?:/liều lượng)?|Liều lượng):\s*[^)]+\)", "", raw_desc, flags=re.IGNORECASE).strip()
                elif r.get("duration_minutes"):
                    qty_val = f"{r['duration_minutes']} phút"

                act_date_str = str(r["activity_date"]) if r["activity_date"] else ""
                c_stage = infer_pomelo_stage_from_date(act_date_str, r.get("growth_stage"))
                item_cd = {
                    "id": r["activity_id"],
                    "activity_id": r["activity_id"],
                    "activity_type": r.get("activity_type"),
                    "table": "cultivation_activities",
                    "ngay_thuc_hien": act_date_str,
                    "activity_date": act_date_str,
                    "activity_time": str(r["activity_time"]) if r["activity_time"] else "",
                    "cong_viec": clean_desc,
                    "task_name": clean_desc,
                    "description": clean_desc,
                    "raw_description": raw_desc,
                    "so_luong": qty_val,
                    "so_luong_lieu_luong": qty_val,
                    "quantity": qty_val,
                    "sinh_vat_gay_hai": p_display,
                    "target_pest": p_display,
                    "growth_stage": c_stage,
                    "giai_doan": c_stage,
                    "plot_code": r.get("plot_code"),
                    "plot_name": r.get("plot_name"),
                    "weather_condition": r.get("weather_condition"),
                    "performed_by": r.get("performed_by"),
                    "raw_text": r.get("stt_text") or voice_map.get(r["activity_id"], ("", ""))[1],
                    "audio_url": r.get("audio_url") or voice_map.get(r["activity_id"], ("", ""))[0],
                    "photo_url": photo_map.get(r["activity_id"], ""),
                    "created_at": r["created_at"].isoformat() if r.get("created_at") else "",
                }
                tables_data["cultivation_activities"].append(item_cd)
                tables_data["cultivation_diaries"].append(item_cd)

            # 8. BẢNG THEO DÕI SÂU BỆNH (pest_observations)
            cur.execute(
                """
                SELECT 
                    po.observation_id,
                    po.activity_id,
                    po.pest_name,
                    po.pest_type,
                    po.severity,
                    po.affected_area_percent,
                    po.action_taken,
                    po.note,
                    ca.activity_date,
                    ca.activity_time,
                    ca.growth_stage,
                    ca.created_at
                FROM pest_observations po
                JOIN cultivation_activities ca ON po.activity_id = ca.activity_id
                ORDER BY ca.activity_date DESC, ca.created_at DESC
                LIMIT %s;
                """,
                (limit,),
            )
            for r in cur.fetchall():
                act_date_str = str(r["activity_date"]) if r["activity_date"] else ""
                po_stage = infer_pomelo_stage_from_date(act_date_str, r.get("growth_stage"))
                item_po = {
                    "id": r["observation_id"],
                    "observation_id": r["observation_id"],
                    "activity_id": r["activity_id"],
                    "table": "pest_observations",
                    "ngay_thuc_hien": act_date_str,
                    "activity_date": act_date_str,
                    "activity_time": str(r["activity_time"]) if r["activity_time"] else "",
                    "growth_stage": po_stage,
                    "giai_doan": po_stage,
                    "pest_name": r.get("pest_name"),
                    "pest_type": r.get("pest_type"),
                    "severity": r.get("severity"),
                    "action_taken": r.get("action_taken"),
                    "note": r.get("note"),
                    "raw_text": voice_map.get(r["activity_id"], ("", ""))[1],
                    "audio_url": voice_map.get(r["activity_id"], ("", ""))[0],
                    "photo_url": photo_map.get(r["activity_id"], ""),
                    "created_at": r["created_at"].isoformat() if r.get("created_at") else "",
                }
                tables_data["pest_observations"].append(item_po)
                tables_data["diary_pests"].append(item_po)

            # 9. BẢNG TƯỚI NƯỚC (irrigation_records)
            cur.execute(
                """
                SELECT 
                    ir.irrigation_id,
                    ir.activity_id,
                    ir.water_source,
                    ir.method,
                    ir.duration_minutes,
                    ir.quantity,
                    ir.note,
                    ca.activity_date,
                    ca.activity_time,
                    ca.growth_stage,
                    ca.created_at
                FROM irrigation_records ir
                JOIN cultivation_activities ca ON ir.activity_id = ca.activity_id
                ORDER BY ca.activity_date DESC, ca.created_at DESC
                LIMIT %s;
                """,
                (limit,),
            )
            for r in cur.fetchall():
                act_date_str = str(r["activity_date"]) if r["activity_date"] else ""
                ir_stage = infer_pomelo_stage_from_date(act_date_str, r.get("growth_stage"))
                item_ir = {
                    "id": r["irrigation_id"],
                    "irrigation_id": r["irrigation_id"],
                    "activity_id": r["activity_id"],
                    "table": "irrigation_records",
                    "ngay_thuc_hien": act_date_str,
                    "activity_date": act_date_str,
                    "activity_time": str(r["activity_time"]) if r["activity_time"] else "",
                    "growth_stage": ir_stage,
                    "giai_doan": ir_stage,
                    "water_source": r.get("water_source"),
                    "method": r.get("method"),
                    "duration_minutes": r.get("duration_minutes"),
                    "quantity": float(r["quantity"]) if r.get("quantity") is not None else None,
                    "note": r.get("note"),
                    "raw_text": voice_map.get(r["activity_id"], ("", ""))[1],
                    "audio_url": voice_map.get(r["activity_id"], ("", ""))[0],
                    "photo_url": photo_map.get(r["activity_id"], ""),
                    "created_at": r["created_at"].isoformat() if r.get("created_at") else "",
                }
                tables_data["irrigation_records"].append(item_ir)

        conn.close()
    except Exception as e:
        logger.exception("Lỗi khi đọc dữ liệu từ CSDL vietgap_diary: %s", e)

    return tables_data


def get_diary_records(farmer_id: Optional[Any] = None, limit: int = 100) -> List[Dict[str, Any]]:
    """Tạo danh sách kết hợp phẳng từ các bảng để hiển thị Timeline hoạt động."""
    tables = get_diary_by_tables(farmer_id=farmer_id, limit=limit)
    core_tables = [
        "b1_5_material_purchases",
        "b1_6_material_usages",
        "b1_7_waste_treatments",
        "b1_8_harvests",
        "b1_9_sales",
        "cultivation_activities",
    ]
    combined = []
    seen_ids = set()
    for tbl in core_tables:
        for row in tables.get(tbl, []):
            unique_key = f"{row.get('table') or tbl}_{row.get('id')}"
            if unique_key not in seen_ids:
                seen_ids.add(unique_key)
                combined.append(row)

    combined.sort(
        key=lambda x: (
            x.get("ngay_thuc_hien") or "",
            x.get("activity_time") or "",
            x.get("created_at") or "",
        ),
        reverse=True,
    )
    return combined


def save_diary_to_mysql(
    farmer_id: Optional[Any],
    data: Dict[str, Any],
    raw_text: str = "",
    audio_url: str = "",
    photo_url: str = "",
    time_process: float = 0.0,
    llm_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Lưu dữ liệu nhật ký trích xuất vào CSDL vietgap_diary:
    - Bảng gốc: cultivation_activities, activity_voice_records, activity_photos
    - Bảng chi tiết: pesticide_applications, fertilizer_applications, pest_observations, irrigation_records, harvest_records
    - Bảng độc lập: input_purchases, sales_records, waste_disposals
    - Bảng AI: ai_extraction_records
    """
    conn = get_db_connection()
    try:
        final_raw_text = raw_text or data.get("raw_text") or ""
        final_audio_url = audio_url or data.get("audio_url") or ""
        final_photo_url = photo_url or data.get("photo_url") or ""
        if final_photo_url and str(final_photo_url).startswith("/root/"):
            if "/uploads/" in str(final_photo_url):
                final_photo_url = f"/uploads/{os.path.basename(final_photo_url)}"
            else:
                final_photo_url = f"/static/photos/{os.path.basename(final_photo_url)}"
        if final_audio_url and str(final_audio_url).startswith("/root/"):
            final_audio_url = f"/static/audio/{os.path.basename(final_audio_url)}"

        # Lấy season_id hợp lệ
        season_id = get_default_season_id(conn, farmer_id)

        # Trích xuất tables hoặc fallback phẳng
        tables_submitted = data.get("tables") if isinstance(data.get("tables"), dict) else {}
        task_types = data.get("task_types") or []
        if isinstance(task_types, str):
            task_types = [task_types]
        elif not isinstance(task_types, list):
            task_types = []

        # Hợp nhất dữ liệu phẳng nếu cần
        flat_data = dict(data)
        if tables_submitted:
            for _, t_fields in tables_submitted.items():
                if isinstance(t_fields, dict):
                    for fk, fv in t_fields.items():
                        lk = str(fk).lower()
                        if (lk not in flat_data or flat_data[lk] is None or flat_data[lk] == "") and fv is not None:
                            flat_data[lk] = fv

        # Xác định user_id hợp lệ trong bảng users (tránh lỗi khóa ngoại 1452)
        valid_user_id = None
        with conn.cursor() as cur:
            if farmer_id:
                try:
                    cur.execute("SELECT user_id FROM users WHERE user_id = %s LIMIT 1;", (farmer_id,))
                    u_chk = cur.fetchone()
                    if u_chk:
                        valid_user_id = u_chk["user_id"]
                except Exception:
                    pass

            if not valid_user_id:
                cur.execute("SELECT user_id FROM users ORDER BY user_id ASC LIMIT 1;")
                u_def = cur.fetchone()
                if u_def:
                    valid_user_id = u_def["user_id"]
                else:
                    valid_user_id = None  # created_by cho phép NULL theo schema

            user_id = valid_user_id

            created_records: Dict[str, Any] = {}
            primary_activity_id: Optional[int] = None
            primary_voice_id: Optional[int] = None

            # ----------------------------------------------------
            # 1. MUA VẬT TƯ (input_purchases) - Bảng 1.5
            # ----------------------------------------------------
            p_dict = (
                tables_submitted.get("purchase")
                or tables_submitted.get("input_purchases")
                or tables_submitted.get("b1_5_material_purchases")
                or (flat_data if ("purchase" in task_types or flat_data.get("table") in ("purchase", "input_purchases", "b1_5_material_purchases") or (flat_data.get("material_name") and flat_data.get("purchase_date"))) else None)
            )
            if p_dict and isinstance(p_dict, dict) and (p_dict.get("material_name") or p_dict.get("ten_vat_tu") or p_dict.get("invoice_items") or p_dict.get("quantity") or "purchase" in task_types):
                p_date = _parse_date(p_dict.get("purchase_date") or p_dict.get("ngay_thuc_hien") or p_dict.get("activity_date")) or date.today().isoformat()
                p_inv = p_dict.get("invoice_no") or p_dict.get("hoa_don")
                sup_id = _ensure_supplier(cur, p_dict.get("supplier_name") or p_dict.get("noi_cung_cap"))

                base_note = p_dict.get("note") or p_dict.get("ghi_chu")
                if final_photo_url and "Ảnh" not in str(base_note or ""):
                    base_note = f"{base_note}; Ảnh chứng từ: {final_photo_url}" if base_note else f"Ảnh chứng từ: {final_photo_url}"
                if final_audio_url and "Ghi âm" not in str(base_note or ""):
                    base_note = f"{base_note}; Ghi âm: {final_audio_url}" if base_note else f"Ghi âm: {final_audio_url}"

                invoice_items_list = p_dict.get("invoice_items") or flat_data.get("invoice_items")
                if isinstance(invoice_items_list, list) and len(invoice_items_list) > 0:
                    inserted_purchase_ids: List[int] = []
                    for it_row in invoice_items_list:
                        if not isinstance(it_row, dict):
                            continue
                        it_name = str(it_row.get("material_name") or it_row.get("ten_vat_tu") or "").strip()
                        if not it_name:
                            continue
                        it_date = _parse_date(it_row.get("purchase_date")) or p_date
                        it_qty = _parse_number(it_row.get("quantity")) or 1.0
                        it_unit = it_row.get("unit") or "đơn vị"
                        it_price = _parse_number(it_row.get("unit_price") or it_row.get("amount"))
                        it_exp = _parse_date(it_row.get("expiry_date")) or _parse_date(p_dict.get("expiry_date"))
                        it_inv = it_row.get("invoice_no") or p_inv
                        it_type_raw = str(it_row.get("material_type") or "").upper()

                        it_note_parts = []
                        if it_row.get("active_ingredient"):
                            it_note_parts.append(f"Hoạt chất/TP: {it_row.get('active_ingredient')}")
                        if it_row.get("amount"):
                            it_note_parts.append(f"Thành tiền: {it_row.get('amount')}")
                        if base_note:
                            it_note_parts.append(str(base_note))
                        it_note = "; ".join(it_note_parts) if it_note_parts else None

                        item_type = "MATERIAL"
                        p_pest_id = it_row.get("pesticide_id")
                        p_fert_id = it_row.get("fertilizer_id")
                        p_mat_id = None

                        if p_pest_id or "PEST" in it_type_raw or "THUỐC" in it_type_raw or "BVTV" in it_type_raw or any(k in it_name.lower() for k in ["thuốc", "bvtv", "trừ sâu", "trừ rầy", "trừ nấm", "trừ bệnh"]):
                            item_type = "PESTICIDE"
                            p_pest_id = p_pest_id or _ensure_pesticide(cur, it_name)
                        elif p_fert_id or "FERT" in it_type_raw or "PHÂN" in it_type_raw or any(k in it_name.lower() for k in ["phân", "npk", "đạm", "lân", "kali", "hữu cơ", "dap", "vi lượng", "canxi"]):
                            item_type = "FERTILIZER"
                            p_fert_id = p_fert_id or _ensure_fertilizer(cur, it_name)
                        elif "SEED" in it_type_raw or "GIỐNG" in it_type_raw or any(k in it_name.lower() for k in ["giống", "cây con", "hạt", "bầu"]):
                            item_type = "MATERIAL"
                            p_mat_id = _ensure_material(cur, it_name, material_type="SEED", unit=it_unit)
                        else:
                            p_mat_id = _ensure_material(cur, it_name, material_type="MATERIAL", unit=it_unit)

                        cur.execute(
                            """
                            INSERT INTO input_purchases (
                                season_id, supplier_id, item_type, pesticide_id, fertilizer_id, material_id,
                                purchase_date, quantity, unit, unit_price, expiry_date, invoice_no, note
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                            """,
                            (season_id, sup_id, item_type, p_pest_id, p_fert_id, p_mat_id, it_date, it_qty, it_unit, it_price, it_exp, it_inv, it_note),
                        )
                        inserted_purchase_ids.append(cur.lastrowid)

                    if inserted_purchase_ids:
                        created_records["input_purchases"] = inserted_purchase_ids[0]
                        created_records["input_purchases_ids"] = inserted_purchase_ids
                else:
                    m_name = p_dict.get("material_name") or p_dict.get("ten_vat_tu") or "Vật tư nông nghiệp"
                    p_qty = _parse_number(p_dict.get("quantity") or p_dict.get("so_luong_vat_tu")) or 1.0
                    p_unit = p_dict.get("unit") or "kg"
                    p_price = _parse_number(p_dict.get("unit_price") or p_dict.get("don_gia"))
                    p_exp = _parse_date(p_dict.get("expiry_date") or p_dict.get("han_su_dung"))
                    p_note = base_note

                    m_type = str(p_dict.get("material_type") or "").upper()

                    item_type = "MATERIAL"
                    p_pest_id = None
                    p_fert_id = None
                    p_mat_id = None

                    if "PEST" in m_type or "THUỐC" in m_type or "BVTV" in m_type or any(k in m_name.lower() for k in ["thuốc", "bvtv", "trừ sâu", "trừ rầy", "trừ nấm"]):
                        item_type = "PESTICIDE"
                        p_pest_id = _ensure_pesticide(cur, m_name)
                    elif "FERT" in m_type or "PHÂN" in m_type or any(k in m_name.lower() for k in ["phân", "npk", "đạm", "lân", "kali", "hữu cơ", "dap"]):
                        item_type = "FERTILIZER"
                        p_fert_id = _ensure_fertilizer(cur, m_name)
                    elif "SEED" in m_type or "GIỐNG" in m_type or any(k in m_name.lower() for k in ["giống", "cây con", "hạt", "bầu"]):
                        item_type = "MATERIAL"
                        p_mat_id = _ensure_material(cur, m_name, material_type="SEED", unit=p_unit)
                    else:
                        p_mat_id = _ensure_material(cur, m_name, material_type="MATERIAL", unit=p_unit)

                    cur.execute(
                        """
                        INSERT INTO input_purchases (
                            season_id, supplier_id, item_type, pesticide_id, fertilizer_id, material_id,
                            purchase_date, quantity, unit, unit_price, expiry_date, invoice_no, note
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                        """,
                        (season_id, sup_id, item_type, p_pest_id, p_fert_id, p_mat_id, p_date, p_qty, p_unit, p_price, p_exp, p_inv, p_note),
                    )
                    purchase_id = cur.lastrowid
                    created_records["input_purchases"] = purchase_id

            # ----------------------------------------------------
            # 2. PHUN THUỐC BVTV (pesticide_applications)
            # ----------------------------------------------------
            spray_dict = (
                tables_submitted.get("spray")
                or tables_submitted.get("pesticide_applications")
                or (flat_data if ("spray" in task_types or flat_data.get("ten_thuoc_bvtv") or flat_data.get("pesticide_name")) else None)
            )
            if spray_dict and isinstance(spray_dict, dict) and (spray_dict.get("ten_thuoc_bvtv") or spray_dict.get("pesticide_name") or "spray" in task_types):
                s_date = _parse_date(spray_dict.get("activity_date") or spray_dict.get("ngay_thuc_hien")) or date.today().isoformat()
                s_time = _parse_time(spray_dict.get("activity_time")) or datetime.now().strftime("%H:%M:%S")
                raw_gs = spray_dict.get("growth_stage") or flat_data.get("growth_stage")
                g_stage = detect_growth_stage_from_text(raw_gs) or raw_gs
                desc = spray_dict.get("cong_viec") or spray_dict.get("description") or f"Phun thuốc {spray_dict.get('ten_thuoc_bvtv', '')}".strip()
                p_name = spray_dict.get("ten_thuoc_bvtv") or spray_dict.get("pesticide_name") or "Thuốc BVTV"
                sup_id = _ensure_supplier(cur, spray_dict.get("supplier_name"))

                c_val = _parse_number(spray_dict.get("concentration_value"))
                c_unit = spray_dict.get("concentration_unit") or "%"
                d_qty = _parse_number(spray_dict.get("dose_quantity") or spray_dict.get("nong_do_va_luong_dung"))
                d_unit = spray_dict.get("dose_unit") or ("gói" if "gói" in str(spray_dict.get("nong_do_va_luong_dung", "")).lower() else "chai")

                app_method = spray_dict.get("application_method") or spray_dict.get("phuong_thuc_phun") or spray_dict.get("phuong_phap_bon") or "Phun sương đều"
                t_pest = spray_dict.get("target_pest") or spray_dict.get("sinh_vat_gay_hai") or "Rầy sâu bệnh hại"
                iso_days = int(_parse_number(spray_dict.get("isolation_days") or spray_dict.get("thoi_gian_cach_ly")) or 7)

                try:
                    s_dt = datetime.strptime(s_date, "%Y-%m-%d").date()
                    safe_date = (s_dt + timedelta(days=iso_days)).isoformat()
                except Exception:
                    safe_date = None

                p_id = _ensure_pesticide(cur, p_name, target_pest=t_pest, min_iso=iso_days)
                s_weather = spray_dict.get("weather_condition") or spray_dict.get("thoi_tiet")
                s_note = spray_dict.get("note") or ""
                if spray_dict.get("dien_tich"):
                    s_note = f"Diện tích: {spray_dict.get('dien_tich')}; {s_note}".strip("; ")

                cur.execute(
                    """
                    INSERT INTO cultivation_activities (
                        season_id, activity_type, activity_date, activity_time, growth_stage,
                        description, weather_condition, performed_by, created_by, source, is_confirmed
                    ) VALUES (%s, 'PESTICIDE', %s, %s, %s, %s, %s, %s, %s, 'VOICE_AI', TRUE);
                    """,
                    (season_id, s_date, s_time, g_stage, desc, s_weather, spray_dict.get("performed_by") or "Nông hộ", user_id),
                )
                act_id = cur.lastrowid
                if not primary_activity_id:
                    primary_activity_id = act_id

                if final_raw_text or final_audio_url:
                    cur.execute(
                        "INSERT INTO activity_voice_records (activity_id, audio_url, stt_text) VALUES (%s, %s, %s);",
                        (act_id, final_audio_url, final_raw_text),
                    )
                    if not primary_voice_id:
                        primary_voice_id = cur.lastrowid

                if final_photo_url:
                    cur.execute(
                        "INSERT INTO activity_photos (activity_id, photo_url, taken_at) VALUES (%s, %s, NOW());",
                        (act_id, final_photo_url),
                    )

                cur.execute(
                    """
                    INSERT INTO pesticide_applications (
                        activity_id, pesticide_id, supplier_id, concentration_value, concentration_unit,
                        dose_quantity, dose_unit, application_method, target_pest, isolation_days, safe_harvest_date, note
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                    """,
                    (act_id, p_id, sup_id, c_val, c_unit, d_qty, d_unit, app_method, t_pest, iso_days, safe_date, s_note or None),
                )
                created_records["pesticide_applications"] = cur.lastrowid

            # ----------------------------------------------------
            # 3. BÓN PHÂN (fertilizer_applications)
            # ----------------------------------------------------
            fert_dict = (
                tables_submitted.get("fertilize")
                or tables_submitted.get("fertilizer_applications")
                or (flat_data if ("fertilize" in task_types or flat_data.get("ten_phan_bon") or flat_data.get("fertilizer_name")) else None)
            )
            if fert_dict and isinstance(fert_dict, dict) and (fert_dict.get("ten_phan_bon") or fert_dict.get("fertilizer_name") or "fertilize" in task_types):
                f_date = _parse_date(fert_dict.get("activity_date") or fert_dict.get("ngay_thuc_hien")) or date.today().isoformat()
                f_time = _parse_time(fert_dict.get("activity_time")) or datetime.now().strftime("%H:%M:%S")
                raw_gs = fert_dict.get("growth_stage") or fert_dict.get("thoi_diem_bon") or flat_data.get("growth_stage")
                g_stage = detect_growth_stage_from_text(raw_gs) or raw_gs
                f_name = fert_dict.get("ten_phan_bon") or fert_dict.get("fertilizer_name") or "Phân bón"
                desc = fert_dict.get("cong_viec") or fert_dict.get("description") or f"Bón phân {f_name}".strip()
                sup_id = _ensure_supplier(cur, fert_dict.get("supplier_name"))

                f_qty = _parse_number(fert_dict.get("quantity") or fert_dict.get("luong_phan_bon")) or 50.0
                f_unit = fert_dict.get("unit") or ("bao" if "bao" in str(fert_dict.get("luong_phan_bon", "")).lower() else "kg")
                app_method = fert_dict.get("phuong_phap_bon") or fert_dict.get("application_method") or "Bón gốc"
                f_weather = fert_dict.get("weather_condition") or fert_dict.get("thoi_tiet")
                f_note = fert_dict.get("note") or ""
                if fert_dict.get("dien_tich"):
                    f_note = f"Diện tích: {fert_dict.get('dien_tich')}; {f_note}".strip("; ")

                fert_id = _ensure_fertilizer(cur, f_name)

                cur.execute(
                    """
                    INSERT INTO cultivation_activities (
                        season_id, activity_type, activity_date, activity_time, growth_stage,
                        description, weather_condition, performed_by, created_by, source, is_confirmed
                    ) VALUES (%s, 'FERTILIZER', %s, %s, %s, %s, %s, %s, %s, 'VOICE_AI', TRUE);
                    """,
                    (season_id, f_date, f_time, g_stage, desc, f_weather, fert_dict.get("performed_by") or "Nông hộ", user_id),
                )
                act_id = cur.lastrowid
                if not primary_activity_id:
                    primary_activity_id = act_id

                if final_raw_text or final_audio_url:
                    cur.execute(
                        "INSERT INTO activity_voice_records (activity_id, audio_url, stt_text) VALUES (%s, %s, %s);",
                        (act_id, final_audio_url, final_raw_text),
                    )
                    if not primary_voice_id:
                        primary_voice_id = cur.lastrowid

                if final_photo_url:
                    cur.execute(
                        "INSERT INTO activity_photos (activity_id, photo_url, taken_at) VALUES (%s, %s, NOW());",
                        (act_id, final_photo_url),
                    )

                cur.execute(
                    """
                    INSERT INTO fertilizer_applications (
                        activity_id, fertilizer_id, supplier_id, quantity, unit, application_method, note
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s);
                    """,
                    (act_id, fert_id, sup_id, f_qty, f_unit, app_method, f_note or None),
                )
                created_records["fertilizer_applications"] = cur.lastrowid

            # ----------------------------------------------------
            # 4. THU HOẠCH (harvest_records) - Bảng 1.8
            # ----------------------------------------------------
            harv_dict = (
                tables_submitted.get("harvest")
                or tables_submitted.get("harvest_records")
                or tables_submitted.get("b1_8_harvests")
                or (flat_data if ("harvest" in task_types or flat_data.get("san_luong_thu_hoach") or flat_data.get("harvest_date")) else None)
            )
            created_harvest_id: Optional[int] = None
            if harv_dict and isinstance(harv_dict, dict) and (harv_dict.get("san_luong_thu_hoach") or harv_dict.get("quantity") or "harvest" in task_types):
                h_date = _parse_date(harv_dict.get("harvest_date") or harv_dict.get("activity_date") or harv_dict.get("ngay_thuc_hien")) or date.today().isoformat()
                h_time = _parse_time(harv_dict.get("activity_time")) or datetime.now().strftime("%H:%M:%S")
                h_qty = _parse_number(harv_dict.get("san_luong_thu_hoach") or harv_dict.get("quantity")) or 100.0
                h_unit = harv_dict.get("unit") or ("tấn" if "tấn" in str(harv_dict.get("san_luong_thu_hoach", "")).lower() else "kg")
                h_proc = harv_dict.get("dia_diem_cach_so_che") or harv_dict.get("processing_location") or "Sơ chế đóng thùng tại vườn"
                h_name = harv_dict.get("harvester_name") or harv_dict.get("performed_by") or "Nông hộ"
                desc = harv_dict.get("cong_viec") or f"Thu hoạch nông sản {harv_dict.get('crop_name', '')}".strip()

                cur.execute(
                    """
                    INSERT INTO cultivation_activities (
                        season_id, activity_type, activity_date, activity_time, growth_stage,
                        description, weather_condition, performed_by, created_by, source, is_confirmed
                    ) VALUES (%s, 'HARVEST', %s, %s, %s, %s, %s, %s, %s, 'VOICE_AI', TRUE);
                    """,
                    (season_id, h_date, h_time, "Thu hoạch", desc, harv_dict.get("weather_condition"), h_name, user_id),
                )
                act_id = cur.lastrowid
                if not primary_activity_id:
                    primary_activity_id = act_id

                if final_raw_text or final_audio_url:
                    cur.execute(
                        "INSERT INTO activity_voice_records (activity_id, audio_url, stt_text) VALUES (%s, %s, %s);",
                        (act_id, final_audio_url, final_raw_text),
                    )
                    if not primary_voice_id:
                        primary_voice_id = cur.lastrowid

                if final_photo_url:
                    cur.execute(
                        "INSERT INTO activity_photos (activity_id, photo_url, taken_at) VALUES (%s, %s, NOW());",
                        (act_id, final_photo_url),
                    )

                cur.execute(
                    """
                    INSERT INTO harvest_records (
                        activity_id, season_id, quantity, unit, processing_location, harvester_name, note
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s);
                    """,
                    (act_id, season_id, h_qty, h_unit, h_proc, h_name, harv_dict.get("note")),
                )
                created_harvest_id = cur.lastrowid
                created_records["harvest_records"] = created_harvest_id

            # ----------------------------------------------------
            # 5. BÁN SẢN PHẨM (sales_records) - Bảng 1.9
            # ----------------------------------------------------
            sale_dict = (
                tables_submitted.get("sale")
                or tables_submitted.get("sales_records")
                or tables_submitted.get("b1_9_sales")
                or (flat_data if ("sale" in task_types or flat_data.get("khoi_luong_tieu_thu") or flat_data.get("co_so_thu_mua") or flat_data.get("buyer_name")) else None)
            )
            if sale_dict and isinstance(sale_dict, dict) and (sale_dict.get("khoi_luong_tieu_thu") or sale_dict.get("co_so_thu_mua") or sale_dict.get("buyer_name") or "sale" in task_types):
                s_date = _parse_date(sale_dict.get("sale_date") or sale_dict.get("ngay_thuc_hien") or sale_dict.get("thoi_gian_xuat_ban")) or date.today().isoformat()
                b_name = sale_dict.get("co_so_thu_mua") or sale_dict.get("buyer_name") or "Thương lái"
                b_contact = sale_dict.get("buyer_contact")
                s_qty = _parse_number(sale_dict.get("khoi_luong_tieu_thu") or sale_dict.get("quantity")) or 100.0
                s_unit = sale_dict.get("unit") or ("tấn" if "tấn" in str(sale_dict.get("khoi_luong_tieu_thu", "")).lower() else "kg")
                u_price = _parse_number(sale_dict.get("unit_price") or sale_dict.get("don_gia"))
                t_amount = _parse_number(sale_dict.get("total_amount") or sale_dict.get("thanh_tien"))
                if not t_amount and u_price and s_qty:
                    t_amount = u_price * s_qty

                h_id_to_use = created_harvest_id
                if not h_id_to_use:
                    cur.execute("SELECT harvest_id FROM harvest_records WHERE season_id = %s ORDER BY harvest_id DESC LIMIT 1;", (season_id,))
                    h_row = cur.fetchone()
                    if h_row:
                        h_id_to_use = h_row["harvest_id"]
                    else:
                        cur.execute(
                            """
                            INSERT INTO cultivation_activities (season_id, activity_type, activity_date, description, performed_by, created_by)
                            VALUES (%s, 'HARVEST', %s, 'Thu hoạch phục vụ bán', 'Nông hộ', %s);
                            """,
                            (season_id, s_date, user_id),
                        )
                        sub_act = cur.lastrowid
                        cur.execute(
                            """
                            INSERT INTO harvest_records (activity_id, season_id, quantity, unit, harvester_name)
                            VALUES (%s, %s, %s, %s, 'Nông hộ');
                            """,
                            (sub_act, season_id, s_qty, s_unit),
                        )
                        h_id_to_use = cur.lastrowid

                cur.execute(
                    """
                    INSERT INTO sales_records (
                        harvest_id, buyer_name, buyer_contact, sale_date, quantity, unit, unit_price, total_amount, note
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
                    """,
                    (h_id_to_use, b_name, b_contact, s_date, s_qty, s_unit, u_price, t_amount, sale_dict.get("note")),
                )
                created_records["sales_records"] = cur.lastrowid

            # ----------------------------------------------------
            # 6. XỬ LÝ CHẤT THẢI / BAO BÌ (waste_disposals) - Bảng 1.7
            # ----------------------------------------------------
            waste_dict = (
                tables_submitted.get("waste")
                or tables_submitted.get("waste_disposals")
                or tables_submitted.get("b1_7_waste_treatments")
                or (flat_data if ("waste" in task_types or flat_data.get("waste_type") or flat_data.get("loai_bao_bi_thuoc_du")) else None)
            )
            if waste_dict and isinstance(waste_dict, dict) and (waste_dict.get("waste_type") or waste_dict.get("loai_bao_bi_thuoc_du") or waste_dict.get("material_name") or "waste" in task_types):
                w_date = _parse_date(waste_dict.get("treatment_date") or waste_dict.get("disposal_date") or waste_dict.get("ngay_thuc_hien")) or date.today().isoformat()
                w_type = waste_dict.get("waste_type") or waste_dict.get("loai_bao_bi_thuoc_du") or waste_dict.get("material_name") or "Vỏ chai bao bì BVTV"
                w_qty = _parse_number(waste_dict.get("quantity"))
                w_unit = waste_dict.get("unit")
                w_loc = waste_dict.get("storage_location") or waste_dict.get("noi_ton_tru") or "Bể chứa bao bì có nắp đậy"
                w_meth = waste_dict.get("treatment_method") or waste_dict.get("cach_xu_ly") or "Súc rửa 3 lần bàn giao thu gom"
                w_note = waste_dict.get("note") or ""
                if final_photo_url and "Ảnh" not in str(w_note):
                    w_note = f"{w_note}; Ảnh chứng từ: {final_photo_url}".strip("; ")
                if final_audio_url and "Ghi âm" not in str(w_note):
                    w_note = f"{w_note}; Ghi âm: {final_audio_url}".strip("; ")

                cur.execute(
                    """
                    INSERT INTO waste_disposals (
                        season_id, disposal_date, waste_type, quantity, unit, storage_location, treatment_method, note
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                    """,
                    (season_id, w_date, w_type, w_qty, w_unit, w_loc, w_meth, w_note or None),
                )
                created_records["waste_disposals"] = cur.lastrowid

            # ----------------------------------------------------
            # 7. QUAN SÁT / SÂU BỆNH (pest_observations)
            # ----------------------------------------------------
            obs_dict = (
                tables_submitted.get("observation")
                or tables_submitted.get("pest_observations")
                or tables_submitted.get("diary_pests")
                or (flat_data if ("observation" in task_types or flat_data.get("sinh_vat_gay_hai") or flat_data.get("pest_name")) and "spray" not in task_types else None)
            )
            if obs_dict and isinstance(obs_dict, dict) and (obs_dict.get("sinh_vat_gay_hai") or obs_dict.get("pest_name") or obs_dict.get("target_pest") or "observation" in task_types):
                o_date = _parse_date(obs_dict.get("activity_date") or obs_dict.get("ngay_thuc_hien") or obs_dict.get("observation_date")) or date.today().isoformat()
                o_time = _parse_time(obs_dict.get("activity_time")) or datetime.now().strftime("%H:%M:%S")
                p_name = obs_dict.get("sinh_vat_gay_hai") or obs_dict.get("pest_name") or obs_dict.get("target_pest") or "Sinh vật gây hại"
                p_type = obs_dict.get("pest_type") or ("Sâu hại" if any(k in p_name.lower() for k in ["sâu", "rầy", "bọ", "rệp"]) else "Bệnh hại")
                severity = obs_dict.get("muc_do") or obs_dict.get("severity") or "Nhẹ"
                action_taken = obs_dict.get("action_taken") or obs_dict.get("bien_phap_xu_ly") or obs_dict.get("notes") or "Theo dõi tiếp"
                raw_gs = obs_dict.get("growth_stage") or obs_dict.get("giai_doan") or flat_data.get("growth_stage")
                g_stage = detect_growth_stage_from_text(raw_gs) or raw_gs
                desc = obs_dict.get("cong_viec") or obs_dict.get("description") or f"Quan sát phát hiện: {p_name}".strip()

                cur.execute(
                    """
                    INSERT INTO cultivation_activities (
                        season_id, activity_type, activity_date, activity_time, growth_stage,
                        description, weather_condition, performed_by, created_by, source, is_confirmed
                    ) VALUES (%s, 'PEST_OBSERVATION', %s, %s, %s, %s, %s, %s, %s, 'VOICE_AI', TRUE);
                    """,
                    (season_id, o_date, o_time, g_stage, desc, obs_dict.get("weather_condition"), obs_dict.get("performed_by") or "Nông hộ", user_id),
                )
                act_id = cur.lastrowid
                if not primary_activity_id:
                    primary_activity_id = act_id

                if final_raw_text or final_audio_url:
                    cur.execute(
                        "INSERT INTO activity_voice_records (activity_id, audio_url, stt_text) VALUES (%s, %s, %s);",
                        (act_id, final_audio_url, final_raw_text),
                    )
                    if not primary_voice_id:
                        primary_voice_id = cur.lastrowid

                if final_photo_url:
                    cur.execute(
                        "INSERT INTO activity_photos (activity_id, photo_url, taken_at) VALUES (%s, %s, NOW());",
                        (act_id, final_photo_url),
                    )

                cur.execute(
                    """
                    INSERT INTO pest_observations (
                        activity_id, pest_name, pest_type, severity, action_taken, note
                    ) VALUES (%s, %s, %s, %s, %s, %s);
                    """,
                    (act_id, p_name, p_type, severity, action_taken, obs_dict.get("note")),
                )
                created_records["pest_observations"] = cur.lastrowid

            # ----------------------------------------------------
            # 8. HOẠT ĐỘNG CHUNG (cultivation_activities - OTHER)
            # ----------------------------------------------------
            gen_dict = (
                tables_submitted.get("general")
                or tables_submitted.get("cultivation_activities")
                or tables_submitted.get("cultivation_diaries")
                or (flat_data if ("general" in task_types or (not created_records and final_raw_text)) else None)
            )
            if gen_dict and isinstance(gen_dict, dict) and (gen_dict.get("cong_viec") or gen_dict.get("description") or "general" in task_types or not created_records):
                g_date = _parse_date(gen_dict.get("activity_date") or gen_dict.get("ngay_thuc_hien")) or date.today().isoformat()
                g_time = _parse_time(gen_dict.get("activity_time")) or datetime.now().strftime("%H:%M:%S")
                desc = gen_dict.get("cong_viec") or gen_dict.get("task_name") or gen_dict.get("description") or final_raw_text or "Công việc canh tác chăm sóc vườn"
                qty = gen_dict.get("so_luong") or gen_dict.get("so_luong_lieu_luong") or gen_dict.get("quantity")
                if qty and str(qty).strip() and str(qty).strip() not in str(desc):
                    desc = f"{desc} (Số lượng/liều lượng: {str(qty).strip()})"
                raw_gs = gen_dict.get("growth_stage") or gen_dict.get("giai_doan") or flat_data.get("growth_stage") or flat_data.get("giai_doan")
                g_stage = detect_growth_stage_from_text(raw_gs) or raw_gs

                cur.execute(
                    """
                    INSERT INTO cultivation_activities (
                        season_id, activity_type, activity_date, activity_time, growth_stage,
                        description, weather_condition, performed_by, created_by, source, is_confirmed
                    ) VALUES (%s, 'OTHER', %s, %s, %s, %s, %s, %s, %s, 'VOICE_AI', TRUE);
                    """,
                    (season_id, g_date, g_time, g_stage, desc, gen_dict.get("weather_condition"), gen_dict.get("performed_by") or "Nông hộ", user_id),
                )
                act_id = cur.lastrowid
                if not primary_activity_id:
                    primary_activity_id = act_id

                if final_raw_text or final_audio_url:
                    cur.execute(
                        "INSERT INTO activity_voice_records (activity_id, audio_url, stt_text) VALUES (%s, %s, %s);",
                        (act_id, final_audio_url, final_raw_text),
                    )
                    if not primary_voice_id:
                        primary_voice_id = cur.lastrowid

                if final_photo_url:
                    cur.execute(
                        "INSERT INTO activity_photos (activity_id, photo_url, taken_at) VALUES (%s, %s, NOW());",
                        (act_id, final_photo_url),
                    )

                created_records["cultivation_activities"] = act_id

            # ----------------------------------------------------
            # 8. LƯU LỊCH SỬ AI EXTRACTION (ai_extraction_records) - PHƯƠNG ÁN A
            # ----------------------------------------------------
            if final_raw_text or data:
                try:
                    # Đóng gói kết quả AI theo Phương án A:
                    # Bảo tồn dữ liệu thô ban đầu (chưa qua normalizer), dữ liệu chuẩn hóa, và metadata mô hình
                    raw_llm_out = None
                    llm_metadata_dict = None

                    if isinstance(llm_meta, dict) and llm_meta:
                        raw_llm_out = (
                            llm_meta.get("raw_response")
                            or llm_meta.get("raw_llm_output")
                            or llm_meta.get("raw_json")
                        )
                        llm_metadata_dict = {
                            "engine": llm_meta.get("engine"),
                            "model": llm_meta.get("model"),
                            "latency_s": time_process or llm_meta.get("latency_s"),
                            "tokens_per_sec": llm_meta.get("tokens_per_sec"),
                            "prompt_tokens": llm_meta.get("prompt_tokens"),
                            "completion_tokens": llm_meta.get("completion_tokens"),
                        }
                    elif isinstance(data, dict):
                        raw_llm_out = data.get("raw_llm_output") or data.get("raw_response")
                        if isinstance(data.get("llm_meta"), dict):
                            llm_metadata_dict = data.get("llm_meta")

                    # Nếu raw_llm_out là chuỗi JSON, parse sang dict cho sạch sẽ
                    if isinstance(raw_llm_out, str):
                        try:
                            raw_llm_out = json.loads(raw_llm_out)
                        except Exception:
                            pass

                    # Kết hợp payload đầy đủ theo Phương án A:
                    # Giữ nguyên toàn bộ cấu trúc ban đầu của data (tables, task_types...) để tương thích ngược 100%,
                    # đồng thời bổ sung các trường raw_llm_output, normalized_data, llm_metadata.
                    ai_record_payload = dict(data) if isinstance(data, dict) else {"data": data}
                    ai_record_payload["raw_llm_output"] = raw_llm_out
                    ai_record_payload["llm_metadata"] = llm_metadata_dict
                    ai_record_payload["normalized_data"] = {
                        k: v for k, v in ai_record_payload.items()
                        if k not in ("raw_llm_output", "llm_metadata")
                    }

                    cur.execute(
                        """
                        INSERT INTO ai_extraction_records (
                            activity_id, voice_id, raw_transcript, ai_json_result, time_process,
                            extraction_status, reviewed_by, reviewed_at
                        ) VALUES (%s, %s, %s, %s, %s, 'APPLIED', %s, NOW());
                        """,
                        (
                            primary_activity_id,
                            primary_voice_id,
                            final_raw_text or "Nhập trực tiếp từ giao diện",
                            json.dumps(ai_record_payload, ensure_ascii=False),
                            time_process,
                            user_id,
                        ),
                    )
                    created_records["ai_extraction_records"] = cur.lastrowid
                except Exception as e_ai:
                    logger.warning("Không thể lưu ai_extraction_records: %s", e_ai)

            # ----------------------------------------------------
            # 9. TỰ ĐỘNG CẬP NHẬT GIAI ĐOẠN MÙA VỤ DỰA TRÊN LỜI NÓI & BIỂU MẪU
            # ----------------------------------------------------
            detected_stage = (
                flat_data.get("growth_stage")
                or flat_data.get("giai_doan")
                or flat_data.get("giai_doan_sinh_truong")
                or detect_growth_stage_from_text(final_raw_text)
            )
            if detected_stage and str(detected_stage).strip():
                clean_stage = detect_growth_stage_from_text(str(detected_stage)) or str(detected_stage).strip()
                try:
                    cur.execute("UPDATE crop_seasons SET growth_stage = %s WHERE season_id = %s;", (clean_stage, season_id))
                    if user_id:
                        cur.execute("UPDATE farm SET growth_stage = %s WHERE owner_user_id = %s;", (clean_stage, user_id))
                    else:
                        cur.execute("UPDATE farm SET growth_stage = %s ORDER BY farm_id ASC LIMIT 1;", (clean_stage,))
                except Exception as e_gs:
                    logger.warning("Không thể cập nhật growth_stage vào mùa vụ: %s", e_gs)

        if primary_activity_id and "cultivation_activities" not in created_records:
            created_records["cultivation_activities"] = primary_activity_id

        conn.commit()
        conn.close()
        return {
            "id": primary_activity_id or (list(created_records.values())[0] if created_records else 1),
            "activity_id": primary_activity_id,
            "status": "success",
            "message": "Đã lưu vào CSDL vietgap_diary thành công",
            "created_records": created_records,
            "growth_stage": detected_stage,
        }
    except Exception as e:
        logger.exception("Lỗi khi lưu vào vietgap_diary: %s", e)
        conn.close()
        raise e


def delete_diary_record(arg1: Any, arg2: Optional[Any] = None) -> bool:
    """
    Xóa bản ghi nhật ký. Hỗ trợ cả 2 thứ tự tham số:
    - delete_diary_record(record_id, table_name)
    - delete_diary_record(table_name, record_id)
    """
    if isinstance(arg1, str) and not arg1.isdigit() and (arg2 is not None):
        table_name = arg1
        record_id = arg2
    else:
        record_id = arg1
        table_name = arg2

    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            tbl = (table_name or "").lower()

            if tbl in ("input_purchases", "b1_5_material_purchases", "material_purchases"):
                cur.execute("DELETE FROM input_purchases WHERE purchase_id = %s;", (record_id,))
            elif tbl in ("sales_records", "b1_9_sales", "sales"):
                cur.execute("DELETE FROM sales_records WHERE sale_id = %s;", (record_id,))
            elif tbl in ("waste_disposals", "b1_7_waste_treatments", "waste_treatments"):
                cur.execute("DELETE FROM waste_disposals WHERE disposal_id = %s;", (record_id,))
            elif tbl in ("harvest_records", "b1_8_harvests", "harvests"):
                cur.execute("SELECT activity_id FROM harvest_records WHERE harvest_id = %s;", (record_id,))
                hr = cur.fetchone()
                if hr:
                    cur.execute("DELETE FROM cultivation_activities WHERE activity_id = %s;", (hr["activity_id"],))
                else:
                    cur.execute("DELETE FROM harvest_records WHERE harvest_id = %s;", (record_id,))
            elif tbl in ("pesticide_applications", "b1_6_pesticides"):
                cur.execute("SELECT activity_id FROM pesticide_applications WHERE application_id = %s OR activity_id = %s;", (record_id, record_id))
                pr = cur.fetchone()
                if pr:
                    cur.execute("DELETE FROM cultivation_activities WHERE activity_id = %s;", (pr["activity_id"],))
                else:
                    cur.execute("DELETE FROM pesticide_applications WHERE application_id = %s;", (record_id,))
            elif tbl in ("fertilizer_applications", "b1_6_fertilizers"):
                cur.execute("SELECT activity_id FROM fertilizer_applications WHERE application_id = %s OR activity_id = %s;", (record_id, record_id))
                fr = cur.fetchone()
                if fr:
                    cur.execute("DELETE FROM cultivation_activities WHERE activity_id = %s;", (fr["activity_id"],))
                else:
                    cur.execute("DELETE FROM fertilizer_applications WHERE application_id = %s;", (record_id,))
            else:
                cur.execute("DELETE FROM cultivation_activities WHERE activity_id = %s;", (record_id,))
                cur.execute("DELETE FROM input_purchases WHERE purchase_id = %s;", (record_id,))
                cur.execute("DELETE FROM sales_records WHERE sale_id = %s;", (record_id,))
                cur.execute("DELETE FROM waste_disposals WHERE disposal_id = %s;", (record_id,))

        conn.close()
        return True
    except Exception as e:
        logger.exception("Lỗi khi xóa bản ghi: %s", e)
        return False


delete_diary_from_mysql = delete_diary_record


def get_categories(category_type: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Lấy danh mục dữ liệu phục vụ gợi ý tự động (autocomplete) và lựa chọn."""
    res = {
        "pesticides": [],
        "fertilizers": [],
        "materials": [],
        "suppliers": [],
        "plots": [],
        "crop_seasons": []
    }
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            if not category_type or category_type == "pesticides":
                cur.execute("""
                    SELECT pesticide_id, name, active_ingredient, formulation, 
                           registration_no, min_isolation_days, max_concentration, 
                           toxicity_group, allowed_crops, manufacturer, rag_knowledge 
                    FROM pesticides ORDER BY name ASC;
                """)
                p_rows = cur.fetchall()
                for r in p_rows:
                    if isinstance(r.get("rag_knowledge"), str):
                        try:
                            r["rag_knowledge"] = json.loads(r["rag_knowledge"])
                        except Exception:
                            pass
                res["pesticides"] = p_rows

            if not category_type or category_type == "fertilizers":
                cur.execute("""
                    SELECT fertilizer_id, name, fertilizer_type, npk_ratio, 
                           registration_no, manufacturer, rag_knowledge 
                    FROM fertilizers ORDER BY name ASC;
                """)
                f_rows = cur.fetchall()
                for r in f_rows:
                    if isinstance(r.get("rag_knowledge"), str):
                        try:
                            r["rag_knowledge"] = json.loads(r["rag_knowledge"])
                        except Exception:
                            pass
                res["fertilizers"] = f_rows

            if not category_type or category_type == "materials":
                cur.execute("SELECT material_id, name, material_type, unit, description FROM materials ORDER BY name ASC;")
                res["materials"] = cur.fetchall()

            if not category_type or category_type == "suppliers":
                cur.execute("SELECT supplier_id, name, address, phone, supplier_type FROM suppliers ORDER BY name ASC;")
                res["suppliers"] = cur.fetchall()

            if not category_type or category_type == "plots":
                cur.execute("SELECT plot_id, name, plot_code, crop_name, variety, area_size, area_unit, latitude, longitude FROM plots ORDER BY name ASC;")
                res["plots"] = cur.fetchall()

            if not category_type or category_type == "crop_seasons":
                cur.execute("SELECT season_id, season_name, crop_name, variety, start_date, expected_yield FROM crop_seasons ORDER BY created_at DESC;")
                res["crop_seasons"] = cur.fetchall()

        conn.close()
    except Exception as e:
        logger.exception("Lỗi khi lấy danh mục: %s", e)
    return res


def get_plots(farm_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Lấy danh sách các lô canh tác kèm tọa độ GPS."""
    plots = []
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            if farm_id:
                cur.execute(
                    "SELECT plot_id, farm_id, name, plot_code, crop_name, variety, area_size, area_unit, "
                    "latitude, longitude, soil_type, water_source, land_use_history FROM plots "
                    "WHERE farm_id = %s ORDER BY name ASC;",
                    (farm_id,)
                )
            else:
                cur.execute(
                    "SELECT plot_id, farm_id, name, plot_code, crop_name, variety, area_size, area_unit, "
                    "latitude, longitude, soil_type, water_source, land_use_history FROM plots "
                    "ORDER BY name ASC;"
                )
            rows = cur.fetchall()
            plots = [_serialize_row(r) for r in rows]
        conn.close()
    except Exception as e:
        logger.exception("Lỗi lấy danh sách lô canh tác: %s", e)
    return plots


def find_nearest_plot(lat: float, lng: float, farm_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Tìm lô canh tác gần nhất dựa trên tọa độ GPS (sử dụng công thức Haversine)."""
    import math
    plots = get_plots(farm_id)
    if not plots:
        return None

    def haversine_distance(lat1, lon1, lat2, lon2):
        R = 6371000  # mét
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)
        a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c

    best_plot = None
    min_dist = float('inf')
    for p in plots:
        p_lat = p.get("latitude")
        p_lng = p.get("longitude")
        if p_lat is not None and p_lng is not None:
            try:
                dist = haversine_distance(float(lat), float(lng), float(p_lat), float(p_lng))
                p["distance_meters"] = round(dist, 1)
                if dist < min_dist:
                    min_dist = dist
                    best_plot = p
            except (ValueError, TypeError):
                continue
    return best_plot


def update_diary_record(record_id: Any, table_name: str, update_data: Dict[str, Any]) -> bool:
    """Cập nhật thông tin bản ghi nhật ký theo đúng bảng CSDL vietgap_diary."""
    if not record_id or not update_data:
        return False
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            tbl = (table_name or "").lower()

            # 1. BẢNG MUA VẬT TƯ (input_purchases)
            if tbl in ("input_purchases", "b1_5_material_purchases", "material_purchases", "purchase"):
                p_date = _parse_date(update_data.get("purchase_date") or update_data.get("ngay_thuc_hien") or update_data.get("activity_date"))
                qty = _parse_number(update_data.get("quantity") or update_data.get("so_luong_vat_tu"))
                unit = update_data.get("unit")
                price = _parse_number(update_data.get("unit_price"))
                exp = _parse_date(update_data.get("expiry_date") or update_data.get("han_su_dung"))
                inv = update_data.get("invoice_no")
                note = update_data.get("note")

                # Cập nhật supplier nếu có tên
                sup_id = _ensure_supplier(cur, update_data.get("supplier_name") or update_data.get("noi_cung_cap"))

                # Cập nhật material nếu có tên
                mat_name = update_data.get("material_name") or update_data.get("ten_vat_tu")
                mat_id = _ensure_material(cur, mat_name, unit=unit) if mat_name else None

                update_clauses = []
                params = []
                if p_date:
                    update_clauses.append("purchase_date = %s")
                    params.append(p_date)
                if qty is not None:
                    update_clauses.append("quantity = %s")
                    params.append(qty)
                if unit:
                    update_clauses.append("unit = %s")
                    params.append(unit)
                if price is not None:
                    update_clauses.append("unit_price = %s")
                    params.append(price)
                if exp:
                    update_clauses.append("expiry_date = %s")
                    params.append(exp)
                if inv is not None:
                    update_clauses.append("invoice_no = %s")
                    params.append(inv)
                if note is not None:
                    update_clauses.append("note = %s")
                    params.append(note)
                if sup_id:
                    update_clauses.append("supplier_id = %s")
                    params.append(sup_id)
                if mat_id:
                    update_clauses.append("material_id = %s")
                    params.append(mat_id)

                if update_clauses:
                    params.append(record_id)
                    cur.execute(f"UPDATE input_purchases SET {', '.join(update_clauses)} WHERE purchase_id = %s;", tuple(params))

            # 2. BẢNG PHUN THUỐC (pesticide_applications)
            elif tbl in ("pesticide_applications", "b1_6_pesticides", "spray"):
                # Tìm activity_id
                cur.execute("SELECT activity_id, application_id FROM pesticide_applications WHERE application_id = %s OR activity_id = %s;", (record_id, record_id))
                row = cur.fetchone()
                if row:
                    act_id = row["activity_id"]
                    app_id = row["application_id"]

                    act_date = _parse_date(update_data.get("activity_date") or update_data.get("ngay_thuc_hien"))
                    act_time = _parse_time(update_data.get("activity_time"))
                    growth = update_data.get("growth_stage") or update_data.get("giai_doan")
                    if growth:
                        growth = detect_growth_stage_from_text(growth) or growth
                    desc = update_data.get("cong_viec") or update_data.get("description")
                    weather = update_data.get("weather_condition")
                    performer = update_data.get("performed_by")

                    # Cập nhật cultivation_activities
                    act_clauses = []
                    act_params = []
                    if act_date:
                        act_clauses.append("activity_date = %s")
                        act_params.append(act_date)
                    if act_time:
                        act_clauses.append("activity_time = %s")
                        act_params.append(act_time)
                    if growth is not None:
                        act_clauses.append("growth_stage = %s")
                        act_params.append(growth)
                    if desc is not None:
                        act_clauses.append("description = %s")
                        act_params.append(desc)
                    if weather is not None:
                        act_clauses.append("weather_condition = %s")
                        act_params.append(weather)
                    if performer is not None:
                        act_clauses.append("performed_by = %s")
                        act_params.append(performer)

                    if act_clauses:
                        act_params.append(act_id)
                        cur.execute(f"UPDATE cultivation_activities SET {', '.join(act_clauses)} WHERE activity_id = %s;", tuple(act_params))

                    # Cập nhật pesticide_applications
                    p_name = update_data.get("pesticide_name") or update_data.get("ten_thuoc_bvtv")
                    p_id = _ensure_pesticide(cur, p_name) if p_name else None
                    sup_id = _ensure_supplier(cur, update_data.get("supplier_name"))
                    d_qty = _parse_number(update_data.get("dose_quantity") or update_data.get("nong_do_va_luong_dung"))
                    d_unit = update_data.get("dose_unit")
                    c_val = _parse_number(update_data.get("concentration_value"))
                    c_unit = update_data.get("concentration_unit")
                    meth = update_data.get("application_method") or update_data.get("phuong_phap_bon")
                    pest = update_data.get("target_pest") or update_data.get("sinh_vat_gay_hai")
                    iso_days = _parse_number(update_data.get("isolation_days") or update_data.get("thoi_gian_cach_ly"))
                    note = update_data.get("note")

                    app_clauses = []
                    app_params = []
                    if p_id:
                        app_clauses.append("pesticide_id = %s")
                        app_params.append(p_id)
                    if sup_id:
                        app_clauses.append("supplier_id = %s")
                        app_params.append(sup_id)
                    if d_qty is not None:
                        app_clauses.append("dose_quantity = %s")
                        app_params.append(d_qty)
                    if d_unit:
                        app_clauses.append("dose_unit = %s")
                        app_params.append(d_unit)
                    if c_val is not None:
                        app_clauses.append("concentration_value = %s")
                        app_params.append(c_val)
                    if c_unit:
                        app_clauses.append("concentration_unit = %s")
                        app_params.append(c_unit)
                    if meth is not None:
                        app_clauses.append("application_method = %s")
                        app_params.append(meth)
                    if pest is not None:
                        app_clauses.append("target_pest = %s")
                        app_params.append(pest)
                    if iso_days is not None:
                        app_clauses.append("isolation_days = %s")
                        app_params.append(int(iso_days))
                    if note is not None:
                        app_clauses.append("note = %s")
                        app_params.append(note)

                    if app_clauses:
                        app_params.append(app_id)
                        cur.execute(f"UPDATE pesticide_applications SET {', '.join(app_clauses)} WHERE application_id = %s;", tuple(app_params))

            # 3. BẢNG BÓN PHÂN (fertilizer_applications)
            elif tbl in ("fertilizer_applications", "b1_6_fertilizers", "fertilize"):
                cur.execute("SELECT activity_id, application_id FROM fertilizer_applications WHERE application_id = %s OR activity_id = %s;", (record_id, record_id))
                row = cur.fetchone()
                if row:
                    act_id = row["activity_id"]
                    app_id = row["application_id"]

                    act_date = _parse_date(update_data.get("activity_date") or update_data.get("ngay_thuc_hien"))
                    act_time = _parse_time(update_data.get("activity_time"))
                    growth = update_data.get("growth_stage") or update_data.get("giai_doan")
                    if growth:
                        growth = detect_growth_stage_from_text(growth) or growth
                    desc = update_data.get("cong_viec") or update_data.get("description")
                    weather = update_data.get("weather_condition")
                    performer = update_data.get("performed_by")

                    act_clauses = []
                    act_params = []
                    if act_date:
                        act_clauses.append("activity_date = %s")
                        act_params.append(act_date)
                    if act_time:
                        act_clauses.append("activity_time = %s")
                        act_params.append(act_time)
                    if growth is not None:
                        act_clauses.append("growth_stage = %s")
                        act_params.append(growth)
                    if desc is not None:
                        act_clauses.append("description = %s")
                        act_params.append(desc)
                    if weather is not None:
                        act_clauses.append("weather_condition = %s")
                        act_params.append(weather)
                    if performer is not None:
                        act_clauses.append("performed_by = %s")
                        act_params.append(performer)

                    if act_clauses:
                        act_params.append(act_id)
                        cur.execute(f"UPDATE cultivation_activities SET {', '.join(act_clauses)} WHERE activity_id = %s;", tuple(act_params))

                    f_name = update_data.get("fertilizer_name") or update_data.get("ten_phan_bon")
                    f_id = _ensure_fertilizer(cur, f_name) if f_name else None
                    sup_id = _ensure_supplier(cur, update_data.get("supplier_name"))
                    qty = _parse_number(update_data.get("quantity") or update_data.get("luong_phan_bon"))
                    unit = update_data.get("unit")
                    meth = update_data.get("application_method") or update_data.get("phuong_phap_bon")
                    note = update_data.get("note")

                    f_clauses = []
                    f_params = []
                    if f_id:
                        f_clauses.append("fertilizer_id = %s")
                        f_params.append(f_id)
                    if sup_id:
                        f_clauses.append("supplier_id = %s")
                        f_params.append(sup_id)
                    if qty is not None:
                        f_clauses.append("quantity = %s")
                        f_params.append(qty)
                    if unit:
                        f_clauses.append("unit = %s")
                        f_params.append(unit)
                    if meth is not None:
                        f_clauses.append("application_method = %s")
                        f_params.append(meth)
                    if note is not None:
                        f_clauses.append("note = %s")
                        f_params.append(note)

                    if f_clauses:
                        f_params.append(app_id)
                        cur.execute(f"UPDATE fertilizer_applications SET {', '.join(f_clauses)} WHERE application_id = %s;", tuple(f_params))

            # 4. BẢNG THU HOẠCH (harvest_records)
            elif tbl in ("harvest_records", "b1_8_harvests", "harvests", "harvest"):
                cur.execute("SELECT activity_id, harvest_id FROM harvest_records WHERE harvest_id = %s OR activity_id = %s;", (record_id, record_id))
                row = cur.fetchone()
                if row:
                    act_id = row["activity_id"]
                    h_id = row["harvest_id"]

                    h_date = _parse_date(update_data.get("harvest_date") or update_data.get("activity_date") or update_data.get("ngay_thuc_hien"))
                    if h_date:
                        cur.execute("UPDATE cultivation_activities SET activity_date = %s WHERE activity_id = %s;", (h_date, act_id))

                    qty = _parse_number(update_data.get("quantity") or update_data.get("san_luong_thu_hoach"))
                    unit = update_data.get("unit")
                    loc = update_data.get("processing_location") or update_data.get("dia_diem_cach_so_che")
                    harvester = update_data.get("harvester_name") or update_data.get("performed_by")
                    note = update_data.get("note")

                    h_clauses = []
                    h_params = []
                    if qty is not None:
                        h_clauses.append("quantity = %s")
                        h_params.append(qty)
                    if unit:
                        h_clauses.append("unit = %s")
                        h_params.append(unit)
                    if loc is not None:
                        h_clauses.append("processing_location = %s")
                        h_params.append(loc)
                    if harvester is not None:
                        h_clauses.append("harvester_name = %s")
                        h_params.append(harvester)
                    if note is not None:
                        h_clauses.append("note = %s")
                        h_params.append(note)

                    if h_clauses:
                        h_params.append(h_id)
                        cur.execute(f"UPDATE harvest_records SET {', '.join(h_clauses)} WHERE harvest_id = %s;", tuple(h_params))

            # 5. BẢNG BÁN SẢN PHẨM (sales_records)
            elif tbl in ("sales_records", "b1_9_sales", "sales", "sale"):
                s_date = _parse_date(update_data.get("sale_date") or update_data.get("thoi_gian_xuat_ban") or update_data.get("ngay_thuc_hien"))
                buyer = update_data.get("buyer_name") or update_data.get("co_so_thu_mua")
                contact = update_data.get("buyer_contact")
                qty = _parse_number(update_data.get("quantity") or update_data.get("khoi_luong_tieu_thu"))
                unit = update_data.get("unit")
                price = _parse_number(update_data.get("unit_price"))
                total = _parse_number(update_data.get("total_amount"))
                note = update_data.get("note")

                s_clauses = []
                s_params = []
                if s_date:
                    s_clauses.append("sale_date = %s")
                    s_params.append(s_date)
                if buyer is not None:
                    s_clauses.append("buyer_name = %s")
                    s_params.append(buyer)
                if contact is not None:
                    s_clauses.append("buyer_contact = %s")
                    s_params.append(contact)
                if qty is not None:
                    s_clauses.append("quantity = %s")
                    s_params.append(qty)
                if unit:
                    s_clauses.append("unit = %s")
                    s_params.append(unit)
                if price is not None:
                    s_clauses.append("unit_price = %s")
                    s_params.append(price)
                if total is not None:
                    s_clauses.append("total_amount = %s")
                    s_params.append(total)
                if note is not None:
                    s_clauses.append("note = %s")
                    s_params.append(note)

                if s_clauses:
                    s_params.append(record_id)
                    cur.execute(f"UPDATE sales_records SET {', '.join(s_clauses)} WHERE sale_id = %s;", tuple(s_params))

            # 6. BẢNG XỬ LÝ BAO BÌ (waste_disposals)
            elif tbl in ("waste_disposals", "b1_7_waste_treatments", "waste_treatments", "waste"):
                w_date = _parse_date(update_data.get("treatment_date") or update_data.get("disposal_date") or update_data.get("ngay_thuc_hien"))
                w_type = update_data.get("waste_type") or update_data.get("loai_bao_bi_thuoc_du")
                qty = _parse_number(update_data.get("quantity"))
                unit = update_data.get("unit")
                loc = update_data.get("storage_location") or update_data.get("noi_ton_tru")
                meth = update_data.get("treatment_method") or update_data.get("cach_xu_ly")
                note = update_data.get("note")

                w_clauses = []
                w_params = []
                if w_date:
                    w_clauses.append("disposal_date = %s")
                    w_params.append(w_date)
                if w_type is not None:
                    w_clauses.append("waste_type = %s")
                    w_params.append(w_type)
                if qty is not None:
                    w_clauses.append("quantity = %s")
                    w_params.append(qty)
                if unit:
                    w_clauses.append("unit = %s")
                    w_params.append(unit)
                if loc is not None:
                    w_clauses.append("storage_location = %s")
                    w_params.append(loc)
                if meth is not None:
                    w_clauses.append("treatment_method = %s")
                    w_params.append(meth)
                if note is not None:
                    w_clauses.append("note = %s")
                    w_params.append(note)

                if w_clauses:
                    w_params.append(record_id)
                    cur.execute(f"UPDATE waste_disposals SET {', '.join(w_clauses)} WHERE disposal_id = %s;", tuple(w_params))

            # 7. BẢNG CANH TÁC KHÁC (cultivation_activities)
            else:
                act_date = _parse_date(update_data.get("activity_date") or update_data.get("ngay_thuc_hien"))
                act_time = _parse_time(update_data.get("activity_time"))
                desc = update_data.get("cong_viec") or update_data.get("task_name") or update_data.get("description")
                qty = update_data.get("so_luong") or update_data.get("so_luong_lieu_luong") or update_data.get("quantity")
                growth = update_data.get("growth_stage") or update_data.get("giai_doan")
                if growth:
                    growth = detect_growth_stage_from_text(growth) or growth
                weather = update_data.get("weather_condition")
                performer = update_data.get("performed_by")

                if desc is not None:
                    desc_clean = re.sub(r"\s*\((?:Số lượng(?:/liều lượng)?|Liều lượng):\s*[^)]+\)", "", str(desc), flags=re.IGNORECASE).strip()
                    if qty and str(qty).strip():
                        desc = f"{desc_clean} (Số lượng/liều lượng: {str(qty).strip()})"
                    else:
                        desc = desc_clean
                elif qty is not None and str(qty).strip():
                    cur.execute("SELECT description FROM cultivation_activities WHERE activity_id = %s;", (record_id,))
                    cur_row = cur.fetchone()
                    if cur_row and cur_row.get("description"):
                        desc_clean = re.sub(r"\s*\((?:Số lượng(?:/liều lượng)?|Liều lượng):\s*[^)]+\)", "", str(cur_row["description"]), flags=re.IGNORECASE).strip()
                        desc = f"{desc_clean} (Số lượng/liều lượng: {str(qty).strip()})"

                act_clauses = []
                act_params = []
                if act_date:
                    act_clauses.append("activity_date = %s")
                    act_params.append(act_date)
                if act_time:
                    act_clauses.append("activity_time = %s")
                    act_params.append(act_time)
                if desc is not None:
                    act_clauses.append("description = %s")
                    act_params.append(desc)
                if growth is not None:
                    act_clauses.append("growth_stage = %s")
                    act_params.append(growth)
                if weather is not None:
                    act_clauses.append("weather_condition = %s")
                    act_params.append(weather)
                if performer is not None:
                    act_clauses.append("performed_by = %s")
                    act_params.append(performer)

                if act_clauses:
                    act_params.append(record_id)
                    cur.execute(f"UPDATE cultivation_activities SET {', '.join(act_clauses)} WHERE activity_id = %s;", tuple(act_params))

        conn.close()
        return True
    except Exception as e:
        logger.exception("Lỗi khi cập nhật bản ghi nhật ký: %s", e)
        return False


def get_diary_record_by_id(table_name: str, record_id: Any) -> Optional[Dict[str, Any]]:
    """Lấy chi tiết một bản ghi nhật ký theo bảng và ID trong CSDL vietgap_diary."""
    if not table_name or not record_id:
        return None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            tbl = (table_name or "").lower()

            if tbl in ("input_purchases", "b1_5_material_purchases", "material_purchases", "purchase"):
                cur.execute(
                    """
                    SELECT 
                        ip.*,
                        COALESCE(p.name, f.name, m.name, 'Vật tư') AS material_name,
                        s.name AS supplier_name,
                        cs.season_name
                    FROM input_purchases ip
                    LEFT JOIN pesticides p ON ip.pesticide_id = p.pesticide_id
                    LEFT JOIN fertilizers f ON ip.fertilizer_id = f.fertilizer_id
                    LEFT JOIN materials m ON ip.material_id = m.material_id
                    LEFT JOIN suppliers s ON ip.supplier_id = s.supplier_id
                    LEFT JOIN crop_seasons cs ON ip.season_id = cs.season_id
                    WHERE ip.purchase_id = %s LIMIT 1;
                    """,
                    (record_id,),
                )
                row = cur.fetchone()
                if row:
                    row["id"] = row["purchase_id"]
                    row["table"] = "input_purchases"
                    return _serialize_row(row)

            elif tbl in ("pesticide_applications", "b1_6_pesticides", "spray"):
                cur.execute(
                    """
                    SELECT 
                        pa.*, ca.activity_date, ca.activity_time, ca.growth_stage, ca.description,
                        ca.weather_condition, ca.performed_by, p.name AS pesticide_name,
                        s.name AS supplier_name, vr.stt_text AS raw_text, vr.audio_url
                    FROM pesticide_applications pa
                    JOIN cultivation_activities ca ON pa.activity_id = ca.activity_id
                    LEFT JOIN pesticides p ON pa.pesticide_id = p.pesticide_id
                    LEFT JOIN suppliers s ON pa.supplier_id = s.supplier_id
                    LEFT JOIN activity_voice_records vr ON ca.activity_id = vr.activity_id
                    WHERE pa.application_id = %s OR pa.activity_id = %s LIMIT 1;
                    """,
                    (record_id, record_id),
                )
                row = cur.fetchone()
                if row:
                    row["id"] = row["activity_id"]
                    row["table"] = "pesticide_applications"
                    return _serialize_row(row)

            elif tbl in ("fertilizer_applications", "b1_6_fertilizers", "fertilize"):
                cur.execute(
                    """
                    SELECT 
                        fa.*, ca.activity_date, ca.activity_time, ca.growth_stage, ca.description,
                        ca.weather_condition, ca.performed_by, f.name AS fertilizer_name,
                        s.name AS supplier_name, vr.stt_text AS raw_text, vr.audio_url
                    FROM fertilizer_applications fa
                    JOIN cultivation_activities ca ON fa.activity_id = ca.activity_id
                    LEFT JOIN fertilizers f ON fa.fertilizer_id = f.fertilizer_id
                    LEFT JOIN suppliers s ON fa.supplier_id = s.supplier_id
                    LEFT JOIN activity_voice_records vr ON ca.activity_id = vr.activity_id
                    WHERE fa.application_id = %s OR fa.activity_id = %s LIMIT 1;
                    """,
                    (record_id, record_id),
                )
                row = cur.fetchone()
                if row:
                    row["id"] = row["activity_id"]
                    row["table"] = "fertilizer_applications"
                    return _serialize_row(row)

            elif tbl in ("harvest_records", "b1_8_harvests", "harvests", "harvest"):
                cur.execute(
                    """
                    SELECT 
                        hr.*, ca.activity_date, ca.activity_time, ca.growth_stage, ca.performed_by,
                        cs.crop_name, cs.variety, vr.stt_text AS raw_text, vr.audio_url
                    FROM harvest_records hr
                    JOIN cultivation_activities ca ON hr.activity_id = ca.activity_id
                    JOIN crop_seasons cs ON hr.season_id = cs.season_id
                    LEFT JOIN activity_voice_records vr ON ca.activity_id = vr.activity_id
                    WHERE hr.harvest_id = %s OR hr.activity_id = %s LIMIT 1;
                    """,
                    (record_id, record_id),
                )
                row = cur.fetchone()
                if row:
                    row["id"] = row["harvest_id"]
                    row["table"] = "harvest_records"
                    return _serialize_row(row)

            elif tbl in ("sales_records", "b1_9_sales", "sales", "sale"):
                cur.execute(
                    """
                    SELECT 
                        sr.*, cs.crop_name
                    FROM sales_records sr
                    LEFT JOIN harvest_records hr ON sr.harvest_id = hr.harvest_id
                    LEFT JOIN crop_seasons cs ON hr.season_id = cs.season_id
                    WHERE sr.sale_id = %s LIMIT 1;
                    """,
                    (record_id,),
                )
                row = cur.fetchone()
                if row:
                    row["id"] = row["sale_id"]
                    row["table"] = "sales_records"
                    return _serialize_row(row)

            elif tbl in ("waste_disposals", "b1_7_waste_treatments", "waste_treatments", "waste"):
                cur.execute(
                    "SELECT wd.*, cs.season_name FROM waste_disposals wd LEFT JOIN crop_seasons cs ON wd.season_id = cs.season_id WHERE wd.disposal_id = %s LIMIT 1;",
                    (record_id,),
                )
                row = cur.fetchone()
                if row:
                    row["id"] = row["disposal_id"]
                    row["table"] = "waste_disposals"
                    return _serialize_row(row)

            else:
                cur.execute(
                    """
                    SELECT ca.*, vr.stt_text AS raw_text, vr.audio_url
                    FROM cultivation_activities ca
                    LEFT JOIN activity_voice_records vr ON ca.activity_id = vr.activity_id
                    WHERE ca.activity_id = %s LIMIT 1;
                    """,
                    (record_id,),
                )
                row = cur.fetchone()
                if row:
                    raw_desc = row.get("description") or ""
                    clean_desc = raw_desc
                    qty_val = ""
                    m_qty = re.search(r"\((?:Số lượng(?:/liều lượng)?|Liều lượng):\s*([^)]+)\)", raw_desc, re.IGNORECASE)
                    if m_qty:
                        qty_val = m_qty.group(1).strip()
                        clean_desc = re.sub(r"\s*\((?:Số lượng(?:/liều lượng)?|Liều lượng):\s*[^)]+\)", "", raw_desc, flags=re.IGNORECASE).strip()
                    row["cong_viec"] = clean_desc
                    row["task_name"] = clean_desc
                    row["description"] = clean_desc
                    row["raw_description"] = raw_desc
                    row["so_luong"] = qty_val
                    row["so_luong_lieu_luong"] = qty_val
                    row["quantity"] = qty_val
                    row["id"] = row["activity_id"]
                    row["table"] = "cultivation_activities"
                    return _serialize_row(row)

        conn.close()
    except Exception as e:
        logger.exception("Lỗi get_diary_record_by_id: %s", e)
    return None


# ==============================================================================
# TRUY XUẤT NGUỒN GỐC VIETGAP (TRACEABILITY & PUBLIC PASSPORT)
# ==============================================================================

def _format_batch_code(harvest_id: Optional[int], activity_date: Any) -> str:
    """Tạo mã lô truy xuất chuẩn NK-YYYYMMDD-H<id>."""
    if not harvest_id:
        return "VN-HBOR-0033"
    d_str = _parse_date(activity_date) or date.today().strftime("%Y-%m-%d")
    compact_date = d_str.replace("-", "")
    return f"NK-{compact_date}-H{harvest_id}"


def _compute_phi_safety(cur, season_id: int, ref_date_str: str) -> Dict[str, Any]:
    """
    Tính toán chỉ số an toàn thời gian cách ly thuốc BVTV (PHI)
    so với ngày thu hoạch (hoặc ngày hiện tại).
    """
    cur.execute(
        """
        SELECT
            ca.activity_id,
            ca.activity_date,
            ca.description,
            pa.dose_quantity,
            pa.dose_unit,
            pa.target_pest,
            pa.isolation_days,
            pa.safe_harvest_date,
            p.name AS pesticide_name,
            p.active_ingredient,
            p.min_isolation_days
        FROM pesticide_applications pa
        JOIN cultivation_activities ca ON pa.activity_id = ca.activity_id
        LEFT JOIN pesticides p ON pa.pesticide_id = p.pesticide_id
        WHERE ca.activity_date <= %s
        ORDER BY ca.activity_date DESC, ca.activity_id DESC
        LIMIT 1;
        """,
        (ref_date_str,),
    )
    last_spray = cur.fetchone()

    commitments = [
        "Không sử dụng thuốc BVTV ngoài danh mục được phép tại Việt Nam",
        "Tuân thủ nghiêm ngặt thời gian cách ly (PHI) trước khi thu hoạch",
        "Nhật ký canh tác được ghi nhận trực tiếp tại vườn qua Edge AI",
    ]

    if not last_spray:
        return {
            "has_spray_record": False,
            "is_safe": True,
            "safety_status": "SAFE_VERIFIED",
            "safety_label": "ĐẠT CHUẨN AN TOÀN VIETGAP — KHÔNG SỬ DỤNG HÓA CHẤT SÁT VỤ",
            "last_spray_date": None,
            "last_pesticide_name": "Canh tác hữu cơ / Chưa phun thuốc",
            "active_ingredient": "Không phát sinh",
            "target_pest": "Phòng trừ tổng hợp (IPM)",
            "required_phi_days": 0,
            "actual_days_elapsed": 30,
            "days_remaining": 0,
            "excess_safe_days": 30,
            "safe_harvest_date": ref_date_str,
            "commitments": commitments,
        }

    spray_date_obj = last_spray.get("activity_date")
    if isinstance(spray_date_obj, str):
        try:
            spray_date_obj = datetime.strptime(spray_date_obj[:10], "%Y-%m-%d").date()
        except Exception:
            spray_date_obj = date.today()
    elif isinstance(spray_date_obj, datetime):
        spray_date_obj = spray_date_obj.date()

    try:
        ref_date_obj = datetime.strptime(ref_date_str[:10], "%Y-%m-%d").date()
    except Exception:
        ref_date_obj = date.today()

    req_phi = (
        last_spray.get("isolation_days")
        or last_spray.get("min_isolation_days")
        or 7
    )
    req_phi = int(req_phi) if req_phi is not None else 7

    elapsed = max(0, (ref_date_obj - spray_date_obj).days)
    is_safe = elapsed >= req_phi
    days_remaining = max(0, req_phi - elapsed)
    excess_days = max(0, elapsed - req_phi)
    safe_date_obj = spray_date_obj + timedelta(days=req_phi)

    return {
        "has_spray_record": True,
        "is_safe": is_safe,
        "safety_status": "SAFE_VERIFIED" if is_safe else "QUARANTINE_PENDING",
        "safety_label": (
            "ĐẠT CHUẨN AN TOÀN VIETGAP — ĐÃ ĐỦ THỜI GIAN CÁCH LY (PHI)"
            if is_safe
            else f"ĐANG TRONG THỜI GIAN CÁCH LY (CÒN {days_remaining} NGÀY)"
        ),
        "last_spray_date": spray_date_obj.isoformat(),
        "last_pesticide_name": last_spray.get("pesticide_name") or last_spray.get("description") or "Thuốc BVTV trong danh mục",
        "active_ingredient": last_spray.get("active_ingredient") or "Theo danh mục cho phép",
        "target_pest": last_spray.get("target_pest") or "Phòng trừ sâu bệnh hại",
        "required_phi_days": req_phi,
        "actual_days_elapsed": elapsed,
        "days_remaining": days_remaining,
        "excess_safe_days": excess_days,
        "safe_harvest_date": safe_date_obj.isoformat(),
        "commitments": commitments,
    }


def get_traceability_batches(user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Lấy danh sách các lô thu hoạch (harvest_records) + hồ sơ vùng trồng
    kèm trạng thái kiểm định an toàn PHI để hiển thị và in mã QR.
    """
    batches: List[Dict[str, Any]] = []
    try:
        conn = get_db_connection()
        season_id = get_default_season_id(conn, user_id)
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    hr.harvest_id,
                    hr.activity_id,
                    hr.season_id,
                    hr.quantity,
                    hr.unit,
                    hr.processing_location,
                    hr.harvester_name,
                    hr.note,
                    ca.activity_date,
                    ca.activity_time,
                    ca.growth_stage,
                    ca.description,
                    ca.performed_by,
                    cs.season_name,
                    cs.crop_name,
                    cs.variety,
                    p.name AS plot_name,
                    p.plot_code,
                    f.name AS farm_name,
                    f.farm_code
                FROM harvest_records hr
                JOIN cultivation_activities ca ON hr.activity_id = ca.activity_id
                JOIN crop_seasons cs ON hr.season_id = cs.season_id
                JOIN plots p ON cs.plot_id = p.plot_id
                JOIN farm f ON p.farm_id = f.farm_id
                ORDER BY ca.activity_date DESC, hr.harvest_id DESC
                LIMIT 25;
                """
            )
            rows = cur.fetchall() or []

            for r in rows:
                h_date = _parse_date(r.get("activity_date")) or date.today().isoformat()
                b_code = _format_batch_code(r.get("harvest_id"), h_date)
                phi = _compute_phi_safety(cur, r.get("season_id") or season_id, h_date)
                qty = _serialize_db_val(r.get("quantity")) or 0
                unit = r.get("unit") or "kg"
                crop = r.get("crop_name") or "Bưởi diễn"
                if "sầu riêng" in str(crop).lower():
                    crop = "Bưởi diễn"
                desc = r.get("description") or f"Thu hoạch {crop}"
                if "sầu riêng" in str(desc).lower():
                    desc = desc.replace("sầu riêng", "Bưởi diễn").replace("Sầu riêng", "Bưởi diễn")

                batches.append({
                    "batch_code": b_code,
                    "harvest_id": r.get("harvest_id"),
                    "activity_id": r.get("activity_id"),
                    "batch_type": "HARVEST_LOT",
                    "title": f"Lô thu hoạch {crop} ({qty:g} {unit})" if isinstance(qty, (int, float)) and qty > 0 else f"Lô thu hoạch {crop}",
                    "product_name": f"{crop} VietGAP — HTX Đại Đồng",
                    "harvest_date": h_date,
                    "quantity": qty,
                    "unit": unit,
                    "plot_code": r.get("plot_code") or "VN-HBOR-0033-L01",
                    "plot_name": r.get("plot_name") or "Lô 1 — Vườn Bưởi diễn Trung tâm",
                    "farm_code": r.get("farm_code") or "VN-HBOR-0033",
                    "farm_name": r.get("farm_name") or "HỢP TÁC XÃ ĐẠI ĐỒNG",
                    "note": r.get("note") or desc,
                    "is_safe": phi["is_safe"],
                    "safety_status": phi["safety_status"],
                    "safety_label": phi["safety_label"],
                    "actual_days_elapsed": phi["actual_days_elapsed"],
                    "required_phi_days": phi["required_phi_days"],
                })

            # Luôn kèm theo 1 Mã Truy Xuất Vùng Trồng Tổng Quát (VN-HBOR-0033)
            today_str = date.today().isoformat()
            phi_farm = _compute_phi_safety(cur, season_id, today_str)
            batches.append({
                "batch_code": "VN-HBOR-0033",
                "harvest_id": None,
                "activity_id": None,
                "batch_type": "FARM_PASSPORT",
                "title": "Mã vùng trồng cố định (P.U.C: VN-HBOR-0033)",
                "product_name": "Bưởi diễn Đặc Sản VietGAP — HTX Đại Đồng",
                "harvest_date": today_str,
                "quantity": 7.0,
                "unit": "HA",
                "plot_code": "VN-HBOR-0033-L01",
                "plot_name": "Toàn vùng trồng HTX Đại Đồng (07 HA)",
                "farm_code": "VN-HBOR-0033",
                "farm_name": "HỢP TÁC XÃ ĐẠI ĐỒNG",
                "note": "Tem truy xuất nguồn gốc vùng trồng & nhật ký canh tác toàn vụ",
                "is_safe": phi_farm["is_safe"],
                "safety_status": phi_farm["safety_status"],
                "safety_label": phi_farm["safety_label"],
                "actual_days_elapsed": phi_farm["actual_days_elapsed"],
                "required_phi_days": phi_farm["required_phi_days"],
            })

        conn.close()
    except Exception as e:
        logger.exception("Lỗi get_traceability_batches: %s", e)
    return batches


def get_public_trace_passport(batch_code: Optional[str] = None) -> Dict[str, Any]:
    """
    Trả về toàn bộ dữ liệu Hộ Chiếu Nông Sản VietGAP (5 tầng) cho một mã batch_code
    (hoặc mã vùng trồng VN-HBOR-0033). Phục vụ trang công khai khi quét QR.
    """
    code_clean = (batch_code or "").strip().upper()
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            # 1. Xác định lô thu hoạch nếu batch_code có dạng NK-...-H<id> hoặc H<id> hoặc A<id>
            target_harvest = None
            harvest_id_match = re.search(r"-H(\d+)$|^H(\d+)$", code_clean)
            act_id_match = re.search(r"-A(\d+)$|^A(\d+)$", code_clean)

            if harvest_id_match:
                h_id = int(harvest_id_match.group(1) or harvest_id_match.group(2))
                cur.execute(
                    """
                    SELECT
                        hr.*, ca.activity_date, ca.activity_time, ca.description,
                        ca.performed_by, ca.growth_stage,
                        cs.season_name, cs.crop_name, cs.variety, cs.start_date,
                        p.name AS plot_name, p.plot_code, p.area_size, p.area_unit, p.soil_type, p.water_source,
                        f.name AS farm_name, f.farm_code, f.address AS farm_address, f.phone AS farm_phone,
                        f.certification_body, f.certification_no, f.certification_date, f.certification_expiry,
                        u.full_name AS farmer_name
                    FROM harvest_records hr
                    JOIN cultivation_activities ca ON hr.activity_id = ca.activity_id
                    JOIN crop_seasons cs ON hr.season_id = cs.season_id
                    JOIN plots p ON cs.plot_id = p.plot_id
                    JOIN farm f ON p.farm_id = f.farm_id
                    LEFT JOIN users u ON f.owner_user_id = u.user_id
                    WHERE hr.harvest_id = %s
                    LIMIT 1;
                    """,
                    (h_id,),
                )
                target_harvest = cur.fetchone()
            elif act_id_match:
                a_id = int(act_id_match.group(1) or act_id_match.group(2))
                cur.execute(
                    """
                    SELECT
                        hr.*, ca.activity_date, ca.activity_time, ca.description,
                        ca.performed_by, ca.growth_stage,
                        cs.season_name, cs.crop_name, cs.variety, cs.start_date,
                        p.name AS plot_name, p.plot_code, p.area_size, p.area_unit, p.soil_type, p.water_source,
                        f.name AS farm_name, f.farm_code, f.address AS farm_address, f.phone AS farm_phone,
                        f.certification_body, f.certification_no, f.certification_date, f.certification_expiry,
                        u.full_name AS farmer_name
                    FROM harvest_records hr
                    JOIN cultivation_activities ca ON hr.activity_id = ca.activity_id
                    JOIN crop_seasons cs ON hr.season_id = cs.season_id
                    JOIN plots p ON cs.plot_id = p.plot_id
                    JOIN farm f ON p.farm_id = f.farm_id
                    LEFT JOIN users u ON f.owner_user_id = u.user_id
                    WHERE hr.activity_id = %s
                    LIMIT 1;
                    """,
                    (a_id,),
                )
                target_harvest = cur.fetchone()
            elif code_clean not in ("VN-HBOR-0033", "VN-HBOR-0033-L01", "VN-HBOR-0033-L02"):
                # Mặc định lấy lô thu hoạch mới nhất nếu không truyền mã cụ thể
                cur.execute(
                    """
                    SELECT
                        hr.*, ca.activity_date, ca.activity_time, ca.description,
                        ca.performed_by, ca.growth_stage,
                        cs.season_name, cs.crop_name, cs.variety, cs.start_date,
                        p.name AS plot_name, p.plot_code, p.area_size, p.area_unit, p.soil_type, p.water_source,
                        f.name AS farm_name, f.farm_code, f.address AS farm_address, f.phone AS farm_phone,
                        f.certification_body, f.certification_no, f.certification_date, f.certification_expiry,
                        u.full_name AS farmer_name
                    FROM harvest_records hr
                    JOIN cultivation_activities ca ON hr.activity_id = ca.activity_id
                    JOIN crop_seasons cs ON hr.season_id = cs.season_id
                    JOIN plots p ON cs.plot_id = p.plot_id
                    JOIN farm f ON p.farm_id = f.farm_id
                    LEFT JOIN users u ON f.owner_user_id = u.user_id
                    ORDER BY ca.activity_date DESC, hr.harvest_id DESC
                    LIMIT 1;
                    """
                )
                target_harvest = cur.fetchone()

            # 2. Lấy thông tin mặc định của Farm & Plot nếu truy xuất theo mã vùng trồng VN-HBOR-0033
            cur.execute(
                """
                SELECT
                    cs.season_id, cs.season_name, cs.crop_name, cs.variety, cs.start_date,
                    p.plot_id, p.name AS plot_name, p.plot_code, p.area_size, p.area_unit, p.soil_type, p.water_source,
                    f.farm_id, f.name AS farm_name, f.farm_code, f.address AS farm_address, f.phone AS farm_phone,
                    f.certification_body, f.certification_no, f.certification_date, f.certification_expiry,
                    u.full_name AS farmer_name
                FROM crop_seasons cs
                JOIN plots p ON cs.plot_id = p.plot_id
                JOIN farm f ON p.farm_id = f.farm_id
                LEFT JOIN users u ON f.owner_user_id = u.user_id
                ORDER BY cs.season_id ASC
                LIMIT 1;
                """
            )
            base_info = cur.fetchone() or {}

            active_row = target_harvest or base_info
            season_id = active_row.get("season_id") or 1
            ref_date = _parse_date(active_row.get("activity_date")) or date.today().isoformat()

            resolved_code = (
                _format_batch_code(target_harvest.get("harvest_id"), ref_date)
                if target_harvest
                else (code_clean if code_clean.startswith("VN-") else "VN-HBOR-0033")
            )

            # 3. Tính toán chỉ số an toàn cách ly PHI
            phi_safety = _compute_phi_safety(cur, season_id, ref_date)

            crop_name = active_row.get("crop_name") or "Bưởi diễn"
            if "sầu riêng" in str(crop_name).lower():
                crop_name = "Bưởi diễn"

            raw_note = (target_harvest or {}).get("note") or (target_harvest or {}).get("description") or "Đạt tiêu chuẩn VietGAP loại 1"
            if "sầu riêng" in str(raw_note).lower():
                raw_note = raw_note.replace("sầu riêng", "Bưởi diễn").replace("Sầu riêng", "Bưởi diễn")

            batch_info = {
                "batch_code": resolved_code,
                "batch_type": "HARVEST_LOT" if target_harvest else "FARM_PASSPORT",
                "product_name": f"{crop_name} Đặc Sản VietGAP",
                "variety": "Bưởi Diễn chính vụ",
                "season_name": active_row.get("season_name") or "Vụ mùa Bưởi diễn 2026",
                "harvest_date": ref_date,
                "quantity": _serialize_db_val((target_harvest or {}).get("quantity")) or 1200.0,
                "unit": (target_harvest or {}).get("unit") or "kg",
                "processing_location": (target_harvest or {}).get("processing_location") or "Nhà sơ chế & đóng gói tại vườn HTX Đại Đồng",
                "harvester_name": (target_harvest or {}).get("harvester_name") or active_row.get("performed_by") or "Vũ Xuân Anh",
                "grade_note": raw_note,
                "is_safe": phi_safety["is_safe"],
                "safety_status": phi_safety["safety_status"],
                "safety_label": phi_safety["safety_label"],
            }

            origin_passport = {
                "farm_name": active_row.get("farm_name") or "HỢP TÁC XÃ ĐẠI ĐỒNG",
                "farmer_name": active_row.get("farmer_name") or "Vũ Xuân Anh",
                "puc_code": active_row.get("farm_code") or "VN-HBOR-0033",
                "plot_code": active_row.get("plot_code") or "VN-HBOR-0033-L01",
                "plot_name": active_row.get("plot_name") or "Lô 1 — Vườn Bưởi diễn Trung tâm",
                "area_text": "07 HA (70.000 m²)",
                "address": "Xóm Đại Đồng - Xã Yên Trị - Tỉnh Phú Thọ",
                "phone": active_row.get("farm_phone") or "0912.345.678",
                "certification_body": active_row.get("certification_body") or "VietGAP Trồng trọt (TCVN 11892-1:2017)",
                "certification_no": active_row.get("certification_no") or "VietGAP-HBOR-0033",
                "certification_expiry": _parse_date(active_row.get("certification_expiry")) or "2027-12-31",
                "soil_type": active_row.get("soil_type") or "Đất đồi trung du & phù sa giàu khoáng",
                "water_source": active_row.get("water_source") or "Nước ngầm giếng khoan kiểm định định kỳ",
            }

            # 4. Lấy Dòng thời gian Nhật ký Canh tác minh bạch (ẩn chi phí/giá bán nội bộ)
            cur.execute(
                """
                SELECT
                    ca.activity_id,
                    ca.activity_type,
                    ca.activity_date,
                    ca.activity_time,
                    ca.growth_stage,
                    ca.description,
                    ca.weather_condition,
                    ca.performed_by,
                    ca.source,
                    pa.dose_quantity,
                    pa.dose_unit,
                    pa.target_pest,
                    pa.isolation_days,
                    pa.application_method AS spray_method,
                    p.name AS pesticide_name,
                    p.active_ingredient,
                    fa.quantity AS fert_quantity,
                    fa.unit AS fert_unit,
                    fa.application_method AS fert_method,
                    fz.name AS fertilizer_name,
                    po.pest_name,
                    po.severity AS severity_level,
                    po.action_taken AS symptom_description,
                    hr.quantity AS harvest_quantity,
                    hr.unit AS harvest_unit,
                    hr.processing_location,
                    hr.note AS harvest_note
                FROM cultivation_activities ca
                LEFT JOIN pesticide_applications pa ON ca.activity_id = pa.activity_id
                LEFT JOIN pesticides p ON pa.pesticide_id = p.pesticide_id
                LEFT JOIN fertilizer_applications fa ON ca.activity_id = fa.activity_id
                LEFT JOIN fertilizers fz ON fa.fertilizer_id = fz.fertilizer_id
                LEFT JOIN pest_observations po ON ca.activity_id = po.activity_id
                LEFT JOIN harvest_records hr ON ca.activity_id = hr.activity_id
                WHERE ca.activity_date <= %s
                ORDER BY ca.activity_date DESC, ca.activity_id DESC
                LIMIT 25;
                """,
                (ref_date,),
            )
            act_rows = cur.fetchall() or []

            timeline: List[Dict[str, Any]] = []
            for ar in act_rows:
                atype = (ar.get("activity_type") or "OTHER").upper()
                a_date = _parse_date(ar.get("activity_date")) or ref_date
                desc = ar.get("description") or ""
                if "sầu riêng" in desc.lower():
                    desc = desc.replace("sầu riêng", "bưởi Diễn").replace("Sầu riêng", "Bưởi Diễn")

                if atype == "HARVEST":
                    h_qty = _serialize_db_val(ar.get("harvest_quantity"))
                    h_unit = ar.get("harvest_unit") or "kg"
                    title = "Thu hoạch & Phân loại tại vườn"
                    detail = desc or f"Thu hoạch {h_qty:g} {h_unit} trái đạt chuẩn VietGAP"
                    badge = f"{h_qty:g} {h_unit}" if isinstance(h_qty, (int, float)) and h_qty > 0 else "Thu hoạch"
                    category = "harvest"
                elif atype == "PESTICIDE":
                    p_name = ar.get("pesticide_name") or "Thuốc BVTV trong danh mục"
                    t_pest = ar.get("target_pest") or "Phòng trừ sâu bệnh"
                    iso = ar.get("isolation_days") or 7
                    title = f"Phun phòng trừ: {p_name}"
                    detail = f"Đối tượng: {t_pest}. Thời gian cách ly quy định: {iso} ngày."
                    badge = f"Cách ly {iso} ngày"
                    category = "spray"
                elif atype == "FERTILIZER":
                    f_name = ar.get("fertilizer_name") or "Phân bón hữu cơ / NPK"
                    f_qty = _serialize_db_val(ar.get("fert_quantity"))
                    f_unit = ar.get("fert_unit") or "kg"
                    f_method = ar.get("fert_method") or "Bón quanh tán gốc"
                    title = f"Bón dinh dưỡng: {f_name}"
                    detail = f"{f_method} ({f_qty:g} {f_unit})" if isinstance(f_qty, (int, float)) and f_qty > 0 else f_method
                    badge = "Dinh dưỡng"
                    category = "fertilize"
                elif atype == "PEST_OBSERVATION":
                    p_obs = ar.get("pest_name") or "Kiểm tra sinh vật gây hại"
                    sym = ar.get("symptom_description") or desc or "Thăm vườn định kỳ"
                    title = f"Thăm vườn & Theo dõi: {p_obs}"
                    detail = sym
                    badge = "Giám sát IPM"
                    category = "observation"
                else:
                    title = "Chăm sóc & Vệ sinh vườn"
                    detail = desc or "Tỉa cành tạo tán, phát cỏ quanh gốc và duy trì độ ẩm"
                    badge = "Canh tác"
                    category = "general"

                performer = ar.get("performed_by") or "Vũ Xuân Anh"
                if performer == "Nguyễn Văn Nông":
                    performer = "Vũ Xuân Anh"

                timeline.append({
                    "activity_id": ar.get("activity_id"),
                    "category": category,
                    "date": a_date,
                    "growth_stage": ar.get("growth_stage") or "Sinh trưởng",
                    "title": title,
                    "detail": detail,
                    "badge": badge,
                    "performed_by": performer,
                    "verified_source": "Edge AI Voice/Vision" if ar.get("source") == "VOICE_AI" else "Sổ điện tử VietGAP",
                })

            conn.close()

            all_batches = get_traceability_batches()
            return {
                "status": "success",
                "batch_info": batch_info,
                "phi_safety": phi_safety,
                "origin_passport": origin_passport,
                "cultivation_timeline": timeline,
                "available_batches": all_batches,
            }
    except Exception as e:
        logger.exception("Lỗi get_public_trace_passport: %s", e)
        return {
            "status": "error",
            "message": str(e),
        }

