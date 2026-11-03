"""Stage 1: Ingestion.

Pulls bills from LegiScan + ILGA, writes them to the bronze tier of Azure
Blob with version detection.

End-to-end flow per session:

  1. Read the session's existing bronze manifest (one row per bill).
     - If session is fully terminal (every bill is_terminal=true), skip.
  2. Hit LegiScan getDataset to get the canonical metadata for this session.
  3. For each bill in the dataset:
       a. If the bill is in the manifest AND already terminal, skip.
       b. Otherwise, attempt to fetch full_text:
            - First: ILGA BillStatus FullText (parsed from state_link)
            - Fallback: ILGA FTP directory match
       c. Compute SHA256(full_text). Compare to manifest's last-known hash.
       d. If hash differs (or bill is new):
            - Write bills/{bill_number}_v{N+1}.json with the full record
            - Update manifest row: bump version, update hash, set
              last_changed_at=now
       e. If hash is unchanged:
            - Just update last_seen_at on the manifest row
       f. If status moved to a terminal code, set is_terminal=true.
  4. Write the updated manifest back to bronze.

Modes:
  live - real APIs, real bronze writes
  dev  - load from local snapshot parquet (skip APIs), write to bronze test prefix
  dry  - real APIs, log what would be written, don't actually write

Returns a dict for the pipeline_logger.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from services.azure_blob_service import BlobClient, TIER_BRONZE
from services.ilga_service import (
    fetch_full_text,
    fill_missing_full_text_from_ilga_ftp,
    get_illinois_general_assembly_number,
    is_missing_full_text_series,
    to_fulltext_url_from_state_link,
)
from services.legiscan_service import (
    download_dataset_zip,
    extract_bills_csv,
    get_dataset_for_session,
    load_bills_csv,
    make_session as make_http_session,
)
from utils.bill_normalize import (
    chamber_from_bill_number,
    content_hash,
    is_terminal_status,
    normalize_bill_number,
)
from utils.config_loader import get_env_or_fail
from utils.paths import (
    bronze_bill_version_path,
    bronze_session_manifest_path,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Manifest schema — one row per bill within a session.
# ---------------------------------------------------------------------------
MANIFEST_COLUMNS = [
    "bill_id", "bill_number", "session_id", "chamber",
    "title", "description",
    "status", "status_desc", "status_date",
    "last_action", "last_action_date",
    "url", "state_link",
    "committee_id", "committee",
    "current_version", "content_hash", "has_full_text", "text_length",
    "full_text_uri",
    "is_terminal", "first_seen_at", "last_seen_at", "last_changed_at",
]


# ---------------------------------------------------------------------------
# Manifest I/O
# ---------------------------------------------------------------------------
def _empty_manifest() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in MANIFEST_COLUMNS})


def load_session_manifest(client: BlobClient, session_id: int) -> pd.DataFrame:
    path = bronze_session_manifest_path(session_id)
    if not client.blob_exists(TIER_BRONZE, path):
        logger.info("No existing manifest for session=%d, starting fresh.", session_id)
        return _empty_manifest()
    df = client.read_parquet(TIER_BRONZE, path)
    logger.info("Loaded manifest session=%d rows=%d", session_id, len(df))
    return df


def save_session_manifest(
    client: BlobClient, session_id: int, manifest: pd.DataFrame, dry: bool = False,
) -> None:
    path = bronze_session_manifest_path(session_id)
    if dry:
        logger.info("[DRY] Would write manifest session=%d rows=%d -> %s",
                    session_id, len(manifest), path)
        return
    client.write_parquet(manifest, TIER_BRONZE, path)


# ---------------------------------------------------------------------------
# Per-bill helpers
# ---------------------------------------------------------------------------
def _to_native(v):
    """Coerce numpy / pandas scalars to JSON-friendly Python types."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(v, "item"):
        return v.item()
    return v


def _row_to_bill_record(row: dict, version: int, ingested_at: str) -> dict:
    """Build the JSON record we write per bill version.

    This is the canonical representation of a bill at a point in time —
    downstream stages can read just this without going back to the manifest.
    """
    return {
        "bill_id": row.get("bill_id"),
        "bill_number": row.get("bill_number"),
        "session_id": row.get("session_id"),
        "status": row.get("status"),
        "status_desc": row.get("status_desc"),
        "status_date": row.get("status_date"),
        "last_action": row.get("last_action"),
        "last_action_date": row.get("last_action_date"),
        "title": row.get("title"),
        "description": row.get("description"),
        "committee_id": row.get("committee_id"),
        "committee": row.get("committee"),
        "url": row.get("url"),
        "state_link": row.get("state_link"),
        "full_text_url": row.get("full_text_url"),
        "full_text": row.get("full_text"),
        "version": version,
        "chamber": chamber_from_bill_number(row.get("bill_number")),
        "ingested_at": ingested_at,
        "content_hash": content_hash(row.get("full_text") or ""),
    }


def _normalize_record_for_json(rec: dict) -> dict:
    return {k: _to_native(v) for k, v in rec.items()}


# ---------------------------------------------------------------------------
# Full-text resolution (BillStatus → FTP fallback)
# ---------------------------------------------------------------------------
def _resolve_full_text(bills_df: pd.DataFrame, http_session, cfg: dict) -> pd.DataFrame:
    """Add full_text and full_text_url columns to bills_df. Returns a copy.

    FTP-FIRST strategy. We learned (via spot-check on bronze HB0001) that
    the BillStatus FullText page only renders the synopsis (~2,800 chars
    typical), while the FTP HTML contains the full statutory text
    (~22,000-30,000+ chars typical) — about 8x more content. The intern's
    final notebook (cell 20) settled on FTP-only after three iterations,
    and we've adopted the same source.

    Pass 1 (primary): for each bill, build the canonical FTP URL from
    state_link's query parameters (or directly from bill_number+ga as a
    fallback) and fetch. The new ilga_service.fetch_full_text already
    does directory-listing fallback internally when canonical 404s.

    Pass 2 (safety net): for any bills still missing text, run
    fill_missing_full_text_from_ilga_ftp which retries via the dir-listing
    matcher with fresh requests. Catches transient network failures.
    """
    out = bills_df.copy()

    # full_text_url is now the FTP URL we'll use (was BillStatus pre-v5).
    # Fall back to deriving it from bill_number+GA when state_link is
    # malformed or missing — the intern hit this often enough that we
    # bake the fallback in.
    ga = get_illinois_general_assembly_number()
    out["full_text_url"] = out["state_link"].apply(to_fulltext_url_from_state_link)
    out["full_text"] = None

    ftp_cfg = cfg.get("ilga_ftp", {})
    timeout = int(ftp_cfg.get("timeout_seconds", 30))
    retries = int(ftp_cfg.get("retries", 3))
    min_sleep = float(ftp_cfg.get("min_sleep_seconds", 0.30))
    max_sleep = float(ftp_cfg.get("max_sleep_seconds", 0.70))
    chunk_size = int(ftp_cfg.get("chunk_size", 25))

    bill_numbers = out["bill_number"].tolist()
    urls = out["full_text_url"].tolist()
    texts: list = []
    for i in range(0, len(urls), chunk_size):
        for j, u in enumerate(urls[i : i + chunk_size]):
            bn = bill_numbers[i + j]
            texts.append(
                fetch_full_text(
                    http_session, u,
                    timeout=timeout, retries=retries,
                    min_sleep=min_sleep, max_sleep=max_sleep,
                    bill_number=bn, ga=ga,
                )
            )
        done = min(i + chunk_size, len(urls))
        logger.info("ILGA FTP fetch progress: %d/%d", done, len(urls))
    out["full_text"] = texts

    # Safety-net pass: anything still missing, retry via dir listing.
    fallback_cfg = cfg.get("ilga_ftp_fallback", {})
    if fallback_cfg.get("enabled", True):
        before = int(is_missing_full_text_series(out["full_text"]).sum())
        if before > 0:
            out = fill_missing_full_text_from_ilga_ftp(
                out,
                ga=ga,
                timeout_seconds=int(fallback_cfg.get("timeout_seconds", 30)),
                sleep_seconds=float(fallback_cfg.get("sleep_seconds", 0.5)),
                min_text_len=int(fallback_cfg.get("min_text_len", 500)),
                pick=str(fallback_cfg.get("pick", "newest")),
                session=http_session,
            )
            after = int(is_missing_full_text_series(out["full_text"]).sum())
            logger.info("FTP safety-net pass: missing before=%d after=%d", before, after)
        else:
            logger.info("FTP safety-net pass skipped: no missing texts after primary pass.")

    # Sanity log: text-length distribution. If we see lots of bills under
    # 5,000 chars on the production session, something's wrong (FTP
    # serving stubs, scraping pattern broken, etc.). Surfacing this as a
    # log line means a regression won't be silent like the BillStatus
    # one was.
    lengths = out["full_text"].fillna("").astype(str).str.len()
    if not lengths.empty:
        logger.info(
            "Text length distribution: median=%d p25=%d p75=%d min=%d max=%d (n=%d)",
            int(lengths.median()), int(lengths.quantile(0.25)),
            int(lengths.quantile(0.75)),
            int(lengths.min()), int(lengths.max()), len(lengths),
        )

    return out


# ---------------------------------------------------------------------------
# The main per-session run
# ---------------------------------------------------------------------------
def _run_session(
    session_id: int,
    cfg: dict,
    blob: BlobClient,
    http_session,
    api_key: str,
    state: str,
    tmp_dir: Path,
    mode: str,
    terminal_codes: frozenset,
) -> dict:
    result = {
        "session_id": session_id,
        "status": "SUCCESS",
        "bills_in_dataset": 0,
        "bills_skipped_terminal": 0,
        "bills_unchanged": 0,
        "bills_new_or_updated": 0,
        "bills_no_text": 0,
        "skip_reason": None,
        "error": None,
    }

    # 1. Manifest
    manifest = load_session_manifest(blob, session_id)
    # Save snapshot of manifest BEFORE this run so change-log can diff before/after
    if not manifest.empty:
        prev_path = bronze_session_manifest_path(session_id).replace(
            "_manifest.parquet", "_manifest_prev.parquet"
        )
        try:
            blob.write_parquet(manifest, TIER_BRONZE, prev_path)
            logger.info("Saved manifest snapshot -> %s", prev_path)
        except Exception as _snap_err:
            logger.warning("Could not save manifest snapshot: %s", _snap_err)
    manifest_by_id = (
        {row["bill_id"]: row for _, row in manifest.iterrows()}
        if not manifest.empty else {}
    )

    # If we have a manifest and EVERY row is terminal, skip the whole session.
    if not manifest.empty and bool(manifest["is_terminal"].fillna(False).all()):
        logger.info("Session %d is fully terminal — skipping LegiScan pull.", session_id)
        result["status"] = "SKIPPED"
        result["skip_reason"] = "all_bills_terminal"
        return result

    # 2. LegiScan dataset (or dev snapshot)
    if mode == "dev":
        snap = Path(cfg.get("dev", {}).get("snapshot_path", ""))
        if not snap.exists():
            raise FileNotFoundError(f"dev mode but snapshot not found: {snap.resolve()}")
        bills_df = pd.read_parquet(snap)
        bills_df = bills_df[bills_df["session_id"] == session_id].copy()
        logger.info("[DEV] Loaded %d bills for session=%d from %s",
                    len(bills_df), session_id, snap)
    else:
        legiscan_cfg = cfg["legiscan"]
        dataset = get_dataset_for_session(
            http_session, api_key, state, session_id,
            timeout_seconds=int(legiscan_cfg.get("timeout_seconds", 60)),
        )
        # Hash check — skip download if dataset unchanged since last run
        _hash_file = Path("prediction/outputs/IL/legiscan_dataset_hashes.json")
        _stored_hashes = json.loads(_hash_file.read_text()) if _hash_file.exists() else {}
        _stored_hash = _stored_hashes.get(str(session_id), "")
        _current_hash = dataset.dataset_hash
        _cached_zip = tmp_dir / f"{dataset.dataset_date}_{_current_hash}.zip"
        if _stored_hash == _current_hash and _cached_zip.exists():
            logger.info(
                "Dataset unchanged for session=%d (hash=%s) — using cached zip",
                session_id, _current_hash,
            )
            zip_path = _cached_zip
        else:
            if _stored_hash != _current_hash:
                logger.info(
                    "Dataset changed for session=%d: %s -> %s",
                    session_id, _stored_hash, _current_hash,
                )
            zip_path = download_dataset_zip(
                http_session, api_key, dataset,
                tmp_dir=tmp_dir,
                timeout_seconds=int(legiscan_cfg.get("dataset_timeout_seconds", 300)),
        )
            # Save updated hash
            _stored_hashes[str(session_id)] = _current_hash
            _hash_file.parent.mkdir(parents=True, exist_ok=True)
            _hash_file.write_text(json.dumps(_stored_hashes, indent=2))
        bills_csv = extract_bills_csv(zip_path, tmp_dir / f"extract_{session_id}")
        bills_df = load_bills_csv(bills_csv)

    result["bills_in_dataset"] = len(bills_df)
    if bills_df.empty:
        logger.warning("LegiScan returned 0 bills for session %d.", session_id)
        return result

    # Normalize types we'll rely on
    bills_df["bill_id"] = pd.to_numeric(bills_df["bill_id"], errors="coerce").astype("Int64")
    bills_df["session_id"] = pd.to_numeric(bills_df["session_id"], errors="coerce").astype("Int64")
    bills_df["status"] = pd.to_numeric(bills_df.get("status"), errors="coerce").astype("Int64")
    bills_df["bill_number_norm"] = bills_df["bill_number"].apply(normalize_bill_number)

    # 3. Filter out terminal bills BEFORE fetching text — saves API calls
    if not manifest.empty:
        terminal_ids = set(
            int(r["bill_id"]) for _, r in manifest.iterrows()
            if bool(r.get("is_terminal", False))
        )
        before = len(bills_df)
        bills_df = bills_df[~bills_df["bill_id"].isin(terminal_ids)].copy()
        result["bills_skipped_terminal"] = before - len(bills_df)
        logger.info("Skipped %d already-terminal bills.", result["bills_skipped_terminal"])

    if bills_df.empty:
        logger.info("Nothing left to process after terminal-skip for session %d.", session_id)
        return result

    # Apply optional bill_limit cap before the expensive FTP fetch.
    # Useful for smoke tests — full 11,726-bill backfill takes ~100 min,
    # 50-bill smoke takes ~30s. Cap is applied AFTER terminal-skip so the
    # bills we cap to are real work (not bills already known terminal).
    bill_limit = cfg.get("bill_limit")
    if bill_limit is not None and bill_limit > 0 and len(bills_df) > bill_limit:
        logger.info(
            "Applying bill_limit=%d (was %d bills) for session %d.",
            bill_limit, len(bills_df), session_id,
        )
        bills_df = bills_df.head(int(bill_limit)).copy()


    BATCH_SIZE = 500
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    write_bills_with_no_text = bool(cfg.get("write_bills_with_no_text", True))
    new_manifest_rows: list[dict] = []

    # 4. Fast-path: skip FTP fetch for bills with no recent LegiScan activity.
    # Compare last_action_date (from LegiScan CSV) vs last_seen_at (manifest).
    # Bills where last_action_date <= last_seen_at haven't changed — skip FTP,
    # just carry manifest row forward with updated last_seen_at.
    # Reduces daily FTP calls from ~9,500 to the handful that actually changed.
    if not manifest.empty and "last_action_date" in bills_df.columns:
        _manifest_seen = manifest.set_index("bill_id")["last_seen_at"].to_dict()

        def _needs_ftp_fetch(row):
            bid = row.get("bill_id")
            if bid not in _manifest_seen:
                return True  # New bill — always fetch
            last_seen   = str(_manifest_seen.get(bid, "") or "")[:10]  # YYYY-MM-DD
            last_action = str(row.get("last_action_date", "") or "")[:10]
            if last_action and last_seen:
                return last_action > last_seen
            return True  # Uncertain — fetch to be safe

        bills_df["_needs_fetch"] = bills_df.apply(_needs_ftp_fetch, axis=1)
        _fetch_mask          = bills_df["_needs_fetch"]
        bills_to_fetch       = bills_df[_fetch_mask].drop(columns=["_needs_fetch"]).copy()
        bills_to_skip_fetch  = bills_df[~_fetch_mask].drop(columns=["_needs_fetch"]).copy()
        logger.info(
            "Session %d: %d bills need FTP fetch, %d skipped (no recent activity)",
            session_id, len(bills_to_fetch), len(bills_to_skip_fetch),
        )
    else:
        bills_to_fetch      = bills_df.copy()
        bills_to_skip_fetch = pd.DataFrame(columns=bills_df.columns)

    # For skipped bills: carry manifest row forward, just update last_seen_at
    # and any metadata fields LegiScan gives us without FTP (status, committee, etc.)
    for _, _skip_row in bills_to_skip_fetch.iterrows():
        _bid      = _to_native(_skip_row.get("bill_id"))
        _existing = manifest_by_id.get(_bid)
        if _existing is None:
            continue
        new_manifest_rows.append({
            **_existing,
            "last_seen_at":    now_iso,
            "status":          _to_native(_skip_row.get("status")),
            "status_desc":     _to_native(_skip_row.get("status_desc")),
            "status_date":     _to_native(_skip_row.get("status_date")),
            "last_action":     _to_native(_skip_row.get("last_action")),
            "last_action_date":_to_native(_skip_row.get("last_action_date")),
            "committee_id":    _to_native(_skip_row.get("committee_id")),
            "committee":       _to_native(_skip_row.get("committee")),
        })
        result["bills_unchanged"] += 1

    # Only run the expensive FTP batch loop on bills that actually need it
    bills_df = bills_to_fetch

    # 4b. Fetch text and process in batches of 500 for checkpoint safety.
    # Each batch fetches FTP text, writes bill JSONs, and saves the manifest
    # so a crash loses at most 500 bills worth of FTP work.
    for batch_start in range(0, len(bills_df), BATCH_SIZE):
        batch_df = bills_df.iloc[batch_start:batch_start + BATCH_SIZE].copy()
        batch_end = min(batch_start + BATCH_SIZE, len(bills_df))
        logger.info("Processing batch %d-%d of %d bills for session %d",
                    batch_start + 1, batch_end, len(bills_df), session_id)

        if mode != "dev":
            batch_df = _resolve_full_text(batch_df, http_session, cfg)
        else:
            if "full_text_url" not in batch_df.columns:
                batch_df["full_text_url"] = None
            if "full_text" not in batch_df.columns:
                batch_df["full_text"] = None

        # 5. Per-bill: decide write actions, hash by hash
        batch_manifest_rows: list[dict] = []
        for _, row in batch_df.iterrows():
            bill_number_norm = row.get("bill_number_norm")
            if not bill_number_norm:
                logger.warning("Skipping unparseable bill_number=%r", row.get("bill_number"))
                continue
            bill_number_norm = row.get("bill_number_norm")
            if not bill_number_norm:
                logger.warning("Skipping unparseable bill_number=%r", row.get("bill_number"))
                continue
    
            bill_id = _to_native(row.get("bill_id"))
            full_text = row.get("full_text")
            new_hash = content_hash(full_text or "")
            has_text = bool(full_text and str(full_text).strip())
    
            if not has_text and not write_bills_with_no_text:
                result["bills_no_text"] += 1
                continue
    
            existing = manifest_by_id.get(bill_id)
            is_new = existing is None
            is_changed = (not is_new) and existing.get("content_hash") != new_hash
    
            if is_new:
                new_version = 1
            elif is_changed:
                new_version = int(existing.get("current_version", 0)) + 1
            else:
                new_version = int(existing.get("current_version", 0))
    
            rec = _row_to_bill_record(
                {**row.to_dict(), "bill_number": bill_number_norm},
                version=new_version,
                ingested_at=now_iso,
            )
            rec = _normalize_record_for_json(rec)
    
            if is_new or is_changed:
                blob_path = bronze_bill_version_path(session_id, bill_number_norm, new_version)
                if mode == "dry":
                    logger.info("[DRY] Would write bill %s v%d -> %s",
                                bill_number_norm, new_version, blob_path)
                else:
                    blob.write_json(rec, TIER_BRONZE, blob_path)
                full_text_uri = blob_path
                result["bills_new_or_updated"] += 1
                last_changed_at = now_iso
            else:
                full_text_uri = existing.get("full_text_uri") if existing is not None else None
                result["bills_unchanged"] += 1
                last_changed_at = (
                    existing.get("last_changed_at") if existing is not None else now_iso
                )
    
            first_seen_at = existing.get("first_seen_at") if existing is not None else now_iso
            text_len = len(full_text) if has_text else 0
            terminal_now = is_terminal_status(_to_native(row.get("status")), terminal_codes)
    
            new_manifest_rows.append({
                "bill_id": bill_id,
                "bill_number": bill_number_norm,
                "session_id": _to_native(row.get("session_id")),
                "chamber": chamber_from_bill_number(bill_number_norm),
                "title": _to_native(row.get("title")),
                "description": _to_native(row.get("description")),
                "status": _to_native(row.get("status")),
                "status_desc": _to_native(row.get("status_desc")),
                "status_date": _to_native(row.get("status_date")),
                "last_action": _to_native(row.get("last_action")),
                "last_action_date": _to_native(row.get("last_action_date")),
                "url": _to_native(row.get("url")),
                "state_link": _to_native(row.get("state_link")),
                "committee_id": _to_native(row.get("committee_id")),
                "committee": _to_native(row.get("committee")),
                "current_version": new_version,
                "content_hash": new_hash,
                "has_full_text": has_text,
                "text_length": text_len,
                "full_text_uri": full_text_uri,
                "is_terminal": terminal_now,
                "first_seen_at": first_seen_at,
                "last_seen_at": now_iso,
                "last_changed_at": last_changed_at,
            })

        # Checkpoint: save manifest after each batch
        new_manifest_rows.extend(batch_manifest_rows)
        seen_ids = {r["bill_id"] for r in new_manifest_rows}
        preserved = (
            manifest[~manifest["bill_id"].isin(seen_ids)].to_dict(orient="records")
            if not manifest.empty else []
        )
        checkpoint_df = pd.DataFrame(new_manifest_rows + preserved, columns=MANIFEST_COLUMNS)
        save_session_manifest(blob, session_id, checkpoint_df, dry=(mode == "dry"))
        logger.info("Checkpoint saved: %d/%d bills for session %d",
                    batch_end, len(bills_df), session_id)
        manifest_by_id.update({r["bill_id"]: r for r in batch_manifest_rows})

    # 6. Final manifest save
    seen_ids = {r["bill_id"] for r in new_manifest_rows}
    preserved = (
        manifest[~manifest["bill_id"].isin(seen_ids)].to_dict(orient="records")
        if not manifest.empty else []
    )
    new_manifest = pd.DataFrame(new_manifest_rows + preserved, columns=MANIFEST_COLUMNS)
    save_session_manifest(blob, session_id, new_manifest, dry=(mode == "dry"))

    return result

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def run(config: dict) -> dict:
    """Run the ingestion stage.

    `config` is the full pipeline config dict. We read the `ingestion`
    sub-key — caller is responsible for loading + passing the YAML.
    """
    ingestion_cfg = config.get("ingestion") or config  # tolerate either shape
    mode = ingestion_cfg.get("mode", "live")
    sessions_to_run = list(ingestion_cfg.get("sessions", []))
    state = ingestion_cfg.get("legiscan", {}).get("state", "IL")
    terminal_codes = frozenset(int(c) for c in ingestion_cfg.get("terminal_status_codes", [4, 5, 6]))
    tmp_dir = Path(ingestion_cfg.get("tmp_dir", "storage/temp/ingestion"))

    if not sessions_to_run:
        return {
            "stage": "ingestion", "status": "FAILED",
            "input_count": 0, "output_count": 0,
            "skip_reason": None,
            "error": "No sessions configured under ingestion.sessions",
            "details": {},
        }

    # API key needed for live + dry, not dev
    api_key = ""
    if mode != "dev":
        api_key_env = ingestion_cfg.get("legiscan", {}).get("api_key_env", __import__("os").environ.get('LEG_COMPONENTS_PIPELINE_STAGES_INGESTION_PY_API_KEY_ENV_L610', ""))
        api_key = get_env_or_fail(api_key_env)

    blob = BlobClient()
    http = make_http_session()

    per_session: dict = {}
    total_input = 0
    total_output = 0
    any_failed = False

    for sid in sessions_to_run:
        logger.info("=" * 60)
        logger.info("INGESTION session=%d mode=%s", sid, mode)
        logger.info("=" * 60)
        try:
            r = _run_session(
                sid, ingestion_cfg, blob, http, api_key, state, tmp_dir, mode, terminal_codes,
            )
            per_session[sid] = r
            total_input += r.get("bills_in_dataset", 0)
            total_output += r.get("bills_new_or_updated", 0)
        except Exception as e:
            logger.exception("Session %d FAILED: %s", sid, e)
            per_session[sid] = {"status": "FAILED", "error": str(e)}
            any_failed = True

    overall_status = "FAILED" if any_failed else "SUCCESS"
    return {
        "stage": "ingestion",
        "status": overall_status,
        "input_count": total_input,
        "output_count": total_output,
        "skip_reason": None,
        "error": None if not any_failed else "see per-session details",
        "details": {
            "mode": mode,
            "sessions": sessions_to_run,
            "per_session": per_session,
        },
    }


# Allow `python -m stages.ingestion` for ad-hoc runs.
if __name__ == "__main__":
    import sys
    from utils.config_loader import load_config

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Quiet the very chatty Azure SDK request logger
    logging.getLogger("azure.core").setLevel(logging.WARNING)
    logging.getLogger("azure.storage").setLevel(logging.WARNING)

    cfg = load_config("configs/ingestion.yaml")
    result = run({"ingestion": cfg})
    print()
    print("=" * 60)
    print("RESULT")
    print("=" * 60)
    for k, v in result.items():
        if k != "details":
            print(f"  {k}: {v}")
    print("  details.per_session:")
    for sid, sr in result["details"]["per_session"].items():
        print(f"    session={sid}: {sr}")

    sys.exit(0 if result["status"] == "SUCCESS" else 1)
