"""Stage 4: Standardizer — Per-Bill Feature Aggregation and Standardization.

PRODUCTION VERSION — collapses per-chunk extractor rows into per-bill gold rows,
applies deterministic standardization to all 15 extracted fields, and writes
to the gold tier with last-write-wins upsert semantics per bill_id.

Architecture
============
Input  : silver features parquet (one row per chunk, 41 columns)
Output : gold standardized parquet (one row per bill)

Two-tier approach:
  Tier 1 — Deterministic (no model calls):
    - Per-bill aggregation: concat summaries, union lists, etc.
    - impact_rating     → 4-level severity enum + numeric score, max across chunks
    - original_law      → boolean (regex fast-path, vLLM for ambiguous)
    - category/subcategory → static 24-cluster taxonomy (hardcoded map)
    - ideological_alignment → token-based collapse → Left/Right/Center/Nonpartisan/Mixed

  Tier 2 — vLLM classification (same service as extractor, port 8000):
    - key_provisions, policy_domain, intended_beneficiaries
    - legislative_strategy, increasing_aspects, decreasing_aspects
    - motivation, intent, legislative_goal
    Uses the curated ACTOR_TAXONOMY for beneficiaries and LLaMA calls
    for the other free-text fields.

Gold path convention:
    legislation/features_standardized/<profile>/session=<N>/_features_standardized.parquet

Idempotency / upsert:
    Existing gold rows for bills in this run are replaced; all other bills
    from prior runs are preserved. This is correct for all lifecycle states:
    - Status 1/2 (active): re-run replaces previous standardized row
    - Status 3/4/5/6 (terminal): ingestion skips these so they never appear
      in fresh feature runs; their gold rows persist untouched

Column naming convention (matches historical standardized outputs):
    <field>_raw          — original LLaMA text, concatenated across chunks
    <field>_standardized — cleaned/classified label
    impact_rating_clean  — normalized severity string
    impact_rating_score  — numeric 0–3
    original_law_standardized — boolean
    category_main_label  — condensed top-level from 24-cluster taxonomy
    category_sub_label   — detailed subcategory from 24-cluster taxonomy
    ideological_alignment_simplified — Left/Right/Center/Nonpartisan/Mixed/Unspecified
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Optional

import pandas as pd
import requests

from services.azure_blob_service import BlobClient, TIER_BRONZE, TIER_SILVER, TIER_GOLD
from utils.timing import StageTimer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gold path helper
# ---------------------------------------------------------------------------

def gold_std_session_path(session_id: int, profile: str | None = None) -> str:
    """Return blob path for standardized gold parquet for one session."""
    prof = profile or "default"
    return f"legislation/features_standardized/{prof}/session={session_id}/_features_standardized.parquet"


# ---------------------------------------------------------------------------
# Gold output schema
# ---------------------------------------------------------------------------

GOLD_COLS = [
    # Identifiers
    "bill_id", "session_id", "bill_number",
    # Provenance
    "n_chunks", "n_chunks_ok", "n_chunks_partial",
    "standardized_at", "profile",
    "status_date", "chamber", "title", "last_action_date",

    # Summary
    "llama_summary_raw",

    # Core features (raw + standardized)
    "legislative_goal_raw",       "legislative_goal_standardized",
    "policy_domain_raw",          "policy_domain_standardized",
    "key_provisions_raw",         "key_provisions_standardized",
    "intended_beneficiaries_raw", "intended_beneficiaries_standardized",
    "legislative_strategy_raw",   "legislative_strategy_standardized",
    "intent_raw",                 "intent_standardized",
    "motivation_raw",             "motivation_standardized",

    # Impact
    "potential_impact_raw",
    "impact_rating_clean",
    "impact_rating_score",
    "impact_rating_standardized",
    "increasing_aspects_raw",     "increasing_aspects_standardized",
    "decreasing_aspects_raw",     "decreasing_aspects_standardized",
    "original_law_raw",           "original_law_standardized",

    # Category taxonomy (derived from category_subcategory)
    "category_subcategory_raw",
    "category_main_label",
    "category_sub_label",
    "category_main_keywords",
    "category_sub_keywords",

    # Ideology
    "ideological_alignment_raw",
    "ideological_alignment_simplified",
]


# ===========================================================================
# ── TIER 1: DETERMINISTIC STANDARDIZERS ──────────────────────────────────
# ===========================================================================

# ---------------------------------------------------------------------------
# Impact Rating
# ---------------------------------------------------------------------------

_IMPACT_RANK = {
    "not impactful": 0,
    "slightly impactful": 1,
    "moderately impactful": 2,
    "very impactful": 3,
}
_RANK_TO_LABEL = {0: "Not Impactful", 1: "Slightly Impactful",
                  2: "Moderately Impactful", 3: "Very Impactful"}
_IMPACT_PLACEHOLDERS = {"", "nan", "none", "not specified", "unknown", "n/a", "na"}


def _standardize_impact_one(value) -> tuple[Optional[str], Optional[int]]:
    """Return (label, score) for a single impact_rating cell (may contain '|||')."""
    if pd.isna(value):
        return None, None
    s = str(value).strip()
    if not s:
        return None, None

    parts = [p.strip() for p in s.split("|||")]
    best_rank = None
    for p in parts:
        p_norm = re.sub(r"\s+", " ", p.lower().strip())
        if p_norm in _IMPACT_PLACEHOLDERS:
            continue
        if p_norm in _IMPACT_RANK:
            r = _IMPACT_RANK[p_norm]
        elif "very" in p_norm and "impact" in p_norm:
            r = 3
        elif "moderate" in p_norm and "impact" in p_norm:
            r = 2
        elif "slight" in p_norm and "impact" in p_norm:
            r = 1
        elif ("not" in p_norm or "no" in p_norm) and "impact" in p_norm:
            r = 0
        else:
            continue
        best_rank = r if best_rank is None else max(best_rank, r)

    if best_rank is None:
        return None, None
    return _RANK_TO_LABEL[best_rank], best_rank


def aggregate_impact_rating(series: pd.Series) -> tuple[Optional[str], Optional[int]]:
    """Max-severity across all chunks."""
    best_rank = None
    for val in series:
        _, r = _standardize_impact_one(val)
        if r is not None:
            best_rank = r if best_rank is None else max(best_rank, r)
    if best_rank is None:
        return None, None
    return _RANK_TO_LABEL[best_rank], best_rank


# ---------------------------------------------------------------------------
# Original Law (boolean classifier)
# ---------------------------------------------------------------------------

_ORIG_LAW_PLACEHOLDER_RE = re.compile(
    r"^\s*(none specified|none|no prior law|no existing law|not specified|"
    r"n/?a|null|na|unknown|no prior statute|no current law|no applicable law|"
    r"new law)\s*$",
    flags=re.IGNORECASE,
)
_ORIG_LAW_TRUE_HINT_RE = re.compile(
    r"\b(ilcs|statute|section|sec\.|code|public act|p\.a\.|act\b|article\b|"
    r"amend|amends|amended|repeal|repeals|effective date|compiled statutes|"
    r"administrative code|usc\b|cfr\b)\b",
    flags=re.IGNORECASE,
)


def classify_original_law_batch(
    texts: list[str],
    vllm_url: str,
    model_name: str,
    timeout: int = 180,
) -> list[bool]:
    """
    Classify a batch of 'original_law' strings as True (references existing law)
    or False (new law / none specified).

    Fast-path first (regex), vLLM only for ambiguous cases.
    Returns a list[bool] aligned to `texts`.
    """
    results: list[Optional[bool]] = [None] * len(texts)
    ambiguous_indices: list[int] = []

    for i, raw in enumerate(texts):
        s = str(raw).strip() if raw and not pd.isna(raw) else ""
        if not s or _ORIG_LAW_PLACEHOLDER_RE.match(s):
            results[i] = False
        elif _ORIG_LAW_TRUE_HINT_RE.search(s):
            results[i] = True
        else:
            ambiguous_indices.append(i)

    if ambiguous_indices:
        logger.info("original_law: %d ambiguous rows → vLLM batch", len(ambiguous_indices))
        ambiguous_texts = [texts[i] for i in ambiguous_indices]
        numbered = "\n".join(f"{j+1}. {t}" for j, t in enumerate(ambiguous_texts))
        prompt = (
            "Task: For each item, decide if an existing law is referenced.\n\n"
            "Definition:\n"
            "- True: references/cites/amends an existing law/statute/code/section "
            "(e.g., ILCS, Act, Code, Section, Public Act).\n"
            "- False: says none/no prior law/not specified, or describes a new policy "
            "without referencing an existing law.\n\n"
            "Return ONLY in this format (one per line):\n"
            "1: True/False\n2: True/False\n...\n\n"
            f"Items:\n{numbered}"
        )
        preds = _call_vllm_chat(prompt, vllm_url, model_name, timeout=timeout)
        parsed = _parse_numbered_bool(preds, len(ambiguous_texts))
        for j, i in enumerate(ambiguous_indices):
            results[i] = parsed[j]

    return [bool(r) if r is not None else False for r in results]


def _call_vllm_chat(
    prompt: str,
    vllm_url: str,
    model_name: str,
    timeout: int = 180,
) -> str:
    """POST to vLLM /v1/chat/completions. Returns model text or '' on error."""
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 512,
        "temperature": 0.0,
    }
    try:
        resp = requests.post(
            f"{vllm_url}/v1/chat/completions",
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning("vLLM call failed: %s", e)
        return ""


def _parse_numbered_bool(text: str, n: int) -> list[bool]:
    """Parse '1: True\n2: False\n...' output. Pad with False if short."""
    results: list[bool] = []
    for line in text.strip().splitlines():
        m = re.match(r"^\s*\d+\s*:\s*(True|False)\s*$", line, re.IGNORECASE)
        if m:
            results.append(m.group(1).lower() == "true")
    while len(results) < n:
        results.append(False)
    return results[:n]


# ---------------------------------------------------------------------------
# Category / Subcategory taxonomy (24-cluster static map)
# ---------------------------------------------------------------------------

_CLUSTER_LABEL_MAP_24 = {
    0:  "Public Safety & Gun Policy",
    1:  "Consumer Protection & Financial Regulation",
    2:  "Environment, Conservation & Water/Waste",
    3:  "Memorials, Tributes & Condolence Resolutions",
    4:  "Taxation & Property Tax Policy",
    5:  "Local Government & Municipal Policy",
    6:  "Education Funding & Higher Education",
    7:  "Transportation, Vehicle & Road Safety",
    8:  "K–12 School Policy & Teacher Policy",
    9:  "Pension Systems & Public Employee Retirement",
    10: "Insurance Regulation & Healthcare Finance",
    11: "Civil Rights & Disability Rights Policy",
    12: "Reproductive Rights & Gun Rights (Rights Issues)",
    13: "Child Welfare, Social Services & Family Support",
    14: "Criminal Justice & Corrections Reform",
    15: "Government Operations & Administrative Procedure",
    16: "Economic Development & Community Development",
    17: "Labor, Employment & Workforce Policy",
    18: "Mental Health & Developmental Disabilities",
    19: "Public Health & Healthcare Services",
    20: "Energy, Utilities & Renewable Energy",
    21: "Tobacco, Cannabis, Gaming & Licensing Regulation",
    22: "Elections, Voting Rights & Election Reform",
    23: "General Government, State Administration & Finance",
}

_CONDENSED_MAP = {
    "Public Safety & Gun Policy":                              "Public Safety & Criminal Justice",
    "Consumer Protection & Financial Regulation":              "Business & Commerce",
    "Environment, Conservation & Water/Waste":                 "Environment & Natural Resources",
    "Memorials, Tributes & Condolence Resolutions":            "General Legislation / Misc",
    "Taxation & Property Tax Policy":                          "Finance & Taxation",
    "Local Government & Municipal Policy":                     "Local Government & Municipal Affairs",
    "Education Funding & Higher Education":                    "Education",
    "Transportation, Vehicle & Road Safety":                   "Transportation & Infrastructure",
    "K–12 School Policy & Teacher Policy":                     "Education",
    "Pension Systems & Public Employee Retirement":            "Labor & Workforce",
    "Insurance Regulation & Healthcare Finance":               "Healthcare & Regulation",
    "Civil Rights & Disability Rights Policy":                 "Civil Rights & Social Equity",
    "Reproductive Rights & Gun Rights (Rights Issues)":        "Civil Rights & Social Equity",
    "Child Welfare, Social Services & Family Support":         "Social Services & Human Welfare",
    "Criminal Justice & Corrections Reform":                   "Public Safety & Criminal Justice",
    "Government Operations & Administrative Procedure":        "Government & Public Administration",
    "Economic Development & Community Development":            "Economic Development",
    "Labor, Employment & Workforce Policy":                    "Labor & Workforce",
    "Mental Health & Developmental Disabilities":              "Health & Human Services",
    "Public Health & Healthcare Services":                     "Health & Human Services",
    "Energy, Utilities & Renewable Energy":                    "Energy & Infrastructure",
    "Tobacco, Cannabis, Gaming & Licensing Regulation":        "Business & Commerce",
    "Elections, Voting Rights & Election Reform":              "Elections & Government Reform",
    "General Government, State Administration & Finance":      "Government & Public Administration",
}

# Keyword patterns for each cluster (used for TF-IDF-free assignment)
# Maps cluster_id → tuple of (keyword_patterns, subcategory_label)
_CATEGORY_PATTERNS: list[tuple[list[str], int]] = [
    # (patterns_list, cluster_id)
    (["gun", "firearm", "weapon", "public safety", "crime", "shooting"],         0),
    (["consumer", "financial regulation", "fraud", "predatory", "protection"],   1),
    (["environment", "conservation", "water", "waste", "pollution", "landfill"], 2),
    (["memorial", "tribute", "condolence", "resolution", "designate"],           3),
    (["tax", "property tax", "taxation", "levy", "revenue", "assessment"],       4),
    (["municipal", "local government", "village", "city", "township", "county"], 5),
    (["higher education", "university", "college", "tuition", "financial aid"],  6),
    (["transportation", "vehicle", "road", "highway", "traffic", "motor"],       7),
    (["school", "teacher", "k-12", "elementary", "secondary", "education"],      8),
    (["pension", "retirement", "public employee", "benefit", "annuity"],         9),
    (["insurance", "premium", "coverage", "healthcare finance", "claim"],       10),
    (["civil rights", "disability", "discrimination", "equal", "ada"],          11),
    (["reproductive", "abortion", "gun rights", "second amendment", "rights"],  12),
    (["child welfare", "foster", "adoption", "dcfs", "family support"],         13),
    (["criminal justice", "corrections", "prison", "sentencing", "parole"],     14),
    (["government operations", "administrative", "procedure", "rulemaking"],    15),
    (["economic development", "community development", "enterprise zone"],       16),
    (["labor", "employment", "workforce", "minimum wage", "workers"],           17),
    (["mental health", "developmental disabilities", "behavioral health"],       18),
    (["public health", "healthcare services", "medicaid", "disease", "health"], 19),
    (["energy", "utility", "renewable", "solar", "wind", "electric"],           20),
    (["cannabis", "tobacco", "gaming", "license", "liquor", "lottery"],         21),
    (["election", "voting", "ballot", "campaign", "voter registration"],        22),
    (["state administration", "general assembly", "finance", "budget"],         23),
]


def classify_category(raw_text: str) -> tuple[str, str, str, str]:
    """
    Assign category_main_label, category_sub_label, main_keywords, sub_keywords
    from the raw category_subcategory text using keyword matching.

    Returns (main_label, sub_label, main_keywords, sub_keywords).
    Falls back to cluster 23 (General) if no keywords match.
    """
    if not raw_text or pd.isna(raw_text):
        return "General Legislation / Misc", "General Government, State Administration & Finance", "", ""

    text_lower = str(raw_text).lower()
    best_cluster = 23  # default fallback
    best_score = 0

    for patterns, cluster_id in _CATEGORY_PATTERNS:
        score = sum(1 for p in patterns if p in text_lower)
        if score > best_score:
            best_score = score
            best_cluster = cluster_id

    sub_label = _CLUSTER_LABEL_MAP_24[best_cluster]
    main_label = _CONDENSED_MAP.get(sub_label, "General Legislation / Misc")

    # Extract keywords from raw text (simple stopword filter)
    _STOPWORDS = {
        "a", "an", "the", "and", "or", "but", "if", "for", "to", "of",
        "in", "on", "at", "by", "from", "with", "as", "is", "are", "was",
        "were", "be", "it", "this", "that", "not", "no", "any", "all",
        "may", "shall", "would", "could", "into", "over", "under", "per",
    }
    words = re.sub(r"[^a-z0-9\s]", " ", text_lower).split()
    seen: set[str] = set()
    kws = [w for w in words if w not in _STOPWORDS and len(w) > 2 and w not in seen
           and not seen.add(w)]  # type: ignore[func-returns-value]
    main_kws = ", ".join(kws[:8])
    sub_kws = ", ".join(kws[:12])

    return main_label, sub_label, main_kws, sub_kws


# ---------------------------------------------------------------------------
# Ideology standardizer
# ---------------------------------------------------------------------------

_IDEOLOGY_KEYWORDS = [
    "progressive", "liberal", "conservative", "republican",
    "democrat", "moderate", "neutral", "bipartisan", "centrist",
    "independent", "left", "right",
]
_CANONICAL_TERMS = {
    "progressive": "Progressive", "liberal": "Liberal",
    "conservative": "Conservative", "republican": "Republican",
    "democratic": "Democratic", "democrat": "Democratic",
    "moderate": "Moderate", "neutral": "Neutral",
    "bipartisan": "Bipartisan", "centrist": "Moderate",
    "independent": "Moderate", "left": "Liberal", "right": "Conservative",
}
_NONE_PATTERNS_IDEO = re.compile(
    r"^\s*(none|none specified|none listed|unknown|not specified|"
    r"no clear ideology|n/?a|na)\s*$",
    re.IGNORECASE,
)
_LEFT = {"Progressive", "Liberal", "Democratic"}
_RIGHT = {"Conservative", "Republican"}
_CENTER = {"Moderate"}
_NONPARTISAN = {"Neutral", "Bipartisan"}


def _normalize_ideology(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).lower().strip())


def _extract_ideology_tokens(text: str) -> list[str]:
    normalized = _normalize_ideology(text)
    parts = re.split(r"[,;/|&]+", normalized)
    return [p.strip() for p in parts
            if any(k in p for k in _IDEOLOGY_KEYWORDS)]


def standardize_ideology(raw_text: str) -> str:
    """Collapse raw ideology text → Left/Right/Center/Nonpartisan/Mixed/Unspecified."""
    if pd.isna(raw_text):
        return "Unspecified"
    normalized = _normalize_ideology(str(raw_text))
    if not normalized or _NONE_PATTERNS_IDEO.match(normalized):
        return "Unspecified"

    tokens = _extract_ideology_tokens(str(raw_text))
    if not tokens:
        return "Unspecified"

    detected: set[str] = set()
    for token in tokens:
        for key, label in _CANONICAL_TERMS.items():
            if key in token:
                detected.add(label)

    if not detected:
        return "Unspecified"
    if detected & _LEFT and not detected & _RIGHT:
        return "Left"
    if detected & _RIGHT and not detected & _LEFT:
        return "Right"
    if detected & _CENTER and not (detected & _LEFT or detected & _RIGHT):
        return "Center"
    if detected & _NONPARTISAN and not (detected & _LEFT or detected & _RIGHT):
        return "Nonpartisan"
    return "Mixed"


# ===========================================================================
# ── TIER 2: vLLM CLASSIFICATION FOR FREE-TEXT FIELDS ─────────────────────
# ===========================================================================

# Fields routed through vLLM classification
_VLLM_FIELDS = [
    "key_provisions",
    "policy_domain",
    "intended_beneficiaries",
    "legislative_strategy",
    "motivation",
    "intent",
    "legislative_goal",
]
# increasing_aspects and decreasing_aspects passthrough unchanged —
# the synthesizer already produces the correct structured format
# ("increases: X for [group]; expands: Y for [group]")
# Running them through vLLM collapses that structure into a short label
# These fields get a special normalization prompt instead of passthrough or label collapse
# The synthesizer already produces correct format for multi-chunk bills
# but single-chunk passthrough bills have plain prose that needs restructuring
_NORMALIZE_FIELDS = ["increasing_aspects", "decreasing_aspects"]

# Field-specific prompts
_FIELD_PROMPTS = {
    "key_provisions": (
        "You are classifying Illinois legislative bill text.\n"
        "Given the following 'Key Provisions' text, return ONE concise category label "
        "(5–10 words max) describing the dominant legislative theme.\n"
        "Return ONLY the label, no explanation.\n\n"
        "Text: {text}\n\nLabel:"
    ),
    "policy_domain": (
        "You are classifying Illinois legislative bill text.\n"
        "Given the following 'Policy Domain' text, return ONE concise policy domain name "
        "(3–6 words ideal, 10 max).\n"
        "Return ONLY the label, no explanation.\n\n"
        "Text: {text}\n\nLabel:"
    ),
    "intended_beneficiaries": (
        "You are classifying Illinois legislative bill text.\n"
        "Given the following 'Intended Beneficiaries' text, return ONE concise label "
        "(5–10 words max) describing the main beneficiary group.\n"
        "Do NOT use the words 'intended beneficiaries'.\n"
        "Return ONLY the label, no explanation.\n\n"
        "Text: {text}\n\nLabel:"
    ),
    "legislative_strategy": (
        "You are classifying Illinois legislative bill text.\n"
        "Given the following 'Legislative Strategy' text, return ONE concise label "
        "(5–10 words max) describing the type of legislative strategy "
        "(e.g., mandate, pilot program, tax incentive, task force).\n"
        "Return ONLY the label, no explanation.\n\n"
        "Text: {text}\n\nLabel:"
    ),
    "increasing_aspects": (
        "You are a legislative data formatter.\n"
        "Convert the following text about what a bill increases or expands into "
        "a semicolon-separated list using format: action: object for [group].\n"
        "Rules: start each item with an action verb and colon "
        "(increases:, expands:, creates:, improves:, provides:, establishes:, grants:, extends:).\n"
        "End each item with for [group] identifying who benefits.\n"
        "Separate items with semicolons.\n"
        "If no increasing aspects exist, return None.\n"
        "Return ONLY the formatted list, no explanation.\n\n"
        "Example: increases: mental health funding for veterans; expands: eligibility for low-income households\n\n"
        "Text: {text}\n\nFormatted list:"
    ),
    "decreasing_aspects": (
        "You are a legislative data formatter.\n"
        "Convert the following text about what a bill decreases or restricts into "
        "a semicolon-separated list using format: action: object for [group].\n"
        "Rules: start each item with an action verb and colon "
        "(reduces:, restricts:, eliminates:, decreases:, limits:, removes:, repeals:, cuts:, lowers:).\n"
        "End each item with for [group] identifying who is restricted or loses something.\n"
        "Separate items with semicolons.\n"
        "If no decreasing aspects exist, return None.\n"
        "Return ONLY the formatted list, no explanation.\n\n"
        "Example: reduces: licensing fees for small businesses; restricts: access to benefits for undocumented residents\n\n"
        "Text: {text}\n\nFormatted list:"
    ),
    "motivation": (
        "You are classifying Illinois legislative bill text.\n"
        "Given the following 'Motivation' text, return ONE concise label "
        "(5–10 words max) describing the primary legislative motivation.\n"
        "Return ONLY the label, no explanation.\n\n"
        "Text: {text}\n\nLabel:"
    ),
    "intent": (
        "You are classifying Illinois legislative bill text.\n"
        "Given the following 'Intent' text, return ONE concise label "
        "(5–10 words max) describing the primary legislative intent.\n"
        "Return ONLY the label, no explanation.\n\n"
        "Text: {text}\n\nLabel:"
    ),
    "legislative_goal": (
        "You are classifying Illinois legislative bill text.\n"
        "Given the following 'Legislative Goal' text, return ONE concise label "
        "(5–10 words max) describing the primary legislative goal.\n"
        "Return ONLY the label, no explanation.\n\n"
        "Text: {text}\n\nLabel:"
    ),
}

_FIELD_NONE_VALUES = {
    "none specified", "none", "not specified", "no clear goal", "no clear intent",
    "no clear strategy", "unknown", "n/a", "na", "none listed",
    "no motivation specified", "no intent specified", "no legislative goal specified",
}


def classify_field_vllm(
    field: str,
    raw_text: str,
    vllm_url: str,
    model_name: str,
    timeout: int = 300,
) -> str:
    """Classify a single free-text field using vLLM. Returns label string."""
    if not raw_text or pd.isna(raw_text):
        return f"No {field.replace('_', ' ').title()} Specified"

    cleaned = str(raw_text).strip()
    if cleaned.lower() in _FIELD_NONE_VALUES or len(cleaned) < 5:
        return f"No {field.replace('_', ' ').title()} Specified"

    # Truncate very long inputs (keep first 800 chars — enough for classification)
    if len(cleaned) > 800:
        cleaned = cleaned[:800] + "..."

    prompt_template = _FIELD_PROMPTS.get(field, _FIELD_PROMPTS["key_provisions"])
    prompt = prompt_template.format(text=cleaned)

    label = _call_vllm_chat(prompt, vllm_url, model_name, timeout=timeout).strip()

    # Sanitize output — reject multi-line or very long responses
    if label:
        label = label.splitlines()[0].strip()
        label = re.sub(r"\s+", " ", label)
        if len(label) > 120:
            label = label[:120]

    return label if label else f"No {field.replace('_', ' ').title()} Specified"


# ===========================================================================
# ── PER-BILL AGGREGATION ──────────────────────────────────────────────────
# ===========================================================================

def _concat_unique(series: pd.Series, sep: str = " ||| ") -> str:
    """Concatenate non-null, non-placeholder values across chunks, deduplicating."""
    seen: set[str] = set()
    parts: list[str] = []
    for val in series:
        if pd.isna(val):
            continue
        s = str(val).strip()
        if not s or s.lower() in _FIELD_NONE_VALUES:
            continue
        norm = re.sub(r"\s+", " ", s.lower())
        if norm not in seen:
            seen.add(norm)
            parts.append(s)
    return sep.join(parts) if parts else ""


def aggregate_bill_features(group: pd.DataFrame) -> dict:
    """
    Collapse all chunk rows for one bill into a single aggregated dict.
    Input: DataFrame of chunk rows for one bill (sorted by chunk_index).
    Output: dict of aggregated raw values ready for standardization.
    """
    # Synthesized input is already one row per bill — no chunk_index
    if "chunk_index" in group.columns:
        g = group.sort_values("chunk_index")
    else:
        g = group.copy()

    n_total = len(g)
    if "parse_status" in g.columns:
        n_ok = (g["parse_status"] == "OK").sum()
        n_partial = (g["parse_status"] == "PARTIAL").sum()
    else:
        n_ok = int(g["n_chunks_ok"].iloc[0]) if "n_chunks_ok" in g.columns else len(g)
        n_partial = int(g["n_chunks_partial"].iloc[0]) if "n_chunks_partial" in g.columns else 0



    return {
        "bill_id":       g["bill_id"].iloc[0],
        "session_id":    g["session_id"].iloc[0] if "session_id" in g.columns else None,
        "bill_number":   g["bill_number"].iloc[0],
        "n_chunks":      n_total,
        "n_chunks_ok":   int(n_ok),
        "n_chunks_partial": int(n_partial),

        # Summary: concatenate all chunk summaries in order
        "llama_summary_raw":           _concat_unique(g["llama_summary"], sep=" "),

        # Core features: union across chunks
        "legislative_goal_raw":        _concat_unique(g["legislative_goal"]),
        "policy_domain_raw":           _concat_unique(g["policy_domain"]),
        "key_provisions_raw":          _concat_unique(g["key_provisions"]),
        "intended_beneficiaries_raw":  _concat_unique(g["intended_beneficiaries"]),
        "legislative_strategy_raw":    _concat_unique(g["legislative_strategy"]),
        "intent_raw":                  _concat_unique(g["intent"]),
        "motivation_raw":              _concat_unique(g["motivation"]),

        # Impact: keep all values (max-severity applied in standardize step)
        "potential_impact_raw":        _concat_unique(g["potential_impact"]),
        "_impact_rating_series":       list(g["impact_rating"].dropna()),
        "increasing_aspects_raw":      _concat_unique(g["increasing_aspects"]),
        "decreasing_aspects_raw":      _concat_unique(g["decreasing_aspects"]),
        "original_law_raw":            _concat_unique(g["original_law"]),

        # Category: use first non-null value (usually consistent across chunks)
        "category_subcategory_raw":    _concat_unique(g["category_subcategory"]),

        # Ideology: use first non-null, or concat
        "ideological_alignment_raw":   _concat_unique(g["ideological_alignment"]),
        # Manifest fields — take first non-null value
        "status_date":      g["status_date"].dropna().iloc[0] if "status_date" in g.columns and g["status_date"].notna().any() else None,
        "chamber":          g["chamber"].dropna().iloc[0] if "chamber" in g.columns and g["chamber"].notna().any() else None,
        "title":            g["title"].dropna().iloc[0] if "title" in g.columns and g["title"].notna().any() else None,
        "last_action_date": g["last_action_date"].dropna().iloc[0] if "last_action_date" in g.columns and g["last_action_date"].notna().any() else None,
    }


# ===========================================================================
# ── FULL BILL STANDARDIZATION ─────────────────────────────────────────────
# ===========================================================================

def standardize_bill(
    agg: dict,
    vllm_url: str,
    model_name: str,
    run_tier2: bool = True,
) -> dict:
    """Apply all standardizers to an aggregated bill dict. Returns gold row dict."""
    row = dict(agg)  # copy

    # --- Impact rating ---
    impact_label, impact_score = aggregate_impact_rating(
        pd.Series(agg.pop("_impact_rating_series", []))
    )
    row["impact_rating_clean"] = impact_label
    row["impact_rating_score"] = impact_score
    row["impact_rating_standardized"] = impact_label  # same label for now

    # --- Original law boolean ---
    orig_raw = agg.get("original_law_raw", "")
    bool_results = classify_original_law_batch(
        [orig_raw], vllm_url=vllm_url, model_name=model_name
    )
    row["original_law_standardized"] = bool_results[0]

    # --- Category taxonomy ---
    cat_raw = agg.get("category_subcategory_raw", "")
    main_label, sub_label, main_kws, sub_kws = classify_category(cat_raw)
    row["category_main_label"] = main_label
    row["category_sub_label"] = sub_label
    row["category_main_keywords"] = main_kws
    row["category_sub_keywords"] = sub_kws

    # --- Ideology ---
    ideo_raw = agg.get("ideological_alignment_raw", "")
    row["ideological_alignment_simplified"] = standardize_ideology(ideo_raw)

    # --- Tier 2: vLLM classification for free-text fields ---
    # Normalize fields — run through vLLM to enforce structured format
    # (increases: X for [group]; expands: Y for [group])
    # This handles both prose passthrough bills and already-structured multi-chunk bills

    if run_tier2:
        for field in _NORMALIZE_FIELDS:
            raw_key = f"{field}_raw"
            std_key = f"{field}_standardized"
            raw_val = agg.get(raw_key, "")
            # Skip if already None/empty
            if not raw_val or str(raw_val).strip().lower() in (
                "", "none", "none.", "missing text", "missing text.",
                "no increasing aspects specified",
                "no decreasing aspects specified"
            ):
                row[std_key] = None
            else:
                row[std_key] = classify_field_vllm(
                    field=field,
                    raw_text=raw_val,
                    vllm_url=vllm_url,
                    model_name=model_name,
                )
        for field in _VLLM_FIELDS:
            raw_key = f"{field}_raw"
            std_key = f"{field}_standardized"
            row[std_key] = classify_field_vllm(
                field=field,
                raw_text=agg.get(raw_key, ""),
                vllm_url=vllm_url,
                model_name=model_name,
            )
    else:
        for field in _NORMALIZE_FIELDS:
            raw_val = agg.get(f"{field}_raw", "")
            row[f"{field}_standardized"] = raw_val if raw_val else None
        for field in _VLLM_FIELDS:
            row[f"{field}_standardized"] = None

    return row


# ===========================================================================
# ── SESSION DRIVER ────────────────────────────────────────────────────────
# ===========================================================================

def _run_session(
    session_id: int,
    cfg: dict,
    blob: BlobClient,
    dry: bool = False,
) -> dict:
    timer = StageTimer()
    profile = cfg.get("profile") or None
    vllm_url = cfg.get("vllm_url", "http://localhost:8000")
    model_name = cfg.get("model_name", "llama31-8b-awq")
    run_tier2 = cfg.get("run_tier2", True)
    bill_limit = cfg.get("bill_limit")

    result = {
        "session_id":         session_id,
        "profile":            profile,
        "status":             "SUCCESS",
        "bills_input":        0,
        "bills_standardized": 0,
        "bills_failed":       0,
        "bills_skipped":      0,
        "skip_reason":        None,
        "error":              None,
        "metrics":            None,
    }

    # ── Read silver features ──────────────────────────────────────────────
    with timer.phase("read_silver_features"):
        # Determine features path from profile
        feat_profile = cfg.get("features_profile") or profile or "llama31_1200"
        # Support both raw features and synthesized features paths
        if feat_profile.startswith("synthesized/"):
            synth_profile = feat_profile.replace("synthesized/", "", 1)
            features_path = (
                f"legislation/features_synthesized/{synth_profile}"
                f"/session={session_id}/_synthesized.parquet"
            )
        else:
            features_path = (
                f"legislation/features_{feat_profile}/session={session_id}/_features.parquet"
            )
        if not blob.blob_exists(TIER_SILVER, features_path):
            logger.warning(
                "No silver features found for session=%d profile=%s at %s",
                session_id, feat_profile, features_path,
            )
            result["status"] = "SKIPPED"
            result["skip_reason"] = "no_silver_features"
            result["metrics"] = timer.snapshot()
            return result

        df_features = blob.read_parquet(TIER_SILVER, features_path)
        logger.info(
            "Loaded %d feature rows for session=%d (profile=%s)",
            len(df_features), session_id, feat_profile,
        )

    # ── Enrich with manifest metadata (status_date, chamber, title) ──────
    with timer.phase("read_manifest"):
        manifest_path = f"legislation/session={session_id}/_manifest.parquet"
        if blob.blob_exists(TIER_BRONZE, manifest_path):
            df_manifest = blob.read_parquet(TIER_BRONZE, manifest_path)
            manifest_cols = [c for c in ["bill_id","status_date","chamber","title","last_action_date"]
                             if c in df_manifest.columns]
            df_manifest = df_manifest[manifest_cols].copy()
            df_manifest["bill_id"] = df_manifest["bill_id"].astype(str)
            df_features["bill_id"] = df_features["bill_id"].astype(str)
            df_features = df_features.merge(df_manifest, on="bill_id", how="left")
            logger.info("Joined manifest: added %s", manifest_cols[1:])

    # ── Filter to OK/PARTIAL only ─────────────────────────────────────────
    if "parse_status" in df_features.columns:
        df_valid = df_features[
            df_features["parse_status"].isin(["OK", "PARTIAL"])
        ].copy()
    else:
        df_valid = df_features.copy()

    if df_valid.empty:
        logger.warning("No valid (OK/PARTIAL) feature rows for session=%d", session_id)
        result["status"] = "SKIPPED"
        result["skip_reason"] = "no_valid_features"
        result["metrics"] = timer.snapshot()
        return result

    # Ensure session_id column exists (may be missing in older extracts)
    if "session_id" not in df_valid.columns:
        df_valid["session_id"] = session_id

    # ── Load existing gold (for upsert) ───────────────────────────────────
    with timer.phase("read_gold_existing"):
        gold_path = gold_std_session_path(session_id, profile=profile)
        if blob.blob_exists(TIER_GOLD, gold_path):
            df_gold_existing = blob.read_parquet(TIER_GOLD, gold_path)
            logger.info(
                "Loaded %d existing gold rows for session=%d", len(df_gold_existing), session_id
            )
        else:
            df_gold_existing = pd.DataFrame()

    # ── Determine which bills to process ──────────────────────────────────
    all_bill_ids = df_valid["bill_id"].astype(str).unique().tolist()
    result["bills_input"] = len(all_bill_ids)

    if bill_limit:
        all_bill_ids = all_bill_ids[: int(bill_limit)]
        logger.info("Bill limit applied: processing first %d bills", bill_limit)

    # ── Per-bill processing (concurrent) ────────────────────────────────────
    gold_rows: list[dict] = []
    now_ts = datetime.utcnow().isoformat(timespec="seconds")
    concurrency = int(cfg.get("concurrency", 1))
    logger.info("Standardizer concurrency: %d", concurrency)

    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    _progress_lock = threading.Lock()
    _counters = {"ok": 0, "fail": 0}

    def _process_bill(bill_id: str):
        bill_group = df_valid[df_valid["bill_id"].astype(str) == bill_id]
        agg = aggregate_bill_features(bill_group)
        gold_row = standardize_bill(
            agg,
            vllm_url=vllm_url,
            model_name=model_name,
            run_tier2=run_tier2,
        )
        gold_row["standardized_at"] = now_ts
        gold_row["profile"] = profile or "default"
        for col in GOLD_COLS:
            if col not in gold_row:
                gold_row[col] = None
        return {c: gold_row.get(c) for c in GOLD_COLS}

    n_total = len(all_bill_ids)
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(_process_bill, str(bid)): bid
            for bid in all_bill_ids
        }
        for idx, future in enumerate(as_completed(futures), start=1):
            bid = futures[future]
            try:
                row = future.result()
                gold_rows.append(row)
                with _progress_lock:
                    _counters["ok"] += 1
                    result["bills_standardized"] += 1
                    timer.add_count("bills_standardized", 1)
            except Exception as e:
                logger.warning("Bill %s standardization failed: %s", bid, e, exc_info=True)
                with _progress_lock:
                    _counters["fail"] += 1
                    result["bills_failed"] += 1
            if idx % 100 == 0:
                logger.info(
                    "Standardizer progress: %d/%d (ok=%d failed=%d)",
                    idx, n_total, _counters["ok"], _counters["fail"],
                )


    # ── Upsert into gold ──────────────────────────────────────────────────
    with timer.phase("write_gold"):
        if not gold_rows:
            logger.info("No gold rows produced for session=%d", session_id)
        else:
            df_new = pd.DataFrame(gold_rows, columns=GOLD_COLS)
            new_bill_ids = set(df_new["bill_id"].astype(str).tolist())

            if not df_gold_existing.empty:
                # Drop existing rows for bills we just re-standardized
                df_keep = df_gold_existing[
                    ~df_gold_existing["bill_id"].astype(str).isin(new_bill_ids)
                ].copy()
                # Ensure schema alignment
                for col in GOLD_COLS:
                    if col not in df_keep.columns:
                        df_keep[col] = None
                df_keep = df_keep[GOLD_COLS]
                df_combined = pd.concat([df_keep, df_new], ignore_index=True)
                logger.info(
                    "Upserted %d new rows, retained %d unchanged rows → %d total",
                    len(df_new), len(df_keep), len(df_combined),
                )
            else:
                df_combined = df_new
                logger.info("Writing %d gold rows (first run)", len(df_combined))

            if dry:
                logger.info(
                    "[DRY] Would write %d total gold rows -> %s", len(df_combined), gold_path
                )
            else:
                blob.write_parquet(df_combined, TIER_GOLD, gold_path)
                logger.info(
                    "Wrote %d total gold rows -> %s", len(df_combined), gold_path
                )

    result["metrics"] = timer.snapshot()

    # ── SLO check ─────────────────────────────────────────────────────────
    slo_cfg = cfg.get("slos", {})
    if slo_cfg:
        from utils.slo import check_slos, format_slo_report
        slo_results = check_slos(result["metrics"], slo_cfg)
        result["slos"] = slo_results
        report = format_slo_report(slo_results)
        if report:
            for line in report.splitlines():
                logger.info(line)

    m = result["metrics"]
    logger.info(
        "Standardizer session=%d profile=%s done: wall=%.2fs | "
        "bills=%d ok=%d failed=%d",
        session_id, profile or "default",
        m["wall_seconds"],
        result["bills_input"],
        result["bills_standardized"],
        result["bills_failed"],
    )
    return result


# ===========================================================================
# ── PUBLIC ENTRY POINT ────────────────────────────────────────────────────
# ===========================================================================

def run(config: dict) -> dict:
    """Run the standardizer stage. Called by pipeline.py."""
    cfg = config.get("standardizer") or config
    sessions = list(cfg.get("sessions", []))
    mode = cfg.get("mode", "live")
    dry = (mode == "dry")

    if not sessions:
        return {
            "stage": "standardizer", "status": "FAILED",
            "input_count": 0, "output_count": 0,
            "skip_reason": None,
            "error": "No sessions configured under standardizer.sessions",
            "details": {},
        }

    blob = BlobClient()
    per_session: dict = {}
    total_in = 0
    total_out = 0
    any_failed = False

    for sid in sessions:
        logger.info("=" * 60)
        logger.info("STANDARDIZER session=%d mode=%s", sid, mode)
        logger.info("=" * 60)
        try:
            r = _run_session(int(sid), cfg, blob, dry=dry)
            per_session[sid] = r
            total_in  += r.get("bills_input", 0)
            total_out += r.get("bills_standardized", 0)
            if r.get("status") == "FAILED":
                any_failed = True
        except Exception as e:
            logger.exception("Session %d FAILED: %s", sid, e)
            per_session[sid] = {"status": "FAILED", "error": str(e)}
            any_failed = True

    return {
        "stage":         "standardizer",
        "status":        "FAILED" if any_failed else "SUCCESS",
        "input_count":   total_in,
        "output_count":  total_out,
        "skip_reason":   None,
        "error":         None if not any_failed else "see per-session details",
        "details": {
            "mode":        mode,
            "sessions":    sessions,
            "per_session": per_session,
        },
    }


# ===========================================================================
# ── CLI entry point ───────────────────────────────────────────────────────
# ===========================================================================

if __name__ == "__main__":
    import sys
    from utils.config_loader import load_config

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("azure.core").setLevel(logging.WARNING)
    logging.getLogger("azure.storage").setLevel(logging.WARNING)

    cfg = load_config("configs/standardizer.yaml")
    result = run({"standardizer": cfg})
    print()
    print("=" * 60)
    print("RESULT")
    print("=" * 60)
    for k, v in result.items():
        if k != "details":
            print(f"  {k}: {v}")
    for sid, sr in result["details"]["per_session"].items():
        m = sr.get("metrics", {})
        print(
            f"  session={sid}: status={sr.get('status')} "
            f"bills_in={sr.get('bills_input', 0)} "
            f"bills_ok={sr.get('bills_standardized', 0)} "
            f"bills_failed={sr.get('bills_failed', 0)} "
            f"wall={m.get('wall_seconds', 0):.2f}s"
        )
    sys.exit(0 if result["status"] == "SUCCESS" else 1)
