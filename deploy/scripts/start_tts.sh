#!/bin/bash
# Khởi động Vietnamese TTS REST API Server
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ -f "$SCRIPT_DIR/.venv/bin/python" ]; then
    PYTHON="$SCRIPT_DIR/.venv/bin/python"
else
    PYTHON="python3"
fi

HOST="${1:-0.0.0.0}"
PORT="${2:-8082}"
ENGINE="${3:-sherpa}"

echo "=========================================================="
echo "Khởi động TTS Server (model: vi_VN-vais1000-medium)"
echo "Host:    http://$HOST:$PORT"
echo "Engine:  $ENGINE"
echo "=========================================================="

exec "$PYTHON" "$SCRIPT_DIR/services/tts_server.py" --host "$HOST" --port "$PORT" --engine "$ENGINE"
