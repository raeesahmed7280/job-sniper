#!/usr/bin/env bash
# run.sh — cron-friendly wrapper for Job Sniper.
#
#   crontab -e
#   0 8 * * *  /root/job-sniper/run.sh >> /root/job-sniper/logs/cron.log 2>&1
#
# Optional guard: skip the run if the last successful run was under 6 hours ago,
# so an accidentally doubled cron entry does not spam your Discord.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

PYTHON="${PYTHON:-$PROJECT_DIR/.venv/bin/python}"
STAMP_FILE="$PROJECT_DIR/state/.last_run"
MIN_INTERVAL_SECONDS="${MIN_INTERVAL_SECONDS:-21600}"   # 6 hours

if [[ "${1:-}" != "--force" && -f "$STAMP_FILE" ]]; then
  last=$(cat "$STAMP_FILE" 2>/dev/null || echo 0)
  now=$(date +%s)
  if (( now - last < MIN_INTERVAL_SECONDS )); then
    echo "[$(date '+%F %T')] skipped: last run was $(( (now - last) / 60 )) min ago"
    exit 0
  fi
fi

echo "[$(date '+%F %T')] Job Sniper starting"
if "$PYTHON" main.py "$@"; then
  mkdir -p "$PROJECT_DIR/state"
  date +%s > "$STAMP_FILE"
  echo "[$(date '+%F %T')] Job Sniper finished OK"
else
  echo "[$(date '+%F %T')] Job Sniper FAILED (exit $?)" >&2
  exit 1
fi
