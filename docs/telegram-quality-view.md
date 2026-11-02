# Post-collection Telegram quality view

This layer screens material that was already collected. The collector's
discovery, requests, parser, channel admission, pagination, priorities and
checkpoints are unchanged. In particular, search_policy.py and search_engine.py
are deliberately unchanged: their current acceptance decisions also influence
which channels the crawler discovers. Changing those decisions would confound
a retrieval-versus-filtering comparison.

quality_filter.screen_records(rows, target, catalogue) reconstructs a quality
view without trusting legacy accepted flags, titles, kinds or versions. Give it
the complete offline device alias catalogue; the small fallback dictionary only
guards familiar cross-device conflicts in standalone use. New manufacturers
require their source-backed aliases, not a new codename-specific rule.

The output separates accepted, candidate, rejected and duplicate records.
Candidates remain retained, and every duplicate keeps its exact message link and
canonical group's source links. Grouping uses explicitly announced SHA-256 or
substantive identical captions plus attachment identity. A generic AOSP caption,
same title/version or shared dependency URL cannot merge resources. Semantic
cross-language duplicate detection remains unfinished.

The input record adapter accepts the metadata audit's text, exact message URL,
observed attachment, publisher and profile evidence. It does not request files,
use legacy download-link availability, infer maker identity or claim verified
file sizes. Patch/tool candidates remain outside the main resource list instead
of being mislabeled Recovery. The output is a derived review schema, not a change
to production database enums or signed contracts.

Run python -m fmr_tg_scanner.quality_replay with --input (frozen JSON list or
records directory), --benchmark (private reference JSON), --catalogue (offline
alias JSON), and --output (private quality-view directory).

The fixed-input cloud replay reads the exact prior 2,008-message audit snapshot
and a separately frozen set of prior human-audit expectations. It asserts that
all pre-existing Telegram source and workflow files match the baseline, then
disables socket connections during screening. No live collection, paid API, D1
publication or mutation of resident accepted records is performed. Outputs and
per-record comparisons are committed only to the existing private test repo.

The reference cases are development regression controls, not independent
precision/recall labels. Correcting those errors and retaining positive controls
establishes limited improvement, not complete inventory accuracy. Future
integration should consume this derived view after collection; it must not feed
its stricter acceptance decisions into source discovery without a separate
retrieval-coverage experiment.
