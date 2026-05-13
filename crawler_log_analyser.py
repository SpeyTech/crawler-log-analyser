#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nginx Crawler Log Analyser for SpeyTech.

Extracts SEO/crawler intelligence from nginx access logs.

Primary goal: operational SEO observability after the atomic deploy fix.

Supports both the standard nginx combined log format and the extended
seo_crawl format (with $host, $scheme, $request_time, $upstream_response_time).
Format is auto-detected per file; use --log-format to override.

Usage:
    python3 crawler_log_analyser.py /var/log/nginx/access.log
    python3 crawler_log_analyser.py /var/log/nginx/access.log* --format markdown --output report.md
    python3 crawler_log_analyser.py /var/log/nginx/access.log --bot googlebot --verify-googlebot
    python3 crawler_log_analyser.py /var/log/nginx/speytech.com.seo.log

No external dependencies required.

Version: 1.6.0
Author: William Murray, SpeyTech
"""

from __future__ import annotations

__version__ = "1.6.0"

import argparse
import gzip
import io
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from glob import glob
from pathlib import Path
from typing import Iterable, Iterator

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Bot classification rules. Order matters: more specific patterns first.
# Each tuple is (display_name, regex_pattern, is_ai_crawler).
# Bot user-agent patterns, evaluated in order. First match wins, so more
# specific patterns must come before more general ones (Googlebot-Image
# before Googlebot, named bots before the GenericBot catch-all).
#
# The third tuple field, is_ai, controls whether the bot is counted in the
# AI Crawler Report and weighted in the search-crawler health score. The
# distinction is operational: AI crawlers signal LLM-driven discovery and
# warrant separate visibility from traditional search-index crawlers.
#
# When adding new patterns, run them against a recent production log via
# the seo_crawl fixture in v1.4-ticket.md before committing. Empty-UA
# requests short-circuit before this list and are handled separately.
BOT_PATTERNS: list[tuple[str, str, bool]] = [
    ("Googlebot-Image",     r"Googlebot-Image",                    False),
    ("Googlebot",           r"Googlebot",                          False),
    ("Bingbot",             r"bingbot",                            False),
    ("Applebot",            r"Applebot",                           True),
    ("OAI-SearchBot",       r"OAI-SearchBot",                      True),
    ("ChatGPT-User",        r"ChatGPT-User",                       True),
    ("GPTBot",              r"GPTBot",                             True),
    ("Claude-SearchBot",    r"Claude-SearchBot",                   True),
    ("Claude-User",         r"Claude-User",                        True),
    ("ClaudeBot",           r"ClaudeBot|anthropic-ai",             True),
    ("PerplexityBot",       r"PerplexityBot",                      True),
    ("MistralBot",          r"MistralBot",                         True),
    ("YandexBot",           r"YandexBot",                          False),
    ("Bytespider",          r"Bytespider",                         False),
    ("AhrefsBot",           r"AhrefsBot",                          False),
    ("SemrushBot",          r"SemrushBot",                         False),
    ("MJ12bot",             r"MJ12bot",                            False),
    ("FacebookExternalHit", r"facebookexternalhit|meta-externalagent", False),
    ("DuckDuckBot",         r"DuckDuckBot",                        False),
    ("BaiduSpider",         r"Baiduspider",                        False),
    ("LinkedInBot",         r"LinkedInBot",                        False),
    ("TwitterBot",          r"Twitterbot",                         False),
    ("Amazonbot",           r"Amazonbot",                          True),
    ("PetalBot",            r"PetalBot",                           False),
    ("SleepBot",            r"SleepBot",                           True),
    ("TikTokSpider",        r"TikTokSpider",                       True),
    # RSS readers. Neither search nor AI; they represent direct
    # subscriber-driven polling. Worth counting but not weighted in the
    # AI crawler report or search-crawler health score (is_ai=False).
    ("News Explorer",       r"News Explorer",                      False),
    ("Feedly",              r"Feedly",                             False),
    # Generic catch-alls last.
    ("GenericBot",          r"\bbot\b|crawler|spider|slurp",       False),
]

# AI crawler set, derived from BOT_PATTERNS for downstream reporting.
AI_CRAWLERS: set[str] = {name for name, _, is_ai in BOT_PATTERNS if is_ai}

# Special SEO files we want to track explicitly.
SPECIAL_FILES: list[str] = [
    "/robots.txt",
    "/sitemap-index.xml",
    "/sitemap-0.xml",
    "/sitemap.xml",
    "/rss.xml",
    "/llms.txt",
    "/llms-full.txt",
]

# v1.6 canonical-loop severity thresholds. See _redirect_note() for the
# full rationale. Calibrated empirically from 2026-05-11..05-13 production
# data which showed working redirects routinely hitting counts of 11-22
# from Googlebot revalidation after IndexNow pings.
CANONICAL_LOOP_HIGH_TRAFFIC_THRESHOLD = 3   # >= this count: annotate as "high-traffic redirect"
CANONICAL_LOOP_CRITICAL_THRESHOLD = 50      # >= this count: flag CRITICAL

# v1.6 AI crawler depth-preference threshold. Used by the new
# render_ai_crawler_depth_preference() subsection. A crawler's "full-content
# ratio" is full_count / (full_count + index_count); 1.0 means it has only
# ever fetched llms-full.txt, 0.0 means it has only ever fetched llms.txt.
# "Exclusive" is determined by the other depth's count being zero (no
# threshold needed); the ratio threshold separates "prefers" from "mixed".
AI_DEPTH_PREFERENCE_RATIO = 0.75

# URL category classifiers, evaluated top to bottom.
URL_CATEGORIES: list[tuple[str, re.Pattern[str]]] = [
    ("homepage",                re.compile(r"^/$")),
    ("robots",                  re.compile(r"^/robots\.txt$")),
    ("sitemap",                 re.compile(r"^/sitemap[\w\-]*\.xml$")),
    ("rss",                     re.compile(r"^/rss\.xml$")),
    ("llms",                    re.compile(r"^/llms(-full)?\.txt$")),
    ("insights article",        re.compile(r"^/insights/[^/]+/?$")),
    ("AI architecture article", re.compile(r"^/ai-architecture/[^/]+/?$")),
    ("open-source page",        re.compile(r"^/open-source(/[^/]+)?/?$")),
    ("product page",            re.compile(r"^/(mdcp|mdlce|cardiocore)/?$")),
    ("contact page",            re.compile(r"^/contact/?$")),
    ("static asset",            re.compile(r"\.(css|js|svg|png|jpg|jpeg|gif|webp|ico|woff2?|ttf|map)(\?.*)?$")),
    ("API",                     re.compile(r"^/api/")),
    ("utility/demo page",       re.compile(r"^/(demo|test|tools)/")),
]

# Severity ordering (lowest to highest) for sorting.
SEVERITY_RANK = {"OK": 0, "INFO": 1, "WARNING": 2, "HIGH": 3, "CRITICAL": 4}

# Nginx combined-format parser.
#
# Two-stage approach: a permissive skeleton regex extracts the structural
# fields (ip, timestamp, request, status, size, referrer, ua), and the
# request line is parsed separately. This tolerates IPv6 addresses, HTTP/2.0,
# arbitrary methods, "-" or empty request lines, and binary garbage in paths.
LOG_RE = re.compile(
    r'^(?P<ip>\S+)\s+'
    r'\S+\s+\S+\s+'                                         # remote_user, ident
    r'\[(?P<ts>[^\]]+)\]\s+'                                # timestamp
    r'"(?P<request>(?:[^"\\]|\\.)*)"\s+'                    # request line (any content, escapes ok)
    r'(?P<status>\d{3})\s+'                                 # status
    r'(?P<size>\d+|-)\s+'                                   # size
    r'"(?P<referrer>(?:[^"\\]|\\.)*)"\s+'                   # referrer
    r'"(?P<ua>(?:[^"\\]|\\.)*)"'                            # user agent
)

# Nginx seo_crawl format parser. Adds $host, $scheme, $request_time, and
# $upstream_response_time to the combined layout. Same two-stage strategy
# as LOG_RE: skeleton match here, request line parsed by REQUEST_RE below.
#
# Recommended nginx definition:
#     log_format seo_crawl '$remote_addr $host $scheme - [$time_local] '
#                          '"$request" $status $body_bytes_sent '
#                          '"$http_referer" "$http_user_agent" '
#                          '$request_time $upstream_response_time';
LOG_RE_SEO = re.compile(
    r'^(?P<ip>\S+)\s+'
    r'(?P<host>\S+)\s+'                                     # $host
    r'(?P<scheme>https?)\s+'                                # $scheme
    r'\S+\s+'                                               # remote_user placeholder (-)
    r'\[(?P<ts>[^\]]+)\]\s+'                                # timestamp
    r'"(?P<request>(?:[^"\\]|\\.)*)"\s+'                    # request line
    r'(?P<status>\d{3})\s+'                                 # status
    r'(?P<size>\d+|-)\s+'                                   # size
    r'"(?P<referrer>(?:[^"\\]|\\.)*)"\s+'                   # referrer
    r'"(?P<ua>(?:[^"\\]|\\.)*)"\s+'                         # user agent
    r'(?P<request_time>[\d.]+|-)\s+'                        # $request_time
    r'(?P<upstream_time>[\d.]+|-)'                          # $upstream_response_time
)

# Request line: METHOD PATH PROTOCOL — but any of the three can be missing
# or malformed. We accept anything for the method (alphanumeric upper-case
# tokens), and the protocol is optional.
REQUEST_RE = re.compile(
    r'^(?P<method>[A-Z]+)\s+(?P<path>\S+)(?:\s+(?P<proto>HTTP/[\d.]+))?\s*$'
)

TS_FORMAT = "%d/%b/%Y:%H:%M:%S %z"

# Sentinel values used by the format auto-detector and CLI.
LOG_FORMAT_COMBINED = "combined"
LOG_FORMAT_SEO = "seo-crawl"
LOG_FORMAT_AUTO = "auto"


def _parse_time_field(val: str) -> float | None:
    """Parse $request_time or $upstream_response_time.

    Returns None for nginx's '-' sentinel (e.g., $upstream_response_time
    on static-file requests with no upstream involvement) and for any
    malformed value. Returns 0.0 only when the log explicitly recorded
    a sub-millisecond response.

    The distinction between None and 0.0 matters for downstream statistics:
      - None values must be excluded from percentile calculations
      - 0.0 values are real data points and must be included
    """
    if val == "-":
        return None
    try:
        return float(val)
    except ValueError:
        return None


# Hostname heuristic for auto-detection: a label-dot-label-… pattern with no
# whitespace. Deliberately permissive — the auto-detector only needs to tell
# this apart from the literal '-' that combined logs put in field 2.
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9\-\.]*[A-Za-z0-9])?$")


def detect_log_format(line: str) -> str | None:
    """Identify the log format of a single sample line.

    Returns "combined", "seo-crawl", or None if neither is a clean match.
    Per spec §3.1: combined has '-' as field 2 (remote_user) AND '-' as
    field 3 (ident). seo_crawl has a hostname as field 2 and 'http'/'https'
    as field 3.
    """
    tokens = line.split(None, 4)
    if len(tokens) < 4:
        return None
    field2 = tokens[1]
    field3 = tokens[2]
    if field2 == "-" and field3 == "-":
        return LOG_FORMAT_COMBINED
    if field3 in ("http", "https") and _HOSTNAME_RE.match(field2):
        return LOG_FORMAT_SEO
    return None

# Patterns that identify exploit-probe traffic. These are paths that could
# only ever be a probe against this static Astro site — there is no PHP,
# no WordPress, no /admin endpoint. Suppressed from the SEO 404 report
# unless --show-security-probes is set.
SECURITY_PROBE_PATTERNS = [
    re.compile(r"\.php(\?|$)", re.IGNORECASE),
    re.compile(r"/wp-(admin|login|content|includes)", re.IGNORECASE),
    re.compile(r"/wordpress/", re.IGNORECASE),
    re.compile(r"/xmlrpc", re.IGNORECASE),
    re.compile(r"/\.(env|git|svn|aws|ssh|htaccess|htpasswd)", re.IGNORECASE),
    re.compile(r"/admin(er)?(\.|/|$)", re.IGNORECASE),
    re.compile(r"/phpmyadmin", re.IGNORECASE),
    re.compile(r"/(shell|backup|config|setup|install)\.(php|asp|aspx|jsp)", re.IGNORECASE),
    re.compile(r"/cgi-bin/", re.IGNORECASE),
    re.compile(r"\.(asp|aspx|jsp|cgi)(\?|$)", re.IGNORECASE),
    re.compile(r"/owa/", re.IGNORECASE),
    re.compile(r"/(boaform|HNAP1|GponForm)", re.IGNORECASE),
    re.compile(r"/manager/html", re.IGNORECASE),
    re.compile(r"/solr/", re.IGNORECASE),
    re.compile(r"/struts2", re.IGNORECASE),
    re.compile(r"/actuator", re.IGNORECASE),
    re.compile(r"\\x[0-9a-f]{2}", re.IGNORECASE),  # binary noise
]


def is_security_probe(path: str) -> bool:
    return any(p.search(path) for p in SECURITY_PROBE_PATTERNS)


# Framework fingerprint probes. Bots probe for these to identify what
# generator built the site (Next.js, Nuxt, webpack, etc). They are not
# exploit attempts, but they ARE 404s on a static Astro site, and they
# would otherwise false-positive the deploy-window content-404 burst
# detector. Tracked separately from security probes so honest reporting
# is preserved.
FRAMEWORK_PROBE_PATTERNS = [
    re.compile(r"^/_next/", re.IGNORECASE),
    re.compile(r"^/_nuxt/", re.IGNORECASE),
    re.compile(r"buildManifest\.js$", re.IGNORECASE),
    re.compile(r"manifest\.json$", re.IGNORECASE),
    re.compile(r"webpack-stats\.json$", re.IGNORECASE),
    re.compile(r"^/stats\.json$", re.IGNORECASE),
    re.compile(r"^/dist/", re.IGNORECASE),
    re.compile(r"^/build/", re.IGNORECASE),
    re.compile(r"^/\.well-known/(?!security\.txt$)", re.IGNORECASE),
]


def is_framework_probe(path: str) -> bool:
    return any(p.search(path) for p in FRAMEWORK_PROBE_PATTERNS)


# Real search crawlers — used for the broader "search crawler health" score.
# Excludes UnknownBot, GenericBot, social-preview bots, AI crawlers.
SEARCH_CRAWLERS: set[str] = {
    "Googlebot", "Googlebot-Image", "Bingbot", "DuckDuckBot",
    "YandexBot", "BaiduSpider",
}

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class LogEntry:
    ip: str
    ts: datetime
    method: str
    path: str
    status: int
    size: int
    referrer: str
    ua: str
    bot: str
    is_ai: bool
    category: str
    is_probe: bool
    is_framework: bool
    # The following four fields are populated only when the entry came from
    # a seo_crawl-format log. Combined-format entries leave them as None,
    # which preserves backwards compatibility for every consumer that does
    # not know about them.
    host: str | None = None
    scheme: str | None = None
    request_time: float | None = None
    upstream_time: float | None = None

    @property
    def is_bot(self) -> bool:
        return self.bot != "Human/Other"


@dataclass
class ParseStats:
    total_lines: int = 0
    parsed: int = 0
    malformed: int = 0
    out_of_range: int = 0
    malformed_samples: list[str] = field(default_factory=list)
    malformed_sample_limit: int = 10
    # v1.3: per-file format-detection failures (spec §3.2). Incremented
    # when auto-detection cannot identify the format of a file after
    # sampling 100 non-empty lines. The file is skipped; main() uses this
    # counter to decide whether to exit 2 when every input failed.
    detection_failures: int = 0
    files_attempted: int = 0

    def record_malformed(self, line: str) -> None:
        self.malformed += 1
        if len(self.malformed_samples) < self.malformed_sample_limit:
            # Truncate very long lines; keep enough to diagnose the shape.
            sample = line if len(line) <= 200 else line[:200] + "…"
            self.malformed_samples.append(sample)


@dataclass
class Finding:
    severity: str
    message: str

    def __lt__(self, other: "Finding") -> bool:
        return (
            -SEVERITY_RANK.get(self.severity, 0),
            self.message,
        ) < (
            -SEVERITY_RANK.get(other.severity, 0),
            other.message,
        )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def open_log(path: Path) -> io.TextIOBase:
    """Open a plain or gzipped log file as text."""
    if str(path).endswith(".gz"):
        return io.TextIOWrapper(
            gzip.open(path, "rb"), encoding="utf-8", errors="replace"
        )
    return open(path, "r", encoding="utf-8", errors="replace")


def classify_bot(ua: str) -> tuple[str, bool]:
    """Return (bot_family, is_ai_crawler) for a user-agent string."""
    if not ua or ua == "-":
        # v1.4: route empty-UA traffic to its own category rather than
        # UnknownBot. Empty UAs are almost always either misconfigured
        # scanners or exploit probes; the path-based is_security_probe
        # detector will catch the exploit case downstream. Reporting
        # these as "Empty-UA" rather than "UnknownBot" prevents the
        # bot-breakdown from suggesting a fleet of mystery crawlers
        # when the operational reality is usually one scanner IP.
        return ("Empty-UA", False)
    for name, pattern, is_ai in BOT_PATTERNS:
        if re.search(pattern, ua, re.IGNORECASE):
            return (name, is_ai)
    return ("Human/Other", False)


def classify_url(path: str) -> str:
    """Bucket a path into a coarse category for section-level reporting."""
    # Strip query string for classification.
    clean = path.split("?", 1)[0]
    for category, pattern in URL_CATEGORIES:
        if pattern.search(clean):
            return category
    return "unknown"


def parse_line(line: str, log_format: str = LOG_FORMAT_COMBINED) -> LogEntry | None:
    """Parse a single nginx access log line. Returns None on failure.

    Two-stage: a permissive skeleton match extracts structural fields, and
    the request line is parsed separately so unusual methods, missing
    protocols, empty requests, and binary noise inside paths don't reject
    an otherwise valid line.

    log_format selects which outer regex to use. The combined-format code
    path is unchanged from v1.2; seo_crawl additionally populates host,
    scheme, request_time, and upstream_time on the returned LogEntry.
    """
    if log_format == LOG_FORMAT_SEO:
        m = LOG_RE_SEO.match(line)
    else:
        m = LOG_RE.match(line)
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group("ts"), TS_FORMAT)
    except ValueError:
        return None
    try:
        status = int(m.group("status"))
    except ValueError:
        return None
    size_raw = m.group("size")
    size = int(size_raw) if size_raw.isdigit() else 0

    request = m.group("request")
    method = "UNKNOWN"
    path = ""
    if request and request != "-":
        rm = REQUEST_RE.match(request)
        if rm:
            method = rm.group("method")
            path = rm.group("path")
        else:
            # Best-effort: take the first whitespace-separated token as method
            # if it looks like one, otherwise tag the whole request as the path
            # so downstream classification still sees something.
            tokens = request.split(None, 2)
            if tokens and re.fullmatch(r"[A-Z]+", tokens[0]):
                method = tokens[0]
                path = tokens[1] if len(tokens) > 1 else ""
            else:
                path = request

    ua = m.group("ua")
    bot, is_ai = classify_bot(ua)
    category = classify_url(path)
    probe = is_security_probe(path) if path else False
    framework = is_framework_probe(path) if path else False

    host: str | None = None
    scheme: str | None = None
    request_time: float | None = None
    upstream_time: float | None = None
    if log_format == LOG_FORMAT_SEO:
        host = m.group("host")
        scheme = m.group("scheme")
        request_time = _parse_time_field(m.group("request_time"))
        upstream_time = _parse_time_field(m.group("upstream_time"))

    return LogEntry(
        ip=m.group("ip"),
        ts=ts,
        method=method,
        path=path,
        status=status,
        size=size,
        referrer=m.group("referrer"),
        ua=ua,
        bot=bot,
        is_ai=is_ai,
        category=category,
        is_probe=probe,
        is_framework=framework,
        host=host,
        scheme=scheme,
        request_time=request_time,
        upstream_time=upstream_time,
    )


def _detect_format_for_file(
    path: Path,
    forced_format: str,
    stats: ParseStats,
) -> str | None:
    """Determine the log format for a single file.

    If forced_format is anything other than 'auto', it's returned as-is.
    Otherwise, scan up to the first 100 non-empty lines and return the
    format identified by the first one that detects cleanly. Returns None
    if every line in that window failed both heuristics — caller must
    treat that as a fatal error per spec §3.2.

    Note: malformed lines encountered while sampling are NOT recorded
    against ParseStats here; they will be re-read by iter_entries and
    counted there. This avoids double-counting when the file is reopened
    for the actual parsing pass.
    """
    if forced_format != LOG_FORMAT_AUTO:
        return forced_format

    try:
        f = open_log(path)
    except OSError:
        # Caller will see the same OSError when it tries to parse for real;
        # let it handle the diagnostic.
        return None

    sample_line: str | None = None
    with f:
        seen = 0
        for raw in f:
            line = raw.rstrip("\n")
            if not line:
                continue
            seen += 1
            if sample_line is None:
                sample_line = line
            detected = detect_log_format(line)
            if detected is not None:
                return detected
            if seen >= 100:
                break

    if sample_line is None:
        # Empty file. Treat as combined by convention — the actual parsing
        # pass will produce zero entries either way, and this keeps the
        # error path reserved for genuinely undetectable content.
        return LOG_FORMAT_COMBINED

    # 100 non-empty lines, none detectable. Per spec §3.2.
    print(
        f"error: cannot determine log format for {path}\n"
        f"       try --log-format combined or --log-format seo-crawl explicitly\n"
        f"       (first malformed line: {sample_line[:200]})",
        file=sys.stderr,
    )
    return None


def iter_entries(
    paths: list[Path],
    date_from: datetime | None,
    date_to: datetime | None,
    stats: ParseStats,
    log_format: str = LOG_FORMAT_AUTO,
) -> Iterator[LogEntry]:
    """Stream parsed entries from one or more log files, applying date filters.

    Format is detected once per file (or taken from log_format if not 'auto')
    and then locked in for that file's parse pass. A per-file diagnostic is
    emitted to stderr noting which format was used, per spec §2.4.
    """
    for p in paths:
        stats.files_attempted += 1
        detected = _detect_format_for_file(p, log_format, stats)
        if detected is None:
            # Detection failed and the diagnostic was already emitted by
            # _detect_format_for_file. Skip this file and continue with
            # any others. main() will exit 2 if every file failed.
            stats.detection_failures += 1
            continue

        if log_format == LOG_FORMAT_AUTO:
            print(f"info: {p}: detected log format '{detected}'", file=sys.stderr)

        try:
            f = open_log(p)
        except OSError as e:
            print(f"warning: cannot open {p}: {e}", file=sys.stderr)
            continue
        with f:
            for raw in f:
                stats.total_lines += 1
                line = raw.rstrip("\n")
                if not line:
                    continue
                entry = parse_line(line, detected)
                if entry is None:
                    stats.record_malformed(line)
                    continue
                if date_from and entry.ts < date_from:
                    stats.out_of_range += 1
                    continue
                if date_to and entry.ts >= date_to:
                    stats.out_of_range += 1
                    continue
                stats.parsed += 1
                yield entry


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass
class Aggregate:
    """Holds all derived stats from the log scan."""
    stats: ParseStats = field(default_factory=ParseStats)
    earliest: datetime | None = None
    latest: datetime | None = None

    # Per-bot status code totals: bot -> Counter[status]
    bot_status: dict[str, Counter[int]] = field(default_factory=lambda: defaultdict(Counter))

    # Per-bot URL totals: bot -> Counter[path]
    bot_urls: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))

    # Per-bot per-URL status: bot -> path -> Counter[status]
    bot_url_status: dict[str, dict[str, Counter[int]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(Counter))
    )

    # Special files: file -> bot -> Counter[status]
    special_files: dict[str, dict[str, Counter[int]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(Counter))
    )

    # Googlebot timeline: hour bucket -> count
    googlebot_hours: Counter[datetime] = field(default_factory=Counter)
    googlebot_timestamps: list[datetime] = field(default_factory=list)

    # Crawler 404 records: path -> bot -> count
    crawler_404s: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    # Sample 404 detail (one per path/bot pair) for the report.
    crawler_404_samples: dict[tuple[str, str], LogEntry] = field(default_factory=dict)

    # Redirects: bot -> Counter[path]
    redirects: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))

    # Section-level Googlebot crawl: category -> count
    googlebot_sections: Counter[str] = field(default_factory=Counter)

    # IPs per bot family (for verification feature).
    bot_ips: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    # Security probe summary (suppressed from main report by default).
    probe_count: int = 0
    probe_paths: Counter[str] = field(default_factory=Counter)
    probe_ips: Counter[str] = field(default_factory=Counter)

    # Framework fingerprint probe summary (Next/Nuxt/webpack/etc).
    framework_probe_count: int = 0
    framework_probe_paths: Counter[str] = field(default_factory=Counter)

    # Bad-request (400) noise: malformed exploit/probe traffic that lands
    # before any URL routing happens, so it skews UnknownBot totals.
    bad_request_count: int = 0
    bad_request_ips: Counter[str] = field(default_factory=Counter)
    bad_request_bots: Counter[str] = field(default_factory=Counter)

    # Per-minute 404 buckets for deploy-window anomaly detection.
    minute_404s: Counter[datetime] = field(default_factory=Counter)
    # Same buckets, but only for content URLs (not probes, not framework
    # probes, not special files, not static assets).
    minute_content_404s: Counter[datetime] = field(default_factory=Counter)
    # Per-bot, per-minute robots.txt failures so anomaly severity can match
    # the Google-vs-non-Google rule from spec §6.4.
    robots_failure_minutes: dict[str, Counter[datetime]] = field(
        default_factory=lambda: defaultdict(Counter)
    )

    # --- seo_crawl-derived fields (v1.3) -----------------------------------
    # All four are only populated when at least one source file was parsed
    # in seo_crawl format. Combined-format-only runs leave them empty and
    # the corresponding report sections / JSON keys are suppressed.

    # True once any entry with non-None request_time has been observed.
    # Used to gate the Response Latency section and latency-based health
    # deductions. Distinct from "all values were 0.0" (still data) and from
    # "every entry was combined-format" (no data, suppress section).
    has_latency_data: bool = False

    # All non-None request_time samples across every entry. Feeds the
    # overall percentile statistics in §5.2.
    all_request_times: list[float] = field(default_factory=list)

    # Per-bot non-None request_time samples. Feeds the per-bot medians.
    request_times_by_bot: dict[str, list[float]] = field(
        default_factory=lambda: defaultdict(list)
    )

    # Top-N slowest entries by request_time. Kept as a simple list and
    # trimmed at report time — N is small (10) and the overhead of a heap
    # is not worth the code complexity for this workload.
    slowest_entries: list[LogEntry] = field(default_factory=list)

    # Per-bot, per-path redirect attribution. For each (bot, path) pair,
    # count observed origin signatures so the attribution numbers
    # reconcile with the per-bot redirect count next to them. Origins:
    #   - "http_to_https": scheme=http and the target was https
    #   - "www_to_apex":   host started with "www." and the apex was likely the target
    #   - "trailing_slash": path didn't end with "/" (best-effort, host/scheme alone don't prove it)
    #   - "canonical_loop": the request was already on the canonical host/scheme
    #                       (a config bug — flagged CRITICAL in §5.1)
    #   - "other":          fallback bucket so totals reconcile with §5.1
    #
    # Structure: redirect_attribution[bot][path] = Counter({cause: count}).
    # v1.4.0 keyed this by path alone, which caused per-bot attribution
    # rows to display the aggregate cause-count across all bots — a real
    # bug (e.g. "6 × /" labelled "(5 HTTP→HTTPS, 4 www→apex)" because
    # the 9 was the site-wide count, not Googlebot's portion). v1.4.1
    # fixes this by keying attribution per-bot so each row's numbers
    # match the redirect count alongside them.
    redirect_attribution: dict[str, dict[str, Counter[str]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(Counter))
    )


def aggregate(entries: Iterable[LogEntry], include_non_bots: bool,
              show_security_probes: bool,
              ignored_probe_ips: frozenset[str] = frozenset()) -> Aggregate:
    """Walk parsed entries and build the Aggregate snapshot.

    Security-probe traffic (PHP/WordPress/etc on a static Astro site) is
    summarised separately and suppressed from the main 404 list unless
    show_security_probes is True.

    ignored_probe_ips (v1.5): source IPs whose probe-classified requests
    are excluded from probe_count, probe_paths, and probe_ips. Used to
    filter operator self-test traffic (e.g. curl tests from the host VM)
    which otherwise inflates the probe-noise tally with internal traffic.
    Bot statistics and health scores are unaffected — the ignored IPs
    still contribute to every other aggregation, so the only visible
    effect is a cleaner "Suppressed Security Probe Noise" section.
    """
    agg = Aggregate()

    for e in entries:
        # Track date range across everything we parse.
        if agg.earliest is None or e.ts < agg.earliest:
            agg.earliest = e.ts
        if agg.latest is None or e.ts > agg.latest:
            agg.latest = e.ts

        # Always track probe summary, regardless of suppression flag.
        # v1.5: skip probe tracking for ignored source IPs (e.g. operator
        # self-tests from the host VM). The entry still flows through to
        # all other classifications below — only the probe-noise tally
        # excludes these IPs.
        if e.is_probe and e.ip not in ignored_probe_ips:
            agg.probe_count += 1
            agg.probe_paths[e.path] += 1
            agg.probe_ips[e.ip] += 1
        if e.is_framework:
            agg.framework_probe_count += 1
            agg.framework_probe_paths[e.path] += 1

        # Bad-request (400) traffic: malformed bytes hitting nginx before
        # routing. Tracked separately so it doesn't skew "real" totals.
        if e.status == 400:
            agg.bad_request_count += 1
            agg.bad_request_ips[e.ip] += 1
            agg.bad_request_bots[e.bot] += 1

        if not include_non_bots and not e.is_bot:
            continue

        agg.bot_status[e.bot][e.status] += 1
        agg.bot_urls[e.bot][e.path] += 1
        agg.bot_url_status[e.bot][e.path][e.status] += 1
        agg.bot_ips[e.bot].add(e.ip)

        clean_path = e.path.split("?", 1)[0]
        if clean_path in SPECIAL_FILES:
            agg.special_files[clean_path][e.bot][e.status] += 1

        if e.bot == "Googlebot":
            hour_bucket = e.ts.replace(minute=0, second=0, microsecond=0)
            agg.googlebot_hours[hour_bucket] += 1
            agg.googlebot_timestamps.append(e.ts)
            agg.googlebot_sections[e.category] += 1

        # 404 handling: suppress probes from the main 404 list by default.
        # Framework probes are always suppressed from the main 404 list — they
        # are not actionable SEO findings.
        if e.status == 404 and e.is_bot:
            include_in_404_list = (
                (show_security_probes or not e.is_probe)
                and not e.is_framework
            )
            if include_in_404_list:
                agg.crawler_404s[e.path][e.bot] += 1
                key = (e.path, e.bot)
                if key not in agg.crawler_404_samples:
                    agg.crawler_404_samples[key] = e

        if e.status in (301, 302, 307, 308):
            agg.redirects[e.bot][e.path] += 1
            # v1.3: concrete redirect attribution when seo_crawl data is
            # present. Derived from the redirect request's own host/scheme,
            # not from any inferred target — the target is known to the
            # downstream client, not to us, but the cause is observable in
            # the source URL.
            if e.host is not None and e.scheme is not None:
                cause = _classify_redirect_cause(e.scheme, e.host, e.path)
                agg.redirect_attribution[e.bot][e.path][cause] += 1

        # v1.3: latency tracking from seo_crawl format.
        if e.request_time is not None:
            agg.has_latency_data = True
            agg.all_request_times.append(e.request_time)
            agg.request_times_by_bot[e.bot].append(e.request_time)
            # Keep the slowest-N list bounded. We use a simple "append +
            # trim when oversize" strategy because N is small (10 for the
            # report) and the buffer cap is 50 — generous enough to absorb
            # ordering noise without making this loop quadratic.
            agg.slowest_entries.append(e)
            if len(agg.slowest_entries) > 50:
                agg.slowest_entries.sort(key=lambda x: x.request_time or 0.0, reverse=True)
                del agg.slowest_entries[50:]

        # Anomaly buckets: minute-resolution.
        # Exclude security probes AND framework probes from the burst counter
        # so a Next.js fingerprint sweep doesn't false-positive a deploy issue.
        minute_bucket = e.ts.replace(second=0, microsecond=0)
        if e.status == 404 and not e.is_probe and not e.is_framework:
            agg.minute_404s[minute_bucket] += 1
            if e.category not in {"robots", "sitemap", "rss", "llms", "static asset"}:
                agg.minute_content_404s[minute_bucket] += 1
        if clean_path == "/robots.txt" and (e.status == 404 or e.status >= 500):
            agg.robots_failure_minutes[e.bot][minute_bucket] += 1

    # Final trim so report-time code can rely on a sorted, bounded list.
    if agg.slowest_entries:
        agg.slowest_entries.sort(key=lambda x: x.request_time or 0.0, reverse=True)
        del agg.slowest_entries[10:]

    return agg


def _classify_redirect_cause(scheme: str, host: str, path: str) -> str:
    """Classify a redirect's cause from its source $scheme/$host/path.

    Returns one of: "http_to_https", "www_to_apex", "trailing_slash",
    "canonical_loop", "other". Mirrors the JSON keys documented in spec §5.4.

    The classification is best-effort: nginx logs the source of the redirect,
    not the target, so we infer the most likely cause from the source. The
    ordering of checks matters — HTTP→HTTPS is the strongest signal because
    it explains every redirect on an http request regardless of host shape.
    """
    host_lower = host.lower()
    if scheme == "http":
        return "http_to_https"
    if host_lower.startswith("www."):
        return "www_to_apex"
    if path and not path.endswith("/") and "." not in path.rsplit("/", 1)[-1]:
        # Path with no trailing slash and no file extension on the last
        # segment — most likely a directory-style URL that nginx is
        # redirecting to add the slash.
        return "trailing_slash"
    # Reached only when scheme=https, host is apex, and path looks canonical.
    # A redirect in this position is a configuration bug — the request was
    # already on the canonical URL but still got a 3xx response.
    return "canonical_loop"


# ---------------------------------------------------------------------------
# Health score
# ---------------------------------------------------------------------------


def compute_percentiles(samples: list[float]) -> dict[str, float] | None:
    """Compute median, p75, p95, p99 from a list of timing samples.

    Returns None if there are too few samples to compute percentiles
    meaningfully (need at least 2). Uses statistics.quantiles with n=100
    per spec §5.2; indices [49], [74], [94], [98] map to p50, p75, p95, p99.

    Samples must already have None values filtered out — this function
    assumes every entry is a real measurement and treats 0.0 as data.
    """
    if len(samples) < 2:
        # statistics.quantiles requires at least 2 data points. With one
        # sample, "percentile" is not meaningful — report it as median only.
        if len(samples) == 1:
            return {"median": samples[0], "p75": samples[0],
                    "p95": samples[0], "p99": samples[0]}
        return None
    qs = statistics.quantiles(samples, n=100)
    # quantiles(n=100) returns 99 cut-points: qs[0]=p1, qs[49]=p50, etc.
    return {
        "median": qs[49],
        "p75": qs[74],
        "p95": qs[94],
        "p99": qs[98],
    }


def compute_health_score(
    agg: Aggregate,
    latency_threshold_ms: int = 1000,
) -> tuple[int, str, list[str]]:
    """Return (score, verdict, reasons) for Googlebot.

    latency_threshold_ms is the p95 ceiling above which Googlebot incurs
    an additional 5-point deduction. This check only fires when seo_crawl
    data is available for Googlebot (per spec §5.3); otherwise it is
    silently skipped, preserving combined-format behaviour bit-for-bit.
    """
    score = 100
    reasons: list[str] = []

    gb_status = agg.bot_status.get("Googlebot", Counter())
    total = sum(gb_status.values())

    # robots.txt for Googlebot
    robots_for_gb = agg.special_files.get("/robots.txt", {}).get("Googlebot", Counter())
    bad_robots = sum(c for s, c in robots_for_gb.items() if s == 404 or s >= 500)
    if bad_robots > 0:
        score -= 30
        reasons.append(f"-30: Googlebot received {bad_robots} robots.txt failures")

    # 5xx rate
    server_errors = sum(c for s, c in gb_status.items() if 500 <= s < 600)
    if server_errors > 0:
        score -= 20
        reasons.append(f"-20: {server_errors} Googlebot 5xx responses")

    # 404 rate
    not_found = gb_status.get(404, 0)
    if total > 0 and not_found / total > 0.01:
        score -= 10
        pct = 100 * not_found / total
        reasons.append(f"-10: Googlebot 404 rate {pct:.1f}% (>1%)")

    # Redirect rate
    redirects = sum(c for s, c in gb_status.items() if 300 <= s < 400)
    if total > 0 and redirects / total > 0.25:
        score -= 5
        pct = 100 * redirects / total
        reasons.append(f"-5: Googlebot redirect rate {pct:.1f}% (>25%)")

    # Sitemap fetched at all by Googlebot?
    sitemap_hits = 0
    for sf in ("/sitemap-index.xml", "/sitemap-0.xml", "/sitemap.xml"):
        sitemap_hits += sum(agg.special_files.get(sf, {}).get("Googlebot", Counter()).values())
    if total > 0 and sitemap_hits == 0:
        score -= 5
        reasons.append("-5: Googlebot did not fetch any sitemap")

    # Robots fetched at all by Googlebot?
    if total > 0 and sum(robots_for_gb.values()) == 0:
        score -= 5
        reasons.append("-5: Googlebot did not fetch robots.txt")

    # v1.3: latency-based deduction. Only fires when seo_crawl data was
    # observed for Googlebot specifically. The threshold matches Google
    # Search Console's "Average response time" warning level by default.
    gb_latencies = agg.request_times_by_bot.get("Googlebot", [])
    if gb_latencies:
        percentiles = compute_percentiles(gb_latencies)
        if percentiles is not None:
            p95_seconds = percentiles["p95"]
            if p95_seconds * 1000 > latency_threshold_ms:
                score -= 5
                reasons.append(
                    f"-5: Googlebot p95 latency {p95_seconds:.1f}s "
                    f"(>{latency_threshold_ms / 1000:.1f}s threshold)"
                )

    score = max(0, min(100, score))

    if score >= 95:
        verdict = "Excellent"
    elif score >= 85:
        verdict = "Healthy"
    elif score >= 70:
        verdict = "Needs attention"
    else:
        verdict = "Investigate immediately"

    return score, verdict, reasons


# ---------------------------------------------------------------------------
# Googlebot DNS verification (optional)
# ---------------------------------------------------------------------------


def verify_googlebot_ips(ips: Iterable[str]) -> dict[str, bool]:
    """Reverse + forward DNS check against googlebot.com / google.com.

    Returns {ip: verified}. Uses stdlib socket only.
    """
    import socket  # local import; avoids cost when not used

    results: dict[str, bool] = {}
    for ip in ips:
        try:
            host, _aliases, _addrs = socket.gethostbyaddr(ip)
        except (socket.herror, socket.gaierror, OSError):
            results[ip] = False
            continue

        host_lower = host.lower().rstrip(".")
        if not (host_lower.endswith(".googlebot.com")
                or host_lower.endswith(".google.com")):
            results[ip] = False
            continue

        try:
            forward = socket.gethostbyname_ex(host)
        except (socket.gaierror, OSError):
            results[ip] = False
            continue

        forward_ips = set(forward[2])
        results[ip] = ip in forward_ips

    return results


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "n/a"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def section_header(title: str, fmt: str) -> str:
    if fmt == "markdown":
        return f"\n## {title}\n"
    rule = "─" * len(title)
    return f"\n{title}\n{rule}\n"


def render_summary(agg: Aggregate, fmt: str) -> str:
    out: list[str] = []
    out.append(section_header("Crawler Log Summary", fmt))

    s = agg.stats
    out.append(f"Date range: {fmt_dt(agg.earliest)} → {fmt_dt(agg.latest)}")
    pct_bad = (100 * s.malformed / s.total_lines) if s.total_lines else 0
    suffix = ""
    if pct_bad > 5:
        suffix = "  [WARNING: malformed rate >5%, see Malformed Lines section]"
    out.append(
        f"Lines parsed: {s.parsed:,} "
        f"(malformed skipped: {s.malformed:,} / {pct_bad:.1f}%, "
        f"out of range: {s.out_of_range:,}){suffix}"
    )

    crawler_total = sum(sum(c.values()) for bot, c in agg.bot_status.items() if bot != "Human/Other")
    out.append(f"Crawler requests: {crawler_total:,}")

    gb = agg.bot_status.get("Googlebot", Counter())
    gb_total = sum(gb.values())
    out.append(f"Googlebot requests: {gb_total:,}")

    if gb_total:
        ok = sum(c for s_, c in gb.items() if 200 <= s_ < 300)
        redir = sum(c for s_, c in gb.items() if 300 <= s_ < 400)
        nf = gb.get(404, 0)
        srv = sum(c for s_, c in gb.items() if 500 <= s_ < 600)
        out.append(f"Googlebot status: {ok} × 2xx, {redir} × 3xx, {nf} × 404, {srv} × 5xx")

    robots_gb = agg.special_files.get("/robots.txt", {}).get("Googlebot", Counter())
    if sum(robots_gb.values()):
        ok = sum(c for s_, c in robots_gb.items() if 200 <= s_ < 300)
        bad = sum(c for s_, c in robots_gb.items() if s_ >= 400)
        out.append(f"robots.txt for Googlebot: {ok} × 200, {bad} failures")
    else:
        out.append("robots.txt for Googlebot: not requested in this period")

    for label, sf in (("sitemap-index", "/sitemap-index.xml"),
                      ("sitemap-0",     "/sitemap-0.xml"),
                      ("llms.txt",      "/llms.txt"),
                      ("llms-full.txt", "/llms-full.txt")):
        gb_hits = sum(agg.special_files.get(sf, {}).get("Googlebot", Counter()).values())
        any_hits = sum(sum(c.values()) for c in agg.special_files.get(sf, {}).values())
        marker = "yes" if gb_hits else ("seen by other crawlers" if any_hits else "no")
        out.append(f"{label} fetched by Googlebot: {marker}")

    return "\n".join(out) + "\n"


def render_status_breakdown(agg: Aggregate, fmt: str) -> str:
    out = [section_header("Status Code Breakdown By Bot", fmt)]
    # Sort bots by total volume desc, but always put Googlebot first if present.
    bots = sorted(agg.bot_status.keys(), key=lambda b: (-sum(agg.bot_status[b].values()), b))
    if "Googlebot" in bots:
        bots.remove("Googlebot")
        bots.insert(0, "Googlebot")
    for bot in bots:
        if bot == "Human/Other":
            continue
        out.append(f"\n{bot}")
        for status in sorted(agg.bot_status[bot]):
            out.append(f"  {status}: {agg.bot_status[bot][status]}")
    return "\n".join(out) + "\n"


def render_googlebot_timeline(agg: Aggregate, fmt: str) -> str:
    out = [section_header("Googlebot Crawl Timeline", fmt)]
    if not agg.googlebot_hours:
        out.append("No Googlebot activity in this period.")
        return "\n".join(out) + "\n"

    for hour in sorted(agg.googlebot_hours):
        count = agg.googlebot_hours[hour]
        label = "request" if count == 1 else "requests"
        out.append(f"{hour.strftime('%Y-%m-%d %H:%M')}  {count:>3} {label}")

    ts_sorted = sorted(agg.googlebot_timestamps)
    out.append("")
    out.append(f"First Googlebot hit: {fmt_dt(ts_sorted[0])}")
    out.append(f"Latest Googlebot hit: {fmt_dt(ts_sorted[-1])}")
    if len(ts_sorted) > 1:
        gaps = [(b - a) for a, b in zip(ts_sorted, ts_sorted[1:])]
        longest = max(gaps)
        out.append(f"Longest gap between Googlebot hits: {format_timedelta(longest)}")
    return "\n".join(out) + "\n"


def format_timedelta(td: timedelta) -> str:
    total = int(td.total_seconds())
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def render_robots_health(agg: Aggregate, fmt: str) -> str:
    out = [section_header("robots.txt Health", fmt)]
    by_bot = agg.special_files.get("/robots.txt", {})
    if not by_bot:
        out.append("No robots.txt requests recorded.")
        return "\n".join(out) + "\n"

    findings: list[Finding] = []
    for bot in sorted(by_bot.keys()):
        statuses = by_bot[bot]
        parts = ", ".join(f"{c} × {s}" for s, c in sorted(statuses.items()))
        out.append(f"{bot}: {parts}")
        bad = sum(c for s, c in statuses.items() if s == 404 or s >= 500)
        if bad > 0:
            if bot == "Googlebot":
                findings.append(Finding("CRITICAL", f"Googlebot received {bad} robots.txt failures"))
            elif bot in AI_CRAWLERS:
                findings.append(Finding("WARNING", f"{bot} received {bad} robots.txt failures"))
            else:
                findings.append(Finding("INFO", f"{bot} received {bad} robots.txt failures"))

    if findings:
        out.append("")
        out.append("Findings:")
        for f in sorted(findings):
            out.append(f"  [{f.severity}] {f.message}")
    return "\n".join(out) + "\n"


def render_special_files(agg: Aggregate, fmt: str) -> str:
    out = [section_header("Special SEO File Access", fmt)]
    for sf in SPECIAL_FILES:
        by_bot = agg.special_files.get(sf, {})
        if not by_bot:
            continue
        bots = ", ".join(sorted(by_bot.keys()))
        out.append(f"{sf:<22} {bots}")
    if len(out) == 1:
        out.append("No special SEO files were requested.")
    return "\n".join(out) + "\n"


def render_top_googlebot_urls(agg: Aggregate, fmt: str, top_n: int) -> str:
    out = [section_header(f"Top {top_n} Googlebot URLs", fmt)]
    urls = agg.bot_urls.get("Googlebot", Counter())
    if not urls:
        out.append("No Googlebot URL hits recorded.")
        return "\n".join(out) + "\n"

    for rank, (path, count) in enumerate(urls.most_common(top_n), 1):
        statuses = agg.bot_url_status["Googlebot"][path]
        parts = " ".join(f"{s}×{c}" for s, c in sorted(statuses.items()))
        out.append(f"{rank:>2}. {path:<55} {count:>4}   [{parts}]")
    return "\n".join(out) + "\n"


def render_crawler_404s(agg: Aggregate, fmt: str) -> str:
    out = [section_header("Crawler-Visible 404s", fmt)]
    if not agg.crawler_404s:
        out.append("No crawler 404s recorded. ✓")
        return "\n".join(out) + "\n"

    # Order: paths with Googlebot 404s first, then by total count.
    def sort_key(item: tuple[str, Counter[str]]) -> tuple[int, int]:
        path, by_bot = item
        gb_first = -by_bot.get("Googlebot", 0)
        return (gb_first, -sum(by_bot.values()))

    for path, by_bot in sorted(agg.crawler_404s.items(), key=sort_key):
        out.append(f"\n{path}")
        for bot in sorted(by_bot.keys()):
            sev = severity_for_404(bot, path)
            out.append(f"  [{sev}] {bot}: {by_bot[bot]}")
            sample = agg.crawler_404_samples.get((path, bot))
            if sample is not None:
                out.append(f"        first seen {fmt_dt(sample.ts)} from {sample.ip}")
    return "\n".join(out) + "\n"


def severity_for_404(bot: str, path: str) -> str:
    important = path in SPECIAL_FILES or path == "/"
    if bot == "Googlebot":
        return "CRITICAL" if important else "HIGH"
    if bot in AI_CRAWLERS:
        return "WARNING" if important else "INFO"
    return "INFO"


def render_redirects(agg: Aggregate, fmt: str) -> str:
    out = [section_header("Redirect Analysis", fmt)]
    any_redirects = False
    for bot in sorted(agg.redirects.keys()):
        paths = agg.redirects[bot]
        total = sum(paths.values())
        if total == 0:
            continue
        any_redirects = True
        out.append(f"\n{bot}: {total} redirects")
        for path, count in paths.most_common(10):
            note, critical = _redirect_note(bot, path, agg)
            prefix = "  [CRITICAL] " if critical else "  "
            out.append(f"{prefix}{count:>3} × {path}{note}")
    if not any_redirects:
        out.append("No redirects observed.")
    return "\n".join(out) + "\n"


def _redirect_note(bot: str, path: str, agg: Aggregate) -> tuple[str, bool]:
    """Return (annotation, is_critical) for a redirect path for a given bot.

    When seo_crawl data is available for this (bot, path) pair, the
    annotation lists the observed causes with counts (e.g.
    "(1 HTTP→HTTPS, 1 www→apex)"). is_critical is True if the
    canonical_loop cause count is >= CANONICAL_LOOP_CRITICAL_THRESHOLD,
    which indicates a genuine configuration bug (per spec §5.1).

    The attribution lookup is per-bot (v1.4.1), so the cause-count
    annotation reconciles with the per-bot redirect count alongside it.

    When no seo_crawl data is available, the original v1.2 heuristic
    note is used so combined-format output remains byte-identical.

    v1.6 severity calibration:
        Two days of production data (2026-05-11..05-13) showed working
        redirects routinely hitting counts of 11-22 from Googlebot
        revalidation after IndexNow pings. The v1.5 threshold (>= 3)
        was producing false CRITICAL flags for redirects that the
        seo-validator section 21 confirmed were resolving cleanly.

        v1.6 raises the CRITICAL threshold to >= 50 and adds a
        "[high-traffic redirect]" annotation for the 3-49 range so the
        operator still sees the volume without it triggering an
        unwarranted CRITICAL flag.

        A genuine configuration loop produces hundreds of retries on
        the same path within a report period. The >= 50 threshold
        comfortably separates working-redirect-at-scale from
        configuration-loop, while remaining low enough to catch real
        loops before they consume significant crawler budget.

        Calibrated empirically; revisit if production data shows the
        gap matters at lower counts.
    """
    bot_attribution = agg.redirect_attribution.get(bot)
    attribution = bot_attribution.get(path) if bot_attribution else None
    if attribution:
        # Render in a fixed, human-friendly order so the same data always
        # produces the same string. The order also matches the JSON keys
        # in §5.4 except "other" which is appended last when present.
        label_order = [
            ("http_to_https", "HTTP→HTTPS"),
            ("www_to_apex", "www→apex"),
            ("trailing_slash", "trailing-slash"),
            ("canonical_loop", "canonical-loop"),
            ("other", "other"),
        ]
        parts = [f"{attribution[k]} {label}"
                 for k, label in label_order if attribution.get(k)]
        note = f"  ({', '.join(parts)})" if parts else ""

        canonical_loop_count = attribution.get("canonical_loop", 0)
        critical = canonical_loop_count >= CANONICAL_LOOP_CRITICAL_THRESHOLD
        # v1.6: annotate non-CRITICAL working redirects at scale so the
        # operator sees the volume context. Threshold band:
        #   1-2  : silent (legacy path rename, single visit)
        #   3-49 : "[high-traffic redirect]" suffix (working redirect
        #          revalidated heavily, e.g. post-IndexNow ping)
        #   >=50 : CRITICAL (genuine configuration loop)
        if (CANONICAL_LOOP_HIGH_TRAFFIC_THRESHOLD
                <= canonical_loop_count
                < CANONICAL_LOOP_CRITICAL_THRESHOLD):
            note = f"{note} [high-traffic redirect]"
        return note, critical

    # Fallback: original v1.2 heuristic. Preserves byte-identical output
    # for combined-format inputs.
    if path == "/":
        note = "  (likely HTTP→HTTPS or www→apex if $host/$scheme not logged)"
    elif "robots.txt?" in path:
        note = "  (suspicious: robots.txt with query string)"
    elif path.startswith("/robots.txt") or path in SPECIAL_FILES:
        note = "  (special file — investigate)"
    elif path.endswith("/"):
        note = "  (likely canonical)"
    else:
        note = "  (likely trailing-slash redirect)"
    return note, False


def render_response_latency(agg: Aggregate, fmt: str) -> str:
    """Render the Response Latency section (v1.3, §5.2).

    Only emitted when at least one seo_crawl entry contributed a non-None
    request_time. Returns an empty string otherwise so that combined-format
    runs produce byte-identical output to v1.2.
    """
    if not agg.has_latency_data:
        return ""

    out = [section_header("Response Latency", fmt)]

    # If every observed latency is sub-millisecond, statistics on them
    # would be spuriously precise — produce a brief honest note instead.
    if all(t < 0.001 for t in agg.all_request_times):
        out.append("All requests completed in under 1ms. No latency outliers to report.")
        return "\n".join(out) + "\n"

    percentiles = compute_percentiles(agg.all_request_times)
    if percentiles is None:
        # has_latency_data is true but we couldn't compute — possible only
        # if all_request_times is empty, which shouldn't happen, but be
        # defensive.
        out.append("Latency data present but statistically insufficient.")
        return "\n".join(out) + "\n"

    out.append("Overall:")
    out.append(
        f"  median: {_fmt_latency(percentiles['median'])}    "
        f"p75: {_fmt_latency(percentiles['p75'])}    "
        f"p95: {_fmt_latency(percentiles['p95'])}    "
        f"p99: {_fmt_latency(percentiles['p99'])}"
    )

    # Per-bot medians for the top 5 most-active bots that have timing data.
    # "Top 5 by volume" per §5.2 — measured by sample count, which equals
    # the number of seo_crawl requests for that bot.
    bot_volumes = [(bot, len(samples))
                   for bot, samples in agg.request_times_by_bot.items()
                   if samples]
    bot_volumes.sort(key=lambda x: (-x[1], x[0]))
    top_bots = bot_volumes[:5]

    if top_bots:
        out.append("")
        out.append("Per-bot medians (top 5 by volume):")
        # Align the bot names for readability.
        width = max(len(bot) for bot, _ in top_bots) + 1
        for bot, _ in top_bots:
            samples = agg.request_times_by_bot[bot]
            med = statistics.median(samples)
            out.append(f"  {bot + ':':<{width + 1}} {_fmt_latency(med)}")

    # Top 10 slowest individual requests. agg.slowest_entries is already
    # sorted descending and trimmed to 10 by aggregate().
    if agg.slowest_entries:
        out.append("")
        out.append("Slowest 10 requests:")
        for rank, e in enumerate(agg.slowest_entries, 1):
            rt = e.request_time if e.request_time is not None else 0.0
            ut = f"{e.upstream_time:.3f}s" if e.upstream_time is not None else "-"
            # v1.4.1: render empty path as a clear placeholder rather
            # than a blank column. Empty paths come from probe traffic
            # with unusual request lines (e.g. "GET // HTTP/1.1") where
            # the parser tolerated the shape but path normalisation
            # produced an empty string.
            raw_path = e.path if e.path else "(empty)"
            path_display = raw_path if len(raw_path) <= 30 else raw_path[:29] + "…"
            bot_display = e.bot if len(e.bot) <= 18 else e.bot[:17] + "…"
            ts_display = e.ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            out.append(
                f"  {rank:>2}. {rt:.3f}s  upstream={ut:<8}  "
                f"{path_display:<30}  {bot_display:<18}  {ts_display}"
            )

    return "\n".join(out) + "\n"


def _fmt_latency(seconds: float) -> str:
    """Render a latency value with honest precision at low scales.

    Values under 1ms render as "<1ms" rather than "0.000s" — the latter
    reads as "zero latency" when in practice it just means "below the
    millisecond display threshold." This matters on fast static sites
    where percentiles legitimately round to sub-millisecond but slow
    outliers are still present in the data; rendering both as 0.000s
    misleads readers into thinking the slowest-10 list contradicts the
    percentile summary.
    """
    if seconds < 0.001:
        return "<1ms"
    return f"{seconds:.3f}s"


def render_health_score(agg: Aggregate, fmt: str,
                        latency_threshold_ms: int = 1000) -> str:
    score, verdict, reasons = compute_health_score(agg, latency_threshold_ms)
    out = [section_header("Googlebot Crawl Health Score", fmt)]
    out.append(f"Score: {score}/100")
    out.append(f"Verdict: {verdict}")
    if reasons:
        out.append("")
        out.append("Deductions:")
        for r in reasons:
            out.append(f"  {r}")
    else:
        out.append("No deductions applied.")
    return "\n".join(out) + "\n"


def render_ai_crawlers(agg: Aggregate, fmt: str, bot_filter: str) -> str:
    out = [section_header("AI Crawler Report", fmt)]
    if bot_filter != "all" and bot_filter.lower() not in {n.lower() for n in AI_CRAWLERS}:
        out.append(f"Skipped because --bot {bot_filter} filter is active.")
        return "\n".join(out) + "\n"
    seen_any = False
    for bot in sorted(AI_CRAWLERS):
        statuses = agg.bot_status.get(bot, Counter())
        total = sum(statuses.values())
        if total == 0:
            continue
        seen_any = True
        files_seen = [sf for sf in SPECIAL_FILES
                      if bot in agg.special_files.get(sf, {})]
        nf = statuses.get(404, 0)
        srv = sum(c for s_, c in statuses.items() if 500 <= s_ < 600)
        out.append(f"\n{bot}")
        out.append(f"  total requests: {total}")
        if files_seen:
            out.append(f"  fetched: {', '.join(files_seen)}")
        else:
            out.append("  fetched: no special SEO files")
        out.append(f"  404: {nf}, 5xx: {srv}")
    if not seen_any:
        out.append("No AI crawler activity recorded.")
        return "\n".join(out) + "\n"

    # v1.6: depth-preference subsection. Only renders AI crawlers that
    # have fetched at least one of /llms.txt or /llms-full.txt. Crawlers
    # that only hit /robots.txt belong in the main listing above; they
    # don't carry a depth signal worth surfacing.
    depth_lines = _render_ai_depth_preference(agg)
    if depth_lines:
        out.append("")
        out.append("Content-depth preference (LLMs file fetches)")
        out.extend(depth_lines)

    return "\n".join(out) + "\n"


def _classify_ai_depth_preference(full_count: int, index_count: int) -> tuple[str, float | None]:
    """Return (label, full_ratio) for an AI crawler's LLMs file fetches.

    Used by both the text renderer and the JSON output so the two stay
    in sync. full_ratio is None when there's insufficient data (single
    fetch or both zero).
    """
    total = full_count + index_count
    if total == 0:
        return ("no data", None)
    if total == 1:
        return ("insufficient data", None)
    if index_count == 0:
        return ("exclusive: full-content", 1.0)
    if full_count == 0:
        return ("exclusive: index-only", 0.0)
    full_ratio = full_count / total
    if full_ratio >= AI_DEPTH_PREFERENCE_RATIO:
        return (f"prefers full-content ({int(round(full_ratio * 100))}% full)", full_ratio)
    if full_ratio <= (1 - AI_DEPTH_PREFERENCE_RATIO):
        return (f"prefers index-only ({int(round((1 - full_ratio) * 100))}% index)", full_ratio)
    return (
        f"mixed ({int(round(full_ratio * 100))}% full, {int(round((1 - full_ratio) * 100))}% index)",
        full_ratio,
    )


def _compute_ai_depth_preference_payload(agg: Aggregate) -> dict[str, dict[str, object]]:
    """Return the structured AI crawler depth-preference data for JSON output.

    Empty dict means "no AI crawler fetched any LLMs file" — caller
    should omit the JSON key entirely so v1.5 output is preserved for
    runs without LLMs file activity.
    """
    payload: dict[str, dict[str, object]] = {}
    for bot in sorted(AI_CRAWLERS):
        url_counts = agg.bot_urls.get(bot)
        if not url_counts:
            continue
        full_count = url_counts.get("/llms-full.txt", 0)
        index_count = url_counts.get("/llms.txt", 0)
        if full_count + index_count == 0:
            continue
        label, full_ratio = _classify_ai_depth_preference(full_count, index_count)
        payload[bot] = {
            "llms_txt_fetches": index_count,
            "llms_full_txt_fetches": full_count,
            "full_content_ratio": full_ratio,
            "preference": label,
        }
    return payload


def _render_ai_depth_preference(agg: Aggregate) -> list[str]:
    """Return the per-crawler depth-preference lines for the AI Crawler
    Report subsection introduced in v1.6.

    For each AI crawler that has fetched at least one of /llms.txt or
    /llms-full.txt during the report period, classify its preference.
    See _classify_ai_depth_preference() for the classification rules.

    A crawler that has only hit /robots.txt (no LLMs file activity) is
    deliberately excluded from this subsection — those crawlers appear
    in the main AI crawler listing above but don't carry a depth signal
    worth surfacing here.
    """
    lines: list[str] = []
    for bot in sorted(AI_CRAWLERS):
        url_counts = agg.bot_urls.get(bot)
        if not url_counts:
            continue
        full_count = url_counts.get("/llms-full.txt", 0)
        index_count = url_counts.get("/llms.txt", 0)
        if full_count + index_count == 0:
            continue
        label, _ = _classify_ai_depth_preference(full_count, index_count)
        lines.append(f"  {bot}")
        lines.append(f"    llms.txt: {index_count}, llms-full.txt: {full_count}")
        lines.append(f"    preference: {label}")
    return lines


def render_section_breakdown(agg: Aggregate, fmt: str) -> str:
    out = [section_header("Googlebot by Section", fmt)]
    if not agg.googlebot_sections:
        out.append("No Googlebot activity recorded.")
        return "\n".join(out) + "\n"
    width = max(len(c) for c in agg.googlebot_sections)
    for category, count in agg.googlebot_sections.most_common():
        out.append(f"  {category:<{width}}  {count}")
    return "\n".join(out) + "\n"


def render_verification(agg: Aggregate, fmt: str, results: dict[str, bool]) -> str:
    out = [section_header("Googlebot Verification", fmt)]
    if not results:
        out.append("No Googlebot IPs to verify.")
        return "\n".join(out) + "\n"
    for ip in sorted(results.keys()):
        verdict = "verified Googlebot" if results[ip] else "unverified / not Google-owned"
        out.append(f"  {ip:<40} {verdict}")
    return "\n".join(out) + "\n"


def render_malformed_samples(agg: Aggregate, fmt: str) -> str:
    s = agg.stats
    if s.malformed == 0:
        return ""
    out = [section_header("Malformed Lines (sample)", fmt)]
    pct = (100 * s.malformed / s.total_lines) if s.total_lines else 0
    out.append(f"{s.malformed} of {s.total_lines} lines ({pct:.1f}%) failed to parse.")
    if pct > 5:
        out.append("[WARNING] Malformed rate is above 5%. Aggregates may understate")
        out.append("          true crawler volume. Inspect the samples below to identify")
        out.append("          the log format variant the parser does not yet handle.")
    out.append("")
    out.append(f"First {len(s.malformed_samples)} samples:")
    for i, sample in enumerate(s.malformed_samples, 1):
        out.append(f"  {i:>2}. {sample}")
    return "\n".join(out) + "\n"


def render_probe_summary(agg: Aggregate, fmt: str) -> str:
    if agg.probe_count == 0:
        return ""
    out = [section_header("Suppressed Security Probe Noise", fmt)]
    out.append(f"Total probe requests: {agg.probe_count:,}")
    out.append("")
    out.append("Top probed paths:")
    for path, count in agg.probe_paths.most_common(10):
        out.append(f"  {count:>4} × {path}")
    out.append("")
    out.append("Top source IPs:")
    for ip, count in agg.probe_ips.most_common(5):
        out.append(f"  {count:>4} requests from {ip}")
    out.append("")
    out.append("Use --show-security-probes to include these in the 404 report.")
    return "\n".join(out) + "\n"


def render_framework_probes(agg: Aggregate, fmt: str) -> str:
    if agg.framework_probe_count == 0:
        return ""
    out = [section_header("Framework Fingerprint Probes", fmt)]
    out.append(f"Total framework probe requests: {agg.framework_probe_count:,}")
    out.append("")
    out.append("These are bots checking which generator built the site")
    out.append("(Next.js, Nuxt, webpack, etc). They are 404s on a static Astro")
    out.append("site, but they are not deploy-window symptoms — they are")
    out.append("excluded from the content-404 burst detector.")
    out.append("")
    out.append("Top framework probe paths:")
    for path, count in agg.framework_probe_paths.most_common(10):
        out.append(f"  {count:>4} × {path}")
    return "\n".join(out) + "\n"


def render_bad_request_noise(agg: Aggregate, fmt: str) -> str:
    if agg.bad_request_count == 0:
        return ""
    out = [section_header("Bad Request Noise (HTTP 400)", fmt)]
    out.append(f"Total HTTP 400 responses: {agg.bad_request_count:,}")
    out.append("")
    out.append("These are malformed requests rejected by nginx before routing —")
    out.append("typically exploit/probe traffic with binary payloads or invalid")
    out.append("HTTP. Excluded from SEO conclusions.")
    out.append("")
    if agg.bad_request_bots:
        out.append("By user-agent classification:")
        for bot, count in agg.bad_request_bots.most_common(5):
            out.append(f"  {count:>4} × {bot}")
        out.append("")
    if agg.bad_request_ips:
        out.append("Top source IPs:")
        for ip, count in agg.bad_request_ips.most_common(5):
            out.append(f"  {count:>4} requests from {ip}")
    return "\n".join(out) + "\n"


def compute_search_crawler_score(agg: Aggregate) -> tuple[int, str, list[str], int]:
    """Broader 'real search crawler' health score.

    Covers Googlebot, Bingbot, DuckDuckBot, Yandex, Baidu, Apple. Excludes
    UnknownBot, GenericBot, AI crawlers, social-preview bots, security
    probes, framework probes.

    Returns (score, verdict, reasons, total_requests).
    """
    score = 100
    reasons: list[str] = []

    real = {b: agg.bot_status[b] for b in agg.bot_status if b in SEARCH_CRAWLERS}
    total = sum(sum(c.values()) for c in real.values())
    if total == 0:
        return (0, "No real search crawler activity", [], 0)

    # Aggregate bad outcomes across the cohort.
    server_errors = sum(
        c for statuses in real.values() for s, c in statuses.items() if 500 <= s < 600
    )
    if server_errors > 0:
        score -= 20
        reasons.append(f"-20: {server_errors} 5xx responses to real search crawlers")

    not_found = sum(
        c for statuses in real.values() for s, c in statuses.items() if s == 404
    )
    if total > 0 and not_found / total > 0.02:
        pct = 100 * not_found / total
        score -= 10
        reasons.append(f"-10: real-crawler 404 rate {pct:.1f}% (>2%)")

    # robots.txt failures across the cohort.
    bad_robots = 0
    for bot in real:
        statuses = agg.special_files.get("/robots.txt", {}).get(bot, Counter())
        bad_robots += sum(c for s, c in statuses.items() if s == 404 or s >= 500)
    if bad_robots > 0:
        score -= 25
        reasons.append(f"-25: {bad_robots} robots.txt failures across real search crawlers")

    score = max(0, min(100, score))
    if score >= 95:
        verdict = "Excellent"
    elif score >= 85:
        verdict = "Healthy"
    elif score >= 70:
        verdict = "Needs attention"
    else:
        verdict = "Investigate immediately"
    return score, verdict, reasons, total


def render_search_crawler_score(agg: Aggregate, fmt: str) -> str:
    out = [section_header("Search Crawler Health Score (broader)", fmt)]
    score, verdict, reasons, total = compute_search_crawler_score(agg)
    if total == 0:
        out.append("No real search crawler activity recorded.")
        return "\n".join(out) + "\n"
    out.append(f"Cohort: {', '.join(sorted(SEARCH_CRAWLERS))}")
    out.append(f"Cohort requests: {total:,}")
    out.append(f"Score: {score}/100")
    out.append(f"Verdict: {verdict}")
    if reasons:
        out.append("")
        out.append("Deductions:")
        for r in reasons:
            out.append(f"  {r}")
    else:
        out.append("No deductions applied.")
    return "\n".join(out) + "\n"


def detect_deploy_anomalies(agg: Aggregate) -> list[Finding]:
    """Detect symptoms of the previous broken-deploy failure mode.

    Specifically:
      - any robots.txt 404/5xx (CRITICAL)
      - bursts of content 404s clustered in a short window
      - a Googlebot 404 followed by a multi-hour crawl gap
    """
    findings: list[Finding] = []

    # robots.txt failures: CRITICAL for Googlebot, WARNING for AI crawlers,
    # INFO for everything else. Mirrors spec §6.4.
    for bot, minutes in agg.robots_failure_minutes.items():
        if not minutes:
            continue
        total = sum(minutes.values())
        first = min(minutes)
        last = max(minutes)
        msg = (f"{bot} robots.txt failed {total} time(s) across "
               f"{len(minutes)} minute(s) "
               f"({fmt_dt(first)} – {fmt_dt(last)})")
        if bot == "Googlebot":
            findings.append(Finding("CRITICAL", msg))
        elif bot in AI_CRAWLERS:
            findings.append(Finding("WARNING", msg))
        else:
            findings.append(Finding("INFO", msg))

    # Burst detection: sliding 2-minute window with ≥5 content 404s.
    if agg.minute_content_404s:
        sorted_minutes = sorted(agg.minute_content_404s)
        window = timedelta(minutes=2)
        for i, m_start in enumerate(sorted_minutes):
            cluster = 0
            cluster_end = m_start
            for m in sorted_minutes[i:]:
                if m - m_start > window:
                    break
                cluster += agg.minute_content_404s[m]
                cluster_end = m
            if cluster >= 5:
                findings.append(Finding(
                    "HIGH",
                    f"Content-404 burst: {cluster} 404s between "
                    f"{fmt_dt(m_start)} and {fmt_dt(cluster_end)} "
                    f"(possible deploy-window symptom)"
                ))
                break  # one report is enough; user has the timestamp

    # Googlebot 404 followed by long gap.
    gb_ts = sorted(agg.googlebot_timestamps)
    if len(gb_ts) >= 2:
        # Find Googlebot 404 timestamps from crawler_404 samples.
        gb_404_times = [
            sample.ts for (_, bot), sample in agg.crawler_404_samples.items()
            if bot == "Googlebot"
        ]
        for t in gb_404_times:
            # Find the next Googlebot hit after this 404.
            after = [x for x in gb_ts if x > t]
            if not after:
                continue
            gap = after[0] - t
            if gap > timedelta(hours=2):
                findings.append(Finding(
                    "WARNING",
                    f"Googlebot 404 at {fmt_dt(t)} followed by "
                    f"{format_timedelta(gap)} crawl gap"
                ))

    return findings


def render_deploy_anomalies(agg: Aggregate, fmt: str) -> str:
    out = [section_header("Deploy-Window Anomaly Detection", fmt)]
    findings = detect_deploy_anomalies(agg)
    if not findings:
        out.append("No deploy-window symptoms detected. ✓")
        out.append("")
        out.append("Checks performed:")
        out.append("  - robots.txt 404/5xx for any crawler")
        out.append("  - content-404 bursts (≥5 within 2 minutes)")
        out.append("  - Googlebot 404 followed by >2h crawl gap")
        return "\n".join(out) + "\n"

    out.append("Symptoms detected:")
    for f in sorted(findings):
        out.append(f"  [{f.severity}] {f.message}")
    return "\n".join(out) + "\n"


def render_log_format_recommendation(agg: Aggregate, fmt: str) -> str:
    out = [section_header("Recommended Nginx Log Format", fmt)]
    if agg.has_latency_data:
        # The input already uses the recommended format (or a superset of
        # it). Avoid telling the user to do something they've already done.
        out.append("This report includes per-request latency because the input log")
        out.append("uses the seo_crawl format. The recommended definition for new")
        out.append("adopters is included below for reference.")
        out.append("")
        out.append("Suggested dedicated SEO log:")
        out.append("")
        if fmt == "markdown":
            out.append("```nginx")
        out.append("    log_format seo_crawl '$remote_addr $host $scheme - [$time_local] '")
        out.append("                         '\"$request\" $status $body_bytes_sent '")
        out.append("                         '\"$http_referer\" \"$http_user_agent\" '")
        out.append("                         '$request_time $upstream_response_time';")
        out.append("")
        out.append("    access_log /var/log/nginx/seo-crawl.log seo_crawl;")
        if fmt == "markdown":
            out.append("```")
        return "\n".join(out) + "\n"

    # Combined-format path: byte-identical to v1.2 per spec §2.1. Do not
    # change a single character here without re-running the regression
    # suite — the byte-identity guarantee is a hard requirement.
    out.append("Adding $host and $scheme to the log format would let this")
    out.append("analyser distinguish HTTP vs HTTPS and apex vs www requests.")
    out.append("Currently they all appear as path '/' with no host context.")
    out.append("")
    out.append("Suggested dedicated SEO log:")
    out.append("")
    if fmt == "markdown":
        out.append("```nginx")
    out.append("    log_format seo_crawl '$remote_addr $host $scheme - [$time_local] '")
    out.append("                         '\"$request\" $status $body_bytes_sent '")
    out.append("                         '\"$http_referer\" \"$http_user_agent\"';")
    out.append("")
    out.append("    access_log /var/log/nginx/seo-crawl.log seo_crawl;")
    if fmt == "markdown":
        out.append("```")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# JSON rendering
# ---------------------------------------------------------------------------


def render_json(agg: Aggregate, top_n: int, verification: dict[str, bool] | None,
                latency_threshold_ms: int = 1000) -> str:
    score, verdict, reasons = compute_health_score(agg, latency_threshold_ms)
    _sc_score, _sc_verdict, _sc_reasons, _sc_total = compute_search_crawler_score(agg)

    summary = {
        "date_range": {
            "from": agg.earliest.astimezone(timezone.utc).isoformat() if agg.earliest else None,
            "to": agg.latest.astimezone(timezone.utc).isoformat() if agg.latest else None,
        },
        "lines": {
            "total": agg.stats.total_lines,
            "parsed": agg.stats.parsed,
            "malformed": agg.stats.malformed,
            "malformed_pct": (
                round(100 * agg.stats.malformed / agg.stats.total_lines, 2)
                if agg.stats.total_lines else 0
            ),
            "malformed_samples": agg.stats.malformed_samples,
            "out_of_range": agg.stats.out_of_range,
        },
        "crawler_requests": sum(
            sum(c.values()) for bot, c in agg.bot_status.items() if bot != "Human/Other"
        ),
    }

    bot_breakdown = {
        bot: {str(s): c for s, c in sorted(statuses.items())}
        for bot, statuses in sorted(agg.bot_status.items())
    }

    top_googlebot = [
        {
            "url": path,
            "count": count,
            "status": {str(s): c for s, c in agg.bot_url_status["Googlebot"][path].items()},
        }
        for path, count in agg.bot_urls.get("Googlebot", Counter()).most_common(top_n)
    ]

    special = {
        sf: {bot: dict({str(s): c for s, c in statuses.items()})
             for bot, statuses in by_bot.items()}
        for sf, by_bot in agg.special_files.items()
    }

    crawler_404s = [
        {"url": path, "by_bot": dict(by_bot), "severity": severity_for_404(
            max(by_bot, key=by_bot.get), path)}
        for path, by_bot in agg.crawler_404s.items()
    ]

    timeline = [
        {"hour": hour.astimezone(timezone.utc).isoformat(), "count": count}
        for hour, count in sorted(agg.googlebot_hours.items())
    ]

    payload = {
        "summary": summary,
        "bot_breakdown": bot_breakdown,
        "top_googlebot_urls": top_googlebot,
        "special_files": special,
        "crawler_404s": crawler_404s,
        "redirects": {
            bot: paths.most_common(20) for bot, paths in agg.redirects.items()
        },
        "googlebot_timeline": timeline,
        "googlebot_sections": dict(agg.googlebot_sections),
        "health_score": {"score": score, "verdict": verdict, "deductions": reasons},
        "search_crawler_score": {
            "cohort": sorted(SEARCH_CRAWLERS),
            "total_requests": _sc_total,
            "score": _sc_score,
            "verdict": _sc_verdict,
            "deductions": _sc_reasons,
        },
        "deploy_anomalies": [
            {"severity": f.severity, "message": f.message}
            for f in sorted(detect_deploy_anomalies(agg))
        ],
        "security_probes": {
            "total": agg.probe_count,
            "top_paths": agg.probe_paths.most_common(10),
            "top_ips": agg.probe_ips.most_common(5),
        },
        "framework_probes": {
            "total": agg.framework_probe_count,
            "top_paths": agg.framework_probe_paths.most_common(10),
        },
        "bad_request_noise": {
            "total": agg.bad_request_count,
            "by_bot": dict(agg.bad_request_bots.most_common(10)),
            "top_ips": agg.bad_request_ips.most_common(5),
        },
    }
    if verification is not None:
        payload["verification"] = verification

    # v1.3: seo_crawl-derived JSON additions. Per spec §5.4 these keys are
    # omitted entirely (not emitted as null or empty objects) when the
    # corresponding data is unavailable. This keeps the JSON output for
    # combined-format inputs identical to v1.2.
    if agg.has_latency_data:
        percentiles = compute_percentiles(agg.all_request_times)
        per_bot_median: dict[str, float] = {}
        for bot, samples in agg.request_times_by_bot.items():
            if samples:
                per_bot_median[bot] = statistics.median(samples)
        slowest_payload = []
        for e in agg.slowest_entries:
            slowest_payload.append({
                "request_time": e.request_time,
                "upstream_time": e.upstream_time,
                "path": e.path,
                "bot": e.bot,
                "ts": e.ts.astimezone(timezone.utc).isoformat(),
            })
        payload["response_latency"] = {
            "overall": percentiles if percentiles is not None else {},
            "per_bot_median": per_bot_median,
            "slowest": slowest_payload,
        }

    if agg.redirect_attribution:
        # v1.4.1: emit per-bot, per-path attribution so the JSON
        # reconciles with the per-bot redirect counts in the "redirects"
        # key. Previous shape was {path: causes}, which lost the
        # per-bot dimension. New shape is {bot: {path: causes}}.
        # Every (bot, path) pair includes all four canonical keys plus
        # "other" if it was used. Missing causes are emitted as 0 so
        # consumers can rely on a stable inner shape without conditional
        # lookups.
        attribution_payload: dict[str, dict[str, dict[str, int]]] = {}
        canonical_keys = ("http_to_https", "www_to_apex",
                          "trailing_slash", "canonical_loop")
        for bot, by_path in agg.redirect_attribution.items():
            bot_payload: dict[str, dict[str, int]] = {}
            for path, causes in by_path.items():
                row: dict[str, int] = {k: causes.get(k, 0) for k in canonical_keys}
                if causes.get("other"):
                    row["other"] = causes["other"]
                bot_payload[path] = row
            if bot_payload:
                attribution_payload[bot] = bot_payload
        payload["redirect_attribution"] = attribution_payload

    # v1.6: AI crawler depth-preference structured output. Omitted when
    # no AI crawler has fetched at least one of /llms.txt or /llms-full.txt
    # during the report period, so combined-format runs without LLMs file
    # activity produce identical JSON to v1.5.
    depth_payload = _compute_ai_depth_preference_payload(agg)
    if depth_payload:
        payload["ai_crawler_depth_preference"] = depth_payload

    return json.dumps(payload, indent=2, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def expand_paths(args_paths: list[str]) -> list[Path]:
    """Expand globs and return sorted unique Paths that exist."""
    out: list[Path] = []
    seen: set[str] = set()
    for raw in args_paths:
        matches = glob(raw)
        if not matches and Path(raw).exists():
            matches = [raw]
        if not matches:
            print(f"warning: no files matched {raw}", file=sys.stderr)
            continue
        for m in matches:
            if m not in seen:
                seen.add(m)
                out.append(Path(m))
    return sorted(out)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="crawler_log_analyser",
        description="SEO/crawler intelligence from nginx access logs.",
    )
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")
    p.add_argument("paths", nargs="+", help="One or more nginx log files (plain or .gz). Globs supported.")
    p.add_argument("--bot", default="all",
                   help="Restrict the report to a single bot family, or 'all' (default).")
    p.add_argument("--from", dest="date_from", default=None,
                   help="Start date (inclusive), YYYY-MM-DD.")
    p.add_argument("--to", dest="date_to", default=None,
                   help="End date (exclusive), YYYY-MM-DD.")
    p.add_argument("--format", choices=("text", "markdown", "json"), default="text",
                   help="Output format (default: text).")
    p.add_argument("--output", default=None,
                   help="Write output to file instead of stdout.")
    p.add_argument("--top", type=int, default=20,
                   help="Top-N URLs to show (default: 20).")
    p.add_argument("--verify-googlebot", action="store_true",
                   help="Reverse + forward DNS validation of Googlebot IPs (slow).")
    p.add_argument("--include-non-bots", action="store_true",
                   help="Include human/other traffic in aggregation (default: bots only).")
    p.add_argument("--show-security-probes", action="store_true",
                   help="Include exploit-probe traffic (PHP/WordPress/etc) in the 404 report. "
                        "By default these are summarised separately to keep SEO output readable.")
    p.add_argument("--ignore-source-ip", action="append", default=[],
                   dest="ignored_ips", metavar="IP",
                   help="Source IP to exclude from probe classification. Pass once per IP "
                        "(e.g. --ignore-source-ip 35.230.156.201). Typically used to filter "
                        "operator self-test traffic from the daily probe-noise summary, "
                        "since curl tests originating from the host server are otherwise "
                        "indistinguishable from external scanner traffic. Does not affect "
                        "bot statistics or health scores — only the probe-noise tally.")
    p.add_argument("--seo-only", action="store_true",
                   help="Aggressive SEO focus: hide probe summary entirely. Implies suppression "
                        "of security probes.")
    p.add_argument("--strict", action="store_true",
                   help="Cron/CI mode: exit 2 on CRITICAL findings, 1 on HIGH/WARNING, 0 otherwise.")
    p.add_argument("--log-format", choices=(LOG_FORMAT_AUTO, LOG_FORMAT_COMBINED, LOG_FORMAT_SEO),
                   default=LOG_FORMAT_AUTO,
                   help="Log format. 'auto' (default) detects from the first "
                        "line. 'combined' is standard nginx; 'seo-crawl' is the "
                        "extended SpeyTech format with $host, $scheme, "
                        "$request_time, and $upstream_response_time.")
    p.add_argument("--latency-threshold-ms", type=int, default=1000,
                   dest="latency_threshold_ms",
                   help="p95 latency threshold in milliseconds for the "
                        "Googlebot health-score deduction (default: 1000). "
                        "Skipped when no $request_time data is available.")
    return p.parse_args(argv)


def parse_date(s: str | None, end_of_day: bool = False) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        print(f"error: invalid date '{s}', expected YYYY-MM-DD", file=sys.stderr)
        sys.exit(2)
    d = d.replace(tzinfo=timezone.utc)
    if end_of_day:
        d = d + timedelta(days=1)
    return d


def filter_for_bot(agg: Aggregate, bot_filter: str) -> Aggregate:
    """If --bot is set to a specific family, narrow the aggregate to that bot."""
    if bot_filter == "all":
        return agg
    # Match case-insensitively against known bot names.
    target = None
    for name, _, _ in BOT_PATTERNS:
        if name.lower() == bot_filter.lower():
            target = name
            break
    if target is None:
        print(f"warning: unknown --bot '{bot_filter}', showing all", file=sys.stderr)
        return agg

    new = Aggregate()
    new.stats = agg.stats
    new.earliest = agg.earliest
    new.latest = agg.latest

    if target in agg.bot_status:
        new.bot_status[target] = agg.bot_status[target]
    if target in agg.bot_urls:
        new.bot_urls[target] = agg.bot_urls[target]
    if target in agg.bot_url_status:
        new.bot_url_status[target] = agg.bot_url_status[target]
    if target in agg.bot_ips:
        new.bot_ips[target] = agg.bot_ips[target]
    for sf, by_bot in agg.special_files.items():
        if target in by_bot:
            new.special_files[sf][target] = by_bot[target]
    if target == "Googlebot":
        new.googlebot_hours = agg.googlebot_hours
        new.googlebot_timestamps = agg.googlebot_timestamps
        new.googlebot_sections = agg.googlebot_sections
    if target in agg.redirects:
        new.redirects[target] = agg.redirects[target]
    for path, by_bot in agg.crawler_404s.items():
        if target in by_bot:
            new.crawler_404s[path][target] = by_bot[target]
            key = (path, target)
            if key in agg.crawler_404_samples:
                new.crawler_404_samples[key] = agg.crawler_404_samples[key]
    return new


def render_report(agg: Aggregate, args: argparse.Namespace,
                  verification: dict[str, bool] | None) -> str:
    if args.format == "json":
        return render_json(agg, args.top, verification, args.latency_threshold_ms)

    fmt = args.format
    parts = [
        render_summary(agg, fmt),
        render_status_breakdown(agg, fmt),
        render_googlebot_timeline(agg, fmt),
        render_robots_health(agg, fmt),
        render_special_files(agg, fmt),
        render_top_googlebot_urls(agg, fmt, args.top),
        render_crawler_404s(agg, fmt),
        render_redirects(agg, fmt),
        render_section_breakdown(agg, fmt),
        render_ai_crawlers(agg, fmt, args.bot),
        render_deploy_anomalies(agg, fmt),
        render_health_score(agg, fmt, args.latency_threshold_ms),
        render_search_crawler_score(agg, fmt),
        render_response_latency(agg, fmt),
        render_probe_summary(agg, fmt),
        render_framework_probes(agg, fmt),
        render_bad_request_noise(agg, fmt),
        render_malformed_samples(agg, fmt),
    ]
    if verification is not None:
        parts.append(render_verification(agg, fmt, verification))
    parts.append(render_log_format_recommendation(agg, fmt))

    header = ""
    if fmt == "markdown":
        header = "# Nginx Crawler Log Report\n\n_Generated by crawler_log_analyser.py_\n"
    return header + "".join(parts)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    paths = expand_paths(args.paths)
    if not paths:
        print("error: no log files to analyse", file=sys.stderr)
        return 2

    date_from = parse_date(args.date_from, end_of_day=False)
    date_to = parse_date(args.date_to, end_of_day=True)

    stats = ParseStats()
    entries = iter_entries(paths, date_from, date_to, stats,
                           log_format=args.log_format)
    agg = aggregate(
        entries,
        include_non_bots=args.include_non_bots,
        show_security_probes=args.show_security_probes,
        ignored_probe_ips=frozenset(args.ignored_ips),
    )
    agg.stats = stats

    # v1.3 §3.2: if auto-detection failed on every input file, treat that
    # as a hard error and exit 2. A single-file failure when other files
    # succeeded is non-fatal — we already emitted a per-file diagnostic.
    if (stats.files_attempted > 0
            and stats.detection_failures == stats.files_attempted):
        print("error: no log files could be parsed (format detection failed for all)",
              file=sys.stderr)
        return 2

    if args.seo_only:
        # Drop probe data so the summary doesn't appear at all.
        agg.probe_count = 0
        agg.probe_paths.clear()
        agg.probe_ips.clear()

    agg = filter_for_bot(agg, args.bot)

    verification: dict[str, bool] | None = None
    if args.verify_googlebot:
        gb_ips = agg.bot_ips.get("Googlebot", set())
        verification = verify_googlebot_ips(gb_ips)

    output = render_report(agg, args, verification)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(output)

    if args.strict:
        # Exit code policy:
        #   2 = any CRITICAL finding (Googlebot robots.txt failure, etc),
        #       or Googlebot health below "Needs attention"
        #   1 = any HIGH/WARNING/INFO deploy-window finding,
        #       Googlebot health below Excellent,
        #       broader search-crawler score below Excellent,
        #       or any non-Google search-crawler 404
        #   0 = clean
        anomalies = detect_deploy_anomalies(agg)
        severities = {f.severity for f in anomalies}
        gb_score, _, _ = compute_health_score(agg, args.latency_threshold_ms)
        sc_score, _, _, _ = compute_search_crawler_score(agg)

        non_google_search_404s = sum(
            statuses.get(404, 0)
            for bot, statuses in agg.bot_status.items()
            if bot in SEARCH_CRAWLERS and bot != "Googlebot"
        )

        if "CRITICAL" in severities or gb_score < 70:
            return 2
        warning_signals = (
            bool({"HIGH", "WARNING", "INFO"} & severities)
            or gb_score < 95
            or sc_score < 95
            or non_google_search_404s > 0
        )
        if warning_signals:
            return 1
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
