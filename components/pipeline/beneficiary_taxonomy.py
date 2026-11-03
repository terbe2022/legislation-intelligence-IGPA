"""
beneficiary_taxonomy.py
========================
Single source of truth for mapping free-text beneficiary / target-group
strings onto the 2-level taxonomy (category -> subcategory -> terms).

Used by:
  - stages/embedder.py        (to populate metadata.parquet beneficiary columns)
  - Explorer app              (filters: category / subcategory)
  - build_graph_v1.py         (network graph node categorization)

By importing the SAME matcher everywhere, beneficiary categorization stays
consistent across the trends dashboard, explorer, and network graph.

Taxonomy file structure (Structure B):
    {
      "individuals": {
        "general_individuals": ["people", "citizens", ...],
        "demographic_groups":  ["youth", "seniors", ...]
      },
      "families": { "family_units": [...] },
      ...
    }
"""
from __future__ import annotations

import json
import os
import re
from difflib import SequenceMatcher
from functools import lru_cache


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_taxonomy(path: str) -> dict:
    """
    Returns a flat lookup:
        { term_lower: {"category": cat, "subcategory": sub, "display": term} }

    Preserves BOTH category and subcategory (unlike the older network-graph
    loader which dropped subcategory).
    """
    if not path or not os.path.exists(path):
        return {}

    with open(path, encoding="utf-8") as f:
        tax = json.load(f)

    mapping: dict[str, dict] = {}

    if isinstance(tax, dict):
        for cat_name, cat_body in tax.items():
            if isinstance(cat_body, dict):
                # Structure B: {category: {subcategory: [terms]}}
                for subcat_name, members in cat_body.items():
                    if not isinstance(members, list):
                        continue
                    for m in members:
                        label = m if isinstance(m, str) else str(m)
                        if label:
                            mapping[label.lower().strip()] = {
                                "category":    cat_name,
                                "subcategory": subcat_name,
                                "display":     label,
                            }
            elif isinstance(cat_body, list):
                # Structure A: {category: [terms]} — no subcategory
                for m in cat_body:
                    label = m if isinstance(m, str) else str(m)
                    if label:
                        mapping[label.lower().strip()] = {
                            "category":    cat_name,
                            "subcategory": "",
                            "display":     label,
                        }
    return mapping


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
# "No beneficiaries", "missing text", etc. should resolve to a clean sentinel
_NULL_TOKENS = {
    "", "none", "none.", "n/a", "na", "unknown", "unspecified",
    "no beneficiaries", "no beneficiaries specified",
    "no intended beneficiaries", "no intended beneficiaries specified",
    "missing text", "missing text.",
}


def _clean(raw: str) -> str:
    return re.sub(r"\s+", " ", str(raw).lower().strip())


def match_one(raw: str, taxonomy: dict, threshold: float = 0.62) -> dict:
    """
    Map a single raw beneficiary phrase to {category, subcategory, display, match_type}.

    Tiers: null-check -> exact -> substring -> token-overlap -> fuzzy ratio.
    Returns category='uncategorized' if nothing clears the threshold.
    """
    raw_norm = _clean(raw)

    if raw_norm in _NULL_TOKENS:
        return {"category": "none", "subcategory": "none",
                "display": "No Beneficiaries", "match_type": "null"}

    if not taxonomy:
        return {"category": "uncategorized", "subcategory": "",
                "display": raw.strip().title(), "match_type": "no_taxonomy"}

    # 1) exact
    if raw_norm in taxonomy:
        e = taxonomy[raw_norm]
        return {**e, "match_type": "exact"}

    # 2) substring containment (handles plurals/suffixes/qualifiers)
    #    prefer the LONGEST taxonomy key contained in the phrase (most specific)
    best_sub = None
    best_sub_len = 0
    for key, entry in taxonomy.items():
        if len(key) < 4:
            continue  # skip tiny keys to avoid spurious substring hits
        if key in raw_norm or raw_norm in key:
            if len(key) > best_sub_len:
                best_sub_len, best_sub = len(key), entry
    if best_sub:
        return {**best_sub, "match_type": "substring"}

    # 3) token overlap — any taxonomy key whose words all appear in the phrase
    raw_tokens = set(raw_norm.split())
    best_tok = None
    best_tok_overlap = 0
    for key, entry in taxonomy.items():
        ktoks = set(key.split())
        if not ktoks:
            continue
        overlap = len(ktoks & raw_tokens)
        if overlap > best_tok_overlap and overlap >= max(1, len(ktoks) // 2):
            best_tok_overlap, best_tok = overlap, entry
    if best_tok:
        return {**best_tok, "match_type": "token"}

    # 4) fuzzy ratio fallback
    best_score, best_entry = 0.0, None
    for key, entry in taxonomy.items():
        score = SequenceMatcher(None, raw_norm, key).ratio()
        if score > best_score:
            best_score, best_entry = score, entry
    if best_score >= threshold and best_entry:
        return {**best_entry, "match_type": f"fuzzy_{best_score:.2f}"}

    # nothing matched
    return {"category": "uncategorized", "subcategory": "",
            "display": raw.strip().title(), "match_type": "unmatched"}


def categorize_beneficiary(raw: str, taxonomy: dict) -> tuple[str, str, str]:
    """
    Convenience wrapper returning the PRIMARY (category, subcategory, display)
    for one phrase. Kept for backward compatibility / single-value columns.
    Prefers a real taxonomy hit over 'uncategorized'/'none'.
    """
    cats = categorize_beneficiary_multi(raw, taxonomy)
    # cats is a list of dicts; pick the first real one
    for c in cats:
        if c["category"] not in ("uncategorized", "none"):
            return c["category"], c.get("subcategory", ""), c["display"]
    c0 = cats[0]
    return c0["category"], c0.get("subcategory", ""), c0["display"]


def categorize_beneficiary_multi(raw: str, taxonomy: dict) -> list[dict]:
    """
    Returns ALL distinct category/subcategory matches for a (possibly compound)
    beneficiary phrase. Compound phrases like "Healthcare Professionals and
    Patients" or "Local Governments and Taxpayers" map to MULTIPLE categories.

    Returns a list of {category, subcategory, display, match_type} dicts,
    de-duplicated by (category, subcategory). Always returns at least one entry.
    """
    if not raw or not str(raw).strip():
        return [{"category": "none", "subcategory": "none",
                 "display": "No Beneficiaries", "match_type": "null"}]

    # Split on commas / semicolons / slashes / " and " / ampersands
    parts = re.split(r"[;,/]| and | & ", str(raw))
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        return [{"category": "none", "subcategory": "none",
                 "display": "No Beneficiaries", "match_type": "null"}]

    seen = set()
    results = []
    has_real = False
    for p in parts:
        m = match_one(p, taxonomy)
        key = (m["category"], m.get("subcategory", ""))
        if key in seen:
            continue
        seen.add(key)
        results.append(m)
        if m["category"] not in ("uncategorized", "none"):
            has_real = True

    # If we found real matches, drop the none/uncategorized noise from the list
    if has_real:
        results = [r for r in results
                   if r["category"] not in ("uncategorized", "none")]

    return results or [{"category": "uncategorized", "subcategory": "",
                        "display": str(raw).strip().title(),
                        "match_type": "unmatched"}]


@lru_cache(maxsize=1)
def _cached_taxonomy(path: str) -> tuple:
    """Internal: cache the loaded taxonomy by path (returns items tuple for hashability)."""
    return tuple(load_taxonomy(path).items())


def get_taxonomy(path: str) -> dict:
    """Load (and cache) the taxonomy from a path. Safe to call repeatedly."""
    return dict(_cached_taxonomy(path))


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    tax_path = sys.argv[1] if len(sys.argv) > 1 else "intended_beneficiary_taxonomy2.json"
    tax = get_taxonomy(tax_path)
    print(f"Loaded {len(tax)} taxonomy terms from {tax_path}\n")

    tests = [
        "Illinois residents with mental health needs.",
        "Low-income families and child care providers.",
        "Healthcare Professionals and Patients",
        "Local Governments and Taxpayers",
        "Cannabis Industry and Transporting Organizations",
        "Veterans and their families",
        "Public School Students and Educators",
        "No Beneficiaries",
        "Agricultural Producers and Grain Industry Stakeholders",
    ]
    for t in tests:
        multi = categorize_beneficiary_multi(t, tax)
        labels = ", ".join(f"{m['category']}/{m.get('subcategory','')}" for m in multi)
        print(f"  {t[:50]:52s} -> {labels}")
