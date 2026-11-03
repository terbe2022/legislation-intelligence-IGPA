"""Azure Blob Storage helper for the legislation pipeline.

A thin, opinionated wrapper around `azure-storage-blob` that exposes only
the operations the pipeline actually needs. Keeps Azure SDK calls out of
the rest of the codebase so that:

  - swapping auth methods (.env -> Key Vault -> Managed Identity) later
    is a one-place change, not a project-wide refactor
  - retries, logging, and content-type handling live in one module
  - tests can mock a single class instead of patching SDK internals

Conventions used here mirror the proposals pipeline:
  - Configuration via environment variables loaded from .env
  - Logging via the standard `logging` module (caller configures handlers)
  - Idempotent reads/writes; no implicit overwrite protection (caller decides)
  - Path style: forward slashes, no leading slash
       e.g. "legislation/year=2026/month=04/day=30/bills.parquet"

Tier conventions (used by the legislation pipeline):
  bronze  -> raw bills as ingested
  silver  -> chunked, summarized, intermediate parquets
  gold    -> production-ready, partitioned parquet snapshots
"""

from __future__ import annotations

import io
import json
import logging
import os
from dataclasses import dataclass
from typing import Iterator, Optional

import pandas as pd
from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContentSettings
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Tier identifiers
# --------------------------------------------------------------------------
# Using string constants (not an Enum) so callers can pass plain strings
# from config files without importing this module everywhere.
TIER_BRONZE = "bronze"
TIER_SILVER = "silver"
TIER_GOLD = "gold"

VALID_TIERS = {TIER_BRONZE, TIER_SILVER, TIER_GOLD}


# --------------------------------------------------------------------------
# Configuration dataclass
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class BlobConfig:
    """Immutable config bundle. Loaded once from environment at module import.

    Frozen so accidental mutation in caller code raises immediately.
    """
    connection_string: str
    account_name: str
    container_bronze: str
    container_silver: str
    container_gold: str

    def container_for_tier(self, tier: str) -> str:
        if tier == TIER_BRONZE:
            return self.container_bronze
        if tier == TIER_SILVER:
            return self.container_silver
        if tier == TIER_GOLD:
            return self.container_gold
        raise ValueError(
            f"Unknown tier {tier!r}. Must be one of {sorted(VALID_TIERS)}."
        )


def load_config(env_file: Optional[str] = None) -> BlobConfig:
    """Load blob storage config from .env (or process environment if already set).

    Raises a clear error if required vars are missing — fail loudly at startup
    rather than half-running and crashing mid-pipeline.
    """
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()  # picks up .env from cwd or parents

    required = {
        "AZURE_STORAGE_CONNECTION_STRING": None,
        "AZURE_STORAGE_ACCOUNT_NAME": None,
        "AZURE_CONTAINER_BRONZE": None,
        "AZURE_CONTAINER_SILVER": None,
        "AZURE_CONTAINER_GOLD": None,
    }
    missing = []
    for key in required:
        val = os.environ.get(key)
        if not val:
            missing.append(key)
        required[key] = val

    if missing:
        raise RuntimeError(
            "Missing required environment variables for blob storage: "
            f"{missing}. Check .env file or process environment."
        )

    return BlobConfig(
        connection_string=required["AZURE_STORAGE_CONNECTION_STRING"],
        account_name=required["AZURE_STORAGE_ACCOUNT_NAME"],
        container_bronze=required["AZURE_CONTAINER_BRONZE"],
        container_silver=required["AZURE_CONTAINER_SILVER"],
        container_gold=required["AZURE_CONTAINER_GOLD"],
    )


# --------------------------------------------------------------------------
# Main client
# --------------------------------------------------------------------------
class BlobClient:
    """Wrapper around BlobServiceClient with pipeline-friendly methods.

    Instantiate once per process (it's threadsafe and pools connections):

        client = BlobClient()
        client.write_parquet(df, TIER_SILVER, "legislation/chunks/y=2026/m=04/chunks.parquet")
        df2 = client.read_parquet(TIER_SILVER, "legislation/chunks/y=2026/m=04/chunks.parquet")
    """

    def __init__(self, config: Optional[BlobConfig] = None):
        self.config = config or load_config()
        self._service = BlobServiceClient.from_connection_string(
            self.config.connection_string
        )
        logger.info(
            "BlobClient initialized for account=%s", self.config.account_name
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _container_client(self, tier: str):
        container_name = self.config.container_for_tier(tier)
        return self._service.get_container_client(container_name)

    def _blob_client(self, tier: str, path: str):
        if path.startswith("/"):
            raise ValueError(
                f"Blob path should not start with '/': got {path!r}"
            )
        return self._container_client(tier).get_blob_client(path)

    # ------------------------------------------------------------------
    # Existence / listing
    # ------------------------------------------------------------------
    def blob_exists(self, tier: str, path: str) -> bool:
        try:
            return self._blob_client(tier, path).exists()
        except Exception as e:
            logger.warning("blob_exists check failed for %s/%s: %s", tier, path, e)
            return False

    def list_blobs(self, tier: str, prefix: str = "") -> Iterator[str]:
        """Yield blob names under a prefix. Use empty prefix to list whole container."""
        container = self._container_client(tier)
        for blob in container.list_blobs(name_starts_with=prefix):
            yield blob.name

    # ------------------------------------------------------------------
    # Parquet I/O (the workhorses for silver/gold)
    # ------------------------------------------------------------------
    def write_parquet(
        self,
        df: pd.DataFrame,
        tier: str,
        path: str,
        overwrite: bool = True,
    ) -> None:
        """Write a DataFrame as parquet to the given tier and path.

        Default overwrite=True because pipeline reruns are normal; if you
        need write-once semantics, check blob_exists() first.
        """
        if df is None:
            raise ValueError("Cannot write None as parquet")

        # Serialize to a buffer first; lets us set content_type and avoids
        # leaving a partial blob behind if serialization fails.
        buf = io.BytesIO()
        df.to_parquet(buf, index=False, engine="pyarrow", compression="snappy")
        buf.seek(0)

        client = self._blob_client(tier, path)
        client.upload_blob(
            buf.getvalue(),
            overwrite=overwrite,
            content_settings=ContentSettings(content_type="application/octet-stream"),
        )
        logger.info(
            "Wrote parquet: tier=%s path=%s rows=%d size_kb=%.1f",
            tier, path, len(df), buf.getbuffer().nbytes / 1024,
        )

    def read_parquet(self, tier: str, path: str) -> pd.DataFrame:
        client = self._blob_client(tier, path)
        try:
            stream = client.download_blob()
        except ResourceNotFoundError:
            raise FileNotFoundError(f"Blob not found: {tier}/{path}")
        buf = io.BytesIO()
        stream.readinto(buf)
        buf.seek(0)
        df = pd.read_parquet(buf, engine="pyarrow")
        logger.info(
            "Read parquet: tier=%s path=%s rows=%d", tier, path, len(df)
        )
        return df

    # ------------------------------------------------------------------
    # JSON I/O (mostly for bronze raw documents)
    # ------------------------------------------------------------------
    def write_json(
        self,
        obj,
        tier: str,
        path: str,
        overwrite: bool = True,
    ) -> None:
        """Write a JSON-serializable object as UTF-8 JSON."""
        data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        client = self._blob_client(tier, path)
        client.upload_blob(
            data,
            overwrite=overwrite,
            content_settings=ContentSettings(content_type="application/json"),
        )
        logger.info(
            "Wrote json: tier=%s path=%s size_kb=%.1f",
            tier, path, len(data) / 1024,
        )

    def read_json(self, tier: str, path: str):
        client = self._blob_client(tier, path)
        try:
            data = client.download_blob().readall()
        except ResourceNotFoundError:
            raise FileNotFoundError(f"Blob not found: {tier}/{path}")
        return json.loads(data.decode("utf-8"))

    # ------------------------------------------------------------------
    # Raw text I/O (e.g. bill text dumps before parquet packaging)
    # ------------------------------------------------------------------
    def write_text(
        self,
        text: str,
        tier: str,
        path: str,
        overwrite: bool = True,
    ) -> None:
        data = text.encode("utf-8")
        client = self._blob_client(tier, path)
        client.upload_blob(
            data,
            overwrite=overwrite,
            content_settings=ContentSettings(content_type="text/plain; charset=utf-8"),
        )
        logger.info(
            "Wrote text: tier=%s path=%s size_kb=%.1f",
            tier, path, len(data) / 1024,
        )

    def write_bytes(
        self,
        data: bytes,
        tier: str,
        path: str,
        content_type: str = "application/octet-stream",
        overwrite: bool = True,
    ) -> None:
        client = self._blob_client(tier, path)
        client.upload_blob(
            data,
            overwrite=overwrite,
            content_settings=ContentSettings(content_type=content_type),
        )
        logger.info(
            "Wrote bytes: tier=%s path=%s size_kb=%.1f",
            tier, path, len(data) / 1024,
        )

    def read_text(self, tier: str, path: str) -> str:
        client = self._blob_client(tier, path)
        try:
            data = client.download_blob().readall()
        except ResourceNotFoundError:
            raise FileNotFoundError(f"Blob not found: {tier}/{path}")
        return data.decode("utf-8")

    # ------------------------------------------------------------------
    # Delete (used by smoke test, cleanup tasks)
    # ------------------------------------------------------------------
    def delete_blob(self, tier: str, path: str) -> bool:
        """Delete a blob. Returns True if deleted, False if it didn't exist."""
        try:
            self._blob_client(tier, path).delete_blob()
            logger.info("Deleted blob: tier=%s path=%s", tier, path)
            return True
        except ResourceNotFoundError:
            return False
