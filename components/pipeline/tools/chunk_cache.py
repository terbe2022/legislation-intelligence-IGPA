"""In-memory extraction cache keyed on chunk content hash.

Why this exists:
  Bills share boilerplate. Statutory citation blocks, "AN ACT concerning..."
  preambles, common amendatory phrasing, effective-date clauses — these
  appear identically across many bills. If we extract from the same chunk
  text twice in a sweep, we're burning GPU time on a result we already have.

  The cache is at the *extraction* level. The chunker still produces all
  chunks (deterministic, cheap). The extractor checks the cache before
  each LLM call; if the chunk text has been extracted before in this run,
  the cached result is reused.

Why in-memory only (not persisted):
  Across sweep runs, models and prompts can change. A cached result from
  Mistral_500 is not valid for Qwen_500. Within a single profile run,
  prompt and model are fixed — so the cache is valid within a profile-run.

  Scope: one cache per profile run. Wiped between profiles.

Thread-safety:
  The sweep harness runs at concurrency=4. Multiple threads may call
  cache.get() and cache.put() simultaneously. The cache uses an
  internal lock for all state mutations. The lock is held only for
  dict operations, not for the LLM call itself, so it doesn't
  serialize the actual work.

Usage (in extractor.py, between chunk read and backend.run):

    from tools.chunk_cache import ChunkCache

    cache = ChunkCache()
    # per-chunk:
    cached = cache.get(chunk_text)
    if cached is not None:
        features = cached
        # record cache hit, skip the LLM call
    else:
        features = call_llm(chunk_text)
        cache.put(chunk_text, features)

    # End of profile run:
    stats = cache.stats()
    log.info(
        f"Cache: {stats['hits']} hits / {stats['misses']} misses "
        f"({stats['hit_rate']:.1%}), {stats['unique_chunks']} unique chunks"
    )
"""

from __future__ import annotations

import hashlib
import logging
import threading
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


class ChunkCache:
    """SHA-256 hash-keyed cache for chunk extraction results.

    Thread-safe: all state mutations are protected by an internal lock.
    The lock is held only for dict operations, never during the LLM
    call itself, so it doesn't serialize the actual extraction work.
    """

    def __init__(self) -> None:
        self._cache: Dict[str, Any] = {}
        self._hits = 0
        self._misses = 0
        self._lock = threading.Lock()

    @staticmethod
    def hash_chunk(text: str) -> str:
        """Hash chunk text. SHA-256 is overkill for collision avoidance
        but cheap enough that the safety margin is free.
        """
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def get(self, chunk_text: str) -> Optional[Any]:
        """Return cached result for this chunk text, or None.
        Records hit/miss for stats. Thread-safe.
        """
        key = self.hash_chunk(chunk_text)
        with self._lock:
            if key in self._cache:
                self._hits += 1
                return self._cache[key]
            self._misses += 1
            return None

    def put(self, chunk_text: str, result: Any) -> None:
        """Store extraction result for this chunk text. Thread-safe."""
        key = self.hash_chunk(chunk_text)
        with self._lock:
            self._cache[key] = result

    def stats(self) -> Dict[str, Any]:
        """Return summary stats. Thread-safe."""
        with self._lock:
            total = self._hits + self._misses
            return {
                "hits": self._hits,
                "misses": self._misses,
                "lookups": total,
                "unique_chunks": len(self._cache),
                "hit_rate": (self._hits / total) if total > 0 else 0.0,
            }

    def reset(self) -> None:
        """Clear cache state. Called between profile runs."""
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0
