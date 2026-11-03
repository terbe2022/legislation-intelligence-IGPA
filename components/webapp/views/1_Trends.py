"""
pages/1_📊_Trends.py  — Legislative Trends Dashboard
=======================================================
§8 Fixes applied vs app_trends_v2.py:
  1. Heatmap: filter null/"missing text"/"no beneficiaries" strings (was drowning signal)
  2. Policy Direction tooltips: same null-filter applied to top_beneficiaries_for_period
  3. KPI cards: removed meaningless House/Senate % cards; replaced with
     Avg Impact Score + Sessions Loaded
  4. ±2 SD band: added SD multiplier selector (1 / 1.5 / 2)
  5. Category Ranking: color-encoded by avg impact_rating_score (not just bill count)
  6. LLM briefing panel: Claude API generates plain-English summary for selected category
  7. Beneficiary taxonomy columns: uses beneficiary_primary_category when present
     (falls back to raw intended_beneficiaries_standardized)
  8. Regression note: tooltip clarifies regression is over full history, display is windowed
"""
import os
import re
import html
import json

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components
from scipy import stats
import time as _time
import ai_briefing

from auth_utils import require_auth
# Audit issue 15: inject_sr_css was missing from this import and never
# called, so the .sr-only rule was absent on this page and all 13 chart
# descriptions were rendering as visible text instead of being hidden.
from chart_a11y import chart_aux, sr_text, inject_sr_css, plotly_chart, a11y_table


def _wordcloud_lib() -> str:
    """wordcloud2.js, bundled in vendor/ so it can't be blocked (see Top Keywords)."""
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "..", "vendor", "wordcloud2.js"),
              os.path.join(os.getcwd(), "vendor", "wordcloud2.js")):
        if os.path.exists(p):
            return open(p, encoding="utf-8").read().replace("</script>", "<\\/script>")
    return ""
from data_catalog import ACTIVE_SESSIONS, GOLD_URLS, SESSION_LABELS, PREDICTION_URLS

require_auth()

# ── Accessibility chrome (ReQtest audit 2026-09-14) ──────────────────────────
# Issues 1/2 (main + nav landmarks), 16 (disabled-state contrast),
# 17/19 (keyboard focus on chart and dataframe overlay controls).
# Must run after require_auth() so it is not emitted on the login gate.
from a11y import apply_page_a11y
apply_page_a11y('Trends')
inject_sr_css()   # audit issue 15 — see the import note above

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    _HAS_SK = True
except Exception:
    _HAS_SK = False

# ── Colours — sourced from the shared accessible theme ───────────────────────
from app_theme import (
    C_DARK as C_TRAPPED_DARKNESS,
    C_SAND as C_CEDAR_PLANK,
    C_BELOW as C_PUMPING_SPICE,     # was red → now colorblind-safe orange
    C_NAVY as C_LAZURITE_BLUE,
    C_ABOVE, C_BELOW, C_STABLE as C_STABLE_THEME,
    PLOTLY_TEMPLATE, style_fig, page_css, direction_label,
    ARROW_UP, ARROW_DOWN, ARROW_FLAT,
)
C_POSITIVE = C_ABOVE      # above baseline / increasing  → accessible blue
C_NEGATIVE = C_BELOW      # below baseline / decreasing  → accessible orange
C_STABLE   = C_STABLE_THEME

# ── Data range ───────────────────────────────────────────────────────────────
# Fallback only; the true lower bound is recomputed from the data's min date once
# the dataset loads (so the 102nd GA / session 1801, dated 2021–2022, is included).
DATA_START_FULL = pd.to_datetime("2021-01-01").date()
DATA_END_FULL   = pd.Timestamp.today().normalize().date()   # dynamic: grows as the pipeline writes current-dated rows

_SPLIT_RE = re.compile(r"[,\|;/\n\t]+")

# ── FIX 1/2: null beneficiary patterns to filter everywhere ─────────────────
_BENEF_NULL = {
    "", "none", "none.", "n/a", "na", "unknown", "unspecified",
    "no beneficiaries", "no beneficiaries specified",
    "no beneficiaries.", "no intended beneficiaries",
    "no intended beneficiaries specified",
    "missing text", "missing text.", "not specified",
}

def _is_null_benef(s: str) -> bool:
    return str(s).strip().lower() in _BENEF_NULL


# #14: stronger null check used by the impact heatmaps — catches bare "no"/"none",
# placeholders, and phrases like "no increasing aspects" / "no decreasing aspects".
_NULL_SUBSTR = ("no increasing", "no decreasing", "no aspect", "no beneficiar",
                "no intended", "not specified", "not applicable", "missing", "none")

def _is_null_token(s) -> bool:
    s = str(s).strip().lower()
    if not s or s in _BENEF_NULL:
        return True
    if s in ("no", "na", "n/a", "nil", "-", "--", "n.a.", "n/a."):
        return True
    return any(sub in s for sub in _NULL_SUBSTR)

STOPWORDS = {
    "bill","bills","act","acts","amend","amends","amended","amendment","amendments",
    "illinois","state","code","section","sections","law","laws","new","provide","provides",
    "making","make","made","relating","regarding","including","include","includes","within",
    "existing","technical","resolution","resolutions","effective","date","public",
    "department","agency","program","programs","general","shall","may","must","also",
    "one","two","three","per","use","used","using","would","could","can","like",
    "not","no","yes","etc","among","upon","require","requires","required","requirement",
    "establish","establishes","established","create","creates","created","implementation",
    "board","boards","commission","commissions","report","reports","reporting",
    "information","data","system","systems","process","processes","administration",
    "student","students","education","educational","school","schools",
    "support","and","the","for","with","that","this","from","have","has","had",
    "be","been","being","are","is","was","were","will","would","should","could",
    "may","might","must","can","shall","need","needs","needed","such","other",
    "any","all","each","some","more","most","than","into","through","between",
    "under","over","about","against","during","after","before","above","below",
    "up","down","in","out","on","off","to","at","by","of","as","or","but","if",
    "when","where","why","how","which","who","whom","whose","what","whether",
    "there","their","they","them","these","those","then","than","only","just",
    "both","either","neither","nor","so","too","very","even","also","however",
    "therefore","thus","hence","accordingly","consequently","furthermore","moreover",
    "nevertheless","nonetheless","otherwise","rather","instead","yet","still",
    "already","always","never","ever","often","sometimes","usually","generally",
    "specifically","particularly","especially","mainly","mostly","largely",
    "context","establishment","legislative","promoting","justice","human","rights","protections"
}

GENERIC_PHRASES = {
    "effective date","public act","existing law","state code","general assembly",
    "relating to","regarding","provide that","provides that","amend the","amends the",
    "this act","the act","state agency","support and","and context","context establishment",
    "legislative support","promoting justice","justice and","and human","human rights",
    "rights protections","and human rights","justice and human","human rights protections",
    "support and context","and context establishment","legislative support and"
}
TFIDF_BLOCK_WORDS = {
    "likely","promote","promotes","promoting","desire","desires",
    "aim","aims","without","specific","etc","mentions","mention",
    "mentioned","provided","provides","appears","suggests","suggest",
    "driven","purpose","express","referred","uses","use","introduce",
    "introduced","unclear","behind","text","motivation","intent","strategy"
}
TFIDF_BLOCK_PHRASES = {"does specific","provided text","mentioned provided text","appears procedural"}

# ── Styles ───────────────────────────────────────────────────────────────────
st.markdown(f"""
<style>
.block-container {{
  padding-top:1.2rem !important; padding-bottom:0.35rem !important;
  padding-left:0.8rem !important; padding-right:0.8rem !important;
}}
header[data-testid="stHeader"] {{ background:transparent !important; height:0rem !important; }}
[data-testid="stToolbar"] {{ display:none !important; }}
.main {{ background-color:#EEF2F3; }}
/* Bug 26: a rule here forced the Inter web font onto every Streamlit element
   (including the sidebar nav) on this page only, so text visibly shifted
   when arriving on Trends. Removed; Trends now uses the app-wide font. */
.header-wrap {{
  background:linear-gradient(90deg,{C_TRAPPED_DARKNESS} 0%,{C_LAZURITE_BLUE} 60%,{C_CEDAR_PLANK} 100%);
  padding:10px 14px; border-radius:12px; margin:6px 0 10px 0;
  box-shadow:0 2px 8px rgba(0,0,0,.10);
}}
.header-title, h1.header-title {{ color:#fff; font-weight:800; font-size:20px; margin:0; padding:0;
                 line-height:1.1; }}
.header-sub {{ color:rgba(255,255,255,.88); font-size:12px; margin-top:2px; line-height:1.2; }}
.kpi-grid {{ display:grid; grid-template-columns:1fr 1fr 1fr 1fr; gap:10px; margin-bottom:10px; }}
.kpi-card {{ background:#fff; border:1px solid #D6DEE0; border-radius:12px; padding:10px 12px;
             box-shadow:0 1px 6px rgba(0,0,0,.06); }}
.kpi-label, h3.kpi-label {{ font-size:11.5px; font-weight:650; color:#454F5A; margin:0 0 6px;
              text-transform:uppercase; letter-spacing:.2px; }}
.kpi-value {{ font-size:24px; font-weight:800; color:{C_TRAPPED_DARKNESS}; line-height:1.05; }}
.filter-row {{ background:#fff; border:1px solid #D6DEE0; border-radius:12px; padding:8px 10px;
               box-shadow:0 1px 6px rgba(0,0,0,.08); margin-bottom:10px; }}
div[data-testid="stVerticalBlock"] > div {{ gap:0.35rem; }}
/* #9/#11: visually-hidden text read by screen readers as the chart's alt text */
.sr-only {{ position:absolute !important; width:1px; height:1px; padding:0; margin:-1px;
            overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }}
</style>""", unsafe_allow_html=True)

# ── Azure URLs ───────────────────────────────────────────────────────────────
AZURE_SESSION_URLS = {sid: GOLD_URLS[sid] for sid in ACTIVE_SESSIONS if sid in GOLD_URLS}

SAMPLE_MODE = False

@st.cache_data(show_spinner=False, ttl=3600)
def load_dataset_from_blob() -> pd.DataFrame:
    columns_needed = [
        "status_date","bill_id","bill_number","chamber","title",
        "category_main_label","category_sub_label",
        "increasing_aspects_standardized","decreasing_aspects_standardized",
        "intended_beneficiaries_standardized",
        "beneficiary_primary_category","beneficiary_primary_subcategory",  # new taxonomy cols
        "impact_rating_score",
        "motivation_raw","intent_raw","legislative_strategy_raw",
        "ideological_alignment_simplified",   # #21 ideological alignment strip
        "session_id",
    ]
    dfs = []
    for session_id, url in AZURE_SESSION_URLS.items():
        try:
            # Only load columns that exist (gracefully ignore new cols if not yet in data)
            df_session = pd.read_parquet(url)
            existing = [c for c in columns_needed if c in df_session.columns]
            df_session = df_session[existing].copy()
            df_session["session_id"] = session_id
            dfs.append(df_session)
        except Exception as e:
            st.warning(f"Session {session_id} ({SESSION_LABELS.get(session_id,'')}) failed to load: {e}")
    if not dfs:
        st.error("No sessions loaded.")
        return pd.DataFrame()
    df = pd.concat(dfs, ignore_index=True)
    if SAMPLE_MODE and len(df) > 5000:
        df = df.sample(5000, random_state=42)
    return df

# ── #22 predictions loader (for Top 5 Bills to Watch) ────────────────────────
@st.cache_data(show_spinner=False, ttl=3600)
def load_predictions_for_trends(session_ids: tuple) -> pd.DataFrame:
    dfs = []
    for sid in session_ids:
        url = PREDICTION_URLS.get(sid)
        if not url:
            continue
        try:
            d = pd.read_parquet(url, columns=["bill_id", "bill_number", "y_prob", "momentum"])
            d["bill_id"] = d["bill_id"].astype(str)
            dfs.append(d)
        except Exception:
            pass
    return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()


# ── FIX 7: decide which beneficiary column to use ───────────────────────────
def _benef_col(df: pd.DataFrame) -> str:
    """Prefer the taxonomy primary category column; fall back to raw."""
    if "beneficiary_primary_category" in df.columns and df["beneficiary_primary_category"].notna().any():
        return "beneficiary_primary_category"
    return "intended_beneficiaries_standardized"

# ── Helpers ──────────────────────────────────────────────────────────────────
def ensure_datetime(df, col):
    df = df.copy()
    df[col] = pd.to_datetime(df[col], errors="coerce")
    return df.dropna(subset=[col])

def add_time_grains(df, date_col):
    df = df.copy()
    d = df[date_col]
    df["month"] = d.dt.to_period("M").astype(str)
    iso = d.dt.isocalendar()
    df["week"] = iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)
    df["calendar_month"] = d.dt.month
    df["year"] = d.dt.year
    return df

def pct(n, d):
    return 0.0 if d == 0 else round((n / d) * 100.0, 1)

def _split_listlike(x):
    if pd.isna(x): return []
    if isinstance(x, list): parts = [str(i) for i in x]
    elif isinstance(x, str): parts = [p.strip() for p in _SPLIT_RE.split(x) if p.strip()]
    else: parts = [str(x).strip()]
    return [p for p in parts if p]

# ── #9/#10/#11 per-chart accessibility + optional AI description ─────────────
# chart_aux() now lives in chart_a11y.py at the app root so that 4_Active_Bills.py
# can use the same helper. Imported at the top of this file.


def safe_col(df, col):
    return col in df.columns and df[col].notna().any()

def tight_layout(fig, height=360):
    # Inherit the shared accessible template (font, palette, gridlines) and only
    # set the height + tight margins here. Gridline/axis colors come from the template.
    fig.update_layout(template=PLOTLY_TEMPLATE, height=height,
                      margin=dict(l=8,r=8,t=8,b=8))
    return fig

def build_full_period_order(start_date, end_date, grain):
    start = pd.to_datetime(start_date)
    end   = pd.to_datetime(end_date)
    if grain == "month":
        return pd.period_range(start=start, end=end, freq="M").astype(str).tolist()
    weeks = pd.date_range(start=start, end=end, freq="W-MON")
    if len(weeks) == 0:
        weeks = pd.date_range(start=start, end=end, freq="D")[:1]
    iso = weeks.isocalendar()
    return (iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)).tolist()

def explode_terms(df, col, stopwords=None, min_len=3):
    if col not in df.columns: return pd.DataFrame()
    stopwords = stopwords or set()
    tmp = df.copy()
    tmp["_term"] = tmp[col].apply(_split_listlike)
    tmp = tmp.explode("_term").dropna(subset=["_term"])
    term = tmp["_term"].astype(str).str.strip().str.lower()
    term = term.str.replace(r"[^a-z0-9\s\-]","",regex=True).str.strip()
    tmp["term"] = term
    tmp = tmp[tmp["term"].str.len() >= min_len]
    tmp = tmp[~tmp["term"].isin(stopwords)]
    tmp["mentions"] = 1
    return tmp.drop(columns=["_term"], errors="ignore")

def _clean_text_for_tfidf(t):
    t = str(t).lower()
    t = re.sub(r'http\S+|www\.\S+','',t)
    t = re.sub(r"[^a-z0-9\s\-]"," ",t)
    t = re.sub(r"\s+"," ",t).strip()
    return t

def _term_is_bad(term):
    term = term.strip().lower()
    if not term: return True
    if term in GENERIC_PHRASES or term in TFIDF_BLOCK_PHRASES: return True
    toks = term.split()
    if len(toks) < 2 or len(toks) > 3: return True
    if any(w in TFIDF_BLOCK_WORDS for w in toks): return True
    if all((w in STOPWORDS or len(w) < 3) for w in toks): return True
    generic_words = {"relating","regarding","provide","provides","amend","amends","section","subsection"}
    if any(w in generic_words for w in toks): return True
    stopword_count = sum(1 for w in toks if w in STOPWORDS)
    if len(toks) > 1 and stopword_count / len(toks) > 0.5: return True
    if len(toks) == 2 and any(w in ["state","bill","act","law"] for w in toks): return True
    return False

def _bill_docs(df_slice, bill_id_col, text_col):
    tmp = df_slice[[bill_id_col, text_col]].dropna().copy()
    tmp[text_col] = tmp[text_col].astype(str).map(_clean_text_for_tfidf)
    tmp = tmp[tmp[text_col].str.len() > 15]
    if tmp.empty: return []
    docs = tmp.groupby(bill_id_col)[text_col].apply(lambda s: " ".join(s.tolist())).tolist()
    return [d for d in docs if d.strip()]

def build_contrastive_tfidf(df_cat, df_rest, bill_id_col, text_col, top_k=15):
    if not _HAS_SK: return []
    docs_cat  = _bill_docs(df_cat,  bill_id_col, text_col)
    docs_rest = _bill_docs(df_rest, bill_id_col, text_col)
    if len(docs_cat) < 2 or len(docs_rest) < 2: return []
    vec = TfidfVectorizer(stop_words=list(STOPWORDS), ngram_range=(2,3), min_df=2,
                          max_df=0.35, sublinear_tf=True, norm="l2",
                          token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z\-]{3,}\b", max_features=2000)
    all_docs = docs_cat + docs_rest
    try: X = vec.fit_transform(all_docs)
    except Exception: return []
    terms = np.array(vec.get_feature_names_out())
    if len(terms) == 0: return []
    X_cat  = X[:len(docs_cat)]
    X_rest = X[len(docs_cat):]
    mean_cat  = np.asarray(X_cat.mean(axis=0)).ravel()
    mean_rest = np.asarray(X_rest.mean(axis=0)).ravel()
    contrast  = mean_cat - mean_rest
    idx = np.argsort(contrast)[::-1]
    out, seen_sets = [], []
    for i in idx:
        if len(out) >= top_k: break
        if contrast[i] <= 0.01: break
        t = terms[i]
        if _term_is_bad(t): continue
        wset = set(t.split())
        if any(len(wset & s) >= max(2, len(wset)-1) for s in seen_sets): continue
        seen_sets.append(wset)
        out.append((t, round(float(contrast[i]), 4)))
    return out

def classify_direction(sdf, period_col, period_order, bill_col):
    if sdf.empty: return ("Stable", 0.0)
    ts = (sdf.groupby(period_col)[bill_col].nunique()
          .reindex(period_order, fill_value=0).to_numpy(dtype=float))
    if ts.sum() == 0 or len(ts) < 2: return ("Stable", 0.0)
    slope = float(np.polyfit(np.arange(len(ts), dtype=float), ts, 1)[0])
    if slope > 0.10:  return ("Rising",   slope)
    if slope < -0.10: return ("Declining", slope)
    return ("Stable", slope)

def short_list(items, n=3):
    items = [x for x in items if x]
    if not items: return "-"
    return ", ".join(items[:n]) + ("..." if len(items) > n else "")

@st.cache_data
def compute_monthly_share_series(df_all, category, cat_col, bill_id_col, date_col):
    df_all = df_all.copy()
    df_all["_ym"] = df_all[date_col].dt.to_period("M")
    total_by_month = df_all.groupby("_ym")[bill_id_col].nunique().reset_index(name="total_bills")
    cat_df = df_all[df_all[cat_col].astype(str) == str(category)]
    cat_by_month = cat_df.groupby("_ym")[bill_id_col].nunique().reset_index(name="cat_bills")
    merged = pd.merge(total_by_month, cat_by_month, on="_ym", how="left").fillna(0)
    merged["share"] = (merged["cat_bills"] / merged["total_bills"] * 100.0).replace([np.inf,-np.inf],0).fillna(0)
    merged["year"] = merged["_ym"].dt.year
    merged["calendar_month"] = merged["_ym"].dt.month
    merged["period_str"] = merged["_ym"].astype(str)
    return merged.sort_values("_ym").reset_index(drop=True)

@st.cache_data
def compute_seasonal_baseline(share_series, lookback_years=5):
    latest_year = share_series["year"].max()
    historical  = share_series[share_series["year"] > latest_year - lookback_years].copy()
    baseline = historical.groupby("calendar_month")["share"].agg(mean_share="mean",std_share="std").reset_index()
    baseline["std_share"] = baseline["std_share"].fillna(0.0)
    return baseline

def compute_regression_on_share(share_series, total_monthly):
    y = share_series["share"].values
    x = np.arange(len(y), dtype=float)
    result = {"cat_slope":0.0,"cat_pvalue":1.0,"cat_intercept":0.0,"total_slope":0.0,"significant_growth":False}
    if len(y) < 3: return result
    try:
        slope, intercept, r, p, se = stats.linregress(x, y)
        result["cat_slope"]     = float(slope)
        result["cat_intercept"] = float(intercept)
        result["cat_pvalue"]    = float(p)
    except Exception: pass
    if total_monthly is not None and len(total_monthly) >= 3:
        try:
            yt = total_monthly["total_bills"].values.astype(float)
            t_slope, *_ = stats.linregress(np.arange(len(yt), dtype=float), yt)
            result["total_slope"] = float(t_slope)
        except Exception: pass
    result["significant_growth"] = result["cat_pvalue"] < 0.05 and result["cat_slope"] > result["total_slope"]
    return result

def compute_zscore_and_residuals(share_series, baseline, reg_stats):
    """#13: compute nested ±1 / ±1.5 / ±2 SD residual bands for the gradient envelope."""
    df = share_series.merge(baseline, on="calendar_month", how="left")
    df["z_score"] = np.where(df["std_share"] > 0,
                              (df["share"] - df["mean_share"]) / df["std_share"], 0.0)
    x = np.arange(len(df), dtype=float)
    df["predicted_share"] = reg_stats["cat_intercept"] + reg_stats["cat_slope"] * x
    df["residual"]   = df["share"] - df["predicted_share"]
    res_std = df["residual"].std() if len(df) > 2 else 0.0
    for _m, _tag in [(1.0, "1"), (1.5, "15"), (2.0, "2")]:
        df[f"upper_{_tag}"] = df["predicted_share"] + _m * res_std
        df[f"lower_{_tag}"] = df["predicted_share"] - _m * res_std
    df["upper_thresh"] = df["mean_share"] + 2.0 * df["std_share"]
    df["lower_thresh"] = df["mean_share"] - 2.0 * df["std_share"]
    df["anomaly"] = np.where(df["share"] > df["upper_thresh"], "High",
                    np.where(df["share"] < df["lower_thresh"], "Low", "Normal"))
    return df

@st.cache_data
def compute_subcategory_momentum(df_cat_filtered, df_full_cat, cat_sub_col, bill_id_col, period_col, period_order):
    if not safe_col(df_cat_filtered, cat_sub_col): return pd.DataFrame()
    df_m = df_cat_filtered.copy()
    df_m[cat_sub_col] = df_m[cat_sub_col].astype(str).str.strip()
    sub_period = (df_m.dropna(subset=[cat_sub_col])
                  .groupby([period_col, cat_sub_col])[bill_id_col].nunique()
                  .reset_index(name="bills"))
    subs = sorted(df_m[cat_sub_col].dropna().unique().tolist())
    if len(period_order) < 2 or not subs or sub_period.empty: return pd.DataFrame()
    panel_short = (sub_period.pivot_table(index=cat_sub_col, columns=period_col, values="bills", aggfunc="sum")
                   .reindex(index=subs, columns=period_order, fill_value=0))
    short_term = {}
    for sub in panel_short.index:
        y = panel_short.loc[sub].to_numpy(dtype=float)
        pct_changes = []
        for i in range(1, len(y)):
            prev, curr = y[i-1], y[i]
            pct_changes.append((curr-prev)/prev*100.0 if prev > 0 else 0.0)
        short_term[sub] = float(np.mean(pct_changes)) if pct_changes else 0.0
    long_term = {}
    if df_full_cat is not None and not df_full_cat.empty and cat_sub_col in df_full_cat.columns:
        df_fl = df_full_cat.copy()
        df_fl[cat_sub_col] = df_fl[cat_sub_col].astype(str).str.strip()
        df_fl["_ym"] = df_fl["status_date"].dt.to_period("M")
        full_sub_monthly = (df_fl.dropna(subset=[cat_sub_col])
                            .groupby(["_ym", cat_sub_col])[bill_id_col].nunique()
                            .reset_index(name="bills"))
        for sub in subs:
            sub_ts = full_sub_monthly[full_sub_monthly[cat_sub_col] == sub].sort_values("_ym")
            y_full = sub_ts["bills"].to_numpy(dtype=float)
            slope_5yr = float(np.polyfit(np.arange(len(y_full),dtype=float),y_full,1)[0]) if len(y_full)>=3 else 0.0
            long_term[sub] = slope_5yr
    mom_rows = [(sub, long_term.get(sub,0.0), short_term.get(sub,0.0)) for sub in subs]
    return (pd.DataFrame(mom_rows, columns=["Subcategory","Slope","AvgPctChange"])
            .sort_values("Slope", ascending=True).reset_index(drop=True))

def plot_category_share_with_baseline(analysis_df, period_order_filter, significant_growth):
    plot_df = analysis_df[analysis_df["period_str"].isin(period_order_filter)].copy()
    if plot_df.empty: return go.Figure()
    _px = list(plot_df["period_str"])
    fig = go.Figure()
    # #13 + colorblind fix: distinct NON-overlapping rings via fill='tonexty', each with a
    # thin contour line so bands separate by position too. Okabe-Ito palette: blue core
    # (typical), orange middle ring, yellow outer ring — distinguishable under all CVD types.
    _BLUE_F, _BLUE_L = "rgba(86,180,233,0.40)", "rgba(40,120,170,0.9)"
    _ORG_F,  _ORG_L  = "rgba(230,159,0,0.32)",  "rgba(200,130,0,0.9)"
    _YEL_F,  _YEL_L  = "rgba(240,228,66,0.45)",  "rgba(170,150,0,0.9)"
    _ring_specs = [
        ("lower_2",  None,      None,    None,    None),
        ("lower_15", "tonexty", _YEL_F,  _YEL_L,  None),
        ("lower_1",  "tonexty", _ORG_F,  _ORG_L,  None),
        ("upper_1",  "tonexty", _BLUE_F, _BLUE_L, "Typical (\u00b11 SD)"),
        ("upper_15", "tonexty", _ORG_F,  _ORG_L,  "\u00b11.5 SD"),
        ("upper_2",  "tonexty", _YEL_F,  _YEL_L,  "\u00b12 SD"),
    ]
    for _col, _fill, _fc, _lc, _legend in _ring_specs:
        _kw = dict(x=_px, y=list(plot_df[_col]), mode="lines",
                   hoverinfo="skip", showlegend=bool(_legend),
                   line=dict(color=_lc, width=1) if _lc else dict(color="rgba(0,0,0,0)", width=0))
        if _fill:
            _kw["fill"] = _fill
            _kw["fillcolor"] = _fc
        if _legend:
            _kw["name"] = _legend
        fig.add_trace(go.Scatter(**_kw))
    fig.add_trace(go.Scatter(x=plot_df["period_str"], y=plot_df["mean_share"], mode="lines",
        name="Seasonal Baseline (5yr mean)", line=dict(color="#90B4BE", dash="dash", width=2),
        hovertemplate="Seasonal Mean: %{y:.2f}%<extra></extra>"))
    fig.add_trace(go.Scatter(x=plot_df["period_str"], y=plot_df["share"], mode="lines+markers",
        name="Observed Share", line=dict(color=C_TRAPPED_DARKNESS, width=3),
        marker=dict(color=C_TRAPPED_DARKNESS, size=6),
        hovertemplate="<b>%{x}</b><br>Share: %{y:.2f}%<extra></extra>"))
    high_anom = plot_df[plot_df["anomaly"] == "High"]
    low_anom  = plot_df[plot_df["anomaly"] == "Low"]
    if not high_anom.empty:
        fig.add_trace(go.Scatter(x=high_anom["period_str"], y=high_anom["share"], mode="markers",
            name="Unusually high (beyond ±2 SD)",
            marker=dict(color=C_PUMPING_SPICE, size=12, symbol="circle",
                        line=dict(color="white", width=1.5))))
    if not low_anom.empty:
        fig.add_trace(go.Scatter(x=low_anom["period_str"], y=low_anom["share"], mode="markers",
            name="Unusually low (beyond ±2 SD)",
            marker=dict(color=C_LAZURITE_BLUE, size=12, symbol="circle",
                        line=dict(color="white", width=1.5))))
    title_suffix = " — Long-term growth trend detected" if significant_growth else ""
    fig.update_layout(template=PLOTLY_TEMPLATE, height=420, margin=dict(l=8,r=8,t=28,b=8),
        hovermode="x unified", yaxis_title="Share (%)", xaxis_title="",
        plot_bgcolor="white", paper_bgcolor="white",
        legend=dict(orientation="h", yanchor="bottom", y=-0.30, xanchor="center", x=0.5, font=dict(size=10)),
        title=dict(text=title_suffix, font=dict(size=11, color=C_PUMPING_SPICE), x=0.5) if title_suffix else {})
    _periods = list(plot_df["period_str"])
    _n = len(_periods)
    _step = max(1, _n // 8)   # cap at ~8 visible labels to prevent overlap
    _idx = list(range(0, _n, _step))
    if (_n - 1) not in _idx:
        _idx.append(_n - 1)   # always label the most recent month
    _tickvals = [_periods[i] for i in _idx]
    _ticktext = [pd.Period(_periods[i], freq="M").strftime("%b '%y") for i in _idx]
    fig.update_xaxes(showgrid=True, gridcolor="#EDF2F4", showline=True, linecolor="#D6DEE0",
                     tickmode="array", tickvals=_tickvals, ticktext=_ticktext, tickangle=-35)
    fig.update_yaxes(showgrid=True, gridcolor="#EDF2F4", showline=True, linecolor="#D6DEE0",
                     range=[0, max(5, float(plot_df["share"].max()) * 1.25)])
    return fig

# ── Column names ─────────────────────────────────────────────────────────────
DATE_COL         = "status_date"
BILL_ID_COL      = "bill_id"
CHAMBER_COL      = "chamber"
CAT_MAIN         = "category_main_label"
CAT_SUB          = "category_sub_label"
INC_COL          = "increasing_aspects_standardized"
DEC_COL          = "decreasing_aspects_standardized"
BENEF_COL_RAW    = "intended_beneficiaries_standardized"
IMPACT_SCORE_COL = "impact_rating_score"
BILL_NUM_COL     = "bill_number"

KW_SOURCES = {
    "Motivation":           "motivation_raw",
    "Intent":               "intent_raw",
    "Legislative Strategy": "legislative_strategy_raw",
}

# ── Load data ─────────────────────────────────────────────────────────────────
# Live-site finding (round 4 spot-check): the "Dataset loaded" caption used to
# appear on the first run only. On the next rerun every element after it moved
# up one slot, Streamlit rebuilt the tabs, and they reset to Macro Trends; so
# the first click on the Category Deep-Dive tab (e.g. View -> Table) jumped
# the user to the other tab. The placeholder keeps the slot on every run.
# The spinner was a second first-run-only element with the same effect (the
# placeholder alone did not fix it — verified in a browser), so it now runs on
# every load; after the first, the data is cached and it never visibly shows.
_load_note = st.empty()
with st.spinner("Loading legislative dataset…"):
    _t0 = _time.time()
    df_raw = load_dataset_from_blob()
    _load_sec = round(_time.time() - _t0, 2)
if "trends_loaded" not in st.session_state:
    st.session_state.trends_loaded = True
    _load_note.caption(f"Dataset loaded in {_load_sec}s")

if not df_raw.empty:
    df_raw["status_date"] = pd.to_datetime(df_raw["status_date"], errors="coerce")

if df_raw is None or df_raw.empty:
    st.error("Dataset is empty or failed to load.")
    st.stop()

required = [DATE_COL, BILL_ID_COL, CAT_MAIN]
missing  = [c for c in required if c not in df_raw.columns]
if missing:
    st.error(f"Missing required columns: {missing}")
    st.stop()

df_raw = df_raw.copy()
if CHAMBER_COL not in df_raw.columns or df_raw[CHAMBER_COL].isna().all():
    if BILL_NUM_COL in df_raw.columns:
        df_raw[CHAMBER_COL] = (df_raw[BILL_NUM_COL].astype(str).str.strip()
                                .str.extract(r"^([A-Za-z]+)", expand=False)
                                .str.upper().str[:2])
    else:
        df_raw[CHAMBER_COL] = "Unknown"

# FIX 7: resolve beneficiary column once
BENEF_COL = _benef_col(df_raw)

@st.cache_data
def preprocess_df(df_raw, start_d, end_d):
    df = ensure_datetime(df_raw, DATE_COL)
    df = add_time_grains(df, DATE_COL)
    df = df[(df[DATE_COL] >= pd.Timestamp(start_d)) &
            (df[DATE_COL] <= pd.Timestamp(end_d))].copy()
    return df

# True lower bound = earliest dated bill across ALL loaded sessions (covers 1801).
_dmin = df_raw[DATE_COL].min()
if pd.notna(_dmin):
    DATA_START_FULL = _dmin.date()

df_full = preprocess_df(df_raw, DATA_START_FULL, DATA_END_FULL)
df = df_full.dropna(subset=[CAT_MAIN]).copy()
if df.empty:
    st.warning("No data in the display range.")
    st.stop()

# ── Header ───────────────────────────────────────────────────────────────────
sessions_loaded = df_raw.get("session_id", pd.Series(dtype=int)).nunique() if "session_id" in df_raw.columns else 1

st.markdown(f"""
<div class="header-wrap">
  <h1 class="header-title">Legislative Trends Dashboard</h1>
  <div class="header-sub">
    Category share · Subcategory drivers · Policy direction ·
    Subcategory momentum · Beneficiary × chamber distribution ·
    {len(df_raw):,} bills across {sessions_loaded} session(s)
  </div>
</div>""", unsafe_allow_html=True)


# ── Moved to module level (were nested in the old single-page flow) ──────────
@st.cache_data
def compute_all_category_monthly_shares(df_all, cat_col, bill_id_col, date_col):
    tmp = df_all.copy()
    tmp["_ym"] = tmp[date_col].dt.to_period("M")
    total_by_month = tmp.groupby("_ym")[bill_id_col].nunique().reset_index(name="total_bills")
    cat_by_month = (tmp.dropna(subset=[cat_col])
                    .groupby(["_ym", cat_col])[bill_id_col].nunique()
                    .reset_index(name="cat_bills"))
    merged = cat_by_month.merge(total_by_month, on="_ym", how="left")
    merged["share"] = (merged["cat_bills"] / merged["total_bills"] * 100.0).replace([np.inf,-np.inf],0).fillna(0)
    merged["period_str"] = merged["_ym"].astype(str)
    merged["year"] = merged["_ym"].dt.year
    merged["calendar_month"] = merged["_ym"].dt.month
    merged = merged.rename(columns={cat_col: "Category"})
    return merged.sort_values("_ym").reset_index(drop=True)

def compute_category_seasonal_baseline_all(share_all, lookback_years=5):
    if share_all is None or share_all.empty:
        return pd.DataFrame(columns=["Category","calendar_month","mean_share","std_share"])
    tmp = share_all.copy()
    if "_ym" not in tmp.columns:
        if "period_str" in tmp.columns:
            tmp["_ym"] = pd.PeriodIndex(tmp["period_str"], freq="M")
        else:
            return pd.DataFrame(columns=["Category","calendar_month","mean_share","std_share"])
    if "calendar_month" not in tmp.columns:
        tmp["calendar_month"] = tmp["_ym"].dt.month
    tmp["year"] = tmp["_ym"].dt.year
    latest_year = int(tmp["year"].max()) if not tmp.empty else 0
    hist = tmp[tmp["year"] > latest_year - lookback_years].copy()
    baseline = (hist.groupby(["Category","calendar_month"])["share"]
                .agg(mean_share="mean",std_share="std").reset_index())
    baseline["std_share"] = baseline["std_share"].fillna(0.0)
    return baseline

def _build_impact_heatmap(src_df, aspect_col, aspect_label, top_cats, topn_benef, min_cell):
    """Beneficiary × aspect heatmap. Drops null/placeholder beneficiaries AND aspects.
    Returns a go.Figure, or None if there's nothing meaningful to show."""
    if not safe_col(src_df, BENEF_COL) or not safe_col(src_df, aspect_col):
        return None, None
    dh = src_df[src_df[CAT_MAIN].isin(top_cats)].copy()
    dh["_benef"] = dh[BENEF_COL].apply(_split_listlike)
    dh = dh.explode("_benef").dropna(subset=["_benef"])
    dh["_benef"] = dh["_benef"].astype(str).str.strip()
    dh = dh[~dh["_benef"].apply(_is_null_token)]
    dh = dh[dh["_benef"].str.len() > 0]
    dh["_asp"] = dh[aspect_col].apply(_split_listlike)
    dh = dh.explode("_asp").dropna(subset=["_asp"])
    dh["_asp"] = dh["_asp"].astype(str).str.strip().str.lower()
    dh["_asp"] = dh["_asp"].str.replace(r"[^a-z0-9\s\-]", "", regex=True).str.strip()
    dh = dh[dh["_asp"].str.len() >= 3]
    dh = dh[~dh["_asp"].isin(STOPWORDS)]
    dh = dh[~dh["_asp"].apply(_is_null_token)]
    if dh.empty:
        return None, None
    top_benef = dh["_benef"].value_counts().head(topn_benef).index.tolist()
    top_asp   = dh["_asp"].value_counts().head(12).index.tolist()
    dh = dh[dh["_benef"].isin(top_benef) & dh["_asp"].isin(top_asp)].copy()
    grp = dh.groupby(["_benef", "_asp"])[BILL_ID_COL].nunique().reset_index(name="bills")
    grp = grp[grp["bills"] >= min_cell]
    if grp.empty:
        return None, None
    pivot = grp.pivot(index="_benef", columns="_asp", values="bills").fillna(0)
    pivot = pivot.loc[pivot.sum(axis=1).sort_values(ascending=False).index,
                      pivot.sum(axis=0).sort_values(ascending=False).index]
    z_actual = pivot.values.astype(float)
    z_scaled = np.log1p(z_actual)
    _mx = z_scaled.max() if z_scaled.max() > 0 else 1
    annotations = []
    for i, rl in enumerate(pivot.index):
        for j, cl in enumerate(pivot.columns):
            v = int(z_actual[i, j])
            if v > 0:
                fc = "white" if (z_scaled[i, j] / _mx) > 0.55 else C_TRAPPED_DARKNESS
                annotations.append(dict(x=cl, y=rl, text=str(v), showarrow=False,
                                        font=dict(color=fc, size=9), xref="x", yref="y"))
    fig = go.Figure(data=go.Heatmap(
        z=z_scaled, x=pivot.columns.astype(str).tolist(), y=pivot.index.astype(str).tolist(),
        colorscale=[[0.0,"#F2F5F6"],[0.25,"#C8D9DE"],[0.5,"#7FAAB7"],[0.75,"#3D7285"],[1.0,C_LAZURITE_BLUE]],
        colorbar=dict(title="Bill Activity", tickfont=dict(size=10), thickness=12, len=0.8),
        customdata=z_actual,
        hovertemplate="Beneficiary: %{y}<br>" + aspect_label + ": %{x}<br>Unique Bills: %{customdata:.0f}<extra></extra>",
        xgap=1, ygap=1))
    fig.update_layout(template=PLOTLY_TEMPLATE, height=max(460, len(pivot.index) * 28 + 120),
        margin=dict(l=8, r=8, t=8, b=90), xaxis_title=aspect_label, yaxis_title="",
        plot_bgcolor="white", paper_bgcolor="white",
        xaxis=dict(tickangle=-40, tickfont=dict(size=9)),
        yaxis=dict(tickfont=dict(size=9)), annotations=annotations)
    _flat = grp.sort_values("bills", ascending=False)
    _tc = _flat.iloc[0]
    _facts = (f"KEY FACTS:\n- Strongest pairing: beneficiary \"{_tc['_benef']}\" with "
              f"{aspect_label.lower()} \"{_tc['_asp']}\" ({int(_tc['bills'])} bills)\n"
              f"- Beneficiaries shown: {pivot.shape[0]}; aspects shown: {pivot.shape[1]}\n"
              "Top pairings (beneficiary / aspect / bills):\n"
              + "\n".join(f"- {r['_benef']} / {r['_asp']}: {int(r['bills'])}"
                          for _, r in _flat.head(8).iterrows()))
    return fig, _facts

# ── Macro helpers: current-window-vs-seasonal-baseline ───────────────────────
SCORE_LABELS = {0: "Not", 1: "Slightly", 2: "Moderately", 3: "Very"}

def _window_bounds(df_all, date_col, mode):
    """(start_date, end_date) for the chosen macro window, anchored to the most
    recent dated bill so 'last 6 months' tracks the live edge of the data."""
    _max = df_all[date_col].max()
    if pd.isna(_max):
        _t = pd.Timestamp.today().normalize()
        return _t.date(), _t.date()
    end = _max
    if mode == "Last 6 months":
        start = end - pd.DateOffset(months=6)
    elif mode == "Last 12 months":
        start = end - pd.DateOffset(months=12)
    else:  # "Current session"
        if "session_id" in df_all.columns and df_all["session_id"].notna().any():
            _cur = df_all["session_id"].dropna().max()
            _cs = df_all.loc[df_all["session_id"] == _cur, date_col].min()
            start = _cs if pd.notna(_cs) else (end - pd.DateOffset(months=24))
        else:
            start = end - pd.DateOffset(months=24)
    return pd.Timestamp(start).date(), pd.Timestamp(end).date()

def compute_window_shifts(share_all, baseline_all, window_months):
    """Aggregate each category's share across the window and score it against its
    multi-year seasonal baseline for the SAME calendar months. Generalises the
    single-month z-score to an N-month window (window of 1 == the old behaviour)."""
    if share_all is None or share_all.empty or baseline_all is None or baseline_all.empty or not window_months:
        return pd.DataFrame()
    cal_months = sorted({pd.Period(p, freq="M").month for p in window_months})
    win = share_all[share_all["period_str"].isin(window_months)].copy()
    if win.empty:
        return pd.DataFrame()
    bl = baseline_all[baseline_all["calendar_month"].isin(cal_months)].copy()
    if bl.empty:
        return pd.DataFrame()
    bl_mean = bl.groupby("Category")["mean_share"].mean()
    # pool the per-month seasonal SDs across the window (root-mean-square)
    bl_std = bl.groupby("Category")["std_share"].apply(
        lambda s: float(np.sqrt(np.mean(np.square(pd.to_numeric(s, errors="coerce").fillna(0.0))))) if len(s) else 0.0)
    win_pivot = win.pivot_table(index="Category", columns="period_str", values="share", aggfunc="mean")
    win_pivot = win_pivot.reindex(columns=window_months).fillna(0.0)
    win_share = win_pivot.mean(axis=1)
    win_bills = win.groupby("Category")["cat_bills"].sum()
    rows = []
    for c in win_share.index:
        m = float(bl_mean.get(c, np.nan))
        if not np.isfinite(m):
            continue
        s = float(bl_std.get(c, 0.0))
        obs = float(win_share.get(c, 0.0))
        z = (obs - m) / s if s > 0 else 0.0
        pct = (obs - m) / m * 100.0 if m > 0 else 0.0
        rows.append({"Category": c, "share": obs, "mean_share": m, "std_share": s,
                     "z_score": z, "pct_change_vs_mean": pct, "period_str": window_months[-1],
                     "cat_bills": int(win_bills.get(c, 0))})
    return pd.DataFrame(rows)

def _impact_distribution(df_src):
    """% of bills falling in each impact tier (0–3), one row per bill (max score)."""
    if not safe_col(df_src, IMPACT_SCORE_COL):
        return None
    t = df_src[[BILL_ID_COL, IMPACT_SCORE_COL]].dropna()
    if t.empty:
        return None
    bs = t.groupby(BILL_ID_COL)[IMPACT_SCORE_COL].max()
    bs = pd.to_numeric(bs, errors="coerce").dropna().round().astype(int).clip(0, 3)
    if bs.empty:
        return None
    return (bs.value_counts(normalize=True).reindex([0, 1, 2, 3]).fillna(0.0) * 100.0)

def _beneficiary_counts(df_src):
    """Unique-bill coverage per beneficiary (null/placeholder beneficiaries dropped)."""
    if not safe_col(df_src, BENEF_COL):
        return pd.Series(dtype=float)
    e = df_src[[BILL_ID_COL, BENEF_COL]].copy()
    e["_b"] = e[BENEF_COL].apply(_split_listlike)
    e = e.explode("_b").dropna(subset=["_b"])
    e["_b"] = e["_b"].astype(str).str.strip()
    e = e[~e["_b"].apply(_is_null_token) & (e["_b"].str.len() > 0)]
    if e.empty:
        return pd.Series(dtype=float)
    return e.groupby("_b")[BILL_ID_COL].nunique()

# ── Shared filters (scope BOTH tabs) ─────────────────────────────────────────
if "clear_filters_t" not in st.session_state:
    st.session_state.clear_filters_t = 0

min_date = DATA_START_FULL
max_date = df_full[DATE_COL].dt.date.max() if not df_full.empty else DATA_END_FULL
cats_all = sorted(df[CAT_MAIN].dropna().astype(str).unique().tolist())

# (removed: an st.markdown can't wrap later elements, so this opening div rendered as an empty bar)
_sf1, _sf2, _sf3 = st.columns([1.4, 1.0, 0.6])
with _sf1:
    chambers_all = ["All"] + sorted(df[CHAMBER_COL].dropna().astype(str).unique().tolist())
    chambers = st.multiselect("Chamber", chambers_all, default=["All"],
                              help="Scopes every chart on both tabs.",
                              key=f"ch_{st.session_state.clear_filters_t}")
with _sf2:
    time_grain = st.radio("Time Grain", ["month", "week"], horizontal=True,
                          key=f"tg_{st.session_state.clear_filters_t}")
with _sf3:
    if st.button("CLEAR", use_container_width=True):
        st.session_state.clear_filters_t += 1
        st.rerun()

# Chamber-scoped frames used everywhere downstream
if "All" in chambers or not chambers:
    df_full_ch = df_full.copy()
    df_ch = df.copy()
else:
    _ch_set = [str(x) for x in chambers]
    df_full_ch = df_full[df_full[CHAMBER_COL].astype(str).isin(_ch_set)].copy()
    df_ch = df[df[CHAMBER_COL].astype(str).isin(_ch_set)].copy()

if df_ch.empty:
    st.warning("No rows match the selected chamber(s).")
    st.stop()

# ── Impact definition selector (beneath Chamber) ─────────────────────────────
# Default: the original University-wide impact score (already in the data as
# impact_rating_score). Users may switch to one of THEIR OWN impact prompts,
# in which case every chart recomputes off their nightly-scored ratings.
# Custom impacts exist for ACTIVE-SESSION bills only, so historical baselines
# are not available for them — the UI states this plainly.
_impact_choice = "University-wide impact (default)"
try:
    from user_profile import list_saved_items
    from impact_loader import load_impact_results
    _t_urec = st.session_state.get("user_record") or {}
    _t_uid  = _t_urec.get("email", "")
    _my_impacts = []
    if _t_uid:
        _my_impacts = [p.get("label", "") for p in list_saved_items(_t_uid, "impact_prompt")
                       if p.get("label")]
    if _my_impacts:
        _impact_choice = st.selectbox(
            "Impact definition",
            ["University-wide impact (default)"] + [f"My impact: {l}" for l in _my_impacts],
            index=0, key=f"impact_def_{st.session_state.clear_filters_t}",
            help="Default is the broad University-wide impact score. Switch to one of "
                 "your own impact questions to re-rank every chart by that lens. "
                 "Custom impacts cover active-session (104th GA) bills only.")
    if _impact_choice.startswith("My impact: "):
        _chosen_label = _impact_choice[len("My impact: "):]
        _ir = load_impact_results(_t_uid)
        if not _ir.empty:
            _ir = _ir[_ir["prompt_label"] == _chosen_label]
            _map = dict(zip(_ir["bill_id"].astype(str), _ir["rating_num"]))
            # Override the impact score column on BOTH frames; every chart reads it.
            for _frame in (df_ch, df_full_ch):
                _bid = _frame[BILL_ID_COL].astype(str)
                _frame[IMPACT_SCORE_COL] = _bid.map(_map)
            # Custom impact = active session only; historical rows have no rating.
            df_full_ch = df_full_ch[df_full_ch[IMPACT_SCORE_COL].notna()].copy()
            df_ch = df_ch[df_ch[IMPACT_SCORE_COL].notna()].copy()
            st.info(f"Showing trends re-ranked by your impact question "
                    f"**{_chosen_label}**. Custom impacts are scored for active-session "
                    f"(104th GA) bills only, so multi-year baselines are not available "
                    f"for this view.")
            if df_ch.empty:
                st.warning("No active-session bills have been scored on this impact "
                           "question yet. Results appear after the nightly scoring run.")
                st.stop()
except Exception as _e_imp:
    pass  # any failure → silently keep the University-wide default

tab_macro, tab_deep = st.tabs(["Macro Trends", "Category Deep-Dive"])

# ============================ TAB 1 · MACRO TRENDS ===========================
with tab_macro:
    st.caption("Baseline = all loaded sessions. Pick a recent window and every panel "
               "below answers: what stands out **right now** versus the multi-year norm?")
    mc1, mc2, mc3 = st.columns([1.2, 1.0, 1.4])
    with mc1:
        macro_window = st.radio(
            "Current window", ["Last 6 months", "Last 12 months", "Current session"],
            index=0, key="macro_window",
            help="The slice treated as 'now'. The seasonal baseline always spans the full history.")
    with mc2:
        _macro_sd = st.selectbox(
            "Shift sensitivity", [1.0, 1.5, 2.0], index=1,
            format_func=lambda s: f"\u00b1{s}\u03c3", key="macro_shift_sd",
            help="How far a category must move from its seasonal norm to be flagged.")
    win_start, win_end = _window_bounds(df_full_ch, DATE_COL, macro_window)
    with mc3:
        st.markdown(
            f"<div style='font-size:.82rem;color:#5b6b71;padding-top:1.6rem'>"
            f"Window: <b>{win_start:%b %d, %Y} to {win_end:%b %d, %Y}</b></div>",
            unsafe_allow_html=True)

    df_window = df_full_ch[(df_full_ch[DATE_COL].dt.date >= win_start) &
                           (df_full_ch[DATE_COL].dt.date <= win_end)].copy()
    df_window_cat = df_window.dropna(subset=[CAT_MAIN]).copy()
    df_before = df_full_ch[df_full_ch[DATE_COL].dt.date < win_start].copy()

    # seasonal baseline + window shifts (centerpiece source)
    share_all = compute_all_category_monthly_shares(df_full_ch, CAT_MAIN, BILL_ID_COL, DATE_COL)
    baseline_all = compute_category_seasonal_baseline_all(share_all, lookback_years=5)
    _win_months = sorted(
        share_all.loc[(pd.to_datetime(share_all["period_str"] + "-01").dt.date >= win_start.replace(day=1)) &
                      (pd.to_datetime(share_all["period_str"] + "-01").dt.date <= win_end), "period_str"]
        .unique().tolist())
    shifts_df = compute_window_shifts(share_all, baseline_all, _win_months)

    # ── Macro KPI row (window-scoped) ────────────────────────────────────────
    _w_bills = int(df_window[BILL_ID_COL].nunique())
    _w_cats = int(df_window_cat[CAT_MAIN].nunique())
    _w_avg = "-"
    if safe_col(df_window, IMPACT_SCORE_COL):
        _t = df_window[[BILL_ID_COL, IMPACT_SCORE_COL]].dropna()
        if not _t.empty:
            _w_avg = f"{float(_t.groupby(BILL_ID_COL)[IMPACT_SCORE_COL].max().mean()):.2f}"
    _n_shift = int((shifts_df["z_score"].abs() >= _macro_sd).sum()) if not shifts_df.empty else 0
    st.markdown(f"""
<div class="kpi-grid">
  <div class="kpi-card kpi-read"><span class="a11y-sr">Bills in window: {_w_bills:,}</span><span aria-hidden="true" class="kpi-label" style="display:block">Bills in window</span><span aria-hidden="true" class="kpi-value" style="display:block">{_w_bills:,}</span></div>
  <div class="kpi-card kpi-read"><span class="a11y-sr">Active categories: {_w_cats:,}</span><span aria-hidden="true" class="kpi-label" style="display:block">Active categories</span><span aria-hidden="true" class="kpi-value" style="display:block">{_w_cats:,}</span></div>
  <div class="kpi-card kpi-read"><span class="a11y-sr">Average impact score: {_w_avg}</span><span aria-hidden="true" class="kpi-label" style="display:block">Average impact score</span><span aria-hidden="true" class="kpi-value" style="display:block">{_w_avg}</span></div>
  <div class="kpi-card kpi-read"><span class="a11y-sr">Categories shifting: {_n_shift}</span><span aria-hidden="true" class="kpi-label" style="display:block">Categories shifting</span><span aria-hidden="true" class="kpi-value" style="display:block">{_n_shift}</span></div>
</div>
""", unsafe_allow_html=True)

    # ── Significant Category Shifts (CENTERPIECE) ────────────────────────────
    st.markdown("## Significant Category Shifts (vs Seasonal Baseline)")
    st.caption(f"Category share across {macro_window.lower()} compared with its five-year "
               "seasonal baseline for the same calendar months.")
    _n_with_baseline = int((shifts_df["std_share"] > 0).sum()) if not shifts_df.empty else 0
    _n_cats = int(shifts_df["Category"].nunique()) if not shifts_df.empty else 0
    if shifts_df.empty or _n_with_baseline < max(2, int(0.15 * max(_n_cats, 1))):
        st.info("Historical baseline is still building — more sessions are being added. Once "
                "additional years load, this panel will flag categories that are unusually "
                "active in the current window versus multi-year norms.")
    else:
        _sig = shifts_df[shifts_df["z_score"].abs() >= _macro_sd].copy()
        _fallback = _sig.empty
        if _fallback:
            st.info(f"No categories exceeded \u00b1{_macro_sd}\u03c3 in this window. Showing the largest movers.")
            _plot = shifts_df.copy()
        else:
            _plot = _sig.copy()
        _plot["abs_z"] = _plot["z_score"].abs()
        _plot = _plot.sort_values("abs_z", ascending=False).head(14)
        _plot["CategoryCue"] = _plot.apply(
            lambda r: f"{ARROW_UP if r['z_score'] >= 0 else ARROW_DOWN}  {r['Category']}", axis=1)
        _plot["Color"] = np.where(_plot["z_score"] >= 0, "Above baseline", "Below baseline")
        _cmap = {"Above baseline": C_POSITIVE, "Below baseline": C_NEGATIVE}
        _lc, _rc = st.columns([1.55, 1.0])
        with _lc:
            # Bug 4 (QA, 2026-10-07): the key was coloured text ("▲ above
            # baseline"), so it couldn't show the bars' patterns. The chart's own
            # legend is now shown instead (striped/solid swatches, like every
            # other chart); the ▲/▼ on each category label stay as a second cue.
            st.markdown('<h2 class="sleek-h">What\u2019s moving the most?</h2>', unsafe_allow_html=True)
            fig_shift = px.bar(_plot.iloc[::-1], x="z_score", y="CategoryCue", orientation="h",
                color="Color", color_discrete_map=_cmap, template=PLOTLY_TEMPLATE,
                custom_data=["share", "mean_share", "std_share", "pct_change_vs_mean", "cat_bills"],
                labels={"z_score": "Z-score (\u03c3 from baseline)", "CategoryCue": ""})
            fig_shift.update_traces(hovertemplate=(
                "<b>%{y}</b><br>Z-score: %{x:.2f}<br>Window share: %{customdata[0]:.2f}%<br>"
                "Baseline mean: %{customdata[1]:.2f}%<br>% change vs mean: %{customdata[3]:.1f}%<br>"
                "Bills in window: %{customdata[4]}<extra></extra>"))
            fig_shift = tight_layout(fig_shift, height=max(420, len(_plot) * 28 + 160))
            fig_shift.update_yaxes(showgrid=False)
            fig_shift.update_xaxes(zeroline=True, zerolinecolor="#C9D3D6")
            fig_shift.update_layout(showlegend=True, legend=dict(
                orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0, title_text=""))
            plotly_chart(fig_shift, title="Significant Category Shifts", use_container_width=True, config={"displayModeBar": False})
            _pos = _plot[_plot["z_score"] >= 0].sort_values("z_score", ascending=False)
            _neg = _plot[_plot["z_score"] < 0].sort_values("z_score", ascending=True)
            _sr = (f"Diverging bar chart of category activity vs seasonal baseline over "
                   f"{macro_window.lower()}: {len(_pos)} above, {len(_neg)} below baseline.")
            _ai = "KEY FACTS (z-score = SDs from the seasonal baseline for this window):\n"
            if not _pos.empty:
                _ai += "- Most above baseline: " + ", ".join(
                    f"{r['Category']} (+{float(r['z_score']):.2f})" for _, r in _pos.head(3).iterrows()) + "\n"
            if not _neg.empty:
                _ai += "- Most below baseline: " + ", ".join(
                    f"{r['Category']} ({float(r['z_score']):.2f})" for _, r in _neg.head(3).iterrows()) + "\n"
            chart_aux("Significant Category Shifts", _sr, _ai, "macro_shifts")
        with _rc:
            st.markdown("**Largest Movers**" if _fallback else "**Current Significant Shifts**")
            def _mk_panel(dpart, arrow):
                if dpart.empty:
                    return pd.DataFrame(columns=["Category", "Direction", "Z-Score", "% Change", "Bills"])
                out = dpart[["Category", "z_score", "pct_change_vs_mean", "cat_bills"]].copy()
                out["Direction"] = arrow
                out["Z-Score"] = out["z_score"].round(2)
                out["% Change"] = out["pct_change_vs_mean"].round(1)
                out["Bills"] = out["cat_bills"].astype(int)
                return out.drop(columns=["z_score", "pct_change_vs_mean", "cat_bills"])
            # Audit bug 7: a bare arrow in a table cell is announced as "upwards
            # arrow"; a word reads cleanly and still sorts/filters.
            _parts = [p for p in (_mk_panel(_pos, "Up"), _mk_panel(_neg, "Down")) if not p.empty]
            _panel = (pd.concat(_parts, axis=0).reset_index(drop=True) if _parts
                      else pd.DataFrame(columns=["Category", "Direction", "Z-Score", "% Change", "Bills"]))
            _panel.insert(0, "Rank", np.arange(1, len(_panel) + 1))
            # Bug 15: real HTML table; Category leads so each row is announced by name.
            a11y_table(_panel[["Category"] + [c for c in _panel.columns if c != "Category"]], download="Category shifts")

    # ── Category Ranking with movers (window rank vs historical rank) ────────
    st.markdown("## Category Ranking — with movers")
    st.caption("Bars = bills in the current window. Arrow = rank change versus the category's "
               "all-time position, so risers and fallers stand out.")
    _win_rank = df_window_cat.groupby(CAT_MAIN)[BILL_ID_COL].nunique().sort_values(ascending=False)
    _hist_rank = df_full_ch.dropna(subset=[CAT_MAIN]).groupby(CAT_MAIN)[BILL_ID_COL].nunique().sort_values(ascending=False)
    if _win_rank.empty:
        st.info("No categorised bills in the selected window.")
    else:
        _hist_pos = {c: i for i, c in enumerate(_hist_rank.index.astype(str), 1)}
        _rk = _win_rank.head(15).reset_index()
        _rk.columns = [CAT_MAIN, "Bills"]
        _rk["win_pos"] = np.arange(1, len(_rk) + 1)
        _rk["hist_pos"] = _rk[CAT_MAIN].astype(str).map(_hist_pos)
        _rk["delta"] = _rk["hist_pos"] - _rk["win_pos"]
        def _mv_label(d):
            if pd.isna(d):
                return "new"
            d = int(d)
            if d > 0:  return f"up {d}"
            if d < 0:  return f"down {abs(d)}"
            return "="
        _rk["Move"] = _rk["delta"].apply(_mv_label)
        _rk["MoveColor"] = np.where(_rk["delta"].fillna(0) > 0, "Riser",
                            np.where(_rk["delta"].fillna(0) < 0, "Faller", "Steady"))
        _rk["Label"] = _rk[CAT_MAIN].astype(str) + "  (" + _rk["Move"] + ")"
        _cmap_mv = {"Riser": C_POSITIVE, "Faller": C_NEGATIVE, "Steady": C_STABLE}
        fig_rank = px.bar(_rk.iloc[::-1], x="Bills", y="Label", orientation="h",
            color="MoveColor", color_discrete_map=_cmap_mv, template=PLOTLY_TEMPLATE,
            custom_data=["Move", "hist_pos", "win_pos"],
            labels={"Bills": "Bills in window", "Label": ""})
        fig_rank.update_traces(hovertemplate=(
            "<b>%{y}</b><br>Bills in window: %{x}<br>Rank now: #%{customdata[2]}<br>"
            "All-time rank: #%{customdata[1]}<br>Move: %{customdata[0]}<extra></extra>"))
        fig_rank = tight_layout(fig_rank, height=460)
        fig_rank.update_yaxes(showgrid=False)
        fig_rank.update_layout(legend=dict(orientation="h", yanchor="bottom", y=-0.16,
                                           xanchor="center", x=0.5, title=""))
        plotly_chart(fig_rank, title="Category Ranking (movers)", use_container_width=True, config={"displayModeBar": False})
        _clmb = _rk.dropna(subset=["delta"]).sort_values("delta", ascending=False)
        _risers = _clmb[_clmb["delta"] > 0].head(3)
        _fallers = _clmb[_clmb["delta"] < 0].sort_values("delta").head(3)
        _sr_rk = (f"Bar chart of the top {len(_rk)} categories by bills in {macro_window.lower()}, "
                  "annotated with rank change versus all-time position.")
        _ai_rk = "KEY FACTS (rank change vs all-time, positive = climbing):\n"
        if not _risers.empty:
            _ai_rk += "- Biggest climbers: " + ", ".join(
                f"{r[CAT_MAIN]} (+{int(r['delta'])})" for _, r in _risers.iterrows()) + "\n"
        if not _fallers.empty:
            _ai_rk += "- Biggest fallers: " + ", ".join(
                f"{r[CAT_MAIN]} ({int(r['delta'])})" for _, r in _fallers.iterrows()) + "\n"
        chart_aux("Category Ranking (movers)", _sr_rk, _ai_rk, "macro_rank")

    # ── Impact Distribution: window vs baseline ──────────────────────────────
    st.markdown("## Impact Distribution — window vs baseline")
    _dist_win = _impact_distribution(df_window)
    _dist_base = _impact_distribution(df_full_ch)
    if _dist_win is None or _dist_base is None:
        st.info("Impact-score data isn't available for this selection yet.")
    else:
        _tiers = [SCORE_LABELS[i] for i in [0, 1, 2, 3]]
        fig_imp = go.Figure()
        fig_imp.add_trace(go.Bar(x=_tiers, y=[float(_dist_base.get(i, 0)) for i in [0, 1, 2, 3]],
            name="Baseline (all history)", marker_color=C_STABLE,
            hovertemplate="<b>%{x}</b><br>Baseline: %{y:.1f}%<extra></extra>"))
        fig_imp.add_trace(go.Bar(x=_tiers, y=[float(_dist_win.get(i, 0)) for i in [0, 1, 2, 3]],
            name="Current window", marker_color=C_LAZURITE_BLUE,
            hovertemplate="<b>%{x}</b><br>Window: %{y:.1f}%<extra></extra>"))
        fig_imp.update_layout(template=PLOTLY_TEMPLATE, barmode="group", height=380,
            margin=dict(l=8, r=8, t=8, b=8), plot_bgcolor="white", paper_bgcolor="white",
            yaxis_title="% of bills", xaxis_title="Impact tier",
            legend=dict(orientation="h", yanchor="bottom", y=-0.25, xanchor="center", x=0.5))
        fig_imp.update_yaxes(showgrid=True, gridcolor="#EDF2F4")
        plotly_chart(fig_imp, title="Impact Distribution (window vs baseline)", use_container_width=True, config={"displayModeBar": False})
        _hi_w = float(_dist_win.get(2, 0) + _dist_win.get(3, 0))
        _hi_b = float(_dist_base.get(2, 0) + _dist_base.get(3, 0))
        _delta_hi = _hi_w - _hi_b
        st.caption(f"Higher-impact bills (Moderately + Very) are **{_hi_w:.0f}%** of this window "
                   f"versus **{_hi_b:.0f}%** historically "
                   f"({'+' if _delta_hi >= 0 else ''}{_delta_hi:.0f} pts).")
        _sr_imp = (f"Grouped bar chart of impact-tier mix, current window vs all-history baseline. "
                   f"Higher-impact share {_hi_w:.0f}% now vs {_hi_b:.0f}% baseline.")
        _ai_imp = ("KEY FACTS (% of bills per impact tier):\n"
                   + "\n".join(f"- {SCORE_LABELS[i]}: window {float(_dist_win.get(i,0)):.0f}%, "
                               f"baseline {float(_dist_base.get(i,0)):.0f}%" for i in [0, 1, 2, 3]))
        chart_aux("Impact Distribution (window vs baseline)", _sr_imp, _ai_imp, "macro_impact")

    # ── Emerging Keywords + Beneficiary Coverage ─────────────────────────────
    ek1, ek2 = st.columns(2)
    with ek1:
        st.markdown("## Emerging Keywords")
        st.caption("Terms over-represented in the current window versus everything before it.")
        macro_kw_src = st.selectbox("Source field", list(KW_SOURCES.keys()), index=0,
            key="macro_kw_src", help="Which extracted field to mine for newly prominent language.")
        _kwcol = KW_SOURCES[macro_kw_src]
        if not safe_col(df_window, _kwcol) or df_before.empty:
            st.info("Not enough history yet to contrast the current window against the past.")
        else:
            _emrg = build_contrastive_tfidf(df_window, df_before, BILL_ID_COL, _kwcol, top_k=15)
            if not _emrg:
                st.info("No distinctive emerging terms surfaced for this window.")
            else:
                _etbl = pd.DataFrame(_emrg, columns=["Keyword", "Over-representation"])
                _etbl["Over-representation"] = _etbl["Over-representation"].round(3)
                _etbl.index = np.arange(1, len(_etbl) + 1)
                a11y_table(_etbl, download="Emerging Keywords")
                _sr_ek = (f"Table of {len(_etbl)} keywords most over-represented in {macro_window.lower()} "
                          f"versus prior history (source: {macro_kw_src}).")
                _ai_ek = ("KEY FACTS (emerging terms, most distinctive first): "
                          + ", ".join(k for k, _ in _emrg[:12]))
                chart_aux("Emerging Keywords", _sr_ek, _ai_ek, "macro_kw")
    with ek2:
        st.markdown("## Beneficiary Coverage")
        st.caption("Who recent bills are written to benefit. Counts unique bills; a minimum-bill "
                   "floor keeps one-off mentions from dominating.")
        _bfloor = st.slider("Min bills per beneficiary", 1, 10, 3, 1, key="macro_benef_floor")
        _bw = _beneficiary_counts(df_window)
        if _bw.empty:
            st.info("No usable beneficiary labels in the current window yet.")
        else:
            _bb = _beneficiary_counts(df_full_ch)
            _bw = _bw[_bw >= _bfloor].sort_values(ascending=False).head(12)
            if _bw.empty:
                st.info(f"No beneficiary reaches \u2265 {_bfloor} bills in this window. Lower the floor to see more.")
            else:
                _base_total = float(_bb.sum()) if _bb.sum() > 0 else 1.0
                _win_total = float(df_window[BILL_ID_COL].nunique()) or 1.0
                _bdf = _bw.reset_index()
                _bdf.columns = ["Beneficiary", "Bills"]
                _bdf["win_share"] = _bdf["Bills"] / _win_total * 100.0
                _bdf["base_share"] = _bdf["Beneficiary"].map(_bb).fillna(0.0) / _base_total * 100.0
                _bdf["delta"] = _bdf["win_share"] - _bdf["base_share"]
                _bdf["Color"] = np.where(_bdf["delta"] >= 0, "Up vs baseline", "Down vs baseline")
                fig_ben = px.bar(_bdf.iloc[::-1], x="Bills", y="Beneficiary", orientation="h",
                    color="Color", color_discrete_map={"Up vs baseline": C_POSITIVE, "Down vs baseline": C_NEGATIVE},
                    template=PLOTLY_TEMPLATE, custom_data=["win_share", "base_share", "delta"],
                    labels={"Bills": "Bills in window", "Beneficiary": ""})
                fig_ben.update_traces(hovertemplate=(
                    "<b>%{y}</b><br>Bills in window: %{x}<br>Window share: %{customdata[0]:.1f}%<br>"
                    "Baseline share: %{customdata[1]:.1f}%<br>Δ: %{customdata[2]:+.1f} pts<extra></extra>"))
                fig_ben = tight_layout(fig_ben, height=380)
                fig_ben.update_yaxes(showgrid=False)
                # Walkthrough finding (bug 4): the colour means up/down versus baseline,
                # but the legend was switched off, so nothing on screen said so. Show it,
                # above the plot; its swatches carry the same hatch as the bars.
                fig_ben.update_layout(showlegend=True, legend=dict(title_text="", orientation="h",
                                      yanchor="bottom", y=1.02, xanchor="left", x=0))
                plotly_chart(fig_ben, title="Beneficiary Coverage", use_container_width=True, config={"displayModeBar": False})
                _sr_ben = (f"Bar chart of the top {len(_bdf)} beneficiaries by bills in {macro_window.lower()} "
                           f"(minimum {_bfloor} bills), coloured by change versus baseline share.")
                _ai_ben = ("KEY FACTS (beneficiary: bills in window, window share vs baseline share):\n"
                           + "\n".join(f"- {r['Beneficiary']}: {int(r['Bills'])} bills, "
                                       f"{r['win_share']:.1f}% vs {r['base_share']:.1f}%"
                                       for _, r in _bdf.iterrows()))
                chart_aux("Beneficiary Coverage", _sr_ben, _ai_ben, "macro_benef")

    # ── Top Bills to Watch (global, impact-floored) ──────────────────────────
    st.markdown("## Top Bills to Watch")
    st.caption("Highest reaches-vote-likelihood bills across all categories — gaining momentum and "
               "at least moderate University impact.")
    _mpreds = load_predictions_for_trends(tuple(sorted(AZURE_SESSION_URLS.keys())))
    if _mpreds.empty:
        st.info("Prediction data isn't available yet. It populates from the daily model run.")
    else:
        _src = df_full_ch.copy()
        if safe_col(_src, IMPACT_SCORE_COL):
            _bill_imp = _src[[BILL_ID_COL, IMPACT_SCORE_COL]].dropna().groupby(BILL_ID_COL)[IMPACT_SCORE_COL].max()
            _keep = _bill_imp[_bill_imp >= 2].index.astype(str)
        else:
            _keep = _src[BILL_ID_COL].astype(str)
        _cols = [c for c in [BILL_ID_COL, "title", BILL_NUM_COL, CAT_MAIN] if c in _src.columns]
        _bills = _src[_cols].drop_duplicates(BILL_ID_COL).copy()
        _bills[BILL_ID_COL] = _bills[BILL_ID_COL].astype(str)
        _bills = _bills[_bills[BILL_ID_COL].isin(_keep)]
        _mm = _bills.merge(_mpreds, left_on=BILL_ID_COL, right_on="bill_id", how="inner")
        if _mm.empty:
            st.info("No scored, at-least-moderate-impact bills available yet.")
        else:
            _mm["y_prob"] = pd.to_numeric(_mm["y_prob"], errors="coerce").fillna(0.0)
            _gain = _mm[_mm["momentum"].astype(str).str.lower() == "gaining"]
            _pool = _gain if not _gain.empty else _mm
            _topm = _pool.sort_values("y_prob", ascending=False).drop_duplicates(BILL_ID_COL).head(8)
            if _gain.empty:
                st.caption("No bills are currently gaining momentum — showing the highest reaches-vote-likelihood bills.")
            _dispm = pd.DataFrame({
                "Bill": _topm[BILL_NUM_COL].astype(str).values if BILL_NUM_COL in _topm.columns else _topm[BILL_ID_COL].values,
                "Category": _topm[CAT_MAIN].astype(str).values if CAT_MAIN in _topm.columns else "",
                "Title": _topm["title"].astype(str).str.slice(0, 60).values if "title" in _topm.columns else "",
                "Reaches-Vote Likelihood": ((_topm["y_prob"] * 100).round(0).astype(int).astype(str) + "%").values,
                "Momentum": _topm["momentum"].astype(str).values,
            })
            a11y_table(_dispm, download="Top Bills to Watch")
            st.caption("Open the **Bill Predictions** page for full probability trails.")
            _sr_tw = (f"Table of the top {len(_dispm)} highest reaches-vote-likelihood bills across all "
                      "categories, gaining-momentum and at least moderate impact first.")
            _ai_tw = ("KEY FACTS (bill: category, reaches-vote likelihood, momentum):\n"
                      + "\n".join(f"- {r['Bill']}: {r['Category']}, {r['Reaches-Vote Likelihood']}, {r['Momentum']}"
                                  for _, r in _dispm.iterrows()))
            chart_aux("Top Bills to Watch", _sr_tw, _ai_tw, "macro_top")

    # ── AI Macro Overview (leads with what's anomalous now) ──────────────────
    with st.expander("AI Macro Overview", expanded=False):
        st.caption("Plain-English read of what stands out in the current window versus the "
                   "multi-year baseline, grounded in the panels above. Uses Azure OpenAI.")
        if st.button("Generate Overview", key="macro_ai_btn"):
            if not ai_briefing.is_configured():
                st.info("AI is not yet configured. Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_KEY "
                        "in Azure App Service, under Environment variables.")
            else:
                _mv_lines = []
                if not shifts_df.empty:
                    _p = shifts_df.sort_values("z_score", ascending=False).head(4)
                    _n = shifts_df.sort_values("z_score", ascending=True).head(4)
                    _mv_lines.append("Above baseline: " + ", ".join(
                        f"{r['Category']} (+{float(r['z_score']):.1f}\u03c3, {float(r['pct_change_vs_mean']):+.0f}%)"
                        for _, r in _p.iterrows() if r["z_score"] > 0))
                    _mv_lines.append("Below baseline: " + ", ".join(
                        f"{r['Category']} ({float(r['z_score']):.1f}\u03c3)"
                        for _, r in _n.iterrows() if r["z_score"] < 0))
                _imp_line = ""
                if _dist_win is not None and _dist_base is not None:
                    _imp_line = (f"Higher-impact bills are {float(_dist_win.get(2,0)+_dist_win.get(3,0)):.0f}% of "
                                 f"the window vs {float(_dist_base.get(2,0)+_dist_base.get(3,0)):.0f}% historically.")
                _prompt = f"""You are a policy analyst at the University of Illinois. In 4-6 sentences, brief an administrator on what stands out RIGHT NOW in Illinois legislation versus the multi-year norm. Lead with the single most notable shift.

Window analysed: {macro_window} ({win_start} to {win_end}); {_w_bills} bills, {_w_cats} active categories.
Categories moving vs their seasonal baseline:
{chr(10).join('- ' + l for l in _mv_lines if l)}
{_imp_line}

Write prose (no bullet points). Be concrete about which policy areas are unusually hot or quiet and what that means for the University of Illinois. If signals are thin, say so plainly."""
                try:
                    with st.spinner("Reading the window…"):
                        _txt = ai_briefing.generate(_prompt, max_tokens=400)
                    st.markdown(_txt)
                except Exception as e:
                    st.error(f"Overview generation failed: {e}")

# ============================ TAB 2 · CATEGORY DEEP-DIVE =====================
with tab_deep:
    st.caption("Drill into one policy category. The seasonal band spans the full "
               "multi-year history; the date range below scopes the filtered views.")
    # (removed: an st.markdown can't wrap later elements, so this opening div rendered as an empty bar)
    _df1, _df2, _df3 = st.columns([2.0, 1.6, 1.4])
    with _df1:
        selected_cat = st.selectbox("Category", cats_all, index=0,
                                    key=f"cat_{st.session_state.clear_filters_t}")
    with _df2:
        date_range = st.date_input("Date Range", value=(min_date, max_date),
            min_value=min_date, max_value=max_date, key=f"date_{st.session_state.clear_filters_t}")
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_date, end_date = date_range
    else:
        start_date = end_date = date_range
    with _df3:
        sub_time = st.selectbox("Momentum Window", ["Overall", "Last 30 days", "Last 60 days"],
            help="Window for the Subcategory Drivers and Momentum panels.",
            key=f"subwin_{st.session_state.clear_filters_t}")

    df_f = df_ch[(df_ch[DATE_COL].dt.date >= start_date) &
                 (df_ch[DATE_COL].dt.date <= end_date)].copy()
    df_f = df_f.dropna(subset=[CAT_MAIN])
    if df_f.empty:
        st.warning("No rows match your filters.")
        st.stop()
    period_col = time_grain
    period_order = build_full_period_order(start_date, end_date, time_grain)

    # ---- Category Share + Subcategory Drivers ----
    # ── Category Share + SD selector + Subcategory Drivers ───────────────────────
    df_cat = df_f[df_f[CAT_MAIN].astype(str) == str(selected_cat)].copy()
    share_series_full = compute_monthly_share_series(df_full, selected_cat, CAT_MAIN, BILL_ID_COL, DATE_COL)
    seasonal_baseline = compute_seasonal_baseline(share_series_full, lookback_years=5)
    total_monthly_full = (df_full.groupby(df_full[DATE_COL].dt.to_period("M"))[BILL_ID_COL]
                          .nunique().reset_index(name="total_bills"))
    reg_stats    = compute_regression_on_share(share_series_full, total_monthly_full)
    month_period_order = build_full_period_order(start_date, end_date, "month")

    # FIX 4: SD multiplier selector
    # #13: SD selector removed — all three bands (±1/±1.5/±2) now shown together as a gradient.
    analysis_df = compute_zscore_and_residuals(share_series_full, seasonal_baseline, reg_stats)

    df_sub = df_cat.copy()
    cutoff = None
    if sub_time != "Overall":
        days = 30 if sub_time == "Last 30 days" else 60
        cutoff = (pd.to_datetime(end_date) - pd.Timedelta(days=days)).date()
        df_sub = df_sub[df_sub[DATE_COL].dt.date >= cutoff]

    emerging_map = {}
    if cutoff is not None and safe_col(df_cat, CAT_SUB):
        before = df_cat[df_cat[DATE_COL].dt.date < cutoff]
        before_set = set(before[CAT_SUB].dropna().astype(str).unique().tolist())
        for s in set(df_sub[CAT_SUB].dropna().astype(str).unique().tolist()):
            emerging_map[s] = (s not in before_set)

    sub_ct = pd.DataFrame()
    if CAT_SUB in df_sub.columns and not df_sub[CAT_SUB].isna().all():
        sub_ct = (df_sub.dropna(subset=[CAT_SUB])
                  .groupby(CAT_SUB)[BILL_ID_COL].nunique()
                  .reset_index(name="Bills").sort_values("Bills", ascending=False).head(12))

    hover_dir = []
    hover_impact = []
    for sub in sub_ct[CAT_SUB].astype(str).tolist() if not sub_ct.empty else []:
        sdf = df_sub[df_sub[CAT_SUB].astype(str) == str(sub)].copy()
        direction, _ = classify_direction(sdf, period_col, period_order, BILL_ID_COL)
        if cutoff is not None and emerging_map.get(sub, False):
            direction = "Emerging"
        if safe_col(sdf, IMPACT_SCORE_COL):
            bmax = sdf[[BILL_ID_COL, IMPACT_SCORE_COL]].dropna().groupby(BILL_ID_COL)[IMPACT_SCORE_COL].max()
            avg_imp = float(bmax.mean()) if len(bmax) else float("nan")
        else:
            avg_imp = float("nan")
        hover_dir.append(direction)
        hover_impact.append(None if np.isnan(avg_imp) else round(avg_imp, 2))
    sub_ct2 = sub_ct.copy()
    if not sub_ct2.empty:
        sub_ct2["Direction"]  = hover_dir
        sub_ct2["AvgImpact"]  = hover_impact

    r1a, r1b = st.columns(2)
    with r1a:
        st.markdown("## Category Share Over Time")
        fig_share = plot_category_share_with_baseline(analysis_df, month_period_order,
                                                       reg_stats["significant_growth"])
        if fig_share.data:
            plotly_chart(fig_share, title="Category Share Over Time", use_container_width=True, config={"displayModeBar":False})
            _an = analysis_df.copy()
            _nh = int((_an["anomaly"] == "High").sum()); _nl = int((_an["anomaly"] == "Low").sum())
            _last = _an.dropna(subset=["share"]).tail(1)
            _sr_sh = (f"Line chart of {selected_cat} share of monthly legislation over time, inside a "
                      f"typical-range band. {_nh} months unusually high and {_nl} unusually low versus "
                      "the five-year seasonal baseline.")
            _ai_sh = ("KEY FACTS (use these verbatim):\n"
                      f"- Category: {selected_cat}\n"
                      f"- Months unusually high (beyond +2 SD): {_nh}\n"
                      f"- Months unusually low (beyond -2 SD): {_nl}\n")
            if not _last.empty:
                _ai_sh += (f"- Most recent monthly share: {float(_last['share'].iloc[0]):.1f}%\n"
                           f"- Five-year seasonal baseline at that month: {float(_last['mean_share'].iloc[0]):.1f}%\n")
            chart_aux("Category Share Over Time", _sr_sh, _ai_sh, "share")
        else:
            st.info("No share data available for this selection.")
    with r1b:
        st.markdown("## Subcategory Drivers")
        if sub_ct2.empty:
            st.info("No subcategory labels available for this selection yet. "
                    "Subcategory detail comes from the LLaMA standardizer stage — it populates "
                    "as session extraction completes and for categories with enough labeled bills. "
                    "Try the 'Overall' momentum window or a broader category.")
        else:
            show = sub_ct2.sort_values("Bills", ascending=True)
            _dir_color = {"Rising": C_POSITIVE, "Declining": C_NEGATIVE,
                          "Stable": C_STABLE, "Emerging": "#7F77DD"}
            _colors = [_dir_color.get(str(d), C_STABLE) for d in show["Direction"]]
            _legend = (f'<div style="font-size:.8rem;color:#5b6b71;margin:-2px 0 6px">'
                       f'<b style="color:{C_POSITIVE}">Rising</b> &nbsp;·&nbsp; '
                       f'<b style="color:{C_NEGATIVE}">Declining</b> &nbsp;·&nbsp; '
                       f'<b style="color:{C_STABLE}">Stable</b>')
            if (show["Direction"].astype(str) == "Emerging").any():
                _legend += ' &nbsp;·&nbsp; <b style="color:#5A50C8">Emerging</b>'
            _legend += "</div>"
            st.markdown(_legend, unsafe_allow_html=True)
            fig = go.Figure(go.Bar(
                x=show["Bills"], y=show[CAT_SUB].astype(str), orientation="h",
                marker_color=_colors,
                customdata=np.stack([show["Direction"].astype(str)], axis=-1),
                hovertemplate="<b>%{y}</b><br>Bills: %{x}<br>Direction: %{customdata[0]}<extra></extra>"))
            fig = tight_layout(fig, height=420)
            fig.update_xaxes(title_text="Bills")
            fig.update_yaxes(showgrid=False)
            plotly_chart(fig, title="Subcategory Drivers", use_container_width=True, config={"displayModeBar":False})
            _sd = show.sort_values("Bills", ascending=False)
            _top_sub = _sd.iloc[0]
            _sr_sd = (f"Bar chart of {len(_sd)} subcategories within {selected_cat}, colored by trend. "
                      f"Largest: {_top_sub[CAT_SUB]} ({int(_top_sub['Bills'])} bills, {_top_sub['Direction']}).")
            _ai_sd = ("KEY FACTS (subcategory: bills, trend), already sorted by bills:\n"
                      + "\n".join(f"- {r[CAT_SUB]}: {int(r['Bills'])} bills, {r['Direction']}"
                                  for _, r in _sd.head(12).iterrows()))
            chart_aux("Subcategory Drivers", _sr_sd, _ai_sd, "subdrv")

    # ---- Cross-Session + Top Keywords ----
    r2a, r2b = st.columns([1.55, 1.0])
    with r2a:
        st.markdown("## Cross-Session Comparison")
        st.caption(f"Monthly \u201c{selected_cat}\u201d bill activity across General Assembly "
                   "sessions \u2014 is this session unusual versus prior years?")
        _xs = df_full[df_full[CAT_MAIN].astype(str) == str(selected_cat)].copy()
        if _xs.empty or "session_id" not in _xs.columns or _xs["session_id"].nunique() < 1:
            st.info("No cross-session data available for this category yet.")
        else:
            _xs["calendar_month"] = _xs[DATE_COL].dt.month
            _grp = (_xs.dropna(subset=["calendar_month"])
                    .groupby(["session_id", "calendar_month"])[BILL_ID_COL]
                    .nunique().reset_index(name="bills"))
            _MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                       "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            _sess_color = {2176: C_LAZURITE_BLUE, 2020: C_CEDAR_PLANK, 1801: C_POSITIVE}
            _palette = [C_TRAPPED_DARKNESS, "#56B4E9", "#CC79A7", C_NEGATIVE]
            _current = max(_grp["session_id"]) if not _grp.empty else None
            fig = go.Figure()
            for _i, _sid in enumerate(sorted(_grp["session_id"].unique(), reverse=True)):
                _sub = (_grp[_grp["session_id"] == _sid]
                        .set_index("calendar_month").reindex(range(1, 13), fill_value=0))
                _is_cur = (_sid == _current)
                _label = SESSION_LABELS.get(_sid, str(_sid))
                fig.add_trace(go.Scatter(
                    x=_MONTHS, y=_sub["bills"].values, mode="lines+markers",
                    name=_label,
                    line=dict(color=_sess_color.get(_sid, _palette[_i % len(_palette)]),
                              width=3 if _is_cur else 2),
                    marker=dict(size=6 if _is_cur else 5),
                    hovertemplate="<b>%{x}</b><br>%{y} bills<extra>" + _label + "</extra>"))
            fig.update_layout(template=PLOTLY_TEMPLATE, height=420, margin=dict(l=8, r=8, t=8, b=8),
                hovermode="x unified", yaxis_title="Bills", xaxis_title="",
                plot_bgcolor="white", paper_bgcolor="white",
                legend=dict(orientation="h", yanchor="bottom", y=-0.22, xanchor="center", x=0.5))
            fig.update_xaxes(showgrid=True, gridcolor="#EDF2F4", showline=True, linecolor="#D6DEE0")
            fig.update_yaxes(showgrid=True, gridcolor="#EDF2F4", showline=True, linecolor="#D6DEE0")
            plotly_chart(fig, title="Cross-Session Comparison", use_container_width=True, config={"displayModeBar": False})
            _st = _grp.groupby("session_id")["bills"].sum().sort_index(ascending=False)
            _sr_xs = (f"Line chart comparing monthly {selected_cat} bill activity across "
                      f"{len(_st)} General Assembly sessions. Total bills per session: "
                      + ", ".join(f"{SESSION_LABELS.get(int(s), str(s))} {int(v)}" for s, v in _st.items()) + ".")
            _ai_xs = ("KEY FACTS (total bills in this category per General Assembly session):\n"
                      + "\n".join(f"- {SESSION_LABELS.get(int(s), str(s))}: {int(v)} bills" for s, v in _st.items())
                      + "\nThe most recent session listed is the current one. State whether it is "
                        "tracking above or below the prior session.")
            chart_aux("Cross-Session Comparison", _sr_xs, _ai_xs, "xsess")

    with r2b:
        st.markdown("## Top Keywords")
        tfidf_mode = st.selectbox("Keyword Source", ["Motivation","Intent","Legislative Strategy"], index=0,
            help="Which extracted field to mine for the words that most distinguish this category.")
        tfidf_text_col = KW_SOURCES[tfidf_mode]
        if not safe_col(df_f, tfidf_text_col):
            st.info(f"Column `{tfidf_text_col}` not available.")
        else:
            df_rest    = df_f[df_f[CAT_MAIN].astype(str) != str(selected_cat)].copy()
            tf_phrases = build_contrastive_tfidf(df_cat, df_rest, BILL_ID_COL, tfidf_text_col, top_k=15)
            if not tf_phrases:
                st.info("TF-IDF returned no meaningful phrases for this slice.")
            else:
                kw_view = st.radio("View", ["Word cloud", "Table"], horizontal=True, key="kw_view")
                if kw_view == "Table":
                    kw_tbl = pd.DataFrame(tf_phrases, columns=["Keyword", "Distinctiveness Score"])
                    kw_tbl.index = np.arange(1, len(kw_tbl) + 1)
                    a11y_table(kw_tbl, download="Top Keywords")
                else:
                    # #16: word cloud via wordcloud2.js, font size weighted by distinctiveness.
                    # Walkthrough finding: the library was fetched from cdnjs at render
                    # time; where that request is blocked the cloud was silently empty.
                    # It is now bundled (vendor/wordcloud2.js) and inlined. Word colours
                    # changed from #C46A1B (3.87:1) and #5b6b71 to ones that pass 4.5:1.
                    _wc_json = json.dumps([[str(k), float(s)] for k, s in tf_phrases])
                    _wc_html = """
    <div id="wc" style="width:100%;height:300px;"></div>
    <script>__WCLIB__</script>
    <script>
    (function(){
      var raw = __WORDS__;
      if(!raw.length){ return; }
      var mx = Math.max.apply(null, raw.map(function(d){return d[1];}));
      var mn = Math.min.apply(null, raw.map(function(d){return d[1];}));
      var span = (mx - mn) || 1;
      var list = raw.map(function(d){ return [d[0], 16 + ((d[1]-mn)/span)*44]; });
      var el = document.getElementById('wc');
      // Walkthrough finding: this sits on the Deep-Dive tab, which Streamlit
      // builds while hidden, so the first draw happened in a 0x0 box and drew
      // nothing. Wait for a real size, and redraw if the size changes.
      var drawnW = 0;
      function draw(){
        if(typeof WordCloud === 'undefined'){ setTimeout(draw, 120); return; }
        if(!el.clientWidth || !el.clientHeight){ setTimeout(draw, 200); return; }
        if(el.clientWidth === drawnW){ return; }
        drawnW = el.clientWidth;
        WordCloud(el, { list:list, gridSize:7, weightFactor:1, minSize:12,
          fontFamily:'Inter, Helvetica, Arial, sans-serif',
          color:function(){ var c=['#1B4B5A','#2E6E85','#A2570F','#454F5A']; return c[Math.floor(Math.random()*c.length)]; },
          backgroundColor:'#ffffff', rotateRatio:0.25, rotationSteps:2, shuffle:false });
      }
      draw();
      if (window.ResizeObserver) { new ResizeObserver(function(){ draw(); }).observe(el); }
    })();
    </script>
    """.replace("__WORDS__", _wc_json).replace("__WCLIB__", _wordcloud_lib())
                    components.html(_wc_html, height=320)
                # Text equivalent covering BOTH views. The word cloud is drawn by
                # wordcloud2.js inside a components.html iframe, which screen readers
                # cannot reach from the parent document — so the keywords and their
                # scores are emitted here, in the parent, as visually-hidden text.
                _kw_sr = (
                    f"Top {len(tf_phrases)} keywords distinguishing {selected_cat} "
                    f"legislation, source field {tfidf_mode}, ranked by distinctiveness "
                    f"score: "
                    + ", ".join(f"{k} {float(s):.2f}" for k, s in tf_phrases)
                    + ". The same data is available as a sortable table using the View "
                      "control above."
                )
                sr_text("Top Keywords", _kw_sr)
                # #16: optional AI takeaway interpreting the distinguishing keywords
                if ai_briefing.is_configured():
                    if st.button("AI takeaway", key="kw_ai"):
                        try:
                            _kw_list = ", ".join(k for k, _ in tf_phrases[:12])
                            _kw_prompt = (
                                f'For University of Illinois policy staff: the words that most distinguish '
                                f'"{selected_cat}" legislation (source: {tfidf_mode}) are: {_kw_list}. '
                                f'In 2-3 sentences, explain in plain English what theme or framing these '
                                f'keywords reveal about how this category of legislation is motivated or '
                                f'positioned. Lead with the takeaway; do not just relist the words.')
                            with st.spinner("Reading the keywords…"):
                                _kw_txt = ai_briefing.generate(_kw_prompt, max_tokens=200)
                            st.info(_kw_txt)
                        except Exception as e:
                            st.error(f"Could not generate takeaway: {e}")

    # ---- Subcategory Momentum + Heatmap ----
    # ── Subcategory Momentum + Heatmap ────────────────────────────────────────────
    st.markdown("")
    r3a, r3b = st.columns(2)
    with r3a:
        st.markdown("## Subcategory Momentum")
        df_full_cat  = df_full[df_full[CAT_MAIN].astype(str) == str(selected_cat)].copy()
        sub_momentum = compute_subcategory_momentum(df_cat, df_full_cat, CAT_SUB, BILL_ID_COL, period_col, period_order)
        if sub_momentum.empty:
            st.info("Not enough data to compute momentum.")
        else:
            sub_momentum["SlopeScaled"] = sub_momentum["Slope"] * 100.0
            sub_momentum["SlopeScaled"] = pd.to_numeric(sub_momentum["SlopeScaled"], errors="coerce").fillna(0.0)
            eps = 1e-4
            sub_momentum["Direction"] = np.where(sub_momentum["SlopeScaled"] > eps, "Rising",
                                        np.where(sub_momentum["SlopeScaled"] < -eps, "Falling", "Stable"))
            movers = sub_momentum[sub_momentum["Direction"] != "Stable"].copy()
            stable = sub_momentum[sub_momentum["Direction"] == "Stable"].copy()
            movers = movers.reindex(movers["SlopeScaled"].abs().sort_values(ascending=False).index).head(10)
            stable = stable.head(5)
            show_df = pd.concat([movers, stable], axis=0).drop_duplicates("Subcategory")
            if show_df.empty: show_df = sub_momentum.head(12).copy()
            min_visible = 0.20
            show_df = show_df.copy()
            show_df["DisplaySlope"] = show_df["SlopeScaled"]
            near_zero = show_df["DisplaySlope"].abs() < min_visible
            show_df.loc[near_zero & (show_df["Direction"]=="Rising"),   "DisplaySlope"] = min_visible
            show_df.loc[near_zero & (show_df["Direction"]=="Falling"),  "DisplaySlope"] = -min_visible
            show_df.loc[near_zero & (show_df["Direction"]=="Stable"),   "DisplaySlope"] = min_visible * 0.6
            show_df = show_df.sort_values("DisplaySlope", ascending=True)
            show_df["AvgPctChange"] = pd.to_numeric(show_df["AvgPctChange"], errors="coerce").fillna(0.0)
            color_map = {"Rising":C_POSITIVE,"Falling":C_NEGATIVE,"Stable":C_STABLE}
            fig = px.bar(show_df, x="DisplaySlope", y="Subcategory", color="Direction",
                orientation="h", color_discrete_map=color_map, template=PLOTLY_TEMPLATE,
                custom_data=["SlopeScaled","Direction","AvgPctChange"],
                labels={"DisplaySlope":"Legislative Activity Trend","Subcategory":""})
            fig.update_traces(hovertemplate=(
                "<b>%{y}</b><br>Direction: %{customdata[1]}<br>"
                "5-Yr Slope (x100): %{customdata[0]:.3f}<br>"
                "Short-Term Avg % Change: %{customdata[2]:.1f}%<extra></extra>"))
            max_abs = max(float(np.nanmax(np.abs(show_df["DisplaySlope"].to_numpy()))) if len(show_df) else 1.0, 1.0)
            fig.update_layout(template=PLOTLY_TEMPLATE, height=520, margin=dict(l=8,r=8,t=8,b=8),
                plot_bgcolor="white", paper_bgcolor="white", barmode="relative",
                legend=dict(orientation="h", yanchor="bottom", y=-0.22, xanchor="center", x=0.5))
            fig.update_xaxes(range=[-max_abs*1.15, max_abs*1.15], showgrid=True, gridcolor="#EDF2F4",
                             zeroline=True, zerolinecolor="#C9D3D6")
            fig.update_yaxes(showgrid=False)
            plotly_chart(fig, title="Subcategory Momentum", use_container_width=True, config={"displayModeBar":False})
            _rise = show_df[show_df["Direction"] == "Rising"].sort_values("SlopeScaled", ascending=False)
            _fall = show_df[show_df["Direction"] == "Falling"].sort_values("SlopeScaled", ascending=True)
            _sr_sm = (f"Diverging bar chart of subcategory momentum within {selected_cat}: "
                      f"{len(_rise)} rising, {len(_fall)} falling.")
            _ai_sm = "KEY FACTS:\n"
            if not _rise.empty:
                _ai_sm += "- Rising fastest: " + ", ".join(_rise["Subcategory"].head(3).astype(str)) + "\n"
            if not _fall.empty:
                _ai_sm += "- Falling fastest: " + ", ".join(_fall["Subcategory"].head(3).astype(str)) + "\n"
            _ai_sm += ("Full data (subcategory: direction, short-term avg % change):\n"
                       + "\n".join(f"- {r['Subcategory']}: {r['Direction']}, {float(r['AvgPctChange']):.0f}%"
                                   for _, r in show_df.iterrows()))
            chart_aux("Subcategory Momentum", _sr_sm, _ai_sm, "submom")
        _dir_choice = st.radio("Aspect direction", ["Increasing", "Decreasing"], horizontal=True,
                               key="heat_dir",
                               help="Which beneficiaries are affected by bills that increase vs decrease "
                                    "something. Bills with no listed beneficiary or aspect are excluded.")
        # Walkthrough finding (bug 5): with the default gap, the first slider's
        # maximum label ran into the second's minimum ("100 1").
        # gap="large" still left only ~6px, because the end labels hang past each
        # slider's edge. An empty spacer column keeps them clearly apart.
        hc1, _hc_gap, hc2 = st.columns([5, 1, 5])
        with hc1:
            cat_bill_thresh = st.slider("Min Bills per Category", min_value=5, max_value=100, value=5, step=5)
        with hc2:
            min_cell_thresh = st.slider("Min Bills per Cell", min_value=1, max_value=15, value=1, step=1)
        topN_benef = st.slider("Top N Beneficiaries", min_value=5, max_value=25, value=10, step=1)

        _asp_col = INC_COL if _dir_choice == "Increasing" else DEC_COL
        _asp_lbl = f"{_dir_choice} aspect"
        cat_counts = df_f.groupby(CAT_MAIN)[BILL_ID_COL].nunique()
        eligible_cats = cat_counts[cat_counts >= cat_bill_thresh].sort_values(ascending=False)
        if eligible_cats.empty:
            st.caption(f"No categories have \u2265 {cat_bill_thresh} bills.")
        else:
            _topc = eligible_cats.head(10).index.tolist()
            _figh, _heat_facts = _build_impact_heatmap(df_f, _asp_col, _asp_lbl, _topc, topN_benef, min_cell_thresh)
            if _figh is None:
                st.caption(f"No usable beneficiary \u00d7 {_dir_choice.lower()}-aspect data "
                           "(rows with no listed beneficiary or aspect are excluded).")
            else:
                plotly_chart(_figh, title=f"Beneficiary Impact ({_dir_choice})", use_container_width=True, config={"displayModeBar": False})
                _sr_bi = (f"Heatmap showing which beneficiaries are affected by bills that "
                          f"{_dir_choice.lower()} something, within {selected_cat}.")
                chart_aux(f"Beneficiary Impact ({_dir_choice})", _sr_bi, _heat_facts, "heat")

    # ---- Ideological Alignment ----
    # ── #21 Ideological Alignment strip (selected category) ──────────────────────
    IDEOLOGY_COL = "ideological_alignment_simplified"
    if IDEOLOGY_COL in df_f.columns:
        _idf = df_f[df_f[CAT_MAIN].astype(str) == str(selected_cat)]
        _ide = _idf[IDEOLOGY_COL].dropna().astype(str).str.strip()
        _ide = _ide[_ide.str.lower().isin(["progressive", "conservative", "bipartisan"])]
        if not _ide.empty:
            _counts = _ide.str.title().value_counts()
            _total = int(_counts.sum())
            _colmap = {"Progressive": C_POSITIVE, "Bipartisan": C_STABLE, "Conservative": C_NEGATIVE}
            st.markdown(f"**Ideological Alignment — {selected_cat}**  "
                        f"<span style='color:#454F5A;font-size:.8rem'>({_total} bills with an alignment label)</span>",
                        unsafe_allow_html=True)
            fig_ide = go.Figure()
            for _lab in ["Progressive", "Bipartisan", "Conservative"]:
                _v = int(_counts.get(_lab, 0))
                if _v == 0:
                    continue
                fig_ide.add_trace(go.Bar(
                    x=[_v], y=["align"], orientation="h", name=_lab,
                    marker_color=_colmap[_lab],
                    text=[f"{_lab} {_v / _total * 100:.0f}%"],
                    textposition="inside", insidetextanchor="middle",
                    textfont=dict(color="white", size=12),
                    hovertemplate=f"{_lab}: {_v} bills<extra></extra>"))
            fig_ide.update_layout(barmode="stack", template=PLOTLY_TEMPLATE,
                height=80, margin=dict(l=8, r=8, t=2, b=2), showlegend=False,
                plot_bgcolor="white", paper_bgcolor="white",
                xaxis=dict(visible=False), yaxis=dict(visible=False))
            plotly_chart(fig_ide, title=f"Ideological alignment — {selected_cat}", use_container_width=True, config={"displayModeBar": False})
            # This chart had no text alternative at all; its only data was text
            # drawn inside the SVG.
            chart_aux(f"Ideological alignment — {selected_cat}",
                      f"{_total} bills in {selected_cat} carry an alignment label: "
                      + "; ".join(f"{_l} {int(_counts.get(_l, 0)):,} ({int(_counts.get(_l, 0)) / _total * 100:.0f}%)"
                                  for _l in ["Progressive", "Bipartisan", "Conservative"] if int(_counts.get(_l, 0))) + ".",
                      None, "ideology")

    # ---- AI Briefing ----
    # ── AI briefing panel (Azure OpenAI via ai_briefing.py) ──────────────────────
    # Grounded in the filtered category data. Dormant until AZURE_OPENAI_* env vars
    # are set; is_configured() then flips it on with no code change.
    with st.expander(f"AI Briefing — {selected_cat}", expanded=False):
        st.caption("Generates a plain-English summary for the selected category, grounded in the filtered data, using Azure OpenAI.")
        if st.button("Generate Briefing", key="gen_briefing"):
            if not ai_briefing.is_configured():
                st.info("AI Briefing is not yet configured. Set AZURE_OPENAI_ENDPOINT and "
                        "AZURE_OPENAI_KEY in Azure App Service, under Environment variables.")
            else:
                df_cat_brief = df_f[df_f[CAT_MAIN].astype(str) == str(selected_cat)].copy()
                bill_count = int(df_cat_brief[BILL_ID_COL].nunique())
                # Gather top keywords from all KW_SOURCES
                top_kws = []
                for col in KW_SOURCES.values():
                    if safe_col(df_cat_brief, col):
                        terms = explode_terms(df_cat_brief, col, stopwords=STOPWORDS)
                        if not terms.empty:
                            top_kws += terms["term"].value_counts().head(5).index.tolist()
                top_kws = list(dict.fromkeys(top_kws))[:10]
                # Top beneficiaries
                top_bens = []
                if safe_col(df_cat_brief, BENEF_COL):
                    raw = df_cat_brief[BENEF_COL].dropna().astype(str)
                    raw = raw[~raw.str.lower().str.strip().isin(_BENEF_NULL)]
                    top_bens = raw.value_counts().head(5).index.tolist()
                # Top increasing aspects
                top_inc_kws = []
                if safe_col(df_cat_brief, INC_COL):
                    inc_terms = explode_terms(df_cat_brief, INC_COL, stopwords=STOPWORDS)
                    if not inc_terms.empty:
                        top_inc_kws = inc_terms["term"].value_counts().head(6).index.tolist()
                # Top decreasing aspects
                top_dec_kws = []
                if safe_col(df_cat_brief, DEC_COL):
                    dec_terms = explode_terms(df_cat_brief, DEC_COL, stopwords=STOPWORDS)
                    if not dec_terms.empty:
                        top_dec_kws = dec_terms["term"].value_counts().head(4).index.tolist()

                # #5: five-year historical context (yearly bill counts) for this category
                _hist = df_full[df_full[CAT_MAIN].astype(str) == str(selected_cat)].copy()
                _hist_yearly = _hist.groupby(_hist[DATE_COL].dt.year)[BILL_ID_COL].nunique().sort_index()
                _hist_str = ", ".join(f"{int(y)}: {int(c)} bills" for y, c in _hist_yearly.items()) or "N/A"

                prompt = f"""You are a policy analyst at the University of Illinois. Write a 4-5 sentence plain-English briefing for an administrator. Frame it as: what is typical historically for this category, then what stands out right now in the selected period.

    Category: "{selected_cat}"
    Selected period: {start_date} to {end_date}

    What's normal (five-year history, bills per year): {_hist_str}

    In the selected period:
    - Bills in category: {bill_count}
    - Top legislative topics: {", ".join(top_kws) if top_kws else "N/A"}
    - Main beneficiaries: {", ".join(top_bens) if top_bens else "N/A"}
    - Top increasing aspects: {", ".join(top_inc_kws) if top_inc_kws else "N/A"}
    - Top decreasing aspects: {", ".join(top_dec_kws) if top_dec_kws else "N/A"}

    Begin with the typical historical pattern, then highlight what is different or notable in the selected period and what it means for the University of Illinois. Be specific and concrete, use active voice, and write prose paragraphs (no bullet points). If the data is thin, say so plainly."""

                try:
                    with st.spinner("Generating briefing…"):
                        briefing_text = ai_briefing.generate(prompt, max_tokens=450)
                    st.markdown(briefing_text)
                except Exception as e:
                    st.error(f"Briefing generation failed: {e}")

    # ---- Top 5 Bills to Watch ----
    # ── #22 Top 5 Bills to Watch (selected category) ─────────────────────────────
    st.markdown("## Top 5 Bills to Watch")
    st.caption(f"Highest reaches-vote-likelihood bills in \u201c{selected_cat}\u201d \u2014 gaining momentum first.")
    _preds = load_predictions_for_trends(tuple(sorted(AZURE_SESSION_URLS.keys())))
    if _preds.empty:
        st.info("Prediction data isn't available for this view yet. It populates from the daily model run.")
    else:
        _cat_bills = (df_f[df_f[CAT_MAIN].astype(str) == str(selected_cat)]
                      [[BILL_ID_COL, "title"]].copy())
        _cat_bills[BILL_ID_COL] = _cat_bills[BILL_ID_COL].astype(str)
        _m = _cat_bills.merge(_preds, left_on=BILL_ID_COL, right_on="bill_id", how="inner")
        if _m.empty:
            st.info(f"No scored bills in \u201c{selected_cat}\u201d yet.")
        else:
            _m["y_prob"] = pd.to_numeric(_m["y_prob"], errors="coerce").fillna(0.0)
            _gaining = _m[_m["momentum"].astype(str).str.lower() == "gaining"]
            _pool = _gaining if not _gaining.empty else _m
            _top = (_pool.sort_values("y_prob", ascending=False)
                    .drop_duplicates(BILL_ID_COL).head(5))
            if _gaining.empty:
                st.caption("No bills are currently gaining momentum in this category \u2014 "
                           "showing the highest reaches-vote-likelihood bills instead.")
            _disp = pd.DataFrame({
                "Bill": _top[BILL_NUM_COL].astype(str).values,
                "Title": _top["title"].astype(str).str.slice(0, 70).values,
                "Reaches-Vote Likelihood": ((_top["y_prob"] * 100).round(0).astype(int).astype(str) + "%").values,
                "Momentum": _top["momentum"].astype(str).values,
            })
            a11y_table(_disp, download="Top 5 Bills to Watch")
            st.caption("Open the **Bill Predictions** page for full probability trails and action history.")
            _sr_t5 = (f"Table of the top {len(_disp)} highest reaches-vote-likelihood bills in {selected_cat}, "
                      "gaining-momentum first.")
            _ai_t5 = ("KEY FACTS (bill: title, reaches-vote likelihood, momentum):\n"
                      + "\n".join(f"- {r['Bill']}: {r['Title']}, {r['Reaches-Vote Likelihood']}, {r['Momentum']}"
                                  for _, r in _disp.iterrows()))
            chart_aux("Top 5 Bills to Watch", _sr_t5, _ai_t5, "top5")

