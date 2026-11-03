"""Subprocess wrapper for the local Ollama CLI.

Direct port of the validated AITS legislation `run_ollama` helper. Calls
`ollama run <model> -- <prompt>` via subprocess and returns the structured
result so callers don't have to handle exceptions.

Why subprocess and not the HTTP API:
  - The legislation project uses subprocess
  - The AITS server has the binary at /usr/local/bin/ollama
  - Not every environment runs `ollama serve`
  - The subprocess form works whether or not a daemon is up
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def run_ollama(
    ollama_path: str,
    model_name: str,
    prompt: str,
    timeout_seconds: int = 180,
) -> Tuple[Optional[str], Optional[float], Optional[str]]:
    """Run one Ollama CLI call.

    Returns:
        (stdout_text_or_none, api_time_seconds_or_none, error_message_or_none)

    api_time measures subprocess runtime end to end (includes model load
    on cold starts, which is the cost the caller actually experiences).
    Errors come back as a string so callers can route them to error
    parquets without having to handle exceptions.
    """
    try:
        start = time.perf_counter()
        result = subprocess.run(
            [ollama_path, "run", model_name, "--", prompt],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_seconds,
        )
        api_time = time.perf_counter() - start

        if result.returncode != 0:
            return None, api_time, (result.stderr.strip() or "Ollama returned non-zero exit code")

        return (result.stdout.strip() if result.stdout else ""), api_time, None

    except subprocess.TimeoutExpired:
        return None, float(timeout_seconds), f"timeout after {timeout_seconds}s"
    except Exception as e:
        return None, None, str(e)
