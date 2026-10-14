#!/bin/bash
# Script khởi chạy Flask Server cho Nông Ký App (hỗ trợ HTTPS)
cd "$(dirname "$0")"

PYTHON_BIN="/root/voice_to_data/.venv/bin/python"
if [ ! -f "$PYTHON_BIN" ]; then
    PYTHON_BIN="python3"
fi

PORT=${PORT:-6000}
SSL_FLAG="--ssl"
if [ "$1" == "--no-ssl" ]; then
    SSL_FLAG="--no-ssl"
fi

LOG_FILE="app.log"

{
echo "=========================================================="
echo "🌾 Khởi chạy Nông Ký - Voice to Text to Data (Flask App)"
echo "   - STT: sherpa-onnx + Silero VAD (Offline Vietnamese STT)"
echo "   - LLM: RKLLM Server trên NPU Rockchip RK3588 (Port 8080)"
echo "   - SSL: $SSL_FLAG (Bật HTTPS để cấp quyền Microphone trên trình duyệt)"
echo "   - Web UI: https://192.168.40.5:$PORT (hoặc https://localhost:$PORT)"
echo "=========================================================="
} | tee -a "$LOG_FILE"

exec $PYTHON_BIN app.py --port "$PORT" $SSL_FLAG
