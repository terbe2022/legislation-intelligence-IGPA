"""
pages/5_Validation.py — Bill Prediction Validation
==================================================

The question this page answers is not "is the model accurate." It is:

    If we treat a bill as called once its score passes X, how often are we
    right, and how many days of warning does that buy?

Move the threshold and both numbers move against each other. A higher bar is
right more often but tells you later, because late in a bill's life a high score
is largely a restatement of evidence already on the record.

ARCHITECTURE
------------
This page performs no calculation. Precision, recall, warning days, histograms
and the miss list are all precomputed on the pipeline server by
build_validation_tracker.py and published to Azure gold as small aggregates
(~65 KB total). Three reasons:

  1. The causal guard - the rule that a threshold crossing only counts if it
     happened BEFORE the floor vote - lives in exactly one place. A second
     implementation here would drift from it.
  2. A published number is auditable. "Precision was 0.81 at 0.75 on 2 Sept"
     points at a versioned file, not at whatever the app recalculated on load.
  3. The crossings table is >100k rows. This tier cannot hold it alongside
     FAISS and the embedder.

REQUIRES in data_catalog.py:
    VALIDATION_CURVE_URL       stage2_validation_curve.parquet
    VALIDATION_HIST_URL        stage2_validation_warning_hist.parquet
    VALIDATION_MISSES_URL      stage2_validation_misses.parquet
    VALIDATION_BY_ACTIONS_URL  stage2_validation_by_actions.parquet
    VALIDATION_AUC_DEPTH_URL   stage2_validation_auc_by_depth.parquet
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import streamlit as st

from auth_utils import require_auth
# Audit issue 15: none of this page's four charts had any text equivalent.
from chart_a11y import inject_sr_css, sr_chart_table, plotly_chart, a11y_table
import plotly.graph_objects as go
from app_theme import style_fig
from chart_a11y import chart_aux


def _ordered_chart(series, title, x_title, y_title, color, kind="bar", caption="", y_range=None):
    """Plotly replacement for st.bar_chart / st.line_chart (walkthrough finding).

    Streamlit's built-in charts sort category labels alphabetically, so bucket
    axes read "0-7, 121-180, 15-21, …" while the Chart details table below them
    was in true order. Plotly keeps the order given, goes through the
    accessible wrapper (named, cues, data table) and matches every other chart.
    """
    xs = [str(i) for i in series.index]
    ys = list(series.values)
    tr = (go.Bar(x=xs, y=ys, marker_color=color, name=y_title) if kind == "bar"
          else go.Scatter(x=xs, y=ys, mode="lines+markers", line=dict(color=color, width=3),
                          marker=dict(size=8), name=y_title))
    fig = go.Figure(tr)
    fig.update_layout(height=300, showlegend=False, margin=dict(l=8, r=8, t=12, b=8))
    fig.update_xaxes(title_text=x_title, type="category", categoryorder="array", categoryarray=xs)
    fig.update_yaxes(title_text=y_title, range=y_range)
    style_fig(fig)
    plotly_chart(fig, title=title, use_container_width=True)
    chart_aux(title, caption, None, title)


def _bucket_key(b):
    """Numeric order for day/action buckets: "0-7" < "8-14" < "730+"."""
    import re as _re
    m = _re.match(r"\s*(\d+)", str(b))
    return int(m.group(1)) if m else 10**9


# Human column names for every table on this page (WCAG 2.4.6). Code names like
# "n_called" are read aloud as "n underscore called".
_COLS = {
    "action_bucket": "Actions recorded", "n_bills": "Bills", "n_predictions": "Predictions",
    "base_rate": "Base rate", "roc_auc": "ROC AUC", "auc_lo": "ROC AUC, 95% CI low",
    "auc_hi": "ROC AUC, 95% CI high", "n_called": "Bills called", "precision": "How often right",
    "precision_lo": "How often right, 95% CI low", "precision_hi": "How often right, 95% CI high",
    "recall": "Recall", "coverage": "Share called", "median_warning_days": "Median warning (days)",
    "p25_warning_days": "Warning, 25th percentile (days)", "p75_warning_days": "Warning, 75th percentile (days)",
    "bill_number": "Bill", "session_id": "Session ID", "classification": "Classification",
    "prob_at_crossing": "Probability at crossing", "peak_prob_before_outcome": "Peak probability before outcome",
    "outcome_date": "Outcome date", "threshold": "Threshold",
}


def _human(df):
    d = df.rename(columns={k: v for k, v in _COLS.items() if k in df.columns})
    if "Classification" in d.columns:
        d["Classification"] = d["Classification"].astype(str).str.replace("_", " ").str.capitalize()
    return d
from data_catalog import SESSION_LABELS

try:
    from data_catalog import (
        VALIDATION_CURVE_URL, VALIDATION_HIST_URL, VALIDATION_MISSES_URL,
        VALIDATION_BY_ACTIONS_URL, VALIDATION_AUC_DEPTH_URL,
    )
except ImportError:
    VALIDATION_CURVE_URL = VALIDATION_HIST_URL = VALIDATION_MISSES_URL = None
    VALIDATION_BY_ACTIONS_URL = VALIDATION_AUC_DEPTH_URL = None

require_auth()

# ── Accessibility chrome (ReQtest audit 2026-09-14) ──────────────────────────
# Issues 1/2 (main + nav landmarks), 16 (disabled-state contrast),
# 17/19 (keyboard focus on chart and dataframe overlay controls).
# Must run after require_auth() so it is not emitted on the login gate.
from a11y import apply_page_a11y
apply_page_a11y('Validation')
inject_sr_css()   # audit issue 15

C_BLUE, C_ORANGE, C_GRAY, C_GREEN, C_RED = (
    "#1B4B5A", "#E07B2A", "#6E5A50", "#3B6D11", "#F55449"   # C_GRAY darkened, audit bug 3
)

st.markdown("""
<style>
.block-container{padding-top:0.8rem !important;padding-left:0.9rem !important;
                  padding-right:0.9rem !important;}
header[data-testid="stHeader"]{background:transparent !important;height:0 !important;}
[data-testid="stToolbar"]{display:none !important;}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div style="background:linear-gradient(90deg,#0F1F38 0%,#1B4B5A 60%,#6E5A50 100%);
            padding:14px 18px;border-radius:12px;margin-bottom:14px;">
  <h1 style="margin:0;padding:0;color:#fff;font-weight:800;font-size:20px;">Bill Prediction Validation</h1>
  <div style="color:rgba(255,255,255,.92);font-size:12px;margin-top:3px;">
    Not whether the model was right, but how far in advance it knew.
  </div>
</div>
""", unsafe_allow_html=True)


@st.cache_data(show_spinner=False, ttl=3600)
def _load(url):
    return pd.read_parquet(url) if url else pd.DataFrame()


if not VALIDATION_CURVE_URL:
    st.markdown("""
<div style="background:#F8F4ED;border-left:4px solid #8E7970;border-radius:0;
            padding:16px 18px;margin:12px 0;">
<div style="font-weight:600;font-size:14px;margin-bottom:8px;">Validation not yet published</div>
<div style="font-size:13px;color:#444;line-height:1.7;">
On the pipeline server, run:<br>
<code>python3 build_validation_tracker.py --threshold 0.75 --upload</code><br><br>
Then add the five SAS URLs to <code>data_catalog.py</code>.
</div>
</div>
""", unsafe_allow_html=True)
    st.stop()

curve = _load(VALIDATION_CURVE_URL)
hist = _load(VALIDATION_HIST_URL)
misses = _load(VALIDATION_MISSES_URL)
by_actions = _load(VALIDATION_BY_ACTIONS_URL)
auc_depth = _load(VALIDATION_AUC_DEPTH_URL)
ACTION_ORDER = ["1", "2-3", "4-5", "6-8", "9-12", "13-20", "21-30", "31+"]

if curve.empty:
    st.warning("Validation curve is empty. Re-run build_validation_tracker.py.")
    st.stop()

# ── Scope ────────────────────────────────────────────────────────────────────
# Restored from commit 51f1eeb (Stage 2 retrain, 2026-09-28), which round 3 of
# the accessibility work overwrote by mistake.
# The Stage 2 model trained on the 96th-101st GA. The 102nd and 103rd were held
# out, so they are the honest views; the page opens on the 103rd (the test set).
HELD_OUT = {"1801", "2020"}
LIVE = {"2176"}
# LegiScan session IDs are not chronological (112 is the 98th GA, 999 the 97th)
_NEWEST_FIRST = ["2176", "2020", "1801", "1630", "1407", "1127", "112", "999", "21"]
def _scope_label(s):
    if s == "ALL":
        return "All sessions (includes training data)"
    s = str(s)
    name = SESSION_LABELS.get(int(s), s) if s.isdigit() else s
    if s in HELD_OUT:
        return f"{name} — held out"
    if s in LIVE:
        return f"{name} — live session"
    return f"{name} — training data"
_all = [str(s) for s in curve["session_scope"].unique()]
curve["session_scope"] = curve["session_scope"].astype(str)
scopes = ([s for s in ["2020", "1801"] if s in _all]
          + ["ALL"]
          + [s for s in _NEWEST_FIRST if s in _all and s not in HELD_OUT])
scope = st.selectbox("Sessions", scopes, index=0, format_func=_scope_label)
c = curve[curve["session_scope"] == scope].sort_values("threshold")
if c.empty:
    st.warning("No validation data for that scope.")
    st.stop()

# ── Threshold ────────────────────────────────────────────────────────────────
st.markdown("## Certainty threshold")
st.caption("The score at which we would treat a bill as called.")
options = [float(x) for x in c["threshold"].tolist()]
# Audit issue 12 (WCAG 3.3.2): the label was empty and collapsed, so the
# slider had no accessible name. The "Certainty threshold" heading above it
# conveyed the meaning visually only.
thr = st.select_slider(
    "Certainty threshold", options=options,
    value=min(options, key=lambda x: abs(x - 0.70)),
    format_func=lambda x: f"{x*100:.0f}%",
    help="The score at which we would treat a bill as called.",
)
row = c[np.isclose(c["threshold"], thr)].iloc[0]

pct = lambda v: "—" if pd.isna(v) else f"{v*100:.0f}%"
dys = lambda v: "—" if pd.isna(v) else f"{v:.0f}"

# Bug 13: spoken phrases for the tiles, computed here rather than nested
# inside the HTML f-string.
_ci = (f" · 95% CI {row['precision_lo']*100:.0f}–{row['precision_hi']*100:.0f}%"
       if 'precision_lo' in row.index and pd.notna(row.get('precision_lo')) else "")
_ci_sr = (f", 95 percent confidence interval {row['precision_lo']*100:.0f} to {row['precision_hi']*100:.0f} percent"
          if _ci else "")
_sr_warn = (f"Median advance warning: {dys(row['median_warning_days'])} days; middle half "
            f"{dys(row['p25_warning_days'])} to {dys(row['p75_warning_days'])} days")
_sr_prec = f"How often right: {pct(row['precision'])} of {int(row['n_called']):,} called{_ci_sr}"
_sr_rec = f"Of those that passed, caught: {pct(row['recall'])} (recall)"
_sr_cov = f"Share of bills called at this threshold: {pct(row['coverage'])}"

st.markdown(f"""
<div style="margin:10px 0 16px;">
  <div style="font-size:11px;font-weight:600;color:#454F5A;text-transform:uppercase;
              letter-spacing:.04em;margin-bottom:8px;">At {thr*100:.0f}% certainty</div>
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;">
    <div class="kpi-read" style="background:#EAF3DE;border-radius:10px;padding:12px;text-align:center;">
      <span class="a11y-sr">{_sr_warn}</span>
      <span aria-hidden="true" style="display:block;font-size:28px;font-weight:700;color:#3B6D11;">{dys(row['median_warning_days'])} days</span>
      <span aria-hidden="true" style="display:block;font-size:10px;color:#3B6D11;font-weight:600;margin-top:2px;">Median advance warning</span>
      <span aria-hidden="true" style="display:block;font-size:10px;color:#454F5A;">{dys(row['p25_warning_days'])}–{dys(row['p75_warning_days'])} middle half</span>
    </div>
    <div class="kpi-read" style="background:#F1EFE8;border-radius:10px;padding:12px;text-align:center;">
      <span class="a11y-sr">{_sr_prec}</span>
      <span aria-hidden="true" style="display:block;font-size:28px;font-weight:700;">{pct(row['precision'])}</span>
      <span aria-hidden="true" style="display:block;font-size:10px;color:#454F5A;font-weight:600;margin-top:2px;">How often right</span>
      <span aria-hidden="true" style="display:block;font-size:10px;color:#454F5A;">of {int(row['n_called']):,} called{_ci}</span>
    </div>
    <div class="kpi-read" style="background:#F1EFE8;border-radius:10px;padding:12px;text-align:center;">
      <span class="a11y-sr">{_sr_rec}</span>
      <span aria-hidden="true" style="display:block;font-size:28px;font-weight:700;">{pct(row['recall'])}</span>
      <span aria-hidden="true" style="display:block;font-size:10px;color:#454F5A;font-weight:600;margin-top:2px;">Of those that passed, caught</span>
      <span aria-hidden="true" style="display:block;font-size:10px;color:#454F5A;">recall</span>
    </div>
    <div class="kpi-read" style="background:#F1EFE8;border-radius:10px;padding:12px;text-align:center;">
      <span class="a11y-sr">{_sr_cov}</span>
      <span aria-hidden="true" style="display:block;font-size:28px;font-weight:700;color:#5F4E46;">{pct(row['coverage'])}</span>
      <span aria-hidden="true" style="display:block;font-size:10px;color:#454F5A;font-weight:600;margin-top:2px;">Share of bills called</span>
      <span aria-hidden="true" style="display:block;font-size:10px;color:#454F5A;">at this threshold</span>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

st.divider()

# ── Trade-off ────────────────────────────────────────────────────────────────
st.markdown("## Certainty is bought with time")
_tradeoff_df = pd.DataFrame({
    "Certainty": (c["threshold"] * 100).round(0).astype(int),
    "How often right (%)": (c["precision"] * 100).round(1),
    "Share called (%)": (c["coverage"] * 100).round(1),
    "Median warning (days)": c["median_warning_days"].round(0),
}).set_index("Certainty")

# Audit issue 4 (WCAG 1.4.1), found during verification.
# This was an st.line_chart, which draws every series as a plain solid line and
# offers no way to vary marker or dash. Its three colours (C_BLUE / C_GRAY /
# C_ORANGE) separated by only 2.33:1 at the closest pair, so two of the three
# lines were near-identical -- and no arrangement of three colours can be
# mutually distinguishable by luminance while all three still clear 3:1 against
# a white background. Colour alone cannot carry three series here.
# Rebuilt on the shared Plotly template instead, which cycles marker symbols
# (circle / square / diamond) and dash patterns (solid / dash / dot) per trace,
# so the series stay separable in greyscale and under any colour-vision
# deficiency. The hidden data table below is unchanged.
_SERIES_STYLE = [
    ("How often right (%)",   C_BLUE,   "solid", "circle"),
    ("Share called (%)",      C_ORANGE, "dash",  "square"),
    ("Median warning (days)", C_GRAY,   "dot",   "diamond"),
]
_fig_tradeoff = go.Figure()
for _name, _colour, _dash, _symbol in _SERIES_STYLE:
    _fig_tradeoff.add_trace(go.Scatter(
        x=_tradeoff_df.index, y=_tradeoff_df[_name], name=_name,
        mode="lines+markers",
        line=dict(color=_colour, dash=_dash, width=2.5),
        marker=dict(color=_colour, symbol=_symbol, size=7),
        hovertemplate=f"{_name}: %{{y}}<br>Certainty: %{{x}}%<extra></extra>",
    ))
_fig_tradeoff.update_layout(
    height=320,
    xaxis_title="Certainty threshold (%)",
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
)
style_fig(_fig_tradeoff)
plotly_chart(_fig_tradeoff, title="Certainty is bought with time", use_container_width=True,
                config={"displayModeBar": False})
sr_chart_table(
    "Certainty is bought with time", _tradeoff_df,
    "For each certainty threshold: how often the call was right, what share of "
    "bills were called, and the median days of warning.")
st.caption(
    "Raising the bar makes the model righter and later. The useful operating point "
    "is wherever the accuracy gain stops being worth the lost notice."
)

# ── Warning distribution ─────────────────────────────────────────────────────
h = hist[(hist["session_scope"] == scope) & np.isclose(hist["threshold"], thr)] \
    if not hist.empty else pd.DataFrame()
if not h.empty:
    st.divider()
    st.markdown(f"## Warning time at {thr*100:.0f}%")
    _warn_df = h.set_index("warning_bucket")["bills"]
    _warn_df = _warn_df.reindex(sorted(_warn_df.index, key=_bucket_key))
    _ordered_chart(_warn_df, f"Warning time at {thr*100:.0f}%", "Days of warning", "Bills", C_BLUE,
                   caption="Number of bills in each band of days between the first "
                           "threshold crossing and the floor vote.")
    st.caption("Days between the first threshold crossing and the floor vote.")

# ── Model quality by actions observed ────────────────────────────────────────
if not auc_depth.empty:
    st.divider()
    st.markdown("## How good is the model, given N actions?")
    a = auc_depth.copy()
    a["_o"] = a["action_bucket"].map({l: i for i, l in enumerate(ACTION_ORDER)})
    a = a.sort_values("_o")
    _auc_df = a.set_index("action_bucket")["roc_auc"]
    _ordered_chart(_auc_df, "How good is the model, given N actions?", "Actions recorded", "ROC AUC",
                   C_BLUE, kind="line", y_range=[0, 1],
                   caption="ROC AUC by how many legislative actions had been recorded "
                           "when the prediction was made. 0.5 is a coin flip.")
    lo = a.iloc[0]; hi = a.iloc[-1]
    ci = ""
    if "roc_auc_lo" in a.columns and pd.notna(hi.get("roc_auc_lo")):
        ci = (f" (95% CI {hi['roc_auc_lo']:.3f}–{hi['roc_auc_hi']:.3f} at "
              f"{hi['action_bucket']} actions)")
    st.caption(
        f"ROC AUC rises from {lo['roc_auc']:.3f} at {lo['action_bucket']} action(s) to "
        f"{hi['roc_auc']:.3f} at {hi['action_bucket']}{ci}. The model is close to useless "
        "at introduction and becomes useful around committee assignment. Deep buckets are "
        "survivorship-filtered: a bill decided at action 8 contributes no row at depth 20, "
        "so part of the rise reflects a changing population rather than better prediction."
    )
    with st.expander("Detail"):
        cols = [c for c in ["action_bucket","n_bills","n_predictions","base_rate",
                            "roc_auc","roc_auc_lo","roc_auc_hi"] if c in a.columns]
        a11y_table(_human(a[cols].round(4)), download="Model accuracy by actions recorded")

# ── Lead time by actions observed ────────────────────────────────────────────
ba = by_actions[np.isclose(by_actions["threshold"], thr)] if not by_actions.empty \
     else pd.DataFrame()
if not ba.empty:
    st.divider()
    st.markdown(f"## At {thr*100:.0f}% certainty, how early did we know?")
    b = ba.copy()
    b["_o"] = b["action_bucket"].map({l: i for i, l in enumerate(ACTION_ORDER)})
    b = b.sort_values("_o")
    _lead_df = b.set_index("action_bucket")["median_warning_days"]
    _ordered_chart(_lead_df, f"At {thr*100:.0f}% certainty, how early did we know?", "Actions recorded",
                   "Median warning (days)", C_GREEN,
                   caption="Median days of notice before the floor vote, by how many "
                           "actions had been recorded when the model crossed the threshold.")
    st.caption(
        "Median days between the threshold crossing and the floor vote, by how many "
        "actions had been recorded when the model crossed. Calls made earlier buy more "
        "notice and are right less often; the table below shows both."
    )
    cols = [c for c in ["action_bucket","n_called","precision","precision_lo",
                        "precision_hi","median_warning_days","p25_warning_days",
                        "p75_warning_days"] if c in b.columns]
    a11y_table(_human(b[cols].round(3)), download="Warning time by actions recorded")

# ── Misses ───────────────────────────────────────────────────────────────────
m = misses[np.isclose(misses["threshold"], thr)] if not misses.empty else pd.DataFrame()
if not m.empty:
    if scope != "ALL" and "session_id" in m.columns:
        m = m[m["session_id"].astype(str) == scope]
    if not m.empty:
        st.divider()
        st.markdown("## Where it was wrong")
        st.caption("Called but never reached a vote, or reached a vote without being called.")
        cols = [x for x in ["bill_number", "session_id", "classification",
                            "prob_at_crossing", "peak_prob_before_outcome", "outcome_date"]
                if x in m.columns]
        a11y_table(_human(m[cols]), max_rows=100, download="Where it was wrong")

# ── Method ───────────────────────────────────────────────────────────────────
with st.expander("How this is measured"):
    st.markdown(f"""
A bill counts as **called** when its score first reaches the selected threshold.

A call only counts if it happened **before** the floor vote. A bill whose score
crosses only after it has already been voted on is recorded as a miss, not a hit.
Without that rule this page would report near-perfect performance and mean nothing,
because a bill with a third-reading action in its history scores high trivially.

Bills in sessions that have not adjourned are **excluded**. We know they have not
reached a vote yet, not that they never will.

Outcomes come from LegiScan `status_desc` (Passed, Engrossed, Enrolled, Vetoed),
independent of anything the model produced. Engrossed is included because a bill
that passed one chamber and then stalled is exactly the case worth warning about;
excluding it would train on a survivorship-filtered sample.

Scores are a **ranking signal**. They are well ordered but overconfident in
absolute terms, so {thr*100:.0f}% means *relatively likely*, not literally
{thr*100:.0f} percent.

**These figures are for House and Senate bills (HB, SB) only.** Resolutions are
excluded: Senate resolutions pass at 95.5% and House resolutions at 86.7%, so
including them would inflate every number on this page for a category nobody needs
predicted. On bills alone the base rate is 13.2%.

**Which sessions are honest tests.** The model trained on the 96th–101st General
Assemblies (2009–2020). The 102nd and 103rd were held out, so their figures describe
how the model performs on legislation it has never seen; the page opens on the 103rd.
"All sessions" includes the training sessions and reads higher. The 104th is still in
progress, so bills that have not yet reached a vote are excluded rather than counted
as failures.
    """)
