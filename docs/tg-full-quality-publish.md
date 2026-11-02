# Full TG quality audit and one-time production publication

Retrieval is paused: the resident workflow has no cron or handoff. The one-time quality workflow runs only on the local metadata runner and reads every row in the saved search ledger. It never downloads firmware or performs new Telegram requests.

The audit reconstructs exact message text from private decision logs, then saved public-page caches for missing messages. Each row must match its stored SHA-256; unverifiable rows remain candidates. Current device/release policy and the default-free-except-Elite policy are rerun for all recoverable observations, including previously rejected observations. Rejections and missing facts are individually recorded, rather than reported as a successful sample audit.

The publication contract requires:

- All 12 taxonomy groups and their nested identity/version fields to be present.
- Existing catalogue and production device identity, a canonical project ID/name, an explicitly credited maintainer and derived branch ID.
- A version stated in the release heading/version fields/download description, rather than an unrelated changelog version or generated Telegram message version.
- An exact message link, matching source hash and observed publication timestamp; ROMs additionally require the Android version.
- Current accepted release policy, free access policy, no Elite origin, no unresolved excluded-device or GSI compatibility blocker.

Recognition, stage, variant and other factually unstated fields retain their allowed unknown/null values. Optional missing version facts are listed in `taxonomy.audit_missing_facts`; a post timestamp is never relabelled as a build date. Missing required facts fail the publication contract. An author claim of Official/Stable is not presented as independent endorsement/stability verification.

Qualified records are written into the existing `custom_resources` table and signed with the existing production Ed25519 key. The complete taxonomy/provenance is retained in additive `metadata_json` and `audit_batch_id` columns; the original SP01 schema stays immutable. The signing key is checked against existing production firmware signatures before writes. Deterministic tokens make the publication cohort resumable/idempotent. Requests use bounded atomic chunks within that one full audited cohort, not device-by-device sample publication. Successful row counts, signatures and free eligibility are read back before enabling `phase2_roms_enabled` for the affected existing devices. Existing opt-outs/click/vote counters are preserved.

Private audit products are `audits/full-quality-<run>/report.json`, `decisions.json`, `qualified.json`, publication exclusions, progress and final verification receipts. Retrieval remains stopped after publication.
