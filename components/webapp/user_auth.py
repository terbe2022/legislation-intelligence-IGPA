'[Deployment-specific example removed from this source docstring.]'

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

import streamlit as st

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowed email domains
# ---------------------------------------------------------------------------
ALLOWED_DOMAINS = {"illinois.edu", "uillinois.edu", "uic.edu", "uis.edu"}


def canonical_email(email: str) -> str:
    """Collapse alias domains to one canonical identity.

    uillinois.edu and illinois.edu are Microsoft aliases delivering to the same
    inbox (Urbana-Champaign / the System), but the app keys identity on the
    literal string — so the same person signing in under each alias would get two
    separate accounts with separate saved items. Folding uillinois.edu ->
    illinois.edu here, at every entry point, makes both aliases resolve to one
    account. All saved items, profile, and team membership follow automatically
    because they key off this value.

    UIC (uic.edu) and UIS (uis.edu) are the other two University of Illinois System
    campuses. They run separate email systems and are NOT aliases of illinois.edu,
    so they are left untouched — each is its own distinct identity.
    """
    email = (email or "").strip().lower()
    if email.endswith("@uillinois.edu"):
        email = email[:-len("@uillinois.edu")] + "@illinois.edu"
    return email


def is_valid_university_email(email: str) -> bool:
    """Return True for any University of Illinois System address —
    @illinois.edu / @uillinois.edu (UIUC + System), @uic.edu (UIC),
    or @uis.edu (UIS)."""
    email = canonical_email(email)
    if "@" not in email:
        return False
    _, domain = email.rsplit("@", 1)
    return domain in ALLOWED_DOMAINS


# ---------------------------------------------------------------------------
# Cosmos DB client — lazy singleton
# ---------------------------------------------------------------------------
_cosmos_client = None
_db = None


def _get_db():
    """Return a lazy-initialised Cosmos database handle."""
    global _cosmos_client, _db
    if _db is not None:
        return _db
    try:
        from azure.cosmos import CosmosClient, PartitionKey, exceptions  # noqa: F401
        endpoint = os.environ["COSMOS_ENDPOINT"].strip().rstrip("/") + "/"
        key      = os.environ["COSMOS_KEY"].strip()
        db_name  = os.environ.get("COSMOS_DATABASE", "legplatform").strip()
        _cosmos_client = CosmosClient(url=endpoint, credential=key)
        _db = _cosmos_client.get_database_client(db_name)
        logger.info("Cosmos DB connected: %s", db_name)
        return _db
    except KeyError as e:
        logger.error("Missing env var for Cosmos: %s", e)
        return None
    except Exception as e:
        logger.error("Cosmos DB init failed: %s", e)
        return None


def _container(name: str):
    db = _get_db()
    if db is None:
        return None
    try:
        return db.get_container_client(name)
    except Exception as e:
        logger.error("Cannot get container %s: %s", name, e)
        return None


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------
_SECRET = None


def _get_secret() -> bytes:
    global _SECRET
    if _SECRET is None:
        raw = os.environ.get("MAGIC_LINK_SECRET", "")
        if not raw:
            # Fallback: derive from APP_PASSWORD if set (for dev only)
            raw = os.environ.get("APP_PASSWORD", __import__("os").environ.get('LEG_COMPONENTS_WEBAPP_USER_AUTH_PY_RAW_L132', ""))
            logger.warning("MAGIC_LINK_SECRET not set — using APP_PASSWORD fallback")
        _SECRET = raw.encode()
    return _SECRET


def _sign(token: str) -> str:
    """HMAC-SHA256 signature for a token string."""
    return hmac.new(_get_secret(), token.encode(), hashlib.sha256).hexdigest()


def generate_magic_link(email: str) -> Optional[str]:
    """
    Create a one-time magic-link token, persist it in Cosmos, return the full URL.
    Returns None if storage fails (caller should show error to user).
    """
    email = canonical_email(email)
    token = secrets.token_urlsafe(32)
    sig   = _sign(token)
    full  = f"{token}.{sig}"

    base_url = os.environ.get("MAGIC_LINK_BASE_URL", "").rstrip("/")
    link     = f"{base_url}/?magic={full}&email={email}"

    c = _container("magic_tokens")
    if c is None:
        logger.error("magic_tokens container unavailable")
        return None

    try:
        c.upsert_item({
            "id":         token,
            "email":      email,
            "sig":        sig,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "used":       False,
            # Cosmos TTL — container must have TTL enabled (set defaultTtl=-1)
            "ttl":        3600,   # 1 hour
        })
        logger.info("Magic token created for %s", email)
        return link
    except Exception as e:
        logger.error("Failed to store magic token: %s", e)
        return None


def verify_magic_link(token_full: str, email: str) -> tuple[bool, str]:
    """
    Validate a magic link token.
    Returns (success: bool, message: str).
    On success the token is marked used and a session_id is returned in message.
    """
    email = canonical_email(email)

    # --- structural check ---
    if "." not in token_full:
        return False, "Invalid link format."

    parts = token_full.rsplit(".", 1)
    if len(parts) != 2:
        return False, "Invalid link format."
    token, provided_sig = parts

    # --- HMAC check ---
    expected_sig = _sign(token)
    if not hmac.compare_digest(provided_sig, expected_sig):
        return False, "Link signature invalid. Please request a new link."

    # --- Cosmos lookup ---
    c = _container("magic_tokens")
    if c is None:
        return False, "Auth service unavailable. Please try again shortly."

    try:
        item = c.read_item(item=token, partition_key=email)
    except Exception:
        return False, "Link not found or expired. Please request a new one."

    if item.get("used"):
        return False, "This link has already been used. Please request a new one."

    if item.get("email") != email:
        return False, "Link does not match this email address."

    # --- Mark used ---
    try:
        item["used"] = True
        item["used_at"] = datetime.now(timezone.utc).isoformat()
        c.replace_item(item=token, body=item)
    except Exception as e:
        logger.warning("Could not mark token used: %s", e)

    # --- Ensure user record exists ---
    _ensure_user(email)

    # --- Create session ---
    session_id = _create_session(email)
    if session_id is None:
        return False, "Could not create session. Please try again."

    return True, session_id


# ---------------------------------------------------------------------------
# User records
# ---------------------------------------------------------------------------

def _ensure_user(email: str) -> dict:
    """Create user record on first login; return existing record otherwise."""
    c = _container("users")
    if c is None:
        return {}
    try:
        return c.read_item(item=email, partition_key=email)
    except Exception:
        pass  # Not found → create

    user = {
        "id":         email,
        "email":      email,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "campus":     None,
        "role":       None,
        "display_name": None,
        "digest_frequency": "weekly",   # "daily" | "weekly" | "none"
        "digest_enabled":   True,
    }
    try:
        c.create_item(user)
        logger.info("New user created: %s", email)
    except Exception as e:
        logger.error("Failed to create user %s: %s", email, e)
    return user


def get_user(email: str) -> Optional[dict]:
    """Load a user record by email. Returns None if not found."""
    c = _container("users")
    if c is None:
        return None
    try:
        return c.read_item(item=email, partition_key=email)
    except Exception:
        return None


def update_user(email: str, fields: dict) -> bool:
    """Merge `fields` into the user record. Resilient to the illinois/uillinois
    alias split: updates every twin record that exists (keeping them in sync), and
    if none exists, creates the canonical one — so the write ALWAYS persists to a
    record the app reads back. Returns True on success."""
    c = _container("users")
    if c is None:
        return False
    email = (email or "").strip().lower()
    canon = canonical_email(email)
    # Every alias this identity could be stored under.
    aliases = {email, canon}
    for a in list(aliases):
        if a.endswith("@illinois.edu"):
            aliases.add(a[:-len("@illinois.edu")] + "@uillinois.edu")
        elif a.endswith("@uillinois.edu"):
            aliases.add(a[:-len("@uillinois.edu")] + "@illinois.edu")
    wrote = False
    for key in aliases:
        try:
            item = c.read_item(item=key, partition_key=key)
        except Exception:
            continue
        try:
            item.update(fields)
            item["updated_at"] = datetime.now(timezone.utc).isoformat()
            c.replace_item(item=item.get("id", key), body=item)
            wrote = True
        except Exception as e:
            logger.error("update_user replace failed for %s: %s", key, e)
    if not wrote:
        # No record under any alias yet — create the canonical one so the value
        # still persists and is readable on the next load.
        try:
            item = {"id": canon, "email": canon,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat()}
            item.update(fields)
            c.upsert_item(body=item)
            wrote = True
        except Exception as e:
            logger.error("update_user create failed for %s: %s", canon, e)
    return wrote


def normalize_team(team: str) -> str:
    """Canonical team key: trimmed, collapsed whitespace, lowercased.

    Team names are a shared secret typed by different people on different days.
    Case- and space-sensitivity is the most common way that silently breaks
    ("IGPA-Fiscal" vs "igpa-fiscal" vs "IGPA Fiscal"). We store BOTH: 'team' keeps
    the display form the user typed, 'team_key' is what membership matches on.
    """
    return " ".join((team or "").split()).lower()[:80]


def set_team(email: str, team: str) -> bool:
    """Assign the user to a team. Stores display name + normalized match key."""
    disp = (team or "").strip()[:80]
    return update_user(email, {"team": disp, "team_key": normalize_team(team)})


def list_team_members(team: str, sharing_only: bool = True) -> list[dict]:
    """Return user records on a team. If sharing_only, only those who opted in.

    Membership + consent are separate: being on a team does not share your bills;
    the team_sharing toggle does. This enforces that at read time.
    """
    c = _container("users")
    if c is None or not team:
        return []
    try:
        q = "SELECT * FROM c WHERE c.team_key = @team"
        params = [{"name": "@team", "value": normalize_team(team)}]
        if sharing_only:
            q += " AND c.team_sharing = true"
        results = c.query_items(query=q, parameters=params,
                                enable_cross_partition_query=True)
        return list(results)
    except Exception as e:
        logger.error("list_team_members failed for %s: %s", team, e)
        return []



# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", 7))


def _create_session(email: str) -> Optional[str]:
    """Write a session record, return session_id."""
    c = _container("user_sessions")
    if c is None:
        return None

    user    = get_user(email)
    user_id = email  # email IS the user_id in this schema

    session_id = secrets.token_urlsafe(32)
    now        = datetime.now(timezone.utc)

    try:
        c.create_item({
            "id":         session_id,
            "user_id":    user_id,
            "email":      email,
            "created_at": now.isoformat(),
            "ttl":        TTL_DAYS * 86400,
        })
        return session_id
    except Exception as e:
        logger.error("Failed to create session: %s", e)
        return None


def get_session(session_id: str) -> Optional[dict]:
    """
    Load a session by session_id.
    Returns dict with at minimum: email, user_id.
    Returns None if expired or not found.
    """
    c = _container("user_sessions")
    if c is None:
        return None
    # We don't know user_id yet, so we query by id across partitions.
    # For scale this is fine — session lookups are infrequent.
    try:
        results = list(c.query_items(
            query="SELECT * FROM c WHERE c.id = @sid",
            parameters=[{"name": "@sid", "value": session_id}],
            enable_cross_partition_query=True,
        ))
        return results[0] if results else None
    except Exception as e:
        logger.error("get_session failed: %s", e)
        return None


def invalidate_session(session_id: str) -> None:
    """Delete a session (logout)."""
    c = _container("user_sessions")
    if c is None:
        return
    try:
        sess = get_session(session_id)
        if sess:
            c.delete_item(item=session_id, partition_key=sess["user_id"])
    except Exception as e:
        logger.warning("Could not invalidate session %s: %s", session_id, e)


# ---------------------------------------------------------------------------
# Streamlit session-state helpers
# ---------------------------------------------------------------------------

def require_user_auth() -> None:
    """
    Guard a page — redirects to login if not authenticated.
    Call at the top of every page file (after imports).
    """
    if not st.session_state.get("user_authed"):
        st.markdown("""
        <div style="max-width:460px;margin:6rem auto;padding:2rem;border-radius:14px;
             border:1px solid #D6DEE0;background:#fff;text-align:center;
             box-shadow:0 2px 12px rgba(0,0,0,0.08);">
          <div style="font-size:2rem;margin-bottom:.5rem">🔐</div>
          <div style="font-weight:700;font-size:1.15rem;margin-bottom:.5rem">
            Sign In Required
          </div>
          <div style="color:#5b6b71;font-size:.95rem">
            Please sign in from the <strong>Home</strong> page to continue.
          </div>
        </div>
        """, unsafe_allow_html=True)
        st.stop()


def current_user() -> Optional[dict]:
    """Return the current user dict from session state, or None."""
    return st.session_state.get("user_record")


def current_email() -> Optional[str]:
    """Return the authenticated user's email, or None."""
    u = current_user()
    return u["email"] if u else None
