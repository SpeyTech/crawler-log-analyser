# crawler-log-analyser v1.10.0 — Verification Recipe

This document captures the reproducible verification procedure for
v1.10.0. Reinstating the discipline that lapsed in v1.9 — v1.8 was the
last release with a verification recipe.

## What v1.10.0 changes

A second-stage post-aggregation override that reclassifies attacker
rotation traffic away from the named-bot it claimed to be, into
`SuspectedBotIdentityRotation`. Three new CLI flags, one new report
section, one new conditional JSON key.

Full motivation and change list: `CHANGELOG-v1.10.0.md`.

## Pre-flight

Confirm the analyser reports v1.10.0:

```
$ python3 crawler_log_analyser.py --version
crawler_log_analyser 1.10.0
```

Confirm the three new flags are present in `--help`:

```
$ python3 crawler_log_analyser.py --help | grep rotation
  --rotation-window-minutes ROTATION_WINDOW_MINUTES
  --rotation-min-identities ROTATION_MIN_IDENTITIES
  --show-rotation-detail
```

## Unit-test suite

23 tests, stdlib only, no fixtures on disk:

```
$ python3 -m unittest test_v110.py -v
...
Ran 23 tests in 0.014s
OK
```

If anything other than `Ran 23 tests in <time>s` + `OK` appears at the
end, the run failed and v1.10.0 is not safe to deploy.

## Backward-compatibility check (text format)

The promise is: on logs that contain no rotation traffic, v1.10.0
produces byte-identical output to v1.9.1. The check uses a clean
synthetic log with three Googlebot fetches and one Bingbot fetch —
none of the inputs that would trigger the rotation override.

Create the clean fixture:

```
cat > /tmp/clean_fixture.log <<'EOF'
66.249.66.10 - - [16/May/2026:09:05:00 +0000] "GET /robots.txt HTTP/1.1" 200 421 "-" "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
66.249.66.10 - - [16/May/2026:09:05:05 +0000] "GET /sitemap.xml HTTP/1.1" 200 1820 "-" "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
66.249.66.10 - - [16/May/2026:09:05:08 +0000] "GET /insights/some-article/ HTTP/1.1" 200 8421 "-" "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
40.77.167.5 - - [16/May/2026:09:10:00 +0000] "GET /robots.txt HTTP/1.1" 200 421 "-" "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)"
EOF
```

Run both versions and diff:

```
python3 v1.9.1/crawler_log_analyser.py /tmp/clean_fixture.log > /tmp/v191.txt
python3 v1.10.0/crawler_log_analyser.py /tmp/clean_fixture.log > /tmp/v110.txt
diff /tmp/v191.txt /tmp/v110.txt
```

Expected: zero output (no differences).

Repeat for JSON:

```
python3 v1.9.1/crawler_log_analyser.py /tmp/clean_fixture.log --format json > /tmp/v191.json
python3 v1.10.0/crawler_log_analyser.py /tmp/clean_fixture.log --format json > /tmp/v110.json
diff /tmp/v191.json /tmp/v110.json
```

Expected: zero output.

## Synthetic rotation reproduction

Reproduce the 2026-05-16 `5.255.104.83` rotation pattern in a
14-line synthetic log. Eleven attacker probes claiming six distinct
identities, plus three legitimate Googlebot fetches from
`66.249.66.10` that the override must leave intact.

The fixture is checked into the test suite as `SYNTHETIC_LOG` in
`test_v110.py`. To run it through the CLI directly:

```
cat > /tmp/rotation_fixture.log <<'EOF'
5.255.104.83 - - [16/May/2026:09:00:01 +0000] "GET /.git/config HTTP/1.1" 404 153 "-" "Mozilla/5.0 (compatible; Baiduspider/2.0; +http://www.baidu.com/search/spider.html)"
5.255.104.83 - - [16/May/2026:09:00:08 +0000] "GET /.env HTTP/1.1" 404 153 "-" "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.3; +https://openai.com/gptbot)"
5.255.104.83 - - [16/May/2026:09:00:12 +0000] "GET /.env HTTP/1.1" 404 153 "-" "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.3; +https://openai.com/gptbot)"
5.255.104.83 - - [16/May/2026:09:00:18 +0000] "GET /.env.development HTTP/1.1" 404 153 "-" "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.3; +https://openai.com/gptbot)"
5.255.104.83 - - [16/May/2026:09:00:22 +0000] "GET /.env.backup HTTP/1.1" 404 153 "-" "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
5.255.104.83 - - [16/May/2026:09:00:27 +0000] "GET /.env.production HTTP/1.1" 404 153 "-" "Mozilla/5.0 (compatible; Baiduspider/2.0; +http://www.baidu.com/search/spider.html)"
5.255.104.83 - - [16/May/2026:09:00:31 +0000] "GET /.env.local HTTP/1.1" 404 153 "-" "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.3; +https://openai.com/gptbot)"
5.255.104.83 - - [16/May/2026:09:00:38 +0000] "GET /.env.bak HTTP/1.1" 404 153 "-" "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
5.255.104.83 - - [16/May/2026:09:00:45 +0000] "GET /config.json HTTP/1.1" 404 153 "-" "Mozilla/5.0 (compatible; YandexBot/3.0; +http://yandex.com/bots)"
5.255.104.83 - - [16/May/2026:09:00:51 +0000] "GET /api/env HTTP/1.1" 404 153 "-" "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; +claudebot@anthropic.com)"
5.255.104.83 - - [16/May/2026:09:00:58 +0000] "GET /actuator/env HTTP/1.1" 404 153 "-" "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)"
66.249.66.10 - - [16/May/2026:09:05:00 +0000] "GET /robots.txt HTTP/1.1" 200 421 "-" "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
66.249.66.10 - - [16/May/2026:09:05:05 +0000] "GET /sitemap.xml HTTP/1.1" 200 1820 "-" "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
66.249.66.10 - - [16/May/2026:09:05:08 +0000] "GET /insights/some-article/ HTTP/1.1" 200 8421 "-" "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
EOF

python3 crawler_log_analyser.py /tmp/rotation_fixture.log
```

Expected output features:

1. **"Rotational Bot-Identity Detection" section appears** between
   "Suspected UA-Spoof Detection" (absent here — no spoof traffic in
   the fixture) and "Framework Fingerprint Probes" (also absent here).
2. **Summary line**: `11 requests across 1 unique IPs exhibited
   rotational bot-identity behaviour`.
3. **Top rotating IPs**: `5.255.104.83` with six identities listed
   alphabetically: `BaiduSpider, Bingbot, ClaudeBot, GPTBot, Googlebot,
   YandexBot`.
4. **Status Code Breakdown By Bot** shows:
    - `Googlebot: 200: 3` (the legitimate traffic, untouched)
    - `SuspectedBotIdentityRotation: 404: 11` (the attacker traffic)
5. **Googlebot Crawl Health Score**: 100/100, no deductions.
6. **Search Crawler Health Score**: 100/100, no deductions (the
   attacker traffic that would have driven a 404-rate deduction in
   v1.9 has been re-attributed).

Run the same fixture with `--format json` and confirm the top-level
key `rotational_bot_identity` is present with:

- `"total": 11`
- `"unique_ips": 1`
- `"rotation_window_minutes": 5`
- `"min_identities_threshold": 3`
- `top_ips[0].ip == "5.255.104.83"`
- `top_ips[0].identities` containing all six bot names
- `bot_breakdown.Googlebot == {"200": 3}` (legitimate traffic preserved)
- `bot_breakdown.SuspectedBotIdentityRotation == {"404": 11}`

## Production regression test (axilog.io seo log)

Re-run v1.10.0 against the 2026-05-16 axilog.io seo log.

Expected outcomes:

1. **New "Rotational Bot-Identity Detection" section appears** in the
   report.
2. **`5.255.104.83` listed as the top rotating IP** with 5 identities
   (YandexBot, Bingbot, ClaudeBot, BaiduSpider, plus one more from
   the wider speytech.com window — the exact identity set depends on
   which logs are concatenated for the run).
3. **"Crawler-Visible 404s" section drops the `5.255.104.83` rotation
   entries**: they are now attacker-classified, not real-crawler.
4. **Broader Search Crawler Health Score returns to 100/100** (was
   90/100 with `-10: real-crawler 404 rate 2.6%` in v1.9 — that
   deduction was driven by the rotation-attributed 404s).
5. **Googlebot Crawl Health Score** also improves if any rotation
   entry claimed Googlebot — the attacker's Googlebot 404s are no
   longer in the Googlebot bucket.

If any of these five outcomes does not hold on the production log,
investigate before deploying v1.10.0 to cron.

## Parameter sweep (optional)

Confirm the CLI flags propagate as expected:

```
# Tighter 2-minute window — should still catch the synthetic
# pattern (which completes in ~57 seconds)
python3 crawler_log_analyser.py /tmp/rotation_fixture.log \
    --rotation-window-minutes 2 \
    --format json | jq '.rotational_bot_identity.total'
# expected: 11

# Higher 4-identity threshold — still catches (synthetic has 6)
python3 crawler_log_analyser.py /tmp/rotation_fixture.log \
    --rotation-min-identities 4 \
    --format json | jq '.rotational_bot_identity.total'
# expected: 11

# 7-identity threshold — does NOT catch (synthetic has only 6)
python3 crawler_log_analyser.py /tmp/rotation_fixture.log \
    --rotation-min-identities 7 \
    --format json | jq '.rotational_bot_identity // "absent"'
# expected: "absent"
```

## Sign-off checklist

- [ ] `--version` reports `1.10.0`
- [ ] `test_v110.py` passes 23/23
- [ ] Clean-log `diff` against v1.9.1 produces zero output (text + JSON)
- [ ] Synthetic rotation fixture produces the expected attribution
- [ ] Production axilog.io 2026-05-16 seo log re-run shows the five
      regression outcomes listed above
- [ ] No new entries in `stderr` warnings beyond expected
      log-format-detection lines
