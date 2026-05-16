# Changelog

All notable changes to `crawler-log-analyser` are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## v1.10.2 — 2026-05-16

Third (and likely final) patch closing a leak surfaced by the v1.10.1
production verification on the 2026-05-16 axilog.io seo log. The
Status Code Breakdown correctly unified the day's rotation traffic
under `SuspectedBotIdentityRotation: 301: 3, 404: 6`, but the
Redirect Analysis section displayed those three 301s as if three
separate named bots had each issued one redirect:

~~
BaiduSpider: 1 redirects
    1 × /api/config  (1 trailing-slash)

ClaudeBot: 1 redirects
    1 × /api/env  (1 trailing-slash)

PerplexityBot: 1 redirects
    1 × /actuator/env  (1 trailing-slash)
~~

These were the no-slash-form trigger-path probes that nginx
redirected to canonical-form 404s before the rotation override
caught the slash-form variants. All three came from the same
`5.255.104.83` rotation burst; all three should have been unified
under the rotation classification alongside the 404s.

The v1.10.0 override migrated `bot_status`, `bot_urls`,
`bot_url_status`, `bot_ips`, `special_files`, `crawler_404s`, and
the Googlebot-specific caches. It did not migrate `redirects` or
`redirect_attribution`, so the per-named-bot view of redirects
remained populated for the rotation entries while the per-named-bot
view of statuses had been cleared. The two views disagreed.

### Fixed

- **`__version__` bumped from `1.10.1` to `1.10.2`.**

- **3xx rotation entries now migrate `agg.redirects` and
  `agg.redirect_attribution` alongside the other counters.** The
  override decrements `agg.redirects[original_bot][path]` for each
  rotation entry with status in `(301, 302, 307, 308)` and
  increments the same under `ROTATION_DETECTION_NAME`. The
  per-cause `agg.redirect_attribution` counter migrates the same
  way when populated (seo_crawl format only; combined-format inputs
  have no `host`/`scheme` and therefore no attribution data to
  migrate — the override tolerates this case via a `.get()` chain
  rather than raising).

  Verified property: after the override runs, the total 3xx count
  in `bot_status[ROTATION_DETECTION_NAME]` equals the sum of
  `redirects[ROTATION_DETECTION_NAME]` across all paths. The
  Status Code Breakdown and the Redirect Analysis now agree.

### Preserved

- All v1.10.1 behaviour preserved. The two earlier fixes
  (trailing-slash normalisation in `is_rotation_trigger_path()`
  and burst-counter decrement in `apply_rotation_override()`)
  continue to work exactly as before.
- Default text output remains byte-identical to v1.9.1 for any log
  that does not contain rotation traffic. Verified with `diff` on
  the clean synthetic log fixture: zero differences.
- JSON output remains byte-identical to v1.9.1 on the same fixture.
- No new dependencies, no schema changes, no CLI changes, no new
  report sections.
- `agg.high_traffic_redirect_first_seen` is not touched by the
  override. The map is keyed by path alone (not bot) and populated
  only for `canonical_loop` causes; rotation entries produce
  `trailing_slash` causes per `_classify_redirect_cause()`, so the
  map never contains rotation paths to begin with. Legitimate
  canonical-loop entries on unrelated paths survive untouched.
- Combined-format invocations work unchanged. The attribution
  migration silently skips when there is no attribution data to
  migrate.

### Verification

- 39-test suite (`test_v110.py`) green: 34 v1.10.1 tests + 5
  redirect-migration tests covering basic combined-format
  migration, seo_crawl-format attribution migration, legitimate
  Googlebot redirects preserved alongside attacker rotation, the
  Status-Code-Breakdown ↔ Redirect-Analysis consistency property,
  and the `high_traffic_redirect_first_seen` untouched invariant.
- Synthetic five-line seo_crawl reproduction of the 2026-05-16
  axilog.io leak (three 301s on the no-slash trigger paths plus
  two 404s, all from `5.255.104.83` under five named identities):
  Redirect Analysis now reads `SuspectedBotIdentityRotation:
  3 redirects` with the three trigger paths listed underneath.
  No named-bot rows in Redirect Analysis. Status Code Breakdown
  shows `SuspectedBotIdentityRotation: 301: 3, 404: 2`. The two
  views agree.
- Clean-log `diff` against v1.9.1: zero differences in text or JSON.

### Versioning note

This release is a patch (1.10.1 → 1.10.2) per the project's v1.x
posture preservation rule. No new flags, no JSON schema changes,
no new report sections, no behavioural changes for inputs that
don't contain rotation 3xx traffic. The fix is strictly migrating
two more counters that the v1.10.0 override should have migrated
from the start.

## v1.10.1 — 2026-05-16

Two-fix patch closing the false-positive failure modes surfaced by
the v1.10.0 production deployment on the 2026-05-16 axilog.io seo
log. Trailing-slash variants of rotation trigger paths were slipping
the trigger filter (`/api/env/` from `5.255.104.83` left a ClaudeBot
404 in the real-crawler 404 list; `/api/config/` left a BaiduSpider
404), and rotation 404s on non-probe trigger paths were inflating
the deploy-window burst counter even after reattribution
(producing a `[HIGH] Content-404 burst: 5 404s` finding on what was
actually unified attacker traffic from a single rotating IP).

### Fixed

- **`__version__` bumped from `1.10.0` to `1.10.1`.**

- **Trailing-slash variants of `ROTATION_TRIGGER_PATHS` now qualify
  for the rotation override.** `is_rotation_trigger_path()` strips a
  single trailing slash before the set lookup. `/api/env/` →
  `/api/env` matches; `/api/config/` → `/api/config` matches; etc.
  The root path `/` is preserved (the `len > 1` guard prevents
  rstrip from collapsing it to `""`, which would have meant root
  fetches falsely qualified). Query strings are stripped before
  normalisation so cache-buster probes like `/api/env?x=1` and
  `/api/env/?x=1` both qualify, matching the `clean_path` convention
  used elsewhere in the analyser.

  Motivated by the 2026-05-16 axilog.io production run: `5.255.104.83`
  hit both `/api/env` (caught) and `/api/env/` (slipped through) in
  the same rotation burst, leaving the trailing-slash 404s misclassified
  as real-crawler failures.

- **`apply_rotation_override()` decrements `minute_404s` and
  `minute_content_404s` for any rotation entry that originally
  contributed to them.** The gating conditions mirror the aggregate()
  increment gate exactly:
  ~~~
  if status == 404 and not is_probe and not is_framework:
      minute_404s[bucket] -= 1
      if category not in {robots, sitemap, rss, llms, static asset}:
          minute_content_404s[bucket] -= 1
  ~~~
  Probe-matched rotation entries never entered the burst counter in
  the first place (the aggregate gate skipped them), so the override
  correctly skips them on the way out. Verified with a mixed-fixture
  test: probe-path rotation does not double-decrement; non-probe
  rotation is stripped cleanly.

  Motivated by the same 2026-05-16 axilog.io run: five rotation
  404s from `5.255.104.83` within one minute fired the burst
  detector even after reattribution to
  `SuspectedBotIdentityRotation`, because the burst counter was
  populated during aggregation and never updated by the override.

### Preserved

- All v1.10.0 behaviour preserved except the two specific
  false-positive paths above. CLI flags, JSON schema, report
  section layout, classification semantics — all unchanged.
- Default text output remains byte-identical to v1.9.1 for any log
  that does not contain rotation traffic. Verified with `diff` on
  the clean synthetic log fixture: zero differences.
- JSON output remains byte-identical to v1.9.1 on the same fixture.
- No new dependencies, no schema changes, no CLI changes.
- Rotation summary fields store the path verbatim — operators see
  the exact form the attacker requested (`/api/env/` if that's
  what nginx logged), not a normalised canonical form. Normalisation
  is applied only inside `is_rotation_trigger_path()` for the
  set-membership check.
- Real content-404 bursts remain visible. The override decrements
  only rotation-attributable entries; legitimate Googlebot 404s in
  the same minute (e.g. a real deploy-window symptom from a
  different IP) continue to drive a `[HIGH] Content-404 burst`
  finding. Verified with a mixed-traffic test: rotation entries
  removed, legitimate burst still flagged.

### Verification

- 34-test suite (`test_v110.py`) green: 23 v1.10.0 tests + 7
  trailing-slash normalisation tests (helper-direct + end-to-end)
  + 4 burst-counter decrement tests (decrement gating, no
  double-decrement on probe paths, partial burst with legitimate
  traffic surviving).
- Synthetic five-line reproduction of the 2026-05-16 axilog.io
  failure mode (`/api/env/`, `/api/config/`, `/actuator/env/`,
  `/secrets.json`, `/appsettings.json` from `5.255.104.83` under
  five identities): "No crawler 404s recorded. ✓", "No
  deploy-window symptoms detected. ✓", and rotation section
  surfaces all 5 entries with all 5 identities. The two
  pre-v1.10.1 false positives are gone.
- Clean-log `diff` against v1.9.1: zero differences in text or JSON.

### Versioning note

This release is a patch (1.10.0 → 1.10.1) per the project's v1.x
posture preservation rule. No new flags, no JSON schema changes,
no new report sections, no behavioural changes for inputs that
don't trigger the two failure modes. The fix is strictly removing
attacker rotation traffic from two counters it should never have
inflated.

## v1.10.0 — 2026-05-16

Rotational bot-identity detection. The 2026-05-16 nginx-probe jail
deployment surfaced a single attacker IP (`5.255.104.83`) issuing
requests under five distinct named-bot user agents within a 60-second
window — Googlebot, GPTBot, Baiduspider, ClaudeBot, YandexBot — while
probing `/.env` variants, `/.git/config`, `/config.json`, `/api/env`,
and `/actuator/env` across speytech.com and axilog.io. No published
crawler operates under multiple identities from a single egress; this
is unambiguously an attacker probing the "do you trust this UA?"
surface. v1.9's `SuspectedUASpoof` classifier catches *behavioural*
spoofing (browser-UA on sitemap paths, no referrer) but not
*rotational* spoofing (one IP, multiple distinct named-bot UAs in
short succession across probe paths). v1.10 closes that gap and
restores accurate health scores on logs containing the pattern.

### Added

- **`SuspectedBotIdentityRotation` classification** applied as a
  second-stage post-aggregation override, mirroring v1.7's
  `SuspectedUASpoof` pattern. A request is reclassified when the
  first-stage classifier returned a named bot in
  `ROTATION_OVERRIDE_CANDIDATES`, the source IP was observed under
  ≥3 distinct named-bot UAs within a 5-minute window, and the path
  is in `ROTATION_TRIGGER_PATHS` or matches the existing
  `SECURITY_PROBE_PATTERNS` regex set.
- **`ROTATION_OVERRIDE_CANDIDATES`** — 24-entry frozenset of real
  named crawlers an attacker would plausibly claim to be (Googlebot,
  Bingbot, all four Claude variants, GPTBot, YandexBot, BaiduSpider,
  PerplexityBot, FacebookExternalHit, etc.). `GenericBot`,
  `UnknownBot`, `Empty-UA`, `Human/Other`, and `SuspectedUASpoof`
  are deliberately excluded — rotational identity claims are
  interesting only when the attacker is specifically pretending to
  be a *known trusted* crawler.
- **`ROTATION_TRIGGER_PATHS`** — 19-entry frozenset covering the
  post-v1.9 attack surface: `/config.json`, `/secrets.json`,
  `/appsettings.json`, `/credentials.json`, `/actuator{,/env,/heapdump}`,
  `/api/{env,config,secrets,credentials,admin,debug,users,auth,tokens,keys}`,
  `/vendor/composer/installed.json`. Path-matched exactly; broader
  probe-shaped paths are caught by the existing `is_security_probe()`
  helper.
- **Rotational Bot-Identity Detection** report section, positioned
  between Suspected UA-Spoof Detection and Framework Fingerprint
  Probes (the two attacker-noise classes adjacent in the report).
  Renders top rotating IPs with their claimed identity sets, top
  identity combinations across IPs (useful for spotting shared
  toolkit signatures), and top probed paths. Suppressed entirely
  when no rotation is detected.
- **`--rotation-window-minutes N`** (default: 5). Window over which
  multiple identities from one IP trigger the override. The
  2026-05-16 production rotation completed in ~60 seconds; the
  default leaves headroom for slower campaigns.
- **`--rotation-min-identities N`** (default: 3, floor: 2). Below 3,
  the signal is too weak — two identities could plausibly be a shared
  egress for two real crawlers. The floor is enforced in `main()`;
  values below 2 fall back to the default with a stderr warning.
- **`--show-rotation-detail`** — full per-request listing analogous
  to `--show-spoof-detail`.
- **`rotational_bot_identity` JSON key** (additive, conditional —
  omitted entirely when no rotation is detected). Carries `total`,
  `unique_ips`, `rotation_window_minutes`, `min_identities_threshold`,
  `top_ips` (each with `ip`, `requests`, sorted `identities`, and up
  to 10 sorted `paths`), `top_identity_combinations`, and `top_paths`.
- **`detect_rotation_indices()` helper.** Pure function over a per-IP
  identity log; window-walking algorithm from the brief §4.3. O(N²)
  worst case per IP, bounded by `ROTATION_LOG_CAP_PER_IP = 100`.
- **`apply_rotation_override()` helper.** Post-aggregation pass that
  re-attributes counters from named bots to
  `SuspectedBotIdentityRotation` across `bot_status`, `bot_urls`,
  `bot_url_status`, `bot_ips`, `special_files`, `crawler_404s`,
  `crawler_404_samples`, and the Googlebot-specific caches.
- **23-test unit suite** (`test_v110.py`) covering the eight §4.4
  boundary cases for the window walker, classification override
  behaviour across eligible/ineligible bots and trigger/non-trigger
  paths, the `ignore_source_ips` exemption, report rendering with
  and without rotation, JSON shape, an end-to-end test against a
  14-line synthetic log reproducing the exact 2026-05-16
  `5.255.104.83` pattern, and CLI flag wiring.

### Changed

- **Crawler-Visible 404s** drops rotation-attributable entries.
  Named-bot 404s from a rotating attacker IP are removed from the
  per-path per-bot 404 list, leaving only genuine real-crawler
  failures.
- **Googlebot Crawl Health Score** improves on logs containing
  rotation traffic. Rotation entries vanish from the Googlebot
  bucket before the score is computed, so 404-rate and redirect-rate
  deductions no longer fire on attacker attribution.
- **Search Crawler Health Score** likewise improves. On the
  2026-05-16 axilog.io seo log this is the score that flips from
  90/100 (with `-10: real-crawler 404 rate 2.6%`) back to 100/100 —
  the deduction was entirely driven by rotation entries.
- **AI Crawler Report** excludes rotation entries from the AI
  crawler cohort. An attacker claiming to be GPTBot or ClaudeBot
  no longer inflates AI crawler attention numbers.
- **Status Code Breakdown By Bot** retains the rotation
  classification as its own row, so operators see exactly how much
  traffic was reclassified alongside the legitimate per-bot totals.
- **Aggregate dataclass** gains `ip_identity_log`, `rotation_count`,
  `rotation_ip_requests`, `rotation_ip_identities`,
  `rotation_ip_paths`, `rotation_identity_combinations`,
  `rotation_path_counts`, `rotation_entries`,
  `rotation_window_minutes`, and `rotation_min_identities`.
- **`filter_for_bot()`** preserves rotation state through `--bot`
  narrowing. The rotation section is a behavioural signal independent
  of which named bot the operator is investigating, so it surfaces
  regardless of filter.

### Preserved

- All v1.9.x CLI invocations work unchanged. The three new flags
  are additive with safe defaults.
- Default text output is byte-identical to v1.9.1 for any log that
  does not contain rotation traffic. Verified with `diff` on a
  synthetic clean log: zero differences.
- JSON output is byte-identical to v1.9.1 for any log that does not
  contain rotation traffic. The new top-level key is conditional —
  omitted entirely (not `null`, not `{}`) when there's no rotation.
- No new dependencies. Single-file Python stdlib script preserved.
- Behaviour of `SuspectedUASpoof`, framework-probe opacity,
  AI crawler expectation, site context, and every other v1.9
  surface is unchanged.

### Notes

- **`ignore_source_ips` interaction.** Source IPs in the resolved
  ignore set are exempt from rotation candidate capture, matching
  v1.5's probe-noise filtering. An operator's own scripted multi-UA
  curl tests from a host VM will not be flagged.
  `--no-config-ignores` opts those IPs back into rotation detection
  for the same investigation reason it opts them back into the
  probe summary.
- **`SuspectedUASpoof` overlap.** `SuspectedUASpoof` is deliberately
  NOT in `ROTATION_OVERRIDE_CANDIDATES`. A spoof-classified request
  never enters the rotation candidate pool; the two classifications
  cannot compete on the same entry.
- **False positives.** Rotational identity claims are unambiguous in
  a way the v1.7 spoof heuristic is not — there is no legitimate
  operational reason for one IP to claim three different named-bot
  identities while probing security-sensitive paths. The
  min-identities floor of 3 protects against the only theoretical
  edge case (shared datacentre egress for two real crawlers). If a
  false positive does emerge, `--rotation-window-minutes` tightens
  the window without rebuild.

### Verification

- 23-test suite (`test_v110.py`) green: 8 window-walker boundary
  cases (§4.4), 7 classification-override tests, 2 report-rendering
  tests, 3 JSON-shape tests, 1 end-to-end test through `main()` on
  the 14-line synthetic 2026-05-16 fixture, 2 CLI-flag tests.
- Backward-compatibility check: `diff` of v1.9.1 vs v1.10.0 text
  output on a clean synthetic log produces zero output; same for
  `--format json`. Both formats are byte-identical when no rotation
  is present.
- Synthetic 2026-05-16 reproduction (`5.255.104.83` claiming six
  identities across 11 probe requests, plus three legitimate
  Googlebot fetches from `66.249.66.10`): rotation section renders
  correctly with all six identities listed; `bot_breakdown.Googlebot`
  shows the three legitimate 2xx hits unchanged; both health scores
  remain at 100/100.
- Production verification recipe in `docs/VERIFICATION-v1.10.0.md`
  with the five expected outcomes for the 2026-05-16 axilog.io
  seo log re-run.

### Versioning note

This release is a minor (1.9.1 → 1.10.0) per the project's v1.x
posture: new CLI flags, new classification, new report section, new
conditional JSON key — all additive. No removed features. No CLI
behaviour changes for existing flags. Zero risk to v1.9.x cron
invocations.

## v1.9.1 — 2026-05-16

Single-line bugfix for a false-positive in the framework fingerprint
probe detector.

### Fixed

- `__version__` bumped from `1.9.0` to `1.9.1`.

- `/.well-known/pgp-key.txt` is now exempt from the framework fingerprint
  probe pattern, alongside the existing `/.well-known/security.txt`
  exemption. Both files are legitimate site assets — the former being
  the published PGP key referenced by the latter under RFC 9116. Previously
  every crawler fetch of `/pgp-key.txt` produced a "Build artefact(s)
  exposed" warning in the daily report, which was a false positive.

  The negative lookahead in `FRAMEWORK_PROBE_PATTERNS` changes from:
  ~~~
  re.compile(r"^/\.well-known/(?!security\.txt$)", re.IGNORECASE)
  ~~~
  to:
  ~~~
  re.compile(r"^/\.well-known/(?!security\.txt$|pgp-key\.txt$)", re.IGNORECASE)
  ~~~

  Motivated by today's deployment of `/.well-known/pgp-key.txt` on
  speytech.com and axilog.io as part of RFC 9116 disclosure infrastructure.
  Verified post-fix: the speytech.com daily report flipped from a
  "Build artefact(s) exposed" warning to "Site is opaque to framework
  fingerprinting" — the latter being correct.

### Preserved

- All v1.9.0 behaviour preserved exactly. The negative lookahead change
  only affects the framework probe classifier; everything else
  (config parsing, AI crawler expectation, opacity scoring, JSON
  schema, CLI flags) is unchanged.
- No new dependencies, no schema changes, no CLI changes.
- Output for inputs that don't contain `/.well-known/pgp-key.txt` requests
  is byte-identical to v1.9.0.

### Verification

- Manual regression test on 12 path edge cases covered legitimate files
  (must NOT match), backup-probe attempts (MUST match), case sensitivity,
  future RFC 9974 (`change-password`) paths, and typo-variant paths.
  All 12 cases passed.
- Production verification: re-ran daily report against
  `/var/log/nginx/speytech.com.seo.log` after deploy. The "Build
  artefact(s) exposed" warning is gone; the report now correctly
  classifies the site as opaque to framework fingerprinting.

### Versioning note

This release is a patch (1.9.0 → 1.9.1) per the project's v1.x posture
preservation rule. No new flags, no JSON schema changes, no new
report sections.

## 1.9.0 — 2026-05-15

Three "explained absence" features. v1.8 reports correctly flagged the
absence of AI crawler activity, framework-probe responses, and per-site
context, but treated each absence as a bare count or a silent gap.
v1.9 turns each into a classified, contextual signal — now that
multi-site data (one mature property, one one-day-old) exists to
calibrate against.

### Added

- **[sites."<hostname>"] config table.** Per-site metadata keyed by the
  `$host` field captured in the seo_crawl log format. Currently carries
  `launch_date` (ISO 8601); the shape is open for future per-site keys
  (latency thresholds, expected traffic floors) without a schema
  rewrite. The analyser reads the host from the first log entry and
  looks up the matching table.
- **[ai_crawlers] config table** with `expected_discovery_window_days_min`
  and `expected_discovery_window_days_max` (defaults 7 and 21). Both
  bounds validated against `[1, 365]`; `min > max` reverts to defaults
  with a warning.
- **AI Crawler Report site-context block.** When no AI crawler activity
  is recorded *and* a launch date is configured, the report classifies
  the absence as one of:
    - `too_early` — site age < window_min; absence expected, revisit-after
      date computed as `launch + (min+max)//2`
    - `in_window` — `window_min ≤ age ≤ window_max`; activity may begin
      any day
    - `overdue` — age > window_max; absence worth investigating, with a
      three-line diagnostic checklist (llms.txt indexing, search-console
      submission, robots.txt restrictions)
    - `active` — at least one AI crawler hit the site; v1.8 wording
      preserved verbatim
  When the launch date isn't configured (or the log is combined-format
  with no host field), classification falls through to `unknown` and the
  v1.8 single-line wording is preserved.
- **Framework Fingerprint Probes opacity signal.** When every framework
  probe returns a 4xx (the static-site case), the section now reads:
    > ✓ Site is opaque to framework fingerprinting. No build-tool
    >   manifests are exposed.
  When any probe returns 2xx or 3xx, the wording flips to a warning
  listing the exposed paths and statuses. The underlying probe path
  list is unchanged.
- **`--show-config` output extended** with `Sites configured:` and `AI
  crawler discovery window:` sections so the operator can verify the
  v1.9 surfaces parsed cleanly without running a full report.
- **JSON output additions** (additive — no existing keys changed shape):
    - `site.{host, launch_date, age_days}` — emitted when launch date
      is configured for the captured host
    - `ai_crawlers.discovery_window.{min, max, units}` — emitted whenever
      a config is in play
    - `ai_crawlers.expectation` — one of the five classifications above
    - `framework_probes.opacity` (bool) and `framework_probes.exposed_paths`
      (list of `{path, status}`) — emitted whenever framework probes
      are detected
- **`compute_site_age(launch_date, reference)` helper.** UTC date-based;
  clamps future-dated launches to 0 rather than returning negative.
- **`classify_ai_crawler_expectation(agg, config)` helper.** Returns
  `(classification, age_days, launch_date)`. Pure function over the
  Aggregate and ResolvedConfig — easy to unit-test in isolation.
- **28-test unit suite** (`test_v19.py`) covering classification
  boundaries, opacity branching, age computation around UTC midnight,
  and seven bad-config validation paths (malformed dates, inverted
  windows, out-of-range values, unknown subkeys, boolean-as-integer).

### Changed

- **Python version floor raised to 3.11.** Hard requirement, checked at
  the top of `main()` with a clear exit message. The previous 3.8–3.10
  fallback TOML parser surface is removed.
- **Framework probe section wording** generalised from "they are 404s
  on a static Astro site" (always true in v1.8) to "they are excluded
  from the content-404 burst detector regardless of response code"
  (true for both the opacity and exposure cases).
- **Aggregate dataclass** gains `host: str | None` (captured from the
  first seo_crawl log entry) and `framework_probe_status:
  dict[str, Counter[int]]` (per-path status tally, populated alongside
  the existing `framework_probe_paths` for backward compatibility).
- **ResolvedConfig dataclass** gains `sites`, `ai_discovery_window_min_days`,
  and `ai_discovery_window_max_days` fields.

### Removed

- **`_parse_toml_subset()` and `_toml_subset_resolve_section()`** — the
  Python 3.8–3.10 fallback TOML parser, ~210 lines. Net effect: the
  analyser now uses stdlib `tomllib` exclusively, removing the
  subset-of-features caveat and any divergence risk between the
  two parser paths.
- **`try/except ImportError` branching in `load_config()`** — now a
  direct `import tomllib`.

### Migration notes

Operators running v1.8 on Python 3.8–3.10 will hit a clean exit message
on first invocation directing them to upgrade. Config files written
for v1.8 (the `ignore_source_ips` key) parse unchanged under v1.9.

To enable the new v1.9 features, append to `~/.config/crawler-log-analyser/config.toml`:

```toml
[sites."<your-hostname>"]
launch_date = "YYYY-MM-DD"

[ai_crawlers]
expected_discovery_window_days_min = 7
expected_discovery_window_days_max = 21
```

Verify with `crawler_log_analyser.py --show-config` before running a
full report.

### Validated against

Live production logs for axilog.io (launched 2026-05-14, in IndexNow
post-launch crawl window) and speytech.com (launched 2026-01-25,
mature steady-state with active AI crawler cohort). Both reports
read cleanly: axilog produces "Site context: axilog.io launched 1
day ago … revisit after 2026-05-28"; speytech preserves the v1.8
wording for the active branch unchanged.

## v1.8.0 — 2026-05-14

One operational improvement deferred from the v1.7 requirements list:
operator IP defaults via config file. Previously every daily run had
to pass `--ignore-source-ip 35.230.156.201` explicitly to filter
operator self-test traffic out of the probe-noise summary; v1.8.0
reads a standing config file so the standing default doesn't need to
be re-typed.

This is also the first stateful file the analyser reads (config is
operator-managed, but the precedent matters). The design is read-only
at run time and additive on top of CLI flags, so the disciplined
"single-file deployment, no external dependencies" property of the
project is preserved.

### Added

- **Config file support** at one of the following locations, searched
  in order (first match wins):

  1. `--config PATH` (explicit override)
  2. `$XDG_CONFIG_HOME/crawler-log-analyser/config.toml`
  3. `~/.config/crawler-log-analyser/config.toml`
  4. `~/.crawler-log-analyser.toml`

  The file is TOML. On Python 3.11+ the analyser uses the stdlib
  `tomllib` parser (full TOML spec). On 3.8-3.10 a minimal fallback
  parser handles the v1.8 subset (string values, string arrays,
  comments, blank lines). No new external dependency in either case.

- **`ignore_source_ips`** config key. List of IPs to exclude from
  probe-classification aggregation. Equivalent to passing
  `--ignore-source-ip` repeatedly. Example:

  ~~~toml
  ignore_source_ips = [
      "35.230.156.201",  # Axioma host self-tests
      "10.0.0.5",        # internal monitoring
  ]
  ~~~

- **`--config PATH`** to specify a non-default config file location.
  An explicit path is used verbatim; if the file doesn't exist, the
  analyser emits a stderr warning and continues without config.

- **`--show-config`** prints the resolved configuration and exits 0.
  Shows the source file path, IPs supplied by config, IPs supplied
  by CLI, the merged effective set, any unknown config keys, and any
  parse warnings. Designed for cron-job verification and for
  diagnosing config-file issues without enabling debug output.
  Does not require log paths — useful as a config sanity check
  before scheduling.

- **`--no-config-ignores`** discards the config-supplied
  `ignore_source_ips` for a single run while keeping any CLI
  `--ignore-source-ip` values. Use this for ad-hoc investigation
  runs where the standing config defaults would mask data the
  operator wants to see.

- Example `examples/config.toml` documenting the format with a
  worked operator-IP entry.

- Verification recipe in `docs/VERIFICATION-v1.8.0.md`.

### Merge semantics

CLI flags **extend** the config file rather than replacing it.

| Setting | Effect on `ignore_source_ips` |
|---------|-------------------------------|
| Config file with IPs, no CLI flags | Config IPs applied |
| Config file with IPs, CLI flags too | Both lists merged (union) |
| Config file with IPs, CLI `--no-config-ignores`, no CLI IPs | Empty set |
| Config file with IPs, CLI `--no-config-ignores`, CLI IPs | CLI IPs only |
| No config file, CLI IPs | CLI IPs only |
| No config file, no CLI IPs | Empty set (v1.7 behaviour) |

Deduplicated via frozenset; an IP listed in both config and CLI
counts once.

### Failure modes (all non-fatal)

A config file problem must never block the daily report. The
following all produce a stderr warning and continue with whatever
could be parsed, falling back to an empty config when nothing could
be:

- Config file path is non-existent (explicit `--config`)
- Config file exists but is unreadable (permissions)
- Config file contains malformed TOML
- `ignore_source_ips` has wrong type (e.g. a string instead of a list)
- Config file contains keys the analyser doesn't recognise (likely
  future-version keys — accepted gracefully so v1.8 invocations
  against newer config files don't fail)

Warnings are deterministic and prefixed with `warning:` for grep
filtering in cron logs.

### Changed

- The positional `paths` argument is now `nargs="*"` (was `nargs="+"`)
  so `--show-config` can run without supplying log files. When `paths`
  is empty and `--show-config` is not set, the analyser still emits
  `error: no log files to analyse` and exits 2, matching v1.7
  behaviour for that scenario.

### Preserved

- All v1.7.0 CLI invocations continue to work unchanged. The four
  new flags (`--config`, `--show-config`, `--no-config-ignores`, and
  the implicit config-file discovery) are additive with defaults
  that preserve v1.7 behaviour.

- **Byte-identical text output to v1.7.0 for any input when no config
  file is present.** Verified by md5sum on identical input log:
  v1.7.0 and v1.8.0 produce identical output (md5:
  `c4614a7d3002756416e4339e60fd47a8` on the smoke-test log fixture).

- JSON output schema is unchanged. No new top-level keys. v1.7.0
  consumers see identical JSON.

- All v1.7 health-score logic, spoof detection, named bot patterns,
  and post-IndexNow window suppression behave identically.

- Zero new dependencies. The program remains a single-file Python
  stdlib script. tomllib on 3.11+ is stdlib; the fallback parser on
  3.8-3.10 is internal.

### Notes

The v1.7 requirements doc captured three deferred items. v1.8.0 ships
item 1 only:

- **Config file defaults** (this release) — solves a daily-friction
  problem (the `--ignore-source-ip 35.230.156.201` repetition).
- **Cross-day spoofer ASN/CIDR tracking** — deferred to v1.9 or
  later. Two days of v1.7 data showed two Vietnamese-broadband-range
  spoofers from `14.169.0.0/16`, which is suggestive but not yet a
  reliable pattern. Premature design here means designing for one
  observation. Worth re-evaluating after a fortnight of v1.8
  daily-report data.
- **CSP log parsing** — different log format, belongs in a separate
  `csp-log-analyser` tool (not this one).

The choice to ship one feature at a time, with explicit verification
of "v1.7 output == v1.8 output when feature not in use", continues
the v1.x posture established by earlier releases.

### Verification

- 27 v1.8-specific tests in `test_v18.py`: 6 path-discovery scenarios,
  6 load_config failure modes, 5 fallback-parser cases, 4 merge-semantics
  cases, 6 end-to-end CLI cases.

- 36 v1.7 regression tests in `test_v17.py` still pass against the
  v1.8 build.

- Backward compatibility confirmed by md5sum: text output for a
  combined-format log with no config file in the discovery search
  order is byte-identical to v1.7.0 output for the same input.

- Verification recipe in `docs/VERIFICATION-v1.8.0.md`.

---

## v1.7.0 — 2026-05-14

Three operational improvements driven by two days of running v1.6.0
against the speytech.com production log. None are urgent. All are
bounded — they reduce daily-report noise, increase signal quality, or
surface a class of traffic the analyser previously missed.

### Added

- **Four crawlers promoted out of `GenericBot`** into named entries:

  | Name | Domain | Notes |
  |------|--------|-------|
  | DotBot | Moz backlink crawler | SEO tool, not user-facing search |
  | Qwantbot | Qwant search engine | European / French regional search |
  | SeznamBot | Seznam.cz | Czechia's major search engine |
  | SERankingBacklinksBot | SE Ranking | SEO tool, same category as DotBot |

  All four classify with `is_ai=False`. Deliberately **not** added to
  `SEARCH_CRAWLERS` — the broader search-crawler health score targets
  major user-facing search engines, and including SEO tools and
  regional crawlers would dilute that signal.

  Operator-visible effect: the four bots appear as named rows in the
  Status Code Breakdown By Bot section instead of being aggregated into
  GenericBot. The `GenericBot` count drops accordingly.

- **UA-spoof behavioural detection** — a new classification
  `SuspectedUASpoof` fires when a request matches all three of:

  1. UA matches a major browser engine (Firefox / Chrome / Safari /
     Edg under `Mozilla/5.0`)
  2. Referrer is empty (`-` or absent)
  3. Path is in the SEO infrastructure set (`/robots.txt`,
     `/sitemap.xml`, `/sitemap-0.xml`, `/sitemap-index.xml`,
     `/sitemap.txt`, `/llms.txt`, `/llms-full.txt`)

  The combination is incompatible with human browsing: real browser
  sessions don't navigate directly to `/sitemap-0.xml` with no
  referrer. The override fires only when the first-stage classifier
  returned `Empty-UA`, `Human/Other`, or `GenericBot` — requests
  matching a named bot pattern are left alone.

  `/rss.xml` is deliberately excluded from the trigger set; some
  legitimate RSS readers spoof browser UAs to bypass anti-bot measures.

  Motivation: the May 13 Firefox-UA analysis surfaced five requests
  from five distinct IPs, each spoofing a different Firefox version,
  each fetching a sitemap-class path with empty referrer. v1.6 bucketed
  these as anonymous browser traffic and silently dropped them from
  the AI Crawler Report, Status Code Breakdown, and most other
  analyses.

- **New report section: Suspected UA-Spoof Detection.** Rendered
  immediately after "Suppressed Security Probe Noise" when spoof
  traffic is observed:

  ~~~
  Suspected UA-Spoof Detection
  ────────────────────────────

  4 requests across 4 unique IPs exhibited browser-UA + sitemap-fetch behaviour
  that is incompatible with human browsing sessions.

  Top spoofed UA strings:
       1 × Firefox/133.0
       1 × Firefox/126.0
       1 × Firefox/124.0
       1 × Chrome/120.0.0.0

  Top source IPs:
       1 requests from 5.255.103.97         (fetched /sitemap.txt)
       1 requests from 14.169.63.28         (fetched /sitemap-0.xml)
       1 requests from 198.51.100.7         (fetched /sitemap-index.xml)
       1 requests from 203.0.113.42         (fetched /llms.txt)

  Use --show-spoof-detail to include full per-request listing.
  ~~~

  The section is suppressed entirely when no spoof entries are
  observed.

- **`--show-spoof-detail`** appends a full per-request listing
  (timestamp, IP, status, path, UA label) to the spoof detection
  section. Useful for triaging specific IPs.

- **`--post-indexnow-window-hours N`** (default 4) configures the
  redirect-rate suppression window. See "Changed" below for the
  scoring-rule update this flag controls.

- **Severe redirect-rate tier at ≥50%** in the Googlebot Crawl Health
  Score. Above this rate the deduction always fires regardless of
  window — the rate is incompatible with normal post-IndexNow
  revalidation and indicates a genuine configuration loop.

- **New JSON key `suspected_ua_spoof`** with structured spoof data:

  ~~~json
  "suspected_ua_spoof": {
    "total": 4,
    "unique_ips": 4,
    "top_ua_labels": [["Firefox/133.0", 1], ["Firefox/126.0", 1]],
    "top_ips": [
      {
        "ip": "5.255.103.97",
        "requests": 1,
        "paths": ["/sitemap.txt"]
      }
    ]
  }
  ~~~

  Key is omitted entirely (not `null` or `{}`) when no spoof entries
  are observed.

### Changed

- **Googlebot Crawl Health Score redirect-rate deduction is now
  window-aware between 25% and 50%.** The previous fixed-threshold
  deduction at >25% was producing false negatives during post-IndexNow
  revalidation bursts — Googlebot legitimately fetches every redirect
  rule aggressively after a freshness-signal ping, pushing the rate
  above 25% while every redirect is resolving cleanly.

  v1.7.0 bands the redirect rate as:

  | Rate | Behaviour |
  |------|-----------|
  | < 25% | no deduction (unchanged) |
  | 25–50% in post-IndexNow window | no deduction, INFO note rendered |
  | 25–50% outside window | -5 deduction (unchanged) |
  | ≥ 50% | -5 deduction with "(severe)" suffix, ignores window |

  The post-IndexNow window is detected via a proxy: the first-seen
  timestamps of URLs already qualifying as high-traffic redirects
  (`canonical_loop` count ≥ 3, < 50 — i.e. working redirects under
  heavy crawler load). When one or more such URLs first appeared
  within `--post-indexnow-window-hours` of `agg.latest`, the analyser
  infers that Googlebot is mid-revalidation sweep.

  In the suppression case, the reasons list contains an INFO line
  instead of a deduction:

  ~~~
  INFO: Googlebot redirect rate 27.4% — within post-IndexNow revalidation window (deduction suppressed)
  ~~~

  Calibrated empirically from the 2026-05-14 production data: 407
  redirects / 1,488 total = 27.4% Googlebot redirect rate, every
  redirect confirmed working by seo-validator v7.4 section 21, all
  driven by the morning's IndexNow ping. Under v1.6.0 this scored
  95/100 with a misleading -5 deduction. Under v1.7.0 it scores
  100/100 with an INFO note documenting the suppression.

- **Health-score section heading switches from "Deductions:" to
  "Notes:"** when the reasons list contains no actual deductions
  (i.e. only INFO suppression notes). Cosmetic, but reads honestly:
  the previous label was misleading in the new INFO-only case.

### Preserved

- All v1.6.0 CLI invocations continue to work unchanged. The two new
  flags (`--post-indexnow-window-hours`, `--show-spoof-detail`) are
  additive with sensible defaults.

- Default text output is byte-identical to v1.6.0 for any log that
  doesn't contain v1.7-triggering traffic. Verified by md5sum
  comparison on a clean 4-line synthetic log: v1.6 and v1.7 produce
  identical output (md5: `e3b5fdd5002dcb79680005fe57bcc6e8`).

- JSON output adds one conditional top-level key (`suspected_ua_spoof`).
  v1.6.0 consumers that ignore it see identical JSON. The key is
  omitted entirely when no spoof entries are observed.

- The redirect-rate logic produces strictly more favourable scores in
  post-IndexNow windows. No regression for any input — the only
  behaviour change is suppression of false-positive deductions.

- `SuspectedUASpoof` entries are excluded from `crawler_404s`, the
  Googlebot Crawl Health Score cohort, and the broader Search Crawler
  Health Score cohort. Spoof traffic is noise, not search signal;
  including it would skew the per-URL 404 view and the score
  computations. Spoof entries do appear in the Status Code Breakdown
  By Bot, so operators retain visibility into the volume.

- Zero new dependencies. The program remains a single-file Python
  stdlib script.

### Notes

The v1.7 requirements document captured three confirmed features
(named bot patterns, UA-spoof detection, post-deploy redirect rate
handling) and four deferred items:

- **Config file defaults** for operator IPs — UX improvement, deferred
  to v1.8 as a separate behaviour-change release.
- **CSP log parsing** — different log format, different data source;
  belongs in its own tool (filed as v1.0 candidate for a separate
  `csp-log-analyser` script).
- **Build determinism check** — operationally adjacent to crawler
  analysis but better hosted in seo-validator.
- **IndexNow URL set dump** — interesting but not action-driving;
  defer until a clear operational reason emerges.

Two known false-positive cases for the UA-spoof detector documented
for users:

- Pre-rendering tools (Vercel, Netlify, etc.) that use browser UAs and
  fetch sitemaps as part of build pipelines would be classified as
  spoofers. False-positive rate is low; pre-rendering happens from
  known cloud IPs.
- Operator-issued `curl` tests with custom browser UA strings against
  sitemap paths will be flagged. This is by design — the request
  shape is bot-like, and the existing `--ignore-source-ip` flag does
  not apply (that flag affects probe classification, not spoof
  classification).

### Verification

- 36 synthetic tests pass against the requirements doc test matrix:
  8 tests for Feature 1 (named bot patterns + SEARCH_CRAWLERS guard),
  14 tests for Feature 2 (UA-spoof detection across the 6 requirements
  scenarios + 5 production fixtures + 3 edge cases),
  9 tests for Feature 3 (redirect-rate tier + window + edge cases),
  3 tests for `in_post_indexnow_window` configurability,
  2 tests for cross-cutting backward compatibility.

- End-to-end smoke test against a synthetic 13-line log mixing all
  v1.7 traffic classes produces the expected report sections with
  correct cross-counting.

- Backward compatibility confirmed by md5sum: text output for a
  combined-format log with no v1.7-triggering traffic is byte-identical
  to v1.6.0 output for the same input. JSON output for the same log
  omits the `suspected_ua_spoof` key entirely, producing identical
  schema to v1.6.0.

- Verification recipe in `docs/VERIFICATION-v1.7.0.md`.

---

## v1.6.0 — 2026-05-13

Two operational improvements driven by two days of running v1.5.0 against
the speytech.com production log. Both came directly from real findings
in the daily report rather than speculative feature work.

### Added

- **AI crawler content-depth preference subsection** in the AI Crawler
  Report. For each AI crawler that has fetched at least one of
  `/llms.txt` or `/llms-full.txt` during the report period, classifies
  the depth preference as one of:

  | Pattern | Label |
  |---------|-------|
  | 100% one depth, 0 of the other | `exclusive: full-content` / `exclusive: index-only` |
  | >= 75% one depth, >0 of the other | `prefers full-content` / `prefers index-only` (with percentage) |
  | 25-75% split | `mixed` (with percentage breakdown) |
  | Single fetch total | `insufficient data` |

  Example output:

  ~~~
  Content-depth preference (LLMs file fetches)
    Amazonbot
      llms.txt: 1, llms-full.txt: 19
      preference: prefers full-content (95% full)
    ChatGPT-User
      llms.txt: 5, llms-full.txt: 0
      preference: exclusive: index-only
  ~~~

  Crawlers that have only hit `/robots.txt` (e.g. ClaudeBot during the
  v1.5.0 → v1.6.0 window) are filtered out of this subsection — they
  still appear in the main AI Crawler Report listing but don't carry a
  depth signal worth surfacing here.

  The distinction between "exclusive" and "prefers" preserves an
  operationally meaningful signal: "exclusive" tells the operator the
  crawler has a hard preference (informative for AEO file-publication
  strategy); "prefers" tells them the crawler will sometimes fetch the
  other depth.

- New JSON key `ai_crawler_depth_preference` with the same classification
  data:

  ~~~json
  "ai_crawler_depth_preference": {
    "Amazonbot": {
      "llms_txt_fetches": 1,
      "llms_full_txt_fetches": 19,
      "full_content_ratio": 0.95,
      "preference": "prefers full-content (95% full)"
    }
  }
  ~~~

  Key is omitted entirely (not `null` or `{}`) when no AI crawler has
  fetched any LLMs file during the period.

### Changed

- **Canonical-loop CRITICAL threshold raised from `>= 3` to `>= 50`,
  with a new `[high-traffic redirect]` annotation for counts in
  the 3-49 band.** The previous threshold was producing false CRITICAL
  flags for working redirects under heavy crawler revalidation. Today's
  daily report showed three working 301s flagged CRITICAL (counts 22,
  11, 11) that the same morning's seo-validator v7.4 section 21
  confirmed were resolving cleanly.

  v1.6.0 distinguishes three bands:

  | Count | Annotation | Severity |
  |-------|------------|----------|
  | 1-2 | (silent) | INFO |
  | 3-49 | `[high-traffic redirect]` | INFO |
  | >= 50 | (none — the `[CRITICAL]` prefix conveys it) | CRITICAL |

  Before (v1.5.0):

  ~~~
    [CRITICAL]  22 × /images/cardiocore-litigation.svg  (22 canonical-loop)
    [CRITICAL]  11 × /contact-us/  (11 canonical-loop)
     2 × /legacy-rename/  (2 canonical-loop)
  ~~~

  After (v1.6.0):

  ~~~
     22 × /images/cardiocore-litigation.svg  (22 canonical-loop) [high-traffic redirect]
     11 × /contact-us/  (11 canonical-loop) [high-traffic redirect]
      2 × /legacy-rename/  (2 canonical-loop)
    [CRITICAL] 100 × /genuinely-looping/  (100 canonical-loop)
  ~~~

  Thresholds are exposed as module-level constants for future tuning:

  ~~~python
  CANONICAL_LOOP_HIGH_TRAFFIC_THRESHOLD = 3
  CANONICAL_LOOP_CRITICAL_THRESHOLD = 50
  ~~~

  Calibrated empirically from production data showing working redirects
  clustering in the 11-22 range under Googlebot revalidation after
  IndexNow pings. The 50 boundary leaves a generous buffer above
  observed working values while remaining low enough to catch a genuine
  loop early.

### Notes

The v1.6 requirements document captured seven candidate features.
v1.6.0 ships items 1 and 2 only. Deferred items:

- **Item 3** (operator IP defaults via config file) — UX improvement,
  doesn't change reporting behaviour. Worth a separate small release
  where it can stand on its own.
- **Item 4** (CSP violation log parsing) — different log source (JSON,
  not nginx-text). Better as a separate tool than wired into this
  analyser.
- **Items 5-7** (build determinism check, IndexNow URL set dump,
  ClaudeBot family aggregation) — low operational urgency, accumulate
  findings before scoping.

### Preserved

- Text output for combined-format input (no `redirect_attribution` data)
  remains byte-identical to v1.5.0. The fallback heuristic notes are
  unchanged.
- JSON output adds one conditional top-level key
  (`ai_crawler_depth_preference`). v1.5.0 consumers that ignore it see
  identical JSON.
- The `redirects` section structure is unchanged. Only the `[CRITICAL]`
  prefix and `[high-traffic redirect]` suffix render changes are
  affected by the threshold tuning.
- No new CLI flags. Every v1.5.0 invocation continues to work unchanged.

### Verification

- 8 unit tests covering boundary behaviour at counts 0/1/2/3/22/49/50/100
  and all 12 depth-preference classification cases.
- End-to-end smoke test against synthetic seo_crawl log producing the
  three redirect bands and three AI crawler patterns.
- Backward compatibility confirmed: combined-format input renders
  identically to v1.5.0; JSON omits the new key when no LLMs activity.

Verification recipe in `docs/VERIFICATION-v1.6.0.md`.

---

# v1.5.0 — 2026-05-13

Two targeted operational improvements following two days of intensive use against the speytech.com production log. Both came directly from real findings in the daily analyser report rather than speculative feature work.

## Added

- **`--ignore-source-ip IP`** — exclude a source IP from probe-classification aggregation. Pass once per IP to filter multiple addresses. Typical use: filter operator self-test traffic from the host VM (e.g. `--ignore-source-ip 35.230.156.201`) so curl tests originating from the same server as nginx don't inflate the daily probe-noise summary. Does not affect bot statistics, health scores, or any other classification — only the "Suppressed Security Probe Noise" tally and its associated path/IP counters. Other entries from the ignored IPs still flow through to every other aggregation normally.

## Changed

- **Canonical-loop criticality threshold raised to count ≥ 3.** Previously, any redirect classified as `canonical_loop` was flagged `[CRITICAL]`. This produced false positives for legacy path renames (e.g. `/contact-us/` → `/contact/` from a site rebranded years before nginx was reconfigured) where a single crawler visit produces a single redirect.

  A true configuration-bug canonical loop — the case the CRITICAL flag is designed to catch — manifests as the same crawler hitting the same path repeatedly, retrying after each redirect. Such loops easily exceed the new threshold. Single-digit canonical-loop counts almost always represent legacy path renames and now render without the CRITICAL prefix, though the `canonical-loop` classification label is preserved in the annotation so the underlying signal is still visible.

  If a true low-count canonical loop ever needs to be flagged, the threshold can be lowered to 2 in `_redirect_note()`.

## Operational notes

The `--ignore-source-ip` flag was motivated by a recurring pattern in the daily reports: operator verification curls from Axioma (35.230.156.201) appearing in the probe-noise IP list, indistinguishable from external scanner traffic. Without filtering, every verification run that exercises `/api/csp-report.php` or similar inflates the probe count and skews the top-IP list. The flag does not exclude the operator IP from any other classification — Googlebot UA spoofing tests from the operator IP, for example, would still flow through to the Googlebot statistics correctly.

The canonical-loop threshold change was motivated by `/contact-us/` showing as `[CRITICAL]` on every daily report despite being a benign legacy path rename. The fix preserves operational visibility (the classification label still appears) while removing the false-alarm severity that was causing the strict-mode exit code path to flag legitimate site states as critical.

## Compatibility

Both changes are backward compatible:

- `--ignore-source-ip` is opt-in; existing invocations without the flag produce byte-identical output to v1.4.1 for the same input.
- The canonical-loop threshold change affects output only when the underlying classifier already identified a canonical-loop case. For logs with no canonical-loop entries (the common case), output is byte-identical to v1.4.1.

The JSON output schema is unchanged. The redirect attribution data structure still includes `canonical_loop` counts; only the rendering of the CRITICAL prefix changed.

## Verified

- Syntax validation via `ast.parse`
- `--version` reports `1.5.0`
- `--ignore-source-ip` filter verified end-to-end against production seo log: filtering one IP drops probe count from 368 to 124; filtering two drops to 39 (sums match per-IP probe contributions exactly)
- Multi-flag composition (`--ignore-source-ip A --ignore-source-ip B`) works as expected via `action="append"`
- No regression on existing test data: identical output structure for runs without the new flag

## v1.4.1 — 2026-05-11

Bug fixes for two issues surfaced by real-traffic review of the May 11
morning report on speytech.com.

### Fixed

- **Redirect attribution numbers now reconcile per-bot.** v1.3 introduced
  per-path redirect attribution but keyed the attribution counter by
  path alone, while the redirect count next to it was per-bot. Effect:
  a row like `6 × /` for Googlebot displayed `(5 HTTP→HTTPS, 4 www→apex)`
  — totalling 9 because it was the site-wide sum across all bots that
  hit `/`, not Googlebot's six. The attribution is now keyed
  `[bot][path][cause]` so cause-counts sum to the redirect count
  alongside them.

  Verified against the May 11 seo log: every bot/path row now
  reconciles. The site-wide attribution remains available by summing
  across bots in the JSON output.

- **Sub-millisecond latency percentiles now render as `<1ms` rather
  than `0.000s`.** When percentiles round below the millisecond display
  threshold but the slowest-N list contains higher-latency outliers,
  the previous rendering read as a contradiction: "p99 is zero but the
  slowest request took 184ms." The new helper renders any value below
  1ms as `<1ms`, which is honest about the display threshold without
  implying zero latency.

- **Empty paths in the slowest-N list now render as `(empty)` instead
  of a blank column.** Empty paths arise from probe traffic with
  unusual request shapes (e.g. `GET // HTTP/1.1`). The blank column
  was visually confusing.

### Schema change (minor, additive)

- The JSON `redirect_attribution` payload changes shape from
  `{path: {cause: count}}` to `{bot: {path: {cause: count}}}`. Consumers
  that aggregated per-path attribution previously can sum across the
  bot dimension to recover the old shape:
  ```python
  per_path = collections.Counter()
  for bot, paths in data["redirect_attribution"].items():
      for path, causes in paths.items():
          for cause, n in causes.items():
              per_path[(path, cause)] += n
  ```
  The new shape is structurally correct and matches the text/markdown
  rendering.

### Preserved

- Combined-format output remains byte-identical to v1.3 and v1.4.0.
  The fixes only affect output when seo_crawl data is present — which
  is the only context where redirect attribution and per-request
  latency are populated.
- All CLI flags, exit codes, and the strict-mode contract are unchanged.

### Verification

Tested against speytech.com seo log (1,190 lines, 854 crawler requests,
197 redirects across 9 bots). Every redirect row reconciles; latency
section now reads honestly; empty-path probe row renders cleanly.

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
