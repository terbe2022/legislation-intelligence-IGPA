"""
a11y.py — Shared accessibility layer for every page.
=====================================================
ReQtest audit, round 3 (2026-09-28).

Streamlit 1.37.1 generates most of the markup the audit flags, so fixes come
in three layers:

  1. CSS injected into the page (contrast, focus, collapsed-sidebar hiding).
  2. One runtime script that corrects Streamlit's generated DOM after every
     render: landmarks, sidebar buttons, widget labels, radio grouping, slider
     thumb names, character-count announcements, chart names, toolbar names.
  3. Python helpers pages call directly (focus management, KPI "sentences").

Every runtime fix below was verified against Streamlit 1.37.1's real rendered
DOM and Chromium's accessibility tree (what screen readers consume), not
inferred from source. Findings that drove each fix are noted inline.

Engineering rules this file follows, both learned the hard way in round 2:
  - Each fix runs in its own try/catch. One throwing must never stop the rest.
  - The JS template is filled by token replacement, never %-formatting, so a
    stray "%" in a comment cannot crash the page.

Usage — once per page, right after require_auth():
    from a11y import apply_page_a11y
    apply_page_a11y("Trends")
"""
import html as _html
import json as _json

import streamlit as st
import streamlit.components.v1 as components

# ── Contrast tokens (bug 3) ──────────────────────────────────────────────────
C_TEXT_MUTED = "#454F5A"    # 8.34:1 on white
C_TEXT_SUBTLE = "#5A6673"   # 5.86:1 on white — placeholders only
C_AXIS_A11Y = "#6B7A80"     # 4.45:1 — axis lines (non-text, 3:1 minimum)

_A11Y_CSS = """
<style>
/* Visually hidden, still read by screen readers. */
.sr-only, .a11y-sr {
  position:absolute !important; width:1px; height:1px; padding:0; margin:-1px;
  overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0;
}

/* ── Bug 3: Streamlit's own secondary text ─────────────────────────────────
   st.caption renders at rgba(<textColor>, 0.6), about 4.2:1 on white. The
   round-2 scan only checked hex values in our code, so it never saw this.
   Widget help text and counters use the same translucent colour. */
[data-testid="stCaptionContainer"],
[data-testid="stCaptionContainer"] p,
[data-testid="stCaptionContainer"] span,
[data-testid="InputInstructions"],
[data-testid="stWidgetLabel"] small,
.stMarkdown small { color:#454F5A !important; opacity:1 !important; }
[data-testid="stWidgetLabel"], [data-testid="stWidgetLabel"] p { color:#1A2733 !important; }
input::placeholder, textarea::placeholder { color:#5A6673 !important; opacity:1 !important; }
/* axe findings (2026-10-02):
   - Streamlit's top-right toolbar (Deploy / "⋮" menu) puts aria-expanded on a
     plain <span> (invalid ARIA, "aria-allowed-attr"). Every page but Explorer
     already hid it; hide it everywhere.
   - In a multiselect with chips, the typing box shrinks to 2px wide, failing
     target size (2.5.8). The visible control is larger; give the input a floor. */
[data-testid="stToolbar"] { display:none !important; }
/* My Tracker live finding: Streamlit's native (Vega) charts carry an export
   menu ("Save as SVG", "View Source"…). Its "…" button was reachable with Tab
   but inside the chart's aria-hidden drawing, so screen readers announced
   nothing on focus (axe: aria-hidden-focus), and its new-tab links had no
   warning. Removed, like Plotly's modebar; the data is in Chart details. */
[data-testid="stArrowVegaLiteChart"] details, [data-testid="stArrowVegaLiteChart"] .vega-actions { display:none !important; }
[data-baseweb="select"] input { min-width:24px !important; }
/* Regression finding: Streamlit draws `inline code` in green at 2.99:1. */
[data-testid="stMarkdownContainer"] code { color:#1A2733 !important; background:#EEF1F4 !important; }
/* Walkthrough finding: in text areas the counter ("933/1500") sat under the
   corner resize grip, which covered its last digit. Move it clear. */
[data-testid="stTextArea"] [data-testid="InputInstructions"] { right:1.5rem !important; }

/* ── Bugs 19 + 21: sidebar ─────────────────────────────────────────────────
   Collapsed, Streamlit only slides the sidebar off-screen: it stays visible
   to screen readers and every link inside stays in the tab order. Hide it
   properly, keyed off the aria-expanded Streamlit already maintains. */
section[data-testid="stSidebar"][aria-expanded="false"] { visibility:hidden !important; }

/* The expand control lives in the header, which every page collapses to zero
   height. Keep it visible top-left. Round 2 painted every SVG path, including
   the icon's invisible fill="none" bounding box — that was the black square.
   Colour only; the icon already uses fill="currentColor". */
[data-testid="collapsedControl"] {
  position:fixed !important; top:.6rem !important; left:.6rem !important;
  z-index:999991 !important; display:flex !important; visibility:visible !important;
}
/* Walkthrough finding: at ~26px it was hard to spot (Mark's original
   complaint) and it covered the top of the page content. Now a 44px-tall
   button with a visible "Menu" label, and content moves down to clear it. */
[data-testid="collapsedControl"] button {
  display:inline-flex !important; align-items:center !important; gap:.35rem !important;
  min-height:44px !important; min-width:44px !important;
  background:#FFFFFF !important; color:#13294B !important;
  border:2px solid #13294B !important; border-radius:8px !important;
  padding:.35rem .8rem .35rem .55rem !important; opacity:1 !important;
  box-shadow:0 2px 8px rgba(19,41,75,.18) !important;
}
[data-testid="collapsedControl"] button svg { color:#13294B !important; width:1.5rem !important; height:1.5rem !important; }
[data-testid="collapsedControl"] button::after {
  content:"Menu"; font-size:15px; font-weight:700; color:#13294B; line-height:1;
}
/* The close button inside the sidebar gets the same visible label treatment. */
[data-testid="stSidebarCollapseButton"] button {
  display:inline-flex !important; align-items:center !important; gap:.3rem !important;
  min-height:40px !important; padding:.3rem .7rem .3rem .45rem !important;
  border:2px solid #13294B !important; border-radius:8px !important;
  background:#FFFFFF !important; color:#13294B !important; opacity:1 !important;
}
/* Streamlit sets this to display:none until the mouse hovers the sidebar,
   so keyboard and screen-reader users could never close the menu. Always show it. */
[data-testid="stSidebarCollapseButton"] { display:flex !important; opacity:1 !important; visibility:visible !important; }
[data-testid="stSidebarCollapseButton"] button::after {
  content:"Close menu"; font-size:14px; font-weight:700; color:#13294B; line-height:1;
}
section[data-testid="stSidebar"][aria-expanded="false"] ~ section .block-container,
section[data-testid="stSidebar"][aria-expanded="false"] ~ section [data-testid="stAppViewBlockContainer"],
section[data-testid="stSidebar"][aria-expanded="false"] ~ div .block-container {
  padding-top:4.25rem !important;
}

/* ── Bug 17: element toolbars (table download/search/fullscreen) ───────────
   Round 2 forced these permanently visible, which left them sitting under
   the charts. Native behaviour is shown-on-hover; add shown-on-keyboard-focus
   and keep them above the element they belong to. */
[data-testid="stElementToolbar"] { z-index:5 !important; }
[data-testid="stElementToolbar"]:focus-within,
[data-testid="stFullScreenFrame"]:focus-within [data-testid="stElementToolbar"],
[data-testid="StyledFullScreenButton"]:focus,
[data-testid="StyledFullScreenButton"]:focus-visible { opacity:1 !important; visibility:visible !important; }

/* Streamlit adds a hover-only "link to this section" icon beside every markdown
   heading: an unnamed image inside an unnamed link, and an extra tab stop per
   heading on every page. Removed for everyone. */
[data-testid="stHeaderActionElements"] { display:none !important; }

/* Visible keyboard focus on everything interactive. */
:is(button, a, input, select, textarea, summary, [role="button"], [role="tab"],
    [role="radio"], [role="checkbox"], [role="option"], [role="slider"],
    [tabindex]:not([tabindex="-1"])):focus-visible {
  outline:3px solid #0B5FA5 !important; outline-offset:2px !important;
}
/* Programmatic focus targets (bug 9) get a ring too, without a tab stop. */
[data-a11y-focus-target]:focus { outline:3px solid #0B5FA5 !important; outline-offset:3px !important; }

/* ── Bug 16 (closed — preserved): disabled controls ────────────────────────
   Scoped to controls. Round 2 also matched the <label>, which carries a bare
   `disabled` attribute, and painted a grey band behind it. */
button:disabled {
  opacity:1 !important; color:#55606B !important; background-color:#EDF0F2 !important;
  border:1px dashed #8A96A0 !important; cursor:not-allowed !important;
}
[data-testid="stMultiSelect"] input[disabled],
[data-testid="stSelectbox"] input[disabled],
[data-testid="stTextInput"] input:disabled,
[data-testid="stTextArea"] textarea:disabled,
[data-testid="stDateInput"] input:disabled,
[data-testid="stNumberInput"] input:disabled {
  opacity:1 !important; color:#55606B !important; cursor:not-allowed !important;
}
[data-testid="stWidgetLabel"][disabled], [data-testid="stWidgetLabel"][disabled] p {
  background:transparent !important; color:#55606B !important;
}

/* ── Bug 13: value/label tiles read as one sentence ────────────────────────
   Visual parts are aria-hidden; one sr-only span carries the whole phrase.
   No lists, no description lists — round 2's <dl> added list noise. */
.kpi-read { display:block; }

/* ── Bug 10: heading scale (outline changes, visuals do not) ──────────────── */
[data-testid="stMarkdownContainer"] h1 { font-size:1.75rem; font-weight:800; line-height:1.25; margin:.4rem 0 .5rem; }
[data-testid="stMarkdownContainer"] h2 { font-size:1.35rem; font-weight:700; line-height:1.3;  margin:.8rem 0 .4rem; }
[data-testid="stMarkdownContainer"] h3 { font-size:1.15rem; font-weight:700; line-height:1.3;  margin:.7rem 0 .3rem; }
[data-testid="stMarkdownContainer"] h4 { font-size:1.00rem; font-weight:700; line-height:1.35; margin:.6rem 0 .3rem; }
h1.sleek-h, h2.sleek-h, h3.sleek-h, h4.sleek-h, h2.kpi-label, h3.kpi-label,
h2.card-title, h3.card-title, h2.sect-head, h3.sect-head {
  margin:0; padding:0; font:inherit; color:inherit; letter-spacing:inherit;
}
</style>
"""

# ── The runtime ──────────────────────────────────────────────────────────────
# Filled by replacing the literal token __PAGE__ with a JSON string. No
# %-formatting anywhere, so "%" in comments or code is always safe.
_RUNTIME_JS = r"""
<script>
(function () {
  var PAGE = __PAGE__;
  var P = window.parent, D = P && P.document;
  if (!D) { return; }

  // This script runs in a zero-height helper iframe. Remove that frame from
  // the tab order and the accessibility tree, and title it for auditors.
  try {
    var fe = window.frameElement;
    if (fe) {
      fe.setAttribute('aria-hidden', 'true');
      fe.setAttribute('tabindex', '-1');
      fe.setAttribute('title', 'Accessibility helper (no visible content)');
    }
  } catch (e) {}

  function all(sel, root) { return Array.prototype.slice.call((root || D).querySelectorAll(sel)); }
  function txt(el) {
    if (!el) { return ''; }
    var c = el.cloneNode(true);
    // Drop Streamlit's help "?" tooltip icon text if present.
    Array.prototype.slice.call(c.querySelectorAll('[data-testid="stTooltipIcon"], svg')).forEach(function (n) { n.remove(); });
    return (c.textContent || '').replace(/\s+/g, ' ').trim();
  }
  var seq = 0;
  function ensureId(el, prefix) {
    if (!el.id) { el.id = prefix + '-' + (++seq) + '-' + Math.random().toString(36).slice(2, 7); }
    return el.id;
  }
  function setAttr(el, k, v) { if (el.getAttribute(k) !== v) { el.setAttribute(k, v); } }
  function delAttr(el, k) { if (el.hasAttribute(k)) { el.removeAttribute(k); } }

  // ── Bug 1: main landmark, labelled by the page's visible <h1> ────────────
  function landmarks() {
    var main = D.querySelector('main') ||
               D.querySelector('section.main') ||
               D.querySelector('[data-testid="stMain"]') ||
               D.querySelector('[data-testid="stAppViewContainer"] > section:not([data-testid="stSidebar"])');
    if (main) {
      if (main.tagName !== 'MAIN') { setAttr(main, 'role', 'main'); }
      var h1 = main.querySelector('h1');
      if (h1) {
        setAttr(main, 'aria-labelledby', ensureId(h1, 'a11y-h1'));
        delAttr(main, 'aria-label');
      } else {
        setAttr(main, 'aria-label', PAGE);
      }
    }
    var nav = D.querySelector('[data-testid="stSidebarNav"]');
    if (nav) { setAttr(nav, 'role', 'navigation'); setAttr(nav, 'aria-label', 'Pages'); }
  }

  // ── Bugs 19 + 21: sidebar buttons named, state exposed, hidden when shut ──
  function sidebar() {
    var sb = D.querySelector('section[data-testid="stSidebar"]');
    if (!sb) { return; }
    var sid = ensureId(sb, 'a11y-sidebar');
    setAttr(sb, 'aria-label', 'Navigation menu');
    var open = sb.getAttribute('aria-expanded') !== 'false';
    // inert = out of tab order and accessibility tree. CSS also hides it.
    if (open) { delAttr(sb, 'inert'); } else { setAttr(sb, 'inert', ''); }
    var close = D.querySelector('[data-testid="stSidebarCollapseButton"] button');
    if (close) {
      // Visible text is "Close menu" (CSS below), so the spoken name must
      // contain it too: voice-control users say what they see (WCAG 2.5.3).
      setAttr(close, 'aria-label', 'Close menu');
      setAttr(close, 'aria-expanded', 'true');
      setAttr(close, 'aria-controls', sid);
    }
    // Number inputs: Streamlit's -/+ step buttons have no names.
    all('[data-testid="stNumberInput-StepDown"]').forEach(function (b) { setAttr(b, 'aria-label', 'Decrease'); });
    all('[data-testid="stNumberInput-StepUp"]').forEach(function (b) { setAttr(b, 'aria-label', 'Increase'); });
    var menu = D.querySelector('[data-testid="stMainMenu"] button');
    if (menu) { setAttr(menu, 'aria-label', 'App menu'); }
    var openBtn = D.querySelector('[data-testid="collapsedControl"] button');
    if (openBtn) {
      setAttr(openBtn, 'aria-label', 'Open menu');
      setAttr(openBtn, 'aria-expanded', 'false');
      setAttr(openBtn, 'aria-controls', sid);
    }
  }

  // ── Bug 14 (+ date field): visible labels, associated by for/id ──────────
  // Streamlit sets aria-hidden="true" on EVERY widget label and names inputs
  // with aria-label instead. Unhide the label and associate it natively.
  // Keep an aria-label only when it carries more than the label (comboboxes
  // say "Selected <value>. <label>", which is how the value is announced).
  var WIDGETS = ['stSelectbox', 'stMultiSelect', 'stTextInput', 'stTextArea',
                 'stNumberInput', 'stDateInput', 'stTimeInput', 'stSlider',
                 'stRadio', 'stSelectSlider'];
  function labels() {
    all(WIDGETS.map(function (t) { return '[data-testid="' + t + '"]'; }).join(',')).forEach(function (root) {
      var lab = root.querySelector('[data-testid="stWidgetLabel"]');
      if (!lab) { return; }
      delAttr(lab, 'aria-hidden');
      var ltext = txt(lab);
      var kind = root.getAttribute('data-testid');
      if (kind === 'stRadio' || kind === 'stSlider' || kind === 'stSelectSlider') {
        ensureId(lab, 'a11y-lbl');     // referenced by radios() / sliders()
        return;
      }
      var inp = root.querySelector('input, textarea');
      if (!inp) { return; }
      var iid = ensureId(inp, 'a11y-in');
      if (lab.tagName === 'LABEL') { setAttr(lab, 'for', iid); }
      else { setAttr(inp, 'aria-labelledby', ensureId(lab, 'a11y-lbl')); }
      var al = (inp.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
      if (al && ltext && al.toLowerCase().indexOf(ltext.toLowerCase()) === -1) {
        delAttr(inp, 'aria-label');                  // e.g. date: "Select a date."
      } else if (al && al === ltext) {
        delAttr(inp, 'aria-label');                  // redundant duplicate
      }
    });
  }

  // ── Bug 6: radios grouped natively ───────────────────────────────────────
  // Streamlit renders every radio with name="", so the browser treats each as
  // its own group — announced "1 of 1". A shared name restores native grouping.
  var rgSeq = 0;
  function radios() {
    all('[role="radiogroup"]').forEach(function (rg) {
      if (!rg.dataset.a11yName) { rg.dataset.a11yName = 'a11y-radio-' + (++rgSeq) + '-' + Math.random().toString(36).slice(2, 6); }
      all('input[type="radio"]', rg).forEach(function (r) { setAttr(r, 'name', rg.dataset.a11yName); });
      var root = rg.closest('[data-testid="stRadio"]');
      var lab = root && root.querySelector('[data-testid="stWidgetLabel"]');
      if (lab && lab.id && txt(lab)) {
        setAttr(rg, 'aria-labelledby', lab.id);    // visible text names the group
        delAttr(rg, 'aria-label');
      }
    });
  }

  // ── Bug 24: range slider thumbs say which end they are ───────────────────
  function sliders() {
    all('[data-testid="stSlider"], [data-testid="stSelectSlider"]').forEach(function (root) {
      var thumbs = all('[role="slider"]', root);
      var lt = txt(root.querySelector('[data-testid="stWidgetLabel"]'));
      if (!lt || !thumbs.length) { return; }
      var pct = /%|percent/i.test(lt);
      thumbs.forEach(function (t, i) {
        var name = thumbs.length === 2 ? (lt + (i === 0 ? ', minimum' : ', maximum')) : lt;
        setAttr(t, 'aria-label', name);
        var v = t.getAttribute('aria-valuenow');
        if (pct && v !== null) { setAttr(t, 'aria-valuetext', v + ' percent'); }
      });
    });
  }

  // ── Bug 11: character-count announcements ────────────────────────────────
  // Round 2's selector (textarea[maxlength]) never matched: Streamlit enforces
  // max_chars in JavaScript, not with a maxlength attribute. Its counter is an
  // InputInstructions element reading "25/200", on text inputs AND text areas.
  // The live region sits on <body>, outside React, and exists before any
  // typing — Mark: a region must exist before its content changes.
  function region() {
    var r = D.getElementById('a11y-count-live');
    if (!r) {
      r = D.createElement('div');
      r.id = 'a11y-count-live';
      r.className = 'a11y-sr';
      r.setAttribute('role', 'status');
      r.setAttribute('aria-live', 'polite');
      r.setAttribute('aria-atomic', 'true');
      D.body.appendChild(r);
    }
    return r;
  }
  function counters() {
    all('[data-testid="stTextInput"], [data-testid="stTextArea"]').forEach(function (root) {
      var ins = root.querySelector('[data-testid="InputInstructions"]');
      var fld = root.querySelector('input, textarea');
      if (!ins || !fld) { return; }
      var m = (ins.textContent || '').match(/(\d+)\s*\/\s*(\d+)/);
      if (!m) { return; }
      var max = parseInt(m[2], 10);
      // Permanent description so the limit is announced on focus.
      var did = ensureId(fld, 'a11y-in') + '-limit';
      var d = D.getElementById(did);
      if (!d) {
        d = D.createElement('span'); d.id = did; d.className = 'a11y-sr';
        // aria-hidden keeps this out of the reading order and outside-landmark
        // checks; aria-describedby still reads hidden text by reference.
        d.setAttribute('aria-hidden', 'true');
        d.textContent = 'Up to ' + max + ' characters.';
        D.body.appendChild(d);
      }
      var db = (fld.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
      if (db.indexOf(did) === -1) { db.push(did); setAttr(fld, 'aria-describedby', db.join(' ')); }
      if (fld.dataset.a11yCounter === '1') { return; }
      fld.dataset.a11yCounter = '1';
      region();
      var last = null, timer = null;
      // Cadence per the ticket: small limits announce each change; large
      // limits stay quiet until half used, then every 50, and at the limit.
      fld.addEventListener('input', function () {
        var left = max - fld.value.length, msg = null, key;
        if (max <= 60) {
          key = left; msg = left === 0 ? 'Character limit reached.' : left + ' characters remaining.';
        } else if (left <= 0) {
          key = 'max'; msg = 'Character limit reached. ' + max + ' of ' + max + ' characters used.';
        } else if (fld.value.length >= max / 2) {
          key = Math.floor(left / 50); msg = left + ' characters remaining.';
        } else { last = null; return; }
        if (key === last) { return; }
        last = key;
        clearTimeout(timer);
        timer = setTimeout(function () { var r = region(); r.textContent = ''; setTimeout(function () { r.textContent = msg; }, 60); }, 450);
      });
    });
  }

  // ── Bug 15: every chart is one named image ───────────────────────────────
  // Vega charts (st.bar_chart / st.line_chart) draw to an unnamed <canvas>.
  // Plotly charts carry their name in layout.meta.a11y_name, set by the
  // chart_a11y.plotly_chart() wrapper; otherwise the nearest heading is used.
  function sentence(s) { s = (s || '').trim(); return /[.!?]$/.test(s) ? s : s + '.'; }
  function precedingHeading(el) {
    var hs = all('h1, h2, h3, h4', D.querySelector('section.main') || D.body), best = '';
    hs.forEach(function (h) { if (h.compareDocumentPosition(el) & 4) { best = txt(h); } });
    return best;
  }
  function charts() {
    all('.js-plotly-plot').forEach(function (gd) {
      var nm = gd.layout && gd.layout.meta && gd.layout.meta.a11y_name;
      nm = nm || ('Chart: ' + sentence(precedingHeading(gd) || 'untitled') + ' Chart details with a data table follow.');
      setAttr(gd, 'role', 'img'); setAttr(gd, 'aria-label', nm);
    });
    // Plotly appends a hidden SVG to <body> purely to measure text.
    var tester = D.getElementById('js-plotly-tester');
    if (tester) { setAttr(tester, 'aria-hidden', 'true'); }
    // Plotly's modebar: <a> elements named only by data-title.
    all('.modebar-btn').forEach(function (a) {
      var t = a.getAttribute('data-title');
      if (t) { setAttr(a, 'role', 'button'); setAttr(a, 'aria-label', t); }
      if (!a.hasAttribute('tabindex')) { setAttr(a, 'tabindex', '0'); }
      all('svg', a).forEach(function (s) { setAttr(s, 'aria-hidden', 'true'); });
    });
    all('[data-testid="stArrowVegaLiteChart"]').forEach(function (v) {
      var nm = 'Chart: ' + sentence(precedingHeading(v) || 'untitled') + ' Chart details with a data table follow.';
      setAttr(v, 'role', 'img'); setAttr(v, 'aria-label', nm);
      // Vega draws to a canvas AND an svg inside its own role=graphics-document
      // wrapper ("Vega visualization"). The container's name stands for all of it.
      Array.prototype.slice.call(v.children).forEach(function (c) { setAttr(c, 'aria-hidden', 'true'); });
    });
    // BaseWeb draws a chevron titled "open" inside every selectbox/multiselect,
    // announced as an image named "open". The combobox already exposes its
    // expanded state, so the chevron is decoration.
    all('[data-testid="stSelectbox"] svg[data-baseweb="icon"], [data-testid="stMultiSelect"] svg[data-baseweb="icon"], ' +
        '[data-testid="stDateInput"] svg[data-baseweb="icon"], [data-testid="stTimeInput"] svg[data-baseweb="icon"]')
      .forEach(function (s) {
        var b = s.closest('button, [role="button"]');
        if (!b || !b.getAttribute('aria-label')) { setAttr(s, 'aria-hidden', 'true'); }
      });
    // Any SVG with no name of its own is decoration: hide it.
    all('svg:not([aria-hidden]):not([role="img"]):not([aria-label]):not([aria-labelledby])').forEach(function (s) {
      if (s.querySelector('title')) { return; }
      if (s.closest('[role="img"]') && s.closest('[role="img"]') !== s) { setAttr(s, 'aria-hidden', 'true'); return; }
      setAttr(s, 'aria-hidden', 'true');
    });
  }

  // ── Bug 17: name the element-toolbar buttons ─────────────────────────────
  // Streamlit renders download / search / fullscreen as icon-only buttons with
  // no name. Identified by icon path (verified in 1.37.1).
  var ICONS = [['M18 15v3H6', 'Download table as CSV'], ['M5 20h14', 'Download table as CSV'],
               ['M15.5 14h', 'Search table'], ['M7 14H5v5h5', 'View fullscreen'],
               ['M5 16h3v3h2v-5H5v2', 'Exit fullscreen']];
  function toolbars() {
    all('[data-testid="stElementToolbar"] button, [data-testid="StyledFullScreenButton"]').forEach(function (b) {
      if (b.getAttribute('aria-label')) { return; }
      var nm = b.getAttribute('title');
      if (!nm) {
        var d = all('path', b).map(function (p) { return p.getAttribute('d') || ''; }).filter(function (x) { return x.indexOf('M0 0h24') !== 0; })[0] || '';
        ICONS.forEach(function (pair) { if (!nm && d.indexOf(pair[0]) === 0) { nm = pair[1]; } });
      }
      if (nm) { setAttr(b, 'aria-label', nm); }
    });
  }

  // ── Bug 8 (ready for test — preserved): redundant checkbox aria-label ─────
  function checkboxes() {
    all('input[type="checkbox"][aria-label], input[role="switch"][aria-label]').forEach(function (inp) {
      var lab = inp.closest('label');
      if (!lab) { return; }
      var v = (lab.textContent || '').replace(/\s+/g, ' ').trim();
      var al = (inp.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
      if (v && (v === al || v.indexOf(al) === 0 || al.indexOf(v) === 0)) { inp.removeAttribute('aria-label'); }
    });
  }

  // ── Bug 3: dropdown placeholder text ("Choose an option") ────────────────
  // Walkthrough finding. BaseWeb draws the placeholder as plain text at 60%
  // opacity of the text colour: exactly the 4.15:1 in Mark's report, on every
  // empty dropdown. Its classes are generated, so CSS can't target it
  // reliably. Give any faded text inside a dropdown a solid grey (5.86:1),
  // still visibly lighter than a real selection.
  function placeholders() {
    all('[data-baseweb="select"] div, [data-baseweb="select"] span').forEach(function (el) {
      if (el.childElementCount || !(el.textContent || '').trim()) { return; }
      var c = P.getComputedStyle(el).color, m = c.match(/rgba\([^)]*,\s*([\d.]+)\)/);
      if (m && parseFloat(m[1]) < 0.95) { el.style.setProperty('color', '#5A6673', 'important'); }
    });
  }

  // ── Layout: invisible elements must not take up space ────────────────────
  // Walkthrough finding: every st.markdown("<style>…") block and every hidden
  // helper frame (this script's own iframe, focus_on()) is a separate
  // Streamlit element, and Streamlit puts a 16px gap between elements whether
  // they're visible or not. The helper frames also rendered ~26px tall.
  // Together that was ~150px of empty space above every page. Take such
  // elements out of the flow (absolute, 0x0): they keep working, but no
  // longer push content down. Never touches anything with visible content.
  var COLLAPSE = 'position:absolute!important;width:0!important;height:0!important;' +
                 'margin:0!important;padding:0!important;overflow:hidden!important;pointer-events:none!important;';
  function collapseInvisible() {
    all('.element-container, [data-testid="element-container"], [data-testid="stElementContainer"]').forEach(function (c) {
      if (c.dataset.a11yCollapsed) { return; }
      var ifr = c.querySelector('iframe');
      var helper = ifr && /^Accessibility helper/.test(ifr.getAttribute('title') || '');
      var styleOnly = !ifr && c.querySelector('style') && !(c.innerText || '').trim() &&
                      !c.querySelector('img, svg, canvas, input, button, select, textarea, table, video, [role]');
      if (helper || styleOnly) {
        c.setAttribute('style', (c.getAttribute('style') || '') + ';' + COLLAPSE);
        c.dataset.a11yCollapsed = '1';
      }
    });
  }

  // ── Bug 22: links Streamlit opens in a new tab on its own ───────────────
  // Live finding (Roadmap): Streamlit's markdown renderer adds target="_blank"
  // to every link, so a plain markdown link opened a new tab without saying
  // so. Email/phone links don't need a new tab at all; web links get the
  // warning in their name. Links that already carry NEW_TAB are left alone.
  function newTabLinks() {
    all('a[target="_blank"]').forEach(function (a) {
      var href = a.getAttribute('href') || '';
      if (/^(mailto|tel):/i.test(href)) { delAttr(a, 'target'); return; }
      if (/opens in new tab/i.test(a.textContent || '') || /opens in new tab/i.test(a.getAttribute('aria-label') || '')) { return; }
      var t = (a.textContent || '').replace(/\s+/g, ' ').trim();
      if (t) { setAttr(a, 'aria-label', t + ' (opens in new tab)'); }
    });
  }

  // ── Bug 27: put focus back when Streamlit rebuilds the focused control ────
  // QA (VoiceOver video, Oct 7): while arrowing a My Tracker slider, focus
  // occasionally jumped to the top of the page. That happens when Streamlit
  // rebuilds the focused element during a rerun: the old node is removed and
  // focus falls to <body>. We remember the focused control by role + name; if
  // it is removed and focus lands on <body> without a click, focus moves to
  // the rebuilt control with the same role and name. A deliberate click or Tab
  // elsewhere is never overridden (the old element is still in the page, or
  // a pointer event just happened, or focus is somewhere other than <body>).
  // State lives on the parent window because this helper frame is replaced on
  // reruns; each new helper takes ownership and older listeners go quiet.
  var OWNER = Math.random().toString(36).slice(2);
  P.__a11yOwner = OWNER;
  function nameOf(el) {
    var n = el.getAttribute('aria-label') || '';
    if (!n && el.id) {
      var l = null;
      try { l = D.querySelector('label[for="' + (P.CSS && P.CSS.escape ? P.CSS.escape(el.id) : el.id) + '"]'); } catch (e) {}
      if (l) { n = txt(l); }
    }
    if (!n && el.getAttribute('aria-labelledby')) {
      n = el.getAttribute('aria-labelledby').split(/\s+/).map(function (id) { return txt(D.getElementById(id)); }).join(' ');
    }
    if (!n) { n = el.getAttribute('title') || ''; }
    if (!n && /^(BUTTON|A)$/.test(el.tagName)) { n = txt(el); }
    return n.replace(/\s+/g, ' ').trim().slice(0, 120);
  }
  function sigOf(el) {
    return (el.getAttribute('role') || el.tagName.toLowerCase()) + '|' + (el.getAttribute('type') || '') + '|' + nameOf(el);
  }
  // A slider handle is remembered by its slider's visible label plus WHICH
  // handle (first/second). Live finding (Oct 27): matching by name alone put
  // focus back on the maximum handle when the user had been on the minimum.
  function sliderSpot(el) {
    if (el.getAttribute('role') !== 'slider') { return null; }
    var box = el.closest('[data-testid="stSlider"]');
    if (!box) { return null; }
    var lab = box.querySelector('[data-testid="stWidgetLabel"]');
    var thumbs = [].slice.call(box.querySelectorAll('[role="slider"]'));
    return { label: lab ? txt(lab) : '', idx: thumbs.indexOf(el) };
  }
  function remember(el) {
    if (!el || el === D.body || !el.closest) { return; }
    if (!el.closest('[role="main"], section.main, [data-testid="stSidebar"]')) { return; }
    var s = sigOf(el);
    if (s.split('|')[2]) { P.__a11yFocus = { el: el, sig: s, spot: sliderSpot(el), at: Date.now() }; }
  }
  D.addEventListener('pointerdown', function () { if (P.__a11yOwner === OWNER) { P.__a11yPointerAt = Date.now(); } }, true);
  D.addEventListener('focusin', function (ev) { if (P.__a11yOwner === OWNER) { remember(ev.target); } }, true);
  D.addEventListener('keydown', function (ev) { if (P.__a11yOwner === OWNER) { remember(ev.target); } }, true);
  function recoverFocus() {
    var f = P.__a11yFocus;
    if (!f) { return; }
    var a = D.activeElement;
    if (a && a !== D.body && a !== D.documentElement) { return; }   // focus is somewhere real
    if (f.el && f.el.isConnected) { return; }                       // not rebuilt: user moved on
    if (Date.now() - (P.__a11yPointerAt || 0) < 1500) { return; }   // a click just happened
    if (Date.now() - f.at > 20000) { P.__a11yFocus = null; return; }
    var c = [];
    if (f.spot && f.spot.idx >= 0) {
      all('[data-testid="stSlider"]').forEach(function (box) {
        var lab = box.querySelector('[data-testid="stWidgetLabel"]');
        var th = [].slice.call(box.querySelectorAll('[role="slider"]'));
        if (box.offsetParent !== null && lab && txt(lab) === f.spot.label && th[f.spot.idx]) { c.push(th[f.spot.idx]); }
      });
    }
    if (!c.length) {
      c = all('[role="slider"], [role="combobox"], [role="radio"], [role="checkbox"], [role="switch"], [role="tab"], input, textarea, select, button, a[href]')
        .filter(function (e) { return e.offsetParent !== null && sigOf(e) === f.sig; });
    }
    if (!c.length) { return; }
    try { c[0].focus({ preventScroll: false }); } catch (e) { c[0].focus(); }
    P.__a11yFocus = { el: c[0], sig: f.sig, spot: f.spot, at: Date.now() };
  }

  // ── Bug 10: sidebar sections as labelled groups (QA, 2026-10-22) ───────────
  // QA asked for a fieldset per sidebar section with its title as the legend.
  // Streamlit draws its own containers, so a section's container (drawn with
  // the title inside it — see _side_group in views/2_Explorer.py) is marked
  // role="group" and labelled by the title: the ARIA equivalent, announced as
  // e.g. "Category & Subcategory, group" when a screen reader enters it.
  function groups() {
    all('[data-a11y-group-title]').forEach(function (t, i) {
      var box = t.closest('[data-testid="stVerticalBlock"]');
      if (!box || box.closest('[data-testid="stSidebarUserContent"]') === box) { return; }
      if (!t.id) { t.id = 'a11y-grp-' + i + '-' + txt(t).toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 40); }
      setAttr(box, 'role', 'group');
      setAttr(box, 'aria-labelledby', t.id);
    });
  }

  var FIXES = [collapseInvisible, newTabLinks, groups, landmarks, sidebar, labels, radios, sliders, counters, charts, toolbars, checkboxes, placeholders, recoverFocus];
  function stamp() {
    FIXES.forEach(function (f) { try { f(); } catch (e) { if (P.console) { P.console.warn('a11y ' + f.name + ': ' + e.message); } } });
  }
  var pending = false;
  function schedule() {
    if (pending) { return; }
    pending = true;
    (P.requestAnimationFrame || setTimeout)(function () { pending = false; stamp(); }, 50);
  }
  stamp();
  try {
    new MutationObserver(schedule).observe(D.body, {
      childList: true, subtree: true, attributes: true,
      attributeFilter: ['aria-expanded', 'aria-hidden', 'aria-valuenow', 'aria-label']
    });
  } catch (e) {}
  // Backstop: Streamlit destroys this iframe on rerun; each new one re-stamps.
  var n = 0, t = setInterval(function () { stamp(); if (++n > 20) { clearInterval(t); setInterval(stamp, 2500); } }, 500);
})();
</script>
"""


def apply_page_a11y(page_label: str = "Page") -> None:
    """Inject the accessibility CSS and runtime for this page.

    Call once per page right after require_auth(). Not guarded by session
    state: Streamlit rebuilds the DOM every rerun, so it must re-emit.
    """
    st.markdown(_A11Y_CSS, unsafe_allow_html=True)
    components.html(_RUNTIME_JS.replace("__PAGE__", _json.dumps(str(page_label))),
                    height=0, width=0)


def wire_char_counters(threshold: int = 100) -> None:
    """Kept for backward compatibility. Counters are now handled for every
    text input and text area by the apply_page_a11y() runtime."""
    return None


# ── Bug 9: move focus to content that appears after a rerun ─────────────────
_FOCUS_JS = r"""
<script>
(function () {
  var SEL = __SEL__, D = window.parent && window.parent.document;
  try { var fe = window.frameElement; if (fe) { fe.setAttribute('aria-hidden','true'); fe.setAttribute('tabindex','-1'); fe.setAttribute('title','Accessibility helper (no visible content)'); } } catch (e) {}
  if (!D) { return; }
  var tries = 0;
  (function go() {
    var el = D.querySelector(SEL);
    if (el) {
      el.setAttribute('tabindex', '-1');
      el.setAttribute('data-a11y-focus-target', '');
      el.focus({ preventScroll: false });
      // Guard (found on the live site): focus was being moved back onto the
      // page's main section after landing here. For a few seconds, if focus
      // drops to <body> or the main section — nothing the user chose — put it
      // back. Moving to a real control (e.g. Tab to a button) is left alone.
      var until = Date.now() + 4000;
      (function guard() {
        if (Date.now() > until || !D.contains(el)) { return; }
        var a = D.activeElement;
        if (!a || a === D.body || (a.matches && a.matches('section.main, [role="main"], [data-testid="stMain"]'))) {
          el.focus({ preventScroll: true });
        }
        setTimeout(guard, 120);
      })();
      return;
    }
    if (++tries < 40) { setTimeout(go, 100); }
  })();
})();
</script>
"""


def focus_on(selector: str) -> None:
    """Move keyboard/screen-reader focus to the element matching `selector`.

    Mark (bug 9): a live region created together with its content is never
    announced; either the region must pre-exist, or focus must move to the new
    content with JS and tabindex=-1. This does the latter. Call it only on the
    rerun where the content first appears, or focus will jump on every rerun.
    """
    components.html(_FOCUS_JS.replace("__SEL__", _json.dumps(selector)), height=0, width=0)


# ── Bug 13: a value and its label read as one sentence ──────────────────────
def kpi_html(value: str, label: str, *, value_style: str = "", label_style: str = "",
             wrap_style: str = "", value_first: bool = True, sr_text: str = None) -> str:
    """HTML for a stacked value/label tile that a screen reader reads as ONE
    phrase ("9,134 introduced"), with no list or description-list noise.

    The visible parts are aria-hidden; a single visually hidden span carries
    the full phrase. `sr_text` overrides the spoken phrase when the natural
    order reads badly.
    """
    v, l = str(value), str(label)
    spoken = sr_text if sr_text else (f"{v} {l}" if value_first else f"{l}: {v}")
    top, bottom = ((v, value_style), (l, label_style)) if value_first else ((l, label_style), (v, value_style))
    return (f'<div class="kpi-read" style="{wrap_style}">'
            f'<span class="a11y-sr">{_html.escape(spoken)}</span>'
            f'<span aria-hidden="true" style="display:block;{top[1]}">{top[0]}</span>'
            f'<span aria-hidden="true" style="display:block;{bottom[1]}">{bottom[0]}</span>'
            f'</div>')


def live_status(message: str, assertive: bool = False) -> None:
    """A role=status/alert block. NOTE (bug 9): only announced if its content
    changes after it exists. For content appearing after a rerun, use
    focus_on() instead."""
    role = "alert" if assertive else "status"
    live = "assertive" if assertive else "polite"
    st.markdown(f'<div role="{role}" aria-live="{live}" aria-atomic="true">{message}</div>',
                unsafe_allow_html=True)


def sr_only(text: str) -> None:
    """Emit visually-hidden text."""
    st.markdown(f'<span class="a11y-sr">{_html.escape(str(text))}</span>', unsafe_allow_html=True)


# ── Bug 22: links that open a new tab ────────────────────────────────────────
NEW_TAB = ('<span aria-hidden="true">&nbsp;&#8599;</span>'
           '<span class="a11y-sr"> (opens in new tab)</span>')
