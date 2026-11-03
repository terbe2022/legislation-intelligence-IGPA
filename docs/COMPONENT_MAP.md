# Where to find each part

Paths below are relative to the preserved server project. They are source navigation aids, not verified production commands.

| Logical component | Where to start | What to inspect |
|---|---|---|
| Legislative ingestion | `stages/ingestion.py`, `configs/ingestion.yaml` | Source retrieval, incremental manifest/change handling, raw output and dev-mode differences |
| Document chunking | `stages/chunker.py` | Chunk boundaries, intermediate records, reading order and source IDs |
| AI feature extraction | `stages/extractor.py`, `services/vllm_service.py` | Model interface, prompt/schema versions, evidence and retry behavior |
| Synthesis and standardization | `stages/synthesizer.py`, `stages/standardizer.py` | Bill-level consolidation, normalized output schema and storage contracts |
| Embedding support | `stages/embedder.py` | Model/version, indexing outputs, and whether used in the current job |
| Blob adapter | `services/azure_blob_service.py` | Tier mapping, serialization, read/write calls and identity/config sources |
| Legislative orchestration | `run_legislation_daily.py`, `run_production.sh`, `orchestration/` | Which stages actually run; schedule, environment and model-service prerequisites |
| Predictions and validation | `run_daily.py`, `build_probability_trail.py`, `build_validation_tracker.py` | Scoring updates, historical trails, validation outputs and current invocation |
| App-ready data | `build_app_snapshot.py` | Snapshot/trail generation, active-bill filtering, output schema and refresh dependencies |
| Personalized impact scoring | `score_impact_prompts.py`, `score_chunks.py` | Exported input consumption, per-user/prompt/bill results and downstream permissions |
| Profile design notes | `settings_profile_schema.md` | Intended fields, consent semantics and integration assumptions; compare with actual app code |
| User interface and application backend | Local app checkout: `app.py`, `views/`, `user_auth.py`, `user_profile.py`, `team_service.py` | Streamlit UI, persistence, app-side authentication/session logic and loaders; see WEB_APP.md |
| Cosmos-to-Blob exporter | App checkout: `digest_function/prompt_exporter/__init__.py` and `function.json` | `%PROMPT_EXPORT_CRON%` timer, exported inputs, permissions/freshness; deployed settings pending |
| Email digests | App checkout: `digest_function/digest_timer/__init__.py`, `function.json`, `email_service.py` | `%DIGEST_CRON%` timer, Cosmos and Blob reads, AI generation, email and Blob state |
| Monitoring and scheduling | Server `logs/`, external job-monitor project; actual scheduler pending | Last successful run by stage/component, alerts, ownership and recovery instructions |

Multiple entry-point and backfill scripts exist. Do not choose an entry point merely because its filename says “daily” or “production.” Match it to the actual scheduler/deployment before documenting an executable run procedure.

## Required integration contracts

For each edge in the architecture map, record the producer, consumer, logical artifact, schema/version, stable IDs, write mode, publication/completion signal, freshness target, authorization boundary and failure owner. Obtain these from both sides of the deployed integration.

Minimum contracts to establish:

1. Legislative inputs to the ingestion manifest and document/chunk IDs.
2. Extracted and standardized features to the app-ready snapshot builder.
3. Snapshot/trail outputs to the web app's loading/cache code.
4. Profile/prompt writes to the Cosmos exporter, including partition-key behavior.
5. Exported inputs to personalization scoring and per-user results back to the app.
6. All component logs to monitoring/alerts.

## Sources and their limits

The server snapshot verifies the named files and code references. The owner confirms that the live system has a web app, Cosmos profile storage and Blob data storage. Azure resource names, deployment IDs, a GitHub mirror for the app/exporter, resource permissions, actual runtime settings and live data contracts remain unverified. The local checkout is identified and its app/exporter/digest source inspected; the supplied web deployment log matches its HEAD. Do not infer a deployed component from an example config or a roadmap note.
