"""Prompt template loader.

Prompts live as plain .txt files in prompts/. Keep them out of Python so:
  - Prompt iteration doesn't trigger code reviews
  - Different prompt versions are git-diffable as text
  - A non-engineer can edit them
"""

from __future__ import annotations

from pathlib import Path


def load_prompt(prompt_path: str | Path) -> str:
    """Load a prompt template's raw text. Caller fills placeholders."""
    p = Path(prompt_path)
    if not p.exists():
        raise FileNotFoundError(f"Prompt template not found: {p.resolve()}")
    return p.read_text(encoding="utf-8")


def fill_prompt(template: str, text: str) -> str:
    """Substitute {text} placeholder. Uses str.replace, not str.format,
    so braces inside the text don't crash."""
    return template.replace("{text}", text)
