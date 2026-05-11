# Changelog

All notable changes to `crawler-log-analyser` are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## v1.4.0 — 2026-05-11

Classifier accuracy improvements driven by real-traffic measurement against
production seo log on speytech.com.

### Changed

- Empty user-agent (`-`) requests are now classified as `Empty-UA` rather
  than `UnknownBot`. The previous classification suggested unknown-bot
  activity when in practice empty-UA traffic is dominated by misconfigured
  scanners and exploit probes. Empty-UA requests on PHP-exploit paths
  continue to be aggregated in the Security Probes section, where they
  belong operationally.

  Operational evidence: the May 10 seo log showed 244 requests from a
  single Azure IP (`4.228.184.28`) with empty UA, exclusively on
  PHP-exploit paths. v1.3 reported these as `UnknownBot: 245`, which read
  as "many unknown crawlers" when the operational reality was "one
  scanner doing one shape of attack." v1.4 reports them as
  `Empty-UA: 245` and surfaces the single-IP concentration in the
  Security Probes summary.

### Added

- `TikTokSpider` classification (AI-family, social/recommendation surface).
- `News Explorer` and `Feedly` classification (RSS readers, non-AI).
  RSS readers represent direct subscriber-driven polling and are now
  counted as their own category rather than disappearing into
  `Human/Other`.
- `Amazonbot`, `PetalBot`, `SleepBot` classification (added during
  v1.3 → v1.4 development).

### Notes

- Bot-breakdown JSON consumers should update to recognise the new
  `Empty-UA`, `News Explorer`, `Feedly`, and `TikTokSpider` categories.
  The `UnknownBot` category remains in place for genuinely
  unidentifiable bot-shaped UAs that the empty-UA short-circuit
  doesn't catch.
- This is a minor version bump because the bot-breakdown report
  changes shape in a visible way. CLI flags, exit codes, and the
  combined-format parser are unchanged.

### Regression evidence

The May 10 seo log (1,186 lines) reclassifies cleanly between v1.3 and
v1.4 with no data loss:

| Category | v1.3 | v1.4 | Change |
|----------|------|------|--------|
| Googlebot | 426 | 426 | — |
| UnknownBot | 245 | 0 | removed |
| Empty-UA | 0 | 245 | new (renamed from UnknownBot) |
| News Explorer | 0 | 53 | newly classified |
| ChatGPT-User | 28 | 28 | — |
| PetalBot | 0 | 25 | newly classified |
| AhrefsBot | 14 | 14 | — |
| MJ12bot | 10 | 10 | — |
| Amazonbot | 0 | 8 | newly classified |
| Bingbot | 8 | 8 | — |
| OAI-SearchBot | 7 | 7 | — |
| YandexBot | 6 | 6 | — |
| Applebot | 5 | 5 | — |
| Feedly | 0 | 4 | newly classified |
| Bytespider | 3 | 3 | — |
| TikTokSpider | 0 | 3 | newly classified |
| DuckDuckBot | 2 | 2 | — |
| FacebookExternalHit | 2 | 2 | — |
| GPTBot | 2 | 2 | — |
| GenericBot | 3 | 0 | absorbed (was TikTokSpider) |
| SleepBot | 0 | 1 | newly classified |

Total crawler requests: 822 (v1.3 sum) → 822 (v1.4 sum). No data lost,
no double-counting. Human/Other count is approximately 364 in both
versions (residual from 1,186 total parsed lines minus crawler
requests).

## v1.3.0 — 2026-05-10

Added support for the nginx `seo_crawl` log format which includes `$host`,
`$scheme`, `$request_time`, and `$upstream_response_time`. Format auto-detected
from the first valid log line; explicit `--log-format` override available.

### Added

- `LOG_RE_SEO` regex for parsing the extended seo_crawl format.
- Per-file format auto-detection in `iter_entries`, with diagnostic info
  written to stderr on first parse of each input file.
- `--log-format {auto,combined,seo-crawl}` CLI flag with `auto` as default.
- `--latency-threshold-ms` CLI flag (default 1000ms, matches Google Search
  Console's "Average response time" warning threshold).
- `--version` CLI flag returning `crawler_log_analyser <semver>`.
- Module-level `__version__` constant for programmatic access.
- Concrete redirect attribution when host/scheme data is available. Old
  output `(likely HTTP→HTTPS or www→apex if $host/$scheme not logged)` is
  replaced with definitive form like `(1 HTTP→HTTPS, 1 www→apex)`.
- New "Response Latency" report section showing overall median, p75, p95,
  p99, per-bot latency medians, and the slowest 10 individual requests.
  Honest empty-state message when all requests are sub-millisecond.
- New health score deduction (-5 points) when Googlebot p95 latency
  exceeds the configured threshold. Gated on seo_crawl data availability;
  silently skipped on combined-format inputs.
- New JSON keys when seo_crawl data is present: `response_latency` and
  `redirect_attribution`. Keys are absent (not null) when data is unavailable.

### Changed

- `parse_line` now takes an explicit `log_format` argument.
- `LogEntry` extended with optional `host`, `scheme`, `request_time`,
  `upstream_time` fields. All default to `None` for combined-format entries
  to preserve backwards compatibility.

### Preserved

- Behaviour for standard combined-format logs is unchanged. v1.2 stdout
  is byte-identical to v1.3 stdout for the same combined-format input.
- All existing CLI flags behave identically.
- All existing JSON keys preserved with identical structure.

### Known issues (for future investigation)

- `--strict` exit codes do not distinguish "no real search-crawler activity
  in the analysis window" from "search-crawler activity with problems".
  Both paths can return exit 1 because `compute_search_crawler_score`
  returns `(0, ...)` when the cohort is empty, and the strict policy's
  `sc_score < 95` rule then trips.

  *Reproduction:* pointing the analyser at a fresh seo-format log during
  a period with no Googlebot/Bingbot/Yandex/etc traffic (only AI crawlers,
  human traffic, or quiet periods) yields `--strict` exit 1, even though
  no errors were observed.

  This is pre-existing v1.2 behaviour, not introduced by v1.3, but the new
  seo-format log split makes it more likely to surface in practice (the
  seo log is a smaller, more recent slice than the rolling combined log).

  *Suggested resolution for v1.3.1 or v1.4:* `compute_search_crawler_score`
  should return a sentinel indicating "no cohort activity" distinct from
  "cohort active but scored 0", and `--strict` should treat the no-activity
  case as exit 0 with a stderr note rather than exit 1.

- Security probe pattern matches `/api/contact.php` even though it is the
  legitimate allowlisted endpoint on the developer's deployment. The probe
  detector flags any `*.php` request, which made sense when v1.0 was built
  for a static site with no PHP. Sites with explicit PHP allowlists should
  be able to declare them. Targeted for v1.4.

### Recommended nginx log_format for adopters

```nginx
log_format seo_crawl '$remote_addr $host $scheme - [$time_local] '
                     '"$request" $status $body_bytes_sent '
                     '"$http_referer" "$http_user_agent" '
                     '$request_time $upstream_response_time';

access_log /var/log/nginx/<site>.access.log combined;
access_log /var/log/nginx/<site>.seo.log    seo_crawl;
```

Both logs populated in parallel; the analyser detects format per-file.

---

## v1.2.0 — 2026-05-10 (earlier)

Refined probe handling, search-crawler scoring, and CI exit codes after
the v1.1 review identified false-positive scenarios in deploy-window
detection and a need for broader cohort visibility.

### Added

- Framework fingerprint probe detection separate from security probes.
  Bot probes for `/_next/`, `/_nuxt/`, `webpack-stats.json`, framework
  manifest paths etc are now classified honestly as framework probes
  rather than miscategorised as security probes or content 404 bursts.
- "Framework Fingerprint Probes" report section, distinct from security
  probe summary.
- "Bad Request Noise (HTTP 400)" section summarising malformed-request
  traffic that gets rejected before nginx routing. Excluded from SEO
  conclusions.
- "Search Crawler Health Score (broader)" — a second 0–100 score covering
  the real-search-crawler cohort: Googlebot, Googlebot-Image, Bingbot,
  DuckDuckBot, YandexBot, BaiduSpider. Excludes UnknownBot, GenericBot,
  AI crawlers, social-preview bots. Uses separate scoring rules: -20 for
  any 5xx, -25 for any robots.txt failure, -10 for >2% 404 rate.
- `--strict` CLI flag for cron / CI use. Exit 0 = clean, 1 = HIGH/WARNING,
  2 = CRITICAL. Tightened to also flag non-Google search-crawler 404s
  and broader-cohort score below Excellent.
- `webp` and `avif` extensions added to internal pattern lists.

### Changed

- Framework probes now excluded from the content-404 burst detector to
  prevent Next/Nuxt/webpack manifest probing from false-positive triggering
  deploy-window alerts.
- Deploy-window anomaly severity is now tiered by bot family, matching
  the spec §6.4 rule: CRITICAL for Googlebot robots.txt failures,
  WARNING for AI crawler robots.txt failures, INFO for everything else.
- The "Recommended Nginx Log Format" section is now adaptive — only
  appears when the input is a combined-format log without host/scheme
  data, because that's the only context where the recommendation
  applies.

### Fixed

- Filtered-mode AI Crawler Report no longer says "No AI crawler activity
  recorded" misleadingly when `--bot googlebot` is set. Now reads
  "Skipped because --bot googlebot filter is active."
- Root `/` redirect annotation is no longer "(likely canonical)". Now
  reads "(likely HTTP→HTTPS or www→apex if $host/$scheme not logged)",
  acknowledging that the analyser can't distinguish without scheme/host
  fields.

### Known issues at v1.2

- The Googlebot health-score deduction for "redirect rate >25%" can
  trip on small samples where most requests happen to be legitimate
  trailing-slash or HTTP→HTTPS redirects. The redirect attribution
  feature added in v1.3 addresses this by replacing speculation with
  evidence.

---

## v1.1.0 — 2026-05-10 (earlier)

Tolerance and observability improvements after v1.0 was tested against
production logs and surfaced parser limitations and missing operational
signals.

### Added

- Two-stage tolerant log parser. The skeleton regex handles unusual
  request shapes (HTTP/2.0, HEAD, OPTIONS *, IPv6 addresses, `"-"` empty
  requests, `""` zero-length requests, binary noise like `/\x90\x90`)
  without rejecting otherwise-valid lines.
- Malformed-line tracking with sample collection. The first 10 unparseable
  lines are captured for inspection; if malformed rate exceeds 5% the
  summary line emits a `[WARNING]` directing the user to the dedicated
  Malformed Lines section.
- Security probe suppression. Exploit-probe traffic (PHP, WordPress,
  `.env`, `phpmyadmin`, etc) is summarised in its own section with top
  paths and source IPs, kept out of the main 404 report by default.
- `--show-security-probes` to include probe traffic in the 404 list.
- `--seo-only` to suppress the probe summary entirely for the cleanest
  possible SEO-focused output.
- "Deploy-Window Anomaly Detection" report section. Detects three
  symptoms of the failure mode the tool was built to catch: robots.txt
  404/5xx for any crawler, content-404 bursts (≥5 within 2 minutes),
  and Googlebot 404 followed by >2h crawl gap.
- "Recommended Nginx Log Format" section in the report output, suggesting
  the dedicated `seo_crawl` log_format with `$host` and `$scheme` for
  better redirect attribution. (This recommendation became the v1.3
  parser support.)

### Changed

- Summary line now reports malformed percentage explicitly:
  `Lines parsed: N (malformed skipped: M / X.X%, out of range: K)`.
- Root `/` redirect annotation reworded to acknowledge the diagnostic
  limit imposed by the standard combined log format.

### Fixed

- v1.0 rejected ~41% of real production log lines because the original
  regex required strict `"GET /path HTTP/1.1"` format. v1.1 accepts
  any uppercase method, optional protocol, and arbitrary path content
  (including binary garbage that legitimately appears in probe traffic).

---

## v1.0.0 — 2026-05-10 (earlier)

Initial implementation. Single-file Python script with no external
dependencies, designed for self-hosted static sites running nginx.

### Capabilities

- Parses nginx combined-format access logs, plain or gzipped.
- Classifies search and AI crawlers from user-agent strings:
  Googlebot, Googlebot-Image, Bingbot, Applebot, OAI-SearchBot,
  ChatGPT-User, GPTBot, ClaudeBot, Claude-SearchBot, Claude-User,
  PerplexityBot, MistralBot, YandexBot, Bytespider, AhrefsBot,
  SemrushBot, MJ12bot, FacebookExternalHit, DuckDuckBot, BaiduSpider,
  LinkedInBot, TwitterBot.
- URL classification into operationally meaningful categories
  (homepage, robots, sitemap, rss, llms, insights article,
  AI architecture article, open-source page, product page, contact,
  static asset, API, utility/demo).
- Tracks special SEO files: `/robots.txt`, `/sitemap-index.xml`,
  `/sitemap-0.xml`, `/sitemap.xml`, `/rss.xml`, `/llms.txt`,
  `/llms-full.txt`.
- Reports: executive summary, per-bot status code breakdown,
  Googlebot crawl timeline by hour, robots.txt health, special SEO
  file access, top Googlebot URLs, crawler-visible 404s, redirect
  analysis, section-level Googlebot crawl breakdown, AI crawler
  report, Googlebot crawl health score (0–100 with verdict bands).
- Output formats: text (default), markdown, JSON.
- CLI flags: `--bot`, `--from`, `--to`, `--format`, `--output`,
  `--top`, `--verify-googlebot`, `--include-non-bots`.
- Optional Googlebot DNS verification (reverse + forward lookup
  against `*.googlebot.com` and `*.google.com`).

### Constraints

- Python 3.8+ standard library only.
- Single-file executable.
- No database, no telemetry, no external service dependencies.
- Cron-compatible.
- Deterministic output ordering.

### Origin

Built to verify recovery from a specific operational failure mode:
deploy-window crawler 404s on a static Astro site after an atomic
deploy fix. The tool's design was driven by observed production
behaviour rather than feature ideation.

---

## Versioning posture

Per the project strategy document:
**v1.x prioritises CLI and report-format stability over feature expansion.
Breaking changes to either require a major version bump.**

A user upgrading within the v1.x line should never see existing flags
behave differently or existing output sections change content. New
features appear as additive sections, additive JSON keys, or new flags
defaulting to backwards-compatible behaviour.

Going forward:
- v1.x.y patch releases for bug fixes and clarifications.
- v1.(x+1).0 minor releases for additive features.
- v2.0.0 only for genuinely breaking changes.
