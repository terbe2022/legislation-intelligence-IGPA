# Operator runbook

Use [SYSTEM_GUIDE.md](SYSTEM_GUIDE.md) for architecture. This runbook covers location, configuration, safe review, update boundaries, troubleshooting and recovery without duplicating that guide. Exact hosts/resources and owner contacts are in the restricted deployment register. No production jobs were run while preparing this release.

## 1. What can run safely now

From the repository root:

```sh
python tools/verify_release.py
```

The check uses only Python's standard library. It validates syntax and evidence files without importing application modules. It is not a substitute for UI integration tests, model evaluation, Azure authorization tests, restore tests or a production run.

## 2. Configure an isolated environment

The three directories under `components/` are separate working/deployment roots. Do not install all their requirements into a single assumed production environment. Use the recorded deployed runtime and a separate virtual environment for each, then establish a tested lock before operational use.

| Component | Dependencies/configuration |
|---|---|
| Web app | `requirements.txt`, `.streamlit/config.toml`; named Cosmos, email, authentication/session and Azure OpenAI settings; configured data-product locations from the catalog |
| Functions | Its own `requirements.txt` and `host.json`; named Cosmos/email/AI/storage settings, timer settings, digest state/test/diagnostic controls |
| Server | Captured requirements, GPU/model service and compatible model revision, restored fitted model files and source data; local configs copied from the templates; environment/secret references used by wrappers |

Established setting families include `COSMOS_ENDPOINT`, `COSMOS_DATABASE`, `COSMOS_KEY`; `ACS_CONNECTION_STRING`, `ACS_SENDER_ADDRESS`; `MAGIC_LINK_BASE_URL`, `MAGIC_LINK_SECRET`, `SESSION_TTL_DAYS`, `APP_PASSWORD`; `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_KEY`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION`; Function storage/timers such as `LEGSTORAGE_CONNECTION_STRING`, `GOLD_CONTAINER`, `PROMPT_EXPORT_CRON`, `DIGEST_CRON`; and server model/storage/telemetry configuration. Names are not values, and not every setting is required by every component.

The scrubbed source additionally externalizes embedded values into the generated `.env.example` keys. Set those in the process environment from an approved configuration store. The inventory is not an automatic dotenv loader for every component. Server YAML/JSON templates require a configured local copy at the original path expected by the source (remove `.example` from its filename). Restore paths/keys locally; do not commit populated configs.

Use synthetic user identities and approved test data for validation. Do not redirect production mail to arbitrary recipients, disable authentication, make storage public or run live server scripts simply to see whether the app starts.

## 3. Deployment/update boundaries

| Component | Observed release method | Missing operational evidence |
|---|---|---|
| Web app | Local Git push to Azure `master`, remote Oryx Python build; observed successful release matches local source | Approved isolated test/release/rollback procedure; current live health and app-to-plan mapping |
| Functions | Azure CLI Functions ZIP deployment, remote Oryx build; Deployment Center reports no configured CI/CD | Exact publishing command/package manifest, deployed source-to-local match, clean release and rollback verification |
| Server | Source contains model-service gating and orchestration/personalization wrappers | Actual scheduler definition/timezone, selected wrapper, current source revision and safe replay/rollback behavior |

The original production checkout must remain untouched by this documentation/import work. Work on a branch/copy. Review diffs, run safe checks, preserve a recovery point, deploy only the intended component and verify an end-to-end synthetic scenario. A GitHub push does not deploy this monorepo to Azure or the server. Do not push this repository blindly to the existing Azure remote: its root layout differs from the original web-app deployment root.

For a web-app deployment, the release package root is `components/webapp/`, not the monorepo root. For a Functions release it is `components/functions/`, including `host.json` and function folders. For the server, retain component-relative imports/config paths. Deployment instructions must be finalized from the actual operator procedure before changing the running system; this guide intentionally does not prescribe an unverified publish command.

## 4. Troubleshooting by symptom

| Symptom | Start here | Verify next |
|---|---|---|
| Site fails to start | Host deployment/startup log, Python/Streamlit requirements | Configuration names, catalog locations, AI/library compatibility; do not expose values in logs |
| Bills/results are stale | Ingestion/stage completion and app-ready publication | Model health, server wrapper chain, Blob artifact integrity and app cache freshness |
| Profile/session fails | App-side identity/session helpers and Cosmos access | Actual partition keys/TTL and caller authorization; distinguish app auth from resource authentication |
| Personalized results stale | Exporter invocation and exported-input publication | Scorer/investigator logs, user/prompt/bill IDs, result publication, app user filtering |
| Digest repeats or seems unchanged | Digest invocation, state read/write and freeze/test controls | Match local and deployed code; state-freeze is not email dry-run |
| Failed email | Function/app email helper and service operation status | Authorized sender, configured recipient/test behavior and failure handling |
| Auth/storage errors | Approved service identity/secret references and expiry | Scope, firewall/network settings and access owner; do not automatically obtain account keys |

Do not equate “Running,” “Enabled,” successful deployment or a row count with a successful complete workflow. A failed process can leave partial outputs; a code revert does not undo data writes. Preserve evidence and inspect the last completed stage before replaying work.

## 5. Preservation and recovery

Original server and web-app preservation copies remain outside Git. A deployable restoration also requires secrets/configuration, model artifacts, schemas/permissions and required data products. Source code alone cannot restore user profiles or historical outputs.

Observed Cosmos configuration uses periodic backups; Blob metadata reports soft-delete retention but does not establish versioning or point-in-time restore. Exact windows and diagnostic state are retained in the restricted register. Neither observation is evidence of a tested independent backup. Establish and test the full recovery path with the authorized platform owner before relying on it.

Never treat the Functions runtime host/secrets storage containers as project datasets or publish/copy their contents into Git. Keep profile inputs, per-user scoring results and diagnostic trails in appropriate restricted storage. Do not load untrusted fitted-model/pickle artifacts during recovery.

## 6. Remaining handoff evidence

- Actual server crontab/service entry and timezone; current source revision and any unsaved changes.
- Source identity of the active Functions package, plus recent successful exporter/digest invocations and effective schedule timing.
- Blob artifact existence/freshness: metadata reads for the expected snapshot and exports were denied to the review identity. An authorized operator must complete this check.
- Actual writer/readers for the extra Cosmos impact-results container, additional Blob container and provisioned Azure Search resource. Do not claim active use from resource existence alone.
- Verified app authentication/user isolation, service identities, network restrictions and approved external model routing.
- Primary/backup owner, access request process, secret rotation location, incident contact and change/release responsibility for every component.
- Tested restoration and a second operator's successful walkthrough of locate -> explain -> isolated update -> diagnose -> recover.

Do not remove these gaps from the documentation until evidence closes them. Add the evidence date and source reference rather than silently changing an assumption into a fact.

## 7. Future Teams question-answering agent

`knowledge-index.json` maps topics to this guide, the system guide and source manifests. A future agent should cite the relevant document section/source path, distinguish inspected code from observed configuration and operational proof, and answer “not yet verified” for open items. Public answers must not draw from restricted resource registers or user records. No Teams bot, connector, deployment, automatic action or secret access is implemented by this release.
