#!/usr/bin/env bash
#
# cron-recipe.sh
#
# Example cron wrapper for crawler-log-analyser. Runs daily, captures
# the analyser's exit code, emails the report when there is something
# to investigate, stays silent otherwise.
#
# Place this somewhere readable by the cron user (typically root):
#
#   /usr/local/bin/crawler-log-cron.sh
#
# And install a daily cron entry, e.g. /etc/cron.d/crawler-log:
#
#   30 06 * * * root /usr/local/bin/crawler-log-cron.sh
#
# The analyser's --strict flag produces structured exit codes:
#   0 = clean
#   1 = HIGH/WARNING findings or score < Excellent
#   2 = CRITICAL findings (Googlebot robots.txt failure, deploy anomaly,
#       5xx on real search crawlers, etc.)
#
# This wrapper exits silently on 0 and emails the report on 1 or 2.

set -euo pipefail

# ----- configuration ---------------------------------------------------

ANALYSER="/home/william/crawler-log-analyser/crawler_log_analyser.py"
LOG_FILE="/var/log/nginx/speytech.com.access.log"
REPORT_DIR="/home/william/crawler-reports"
MAILTO="william@example.com"
HOSTNAME="$(hostname -s)"

# ----- runtime ---------------------------------------------------------

mkdir -p "$REPORT_DIR"
TODAY="$(date +%F)"
REPORT="$REPORT_DIR/speytech-${TODAY}.md"

# Run the analyser; do not abort on non-zero exit.
set +e
python3 "$ANALYSER" \
    "$LOG_FILE" \
    --format markdown \
    --output "$REPORT" \
    --strict
EXIT_CODE=$?
set -e

case "$EXIT_CODE" in
    0)
        # Clean run — stay silent.
        exit 0
        ;;
    1)
        SUBJECT="[$HOSTNAME] crawler-log: WARNING findings"
        ;;
    2)
        SUBJECT="[$HOSTNAME] crawler-log: CRITICAL findings"
        ;;
    *)
        SUBJECT="[$HOSTNAME] crawler-log: analyser exited $EXIT_CODE"
        ;;
esac

# Send the report by mail. Requires mailx (or equivalent) to be
# installed and configured. On Debian/Ubuntu:
#   apt install bsd-mailx
if command -v mailx >/dev/null 2>&1; then
    mailx -s "$SUBJECT" "$MAILTO" < "$REPORT"
elif command -v mail >/dev/null 2>&1; then
    mail -s "$SUBJECT" "$MAILTO" < "$REPORT"
else
    echo "warning: neither mailx nor mail available; report saved to $REPORT" >&2
fi

exit "$EXIT_CODE"
