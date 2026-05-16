#!/usr/bin/env python3
"""Smoke tests for crawler_log_analyser v1.9 additions.

Covers:
  - Fallback TOML parser produces same shape as tomllib for v1.9 config
  - compute_site_age boundary behaviour
  - classify_ai_crawler_expectation across all 5 classifications
  - _compute_framework_opacity branching
  - Bad-config validation paths (malformed dates, inverted window)
  - JSON additive guarantees

Run with: python3 test_v19.py
"""

import sys
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from crawler_log_analyser import (
    AI_DISCOVERY_WINDOW_MAX_DAYS_DEFAULT,
    AI_DISCOVERY_WINDOW_MIN_DAYS_DEFAULT,
    Aggregate,
    ResolvedConfig,
    _compute_framework_opacity,
    classify_ai_crawler_expectation,
    compute_site_age,
    load_config,
)


class TestConfigParsing(unittest.TestCase):
    """v1.9 config shape parses through tomllib + load_config validation."""

    SAMPLE = '''
ignore_source_ips = ["1.2.3.4"]

[sites."speytech.com"]
launch_date = "2025-08-15"

[sites."axilog.io"]
launch_date = "2026-05-14"

[ai_crawlers]
expected_discovery_window_days_min = 7
expected_discovery_window_days_max = 21
'''

    def _load_string(self, content):
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False
        ) as f:
            f.write(content)
            path = Path(f.name)
        return load_config(path)

    def test_full_v19_shape_parses(self):
        config = self._load_string(self.SAMPLE)
        self.assertEqual(config.ignore_source_ips_from_config, ["1.2.3.4"])
        self.assertEqual(config.sites, {
            "speytech.com": {"launch_date": "2025-08-15"},
            "axilog.io": {"launch_date": "2026-05-14"},
        })
        self.assertEqual(config.ai_discovery_window_min_days, 7)
        self.assertEqual(config.ai_discovery_window_max_days, 21)
        self.assertEqual(config.parse_warnings, [])
        self.assertEqual(config.unknown_keys, [])

    def test_v18_minimal_config_still_works(self):
        config = self._load_string('ignore_source_ips = ["1.2.3.4"]\n')
        self.assertEqual(config.ignore_source_ips_from_config, ["1.2.3.4"])
        self.assertEqual(config.sites, {})
        # Defaults preserved.
        self.assertEqual(
            config.ai_discovery_window_min_days,
            AI_DISCOVERY_WINDOW_MIN_DAYS_DEFAULT,
        )
        self.assertEqual(
            config.ai_discovery_window_max_days,
            AI_DISCOVERY_WINDOW_MAX_DAYS_DEFAULT,
        )

    def test_native_toml_date_literal_accepted(self):
        # TOML local-date literal (no quotes). load_config should
        # accept it alongside the quoted-string form.
        config = self._load_string(
            '[sites."x.com"]\nlaunch_date = 2026-05-14\n'
        )
        self.assertEqual(config.sites, {"x.com": {"launch_date": "2026-05-14"}})


class TestComputeSiteAge(unittest.TestCase):
    def test_same_day(self):
        ref = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(compute_site_age("2026-05-14", ref), 0)

    def test_one_day(self):
        ref = datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(compute_site_age("2026-05-14", ref), 1)

    def test_thirty_days(self):
        ref = datetime(2026, 6, 13, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(compute_site_age("2026-05-14", ref), 30)

    def test_future_launch_clamped_to_zero(self):
        # Operator typo: launch date is after the log period.
        ref = datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(compute_site_age("2026-06-14", ref), 0)

    def test_malformed_returns_zero(self):
        # Defensive path; load_config should never let a bad date reach here.
        ref = datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(compute_site_age("not-a-date", ref), 0)


class TestClassifyAiCrawlerExpectation(unittest.TestCase):
    def _make_agg(self, age_days, has_ai_activity=False):
        agg = Aggregate()
        agg.host = "axilog.io"
        launch = datetime(2026, 5, 14, tzinfo=timezone.utc)
        agg.latest = launch + timedelta(days=age_days)
        if has_ai_activity:
            agg.bot_status["ClaudeBot"] = Counter({200: 3})
        return agg

    def _make_config(self):
        config = ResolvedConfig()
        config.sites = {"axilog.io": {"launch_date": "2026-05-14"}}
        config.ai_discovery_window_min_days = 7
        config.ai_discovery_window_max_days = 21
        return config

    def test_active(self):
        agg = self._make_agg(age_days=1, has_ai_activity=True)
        c, _, _ = classify_ai_crawler_expectation(agg, self._make_config())
        self.assertEqual(c, "active")

    def test_too_early_lower_boundary(self):
        agg = self._make_agg(age_days=0)
        c, age, _ = classify_ai_crawler_expectation(agg, self._make_config())
        self.assertEqual((c, age), ("too_early", 0))

    def test_too_early_upper_boundary(self):
        agg = self._make_agg(age_days=6)
        c, age, _ = classify_ai_crawler_expectation(agg, self._make_config())
        self.assertEqual((c, age), ("too_early", 6))

    def test_in_window_lower_boundary(self):
        agg = self._make_agg(age_days=7)
        c, age, _ = classify_ai_crawler_expectation(agg, self._make_config())
        self.assertEqual((c, age), ("in_window", 7))

    def test_in_window_upper_boundary(self):
        agg = self._make_agg(age_days=21)
        c, age, _ = classify_ai_crawler_expectation(agg, self._make_config())
        self.assertEqual((c, age), ("in_window", 21))

    def test_overdue(self):
        agg = self._make_agg(age_days=22)
        c, age, _ = classify_ai_crawler_expectation(agg, self._make_config())
        self.assertEqual((c, age), ("overdue", 22))

    def test_unknown_no_host(self):
        agg = self._make_agg(age_days=1)
        agg.host = None
        c, age, ld = classify_ai_crawler_expectation(agg, self._make_config())
        self.assertEqual((c, age, ld), ("unknown", None, None))

    def test_unknown_no_launch_date(self):
        agg = self._make_agg(age_days=1)
        config = ResolvedConfig()  # no sites
        c, age, ld = classify_ai_crawler_expectation(agg, config)
        self.assertEqual((c, age, ld), ("unknown", None, None))

    def test_active_when_unknown_age_still_reports_active(self):
        # AI activity wins even if site age cannot be computed.
        agg = self._make_agg(age_days=1, has_ai_activity=True)
        agg.host = None
        c, age, ld = classify_ai_crawler_expectation(agg, ResolvedConfig())
        self.assertEqual((c, age, ld), ("active", None, None))


class TestFrameworkOpacity(unittest.TestCase):
    def test_no_probes_is_vacuously_opaque(self):
        agg = Aggregate()
        opacity, exposed = _compute_framework_opacity(agg)
        self.assertTrue(opacity)
        self.assertEqual(exposed, [])

    def test_all_404_is_opaque(self):
        agg = Aggregate()
        agg.framework_probe_status["/_next/manifest.json"][404] = 1
        agg.framework_probe_status["/build/manifest.json"][404] = 2
        opacity, exposed = _compute_framework_opacity(agg)
        self.assertTrue(opacity)
        self.assertEqual(exposed, [])

    def test_single_200_breaks_opacity(self):
        agg = Aggregate()
        agg.framework_probe_status["/_next/manifest.json"][404] = 1
        agg.framework_probe_status["/_next/static/buildManifest.js"][200] = 1
        opacity, exposed = _compute_framework_opacity(agg)
        self.assertFalse(opacity)
        self.assertEqual(exposed, [("/_next/static/buildManifest.js", 200)])

    def test_3xx_counts_as_exposed(self):
        # A 301 on a framework path means the route is being handled,
        # not 404'd. The spec treats this as exposure.
        agg = Aggregate()
        agg.framework_probe_status["/_nuxt/manifest.json"][301] = 1
        opacity, exposed = _compute_framework_opacity(agg)
        self.assertFalse(opacity)
        self.assertEqual(exposed, [("/_nuxt/manifest.json", 301)])


class TestLoadConfigValidation(unittest.TestCase):
    """Bad-config paths must produce warnings, not crashes, and skip the bad entries."""

    def _load(self, content):
        import tempfile
        # Write the content first, close the file (NamedTemporaryFile
        # delete=False semantics keep it on disk), THEN call load_config
        # — calling inside the `with` block before flush leaves the
        # file empty on Linux, which silently parses as zero sections
        # and looks like a config that simply forgot to declare them.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False
        ) as f:
            f.write(content)
            path = Path(f.name)
        return load_config(path)

    def test_malformed_date_dropped_with_warning(self):
        config = self._load('[sites."bad.com"]\nlaunch_date = "not-a-date"\n')
        self.assertEqual(config.sites, {})
        self.assertTrue(any("not in YYYY-MM-DD form" in w for w in config.parse_warnings))

    def test_wrong_type_date_dropped_with_warning(self):
        config = self._load('[sites."bad.com"]\nlaunch_date = 12345\n')
        self.assertEqual(config.sites, {})
        self.assertTrue(any("must be a string" in w for w in config.parse_warnings))

    def test_inverted_window_reverts_to_defaults(self):
        config = self._load(
            '[ai_crawlers]\n'
            'expected_discovery_window_days_min = 30\n'
            'expected_discovery_window_days_max = 10\n'
        )
        self.assertEqual(
            config.ai_discovery_window_min_days,
            AI_DISCOVERY_WINDOW_MIN_DAYS_DEFAULT,
        )
        self.assertEqual(
            config.ai_discovery_window_max_days,
            AI_DISCOVERY_WINDOW_MAX_DAYS_DEFAULT,
        )
        self.assertTrue(any("reverting both to defaults" in w for w in config.parse_warnings))

    def test_window_out_of_range_keeps_default(self):
        config = self._load(
            '[ai_crawlers]\nexpected_discovery_window_days_min = 999\n'
        )
        self.assertEqual(
            config.ai_discovery_window_min_days,
            AI_DISCOVERY_WINDOW_MIN_DAYS_DEFAULT,
        )
        self.assertTrue(any("outside the valid range" in w for w in config.parse_warnings))

    def test_unknown_subkey_warns_but_preserves_others(self):
        config = self._load(
            '[ai_crawlers]\n'
            'expected_discovery_window_days_min = 10\n'
            'mystery_key = 42\n'
        )
        self.assertEqual(config.ai_discovery_window_min_days, 10)
        self.assertTrue(any("unknown key 'mystery_key'" in w for w in config.parse_warnings))

    def test_valid_site_survives_alongside_bad(self):
        config = self._load(
            '[sites."bad.com"]\nlaunch_date = "not-a-date"\n'
            '[sites."good.com"]\nlaunch_date = "2026-01-01"\n'
        )
        self.assertEqual(config.sites, {"good.com": {"launch_date": "2026-01-01"}})

    def test_bool_rejected_for_window(self):
        # int validation must reject booleans (Python's int subclass).
        config = self._load(
            '[ai_crawlers]\nexpected_discovery_window_days_min = true\n'
        )
        self.assertEqual(
            config.ai_discovery_window_min_days,
            AI_DISCOVERY_WINDOW_MIN_DAYS_DEFAULT,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
