"""Phase timing and throughput KPI helpers for pipeline stages.

Every stage produces (a) a result dict for the orchestrator and pipeline
logger, and (b) per-stage timing breakdowns for performance attribution.
This module gives all stages a consistent way to do (b) so the metrics
are comparable across stages.

Why care about this:
  - Performance regressions need to be attributable to a specific phase.
    If chunker latency doubles, was it tokenization, the chunking math,
    or the silver write? Without phase timing, you can't tell.
  - Throughput KPIs (bills/sec, chunks/sec, tokens/sec) are the unit of
    comparison when evaluating infrastructure changes. Same pattern as
    GuideLLM's request/sec and tokens/sec for inference serving — applied
    one tier earlier.
  - Idempotency means re-runs do less work; "wall time" alone is misleading.
    We separate "wall_seconds" (total elapsed) from "active_seconds" (time
    in productive phases) so the KPIs reflect actual work done.
  - Tail latency (p50/p95/p99) matters for stages with per-event variance.
    A mean api_time of 11s hides the case where p99 is 30s. The extractor
    in particular benefits from this because LLM calls have a long tail.

Usage in a stage:

    from utils.timing import StageTimer

    timer = StageTimer()
    with timer.phase("read_manifest"):
        manifest = blob.read_parquet(...)

    with timer.phase("tokenize"):
        tokens = encode(text)

    # Per-event latency samples (for percentile rollups)
    for chunk in chunks:
        t0 = time.perf_counter()
        result = backend.run(chunk)
        timer.add_latency("api_time", time.perf_counter() - t0)

    timer.add_count("bills_chunked", 50)
    timer.add_count("chunks_written", 137)
    timer.add_count("tokens_processed", 412_318)

    metrics = timer.snapshot()
    # -> {
    #     "wall_seconds": 12.4,
    #     "active_seconds": 11.9,
    #     "phases": {"read_manifest": 0.3, "tokenize": 4.2, ...},
    #     "counts": {"bills_chunked": 50, "chunks_written": 137, ...},
    #     "throughput": {
    #         "bills_chunked_per_second": 4.03,
    #         ...
    #     },
    #     "latency": {
    #         "api_time": {
    #             "n": 50, "mean": 11.2, "p50": 10.8, "p95": 18.4,
    #             "p99": 22.1, "min": 4.2, "max": 23.5,
    #         }
    #     },
    # }

The dict goes into the stage's result under the "metrics" key, so the
shape is identical for ingestion, chunker, extractor.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolated percentile of a sorted list. pct in [0,1].

    Standard percentile formula (numpy's linear interpolation method).
    No numpy dep here so utils stays pure-stdlib.
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = pct * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def _summarize_latencies(samples: list[float]) -> dict:
    """Roll a list of per-event latencies into a stats dict."""
    if not samples:
        return {"n": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0,
                "min": 0.0, "max": 0.0}
    s = sorted(samples)
    return {
        "n": len(s),
        "mean": round(sum(s) / len(s), 4),
        "p50": round(_percentile(s, 0.50), 4),
        "p95": round(_percentile(s, 0.95), 4),
        "p99": round(_percentile(s, 0.99), 4),
        "min": round(s[0], 4),
        "max": round(s[-1], 4),
    }


class StageTimer:
    """Tracks wall-clock, per-phase timing, throughput counts, and
    per-event latency samples for a single stage run.

    Not thread-safe. Each stage worker should use its own instance.
    Phases are mutually exclusive — entering a new phase does NOT close
    the previous one (use `with` blocks). Same phase entered twice
    accumulates time across both entries.
    """

    def __init__(self) -> None:
        self._t0 = time.perf_counter()
        self._phase_seconds: dict[str, float] = {}
        self._counts: dict[str, int] = {}
        self._latency_samples: dict[str, list[float]] = {}

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Time a code block and accumulate under `name`."""
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - start
            self._phase_seconds[name] = self._phase_seconds.get(name, 0.0) + elapsed

    def add_count(self, name: str, n: int) -> None:
        """Increment a counter (bills_chunked, tokens_processed, etc.).

        Counts are what the throughput KPIs are computed against. If you
        want a value to show up as `<name>_per_second` in throughput,
        add_count it here.
        """
        self._counts[name] = self._counts.get(name, 0) + int(n)

    def set_count(self, name: str, n: int) -> None:
        """Replace a counter (use sparingly — add_count is the default)."""
        self._counts[name] = int(n)

    def add_latency(self, name: str, seconds: float) -> None:
        """Record a single per-event latency sample under `name`.

        Use for any operation where each invocation has independent
        variance — LLM calls, HTTP fetches, blob writes. The snapshot
        will roll all samples for a given name into mean/p50/p95/p99.
        """
        if seconds is None:
            return
        self._latency_samples.setdefault(name, []).append(float(seconds))

    @contextmanager
    def latency(self, name: str) -> Iterator[None]:
        """Convenience context manager: record block duration as a sample.

        Equivalent to:
            t0 = time.perf_counter()
            ...
            timer.add_latency(name, time.perf_counter() - t0)
        """
        start = time.perf_counter()
        try:
            yield
        finally:
            self.add_latency(name, time.perf_counter() - start)

    def snapshot(self) -> dict:
        """Return the current metrics dict. Safe to call multiple times."""
        wall = time.perf_counter() - self._t0
        active = sum(self._phase_seconds.values())

        # Throughput: counts divided by ACTIVE time, not wall time.
        # Wall includes idle gaps (e.g. waiting on user, sleeping between
        # rate-limited calls). Active is what was actually doing work,
        # which is what you want for cross-run comparison.
        throughput = {}
        if active > 0:
            for name, n in self._counts.items():
                throughput[f"{name}_per_second"] = round(n / active, 3)

        latency = {
            name: _summarize_latencies(samples)
            for name, samples in self._latency_samples.items()
        }

        return {
            "wall_seconds": round(wall, 3),
            "active_seconds": round(active, 3),
            "phases": {k: round(v, 3) for k, v in self._phase_seconds.items()},
            "counts": dict(self._counts),
            "throughput": throughput,
            "latency": latency,
        }
