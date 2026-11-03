# D1 publisher compatibility and safe rollout

## Ownership and version contract

Collectors and TG cleaners continue writing their existing local snapshots. Their formats, evidence, classifiers and device traversal are unchanged. Only final publishers access production D1.

The existing `catalogue_publish.yml` now runs a schema job before official, Samsung and TG publication. It applies versions 4–5 once and verifies migration checksums. Publishers independently check readiness before committing any resource or local success receipt. A quota failure or incomplete schema leaves snapshots/outboxes pending. It does not mark them published. No second TG workflow is introduced and pause variables are preserved.

Migrations live in `fmr_core/migrations/`. The Web mirror for guarded upgrades lives in `db-upgrades/`, outside Wrangler's automatic migration directory. **Do not apply guarded upgrades as unexamined raw SQL.** The shared Python runner resolves columns already created by legacy runtime migrations and commits the migration and checksum receipt atomically.

```
python -m fmr_core.schema_migrations --apply --through 5
```

Version 5 adds required resource metadata columns when absent, maintains compact version-substring search and pending extraction ordering, and removes the obsolete voting limiter table. Existing signatures, tokens and resource columns remain compatible. Read requests no longer run ALTER TABLE.

## Model identity: version 6 is a reviewed maintenance operation

The old unique constraint omits model_code. Version 6 replaces it with an expression unique index including the normalized model. It atomically rebuilds firmware and image tables together, retaining all IDs, download tokens, URLs, signatures, counters, extraction states and AUTOINCREMENT high-water marks. Rebuilding both parent and child avoids a deferred-foreign-key violation at commit. Unexpected source columns, indices, triggers or foreign-key dependants must stop the migration rather than discard them.

A large 70,000-row database plus indexes can exceed a Free day's write allowance. The migration runner refuses that operation unless a capacity-reviewed window explicitly sets `FMR_D1_LARGE_MIGRATION_CONFIRMED=true`. This flag is not an account upgrade and must not be set merely to silence a quota error. Quiesce old publisher executions before this operation; use the updated publisher code afterward.

```
python -m fmr_core.schema_migrations --apply --through 6
```

Until version 6 is applied, ordinary identities remain publishable. A colliding model stays queued and is logged as pending, never acknowledged as a successful existing firmware. Local reconciliation invalidates old acknowledgements only for potentially conflated model identities, so a previously silently ignored model can be retried without replaying every release or refetching firmware metadata.

## Successful publication and retry semantics

- The YAML writer checks actual persisted identity/token pairs in bounded 100-parameter batches. It does not interpret D1 `changes` as the number of firmware records: triggers add physical changes.
- Existing official records are read before insertion, avoiding ignored-insert sequence writes. Refresh descriptors, signed URLs, extraction status and public tokens retain their established behavior.
- TG reads only candidate device IDs/current and legacy tokens. Withdrawals use the publisher ownership index and keyset pages, not a scan of unrelated resources.
- TG writes chunks of ten and withdraws old owned rows only after new rows are committed and verified. A partially imported cohort has no final receipt; retry skips unchanged committed rows and finishes the remainder. Opt-outs also protect legacy tokens.
- The website verifier follows project pages and both ROM/recovery tabs. It verifies the actually published token map and allows unrelated publishers' resources to coexist.
- Official, TG, extraction, Sony bundle and public projection entrypoints share Runner reservations. Actual API row counts include triggers and indices; settlement accounts for overages. Web admission is coordinated separately across locations, leaving headroom between its allocation and Runner allocation. Request reservations stop subsequent SQL at the per-request threshold; a single SQL statement cannot be interrupted at an exact row count.

## Verification

Tests cover baseline/v5/v6 official publishing, signed refresh descriptors, complete source rows, native D1/DO execution in Web CI, firmware/image foreign keys and signatures, partially committed TG cohorts, retry idempotency, schema checksum guards, and workflow dependency order. No production firmware payload is downloaded for these tests.

Live rollout on 2026-11-03: version 4 applied successfully, and the production coverage SELECT was verified at exactly 1 row read. Version 5 was rejected with D1 error 7500 (daily read allocation); its transaction rolled back, leaving no firmware_search table or new version-5 indices, and only version 4 in the registry. The routine workflow retries version 5 after quota recovery. Version 6 remains explicitly capacity-gated; model conflicts stay pending. Worker deployment and the SQLite budget namespace succeeded, with workers.dev and preview URLs disabled.

## Three-day offline migration, requested 2026-11-03

- Dedicated workflow: `D1 · 三日断点迁移` (`d1_identity_migration.yml`). Schedule: **2026-11-04, 05, 06 at 09:00 Asia/Shanghai (01:00 UTC)**. GitHub may delay scheduled starts; it cannot promise exact wall-clock execution. The Python entrypoint rejects other dates/years and times before 09:00. A durable per-UTC-day claim prevents a rerun from retrying a failed day.
- All other operational workflows are paused in their existing files. Their exact original contents are retained in `ops/d1_maintenance_restore_workflows.json`; previous vendor/TG pause flags are not discarded. CI remains active and has no production D1 access. On the setup push the dedicated workflow cancels active/queued operational executions, then installs source-write locks. The scheduled migration refuses to start if any other operational execution is still active.
- The website is placed in explicit maintenance with `D1_MIGRATION_MAINTENANCE=true`, before page caches and database admission. Database routes return HTTP 503 with Retry-After and no-store, without any D1 request. Health, static assets, robots and disabled voting remain database-free.
- No half-day budget applies in this window. The migration runs sequentially until completion, the **first error**, UTC day rollover or the workflow timeout. No automatic retry follows any error, including quota rejection or an ambiguous/lost HTTP response. It does not exploit delayed quota enforcement. Error logs/summary use local confirmed progress only; no follow-up D1 request is made after failure.
- Version 5 runs first if needed. Version 6 uses `fmr_core.identity_maintenance`, not the capacity override. Source tables are frozen by D1 triggers. Original non-unique indexes are moved to initially empty shadow tables; complete rows are copied by ascending integer ID, 100 at a time, with all target indexes already present. Every copy/verification batch and its cursor share **one atomic D1 batch**. No source row, firmware payload or secret is downloaded to a runner.
- The authoritative checkpoint is `fmr_identity_maintenance`, not a local file or Action artifact. A failed transaction cannot advance its cursor; a committed request whose response was lost is recognized from the server cursor tomorrow. Exact-column and FK verification runs in resumable batches before cutover. Source column/index/trigger/FK drift is rejected before initialization. Original IDs, public tokens, signatures, metadata, extraction states, counters and AUTOINCREMENT high-water marks are retained.
- Cutover is one atomic rename/trigger/receipt transaction. Immutable originals remain as `stock_firmwares_before_v6` and `extracted_boot_images_before_v6`; large DELETE/DROP work is deliberately excluded from cutover. Both backup and live tables remain write-locked until an explicit release. The atomic v6 entrypoint refuses to overwrite an active staged checkpoint.
- **Three reserved days do not guarantee completion**: indexes and checkpoint writes also count against the account's allowance. After November 6 no additional automatic migration day is authorized. If incomplete, the original data, shadow tables and checkpoints remain in place and production stays paused. Even if completed early, other production work stays paused for this reserved window. Restoring service is a separate, deliberate operation: verify `phase=complete`, version-6 checksum, FK checks and website acceptance; remove `fmr_maintenance_%` triggers atomically; restore the saved workflow files (preserving earlier pauses); clear the Web maintenance flag and redeploy. Retain or delete the immutable backups only after acceptance and a separate cost/storage review.

Local regression tests include interrupted/committed-but-unacknowledged requests, full transaction rollback, corrupt shadow rows, unexpected columns, date/UTC gates and preservation of every source field. A deterministic transaction fixture is replayed against native workerd D1 in Web CI, checking actual batch rollback, FK-safe renames, counters, source locks, model-aware inserts and high-water marks.
