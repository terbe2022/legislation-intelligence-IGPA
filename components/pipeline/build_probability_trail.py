#!/usr/bin/env python3
'[Deployment-specific example removed from this source docstring.]'

import argparse
import io
import json
import logging
import os
import sys
import time
from pathlib import Path

import joblib
import pandas as pd
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("trail_builder")

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE = Path(__import__("os").environ.get('LEG_COMPONENTS_PIPELINE_BUILD_PROBABILITY_TRAIL_PY_BASE_L48', ""))
OUTPUT_DIR = BASE / "prediction/outputs/IL"
MODELS_DIR = BASE / "prediction/models"

LABELED_PATH  = OUTPUT_DIR / "history_actions_labeled.csv"
PREDS_PATH    = OUTPUT_DIR / "stage2_test_predictions.csv"
TRAIL_PATH    = OUTPUT_DIR / "stage2_probability_trail.parquet"
MODEL_PATH    = MODELS_DIR / "stage2_survival_model.joblib"
CONFIG_PATH   = BASE / "config.json"

# Azure blob path — same pattern as prediction parquets
AZURE_TRAIL_PATH = "legislation/predictions/llama31_1200/probability_trail/stage2_probability_trail.parquet"

# Leakage terms — must match run_daily.py exactly
import re
_LEAKAGE_TERMS = [
    r"\bpassed\b", r"\bfailed\b", r"\bvetoed?\b", r"\badopted\b",
    r"\benrolled\b", r"\boverride\b", r"\bwithdrawn\b",
    r"governor approved", r"public act", r"session sine die",
    r"sent to the governor",
]
_LEAKAGE_PATTERN = re.compile("|".join(_LEAKAGE_TERMS), re.IGNORECASE)
_LEAKAGE_STAGES = {"Passed / Enrolled", "Failed / Rejected", "Governor Certification / Return"}


# ── Custom transformer needed to unpickle Stage 2 model ───────────────────────
try:
    import scipy.sparse as _sp
    from sklearn.base import BaseEstimator, TransformerMixin

    class SparseToDense(BaseEstimator, TransformerMixin):
        def fit(self, X, y=None): return self
        def transform(self, X):
            return X.toarray() if _sp.issparse(X) else X
except ImportError:
    pass


def load_model():
    if not MODEL_PATH.exists():
        raise FileNotFoundError(f"Stage 2 model not found: {MODEL_PATH}")
    bundle = joblib.load(MODEL_PATH)
    pipeline = bundle["pipeline"]
    log.info("Model loaded: %s", pipeline.named_steps["clf"].__class__.__name__)
    return pipeline


def load_labeled_history() -> pd.DataFrame:
    log.info("Loading history_actions_labeled.csv...")
    df = pd.read_csv(LABELED_PATH, low_memory=False, encoding="utf-8-sig")
    df["bill_id"] = pd.to_numeric(df["bill_id"], errors="coerce").astype("Int64")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.sort_values(["bill_id", "date", "sequence"]).reset_index(drop=True)
    log.info("  %d rows, %d unique bills", len(df), df["bill_id"].nunique())
    return df


def load_predictions() -> pd.DataFrame:
    log.info("Loading stage2_test_predictions.csv...")
    df = pd.read_csv(PREDS_PATH, encoding="utf-8-sig")
    df["bill_id"] = pd.to_numeric(df["bill_id"], errors="coerce").astype("Int64")
    return df


def build_trail_for_bill(
    bill_id,
    bill_number: str,
    actions: pd.DataFrame,
    pipeline,
) -> list[dict]:
    """
    Score a single bill at every prefix depth.
    actions must be sorted by date/sequence already.
    Stops at the first leakage action (outcome known).
    """
    rows = []
    action_texts = []
    semantic_stages = []

    for i, (_, row) in enumerate(actions.iterrows(), start=1):
        action_text = str(row.get("action", "") or "")
        sem_stage   = str(row.get("semantic_stage", "") or "Unlabeled")
        action_date = row["date"].strftime("%Y-%m-%d") if pd.notna(row.get("date")) else ""

        # Stop at leakage — outcome is known from here
        if (
            sem_stage in _LEAKAGE_STAGES
            or _LEAKAGE_PATTERN.search(action_text)
        ):
            break

        action_texts.append(action_text)
        semantic_stages.append(sem_stage)

        # Build the same combined text the Stage 2 model was trained on
        combined = (
            " || ".join(action_texts)
            + " || "
            + " || ".join(semantic_stages)
        )

        try:
            y_prob = float(pipeline.predict_proba([combined])[0, 1])
        except Exception:
            y_prob = float("nan")

        rows.append({
            "bill_id":       bill_id,
            "bill_number":   bill_number,
            "action_index":  i,
            "action_date":   action_date,
            "semantic_stage":sem_stage,
            "action_text":   action_text[:120],
            "y_prob":        round(y_prob, 6),
        })

    return rows


def build_full_trail(
    history: pd.DataFrame,
    preds: pd.DataFrame,
    pipeline,
    bill_ids = None,
) -> pd.DataFrame:
    """
    Build probability trails for all bills (or a subset).
    bill_ids: if provided, only score those bills (for daily incremental update).
    """
    # Join bill_number from predictions
    bill_number_map = dict(zip(
        preds["bill_id"].dropna().astype("Int64"),
        preds["bill_number"].fillna("").astype(str),
    ))

    # Filter history to requested bills
    if bill_ids:
        history = history[history["bill_id"].isin(bill_ids)].copy()

    grouped = history.groupby("bill_id", sort=False)
    n_bills = len(grouped)
    log.info("Scoring %d bills at every prefix depth...", n_bills)

    all_rows = []
    t0 = time.perf_counter()
    n_done = 0

    for bill_id, grp in grouped:
        bill_number = bill_number_map.get(bill_id, "")
        rows = build_trail_for_bill(bill_id, bill_number, grp, pipeline)
        all_rows.extend(rows)
        n_done += 1

        if n_done % 5000 == 0:
            elapsed = time.perf_counter() - t0
            rate = n_done / elapsed
            eta = (n_bills - n_done) / rate if rate > 0 else 0
            log.info(
                "  %d / %d bills (%.0f bills/s, ETA %.0f s)",
                n_done, n_bills, rate, eta,
            )

    elapsed = time.perf_counter() - t0
    log.info(
        "Done: %d trail rows from %d bills in %.1f s (%.0f bills/s)",
        len(all_rows), n_done, elapsed, n_done / elapsed if elapsed > 0 else 0,
    )

    if not all_rows:
        return pd.DataFrame(columns=[
            "bill_id", "bill_number", "action_index", "action_date",
            "semantic_stage", "action_text", "y_prob",
        ])

    df = pd.DataFrame(all_rows)
    df["bill_id"] = pd.to_numeric(df["bill_id"], errors="coerce").astype("Int64")
    return df


def save_trail(df: pd.DataFrame, path: Path = TRAIL_PATH) -> None:
    df.to_parquet(path, index=False, engine="pyarrow")
    sz = path.stat().st_size // 1024
    log.info("Saved: %s (%d KB, %d rows)", path, sz, len(df))


def upload_trail(trail_path: Path = TRAIL_PATH) -> bool:
    """Upload trail parquet to Azure gold."""
    try:
        from azure.storage.blob import ContainerClient
    except ImportError:
        log.error("azure-storage-blob not installed. Run: pip install azure-storage-blob")
        return False

    if not CONFIG_PATH.exists():
        log.error("config.json not found at %s", CONFIG_PATH)
        return False

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    sas_url = cfg.get("azure", {}).get("gold_container_sas_url", "")
    if not sas_url or "YOUR_SAS" in sas_url:
        log.error("Azure SAS URL not configured in config.json")
        return False

    try:
        container = ContainerClient.from_container_url(sas_url)
        buf = io.BytesIO()
        pd.read_parquet(trail_path).to_parquet(buf, engine="pyarrow", index=False)
        buf.seek(0)
        blob = container.get_blob_client(AZURE_TRAIL_PATH)
        blob.upload_blob(buf, overwrite=True)
        log.info("Uploaded → gold/%s", AZURE_TRAIL_PATH)
        return True
    except Exception as e:
        log.error("Azure upload failed: %s", e)
        return False


def main():
    parser = argparse.ArgumentParser(description="Build Stage 2 probability trails")
    parser.add_argument("--upload", action="store_true",
                        help="Upload trail parquet to Azure gold after building")
    parser.add_argument("--bills", nargs="*", type=int,
                        help="Only score specific bill_ids (for testing)")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("STAGE 2 PROBABILITY TRAIL BUILDER")
    log.info("=" * 60)

    pipeline = load_model()
    history  = load_labeled_history()
    preds    = load_predictions()

    bill_ids = set(args.bills) if args.bills else None
    trail_df = build_full_trail(history, preds, pipeline, bill_ids=bill_ids)

    # If updating a subset, merge with existing trail
    if bill_ids and TRAIL_PATH.exists():
        log.info("Merging with existing trail (%d rows)...", len(pd.read_parquet(TRAIL_PATH)))
        existing = pd.read_parquet(TRAIL_PATH)
        existing["bill_id"] = pd.to_numeric(existing["bill_id"], errors="coerce").astype("Int64")
        existing = existing[~existing["bill_id"].isin(bill_ids)]
        trail_df = pd.concat([existing, trail_df], ignore_index=True)
        log.info("Merged trail: %d rows", len(trail_df))

    save_trail(trail_df)

    if args.upload:
        upload_trail()
    else:
        log.info("Skipping Azure upload. Re-run with --upload to push to Azure.")

    log.info("Done.")


if __name__ == "__main__":
    main()
