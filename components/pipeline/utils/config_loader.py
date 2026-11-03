"""Config loading helpers.

Direct port of the legislative_pipeline_1 shared_lib/config_loader.py with
small hardening: typed return, explicit encoding, helper for nested keys,
and auto-loading of .env on first env-var read.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

# Load .env once at import time so any code path that calls get_env_or_fail
# sees the values, regardless of whether something else (e.g. BlobClient)
# has loaded dotenv yet. This is idempotent — calling load_dotenv multiple
# times is fine.
load_dotenv()


def load_config(config_path: str | Path) -> dict:
    """Load a YAML config file as a dict.

    Raises FileNotFoundError with the resolved path so misconfigurations
    are easy to debug from logs.
    """
    p = Path(config_path)
    if not p.exists():
        raise FileNotFoundError(f"Config not found: {p.resolve()}")
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg


def get_env_or_fail(env_name: str) -> str:
    """Read an env var. Raise EnvironmentError if missing or empty.

    Empty strings count as missing — a blank API key is never useful.
    """
    v = os.getenv(env_name)
    if not v:
        raise EnvironmentError(f"Missing required environment variable: {env_name}")
    return v


def get_nested(cfg: dict, *keys: str, default: Optional[Any] = None) -> Any:
    """Safely walk a nested config dict.

    Usage:
        get_nested(cfg, "stage_1_ingestion", "legiscan", "timeout_seconds", default=60)

    Returns `default` if any key in the path is missing or if a non-dict is
    encountered before reaching the last key.
    """
    cur = cfg
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur
