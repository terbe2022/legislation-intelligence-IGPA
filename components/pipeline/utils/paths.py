"""Centralized blob path builders.

All blob paths used by the pipeline come from here so partition conventions
are defined in one place. If we change "year=YYYY/month=MM" to something
else, only this file changes.
"""

from __future__ import annotations

from datetime import date, datetime


def _date_partition(d: date) -> str:
    return f"year={d.year:04d}/month={d.month:02d}/day={d.day:02d}"


def _month_partition(d: date) -> str:
    return f"year={d.year:04d}/month={d.month:02d}"


# ---------------------------------------------------------------- bronze
# Bronze layout for ingestion is session-partitioned, not date-partitioned:
#
#   legislation/session={session_id}/_manifest.parquet
#   legislation/session={session_id}/bills/{bill_number}_v{N}.json
#
# Rationale: the cron job needs the session-wide view at startup ("what's
# terminal, skip those"). One manifest read beats N per-bill state reads.
# Bill JSONs are append-only (a new version writes _v{N+1}.json, never
# rewrites _v{N}.json), so history is preserved.

def bronze_session_prefix(session_id: int) -> str:
    return f"legislation/session={session_id}/"


def bronze_session_manifest_path(session_id: int) -> str:
    return f"{bronze_session_prefix(session_id)}_manifest.parquet"


def bronze_bills_prefix(session_id: int) -> str:
    return f"{bronze_session_prefix(session_id)}bills/"


def bronze_bill_version_path(session_id: int, bill_number: str, version: int) -> str:
    """e.g. legislation/session=2176/bills/HB0001_v1.json"""
    return f"{bronze_bills_prefix(session_id)}{bill_number}_v{version}.json"


# ---------------------------------------------------------------- silver
def silver_chunks_path(d: date, batch_id: str) -> str:
    return f"legislation/chunks/{_date_partition(d)}/chunks_{batch_id}.parquet"


def silver_features_raw_path(d: date, batch_id: str) -> str:
    return f"legislation/features_raw/{_date_partition(d)}/features_raw_{batch_id}.parquet"


def silver_features_parsed_path(d: date, batch_id: str) -> str:
    return f"legislation/features_parsed/{_date_partition(d)}/features_parsed_{batch_id}.parquet"


# ------------------------------------------------------------------ gold
def gold_features_standardized_path(d: date) -> str:
    return f"legislation/features_standardized/{_month_partition(d)}/features_{d.year:04d}_{d.month:02d}.parquet"


def gold_manifest_path() -> str:
    return "legislation/features_standardized/_latest_manifest.json"


# --------------------------------------------------------------- indexes
# (separate container; left as a string convention for now)
def index_dir(version: int) -> str:
    return f"legislation/faiss_v{version}/"


def index_faiss_path(version: int) -> str:
    return f"{index_dir(version)}index.faiss"


def index_metadata_path(version: int) -> str:
    return f"{index_dir(version)}metadata.parquet"


def index_manifest_path(version: int) -> str:
    return f"{index_dir(version)}manifest.json"
