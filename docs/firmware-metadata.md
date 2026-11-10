# Official firmware metadata contract

Each existing `catalogue_<vendor>.yml` continues to call
`staged_catalogue collect` → `vendor_catalogue.run`. No extra workflow,
firmware download or D1 credential is added to collectors.

- `oem_sources.system_fields` separates raw vendor OS labels from Android versions.
- `release_metadata.normalize` emits schema-1 public fields, retaining the original
  full version and archive name. Google channels and Sony CDF/customization remain
  explicit. Download identities are not derived from display labels.
- `samsung_metadata.attach` reads the exact model/CSC official update index and its
  language page through `MetadataHTTP`/`CachedClient`. It matches AP only for public
  changelog facts, never for deduplication or download selection. Missing notices
  preserve valid packages and are retried during a subsequent model refresh.
- `catalogue_job.collect_model` merges Samsung history by normalized complete tuple.
- `vendor_catalogue.input_hash` includes OS/date/size/source metadata. A metadata-only
  change preserves resolved downloads. `upgrade_metadata` upgrades at most 50 old
  resolved checkpoints per batch with persistent cursors and a metadata revision;
  full batches request continuation. It never resets collection history.
- `catalogue_publish.candidates` admits dirty metadata on previously published rows.
  The existing ledger fingerprints the release and metadata revision. A new revision
  may incrementally republish existing records; the normal record/write budgets apply.
- `metadata_publish.publish` compares content hashes, preserves known values during
  source outages, updates public metadata and stock Android/date/size in ONE atomic
  batch, and checks persistence before a local success receipt. Replays are no-ops.
- Different models under an old conflicting unique constraint remain pending;
  they never overwrite another model's metadata or receive false acknowledgements.

## D1 ownership and rollout

The existing `catalogue_publish.yml` schema job still applies only `--through 5`.
It then explicitly invokes:

```
python -m fmr_core.firmware_metadata_schema --apply
```

This creates only `firmware_metadata(public_token PRIMARY KEY, metadata_json,
content_hash, updated_at)`. Tokens logically reference existing firmware records;
there is deliberately no new FK dependency on tables covered by the historical
v6 rebuild guards. Writers validate the existing firmware token before saving.
No existing table/identity/index is rebuilt. The Web mirror is
`db-upgrades/firmware_metadata.sql`, outside automatic Wrangler migrations.

Missing/drifted metadata schema stops publication before a success receipt.
Provider daily quota failure stops subsequent database work and budget settlement;
a local UTC-day marker suppresses further publisher requests until the next day.
Collectors continue. Existing pause switches, ten-minute schedule and artifact
retention remain unchanged. The metadata table is filled incrementally, never by
an unbounded production backfill.

Web queries and the public Markdown index consume the same public projection.
Stock dates are updated before SQL ordering/pagination. Web reads tolerate an
absent metadata table during rollout, and older records retain legacy labels.
The A71/XAA static supplement remains only as a transitional fallback for records
without formal metadata; a formal row always takes precedence. Remove the static
file only after its affected rows have been republished and verified on the site.

Tests cover metadata-only updates, atomic failure/replay, exact model isolation,
all active vendors, optional source failures, UTC quota handling and stable
signatures/tokens/extraction state. No production firmware payload is downloaded.

## Acceptance fixes, 2026-11-10

- Public JSON remains schema 1. Checkpoint normalization has its own revision 2,
  so gradual metadata upgrades do not change Web's public schema contract.
- Google build identities come from the official archive filename. Carrier/date
  order in captions is irrelevant. Historical caption-derived records are reused
  only after an exact device/model/package/legacy-key lookup and official-file
  match; mismatches stay pending. Public carrier labels never rekey old URLs.
- Atlas OS parsing receives the vendor: numeric Nothing OS labels remain vendor
  OS versions. `realme UI` is recognized. A proven legacy Nothing OS-as-Android
  value can be cleared while a separate known Android version is retained.
- Repeated Samsung AP notices retain only agreed Android/date/patch fields.
  Different dates do not invalidate unrelated AP notices or remove packages.
- Sony public source provenance excludes service query parameters.
