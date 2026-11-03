'[Deployment-specific example removed from this source docstring.]'
from __future__ import annotations

import io
import json
import logging
import os
import re
from datetime import datetime, timezone, date
from typing import Optional

import azure.functions as func  # type: ignore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CURRENT_SESSION       = 2176   # 104th GA — keep in sync with My Tracker
MAX_PINNED_IN_DIGEST  = 10
MAX_PER_CATEGORY      = 3
MAX_MOVED             = 8      # biggest movers included in the brief
MAX_CAT_IN_UNIVERSE   = 5      # top bills per category counted as "relevant"
MAX_TRACKED_CHANGED   = 10     # tracked-bill changes reported per brief
MAX_NEWLY_FLAGGED     = 6
MAX_IMMINENT          = 3
PROB_DELTA_PTS        = 5      # minimum ±pts to count as "moved"
SESSION_AVG_PROB      = 20     # session avg P(reaches vote), for context
OPENAI_MAX_TOKENS     = 1600

IMPACT_RESULTS_PATH = os.environ.get(
    "IMPACT_RESULTS_PATH",
    "legislation/impact_results/impact_results.parquet")
STATE_PREFIX = os.environ.get(
    "DIGEST_STATE_PREFIX", "legislation/digest_state/")

APP_BASE_URL = os.environ.get(
    "APP_BASE_URL",
    __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DIGEST_TIMER_INIT_PY_APP_BASE_URL_L82', ""),
).rstrip("/")


# ---------------------------------------------------------------------------
# DIAGNOSTIC TRAIL → Cosmos digest_diag. v2: OFF unless DIGEST_DIAG=1.
# ---------------------------------------------------------------------------
import uuid as _uuid


def _diag(step: str, detail: str = "", level: str = "info") -> None:
    """Write one diagnostic record to Cosmos. Never raises. Gated by DIGEST_DIAG."""
    if os.environ.get("DIGEST_DIAG", "").strip().lower() not in ("1", "true", "yes"):
        return
    try:
        from azure.cosmos import CosmosClient, PartitionKey
        endpoint = os.environ["COSMOS_ENDPOINT"].strip().rstrip("/") + "/"
        key      = os.environ["COSMOS_KEY"].strip()
        db_name  = os.environ.get("COSMOS_DATABASE", "legplatform").strip()
        client   = CosmosClient(url=endpoint, credential=key)
        db       = client.get_database_client(db_name)
        cont     = db.create_container_if_not_exists(
            id="digest_diag", partition_key=PartitionKey(path="/run_id"))
        cont.upsert_item({
            "id":       str(_uuid.uuid4()),
            "run_id":   os.environ.get("_DIAG_RUN_ID", "unknown"),
            "ts":       datetime.now(timezone.utc).isoformat(),
            "step":     step,
            "detail":   str(detail)[:1000],
            "level":    level,
        })
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Self-contained Cosmos access (user_auth/user_profile import streamlit,
# which is not installed here — this is why v0 silently sent nothing).
# ---------------------------------------------------------------------------
_DIGEST_DB = None


def _cross_partition_query(container, query: str, params: list | None = None) -> list:
    """Cross-partition query robust across azure-cosmos SDK versions."""
    kwargs = {"query": query}
    if params:
        kwargs["parameters"] = params
    try:
        return list(container.query_items(**kwargs))
    except Exception:
        return list(container.query_items(**kwargs, enable_cross_partition_query=True))


def _get_container_local(name: str):
    global _DIGEST_DB
    try:
        if _DIGEST_DB is None:
            from azure.cosmos import CosmosClient
            endpoint = os.environ["COSMOS_ENDPOINT"].strip().rstrip("/") + "/"
            key      = os.environ["COSMOS_KEY"].strip()
            db_name  = os.environ.get("COSMOS_DATABASE", "legplatform").strip()
            _DIGEST_DB = CosmosClient(url=endpoint, credential=key)\
                .get_database_client(db_name)
        return _DIGEST_DB.get_container_client(name)
    except Exception as e:
        _diag("get_container_error", f"{name}: {repr(e)}", "error")
        return None


def _get_digest_items_local(user_id: str) -> dict:
    """User's notify=True saved items grouped by type (self-contained)."""
    out = {"bill": [], "search": [], "category": []}
    c = _get_container_local("saved_items")
    if c is None:
        return out
    try:
        items = _cross_partition_query(
            c, "SELECT * FROM c WHERE c.user_id=@u",
            [{"name": "@u", "value": user_id}])
        for it in items:
            if it.get("notify"):
                t = it.get("type", "")
                if t in out:
                    out[t].append(it)
    except Exception as e:
        _diag("digest_items_query_error", f"{user_id}: {repr(e)}", "error")
    return out


# ---------------------------------------------------------------------------
# Blob access (<deployment-location>) — impact results + per-user delta state
# ---------------------------------------------------------------------------
_BLOB_SVC = None


def _gold_blob(path: str):
    """Return a BlobClient for a path in the gold container, or None."""
    global _BLOB_SVC
    try:
        if _BLOB_SVC is None:
            from azure.storage.blob import BlobServiceClient
            conn = os.environ["LEGSTORAGE_CONNECTION_STRING"].strip()
            _BLOB_SVC = BlobServiceClient.from_connection_string(conn)
        container = os.environ.get("GOLD_CONTAINER", "gold").strip()
        return _BLOB_SVC.get_container_client(container).get_blob_client(path)
    except Exception as e:
        _diag("gold_blob_error", f"{path}: {repr(e)}", "error")
        logger.warning("Gold blob client unavailable (%s): %s", path, e)
        return None


def _load_impact_results():
    """The nightly scorer's full results parquet. None on any failure —
    the digest degrades gracefully to a no-impact-data brief."""
    import pandas as pd
    client = _gold_blob(IMPACT_RESULTS_PATH)
    if client is None:
        return None
    try:
        buf = io.BytesIO()
        client.download_blob().readinto(buf)
        buf.seek(0)
        df = pd.read_parquet(buf)
        df["bill_id"] = df["bill_id"].astype(str).str.strip()
        logger.info("Impact results loaded: %d rows", len(df))
        return df
    except Exception as e:
        _diag("impact_results_load_error", repr(e), "error")
        logger.warning("Impact results unavailable: %s", e)
        return None


def _load_trail():
    """Per-bill action history (active-bills trail). Returns a dict
    {bill_id: [ {action_index, action_date, action_text, y_prob}, ... ]}
    sorted by action_index, or {} on failure."""
    import pandas as pd
    try:
        from data_catalog import SNAPSHOT_TRAIL_URL
        df = pd.read_parquet(SNAPSHOT_TRAIL_URL)
        df["bill_id"] = df["bill_id"].astype(str).str.strip()
        out: dict = {}
        for r in df.sort_values(["bill_id", "action_index"]).itertuples(index=False):
            out.setdefault(str(r.bill_id), []).append({
                "idx":  int(getattr(r, "action_index", 0) or 0),
                "date": str(getattr(r, "action_date", "") or "")[:10],
                "text": str(getattr(r, "action_text", "") or ""),
                "prob": _num(getattr(r, "y_prob", None)),
            })
        logger.info("Trail loaded: %d bills", len(out))
        return out
    except Exception as e:
        logger.warning("Trail unavailable: %s", e)
        return {}


def _load_diffs():
    """Per-bill text-change records. Empty during recess; self-populates when
    bills are amended (build_diffs.py Step 4 of the nightly pipeline).
    Returns {bill_id: {fields_changed, n_lines_added, n_lines_removed,
    changed_at, version_to}} keyed to the most recent change."""
    import pandas as pd
    try:
        from data_catalog import DIFF_URLS
        url = DIFF_URLS.get(CURRENT_SESSION)
        if not url:
            return {}
        df = pd.read_parquet(url)
        if df.empty:
            return {}
        df["bill_id"] = df["bill_id"].astype(str).str.strip()
        out = {}
        for r in df.sort_values("changed_at").itertuples(index=False):
            out[str(r.bill_id)] = {
                "fields":  str(getattr(r, "fields_changed", "") or ""),
                "added":   int(getattr(r, "n_lines_added", 0) or 0),
                "removed": int(getattr(r, "n_lines_removed", 0) or 0),
                "changed_at": str(getattr(r, "changed_at", "") or "")[:10],
                "version_to": str(getattr(r, "version_to", "") or ""),
            }
        logger.info("Bill diffs loaded: %d bills", len(out))
        return out
    except Exception as e:
        logger.warning("Bill diffs unavailable: %s", e)
        return {}


def _summarize_actions(actions: list, since_idx: int) -> dict:
    """Split a bill's action history into recent (since last digest) and the
    overall arc. `since_idx` comes from the delta state; on first run it is 0,
    so nothing is 'recent' and the full arc is summarized."""
    if not actions:
        return {"recent": [], "arc": "", "n_total": 0}
    recent = [a for a in actions if a["idx"] > since_idx] if since_idx else []
    first, last = actions[0], actions[-1]
    arc = ("introduced " + first["date"] + ", " + str(len(actions)) +
           " actions to date, latest " + repr(last["text"]) +
           " (" + last["date"] + ")")
    recent_lines = []
    for a in recent[-6:]:
        prev = next((x for x in actions if x["idx"] == a["idx"] - 1), None)
        move = ""
        if prev and prev["prob"] is not None and a["prob"] is not None:
            dp = round((a["prob"] - prev["prob"]) * 100)
            if abs(dp) >= 3:
                sign = "+" if dp > 0 else ""
                move = " (likelihood " + sign + str(dp) + " pts)"
        recent_lines.append(a["date"] + ": " + repr(a["text"]) + move)
    return {"recent": recent_lines, "arc": arc, "n_total": len(actions)}


def _state_path(email: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", email.strip().lower())
    return f"{STATE_PREFIX}{safe}.json"


def _load_state(email: str) -> dict:
    """The user's last-digest snapshot. {} = first digest (baseline)."""
    client = _gold_blob(_state_path(email))
    if client is None:
        return {}
    try:
        data = client.download_blob().readall()
        return json.loads(data.decode("utf-8"))
    except Exception:
        return {}      # not found / unreadable → treat as first digest


def _save_state(email: str, state: dict) -> None:
    """Persist this digest's snapshot for next time. Never raises."""
    if os.environ.get("DIGEST_STATE_FREEZE", "").strip().lower() in ("1", "true", "yes"):
        logger.info("[STATE FREEZE] Not updating delta state for %s", email)
        return
    client = _gold_blob(_state_path(email))
    if client is None:
        return
    try:
        from azure.storage.blob import ContentSettings
        client.upload_blob(
            json.dumps(state, ensure_ascii=False).encode("utf-8"),
            overwrite=True,
            content_settings=ContentSettings(content_type="application/json"))
    except Exception as e:
        _diag("state_save_error", f"{email}: {repr(e)}", "error")
        logger.warning("Could not save digest state for %s: %s", email, e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(mytimer: func.TimerRequest) -> None:
    utc_now = datetime.now(timezone.utc)
    os.environ["_DIAG_RUN_ID"] = utc_now.strftime("%Y%m%dT%H%M%S")
    _diag("run_started", f"utc={utc_now.isoformat()} past_due={mytimer.past_due}")
    logger.info("Digest v2 timer fired at %s (past due: %s)",
                utc_now.isoformat(), mytimer.past_due)

    try:
        master = _load_master(CURRENT_SESSION)
    except Exception as exc:
        _diag("master_load_exception", repr(exc), "error")
        logger.error("master load raised: %s", exc, exc_info=True)
        return
    if master is None or master.empty:
        _diag("master_unusable", "None or empty", "error")
        logger.error("Gold data unusable for session %d — aborting run", CURRENT_SESSION)
        return
    _diag("master_loaded", f"rows={len(master)}")

    impact = _load_impact_results()   # may be None — brief degrades gracefully
    trail  = _load_trail()            # per-bill action history (SNAPSHOT_TRAIL)
    diffs  = _load_diffs()            # per-bill text changes (usually empty in recess)

    users = _list_all_users()
    _diag("users_listed", f"count={len(users)}")
    logger.info("Processing %d users", len(users))

    sent = skipped = failed = 0
    for user in users:
        try:
            result = _process_user(user, master, impact, trail, diffs, utc_now)
            _diag("user_result", f"{user.get('email','?')} -> {result}")
            if result == "sent":
                sent += 1
            elif result == "skipped":
                skipped += 1
            else:
                failed += 1
        except Exception as exc:
            _diag("user_exception", f"{user.get('email','?')}: {repr(exc)}", "error")
            logger.error("Unhandled error for user %s: %s",
                         user.get("email", "?"), exc, exc_info=True)
            failed += 1

    _diag("run_complete", f"sent={sent} skipped={skipped} failed={failed}")
    logger.info("Run complete — sent:%d  skipped:%d  failed:%d", sent, skipped, failed)


# ---------------------------------------------------------------------------
# Per-user flow
# ---------------------------------------------------------------------------
def _process_user(user: dict, master, impact, trail, diffs, utc_now: datetime) -> str:
    """Returns "sent" | "skipped" | "error"."""
    import pandas as pd
    today = utc_now.date()
    email = user.get("email", "")
    if not email:
        return "skipped"
    if not user.get("digest_enabled", False):
        _diag("skip_not_enabled", email)
        return "skipped"
    if not _should_send_today(user, today):
        _diag("skip_not_send_day", f"{email} freq={user.get('digest_frequency')}")
        return "skipped"

    try:
        digest_items = _get_digest_items_local(email)
    except Exception as e:
        _diag("digest_items_error", f"{email}: {repr(e)}", "error")
        return "error"

    pinned_bills = digest_items.get("bill", [])
    categories   = digest_items.get("category", [])

    # This user's impact ratings (from the nightly scorer)
    user_impact = None
    if impact is not None and not impact.empty:
        user_impact = impact[impact["user_id"] == email].copy()
        if user_impact.empty:
            user_impact = None

    if not pinned_bills and not categories and user_impact is None:
        _diag("skip_no_items", email)
        return "skipped"

    prev_state = _load_state(email)

    sections_pref = user.get("digest_sections") or ["tracked", "flagged",
                                                     "imminent", "trends"]
    brief = _assemble_brief(master, pinned_bills, categories,
                            user_impact, prev_state, sections_pref,
                            trail, diffs)
    if brief is None:
        _diag("skip_empty_brief", email)
        return "skipped"

    prompt = _build_prompt_v2(brief, prev_state, today,
                              user.get("digest_frequency", "weekly"),
                              sections_pref)
    _diag("prompt_built", f"{email} chars={len(prompt)}")

    try:
        digest_text = _generate_digest(prompt)
    except Exception as e:
        _diag("openai_exception", f"{email}: {repr(e)}", "error")
        return "error"
    if not digest_text:
        _diag("openai_empty", email, "error")
        return "error"

    html    = _build_email_html(digest_text, email, today)
    subject = _build_subject(today, brief)

    test_recipient = os.environ.get("DIGEST_TEST_RECIPIENT", "").strip()
    dry_run = os.environ.get("DIGEST_DRY_RUN", "").strip().lower() in ("1", "true", "yes")

    if dry_run:
        logger.info("[DRY RUN] Would send to %s (subject: %s, %d chars)",
                    email, subject, len(html))
        return "sent"   # NOTE: dry-run does not update state

    actual_recipient = email
    if test_recipient:
        actual_recipient = test_recipient
        subject = f"[TEST → {email}] {subject}"
        logger.info("[TEST MODE] Redirecting %s's digest to %s", email, test_recipient)

    try:
        from email_service import send_digest
        ok = send_digest(actual_recipient, subject, html)
    except Exception as e:
        _diag("send_exception", f"{actual_recipient}: {repr(e)}", "error")
        return "error"

    if ok:
        _save_state(email, brief["snapshot"])
        logger.info("Digest sent to %s", actual_recipient)
        return "sent"
    logger.warning("send_digest returned False for %s", actual_recipient)
    return "error"


# ---------------------------------------------------------------------------
# Schedule logic (unchanged from v1)
# ---------------------------------------------------------------------------
_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
                  "Friday", "Saturday", "Sunday"]


def _should_send_today(user: dict, today: date) -> bool:
    alert_mode = user.get("alert_mode", "scheduled")
    if alert_mode == "realtime":
        return False
    freq = user.get("digest_frequency", "weekly")
    today_name = _WEEKDAY_NAMES[today.weekday()]
    if freq == "daily":
        return True
    if freq == "weekly":
        return today_name == "Monday"
    if freq == "custom":
        days = user.get("digest_days") or ["Monday"]
        return today_name in days
    return today_name == "Monday"


# ---------------------------------------------------------------------------
# Brief assembly — pure data. The LLM formats; it never discovers or invents.
# ---------------------------------------------------------------------------
def _resolve_col(df, col: str) -> str:
    FIELD_FALLBACK = {
        "legislative_goal_raw":       "legislative_goal_standardized",
        "key_provisions_raw":         "key_provisions_standardized",
        "intent_raw":                 "intent_standardized",
        "motivation_raw":             "motivation_standardized",
        "intended_beneficiaries_raw": "intended_beneficiaries_standardized",
        "increasing_aspects_raw":     "increasing_aspects_standardized",
        "decreasing_aspects_raw":     "decreasing_aspects_standardized",
    }
    if col in df.columns:
        return col
    fb = FIELD_FALLBACK.get(col)
    return fb if (fb and fb in df.columns) else col


def _category_bill_ids(master, categories: list) -> tuple:
    """Returns (universe_hits, cat_stats).

    universe_hits: {bill_id: [labels]} — capped to the TOP MAX_CAT_IN_UNIVERSE
    bills per category by floor-vote likelihood, so broad keyword monitors
    don't flood the briefing universe (or the delta state) with thousands of
    grazers. Full match counts are preserved in cat_stats for the trends
    section: {label: {"total": n, "gaining": g}}.
    """
    import pandas as pd
    hits: dict[str, list] = {}
    cat_stats: dict[str, dict] = {}
    for cat_item in categories:
        data     = cat_item.get("data", {})
        cat_type = data.get("type", "")
        keywords = data.get("keywords", [])
        fields   = data.get("fields", [])
        cat_val  = data.get("value", "")
        label    = cat_item.get("label", cat_val)

        mask = pd.Series(False, index=master.index)
        if cat_type == "keyword" and keywords and fields:
            for kw in keywords:
                for f in fields:
                    fc = _resolve_col(master, f)
                    if fc in master.columns:
                        mask |= (master[fc].astype(str).str.lower()
                                 .str.contains(kw.lower(), na=False))
        else:
            col_map = {"policy_domain": "policy_domain_standardized",
                       "beneficiary":   "beneficiary_primary_category"}
            col = col_map.get(cat_type, "category_main_label")
            if col in master.columns and cat_val:
                mask = master[col].str.contains(cat_val, case=False, na=False)

        matched = master.loc[mask]
        if matched.empty:
            continue
        gaining = 0
        if "momentum" in matched.columns:
            gaining = int((matched["momentum"].astype(str) == "Gaining").sum())
        cat_stats[label] = {"total": int(len(matched)), "gaining": gaining}
        if "y_prob" in matched.columns:
            matched = matched.sort_values("y_prob", ascending=False)
        for bid in matched["bill_id"].astype(str).head(MAX_CAT_IN_UNIVERSE):
            hits.setdefault(bid, []).append(label)
    return hits, cat_stats


def _num(v, default=None):
    import pandas as pd
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return default
        return float(v)
    except Exception:
        return default


def _assemble_brief(master, pinned_bills: list, categories: list,
                    user_impact, prev_state: dict,
                    sections_pref: list, trail: dict = None,
                    diffs: dict = None) -> Optional[dict]:
    """Compute everything the sections need, plus the next state snapshot.

    Returns dict:
      first_run   bool
      moved       [ {line-dict} ]   changed since last digest
      flagged     [ {line-dict} ]   newly Moderate/High on user's prompts
      imminent    [ {line-dict} ]   highest floor-vote likelihood
      watch_count int               relevant bills with no change
      prompts     [labels]          the user's prompt labels
      snapshot    dict              state to persist after send
    """
    import pandas as pd

    m = master.copy()
    m["bill_id"] = m["bill_id"].astype(str).str.strip()
    m = m.drop_duplicates(subset=["bill_id"], keep="last").set_index("bill_id", drop=False)

    pinned_ids = {str(s.get("data", {}).get("bill_id")) for s in pinned_bills}
    pinned_ids = {b for b in pinned_ids if b and b in m.index}
    cat_hits, cat_stats = _category_bill_ids(m, categories)

    # Impact lookups: per bill, this user's ratings; flagged = rated >= Moderate
    imp_by_bill: dict[str, list] = {}
    flagged_now: dict[str, list] = {}     # bill_id -> [prompt labels >= Moderate]
    prompt_labels: list = []
    if user_impact is not None:
        prompt_labels = sorted(user_impact["prompt_label"].dropna().unique().tolist())
        for r in user_impact.itertuples(index=False):
            bid = str(r.bill_id)
            imp_by_bill.setdefault(bid, []).append({
                "label": getattr(r, "prompt_label", ""),
                "rating": getattr(r, "rating_label", ""),
                "rating_num": int(getattr(r, "rating_num", 0) or 0),
                "why": str(getattr(r, "explanation", "") or "")[:220],
            })
            if int(getattr(r, "rating_num", 0) or 0) >= 2:   # Moderate/High
                flagged_now.setdefault(bid, []).append(
                    f"{getattr(r, 'prompt_label', '')} ({getattr(r, 'rating_label', '')})")

    # The user's relevant universe: pinned ∪ top category hits ∪ impact-
    # flagged. Moderate-rated bills only count toward the universe when they
    # are reasonably imminent (>=50% likelihood) — otherwise hundreds of
    # low-probability Moderates inflate WATCHING into meaninglessness.
    # (The FULL flagged set is still recorded in state for newly-flagged
    # detection, so a Moderate that later gains steam surfaces normally.)
    def _prob_of(bid):
        v = _num(m.loc[bid].get("y_prob")) if bid in m.index else None
        return 0 if v is None else v * 100
    high_ids = {b for b, _ in flagged_now.items()
                if any(i["rating_num"] >= 3 for i in imp_by_bill.get(b, []))}
    mod_imminent = {b for b in flagged_now
                    if b not in high_ids and _prob_of(b) >= 50}
    relevant = set(pinned_ids) | set(cat_hits) | high_ids | mod_imminent
    relevant = {b for b in relevant if b in m.index}
    if not relevant:
        return None

    prev_bills   = prev_state.get("bills", {})
    prev_flagged = set(prev_state.get("flagged_ids", []))
    first_run    = not bool(prev_state)

    def bill_facts(bid: str) -> dict:
        row = m.loc[bid]
        prob = _num(row.get("y_prob"))
        n_act = _num(row.get("n_actions"))
        return {
            "bill_id":    bid,
            "number":     str(row.get("bill_number", "")).strip(),
            "title":      str(row.get("title", "Untitled"))[:110],
            "prob":       None if prob is None else round(prob * 100),
            "momentum":   str(row.get("momentum", "") or ""),
            "status":     str(row.get("status_desc", "") or ""),
            "n_actions":  None if n_act is None else int(n_act),
            "last_action": str(row.get("last_action", "") or "")[:120],
            "last_action_date": str(row.get("last_action_date", "") or "")[:10],
            "pinned":     bid in pinned_ids,
            "categories": cat_hits.get(bid, []),
            "impact":     sorted(imp_by_bill.get(bid, []),
                                 key=lambda d: -d["rating_num"])[:5],
            "actions":    (trail or {}).get(bid, []),
            "diff":       (diffs or {}).get(bid),
        }

    def _changes_vs(f: dict, prev: dict) -> list:
        changes = []
        p_now, p_prev = f["prob"], prev.get("prob")
        if p_now is not None and p_prev is not None and abs(p_now - p_prev) >= PROB_DELTA_PTS:
            changes.append(f"floor-vote likelihood {p_prev}% → {p_now}% "
                           f"({'+' if p_now > p_prev else ''}{p_now - p_prev} pts)")
        if f["status"] and prev.get("status") and f["status"] != prev["status"]:
            changes.append(f"status {prev['status']} → {f['status']}")
        if f["momentum"] and prev.get("momentum") and f["momentum"] != prev["momentum"]:
            changes.append(f"momentum {prev['momentum']} → {f['momentum']}")
        a_now, a_prev = f["n_actions"], prev.get("n_actions")
        if a_now is not None and a_prev is not None and a_now > a_prev:
            latest = f'; latest: "{f["last_action"]}"' if f["last_action"] else ""
            changes.append(f"{a_now - a_prev} new legislative action(s)"
                           f"{latest} ({f['last_action_date']})")
        return changes

    # ── TRACKED: the user's pinned watchlist — changes reported explicitly ──
    tracked_changed, tracked_unchanged = [], 0
    for bid in pinned_ids:
        f = bill_facts(bid)
        if bid in prev_bills:
            changes = _changes_vs(f, prev_bills[bid])
            if changes:
                f["changes"] = changes
                f["change_size"] = len(changes) * 10 + abs(
                    (f["prob"] or 0) - (prev_bills[bid].get("prob") or 0))
                tracked_changed.append(f)
            else:
                tracked_unchanged += 1
        else:
            tracked_unchanged += 1   # first sighting — baselined, not "changed"
    tracked_changed.sort(key=lambda d: -d["change_size"])
    tracked_changed = tracked_changed[:MAX_TRACKED_CHANGED]
    tracked_baseline = []
    if first_run and pinned_ids:
        tracked_baseline = sorted((bill_facts(b) for b in pinned_ids),
                                  key=lambda d: -(d["prob"] or 0))[:6]

    # ── MOVED: non-pinned relevant bills vs previous snapshot ───────────────
    moved = []
    for bid in relevant - pinned_ids:
        if bid not in prev_bills:
            continue
        f = bill_facts(bid)
        changes = _changes_vs(f, prev_bills[bid])
        if changes:
            f["changes"] = changes
            f["change_size"] = abs((f["prob"] or 0)
                                   - (prev_bills[bid].get("prob") or 0))
            moved.append(f)
    moved.sort(key=lambda d: -d["change_size"])
    moved = moved[:MAX_MOVED]

    # ── NEWLY FLAGGED: Moderate/High now, not flagged in last digest ────────
    flagged = []
    for bid, labels in flagged_now.items():
        if bid in prev_flagged:
            continue
        f = bill_facts(bid)
        f["flagged_on"] = labels
        flagged.append(f)
    flagged.sort(key=lambda d: (-max((i["rating_num"] for i in d["impact"]), default=0),
                                -(d["prob"] or 0)))
    flagged = flagged[:MAX_NEWLY_FLAGGED]

    # Group flagged bills under the prompt(s) that flagged them, so the brief
    # reads as "answers to YOUR questions" rather than a flat bill list.
    # A bill can appear under multiple prompts; within each we keep the
    # highest-rated first. This is what makes the digest legible to the
    # theme-oriented reader (e.g. academic-policy lens vs budget lens).
    flagged_by_prompt: dict = {}
    for f in flagged:
        for imp in f["impact"]:
            if imp["rating_num"] >= 2:
                flagged_by_prompt.setdefault(imp["label"], []).append({
                    "number": f["number"], "title": f["title"],
                    "prob": f["prob"], "momentum": f["momentum"],
                    "rating": imp["rating"], "rating_num": imp["rating_num"],
                    "why": imp["why"],
                })
    for lbl in flagged_by_prompt:
        flagged_by_prompt[lbl].sort(key=lambda d: -d["rating_num"])

    # ── MOST IMMINENT: top probability among relevant ────────────────────────
    moved_ids   = {d["bill_id"] for d in moved}
    flagged_ids = {d["bill_id"] for d in flagged}
    def _max_rating(d):
        return max((i["rating_num"] for i in d["impact"]), default=0)
    imminent_pool = [bill_facts(b) for b in relevant]
    if prompt_labels:   # user has impact questions → imminent ∩ impactful
        imminent_pool = [d for d in imminent_pool
                         if _max_rating(d) >= 2 or d["pinned"]]
    imminent = sorted(imminent_pool,
                      key=lambda d: (-(d["prob"] or 0), -_max_rating(d)))
    imminent = [d for d in imminent
                if d["bill_id"] not in moved_ids and d["bill_id"] not in flagged_ids]
    imminent = imminent[:MAX_IMMINENT]

    covered = (moved_ids | flagged_ids | {d["bill_id"] for d in imminent}
               | {d["bill_id"] for d in tracked_changed})
    watch_count = len(relevant - covered)

    # ── TRENDS: session + category weather (computed, current-state only) ───
    total_active = int(len(m))
    gaining_all  = int((m.get("momentum", "").astype(str) == "Gaining").sum()) \
        if "momentum" in m.columns else 0
    near_vote    = 0
    if "y_prob" in m.columns:
        import pandas as _pd
        near_vote = int((_pd.to_numeric(m["y_prob"], errors="coerce") >= 0.65).sum())
    trends = {
        "total_active": total_active,
        "gaining":      gaining_all,
        "near_vote":    near_vote,
        "categories":   cat_stats,
    }

    # ── Snapshot for next time ───────────────────────────────────────────────
    snapshot = {
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "bills": {
            bid: {
                "prob":      bill_facts(bid)["prob"],
                "status":    bill_facts(bid)["status"],
                "momentum":  bill_facts(bid)["momentum"],
                "n_actions": bill_facts(bid)["n_actions"],
                "last_idx":  (bill_facts(bid)["actions"][-1]["idx"]
                              if bill_facts(bid)["actions"] else 0),
            } for bid in relevant
        },
        "flagged_ids": sorted(flagged_now.keys()),
    }

    return {
        "first_run":        first_run,
        "tracked_changed":  tracked_changed,
        "tracked_unchanged": tracked_unchanged,
        "tracked_baseline": tracked_baseline,
        "moved":            moved,
        "flagged":          flagged,
        "flagged_by_prompt": flagged_by_prompt,
        "imminent":         imminent,
        "trends":           trends,
        "watch_count":      watch_count,
        "n_relevant":       len(relevant),
        "prompts":          prompt_labels,
        "snapshot":         snapshot,
        "sections":         list(sections_pref),
    }


# ---------------------------------------------------------------------------
# Prompt v2 — the model formats and synthesizes; the data is authoritative.
# ---------------------------------------------------------------------------
def _tracked_line(f: dict, prev_bills: dict) -> str:
    """Rich line for a watchlist bill: what happened this period, the arc,
    and any bill-text change. Feeds the LLM structured facts to narrate."""
    since_idx = prev_bills.get(f["bill_id"], {}).get("last_idx", 0)
    summ = _summarize_actions(f["actions"], since_idx)
    head = f"{f['number']} — {f['title']}"
    if f["prob"] is not None:
        head += f" | floor-vote likelihood {f['prob']}% (session avg {SESSION_AVG_PROB}%)"
    if f["momentum"]:
        head += f" | momentum {f['momentum']}"
    line = "  • " + head
    if summ["recent"]:
        line += "\n      RECENT ACTIONS (since last briefing): " + "; ".join(summ["recent"])
    if summ["arc"]:
        line += "\n      HISTORY: " + summ["arc"]
    if f.get("diff"):
        d = f["diff"]
        line += (f"\n      BILL TEXT CHANGED ({d['changed_at']}): "
                 f"{d['added']} lines added, {d['removed']} removed"
                 + (f"; fields: {d['fields']}" if d["fields"] else ""))
    for imp in f["impact"][:3]:
        if imp["rating_num"] >= 1:
            line += (f"\n      your \"{imp['label']}\" rating: {imp['rating']}"
                     f" — {imp['why']}")
    return line


def _friendly_label(lbl: str) -> str:
    """'Role Profile' is an internal label — the reader never created a question
    by that name. Render it as what it actually is."""
    s = str(lbl or "").strip()
    if s.lower() in ("role profile", "role_profile", "profile"):
        return "your role profile"
    return s


def _fact_line(f: dict) -> str:
    bits = [f"{f['number']} — {f['title']}"]
    if f["prob"] is not None:
        bits.append(f"floor-vote likelihood {f['prob']}% (session avg {SESSION_AVG_PROB}%)")
    if f["momentum"]:
        bits.append(f"momentum {f['momentum']}")
    if f["status"]:
        bits.append(f"status {f['status']}")
    if f.get("changes"):
        bits.append("CHANGED: " + "; ".join(f["changes"]))
    if f.get("flagged_on"):
        bits.append("newly flagged on: " + ", ".join(
            _friendly_label(x) for x in f["flagged_on"]))
    if f["pinned"]:
        bits.append("on the reader's watchlist")
    if f["categories"]:
        bits.append("matched monitors: " + ", ".join(f["categories"][:3]))
    line = "  • " + " | ".join(bits)
    for imp in f["impact"][:3]:
        if imp["rating_num"] >= 1:   # skip No Impact in the prompt for brevity
            line += (f"\n      - reader's \"{imp['label']}\" rating: {imp['rating']}"
                     f" — stored explanation: \"{imp['why']}\"")
    return line


def _build_prompt_v2(brief: dict, prev_state: dict, today: date, freq: str,
                     sections_pref: list) -> str:
    period = "since your last briefing"
    if prev_state.get("sent_at"):
        period = f"since your last briefing ({prev_state['sent_at'][:10]})"

    want = set(sections_pref)
    prev_bills = prev_state.get("bills", {})
    sections = []

    if "tracked" in want and brief["tracked_changed"]:
        sections.append("YOUR TRACKED BILLS\n(changes " + period + ")\n" +
                        "\n".join(_tracked_line(f, prev_bills) for f in brief["tracked_changed"]) +
                        f"\n  ({brief['tracked_unchanged']} other tracked bills unchanged)")
    elif "tracked" in want and brief["tracked_baseline"]:
        sections.append("YOUR TRACKED BILLS\n(baseline — first briefing)\n" +
                        "\n".join(_tracked_line(f, prev_bills) for f in brief["tracked_baseline"]))
    elif "tracked" in want and brief["tracked_unchanged"]:
        sections.append(f"YOUR TRACKED BILLS\nAll {brief['tracked_unchanged']} "
                        "unchanged this period.")

    if brief["moved"]:
        sections.append("OTHER MOVERS\n(changes " + period + ")\n" +
                        "\n".join(_fact_line(f) for f in brief["moved"]))
    if "flagged" in want and brief["flagged_by_prompt"]:
        blocks = []
        for lbl, bills in brief["flagged_by_prompt"].items():
            lines = []
            for bb in bills:
                mv = f" | momentum {bb['momentum']}" if bb["momentum"] else ""
                pr = f" | likelihood {bb['prob']}%" if bb["prob"] is not None else ""
                lines.append(f"    - {bb['number']} — {bb['title']}{pr}{mv} "
                             f"| {bb['rating']} — {bb['why']}")
            blocks.append(f'  Your "{lbl}" question flagged:\n' + "\n".join(lines))
        sections.append("NEWLY FLAGGED FOR YOU\n(grouped by your impact questions)\n" +
                        "\n".join(blocks))
    if "imminent" in want and brief["imminent"]:
        sections.append("MOST IMMINENT\n(highest reaches-vote likelihood among the "
                        "reader's items)\n" +
                        "\n".join(_fact_line(f) for f in brief["imminent"]))
    if "trends" in want:
        t = brief["trends"]
        # Portfolio-level first (what the reader tracks), session-level as backdrop.
        pf = (f"{brief['n_relevant']} bills across your questions and watchlist; "
              f"{len(brief['flagged'])} newly flagged this period; "
              f"{len(brief['tracked_changed'])} of your tracked bills moved")
        cat_bits = "; ".join(
            f"{lbl}: {s['total']} active ({s['gaining']} gaining)"
            for lbl, s in list(t["categories"].items())[:6])
        sections.append(
            "THE BIG PICTURE:\n"
            f"  • YOUR PORTFOLIO: {pf}"
            + (f"\n  • Your monitored themes — {cat_bits}" if cat_bits else "")
            + f"\n  • Session backdrop: {t['total_active']} active bills statewide, "
              f"{t['gaining']} gaining momentum, {t['near_vote']} near a floor vote")

    data_block = "\n\n".join(sections) if sections else "(no items)"

    # If the reader has pinned nothing, no YOUR TRACKED BILLS section reaches the
    # DATA block at all — but the model has been inventing one from the flagged
    # bills and asserting the reader tracks them. Forbid it explicitly.
    no_tracked_note = ""
    if not (brief["tracked_changed"] or brief["tracked_baseline"]
            or brief["tracked_unchanged"]):
        no_tracked_note = (
            "The reader has NO tracked bills — they have not pinned anything. "
            "Do NOT emit a YOUR TRACKED BILLS section under any circumstances. "
            "Never describe any bill as one the reader is tracking, watching, "
            "following, or monitoring.\n")

    first_run_note = ""
    if brief["first_run"]:
        first_run_note = (
            "This is the reader's FIRST briefing. REQUIRED: open with a section "
            "titled YOUR BASELINE BRIEFING containing exactly two sentences: this "
            "briefing establishes the baseline for their watchlist and questions, "
            "and future editions will report only what changed. Then continue "
            "with the sections below (there is no change data yet).\n")

    has_changes = bool(brief["tracked_changed"] or brief["moved"] or brief["flagged"])
    quiet_note = ""
    if not has_changes and not brief["first_run"]:
        quiet_note = (
            "Nothing material moved. Write a SHORT briefing (under 120 words): "
            "state plainly that none of the reader's items changed, give the one "
            "most imminent item as a single sentence, and stop. Do not pad.\n")

    # Build the section list from what is ACTUALLY in the data block. Listing all
    # eight and relying on a prohibition doesn't work — the model treats the list
    # as a template and fills in missing sections from whatever data it has.
    present = {s.split("\n", 1)[0].strip().rstrip(":").strip() for s in sections}
    canonical = ["YOUR TRACKED BILLS", "OTHER MOVERS", "NEWLY FLAGGED FOR YOU",
                 "MOST IMMINENT", "THE BIG PICTURE"]
    ordered = ["THE HEADLINE"]
    if brief["first_run"]:
        ordered.append("YOUR BASELINE BRIEFING")
    ordered += [h for h in canonical if h in present]
    ordered.append("WATCHING")
    section_order = ", ".join(ordered)
    omitted = [h for h in canonical if h not in present]
    omit_note = ""
    if omitted:
        omit_note = ("\nThese sections have NO data this period and must NOT appear "
                     "in your output at all — not as a header, not as a line saying "
                     "there is nothing: " + ", ".join(omitted) + ".\n")

    return f"""You are a senior legislative analyst writing a personalized intelligence briefing for one University of Illinois stakeholder. Today is {today.strftime('%A, %B %d, %Y')}. The reader receives this {freq}.

THE DATA BELOW IS AUTHORITATIVE. Use only these bills and these numbers. Never invent bills, probabilities, dates, or impacts. The "stored explanation" lines are the reader's own AI impact analysis — quote or closely paraphrase them; do not re-speculate.

{first_run_note}{quiet_note}{no_tracked_note}FORMAT — output EXACTLY these sections, in this order, using exactly these headers: {section_order}. Do not add any other section. Never rename a section, never merge two sections, never move content between sections.{omit_note}

THE HEADLINE
2-4 sentences of SYNTHESIS, not a single bill. Step back and tell the reader what this period means for THEIR portfolio as a whole: which of their themes/questions saw the most movement, the single most consequential specific item (with its number, likelihood, and trend), and any emerging risk or opportunity. Write for a busy executive who may read only this section — it must stand alone as the week's verdict. Ground every claim in the data below; name the specific bill for the headline item, but frame the rest thematically (e.g. "your budget lens saw the most activity" / "higher-ed accountability advanced on three fronts"). No throat-clearing, no generic openers.

YOUR TRACKED BILLS
For each bill, write a short narrative paragraph (2-4 sentences):
  1. What happened THIS PERIOD — translate the RECENT ACTIONS into plain English (what the legislative action means, not just its official label) and note any likelihood movement.
  2. If BILL TEXT CHANGED is present, state plainly what changed (fields, scale of edit) and why it may matter.
  3. If a HISTORY line is present for the bill, give one clause of that context. If NO HISTORY line is present, say NOTHING about how far along the bill is — omit stage entirely rather than guessing.
  4. If an impact rating is given, connect the movement to why this reader cares.
Do not invent actions or dates — use only what is provided. If a bill has no recent actions, give one sentence on its current status and stop.

OTHER MOVERS / NEWLY FLAGGED FOR YOU
One line per bill: what changed or which impact question flagged it, the rating, and the stored explanation's core point.

MOST IMMINENT
One line per bill, STRICTLY: bill number, its title, and the likelihood vs the {SESSION_AVG_PROB}% session average. Do NOT add significance commentary — if no rating or explanation was provided for a bill, say nothing beyond number, title, likelihood.

THE BIG PICTURE
Two or three sentences. The FIRST sentence MUST report the reader's own portfolio using the YOUR PORTFOLIO line in the data (their bill count, newly flagged count, tracked bills moved) — this is mandatory, never omit it. Only AFTER that, mention the statewide session backdrop as one closing clause. Numbers only from the data.

WATCHING
Exactly one sentence: "{brief['watch_count']} other items on your radar showed no material change." (adjust the number only)

STYLE RULES — strictly enforced:
- Declarative sentences. Render judgments, not possibilities.
- Every probability cited with its trend or vs the {SESSION_AVG_PROB}% session average.
- At most ONE hedge word ("may", "could", "might") per bill. Zero is better.
- BANNED: any closing outlook sentence ("leadership should remain engaged",
  "the landscape is evolving", "stay tuned"), and any speculation about media
  or political reception. Describe mechanism and status only.
- NEVER describe a bill's stage or position in the process unless a HISTORY line
  or a status value is supplied for that specific bill. Without that data these
  are BANNED: "early stages", "final stages", "well along", "very advanced",
  "positioned for a vote", "nearing the end", "progressing through", "under
  consideration", "awaiting further action". Say nothing about stage instead.
- NEVER use a change verb for a bill unless a CHANGED line is supplied for it.
  BANNED without CHANGED data: "dropped to", "rose to", "fell to", "now rated",
  "has passed", "was introduced", "gaining traction", "lost support".
- The model predicts a bill REACHING A FLOOR VOTE, not becoming law. BANNED
  words: "passage", "pass", "passes", "passing", "will become law", "enacted".
  Always write "reaches-vote likelihood" or "likelihood of reaching a floor vote".
- Plain professional prose. No emoji. Bill numbers in caps (HB1234, SB567).
- Do NOT use markdown formatting — no asterisks for bold, no underscores for
  italics. Plain text only; the email renderer does its own styling.
- Total length: under 400 words (under 120 if the quiet-day rule applies).

READER'S IMPACT QUESTIONS: {', '.join(_friendly_label(p) for p in brief['prompts']) if brief['prompts'] else '(none defined yet)'}

DATA:
{data_block}
"""


# ---------------------------------------------------------------------------
# Azure OpenAI (unchanged mechanics; slightly cooler temperature)
# ---------------------------------------------------------------------------
def _generate_digest(prompt: str) -> Optional[str]:
    endpoint   = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
    key        = os.environ.get("AZURE_OPENAI_KEY", "").strip()
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini").strip()
    api_ver    = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview").strip()
    if not endpoint or not key:
        logger.error("Azure OpenAI not configured — cannot generate digest")
        return None
    try:
        from openai import AzureOpenAI
        client = AzureOpenAI(azure_endpoint=endpoint, api_key=key, api_version=api_ver)
        resp = client.chat.completions.create(
            model=deployment,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=OPENAI_MAX_TOKENS,
            temperature=0.3,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.error("OpenAI generation failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Email rendering — sections become styled blocks
# ---------------------------------------------------------------------------
_SECTION_HEADERS = ["THE HEADLINE", "YOUR BASELINE", "YOUR TRACKED BILLS",
                    "OTHER MOVERS", "MOVED", "NEWLY FLAGGED", "MOST IMMINENT",
                    "THE BIG PICTURE", "WATCHING"]

# Sub-headers within NEWLY FLAGGED (the per-question groupings) render as
# bolded inline labels, not full section rules.


def _build_subject(today: date, brief: dict) -> str:
    base = f"IL Legislative Briefing — {today.strftime('%b %d, %Y')}"
    n_moved = len(brief["moved"]) + len(brief["tracked_changed"])
    n_flag  = len(brief["flagged"])
    tags = []
    if n_moved:
        tags.append(f"{n_moved} moved")
    if n_flag:
        tags.append(f"{n_flag} newly flagged")
    if brief["first_run"]:
        tags = ["your baseline briefing"]
    if not tags and not brief["first_run"]:
        tags = ["quiet day"]
    return f"{base} · {', '.join(tags)}"


def _render_digest_html(digest_text: str) -> str:
    """Turn the model's sectioned plain text into styled HTML blocks."""
    import html as _html
    out, in_list = [], False

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in digest_text.split("\n"):
        line = raw.strip()
        if not line:
            close_list()
            continue
        stripped = line.rstrip(":").strip("*# ").upper()
        if any(stripped.startswith(h) for h in _SECTION_HEADERS):
            close_list()
            out.append(
                f'<p style="margin:22px 0 8px;font-size:11px;font-weight:800;'
                f'letter-spacing:.08em;color:#13294B;text-transform:uppercase;'
                f'border-bottom:2px solid #E2E8F0;padding-bottom:5px;">'
                f'{_html.escape(line.rstrip(":").strip("*# "))}</p>')
            continue
        # Strip markdown emphasis the model sometimes emits — otherwise the
        # asterisks render literally in the email.
        body = re.sub(r"\*\*(.+?)\*\*", r"\1", line.lstrip("-• ").strip())
        body = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", body)
        body = _html.escape(body)
        # bold bill numbers for scannability
        body = re.sub(r"\b((?:HB|SB|HR|SR|HJR|SJR)\s?\d{1,5})\b",
                      r"<strong>\1</strong>", body)
        if line.startswith(("-", "•")):
            if not in_list:
                out.append('<ul style="margin:4px 0 10px;padding-left:20px;">')
                in_list = True
            out.append(f'<li style="margin:0 0 8px;line-height:1.65;">{body}</li>')
        else:
            close_list()
            out.append(f'<p style="margin:0 0 12px;line-height:1.7;">{body}</p>')
    close_list()
    return "\n".join(out)


def _build_email_html(digest_text: str, email: str, today: date) -> str:
    body_html = _render_digest_html(digest_text)
    date_str  = today.strftime("%B %d, %Y")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>IL Legislative Intelligence — Briefing</title>
</head>
<body style="margin:0;padding:0;background:#F8FAFC;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#F8FAFC;padding:40px 0;">
    <tr><td align="center">
      <table width="580" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:16px;border:1px solid #E2E8F0;overflow:hidden;">

        <!-- Header -->
        <tr><td style="background:linear-gradient(135deg,#0F1F38 0%,#1B4B5A 55%,#2D6A7A 100%);padding:28px 36px;">
          <div style="font-size:1.6rem;margin-bottom:6px">🏛</div>
          <div style="color:#fff;font-size:19px;font-weight:800;letter-spacing:-.03em;">
            IL Legislative Intelligence
          </div>
          <div style="color:rgba(255,255,255,.65);font-size:11px;margin-top:3px;">
            Personalized Briefing · {date_str}
          </div>
        </td></tr>

        <!-- Body -->
        <tr><td style="padding:24px 36px 8px;font-size:14px;color:#374151;">
          {body_html}
        </td></tr>

        <!-- CTA -->
        <tr><td style="padding:12px 36px 28px;">
          <table cellpadding="0" cellspacing="0">
            <tr><td style="background:#13294B;border-radius:10px;">
              <a href="{APP_BASE_URL}"
                 style="display:inline-block;padding:12px 28px;color:#fff;font-size:14px;
                        font-weight:700;text-decoration:none;">
                Open Platform →
              </a>
            </td></tr>
          </table>
        </td></tr>

        <!-- Footer -->
        <tr><td style="padding:20px 36px;border-top:1px solid #F1F5F9;">
          <p style="margin:0;font-size:11px;color:#454F5A;line-height:1.6;">
            University of Illinois System · Institute of Government and Public Affairs<br>
            Ratings and explanations come from your own impact questions,
            scored nightly against every active bill.<br>
            You're receiving this because digest emails are enabled in your
            <a href="{APP_BASE_URL}" style="color:#13294B;">My Tracker</a> settings.
            To unsubscribe: My Tracker → ⚙️ Settings → disable "Enable digest emails".
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Data loading (unchanged from v1)
# ---------------------------------------------------------------------------
def _load_master(session_id: int):
    import pandas as pd
    try:
        from data_catalog import GOLD_URLS, PREDICTION_URLS
    except ImportError as e:
        logger.error("data_catalog not available: %s", e)
        return None

    gold_url = GOLD_URLS.get(session_id)
    if not gold_url:
        logger.error("No GOLD_URL for session %d", session_id)
        return None
    try:
        gold = pd.read_parquet(gold_url)
        gold["bill_id"] = gold["bill_id"].astype(str).str.strip()
        gold["impact_rating_score"] = pd.to_numeric(
            gold.get("impact_rating_score"), errors="coerce").fillna(0).astype(int)
    except Exception as e:
        logger.error("Failed to load gold for session %d: %s", session_id, e)
        return None

    pred_url = PREDICTION_URLS.get(session_id)
    if pred_url:
        try:
            preds = pd.read_parquet(pred_url)
            preds["bill_id"] = preds["bill_id"].astype(str).str.strip()
            keep = [c for c in ["bill_id", "y_prob", "momentum", "n_actions",
                                "status_desc", "last_action"]
                    if c in preds.columns]
            gold = gold.merge(preds[keep], on="bill_id", how="left",
                              suffixes=("", "_pred"))
        except Exception as e:
            logger.warning("Predictions unavailable for session %d: %s", session_id, e)
    return gold


def _list_all_users() -> list[dict]:
    try:
        from azure.cosmos import CosmosClient
        endpoint = os.environ["COSMOS_ENDPOINT"].strip().rstrip("/") + "/"
        key      = os.environ["COSMOS_KEY"].strip()
        db_name  = os.environ.get("COSMOS_DATABASE", "legplatform").strip()
        client   = CosmosClient(url=endpoint, credential=key)
        db       = client.get_database_client(db_name)
        c        = db.get_container_client("users")
        results  = _cross_partition_query(c, "SELECT * FROM c")

        # Four people exist twice — once under @illinois.edu and once under
        # @uillinois.edu — and both records carry saved items, so both receive
        # mail. The @illinois.edu record is the populated one in every case.
        # Sorting puts "@illinois" before "@uillinois", so first-seen wins.
        seen, deduped = set(), []
        for u in sorted(results, key=lambda x: str(x.get("email", "")).strip().lower()):
            canon = str(u.get("email", "")).strip().lower().replace(
                "@uillinois.edu", "@illinois.edu")
            if not canon:
                continue
            if canon in seen:
                logger.info("Skipping duplicate user record: %s", u.get("email"))
                continue
            seen.add(canon)
            deduped.append(u)
        logger.info("Users: %d records -> %d after dedupe", len(results), len(deduped))
        return deduped
    except Exception as e:
        _diag("list_users_exception", repr(e), "error")
        logger.error("_list_all_users failed: %s", e)
        return []
