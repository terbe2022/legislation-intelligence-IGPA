"""
data_catalog.py
================
Single source of truth for all Azure SAS URLs, session IDs, and
file-path conventions. Import this everywhere instead of repeating URLs.

When new sessions complete or SAS URLs are regenerated, update ONLY here.

Last updated: 2026-06-24
Complete sessions: 2176, 2020, 1801
Next in queue:    1630, 1407, 1127, 999
"""

# ---------------------------------------------------------------------------
# Gold parquet SAS URLs (expire 2029)
# ---------------------------------------------------------------------------
GOLD_URLS = {
    # 104th GA (2025-2026)
    2176: (
        __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_GOLD_URLS_L20', "")
    ),

    # 103rd GA (2023-2024)
    2020: (
        __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_GOLD_URLS_L30', "")
    ),

    # 102nd GA (2021-2022)
    1801: (
        __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_GOLD_URLS_L40', "")
    ),
    # 101st GA (2019-2020)
    1630: (
        __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_GOLD_URLS_L48', "")
    ),
}

# ---------------------------------------------------------------------------
# Prediction parquet SAS URLs
# ---------------------------------------------------------------------------
PREDICTION_URLS = {

    2176: (
        __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_PREDICTION_URLS_L63', "")
    ),

    2020: (
        __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_PREDICTION_URLS_L72', "")
    ),

    1801: (
        __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_PREDICTION_URLS_L81', "")
    ),

    1630: (
        __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_PREDICTION_URLS_L90', "")
    ),

    1407: (
        __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_PREDICTION_URLS_L99', "")
    ),

    1127: (
        __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_PREDICTION_URLS_L108', "")
    ),

    112: (
        __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_PREDICTION_URLS_L117', "")
    ),

    999: (
        __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_PREDICTION_URLS_L126', "")
    ),

    21: (
        __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_PREDICTION_URLS_L135', "")
    ),
}

# ---------------------------------------------------------------------------
# Explorer artifact SAS URLs
# ---------------------------------------------------------------------------
EXPLORER_URLS = {
    1630: {
        "metadata": (
            __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_EXPLORER_URLS_L150', "")
        ),
        "faiss": (
            __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_EXPLORER_URLS_L158', "")
        ),
        "config": (
            __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_EXPLORER_URLS_L166', "")
        ),
    },

    2176: {
        "metadata": (
            __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_EXPLORER_URLS_L177', "")
        ),
        "faiss": (
            __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_EXPLORER_URLS_L185', "")
        ),
        "config": (
            __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_EXPLORER_URLS_L193', "")
        ),
    },

    2020: {
        "metadata": (
            __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_EXPLORER_URLS_L204', "")
        ),
        "faiss": (
            __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_EXPLORER_URLS_L212', "")
        ),
        "config": (
            __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_EXPLORER_URLS_L220', "")
        ),
    },

    1801: {
        "metadata": (
            __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_EXPLORER_URLS_L231', "")
        ),
        "faiss": (
            __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_EXPLORER_URLS_L238', "")
        ),
        "config": (
            __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_EXPLORER_URLS_L245', "")
        ),
    },
}

# ---------------------------------------------------------------------------
# Network graph JSON SAS URLs
# ---------------------------------------------------------------------------
GRAPH_URLS = {
    "latest": None,
}

# ---------------------------------------------------------------------------
# Human-readable session labels
# ---------------------------------------------------------------------------
SESSION_LABELS = {
    2176: "104th GA (2025–2026)",
    2020: "103rd GA (2023–2024)",
    1801: "102nd GA (2021–2022)",
    1630: "101st GA (2019–2020)",
    1407: "100th GA (2017–2018)",
    1127: "99th GA (2015–2016)",
    112:  "98th GA (2013–2014)",
    999:  "97th GA (2011–2012)",
    21:   "96th GA (2009–2010)",
}

# Sessions where gold + explorer data is confirmed complete
# Update this list as each session finishes the full pipeline
COMPLETE_SESSIONS = [
    2176,
    2020,
    1801,
    1630,
    # 1407 — in queue
    # 1127 — in queue
    # 999  — in queue
]

ACTIVE_SESSIONS = [
    2176,
    2020,
    1801,
    1630,
    1407,
    1127,
    112,
    999,
    21,
]

# ---------------------------------------------------------------------------
# App snapshot SAS URLs — pre-joined, pre-filtered files built by
# build_app_snapshot.py. These replace the 4-parquet load + joins in
# 4_Active_Bills.py with 2 reads. Regenerate alongside the daily pipeline run.
# ---------------------------------------------------------------------------
SNAPSHOT_URL = (
    __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_SNAPSHOT_URL_L306', "")
)
SNAPSHOT_TRAIL_URL = (
    __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_SNAPSHOT_TRAIL_URL_L313', "")
)

# ---------------------------------------------------------------------------
# Probability trail SAS URL
# Blob: gold/legislation/predictions/llama31_1200/probability_trail/stage2_probability_trail.parquet
# ---------------------------------------------------------------------------
TRAIL_URL = (
    __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_TRAIL_URL_L325', "")
)

# ---------------------------------------------------------------------------
# Validation tracker SAS URL
# Blob: gold/legislation/predictions/llama31_1200/validation/stage2_validation_tracker.parquet
# ---------------------------------------------------------------------------
VALIDATION_URL = None  # TODO: populate after first successful daily run

# ---------------------------------------------------------------------------
# Taxonomy path
# ---------------------------------------------------------------------------
TAXONOMY_PATH = "intended_beneficiary_taxonomy2.json"

# ---------------------------------------------------------------------------
# Azure OpenAI resource (available for app-side briefing generation)
# Resource: <deployment-location> | RG: legislative-analytics | Model: gpt-4o-mini
# Not used by pipeline — pipeline uses local vLLM
# ---------------------------------------------------------------------------
AZURE_OPENAI_RESOURCE = __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_AZURE_OPENAI_RESOURCE_L349', "")
AZURE_OPENAI_MODEL    = "gpt-4o-mini"

# ---------------------------------------------------------------------------
# Bronze manifest SAS URLs (raw LegiScan fields: status, status_desc, committee,
# committee_id, last_action, is_terminal). Joined into Active Bills to power the
# stage breakdown / enrollment KPI — gold does NOT carry status_desc.
# Blob: bronze/legislation/session=<N>/_manifest.parquet
# ---------------------------------------------------------------------------
MANIFEST_URLS = {
    2176: (
        __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_MANIFEST_URLS_L360', "")
    ),
    2020: (
        __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_MANIFEST_URLS_L367', "")
    ),
    1801: (
        __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_MANIFEST_URLS_L374', "")
    ),
    1630: (
        __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_MANIFEST_URLS_L381', "")
    ),
}

# ---------------------------------------------------------------------------
# DATA_CATALOG — assembled from above dicts
# ---------------------------------------------------------------------------
DATA_CATALOG = {
    "Illinois": {
        session_id: {
            "gold":        GOLD_URLS.get(session_id),
            "predictions": PREDICTION_URLS.get(session_id),
            "explorer":    EXPLORER_URLS.get(session_id, {}),
            "manifest":    MANIFEST_URLS.get(session_id),
            "label":       SESSION_LABELS.get(session_id, str(session_id)),
            "complete":    session_id in COMPLETE_SESSIONS,
        }
        for session_id in ACTIVE_SESSIONS
    }
}

# ---------------------------------------------------------------------------
# Embedding model
# ---------------------------------------------------------------------------
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

# ---------------------------------------------------------------------------
# Bill diff SAS URLs — generated by build_diffs.py --upload
# Blob: gold/legislation/bill_diffs/llama31_1200/session=N/bill_diffs.parquet
# Currently empty (0 rows) — populates automatically as bills get amended.
# ---------------------------------------------------------------------------
DIFF_URLS = {
    2176: (
        __import__("os").environ.get('LEG_COMPONENTS_FUNCTIONS_DATA_CATALOG_PY_DIFF_URLS_L418', "")
    ),
}
