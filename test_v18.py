#!/usr/bin/env python3
"""v1.8 verification — config file loading + merge semantics + --show-config."""
import sys, os, tempfile, subprocess, argparse

# Resolve the analyser location relative to this test file so the suite
# is portable across repo checkouts (works in any clone, not just the
# author's sandbox).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from pathlib import Path
from crawler_log_analyser import (
    discover_config_path,
    load_config,
    _parse_toml_subset,
    render_config_summary,
    ResolvedConfig,
    CONFIG_KNOWN_KEYS,
)

results = []
def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((status, name, detail))
    print(f"{status}  {name}" + (f"  ({detail})" if detail else ""))


# ─────────────────────────────────────────────────────────────────────
# Discover path order
# ─────────────────────────────────────────────────────────────────────
print("\n=== Discover path order ===\n")

# 1. Explicit --config wins
with tempfile.TemporaryDirectory() as td:
    explicit = Path(td) / "explicit.toml"
    explicit.write_text('ignore_source_ips = ["1.2.3.4"]\n')
    discovered = discover_config_path(str(explicit))
    check("Explicit --config returns the given path",
          discovered == explicit, f"got {discovered}")

# 2. Explicit path that doesn't exist still returned (caller surfaces the error)
discovered = discover_config_path("/nonexistent/path/config.toml")
check("Explicit --config with non-existent path still returns it (caller handles)",
      discovered == Path("/nonexistent/path/config.toml"))

# 3. No XDG, no home-dot, no anything → None
import os as _os
saved_xdg = _os.environ.pop("XDG_CONFIG_HOME", None)
saved_home = _os.environ.get("HOME")
with tempfile.TemporaryDirectory() as empty_home:
    _os.environ["HOME"] = empty_home
    discovered = discover_config_path()
    check("No config files anywhere → None",
          discovered is None, f"got {discovered}")
if saved_xdg is not None:
    _os.environ["XDG_CONFIG_HOME"] = saved_xdg
if saved_home is not None:
    _os.environ["HOME"] = saved_home

# 4. XDG wins over home-dot
with tempfile.TemporaryDirectory() as xdg_dir, \
     tempfile.TemporaryDirectory() as home_dir:
    xdg_config = Path(xdg_dir) / "crawler-log-analyser" / "config.toml"
    xdg_config.parent.mkdir(parents=True)
    xdg_config.write_text("# xdg\n")

    home_config = Path(home_dir) / ".crawler-log-analyser.toml"
    home_config.write_text("# home dot\n")

    _os.environ["XDG_CONFIG_HOME"] = xdg_dir
    _os.environ["HOME"] = home_dir
    discovered = discover_config_path()
    check("XDG location wins over home-dot when both exist",
          discovered == xdg_config, f"got {discovered}")

# 5. ~/.config/ fallback works when XDG unset
_os.environ.pop("XDG_CONFIG_HOME", None)
with tempfile.TemporaryDirectory() as home_dir:
    dot_config = Path(home_dir) / ".config" / "crawler-log-analyser" / "config.toml"
    dot_config.parent.mkdir(parents=True)
    dot_config.write_text("# default xdg\n")
    _os.environ["HOME"] = home_dir
    discovered = discover_config_path()
    check("~/.config/ fallback when XDG_CONFIG_HOME unset",
          discovered == dot_config, f"got {discovered}")

# 6. Home-dot fallback when nothing else exists
with tempfile.TemporaryDirectory() as home_dir:
    home_config = Path(home_dir) / ".crawler-log-analyser.toml"
    home_config.write_text("# home dot fallback\n")
    _os.environ["HOME"] = home_dir
    discovered = discover_config_path()
    check("Home-dot fallback when neither XDG nor ~/.config/ has config",
          discovered == home_config, f"got {discovered}")

# Restore env
if saved_xdg is not None: _os.environ["XDG_CONFIG_HOME"] = saved_xdg
if saved_home is not None: _os.environ["HOME"] = saved_home


# ─────────────────────────────────────────────────────────────────────
# load_config behaviour
# ─────────────────────────────────────────────────────────────────────
print("\n=== load_config behaviour ===\n")

# 1. None path → empty config, no warnings
cfg = load_config(None)
check("None path → empty config",
      cfg.source_path is None and cfg.ignore_source_ips_from_config == []
      and cfg.parse_warnings == [])

# 2. Valid file with known key
with tempfile.NamedTemporaryFile(mode='w', suffix='.toml', delete=False) as f:
    f.write('ignore_source_ips = ["35.230.156.201", "10.0.0.5"]\n')
    valid_path = Path(f.name)
cfg = load_config(valid_path)
check("Valid file → IPs loaded",
      cfg.ignore_source_ips_from_config == ["35.230.156.201", "10.0.0.5"]
      and not cfg.parse_warnings,
      f"got IPs={cfg.ignore_source_ips_from_config}, warnings={cfg.parse_warnings}")
valid_path.unlink()

# 3. File with unknown key → loaded but warning
with tempfile.NamedTemporaryFile(mode='w', suffix='.toml', delete=False) as f:
    f.write('ignore_source_ips = ["1.1.1.1"]\nfuture_setting = "something"\n')
    p = Path(f.name)
cfg = load_config(p)
check("Unknown key → recorded but doesn't fail load",
      cfg.ignore_source_ips_from_config == ["1.1.1.1"]
      and "future_setting" in cfg.unknown_keys,
      f"unknown_keys={cfg.unknown_keys}")
p.unlink()

# 4. Malformed TOML → warning, empty config
with tempfile.NamedTemporaryFile(mode='w', suffix='.toml', delete=False) as f:
    f.write('this is not = valid toml = anywhere\n[ unclosed bracket\n')
    p = Path(f.name)
cfg = load_config(p)
check("Malformed TOML → parse warning, no IPs",
      cfg.ignore_source_ips_from_config == []
      and len(cfg.parse_warnings) >= 1,
      f"warnings={cfg.parse_warnings}")
p.unlink()

# 5. Non-existent file passed explicitly → warning, empty config
cfg = load_config(Path("/nonexistent/config.toml"))
check("Non-existent file → warning, no IPs",
      cfg.ignore_source_ips_from_config == []
      and any("not found" in w for w in cfg.parse_warnings),
      f"warnings={cfg.parse_warnings}")

# 6. Wrong type for ignore_source_ips → warning, no IPs
with tempfile.NamedTemporaryFile(mode='w', suffix='.toml', delete=False) as f:
    f.write('ignore_source_ips = "not_a_list"\n')
    p = Path(f.name)
cfg = load_config(p)
check("Wrong type for ignore_source_ips → warning, no IPs",
      cfg.ignore_source_ips_from_config == []
      and any("must be a list" in w for w in cfg.parse_warnings),
      f"warnings={cfg.parse_warnings}")
p.unlink()


# ─────────────────────────────────────────────────────────────────────
# Fallback TOML parser
# ─────────────────────────────────────────────────────────────────────
print("\n=== Fallback TOML parser ===\n")

# 1. Simple string value
parsed, warnings = _parse_toml_subset('greeting = "hello"\n')
check("Fallback: string value parses",
      parsed == {"greeting": "hello"} and warnings == [],
      f"parsed={parsed}, warnings={warnings}")

# 2. Array of strings
parsed, warnings = _parse_toml_subset('xs = ["a", "b", "c"]\n')
check("Fallback: string array parses",
      parsed == {"xs": ["a", "b", "c"]} and warnings == [])

# 3. Empty array
parsed, warnings = _parse_toml_subset('xs = []\n')
check("Fallback: empty array parses",
      parsed == {"xs": []} and warnings == [])

# 4. Comments and blank lines skipped
parsed, warnings = _parse_toml_subset(
    '# This is a comment\n'
    '\n'
    'key = "value"\n'
    '# Another comment\n'
)
check("Fallback: comments and blank lines skipped",
      parsed == {"key": "value"} and warnings == [])

# 5. Section table → warning, key skipped
parsed, warnings = _parse_toml_subset('[section]\nkey = "value"\n')
check("Fallback: [section] table → warning",
      "section" not in parsed and any("section" in w for w in warnings),
      f"parsed={parsed}, warnings={warnings}")


# ─────────────────────────────────────────────────────────────────────
# Merge semantics
# ─────────────────────────────────────────────────────────────────────
print("\n=== Merge semantics ===\n")

# Config IPs + CLI IPs = both
cfg = ResolvedConfig(
    ignore_source_ips_from_config=["35.230.156.201"],
    ignore_source_ips_from_cli=["1.2.3.4"],
)
effective = cfg.effective_ignore_source_ips
check("Config + CLI IPs are merged additively",
      effective == frozenset({"35.230.156.201", "1.2.3.4"}),
      f"got {sorted(effective)}")

# CLI-only
cfg = ResolvedConfig(ignore_source_ips_from_cli=["1.2.3.4"])
check("CLI-only IPs work without config",
      cfg.effective_ignore_source_ips == frozenset({"1.2.3.4"}))

# Config-only
cfg = ResolvedConfig(ignore_source_ips_from_config=["35.230.156.201"])
check("Config-only IPs work without CLI",
      cfg.effective_ignore_source_ips == frozenset({"35.230.156.201"}))

# Duplicate IPs collapse via frozenset
cfg = ResolvedConfig(
    ignore_source_ips_from_config=["1.2.3.4"],
    ignore_source_ips_from_cli=["1.2.3.4"],
)
check("Duplicate IPs collapse in effective set",
      cfg.effective_ignore_source_ips == frozenset({"1.2.3.4"}))


# ─────────────────────────────────────────────────────────────────────
# End-to-end via subprocess
# ─────────────────────────────────────────────────────────────────────
print("\n=== End-to-end CLI ===\n")

ANALYSER = os.path.join(_HERE, "crawler_log_analyser.py")

# 1. --version still works
r = subprocess.run(["python3", ANALYSER, "--version"],
                   capture_output=True, text=True)
check("--version reports 1.8.0",
      "1.8.0" in r.stdout, f"got {r.stdout!r}")

# 2. --show-config with no config → reports 'none'
with tempfile.TemporaryDirectory() as empty_home:
    env = {**os.environ, "HOME": empty_home}
    env.pop("XDG_CONFIG_HOME", None)
    r = subprocess.run(["python3", ANALYSER, "--show-config"],
                       capture_output=True, text=True, env=env)
    check("--show-config with no config → exit 0, reports 'none'",
          r.returncode == 0 and "(none)" in r.stdout,
          f"rc={r.returncode}, stdout={r.stdout[:200]!r}")

# 3. --show-config with explicit config → reports IPs
with tempfile.NamedTemporaryFile(mode='w', suffix='.toml', delete=False) as f:
    f.write('ignore_source_ips = ["35.230.156.201", "10.0.0.5"]\n')
    cp = f.name
r = subprocess.run(["python3", ANALYSER, "--config", cp, "--show-config"],
                   capture_output=True, text=True)
check("--show-config with explicit config shows IPs",
      r.returncode == 0
      and "35.230.156.201" in r.stdout
      and "10.0.0.5" in r.stdout,
      f"rc={r.returncode}, stdout snippet={r.stdout[:300]!r}")

# 4. --show-config with CLI IPs merges them in
r = subprocess.run([
    "python3", ANALYSER, "--config", cp,
    "--ignore-source-ip", "9.9.9.9",
    "--ignore-source-ip", "8.8.8.8",
    "--show-config"
], capture_output=True, text=True)
check("--show-config shows merged effective set",
      r.returncode == 0
      and "35.230.156.201" in r.stdout
      and "9.9.9.9" in r.stdout
      and "8.8.8.8" in r.stdout
      and "Effective ignore-source-ip set (4 IPs)" in r.stdout,
      f"rc={r.returncode}")

# 5. --no-config-ignores clears config IPs but keeps CLI
r = subprocess.run([
    "python3", ANALYSER, "--config", cp,
    "--ignore-source-ip", "9.9.9.9",
    "--no-config-ignores",
    "--show-config"
], capture_output=True, text=True)
check("--no-config-ignores clears config IPs but keeps CLI",
      r.returncode == 0
      and "35.230.156.201" not in r.stdout.split("Effective")[1]  # not in effective set
      and "9.9.9.9" in r.stdout
      and "Effective ignore-source-ip set (1 IPs)" in r.stdout,
      f"rc={r.returncode}")

os.unlink(cp)

# 6. Missing config file → run continues with stderr warning
#
# Write a minimal one-line log to a tempfile so the test is self-contained
# (no dependency on a fixture file that may not be in every checkout).
MINIMAL_LOG_LINE = (
    '66.249.66.1 - - [14/May/2026:09:10:00 +0000] '
    '"GET /robots.txt HTTP/1.1" 200 543 "-" '
    '"Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"\n'
)
with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as f:
    f.write(MINIMAL_LOG_LINE)
    log = f.name
r = subprocess.run([
    "python3", ANALYSER, log,
    "--config", "/nonexistent/path/config.toml"
], capture_output=True, text=True)
check("Missing config file → run continues with stderr warning",
      r.returncode == 0
      and "warning" in r.stderr.lower()
      and "Crawler Log Summary" in r.stdout,
      f"rc={r.returncode}, stderr={r.stderr[:300]!r}")
os.unlink(log)


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
