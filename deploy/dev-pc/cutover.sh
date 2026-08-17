#!/usr/bin/env bash
#
# Cutover the dev-pc embedding stack from Docker Desktop (DD) to native
# docker-ce, INSIDE Ubuntu-24.04. Non-destructive and reversible: DD images
# are saved to a tarball and RETAINED (never deleted here — see finalize.sh).
#
# Canonical cutover order (load-bearing — enforced by the asserts below):
#   0. Preconditions: native docker INACTIVE + DD owns the socket.
#   1. SAVE: build under DD if images missing; docker save -> tarball; PROVE
#      the tarball is restorable (tar tf lists both manifests + throwaway load).
#   2. STOP: docker rm -f the two CONTAINERS only (images RETAINED); assert the
#      DD engine now shows zero brain_embedding_*; persist a proof log line.
#   3. INTEGRATION OFF (manual, gated): operator disables DD -> Ubuntu-24.04 WSL
#      integration; wait for confirm; assert the old socket/context is released.
#   4. NATIVE UP: systemctl enable --now docker; assert engine != Docker Desktop;
#      docker load < tarball; docker compose up -d --no-build supervisor (+ qodo
#      --no-start).
#   5. INVARIANT: exactly one OWNER ENGINE — collect ALL :8003 listener PIDs
#      (IPv6 dual-stack yields one docker-proxy per family) and require EVERY
#      distinct PID to belong to the native dockerd tree; log it. Then disable
#      DD auto-start (documented; GUI-only).
#
# Usage:
#   ./deploy/dev-pc/cutover.sh           # interactive (gates on confirmation)
#   ./deploy/dev-pc/cutover.sh --yes     # non-interactive (auto-confirm gates)
#
# Idempotent: safe to re-run. Each step detects already-done state and skips.

set -euo pipefail

# Decision A — run-as-root: re-exec under sudo (preserving env + args) if not
# already root. Inner `sudo` calls below then become no-ops.
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then exec sudo -E -- "$0" "$@"; fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"

# --- Shared conventions (other migration scripts depend on these) ---
SUPERVISOR_CONTAINER="brain_embedding_supervisor"
QODO_CONTAINER="brain_embedding_qodo"
IMAGE_TARBALL="/var/backups/brain-embedding-images.tar"
PORT="8003"
PROOF_LOG="/var/log/brain-embedding-cutover.log"

ASSUME_YES=false
for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES=true ;;
    -h|--help)
      sed -n '2,27p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

log()  { echo "[cutover] $*"; }
warn() { echo "[cutover] WARN: $*" >&2; }
die()  { echo "[cutover] ABORT: $*" >&2; exit 1; }

# Append a durable, timestamped proof line. Best-effort on the log file:
# falls back to stdout-only if /var/log is not writable.
proof() {
  local line
  line="$(date -u +%FT%TZ) $*"
  echo "[cutover][proof] $*"
  if { : >>"$PROOF_LOG"; } 2>/dev/null; then
    echo "$line" >>"$PROOF_LOG"
  else
    warn "cannot write $PROOF_LOG (non-fatal); proof emitted to stdout only"
  fi
}

confirm() {
  # $1 = prompt. Honours --yes. Returns 0 on confirm, dies otherwise.
  local prompt="$1" reply
  if $ASSUME_YES; then
    log "--yes: auto-confirming: $prompt"
    return 0
  fi
  read -r -p "[cutover] $prompt [type 'yes' to continue] " reply
  [[ "$reply" == "yes" ]] || die "not confirmed (got '$reply')"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

# Read the OperatingSystem field from `docker info` for whatever engine the
# docker CLI is currently talking to. Empty string if docker is unreachable.
docker_engine_os() {
  docker info --format '{{.OperatingSystem}}' 2>/dev/null || true
}

# --- Preflight: tools we rely on ---
require_cmd docker
require_cmd python3
require_cmd ss
require_cmd systemctl
require_cmd tar

[[ -f "$COMPOSE_FILE" ]] || die "compose file not found: $COMPOSE_FILE"

log "repo root:    $REPO_ROOT"
log "compose file: $COMPOSE_FILE"
log "tarball:      $IMAGE_TARBALL"
proof "cutover START (yes=$ASSUME_YES)"

# =====================================================================
# STEP 0 — Preconditions
# =====================================================================
log "STEP 0 — preconditions (native docker INACTIVE + DD owns the socket)"

native_state="$(systemctl is-active docker 2>/dev/null || true)"
if [[ "$native_state" == "active" ]]; then
  die "native docker.service is ACTIVE ('$native_state'). Two engines must not \
coexist on /var/run/docker.sock. Run 'sudo systemctl stop docker' and re-run \
(see operator step 1: --verify-gpu transiently starts dockerd; it MUST be stopped)."
fi
log "native docker.service is '$native_state' (not active) — OK"

# docker.socket must ALSO be inactive — socket-activation would resurrect native
# dockerd onto /var/run/docker.sock the moment a client connects.
socket_state="$(systemctl is-active docker.socket 2>/dev/null || true)"
if [[ "$socket_state" == "active" ]]; then
  die "native docker.socket is ACTIVE ('$socket_state'). Socket-activation would \
resurrect native dockerd onto /var/run/docker.sock. Run 'sudo systemctl stop docker \
docker.socket' and re-run."
fi
log "native docker.socket is '$socket_state' (not active) — OK"

engine_os="$(docker_engine_os)"
if [[ "$engine_os" != "Docker Desktop" ]]; then
  die "docker CLI is NOT talking to Docker Desktop (OperatingSystem='$engine_os'). \
Expected 'Docker Desktop'. Refusing to proceed — cannot SAVE the DD images we \
are migrating away from."
fi
log "docker engine OperatingSystem == 'Docker Desktop' — OK"
proof "STEP 0 OK (native=$native_state, socket=$socket_state, engine='$engine_os')"

# =====================================================================
# STEP 1 — SAVE (non-destructive): build if missing, save, PROVE restorable
# =====================================================================
log "STEP 1 — SAVE DD images -> tarball (and prove restorable)"

# Resolve the image IDs the compose file refers to. `docker compose config
# --images` prints the image names compose would use for each service.
mapfile -t COMPOSE_IMAGES < <(
  docker compose -f "$COMPOSE_FILE" config --images 2>/dev/null | sort -u
)
if [[ "${#COMPOSE_IMAGES[@]}" -eq 0 ]]; then
  die "could not resolve image names from compose config --images"
fi
log "compose images: ${COMPOSE_IMAGES[*]}"

# Build only if any image is missing from the DD engine.
need_build=false
for img in "${COMPOSE_IMAGES[@]}"; do
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    log "image missing under DD: $img"
    need_build=true
  fi
done
if $need_build; then
  log "building images under Docker Desktop (docker compose build)"
  docker compose -f "$COMPOSE_FILE" build
else
  log "both images already present under DD — skipping build (idempotent)"
fi

# Re-assert all images now exist before saving.
for img in "${COMPOSE_IMAGES[@]}"; do
  docker image inspect "$img" >/dev/null 2>&1 \
    || die "image still missing after build: $img"
done

# Write the tarball. /var/backups must exist + be writable (likely needs sudo
# at the dir level on first run — surface that clearly rather than failing deep).
tarball_dir="$(dirname "$IMAGE_TARBALL")"
if [[ ! -d "$tarball_dir" ]]; then
  die "tarball dir does not exist: $tarball_dir (create it: sudo install -d -m0755 $tarball_dir)"
fi
if [[ ! -w "$tarball_dir" ]]; then
  die "tarball dir not writable: $tarball_dir (fix perms or run via sudo)"
fi

# Idempotent: if a tarball exists and already proves restorable for these
# images, keep it; otherwise (re)write it.
save_needed=true
if [[ -f "$IMAGE_TARBALL" ]]; then
  log "existing tarball found — checking it already contains the images"
  if tar tf "$IMAGE_TARBALL" >/dev/null 2>&1; then
    # manifest.json lists the repo tags inside the archive
    if tar xfO "$IMAGE_TARBALL" manifest.json 2>/dev/null \
        | python3 -c '
import json,sys
data=json.load(sys.stdin)
tags=set()
for e in data:
    for t in (e.get("RepoTags") or []):
        tags.add(t)
want=set(sys.argv[1:])
missing=want-tags
if missing:
    sys.exit(1)
' "${COMPOSE_IMAGES[@]}"; then
      log "existing tarball already contains all compose images — skipping save (idempotent)"
      save_needed=false
    else
      log "existing tarball is stale (missing some images) — rewriting"
    fi
  else
    warn "existing tarball is unreadable — rewriting"
  fi
fi

if $save_needed; then
  log "docker save -> $IMAGE_TARBALL"
  docker save -o "$IMAGE_TARBALL" "${COMPOSE_IMAGES[@]}"
fi

# PROVE restorable (judge N4) — NEVER a size-only check.
#   (a) tar tf lists a manifest for the archive.
#   (b) the manifest names both compose images (RepoTags).
#   (c) a throwaway `docker load` actually parses + loads the archive.
log "proving tarball is restorable (tar tf + manifest + throwaway docker load)"

tar tf "$IMAGE_TARBALL" | grep -q 'manifest.json' \
  || die "tarball has no manifest.json — not a valid 'docker save' archive"

tar xfO "$IMAGE_TARBALL" manifest.json \
  | python3 -c '
import json,sys
data=json.load(sys.stdin)
tags=set()
for e in data:
    for t in (e.get("RepoTags") or []):
        tags.add(t)
want=set(sys.argv[1:])
missing=want-tags
if missing:
    sys.stderr.write("tarball manifest missing images: %s\n" % ", ".join(sorted(missing)))
    sys.exit(1)
sys.stderr.write("tarball manifest lists all images: %s\n" % ", ".join(sorted(want)))
' "${COMPOSE_IMAGES[@]}" >&2 \
  || die "tarball manifest does not list both supervisor+qodo images"

# Throwaway load: `docker load` is idempotent (it re-imports the same layers/
# tags that already exist under DD, a no-op for content), and it parses the
# WHOLE archive — so a successful load proves the bytes are restorable.
log "throwaway 'docker load' to confirm the archive parses + loads"
docker load -i "$IMAGE_TARBALL" >/dev/null \
  || die "throwaway 'docker load' FAILED — tarball is not restorable; aborting before any rm"

proof "STEP 1 OK (saved + proved restorable: ${COMPOSE_IMAGES[*]} -> $IMAGE_TARBALL)"

# =====================================================================
# STEP 2 — STOP: remove the two CONTAINERS only (images RETAINED)
# =====================================================================
log "STEP 2 — remove DD CONTAINERS only (images retained)"

for c in "$SUPERVISOR_CONTAINER" "$QODO_CONTAINER"; do
  if docker container inspect "$c" >/dev/null 2>&1; then
    log "docker rm -f $c"
    docker rm -f "$c" >/dev/null
  else
    log "$c not present (already removed) — skipping (idempotent)"
  fi
done

# Falsifiable R5 check, done while DD is STILL inspectable.
remaining="$(docker ps -a --filter 'name=brain_embedding_' --format '{{.Names}}' 2>/dev/null || true)"
if [[ -n "$remaining" ]]; then
  die "DD engine still shows brain_embedding_* containers after rm: $remaining"
fi

# Re-assert images are still present (we removed containers, NOT images).
for img in "${COMPOSE_IMAGES[@]}"; do
  docker image inspect "$img" >/dev/null 2>&1 \
    || die "image disappeared after container rm (must be RETAINED): $img"
done

proof "STEP 2 OK (zero brain_embedding_* containers under DD; images retained)"

# =====================================================================
# STEP 3 — INTEGRATION OFF (manual, gated)
# =====================================================================
log "STEP 3 — disable Docker Desktop WSL integration for Ubuntu-24.04 (MANUAL)"
cat <<'EOF'

  ┌─────────────────────────────────────────────────────────────────────┐
  │  MANUAL ACTION REQUIRED (on the Windows side, on the dev-pc):        │
  │                                                                       │
  │    Docker Desktop  ->  Settings  ->  Resources  ->  WSL integration  │
  │      -> turn OFF  "Ubuntu-24.04"                                      │
  │      -> Apply & restart                                               │
  │                                                                       │
  │  This frees /var/run/docker.sock so native dockerd can own it. Brief  │
  │  embedding downtime here is acceptable (brain-v42 falls back to       │
  │  FTS-only search until native is up).                                 │
  └─────────────────────────────────────────────────────────────────────┘

EOF
confirm "Confirm DD WSL integration for Ubuntu-24.04 is now DISABLED?"

# Assert the old socket/context is released: with native docker still inactive
# and DD integration off, the docker CLI should NO LONGER reach Docker Desktop.
log "asserting the old DD socket/context is released"
engine_os_now="$(docker_engine_os)"
if [[ "$engine_os_now" == "Docker Desktop" ]]; then
  die "docker CLI STILL talks to Docker Desktop (OperatingSystem='$engine_os_now'). \
WSL integration is not actually off, or DD has not restarted. Re-disable it and re-run."
fi
log "docker CLI no longer reaches Docker Desktop (engine='${engine_os_now:-unreachable}') — socket released"
proof "STEP 3 OK (DD WSL integration off; socket released; engine='${engine_os_now:-unreachable}')"

# =====================================================================
# STEP 4 — NATIVE UP
# =====================================================================
log "STEP 4 — bring up native docker-ce and the stack"

log "systemctl enable --now docker"
sudo systemctl enable --now docker

# Wait briefly for the socket to be ready.
for _ in $(seq 1 30); do
  if docker info >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

engine_native="$(docker_engine_os)"
[[ -n "$engine_native" ]] || die "native docker is not responding after enable --now"
if [[ "$engine_native" == "Docker Desktop" ]]; then
  die "engine is STILL 'Docker Desktop' after starting native docker — the two \
engines are fighting over the socket. Abort. (DD integration must be off.)"
fi
log "native engine OperatingSystem == '$engine_native' (!= Docker Desktop) — OK"

# Restore the images into the NATIVE engine.
log "docker load < $IMAGE_TARBALL (into native engine)"
docker load -i "$IMAGE_TARBALL" >/dev/null \
  || die "docker load into native engine FAILED — cannot bring up the stack"

# Confirm both images are present under native before up.
for img in "${COMPOSE_IMAGES[@]}"; do
  docker image inspect "$img" >/dev/null 2>&1 \
    || die "image not present under native engine after load: $img"
done

# Create qodo but do NOT start it (supervisor controls it lazily), then bring
# the supervisor up. --no-build: we loaded prebuilt images, never rebuild here.
log "docker compose up -d --no-build --no-start $QODO_CONTAINER (qodo created, not started)"
docker compose -f "$COMPOSE_FILE" up -d --no-build --no-start embedding-qodo

log "docker compose up -d --no-build embedding-supervisor (always-up, :$PORT)"
docker compose -f "$COMPOSE_FILE" up -d --no-build embedding-supervisor

proof "STEP 4 OK (native engine='$engine_native'; images loaded; supervisor up; qodo created/not-started)"

# =====================================================================
# STEP 5 — INVARIANT: every :8003 listener owned by ONE native dockerd engine
# =====================================================================
log "STEP 5 — host-level single-owner-engine invariant on :$PORT"

# Collect the PIDs of every listener on :PORT (both address families).
# ss output line carries users:(("name",pid=NNN,fd=MM)). Extract all pids.
listen_line="$(ss -ltnpH "sport = :$PORT" 2>/dev/null || true)"
if [[ -z "$listen_line" ]]; then
  die "no listener on :$PORT after bring-up — supervisor failed to publish the port"
fi

mapfile -t LISTEN_PIDS < <(
  printf '%s\n' "$listen_line" \
    | grep -oE 'pid=[0-9]+' | sed 's/pid=//' | sort -un
)
if [[ "${#LISTEN_PIDS[@]}" -eq 0 ]]; then
  # ss could not show pids (needs privilege). Surface clearly.
  warn "ss did not expose listener PIDs on :$PORT (need root for users:). Re-run with sudo to assert ownership."
  warn "ss line was: $listen_line"
  die "cannot prove :$PORT ownership without listener PIDs"
fi

# Invariant: exactly ONE OWNER ENGINE, not exactly one PID. On an IPv6
# dual-stack host, docker spawns TWO docker-proxy PIDs for :$PORT (one per
# address family) — both children of the SAME native dockerd. So we require at
# least one listener PID and PASS iff EVERY distinct PID resolves (via the
# parent-walk below) to the native dockerd tree; we FAIL only if some PID does
# NOT belong to dockerd (a real foreign owner).
log "listeners on :$PORT -> pid(s) ${LISTEN_PIDS[*]} (count ${#LISTEN_PIDS[@]})"

# Resolve a PID's ownership chain up to dockerd. The published port is held by a
# docker-proxy child of dockerd; walk parents until we hit dockerd, or match the
# process name directly.
pid_belongs_to_dockerd() {
  local pid="$1" hops=0 comm ppid
  while [[ -n "$pid" && "$pid" != "0" && "$pid" != "1" && "$hops" -lt 12 ]]; do
    comm="$(ps -o comm= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    case "$comm" in
      dockerd|docker-proxy|docker-init|containerd|containerd-shim*) return 0 ;;
    esac
    ppid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    [[ -n "$ppid" && "$ppid" != "$pid" ]] || break
    pid="$ppid"
    hops=$((hops + 1))
  done
  return 1
}

for listen_pid in "${LISTEN_PIDS[@]}"; do
  if pid_belongs_to_dockerd "$listen_pid"; then
    owner_comm="$(ps -o comm= -p "$listen_pid" 2>/dev/null | tr -d ' ' || echo '?')"
    log "listener PID $listen_pid ('$owner_comm') resolves to the native dockerd process tree — OK"
  else
    chain="$(ps -o pid=,ppid=,comm= -p "$listen_pid" 2>/dev/null || true)"
    die "listener PID $listen_pid on :$PORT does NOT belong to native dockerd. ps: [$chain]. \
A non-docker process (or DD) owns the port — single-owner invariant VIOLATED."
  fi
done

# And confirm the native engine itself reports the supervisor container running.
sup_status="$(docker ps --filter "name=$SUPERVISOR_CONTAINER" --format '{{.Status}}' 2>/dev/null || true)"
[[ -n "$sup_status" ]] || die "supervisor container not running under native engine"
log "native supervisor container: $sup_status"

proof "STEP 5 OK (:$PORT listener pid(s)=${LISTEN_PIDS[*]} all owned by native dockerd; supervisor '$sup_status')"

# =====================================================================
# Disable Docker Desktop auto-start (GUI-only — documented)
# =====================================================================
cat <<'EOF'

  ┌─────────────────────────────────────────────────────────────────────┐
  │  MANUAL ACTION (on the Windows side): disable DD auto-start.         │
  │                                                                       │
  │    Docker Desktop  ->  Settings  ->  General                         │
  │      -> UNCHECK  "Start Docker Desktop when you sign in"             │
  │      -> Apply & restart                                               │
  │                                                                       │
  │  There is no documented Docker Desktop CLI flag for this toggle;      │
  │  it is a GUI-only setting. Leaving it on would let DD re-grab the     │
  │  stack at the next interactive login (duplicate-owner risk).          │
  └─────────────────────────────────────────────────────────────────────┘

EOF
confirm "Confirm 'Start Docker Desktop when you sign in' is now UNCHECKED?"
proof "DD auto-start disabled (operator-confirmed)"

log "CUTOVER COMPLETE — native docker-ce owns the embedding stack on :$PORT."
log "Next: 'wsl --shutdown' (note: bounces DD too — harmless), then reboot and"
log "do NOT log in; validate from pc-serveur with validate-headless.sh."
log "DO NOT run finalize.sh until the headless path is proven stable for N days."
proof "cutover DONE"
