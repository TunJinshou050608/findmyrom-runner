# FindMyROM Runner

https://findmyrom.com

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

The diagnostic above verifies credentials. A separate real-firmware acceptance
run has now passed, as documented below. Scheduled extraction stays disabled
(`FMR_EXTRACTION_ENABLED` unset).

The source changes committed during the disconnected session have since been
reviewed and synchronized back into the local workspace (2026-10-31).


## Phase 1 real-firmware acceptance (2026-10-31)

The manual cloud-only `phase1_acceptance.yml` workflow passed one official full
Recovery ZIP through signed ingestion, HTTP Range extraction, private Vault
archiving, SSR views, download tickets, streamed SHA-256/length verification and
edge cache hits for two stock partitions. Real production records are retained.
Evidence: https://github.com/TunJinshou050608/findmyrom-runner/actions/runs/41185952812

The source ZIP was 2,557,402,968 bytes; extraction consumed 15,294,251 response
body bytes across 446 Range requests, with no full-ROM fallback. All real image
handling and verification downloads ran on GitHub's cloud runner. No real images or
production source metadata are published as Actions artifacts or checked into Runner.

Source selection is held in encrypted production Secret `FMR_PHASE1_CONFIG`.
The verifier pins the upstream metadata commit, selects one exact Stable full
Recovery build, checks its recorded MD5 against the reviewed selection, and
checks Range support before ingestion. It never claims to have recomputed the
full-ROM MD5. The public script contains no production selection records or URLs.

This is one successful direct-ZIP case, not proof of all brands, modern OTA
codecs, root patching or successful on-device flashing. SP06 remains the next
index/Bot extension; it is not a prerequisite for this delivery loop. Scheduled
extraction remains disabled pending controlled data and format expansion.


## Autonomous historical coverage experiment

`history_sweep.yml` is a finite, manually started experiment, separate from the
regular extraction schedule. Start it on a frozen source tag; the selected
devices are supplied only by encrypted production Secret `FMR_HISTORY_CONFIG`.
It discovers both MIUI and HyperOS archive indexes, follows history pagination,
parses full Recovery/Fastboot packages, and signs the discovered catalog into D1.
It does not substitute the latest-only tracker for a historical archive.

All subsequent jobs use the original tag and verify the original source SHA.
The workflow's repository-scoped ephemeral `GITHUB_TOKEN` has Actions write
permission solely to dispatch the next bounded batch. It never uses the Vault
credential for workflow control. Each batch is at most 12 catalog entries with
two extractor workers, a 50-minute scheduler deadline, and the existing 250 MiB
per-firmware response-body limit. The next batch starts automatically, with
finite attempt limits; no cron or assistant polling is required.

Only the frozen manifest's firmware tokens enter this test's scheduler. Checks
include both source links, site redirects, actual package partition inventory,
image SHA-256/length and cache hits. Fastboot archives are indexed, but the
existing extractor has no full-TGZ random-access implementation; versions with
no successfully verified corresponding Recovery images remain coverage gaps.
Unsupported codecs, lost links, missing source history, conflicts under the
frozen schema, and exhausted attempts remain failures. The extraction algorithm
is not patched mid-run to rescue individual versions.

Detailed source manifests and per-version checkpoints stay in private D1's
existing `crawler_sources` table. No binary artifacts or private archive IDs are
published in Actions logs. Public step summaries contain aggregate coverage
counts and errors; an in-progress batch explicitly is **not** an overall pass.
A terminal run with any requested coverage gaps exits nonzero. Source indexes
can demonstrate only observable public history, not withdrawn or private builds.

## Historical multi-manufacturer catalogue (retired)

`oem_catalogue.yml` discovers models dynamically rather than reading a fixed
case list. Run on a frozen tag with `brands=all` (or `web`, or comma-separated
vendor IDs). Native Xiaomi archive and Google OTA/Factory adapters are combined
with provenance-labelled public community indexes for Samsung, Motorola,
OnePlus, OPPO, realme, vivo, ASUS and Nothing. Huawei/Honor product discovery is
not full firmware support. Sony is deliberately deferred by the user.

Default `mode=audit` writes only private D1 checkpoints. `publish` adds resolved
stock links insert-only; it preserves existing firmware identities and extraction
inputs. It rejects unsupported origins and expiring signed downloads. Newly
published metadata has extraction status `skipped`; **this workflow does not run
image extraction or claim an image-verification pass**. Full source history,
authenticated/encrypted downloads and real package coverage remain separate
acceptance gates. Missing sources and unresolved downloads are terminal gaps.

Leave run ID empty and generation zero to start. Continuations retain the frozen
tag/SHA, save progress privately and self-dispatch with the ephemeral Actions
token. Public summaries contain only aggregate results. No real firmware bodies
are requested by the metadata adapters, including when resolving download-page
redirects. The independent concurrency group does not control existing history
experiments. A green continuation is not a completed all-manufacturer pass.

The extraction engine additionally understands bounded REPLACE_ZSTD operations,
ZIP_STORED nested Pixel factory image ZIPs, and unencrypted ZIP_STORED Samsung
AP TAR members containing raw or LZ4 images. Compressed parent containers,
unsupported encryption, incrementals requiring a base image and exceeded byte
budgets fail closed, without full-ROM fallback. These new branches are covered
by synthetic offline tests, not yet a multi-OEM real-firmware acceptance run.

## Current operation: one workflow per manufacturer (incremental v2)

The old mixed `oem_catalogue.yml` workflow is retired. Use a manufacturer entry
below, on `main`; no frozen tag or device whitelist is needed for normal operation.
Sony remains deferred pending the user's adapter. Huawei/Honor have manual product
catalogue checks only and still have no accepted full-firmware source.

| Manufacturer | Actions entry |
| --- | --- |
| Xiaomi / Redmi / POCO | [小米：固件目录更新](https://github.com/TunJinshou050608/findmyrom-runner/actions/workflows/catalogue_xiaomi.yml) |
| Google Pixel / Nexus | [Google：固件目录更新](https://github.com/TunJinshou050608/findmyrom-runner/actions/workflows/catalogue_google.yml) |
| OnePlus | [一加：固件目录更新](https://github.com/TunJinshou050608/findmyrom-runner/actions/workflows/catalogue_oneplus.yml) |
| Samsung | [三星：固件目录更新](https://github.com/TunJinshou050608/findmyrom-runner/actions/workflows/catalogue_samsung.yml) |
| Motorola | [摩托罗拉：固件目录更新](https://github.com/TunJinshou050608/findmyrom-runner/actions/workflows/catalogue_motorola.yml) |
| OPPO | [OPPO：固件目录更新](https://github.com/TunJinshou050608/findmyrom-runner/actions/workflows/catalogue_oppo.yml) |
| realme | [realme：固件目录更新](https://github.com/TunJinshou050608/findmyrom-runner/actions/workflows/catalogue_realme.yml) |
| vivo | [vivo：固件目录更新](https://github.com/TunJinshou050608/findmyrom-runner/actions/workflows/catalogue_vivo.yml) |
| ASUS | [ASUS：固件目录更新](https://github.com/TunJinshou050608/findmyrom-runner/actions/workflows/catalogue_asus.yml) |
| Nothing | [Nothing：固件目录更新](https://github.com/TunJinshou050608/findmyrom-runner/actions/workflows/catalogue_nothing.yml) |
| Huawei | [华为：检查来源缺口](https://github.com/TunJinshou050608/findmyrom-runner/actions/workflows/catalogue_huawei.yml) |
| Honor | [荣耀：检查来源缺口](https://github.com/TunJinshou050608/findmyrom-runner/actions/workflows/catalogue_honor.yml) |

Click **Run workflow** and leave `cycle` empty and `batch=1`. The default
`operation=incremental`, `mode=audit` resumes pending work and checks only due
sources. Normal supported entries also run every six hours at staggered times.
Huawei/Honor are manual-only while their firmware sources remain blocked.

- `retry_failed` also rechecks failed/blocked tasks. Successful fresh models and
  resolved links remain reusable. **Re-run failed jobs** also resumes actual work;
  terminal gaps are retried rather than reported as a fake successful stale skip.
- `refresh_sources` revalidates model/history pages, preserving the task database;
  it is not a delete-and-rebuild operation.
- `mode=publish` incrementally adds already-resolved stable firmware links, with
  existing tokens/signatures/extraction inputs preserved. Audit results can be
  published later without repeating successful link discovery. Neither mode
  starts binary extraction or claims an image acceptance pass.

Each workflow owns its concurrency group and private D1 namespace. A new job
loads durable state automatically, imports old mixed-job checkpoints once, and
continues the existing queue. Model and release tasks have stable keys across
run IDs; successes are not scoped to a single experiment. Stale continuations
are discarded. A changed manual operation starts a new cycle over the same data.
Scheduled jobs resume an active cycle without silently changing its mode.

The text-only HTTP cache persists bounded compressed metadata in private D1.
Device/model index pages refresh after one hour; model history tasks after six
hours; immutable version-detail pages and stable resolved links after 30 days.
Fresh cache entries avoid network requests. Expired entries use ETag or
Last-Modified conditional requests when available; a 304 reuses the saved body.
Changed/new source entries enqueue only the corresponding release records.
Download landing/go-token pages are never persisted. Temporary/signed download
URLs remain gaps rather than being advertised as permanent links. Failed tasks
have finite retries and a 24-hour retry window unless explicitly retried.

Each batch handles up to four model tasks and 100 release tasks. A 45-minute
HTTP/fanout budget hands remaining work to the next batch instead of consuming
its 90-minute hard job limit. The checkpoint import is itself paginated and
resumable. Existing legacy rows are preserved and the separate frozen historical
image experiment is outside this migration.

Open the collection step for Chinese phase/progress messages, checkpoint counts
and a 30-second liveness heartbeat. A heartbeat explicitly reports execution and
checkpoint age; it does not claim a source request has succeeded. The step
summary shows model/release status counts and network/cache/304 totals. GitHub
usually exposes the summary when its step ends; the step log is the live view.
Individual firmware identifiers, source URLs, raw page bodies, signatures and
private Vault IDs stay out of public logs. Detailed state remains in private D1.
All local tests use synthetic metadata/archives; real firmware work remains cloud-only.

Changes to a manufacturer's own workflow file on `main` also trigger only that
manufacturer's incremental audit. This provides a direct initial handoff from
the retired mixed workflow; normal manual and staggered scheduled runs remain
available. Runtime source collection is still text-only and cloud-only.

## 2026-10-31: live publication, D1 quota protection and Sony complete bundles

The lack of new website entries was caused by the production workflows running
in `audit` mode: resolved records stayed in private checkpoints. The normal
workflow default is now **publish**. Already-resolved records are published
first, incrementally, without refetching their source or waiting for every
manufacturer to finish. Existing tokens, signatures and extraction inputs stay
unchanged. Successful image uploads already commit individually to D1.

A user-requested pause blocks cloud database startup before **2026-11-01 00:00
UTC**. Existing workflows were disabled and active D1 jobs cancelled with their
checkpoints retained. `quota_resume.yml` schedules a handoff at **00:07 UTC** on
2026-11-01 (GitHub scheduling can be delayed). It enables manufacturer workflows
and dispatches incremental publication. One indexed read then resumes the saved
frozen image experiment on its original tag/generation, if it is unfinished.
The retired mixed workflow stays disabled. Nothing is re-discovered from zero.

The collector now maintains atomic persistent counters and ordered scan cursors,
using the existing source-key index instead of grouping/sorting every historical
row in every batch. It reserves a conservative per-project metadata allowance
of 2 million reads and 40,000 writes per UTC day; every job reserves 100,000
reads / 2,000 writes. D1 response usage is metered, with room for a final
checkpoint. Unused reservations are deliberately not reclaimed. This is not a
measurement of other databases or all account traffic: room remains for website
visitors and the finite image test. A fresh UTC day allows pending jobs to carry
on. No subscription or paid D1 upgrade has been enabled.

`catalogue_sony.yml` reads the user's XperiFirm-compatible resource catalogue
and Sony's ESS XML services. A real metadata-only probe discovered 216 hardware
models and resolved a current customization service with three components.
No real firmware was downloaded locally. The source distinguishes current
service availability from complete historical coverage. Sony's transport chunks
are private implementation details: **one release becomes one complete firmware
ZIP download**, not a list of individual chunks. SIN/partition extraction remains
disabled. The matched preferred service supplies its own version number.

Sony bundling is on demand via `sony_bundle.yml`. A cloud runner downloads the
original components, resumes local partial chunks safely, creates one ZIP64
archive containing all original components, validates its ZIP CRCs, and uploads
it to a private temporary R2 bucket through an authenticated multipart gateway.
No full firmware archive is stored in GitHub Releases. The site issues a signed
download ticket and streams that ONE ZIP, with HTTP Range support and SHA-256.
Completed local archive checkpoints are reusable. A new cloud VM cannot retain
the old VM's temporary files; it can have to reacquire interrupted build inputs.

The service retains a single temporary object of at most 8 GiB plus small ZIP
headers and permits at most two complete builds per UTC day. Old cached Sony
bundles are evicted when a new one is uploaded; multipart state is cleaned up
before another allocation. Configure a one-day lifecycle for `sony/` and
incomplete multipart uploads when enabling the dedicated bucket. It is an
on-demand cache, not a permanent mirror of every large ROM. These limits apply
to this service; they do not account for unrelated account storage.

**Activation still needed:** Cloudflare returned error 10042, requiring R2 to
be enabled in the dashboard. After enabling R2, create a dedicated PRIVATE
`findmyrom-sony-temp` bucket, bind it to Web as `SONY_BUNDLES`, and configure a
new shared random `SONY_BUNDLE_UPLOAD_SECRET` in both Worker secrets and Runner's
production environment. Then enable `sony_bundle.yml`. Until that is configured,
the builder exits before firmware downloads/D1 reads and the site says the
complete-package service is not ready. Do not publish the secret, resource XML,
raw component URLs or any real firmware as Actions artifacts. The included
resource-format key is public protocol metadata, not an account credential.

### SP06 public index and Telegram entry points

`python -m fmr_extractor.jumper_sync --output-dir ./public-index` exports a
strictly public Markdown projection of the existing database. Only verification
links on `https://findmyrom.com` are exported; firmware, private asset locations,
signatures and download URLs never enter this tree. Exports are deterministic,
remove obsolete Markdown, limit each device to 50 versions and each file to
512,000 bytes. `LOCAL_SQLITE_PATH` supports completely offline use.

Actions → **Publish public firmware index** (`jumper_sync.yml`) runs every six
hours or by manual dispatch. It only runs when repository variable
`FMR_JUMPER_ENABLED=true`. Production environment secret `GH_JUMPER_SSH_KEY`
is a dedicated deploy key restricted to `android-boot-images-index`; it is not
the private Vault credential. The initial quota pause is enforced before any
production DB connection. Public referral publishing is currently disabled by the owner: variable
`FMR_JUMPER_ENABLED=false`, workflow disabled, and quota recovery does not
activate it. No production export is scheduled until explicitly requested.

Telegram request handling lives in Repo-Web. One-time registration runs outside
the Worker: provide `TG_BOT_TOKEN`, `TG_WEBHOOK_SECRET`, and optionally
`PUBLIC_SITE_URL` through the environment, then execute
`python -m fmr_extractor.telegram_setup`. First store the same webhook secret in
the production Worker. It registers only message/inline updates and commands;
it does not send chat messages. Enable inline mode with BotFather `/setinline`.
The Worker never calls the Telegram API while serving a webhook request.

Offline acceptance: `FMR_TEST_NO_SLEEP=1 python -m pytest -v` plus
`tests/test_sp06_jumper_sync.py`'s CLI test, which seeds its own SQLite database
and compares two independently generated file trees.

### TG01 independent public-channel scanner (single FindMyROM bot)

TG01 is a link-only catalogue: package name (`package_name`), download links,
message URL (`tg_post_url`), and observed sender ID/username are recorded. It
never downloads a package or submits it to the official-ROM image extractor.
`maintainer_handle` from a post's "By" line is distinct from its message sender.
Public previews may not expose the sender's numeric ID; `sender_id` stays null
in that case. Display names, channel names and maintainer handles never become
invented numeric sender IDs. Package names come from attachment/link metadata,
with the release title as fallback; no package request is made to discover them.

The project uses only `@FindMyROM_bot`; the SP06 query/referral frontend and
TG01 resource discovery belong to that same project. Discovery uses public
`https://t.me/s/` HTML, without another bot or a personal Telegram login. The
Bot Token is stored in Worker secrets, never in code or scanner checkpoints.
Public referral delivery is **disabled**, including webhook registration and
public GitHub index publishing, until the owner enables it after data preparation.

`python -m fmr_tg_scanner.cli --channel CHANNEL --default-codename DEVICE
--max-pages 20 --direction both --no-llm` uses independent local SQLite state
(`LOCAL_SQLITE_PATH`, default `tg01-state.sqlite`). It never implicitly connects
to production D1. No ROM/image file is downloaded. HTML parser and storage use
the standard library; HTTP uses httpx. Optional SP07 signing reuses project crypto.

Cursors commit atomically with packages on each page. Backfill starts below the
oldest completed floor; incremental catch-up persists its anchor/target/next page
when the page budget is exhausted. Overlaps and unchanged posts do not rewrite
packages. Identical text hashes reuse parsed AI metadata; AI attempt intent is
persisted before each call so interruption does not trigger another charge.
Noise/complete releases consume zero AI calls. AI is disabled by default in cloud
Actions; local optional fallback has a strict per-run call budget. Existing
packages and the latest checkpoint are retained on page errors.

Actions → **Telegram resources · incremental scan** (`telegram_scan.yml`):
enter a real public channel username, an optional device codename, and a finite
page budget. Run again with the same channel to continue. Live page statistics
appear in logs. Scans are serialized to prevent concurrent checkpoint races.
Checkpoint generations stay in the existing private Vault and are SHA-256 and
SQLite-integrity checked; a new generation uploads successfully before deleting
its predecessor. Normal exceptions/SIGTERM save the completed pages. An abrupt
VM kill can replay at most the current batch (default 20 pages), not its entire
history. The cloud checkpoint currently has a 128 MiB per-channel safety limit.
No public Actions artifact or public repository receives scanner records.

`--export-jsonl FILE` creates a deterministic snapshot and skips writing an
unchanged snapshot. `--sync-to-sp07` explicitly bridges into a prepared local
main-site schema using `SIGN_PRIVATE_KEY_HEX`; it preserves author post URLs,
existing public tokens, votes and author opt-outs. It does not activate frontend
SP07 tabs automatically. No production main-site sync runs by default.

Limitations: Telegram public previews may omit deleted/private/unavailable posts;
no scraper can guarantee recovery of those. `author_direct` describes observed
post provenance, not cryptographic verification of the developer's identity.
No production channel scan has been launched without a channel source list.

### Autonomous private annibale trial

`telegram_trial.yml` runs on the public Runner, while its data, detailed logs,
SQLite checkpoint, JSONL records and integrity reports go exclusively to the
private `TunJinshou050608/findmyrom-tg01-test` repository through a dedicated
SSH deploy key. Its `config.json` input is only `{"target_codenames":["annibale"]}`.
Manual channel seeds are rejected. Public Actions output shows phase names and
counts; private commit file lists are suppressed.

Source discovery automatically resolves model aliases from public metadata,
searches public indexes, validates public Telegram previews, and follows related
Android sources. Search/discovery results and errors have persistent caches and
cooldowns. No operator provides a channel, author or marketing model name.
A source being found or a process exiting successfully does not by itself pass
acceptance: directory references go under `candidates/`, while `packages/`
contains records with concrete package/build evidence. Main release titles take
precedence over companion recovery links; feature, support and installation links
are excluded. Unofficial builds are retained. Stock and mainstream official
releases are labelled for later list-based policy rather than silently discarded.

A bounded resident session scans and audits every ten minutes, committing each
cycle privately before waiting. Its persisted next-check timestamp survives the
55-minute session handoff; cron is a fallback. GitHub queue delays are possible.
To stop the experiment, set Runner variable `FMR_TG_TRIAL_ENABLED=false` and cancel
the active run; to resume, set it to `true` and dispatch this workflow.
`monitor_state.json` records the last audit result and next due time separately
from process heartbeats. Reports include skip reasons, cursors, duplicate and
integrity checks, source failures, missing sender IDs and unsupported previews.
This is a scripted audit, not a claim that an AI independently rewrites code every
ten minutes after this chat closes. Public referral and production D1 remain off.
A versioned metadata repair visits only existing records, once per rule revision;
it does not reset channel history, and failures receive a retry cooldown.
This experiment validates the annibale path, not exhaustive all-Telegram coverage
or production readiness across every manufacturer.
