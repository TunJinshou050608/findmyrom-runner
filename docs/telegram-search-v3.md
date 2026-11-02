# Autonomous Telegram metadata search

The private trial now has a separate adaptive search engine. The resident process reads its device targets and model aliases automatically from the existing local catalogue snapshots; it requires no manually entered device or channel list. Its products, rejected candidates, SQLite checkpoints and detailed logs belong in the private `findmyrom-tg01-test` repository. It never downloads firmware or writes D1.

## Discovery and evidence

The main retrieval path is public Telegram channel keyword search (`/s/channel?q=...`), exact message embeds, linked channels and native forwards. Lyzem supplies candidate floors; its negative control is checked daily, and unrelated results get lower scheduling priority. Public Android project READMEs supply an optional channel-discovery fallback. General web search engines are not part of this engine.

Verified Android aggregators are shared across tracked devices. A source discovered for one model is queried for the others; an explicit different tracked model in a fetched post is routed to its own evidence checks. Dedicated-device sources retain their narrower scope. Unresolved devices also search ROM/recovery/kernel project metadata, public maintainer bios and release notes, rather than depending on five `android` README results. Bare Telegram links and explicitly labelled Telegram handles are recognized. Search spellings include deterministic English/Chinese variants of verified catalogue names.

The `focus` lab mode reads the resident source metadata without changing its database, and writes a separate experiment directory. It imports no product judgments or pagination cursors. Its diagnosis and per-device funnel distinguish discovery, message reading and rejection reasons; explicit zero counts remain visible. Private Git publication configures commit identity for rebases and retries concurrent pushes.

Device aliases come from the public Xiaomi device dictionary. Alias-derived channel names are guesses used for discovery only. They do not establish a package's device. The actual post, attachment path, or an unambiguous channel profile must supply that evidence. Multi-device profiles and GSI donor-device mentions must not imply compatibility.

Profile model matching resolves the longest alias span first: `Xiaomi Pad 6 Pro` is not two mentions of `Pad 6` and `Pad 6 Pro`, and `Xiaomi 13 Ultra` is not a second mention of `Xiaomi 13`. Disjoint mentions still identify a mixed profile. A source profile never overrides an explicitly different model in the post. Sources previously treated as ambiguous are rescheduled once after this resolution change. Unresolved-device name discovery interleaves model spellings with singular/plural Update variants, and priority upgrades revive old low-priority attempts once without repeatedly erasing cooldowns.

The deliverable is the **exact Telegram message jump link**, `https://t.me/channel/message_id`. An eligible release announcement does not need an external file URL or an attachment. Results expose `target_post_url`, device evidence, post title, resource type, text hash and observed provenance. File URLs are not collected as a product or checked for availability. Compatibility fields retained internally use an empty download-link list. Missing author identity and numeric account IDs remain unknown.

Deduplication uses device plus the actual message identity, not a file URL. A multi-device announcement may link to the same message under each explicitly listed device. Existing records migrate to message delivery, candidates previously rejected for missing file links are requeued, and cached public pages are rechecked in bounded batches without extra requests. A fresh re-read checks the exact floor and whether its content still establishes the target device and a resource announcement; it does not test a downloadable file or establish authorship.

Stock OTA filenames remain stock when a mirror hostname changes. GSI donor matches, engineering firmware and warning posts are excluded. Mainstream official ROMs are excluded; unclassified official ROM scope and unproved mainstream release status stay in candidates. Official recovery/kernel resources are considered separately. Explicit custom ports and community/unofficial builds remain eligible. Companion recovery/kernel installation instructions do not determine the main release's type.

## Autonomous progress

The SQLite frontier alternates devices and reserves work for both new discovery and continued pagination. Query cursors are separate from the legacy channel cursors. An incremental head watermark advances only after the old watermark is reached, including when a gap spans runs. Query pagination failures enqueue a bounded historical fallback. Repeated/empty pages never establish complete Telegram coverage.

Resident retrieval is adaptive: a profile independently identifying exactly one device uses historical pages plus incremental head checks; aggregator or ambiguous channels use keyword queries plus head checks. The finite ordering experiment found that neither ordering dominated on every device. Fixed-order lab arms remain available for comparisons.

Requests share a host limiter and persistent compressed cache. Each batch has request/time limits, response-size and MIME checks, retry limits and cooldowns. The frontier has a per-device ceiling reported explicitly. Exact-message failures receive a longer cooldown than transient transport failures. No CAPTCHA bypass, personal account login or paid search is attempted. Optional DeepSeek classification covers unresolved rule decisions and evidence-backed release tagging, with a default ceiling of 50 calls per batch. Both the lab and resident workflow use the `DEEPSEEK_API_KEY` secret. Missing/invalid configuration, provider authentication failures, real call attempts, successful decisions, cache hits, invalid decisions and budget skips are reported without exposing credentials. A 401/403 stops further model calls in that batch; rules continue normally.

`search_watch` uses the existing private checkpoint/handoff loop: a default 100-request/360-second batch every ten minutes, thirty-second heartbeats, and a bounded resident session. `tg_search_config.json` controls target codenames and budgets. SQLite commits each observation; checkpoints and metadata logs are committed privately each cycle. Search failure is reported separately from inventory size and new-product counts.

Quiet sources back off automatically (head checks up to one hour, keyword refreshes up to six hours). A newly created resident state can import channels already found by the private lab, retaining discovery provenance. It imports neither accepted products nor pagination watermarks, and rechecks sources under the current policy. Cold comparison runs never use these sources.

## Running and evaluation

Use the `telegram_search_lab.yml` manual workflow with `batch` or `compare`. Four-device defaults are `annibale,ishtar,liuqin,alioth`. Each cold arm receives 180 requests and 480 seconds. A separate 90-request continuation restores the query-first frontier without adding channels. The comparison changes search ordering in the new engine; it is **not** a frozen legacy baseline. Its continuation is excluded from the equal-budget comparison.

The lab re-reads up to 24 floors in a separate transport cache and reports exact message identity, device and announcement checks. It does not require or test external file links. These are evidence checks, **not independent precision or recall**. Targets such as 90% accessible benchmark recall and 50% lower request cost must not be claimed until an independent multi-device benchmark establishes them. A device with no result in a bounded batch remains unresolved, rather than declared absent from Telegram.

The resident workflow retains its enable flag. The D1 pause flag is independent and remains enabled during these private tests. No production TG publication bridge is enabled by this change.

## Capability boundaries

Public pages cannot cover private, deleted, login-only or otherwise unexposed messages. Telegram's global `channels.searchPosts` is user-only; no account credentials or Stars budget are configured. Bot-to-bot private communication requires support on both endpoints and is not assumed merely because another search bot exists. External index candidates are useful discovery inputs, not a claim of global Telegram coverage.

References: [Telegram global search](https://core.telegram.org/api/search), [searchPosts](https://core.telegram.org/method/channels.searchPosts), [bot-to-bot conditions](https://core.telegram.org/api/bots/bot-to-bot), [TGStat API](https://api.tgstat.ru/docs/ru/posts/search.html), [Media Search Bot](https://github.com/Mahesh0253/Media-Search-bot).

## Model decisions and identity safeguards

Model acceptance requires an actual JSON boolean, a finite confidence from 0.90 to 1.00, an allowed classification consistent with acceptance, and no contradictory release/stock flags. Invalid or low-confidence results keep the rule decision. Cached decisions are scoped to the exact text, target device, rule reason, model, API origin and prompt version. Model decisions do not add projects to a global acceptance whitelist. Existing learned project lists are ignored.

Message deduplication uses device, channel and message ID. Different posts remain separate even if their project/version matches, preserving different maintainers and rebuilds. This deliberately retains cross-posts until stronger build identity evidence exists. Existing stored fingerprints migrate automatically without deleting observations.

## Local container execution and automatic enrollment

All three TG retrieval workflows use `[self-hosted, findmyrom-metadata]` without an Ubuntu-hosted fallback. GitHub still triggers jobs and stores private checkpoints. The resident and lab jobs read `FMR_CATALOGUE_STATE_DIR` from the repository variable, defaulting to `/var/lib/findmyrom/catalogue-v3`, the directory created by `ops/setup_metadata_runner.sh`. Access is read-only; the TG task does not query D1 or scan firmware release rows to discover models. Existing catalogue model records provide site device identities, marketing aliases, upstream codenames and hardware model numbers. Excluded/non-Android model records are skipped. Published `devices` tables serve as a fallback only for snapshots without a model frontier.

Every resident cycle reloads the catalogue and selects the next eight devices by a persisted cursor (`target_batch_size`, range 1–100). Existing per-device crawl queues/cursors stay intact across rotations. New models are included automatically; old `targets` arrays in `tg_search_config.json` no longer constrain enrollment. An empty/missing catalogue fails the cycle visibly rather than silently reverting to four sample devices. The lab workflow also selects devices automatically from the same catalogue and has no cloud-runner or manual-target input. Optional CLI target overrides remain for isolated developer experiments.

## Release taxonomy and publication boundary

Each search-ledger payload stores a versioned `taxonomy` object. Every classification has its evidence/source in `taxonomy.evidence`; unknown values remain explicit. Legacy scanner records store the same object in `taxonomy_json` (automatic additive schema migration). Cached public pages are replayed in bounded batches when the taxonomy version changes. Records without current tags never enter the publication-ready export.

| User-facing category | Stored fields / values |
| --- | --- |
| 收录边界 | `access`: `free`, `paid`, `unknown` |
| 资源类型 | `resource_type`: `rom`, `recovery`, `kernel`, `other_tool` |
| 适配设备 | `devices.device_id`, `codename`, `applicable_models`, `excluded_models`, `compatibility_conditions` |
| ROM 分类 | `rom_family`: `aosp_like`, `manufacturer_modified`, `manufacturer_port`, `other`, `pending`; non-ROM uses null |
| 系统基础 | `system_base`: `AOSP`, `HyperOS`, `MIUI`, `One UI`, `OxygenOS`, `ColorOS`, `OriginOS`, `unknown` |
| 项目身份 | `project.id`, `name`, `aliases` (local canonical registry; no claim of external registry registration) |
| 维护分支 | `maintenance.maintainer_id`, `branch_id`, `branch_name`, `identity_verified` |
| 项目认可 | `project_recognition`: `official`, `unofficial`, `unknown`; source is an author claim |
| 版本 | `versions.android`, `project_version`, `build_id`, `build_date` |
| 构建变体 | `build_variants`: list of `vanilla`, `gapps`, `other`, `unknown`; one post may announce multiple variants |
| 包形式 | `package_form`: `device_specific`, `gsi`, `unknown` |
| 发布阶段 | `release_stage`: `stable`, `beta`, `alpha`, `unknown`; source is an author claim |

Rules identify explicit statements and known project identities first. The optional, bounded model call supplies evidence-backed family/base/access suggestions for qualified releases and assists ambiguous release decisions. Model output never creates supported-device mappings, real account IDs or verified endorsements. Missing values are not inferred from public links, channel ownership or message timestamps. Build dates with ambiguous day/month order stay null. A `tg-handle:` maintainer ID identifies an observed credited handle, not a verified Telegram numeric account; absent author evidence leaves maintainer and branch IDs null. Branch IDs include project, credited maintainer, device and any declared branch, so different maintainers do not collapse.

`products.json` remains the accepted inventory, including paid/unknown access for review. `publishable_products.json` contains only records with `taxonomy.publication.eligible == true`. Under the user-authorized TG policy, releases default to free unless explicit payment gates apply. EliteDevelopment/EliteROM sources and their identifiable reposts are excluded from the free publication list. Optional donations do not make a release paid. Policy defaults are recorded as `user_policy`, separately from explicit author claims. Explicit excluded models and GSI compatibility requiring review also block publication. The legacy signed SP07 export applies the same access/current-taxonomy gate. These changes do not enable production publication.

Access policy revision: `tg-default-free-except-elite-v1` (`release-taxonomy-v2`). Existing v1/v2 classified ledger records are retagged locally during export without re-fetching messages or changing device/release judgments. Untagged records remain pending classification. Elite matching uses source channel identities, observed EliteDevelopment links/handles and EliteROM release branding, rather than an unrelated use of the word elite. Explicit paid gates remain paid. A configuration deployment hands the local resident over to the new revision; scheduled checks do not cancel active sessions.
