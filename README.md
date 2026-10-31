# FindMyROM Runner

SP01 provides the database and cryptographic primitives. SP02 imports YAML device
and official firmware metadata into SQLite or Cloudflare D1. It does not download
firmware contents or upload Vault assets.

## Setup

Use Python 3.11 or later, install `requirements.txt`, and export
`SIGN_PRIVATE_KEY_HEX` as a 64-character lowercase Ed25519 private key. Keep it out
of source control. For local development, the existing `generate_keys.py` can
create ignored development configuration; the CLI reads exported environment
variables, so load the configuration into your shell before running it.

Set `LOCAL_SQLITE_PATH` to use a local database. Without it, provide
`CF_ACCOUNT_ID`, `CF_D1_DATABASE_ID`, and `CF_API_TOKEN` for remote D1.
A fresh database is initialized from the bundled `fmr_core/schema.sql`. Existing
pre-signature databases receive the SP02 compatibility column and signature
backfill without changing the frozen SP01 migration.

## Offline example

From this directory, with the environment configured:

```sh
python -m fmr_ingest_stock.cli --mode all \
  --devices-source tests/fixtures/sp02_devices_sample.yaml \
  --firmwares-source tests/fixtures/sp02_firmwares_sample.yml

python -m fmr_ingest_stock.cli --mode all \
  --devices-source tests/fixtures/sp02_devices_sample.yaml \
  --firmwares-source tests/fixtures/sp02_firmwares_sample.yml \
  --incremental
```

The first command imports four devices and five firmware records, rejecting one
invalid device and four invalid firmware records. Three ZIPs are `pending`; two
fastboot packages are `skipped`. The second command reports
`"status": "skipped_unchanged"` for the unchanged firmware source; device metadata
is still synchronized.

Both sources accept a local path or a direct HTTP(S) YAML URL with a list root.
Remote metadata has a 30-second timeout per attempt and a 50 MB decoded limit;
only timeouts and 5xx responses receive two retries. Redirects are rejected: use
the final metadata URL. These are metadata requests, never requests to firmware
links contained in the YAML.

Other options are `--mode devices|firmwares|all`, `--brand-id` (default `xiaomi`),
`--priority-codenames marble,alioth`, and `--batch-size` (50–500, default 200).
The last stdout line is a JSON summary. Exit codes are 0 for success, 1 for
runtime/configuration failure, and 2 for invalid CLI arguments.

## Synchronization behaviour

- Existing firmware public tokens remain stable; changed CDN URLs are re-signed.
- `done`, `processing`, and `failed` extraction states, retry counters and errors
  survive re-imports. Priority changes move eligible ZIPs between `pending` and
  `skipped`; fastboot packages remain skipped.
- All batches are atomic. A failed batch leaves previous committed batches in
  place and does not advance the source cursor. Re-run to resume idempotently.
- Concurrent insertions cannot replace a stored token/signature pair with a
  signature for a different token. The affected run fails without advancing its
  cursor and can be re-run.
- Incremental firmware detection hashes source bytes, not the priority list or
  signing key. To change priority policy or re-sign unchanged metadata, run once
  without `--incremental`.
- The seven brand records and `__global__` cursor anchor are internal bootstrap
  data. Future UI queries should exclude the anchor from device listings.
- URL validation follows SP02's literal-host rules; it does not resolve DNS or
  probe firmware availability.

## Verification

```sh
pytest -v
python verify_sp02.py
```

`verify_sp02.py` runs all Runner tests, the two CLI imports, real Ed25519
verification of stored rows, CLI failure checks, and the YAML loader audit. It
uses an isolated temporary database and a disposable test key. It also works
when this Runner directory is checked out without Repo-Web. Remote transport is
mocked in tests; live D1 credentials and production metadata URLs are not
configured by this module.

## SP03 extraction

`python verify_sp03.py` runs the complete offline extraction acceptance suite.
Tests construct small ZIP and CrAU payloads in memory and intercept HTTP with
`httpx.MockTransport`; no real ROM is downloaded on the development computer.

Production entry point: `python -m fmr_extractor.cli`. Use `--dry-run` to recover
stale locks and inspect the queue without downloading or uploading. Argument
limits are `--batch-limit 1..500`, `--max-workers 1..6`, and
`--max-runtime-sec 10..3600`. Production credentials are the D1 variables above
plus `SIGN_PRIVATE_KEY_HEX`, `GH_VAULT_TOKEN`, and `GH_VAULT_REPO`.

The extractor supports ZIP/ZIP64 partition entries and stored CrAU v2 payloads
with REPLACE, REPLACE_BZ, REPLACE_XZ and ZERO operations. Incremental operations
requiring a source partition and newer unsupported codecs fail safely; do not
classify that failure as successful extraction. Payload operation and complete
partition hashes are verified before archiving. A budgeted optional Go executor
adapter is available for payloads that fit the remaining download budget.

Each firmware is limited to 250 MiB of HTTP response body data; ignored Range
responses are closed without consuming their body. Known image size and magic
limits apply before any upload. All compressed images are deterministic and
signed using their compressed hash/size. Root patch hooks are opt-in trusted
callbacks with recorded tool versions; none is configured by default.

Vault visibility is checked before every upload. Public Vaults are rejected.
The production workflow is disabled until repository variable
`FMR_EXTRACTION_ENABLED=true` and the `production` environment's Secrets are
configured. Configure them through GitHub's encrypted Secrets UI/CLI; never
commit them. Start with a manual dry-run in Actions and a small cloud-only real
batch. No images are published as Actions artifacts or public Releases.

Dispatch stops at the runtime deadline; HTTP operations are time-bounded and
in-flight tasks finish or fail before cleanup. HTTP libraries may exceed a
wall-clock deadline slightly while a current I/O operation is unwinding. Stale
processing claims recover after 30 minutes, with at most three failures.


## Production credential verification (2026-10-31)

The cloud-only diagnostic passed D1 reads/writes, private Vault upload, Worker
Ed25519 verification, ticket issuance, byte-for-byte download and edge cache hit.
Evidence: https://github.com/TunJinshou050608/findmyrom-runner/actions/runs/41184710718

It used only a tiny gzip diagnostic text file, never firmware. Temporary D1 rows,
Release assets, the Release and its tag were deleted. No credentials or private
archive URLs were logged. Draft Releases are unsuitable for the read-only gateway
probe; the probe now uses a published Release inside the private Vault, matching
production archiving. The Worker sends GitHub a User-Agent header.

This verifies credential usability and the diagnostic delivery path, not a real
firmware extraction. Real firmware metadata ingestion and extraction remain
unverified. Scheduled extraction stays disabled (`FMR_EXTRACTION_ENABLED` unset).

The local editor environment disconnected during this check. Subsequent source
changes were committed directly to GitHub; synchronize the local source before
publishing another curated snapshot so these fixes are preserved.
