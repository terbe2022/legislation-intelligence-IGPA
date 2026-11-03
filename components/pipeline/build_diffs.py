"""
build_diffs.py — Bill Version Diff Builder
===========================================
Reads versioned bill JSONs from bronze, computes structured diffs between
consecutive versions, and writes a single bill_diffs.parquet to gold per
session. The Streamlit tracker reads this to show Hiatt exactly what changed
in the legislative text between pipeline runs — without the app needing
direct bronze access.

Output schema (one row per bill per version change):
    bill_id          Int64   — LegiScan bill ID
    bill_number      str     — e.g. "HB0496"
    session_id       Int64
    version_from     Int64   — prior version number
    version_to       Int64   — new version number
    changed_at       str     — ISO timestamp from manifest last_changed_at
    full_text_to     str     — full text of the new version (latest)
    diff_hunks       str     — JSON: [{type, lines}] for rendering
    n_lines_added    Int64
    n_lines_removed  Int64
    fields_changed   str     — JSON list of gold feature field names that changed
    status_desc      str     — current bill status label
    last_action      str     — most recent action text

Gold output path:
    legislation/bill_diffs/llama31_1200/session={N}/bill_diffs.parquet

Usage (run on the pipeline server after ingestion completes):
    python build_diffs.py --session 2176 --upload
    python build_diffs.py --session 2176 2020 --upload
    python build_diffs.py --all --upload

After running, generate a SAS URL for each session's parquet and paste into
data_catalog.py under DIFF_URLS[session_id].

Dependencies: same as pipeline (azure-storage-blob, pandas, pyarrow).
No new packages needed — difflib is stdlib.
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import sys
from pathlib import Path

import pandas as pd

# ── Pipeline service imports (same pattern as other pipeline scripts) ─────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from services.azure_blob_service import BlobClient, TIER_BRONZE, TIER_GOLD
    from utils.paths import bronze_session_manifest_path, bronze_bill_version_path
    _HAVE_PIPELINE = True
except ImportError:
    _HAVE_PIPELINE = False
    print("WARNING: Pipeline services not found. Running in stub mode.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("build_diffs")

PROFILE = "llama31_1200"

# Gold feature fields we track for field-level diff.
# Matches the 14 searchable fields in the tracker page.
TRACKED_GOLD_FIELDS = [
    "legislative_goal_standardized",
    "key_provisions_standardized",
    "intent_standardized",
    "motivation_standardized",
    "potential_impact_raw",
    "intended_beneficiaries_standardized",
    "increasing_aspects_standardized",
    "decreasing_aspects_standardized",
    "impact_rating_score",
    "policy_domain_standardized",
    "beneficiary_primary_category",
    "ideological_alignment_simplified",
]


# ---------------------------------------------------------------------------
# Diff computation
# ---------------------------------------------------------------------------

def compute_diff_hunks(text_old: str, text_new: str,
                       context_lines: int = 3) -> tuple[list[dict], int, int]:
    """
    Compute a structured diff between two bill text versions.

    Returns:
        hunks       — list of {type: "added"|"removed"|"context", lines: [...]}
        n_added     — total lines added
        n_removed   — total lines removed

    Uses unified_diff with context so the reader sees surrounding lines
    for each change — critical for legislative text where a single word
    change can matter.
    """
    old_lines = (text_old or "").splitlines()
    new_lines = (text_new or "").splitlines()

    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        lineterm="",
        n=context_lines,
    ))

    hunks: list[dict] = []
    n_added   = 0
    n_removed = 0

    # Parse unified diff output into typed hunks
    current_hunk: dict | None = None

    for line in diff:
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("@@"):
            if current_hunk:
                hunks.append(current_hunk)
            current_hunk = {"header": line, "lines": []}
            continue
        if current_hunk is None:
            current_hunk = {"header": "", "lines": []}

        if line.startswith("+"):
            current_hunk["lines"].append({"type": "added",   "text": line[1:]})
            n_added += 1
        elif line.startswith("-"):
            current_hunk["lines"].append({"type": "removed", "text": line[1:]})
            n_removed += 1
        else:
            current_hunk["lines"].append({"type": "context", "text": line[1:]})

    if current_hunk:
        hunks.append(current_hunk)

    return hunks, n_added, n_removed


# ---------------------------------------------------------------------------
# Gold field diff (reads gold parquet to compare feature values)
# ---------------------------------------------------------------------------

def get_fields_changed(blob: "BlobClient", session_id: int,
                       bill_id: str) -> list[str]:
    """
    Compare the most recent gold features for a bill against what was
    there before this pipeline run.

    NOTE: This is a best-effort check. The gold parquet is a single
    snapshot (last-write-wins). We detect field changes by comparing
    the current gold row against the snapshot stored in Cosmos DB when
    the user pinned the bill. That comparison happens in the tracker UI.

    Here we simply return the list of tracked fields that are non-null
    for this bill, as a signal of data completeness.

    TODO (production): store per-version gold snapshots in a separate
    parquet so field diffs can be computed deterministically here.
    """
    # Placeholder — field-level diff is handled in the tracker UI
    # via the snapshot stored in Cosmos at pin time.
    return []


# ---------------------------------------------------------------------------
# Per-session runner
# ---------------------------------------------------------------------------

def build_diffs_for_session(blob: "BlobClient", session_id: int,
                             upload: bool = True) -> pd.DataFrame:
    """
    Read the bronze manifest for a session, find all bills with
    current_version >= 2, load v(N-1) and v(N), compute diffs,
    return a DataFrame and optionally upload to gold.
    """
    logger.info("Building diffs for session=%d", session_id)

    # Load manifest
    manifest_path = bronze_session_manifest_path(session_id)
    if not blob.blob_exists(TIER_BRONZE, manifest_path):
        logger.warning("No manifest found for session=%d", session_id)
        return pd.DataFrame()

    manifest = blob.read_parquet(TIER_BRONZE, manifest_path)
    logger.info("Manifest rows: %d", len(manifest))

    # Only bills with at least 2 versions have a diff
    changed = manifest[manifest["current_version"].fillna(1).astype(int) >= 2].copy()
    logger.info("Bills with version >= 2: %d", len(changed))

    if changed.empty:
        logger.info("No versioned bills found — writing empty diffs parquet.")
        return pd.DataFrame()

    rows: list[dict] = []

    for _, mrow in changed.iterrows():
        bill_number  = str(mrow.get("bill_number", ""))
        bill_id      = mrow.get("bill_id")
        cur_version  = int(mrow.get("current_version", 1))
        prior_version = cur_version - 1
        changed_at   = str(mrow.get("last_changed_at", ""))
        status_desc  = str(mrow.get("status_desc", ""))
        last_action  = str(mrow.get("last_action", ""))

        if not bill_number or cur_version < 2:
            continue

        # Load prior and current version JSON from bronze
        path_old = bronze_bill_version_path(session_id, bill_number, prior_version)
        path_new = bronze_bill_version_path(session_id, bill_number, cur_version)

        try:
            rec_old = blob.read_json(TIER_BRONZE, path_old)
            rec_new = blob.read_json(TIER_BRONZE, path_new)
        except Exception as e:
            logger.warning("Could not load versions for %s: %s", bill_number, e)
            continue

        text_old = str(rec_old.get("full_text") or "")
        text_new = str(rec_new.get("full_text") or "")

        # Skip if texts are identical (hash collision guard)
        if text_old == text_new:
            logger.debug("%s v%d→v%d: texts identical, skipping",
                         bill_number, prior_version, cur_version)
            continue

        hunks, n_added, n_removed = compute_diff_hunks(text_old, text_new)

        rows.append({
            "bill_id":        bill_id,
            "bill_number":    bill_number,
            "session_id":     session_id,
            "version_from":   prior_version,
            "version_to":     cur_version,
            "changed_at":     changed_at,
            "full_text_to":   text_new[:50_000],  # cap at 50K chars for parquet
            "diff_hunks":     json.dumps(hunks),
            "n_lines_added":  n_added,
            "n_lines_removed": n_removed,
            "fields_changed": json.dumps([]),  # populated by tracker UI via snapshot
            "status_desc":    status_desc,
            "last_action":    last_action,
        })

        logger.info("Diffed %s v%d→v%d: +%d/-%d lines",
                    bill_number, prior_version, cur_version, n_added, n_removed)

    if not rows:
        logger.info("No diffs computed for session=%d", session_id)
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["bill_id"]         = pd.to_numeric(df["bill_id"], errors="coerce").astype("Int64")
    df["session_id"]      = pd.to_numeric(df["session_id"], errors="coerce").astype("Int64")
    df["version_from"]    = df["version_from"].astype("Int64")
    df["version_to"]      = df["version_to"].astype("Int64")
    df["n_lines_added"]   = df["n_lines_added"].astype("Int64")
    df["n_lines_removed"] = df["n_lines_removed"].astype("Int64")

    logger.info("Diffs built: %d rows for session=%d", len(df), session_id)

    if upload:
        gold_path = (f"legislation/bill_diffs/{PROFILE}"
                     f"/session={session_id}/bill_diffs.parquet")
        blob.write_parquet(df, TIER_GOLD, gold_path)
        logger.info("Uploaded → gold/%s", gold_path)
        logger.info(
            "\n"
            "=" * 60 + "\n"
            "NEXT STEP: generate a SAS URL for this blob and paste it\n"
            "into data_catalog.py under DIFF_URLS[%d].\n"
            "\n"
            "Blob path: gold/%s\n"
            "=" * 60,
            session_id, gold_path,
        )

    return df


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Build bill version diffs.")
    parser.add_argument("--session", type=int, nargs="+",
                        help="Session IDs to process")
    parser.add_argument("--all", action="store_true",
                        help="Process all sessions in ACTIVE_SESSIONS")
    parser.add_argument("--upload", action="store_true", default=True,
                        help="Upload result to gold (default: True)")
    parser.add_argument("--no-upload", action="store_false", dest="upload",
                        help="Skip upload (dry run)")
    args = parser.parse_args()

    if not _HAVE_PIPELINE:
        logger.error("Pipeline services not available. "
                     "Run from the pipeline server environment.")
        sys.exit(1)

    # Determine which sessions to run
    if args.all:
        from data_catalog import ACTIVE_SESSIONS
        sessions = ACTIVE_SESSIONS
    elif args.session:
        sessions = args.session
    else:
        parser.print_help()
        sys.exit(1)

    blob = BlobClient()
    results = {}

    for sid in sessions:
        try:
            df = build_diffs_for_session(blob, sid, upload=args.upload)
            results[sid] = {"status": "OK", "rows": len(df)}
        except Exception as e:
            logger.exception("Session %d failed: %s", sid, e)
            results[sid] = {"status": "FAILED", "error": str(e)}

    print("\n" + "=" * 50)
    print("BUILD DIFFS RESULTS")
    print("=" * 50)
    for sid, r in results.items():
        print(f"  session={sid}: {r}")

    failed = [s for s, r in results.items() if r["status"] == "FAILED"]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
