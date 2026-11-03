#!/usr/bin/env python3
"""
run_legislation_daily.py
Replaces legislation_daily.ipynb for cron execution.
All cells from the notebook combined into a single runnable script.
"""
import os, subprocess, sys, json, logging, time
from datetime import datetime

PIPELINE_DIR    = __import__("os").environ.get('LEG_COMPONENTS_PIPELINE_RUN_LEGISLATION_DAILY_PY_PIPELINE_DIR_L10', "")
LOG_DIR         = os.path.join(PIPELINE_DIR, "logs")
RUN_ID          = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_PATH        = os.path.join(LOG_DIR, f"daily_{RUN_ID}.log")
ACTIVE_SESSIONS = [2176]
VLLM_URL        = "http://localhost:8000"

os.makedirs(LOG_DIR, exist_ok=True)
os.chdir(PIPELINE_DIR)
sys.path.insert(0, PIPELINE_DIR)

os.environ.setdefault("AZURE_STORAGE_ACCOUNT_NAME", __import__("os").environ.get('LEG_COMPONENTS_PIPELINE_RUN_LEGISLATION_DAILY_PY_VALUE_L21', ""))
os.environ.setdefault("AZURE_CONTAINER_BRONZE", "bronze")
os.environ.setdefault("AZURE_CONTAINER_SILVER", "silver")
os.environ.setdefault("AZURE_CONTAINER_GOLD",   "gold")
os.environ.setdefault("TIKTOKEN_CACHE_DIR",
    os.path.join(PIPELINE_DIR, "storage/cache"))
os.environ.setdefault("LANGFUSE_PUBLIC_KEY",  __import__("os").environ.get('LEG_COMPONENTS_PIPELINE_RUN_LEGISLATION_DAILY_PY_VALUE_L27', ""))
os.environ.setdefault("LANGFUSE_SECRET_KEY",  __import__("os").environ.get('LEG_COMPONENTS_PIPELINE_RUN_LEGISLATION_DAILY_PY_VALUE_L28', ""))
os.environ.setdefault("LANGFUSE_HOST",        "https://us.cloud.langfuse.com")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)


def abort(msg):
    log.error(f"ABORT: {msg}")
    sys.exit(1)


log.info("=== LEGISLATION DAILY PIPELINE ===")
log.info(f"Run ID : {RUN_ID}")
log.info(f"Log    : {LOG_PATH}")

# ── vLLM health check ─────────────────────────────────────────────────────────
import requests as req


def check_vllm():
    try:
        return req.get(f"{VLLM_URL}/health", timeout=5).status_code == 200
    except Exception:
        return False


if check_vllm():
    log.info("vLLM: healthy")
else:
    log.info("vLLM not responding — starting...")
    subprocess.Popen(
        ["bash", "scripts/start_vllm.sh"],
        stdout=open(os.path.join(LOG_DIR, f"vllm_{RUN_ID}.log"), "w"),
        stderr=subprocess.STDOUT,
        cwd=PIPELINE_DIR,
    )
    log.info("Waiting 90s for vLLM to load...")
    time.sleep(90)
    if check_vllm():
        log.info("vLLM: started successfully")
    else:
        abort("vLLM failed to start")

# ── Step 1: Ingestion ─────────────────────────────────────────────────────────
log.info("=== PIPELINE A: TEXT INTELLIGENCE ===")
log.info("Step 1: Ingestion")

with open(LOG_PATH, "a") as lf:
    r = subprocess.run(
        ["/bin/python3", "-c", f"""
import sys, logging
sys.path.insert(0, '{PIPELINE_DIR}')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
from utils.config_loader import load_config
from stages.ingestion import run
cfg = load_config('configs/ingestion.yaml')
result = run({{'ingestion': cfg}})
print(f"Ingestion: {{result['status']}} | new/updated: {{result.get('output_count', 0):,}}")
sys.exit(0 if result['status'] == 'SUCCESS' else 1)
"""],
        cwd=PIPELINE_DIR, stdout=lf, stderr=lf, text=True,
    )

if r.returncode != 0:
    abort("Ingestion failed — check log")
log.info("Step 1: Ingestion OK")

# ── Step 2: Change detection ──────────────────────────────────────────────────
log.info("Step 2: Change detection")
changed_bill_ids = []
try:
    import pandas as pd
    from services.azure_blob_service import BlobClient, TIER_BRONZE, TIER_SILVER
    b = BlobClient()
    for session_id in ACTIVE_SESSIONS:
        manifest_path = f"legislation/session={session_id}/_manifest.parquet"
        features_path = f"legislation/features_llama31_1200/session={session_id}/_features.parquet"
        if not b.blob_exists(TIER_BRONZE, manifest_path):
            log.info(f"  Session {session_id}: no manifest — skipping")
            continue
        manifest = b.read_parquet(TIER_BRONZE, manifest_path)
        manifest["bill_id"] = manifest["bill_id"].astype(str)
        if not b.blob_exists(TIER_SILVER, features_path):
            ids = manifest["bill_id"].tolist()
            log.info(f"  Session {session_id}: no features — {len(ids):,} bills need extraction")
            changed_bill_ids.extend(ids)
            continue
        features = b.read_parquet(TIER_SILVER, features_path)
        features["bill_id"] = features["bill_id"].astype(str)
        extracted_ids = set(features["bill_id"].unique())
        manifest_ids  = set(manifest["bill_id"].unique())
        new_ids       = manifest_ids - extracted_ids
        updated_ids   = set()
        if "last_changed_at" in manifest.columns and "extracted_at" in features.columns:
            feat_latest = features.groupby("bill_id")["extracted_at"].max().reset_index()
            merged = manifest.merge(feat_latest, on="bill_id", how="inner")
            merged["last_changed_at"] = pd.to_datetime(merged["last_changed_at"], errors="coerce")
            merged["extracted_at"]    = pd.to_datetime(merged["extracted_at"],    errors="coerce")
            updated_ids = set(merged[merged["last_changed_at"] > merged["extracted_at"]]["bill_id"])
        thin_ids = set()
        if "prompt_tokens" in features.columns and "text_length" in manifest.columns:
            thin_bills = set(features[features["prompt_tokens"] < 800]["bill_id"])
            if thin_bills:
                m_thin = manifest[
                    manifest["bill_id"].isin(thin_bills) &
                    (manifest["text_length"].fillna(0) > 2000)
                ]
                thin_ids = set(m_thin["bill_id"].tolist())
        session_changed = new_ids | updated_ids | thin_ids
        log.info(f"  Session {session_id}: {len(new_ids)} new, {len(updated_ids)} updated, {len(thin_ids)} thin")
        changed_bill_ids.extend(list(session_changed))
except Exception as e:
    log.warning(f"  Change detection failed ({e}) — skipping re-extraction")

changed_bill_ids = list(set(changed_bill_ids))
log.info(f"Bills requiring re-extraction: {len(changed_bill_ids)}")

# ── Step 3: Re-extraction chain ───────────────────────────────────────────────
log.info("Step 3: Incremental re-extraction")
if not changed_bill_ids:
    log.info("No text changes — skipping re-extraction")
else:
    ids_path = "/tmp/changed_bill_ids.json"
    with open(ids_path, "w") as f:
        json.dump(changed_bill_ids, f)

    stages = [
        ("extractor",    "configs/extractor_llama31_1200.yaml",  "stages.extractor"),
        ("synthesizer",  "configs/synthesizer.yaml",             "stages.synthesizer"),
        ("standardizer", "configs/standardizer.yaml",            "stages.standardizer"),
        ("embedder",     "configs/embedder.yaml",                "stages.embedder"),
    ]
    extraction_failed = False
    for stage_name, config_path, module in stages:
        has_cfg = config_path and os.path.exists(os.path.join(PIPELINE_DIR, config_path))
        concurrency_line = "cfg['concurrency'] = 12\n" if stage_name == "extractor" else ""
        if has_cfg:
            cfg_block = f"""
cfg = load_config('{config_path}')
cfg['bill_ids'] = changed_ids
cfg['sessions'] = {ACTIVE_SESSIONS}
cfg['mode'] = 'live'
{concurrency_line}"""
        else:
            cfg_block = f"cfg = {{'sessions': {ACTIVE_SESSIONS}, 'bill_ids': changed_ids}}\n"

        script = f"""
import sys, json, logging
sys.path.insert(0, '{PIPELINE_DIR}')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
changed_ids = json.load(open('{ids_path}'))
from utils.config_loader import load_config
from {module} import run
{cfg_block}
result = run(cfg)
print(f"{stage_name}: {{result['status']}} — {{result.get('output_count', 0)}} records")
sys.exit(0 if result['status'] in ('SUCCESS', 'SKIPPED') else 1)
"""
        with open(LOG_PATH, "a") as lf:
            r = subprocess.run(
                ["/bin/python3", "-c", script],
                cwd=PIPELINE_DIR, stdout=lf, stderr=lf, text=True,
            )
        if r.returncode == 0:
            log.info(f"  OK: {stage_name}")
        else:
            log.error(f"  FAILED: {stage_name}")
            extraction_failed = True
            break

    if os.path.exists(ids_path):
        os.remove(ids_path)
    if extraction_failed:
        log.warning("Re-extraction chain failed — Pipeline B will still run")

# ── Step 4: Diffs ─────────────────────────────────────────────────────────────
log.info("Step 4: Build bill version diffs")
diffs_script = os.path.join(PIPELINE_DIR, "build_diffs.py")
if os.path.exists(diffs_script):
    with open(LOG_PATH, "a") as lf:
        r = subprocess.run(
            ["/bin/python3", "build_diffs.py", "--upload"]
            + ["--session"] + [str(s) for s in ACTIVE_SESSIONS],
            cwd=PIPELINE_DIR, stdout=lf, stderr=lf, text=True,
        )
    log.info("Step 4: Diffs " + ("OK" if r.returncode == 0 else "FAILED (non-fatal)"))
else:
    log.info("Step 4: build_diffs.py not found — skipping")

# ── Step 5: Predictions ───────────────────────────────────────────────────────
log.info("=== PIPELINE B: PREDICTIONS ===")
log.info("Step 5: run_daily.py")
with open(LOG_PATH, "a") as lf:
    r = subprocess.run(
        ["/bin/python3", "run_daily.py"],
        cwd=PIPELINE_DIR, stdout=lf, stderr=lf, text=True,
    )
log.info("Step 5: Predictions " + ("OK" if r.returncode == 0 else "FAILED (non-fatal)"))

# ── Step 6: Probability trail ─────────────────────────────────────────────────
log.info("Step 6: Incremental probability trail update")
import pandas as pd
trail_script = os.path.join(PIPELINE_DIR, "build_probability_trail.py")
preds_path   = os.path.join(PIPELINE_DIR, "prediction/outputs/IL/stage2_test_predictions.csv")
if not os.path.exists(trail_script):
    log.info("Step 6: build_probability_trail.py not found — skipping")
elif not os.path.exists(preds_path):
    log.info("Step 6: No predictions file — skipping")
else:
    try:
        preds_df = pd.read_csv(preds_path)
        active_preds = preds_df[preds_df["bill_year"].isin([2025, 2026])] if "bill_year" in preds_df.columns else preds_df
        trail_bill_ids = active_preds["bill_id"].dropna().astype(int).tolist()
        log.info(f"  Trail: updating {len(trail_bill_ids)} bills")
    except Exception as e:
        log.warning(f"  Trail: could not read predictions ({e}) — skipping")
        trail_bill_ids = []
    if trail_bill_ids:
        with open(LOG_PATH, "a") as lf:
            r = subprocess.run(
                ["/bin/python3", trail_script, "--upload", "--bills"]
                + [str(x) for x in trail_bill_ids],
                cwd=PIPELINE_DIR, stdout=lf, stderr=lf, text=True,
                timeout=600,
            )
        log.info("Step 6: Trail " + ("OK" if r.returncode == 0 else "FAILED (non-fatal)"))
    else:
        log.info("Step 6: No active bills to update — skipping")

# [DISABLED-STEP7] # ── Step 7: Impact prompt scoring ──────────────────────────────────────────────
# [DISABLED-STEP7] log.info("Step 7: Impact prompt scoring (user custom prompts)")
# [DISABLED-STEP7] scoring_script = os.path.join(PIPELINE_DIR, "score_impact_prompts.py")
# [DISABLED-STEP7] if os.path.exists(scoring_script):
# [DISABLED-STEP7]     with open(LOG_PATH, "a") as lf:
# [DISABLED-STEP7]         r = subprocess.run(
# [DISABLED-STEP7]             ["/bin/python3", scoring_script],
# [DISABLED-STEP7]             cwd=PIPELINE_DIR, stdout=lf, stderr=lf, text=True,
# [DISABLED-STEP7]             timeout=7200,
# [DISABLED-STEP7]         )
# [DISABLED-STEP7]     log.info("Step 7: Impact scoring " + ("OK" if r.returncode == 0 else "FAILED (non-fatal)"))
# [DISABLED-STEP7] else:
# [DISABLED-STEP7]     log.info("Step 7: score_impact_prompts.py not found — skipping")

log.info("=== DAILY PIPELINE COMPLETE ===")
log.info(f"Run ID  : {RUN_ID}")
log.info(f"Changed : {len(changed_bill_ids)} bills re-extracted")

# ── Stamp pipeline_log.parquet so the health monitor sees this run ────────────
try:
    import pandas as _pd
    from datetime import datetime as _dt, timezone as _tz
    import socket as _socket, getpass as _getpass, sys as _sys
    _log_parquet = os.path.join(LOG_DIR, "pipeline_log.parquet")
    _now = _dt.now(_tz.utc)
    _row = {
        "workflow_name": "legislation_pipeline", "pipeline_version": "daily",
        "run_id": RUN_ID, "script_name": "PIPELINE", "script_order": 9999,
        "status": "PIPELINE_END", "is_success": True, "failure_stage": None,
        "event_time": _now, "start_time": _now, "end_time": _now,
        "duration_seconds": 0.0, "logged_at": _now,
        "doc_count": None, "time_per_doc": None, "input_count": None,
        "output_count": None, "rows_written": None, "skip_reason": None,
        "input_file": None, "output_file": None, "input_file_exists": None,
        "output_file_written": None, "error_type": None, "error_message": None,
        "traceback": None,
        "warning_flags": json.dumps({"changed_bills": len(changed_bill_ids)}),
        "hostname": _socket.gethostname(), "user": _getpass.getuser(),
        "python_version": f"{_sys.version_info.major}.{_sys.version_info.minor}.{_sys.version_info.micro}",
        "working_directory": PIPELINE_DIR,
    }
    if os.path.exists(_log_parquet):
        _combined = _pd.concat([_pd.read_parquet(_log_parquet), _pd.DataFrame([_row])], ignore_index=True)
    else:
        _combined = _pd.DataFrame([_row])
    _combined.to_parquet(_log_parquet, index=False)
    log.info(f"Stamped pipeline_log.parquet with success row (now {len(_combined)} rows)")
except Exception as _e:
    log.warning(f"Could not stamp pipeline_log.parquet: {_e}")

# [DISABLED-STEP7] # ── Step 7: Impact-prompt scoring (appended) ─────────────────────────────
# [DISABLED-STEP7] # Scores user impact prompts x active bills via local vLLM.
# [DISABLED-STEP7] # Self-contained + non-fatal: must never crash or fail the daily run.
# [DISABLED-STEP7] try:
# [DISABLED-STEP7]     import os as _os, sys as _sys, subprocess as _sp
# [DISABLED-STEP7]     _pdir = "<deployment-location>"
# [DISABLED-STEP7]     _scorer = _os.path.join(_pdir, "score_impact_prompts.py")
# [DISABLED-STEP7]     if _os.path.exists(_scorer):
# [DISABLED-STEP7]         print("[STEP 7] Impact-prompt scoring starting...")
# [DISABLED-STEP7]         _r = _sp.run([_sys.executable, _scorer], cwd=_pdir, timeout=4*3600)
# [DISABLED-STEP7]         print(f"[STEP 7] Impact scoring {'OK' if _r.returncode == 0 else 'FAILED (non-fatal)'}")
# [DISABLED-STEP7]     else:
# [DISABLED-STEP7]         print("[STEP 7] score_impact_prompts.py not found — skipping")
# [DISABLED-STEP7] except Exception as _e:
# [DISABLED-STEP7]     print(f"[STEP 7] Impact scoring errored (non-fatal): {_e}")
# [DISABLED-STEP7] # ── End Step 7 ───────────────────────────────────────────────────────────
