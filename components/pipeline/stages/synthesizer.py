"""Stage 3.5: Synthesizer — Multi-Chunk Bill Feature Consolidation.

Recursive meta-extraction for large bills
==========================================
Bills with >7 chunk outputs exceed the synthesizer's 7,000-token context
budget. For these bills, we run a recursive meta-extraction pass:

  Round 1: Group chunk outputs into batches of 6 (with 1-chunk overlap)
           Run one vLLM call per batch → produces 1 compressed output per batch
           Now have ceil(n/5) meta-chunks instead of n chunks
  Round 2: If still >7 meta-chunks, recurse (handles 50+ chunk bills)
  Final:   Synthesize from <=7 meta-chunks → single authoritative row

This mirrors the chunker's adaptive divisor logic:
  - target=6 outputs per batch (matching 1200-token chunk philosophy)
  - overlap=1 output between batches (matching 200-token overlap philosophy)
  - floor/ceiling enforced so no batch is too small or too large

concurrency=N: set in configs/synthesizer.yaml (recommend 12)
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from services.azure_blob_service import BlobClient, TIER_SILVER
from utils.timing import StageTimer

logger = logging.getLogger(__name__)

_PIPELINE_LOG_DIR = Path(
    __import__("os").environ.get('LEG_COMPONENTS_PIPELINE_STAGES_SYNTHESIZER_PY__PIPELINE_LOG_DIR_L41', "")
)

# ── Synthesizer context budget ────────────────────────────────────────────────
# Model max: 8,192 tokens
# Prompt template overhead: ~200 tokens
# Safety margin: 500 tokens
# Available for chunk content: 7,500 tokens
# Each chunk output: ~900 tokens (15 fields × ~60 tokens avg)
# Max chunk outputs that fit: 7,500 / 900 = ~8, use 7 to be safe
MAX_CHUNKS_DIRECT = 7       # synthesize directly if n_chunks <= this
META_BATCH_SIZE   = 6       # chunk outputs per meta-extraction batch
META_OVERLAP      = 1       # overlap between batches (mirrors chunker)


# ---------------------------------------------------------------------------
# Silver path helper
# ---------------------------------------------------------------------------

def silver_synthesized_session_path(session_id: int, profile=None) -> str:
    prof = profile or "default"
    return (
        f"legislation/features_synthesized/{prof}"
        f"/session={session_id}/_synthesized.parquet"
    )


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

SYNTH_COLS = [
    "bill_id", "session_id", "bill_number",
    "n_chunks", "n_chunks_ok", "n_chunks_partial",
    "synthesized_at", "profile", "synthesis_method",
    "llama_summary", "legislative_goal", "policy_domain", "key_provisions",
    "intended_beneficiaries", "legislative_strategy", "increasing_aspects",
    "decreasing_aspects", "category_subcategory", "ideological_alignment",
    "potential_impact", "impact_rating", "intent", "motivation", "original_law",
]

_FIELD_LABELS = [
    ("llama_summary",          "Llama Summary"),
    ("legislative_goal",       "Legislative Goal"),
    ("policy_domain",          "Policy Domain"),
    ("key_provisions",         "Key Provisions"),
    ("intended_beneficiaries", "Intended Beneficiaries"),
    ("legislative_strategy",   "Legislative Strategy"),
    ("increasing_aspects",     "Increasing Aspects"),
    ("decreasing_aspects",     "Decreasing Aspects"),
    ("category_subcategory",   "Category & Subcategory"),
    ("ideological_alignment",  "Ideological Alignment"),
    ("potential_impact",       "Potential Impact"),
    ("impact_rating",          "Impact Rating"),
    ("intent",                 "Intent"),
    ("motivation",             "Motivation"),
    ("original_law",           "Original Law"),
]

_PROMPT_TEMPLATE: Optional[str] = None


def _load_prompt(prompt_path: str) -> str:
    global _PROMPT_TEMPLATE
    if _PROMPT_TEMPLATE is None:
        with open(prompt_path, "r", encoding="utf-8") as f:
            _PROMPT_TEMPLATE = f.read()
    return _PROMPT_TEMPLATE


# ---------------------------------------------------------------------------
# vLLM call
# ---------------------------------------------------------------------------

def _call_vllm(prompt: str, vllm_url: str, model_name: str,
               timeout: int = 120, max_tokens: int = 900) -> str:
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    try:
        resp = requests.post(
            f"{vllm_url}/v1/chat/completions",
            json=payload, timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning("vLLM call failed: %s", e)
        return ""


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------

def _parse_response(text: str) -> dict:
    result = {}
    for col, label in _FIELD_LABELS:
        all_labels = [lbl for _, lbl in _FIELD_LABELS]
        boundary = "|".join(re.escape(lbl + ":") for lbl in all_labels)
        pattern = re.compile(
            rf"^{re.escape(label)}:\s*(.+?)(?=(?:{boundary})|$)",
            re.IGNORECASE | re.MULTILINE | re.DOTALL,
        )
        m = pattern.search(text)
        if m:
            val = re.sub(r"\s+", " ", m.group(1).strip()).strip()
            result[col] = val if val else "None"
        else:
            result[col] = ""
    return result


# ---------------------------------------------------------------------------
# Ideology normalizer
# ---------------------------------------------------------------------------

_IDEO_ENUM = {"Left", "Right", "Center", "Nonpartisan", "Mixed", "Unspecified"}

def _normalize_ideology(value: str) -> str:
    if not value: return "Unspecified"
    s = str(value).strip()
    if s in _IDEO_ENUM: return s
    sl = s.lower()
    has_left  = any(k in sl for k in ["liberal","progressive","democrat"])
    has_right = any(k in sl for k in ["conservative","republican"])
    if has_left and has_right: return "Mixed"
    if has_left:  return "Left"
    if has_right: return "Right"
    if any(k in sl for k in ["bipartisan","nonpartisan","neutral","no clear","no ideolog"]): return "Nonpartisan"
    if any(k in sl for k in ["moderate","centrist"]): return "Center"
    return "Unspecified"


# ---------------------------------------------------------------------------
# Passthrough normalizer
# ---------------------------------------------------------------------------

def _normalize_passthrough(col: str, value) -> str:
    if not value: return ""
    import re as _re
    s = str(value).strip()
    if not s or s.lower() == "nan": return ""
    lines = s.splitlines()
    bullet_lines = [l.strip() for l in lines if _re.match(r"^[-*•]\s+", l.strip())]
    if len(bullet_lines) >= 1:
        items = [_re.sub(r"^[-*•]\s+", "", l).strip() for l in bullet_lines]
        items = [i for i in items if i and i.lower() not in ("none","n/a","na")]
        if not items: return "None"
        return "; ".join(items) if col in ("increasing_aspects","decreasing_aspects") else ", ".join(items)
    return s


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _build_section_analyses(chunk_rows: list, fields=None) -> str:
    """Build the section analyses block for the synthesis prompt.
    Uses all 15 fields — no truncation needed because recursive meta-extraction
    ensures n_chunks <= MAX_CHUNKS_DIRECT before this is called.
    """
    if fields is None:
        fields = _FIELD_LABELS
    n = len(chunk_rows)
    parts = []
    for i, row in enumerate(chunk_rows, start=1):
        lines = [f"=== Section {i} of {n} ==="]
        for col, label in fields:
            val = row.get(col, "None")
            if pd.isna(val) or str(val).strip() == "":
                val = "None"
            lines.append(f"{label}: {val}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _build_synthesis_prompt(bill_number: str, n_chunks: int,
                             chunk_rows: list, prompt_template: str) -> str:
    return prompt_template.format(
        bill_number=bill_number,
        n_chunks=n_chunks,
        section_analyses=_build_section_analyses(chunk_rows),
    )


def _build_meta_prompt(bill_number: str, batch_rows: list,
                        batch_num: int, total_batches: int) -> str:
    """Build a meta-extraction prompt that compresses N chunk outputs into 1.
    Uses the same 15-field output format so the result feeds naturally into
    the next round or final synthesis.
    """
    section_text = _build_section_analyses(batch_rows)
    return f"""You are consolidating {len(batch_rows)} section analyses of bill {bill_number} \
(batch {batch_num} of {total_batches}) into a single unified analysis.

Synthesize all sections into ONE coherent response using these exact field labels:

{section_text}

Produce a single unified response with all 15 fields. Merge, deduplicate, and \
resolve contradictions. Output format — each field on its own line:
Llama Summary: <unified summary>
Legislative Goal: <unified goal>
Policy Domain: <domain>
Key Provisions: <all key provisions combined>
Intended Beneficiaries: <all groups, comma-separated>
Legislative Strategy: <strategy>
Increasing Aspects: <semicolon-separated>
Decreasing Aspects: <semicolon-separated>
Category & Subcategory: <category>
Ideological Alignment: <Left|Right|Center|Nonpartisan|Mixed|Unspecified>
Potential Impact: <impact description>
Impact Rating: <Low|Medium|High|Transformative>
Intent: <intent>
Motivation: <motivation>
Original Law: <law being amended or None>"""


# ---------------------------------------------------------------------------
# Recursive meta-extraction
# ---------------------------------------------------------------------------

def _meta_extract_chunks(
    bill_number: str,
    chunk_rows: list,
    vllm_url: str,
    model_name: str,
    vllm_timeout: int,
    depth: int = 0,
) -> list:
    """
    Recursively compress chunk_rows until len <= MAX_CHUNKS_DIRECT.

    Each recursive call groups chunk_rows into batches of META_BATCH_SIZE
    with META_OVERLAP overlap, runs one vLLM call per batch, and returns
    a compressed list of row dicts in the same format as the original
    chunk_rows (same 15 fields).

    Depth limit of 3 prevents infinite recursion for pathological cases.
    """
    n = len(chunk_rows)
    if n <= MAX_CHUNKS_DIRECT or depth >= 3:
        return chunk_rows

    logger.info(
        "Bill %s: meta-extraction round %d — compressing %d chunks → batches of %d",
        bill_number, depth + 1, n, META_BATCH_SIZE,
    )

    # Build batches with overlap
    # Step size = META_BATCH_SIZE - META_OVERLAP so adjacent batches share
    # META_OVERLAP chunks (mirrors the chunker's overlap philosophy)
    step = META_BATCH_SIZE - META_OVERLAP
    batches = []
    i = 0
    while i < n:
        batch = chunk_rows[i: i + META_BATCH_SIZE]
        batches.append(batch)
        i += step
        if i >= n:
            break

    total_batches = len(batches)
    meta_rows = []

    for batch_num, batch in enumerate(batches, start=1):
        prompt = _build_meta_prompt(
            bill_number, batch, batch_num, total_batches
        )
        raw = _call_vllm(prompt, vllm_url, model_name, timeout=vllm_timeout)
        if raw:
            parsed = _parse_response(raw)
            parsed["ideological_alignment"] = _normalize_ideology(
                parsed.get("ideological_alignment", "")
            )
            # Mark as meta-extracted so we can track provenance
            parsed["parse_status"] = "OK"
            parsed["chunk_index"]  = batch_num
            meta_rows.append(parsed)
        else:
            # Fallback: use first row of batch as representative
            logger.warning(
                "Bill %s: meta-extraction batch %d/%d failed, using first chunk",
                bill_number, batch_num, total_batches,
            )
            meta_rows.append(batch[0])

    logger.info(
        "Bill %s: meta-extraction round %d complete — %d chunks → %d meta-chunks",
        bill_number, depth + 1, n, len(meta_rows),
    )

    # Recurse if still too many
    return _meta_extract_chunks(
        bill_number, meta_rows, vllm_url, model_name, vllm_timeout, depth + 1
    )


# ---------------------------------------------------------------------------
# Per-bill synthesis (thread-safe)
# ---------------------------------------------------------------------------

def synthesize_bill(
    bill_id: str,
    bill_number: str,
    chunk_rows: list,
    prompt_template: str,
    vllm_url: str,
    model_name: str,
    vllm_timeout: int = 120,
) -> dict:
    """
    Produce a single synthesized row for one bill.

    Single-chunk:        passthrough (no vLLM call)
    2-7 chunks:          direct synthesis (1 vLLM call)
    8+ chunks:           recursive meta-extraction then synthesis
    """
    n_total   = len(chunk_rows)
    n_ok      = sum(1 for r in chunk_rows if r.get("parse_status") == "OK")
    n_partial = sum(1 for r in chunk_rows if r.get("parse_status") == "PARTIAL")
    now_ts    = datetime.utcnow().isoformat(timespec="seconds")

    base = {
        "bill_id":          bill_id,
        "session_id":       chunk_rows[0].get("session_id"),
        "bill_number":      bill_number,
        "n_chunks":         n_total,
        "n_chunks_ok":      n_ok,
        "n_chunks_partial": n_partial,
        "synthesized_at":   now_ts,
    }

    # ── Single-chunk passthrough ──────────────────────────────────────────
    if n_total == 1:
        row = chunk_rows[0]
        result = {col: _normalize_passthrough(col, row.get(col, ""))
                  for col, _ in _FIELD_LABELS}
        result["ideological_alignment"] = _normalize_ideology(
            result.get("ideological_alignment", "")
        )
        result.update(base)
        result["synthesis_method"] = "passthrough"
        return {c: result.get(c) for c in SYNTH_COLS}

    # ── Recursive meta-extraction for large bills ─────────────────────────
    synthesis_method_prefix = f"synthesized_{n_total}_chunks"
    if n_total > MAX_CHUNKS_DIRECT:
        chunk_rows = _meta_extract_chunks(
            bill_number, chunk_rows, vllm_url, model_name, vllm_timeout
        )
        synthesis_method_prefix = (
            f"meta_extracted_{n_total}_to_{len(chunk_rows)}_chunks"
        )
        logger.info(
            "Bill %s: meta-extraction complete, synthesizing from %d meta-chunks",
            bill_number, len(chunk_rows),
        )

    # ── Synthesis (direct or post-meta) ──────────────────────────────────
    prompt = _build_synthesis_prompt(
        bill_number=bill_number,
        n_chunks=len(chunk_rows),
        chunk_rows=chunk_rows,
        prompt_template=prompt_template,
    )

    raw_response = _call_vllm(
        prompt, vllm_url, model_name, timeout=vllm_timeout
    )

    if not raw_response:
        logger.warning(
            "Bill %s: synthesis call failed, falling back to first chunk",
            bill_number,
        )
        row = chunk_rows[0]
        result = {col: row.get(col, "") for col, _ in _FIELD_LABELS}
        result.update(base)
        result["synthesis_method"] = "fallback_first_chunk"
        return {c: result.get(c) for c in SYNTH_COLS}

    parsed = _parse_response(raw_response)
    parsed["ideological_alignment"] = _normalize_ideology(
        parsed.get("ideological_alignment", "")
    )
    result = {**base, **parsed}
    result["synthesis_method"] = synthesis_method_prefix
    return {c: result.get(c) for c in SYNTH_COLS}


# ---------------------------------------------------------------------------
# Session driver
# ---------------------------------------------------------------------------

def _run_session(session_id: int, cfg: dict, blob: BlobClient,
                 dry: bool = False) -> dict:
    timer        = StageTimer()
    profile      = cfg.get("profile") or None
    vllm_url     = cfg.get("vllm_url", "http://localhost:8000")
    model_name   = cfg.get("model_name", "llama31-8b-awq")
    vllm_timeout = cfg.get("vllm_timeout_seconds", 120)
    bill_limit   = cfg.get("bill_limit")
    concurrency  = max(1, int(cfg.get("concurrency", 1)))
    prompt_path  = cfg.get("synthesis_prompt_path", "prompts/synthesis_prompt_v1.txt")

    result = {
        "session_id": session_id, "profile": profile, "status": "SUCCESS",
        "bills_input": 0, "bills_passthrough": 0, "bills_synthesized": 0,
        "bills_meta_extracted": 0, "bills_failed": 0,
        "skip_reason": None, "error": None, "metrics": None,
    }

    try:
        from utils.pipeline_logger import PipelineLogger
        pl = PipelineLogger(
            workflow_name="legislation_pipeline",
            log_dir=_PIPELINE_LOG_DIR,
            pipeline_version=cfg.get("pipeline_version", "unknown"),
        )
        _log_ok = True
    except Exception as _le:
        logger.warning("PipelineLogger unavailable (%s)", _le)
        pl = None; _log_ok = False

    def _do_run():
        try:
            prompt_template = _load_prompt(prompt_path)
        except FileNotFoundError:
            result["status"] = "FAILED"
            result["error"]  = f"Prompt not found: {prompt_path}"
            result["metrics"] = timer.snapshot()
            return

        with timer.phase("read_silver_features"):
            feat_profile  = cfg.get("features_profile") or profile or "llama31_1200"
            features_path = (
                f"legislation/features_{feat_profile}"
                f"/session={session_id}/_features.parquet"
            )
            if not blob.blob_exists(TIER_SILVER, features_path):
                result["status"] = "SKIPPED"
                result["skip_reason"] = "no_silver_features"
                result["metrics"] = timer.snapshot()
                return
            df = blob.read_parquet(TIER_SILVER, features_path)
            logger.info("Loaded %d feature rows for session=%d", len(df), session_id)

        df_valid = df[df["parse_status"].isin(["OK","PARTIAL"])].copy()
        if df_valid.empty:
            result["status"] = "SKIPPED"
            result["skip_reason"] = "no_valid_features"
            result["metrics"] = timer.snapshot()
            return

        if "session_id" not in df_valid.columns:
            df_valid["session_id"] = session_id

        all_bill_ids = df_valid["bill_id"].astype(str).unique().tolist()
        result["bills_input"] = len(all_bill_ids)
        if bill_limit:
            all_bill_ids = all_bill_ids[:int(bill_limit)]

        with timer.phase("read_existing_synthesized"):
            synth_path = silver_synthesized_session_path(session_id, profile=profile)
            if blob.blob_exists(TIER_SILVER, synth_path):
                df_existing  = blob.read_parquet(TIER_SILVER, synth_path)
                existing_ids = set(df_existing["bill_id"].astype(str).tolist())
                before = len(all_bill_ids)
                all_bill_ids = [b for b in all_bill_ids if b not in existing_ids]
                logger.info(
                    "Skipping %d already synthesized, %d remaining",
                    before - len(all_bill_ids), len(all_bill_ids),
                )
            else:
                df_existing = pd.DataFrame()

        if not all_bill_ids:
            logger.info("All bills already synthesized for session=%d", session_id)
            result["metrics"] = timer.snapshot()
            return

        # Build self-contained work items
        work_items = []
        for bill_id in all_bill_ids:
            group = df_valid[df_valid["bill_id"].astype(str) == str(bill_id)]
            if group.empty: continue
            n_chunks = len(group)
            work_items.append((
                bill_id,
                str(group["bill_number"].iloc[0]),
                group.sort_values("chunk_index").to_dict("records"),
                n_chunks,
            ))

        # Log the chunk distribution for this run
        chunk_counts = [w[3] for w in work_items]
        large_bills  = sum(1 for c in chunk_counts if c > MAX_CHUNKS_DIRECT)
        logger.info(
            "Synthesizer starting: %d bills, concurrency=%d | "
            "%d need meta-extraction (>%d chunks), %d direct",
            len(work_items), concurrency,
            large_bills, MAX_CHUNKS_DIRECT,
            len(work_items) - large_bills,
        )

        synth_rows = []
        n_done = 0
        n_total_bills = len(work_items)
        t0 = time.perf_counter()

        def _worker(item):
            bill_id, bill_number, chunk_rows, _ = item
            return synthesize_bill(
                bill_id, bill_number, chunk_rows,
                prompt_template, vllm_url, model_name, vllm_timeout,
            )

        if concurrency == 1:
            for item in work_items:
                _, bill_number, _, _ = item
                try:
                    with timer.phase("synthesize_bill"):
                        row = _worker(item)
                    row["profile"] = profile or "default"
                    method = row.get("synthesis_method", "")
                    if "passthrough"    in method: result["bills_passthrough"] += 1
                    elif "meta"        in method: result["bills_meta_extracted"] += 1
                    else:                          result["bills_synthesized"] += 1
                    synth_rows.append(row)
                except Exception as e:
                    logger.warning("Bill %s failed: %s", bill_number, e)
                    result["bills_failed"] += 1
                n_done += 1
                if n_done % 20 == 0:
                    elapsed = time.perf_counter() - t0
                    rate = n_done / elapsed if elapsed > 0 else 0
                    eta  = (n_total_bills - n_done) / rate if rate > 0 else 0
                    logger.info(
                        "Progress: %d/%d (%.1f b/s ETA %.0fm) "
                        "pass=%d synth=%d meta=%d fail=%d",
                        n_done, n_total_bills, rate, eta / 60,
                        result["bills_passthrough"], result["bills_synthesized"],
                        result["bills_meta_extracted"], result["bills_failed"],
                    )
        else:
            with timer.phase("synthesize_all"):
                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    futures = {pool.submit(_worker, item): item for item in work_items}
                    for future in as_completed(futures):
                        _, bill_number, _, _ = futures[future]
                        try:
                            row = future.result()
                            row["profile"] = profile or "default"
                            method = row.get("synthesis_method", "")
                            if "passthrough" in method: result["bills_passthrough"] += 1
                            elif "meta"      in method: result["bills_meta_extracted"] += 1
                            else:                        result["bills_synthesized"] += 1
                            synth_rows.append(row)
                        except Exception as e:
                            logger.warning("Bill %s failed: %s", bill_number, e)
                            result["bills_failed"] += 1
                        n_done += 1
                        if n_done % 100 == 0:
                            elapsed = time.perf_counter() - t0
                            rate = n_done / elapsed if elapsed > 0 else 0
                            eta  = (n_total_bills - n_done) / rate if rate > 0 else 0
                            logger.info(
                                "Progress: %d/%d (%.1f b/s ETA %.0fm) "
                                "pass=%d synth=%d meta=%d fail=%d [conc=%d]",
                                n_done, n_total_bills, rate, eta / 60,
                                result["bills_passthrough"], result["bills_synthesized"],
                                result["bills_meta_extracted"], result["bills_failed"],
                                concurrency,
                            )

        elapsed_total = time.perf_counter() - t0
        logger.info(
            "Synthesis loop done: %d bills %.1fs (%.1f b/s) "
            "pass=%d synth=%d meta=%d fail=%d",
            n_done, elapsed_total,
            n_done / elapsed_total if elapsed_total > 0 else 0,
            result["bills_passthrough"], result["bills_synthesized"],
            result["bills_meta_extracted"], result["bills_failed"],
        )

        with timer.phase("write_synthesized"):
            if synth_rows:
                df_new  = pd.DataFrame(synth_rows, columns=SYNTH_COLS)
                new_ids = set(df_new["bill_id"].astype(str).tolist())
                if not df_existing.empty:
                    df_keep = df_existing[
                        ~df_existing["bill_id"].astype(str).isin(new_ids)
                    ].copy()
                    for col in SYNTH_COLS:
                        if col not in df_keep.columns: df_keep[col] = None
                    df_keep     = df_keep[SYNTH_COLS]
                    df_combined = pd.concat([df_keep, df_new], ignore_index=True)
                    logger.info("Upserted %d, retained %d → %d total",
                                len(df_new), len(df_keep), len(df_combined))
                else:
                    df_combined = df_new
                if not dry:
                    blob.write_parquet(df_combined, TIER_SILVER, synth_path)
                    logger.info("Wrote %d rows → %s", len(df_combined), synth_path)

        result["metrics"] = timer.snapshot()
        m = result["metrics"]
        logger.info(
            "Synthesizer session=%d done: wall=%.1fs "
            "bills=%d pass=%d synth=%d meta=%d fail=%d conc=%d",
            session_id, m["wall_seconds"],
            result["bills_input"], result["bills_passthrough"],
            result["bills_synthesized"], result["bills_meta_extracted"],
            result["bills_failed"], concurrency,
        )

    if _log_ok:
        with pl.pipeline_run():
            with pl.stage("synthesizer", order=4):
                _do_run()
                pl.set_stage_metadata(
                    input_count=result.get("bills_input", 0),
                    output_count=(result.get("bills_passthrough", 0)
                                  + result.get("bills_synthesized", 0)
                                  + result.get("bills_meta_extracted", 0)),
                    rows_written=(result.get("bills_passthrough", 0)
                                  + result.get("bills_synthesized", 0)
                                  + result.get("bills_meta_extracted", 0)),
                    extra={
                        "bills_meta_extracted": result.get("bills_meta_extracted", 0),
                        "bills_failed":         result.get("bills_failed", 0),
                        "session_id":           session_id,
                        "concurrency":          concurrency,
                    },
                )
    else:
        _do_run()

    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run(config: dict) -> dict:
    cfg      = config.get("synthesizer") or config
    sessions = list(cfg.get("sessions", []))
    mode     = cfg.get("mode", "live")
    dry      = (mode == "dry")

    if not sessions:
        return {
            "stage": "synthesizer", "status": "FAILED",
            "input_count": 0, "output_count": 0,
            "skip_reason": None,
            "error": "No sessions configured",
            "details": {},
        }

    blob       = BlobClient()
    per_session = {}
    total_in = total_out = 0
    any_failed = False

    for sid in sessions:
        logger.info("=" * 60)
        logger.info(
            "SYNTHESIZER session=%d mode=%s concurrency=%d "
            "meta_extraction_threshold=%d_chunks",
            sid, mode, max(1, int(cfg.get("concurrency", 1))), MAX_CHUNKS_DIRECT,
        )
        logger.info("=" * 60)
        try:
            r = _run_session(int(sid), cfg, blob, dry=dry)
            per_session[sid] = r
            total_in  += r.get("bills_input", 0)
            total_out += (r.get("bills_passthrough", 0)
                         + r.get("bills_synthesized", 0)
                         + r.get("bills_meta_extracted", 0))
            if r.get("status") == "FAILED":
                any_failed = True
        except Exception as e:
            logger.exception("Session %d FAILED: %s", sid, e)
            per_session[sid] = {"status": "FAILED", "error": str(e)}
            any_failed = True

    return {
        "stage":        "synthesizer",
        "status":       "FAILED" if any_failed else "SUCCESS",
        "input_count":  total_in,
        "output_count": total_out,
        "skip_reason":  None,
        "error":        None if not any_failed else "see per-session details",
        "details": {
            "mode": mode, "sessions": sessions, "per_session": per_session,
        },
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from utils.config_loader import load_config

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("azure.core").setLevel(logging.WARNING)
    logging.getLogger("azure.storage").setLevel(logging.WARNING)

    cfg    = load_config("configs/synthesizer.yaml")
    result = run({"synthesizer": cfg})
    print("\n" + "=" * 60 + "\nRESULT\n" + "=" * 60)
    for k, v in result.items():
        if k != "details":
            print(f"  {k}: {v}")
    for sid, sr in result["details"]["per_session"].items():
        m = sr.get("metrics", {})
        print(
            f"  session={sid}: status={sr.get('status')} "
            f"bills_in={sr.get('bills_input',0)} "
            f"pass={sr.get('bills_passthrough',0)} "
            f"synth={sr.get('bills_synthesized',0)} "
            f"meta={sr.get('bills_meta_extracted',0)} "
            f"fail={sr.get('bills_failed',0)} "
            f"wall={m.get('wall_seconds',0):.1f}s"
        )
    sys.exit(0 if result["status"] == "SUCCESS" else 1)
