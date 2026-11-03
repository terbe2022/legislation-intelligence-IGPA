"""SLO (Service Level Objective) checks for pipeline stages.

A KPI is a metric you track. An SLO is a target you commit to.
This module reads a stage's metrics dict (from StageTimer.snapshot())
and checks it against an SLO config block (from YAML), returning a
list of pass/fail results that the stage records on its result and
the orchestrator can act on.

Why care about this:
  - Without SLOs, "the pipeline ran" only tells you it didn't crash.
    With SLOs, "the pipeline met its SLOs" tells you it ran *well enough*.
  - SLOs in code (not just dashboards) means a stage can self-report
    its quality, and the orchestrator can decide to alert / page /
    pause downstream stages without humans interpreting raw metrics.
  - Same conceptual model PSAP applies to RHAIIS — production LLM
    inference SLOs (p99 latency under X, throughput above Y, error
    rate under Z) — applied at our application layer.

SLO config shape (in YAML, under any stage's config):

    slos:
      # Throughput floors: throughput must be AT LEAST this value
      throughput_min:
        chunks_extracted_per_second: 0.05      # at least 1 chunk per 20s
        tokens_processed_per_second: 50000     # 50k tokens/s

      # Latency ceilings: latency stat must be AT MOST this value (seconds)
      latency_max:
        api_time.p99: 30.0                     # p99 LLM call < 30s
        api_time.p50: 15.0                     # p50 LLM call < 15s

      # Counter ceilings: count must be AT MOST this value
      counts_max:
        chunks_failed: 0                       # zero failures allowed

      # Counter ratios: count_a / count_b must be AT MOST this value
      ratio_max:
        - numerator: chunks_failed
          denominator: chunks_extracted
          max: 0.05                            # error rate <= 5%

Each SLO becomes one entry in the returned list:
    {
      "name": "throughput_min.chunks_extracted_per_second",
      "kind": "throughput_min",
      "target": 0.05,
      "actual": 0.083,
      "passed": True,
      "message": "...",
    }

Stages should treat SLO failures as warnings, not exceptions —
the data is still good, the *speed* didn't meet the target. Whether
that should fail a CI build is a policy decision for the orchestrator,
not the stage.
"""

from __future__ import annotations

from typing import Any


def _get_nested(d: dict, dotted: str) -> Any:
    """Look up a value by dotted path. e.g. 'api_time.p99' -> d['api_time']['p99'].

    Returns None if any segment is missing. Used so SLO config can name
    nested metrics (latency.api_time.p99) without us hardcoding shapes.
    """
    cur: Any = d
    for seg in dotted.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return None
        cur = cur[seg]
    return cur


def _check_throughput_min(metrics: dict, targets: dict) -> list[dict]:
    """Each target: actual >= target. Lower than target = fail."""
    results = []
    throughput = metrics.get("throughput", {})
    for name, target in targets.items():
        actual = throughput.get(name)
        if actual is None:
            results.append({
                "name": f"throughput_min.{name}",
                "kind": "throughput_min",
                "target": target,
                "actual": None,
                "passed": False,
                "message": f"metric not found in snapshot",
            })
            continue
        passed = float(actual) >= float(target)
        results.append({
            "name": f"throughput_min.{name}",
            "kind": "throughput_min",
            "target": float(target),
            "actual": float(actual),
            "passed": passed,
            "message": f"actual {actual} {'>=' if passed else '<'} target {target}",
        })
    return results


def _check_latency_max(metrics: dict, targets: dict) -> list[dict]:
    """Each target: actual <= target. Higher than target = fail.

    Targets use dotted paths: 'api_time.p99' looks under metrics['latency']['api_time']['p99'].
    """
    results = []
    latency = metrics.get("latency", {})
    for path, target in targets.items():
        actual = _get_nested(latency, path)
        if actual is None:
            results.append({
                "name": f"latency_max.{path}",
                "kind": "latency_max",
                "target": target,
                "actual": None,
                "passed": False,
                "message": f"metric not found in snapshot",
            })
            continue
        passed = float(actual) <= float(target)
        results.append({
            "name": f"latency_max.{path}",
            "kind": "latency_max",
            "target": float(target),
            "actual": float(actual),
            "passed": passed,
            "message": f"actual {actual}s {'<=' if passed else '>'} target {target}s",
        })
    return results


def _check_counts_max(metrics: dict, targets: dict) -> list[dict]:
    """Each target: actual <= target."""
    results = []
    counts = metrics.get("counts", {})
    for name, target in targets.items():
        actual = counts.get(name, 0)
        passed = float(actual) <= float(target)
        results.append({
            "name": f"counts_max.{name}",
            "kind": "counts_max",
            "target": float(target),
            "actual": float(actual),
            "passed": passed,
            "message": f"actual {actual} {'<=' if passed else '>'} target {target}",
        })
    return results


def _check_ratio_max(metrics: dict, ratios: list) -> list[dict]:
    """Each ratio: numerator/denominator <= max.

    If denominator is 0, the ratio is treated as 0 (no events = no failures).
    """
    results = []
    counts = metrics.get("counts", {})
    for ratio in ratios:
        num_name = ratio["numerator"]
        denom_name = ratio["denominator"]
        target = float(ratio["max"])
        num = float(counts.get(num_name, 0))
        denom = float(counts.get(denom_name, 0))
        actual = (num / denom) if denom > 0 else 0.0
        passed = actual <= target
        results.append({
            "name": f"ratio_max.{num_name}_per_{denom_name}",
            "kind": "ratio_max",
            "target": target,
            "actual": round(actual, 6),
            "passed": passed,
            "message": (
                f"{num_name}/{denom_name} = {num}/{denom} = {actual:.4f} "
                f"{'<=' if passed else '>'} target {target}"
            ),
        })
    return results


def check_slos(metrics: dict, slo_cfg: dict) -> dict:
    """Run all SLO checks defined in `slo_cfg` against `metrics`.

    Returns a dict:
        {
          "passed": bool,            # True iff every SLO passed
          "n_total": int,
          "n_passed": int,
          "n_failed": int,
          "results": [ {name, kind, target, actual, passed, message}, ... ],
        }
    """
    if not slo_cfg:
        return {"passed": True, "n_total": 0, "n_passed": 0, "n_failed": 0, "results": []}

    results: list[dict] = []

    if "throughput_min" in slo_cfg:
        results.extend(_check_throughput_min(metrics, slo_cfg["throughput_min"]))
    if "latency_max" in slo_cfg:
        results.extend(_check_latency_max(metrics, slo_cfg["latency_max"]))
    if "counts_max" in slo_cfg:
        results.extend(_check_counts_max(metrics, slo_cfg["counts_max"]))
    if "ratio_max" in slo_cfg:
        results.extend(_check_ratio_max(metrics, slo_cfg["ratio_max"]))

    n_total = len(results)
    n_passed = sum(1 for r in results if r["passed"])
    n_failed = n_total - n_passed

    return {
        "passed": (n_failed == 0),
        "n_total": n_total,
        "n_passed": n_passed,
        "n_failed": n_failed,
        "results": results,
    }


def format_slo_report(slo_results: dict) -> str:
    """Format SLO check output as a multi-line string for logging."""
    if not slo_results or slo_results["n_total"] == 0:
        return ""
    lines = []
    overall = "PASS" if slo_results["passed"] else "FAIL"
    lines.append(
        f"SLO check: {overall} ({slo_results['n_passed']}/{slo_results['n_total']} met)"
    )
    for r in slo_results["results"]:
        marker = "  ✓" if r["passed"] else "  ✗"
        lines.append(f"{marker} {r['name']}: {r['message']}")
    return "\n".join(lines)