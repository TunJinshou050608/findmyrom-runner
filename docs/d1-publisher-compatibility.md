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

This rolled-back build deliberately keeps the legacy natural-key publisher. A colliding model is treated as the existing firmware (`preserved_existing`) and acknowledged, matching the pre-migration behavior. Version 6 can still be applied later, but this build does not write a second model row; restoring model-aware publishing requires the reverted identity patch.

## Successful publication and retry semantics

- The YAML writer checks actual persisted identity/token pairs in bounded 100-parameter batches. It does not interpret D1 `changes` as the number of firmware records: triggers add physical changes.
- Existing official records are read before insertion, avoiding ignored-insert sequence writes. Refresh descriptors, signed URLs, extraction status and public tokens retain their established behavior.
- TG reads only candidate device IDs/current and legacy tokens. Withdrawals use the publisher ownership index and keyset pages, not a scan of unrelated resources.
- TG writes chunks of ten and withdraws old owned rows only after new rows are committed and verified. A partially imported cohort has no final receipt; retry skips unchanged committed rows and finishes the remainder. Opt-outs also protect legacy tokens.
- The website verifier follows project pages and both ROM/recovery tabs. It verifies the actually published token map and allows unrelated publishers' resources to coexist.
- Official, TG, extraction, Sony bundle and public projection entrypoints share Runner reservations. Actual API row counts include triggers and indices; settlement accounts for overages. Web admission is coordinated separately across locations, leaving headroom between its allocation and Runner allocation. Request reservations stop subsequent SQL at the per-request threshold; a single SQL statement cannot be interrupted at an exact row count.

## Verification

Tests cover baseline/v5/v6 official publishing, signed refresh descriptors, complete source rows, native D1/DO execution in Web CI, firmware/image foreign keys and signatures, partially committed TG cohorts, retry idempotency, schema checksum guards, and workflow dependency order. No production firmware payload is downloaded for these tests.

Live rollout on 2026-11-03: version 4 applied successfully, and the production coverage SELECT was verified at exactly 1 row read. Version 5 was rejected with D1 error 7500 (daily read allocation); its transaction rolled back, leaving no firmware_search table or new version-5 indices, and only version 4 in the registry. The routine workflow retries version 5 after quota recovery. Version 6 remains explicitly capacity-gated; this build keeps the legacy natural-key publisher, so model conflicts preserve the existing row and are acknowledged instead of staying pending. Worker deployment and the SQLite budget namespace succeeded, with workers.dev and preview URLs disabled.
