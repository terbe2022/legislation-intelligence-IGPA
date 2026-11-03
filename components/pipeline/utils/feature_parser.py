"""Parser for the labeled-field LLM output.

Expected output format (from prompts/feature_extraction_v1.txt):

    Llama Summary: <text>
    Legislative Goal: <text>
    Policy Domain: <comma-list>
    ...
    Impact Rating: <one of 4 categories>

Behaviors:
  - Tolerant of asterisk-wrapped labels (**Llama Summary:**)
  - Tolerant of leading dashes/bullets (- Llama Summary:)
  - Multi-line values supported (kept until next labeled line)
  - Missing field → None (not an exception)
  - Unknown extra fields → captured under 'extras' for debugging
  - Returns canonical column names (snake_case) regardless of label casing
"""

from __future__ import annotations

import re
from typing import Optional

# The canonical fields we expect. Map each to the regex-friendly label
# patterns the LLM might use. Order matters for matching priority.
FIELD_LABELS = [
    ("llama_summary",         ["Llama Summary", "Summary"]),
    ("legislative_goal",      ["Legislative Goal", "Goal"]),
    ("policy_domain",         ["Policy Domain", "Policy Domains", "Domain"]),
    ("key_provisions",        ["Key Provisions", "Provisions"]),
    ("intended_beneficiaries", ["Intended Beneficiaries", "Beneficiaries"]),
    ("legislative_strategy",  ["Legislative Strategy", "Strategy"]),
    ("increasing_aspects",    ["Increasing Aspects", "Increases"]),
    ("decreasing_aspects",    ["Decreasing Aspects", "Decreases"]),
    ("category_subcategory",  ["Category & Subcategory", "Category and Subcategory", "Category"]),
    ("ideological_alignment", ["Ideological Alignment", "Alignment"]),
    ("potential_impact",      ["Potential Impact", "Impact"]),
    ("impact_rating",         ["Impact Rating", "Rating"]),
    ("intent",                ["Intent"]),
    ("motivation",            ["Motivation"]),
    ("original_law",          ["Original Law", "Original Laws", "Existing Law"]),
]

CANONICAL_FIELDS = [k for k, _ in FIELD_LABELS]

VALID_IMPACT_RATINGS = {
    "Very Impactful",
    "Moderately Impactful",
    "Slightly Impactful",
    "Not Impactful",
}


# Build a master regex that finds any labeled line. Greedy on the label
# variants so longest-match wins (e.g. "Policy Domains" before "Domain").
def _build_label_regex() -> re.Pattern:
    all_labels = []
    for canonical, variants in FIELD_LABELS:
        for v in sorted(variants, key=len, reverse=True):
            all_labels.append(re.escape(v))
    pattern = (
        r"^\s*(?:[-*•]\s*)?"           # optional bullet/dash
        r"(?:\*\*)?\s*"                # optional opening asterisks
        r"(?P<label>" + "|".join(all_labels) + r")"
        r"\s*(?:\*\*)?\s*:\s*"          # optional closing asterisks before colon
        r"(?P<value>.*)$"
    )
    return re.compile(pattern, re.IGNORECASE | re.MULTILINE)


_LABEL_RE = _build_label_regex()


def _label_to_canonical(label: str) -> Optional[str]:
    """Map any label variant to the canonical field name."""
    label_lc = label.strip().lower()
    for canonical, variants in FIELD_LABELS:
        for v in variants:
            if v.lower() == label_lc:
                return canonical
    return None


def parse_features(llm_output: str) -> dict:
    """Parse the labeled LLM output into a dict of canonical field names.

    Always returns a dict with all CANONICAL_FIELDS as keys (None if missing),
    plus:
      - parse_status: "OK" | "PARTIAL" | "EMPTY"
      - missing_fields_count: int
      - parse_errors: list of strings (warnings, not exceptions)

    The function never raises on bad LLM output; it captures issues in
    parse_errors so the extractor can still write a row.
    """
    result: dict = {f: None for f in CANONICAL_FIELDS}
    result["parse_status"] = "EMPTY"
    result["missing_fields_count"] = len(CANONICAL_FIELDS)
    result["parse_errors"] = []

    if not llm_output or not isinstance(llm_output, str):
        result["parse_errors"].append("empty or non-string LLM output")
        return result

    # Find all labeled positions in the text
    matches = list(_LABEL_RE.finditer(llm_output))
    if not matches:
        result["parse_errors"].append("no labeled fields found in output")
        return result

    # For each match, the value runs from end-of-this-match to start-of-next
    for i, m in enumerate(matches):
        canonical = _label_to_canonical(m.group("label"))
        if not canonical:
            result["parse_errors"].append(f"unknown label: {m.group('label')!r}")
            continue

        value_start = m.start("value")
        value_end = matches[i + 1].start() if i + 1 < len(matches) else len(llm_output)
        raw_value = llm_output[value_start:value_end].strip()

        # Strip wrapping asterisks and trailing punctuation noise
        raw_value = raw_value.strip("*").strip()

        # Treat literal "None" / "N/A" / empty as missing
        if not raw_value or raw_value.lower() in {"none", "n/a", "na", "null"}:
            continue

        # First non-empty wins (don't overwrite if the LLM labels something twice)
        if result.get(canonical) is None:
            result[canonical] = raw_value

    # Validate impact rating against the known set
    rating = result.get("impact_rating")
    if rating and rating not in VALID_IMPACT_RATINGS:
        # Try a fuzzy match — strip "Impactful", common typos
        normalized = rating.strip().rstrip(".").strip()
        match = next((v for v in VALID_IMPACT_RATINGS if v.lower() == normalized.lower()), None)
        if match:
            result["impact_rating"] = match
        else:
            result["parse_errors"].append(
                f"impact_rating not in valid set: {rating!r}"
            )

    # Compute status
    missing = [f for f in CANONICAL_FIELDS if result.get(f) is None]
    result["missing_fields_count"] = len(missing)
    if not missing:
        result["parse_status"] = "OK"
    elif len(missing) < len(CANONICAL_FIELDS):
        result["parse_status"] = "PARTIAL"
    else:
        result["parse_status"] = "EMPTY"

    return result
