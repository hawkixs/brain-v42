#!/usr/bin/env bash
#
# setup-docker-ce.sh — Task 1 of the dev-pc headless embedding re-platform.
#
# Runs INSIDE the Ubuntu-24.04 WSL2 distro on the dev-pc (192.168.1.11).
# Installs native docker-ce + nvidia-container-toolkit (with the WSL2-required
# `no-cgroups = true`), pre-pulls the supervisor's exact GPU-probe image, and
# installs+enables the keepalive systemd unit that holds the WSL VM alive.
#
# IMPORTANT — it does NOT leave docker running. Pre-cutover, Docker Desktop
# still owns /var/run/docker.sock; a persistently-started native dockerd here
# would make two engines fight over the socket. The ONLY native starts are
# TRANSIENT: prepull_probe_image and `--verify-gpu` each start dockerd, do their
# work, then `systemctl stop docker docker.socket` + assert it inactive again.
# So the cutover step-0 precondition (native docker INACTIVE) still holds after
# this script returns. The lasting native start is cutover.sh (step 4).
#
# Idempotent + re-run safe: keyring overwritten via `install -m0644`, apt repo
# rewritten via `tee`, daemon.json asserted valid JSON, keepalive unit re-enabled.
#
# Usage:
#   ./setup-docker-ce.sh               # install engine + toolkit + keepalive (no start)
#   ./setup-docker-ce.sh --verify-gpu  # transiently start docker, prove GPU via CLI
#                                       # AND docker-py API, then stop docker again
#   ./setup-docker-ce.sh -h|--help
#
# Run as root (sudo). `bash -n` clean; written shellcheck-clean.

set -euo pipefail

# --- Constants (shared conventions — other cutover scripts depend on these) ---
PROBE_IMAGE="nvidia/cuda:12.4.1-base-ubuntu22.04@sha256:0f6bfcbf267e65123bcc2287e2153dedfc0f24772fb5ce84afe16ac4b2fada95"   # EXACT supervisor GPU-probe reference
KEEPALIVE_UNIT="brain-embedding-keepalive.service"
NVIDIA_RUNTIME_CONFIG="/etc/nvidia-container-runtime/config.toml"
DOCKER_DAEMON_JSON="/etc/docker/daemon.json"
DOCKER_KEYRING="/etc/apt/keyrings/docker.asc"
DOCKER_APT_LIST="/etc/apt/sources.list.d/docker.list"
DOCKER_GPG_URL="https://download.docker.com/linux/ubuntu/gpg"
DOCKER_APT_BASE="https://download.docker.com/linux/ubuntu"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEEPALIVE_SRC="$SCRIPT_DIR/systemd/$KEEPALIVE_UNIT"

log()  { echo "[setup-docker-ce] $*"; }
warn() { echo "[setup-docker-ce] WARN: $*" >&2; }
die()  { echo "[setup-docker-ce] ERROR: $*" >&2; exit 1; }

# --- Arg parse ---
VERIFY_GPU=false
for arg in "$@"; do
  case "$arg" in
    --verify-gpu) VERIFY_GPU=true ;;
    -h|--help)
      sed -n '2,27p' "$0"
      exit 0
      ;;
    *)
      die "unknown arg: $arg (try --help)"
      ;;
  esac
done

require_root() {
  [[ "$(id -u)" -eq 0 ]] || die "must run as root (use sudo)"
}

# A native docker-ce engine is one whose `docker info` OperatingSystem is NOT
# "Docker Desktop". We only probe when the socket is actually answering.
docker_running() {
  docker info >/dev/null 2>&1
}

docker_is_native() {
  # True only when dockerd answers AND it is not Docker Desktop.
  local os
  os="$(docker info --format '{{.OperatingSystem}}' 2>/dev/null || true)"
  [[ -n "$os" && "$os" != "Docker Desktop" ]]
}

keepalive_enabled() {
  systemctl is-enabled "$KEEPALIVE_UNIT" >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# Install steps (each idempotent / overwrite-safe)
# ---------------------------------------------------------------------------

install_docker_ce() {
  log "installing docker-ce + cli + containerd + compose-plugin"
  export DEBIAN_FRONTEND=noninteractive

  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg

  # Keyring — `install -m0644` overwrites in place (re-run safe), unlike a
  # bare `curl -o` that could leave stale perms, or `gpg --dearmor` appending.
  install -m 0755 -d /etc/apt/keyrings
  local tmp_key
  tmp_key="$(mktemp)"
  curl -fsSL "$DOCKER_GPG_URL" -o "$tmp_key"
  install -m 0644 "$tmp_key" "$DOCKER_KEYRING"
  rm -f "$tmp_key"

  # APT repo via `tee` (full rewrite — NEVER append; appending duplicates the
  # line on every re-run and breaks `apt-get update`).
  local codename arch
  arch="$(dpkg --print-architecture)"
  # shellcheck disable=SC1091
  codename="$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")"
  echo "deb [arch=${arch} signed-by=${DOCKER_KEYRING}] ${DOCKER_APT_BASE} ${codename} stable" \
    | tee "$DOCKER_APT_LIST" >/dev/null

  apt-get update -qq
  apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin

  log "docker-ce installed (NOT started — Docker Desktop still owns the socket)"
}

install_nvidia_toolkit() {
  log "installing nvidia-container-toolkit"
  export DEBIAN_FRONTEND=noninteractive

  local nv_keyring="/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg"
  local nv_list="/etc/apt/sources.list.d/nvidia-container-toolkit.list"

  # NVIDIA keyring — dearmor to a temp file, then `install` it (overwrite-safe).
  local tmp_nv_key
  tmp_nv_key="$(mktemp)"
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor > "$tmp_nv_key"
  install -m 0644 "$tmp_nv_key" "$nv_keyring"
  rm -f "$tmp_nv_key"

  # Repo via `tee` (rewrite, not append). The upstream list embeds a
  # `signed-by=` placeholder we rewrite to our keyring path.
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed "s#deb https://#deb [signed-by=${nv_keyring}] https://#g" \
    | tee "$nv_list" >/dev/null

  apt-get update -qq
  apt-get install -y -qq nvidia-container-toolkit

  # Wire the nvidia runtime into dockerd's config (writes /etc/docker/daemon.json).
  log "nvidia-ctk runtime configure --runtime=docker"
  nvidia-ctk runtime configure --runtime=docker

  # WSL2 requirement: cgroup management must be disabled or the GPU container
  # fails to start under WSL's kernel. `nvidia-ctk config --set` is idempotent.
  log "setting no-cgroups = true in $NVIDIA_RUNTIME_CONFIG (WSL2 requirement)"
  if command -v nvidia-ctk >/dev/null 2>&1; then
    nvidia-ctk config --in-place --set nvidia-container-cli.no-cgroups=true \
      || warn "nvidia-ctk config --set failed; falling back to manual edit"
  fi
  ensure_no_cgroups_true

  assert_daemon_json_valid
}

# Belt-and-suspenders: guarantee `no-cgroups = true` lives under the
# [nvidia-container-cli] table even if `nvidia-ctk config --set` is unavailable
# or wrote a differently-shaped file. Idempotent.
ensure_no_cgroups_true() {
  mkdir -p "$(dirname "$NVIDIA_RUNTIME_CONFIG")"
  [[ -f "$NVIDIA_RUNTIME_CONFIG" ]] || : > "$NVIDIA_RUNTIME_CONFIG"

  if grep -Eq '^\s*no-cgroups\s*=\s*true\s*$' "$NVIDIA_RUNTIME_CONFIG"; then
    log "no-cgroups = true already present"
    return 0
  fi

  python3 - "$NVIDIA_RUNTIME_CONFIG" <<'PY'
import re
import sys

path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
except FileNotFoundError:
    text = ""

lines = text.splitlines()
out = []
in_cli = False
done = False

for line in lines:
    stripped = line.strip()
    # Drop any existing no-cgroups assignment; we re-emit the canonical one.
    if re.match(r"^\s*no-cgroups\s*=", line):
        continue
    if re.match(r"^\s*\[", line):
        # Leaving a table: if we were inside [nvidia-container-cli], emit here.
        if in_cli and not done:
            out.append("no-cgroups = true")
            done = True
        in_cli = stripped == "[nvidia-container-cli]"
    out.append(line)

if in_cli and not done:
    out.append("no-cgroups = true")
    done = True

if not done:
    if out and out[-1].strip():
        out.append("")
    out.append("[nvidia-container-cli]")
    out.append("no-cgroups = true")

with open(path, "w", encoding="utf-8") as fh:
    fh.write("\n".join(out).rstrip("\n") + "\n")
PY

  grep -Eq '^\s*no-cgroups\s*=\s*true\s*$' "$NVIDIA_RUNTIME_CONFIG" \
    || die "failed to set no-cgroups = true in $NVIDIA_RUNTIME_CONFIG"
  log "no-cgroups = true ensured"
}

assert_daemon_json_valid() {
  [[ -f "$DOCKER_DAEMON_JSON" ]] || {
    warn "$DOCKER_DAEMON_JSON does not exist yet (nvidia-ctk did not write it)"
    return 0
  }
  if python3 -m json.tool "$DOCKER_DAEMON_JSON" >/dev/null 2>&1; then
    log "$DOCKER_DAEMON_JSON is valid JSON"
  else
    die "$DOCKER_DAEMON_JSON is NOT valid JSON — refusing to continue"
  fi
}

install_keepalive_unit() {
  [[ -f "$KEEPALIVE_SRC" ]] || die "keepalive unit not found: $KEEPALIVE_SRC"

  log "installing $KEEPALIVE_UNIT -> /etc/systemd/system/"
  install -m 0644 "$KEEPALIVE_SRC" "/etc/systemd/system/$KEEPALIVE_UNIT"

  systemctl daemon-reload
  # enable --now: hold the VM up immediately AND on every boot. Re-running is a
  # no-op if already enabled+active.
  systemctl enable --now "$KEEPALIVE_UNIT"

  if systemctl is-active "$KEEPALIVE_UNIT" >/dev/null 2>&1; then
    log "$KEEPALIVE_UNIT enabled + active (VM stays alive headless)"
  else
    warn "$KEEPALIVE_UNIT enabled but not active — check 'systemctl status $KEEPALIVE_UNIT'"
  fi
}

prepull_probe_image() {
  # Pre-pull the EXACT supervisor GPU-probe image so a cold start never blocks
  # on a registry pull. The pull needs a running dockerd, so we do it inside a
  # transient start window. If docker is already native+running, reuse it.
  log "pre-pulling probe image: $PROBE_IMAGE"
  local started_here=false
  if ! docker_running; then
    log "transiently starting docker to pull (will stop after)"
    systemctl start docker
    started_here=true
    wait_for_docker
  fi

  if docker_is_native; then
    docker pull "$PROBE_IMAGE" || warn "pull of $PROBE_IMAGE failed (cold start may pull later)"
  else
    warn "active engine is NOT native (Docker Desktop owns the socket); skipping pull"
    warn "re-run after cutover, or let cutover/--verify-gpu pre-pull it"
  fi

  if $started_here; then
    log "stopping docker again (pre-cutover: native dockerd must stay inactive)"
    # Stop docker.socket too — socket-activation can resurrect dockerd otherwise.
    systemctl stop docker docker.socket
    assert_docker_inactive
  fi
}

wait_for_docker() {
  local i
  for i in $(seq 1 30); do
    if docker_running; then
      return 0
    fi
    sleep 1
  done
  die "docker did not become reachable within 30s"
}

assert_docker_inactive() {
  local state sock_state
  state="$(systemctl is-active docker 2>/dev/null || true)"
  if [[ "$state" == "inactive" || "$state" == "failed" || "$state" == "dead" ]]; then
    log "systemctl is-active docker == $state (cutover step-0 precondition holds)"
  else
    die "docker is '$state' but MUST be inactive pre-cutover (two engines on one socket)"
  fi

  # docker.socket must ALSO be inactive — an active socket re-activates dockerd
  # on the next connection, silently resurrecting the engine pre-cutover.
  sock_state="$(systemctl is-active docker.socket 2>/dev/null || true)"
  if [[ "$sock_state" == "active" ]]; then
    die "docker.socket is '$sock_state' but MUST NOT be active pre-cutover \
(socket-activation would resurrect dockerd onto /var/run/docker.sock)"
  fi
  log "systemctl is-active docker.socket == ${sock_state:-inactive} (no socket-activation risk)"
}

# ---------------------------------------------------------------------------
# --verify-gpu : transiently start docker, prove GPU via BOTH paths, stop again
# ---------------------------------------------------------------------------

verify_gpu() {
  require_root
  log "--verify-gpu: transient docker start, dual GPU probe (CLI + docker-py API)"

  local started_here=false
  if ! docker_running; then
    log "starting docker (transient — will stop before exit)"
    systemctl start docker
    started_here=true
    wait_for_docker
  else
    warn "docker already running; not starting (will NOT stop an engine we did not start)"
  fi

  # Guard: never probe against Docker Desktop — this verify path is about the
  # native nvidia runtime. If DD owns the socket, abort loudly.
  if ! docker_is_native; then
    die "active engine is Docker Desktop, not native docker-ce; cannot verify native GPU path"
  fi

  # Probe image must be present (setup pre-pulls it; pull defensively here too).
  docker image inspect "$PROBE_IMAGE" >/dev/null 2>&1 || docker pull "$PROBE_IMAGE"

  local cli_ok=false api_ok=false

  # --- CLI path: docker run --gpus all ... nvidia-smi -L ---
  log "CLI GPU path: docker run --rm --gpus all $PROBE_IMAGE nvidia-smi -L"
  if docker run --rm --gpus all "$PROBE_IMAGE" nvidia-smi -L; then
    cli_ok=true
    echo "GPU CLI PATH: PASS"
  else
    echo "GPU CLI PATH: FAIL" >&2
  fi

  # --- API path: docker-py device_requests (matches supervisor + compose deploy.resources) ---
  log "API GPU path: docker-py device_requests=[DeviceRequest(count=-1, capabilities=[[gpu]])]"
  if verify_gpu_api; then
    api_ok=true
    echo "GPU API PATH: PASS"
  else
    echo "GPU API PATH: FAIL" >&2
  fi

  # --- Always stop docker again if we started it, so step-0 precondition holds ---
  if $started_here; then
    log "stopping docker (restoring pre-cutover inactive state)"
    # Stop docker.socket too — socket-activation can resurrect dockerd otherwise.
    systemctl stop docker docker.socket
    assert_docker_inactive
  else
    warn "docker was already running before --verify-gpu; left as-is"
    warn "stop it manually before cutover: sudo systemctl stop docker"
  fi

  if $cli_ok && $api_ok; then
    log "--verify-gpu: BOTH GPU paths PASS"
    return 0
  fi
  die "--verify-gpu: at least one GPU path FAILED (CLI=$cli_ok API=$api_ok)"
}

# docker-py probe using the exact device_requests shape the supervisor uses to
# start qodo (and that compose's deploy.resources reservation lowers to). Falls
# back to `pip install docker` into a throwaway venv if docker-py is absent, so
# the host python stays untouched.
verify_gpu_api() {
  local py_runner="python3"
  if ! python3 -c 'import docker' >/dev/null 2>&1; then
    warn "python3 'docker' (docker-py) not importable; provisioning a throwaway venv"
    local venv
    venv="$(mktemp -d)"
    if python3 -m venv "$venv" >/dev/null 2>&1 \
       && "$venv/bin/pip" install --quiet --disable-pip-version-check docker >/dev/null 2>&1; then
      py_runner="$venv/bin/python"
    else
      warn "could not provision docker-py venv; API path cannot run"
      rm -rf "$venv"
      return 1
    fi
  fi

  PROBE_IMAGE="$PROBE_IMAGE" "$py_runner" - <<'PY'
import os
import sys

try:
    import docker
    from docker.types import DeviceRequest
except Exception as exc:  # pragma: no cover - import guard
    print(f"docker-py import failed: {exc}", file=sys.stderr)
    sys.exit(1)

image = os.environ["PROBE_IMAGE"]

try:
    client = docker.from_env()
except Exception as exc:
    print(f"docker.from_env() failed: {exc}", file=sys.stderr)
    sys.exit(1)

# EXACT shape the supervisor uses to start qodo, and that compose's
# deploy.resources.reservations.devices reservation lowers to.
dev_req = DeviceRequest(count=-1, capabilities=[["gpu"]])

try:
    out = client.containers.run(
        image,
        "nvidia-smi -L",
        remove=True,
        device_requests=[dev_req],
        stderr=True,
    )
except Exception as exc:
    print(f"docker-py GPU run failed: {exc}", file=sys.stderr)
    sys.exit(1)

text = out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out)
sys.stdout.write(text)
if "GPU" not in text:
    print("docker-py run produced no GPU line", file=sys.stderr)
    sys.exit(1)
sys.exit(0)
PY
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
  if $VERIFY_GPU; then
    verify_gpu
    exit 0
  fi

  require_root

  # Idempotency fast-path: already native + keepalive enabled → nothing to do.
  if docker_is_native && keepalive_enabled; then
    log "already native docker-ce AND $KEEPALIVE_UNIT enabled — nothing to do (idempotent exit)"
    exit 0
  fi

  install_docker_ce
  install_nvidia_toolkit
  install_keepalive_unit
  prepull_probe_image

  log "install complete. Docker NOT started (Docker Desktop still owns the socket)."
  log "Next: ./setup-docker-ce.sh --verify-gpu  → prove both GPU paths, then it"
  log "      stops docker so 'systemctl is-active docker' == inactive for cutover."
  log "Reminder: set '[wsl2] vmIdleTimeout=-1' in C:\\Users\\arman\\.wslconfig (see README)."
}

main
