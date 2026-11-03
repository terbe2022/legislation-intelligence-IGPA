"""HTTP client for vLLM's OpenAI-compatible chat completions endpoint.

This module wraps vLLM's OpenAI-compatible /v1/chat/completions with
*streaming* requests (SSE) so we can capture per-request performance
breakdowns:

  - TTFT (Time To First Token): how long until vLLM produces the first
    output token. Measures prefill latency.
  - Per-token arrival times: lets us compute ITL (Inter-Token Latency)
    distributions per request.
  - Decode throughput: completion_tokens / (decode_end - first_token_time)
  - Prefill throughput: prompt_tokens / TTFT
  - End-to-end latency: total wall time

These are the industry-standard breakdown metrics that tools like
GuideLLM and vLLM's own benchmark_serving.py capture. Without these,
you can only report "request took N seconds" — with these, you can
say "prefill took X seconds at Y tokens/sec, decode took Z seconds at
W tokens/sec, KV cache had hit rate R%". That's the level of detail
a senior performance engineer expects.

API contract (intentionally unchanged from the previous non-streaming
version so existing callers don't break):

    text, api_time_seconds, error = run_vllm(config, prompt)

Extra per-request metrics are captured into thread-local storage and
readable after the call:

    from services.vllm_service import get_last_call_metrics
    text, api_time, err = run_vllm(config, prompt)
    metrics = get_last_call_metrics()
    # metrics is a dict with keys like ttft_seconds, decode_tput_tps, etc.
    # or None if the call failed before any metrics were captured

Thread-local storage means concurrent callers (concurrency=4 in the
sweep harness) each see their own last-call metrics without races.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection pooling: one Session per process, reused across all requests.
# Avoids per-call TCP setup, keeps localhost connections warm.
# ---------------------------------------------------------------------------
_SESSION: Optional[requests.Session] = None
_SESSION_LOCK = threading.Lock()


def _get_session() -> requests.Session:
    """Lazy-initialized module-level Session. Thread-safe."""
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                _SESSION = requests.Session()
                # Allow up to 16 concurrent connections to the same host.
                # We expect at most concurrency=8 in practice; 16 is headroom.
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=16,
                    pool_maxsize=16,
                    max_retries=0,  # don't retry — we want to see real errors
                )
                _SESSION.mount("http://", adapter)
                _SESSION.mount("https://", adapter)
    return _SESSION


# ---------------------------------------------------------------------------
# Thread-local per-request metrics capture.
# Each thread sees its own last-call metrics; no races under concurrency=4+.
# ---------------------------------------------------------------------------
_TLS = threading.local()

# ---------------------------------------------------------------------------
# Langfuse LLM tracing — optional, silently disabled if keys not configured.
# Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY env vars to enable.
# ---------------------------------------------------------------------------
import os as _os

_LANGFUSE_CLIENT = None
_LANGFUSE_ENABLED = None  # None = not yet checked


def _get_langfuse():
    """Lazy-init Langfuse client. Returns None if keys not configured."""
    global _LANGFUSE_CLIENT, _LANGFUSE_ENABLED
    if _LANGFUSE_ENABLED is False:
        return None
    if _LANGFUSE_CLIENT is not None:
        return _LANGFUSE_CLIENT
    with _SESSION_LOCK:
        if _LANGFUSE_ENABLED is False:
            return None
        if _LANGFUSE_CLIENT is not None:
            return _LANGFUSE_CLIENT
        pk   = _os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        sk   = _os.environ.get("LANGFUSE_SECRET_KEY", "")
        host = _os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
        if not pk or not sk:
            _LANGFUSE_ENABLED = False
            return None
        try:
            from langfuse import Langfuse
            _LANGFUSE_CLIENT  = Langfuse(
                public_key=pk,
                secret_key=sk,
                host=host,
                flush_at=10,
                flush_interval=30,
            )
            _LANGFUSE_ENABLED = True
            logger.info("Langfuse tracing enabled (host=%s)", host)
        except Exception as e:
            logger.warning("Langfuse init failed — tracing disabled: %s", e)
            _LANGFUSE_ENABLED = False
            _LANGFUSE_CLIENT  = None
    return _LANGFUSE_CLIENT




def get_last_call_metrics() -> Optional[Dict[str, Any]]:
    """Return the per-request metrics dict from the most recent run_vllm
    call on THIS thread. Returns None if no call has been made yet on
    this thread, or if the last call failed before metrics could be captured.

    Keys in the returned dict (all optional, may be None if not captured):
        - prompt_tokens:           int, from usage block
        - completion_tokens:       int, from usage block
        - total_tokens:            int, from usage block
        - ttft_seconds:            float, time to first generated token
        - decode_time_seconds:     float, time from first token to last token
        - e2e_latency_seconds:     float, total wall time
        - n_output_chunks:         int, number of streaming chunks received
        - itl_values_ms:           list[float], inter-token latencies in ms
        - itl_mean_ms:             float
        - itl_p50_ms:              float
        - itl_p99_ms:              float
        - decode_tput_tps:         float, completion_tokens / decode_time
        - prefill_tput_tps:        float, prompt_tokens / ttft
        - finish_reason:           str, "stop" / "length" / "error"
    """
    return getattr(_TLS, "last_metrics", None)


def _set_last_call_metrics(metrics: Optional[Dict[str, Any]]) -> None:
    _TLS.last_metrics = metrics


@dataclass(frozen=True)
class VLLMConfig:
    """Connection + sampling config for vLLM."""
    base_url: str
    model_name: str
    timeout_seconds: int = 180
    temperature: float = 0.0
    top_p: float = 1.0
    max_tokens: int = 1024


def health_check(config: VLLMConfig) -> Tuple[bool, str]:
    """Quick "is vLLM up and serving the right model?" check.
    Hits /models endpoint, confirms the configured model is listed.
    """
    url = f"{config.base_url}/models"
    try:
        session = _get_session()
        r = session.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        served = [m["id"] for m in data.get("data", [])]
        if config.model_name in served:
            return True, f"vLLM up, serving: {served}"
        return False, (
            f"vLLM up but model '{config.model_name}' not served. "
            f"Available: {served}"
        )
    except requests.exceptions.ConnectionError:
        return False, f"Could not connect to {url}. Is vLLM running?"
    except Exception as e:
        return False, f"Health check failed: {e}"


# ---------------------------------------------------------------------------
# Stats helpers — kept small to avoid pulling in numpy/scipy here
# ---------------------------------------------------------------------------
def _percentile(values: List[float], pct: float) -> float:
    """Linear-interpolation percentile. pct in [0, 100]. Returns 0.0 for empty list."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _mean(values: List[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


# ---------------------------------------------------------------------------
# Core streaming request
# ---------------------------------------------------------------------------
def run_vllm(
    config: VLLMConfig,
    prompt: str,
    *,
    system_prompt: Optional[str] = None,
) -> Tuple[Optional[str], Optional[float], Optional[str]]:
    """Single streaming call to vLLM's chat completions endpoint.

    Captures per-token timing for TTFT / ITL / decode-throughput
    analysis. Stores per-request metrics in thread-local storage;
    read them via get_last_call_metrics().

    Returns (text, api_time_seconds, error) — same 3-tuple as the prior
    non-streaming version, for backward compatibility with callers
    that don't care about the breakdown.
    """
    url = f"{config.base_url}/chat/completions"

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": config.model_name,
        "messages": messages,
        "temperature": config.temperature,
        "top_p": config.top_p,
        "max_tokens": config.max_tokens,
        "stream": True,
        # Ask vLLM to include `usage` in the final stream chunk. Without
        # this option, vLLM omits usage from streaming responses.
        "stream_options": {"include_usage": True},
    }

    # Reset thread-local metrics so a failed call doesn't leak stale state
    _set_last_call_metrics(None)

    session = _get_session()
    accumulated_text_parts: List[str] = []
    token_arrival_times: List[float] = []
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    finish_reason: Optional[str] = None
    first_token_time: Optional[float] = None
    n_chunks_received = 0
    request_start = time.perf_counter()

    try:
        with session.post(
            url,
            json=payload,
            timeout=config.timeout_seconds,
            stream=True,
        ) as response:

            if response.status_code != 200:
                # Read body for error detail before giving up
                err_body = response.text[:400]
                api_time = time.perf_counter() - request_start
                return None, api_time, f"HTTP {response.status_code}: {err_body}"

            for raw_line in response.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                # SSE chunks are prefixed with "data: "
                if not raw_line.startswith("data: "):
                    continue
                data_str = raw_line[len("data: "):].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk_obj = json.loads(data_str)
                except json.JSONDecodeError:
                    logger.debug("Skipping malformed SSE chunk: %r", data_str[:120])
                    continue

                now = time.perf_counter()

                # Each "data" chunk has choices[0].delta.content (the new token text)
                # except the final usage-only chunk which has no choices.
                choices = chunk_obj.get("choices") or []
                if choices:
                    delta = (choices[0].get("delta") or {})
                    content = delta.get("content")
                    if content:
                        if first_token_time is None:
                            first_token_time = now
                        accumulated_text_parts.append(content)
                        token_arrival_times.append(now)
                        n_chunks_received += 1
                    # finish_reason appears on the last "content" chunk
                    fr = choices[0].get("finish_reason")
                    if fr is not None:
                        finish_reason = fr

                # The final usage-only chunk (when include_usage=True)
                usage = chunk_obj.get("usage")
                if usage:
                    prompt_tokens = usage.get("prompt_tokens")
                    completion_tokens = usage.get("completion_tokens")
                    total_tokens = usage.get("total_tokens")

        request_end = time.perf_counter()
        api_time = request_end - request_start
        text = "".join(accumulated_text_parts).strip()

        # Compute derived metrics
        metrics: Dict[str, Any] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "finish_reason": finish_reason,
            "n_output_chunks": n_chunks_received,
            "e2e_latency_seconds": api_time,
        }

        if first_token_time is not None:
            ttft = first_token_time - request_start
            metrics["ttft_seconds"] = ttft
            decode_time = request_end - first_token_time
            metrics["decode_time_seconds"] = decode_time

            # Inter-token latencies in milliseconds (gaps between consecutive arrivals)
            itl_ms: List[float] = []
            for i in range(1, len(token_arrival_times)):
                gap_ms = (token_arrival_times[i] - token_arrival_times[i - 1]) * 1000.0
                itl_ms.append(gap_ms)
            metrics["itl_values_ms"] = itl_ms
            metrics["itl_mean_ms"] = _mean(itl_ms)
            metrics["itl_p50_ms"] = _percentile(itl_ms, 50)
            metrics["itl_p99_ms"] = _percentile(itl_ms, 99)

            # Throughputs
            if completion_tokens and decode_time > 0:
                metrics["decode_tput_tps"] = completion_tokens / decode_time
            if prompt_tokens and ttft > 0:
                metrics["prefill_tput_tps"] = prompt_tokens / ttft

        _set_last_call_metrics(metrics)

        # ── Langfuse trace ────────────────────────────────────────────────
        lf = _get_langfuse()
        if lf is not None:
            try:
                trace = lf.trace(
                    name="vllm-generation",
                    metadata={
                        "model":       config.model_name,
                        "base_url":    config.base_url,
                        "temperature": config.temperature,
                        "max_tokens":  config.max_tokens,
                    },
                )
                trace.generation(
                    name="chat-completion",
                    model=config.model_name,
                    input=messages,
                    output="".join(accumulated_text_parts),
                    usage={
                        "input":  prompt_tokens or 0,
                        "output": completion_tokens or 0,
                        "total":  total_tokens or 0,
                        "unit":   "TOKENS",
                    },
                    metadata={
                        "ttft_seconds":        metrics.get("ttft_seconds"),
                        "e2e_latency_seconds": metrics.get("e2e_latency_seconds"),
                        "decode_tput_tps":     metrics.get("decode_tput_tps"),
                        "prefill_tput_tps":    metrics.get("prefill_tput_tps"),
                        "itl_mean_ms":         metrics.get("itl_mean_ms"),
                        "itl_p99_ms":          metrics.get("itl_p99_ms"),
                        "finish_reason":       finish_reason,
                    },
                )
                lf.flush()
            except Exception as _lf_err:
                logger.debug("Langfuse trace failed (non-fatal): %s", _lf_err)
        # ── End Langfuse trace ────────────────────────────────────────────

        if not text:
            return None, api_time, f"Empty response (finish_reason={finish_reason})"
        return text, api_time, None

    except requests.exceptions.Timeout:
        return None, float(config.timeout_seconds), f"timeout after {config.timeout_seconds}s"
    except Exception as e:
        api_time = time.perf_counter() - request_start
        return None, api_time, str(e)
