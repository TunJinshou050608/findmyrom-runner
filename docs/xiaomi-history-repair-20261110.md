# Xiaomi history collection repair — 2026-11-10

## Evidence and scope

A read-only inspection of the active task-side checkpoint found 335 models:
52 ok, 282 retry and 1 failed, with 51,027 resolved release records. All 3,471
ValueError detail failures across 216 models contained unescaped path spaces.
Other cohorts overlapped: 126 models had NoFullROMRecord, 56 had 2,351 candidate
conflicts, four had failed detail requests, and two had no available archive.
The archive for annibale also had a transient 503. Counts are not additive.

Real pages established that missing MD5 was rejecting full official links for
liuqin OS1.0.9.0.UMYCNXM and libra 7.6.15. Whyred 8.10.11 contains China and
Global ZIP/TGZ packages under the same weekly version; defaulting both to CN
created false identity conflicts. Conflicts must still be checked individually.

## Changes

- Encode archive-discovered URL paths before the existing strict URL/security
  gate. Keep the HTTPS, origin, text-only and response-size checks unchanged.
- Accept explicitly missing checksums as null; never manufacture a hash. Reject
  malformed nonempty checksum values and retain partial-page failure evidence.
  Empty field parsing no longer consumes the next label as the field value.
- Use stable-version region suffixes first, then explicit Global archive naming
  for weekly builds whose version string has no market suffix.
- Classify mirror aliases and normal/debug alternatives as retained nonblocking
  observations. Different archives or contradictory checksums stay quarantined;
  candidates remain in source evidence and cannot receive publication receipts.
- Store validated parsed-page results in local SQLite checkpoint keys
  `v2:xiaomi:history:<device>:<URL digest>`. This is source evidence, not another
  HTML cache: it contains only small parsed rows and provenance, no firmware
  bytes. It is included in existing encrypted exports and survives HTML eviction.
- Each model pass checks at most 32 due detail pages, prioritizing known gaps.
  Unfinished pages remain pending without consuming source-failure attempts.
  Successful pages are reused for 30 days; conflicted pages are rechecked after
  one day. Missing pages and malformed records back off for one day; temporary
  failures back off at least 15 minutes. Prior parsed rows survive failed refreshes.
- A one-time parser revision requeues only failed/retry/blocked model checkpoints,
  retaining resolved releases, published state, healthy models and active cursors.
  Existing older successful pages are upgraded gradually within the normal budget;
  no collection history is reset or full vendor run dispatched.
- Publishing refuses a Xiaomi identity that already points to a different archive
  or a contradictory known checksum. Old tokens, URLs and signatures stay intact.
  Historical wrong-region D1 records require a separate bounded review; this
  repair does not silently relabel or overwrite them when publishing resumes.

## Validation

628 offline tests passed, covering real SQLite persistence, process/deadline
resume, URL gates, missing/invalid checksums, weekly regions, bounded pending
pages, targeted retries, conflict quarantine and immutable existing identities.

Four real upstream HTML detail pages (135,810 bytes total) were fetched and then
processed through the production URL gate, parser and task-side checkpoints with
bounded archive fixtures. No full history crawl was performed.

| Device | Parsed records | Regions | Missing checksums | Repeated detail fetches |
|---|---:|---|---:|---:|
| air | 1 | IN | 0 | 0 |
| liuqin | 1 | CN | 1 | 0 |
| libra | 2 | CN | 2 | 0 |
| whyred | 4 | CN, Global | 0 | 0 |

The correction used zero production D1 requests/writes and downloaded no ROM or
partition images. Existing publisher pause switches and workflows were unchanged.
Source 404/503 responses and genuine unresolved file conflicts remain visible;
they are not converted into successful coverage claims.
