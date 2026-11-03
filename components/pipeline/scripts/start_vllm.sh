#!/usr/bin/env bash
# Start vLLM serving LLaMA 3.1 8B AWQ on the AITS L4 GPU.
#
# Run from the project root:
#   bash scripts/start_vllm.sh
#
# Background:
#   nohup bash scripts/start_vllm.sh > logs/vllm.log 2>&1 &
#   echo $! > logs/vllm.pid
#
# Stop:
#   kill $(cat logs/vllm.pid)

set -euo pipefail

LOG_DIR="logs"
mkdir -p "$LOG_DIR"

echo "Starting vLLM at $(date)"
echo "GPU before launch:"
nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader

MODEL="${VLLM_MODEL:-<deployment-location>"
SERVED_NAME="${VLLM_SERVED_NAME:-llama31-8b-awq}"
PORT="${VLLM_PORT:-8000}"

echo "Model:        $MODEL"
echo "Served as:    $SERVED_NAME"
echo "Port:         $PORT"

exec <deployment-location> serve "$MODEL" \
    --served-model-name "$SERVED_NAME" \
    --quantization awq \
    --gpu-memory-utilization "${VLLM_GPU_FRAC:-0.35}" \
    --max-model-len 8192 \
    --port "$PORT"
