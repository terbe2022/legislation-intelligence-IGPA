"""
views/4_Active_Bills.py  —  Bill Predictions
=====================================================
Reads pre-joined snapshot parquets built by build_app_snapshot.py.
Two reads instead of 4+ reads + joins at page load time.
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from auth_utils import require_auth
from chart_a11y import inject_sr_css, sr_block
from data_catalog import (
    GOLD_URLS, PREDICTION_URLS, TRAIL_URL, SESSION_LABELS, MANIFEST_URLS,
    SNAPSHOT_URL, SNAPSHOT_TRAIL_URL,
)
from predictions_loader import PROB_COL, MOMENTUM_COL, PROB_BUCKETS, predictions_freshness
from histogram_filters import likelihood_histogram_filter

require_auth()

# ── Accessibility chrome (ReQtest audit 2026-09-14) ──────────────────────────
# Issues 1/2 (main + nav landmarks), 16 (disabled-state contrast),
# 17/19 (keyboard focus on chart and dataframe overlay controls).
# Must run after require_auth() so it is not emitted on the login gate.
from a11y import apply_page_a11y
apply_page_a11y('Bill Predictions')
inject_sr_css()

DATE_COL        = "status_date"
BILL_ID_COL     = "bill_id"
BILL_NUM_COL    = "bill_number"
TITLE_COL       = "title"
CAT_MAIN        = "category_main_label"
POLICY_COL      = "policy_domain_standardized"
BENEF_PRIMARY   = "beneficiary_primary_category"
IMPACT_COL      = "impact_rating_score"
N_ACTIONS_COL   = "n_actions"
CURRENT_SESSION = 2176

AVAILABLE_SESSIONS = sorted(
    [sid for sid in GOLD_URLS if GOLD_URLS[sid] and PREDICTION_URLS.get(sid)],
    reverse=True,
)

st.markdown("""
<style>
.block-container{padding-top:0.8rem !important;padding-left:0.9rem !important;padding-right:0.9rem !important;}
header[data-testid="stHeader"]{background:transparent !important;height:0 !important;}
[data-testid="stToolbar"]{display:none !important;}
</style>
""", unsafe_allow_html=True)


# ── Snapshot loaders — two reads, no joins ────────────────────────────────────

@st.cache_data(show_spinner=False, ttl=3600)
def load_snapshot() -> pd.DataFrame:
    """Load the pre-joined active-bills snapshot from blob storage."""
    if not SNAPSHOT_URL:
        return pd.DataFrame()
    try:
        df = pd.read_parquet(SNAPSHOT_URL)
        df["bill_id"] = df["bill_id"].astype(str)
        if DATE_COL in df.columns:
            df[DATE_COL] = pd.to_datetime(df[DATE_COL], errors="coerce")
        return df
    except Exception as e:
        st.warning(f"Snapshot load failed: {e}")
        return pd.DataFrame()


@st.cache_data(show_spinner=False, ttl=3600)
def load_snapshot_trail() -> pd.DataFrame:
    """Load the pre-filtered probability trail snapshot."""
    if not SNAPSHOT_TRAIL_URL:
        return pd.DataFrame()
    try:
        df = pd.read_parquet(SNAPSHOT_TRAIL_URL)
        df["bill_id"] = df["bill_id"].astype(str)
        if "action_date" in df.columns:
            df["action_date"] = pd.to_datetime(df["action_date"], errors="coerce")
        return df
    except Exception as e:
        st.warning(f"Trail snapshot load failed: {e}")
        return pd.DataFrame()


# ── Fallback loaders — used when snapshots not yet generated ──────────────────
# Remove these once SNAPSHOT_URL and SNAPSHOT_TRAIL_URL are populated.

_MANIFEST_COLS = ["bill_id", "status", "status_desc", "committee", "committee_id",
                  "last_action", "is_terminal"]

@st.cache_data(show_spinner=False, ttl=3600)
def _load_manifest(sid: int) -> pd.DataFrame:
    url = MANIFEST_URLS.get(sid)
    if not url or url.startswith("REPLACE_WITH"):
        return pd.DataFrame()
    try:
        m = pd.read_parquet(url)
        keep = [c for c in _MANIFEST_COLS if c in m.columns]
        m = m[keep].copy()
        m["bill_id"] = m["bill_id"].astype(str)
        return m
    except Exception:
        return pd.DataFrame()

@st.cache_data(show_spinner=False, ttl=3600)
def _load_features_fallback(session_ids: tuple) -> pd.DataFrame:
    cols = [DATE_COL, BILL_ID_COL, BILL_NUM_COL, TITLE_COL,
            CAT_MAIN, POLICY_COL, BENEF_PRIMARY, IMPACT_COL,
            "llama_summary_raw", "potential_impact_raw", "impact_rating_clean",
            "session_id", "chamber", "n_actions", "last_action_text"]
    dfs = []
    for sid in session_ids:
        url = GOLD_URLS.get(sid)
        if not url: continue
        try:
            d = pd.read_parquet(url)
            keep = [c for c in cols if c in d.columns]
            d = d[keep].copy()
            d["bill_id"] = d["bill_id"].astype(str)
            d["session_id"] = sid
            man = _load_manifest(sid)
            if not man.empty:
                drop_overlap = [c for c in man.columns if c != "bill_id" and c in d.columns]
                if drop_overlap:
                    d = d.drop(columns=drop_overlap)
                d = d.merge(man, on="bill_id", how="left")
            dfs.append(d)
        except Exception as e:
            st.warning(f"Session {sid} failed: {e}")
    return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()

@st.cache_data(show_spinner=False, ttl=3600)
def _load_trail_fallback() -> pd.DataFrame:
    if not TRAIL_URL: return pd.DataFrame()
    try:
        df = pd.read_parquet(TRAIL_URL)
        df["bill_id"] = df["bill_id"].astype(str)
        df["action_date"] = pd.to_datetime(df["action_date"], errors="coerce")
        return df
    except Exception:
        return pd.DataFrame()


# Category base rates (historical pass rate per category)
BASE_RATES = {
    "Healthcare & Medicaid": 0.1883,
    "Government & Public Administration": 0.1632,
    "Education": 0.1547,
    "Criminal Justice": 0.1323,
    "Employment & Labor": 0.0886,
    "Finance & Taxation": 0.1204,
    "Environment": 0.0943,
    "Transportation": 0.1456,
    "Social Services & Human Welfare": 0.1211,
}

STAGE_COLORS = {
    "Filed":                "#888780",
    "Referred":             "#888780",
    "Committee Referral":   "#378ADD",
    "Committee Action":     "#1D9E75",
    "Committee Action (Held)":"#E24B4A",
    "Sponsorship":          "#7F77DD",
    "Floor":                "#BA7517",
    "Inter-Chamber":        "#185FA5",
    "Amendment":            "#993C1D",
    "Deadline":             "#888780",
    "Enrolled":             "#185FA5",
    "Governor":             "#3B6D11",
}

# Ordered stage ladder for the cutoff filter — built on status_desc, LegiScan's
# own canonical per-bill status (same field already powering the "by stage"
# KPI chips above). Vetoed sits last chronologically — it can only happen
# after Passed — even though it's a failure outcome, not forward progress.
# NOTE: status_desc is coarse. Most active bills show "Introduced" for their
# entire first-chamber journey (committee referrals, committee votes, floor
# action) — LegiScan only advances this field at full-chamber milestones.
# This filter distinguishes chamber-crossing progress, not in-chamber detail.
STATUS_STAGE_LADDER = ["Introduced", "Engrossed", "Enrolled", "Passed", "Vetoed"]
STATUS_STAGE_RANK   = {s.lower(): i for i, s in enumerate(STATUS_STAGE_LADDER)}

def detect_stage(action_text: str) -> str:
    t = action_text.lower()
    if any(k in t for k in ["governor", "enrolled", "approved", "signed"]): return "Governor"
    if any(k in t for k in ["passed both houses", "enrolled"]): return "Enrolled"
    if "inter-chamber" in t or "arrive in" in t or "arrived in" in t: return "Inter-Chamber"
    if any(k in t for k in ["third reading", "second reading", "floor"]): return "Floor"
    if "amendment" in t: return "Amendment"
    if any(k in t for k in ["do pass", "do fail", "held", "tabled"]): return "Committee Action"
    if any(k in t for k in ["assigned to", "referred to assignments", "placed on calendar"]): return "Committee Referral"
    if any(k in t for k in ["co-sponsor", "sponsor"]): return "Sponsorship"
    if any(k in t for k in ["referred to rules", "first reading", "referred"]): return "Referred"
    if any(k in t for k in ["filed", "prefiled"]): return "Filed"
    if "deadline" in t or "rule 2-10" in t: return "Deadline"
    return "Referred"


# ── Session scope — its VALUE gates what's fetched, so it's read here ────────
# Live finding (2026-10-02): the checkbox itself was drawn here too, above the
# page's H1 banner, so screen-reader users met a filter before the page title.
# Streamlit keeps a keyed widget's value in session_state, so read it now and
# draw the checkbox below the banner (see "Session scope control" below).
current_only = st.session_state.get("ab_current_only", True)
_sessions_to_load = (CURRENT_SESSION,) if current_only else tuple(AVAILABLE_SESSIONS)

# ── Load data — snapshot path (fast) or fallback (slower) ────────────────────
_use_snapshot = bool(SNAPSHOT_URL and SNAPSHOT_TRAIL_URL)

with st.spinner("Loading bills…"):
    if _use_snapshot:
        # Fast path: 2 small pre-joined reads, no joins, no filtering
        df_all   = load_snapshot()
        trail_df = load_snapshot_trail()
        # snapshot is already current-session only; if user wants all sessions,
        # fall through to the full load for now (snapshot covers 2176 only)
        if not current_only:
            df_all_extra = _load_features_fallback(
                tuple(s for s in _sessions_to_load if s != CURRENT_SESSION)
            )
            if not df_all_extra.empty:
                df_all = pd.concat([df_all, df_all_extra], ignore_index=True)
            trail_df_extra = _load_trail_fallback()
            if not trail_df_extra.empty:
                extra_ids = set(df_all_extra["bill_id"].astype(str)) if not df_all_extra.empty else set()
                trail_df_extra = trail_df_extra[trail_df_extra["bill_id"].isin(extra_ids)]
                trail_df = pd.concat([trail_df, trail_df_extra], ignore_index=True)
    else:
        # Fallback: original multi-parquet load (used until snapshots are generated)
        df_all   = _load_features_fallback(_sessions_to_load)
        trail_df = _load_trail_fallback()
        if current_only and not trail_df.empty and not df_all.empty:
            _loaded_ids = set(df_all["bill_id"].astype(str))
            trail_df = trail_df[trail_df["bill_id"].isin(_loaded_ids)]

if df_all.empty:
    st.error("No session data. Check GOLD_URLS in data_catalog.py.")
    st.stop()

# Snapshot already has passage_prob joined in; fallback data needs it added.
if not _use_snapshot or PROB_COL not in df_all.columns:
    from predictions_loader import join_predictions
    df_all = join_predictions(df_all, session_ids=_sessions_to_load)
if PROB_COL not in df_all.columns:
    st.warning("Predictions not yet available.")
    st.stop()

# ── ENROLLMENT DETECTION — must run on df_all BEFORE the PROB_COL filter ─────
# Bug: originally ran on `active` (bills-with-predictions only).  Enrolled bills
# often have no Stage 2 prediction (pipeline treats them as terminal), so they
# were silently excluded and the "In enrollment" KPI always showed 0.
def _is_enrolled(row) -> bool:
    # Canonical signal: LegiScan status_desc == "Enrolled" (exact). This is the
    # clean, unambiguous flag (gives the true enrolled count). We deliberately do
    # NOT fuzzy-match "governor"/"approved"/"passed" here, because those are LATER
    # stages than enrollment and would inflate/conflate the count.
    status = str(row.get("status_desc", "") or "").strip().lower()
    return status == "enrolled"

_have_enroll_cols = any(c in df_all.columns for c in ["status_desc", "last_action", "last_action_text"])
if _have_enroll_cols:
    df_all["_enrolled"] = df_all.apply(_is_enrolled, axis=1)
else:
    df_all["_enrolled"] = False

# Enrolled bills (passed both chambers) legitimately have no Stage 2 score.
# Pin their probability to 1.0 so they pass the .notna() filter and appear in
# the card list.  Stage 3 will overwrite this with a real vote-outcome score.
# Momentum: enrolled/complete bills should read "Complete", not None/blank.
if MOMENTUM_COL in df_all.columns:
    df_all[MOMENTUM_COL] = df_all[MOMENTUM_COL].astype("object")
    _blank_mom = (df_all[MOMENTUM_COL].isna()
                  | df_all[MOMENTUM_COL].astype(str).str.strip()
                        .isin(["", "None", "nan"]))
    df_all.loc[df_all["_enrolled"], MOMENTUM_COL] = "Complete"
    df_all.loc[_blank_mom & ~df_all["_enrolled"], MOMENTUM_COL] = "Stable"

_enrolled_no_pred = df_all["_enrolled"] & df_all[PROB_COL].isna()
if _enrolled_no_pred.any():
    df_all.loc[_enrolled_no_pred, PROB_COL] = 1.0

active = df_all[df_all[PROB_COL].notna()].copy()
if DATE_COL in active.columns:
    active[DATE_COL] = pd.to_datetime(active[DATE_COL], errors="coerce")

if active.empty:
    st.info("No active bills found.")
    st.stop()

now_utc = pd.Timestamp(datetime.now(timezone.utc)).tz_localize(None)

if DATE_COL in active.columns:
    dates = active[DATE_COL]
    if dates.dt.tz is not None:
        dates = dates.dt.tz_localize(None)
    days_since = (now_utc - dates).dt.days.clip(lower=0).fillna(365)
    active["_recency"] = np.exp(-days_since / 180).clip(0, 1)
else:
    active["_recency"] = 0.5

_MOM_RANK = {"Gaining": 2, "Stable": 1, "Losing": 0}
active["_mom_rank"] = active[MOMENTUM_COL].map(_MOM_RANK).fillna(1) if MOMENTUM_COL in active.columns else 1
active["_n_actions"] = pd.to_numeric(active[N_ACTIONS_COL], errors="coerce").fillna(0) if N_ACTIONS_COL in active.columns else pd.Series(0, index=active.index)
active["_imminence"] = (active[PROB_COL].fillna(0) * 0.60
                       + active["_recency"] * 0.25
                       + (active["_mom_rank"] / 2) * 0.15)
active["_session_label"] = active["session_id"].map(SESSION_LABELS).fillna(active["session_id"].astype(str))

# Current stage — read directly from status_desc (LegiScan's canonical bill
# status), not derived from action text. Unrecognized/blank status_desc
# defaults to rank 0 (Introduced): most active bills haven't crossed a
# full-chamber milestone yet, so treating unknowns as "latest" would
# silently hide them from every cutoff except "All stages."
if "status_desc" in active.columns:
    _sd_norm = active["status_desc"].fillna("").astype(str).str.strip().str.lower()
    _rank_to_label = {i: s for i, s in enumerate(STATUS_STAGE_LADDER)}
    active["_stage_rank"]    = _sd_norm.map(STATUS_STAGE_RANK).fillna(0).astype(int)
    active["_current_stage"] = active["_stage_rank"].map(_rank_to_label).fillna("Introduced")
else:
    active["_current_stage"] = "Introduced"
    active["_stage_rank"]    = 0

# (_enrolled already set on df_all above and propagates through active → view)

# Header
st.markdown(f"""
<div style="background:linear-gradient(90deg,#0F1F38 0%,#1B4B5A 60%,#6E5A50 100%);
            padding:12px 16px;border-radius:12px;margin-bottom:10px;">
  <h1 style="margin:0;padding:0;color:#fff;font-weight:800;font-size:20px;">
    Bill Predictions</h1>
  <div style="color:rgba(255,255,255,.88);font-size:12px;margin-top:2px;">
    Live survival-to-vote predictions · {len(active):,} active bills across {len(AVAILABLE_SESSIONS)} session(s)
  </div>
</div>
""", unsafe_allow_html=True)
st.caption(predictions_freshness(AVAILABLE_SESSIONS))

# Session scope control (value read near the top of the page; drawn here so
# it follows the page title).
st.checkbox("104th GA only (current session)", value=True, key="ab_current_only")

# Filters
# Filters — row 1
fc1, fc2, fc3 = st.columns([3, 3, 2])
with fc1:
    # Audit issue 12 (WCAG 3.3.2): the label was an empty string, so this
    # field had no accessible name whatsoever and the placeholder was the
    # only cue. Label restored and kept visible.
    search = st.text_input("Search bills",
                           placeholder="Search bill number or title…",
                           help="Matches on bill number or any word in the title.")
with fc2:
    _sys_cats = (sorted(v for v in active[CAT_MAIN].dropna().astype(str).str.strip().unique() if v)
                 if CAT_MAIN in active.columns else [])
    # User-created categories (from My Tracker) offered alongside system ones.
    _user_cat_defs = {}
    try:
        from user_profile import list_saved_items
        _urec = st.session_state.get("user_record") or {}
        _uid  = _urec.get("email", "")
        if _uid:
            for _c in list_saved_items(_uid, "category"):
                _lbl = _c.get("label", "")
                if _lbl:
                    _user_cat_defs[f"Mine: {_lbl}"] = _c.get("data", {})
    except Exception:
        _user_cat_defs = {}
    _cat_options = _sys_cats + list(_user_cat_defs.keys())
    sel_cat = st.multiselect(
        "Category", _cat_options,
        help="System categories plus your own saved categories (prefixed 'Mine:').")
with fc3:
    _rvl_lo, _rvl_hi = likelihood_histogram_filter(active, PROB_COL, "pred_rvl")

# Filters — row 2
fc4, fc5 = st.columns([2, 2])
with fc4:
    sort_choice = st.selectbox("Sort by", [
        "Most imminent",       "Least imminent",
        "Highest probability", "Lowest probability",
        "Gaining momentum first", "Losing momentum first",
        "Most recent activity",   "Least recent activity",
        "Enrollment first",
    ])
with fc5:
    st.markdown("**Include stages**")
    _stage_cols = st.columns(len(STATUS_STAGE_LADDER))
    _selected_stages = []
    for _i, _stg in enumerate(STATUS_STAGE_LADDER):
        with _stage_cols[_i]:
            if st.checkbox(_stg, value=True, key=f"stage_cb_{_stg}"):
                _selected_stages.append(_stg)

# Apply filters
view = active.copy()
# (session scope already applied at load time via _sessions_to_load)
if search:
    sq = search.lower()
    mask = view[BILL_NUM_COL].astype(str).str.lower().str.contains(sq, na=False)
    if TITLE_COL in view.columns:
        mask |= view[TITLE_COL].astype(str).str.lower().str.contains(sq, na=False)
    view = view[mask]
if sel_cat:
    _sys_selected  = [c for c in sel_cat if not c.startswith("Mine: ")]
    _user_selected = [c for c in sel_cat if c.startswith("Mine: ")]
    _cat_mask = pd.Series(False, index=view.index)
    if _sys_selected and CAT_MAIN in view.columns:
        _cat_mask |= view[CAT_MAIN].astype(str).isin(_sys_selected)
    for _uc in _user_selected:
        _d = _user_cat_defs.get(_uc, {})
        _kws, _fls = _d.get("keywords", []), _d.get("fields", [])
        if _d.get("type") == "keyword" and _kws and _fls:
            for _kw in _kws:
                for _f in _fls:
                    if _f in view.columns:
                        _cat_mask |= (view[_f].astype(str).str.lower()
                                      .str.contains(_kw.lower(), na=False))
        else:
            _cm  = {"policy_domain": "policy_domain_standardized",
                    "beneficiary":   "beneficiary_primary_category"}
            _col = _cm.get(_d.get("type", ""), CAT_MAIN)
            if _col in view.columns:
                _cat_mask |= view[_col].astype(str).str.contains(
                    str(_d.get("value", "")), case=False, na=False)
    view = view[_cat_mask]

# Reaches-vote likelihood range (histogram slider, 0–100)
_pp = pd.to_numeric(view[PROB_COL], errors="coerce").fillna(0) * 100
view = view[(_pp >= _rvl_lo) & (_pp <= _rvl_hi)]

# Multi-stage checkboxes (any selected stage matches)
if _selected_stages and len(_selected_stages) < len(STATUS_STAGE_LADDER):
    _want_ranks = {STATUS_STAGE_RANK.get(s.lower(), 99) for s in _selected_stages}
    view = view[view["_stage_rank"].isin(_want_ranks)]
elif not _selected_stages:
    view = view.iloc[0:0]  # no stages checked → empty

if view.empty:
    st.info("No bills match these filters.")
    st.stop()

# Sort
if sort_choice == "Most imminent":
    view = view.sort_values("_imminence", ascending=False)
elif sort_choice == "Least imminent":
    view = view.sort_values("_imminence", ascending=True)
elif sort_choice == "Highest probability":
    view = view.sort_values(PROB_COL, ascending=False)
elif sort_choice == "Lowest probability":
    view = view.sort_values(PROB_COL, ascending=True)
elif sort_choice == "Gaining momentum first":
    view = view.sort_values(["_mom_rank", PROB_COL], ascending=[False, False])
elif sort_choice == "Losing momentum first":
    view = view.sort_values(["_mom_rank", PROB_COL], ascending=[True, False])
elif sort_choice == "Most recent activity":
    if DATE_COL in view.columns:
        view = view.sort_values(DATE_COL, ascending=False)
elif sort_choice == "Least recent activity":
    if DATE_COL in view.columns:
        view = view.sort_values(DATE_COL, ascending=True)
else:  # Enrollment first
    view = view.sort_values(["_enrolled", PROB_COL], ascending=[False, False])

# KPIs
n_total   = len(view)
n_high    = int((view[PROB_COL] >= 0.65).sum())
n_gaining = int((view[MOMENTUM_COL] == "Gaining").sum()) if MOMENTUM_COL in view.columns else 0
avg_prob  = f"{view[PROB_COL].mean()*100:.0f}%" if n_total else "—"

# ── Stage breakdown over ALL bills in the session (not just predicted ones) ──
# Counts by canonical LegiScan status_desc. Scoped to the current session when
# the "104th GA only" filter is on, else across all loaded sessions. Past/failed
# (Vetoed) is therefore session-scoped too, per design.
def _compute_stage_counts(frame) -> dict:
    if "status_desc" not in frame.columns:
        return {}
    sd = frame["status_desc"].fillna("").astype(str).str.strip().str.lower()
    return {
        "Introduced": int((sd == "introduced").sum()),
        "Engrossed":  int((sd == "engrossed").sum()),
        "Enrolled":   int((sd == "enrolled").sum()),
        "Passed":     int((sd == "passed").sum()),
        "Vetoed":     int((sd == "vetoed").sum()),
    }

_stage_base = (df_all[df_all["session_id"] == CURRENT_SESSION]
               if (current_only and "session_id" in df_all.columns)
               else df_all)
stage_counts = _compute_stage_counts(_stage_base)
# Ordered list of (label, count, color) for rendering; skip any zero-count stage
# EXCEPT Enrolled (always show it, since it's the Stage-3 trigger users care about).
_STAGE_ORDER = [
    ("Introduced", "#5F5E5A"),   # regression finding: was #888780 (3.61:1)
    ("Engrossed",  "#534AB7"),   # was #7F77DD (3.76:1)
    ("Enrolled",   "#185FA5"),
    ("Passed",     "#3B6D11"),
    ("Vetoed",     "#B42318"),   # was #E24B4A (fails as text)
]
_stage_chips = "".join(
    f'<span style="display:inline-block;margin:0 6px 2px 0;font-size:10px;'
    f'color:var(--color-text-secondary);white-space:nowrap;">'
    f'<b style="color:{color};font-size:12px;">{stage_counts.get(label,0):,}</b> {label}</span>'
    for label, color in _STAGE_ORDER
    if stage_counts.get(label, 0) > 0 or label == "Enrolled"
)

st.markdown(f"""
<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:8px 0 12px;">
  <div style="background:var(--color-background-secondary);border-radius:10px;padding:8px 10px;text-align:center;">
    <div class="kpi-read"><span class="a11y-sr">{n_total:,} active bills</span>
      <span aria-hidden="true" style="display:block;font-size:22px;font-weight:600;">{n_total:,}</span>
      <span aria-hidden="true" style="display:block;font-size:10px;color:#454F5A;margin-top:1px;">Active bills</span></div>
  </div>
  <div style="background:var(--color-background-secondary);border-radius:10px;padding:8px 10px;text-align:center;">
    <div style="font-size:10px;color:#454F5A;margin-bottom:3px;font-weight:600;text-transform:uppercase;letter-spacing:.03em;">By stage</div>
    <div style="line-height:1.5;">{_stage_chips}</div>
  </div>
  <div style="background:var(--color-background-secondary);border-radius:10px;padding:8px 10px;text-align:center;">
    <div class="kpi-read"><span class="a11y-sr">{n_gaining:,} gaining momentum</span>
      <span aria-hidden="true" style="display:block;font-size:22px;font-weight:600;color:#3B6D11;">{n_gaining:,}</span>
      <span aria-hidden="true" style="display:block;font-size:10px;color:#454F5A;margin-top:1px;">Gaining momentum</span></div>
  </div>
  <div style="background:var(--color-background-secondary);border-radius:10px;padding:8px 10px;text-align:center;">
    <div class="kpi-read"><span class="a11y-sr">Average probability of reaching a vote: {avg_prob}</span>
      <span aria-hidden="true" style="display:block;font-size:22px;font-weight:600;">{avg_prob}</span>
      <span aria-hidden="true" style="display:block;font-size:10px;color:#454F5A;margin-top:1px;">Avg P(reaches vote)</span></div>
  </div>
</div>
""", unsafe_allow_html=True)

MAX_CARDS = 200
view_top = view.head(MAX_CARDS).reset_index(drop=True)
if len(view) > MAX_CARDS:
    st.caption(f"Showing top {MAX_CARDS} of {len(view):,} bills. Use filters to narrow.")


def _last_action_str(row) -> str:
    if DATE_COL in row.index and pd.notna(row[DATE_COL]):
        d = pd.Timestamp(row[DATE_COL])
        if d.tzinfo: d = d.tz_localize(None)
        days = (now_utc - d).days
        if days < 1:   return "today"
        if days < 7:   return f"{days}d ago"
        if days < 60:  return f"{days // 7}w ago"
        if days < 365: return f"{days // 30}mo ago"
        return f"{days // 365}yr ago"
    return "—"


# Build trail index for fast lookup
trail_index = {}
if not trail_df.empty and "bill_id" in trail_df.columns:
    for bid, grp in trail_df.groupby("bill_id"):
        trail_index[str(bid)] = grp.sort_values("action_index").to_dict("records")


bills_data = []
for i, row in view_top.iterrows():
    prob       = float(row.get(PROB_COL, 0) or 0)
    mom        = str(row.get(MOMENTUM_COL, "Stable") or "Stable")
    cat        = str(row.get(CAT_MAIN, "") or "")
    pol        = str(row.get(POLICY_COL, "") or "")
    ben        = str(row.get(BENEF_PRIMARY, "") or "")
    sess       = str(row.get("_session_label", "") or "")
    n_act      = int(row.get("_n_actions", 0) or 0)
    last_str   = _last_action_str(row)
    bill_num   = str(row.get(BILL_NUM_COL, "") or "")
    title      = str(row.get(TITLE_COL, "") or "")
    bid        = str(row.get(BILL_ID_COL, "") or "")
    chamber    = str(row.get("chamber", "") or "")
    summary    = str(row.get("llama_summary_raw", "") or "")[:300]
    impact_txt = str(row.get("potential_impact_raw", "") or "")[:250]
    impact_rtg = str(row.get("impact_rating_clean", "") or "")
    enrolled   = bool(row.get("_enrolled", False))
    status_desc= str(row.get("status_desc", "") or "")
    last_action_text = str(row.get("last_action", "") or "")
    base_rate  = BASE_RATES.get(cat, 0)

    # Build probability trail actions for Chart.js
    trail_actions = []
    bill_trail = trail_index.get(bid, [])
    if bill_trail:
        for t in bill_trail:
            action_text = str(t.get("action_text", t.get("action", "")) or "")
            action_date = t.get("action_date")
            y_prob      = float(t.get("y_prob", 0) or 0)
            date_str    = ""
            if action_date is not None:
                try:
                    d = pd.Timestamp(action_date)
                    if pd.notna(d):
                        date_str = d.strftime("%b %-d")
                except Exception:
                    date_str = str(action_date)[:10] if action_date else ""
            stage = detect_stage(action_text)
            trail_actions.append({
                "date":  date_str,
                "text":  action_text[:80],
                "stage": stage,
                "prob":  round(y_prob, 3),
            })
    else:
        # No trail yet — show placeholder with current prob as last point
        trail_actions = [{"date": "Current", "text": "Current score", "stage": "Filed", "prob": round(prob, 3)}]

    ilga_base = bill_num.replace("HB","").replace("SB","") if bill_num else ""
    doc_type  = "HB" if bill_num.startswith("H") else "SB"
    legiscan_url = f"https://legiscan.com/IL/bill/{bill_num}/2025" if bill_num else "#"
    ilga_url = (f"https://www.ilga.gov/legislation/BillStatus.asp?DocNum={ilga_base}&GAID=18&DocTypeID={doc_type}"
                if ilga_base else "#")

    bills_data.append({
        "rank":         i + 1,
        "id":           bill_num or bid,
        "title":        title[:120] + ("…" if len(title) > 120 else ""),
        "summary":      summary,
        "category":     cat,
        "policy":       pol,
        "beneficiary":  ben,
        "impact_txt":   impact_txt,
        "impact_rtg":   impact_rtg,
        "chamber":      chamber,
        "session":      sess,
        "prob":         round(prob, 4),
        "momentum":     mom,
        "n_actions":    n_act,
        "last_action":  last_str,
        "last_action_text": last_action_text[:100],
        "status_desc":  status_desc[:80],
        "enrolled":     enrolled,
        "base_rate":    round(base_rate, 4),
        "actions":      trail_actions,
        "legiscan_url": legiscan_url,
        "ilga_url":     ilga_url,
    })

bills_json = json.dumps(bills_data)

_BILLS_PLACEHOLDER = "___BILLS_JSON___"
widget_html = f"""
<style>
/* Streamlit's theme CSS variables are NOT available inside a components.html
   iframe (separate document), so the bill cards rendered transparent/invisible.
   Define them here with the app's light palette so the cards style correctly. */
:root{{
  --color-text-primary:#1A1A17;
  --color-text-secondary:#454F5A;   /* bug 3: was #5F5E5A (6.49:1) -> 8.34:1 */
  --color-text-tertiary:#55606B;    /* bug 3: was #8A8985 (3.5:1, FAILED) -> 6.42:1 */
  --color-background-primary:#FFFFFF;
  --color-background-secondary:#F1EFE8;
  --color-border-tertiary:#E4E2DA;
  --color-border-secondary:#C9C7BF;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:var(--font-sans,system-ui,sans-serif);color:var(--color-text-primary);background:transparent;font-size:14px}}
.bill-card{{border:0.5px solid var(--color-border-tertiary);border-radius:12px;padding:11px 13px;
           margin-bottom:8px;background:var(--color-background-primary);cursor:pointer;transition:border-color .15s}}
.bill-card:hover{{border-color:var(--color-border-secondary)}}
.bill-card.expanded{{border-color:#185FA5}}
/* Accessibility (audit follow-up): the card was a <div onclick>, so the detail
   panel could not be opened by keyboard at all and nothing announced whether a
   card was expanded. This is a real <button> carrying aria-expanded and
   aria-controls, so it is reachable by Tab, operable with Enter/Space for free,
   and its state is announced. */
.bill-toggle{{display:inline-flex;align-items:center;gap:4px;margin-top:4px;
  padding:3px 9px;font-size:11px;font-weight:600;font-family:inherit;
  color:#1A2733;background:#F1F5F9;border:1px solid #8A96A0;border-radius:6px;
  cursor:pointer;line-height:1.4}}
.bill-toggle:hover{{background:#E2E8F0}}
.bill-toggle:focus-visible{{outline:3px solid #0B5FA5;outline-offset:2px}}
.bill-toggle[aria-expanded="true"] .bill-toggle-ico{{transform:rotate(180deg)}}
.bill-toggle-ico{{display:inline-block;transition:transform .15s}}
.bill-card.enrollment{{border-left:3px solid #185FA5;border-radius:0 12px 12px 0}}
.bill-header{{display:flex;align-items:flex-start;gap:9px}}
.rank{{width:20px;height:20px;border-radius:50%;background:var(--color-background-secondary);
       display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:500;
       color:var(--color-text-secondary);flex-shrink:0;margin-top:2px}}
.bill-num{{font-size:12px;font-weight:500;color:#185FA5;white-space:nowrap}}
.bill-title{{font-size:13px;font-weight:500;line-height:1.4}}
.bill-summary{{font-size:12px;color:var(--color-text-secondary);line-height:1.5;margin-top:4px;font-style:italic}}
.tag{{font-size:10px;padding:2px 6px;border-radius:7px;background:var(--color-background-secondary);
     color:var(--color-text-secondary);border:0.5px solid var(--color-border-tertiary);white-space:nowrap}}
.mbadge{{font-size:11px;font-weight:500;padding:2px 7px;border-radius:8px;white-space:nowrap}}
.impact-badge{{display:inline-block;font-size:10px;font-weight:600;padding:2px 8px;border-radius:6px;margin-left:4px}}
.bill-detail{{margin-top:10px;padding-top:10px;border-top:0.5px solid var(--color-border-tertiary);display:none}}
.bill-card.expanded .bill-detail{{display:block}}
.sbar-wrap{{width:44px;height:3px;background:var(--color-border-tertiary);border-radius:2px;overflow:hidden;margin-top:3px}}
.sbar{{height:100%;border-radius:2px}}
.s3-banner{{background:#E6F1FB;border-radius:8px;padding:6px 10px;margin-bottom:8px;
           display:flex;align-items:center;gap:8px;font-size:11px;color:#0C447C;font-weight:500}}
.detail-lbl{{font-size:10px;color:var(--color-text-secondary);text-transform:uppercase;
            letter-spacing:.04em;margin-bottom:3px;font-weight:600;margin-top:8px}}
.detail-val{{font-size:12px;line-height:1.5;margin-bottom:6px}}
.action-scroll{{max-height:200px;overflow-y:auto;border:0.5px solid var(--color-border-tertiary);
               border-radius:6px;padding:4px 0}}
.action-row{{display:flex;align-items:center;gap:6px;padding:4px 8px;
            border-bottom:0.5px solid var(--color-border-tertiary);font-size:11px}}
.action-row:last-child{{border-bottom:none}}
.adot{{width:7px;height:7px;border-radius:50%;flex-shrink:0}}
.adate{{font-size:10px;color:var(--color-text-tertiary);width:38px;flex-shrink:0}}
.atext{{flex:1;color:var(--color-text-secondary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.aprob{{font-size:10px;font-weight:600;flex-shrink:0}}
.chart-wrap{{height:90px;margin:8px 0 4px;position:relative}}
.ext-link{{font-size:11px;color:#185FA5;text-decoration:none;margin-right:10px}}
.a11y-sr{{position:absolute!important;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}}
.ext-link:hover{{text-decoration:underline}}
.no-results{{text-align:center;padding:20px;color:var(--color-text-secondary);font-size:13px}}
</style>

<div id="blist"></div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
try{{window.frameElement&&window.frameElement.setAttribute('title','Bill predictions list');}}catch(e){{}}
const BILLS=___BILLS_JSON___;
const SCOL={{"Filed":"#888780","Referred":"#888780","Committee Referral":"#378ADD","Committee Action":"#1D9E75","Committee Action (Held)":"#E24B4A","Sponsorship":"#7F77DD","Floor":"#BA7517","Inter-Chamber":"#185FA5","Amendment":"#993C1D","Deadline":"#888780","Enrolled":"#185FA5","Governor":"#3B6D11"}};

const charts={{}};

function pc(p){{return p>=.65?"#3B6D11":p>=.35?"#BA7517":"#A32D2D"}}

function ms(m,enrolled){{
  if(enrolled) return{{l:"In Enrollment",c:"#0C447C",bg:"#E6F1FB",a:""}};
  if(m==="Gaining") return{{l:"Gaining",c:"#3B6D11",bg:"#EAF3DE",a:"↗"}};
  if(m==="Losing")  return{{l:"Losing",c:"#854F0B",bg:"#FAEEDA",a:"↘"}};
  return{{l:"Stable",c:"#5F5E5A",bg:"#F1EFE8",a:"→"}};
}}

function ic(r){{
  if(!r) return{{c:"#5F5E5A",bg:"#F1EFE8"}};
  const rl=r.toLowerCase();
  if(rl.includes("transform")) return{{c:"#A32D2D",bg:"#FCEBEB"}};
  if(rl.includes("high"))      return{{c:"#854F0B",bg:"#FAEEDA"}};
  if(rl.includes("medium")||rl.includes("moderate")) return{{c:"#185FA5",bg:"#EBF2FF"}};
  return{{c:"#5F5E5A",bg:"#F1EFE8"}};
}}

function toggle(id){{
  const card=document.getElementById("c-"+id);
  const was=card.classList.contains("expanded");
  document.querySelectorAll(".bill-card").forEach(c=>c.classList.remove("expanded"));
  // Accessibility (audit follow-up): only one card is open at a time, so every
  // other card's button must be reset to collapsed or the announced state
  // drifts out of step with what is actually on screen.
  document.querySelectorAll(".bill-toggle").forEach(function(btn){{
    btn.setAttribute("aria-expanded","false");
    const t=btn.querySelector(".bill-toggle-txt"); if(t) t.textContent="Details";
  }});
  if(!was){{
    card.classList.add("expanded");
    const btn=document.getElementById("t-"+id);
    if(btn){{
      btn.setAttribute("aria-expanded","true");
      const t=btn.querySelector(".bill-toggle-txt"); if(t) t.textContent="Hide";
    }}
    const bill=BILLS.find(b=>b.id===id);
    setTimeout(()=>{{
      if(charts["m"+id]){{charts["m"+id].destroy();}}
      const cv=document.getElementById("mc"+id);
      if(!cv||!bill.actions.length) return;
      const probs=bill.actions.map(a=>Math.round(a.prob*100));
      const labels=bill.actions.map(a=>a.date);
      const col=pc(bill.prob);
      charts["m"+id]=new Chart(cv,{{
        type:"line",
        data:{{labels,datasets:[{{
          data:probs,borderColor:col,borderWidth:2,
          pointRadius:4,pointBackgroundColor:col,
          fill:true,backgroundColor:col+"18",tension:.3
        }}]}},
        options:{{
          responsive:true,maintainAspectRatio:false,
          plugins:{{
            legend:{{display:false}},
            tooltip:{{callbacks:{{label:c=>c.parsed.y+"%"}}}}
          }},
          scales:{{
            x:{{ticks:{{font:{{size:9}},maxRotation:0,autoSkip:true,maxTicksLimit:8}},grid:{{display:false}}}},
            y:{{min:0,max:100,ticks:{{callback:v=>v+"%",font:{{size:9}}}},grid:{{color:"rgba(128,128,128,.07)"}}}}
          }}
        }}
      }});
    }},50);
  }}
}}

const list=document.getElementById("blist");
if(!BILLS.length){{
  list.innerHTML='<div class="no-results">No bills match filters.</div>';
}} else {{
  list.innerHTML=BILLS.map(b=>{{
    const pct=Math.round(b.prob*100);
    const mo=ms(b.momentum,b.enrolled);
    const col=pc(b.prob);
    const impC=ic(b.impact_rtg);

    const tags=[
      b.category?`<span class="tag">${{b.category}}</span>`:"",
      b.chamber?`<span class="tag">${{b.chamber}}</span>`:"",
      b.session?`<span class="tag">${{b.session}}</span>`:"",
      b.enrolled?`<span class="tag" style="background:#E6F1FB;color:#0C447C;border-color:#B5D4F4">Enrollment</span>`:"",
    ].filter(Boolean).join(" ");

    const impBadge=b.impact_rtg?
      `<span class="impact-badge" style="background:${{impC.bg}};color:${{impC.c}}">${{b.impact_rtg}}</span>`:"";

    const s3banner=b.enrolled?`
      <div class="s3-banner">
        Enrollment — voter roster known.
        <strong style="margin-left:4px">Stage 3 vote prob: pending</strong>
        <span style="margin-left:auto;font-size:10px;color:#454F5A">OpenSecrets layer: pending</span>
      </div>`:"";

    const arows=b.actions.slice().reverse().map(a=>`
      <div class="action-row">
        <div class="adot" style="background:${{SCOL[a.stage]||"#888"}}"></div>
        <span class="adate">${{a.date}}</span>
        <span class="atext" title="${{a.text}}">${{a.text}}</span>
        <span class="aprob" style="color:${{pc(a.prob)}}">${{Math.round(a.prob*100)}}%</span>
      </div>`).join("");

    const baseRateStr=b.base_rate>0?
      `<div style="font-size:10px;color:var(--color-text-tertiary);margin-top:2px">
        Historical ${{b.category||"category"}} base rate: ${{Math.round(b.base_rate*100)}}%
      </div>`:"";

    const statusStr=b.status_desc?
      `<div style="font-size:11px;color:var(--color-text-secondary);margin-top:2px">
        Status: ${{b.status_desc}}
      </div>`:"";

    return `<div class="bill-card${{b.enrolled?" enrollment":""}}" id="c-${{b.id}}" onclick="toggle('${{b.id}}')">
      <div class="bill-header">
        <div class="rank">${{b.rank}}</div>
        <div style="flex:1;min-width:0">
          <div style="display:flex;align-items:center;gap:5px;margin-bottom:3px;flex-wrap:wrap">
            <span class="bill-num">${{b.id}}</span>${{tags}}${{impBadge}}
          </div>
          <div class="bill-title">${{b.title}}</div>
          ${{b.summary?`<div class="bill-summary">${{b.summary}}</div>`:""}}
          <div style="font-size:11px;color:var(--color-text-secondary);margin-top:3px">
            ${{b.n_actions>0?b.n_actions+" actions · ":""}}Last activity: ${{b.last_action}}
          </div>
          ${{statusStr}}
          ${{baseRateStr}}
        </div>
        <div style="display:flex;flex-direction:column;align-items:flex-end;gap:5px;flex-shrink:0;margin-left:10px">
          <div style="font-size:26px;font-weight:500;color:${{col}};line-height:1">${{pct}}%</div>
          <div style="font-size:10px;color:var(--color-text-secondary)">P(reaches vote)</div>
          <div class="mbadge" style="background:${{mo.bg}};color:${{mo.c}}"><span aria-hidden="true">${{mo.a}}</span> ${{mo.l}}</div>
          <div class="sbar-wrap"><div class="sbar" style="width:${{Math.min(100,pct)}}%;background:${{col}}"></div></div>
          <button type="button" class="bill-toggle" id="t-${{b.id}}"
                  aria-expanded="false" aria-controls="d-${{b.id}}"
                  onclick="event.stopPropagation();toggle('${{b.id}}')">
            <span class="bill-toggle-txt">Details</span>
            <span class="bill-toggle-ico" aria-hidden="true">\u25be</span>
          </button>
        </div>
      </div>
      <div class="bill-detail" id="d-${{b.id}}" role="region"
           aria-labelledby="t-${{b.id}}">
        ${{s3banner}}
        <div class="detail-lbl">Probability Trail</div>
        <div class="chart-wrap"><canvas id="mc${{b.id}}"></canvas></div>
        <div style="font-size:10px;color:var(--color-text-tertiary);margin-top:-2px;margin-bottom:8px">
          Each point = model score after that action
        </div>
        <div class="detail-lbl">Action History <span style="font-weight:400;text-transform:none">(most recent first)</span></div>
        <div class="action-scroll">${{arows}}</div>
        ${{b.summary?`<div class="detail-lbl">Bill Summary</div><div class="detail-val">${{b.summary}}</div>`:"" }}
        ${{b.impact_txt?`<div class="detail-lbl">University Impact</div><div class="detail-val">${{b.impact_txt}}</div>`:"" }}
        ${{b.policy||b.beneficiary?`
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:6px">
            ${{b.policy?`<div><div class="detail-lbl">Policy Domain</div><div class="detail-val">${{b.policy}}</div></div>`:""}}
            ${{b.beneficiary?`<div><div class="detail-lbl">Intended Beneficiary</div><div class="detail-val">${{b.beneficiary}}</div></div>`:""}}
          </div>`:""}}
        <div style="margin-top:10px">
          <a class="ext-link" href="${{b.legiscan_url}}" target="_blank" rel="noopener"><span class="a11y-sr">${{b.id}} on </span>LegiScan<span aria-hidden="true"> &#8599;</span><span class="a11y-sr"> (opens in new tab)</span></a>
          <a class="ext-link" href="${{b.ilga_url}}" target="_blank" rel="noopener"><span class="a11y-sr">${{b.id}} on </span>ILGA.gov<span aria-hidden="true"> &#8599;</span><span class="a11y-sr"> (opens in new tab)</span></a>
        </div>
      </div>
    </div>`;
  }}).join("");
}}
</script>
"""

# ── Screen-reader text equivalent ─────────────────────────────────────────────
# The bill cards and their probability-trail charts are drawn by Chart.js inside
# a components.html iframe. Assistive technology in the parent document cannot
# reach into that iframe, so the same information is emitted here as
# visually-hidden, structurally navigable markup in normal reading order.
def _trail_summary(actions: list) -> str:
    """One-line text equivalent of a bill's probability trail chart."""
    probs = [a.get("prob") for a in (actions or []) if a.get("prob") is not None]
    if not probs:
        return "no probability trail available"
    first, last = round(float(probs[0]) * 100), round(float(probs[-1]) * 100)
    if len(probs) == 1:
        return f"single point at {last}%"
    direction = "rising" if last > first else ("falling" if last < first else "flat")
    return (f"{len(probs)} points, {direction} from {first}% after the first action "
            f"to {last}% after the most recent")


_sr_rows = "".join(
    "<tr>"
    f"<td>{b['rank']}</td>"
    f"<td>{html.escape(str(b['id']))}</td>"
    f"<td>{html.escape(str(b['title']))}</td>"
    f"<td>{round(float(b['prob']) * 100)}%</td>"
    f"<td>{html.escape(str(b['momentum']))}</td>"
    f"<td>{html.escape(str(b['impact_rtg']) or '—')}</td>"
    f"<td>{b['n_actions']}</td>"
    f"<td>{html.escape(str(b['last_action']))}</td>"
    f"<td>{html.escape(_trail_summary(b['actions']))}</td>"
    "</tr>"
    for b in bills_data
)
sr_block(
    "<h2>Bill predictions — text equivalent</h2>"
    f"<p>{len(bills_data)} bills matching the current filters, in the same order as the "
    "cards below. Probability is the model's estimate that the bill reaches a floor "
    "vote. The trail column summarises each bill's probability trail chart.</p>"
    "<table><caption>Bills matching the current filters</caption><thead><tr>"
    "<th>Rank</th><th>Bill</th><th>Title</th><th>Probability of reaching a vote</th>"
    "<th>Momentum</th><th>University impact</th><th>Actions</th>"
    "<th>Last activity</th><th>Probability trail</th>"
    f"</tr></thead><tbody>{_sr_rows}</tbody></table>"
)

components.html(widget_html.replace("___BILLS_JSON___", bills_json), height=min(700 + len(view_top) * 85, 5000), scrolling=True)

dl_cols = [c for c in [BILL_NUM_COL, TITLE_COL, "session_id", CAT_MAIN,
            POLICY_COL, BENEF_PRIMARY, PROB_COL, MOMENTUM_COL, DATE_COL] if c in view.columns]
from chart_a11y import download_data_button
download_data_button(view[dl_cols], "Filtered bills")   # Bug 17: Excel

st.caption(
    "Reaches-Vote Likelihood = Stage 2 model P(bill reaches a floor vote), AUC 0.9944 across 117,667 bills (2009–2026). "
    "Probability trail shows model score at each legislative action. "
    "Base rate = historical pass rate for this category. "
    "Chart.js interactive — hover for exact probability at each action."
)
