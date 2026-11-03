"""
team_service.py — Team sharing foundation for the Legislative Intelligence Platform.

Data model (Cosmos container `teams`, partition key /pk):
  Team document:
    { "id": <team_id>, "pk": <team_id>, "doc": "team",
      "name": str, "owner_email": str, "created_at": iso }

  Membership document:
    { "id": "<team_id>::<user_email>", "pk": <team_id>, "doc": "member",
      "team_id": <team_id>, "user_email": str,
      "status": "invited" | "active",
      "role": "owner" | "member",
      "invited_by": str, "invited_at": iso, "joined_at": iso|None }

Design decisions (locked with product owner):
  - Adding a member requires BOTH sides: owner invites by email, invitee accepts.
  - Membership is mutual: active members see each other's shared items.
  - Sharing defaults ON when a member becomes active (a per-user profile flag
    `team_share_enabled` governs whether that member's items appear to the team;
    absence of the flag is treated as True / sharing-on).
  - Multi-team is supported by the data model; UI may start single-team.

All functions are defensive: they return simple values and never raise into the
UI layer, matching the rest of the codebase.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone

# Cap on how many teams a single user can actively belong to at once (owned or
# joined). Pending invites don't count — only accepted memberships. Env-tunable
# so we can raise it later without a code change.
MAX_TEAMS = int(os.environ.get("MAX_TEAMS_PER_USER", "3"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canon(email: str) -> str:
    """Canonical identity for an email — folds @uillinois.edu -> @illinois.edu
    (plus strip/lower), matching how the rest of the app keys every user. Every
    email that enters this module goes through here so team membership lands on
    the SAME identity as a person's saved bills, prompts, and investigations.
    Falls back to a plain strip/lower if user_auth isn't importable.
    """
    try:
        from user_auth import canonical_email
        return canonical_email(email)
    except Exception:
        return (email or "").strip().lower()


def _teams_container():
    """Get (create-if-missing) the `teams` container, partitioned by /pk."""
    try:
        from user_auth import _get_db
        from azure.cosmos import PartitionKey
        db = _get_db()
        if db is None:
            return None
        return db.create_container_if_not_exists(
            id="teams", partition_key=PartitionKey(path="/pk"))
    except Exception:
        try:
            from user_auth import _container
            return _container("teams")
        except Exception:
            return None


# ── Team lifecycle ────────────────────────────────────────────────────────────

def create_team(name: str, owner_email: str) -> str | None:
    """Create a team owned by owner_email; owner is added as an active member."""
    name = (name or "").strip()
    owner_email = _canon(owner_email)
    if not name or not owner_email:
        return None
    # Cap check: creating a team adds an active owner-membership, so refuse if
    # the user is already at the limit. UI gates this too; this is defense-in-depth.
    if count_active_teams(owner_email) >= MAX_TEAMS:
        return None
    c = _teams_container()
    if c is None:
        return None
    team_id = "team_" + secrets.token_urlsafe(9)
    try:
        c.upsert_item({
            "id": team_id, "pk": team_id, "doc": "team",
            "name": name, "owner_email": owner_email, "created_at": _now(),
        })
        c.upsert_item({
            "id": f"{team_id}::{owner_email}", "pk": team_id, "doc": "member",
            "team_id": team_id, "user_email": owner_email,
            "status": "active", "role": "owner",
            "invited_by": owner_email, "invited_at": _now(), "joined_at": _now(),
        })
        return team_id
    except Exception:
        return None


def get_team(team_id: str) -> dict | None:
    c = _teams_container()
    if c is None:
        return None
    try:
        return c.read_item(item=team_id, partition_key=team_id)
    except Exception:
        return None


def rename_team(team_id: str, new_name: str, actor_email: str) -> bool:
    """Owner-only rename."""
    new_name = (new_name or "").strip()
    if not new_name:
        return False
    t = get_team(team_id)
    if not t or t.get("owner_email") != _canon(actor_email):
        return False
    c = _teams_container()
    try:
        t["name"] = new_name
        c.upsert_item(t)
        return True
    except Exception:
        return False


# ── Membership ────────────────────────────────────────────────────────────────

def invite_member(team_id: str, invitee_email: str, actor_email: str) -> str:
    """
    Owner invites someone by email. Returns a status string for the UI:
    'invited' | 'already_member' | 'already_invited' | 'not_owner' |
    'bad_domain' | 'error'.
    """
    # Keep the address the owner actually typed for the *email* — it's sourced
    # from the directory and guaranteed to deliver. But key IDENTITY on the
    # canonical form, so membership matches however the invitee signs in. This
    # matters because some accounts have only one alias (e.g. @uillinois.edu with
    # no @illinois.edu mailbox), so we must not email the canonical form blindly.
    raw_invitee = (invitee_email or "").strip().lower()
    invitee_email = _canon(invitee_email)
    actor_email = _canon(actor_email)
    if not invitee_email or "@" not in invitee_email:
        return "error"
    # Only University of Illinois addresses can be invited — same allow-list the
    # login gate enforces (@illinois.edu / @uillinois.edu). This keeps teams to
    # real, sign-in-able accounts; the invitee still has to sign in and accept.
    try:
        from user_auth import is_valid_university_email
        if not is_valid_university_email(invitee_email):
            return "bad_domain"
    except Exception:
        pass  # if the validator is unavailable, fall through (don't hard-fail)
    t = get_team(team_id)
    if not t or t.get("owner_email") != actor_email:
        return "not_owner"
    c = _teams_container()
    if c is None:
        return "error"
    mid = f"{team_id}::{invitee_email}"
    try:
        try:
            existing = c.read_item(item=mid, partition_key=team_id)
            if existing.get("status") == "active":
                return "already_member"
            return "already_invited"
        except Exception:
            pass  # no existing membership — proceed
        c.upsert_item({
            "id": mid, "pk": team_id, "doc": "member",
            "team_id": team_id, "user_email": invitee_email,
            "status": "invited", "role": "member",
            "invited_by": actor_email, "invited_at": _now(), "joined_at": None,
        })
        # Best-effort invite email. Never let a send failure undo the invite —
        # the pending invite is already recorded and will show in their Profile.
        try:
            from email_service import send_team_invite
            try:
                from user_auth import get_user
                _inviter = ((get_user(actor_email) or {}).get("display_name")
                            or actor_email.split("@")[0])
            except Exception:
                _inviter = actor_email.split("@")[0]
            send_team_invite(raw_invitee or invitee_email, t.get("name", ""), _inviter)
        except Exception:
            pass
        return "invited"
    except Exception:
        return "error"


def accept_invite(team_id: str, user_email: str) -> bool:
    """Invitee accepts a pending invite → becomes active."""
    user_email = _canon(user_email)
    c = _teams_container()
    if c is None:
        return False
    mid = f"{team_id}::{user_email}"
    try:
        m = c.read_item(item=mid, partition_key=team_id)
        if m.get("status") != "invited":
            return False
        # Cap check: accepting turns this into an active membership. Refuse if the
        # user is already at the limit (they must leave a team first). The UI
        # disables Accept at the cap; this is the defense-in-depth backstop.
        if count_active_teams(user_email) >= MAX_TEAMS:
            return False
        m["status"] = "active"
        m["joined_at"] = _now()
        c.upsert_item(m)
        return True
    except Exception:
        return False


def decline_invite(team_id: str, user_email: str) -> bool:
    """Invitee declines → membership record removed."""
    return leave_team(team_id, user_email, allow_owner=False)


def leave_team(team_id: str, user_email: str, allow_owner: bool = False) -> bool:
    """Remove a membership. Owner cannot leave their own team unless allow_owner.
    Tries the canonical id first, then the raw-email id, so memberships written
    before email-canonicalization (stored under @uillinois.edu) can still be
    removed."""
    raw   = (user_email or "").strip().lower()
    canon = _canon(user_email)
    c = _teams_container()
    if c is None:
        return False
    t = get_team(team_id)
    if t and t.get("owner_email") == canon and not allow_owner:
        return False
    for _eid in dict.fromkeys([canon, raw]):
        if not _eid:
            continue
        try:
            c.delete_item(item=f"{team_id}::{_eid}", partition_key=team_id)
            return True
        except Exception:
            continue
    return False


def remove_member(team_id: str, target_email: str, actor_email: str) -> bool:
    """Owner removes a member (not themselves). Passes the raw target to
    leave_team so orphaned non-canonical invites can also be cleared."""
    actor_email = _canon(actor_email)
    t = get_team(team_id)
    if not t or t.get("owner_email") != actor_email:
        return False
    if _canon(target_email) == actor_email:
        return False  # owner can't remove self here
    return leave_team(team_id, target_email, allow_owner=False)


def delete_team(team_id: str, actor_email: str) -> bool:
    """Owner disbands a team: delete every membership doc (by its exact stored
    id, so orphaned non-canonical rows go too) and then the team doc itself.
    Owner-only. Returns True if the team doc was removed."""
    actor_email = _canon(actor_email)
    t = get_team(team_id)
    if not t or t.get("owner_email") != actor_email:
        return False
    c = _teams_container()
    if c is None:
        return False
    try:
        for _m in list_members(team_id):
            _mid = _m.get("id")
            if _mid:
                try:
                    c.delete_item(item=_mid, partition_key=team_id)
                except Exception:
                    pass
        c.delete_item(item=team_id, partition_key=team_id)
        return True
    except Exception:
        return False


# ── Queries ───────────────────────────────────────────────────────────────────

def list_members(team_id: str, status: str | None = None) -> list[dict]:
    """All membership docs for a team, optionally filtered by status."""
    c = _teams_container()
    if c is None:
        return []
    try:
        if status:
            q = ("SELECT * FROM c WHERE c.pk=@pk AND c.doc='member' "
                 "AND c.status=@st")
            params = [{"name": "@pk", "value": team_id},
                      {"name": "@st", "value": status}]
        else:
            q = "SELECT * FROM c WHERE c.pk=@pk AND c.doc='member'"
            params = [{"name": "@pk", "value": team_id}]
        return list(c.query_items(query=q, parameters=params,
                                  partition_key=team_id))
    except Exception:
        return []


def list_teams_for_user(user_email: str) -> list[dict]:
    """
    All teams where the user has a membership. Returns list of dicts:
      { team_id, name, owner_email, role, status }
    Cross-partition query (membership id encodes email; we match on user_email).
    """
    user_email = _canon(user_email)
    c = _teams_container()
    if c is None or not user_email:
        return []
    out = []
    try:
        memberships = list(c.query_items(
            query=("SELECT * FROM c WHERE c.doc='member' AND c.user_email=@e"),
            parameters=[{"name": "@e", "value": user_email}],
            enable_cross_partition_query=True,
        ))
        for m in memberships:
            t = get_team(m.get("team_id", ""))
            if not t:
                continue
            out.append({
                "team_id": t["id"], "name": t.get("name", ""),
                "owner_email": t.get("owner_email", ""),
                "role": m.get("role", "member"),
                "status": m.get("status", "invited"),
            })
    except Exception:
        return out
    return out


def list_pending_invites(user_email: str) -> list[dict]:
    """Teams the user has been invited to but hasn't accepted yet."""
    return [t for t in list_teams_for_user(user_email)
            if t.get("status") == "invited"]


def count_active_teams(user_email: str) -> int:
    """How many teams the user is an ACTIVE member of (owned or joined).

    Used to enforce MAX_TEAMS. Pending invites are not counted. Uses a COUNT
    aggregate so it stays cheap regardless of how many teams exist.
    """
    user_email = _canon(user_email)
    c = _teams_container()
    if c is None or not user_email:
        return 0
    try:
        rows = list(c.query_items(
            query=("SELECT VALUE COUNT(1) FROM c WHERE c.doc='member' "
                   "AND c.user_email=@e AND c.status='active'"),
            parameters=[{"name": "@e", "value": user_email}],
            enable_cross_partition_query=True,
        ))
        return int(rows[0]) if rows else 0
    except Exception:
        return 0


def active_teammates(user_email: str, team_id: str) -> list[str]:
    """
    Emails of ACTIVE members of a team the user actively belongs to,
    EXCLUDING the user themselves. Returns [] if the user isn't an active
    member (so callers can't leak data to non-members).
    """
    user_email = _canon(user_email)
    members = list_members(team_id, status="active")
    emails = {m.get("user_email", "") for m in members}
    if user_email not in emails:
        return []
    return sorted(e for e in emails if e and e != user_email)


# ── Sharing preference (profile flag) ─────────────────────────────────────────

def get_share_enabled(user: dict) -> bool:
    """Whether this user's items are shared with their team(s). Defaults True."""
    if not user:
        return True
    return bool(user.get("team_share_enabled", True))


def set_share_enabled(user_email: str, enabled: bool) -> bool:
    """Persist the user's team-sharing preference on their profile."""
    try:
        from user_auth import update_user
        return bool(update_user(_canon(user_email),
                                {"team_share_enabled": bool(enabled)}))
    except Exception:
        return False
