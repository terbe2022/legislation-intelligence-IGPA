"""Stage 5: Embedder — Semantic Vector Index Builder.

Reads gold standardized features, joins with bronze manifest for ILGA
metadata, embeds bill summaries using sentence-transformers, and writes:

  1. faiss_index.bin  — FAISS IndexFlatIP (384-dim, cosine similarity)
  2. metadata.parquet — Per-bill metadata with Explorer app column names
  3. config.json      — Matches existing Explorer config format

Output path: legislation/explorer/<profile>/session=<N>/

Column mapping (gold → Explorer):
    llama_summary_raw                → Llama Summary
    category_main_label              → category_std
    category_sub_label               → subcategory_std
    policy_domain_standardized       → policy_domain_final
    intended_beneficiaries_standardized → intended_beneficiaries_final
    legislative_strategy_standardized → legislative_strategy_std
    legislative_goal_standardized    → legislative_goal_std
    intent_standardized              → intent_std
    increasing_aspects_standardized  → increasing_aspects_std
    decreasing_aspects_standardized  → decreasing_aspects_std
    motivation_standardized          → motivation_std
    impact_rating_standardized       → impact_rating_std
    impact_rating_score              → impact_rating_score
    potential_impact_raw             → Potential Impact

ILGA fields from bronze manifest:
    title, description, status_date, status_desc,
    chamber, state_link → ftp_url
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
import json as _json
from beneficiary_taxonomy import get_taxonomy, categorize_beneficiary_multi
import yaml
import yaml

import numpy as np
import pandas as pd

from services.azure_blob_service import BlobClient, TIER_BRONZE, TIER_GOLD
from utils.timing import StageTimer

logger = logging.getLogger(__name__)

_faiss = None
_ST = None

def _import_faiss():
    global _faiss
    if _faiss is None:
        import faiss as f; _faiss = f
    return _faiss

def _import_st():
    global _ST
    if _ST is None:
        from sentence_transformers import SentenceTransformer as S; _ST = S
    return _ST


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------

def explorer_path(session_id: int, profile: str, filename: str) -> str:
    return f"legislation/explorer/{profile}/session={session_id}/{filename}"


# ---------------------------------------------------------------------------
# Column rename map: gold → Explorer app
# ---------------------------------------------------------------------------

GOLD_TO_EXPLORER = {
    "bill_id":                              "bill_id",
    "bill_number":                          "bill_number",
    "session_id":                           "session",
    "n_chunks":                             "n_chunks",
    "llama_summary_raw":                    "Llama Summary",
    "category_main_label":                  "category_std",
    "category_sub_label":                   "subcategory_std",
    "policy_domain_standardized":           "policy_domain_final",
    "intended_beneficiaries_standardized":  "intended_beneficiaries_final",
    "legislative_strategy_standardized":    "legislative_strategy_std",
    "legislative_goal_standardized":        "legislative_goal_std",
    "intent_standardized":                  "intent_std",
    "increasing_aspects_standardized":      "increasing_aspects_std",
    "decreasing_aspects_standardized":      "decreasing_aspects_std",
    "motivation_standardized":              "motivation_std",
    "motivation_raw":                       "motivation_raw",
    "intent_raw":                           "intent_raw",
    "legislative_strategy_raw":             "legislative_strategy_raw",
    "impact_rating_standardized":           "impact_rating_std",
    "impact_rating_score":                  "impact_rating_score",
    "potential_impact_raw":                 "Potential Impact",
    "original_law_standardized":            "original_law_std",
    "ideological_alignment_simplified":     "ideological_alignment",
    "key_provisions_standardized":          "key_provisions_std",
    "standardized_at":                      "standardized_at",
}

MANIFEST_COLS = [
    "bill_id", "title", "description", "status_date",
    "status_desc", "chamber", "state_link", "url",
    "last_action", "last_action_date", "is_terminal",
]


# ---------------------------------------------------------------------------
# Session driver
# ---------------------------------------------------------------------------

def _run_session(session_id: int, cfg: dict, blob: BlobClient, dry: bool = False) -> dict:
    timer = StageTimer()
    profile    = cfg.get("profile", "llama31_1200")
    model_name = cfg.get("model_name", "sentence-transformers/all-MiniLM-L6-v2")
    batch_size = int(cfg.get("batch_size", 64))
    index_ver  = int(cfg.get("index_version", 1))

    result = {
        "session_id": session_id, "profile": profile,
        "status": "SUCCESS", "bills_embedded": 0,
        "skip_reason": None, "error": None, "metrics": None,
    }

    # ── Read gold ────────────────────────────────────────────────────────
    with timer.phase("read_gold"):
        gold_path = (
            f"legislation/features_standardized/{profile}"
            f"/session={session_id}/_features_standardized.parquet"
        )
        if not blob.blob_exists(TIER_GOLD, gold_path):
            logger.warning("No gold features for session=%d", session_id)
            result.update({"status": "SKIPPED", "skip_reason": "no_gold_features",
                           "metrics": timer.snapshot()})
            return result
        df_gold = blob.read_parquet(TIER_GOLD, gold_path)
        logger.info("Loaded %d gold rows for session=%d", len(df_gold), session_id)

    # ── Read manifest ─────────────────────────────────────────────────────
    with timer.phase("read_manifest"):
        manifest_path = f"legislation/session={session_id}/_manifest.parquet"
        if blob.blob_exists(TIER_BRONZE, manifest_path):
            df_manifest = blob.read_parquet(TIER_BRONZE, manifest_path)
            avail = [c for c in MANIFEST_COLS if c in df_manifest.columns]
            df_manifest = df_manifest[avail].copy()
            df_manifest["bill_id"] = df_manifest["bill_id"].astype(str)
            logger.info("Loaded manifest: %d rows", len(df_manifest))
        else:
            logger.warning("No manifest for session=%d", session_id)
            df_manifest = pd.DataFrame(columns=MANIFEST_COLS)

    # ── Build metadata ────────────────────────────────────────────────────
    with timer.phase("build_metadata"):
        df_gold["bill_id"] = df_gold["bill_id"].astype(str)

        # Rename gold columns to explorer app column names
        rename_map = {
            "llama_summary_raw":                "Llama Summary",
            "category_main_label":              "category_std",
            "category_sub_label":               "subcategory_std",
            "policy_domain_standardized":       "policy_domain_final",
            "intended_beneficiaries_standardized": "intended_beneficiaries_final",
            "impact_rating_standardized":       "impact_rating_std",
        }
        df_meta = df_gold.rename(columns=rename_map)

        # ── Beneficiary taxonomy categorization (multi-category) ──────────
        # Uses the shared beneficiary_taxonomy module so the explorer, trends
        # dashboard, and network graph all categorize beneficiaries identically.
        _TAX_PATH = cfg.get("beneficiary_taxonomy_path", "intended_beneficiary_taxonomy2.json")
        _tax = get_taxonomy(_TAX_PATH)
        logger.info("Loaded beneficiary taxonomy: %d terms from %s", len(_tax), _TAX_PATH)

        _prim_cat, _prim_sub = [], []
        _all_cat, _all_sub = [], []
        for _val in df_meta["intended_beneficiaries_final"].fillna(""):
            _matches = categorize_beneficiary_multi(_val, _tax)
            # primary = first match (first-named beneficiary, real match preferred)
            _prim_cat.append(_matches[0]["category"])
            _prim_sub.append(_matches[0].get("subcategory", ""))
            # all distinct categories / subcategories as JSON strings
            _cats = []
            _subs = []
            for _m in _matches:
                if _m["category"] not in _cats:
                    _cats.append(_m["category"])
                _s = _m.get("subcategory", "")
                if _s and _s not in _subs:
                    _subs.append(_s)
            _all_cat.append(_json.dumps(_cats))
            _all_sub.append(_json.dumps(_subs))

        df_meta["beneficiary_primary_category"]    = _prim_cat
        df_meta["beneficiary_primary_subcategory"] = _prim_sub
        df_meta["beneficiary_all_categories"]      = _all_cat
        df_meta["beneficiary_all_subcategories"]   = _all_sub

        # Back-compat columns the existing explorer references
        # (primary maps to the old single-value column names)
        df_meta["intended_beneficiaries_category"]    = _prim_cat
        df_meta["intended_beneficiaries_subcategory"] = _prim_sub
        df_meta["intended_beneficiaries_keyword"]     = df_meta["intended_beneficiaries_final"]

        # Merge manifest columns if available
        if not df_manifest.empty:
            df_meta = df_meta.merge(df_manifest, on="bill_id", how="left")

        # Keep only columns the explorer needs + extras
        keep_cols = [
            "bill_id", "bill_number", "session_id", "title", "chamber",
            "status_date", "last_action_date",
            "Llama Summary",
            "category_std", "subcategory_std",
            "policy_domain_final",
            "intended_beneficiaries_final",
            "intended_beneficiaries_category",
            "intended_beneficiaries_subcategory",
            "intended_beneficiaries_keyword",
            "beneficiary_primary_category",
            "beneficiary_primary_subcategory",
            "beneficiary_all_categories",
            "beneficiary_all_subcategories",
            "impact_rating_std", "impact_rating_score",
            "ideological_alignment_simplified",
            "increasing_aspects_standardized",
            "decreasing_aspects_standardized",
            "key_provisions_standardized",
            "legislative_goal_standardized",
            "legislative_strategy_standardized",
            "intent_standardized",
            "motivation_standardized",
        ]
        # Add status_desc from manifest if present
        if "status_desc" in df_meta.columns:
            keep_cols.append("status_desc")

        keep_cols = [c for c in keep_cols if c in df_meta.columns]
        df_meta = df_meta[keep_cols].copy()

        logger.info("Metadata built: %d rows, %d cols", len(df_meta), len(df_meta.columns))

    # ── Embed ─────────────────────────────────────────────────────────────
    with timer.phase("embed"):
        ST = _import_st()
        model_name = cfg.get("model_name", "sentence-transformers/all-MiniLM-L6-v2")
        import torch as _torch
        model = ST(model_name, device=("cuda" if _torch.cuda.is_available() else "cpu"))
        texts = df_meta["Llama Summary"].fillna("").tolist()
        batch_size = cfg.get("batch_size", 64)
        embeddings = model.encode(
            texts,
            batch_size=batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        logger.info("Embeddings shape: %s", embeddings.shape)

    # ── Build FAISS index ─────────────────────────────────────────────────
    with timer.phase("build_index"):
        faiss = _import_faiss()
        dim = embeddings.shape[1]
        index = faiss.IndexFlatIP(dim)
        index.add(embeddings.astype("float32"))
        logger.info("FAISS index: %d vectors, dim=%d", index.ntotal, dim)

    # ── Write to Azure blob ───────────────────────────────────────────────
    with timer.phase("upload"):
        profile = cfg.get("features_profile", cfg.get("profile", "llama31_1200"))

        # FAISS index → bytes → blob
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
            faiss_tmp = tmp.name
        faiss.write_index(index, faiss_tmp)
        with open(faiss_tmp, "rb") as f:
            faiss_bytes = f.read()
        os.unlink(faiss_tmp)
        faiss_blob_path = explorer_path(session_id, profile, "faiss_index.bin")
        blob.write_bytes(faiss_bytes, TIER_GOLD, faiss_blob_path)
        logger.info("Uploaded faiss_index.bin -> %s", faiss_blob_path)

        # metadata parquet → blob
        meta_blob_path = explorer_path(session_id, profile, "metadata.parquet")
        blob.write_parquet(df_meta, TIER_GOLD, meta_blob_path)
        logger.info("Uploaded metadata.parquet -> %s", meta_blob_path)

        # config.json → blob
        config_data = {
            "embedding_model_name": "all-MiniLM-L6-v2",
            "text_column": "Llama Summary",
            "id_column": "bill_id",
            "embedding_dimension": int(dim),
            "top_k_default": 10,
            "session_id": session_id,
            "profile": profile,
            "bill_count": len(df_meta),
        }
        config_blob_path = explorer_path(session_id, profile, "config.json")
        blob.write_json(config_data, TIER_GOLD, config_blob_path)
        logger.info("Uploaded config.json -> %s", config_blob_path)

    result.update({
        "status": "SUCCESS",
        "metrics": timer.snapshot(),
        "bills_embedded": len(df_meta),
        "index_size": index.ntotal,
        "dim": dim,
    })
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Embedder stage")
    parser.add_argument("--dry-run", action="store_true")
    args, _ = parser.parse_known_args()

    from dotenv import load_dotenv
    load_dotenv()

    cfg_path = Path("configs/embedder.yaml")
    with open(cfg_path) as f:
        stage_cfg = yaml.safe_load(f)

    pipe_path = Path("configs/pipeline.yaml")
    with open(pipe_path) as f:
        pipe_cfg = yaml.safe_load(f)

    load_dotenv(".env")
    blob = BlobClient()

    sessions = stage_cfg.get("sessions", [])
    results = []
    for session_id in sessions:
        logger.info("=" * 60)
        logger.info("EMBEDDER session=%d", session_id)
        r = _run_session(session_id, stage_cfg, blob, dry=args.dry_run)
        results.append(r)
        logger.info(
            "  session=%d: status=%s bills_embedded=%s wall=%.2fs",
            session_id,
            r.get("status"),
            r.get("bills_embedded", "?"),
            r.get("metrics", {}).get("total_wall", 0),
        )

    failed = [r for r in results if r.get("status") not in ("SUCCESS", "SKIPPED")]
    if failed:
        logger.error("%d session(s) failed", len(failed))
        raise SystemExit(1)
    logger.info("EMBEDDER COMPLETE")


if __name__ == "__main__":
    main()


def run(cfg: dict) -> dict:
    """Standard pipeline entry point — matches extractor/synthesizer/standardizer interface."""
    blob = BlobClient()
    sessions = cfg.get("sessions", [])
    results = []
    total_embedded = 0

    for session_id in sessions:
        logger.info("=" * 60)
        logger.info("EMBEDDER session=%d", session_id)
        r = _run_session(session_id, cfg, blob)
        results.append(r)
        total_embedded += r.get("bills_embedded", 0)
        logger.info(
            "  session=%d: status=%s bills_embedded=%s",
            session_id, r.get("status"), r.get("bills_embedded", "?"),
        )

    failed = [r for r in results if r.get("status") not in ("SUCCESS", "SKIPPED")]
    return {
        "status": "FAILED" if failed else "SUCCESS",
        "output_count": total_embedded,
        "details": results,
    }
