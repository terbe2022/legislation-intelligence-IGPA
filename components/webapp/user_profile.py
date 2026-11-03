'[Deployment-specific example removed from this source docstring.]'

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy import — same pattern as user_auth.py
from user_auth import _container, get_user, update_user  # noqa: E402


# ---------------------------------------------------------------------------
# Saved items
# ---------------------------------------------------------------------------

CAMPUS_OPTIONS = [
    "UIC — University of Illinois Chicago",
    "UIUC — University of Illinois Urbana-Champaign",
    "UIS — University of Illinois Springfield",
    "System Administration",
    "Other",
]

ROLE_OPTIONS = [
    "Researcher / Faculty",
    "Policy Analyst",
    "Administrator",
    "Graduate Student",
    "Legislative Affairs",
    "Other",
]


# ---------------------------------------------------------------------------
# Saved items CRUD
# ---------------------------------------------------------------------------

def list_saved_items(user_id: str, item_type: Optional[str] = None,
                     raise_on_error: bool = False) -> list[dict]:
    """Return all saved items for a user, optionally filtered by type.

    raise_on_error: re-raise query failures instead of returning [] — lets a
    caller tell "nothing saved" apart from "the read failed" (Bug 27).
    """
    c = _container("saved_items")
    if c is None:
        return []
    try:
        if item_type:
            results = c.query_items(
                query="SELECT * FROM c WHERE c.user_id = @uid AND c.type = @t ORDER BY c.created_at DESC",
                parameters=[
                    {"name": "@uid", "value": user_id},
                    {"name": "@t",   "value": item_type},
                ],
                partition_key=user_id,
            )
        else:
            results = c.query_items(
                query="SELECT * FROM c WHERE c.user_id = @uid ORDER BY c.created_at DESC",
                parameters=[{"name": "@uid", "value": user_id}],
                partition_key=user_id,
            )
        return list(results)
    except Exception as e:
        logger.error("list_saved_items failed for %s: %s", user_id, e)
        if raise_on_error:
            raise
        return []


def save_bill(user_id: str, bill_id: str, bill_number: str,
              title: str, session_id: int, notify: bool = True) -> bool:
    """Save a bill to the user's watchlist."""
    # Prevent duplicates
    existing = list_saved_items(user_id, "bill")
    for item in existing:
        if item.get("data", {}).get("bill_id") == bill_id:
            return True  # Already saved

    c = _container("saved_items")
    if c is None:
        return False
    try:
        c.create_item({
            "id":         secrets.token_urlsafe(16),
            "user_id":    user_id,
            "type":       "bill",
            "label":      f"{bill_number} — {title[:80]}",
            "data": {
                "bill_id":     bill_id,
                "bill_number": bill_number,
                "title":       title,
                "session_id":  session_id,
            },
            "notify":     notify,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return True
    except Exception as e:
        logger.error("save_bill failed: %s", e)
        return False


def save_search(user_id: str, label: str, query: str,
                filters: dict, notify: bool = False) -> bool:
    """Save a search query + filter combo."""
    c = _container("saved_items")
    if c is None:
        return False
    try:
        c.create_item({
            "id":         secrets.token_urlsafe(16),
            "user_id":    user_id,
            "type":       "search",
            "label":      label or query[:60],
            "data": {
                "query":   query,
                "filters": filters,
            },
            "notify":     notify,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return True
    except Exception as e:
        logger.error("save_search failed: %s", e)
        return False


def save_category(user_id: str, category_type: str,
                  category_value: str, notify: bool = True) -> bool:
    """
    Save a policy domain or beneficiary category watch.
    category_type: "policy_domain" | "beneficiary"
    """
    existing = list_saved_items(user_id, "category")
    for item in existing:
        d = item.get("data", {})
        if d.get("type") == category_type and d.get("value") == category_value:
            return True  # Already watching

    c = _container("saved_items")
    if c is None:
        return False
    try:
        c.create_item({
            "id":         secrets.token_urlsafe(16),
            "user_id":    user_id,
            "type":       "category",
            "label":      f"{category_type.replace('_', ' ').title()}: {category_value}",
            "data": {
                "type":  category_type,
                "value": category_value,
            },
            "notify":     notify,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return True
    except Exception as e:
        logger.error("save_category failed: %s", e)
        return False


def delete_saved_item(user_id: str, item_id: str) -> bool:
    """Remove a saved item."""
    c = _container("saved_items")
    if c is None:
        return False
    try:
        c.delete_item(item=item_id, partition_key=user_id)
        return True
    except Exception as e:
        logger.error("delete_saved_item failed: %s", e)
        return False




def set_bill_note(user_id: str, bill_id: str, note: str) -> bool:
    """Attach/replace a personal note on a pinned bill (data['note']).

    Notes are the one piece of a watchlist that is genuinely human judgement —
    an analyst's read on why a bill matters — so they are the most valuable thing
    a team can share. Stored in the free-form data dict; no schema change.
    """
    c = _container("saved_items")
    if c is None:
        return False
    try:
        for item in list_saved_items(user_id, "bill"):
            if item.get("data", {}).get("bill_id") == bill_id:
                item.setdefault("data", {})["note"] = (note or "")[:4000]
                item["updated_at"] = datetime.now(timezone.utc).isoformat()
                c.replace_item(item=item["id"], body=item)
                return True
        return False
    except Exception as e:
        logger.error("set_bill_note failed for %s/%s: %s", user_id, bill_id, e)
        return False


def set_bill_annotation(user_id: str, bill_id: str, annotation: dict) -> bool:
    """Attach a structured annotation to a pinned bill (data['annotation']).

    Four fields: assessment (the human's read), direction (positive/negative/
    mixed/unclear — the one enum the digest aggregates on), affects (who/what
    unit), notify_who (routing — who should see this). Stamped with author +
    updated_at so that when the bill is shared or imported, the judgment stays
    attributed to the person who made it.
    """
    c = _container("saved_items")
    if c is None:
        return False
    try:
        for item in list_saved_items(user_id, "bill"):
            if item.get("data", {}).get("bill_id") == bill_id:
                _flagged = annotation.get("flagged", []) or []
                if not isinstance(_flagged, list):
                    _flagged = []
                ann = {
                    "assessment": str(annotation.get("assessment", ""))[:4000],
                    "direction":  str(annotation.get("direction", "unclear")),
                    "affects":    str(annotation.get("affects", ""))[:500],
                    "notify_who": str(annotation.get("notify_who", ""))[:500],
                    "flagged":    [str(e)[:200] for e in _flagged][:20],
                    "author":     user_id,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                item.setdefault("data", {})["annotation"] = ann
                item["updated_at"] = ann["updated_at"]
                c.replace_item(item=item["id"], body=item)
                return True
        return False
    except Exception as e:
        logger.error("set_bill_annotation failed for %s/%s: %s", user_id, bill_id, e)
        return False


def share_briefing(sharer: str, recipients: list[str], scope: str,
                   bill: dict, briefing: dict, via_team: str = "") -> int:
    """Share an AI briefing item with teammates. One doc per recipient, written
    to THAT recipient's partition so their Team tab reads it same-partition.

    A briefing item is AI-generated (owned by the model, curated for the sharer),
    distinct from a human annotation. scope is 'team' or 'individual' and drives
    the "why you're seeing this" label on the recipient's end. via_team is the
    name of the team the share was routed through (for "from <sharer> in <team>"
    attribution); empty means a direct person-to-person share. Returns the count
    written.
    """
    c = _container("saved_items")
    if c is None or not recipients:
        return 0
    import secrets as _sec
    written = 0
    for r in recipients:
        if not r or r == sharer:
            continue
        try:
            c.create_item({
                "id":       "shbrf_" + _sec.token_urlsafe(12),
                "user_id":  r,                       # recipient partition
                "type":     "shared_briefing",
                "label":    f"{bill.get('bill_number','')} — shared by {sharer.split('@')[0]}",
                "data": {
                    "bill_id":      str(bill.get("bill_id", "")),
                    "bill_number":  bill.get("bill_number", ""),
                    "title":        bill.get("title", ""),
                    "session_id":   int(bill.get("session_id", 0) or 0),
                    "shared_by":    sharer,
                    "share_scope":  scope,           # 'team' | 'individual'
                    "share_via_team": via_team or "", # team name, or '' if direct
                    "briefing":     briefing,        # the AI briefing content
                    "shared_at":    datetime.now(timezone.utc).isoformat(),
                },
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            written += 1
        except Exception as e:
            logger.error("share_briefing failed for %s: %s", r, e)
    return written


def list_shared_briefings(me: str) -> list[dict]:
    """Briefing items other teammates have shared with `me` (my partition)."""
    c = _container("saved_items")
    if c is None:
        return []
    try:
        return list(c.query_items(
            query="SELECT * FROM c WHERE c.user_id=@u AND c.type='shared_briefing'",
            parameters=[{"name": "@u", "value": me}],
            partition_key=me))
    except Exception as e:
        logger.error("list_shared_briefings failed for %s: %s", me, e)
        return []


def list_briefings_i_shared(me: str) -> list[dict]:
    """Shared-briefing items THIS user sent to teammates. They live in the
    recipients' partitions (data.shared_by == me), so this is a cross-partition
    query. Powers the 'Briefings I've shared with others' review section — each
    item carries the recipient in user_id (added as '_recipient')."""
    c = _container("saved_items")
    if c is None:
        return []
    try:
        results = c.query_items(
            query=("SELECT * FROM c WHERE c.type='shared_briefing' "
                   "AND c.data.shared_by=@u ORDER BY c.created_at DESC"),
            parameters=[{"name": "@u", "value": me}],
            enable_cross_partition_query=True)
        out = []
        for it in results:
            it["_recipient"] = it.get("user_id", "")
            out.append(it)
        return out
    except Exception as e:
        logger.error("list_briefings_i_shared failed for %s: %s", me, e)
        return []


def list_flagged_for(me: str, team_emails: list[str]) -> list[dict]:
    """Return teammates' annotated bills where `me` is in the annotation's flagged list.

    Powers the "flagged for you" surfacing: a directed hand-off from a colleague.
    Each returned item carries the bill data + the annotation (which names the author).
    """
    out = []
    for it in list_team_shared(team_emails, "bill"):
        ann = it.get("data", {}).get("annotation", {}) or {}
        if me in (ann.get("flagged", []) or []):
            out.append(it)
    return out


def list_team_shared(emails: list[str], item_type: str = "bill") -> list[dict]:
    """Read saved_items of a given type across a set of teammates.

    saved_items is partitioned by user_id, so a per-partition IN query would need
    one round trip per teammate. Cross-partition is simpler and fine at team scale
    (tens of users). Returns each item with an added '_owner' field so the UI can
    attribute it. The caller is responsible for confirming each email actually
    consented to share (team_sharing flag) before displaying.
    """
    c = _container("saved_items")
    if c is None or not emails:
        return []
    try:
        # Build an IN clause with named params.
        names = [f"@u{i}" for i in range(len(emails))]
        params = [{"name": n, "value": e} for n, e in zip(names, emails)]
        params.append({"name": "@t", "value": item_type})
        q = (f"SELECT * FROM c WHERE c.type = @t "
             f"AND c.user_id IN ({', '.join(names)}) ORDER BY c.created_at DESC")
        results = c.query_items(query=q, parameters=params,
                                enable_cross_partition_query=True)
        out = []
        for it in results:
            it["_owner"] = it.get("user_id", "")
            out.append(it)
        return out
    except Exception as e:
        logger.error("list_team_shared failed: %s", e)
        return []

def toggle_notify(user_id: str, item_id: str, notify: bool) -> bool:
    """Toggle digest notification for a saved item."""
    c = _container("saved_items")
    if c is None:
        return False
    try:
        items = list(c.query_items(
            query="SELECT * FROM c WHERE c.id = @id AND c.user_id = @uid",
            parameters=[
                {"name": "@id",  "value": item_id},
                {"name": "@uid", "value": user_id},
            ],
            partition_key=user_id,
        ))
        if not items:
            return False
        item = items[0]
        item["notify"] = notify
        c.replace_item(item=item_id, body=item)
        return True
    except Exception as e:
        logger.error("toggle_notify failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Profile helpers
# ---------------------------------------------------------------------------

def profile_complete(user: dict) -> bool:
    """Return True if the user has filled in campus + role."""
    return bool(user.get("campus") and user.get("role"))


def get_digest_items(user_id: str) -> dict[str, list]:
    """
    Return saved items that have notify=True, grouped by type.
    Used by the digest pipeline.
    """
    all_items = list_saved_items(user_id)
    result: dict[str, list] = {"bill": [], "search": [], "category": []}
    for item in all_items:
        if item.get("notify"):
            t = item.get("type", "")
            if t in result:
                result[t].append(item)
    return result
