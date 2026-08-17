# Design — Re-platform the dev-pc embedding off Docker Desktop to headless native docker-ce

**Status**: design approved 2026-06-27; **revised post-judge** (3-judge adversarial critique) — networking
inverted to NAT+portproxy, keepalive mandatory, rollback non-destructive, GPU gate proves `device=cuda`.
See the plan's "Judge findings → resolution" matrix.
**Date**: 2026-06-27
**Owner**: brain-v42 (the embedding is brain-v42's own service / repo).
**Brain refs**: decision `296dd28f` (HANDOFF: place the embedding / kill the SPOF), learning `0269bfa2`
(dev-pc not 24/7), decision `d846389b` (red-llm moved to pc-serveur 24/7), skill
`debug-brain-v42-embedding-500` (lazy-supervisor topology).

---

## 1. Problem

The embedding service (Qodo-Embed-1-1.5B, ~5.1 GiB VRAM, port `:8003`) is the backbone of the
whole ReD ecosystem — every `brain_*` call across every sub-project depends on it. It runs on the
dev-pc (192.168.1.11, RTX 5070 Ti 16 GiB).

It runs **under Docker Desktop**, verified live on 2026-06-27:

| Probe | Result |
|---|---|
| `docker info` (in Ubuntu-24.04) | `Name=docker-desktop`, `OperatingSystem="Docker Desktop"`, `v29.1.3` |
| docker engine | Docker Desktop WSL integration (`docker-desktop-user-distro proxy`); **no native `dockerd`**, no `docker.service`, no `/etc/docker/daemon.json` |
| AutoAdminLogon | **not set** → Windows boots to the **login screen** |
| `:8003` LAN exposure | provided **only** by Docker Desktop port forwarding — **no `netsh portproxy` entry for 8003** |
| Ubuntu-24.04 | `systemd=true` (PID 1); GPU-PV works (`/usr/lib/wsl/lib/nvidia-smi` sees the RTX 5070 Ti) |
| WSL / Windows | WSL **2.6.1** on Windows 11 (build 26200) → **mirrored networking available**; global `.wslconfig` empty (default NAT) |

**Root cause**: Docker Desktop is a Windows GUI app bound to the interactive logon session. After a
crash/reboot the dev-pc sits at the login screen, Docker Desktop never starts, the embedding is down,
and the entire brain degrades to FTS-only search until someone physically logs in. The operator wants
the embedding to start and be controllable **even when the machine is at the login screen**, while
keeping the ability to **manually disable it for gaming** (as today).

## 2. Goals / non-goals

**Goals**
- Embedding stays on the dev-pc GPU (it has the VRAM; this is settled — not a relocation).
- Runs on a **headless runtime** that starts at Windows boot **without an interactive logon**.
- `:8003` reachable on `192.168.1.11:8003` after a reboot, surviving WSL IP churn.
- Remotely launchable/controllable (SSH → `wsl`), including from the login-screen state.
- Manual "disable for gaming" preserved (idle-stop + manual stop), as today.
- **No duplicate setup**: exactly one container engine owns the embedding stack at the end.

**Non-goals**
- No model change, no re-embedding (vectors stay Qodo-1536 — unchanged).
- No relocation to pc-serveur / VPS, no red-llm swap, no GPU purchase.
- No change to any consumer: `EMBEDDING_SERVICE_URL` stays `http://192.168.1.11:8003`.

## 3. Requirements

- **R1** — Embedding starts at Windows boot with nobody logged in.
- **R2** — Remotely launchable/controllable over the network in the login-screen state.
- **R3** — `:8003` exposed on the host LAN IP, surviving reboot + WSL IP churn.
- **R4** — Manual disable for gaming preserved (idle-stop + manual stop), auto-wake on next request.
- **R5** — Single-owner invariant: no two engines running the stack; old setup cleanly decommissioned.

## 4. Target architecture

```
Windows 11 (dev-pc) — boots to LOGIN SCREEN, no logon required
└─ Task Scheduler "brain-embedding-boot"  (At startup · "Run whether logged on or not" · arman · batch-logon)
   └─ boot.ps1: wsl -d Ubuntu-24.04 (blocks via keepalive) → wait :8003 → refresh netsh portproxy (:8003 only)
      ├─ systemd: docker.service                (native docker-ce, nvidia no-cgroups=true)
      │    └─ brain_embedding_supervisor         (restart=unless-stopped, publishes 0.0.0.0:8003)
      │         └─ lazy: docker start brain_embedding_qodo   → GPU via nvidia-container-toolkit
      └─ brain-embedding-keepalive.service (sleep infinity) keeps the VM alive
   NAT (networkingMode UNCHANGED) + netsh portproxy 0.0.0.0:8003 → <wsl-ip>:8003 → live on 192.168.1.11:8003
```

`EMBEDDING_SERVICE_URL` on pc-serveur is already `http://192.168.1.11:8003` — same IP:port, only the
engine behind it changes. Zero consumer-side change.

## 5. Detailed design

### 5.1 Native docker-ce in Ubuntu-24.04
- Install `docker-ce`, `docker-ce-cli`, `containerd.io`, `docker-compose-plugin` from Docker's apt repo.
- Install `nvidia-container-toolkit`; `sudo nvidia-ctk runtime configure --runtime=docker`; restart docker.
- `systemctl enable --now docker` (systemd is PID 1 in this distro, so this persists across boots).
- **Disable Docker Desktop's WSL integration for Ubuntu-24.04** (Docker Desktop → Settings → Resources
  → WSL integration → turn off `Ubuntu-24.04`) so native `dockerd` owns `/var/run/docker.sock` and the
  two engines never fight over the socket.

### 5.2 Redeploy the existing lazy-supervisor under native docker-ce
- Reuse `deploy/dev-pc/docker-compose.yml` unchanged in shape: `embedding-supervisor`
  (`restart=unless-stopped`, `0.0.0.0:8003`) + `embedding-qodo` (`restart=no`, supervisor-controlled,
  GPU reservation). The supervisor's idle-stop (`IDLE_TIMEOUT_SEC=900`), GPU-headroom gate
  (`GPU_MIN_FREE_MIB`), and manual-stop behaviour are unchanged → **R4 preserved for free**.
- Validate GPU works **inside the container under native docker-ce** (the toolkit + `deploy.resources`
  reservation path), independent of Docker Desktop.

### 5.3 Headless boot (R1)
- A Task Scheduler task ("run whether user is logged on or not", at system startup) runs `boot.ps1`,
  which instantiates `wsl -d Ubuntu-24.04` → systemd → `docker.service` → the `restart=unless-stopped`
  supervisor.
- **Keepalive is mandatory, not insurance** (judge C4): `/bin/true` exits instantly and WSL reaps an idle
  distro (~8 s / `vmIdleTimeout`), killing systemd+docker. So we ship an always-on systemd keepalive unit
  (`brain-embedding-keepalive.service`, `sleep infinity`) **and** set `[wsl2] vmIdleTimeout=-1` in
  `.wslconfig`. Validated by idling the box past the idle window (not just quick reboots).

### 5.4 Networking — NAT + portproxy (R3) — primary path (revised post-judge)
- **Mirrored networking was DEMOTED** (judge C5). Tracked WSL issues (#10494/#10683/#13868) document that
  `networkingMode=mirrored` frequently fails to bind container-published ports on the Windows side — a
  silent unowned port. Betting the brain backbone on it is wrong, and the global `.wslconfig` change would
  also reshape the `docker-desktop` distro and the box's existing `:8001`/`:9100` services.
- **Primary**: keep **default NAT** (no `networkingMode` change → no global blast radius) and expose
  `:8003` with a **boot-time `netsh portproxy` refresh** — exactly the model the box already uses for
  `:8001`/`:9100`. `boot.ps1` computes the current WSL IP each boot and re-points **only** the `:8003`
  rule (`add`/`delete` for `:8003`, never `reset all`, never touching `:8001`/`:9100`), and ensures a
  firewall allow rule for TCP 8003.
- `.wslconfig` is used **only** for `[wsl2] vmIdleTimeout=-1` (keepalive) — `networkingMode` untouched.
- Mirrored networking is deferred to a separate experiment, **out of scope** for this migration.

### 5.5 Remote control + gaming-disable (R2, R4)
- Baseline (no new code): everything is controllable via `ssh arman@192.168.1.11` → `wsl -d
  Ubuntu-24.04 -e docker ...` / `systemctl ...`. Manual disable for gaming:
  `ssh arman@192.168.1.11 wsl -d Ubuntu-24.04 -e docker stop brain_embedding_qodo`
  (supervisor auto-wakes it on the next request, exactly like today).
- **Optional (flagged, can be cut)**: add `POST /disable` + `POST /enable` to the supervisor so a
  "gaming mode" can be toggled with a single `curl` from pc-serveur and `/disable` *suppresses
  auto-wake* until `/enable`. Nice-to-have; the SSH one-liner already satisfies R4.

### 5.6 Single-owner invariant — no duplicate setup (R5)
This is the operator's explicit requirement. At all times after cutover, **exactly one engine** holds
`brain_embedding_*` and **exactly one process** listens on host `:8003`.

Canonical order (reconciled with the plan; non-destructive; falsifiable check — judges C2/H1/H4):
0. **Preconditions**: native docker **inactive** (`systemctl is-active docker` = inactive) and DD owns
   the socket (`docker info` OperatingSystem == "Docker Desktop"). Abort loudly otherwise — prevents the
   two-daemons-fight-over-`/var/run/docker.sock` race.
1. **SAVE (non-destructive)**: build the images under DD if missing, then `docker save` supervisor+qodo
   to a tarball. The DD images are the rollback artifact — they are **retained**, not deleted.
2. **Decommission DD containers only** (images kept): `docker rm -f` the two containers while DD is still
   reachable; assert (against the live DD engine) zero `brain_embedding_*` remain and persist the proof
   log. This is the falsifiable R5 check — done while DD is still inspectable, **not** after it is severed.
3. **Disable DD's Ubuntu-24.04 WSL integration** (frees `/var/run/docker.sock`); confirm the old socket
   is released. Brief embedding downtime here is acceptable (FTS-only fallback).
4. **Native up**: `systemctl enable --now docker`; assert engine != "Docker Desktop"; `docker load` the
   tarball; `docker compose up -d --no-build`.
5. **Host-level invariant**: exactly one listener on `:8003` **and its PID belongs to native dockerd**
   (not "DD unreachable == clean", which is unfalsifiable). Disable DD auto-start.
- DD stays **installed** (integration off, auto-start off, images retained until `finalize.sh` after N
  stable days). Full DD removal remains an open option; until finalize, rollback is non-destructive.

## 6. Migration / cutover (gated) and rollback

1. Stage: install docker-ce + toolkit, write `.wslconfig`, build images natively, create the Task
   Scheduler task — all alongside the still-running Docker Desktop stack.
2. Decommission per §5.6 (single-owner invariant), bring the stack up natively.
3. **VALIDATION GATE (make-or-break)**: reboot the dev-pc and **do NOT log in**. From pc-serveur, at the
   login screen:
   - First isolate GPU-PV: `ssh … wsl -d Ubuntu-24.04 -e /usr/lib/wsl/lib/nvidia-smi -L` → the RTX 5070 Ti
     (separates "headless GPU-PV" from "nvidia runtime in container").
   - Then, because the gate runs from pc-serveur and **cannot stop qodo itself**, the OPERATOR first forces a
     true **cold start** over SSH — `ssh arman@192.168.1.11 wsl -d Ubuntu-24.04 -e docker stop brain_embedding_qodo`
     — so the supervisor's separate GPU-probe container path runs at session 0 (judge C3).
   - Then `validate-headless.sh`: `/healthz` 200; **`GET /` → `device=="cuda"` AND `cuda_available==true`**
     (a 1536-float vector ALONE is NOT enough — qodo silently falls back to CPU, judge C1) — this first
     `GET /` triggers the genuine cold start and is timed; `/embed` → 1×1536 within a warm-latency ceiling;
     the boot→first-`/embed` window (GET / wake + first /embed) within the agreed bound.
   Only green = trust the headless path. Do **not** decommission DD (run `finalize.sh`) until green and
   stable. On red, `rollback.sh`; evaluate the AutoAdminLogon-to-locked fallback (operator sign-off; it
   stores a cleartext registry password — security regression).
4. Confirm `brain_search` works from a live Claude session.
5. **Rollback (non-destructive)**: `rollback.sh` — re-enable DD's Ubuntu-24.04 integration; the DD images
   were **retained** (cutover only removed containers), so DD comes back to today's state. If
   `finalize.sh` already removed them, `rollback.sh` `docker load`s the tarball first.

## 7. Success criteria / acceptance tests

After a **cold reboot with nobody logged in**:
- `GET /` reports `device=="cuda"` + `cuda_available==true` (**GPU, not silent CPU fallback**); `/embed`
  returns a 1536-d vector within the warm-latency ceiling.
- Cold-start within bound: **supervisor reachable ≤ 25 s**, **first warm `/embed` ≤ 120 s** (provisional;
  measured on the box, then frozen). The gate times and enforces it.
- `brain_search` works from a live Claude session (semantic, not FTS-fallback).
- Manual disable (`docker stop` of qodo) frees the GPU; next request auto-wakes it (tested over SSH at the
  login screen — R2+R4 headless).
- The stack survives **idling past the WSL idle window** (proves keepalive) AND a **second** reboot.
- Single-owner invariant holds: one `:8003` listener owned by native dockerd; DD auto-start off.

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| **GPU-PV fails with no logon (session 0)** | §6 gate isolates it (`/usr/lib/wsl/lib/nvidia-smi` first); do not finalize until green. Fallback: AutoAdminLogon-to-locked (sign-off). |
| **Silent CPU fallback** (qodo `device=cpu`) reads as success | Gate asserts `device==cuda` via `GET /` + latency ceiling (judge C1). |
| **Supervisor GPU-probe** container fails headless → `gpu_busy`, qodo never starts | Pre-pull exact probe image `…ubuntu22.04`; the **operator** forces a cold start before the gate (`ssh … docker stop brain_embedding_qodo` — the gate runs from pc-serveur and cannot stop qodo), so the gate's first `GET /` runs the probe path (judge C3). |
| **Non-restorable rollback** (images deleted) | Cutover is non-destructive: `docker save` tarball + retain DD images until `finalize.sh` (judge C2). |
| WSL distro self-terminates when idle | **Mandatory** keepalive unit + `vmIdleTimeout=-1` (judge C4); validate by idling past the window. |
| Mirrored networking breaks docker port-publish | Mirrored **demoted**; NAT + `netsh portproxy` for `:8003` is primary (judge C5). |
| Task Scheduler stored-password / rotation / batch-logon | Document preconditions; **heartbeat alert** from pc-serveur catches silent boot-task failure (judge H2). |
| nvidia runtime under native docker-ce (WSL) | `no-cgroups=true`; verify CLI **and** docker-py API/`deploy.resources` path (judge H3). |
| Docker Desktop vs docker-ce socket conflict | Step-0 precondition asserts (native inactive, DD owns socket) abort out-of-order (judge H4). |
| **Duplicate stack** at next login | Falsifiable host-level invariant + DD auto-start off + post-login re-check (judge H1). |

## 9. Open decisions (resolved as autonomous defaults — operator may override)
1. **Docker Desktop**: keep installed (integration off, auto-start off, images retained until finalize).
2. **Supervisor `/disable`+`/enable`**: out of scope (SSH `docker stop` is enough).
3. **Cold-start**: supervisor ≤ 25 s / first warm `/embed` ≤ 120 s (provisional; measure → freeze).
4. **Networking**: NAT + `netsh portproxy` (primary); mirrored deferred.
