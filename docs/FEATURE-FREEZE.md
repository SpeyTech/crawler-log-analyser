# Feature Freeze — 2026-05-16

## Status

v1.10.2 is the resting state of `crawler-log-analyser`. No new feature
work is planned through 31 August 2026.

## Scope of the freeze

**Frozen:**

* New report sections
* New CLI flags
* New classification categories
* New JSON top-level keys
* New input formats (Apache, Caddy)
* New output formats
* Any architectural change (single-file → package, etc.)

**Not frozen — always welcome:**

* Bug fixes against v1.10.2 behaviour
* Documentation improvements (README clarifications, typo fixes,
  verification recipe additions)
* Test additions that strengthen existing coverage without adding
  new tested surfaces
* Performance fixes that preserve byte-identical output
* Security fixes (none currently known)

## Reasoning

The analyser covers every signal a self-hosted operator currently needs.
Each deferred feature in `docs/DEFERRED-FEATURES.md` closes a real gap,
but none is pressing. Stopping the feature cadence buys three things:

1. **Production exposure surfaces latent issues.** The v1.10.0 → .1 → .2
   sequence took 24 hours because v1.10.0 hit production within hours
   of being written; the leaks were visible immediately. The next bug
   wants weeks of exposure to find. A frozen feature surface is what
   makes that exposure diagnostic rather than confounding.

2. **Maintenance time becomes available.** Roughly one feature per month
   since v1.4 has been the cadence. Pausing frees that time for
   adjacent work — the v1.10 article, continued production observation,
   and the rest of the maintainer's portfolio that isn't this tool.

3. **The article claims hold.** The two SpeyTech Insights articles
   describing the tool (May 12 operational-observability, forthcoming
   v1.10 post-mortem) make specific claims about the analyser's
   capabilities. A frozen v1.10.2 means those claims stay accurate
   without follow-up edits.

## Reconsideration

The freeze lifts on 1 September 2026, or earlier if a deferred feature's
"when to reconsider" condition triggers (see `docs/DEFERRED-FEATURES.md`).

A bug found in v1.10.2 may be patched as v1.10.3, v1.10.4, etc. without
lifting the freeze — bug fixes are not features.

## Contributing during the freeze

Pull requests that fit the "not frozen" categories above are welcome and
will be reviewed as time permits. PRs adding features (anything in the
"frozen" list) will be acknowledged but deferred to the post-freeze
development window with a link back to this document.

If a feature feels genuinely urgent — an actual production failure mode
that v1.10.2 cannot detect — open an issue describing the observed
behaviour rather than a PR adding a fix. Real failure modes are the only
trigger that justifies lifting the freeze early, and the issue thread
is the right place to establish whether the failure mode is real.

— William
