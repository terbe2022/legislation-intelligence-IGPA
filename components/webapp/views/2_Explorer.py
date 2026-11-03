"""
pages/2_🔍_Explorer.py  — Legislation Explorer
================================================
Changes vs explorer_app.py:
  - Loads FAISS index, metadata.parquet, and config.json from Azure blob SAS URLs
  - Session selector: choose which GA session(s) to search
  - New beneficiary taxonomy columns
  - Falls back to old intended_beneficiaries_category / _subcategory if new cols absent
  - DATA_CATALOG drives all URLs; no hardcoded paths here
  - Passage probability: Stage 2 model scores joined on bill_id;
    new "Pass Likelihood" filter, ProgressColumn in bill table,
    "Likely to Pass" KPI card — enables queries like
    "show high-impact bills likely to pass that affect students"
"""
import json
import csv
import os
import tempfile
from datetime import datetime

import numpy as np
import pandas as pd
from contextlib import contextmanager
import streamlit as st
import altair as alt

from auth_utils import require_auth
from data_catalog import DATA_CATALOG, SESSION_LABELS, COMPLETE_SESSIONS, EMBEDDING_MODEL
from predictions_loader import (
    join_predictions, PROB_COL, MOMENTUM_COL, PROB_BUCKETS, predictions_freshness
)
from histogram_filters import (likelihood_histogram_filter,
                                impact_histogram_filter, IMPACT_SCORE_OF)

require_auth()

# ── Accessibility chrome (ReQtest audit 2026-09-14) ──────────────────────────
# Issues 1/2 (main + nav landmarks), 16 (disabled-state contrast),
# 17/19 (keyboard focus on chart and dataframe overlay controls).
# Must run after require_auth() so it is not emitted on the login gate.
from a11y import apply_page_a11y
from chart_a11y import download_data_button, a11y_html_table
apply_page_a11y('Explorer')

# ── imports that may not be on every machine ─────────────────────────────────
# NOTE: these used to catch ImportError and silently set the flag False, which
# meant a broken dependency degraded to "search box does nothing" with no clue
# anywhere. Capture the real message and surface it.
_IMPORT_ERRORS: list[str] = []

try:
    import faiss
    _HAS_FAISS = True
except Exception as _e:                      # noqa: BLE001
    _HAS_FAISS = False
    _IMPORT_ERRORS.append(f"faiss: {type(_e).__name__}: {_e}")

try:
    from sentence_transformers import SentenceTransformer
    _HAS_ST = True
except Exception as _e:                      # noqa: BLE001
    _HAS_ST = False
    _IMPORT_ERRORS.append(f"sentence_transformers: {type(_e).__name__}: {_e}")

try:
    import requests as _requests
    _HAS_REQUESTS = True
except Exception as _e:                      # noqa: BLE001
    _HAS_REQUESTS = False
    _IMPORT_ERRORS.append(f"requests: {type(_e).__name__}: {_e}")

# ── Styles ────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
.block-container { padding-top:3rem; padding-bottom:2rem; padding-left:1.5rem; padding-right:1.5rem; }
.main-title, h1.main-title { font-size:2rem; font-weight:700;
                margin:0 0 .25rem; padding:0; line-height:1.25; }
.sub-title    { font-size:1rem; margin-bottom:1rem; color:#33404B; }
.section-title{ font-size:1.15rem; font-weight:700; margin-top:1rem; margin-bottom:.7rem; }
.kpi-card     { padding:1rem; border-radius:14px; border:1px solid rgba(128,128,128,.25);
                background:rgba(255,255,255,.04); margin-bottom:.75rem; }
.kpi-label    { font-size:.95rem; color:#454F5A; margin-bottom:.35rem; }
.kpi-value    { font-size:2rem; font-weight:700; line-height:1.1; }
.kpi-sub      { font-size:.75rem; color:#454F5A; margin-top:.2rem; }
div[data-testid="stDataFrame"] { border:1px solid rgba(128,128,128,.25); border-radius:12px; overflow:hidden; }
div.stButton > button { border-radius:10px; font-weight:600; }
</style>
""", unsafe_allow_html=True)

# ── Constants ─────────────────────────────────────────────────────────────────
FEEDBACK_CSV  = "impact_feedback.csv"
DEFAULT_TOP_K = 10
IMPACT_ORDER  = ["Not Impactful","Slightly Impactful","Moderately Impactful","Very Impactful"]
DATE_COL      = "status_date"
SUMMARY_COL   = "Llama Summary"
LINK_COL      = "ftp_url"

CATEGORY_COL    = "category_std"
SUBCATEGORY_COL = "subcategory_std"
POLICY_COL      = "policy_domain_final"

BENEF_PRIMARY_CAT = "beneficiary_primary_category"
BENEF_PRIMARY_SUB = "beneficiary_primary_subcategory"
BENEF_ALL_CATS    = "beneficiary_all_categories"
BENEF_ALL_SUBS    = "beneficiary_all_subcategories"
BENEF_CAT_LEGACY  = "intended_beneficiaries_category"
BENEF_SUB_LEGACY  = "intended_beneficiaries_subcategory"
BENEF_KW_COL      = "intended_beneficiaries_keyword"
BENEF_FINAL_COL   = "intended_beneficiaries_final"

IMPACT_COL_PREFERRED = "impact_rating_std"
IMPACT_COL_FALLBACK  = "Impact Rating"

STRATEGY_COL   = "legislative_strategy_std"
GOAL_COL       = "legislative_goal_std"
MOTIVATION_COL = "motivation_std"
INCREASING_COL = "increasing_aspects_std"
DECREASING_COL = "decreasing_aspects_std"

# Full original-LLaMA feature set for the search-result detail panel.
# Each entry: (candidate columns in preference order, display label). Raw model
# output is preferred; the *_std / display-named column is a fallback so the panel
# still renders if a session's metadata lacks a given raw column.
RAW_FEATURE_FIELDS = [
    (["llama_summary_raw", "Llama Summary"],                      "Summary"),
    (["legislative_goal_raw", "legislative_goal_std"],           "Legislative Goal"),
    (["key_provisions_raw", "key_provisions_std"],               "Key Provisions"),
    (["intent_raw", "intent_std"],                               "Intent"),
    (["motivation_raw", "motivation_std"],                       "Motivation"),
    (["legislative_strategy_raw", "legislative_strategy_std"],   "Legislative Strategy"),
    (["intended_beneficiaries_raw", "intended_beneficiaries_final"], "Intended Beneficiaries"),
    (["increasing_aspects_raw", "increasing_aspects_std"],       "Increasing Aspects"),
    (["decreasing_aspects_raw", "decreasing_aspects_std"],       "Decreasing Aspects"),
    (["potential_impact_raw", "Potential Impact"],               "Potential University of Illinois Impact"),
    (["policy_domain_raw", "policy_domain_final"],               "Policy Domain"),
    (["category_subcategory_raw"],                                "Category & Subcategory"),
    (["ideological_alignment_raw", "ideological_alignment"],     "Ideological Alignment"),
    (["original_law_raw", "original_law_std"],                   "Original Law (current statute)"),
]


def _first_present_value(row, candidates):
    """Return (value, column_used) for the first candidate column present and non-empty."""
    for c in candidates:
        if c in row.index:
            v = row.get(c)
            if pd.notna(v) and str(v).strip() and str(v).strip().lower() not in ("none", "n/a", "nan"):
                return str(v).strip(), c
    return None, None



def _resolve_benef_cat_col(meta: pd.DataFrame) -> str:
    if BENEF_PRIMARY_CAT in meta.columns and meta[BENEF_PRIMARY_CAT].notna().any():
        return BENEF_PRIMARY_CAT
    return BENEF_CAT_LEGACY

def _resolve_benef_sub_col(meta: pd.DataFrame) -> str:
    if BENEF_PRIMARY_SUB in meta.columns and meta[BENEF_PRIMARY_SUB].notna().any():
        return BENEF_PRIMARY_SUB
    return BENEF_SUB_LEGACY


@contextmanager
def _side_group(text: str, top: bool = False):
    """A sidebar section: its title and fields drawn inside one container.

    Bug 10 (QA, 2026-10-22): section titles had become plain bold text, so the
    only link between a title and its fields was visual. QA asked for a
    fieldset with the title as its legend. Streamlit draws its own markup, so
    each section is a container that the a11y runtime marks role="group",
    labelled by the title (aria-labelledby) — the ARIA equivalent: screen
    readers announce e.g. "Category & Subcategory, group" on entering.
    """
    with st.container():
        _side_title(text, top=top, group=True)
        yield


def _side_title(text: str, top: bool = False, group: bool = False) -> None:
    """Sidebar title styled like a heading but NOT a heading (Bug 10, Oct 7):
    the sidebar comes before the page H1, so headings here broke the order."""
    import html as _h
    size = "1.15rem" if top else "1rem"
    attr = ' data-a11y-group-title="1"' if group else ""
    st.markdown(f'<p{attr} style="font-weight:700;font-size:{size};color:#1A2733;'
                f'margin:{".2rem" if top else ".9rem"} 0 .3rem">{_h.escape(text)}</p>',
                unsafe_allow_html=True)


# ── Sidebar: session selector (logout is in the app.py router) ───────────────
with st.sidebar:
    # Bug 10: sidebar sections below are H3 sub-sections of this H2.
    _side_title("Filters", top=True)

    available_sessions = {
        sid: info["label"]
        for sid, info in DATA_CATALOG["Illinois"].items()
        if info.get("explorer") and info["explorer"].get("metadata") is not None
    }
    if not available_sessions:
        available_sessions = {
            sid: SESSION_LABELS.get(sid, str(sid))
            for sid in COMPLETE_SESSIONS
        }

    if len(available_sessions) > 1:
        selected_session_label = st.selectbox(
            "Session",
            options=list(available_sessions.values()),
            index=0,
        )
        selected_session = [k for k, v in available_sessions.items()
                            if v == selected_session_label][0]
    else:
        selected_session = list(available_sessions.keys())[0] if available_sessions else None


# ── Load vector DB from Azure blob ────────────────────────────────────────────
@st.cache_resource(show_spinner="Loading search index…")
def load_vector_db(session_id: int):
    if not _HAS_FAISS or not _HAS_ST or not _HAS_REQUESTS:
        return None, None, None, None, (
            "Search dependencies failed to import:\n\n- "
            + "\n- ".join(_IMPORT_ERRORS or ["(no detail captured)"]))

    session_info = DATA_CATALOG["Illinois"].get(session_id, {})
    explorer     = session_info.get("explorer", {})
    meta_url     = explorer.get("metadata")
    faiss_url    = explorer.get("faiss")
    cfg_url      = explorer.get("config")

    if not meta_url or not faiss_url or not cfg_url:
        return None, None, None, None, (
            f"Explorer artifacts for Session {session_id} ({SESSION_LABELS.get(session_id,'')}) "
            "are not yet configured in data_catalog.py. "
            "Generate SAS URLs in Azure portal after the pipeline chain completes, "
            "then paste them into EXPLORER_URLS in data_catalog.py."
        )

    try:
        cfg_resp = _requests.get(cfg_url, timeout=30)
        cfg_resp.raise_for_status()
        cfg = cfg_resp.json()

        meta = pd.read_parquet(meta_url)
        # The FAISS index stores vectors in vec_id order. Everything downstream
        # (index.search -> meta_df.loc[ids]) assumes row position == vec_id, so
        # make that true explicitly rather than hoping the parquet arrived
        # sorted. If the column is absent, row order IS the vec_id order.
        if "vec_id" in meta.columns:
            meta["vec_id"] = pd.to_numeric(meta["vec_id"], errors="coerce")
            meta = meta.sort_values("vec_id", kind="stable").reset_index(drop=True)
        else:
            meta = meta.reset_index(drop=True)
            meta["vec_id"] = meta.index

        faiss_resp = _requests.get(faiss_url, timeout=180)
        faiss_resp.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
            tmp.write(faiss_resp.content)
            tmp_path = tmp.name
        index = faiss.read_index(tmp_path)
        os.unlink(tmp_path)

        # A FAISS index and a metadata table that disagree on row count is the
        # single most likely reason semantic search returns nothing: FAISS hands
        # back ids that don't exist in meta, every hit is discarded, and the page
        # reports "No results" for every query. Fail loudly instead.
        if index.ntotal != len(meta):
            return None, None, None, None, (
                f"Explorer artifacts for Session {session_id} are out of sync: the FAISS "
                f"index holds {index.ntotal:,} vectors but metadata.parquet has "
                f"{len(meta):,} rows. Search is disabled until both are regenerated "
                f"together from the same gold snapshot."
            )

        model_name = cfg.get("embedding_model_name", EMBEDDING_MODEL)
        model = SentenceTransformer(model_name)

        return index, meta, model, cfg, None

    except Exception as e:
        return None, None, None, None, str(e)


if selected_session is None:
    st.warning("No sessions with configured explorer artifacts found. "
               "Please generate SAS URLs in data_catalog.py.")
    st.stop()

index, meta_df, embed_model, cfg, load_error = load_vector_db(selected_session)

if load_error:
    st.warning(f"{load_error}")   # st.warning supplies its own icon (audit bug 7)
    if meta_df is None:
        st.stop()

# ── Resolve column names ──────────────────────────────────────────────────────
IMPACT_COL = IMPACT_COL_PREFERRED if (meta_df is not None and IMPACT_COL_PREFERRED in meta_df.columns) else IMPACT_COL_FALLBACK
BENEF_CAT  = _resolve_benef_cat_col(meta_df) if meta_df is not None else BENEF_CAT_LEGACY
BENEF_SUB  = _resolve_benef_sub_col(meta_df) if meta_df is not None else BENEF_SUB_LEGACY

@st.cache_data(show_spinner=False, ttl=3600)
def _load_status_dates(session_id):
    """bill_id -> status_date from the gold features parquet (bug 18).

    Explorer's metadata.parquet has no status_date column at all, so the date
    filter could never work — Mark found it disabled on every session. Trends
    and Home read status_date from the gold parquet; join it from there.
    """
    from data_catalog import GOLD_URLS
    try:
        url = GOLD_URLS.get(int(session_id))
    except Exception:
        return None
    if not url:
        return None
    try:
        d = pd.read_parquet(url, columns=["bill_id", "status_date"])
    except Exception:
        return None
    d = d.dropna(subset=["bill_id"])
    d["_bid"] = pd.to_numeric(d["bill_id"], errors="coerce").astype("Int64").astype(str)
    return d.drop_duplicates("_bid", keep="last")[["_bid", "status_date"]]


if meta_df is not None:
    if DATE_COL not in meta_df.columns and "bill_id" in meta_df.columns:
        _sd = _load_status_dates(selected_session)
        if _sd is not None and len(_sd):
            meta_df = meta_df.copy()
            meta_df["_bid"] = pd.to_numeric(meta_df["bill_id"], errors="coerce").astype("Int64").astype(str)
            meta_df = meta_df.merge(_sd.rename(columns={"status_date": DATE_COL}), on="_bid", how="left").drop(columns=["_bid"])
    if DATE_COL in meta_df.columns:
        meta_df[DATE_COL] = pd.to_datetime(meta_df[DATE_COL], errors="coerce")
    if "impact_rating_score" not in meta_df.columns and IMPACT_COL in meta_df.columns:
        def _impact_to_score(x):
            if pd.isna(x): return np.nan
            s = str(x).strip().lower()
            # Handle various formats from standardizer
            if any(k in s for k in ["not impact","no impact","minimal"]): return 0
            if any(k in s for k in ["slight","low","minor"]): return 1
            if any(k in s for k in ["moderate","medium","moder"]): return 2
            if any(k in s for k in ["very impact","high impact","transform","major","significant"]): return 3
            # Numeric fallback
            try: return float(s)
            except: return np.nan
        meta_df["impact_rating_score"] = meta_df[IMPACT_COL].apply(_impact_to_score)

    # ── Join passage probability predictions ──────────────────────────────────
    meta_df = join_predictions(meta_df, session_ids=[selected_session])
    _pred_available = PROB_COL in meta_df.columns and meta_df[PROB_COL].notna().any()

    # ── Outcome overrides ─────────────────────────────────────────────────────
    # Once a bill has reached a late/known stage, the earlier survival-model
    # estimate is stale. Override the DISPLAYED reaches-vote likelihood and
    # momentum to reflect the known outcome instead of a mid-process guess.
    #   Enrolled/Passed/Enacted → reached a floor vote → 100%, momentum "Complete"
    # (Underlying model scores are untouched in the trail; this is display truth.)
    if "status_desc" in meta_df.columns:
        _sd = meta_df["status_desc"].astype(str).str.lower()
        _reached = _sd.str.contains("enrolled|passed|enacted|public act|chaptered|adopted",
                                    na=False, regex=True)
        if PROB_COL in meta_df.columns:
            meta_df.loc[_reached, PROB_COL] = 1.0
        if MOMENTUM_COL in meta_df.columns:
            meta_df[MOMENTUM_COL] = meta_df[MOMENTUM_COL].astype("object")
            meta_df.loc[_reached, MOMENTUM_COL] = "Complete"
            # Any remaining blank/None momentum on active bills → "Stable"
            _blank = meta_df[MOMENTUM_COL].isna() | \
                     meta_df[MOMENTUM_COL].astype(str).str.strip().isin(["", "None", "nan"])
            meta_df.loc[_blank & ~_reached, MOMENTUM_COL] = "Stable"
            meta_df.loc[_blank & _reached, MOMENTUM_COL] = "Complete"
else:
    _pred_available = False

# ── Default filters ───────────────────────────────────────────────────────────
# Audit follow-up (found during verification, not a ReQtest item):
# DATE_COL is parsed with errors="coerce" above, so any unparseable value
# becomes NaT. When the column is missing entirely, or every value fails to
# parse, BOTH bounds fell through to today's date -- producing a date filter
# whose entire selectable range was a single day. The filter looked normal and
# did nothing. Track that case explicitly so the UI can say so instead of
# presenting a broken control.
_dates_usable = (
    meta_df is not None
    and DATE_COL in meta_df.columns
    and pd.notna(meta_df[DATE_COL].min())
    and pd.notna(meta_df[DATE_COL].max())
    and meta_df[DATE_COL].min().date() != meta_df[DATE_COL].max().date()
)
if _dates_usable:
    _date_min = meta_df[DATE_COL].min().date()
    _date_max = meta_df[DATE_COL].max().date()
else:
    _date_min = _date_max = datetime.utcnow().date()

# Why it is unusable, for the message shown next to the disabled control.
if _dates_usable:
    _dates_problem = ""
elif meta_df is None or DATE_COL not in (meta_df.columns if meta_df is not None else []):
    _dates_problem = f"this session's metadata has no '{DATE_COL}' column"
elif meta_df[DATE_COL].notna().sum() == 0:
    _dates_problem = f"no bill in this session has a readable '{DATE_COL}' value"
else:
    _dates_problem = f"every bill in this session shares the same '{DATE_COL}'"

DEFAULT_FILTERS = {
    "categories": [], "subcategories": [],
    "beneficiary_categories": [], "beneficiary_subcategories": [],
    "benef_touches_categories": [],
    "policy_domains": [], "impact_selected": [], "bill_statuses": [],
    "date_range": (_date_min, _date_max),
    "num_search_results": DEFAULT_TOP_K,
    "min_passage_prob_label": "Any",    # passage likelihood filter
    "momentum_filter": [],               # momentum filter
    "my_categories_filter": [],          # user's saved interest categories
    "my_impact_prompt": "(none)",        # user's saved impact prompt
    "my_impact_levels": [],              # impact levels for selected prompt
}
for key, value in DEFAULT_FILTERS.items():
    if key not in st.session_state:
        st.session_state[key] = value
if "search_results" not in st.session_state:
    st.session_state.search_results = None
if "current_query"  not in st.session_state:
    st.session_state.current_query = ""
if "history" not in st.session_state:
    st.session_state.history = []


# ── Sidebar filter controls ────────────────────────────────────────────────────
def _get_opts(col):
    if meta_df is None or col not in meta_df.columns: return []
    return sorted(v for v in meta_df[col].dropna().astype(str).str.strip().unique() if v)

def _get_dep_opts(df, parent_col, child_col, selected_parents):
    if df is None or child_col not in df.columns: return []
    sub_df = df if not selected_parents else df[df[parent_col].isin(selected_parents)]
    return sorted(v for v in sub_df[child_col].dropna().astype(str).str.strip().unique() if v)

def _parse_all_cats(val):
    try:
        parsed = json.loads(val) if val else []
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []

with st.sidebar:
    if st.button("Reset Filters", use_container_width=True):
        for k, v in DEFAULT_FILTERS.items():
            st.session_state[k] = v
        st.session_state.search_results = None
        st.session_state.current_query  = ""
        st.rerun()

    st.caption("Filters apply to the bill table and search results.")

    # ── My Categories (user's saved interest categories from My Tracker) ─────
    _user_rec = st.session_state.get("user_record") or {}
    _uid = _user_rec.get("email", "")
    _my_cats, _my_prompts = [], []
    if _uid:
        try:
            from user_profile import list_saved_items
            _my_cats    = list_saved_items(_uid, "category")
            _my_prompts = list_saved_items(_uid, "impact_prompt")
        except Exception:
            pass

    if _my_cats:
        # Audit bug 7: emoji inside a markdown heading is part of the heading
        # text, so it was announced with the heading.
        with _side_group("My Categories"):
            _cat_labels = [c.get("label", "(unnamed)") for c in _my_cats]
            st.multiselect(
                "Filter by my saved categories", options=_cat_labels,
                key="my_categories_filter",
                help="Your interest categories from My Tracker — matches the same "
                     "keywords against the same fields.",
            )

    if _my_prompts:
        with _side_group("My Impact Prompts"):
            _p_labels = ["(none)"] + [p.get("label", "(unnamed)") for p in _my_prompts]
            st.selectbox("Impact prompt", _p_labels, key="my_impact_prompt")
            if st.session_state.get("my_impact_prompt", "(none)") != "(none)":
                st.multiselect(
                    "Impact level", ["High Impact", "Moderate Impact",
                                     "Low Impact", "No Impact"],
                    key="my_impact_levels",
                    help="Show only bills the AI rated at these levels for your "
                         "selected prompt. Scored nightly.",
                )

    with _side_group("Category & Subcategory"):
        has_cat_cols = meta_df is not None and CATEGORY_COL in meta_df.columns and SUBCATEGORY_COL in meta_df.columns
        if has_cat_cols:
            cat_opts = _get_opts(CATEGORY_COL)
            st.multiselect("Category", options=cat_opts, key="categories")
            sub_opts = _get_dep_opts(meta_df, CATEGORY_COL, SUBCATEGORY_COL, st.session_state.categories)
            valid_subs = [x for x in st.session_state.get("subcategories",[]) if x in sub_opts]
            if valid_subs != st.session_state.get("subcategories",[]):
                st.session_state["subcategories"] = valid_subs
            st.multiselect("Subcategory", options=sub_opts, key="subcategories")
        else:
            st.info("category_std / subcategory_std not present in this session's metadata.")

    with _side_group("Intended Beneficiaries"):
        benef_cat_opts = _get_opts(BENEF_CAT)
        st.multiselect("Beneficiary Category (primary)", options=benef_cat_opts, key="beneficiary_categories")
        benef_sub_opts = _get_dep_opts(meta_df, BENEF_CAT, BENEF_SUB, st.session_state.beneficiary_categories)
        valid_bsubs = [x for x in st.session_state.get("beneficiary_subcategories",[]) if x in benef_sub_opts]
        if valid_bsubs != st.session_state.get("beneficiary_subcategories",[]):
            st.session_state["beneficiary_subcategories"] = valid_bsubs
        st.multiselect("Beneficiary Subcategory (primary)", options=benef_sub_opts, key="beneficiary_subcategories")

        if meta_df is not None and BENEF_ALL_CATS in meta_df.columns:
            all_cats_flat = sorted(set(
                cat
                for val in meta_df[BENEF_ALL_CATS].dropna()
                for cat in _parse_all_cats(val)
                if cat and cat not in ("none", "uncategorized")
            ))
            if all_cats_flat:
                st.multiselect(
                    "Bill touches ANY of these categories",
                    options=all_cats_flat,
                    key="benef_touches_categories",
                    help="Shows bills where ANY of their beneficiary categories matches.",
                )

    with _side_group("Policy & Status"):
        st.multiselect("Policy Area",   options=_get_opts(POLICY_COL),   key="policy_domains")
        st.multiselect("Bill Status",   options=_get_opts("status_desc"), key="bill_statuses")

        # ── Impact filter (histogram) ─────────────────────────────────────────────
    with _side_group("Impact"):
        st.session_state["impact_hist_levels"] = impact_histogram_filter(
            meta_df, "impact_rating_score", "explorer_impact", label="Impact level")

        # ── Reaches-Vote Likelihood filter (histogram range) ──────────────────────
    with _side_group("Reaches-Vote Likelihood"):
        if _pred_available:
            st.session_state["rvl_range"] = likelihood_histogram_filter(
                meta_df, PROB_COL, "explorer_rvl")
            st.multiselect(
                "Momentum",
                options=["Gaining", "Stable", "Losing", "Complete"],
                key="momentum_filter",
                help="Gaining = accelerating through chamber stages; Losing = stalling or re-referred; "
                     "Complete = already reached a floor vote.",
            )
            st.caption(predictions_freshness((selected_session,)))
        else:
            st.caption("Predictions not yet available for this session.")

    with _side_group("Time Filter"):
        # Reconcile any stored range with THIS session's bounds before rendering.
        # Switching sessions can leave a date_range that falls outside the new
        # session's min/max (e.g. a session whose metadata has no dates -> min==max),
        # which would make st.date_input raise. Reset to the full span when invalid.
        def _range_stale(dr):
            """True only for values that don't fit THIS session's dates.

            Bug 18 (Mark's original video): the old check treated an EMPTY box as
            invalid and wrote the full range back, so clearing the field made it
            refill itself. Empty now means "no date filter" and stays empty; a
            half-entered range (start date only) is left alone too.
            """
            if dr is None or (isinstance(dr, (tuple, list)) and len(dr) == 0):
                return False
            vals = dr if isinstance(dr, (tuple, list)) else (dr,)
            try:
                return not all(_date_min <= d <= _date_max for d in vals)
            except TypeError:
                return True
        if _range_stale(st.session_state.get("date_range")):
            st.session_state["date_range"] = (_date_min, _date_max)
        # Audit issue 18 (WCAG 3.3.2): the accepted format and range were never
        # shown. An out-of-range value was also silently reset by _range_ok above
        # with no message, so the user had no way to know what went wrong.
        if _dates_usable:
            st.date_input(
                "Status Date Range",
                min_value=_date_min, max_value=_date_max, key="date_range",
                format="YYYY/MM/DD",
                help=(f"Format YYYY/MM/DD. Pick any range between "
                      f"{_date_min:%Y/%m/%d} and {_date_max:%Y/%m/%d}. "
                      f"Dates outside that span reset to the full range."),
            )
            st.caption(f"Format YYYY/MM/DD · available range "
                       f"{_date_min:%Y/%m/%d} to {_date_max:%Y/%m/%d} · "
                       f"clear the box to include all dates")
        else:
            # Disabled rather than hidden: a control that vanishes is as confusing
            # as one that silently does nothing. The disabled styling in a11y.py
            # (audit issue 16) makes the state discernible, and the reason is
            # stated in text rather than only in a hover tooltip.
            st.date_input("Status Date Range", value=None, disabled=True,
                          key="date_range_disabled", format="YYYY/MM/DD")
            st.caption(f"Date filtering is unavailable for this session — "
                       f"{_dates_problem}.")

    if os.path.exists(FEEDBACK_CSV):
        try:
            df_fb = pd.read_csv(FEEDBACK_CSV)
            st.info(f"Feedback records: {len(df_fb)}")
            with open(FEEDBACK_CSV, "rb") as f:
                st.download_button("Download impact_feedback.csv", f.read(),
                                   "impact_feedback.csv", "text/csv")
        except Exception:
            pass

    with st.expander("Search History"):
        for i, item in enumerate(reversed(st.session_state.history[-5:]), 1):
            st.write(f"{i}. {item.get('query','')}")


# ── Build filter mask ─────────────────────────────────────────────────────────
def build_filter_mask(df):
    if df is None: return pd.Series(dtype=bool)
    mask = pd.Series(True, index=df.index)

    if st.session_state.categories and CATEGORY_COL in df.columns:
        mask &= df[CATEGORY_COL].isin(st.session_state.categories)
    if st.session_state.subcategories and SUBCATEGORY_COL in df.columns:
        mask &= df[SUBCATEGORY_COL].isin(st.session_state.subcategories)

    if st.session_state.beneficiary_categories and BENEF_CAT in df.columns:
        mask &= df[BENEF_CAT].isin(st.session_state.beneficiary_categories)
    if st.session_state.beneficiary_subcategories and BENEF_SUB in df.columns:
        mask &= df[BENEF_SUB].isin(st.session_state.beneficiary_subcategories)

    touches = st.session_state.get("benef_touches_categories", [])
    if touches and BENEF_ALL_CATS in df.columns:
        def _touches_any(val):
            return any(c in touches for c in _parse_all_cats(val))
        mask &= df[BENEF_ALL_CATS].fillna("[]").apply(_touches_any)

    if st.session_state.policy_domains and POLICY_COL in df.columns:
        mask &= df[POLICY_COL].isin(st.session_state.policy_domains)
    _imp_levels = st.session_state.get("explorer_impact_levels", [])
    if _imp_levels and "impact_rating_score" in df.columns:
        from histogram_filters import IMPACT_SCORE_OF
        _want_scores = {IMPACT_SCORE_OF[l] for l in _imp_levels}
        _sc = pd.to_numeric(df["impact_rating_score"], errors="coerce").round()
        mask &= _sc.isin(_want_scores)
    if st.session_state.bill_statuses and "status_desc" in df.columns:
        mask &= df["status_desc"].isin(st.session_state.bill_statuses)

    # Only apply the date filter when the control was actually offered. When
    # the range collapsed to a single day the stored value is (today, today),
    # which would silently filter everything out even though the user never
    # touched the control.
    dr = st.session_state.get("date_range") if _dates_usable else None
    if dr and DATE_COL in df.columns and df[DATE_COL].notna().any():
        if isinstance(dr, (tuple, list)):
            # 2 dates = a range; 1 date = start picked, end not yet (open-ended).
            start, end = (dr[0], dr[1]) if len(dr) >= 2 else (dr[0], _date_max)
        else:
            start, end = dr, dr
        mask &= df[DATE_COL].between(pd.to_datetime(start), pd.to_datetime(end))

    # ── Reaches-vote likelihood range filter (histogram) ──────────────────────
    _rvl = st.session_state.get("explorer_rvl_range", (0, 100))
    if _rvl != (0, 100) and PROB_COL in df.columns:
        _lo, _hi = _rvl
        _pp = pd.to_numeric(df[PROB_COL], errors="coerce").fillna(0) * 100
        mask &= (_pp >= _lo) & (_pp <= _hi)

    # ── Momentum filter ───────────────────────────────────────────────────────
    mom_filter = st.session_state.get("momentum_filter", [])
    if mom_filter and MOMENTUM_COL in df.columns:
        mask &= df[MOMENTUM_COL].isin(mom_filter)

    # ── My Categories filter (saved interest categories from My Tracker) ─────
    _sel_cats = st.session_state.get("my_categories_filter", [])
    if _sel_cats:
        _user_rec = st.session_state.get("user_record") or {}
        _uid = _user_rec.get("email", "")
        _cat_mask = pd.Series(False, index=df.index)
        try:
            from user_profile import list_saved_items
            for _c in list_saved_items(_uid, "category"):
                if _c.get("label", "") not in _sel_cats:
                    continue
                _d   = _c.get("data", {})
                _kws = _d.get("keywords", [])
                _fls = _d.get("fields", [])
                if _d.get("type") == "keyword" and _kws and _fls:
                    for _kw in _kws:
                        for _f in _fls:
                            if _f in df.columns:
                                _cat_mask |= (df[_f].astype(str).str.lower()
                                              .str.contains(_kw.lower(), na=False))
                else:
                    _cm  = {"policy_domain": "policy_domain_standardized",
                            "beneficiary":   "beneficiary_primary_category"}
                    _col = _cm.get(_d.get("type", ""), "category_main_label")
                    if _col in df.columns:
                        _cat_mask |= df[_col].astype(str).str.contains(
                            str(_d.get("value", "")), case=False, na=False)
        except Exception:
            _cat_mask = pd.Series(True, index=df.index)
        mask &= _cat_mask

    # ── My Impact Prompt filter ───────────────────────────────────────────────
    _prompt_lbl = st.session_state.get("my_impact_prompt", "(none)")
    _levels     = st.session_state.get("my_impact_levels", [])
    if _prompt_lbl != "(none)" and _levels:
        _user_rec = st.session_state.get("user_record") or {}
        _uid = _user_rec.get("email", "")
        _ratings = {}
        try:
            from impact_loader import impact_map_by_label
            _ratings = impact_map_by_label(_uid, _prompt_lbl)
        except Exception:
            _ratings = {}
        if _ratings:
            mask &= df["bill_id"].astype(str).map(
                lambda b: _ratings.get(b, "") in _levels)

    return mask


if meta_df is None:
    st.error("Metadata not available. Check data_catalog.py explorer URLs.")
    st.stop()

filtered_df = meta_df[build_filter_mask(meta_df)].copy()

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_search = st.container()  # Explorer is search-only; aggregate charts live on Trends page


# ═══════════════════════════════════════════════════════════════════════════════
# SEARCH TAB
# ═══════════════════════════════════════════════════════════════════════════════
with tab_search:
    # Audit issue 10: this page's only title was a styled <div>, so the page had
    # no <h1> and its section headings started at level 3 with nothing above
    # them. Promoted in place rather than adding a second title.
    st.markdown('<h1 class="main-title">IGPA Legislation Explorer</h1>',
                unsafe_allow_html=True)
    st.markdown(
        f'<div class="sub-title">'
        f'Session: {SESSION_LABELS.get(selected_session, selected_session)} · '
        f'Semantic search + filters across {len(meta_df):,} bills.</div>',
        unsafe_allow_html=True)

    benef_col_display = BENEF_FINAL_COL if BENEF_FINAL_COL in filtered_df.columns else BENEF_CAT

    # Build KPI cards. Impact tiers are dynamic: only tiers with a non-zero count
    # are shown (so an empty "Very Impactful" card never sits there reading 0).
    kpi_cards = [("Total Bills", f"{len(filtered_df):,}", "")]
    pd_count = filtered_df[POLICY_COL].nunique() if POLICY_COL in filtered_df.columns else 0
    kpi_cards.append(("Policy Domains", f"{pd_count}", ""))
    ben_count = filtered_df[benef_col_display].nunique() if benef_col_display in filtered_df.columns else 0
    kpi_cards.append(("Beneficiary Groups", f"{ben_count}", ""))

    if IMPACT_COL in filtered_df.columns:
        for _tier, _short in [("Slightly Impactful", "Slightly Impactful"),
                              ("Moderately Impactful", "Moderately Impactful"),
                              ("Very Impactful", "Very Impactful")]:
            _cnt = int((filtered_df[IMPACT_COL] == _tier).sum())
            if _cnt > 0:
                kpi_cards.append((_short, f"{_cnt:,}", ""))

    if _pred_available and PROB_COL in filtered_df.columns:
        high_lik = int((filtered_df[PROB_COL].fillna(0) >= 0.65).sum())
        _sub = ""
        if IMPACT_COL in filtered_df.columns:
            both = int(((filtered_df[PROB_COL].fillna(0) >= 0.65) &
                        (filtered_df[IMPACT_COL] == "Very Impactful")).sum())
            if both > 0:
                _sub = f"{both} also Very Impactful"
        kpi_cards.append(("Reaches a vote (65% or higher)", f"{high_lik:,}", _sub))
    else:
        kpi_cards.append(("Reaches a vote", "—", "predictions loading"))

    for _c, (_label, _value, _sub) in zip(st.columns(len(kpi_cards)), kpi_cards):
        # Bug 13 (round 3): one spoken phrase per card. Round 2's <dl> added
        # list enter/exit noise; the visual parts are now hidden instead.
        _spoken = f"{_label}: {_value}" + (f", {_sub}" if _sub else "")
        _sub_html = f'<span aria-hidden="true" class="kpi-sub" style="display:block">{_sub}</span>' if _sub else ""
        _c.markdown(f'<div class="kpi-card kpi-read"><span class="a11y-sr">{_spoken}</span>'
                    f'<span aria-hidden="true" class="kpi-label" style="display:block">{_label}</span>'
                    f'<span aria-hidden="true" class="kpi-value" style="display:block">{_value}</span>{_sub_html}</div>',
                    unsafe_allow_html=True)

    # NOTE: "Most Impacted Beneficiary Keywords" chart was removed from the Explorer.
    # Its averaging surfaced single-bill beneficiaries (all reading 3.0) and it's a
    # macro/global view, not a per-session search aid. It is being rebuilt — with a
    # minimum-bill threshold so the bars actually vary — on the Trends macro page.



    # Filter bill table
    st.markdown('<div class="section-title">Bills Matching Selected Filters</div>', unsafe_allow_html=True)

    # ── Impact-column swap: if the user is viewing one of THEIR impact prompts,
    #    show THAT rating in the table instead of the University-wide score. ──
    _disp_df = filtered_df.copy()
    _impact_display_label = "Impact Rating"
    _my_prompt_sel = st.session_state.get("my_impact_prompt", "(none)")
    if _my_prompt_sel != "(none)":
        try:
            from impact_loader import impact_map_by_label
            _urec = st.session_state.get("user_record") or {}
            _uid2 = _urec.get("email", "")
            _rmap = impact_map_by_label(_uid2, _my_prompt_sel)
            if _rmap:
                _disp_df[IMPACT_COL] = _disp_df["bill_id"].astype(str).map(_rmap)
                _impact_display_label = f"Impact: {_my_prompt_sel}"
        except Exception:
            pass

    # ── Sort control ──────────────────────────────────────────────────────────
    _SORT_OPTS = {
        "Reaches-vote likelihood (high to low)": (PROB_COL, False),
        "Reaches-vote likelihood (low to high)": (PROB_COL, True),
        "Impact (high to low)": ("impact_rating_score", False),
        "Impact (low to high)": ("impact_rating_score", True),
        "Momentum": (MOMENTUM_COL, True),
        "Bill status": ("_status_order", True),
        "Bill number": ("bill_number", True),
    }
    _sort_choice = st.selectbox("Sort by", list(_SORT_OPTS.keys()),
                                key="explorer_sort")
    _scol, _asc = _SORT_OPTS[_sort_choice]
    if _scol == "_status_order":
        _STATUS_RANK = {"Introduced": 0, "Assigned": 1, "Committee": 2,
                        "Engrossed": 3, "Enrolled": 4, "Passed": 5,
                        "Vetoed": 6, "Failed": 7, "Complete": 4}
        if "status_desc" in _disp_df.columns:
            _disp_df["_status_order"] = _disp_df["status_desc"].astype(str).map(
                lambda s: _STATUS_RANK.get(s, 99))
            _disp_df = _disp_df.sort_values("_status_order", ascending=_asc,
                                            na_position="last").drop(columns=["_status_order"])
    elif _scol in _disp_df.columns:
        _disp_df = _disp_df.sort_values(_scol, ascending=_asc, na_position="last")

    display_cols = {
        "bill_number":   "Bill Number",
        "title":         "Title",
        CATEGORY_COL:    "Category",
        SUBCATEGORY_COL: "Subcategory",
        BENEF_CAT:       "Beneficiary Category",
        BENEF_SUB:       "Beneficiary Subcategory",
        BENEF_ALL_CATS:  "All Beneficiary Categories",
        PROB_COL:        "Reaches-Vote Likelihood",
        MOMENTUM_COL:    "Momentum",
        SUMMARY_COL:     "Llama Summary",
        IMPACT_COL:      "Impact Rating",
        "status_desc":   "Status",
        LINK_COL:        "Bill Link",
    }
    avail = [c for c in display_cols if c in _disp_df.columns]
    filter_bill_df = _disp_df[avail].rename(columns=display_cols).copy()
    # dynamic impact header when viewing a custom impact
    if _impact_display_label != "Impact Rating" and "Impact Rating" in filter_bill_df.columns:
        filter_bill_df = filter_bill_df.rename(columns={"Impact Rating": _impact_display_label})

    col_config = {}
    if "Bill Link" in filter_bill_df.columns:
        col_config["Bill Link"] = st.column_config.LinkColumn("Bill Link", display_text="Open")
    if "Reaches-Vote Likelihood" in filter_bill_df.columns:
        # Stored 0–1; scale to 0–100 so the ProgressColumn % label and bar fill render correctly.
        filter_bill_df["Reaches-Vote Likelihood"] = (
            pd.to_numeric(filter_bill_df["Reaches-Vote Likelihood"], errors="coerce").fillna(0) * 100
        )
        col_config["Reaches-Vote Likelihood"] = st.column_config.ProgressColumn(
            "Reaches-Vote Likelihood",
            help="Stage 2 model: probability of reaching a floor vote (AUC 0.93). "
                 "Bills already enrolled/passed are shown at 100%.",
            format="%.0f%%",
            min_value=0,
            max_value=100,
        )

    # Bug 15: real HTML table (keyboard focus visible, links named per bill).
    a11y_html_table(filter_bill_df, "Bills matching selected filters", key="explorer_bills",
                    link_cols=("Bill Link",), pct_cols=("Reaches-Vote Likelihood",))
    download_data_button(filter_bill_df, "Bills matching selected filters")   # Bug 17: Excel

    # Search
    st.markdown('<div class="section-title">Semantic Search</div>', unsafe_allow_html=True)
    search_col, btn_col, k_col = st.columns([5, 1, 1])
    with search_col:
        query = st.text_input("Ask a question about legislation",
                              value=st.session_state.current_query,
                              placeholder="e.g. healthcare funding for veterans, education workforce policy")
    with btn_col:
        st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        search_clicked = st.button("Search", use_container_width=True)
    with k_col:
        st.slider("Results", 5, 50, step=5, key="num_search_results")

    if index is not None:
        st.caption(f"Index: {index.ntotal:,} vectors · Metadata: {len(meta_df):,} rows")

    if index is None:
        st.info("Search index not loaded — metadata-only view active. Configure SAS URLs to enable search.")
    elif search_clicked and query.strip():
        st.session_state.current_query = query
        st.session_state.history.append({"query": query})
        q_vec = embed_model.encode([query], normalize_embeddings=True, convert_to_numpy=True).astype("float32")
        top_k   = st.session_state.num_search_results
        n_search = max(1, min(index.ntotal, top_k * 5))
        scores, ids = index.search(q_vec, n_search)
        ids, scores = ids[0], scores[0]
        # FAISS pads with -1 when it has fewer than n_search vectors. Row position
        # == vec_id (enforced in load_vector_db), so an id is a valid row label.
        _n_rows = len(meta_df)
        _hits   = [(int(i), float(s)) for i, s in zip(ids, scores)
                   if 0 <= int(i) < _n_rows]
        allowed = set(filtered_df.index)
        kept    = [(i, s) for i, s in _hits if i in allowed][:top_k]
        if not kept:
            if not _hits:
                st.error(
                    f"Search returned {len(ids)} vector ids, none of which map to a row "
                    f"in metadata ({_n_rows:,} rows). The FAISS index and metadata.parquet "
                    f"are mismatched — regenerate both together.")
            else:
                st.warning(
                    f"No results for this query under the current filters. "
                    f"({len(_hits)} semantic matches were found, but all were excluded by "
                    f"your sidebar filters — try Reset Filters.)")
            st.session_state.search_results = None
        else:
            results = meta_df.loc[[i for i, _ in kept]].copy()
            results["similarity"] = [s for _, s in kept]
            st.session_state.search_results = results

    if st.session_state.search_results is not None:
        results = st.session_state.search_results
        st.markdown('<div class="section-title">Search Results</div>', unsafe_allow_html=True)
        review_cols = [
            "bill_number", "title", CATEGORY_COL, SUBCATEGORY_COL,
            BENEF_CAT, BENEF_SUB, BENEF_ALL_CATS,
            PROB_COL, MOMENTUM_COL,
            SUMMARY_COL, GOAL_COL, INCREASING_COL, DECREASING_COL,
            IMPACT_COL, "similarity", LINK_COL,
        ]
        review_df = results[[c for c in review_cols if c in results.columns]].copy()
        review_df.rename(columns={
            "bill_number":   "Bill #",
            "title":         "Title",
            CATEGORY_COL:    "Category",
            SUBCATEGORY_COL: "Subcategory",
            BENEF_CAT:       "Beneficiary Category",
            BENEF_SUB:       "Beneficiary Subcategory",
            BENEF_ALL_CATS:  "All Beneficiary Categories",
            PROB_COL:        "Reaches-Vote Likelihood",
            MOMENTUM_COL:    "Momentum",
            SUMMARY_COL:     "LLaMA Summary",
            GOAL_COL:        "Legislative Goal",
            INCREASING_COL:  "Increasing Aspects",
            DECREASING_COL:  "Decreasing Aspects",
            IMPACT_COL:      "Impact Rating",
            "similarity":    "Similarity",
            LINK_COL:        "Bill URL",
        }, inplace=True)

        review_col_config = {}
        if "Bill URL" in review_df.columns:
            review_col_config["Bill URL"] = st.column_config.LinkColumn("Bill URL", display_text="Open")
        if "Reaches-Vote Likelihood" in review_df.columns:
            review_df["Reaches-Vote Likelihood"] = (
                pd.to_numeric(review_df["Reaches-Vote Likelihood"], errors="coerce").fillna(0) * 100
            )
            review_col_config["Reaches-Vote Likelihood"] = st.column_config.ProgressColumn(
                "Reaches-Vote Likelihood", format="%.0f%%", min_value=0, max_value=100,
            )

        a11y_html_table(review_df, "Semantic search results", key="explorer_search",
                        link_cols=("Bill URL",), pct_cols=("Reaches-Vote Likelihood",), page_size=50)
        download_data_button(review_df, "Semantic search results")   # Bug 17: Excel
        st.markdown("---")
        st.markdown('<div class="section-title">Top Matching Bills</div>', unsafe_allow_html=True)
        for idx_row, row in results.iterrows():
            # Show pass probability badge inline with bill number
            prob_badge = ""
            if PROB_COL in row.index and pd.notna(row.get(PROB_COL)):
                p = float(row[PROB_COL])
                color = "#3B6D11" if p >= 0.65 else ("#BA7517" if p >= 0.35 else "#999")
                prob_badge = f' <span style="font-size:11px;color:{color};font-weight:600">[{p:.0%} reaches-vote]</span>'
            st.markdown(f"**{row.get('bill_number','N/A')}** — {row.get('title','N/A')}{prob_badge}",
                        unsafe_allow_html=True)
            if pd.notna(row.get(LINK_COL)):
                st.markdown(f"[Open Bill]({row.get(LINK_COL)})")

            with st.expander("Full LLaMA Analysis — all features as generated"):
                # Status / impact / prediction line first (not LLaMA features, but useful)
                meta_bits = []
                if "status_desc" in results.columns and pd.notna(row.get("status_desc")):
                    meta_bits.append(f"**Status:** {row.get('status_desc')}")
                if IMPACT_COL in results.columns and pd.notna(row.get(IMPACT_COL)):
                    meta_bits.append(f"**Impact Rating:** {row.get(IMPACT_COL)}")
                if PROB_COL in results.columns and pd.notna(row.get(PROB_COL)):
                    meta_bits.append(f"**Reaches-Vote Likelihood:** {float(row.get(PROB_COL)):.1%}")
                if MOMENTUM_COL in results.columns and pd.notna(row.get(MOMENTUM_COL)):
                    meta_bits.append(f"**Momentum:** {row.get(MOMENTUM_COL)}")
                if meta_bits:
                    st.markdown("  ·  ".join(meta_bits))
                    st.markdown("")
                # Every raw feature the model produced
                shown = 0
                for candidates, label in RAW_FEATURE_FIELDS:
                    val, _col = _first_present_value(row, candidates)
                    if val:
                        st.markdown(f"**{label}**")
                        st.write(val)
                        shown += 1
                if shown == 0:
                    st.caption("No LLaMA feature columns are present in this session's metadata.")
            with st.expander("Impact Rating Feedback", expanded=False):
                predicted = row.get(IMPACT_COL,"")
                bid = str(row.get("bill_id", idx_row))
                if st.session_state.get(f"fb_done_{bid}", False):
                    st.success("Feedback saved.")
                else:
                    c_yes, c_no = st.columns(2)
                    with c_yes:
                        if st.button("Accurate", key=f"yes_{bid}", use_container_width=True):
                            try:
                                exists = os.path.isfile(FEEDBACK_CSV)
                                with open(FEEDBACK_CSV,"a",newline="",encoding="utf-8") as f:
                                    w = csv.writer(f)
                                    if not exists: w.writerow(["timestamp","bill_id","predicted","response","corrected","comment"])
                                    w.writerow([datetime.utcnow().isoformat(), bid, predicted, "Yes","",""])
                                st.session_state[f"fb_done_{bid}"] = True
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))
                    with c_no:
                        if st.button("Incorrect", key=f"no_{bid}", use_container_width=True):
                            st.session_state[f"show_corr_{bid}"] = True
                            st.rerun()
                    if st.session_state.get(f"show_corr_{bid}", False):
                        corrected = st.selectbox("Correct rating:", IMPACT_ORDER, key=f"corr_{bid}")
                        comment   = st.text_area("Note (optional):", max_chars=250, key=f"cmt_{bid}")
                        cs, cc = st.columns([3,1])
                        with cs:
                            if st.button("Submit", key=f"sub_{bid}", type="primary"):
                                try:
                                    exists = os.path.isfile(FEEDBACK_CSV)
                                    with open(FEEDBACK_CSV,"a",newline="",encoding="utf-8") as f:
                                        w = csv.writer(f)
                                        if not exists: w.writerow(["timestamp","bill_id","predicted","response","corrected","comment"])
                                        w.writerow([datetime.utcnow().isoformat(), bid, predicted, "No", corrected, comment])
                                    st.session_state[f"fb_done_{bid}"] = True
                                    st.session_state[f"show_corr_{bid}"] = False
                                    st.rerun()
                                except Exception as e:
                                    st.error(str(e))
                        with cc:
                            if st.button("Cancel", key=f"can_{bid}"):
                                st.session_state[f"show_corr_{bid}"] = False
                                st.rerun()
            st.markdown("---")


