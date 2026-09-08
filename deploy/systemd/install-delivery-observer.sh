#!/usr/bin/env bash
# Render the isolated delivery observer unit. This installer never activates systemd units.
set -euo pipefail

UNIT_NAME="brain-v42-delivery-observer.service"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"
TEMPLATE="$SCRIPT_DIR/$UNIT_NAME.tmpl"
MODE=""
RENDER_TARGET=""
STAGING_DIR=""
VERIFIER_PID=""
VALIDATED_PYTHON_IDENTITY=""
VALIDATED_PRIVATE_ENV_IDENTITY=""

usage() {
  cat <<'EOF'
Usage:
  install-delivery-observer.sh --check-only
  install-delivery-observer.sh --render-dir ABSOLUTE_NEW_DIRECTORY

Required environment:
  BRAIN_DELIVERY_OBSERVER_PYTHON    absolute, regular, executable Python path
  BRAIN_DELIVERY_OBSERVER_ENV_FILE  absolute private 0600 regular file

Both modes render and verify only brain-v42-delivery-observer.service. They never
call systemctl and never activate, restart, stop, or modify a live unit.
EOF
}

fail() {
  printf '%s\n' "ERROR: $1" >&2
  exit 2
}

cleanup() {
  if [[ -n "$STAGING_DIR" && -d "$STAGING_DIR" ]]; then
    rm -rf -- "$STAGING_DIR"
  fi
}

interrupt() {
  if [[ -n "$VERIFIER_PID" ]]; then
    kill "$VERIFIER_PID" 2>/dev/null || true
    wait "$VERIFIER_PID" 2>/dev/null || true
    VERIFIER_PID=""
  fi
  cleanup
  exit 143
}
trap cleanup EXIT
trap interrupt INT TERM HUP

path_has_safe_unit_syntax() {
  local value="$1"
  [[ "$value" =~ ^/[A-Za-z0-9._/-]+$ ]] || return 1
  [[ "$value" != *"//"* && "$value" != *"/./"* && "$value" != *".."* ]]
}

safe_directory_ancestry() {
  local directory="$1" current="/" part
  local -a parts=()
  [[ "$directory" == /* && -d "$directory" && ! -L "$directory" ]] || return 1
  IFS='/' read -r -a parts <<< "${directory#/}"
  for part in "${parts[@]}"; do
    [[ -n "$part" ]] || continue
    [[ "$part" != "." && "$part" != ".." ]] || return 1
    current="$current$part"
    [[ -d "$current" && ! -L "$current" ]] || return 1
    current="$current/"
  done
}

path_chain_blocks_other_users() {
  local entry="$1" effective_uid="$2"
  local container container_mode container_mode_value container_uid entry_uid root_uid

  root_uid="$(stat -c '%u' -- /)" || return 1
  while [[ "$entry" != "/" ]]; do
    container="${entry%/*}"
    [[ -n "$container" ]] || container=/
    entry_uid="$(stat -c '%u' -- "$entry")" || return 1
    container_uid="$(stat -c '%u' -- "$container")" || return 1
    container_mode="$(stat -c '%a' -- "$container")" || return 1
    [[ "$container_mode" =~ ^[0-7]{3,4}$ ]] || return 1
    [[ "$container_uid" == "$root_uid" || "$container_uid" == "$effective_uid" ]] || return 1
    container_mode_value=$((8#$container_mode))
    if (( (container_mode_value & 0022) != 0 )); then
      (( (container_mode_value & 01000) != 0 )) || return 1
      [[ "$entry_uid" == "$root_uid" || "$entry_uid" == "$effective_uid" ]] || return 1
    fi
    entry="$container"
  done
}

path_identity() {
  stat -c '%d:%i:%u:%a:%F' -- "$1"
}

validate_python() {
  local path="$1" mode
  [[ "$path" == /* ]] || fail "observer Python path must be absolute"
  path_has_safe_unit_syntax "$path" || fail "observer Python path is unsafe"
  safe_directory_ancestry "$(dirname -- "$path")" || fail "observer Python parent is unsafe"
  [[ -f "$path" && -x "$path" && ! -L "$path" ]] || fail "observer Python must be regular, executable, and not a symlink"
  mode="$(stat -c '%a' -- "$path")"
  (( (8#$mode & 0022) == 0 )) || fail "observer Python must not be writable by group or others"
  path_chain_blocks_other_users "$path" "$(id -u)" || fail "observer Python path ancestry permits replacement by other users"
}

validate_private_environment() {
  local path="$1" owner mode
  [[ "$path" == /* ]] || fail "observer private environment path must be absolute"
  path_has_safe_unit_syntax "$path" || fail "observer private environment path is unsafe"
  safe_directory_ancestry "$(dirname -- "$path")" || fail "observer private environment parent is unsafe"
  [[ -f "$path" && ! -L "$path" ]] || fail "observer private environment must be a regular non-symlink file"
  owner="$(stat -c '%u' -- "$path")"
  mode="$(stat -c '%a' -- "$path")"
  [[ "$owner" == "$(id -u)" && "$mode" == "600" ]] || fail "observer private environment must be owned by this user and mode 0600"
  path_chain_blocks_other_users "$path" "$(id -u)" || fail "observer private environment path ancestry permits replacement by other users"
}

revalidate_configured_paths() {
  local current_python_identity current_private_env_identity

  validate_python "$OBSERVER_PYTHON"
  validate_private_environment "$PRIVATE_ENV"
  current_python_identity="$(path_identity "$OBSERVER_PYTHON")"
  current_private_env_identity="$(path_identity "$PRIVATE_ENV")"
  [[ "$current_python_identity" == "$VALIDATED_PYTHON_IDENTITY" ]] || fail "observer Python changed during verification"
  [[ "$current_private_env_identity" == "$VALIDATED_PRIVATE_ENV_IDENTITY" ]] || fail "observer private environment changed during verification"
}

validate_render_parent() {
  local path="$1" owner mode
  safe_directory_ancestry "$path" || fail "unsafe --render-dir parent"
  owner="$(stat -c '%u' -- "$path")"
  mode="$(stat -c '%a' -- "$path")"
  [[ "$owner" == "$(id -u)" && "$mode" == "700" ]] || fail "--render-dir parent must be owned by this user and mode 0700"
}

render_parent_identity() {
  stat -c '%d:%i:%u:%a' -- "$1"
}

escape_sed() {
  sed 's/[\\&|]/\\&/g' <<< "$1"
}

render_unit() {
  local output="$1"
  sed \
    -e "s|__REPO_ROOT__|$(escape_sed "$REPO_ROOT")|g" \
    -e "s|__PYTHON__|$(escape_sed "$OBSERVER_PYTHON")|g" \
    -e "s|__ENV_FILE__|$(escape_sed "$PRIVATE_ENV")|g" \
    "$TEMPLATE" > "$output"
  chmod 0644 -- "$output"
}

verify_stage() {
  local analyzer
  analyzer="$(command -v systemd-analyze || true)"
  [[ -n "$analyzer" ]] || fail "systemd-analyze is required for observer rendering"
  "$analyzer" --user verify "$STAGING_DIR/$UNIT_NAME" &
  VERIFIER_PID="$!"
  if ! wait "$VERIFIER_PID"; then
    VERIFIER_PID=""
    fail "systemd-analyze verify failed"
  fi
  VERIFIER_PID=""
}

[[ $# -ge 1 ]] || { usage >&2; exit 2; }
case "$1" in
  --check-only)
    [[ $# -eq 1 ]] || fail "--check-only accepts no other arguments"
    MODE="check-only"
    ;;
  --render-dir)
    [[ $# -eq 2 ]] || fail "--render-dir requires one absolute new directory"
    MODE="render-dir"
    RENDER_TARGET="$2"
    ;;
  --help|-h)
    [[ $# -eq 1 ]] || fail "--help accepts no other arguments"
    usage
    exit 0
    ;;
  *)
    fail "unsupported observer installer option"
    ;;
esac

[[ -f "$TEMPLATE" && ! -L "$TEMPLATE" ]] || fail "observer service template is unavailable"
path_has_safe_unit_syntax "$REPO_ROOT" || fail "observer repository path is unsafe"
safe_directory_ancestry "$REPO_ROOT" || fail "observer repository path is unsafe"
OBSERVER_PYTHON="${BRAIN_DELIVERY_OBSERVER_PYTHON:-}"
PRIVATE_ENV="${BRAIN_DELIVERY_OBSERVER_ENV_FILE:-}"
[[ -n "$OBSERVER_PYTHON" && -n "$PRIVATE_ENV" ]] || fail "observer Python and private environment paths are required"
validate_python "$OBSERVER_PYTHON"
validate_private_environment "$PRIVATE_ENV"
VALIDATED_PYTHON_IDENTITY="$(path_identity "$OBSERVER_PYTHON")"
VALIDATED_PRIVATE_ENV_IDENTITY="$(path_identity "$PRIVATE_ENV")"

if [[ "$MODE" == "check-only" ]]; then
  temporary_root="${TMPDIR:-/tmp}"
  safe_directory_ancestry "$temporary_root" || fail "temporary staging directory is unsafe"
  umask 0077
  STAGING_DIR="$(mktemp -d "$temporary_root/.brain-v42-delivery-observer.XXXXXX")"
  render_unit "$STAGING_DIR/$UNIT_NAME"
  verify_stage
  revalidate_configured_paths
  printf '%s\n' "[delivery-observer] check-only: verified isolated render"
  exit 0
fi

[[ "$RENDER_TARGET" == /* ]] || fail "--render-dir requires an absolute target"
path_has_safe_unit_syntax "$RENDER_TARGET" || fail "unsafe --render-dir target"
RENDER_PARENT="$(dirname -- "$RENDER_TARGET")"
validate_render_parent "$RENDER_PARENT"
RENDER_PARENT_IDENTITY="$(render_parent_identity "$RENDER_PARENT")"
[[ ! -e "$RENDER_TARGET" && ! -L "$RENDER_TARGET" ]] || fail "--render-dir target must be a new directory"
umask 0077
STAGING_DIR="$(mktemp -d "$RENDER_PARENT/.brain-v42-delivery-observer.XXXXXX")"
render_unit "$STAGING_DIR/$UNIT_NAME"
verify_stage

revalidate_configured_paths
validate_render_parent "$RENDER_PARENT"
[[ "$(render_parent_identity "$RENDER_PARENT")" == "$RENDER_PARENT_IDENTITY" ]] || fail "--render-dir parent changed before publication"
[[ ! -e "$RENDER_TARGET" && ! -L "$RENDER_TARGET" ]] || fail "--render-dir target appeared before publication"

# GNU mv's no-clobber, no-target-directory path uses a same-filesystem rename
# when available. If another writer creates the destination, retain staging and
# fail rather than replacing or merging with that directory.
if ! mv -T -n -- "$STAGING_DIR" "$RENDER_TARGET"; then
  fail "--render-dir publication failed without replacing the target"
fi
if [[ -d "$STAGING_DIR" || ! -d "$RENDER_TARGET" || -L "$RENDER_TARGET" ]]; then
  fail "--render-dir target appeared during no-clobber publication"
fi
STAGING_DIR=""
printf '%s\n' "[delivery-observer] render-dir: published verified observer unit"
