#!/usr/bin/env bash
# Daily 90-day log retention. Run manually with --dry-run to inspect candidates;
# the systemd user timer is installed dormant and must be enabled by an operator.
# Optional targets: BRAIN_LOGS_ROTATE_TARGETS (colon-separated) or positional paths.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${BRAIN_LOGS_REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
RETENTION_DAYS=90
DRY_RUN=false
TARGETS=()

while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --help|-h)
      printf 'Usage: %s [--dry-run] [LOG_DIRECTORY ...]\n' "$0"
      exit 0
      ;;
    --*) printf 'ERROR: unknown option: %s\n' "$1" >&2; exit 2 ;;
    *) TARGETS+=("$1"); shift ;;
  esac
done

if ((${#TARGETS[@]} == 0)) && [[ -n "${BRAIN_LOGS_ROTATE_TARGETS:-}" ]]; then
  IFS=: read -r -a TARGETS <<< "$BRAIN_LOGS_ROTATE_TARGETS"
elif ((${#TARGETS[@]} == 0)); then
  [[ -d "$REPO_ROOT/logs" ]] && TARGETS+=("$REPO_ROOT/logs")
  RELEASE_ROOT="${HOME:?HOME must be set}/.local/share/brain-v42/releases"
  if [[ -d "$RELEASE_ROOT" ]]; then
    shopt -s nullglob
    for logs in "$RELEASE_ROOT"/*/brain-v42/logs; do
      [[ -d "$logs" ]] && TARGETS+=("$logs")
    done
  fi
fi

printf '=== logs-rotate run at %s ===\n' "$(date --iso-8601=seconds)"
had_errors=false
for target in "${TARGETS[@]}"; do
  if [[ -L "$target" ]]; then
    printf 'ERROR: target is a symlink: %s\n' "$target" >&2
    had_errors=true
    continue
  fi
  if [[ ! -d "$target" ]]; then
    printf 'ERROR: target is not a directory: %s\n' "$target" >&2
    had_errors=true
    continue
  fi
  owner="$(stat -c '%u' -- "$target")"
  if [[ "$owner" != "$(id -u)" ]]; then
    printf 'ERROR: target is not owned by the current user: %s\n' "$target" >&2
    had_errors=true
    continue
  fi
  printf 'target: %s (mtime > %s days)\n' "$target" "$RETENTION_DAYS"
  before="$(find "$target" -type f -printf '.' | wc -c)"
  deleted=0
  while IFS= read -r -d '' file; do
    if $DRY_RUN; then
      printf 'would delete: %s\n' "$file"
    else
      if rm -- "$file"; then
        printf 'deleted: %s\n' "$file"
      else
        printf 'ERROR: failed to delete: %s\n' "$file" >&2
        had_errors=true
      fi
    fi
    ((deleted += 1))
  done < <(find "$target" -type f -mtime "+$RETENTION_DAYS" -print0)
  after="$(find "$target" -type f -printf '.' | wc -c)"
  printf 'files before: %s\nfiles %s: %s\nfiles after: %s\n' \
    "$before" "$([[ $DRY_RUN == true ]] && printf 'eligible' || printf 'deleted')" \
    "$deleted" "$after"
done
printf '=== done ===\n'
$had_errors && exit 1
exit 0
