"""LegiScan API operations.

Direct port of the LegiScan-talking parts of legislative_pipeline_1's
ingestion_main.py. No logic changes, just rehoused as a service module
so it can be reused by ingestion or any future LegiScan-backed pipeline.

LegiScan endpoints used:
  getSessionList   -> list of all sessions for a state
  getDatasetList   -> list of downloadable datasets keyed by session_id
  getDataset       -> the actual zipped CSV bundle (base64-encoded JSON)

The dataset endpoint is the workhorse: one call yields a zip that contains
bills.csv with metadata for every bill in the session. We extract bills.csv
and hand it to the caller as a DataFrame.
"""

from __future__ import annotations

import base64
import logging
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)


# Default User-Agent: LegiScan and ILGA both serve plainly to a browser-like UA.
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)


@dataclass(frozen=True)
class SessionInfo:
    session_id: int
    name: str
    year_start: Optional[int] = None
    year_end: Optional[int] = None
    raw: Optional[dict] = None


@dataclass(frozen=True)
class DatasetInfo:
    session_id: int
    access_key: str
    dataset_date: str
    dataset_hash: str


def make_session(user_agent: str = DEFAULT_UA) -> requests.Session:
    """Build a requests.Session with the standard UA. Used by both LegiScan
    and ILGA helpers so call patterns stay consistent."""
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent})
    return s


# -------------------------------------------------------- session discovery
def list_sessions(
    session: requests.Session,
    api_key: str,
    state: str,
    timeout_seconds: int = 60,
) -> list[SessionInfo]:
    """Fetch all sessions LegiScan knows about for a given state."""
    url = f"https://api.legiscan.com/?key={api_key}&op=getSessionList&state={state}"
    r = session.get(url, timeout=timeout_seconds)
    r.raise_for_status()
    payload = r.json()
    raw_sessions = payload.get("sessions", [])
    out = []
    for s in raw_sessions:
        out.append(
            SessionInfo(
                session_id=int(s["session_id"]),
                name=s.get("session_name") or s.get("name", ""),
                year_start=s.get("year_start"),
                year_end=s.get("year_end"),
                raw=s,
            )
        )
    return out


def pick_latest_session_id(sessions: list[SessionInfo]) -> int:
    """Pick the session with the highest session_id.

    LegiScan assigns ids monotonically, so this is reliable.
    """
    if not sessions:
        raise ValueError("LegiScan returned no sessions for this state.")
    return max(sessions, key=lambda s: s.session_id).session_id


# ---------------------------------------------------------- dataset listing
def get_dataset_for_session(
    session: requests.Session,
    api_key: str,
    state: str,
    session_id: int,
    timeout_seconds: int = 60,
) -> DatasetInfo:
    """Find the downloadable dataset metadata for a specific session_id."""
    url = f"https://api.legiscan.com/?key={api_key}&op=getDatasetList&state={state}"
    r = session.get(url, timeout=timeout_seconds)
    r.raise_for_status()
    datasets = r.json().get("datasetlist", [])
    match = next((d for d in datasets if int(d["session_id"]) == int(session_id)), None)
    if match is None:
        raise LookupError(
            f"No LegiScan dataset found for session_id={session_id} in state={state}. "
            f"Available session_ids: {[int(d['session_id']) for d in datasets]}"
        )
    return DatasetInfo(
        session_id=int(match["session_id"]),
        access_key=match["access_key"],
        dataset_date=match["dataset_date"],
        dataset_hash=match.get("dataset_hash", ""),
    )


# ------------------------------------------------------------- dataset pull
def download_dataset_zip(
    session: requests.Session,
    api_key: str,
    dataset: DatasetInfo,
    tmp_dir: Path,
    timeout_seconds: int = 300,
) -> Path:
    """Download the dataset zip bytes, write to tmp_dir, return the zip path.

    The endpoint returns base64-encoded zip bytes inside JSON. Decoded zip
    is written so we can both inspect it and unzip with zipfile.
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    url = (
        f"https://api.legiscan.com/?key={api_key}&op=getDataset"
        f"&id={dataset.session_id}&access_key={dataset.access_key}&format=csv"
    )
    r = session.get(url, timeout=timeout_seconds)
    r.raise_for_status()
    payload = r.json()
    zip_bytes = base64.b64decode(payload["dataset"]["zip"])

    zip_name = (
        f"legiscan_session_{dataset.session_id}_"
        f"{dataset.dataset_date}_{dataset.dataset_hash}.zip"
    )
    zip_path = tmp_dir / zip_name
    zip_path.write_bytes(zip_bytes)
    logger.info(
        "Downloaded LegiScan dataset zip: %s (%d bytes)",
        zip_path.name, len(zip_bytes),
    )
    return zip_path


def extract_bills_csv(zip_path: Path, extract_dir: Path) -> Path:
    """Unzip the dataset bundle and return the path to bills.csv inside it.

    The bundle layout varies slightly across sessions; we walk recursively
    rather than assuming a fixed nested folder.
    """
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_dir)

    bills_csv = next(extract_dir.rglob("bills.csv"), None)
    if bills_csv is None:
        raise FileNotFoundError(
            f"bills.csv not found inside dataset zip extracted to {extract_dir}"
        )
    logger.info("Extracted bills.csv: %s", bills_csv)
    return bills_csv


def load_bills_csv(bills_csv_path: Path) -> pd.DataFrame:
    """Read bills.csv with the same dtype hint the intern's code used."""
    return pd.read_csv(bills_csv_path, dtype={"bill_id": "string"})
