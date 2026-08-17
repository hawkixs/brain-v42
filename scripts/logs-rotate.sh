#!/usr/bin/env bash
# Nightly log retention for brain-v42.
# Deletes files under logs/ that have not been modified in the last 90 days.
#
# Installed via user crontab:
#   0 5 * * * /home/hawixs/hawkixs_infra/git_repo/brain_v42/scripts/logs-rotate.sh >> /tmp/brain-v42-logs-rotate.log 2>&1
#
# Rationale: dream logs and otel splits accumulate one file per phase per
# date (6 phases × ~365 days ≈ 2200 files/year, ~30 MB/year). 90d retention
# covers two full weekly dream reviews + a comfortable forensic window.

set -u

LOGS_DIR="/home/hawixs/hawkixs_infra/git_repo/brain_v42/logs"
RETENTION_DAYS=90

echo "=== logs-rotate run at $(date --iso-8601=seconds) ==="
echo "target: $LOGS_DIR (mtime > $RETENTION_DAYS days)"

if [ ! -d "$LOGS_DIR" ]; then
  echo "ERROR: logs dir not found"
  exit 2
fi

before=$(find "$LOGS_DIR" -type f | wc -l)
deleted=$(find "$LOGS_DIR" -type f -mtime "+$RETENTION_DAYS" -delete -print | wc -l)
after=$(find "$LOGS_DIR" -type f | wc -l)

echo "files before: $before"
echo "deleted:      $deleted"
echo "files after:  $after"
echo "=== done ==="
