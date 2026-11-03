# Legislation Intelligence: architecture and handoff

Start here to understand the system without needing access to its server or Azure resources. This public repository documents the connections and operator responsibilities; it does not contain user profiles, production data, credentials or an exact server replica.

The application has several independently maintained parts:

1. **Server pipeline:** obtains legislative documents and runs AI extraction, synthesis, standardization and scoring.
2. **Blob Storage:** holds source/intermediate data, curated results and app-ready snapshots.
3. **Web application:** presents searchable/filterable results and user-specific features.
4. **Cosmos DB:** stores user-related application information, confirmed by the project owner.
5. **Export bridge and digest:** inspected local Azure Function code exports user prompts/profile inputs from Cosmos to Blob Storage and implements a scheduled email digest. Live Function deployments still need verification.

Read the [connection map](docs/ARCHITECTURE.md), then the [component directory](docs/COMPONENT_MAP.md). Operators should use the [handoff runbook](docs/OPERATIONS.md) and complete the [Azure inventory checklist](docs/AZURE_INVENTORY.md).

## What is verified

This initial map combines the owner's November 3, 2026 description with static inspection of the November 2 server snapshot. Server source paths are listed relative to the legislation project, so readers can locate implementations without publishing internal hosts or deployment addresses. The local Streamlit web-app source and exporter/digest Function source have now been inspected. The supplied web-app deployment log matches the local HEAD. Azure portal settings, live access-control behavior, actual Function deployments and site health remain unverified. See [the web-app source map](docs/WEB_APP.md).

The [existing legislative intelligence hub](https://github.com/terbe2022/uiuc-legislative-intelligence-hub) currently contains documentation. It must not be mistaken for a verified source checkout of the deployed web app. The owner has identified a local Git checkout containing the web app and Function sources; exact local/deployment locations are recorded in the restricted register. A GitHub source mirror has not yet been established by this documentation work.

## Public documentation versus restricted records

Public docs use logical aliases such as `WEB-APP`, `PROFILE-STORE` and `RESULTS-STORE`. Authorized operators need a separate restricted register mapping these aliases to exact Azure resources, repository/commit identities, server paths, owners and credential-management locations. Credentials themselves must stay in approved secret storage. A private repo is not automatically approved storage for secrets or user records.

No production jobs, account settings, deployment resources or resource visibility were changed to prepare this handoff. Source import, deployment verification and a full recovery exercise remain separate work.
