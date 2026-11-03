"""
app_theme.py — Single source of truth for visual styling across all pages.
==========================================================================
Colorblind-safe palette (blue / orange, not blue / red), WCAG-AA contrast,
the network-graph font stack, and a shared Plotly template so every chart
renders to one consistent, accessible standard.

Accessibility principles enforced here:
  - Color is NEVER the only signal. Direction is also encoded by sign,
    axis position, and arrows (↑/↓) added at the call site.
  - Blue/orange chosen over blue/red: distinguishable under all common
    color-vision deficiencies (deuteranopia, protanopia, tritanopia).
  - Text/label contrast meets WCAG AA (≥ 4.5:1) on white.
  - Direct labeling preferred over legends where practical.
"""

import plotly.graph_objects as go
import plotly.io as pio

# ── Font stack (matches the network graph) ───────────────────────────────────
# Bug 26 follow-up: charts render in the page, where Streamlit has already
# loaded Source Sans Pro, so they match the rest of the app.
FONT_STACK = '"Source Sans Pro", sans-serif'

# ── Brand neutrals (from the existing app identity) ───────────────────────────
C_DARK   = "#0F1F38"   # trapped darkness — headers, dark chrome
C_NAVY   = "#1B4B5A"   # lazurite — primary brand
C_SAND   = "#6E5A50"   # cedar plank — neutral/stable. Audit bug 3: was
                       # #8E7970 at 4.10:1 (the 4.15:1 the auditor measured);
                       # now 6.48:1 on white.
C_INK    = "#1A2733"   # near-black text on light
C_MUTED  = "#454F5A"   # secondary text — 8.34:1 on white (audit issue 3;
                       # was #5B6B71 at 5.55:1, below the 7:1 the audit asked for)
C_GRID   = "#E3E9EB"   # hairline gridlines
C_AXIS   = "#6B7A80"   # axis lines/ticks — 4.45:1 (audit issue 3; was
                       # #9AA7AC at 2.47:1, which failed even the 3:1
                       # non-text minimum in WCAG 1.4.11)

# ── Colorblind-safe semantic palette (Okabe–Ito derived, contrast-tuned) ──────
# Blue = above baseline / increase / positive
# Orange = below baseline / decrease / negative
# Both clear WCAG 3:1 vs white (graphics minimum); maximally separable across CVD.
C_ABOVE  = "#0072B2"   # accessible blue  — 5.19:1 on white
C_BELOW  = "#A2570F"   # accessible burnt-orange — 5.49:1 (audit issue 3;
                       # was #B5651D at 4.34:1, fine for graphics but it is
                       # also used for text at several call sites)
C_STABLE = "#6E5A50"   # sand — stable / neutral (see C_SAND note)
C_ACCENT = "#0072B2"   # default accent = the blue

# Light fills (for shaded bands, hover, subtle backgrounds)
C_ABOVE_FILL = "rgba(0,114,178,0.14)"
C_BELOW_FILL = "rgba(181,101,29,0.16)"
C_BAND_FILL  = "rgba(27,75,90,0.08)"

# Sequential ramp for heatmaps (colorblind-safe, perceptually uniform-ish)
# Light -> deep blue. Avoids red/green entirely.
HEATMAP_SCALE = [
    [0.00, "#F7FBFF"],
    [0.25, "#C9E0F0"],
    [0.50, "#84BCDB"],
    [0.75, "#3787BF"],
    [1.00, "#0B4F8A"],
]

# Direction glyphs — redundant (non-color) cue. Use at call sites.
ARROW_UP   = "▲"
ARROW_DOWN = "▼"
ARROW_FLAT = "■"


def direction_label(value, kind="arrow"):
    """Return a non-color directional cue for a signed value.
    kind='arrow' -> ▲/▼/■ ; kind='word' -> Above/Below/Stable."""
    if value > 0:
        return ARROW_UP if kind == "arrow" else "Above"
    if value < 0:
        return ARROW_DOWN if kind == "arrow" else "Below"
    return ARROW_FLAT if kind == "arrow" else "Stable"


# ── Shared Plotly template ────────────────────────────────────────────────────
def _build_template():
    t = go.layout.Template()
    t.layout = go.Layout(
        font=dict(family=FONT_STACK, size=13, color=C_INK),
        paper_bgcolor="white",
        plot_bgcolor="white",
        colorway=[C_ABOVE, C_BELOW, C_NAVY, C_SAND, C_DARK, "#56B4E9", "#CC79A7"],
        title=dict(font=dict(family=FONT_STACK, size=16, color=C_DARK), x=0.0, xanchor="left"),
        # Audit issue 5: l=10/b=10 was far too tight for rotated category
        # labels and axis titles. With automargin on (below), Plotly grows
        # these as needed; these values are now only the floor.
        margin=dict(l=48, r=18, t=46, b=48),
        xaxis=dict(showgrid=True, gridcolor=C_GRID, gridwidth=1, zeroline=False,
                   linecolor=C_AXIS, ticks="outside", tickcolor=C_AXIS,
                   # Audit issue 5: without automargin Plotly clips or overlaps
                   # rotated ticks and axis titles instead of making room.
                   automargin=True,
                   tickfont=dict(family=FONT_STACK, size=12, color=C_MUTED),
                   title=dict(font=dict(size=12, color=C_MUTED))),
        yaxis=dict(showgrid=True, gridcolor=C_GRID, gridwidth=1, zeroline=False,
                   linecolor=C_AXIS, ticks="outside", tickcolor=C_AXIS,
                   # Audit issue 5: without automargin Plotly clips or overlaps
                   # rotated ticks and axis titles instead of making room.
                   automargin=True,
                   tickfont=dict(family=FONT_STACK, size=12, color=C_MUTED),
                   title=dict(font=dict(size=12, color=C_MUTED))),
        legend=dict(font=dict(family=FONT_STACK, size=12, color=C_INK),
                    bgcolor="rgba(0,0,0,0)", borderwidth=0),
        hoverlabel=dict(font=dict(family=FONT_STACK, size=12), bgcolor=C_DARK,
                        font_color="white", bordercolor=C_DARK),
        colorscale=dict(sequential=HEATMAP_SCALE),
    )
    # Audit bug 4 note: dash/marker/hatch cycles USED to live here, in the
    # template. Plotly Express overrides template values, so they did nothing
    # for px charts, while hand-built charts (e.g. Trends' confidence bands)
    # picked them up where the author never asked for them. Cues are now set
    # per trace, only where series are indistinguishable, by
    # enforce_non_color_cues() — called from style_fig() and plotly_chart().
    return t


pio.templates["leganalytics"] = _build_template()
PLOTLY_TEMPLATE = "leganalytics"


def style_fig(fig, height=None, show_legend=None):
    """Apply final consistent touches to any figure built with the template."""
    fig.update_layout(template=PLOTLY_TEMPLATE)
    if height is not None:
        fig.update_layout(height=height)
    if show_legend is not None:
        fig.update_layout(showlegend=show_legend)
    fig.update_layout(font=dict(family=FONT_STACK))
    enforce_non_color_cues(fig)
    fix_overlaps(fig)
    return fig


# ── Bug 4: series must differ by more than colour ────────────────────────────
# Round 2 put symbol/dash/hatch cycles in the TEMPLATE. That did nothing:
# Plotly Express writes dash="solid", symbol="circle" and pattern="" onto
# every trace explicitly, and explicit trace values always beat template
# defaults. Trends also never called style_fig. So cues are now written onto
# each trace directly, after the figure is built.
_DASHES = ["solid", "dash", "dot", "dashdot", "longdash", "longdashdot"]
_SYMBOLS = ["circle", "square", "diamond", "triangle-up", "x", "star", "cross"]
_PATTERNS = ["", "/", ".", "x", "-", "|", "+"]


def enforce_non_color_cues(fig):
    """Give every series a non-colour distinguishing cue. Idempotent.

    Only series that are actually indistinguishable get new cues, and only
    within their own group. Walkthrough finding: cycling cues onto every trace
    by position overrode deliberate design (Trends' Category Share chart lost
    its thick solid "observed" line, and its shaded bands got markers), which
    hurt readability. Author choices that already separate series are kept.
    Shaded bands (filled areas and their invisible edges) are never touched.
    """
    def is_band(t):
        return ((t.fill not in (None, "none")) or (t.hoverinfo == "skip")
                or (t.line is not None and t.line.width == 0))

    data = list(fig.data)
    # ── Lines / markers: group by current visual signature ──────────────────
    groups = {}
    for t in data:
        if t.type != "scatter" or is_band(t):
            continue
        mode = t.mode or "lines"
        sig = ("lines" in mode, "markers" in mode, t.line.dash or "solid",
               t.marker.symbol or "circle")
        groups.setdefault(sig, []).append(t)
    for sig, members in groups.items():
        if len(members) < 2:
            continue                          # already distinct: leave it alone
        has_lines = sig[0]
        for i, t in enumerate(members):
            t.marker.symbol = _SYMBOLS[i % len(_SYMBOLS)]
            if has_lines:
                t.line.dash = _DASHES[i % len(_DASHES)]
                if "markers" not in (t.mode or "lines"):
                    n = len(t.x) if t.x is not None else 0
                    t.mode = (t.mode or "lines") + "+markers"
                    t.marker.maxdisplayed = 24 if n > 24 else 0
                if not t.marker.size:
                    t.marker.size = 7
    # ── Bars: several traces sharing one look get distinct hatches ──────────
    bars = [t for t in data if t.type == "bar"]
    if len(bars) >= 2:
        shapes = [(t.marker.pattern.shape if t.marker.pattern else "") or "" for t in bars]
        if len(set(map(str, shapes))) < len(bars):
            for i, t in enumerate(bars):
                # fillmode="overlay" draws the hatch ON TOP of the bar colour.
                # The default ("replace") swaps the colour for the hatch, and
                # with a white hatch the bars disappear entirely.
                t.marker.pattern = dict(shape=_PATTERNS[i % len(_PATTERNS)], solidity=0.3,
                                        fgcolor="rgba(255,255,255,0.75)", fillmode="overlay")
    elif len(bars) == 1:
        # One trace whose bars are coloured by meaning: one hatch per colour.
        t = bars[0]
        cols = t.marker.color
        if isinstance(cols, (list, tuple)) and len(set(map(str, cols))) >= 2:
            order = list(dict.fromkeys(map(str, cols)))
            t.marker.pattern = dict(shape=[_PATTERNS[order.index(str(c)) % len(_PATTERNS)] for c in cols],
                                    solidity=0.3, fgcolor="rgba(255,255,255,0.75)", fillmode="overlay")
    for t in data:
        if t.type == "pie":
            n = len(t.labels) if t.labels is not None else 0
            t.marker.pattern = dict(shape=[_PATTERNS[i % len(_PATTERNS)] for i in range(n)], solidity=0.3,
                                    fgcolor="rgba(255,255,255,0.75)", fillmode="overlay")
            if not t.textinfo:
                t.textinfo = "label+percent"
    return fig


# ── Bug 5: overlapping legends and labels ────────────────────────────────────
# Three Trends charts placed a horizontal legend BELOW the plot (y=-0.22) while
# fixing the bottom margin at 8px, so the legend sat on the x-axis labels.
# automargin sizes axes, not a legend pushed into the margin.
def fix_overlaps(fig):
    """Move below-plot horizontal legends above the plot; automargin all axes."""
    lg = fig.layout.legend
    if lg.orientation == "h" and lg.y is not None and lg.y < 0:
        fig.update_layout(legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                      xanchor="left", x=0))
        m = fig.layout.margin
        if m.t is None or m.t < 44:
            fig.update_layout(margin=dict(t=44))
    fig.update_xaxes(automargin=True)
    fig.update_yaxes(automargin=True)
    return fig


# ── Shared CSS injected per page (cards, headers, spacing) ────────────────────
def page_css():
    """Return the shared <style> block for consistent chrome across pages."""
    return f"""
<style>
  /* Base typography to match the network graph */
  html, body, [class*="css"] {{ font-family: {FONT_STACK}; }}
  .block-container {{ padding-top:1.4rem; }}

  /* Section headers.
     Audit issue 10: these are now emitted on real <h2>/<h3> elements rather
     than <div>s, so the UA's own heading margins/sizes must be reset or the
     page reflows. Selector covers both while callers are migrated. */
  .sleek-h, h1.sleek-h, h2.sleek-h, h3.sleek-h, h4.sleek-h {{
              font-family:{FONT_STACK}; font-weight:800; font-size:1.15rem;
              color:{C_DARK}; letter-spacing:-.01em; margin:.2rem 0 .1rem;
              padding:0; line-height:1.3; }}
  .sleek-sub {{ font-family:{FONT_STACK}; font-size:.85rem; color:{C_MUTED};
                margin-bottom:.6rem; }}

  /* Cards */
  .sleek-card {{ background:#fff; border:1px solid {C_GRID}; border-radius:14px;
                 padding:1rem 1.2rem; box-shadow:0 1px 8px rgba(15,31,56,.06); }}

  /* KPI tiles */
  .kpi-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:.4rem 0 1rem; }}
  .kpi-card {{ background:#fff; border:1px solid {C_GRID}; border-radius:14px;
               padding:.7rem .9rem; box-shadow:0 1px 8px rgba(15,31,56,.06); }}
  .kpi-label, h3.kpi-label {{ font-size:.7rem; font-weight:700; letter-spacing:.04em;
                text-transform:uppercase; color:{C_MUTED}; margin:0 0 .3rem;
                padding:0; line-height:1.3; }}
  .kpi-value {{ font-size:1.5rem; font-weight:800; color:{C_DARK}; }}

  /* Legend chips that carry a non-color cue */
  .cue {{ display:inline-flex; align-items:center; gap:.3rem; font-size:.8rem;
          color:{C_MUTED}; margin-right:1rem; }}
  .cue b {{ font-weight:700; }}
</style>
"""
