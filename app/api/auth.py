"""
Routes cho xác thực tài khoản nông hộ và quản lý phiên làm việc.
"""

import time
from flask import Blueprint, request, jsonify, session, redirect

from app.common import logger
from services.database import authenticate_farmer, get_farmer_by_id
from conversation import conversation_manager

auth_bp = Blueprint("auth", __name__)

_last_session_cleanup_time = 0.0


@auth_bp.before_app_request
def refresh_farmer_session():
    """Tự động đồng bộ thông tin nông hộ & vùng trồng mới nhất từ CSDL vào session, đồng thời dọn dẹp phiên hết hạn."""
    global _last_session_cleanup_time
    now = time.time()
    if (now - _last_session_cleanup_time) > 60:
        _last_session_cleanup_time = now
        try:
            conversation_manager.store.cleanup_expired()
            conversation_manager.cleanup_old_tts_files(max_age_seconds=180, keep_latest=0)
        except Exception as _e_clean:
            logger.debug("Lỗi dọn dẹp phiên hết hạn hook: %s", _e_clean)

    if "farmer" in session and session["farmer"]:
        u_id = session["farmer"].get("user_id") or session["farmer"].get("id")
        if u_id:
            try:
                fresh = get_farmer_by_id(u_id)
                if fresh:
                    session["farmer"] = fresh
            except Exception:
                pass


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """Xác thực đăng nhập nông hộ kết nối CSDL MySQL local."""
    if request.method == "GET":
        host = request.host.split(":")[0]
        return redirect(f"https://{host}:5001/")

    data = request.get_json(silent=True) or request.form
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    farmer = authenticate_farmer(username, password)
    if not farmer:
        return jsonify({"status": "error", "message": "Tên đăng nhập hoặc mật khẩu không chính xác."}), 401

    session["farmer"] = farmer
    logger.info("👨‍🌾 Nông dân '%s' (%s) đã đăng nhập thành công.", farmer.get("username"), farmer.get("full_name"))
    return jsonify({
        "status": "success",
        "farmer": farmer,
        "redirect": "/nongky",
    })


@auth_bp.route("/logout", methods=["GET", "POST"])
def logout():
    """Đăng xuất tài khoản nông hộ và dọn dẹp các file audio TTS của phiên."""
    try:
        conv_sid = session.get("conv_session_id")
        if conv_sid:
            conversation_manager.store.remove(conv_sid)

        farmer = session.get("farmer") or {}
        f_id = farmer.get("user_id") or farmer.get("id")
        if f_id:
            conversation_manager.cleanup_farmer_sessions(int(f_id))

        conversation_manager.cleanup_old_tts_files(max_age_seconds=60, keep_latest=0)
    except Exception as e:
        logger.debug("Lỗi dọn dẹp TTS khi logout: %s", e)

    session.clear()
    if request.method == "POST" or request.is_json:
        return jsonify({"status": "success", "logged_in": False})
    host = request.host.split(":")[0]
    return redirect(f"https://{host}:5001/")


@auth_bp.route("/api/auth/me", methods=["GET"])
def auth_me():
    """Lấy thông tin tài khoản đang đăng nhập."""
    farmer = session.get("farmer")
    if not farmer:
        return jsonify({"status": "unauthorized", "logged_in": False}), 401
    return jsonify({"status": "success", "logged_in": True, "farmer": farmer})
