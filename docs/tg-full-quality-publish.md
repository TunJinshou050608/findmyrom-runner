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

## B01 grounded cleaning

`TG · 清洗` now enables `FMR_TG_GROUNDED_CLEANER=true`. Only cleaning changes; discovery remains untouched. It processes stored source text through a broad release-signal/device funnel, a static-prefix DeepSeek JSON extraction, strict quote/value validation, and deterministic catalogue/taxonomy mapping. Explicit release announcements need not contain a direct download URL. Alioth's known retail aliases are added within the cleaner only. Generic project names receive post-scoped observed IDs; textual maintainer credits receive channel-scoped observed-name IDs, never verified-account claims.

Direct HTTP requests use top-level `thinking: {type: disabled}`, not the SDK-only `extra_body` wrapper. Four requests can run concurrently, with a default 200-call budget per audit. Successful validated responses are cached by prompt revision, model, device and complete source text. Usage records API cache-hit/miss and output tokens. Cache hits are best effort; no artificial 64-token padding, guaranteed cost, recovered-record count or zero-hallucination claim is made.

API errors, invalid quotes, incomplete JSON or exhausted budgets defer the entire replacement cohort and retain successful cache entries for the next audit. No source fingerprint or publication pointer advances until the cohort completes. Existing free/paid, Elite, stock/mainstream and device gates remain. Source link evidence is recovered from stored message text; no firmware is downloaded.

The existing telegram_clean.yml is updated in place. Workflow enable/disable state is managed separately; this code update does not resume manually paused workflows.
