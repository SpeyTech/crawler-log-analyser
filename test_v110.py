#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Test suite for crawler_log_analyser v1.10 rotational bot-identity
detection.

Covers the eight boundary cases enumerated in the v1.10 brief §4.4,
the classification override logic, the report and JSON output shape,
and an end-to-end test using a synthetic reproduction of the
2026-05-16 5.255.104.83 rotation pattern.

Run from the repository root:

    python3 -m unittest test_v110.py -v

No external dependencies. stdlib only, matching the analyser's
no-deps posture.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure the analyser module imports from the working directory
# regardless of where the test runner is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import crawler_log_analyser as cla


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _ts(minute: int = 0, second: int = 0) -> datetime:
    """Build a UTC timestamp at 2026-05-16 09:MM:SS.

    Tests parameterise only the minute/second components so a window
    crossing (or not crossing) is obvious from the call site.
    """
    return datetime(2026, 5, 16, 9, minute, second, tzinfo=timezone.utc)


def _make_entry(
    ip: str,
    bot: str,
    path: str,
    status: int = 404,
    minute: int = 0,
    second: int = 0,
    ua: str | None = None,
    host: str | None = None,
    scheme: str | None = None,
) -> cla.LogEntry:
    """Build a LogEntry directly without going through parse_line.

    The algorithm-level tests don't need a real log line; they need a
    LogEntry with the fields the rotation detector reads. Using this
    helper keeps the tests focused on the override logic rather than
    on parsing.

    host and scheme default to None (combined-format behaviour). Pass
    explicit values to exercise the seo_crawl-format code paths that
    populate redirect_attribution.
    """
    return cla.LogEntry(
        ip=ip,
        ts=_ts(minute, second),
        method="GET",
        path=path,
        status=status,
        size=0,
        referrer="-",
        ua=ua or f"Mozilla/5.0 (compatible; {bot})",
        bot=bot,
        is_ai=False,
        category=cla.classify_url(path),
        is_probe=cla.is_security_probe(path),
        is_framework=False,
        host=host,
        scheme=scheme,
    )


def _identity_log(
    ip: str,
    items: list[tuple[int, str, str, int]],
) -> list[tuple[datetime, str, str, int, cla.LogEntry]]:
    """Build a per-IP identity log shaped exactly as aggregate() does.

    items is a list of (seconds_from_start, bot, path, status) tuples.
    seconds_from_start is the second-of-minute on 09:00 — keeping it
    short avoids minute/hour bookkeeping in the test cases.
    """
    out: list[tuple[datetime, str, str, int, cla.LogEntry]] = []
    for sec, bot, path, status in items:
        # Allow minute rollover: pass seconds directly, normalise to
        # (minute, second). Keeps the test inputs readable for the
        # spread-across-30-minutes case.
        minute, second = divmod(sec, 60)
        entry = _make_entry(ip, bot, path, status,
                            minute=minute, second=second)
        out.append((entry.ts, bot, path, status, entry))
    return out


# ---------------------------------------------------------------------------
# §4.4 boundary cases — window-walker algorithm
# ---------------------------------------------------------------------------


class TestRotationWindowWalker(unittest.TestCase):
    """Boundary cases for detect_rotation_indices().

    Each case mirrors one of the enumerated scenarios in the v1.10
    brief §4.4. The walker is the part of v1.10 most sensitive to
    off-by-one and inclusive/exclusive-window bugs; this suite is the
    safety net for those.
    """

    WINDOW = timedelta(minutes=5)
    MIN_IDENTITIES = 3

    def test_single_ip_three_identities_in_window_all_flagged(self) -> None:
        """One IP, 3 distinct identities, all within 60 seconds → all flagged."""
        entries = _identity_log("1.1.1.1", [
            (0, "Googlebot", "/.env", 404),
            (10, "GPTBot", "/.env", 404),
            (20, "ClaudeBot", "/config.json", 404),
        ])
        idx = cla.detect_rotation_indices(entries, self.WINDOW, self.MIN_IDENTITIES)
        self.assertEqual(idx, {0, 1, 2})

    def test_three_identities_outside_window_none_flagged(self) -> None:
        """3 identities spread across 30 minutes → no window contains 3 → none flagged."""
        entries = _identity_log("1.1.1.1", [
            (0,    "Googlebot", "/.env", 404),
            (600,  "GPTBot",    "/.env", 404),    # +10 min
            (1800, "ClaudeBot", "/.env", 404),    # +30 min
        ])
        idx = cla.detect_rotation_indices(entries, self.WINDOW, self.MIN_IDENTITIES)
        self.assertEqual(idx, set())

    def test_two_identities_below_threshold_none_flagged(self) -> None:
        """2 distinct identities falls below min_identities=3 → none flagged."""
        entries = _identity_log("1.1.1.1", [
            (0,  "Googlebot", "/.env", 404),
            (10, "GPTBot",    "/.env", 404),
            (20, "Googlebot", "/.env", 404),   # same identity as 0
        ])
        idx = cla.detect_rotation_indices(entries, self.WINDOW, self.MIN_IDENTITIES)
        self.assertEqual(idx, set())

    def test_overlapping_clusters_merge(self) -> None:
        """5 identities where windows overlap: all flagged.

        Brief §4.4: 'IP with 5 identities where first 3 are in window-1
        and last 3 in window-2 → all 5 flagged'. Construct timestamps
        so the only way for both 3-identity windows to exist is if
        the middle identity participates in both.
        """
        entries = _identity_log("1.1.1.1", [
            (0,   "Googlebot", "/.env", 404),
            (60,  "GPTBot",    "/.env", 404),
            (120, "ClaudeBot", "/.env", 404),   # in both windows
            (240, "Bingbot",   "/.env", 404),
            (360, "YandexBot", "/.env", 404),   # +6 min from idx 0 (outside window 0..5)
        ])
        idx = cla.detect_rotation_indices(entries, self.WINDOW, self.MIN_IDENTITIES)
        self.assertEqual(idx, {0, 1, 2, 3, 4})

    def test_empty_identity_log_returns_empty_set(self) -> None:
        """No entries → no rotation → empty set, no exception."""
        idx = cla.detect_rotation_indices([], self.WINDOW, self.MIN_IDENTITIES)
        self.assertEqual(idx, set())

    def test_exact_window_boundary_inclusive(self) -> None:
        """Entries exactly window apart are still within the window.

        Brief implies inclusive boundary (window_end = start + window,
        check is entries[j].ts > window_end → break). Confirm
        explicitly: three identities at 0s, 150s, 300s with a
        5-minute window catches all three.
        """
        entries = _identity_log("1.1.1.1", [
            (0,   "Googlebot", "/.env", 404),
            (150, "GPTBot",    "/.env", 404),
            (300, "ClaudeBot", "/.env", 404),    # exactly +5 min
        ])
        idx = cla.detect_rotation_indices(entries, self.WINDOW, self.MIN_IDENTITIES)
        self.assertEqual(idx, {0, 1, 2})

    def test_just_outside_window_excludes(self) -> None:
        """One second past the window boundary → that entry not flagged.

        Pair with test_exact_window_boundary_inclusive to lock in the
        inclusive-on, exclusive-just-after behaviour.
        """
        entries = _identity_log("1.1.1.1", [
            (0,   "Googlebot", "/.env", 404),
            (150, "GPTBot",    "/.env", 404),
            (301, "ClaudeBot", "/.env", 404),    # +5 min 1 sec — outside
        ])
        idx = cla.detect_rotation_indices(entries, self.WINDOW, self.MIN_IDENTITIES)
        self.assertEqual(idx, set())

    def test_duplicate_identity_does_not_count_twice(self) -> None:
        """Same identity twice in window doesn't satisfy 3-distinct."""
        entries = _identity_log("1.1.1.1", [
            (0,  "Googlebot", "/.env", 404),
            (10, "Googlebot", "/.env.backup", 404),
            (20, "Googlebot", "/.env.local", 404),
        ])
        idx = cla.detect_rotation_indices(entries, self.WINDOW, self.MIN_IDENTITIES)
        self.assertEqual(idx, set())


# ---------------------------------------------------------------------------
# Classification override — apply_rotation_override end-to-end
# ---------------------------------------------------------------------------


class TestClassificationOverride(unittest.TestCase):
    """Verify apply_rotation_override re-attributes counters correctly."""

    def _build_aggregate_from_entries(
        self, entries: list[cla.LogEntry]
    ) -> cla.Aggregate:
        """Run aggregate() over a list of LogEntry objects.

        aggregate() takes an Iterable[LogEntry], so passing the list
        directly works. Replicates main()'s call path up to (but not
        including) the rotation override pass.
        """
        return cla.aggregate(
            entries,
            include_non_bots=False,
            show_security_probes=False,
            ignored_probe_ips=frozenset(),
        )

    def test_eligible_named_bots_reclassified(self) -> None:
        """Three eligible named bots from one IP on probe paths → all become rotation."""
        entries = [
            _make_entry("1.1.1.1", "Googlebot", "/.env",      404, second=1),
            _make_entry("1.1.1.1", "GPTBot",    "/.env.bak",  404, second=10),
            _make_entry("1.1.1.1", "ClaudeBot", "/config.json", 404, second=20),
        ]
        agg = self._build_aggregate_from_entries(entries)
        # Before override: each bot has one entry under itself.
        self.assertEqual(agg.bot_status["Googlebot"][404], 1)
        self.assertEqual(agg.bot_status["GPTBot"][404], 1)
        self.assertEqual(agg.bot_status["ClaudeBot"][404], 1)

        cla.apply_rotation_override(agg, window_minutes=5, min_identities=3)

        # After: all three under SuspectedBotIdentityRotation.
        self.assertEqual(agg.rotation_count, 3)
        self.assertEqual(
            agg.bot_status[cla.ROTATION_DETECTION_NAME][404], 3
        )
        # Original buckets are emptied.
        self.assertNotIn("Googlebot", agg.bot_status)
        self.assertNotIn("GPTBot",    agg.bot_status)
        self.assertNotIn("ClaudeBot", agg.bot_status)

    def test_ineligible_bots_left_alone(self) -> None:
        """GenericBot is not in ROTATION_OVERRIDE_CANDIDATES → never reclassified.

        Three GenericBot hits from one IP on probe paths should not
        produce any rotation entries even though the path criterion
        is satisfied: GenericBot lacks the "claimed trusted identity"
        signal that the override is designed to catch.
        """
        entries = [
            _make_entry("1.1.1.1", "GenericBot", "/.env",      404, second=1,
                        ua="Mozilla/5.0 (compatible; UnknownBotCrawler/1.0)"),
            _make_entry("1.1.1.1", "GenericBot", "/.env.bak",  404, second=10,
                        ua="Mozilla/5.0 (compatible; UnknownBotCrawler/1.0)"),
            _make_entry("1.1.1.1", "GenericBot", "/config.json", 404, second=20,
                        ua="Mozilla/5.0 (compatible; UnknownBotCrawler/1.0)"),
        ]
        agg = self._build_aggregate_from_entries(entries)
        cla.apply_rotation_override(agg, window_minutes=5, min_identities=3)
        self.assertEqual(agg.rotation_count, 0)
        # GenericBot should retain its three hits unchanged.
        self.assertEqual(agg.bot_status["GenericBot"][404], 3)

    def test_legitimate_googlebot_traffic_preserved(self) -> None:
        """Rotation 404 on probe path does not erase Googlebot's legitimate hits.

        This is the core promise of the v1.10 design: only the
        rotation entries are re-attributed. A real Googlebot fetch
        from a DIFFERENT IP must survive unchanged.
        """
        entries = [
            # Real Googlebot.
            _make_entry("66.249.66.10", "Googlebot", "/robots.txt", 200,
                        minute=5, second=0),
            _make_entry("66.249.66.10", "Googlebot", "/sitemap.xml", 200,
                        minute=5, second=5),
            # Rotation attacker also claiming Googlebot.
            _make_entry("5.255.104.83", "Googlebot",  "/.env",       404, second=1),
            _make_entry("5.255.104.83", "GPTBot",     "/.env.bak",   404, second=10),
            _make_entry("5.255.104.83", "ClaudeBot",  "/config.json", 404, second=20),
        ]
        agg = self._build_aggregate_from_entries(entries)
        cla.apply_rotation_override(agg, window_minutes=5, min_identities=3)

        # Legitimate Googlebot 200s survive.
        self.assertEqual(agg.bot_status["Googlebot"][200], 2)
        # The attacker's Googlebot 404 is gone.
        self.assertEqual(agg.bot_status["Googlebot"].get(404, 0), 0)
        # Rotation captured all three attacker entries.
        self.assertEqual(agg.rotation_count, 3)
        # Real Googlebot IP still attached.
        self.assertIn("66.249.66.10", agg.bot_ips["Googlebot"])

    def test_rotation_entries_dropped_from_crawler_404s(self) -> None:
        """404 from a rotation entry is removed from the real-crawler 404 list.

        Three paths chosen to mix the two filter classes:
          - /.env IS a security probe → pre-override it's already absent
            from crawler_404s (the v1.8 probe filter caught it). The
            post-override state must be: still absent.
          - /config.json is rotation-trigger-only (not probe-matched)
            → pre-override it IS in crawler_404s under the named bot.
            The post-override state must be: removed.
          - /api/env is rotation-trigger-only as well, same expectation.

        Together the three assertions confirm the override clears
        rotation-attributable 404s regardless of which filter class
        the path falls under.
        """
        # Sanity: confirm the classification each path falls under.
        # If these assertions fail in a future version they indicate a
        # regex drift and the test below will need re-baselining.
        self.assertTrue(cla.is_security_probe("/.env"))
        self.assertFalse(cla.is_security_probe("/config.json"))
        self.assertFalse(cla.is_security_probe("/api/env"))

        entries = [
            _make_entry("1.1.1.1", "Googlebot",  "/.env",       404, second=1),
            _make_entry("1.1.1.1", "GPTBot",     "/config.json", 404, second=10),
            _make_entry("1.1.1.1", "ClaudeBot",  "/api/env",    404, second=20),
        ]
        agg = self._build_aggregate_from_entries(entries)

        # /.env is a security probe → absent from crawler_404s pre-override.
        self.assertNotIn("/.env", agg.crawler_404s)
        # /config.json and /api/env are NOT probe-matched → present pre-override.
        self.assertIn("/config.json", agg.crawler_404s)
        self.assertIn("/api/env",    agg.crawler_404s)

        cla.apply_rotation_override(agg, window_minutes=5, min_identities=3)

        # Post-override: all three rotation paths are absent. /.env was
        # never present; /config.json and /api/env were stripped by the
        # override.
        self.assertNotIn("/.env",       agg.crawler_404s)
        self.assertNotIn("/config.json", agg.crawler_404s)
        self.assertNotIn("/api/env",    agg.crawler_404s)
        # And rotation_count reflects all three.
        self.assertEqual(agg.rotation_count, 3)


class TestNon404RotationPath(unittest.TestCase):
    """Cover the case where rotation triggers a path NOT caught by the
    existing security-probe regex but IS in ROTATION_TRIGGER_PATHS.

    /api/auth is in ROTATION_TRIGGER_PATHS but does not match any
    SECURITY_PROBE_PATTERNS regex. A 404 on this path WILL therefore
    land in crawler_404s pre-override, and the rotation override must
    remove it. (/api/admin would have worked but it matches /admin in
    the probe regex, so the v1.8 filter excludes it from crawler_404s
    already — verified by the sanity-check assertion below.)
    """

    def test_api_auth_404_removed_from_crawler_404s_by_rotation(self) -> None:
        # Sanity: confirm the path's trigger-but-not-probe status. If
        # this changes (e.g. SECURITY_PROBE_PATTERNS gains /api/auth
        # in a later version), the test becomes vacuous; the assertion
        # below will catch that drift loudly.
        self.assertTrue(cla.is_rotation_trigger_path("/api/auth"))
        self.assertFalse(cla.is_security_probe("/api/auth"))

        entries = [
            _make_entry("1.1.1.1", "Googlebot",  "/api/auth",  404, second=1),
            _make_entry("1.1.1.1", "GPTBot",     "/api/auth",  404, second=10),
            _make_entry("1.1.1.1", "ClaudeBot",  "/api/auth",  404, second=20),
        ]
        agg = cla.aggregate(
            entries, include_non_bots=False, show_security_probes=False
        )
        # Before override: /api/auth should be in crawler_404s under
        # all three bots (it's a 404 by a real-classified bot, and
        # /api/auth isn't probe-filtered).
        self.assertIn("/api/auth", agg.crawler_404s)
        self.assertEqual(set(agg.crawler_404s["/api/auth"].keys()),
                         {"Googlebot", "GPTBot", "ClaudeBot"})

        cla.apply_rotation_override(agg, window_minutes=5, min_identities=3)

        # After: /api/auth is no longer in crawler_404s at all
        # (all three contributing bots had their attribution removed).
        self.assertNotIn("/api/auth", agg.crawler_404s)


class TestIgnoreSourceIPsExempt(unittest.TestCase):
    """Open question 3 in the brief: config-ignored IPs are exempt
    from rotation candidate capture, matching v1.5's probe-noise
    filtering. An operator's own multi-UA curl tests must not flag.
    """

    def test_ignored_ip_does_not_enter_rotation_pool(self) -> None:
        entries = [
            _make_entry("10.0.0.1", "Googlebot",  "/.env",       404, second=1),
            _make_entry("10.0.0.1", "GPTBot",     "/.env.bak",   404, second=10),
            _make_entry("10.0.0.1", "ClaudeBot",  "/config.json", 404, second=20),
        ]
        agg = cla.aggregate(
            entries,
            include_non_bots=False,
            show_security_probes=False,
            ignored_probe_ips=frozenset({"10.0.0.1"}),
        )
        cla.apply_rotation_override(agg, window_minutes=5, min_identities=3)
        self.assertEqual(agg.rotation_count, 0)
        # ip_identity_log was never populated for the ignored IP.
        self.assertNotIn("10.0.0.1", agg.ip_identity_log)


class TestRotationOnNonTriggerPath(unittest.TestCase):
    """Rotation pattern on an innocuous path (e.g. /about/) must not flag.

    The override's path filter is critical: three real crawlers
    landing on /about/ within a 5-minute window IS legitimate (it's
    a known shared-cohort behaviour around indexing pings) and must
    not be reclassified.
    """

    def test_non_trigger_path_excluded(self) -> None:
        entries = [
            _make_entry("1.1.1.1", "Googlebot",  "/about/", 200, second=1),
            _make_entry("1.1.1.1", "GPTBot",     "/about/", 200, second=10),
            _make_entry("1.1.1.1", "ClaudeBot",  "/about/", 200, second=20),
        ]
        agg = cla.aggregate(
            entries, include_non_bots=False, show_security_probes=False
        )
        cla.apply_rotation_override(agg, window_minutes=5, min_identities=3)
        self.assertEqual(agg.rotation_count, 0)
        # Each named bot retains its single hit.
        self.assertEqual(agg.bot_status["Googlebot"][200], 1)
        self.assertEqual(agg.bot_status["GPTBot"][200], 1)
        self.assertEqual(agg.bot_status["ClaudeBot"][200], 1)


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


class TestReportRendering(unittest.TestCase):
    """Verify the text report section behaves correctly."""

    def test_section_omitted_when_no_rotation(self) -> None:
        """Clean Aggregate → render_rotation_detection returns empty string."""
        agg = cla.Aggregate()
        out = cla.render_rotation_detection(agg, fmt="text")
        self.assertEqual(out, "")

    def test_section_rendered_when_rotation_present(self) -> None:
        """Populated Aggregate → section header + summary text."""
        entries = [
            _make_entry("5.255.104.83", "Googlebot",  "/.env",        404, second=1),
            _make_entry("5.255.104.83", "GPTBot",     "/.env.bak",    404, second=10),
            _make_entry("5.255.104.83", "ClaudeBot",  "/config.json", 404, second=20),
        ]
        agg = cla.aggregate(
            entries, include_non_bots=False, show_security_probes=False
        )
        cla.apply_rotation_override(agg, window_minutes=5, min_identities=3)
        out = cla.render_rotation_detection(agg, fmt="text")
        # Section header present
        self.assertIn("Rotational Bot-Identity Detection", out)
        # Summary line present
        self.assertIn("3 requests across 1 unique IPs", out)
        # The rotating IP appears, with all three identities listed
        # alphabetically (sorted() guarantees deterministic order)
        self.assertIn("5.255.104.83", out)
        self.assertIn("ClaudeBot", out)
        self.assertIn("GPTBot", out)
        self.assertIn("Googlebot", out)
        # Helper line directing to --show-rotation-detail
        self.assertIn("--show-rotation-detail", out)


# ---------------------------------------------------------------------------
# JSON shape
# ---------------------------------------------------------------------------


class TestJSONOutput(unittest.TestCase):
    """Verify the JSON top-level key behaves additively per the v1.x
    backward-compat contract.
    """

    def _json_payload(self, agg: cla.Aggregate) -> dict:
        """Render JSON and parse it back. Latency thresholds and
        IndexNow window default values are fine for these tests —
        the rotation key is independent.
        """
        out = cla.render_json(
            agg, top_n=20, verification=None,
            latency_threshold_ms=1000,
            post_indexnow_window_hours=cla.POST_INDEXNOW_WINDOW_HOURS_DEFAULT,
            config=None,
        )
        return json.loads(out)

    def test_key_absent_when_no_rotation(self) -> None:
        """No rotation traffic → no rotational_bot_identity key (not null, not {})."""
        agg = cla.Aggregate()
        payload = self._json_payload(agg)
        self.assertNotIn("rotational_bot_identity", payload)

    def test_key_present_when_rotation_detected(self) -> None:
        """Rotation detected → key present, well-formed."""
        entries = [
            _make_entry("5.255.104.83", "Googlebot",  "/.env",        404, second=1),
            _make_entry("5.255.104.83", "GPTBot",     "/.env.bak",    404, second=10),
            _make_entry("5.255.104.83", "ClaudeBot",  "/config.json", 404, second=20),
        ]
        agg = cla.aggregate(
            entries, include_non_bots=False, show_security_probes=False
        )
        cla.apply_rotation_override(agg, window_minutes=5, min_identities=3)
        payload = self._json_payload(agg)
        self.assertIn("rotational_bot_identity", payload)

    def test_json_schema_shape(self) -> None:
        """The JSON object has the documented shape from §2.6 of the brief."""
        entries = [
            _make_entry("5.255.104.83", "Googlebot",  "/.env",        404, second=1),
            _make_entry("5.255.104.83", "GPTBot",     "/.env.bak",    404, second=10),
            _make_entry("5.255.104.83", "ClaudeBot",  "/config.json", 404, second=20),
        ]
        agg = cla.aggregate(
            entries, include_non_bots=False, show_security_probes=False
        )
        cla.apply_rotation_override(agg, window_minutes=5, min_identities=3)
        payload = self._json_payload(agg)
        rb = payload["rotational_bot_identity"]
        # Required scalar fields
        self.assertEqual(rb["total"], 3)
        self.assertEqual(rb["unique_ips"], 1)
        self.assertEqual(rb["rotation_window_minutes"], 5)
        self.assertEqual(rb["min_identities_threshold"], 3)
        # top_ips structure
        self.assertEqual(len(rb["top_ips"]), 1)
        top_ip = rb["top_ips"][0]
        self.assertEqual(top_ip["ip"], "5.255.104.83")
        self.assertEqual(top_ip["requests"], 3)
        self.assertEqual(sorted(top_ip["identities"]),
                         ["ClaudeBot", "GPTBot", "Googlebot"])
        # paths in top_ip should be a sorted list
        self.assertEqual(top_ip["paths"],
                         sorted(top_ip["paths"]))
        # top_identity_combinations structure
        self.assertEqual(len(rb["top_identity_combinations"]), 1)
        combo = rb["top_identity_combinations"][0]
        self.assertEqual(combo["ip_count"], 1)
        self.assertEqual(sorted(combo["identities"]),
                         ["ClaudeBot", "GPTBot", "Googlebot"])
        # top_paths structure
        self.assertEqual(len(rb["top_paths"]), 3)
        for entry in rb["top_paths"]:
            self.assertIn("path", entry)
            self.assertIn("requests", entry)


# ---------------------------------------------------------------------------
# End-to-end against the 2026-05-16 5.255.104.83 pattern
# ---------------------------------------------------------------------------


SYNTHETIC_LOG = """\
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
"""


class TestEndToEndProductionPattern(unittest.TestCase):
    """End-to-end test exercising the full pipeline through main().

    Uses a 14-line synthetic log reproducing the 2026-05-16
    5.255.104.83 rotation pattern plus three legitimate Googlebot
    hits from 66.249.66.10. Verifies that running the analyser
    in JSON format produces the expected attribution and that the
    legitimate Googlebot traffic survives intact.
    """

    def setUp(self) -> None:
        # Write the synthetic log to a tempfile. Cleaned up in tearDown.
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "access.log"
        self.log_path.write_text(SYNTHETIC_LOG, encoding="utf-8")

    def tearDown(self) -> None:
        try:
            self.log_path.unlink()
            os.rmdir(self.tmpdir)
        except OSError:
            pass

    def _run_main_json(self) -> dict:
        """Invoke main() in JSON mode and return the parsed payload."""
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        argv = [str(self.log_path), "--format", "json"]
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            rc = cla.main(argv)
        self.assertEqual(rc, 0, msg=f"main() returned {rc}; "
                                    f"stderr={stderr_buf.getvalue()}")
        return json.loads(stdout_buf.getvalue())

    def test_synthetic_log_produces_expected_attribution(self) -> None:
        payload = self._run_main_json()

        # Rotation key present.
        self.assertIn("rotational_bot_identity", payload)
        rb = payload["rotational_bot_identity"]

        # 11 attacker rotation hits + 1 unique IP.
        self.assertEqual(rb["total"], 11)
        self.assertEqual(rb["unique_ips"], 1)
        # All six identities the attacker claimed.
        top_ip = rb["top_ips"][0]
        self.assertEqual(top_ip["ip"], "5.255.104.83")
        self.assertEqual(top_ip["requests"], 11)
        self.assertEqual(
            sorted(top_ip["identities"]),
            ["BaiduSpider", "Bingbot", "ClaudeBot",
             "GPTBot", "Googlebot", "YandexBot"],
        )

        # The legitimate Googlebot traffic survives unchanged.
        # bot_breakdown[Googlebot] is a flat {status: count} mapping —
        # the three legitimate hits all returned 2xx.
        bb = payload.get("bot_breakdown", {})
        gb = bb.get("Googlebot")
        self.assertIsNotNone(gb, msg="legitimate Googlebot bucket must remain")
        # Total Googlebot requests — exactly the 3 legit hits, no
        # attacker-claimed 404s mixed in.
        self.assertEqual(sum(gb.values()), 3)
        # 404 attribution to real crawlers should be zero — the rotation
        # override stripped the attacker's 404s away. JSON keys are
        # strings, so we look up "404" not 404.
        self.assertEqual(gb.get("404", 0), 0)
        # The rotation classification absorbed all 11 attacker requests.
        rotation_bucket = bb.get(cla.ROTATION_DETECTION_NAME, {})
        self.assertEqual(rotation_bucket.get("404", 0), 11)


# ---------------------------------------------------------------------------
# CLI behaviour and parameter wiring
# ---------------------------------------------------------------------------


class TestTrailingSlashNormalisation(unittest.TestCase):
    """v1.10.1: trailing-slash variants of rotation trigger paths must
    qualify, so attackers hitting both /api/env and /api/env/ in the
    same rotation burst get both variants reattributed.

    Production verification on the 2026-05-16 axilog.io seo log
    showed exactly this gap: 5.255.104.83 hit both /api/env (caught)
    and /api/env/ (slipped through) — the trailing-slash variants
    left two ClaudeBot/BaiduSpider 404s in the real-crawler 404 list
    even though the same IP at the same minute was clearly rotating.
    """

    def test_trailing_slash_variants_qualify(self) -> None:
        """Trailing-slash variants of trigger paths return True."""
        for path in (
            "/api/env/", "/api/config/", "/actuator/env/",
            "/api/secrets/", "/api/admin/", "/api/auth/",
            "/config.json/",  # silly but follows the same rule
        ):
            with self.subTest(path=path):
                self.assertTrue(
                    cla.is_rotation_trigger_path(path),
                    msg=f"{path!r} should be a rotation trigger"
                )

    def test_canonical_paths_still_qualify(self) -> None:
        """No-slash canonical forms continue to qualify (regression guard)."""
        for path in (
            "/api/env", "/api/config", "/actuator/env",
            "/api/auth", "/config.json",
        ):
            with self.subTest(path=path):
                self.assertTrue(cla.is_rotation_trigger_path(path))

    def test_root_path_does_not_qualify(self) -> None:
        """The '/' root path must not be normalised into '' and falsely match.

        rstrip('/') on '/' returns '' — defensive: confirm root is
        rejected. The is_rotation_trigger_path() implementation
        preserves '/' explicitly via a len > 1 guard.
        """
        self.assertFalse(cla.is_rotation_trigger_path("/"))

    def test_empty_path_does_not_qualify(self) -> None:
        """Empty path is rejected at the top of the helper."""
        self.assertFalse(cla.is_rotation_trigger_path(""))

    def test_query_string_stripped_before_normalisation(self) -> None:
        """Probes with cache-busters like /api/env?x=1 still trigger.

        Attackers sometimes append a query string to evade naive
        path-set matchers. The normalisation strips the query string
        before checking, matching the convention used elsewhere in the
        analyser (clean_path in aggregate()).
        """
        self.assertTrue(cla.is_rotation_trigger_path("/api/env?x=1"))
        self.assertTrue(cla.is_rotation_trigger_path("/api/env/?x=1"))

    def test_unrelated_trailing_slash_path_does_not_qualify(self) -> None:
        """Normalisation must not turn /about/ into a trigger.

        /about/ -> /about, neither in trigger set nor probe-matched,
        so must remain False. Guards against the over-normalisation
        failure mode.
        """
        self.assertFalse(cla.is_rotation_trigger_path("/about/"))
        self.assertFalse(cla.is_rotation_trigger_path("/about"))

    def test_end_to_end_trailing_slash_rotation_caught(self) -> None:
        """A rotation burst over trailing-slash variants is fully caught.

        Reproduces the 2026-05-16 axilog.io gap: three trailing-slash
        probes from one IP under three identities. Before v1.10.1 the
        two trigger-only paths (/api/env/, /api/config/) slipped the
        trigger filter and left 404s in the real-crawler 404 list;
        after, all three are reattributed and crawler_404s is empty.

        Note on /actuator/env/: this path IS probe-matched by the
        existing regex set (the /actuator pattern is broad), so it
        never enters crawler_404s to begin with. The test pre-checks
        only the two paths that pre-load into crawler_404s, but
        verifies all three are captured by rotation post-override.
        """
        # Sanity: confirm the probe-match status of each path so a
        # future regex change makes this test fail loudly rather than
        # silently producing the wrong shape.
        self.assertFalse(cla.is_security_probe("/api/env/"))
        self.assertFalse(cla.is_security_probe("/api/config/"))
        self.assertTrue(cla.is_security_probe("/actuator/env/"))

        entries = [
            _make_entry("5.255.104.83", "ClaudeBot",   "/api/env/",     404, second=1),
            _make_entry("5.255.104.83", "BaiduSpider", "/api/config/",  404, second=5),
            _make_entry("5.255.104.83", "YandexBot",   "/actuator/env/", 404, second=9),
        ]
        agg = cla.aggregate(
            entries, include_non_bots=False, show_security_probes=False
        )
        # Before override: the two non-probe paths land in
        # crawler_404s; /actuator/env/ does not (probe-filtered).
        self.assertIn("/api/env/",    agg.crawler_404s)
        self.assertIn("/api/config/", agg.crawler_404s)
        self.assertNotIn("/actuator/env/", agg.crawler_404s)

        cla.apply_rotation_override(agg, window_minutes=5, min_identities=3)

        # After override: all three reattributed; crawler_404s empty.
        self.assertEqual(agg.rotation_count, 3)
        self.assertNotIn("/api/env/",    agg.crawler_404s)
        self.assertNotIn("/api/config/", agg.crawler_404s)
        # Rotation summary captures the slash form verbatim (we
        # normalise for the trigger check, not for storage —
        # operators want to see the exact path the attacker hit).
        self.assertIn("/api/env/",      agg.rotation_path_counts)
        self.assertIn("/api/config/",   agg.rotation_path_counts)
        self.assertIn("/actuator/env/", agg.rotation_path_counts)


class TestBurstCounterDecrement(unittest.TestCase):
    """v1.10.1: rotation entries must be removed from the deploy-window
    burst counters so detect_deploy_anomalies() does not raise a
    false-positive HIGH finding on attacker probe bursts.

    The 2026-05-16 axilog.io production run showed exactly this:
    five rotation 404s from 5.255.104.83 within one minute produced
    `[HIGH] Content-404 burst: 5 404s` even though all five had been
    reattributed to SuspectedBotIdentityRotation. The bursts counter
    was untouched by the override.
    """

    def test_minute_content_404s_decremented_for_non_probe_rotation(self) -> None:
        """Rotation 404s on /api/env-class paths leave minute_content_404s empty."""
        entries = [
            _make_entry("1.1.1.1", "ClaudeBot",   "/api/env",     404, second=1),
            _make_entry("1.1.1.1", "BaiduSpider", "/api/config",  404, second=5),
            _make_entry("1.1.1.1", "YandexBot",   "/api/secrets", 404, second=9),
            _make_entry("1.1.1.1", "Bingbot",     "/api/auth",    404, second=13),
            _make_entry("1.1.1.1", "PerplexityBot", "/api/users", 404, second=17),
        ]
        agg = cla.aggregate(
            entries, include_non_bots=False, show_security_probes=False
        )
        # Before override: all 5 entries hit the burst counter (none
        # are probe-matched, none are framework probes, none are in
        # the suppressed-category set).
        total_before = sum(agg.minute_content_404s.values())
        self.assertEqual(total_before, 5)

        cla.apply_rotation_override(agg, window_minutes=5, min_identities=3)

        # After override: burst counter is empty.
        total_after = sum(agg.minute_content_404s.values())
        self.assertEqual(total_after, 0)
        # And the deploy-window detector sees nothing to flag.
        findings = cla.detect_deploy_anomalies(agg)
        burst_findings = [f for f in findings
                          if "Content-404 burst" in f.message]
        self.assertEqual(burst_findings, [])

    def test_minute_404s_also_decremented(self) -> None:
        """minute_404s (the broader bucket) is also decremented."""
        entries = [
            _make_entry("1.1.1.1", "ClaudeBot",   "/api/env",     404, second=1),
            _make_entry("1.1.1.1", "BaiduSpider", "/api/config",  404, second=5),
            _make_entry("1.1.1.1", "YandexBot",   "/api/secrets", 404, second=9),
        ]
        agg = cla.aggregate(
            entries, include_non_bots=False, show_security_probes=False
        )
        self.assertEqual(sum(agg.minute_404s.values()), 3)
        cla.apply_rotation_override(agg, window_minutes=5, min_identities=3)
        self.assertEqual(sum(agg.minute_404s.values()), 0)

    def test_probe_path_rotation_does_not_double_decrement(self) -> None:
        """Rotation entries on probe-matched paths never entered the
        burst counter in the first place — the override must not
        decrement them.

        /.env is matched by SECURITY_PROBE_PATTERNS, so the
        aggregate() gate (`not e.is_probe`) skips it. If the override
        decremented blindly, the counter would go negative (or, with
        the guard, simply not change but the bookkeeping would be
        confused). Test by mixing probe paths and non-probe paths
        and confirming only the non-probe contributions are stripped.
        """
        entries = [
            # Probe-matched (never entered burst counter)
            _make_entry("1.1.1.1", "Googlebot",  "/.env",       404, second=1),
            _make_entry("1.1.1.1", "GPTBot",     "/.env.bak",   404, second=5),
            # Trigger-only (DID enter burst counter)
            _make_entry("1.1.1.1", "ClaudeBot",  "/api/env",    404, second=9),
            _make_entry("1.1.1.1", "BaiduSpider", "/api/config", 404, second=13),
        ]
        agg = cla.aggregate(
            entries, include_non_bots=False, show_security_probes=False
        )
        # Before override: only the two non-probe entries are in the
        # burst counter.
        self.assertEqual(sum(agg.minute_content_404s.values()), 2)

        cla.apply_rotation_override(agg, window_minutes=5, min_identities=3)

        # After override: burst counter empty (decremented the two
        # that entered, untouched the two that didn't), and rotation
        # captured all four.
        self.assertEqual(sum(agg.minute_content_404s.values()), 0)
        self.assertEqual(agg.rotation_count, 4)
        # Defensive: counter values are never negative.
        for v in agg.minute_content_404s.values():
            self.assertGreaterEqual(v, 0)
        for v in agg.minute_404s.values():
            self.assertGreaterEqual(v, 0)

    def test_partial_burst_legitimate_entries_survive(self) -> None:
        """A real content-404 burst alongside a rotation must remain visible.

        Scenario: three rotation 404s from one attacker IP at minute X,
        and six legitimate Googlebot content 404s at the same minute
        from a deploy-window symptom (different IP). The override
        strips the three attacker entries; the six legitimate
        entries must remain and trigger the burst finding correctly.
        """
        entries = [
            # Attacker rotation
            _make_entry("1.1.1.1", "ClaudeBot",   "/api/env",     404, second=1),
            _make_entry("1.1.1.1", "BaiduSpider", "/api/config",  404, second=5),
            _make_entry("1.1.1.1", "YandexBot",   "/api/secrets", 404, second=9),
            # Legitimate Googlebot 404s (different IP, content paths)
            _make_entry("66.249.66.10", "Googlebot", "/insights/article-a/", 404, second=13),
            _make_entry("66.249.66.10", "Googlebot", "/insights/article-b/", 404, second=17),
            _make_entry("66.249.66.10", "Googlebot", "/insights/article-c/", 404, second=21),
            _make_entry("66.249.66.10", "Googlebot", "/insights/article-d/", 404, second=25),
            _make_entry("66.249.66.10", "Googlebot", "/insights/article-e/", 404, second=29),
            _make_entry("66.249.66.10", "Googlebot", "/insights/article-f/", 404, second=33),
        ]
        agg = cla.aggregate(
            entries, include_non_bots=False, show_security_probes=False
        )
        # Before override: 9 entries in burst counter.
        self.assertEqual(sum(agg.minute_content_404s.values()), 9)

        cla.apply_rotation_override(agg, window_minutes=5, min_identities=3)

        # After: 6 legitimate Googlebot 404s remain.
        self.assertEqual(sum(agg.minute_content_404s.values()), 6)
        # Burst detector correctly flags the legitimate burst.
        findings = cla.detect_deploy_anomalies(agg)
        burst_findings = [f for f in findings
                          if "Content-404 burst" in f.message]
        self.assertEqual(len(burst_findings), 1)
        self.assertEqual(burst_findings[0].severity, "HIGH")


class TestRedirectMigration(unittest.TestCase):
    """v1.10.2: 3xx rotation entries get recorded under their claimed
    named-bot identity in agg.redirects and agg.redirect_attribution
    during aggregation. The override must migrate them to the rotation
    bucket so Redirect Analysis matches Status Code Breakdown.

    Production verification on the 2026-05-16 axilog.io seo log showed
    the leak: three trailing-slash 301s from 5.255.104.83 appeared as
    BaiduSpider/ClaudeBot/PerplexityBot each issuing one redirect, even
    though Status Code Breakdown correctly unified them under
    SuspectedBotIdentityRotation.
    """

    def test_basic_redirect_migration_combined_format(self) -> None:
        """3xx rotation entries move out of named-bot redirects buckets.

        Combined-format inputs have no host/scheme, so the attribution
        path is skipped. The redirects counter must still migrate.
        """
        entries = [
            _make_entry("5.255.104.83", "ClaudeBot",     "/api/env",      301, second=1),
            _make_entry("5.255.104.83", "BaiduSpider",   "/api/config",   301, second=5),
            _make_entry("5.255.104.83", "PerplexityBot", "/actuator/env", 301, second=9),
        ]
        agg = cla.aggregate(
            entries, include_non_bots=False, show_security_probes=False
        )
        # Pre-override: redirects split across three named bots.
        self.assertEqual(agg.redirects["ClaudeBot"]["/api/env"], 1)
        self.assertEqual(agg.redirects["BaiduSpider"]["/api/config"], 1)
        self.assertEqual(agg.redirects["PerplexityBot"]["/actuator/env"], 1)

        cla.apply_rotation_override(agg, window_minutes=5, min_identities=3)

        # Post-override: redirects unified under rotation, named bots gone.
        self.assertNotIn("ClaudeBot",     agg.redirects)
        self.assertNotIn("BaiduSpider",   agg.redirects)
        self.assertNotIn("PerplexityBot", agg.redirects)
        rot = agg.redirects[cla.ROTATION_DETECTION_NAME]
        self.assertEqual(rot["/api/env"],      1)
        self.assertEqual(rot["/api/config"],   1)
        self.assertEqual(rot["/actuator/env"], 1)
        self.assertEqual(sum(rot.values()), 3)

    def test_attribution_migrates_seo_crawl_format(self) -> None:
        """seo_crawl-format input populates redirect_attribution per-cause.

        host='axilog.io' + scheme='https' + no-slash path means the
        v1.3 cause classifier returns 'trailing_slash' (the request
        URL is canonical but nginx is redirecting it for some other
        reason — and trailing_slash is the residual bucket when none
        of the more specific causes apply). Attribution must migrate
        to the rotation bucket alongside the redirects counter.
        """
        entries = [
            _make_entry("5.255.104.83", "ClaudeBot",     "/api/env",      301,
                        second=1, host="axilog.io", scheme="https"),
            _make_entry("5.255.104.83", "BaiduSpider",   "/api/config",   301,
                        second=5, host="axilog.io", scheme="https"),
            _make_entry("5.255.104.83", "PerplexityBot", "/actuator/env", 301,
                        second=9, host="axilog.io", scheme="https"),
        ]
        agg = cla.aggregate(
            entries, include_non_bots=False, show_security_probes=False
        )
        # Pre-override: attribution split across three named bots.
        # Confirm by checking each named bot has a non-empty cause counter.
        for bot, path in (("ClaudeBot", "/api/env"),
                          ("BaiduSpider", "/api/config"),
                          ("PerplexityBot", "/actuator/env")):
            self.assertIn(bot, agg.redirect_attribution)
            self.assertIn(path, agg.redirect_attribution[bot])
            self.assertEqual(sum(agg.redirect_attribution[bot][path].values()), 1)

        cla.apply_rotation_override(agg, window_minutes=5, min_identities=3)

        # Post-override: attribution unified, named-bot entries gone.
        self.assertNotIn("ClaudeBot",     agg.redirect_attribution)
        self.assertNotIn("BaiduSpider",   agg.redirect_attribution)
        self.assertNotIn("PerplexityBot", agg.redirect_attribution)
        rot_attrib = agg.redirect_attribution[cla.ROTATION_DETECTION_NAME]
        for path in ("/api/env", "/api/config", "/actuator/env"):
            self.assertIn(path, rot_attrib)
            self.assertEqual(sum(rot_attrib[path].values()), 1)

    def test_legitimate_googlebot_redirects_preserved(self) -> None:
        """Legitimate Googlebot redirects survive — only rotation entries move.

        Mixed traffic: three rotation 301s from one attacker IP, plus
        a separate Googlebot 301 from a different IP on a legitimate
        path. The Googlebot redirect must remain attributed to
        Googlebot in both agg.redirects and agg.redirect_attribution.
        """
        entries = [
            # Attacker rotation
            _make_entry("5.255.104.83", "ClaudeBot",     "/api/env",      301,
                        second=1, host="axilog.io", scheme="https"),
            _make_entry("5.255.104.83", "BaiduSpider",   "/api/config",   301,
                        second=5, host="axilog.io", scheme="https"),
            _make_entry("5.255.104.83", "PerplexityBot", "/actuator/env", 301,
                        second=9, host="axilog.io", scheme="https"),
            # Legitimate Googlebot www→apex redirect on a real page
            _make_entry("66.249.66.10", "Googlebot", "/", 301,
                        minute=5, second=0, host="www.axilog.io", scheme="https"),
        ]
        agg = cla.aggregate(
            entries, include_non_bots=False, show_security_probes=False
        )

        cla.apply_rotation_override(agg, window_minutes=5, min_identities=3)

        # Googlebot's legitimate redirect survives intact.
        self.assertIn("Googlebot", agg.redirects)
        self.assertEqual(agg.redirects["Googlebot"]["/"], 1)
        # And its attribution survives too.
        self.assertIn("Googlebot", agg.redirect_attribution)
        gb_attrib = agg.redirect_attribution["Googlebot"]["/"]
        self.assertEqual(sum(gb_attrib.values()), 1)
        # The three attacker entries are unified under rotation.
        self.assertEqual(
            sum(agg.redirects[cla.ROTATION_DETECTION_NAME].values()), 3
        )

    def test_status_breakdown_matches_redirect_total(self) -> None:
        """The internal consistency property: total 3xx in bot_status
        for SuspectedBotIdentityRotation must equal the sum of its
        redirects entries.

        This is the property that was violated in v1.10.0 — Status
        Code Breakdown showed SuspectedBotIdentityRotation: 301 × 3
        while Redirect Analysis showed three separate named bots each
        with one redirect. Both views must agree.
        """
        entries = [
            _make_entry("5.255.104.83", "ClaudeBot",     "/api/env",      301,
                        second=1, host="axilog.io", scheme="https"),
            _make_entry("5.255.104.83", "BaiduSpider",   "/api/config",   301,
                        second=5, host="axilog.io", scheme="https"),
            _make_entry("5.255.104.83", "PerplexityBot", "/actuator/env", 301,
                        second=9, host="axilog.io", scheme="https"),
            _make_entry("5.255.104.83", "Bingbot",       "/secrets.json", 404,
                        second=13, host="axilog.io", scheme="https"),
        ]
        agg = cla.aggregate(
            entries, include_non_bots=False, show_security_probes=False
        )
        cla.apply_rotation_override(agg, window_minutes=5, min_identities=3)

        # bot_status 3xx total for rotation
        rot_status = agg.bot_status[cla.ROTATION_DETECTION_NAME]
        rot_3xx = sum(c for s, c in rot_status.items() if 300 <= s < 400)
        # redirects total for rotation
        rot_redirects = sum(agg.redirects[cla.ROTATION_DETECTION_NAME].values())
        self.assertEqual(rot_3xx, rot_redirects, msg=(
            "bot_status 3xx count must match redirects total for "
            "SuspectedBotIdentityRotation — the two views were "
            "inconsistent in v1.10.0/v1.10.1"
        ))
        self.assertEqual(rot_3xx, 3)

    def test_high_traffic_redirect_first_seen_untouched(self) -> None:
        """high_traffic_redirect_first_seen is keyed by path only and
        populated for canonical_loop causes — rotation entries
        produce trailing_slash causes, so the map should never
        contain rotation-path entries to begin with, and the override
        must not touch it.

        Defensive check: a legitimate canonical_loop entry on a
        non-rotation path must survive untouched.
        """
        entries = [
            # Attacker rotation
            _make_entry("5.255.104.83", "ClaudeBot",     "/api/env",      301,
                        second=1, host="axilog.io", scheme="https"),
            _make_entry("5.255.104.83", "BaiduSpider",   "/api/config",   301,
                        second=5, host="axilog.io", scheme="https"),
            _make_entry("5.255.104.83", "PerplexityBot", "/actuator/env", 301,
                        second=9, host="axilog.io", scheme="https"),
            # Legitimate canonical-loop redirect on an unrelated path.
            # apex host + https + path with trailing slash already
            # → canonical_loop cause per _classify_redirect_cause.
            _make_entry("66.249.66.10", "Googlebot", "/images/hero.svg", 301,
                        minute=5, second=0, host="axilog.io", scheme="https"),
        ]
        agg = cla.aggregate(
            entries, include_non_bots=False, show_security_probes=False
        )
        # Pre-override: the legitimate canonical_loop has populated
        # the first-seen map. Rotation paths should NOT appear there
        # because they produce trailing_slash, not canonical_loop.
        self.assertIn("/images/hero.svg", agg.high_traffic_redirect_first_seen)
        for rot_path in ("/api/env", "/api/config", "/actuator/env"):
            self.assertNotIn(rot_path, agg.high_traffic_redirect_first_seen)

        cla.apply_rotation_override(agg, window_minutes=5, min_identities=3)

        # Post-override: legitimate entry survives untouched.
        self.assertIn("/images/hero.svg", agg.high_traffic_redirect_first_seen)


class TestCLIParameters(unittest.TestCase):
    """Verify the three new CLI flags parse and propagate as expected."""

    def test_default_values(self) -> None:
        args = cla.parse_args(["dummy.log"])
        self.assertEqual(args.rotation_window_minutes,
                         cla.ROTATION_WINDOW_MINUTES_DEFAULT)
        self.assertEqual(args.rotation_min_identities,
                         cla.ROTATION_MIN_IDENTITIES_DEFAULT)
        self.assertFalse(args.show_rotation_detail)

    def test_explicit_values(self) -> None:
        args = cla.parse_args([
            "dummy.log",
            "--rotation-window-minutes", "3",
            "--rotation-min-identities", "4",
            "--show-rotation-detail",
        ])
        self.assertEqual(args.rotation_window_minutes, 3)
        self.assertEqual(args.rotation_min_identities, 4)
        self.assertTrue(args.show_rotation_detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
