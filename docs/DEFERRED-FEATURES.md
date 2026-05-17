# Deferred Features

This document tracks features that have been considered for the analyser
and explicitly deferred. Each entry records the gap it would close, the
reasoning for deferral, and the conditions under which it should be
reconsidered.

The point of this file is **not** to be a TODO list. Items here are
explicitly *not* on the roadmap. The file exists so that:

1. Contributors know what has already been thought about and rejected
   (saves them writing PRs that will be declined).
2. Future maintainers can revisit the reasoning rather than re-deriving
   it from scratch.
3. The "no" is documented and reversible. Each item lists the conditions
   under which "no" becomes "yes" — which is much harder to write
   honestly than just listing features.

If an item here moves to active development, it should be removed from
this file and added to the changelog of whichever version implements it.

---

## Cross-day rotation tracking

**Gap:** A patient attacker who rotates one bot identity per hour across
24 hours never trips the 5-minute window in v1.10's
`SuspectedBotIdentityRotation`. Today's analyser cannot see this pattern.

**Why deferred:** Requires stateful storage across runs (an index file
mapping IP → rolling identity history). This crosses the read-only-tool
boundary the analyser has held since v1.0. The single-file,
no-state-on-disk promise is part of the trust story — every run is
self-contained, no surprise files appear in the operator's home
directory, no migration concerns, no "is the index corrupt" failure mode.

**When to reconsider:** When a multi-day attacker pattern is actually
observed in production logs and the analyser misses it. Until then, the
shape of the cross-day attacker is theoretical. Reconsider also if a
related project (e.g. fail2ban integration) provides the cross-run state
naturally, in which case the analyser can consume it without owning it.

**Workaround today:** Concatenate multi-day logs and run the analyser
against the combined input with a widened `--rotation-window-minutes`.
Imperfect (the analyser doesn't normalise across day boundaries) but
catches the obvious case.

---

## Apache log format support

**Gap:** The analyser is hardcoded to nginx's combined and `seo_crawl`
formats. Apache `combined` and `vhost_combined` formats are similar but
not identical. Apache users cannot use the tool without writing their
own parser.

**Why deferred:** A parser addition is roughly 200 lines, but the test
surface doubles — every existing test needs an Apache equivalent fixture
to prove the parser produces semantically identical entries to the
nginx parser. The two-format invariant ("same log content, same report
output regardless of source format") would need its own dedicated test
class. Operationally, Apache users tend to be on stacks where existing
log-analysis tooling (AWStats, GoAccess) is already in place; adoption
friction is high.

**When to reconsider:** If three or more independent Apache users open
GitHub issues asking for it. One person asking is curiosity; three is
demand.

**Workaround today:** Convert Apache logs to nginx combined format with
a one-line `awk` or `sed` script. Apache's `combined` format is almost
identical to nginx's; only field ordering differs in edge cases.

---

## Multi-site comparison mode

**Gap:** Running the analyser against both `speytech.com.seo.log` and
`axilog.io.seo.log` produces two reports. Comparing them manually is
how the operator notices that the same `5.255.104.83` rotation appears
on both sites (signal: network-layer attacker, not site-layer issue) or
that a deploy-window anomaly fires on one site but not the other
(signal: site-specific deploy bug). The comparison is useful; today
it's mental.

**Why deferred:** Real complexity in the aggregation layer. The
`Aggregate` dataclass would need to become per-site, and every render
function would need a "single site or comparison?" branch. The
section-by-section layout that works for one site doesn't work for two
side-by-side. The cost is high; the operator who's running two sites is
also the operator who can read two reports.

**When to reconsider:** When the fleet reaches four or more sites. Two
reports is comfortable; four is not. At four sites the manual comparison
breaks down and the case for built-in comparison gets stronger. Also
worth reconsidering if the analyser's JSON output starts being consumed
by an external dashboard, in which case the comparison lives in the
dashboard layer and the analyser stays simple.

**Workaround today:** Run twice, eyeball the differences. For programmatic
comparison, run with `--format json` against each site and `jq`
side-by-side.

---

## HTTP/2-aware latency analysis

**Gap:** When Googlebot uses HTTP/2, multiple requests can share a TCP
connection. The `$request_time` and `$upstream_response_time` values
nginx records are still per-request, but they don't reflect connection
coalescing — so two parallel HTTP/2 requests on the same connection can
look like two independent slow requests when they're really one stalled
upstream serving two streams. The analyser's per-bot latency medians
are directionally accurate but not strictly accurate under heavy HTTP/2
multiplexing.

**Why deferred:** Precision issue, not a correctness issue. Today's
numbers are good enough to drive operational decisions (the latency
deduction in the Googlebot health score fires when it should). HTTP/2
awareness requires `$connection` and `$connection_requests` fields in
the log format, and a stream-grouping step in the analyser. Substantial
work for a refinement most operators will never notice.

**When to reconsider:** If an operator reports a real misdiagnosis driven
by this — e.g. "the analyser said Googlebot's p95 latency was 1.2s but
the underlying connection was actually serving fine, the multiplexing
just made it look slow". Until that report arrives, this is hypothetical.

**Workaround today:** Read the slowest-10 list with awareness that some
of those latencies may be coalesced multiplexed streams. If the slowest
entries all share a tight timestamp cluster from the same IP, they're
likely one connection serving in parallel rather than ten independent
slow requests.

---

## Auto-feeding rotation IPs to fail2ban

**Gap:** When the analyser detects rotational identity spoofing from
a specific IP, that IP is almost certainly attacker traffic. Today the
operator reads the report and manually adds the IP to fail2ban (or
nginx's `deny` list). Automating this would close the loop between
detection and response.

**Why deferred:** Crosses the read-only-tool boundary explicitly. The
analyser has never written to system state outside its own output files,
and auto-banning would change that fundamentally. False-positive risk
matters here too — a false positive on a real Googlebot IP would damage
SEO; the cost of being wrong is asymmetric and high. The nginx-probe
jail already catches these IPs at the network layer via path matching;
the analyser surfacing the pattern is sufficient signal for an operator
to act, and the operator's judgement is the safety check.

**When to reconsider:** Probably never as a feature of the analyser
itself. The right shape is a separate tool that consumes the analyser's
JSON output and feeds fail2ban — keeping the analyser's read-only
property intact while still automating the loop.

**Workaround today:** `crawler_log_analyser.py --format json ... | jq
'.rotational_bot_identity.top_ips[].ip' | xargs -n1 fail2ban-client set
crawler-jail banip`. Three lines of shell, no tool changes needed.

---

## Per-site rotation comparison

**Gap:** Multiple IPs from the same /24 cycling identities is a stronger
signal than single-IP rotation — it indicates a botnet or VPN exit pool
rather than one attacker host. Today the analyser detects single-IP
rotation cleanly but doesn't cluster across IPs.

**Why deferred:** Multi-day data hasn't accumulated enough to justify
the design. The pattern is plausible but unobserved. Designing for an
unobserved pattern risks getting the design wrong.

**When to reconsider:** After multi-day rotation tracking lands (see
first entry). The two features compose naturally — once the analyser
has cross-day visibility, CIDR-level aggregation becomes a small
extension. Doing it first would mean designing in the dark.

**Workaround today:** Eyeball the rotation section's top-IPs list for
shared /24 prefixes. With single-digit rotation incidents per day this
is tractable manually.

---

## CIDR/ASN aggregation generally

**Gap:** Throughout the report, IPs are treated as opaque tokens. ASN
context ("this IP belongs to AS197207, a Russian cloud provider") would
add diagnostic value to the spoof, rotation, and probe sections.

**Why deferred:** ASN lookup requires either a bundled MaxMind GeoIP
database (large file, license terms, update cadence) or a network call
per IP (slow, brittle, adds a dependency). Both options compromise the
single-file, no-dependencies, no-network promise.

**When to reconsider:** If a permissive-licensed offline ASN database
becomes available in a form small enough to bundle, OR if the analyser
adopts an optional-dependency pattern where ASN annotation is a "nice
to have if `pyasn` is installed, silently skipped otherwise" feature.

**Workaround today:** `whois <ip>` or `dig +short -x <ip>` on the IPs
in the rotation/spoof/probe sections. Manual but fast for the typical
2-5 IPs an operator wants to look up per day.

---

## Notes on this list

The list above is not exhaustive. New features may be added here when
they come up; existing features may be removed when they're either
implemented (with a note pointing to the implementing version) or
explicitly declared out of scope (with reasoning).

The shape of each entry — Gap, Why deferred, When to reconsider,
Workaround today — is deliberate. "Workaround today" matters because
some of these gaps have shell-pipeline workarounds that are genuinely
fine for the volumes a self-hosted operator encounters. A feature
that has a three-line workaround is rarely worth a hundred lines of
implementation.

The "When to reconsider" condition is the hardest field to fill in
honestly. It's easy to write "when there's demand" — but demand without
a measurable threshold is permission to ship anything. Each entry above
tries to name a specific trigger (a count, an observed pattern, a
related feature landing) so that "yes, reconsider this" can be decided
on facts rather than feelings.
