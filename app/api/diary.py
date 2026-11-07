"""
Routes cho Sổ nhật ký canh tác VietGAP: danh sách nhật ký, thêm mới, sửa, xoá, danh mục và quản lý lô thửa GPS.
"""

from flask import Blueprint, request, jsonify, session

import config
from app.common import (
    logger,
    apply_normalizer,
)
from services.database import (
    get_diary_records,
    get_diary_by_tables,
    get_diary_record_by_id,
    save_diary_to_mysql,
    update_diary_record,
    delete_diary_record,
    get_categories,
    get_plots,
    find_nearest_plot,
    detect_growth_stage_from_text,
    update_season_growth_stage,
    GROWTH_STAGES,
)

diary_bp = Blueprint("diary", __name__)

ACTIVITY_REQUIRED_FIELDS = {
    "spray": [
        ("ten_thuoc_bvtv", "Tên thuốc BVTV", ["material_name", "ten_thuoc", "ten_phan_thuoc"]),
        ("target_pest", "Đối tượng sâu bệnh", ["sinh_vat_gay_hai", "pest_name"]),
        ("nong_do_va_luong_dung", "Nồng độ & liều lượng", ["concentration", "luong_nuoc_pha", "lieu_luong_thuoc", "lieu_luong"]),
        ("thoi_gian_cach_ly", "Thời gian cách ly", ["isolation_days", "quarantine_days", "cach_ly"]),
    ],
    "fertilize": [
        ("ten_phan_bon", "Tên phân bón", ["material_name", "ten_phan_thuoc"]),
        ("luong_phan_bon", "Lượng bón", ["quantity", "luong_su_dung", "so_luong"]),
    ],
    "purchase": [
        ("material_name", "Tên vật tư", ["ten_vat_tu", "ten_vat_tu_nhap", "input_name"]),
        ("quantity", "Số lượng / Khối lượng", ["so_luong_vat_tu", "luong_su_dung", "so_luong"]),
        ("unit", "Đơn vị tính", ["don_vi"]),
    ],
    "harvest": [
        ("crop_name", "Loại nông sản", ["loai_nong_san", "ten_cay_trong"]),
        ("san_luong_thu_hoach", "Sản lượng thu hoạch", ["quantity", "harvest_quantity", "so_luong", "yield_amount"]),
    ],
    "sale": [
        ("crop_name", "Loại nông sản", ["loai_nong_san", "ten_cay_trong"]),
        ("khoi_luong_tieu_thu", "Khối lượng bán", ["khoi_luong_ban", "sale_quantity", "quantity", "so_luong"]),
    ],
    "waste": [
        ("material_name", "Tên thuốc / vật tư", ["ten_vat_tu", "ten_thuoc"]),
        ("waste_type", "Loại bao bì / rác", ["loai_bao_bi", "loai_bao_bi_thuoc_du"]),
        ("treatment_method", "Cách xử lý & nơi lưu trữ", ["cach_xu_ly", "phuong_phap_xu_ly", "storage_location"]),
    ],
    "observation": [
        ("sinh_vat_gay_hai", "Sinh vật gây hại phát hiện", ["target_pest", "pest_name", "doi_tuong_sau_benh"]),
    ],
    "general": [
        ("cong_viec", "Tên / Nội dung công việc", ["task_name", "description", "noi_dung_cong_viec"]),
    ],
}

TASK_NAME_DISPLAY = {
    "spray": "Phun thuốc BVTV",
    "fertilize": "Bón phân",
    "purchase": "Mua vật tư",
    "harvest": "Thu hoạch",
    "sale": "Tiêu thụ / Bán",
    "waste": "Xử lý bao bì",
    "observation": "Quan sát",
    "general": "Hoạt động chung",
}


@diary_bp.route("/api/logs", methods=["GET"])
@diary_bp.route("/api/diary", methods=["GET"])
def get_logs():
    """Lấy danh sách nhật ký trực tiếp từ CSDL MySQL vietgap_diary."""
    farmer = session.get("farmer")
    farmer_id = farmer.get("id") if farmer else None
    table_filter = request.args.get("table")
    limit = int(request.args.get("limit", 100))

    try:
        tables = get_diary_by_tables(farmer_id=farmer_id, limit=limit)
        flat_logs = get_diary_records(farmer_id=farmer_id, limit=limit)

        if table_filter and table_filter in tables:
            filtered_logs = tables[table_filter]
            return jsonify({
                "status": "success",
                "source": "mysql_local",
                "database": config.MYSQL_DATABASE,
                "table": table_filter,
                "count": len(filtered_logs),
                "data": filtered_logs,
                "logs": filtered_logs,
            })

        return jsonify({
            "status": "success",
            "source": "mysql_local",
            "database": config.MYSQL_DATABASE,
            "tables": tables,
            "logs": flat_logs,
            "total": len(flat_logs),
        })
    except Exception as e:
        logger.exception("Lỗi khi đọc nhật ký từ MySQL: %s", e)
        return jsonify({
            "status": "error",
            "message": str(e),
            "tables": {},
            "logs": [],
        }), 500


@diary_bp.route("/api/logs", methods=["POST"])
@diary_bp.route("/api/diary", methods=["POST"])
def save_log():
    """Lưu nhật ký canh tác tổng hợp vào CSDL vietgap_diary."""
    entry = request.get_json(silent=True) or {}
    farmer = session.get("farmer")
    farmer_id = farmer.get("id") if farmer else None

    tables_data = entry.get("tables") or {}
    task_types = entry.get("task_types") or []
    if isinstance(task_types, str):
        task_types = [task_types]
    elif not isinstance(task_types, list):
        task_types = []

    if not task_types and isinstance(tables_data, dict):
        task_types = list(tables_data.keys())

    missing_errors = []
    canonical_map = {
        "b1_6_material_usages": "spray",
        "pesticide_applications": "spray",
        "fertilizer_applications": "fertilize",
        "b1_5_material_purchases": "purchase",
        "input_purchases": "purchase",
        "b1_8_harvests": "harvest",
        "harvest_records": "harvest",
        "b1_9_sales": "sale",
        "sales_records": "sale",
        "b1_7_waste_treatments": "waste",
        "waste_disposals": "waste",
        "pest_observations": "observation",
        "cultivation_diaries": "general",
        "cultivation_activities": "general",
    }

    for t_key in task_types:
        canonical_task = canonical_map.get(t_key, t_key)
        reqs = ACTIVITY_REQUIRED_FIELDS.get(canonical_task) or []
        t_data = {}
        if isinstance(tables_data, dict):
            t_data = tables_data.get(t_key) or tables_data.get(canonical_task) or {}

        for req_key, req_label, aliases in reqs:
            val = t_data.get(req_key) if isinstance(t_data, dict) else None
            if not val and isinstance(t_data, dict):
                for al in aliases:
                    val = t_data.get(al)
                    if val:
                        break
            if not val:
                val = entry.get(req_key)
            if not val:
                for al in aliases:
                    val = entry.get(al)
                    if val:
                        break

            if req_key == "crop_name" and (not val or not str(val).strip()):
                crop_from_db = farmer.get("crop_name") if farmer else None
                if not crop_from_db:
                    try:
                        from services.database import get_db_connection, get_crop_name_for_farmer
                        conn = get_db_connection()
                        crop_from_db = get_crop_name_for_farmer(conn, user_id=farmer_id)
                        conn.close()
                    except Exception:
                        crop_from_db = "Bưởi diễn"
                val = crop_from_db or "Bưởi diễn"
                if isinstance(t_data, dict):
                    t_data["crop_name"] = val
                entry["crop_name"] = val

            if not val or not str(val).strip():
                t_display = TASK_NAME_DISPLAY.get(canonical_task, canonical_task)
                missing_errors.append(f"'{req_label}' của {t_display}")

    if missing_errors:
        return jsonify({
            "status": "error",
            "message": f"Thiếu thông tin bắt buộc trước khi lưu: {', '.join(missing_errors)}",
            "missing_fields": missing_errors,
        }), 400

    normalized_entry = apply_normalizer(entry)
    for k in ["id", "created_at", "raw_text", "audio_url", "photo_url", "task_types"]:
        if k in entry:
            normalized_entry[k] = entry[k]

    llm_meta = entry.get("llm_meta") or {}
    if not isinstance(llm_meta, dict):
        llm_meta = {}
    if "raw_response" not in llm_meta and "raw_response" in entry:
        llm_meta["raw_response"] = entry["raw_response"]
    if "raw_llm_output" not in llm_meta and "raw_llm_output" in entry:
        llm_meta["raw_llm_output"] = entry["raw_llm_output"]
    if "raw_llm_output" not in llm_meta and "raw_response" in llm_meta:
        llm_meta["raw_llm_output"] = llm_meta["raw_response"]
    time_process = float(llm_meta.get("latency_s") or 0.0)

    db_res = None
    save_error = None
    try:
        db_res = save_diary_to_mysql(
            farmer_id,
            normalized_entry,
            raw_text=entry.get("raw_text", ""),
            audio_url=entry.get("audio_url", ""),
            photo_url=entry.get("photo_url", ""),
            time_process=time_process,
            llm_meta=llm_meta,
        )
        normalized_entry["id"] = db_res.get("id", normalized_entry.get("id"))
        normalized_entry["table"] = "cultivation_activities"
    except Exception as e:
        save_error = str(e)
        logger.exception("Lỗi khi lưu nhật ký vào MySQL: %s", e)

    updated_stage = db_res.get("growth_stage") if db_res else None
    if updated_stage:
        if "farmer" in session and session["farmer"]:
            session["farmer"]["growth_stage"] = updated_stage
            session.modified = True

    if save_error and not db_res:
        return jsonify({
            "status": "error",
            "message": f"Lỗi ghi sổ nhật ký: {save_error}",
            "entry": normalized_entry,
            "db": None,
        }), 500

    return jsonify({
        "status": "success",
        "message": "Đã lưu sổ nhật ký thành công",
        "source": "mysql_local" if db_res else "local_json",
        "activity_id": db_res.get("id") if db_res else normalized_entry.get("id"),
        "created_records": db_res.get("created_records", {}) if db_res else {},
        "growth_stage": updated_stage,
        "entry": normalized_entry,
        "db": db_res,
    }), 201 if db_res else 200


@diary_bp.route("/api/diary/<table_name>", methods=["POST"])
def save_specific_diary_table(table_name):
    """
    API chuyên biệt lưu trực tiếp một bản ghi nhật ký vào bảng cụ thể theo đúng schema vietgap_diary.
    """
    body = request.get_json(silent=True) or {}
    farmer = session.get("farmer")
    farmer_id = farmer.get("id") if farmer else None

    tbl = str(table_name).strip().lower().replace("-", "_")

    task_map = {
        "pesticide_applications": "spray",
        "spray": "spray",
        "b1_6_pesticides": "spray",
        "fertilizer_applications": "fertilize",
        "fertilize": "fertilize",
        "b1_6_fertilizers": "fertilize",
        "cultivation_activities": "general",
        "cultivation_diaries": "general",
        "general": "general",
        "harvest_records": "harvest",
        "harvests": "harvest",
        "harvest": "harvest",
        "b1_8_harvests": "harvest",
        "sales_records": "sale",
        "sales": "sale",
        "sale": "sale",
        "b1_9_sales": "sale",
        "input_purchases": "purchase",
        "material_purchases": "purchase",
        "b1_5_material_purchases": "purchase",
        "purchase": "purchase",
        "waste_disposals": "waste",
        "waste_treatments": "waste",
        "b1_7_waste_treatments": "waste",
        "waste": "waste",
        "pest_observations": "observation",
        "observation": "observation",
        "quan_sat": "observation",
        "diary_pests": "observation",
        "pest_observation": "observation",
        "irrigation_records": "irrigation",
    }
    task_type = task_map.get(tbl, "general")

    payload = {
        "task_types": [task_type],
        "tables": {
            task_type: body,
            tbl: body,
        },
        "raw_text": body.get("raw_text", ""),
        "audio_url": body.get("audio_url", ""),
    }

    try:
        db_res = save_diary_to_mysql(
            farmer_id,
            payload,
            raw_text=body.get("raw_text", ""),
            audio_url=body.get("audio_url", ""),
        )
        return jsonify({
            "status": "success",
            "message": f"Đã lưu bản ghi vào bảng {tbl} thành công",
            "table": tbl,
            "activity_id": db_res.get("id"),
            "created_records": db_res.get("created_records", {}),
            "db": db_res,
        }), 201
    except Exception as e:
        logger.exception("Lỗi khi lưu bảng %s: %s", tbl, e)
        return jsonify({
            "status": "error",
            "message": f"Lỗi ghi vào bảng {tbl}: {str(e)}",
        }), 500


@diary_bp.route("/api/diary/<table_name>/<record_id>", methods=["GET"])
def get_diary_detail(table_name, record_id):
    """Lấy chi tiết một bản ghi nhật ký theo ID và tên bảng."""
    row = get_diary_record_by_id(table_name, record_id)
    if not row:
        return jsonify({"status": "error", "message": "Không tìm thấy bản ghi"}), 404
    return jsonify({
        "status": "success",
        "table": table_name,
        "record_id": record_id,
        "data": row,
    })


@diary_bp.route("/api/season/growth-stage", methods=["GET", "POST"])
def api_season_growth_stage():
    """API lấy hoặc cập nhật giai đoạn mùa vụ từ lời nói hoặc văn bản người dân."""
    farmer = session.get("farmer")
    farmer_id = farmer.get("id") if farmer else None

    if request.method == "GET":
        stage = (farmer.get("growth_stage") if farmer else None) or "Sinh trưởng"
        return jsonify({"status": "success", "growth_stage": stage, "available_stages": GROWTH_STAGES})

    body = request.get_json(silent=True) or request.form or {}
    text = body.get("text") or body.get("growth_stage") or ""
    stage = detect_growth_stage_from_text(text)
    if not stage:
        t_clean = str(text).strip()
        for s in GROWTH_STAGES:
            if t_clean.lower() == s.lower():
                stage = s
                break
    if not stage:
        stage = "Sinh trưởng" if not str(text).strip() else str(text).strip()

    ok = update_season_growth_stage(stage, farmer_id=farmer_id)
    if ok and "farmer" in session and session["farmer"]:
        session["farmer"]["growth_stage"] = stage
        session.modified = True

    return jsonify({
        "status": "success",
        "growth_stage": stage,
        "available_stages": GROWTH_STAGES,
        "message": f"Đã cập nhật giai đoạn mùa vụ: {stage}",
    })


@diary_bp.route("/api/logs/<log_id>", methods=["PUT"])
@diary_bp.route("/api/diary/<table_name>/<record_id>", methods=["PUT"])
def update_log(log_id=None, table_name=None, record_id=None):
    """Cập nhật / chỉnh sửa thông tin bản ghi nhật ký trong CSDL vietgap_diary."""
    rec_id = record_id or log_id
    tbl = table_name or request.args.get("table") or "cultivation_activities"
    update_data = request.get_json(silent=True) or {}

    if not rec_id:
        return jsonify({"status": "error", "message": "Thiếu ID bản ghi cần sửa"}), 400

    try:
        ok = update_diary_record(rec_id, tbl, update_data)
        if ok:
            return jsonify({
                "status": "success",
                "message": f"Đã cập nhật bản ghi {rec_id} trong bảng {tbl} thành công",
                "record_id": rec_id,
                "table": tbl,
            })
        else:
            return jsonify({
                "status": "error",
                "message": f"Không thể cập nhật bản ghi {rec_id}",
            }), 400
    except Exception as e:
        logger.exception("Lỗi khi cập nhật nhật ký: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@diary_bp.route("/api/logs/<log_id>", methods=["DELETE"])
@diary_bp.route("/api/diary/<table_name>/<record_id>", methods=["DELETE"])
def delete_log(log_id=None, table_name=None, record_id=None):
    """Xóa một dòng nhật ký theo ID khỏi CSDL vietgap_diary và file backup."""
    rec_id = record_id or log_id
    tbl = table_name or request.args.get("table")
    try:
        delete_diary_record(rec_id, tbl)
    except Exception as e:
        logger.warning("Lỗi xóa từ MySQL: %s", e)

    return jsonify({"status": "success", "deleted_id": rec_id, "table": tbl})


@diary_bp.route("/api/categories", methods=["GET"])
@diary_bp.route("/api/categories/<cat_type>", methods=["GET"])
def get_all_categories(cat_type=None):
    """Lấy danh mục dữ liệu phục vụ gợi ý tự động (autocomplete) và dropdown trên form."""
    try:
        cats = get_categories(cat_type)
        return jsonify({
            "status": "success",
            "category_type": cat_type or "all",
            "data": cats,
        })
    except Exception as e:
        logger.exception("Lỗi lấy danh mục: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@diary_bp.route("/api/plots", methods=["GET"])
def api_get_plots():
    """Lấy danh sách các lô canh tác kèm tọa độ GPS."""
    try:
        farmer = session.get("farmer")
        farm_id = farmer.get("farm_id") if farmer else None
        plots = get_plots(farm_id)
        return jsonify({"status": "success", "data": plots})
    except Exception as e:
        logger.exception("Lỗi lấy danh sách lô: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@diary_bp.route("/api/plots/nearest", methods=["POST", "GET"])
def api_nearest_plot():
    """Tìm lô canh tác gần nhất từ tọa độ GPS."""
    try:
        data = request.get_json(silent=True) or request.values or {}
        lat = data.get("latitude") or data.get("lat")
        lng = data.get("longitude") or data.get("lng") or data.get("lon")
        if lat is None or lng is None:
            return jsonify({"status": "error", "message": "Thiếu tọa độ latitude hoặc longitude"}), 400

        farmer = session.get("farmer")
        farm_id = farmer.get("farm_id") if farmer else None
        nearest = find_nearest_plot(float(lat), float(lng), farm_id)
        plots = get_plots(farm_id)
        return jsonify({
            "status": "success",
            "nearest_plot": nearest,
            "plots": plots,
        })
    except Exception as e:
        logger.exception("Lỗi tìm lô gần nhất: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500
