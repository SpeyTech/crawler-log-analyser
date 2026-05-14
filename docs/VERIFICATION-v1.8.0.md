# Verification — crawler-log-analyser v1.8.0

This document captures the verification approach used to validate the
v1.8.0 release before tagging. Reproducible recipes only; no per-run
state.

## Summary

v1.8.0 ships one feature: operator IP defaults via config file.
Backward compatibility is the strongest possible kind — byte-identical
text output to v1.7.0 for any input when no config file is present.

## Reproducibility recipe

### 1. Regression: v1.7 test suite must pass

```bash
cd /home/william/crawler-log-analyser
python3 test_v17.py
# Expected: 36 passed, 0 failed
```

This proves no v1.7-era behaviour has regressed.

### 2. New tests: v1.8 test suite must pass

```bash
cd /home/william/crawler-log-analyser
python3 test_v18.py
# Expected: 27 passed, 0 failed
```

27 tests cover six path-discovery scenarios, six load_config failure
modes, five fallback-parser cases, four merge-semantics cases, and six
end-to-end CLI cases.

### 3. Byte-identical text output for v1.7 → v1.8 on same input

This check isolates v1.8 to its "no config present" code path so the
byte-identical guarantee can be verified directly. If a config file is
already in place at one of the discovery locations, temporarily move
it aside for the comparison.

```bash
cd /home/william/crawler-log-analyser

# If a config file is in place at the XDG-default location, move it
# aside for the duration of this check so v1.8 runs with no config —
# which is the only path the byte-identical guarantee covers.
CONFIG="$HOME/.config/crawler-log-analyser/config.toml"
if [ -f "$CONFIG" ]; then
    mv "$CONFIG" "$CONFIG.bak"
fi

python3 crawler_log_analyser.py /var/log/nginx/speytech.com.seo.log > /tmp/v18-prod.txt
git show v1.7.0:crawler_log_analyser.py > /tmp/v17-analyser.py
chmod +x /tmp/v17-analyser.py
python3 /tmp/v17-analyser.py /var/log/nginx/speytech.com.seo.log > /tmp/v17-prod.txt
md5sum /tmp/v17-prod.txt /tmp/v18-prod.txt
# Expected: both md5sums identical

# Restore the config file
if [ -f "$CONFIG.bak" ]; then
    mv "$CONFIG.bak" "$CONFIG"
fi
```

This proves v1.8 is byte-identical to v1.7 for the same input when no
config file is present in the discovery search order.

If you do *not* move the config aside, the md5sums will differ —
that's expected behaviour, not a regression. With a config file in
place, v1.8 filters the operator IPs from the probe-noise summary
while v1.7 does not, so the "Suppressed Security Probe Noise" section
will differ between the two. The byte-identical guarantee covers only
the no-config code path.

### 4. Config file behaviour spot check

```bash
# Create a test config
mkdir -p ~/.config/crawler-log-analyser
cat > ~/.config/crawler-log-analyser/config.toml <<'EOF'
ignore_source_ips = ["35.230.156.201"]
EOF

# Verify it's discovered and merged correctly
python3 crawler_log_analyser.py --show-config
# Expected output mentions the file path and the IP

# Verify --no-config-ignores clears it
python3 crawler_log_analyser.py --no-config-ignores --show-config
# Expected: 0 effective IPs

# Cleanup
rm ~/.config/crawler-log-analyser/config.toml
rmdir ~/.config/crawler-log-analyser
```

### 5. Publish-integrity verification (after tagging)

```bash
git show v1.8.0:crawler_log_analyser.py | md5sum
md5sum crawler_log_analyser.py
# Expected: both md5sums identical
```

Non-negotiable check after the v1.5.0 incident.

## Production verification (the day of release)

Run the analyser against today's production log:

```bash
./crawler_log_analyser.py /var/log/nginx/speytech.com.seo.log
```

Expected behaviour:

- If `~/.config/crawler-log-analyser/config.toml` (or one of the other
  discovery locations) exists, the operator IPs listed there are
  silently applied. The "Suppressed Security Probe Noise" section
  reflects the filtering.
- If no config file exists, output is byte-identical to v1.7 (verified
  in recipe step 3 above).
- No new mandatory CLI flags. Every v1.7 invocation continues to work.

## Failure-mode verification

The following error conditions must be non-fatal:

| Condition | Expected behaviour |
|-----------|--------------------|
| Config file doesn't exist (explicit `--config`) | stderr warning, run continues |
| Config file unreadable (permissions) | stderr warning, run continues |
| Malformed TOML | stderr warning, run continues |
| Wrong type for `ignore_source_ips` | stderr warning, key ignored, run continues |
| Unknown config key | stderr warning, key ignored, run continues |
| `--show-config` with no log paths | exit 0, prints resolved config |

All six are covered by `test_v18.py`.
