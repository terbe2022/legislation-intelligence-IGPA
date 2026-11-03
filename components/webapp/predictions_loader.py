"""
predictions_loader.py
=====================

Shared utility for loading Stage 2 passage probability predictions from
Azure Blob Storage.

Prediction parquet files are configured centrally in data_catalog.py:

    PREDICTION_URLS = {
        2176: "<104th GA predictions parquet>",
        2020: "<103rd GA predictions parquet>",
        1801: "<102nd GA predictions parquet>",
        1630: "<101st GA predictions parquet>",
        1407: "<100th GA predictions parquet>",
        1127: "<99th GA predictions parquet>",
        112:  "<98th GA predictions parquet>",
        999:  "<97th GA predictions parquet>",
        21:   "<96th GA predictions parquet>",
    }

The parquet uploaded by run_daily.py contains:

    bill_id
    y_prob
    n_actions
    reached_vote
    momentum
    generated_at

Usage:

    from predictions_loader import (
        join_predictions,
        PROB_COL,
        MOMENTUM_COL,
    )

    df = join_predictions(
        df,
        session_ids=[2176, 2020, 1801]
    )

    # Adds:
    #   passage_prob
    #   momentum

Notes:

    - URLs are maintained exclusively in data_catalog.py
    - Predictions are optional and load gracefully
    - Multiple sessions may be loaded simultaneously
    - Streamlit cache TTL = 1 hour
"""

import pandas as pd
import streamlit as st

try:
    from data_catalog import PREDICTION_URLS
except (ImportError, AttributeError):
    PREDICTION_URLS = {}

# Column names exposed to the pages — don't change these without updating both pages
PROB_COL     = "passage_prob"
MOMENTUM_COL = "momentum"

# Threshold buckets — used consistently across pages and filters
PROB_BUCKETS = {
    "High (≥ 65%)":   0.65,
    "Medium (≥ 35%)": 0.35,
    "Any":            0.00,
}


@st.cache_data(ttl=3600, show_spinner=False)
def load_predictions(session_ids: tuple) -> pd.DataFrame:
    """
    Load Stage 2 predictions for one or more sessions.
    session_ids must be a tuple (not list) for Streamlit cache hash.

    Returns DataFrame with: bill_id, passage_prob, momentum, generated_at
    Returns empty DataFrame gracefully if URLs not configured or not yet uploaded.
    """
    if not PREDICTION_URLS:
        return pd.DataFrame()

    frames = []
    for sid in session_ids:
        url = PREDICTION_URLS.get(sid)
        if not url:
            continue
        try:
            try:
                df = pd.read_parquet(url, columns=["bill_id", "y_prob", "momentum", "generated_at"])
            except Exception:
                df = pd.read_parquet(url, columns=["bill_id", "y_prob", "momentum"])
            df = df.rename(columns={"y_prob": PROB_COL})
            df["session_id"] = sid
            frames.append(df)
        except Exception:
            pass

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    # Normalise the join key dtype — gold parquet bill_id is sometimes string,
    # prediction parquet bill_id is Int64. Coerce both sides to string so the
    # merge in join_predictions never fails on a dtype mismatch.
    combined["bill_id"] = combined["bill_id"].astype(str).str.strip()
    # Keep the most recent prediction per bill if sessions overlap
    return combined.drop_duplicates(subset="bill_id", keep="last").reset_index(drop=True)


def join_predictions(df: pd.DataFrame, session_ids) -> pd.DataFrame:
    """
    Left-join passage predictions onto an existing bill DataFrame on bill_id.
    Safe: if predictions aren't available, df is returned unchanged.
    Adds columns: passage_prob (float 0-1), momentum (str Gaining/Stable/Losing)
    """
    if df is None or df.empty or "bill_id" not in df.columns:
        return df

    # Normalise session_ids to tuple for cache
    if not isinstance(session_ids, (tuple, list)):
        session_ids = (session_ids,)
    preds = load_predictions(tuple(session_ids))

    if preds.empty:
        return df

    # Avoid duplicating columns if already joined
    drop_cols = [c for c in [PROB_COL, MOMENTUM_COL] if c in df.columns]
    if drop_cols:
        df = df.drop(columns=drop_cols)

    # Match the join-key dtype to the predictions side (string) so the merge
    # cannot fail on object-vs-Int64 mismatch. Work on a copy of the key only.
    df = df.copy()
    df["bill_id"] = df["bill_id"].astype(str).str.strip()

    return df.merge(
        preds[["bill_id", PROB_COL, MOMENTUM_COL]],
        on="bill_id",
        how="left",
    )


def predictions_available(session_ids) -> bool:
    """Quick check — returns True if at least one session has predictions loaded."""
    if not isinstance(session_ids, (tuple, list)):
        session_ids = (session_ids,)
    return not load_predictions(tuple(session_ids)).empty


def predictions_freshness(session_ids) -> str:
    """Returns a human-readable freshness string, e.g. 'Updated 2026-06-16 17:05'."""
    if not isinstance(session_ids, (tuple, list)):
        session_ids = (session_ids,)
    preds = load_predictions(tuple(session_ids))
    if preds.empty or "generated_at" not in preds.columns:
        return "Predictions not yet available"
    ts = preds["generated_at"].dropna()
    if ts.empty:
        return "Predictions loaded"
    return f"Predictions updated {ts.iloc[0]}"
