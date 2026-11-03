"""
app.py — Router for the Illinois Legislative Intelligence Platform
==================================================================
Auth gate: university magic-link only (@illinois.edu / @uillinois.edu).

The legacy admin-password path was removed — every session is now tied to a
real, identifiable university account, so access is always attributable.

Magic-link flow:
  a. User enters university email → POST request → magic link emailed
  b. User clicks link → ?magic=<token>&email=<email> query params
  c. verify_magic_link() validates token → session created → user_authed=True
  d. Every page calls require_user_auth() from user_auth.py

Session state keys set here:
  user_authed  bool  — university user auth
  user_record  dict  — loaded user profile
  session_id   str   — Cosmos session ID
"""
import os
import streamlit as st

# ── Page config (set ONLY here) ───────────────────────────────────────────────
st.set_page_config(
    page_title="IL Legislative Intelligence",
    page_icon="🏛",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Initialise session state keys ─────────────────────────────────────────────
for key, default in [
    ("user_authed", False),
    ("user_record", None),
    ("session_id",  None),
    ("magic_sent",  False),
    ("magic_email", ""),
]:
    if key not in st.session_state:
        st.session_state[key] = default

# ── Magic-link callback — runs before any UI renders ─────────────────────────
def _handle_magic_link_params():
    """Check URL query params for a magic-link token and verify it."""
    params = st.query_params
    token  = params.get("magic", "")
    email  = params.get("email", "")

    if not token or not email:
        return  # Normal load — nothing to do

    if st.session_state.user_authed:
        # Already authenticated. Strip the one-time magic token/email from the
        # URL, but KEEP any sid so a later refresh can still restore this session.
        for _p in ("magic", "email"):
            try:
                del st.query_params[_p]
            except Exception:
                pass
        return

    try:
        from user_auth import verify_magic_link, get_user
        ok, result = verify_magic_link(token, email)
    except Exception as e:
        for _p in ("magic", "email"):
            try:
                del st.query_params[_p]
            except Exception:
                pass
        st.session_state["_auth_error"] = f"Auth service error: {e}"
        return

    # Strip the one-time magic token + email from the URL (security), but do
    # NOT blanket-clear — we're about to write the durable sid below.
    for _p in ("magic", "email"):
        try:
            del st.query_params[_p]
        except Exception:
            pass

    if ok:
        session_id  = result
        user_record = get_user(email)
        st.session_state.user_authed  = True
        st.session_state.session_id   = session_id
        st.session_state.user_record  = user_record
        st.session_state.magic_sent   = False
        st.session_state.magic_email  = ""
        # ── Persist the session in the URL so a page refresh, server recycle,
        #    or crash can silently restore it (auth_utils._try_restore_from_url).
        #    This is the wire that was never connected: without it, refresh =
        #    logged out, and _user_id goes empty so saves silently no-op.
        st.query_params["sid"] = session_id
    else:
        st.session_state["_auth_error"] = result


_handle_magic_link_params()

# ── Restore a persisted session on refresh / recycle ─────────────────────────
# app.py is the router and runs BEFORE any page's require_auth(). On a refresh,
# in-memory session_state is empty and there's no ?magic= token, so without this
# the login gate below would fire and st.stop() before the page ever gets a
# chance to restore from ?sid=. We therefore attempt the same restore here, so a
# refresh anywhere in the app silently resumes the session instead of bouncing
# the user back to sign-in.
#
# DIAGNOSTIC: append ?debug=1 to the URL to see exactly what the restore sees.
# Remove this block once the persistence issue is confirmed fixed.
# SECURITY (audit follow-up, not a ReQtest item): this panel prints the
# session id and the authenticated email to anyone who appends ?debug=1.
# Now additionally gated on an explicit server-side opt-in so it cannot be
# turned on from the URL alone. Set AUTH_DEBUG=1 in App Service settings to
# re-enable while troubleshooting.
_DEBUG_AUTH = (st.query_params.get("debug") == "1"
               and os.environ.get("AUTH_DEBUG") == "1")
_dbg = {"had_session_in_state": bool(st.session_state.get("user_authed")),
        "sid_in_url": st.query_params.get("sid", "(none)"),
        "get_session_found": None,
        "restored": None,
        "error": None}

if not st.session_state.get("user_authed"):
    try:
        from auth_utils import _try_restore_from_url
        # Instrument: check what get_session returns for the sid, independent of restore
        _sid_probe = st.query_params.get("sid", "")
        if _sid_probe:
            try:
                from user_auth import get_session as _gs_probe
                _found = _gs_probe(_sid_probe)
                _dbg["get_session_found"] = bool(_found)
                if _found:
                    _dbg["session_email"] = _found.get("email", "(no email)")
            except Exception as _e:
                _dbg["error"] = f"get_session: {_e}"
        _dbg["restored"] = _try_restore_from_url()
    except Exception as _e:
        _dbg["error"] = f"restore: {_e}"
        pass  # fall through to the login gate if restore is unavailable

if _DEBUG_AUTH:
    st.warning(
        "**Auth debug** (append `?debug=1` to see this):\n\n"
        f"- sid in URL: `{_dbg['sid_in_url']}`\n"
        f"- session already in memory: `{_dbg['had_session_in_state']}`\n"
        f"- get_session found a record: `{_dbg['get_session_found']}`\n"
        f"- session email: `{_dbg.get('session_email', '(n/a)')}`\n"
        f"- restore succeeded: `{_dbg['restored']}`\n"
        f"- now user_authed: `{st.session_state.get('user_authed')}`\n"
        f"- error: `{_dbg['error']}`"
    )

# ── Keep the persistence token in the address (router-level) ─────────────────
# st.navigation / st.switch_page clears query params on page changes, dropping
# the ?sid= written at login. Bug 27 (QA, 2026-10-27): writing it back with
# st.query_params rebuilt the page's widgets on the first rerun after every
# page change (sliders lost focus and values). keep_sid_in_url() restores it in
# the browser instead, so a refresh from ANY page still restores the session.
try:
    if st.session_state.get("user_authed"):
        from auth_utils import keep_sid_in_url
        keep_sid_in_url(st.session_state.get("session_id"))
except Exception:
    pass

# ── Shared login-page CSS ─────────────────────────────────────────────────────
LOGIN_CSS = """
<style>
/* Bug 26 follow-up: one font app-wide. Inter (a Google Fonts download) was
   used on some pages and Streamlit's built-in Source Sans Pro on others. */
#MainMenu, footer, header[data-testid="stHeader"] { display:none !important; }
[data-testid="stSidebarNav"]                       { display:none !important; }
.block-container { padding-top:3rem !important; }

.login-card {
  max-width:440px; margin:0 auto; padding:2.5rem 2.8rem;
  border-radius:18px; border:1px solid #D6DEE0;
  background:#fff; box-shadow:0 4px 28px rgba(0,0,0,0.09);
}
.login-header-grad {
  background:linear-gradient(135deg,#0F1F38 0%,#1B4B5A 55%,#2D6A7A 100%);
  border-radius:12px; padding:22px 24px; margin-bottom:24px; text-align:center;
}
.tab-row {
  display:flex; gap:0; margin-bottom:22px;
  border:1px solid #E2E8F0; border-radius:10px; overflow:hidden;
}
.tab-btn {
  flex:1; padding:10px 0; font-size:13px; font-weight:600;
  cursor:pointer; border:none; background:#F8FAFC; color:#454F5A;
  transition:all .15s;
}
.tab-btn.active { background:#13294B; color:#fff; }
.sent-box {
  background:#F0FDF4; border:1px solid #BBF7D0; border-radius:10px;
  padding:18px 20px; text-align:center;
}
</style>
"""

# ── If neither auth mode is active, show login ───────────────────────────────
if not st.session_state.user_authed:
    st.markdown(LOGIN_CSS, unsafe_allow_html=True)
    # Audit issues 1/2/16/17: the login screen never reaches a page module, so
    # it needs the shared accessibility chrome applied here.
    try:
        from a11y import apply_page_a11y
        apply_page_a11y("Sign in")
    except Exception:
        pass

    _, center, _ = st.columns([1, 1.5, 1])
    with center:
        # Header
        st.markdown("""
        <div class="login-card">
          <div class="login-header-grad">
            <div style="font-size:2rem;margin-bottom:6px" aria-hidden="true">🏛</div>
            <h1 style="color:#fff;font-size:20px;font-weight:800;letter-spacing:-.03em;
                       margin:0;padding:0;line-height:1.3">
              IL Legislative Intelligence
            </h1>
            <div style="color:rgba(255,255,255,.92);font-size:11px;margin-top:4px">
              Institute of Government and Public Affairs · University of Illinois System
            </div>
          </div>
        </div>
        """, unsafe_allow_html=True)

        # Show any auth errors
        if st.session_state.get("_auth_error"):
            st.error(st.session_state.pop("_auth_error"))

        # ── University sign-in (the only auth path) ──────────────────────────
        if st.session_state.magic_sent:
            # Confirmation state
            sent_email = st.session_state.magic_email
            # Bug 9 (round 3). Round 2 put role="status" on this box. Mark:
            # a live region created together with its content is never
            # announced — it must pre-exist, or focus must move to the new
            # content. After st.rerun() nothing pre-exists, so focus moves to
            # the heading (tabindex=-1), once, on the rerun that shows it.
            # The status role is removed so no reader announces it twice.
            # The envelope emoji is decorative (bug 7).
            st.markdown(f"""
            <div class="sent-box">
              <div style="font-size:1.8rem;margin-bottom:8px" aria-hidden="true">📬</div>
              <h2 style="font-weight:700;font-size:15px;color:#15803D;margin:0 0 6px">
                Check your inbox
              </h2>
              <div style="font-size:13px;color:#166534;line-height:1.6">
                We sent a sign-in link to<br>
                <strong>{sent_email}</strong><br><br>
                Click the link in that email to continue.<br>
                It expires in <strong>1 hour</strong>.
              </div>
            </div>
            """, unsafe_allow_html=True)
            if st.session_state.pop("_focus_sent", False):
                try:
                    from a11y import focus_on
                    # Streamlit replaces ids on markdown headings with its own
                    # anchor slug, so target the heading by structure.
                    focus_on(".sent-box h2")
                except Exception:
                    pass
            st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

            # Dev mode — show link directly if ACS not configured
            if st.session_state.get("_dev_magic_link"):
                st.info(f"**Dev mode** — ACS not configured. [Click here to sign in]({st.session_state._dev_magic_link})")

            # Audit issue 7: the arrow was part of the accessible name.
            if st.button("Use a different email", use_container_width=True):
                st.session_state.magic_sent  = False
                st.session_state.magic_email = ""
                st.session_state.pop("_dev_magic_link", None)
                st.rerun()

        else:
            # Email entry state
            st.markdown("""
            <p style="font-size:13px;color:#475569;margin-bottom:14px;line-height:1.6">
              Enter your University of Illinois email address to receive
              a secure, one-time sign-in link.
            </p>
            """, unsafe_allow_html=True)

            # Audit issue 20 (WCAG 2.1.1): the input and the button used to be
            # siblings with no form, so pressing Enter in the field did
            # nothing. st.form gives implicit submission for free.
            # Audit issue 12 (WCAG 3.3.2): the label was collapsed and the
            # placeholder was carrying it. The label is now persistently
            # visible and the placeholder is only an example.
            with st.form("login_form", clear_on_submit=False, border=False):
                email_input = st.text_input(
                    "University email address",
                    placeholder=__import__("os").environ.get('LEG_COMPONENTS_WEBAPP_APP_PY_EMAIL_INPUT_L300', ""),
                    key="email_input_field",
                    help="Use your @illinois.edu or @uillinois.edu address.",
                )
                _submitted = st.form_submit_button(
                    "Send Sign-In Link", use_container_width=True, type="primary")

            if _submitted:
                email_clean = email_input.strip().lower()

                try:
                    from user_auth import is_valid_university_email, generate_magic_link
                    from email_service import send_magic_link

                    if not email_clean:
                        st.error("Please enter your email address.")
                    elif not is_valid_university_email(email_clean):
                        st.error("Please use your @illinois.edu or @uillinois.edu email address.")
                    else:
                        with st.spinner("Sending sign-in link…"):
                            link = generate_magic_link(email_clean)

                        if link is None:
                            st.error("Could not generate sign-in link. Please contact your administrator.")
                        else:
                            sent, msg = send_magic_link(email_clean, link)
                            st.session_state.magic_sent  = True
                            st.session_state.magic_email = email_clean
                            st.session_state["_focus_sent"] = True   # bug 9: focus once
                            if not sent:
                                # Dev fallback — ACS not configured
                                st.session_state["_dev_magic_link"] = msg
                            st.rerun()

                except ImportError as e:
                    st.error(f"Auth module not available: {e}")

            st.markdown("""
            <p style="font-size:11px;color:#454F5A;text-align:center;margin-top:12px">
              Access is restricted to University of Illinois affiliates.<br>
              Only @illinois.edu and @uillinois.edu addresses are accepted.
            </p>
            """, unsafe_allow_html=True)

    st.stop()

# ── Navigation ────────────────────────────────────────────────────────────────
# Audit issue 7: st.Page(icon=...) renders the emoji into the link text, so a
# screen reader announces "bar chart, Trends". Streamlit exposes no way to mark
# it decorative from Python, so a11y.inject (see a11y.py) sets aria-hidden on
# the emoji span in the parent document instead; the icons stay for sighted
# users. Validation's icon was also a duplicate 📊 (same as Trends), carrying no
# distinguishing information — changed to 📋 to match the home page card.
# Audit bug 7, second pass: the auditor's step 2 is "observe menu icons".
# st.Page(icon=...) injects the emoji into the link text itself, and whether
# Streamlit renders it as a separate <span> that can be marked decorative is
# version-dependent. Removing the icons is the only guaranteed fix; the home
# page cards keep theirs (marked aria-hidden in our own markup).
pages = [
    st.Page("home.py",                 title="Home",             default=True),
    st.Page("views/6_My_Tracker.py",   title="My Tracker"),
    st.Page("views/1_Trends.py",       title="Trends"),
    st.Page("views/2_Explorer.py",     title="Explorer"),
    st.Page("views/4_Active_Bills.py", title="Bill Predictions"),
    st.Page("views/5_Validation.py",   title="Validation"),
    st.Page("views/7_Roadmap.py",      title="Roadmap"),
]

# ── Sidebar: user info + logout ───────────────────────────────────────────────
with st.sidebar:
    if st.session_state.user_authed and st.session_state.user_record:
        u = st.session_state.user_record
        display = u.get("display_name") or u.get("email", "")
        campus  = u.get("campus") or ""
        st.markdown(f"""
        <div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:10px;
                    padding:12px 14px;margin-bottom:10px;">
          <div style="font-size:12px;font-weight:700;color:#0F172A;
                      white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
            {display}
          </div>
          {"<div style='font-size:11px;color:#454F5A;margin-top:2px'>" + campus + "</div>" if campus else ""}
        </div>
        """, unsafe_allow_html=True)

    if st.button("Sign Out", use_container_width=True):
        # Invalidate server-side session if user-authed
        if st.session_state.session_id:
            try:
                from user_auth import invalidate_session
                invalidate_session(st.session_state.session_id)
            except Exception:
                pass
        for key in ["user_authed", "user_record", "session_id",
                    "magic_sent", "magic_email", "_dev_magic_link"]:
            st.session_state[key] = False if key == "user_authed" else None
        # Clear the durable session token from the URL, else a refresh right
        # after logout would silently restore the session via ?sid=.
        try:
            st.query_params.clear()
        except Exception:
            pass
        st.rerun()

nav = st.navigation(pages)

# Audit issues 1 + 2: stamp the main/navigation landmark roles onto Streamlit's
# own containers before the page body renders. Individual pages call
# apply_page_a11y() too (which is idempotent) so that the roles survive a
# direct page load as well as a nav click.
try:
    from a11y import apply_page_a11y
    apply_page_a11y(getattr(nav, "title", "Page"))
except Exception:
    pass

nav.run()
