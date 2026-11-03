"""Pluggable inference layer.

A thin router that hides whether we're calling vLLM or Ollama from the
extractor. Both backends share the same return contract:

    (response_text, api_time_seconds, error_message)

Switching backends is a config flag, not a code change. This is the
"30 minutes of insurance" we agreed on Thursday — if vLLM has output
quality issues mid-Friday, flip the YAML, fall back to Ollama, ship.

Usage in stages/extractor.py:

    backend = build_backend(cfg["extractor"])
    text, ms, err = backend.run(prompt)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

from services.ollama_service import run_ollama
from services.vllm_service import VLLMConfig, health_check, run_vllm

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backend wrappers — same shape, different implementations
# ---------------------------------------------------------------------------
class _BackendBase:
    name: str
    def run(self, prompt: str) -> Tuple[Optional[str], Optional[float], Optional[str]]:
        raise NotImplementedError
    def healthcheck(self) -> Tuple[bool, str]:
        return True, "no healthcheck implemented"


class OllamaBackend(_BackendBase):
    name = "ollama"

    def __init__(self, ollama_path: str, model_name: str, timeout_seconds: int = 180):
        self.ollama_path = ollama_path
        self.model_name = model_name
        self.timeout_seconds = timeout_seconds

    def run(self, prompt: str):
        return run_ollama(
            ollama_path=self.ollama_path,
            model_name=self.model_name,
            prompt=prompt,
            timeout_seconds=self.timeout_seconds,
        )


class VLLMBackend(_BackendBase):
    name = "vllm"

    def __init__(self, config: VLLMConfig, system_prompt: Optional[str] = None):
        self.config = config
        self.system_prompt = system_prompt

    def run(self, prompt: str):
        return run_vllm(self.config, prompt, system_prompt=self.system_prompt)

    def healthcheck(self):
        return health_check(self.config)


# ---------------------------------------------------------------------------
# Factory: read config, return a configured backend
# ---------------------------------------------------------------------------
def build_backend(extractor_cfg: dict) -> _BackendBase:
    """Build the inference backend from the extractor config block.

    Expected config shape:

        backend: vllm           # 'vllm' or 'ollama'

        vllm:
          base_url: http://localhost:8000/v1
          model_name: llama3.2
          timeout_seconds: 180
          temperature: 0.0
          max_tokens: 1024
          system_prompt: null    # optional

        ollama:
          path: /usr/local/bin/ollama
          model_name: llama3.2
          timeout_seconds: 180
    """
    backend_name = (extractor_cfg.get("backend") or "ollama").lower()

    if backend_name == "vllm":
        v = extractor_cfg.get("vllm") or {}
        cfg = VLLMConfig(
            base_url=v.get("base_url", "http://localhost:8000/v1"),
            model_name=v.get("model_name", "llama3.2"),
            timeout_seconds=int(v.get("timeout_seconds", 180)),
            temperature=float(v.get("temperature", 0.0)),
            top_p=float(v.get("top_p", 1.0)),
            max_tokens=int(v.get("max_tokens", 1024)),
        )
        backend = VLLMBackend(cfg, system_prompt=v.get("system_prompt"))
        ok, msg = backend.healthcheck()
        if not ok:
            raise RuntimeError(f"vLLM health check failed: {msg}")
        logger.info("Inference backend: vLLM (%s)", msg)
        return backend

    if backend_name == "ollama":
        o = extractor_cfg.get("ollama") or {}
        backend = OllamaBackend(
            ollama_path=o.get("path", "/usr/local/bin/ollama"),
            model_name=o.get("model_name", "llama3.2"),
            timeout_seconds=int(o.get("timeout_seconds", 180)),
        )
        logger.info("Inference backend: Ollama (model=%s)", backend.model_name)
        return backend

    raise ValueError(
        f"Unknown extractor.backend: {backend_name!r}. Use 'vllm' or 'ollama'."
    )
