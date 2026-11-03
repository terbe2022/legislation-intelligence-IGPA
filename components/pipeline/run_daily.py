#!/usr/bin/env python3
"""
Legislation Pipeline — Daily Production Script
==============================================
Full incremental cycle:
  1. LegiScan hash check → download only changed sessions
  2. Identify bills with new actions (diff on action count)
  3. Update history_actions_labeled.csv  (harmonized labels — matches training)
  4. Rebuild history_actions_aggregated.csv for changed bills
  5. Stage 2: re-score changed bills → stage2_test_predictions.csv
  6. Stage 3: refresh legislator profiles → re-score bills with new roll calls
  7. Stage 1: score new bills (auto-activates when model file is present)
  8. Build integration snapshot → upload to Azure gold blob as parquet
  9. Save dataset hashes so next run only downloads what changed

Usage:
    python run_daily.py
    python run_daily.py --force-refresh     # re-download all sessions regardless of hash
    python run_daily.py --force-rescore     # re-score all active bills
    python run_daily.py --dry-run           # hash check only, no downloads or writes
    python run_daily.py --state IL          # single-state override
    python run_daily.py --config /path/to/config.json

Prerequisites:
    - config.json (copy from config_template.json, fill in values)
    - models/ dir containing stage2_survival_model.joblib and stage3_vote_model.joblib
      (copy from your Windows pipeline_outputs/ after the consolidated pipeline runs)
    - stage3_legislator_features.csv (see SETUP NOTE below)

SETUP NOTE — one line to add to IL_Consolidated_Pipeline.ipynb after Cell 14:
    leg_profile_full = leg_profile.merge(ftm_profiles, on='people_id', how='left')
    leg_profile_full.to_csv(
        os.path.join(OUTPUT_DIR, 'stage3_legislator_features.csv'),
        index=False, encoding='utf-8-sig'
    )
    This saves the full legislator feature matrix so the incremental script can
    load pre-computed features and update only the vote-count columns each day.
"""

import argparse
import base64
import io
import json
import logging
import os
import re
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import requests

# Azure SDK — optional; upload step is skipped with a warning if absent
try:
    from azure.storage.blob import ContainerClient
    AZURE_SDK = True
except ImportError:
    AZURE_SDK = False

# scipy for Stage 1 sparse matrix concat
try:
    import scipy.sparse as sp
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False


# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# Label map is the authoritative source — matches IL_Consolidated_Pipeline
# training labels exactly. DO NOT modify without retraining Stage 2.
# ══════════════════════════════════════════════════════════════════════════════

SEMANTIC_STAGE_MAP = {
    "Filed / Introduced": [
        "filed with the clerk", "filed by", "first reading", "prefiled",
        "placed on calendar order of first reading", "introduced",
        "read in full a first time",
    ],
    "Referred / Assigned": [
        "referred to rules committee", "referred to assignments",
        "re-referred to rules committee", "re-referred to assignments",
        "assigned to", "re-referred to", "sent to rules", "sent to assignments",
    ],
    "Committee Referral": [
        "assigned to committee", "rules refers to", "assignments refer",
        "referred to standing committee", "referred to joint committee",
    ],
    "Committee Action": [
        "do pass", "do not pass", "failed in committee", "held in committee",
        "postponed", "re-referred from committee", "tabled in committee",
        "committee substitute", "reported from committee",
    ],
    "Floor Consideration": [
        "second reading", "third reading", "placed on calendar",
        "held on calendar", "removed from calendar", "called from calendar",
        "short debate", "standard debate", "concurrence",
    ],
    "Amendment Processing": [
        "amendment filed", "amendment adopted", "amendment rejected",
        "amendment tabled", "floor amendment", "amendment withdrawn",
    ],
    "Inter-Chamber Transfer": [
        "arrived in house", "arrived in senate", "returned to house",
        "returned to senate", "sent to house", "sent to senate",
        "passed house", "passed senate",
        "chief house sponsor", "chief senate sponsor",
    ],
    "Passed / Enrolled": [
        "enrolled", "sent to governor", "passed both houses",
        "sent to the governor", "approved by governor", "public act",
    ],
    "Failed / Rejected": [
        "session sine die", "failed", "vetoed", "motion failed",
        "bill dead", "bill withdrawn", "motion to reconsider failed",
    ],
    "Governor Certification / Return": [
        "governor approved", "governor vetoed", "returned by governor",
        "line item veto", "amendatory veto", "governor action",
    ],
    "Deadline / Extension": [
        "deadline established", "rule 2-10", "rule 19",
        "deadline extended", "third reading deadline",
    ],
    "Sponsorship Activity": [
        "added as co-sponsor", "added as chief co-sponsor",
        "removed as co-sponsor", "sponsor removed",
    ],
}

# Stages whose presence in an action sequence signals outcome leakage
LEAKAGE_STAGES = {
    "Passed / Enrolled",
    "Failed / Rejected",
    "Governor Certification / Return",
}

LEAKAGE_TERMS = [
    r"\bpassed\b", r"\bfailed\b", r"\bvetoed?\b", r"\badopted\b",
    r"\benrolled\b", r"\boverride\b", r"\bwithdrawn\b",
    r"governor approved", r"public act", r"session sine die",
    r"sent to the governor",
]

ACTIVE_STATUS_CODES   = {1, 2, 3}   # Introduced, Engrossed, Enrolled
TERMINAL_STATUS_CODES = {4, 5, 6, 7, 8}
PASS_TERMS = ["passed", "enacted", "adopted", "approved", "signed"]
SMOOTHING_ALPHA = 8.0

CSV_TABLES = ["bills", "history", "people", "rollcalls", "sponsors", "votes"]

# Pre-compiled patterns
_STAGE_PATTERNS = {
    stage: re.compile(
        "|".join(re.escape(p) for p in phrases), re.IGNORECASE
    )
    for stage, phrases in SEMANTIC_STAGE_MAP.items()
}
_STAGE_PRIORITY  = list(SEMANTIC_STAGE_MAP.keys())
_LEAKAGE_PATTERN = re.compile("|".join(LEAKAGE_TERMS), re.IGNORECASE)
_VC_PATTERN      = re.compile(r"\b\d{3}-\d{3}-\d{3}\b")  # vote counts embedded in action text

LEGISCAN_BASE = "https://api.legiscan.com/"

# Pipeline logger log directory — written to prediction/logs/ so the
# Job Execution Monitor finds it via EXTRA_LOG_FILES in config.py
_PIPELINE_LOG_DIR = Path(
    __import__("os").environ.get('LEG_COMPONENTS_PIPELINE_RUN_DAILY_PY__PIPELINE_LOG_DIR_L173', "")
)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

# ── Custom transformer required to unpickle Stage 2 model ──────────────────
try:
    import scipy.sparse as _sp
    from sklearn.base import BaseEstimator, TransformerMixin

    class SparseToDense(BaseEstimator, TransformerMixin):
        def fit(self, X, y=None):
            return self
        def transform(self, X):
            if _sp.issparse(X):
                return X.toarray()
            return X
except ImportError:
    pass


def _safe_int(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").astype("Int64")


def _sep_join(s: pd.Series) -> str:
    return " || ".join(s.astype(str))


def label_action(text: str) -> str:
    if pd.isna(text):
        return "Unlabeled"
    t = str(text).lower()
    for stage in _STAGE_PRIORITY:
        if _STAGE_PATTERNS[stage].search(t):
            return stage
    return "Unlabeled"


def compute_momentum(semantic_concat: str) -> str:
    if not semantic_concat:
        return "Stable"
    stages = [s.strip() for s in str(semantic_concat).split(" || ") if s.strip()]
    n = len(stages)
    if n < 4:
        return "Stable"
    pos = {
        "Floor Consideration", "Inter-Chamber Transfer",
        "Committee Action", "Passed / Enrolled",
    }
    neg = {"Failed / Rejected", "Session Closure"}
    late  = stages[max(0, n - 3):]
    early = stages[max(0, n - 6):max(0, n - 3)]
    ls = sum(1 for s in late  if s in pos) - sum(1 for s in late  if s in neg)
    es = sum(1 for s in early if s in pos) - sum(1 for s in early if s in neg)
    d  = ls - es
    return "Gaining" if d >= 1 else ("Losing" if d <= -1 else "Stable")


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG & LOGGING
# ══════════════════════════════════════════════════════════════════════════════

def load_config(path: str) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            "Copy config_template.json → config.json and fill in your values."
        )
    with open(path) as f:
        cfg = json.load(f)

    required = ["legiscan.api_key", "states"]
    for key in required:
        parts = key.split(".")
        val = cfg
        for p in parts:
            val = val.get(p, None) if isinstance(val, dict) else None
        if not val or val == "YOUR_LEGISCAN_API_KEY" or "YOUR_SAS_TOKEN" in str(val):
            raise ValueError(
                f"Config key '{key}' is missing or still set to placeholder value.\n"
                f"Edit config.json and fill in the real value."
            )
    return cfg


def setup_logging(log_dir: str, run_id: str) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"run_{run_id}.log")
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path),
        ],
    )
    log = logging.getLogger("legislation_pipeline")
    log.info(f"Log file: {log_path}")
    return log


# ══════════════════════════════════════════════════════════════════════════════
# LEGISCAN API
# ══════════════════════════════════════════════════════════════════════════════

def api_call(api_key: str, op: str, params: dict = None, max_retries: int = 3) -> dict:
    p = {"key": api_key, "op": op}
    if params:
        p.update(params)
    for attempt in range(max_retries):
        try:
            r = requests.get(LEGISCAN_BASE, params=p, timeout=30)
            r.raise_for_status()
            data = r.json()
            if data.get("status") == "ERROR":
                msg = data.get("alert", {}).get("message", "Unknown error")
                raise ValueError(f"LegiScan API error [{op}]: {msg}")
            return data
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
    return {}


def download_session_zip(api_key: str, session_id: int, access_key: str) -> bytes:
    p = {
        "key": api_key, "op": "getDataset",
        "access_key": access_key, "id": session_id,
    }
    r = requests.get(LEGISCAN_BASE, params=p, timeout=120)
    r.raise_for_status()
    data = r.json()
    if data.get("status") == "ERROR":
        raise ValueError(f"getDataset error for session {session_id}: {data}")
    zip_b64 = data.get("dataset", {}).get("zip", "")
    if not zip_b64:
        raise ValueError(
            f"No zip data returned for session {session_id}. "
            "Check that your API key has dataset access."
        )
    return base64.b64decode(zip_b64)


# ══════════════════════════════════════════════════════════════════════════════
# JSON DATASET PARSER
# ══════════════════════════════════════════════════════════════════════════════

def _parse_json_dataset(extract_dir: str, session_id: int, session_name: str,
                        log: logging.Logger) -> dict:
    bill_files = sorted(Path(extract_dir).rglob("bill/*.json"))
    vote_files = sorted(Path(extract_dir).rglob("vote/*.json"))

    if not bill_files:
        raise RuntimeError(
            f"Neither bills.csv nor JSON bill files found in {extract_dir}. "
            "The LegiScan dataset format may have changed or the download was incomplete."
        )

    log.info(f"    Parsing {len(bill_files):,} bill JSON files + {len(vote_files):,} vote files...")

    bills_rows    = []
    history_rows  = []
    sponsors_rows = []
    rc_rows       = []
    people_dict   = {}

    for bf in bill_files:
        try:
            with open(bf) as f:
                raw = json.load(f)
            bill = raw.get("bill", raw)
            bid  = bill.get("bill_id")
            if not bid:
                continue

            bills_rows.append({
                "bill_id":         bid,
                "session_id":      session_id,
                "session":         session_name,
                "bill_number":     bill.get("bill_number", ""),
                "status":          bill.get("status", 0),
                "status_desc":     bill.get("status_desc", ""),
                "title":           bill.get("title", ""),
                "description":     bill.get("description", ""),
                "chamber":         bill.get("body", bill.get("chamber", "")),
                "last_action":     bill.get("last_action", ""),
                "last_action_date":bill.get("last_action_date", ""),
                "introduced_date": bill.get("status_date", ""),
            })

            for seq, h in enumerate(bill.get("history", []), 1):
                history_rows.append({
                    "bill_id":   bid,
                    "session_id":session_id,
                    "session":   session_name,
                    "date":      h.get("date", ""),
                    "action":    h.get("action", ""),
                    "chamber":   h.get("chamber", ""),
                    "sequence":  seq,
                })

            for sp in bill.get("sponsors", []):
                pid = sp.get("people_id")
                if not pid:
                    continue
                sponsors_rows.append({
                    "bill_id":   bid,
                    "people_id": pid,
                    "session":   session_name,
                    "session_id":session_id,
                    "position":  sp.get("sponsor_type", sp.get("sponsor_type_desc", "")),
                    "sequence":  sp.get("sponsor_order", 0),
                    "party":     sp.get("party", ""),
                })
                if pid not in people_dict:
                    people_dict[pid] = {
                        "people_id": pid,
                        "name":      sp.get("name", ""),
                        "party":     sp.get("party", ""),
                        "role":      sp.get("role", ""),
                        "session":   session_name,
                        "session_id":session_id,
                    }

            for v in bill.get("votes", []):
                rc_id = v.get("roll_call_id")
                if rc_id:
                    rc_rows.append({
                        "roll_call_id": rc_id,
                        "bill_id":      bid,
                        "session":      session_name,
                        "session_id":   session_id,
                        "date":         v.get("date", ""),
                        "desc":         v.get("desc", ""),
                        "yea":          v.get("yea", 0),
                        "nay":          v.get("nay", 0),
                        "passed":       v.get("passed", 0),
                        "chamber":      v.get("chamber", ""),
                    })

        except Exception as e:
            log.warning(f"    Skipping {bf.name}: {e}")
            continue

    vote_rows = []
    for vf in vote_files:
        try:
            with open(vf) as f:
                raw = json.load(f)
            rc = raw.get("roll_call", raw)
            rc_id = rc.get("roll_call_id")
            bid   = rc.get("bill_id")
            if not rc_id:
                continue
            for v in rc.get("votes", []):
                pid = v.get("people_id")
                if pid:
                    vote_rows.append({
                        "roll_call_id": rc_id,
                        "bill_id":      bid,
                        "people_id":    pid,
                        "vote_id":      v.get("vote_id", 0),
                        "vote_desc":    v.get("vote_desc", ""),
                        "session":      session_name,
                        "session_id":   session_id,
                    })
                    if pid not in people_dict:
                        people_dict[pid] = {
                            "people_id": pid,
                            "name":      v.get("name", ""),
                            "party":     v.get("party", ""),
                            "role":      "",
                            "session":   session_name,
                            "session_id":session_id,
                        }
        except Exception as e:
            log.warning(f"    Skipping vote file {vf.name}: {e}")
            continue

    tables = {}
    if bills_rows:
        tables["bills"]     = pd.DataFrame(bills_rows)
        log.info(f"    {'bills':12s}: {len(bills_rows):>8,} rows (JSON)")
    if history_rows:
        tables["history"]   = pd.DataFrame(history_rows)
        log.info(f"    {'history':12s}: {len(history_rows):>8,} rows (JSON)")
    if sponsors_rows:
        tables["sponsors"]  = pd.DataFrame(sponsors_rows)
        log.info(f"    {'sponsors':12s}: {len(sponsors_rows):>8,} rows (JSON)")
    if rc_rows:
        tables["rollcalls"] = pd.DataFrame(rc_rows).drop_duplicates("roll_call_id")
        log.info(f"    {'rollcalls':12s}: {len(tables['rollcalls']):>8,} rows (JSON)")
    if vote_rows:
        tables["votes"]     = pd.DataFrame(vote_rows)
        log.info(f"    {'votes':12s}: {len(vote_rows):>8,} rows (JSON)")
    if people_dict:
        tables["people"]    = pd.DataFrame(list(people_dict.values()))
        log.info(f"    {'people':12s}: {len(people_dict):>8,} rows (JSON)")

    if "bills" not in tables:
        raise RuntimeError(f"No bills parsed from JSON files in {extract_dir}")

    return tables


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — HASH CHECK & DOWNLOAD
# ══════════════════════════════════════════════════════════════════════════════

def load_hashes(output_dir: str) -> dict:
    p = os.path.join(output_dir, "legiscan_dataset_hashes.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {}


def save_hashes(output_dir: str, hashes: dict):
    p = os.path.join(output_dir, "legiscan_dataset_hashes.json")
    with open(p, "w") as f:
        json.dump(hashes, f, indent=2)


def check_and_download(
    config: dict, state: str, force_refresh: bool, dry_run: bool, log: logging.Logger
) -> list:
    api_key   = config["legiscan"]["api_key"]
    state_cfg = config["states"][state]
    output_dir = state_cfg["pipeline_output_dir"]
    download_dir = config.get(
        "download_dir",
        os.path.join(output_dir, ".downloads")
    )
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(download_dir, exist_ok=True)

    stored_hashes = load_hashes(output_dir)
    data = api_call(api_key, "getDatasetList", {"state": state})
    sessions = data.get("datasetlist", [])

    if not sessions:
        raise RuntimeError(
            f"getDatasetList returned no sessions for {state}. "
            "Check your API key and state abbreviation."
        )

    log.info(f"[{state}] LegiScan reports {len(sessions)} session(s)")
    active_id = state_cfg.get("active_session_id")
    results = []

    for sess in sessions:
        sid        = sess.get("session_id")
        name       = sess.get("session_name", str(sid))
        new_hash   = sess.get("dataset_hash", "")
        access_key = sess.get("access_key", "")
        stored     = stored_hashes.get(str(sid), "")
        changed    = (new_hash != stored) or force_refresh

        marker = " ← ACTIVE" if sid == active_id else ""
        log.info(
            f"[{state}]   Session {sid} ({name}): "
            f"{'CHANGED' if changed else 'unchanged'}{marker}"
        )

        if not changed:
            continue
        if dry_run:
            log.info(f"[{state}]   DRY RUN — would download session {sid}")
            continue

        log.info(f"[{state}]   Downloading session {sid}...")
        zip_bytes = download_session_zip(api_key, sid, access_key)
        log.info(f"[{state}]   Downloaded {len(zip_bytes) / 1024 / 1024:.1f} MB")

        safe_name  = re.sub(r"[^\w\-]", "_", name)
        extract_to = os.path.join(download_dir, f"{state}_{sid}_{safe_name}")
        os.makedirs(extract_to, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall(extract_to)

        bills_csv = list(Path(extract_to).rglob("bills.csv"))
        if bills_csv:
            csv_dir = str(bills_csv[0].parent)
            tables = {}
            for table in CSV_TABLES:
                fpath = os.path.join(csv_dir, f"{table}.csv")
                if os.path.exists(fpath):
                    df = pd.read_csv(fpath, low_memory=False)
                    df["session"]    = name
                    df["session_id"] = sid
                    tables[table]    = df
                    log.info(f"[{state}]     {table:12s}: {len(df):>8,} rows")
            if "bills" not in tables:
                raise RuntimeError(f"bills table missing for session {sid}")
        else:
            log.info(f"[{state}]   No CSVs found — parsing JSON bill files...")
            tables = _parse_json_dataset(extract_to, sid, name, log)

        results.append((
            {
                "session_id":  sid,
                "name":        name,
                "access_key":  access_key,
                "new_hash":    new_hash,
                "is_active":   (sid == active_id),
            },
            tables,
        ))
        log.info(f"[{state}]   Session {sid} ready")

    return results


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — IDENTIFY CHANGED BILLS
# ══════════════════════════════════════════════════════════════════════════════

def find_changed_bills(
    new_tables: dict, agg_path: str, force_rescore: bool, log: logging.Logger,
    include_terminal: bool = False,
) -> set:
    new_bills   = new_tables.get("bills",   pd.DataFrame()).copy()
    new_history = new_tables.get("history", pd.DataFrame()).copy()

    if new_bills.empty:
        log.warning("  Bills table is empty — nothing to process")
        return set()

    new_bills["bill_id"] = _safe_int(new_bills["bill_id"])
    new_bills["status"]  = (
        pd.to_numeric(new_bills.get("status", 0), errors="coerce").fillna(0).astype(int)
    )
    if include_terminal:
        active_ids = set(new_bills["bill_id"].dropna().astype(int))
        log.info(f"  ALL bills incl. terminal : {len(active_ids):,}")
    else:
        active_ids = set(
            new_bills[new_bills["status"].isin(ACTIVE_STATUS_CODES)]["bill_id"]
            .dropna().astype(int)
        )
        log.info(f"  Active bills in download : {len(active_ids):,}")

    if force_rescore or not os.path.exists(agg_path):
        log.info(f"  Rescoring all {len(active_ids):,} active bills (force or no prior data)")
        return active_ids

    stored = pd.read_csv(
        agg_path, low_memory=False, encoding="utf-8-sig", usecols=["bill_id", "n_actions"]
    )
    stored["bill_id"] = _safe_int(stored["bill_id"])
    stored_n = dict(
        zip(stored["bill_id"].dropna().astype(int), stored["n_actions"])
    )

    new_history["bill_id"] = _safe_int(new_history["bill_id"])
    new_n = (
        new_history.groupby("bill_id").size().to_dict()
        if not new_history.empty else {}
    )

    stored_ids  = set(stored_n.keys())
    new_active  = active_ids - stored_ids
    changed     = set(new_active)

    for bid in active_ids & stored_ids:
        if new_n.get(bid, 0) > stored_n.get(bid, 0):
            changed.add(bid)

    log.info(f"  Bills with new actions   : {len(changed) - len(new_active):,}")
    log.info(f"  Brand new active bills   : {len(new_active):,}")
    log.info(f"  Total bills to re-score  : {len(changed):,}")
    return changed


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 & 4 — HISTORY UPDATE
# ══════════════════════════════════════════════════════════════════════════════

def update_history(
    new_tables: dict,
    changed_ids: set,
    labeled_path: str,
    agg_path: str,
    log: logging.Logger,
) -> tuple:
    new_history   = new_tables.get("history",   pd.DataFrame()).copy()
    new_bills     = new_tables.get("bills",     pd.DataFrame()).copy()
    new_rollcalls = new_tables.get("rollcalls", pd.DataFrame()).copy()

    if new_history.empty:
        raise RuntimeError(
            "History table is empty in the downloaded data — aborting history update. "
            "Check the LegiScan session download."
        )

    new_history["bill_id"] = _safe_int(new_history["bill_id"])
    new_history["date"]    = pd.to_datetime(new_history["date"], errors="coerce")

    changed_history = new_history[new_history["bill_id"].isin(changed_ids)].copy()
    log.info(f"  New history rows for changed bills : {len(changed_history):,}")

    if os.path.exists(labeled_path):
        sz = os.path.getsize(labeled_path) // 1024 // 1024
        log.info(f"  Loading stored labeled history ({sz} MB)...")
        stored = pd.read_csv(labeled_path, low_memory=False, encoding="utf-8-sig")
        stored["bill_id"] = _safe_int(stored["bill_id"])
        stored = stored[~stored["bill_id"].isin(changed_ids)]
        log.info(f"  Stored rows after trimming changed bills : {len(stored):,}")
    else:
        stored = pd.DataFrame()

    changed_history["semantic_stage"]   = changed_history["action"].apply(label_action)
    changed_history["procedural_stage"] = changed_history["semantic_stage"]
    changed_history["activity_type"]    = "Unlabeled"
    if "sequence" not in changed_history.columns:
        changed_history["sequence"] = (
            changed_history.groupby("bill_id").cumcount() + 1
        )

    labeling_rate = (
        (changed_history["semantic_stage"] != "Unlabeled").sum()
        / max(len(changed_history), 1) * 100
    )
    if labeling_rate < 60:
        raise RuntimeError(
            f"Action labeling rate is only {labeling_rate:.1f}% — expected >60%. "
            "History table may be corrupt or action text format has changed."
        )
    log.info(f"  Labeling coverage : {labeling_rate:.1f}%")

    updated = (
        pd.concat([stored, changed_history], ignore_index=True)
        .sort_values(["bill_id", "date", "sequence"])
        .drop_duplicates(subset=["bill_id", "date", "sequence"])
        .reset_index(drop=True)
    )
    updated.to_csv(labeled_path, index=False, encoding="utf-8-sig")
    log.info(f"  History saved : {len(updated):,} rows → history_actions_labeled.csv")

    new_bills["bill_id"] = _safe_int(new_bills["bill_id"])
    new_bills["_status_lower"] = new_bills["status_desc"].fillna("").str.lower()
    new_bills["target_pass"] = (
        new_bills["_status_lower"]
        .str.contains("|".join(PASS_TERMS), regex=True)
        .astype(int)
    )
    SESSION_YEAR_MAP = {
        2176: 2025, 2020: 2023, 1801: 2021, 1630: 2019,
        1407: 2017, 1127: 2015, 112: 2013, 999: 2011, 21: 2009,
    }
    if not updated.empty and "date" in updated.columns and "bill_id" in updated.columns:
        hist_dates = (
            updated[updated["bill_id"].isin(changed_ids)]
            .groupby("bill_id")["date"].min()
            .reset_index().rename(columns={"date": "_hist_date"})
        )
        new_bills = new_bills.merge(hist_dates, on="bill_id", how="left")
        new_bills["bill_year"] = pd.to_datetime(
            new_bills["_hist_date"], errors="coerce"
        ).dt.year
    else:
        new_bills["bill_year"] = pd.NaT

    fallback = pd.to_datetime(
        new_bills.get("last_action_date", None), errors="coerce"
    ).dt.year
    new_bills["bill_year"] = new_bills["bill_year"].fillna(fallback)
    if "session_id" in new_bills.columns:
        new_bills["bill_year"] = new_bills["bill_year"].fillna(
            new_bills["session_id"].map(SESSION_YEAR_MAP)
        )
    new_bills["reached_vote"] = 0
    if not new_rollcalls.empty:
        new_rollcalls["bill_id"] = _safe_int(new_rollcalls["bill_id"])
        voted_ids = set(new_rollcalls["bill_id"].dropna().astype(int))
        new_bills.loc[new_bills["bill_id"].isin(voted_ids), "reached_vote"] = 1

    bill_number_col = "bill_number" if "bill_number" in new_bills.columns else None
    meta_cols = ["bill_id", "target_pass", "reached_vote", "bill_year"]
    if bill_number_col:
        meta_cols.insert(1, "bill_number")
    bill_meta = new_bills[meta_cols].copy()

    ch = updated[updated["bill_id"].isin(changed_ids)].copy()
    ch["_is_leakage"] = (
        ch["semantic_stage"].isin(LEAKAGE_STAGES)
        | ch["action"].fillna("").str.contains(_LEAKAGE_PATTERN, regex=True)
    )
    ch_clean = ch[~ch["_is_leakage"]].merge(bill_meta, on="bill_id", how="left")

    ch_clean["_clean_action"] = (
        ch_clean["action"].fillna("").str.replace(_VC_PATTERN, "", regex=True).str.strip()
    )

    new_agg = ch_clean.groupby("bill_id").agg(
        actions_concat       =("_clean_action",    _sep_join),
        semantic_concat      =("semantic_stage",   _sep_join),
        procedural_concat    =("procedural_stage", _sep_join),
        activity_concat      =("activity_type",    _sep_join),
        n_actions            =("_clean_action",    "count"),
        n_distinct_stages    =("semantic_stage",   "nunique"),
        has_committee_action =("semantic_stage",   lambda s: int(s.isin(["Committee Action"]).any())),
        has_floor_action     =("semantic_stage",   lambda s: int(s.isin(["Floor Consideration"]).any())),
        has_inter_chamber    =("semantic_stage",   lambda s: int(s.isin(["Inter-Chamber Transfer"]).any())),
        has_amendment        =("semantic_stage",   lambda s: int(s.isin(["Amendment Processing"]).any())),
        n_re_referred        =("action",           lambda s: int(s.str.lower().str.contains("re-referred", na=False).sum())),
    ).reset_index()
    new_agg = new_agg.merge(bill_meta, on="bill_id", how="left")

    if len(new_agg) == 0:
        raise RuntimeError(
            "Aggregated rebuild produced 0 rows. "
            "All changed bills may have been filtered as leakage — check action text."
        )

    if os.path.exists(agg_path):
        stored_agg = pd.read_csv(agg_path, low_memory=False, encoding="utf-8-sig")
        stored_agg["bill_id"] = _safe_int(stored_agg["bill_id"])
        stored_agg = stored_agg[~stored_agg["bill_id"].isin(changed_ids)]
        updated_agg = pd.concat([stored_agg, new_agg], ignore_index=True)
    else:
        updated_agg = new_agg

    updated_agg = (
        updated_agg.drop_duplicates(subset=["bill_id"]).reset_index(drop=True)
    )
    updated_agg.to_csv(agg_path, index=False, encoding="utf-8-sig")
    log.info(f"  Aggregated saved : {len(updated_agg):,} bills → history_actions_aggregated.csv")

    return updated, updated_agg


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5 — STAGE 2 RE-SCORING
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# [patch_run_daily_v5] training-format text
# Nightly scoring must build input text exactly as the model was trained.
# All construction lives in stage2_transformers.py; see patch_run_daily_v5.py.
# ══════════════════════════════════════════════════════════════════════════════
_NIGHTLY = {}   # filled in _run_body with tonight's downloaded bills table
_S2_LAB_COLS = ["bill_id", "date", "action", "sequence",
                "semantic_stage", "procedural_stage", "activity_type"]


def _s2_import():
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import stage2_transformers as s2t
    return s2t


def _s2_heads(bill_ids, log):
    """bill_id -> title/synopsis head, from tonight's download then raw extracts."""
    s2t = _s2_import()
    heads = {}
    b = _NIGHTLY.get("bills")
    if b is not None and {"bill_id", "title", "description"} <= set(b.columns):
        bb = b[["bill_id", "title", "description"]].copy()
        bb["bill_id"] = pd.to_numeric(bb["bill_id"], errors="coerce")
        bb = bb.dropna(subset=["bill_id"])
        bb["bill_id"] = bb["bill_id"].astype("int64")
        bb = bb[bb["bill_id"].isin(bill_ids)].drop_duplicates("bill_id")
        heads.update({int(i): s2t.head_text(t, d) for i, t, d in
                      zip(bb.bill_id, bb.title.fillna(""), bb.description.fillna(""))})
    missing = set(bill_ids) - set(heads)
    if missing:
        import glob as _glob
        for g in _glob.glob("storage/temp/ingestion/extract_*/IL/*/csv/bills.csv"):
            try:
                e = pd.read_csv(g, usecols=["bill_id", "title", "description"],
                                low_memory=False)
            except Exception:
                continue
            e["bill_id"] = pd.to_numeric(e["bill_id"], errors="coerce")
            e = e.dropna(subset=["bill_id"])
            e["bill_id"] = e["bill_id"].astype("int64")
            e = e[e["bill_id"].isin(missing)].drop_duplicates("bill_id")
            heads.update({int(i): s2t.head_text(t, d) for i, t, d in
                          zip(e.bill_id, e.title.fillna(""), e.description.fillna(""))})
    still = set(bill_ids) - set(heads)
    if still:
        log.warning("  Stage 2: %d bills have no title/synopsis; scored without a head",
                    len(still))
    return heads


def _s2_prepared(bill_ids, labeled_path=None, hist=None):
    s2t = _s2_import()
    if hist is None:
        hist = pd.read_csv(labeled_path, low_memory=False, encoding="utf-8-sig",
                           usecols=_S2_LAB_COLS)
    h = hist.copy()
    h["bill_id"] = pd.to_numeric(h["bill_id"], errors="coerce")
    h = h.dropna(subset=["bill_id"])
    h["bill_id"] = h["bill_id"].astype("int64")
    h = h[h["bill_id"].isin(bill_ids)]
    return s2t.prepare_actions(h)


def _s2_full_texts(bill_ids, agg_path, bundle, log):
    """bill_id -> full-history text in training format."""
    s2t = _s2_import()
    ids = {int(x) for x in bill_ids}
    lab = os.path.join(os.path.dirname(agg_path), "history_actions_labeled.csv")
    if not os.path.exists(lab):
        log.warning("  Stage 2: %s missing; falling back to legacy text", lab)
        return {}
    prep = _s2_prepared(ids, labeled_path=lab)
    heads = _s2_heads(ids, log) if s2t.model_needs_title(bundle) else None
    out = {}
    for bid, g in prep.groupby("bill_id", sort=False):
        t = s2t.full_history_text(g, heads.get(bid, "") if heads is not None else None)
        if t is not None:
            out[int(bid)] = t
    log.info("  Stage 2: training-format text for %d bills (title head: %s)",
             len(out), heads is not None)
    return out


def _s2_trail_rows(hist, bn_map, bundle, pipeline, log):
    """Trail rows for changed bills, in training format. Same schema as before."""
    s2t = _s2_import()
    ids = {int(x) for x in pd.to_numeric(hist["bill_id"], errors="coerce").dropna()}
    prep = _s2_prepared(ids, hist=hist)
    heads = _s2_heads(ids, log) if s2t.model_needs_title(bundle) else None
    recs = []
    for bid, g in prep.groupby("bill_id", sort=False):
        texts = s2t.bill_prefix_texts(g, heads.get(bid, "") if heads is not None else None)
        real, idx = 0, 0
        for (_, row), text in zip(g.iterrows(), texts):
            if not row["is_leak"]:
                real += 1
            if text is None:
                continue
            idx += 1
            act = "" if pd.isna(row["action"]) else str(row["action"])
            recs.append({
                "bill_id": int(bid),
                "bill_number": bn_map.get(bid, ""),
                "action_index": idx,
                "action_date": row["date"].strftime("%Y-%m-%d") if pd.notna(row["date"]) else "",
                "semantic_stage": row["semantic_stage"],
                "action_text": act[:300],
                "_text": text,
            })
            if real >= s2t.MAX_PREFIX_DEPTH and not row["is_leak"]:
                break
    if not recs:
        return []
    uniq = list(dict.fromkeys(r["_text"] for r in recs))
    probs = {}
    for i in range(0, len(uniq), 20000):
        chunk = uniq[i:i + 20000]
        for t, p in zip(chunk, pipeline.predict_proba(chunk)[:, 1]):
            probs[t] = float(p)
    for r in recs:
        r["y_prob"] = round(probs[r.pop("_text")], 6)
    log.info("  Trail: training-format rows for %d bills (title head: %s)",
             len({r['bill_id'] for r in recs}), heads is not None)
    return recs



def rescore_stage2(
    changed_ids: set,
    agg_path: str,
    model_path: str,
    preds_path: str,
    log: logging.Logger,
) -> pd.DataFrame:
    if not os.path.exists(model_path):
        raise FileNotFoundError(
            f"Stage 2 model not found: {model_path}\n"
            "Run IL_Consolidated_Pipeline.ipynb on your Windows machine first, "
            "then copy stage2_survival_model.joblib to the models/ dir on this server."
        )

    bundle   = joblib.load(model_path)
    pipeline = bundle["pipeline"]
    log.info(f"  Stage 2 model : {pipeline.named_steps['clf'].__class__.__name__}")

    agg = pd.read_csv(agg_path, low_memory=False, encoding="utf-8-sig")
    agg["bill_id"] = _safe_int(agg["bill_id"])

    to_score = agg[agg["bill_id"].isin(changed_ids)].copy()
    if to_score.empty:
        log.warning("  Stage 2: 0 bills matched in aggregated CSV — skipping scoring")
        return pd.DataFrame()

    _texts = _s2_full_texts(
        pd.to_numeric(to_score["bill_id"], errors="coerce").dropna().astype("int64"),
        agg_path, bundle, log)
    _legacy = (to_score["actions_concat"].fillna("") + " || " +
               to_score["semantic_concat"].fillna(""))
    to_score["combined_text"] = [
        _texts.get(int(b), l) if pd.notna(b) else l
        for b, l in zip(to_score["bill_id"], _legacy)
    ]
    _n_legacy = sum(1 for b in to_score["bill_id"] if pd.isna(b) or int(b) not in _texts)
    if _n_legacy:
        log.warning("  Stage 2: %d bills fell back to legacy text", _n_legacy)

    y_prob = pipeline.predict_proba(to_score["combined_text"])[:, 1]
    y_pred = pipeline.predict(to_score["combined_text"])
    to_score["y_prob"] = y_prob
    to_score["y_pred"] = y_pred

    high = (y_prob >= 0.65).sum()
    mid  = ((y_prob >= 0.35) & (y_prob < 0.65)).sum()
    low  = (y_prob  < 0.35).sum()
    log.info(
        f"  Stage 2 scored {len(to_score):,} bills  "
        f"| high ≥0.65: {high}  mid: {mid}  low <0.35: {low}"
    )

    keep = ["bill_id", "bill_number", "y_prob", "y_pred", "n_actions", "reached_vote", "bill_year"]
    if "target_pass" in to_score.columns:
        keep.append("target_pass")
    new_preds = to_score[[c for c in keep if c in to_score.columns]]

    if os.path.exists(preds_path):
        stored = pd.read_csv(preds_path, encoding="utf-8-sig")
        stored["bill_id"] = _safe_int(stored["bill_id"])
        stored = stored[~stored["bill_id"].isin(changed_ids)]
        new_preds = pd.concat([stored, new_preds], ignore_index=True)

    new_preds = new_preds.drop_duplicates(subset=["bill_id"]).reset_index(drop=True)
    new_preds.to_csv(preds_path, index=False, encoding="utf-8-sig")
    log.info(f"  Predictions saved : {len(new_preds):,} total bills → stage2_test_predictions.csv")
    return new_preds


# ══════════════════════════════════════════════════════════════════════════════
# STEP 6 — STAGE 3 REFRESH
# ══════════════════════════════════════════════════════════════════════════════

def refresh_stage3(
    new_tables: dict,
    changed_ids: set,
    output_dir: str,
    model_path: str,
    log: logging.Logger,
):
    if not os.path.exists(model_path):
        log.info("  Stage 3: model not found — skipping (run consolidated pipeline first)")
        return

    features_path = os.path.join(output_dir, "stage3_legislator_features.csv")
    votes_path    = os.path.join(output_dir, "votes_session_running.csv")
    s3_preds_path = os.path.join(output_dir, "stage3_vote_predictions.csv")

    if not os.path.exists(features_path):
        log.warning(
            "  Stage 3: stage3_legislator_features.csv not found. "
            "Add the save step to IL_Consolidated_Pipeline.ipynb (see SETUP NOTE) "
            "and copy the file to this server. Skipping Stage 3 for now."
        )
        return

    new_rollcalls = new_tables.get("rollcalls", pd.DataFrame()).copy()
    new_votes     = new_tables.get("votes",     pd.DataFrame()).copy()
    new_people    = new_tables.get("people",    pd.DataFrame()).copy()

    if new_rollcalls.empty or new_votes.empty:
        log.info("  Stage 3: no roll calls or votes in this download — skipping")
        return

    for df, cols in [
        (new_rollcalls, ["bill_id", "roll_call_id"]),
        (new_votes,     ["roll_call_id", "people_id"]),
        (new_people,    ["people_id"]),
    ]:
        for c in cols:
            if c in df.columns:
                df[c] = _safe_int(df[c])

    new_votes["is_yea"] = (
        new_votes["vote_desc"].str.lower().str.startswith("yea", na=False)
    ).astype(float)

    current_rc_ids = set(new_rollcalls["roll_call_id"].dropna().astype(int))
    new_vote_rows  = new_votes[new_votes["roll_call_id"].isin(current_rc_ids)].copy()

    if os.path.exists(votes_path):
        stored_votes = pd.read_csv(votes_path, low_memory=False, encoding="utf-8-sig")
        stored_votes["roll_call_id"] = _safe_int(stored_votes["roll_call_id"])
        stored_votes["people_id"]    = _safe_int(stored_votes["people_id"])
        all_votes = (
            pd.concat([stored_votes, new_vote_rows], ignore_index=True)
            .sort_values(["roll_call_id", "people_id"])
            .drop_duplicates(subset=["roll_call_id", "people_id"], keep="last")
            .reset_index(drop=True)
        )
    else:
        all_votes = new_vote_rows.copy()

    all_votes.to_csv(votes_path, index=False, encoding="utf-8-sig")

    leg_features = pd.read_csv(features_path, encoding="utf-8-sig")
    leg_features["people_id"] = _safe_int(leg_features["people_id"])

    affected_pids = set(new_vote_rows["people_id"].dropna().astype(int))
    log.info(f"  Stage 3: {len(affected_pids):,} legislators have new votes")

    for pid in affected_pids:
        leg_votes = all_votes[all_votes["people_id"] == pid]
        n_total   = len(leg_votes)
        if n_total == 0:
            continue
        n_yea    = leg_votes["is_yea"].sum()
        raw_rate = n_yea / n_total
        smoothed = (n_yea + SMOOTHING_ALPHA * 0.5) / (n_total + SMOOTHING_ALPHA)

        mask = leg_features["people_id"] == pid
        if mask.any():
            leg_features.loc[mask, "raw_yea_rate"]      = round(float(raw_rate), 4)
            leg_features.loc[mask, "smoothed_yea_rate"] = round(float(smoothed), 4)
            leg_features.loc[mask, "n_votes"]           = n_total
        else:
            new_row = pd.DataFrame([{
                "people_id":         pid,
                "raw_yea_rate":      round(float(raw_rate), 4),
                "smoothed_yea_rate": round(float(smoothed), 4),
                "n_votes":           n_total,
            }])
            if not new_people.empty:
                p_row = new_people[new_people["people_id"] == pid]
                if not p_row.empty:
                    new_row["party"] = p_row.iloc[0].get("party", "Unknown")
            leg_features = pd.concat([leg_features, new_row], ignore_index=True)

    leg_features.to_csv(features_path, index=False, encoding="utf-8-sig")
    log.info(f"  Stage 3: profiles updated for {len(affected_pids):,} legislators")

    rc_for_changed = new_rollcalls[new_rollcalls["bill_id"].isin(changed_ids)].copy()
    if rc_for_changed.empty:
        log.info("  Stage 3: no new roll calls for changed bills")
        return

    log.info(
        f"  Stage 3: scoring {len(rc_for_changed):,} roll calls "
        f"across {rc_for_changed['bill_id'].nunique():,} bills"
    )

    s3_bundle    = joblib.load(model_path)
    stage3_model = s3_bundle["model"]
    feature_cols = s3_bundle.get("feature_cols", [])

    if not feature_cols:
        log.warning("  Stage 3: feature_cols missing from model bundle — skipping scoring")
        return

    scored_rows = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    for _, rc_row in rc_for_changed.iterrows():
        rc_id    = rc_row["roll_call_id"]
        bid      = rc_row["bill_id"]
        rc_votes = new_votes[new_votes["roll_call_id"] == rc_id].copy()
        if rc_votes.empty:
            continue

        rc_votes = rc_votes.merge(
            leg_features[["people_id"] + [c for c in feature_cols if c in leg_features.columns]],
            on="people_id", how="left",
        )
        for col in feature_cols:
            if col not in rc_votes.columns:
                rc_votes[col] = 0

        X = rc_votes[feature_cols].fillna(0)
        if len(X) == 0:
            continue

        probs = stage3_model.predict_proba(X)[:, 1]
        scored_rows.append(pd.DataFrame({
            "bill_id":      bid,
            "roll_call_id": rc_id,
            "people_id":    rc_votes["people_id"].values,
            "yea_prob":     probs,
            "scored_at":    now_str,
        }))

    if scored_rows:
        new_s3 = pd.concat(scored_rows, ignore_index=True)
        if os.path.exists(s3_preds_path):
            stored_s3 = pd.read_csv(s3_preds_path, encoding="utf-8-sig")
            stored_s3["bill_id"] = _safe_int(stored_s3["bill_id"])
            stored_s3 = stored_s3[~stored_s3["bill_id"].isin(changed_ids)]
            new_s3 = pd.concat([stored_s3, new_s3], ignore_index=True)
        new_s3.to_csv(s3_preds_path, index=False, encoding="utf-8-sig")
        log.info(
            f"  Stage 3 predictions saved : {len(new_s3):,} "
            f"legislator-vote rows → stage3_vote_predictions.csv"
        )


# ══════════════════════════════════════════════════════════════════════════════
# STEP 7 — STAGE 1
# ══════════════════════════════════════════════════════════════════════════════

def score_new_bills_stage1(
    new_tables: dict, output_dir: str, model_path: str, log: logging.Logger
):
    if not os.path.exists(model_path):
        return

    if not SCIPY_AVAILABLE:
        log.warning("  Stage 1: scipy not available — skipping (pip install scipy)")
        return

    azure_cache = os.path.join(output_dir, "stage1_azure_cache.csv")
    s1_path     = os.path.join(output_dir, "stage1_new_bill_predictions.csv")

    if not os.path.exists(azure_cache):
        log.info("  Stage 1: azure cache not found — skipping")
        return

    bundle = joblib.load(model_path)
    model  = bundle["model"]
    tfidf  = bundle["tfidf"]
    dcols  = bundle["direct_cols"]

    llama_df = pd.read_csv(azure_cache, low_memory=False, encoding="utf-8-sig")
    llama_df["bill_id"] = _safe_int(llama_df["bill_id"])

    new_bills = new_tables.get("bills", pd.DataFrame()).copy()
    new_bills["bill_id"] = _safe_int(new_bills["bill_id"])
    new_ids = set(new_bills["bill_id"].dropna().astype(int))

    to_score = llama_df[llama_df["bill_id"].isin(new_ids)].copy()
    if to_score.empty:
        log.info("  Stage 1: no new bills in azure cache to score")
        return

    text_col = "combined_text_stage1" if "combined_text_stage1" in to_score.columns else None
    text     = to_score[text_col].fillna("") if text_col else pd.Series([""] * len(to_score))

    direct = pd.DataFrame(index=to_score.index)
    for c in dcols:
        direct[c] = pd.to_numeric(to_score.get(c, 0), errors="coerce").fillna(0)
    direct = direct[dcols]

    X = sp.hstack([
        tfidf.transform(text),
        sp.csr_matrix(direct.values),
    ]).toarray()
    probs = model.predict_proba(X)[:, 1]

    result = to_score[["bill_id"]].copy()
    result["s1_prob_passes"] = probs
    result["scored_at"]      = datetime.now().strftime("%Y-%m-%d %H:%M")

    if os.path.exists(s1_path):
        stored = pd.read_csv(s1_path, encoding="utf-8-sig")
        stored["bill_id"] = _safe_int(stored["bill_id"])
        stored = stored[~stored["bill_id"].isin(new_ids)]
        result = pd.concat([stored, result], ignore_index=True)

    result.to_csv(s1_path, index=False, encoding="utf-8-sig")
    log.info(f"  Stage 1 scored {len(result):,} bills → stage1_new_bill_predictions.csv")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 8a — INTEGRATION SNAPSHOT
# ══════════════════════════════════════════════════════════════════════════════

def build_integration_snapshot(
    preds_path: str, agg_path: str, output_dir: str, log: logging.Logger
) -> pd.DataFrame:
    preds = pd.read_csv(preds_path, encoding="utf-8-sig")
    agg   = pd.read_csv(agg_path, low_memory=False, encoding="utf-8-sig",
                        usecols=["bill_id", "semantic_concat", "n_actions"])
    for df in [preds, agg]:
        df["bill_id"] = _safe_int(df["bill_id"])

    snap = preds.merge(
        agg[["bill_id", "semantic_concat"]], on="bill_id", how="left"
    )

    active_mask = snap["reached_vote"] == 0
    if "target_pass" in snap.columns:
        active_mask &= (snap["target_pass"] == 0)
    active = snap[active_mask].copy()

    active["momentum"]     = active["semantic_concat"].fillna("").apply(compute_momentum)
    active["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    out_cols = ["bill_id", "bill_number", "y_prob", "n_actions", "reached_vote", "bill_year", "momentum", "generated_at"]
    if "target_pass" in active.columns:
        out_cols.insert(5, "target_pass")
    if "session_id" in active.columns:
        out_cols.insert(1, "session_id")

    integ = active[[c for c in out_cols if c in active.columns]]
    integ_path = os.path.join(output_dir, "stage2_predictions_integration.csv")
    integ.to_csv(integ_path, index=False, encoding="utf-8-sig")

    log.info(
        f"  Integration snapshot : {len(integ):,} active bills "
        f"→ stage2_predictions_integration.csv"
    )
    log.info(
        f"  Momentum breakdown   : "
        f"Gaining={integ['momentum'].eq('Gaining').sum()}  "
        f"Stable={integ['momentum'].eq('Stable').sum()}  "
        f"Losing={integ['momentum'].eq('Losing').sum()}"
    )
    return integ


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5b — PROBABILITY TRAIL UPDATE
# Scores every changed bill at every prefix depth for the sparkline chart.
# ══════════════════════════════════════════════════════════════════════════════

TRAIL_FILENAME = "stage2_probability_trail.parquet"
AZURE_TRAIL_BLOB = "legislation/predictions/llama31_1200/probability_trail/stage2_probability_trail.parquet"


def _build_trail_rows_for_bill(bill_id, bill_number: str,
                                actions: pd.DataFrame, pipeline) -> list:
    """Score one bill at every prefix depth. Stops at leakage."""
    rows = []
    action_texts = []
    semantic_stages = []
    for i, (_, row) in enumerate(actions.iterrows(), start=1):
        action_text = str(row.get("action", "") or "")
        sem_stage   = str(row.get("semantic_stage", "") or "Unlabeled")
        action_date = (
            row["date"].strftime("%Y-%m-%d") if pd.notna(row.get("date")) else ""
        )
        if sem_stage in LEAKAGE_STAGES or _LEAKAGE_PATTERN.search(action_text):
            break
        action_texts.append(action_text)
        semantic_stages.append(sem_stage)
        combined = (
            " || ".join(action_texts) + " || " + " || ".join(semantic_stages)
        )
        try:
            y_prob = float(pipeline.predict_proba([combined])[0, 1])
        except Exception:
            y_prob = float("nan")
        rows.append({
            "bill_id":        bill_id,
            "bill_number":    bill_number,
            "action_index":   i,
            "action_date":    action_date,
            "semantic_stage": sem_stage,
            "action_text":    action_text[:120],
            "y_prob":         round(y_prob, 6),
        })
    return rows


def update_probability_trail(changed_ids: set, agg_path: str, model_path: str,
                               output_dir: str, config: dict, log: logging.Logger):
    """Update trail parquet for changed bills, upload to Azure."""
    if not changed_ids or not os.path.exists(model_path):
        log.info("  Trail: skipping (no changed bills or model missing)")
        return
    labeled_path = os.path.join(output_dir, "history_actions_labeled.csv")
    trail_path   = os.path.join(output_dir, TRAIL_FILENAME)
    if not os.path.exists(labeled_path):
        log.warning("  Trail: history_actions_labeled.csv not found — skipping")
        return
    t0 = time.time()
    try:
        bundle   = joblib.load(model_path)
        pipeline = bundle["pipeline"]
    except Exception as e:
        log.error("  Trail: model load failed: %s", e)
        return
    try:
        hist = pd.read_csv(labeled_path, low_memory=False, encoding="utf-8-sig")
        hist["bill_id"] = pd.to_numeric(hist["bill_id"], errors="coerce").astype("Int64")
        hist["date"]    = pd.to_datetime(hist["date"], errors="coerce")
        hist = hist[hist["bill_id"].isin(changed_ids)].sort_values(
            ["bill_id", "date", "sequence"]
        ).reset_index(drop=True)
    except Exception as e:
        log.error("  Trail: could not load history: %s", e)
        return
    if hist.empty:
        log.info("  Trail: no history rows for changed bills")
        return
    preds_path = os.path.join(output_dir, "stage2_test_predictions.csv")
    bn_map = {}
    if os.path.exists(preds_path):
        try:
            p = pd.read_csv(preds_path, encoding="utf-8-sig",
                            usecols=["bill_id", "bill_number"])
            p["bill_id"] = pd.to_numeric(p["bill_id"], errors="coerce").astype("Int64")
            bn_map = dict(zip(p["bill_id"], p["bill_number"].astype(str)))
        except Exception:
            pass
    new_rows = _s2_trail_rows(hist, bn_map, bundle, pipeline, log)
    if not new_rows:
        log.info("  Trail: no rows produced")
        return
    new_df = pd.DataFrame(new_rows)
    new_df["bill_id"] = pd.to_numeric(new_df["bill_id"], errors="coerce").astype("Int64")
    if os.path.exists(trail_path):
        try:
            existing = pd.read_parquet(trail_path)
            existing["bill_id"] = pd.to_numeric(
                existing["bill_id"], errors="coerce"
            ).astype("Int64")
            existing = existing[~existing["bill_id"].isin(changed_ids)]
            combined = pd.concat([existing, new_df], ignore_index=True)
        except Exception as e:
            log.warning("  Trail: could not merge existing (%s) — overwriting", e)
            combined = new_df
    else:
        combined = new_df
    combined.to_parquet(trail_path, index=False, engine="pyarrow")
    log.info("  Trail: %d new rows for %d bills, total %d rows (%.1fs)",
             len(new_df), len(changed_ids), len(combined), time.time() - t0)
    # Upload to Azure
    if AZURE_SDK:
        try:
            sas_url = config["azure"]["gold_container_sas_url"]
            container = ContainerClient.from_container_url(sas_url)
            buf = io.BytesIO()
            combined.to_parquet(buf, engine="pyarrow", index=False)
            buf.seek(0)
            container.get_blob_client(AZURE_TRAIL_BLOB).upload_blob(buf, overwrite=True)
            log.info("  Trail uploaded → gold/%s", AZURE_TRAIL_BLOB)
        except Exception as e:
            log.warning("  Trail Azure upload failed (local saved): %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5c — VALIDATION TRACKER
# Records model predictions at the time bills are scored, then checks
# daily whether any tracked bills have since passed or failed.
# Launch date: 2026-06-17. Only bills scored ON OR AFTER this date are tracked.
# ══════════════════════════════════════════════════════════════════════════════

VALIDATION_FILENAME  = "stage2_validation_tracker.parquet"
AZURE_VALIDATION_BLOB = "legislation/predictions/llama31_1200/validation/stage2_validation_tracker.parquet"
VALIDATION_LAUNCH_DATE = "2026-06-17"


def update_validation_tracker(
    changed_ids: set,
    new_tables: dict,
    preds_path: str,
    output_dir: str,
    config: dict,
    log: logging.Logger,
):
    """
    Maintains stage2_validation_tracker.parquet — the ground-truth record
    for ongoing model accuracy measurement starting 2026-06-17.

    Two operations each run:

    1. RECORD new predictions — for every changed bill that was just rescored,
       snapshot: bill_id, bill_number, session_id, scored_at, y_prob, momentum,
       n_actions, predicted_pass (y_prob >= 0.65), plus outcome columns (null
       until outcome is known).

    2. RESOLVE outcomes — for any tracked bill that now has a terminal status
       (passed / failed / vetoed / session sine die), fill in:
       outcome_known_at, actual_passed, days_to_outcome, correct_prediction.
    """
    if not changed_ids:
        return

    tracker_path = os.path.join(output_dir, VALIDATION_FILENAME)
    t0 = time.time()

    # Load or create tracker
    if os.path.exists(tracker_path):
        try:
            tracker = pd.read_parquet(tracker_path)
            tracker["bill_id"] = pd.to_numeric(
                tracker["bill_id"], errors="coerce"
            ).astype("Int64")
        except Exception as e:
            log.warning("  Validation: could not load tracker (%s) — starting fresh", e)
            tracker = pd.DataFrame()
    else:
        tracker = pd.DataFrame()

    # ── 1. RECORD new predictions ─────────────────────────────────────────
    scored_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    if os.path.exists(preds_path):
        try:
            preds = pd.read_csv(preds_path, encoding="utf-8-sig")
            preds["bill_id"] = pd.to_numeric(
                preds["bill_id"], errors="coerce"
            ).astype("Int64")
            new_preds = preds[preds["bill_id"].isin(changed_ids)].copy()

            if not new_preds.empty:
                new_records = pd.DataFrame({
                    "bill_id":         new_preds["bill_id"],
                    "bill_number":     new_preds.get("bill_number", ""),
                    "session_id":      new_preds.get("session_id", pd.NA),
                    "scored_at":       scored_at,
                    "scored_date":     datetime.now().strftime("%Y-%m-%d"),
                    "y_prob":          new_preds["y_prob"].round(6),
                    "momentum":        new_preds.get("momentum", "Stable"),
                    "n_actions":       new_preds.get("n_actions", pd.NA),
                    "predicted_pass":  (new_preds["y_prob"] >= 0.65),
                    # Outcome columns — filled later when outcome is known
                    "outcome_known_at": pd.NA,
                    "actual_passed":    pd.NA,
                    "terminal_status":  pd.NA,
                    "days_to_outcome":  pd.NA,
                    "correct_prediction": pd.NA,
                    "tracking_version": "v1.0",
                })

                if tracker.empty:
                    tracker = new_records
                else:
                    # Upsert — remove stale rows for same bill, add fresh
                    tracker = tracker[~tracker["bill_id"].isin(changed_ids)]
                    tracker = pd.concat([tracker, new_records], ignore_index=True)

                log.info("  Validation: recorded %d new predictions", len(new_records))
        except Exception as e:
            log.warning("  Validation: could not record predictions: %s", e)

    # ── 2. RESOLVE outcomes ───────────────────────────────────────────────
    new_bills = new_tables.get("bills", pd.DataFrame()).copy()
    if not new_bills.empty and not tracker.empty:
        try:
            new_bills["bill_id"] = pd.to_numeric(
                new_bills["bill_id"], errors="coerce"
            ).astype("Int64")
            new_bills["_status_lower"] = (
                new_bills.get("status_desc", pd.Series(dtype=str))
                .fillna("").str.lower()
            )

            PASS_TERMS_RE = re.compile(
                r"\b(passed|enacted|adopted|approved by governor|signed|public act)\b",
                re.IGNORECASE,
            )
            FAIL_TERMS_RE = re.compile(
                r"\b(failed|vetoed|session sine die|withdrawn|dead)\b",
                re.IGNORECASE,
            )

            # Only resolve rows where outcome is not yet known
            unresolved_mask = tracker["outcome_known_at"].isna()
            unresolved_ids  = set(tracker.loc[unresolved_mask, "bill_id"].dropna().astype(int))
            terminal_bills  = new_bills[new_bills["bill_id"].isin(unresolved_ids)]

            resolved_count = 0
            for _, bill_row in terminal_bills.iterrows():
                bid     = bill_row["bill_id"]
                status  = bill_row["_status_lower"]
                is_pass = bool(PASS_TERMS_RE.search(status))
                is_fail = bool(FAIL_TERMS_RE.search(status))
                if not (is_pass or is_fail):
                    continue

                idx = tracker[(tracker["bill_id"] == bid) & unresolved_mask].index
                if idx.empty:
                    continue

                outcome_date = datetime.now().strftime("%Y-%m-%d %H:%M")
                for i in idx:
                    scored_dt = pd.to_datetime(tracker.at[i, "scored_date"], errors="coerce")
                    days = (
                        (pd.Timestamp(datetime.now()) - scored_dt).days
                        if pd.notna(scored_dt) else pd.NA
                    )
                    tracker.at[i, "outcome_known_at"]   = outcome_date
                    tracker.at[i, "actual_passed"]      = is_pass
                    tracker.at[i, "terminal_status"]    = status[:80]
                    tracker.at[i, "days_to_outcome"]    = days
                    tracker.at[i, "correct_prediction"] = (
                        tracker.at[i, "predicted_pass"] == is_pass
                    )
                resolved_count += 1

            if resolved_count:
                log.info("  Validation: resolved outcomes for %d bills", resolved_count)
        except Exception as e:
            log.warning("  Validation: outcome resolution failed: %s", e)

    if tracker.empty:
        return

    # Save locally
    tracker.to_parquet(tracker_path, index=False, engine="pyarrow")
    log.info(
        "  Validation tracker: %d total rows, %d resolved, %.1fs",
        len(tracker),
        int(tracker["outcome_known_at"].notna().sum()),
        time.time() - t0,
    )

    # Upload to Azure
    if AZURE_SDK:
        try:
            sas_url = config["azure"]["gold_container_sas_url"]
            container = ContainerClient.from_container_url(sas_url)
            buf = io.BytesIO()
            tracker.to_parquet(buf, engine="pyarrow", index=False)
            buf.seek(0)
            container.get_blob_client(AZURE_VALIDATION_BLOB).upload_blob(
                buf, overwrite=True
            )
            log.info("  Validation uploaded → gold/%s", AZURE_VALIDATION_BLOB)
        except Exception as e:
            log.warning("  Validation Azure upload failed (local saved): %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 8b — AZURE UPLOAD
# ══════════════════════════════════════════════════════════════════════════════

def upload_to_azure(
    integ_df: pd.DataFrame,
    session_ids: list,
    config: dict,
    log: logging.Logger,
) -> bool:
    """Returns True if all uploads succeeded, False otherwise."""
    if not AZURE_SDK:
        log.warning(
            "  azure-storage-blob not installed — skipping upload. "
            "Run: pip install azure-storage-blob"
        )
        return False

    sas_url   = config["azure"]["gold_container_sas_url"]
    path_tmpl = config["azure"].get(
        "predictions_path_template",
        "legislation/predictions/llama31_1200/session={session_id}/stage2_predictions.parquet",
    )

    try:
        container = ContainerClient.from_container_url(sas_url)
    except Exception as e:
        log.error(f"  Azure ContainerClient failed: {e}")
        return False

    all_ok = True
    for sid in session_ids:
        blob_name = path_tmpl.format(session_id=sid)
        buf = io.BytesIO()
        integ_df.to_parquet(buf, engine="pyarrow", index=False)
        buf.seek(0)
        try:
            blob = container.get_blob_client(blob_name)
            blob.upload_blob(buf, overwrite=True)
            log.info(f"  Uploaded → gold/{blob_name}")
        except Exception as e:
            log.error(f"  Azure upload failed for session {sid}: {e}")
            all_ok = False

    return all_ok


# ══════════════════════════════════════════════════════════════════════════════
# MAIN STATE RUNNER
# ══════════════════════════════════════════════════════════════════════════════

def run_state(state: str, config: dict, args, log: logging.Logger):
    """
    Runs the full prediction pipeline for one state.
    Wraps all steps in PipelineLogger so the Job Execution Monitor
    can track health, detect silences, and alert on failures.
    """
    # ── Pipeline logger setup ─────────────────────────────────────────────
    # Import here so the module still runs if pipeline_logger has an issue.
    try:
        from utils.pipeline_logger import PipelineLogger
        pl = PipelineLogger(
            workflow_name="legislation_prediction_pipeline",
            log_dir=_PIPELINE_LOG_DIR,
            pipeline_version=datetime.now().strftime("%Y-%m-%d"),
        )
        _logging_available = True
    except Exception as _le:
        log.warning(f"  PipelineLogger unavailable ({_le}) — run proceeds without monitor logging")
        _logging_available = False
        pl = None

    state_cfg  = config["states"][state]
    output_dir = state_cfg["pipeline_output_dir"]
    models_dir = config.get("models_dir", os.path.join(output_dir, "models"))

    for d in [output_dir, models_dir]:
        os.makedirs(d, exist_ok=True)

    s1_model  = os.path.join(models_dir, "stage1_introduction_model.joblib")
    s2_model  = os.path.join(models_dir, "stage2_survival_model.joblib")
    s3_model  = os.path.join(models_dir, "stage3_vote_model.joblib")
    agg_path  = os.path.join(output_dir, "history_actions_aggregated.csv")
    lab_path  = os.path.join(output_dir, "history_actions_labeled.csv")
    pred_path = os.path.join(output_dir, "stage2_test_predictions.csv")

    log.info(f"\n{'='*60}")
    log.info(f"[{state}] Pipeline run starting")
    log.info(f"[{state}] Output dir : {output_dir}")
    log.info(
        f"[{state}] Models     : "
        f"S1={'✓' if os.path.exists(s1_model) else '✗ (pending)'}  "
        f"S2={'✓' if os.path.exists(s2_model) else '✗ REQUIRED'}  "
        f"S3={'✓' if os.path.exists(s3_model) else '✗ (optional)'}"
    )
    log.info(f"{'='*60}")

    # Track run-level metrics for the monitor log
    _t_start         = time.time()
    _n_sessions      = 0
    _n_bills_scored  = 0
    _upload_ok       = False

    # ── Wrap everything in pipeline_run() ────────────────────────────────
    def _run_body():
        nonlocal _n_sessions, _n_bills_scored, _upload_ok

        # ── Step 1: Hash check + download ─────────────────────────────
        log.info(f"[{state}] Step 1: Hash check & download")
        if _logging_available:
            with pl.stage("legiscan_hash_check", order=1):
                downloaded = check_and_download(
                    config, state, args.force_refresh, args.dry_run, log
                )
                pl.set_stage_metadata(
                    input_count=1,
                    output_count=len(downloaded),
                    extra={"sessions_downloaded": len(downloaded), "state": state},
                )
        else:
            downloaded = check_and_download(
                config, state, args.force_refresh, args.dry_run, log
            )

        if not downloaded:
            log.info(f"[{state}] No sessions changed — nothing to update. Run complete.")
            return

        _n_sessions = len(downloaded)

        # ── Combine tables ────────────────────────────────────────────
        all_changed_ids = set()
        all_session_ids = []
        combined        = {t: [] for t in CSV_TABLES}

        for sess_info, tables in downloaded:
            sid = sess_info["session_id"]
            all_session_ids.append(sid)

            log.info(f"[{state}] Step 2: Identifying changed bills (session {sid})")
            changed = find_changed_bills(tables, agg_path, args.force_rescore, log,
                                         include_terminal=args.include_terminal)
            all_changed_ids |= changed

            for t, df in tables.items():
                combined[t].append(df)

        combined_tables = {
            t: pd.concat(frames, ignore_index=True)
            for t, frames in combined.items() if frames
        }
        _NIGHTLY["bills"] = combined_tables.get("bills")

        if not all_changed_ids:
            log.info(f"[{state}] Sessions downloaded but no bill changes detected. Run complete.")
            if not args.dry_run:
                hashes = load_hashes(output_dir)
                for sess_info, _ in downloaded:
                    hashes[str(sess_info["session_id"])] = sess_info["new_hash"]
                save_hashes(output_dir, hashes)
            return

        log.info(f"[{state}] Total bills to process : {len(all_changed_ids):,}")

        # ── Step 3+4: Update history ──────────────────────────────────
        log.info(f"[{state}] Steps 3-4: Updating history & aggregated data")
        if _logging_available:
            with pl.stage("update_history", order=2):
                updated, updated_agg = update_history(
                    combined_tables, all_changed_ids, lab_path, agg_path, log
                )
                pl.set_stage_metadata(
                    input_count=len(all_changed_ids),
                    output_count=len(updated_agg),
                    rows_written=len(updated_agg),
                    output_file=agg_path,
                    output_file_written=True,
                )
        else:
            update_history(combined_tables, all_changed_ids, lab_path, agg_path, log)

        # ── Step 5: Stage 2 re-scoring ────────────────────────────────
        log.info(f"[{state}] Step 5: Stage 2 re-scoring")
        if _logging_available:
            with pl.stage("stage2_rescore", order=3):
                new_preds = rescore_stage2(
                    all_changed_ids, agg_path, s2_model, pred_path, log
                )
                _n_bills_scored = len(new_preds) if not new_preds.empty else 0
                pl.set_stage_metadata(
                    input_count=len(all_changed_ids),
                    output_count=_n_bills_scored,
                    rows_written=_n_bills_scored,
                    output_file=pred_path,
                    output_file_written=True,
                    extra={"bills_scored_this_run": len(all_changed_ids)},
                )
        else:
            new_preds = rescore_stage2(
                all_changed_ids, agg_path, s2_model, pred_path, log
            )
            _n_bills_scored = len(new_preds) if not new_preds.empty else 0

        # ── Step 5b: Probability trail update ─────────────────────────
        log.info(f"[{state}] Step 5b: Updating probability trails")
        if _logging_available:
            with pl.stage("update_trail", order=8):
                update_probability_trail(
                    all_changed_ids, agg_path, s2_model, output_dir, config, log
                )
                pl.set_stage_metadata(
                    input_count=len(all_changed_ids),
                    extra={"trail_bills": len(all_changed_ids)},
                )
        else:
            update_probability_trail(
                all_changed_ids, agg_path, s2_model, output_dir, config, log
            )

        # ── Step 5c: Validation tracker update ────────────────────────
        log.info(f"[{state}] Step 5c: Updating validation tracker")
        if _logging_available:
            with pl.stage("update_validation", order=9):
                update_validation_tracker(
                    all_changed_ids, combined_tables,
                    pred_path, output_dir, config, log
                )
                pl.set_stage_metadata(
                    input_count=len(all_changed_ids),
                    extra={"tracking_since": VALIDATION_LAUNCH_DATE},
                )
        else:
            update_validation_tracker(
                all_changed_ids, combined_tables,
                pred_path, output_dir, config, log
            )

        # ── Step 6: Stage 3 ───────────────────────────────────────────
        log.info(f"[{state}] Step 6: Stage 3 legislator refresh")
        if _logging_available:
            with pl.stage("stage3_refresh", order=4):
                refresh_stage3(
                    combined_tables, all_changed_ids, output_dir, s3_model, log
                )
                pl.set_stage_metadata(
                    input_count=len(all_changed_ids),
                    skip_reason=None if os.path.exists(s3_model) else "model_not_present",
                )
        else:
            refresh_stage3(combined_tables, all_changed_ids, output_dir, s3_model, log)

        # ── Step 7: Stage 1 ───────────────────────────────────────────
        log.info(f"[{state}] Step 7: Stage 1 new bill scoring")
        if _logging_available:
            with pl.stage("stage1_score", order=5):
                score_new_bills_stage1(combined_tables, output_dir, s1_model, log)
                pl.set_stage_metadata(
                    skip_reason=None if os.path.exists(s1_model) else "model_not_present",
                )
        else:
            score_new_bills_stage1(combined_tables, output_dir, s1_model, log)

        # ── Step 8a: Integration snapshot ─────────────────────────────
        log.info(f"[{state}] Step 8a: Building integration snapshot")
        integ_path = os.path.join(output_dir, "stage2_predictions_integration.csv")
        if _logging_available:
            with pl.stage("build_snapshot", order=6):
                integ = build_integration_snapshot(
                    pred_path, agg_path, output_dir, log
                )
                pl.set_stage_metadata(
                    input_count=_n_bills_scored,
                    output_count=len(integ),
                    rows_written=len(integ),
                    output_file=integ_path,
                    output_file_written=True,
                )
        else:
            integ = build_integration_snapshot(pred_path, agg_path, output_dir, log)

        # ── Step 8b: Azure upload ──────────────────────────────────────
        log.info(f"[{state}] Step 8b: Uploading to Azure gold blob")
        if _logging_available:
            with pl.stage("azure_upload", order=7):
                _upload_ok = upload_to_azure(integ, all_session_ids, config, log)
                pl.set_stage_metadata(
                    input_count=len(all_session_ids),
                    output_count=len(all_session_ids),
                    output_file_written=_upload_ok,
                    extra={
                        "sessions_uploaded":  len(all_session_ids),
                        "bills_in_snapshot":  len(integ),
                        "run_duration_secs":  round(time.time() - _t_start, 1),
                    },
                )
        else:
            _upload_ok = upload_to_azure(integ, all_session_ids, config, log)

        # ── Step 9: Save hashes ────────────────────────────────────────
        if not args.dry_run:
            log.info(f"[{state}] Step 9: Saving dataset hashes")
            hashes = load_hashes(output_dir)
            for sess_info, _ in downloaded:
                hashes[str(sess_info["session_id"])] = sess_info["new_hash"]
            save_hashes(output_dir, hashes)
            log.info(f"[{state}] {len(hashes)} session hashes saved")

        log.info(f"[{state}] ✓ Run complete")

    # ── Execute with or without pipeline_run() wrapper ───────────────────
    if _logging_available:
        with pl.pipeline_run():
            _run_body()
    else:
        _run_body()


# ══════════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Legislation Pipeline — Daily Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config",        default="config.json",
                        help="Path to config.json (default: ./config.json)")
    parser.add_argument("--state",         default=None,
                        help="Run a single state only (e.g. IL)")
    parser.add_argument("--force-refresh", action="store_true",
                        help="Re-download all sessions even if hash is unchanged")
    parser.add_argument("--force-rescore", action="store_true",
                        help="Re-score all active bills, not just changed ones")
    parser.add_argument("--include-terminal", action="store_true",
                        help="Include Passed/Failed/Vetoed bills (backfill only)")
    parser.add_argument("--dry-run",       action="store_true",
                        help="Hash check only — no downloads, no file writes")
    args, _ = parser.parse_known_args()  # parse_known_args ignores Jupyter kernel args

    config = load_config(args.config)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    log_dir = config.get("pipeline", {}).get(
        "log_dir",
        "/datashare/Admin-pipeline/pipelines/Legislation_Pipeline/logs",
    )
    log = setup_logging(log_dir, run_id)

    log.info("=" * 60)
    log.info("LEGISLATION PIPELINE — DAILY RUN")
    log.info(f"Run ID     : {run_id}")
    log.info(f"Config     : {args.config}")
    log.info(f"Dry run    : {args.dry_run}")
    log.info(f"Frce rfrsh : {args.force_refresh}")
    log.info(f"Frce rscr  : {args.force_rescore}")
    log.info(f"Azure SDK  : {'available' if AZURE_SDK else 'NOT installed — upload disabled'}")
    log.info("=" * 60)

    states  = [args.state] if args.state else list(config["states"].keys())
    success = []
    failed  = []

    for state in states:
        try:
            run_state(state, config, args, log)
            success.append(state)
        except Exception as e:
            log.error(f"[{state}] PIPELINE FAILED: {e}", exc_info=True)
            failed.append((state, str(e)))

    log.info("\n" + "=" * 60)
    log.info("SUMMARY")
    log.info(f"  Completed : {', '.join(success) if success else 'none'}")
    if failed:
        for state, err in failed:
            log.error(f"  FAILED    : {state} — {err}")
        sys.exit(1)
    log.info("  All states complete.")


if __name__ == "__main__":
    main()
