'[Deployment-specific example removed from this source docstring.]'
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from auth_utils import require_auth
# Audit issue 15: neither histogram on this page had a text equivalent.
from chart_a11y import inject_sr_css, sr_chart_table, band_table
from data_catalog import GOLD_URLS, PREDICTION_URLS, TRAIL_URL, SESSION_LABELS, MANIFEST_URLS

# Import DIFF_URLS gracefully — may not exist in older deployments
try:
    from data_catalog import DIFF_URLS
except ImportError:
    DIFF_URLS = {}

require_auth()

# ── Accessibility chrome (ReQtest audit 2026-09-14) ──────────────────────────
# Issues 1/2 (main + nav landmarks), 16 (disabled-state contrast),
# 17/19 (keyboard focus on chart and dataframe overlay controls).
# Must run after require_auth() so it is not emitted on the login gate.
from a11y import apply_page_a11y, wire_char_counters, NEW_TAB
apply_page_a11y('My Tracker')

# ── Bug 27 DIAGNOSTIC (temporary, Nov 3) ─────────────────────────────────────
# Records every run so the slider reset can be pinned to a cause: a new
# session, a new server process, a different page identity, a run that never
# reaches the slider, or one that ends early. The log lives in a process-wide
# cache keyed by the sign-in token, so it survives a session restart. Shown
# only when the address contains &debug=1. Remove once Bug 27 is fixed.
@st.cache_resource
def _dbg_store():
    return {}
try:
    import os as _dbg_os, socket as _dbg_sock, time as _dbg_time
    from streamlit.runtime.scriptrunner import get_script_run_ctx as _dbg_gc
    _dbg_c = _dbg_gc()
    _dbg_key = str(st.session_state.get("session_id") or "anon")[:16]
    _DBG = _dbg_store().setdefault(_dbg_key, [])
    st.session_state["_dbg_n"] = st.session_state.get("_dbg_n", 0) + 1
    _dbg_row = {
        "time": _dbg_time.strftime("%H:%M:%S") + f".{int(_dbg_time.time() * 1000) % 1000:03d}",
        "run": st.session_state["_dbg_n"],
        "session": (_dbg_c.session_id[:6] if _dbg_c else "?"),
        "server": f"{_dbg_sock.gethostname()[-6:]}:{_dbg_os.getpid()}",
        "page": (getattr(_dbg_c, "active_script_hash", "") or "")[:6] + "/" + (getattr(_dbg_c, "page_script_hash", "") or "")[:6],
        "before": str(st.session_state.get("pin_prob_range", "(none)")),
        "drawn": "no", "returned": "", "end": "no",
    }
    _DBG.append(_dbg_row)
    del _DBG[:-80]
except Exception as _dbg_e:
    _dbg_row = {"error": str(_dbg_e)}

# ── Bug 27 FIX: never create widgets under the wrong page identity ───────────
# Streamlit 1.37 builds every widget's ID from ctx.active_script_hash. The live
# diagnostic (Nov 3) caught My Tracker running with active hash md5("app.py")
# (3f41e5) instead of md5("My_Tracker") (d12f8c). Widgets created on such a
# run get different IDs — a second, separate copy of every filter with its own
# saved value — which QA saw as sliders "occasionally resetting the entire
# page" (and our logs as values flipping between two histories). If this page
# is running under any other identity, rerun at once, before drawing anything.
# Capped at 2 consecutive retries so it can never loop.
import hashlib as _idfix_hl
from streamlit.runtime.scriptrunner import get_script_run_ctx as _idfix_gc
_idfix_c = _idfix_gc()
_idfix_want = _idfix_hl.md5(b"My_Tracker").hexdigest()
if _idfix_c is not None and _idfix_c.active_script_hash != _idfix_want:
    if st.session_state.get("_idfix_n", 0) < 2:
        st.session_state["_idfix_n"] = st.session_state.get("_idfix_n", 0) + 1
        try:
            _dbg_row["end"] = "RERUN: wrong identity " + _idfix_c.active_script_hash[:6]
        except Exception:
            pass
        st.rerun()
else:
    st.session_state["_idfix_n"] = 0
inject_sr_css()          # audit issue 15
# Audit issue 11: this page has text_area fields with max_chars up to 1500;
# Streamlit's "X characters remaining" notice is not a live region.
wire_char_counters(threshold=100)

_user_authed = st.session_state.get("user_authed", False)
_user        = st.session_state.get("user_record") or {}
_user_id     = _user.get("email", "") if _user_authed else ""

# Fresh per-run cache for saved-items reads (see _cosmos_list). Streamlit reruns
# the whole script top-to-bottom on every interaction; clearing here means each
# render fetches each item type from Cosmos at most once, while still reflecting
# any save/delete from the previous run (which always ended in st.rerun()).
st.session_state.pop("_saved_items_cache", None)

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* Bug 26 follow-up: one font app-wide. Inter (a Google Fonts download) was
   used on some pages and Streamlit's built-in Source Sans Pro on others. */
.block-container{
  padding-top:.8rem !important;padding-left:.9rem !important;
  padding-right:.9rem !important;
}
header[data-testid="stHeader"]{background:transparent !important;height:0 !important;}
[data-testid="stToolbar"]{display:none !important;}

.bill-card{background:#fff;border:1px solid #E2E8F0;border-radius:12px;
           padding:16px 18px;margin-bottom:10px;}
.bill-card:hover{box-shadow:0 4px 16px rgba(19,41,75,.09);}
.bill-num{font-size:13px;font-weight:700;color:#13294B;}
.bill-title{font-size:13px;font-weight:600;color:#0F172A;margin:4px 0 6px;line-height:1.45;}
.bill-meta{font-size:11px;color:#454F5A;display:flex;gap:10px;flex-wrap:wrap;}
.uiuc-box{background:#EFF6FF;border-left:3px solid #3B82F6;border-radius:0 6px 6px 0;
          padding:8px 12px;font-size:12px;color:#1E40AF;line-height:1.55;margin-top:8px;}
.impact-pill{display:inline-block;padding:2px 8px;border-radius:5px;font-size:11px;font-weight:600;}
.momentum-badge{display:inline-block;padding:2px 7px;border-radius:5px;font-size:11px;font-weight:700;}
.prob-wrap{height:6px;background:#E2E8F0;border-radius:3px;margin:6px 0 2px;}
.prob-fill{height:6px;border-radius:3px;}

/* Diff styles */
.diff-hunk{border:1px solid #E2E8F0;border-radius:8px;margin-bottom:10px;overflow:hidden;}
.diff-hunk-header{background:#F8FAFC;padding:6px 12px;font-size:11px;
                  color:#454F5A;font-family:monospace;border-bottom:1px solid #E2E8F0;}
.diff-line{padding:2px 12px;font-size:12px;font-family:monospace;line-height:1.6;white-space:pre-wrap;}
.diff-added{background:#F0FDF4;color:#065F46;}
.diff-removed{background:#FEF2F2;color:#991B1B;text-decoration:line-through;}
.diff-context{background:#fff;color:#374151;}
.feat-diff-old{background:#FEF2F2;border-left:3px solid #EF4444;
               padding:8px 12px;border-radius:0 6px 6px 0;
               font-size:12px;color:#991B1B;margin-bottom:4px;}
.feat-diff-new{background:#F0FDF4;border-left:3px solid #10B981;
               padding:8px 12px;border-radius:0 6px 6px 0;
               font-size:12px;color:#065F46;margin-bottom:4px;}

/* Actions */
.action-row{display:flex;gap:10px;align-items:flex-start;
            padding:8px 0;border-bottom:1px solid #F1F5F9;font-size:12px;}
.action-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;margin-top:4px;}

.sect-head, [data-testid="stMarkdownContainer"] .sect-head{font-size:16px;font-weight:700;color:#0F172A;letter-spacing:-.02em;margin:14px 0 8px;padding:0;line-height:1.35;}
.empty-state{text-align:center;padding:48px 24px;background:#F8FAFC;
             border:1px dashed #CBD5E1;border-radius:12px;color:#454F5A;font-size:13px;}
.kw-tag{display:inline-block;background:#EFF6FF;color:#1E40AF;border:1px solid #BFDBFE;
        border-radius:6px;padding:3px 10px;font-size:12px;font-weight:600;margin:2px;}
.field-badge{display:inline-block;background:#F5F3FF;color:#6D28D9;border:1px solid #DDD6FE;
             border-radius:6px;padding:2px 8px;font-size:11px;margin-right:4px;}
</style>
""", unsafe_allow_html=True)

# ── Constants ─────────────────────────────────────────────────────────────────
CURRENT_SESSION = 2176

IMPACT_META = {
    3: ("Very Impactful",       "#065F46", "#D1FAE5"),
    2: ("Moderately Impactful", "#92400E", "#FEF3C7"),
    1: ("Slightly Impactful",   "#1E40AF", "#DBEAFE"),
    0: ("Not Impactful",        "#475569", "#F1F5F9"),
}
MOMENTUM_META = {
    "Gaining": ("▲ Gaining", "#065F46", "#D1FAE5"),
    "Stable":  ("■ Stable",  "#475569", "#F1F5F9"),
    "Losing":  ("▼ Losing",  "#9A3412", "#FEE2E2"),
}
STAGE_COLORS = {
    "Filed/Introduced":       "#454F5A",
    "Committee Action":       "#F59E0B",
    "Floor Consideration":    "#3B82F6",
    "Inter-Chamber Transfer": "#8B5CF6",
    "Enrolled/Sent to Gov":   "#10B981",
    "Signed/Enacted":         "#059669",
    "Vetoed":                 "#EF4444",
    "Unlabeled":              "#CBD5E1",
}

# Caps (Phase 1)
MAX_TRACKED_BILLS = 50
MAX_CATEGORIES    = 10

# Per-rerun counter so card widget keys are unique even when the same bill_id
# is rendered in multiple unsaved contexts (search results + a topic's top bills).
# Re-initialised on every script run, so keys stay stable within a run.
_CARD_SEQ = [0]

# Searchable feature fields — prefer the RAW LLM output (ground truth) over the
# standardized/condensed version. _resolve_col() falls back to the standardized
# column when a raw column isn't present in the data, so nothing breaks.
SEARCHABLE_FIELDS = {
    "Summary":               "llama_summary_raw",
    "Legislative Goal":      "legislative_goal_raw",
    "Key Provisions":        "key_provisions_raw",
    "Intent":                "intent_raw",
    "Motivation":            "motivation_raw",
    "U of I Impact":         "potential_impact_raw",
    "Beneficiaries":         "intended_beneficiaries_raw",
    "Increasing Aspects":    "increasing_aspects_raw",
    "Decreasing Aspects":    "decreasing_aspects_raw",
    "Policy Domain":         "policy_domain_standardized",
    "Category":              "category_main_label",
    "Subcategory":           "category_sub_label",
    "Beneficiary Group":     "beneficiary_primary_category",
    "Ideological Alignment": "ideological_alignment_simplified",
}

# raw column -> standardized fallback (used only when the raw column is absent)
FIELD_FALLBACK = {
    "legislative_goal_raw":       "legislative_goal_standardized",
    "key_provisions_raw":         "key_provisions_standardized",
    "intent_raw":                 "intent_standardized",
    "motivation_raw":             "motivation_standardized",
    "intended_beneficiaries_raw": "intended_beneficiaries_standardized",
    "increasing_aspects_raw":     "increasing_aspects_standardized",
    "decreasing_aspects_raw":     "decreasing_aspects_standardized",
}

def _resolve_col(df, col: str) -> str:
    """Prefer the given (raw) column; fall back to its standardized equivalent
    if the raw column isn't in this dataset."""
    if df is None or col in getattr(df, "columns", []):
        return col
    fb = FIELD_FALLBACK.get(col)
    return fb if (fb and fb in df.columns) else col

# Feature fields tracked for snapshot diff (subset of searchable)
SNAPSHOT_FIELDS = [
    "legislative_goal_standardized", "key_provisions_standardized",
    "intent_standardized", "motivation_standardized", "potential_impact_raw",
    "intended_beneficiaries_standardized", "increasing_aspects_standardized",
    "decreasing_aspects_standardized", "impact_rating_score",
    "policy_domain_standardized",
]

PRESET_DOMAINS = [
    "Education", "Healthcare & Public Health", "Higher Education",
    "Criminal Justice & Public Safety", "Finance & Taxation",
    "Employment & Labor", "Environment & Natural Resources",
    "Transportation & Infrastructure", "Government & Public Administration",
    "Social Services & Human Welfare", "Business & Commerce",
    "Civil Rights & Liberties",
]
PRESET_BENEFICIARIES = [
    "Higher Education Institutions & Students",
    "K-12 Students & Schools", "Healthcare Providers & Patients",
    "State Government & Public Employees", "Local Government & Municipal Bodies",
    "Businesses & Employers", "General Public / Illinois Residents",
    "Vulnerable Populations", "Labor & Workers",
    "Law Enforcement & Criminal Justice",
]

# ── Data loaders ──────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False, ttl=3600)
def load_gold(session_id: int) -> pd.DataFrame:
    url = GOLD_URLS.get(session_id)
    if not url:
        return pd.DataFrame()
    try:
        df = pd.read_parquet(url)
        df["bill_id"]             = df["bill_id"].astype(str).str.strip()
        df["session_id"]          = session_id
        df["impact_rating_score"] = pd.to_numeric(
            df.get("impact_rating_score"), errors="coerce").fillna(0).astype(int)
        return df
    except Exception as e:
        st.warning(f"Could not load session {session_id}: {e}")
        return pd.DataFrame()


@st.cache_data(show_spinner=False, ttl=3600)
def load_preds(session_id: int) -> pd.DataFrame:
    url = PREDICTION_URLS.get(session_id)
    if not url:
        return pd.DataFrame()
    try:
        df = pd.read_parquet(url)
        df["bill_id"] = df["bill_id"].astype(str).str.strip()
        keep = [c for c in ["bill_id","y_prob","momentum","n_actions","generated_at"]
                if c in df.columns]
        return df[keep].copy()
    except Exception:
        return pd.DataFrame()


@st.cache_data(show_spinner=False, ttl=3600)
def load_trail_for_bill(bill_id: str) -> pd.DataFrame:
    if not TRAIL_URL:
        return pd.DataFrame()
    try:
        trail = pd.read_parquet(TRAIL_URL)
        trail["bill_id"] = trail["bill_id"].astype(str).str.strip()
        return trail[trail["bill_id"] == bill_id].sort_values("action_index")
    except Exception:
        return pd.DataFrame()


@st.cache_data(show_spinner=False, ttl=3600)
def load_diffs(session_id: int) -> pd.DataFrame:
    """
    Load bill diffs parquet for a session.
    Returns empty DataFrame if DIFF_URLS[session_id] is None (not yet generated).
    Run Ready to Migrate to Server/build_diffs.py then paste SAS URL into
    data_catalog.py DIFF_URLS[session_id] to activate.
    """
    url = DIFF_URLS.get(session_id)
    if not url:
        return pd.DataFrame()
    try:
        df = pd.read_parquet(url)
        df["bill_id"] = df["bill_id"].astype(str).str.strip()
        return df
    except Exception as e:
        st.warning(f"Could not load diffs for session {session_id}: {e}")
        return pd.DataFrame()


@st.cache_data(show_spinner=False, ttl=3600)
def load_master(session_id: int) -> pd.DataFrame:
    gold  = load_gold(session_id)
    if gold.empty:
        return pd.DataFrame()
    preds = load_preds(session_id)
    if not preds.empty:
        gold = gold.merge(preds, on="bill_id", how="left")
    return gold


@st.cache_data(show_spinner=False, ttl=3600)
def load_manifest(session_id: int) -> pd.DataFrame:
    """Bronze manifest: LegiScan's per-bill status. Gives status_desc (the coarse
    stage: Introduced/Engrossed/Enrolled/Passed/Vetoed) and is_terminal. Gold
    does not carry these, so we load and join them for the briefing's stage
    filter. Returns empty on any failure (stage filter then just no-ops)."""
    url = MANIFEST_URLS.get(session_id)
    if not url or str(url).startswith("REPLACE_WITH"):
        return pd.DataFrame()
    try:
        m = pd.read_parquet(url)
        keep = [c for c in ("bill_id", "status_desc", "is_terminal", "last_action")
                if c in m.columns]
        m = m[keep].copy()
        m["bill_id"] = m["bill_id"].astype(str)
        return m
    except Exception:
        return pd.DataFrame()


# Coarse LegiScan stage ladder (status_desc), earliest -> latest. Vetoed sits
# last (it can only follow Passed). Used by the briefing stage filter/sort.
BRF_STAGE_LADDER = ["Introduced", "Engrossed", "Enrolled", "Passed", "Vetoed"]
BRF_STAGE_RANK   = {s.lower(): i for i, s in enumerate(BRF_STAGE_LADDER)}


# ── Cosmos helpers ────────────────────────────────────────────────────────────

def _cosmos_list(item_type: str) -> list[dict]:
    """
    Return the user's saved items of a given type.

    PERFORMANCE: a single Settings/tracker render calls this 8-10 times, often
    for the SAME type repeatedly (category is read 4x, impact_prompt 3x, bill 2x).
    Each raw call is a separate Cosmos round-trip; on the B2 tier that stacked up
    to ~45s to load Settings. We memoize per script-run in session_state so each
    type hits Cosmos at most once per render. Every save/delete in this page is
    followed by st.rerun(), which resets the cache below — so reads stay fresh
    without any manual invalidation.
    """
    if not _user_id:
        return []
    cache = st.session_state.get("_saved_items_cache")
    if cache is None:
        cache = {}
        st.session_state["_saved_items_cache"] = cache
    if item_type in cache:
        return cache[item_type]
    # Bug 27 (QA, 2026-10-07): every arrow-key press on a slider is a rerun, and
    # every rerun re-reads Cosmos. When a read failed (e.g. throttled during a
    # burst of key presses) this returned [], so for that one run the saved
    # categories/prompts vanished, the filters above the sliders changed shape,
    # and Streamlit rebuilt everything below them: the slider reset and keyboard
    # focus fell to the top of the page. A failed read now reuses the last good
    # result (retrying once first), so the page keeps its shape.
    last = st.session_state.setdefault("_saved_items_last", {})
    from user_profile import list_saved_items
    items = None
    for _attempt in (1, 2):
        try:
            items = list_saved_items(_user_id, item_type, raise_on_error=True)
            last[item_type] = items
            break
        except Exception:
            items = None
    if items is None:
        items = last.get(item_type, [])
    cache[item_type] = items
    return items


def _bust_saved_items_cache() -> None:
    """Drop the per-run saved-items cache so the next read re-fetches from Cosmos."""
    st.session_state.pop("_saved_items_cache", None)


def _cosmos_save_bill(bill_id, bill_number, title,
                      session_id, snapshot: dict, notify: bool) -> bool:
    if not _user_id:
        return False
    try:
        import secrets as _sec
        from user_auth import _container
        c = _container("saved_items")
        if c is None:
            return False
        c.create_item({
            "id":         _sec.token_urlsafe(16),
            "user_id":    _user_id,
            "type":       "bill",
            "label":      f"{bill_number} — {title[:80]}",
            "data":       {"bill_id": bill_id, "bill_number": bill_number,
                           "title": title, "session_id": session_id},
            "snapshot":   snapshot,
            "notify":     notify,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return True
    except Exception as e:
        st.warning(f"Save failed: {e}")
        return False


def _cosmos_save_topic(cat_type, cat_value, fields, keywords, notify) -> bool:
    if not _user_id:
        st.error("Your session isn't active. Refresh the page, sign in again, "
                 "and retry — your entry was not saved.")
        return False
    try:
        import secrets as _sec
        from user_auth import _container
        c = _container("saved_items")
        if c is None:
            return False
        # For keyword type, cat_value IS the user-chosen name
        if cat_type == "keyword" and cat_value:
            label = cat_value
        elif keywords:
            label = f"Keyword '{', '.join(keywords[:2])}' in {', '.join([next((k for k,v in SEARCHABLE_FIELDS.items() if v==f),f) for f in fields[:2]])}"
        else:
            label = f"{cat_type.replace('_',' ').title()}: {cat_value}"
        c.create_item({
            "id":         _sec.token_urlsafe(16),
            "user_id":    _user_id,
            "type":       "category",
            "label":      label,
            "data":       {"type": cat_type, "value": cat_value,
                           "fields": fields, "keywords": keywords},
            "notify":     notify,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return True
    except Exception as e:
        st.warning(f"Save failed: {e}")
        return False


def _cosmos_delete(item_id: str) -> bool:
    if not _user_id or not item_id:
        return False
    try:
        from user_profile import delete_saved_item
        return delete_saved_item(_user_id, item_id)
    except Exception:
        return False


def _cosmos_save_impact_feedback(bill_id: str, thumbs: str,
                                 reason: str = "") -> bool:
    """Save thumbs up/down feedback on U of I impact description,
    with an optional free-text reason on thumbs-down."""
    if not _user_id:
        return False
    try:
        import secrets as _sec
        from user_auth import _container
        c = _container("saved_items")
        if c is None:
            return False
        # Upsert — remove existing feedback for this bill first
        existing = list(c.query_items(
            query="SELECT * FROM c WHERE c.user_id=@uid AND c.type='impact_feedback' AND c.data.bill_id=@bid",
            parameters=[{"name":"@uid","value":_user_id},{"name":"@bid","value":bill_id}],
            partition_key=_user_id,
        ))
        for item in existing:
            try:
                c.delete_item(item["id"], partition_key=_user_id)
            except Exception:
                pass
        c.create_item({
            "id":         _sec.token_urlsafe(16),
            "user_id":    _user_id,
            "type":       "impact_feedback",
            "label":      f"Impact feedback: {bill_id}",
            "data":       {"bill_id": bill_id, "thumbs": thumbs,
                           "reason": reason or ""},
            "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        })
        return True
    except Exception:
        return False


# Rating label + color for the U of I impact_rating_score, matching the style of
# the nightly impact-prompt ratings so U of I impact reads as one of the group.
_UIUC_RATING = {3: ("High Impact", "#DC2626"), 2: ("Moderate Impact", "#A2570F"),
                1: ("Low Impact", "#2563EB"), 0: ("No Impact", "#454F5A")}


def _feedback_categories(row: pd.Series | None = None) -> list[tuple]:
    """The impact 'assessments' a user can give feedback on: the built-in U of I
    impact plus each of their own saved impact prompts (and their role profile).
    Returns [(key, label), ...]; key is 'uiuc' or the prompt id."""
    cats = [("uiuc", "U of I Impact")]
    try:
        for _p in _cosmos_list("impact_prompt"):
            _pid = str(_p.get("id", ""))
            _lbl = _p.get("label") or _p.get("data", {}).get("label") \
                or _p.get("data", {}).get("question", "")[:40] or "Prompt"
            if _pid:
                cats.append((_pid, _lbl))
    except Exception:
        pass
    return cats


def _save_impact_feedback_v2(bill_id: str, cat_key: str, cat_label: str,
                             payload: dict) -> bool:
    """Structured feedback on a SPECIFIC impact assessment (U of I impact or one
    of the user's prompts), upserted per (bill, category)."""
    if not _user_id:
        return False
    try:
        import secrets as _sec
        from user_auth import _container
        c = _container("saved_items")
        if c is None:
            return False
        existing = list(c.query_items(
            query=("SELECT * FROM c WHERE c.user_id=@uid AND c.type='impact_feedback' "
                   "AND c.data.bill_id=@bid AND c.data.category_key=@ck"),
            parameters=[{"name": "@uid", "value": _user_id},
                        {"name": "@bid", "value": bill_id},
                        {"name": "@ck", "value": cat_key}],
            partition_key=_user_id))
        for _it in existing:
            try:
                c.delete_item(_it["id"], partition_key=_user_id)
            except Exception:
                pass
        import datetime as _dt
        c.create_item({
            "id":         _sec.token_urlsafe(16),
            "user_id":    _user_id,
            "type":       "impact_feedback",
            "label":      f"Impact feedback: {bill_id} / {cat_label}",
            "data":       {"bill_id": bill_id, "category_key": cat_key,
                           "category_label": cat_label, **payload},
            "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        })
        return True
    except Exception:
        return False


def _bill_label(row) -> str:
    """Bill number for labelling per-bill controls ("" if unavailable)."""
    try:
        v = row.get("bill_number", "") if hasattr(row, "get") else ""
        return "" if v is None or str(v) in ("nan", "None", "<NA>") else str(v)
    except Exception:
        return ""


def _render_impact_feedback(bill_id: str, categories: list[tuple],
                            sfx: str = "", bill_label: str = "") -> None:
    """Impact feedback in a collapsible expander (click the bar to open, click
    again to close, like My annotation): pick which assessment(s) are off — U of I
    impact or any of your prompts — and give structured, model-useful feedback."""
    if not _user_id or not categories:
        return
    _cat_map = dict(categories)
    # Walkthrough finding: this form repeats under every bill card, so its
    # controls all had identical names. Naming the bill in the expander gives
    # every control inside it that context for screen-reader users.
    with st.expander(f"Give feedback on impact — {bill_label}" if bill_label
                     else "Give feedback on impact", expanded=False):
        with st.form(f"impfb_form_{bill_id}_{sfx}", clear_on_submit=True):
            st.caption("Which assessment(s) are off, and how should they read? Pick "
                       "one or more — your U of I impact and each of your prompts can "
                       "be corrected independently.")
            _pick = st.multiselect(
                "Assessment(s) to correct", [k for k, _ in categories],
                format_func=lambda k: _cat_map.get(k, k),
                key=f"impfb_pick_{bill_id}_{sfx}")
            _acc = st.radio("Is it accurate?", ["Accurate", "Partly off", "Wrong"],
                            horizontal=True, key=f"impfb_acc_{bill_id}_{sfx}")
            _fc1, _fc2 = st.columns(2)
            with _fc1:
                _score = st.selectbox(
                    "What should the impact rating be?",
                    ["(leave as is)", "High Impact", "Moderate Impact",
                     "Low Impact", "No Impact"], key=f"impfb_score_{bill_id}_{sfx}")
            with _fc2:
                _affects = st.text_input(
                    "Who does it actually affect?",
                    key=f"impfb_affects_{bill_id}_{sfx}",
                    placeholder="e.g. Sponsored Programs Administration")
            _how = st.text_input(
                "How does it affect them?", key=f"impfb_how_{bill_id}_{sfx}",
                placeholder="e.g. new quarterly reporting burden starting FY27")
            _details = st.text_area(
                "What's wrong, or what should it say?", height=80,
                key=f"impfb_det_{bill_id}_{sfx}",
                placeholder="Be specific — this is what the model learns from.")
            if st.form_submit_button("Submit feedback", type="primary"):
                if not _pick:
                    st.warning("Pick at least one assessment to report on.")
                else:
                    _ok = 0
                    for _k in _pick:
                        if _save_impact_feedback_v2(
                                bill_id, _k, _cat_map.get(_k, _k),
                                {"accurate": _acc, "suggested_rating": _score,
                                 "affects": _affects, "how": _how,
                                 "details": _details}):
                            _ok += 1
                    if _ok:
                        st.toast(f"Thanks — saved feedback on {_ok} assessment(s).")
                        st.rerun()
                    else:
                        st.toast("Couldn't save that — please try again.")


def _render_impact_assessments(row, bill_id: str) -> None:
    """Grouped impact block: U of I impact first (full text + its rating label),
    then every prompt rating — 'which impact · how impactful · what it says'.
    Shared by My Bills and Search & Pin so both read identically."""
    _uiuc_full = str(row.get("potential_impact_raw", "") or "")
    _uiuc_ok = bool(_uiuc_full) and _uiuc_full.lower() not in ("none", "n/a")
    _blocks = []
    if _uiuc_ok:
        _us = int(row.get("impact_rating_score", 0) or 0)
        _ulbl, _uclr = _UIUC_RATING.get(_us, ("Unrated", "#454F5A"))
        _blocks.append(
            f"<div style='margin:3px 0 9px 12px;font-size:12.5px;line-height:1.5'>"
            f"<span style='font-weight:600'>U of I Impact</span>: "
            f"<span style='color:{_uclr};font-weight:600'>{_ulbl}</span>"
            f" — <span style='color:#475569'>{_uiuc_full}</span></div>")
    try:
        from impact_loader import bill_impact_rows
        _ir = bill_impact_rows(_user_id, bill_id)
        if not _ir.empty:
            _RATING_COLORS = {"High Impact": "#DC2626", "Moderate Impact": "#A2570F",
                              "Low Impact": "#2563EB", "No Impact": "#454F5A"}
            for _, _r in _ir.iterrows():
                _c = _RATING_COLORS.get(_r["rating_label"], "#454F5A")
                _blocks.append(
                    f"<div style='margin:3px 0 9px 12px;font-size:12.5px;line-height:1.5'>"
                    f"<span style='font-weight:600'>{_r['prompt_label']}</span>: "
                    f"<span style='color:{_c};font-weight:600'>{_r['rating_label']}</span>"
                    f" — <span style='color:#475569'>{_r['explanation']}</span></div>")
    except Exception:
        pass
    if _blocks:
        st.markdown("<div style='font-size:12px;font-weight:700;color:#334155;"
                    "margin:10px 0 2px'>Impact assessments</div>"
                    + "".join(_blocks), unsafe_allow_html=True)
    else:
        st.caption("No impact assessments have been scored for this bill yet.")


def _render_prediction_trail(bill_id: str, current_prob: float | None = None) -> None:
    """Exact replica of the Active Bills 'Probability Trail': a Chart.js area
    chart (colored by likelihood, dots, hover tooltips showing each action's %,
    a 0-100% axis) plus the per-action history list. Rendered in a components
    iframe so it matches the bill-predictions page rather than the plain
    st.line_chart."""
    trail = load_trail_for_bill(bill_id)
    if trail.empty or "y_prob" not in trail.columns:
        return
    t = trail.sort_values("action_index")
    actions = []
    for _, r in t.iterrows():
        _yp = r.get("y_prob")
        if pd.isna(_yp):
            continue
        _ad = r.get("action_date")
        _date_str = ""
        if _ad is not None and str(_ad) not in ("", "None", "NaT"):
            try:
                _d = pd.Timestamp(_ad)
                _date_str = _d.strftime("%b %d") if pd.notna(_d) else str(_ad)[:10]
            except Exception:
                _date_str = str(_ad)[:10]
        actions.append({
            "date":  _date_str,
            "text":  str(r.get("action_text", "") or ""),
            "stage": str(r.get("semantic_stage", "Filed") or "Filed"),
            "prob":  round(float(_yp), 3),
        })
    if not actions:
        return
    # ── Walkthrough rewrite (2026-10-02) ──────────────────────────────────
    # The old version was a Chart.js chart in a components iframe. On the live
    # site it (1) crashed when current_prob was NaN — Python wrote "pc(nan)"
    # into the JavaScript; (2) loaded Chart.js from cdnjs, which can be blocked;
    # (3) was unreachable by the a11y runtime: unnamed canvas, untitled frame,
    # 3.5:1 grey text, and stage shown only as a coloured dot. It is now an
    # inline SVG (one named image) plus a real table, rendered in the page.
    import html as _h_pt
    try:
        _cur = float(current_prob)
    except (TypeError, ValueError):
        _cur = None
    if _cur is None or _cur != _cur:                 # None or NaN
        _cur = float(actions[-1]["prob"])

    def _pc(p):                                      # all >= 4.5:1 on white
        return "#3B6D11" if p >= .65 else ("#8A5300" if p >= .35 else "#A32D2D")
    _col = _pc(_cur)
    _SCOL = {"Filed": "#5F5E5A", "Referred": "#5F5E5A", "Committee Referral": "#1F6FB8",
             "Committee Action": "#0F7A55", "Committee Action (Held)": "#B42318",
             "Sponsorship": "#534AB7", "Floor": "#8A5300", "Inter-Chamber": "#185FA5",
             "Amendment": "#993C1D", "Deadline": "#5F5E5A", "Enrolled": "#185FA5",
             "Governor": "#3B6D11"}

    # SVG trail
    W, H, PL, PR, PT, PB = 600, 130, 36, 12, 10, 20
    n = len(actions)
    xs = [PL + (W - PL - PR) * (i / (n - 1) if n > 1 else 0.5) for i in range(n)]
    ys = [PT + (H - PT - PB) * (1 - max(0.0, min(1.0, a["prob"]))) for a in actions]
    _pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    _area = f"{xs[0]:.1f},{H - PB} {_pts} {xs[-1]:.1f},{H - PB}"
    _grid = "".join(
        f'<line x1="{PL}" x2="{W - PR}" y1="{PT + (H - PT - PB) * (1 - g):.1f}" y2="{PT + (H - PT - PB) * (1 - g):.1f}" stroke="#E4E7EB" stroke-width="1"/>'
        f'<text x="{PL - 6}" y="{PT + (H - PT - PB) * (1 - g) + 3:.1f}" text-anchor="end" font-size="10" fill="#454F5A">{int(g * 100)}%</text>'
        for g in (0, .5, 1))
    _dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{_col}"/>' for x, y in zip(xs, ys))
    _xl = (f'<text x="{xs[0]:.1f}" y="{H - 5}" font-size="10" fill="#454F5A" text-anchor="start">{_h_pt.escape(actions[0]["date"])}</text>'
           + (f'<text x="{xs[-1]:.1f}" y="{H - 5}" font-size="10" fill="#454F5A" text-anchor="end">{_h_pt.escape(actions[-1]["date"])}</text>' if n > 1 else ""))
    _probs = [round(a["prob"] * 100) for a in actions]
    _summary = (f"Probability trail: {n} action{'s' if n != 1 else ''}, "
                + (f"from {_probs[0]}% ({actions[0]['date']}) to {_probs[-1]}% ({actions[-1]['date']}); "
                   f"lowest {min(_probs)}%, highest {max(_probs)}%." if n > 1 else f"{_probs[0]}%.")
                + " Every action is listed in the table below.")
    _svg = (f'<div role="img" aria-label="{_h_pt.escape(_summary, quote=True)}" style="max-width:640px">'
            f'<svg aria-hidden="true" focusable="false" viewBox="0 0 {W} {H}" width="100%" style="display:block">'
            f'{_grid}<polygon points="{_area}" fill="{_col}" fill-opacity="0.10"/>'
            f'<polyline points="{_pts}" fill="none" stroke="{_col}" stroke-width="2.5"/>{_dots}{_xl}</svg></div>')

    # Action history as a real table (most recent first)
    _rows = "".join(
        f'<tr><th scope="row" style="white-space:nowrap;font-weight:600">{_h_pt.escape(a["date"])}</th>'
        f'<td>{_h_pt.escape(a["text"])}</td>'
        f'<td style="white-space:nowrap"><span aria-hidden="true" style="display:inline-block;width:8px;height:8px;'
        f'border-radius:50%;background:{_SCOL.get(a["stage"], "#5F5E5A")};margin-right:5px"></span>{_h_pt.escape(a["stage"])}</td>'
        f'<td style="white-space:nowrap;font-weight:700;color:{_pc(a["prob"])}">{round(a["prob"] * 100)}%</td></tr>'
        for a in reversed(actions))
    st.markdown(
        '<div style="font-size:11px;font-weight:700;color:#454F5A;text-transform:uppercase;letter-spacing:.04em;margin:6px 0 3px">Probability trail</div>'
        + _svg
        + '<div style="font-size:11px;color:#454F5A;margin:2px 0 8px">Each point is the model score after that action.</div>'
        + '<div role="region" tabindex="0" aria-label="Action history" style="max-height:220px;overflow:auto;border:1px solid #D5DCE3;border-radius:6px">'
        + '<table style="border-collapse:collapse;width:100%;font-size:12px;color:#1A2733">'
        + '<caption style="caption-side:top;text-align:left;font-weight:700;padding:6px 8px;color:#1A2733">Action history (most recent first)</caption>'
        + '<thead><tr style="background:#F1F5F9"><th scope="col" style="text-align:left;padding:4px 8px">Date</th>'
        + '<th scope="col" style="text-align:left;padding:4px 8px">Action</th><th scope="col" style="text-align:left;padding:4px 8px">Stage</th>'
        + '<th scope="col" style="text-align:left;padding:4px 8px">Probability</th></tr></thead>'
        + f'<tbody>{_rows}</tbody></table></div>'
        + '<style>div[aria-label="Action history"] td, div[aria-label="Action history"] tbody th{padding:4px 8px;border-top:1px solid #EEF1F4;vertical-align:top}</style>',
        unsafe_allow_html=True)


def _build_snapshot(row: pd.Series) -> dict:
    snap = {}
    for f in SNAPSHOT_FIELDS:
        val = row.get(f, None)
        snap[f] = str(val) if val is not None else ""
    return snap


# ── Rendering helpers ─────────────────────────────────────────────────────────

def _impact_pill(score: int) -> str:
    label, fg, bg = IMPACT_META.get(score, ("Unknown","#475569","#F1F5F9"))
    return f'<span class="impact-pill" style="background:{bg};color:{fg}">{label}</span>'


def _momentum_badge(m: str) -> str:
    if m not in MOMENTUM_META:
        return ""
    txt, fg, bg = MOMENTUM_META[m]
    # Audit bug 7: the glyph is a visual cue (bug 4); the word carries the
    # meaning, so hide the glyph from AT rather than announcing "black
    # up-pointing triangle Gaining".
    _g, _w = (txt.split(" ", 1) + [""])[:2] if " " in txt else ("", txt)
    return (f'<span class="momentum-badge" style="background:{bg};color:{fg}">'
            f'<span aria-hidden="true">{_g}</span> {_w}</span>')


def _prob_bar(prob) -> str:
    if prob is None or (isinstance(prob, float) and pd.isna(prob)):
        return ""
    pct   = int(float(prob) * 100)
    # Audit issue 4 (WCAG 1.4.1): the bar's fill colour was the only signal of
    # which likelihood band a bill fell into — the numeral alone did not name
    # the band, and the three colours were indistinguishable to a user with a
    # colour-vision deficiency or on a monochrome display. Each band now also
    # carries a glyph and a word.
    if pct >= 65:
        color, glyph, band = "#0072B2", "\u25b2", "Likely"
    elif pct >= 35:
        color, glyph, band = "#A2570F", "\u25c6", "Uncertain"
    else:
        color, glyph, band = "#454F5A", "\u25bc", "Unlikely"
    return (f'<div class="prob-wrap"><div class="prob-fill" '
            f'style="width:{pct}%;background:{color}"></div></div>'
            f'<span style="font-size:11px;color:#475569">'
            f'Floor vote likelihood: <b>{pct}%</b> '
            f'<span style="color:{color};font-weight:700" aria-hidden="true">{glyph}</span> {band}</span>')


def render_diff_tab(bill_id: str, bill_number: str,
                    saved_snap: dict | None, current_row: pd.Series,
                    session_id: int) -> None:
    """
    Full amendment history — all version changes from introduction to today,
    newest first. Includes an AI narrative summary button.
    """
    diffs_df = load_diffs(session_id)

    if diffs_df.empty:
        st.markdown("""
<div style="background:#FFFBEB;border:1px solid #FDE68A;border-radius:8px;padding:14px 16px;">
  <div style="font-weight:600;color:#92400E;margin-bottom:4px">Amendment history not yet available</div>
  <div style="font-size:12px;color:#92400E;line-height:1.6">
    Run <code>build_diffs.py --session {sid} --upload</code> on the pipeline server,
    generate a SAS URL, and add it to
    <code>data_catalog.py &rarr; DIFF_URLS[{sid}]</code>.
  </div>
</div>
""".format(sid=session_id), unsafe_allow_html=True)
        return

    bill_diffs = (
        diffs_df[diffs_df["bill_id"] == bill_id]
        .sort_values("changed_at", ascending=True)
        .reset_index(drop=True)
    )

    if bill_diffs.empty:
        st.caption(
            f"No amendments recorded for {bill_number} yet. "
            "Changes appear here after the next pipeline run."
        )
        return

    # ── Summary header ────────────────────────────────────────────────────────
    n_changes     = len(bill_diffs)
    first_date    = str(bill_diffs.iloc[0].get("changed_at", ""))[:10]
    latest_date   = str(bill_diffs.iloc[-1].get("changed_at", ""))[:10]
    total_added   = int(bill_diffs["n_lines_added"].fillna(0).sum())
    total_removed = int(bill_diffs["n_lines_removed"].fillna(0).sum())

    st.markdown(f"""
<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:12px;padding:10px 14px;
            background:#F8FAFC;border-radius:8px;border:1px solid #E2E8F0;">
  <span style="font-size:12px;color:#0F172A;font-weight:600">
    {n_changes} amendment{"s" if n_changes != 1 else ""}
  </span>
  <span style="font-size:12px;color:#454F5A">{first_date} → {latest_date}</span>
  <span style="font-size:12px;background:#F0FDF4;color:#065F46;
               padding:1px 8px;border-radius:4px">+{total_added} lines total</span>
  <span style="font-size:12px;background:#FEF2F2;color:#991B1B;
               padding:1px 8px;border-radius:4px">−{total_removed} lines total</span>
</div>
""", unsafe_allow_html=True)

    # ── AI Summary button — uses ai_briefing (same pattern as Trends/Explorer) ─
    try:
        import ai_briefing
        _ai_ready = ai_briefing.is_configured()
    except Exception:
        _ai_ready = False

    if _ai_ready:
        if st.button("AI Summary — Full Amendment History",
                     key=f"ai_hist_{bill_id}", type="secondary"):
            with st.spinner("Generating summary…"):
                try:
                    context_parts = []
                    for _, r in bill_diffs.iterrows():
                        changed   = str(r.get("changed_at", ""))[:10]
                        action    = str(r.get("last_action", "") or "")
                        v_from    = int(r.get("version_from", 0))
                        v_to      = int(r.get("version_to", 0))
                        n_add     = int(r.get("n_lines_added", 0))
                        n_rem     = int(r.get("n_lines_removed", 0))
                        added_text = ""
                        try:
                            hunks = json.loads(str(r.get("diff_hunks", "[]")))
                            added_text = " ".join(
                                ln.get("text", "") for h in hunks
                                for ln in h.get("lines", [])
                                if ln.get("type") == "added"
                            )[:500]
                        except Exception:
                            pass
                        context_parts.append(
                            f"[{changed}] Amendment v{v_from}→v{v_to}: "
                            f"+{n_add}/−{n_rem} lines. "
                            f"Action: {action}. "
                            f"New text added: {added_text}"
                        )

                    title   = str(current_row.get("title", bill_number))
                    summary = str(current_row.get("llama_summary_raw", "") or "")[:400]
                    impact  = str(current_row.get("potential_impact_raw", "") or "")[:300]

                    prompt = (
                        f"You are writing about Illinois bill {bill_number}: \"{title}\".\n\n"
                        f"Current bill summary: {summary}\n"
                        f"U of I impact: {impact}\n\n"
                        f"Amendment history (chronological):\n"
                        + "\n\n".join(context_parts)
                        + "\n\nWrite 2-3 paragraphs covering: "
                        "(1) What this bill does and who it affects. "
                        "(2) How it has evolved through amendments. "
                        "(3) What its current trajectory means for the University of Illinois."
                    )

                    ai_text = ai_briefing.generate(prompt, max_tokens=700)
                    st.markdown(f"""
<div style="background:#F0FDF4;border:1px solid #BBF7D0;border-radius:12px;
            padding:18px 22px;margin-bottom:14px;">
  <div style="font-size:12px;font-weight:700;color:#15803D;margin-bottom:8px">
    AI-Generated Amendment Summary
  </div>
  <div style="font-size:13px;color:#064E3B;line-height:1.75">{ai_text}</div>
</div>
""", unsafe_allow_html=True)

                except Exception as e:
                    st.error(f"Could not generate summary: {e}")
    else:
        st.caption(
            "Enable Azure OpenAI in app settings to generate "
            "AI summaries of amendment history."
        )

    st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)

    # ── All amendments, newest first ──────────────────────────────────────────
    for idx, (_, row) in enumerate(bill_diffs.iloc[::-1].reset_index(drop=True).iterrows()):
        v_from    = int(row.get("version_from", 0))
        v_to      = int(row.get("version_to", 0))
        changed   = str(row.get("changed_at", ""))[:10]
        n_added   = int(row.get("n_lines_added", 0))
        n_removed = int(row.get("n_lines_removed", 0))
        action    = str(row.get("last_action", "") or "")

        exp_label = (
            f"v{v_from} → v{v_to}  ·  {changed}"
            f"  ·  +{n_added} / −{n_removed} lines"
        )
        with st.expander(exp_label, expanded=(idx == 0)):
            if action:
                st.caption(f"Legislative action: {action}")

            try:
                hunks = json.loads(str(row.get("diff_hunks", "[]")))
            except Exception:
                hunks = []

            if not hunks:
                st.caption("No diff details available for this amendment.")
            else:
                for hunk in hunks:
                    header = hunk.get("header", "")
                    lines  = hunk.get("lines", [])
                    html   = '<div class="diff-hunk">'
                    if header:
                        html += f'<div class="diff-hunk-header">{header}</div>'
                    for line in lines:
                        ltype  = line.get("type", "context")
                        text   = line.get("text", "").replace("<", "&lt;").replace(">", "&gt;")
                        prefix = "+" if ltype == "added" else "−" if ltype == "removed" else " "
                        html  += (
                            f'<div class="diff-line diff-{ltype}">'
                            f'{prefix} {text}</div>'
                        )
                    html += "</div>"
                    st.markdown(html, unsafe_allow_html=True)


def _clip_html(text: str, limit: int) -> str:
    """Shorten a description without cutting it off mid-sentence (bug 23).

    Hard slices like text[:200] + "…" ended descriptions mid-word ("submitted
    through automate…"), visually and for screen readers. Prefer the first
    whole sentence(s) within ~1.6x the limit; otherwise end on a whole word.
    When anything is dropped, the full text is available in a native
    <details> disclosure, which screen readers announce as expandable.
    """
    import re as _re
    t = " ".join(str(text or "").split())
    if len(t) <= limit:
        return t
    cut = None
    for m in _re.finditer(r"[.!?](?=\s|$)", t):
        if m.end() <= int(limit * 1.6):
            cut = m.end()
        else:
            break
    if cut:
        short = t[:cut]
    else:
        short = t[:limit].rsplit(" ", 1)[0].rstrip(",;:—-") + "…"
    if len(short) >= len(t):
        return t
    return (f"{short}"
            f"<details style='margin-top:3px'><summary style='cursor:pointer;font-size:11px;"
            f"color:#13294B;font-weight:600'>Full description</summary>"
            f"<div style='margin-top:4px'>{t}</div></details>")


def _ilga_url(bill_number: str, session_id: int = None) -> str:
    """Deep link to a bill's status page on the Illinois General Assembly site.

    Constructable for the 104th GA (session 2176) from just the bill number —
    ILGA's internal ids (GAID, SessionID) are fixed per General Assembly, and
    LegId=0 resolves fine. Falls back to LegiScan for other sessions, which still
    lands on the bill. Returns "" if the bill number can't be parsed.
    """
    import re
    if session_id is None:
        session_id = CURRENT_SESSION
    m = re.match(r"^\s*([A-Za-z]+)\s*0*(\d+)\s*$", str(bill_number or ""))
    if not m:
        return ""
    doctype, docnum = m.group(1).upper(), m.group(2)
    # session_id -> (GA, GAID, SessionID) for the ILGA BillStatus URL.
    _ILGA = {2176: ("104", "18", "114")}
    if session_id in _ILGA:
        ga, gaid, sid = _ILGA[session_id]
        return (f"https://www.ilga.gov/Legislation/BillStatus?GAID={gaid}"
                f"&DocNum={docnum}&DocTypeID={doctype}&LegId=0"
                f"&SessionID={sid}&GA={ga}")
    _year = {2176: 2025, 2020: 2023, 1801: 2021}.get(session_id, 2025)
    return f"https://legiscan.com/IL/bill/{bill_number}/{_year}"


def _ilga_link_html(bill_number: str, session_id: int = None) -> str:
    """The 'View on ILGA' pill, or "" if no URL could be built."""
    _u = _ilga_url(bill_number, session_id)
    if not _u:
        return ""
    return (f"<a href='{_u}' target='_blank' style='font-size:11px;color:#13294B;"
            f"font-weight:600;text-decoration:none;background:#F1F5F9;padding:2px 8px;"
            f"border-radius:5px;border:1px solid #E2E8F0'>View on ILGA{NEW_TAB}</a>")


def _display_name(email, *, refresh: bool = False) -> str:
    """Resolve an email to the short display name the person chose in My Profile,
    falling back to the email prefix. This is the single source of truth for how
    a person is named anywhere in the tracker — the profile name overrides the
    prefix everywhere (overlap, shared bills/briefings, digest annotations, My
    Bills imported reads). Cached per session so repeated lookups within one
    render don't each hit Cosmos; the cache is cleared when a name is saved.
    """
    e = str(email or "").strip()
    if not e:
        return "a teammate"
    _cache = st.session_state.setdefault("_dn_cache", {})
    if not refresh and e in _cache:
        return _cache[e]
    name = e.split("@")[0] or "a teammate"
    try:
        from user_auth import get_user, canonical_email
        rec = get_user(canonical_email(e)) or get_user(e) or {}
        name = (rec.get("display_name") or name)
    except Exception:
        pass
    _cache[e] = name
    return name


def _bill_context_html(row: pd.Series, summary_chars: int = 280) -> str:
    """Compact bill context — title, summary, category, stage/last action,
    floor-vote likelihood, U of I impact, and the ILGA link. The same facts a
    My Bills card shows, minus the interactive expander, so it can be dropped
    into the overlap / briefing / digest sections (even inside an expander).
    Pass a `master` row. Returns "" if the row is empty.
    """
    if row is None or (hasattr(row, "empty") and getattr(row, "empty", False)):
        return ""
    bn    = str(row.get("bill_number", "") or "")
    sid   = int(row.get("session_id", CURRENT_SESSION) or CURRENT_SESSION)
    title = str(row.get("title", "") or "")
    summ  = str(row.get("llama_summary_raw", "") or "")
    summ  = _clip_html(summ, summary_chars)
    cat   = str(row.get("category_main_label", "") or "")
    ldate = str(row.get("last_action_date", "") or "")
    prob  = row.get("y_prob", None)
    mom   = str(row.get("momentum", "") or "")
    goal  = str(row.get("legislative_goal_standardized", "")
                or row.get("legislative_goal_raw", "") or "")
    goal  = _clip_html(goal, 160)
    uiuc  = str(row.get("potential_impact_raw", "") or "")
    uiuc  = _clip_html(uiuc, 240)
    show_uiuc = bool(uiuc) and uiuc.lower() not in ("none", "n/a")
    # Audit bug 7: decorative; MOMENTUM_META already carries the ▲■▼ text cue.
    _mom_icon = ""   # emoji removed (2026-09-30 decision); the word carries the meaning
    _mom_icon = f"<span aria-hidden='true'>{_mom_icon}</span>" if _mom_icon else ""
    parts = []
    if title:
        parts.append(f"<div style='font-size:13px;font-weight:600;color:#0F172A;"
                     f"margin-top:2px'>{title}</div>")
    if summ:
        parts.append(f"<div style='font-size:12px;color:#475569;line-height:1.5;"
                     f"margin-top:3px'>{summ}</div>")
    if goal and goal.lower() not in ("none", "n/a"):
        parts.append(f"<div style='font-size:11.5px;color:#475569;margin-top:3px'>"
                     f"<b>Goal:</b> {goal}</div>")
    meta = []
    if cat:
        meta.append(f"{cat}")
    if mom:
        meta.append(f"{_mom_icon} {mom}".strip())
    if ldate:
        meta.append(f"{ldate}")
    if pd.notna(prob):
        try:
            meta.append(f"{int(float(prob) * 100)}% floor-vote likelihood")
        except Exception:
            pass
    if meta:
        parts.append(f"<div style='font-size:11px;color:#454F5A;margin-top:4px'>"
                     f"{' · '.join(meta)}</div>")
    if show_uiuc:
        parts.append(f"<div style='font-size:11.5px;color:#0369A1;margin-top:4px'>"
                     f"<b>U of I:</b> {uiuc}</div>")
    _lnk = _ilga_link_html(bn, sid)
    if _lnk:
        parts.append(f"<div style='margin-top:6px'>{_lnk}</div>")
    return "".join(parts)


def _section_banner(emoji: str, title: str, subtitle: str,
                    accent: str, bg: str, fg: str) -> str:
    """A bold, color-coded section header for the My Team tab, so the distinct
    functions (overlap / shared briefings / shared bills) read as clearly
    separate blocks rather than one long scroll. accent = left bar + heading
    color family, bg = tinted background, fg = heading/subtitle text color.
    """
    sub = (f"<div style='font-size:12px;color:{fg};margin-top:2px'>"
           f"{subtitle}</div>") if subtitle else ""
    return (f"<div style='margin:22px 0 10px;padding:11px 16px;border-radius:10px;"
            f"background:{bg};border-left:6px solid {accent}'>"
            f"<h3 style='font-size:17px;font-weight:800;color:{fg};margin:0;padding:0;line-height:1.3;"
            f"letter-spacing:-.02em'>{title}</h3>{sub}</div>")


# Section palette for My Team — overlap=orange, briefings=blue, shared bills=green.
_TEAM_ORANGE = ("#C2410C", "#FFF7ED", "#9A3412")
_TEAM_BLUE   = ("#4F46E5", "#EEF2FF", "#3730A3")
_TEAM_GREEN  = ("#16A34A", "#F0FDF4", "#166534")


def _compute_overlaps(user_id: str, roster_emails, session_default: int = None):
    """Bills the AI independently flagged High (rating 3) for this user AND at
    least one teammate — team convergence, ranked by how many people a bill
    lands on. Factored out so My Team and My Digest read one source of truth.
    Returns a list of dicts sorted by (n, max_rating) desc; [] on any error.
    """
    if session_default is None:
        session_default = CURRENT_SESSION
    out = []
    try:
        from investigation_loader import load_investigations_for_users
        roster = {user_id} | {e for e in (roster_emails or []) if e}
        team_inv = load_investigations_for_users(roster)
        if team_inv.empty or "max_rating_num" not in team_inv.columns:
            return []
        hi = team_inv[team_inv["max_rating_num"] >= 3]
        for _bid, _g in hi.groupby("bill_id"):
            users = list(dict.fromkeys(_g["user_id"].tolist()))
            if user_id not in users or len(users) < 2:
                continue   # only YOUR overlaps, and needs you + ≥1 teammate
            r0 = _g.sort_values("max_rating_num", ascending=False).iloc[0]
            out.append({
                "bill_id": str(_bid),
                "bill_number": str(r0.get("bill_number", "")),
                "headline": str(r0.get("headline", "") or ""),
                "n": len(users),
                "max_rating": int(_g["max_rating_num"].max()),
                "others": [u for u in users if u != user_id],
                "session_id": int(r0.get("session_id", session_default)),
            })
        out.sort(key=lambda o: (o["n"], o["max_rating"]), reverse=True)
    except Exception:
        return []
    return out


def _render_actions_changes(bill_id: str, bill_number: str,
                            saved_item: dict | None, row, session_id: int) -> None:
    """The 'Actions & Changes' expander: legislative action trail + text diff.
    Extracted so it can render at a chosen position (e.g. after the prediction
    trail in My Bills) rather than only inside render_bill_card."""
    with st.expander(f"Actions & Changes — {bill_number}"):
        tab_actions, tab_diff = st.tabs(["Legislative Actions", "What Changed"])
        with tab_actions:
            trail = load_trail_for_bill(bill_id)
            if trail.empty:
                st.caption("No action history available yet.")
            else:
                for _, t in trail.sort_values("action_index",
                                               ascending=False).iterrows():
                    stage  = str(t.get("semantic_stage", "Unlabeled"))
                    color  = STAGE_COLORS.get(stage, "#CBD5E1")
                    a_date = str(t.get("action_date", ""))[:10]
                    a_text = str(t.get("action_text", ""))
                    prob_t = t.get("y_prob", None)
                    prob_s = (f" · {int(float(prob_t)*100)}% confidence"
                              if prob_t is not None else "")
                    st.markdown(f"""
<div class="action-row">
  <div class="action-dot" style="background:{color};margin-top:4px"></div>
  <div style="flex:1">
    <span style="font-weight:600;color:#0F172A">{a_text}</span>
    <span style="color:#454F5A;font-size:11px"> · {a_date}{prob_s}</span>
    <br><span style="font-size:10px;color:#454F5A">{stage}</span>
  </div>
</div>
""", unsafe_allow_html=True)
        with tab_diff:
            snap = saved_item.get("snapshot") if saved_item else None
            render_diff_tab(bill_id, bill_number, snap, row, session_id)


def render_bill_card(row: pd.Series, saved_item: dict | None = None,
                     render_uiuc: bool = True, render_feedback: bool = True,
                     render_actions: bool = True) -> None:
    _CARD_SEQ[0] += 1
    _seq = _CARD_SEQ[0]
    bill_id     = str(row.get("bill_id", ""))
    bill_number = str(row.get("bill_number", ""))
    title       = str(row.get("title", "No title"))
    session_id  = int(row.get("session_id", CURRENT_SESSION))
    score       = int(row.get("impact_rating_score", 0))
    prob        = row.get("y_prob", None)
    momentum    = str(row.get("momentum", "")) or ""
    last_date   = str(row.get("last_action_date", "")) or ""
    category    = str(row.get("category_main_label", "")) or ""
    uiuc        = str(row.get("potential_impact_raw", "")) or ""
    session_lbl = SESSION_LABELS.get(session_id, str(session_id))
    uiuc_short  = (uiuc[:240] + "…") if len(uiuc) > 240 else uiuc
    show_uiuc   = uiuc and uiuc.lower() not in ("none", "n/a", "")

    # Extra fields for enriched card
    summary     = str(row.get("llama_summary_raw", "") or "")
    goal        = str(row.get("legislative_goal_standardized", "") or "")
    summary_short = _clip_html(summary, 200)
    goal_short    = _clip_html(goal, 120)

    # Build the source URL — Illinois General Assembly bill status page.
    legiscan_url = _ilga_url(bill_number, session_id)

    col_main, col_rm = st.columns([11, 1])

    with col_main:
        st.markdown(f"""
<div class="bill-card">
  <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px">
    <span class="bill-num">{bill_number}</span>
    <span style="font-size:11px;color:#454F5A">·</span>
    <span style="font-size:11px;color:#454F5A">{session_lbl}</span>
    {_impact_pill(score)}
    {_momentum_badge(momentum)}
    {(f'<a href="{legiscan_url}" target="_blank" style="font-size:11px;color:#13294B;font-weight:600;text-decoration:none;background:#F1F5F9;padding:2px 8px;border-radius:5px;border:1px solid #E2E8F0">View on ILGA{NEW_TAB}</a>') if legiscan_url else ""}
  </div>
  <div class="bill-title">{title}</div>
  {(f'<div style="font-size:12px;color:#475569;line-height:1.5;margin-bottom:4px">{summary_short}</div>') if summary_short else ""}
  {(f'<div style="font-size:11px;color:#454F5A;margin-bottom:4px"><b>Goal:</b> {goal_short}</div>') if goal_short else ""}
  <div class="bill-meta">
    {"<span>" + category + "</span>" if category else ""}
    {"<span>" + last_date + "</span>" if last_date else ""}
  </div>
  {_prob_bar(prob)}
  {('<div class="uiuc-box"><b>U of I Impact:</b> ' + uiuc + '</div>') if (show_uiuc and render_uiuc) else ""}
</div>
""", unsafe_allow_html=True)

    with col_rm:
        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
        if saved_item and _user_authed:
            if st.button("Remove", key=f"rm_{saved_item.get('id',bill_id)}",
                         help="Stop tracking"):
                if _cosmos_delete(saved_item.get("id", "")):
                    st.success("Removed")
                    st.rerun()
        elif _user_authed and bill_id:
            # Universal pin — this card appears in Topics, Briefing, and search,
            # so a pin here lets you track a bill from wherever you found it.
            _pinned_ids = {str(b.get("data", {}).get("bill_id", ""))
                           for b in _cosmos_list("bill")}
            if bill_id in _pinned_ids:
                st.markdown("<div style='font-size:11px;color:#1B7F3B;"
                            "text-align:center;margin-top:2px'>tracked</div>",
                            unsafe_allow_html=True)
            elif len(_pinned_ids) >= MAX_TRACKED_BILLS:
                st.markdown(f"<div style='font-size:10px;color:#454F5A;"
                            f"text-align:center;margin-top:2px'>"
                            f"{MAX_TRACKED_BILLS} max</div>",
                            unsafe_allow_html=True)
            else:
                if st.button("Pin", key=f"pin_{bill_id}_{_seq}",
                             help="Pin to My Bills"):
                    _snap = {}
                    for _sc in ("legislative_goal_standardized", "key_provisions_standardized",
                                "intent_standardized", "motivation_standardized",
                                "potential_impact_raw", "impact_rating_score",
                                "policy_domain_standardized"):
                        if _sc in row.index:
                            _snap[_sc] = row.get(_sc)
                    if _cosmos_save_bill(bill_id, bill_number, title,
                                         session_id, _snap, notify=True):
                        st.success(f"Pinned {bill_number}")
                        st.rerun()

    # Impact feedback — redesigned: pick which assessment is off (U of I impact or
    # any of your prompts) and give structured, model-useful feedback. Same widget
    # used in My Bills, so feedback works the same everywhere the card appears.
    if show_uiuc and render_feedback and _user_authed:
        _sfx = saved_item.get('id', '') if saved_item else f"s{_seq}"
        _render_impact_feedback(bill_id, _feedback_categories(row), _sfx, bill_label=_bill_label(row))

    if render_actions:
        _render_actions_changes(bill_id, bill_number, saved_item, row, session_id)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE HEADER
# ══════════════════════════════════════════════════════════════════════════════

st.markdown("""
<div style="background:linear-gradient(135deg,#0F1F38 0%,#1B4B5A 55%,#2D6A7A 100%);
            border-radius:14px;padding:22px 28px;margin-bottom:16px;">
  <div style="color:rgba(255,255,255,.92);font-size:10px;font-weight:700;
              letter-spacing:.14em;text-transform:uppercase;margin-bottom:6px">
    Personal Tracker
  </div>
  <h1 style="margin:0;padding:0;color:#fff;font-size:24px;font-weight:800;letter-spacing:-.03em;
              line-height:1.15;margin-bottom:8px">
    My Legislative Tracker
  </h1>
  <div style="color:rgba(255,255,255,.92);font-size:13px;line-height:1.6">
    Track bills, monitor topics by keyword or category, see exactly what
    changed in the legislative text, and get AI-generated digests —
    personalized for your role at the University of Illinois.
  </div>
</div>
""", unsafe_allow_html=True)

if not _user_authed:
    st.info("Sign in with your University of Illinois email to save bills "
            "and topics across sessions.")

# ══════════════════════════════════════════════════════════════════════════════
# ACTIVE TEAM RESOLUTION  (team_service model — replaces the old free-text `team`)
# ══════════════════════════════════════════════════════════════════════════════
# One resolver, shared by every tab. Membership now lives in the `teams` container
# (owned teams + invite/accept), and a user can belong to several teams, so we
# resolve ONE "active team" per run (persisted in session_state) and derive its
# rosters here. Every downstream section (flag picker, digest, briefing share,
# overlap, My Team) reads these instead of `_user.get("team")`.
#
#   _my_teams        all memberships: [{team_id, name, owner_email, role, status}]
#   _active_team     the currently-selected active-membership dict (or None)
#   _active_team_id  its id ("" if none)
#   _active_roster   active teammates' emails (excl. me) — the "send-to" roster
#   _sharing_roster  subset of _active_roster who have sharing enabled — the
#                    "exposes-their-data" roster (shared bills, overlap, notes)
#   _pending_invites memberships with status 'invited' — accept/decline in Profile
_my_teams: list = []
_active_team = None
_active_team_id = ""
_active_roster: list = []
_sharing_roster: list = []
_pending_invites: list = []
_team_name_by_email: dict = {}

if _user_authed and _user_id:
    try:
        from team_service import (list_teams_for_user, active_teammates,
                                   get_share_enabled)
        from user_auth import (get_user as _get_user_rec,
                               canonical_email as _canon_nm)

        _my_teams = list_teams_for_user(_user_id)
        _active_memberships = [t for t in _my_teams if t.get("status") == "active"]
        _pending_invites    = [t for t in _my_teams if t.get("status") == "invited"]

        # Resolve the active team: honour the session pick if still valid, else
        # fall back to the first active membership.
        _valid_ids = {t["team_id"] for t in _active_memberships}
        _sel = st.session_state.get("active_team_id", "")
        if _sel not in _valid_ids:
            _sel = _active_memberships[0]["team_id"] if _active_memberships else ""
            st.session_state["active_team_id"] = _sel
        _active_team_id = _sel
        _active_team = next(
            (t for t in _active_memberships if t["team_id"] == _sel), None)

        if _active_team_id:
            _active_roster = active_teammates(_user_id, _active_team_id)
            for _e in _active_roster:
                try:
                    # Records are duplicated across @uillinois.edu / @illinois.edu;
                    # display_name + sharing live on the canonical (@illinois.edu)
                    # twin, so resolve canonical first, then fall back to the raw
                    # address if that's the only record that exists.
                    _rec = (_get_user_rec(_canon_nm(_e))
                            or _get_user_rec(_e) or {})
                except Exception:
                    _rec = {}
                _team_name_by_email[_e] = (_rec.get("display_name")
                                           or _e.split("@")[0])
                if get_share_enabled(_rec):
                    _sharing_roster.append(_e)
        _team_name_by_email[_user_id] = "you"
    except Exception:
        # Any failure here must not break the tracker — fall back to no team.
        _my_teams = _my_teams or []

tab_topics, tab_bills, tab_briefing, tab_team, tab_digest, tab_settings = st.tabs([
    # Audit bug 7: these six strings are the tabs' accessible names, so each
    # emoji was announced before the tab name ("magnifying glass tilted left,
    # Search and Pin"). Removed rather than hidden — st.tabs offers no way to
    # mark part of a label decorative.
    "Search & Pin", "My Bills", "My Briefing", "My Team", "My Digest", "My Profile"
])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — MY BILLS
# ══════════════════════════════════════════════════════════════════════════════
with tab_bills:
    st.markdown('<h2 class="sect-head">Tracked Bills</h2>',
                unsafe_allow_html=True)
    st.caption("Bills you're watching. Expand any card to see the full "
               "legislative actions tab and what changed since you pinned it.")

    saved_bills = _cosmos_list("bill")

    # Team roster for the "flag a teammate" picker in each annotation.
    # Send-to picker → the full active roster of the selected team.
    _team_roster = {e: _team_name_by_email.get(e, e.split("@")[0])
                    for e in _active_roster}
    master      = load_master(CURRENT_SESSION)

    # Teammates' shared annotations, indexed by bill_id and fetched ONCE (not per
    # card). Only teammates who consented to share appear in _sharing_roster.
    _team_bill_annots = {}
    try:
        from user_profile import list_team_shared as _lts_bills
        for _sit in _lts_bills(list(_sharing_roster), "bill"):
            _sd = _sit.get("data", {}) or {}
            _sann = _sd.get("annotation", {}) or {}
            if not str(_sann.get("assessment", "")).strip():
                continue
            _sbid = str(_sd.get("bill_id", "") or "")
            if not _sbid:
                continue
            _team_bill_annots.setdefault(_sbid, []).append({
                "author":     _display_name(_sit.get("_owner", "")),
                "assessment": _sann.get("assessment", ""),
                "direction":  _sann.get("direction", "unclear"),
                "affects":    _sann.get("affects", ""),
            })
    except Exception:
        _team_bill_annots = {}

    if saved_bills:
        _n = len(saved_bills)
        st.caption(f"{_n} / {MAX_TRACKED_BILLS} bills tracked"
                   + ("  ·  limit reached — remove one to add more" if _n >= MAX_TRACKED_BILLS else ""))

    if saved_bills:
        for item in saved_bills:
            data    = item.get("data", {})
            bid     = str(data.get("bill_id", ""))
            sess_id = int(data.get("session_id", CURRENT_SESSION))

            if not master.empty and bid in master["bill_id"].values:
                row = master[master["bill_id"] == bid].iloc[0]
            else:
                row = pd.Series({
                    "bill_id": bid,
                    "bill_number": data.get("bill_number",""),
                    "title": data.get("title",""),
                    "session_id": sess_id,
                    "impact_rating_score": 0,
                })
            with st.container(border=True):
                # 1) Bill info card only — U of I impact, feedback, and Actions &
                #    Changes are rendered below, in the reorganized order.
                render_bill_card(row, saved_item=item, render_uiuc=False,
                                 render_feedback=False, render_actions=False)

                # 2) Passage-likelihood trail — exact replica of the bill-predictions
                #    probability trail (Chart.js area chart + action history), right
                #    under the floor-vote bar.
                _render_prediction_trail(bid, current_prob=row.get("y_prob"))

                # 3) Actions & Changes (collapsible), under the trail.
                _render_actions_changes(bid, str(row.get("bill_number", "")),
                                        item, row, sess_id)

                # 4) Impact assessments, grouped: U of I impact FIRST (full text,
                #    with its rating) then every prompt rating — shared helper.
                _render_impact_assessments(row, bid)

                # 5) Give feedback on impact — redesigned, per-assessment (U of I impact
                #    or any of your prompts), structured for the model.
                _render_impact_feedback(bid, _feedback_categories(row),
                                        sfx=str(item.get("id", bid)), bill_label=_bill_label(row))

                # ── My annotation (structured human judgement; shareable) ───────
                _ann = data.get("annotation", {}) or {}
                _dir_opts = ["unclear", "positive", "negative", "mixed"]
                _dir_labels = {"unclear": "Unclear", "positive": "Positive",
                               "negative": "Negative", "mixed": "Mixed"}
                _has_ann = bool(str(_ann.get("assessment", "")).strip())
                with st.expander(
                    "My annotation" + ("  ·  written" if _has_ann else ""),
                    expanded=False,
                ):
                    st.caption(
                        "Your read on this bill — this is expert judgement the AI can't "
                        "produce. If you share with a team, it travels with the bill, "
                        "attributed to you, and feeds your team digest."
                    )
                    with st.form(f"ann_form_{bid}", clear_on_submit=False):
                        _a_assess = st.text_area(
                            "Your assessment", value=str(_ann.get("assessment", "")),
                            height=100, key=f"ann_assess_{bid}",
                            placeholder="What's your read? Why does this matter, and what's "
                                        "your concern? e.g. Shifts reporting annual→quarterly; "
                                        "real load for SPA in the FY27 cycle.",
                        )
                        _a_dir = st.radio(
                            "Impact direction", _dir_opts,
                            index=_dir_opts.index(_ann.get("direction", "unclear"))
                                  if _ann.get("direction", "unclear") in _dir_opts else 0,
                            format_func=lambda x: _dir_labels[x],
                            horizontal=True, key=f"ann_dir_{bid}",
                        )
                        _c1, _c2 = st.columns(2)
                        with _c1:
                            _a_affects = st.text_input(
                                "Who / what it affects", value=str(_ann.get("affects", "")),
                                key=f"ann_affects_{bid}",
                                placeholder="e.g. Sponsored Programs Admin",
                            )
                        with _c2:
                            _flag_opts = list(_team_roster.keys())
                            _prev_flagged = [e for e in (_ann.get("flagged", []) or [])
                                             if e in _flag_opts]
                            if _flag_opts:
                                _a_flagged = st.multiselect(
                                    "Flag teammates", _flag_opts,
                                    default=_prev_flagged,
                                    format_func=lambda e: _team_roster.get(e, e),
                                    key=f"ann_flag_{bid}",
                                    help="They'll see this flag in their My Team tab and digest.",
                                )
                            else:
                                st.caption("Flag teammates")
                                st.caption("_No teammates on your team yet._")
                                _a_flagged = []
                        _a_notify = st.text_input(
                            "Who needs to know (roles / offices outside the app)",
                            value=str(_ann.get("notify_who", "")),
                            key=f"ann_notify_{bid}",
                            placeholder="e.g. VP for Academic Affairs, grants compliance",
                            help="Free text for people or offices not on your team. "
                                 "Use 'Flag teammates' above to notify app users directly.",
                        )
                        if st.form_submit_button("Save annotation", type="primary"):
                            try:
                                from user_profile import set_bill_annotation
                                _new_ann = {"assessment": _a_assess, "direction": _a_dir,
                                            "affects": _a_affects, "flagged": _a_flagged,
                                            "notify_who": _a_notify}
                                if set_bill_annotation(_user_id, bid, _new_ann):
                                    data["annotation"] = _new_ann  # reflect locally
                                    st.success("Annotation saved.")
                                else:
                                    st.warning("Couldn't find this bill to annotate.")
                            except Exception as e:
                                st.error(f"Save failed: {e}")

                # ── Imported annotation from a teammate (read-only, attributed) ──
                _imported = data.get("imported_annotation")
                if _imported and str(_imported.get("assessment", "")).strip():
                    _owner = _display_name(_imported.get("author", ""))
                    _dcolor = {"positive": "#16A34A", "negative": "#DC2626",
                               "mixed": "#A2570F", "unclear": "#454F5A"}.get(
                                   _imported.get("direction", "unclear"), "#454F5A")
                    st.markdown(
                        f"<div style='margin:4px 0 8px 0;padding:8px 12px;border-radius:8px;"
                        f"background:#F8FAFC;border-left:3px solid {_dcolor}'>"
                        f"<div style='font-size:11px;color:#454F5A;font-weight:600;"
                        f"text-transform:uppercase;letter-spacing:.04em'>"
                        f"{_owner}'s read · {_imported.get('direction','unclear').title()}</div>"
                        f"<div style='font-size:13px;color:#334155;margin-top:3px'>"
                        f"{_imported.get('assessment','')}</div>"
                        + (f"<div style='font-size:11.5px;color:#454F5A;margin-top:3px'>"
                           f"Affects: {_imported.get('affects','')}</div>"
                           if _imported.get('affects') else "")
                        + "</div>",
                        unsafe_allow_html=True)
                # ── Teammates' annotations on this bill (shared with you) ─────────
                _tanns = _team_bill_annots.get(bid, [])
                if _tanns:
                    st.markdown("<div style='font-size:12px;font-weight:700;"
                                "color:#334155;margin:8px 0 2px'>"
                                "<span aria-hidden='true'>\U0001F465</span> Team annotations</div>",
                                unsafe_allow_html=True)
                    for _ta in _tanns:
                        _dc = {"positive": "#16A34A", "negative": "#DC2626",
                               "mixed": "#A2570F", "unclear": "#454F5A"}.get(
                                   _ta.get("direction", "unclear"), "#454F5A")
                        st.markdown(
                            f"<div style='margin:3px 0 6px 0;padding:8px 12px;"
                            f"border-radius:8px;background:#F8FAFC;"
                            f"border-left:3px solid {_dc}'>"
                            f"<div style='font-size:11px;color:#454F5A;font-weight:600;"
                            f"text-transform:uppercase;letter-spacing:.04em'>"
                            f"<span aria-hidden='true'>\U0001F4DD</span> {_ta['author']}'s read \u00b7 "
                            f"{_ta.get('direction','unclear').title()}</div>"
                            f"<div style='font-size:13px;color:#334155;margin-top:3px'>"
                            f"{_ta.get('assessment','')}</div>"
                            + (f"<div style='font-size:11.5px;color:#454F5A;"
                               f"margin-top:3px'>Affects: {_ta.get('affects','')}</div>"
                               if _ta.get('affects') else "")
                            + "</div>", unsafe_allow_html=True)
    else:
        st.markdown("""
        <div class="empty-state">
          <div style="font-weight:600;margin-bottom:6px">No bills tracked yet</div>
          <div>Use the <b>Search &amp; Pin</b> tab to find and pin bills you care about.</div>
        </div>
        """, unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — MY TOPICS
# ══════════════════════════════════════════════════════════════════════════════

with tab_topics:
    # Audit bug 7: rendered HTML, so the glyph stays visible but is removed
    # from the accessibility tree.
    st.markdown('<h2 class="sect-head">'
                'Search &amp; Pin</h2>',
                unsafe_allow_html=True)
    st.caption("Free search across every active bill using your own metadata — "
               "your categories, keywords, impact areas, and passage likelihood. "
               "Find what's relevant, then click Pin to add it to your "
               "**My Bills** board.")

    master = load_master(CURRENT_SESSION)
    saved_bills = _cosmos_list("bill")

    # ── Load metadata that powers profile-aware search ──────────────────────
    from impact_loader import load_impact_results as _load_ir
    _ir_all = _load_ir(_user_id)                       # prompts + role profile
    _saved_cats_sp = _cosmos_list("category")
    already_ids = {str(s.get("data", {}).get("bill_id")) for s in saved_bills}
    _at_cap = len(saved_bills) >= MAX_TRACKED_BILLS

    # Impact "lenses" = the user's prompts + their always-on role profile
    # (scored into __profile__<uid>). Build label -> prompt_id for the picker.
    _lens_opts = {}
    if not _ir_all.empty and "prompt_id" in _ir_all.columns:
        for _pid, _grp in _ir_all.groupby("prompt_id"):
            _lbl = str(_grp.iloc[0].get("prompt_label", "") or "")
            if str(_pid).startswith("__profile__"):
                _lbl = "My role profile"
            elif not _lbl:
                _lbl = str(_pid)
            _lens_opts[_lbl] = _pid
    _LVL_TO_LABEL = {"High": "High Impact", "Moderate": "Moderate Impact",
                     "Somewhat": "Low Impact", "Not important": "No Impact"}

    # ── Row 1: specific bill / keyword lookup + search-mode toggle ─────────
    try:
        import semantic_search as _ss_avail
        _sem_ok = _ss_avail.available()
    except Exception:
        _sem_ok = False
    _q1, _q2 = st.columns([4, 1.2])
    with _q1:
        pin_q = st.text_input(
            "Find a specific bill, or search keywords",
            placeholder="e.g. HB1234 · a bill title · graduate tuition · capital construction",
            key="pin_search", max_chars=120)
    with _q2:
        st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)
        if _sem_ok:
            # Audit issue 12: the label was collapsed, so this control had no
            # visible or announced name. Audit issue 7: the option values were
            # emoji-prefixed, so they announced as "brain, Semantic".
            _mode = st.radio(
                "Match mode", ["Semantic", "Exact"], horizontal=True,
                key="pin_search_mode",
                help="Semantic matches by meaning and ranks by relevance. Exact "
                     "matches your literal words — use it to pull up a specific "
                     "bill number or an exact phrase. Semantic loads a search "
                     "index on first use (a few seconds), then it's cached.")
            pin_semantic = (_mode == "Semantic")
        else:
            pin_semantic = False
            st.caption("Exact match")

    # ── Row 2: profile lenses + rating levels (most valuable filter) ────────
    _pl1, _pl2 = st.columns([2, 2])
    with _pl1:
        _lens_pick = st.multiselect(
            "Filter by my profile & prompts", list(_lens_opts.keys()),
            key="pin_lenses",
            help="Your saved impact prompts and your always-on role profile, "
                 "scored nightly. Pick one or more to keep only the bills they "
                 "rate at the levels you choose.")
    with _pl2:
        _lens_levels = st.multiselect(
            "Rated at least", list(_LVL_TO_LABEL.keys()),
            default=["High", "Moderate"], key="pin_lens_levels",
            help="Defaults to High + Moderate. Widen to include lower ratings.")
    _keep_labels = {_LVL_TO_LABEL[l] for l in _lens_levels} if _lens_levels else set()
    st.caption("Create your role profile, impact prompts, and saved categories in "
               "the **My Profile** tab. Note: new prompts and profile changes "
               "aren't scored instantly — their ratings take a few days to appear here.")

    # ── Row 3: saved categories · sort (momentum lives in Sort, not a
    #    separate filter — the two were redundant) ────────────────────────────
    _r3a, _r3c = st.columns([1, 1])
    with _r3a:
        _cat_pick = st.multiselect(
            "My saved categories",
            [c.get("label", "(unnamed)") for c in _saved_cats_sp],
            key="pin_cats",
            help="The keyword/topic monitors you built in My Profile.")
    with _r3c:
        pin_sort = st.selectbox(
            "Sort by",
            ["Likely to pass", "Gaining momentum", "Losing momentum",
             "Recent action", "Highest impact"], key="pin_sort")

    # ── Advanced: standardized metadata + raw-column keyword search ─────────
    with st.expander("More filters — metadata dropdowns & column keyword search"):
        def _distinct(col):
            if master.empty or col not in master.columns:
                return []
            return sorted({str(v).strip() for v in master[col].dropna()
                           if str(v).strip()
                           and str(v).strip().lower() not in ("none", "n/a")})
        _m1, _m2, _m3 = st.columns(3)
        with _m1:
            _f_cat = st.multiselect("Category", _distinct("category_main_label"),
                                    key="pin_md_cat")
        with _m2:
            _f_dom = st.multiselect("Policy domain",
                                    _distinct("policy_domain_standardized"),
                                    key="pin_md_dom")
        with _m3:
            # The beneficiary categories can sit under different column names
            # depending on the extraction run; use the first one that actually
            # has values, and if none does, disable the control with a note
            # instead of showing an empty "No options to select" dropdown.
            _ben_col = next(
                (c for c in ("beneficiary_primary_category",
                             "intended_beneficiary_standardized",
                             "beneficiary_group_standardized",
                             "beneficiary_standardized",
                             "primary_beneficiary_standardized")
                 if _distinct(c)), None)
            if _ben_col:
                _f_ben = st.multiselect("Intended beneficiary",
                                        _distinct(_ben_col), key="pin_md_ben")
            else:
                _f_ben = []
                st.multiselect("Intended beneficiary", [], key="pin_md_ben",
                               disabled=True,
                               help="No beneficiary categories are populated in "
                                    "the current dataset.")
        _mi1, _mi2 = st.columns([1, 3])
        with _mi1:
            pin_impact = st.selectbox("Min. U of I impact", [0, 1, 2, 3],
                                      format_func=lambda x: IMPACT_META[x][0],
                                      index=0, key="pin_impact")
        st.markdown("<div style='font-size:12px;font-weight:600;color:#475569;"
                    "margin-top:8px'>Keyword search within specific columns</div>",
                    unsafe_allow_html=True)
        st.caption("Searches the raw source text of the columns you pick — e.g. a "
                   "term restricted to Beneficiaries or Key Provisions. Matching is "
                   "literal (exact terms), not yet semantic.")
        _ck1, _ck2 = st.columns([2, 3])
        with _ck1:
            _col_pick = st.multiselect("Columns to search",
                                       list(SEARCHABLE_FIELDS.keys()),
                                       key="pin_cols")
        with _ck2:
            _col_kw = st.text_input("Keywords (comma-separated)",
                                    key="pin_col_kw", max_chars=120)

    # ══ Apply filters ══════════════════════════════════════════════════════
    if master.empty:
        st.info("No data available for this session yet.")
    else:
        results = master.copy()
        results["bill_id"] = results["bill_id"].astype(str)

        # Semantic ranking (opt-in). Only touches the model/index when the toggle
        # is on AND there's a query, so the default path stays instant. Falls back
        # to literal matching if the index/deps aren't available.
        _sem_active = bool(pin_semantic and pin_q.strip())
        _sem_rank = {}
        if _sem_active:
            try:
                import semantic_search as _ss
                _sem_ids, _sem_scores, _sem_err = _ss.rank_bill_ids(
                    pin_q.strip(), CURRENT_SESSION, top_k=400)
                if _sem_err or not _sem_ids:
                    st.caption(f"Semantic search unavailable "
                               f"({_sem_err or 'no matches'}) — using literal match.")
                    _sem_active = False
                else:
                    _sem_rank = {b: r for r, b in enumerate(_sem_ids)}
            except Exception as _e_sem:
                st.caption("Semantic search unavailable — using literal match.")
                _sem_active = False

        # (a) profile lenses (prompts + role profile), OR across selected lenses
        if _lens_pick and _keep_labels and not _ir_all.empty:
            _lens_ids = [_lens_opts[l] for l in _lens_pick]
            _hit = _ir_all[_ir_all["prompt_id"].isin(_lens_ids)
                           & _ir_all["rating_label"].isin(_keep_labels)]
            results = results[results["bill_id"].isin(set(_hit["bill_id"].astype(str)))]

        # (b) saved categories (union of their matches)
        if _cat_pick and not results.empty:
            _catmask = pd.Series(False, index=results.index)
            for _c in _saved_cats_sp:
                if _c.get("label", "(unnamed)") not in _cat_pick:
                    continue
                _cd = _c.get("data", {})
                if _cd.get("type") == "keyword" and _cd.get("keywords") and _cd.get("fields"):
                    for _kw in _cd["keywords"]:
                        for _f in _cd["fields"]:
                            _fc = _resolve_col(results, _f)
                            if _fc in results.columns:
                                _catmask |= results[_fc].astype(str).str.lower(
                                    ).str.contains(str(_kw).lower(), na=False)
                else:
                    _colm = {"policy_domain": "policy_domain_standardized",
                             "beneficiary": "beneficiary_primary_category"}.get(
                                 _cd.get("type"), "category_main_label")
                    if _colm in results.columns:
                        _catmask |= results[_colm].astype(str).str.contains(
                            str(_cd.get("value", "")), case=False, na=False)
            results = results[_catmask]

        # (d) standardized metadata dropdowns
        for _col, _sel in (("category_main_label", _f_cat),
                           ("policy_domain_standardized", _f_dom),
                           (_ben_col or "beneficiary_primary_category", _f_ben)):
            if _sel and _col in results.columns and not results.empty:
                results = results[results[_col].astype(str).isin(_sel)]

        # (e) min U of I impact
        if pin_impact > 0 and "impact_rating_score" in results.columns and not results.empty:
            results = results[results["impact_rating_score"] >= pin_impact]

        # (f) raw column-scoped keyword search (literal; OR across cols+keywords)
        if _col_pick and _col_kw.strip() and not results.empty:
            _kws = [k.strip().lower() for k in _col_kw.split(",") if k.strip()]
            _cmask = pd.Series(False, index=results.index)
            for _fname in _col_pick:
                _rawcol = _resolve_col(results, SEARCHABLE_FIELDS[_fname])
                if _rawcol in results.columns:
                    _s = results[_rawcol].astype(str).str.lower()
                    for _k in _kws:
                        _cmask |= _s.str.contains(_k, na=False)
            results = results[_cmask]

        # (g) query search — semantic (by meaning) or literal (exact terms)
        if pin_q.strip() and not results.empty:
            if _sem_active:
                results = results[results["bill_id"].isin(set(_sem_rank))]
            else:
                _q = pin_q.strip().lower()
                _fmask = pd.Series(False, index=results.index)
                for _col in ["bill_number", "title", "llama_summary_raw",
                             _resolve_col(results, "legislative_goal_raw"),
                             "potential_impact_raw",
                             _resolve_col(results, "key_provisions_raw")]:
                    if _col in results.columns:
                        _fmask |= results[_col].astype(str).str.lower().str.contains(_q, na=False)
                results = results[_fmask]

        # ── sort ──
        if pin_sort == "Likely to pass" and "y_prob" in results.columns:
            results = results.sort_values("y_prob", ascending=False, na_position="last")
        elif pin_sort in ("Gaining momentum", "Losing momentum") and "momentum" in results.columns:
            _order = ({"Gaining": 0, "Stable": 1, "Losing": 2}
                      if pin_sort == "Gaining momentum"
                      else {"Losing": 0, "Stable": 1, "Gaining": 2})
            results = (results.assign(_m=results["momentum"].map(_order).fillna(3))
                       .sort_values(["_m", "y_prob"], ascending=[True, False],
                                    na_position="last").drop(columns=["_m"]))
        elif pin_sort == "Recent action" and "last_action_date" in results.columns:
            results = results.assign(_d=pd.to_datetime(
                results["last_action_date"], errors="coerce")).sort_values(
                "_d", ascending=False, na_position="last").drop(columns=["_d"])
        elif pin_sort == "Highest impact" and "impact_rating_score" in results.columns:
            _sc = "y_prob" if "y_prob" in results.columns else "impact_rating_score"
            results = results.sort_values(["impact_rating_score", _sc],
                                          ascending=[False, False], na_position="last")

        # Semantic relevance wins the ordering when it's driving the search.
        if _sem_active and not results.empty:
            results = (results.assign(
                _sr=results["bill_id"].map(_sem_rank).fillna(1e9))
                .sort_values("_sr").drop(columns="_sr"))

        # ── likelihood histogram + range slider (LAST; reflects every filter
        #    above and re-renders on each selection) ──
        # Bug 27: the slider is ALWAYS drawn — even on a run with no matching
        # bills or no probability column — so Streamlit never discards its value
        # (a run that finishes without a widget drops that widget's state, which
        # QA saw as the slider resetting to 0).
        if True:
            import numpy as _np
            _prob_pct = (pd.to_numeric(results["y_prob"], errors="coerce").dropna() * 100
                         if "y_prob" in results.columns else pd.Series(dtype=float))
            if True:
                _lc_col, _lc_pad = st.columns([3, 2])
                with _lc_col:
                    st.markdown("<div style='font-size:12px;font-weight:600;"
                                "color:#475569;margin-top:6px'>Floor-vote "
                                "likelihood</div>", unsafe_allow_html=True)
                    # Audit issue 12: label was collapsed; the adjacent styled
                    # div carried the name visually only.
                    _lo, _hi = st.slider(
                        "Floor-vote likelihood (%)", 0, 100, (0, 100),
                        key="pin_prob_range",
                        help="Narrow to a passage-likelihood band. The bars show how "
                             "the bills matching your other filters are distributed — "
                             "they update as you change any filter above.")
                    _dbg_row["drawn"] = "yes"; _dbg_row["returned"] = f"{_lo}-{_hi}"   # Bug 27 diagnostic
                    _counts, _edges = _np.histogram(_prob_pct, bins=20, range=(0, 100))
                    _hist = None if _prob_pct.empty else pd.DataFrame(
                        {"likelihood %": [int(_edges[i]) for i in range(len(_counts))],
                         "bills": _counts}).set_index("likelihood %")
                    if _hist is not None:
                        st.bar_chart(_hist, height=96)
                        sr_chart_table(
                            "Floor-vote likelihood distribution",
                            band_table(_counts, _edges, "Floor-vote likelihood"),
                            "How the bills matching your current filters are spread "
                            "across likelihood bands.")
                if (_lo, _hi) != (0, 100) and "y_prob" in results.columns:
                    _pn = pd.to_numeric(results["y_prob"], errors="coerce") * 100
                    results = results[(_pn >= _lo) & (_pn <= _hi)]

        # ── results ──
        if _sem_active:
            st.caption(f"{len(results):,} bill(s) match your filters · ranked by "
                       f"semantic relevance to “{pin_q.strip()}” · showing top 20")
        else:
            st.caption(f"{len(results):,} bill(s) match your filters · showing top 20")
        if results.empty:
            st.info("No bills match — try widening your filters (lower a rating "
                    "level, clear a category, or loosen the likelihood band).")
        for _, row in results.head(20).iterrows():
            _rbid = str(row.get("bill_id", ""))
            # One bounding box per bill, mirroring My Bills: info card (U of I
            # impact and feedback move below), Actions & Changes, the full grouped
            # impact assessments, then the feedback expander.
            with st.container(border=True):
                render_bill_card(row, render_uiuc=False, render_feedback=False,
                                 render_actions=True)
                _render_impact_assessments(row, _rbid)
                if _rbid and _user_authed:
                    _render_impact_feedback(_rbid, _feedback_categories(row),
                                            sfx=f"sp_{_rbid}", bill_label=_bill_label(row))

    st.markdown("---")
    st.markdown('<h3 class="sect-head">Your Topic &amp; Keyword Monitors</h3>',
                unsafe_allow_html=True)
    st.caption("Bills matching the categories you've saved, with predictions and "
               "impact filters. Active session (104th GA) only.")

    saved_cats = _cosmos_list("category")
    saved_prompts = _cosmos_list("impact_prompt")

    if not saved_cats:
        st.markdown("""
        <div class="empty-state">
          <div style="font-weight:600;margin-bottom:6px">No topics watched yet</div>
          <div>Create your interest categories in the <b>My Profile</b> tab.</div>
        </div>
        """, unsafe_allow_html=True)
    else:
        master_t = load_master(CURRENT_SESSION)

        # ── Controls row: category focus + sort + impact prompt filter ───────
        ctl1, ctl2, ctl3, ctl4 = st.columns([3, 2, 3, 2])
        with ctl1:
            _labels = ["All categories"] + [c.get("label", "(unnamed)") for c in saved_cats]
            _focus  = st.selectbox("Category", _labels, key="topic_focus")
        with ctl2:
            _sort = st.selectbox(
                "Sort by",
                ["Most Imminent", "Gaining Momentum", "Recent Action", "Best Match"],
                key="topic_sort",
            )
        with ctl3:
            _prompt_opts = ["(none)"] + [p.get("label", "(unnamed)") for p in saved_prompts]
            _prompt_pick = st.selectbox(
                "Filter by my impact prompt", _prompt_opts, key="topic_impact_prompt",
                help="Shows only bills rated Moderate/High impact for the selected "
                     "impact prompt. Prompts are scored nightly on the server.",
            )
        with ctl4:
            # Impact level filter — maps to impact_rating_score (3/2/1/0)
            _LVL_TO_SCORE = {"High": 3, "Moderate": 2, "Low": 1, "None": 0}
            _sel_levels = st.multiselect(
                "Impact level", ["High", "Moderate", "Low", "None"],
                default=["High", "Moderate", "Low", "None"],
                key="topic_levels",
                help="Filter by the bill's U of I impact rating.",
            )
        _keep_scores = {_LVL_TO_SCORE[l] for l in _sel_levels} if _sel_levels else {0,1,2,3}

        # Load this user's impact results once if a prompt filter is active
        _impact_map = {}
        if _prompt_pick != "(none)":
            _sel_prompt = next(
                (p for p in saved_prompts if p.get("label") == _prompt_pick), None)
            if _sel_prompt:
                try:
                    from impact_loader import impact_map as _imp_map
                    _impact_map = _imp_map(_user_id, _sel_prompt.get("id", ""))
                except Exception:
                    _impact_map = {}
                if not _impact_map:
                    st.info("No impact results yet for this prompt — scoring runs "
                            "nightly. Results appear the morning after you create "
                            "a prompt.")

        st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)

        # ── Per-category sections ─────────────────────────────────────────────
        for item in saved_cats:
            if _focus != "All categories" and item.get("label", "(unnamed)") != _focus:
                continue
            data     = item.get("data", {})
            cat_type = data.get("type", "")
            keywords = data.get("keywords", [])
            fields   = data.get("fields", [])
            cat_val  = data.get("value", "")
            label    = item.get("label", cat_val)

            subset = pd.DataFrame()
            if not master_t.empty:
                if cat_type == "keyword" and keywords and fields:
                    mask = pd.Series(False, index=master_t.index)
                    for kw in keywords:
                        for f in fields:
                            fc = _resolve_col(master_t, f)
                            if fc in master_t.columns:
                                mask |= (master_t[fc].astype(str).str.lower()
                                                    .str.contains(kw.lower(), na=False))
                    subset = master_t[mask]
                else:
                    col_map = {"policy_domain": "policy_domain_standardized",
                               "beneficiary":   "beneficiary_primary_category"}
                    col = col_map.get(cat_type, "category_main_label")
                    if col in master_t.columns:
                        subset = master_t[master_t[col].str.contains(
                            cat_val, case=False, na=False)]

            # Apply impact-prompt filter if active
            if _impact_map and not subset.empty:
                _keep = subset["bill_id"].astype(str).map(
                    lambda b: _impact_map.get(b, "") in
                              ("Moderate Impact", "High Impact"))
                subset = subset[_keep]

            # Apply impact-level filter (by U of I rating score)
            if not subset.empty and "impact_rating_score" in subset.columns \
                    and len(_keep_scores) < 4:
                subset = subset[subset["impact_rating_score"].isin(_keep_scores)]

            # Apply sort
            if not subset.empty:
                if _sort == "Most Imminent" and "y_prob" in subset.columns:
                    subset = subset.sort_values("y_prob", ascending=False)
                elif _sort == "Gaining Momentum" and "momentum" in subset.columns:
                    _mom_rank = {"Gaining": 0, "Stable": 1, "Losing": 2}
                    subset = subset.assign(
                        _mr=subset["momentum"].map(_mom_rank).fillna(3)
                    ).sort_values(["_mr", "y_prob"],
                                  ascending=[True, False]).drop(columns=["_mr"])
                elif _sort == "Recent Action" and "last_action_date" in subset.columns:
                    subset = subset.sort_values("last_action_date", ascending=False)
                # "Best Match" keeps natural match order

            n_bills = len(subset)
            gaining = int((subset["momentum"] == "Gaining").sum()) \
                      if "momentum" in subset.columns else 0

            field_tags = "".join(
                f'<span class="field-badge">{next((k for k,v in SEARCHABLE_FIELDS.items() if v==f),f)}</span>'
                for f in fields) if fields else ""
            kw_tags = "".join(
                f'<span class="kw-tag">{kw}</span>' for kw in keywords) if keywords else ""

            col_info, col_del = st.columns([11, 1])
            with col_info:
                st.markdown(f"""
<div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:10px;
            padding:14px 16px;margin-bottom:8px;">
  <div style="font-size:13px;font-weight:600;color:#0F172A;margin-bottom:4px">{label}</div>
  <div style="margin-bottom:6px">{field_tags}{kw_tags}</div>
  <div style="font-size:11px;color:#454F5A">
    {n_bills:,} matching bills · {gaining} gaining momentum
    · {"Digest on" if item.get("notify") else "Digest off"}
  </div>
</div>
""", unsafe_allow_html=True)

                if n_bills > 0:
                    # Paginated: show N at a time with a "Show more" button
                    _page_key = f"topic_page_{item.get('id','')}"
                    if _page_key not in st.session_state:
                        st.session_state[_page_key] = 10
                    _show_n = st.session_state[_page_key]

                    for _, row in subset.head(_show_n).iterrows():
                        _bid = str(row.get("bill_id", ""))
                        if _impact_map and _bid in _impact_map:
                            st.markdown(
                                f'<div style="font-size:11px;color:#7C3AED;'
                                f'font-weight:600;margin:2px 0">'
                                f'{_impact_map[_bid]} — "{_prompt_pick}"</div>',
                                unsafe_allow_html=True)
                        render_bill_card(row)

                    if n_bills > _show_n:
                        if st.button(f"Show more ({n_bills - _show_n} remaining)",
                                     key=f"more_{item.get('id','')}"):
                            st.session_state[_page_key] = _show_n + 10
                            st.rerun()

            with col_del:
                if _user_authed:
                    if st.button("Delete", key=f"delcat_{item.get('id','')}",
                                 help="Stop watching"):
                        if _cosmos_delete(item.get("id", "")):
                            st.rerun()

with tab_digest:
    st.markdown('<h2 class="sect-head">My Digest</h2>', unsafe_allow_html=True)
    st.caption(
        "Your debrief across everything in the tracker — bills you track, bills "
        "the AI flagged as high-impact for you, momentum, your team's overlap, and "
        "what teammates shared. Pick any lens below to generate a focused AI read, "
        "tailored to your role and stakeholders. For the full searchable archive of "
        "every investigated bill, open **My Briefing**; manage email and team "
        "visibility in **My Profile**."
    )

    # ── 🚩 Flagged for you (top of digest — directed hand-offs) ──────────────
    if _user_authed:
        if _active_team_id:
            try:
                from user_profile import list_flagged_for
                _fy_sh = list(_sharing_roster)
                _fy = list_flagged_for(_user_id, _fy_sh) if _fy_sh else []
                if _fy:
                    st.markdown('<h3 class="sect-head">Flagged for you</h3>',
                                unsafe_allow_html=True)
                    st.caption(f"{len(_fy)} bill(s) a teammate specifically flagged for you.")
                    for _it in _fy:
                        _d = _it.get("data", {})
                        _a = _d.get("annotation", {}) or {}
                        _auth = _display_name(_a.get("author", ""))
                        _dir = _a.get("direction", "unclear")
                        _dc = {"positive": "#16A34A", "negative": "#DC2626",
                               "mixed": "#A2570F", "unclear": "#454F5A"}.get(_dir, "#454F5A")
                        st.markdown(
                            f"<div style='margin:6px 0;padding:10px 14px;border-radius:10px;"
                            f"background:#FFFBEB;border:1px solid #FCD34D;border-left:4px solid {_dc}'>"
                            f"<div style='font-size:13px;font-weight:600;color:#0F172A'>"
                            f"{_d.get('bill_number','')} — {_d.get('title','')[:80]}</div>"
                            f"<div style='font-size:11px;color:{_dc};font-weight:600;"
                            f"margin:2px 0'>{_dir.title()} · flagged by {_auth}</div>"
                            f"<div style='font-size:13px;color:#334155'>"
                            f"{_a.get('assessment','')}</div></div>",
                            unsafe_allow_html=True)
                    st.markdown("---")
            except Exception:
                pass

    # ── What your team is flagging ───────────────────────────────────────────
    # Team annotations rendered directly (no AI needed) — their own section so
    # colleagues' expert judgement is visually distinct from AI ratings.
    if _user_authed:
        if _active_team_id:
            try:
                from user_profile import list_team_shared
                _tsh = list(_sharing_roster)
                _flagged = []
                if _tsh:
                    for _it in list_team_shared(_tsh, "bill"):
                        _ta = _it.get("data", {}).get("annotation", {}) or {}
                        if str(_ta.get("assessment", "")).strip():
                            _flagged.append((_it, _ta))
                if _flagged:
                    # group by direction for a quick headline
                    _counts = {}
                    for _it, _ta in _flagged:
                        _d = _ta.get("direction", "unclear")
                        _counts[_d] = _counts.get(_d, 0) + 1
                    _headline = " · ".join(f"{v} {k}" for k, v in sorted(_counts.items()))
                    st.markdown('<h3 class="sect-head">What your team is flagging</h3>',
                                unsafe_allow_html=True)
                    st.caption(f"{len(_flagged)} bill(s) your teammates have annotated  ·  {_headline}")
                    _order = {"negative": 0, "mixed": 1, "positive": 2, "unclear": 3}
                    for _it, _ta in sorted(_flagged,
                                           key=lambda x: _order.get(x[1].get("direction","unclear"), 9)):
                        _d = _it.get("data", {})
                        _dir = _ta.get("direction", "unclear")
                        _dc = {"positive": "#16A34A", "negative": "#DC2626",
                               "mixed": "#A2570F", "unclear": "#454F5A"}.get(_dir, "#454F5A")
                        _auth = _display_name(_ta.get("author", ""))
                        st.markdown(
                            f"<div style='margin:6px 0;padding:10px 14px;border-radius:10px;"
                            f"background:#fff;border:1px solid #E2E8F0;border-left:4px solid {_dc}'>"
                            f"<div style='font-size:13px;font-weight:600;color:#0F172A'>"
                            f"{_d.get('bill_number','')} — {_d.get('title','')[:80]}</div>"
                            f"<div style='font-size:11px;color:{_dc};font-weight:600;"
                            f"text-transform:uppercase;letter-spacing:.03em;margin:2px 0'>"
                            f"{_dir} · flagged by {_auth}</div>"
                            f"<div style='font-size:13px;color:#334155;margin-top:2px'>"
                            f"{_ta.get('assessment','')}</div>"
                            + (f"<div style='font-size:11.5px;color:#454F5A;margin-top:3px'>"
                               f"Affects: {_ta.get('affects','')}"
                               + (f" · Flagged: {', '.join(e.split('@')[0] for e in _ta.get('flagged', []))}"
                                  if _ta.get('flagged') else "")
                               + (f" · Needs to know: {_ta.get('notify_who','')}"
                                  if _ta.get('notify_who') else "") + "</div>"
                               if _ta.get('affects') or _ta.get('flagged') or _ta.get('notify_who') else "")
                            + "</div>",
                            unsafe_allow_html=True)
                    st.markdown("---")
            except Exception:
                pass

    # (The old 'Top from your Briefing' top-3 was removed - it surfaced only
    # three bills by a flat impact sort and truncated their text; the digest
    # lenses below and My Briefing cover this far better.)
    # ── AI Digest — STUBBED pending Azure OpenAI provisioning ────────────────
    # -------------------------------------------------------------------------
    # TO ACTIVATE:
    # 1. portal.azure.com → Create resource → Azure OpenAI
    #    Resource group: legislative-analytics
    #    Name: legplatform-openai
    #    Region: East US
    #    Tier: Standard S0
    # 2. Model deployments → Deploy → gpt-4o-mini
    #    Deployment name: gpt-4o-mini
    # 3. Keys and Endpoint blade → copy URI + Key 1
    # 4. App Service → Environment variables → add:
    #    AZURE_OPENAI_ENDPOINT   = <deployment-location>/
    #    AZURE_OPENAI_KEY        = <key>
    #    AZURE_OPENAI_DEPLOYMENT = gpt-4o-mini
    # 5. requirements.txt already has openai==1.30.5
    # The button below activates automatically once env vars are set.
    # Cost: ~$0.001 per digest at gpt-4o-mini pricing. Negligible.
    # -------------------------------------------------------------------------

    st.markdown("---")
    st.markdown('<h3 class="sect-head">AI-Generated Digest Summary</h3>',
                unsafe_allow_html=True)

    az_endpoint   = os.environ.get("AZURE_OPENAI_ENDPOINT","")
    az_key        = os.environ.get("AZURE_OPENAI_KEY","")
    az_deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT","gpt-4o-mini")
    az_ready      = bool(az_endpoint and az_key)

    if not az_ready:
        st.markdown("""
<div style="background:#F8FAFC;border:1px dashed #CBD5E1;border-radius:12px;
            padding:20px 24px;text-align:center;">
  <div style="font-weight:700;color:#0F172A;margin-bottom:6px">
    AI Digest Coming Soon
  </div>
  <div style="font-size:13px;color:#454F5A;line-height:1.6;max-width:420px;margin:0 auto">
    Once Azure OpenAI is provisioned, this button will generate a plain-English
    weekly summary of your watchlist — framed around University of Illinois
    impact — ready to forward to leadership.
  </div>
</div>
""", unsafe_allow_html=True)
    else:
        saved_b = _cosmos_list("bill")
        saved_c = _cosmos_list("category")

        if not saved_b and not saved_c:
            st.info("Track some bills or topics first, then pick a lens below.")
        else:
            import ai_briefing

            _SYS_DIGEST = (
                "You are a legislative analyst for the University of Illinois IGPA. "
                "Write clear, plain-language analysis for university staff and "
                "leadership. Short paragraphs, no markdown headers or bullet lists "
                "unless asked. If the data is thin, say so briefly rather than "
                "padding. "
                "BILL NAMING (MANDATORY): every time you mention a bill, write its "
                "number followed by an em dash and its short title — e.g. "
                "'HB1234 — Public Records Act'. The title is given for every bill in "
                "the data. NEVER write a bare bill number with no title. "
                "TAILORING (MANDATORY): an 'ABOUT THIS READER' block describes the "
                "person's role, functional area, and stakeholders. Tailor everything "
                "to them — for each bill, connect it concretely to their role, the "
                "stakeholders they serve, and the kinds of bills they track. Say why "
                "it matters to THEM specifically, not just to the university in the "
                "abstract. "
                "CRITICAL: When a bill line includes a 'TEAM ANALYST NOTE', that is "
                "an assessment written by a legislative analyst on this user's own "
                "team who has read the bill. Treat it as expert, authoritative input "
                "and weight it heavily - it can change how the bill should be "
                "interpreted. Always reflect the teammate's read explicitly in your "
                "analysis of that bill (e.g. 'your colleague X flags this as...'), "
                "and let it shape your framing rather than restating the raw bill data."
            )

            # ---- shared loaders (cached; cheap to call per lens) ----------------
            def _master():
                return load_master(CURRENT_SESSION)

            def _my_investigations():
                try:
                    from investigation_loader import load_investigations
                    return load_investigations(_user_id)
                except Exception:
                    return pd.DataFrame()

            def _pinned_rows(md):
                if not saved_b or md.empty:
                    return md.iloc[0:0]
                ids = {str(s.get("data", {}).get("bill_id")) for s in saved_b}
                return md[md["bill_id"].astype(str).isin(ids)]

            # ---- Compact reader profile, prepended to EVERY lens so the AI tailors
            # its analysis to this person's role, functional area, and stakeholders
            # rather than producing generic university-wide summaries. ------------
            def _build_profile_ctx() -> str:
                try:
                    _pf = _cosmos_list("user_profile")
                    _pd = (_pf[0].get("data", {}) if _pf else {})
                except Exception:
                    _pd = {}
                _bits = []
                if _pd.get("title"):
                    _bits.append(f"Role / title: {str(_pd['title'])[:120]}")
                if _pd.get("functional_area"):
                    _bits.append(f"Functional area: {str(_pd['functional_area'])[:120]}")
                if _pd.get("stakeholders"):
                    _bits.append(f"Stakeholders they serve: {str(_pd['stakeholders'])[:200]}")
                if _pd.get("description"):
                    _bits.append(f"About their work: {str(_pd['description'])[:300]}")
                try:
                    _iq = [p.get("data", {}).get("question", "").strip()
                           for p in _cosmos_list("impact_prompt")]
                    _iq = [q for q in _iq if q][:6]
                    if _iq:
                        _bits.append("Topics they actively watch for: "
                                     + "; ".join(_iq)[:400])
                except Exception:
                    pass
                if not _bits:
                    return ""
                return ("ABOUT THIS READER (tailor everything below to them; explain "
                        "why each bill matters to this specific person, their "
                        "stakeholders, and the bills they track):\n- "
                        + "\n- ".join(_bits))

            _profile_ctx = _build_profile_ctx()

            # ---- Team annotation index (expert reads, weighted into every lens) --
            # Any bill a teammate has annotated and shared: keyed by bill_id so any
            # lens that mentions that bill can attach the teammate's assessment.
            # Scoped to the sharing roster, same privacy gate as the rest of My Team.
            def _build_annot_index():
                idx = {}
                try:
                    from user_profile import list_team_shared
                    for _it in list_team_shared(list(_sharing_roster), "bill"):
                        _d = _it.get("data", {}) or {}
                        _ann = _d.get("annotation", {}) or {}
                        _assess = str(_ann.get("assessment", "") or "").strip()
                        _bid = str(_d.get("bill_id", "") or "")
                        if not _bid or not _assess:
                            continue
                        _author = _display_name(_it.get("_owner", ""))
                        idx.setdefault(_bid, []).append({
                            "author": _author,
                            "direction": str(_ann.get("direction", "") or ""),
                            "assessment": _assess,
                            "affects": str(_ann.get("affects", "") or ""),
                        })
                except Exception:
                    return {}
                return idx

            _annot_by_bill = _build_annot_index()

            def _annot_suffix(bid) -> str:
                """The 'TEAM ANALYST NOTE' string for a bill, or '' if none. Every
                lens appends this to its per-bill line so a teammate's expert read
                is always in front of the model wherever that bill appears."""
                anns = _annot_by_bill.get(str(bid))
                if not anns:
                    return ""
                segs = []
                for a in anns[:3]:
                    s = a["author"]
                    if a.get("direction"):
                        s += f" ({a['direction']})"
                    s += f": {a['assessment'][:220]}"
                    if a.get("affects"):
                        s += f" [affects: {a['affects'][:80]}]"
                    segs.append(s)
                return "  || TEAM ANALYST NOTE - " + " ; ".join(segs)

            def _enrich_inv(inv):
                """Attach floor-vote likelihood + impact + title from master onto
                an investigations frame, so a lens can prioritize by them (raw
                investigations don't carry y_prob)."""
                if inv is None or inv.empty:
                    return inv
                md = _master()
                if md.empty:
                    return inv
                cols = [c for c in ("bill_id", "y_prob", "impact_rating_score",
                                    "title", "last_action_date",
                                    "category_main_label") if c in md.columns]
                mm = md[cols].copy()
                mm["bill_id"] = mm["bill_id"].astype(str)
                inv = inv.copy()
                inv["bill_id"] = inv["bill_id"].astype(str)
                drop = [c for c in mm.columns if c != "bill_id" and c in inv.columns]
                if drop:
                    inv = inv.drop(columns=drop)
                return inv.merge(mm, on="bill_id", how="left")

            def _funnel(df, prob_threshold=0.60, floor=10, cap=30):
                """Prioritize then cap candidate bills before the AI sees them, so
                a lens never dumps hundreds of bills at the model. Sort by
                floor-vote likelihood (then impact); keep those at/above the
                threshold; but always keep at least `floor` (backfilling with the
                next-best) and never more than `cap`. Returns (kept_df, pool_size).
                """
                if df is None or df.empty:
                    return df, 0
                d = df.copy()
                d["_p"] = (pd.to_numeric(d["y_prob"], errors="coerce")
                           if "y_prob" in d.columns else pd.Series(index=d.index))
                d["_p"] = d["_p"].fillna(-1.0)
                d["_i"] = (pd.to_numeric(d["impact_rating_score"], errors="coerce")
                           .fillna(0) if "impact_rating_score" in d.columns else 0)
                d = d.sort_values(["_p", "_i"], ascending=[False, False])
                pool = len(d)
                above = d[d["_p"] >= prob_threshold]
                if len(above) >= floor:
                    keep = above.head(cap)
                else:
                    keep = d.head(min(cap, max(floor, len(above))))
                return keep.drop(columns=["_p", "_i"], errors="ignore"), pool

            # ---- LENS 1: Weekly leadership digest -------------------------------
            def _lens_leadership():
                md = _master()
                pinned_lines, seen = [], set()
                for _, r in _pinned_rows(md).head(10).iterrows():
                    seen.add(str(r["bill_id"]))
                    prob_s = (f"{int(r['y_prob']*100)}% floor vote"
                              if pd.notna(r.get("y_prob")) else "")
                    pinned_lines.append(
                        f"- {r['bill_number']}: {r['title']}. {prob_s}. "
                        f"Momentum: {r.get('momentum','N/A')}. "
                        f"U of I: {str(r.get('potential_impact_raw',''))[:200]}"
                        + _annot_suffix(r['bill_id']))
                cat_sections = []
                if saved_c and not md.empty:
                    for cat_item in saved_c:
                        if not cat_item.get("notify", True):
                            continue
                        data = cat_item.get("data", {})
                        ctype, kws = data.get("type", ""), data.get("keywords", [])
                        flds, cval = data.get("fields", []), data.get("value", "")
                        label = cat_item.get("label", cval)
                        mask = pd.Series(False, index=md.index)
                        if ctype == "keyword" and kws and flds:
                            for kw in kws:
                                for f in flds:
                                    fc = _resolve_col(md, f)
                                    if fc in md.columns:
                                        mask |= (md[fc].astype(str).str.lower()
                                                 .str.contains(kw.lower(), na=False))
                        else:
                            col = {"policy_domain": "policy_domain_standardized",
                                   "beneficiary": "beneficiary_primary_category"
                                   }.get(ctype, "category_main_label")
                            if col in md.columns and cval:
                                mask = md[col].str.contains(cval, case=False, na=False)
                        sub = md[mask]
                        sub = sub[~sub["bill_id"].astype(str).isin(seen)]
                        if sub.empty:
                            continue
                        if "y_prob" in sub.columns:
                            sub = sub.sort_values(["momentum", "y_prob"],
                                                  ascending=[True, False],
                                                  na_position="last")
                        lines = []
                        for _, r in sub.head(3).iterrows():
                            seen.add(str(r["bill_id"]))
                            prob_s = (f"{int(r['y_prob']*100)}% floor vote"
                                      if pd.notna(r.get("y_prob")) else "")
                            lines.append(
                                f"  - {r['bill_number']}: {r['title']}. {prob_s}. "
                                f"Momentum: {r.get('momentum','N/A')}. "
                                f"U of I: {str(r.get('potential_impact_raw',''))[:160]}"
                                + _annot_suffix(r['bill_id']))
                        if lines:
                            cat_sections.append(f"Category — {label}:\n" + "\n".join(lines))
                iqs = []
                for _pi in _cosmos_list("impact_prompt"):
                    _q = _pi.get("data", {}).get("question", "").strip()
                    if _q:
                        iqs.append(f"- How do these bills impact {_q}?")
                if not pinned_lines and not cat_sections:
                    return None
                parts = ["Write a concise weekly digest (3-5 short paragraphs) for "
                         "university leadership. Focus on bills moving toward a vote, "
                         "direct University of Illinois impacts, and policy areas "
                         "gaining momentum. Where category sections are given, note "
                         "what's active in each. If impact questions are provided, "
                         "weave answers in."]
                if pinned_lines:
                    parts.append("\nPinned bills (personal watchlist):\n"
                                 + "\n".join(pinned_lines))
                if cat_sections:
                    parts.append("\nCategory monitors:\n" + "\n\n".join(cat_sections))
                if iqs:
                    parts.append("\nAddress these questions using the bills above:\n"
                                 + "\n".join(iqs))
                n_cat = sum(s.count("  - ") for s in cat_sections)
                cap = (f"Covers {len(pinned_lines)} pinned bill(s) and {n_cat} "
                       f"category-matched bill(s) across {len(cat_sections)} monitor(s).")
                return ("\n\n".join(parts), cap)

            # ---- LENS 2: High-impact, organized (your investigations) -----------
            def _lens_high_impact():
                inv = _my_investigations()
                if inv.empty or "max_rating_num" not in inv.columns:
                    return None
                hi, lvl = inv[inv["max_rating_num"] >= 3], "High"
                if hi.empty:
                    hi, lvl = inv[inv["max_rating_num"] >= 2], "Moderate+"
                if hi.empty:
                    return None
                hi = _enrich_inv(hi).copy()
                hi["bill_id"] = hi["bill_id"].astype(str)

                # Impact is scored from MULTIPLE signals, not one:
                #  1) how many of the user's own prompts + role profile rate it high,
                #  2) how many teammates independently flagged it (overlap),
                #  3) the bill's general University of Illinois impact rating,
                #  4) floor-vote likelihood.
                _prompt_hits = {}
                try:
                    from impact_loader import load_impact_results as _lir_hi
                    _ir_hi = _lir_hi(_user_id)
                    if not _ir_hi.empty:
                        _hh = _ir_hi[_ir_hi["rating_label"].isin(
                            ["High Impact", "Moderate Impact"])]
                        _prompt_hits = _hh.groupby("bill_id")["prompt_id"].nunique().to_dict()
                except Exception:
                    pass
                _team_hits = {}
                try:
                    for _o in _compute_overlaps(_user_id, list(_sharing_roster)):
                        _team_hits[str(_o["bill_id"])] = len(_o.get("others", []))
                except Exception:
                    pass

                def _score(r):
                    _bid = str(r["bill_id"])
                    _yp = float(r["y_prob"]) if pd.notna(r.get("y_prob")) else 0.0
                    return (int(r.get("max_rating_num", 0) or 0) * 3
                            + int(_prompt_hits.get(_bid, 0)) * 2
                            + int(_team_hits.get(_bid, 0)) * 2
                            + int(r.get("impact_rating_score", 0) or 0)
                            + _yp)
                hi["_score"] = hi.apply(_score, axis=1)
                hi = hi.sort_values("_score", ascending=False)
                _pool = len(hi)
                hi = hi.head(30)
                lines = []
                for _, r in hi.iterrows():
                    _bid = str(r["bill_id"])
                    _pb = r.get("y_prob")
                    _ps = (f" ({int(float(_pb)*100)}% to floor vote)"
                           if pd.notna(_pb) else "")
                    _ttl = str(r.get("title", "") or r.get("headline", "") or "")[:120]
                    _sig = []
                    _np = int(_prompt_hits.get(_bid, 0))
                    if _np:
                        _sig.append(f"rated high on {_np} of your prompts/profile")
                    _nt = int(_team_hits.get(_bid, 0))
                    if _nt:
                        _sig.append(f"independently flagged by {_nt} teammate(s)")
                    _gi = int(r.get("impact_rating_score", 0) or 0)
                    if _gi >= 3:
                        _sig.append("high general U of I impact")
                    elif _gi == 2:
                        _sig.append("moderate general U of I impact")
                    _sigtxt = ("  [why high-impact: " + "; ".join(_sig) + "]"
                               if _sig else "")
                    lines.append(
                        f"- {r.get('bill_number','')}: {_ttl}{_ps}{_sigtxt}"
                        + (f" - {str(r.get('what_changes','') or '')[:150]}"
                           if r.get('what_changes') else "")
                        + _annot_suffix(_bid))
                prompt = ("Below are the bills that matter most to THIS user, ranked by "
                          "a composite of several signals: how many of the user's own "
                          "impact prompts and role profile rate them high, whether "
                          "teammates independently flagged them, the bill's general "
                          "University of Illinois impact, and floor-vote likelihood. "
                          "Each line lists the specific reasons it scored high. Organize "
                          "them by theme; for each, explain what's at stake for this "
                          "user and their stakeholders, leaning on those 'why "
                          "high-impact' signals to justify the ranking. End with the 3 "
                          "that most deserve attention and say why each earned the top "
                          "spots.\n\n"
                          f"Priority bills ({lvl}, showing {len(hi)} of {_pool}):\n"
                          + "\n".join(lines))
                return (prompt, f"Ranks {len(hi)} of {_pool} {lvl.lower()} bill(s) by a "
                                f"composite of your prompts, team overlap, general "
                                f"impact, and likelihood — not one flat sort.")

            # ---- LENS 3: Momentum & movers (constrained to what's relevant) -----
            def _lens_movers():
                md = _master()
                if md.empty:
                    return None
                d = md.copy()
                d["bill_id"] = d["bill_id"].astype(str)
                for c in ("impact_rating_score", "y_prob"):
                    if c not in d.columns:
                        d[c] = 0
                # The relevant universe: bills you track, bills your team shared, and
                # bills the AI flagged High for you. A generic "everything high-impact
                # and moving" list isn't useful — movers should be things tied to you.
                _pin_ids = {str(s.get("data", {}).get("bill_id", "")) for s in saved_b}
                _team_ids, _mine_ids = set(), set()
                try:
                    from user_profile import list_team_shared as _lts_m
                    _team_ids = {str(it.get("data", {}).get("bill_id", ""))
                                 for it in _lts_m(list(_sharing_roster), "bill")}
                except Exception:
                    pass
                _inv_m = _my_investigations()
                if not _inv_m.empty and "max_rating_num" in _inv_m.columns:
                    _mine_ids = set(_inv_m[_inv_m["max_rating_num"] >= 2]
                                    ["bill_id"].astype(str))
                _relevant = (_pin_ids | _team_ids | _mine_ids) - {""}

                def _why(bid):
                    _t = []
                    if bid in _pin_ids:
                        _t.append("you track it")
                    if bid in _team_ids:
                        _t.append("a teammate shared it")
                    if bid in _mine_ids:
                        _t.append("flagged high-impact for you")
                    return "; ".join(_t)

                if _relevant:
                    d = d[d["bill_id"].isin(_relevant)]
                else:
                    # Nothing personal yet — fall back to high U of I impact so the
                    # lens still produces something, but say so in the caption.
                    d = d[d["impact_rating_score"] >= 3]
                if d.empty:
                    return None
                if "momentum" in d.columns:
                    _mom = d["momentum"].astype(str).str.contains(
                        "gain|increas|rising|up", case=False, na=False)
                else:
                    _mom = pd.Series(False, index=d.index)
                _movers = d[_mom | (d["y_prob"] >= 0.70)]
                if _movers.empty:                    # no movers among relevant → show all relevant
                    _movers = d
                _movers = _movers.sort_values(
                    "y_prob", ascending=False, na_position="last").head(20)
                lines = []
                for _, r in _movers.iterrows():
                    prob_s = (f"{int(r['y_prob']*100)}% floor vote"
                              if pd.notna(r.get("y_prob")) else "")
                    _wtag = _why(str(r["bill_id"]))
                    lines.append(
                        f"- {r['bill_number']}: {r['title']}. {prob_s}. "
                        f"Momentum: {r.get('momentum','N/A')}. "
                        f"Last action: {r.get('last_action_date','')}. "
                        + (f"Why it's on your radar: {_wtag}. " if _wtag else "")
                        + f"U of I: {str(r.get('potential_impact_raw',''))[:150]}"
                        + _annot_suffix(r['bill_id']))
                prompt = ("These are bills relevant to THIS user (they track them, a "
                          "teammate shared them, or the AI flagged them high-impact for "
                          "them) that are gaining momentum or already likely to reach a "
                          "floor vote. For each, note why it's on their radar and what "
                          "the movement means for their work and stakeholders. Say what "
                          "to watch this week. Note: week-over-week likelihood deltas "
                          "aren't available, so focus on current momentum and standing.\n\n"
                          "Movers on your radar:\n" + "\n".join(lines))
                _cap = (f"Reads {len(_movers)} moving bill(s) tied to you — tracked, "
                        f"team-shared, or flagged for you."
                        if _relevant else
                        f"No personal bills yet — showing {len(_movers)} high-impact "
                        f"movers university-wide.")
                return (prompt, _cap)

            # ---- LENS 4: My tracked bills breakdown (pinned) --------------------
            def _lens_tracked():
                rows = _pinned_rows(_master())
                if rows.empty:
                    return None
                lines = []
                for _, r in rows.head(50).iterrows():
                    prob_s = (f"{int(r['y_prob']*100)}% floor vote"
                              if pd.notna(r.get("y_prob")) else "")
                    lines.append(
                        f"- {r['bill_number']}: {r['title']}. {prob_s}. "
                        f"Momentum: {r.get('momentum','N/A')}. "
                        f"Last action: {r.get('last_action_date','')}. "
                        f"Impact score: {r.get('impact_rating_score','')}. "
                        f"U of I: {str(r.get('potential_impact_raw',''))[:180]}"
                        + _annot_suffix(r['bill_id']))
                prompt = ("These are the bills this user is personally tracking. Give a "
                          "focused breakdown: what's most important right now, what has "
                          "advanced or changed, what's stalled, and what needs attention. "
                          "Rank the top few by urgency.\n\n"
                          "Tracked bills:\n" + "\n".join(lines))
                return (prompt, f"Breaks down your {len(rows)} tracked bill(s) by urgency.")

            # ---- LENS 5: Me & my stakeholders (profile-driven) ------------------
            def _lens_stakeholders():
                profs = _cosmos_list("user_profile")
                prof = (profs[0].get("data", {}) if profs else {})
                prompts = [p.get("data", {}).get("question", "").strip()
                           for p in _cosmos_list("impact_prompt")]
                prompts = [q for q in prompts if q]
                inv = _my_investigations()
                lines = []
                if not inv.empty and "max_rating_num" in inv.columns:
                    _si = _enrich_inv(inv[inv["max_rating_num"] >= 2])
                    if _si is None or _si.empty:
                        _si = _enrich_inv(inv)
                    _si, _ = _funnel(_si, prob_threshold=0.60, floor=10, cap=20)
                    for _, r in _si.iterrows():
                        _pb = r.get("y_prob")
                        _ps = (f" ({int(float(_pb)*100)}% to vote)"
                               if pd.notna(_pb) else "")
                        _ttl = str(r.get("title", "") or r.get("headline", "") or "")[:120]
                        lines.append(
                            f"- {r.get('bill_number','')}: {_ttl}{_ps}"
                            + (f" - affects: {str(r.get('who_it_affects','') or '')[:120]}"
                               if r.get('who_it_affects') else "")
                            + _annot_suffix(r.get('bill_id', '')))
                if not lines and _pinned_rows(_master()).empty:
                    return None
                who = []
                if prof.get("title"):
                    who.append(f"Role: {prof.get('title')}")
                if prof.get("stakeholders"):
                    who.append(f"Stakeholders: {prof.get('stakeholders')}")
                if prof.get("description"):
                    who.append(f"About: {str(prof.get('description'))[:400]}")
                prompt = ("Speak directly to THIS user about what in the current "
                          "legislation matters to them and the people they serve. Use "
                          "their role and stakeholders to filter - skip anything not "
                          "relevant to them. Be concrete about what they should personally "
                          "care about and why.\n\n"
                          + ("Who they are:\n" + "\n".join(who) + "\n\n" if who else "")
                          + ("Their interest questions: " + "; ".join(prompts) + "\n\n"
                             if prompts else "")
                          + ("Bills flagged for them:\n" + "\n".join(lines)
                             if lines else ""))
                return (prompt, "Filters the legislation to your role and stakeholders.")

            # ---- LENS 6: Team overlap trends (convergence) ----------------------
            def _lens_overlap():
                ovs = _compute_overlaps(_user_id, list(_sharing_roster))
                if not ovs:
                    return None
                md = _master()
                lines = []
                for o in ovs[:15]:
                    _t = ""
                    if (not md.empty
                            and o["bill_id"] in md["bill_id"].astype(str).values):
                        _mr = md[md["bill_id"].astype(str) == o["bill_id"]].iloc[0]
                        _t = f" {_mr.get('title','')}."
                    lines.append(
                        f"- {o['bill_number']}:{_t} Flagged High for you and "
                        f"{o['n']-1} teammate(s). {o['headline'][:120]}"
                        + _annot_suffix(o['bill_id']))
                prompt = ("These bills were independently flagged High-impact for this "
                          "user AND one or more teammates - the team is converging on them "
                          "without anyone coordinating. Identify the trend: what themes or "
                          "policy areas is the team collectively most exposed to, and what "
                          "does that convergence suggest they should prioritize "
                          "together?\n\n"
                          "Overlapping bills (most-shared first):\n" + "\n".join(lines))
                return (prompt, f"Finds the trend across {len(ovs)} bill(s) your "
                                f"team converges on.")

            # ---- LENS 7: Team shared-bills highlights ---------------------------
            def _lens_shared():
                try:
                    from user_profile import list_team_shared
                    shared = list_team_shared(list(_sharing_roster), "bill")
                except Exception:
                    shared = []
                if not shared:
                    return None
                lines = []
                for it in shared[:30]:
                    d = it.get("data", {})
                    owner = _display_name(it.get("_owner", ""))
                    lines.append(
                        f"- {d.get('bill_number','')}: "
                        f"{str(d.get('title','') or '')[:120]}. Tracked by {owner}."
                        + _annot_suffix(str(d.get('bill_id', ''))))
                prompt = ("Below are bills the team is tracking and has shared with each "
                          "other, including teammates' own assessments. Produce AI "
                          "highlights: the most important shared priorities, where "
                          "teammates agree or diverge in their reads, and the single thing "
                          "the team should align on next.\n\n"
                          "Shared bills:\n" + "\n".join(lines))
                return (prompt, f"Highlights {len(shared)} bill(s) your team is "
                                f"tracking together.")

            _LENSES = [
                # Audit bug 7: the second tuple element is prepended to the button label,
                # so these were part of the accessible name. Emptied rather
                # than removed so the tuple shape is unchanged.
                ("leadership", "", "Weekly leadership digest",
                 "The polished 3-5 paragraph summary - pinned bills, categories, and "
                 "your questions, for leadership.", _lens_leadership),
                ("high_impact", "", "High-impact, organized",
                 "Ranked by a composite — how many of your prompts/profile rate it "
                 "high, team overlap, general impact, and likelihood — with why-tags.",
                 _lens_high_impact),
                ("movers", "", "Momentum & movers",
                 "Bills you track, your team shared, or the AI flagged for you that "
                 "are gaining momentum or near a floor vote.", _lens_movers),
                ("tracked", "", "My tracked bills",
                 "A focused read of just the bills you pinned - what's urgent, what "
                 "moved.", _lens_tracked),
                ("stakeholders", "", "Me & my stakeholders",
                 "Filters the legislation to your role and the people you serve.",
                 _lens_stakeholders),
                ("overlap", "", "Team overlap trends",
                 "The themes your whole team is independently converging on.",
                 _lens_overlap),
                ("shared", "", "Team shared highlights",
                 "AI highlights from the bills your team is tracking together.",
                 _lens_shared),
            ]
            _LENS_THEME = {"overlap": _TEAM_ORANGE, "shared": _TEAM_GREEN}
            _DEFAULT_THEME = ("#13294B", "#F1F5F9", "#13294B")

            st.caption("Pick a lens - each generates a focused AI read of a slice of "
                       "your data, tailored to your profile. Generate as many as you "
                       "like; results stay until you regenerate them.")

            def _lens_export_md(_title, _body):
                _clean = re.sub(r"<[^>]+>", "", str(_body or ""))
                return (f"# {_title}\n\n_University of Illinois IGPA — legislative "
                        f"digest_\n\n{_clean}\n").encode("utf-8")

            for _i, (_lk, _le, _ll, _lb, _lfn) in enumerate(_LENSES):
                if st.button(f"{_le}  {_ll}", key=f"lensbtn_{_lk}",
                             use_container_width=True):
                    st.session_state["_digest_active_lens"] = _lk
                    st.session_state[f"_digest_regen_{_lk}"] = True
                st.caption(_lb)

                # Render THIS lens's result inline, directly under its button.
                if st.session_state.get("_digest_active_lens") == _lk:
                    _ck = f"_digest_out_{_lk}"
                    if (st.session_state.pop(f"_digest_regen_{_lk}", False)
                            or _ck not in st.session_state):
                        _built, _build_err = None, False
                        try:
                            _built = _lfn()
                        except Exception as _e:
                            _build_err = True
                            st.session_state[_ck] = (None, f"Could not build this lens: {_e}")
                        if not _build_err:
                            if _built is None:
                                st.session_state[_ck] = (
                                    None, "No data for this lens yet - track or flag "
                                    "some bills, then try again.")
                            else:
                                _pr, _cap = _built
                                _pr_full = ((_profile_ctx + "\n\n" + _pr)
                                            if _profile_ctx else _pr)
                                try:
                                    with st.spinner(f"Generating - {_ll}..."):
                                        _txt = ai_briefing.generate(
                                            _pr_full, system=_SYS_DIGEST, max_tokens=1100)
                                    st.session_state[_ck] = (_txt, _cap)
                                except RuntimeError as _e:
                                    st.session_state[_ck] = (None, str(_e))
                                except Exception as _e:
                                    st.session_state[_ck] = (None, f"Generation failed: {_e}")
                    _txt, _cap = st.session_state.get(_ck, (None, ""))
                    _accent, _bg, _fg = _LENS_THEME.get(_lk, _DEFAULT_THEME)
                    if _txt:
                        st.markdown(
                            f"<div style='background:{_bg};border:1px solid {_accent}33;"
                            f"border-left:5px solid {_accent};border-radius:12px;"
                            f"padding:18px 20px;margin-top:6px'>"
                            f"<div style='font-size:13px;font-weight:800;color:{_fg};"
                            f"margin-bottom:8px'>{_le}  {_ll}</div>"
                            f"<div style='font-size:13px;color:#0F172A;line-height:1.7'>"
                            f"{_txt}</div></div>",
                            unsafe_allow_html=True)
                        st.caption(_cap)
                        _ex1, _ex2, _ex3 = st.columns([1.2, 1.4, 3])
                        with _ex1:
                            if st.button("Regenerate", key=f"regen_{_lk}",
                                         use_container_width=True):
                                st.session_state[f"_digest_regen_{_lk}"] = True
                                st.rerun()
                        with _ex2:
                            st.download_button(
                                "Download (.md)", data=_lens_export_md(_ll, _txt),
                                file_name=f"digest_{_lk}.md", mime="text/markdown",
                                key=f"dl_{_lk}", use_container_width=True)
                        with st.expander("Copy this summary"):
                            st.code(re.sub(r"<[^>]+>", "", _txt), language=None)
                    elif _cap:
                        st.info(_cap)
                st.markdown("<div style='height:2px'></div>", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB — MY BRIEFING  (personalized deep-dive investigations of flagged bills)
# ══════════════════════════════════════════════════════════════════════════════

with tab_briefing:
    st.markdown('<h2 class="sect-head">My Briefing</h2>', unsafe_allow_html=True)
    st.caption(
        "Every bill your **impact prompts** and **role profile** flagged as "
        "important to you — each with the impact score your prompts gave it and a "
        "deep-dive analysis. This is the response *to your own prompts*, so what "
        "shows up here is driven by what you asked to watch in **My Profile**. "
        "Filter and sort below; open any bill for the full analysis. For just the "
        "highlights, see **My Digest**."
    )
    st.info(
        "**AI-generated analysis — not an institutional position.** These "
        "briefings are produced automatically from bill text and your role "
        "profile. They may be incomplete or wrong, do not represent the position "
        "of the University of Illinois System, and should be verified against the "
        "bill before you act on them.",
        icon=None,
    )
    if not _user_authed:
        st.warning("Sign in with your University of Illinois email to see your briefing.")
    else:
        _inv = None
        try:
            from investigation_loader import load_investigations
            _inv = load_investigations(_user_id)
        except Exception:
            st.info("Briefing isn't available right now — check back after the next run.")
        if _inv is None or _inv.empty:
            st.info(
                "No briefing yet. Add impact prompts and your role profile in "
                "My Profile — the next scheduled run investigates the bills that "
                "matter to you and they'll appear here."
            )
        else:
            import json as _json

            BRF_PAGE_SIZE = 10
            _RATING_LABEL = {3: "High Impact", 2: "Moderate Impact",
                             1: "Low Impact", 0: "No Impact"}

            # prompt_id -> human label, so "flagged by" reads as your prompt names.
            _pid_to_label: dict[str, str] = {}
            try:
                for _pr in _cosmos_list("impact_prompt"):
                    _pid_to_label[str(_pr.get("id", ""))] = _pr.get("label", "(unnamed prompt)")
                for _pp in _cosmos_list("user_profile"):
                    _pid_to_label[str(_pp.get("id", ""))] = "My Role Profile"
            except Exception:
                pass

            def _flag_labels(raw_json) -> list[str]:
                try:
                    ids = _json.loads(raw_json) if raw_json else []
                except Exception:
                    ids = []
                if not isinstance(ids, list):
                    ids = []
                return [_pid_to_label.get(str(i), "My Role Profile"
                                          if str(i).lower() == "profile" else str(i)[:12])
                        for i in ids]

            _inv = _inv.copy()
            _inv["_flags"]  = _inv["flagged_by"].map(_flag_labels)
            _inv["_rating"] = _inv["max_rating_num"].map(
                lambda n: _RATING_LABEL.get(int(n), "No Impact"))

            # Bring bill-level fields (floor-vote %, last action, title, summary,
            # category, stage) onto the investigations so the card, filters, and
            # sorts can use them — investigations alone don't carry these.
            _master_brf = load_master(CURRENT_SESSION)
            _inv["bill_id"] = _inv["bill_id"].astype(str)
            _mcols = ["bill_id", "title", "llama_summary_raw", "y_prob",
                      "last_action_date", "category_main_label", "momentum",
                      "impact_rating_score"]
            _mkeep = [c for c in _mcols if c in _master_brf.columns]
            if _mkeep and not _master_brf.empty:
                _mm = _master_brf[_mkeep].copy()
                _mm["bill_id"] = _mm["bill_id"].astype(str)
                _drop = [c for c in _mm.columns if c != "bill_id" and c in _inv.columns]
                if _drop:
                    _inv = _inv.drop(columns=_drop)
                _inv = _inv.merge(_mm, on="bill_id", how="left")
            # Stage / status from the bronze manifest (Introduced..Vetoed + terminal)
            _man_brf = load_manifest(CURRENT_SESSION)
            if not _man_brf.empty:
                _drop2 = [c for c in _man_brf.columns
                          if c != "bill_id" and c in _inv.columns]
                if _drop2:
                    _inv = _inv.drop(columns=_drop2)
                _inv = _inv.merge(_man_brf, on="bill_id", how="left")
            for _c, _dv in (("status_desc", ""), ("is_terminal", False),
                            ("y_prob", pd.NA), ("last_action_date", ""),
                            ("title", ""), ("llama_summary_raw", ""),
                            ("category_main_label", "")):
                if _c not in _inv.columns:
                    _inv[_c] = _dv
            _inv["status_desc"] = _inv["status_desc"].fillna("").astype(str)

            _all_flags = sorted({f for row in _inv["_flags"] for f in row})
            _stages_present = [s for s in BRF_STAGE_LADDER
                               if s.lower() in set(_inv["status_desc"].str.lower())]

            # ── Filters ───────────────────────────────────────────────────────
            st.caption(f"{len(_inv):,} bill(s) investigated for you.")
            _fc1, _fc2, _fc3 = st.columns(3)
            with _fc1:
                _sel_levels = st.multiselect(
                    "Impact level", ["High Impact", "Moderate Impact",
                                     "Low Impact", "No Impact"],
                    default=["High Impact", "Moderate Impact"],
                    key="brf_levels",
                    help="Highest rating this bill received across your prompts.")
            with _fc2:
                _sel_flags = st.multiselect(
                    "Flagged by", _all_flags, key="brf_flags",
                    help="Which of your impact prompts (or your role profile) "
                         "surfaced this bill. Leave empty for all.")
            with _fc3:
                _sort_by = st.selectbox(
                    "Sort by",
                    ["Likely to pass", "Recent action", "Highest impact",
                     "Earliest stage"],
                    key="brf_sort",
                    help="Default surfaces the bills most likely to reach a floor "
                         "vote, newest action first.")

            _sc1, _sc2 = st.columns([2, 3])
            with _sc1:
                _sel_stages = st.multiselect(
                    "Stage", _stages_present,
                    default=[s for s in _stages_present
                             if s not in ("Passed", "Vetoed")],
                    key="brf_stages",
                    help="LegiScan status. Passed/Vetoed are off by default — "
                         "those outcomes are already decided.")
            with _sc2:
                _prob_pct = (_inv["y_prob"].dropna().astype(float) * 100)
                # Bug 27: always drawn, so its value is never discarded.
                if True:
                    _lo, _hi = st.slider(
                        "Floor-vote likelihood (%)", 0, 100, (0, 100),
                        key="brf_prob_range",
                        help="Filter to a passage-likelihood band — e.g. 50–80% to "
                             "skip the long shots and the near-certain.")
                    import numpy as _np
                    _counts, _edges = _np.histogram(_prob_pct, bins=20, range=(0, 100))
                    _hist_df = pd.DataFrame(
                        {"likelihood %": [int(_edges[i]) for i in range(len(_counts))],
                         "bills": _counts}).set_index("likelihood %")
                    if not _prob_pct.empty:
                        st.bar_chart(_hist_df, height=110)
                        sr_chart_table("Briefing results distribution",
                                       band_table(_counts, _edges, "Floor-vote likelihood"))

            _q = st.text_input("Filter by bill number or headline",
                               key="brf_q", max_chars=80,
                               placeholder="e.g. HB1234  or  tuition")

            _view = _inv
            if _sel_levels:
                _view = _view[_view["_rating"].isin(_sel_levels)]
            if _sel_flags:
                _view = _view[_view["_flags"].map(
                    lambda fs: any(f in _sel_flags for f in fs))]
            if _sel_stages:
                _view = _view[_view["status_desc"].str.lower().isin(
                    {s.lower() for s in _sel_stages})]
            if (_lo, _hi) != (0, 100):
                _pp = _view["y_prob"].astype(float) * 100
                _view = _view[(_pp >= _lo) & (_pp <= _hi)]
            if _q.strip():
                _needle = _q.strip().lower()
                _view = _view[
                    _view["bill_number"].astype(str).str.lower().str.contains(_needle, na=False)
                    | _view["headline"].astype(str).str.lower().str.contains(_needle, na=False)]

            # ── Sort ──────────────────────────────────────────────────────────
            if _sort_by == "Likely to pass":
                _view = _view.assign(_ld=pd.to_datetime(
                    _view["last_action_date"], errors="coerce"))
                _view = _view.sort_values(["y_prob", "_ld"], ascending=[False, False],
                                          na_position="last").drop(columns=["_ld"])
            elif _sort_by == "Recent action":
                _view = _view.assign(_ld=pd.to_datetime(
                    _view["last_action_date"], errors="coerce"))
                _view = _view.sort_values(["_ld", "y_prob"], ascending=[False, False],
                                          na_position="last").drop(columns=["_ld"])
            elif _sort_by == "Earliest stage":
                _view = _view.assign(_sr=_view["status_desc"].astype(str).str.lower()
                                     .map(BRF_STAGE_RANK).fillna(99))
                _view = _view.sort_values(["_sr", "y_prob"], ascending=[True, False],
                                          na_position="last").drop(columns=["_sr"])
            else:  # Highest impact
                _view = _view.sort_values(["max_rating_num", "y_prob"],
                                          ascending=[False, False], na_position="last")

            _total = len(_view)
            if _total == 0:
                st.info("No investigated bills match these filters.")
            else:
                # ── Pagination ────────────────────────────────────────────────
                # CRITICAL: render only a slice. Streamlit ships an expander's full
                # contents to the browser even when collapsed, so rendering all
                # 1,000+ investigations produced a ForwardMsg large enough to kill
                # the websocket — the client then reconnected, re-ran the script,
                # and rebuilt the same payload, looping every few seconds.
                _pk = "brf_shown"
                if _pk not in st.session_state:
                    st.session_state[_pk] = BRF_PAGE_SIZE
                _shown = min(st.session_state[_pk], _total)
                st.caption(f"Showing **{_shown}** of **{_total:,}** — "
                           f"{_sort_by.lower()} first.")

                _RATING_PILL = {"High Impact": "#DC2626", "Moderate Impact": "#A2570F",
                                "Low Impact": "#0E7490", "No Impact": "#454F5A"}

                # Cross-team share targets, built ONCE (not per card): every team the
                # user is in, each team's roster, and the distinct people across all
                # teams (for direct person-to-person shares that skip a team lens).
                _share_teams = []       # [(team_id, team_name, {email: name}), ...]
                _share_people = {}      # email -> name, deduped across all teams
                try:
                    from team_service import active_teammates as _at_share
                    for _tm in (_active_memberships or []):
                        _tid = _tm.get("team_id")
                        _tnm = _tm.get("name", "team")
                        _ros = {e: _display_name(e) for e in _at_share(_user_id, _tid)}
                        if _ros:
                            _share_teams.append((_tid, _tnm, _ros))
                            for _e, _nm in _ros.items():
                                _share_people.setdefault(_e, _nm)
                except Exception:
                    pass

                for _r in _view.head(_shown).itertuples(index=False):
                    _rating = getattr(_r, "_rating", "")
                    _flags  = getattr(_r, "_flags", []) or []
                    _bn    = str(getattr(_r, "bill_number", ""))
                    _bbid  = str(getattr(_r, "bill_id", "") or "")
                    _title = str(getattr(_r, "title", "") or "")
                    _summ_full = str(getattr(_r, "llama_summary_raw", "") or "")
                    _summ_short = ((_summ_full[:220] + "…")
                                   if len(_summ_full) > 220 else _summ_full)
                    _prob  = getattr(_r, "y_prob", None)
                    _ld    = str(getattr(_r, "last_action_date", "") or "")
                    _stage = str(getattr(_r, "status_desc", "") or "")
                    _cat   = str(getattr(_r, "category_main_label", "") or "")
                    _sid_b = int(getattr(_r, "session_id", CURRENT_SESSION)
                                 or CURRENT_SESSION)
                    _rc = _RATING_PILL.get(_rating, "#454F5A")
                    _itype  = str(getattr(_r, "impact_type", "") or "")
                    _direct = str(getattr(_r, "directness", "") or "")
                    _meta = []
                    if _stage:
                        _meta.append(f"{_stage}")
                    if pd.notna(_prob):
                        try:
                            _meta.append(f"{int(float(_prob) * 100)}% to floor vote")
                        except Exception:
                            pass
                    if _ld:
                        _meta.append(f"{_ld}")
                    if _cat:
                        _meta.append(f"{_cat}")
                    if _itype or _direct:
                        _meta.append(f"{_itype}"
                                     f"{' · ' if _itype and _direct else ''}{_direct}")
                    _ilga = _ilga_link_html(_bn, _sid_b)
                    # Visible card — full facts up front (full summary + category tag),
                    # with Pin right here on the main card, not buried in the expander.
                    _cardcol, _pincol = st.columns([11, 1])
                    with _cardcol:
                        st.markdown(
                            f"<div style='margin:10px 0 0;padding:12px 16px;"
                            f"border:1px solid #E2E8F0;border-left:4px solid {_rc};"
                            f"border-radius:10px;background:#fff'>"
                            f"<div style='display:flex;gap:8px;align-items:baseline;"
                            f"flex-wrap:wrap'><span style='font-size:13px;font-weight:800;"
                            f"color:#0F172A'>{_bn}</span>"
                            f"<span style='font-size:11px;font-weight:700;color:{_rc}'>"
                            f"{_rating}</span></div>"
                            f"<div style='font-size:13px;font-weight:600;color:#0F172A;"
                            f"margin-top:3px'>{_title}</div>"
                            + (f"<div style='font-size:12px;color:#475569;line-height:1.55;"
                               f"margin-top:4px'>{_summ_full}</div>" if _summ_full else "")
                            + (f"<div style='font-size:11px;color:#454F5A;margin-top:6px'>"
                               f"{' · '.join(_meta)}</div>" if _meta else "")
                            + (f"<div style='margin:7px 0 2px'>{_ilga}</div>"
                               if _ilga else "")
                            + "</div>",
                            unsafe_allow_html=True)
                    with _pincol:
                        st.markdown("<div style='height:12px'></div>",
                                    unsafe_allow_html=True)
                        if _user_authed and _bbid:
                            _bpinned = {str(b.get("data", {}).get("bill_id", ""))
                                        for b in _cosmos_list("bill")}
                            if _bbid in _bpinned:
                                st.caption("Pinned")
                            elif len(_bpinned) >= MAX_TRACKED_BILLS:
                                st.caption("List full")
                            elif st.button("Pin", key=f"brfpin_{_bbid}",
                                           help="Pin to My Bills"):
                                if _cosmos_save_bill(
                                        _bbid, _bn,
                                        str(getattr(_r, "headline", "") or _bn),
                                        _sid_b, {}, notify=True):
                                    st.success("Pinned")
                                    st.rerun()
                    # Your impact-prompt ratings for this bill: category + response.
                    # The prompt you saved rated this bill; show the label + rating +
                    # the explanation (the "response"), on every card.
                    try:
                        from impact_loader import bill_impact_rows as _bir_brf
                        _pir = _bir_brf(_user_id, _bbid)
                        if _pir is not None and not _pir.empty:
                            _PRC = {"High Impact": "#DC2626", "Moderate Impact": "#A2570F",
                                    "Low Impact": "#0E7490", "No Impact": "#454F5A"}
                            _pp = []
                            for _, _prow in _pir.iterrows():
                                _pcl = _PRC.get(str(_prow.get("rating_label", "")), "#454F5A")
                                _pp.append(
                                    f"<div style='margin:3px 0 0 8px;font-size:12px;"
                                    f"line-height:1.45'>"
                                    f"<span style='font-weight:600;color:#0F172A'>"
                                    f"{_prow.get('prompt_label','')}</span> · "
                                    f"<span style='color:{_pcl};font-weight:600'>"
                                    f"{_prow.get('rating_label','')}</span>"
                                    f"<span style='color:#475569'> — "
                                    f"{_prow.get('explanation','')}</span></div>")
                            st.markdown(
                                "<div style='margin:3px 0 2px'>"
                                "<span style='font-size:11px;font-weight:700;"
                                "color:#475569'>Your prompt ratings</span>"
                                + "".join(_pp) + "</div>", unsafe_allow_html=True)
                    except Exception:
                        pass
                    with st.expander(f"Full analysis — {_bn}", expanded=False):
                        if _flags:
                            st.caption("Flagged by: " + ", ".join(_flags))
                        _wc = str(getattr(_r, "what_changes", "") or "")
                        if _wc:
                            st.markdown(f"**What changes for you**  \n{_wc}")
                        _who = str(getattr(_r, "who_it_affects", "") or "")
                        if _who:
                            st.markdown(f"**Who it affects**  \n{_who}")
                        try:
                            _acts = _json.loads(getattr(_r, "recommended_actions", "") or "[]")
                        except Exception:
                            _acts = []
                        if _acts:
                            st.markdown("**Recommended actions**")
                            for _a in _acts:
                                st.markdown(f"- {_a}")
                        _swot_raw = getattr(_r, "swot", "") or ""
                        if _swot_raw:
                            try:
                                _sw = _json.loads(_swot_raw)
                            except Exception:
                                _sw = None
                            if isinstance(_sw, dict):
                                st.markdown("**SWOT**")
                                for _k in ("strengths", "weaknesses",
                                           "opportunities", "threats"):
                                    if _sw.get(_k):
                                        st.markdown(f"- *{_k.title()}:* {_sw[_k]}")
                        _tl   = str(getattr(_r, "timeline", "") or "unknown")
                        _conf = str(getattr(_r, "confidence", "") or "n/a")
                        _cr   = str(getattr(_r, "confidence_reason", "") or "")
                        st.markdown(
                            f"<span style='font-size:12px;color:#454F5A'>"
                            f"Timeline: {_tl} · Confidence: {_conf}"
                            f"{(' — ' + _cr) if _cr else ''}</span>",
                            unsafe_allow_html=True)

                        # ── Share this AI briefing — across ALL your teams ────────
                        # Pick a team and send to everyone or one member (routed
                        # through that team's lens), OR send directly to a person in
                        # any of your teams (no team lens). Attribution is stored so
                        # the recipient sees exactly how it reached them.
                        if _user_authed and _bbid and (_share_teams or _share_people):
                            st.markdown("<div style='height:6px'></div>",
                                        unsafe_allow_html=True)
                            st.caption("Share this briefing — teammates see it's "
                                       "AI-generated, not your personal note.")
                            _via_opts = (["Direct to a person"]
                                         + [_t[1] for _t in _share_teams])
                            _sv1, _sv2, _sv3 = st.columns([2, 2, 1])
                            with _sv1:
                                _via = st.selectbox(
                                    "Share via", _via_opts, key=f"shvia_{_bbid}",
                                    help="Send through a team (everyone or one "
                                         "member), or straight to a person in any "
                                         "of your teams.")
                            if _via == "Direct to a person":
                                _rec_opts = list(_share_people.keys())
                                _team_ctx, _ros_sel = "", {}
                                def _fmt_rec(e, _p=_share_people):
                                    return _p.get(e, e) + "  · directly"
                            else:
                                _tsel = next((_t for _t in _share_teams
                                              if _t[1] == _via), None)
                                _team_ctx = _tsel[1] if _tsel else ""
                                _ros_sel = _tsel[2] if _tsel else {}
                                _rec_opts = ["__ALL__"] + list(_ros_sel.keys())
                                def _fmt_rec(e, _r=_ros_sel, _tc=_team_ctx):
                                    return (f"Everyone in {_tc}" if e == "__ALL__"
                                            else _r.get(e, e))
                            with _sv2:
                                _recp = st.selectbox(
                                    "Recipient", _rec_opts,
                                    key=f"shrec_{_bbid}_{_via}",
                                    format_func=_fmt_rec)
                            with _sv3:
                                st.markdown("<div style='height:2px'></div>",
                                            unsafe_allow_html=True)
                                if st.button("Share", key=f"shbrf_go_{_bbid}",
                                             use_container_width=True):
                                    try:
                                        _acts_share = _json.loads(
                                            getattr(_r, "recommended_actions", "") or "[]")
                                    except Exception:
                                        _acts_share = []
                                    _brf = {
                                        "headline": str(getattr(_r, "headline", "") or ""),
                                        "rating": _rating,
                                        "what_changes": str(getattr(_r, "what_changes", "") or ""),
                                        "who_it_affects": str(getattr(_r, "who_it_affects", "") or ""),
                                        "recommended_actions": _acts_share,
                                        "timeline": str(getattr(_r, "timeline", "") or ""),
                                        "confidence": str(getattr(_r, "confidence", "") or ""),
                                    }
                                    _bill = {
                                        "bill_id": _bbid,
                                        "bill_number": str(getattr(_r, "bill_number", "")),
                                        "title": (_title or str(getattr(_r, "headline", "")
                                                  or getattr(_r, "bill_number", ""))),
                                        "session_id": int(getattr(_r, "session_id",
                                                                   CURRENT_SESSION)),
                                    }
                                    if _via == "Direct to a person":
                                        _recips, _scope, _vt = [_recp], "individual", ""
                                    elif _recp == "__ALL__":
                                        _recips = list(_ros_sel.keys())
                                        _scope, _vt = "team", _team_ctx
                                    else:
                                        _recips, _scope, _vt = [_recp], "individual", _team_ctx
                                    try:
                                        from user_profile import share_briefing
                                        _n = share_briefing(_user_id, _recips, _scope,
                                                             _bill, _brf, via_team=_vt)
                                        if _n:
                                            _dest = (f"everyone in {_team_ctx}"
                                                     if _scope == "team"
                                                     else _share_people.get(_recp,
                                                          _ros_sel.get(_recp, "them")))
                                            st.success(f"Shared with {_dest} "
                                                       f"({_n} recipient(s)).")
                                        else:
                                            st.warning("Nothing shared.")
                                    except Exception as e:
                                        st.error(f"Share failed: {e}")

                if _shown < _total:
                    if st.button(f"Show {min(BRF_PAGE_SIZE, _total - _shown)} more "
                                 f"({_total - _shown:,} remaining)", key="brf_more"):
                        st.session_state[_pk] = _shown + BRF_PAGE_SIZE
                        st.rerun()
                elif _total > BRF_PAGE_SIZE:
                    if st.button("Collapse back to top 10", key="brf_less"):
                        st.session_state[_pk] = BRF_PAGE_SIZE
                        st.rerun()


with tab_team:
    st.markdown('<h2 class="sect-head">My Team</h2>', unsafe_allow_html=True)
    if not _user_authed:
        st.warning("Sign in with your University of Illinois email to see your team.")
    elif not _active_team:
        # No active membership. Point at Profile; surface pending invites if any.
        if _pending_invites:
            st.info(f"You have {len(_pending_invites)} pending team invite(s). "
                    "Accept them in My Profile → Team to get started.")
        else:
            st.info("You're not on a team yet. Create a team or accept an invite "
                    "in My Profile → Team, then invite colleagues by email.")
    else:
        _me = _user
        # ── Team switcher (only shown when you're active in more than one) ───────
        _active_memberships_ui = [t for t in _my_teams if t.get("status") == "active"]
        if len(_active_memberships_ui) > 1:
            _opts = [t["team_id"] for t in _active_memberships_ui]
            _label_by_id = {t["team_id"]: t["name"] for t in _active_memberships_ui}
            _cur = (_active_team_id if _active_team_id in _opts else _opts[0])
            _pick = st.selectbox(
                "Viewing team", _opts, index=_opts.index(_cur),
                format_func=lambda i: _label_by_id.get(i, i),
                key="team_switcher",
                help="You're on more than one team. Everything below — overlap, "
                     "shared bills, the AI overview — is for the team you pick here.")
            if _pick != _active_team_id:
                st.session_state["active_team_id"] = _pick
                st.rerun()
        _team = _active_team.get("name", "") or "your team"
        _is_owner = (_active_team.get("role") == "owner")
        st.caption(f"Team: **{_team}**"
                   + ("  ·  you're the owner" if _is_owner else "")
                   + ". You see shared bills and convergence from active "
                   "teammates. Manage membership in My Profile → Team.")
        if True:
            try:
                from user_profile import list_team_shared
                # Rosters come from the global active-team resolver:
                #   _active_roster   → all active teammates (send-to)
                #   _sharing_roster  → those exposing their data (see/overlap)
                #   _team_name_by_email → email → display name (incl. "you")
                _sharers = list(_sharing_roster)
                _name_by_email = dict(_team_name_by_email)

                def _disp(_email) -> str:
                    """Consistent teammate label — the display name ('Pramod') when
                    we have it, falling back to the email prefix. Used everywhere a
                    teammate is named so the overlap, briefing, and bill sections
                    don't disagree ('Pramod' vs 'pro')."""
                    _e = str(_email or "")
                    return _name_by_email.get(_e, _e.split("@")[0] or "a teammate")

                # ── Team overlap (algorithmic convergence) ──────────────────────
                # Bills the AI independently flagged as impactful for YOU and for
                # sharing teammates — through each person's own prompts and role
                # profile, not because anyone pinned them. Computed live from the
                # investigations parquet (cached 30 min), so it refreshes every
                # server run with no pipeline change. Consent basis: only active
                # teammates with sharing enabled are in _sharing_roster, and we
                # surface only that a bill converged — never their prompt or profile.
                _overlaps = _compute_overlaps(_user_id, _sharers)

                # ── 🧭 AI team overview (on-demand) ──────────────────────────────
                # A short synthesis of what's converging across the team: the
                # overlap above, what's been shared/flagged for you, and the read
                # your teammates left on bills you both track. On-demand for now
                # (one gpt-4o-mini call, cached for this session); the server-side
                # path to make it automatic is noted in the roadmap.
                _ov_cache_key = f"_team_overview::{_active_team_id}"
                st.markdown('<h3 class="sect-head">Team overview</h3>',
                            unsafe_allow_html=True)
                _ov_col1, _ov_col2 = st.columns([4, 1])
                with _ov_col2:
                    _ov_go = st.button("Generate", key="gen_team_overview",
                                       use_container_width=True)
                with _ov_col1:
                    st.caption("An AI read of your team, in three parts — where you "
                               "overlap, the briefings shared with you, and the bills "
                               "shared with you — with teammates' annotations quoted and "
                               "attributed. Regenerate any time.")
                if _ov_go:
                    with st.spinner("Reading the room…"):
                        try:
                            import ai_briefing
                            from user_profile import (list_shared_briefings as _lsb,
                                                       list_team_shared as _lts)
                            _mo = load_master(CURRENT_SESSION)

                            def _title_for(_bid, _fallback=""):
                                _bid = str(_bid)
                                if (not _mo.empty
                                        and _bid in _mo["bill_id"].astype(str).values):
                                    _t = str(_mo[_mo["bill_id"].astype(str) == _bid]
                                             .iloc[0].get("title", "") or "")
                                    return _t or _fallback
                                return _fallback

                            _sections = []

                            # 1) Where your team overlaps (bill number + title)
                            _ov_lines = []
                            for _o in (_overlaps or [])[:8]:
                                _nm = ", ".join(_name_by_email.get(e, e.split("@")[0])
                                                for e in _o["others"])
                                _ttl = _title_for(_o["bill_id"], _o["headline"][:80])
                                _ov_lines.append(
                                    f"- {_o['bill_number']} — {_ttl} "
                                    f"({_o['n']}-way, incl. you + {_nm})")
                            if _ov_lines:
                                _sections.append(
                                    "SECTION 1 — WHERE YOUR TEAM OVERLAPS (bills the AI "
                                    "independently flagged High for you AND teammates):\n"
                                    + "\n".join(_ov_lines))

                            # 2) Briefings shared with you (this team), with content
                            _shb2 = _lsb(_user_id)
                            _te2 = {e.lower() for e in _active_roster}
                            _shb2 = [s for s in _shb2
                                     if str(s.get("data", {}).get("shared_by", "")).lower()
                                     in _te2]
                            _brf_lines = []
                            for _s in _shb2[:8]:
                                _sd2 = _s.get("data", {})
                                _bf2 = _sd2.get("briefing", {}) or {}
                                _who2 = _disp(_sd2.get("shared_by", ""))
                                _find = str(_bf2.get("what_changes", "")
                                            or _bf2.get("headline", "") or "")[:220]
                                _ttl2 = (_sd2.get("title", "")
                                         or _title_for(_sd2.get("bill_id", "")))
                                _brf_lines.append(
                                    f"- {_sd2.get('bill_number','')} — {_ttl2} "
                                    f"(shared by {_who2}): {_find}")
                            if _brf_lines:
                                _sections.append(
                                    "SECTION 2 — BRIEFINGS SHARED WITH YOU (deep AI "
                                    "investigations teammates sent you; name who sent each):\n"
                                    + "\n".join(_brf_lines))

                            # 3) Bills shared with you + attributed annotations
                            _bill_lines = []
                            try:
                                for _it in (_lts(_sharers, "bill") if _sharers else []):
                                    _d3 = _it.get("data", {})
                                    _a3 = _d3.get("annotation", {}) or {}
                                    _who3 = _disp(_it.get("_owner", ""))
                                    _ttl3 = (_d3.get("title", "")
                                             or _title_for(_d3.get("bill_id", "")))
                                    _as3 = str(_a3.get("assessment", "") or "").strip()
                                    _ln = (f"- {_d3.get('bill_number','')} — {_ttl3} "
                                           f"(tracked by {_who3})")
                                    if _as3:
                                        _ln += (f" — {_who3}'s note "
                                                f"({_a3.get('direction','unclear')}): "
                                                f"{_as3[:180]}")
                                    _bill_lines.append(_ln)
                            except Exception:
                                pass
                            if _bill_lines:
                                _sections.append(
                                    "SECTION 3 — BILLS SHARED WITH YOU (bills teammates "
                                    "track and shared, with their annotations and who "
                                    "wrote them):\n" + "\n".join(_bill_lines[:12]))

                            if not _sections:
                                st.session_state[_ov_cache_key] = (
                                    "No convergence to summarize yet. Once you and "
                                    "your teammates have impact prompts and role "
                                    "profiles scored — and start sharing briefings and "
                                    "bills — overlaps and shared reads show up here.")
                            else:
                                _prompt = (
                                    "You are a legislative analyst briefing one member of "
                                    f"a University of Illinois team (team: {_team}). Write "
                                    "in plain prose, addressing the reader as 'you'. "
                                    "Produce EXACTLY THREE clearly separated sections, in "
                                    "this order, each under its own short bold header: "
                                    "'Where your team overlaps', 'Briefings shared with "
                                    "you', and 'Bills shared with you'. Be concrete and "
                                    "specific to the bills named. CRITICAL RULES: (1) every "
                                    "time you mention a bill, give BOTH its number AND its "
                                    "title — never a bare bill number. (2) When a teammate's "
                                    "briefing or annotation is provided, attribute it to "
                                    "them by name and reference what they specifically "
                                    "said about that bill. If a section has no data, write "
                                    "one short line saying so.\n\n"
                                    + "\n\n".join(_sections))
                                st.session_state[_ov_cache_key] = ai_briefing.generate(
                                    _prompt, max_tokens=1200)
                        except RuntimeError as _e:
                            st.session_state[_ov_cache_key] = None
                            st.info("Team overview needs the AI service configured "
                                    "(Azure OpenAI). " + str(_e))
                        except Exception as _e:
                            st.session_state[_ov_cache_key] = None
                            st.error(f"Couldn't generate overview: {_e}")
                if st.session_state.get(_ov_cache_key):
                    st.markdown(
                        f"<div style='background:#F0F9FF;border:1px solid #BAE6FD;"
                        f"border-radius:12px;padding:16px 20px;margin:6px 0 10px'>"
                        f"<div style='font-size:12px;font-weight:700;color:#0369A1;"
                        f"margin-bottom:8px'>What's converging on {_team}</div>"
                        f"<div style='font-size:13px;color:#0C4A6E;line-height:1.7'>"
                        f"{st.session_state[_ov_cache_key]}</div></div>",
                        unsafe_allow_html=True)
                st.markdown("---")

                # ── 🚩 Flagged for you (directed hand-offs from teammates) ──────
                from user_profile import list_flagged_for
                _flagged_me = list_flagged_for(_user_id, _sharers) if _sharers else []
                if _flagged_me:
                    st.markdown(
                        f"<div style='margin:2px 0 10px 0;padding:12px 16px;border-radius:10px;"
                        f"background:#FEF3C7;border:1px solid #FCD34D'>"
                        f"<div style='font-weight:700;color:#92400E;font-size:14px'>"
                        f"{len(_flagged_me)} bill(s) flagged for you</div>"
                        f"<div style='font-size:12px;color:#92400E'>"
                        f"A teammate specifically wants you to see these.</div></div>",
                        unsafe_allow_html=True)
                    for _fit in _flagged_me:
                        _fd = _fit.get("data", {})
                        _fa = _fd.get("annotation", {}) or {}
                        _fauth = _disp(_fa.get("author", ""))
                        _fdir = _fa.get("direction", "unclear")
                        _fdc = {"positive": "#16A34A", "negative": "#DC2626",
                                "mixed": "#A2570F", "unclear": "#454F5A"}.get(_fdir, "#454F5A")
                        st.markdown(
                            f"<div style='margin:4px 0 8px 12px;padding:8px 12px;"
                            f"border-left:3px solid {_fdc};background:#fff'>"
                            f"<b>{_fd.get('bill_number','')}</b> — {_fd.get('title','')[:80]}<br>"
                            f"<span style='font-size:11px;color:{_fdc};font-weight:600'>"
                            f"{_fdir.title()} · flagged by {_fauth}</span>"
                            f"<div style='font-size:12.5px;color:#334155;margin-top:2px'>"
                            f"{_fa.get('assessment','')}</div></div>",
                            unsafe_allow_html=True)
                    st.markdown("---")

                # ── 🔗 Where your team overlaps (ranked convergence) ─────────────
                if _overlaps:
                    st.markdown(_section_banner(
                        "", "Where your team overlaps",
                        "Bills the AI flagged for you and teammates independently.",
                        *_TEAM_ORANGE), unsafe_allow_html=True)
                    st.caption(
                        "**High-impact** bills the AI independently flagged for you "
                        "**and** for teammates — through each person's own prompts "
                        "and role profile, not because anyone pinned them. Ranked by "
                        "how many people a bill lands on: the more overlap, the "
                        "stronger the signal.")
                    _RLBL_OV = {3: "High Impact", 2: "Moderate Impact",
                                1: "Low Impact", 0: "No Impact"}
                    _my_pins_ov = {str(b.get("data", {}).get("bill_id", ""))
                                   for b in _cosmos_list("bill")}
                    _at_cap_ov = len(_my_pins_ov) >= MAX_TRACKED_BILLS
                    _master_ov = load_master(CURRENT_SESSION)
                    for _ov in _overlaps[:8]:
                        _others_nm = ", ".join(
                            _name_by_email.get(e, e.split("@")[0]) for e in _ov["others"])
                        _n = _ov["n"]
                        # Warmer as more teammates converge on the same bill.
                        _bc = ("#DC2626" if _n >= 4 else
                               "#C2410C" if _n == 3 else "#A2570F")
                        # Full bill context from master (title, summary, stage,
                        # floor-vote, U of I, ILGA link) — same facts as My Bills.
                        _mrow_ov = None
                        if (not _master_ov.empty
                                and _ov["bill_id"] in _master_ov["bill_id"].values):
                            _mrow_ov = _master_ov[
                                _master_ov["bill_id"] == _ov["bill_id"]].iloc[0]
                        if _mrow_ov is not None:
                            _ctx_ov = _bill_context_html(_mrow_ov)
                        else:
                            _ctx_ov = (
                                f"<div style='font-size:13px;font-weight:600;"
                                f"color:#0F172A;margin-top:2px'>{_ov['headline'][:120]}</div>"
                                f"<div style='margin-top:6px'>"
                                f"{_ilga_link_html(_ov['bill_number'], _ov['session_id'])}</div>")
                        _oc_main, _oc_btn = st.columns([9, 2])
                        with _oc_main:
                            st.markdown(
                                f"<div style='margin:6px 0;padding:12px 16px;"
                                f"border-radius:10px;background:#fff;border:1px solid "
                                f"#E2E8F0;border-left:5px solid {_bc}'>"
                                f"<div style='font-size:13px;font-weight:700;"
                                f"color:#0F172A'>{_ov['bill_number']}</div>"
                                f"<div style='font-size:11.5px;color:{_bc};"
                                f"font-weight:700;margin:2px 0 1px'>{_n}-way "
                                f"overlap · peak {_RLBL_OV.get(_ov['max_rating'],'')}</div>"
                                f"<div style='font-size:11.5px;color:#454F5A'>"
                                f"Independently flagged for <b>you</b> and "
                                f"<b>{_others_nm}</b></div>"
                                + _ctx_ov
                                + "</div>",
                                unsafe_allow_html=True)
                        with _oc_btn:
                            st.markdown("<div style='height:14px'></div>",
                                        unsafe_allow_html=True)
                            _obid = _ov["bill_id"]
                            if _obid in _my_pins_ov:
                                st.caption("Tracked")
                            elif _at_cap_ov:
                                st.caption(f"{MAX_TRACKED_BILLS} max")
                            elif st.button("Pin", key=f"ovpin_{_obid}",
                                           help="Pin to My Bills"):
                                if _cosmos_save_bill(
                                        _obid, _ov["bill_number"], _ov["headline"],
                                        int(_ov["session_id"]), {}, notify=True):
                                    st.success("Pinned")
                                    st.rerun()
                    st.markdown("---")

                # ── 📨 Briefings shared with you (AI-generated, from a teammate) ──
                from user_profile import list_shared_briefings
                _shb = list_shared_briefings(_user_id)
                # Scope to the team you're VIEWING: only keep briefings shared by an
                # active member of this team. list_shared_briefings returns every
                # briefing shared with you across all your teams, so without this
                # filter the switcher has no effect on this section.
                # Direct person-to-person shares (no team lens) always show; team-
                # routed shares show when you're viewing that team (or the sharer is
                # in its roster). This is why the team switcher still scopes team
                # shares but never hides a briefing sent straight to you.
                _team_emails = {e.lower() for e in _active_roster}
                _active_team_name = (_active_team.get("name", "")
                                     if _active_team else "")

                def _keep_shb(_s):
                    _d = _s.get("data", {})
                    _via = str(_d.get("share_via_team", "") or "")
                    _sharer = str(_d.get("shared_by", "")).lower()
                    if not _via:
                        return True
                    return _via == _active_team_name or _sharer in _team_emails
                _shb = [s for s in _shb if _keep_shb(s)]
                if _shb:
                    _my_pins = {str(b.get("data", {}).get("bill_id", ""))
                                for b in _cosmos_list("bill")}
                    _master_sh = load_master(CURRENT_SESSION)
                    st.markdown(_section_banner(
                        "", "Briefings shared with you",
                        f"{len(_shb)} AI briefing(s) a teammate thought you should see.",
                        *_TEAM_BLUE), unsafe_allow_html=True)
                    for _sb in _shb:
                        _sd = _sb.get("data", {})
                        _by = _disp(_sd.get("shared_by", ""))
                        _scope = _sd.get("share_scope", "individual")
                        _via_t = str(_sd.get("share_via_team", "") or "")
                        if _scope == "team":
                            _why = (f"shared with everyone in {_via_t}" if _via_t
                                    else "shared with the team")
                        elif _via_t:
                            _why = f"shared with you · via {_via_t}"
                        else:
                            _why = "shared with you directly"
                        _brf = _sd.get("briefing", {}) or {}
                        _sbid = str(_sd.get("bill_id", ""))
                        _ctx_shb = ""
                        if (not _master_sh.empty
                                and _sbid in _master_sh["bill_id"].astype(str).values):
                            _ctx_shb = _bill_context_html(
                                _master_sh[_master_sh["bill_id"].astype(str) == _sbid].iloc[0])
                        st.markdown(
                            f"<div style='margin:4px 0;padding:10px 14px;border-radius:10px;"
                            f"background:#fff;border:1px solid #E0E7FF;border-left:4px solid #6366F1'>"
                            f"<div style='font-size:13px;font-weight:600;color:#0F172A'>"
                            f"{_sd.get('bill_number','')} — {_sd.get('title','')[:80]}</div>"
                            f"<div style='font-size:11px;color:#4F46C4;font-weight:600;"
                            f"margin:2px 0'>AI briefing · {_by} {_why}</div>"
                            + _ctx_shb
                            + "</div>",
                            unsafe_allow_html=True)
                        # Full shared investigation — the same depth as My Briefing.
                        with st.expander(f"Full briefing from {_by} — "
                                         f"{_sd.get('bill_number','')}"):
                            _bf_order = [
                                ("headline", "Headline"),
                                ("what_changes", "What changes"),
                                ("who_it_affects", "Who it affects"),
                                ("recommended_actions", "Recommended actions"),
                                ("swot", "SWOT"),
                                ("timeline", "Timeline"),
                                ("confidence", "Confidence"),
                            ]
                            _shown_any = False
                            for _k, _lbl in _bf_order:
                                _v = _brf.get(_k)
                                if not _v:
                                    continue
                                _shown_any = True
                                if isinstance(_v, dict):
                                    _v = "  \n".join(f"*{_kk}:* {_vv}"
                                                     for _kk, _vv in _v.items() if _vv)
                                elif isinstance(_v, list):
                                    _v = "  \n".join(f"- {x}" for x in _v)
                                st.markdown(f"**{_lbl}**  \n{_v}")
                            # Anything else the briefing carried that we didn't name.
                            for _k, _v in _brf.items():
                                if _k in {c for c, _ in _bf_order} or not _v:
                                    continue
                                if isinstance(_v, (str, int, float)):
                                    st.markdown(f"**{_k.replace('_',' ').title()}**  \n{_v}")
                                    _shown_any = True
                            if not _shown_any:
                                st.caption("This shared briefing didn't include "
                                           "detailed fields.")
                        # Offer to pin the bill (respects cap / dedupe)
                        _sc_a, _sc_b = st.columns([5, 1])
                        with _sc_b:
                            if _sbid in _my_pins:
                                st.caption("Tracked")
                            elif len(_my_pins) >= MAX_TRACKED_BILLS:
                                st.caption("List full")
                            elif st.button("Pin", key=f"shbpin_{_sb.get('id','')}",
                                           help="Pin to My Bills"):
                                if _cosmos_save_bill(
                                        _sbid, _sd.get("bill_number", ""),
                                        _sd.get("title", ""),
                                        int(_sd.get("session_id", CURRENT_SESSION) or CURRENT_SESSION),
                                        {}, notify=True):
                                    st.success("Pinned")
                                    st.rerun()
                    st.markdown("---")

                # ── 📤 Briefings I've shared with others (review what I sent) ────
                # Resilient import: if a stale user_profile.py (without this helper)
                # is ever deployed, skip this one section rather than break the whole
                # team view.
                try:
                    from user_profile import list_briefings_i_shared as _lbis
                    _i_shared = _lbis(_user_id)
                except Exception:
                    _i_shared = []
                # "Everyone in a team" writes one doc per member, so group by bill
                # and dedupe recipients into a single row per bill.
                _sent = {}
                for _ish in _i_shared:
                    _isd = _ish.get("data", {})
                    _bnum = _isd.get("bill_number", "") or "(bill)"
                    _titl = str(_isd.get("title", ""))[:80]
                    _iscope = _isd.get("share_scope", "individual")
                    _isvia = str(_isd.get("share_via_team", "") or "")
                    if _iscope == "team":
                        _lbl = (f"everyone in {_isvia}" if _isvia
                                else "the whole team")
                    else:
                        _to = _disp(_ish.get("_recipient", ""))
                        _lbl = f"{_to} (via {_isvia})" if _isvia else f"{_to} (direct)"
                    _e = _sent.setdefault(_bnum, {"title": _titl, "labels": set()})
                    _e["labels"].add(_lbl)
                if _sent:
                    st.markdown(_section_banner(
                        "", "Briefings I've shared with others",
                        f"{len(_sent)} bill briefing(s) you've sent — and who got each.",
                        *_TEAM_BLUE), unsafe_allow_html=True)
                    for _bnum, _e in _sent.items():
                        _lbls = " · ".join(sorted(_e["labels"]))
                        st.markdown(
                            f"<div style='margin:4px 0;padding:10px 14px;border-radius:10px;"
                            f"background:#fff;border:1px solid #DBEAFE;"
                            f"border-left:4px solid #2563EB'>"
                            f"<div style='font-size:13px;font-weight:600;color:#0F172A'>"
                            f"{_bnum} — {_e['title']}</div>"
                            f"<div style='font-size:11px;color:#2563EB;font-weight:600;"
                            f"margin:2px 0'>Shared with: {_lbls}</div>"
                            + "</div>", unsafe_allow_html=True)
                    st.markdown("---")

                st.markdown(_section_banner(
                    "", "Bills shared with you",
                    "Bills teammates are tracking and shared for you to review.",
                    *_TEAM_GREEN), unsafe_allow_html=True)
                if not _sharers:
                    st.info("No teammates are sharing yet. When they turn on sharing "
                            "in My Profile, their tracked bills appear here.")
                else:
                    _shared = list_team_shared(_sharers, "bill")
                    _master_sh = load_master(CURRENT_SESSION)
                    # my current pins, to show what I already track / enforce the cap
                    _mine = _cosmos_list("bill")
                    _mine_ids = {str(b.get("data", {}).get("bill_id", "")) for b in _mine}
                    _at_cap = len(_mine) >= MAX_TRACKED_BILLS

                    st.caption(f"{len(_shared)} shared bill(s) from "
                               f"{len(_sharers)} teammate(s).")
                    for it in _shared:
                        d = it.get("data", {})
                        bid = str(d.get("bill_id", ""))
                        bnum = d.get("bill_number", "")
                        title = d.get("title", "")
                        owner = _disp(it.get("_owner", ""))
                        _ann = d.get("annotation", {}) or {}
                        _assess = str(_ann.get("assessment", "") or "").strip()
                        _dir = _ann.get("direction", "unclear")
                        _dcolor = {"positive": "#16A34A", "negative": "#DC2626",
                                   "mixed": "#A2570F", "unclear": "#454F5A"}.get(_dir, "#454F5A")
                        # Standard bill block (summary, goal, momentum, stage,
                        # floor-vote, U of I, ILGA link) — same facts as My Bills.
                        _ctx_sh = ""
                        if (not _master_sh.empty
                                and bid in _master_sh["bill_id"].astype(str).values):
                            _ctx_sh = _bill_context_html(
                                _master_sh[_master_sh["bill_id"].astype(str) == bid].iloc[0])

                        c_main, c_btn = st.columns([9, 2])
                        with c_main:
                            st.markdown(
                                f"<div style='margin:4px 0;padding:10px 14px;"
                                f"border-radius:10px;background:#fff;border:1px solid "
                                f"#DCFCE7;border-left:4px solid #16A34A'>"
                                f"<div style='font-size:13px;font-weight:700;"
                                f"color:#0F172A'>{bnum} — {title[:90]}</div>"
                                f"<div style='font-size:11.5px;color:#15803D;"
                                f"font-weight:600;margin-top:2px'>tracked by {owner}"
                                + ("  ·  already in your list" if bid in _mine_ids else "")
                                + "</div>"
                                + _ctx_sh
                                + "</div>",
                                unsafe_allow_html=True)
                            if _assess:
                                st.markdown(
                                    f"<div style='margin:2px 0 6px 12px;font-size:12.5px;"
                                    f"border-left:3px solid {_dcolor};padding-left:8px;"
                                    f"color:#475569'>"
                                    f"<span style='font-weight:600;color:{_dcolor}'>"
                                    f"{_dir.title()}</span> · {_assess}"
                                    + (f"<br><span style='color:#454F5A;font-size:11.5px'>"
                                       f"Affects: {_ann.get('affects','')}"
                                       + (f" · Flagged: {', '.join(e.split('@')[0] for e in _ann.get('flagged', []))}"
                                          if _ann.get('flagged') else "")
                                       + (f" · Needs to know: {_ann.get('notify_who','')}"
                                          if _ann.get('notify_who') else "")
                                       + "</span>"
                                       if _ann.get('affects') or _ann.get('flagged') or _ann.get('notify_who') else "")
                                    + "</div>",
                                    unsafe_allow_html=True)
                        with c_btn:
                            if bid in _mine_ids:
                                # Active untrack — you can undo an accidental import.
                                _my_doc_id = next(
                                    (b.get("id") for b in _mine
                                     if str(b.get("data", {}).get("bill_id", "")) == bid),
                                    None)
                                if st.button("Untrack", key=f"unimp_{bid}_{owner}",
                                             help="Remove from your tracked bills"):
                                    if _my_doc_id and _cosmos_delete(_my_doc_id):
                                        st.success(f"Removed {bnum}")
                                        st.rerun()
                            elif _at_cap:
                                st.button("List full", key=f"imp_{bid}_{owner}",
                                          disabled=True,
                                          help=f"You're at the {MAX_TRACKED_BILLS}-bill "
                                               "limit. Remove one to import.")
                            else:
                                if st.button("Import", key=f"imp_{bid}_{owner}",
                                             type="primary"):
                                    if _cosmos_save_bill(
                                            bid, bnum, title,
                                            int(d.get("session_id", CURRENT_SESSION)),
                                            it.get("snapshot", {}), notify=True):
                                        # Carry the teammate's annotation across,
                                        # read-only and attributed, so their read
                                        # travels with the bill into my list.
                                        if _assess:
                                            try:
                                                from user_auth import _container as _ct
                                                _sc = _ct("saved_items")
                                                for _b in _cosmos_list("bill"):
                                                    if str(_b.get("data", {}).get("bill_id")) == bid:
                                                        _b.setdefault("data", {})["imported_annotation"] = _ann
                                                        _sc.replace_item(item=_b["id"], body=_b)
                                                        break
                                            except Exception:
                                                pass
                                        st.success(f"Imported {bnum}.")
                                        st.rerun()
                        st.markdown("<hr style='margin:6px 0;border:none;"
                                    "border-top:1px solid #F1F5F9'>",
                                    unsafe_allow_html=True)
            except Exception as e:
                st.error(f"Couldn't load team view: {e}")


with tab_settings:
    st.markdown('<h2 class="sect-head">My Profile</h2>', unsafe_allow_html=True)

    if not _user_authed:
        st.warning("Sign in with your University of Illinois email to manage your settings.")
    else:
        # Display name (what teammates see) is edited inside the Role Profile
        # card below and saved by the SAME "Save role profile" button — no
        # separate Apply flow. Resolve the current value here so the field can
        # be pre-filled.
        try:
            from user_auth import get_user as _gu_dn, canonical_email as _ce_dn
            _dn_target = _ce_dn(_user_id)
            _me_rec = _gu_dn(_dn_target) or _gu_dn(_user_id) or {}
        except Exception:
            _me_rec = {}
            _dn_target = _user_id
        _cur_dn = str(_me_rec.get("display_name") or "")

        # ── My Role Profile ────────────────────────────────────────────────────
        st.markdown("### My Role Profile")
        st.caption(
            "Tell the system who you are and what you watch for. This becomes one "
            "always-on impact lens — the AI rates every active bill for how it "
            "affects someone in your role, and uses it to build your personalized "
            "briefing. Scored on the next scheduled run."
        )
        _profile_items = _cosmos_list("user_profile")
        # Newest wins. A failed delete used to leave two profile docs, and
        # [0] could return the OLD one — so the form showed stale text on every
        # refresh no matter how many times you saved.
        _profile_items = sorted(_profile_items,
                                key=lambda d: d.get("created_at", ""),
                                reverse=True)
        _profile = (_profile_items[0].get("data", {}) if _profile_items else {})
        if len(_profile_items) > 1:
            st.caption(f"Warning: {len(_profile_items)} profile records found — "
                       "the newest is shown; the extras are removed on next save.")

        with st.form("set_profile_form", clear_on_submit=False):
            _pf_dispname = st.text_input(
                "Your display name (what teammates see)",
                value=_cur_dn, max_chars=13, placeholder="e.g. Terrence",
                help="The short name teammates see wherever you're referenced — "
                     "shared bills, briefings, annotations, and team overlap. Saved "
                     "with this button, so it persists like the rest of your profile.")
            _pf_title = st.text_input(
                "Your title", value=_profile.get("title", ""),
                max_chars=200, placeholder="e.g. Director of IT Procurement",
            )
            _pf_sector = st.text_input(
                "Sector / industry", value=_profile.get("sector", ""),
                max_chars=200, placeholder="e.g. higher education, public sector",
            )
            _pf_func = st.text_input(
                "Functional role / area", value=_profile.get("functional_role", ""),
                max_chars=300,
                placeholder="what you actually do — e.g. purchasing, data privacy, AI governance",
            )
            _pf_stake = st.text_input(
                "Key stakeholders / clients (optional)",
                value=_profile.get("stakeholders", ""),
                max_chars=300, placeholder="e.g. campus IT, procurement office, vendors",
            )
            _pf_desc = st.text_area(
                "What you do and what you watch for in legislation",
                value=_profile.get("description", ""),
                max_chars=1500, height=160,
                placeholder="Describe your responsibilities and the kinds of bills "
                            "you need to know about. The more specific, the sharper "
                            "your briefing. (up to 1500 characters)",
                help="Pasting more than 1500 characters will be silently blocked "
                     "by your browser — trim it first if nothing appears.",
            )
            _pf_consent = st.checkbox(
                "My role and interests may inform anonymized, aggregate trends "
                "(never my individual data or identity).",
                value=bool(_profile.get("aggregate_consent", False)),
            )
            _profile_submitted = st.form_submit_button(
                "Save role profile", type="primary")

        if _profile_submitted:
            if not _user_id:
                st.error("Your session isn't active. Refresh the page, sign in "
                         "again, and retry.")
            elif not _pf_desc.strip():
                st.warning("Add a short description of what you do and watch for — "
                           "it's the field that drives your briefing.")
            else:
                try:
                    import secrets as _sec
                    from user_auth import _container
                    _c = _container("saved_items")
                    if _c is None:
                        st.error("Couldn't reach the database just now. "
                                 "Please try again in a moment.")
                    else:
                        # Single profile per user: remove EVERY existing doc
                        # before writing. A silent delete failure previously
                        # left duplicates that stranded the form on stale data.
                        _failed = 0
                        for _old in _profile_items:
                            if not _cosmos_delete(_old.get("id", "")):
                                _failed += 1
                        if _failed:
                            st.warning(
                                f"Couldn't remove {_failed} old profile record(s). "
                                "Your new profile is saved and takes precedence, "
                                "but tell an admin if this repeats.")
                        _c.create_item({
                            "id":         _sec.token_urlsafe(16),
                            "user_id":    _user_id,
                            "type":       "user_profile",
                            "label":      "Role Profile",
                            "data": {
                                "title":             _pf_title.strip()[:200],
                                "sector":            _pf_sector.strip()[:200],
                                "functional_role":   _pf_func.strip()[:300],
                                "stakeholders":      _pf_stake.strip()[:300],
                                "description":       _pf_desc.strip()[:1500],
                                "aggregate_consent": bool(_pf_consent),
                            },
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        })
                        # Save the display name to the users record (what teammates
                        # see) with the SAME button. update_user is now resilient to
                        # the illinois/uillinois alias split, so this persists.
                        _dn_clean = " ".join(str(_pf_dispname or "").split())[:13].strip()
                        try:
                            from user_auth import update_user as _uu_dn
                            if not _uu_dn(_dn_target, {"display_name": _dn_clean}):
                                st.warning("Role profile saved, but the display name "
                                           "couldn't be written — try again, and tell "
                                           "an admin if it repeats.")
                            st.session_state.pop("_dn_cache", None)
                        except Exception as _e_dn:
                            st.warning(f"Profile saved, but the display name didn't: "
                                       f"{_e_dn}")
                        _bust_saved_items_cache()
                        st.success(
                            "Role profile saved. It's scored on the next "
                            "scheduled run, and your briefing will populate from it.")
                        st.rerun()
                except Exception as e:
                    st.error(f"Could not save profile: {e}")

        st.markdown("---")

        # ── Impact Prompts ─────────────────────────────────────────────────────
        st.markdown("### Impact Prompts")
        MAX_PROMPTS = 5
        _prompts = _cosmos_list("impact_prompt")
        st.caption(
            f"{len(_prompts)} / {MAX_PROMPTS} prompts. Ask a specific impact question "
            "and the AI scores every active bill against it — No / Low / Moderate / "
            "High Impact, same scale as the standard U of I rating. Scoring runs "
            "nightly on the server; new prompts show results the next morning. "
            "Results power the impact filter in Search & Pin and Explorer."
        )

        if _prompts:
            for it in _prompts:
                d = it.get("data", {})
                q = d.get("question", "")
                c_txt, c_del = st.columns([8, 1])
                with c_txt:
                    st.markdown(
                        f"**{it.get('label','(unnamed)')}**  \n"
                        f"<span style='font-size:12px;color:#454F5A'>"
                        f"How does this bill impact {q}?</span>",
                        unsafe_allow_html=True,
                    )
                with c_del:
                    if st.button("Delete", key=f"set_delprompt_{it.get('id','')}",
                                 help="Delete prompt (stops nightly scoring)"):
                        if _cosmos_delete(it.get("id", "")):
                            st.rerun()
        else:
            st.caption("No impact prompts yet — create your first below.")

        st.markdown("")
        if len(_prompts) >= MAX_PROMPTS:
            st.info(f"You've reached the {MAX_PROMPTS}-prompt limit. "
                    "Delete one above to add another.")
        else:
            st.markdown("**Create an impact prompt**")
            st.markdown(
                '<div style="font-size:12.5px;color:#475569;background:#F1F5F9;'
                'border:1px solid #E2E8F0;border-radius:8px;padding:9px 12px;'
                'margin-bottom:12px;line-height:1.5">'
                '<strong>Heads up:</strong> a new prompt is scored overnight. '
                'Results appear in <strong>Search & Pin</strong> and on your bills by about '
                '<strong>9:00&nbsp;AM Central</strong> the next morning — not instantly. '
                'You can create it now and check back tomorrow.'
                '</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                '<div style="font-size:13px;color:#475569;margin-bottom:4px">'
                'How does this bill impact&nbsp;'
                '<span style="color:#454F5A">___________</span> ?</div>',
                unsafe_allow_html=True,
            )
            # Same atomic-submit treatment as the category form above.
            with st.form("set_prompt_form", clear_on_submit=False):
                # Audit issue 12: label was collapsed and the placeholder was
                # carrying it.
                _p_target = st.text_input(
                    "Complete the question", key="set_prompt_target",
                    placeholder="e.g. the budgeting and finance department at UIUC",
                    max_chars=300,
                    help="Kept short on purpose: this text is sent to the AI once "
                         "per active bill, and long questions crowd out the bill "
                         "text in the model's context window.",
                )
                _p_name = st.text_input(
                    "Short name for this prompt", key="set_prompt_name",
                    placeholder="e.g. Budget & Finance", max_chars=60,
                )
                _prompt_submitted = st.form_submit_button("+ Create impact prompt",
                                                          type="primary")
            if _prompt_submitted:
                # max_chars is a browser attribute only — enforce server-side too.
                target = _p_target.strip().rstrip("?").strip()[:300]
                name   = (_p_name.strip() or (target[:40] if target else ""))[:60]
                if len(_cosmos_list("impact_prompt")) >= MAX_PROMPTS:
                    st.warning(f"Prompt limit ({MAX_PROMPTS}) reached.")
                elif not target:
                    st.warning("Complete the impact question.")
                elif len(target) < 5:
                    st.warning("Be a bit more specific about who or what is impacted.")
                else:
                    if not _user_id:
                        st.error("Your session isn't active. Refresh the page, "
                                 "sign in again, and retry.")
                    else:
                        try:
                            import secrets as _sec
                            from user_auth import _container
                            _c = _container("saved_items")
                            if _c is None:
                                st.error("Couldn't reach the database just now. "
                                         "Please try again in a moment.")
                            else:
                                _c.create_item({
                                    "id":         _sec.token_urlsafe(16),
                                    "user_id":    _user_id,
                                    "type":       "impact_prompt",
                                    "label":      name,
                                    "data":       {"question": target},
                                    "created_at": datetime.now(timezone.utc).isoformat(),
                                })
                                st.success(
                                    f"Created '{name}'. It's saved now and will be "
                                    "scored overnight — results appear in Search & Pin by "
                                    "about 9:00 AM Central tomorrow.")
                                st.rerun()
                        except Exception as e:
                            st.error(f"Could not save prompt: {e}")

        st.markdown("---")

        # ── Interest Categories ────────────────────────────────────────────────
        st.markdown("### Interest Categories")
        _cats = _cosmos_list("category")
        st.caption(
            f"{len(_cats)} / {MAX_CATEGORIES} categories. These power your topic "
            "monitors and digests. Keyword matching runs against the raw LLM output "
            "(ground truth), and falls back to the standardized field only when a raw "
            "field isn't present."
        )

        if _cats:
            for it in _cats:
                d    = it.get("data", {})
                kws  = d.get("keywords", [])
                flds = d.get("fields", [])
                fld_names = ", ".join(
                    next((k for k, v in SEARCHABLE_FIELDS.items() if v == f), f) for f in flds
                ) if flds else (d.get("type", "").replace("_", " ").title() or "—")
                c_txt, c_del = st.columns([8, 1])
                with c_txt:
                    kw_str = (" · keywords: " + ", ".join(kws)) if kws else ""
                    st.markdown(
                        f"**{it.get('label','(unnamed)')}**  \n"
                        f"<span style='font-size:12px;color:#454F5A'>{fld_names}{kw_str}</span>",
                        unsafe_allow_html=True,
                    )
                with c_del:
                    if st.button("Delete", key=f"set_delcat_{it.get('id','')}", help="Delete category"):
                        if _cosmos_delete(it.get("id", "")):
                            st.rerun()
        else:
            st.caption("No categories yet — create your first below.")

        st.markdown("")
        if len(_cats) >= MAX_CATEGORIES:
            st.info(f"You've reached the {MAX_CATEGORIES}-category limit. "
                    "Delete one above to add another.")
        else:
            st.markdown("**Create a category**")
            # st.form batches ALL inputs into a single atomic submit: typing and
            # field-picking cause ZERO server reruns until the button is pressed.
            # Previously every commit triggered a full page rerun, and any
            # websocket blip mid-entry silently wiped the form and ate the save.
            with st.form("set_cat_form", clear_on_submit=False):
                _new_name = st.text_input(
                    "Name", key="set_cat_name", max_chars=60,
                    placeholder="e.g. Foreign Exchange Students, Teacher Pay, Rural Hospitals",
                )
                _new_kws = st.text_input(
                    "Keywords (comma-separated)", key="set_cat_kws", max_chars=300,
                    placeholder="e.g. foreign exchange, international student, visiting scholar",
                    help="Up to 15 keywords. Each one is matched against every bill, "
                         "so a focused list performs better than a long one.",
                )
                _new_fields = st.multiselect(
                    "Match against these fields (raw LLM output preferred)",
                    options=list(SEARCHABLE_FIELDS.keys()),
                    default=["Beneficiaries", "Summary"],
                    key="set_cat_fields",
                )
                _notify_new = st.checkbox("Include in my digest", value=True, key="set_cat_notify")
                _cat_submitted = st.form_submit_button("+ Create category", type="primary")
            if _cat_submitted:
                MAX_KEYWORDS = 15
                # Each keyword is scanned against every bill in every matched
                # field, so cap the count as well as the length.
                kws   = [k.strip()[:60] for k in _new_kws.split(",") if k.strip()]
                _kw_overflow = len(kws) > MAX_KEYWORDS
                kws   = kws[:MAX_KEYWORDS]
                fcols = [SEARCHABLE_FIELDS[f] for f in _new_fields if f in SEARCHABLE_FIELDS]
                name  = (_new_name.strip() or (f"Keywords: {', '.join(kws[:2])}" if kws else ""))[:60]
                if len(_cosmos_list("category")) >= MAX_CATEGORIES:
                    st.warning(f"Category limit ({MAX_CATEGORIES}) reached.")
                elif not name:
                    st.warning("Give your category a name.")
                elif not kws:
                    st.warning("Enter at least one keyword.")
                elif not fcols:
                    st.warning("Choose at least one field to match against.")
                else:
                    if _kw_overflow:
                        st.info(f"Only the first {MAX_KEYWORDS} keywords were kept.")
                    if _cosmos_save_topic("keyword", name, fcols, kws, _notify_new):
                        st.success(f"Created '{name}'.")
                        st.rerun()

        st.markdown("---")

        # ── Email & Alerts ─────────────────────────────────────────────────────
        # Everything below lives inside st.form(). Streamlit holds these widget
        # values client-side and reruns the script ONLY when Apply is pressed.
        #
        # This replaces a cluster of widgets that each carried BOTH key= and an
        # explicit value=/index=/default=, with several disabled= flags computed
        # from a sibling widget in the same run, and one widget (`set_days`)
        # instantiated conditionally. Inside a form none of that can churn:
        # no keys, no disabled= interdependencies, no conditional widgets.
        st.markdown("### Email & Alerts")

        _WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday",
                     "Friday", "Saturday", "Sunday"]
        _SECTION_DEFS = [
            # Audit bug 7: these render as checkbox labels in Email & Alerts,
            # which the report's Steps to Recreate name directly.
            ("tracked",  "My tracked bills — what changed on your watchlist "
                         "(new actions, probability moves, status changes)"),
            ("flagged",  "Newly flagged for you — bills newly rated "
                         "Moderate/High on your impact questions"),
            ("imminent", "Imminent votes — your items closest to a floor vote"),
            ("trends",   "The big picture — session-level activity and "
                         "momentum in your categories"),
        ]
        _saved_sections = _user.get("digest_sections") or [k for k, _ in _SECTION_DEFS]
        _freq_opts  = ["Daily", "Weekly", "Custom days"]
        _freq_index = {"daily": 0, "weekly": 1, "custom": 2}.get(
            _user.get("digest_frequency", "weekly"), 1)
        _saved_days = _user.get("digest_days") or ["Monday"]

        with st.form("email_prefs_form"):
            _enabled = st.toggle("Enable digest emails",
                                 value=bool(_user.get("digest_enabled", True)))
            _mode = st.radio(
                "When should we email you?",
                ["Scheduled digest", "Real-time (as changes happen)"],
                index=0 if _user.get("alert_mode", "scheduled") == "scheduled" else 1,
                horizontal=True,
            )
            _freq = st.selectbox("Frequency", _freq_opts, index=_freq_index)
            _days = st.multiselect(
                "Send on", _WEEKDAYS, default=[d for d in _saved_days if d in _WEEKDAYS],
                help="Only used when Frequency is set to 'Custom days'.")

            st.markdown("**My briefing includes** "
                        "&nbsp;<span style='color:#454F5A;font-size:12px'>(the headline "
                        "and summary line are always included)</span>",
                        unsafe_allow_html=True)
            _section_flags = [
                (_key, st.checkbox(_lbl, value=(_key in _saved_sections)))
                for _key, _lbl in _SECTION_DEFS
            ]

            st.caption(
                "Real-time emails you each time a tracked bill changes (new action, text "
                "change, or momentum shift). Scheduled bundles changes into a digest on your "
                "cadence. Delivery runs from a backend job (Phase 3). "
                "Nothing is saved until you press Apply."
            )
            _email_submitted = st.form_submit_button(
                "Apply email preferences", type="primary")

        if _email_submitted:
            if not _user_id:
                st.error("Your session isn't active. Sign in again and retry.")
            else:
                try:
                    from user_auth import update_user
                    _scheduled = _mode.startswith("Scheduled")
                    _sections  = [k for k, on in _section_flags if on]
                    freq_val = {"Daily": "daily", "Weekly": "weekly",
                                "Custom days": "custom"}[_freq]
                    mode_val = "scheduled" if _scheduled else "realtime"
                    _days_val = _days or ["Monday"]
                    update_user(_user_id, {
                        "digest_enabled":   _enabled,
                        "alert_mode":       mode_val,
                        "digest_frequency": freq_val,
                        "digest_days":      _days_val,
                        "digest_sections":  _sections,
                    })
                    st.session_state.user_record.update({
                        "digest_enabled": _enabled, "alert_mode": mode_val,
                        "digest_frequency": freq_val, "digest_days": _days_val,
                        "digest_sections": _sections,
                    })
                    st.success("Saved.")
                except Exception as e:
                    st.error(str(e))

        st.markdown("---")

        # ── Team ───────────────────────────────────────────────────────────────
        st.markdown("### Team")
        st.caption("Create a team and invite colleagues by email — they'll get a "
                   "sign-in link and see the invite here to accept. Or accept an "
                   "invite below. Membership and sharing are separate — flip the "
                   "toggle to let teammates see your tracked bills and notes.")

        try:
            from team_service import (create_team, invite_member, accept_invite,
                                      decline_invite, leave_team, remove_member,
                                      delete_team,
                                      list_members, list_teams_for_user,
                                      get_share_enabled, set_share_enabled,
                                      MAX_TEAMS)
            from user_auth import get_user as _gu2, \
                                  canonical_email as _ce_mem

            # Sharing preference — one flag, applies to every team you're on.
            _cur_share = get_share_enabled(_user)
            _new_share = st.toggle(
                "Share my tracked bills and notes with my teammates",
                value=_cur_share, key="team_share_toggle")
            if _new_share != _cur_share:
                if set_share_enabled(_user_id, _new_share):
                    st.session_state.user_record["team_share_enabled"] = _new_share
                    st.rerun()

            _teams_ui   = list_teams_for_user(_user_id)
            _pending_ui = [t for t in _teams_ui if t.get("status") == "invited"]
            _active_ui  = [t for t in _teams_ui if t.get("status") == "active"]
            _at_team_cap = len(_active_ui) >= MAX_TEAMS
            st.caption(f"You're in **{len(_active_ui)} of {MAX_TEAMS}** teams."
                       + ("  Leave one to create or join another."
                          if _at_team_cap else ""))

            # ── Pending invites ────────────────────────────────────────────────
            if _pending_ui:
                st.markdown("**Pending invites**")
                for _inv in _pending_ui:
                    _c1, _c2, _c3 = st.columns([4, 1, 1])
                    with _c1:
                        st.markdown(
                            f"**{_inv['name']}** — invited by "
                            f"{_inv.get('owner_email','').split('@')[0]}")
                    with _c2:
                        if _at_team_cap:
                            st.button("Accept", key=f"acc_{_inv['team_id']}",
                                      disabled=True, use_container_width=True,
                                      help=f"You're at the {MAX_TEAMS}-team limit — "
                                           "leave a team to accept this.")
                        elif st.button("Accept", key=f"acc_{_inv['team_id']}",
                                       type="primary", use_container_width=True):
                            if accept_invite(_inv["team_id"], _user_id):
                                st.session_state["active_team_id"] = _inv["team_id"]
                                st.rerun()
                            else:
                                st.error("Couldn't accept — try again.")
                    with _c3:
                        if st.button("Decline", key=f"dec_{_inv['team_id']}",
                                     use_container_width=True):
                            if decline_invite(_inv["team_id"], _user_id):
                                st.rerun()
                st.markdown("---")

            # ── Teams you're on ────────────────────────────────────────────────
            if _active_ui:
                st.markdown("**Your teams**")
                for _t in _active_ui:
                    _tid   = _t["team_id"]
                    _owner = (_t.get("role") == "owner")
                    with st.expander(
                            f"{_t['name']}" + ("  ·  you're the owner" if _owner else ""),
                            expanded=(len(_active_ui) == 1)):
                        _mem = list_members(_tid)
                        for _m in sorted(_mem, key=lambda x: (
                                x.get("role") != "owner", x.get("user_email", ""))):
                            _m_email = _m.get("user_email", "")
                            _m_name  = ((_gu2(_ce_mem(_m_email)) or _gu2(_m_email)
                                         or {}).get("display_name")
                                        or _m_email.split("@")[0])
                            _m_role  = _m.get("role", "member")
                            _m_stat  = _m.get("status", "invited")
                            _badge   = ("owner" if _m_role == "owner"
                                        else "active" if _m_stat == "active"
                                        else "⏳ invited")
                            _rc1, _rc2 = st.columns([5, 1])
                            with _rc1:
                                st.markdown(
                                    f"{_m_name}  <span style='color:#454F5A;"
                                    f"font-size:12px'>· {_m_email} · {_badge}</span>",
                                    unsafe_allow_html=True)
                            with _rc2:
                                if _owner and _m_email != _user_id:
                                    if st.button("Remove", key=f"rm_{_tid}_{_m_email}"):
                                        if remove_member(_tid, _m_email, _user_id):
                                            st.rerun()

                        if _owner:
                            st.markdown("<div style='height:6px'></div>",
                                        unsafe_allow_html=True)
                            with st.form(f"invite_form_{_tid}", clear_on_submit=True):
                                _inv_email = st.text_input(
                                    "Invite a teammate by email",
                                    placeholder=__import__("os").environ.get('LEG_COMPONENTS_WEBAPP_VIEWS_6_MY_TRACKER_PY__INV_EMAIL_L4633', ""))
                                if st.form_submit_button("Send invite"):
                                    _r = invite_member(_tid, _inv_email, _user_id)
                                    _msgs = {
                                        "invited": f"Invited {(_inv_email or '').strip().lower()} — "
                                                   "we emailed them a sign-in link.",
                                        "already_member": "They're already a member.",
                                        "already_invited": "They've already been invited.",
                                        "not_owner": "Only the team owner can invite.",
                                        "bad_domain": "Only University of Illinois "
                                                      "System addresses can be invited "
                                                      "(@illinois.edu, @uillinois.edu, "
                                                      "@uic.edu, @uis.edu).",
                                        "error": "Couldn't send that — check the email address."}
                                    if _r == "invited":
                                        st.success(_msgs[_r]); st.rerun()
                                    else:
                                        st.warning(_msgs.get(_r, "Couldn't invite."))
                            # Owner disband — the way to get below the team cap.
                            st.markdown("<div style='height:4px'></div>",
                                        unsafe_allow_html=True)
                            _delk = f"delteam_confirm_{_tid}"
                            if not st.session_state.get(_delk):
                                if st.button("Delete team", key=f"delteam_{_tid}",
                                             help="Disband this team for everyone. "
                                                  "Frees a slot toward your team limit."):
                                    st.session_state[_delk] = True
                                    st.rerun()
                            else:
                                st.warning(f"Delete **{_t['name']}** for everyone? "
                                           "This removes all members and can't be undone.")
                                _dc1, _dc2 = st.columns(2)
                                with _dc1:
                                    if st.button("Yes, delete", type="primary",
                                                 key=f"delteam_yes_{_tid}"):
                                        if delete_team(_tid, _user_id):
                                            st.session_state.pop("active_team_id", None)
                                            st.session_state.pop(_delk, None)
                                            st.success("Team deleted.")
                                            st.rerun()
                                        else:
                                            st.error("Couldn't delete — only the "
                                                     "owner can, and it must still exist.")
                                with _dc2:
                                    if st.button("Cancel", key=f"delteam_no_{_tid}"):
                                        st.session_state.pop(_delk, None)
                                        st.rerun()
                        else:
                            if st.button("Leave this team", key=f"leave_{_tid}"):
                                if leave_team(_tid, _user_id):
                                    st.session_state.pop("active_team_id", None)
                                    st.rerun()
                st.markdown("---")

            # ── Create a team ──────────────────────────────────────────────────
            st.markdown("**Create a team**")
            if _at_team_cap:
                st.info(f"You're in the maximum of {MAX_TEAMS} teams. Leave one "
                        "above — or delete a team you own — to create another.")
            else:
                with st.form("create_team_form", clear_on_submit=True):
                    _new_team_name = st.text_input(
                        "Team name", placeholder="e.g. Planning & Budgeting",
                        help="You'll be the team owner and can invite colleagues by email.")
                    if st.form_submit_button("Create team", type="primary"):
                        _name_clean = (_new_team_name or "").strip()
                        if not _name_clean:
                            st.error("Give your team a name.")
                        else:
                            _tid_new = create_team(_name_clean, _user_id)
                            if _tid_new:
                                st.session_state["active_team_id"] = _tid_new
                                st.success(f"Created {_name_clean} — you're the owner.")
                                st.rerun()
                            else:
                                st.error("Couldn't create the team — you may be at "
                                         f"the {MAX_TEAMS}-team limit. Try again.")
        except Exception as _e:
            st.error(f"Team management is unavailable right now: {_e}")

# ── Bug 27 DIAGNOSTIC (temporary): end-of-run marker and the log ─────────────
try:
    _dbg_row["end"] = "yes"
    if st.query_params.get("debug") == "1":
        with st.expander("Diagnostic: run log (Bug 27) — newest first", expanded=True):
            _cols = ["time", "run", "session", "server", "page", "before", "drawn", "returned", "end"]
            _lines = ["  ".join(c.ljust(w) for c, w in zip(_cols, [13, 5, 8, 16, 14, 12, 6, 9, 4]))]
            for _r in reversed(_DBG):
                _lines.append("  ".join(str(_r.get(c, "")).ljust(w) for c, w in zip(_cols, [13, 5, 8, 16, 14, 12, 6, 9, 4])))
            st.code("\n".join(_lines), language=None)
except Exception:
    pass
