"""
investigate_flagged_bills.py — Legislation Pipeline (Stage 3 / Step 8)
=====================================================================
The "AI analyst" deep-dive. Reads the Stage-2 impact scores, keeps only the
bills a user's prompts/profile flagged as impactful, pulls the bill text
(reassembled from silver chunks), and produces a structured, personalized
investigation: what changes for this person, who it affects relative to them
and their stakeholders, recommended actions, a conditional SWOT, and a
confidence read.

Runs on the pipeline server as Step 8 (via scripts/run_personalization.sh),
AFTER Step 7 (score_impact_prompts.py). Non-fatal by design.

    scores    IN:  gold/legislation/impact_results/impact_results.parquet   (Stage 2)
    profiles  IN:  gold/legislation/impact_prompts/user_profiles.json       (exporter)
    features  IN:  gold/legislation/features_standardized/llama31_1200/
                   session={id}/_features_standardized.parquet              (objective, shared)
    chunks    IN:  silver/legislation/chunks_llama31_1200/
                   session={id}/_chunks.parquet                             (bill text)
    invest.   OUT: gold/legislation/investigations/investigations.parquet

Granularity: one investigation per (user_id, bill_id) — a bill flagged by
several of a user's prompts gets ONE coherent brief, not one per prompt.

CONTEXT BUDGET (the hard constraint)
------------------------------------
vLLM runs with --max-model-len 8192. Every request must fit:
    profile (~500 tok) + flags (~525) + features (~1000) + scaffolding (~375)
    + bill text + reserved output (max_tokens=1400)  <=  8192
That leaves ~4,400 tokens for bill text in the worst case; we budget ~3,000
(12,000 chars) and then ENFORCE it with a real token count before sending.

TEXT SELECTION (why head/tail)
------------------------------
26% of session-2176 bills exceed 12,000 chars of chunk text; above P90 they
exceed 41k. Reading chunks in order and stopping at the cap yields only the
bill's opening (short title + definitions) — the least informative part.
Instead we take chunks from the FRONT and the BACK, mirroring the chunker's
own handle_massive_bill() first_n/last_n philosophy, with an explicit elision
marker between. Bills usually put substance early and effective dates /
appropriations late; the middle is often boilerplate.

The excerpt is *supplementary*: the objective features block passed alongside
it was produced by the synthesizer's recursive meta-extraction over the WHOLE
bill, so nothing is truly lost — the excerpt adds primary-source specificity.
The prompt tells the model exactly this, so `confidence` stays honest.

CHECKPOINTING
-------------
Results are flushed to blob every --checkpoint-every completed investigations
(default 25), not only at the end. A crash costs one batch, not the whole run.
Because the delta keys on (content_hash, triage_sig, profile_sig), a re-run
skips everything already written.

Delta: a (user, bill) is re-investigated only when the bill text changed
(content_hash), the set/severity of flagging prompts changed (triage_sig), or
the user's profile changed (profile_hash) — else it is skipped.

Usage:
    python investigate_flagged_bills.py                 # nightly/weekend run
    python investigate_flagged_bills.py --dry-run       # build, write nothing
    python investigate_flagged_bills.py --limit 200     # cap investigations
    python investigate_flagged_bills.py --min-rating 1  # Low-and-above (default 2)
    python investigate_flagged_bills.py --workers 6
    python investigate_flagged_bills.py --checkpoint-every 25
"""
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

# The wrapper script does not export TIKTOKEN_CACHE_DIR the way
# run_legislation_daily.py does; set it so the tokenizer works offline.
os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(PIPELINE_DIR / "storage" / "cache"))

from services.azure_blob_service import BlobClient, TIER_GOLD, TIER_SILVER   # noqa: E402
from services.vllm_service import VLLMConfig, health_check, run_vllm          # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
# The Azure SDK logs every HTTP request/response at INFO, which buries the
# lines that matter in a long run.
logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
log = logging.getLogger("investigator")

# ── Configuration (env-overridable) ──────────────────────────────────────────
ACTIVE_SESSIONS = [int(s) for s in
                   os.environ.get("IMPACT_SESSIONS", "2176").split(",")]

RESULTS_PATH = os.environ.get(
    "IMPACT_RESULTS_PATH",
    "legislation/impact_results/impact_results.parquet")
PROFILES_PATH = os.environ.get(
    "IMPACT_PROFILES_PATH",
    "legislation/impact_prompts/user_profiles.json")
FEATURES_PATH_T = os.environ.get(
    "IMPACT_FEATURES_PATH",
    "legislation/features_standardized/llama31_1200/"
    "session={session}/_features_standardized.parquet")
CHUNKS_PATH_T = os.environ.get(
    "INVESTIGATE_CHUNKS_PATH",
    "legislation/chunks_llama31_1200/session={session}/_chunks.parquet")
INVEST_PATH = os.environ.get(
    "INVESTIGATE_RESULTS_PATH",
    "legislation/investigations/investigations.parquet")

MIN_RATING = int(os.environ.get("INVESTIGATE_MIN_RATING", "2"))

# Bill-text budget. 12,000 chars ~= 3,000 tokens. See CONTEXT BUDGET above.
TEXT_CHAR_CAP = int(os.environ.get("INVESTIGATE_TEXT_CHARS", "12000"))

# Hard context ceiling of the served model (must match vLLM --max-model-len).
MAX_MODEL_LEN = int(os.environ.get("VLLM_MAX_MODEL_LEN", "8192"))
# Tokens reserved for the model's JSON answer + a safety pad.
OUTPUT_RESERVE = int(os.environ.get("INVESTIGATE_MAX_TOKENS", "1400"))
BUDGET_PAD = int(os.environ.get("INVESTIGATE_BUDGET_PAD", "128"))

# Flush to blob every N completed investigations.
CHECKPOINT_EVERY = int(os.environ.get("INVESTIGATE_CHECKPOINT_EVERY", "25"))

VLLM_CFG = VLLMConfig(
    base_url        = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1"),
    model_name      = os.environ.get("VLLM_MODEL", "llama31-8b-awq"),
    timeout_seconds = int(os.environ.get("INVESTIGATE_TIMEOUT", "300")),
    temperature     = 0.0,
    top_p           = 1.0,
    max_tokens      = OUTPUT_RESERVE,
)

RATING_LABELS = ["No Impact", "Low Impact", "Moderate Impact", "High Impact"]

FEATURE_COLS = [
    ("llama_summary_raw",          "Summary",                 1200),
    ("legislative_goal_raw",       "Legislative goal",         400),
    ("key_provisions_raw",         "Key provisions",           800),
    ("intent_raw",                 "Intent",                   400),
    ("intended_beneficiaries_raw", "Intended beneficiaries",   400),
    ("increasing_aspects_raw",     "Aspects being increased",  400),
    ("decreasing_aspects_raw",     "Aspects being decreased",  400),
]

MAX_FLAG_ROWS = int(os.environ.get("INVESTIGATE_MAX_FLAGS", "6"))
FLAG_EXPL_CHARS = int(os.environ.get("INVESTIGATE_FLAG_CHARS", "300"))

SWOT_IMPACT_TYPES = {"strategic context", "advocacy", "compliance obligation"}
IMPACT_TYPES = [
    "compliance obligation", "funding or benefit", "operational change",
    "strategic context", "advocacy", "informational",
]

ELISION = "\n\n[... middle of bill elided — see extracted summary above ...]\n\n"

SYSTEM_PROMPT = (
    "You are a senior legislative analyst preparing a personalized briefing for "
    "one professional about one bill. You are precise, actionable, and grounded "
    "strictly in the material provided. You describe what legislation DOES and "
    "what it REQUIRES — never whether it is good, bad, popular, or controversial, "
    "and never whether it aligns with anyone's values or politics. Your reader "
    "works at a public institution and this briefing may become a public record. "
    "You always respond with valid JSON only — no preamble, no markdown fences."
)

PROMPT_TEMPLATE = """You are briefing the following person:
{profile_block}

This bill was flagged as potentially important to them for these reasons:
{flag_block}

OBJECTIVE BILL INFORMATION (extracted from the COMPLETE bill text):
{features_block}

BILL TEXT{excerpt_note}:
{bill_text}

Produce a personalized investigation as a SINGLE JSON object with exactly these keys:
  "headline": one sentence on why this bill matters to THIS person.
  "impact_type": exactly one of {impact_types}.
  "directness": "direct" (binds their org/role specifically) or "indirect" (affects their sector/environment).
  "what_changes": 1-3 sentences on what concretely changes for them if this becomes law.
  "recommended_actions": a JSON array of 1-4 short action strings (e.g. "Review Section 15", "Flag to compliance", "Monitor at committee").
  "who_it_affects": 1-2 sentences on who is affected in relation to them — their team, stakeholders, or clients.
  "timeline": a SHORT phrase (under 8 words) on urgency/stage if inferable, else exactly "unknown".
  "confidence": "High", "Medium", or "Low".
  "confidence_reason": one short clause grounding the confidence.
  "evidence": a VERBATIM quotation of 8-40 words copied from the bill text above —
    the specific language that justifies your headline. Copy it word for word; do
    not paraphrase or summarize. CRITICAL: the span must contain NO double-quote
    character and NO backslash and NO line break. If the passage you want contains
    any of those, pick a shorter span that does not. If no single passage supports
    the headline, use the empty string "".
  "deontic": what the cited language DOES, exactly one of "obligation" (someone
    must act), "permission" (someone may act), "prohibition" (someone must not
    act), "recommendation" (someone should act), or "none" (it does none of these
    — e.g. definitions, findings, appropriations without conditions).
  "swot": ONLY if impact_type is one of ["strategic context","advocacy","compliance obligation"], a JSON object with keys "strengths","weaknesses","opportunities","threats" (each one sentence); OTHERWISE null.

RULES FOR "swot" — read carefully:
  - Confine every quadrant to OPERATIONAL, COMPLIANCE, FISCAL, or ADMINISTRATIVE
    consequences for the reader's organization: cost, staffing, systems,
    reporting burden, timelines, legal exposure.
  - Do NOT assert anything about the organization's values, mission, politics,
    reputation, public standing, or public perception. Do NOT speculate about
    whether the bill is good or bad, popular or controversial, or aligned with
    anyone's beliefs. Do NOT restate the bill's own claimed social benefits as
    an "opportunity" — an opportunity means an operational or fiscal advantage
    to the reader's organization, nothing else.
  - Every claim must be traceable to the bill information above. If a quadrant
    has no grounded, operational content, use the exact string "None identified".
    Most compliance obligations have no strengths and no opportunities; that is
    a normal and correct answer.

Respond with ONLY the JSON object."""

EXCERPT_NOTE = (
    " (EXCERPT — the opening and closing sections of a longer bill; the middle "
    "is elided. The OBJECTIVE BILL INFORMATION above was extracted from the "
    "complete text, so rely on it for anything not visible here. Do not claim a "
    "provision is absent merely because it is not shown; lower your confidence "
    "instead)"
)


# ── Tokenizer (graceful fallback) ────────────────────────────────────────────
_ENC = None
_ENC_TRIED = False


def _count_tokens(text: str) -> int:
    """Token count via tiktoken (same encoding the chunker uses). Falls back to
    a chars/4 estimate if tiktoken can't load — never raises."""
    global _ENC, _ENC_TRIED
    if not _ENC_TRIED:
        _ENC_TRIED = True
        try:
            import tiktoken
            _ENC = tiktoken.get_encoding("cl100k_base")
            log.info("Token budget: using tiktoken cl100k_base")
        except Exception as e:      # offline / cache missing
            _ENC = None
            log.warning("tiktoken unavailable (%s) — using chars/4 estimate", e)
    if _ENC is None:
        return max(1, len(text) // 4)
    return len(_ENC.encode(text))


# ── Helpers ──────────────────────────────────────────────────────────────────
def _sha(s: str, n: int = 16) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:n]


def _profile_block(profile: dict) -> str:
    if not profile:
        return "(no profile provided — brief them generically for their flagged interests)"
    fields = [
        ("Title", profile.get("title")),
        ("Sector", profile.get("sector")),
        ("Functional role", profile.get("functional")),
        ("Stakeholders/clients", profile.get("stakeholders")),
        ("Describes their work as", profile.get("description")),
    ]
    lines = [f"  {label}: {val}" for label, val in fields
             if val and str(val).strip()]
    return "\n".join(lines) if lines else "(profile is empty)"


def _profile_sig(profile: dict) -> str:
    return _sha(json.dumps(profile or {}, sort_keys=True), 12)


def _flag_block(flag_rows: List[dict]) -> str:
    """Highest-severity flags first, capped in count and per-row length so a
    first-run user with 5 prompts + profile can't blow the context budget."""
    rows = sorted(flag_rows, key=lambda r: -int(r.get("rating_num", 0)))
    rows = rows[:MAX_FLAG_ROWS]
    lines = []
    for r in rows:
        lbl = r.get("prompt_label") or r.get("prompt_title") or "interest"
        expl = str(r.get("explanation", "")).strip()[:FLAG_EXPL_CHARS]
        lines.append(f'  - [{r.get("rating_label","?")}] {lbl}: {expl}')
    return "\n".join(lines) if lines else "  - (flagged by profile relevance)"


def _triage_sig(flag_rows: List[dict]) -> str:
    pairs = sorted((str(r.get("prompt_id", "")), int(r.get("rating_num", 0)))
                   for r in flag_rows)
    return _sha(json.dumps(pairs), 12)


def _features_block(row: Optional[pd.Series]) -> str:
    if row is None:
        return "(no extracted features available)"
    parts = []
    for col, label, cap in FEATURE_COLS:
        val = str(row.get(col, "") or "").strip()
        if val and val.lower() not in ("nan", "none"):
            parts.append(f"{label}: {val[:cap]}")
    return "\n".join(parts) if parts else "(no extracted features available)"


def _reassemble_text(chunks_for_bill: pd.DataFrame,
                     char_cap: int) -> Tuple[str, str, bool, int, int]:
    """Rebuild a bill's text from its chunks.

    Whole bill if it fits. Otherwise HEAD + TAIL sampling with an elision
    marker — mirrors the chunker's handle_massive_bill(first_n/last_n) idea,
    applied at read time. Reading only the head would yield short title +
    definitions and miss operative sections, effective dates, appropriations.

    Returns (text, content_hash, truncated, chunks_kept, chunks_total).
    """
    if chunks_for_bill.empty:
        return "", "", False, 0, 0

    df = chunks_for_bill.sort_values("chunk_index")
    chash = str(df.iloc[0].get("content_hash", "") or "")
    texts = [str(t) for t in df["chunk_text"]]
    n_total = len(texts)

    full_len = sum(len(t) for t in texts) + max(0, n_total - 1)
    if full_len <= char_cap:
        return "\n".join(texts).strip(), chash, False, n_total, n_total

    # Split the budget between head and tail (minus the elision marker).
    usable = max(0, char_cap - len(ELISION))
    head_budget = usable // 2
    tail_budget = usable - head_budget

    # A single chunk is ~1200 target tokens PLUS up to 200 overlap on each
    # side, which in practice runs 6,000-7,500 chars — larger than half of a
    # 12,000-char budget. So a plain "fill until full" loop takes ZERO head
    # chunks and the excerpt degenerates to tail-only. Guarantee the first and
    # last chunk are always present, char-truncating them if oversized, then
    # fill any remaining space with their neighbours.
    if n_total == 1:
        return texts[0][:char_cap].strip(), chash, True, 1, n_total

    head = [texts[0][:head_budget]]
    used = len(head[0])
    i = 1
    while i < n_total - 1 and used + len(texts[i]) <= head_budget:
        head.append(texts[i]); used += len(texts[i]); i += 1

    tail = [texts[-1][:tail_budget]]
    used_t = len(tail[0])
    j = n_total - 2
    while j >= i and used_t + len(texts[j]) <= tail_budget:
        tail.append(texts[j]); used_t += len(texts[j]); j -= 1
    tail.reverse()

    kept = len(head) + len(tail)
    text = ("\n".join(head) + ELISION + "\n".join(tail)).strip()
    return text, chash, True, kept, n_total


def _fit_to_budget(prompt: str, bill_text: str, rebuild) -> Tuple[str, int, bool]:
    """Guarantee prompt + reserved output <= MAX_MODEL_LEN.

    Char caps alone cannot promise this: a verbose features block plus five
    flag explanations plus a 1000-char profile can push a request over the
    line. Measure, then trim the bill text (the only expendable part) until
    it fits. `rebuild(new_text)` re-renders the prompt.

    Returns (prompt, token_count, trimmed).
    """
    limit = MAX_MODEL_LEN - OUTPUT_RESERVE - BUDGET_PAD
    n = _count_tokens(prompt)
    if n <= limit or not bill_text:
        return prompt, n, False

    trimmed_text = bill_text
    for _ in range(6):                       # geometric backoff, bounded
        over = n - limit
        # ~4 chars/token, shave 20% extra so we converge quickly
        cut = int(over * 4 * 1.2)
        if cut >= len(trimmed_text):
            trimmed_text = ""
            break
        trimmed_text = trimmed_text[: len(trimmed_text) - cut]
        prompt = rebuild(trimmed_text)
        n = _count_tokens(prompt)
        if n <= limit:
            return prompt, n, True
    prompt = rebuild(trimmed_text)
    return prompt, _count_tokens(prompt), True


def _extract_json(text: str) -> Optional[dict]:
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


def _norm_impact_type(v: str) -> str:
    v = (v or "").strip().lower()
    for t in IMPACT_TYPES:
        if t in v or v in t:
            return t
    return "informational"


# ── Investigation core ───────────────────────────────────────────────────────
_counter_lock = threading.Lock()
_counters = {"calls": 0, "parse_failures": 0, "truncated": 0, "budget_trimmed": 0,
             "evidence_unverified": 0}


def _bump(key: str, n: int = 1):
    with _counter_lock:
        _counters[key] += n


def investigate_one(job: dict) -> Optional[dict]:
    """One deep-dive vLLM call for a (user, bill)."""
    note = EXCERPT_NOTE if job["truncated"] else ""

    def _render(bill_text: str) -> str:
        return PROMPT_TEMPLATE.format(
            profile_block=_profile_block(job["profile"]),
            flag_block=_flag_block(job["flag_rows"]),
            features_block=_features_block(job["features_row"]),
            bill_text=bill_text or "(bill text unavailable)",
            excerpt_note=note,
            impact_types=json.dumps(IMPACT_TYPES),
        )

    prompt = _render(job["bill_text"])
    prompt, n_tok, trimmed = _fit_to_budget(prompt, job["bill_text"], _render)
    if trimmed:
        _bump("budget_trimmed")
        log.warning("Budget trim: user=%s bill=%s -> %d tokens",
                    job["user_id"], job["bill_id"], n_tok)

    text, _, err = run_vllm(VLLM_CFG, prompt, system_prompt=SYSTEM_PROMPT)
    _bump("calls")
    obj = _extract_json(text) if err is None else None
    if not obj:
        _bump("parse_failures")
        log.warning("Uninvestigable: user=%s bill=%s (%s)",
                    job["user_id"], job["bill_id"], err or "parse failure")
        # A silent parse failure is an investigation the user never sees and
        # never knows is missing. Log the raw response so the JSON contract can
        # be fixed rather than guessed at.
        if os.environ.get("INVESTIGATE_DEBUG_RAW", "").lower() in ("1", "true"):
            _t = text or ""
            # The TAIL is what matters: a response cut off by max_tokens ends
            # mid-token with no closing brace. One that ends cleanly failed for
            # a different reason (bad escape, trailing prose, nested fence).
            log.warning("RAW[bill=%s] len=%d closes=%s TAIL=%r",
                        job["bill_id"], len(_t),
                        _t.rstrip().endswith("}"), _t[-300:])
            # The response is well-formed at both ends, so json.loads is failing
            # on something in the middle. Report the exact offset and its context.
            _s, _e = _t.find("{"), _t.rfind("}")
            if _s != -1 and _e > _s:
                try:
                    json.loads(_t[_s:_e + 1])
                except json.JSONDecodeError as _je:
                    _abs = _s + _je.pos
                    log.warning("JSONERR[bill=%s]: %s\n  ...%r...",
                                job["bill_id"], _je,
                                _t[max(0, _abs - 90):_abs + 90])
        return None

    # ── Evidence verification ────────────────────────────────────────────────
    # Asked whether a bill *could* affect them, a language model will construct a
    # plausible chain of consequences for almost any bill — the question has no
    # wrong answer. Requiring a verbatim citation, and checking it against the
    # source, makes the claim falsifiable. An unverifiable finding is still
    # written (we do not silently drop it) but is flagged so the app can demote it
    # and so precision can be measured.
    _ev = str(obj.get("evidence", "") or "")
    _ev = _ev.replace("\\", " ").replace('"', " ")   # never round-trip a quote
    _ev = " ".join(_ev.split()).strip()[:400]
    # Verification tiers. Spot-checking showed the model is faithful to the source
    # but often smooths or stitches — e.g. writing "The Illinois Procurement Code"
    # where the section reads "This Code", or joining a header total to its clause.
    # A pure substring test wrongly fails those. So: try exact first (still the
    # strongest signal), then fall back to token-overlap, which passes a quote whose
    # words appear in the bill in near-contiguous order. verify_mode records which.
    _ev_ok = False
    _verify_mode = "none"
    if _ev:
        _norm = lambda s: " ".join(s.split()).lower()
        _bt = _norm(job["bill_text"])
        _evn = _norm(_ev)
        if _evn in _bt:
            _ev_ok, _verify_mode = True, "exact"
        else:
            # token-overlap: fraction of the quote's words that appear in the bill,
            # requiring a run of consecutive matches so we don't pass a bag of
            # common words. Threshold 0.85 chosen against the spot-check cases.
            _q = _evn.split()
            if len(_q) >= 6:
                _btset = set(_bt.split())
                _hits = sum(1 for w in _q if w in _btset)
                _frac = _hits / len(_q)
                # longest consecutive run of quote-words found in the bill text
                _run = _best = 0
                for w in _q:
                    if w in _btset:
                        _run += 1; _best = max(_best, _run)
                    else:
                        _run = 0
                if _frac >= 0.85 and _best >= max(5, int(0.6 * len(_q))):
                    _ev_ok, _verify_mode = True, "fuzzy"
    if _ev and not _ev_ok:
        _bump("evidence_unverified")

    _deo = str(obj.get("deontic", "")).strip().lower()
    if _deo not in ("obligation", "permission", "prohibition",
                    "recommendation", "none"):
        _deo = "none"

    itype = _norm_impact_type(str(obj.get("impact_type", "")))
    actions = obj.get("recommended_actions", [])
    if isinstance(actions, str):
        actions = [actions]
    swot = obj.get("swot")
    if itype not in SWOT_IMPACT_TYPES or not isinstance(swot, dict):
        swot = None

    # The model sometimes returns a sentence where a phrase was asked for.
    _tl = str(obj.get("timeline", "unknown")).strip()
    if len(_tl) > 60 or _tl.count(" ") > 9:
        _tl = _tl.split(",")[0].split(".")[0].strip()[:60] or "unknown"

    return {
        "user_id":           job["user_id"],
        "bill_id":           str(job["bill_id"]),
        "bill_number":       job["bill_number"],
        "session_id":        int(job["session_id"]),
        "headline":          str(obj.get("headline", "")).strip()[:400],
        "impact_type":       itype,
        "directness":        ("direct" if "direct" ==
                              str(obj.get("directness", "")).strip().lower()
                              else "indirect"),
        "evidence":          _ev,
        "evidence_verified": bool(_ev_ok),
        "verify_mode":       _verify_mode,
        "deontic":           _deo,
        "what_changes":      str(obj.get("what_changes", "")).strip()[:1200],
        "recommended_actions": json.dumps(
                                 [str(a).strip()[:200] for a in actions][:4]),
        "who_it_affects":    str(obj.get("who_it_affects", "")).strip()[:600],
        "timeline":          _tl[:120],
        "confidence":        str(obj.get("confidence", "")).strip()[:20],
        "confidence_reason": str(obj.get("confidence_reason", "")).strip()[:300],
        "swot":              json.dumps(swot) if swot else "",
        "flagged_by":        json.dumps(
                                 [r.get("prompt_id") for r in job["flag_rows"]]),
        "max_rating_num":    job["max_rating"],
        "text_truncated":    bool(job["truncated"]),
        "chunks_kept":       int(job["chunks_kept"]),
        "chunks_total":      int(job["chunks_total"]),
        "prompt_tokens":     int(n_tok),
        "content_hash":      job["content_hash"],
        "triage_sig":        job["triage_sig"],
        "profile_sig":       job["profile_sig"],
        "investigated_at":   datetime.now(timezone.utc).isoformat(),
    }


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="Deep-dive investigation of flagged bills")
    ap.add_argument("--force",      action="store_true",
                    help="re-investigate every flagged bill, ignoring the "
                         "(content_hash, triage_sig, profile_sig) delta. Required "
                         "after a prompt change: none of those keys move when the "
                         "prompt template does, so a plain re-run silently skips.")
    ap.add_argument("--dry-run",    action="store_true", help="build but write nothing")
    ap.add_argument("--workers",    type=int, default=int(os.environ.get("INVESTIGATE_WORKERS", "6")))
    ap.add_argument("--limit",      type=int, default=int(os.environ.get("INVESTIGATE_MAX_PER_RUN", "0")),
                    help="cap investigations this run; 0 = unlimited")
    ap.add_argument("--min-rating", type=int, default=MIN_RATING,
                    help="min severity (0-3) to investigate; default 2 (Moderate+)")
    ap.add_argument("--checkpoint-every", type=int, default=CHECKPOINT_EVERY,
                    help="flush results to blob every N investigations (0 = only at end)")
    args = ap.parse_args()
    t0 = time.time()

    ok, msg = health_check(VLLM_CFG)
    if not ok:
        log.error("vLLM health check failed: %s", msg)
        return 1
    log.info("vLLM: %s", msg)
    log.info("Context budget: max_model_len=%d reserve=%d pad=%d -> prompt limit %d tok",
             MAX_MODEL_LEN, OUTPUT_RESERVE, BUDGET_PAD,
             MAX_MODEL_LEN - OUTPUT_RESERVE - BUDGET_PAD)

    blob = BlobClient()

    # 1. Stage-2 scores
    try:
        scores = blob.read_parquet(TIER_GOLD, RESULTS_PATH)
    except FileNotFoundError:
        log.info("No impact_results at gold/%s — nothing to investigate. "
                 "(Has Step 7 run yet?)", RESULTS_PATH)
        return 0
    if scores.empty:
        log.info("impact_results is empty — nothing to investigate.")
        return 0
    scores["bill_id"] = scores["bill_id"].astype(str)

    # 2. Profiles
    try:
        profiles = blob.read_json(TIER_GOLD, PROFILES_PATH).get("profiles", {})
    except FileNotFoundError:
        profiles = {}
    log.info("Loaded %d user profile(s)", len(profiles))

    # 3. Existing investigations (delta + checkpoint base)
    try:
        existing = blob.read_parquet(TIER_GOLD, INVEST_PATH)
    except FileNotFoundError:
        existing = pd.DataFrame()
    done: Dict[Tuple[str, str], Tuple[str, str, str]] = {}
    if not existing.empty:
        existing["bill_id"] = existing["bill_id"].astype(str)
        for r in existing.itertuples(index=False):
            done[(r.user_id, str(r.bill_id))] = (
                getattr(r, "content_hash", ""), getattr(r, "triage_sig", ""),
                getattr(r, "profile_sig", ""))

    # 4. Flagged (user, bill) groups
    flagged = scores[scores["rating_num"] >= args.min_rating]
    if flagged.empty:
        log.info("No bills at rating >= %d — nothing to investigate.", args.min_rating)
        return 0
    log.info("Flagged rows at rating >= %d: %d", args.min_rating, len(flagged))

    # 5. Lazy per-session loads
    feats_by_session: Dict[int, Optional[pd.DataFrame]] = {}
    chunks_by_session: Dict[int, Optional[pd.DataFrame]] = {}

    def _feats(sid: int) -> Optional[pd.DataFrame]:
        if sid not in feats_by_session:
            try:
                df = blob.read_parquet(TIER_GOLD, FEATURES_PATH_T.format(session=sid))
                df["bill_id"] = df["bill_id"].astype(str)
                feats_by_session[sid] = df.drop_duplicates("bill_id", keep="last")
            except FileNotFoundError:
                feats_by_session[sid] = None
        return feats_by_session[sid]

    def _chunks(sid: int) -> Optional[pd.DataFrame]:
        if sid not in chunks_by_session:
            try:
                df = blob.read_parquet(TIER_SILVER, CHUNKS_PATH_T.format(session=sid))
                df["bill_id"] = df["bill_id"].astype(str)
                chunks_by_session[sid] = df
            except FileNotFoundError:
                chunks_by_session[sid] = None
        return chunks_by_session[sid]

    # 6. Build jobs (apply delta)
    jobs: List[dict] = []
    budget = args.limit if args.limit > 0 else 10**12
    skipped = 0
    for (uid, bid), grp in flagged.groupby(["user_id", "bill_id"]):
        if len(jobs) >= budget:
            break
        flag_rows = grp.to_dict("records")
        sid = int(grp.iloc[0].get("session_id", ACTIVE_SESSIONS[0]))
        profile = profiles.get(uid, {})

        cdf = _chunks(sid)
        bill_chunks = (cdf[cdf["bill_id"] == bid] if cdf is not None
                       else pd.DataFrame())
        bill_text, content_hash, truncated, kept, total = _reassemble_text(
            bill_chunks, TEXT_CHAR_CAP)

        tri = _triage_sig(flag_rows)
        psig = _profile_sig(profile)
        if not args.force and done.get((uid, bid)) == (content_hash, tri, psig):
            skipped += 1
            continue

        if truncated:
            _bump("truncated")
            log.info("Truncated bill %s: kept %d/%d chunks (head+tail)",
                     bid, kept, total)

        fdf = _feats(sid)
        frow = None
        if fdf is not None:
            hit = fdf[fdf["bill_id"] == bid]
            if not hit.empty:
                frow = hit.iloc[0]

        jobs.append({
            "user_id": uid, "bill_id": bid,
            "bill_number": str(grp.iloc[0].get("bill_number", "") or ""),
            "session_id": sid, "profile": profile, "flag_rows": flag_rows,
            "features_row": frow, "bill_text": bill_text,
            "truncated": truncated, "chunks_kept": kept, "chunks_total": total,
            "content_hash": content_hash, "triage_sig": tri, "profile_sig": psig,
            "max_rating": int(grp["rating_num"].max()),
        })

    log.info("Investigations to run: %d (skipped %d unchanged, %d truncated)",
             len(jobs), skipped, _counters["truncated"])
    if not jobs:
        log.info("Everything up to date — done in %.1fs", time.time() - t0)
        return 0

    # Base frame that survives this run: prior rows for pairs we are NOT rewriting.
    rewriting = {(j["user_id"], j["bill_id"]) for j in jobs}
    if not existing.empty:
        keep_mask = ~existing.apply(
            lambda r: (r["user_id"], str(r["bill_id"])) in rewriting, axis=1)
        base = existing[keep_mask].copy()
    else:
        base = pd.DataFrame()

    new_rows: List[dict] = []
    write_lock = threading.Lock()

    def _flush(tag: str) -> None:
        if args.dry_run or not new_rows:
            return
        merged = (pd.concat([base, pd.DataFrame(new_rows)], ignore_index=True)
                  if not base.empty else pd.DataFrame(new_rows))
        blob.write_parquet(merged, TIER_GOLD, INVEST_PATH)
        log.info("Checkpoint (%s): wrote %d rows (%d new) -> gold/%s",
                 tag, len(merged), len(new_rows), INVEST_PATH)

    # 7. Run concurrently, checkpointing as we go
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(investigate_one, j) for j in jobs]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                row = fut.result()
                if row:
                    with write_lock:
                        new_rows.append(row)
                        if (args.checkpoint_every > 0
                                and len(new_rows) % args.checkpoint_every == 0):
                            try:
                                _flush(f"{len(new_rows)} done")
                            except Exception as e:
                                log.warning("Checkpoint write failed "
                                            "(continuing): %s", e)
            except Exception as e:
                log.error("Investigation task failed: %s", e)
            if i % 25 == 0 or i == len(futures):
                log.info("  progress: %d/%d (%d written, %d parse-fail, %d trimmed)",
                         i, len(futures), len(new_rows),
                         _counters["parse_failures"], _counters["budget_trimmed"])

    # 8. Final write
    new_df = pd.DataFrame(new_rows)
    merged = (pd.concat([base, new_df], ignore_index=True)
              if not new_df.empty else base)

    elapsed = time.time() - t0
    log.info("Investigated %d (calls=%d parse_fail=%d truncated=%d trimmed=%d) "
             "in %.1f min | total rows=%d",
             len(new_df), _counters["calls"], _counters["parse_failures"],
             _counters["truncated"], _counters["budget_trimmed"],
             elapsed / 60, len(merged))

    if args.dry_run:
        log.info("[DRY RUN] Not writing. Sample:\n%s",
                 new_df.head(5).to_string() if not new_df.empty else "(none)")
        return 0

    blob.write_parquet(merged, TIER_GOLD, INVEST_PATH)
    log.info("Wrote gold/%s", INVEST_PATH)

    # 9. Stamp pipeline log (non-fatal)
    if os.environ.get("IMPACT_SKIP_PIPELINE_LOG", "").lower() not in ("1", "true"):
        try:
            from utils.pipeline_logger import PipelineLogger
            pl = PipelineLogger(workflow_name="bill_investigator",
                                log_dir=PIPELINE_DIR / "logs",
                                pipeline_version=datetime.now().strftime("%Y-%m-%d"))
            with pl.pipeline_run():
                with pl.stage("investigate_flagged_bills", order=1):
                    pl.set_stage_metadata(
                        input_count=len(jobs),
                        output_count=len(new_df),
                        rows_written=len(merged),
                        output_file=f"gold/{INVEST_PATH}",
                        output_file_written=True,
                        extra={"vllm_calls": _counters["calls"],
                               "parse_failures": _counters["parse_failures"],
                               "truncated": _counters["truncated"],
                               "budget_trimmed": _counters["budget_trimmed"],
                               "min_rating": args.min_rating,
                               "elapsed_seconds": round(elapsed, 1)},
                    )
        except Exception as e:
            log.warning("Pipeline-log stamping failed (non-fatal): %s", e)

    return 0


if __name__ == "__main__":
    sys.exit(main())
