"""
Routes cho xuất báo cáo VietGAP (Excel, Word, PDF) và truy xuất nguồn gốc nông sản (QR Code Passport).
"""

from flask import Blueprint, request, jsonify, session, send_file

from app.common import logger
from services.database import (
    get_traceability_batches,
    get_public_trace_passport,
)
from services.exporters import (
    export_cultivation_diary_to_bytes,
    export_cultivation_diary_to_docx_bytes,
    export_cultivation_diary_to_pdf_bytes,
    get_available_seasons,
    export_vietgap_tables_excel_bytes,
    export_vietgap_tables_docx_bytes,
    export_vietgap_tables_pdf_bytes,
)

export_bp = Blueprint("export", __name__)


@export_bp.route("/api/export/excel", methods=["GET", "POST"])
@export_bp.route("/export/excel", methods=["GET"])
def export_excel():
    """Xuất sổ nhật ký canh tác theo chuẩn VietGAP ra file Excel (.xlsx)."""
    try:
        farmer = session.get("farmer")
        farmer_id = request.args.get("farmer_id")
        if not farmer_id and farmer:
            farmer_id = farmer.get("user_id") or farmer.get("farmer_id")

        season_id = request.args.get("season_id")
        template = request.args.get("template") or request.args.get("type")
        if not season_id and request.is_json:
            season_id = request.json.get("season_id")
            if not template:
                template = request.json.get("template")
        if season_id:
            try:
                season_id = int(season_id)
            except (ValueError, TypeError):
                season_id = None

        if template in ("vietgap", "tables", "pdf_template"):
            buf, filename = export_vietgap_tables_excel_bytes(season_id=season_id, farmer_id=farmer_id)
        else:
            buf, filename = export_cultivation_diary_to_bytes(season_id=season_id, farmer_id=farmer_id)

        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as e:
        logger.exception("Lỗi xuất Excel: %s", e)
        return jsonify({"status": "error", "message": f"Không thể xuất file Excel: {str(e)}"}), 500


@export_bp.route("/api/export/docx", methods=["GET", "POST"])
@export_bp.route("/export/docx", methods=["GET"])
def export_docx():
    """Xuất sổ nhật ký canh tác theo chuẩn VietGAP ra file Word (.docx)."""
    try:
        farmer = session.get("farmer")
        farmer_id = request.args.get("farmer_id")
        if not farmer_id and farmer:
            farmer_id = farmer.get("user_id") or farmer.get("farmer_id")

        season_id = request.args.get("season_id")
        template = request.args.get("template") or request.args.get("type")
        if not season_id and request.is_json:
            season_id = request.json.get("season_id")
            if not template:
                template = request.json.get("template")
        if season_id:
            try:
                season_id = int(season_id)
            except (ValueError, TypeError):
                season_id = None

        if template in ("vietgap", "tables", "pdf_template"):
            buf, filename = export_vietgap_tables_docx_bytes(season_id=season_id, farmer_id=farmer_id)
        else:
            buf, filename = export_cultivation_diary_to_docx_bytes(season_id=season_id, farmer_id=farmer_id)

        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as e:
        logger.exception("Lỗi xuất Word (docx): %s", e)
        return jsonify({"status": "error", "message": f"Không thể xuất file Word: {str(e)}"}), 500


@export_bp.route("/api/export/pdf", methods=["GET", "POST"])
@export_bp.route("/export/pdf", methods=["GET"])
def export_pdf():
    """Xuất sổ nhật ký canh tác theo chuẩn VietGAP ra file PDF (.pdf)."""
    try:
        farmer = session.get("farmer")
        farmer_id = request.args.get("farmer_id")
        if not farmer_id and farmer:
            farmer_id = farmer.get("user_id") or farmer.get("farmer_id")

        season_id = request.args.get("season_id")
        template = request.args.get("template") or request.args.get("type")
        if not season_id and request.is_json:
            season_id = request.json.get("season_id")
            if not template:
                template = request.json.get("template")
        if season_id:
            try:
                season_id = int(season_id)
            except (ValueError, TypeError):
                season_id = None

        if template in ("vietgap", "tables", "pdf_template"):
            buf, filename = export_vietgap_tables_pdf_bytes(season_id=season_id, farmer_id=farmer_id)
        else:
            buf, filename = export_cultivation_diary_to_pdf_bytes(season_id=season_id, farmer_id=farmer_id)

        return send_file(
            buf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as e:
        logger.exception("Lỗi xuất PDF: %s", e)
        return jsonify({"status": "error", "message": f"Không thể xuất file PDF: {str(e)}"}), 500


@export_bp.route("/api/export/vietgap/<fmt>", methods=["GET", "POST"])
@export_bp.route("/export/vietgap/<fmt>", methods=["GET"])
def export_vietgap_direct(fmt):
    """Xuất trực tiếp các biểu mẫu theo chuẩn vietGAP.pdf (excel, docx, pdf)."""
    fmt_lower = fmt.lower()
    if fmt_lower in ("excel", "xlsx"):
        return export_excel()
    elif fmt_lower in ("docx", "doc", "word"):
        return export_docx()
    elif fmt_lower == "pdf":
        return export_pdf()
    else:
        return jsonify({"status": "error", "message": f"Định dạng {fmt} không được hỗ trợ"}), 400


@export_bp.route("/api/seasons", methods=["GET"])
def get_seasons_api():
    """Lấy danh sách các mùa vụ canh tác để chọn khi xuất báo cáo."""
    try:
        farmer = session.get("farmer")
        farmer_id = request.args.get("farmer_id") or (farmer.get("user_id") if farmer else None)
        seasons = get_available_seasons(farmer_id=farmer_id)
        return jsonify({"status": "success", "data": seasons})
    except Exception as e:
        logger.exception("Lỗi lấy danh sách mùa vụ: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@export_bp.route("/api/traceability/batches", methods=["GET"])
def api_traceability_batches():
    """Lấy danh sách các lô thu hoạch & mã vùng trồng phục vụ cấp mã QR truy xuất."""
    try:
        farmer = session.get("farmer")
        farmer_id = request.args.get("farmer_id") or (farmer.get("user_id") if farmer else None)
        batches = get_traceability_batches(user_id=farmer_id)
        return jsonify({"status": "success", "data": batches})
    except Exception as e:
        logger.exception("Lỗi lấy danh sách lô truy xuất: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@export_bp.route("/api/public/trace", methods=["GET"])
@export_bp.route("/api/public/trace/<path:batch_code>", methods=["GET"])
def api_public_trace(batch_code=None):
    """
    API Công khai (không yêu cầu đăng nhập) trả về Hộ chiếu Nông sản VietGAP 5 tầng
    khi người tiêu dùng / thương lái quét mã QR trên sản phẩm.
    """
    try:
        code = batch_code or request.args.get("code") or request.args.get("batch")
        passport = get_public_trace_passport(code)
        if passport.get("status") == "error":
            return jsonify(passport), 500
        return jsonify(passport)
    except Exception as e:
        logger.exception("Lỗi truy xuất nguồn gốc công khai: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500
