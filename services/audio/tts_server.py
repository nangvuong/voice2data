"""
TTS HTTP Server - REST API & OpenAI-compatible Speech Server.
Cung cấp các endpoint HTTP cho Text-to-Speech:
1. POST /v1/audio/speech        (OpenAI standard format)
2. POST /api/tts                (JSON request: text, engine, speed, format)
3. GET  /api/tts                (Query params: text, engine, speed, format)
4. GET  /health                 (Health check & stats)
5. GET  /models                 (Danh sách model có sẵn)
"""

import argparse
import base64
import io
import logging
import os
import sys

from flask import Flask, Response, jsonify, request, send_file
from flask_cors import CORS

# Đảm bảo import được module trong repo
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config
from services.engines.tts_engine import AudioResult, TTSError, tts_engine

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("tts_server")

app = Flask("tts_server")
CORS(app)

MIME_TYPES = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "opus": "audio/ogg; codecs=opus",
}


@app.route("/health", methods=["GET"])
def health_check():
    """Kiểm tra tình trạng hoạt động của TTS Server."""
    model_dir = getattr(config, "TTS_MODEL_DIR", "/mnt/data/vits-piper-vi_VN-vais1000-medium")
    onnx_file = os.path.join(model_dir, "vi_VN-vais1000-medium.onnx")
    return jsonify({
        "status": "healthy",
        "service": "Vietnamese TTS Server",
        "model_dir": model_dir,
        "model_exists": os.path.exists(onnx_file),
        "available_engines": ["sherpa", "piper"],
        "default_engine": getattr(config, "TTS_ENGINE", "sherpa"),
    })


@app.route("/models", methods=["GET"])
def list_models():
    """Danh sách các model TTS khả dụng."""
    return jsonify({
        "data": [
            {
                "id": "vits-piper-vi_VN-vais1000-medium",
                "object": "model",
                "language": "vi_VN",
                "sample_rate": 22050,
                "speakers": 1,
                "voices": ["vais1000", "default"],
            }
        ]
    })


@app.route("/v1/audio/speech", methods=["POST"])
def openai_speech():
    """
    OpenAI-compatible speech endpoint.
    Format payload:
    {
        "model": "vits-piper-vi_VN-vais1000-medium",
        "input": "Nội dung cần chuyển thành giọng nói",
        "voice": "vais1000",
        "response_format": "mp3" | "wav" | "ogg",
        "speed": 1.0
    }
    """
    data = request.get_json(silent=True) or {}
    text = data.get("input") or data.get("text")
    if not text:
        return jsonify({"error": {"message": "Field 'input' không được để trống.", "type": "invalid_request_error"}}), 400

    resp_format = data.get("response_format", "mp3").lower()
    if resp_format not in MIME_TYPES:
        resp_format = "mp3"

    speed = float(data.get("speed", 1.0))
    engine_name = data.get("engine", getattr(config, "TTS_ENGINE", "sherpa"))

    try:
        audio_result = tts_engine.synthesize_sync(
            text=text,
            speed=speed,
            engine=engine_name,
        )

        if resp_format == "wav":
            audio_bytes = audio_result.to_wav_bytes()
        else:
            # Lưu tạm để convert qua ffmpeg
            tmp_wav = os.path.join(getattr(config, "TMP_DIR", "tmp"), f"temp_{os.getpid()}_{id(audio_result)}.wav")
            tmp_out = os.path.join(getattr(config, "TMP_DIR", "tmp"), f"temp_{os.getpid()}_{id(audio_result)}.{resp_format}")
            try:
                audio_result.save_wav(tmp_wav)
                audio_result.export(tmp_out, format=resp_format)
                with open(tmp_out, "rb") as f:
                    audio_bytes = f.read()
            finally:
                if os.path.exists(tmp_wav):
                    os.remove(tmp_wav)
                if os.path.exists(tmp_out):
                    os.remove(tmp_out)

        return Response(
            audio_bytes,
            mimetype=MIME_TYPES[resp_format],
            headers={
                "Content-Disposition": f"attachment; filename=speech.{resp_format}",
                "X-Audio-Duration": str(audio_result.duration_sec),
                "X-Inference-Time": str(audio_result.elapsed_sec),
                "X-RTF": str(audio_result.rtf),
            }
        )
    except Exception as e:
        logger.exception("Lỗi khi xử lý OpenAI TTS: %s", e)
        return jsonify({"error": {"message": str(e), "type": "server_error"}}), 500


@app.route("/api/tts", methods=["GET", "POST"])
def synthesize_api():
    """
    Endpoint đa năng xử lý cả GET và POST.
    POST JSON: {"text": "...", "engine": "sherpa", "speed": 1.0, "format": "wav", "return_base64": false}
    GET Query: ?text=...&engine=sherpa&speed=1.0&format=wav
    """
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        text = data.get("text") or data.get("input")
        engine_name = data.get("engine", getattr(config, "TTS_ENGINE", "sherpa"))
        speed = float(data.get("speed", 1.0))
        audio_format = data.get("format", "wav").lower()
        return_base64 = bool(data.get("return_base64", False))
    else:
        text = request.args.get("text", "")
        engine_name = request.args.get("engine", getattr(config, "TTS_ENGINE", "sherpa"))
        speed = float(request.args.get("speed", 1.0))
        audio_format = request.args.get("format", "wav").lower()
        return_base64 = request.args.get("return_base64", "0") in ("1", "true", "yes")

    if not text:
        return jsonify({"success": False, "error": "Tham số 'text' không được để trống"}), 400

    if audio_format not in MIME_TYPES:
        audio_format = "wav"

    try:
        audio_result = tts_engine.synthesize_sync(
            text=text,
            speed=speed,
            engine=engine_name,
        )

        if audio_format == "wav":
            audio_bytes = audio_result.to_wav_bytes()
        else:
            tmp_wav = os.path.join(getattr(config, "TMP_DIR", "tmp"), f"temp_{os.getpid()}_{id(audio_result)}.wav")
            tmp_out = os.path.join(getattr(config, "TMP_DIR", "tmp"), f"temp_{os.getpid()}_{id(audio_result)}.{audio_format}")
            try:
                audio_result.save_wav(tmp_wav)
                audio_result.export(tmp_out, format=audio_format)
                with open(tmp_out, "rb") as f:
                    audio_bytes = f.read()
            finally:
                if os.path.exists(tmp_wav):
                    os.remove(tmp_wav)
                if os.path.exists(tmp_out):
                    os.remove(tmp_out)

        if return_base64:
            b64_audio = base64.b64encode(audio_bytes).decode("utf-8")
            return jsonify({
                "success": True,
                "format": audio_format,
                "duration_sec": round(audio_result.duration_sec, 3),
                "elapsed_sec": round(audio_result.elapsed_sec, 3),
                "rtf": round(audio_result.rtf, 3),
                "engine": audio_result.engine_name,
                "audio_base64": b64_audio,
            })

        return Response(
            audio_bytes,
            mimetype=MIME_TYPES[audio_format],
            headers={
                "Content-Disposition": f"inline; filename=tts_output.{audio_format}",
                "X-Audio-Duration": str(audio_result.duration_sec),
                "X-Inference-Time": str(audio_result.elapsed_sec),
                "X-RTF": str(audio_result.rtf),
            }
        )
    except Exception as e:
        logger.exception("Lỗi khi xử lý TTS: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


def main():
    parser = argparse.ArgumentParser(description="Vietnamese TTS REST API Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=getattr(config, "TTS_SERVER_PORT", 8082), help="Port (default: 8082)")
    parser.add_argument("--engine", default="sherpa", choices=["sherpa", "piper"], help="Default engine")
    args = parser.parse_args()

    logger.info("Khởi động TTS Server tại http://%s:%d với default engine: %s", args.host, args.port, args.engine)
    # Warmup default engine
    try:
        tts_engine.synthesize_sync("Xin chào", engine=args.engine)
    except Exception as e:
        logger.warning("Không thể preload engine '%s': %s", args.engine, e)

    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
