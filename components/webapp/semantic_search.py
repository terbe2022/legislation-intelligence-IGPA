"""
semantic_search.py
==================
Thin, reusable wrapper around the Explorer vector index (FAISS + a
SentenceTransformer embedding of the initial Llama feature-extraction text),
so pages other than the Explorer can do true semantic bill search.

The artifacts live in Azure Blob (metadata.parquet + faiss_index.bin +
config.json) and are catalogued per session in data_catalog.EXPLORER_URLS —
including the current 104th GA (session 2176). The index row order == FAISS
vec_id, and metadata carries the bill_id per row.

Everything is lazy and guarded: importing this module never loads the model,
and if faiss/sentence-transformers/requests aren't available (or a download
fails) the callers get an error string and can fall back to literal matching.
Nothing here can break a page that imports it.
"""
from __future__ import annotations

import os
import tempfile

import pandas as pd
import streamlit as st

from data_catalog import DATA_CATALOG, EMBEDDING_MODEL

_IMPORT_ERRORS: list[str] = []
try:
    import faiss
    _HAS_FAISS = True
except Exception as _e:                              # noqa: BLE001
    _HAS_FAISS = False
    _IMPORT_ERRORS.append(f"faiss: {type(_e).__name__}")
try:
    from sentence_transformers import SentenceTransformer
    _HAS_ST = True
except Exception as _e:                              # noqa: BLE001
    _HAS_ST = False
    _IMPORT_ERRORS.append(f"sentence_transformers: {type(_e).__name__}")
try:
    import requests as _requests
    _HAS_REQUESTS = True
except Exception as _e:                              # noqa: BLE001
    _HAS_REQUESTS = False
    _IMPORT_ERRORS.append(f"requests: {type(_e).__name__}")


def available() -> bool:
    """True if the runtime has the pieces needed to do semantic search."""
    return _HAS_FAISS and _HAS_ST and _HAS_REQUESTS


@st.cache_resource(show_spinner="Loading semantic search index…")
def load_index(session_id: int):
    """Load (index, meta_df, model) for a session. Cached once per process, so
    the model + FAISS index download happens on the first semantic query only.
    Returns (index, meta, model, error_str). On any failure the first three are
    None and error_str explains why.
    """
    if not available():
        return None, None, None, ("Semantic search dependencies missing: "
                                  + ", ".join(_IMPORT_ERRORS or ["unknown"]))
    explorer = DATA_CATALOG.get("Illinois", {}).get(session_id, {}).get("explorer", {})
    meta_url = explorer.get("metadata")
    faiss_url = explorer.get("faiss")
    cfg_url = explorer.get("config")
    if not (meta_url and faiss_url and cfg_url):
        return None, None, None, (f"No semantic index configured for session "
                                  f"{session_id}.")
    try:
        cfg = _requests.get(cfg_url, timeout=30).json()

        meta = pd.read_parquet(meta_url)
        # FAISS stores vectors in vec_id order; enforce row position == vec_id.
        if "vec_id" in meta.columns:
            meta["vec_id"] = pd.to_numeric(meta["vec_id"], errors="coerce")
            meta = meta.sort_values("vec_id", kind="stable").reset_index(drop=True)
        else:
            meta = meta.reset_index(drop=True)
        if "bill_id" in meta.columns:
            meta["bill_id"] = meta["bill_id"].astype(str)

        _r = _requests.get(faiss_url, timeout=180)
        _r.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as _tmp:
            _tmp.write(_r.content)
            _tmp_path = _tmp.name
        index = faiss.read_index(_tmp_path)
        os.unlink(_tmp_path)

        if index.ntotal != len(meta):
            return None, None, None, (
                f"Index/metadata out of sync ({index.ntotal:,} vectors vs "
                f"{len(meta):,} rows).")

        model = SentenceTransformer(cfg.get("embedding_model_name", EMBEDDING_MODEL))
        return index, meta, model, None
    except Exception as e:                           # noqa: BLE001
        return None, None, None, f"{type(e).__name__}: {e}"


def rank_bill_ids(query: str, session_id: int, top_k: int = 400):
    """Semantic-rank bills for a free-text query.

    Returns (ordered_bill_ids, score_by_bill_id, error_str). ordered_bill_ids is
    most-similar-first; score_by_bill_id maps bill_id -> cosine similarity. On
    failure returns ([], {}, error_str) so callers fall back to literal search.
    """
    query = (query or "").strip()
    if not query:
        return [], {}, "empty query"
    index, meta, model, err = load_index(session_id)
    if err or index is None:
        return [], {}, err or "index unavailable"
    try:
        q_vec = model.encode([query], normalize_embeddings=True,
                             convert_to_numpy=True).astype("float32")
        k = max(1, min(index.ntotal, top_k))
        scores, ids = index.search(q_vec, k)
        ids, scores = ids[0], scores[0]
        n_rows = len(meta)
        ordered, score_map = [], {}
        for _i, _s in zip(ids, scores):
            _i = int(_i)
            if 0 <= _i < n_rows:
                _bid = str(meta.iloc[_i].get("bill_id", "")) if "bill_id" in meta.columns else ""
                if _bid and _bid not in score_map:
                    ordered.append(_bid)
                    score_map[_bid] = float(_s)
        return ordered, score_map, None
    except Exception as e:                           # noqa: BLE001
        return [], {}, f"{type(e).__name__}: {e}"
