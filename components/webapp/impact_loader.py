"""
impact_loader.py
================
Loads nightly impact-prompt results from the gold parquet written by
score_impact_prompts.py on the pipeline server.

Long format — one row per (user x prompt x bill):
  user_id, prompt_id, prompt_label, prompt_title, question,
  bill_id, bill_number, session_id,
  rating_num (0-3), rating_label, explanation, features_hash,
  question_hash, scored_at

Replaces the old Cosmos impact_results reads: the scorer now runs on the
pipeline server (local Llama via vLLM) and publishes to Blob, since the
campus firewall blocks Cosmos from that server.
"""
import pandas as pd
import streamlit as st

from data_catalog import IMPACT_RESULTS_URL


@st.cache_data(ttl=1800, show_spinner=False)
def _load_all_impact_results() -> pd.DataFrame:
    """Full results parquet, cached 30 min (it changes once nightly)."""
    try:
        df = pd.read_parquet(IMPACT_RESULTS_URL)
        df["bill_id"] = df["bill_id"].astype(str)
        return df
    except Exception:
        return pd.DataFrame()


def load_impact_results(user_id: str) -> pd.DataFrame:
    """This user's rows only. Empty DataFrame if nothing scored yet."""
    df = _load_all_impact_results()
    if df.empty or not user_id:
        return pd.DataFrame()
    return df[df["user_id"] == user_id].copy()


def impact_map(user_id: str, prompt_id: str) -> dict:
    """{bill_id: rating_label} for one prompt — the shape the filters use."""
    df = load_impact_results(user_id)
    if df.empty:
        return {}
    df = df[df["prompt_id"] == prompt_id]
    return dict(zip(df["bill_id"], df["rating_label"]))


def impact_map_by_label(user_id: str, prompt_label: str) -> dict:
    """Same, keyed by the user's prompt label (used by Explorer)."""
    df = load_impact_results(user_id)
    if df.empty:
        return {}
    df = df[df["prompt_label"] == prompt_label]
    return dict(zip(df["bill_id"], df["rating_label"]))


def bill_impact_rows(user_id: str, bill_id: str) -> pd.DataFrame:
    """All of this user's prompt ratings for one bill (for bill cards)."""
    df = load_impact_results(user_id)
    if df.empty:
        return pd.DataFrame()
    return df[df["bill_id"] == str(bill_id)][
        ["prompt_label", "prompt_title", "rating_num", "rating_label",
         "explanation", "scored_at"]
    ].sort_values("rating_num", ascending=False)
