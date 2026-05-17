# crawler-log-analyser

Operational SEO observability for self-hosted static sites running nginx.

## Why This Exists

Most crawler-visible SEO failures happen during deployment windows, not during steady-state operation.

A site can pass uptime checks, synthetic crawls, and Lighthouse audits while Googlebot receives transient 404s, broken `robots.txt`, redirect loops, or incomplete sitemap states during cache invalidation or atomic deploy transitions. Conventional SEO tooling usually misses these conditions because it observes after systems stabilise.

`crawler-log-analyser` exists to close that operational visibility gap using live nginx access logs rather than synthetic scans.

📖 Read the design philosophy: [Self-Hosted Static Sites Need Operational SEO Observability](https://speytech.com/insights/operational-seo-observability/)

## What It Does

* parses nginx access logs, including rotated `.gz` logs
* supports both the standard combined format and the extended seo_crawl format (with `$host`, `$scheme`, `$request_time`, `$upstream_response_time`)
* classifies search and AI crawlers from user-agent traffic
* detects crawler-visible deploy-window anomalies (robots.txt failures, content-404 bursts, post-404 crawl gaps)
* scores Googlebot crawl health and a broader search-crawler cohort from observed responses
* surfaces `robots.txt`, sitemap, RSS, `llms.txt`, and `llms-full.txt` access patterns
* attributes redirects concretely (HTTP→HTTPS vs www→apex vs trailing-slash vs canonical-loop) when seo_crawl data is available
* classifies AI crawler activity against a configurable discovery window (too_early / in_window / overdue / active) on new sites
* detects browser-UA spoofing on sitemap-class paths (`SuspectedUASpoof`)
* detects rotational identity claims — one IP cycling through multiple named-bot UAs on security-sensitive paths (`SuspectedBotIdentityRotation`)
* suppresses security-probe noise (PHP/WordPress/`.env` scans) so it doesn't pollute the SEO 404 report
* reads a TOML config file for standing operator preferences (ignored source IPs, per-site launch dates, AI crawler discovery window)
* exits non-zero on critical findings for cron and CI use
* emits text, markdown, or JSON reports

## Example Output

```text
## Deploy-Window Anomaly Detection

Symptoms detected:
  [CRITICAL] Googlebot received 2 robots.txt failures
  [WARNING] OAI-SearchBot received 1 robots.txt failure

## Redirect Analysis

Googlebot: 3 redirects
    2 × /  (1 HTTP→HTTPS, 1 www→apex)
    1 × /old-page  (1 trailing-slash)

## Rotational Bot-Identity Detection

7 requests across 1 unique IPs exhibited rotational bot-identity behaviour:
a single IP claiming multiple distinct bot identities within a 5-minute window.

Top rotating IPs:
     7 requests from 5.255.104.83  (5 identities: BaiduSpider, Bingbot,
                                    ClaudeBot, PerplexityBot, YandexBot)

Top probed paths during rotation:
     1 × /api/env       1 × /api/config       1 × /actuator/env
     1 × /secrets.json  1 × /appsettings.json

## Response Latency

Overall:
  median: 0.002s    p75: 0.004s    p95: 0.087s    p99: 0.149s

## Suppressed Security Probe Noise

Total probe requests: 368

Top probed paths:
    14 × /wp-login.php
     8 × /.env
     2 × /config.phpinfo

Use --show-security-probes to include these in the 404 report.

## Googlebot Crawl Health Score

Score: 70/100
Verdict: Needs attention
```

## Quick Start

```bash
curl -O https://raw.githubusercontent.com/SpeyTech/crawler-log-analyser/main/crawler_log_analyser.py
chmod +x crawler_log_analyser.py
./crawler_log_analyser.py /var/log/nginx/access.log
```

The script has a `#!/usr/bin/env python3` shebang so it runs directly once executable. If you'd rather not set the executable bit (e.g. on a shared system), invoke it via the interpreter instead: `python3 crawler_log_analyser.py /var/log/nginx/access.log`.

Cron / CI example:

```bash
python3 crawler_log_analyser.py \
  /var/log/nginx/access.log* \
  --format markdown \
  --output crawler-report.md \
  --strict
```

The log format is auto-detected per file. Use `--log-format combined` or `--log-format seo-crawl` to override. The recommended seo_crawl `log_format` definition is included in the report output.

### Optional Config File

Standing operator preferences (ignored source IPs, per-site launch dates, AI crawler discovery window) can be set in `~/.config/crawler-log-analyser/config.toml`:

```toml
ignore_source_ips = ["35.230.156.201"]

[sites."example.com"]
launch_date = "2026-05-14"

[ai_crawlers]
expected_discovery_window_days_min = 7
expected_discovery_window_days_max = 21
```

Verify with `--show-config` before running a full report. CLI flags always layer additively on top of config values; `--no-config-ignores` opts out for one-off investigation runs.

## What It Doesn't Do

* no dashboards, no web UI, no live monitoring mode
* no database, telemetry, or external services
* no Apache or IIS support guarantees
* no Slack, Prometheus, Elasticsearch, or SaaS integrations
* no Cloudflare analytics replacement
* no distributed monitoring features
* JSON output is the integration boundary

Companion project: `seo-validator` handles pre-deploy correctness; this tool handles post-deploy crawler observation.

## Requirements

Python 3.11+ (stdlib `tomllib` is used for config parsing). No external dependencies.

## Licence

Licensed under AGPL-3.0-or-later. Commercial use, internal modification, and integration into internal tooling are permitted. If modified versions are offered as a network service, corresponding source code must also be made available under the AGPL. See `LICENSE`.

## Status

v1.10.2. Tested against production nginx logs on `speytech.com` and `axilog.io`, including production crawler traffic from Googlebot, Bingbot, OAI-SearchBot, ClaudeBot, GPTBot, ChatGPT-User, PerplexityBot, Applebot, YandexBot, and others. **Feature-frozen through 31 August 2026 — see [`docs/FEATURE-FREEZE.md`](docs/FEATURE-FREEZE.md).** PRs are welcome; review may be slow as the project is maintained as time permits.
