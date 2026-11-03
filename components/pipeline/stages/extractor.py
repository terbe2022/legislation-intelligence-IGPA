"""Stage 3: Feature Extractor.

Reads chunks from silver, calls the inference backend (vLLM or Ollama),
parses the labeled-field output, and writes structured features back to
silver.

Flow:
  1. Read silver/legislation/chunks/session={N}/_chunks.parquet
  2. Read silver/legislation/features_raw/session={N}/_features_raw.parquet
     (idempotency: skip chunks already extracted)
  3. For each unprocessed chunk:
       a. Format prompt with chunk text
       b. Call inference backend via inference_service router
       c. Parse output into labeled fields
       d. Append result to in-memory list
  4. Periodically flush to silver (every N chunks, in case of crash)
  5. Final flush at end

Outputs (silver tier, per session):
  features_raw/session={N}/_features_raw.parquet
    columns: chunk_id, bill_id, bill_number, chunk_index,
             prompt_version, model_name, backend, llm_output_text,
             api_time_seconds, error, extracted_at,
             llama_summary, legislative_goal, ... (all parsed fields),
             parse_status, missing_fields_count, parse_errors

Returns a dict for pipeline_logger.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from services.azure_blob_service import BlobClient, TIER_SILVER
from services.inference_service import build_backend
from utils.feature_parser import CANONICAL_FIELDS, parse_features
from utils.prompt_loader import fill_prompt, load_prompt
from utils.timing import StageTimer
from utils.slo import check_slos, format_slo_report

logger = logging.getLogger(__name__)

# Pipeline logger log directory — same root used by run_daily.py and the monitor
_PIPELINE_LOG_DIR = Path(
    __import__("os").environ.get('LEG_COMPONENTS_PIPELINE_STAGES_EXTRACTOR_PY__PIPELINE_LOG_DIR_L52', "")
)


# Path conventions for silver — mirrors the chunker's output location.
def _silver_chunks_path(session_id: int, profile: str | None = None) -> str:
    if profile:
        return f"legislation/chunks_{profile}/session={session_id}/_chunks.parquet"
    return f"legislation/chunks/session={session_id}/_chunks.parquet"


def _silver_features_raw_path(session_id: int, profile: str | None = None) -> str:
    if profile:
        return f"legislation/features_{profile}/session={session_id}/_features.parquet"
    return f"legislation/features_raw/session={session_id}/_features_raw.parquet"


# Columns we always write to features_raw, even when LLM call fails.
BASE_COLS = [
    "chunk_id", "bill_id", "bill_number", "chunk_index",
    "prompt_version", "model_name", "backend",
    "llm_output_text", "api_time_seconds", "error", "extracted_at",
    # Per-request performance breakdown (added May 20, 2026 for re-eval)
    "ttft_seconds",
    "decode_time_seconds",
    "prompt_tokens",
    "completion_tokens",
    "decode_tput_tps",
    "prefill_tput_tps",
    "itl_mean_ms",
    "itl_p50_ms",
    "itl_p99_ms",
    "n_output_chunks",
    "finish_reason",
    "cache_hit",
]
PARSED_COLS = list(CANONICAL_FIELDS) + ["parse_status", "missing_fields_count", "parse_errors"]
ALL_COLS = BASE_COLS + PARSED_COLS


def _load_existing_features(blob: BlobClient, session_id: int, profile: str | None = None) -> pd.DataFrame:
    """Load already-extracted features for idempotency. Empty if first run."""
    path = _silver_features_raw_path(session_id, profile=profile)
    if not blob.blob_exists(TIER_SILVER, path):
        return pd.DataFrame(columns=ALL_COLS)
    df = blob.read_parquet(TIER_SILVER, path)
    logger.info("Loaded %d existing features for session=%d (profile=%s)",
                len(df), session_id, profile or "default")
    return df


def _flush(blob: BlobClient, session_id: int, df: pd.DataFrame, profile: str | None = None) -> None:
    """Write the current accumulated features back to silver."""
    if df.empty:
        return
    path = _silver_features_raw_path(session_id, profile=profile)
    df["bill_id"] = df["bill_id"].astype(str)
    blob.write_parquet(df, TIER_SILVER, path)


def _extract_one(
    backend,
    chunk_text: str,
    prompt_template: str,
    prompt_version: str,
    cache=None,
) -> dict:
    """Run one extraction. Returns a row dict ready to merge into the DataFrame."""
    # 1. Cache lookup
    if cache is not None:
        cached_row = cache.get(chunk_text)
        if cached_row is not None:
            row = dict(cached_row)
            row["cache_hit"] = True
            return row

    # 2. Real LLM call
    prompt = fill_prompt(prompt_template, chunk_text)
    text, api_time, err = backend.run(prompt)

    # 3. Capture per-request metrics from vllm_service (vLLM only).
    perf_metrics = {
        "ttft_seconds": None, "decode_time_seconds": None,
        "prompt_tokens": None, "completion_tokens": None,
        "decode_tput_tps": None, "prefill_tput_tps": None,
        "itl_mean_ms": None, "itl_p50_ms": None, "itl_p99_ms": None,
        "n_output_chunks": None, "finish_reason": None,
    }
    if backend.name == "vllm":
        try:
            from services.vllm_service import get_last_call_metrics
            m = get_last_call_metrics()
            if m is not None:
                for k in perf_metrics:
                    if k in m and m[k] is not None:
                        perf_metrics[k] = m[k]
        except Exception as exc:
            logger.debug("Could not capture per-call metrics: %s", exc)

    base = {
        "model_name": getattr(backend, "model_name", None) or
                      getattr(backend, "config", None) and backend.config.model_name,
        "backend": backend.name,
        "prompt_version": prompt_version,
        "llm_output_text": text,
        "api_time_seconds": api_time,
        "error": err,
        "extracted_at": datetime.utcnow().isoformat(timespec="seconds"),
        "cache_hit": False,
        **perf_metrics,
    }

    if err or not text:
        parsed = {f: None for f in CANONICAL_FIELDS}
        parsed["parse_status"] = "EMPTY"
        parsed["missing_fields_count"] = len(CANONICAL_FIELDS)
        parsed["parse_errors"] = [err or "empty LLM output"]
    else:
        parsed = parse_features(text)

    row = {**base, **parsed}

    if cache is not None and not err and text:
        cache.put(chunk_text, row)

    return row


def _run_session(
    session_id: int,
    cfg: dict,
    blob: BlobClient,
) -> dict:
    """Process one session's chunks end-to-end."""
    timer = StageTimer()
    profile = cfg.get("profile") or None
    result = {
        "session_id": session_id,
        "profile": profile,
        "status": "SUCCESS",
        "chunks_input": 0,
        "chunks_skipped_existing": 0,
        "chunks_extracted": 0,
        "chunks_failed": 0,
        "skip_reason": None,
        "error": None,
        "metrics": None,
        "slos": None,
    }

    # ── Pipeline logger (defensive — never crashes the stage) ────────────
    try:
        from utils.pipeline_logger import PipelineLogger
        pl = PipelineLogger(
            workflow_name="legislation_pipeline",
            log_dir=_PIPELINE_LOG_DIR,
            pipeline_version=cfg.get("pipeline_version", "unknown"),
        )
        _log_ok = True
    except Exception as _le:
        logger.warning("PipelineLogger unavailable (%s) — stage proceeds without monitor logging", _le)
        pl = None
        _log_ok = False

    def _do_run():
        # 1. Load chunks (input)
        with timer.phase("read_chunks_and_state"):
            chunks_path = _silver_chunks_path(session_id, profile=profile)
            if not blob.blob_exists(TIER_SILVER, chunks_path):
                logger.warning(
                    "No chunks parquet found for session=%d profile=%s at %s",
                    session_id, profile or "default", chunks_path,
                )
                result["status"] = "SKIPPED"
                result["skip_reason"] = "no_chunks_input"
                result["metrics"] = timer.snapshot()
                return

            chunks_df = blob.read_parquet(TIER_SILVER, chunks_path)
            result["chunks_input"] = len(chunks_df)
            if chunks_df.empty:
                logger.info("Chunks parquet for session=%d is empty.", session_id)
                result["status"] = "SKIPPED"
                result["skip_reason"] = "empty_chunks"
                result["metrics"] = timer.snapshot()
                return

            # 2. Idempotency: skip chunks already extracted
            existing = _load_existing_features(blob, session_id, profile=profile)
            if not existing.empty:
                done_ids = set(existing["chunk_id"].astype(str).tolist())
                before = len(chunks_df)
                chunks_df = chunks_df[~chunks_df["chunk_id"].astype(str).isin(done_ids)].copy()
                result["chunks_skipped_existing"] = before - len(chunks_df)
                logger.info("Skipped %d chunks already extracted.", result["chunks_skipped_existing"])

        if chunks_df.empty:
            logger.info("Nothing to extract — all chunks already done.")
            result["metrics"] = timer.snapshot()
            return

        chunk_limit = cfg.get("chunk_limit")
        if chunk_limit is not None and chunk_limit > 0 and len(chunks_df) > chunk_limit:
            logger.info(
                "Applying chunk_limit=%d (was %d chunks) for session %d.",
                chunk_limit, len(chunks_df), session_id,
            )
            chunks_df = chunks_df.head(int(chunk_limit)).copy()

        # 3. Build backend and load prompt
        backend = build_backend(cfg)
        prompt_template = load_prompt(cfg["prompt_path"])
        prompt_version = cfg.get("prompt_version", "v1")
        flush_every = int(cfg.get("flush_every_n_chunks", 25))

        warmup_enabled = cfg.get("warmup_enabled", True)
        warmup_n = int(cfg.get("warmup_n", 5))
        if warmup_enabled and warmup_n > 0:
            with timer.phase("warmup"):
                warmup_times = []
                for i in range(warmup_n):
                    try:
                        _, warmup_t, warmup_err = backend.run(
                            f"Respond with the word 'ready' (warmup call {i+1}/{warmup_n})."
                        )
                        if warmup_err:
                            logger.warning("Warmup call %d/%d returned error: %s",
                                           i + 1, warmup_n, warmup_err)
                        else:
                            warmup_times.append(warmup_t)
                    except Exception as e:
                        logger.warning("Warmup call %d/%d failed: %s", i + 1, warmup_n, e)
                if warmup_times:
                    logger.info(
                        "Warmup complete: %d/%d calls succeeded, "
                        "first=%.2fs last=%.2fs mean=%.2fs",
                        len(warmup_times), warmup_n,
                        warmup_times[0], warmup_times[-1],
                        sum(warmup_times) / len(warmup_times),
                    )
        else:
            logger.info("Warmup disabled by config (warmup_enabled=%s, warmup_n=%d)",
                        warmup_enabled, warmup_n)

        cache = None
        if cfg.get("chunk_cache_enabled", False):
            from tools.chunk_cache import ChunkCache
            cache = ChunkCache()
            logger.info("Chunk cache enabled for this profile run")

        # 4. Process loop with periodic flush
        new_rows: list[dict] = []
        accumulated = existing

        concurrency = int(cfg.get("concurrency", 1))
        if concurrency < 1:
            concurrency = 1
        logger.info("Extractor concurrency: %d", concurrency)
        timer.set_count("concurrency", concurrency)

        work_items: list[tuple[int, dict, str]] = []
        for i, (_, row) in enumerate(chunks_df.iterrows(), start=1):
            chunk_text = row.get("chunk_text") or ""
            identity = {
                "chunk_id": row.get("chunk_id"),
                "bill_id": row.get("bill_id"),
                "bill_number": row.get("bill_number"),
                "chunk_index": row.get("chunk_index"),
            }
            if not chunk_text.strip():
                logger.warning("Empty chunk_text for chunk_id=%s, skipping", identity["chunk_id"])
                continue
            work_items.append((i, identity, chunk_text))

        def _process_one(work_item: tuple[int, dict, str]) -> tuple[int, dict, dict, float]:
            idx, identity_inner, chunk_text_inner = work_item
            t0 = time.perf_counter()
            rec_inner = _extract_one(backend, chunk_text_inner, prompt_template, prompt_version,
                                      cache=cache)
            elapsed = time.perf_counter() - t0
            return idx, identity_inner, rec_inner, elapsed

        if concurrency == 1:
            for work_item in work_items:
                with timer.phase("llm_inference"):
                    idx, identity, rec, elapsed = _process_one(work_item)
                if rec.get("api_time_seconds") is not None and not rec.get("error"):
                    timer.add_latency("api_time", rec["api_time_seconds"])

                full_row = {**identity, **rec}
                new_rows.append(full_row)

                if rec.get("error"):
                    result["chunks_failed"] += 1
                    timer.add_count("chunks_failed", 1)
                else:
                    result["chunks_extracted"] += 1
                    timer.add_count("chunks_extracted", 1)
                    ps = rec.get("parse_status") or "UNKNOWN"
                    timer.add_count(f"parse_{ps.lower()}", 1)

                if idx % flush_every == 0:
                    with timer.phase("write_silver"):
                        df_so_far = pd.DataFrame(new_rows, columns=ALL_COLS)
                        combined = pd.concat([accumulated, df_so_far], ignore_index=True)
                        _flush(blob, session_id, combined, profile=profile)
                    logger.info("Flushed at %d/%d chunks (extracted=%d failed=%d)",
                                idx, len(chunks_df), result["chunks_extracted"], result["chunks_failed"])
        else:
            results_by_idx: dict[int, tuple[dict, dict, float]] = {}
            next_to_process = 1

            with timer.phase("llm_inference"):
                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    futures = {pool.submit(_process_one, w): w[0] for w in work_items}
                    for future in as_completed(futures):
                        try:
                            idx, identity, rec, elapsed = future.result()
                        except Exception as e:
                            wi_idx = futures[future]
                            logger.exception("Worker raised on chunk idx=%d: %s", wi_idx, e)
                            continue

                        if rec.get("api_time_seconds") is not None and not rec.get("error"):
                            timer.add_latency("api_time", rec["api_time_seconds"])

                        results_by_idx[idx] = (identity, rec, elapsed)

                        while next_to_process in results_by_idx:
                            identity, rec, elapsed = results_by_idx.pop(next_to_process)
                            full_row = {**identity, **rec}
                            new_rows.append(full_row)
                            if rec.get("error"):
                                result["chunks_failed"] += 1
                                timer.add_count("chunks_failed", 1)
                            else:
                                result["chunks_extracted"] += 1
                                timer.add_count("chunks_extracted", 1)
                                ps = rec.get("parse_status") or "UNKNOWN"
                                timer.add_count(f"parse_{ps.lower()}", 1)
                            if next_to_process % flush_every == 0:
                                with timer.phase("write_silver"):
                                    df_so_far = pd.DataFrame(new_rows, columns=ALL_COLS)
                                    combined = pd.concat([accumulated, df_so_far], ignore_index=True)
                                    _flush(blob, session_id, combined, profile=profile)
                                logger.info(
                                    "Flushed at %d/%d chunks (extracted=%d failed=%d) [concurrency=%d]",
                                    next_to_process, len(chunks_df),
                                    result["chunks_extracted"], result["chunks_failed"],
                                    concurrency,
                                )
                            next_to_process += 1

        # 5. Final flush
        if new_rows:
            with timer.phase("write_silver"):
                df_new = pd.DataFrame(new_rows, columns=ALL_COLS)
                combined = pd.concat([accumulated, df_new], ignore_index=True)
                _flush(blob, session_id, combined, profile=profile)
            logger.info("Final flush: total rows=%d", len(combined))

        if cache is not None:
            cstats = cache.stats()
            logger.info(
                "Chunk cache: %d hits / %d misses (%.1f%% hit rate), %d unique chunks",
                cstats["hits"], cstats["misses"], cstats["hit_rate"] * 100,
                cstats["unique_chunks"],
            )
            result["chunk_cache_stats"] = cstats

        # 6. Snapshot metrics + SLO check
        metrics = timer.snapshot()
        result["metrics"] = metrics

        slo_cfg = cfg.get("slos", {})
        if slo_cfg:
            slo_results = check_slos(metrics, slo_cfg)
            result["slos"] = slo_results
            report = format_slo_report(slo_results)
            if report:
                for line in report.splitlines():
                    logger.info(line)

        tput = metrics.get("throughput", {})
        api_lat = (metrics.get("latency") or {}).get("api_time", {})
        logger.info(
            "Extractor session=%d done: wall=%.2fs active=%.2fs | "
            "chunks/s=%.2f | api_time mean=%.2fs p50=%.2fs p95=%.2fs p99=%.2fs",
            session_id, metrics["wall_seconds"], metrics["active_seconds"],
            tput.get("chunks_extracted_per_second", 0.0),
            api_lat.get("mean", 0.0), api_lat.get("p50", 0.0),
            api_lat.get("p95", 0.0), api_lat.get("p99", 0.0),
        )

    # ── Run with or without pipeline logger wrapper ───────────────────────
    if _log_ok:
        with pl.pipeline_run():
            with pl.stage("extractor", order=3):
                _do_run()
                # set_stage_metadata after _do_run() so result is populated
                pl.set_stage_metadata(
                    input_count=result.get("chunks_input", 0),
                    output_count=result.get("chunks_extracted", 0),
                    rows_written=result.get("chunks_extracted", 0),
                    extra={
                        "chunks_failed":   result.get("chunks_failed", 0),
                        "chunks_skipped":  result.get("chunks_skipped_existing", 0),
                        "session_id":      session_id,
                        "profile":         profile or "default",
                        "skip_reason":     result.get("skip_reason"),
                    },
                )
    else:
        _do_run()

    return result


def run(config: dict) -> dict:
    """Run the extractor stage."""
    extractor_cfg = config.get("extractor") or config
    sessions = list(extractor_cfg.get("sessions", []))

    if not sessions:
        return {
            "stage": "extractor", "status": "FAILED",
            "input_count": 0, "output_count": 0,
            "skip_reason": None,
            "error": "No sessions configured under extractor.sessions",
            "details": {},
        }

    blob = BlobClient()

    per_session = {}
    total_input = 0
    total_output = 0
    any_failed = False

    for sid in sessions:
        logger.info("=" * 60)
        logger.info("EXTRACTOR session=%d profile=%s backend=%s",
                    sid,
                    extractor_cfg.get("profile") or "default",
                    extractor_cfg.get("backend", "ollama"))
        logger.info("=" * 60)
        try:
            r = _run_session(sid, extractor_cfg, blob)
            per_session[sid] = r
            total_input += r.get("chunks_input", 0)
            total_output += r.get("chunks_extracted", 0)
        except Exception as e:
            logger.exception("Session %d FAILED: %s", sid, e)
            per_session[sid] = {"status": "FAILED", "error": str(e)}
            any_failed = True

    return {
        "stage": "extractor",
        "status": "FAILED" if any_failed else "SUCCESS",
        "input_count": total_input,
        "output_count": total_output,
        "skip_reason": None,
        "error": None if not any_failed else "see per-session details",
        "details": {
            "backend": extractor_cfg.get("backend", "ollama"),
            "sessions": sessions,
            "per_session": per_session,
        },
    }


if __name__ == "__main__":
    import sys
    from utils.config_loader import load_config

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("azure.core").setLevel(logging.WARNING)
    logging.getLogger("azure.storage").setLevel(logging.WARNING)

    cfg = load_config("configs/extractor.yaml")
    result = run({"extractor": cfg})
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
