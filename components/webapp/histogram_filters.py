"""
histogram_filters.py
====================
Reusable Airbnb/Zillow-style histogram filter controls, shared across
Explorer, Bill Predictions, and anywhere Reaches-Vote Likelihood or Impact
is filtered. Each control renders a small distribution bar chart showing how
many bills fall in each bucket BEFORE the user commits a range, then returns
the selected range so the caller can build a mask.

Design goals:
  * Pure Streamlit + a tiny inline SVG/HTML bar strip (no plotly dependency).
  * Stateless: caller owns the dataframe and the mask; these helpers only
    render the control and return the selection.
  * Terminology is fixed to "Reaches-Vote Likelihood" per the platform standard.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import streamlit as st

# Canonical four-level impact scale (matches the scorer's output exactly).
IMPACT_LEVELS = ["No Impact", "Low Impact", "Moderate Impact", "High Impact"]
IMPACT_SCORE_OF = {lbl: i for i, lbl in enumerate(IMPACT_LEVELS)}
_BAR_COLORS = {"No Impact": "#454F5A", "Low Impact": "#60A5FA",
               "Moderate Impact": "#F59E0B", "High Impact": "#DC2626"}


def _bar_strip(pairs, color="#2D6A7A", height=46):
    """pairs: list of (label, count). Renders a labeled bar strip; returns HTML."""
    counts = [c for _, c in pairs]
    cmax = max(counts) if counts and max(counts) > 0 else 1
    cells = []
    for lbl, c in pairs:
        h = max(3, round(c / cmax * height))
        col = color(lbl) if callable(color) else color
        cells.append(
            f'<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;">'
            f'<div style="font-size:9px;color:#454F5A;font-weight:600">{c:,}</div>'
            f'<div title="{lbl}: {c:,}" style="width:78%;height:{h}px;background:{col};'
            f'border-radius:3px 3px 0 0;"></div>'
            f'<div style="font-size:8.5px;color:#454F5A;text-align:center;line-height:1.1">{lbl}</div>'
            f'</div>')
    return (f'<div style="display:flex;align-items:flex-end;gap:4px;'
            f'padding:6px 2px 2px;min-height:{height + 26}px;">' + "".join(cells) + "</div>")


def likelihood_histogram_filter(df, prob_col, key_prefix, label="Reaches-Vote Likelihood"):
    """Histogram + min/max range selector over probability (stored 0-1).
    Returns (min_pct, max_pct) as ints 0..100. Bins are 10% increments.
    Renders the count-per-bin bars so the user sees the distribution first."""
    if prob_col not in df.columns or df[prob_col].notna().sum() == 0:
        st.caption("Predictions not available for this selection.")
        return (0, 100)

    p = (pd.to_numeric(df[prob_col], errors="coerce").dropna().clip(0, 1) * 100)
    edges = list(range(0, 101, 10))
    counts, _ = np.histogram(p, bins=edges)
    pairs = [(f"{edges[i]}\u2013{edges[i+1]}", int(counts[i])) for i in range(len(counts))]

    st.markdown(f"**{label}**")
    st.markdown(_bar_strip(pairs), unsafe_allow_html=True)
    _rk = f"{key_prefix}_range"
    # select_slider returns a *range* (two values) only when given a two-element
    # `value=`. Do NOT also pre-seed st.session_state[_rk] here — setting a keyed
    # widget's value manually before creating it triggers Streamlit's
    # "default value + Session State" warning. value=(0, 100) supplies the initial
    # default; after that the widget owns the key.
    lo, hi = st.select_slider(
        "Range (%)",
        options=list(range(0, 101, 10)),
        value=(0, 100),
        key=_rk,
        help=f"Bars show how many bills fall in each 10% band of {label.lower()}. "
             f"Drag the ends to set a min/max range.")
    total = int(((p >= lo) & (p <= hi)).sum())
    st.caption(f"**{total:,}** bills between {lo}% and {hi}% {label.lower()}.")
    return (lo, hi)


def impact_histogram_filter(df, score_col, key_prefix, label="Impact"):
    """Histogram of the four impact levels + a multiselect of levels to keep.
    Returns the list of selected level labels (empty = no filter).
    score_col holds 0-3 integer impact scores."""
    if score_col not in df.columns or df[score_col].notna().sum() == 0:
        st.caption("Impact ratings not available for this selection.")
        return []

    s = pd.to_numeric(df[score_col], errors="coerce").dropna().round().astype(int).clip(0, 3)
    counts = s.value_counts().reindex([0, 1, 2, 3]).fillna(0).astype(int)
    pairs = [(IMPACT_LEVELS[i], int(counts[i])) for i in range(4)]

    st.markdown(f"**{label}**")
    st.markdown(_bar_strip(pairs, color=lambda l: _BAR_COLORS.get(l, "#2D6A7A")),
                unsafe_allow_html=True)
    _lk = f"{key_prefix}_levels"
    if _lk not in st.session_state:
        st.session_state[_lk] = []
    selected = st.multiselect(
        "Include impact levels",
        options=IMPACT_LEVELS,
        key=_lk,
        help="Bars show how many bills sit at each impact level. "
             "Select one or more to filter; leave empty to include all.")
    return selected
