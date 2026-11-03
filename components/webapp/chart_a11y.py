"""Shared chart and table accessibility helpers — ReQtest round 3.

Every chart goes through plotly_chart() (Plotly) or is followed by
sr_chart_table() (native st.bar_chart / st.line_chart). Each chart then:

  * is announced as ONE named image ("Bar chart: Category ranking. Chart
    details with a data table follow.") instead of an unlabelled canvas or a
    mass of SVG nodes (bug 15);
  * differs by dash, marker or hatch, not colour alone (bug 4);
  * has its legend kept off the axis labels (bug 5);
  * is followed by a native, collapsed "Chart details" expander holding a
    plain-language description and a real HTML <table> of the plotted values.

The description is DETERMINISTIC, computed from the figure's own data. The
old on-demand AI "Describe this chart" readout misreported values (bug 25), so
it is removed: a screen-reader user must get the same numbers a sighted user
sees, every time.

Tables that used st.dataframe (a canvas grid with no visible keyboard focus,
bug 15) render through a11y_table() instead: a real HTML table.

Usage
-----
    from chart_a11y import plotly_chart, chart_aux, a11y_table, inject_sr_css
    plotly_chart(fig, title="Category ranking", use_container_width=True)
    chart_aux("Category ranking", sr_sentence, ai_payload_unused, key="rank")
"""
import html
import re
import math

import pandas as pd
import streamlit as st

try:
    from app_theme import enforce_non_color_cues, fix_overlaps
except Exception:  # pragma: no cover
    def enforce_non_color_cues(fig): return fig
    def fix_overlaps(fig): return fig

_SR_ONLY_CSS = """
<style>
.sr-only { position:absolute !important; width:1px; height:1px; padding:0; margin:-1px;
           overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }
</style>
"""

# Most recent figure's table, consumed by the chart_aux() that follows it.
_LAST = {"title": None, "table": None, "kind": None}


def inject_sr_css():
    """Emit the .sr-only rule. Safe to call every rerun."""
    st.markdown(_SR_ONLY_CSS, unsafe_allow_html=True)


def sr_text(title: str, text: str, role: str = None):
    """Visually hidden heading + paragraph. No ARIA (bug 8)."""
    _t = html.escape(str(title)); _s = html.escape(" ".join(str(text).split()))
    # No heading here: the visible section heading already names it, and a
    # hidden duplicate was announced twice in the heading list (Mark's 10B).
    st.markdown(f'<div class="sr-only"><p><strong>{_t}:</strong> {_s}</p></div>', unsafe_allow_html=True)


def sr_block(inner_html: str):
    """Visually hidden block of real, navigable markup."""
    st.markdown(f'<div class="sr-only">{inner_html}</div>', unsafe_allow_html=True)


# ── Figure → table ───────────────────────────────────────────────────────────
def _kind(fig) -> str:
    t = [d.type for d in fig.data]
    if not t:
        return "Chart"
    if t[0] == "bar":
        return "Bar chart"
    if t[0] == "pie":
        return "Pie chart"
    if t[0] == "heatmap":
        return "Heatmap"
    if t[0] == "scatter":
        mode = fig.data[0].mode or "lines"
        return "Line chart" if "lines" in mode else "Scatter chart"
    return "Chart"


def _axis_title(ax) -> str:
    try:
        return ax.title.text or ""
    except Exception:
        return ""


def figure_table(fig) -> pd.DataFrame:
    """The plotted values as a DataFrame — exactly what the chart draws."""
    # Heatmap: rows x columns. Prefer customdata — a heatmap may be coloured by
    # a rescaled z while the true values live in customdata (Trends does this).
    for t in fig.data:
        if t.type == "heatmap" and t.z is not None:
            vals = t.customdata if t.customdata is not None else t.z
            try:
                df = pd.DataFrame([list(r) for r in vals],
                                  index=[str(y) for y in (t.y if t.y is not None else range(len(vals)))],
                                  columns=[str(x) for x in (t.x if t.x is not None else range(len(vals[0])))])
            except Exception:
                return pd.DataFrame()
            df.index.name = _axis_title(fig.layout.yaxis) or "Row"
            return df.reset_index()
    rows = {}
    order = []
    cat_label = "Category"
    for i, t in enumerate(fig.data):
        name = (t.name or "").strip() or ("Value" if len(fig.data) == 1 else f"Series {i + 1}")
        if t.type == "pie":
            labs, vals = list(t.labels or []), list(t.values or [])
            cat_label = "Segment"
        elif t.type in ("bar", "scatter"):
            horiz = getattr(t, "orientation", None) == "h"
            cats = list(t.y if horiz else t.x) if (t.y if horiz else t.x) is not None else []
            vals = list(t.x if horiz else t.y) if (t.x if horiz else t.y) is not None else []
            labs = cats
            ax = fig.layout.yaxis if horiz else fig.layout.xaxis
            cat_label = _axis_title(ax) or cat_label
        else:
            continue
        for c, v in zip(labs, vals):
            k = str(c)
            if k not in rows:
                rows[k] = {}
                order.append(k)
            rows[k][name] = v
    if not order:
        return pd.DataFrame()
    df = pd.DataFrame([rows[k] for k in order], index=order)
    df.index.name = cat_label
    # One category, several series (e.g. a single stacked bar with one trace
    # per label): flip so each label is a row, not one meaningless row.
    if len(df) == 1 and df.shape[1] > 1:
        df = df.T
        df.columns = ["Value"]
        df.index.name = "Label"
    return df.reset_index()


def _num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except Exception:
        return None


def _fmtv(v) -> str:
    f = _num(v)
    if f is None:
        return "no data" if v is None or (isinstance(v, float) and math.isnan(v)) else str(v)
    if abs(f) >= 1000:
        return f"{f:,.0f}"
    return f"{f:,.2f}".rstrip("0").rstrip(".")


_BAND = re.compile(r"^\s*-?\d+(?:\.\d+)?\s*[–-]\s*-?\d+(?:\.\d+)?\s*%?\s*$|^\s*\d+\s*\+\s*%?\s*$")


def _plural(noun: str, n: int) -> str:
    if n == 1 or noun.endswith("s"):
        return noun
    if noun.endswith("y") and not noun.endswith(("ay", "ey", "oy", "uy")):
        return noun[:-1] + "ies"
    return noun + "s"


def _plural_phrase(phrase: str, n: int) -> str:
    """'impact tier' -> 'impact tiers'; 'category' -> 'categories'; phrases
    already plural or ending in a participle ('days of warning', 'actions
    recorded') are left alone."""
    if n == 1:
        return phrase
    words = phrase.split()
    if " of " in phrase or any(w.endswith("s") for w in words[:1]) or words[-1].endswith("ed"):
        return phrase
    return " ".join(words[:-1] + [_plural(words[-1], 2)])


_UNIT_WORDS = {"%", "percent", "days", "day", "weeks", "months", "years", "bills", "points", "pts", "$"}


def _value_unit(name: str, units: str = "") -> str:
    """Suffix for numbers in one series: '%', ' days', ' bills' or ''.
    Only real unit words in trailing parentheses count, so a series called
    "Baseline (all history)" isn't given the unit "all history"."""
    m = re.search(r"\(([^)]+)\)\s*$", str(name))
    u = m.group(1).strip().lower() if m else ""
    if u not in _UNIT_WORDS:
        u = ""
    if u in ("%", "percent") or (not u and str(units).strip().startswith("%")):
        return "%"
    if u:
        return " " + u
    if str(name).strip().lower() in ("bills", "bill count", "count", "number of bills", "bills in window"):
        return " bills"
    return ""


def describe_table(df: pd.DataFrame, kind: str, units: str = "") -> str:
    """Plain-language summary computed from the data. No model involved.

    Bug 25 (QA, 2026-10-08): the old wording read "Chart of 20 likelihood %s.
    highest 1,981 at likelihood % 0; lowest 0 at likelihood % 45." It treated
    the column name as a noun, gave no units, and named one band when several
    tied. Now, e.g.: "Chart of 20 bands of floor-vote likelihood. Highest:
    1,981 bills at 0–5%. Lowest: 0 bills, in 10 of the 20 bands."
    """
    if df is None or df.empty:
        return f"{kind} with no data to show."
    cat = str(df.columns[0])
    series = [str(c) for c in df.columns[1:]]
    labels = [str(x) for x in df[cat]]
    pct_cat = "%" in cat
    base = re.sub(r"\s*\(?%\)?\s*", " ", cat).strip(" -()").strip() or "item"
    base_l = base if base[:2].isupper() else base[0].lower() + base[1:]
    banded = all(_BAND.match(l) or _num(l) is not None for l in labels)
    n = len(df)
    if banded:
        head = f"{n} band{'s' if n != 1 else ''} of {base_l}"
    else:
        head = f"{n} {_plural_phrase(base_l, n)}"

    def lab(l):                      # one label, % added for percent categories
        return l if l.strip().endswith("%") or not pct_cat else f"{l}%"

    def places(labs):
        if len(labs) > 3:
            return f", in {len(labs)} of the {n} {'bands' if banded else _plural_phrase(base_l, 2)}"
        ls = [lab(l) for l in labs]
        joined = ls[0] if len(ls) == 1 else ", ".join(ls[:-1]) + " and " + ls[-1]
        if banded and not pct_cat and not any(x.endswith("%") for x in ls):
            return f" at {joined} {base_l}"      # unit once, after the list
        return f" at {joined}" if banded or pct_cat else f" for {joined}"

    parts = [f"{kind} of {head}" + (f", {len(series)} series ({'; '.join(series)})." if len(series) > 1 else ".")]
    for s in series[:4]:
        vals = [(_num(v), l) for l, v in zip(labels, df[s]) if _num(v) is not None]
        if not vals:
            continue
        suf = _value_unit(s, units)
        def fv(x):
            t = _fmtv(x) + suf
            return t[:-1] if suf == " bills" and x == 1 else t
        hi = max(v for v, _ in vals); lo = min(v for v, _ in vals)
        his = [l for v, l in vals if v == hi]; los = [l for v, l in vals if v == lo]
        if len(vals) == 1:
            parts.append((f"{s}: " if len(series) > 1 else "") + f"{fv(hi)}{places(his)}.")
        elif hi == lo:
            parts.append((f"{s}: " if len(series) > 1 else "") + f"All {len(vals)} are {fv(hi)}.")
        elif len(series) > 1:
            parts.append(f"{s}: highest {fv(hi)}{places(his)}; lowest {fv(lo)}{places(los)}.")
        else:
            parts.append(f"Highest: {fv(hi)}{places(his)}. Lowest: {fv(lo)}{places(los)}.")
    return " ".join(parts)


# ── Tables ───────────────────────────────────────────────────────────────────
def band_table(counts, edges, label: str, unit: str = "%", value: str = "Bills") -> pd.DataFrame:
    """Histogram counts -> a details table with real band labels ("0–5%"),
    so descriptions say "1,981 bills at 0–5%" rather than "at likelihood % 0"
    (Bug 25). Use for the Chart details only; keep the chart's numeric axis."""
    lab = [f"{_fmtv(edges[i])}–{_fmtv(edges[i + 1])}{unit}" for i in range(len(counts))]
    return pd.DataFrame({label: lab, value: [int(c) for c in counts]}).set_index(label)


def _download_key(base: str) -> str:
    """Stable, unique widget key per download button within a run."""
    k = "dl_" + (re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_") or "table")
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        used = get_script_run_ctx().widget_user_keys_this_run
    except Exception:
        used = set()
    out, i = k, 1
    while out in used:
        i += 1; out = f"{k}_{i}"
    return out


_XL_MAX = 32767   # Excel's per-cell character limit


def excel_bytes(df, sheet: str = "Data") -> bytes:
    """A screen-reader-friendly .xlsx: data from A1, one bold header row,
    formatted as a real Excel Table (named, filterable, banded rows).

    Bug 17 (QA, 2026-10-16): CSVs are hard to use with a screen reader; QA
    asked for Excel with data in A1, proper headers, ideally an Excel Table.
    Index labels (e.g. likelihood bands) become the first column — the old CSV
    export dropped them. Text over Excel's 32,767-character cell limit is cut
    with a visible marker instead of breaking the file.
    """
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    d = df.copy()
    # Keep the index only when it carries meaning (it has a name, e.g. the
    # likelihood bands). A filtered table's leftover row numbers came out as a
    # junk "index" column (live finding, Oct 30).
    if d.index.name or (isinstance(d.index, pd.MultiIndex) and any(d.index.names)):
        d = d.reset_index()
    else:
        d = d.reset_index(drop=True)
    heads, seen = [], set()
    for c in d.columns:
        h = " ".join(str(c).split()) or "Column"
        b, i = h, 2
        while h.lower() in seen:
            h = f"{b} ({i})"; i += 1
        seen.add(h.lower()); heads.append(h)

    def cell(v):
        if v is None:
            return None
        if isinstance(v, (list, tuple, set)):
            v = ", ".join(str(x) for x in v)
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            pass
        if hasattr(v, "item") and not isinstance(v, str):
            try:
                v = v.item()
            except Exception:
                pass
        if isinstance(v, pd.Timestamp):
            v = v.tz_localize(None).to_pydatetime() if v.tzinfo else v.to_pydatetime()
        if isinstance(v, str) and len(v) > 1 and v[0] == "[" and v[-1] == "]":
            try:
                import json as _json
                _l = _json.loads(v)
                if isinstance(_l, list):
                    v = ", ".join(str(x) for x in _l)   # was shown raw: ["individuals", "families"]
            except Exception:
                pass
        if isinstance(v, str):
            v = ILLEGAL_CHARACTERS_RE.sub("", v)
            if len(v) > _XL_MAX:
                v = v[:_XL_MAX - 14] + " …[truncated]"
        return v

    wb = Workbook(); ws = wb.active
    ws.title = (re.sub(r"[\[\]\*\?/\\:]", " ", str(sheet)).strip() or "Data")[:31]
    ws.append(heads)
    for row in d.itertuples(index=False):
        ws.append([cell(v) for v in row])
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.freeze_panes = "A2"
    if len(d):
        tname = "T_" + (re.sub(r"[^A-Za-z0-9_]", "_", ws.title) or "Data")[:200]
        t = Table(displayName=tname, ref=f"A1:{get_column_letter(len(heads))}{len(d) + 1}")
        # "Medium 2" put white header text on mid-blue, which Excel's own
        # Accessibility Checker flags ("Hard-to-read text contrast"). Light 1:
        # black text, white/light-grey bands.
        t.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=True)
        ws.add_table(t)
    for j, h in enumerate(heads, 1):
        longest = max([len(h)] + [len(str(x)) for x in d.iloc[:200, j - 1].tolist() if x is not None])
        ws.column_dimensions[get_column_letter(j)].width = min(max(10, longest + 2), 60)
    bio = io.BytesIO(); wb.save(bio)
    return bio.getvalue()


@st.cache_data(show_spinner=False, max_entries=64, ttl=3600)
def _excel_cached(fingerprint: str, sheet: str, _df) -> bytes:
    """excel_bytes(), cached by a fingerprint of the data.

    Live finding (Oct 27): Streamlit builds a download's file on EVERY rerun,
    not only when clicked. For Explorer's 12,014 bills that took ~1.5 s per
    rerun (CSV took 0.14 s), slowing every interaction. The fingerprint costs
    ~0.04 s, so the workbook is built once per distinct result set. No
    spinner: a spinner on some runs only would shift the page (Bug 27).
    """
    return excel_bytes(_df, sheet)


def _fingerprint(df) -> str:
    import hashlib
    try:
        body = pd.util.hash_pandas_object(df.astype(str), index=True).values.tobytes()
    except Exception:
        body = df.to_csv().encode("utf-8", "ignore")
    head = "|".join(map(str, df.columns)) + "|" + str(df.index.name)
    return hashlib.md5(head.encode("utf-8", "ignore") + body).hexdigest()


def download_data_button(df, name: str):
    """Bug 17 (QA, 2026-10-08): QA asked to keep the data download the old grid
    toolbar offered, with a proper label. A native button whose visible text
    names the data ("Download Largest Movers data (Excel)"), so several on one
    page are distinguishable and voice-control users can say what they see."""
    if df is None or len(df) == 0:
        return
    nm = " ".join(str(name or "table").split())
    try:
        st.download_button(f"Download {nm} data (Excel)", data=_excel_cached(_fingerprint(df), nm, df),
                           file_name=(re.sub(r"[^A-Za-z0-9]+", "_", nm).strip("_") or "data") + ".xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           key=_download_key(nm))
    except Exception:
        pass


def a11y_table(df, caption: str = None, max_rows: int = 60, fmt: dict = None, label: str = None,
               download: str = None):
    """Render a DataFrame as a real HTML <table> with explicit headers.

    Replaces st.dataframe (a canvas grid with no visible keyboard focus,
    bug 15). Walkthrough finding: st.table was tried first, but it drops the
    index name (the first column lost its header, e.g. "Bill") and greys the
    index cells. So every table renders through the same helper as Explorer,
    which writes every header and row header explicitly.
    """
    if df is None or len(df) == 0:
        st.caption("No rows to show.")
        return
    d = df.head(max_rows) if max_rows else df
    if fmt:
        d = d.copy()
        for c, f in fmt.items():
            if c in d.columns:
                d[c] = d[c].map(lambda v, f=f: (f.format(v) if isinstance(f, str) else f(v)) if pd.notna(v) else "")
    # A named table's scroll region takes its name too ("Top Bills to Watch"),
    # so two tables with the same columns never share a landmark name.
    _render_table(d, caption, len(df), label=label or download)
    _name = download or (label[9:] if label and label.startswith("Data for ") else (caption or label))
    if _name:
        download_data_button(df, _name)


def a11y_html_table(df, caption: str, key: str, link_cols=(), pct_cols=(),
                    page_size: int = 25, link_label_col: str = None):
    """Accessible replacement for st.dataframe on large, linked tables (bug 15).

    Streamlit's dataframe is a canvas grid with no visible keyboard focus.
    This renders a real <table>: <caption>, column headers with scope="col",
    the first column as row headers (scope="row"), link cells with
    descriptive text ("Open HB0031 (opens in new tab)" rather than "Open"),
    percentages as text, and simple pagination. Wide tables scroll inside a
    focusable, named region so keyboard users can scroll them too.
    """
    if df is None or len(df) == 0:
        st.caption("No rows to show.")
        return
    n = len(df)
    pages = max(1, math.ceil(n / page_size))
    page = 1
    if pages > 1:
        page = int(st.number_input(f"Page (of {pages:,})", min_value=1, max_value=pages,
                                   value=1, step=1, key=f"{key}_page"))
    lo, hi = (page - 1) * page_size, min(page * page_size, n)
    _render_table(df.iloc[lo:hi], caption, n, lo=lo, link_cols=link_cols,
                  pct_cols=pct_cols, link_label_col=link_label_col)


def _render_table(d, caption, n_total, lo=0, link_cols=(), pct_cols=(), link_label_col=None, label=None):
    """Write a <table>: caption, column headers (scope=col), first column as
    row headers (scope=row), full-contrast text, focusable scroll region."""
    try:
        from a11y import NEW_TAB
    except Exception:  # pragma: no cover
        NEW_TAB = ' <span class="a11y-sr">(opens in new tab)</span>'
    n = n_total
    hi = lo + len(d)
    cols = list(d.columns)
    lab_col = link_label_col or cols[0]
    esc = html.escape

    def cell(c, v, row):
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return ""
        # Walkthrough findings: "<NA>" was read aloud letter by letter; dates
        # carried a meaningless " 00:00:00"; IDs were formatted like amounts
        # ("2,020" for session 2020).
        if str(v) in ("<NA>", "NaT", "nan", "None"):
            return ""
        if hasattr(v, "strftime"):
            try:
                return esc(v.strftime("%Y-%m-%d") if (getattr(v, "hour", 0), getattr(v, "minute", 0)) == (0, 0)
                           else v.strftime("%Y-%m-%d %H:%M"))
            except Exception:
                pass
        if isinstance(v, str) and v.endswith(" 00:00:00"):
            return esc(v[:-9])
        cl = str(c).lower()
        if (cl.endswith("id") or " id" in cl or "session" in cl or "year" in cl) and _num(v) is not None:
            f = _num(v)
            return esc(str(int(f)) if f == int(f) else str(v))
        # Lists/arrays read bracket-by-bracket ("left bracket quote healthcare");
        # join them into plain text.
        if not isinstance(v, (str, bytes)) and hasattr(v, "__iter__"):
            return esc(", ".join(str(x) for x in v))
        if isinstance(v, str) and v.startswith("[") and v.endswith("]"):
            return esc(", ".join(p.strip().strip("'\"") for p in v[1:-1].split(",") if p.strip()))
        if c in pct_cols:
            f = _num(v)
            return "" if f is None else f"{f:.0f}%"
        if c in link_cols:
            u = str(v)
            if not u.startswith(("http://", "https://")):
                return esc(u)
            lbl = esc(str(row[lab_col])) if lab_col in row else "bill"
            return f'<a href="{esc(u, quote=True)}" target="_blank" rel="noopener">Open {lbl}{NEW_TAB}</a>'
        f = _num(v)
        if f is not None and not isinstance(v, bool) and c not in (cols[0],):
            return esc(_fmtv(v))
        return esc(str(v))

    def _hdr(c):
        s = str(c)
        # Fallback for code-style names: "median_warning_days" -> "Median warning days".
        return (s.replace("_", " ").strip().capitalize()) if "_" in s else s
    head = "".join(f'<th scope="col">{esc(_hdr(c))}</th>' for c in cols)
    body = []
    for _, r in d.iterrows():
        tds = []
        for i, c in enumerate(cols):
            tag = ('th scope="row"', "th") if i == 0 else ("td", "td")
            tds.append(f"<{tag[0]}>{cell(c, r[c], r)}</{tag[1]}>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    if caption and n > len(d):
        cap_html = f"<caption>{esc(caption)} — rows {lo + 1:,} to {hi:,} of {n:,}</caption>"
    elif caption:
        cap_html = f"<caption>{esc(caption)}</caption>"
    else:
        cap_html = ""
    # axe finding: every uncaptioned table's scroll region was named "Data
    # table", so a page had several identically named landmarks. Name each
    # by its purpose, or failing that by its columns.
    region = esc(label or caption or ("Table: " + ", ".join(str(c) for c in cols[:3])))
    st.markdown(
        '<style>.a11y-tbl{border-collapse:collapse;width:100%;font-size:13px;line-height:1.4}'
        '.a11y-tbl caption{text-align:left;font-weight:600;color:#1A2733;padding:4px 0 6px}'
        '.a11y-tbl th,.a11y-tbl td{border:1px solid #D5DCE3;padding:6px 8px;vertical-align:top;text-align:left;color:#1A2733;max-width:340px}'
        '.a11y-tbl thead th{background:#F1F5F9;font-weight:700}'
        '.a11y-tbl tbody th{font-weight:600;white-space:nowrap}'
        '.a11y-tbl a{color:#0B5FA5;font-weight:600}'
        '.a11y-tblwrap{overflow-x:auto;max-width:100%}</style>'
        f'<div class="a11y-tblwrap" role="region" tabindex="0" aria-label="{region}">'
        f'<table class="a11y-tbl" style="min-width:{max(100, len(cols) * 120) if len(cols) > 6 else 0}px">'
        f'{cap_html}<thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>',
        unsafe_allow_html=True)


# ── Charts ───────────────────────────────────────────────────────────────────
def plotly_chart(fig, title: str = None, **kwargs):
    """st.plotly_chart with non-colour cues, overlap fixes and a name.

    Call exactly like st.plotly_chart. Follow with chart_aux(...) (or
    chart_details(...)) to render the data table.
    """
    enforce_non_color_cues(fig)
    fix_overlaps(fig)
    kind = _kind(fig)
    t = title or (fig.layout.title.text if fig.layout.title and fig.layout.title.text else None)
    name = (f"{kind}: {t}" + ("" if str(t).rstrip().endswith((".", "?", "!")) else ".")) if t else f"{kind}."
    fig.update_layout(meta={"a11y_name": name + " Chart details with a data table follow."})
    # Units for the description come from the value axis title (e.g. "% of bills").
    horiz = any(getattr(d, "orientation", None) == "h" for d in fig.data)
    units = _axis_title(fig.layout.xaxis if horiz else fig.layout.yaxis) if kind != "Heatmap" else ""
    _LAST.update(title=t, table=figure_table(fig), kind=kind, units=units)
    # The modebar is a row of icon links; zoom/pan add nothing the data table
    # does not. Off unless a page explicitly asks for it.
    kwargs.setdefault("config", {"displayModeBar": False})
    return st.plotly_chart(fig, **kwargs)


def chart_details(title: str, summary: str = "", table=None, kind: str = "Chart", units: str = ""):
    """Collapsed native expander: description + real table."""
    with st.expander(f"Chart details: {title}", expanded=False):
        if summary:
            st.markdown(html.escape(" ".join(str(summary).split())))
        if table is not None and len(table):
            desc = describe_table(table, kind, units)
            if units:
                # Walkthrough finding: "highest 57.94" didn't say 57.94 of what.
                desc += f" Values are {units}."
            st.markdown(html.escape(desc))
            a11y_table(table, label=f"Data for {title}")


def chart_aux(title: str, sr: str, ai_data: str = None, key: str = None):
    """Details for the chart just drawn with plotly_chart().

    `sr` is the page's own sentence about the chart (computed from data).
    `ai_data` is accepted for backward compatibility and ignored: the AI
    readout was removed for accuracy (bug 25).
    """
    table = _LAST.get("table")
    kind = _LAST.get("kind") or "Chart"
    units = _LAST.get("units") or ""
    _LAST.update(title=None, table=None, kind=None, units=None)
    if table is None or len(table) == 0:
        # Follows a table, not a chart: the table is already its own text
        # alternative. An extra "Chart details" panel would only add noise.
        return
    chart_details(title, sr, table, kind, units)


def sr_chart_table(title: str, data, caption: str = "", max_rows: int = 40):
    """Details for a native st.bar_chart / st.line_chart (bug 15).

    Previously a visually hidden table. Now a visible, collapsed expander so
    sighted keyboard and low-vision users can read the values too.
    """
    if data is None:
        return
    df = data.to_frame() if isinstance(data, pd.Series) else data.copy()
    if df is None or len(df) == 0:
        return
    idx = df.index.name or "Category"
    df = df.reset_index().rename(columns={"index": idx})
    kind = "Chart"
    with st.expander(f"Chart details: {title}", expanded=False):
        if caption:
            st.markdown(html.escape(caption))
        st.markdown(html.escape(describe_table(df, kind)))
        a11y_table(df, max_rows=max_rows, label=f"Data for {title}")
