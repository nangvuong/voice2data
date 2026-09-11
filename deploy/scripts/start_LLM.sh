#!/usr/bin/env bash
# ==============================================================================
# AgriLog AI - Script khởi động Local LLM Server trên Orange Pi 5 Plus (RK3588)
# ==============================================================================
# Sử dụng:
#   ./start_LLM.sh            -> Chạy Qwen3.5-2B trên NPU RK3588 (Mặc định, chuẩn VietGAP)
#   ./start_LLM.sh 2b         -> Chạy Qwen3.5-2B trên NPU RK3588 (2.8 GB RAM)
#   ./start_LLM.sh 4b         -> Chạy Qwen3-4B trên NPU RK3588 (4.6 GB RAM)
#   ./start_LLM.sh 0.6b       -> Chạy Qwen3-0.6B trên NPU RK3588 (0.9 GB RAM)
#   ./start_LLM.sh cpu        -> Chạy Qwen3.5-2B trên CPU (llama-server với KV Cache Prefix)
# ==============================================================================

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="${PROJECT_ROOT}/.venv/bin/python3"
DEFAULT_PORT=8080

MODEL_ARG="${1:-2b}"
PORT="${2:-$DEFAULT_PORT}"

echo "=============================================================================="
echo "          AGRILOG AI - KHỞI ĐỘNG LOCAL LLM SERVER (PORT: ${PORT})"
echo "=============================================================================="

# Dọn dẹp tiến trình cũ trên PORT
echo "🧹 Đang giải phóng cổng ${PORT}..."
fuser -k "${PORT}/tcp" 2>/dev/null || true
sleep 1

case "${MODEL_ARG}" in
    "dual"|"both"|"all")
        RKLLM_06B="/mnt/data/qwen3-0.6b_w8a8_rk3588.rkllm"
        RKLLM_2B="/mnt/data/Qwen3.5-2B_RK3588_w8a8.rkllm"
        echo "🚀 Khởi động DUAL RKLLM: Qwen3-0.6B (Port 8081) + Qwen3.5-2B (Port 8080)..."
        fuser -k 8081/tcp 2>/dev/null || true
        cd "${PROJECT_ROOT}/rkllm_server"
        "${VENV_PYTHON}" flask_server.py --rkllm_model_path "${RKLLM_06B}" --target_platform rk3588 --port 8081 &
        sleep 2
        exec "${VENV_PYTHON}" flask_server.py --rkllm_model_path "${RKLLM_2B}" --target_platform rk3588 --port 8080
        ;;

    "cpu"|"gguf"|"llama")
        GGUF_PATH="/mnt/data/Qwen3.5-2B-Q4_K_M.gguf"
        if [ ! -f "${GGUF_PATH}" ]; then
            echo "❌ Không tìm thấy ${GGUF_PATH}"
            exit 1
        fi
        echo "🚀 Khởi động Qwen3.5-2B (GGUF) trên 4 nhân CPU A76 với KV CACHE PREFIX REUSE..."
        echo "⚡ Tái sử dụng KV Cache từ RAM - Phản hồi cực nhanh ~9s - 17s"
        exec taskset -c 4-7 /usr/local/bin/llama-server \
            -m "${GGUF_PATH}" \
            --host 0.0.0.0 \
            --port "${PORT}" \
            -t 8 \
            -c 4096 \
            -np 1 \
            -fa on \
            --cache-prompt \
            --reasoning off \
            --reasoning-budget 0 \
            --log-disable
        ;;

    "4b"|"4B"|"qwen4b")
        RKLLM_PATH="/mnt/data/qwen3-4b-instruct-2507_w8a8_rk3588.rkllm"
        echo "🚀 Khởi động Qwen3-4B trên 3 Cores NPU RK3588 (W8A8)..."
        cd "${PROJECT_ROOT}/rkllm_server"
        exec "${VENV_PYTHON}" flask_server.py \
            --rkllm_model_path "${RKLLM_PATH}" \
            --target_platform rk3588 \
            --port "${PORT}"
        ;;

    "0.6b"|"0.6B"|"qwen0.6b")
        RKLLM_PATH="/mnt/data/qwen3-0.6b_w8a8_rk3588.rkllm"
        echo "🚀 Khởi động Qwen3-0.6B trên 3 Cores NPU RK3588..."
        cd "${PROJECT_ROOT}/rkllm_server"
        exec "${VENV_PYTHON}" flask_server.py \
            --rkllm_model_path "${RKLLM_PATH}" \
            --target_platform rk3588 \
            --port "${PORT}"
        ;;

    "2b"|"2B"|"qwen2b"|*)
        if [ -f "${MODEL_ARG}" ]; then
            RKLLM_PATH="${MODEL_ARG}"
        else
            RKLLM_PATH="/mnt/data/Qwen3.5-2B_RK3588_w8a8.rkllm"
        fi
        echo "🚀 Khởi động Qwen3.5-2B trên 3 Cores NPU RK3588 (Khuyên dùng cho VietGAP)..."
        echo "⚡ NPU Session In-Memory - 100% JSON chuẩn xác, giải phóng 100% CPU"
        cd "${PROJECT_ROOT}/rkllm_server"
        exec "${VENV_PYTHON}" flask_server.py \
            --rkllm_model_path "${RKLLM_PATH}" \
            --target_platform rk3588 \
            --port "${PORT}"
        ;;
esac