#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# run_personalization.sh
# Runs the PERSONALIZATION layer only — Step 7 (score_impact_prompts.py) then
# Step 8 (investigate_flagged_bills_v2.py) — decoupled from the nightly core
# pipeline. Both stages are delta-based, so this only does work for prompts /
# profiles / bills that actually changed.
#
# USE:
#   Run now (development, populate data immediately):
#       bash scripts/run_personalization.sh
#   Weekend schedule (see crontab line below) calls this same script.
#
# It health-gates vLLM exactly like run_daily_with_vllm_check.sh, so it is safe
# to run on its own without the nightly pipeline having started the model.
# ─────────────────────────────────────────────────────────────────────────────
PIPELINE_DIR="${LEG_PIPELINE_ROOT:?Set LEG_PIPELINE_ROOT}"
LOG_DIR="$PIPELINE_DIR/logs/cron"
mkdir -p "$LOG_DIR"
LOGFILE="$LOG_DIR/personalization_$(date +%Y%m%d_%H%M%S).log"

exec >> "$LOGFILE" 2>&1
echo "=== Personalization run started: $(date) ==="

# ── vLLM health gate (start if down, wait up to 6 min) ───────────────────────
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

source ~/.cosmos_env 2>/dev/null
cd "$PIPELINE_DIR"

# ── Step 7: impact-prompt + profile scoring (triage) ─────────────────────────
echo "[STEP 7] Impact-prompt scoring starting..."
if /bin/python3 score_impact_prompts.py; then
    echo "[STEP 7] Impact scoring OK"
else
    echo "[STEP 7] Impact scoring FAILED (non-fatal) — continuing to investigation"
fi

# ── Step 8: personalized investigation (deep dive over flagged bills) ────────
echo "[STEP 8] Investigation starting..."
if /bin/python3 investigate_flagged_bills_v2.py --min-rating 3; then
    echo "[STEP 8] Investigation OK"
else
    echo "[STEP 8] Investigation FAILED (non-fatal)"
fi

echo "=== Personalization run finished: $(date) ==="
