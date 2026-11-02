# Autonomous Telegram metadata search

The private trial now has a separate adaptive search engine. It accepts device codenames, not channel lists. Its products, rejected candidates, SQLite checkpoints and detailed logs belong in the private `findmyrom-tg01-test` repository. It never downloads firmware or writes D1.

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

Requests share a host limiter and persistent compressed cache. Each batch has request/time limits, response-size and MIME checks, retry limits and cooldowns. The frontier has a per-device ceiling reported explicitly. Exact-message failures receive a longer cooldown than transient transport failures. No CAPTCHA bypass, personal account login, paid search or LLM assistance is attempted.

`search_watch` uses the existing private checkpoint/handoff loop: a default 100-request/360-second batch every ten minutes, thirty-second heartbeats, and a bounded resident session. `tg_search_config.json` controls target codenames and budgets. SQLite commits each observation; checkpoints and metadata logs are committed privately each cycle. Search failure is reported separately from inventory size and new-product counts.

Quiet sources back off automatically (head checks up to one hour, keyword refreshes up to six hours). A newly created resident state can import channels already found by the private lab, retaining discovery provenance. It imports neither accepted products nor pagination watermarks, and rechecks sources under the current policy. Cold comparison runs never use these sources.

## Running and evaluation

Use the `telegram_search_lab.yml` manual workflow with `batch` or `compare`. Four-device defaults are `annibale,ishtar,liuqin,alioth`. Each cold arm receives 180 requests and 480 seconds. A separate 90-request continuation restores the query-first frontier without adding channels. The comparison changes search ordering in the new engine; it is **not** a frozen legacy baseline. Its continuation is excluded from the equal-budget comparison.

The lab re-reads up to 24 floors in a separate transport cache and reports exact message identity, device and announcement checks. It does not require or test external file links. These are evidence checks, **not independent precision or recall**. Targets such as 90% accessible benchmark recall and 50% lower request cost must not be claimed until an independent multi-device benchmark establishes them. A device with no result in a bounded batch remains unresolved, rather than declared absent from Telegram.

The resident workflow retains its enable flag. The D1 pause flag is independent and remains enabled during these private tests. No production TG publication bridge is enabled by this change.

## Capability boundaries

Public pages cannot cover private, deleted, login-only or otherwise unexposed messages. Telegram's global `channels.searchPosts` is user-only; no account credentials or Stars budget are configured. Bot-to-bot private communication requires support on both endpoints and is not assumed merely because another search bot exists. External index candidates are useful discovery inputs, not a claim of global Telegram coverage.

References: [Telegram global search](https://core.telegram.org/api/search), [searchPosts](https://core.telegram.org/method/channels.searchPosts), [bot-to-bot conditions](https://core.telegram.org/api/bots/bot-to-bot), [TGStat API](https://api.tgstat.ru/docs/ru/posts/search.html), [Media Search Bot](https://github.com/Mahesh0253/Media-Search-bot).
