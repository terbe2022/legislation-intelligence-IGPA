# Operator handoff and maintenance

This is the navigation/runbook skeleton for an incoming maintainer. Exact resource identifiers and authorized-access instructions belong in a restricted operations register. No production commands are prescribed until current deployment and side effects are verified.

## First day checklist

1. Obtain approved read access to the source repos and restricted resource register. Confirm a primary and backup owner for every component.
2. Locate `PIPELINE-SERVER`, `MODEL-SERVICE`, `WEB-APP`, `RESULTS-STORE`, `PROFILE-STORE`, `PROMPT-EXPORTER`, secret storage and monitoring using that register.
3. Record the deployed commit/version for the server, web app and exporter; compare against the intended authoritative checkout. Record uncommitted server changes.
4. Inspect the actual schedules, startup/deployment commands and last successful runs without starting jobs or changing them.
5. Follow one synthetic bill and fictional user/prompt through an isolated environment. Establish the data contracts in `COMPONENT_MAP.md`.
6. Walk through a recovery exercise with the outgoing owner and record actual results and gaps.

## Troubleshooting by symptom

| Symptom | Inspect first | Next dependency |
|---|---|---|
| Site unavailable | Web host health, deployment/startup logs and last release | Runtime configuration, identity and networking; verify actual hosting service first |
| New bills missing or general results stale | Ingestion/model-stage timestamps and app-snapshot freshness | Blob publication, snapshot-build completion and app cache/read paths |
| Profile changes not saved | App save request and backend logs using a synthetic account | Cosmos write authorization, partition key, conflict handling and server-side user identity |
| Personalized scores stale while general results are current | Exporter last successful invocation and exported-input freshness | Scoring job/model health, changed-input detection, result publication and app filtering |
| One user sees another user's information | Stop the affected exposure path through the authorized incident process | App authorization, export/result permissions and cache scope; preserve audit evidence |
| Sudden storage/model authorization failures | Approved identity/secret references, expiry and configuration history | Role assignments, network rules and service health; do not paste credentials into tickets |

## Deliberate update procedure

- Identify the affected component and downstream contract before changing code.
- Work in its designated checkout/branch, inspect the diff and test with approved synthetic/isolated fixtures.
- Commit source and document schema/config changes separately from data updates. Preserve the previous deployable version and necessary artifacts.
- Deploy only the intended component through its verified process. A web-app redeploy does not update server code; a server Git push does not update the exporter.
- Verify component health and an end-to-end synthetic scenario, not just “deployment succeeded.” Record the release commit, evidence, operator and recovery point.

## Recovery records that must exist

| Component | Recovery information required |
|---|---|
| Server and model service | Exact code/environment/model revisions, scheduler definition, approved config restoration, intermediate-state/retry rules |
| Blob data | Confirmed versioning/soft-delete/backup policy, current retention, consistent published dataset recovery and restore-test evidence |
| Cosmos | Actual backup mode and recovery window, partition/schema/index requirements, access-restoration and profile isolation tests |
| Web app | Verified build/deploy source, dependencies, startup command, identity/settings references, rollback version and health check |
| Exporter | Source/version, trigger/timezone, last-run checks, idempotency/deletion behavior and safe replay procedure |
| Secrets and access | Approved storage location, responsible owner, rotation/access-grant procedure and break-glass contact; never secret values in Git |

Do not assume these backup features are enabled. Record observed settings and a completed restoration exercise. Reverting code does not roll back Cosmos changes, Blob overwrites or deleted profiles.

## Handoff completion criteria

A second authorized operator can locate every component, identify what is running, follow one synthetic record through the system, deploy an isolated change, troubleshoot a failed handoff between components, and restore the required state using documented instructions. Record which of these were actually demonstrated. The initial documentation map alone does not satisfy these operational checks.
