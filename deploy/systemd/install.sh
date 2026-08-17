#!/usr/bin/env bash
#
# Install brain-v42 systemd user units (Dream, graph recon, MCP HTTP, automation).
#
# Usage:
#   ./deploy/systemd/install.sh             # install + enable + start timer
#   ./deploy/systemd/install.sh --dry-run   # only generate files, no systemctl
#   ./deploy/systemd/install.sh --check-only # isolated render + mandatory verify
#   ./deploy/systemd/install.sh --render-dir /absolute/new/path
#   ./deploy/systemd/install.sh --uninstall # stop, disable, remove every managed unit
# WARNING: --uninstall affects every managed unit, including production MCP HTTP.
#
# The service template contains __REPO_ROOT__ placeholders that are
# replaced with the absolute path of this repository at install time.
# This keeps the committed template portable across machines.
#
# Host-local environment (e.g. Dream killswitches BRAIN_DREAM_*) must live
# in a systemd drop-in — ~/.config/systemd/user/<unit>.service.d/*.conf —
# NEVER as hand-added Environment= lines in the generated .service file:
# every reinstall regenerates the unit from the template and wipes them
# (incident 2026-06-30: PROMOTE+REORG silently disabled for 2 nights).
#
# On a normal install, MCP HTTP units (brain-mcp-http.service,
# brain-mcp-http-watchdog.service, brain-mcp-http-watchdog.timer) are generated
# and validated but never auto-enabled. Their production lifecycle is
# operator-managed. The explicit --uninstall path stops, disables and removes them.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

# Two paired (service, timer) sets:
#   - dream:       daily 06:00 — knowledge consolidation pipeline
#   - graph-recon: weekly Sunday 04:00 — read-only graph ledger inventory
UNITS=(
  "brain-v42-dream"
  "brain-v42-graph-recon"
)

MANAGED_UNIT_FILES=(
  brain-v42-dream.service
  brain-v42-dream.timer
  brain-v42-graph-recon.service
  brain-v42-graph-recon.timer
  brain-mcp-http.service
  brain-mcp-http-watchdog.service
  brain-mcp-http-watchdog.timer
  brain-v42-automation.service
  brain-v42-embedding-backfill.service
  brain-v42-embedding-backfill.timer
)

# The repository-managed production client and systemd path use one fixed port.
REQUESTED_MCP_HTTP_HOST="${MCP_HTTP_HOST:-127.0.0.1}"
REQUESTED_MCP_HTTP_PORT="${MCP_HTTP_PORT:-8765}"
MCP_HTTP_HOST=127.0.0.1
MCP_HTTP_PORT=8765
readonly REQUESTED_MCP_HTTP_HOST REQUESTED_MCP_HTTP_PORT MCP_HTTP_HOST MCP_HTTP_PORT

MODE=install
RENDER_TARGET=""

select_mode() {
  local requested_mode="$1"

  if [[ "$MODE" != "install" ]]; then
    echo "ERROR: installer modes cannot be combined or repeated; no units were changed." >&2
    exit 2
  fi
  MODE="$requested_mode"
}

while (($# > 0)); do
  case "$1" in
    --dry-run)
      select_mode dry_run
      shift
      ;;
    --uninstall)
      select_mode uninstall
      shift
      ;;
    --check-only)
      select_mode check_only
      shift
      ;;
    --render-dir)
      select_mode render_dir
      if (($# < 2)) || [[ "$2" == --* ]]; then
        echo "ERROR: --render-dir requires an absolute target path; no units were changed." >&2
        exit 2
      fi
      RENDER_TARGET="$2"
      shift 2
      ;;
    -h|--help)
      if (($# != 1)) || [[ "$MODE" != "install" ]]; then
        echo "ERROR: --help cannot be combined with installer modes." >&2
        exit 2
      fi
      sed -n '3,20p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

DRY_RUN=false
UNINSTALL=false
[[ "$MODE" == "dry_run" ]] && DRY_RUN=true
[[ "$MODE" == "uninstall" ]] && UNINSTALL=true

log() { echo "[install] $*"; }

VALIDATED_RENDER_PARENT=""
VALIDATED_RENDER_PARENT_ID=""
VALIDATED_RENDER_BASENAME=""
VALIDATED_RENDER_UID=""

render_target_error() {
  echo "ERROR: unsafe --render-dir target: $1; no units were changed." >&2
  return 2
}

path_chain_blocks_other_users() {
  local entry="$1"
  local effective_uid="$2"
  local container
  local container_mode
  local container_mode_value
  local container_uid
  local entry_uid
  local root_uid

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
      # A writable ancestor is safe only with sticky ownership semantics, as on /tmp.
      (( (container_mode_value & 01000) != 0 )) || return 1
      [[ "$entry_uid" == "$root_uid" || "$entry_uid" == "$effective_uid" ]] || return 1
    fi
    entry="$container"
  done
}

validate_render_target() {
  local target="$RENDER_TARGET"
  local target_parent
  local target_basename
  local canonical_parent
  local canonical_target
  local canonical_xdg
  local canonical_user_unit_dir
  local effective_uid
  local parent_uid
  local parent_mode
  local parent_mode_value

  [[ "$target" == /* ]] || {
    render_target_error "the path must be absolute"
    return
  }
  [[ "$target" != *$'\n'* ]] || {
    render_target_error "newlines are not accepted"
    return
  }
  case "$target" in
    "$USER_UNIT_DIR"|"$USER_UNIT_DIR"/*)
      render_target_error "the target must stay outside the live user unit directory"
      return
      ;;
  esac
  if [[ -e "$target" || -L "$target" ]]; then
    render_target_error "the target must not already exist"
    return
  fi

  target_parent="${target%/*}"
  target_basename="${target##*/}"
  [[ -n "$target_parent" && -n "$target_basename" ]] || {
    render_target_error "the target must name a new directory"
    return
  }
  [[ "$target_basename" != "." && "$target_basename" != ".." ]] || {
    render_target_error "dot path components are not accepted"
    return
  }
  [[ -d "$target_parent" && ! -L "$target_parent" ]] || {
    render_target_error "the parent must be an existing real directory"
    return
  }

  if ! canonical_parent="$(realpath -e -- "$target_parent")"; then
    render_target_error "the parent cannot be canonicalized"
    return
  fi
  canonical_target="$canonical_parent/$target_basename"
  [[ "$target" == "$canonical_target" ]] || {
    render_target_error "every path component must already be canonical and non-symlinked"
    return
  }

  if ! canonical_xdg="$(realpath -m -- "${XDG_CONFIG_HOME:-$HOME/.config}")"; then
    render_target_error "the user configuration root cannot be canonicalized"
    return
  fi
  if [[ -L "$canonical_xdg/systemd" || -L "$canonical_xdg/systemd/user" ]]; then
    render_target_error "the live user unit path must not contain symlink components"
    return
  fi
  canonical_user_unit_dir="$canonical_xdg/systemd/user"
  case "$canonical_target" in
    "$canonical_user_unit_dir"|"$canonical_user_unit_dir"/*)
      render_target_error "the target must stay outside the live user unit directory"
      return
      ;;
  esac

  effective_uid="$(id -u)"
  parent_uid="$(stat -c '%u' -- "$canonical_parent")" || {
    render_target_error "the parent owner cannot be inspected"
    return
  }
  [[ "$parent_uid" == "$effective_uid" ]] || {
    render_target_error "the parent must be owned by the current user"
    return
  }
  parent_mode="$(stat -c '%a' -- "$canonical_parent")" || {
    render_target_error "the parent permissions cannot be inspected"
    return
  }
  [[ "$parent_mode" =~ ^[0-7]{3,4}$ ]] || {
    render_target_error "the parent permissions are invalid"
    return
  }
  parent_mode_value=$((8#$parent_mode))
  if (( (parent_mode_value & 0300) != 0300 )); then
    render_target_error "the parent must be writable and searchable by its owner"
    return
  fi
  if (( (parent_mode_value & 0022) != 0 )); then
    render_target_error "the parent must not be writable by group or others"
    return
  fi
  if ! path_chain_blocks_other_users "$canonical_parent" "$effective_uid"; then
    render_target_error "the parent ancestry permits replacement by another user"
    return
  fi

  VALIDATED_RENDER_PARENT="$canonical_parent"
  VALIDATED_RENDER_BASENAME="$target_basename"
  VALIDATED_RENDER_UID="$effective_uid"
  VALIDATED_RENDER_PARENT_ID="$(stat -c '%d:%i' -- "$canonical_parent")" || {
    render_target_error "the parent identity cannot be inspected"
    return
  }
}

if [[ "$MODE" == "render_dir" ]]; then
  validate_render_target
fi

# Warn loudly when regenerating a unit would wipe hand-added Environment=
# lines (they belong in a <unit>.service.d/*.conf drop-in, which survives
# regeneration). Backstop for the 2026-06-30 killswitch-wipe incident.
count_environment_directives() {
  local unit_file="$1"

  awk '
    function count_environment(logical_line) {
      if (logical_line ~ /^[[:space:]]*Environment[[:space:]]*=/) {
        environment_count++
      }
    }

    {
      physical_line = $0
      sub(/\r$/, "", physical_line)
      if (physical_line ~ /^[[:space:]]*[#;]/) {
        next
      }

      trailing_backslashes = 0
      position = length(physical_line)
      while (position > 0 && substr(physical_line, position, 1) == "\\") {
        trailing_backslashes++
        position--
      }

      if (trailing_backslashes % 2 == 1) {
        logical_line = logical_line substr(physical_line, 1, length(physical_line) - 1) " "
        next
      }

      logical_line = logical_line physical_line
      count_environment(logical_line)
      logical_line = ""
    }

    END {
      if (logical_line != "") {
        count_environment(logical_line)
      }
      print environment_count + 0
    }
  ' "$unit_file" 2>/dev/null
}

warn_wiped_env() {
  local unit_file="$1"
  local environment_count
  local scanner_status

  [[ -f "$unit_file" ]] || return 0
  if environment_count="$(count_environment_directives "$unit_file")"; then
    :
  else
    scanner_status=$?
    echo "ERROR: failed to inspect Environment= directives in $unit_file (scanner exit $scanner_status)" >&2
    return "$scanner_status"
  fi
  if [[ ! "$environment_count" =~ ^(0|[1-9][0-9]*)$ ]]; then
    echo "ERROR: invalid Environment= directive count from scanner for $unit_file" >&2
    return 1
  fi
  if ((environment_count == 0)); then
    return 0
  fi

  echo "WARN: $unit_file carries Environment= lines that this reinstall WIPES:" >&2
  echo "      $environment_count Environment= directives; values redacted." >&2
  echo "      Move them to ${unit_file}.d/*.conf (drop-ins survive regeneration)." >&2
}

unit_state() {
  local state_command="$1"
  local unit="$2"
  local state

  state="$(systemctl --user "$state_command" "$unit" 2>/dev/null || true)"
  [[ -n "$state" ]] || {
    echo "ERROR: unable to determine $state_command state for $unit." >&2
    return 1
  }
  printf '%s\n' "$state"
}

unit_requires_disable() {
  local unit="$1"
  local active_state
  local enabled_state

  [[ -e "$USER_UNIT_DIR/$unit" ]] && return 0
  active_state="$(unit_state is-active "$unit")" || return 1
  enabled_state="$(unit_state is-enabled "$unit")" || return 1
  case "$active_state" in
    active|activating|reloading|deactivating) return 0 ;;
    inactive|failed|unknown) ;;
    *)
      echo "ERROR: unexpected active state for $unit." >&2
      return 1
      ;;
  esac
  case "$enabled_state" in
    enabled|enabled-runtime|linked|linked-runtime) return 0 ;;
    disabled|static|masked|not-found) return 1 ;;
    *)
      echo "ERROR: unexpected enablement state for $unit." >&2
      return 1
      ;;
  esac
}

assert_unit_inactive() {
  local unit="$1"
  local state

  state="$(unit_state is-active "$unit")" || return 1
  case "$state" in
    inactive|failed|unknown) ;;
    *)
      echo "ERROR: $unit remains active after quiesce." >&2
      return 1
      ;;
  esac
}

assert_unit_disabled() {
  local unit="$1"
  local state

  state="$(unit_state is-enabled "$unit")" || return 1
  case "$state" in
    disabled|static|masked|not-found) ;;
    *)
      echo "ERROR: $unit remains enabled after quiesce." >&2
      return 1
      ;;
  esac
}

disable_and_stop_unit() {
  local unit="$1"

  if unit_requires_disable "$unit"; then
    systemctl --user disable --now "$unit"
  fi
  assert_unit_inactive "$unit"
  assert_unit_disabled "$unit"
}

stop_unit() {
  local unit="$1"

  if [[ -e "$USER_UNIT_DIR/$unit" ]]; then
    systemctl --user stop "$unit"
  else
    case "$(unit_state is-active "$unit")" in
      active|activating|reloading|deactivating) systemctl --user stop "$unit" ;;
      inactive|failed|unknown) ;;
      *)
        echo "ERROR: unexpected active state for $unit." >&2
        return 1
        ;;
    esac
  fi
  assert_unit_inactive "$unit"
}

require_mcp_watchdog_quiescent() {
  local timer_active
  local timer_enabled
  local service_active

  timer_active="$(unit_state is-active brain-mcp-http-watchdog.timer)" || return 1
  timer_enabled="$(unit_state is-enabled brain-mcp-http-watchdog.timer)" || return 1
  service_active="$(unit_state is-active brain-mcp-http-watchdog.service)" || return 1
  case "$timer_active" in
    inactive|failed|unknown) ;;
    *)
      echo "ERROR: MCP HTTP watchdog must be inactive and disabled before installation." >&2
      return 1
      ;;
  esac
  case "$timer_enabled" in
    disabled|static|masked|not-found) ;;
    *)
      echo "ERROR: MCP HTTP watchdog must be inactive and disabled before installation." >&2
      return 1
      ;;
  esac
  case "$service_active" in
    inactive|failed|unknown) ;;
    *)
      echo "ERROR: MCP HTTP watchdog must be inactive and disabled before installation." >&2
      return 1
      ;;
  esac
}

# --- Uninstall branch ---
if $UNINSTALL; then
  log "stopping + disabling managed units"
  systemctl --user show-environment >/dev/null
  for unit in "${UNITS[@]}"; do
    disable_and_stop_unit "$unit.timer"
    disable_and_stop_unit "$unit.service"
  done
  disable_and_stop_unit brain-v42-automation.service
  disable_and_stop_unit brain-v42-embedding-backfill.timer
  disable_and_stop_unit brain-v42-embedding-backfill.service
  disable_and_stop_unit brain-mcp-http-watchdog.timer
  disable_and_stop_unit brain-mcp-http-watchdog.service
  disable_and_stop_unit brain-mcp-http.service
  # Do not remove unit files until every process is quiesced and every enablement
  # state is safe; a failed command leaves a retryable installation on disk.
  systemctl --user show-environment >/dev/null
  for unit in "${UNITS[@]}"; do
    rm -f "$USER_UNIT_DIR/$unit.service" "$USER_UNIT_DIR/$unit.timer"
  done
  rm -f "$USER_UNIT_DIR/brain-v42-automation.service"
  rm -f "$USER_UNIT_DIR/brain-v42-embedding-backfill.service"
  rm -f "$USER_UNIT_DIR/brain-v42-embedding-backfill.timer"
  rm -f "$USER_UNIT_DIR/brain-mcp-http.service"
  rm -f "$USER_UNIT_DIR/brain-mcp-http-watchdog.service"
  rm -f "$USER_UNIT_DIR/brain-mcp-http-watchdog.timer"
  systemctl --user daemon-reload
  log "uninstalled"
  exit 0
fi

# --- Sanity checks ---
if [[ "$REQUESTED_MCP_HTTP_PORT" != "$MCP_HTTP_PORT" ]]; then
  echo "ERROR: the production systemd MCP HTTP port is fixed to 8765." >&2
  exit 2
fi
if [[ "$REQUESTED_MCP_HTTP_HOST" != "$MCP_HTTP_HOST" ]]; then
  echo "ERROR: the production systemd MCP HTTP host is fixed to 127.0.0.1." >&2
  exit 2
fi
[[ -x "$REPO_ROOT/.venv/bin/python" ]] || {
  echo "missing project interpreter: $REPO_ROOT/.venv/bin/python" >&2
  exit 1
}
[[ -f "$REPO_ROOT/scripts/check_mcp_http_port.py" ]] || {
  echo "missing MCP port preflight: $REPO_ROOT/scripts/check_mcp_http_port.py" >&2
  exit 1
}
[[ -f "$REPO_ROOT/scripts/check_graph_projector_env.py" ]] || {
  echo "missing graph projector preflight: $REPO_ROOT/scripts/check_graph_projector_env.py" >&2
  exit 1
}
"$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/scripts/check_graph_projector_env.py" \
  --shared "$REPO_ROOT/.env" \
  --private "$HOME/.config/brain-v42/graph-projector.env"
"$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/scripts/check_mcp_http_port.py" \
  --shared "$REPO_ROOT/.env" --expected "$MCP_HTTP_PORT" --expected-host "$MCP_HTTP_HOST" \
  --token-file "$HOME/.config/brain-v42/mcp-token.env"

for unit in "${UNITS[@]}"; do
  [[ -f "$SCRIPT_DIR/$unit.service.tmpl" ]] || {
    echo "missing template: $SCRIPT_DIR/$unit.service.tmpl" >&2
    exit 1
  }
  [[ -f "$SCRIPT_DIR/$unit.timer" ]] || {
    echo "missing timer: $SCRIPT_DIR/$unit.timer" >&2
    exit 1
  }
done
[[ -f "$REPO_ROOT/scripts/dream.sh" ]] || {
  echo "scripts/dream.sh not found at $REPO_ROOT" >&2
  exit 1
}
[[ -x "$REPO_ROOT/scripts/dream.sh" ]] || {
  echo "scripts/dream.sh is not executable" >&2
  exit 1
}

for tmpl in brain-mcp-http.service.tmpl brain-mcp-http-watchdog.service.tmpl brain-mcp-http-watchdog.timer.tmpl; do
  [[ -f "$SCRIPT_DIR/$tmpl" ]] || {
    echo "missing MCP template: $SCRIPT_DIR/$tmpl" >&2
    exit 1
  }
done

AUTOMATION_TEMPLATE="$SCRIPT_DIR/brain-v42-automation.service.tmpl"
[[ -f "$AUTOMATION_TEMPLATE" ]] || {
  echo "ERROR: missing automation template: $AUTOMATION_TEMPLATE" >&2
  exit 1
}
BACKFILL_TEMPLATE="$SCRIPT_DIR/brain-v42-embedding-backfill.service.tmpl"
BACKFILL_TIMER="$SCRIPT_DIR/brain-v42-embedding-backfill.timer"
[[ -f "$BACKFILL_TEMPLATE" && -f "$BACKFILL_TIMER" ]] || {
  echo "ERROR: missing embedding backfill unit template or timer" >&2
  exit 1
}

ISOLATED_STAGING_ROOT=""
ISOLATED_STAGING_PARENT=""
ISOLATED_STAGING_PREFIX=""
ISOLATED_STAGING_ID=""
ISOLATED_RENDER_DIR=""
ISOLATED_SOURCE_ID=""
ISOLATED_SUCCESS=false
SYSTEMD_ANALYZE=""

cleanup_isolated_staging() {
  local status=$?
  local cleanup_failed=false
  local current_staging_id=""
  local quarantined_entry_id=""
  local restored_entry_id=""
  local preserve_staging=false

  trap - EXIT
  trap '' INT TERM
  set +e

  if [[ "$MODE" == "render_dir" && "$ISOLATED_SUCCESS" != "true" \
    && -n "$ISOLATED_SOURCE_ID" && ( -e "$RENDER_TARGET" || -L "$RENDER_TARGET" ) ]]; then
    # Move the entry back into the private staging tree before inspecting it. This closes the
    # target stat->rm race: cleanup never recursively deletes RENDER_TARGET by pathname.
    if [[ ! -e "$ISOLATED_RENDER_DIR" && ! -L "$ISOLATED_RENDER_DIR" ]] \
      && /usr/bin/mv -T --no-clobber -- "$RENDER_TARGET" "$ISOLATED_RENDER_DIR"; then
      if [[ -e "$ISOLATED_RENDER_DIR" || -L "$ISOLATED_RENDER_DIR" ]]; then
        quarantined_entry_id="$(stat -c '%d:%i' -- "$ISOLATED_RENDER_DIR" 2>/dev/null)"
      fi
    fi
    if [[ -n "$quarantined_entry_id" && "$quarantined_entry_id" == "$ISOLATED_SOURCE_ID" \
      && -d "$ISOLATED_RENDER_DIR" && ! -L "$ISOLATED_RENDER_DIR" ]]; then
      : # The owned rendered directory is quarantined and removed with its staging root below.
    elif [[ -n "$quarantined_entry_id" ]]; then
      # A concurrent replacement was quarantined. Restore that exact entry; never delete it.
      if [[ ! -e "$RENDER_TARGET" && ! -L "$RENDER_TARGET" ]] \
        && /usr/bin/mv -T --no-clobber -- "$ISOLATED_RENDER_DIR" "$RENDER_TARGET"; then
        if [[ -e "$RENDER_TARGET" || -L "$RENDER_TARGET" ]]; then
          restored_entry_id="$(stat -c '%d:%i' -- "$RENDER_TARGET" 2>/dev/null)"
        fi
      fi
      if [[ "$restored_entry_id" == "$quarantined_entry_id" \
        && ! -e "$ISOLATED_RENDER_DIR" && ! -L "$ISOLATED_RENDER_DIR" ]]; then
        echo "WARN: preserved a replaced --render-dir target during cleanup." >&2
      else
        echo "ERROR: could not restore a replaced --render-dir target; recovery data preserved in $ISOLATED_STAGING_ROOT." >&2
        preserve_staging=true
        cleanup_failed=true
      fi
    else
      echo "WARN: refusing to move an unverified --render-dir target during cleanup." >&2
    fi
  fi

  if [[ -n "$ISOLATED_STAGING_ROOT" && "$preserve_staging" != "true" ]]; then
    case "$ISOLATED_STAGING_ROOT" in
      "$ISOLATED_STAGING_PARENT"/"$ISOLATED_STAGING_PREFIX"*)
        if [[ -d "$ISOLATED_STAGING_ROOT" && ! -L "$ISOLATED_STAGING_ROOT" ]]; then
          current_staging_id="$(stat -c '%d:%i' -- "$ISOLATED_STAGING_ROOT" 2>/dev/null)"
          if [[ -n "$ISOLATED_STAGING_ID" && "$current_staging_id" == "$ISOLATED_STAGING_ID" ]]; then
            if ! rm -rf -- "$ISOLATED_STAGING_ROOT"; then
              echo "ERROR: failed to remove the owned isolated staging directory." >&2
              cleanup_failed=true
            fi
          else
            echo "WARN: refusing to remove a replaced isolated staging directory." >&2
            cleanup_failed=true
          fi
        elif [[ -e "$ISOLATED_STAGING_ROOT" || -L "$ISOLATED_STAGING_ROOT" ]]; then
          echo "WARN: refusing to remove a replaced isolated staging path." >&2
          cleanup_failed=true
        fi
        ;;
      *)
        echo "WARN: refusing to remove an unexpected isolated staging path." >&2
        cleanup_failed=true
        ;;
    esac
  fi

  if [[ "$status" == "0" && "$cleanup_failed" == "true" ]]; then
    status=1
  fi
  exit "$status"
}

prepare_isolated_staging() {
  local staging_template

  if [[ "$MODE" == "check_only" ]]; then
    ISOLATED_STAGING_PARENT=/tmp
    ISOLATED_STAGING_PREFIX=brain-v42-systemd-check.
  else
    ISOLATED_STAGING_PARENT="$VALIDATED_RENDER_PARENT"
    ISOLATED_STAGING_PREFIX=".${VALIDATED_RENDER_BASENAME}.brain-v42-render."
  fi
  staging_template="$ISOLATED_STAGING_PARENT/${ISOLATED_STAGING_PREFIX}XXXXXX"

  trap cleanup_isolated_staging EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  ISOLATED_STAGING_ROOT="$(mktemp -d "$staging_template")"
  chmod 700 "$ISOLATED_STAGING_ROOT"
  ISOLATED_STAGING_ID="$(stat -c '%d:%i' -- "$ISOLATED_STAGING_ROOT")"
  ISOLATED_RENDER_DIR="$ISOLATED_STAGING_ROOT/rendered"
  mkdir -m 700 \
    "$ISOLATED_RENDER_DIR" \
    "$ISOLATED_STAGING_ROOT/verifier-home" \
    "$ISOLATED_STAGING_ROOT/verifier-config" \
    "$ISOLATED_STAGING_ROOT/verifier-cache" \
    "$ISOLATED_STAGING_ROOT/verifier-data" \
    "$ISOLATED_STAGING_ROOT/verifier-state" \
    "$ISOLATED_STAGING_ROOT/verifier-runtime"
}

render_isolated_units() {
  local unit

  for unit in "${UNITS[@]}"; do
    sed "s|__REPO_ROOT__|$REPO_ROOT|g" "$SCRIPT_DIR/$unit.service.tmpl" \
      > "$ISOLATED_RENDER_DIR/$unit.service"
    cp "$SCRIPT_DIR/$unit.timer" "$ISOLATED_RENDER_DIR/$unit.timer"
  done
  sed "s|__REPO_ROOT__|$REPO_ROOT|g" \
    "$SCRIPT_DIR/brain-mcp-http.service.tmpl" \
    > "$ISOLATED_RENDER_DIR/brain-mcp-http.service"
  sed -e "s|__REPO_ROOT__|$REPO_ROOT|g" \
      -e "s|__MCP_PORT__|$MCP_HTTP_PORT|g" \
    "$SCRIPT_DIR/brain-mcp-http-watchdog.service.tmpl" \
    > "$ISOLATED_RENDER_DIR/brain-mcp-http-watchdog.service"
  cp "$SCRIPT_DIR/brain-mcp-http-watchdog.timer.tmpl" \
    "$ISOLATED_RENDER_DIR/brain-mcp-http-watchdog.timer"
  sed "s|__REPO_ROOT__|$REPO_ROOT|g" \
    "$AUTOMATION_TEMPLATE" \
    > "$ISOLATED_RENDER_DIR/brain-v42-automation.service"
  sed "s|__REPO_ROOT__|$REPO_ROOT|g" \
    "$BACKFILL_TEMPLATE" \
    > "$ISOLATED_RENDER_DIR/brain-v42-embedding-backfill.service"
  cp "$BACKFILL_TIMER" "$ISOLATED_RENDER_DIR/brain-v42-embedding-backfill.timer"
}

validate_isolated_artifacts() {
  local artifact
  local entries=()

  shopt -s nullglob dotglob
  entries=("$ISOLATED_RENDER_DIR"/*)
  shopt -u nullglob dotglob
  if ((${#entries[@]} != ${#MANAGED_UNIT_FILES[@]})); then
    echo "ERROR: isolated render produced an unexpected artifact count." >&2
    return 1
  fi
  for artifact in "${MANAGED_UNIT_FILES[@]}"; do
    if [[ ! -f "$ISOLATED_RENDER_DIR/$artifact" || -L "$ISOLATED_RENDER_DIR/$artifact" ]]; then
      echo "ERROR: isolated render did not produce the expected regular file: $artifact" >&2
      return 1
    fi
    if grep -Eq '__REPO_ROOT__|__MCP_PORT__' "$ISOLATED_RENDER_DIR/$artifact"; then
      echo "ERROR: isolated render left an unresolved placeholder in $artifact" >&2
      return 1
    fi
  done
}

verify_isolated_artifacts() {
  local verify_paths=()
  local artifact
  local unit_path

  for artifact in "${MANAGED_UNIT_FILES[@]}"; do
    verify_paths+=("$ISOLATED_RENDER_DIR/$artifact")
  done
  unit_path="$ISOLATED_RENDER_DIR:/usr/local/lib/systemd/user:/usr/local/share/systemd/user:/usr/lib/systemd/user:/usr/share/systemd/user:/lib/systemd/user"
  /usr/bin/env -i \
    HOME="$ISOLATED_STAGING_ROOT/verifier-home" \
    LANG=C.UTF-8 \
    PATH=/usr/bin:/bin \
    SYSTEMD_UNIT_PATH="$unit_path" \
    XDG_CACHE_HOME="$ISOLATED_STAGING_ROOT/verifier-cache" \
    XDG_CONFIG_HOME="$ISOLATED_STAGING_ROOT/verifier-config" \
    XDG_DATA_HOME="$ISOLATED_STAGING_ROOT/verifier-data" \
    XDG_RUNTIME_DIR="$ISOLATED_STAGING_ROOT/verifier-runtime" \
    XDG_STATE_HOME="$ISOLATED_STAGING_ROOT/verifier-state" \
    "$SYSTEMD_ANALYZE" --user --generators=no --man=no verify "${verify_paths[@]}"
  log "systemd-analyze verify: OK"
}

run_isolated_mode() {
  local current_parent_id
  local current_parent_mode
  local current_parent_mode_value
  local current_parent_real
  local current_parent_uid
  local published_id

  if ! SYSTEMD_ANALYZE="$(command -v systemd-analyze)"; then
    echo "ERROR: systemd-analyze is required for isolated installer modes." >&2
    return 1
  fi

  umask 077
  prepare_isolated_staging
  render_isolated_units
  validate_isolated_artifacts
  verify_isolated_artifacts

  if [[ "$MODE" == "check_only" ]]; then
    log "check-only: no managed units changed"
    ISOLATED_SUCCESS=true
    return 0
  fi

  if [[ ! -d "$VALIDATED_RENDER_PARENT" || -L "$VALIDATED_RENDER_PARENT" ]]; then
    echo "ERROR: --render-dir parent changed before publication." >&2
    return 1
  fi
  current_parent_id="$(stat -c '%d:%i' -- "$VALIDATED_RENDER_PARENT")"
  if [[ "$current_parent_id" != "$VALIDATED_RENDER_PARENT_ID" ]]; then
    echo "ERROR: --render-dir parent identity changed before publication." >&2
    return 1
  fi
  current_parent_real="$(realpath -e -- "$VALIDATED_RENDER_PARENT")"
  current_parent_uid="$(stat -c '%u' -- "$VALIDATED_RENDER_PARENT")"
  current_parent_mode="$(stat -c '%a' -- "$VALIDATED_RENDER_PARENT")"
  if [[ "$current_parent_real" != "$VALIDATED_RENDER_PARENT" \
    || "$current_parent_uid" != "$VALIDATED_RENDER_UID" \
    || ! "$current_parent_mode" =~ ^[0-7]{3,4}$ ]]; then
    echo "ERROR: --render-dir parent safety changed before publication." >&2
    return 1
  fi
  current_parent_mode_value=$((8#$current_parent_mode))
  if (( (current_parent_mode_value & 0300) != 0300 \
    || (current_parent_mode_value & 0022) != 0 )); then
    echo "ERROR: --render-dir parent permissions changed before publication." >&2
    return 1
  fi
  if ! path_chain_blocks_other_users "$VALIDATED_RENDER_PARENT" "$VALIDATED_RENDER_UID"; then
    echo "ERROR: --render-dir parent ancestry changed before publication." >&2
    return 1
  fi
  if [[ -e "$RENDER_TARGET" || -L "$RENDER_TARGET" ]]; then
    echo "ERROR: --render-dir target appeared before publication." >&2
    return 1
  fi

  ISOLATED_SOURCE_ID="$(stat -c '%d:%i' -- "$ISOLATED_RENDER_DIR")"
  mv -T --no-clobber -- "$ISOLATED_RENDER_DIR" "$RENDER_TARGET"
  if [[ ! -d "$RENDER_TARGET" || -L "$RENDER_TARGET" ]]; then
    echo "ERROR: --render-dir publication did not preserve the rendered directory." >&2
    return 1
  fi
  published_id="$(stat -c '%d:%i' -- "$RENDER_TARGET")"
  if [[ "$published_id" != "$ISOLATED_SOURCE_ID" ]]; then
    echo "ERROR: --render-dir publication identity changed unexpectedly." >&2
    return 1
  fi
  log "render-dir: published verified managed units to $RENDER_TARGET"
  ISOLATED_SUCCESS=true
}

if [[ "$MODE" == "check_only" || "$MODE" == "render_dir" ]]; then
  run_isolated_mode
  exit 0
fi

if [[ "$MODE" == "install" ]]; then
  require_mcp_watchdog_quiescent || {
    echo "ERROR: use --check-only, then --render-dir in a private directory; follow MCP_HTTP_RUNBOOK.md for a canary upgrade." >&2
    exit 2
  }
fi

# Legacy install and dry-run publish into the live user unit directory. Keep every newly
# created directory, backup and unit private regardless of the caller's ambient umask.
umask 077
mkdir -p "$USER_UNIT_DIR"
legacy_effective_uid="$EUID"
if [[ ! -d "$USER_UNIT_DIR" || -L "$USER_UNIT_DIR" ]]; then
  echo "ERROR: unsafe user unit directory; expected a real directory." >&2
  exit 1
fi
legacy_user_unit_uid="$(stat -c '%u' -- "$USER_UNIT_DIR")"
if [[ "$legacy_user_unit_uid" != "$legacy_effective_uid" ]]; then
  echo "ERROR: unsafe user unit directory owner; expected the current user." >&2
  exit 1
fi
if ! path_chain_blocks_other_users "$USER_UNIT_DIR" "$legacy_effective_uid"; then
  echo "ERROR: unsafe user unit directory ancestry permits replacement by another user." >&2
  exit 1
fi
chmod 700 "$USER_UNIT_DIR"
STAGING_DIR="$(mktemp -d "$USER_UNIT_DIR/.brain-v42-install.XXXXXX")"
RENDER_DIR="$STAGING_DIR/rendered"
BACKUP_DIR="$STAGING_DIR/backup"
mkdir -m 700 "$RENDER_DIR" "$BACKUP_DIR"
PUBLISHED_FILES=()

cleanup_staging_dir() {
  case "$STAGING_DIR" in
    "$USER_UNIT_DIR"/.brain-v42-install.*) rm -rf -- "$STAGING_DIR" ;;
    *)
      echo "ERROR: refusing to remove unexpected staging directory." >&2
      return 1
      ;;
  esac
}

rollback_published_units() {
  local status=$?
  local index
  local filename
  local target
  local backup
  local rollback_failed=false

  ((status != 0)) || status=1
  trap - ERR INT TERM
  set +e
  for ((index = ${#PUBLISHED_FILES[@]} - 1; index >= 0; index--)); do
    filename="${PUBLISHED_FILES[$index]}"
    target="$USER_UNIT_DIR/$filename"
    backup="$BACKUP_DIR/$filename"
    if [[ -e "$backup" || -L "$backup" ]]; then
      if ! mv -f -- "$backup" "$target"; then
        echo "ERROR: failed to restore managed unit backup: $filename" >&2
        rollback_failed=true
      fi
    else
      if ! rm -f -- "$target"; then
        echo "ERROR: failed to remove partially published unit: $filename" >&2
        rollback_failed=true
      fi
    fi
  done
  if $rollback_failed; then
    echo "ERROR: unit rollback incomplete; manual recovery backups retained in $BACKUP_DIR" >&2
    exit "$status"
  fi
  if ! cleanup_staging_dir; then
    echo "ERROR: rollback completed but staging cleanup failed; manual recovery backups retained in $BACKUP_DIR" >&2
  fi
  exit "$status"
}

trap rollback_published_units ERR INT TERM

# --- Generate the .service from the template ---
log "repo root: $REPO_ROOT"
log "unit dir:  $USER_UNIT_DIR"

for unit in "${UNITS[@]}"; do
  warn_wiped_env "$USER_UNIT_DIR/$unit.service"
  # sed with | delimiter because REPO_ROOT contains /
  sed "s|__REPO_ROOT__|$REPO_ROOT|g" "$SCRIPT_DIR/$unit.service.tmpl" \
    > "$RENDER_DIR/$unit.service"
  # Timer has no placeholder — straight copy.
  cp "$SCRIPT_DIR/$unit.timer" "$RENDER_DIR/$unit.timer"
  log "rendered $unit.service + $unit.timer"
done

# --- Generate MCP HTTP units (lifecycle remains operator-managed) ---
# Substitute __REPO_ROOT__ in the main service.
warn_wiped_env "$USER_UNIT_DIR/brain-mcp-http.service"
sed "s|__REPO_ROOT__|$REPO_ROOT|g" \
  "$SCRIPT_DIR/brain-mcp-http.service.tmpl" \
  > "$RENDER_DIR/brain-mcp-http.service"
log "rendered brain-mcp-http.service"

# Substitute __REPO_ROOT__ and __MCP_PORT__ in the watchdog service.
warn_wiped_env "$USER_UNIT_DIR/brain-mcp-http-watchdog.service"
sed -e "s|__REPO_ROOT__|$REPO_ROOT|g" \
    -e "s|__MCP_PORT__|$MCP_HTTP_PORT|g" \
  "$SCRIPT_DIR/brain-mcp-http-watchdog.service.tmpl" \
  > "$RENDER_DIR/brain-mcp-http-watchdog.service"
log "rendered brain-mcp-http-watchdog.service (fixed production port $MCP_HTTP_PORT)"

# Watchdog timer has no __REPO_ROOT__ or __MCP_PORT__ — straight copy.
cp "$SCRIPT_DIR/brain-mcp-http-watchdog.timer.tmpl" \
   "$RENDER_DIR/brain-mcp-http-watchdog.timer"
log "rendered brain-mcp-http-watchdog.timer"

# --- Generate automation unit (dormant until the operator follows the runbook) ---
warn_wiped_env "$USER_UNIT_DIR/brain-v42-automation.service"
sed "s|__REPO_ROOT__|$REPO_ROOT|g" \
  "$AUTOMATION_TEMPLATE" \
  > "$RENDER_DIR/brain-v42-automation.service"
log "rendered brain-v42-automation.service"

# --- Generate bounded backfill units (operator-managed, never auto-enabled) ---
warn_wiped_env "$USER_UNIT_DIR/brain-v42-embedding-backfill.service"
sed "s|__REPO_ROOT__|$REPO_ROOT|g" \
  "$BACKFILL_TEMPLATE" \
  > "$RENDER_DIR/brain-v42-embedding-backfill.service"
cp "$BACKFILL_TIMER" "$RENDER_DIR/brain-v42-embedding-backfill.timer"
log "rendered brain-v42-embedding-backfill.service + timer (operator-managed)"

# --- Validate with systemd-analyze ---
if command -v systemd-analyze >/dev/null; then
  for unit in "${UNITS[@]}"; do
    systemd-analyze --user verify "$RENDER_DIR/$unit.service"
    systemd-analyze --user verify "$RENDER_DIR/$unit.timer"
  done
  systemd-analyze --user verify "$RENDER_DIR/brain-mcp-http.service"
  systemd-analyze --user verify "$RENDER_DIR/brain-mcp-http-watchdog.service"
  systemd-analyze --user verify "$RENDER_DIR/brain-mcp-http-watchdog.timer"
  systemd-analyze --user verify "$RENDER_DIR/brain-v42-automation.service"
  systemd-analyze --user verify "$RENDER_DIR/brain-v42-embedding-backfill.service"
  systemd-analyze --user verify "$RENDER_DIR/brain-v42-embedding-backfill.timer"
  log "systemd-analyze verify: OK"
fi

for filename in "${MANAGED_UNIT_FILES[@]}"; do
  target="$USER_UNIT_DIR/$filename"
  if [[ -e "$target" || -L "$target" ]]; then
    cp -a -- "$target" "$BACKUP_DIR/$filename"
  fi
  mv -f -- "$RENDER_DIR/$filename" "$target"
  PUBLISHED_FILES+=("$filename")
done
trap - ERR INT TERM
cleanup_staging_dir
log "published validated managed units"

if $DRY_RUN; then
  log "--dry-run: skipping systemctl reload / enable"
  exit 0
fi

# --- Reload + enable ---
systemctl --user daemon-reload
for unit in "${UNITS[@]}"; do
  systemctl --user enable --now "$unit.timer"
done

# MCP HTTP units: generate + validate only. Their lifecycle is operator-managed;
# this installer deliberately preserves the current enable/start state.
log "brain-mcp-http.* generated and validated; lifecycle remains operator-managed"
log "brain-v42-automation.service remains dormant; follow deploy/systemd/README.md for cutover"

# Linger check — warn if disabled (timer won't run without an active session).
effective_uid="$(id -u)"
if ! command -v loginctl >/dev/null; then
  echo "WARN: loginctl is unavailable; unable to determine linger status for UID $effective_uid." >&2
elif loginctl_status="$(loginctl show-user -- "$effective_uid" 2>/dev/null)"; then
  linger=""
  while IFS='=' read -r key value; do
    if [[ "$key" == "Linger" ]]; then
      linger="$value"
      break
    fi
  done <<< "$loginctl_status"
  if [[ "$linger" != "yes" ]]; then
    echo "WARN: loginctl shows Linger=$linger for UID $effective_uid." >&2
    echo "      Run: sudo loginctl enable-linger $effective_uid" >&2
    echo "      Without linger, the timer only runs when you are logged in." >&2
  fi
else
  echo "WARN: unable to determine linger status for UID $effective_uid." >&2
fi

# --- Status summary ---
log "timers installed"
for unit in "${UNITS[@]}"; do
  systemctl --user list-timers "$unit.timer" --no-pager || true
done
