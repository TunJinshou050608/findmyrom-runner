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


## Full saved-state closure audit and follow-up

Read-only inspection covered all 335 model records and every saved release,
including the provenance of every legacy conflict. A consistent metadata-only
export contained 54,567 release rows. No HTML cache, credentials, ROM files or
production D1 data was exported. Replay used a disposable local SQLite database
with HTTP requests forbidden.

| Replay check | Result |
| --- | ---: |
| Original release rows | 54,567 |
| Original archives and publication receipts preserved | 54,567 |
| Incorrect legacy identities corrected | 4,514 |
| Active identity mismatches after repair | 0 |
| Active candidates requiring conflict review | 13 |
| Re-running a completed repair | No changes |
| Network / production D1 requests | 0 / 0 |

The corrected copy contains 58,298 release slots, including 4,514 superseded
slots retaining evidence. Counts are not unique version counts. Conflict rows
remain blocked, with immutable original records available for investigation.

Follow-up changes:

- Repair old queued regions and known editions before normal collection. A
  persistent ordered cursor processes 500 rows per atomic batch within the job's
  existing time budget. Original evidence, source slots, target slots and counts
  commit together. Replays and interrupted runs are safe; no records are deleted.
- Distinguish Singapore packages and five proven editions: China Mobile hermes,
  China Telecom LTE cancro, China Telecom umi, Pro wt88047 and 48 MP picasso.
  Existing ordinary identities stay stable. Known editions get distinct internal
  keys and human-readable metadata; website labels and SEO show the real version
  plus edition. Exact archive names remain visible.
- Recognize userroot/userdebug/engineering builds, and same-checksum file aliases.
  Contradictory checksums or genuinely different unresolved files remain blocked.
- Due failed/conflicted pages revalidate stale HTML rather than reusing the same
  invalid cache for another 30 days. Healthy pages retain normal cache behavior.
- Publication checks trusted filename market/edition against staged descriptors.
  This is an additional guard even if a legacy candidate reaches the publisher.
- Real run 42778485051 collected and verified its snapshot successfully, but
  GitHub CreateArtifact failed with ECONNRESET and prevented continuation. Xiaomi
  now retries the same verified encrypted snapshot once. Pruning and continuation
  require a successful attempt. Both failures produce a failed run and preserve
  the local checkpoint; no endless retry or unbacked continuation is introduced.

At export time the model states were 100 ok, 226 pending, 5 retry and 4 blocked.
Pending work is unfinished history, not successful coverage. In particular,
emeraldr and shennongt had no working archive family (404); unused archive
families for hm2013023 and steppe had 503 responses. These source gaps are visible
and retained; no unproven cross-device alias is substituted.

The earlier production D1 rows have not been rewritten. Publisher pause variables
remain enabled. Public site reads and native local D1 tests do not constitute
production publication or firmware-download acceptance.

### Production rollout verification

- Runner commit `886ed1e`: 639 local tests passed; CI passed on Python 3.11,
  3.12 and 3.13. Web commit `b1d47c2`: 113 tests, typecheck, six read-cost
  tests and native Worker/D1/DO runtime tests passed locally and in Web CI.
- Xiaomi run `42797293876` loaded `886ed1e` and completed collection, fresh
  export verification, encrypted snapshot upload, three-copy retention and
  continuation dispatch successfully. The retry path was not needed in this run.
- Read-only inspection of the actual collector database confirmed identity
  revision 3 done, 4,514 repaired rows, and the expected current code SHA.
- Worker deployment `6fbcb6c58cb54da6879e4f87fb96ad43` succeeded, preserving
  bindings and secrets. The existing popsicle release URL returned HTTP 200,
  its expected title and canonical URL, and standalone package choices.
- All three D1 write pause variables remain true. No production D1 write or
  firmware/image download was performed during this verification.
