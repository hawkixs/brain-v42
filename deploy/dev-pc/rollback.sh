#!/usr/bin/env bash
#
# Roll the dev-pc embedding stack BACK to Docker Desktop, INSIDE Ubuntu-24.04.
#
# Self-contained — does NOT assume the saved images still exist. Three cases:
#   * cutover ran, finalize did NOT  -> DD images are intact; just re-enable DD.
#   * cutover + finalize ran         -> DD images gone, but the tarball may also
#                                       be gone; if no images and no tarball, we
#                                       REBUILD under DD.
#   * tarball present, images gone    -> `docker load` the tarball back into DD.
#
# Order:
#   1. Stop the NATIVE stack (rm -f the two containers, stop native docker) so
#      the native engine releases :8003 / the socket.
#   2. (MANUAL, gated) operator RE-ENABLES Docker Desktop WSL integration for
#      Ubuntu-24.04, so the docker CLI talks to Docker Desktop again.
#   3. Ensure DD has the images: docker load the tarball if present; else rebuild
#      under DD if images are missing.
#   4. Bring the stack up under DD (supervisor up, qodo created/not-started).
#   5. Verify exactly one :8003 listener AND the engine is Docker Desktop again.
#
# Idempotent: re-running detects already-done state and proceeds safely.
#
# Usage:
#   ./deploy/dev-pc/rollback.sh         # interactive (gates on confirmation)
#   ./deploy/dev-pc/rollback.sh --yes   # non-interactive

set -euo pipefail

# Decision A — run-as-root: re-exec under sudo (preserving env + args) if not
# already root. Inner `sudo` calls below then become no-ops.
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then exec sudo -E -- "$0" "$@"; fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"

# --- Shared conventions (must match cutover.sh / finalize.sh) ---
SUPERVISOR_CONTAINER="brain_embedding_supervisor"
QODO_CONTAINER="brain_embedding_qodo"
IMAGE_TARBALL="/var/backups/brain-embedding-images.tar"
PORT="8003"

ASSUME_YES=false
for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES=true ;;
    -h|--help)
      sed -n '2,26p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

log()  { echo "[rollback] $*"; }
warn() { echo "[rollback] WARN: $*" >&2; }
die()  { echo "[rollback] ABORT: $*" >&2; exit 1; }

confirm() {
  local prompt="$1" reply
  if $ASSUME_YES; then
    log "--yes: auto-confirming: $prompt"
    return 0
  fi
  read -r -p "[rollback] $prompt [type 'yes' to continue] " reply
  [[ "$reply" == "yes" ]] || die "not confirmed (got '$reply')"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

docker_engine_os() {
  docker info --format '{{.OperatingSystem}}' 2>/dev/null || true
}

require_cmd docker
require_cmd ss
require_cmd systemctl
[[ -f "$COMPOSE_FILE" ]] || die "compose file not found: $COMPOSE_FILE"

log "repo root:    $REPO_ROOT"
log "compose file: $COMPOSE_FILE"
log "tarball:      $IMAGE_TARBALL"
warn "ROLLING BACK to Docker Desktop — this reverts the headless cutover."
confirm "Proceed with rollback to Docker Desktop?"

# =====================================================================
# STEP 1 — Stop the NATIVE stack and release the socket / :8003
# =====================================================================
log "STEP 1 — stop the native stack"

native_state="$(systemctl is-active docker 2>/dev/null || true)"
if [[ "$native_state" == "active" ]]; then
  engine_now="$(docker_engine_os)"
  if [[ "$engine_now" != "Docker Desktop" ]]; then
    # We're talking to native docker — tear the containers down cleanly first.
    for c in "$SUPERVISOR_CONTAINER" "$QODO_CONTAINER"; do
      if docker container inspect "$c" >/dev/null 2>&1; then
        log "docker rm -f $c (native)"
        docker rm -f "$c" >/dev/null || warn "could not remove $c (continuing)"
      else
        log "$c not present under native — skipping"
      fi
    done
  else
    warn "native docker.service is active but CLI already reports Docker Desktop; skipping native container teardown"
  fi
  log "systemctl disable --now docker docker.socket (release the socket for DD)"
  # Disable docker.socket too — otherwise socket-activation resurrects native
  # dockerd and re-grabs /var/run/docker.sock away from Docker Desktop.
  sudo systemctl disable --now docker docker.socket || warn "could not disable native docker (continuing)"
else
  log "native docker.service is '$native_state' (not active) — nothing to stop"
fi

# =====================================================================
# STEP 2 — RE-ENABLE Docker Desktop WSL integration (MANUAL, gated)
# =====================================================================
log "STEP 2 — re-enable Docker Desktop WSL integration for Ubuntu-24.04 (MANUAL)"
cat <<'EOF'

  ┌─────────────────────────────────────────────────────────────────────┐
  │  MANUAL ACTION REQUIRED (on the Windows side, on the dev-pc):        │
  │                                                                       │
  │   1. Start Docker Desktop (it may have been set to NOT auto-start).  │
  │   2. Docker Desktop -> Settings -> Resources -> WSL integration      │
  │        -> turn ON  "Ubuntu-24.04"                                     │
  │        -> Apply & restart                                             │
  │   3. (optional) Settings -> General -> re-check                      │
  │        "Start Docker Desktop when you sign in" if you want DD back   │
  │        as the boot owner.                                             │
  │                                                                       │
  │  This re-points /var/run/docker.sock at Docker Desktop so the docker  │
  │  CLI below talks to DD again.                                         │
  └─────────────────────────────────────────────────────────────────────┘

EOF
confirm "Confirm Docker Desktop WSL integration for Ubuntu-24.04 is now ENABLED?"

log "asserting the docker CLI now talks to Docker Desktop"
engine_dd="$(docker_engine_os)"
if [[ "$engine_dd" != "Docker Desktop" ]]; then
  die "docker CLI is NOT talking to Docker Desktop (OperatingSystem='${engine_dd:-unreachable}'). \
Re-enable WSL integration and ensure DD is started, then re-run."
fi
log "docker engine OperatingSystem == 'Docker Desktop' — OK"

# =====================================================================
# STEP 3 — Ensure DD has the images (load tarball, else rebuild)
# =====================================================================
log "STEP 3 — ensure Docker Desktop has the embedding images"

mapfile -t COMPOSE_IMAGES < <(
  docker compose -f "$COMPOSE_FILE" config --images 2>/dev/null | sort -u
)
[[ "${#COMPOSE_IMAGES[@]}" -gt 0 ]] || die "could not resolve image names from compose config --images"
log "compose images: ${COMPOSE_IMAGES[*]}"

images_present=true
for img in "${COMPOSE_IMAGES[@]}"; do
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    images_present=false
  fi
done

if $images_present; then
  log "all images already present under DD (cutover did not finalize) — no load/rebuild needed"
elif [[ -f "$IMAGE_TARBALL" ]]; then
  log "images missing but tarball present — docker load < $IMAGE_TARBALL"
  docker load -i "$IMAGE_TARBALL" >/dev/null \
    || die "docker load of rollback tarball FAILED"
  for img in "${COMPOSE_IMAGES[@]}"; do
    docker image inspect "$img" >/dev/null 2>&1 \
      || die "image still missing after docker load: $img"
  done
  log "images restored from tarball"
else
  warn "images missing AND no tarball ($IMAGE_TARBALL) — finalize.sh already ran."
  warn "rolling back now requires a REBUILD under Docker Desktop."
  confirm "Rebuild the images under Docker Desktop now (docker compose build)?"
  log "docker compose build (under Docker Desktop)"
  docker compose -f "$COMPOSE_FILE" build
  for img in "${COMPOSE_IMAGES[@]}"; do
    docker image inspect "$img" >/dev/null 2>&1 \
      || die "image still missing after rebuild: $img"
  done
  log "images rebuilt under Docker Desktop"
fi

# =====================================================================
# STEP 4 — Bring the stack up under Docker Desktop
# =====================================================================
log "STEP 4 — bring the stack up under Docker Desktop"

# Remove any stale containers DD may still hold before recreating, so up is clean.
for c in "$SUPERVISOR_CONTAINER" "$QODO_CONTAINER"; do
  if docker container inspect "$c" >/dev/null 2>&1; then
    log "removing stale DD container before recreate: $c"
    docker rm -f "$c" >/dev/null || warn "could not remove stale $c (continuing)"
  fi
done

log "docker compose up -d --no-build --no-start embedding-qodo (qodo created, not started)"
docker compose -f "$COMPOSE_FILE" up -d --no-build --no-start embedding-qodo

log "docker compose up -d --no-build embedding-supervisor (always-up, :$PORT)"
docker compose -f "$COMPOSE_FILE" up -d --no-build embedding-supervisor

# =====================================================================
# STEP 5 — Verify :8003 served by Docker Desktop again
# =====================================================================
log "STEP 5 — verify :$PORT is served by Docker Desktop again"

# Wait briefly for DD to publish the port.
listen_line=""
for _ in $(seq 1 30); do
  listen_line="$(ss -ltnpH "sport = :$PORT" 2>/dev/null || true)"
  [[ -n "$listen_line" ]] && break
  sleep 1
done
[[ -n "$listen_line" ]] || die "no listener on :$PORT after DD bring-up — rollback did not restore the service"

mapfile -t LISTEN_PIDS < <(
  printf '%s\n' "$listen_line" \
    | grep -oE 'pid=[0-9]+' | sed 's/pid=//' | sort -un
)
if [[ "${#LISTEN_PIDS[@]}" -ne 1 ]]; then
  warn "expected exactly one :$PORT listener, found ${#LISTEN_PIDS[@]}: ${LISTEN_PIDS[*]:-none}"
  warn "ss line: $listen_line"
  die "single-owner invariant not restored on rollback"
fi
log "single listener on :$PORT -> pid ${LISTEN_PIDS[0]}"

# Re-assert the engine is Docker Desktop and the supervisor container is up.
engine_final="$(docker_engine_os)"
[[ "$engine_final" == "Docker Desktop" ]] \
  || die "engine is '$engine_final' (expected 'Docker Desktop') after rollback"

sup_status="$(docker ps --filter "name=$SUPERVISOR_CONTAINER" --format '{{.Status}}' 2>/dev/null || true)"
[[ -n "$sup_status" ]] || die "supervisor container not running under Docker Desktop after rollback"

log "ROLLBACK COMPLETE — Docker Desktop owns the embedding stack on :$PORT again."
log "  engine: $engine_final"
log "  supervisor: $sup_status"
log "Remember to revert the Windows side too if you migrated it: disable the"
log "'brain-embedding-boot' Task Scheduler task (it expects native WSL docker),"
log "and re-enable DD auto-start if you want DD to own the boot path."
