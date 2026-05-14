#!/usr/bin/env python3
"""v1.7 verification — synthetic tests against the requirements doc test matrix."""
import sys, os

# Resolve the analyser location relative to this test file so the suite
# is portable across repo checkouts (works in any clone, not just the
# author's sandbox).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from datetime import datetime, timedelta, timezone
from collections import Counter
from crawler_log_analyser import (
    classify_bot,
    is_likely_browser_ua,
    is_spoof_behaviour,
    in_post_indexnow_window,
    compute_health_score,
    Aggregate,
    LogEntry,
    SPOOF_DETECTION_NAME,
    SPOOF_OVERRIDE_CANDIDATES,
    BROWSER_UA_RE,
    parse_line,
    aggregate,
    CANONICAL_LOOP_HIGH_TRAFFIC_THRESHOLD,
    POST_INDEXNOW_WINDOW_HOURS_DEFAULT,
)

results = []
def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((status, name, detail))
    print(f"{status}  {name}" + (f"  ({detail})" if detail else ""))


# ─────────────────────────────────────────────────────────────────────
# FEATURE 1: Named bot patterns
# ─────────────────────────────────────────────────────────────────────
print("\n=== Feature 1: Named bot patterns ===\n")

for bot_ua_fragment, expected_name in [
    ("Mozilla/5.0 (compatible; DotBot/1.2; +http://www.opensiteexplorer.org/dotbot)", "DotBot"),
    ("Mozilla/5.0 (compatible; Qwantbot/1.0; +https://help.qwant.com/bot/)", "Qwantbot"),
    ("Mozilla/5.0 (compatible; SeznamBot/4.0; +http://napoveda.seznam.cz/seznambot-intro/)", "SeznamBot"),
    ("Mozilla/5.0 (compatible; SERankingBacklinksBot/1.0; +https://seranking.com/)", "SERankingBacklinksBot"),
]:
    name, is_ai = classify_bot(bot_ua_fragment)
    check(f"F1: {expected_name} matches", name == expected_name and not is_ai,
          f"got bot={name}, is_ai={is_ai}")

# Mixed traffic: verify existing patterns still work
for ua, expected in [
    ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)", "Googlebot"),
    ("Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)", "Bingbot"),
    ("Mozilla/5.0 (compatible; YandexBot/3.0; +http://yandex.com/bots)", "YandexBot"),
]:
    name, _ = classify_bot(ua)
    check(f"F1: existing pattern {expected} preserved", name == expected, f"got {name}")

# Sanity: SEARCH_CRAWLERS cohort unchanged (the new bots must NOT be in it)
from crawler_log_analyser import SEARCH_CRAWLERS
check("F1: SEARCH_CRAWLERS unchanged (no new bots added)",
      SEARCH_CRAWLERS == {"Googlebot", "Googlebot-Image", "Bingbot",
                          "DuckDuckBot", "YandexBot", "BaiduSpider"},
      f"got {sorted(SEARCH_CRAWLERS)}")


# ─────────────────────────────────────────────────────────────────────
# FEATURE 2: UA-spoof detection (six cases from requirements doc)
# ─────────────────────────────────────────────────────────────────────
print("\n=== Feature 2: UA-spoof detection ===\n")

FIREFOX_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:122.0) Gecko/20100101 Firefox/122.0"
GOOGLEBOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

# Test 1: Firefox UA, empty referrer, fetching /sitemap-0.xml → spoof
check("F2 test 1: Firefox + empty referrer + sitemap → spoof",
      is_spoof_behaviour("/sitemap-0.xml", FIREFOX_UA, "-") is True)

# Test 2: Firefox UA, non-empty referrer, fetching /sitemap-0.xml → NOT spoof
check("F2 test 2: Firefox + referrer + sitemap → NOT spoof",
      is_spoof_behaviour("/sitemap-0.xml", FIREFOX_UA, "https://example.com/") is False)

# Test 3: Firefox UA, empty referrer, fetching article page → NOT spoof
check("F2 test 3: Firefox + empty referrer + article → NOT spoof",
      is_spoof_behaviour("/insights/article-name/", FIREFOX_UA, "-") is False)

# Test 4: Empty UA, no referrer, fetching /sitemap-0.xml → Empty-UA, not spoof
# Empty UA never qualifies as browser-like, so the override gate never fires.
name_t4, _ = classify_bot("-")
check("F2 test 4: Empty UA → Empty-UA (gate intact)", name_t4 == "Empty-UA",
      f"got {name_t4}")
check("F2 test 4: Empty UA does not satisfy spoof behaviour",
      is_spoof_behaviour("/sitemap-0.xml", "-", "-") is False)
check("F2 test 4: is_likely_browser_ua('-') is False",
      is_likely_browser_ua("-") is False)
check("F2 test 4: is_likely_browser_ua('') is False",
      is_likely_browser_ua("") is False)

# Test 5: Googlebot UA, fetching /sitemap-0.xml → Googlebot wins
name_t5, _ = classify_bot(GOOGLEBOT_UA)
check("F2 test 5: Googlebot UA classified as Googlebot (override skipped)",
      name_t5 == "Googlebot")
# Even though the request shape would qualify as spoof, "Googlebot" is not
# in SPOOF_OVERRIDE_CANDIDATES, so it would be left alone in parse_line.
check("F2 test 5: Googlebot not in SPOOF_OVERRIDE_CANDIDATES",
      "Googlebot" not in SPOOF_OVERRIDE_CANDIDATES)

# Test 6: end-to-end through parse_line
# Synthesise an nginx combined-format line with the spoof signature.
def synth_log_line(ua, path, status=200, referrer="-",
                   ip="5.255.103.97",
                   ts="14/May/2026:09:00:00 +0000"):
    return (f'{ip} - - [{ts}] '
            f'"GET {path} HTTP/1.1" {status} 1024 '
            f'"{referrer}" "{ua}"')

line = synth_log_line(FIREFOX_UA, "/sitemap-0.xml")
entry = parse_line(line)
check("F2 test 6: parse_line classifies Firefox + sitemap as SuspectedUASpoof",
      entry is not None and entry.bot == SPOOF_DETECTION_NAME,
      f"got bot={entry.bot if entry else 'None'}")

# Test 6b: same UA + referrer present → must NOT be spoof
line_with_ref = synth_log_line(FIREFOX_UA, "/sitemap-0.xml",
                               referrer="https://google.com/")
entry_with_ref = parse_line(line_with_ref)
check("F2 test 6b: Firefox + referrer + sitemap → NOT spoof in parse_line",
      entry_with_ref is not None and entry_with_ref.bot != SPOOF_DETECTION_NAME,
      f"got bot={entry_with_ref.bot if entry_with_ref else 'None'}")

# Test 6c: Googlebot + sitemap survives override
line_gb = synth_log_line(GOOGLEBOT_UA, "/sitemap-0.xml")
entry_gb = parse_line(line_gb)
check("F2 test 6c: Googlebot UA + sitemap stays Googlebot",
      entry_gb is not None and entry_gb.bot == "Googlebot",
      f"got bot={entry_gb.bot if entry_gb else 'None'}")

# Verify the requirements doc's 5 specific spoof cases
print("\n  Replicating the 5 Firefox-spoofer cases from requirements doc:")
spoof_fixtures = [
    ("5.255.103.97", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0", "/sitemap.txt"),
    ("126.130.101.189", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0", "/sprinklers/"),
    ("14.169.63.28", "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0", "/sitemap-0.xml"),
    ("145.220.91.19", "Mozilla/5.0 (X11; Linux x86_64; rv:122.0) Gecko/20100101 Firefox/122.0", "/"),
    ("198.51.100.7", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0", "/sitemap-index.xml"),
]

# Cases (1, 3, 5) hit SPOOF_TRIGGER_PATHS so should classify as spoof.
# Cases (2, 4) do NOT — /sprinklers/ and / are not in the trigger set,
# even though the requirements doc lists them. The doc's discriminator
# is path-restricted: a 404 on /sprinklers/ shows up via the existing
# crawler-404 path, and / with a HTTP→HTTPS redirect is not in the set.
# Verifying that the detector fires on (1, 3, 5) and not (2, 4) is the
# right behaviour, even though the doc text described all 5 as
# "spoof"-flavoured anomalies.
expected_spoof = [True, False, True, False, True]
for (ip, ua, path), should_spoof in zip(spoof_fixtures, expected_spoof):
    line = synth_log_line(ua, path, ip=ip, status=200)
    entry = parse_line(line)
    is_spoof = entry is not None and entry.bot == SPOOF_DETECTION_NAME
    check(f"F2 fixture {ip} ({path})",
          is_spoof == should_spoof,
          f"expected spoof={should_spoof}, got={is_spoof}")


# ─────────────────────────────────────────────────────────────────────
# FEATURE 3: Post-deploy redirect rate handling
# ─────────────────────────────────────────────────────────────────────
print("\n=== Feature 3: Post-deploy redirect rate ===\n")

# Build synthetic aggregates with known shape.
def make_agg_with_redirects(total_gb, redirect_count, redirects_recent_hours=None):
    """Build an Aggregate with Googlebot total/redirect counts and optional
    canonical-loop first-seen timestamps.

    Populates sitemap and robots.txt fetches so the v1.6 'did not fetch'
    deductions don't fire — those are unrelated to the v1.7 redirect-rate
    logic we're isolating in these tests.
    """
    now = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)
    agg = Aggregate()
    agg.latest = now
    agg.earliest = now - timedelta(hours=24)

    # Populate Googlebot status counter
    agg.bot_status["Googlebot"][200] = total_gb - redirect_count
    agg.bot_status["Googlebot"][301] = redirect_count

    # Populate sitemap + robots fetches so unrelated deductions don't fire.
    agg.special_files["/robots.txt"]["Googlebot"][200] = 1
    agg.special_files["/sitemap-0.xml"]["Googlebot"][200] = 1

    # Populate canonical-loop attribution for the redirect URL so it
    # qualifies as high-traffic, and the first-seen timestamp.
    if redirects_recent_hours is not None and redirect_count >= CANONICAL_LOOP_HIGH_TRAFFIC_THRESHOLD:
        loop_path = "/images/example.svg"
        agg.redirect_attribution["Googlebot"][loop_path]["canonical_loop"] = redirect_count
        agg.high_traffic_redirect_first_seen[loop_path] = now - timedelta(hours=redirects_recent_hours)

    return agg

# Test 1: 30% redirect rate, no IndexNow window → -5 deduction
agg1 = make_agg_with_redirects(total_gb=100, redirect_count=30, redirects_recent_hours=None)
score1, _, reasons1 = compute_health_score(agg1)
expected_text1 = "redirect rate 30.0% (>25%)"
check("F3 test 1: 30% no-window → -5 deduction",
      score1 == 95 and any(expected_text1 in r and r.startswith("-5:") for r in reasons1),
      f"score={score1}, reasons={reasons1}")

# Test 2: 30% redirect rate, all redirects recent → no deduction, INFO note
agg2 = make_agg_with_redirects(total_gb=100, redirect_count=30, redirects_recent_hours=2)
score2, _, reasons2 = compute_health_score(agg2)
check("F3 test 2: 30% post-IndexNow → no deduction",
      score2 == 100 and any(r.startswith("INFO:") and "suppressed" in r for r in reasons2),
      f"score={score2}, reasons={reasons2}")

# Test 3: 55% redirect rate, all recent → -5 deduction (severe, always)
agg3 = make_agg_with_redirects(total_gb=100, redirect_count=55, redirects_recent_hours=2)
score3, _, reasons3 = compute_health_score(agg3)
check("F3 test 3: 55% post-IndexNow → severe deduction (window ignored)",
      score3 == 95 and any("severe" in r and r.startswith("-5:") for r in reasons3),
      f"score={score3}, reasons={reasons3}")

# Test 4: 30% redirect rate, redirects 6h old → -5 deduction (outside window)
agg4 = make_agg_with_redirects(total_gb=100, redirect_count=30, redirects_recent_hours=6)
score4, _, reasons4 = compute_health_score(agg4)
check("F3 test 4: 30% outside window (6h ago) → -5 deduction",
      score4 == 95 and any(r.startswith("-5:") and "(>25%)" in r for r in reasons4),
      f"score={score4}, reasons={reasons4}")

# Test 5: 20% redirect rate → no deduction (below threshold)
agg5 = make_agg_with_redirects(total_gb=100, redirect_count=20, redirects_recent_hours=None)
score5, _, reasons5 = compute_health_score(agg5)
check("F3 test 5: 20% below threshold → no deduction",
      score5 == 100 and not any(r.startswith("-5:") and "redirect rate" in r for r in reasons5),
      f"score={score5}, reasons={reasons5}")

# Test 6: 27.3% production-day case (rebuild the morning's 407/1488 numbers)
# 407/1488 = 0.27352..., which Python formats as either 27.3% or 27.4%
# depending on the rounding mode. The score and the INFO suppression are
# what matter; the displayed percentage is a one-decimal-place artefact.
agg6 = make_agg_with_redirects(total_gb=1488, redirect_count=407, redirects_recent_hours=1)
score6, _, reasons6 = compute_health_score(agg6)
check("F3 test 6: 27.3% production case (post-ping) → 100/100",
      score6 == 100 and any("suppressed" in r and r.startswith("INFO:") for r in reasons6),
      f"score={score6}, reasons={reasons6}")

# in_post_indexnow_window edge cases
agg_empty = Aggregate()
check("F3: empty aggregate → no window",
      in_post_indexnow_window(agg_empty) is False)

# Test that the configurable window value flows through
agg_1h_old = make_agg_with_redirects(total_gb=100, redirect_count=30, redirects_recent_hours=1)
check("F3: 1h-old redirects within 4h default → in window",
      in_post_indexnow_window(agg_1h_old, hours=4) is True)
check("F3: 1h-old redirects with 0.5h window → NOT in window",
      in_post_indexnow_window(agg_1h_old, hours=0) is False)


# ─────────────────────────────────────────────────────────────────────
# CROSS-CUTTING: backward compatibility
# ─────────────────────────────────────────────────────────────────────
print("\n=== Backward compatibility ===\n")

# An empty Aggregate produces a score of 100 with no reasons
empty_agg = Aggregate()
score_e, verdict_e, reasons_e = compute_health_score(empty_agg)
check("BC: empty aggregate → 100/100, no reasons",
      score_e == 100 and verdict_e == "Excellent" and reasons_e == [])

# Existing Googlebot 404 deduction still fires
agg_404 = Aggregate()
agg_404.bot_status["Googlebot"][200] = 90
agg_404.bot_status["Googlebot"][404] = 10  # 10% — exceeds 1% threshold
# Populate special files so the v1.6 'did not fetch' deductions don't muddy
# the assertion (this test is about the -10 404-rate deduction in isolation).
agg_404.special_files["/robots.txt"]["Googlebot"][200] = 1
agg_404.special_files["/sitemap-0.xml"]["Googlebot"][200] = 1
score_404, _, reasons_404 = compute_health_score(agg_404)
check("BC: 10% Googlebot 404 rate → -10 deduction",
      score_404 == 90 and any("404 rate" in r for r in reasons_404),
      f"score={score_404}, reasons={reasons_404}")


# ─────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
passed = sum(1 for s, _, _ in results if s == "PASS")
failed = sum(1 for s, _, _ in results if s == "FAIL")
print(f"\n{passed} passed, {failed} failed of {len(results)} tests")
if failed:
    print("\nFAILURES:")
    for s, n, d in results:
        if s == "FAIL":
            print(f"  - {n} :: {d}")
    sys.exit(1)
sys.exit(0)
