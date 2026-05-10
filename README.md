# crawler-log-analyser

Operational SEO observability for self-hosted static sites running nginx.

## Why This Exists

Most crawler-visible SEO failures happen during deployment windows, not during steady-state operation.

A site can pass uptime checks, synthetic crawls, and Lighthouse audits while Googlebot receives transient 404s, broken `robots.txt`, redirect loops, or incomplete sitemap states during cache invalidation or atomic deploy transitions. Conventional SEO tooling usually misses these conditions because it observes after systems stabilise.

`crawler-log-analyser` exists to close that operational visibility gap using live nginx access logs rather than synthetic scans.

## What It Does

- parses nginx access logs, including rotated `.gz` logs
- supports both the standard combined format and the extended seo_crawl format (with `$host`, `$scheme`, `$request_time`, `$upstream_response_time`)
- classifies search and AI crawlers from user-agent traffic
- detects crawler-visible deploy-window anomalies
- scores Googlebot crawl health from observed responses
- surfaces `robots.txt`, sitemap, RSS, and `llms.txt` access patterns
- attributes redirects concretely (HTTP→HTTPS vs www→apex vs trailing-slash) when seo_crawl data is available
- exits non-zero on critical findings for cron and CI use
- emits text, markdown, or JSON reports

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

## Response Latency

Overall:
  median: 0.002s    p75: 0.004s    p95: 0.087s    p99: 0.149s

## Googlebot Crawl Health Score

Score: 70/100
Verdict: Needs attention
```

## Quick Start

```bash
curl -O https://raw.githubusercontent.com/SpeyTech/crawler-log-analyser/main/crawler_log_analyser.py
python3 crawler_log_analyser.py /var/log/nginx/access.log
```

Cron / CI example:

```bash
python3 crawler_log_analyser.py \
  /var/log/nginx/access.log* \
  --format markdown \
  --output crawler-report.md \
  --strict
```

The log format is auto-detected per file. Use `--log-format combined` or `--log-format seo-crawl` to override. The recommended seo_crawl log_format definition is included in the report output.

## What It Doesn't Do

- no dashboards, no web UI, no live monitoring mode
- no database, telemetry, or external services
- no Apache or IIS support guarantees
- no Slack, Prometheus, Elasticsearch, or SaaS integrations
- no Cloudflare analytics replacement
- no distributed monitoring features
- JSON output is the integration boundary

Companion project: `seo-validator` handles pre-deploy correctness; this tool handles post-deploy crawler observation.

## Requirements

Python 3.8+. No external dependencies.

## Licence

Licensed under AGPL-3.0-or-later. Commercial use, internal modification, and integration into internal tooling are permitted. If modified versions are offered as a network service, corresponding source code must also be made available under the AGPL. See `LICENSE`.

## Status

v1.3. Tested against production nginx logs on `speytech.com`, including production crawler traffic from Googlebot, Bingbot, OAI-SearchBot, Claude-SearchBot, and Applebot. PRs are welcome; review may be slow as the project is maintained as time permits.
