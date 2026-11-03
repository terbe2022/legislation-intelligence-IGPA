'[Deployment-specific example removed from this source docstring.]'
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import azure.functions as func

log = logging.getLogger("prompt_exporter")

EXPORT_PATH = "legislation/impact_prompts/prompts_for_scoring.json"
PROFILES_EXPORT_PATH = "legislation/impact_prompts/user_profiles.json"


def _container():
    """One Cosmos saved_items container client."""
    from azure.cosmos import CosmosClient
    endpoint = os.environ["COSMOS_ENDPOINT"].strip().rstrip("/") + "/"
    key      = os.environ["COSMOS_KEY"].strip()
    db_name  = os.environ.get("COSMOS_DATABASE", "legplatform").strip()
    return (CosmosClient(url=endpoint, credential=key)
            .get_database_client(db_name)
            .get_container_client("saved_items"))


def _load_prompts() -> list:
    c = _container()
    docs = list(c.query_items(
        query="SELECT c.id, c.user_id, c.label, c.data, c.created_at "
              "FROM c WHERE c.type = 'impact_prompt'",
        enable_cross_partition_query=True,
    ))
    out = []
    for d in docs:
        q = (d.get("data") or {}).get("question", "").strip()
        if not (d.get("user_id") and d.get("id") and q):
            continue
        out.append({
            "user_id":      d["user_id"],
            "prompt_id":    d["id"],
            "prompt_label": d.get("label") or q[:40],
            "question":     q,
            "created_at":   d.get("created_at", ""),
        })
    return out


def _load_profiles() -> dict:
    """Role profiles → {user_id: {title, sector, functional, stakeholders,
    description}}. The scorer turns each into one always-on synthetic prompt.

    Only profiles with a description are exported (it is the field that
    actually drives targeting). If a user somehow has more than one profile
    doc, the most recently created wins.
    """
    c = _container()
    docs = list(c.query_items(
        query="SELECT c.id, c.user_id, c.data, c.created_at "
              "FROM c WHERE c.type = 'user_profile'",
        enable_cross_partition_query=True,
    ))
    docs.sort(key=lambda d: d.get("created_at", ""))   # newest last → wins
    profiles: dict = {}
    for d in docs:
        uid = d.get("user_id")
        if not uid:
            continue
        data = d.get("data") or {}
        desc = (data.get("description") or "").strip()
        if not desc:
            continue
        profiles[uid] = {
            "title":        (data.get("title") or "").strip()[:200],
            "sector":       (data.get("sector") or "").strip()[:200],
            "functional":   (data.get("functional_role") or "").strip()[:300],
            "stakeholders": (data.get("stakeholders") or "").strip()[:300],
            "description":  desc[:1000],
        }
    return profiles


def _write_to_gold(payload: dict, path: str = EXPORT_PATH) -> None:
    from azure.storage.blob import BlobServiceClient, ContentSettings
    conn = os.environ["LEGSTORAGE_CONNECTION_STRING"].strip()
    container = os.environ.get("GOLD_CONTAINER", "gold").strip()
    svc = BlobServiceClient.from_connection_string(conn)
    blob = svc.get_container_client(container).get_blob_client(path)
    blob.upload_blob(
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        overwrite=True,
        content_settings=ContentSettings(content_type="application/json"),
    )


def main(mytimer: func.TimerRequest) -> None:
    log.info("=== PROMPT EXPORTER START ===")

    prompts = _load_prompts()
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "prompt_count": len(prompts),
        "user_count": len({p["user_id"] for p in prompts}),
        "prompts": prompts,
    }
    _write_to_gold(payload, EXPORT_PATH)
    log.info("Exported %d prompt(s) for %d user(s) → gold/%s",
             payload["prompt_count"], payload["user_count"], EXPORT_PATH)

    # Role profiles — non-fatal: a profile failure must never block the
    # prompt export the nightly scorer depends on.
    try:
        profiles = _load_profiles()
        profiles_payload = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "profile_count": len(profiles),
            "profiles": profiles,
        }
        _write_to_gold(profiles_payload, PROFILES_EXPORT_PATH)
        log.info("Exported %d role profile(s) → gold/%s",
                 len(profiles), PROFILES_EXPORT_PATH)
    except Exception as e:
        log.error("Profile export failed (non-fatal): %s", e)

    log.info("=== PROMPT EXPORTER COMPLETE ===")
