#!/usr/bin/env python3
"""
Script nhập dữ liệu phân bón và thuốc BVTV từ thư mục extracted_data_json vào CSDL MySQL vietgap_diary.
- Tự động nới rộng độ dài các trường trong CSDL để đảm bảo không bị cắt cụt dữ liệu.
- Thêm cột rag_knowledge (JSON) để lưu trữ toàn bộ tri thức kỹ thuật nông nghiệp phục vụ tra cứu & RAG.
- Cập nhật thông tin chi tiết cho các sản phẩm đã có và thêm mới các sản phẩm chưa có.
"""

import json
import logging
import os
import sys

import pymysql

sys.path.insert(0, "/root/voice_to_data")
import config

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("import_extracted_data")

EXTRACTED_JSON_PATH = "/root/voice_to_data/extracted_data_json/tat_ca_san_pham.json"


def get_connection():
    unix_socket = config.MYSQL_SOCKET if config.MYSQL_SOCKET and os.path.exists(config.MYSQL_SOCKET) else None
    return pymysql.connect(
        host=config.MYSQL_HOST,
        port=config.MYSQL_PORT,
        user=config.MYSQL_USER,
        password=config.MYSQL_PASSWORD,
        database=config.MYSQL_DATABASE,
        unix_socket=unix_socket,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def upgrade_schema_if_needed(cur):
    """Nới rộng kích thước các cột và bổ sung cột rag_knowledge nếu chưa tồn tại."""
    logger.info("Kiểm tra và nâng cấp schema bảng fertilizers và pesticides...")

    # 1. Nâng cấp bảng fertilizers
    cur.execute("ALTER TABLE fertilizers MODIFY COLUMN fertilizer_type VARCHAR(100) NULL;")
    cur.execute("ALTER TABLE fertilizers MODIFY COLUMN npk_ratio TEXT NULL;")
    cur.execute("ALTER TABLE fertilizers MODIFY COLUMN manufacturer VARCHAR(255) NULL;")

    # Kiểm tra cột rag_knowledge trong fertilizers
    cur.execute("""
        SELECT COUNT(*) as cnt FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'fertilizers' AND COLUMN_NAME = 'rag_knowledge';
    """, (config.MYSQL_DATABASE,))
    if cur.fetchone()["cnt"] == 0:
        logger.info("Thêm cột 'rag_knowledge' vào bảng fertilizers...")
        cur.execute("ALTER TABLE fertilizers ADD COLUMN rag_knowledge JSON NULL AFTER manufacturer;")

    # 2. Nâng cấp bảng pesticides
    cur.execute("ALTER TABLE pesticides MODIFY COLUMN active_ingredient VARCHAR(255) NULL;")
    cur.execute("ALTER TABLE pesticides MODIFY COLUMN allowed_crops TEXT NULL;")
    cur.execute("ALTER TABLE pesticides MODIFY COLUMN max_concentration VARCHAR(255) NULL;")
    cur.execute("ALTER TABLE pesticides MODIFY COLUMN toxicity_group VARCHAR(50) NULL;")
    cur.execute("ALTER TABLE pesticides MODIFY COLUMN manufacturer VARCHAR(255) NULL;")

    # Kiểm tra cột rag_knowledge trong pesticides
    cur.execute("""
        SELECT COUNT(*) as cnt FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'pesticides' AND COLUMN_NAME = 'rag_knowledge';
    """, (config.MYSQL_DATABASE,))
    if cur.fetchone()["cnt"] == 0:
        logger.info("Thêm cột 'rag_knowledge' vào bảng pesticides...")
        cur.execute("ALTER TABLE pesticides ADD COLUMN rag_knowledge JSON NULL AFTER manufacturer;")

    logger.info("Nâng cấp schema thành công.")


def clean_str(val):
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def clean_int(val):
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def import_data():
    if not os.path.exists(EXTRACTED_JSON_PATH):
        logger.error("Không tìm thấy file %s", EXTRACTED_JSON_PATH)
        return

    with open(EXTRACTED_JSON_PATH, "r", encoding="utf-8") as f:
        products = json.load(f)

    logger.info("Đã tải %d sản phẩm từ file %s", len(products), EXTRACTED_JSON_PATH)

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            upgrade_schema_if_needed(cur)

            # Lấy danh sách sản phẩm hiện có trong CSDL
            cur.execute("SELECT pesticide_id, name FROM pesticides;")
            existing_pests = {r["name"].lower().strip(): r["pesticide_id"] for r in cur.fetchall()}

            cur.execute("SELECT fertilizer_id, name FROM fertilizers;")
            existing_ferts = {r["name"].lower().strip(): r["fertilizer_id"] for r in cur.fetchall()}

            pesticides_inserted = 0
            pesticides_updated = 0
            fertilizers_inserted = 0
            fertilizers_updated = 0

            for p in products:
                cat = p.get("category")
                rag = p.get("rag_knowledge")
                rag_json = json.dumps(rag, ensure_ascii=False) if rag else None

                # ----------------------------------------------------
                # THUỐC BẢO VỆ THỰC VẬT (PESTICIDE)
                # ----------------------------------------------------
                if cat == "pesticide" or p.get("db_pesticide"):
                    p_info = p.get("db_pesticide") or {}
                    name = clean_str(p_info.get("name") or p.get("product_name"))
                    if not name:
                        continue

                    active_ingredient = clean_str(p_info.get("active_ingredient"))
                    formulation = clean_str(p_info.get("formulation"))
                    registration_no = clean_str(p_info.get("registration_no"))
                    allowed_crops = clean_str(p_info.get("allowed_crops"))
                    min_isolation_days = clean_int(p_info.get("min_isolation_days"))
                    if min_isolation_days is None and rag:
                        min_isolation_days = clean_int(rag.get("thoi_gian_cach_ly_ngay"))
                    max_concentration = clean_str(p_info.get("max_concentration"))
                    toxicity_group = clean_str(p_info.get("toxicity_group"))
                    is_banned = bool(p_info.get("is_banned", False))
                    manufacturer = clean_str(p_info.get("manufacturer"))

                    norm_name = name.lower().strip()
                    if norm_name in existing_pests:
                        # Cập nhật sản phẩm đã có
                        p_id = existing_pests[norm_name]
                        cur.execute("""
                            UPDATE pesticides
                            SET active_ingredient = %s,
                                formulation = %s,
                                registration_no = %s,
                                allowed_crops = %s,
                                min_isolation_days = %s,
                                max_concentration = %s,
                                toxicity_group = %s,
                                is_banned = %s,
                                manufacturer = %s,
                                rag_knowledge = %s,
                                updated_at = NOW()
                            WHERE pesticide_id = %s;
                        """, (
                            active_ingredient, formulation, registration_no,
                            allowed_crops, min_isolation_days, max_concentration,
                            toxicity_group, is_banned, manufacturer, rag_json, p_id
                        ))
                        pesticides_updated += 1
                        logger.info("  [PESTICIDE] UPDATE (ID=%d): %s", p_id, name)
                    else:
                        # Thêm mới thuốc BVTV
                        cur.execute("""
                            INSERT INTO pesticides (
                                name, active_ingredient, formulation, registration_no,
                                allowed_crops, min_isolation_days, max_concentration,
                                toxicity_group, is_banned, manufacturer, rag_knowledge
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                        """, (
                            name, active_ingredient, formulation, registration_no,
                            allowed_crops, min_isolation_days, max_concentration,
                            toxicity_group, is_banned, manufacturer, rag_json
                        ))
                        new_id = cur.lastrowid
                        existing_pests[norm_name] = new_id
                        pesticides_inserted += 1
                        logger.info("  [PESTICIDE] INSERT (ID=%d): %s", new_id, name)

                # ----------------------------------------------------
                # PHÂN BÓN (FERTILIZER)
                # ----------------------------------------------------
                elif cat == "fertilizer" or p.get("db_fertilizer"):
                    f_info = p.get("db_fertilizer") or {}
                    name = clean_str(f_info.get("name") or p.get("product_name"))
                    if not name:
                        continue

                    fertilizer_type = clean_str(f_info.get("fertilizer_type"))
                    npk_ratio = clean_str(f_info.get("npk_ratio"))
                    registration_no = clean_str(f_info.get("registration_no"))
                    manufacturer = clean_str(f_info.get("manufacturer"))

                    norm_name = name.lower().strip()
                    if norm_name in existing_ferts:
                        # Cập nhật phân bón đã có
                        f_id = existing_ferts[norm_name]
                        cur.execute("""
                            UPDATE fertilizers
                            SET fertilizer_type = %s,
                                npk_ratio = %s,
                                registration_no = %s,
                                manufacturer = %s,
                                rag_knowledge = %s,
                                updated_at = NOW()
                            WHERE fertilizer_id = %s;
                        """, (
                            fertilizer_type, npk_ratio, registration_no,
                            manufacturer, rag_json, f_id
                        ))
                        fertilizers_updated += 1
                        logger.info("  [FERTILIZER] UPDATE (ID=%d): %s", f_id, name)
                    else:
                        # Thêm mới phân bón
                        cur.execute("""
                            INSERT INTO fertilizers (
                                name, fertilizer_type, npk_ratio,
                                registration_no, manufacturer, rag_knowledge
                            ) VALUES (%s, %s, %s, %s, %s, %s);
                        """, (
                            name, fertilizer_type, npk_ratio,
                            registration_no, manufacturer, rag_json
                        ))
                        new_id = cur.lastrowid
                        existing_ferts[norm_name] = new_id
                        fertilizers_inserted += 1
                        logger.info("  [FERTILIZER] INSERT (ID=%d): %s", new_id, name)

            conn.commit()

            # Thống kê kết quả
            cur.execute("SELECT COUNT(*) as cnt FROM pesticides;")
            total_pests = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(*) as cnt FROM fertilizers;")
            total_ferts = cur.fetchone()["cnt"]

            print("\n" + "=" * 80)
            print("🎉 HOÀN THÀNH NHẬP DỮ LIỆU VÀO CƠ SỞ DỮ LIỆU VIETGAP_DIARY")
            print("=" * 80)
            print("• THUỐC BVTV (PESTICIDES):")
            print(f"  + Thêm mới thành công : {pesticides_inserted} sản phẩm")
            print(f"  + Cập nhật bổ sung    : {pesticides_updated} sản phẩm")
            print(f"  + Tổng số trong CSDL  : {total_pests} sản phẩm")
            print("-" * 80)
            print("• PHÂN BÓN (FERTILIZERS):")
            print(f"  + Thêm mới thành công : {fertilizers_inserted} sản phẩm")
            print(f"  + Cập nhật bổ sung    : {fertilizers_updated} sản phẩm")
            print(f"  + Tổng số trong CSDL  : {total_ferts} sản phẩm")
            print("=" * 80)

    except Exception as e:
        conn.rollback()
        logger.exception("Lỗi khi nhập dữ liệu: %s", e)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    import_data()
