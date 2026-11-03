# Legislative Intelligence Platform

A source-based handoff for a legislative document pipeline, Streamlit application and Azure Functions. It explains the implementation end to end without distributing production documents, user records or credentials.

Start with the [system guide and architecture](docs/SYSTEM_GUIDE.md) or the [five-page architecture overview](docs/Legislative_Intelligence_System_Overview.pdf). Use the [operator runbook](docs/OPERATIONS.md) for setup, updates, troubleshooting and recovery. The [source manifest](docs/SOURCE_MANIFEST.json) records provenance, scrubbing and exclusions; the [knowledge index](docs/knowledge-index.json) points future question-answering tools at those canonical documents.

## Repository layout

| Directory | Independently operated component |
|---|---|
| `components/pipeline/` | Server ingestion, AI extraction, synthesis, standardization, prediction and personalization source |
| `components/webapp/` | Streamlit application, registered pages, authentication, persistence and data loaders |
| `components/functions/` | Prompt-export and digest timer Functions, with their own dependencies and helper copies |
| `docs/` | One system guide, one runbook, evidence/configuration manifests and standalone architecture PDF |
| `tools/` | Offline release checks; no production imports or connections |

The source uses the original component-relative layout. Separate Function helpers are retained because they belong to a separate deployment package; they are not automatically synchronized with the web app's copies.

## Evidence boundary

The web-app source matches the local Git revision in the owner's successful deployment log. Azure CLI/portal output confirms the app/Function hosting, deployed timer names and relevant storage/database metadata. Server source comes from the verified November 2 preservation snapshot; its actual scheduler entry is still unverified. The current local Function source differs from a saved local deployment ZIP and has not been matched to the active Azure package. “Running” and “Enabled” are not evidence of successful end-to-end execution.

This is the production-oriented, source-traced subset, not a claim that every selected server file is currently scheduled. It contains no speculative implementation or new application behavior. The remaining evidence gaps are listed in the runbook.

## Safe review

```sh
python tools/verify_release.py
```

This parses source, verifies manifest hashes and local documentation links, and checks for excluded files and embedded credential patterns. It does not launch Streamlit, connect to Azure, run the model, send email or execute pipeline jobs. Actual use requires approved data/configuration and dependencies; there is no synthetic replacement for the production model or user records in this source release.

Embedded signed URLs/credential literals are externalized into environment settings listed in each component's `.env.example`. Those files are inventories, not automatically loaded everywhere. Preserve current configuration through an approved secret-management process before attempting a deployment. Do not deploy this scrubbed copy over a running installation without restoring and validating its configuration.

Source import baseline: [6e355a6](https://github.com/terbe2022/legislation-intelligence-IGPA/commit/6e355a65eebd25cbc2ad8166f270cf63f142a1e9). Documentation consolidation is committed separately.
