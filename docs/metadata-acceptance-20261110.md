# Bounded firmware metadata acceptance — 2026-11-10

## Limits and method

No full collector/publisher dispatch, firmware transfer, historical rebuild or
production failure injection. The test limit was 16 firmware samples,
10,000 production D1 rows read and 200 rows written. Costly cases ran locally.

Fourteen existing records were inspected across all eleven active vendors.
Twelve records from nine vendors had sufficient raw descriptors or official
source evidence for a real atomic metadata batch. OPPO and Nothing samples
lacked recoverable raw versions and their Atlas pages returned HTTP 403 from
this environment; they were not rewritten using guessed strings.

Actual `metadata_publish.publish` generated 20 statements for those 12 records
against a snapshot of their existing values. The same statements were submitted
in one production D1 atomic batch. Twelve metadata rows and eight stock
Android/date/size updates persisted. Cloudflare charged 40 written rows including
indexes. All stored hashes and selected stock fields matched the expected output.
All fourteen records' immutable fields matched their pre-test fingerprints,
including tokens, identity, source URLs, signatures and extraction state.
Replaying the actual publisher against the production readback generated zero
write statements for all twelve records.

Direct diagnostic, schema, sample and readback SQL used 830 rows read and
40 rows written. This excludes ordinary public-page reads, which are tracked
separately through the provider's daily analytics.

## Coverage

| Area | Evidence |
|---|---|
| Active vendor adapters, checkpoints and existing workflows | Runner suite: 610 passed |
| Metadata changes, failure rollback, exact model identity, local acknowledgements and retries | Real SQLite/crypto regression tests; replay against production readback |
| Quota stop and no follow-up/settlement calls after provider rejection | Deterministic provider-error tests, no production error injection |
| Web labels, grouping, gateway and absent-metadata budget handling | Web suite: 112 passed; TypeScript check passed |
| Indexed reads and coverage counter cost | 6 tests passed with a local 70,000-row benchmark |
| Native D1/DO contracts and SEO hydration | workerd tests passed, including Samsung and carrier-first Google captions |
| Production presentation and signatures | 17 HTTP 200 HTML requests: 3 release pages, 1 device page, 12 signature verification pages and 1 search page |
| Stable URLs and SEO | Canonical links, titles, descriptions, carrier labels and Xiaomi combined packages checked |

Google's official factory page confirmed the real caption
`7.1.0 (Verizon, NDE63X, Nov 2016)` and its archive filename. Samsung's official
index and English notice page were fetched and processed by the actual adapter;
A716U1UEU9GWF7 attached Android 13, and repeated A716U1UEU6DVA1 notices attached
Android 11 with a deliberately unresolved release date.

## Findings corrected

1. Google carrier-first captions previously swapped build/carrier identities.
   Official filename parsing now supplies the build. Exact legacy file matching
   avoids new duplicate rows; carrier labels are hydrated independently of URLs.
2. Numeric Nothing OS versions could be mislabelled as Android. The vendor-aware
   parser separates them and preserves independently known Android versions.
3. `realme UI` was missing from the OS-name recognizer.
4. One Samsung repeated AP with conflicting dates previously rejected the whole
   optional notice page. Only conflicting fields are left unresolved now.
5. Sony public provenance now removes service query parameters.

## Remaining limits

Live Atlas fetches could not be validated because of HTTP 403. Adapter parsing,
retry/checkpoint behavior and all affected vendor contracts passed local tests,
but this is not proof of live source access from the production runner.
OPPO/Nothing production metadata updates are therefore unverified in this run.
No encrypted archive decryption, Sony ZIP assembly, partition extraction, actual
firmware delivery or full scheduled run was executed. Existing download
signatures and preparation interfaces were checked without transferring payloads.
Legacy Google identity strings remain immutable; this small repair does not
backfill the site's version-search index for every corrected historical label.
