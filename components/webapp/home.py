"""
home.py — IGPA Legislative Intelligence Platform Home Page v5
Pure Streamlit — no HTML in feature cards. Consistent navy buttons.
"""
import streamlit as st
import pandas as pd
from data_catalog import (GOLD_URLS, PREDICTION_URLS, SESSION_LABELS,
                          COMPLETE_SESSIONS, SNAPSHOT_URL)

st.markdown("""
<style>
/* Bug 26 follow-up: one font app-wide. Inter (a Google Fonts download) was
   used on some pages and Streamlit's built-in Source Sans Pro on others. */
.block-container{
  padding-top:0.8rem !important;
  padding-left:1.2rem !important;
  padding-right:1.2rem !important;
  max-width:1100px;
}
header[data-testid="stHeader"]{background:transparent !important;height:0 !important;}
[data-testid="stToolbar"]{display:none !important;}
.sess-row{
  background:#FFFFFF;border:1px solid #E2E8F0;border-radius:10px;
  padding:12px 16px;display:flex;align-items:center;gap:12px;margin-bottom:8px;
}
.sess-stat{font-size:11px;color:#454F5A;padding:2px 8px;
           background:#F8FAFC;border-radius:5px;border:1px solid #E2E8F0;}
</style>
""", unsafe_allow_html=True)

# ── Accessibility chrome (ReQtest audit 2026-09-14) ──────────────────────────
# Issues 1/2 (main + nav landmarks), 16 (disabled-state contrast),
# 17/19 (keyboard focus on chart and dataframe overlay controls).
from a11y import apply_page_a11y, NEW_TAB
apply_page_a11y('Home')

# ── Live stats ────────────────────────────────────────────────────────────────
def _blob_last_modified_date(url):
    """Actual upload time of an Azure blob — reflects the real data-refresh date.
    Verified on deploy; falls back to latest status_date if the header is unavailable."""
    try:
        import requests
        r = requests.head(url, timeout=10, allow_redirects=True)
        lm = r.headers.get("Last-Modified")
        if lm:
            d = pd.to_datetime(lm, errors="coerce")
            return d.date() if pd.notna(d) else None
    except Exception:
        pass
    return None

@st.cache_data(show_spinner=False, ttl=3600)
def load_home_stats():
    stats = {"total_bills": 0, "session_count": 0, "last_updated": None}
    for sid, url in GOLD_URLS.items():
        if not url:
            continue
        try:
            gdf = pd.read_parquet(url, columns=["status_date"])
        except Exception:
            try:
                gdf = pd.read_parquet(url)
            except Exception:
                continue
        stats["total_bills"]   += len(gdf)
        stats["session_count"] += 1
        # Freshness: prefer the blob's upload time; fall back to the latest bill action date.
        cand = _blob_last_modified_date(url)
        if cand is None and "status_date" in gdf.columns:
            sd = pd.to_datetime(gdf["status_date"], errors="coerce").max()
            cand = sd.date() if pd.notna(sd) else None
        if cand is not None and (stats["last_updated"] is None or cand > stats["last_updated"]):
            stats["last_updated"] = cand
    return stats


@st.cache_data(show_spinner=False, ttl=3600)
def load_active_session_stats():
    """Current-session counts from the active-bills snapshot:
    active total, predicted-to-reach-vote (>=95%), Introduced count,
    and a full breakdown by legislative status."""
    out = {"active_total": 0, "high_vote": 0, "introduced": 0,
           "by_status": {}, "hist": []}
    try:
        df = pd.read_parquet(SNAPSHOT_URL)
    except Exception:
        return out
    out["active_total"] = int(len(df))
    if "passage_prob" in df.columns:
        p = pd.to_numeric(df["passage_prob"], errors="coerce").dropna()
        out["high_vote"] = int((p >= 0.95).sum())
        # 10 bins, 0-10% ... 90-100%
        import numpy as _np
        counts, _ = _np.histogram(p.clip(0, 1), bins=[i/10 for i in range(11)])
        out["hist"] = [int(c) for c in counts]
    if "status_desc" in df.columns:
        counts = df["status_desc"].astype(str).value_counts()
        out["by_status"] = {k: int(v) for k, v in counts.items()}
        out["introduced"] = int(counts.get("Introduced", 0))
    return out

@st.cache_data(show_spinner=False, ttl=3600)
def load_session_stats(sid, gold_url, pred_url):
    bill_count = reached_vote = 0
    last_update = None
    try:
        bill_count = len(pd.read_parquet(gold_url))
    except Exception:
        pass
    if pred_url:
        try:
            pf = pd.read_parquet(pred_url)
            if "reached_vote" in pf.columns:
                reached_vote = int(pf["reached_vote"].sum())
            if "generated_at" in pf.columns:
                last_update = pd.to_datetime(
                    pf["generated_at"].dropna(), errors="coerce").max()
        except Exception:
            pass
    return bill_count, reached_vote, last_update

with st.spinner("Loading…"):
    hs = load_home_stats()
    ass = load_active_session_stats()

total_bills      = hs["total_bills"]
session_count    = hs["session_count"]
last_updated     = hs["last_updated"]
last_updated_str = last_updated.strftime("%b %d, %Y") if last_updated else "—"

# ── Mini histogram of reaches-vote likelihood (for the hero tile) ────────────
_hist = ass.get("hist", [])
if _hist:
    _hmax = max(_hist) or 1
    # Bug 15: the bars carried their numbers only in hover tooltips. The tile
    # now speaks every band; the bars themselves are decorative.
    _hist_sr = ("Reaches-vote likelihood distribution, active bills: "
                + "; ".join(f"{i*10} to {(i+1)*10} percent, {c:,} bills" for i, c in enumerate(_hist)) + ".")
    _hist_bars = "".join(
        f'<div title="{i*10}–{(i+1)*10}%: {c:,} bills" '
        f'style="flex:1;background:rgba(255,255,255,.55);border-radius:2px 2px 0 0;'
        f'height:{max(3, round(c/_hmax*34))}px;"></div>'
        for i, c in enumerate(_hist))
else:
    _hist_bars = ""
    _hist_sr = ""

# ── Hero ──────────────────────────────────────────────────────────────────────
st.markdown(f"""
<div style="background:linear-gradient(135deg,#0F1F38 0%,#1B4B5A 55%,#2D6A7A 100%);
            border-radius:14px;padding:28px 32px;margin-bottom:18px;">
  <div style="color:rgba(255,255,255,.92);font-size:10px;font-weight:700;
              letter-spacing:.14em;text-transform:uppercase;margin-bottom:8px;">
    Institute of Government and Public Affairs · University of Illinois System
  </div>
  <h1 style="margin:0;padding:0;color:#FFFFFF;font-size:28px;font-weight:800;
              letter-spacing:-.03em;line-height:1.15;margin-bottom:10px;">
    Legislative Intelligence Platform
  </h1>
  <div style="color:rgba(255,255,255,.92);font-size:13px;
              line-height:1.7;max-width:580px;margin-bottom:20px;">
    AI-powered analysis of Illinois state legislation — tracking bills,
    predicting outcomes, and surfacing policy impacts for researchers,
    analysts, and policy teams across UIC, UIS, and UIUC.
  </div>
  <div style="display:flex;gap:10px;flex-wrap:wrap;">
    <div style="background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.2);
                border-radius:10px;padding:10px 18px;text-align:center;min-width:90px;">
      <div class="kpi-read">
        <span class="a11y-sr">{total_bills:,} bills analyzed</span>
        <span aria-hidden="true" style="display:block;color:#FFFFFF;font-size:22px;font-weight:800;line-height:1">{total_bills:,}</span>
        <span aria-hidden="true" style="display:block;color:rgba(255,255,255,.88);font-size:10px;margin-top:3px;font-weight:500">Bills analyzed</span>
      </div>
    </div>
    <div style="background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.2);
                border-radius:10px;padding:10px 18px;text-align:center;min-width:90px;">
      <div class="kpi-read">
        <span class="a11y-sr">{ass['active_total']:,} active bills in the 104th General Assembly</span>
        <span aria-hidden="true" style="display:block;color:#FFFFFF;font-size:22px;font-weight:800;line-height:1">{ass['active_total']:,}</span>
        <span aria-hidden="true" style="display:block;color:rgba(255,255,255,.88);font-size:10px;margin-top:3px;font-weight:500">Active bills (104th GA)</span>
      </div>
    </div>
    <div style="background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.2);
                border-radius:10px;padding:10px 14px;min-width:200px;">
      <span class="a11y-sr">{_hist_sr}</span>
      <div aria-hidden="true" style="display:flex;align-items:flex-end;gap:3px;height:34px;">{_hist_bars}</div>
      <div aria-hidden="true" style="color:rgba(255,255,255,.88);font-size:10px;margin-top:5px;font-weight:500;text-align:center">Reaches-vote likelihood · distribution</div>
    </div>
    <div style="background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.2);
                border-radius:10px;padding:10px 18px;text-align:center;min-width:90px;">
      <div class="kpi-read">
        <span class="a11y-sr">{session_count} sessions loaded</span>
        <span aria-hidden="true" style="display:block;color:#FFFFFF;font-size:22px;font-weight:800;line-height:1">{session_count}</span>
        <span aria-hidden="true" style="display:block;color:rgba(255,255,255,.88);font-size:10px;margin-top:3px;font-weight:500">Sessions loaded</span>
      </div>
    </div>
    <div style="background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.2);
                border-radius:10px;padding:10px 18px;text-align:center;min-width:90px;">
      <div class="kpi-read">
        <span class="a11y-sr">Updated daily, most recently {last_updated_str}</span>
        <span aria-hidden="true" style="display:block;color:#FFFFFF;font-size:22px;font-weight:800;line-height:1">Daily</span>
        <span aria-hidden="true" style="display:block;color:rgba(255,255,255,.88);font-size:10px;margin-top:3px;font-weight:500">Updated · {last_updated_str}</span>
      </div>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

# ── Active-session status breakdown ───────────────────────────────────────────
if ass.get("by_status"):
    # Standardized legislative order; unknown statuses appended after.
    _ORDER = ["Introduced", "Assigned", "Committee", "Engrossed",
              "Enrolled", "Passed", "Vetoed", "Failed"]
    _bs = ass["by_status"]
    _ordered = [(k, _bs[k]) for k in _ORDER if k in _bs]
    _ordered += [(k, v) for k, v in sorted(_bs.items(), key=lambda x: -x[1])
                 if k not in _ORDER]
    # Round 3 (bug 13): Mark found the <ul> and the stacked spans still read
    # as "9,134" then "Introduced", plus list enter/exit noise. Each chip now
    # speaks one phrase, "9,134 introduced"; the visual parts are hidden.
    _chips = "".join(
        f'<div class="kpi-read" style="background:#F1F5F9;border:1px solid #E2E8F0;border-radius:8px;'
        f'padding:8px 14px;text-align:center;min-width:78px;">'
        f'<span class="a11y-sr">{v:,} {k.lower()}</span>'
        f'<span aria-hidden="true" style="font-size:17px;font-weight:800;color:#0F1F38;line-height:1;'
        f'display:block">{v:,}</span>'
        f'<span aria-hidden="true" style="font-size:10px;color:#454F5A;margin-top:2px;font-weight:600;'
        f'display:block">{k}</span></div>'
        for k, v in _ordered)
    st.markdown(
        '<h2 style="font-size:11px;font-weight:700;letter-spacing:.08em;'
        'text-transform:uppercase;color:#454F5A;margin:2px 0 8px;padding:0;">'
        'Active Bills by Status · 104th General Assembly</h2>'
        f'<div style="display:flex;gap:8px;flex-wrap:wrap;margin:0 0 20px;'
        f'padding:0;">{_chips}</div>',
        unsafe_allow_html=True)

# ── Feature cards — pure Streamlit, no HTML ───────────────────────────────────
st.markdown("## Platform Features")

# Dynamic corpus size for the Explorer card — rounds to nearest thousand, updates every refresh
_explorer_bills = f"{round(total_bills/1000)}K+" if total_bills >= 1000 else f"{total_bills}"

FEATURES = [
    ("📌", "My Tracker",               "views/6_My_Tracker.py",
     "Your personal legislative watchlist. Track bills, monitor topics by keyword, and get AI-generated digest emails."),
    ("📊", "Trends",                   "views/1_Trends.py",
     "Legislative activity, momentum, seasonal baselines, and policy direction analysis with AI briefings."),
    ("🔍", "Explorer",                 "views/2_Explorer.py",
     f"Semantic search over {_explorer_bills} bills. Filter by policy area, beneficiary group, impact, and passage likelihood."),
    ("🔥", "Bill Passage Predictions", "views/4_Active_Bills.py",
     "Live floor-vote likelihood, probability trail charts, action history, and momentum for active bills."),
    ("📋", "Validation",               "views/5_Validation.py",
     "Advance warning framework — how many days before passage the model first reached certainty."),
    ("🗺️","Roadmap",                  "views/7_Roadmap.py",
     "Upcoming features — email digests, AI network insights, floor vote summaries, multi-state expansion."),
]

row1 = st.columns(3, gap="medium")
row2 = st.columns(3, gap="medium")

# Audit issue 13: each card used to be three unrelated siblings — a bold
# markdown line, a caption, and a button, separated by a "---" rule that
# carried the grouping visually only. A screen reader read eighteen
# disconnected items. Each card is now a single <article> with a real heading
# and its description in one element, so the title and its text are announced
# together; the button follows inside the same container.
# Audit issue 10: the card title is now an <h3> under the "Platform Features"
# <h2>, so the outline is continuous.
# Audit bug 7: icons removed from card headings outright. aria-hidden would
# satisfy the screen-reader half, but the ticket also notes emoji render
# inconsistently across devices, and there is no reason to keep them.
for i, (icon, title, page, desc) in enumerate(FEATURES):
    col = row1[i] if i < 3 else row2[i - 3]
    with col:
        with st.container(border=True):
            st.markdown(
                f'<article>'
                f'<h3 style="font-size:1rem;font-weight:700;margin:0 0 .35rem;'
                f'padding:0;color:#0F1F38;line-height:1.3">'
                f'{title}</h3>'
                f'<p style="font-size:.82rem;color:#454F5A;margin:0 0 .6rem;'
                f'line-height:1.5">{desc}</p>'
                f'</article>',
                unsafe_allow_html=True)
            if st.button(f"Open {title}", key=f"btn_{i}",
                         use_container_width=True):
                st.switch_page(page)

# ── Available Sessions ────────────────────────────────────────────────────────
st.markdown("## Available Sessions")

SESSION_CONFIG = [
    (2176, "104th General Assembly", "2025–2026"),
    (2020, "103rd General Assembly", "2023–2024"),
    (1801, "102nd General Assembly", "2021–2022"),
    (1630, "101st General Assembly", "2019–2020"),
]
ACTIVE_SESSION_ID = 2176  # current General Assembly

def _session_status(sid):
    if sid == ACTIVE_SESSION_ID:
        return "active"
    return "complete" if sid in COMPLETE_SESSIONS else "processing"

STATUS_STYLES = {
    "active":     ("#D97706", "#FEF3C7", "#92400E", "Active Session"),
    "complete":   ("#16A34A", "#DCFCE7", "#15803D", "Complete"),
    "processing": ("#6D28D9", "#EDE9FE", "#5B21B6", "Processing"),
}

for sid, label, years in SESSION_CONFIG:
    status = _session_status(sid)
    dot_c, bg_c, txt_c, status_txt = STATUS_STYLES[status]
    gold_url = GOLD_URLS.get(sid)
    pred_url = PREDICTION_URLS.get(sid)

    bill_count = reached_vote = 0
    last_upd   = None
    if gold_url:
        bill_count, reached_vote, last_upd = load_session_stats(
            sid, gold_url, pred_url or "")

    upd_str    = last_upd.strftime("Updated %b %d") if last_upd is not None and pd.notna(last_upd) else ""
    stats_html = ""
    if bill_count:
        stats_html += f'<span class="sess-stat">{bill_count:,} bills</span>'
    if reached_vote:
        stats_html += f'<span class="sess-stat">{reached_vote:,} reached vote</span>'
    if upd_str:
        stats_html += f'<span class="sess-stat">{upd_str}</span>'
    # Build badge as Python string to avoid multi-line f-string rendering issues
    badge = f'<span style="background:{bg_c};color:{txt_c};font-size:11px;font-weight:600;padding:3px 10px;border-radius:6px">{status_txt}</span>'
    stats_html += badge

    st.markdown(f"""
<div class="sess-row">
  <div style="width:9px;height:9px;border-radius:50%;background:{dot_c};flex-shrink:0"></div>
  <div style="flex:1">
    <span style="font-size:14px;font-weight:600;color:#0F172A">{label}</span>
    <span style="font-size:12px;color:#454F5A;margin-left:8px">{years}</span>
  </div>
  <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
    {stats_html}
  </div>
</div>""", unsafe_allow_html=True)

# ── Footer ────────────────────────────────────────────────────────────────────
st.markdown(f"""
<div style="border-top:1px solid #E2E8F0;padding-top:12px;margin-top:16px;
            display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;
            font-size:11px;color:#454F5A;">
  <div>University of Illinois System · Institute of Government and Public Affairs ·
    <a href="https://igpa.uillinois.edu" target="_blank"
       style="color:#13294B;font-weight:500">igpa.uillinois.edu{NEW_TAB}</a>
  </div>
  <div>Stage 2 survival model · Last updated {last_updated_str}</div>
</div>
""", unsafe_allow_html=True)
