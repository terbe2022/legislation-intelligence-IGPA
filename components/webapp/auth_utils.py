"""
auth_utils.py
=============
Shared authentication helper for every page in the multi-page app.

Accepts:
  - user_authed=True (university magic link login)

The admin-password path was removed: every session is now tied to a real
university account, so access is always attributable to a known person.

Usage at the top of each page file:
    from auth_utils import require_auth
    require_auth()

PERSISTENT SESSIONS
-------------------
Streamlit keeps auth in st.session_state, which is in-memory and dies on a
page refresh, a server recycle, or an app crash — dumping the user back to the
login screen. To survive that, on login app.py writes the Cosmos session_id
into the URL (?sid=...). require_auth() below looks for that token when the
in-memory session is empty, re-validates it against Cosmos (get_session), and
silently restores the session so the user stays logged in across refreshes.

"""
import streamlit as st


def _try_restore_from_url() -> bool:
    """
    Attempt to restore an authenticated session from URL query params.
    Returns True if a session was restored (session_state now reflects login),
    False otherwise. Safe to call on every page load — it no-ops when there's
    nothing to restore or when already authenticated.
    """
    # Already authenticated in this run — nothing to do.
    if st.session_state.get("user_authed"):
        return True

    params = st.query_params

    # ── University session token ─────────────────────────────────────────────
    sid = params.get("sid", "")
    if not sid:
        return False

    # Validate the token against Cosmos. Import lazily so a missing/partial
    # user_auth never hard-crashes the gate — worst case we fall through to login.
    try:
        from user_auth import get_session, get_user
    except Exception:
        return False

    try:
        sess = get_session(sid)
    except Exception:
        sess = None

    if not sess:
        # Stale/expired/invalid token — strip it so the URL doesn't keep a dead sid.
        try:
            del st.query_params["sid"]
        except Exception:
            pass
        return False

    email = sess.get("email")
    if not email:
        return False

    # Rebuild the in-memory session from the validated record.
    user_record = None
    try:
        user_record = get_user(email)
    except Exception:
        user_record = {"email": email}

    st.session_state["user_authed"] = True
    st.session_state["session_id"]  = sid
    st.session_state["user_record"] = user_record or {"email": email}
    return True


def require_auth() -> None:
    """
    Block page rendering if the user is not authenticated.
    Accepts both admin (authed) and university (user_authed) sessions.

    Before blocking, attempts a silent restore from the URL token so a refresh
    or crash doesn't force the user to log in again.
    """
    # First, try to resume a persisted session (handles refresh / recycle / crash).
    _try_restore_from_url()

    is_user = st.session_state.get("user_authed", False)

    # (Bug 27, QA 2026-10-27) The ?sid= repair now lives in keep_sid_in_url(),
    # called once per run from app.py. Writing st.query_params here made
    # Streamlit rebuild the page's widgets on the first rerun after each page
    # change: sliders lost focus and values. Left as a no-op for reference.
    # ── Re-stamp the persistence token on EVERY page run (disabled) ──────────
    # Streamlit's st.navigation / st.switch_page (1.37.x) clears URL query
    # params on page changes, silently dropping the ?sid= written at login.
    # Without it, the next refresh or websocket reconnect cannot restore the
    # session and bounces the user to sign-in. Repair the URL each run so the
    # token survives navigation. Idempotent: only writes when missing/stale.
    try:
        if is_user:
            _sid = st.session_state.get("session_id")
            if _sid:
                pass  # handled by keep_sid_in_url() in app.py
    except Exception:
        pass  # URL repair must never break a page

    if not is_user:
        st.markdown(
            """
            <div style="
                max-width: 460px; margin: 6rem auto;
                padding: 2rem; border-radius: 14px;
                border: 1px solid #D6DEE0;
                background: #fff; text-align: center;
                box-shadow: 0 2px 12px rgba(0,0,0,0.08);
            ">
              <div style="font-size:2rem; margin-bottom:.5rem">🔐</div>
              <div style="font-weight:700; font-size:1.15rem; margin-bottom:.5rem">
                Authentication Required
              </div>
              <div style="color:#5b6b71; font-size:.95rem">
                Please log in from the <strong>Home</strong> page to continue.
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.stop()


def keep_sid_in_url(sid: str) -> None:
    """Keep ?sid=<token> in the browser address WITHOUT using st.query_params.

    Bug 27 (QA, 2026-10-27): st.navigation drops query params on page changes,
    and the app used to write ?sid= back with st.query_params on the next
    rerun. In Streamlit 1.37 that write rebuilds the page's widgets: a slider
    being used lost keyboard focus and could show a stale value. This hidden
    helper restores ?sid= with history.replaceState instead: no rerun, nothing
    rebuilt. A refresh still sends ?sid=, so session restore is unchanged.
    """
    import json
    import streamlit.components.v1 as components
    if not sid:
        return
    components.html(
        "<script>(function(){var P=window.parent,S=" + json.dumps(str(sid)) + ";"
        "try{var fe=window.frameElement;if(fe){fe.setAttribute('aria-hidden','true');"
        "fe.setAttribute('tabindex','-1');fe.setAttribute('title','Accessibility helper (no visible content)');}}catch(e){}"
        "function fix(){try{var u=new URL(P.location.href);if(u.searchParams.get('sid')!==S){"
        "u.searchParams.set('sid',S);P.history.replaceState(P.history.state,'',u.toString());}}catch(e){}}"
        "fix();setInterval(fix,800);})();</script>",
        height=0)
