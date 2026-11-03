# Azure information needed to complete the handoff

Start with metadata and schema, not data exports. The public map needs component types and logical relationships; exact identifiers go into a separate approved restricted register.

## First items to provide

1. **Located:** local Git checkout for the Streamlit web app. Confirm its intended GitHub mirror and live deployment resource/settings.
2. **Located:** exporter and digest source under the app checkout’s `digest_function/`. Confirm their deployed Function app, source version, timer setting values/timezone and successful invocations.
3. A redacted resource-group inventory showing resource **types**, component roles and which resources connect. Map actual names to aliases before sharing publicly.

## Inventory by component

| Component | Metadata needed | Where an authorized operator can look |
|---|---|---|
| Web hosting | Actual service type, runtime/version, deployed source/commit, build/startup command, release method, slots if used, health/log location | Hosting resource overview and deployment configuration; pipeline/source settings |
| App configuration | Setting names only and what consumes each one; whether each is a secret reference, endpoint or non-secret flag | Hosting configuration viewed locally; never export values wholesale |
| Sign-in and authorization | Identity provider, user-ID claim, app/backend access checks, service identities, responsible owner | App source and identity/auth configuration; redacted permissions summary |
| Cosmos | API type, logical database/container roles, partition-key paths, indexing/TTL/consistency choices, backup mode and network/identity access | Cosmos resource settings and container schema; no user documents |
| Blob | Logical tier/container roles, artifact prefixes/schema, publication pattern, access identity, network rules, versioning/retention/recovery settings | Storage settings and source read/write paths; no account keys, SAS links or private object data |
| Export Function | App/runtime, code/commit, trigger schedule/timezone, exported document types, schema, auth, retry/dead-letter behavior, deletion propagation and logs | Function source and deployment/trigger configuration |
| Monitoring | Health signals, stage freshness targets, alert routes, dashboards, primary/backup owner and incident procedure | Hosting/Function monitoring and server job monitor |
| Costs/ownership | Resource owner, billing owner, allocation/renewal dependencies and approved spending controls | Restricted resource register and owner confirmation |

Do not send connection strings, keys, passwords, SAS query strings, publish profiles, OAuth/client secrets, live profile records, tenant/subscription exports or complete app-settings downloads. A schema-only example must use invented identities and values.

Portal labels vary by resource/service; these are inspection categories, not a verified click-by-click portal procedure. Capture current metadata with an authorized operator and cross-check it against deployed source before completing the exact register.
