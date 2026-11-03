'[Deployment-specific example removed from this source docstring.]'
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

PIPELINE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PIPELINE_DIR))

from services.azure_blob_service import BlobClient, TIER_GOLD          # noqa: E402
from services.vllm_service import VLLMConfig, health_check, run_vllm   # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("impact_scorer")

# ── Configuration (env-overridable) ──────────────────────────────────────────
ACTIVE_SESSIONS  = [int(s) for s in
                    os.environ.get("IMPACT_SESSIONS", "2176").split(",")]
PROMPTS_PATH     = os.environ.get(
    "IMPACT_PROMPTS_PATH",
    "legislation/impact_prompts/prompts_for_scoring.json")
FEATURES_PATH_T  = os.environ.get(
    "IMPACT_FEATURES_PATH",
    "legislation/features_standardized/llama31_1200/"
    "session={session}/_features_standardized.parquet")
RESULTS_PATH     = os.environ.get(
    "IMPACT_RESULTS_PATH",
    "legislation/impact_results/impact_results.parquet")
TITLES_PATH      = os.environ.get(
    "IMPACT_TITLES_PATH",
    "legislation/impact_prompts/prompt_titles.json")
PROFILES_PATH    = os.environ.get(
    "IMPACT_PROFILES_PATH",
    "legislation/impact_prompts/user_profiles.json")

VLLM_CFG = VLLMConfig(
    base_url        = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1"),
    model_name      = os.environ.get("VLLM_MODEL", "llama31-8b-awq"),
    timeout_seconds = int(os.environ.get("IMPACT_TIMEOUT", "180")),
    temperature     = 0.0,
    top_p           = 1.0,
    max_tokens      = int(os.environ.get("IMPACT_MAX_TOKENS", "700")),
)

RATINGS     = ["No Impact", "Low Impact", "Moderate Impact", "High Impact"]
RATING_NUM  = {r: i for i, r in enumerate(RATINGS)}          # 0..3
MAX_QUESTIONS_PER_CALL = 6

# Context-rich raw feature columns used as scoring context (same set the
# platform's standard U of I impact rating was derived from).
FEATURE_COLS = [
    ("llama_summary_raw",          "Summary",                 1200),
    ("legislative_goal_raw",       "Legislative goal",         400),
    ("key_provisions_raw",         "Key provisions",           800),
    ("intent_raw",                 "Intent",                   400),
    ("motivation_raw",             "Motivation",               400),
    ("intended_beneficiaries_raw", "Intended beneficiaries",   400),
    ("increasing_aspects_raw",     "Aspects being increased",  400),
    ("decreasing_aspects_raw",     "Aspects being decreased",  400),
]

SYSTEM_PROMPT = (
    "You are a legislative impact analyst for the University of Illinois "
    "Institute of Government and Public Affairs. You rate how bills impact "
    "specific stakeholders. You always respond with valid JSON and nothing "
    "else — no preamble, no markdown fences."
)

PROMPT_TEMPLATE = """Below is extracted information about one Illinois bill, followed by {n} impact question(s).

BILL INFORMATION:
{features_block}

QUESTIONS — for each, rate how this bill impacts the subject:
{questions_block}

For EACH question return:
  "rating": exactly one of "No Impact", "Low Impact", "Moderate Impact", "High Impact"
  "why": a 1-2 sentence explanation grounded in the bill information above

Respond with ONLY a JSON object keyed by question number, like:
{{"1": {{"rating": "...", "why": "..."}}, "2": {{"rating": "...", "why": "..."}}}}"""


# ── Helpers ──────────────────────────────────────────────────────────────────
def _features_hash(row: pd.Series) -> str:
    blob = "|".join(str(row.get(c, "") or "") for c, _, _ in FEATURE_COLS)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _question_hash(q: str) -> str:
    return hashlib.sha256(q.strip().lower().encode()).hexdigest()[:12]


def _features_block(row: pd.Series) -> str:
    parts = []
    for col, label, cap in FEATURE_COLS:
        val = str(row.get(col, "") or "").strip()
        if val and val.lower() not in ("nan", "none"):
            parts.append(f"{label}: {val[:cap]}")
    return "\n".join(parts) if parts else "(no extracted features available)"


def _parse_rating(text: str) -> Optional[str]:
    """Map arbitrary model text to one of the four canonical ratings."""
    t = (text or "").strip().lower()
    for r in RATINGS:
        if r.lower() in t:
            return r
    # numeric fallbacks the model sometimes emits
    m = re.search(r"\b([0-3])\b", t)
    if m:
        return RATINGS[int(m.group(1))]
    return None


def _extract_json(text: str) -> Optional[dict]:
    """Tolerant JSON extraction: strips fences, grabs outermost braces."""
    if not text:
        return None
    t = re.sub(r"```(?:json)?", "", text).strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(t[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


TITLE_PROMPT = (
    "Create a very short title (2 to 4 words, Title Case, no punctuation, no "
    "quotes) summarizing the SUBJECT of this legislative impact question. "
    "Respond with ONLY the title.\n\n"
    "Question: How does this bill impact {question}?\n\nTitle:"
)


def _ensure_titles(blob, prompts: list) -> None:
    """Attach a short AI-generated prompt_title to each prompt dict.
    Cached in a sidecar JSON in gold keyed by question_hash."""
    try:
        cache = blob.read_json(TIER_GOLD, TITLES_PATH)
    except FileNotFoundError:
        cache = {}
    dirty = False
    for p in prompts:
        qh = p["question_hash"]
        title = cache.get(qh, "")
        if not title:
            text, _, err = run_vllm(
                VLLMConfig(base_url=VLLM_CFG.base_url,
                           model_name=VLLM_CFG.model_name,
                           timeout_seconds=VLLM_CFG.timeout_seconds,
                           temperature=0.0, top_p=1.0, max_tokens=16),
                TITLE_PROMPT.format(question=p["question"]))
            if err is None and text:
                title = re.sub(r'["\'\n]', "", text).strip()
                title = re.sub(r'^title\s*[:\-]\s*', "", title,
                               flags=re.IGNORECASE)[:40]
                title = " ".join(title.split()[:5])
            if not title:
                title = p.get("prompt_label") or p["question"][:30]
            cache[qh] = title
            dirty = True
            log.info("Generated title for '%s...': %s", p["question"][:40], title)
        p["prompt_title"] = title
    if dirty:
        try:
            blob.write_json(cache, TIER_GOLD, TITLES_PATH)
        except Exception as e:
            log.warning("Could not persist title cache (non-fatal): %s", e)


def _profile_question(profile: dict) -> str:
    """Compose a user's role profile into ONE impact-scoring question.
    Scored exactly like any other prompt. Missing fields are omitted."""
    bits = []
    if profile.get("title"):
        bits.append(f"someone whose role is {profile['title']}")
    if profile.get("sector"):
        bits.append(f"working in the {profile['sector']} sector")
    if profile.get("functional"):
        bits.append(f"whose functional responsibilities are: {profile['functional']}")
    if profile.get("stakeholders"):
        bits.append(f"serving stakeholders/clients such as {profile['stakeholders']}")
    who = ", ".join(bits) if bits else "this professional"
    desc = (profile.get("description") or "").strip()
    tail = (f" They describe their work and what they watch for in legislation as: {desc}"
            if desc else "")
    q = f"{who}, considering what matters to their job and responsibilities.{tail}"
    return " ".join(q.split()).strip()


# ── Scoring core ─────────────────────────────────────────────────────────────
_counter_lock = threading.Lock()
_counters = {"calls": 0, "fallback_calls": 0, "parse_failures": 0}


def _bump(key: str, n: int = 1):
    with _counter_lock:
        _counters[key] += n


def score_user_bill(questions: List[dict], bill_row: pd.Series) -> List[dict]:
    """One batched vLLM call for all of one user's pending questions on one
    bill. Returns result-row dicts (may be empty on total failure).
    questions: [{prompt_id, prompt_label, question, question_hash}, ...]
    """
    fblock = _features_block(bill_row)
    qblock = "\n".join(f'{i + 1}. How does this bill impact {q["question"]}?'
                       for i, q in enumerate(questions))
    prompt = PROMPT_TEMPLATE.format(n=len(questions),
                                    features_block=fblock,
                                    questions_block=qblock)

    text, _, err = run_vllm(VLLM_CFG, prompt, system_prompt=SYSTEM_PROMPT)
    _bump("calls")

    parsed: Dict[str, dict] = {}
    if err is None:
        obj = _extract_json(text)
        if obj:
            for i in range(len(questions)):
                entry = obj.get(str(i + 1)) or obj.get(i + 1)
                if isinstance(entry, dict):
                    rating = _parse_rating(str(entry.get("rating", "")))
                    if rating:
                        parsed[str(i + 1)] = {
                            "rating": rating,
                            "why": str(entry.get("why", "")).strip()[:600],
                        }

    # Per-question fallback for anything the batched call didn't cover
    for i, q in enumerate(questions):
        if str(i + 1) in parsed:
            continue
        _bump("fallback_calls")
        single = PROMPT_TEMPLATE.format(
            n=1, features_block=fblock,
            questions_block=f'1. How does this bill impact {q["question"]}?')
        t2, _, e2 = run_vllm(VLLM_CFG, single, system_prompt=SYSTEM_PROMPT)
        rating, why = None, ""
        if e2 is None:
            obj = _extract_json(t2)
            entry = (obj or {}).get("1")
            if isinstance(entry, dict):
                rating = _parse_rating(str(entry.get("rating", "")))
                why = str(entry.get("why", "")).strip()[:600]
            if rating is None:                    # last resort: scan raw text
                rating = _parse_rating(t2 or "")
        if rating is None:
            _bump("parse_failures")
            log.warning("Unscorable: bill %s q='%s' (%s)",
                        bill_row.get("bill_number", bill_row["bill_id"]),
                        q["question"][:60], (e2 or "parse failure"))
            continue
        parsed[str(i + 1)] = {"rating": rating, "why": why}

    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for i, q in enumerate(questions):
        entry = parsed.get(str(i + 1))
        if not entry:
            continue
        rows.append({
            "user_id":       q["user_id"],
            "prompt_id":     q["prompt_id"],
            "prompt_label":  q["prompt_label"],
            "prompt_title":  q.get("prompt_title", q["prompt_label"]),
            "question":      q["question"],
            "bill_id":       str(bill_row["bill_id"]),
            "bill_number":   str(bill_row.get("bill_number", "") or ""),
            "session_id":    int(bill_row.get("session_id", ACTIVE_SESSIONS[0])),
            "rating_num":    RATING_NUM[entry["rating"]],
            "rating_label":  entry["rating"],
            "explanation":   entry["why"],
            "features_hash": bill_row["_fhash"],
            "question_hash": q["question_hash"],
            "scored_at":     now,
        })
    return rows


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="Score user impact prompts via local vLLM")
    ap.add_argument("--dry-run",   action="store_true", help="score but write nothing")
    ap.add_argument("--workers",   type=int, default=int(os.environ.get("IMPACT_WORKERS", "12")))
    ap.add_argument("--limit",     type=int, default=int(os.environ.get("IMPACT_MAX_PER_RUN", "0")),
                    help="cap (user,prompt,bill) scorings this run; 0 = unlimited")
    ap.add_argument("--max-bills", type=int, default=0, help="test: only first N bills")
    args = ap.parse_args()
    t0 = time.time()

    # 0. vLLM must be up (the daily notebook health-checks/starts it already,
    #    but this script must also be safe to run standalone).
    ok, msg = health_check(VLLM_CFG)
    if not ok:
        log.error("vLLM health check failed: %s", msg)
        return 1
    log.info("vLLM: %s", msg)

    blob = BlobClient()

    # 1. Prompts from gold (exported from Cosmos by the Azure Function)
    try:
        pdoc = blob.read_json(TIER_GOLD, PROMPTS_PATH)
    except FileNotFoundError:
        log.info("No prompts file at gold/%s — nothing to score. "
                 "(Has prompt_exporter run yet?)", PROMPTS_PATH)
        return 0
    prompts = pdoc.get("prompts", [])
    prompts = [p for p in prompts
               if p.get("user_id") and p.get("prompt_id") and p.get("question")]
    log.info("Prompts file exported_at=%s — %d prompt(s) across %d user(s)",
             pdoc.get("exported_at", "?"), len(prompts),
             len({p["user_id"] for p in prompts}))
    for p in prompts:
        p["question_hash"] = _question_hash(p["question"])
        p.setdefault("prompt_label", p["question"][:40])
    valid_prompt_ids = {p["prompt_id"] for p in prompts}
    if prompts:
        _ensure_titles(blob, prompts)

    # ── 1b. Role profiles → one always-on synthetic prompt per user ─────────
    # Placed AFTER _ensure_titles so the synthetic prompt skips the extra
    # title-generation vLLM call and uses a fixed "Role Profile" title.
    try:
        prof_doc = blob.read_json(TIER_GOLD, PROFILES_PATH)
        profiles = prof_doc.get("profiles", {})
    except FileNotFoundError:
        profiles = {}
    _added = 0
    for uid, prof in profiles.items():
        q = _profile_question(prof)
        if not q or not uid:
            continue
        sp = {
            "user_id":       uid,
            "prompt_id":     f"__profile__{uid}",
            "prompt_label":  "Role Profile",
            "question":      q,
            "question_hash": _question_hash(q),
            "prompt_title":  "Role Profile",
        }
        prompts.append(sp)
        valid_prompt_ids.add(sp["prompt_id"])
        _added += 1
    if _added:
        log.info("Added %d role-profile synthetic prompt(s)", _added)

    # 2. Existing results (for delta + pruning)
    try:
        existing = blob.read_parquet(TIER_GOLD, RESULTS_PATH)
    except FileNotFoundError:
        existing = pd.DataFrame()
    if not existing.empty:
        pre = len(existing)
        existing = existing[existing["prompt_id"].isin(valid_prompt_ids)]
        if len(existing) < pre:
            log.info("Pruned %d result rows for deleted prompts", pre - len(existing))
    done: Dict[Tuple[str, str], Tuple[str, str]] = {}
    if not existing.empty:
        for r in existing.itertuples(index=False):
            done[(r.prompt_id, str(r.bill_id))] = (r.features_hash, r.question_hash)

    if not prompts:
        log.info("Zero prompts — writing pruned results and exiting.")
        if not args.dry_run:
            blob.write_parquet(existing, TIER_GOLD, RESULTS_PATH)
        return 0

    # 3. Active-bill features
    frames = []
    for sid in ACTIVE_SESSIONS:
        path = FEATURES_PATH_T.format(session=sid)
        df = blob.read_parquet(TIER_GOLD, path)
        df["session_id"] = sid
        frames.append(df)
    feats = pd.concat(frames, ignore_index=True)
    feats["bill_id"] = feats["bill_id"].astype(str)
    # Active set = the app snapshot (one source of truth with the app's views).
    # Includes Enrolled bills — the most imminent ones. Falls back to no filter
    # if the snapshot is unavailable.
    _SNAPSHOT_PATH = os.environ.get(
        "IMPACT_SNAPSHOT_PATH",
        "legislation/app_snapshots/active_bills_snapshot.parquet")
    try:
        _snap = blob.read_parquet(TIER_GOLD, _SNAPSHOT_PATH)
        _active_ids = set(_snap["bill_id"].astype(str))
        feats = feats[feats["bill_id"].isin(_active_ids)]
        log.info("Filtered to app-snapshot active set: %d bills", len(feats))
    except FileNotFoundError:
        log.warning("Active-bills snapshot not found at gold/%s — scoring ALL "
                    "feature rows", _SNAPSHOT_PATH)
    feats = feats.drop_duplicates(subset=["bill_id"], keep="last")
    feats["_fhash"] = feats.apply(_features_hash, axis=1)
    if args.max_bills:
        feats = feats.head(args.max_bills)
    log.info("Active bills with features: %d", len(feats))

    # 4. Build work units: (user, bill) -> pending questions
    by_user: Dict[str, List[dict]] = {}
    for p in prompts:
        by_user.setdefault(p["user_id"], []).append(p)

    tasks: List[Tuple[List[dict], pd.Series]] = []
    n_pending = 0
    budget = args.limit if args.limit > 0 else 10**12
    for _, bill in feats.iterrows():
        if n_pending >= budget:
            break
        for user_id, uprompts in by_user.items():
            pending = [p for p in uprompts
                       if done.get((p["prompt_id"], bill["bill_id"]))
                       != (bill["_fhash"], p["question_hash"])]
            if not pending:
                continue
            take = pending[:min(len(pending),
                                MAX_QUESTIONS_PER_CALL,
                                max(1, int(budget - n_pending)))]
            tasks.append((take, bill))
            n_pending += len(take)
            if n_pending >= budget:
                break
    log.info("Pending scorings: %d (across %d vLLM calls, %d workers)",
             n_pending, len(tasks), args.workers)
    if not tasks:
        log.info("Everything up to date — done in %.1fs", time.time() - t0)
        return 0

    # 5. Score concurrently
    new_rows: List[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(score_user_bill, qs, bill) for qs, bill in tasks]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                new_rows.extend(fut.result())
            except Exception as e:
                log.error("Task failed: %s", e)
            if i % 200 == 0 or i == len(futures):
                log.info("  progress: %d/%d calls (%d rows, %d fallbacks)",
                         i, len(futures), len(new_rows),
                         _counters["fallback_calls"])

    # 6. Merge + write
    new_df = pd.DataFrame(new_rows)
    if not new_df.empty:
        if not existing.empty:
            key = ["prompt_id", "bill_id"]
            replaced = existing.merge(new_df[key].drop_duplicates(), on=key,
                                      how="left", indicator=True)
            existing = existing[replaced["_merge"] == "left_only"]
        merged = pd.concat([existing, new_df], ignore_index=True)
    else:
        merged = existing

    elapsed = time.time() - t0
    log.info("Scored %d new rows in %.1f min | vLLM calls=%d fallbacks=%d "
             "parse_failures=%d | results total=%d",
             len(new_df), elapsed / 60, _counters["calls"],
             _counters["fallback_calls"], _counters["parse_failures"],
             len(merged))

    if args.dry_run:
        log.info("[DRY RUN] Not writing. Sample:\n%s",
                 new_df.head(10).to_string() if not new_df.empty else "(none)")
        return 0

    blob.write_parquet(merged, TIER_GOLD, RESULTS_PATH)
    log.info("Wrote gold/%s", RESULTS_PATH)

    # 7. Stamp success for the job-execution monitor (never fatal)
    if os.environ.get("IMPACT_SKIP_PIPELINE_LOG", "").lower() not in ("1", "true"):
        try:
            from utils.pipeline_logger import PipelineLogger
            pl = PipelineLogger(workflow_name="impact_prompt_scorer",
                                log_dir=PIPELINE_DIR / "logs",
                                pipeline_version=datetime.now().strftime("%Y-%m-%d"))
            with pl.pipeline_run():
                with pl.stage("score_impact_prompts", order=1):
                    pl.set_stage_metadata(
                        input_count=n_pending,
                        output_count=len(new_df),
                        rows_written=len(merged),
                        output_file=f"gold/{RESULTS_PATH}",
                        output_file_written=True,
                        extra={"vllm_calls": _counters["calls"],
                               "fallback_calls": _counters["fallback_calls"],
                               "parse_failures": _counters["parse_failures"],
                               "elapsed_seconds": round(elapsed, 1)},
                    )
        except Exception as e:
            log.warning("Pipeline-log stamping failed (non-fatal): %s", e)

    return 0


if __name__ == "__main__":
    sys.exit(main())
