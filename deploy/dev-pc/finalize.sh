#!/usr/bin/env bash
#
# Finalize the DD->native embedding cutover, INSIDE Ubuntu-24.04.
#
# Run this ONLY after the headless native stack has been proven stable for N
# days (see deploy/dev-pc/README.md). It is the LAST step of the migration and
# it makes rollback-to-Docker-Desktop more expensive:
#
#   - `docker rmi` the old Docker Desktop images (now redundant — native is
#     proven and the running containers reference the native-loaded images).
#   - remove the rollback tarball (/var/backups/brain-embedding-images.tar).
#
# After finalize, rollback.sh can no longer `docker load` the tarball: rolling
# back to DD requires REBUILDING the images (rollback.sh handles this, but it is
# slower and needs the build context).
#
# This script is idempotent: re-running after a successful finalize is a no-op.
#
# Usage:
#   ./deploy/dev-pc/finalize.sh         # interactive confirmation
#   ./deploy/dev-pc/finalize.sh --yes   # non-interactive

set -euo pipefail

# Decision A — run-as-root: re-exec under sudo (preserving env + args) if not
# already root.
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then exec sudo -E -- "$0" "$@"; fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"

# --- Shared conventions (must match cutover.sh / rollback.sh) ---
SUPERVISOR_CONTAINER="brain_embedding_supervisor"
QODO_CONTAINER="brain_embedding_qodo"
IMAGE_TARBALL="/var/backups/brain-embedding-images.tar"

ASSUME_YES=false
for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES=true ;;
    -h|--help)
      sed -n '2,21p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

log()  { echo "[finalize] $*"; }
warn() { echo "[finalize] WARN: $*" >&2; }
die()  { echo "[finalize] ABORT: $*" >&2; exit 1; }

confirm() {
  local prompt="$1" reply
  if $ASSUME_YES; then
    log "--yes: auto-confirming: $prompt"
    return 0
  fi
  read -r -p "[finalize] $prompt [type 'yes' to continue] " reply
  [[ "$reply" == "yes" ]] || die "not confirmed (got '$reply')"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

require_cmd docker
[[ -f "$COMPOSE_FILE" ]] || die "compose file not found: $COMPOSE_FILE"

# --- Safety preconditions: finalize ONLY when native owns a healthy stack ---
log "verifying native docker owns a running supervisor before finalizing"

engine_os="$(docker info --format '{{.OperatingSystem}}' 2>/dev/null || true)"
if [[ "$engine_os" == "Docker Desktop" ]]; then
  die "docker CLI is talking to Docker Desktop (OperatingSystem='$engine_os'). \
Finalize must run against the NATIVE engine that now owns the stack. Refusing — \
removing images now would break the running DD stack and the rollback artifact."
fi
[[ -n "$engine_os" ]] || die "docker is unreachable — refusing to finalize blind"
log "native engine OperatingSystem == '$engine_os' (!= Docker Desktop) — OK"

sup_status="$(docker ps --filter "name=$SUPERVISOR_CONTAINER" --format '{{.Status}}' 2>/dev/null || true)"
if [[ -z "$sup_status" ]]; then
  die "supervisor '$SUPERVISOR_CONTAINER' is NOT running under the native engine. \
Do not finalize until the native headless stack is proven stable. (Run the \
validation gate first.)"
fi
log "native supervisor running: $sup_status"

cat <<EOF

  This will PERMANENTLY:
    - docker rmi the Docker Desktop embedding images
    - remove the rollback tarball: $IMAGE_TARBALL

  After this, rolling back to Docker Desktop requires a REBUILD (slower).
  Only proceed if the native headless stack has been stable for N days.

EOF
confirm "Finalize the cutover (make DD rollback require a rebuild)?"

# Resolve the image names from the compose file (same set cutover.sh saved).
mapfile -t COMPOSE_IMAGES < <(
  docker compose -f "$COMPOSE_FILE" config --images 2>/dev/null | sort -u
)
if [[ "${#COMPOSE_IMAGES[@]}" -eq 0 ]]; then
  warn "could not resolve image names from compose config --images"
  warn "continuing with tarball removal only"
fi

# --- Remove the (now redundant) images. Idempotent: skip already-absent. ---
# NOTE: the native running containers reference images by ID; rmi-by-tag here
# untags/removes the stored layers only if no container/other tag holds them.
# `docker rmi` will refuse (and we tolerate) if an image is still in use.
for img in "${COMPOSE_IMAGES[@]:-}"; do
  [[ -n "$img" ]] || continue
  if docker image inspect "$img" >/dev/null 2>&1; then
    log "docker rmi $img"
    if ! docker rmi "$img" >/dev/null 2>&1; then
      warn "could not remove image '$img' (likely still referenced by a running \
container — that is fine; the tag will clear when the container is recreated)."
    fi
  else
    log "image already absent: $img (idempotent skip)"
  fi
done

# --- Remove the rollback tarball. Idempotent: skip if already gone. ---
if [[ -f "$IMAGE_TARBALL" ]]; then
  log "removing rollback tarball: $IMAGE_TARBALL"
  if ! rm -f "$IMAGE_TARBALL"; then
    die "failed to remove $IMAGE_TARBALL (check permissions / run via sudo)"
  fi
else
  log "tarball already absent: $IMAGE_TARBALL (idempotent skip)"
fi

log "FINALIZE COMPLETE."
log "Docker Desktop rollback now requires a REBUILD — rollback.sh will rebuild"
log "the images from the compose build context if you ever need to revert."
