"""Stage 2: Chunker — Adaptive Token-Aware Split Strategy.

PRODUCTION VERSION — replaces fixed max_tokens with an adaptive divisor
algorithm that keeps every chunk as close to TARGET_TOKENS (1200) as
possible, never below CHUNK_FLOOR (600) and never above CHUNK_CEILING
(1800). Interior chunk boundaries get OVERLAP_TOKENS (200) of context
from each neighbouring chunk so no sentence is stranded mid-thought.

Background (from evaluation, May 2026):
  Phase 2 of the qualitative review showed that SMALLER chunks score >=
  larger chunks on substantive quality for every model tested. The dominant
  failure mode at larger sizes is "off-section drift": the model anchors on
  an adjacent section and populates all fields correctly — but for the wrong
  part of the bill. Staying near 1200 tokens minimises that risk.

  The 600-token floor is chosen because 500-token chunks in the evaluation
  were "faithful but too compressed to be complete". 600 gives completeness
  headroom while still avoiding drift. 600 + 600 = 1200, 1200 + 600 = 1800
  — symmetric around the target.

Adaptive algorithm (find_best_divisor):
  Given a bill of B tokens:
    - If B <= CHUNK_CEILING: one chunk, no split.
    - Otherwise: try divisors N = 2, 3, 4, … until B/N < CHUNK_FLOOR.
      Keep the N that yields B/N closest to TARGET, within [FLOOR, CEILING].
  This produces equal-size ideal chunks; the actual token split distributes
  any remainder across chunks (never more than 1 token difference per chunk).

Overlap:
  Only interior boundaries get overlap — the first chunk gets no left-pad
  and the last chunk gets no right-pad. Each interior boundary contributes
  OVERLAP_TOKENS from the chunk before AND the chunk after, so context is
  shared bidirectionally at every seam.

New tracking columns added to CHUNK_COLS:
  split_divisor       — N chosen by the algorithm (1 = single chunk)
  target_chunk_tokens — ideal B/N (float) before remainder distribution
  overlap_tokens_used — actual overlap applied at each interior boundary

Unchanged from original:
  - All session / blob / idempotency / SLO / flush logic
  - handle_massive_bill() for pathologically huge bills
  - CHUNK_COLS schema (three new columns appended, no renames)
  - silver_chunks_session_path() convention
  - Public run() entry point signature

Original chunker preserved at:
  stages/chunker.py.bak.pre_adaptive
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional, Tuple

import pandas as pd
import tiktoken

from services.azure_blob_service import BlobClient, TIER_BRONZE, TIER_SILVER
from utils.paths import (
    bronze_bill_version_path,
    bronze_session_manifest_path,
)
from utils.timing import StageTimer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Adaptive chunking parameters
# These are the production-calibrated values from the May 2026 evaluation.
# Change here only; they flow into every function below automatically.
# ---------------------------------------------------------------------------
TARGET_TOKENS: int = 1200    # ideal chunk size — the "sweet spot" from eval
CHUNK_FLOOR: int = 600       # minimum acceptable chunk (below = single chunk)
CHUNK_CEILING: int = 1800    # maximum acceptable chunk (above = must split)
OVERLAP_TOKENS: int = 200    # context carried across each interior boundary


# ---------------------------------------------------------------------------
# Silver path helper — unchanged from original
# ---------------------------------------------------------------------------
def silver_chunks_session_path(session_id: int, profile: str | None = None) -> str:
    if profile:
        return f"legislation/chunks_{profile}/session={session_id}/_chunks.parquet"
    return f"legislation/chunks/session={session_id}/_chunks.parquet"


# ---------------------------------------------------------------------------
# Output schema — three new tracking columns appended to the original set
# ---------------------------------------------------------------------------
CHUNK_COLS = [
    # --- original columns (unchanged) ---
    "bill_id", "session_id", "bill_number", "version", "content_hash",
    "chunk_strategy", "chunk_index", "chunk_id",
    "chunk_token_count", "bill_token_count", "char_len", "chunk_text",
    "created_at",
    # --- new adaptive-chunking provenance columns ---
    "split_divisor",        # N chosen by find_best_divisor (1 = single chunk)
    "target_chunk_tokens",  # ideal bill_tokens / N (float)
    "overlap_tokens_used",  # overlap actually applied (0 for single chunks)
]


# ---------------------------------------------------------------------------
# Tokenizer — loaded once per process, tiktoken caches internally
# ---------------------------------------------------------------------------
_ENCODING = None


def _get_encoding():
    global _ENCODING
    if _ENCODING is None:
        _ENCODING = tiktoken.get_encoding("cl100k_base")
    return _ENCODING


def count_tokens(text: str) -> int:
    if not text:
        return 0
    return len(_get_encoding().encode(text))


# ---------------------------------------------------------------------------
# Core algorithm: find_best_divisor
# Pure function — no I/O, fully unit-testable.
# ---------------------------------------------------------------------------
def find_best_divisor(
    total_tokens: int,
    target: int = TARGET_TOKENS,
    floor: int = CHUNK_FLOOR,
    ceiling: int = CHUNK_CEILING,
) -> Tuple[int, float]:
    """Find the smallest divisor N >= 2 that keeps total_tokens / N
    within [floor, ceiling] and as close to target as possible.

    Returns (N, ideal_chunk_size) where ideal_chunk_size = total_tokens / N.

    If total_tokens <= ceiling, returns (1, total_tokens) — no split needed.
    If no valid divisor exists (pathological case where even N=2 drops below
    the floor), returns (1, total_tokens) and logs a warning — the caller
    will produce a single oversized chunk rather than silently truncate.

    Algorithm:
      Iterate N = 2, 3, 4, … Stop when total/N < floor (going further can
      only make chunks smaller). Among all N where total/N is in [floor,
      ceiling], pick the one minimising |total/N - target|. Ties broken by
      smaller N (fewer, larger chunks — less context fragmentation).

    Complexity: O(total_tokens / floor) iterations — for a 10 000-token
    bill with floor=600 that's at most ~17 iterations. Fast.
    """
    if total_tokens <= ceiling:
        return 1, float(total_tokens)

    best_n: Optional[int] = None
    best_size: Optional[float] = None
    best_dist = float("inf")

    for n in range(2, total_tokens + 1):
        chunk_size = total_tokens / n
        if chunk_size < floor:
            # All further divisors will only produce smaller chunks — stop.
            break
        if chunk_size > ceiling:
            # This divisor doesn't split enough yet — keep going.
            continue
        dist = abs(chunk_size - target)
        # Prefer closer to target; on exact tie prefer smaller N.
        if dist < best_dist or (dist == best_dist and (best_n is None or n < best_n)):
            best_dist = dist
            best_n = n
            best_size = chunk_size

    if best_n is None:
        # No valid divisor found — return unsplit and let caller handle it.
        logger.warning(
            "find_best_divisor: no valid divisor for %d tokens "
            "(floor=%d, ceiling=%d). Returning single chunk.",
            total_tokens, floor, ceiling,
        )
        return 1, float(total_tokens)

    return best_n, best_size


# ---------------------------------------------------------------------------
# Adaptive token splitter
# ---------------------------------------------------------------------------
def adaptive_chunk_with_overlap(
    text: str,
    overlap: int = OVERLAP_TOKENS,
    target: int = TARGET_TOKENS,
    floor: int = CHUNK_FLOOR,
    ceiling: int = CHUNK_CEILING,
) -> Tuple[List[str], int, float, int]:
    """Split `text` into chunks using the adaptive divisor algorithm.

    Returns:
        chunks          — list of decoded text strings
        divisor         — N chosen (1 = single chunk)
        target_size     — ideal chunk size (float, bill_tokens / N)
        overlap_used    — overlap tokens actually applied (0 for N=1)

    Overlap mechanics:
        Only interior boundaries get overlap. Given N chunks of size S:
          chunk 0:  tokens[0 : S]               (no left-pad)
          chunk k:  tokens[k*S - overlap : (k+1)*S + overlap]   (both sides)
          chunk N-1: tokens[(N-1)*S - overlap : end]            (no right-pad)

        This means each interior seam shares 200 tokens with its neighbour
        on each side. The first and last chunks are not padded outward
        because there is no neighbour to draw from.

    Single-chunk bills (bill_tokens <= ceiling, or no valid divisor found):
        Returned as-is with divisor=1, overlap_used=0.
    """
    enc = _get_encoding()
    tokens = enc.encode(text)
    total = len(tokens)

    divisor, target_size = find_best_divisor(total, target=target,
                                             floor=floor, ceiling=ceiling)

    if divisor == 1:
        return [text], 1, float(total), 0

    # Distribute tokens across N chunks as evenly as possible.
    # base_size chunks get `base` tokens; the first `remainder` chunks
    # get one extra token so the total is exact.
    base = total // divisor
    remainder = total % divisor

    # Build the start index of each chunk in the flat token list.
    starts: List[int] = []
    pos = 0
    for i in range(divisor):
        starts.append(pos)
        pos += base + (1 if i < remainder else 0)
    ends: List[int] = starts[1:] + [total]

    chunks: List[str] = []
    for i, (s, e) in enumerate(zip(starts, ends)):
        # Left boundary: pad back by overlap unless this is the first chunk.
        left = max(0, s - overlap) if i > 0 else s
        # Right boundary: pad forward by overlap unless this is the last chunk.
        right = min(total, e + overlap) if i < divisor - 1 else e

        chunk_tokens = tokens[left:right]
        chunks.append(enc.decode(chunk_tokens))

    return chunks, divisor, target_size, overlap


# ---------------------------------------------------------------------------
# Legacy fixed-window chunker — kept for massive-bill compression path
# ---------------------------------------------------------------------------
def token_chunk_with_overlap(
    text: str, max_tokens: int, overlap_tokens: int
) -> List[str]:
    """Standard sliding-window chunker (original implementation).

    Used only by handle_massive_bill() for the compressed-middle strategy.
    Normal bills go through adaptive_chunk_with_overlap() instead.
    """
    enc = _get_encoding()
    tokens = enc.encode(text)
    chunks: list[str] = []
    start = 0
    total = len(tokens)

    while start < total:
        end = min(total, start + max_tokens)
        chunk_tokens = tokens[start:end]
        chunks.append(enc.decode(chunk_tokens))
        if end >= total:
            break
        start = end - overlap_tokens

    return chunks


# ---------------------------------------------------------------------------
# Massive-bill handler — unchanged from original
# ---------------------------------------------------------------------------
def handle_massive_bill(
    text: str,
    max_tokens: int,
    overlap_tokens: int,
    first_n: int,
    last_n: int,
    middle_group_size: int,
) -> List[str]:
    """Compression strategy for bills exceeding MASSIVE_THRESHOLD.

    Unchanged from the original chunker. Keeps first_n + last_n chunks
    verbatim and compresses the middle into grouped, truncated chunks.
    """
    raw_chunks = token_chunk_with_overlap(text, max_tokens, overlap_tokens)

    if len(raw_chunks) <= (first_n + last_n):
        return raw_chunks

    enc = _get_encoding()
    first_chunks = raw_chunks[:first_n]
    last_chunks = raw_chunks[-last_n:]
    middle_chunks = raw_chunks[first_n:-last_n]

    compressed_middle: list[str] = []
    for i in range(0, len(middle_chunks), middle_group_size):
        group = middle_chunks[i:i + middle_group_size]
        combined = " ".join(group)
        summary_tokens = enc.encode(combined)[:max_tokens]
        compressed_middle.append(enc.decode(summary_tokens))

    return first_chunks + compressed_middle + last_chunks


def _maybe_pre_truncate(text: str, hard_truncate_tokens: int) -> str:
    """Safety net: bills over hard_truncate_tokens get char-truncated."""
    if not text:
        return text
    char_cap = hard_truncate_tokens * 4
    if len(text) > char_cap:
        return text[:char_cap]
    return text


# ---------------------------------------------------------------------------
# Per-bill orchestration — updated for adaptive splitting
# ---------------------------------------------------------------------------
def _chunk_one_bill(
    bill: dict,
    cfg: dict,
) -> Tuple[List[dict], Optional[str], int]:
    """Return (chunk_rows, error_message, bill_tokens).

    Routing:
      - bill_tokens >= massive_threshold_tokens → handle_massive_bill()
        (uses fixed max_tokens from config for the compression step)
      - otherwise → adaptive_chunk_with_overlap()
        (uses TARGET/FLOOR/CEILING/OVERLAP constants, overridable via cfg)

    New per-row columns populated here:
      split_divisor, target_chunk_tokens, overlap_tokens_used
    """
    bill_id = bill.get("bill_id")
    bill_number = bill.get("bill_number")
    session_id = bill.get("session_id")
    version = bill.get("version")
    content_hash = bill.get("content_hash")
    text = (bill.get("full_text") or "").strip()

    if not text:
        return [], "empty full_text", 0

    text = _maybe_pre_truncate(text, cfg["hard_truncate_tokens"])
    bill_tokens = count_tokens(text)

    # --- Routing ---
    if bill_tokens >= cfg["massive_threshold_tokens"]:
        # Massive-bill path: use configured fixed max_tokens for compression.
        # Adaptive splitting is not applied here because the compression
        # strategy already controls chunk count via first_n / last_n / group.
        chunks = handle_massive_bill(
            text,
            max_tokens=cfg["max_tokens"],
            overlap_tokens=cfg["overlap_tokens"],
            first_n=cfg["massive_first_n"],
            last_n=cfg["massive_last_n"],
            middle_group_size=cfg["massive_middle_group_size"],
        )
        strategy = "massive_compressed"
        divisor = len(chunks)          # informational
        target_size = float(cfg["max_tokens"])
        overlap_used = cfg["overlap_tokens"]
    else:
        # Normal path: adaptive divisor algorithm.
        # Allow config overrides so tests / benchmarks can vary the params
        # without touching the module-level constants.
        chunks, divisor, target_size, overlap_used = adaptive_chunk_with_overlap(
            text,
            overlap=cfg.get("overlap_tokens", OVERLAP_TOKENS),
            target=cfg.get("target_tokens", TARGET_TOKENS),
            floor=cfg.get("chunk_floor", CHUNK_FLOOR),
            ceiling=cfg.get("chunk_ceiling", CHUNK_CEILING),
        )
        strategy = "adaptive" if divisor > 1 else "single"

    now = datetime.utcnow().isoformat(timespec="seconds")
    rows: list[dict] = []
    for c_idx, chunk_text in enumerate(chunks):
        rows.append({
            "bill_id": bill_id,
            "session_id": session_id,
            "bill_number": bill_number,
            "version": version,
            "content_hash": content_hash,
            "chunk_strategy": strategy,
            "chunk_index": c_idx,
            "chunk_id": f"{bill_id}::{c_idx:04d}",
            "chunk_token_count": count_tokens(chunk_text),
            "bill_token_count": bill_tokens,
            "char_len": len(chunk_text),
            "chunk_text": chunk_text,
            "created_at": now,
            # --- new provenance columns ---
            "split_divisor": divisor,
            "target_chunk_tokens": round(target_size, 1),
            "overlap_tokens_used": overlap_used,
        })
    return rows, None, bill_tokens


# ---------------------------------------------------------------------------
# Idempotency index — unchanged from original
# ---------------------------------------------------------------------------
def _build_done_index(existing: pd.DataFrame) -> dict:
    """Return {bill_id: content_hash} for bills already chunked."""
    if existing.empty:
        return {}
    e = existing.copy()
    e["bill_id"] = e["bill_id"].astype(str)
    grp = e.groupby("bill_id")["content_hash"].agg(
        lambda s: s.dropna().iloc[-1] if not s.dropna().empty else None
    )
    return grp.to_dict()


# ---------------------------------------------------------------------------
# Main session driver — unchanged from original except cfg key additions
# ---------------------------------------------------------------------------
def _run_session(
    session_id: int,
    cfg: dict,
    blob: BlobClient,
    dry: bool = False,
) -> dict:
    """Process one session's bronze bills end-to-end."""
    timer = StageTimer()
    profile = cfg.get("profile") or None
    result = {
        "session_id": session_id,
        "profile": profile,
        "status": "SUCCESS",
        "bills_in_manifest": 0,
        "bills_skipped_unchanged": 0,
        "bills_chunked": 0,
        "bills_failed": 0,
        "chunks_written": 0,
        "skip_reason": None,
        "error": None,
        "metrics": None,
    }

    with timer.phase("read_manifest_and_state"):
        manifest_path = bronze_session_manifest_path(session_id)
        if not blob.blob_exists(TIER_BRONZE, manifest_path):
            logger.warning("No bronze manifest for session=%d at %s",
                           session_id, manifest_path)
            result["status"] = "SKIPPED"
            result["skip_reason"] = "no_bronze_manifest"
            result["metrics"] = timer.snapshot()
            return result

        manifest = blob.read_parquet(TIER_BRONZE, manifest_path)
        result["bills_in_manifest"] = len(manifest)
        if manifest.empty:
            logger.info("Bronze manifest for session=%d is empty.", session_id)
            result["status"] = "SKIPPED"
            result["skip_reason"] = "empty_manifest"
            result["metrics"] = timer.snapshot()
            return result

        chunks_path = silver_chunks_session_path(session_id, profile=profile)
        if blob.blob_exists(TIER_SILVER, chunks_path):
            existing = blob.read_parquet(TIER_SILVER, chunks_path)
            logger.info(
                "Loaded %d existing chunks for session=%d (profile=%s)",
                len(existing), session_id, profile or "default",
            )
        else:
            existing = pd.DataFrame(columns=CHUNK_COLS)

        done = _build_done_index(existing)

    bill_limit = cfg.get("bill_limit")
    if bill_limit:
        manifest = manifest.head(int(bill_limit))
        logger.info("Bill limit applied: processing first %d bills", bill_limit)

    new_rows: list[dict] = []
    rechunked_bill_ids: set = set()

    for idx, (_, m_row) in enumerate(manifest.iterrows(), start=1):
        bill_id = str(m_row["bill_id"])
        bill_number = str(m_row["bill_number"])
        current_hash = m_row.get("content_hash")
        version = m_row.get("current_version", 1)

        if not m_row.get("has_full_text"):
            continue

        prior_hash = done.get(bill_id)
        if prior_hash is not None and prior_hash == current_hash:
            result["bills_skipped_unchanged"] += 1
            continue
        if prior_hash is not None and prior_hash != current_hash:
            rechunked_bill_ids.add(bill_id)

        with timer.phase("read_bill_json"):
            bill_path = bronze_bill_version_path(session_id, bill_number, int(version))
            if not blob.blob_exists(TIER_BRONZE, bill_path):
                logger.warning(
                    "Manifest references missing bill JSON: %s (skipping)", bill_path,
                )
                result["bills_failed"] += 1
                continue
            bill = blob.read_json(TIER_BRONZE, bill_path)

        with timer.phase("tokenize_and_chunk"):
            rows, err, bill_tokens = _chunk_one_bill(bill, cfg)
        if err:
            logger.warning("Bill %s failed to chunk: %s", bill_number, err)
            result["bills_failed"] += 1
            continue

        # Log the chunking decision for every bill so we can audit splits.
        if rows:
            r0 = rows[0]
            logger.info(
                "Bill %s: %d tokens → divisor=%d strategy=%s "
                "target_chunk=%.0f chunks=%d",
                bill_number,
                r0["bill_token_count"],
                r0["split_divisor"],
                r0["chunk_strategy"],
                r0["target_chunk_tokens"],
                len(rows),
            )

        new_rows.extend(rows)
        result["bills_chunked"] += 1
        result["chunks_written"] += len(rows)
        timer.add_count("bills_chunked", 1)
        timer.add_count("chunks_written", len(rows))
        timer.add_count("tokens_processed", bill_tokens)

        if idx % 25 == 0:
            logger.info(
                "Chunker progress: %d/%d (chunked=%d skipped=%d failed=%d)",
                idx, len(manifest),
                result["bills_chunked"],
                result["bills_skipped_unchanged"],
                result["bills_failed"],
            )

    with timer.phase("write_silver"):
        if new_rows:
            new_df = pd.DataFrame(new_rows, columns=CHUNK_COLS)
            if not existing.empty:
                if rechunked_bill_ids:
                    existing = existing[
                        ~existing["bill_id"].astype(str).isin(rechunked_bill_ids)
                    ].copy()
                    logger.info("Replaced rows for %d re-chunked bills",
                                len(rechunked_bill_ids))
                combined = pd.concat([existing, new_df], ignore_index=True)
            else:
                combined = new_df

            if dry:
                logger.info(
                    "[DRY] Would write %d total chunk rows -> %s",
                    len(combined), chunks_path,
                )
            else:
                blob.write_parquet(combined, TIER_SILVER, chunks_path)
                logger.info("Wrote %d total chunk rows -> %s",
                            len(combined), chunks_path)
        else:
            logger.info("No new chunks to write for session=%d", session_id)

    result["metrics"] = timer.snapshot()

    slo_cfg = cfg.get("slos", {})
    if slo_cfg:
        from utils.slo import check_slos, format_slo_report
        slo_results = check_slos(result["metrics"], slo_cfg)
        result["slos"] = slo_results
        report = format_slo_report(slo_results)
        if report:
            for line in report.splitlines():
                logger.info(line)

    m = result["metrics"]
    tput = m.get("throughput", {})
    logger.info(
        "Chunker session=%d profile=%s done: wall=%.2fs active=%.2fs | "
        "bills/s=%.2f chunks/s=%.2f tokens/s=%.0f",
        session_id, profile or "default",
        m["wall_seconds"], m["active_seconds"],
        tput.get("bills_chunked_per_second", 0.0),
        tput.get("chunks_written_per_second", 0.0),
        tput.get("tokens_processed_per_second", 0.0),
    )

    return result


# ---------------------------------------------------------------------------
# Public entry point — signature unchanged
# ---------------------------------------------------------------------------
def run(config: dict) -> dict:
    """Run the chunker stage."""
    cfg = config.get("chunker") or config
    sessions = list(cfg.get("sessions", []))
    mode = cfg.get("mode", "live")
    dry = (mode == "dry")

    if not sessions:
        return {
            "stage": "chunker", "status": "FAILED",
            "input_count": 0, "output_count": 0,
            "skip_reason": None,
            "error": "No sessions configured under chunker.sessions",
            "details": {},
        }

    blob = BlobClient()
    per_session: dict = {}
    total_in = 0
    total_out = 0
    any_failed = False

    for sid in sessions:
        logger.info("=" * 60)
        logger.info("CHUNKER session=%d mode=%s", sid, mode)
        logger.info("=" * 60)
        try:
            r = _run_session(int(sid), cfg, blob, dry=dry)
            per_session[sid] = r
            total_in += r.get("bills_in_manifest", 0)
            total_out += r.get("chunks_written", 0)
        except Exception as e:
            logger.exception("Session %d FAILED: %s", sid, e)
            per_session[sid] = {"status": "FAILED", "error": str(e)}
            any_failed = True

    return {
        "stage": "chunker",
        "status": "FAILED" if any_failed else "SUCCESS",
        "input_count": total_in,
        "output_count": total_out,
        "skip_reason": None,
        "error": None if not any_failed else "see per-session details",
        "details": {
            "mode": mode,
            "sessions": sessions,
            "per_session": per_session,
        },
    }


# ---------------------------------------------------------------------------
# CLI entry point — unchanged
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

    cfg = load_config("configs/chunker.yaml")
    result = run({"chunker": cfg})
    print()
    print("=" * 60)
    print("RESULT")
    print("=" * 60)
    for k, v in result.items():
        if k != "details":
            print(f"  {k}: {v}")
    for sid, sr in result["details"]["per_session"].items():
        print(f"  session={sid}: {sr}")
    sys.exit(0 if result["status"] == "SUCCESS" else 1)
