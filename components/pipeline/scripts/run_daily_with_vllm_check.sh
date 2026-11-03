#!/bin/bash
PIPELINE_DIR="${LEG_PIPELINE_ROOT:?Set LEG_PIPELINE_ROOT}"
LOG_DIR="$PIPELINE_DIR/logs/cron"
mkdir -p "$LOG_DIR"
LOGFILE="$LOG_DIR/run_$(date +%Y%m%d_%H%M%S).log"

exec >> "$LOGFILE" 2>&1
echo "=== Pipeline cron started: $(date) ==="

# Check vLLM, start if down
if ! curl -sf http://localhost:8000/health > /dev/null 2>&1; then
    echo "vLLM is down — starting..."
    mkdir -p "$PIPELINE_DIR/logs/vllm"
    nohup bash "$PIPELINE_DIR/scripts/start_vllm.sh" \
        > "$PIPELINE_DIR/logs/vllm/vllm_restart_$(date +%Y%m%d_%H%M%S).log" 2>&1 &
    for i in {1..24}; do
        sleep 15
        if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
            echo "vLLM healthy after $((i * 15))s"; break
        fi
        if [ $i -eq 24 ]; then
            echo "ERROR: vLLM failed to start. Aborting."; exit 1
        fi
    done
else
    echo "vLLM already healthy."
fi

# Run full pipeline with system Python (has all pipeline dependencies)
source ~/.cosmos_env 2>/dev/null
echo "Starting legislation pipeline..."
cd "$PIPELINE_DIR"
/bin/python3 run_legislation_daily.py

echo "=== Pipeline cron finished: $(date) ==="

# ── Personalization layer (Step 7 scoring + Step 8 investigation) ──
bash "$(dirname "$0")/run_personalization.sh" >> "$(dirname "$0")/../logs/cron/personalization_$(date +%Y%m%d_%H%M%S).log" 2>&1
