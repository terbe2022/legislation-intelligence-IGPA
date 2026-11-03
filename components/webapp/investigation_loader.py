"""
investigation_loader.py
========================
Loads personalized investigations from the gold parquet written by
investigate_flagged_bills.py (Step 8) on the pipeline server.

One row per (user_id x bill_id):
  user_id, bill_id, bill_number, session_id,
  headline, impact_type, directness, what_changes, recommended_actions (JSON),
  who_it_affects, timeline, confidence, confidence_reason, swot (JSON or ""),
  flagged_by (JSON), max_rating_num, content_hash, triage_sig, profile_sig,
  investigated_at

Mirrors impact_loader.py: read-only SAS parquet, cached 30 min (it changes
once per scheduled run).
"""
import pandas as pd
import streamlit as st

from data_catalog import INVESTIGATIONS_URL


@st.cache_data(ttl=1800, show_spinner=False)
def _load_all_investigations() -> pd.DataFrame:
    """Full investigations parquet, cached 30 min."""
    try:
        df = pd.read_parquet(INVESTIGATIONS_URL)
        df["bill_id"] = df["bill_id"].astype(str)
        if "max_rating_num" in df.columns:
            df["max_rating_num"] = pd.to_numeric(
                df["max_rating_num"], errors="coerce").fillna(0).astype(int)
        return df
    except Exception:
        return pd.DataFrame()


def load_investigations(user_id: str) -> pd.DataFrame:
    """This user's investigations only. Empty DataFrame if none yet."""
    df = _load_all_investigations()
    if df.empty or not user_id:
        return pd.DataFrame()
    return df[df["user_id"] == user_id].copy()


def load_investigations_for_users(user_ids) -> pd.DataFrame:
    """Investigations for a set/list of user_ids (a team roster).

    Powers the "team overlap" view: one read of the cached full parquet,
    filtered to the roster. Empty DataFrame if the set is empty or nothing
    has been investigated yet. Cheap — the underlying frame is cached 30 min,
    so calling this alongside per-user load_investigations costs nothing extra.
    """
    df = _load_all_investigations()
    if df.empty or not user_ids:
        return pd.DataFrame()
    _ids = {str(u) for u in user_ids if u}
    if not _ids:
        return pd.DataFrame()
    return df[df["user_id"].isin(_ids)].copy()


def investigation_for_bill(user_id: str, bill_id: str) -> dict:
    """One user's investigation of one bill as a dict (for bill cards)."""
    df = load_investigations(user_id)
    if df.empty:
        return {}
    hit = df[df["bill_id"] == str(bill_id)]
    return hit.iloc[0].to_dict() if not hit.empty else {}
