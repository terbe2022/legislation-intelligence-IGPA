# Release maintenance instructions

Read docs/SYSTEM_GUIDE.md and docs/OPERATIONS.md. Keep these as the canonical descriptive and operational documents; do not create parallel copies of the same facts.

This is a scrubbed, production-oriented source subset with explicit deployment-evidence gaps. Verify the source manifest and current entry points before adding/removing code. Preserve provenance and existing originals. Keep Function helper copies separate where their packaging requires them; do not unify them without deployment tests.

Never publish credentials, signed URLs, user records, local.settings.json, exact internal resource registers or preservation archives. Populate configuration only outside Git. Do not execute production scripts, send email, run model jobs, redeploy, alter visibility or modify Azure settings merely to test this repository.

Public claims must distinguish architecture/source evidence from successful runtime tests and measured business outcomes. A future Teams agent should cite the knowledge index and canonical source sections, respect access tiers and retain uncertainty. This repository does not deploy such an agent.
