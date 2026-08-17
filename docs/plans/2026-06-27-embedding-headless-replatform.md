# Plan — Headless re-platform of the dev-pc embedding off Docker Desktop (v2, post-judge)

**Source spec**: `docs/superpowers/specs/2026-06-27-embedding-headless-replatform-design.md`.
**Branch**: `feat/embedding-headless-replatform`.
**Brain ref**: HANDOFF decision `296dd28f`.
**Revision**: v2 — rewritten after a 3-judge adversarial critique (2026-06-27). See "Judge findings → resolution".

## Goal

Move the Qodo embedding stack on the dev-pc (192.168.1.11) from **Docker Desktop** (bound to the Windows
logon session) to **native docker-ce inside Ubuntu-24.04 WSL2**, started **headless at Windows boot**, so
`192.168.1.11:8003` survives a reboot-to-login-screen. Keep embedding on the dev-pc GPU; preserve the
gaming-disable toggle; enforce a **single-owner invariant**; and — critically — make the migration
**non-destructive and reversible**, with a validation gate that actually proves **GPU (not CPU)** at
session 0.

The repo deliverables (scripts + Windows artifacts + runbook) are committed and reviewable; the **live
cutover is operator-run** following the committed runbook, with hard gates. Not auto-executed by subagents.

## Judge findings → resolution (traceability)

| # | Sev | Finding | Resolution in v2 |
|---|---|---|---|
| C1 | Crit | Gate passes on silent **CPU fallback** (`qodo/main.py:35`) | Task 4 gate asserts `GET /` → `device=="cuda"` AND `cuda_available==true` AND a warm-embed latency ceiling; CPU body → non-zero exit (tested). |
| C2 | Crit | Rollback impossible — cutover **deletes DD images** before native is proven | New order: `docker save` DD images → tarball; native brings up via `docker load` (no rebuild); DD images/containers **never deleted at cutover**; `docker rmi` only in a **finalize** step after N stable days. Rollback = re-enable DD integration (images intact) or `docker load` tarball. |
| C3 | Crit | Supervisor **GPU-probe path** untested; wrong verify image (22.04 vs 24.04) | setup verifies the **exact** probe image `nvidia/cuda:12.4.1-base-ubuntu22.04` + pre-pulls it; the **OPERATOR forces a cold start** before the gate (`ssh arman@192.168.1.11 wsl -d Ubuntu-24.04 -e docker stop brain_embedding_qodo` — the gate runs from pc-serveur and cannot stop qodo itself), so the gate's first `GET /` triggers the probe→start path at session 0; probe logs WARN on 0. |
| C4 | Crit | R1 keepalive left to chance (`/bin/true` exits → distro reaped) | Mandatory: ship `brain-embedding-keepalive.service` (systemd) **and** `[wsl2] vmIdleTimeout=-1` in `.wslconfig`; boot action blocks (`sleep infinity`). Validated by idling past the idle window, not just quick reboots. |
| C5 | Crit | **Mirrored networking** default is known-fragile for docker port-publish (#10494/#10683/#13868) | **Inverted**: NAT + boot-time `netsh portproxy` refresh for :8003 is the **PRIMARY** path (matches the box's existing :8001/:9100 model). `.wslconfig` networkingMode is **unchanged** → no global blast radius. Mirrored is a deferred experiment, out of this migration. |
| H1 | High | Single-owner check **unfalsifiable** ("unreachable == clean") | Decommission proof captured **before** integration-off (assert DD engine shows zero `brain_embedding_*`); post-cutover check is **host-level**: exactly one `:8003` listener AND its PID belongs to native `dockerd`; plus explicit post-login DD re-check + **disable DD auto-start**. |
| H2 | High | Task Scheduler stored-password / batch-logon / rotation / AutoAdminLogon security | Documented preconditions (non-empty password, "Log on as a batch job" right); password-rotation-breaks-boot warning; **heartbeat ships WITH cutover** (`heartbeat-check.sh` + cron, op-seq step 7) — curls `GET /` for `device==cuda`, the sole detector of a silent unattended boot failure; no creds in committed XML; AutoAdminLogon fallback requires explicit sign-off + cleartext-registry warning. |
| N1-N4 | High/Med/Low | Final-review regressions: heartbeat deferred; `--verify-gpu` socket window; `/healthz` carries no device; weak tarball verify | Heartbeat promoted in-scope (Task 4 + op-seq 7); op-seq 1 stops docker + asserts inactive before cutover; heartbeat/gate curl `GET /` not `/healthz`; SAVE proves restorable via `tar tf` + throwaway `docker load`. |
| H3 | High | nvidia runtime under native docker-ce needs `no-cgroups=true` + API path | setup sets `no-cgroups=true` in `/etc/nvidia-container-runtime/config.toml`; verifies **both** CLI `--gpus all` and the docker-py `device_requests`/compose `deploy.resources` path. |
| H4 | High | Cutover ordering self-contradiction (plan vs spec) + socket race | One canonical order (below) with precondition asserts: native docker **inactive** + DD owns socket before rm; assert "talking to Docker Desktop" before any rm; abort loudly out-of-order. Spec §5.6 updated to match. |
| M1 | Med | ≤60s cold-start unrealistic + unmeasured | Split: **supervisor reachable ≤ 25 s**, **first warm `/embed` ≤ 120 s** (provisional — measured on the box, then frozen). Gate times the window and fails if exceeded; images pre-pulled. |
| M2 | Med | DD auto-start not disabled | Disable "Start Docker Desktop when you sign in"; add to invariant + runbook. |
| M3 | Med | `setup` idempotency (keyring/repo/daemon.json) | Keyring via `install -m0644` (overwrite); repo via `tee` (not append); assert `daemon.json` valid JSON; re-run-twice test. |
| M4 | Low | bats vs pytest undecided | **pytest** stub-server test (existing CI lane), no new CI dep. |
| M5 | Med | R2 ssh→wsl never tested no-login | Acceptance test: from pc-serveur at login screen, run the exact gaming-disable one-liner + confirm auto-wake; verify cmd.exe→wsl quoting. |
| M6 | Low | `wsl --shutdown` foot-gun ordering | Sequenced after invariant check; documented it bounces DD too, harmless before the no-login reboot. |

## Resolved decisions (autonomous defaults — operator may override)
1. **Docker Desktop**: keep installed; integration **off** for Ubuntu-24.04; **auto-start off**; embedding
   stripped (containers stopped, images retained as rollback artifact until finalize).
2. **Supervisor `/disable`+`/enable`**: out of scope (SSH `docker stop` covers gaming-disable).
3. **Cold-start targets**: supervisor reachable ≤ 25 s; first warm `/embed` ≤ 120 s (measure → freeze).
4. **Networking**: NAT + `netsh portproxy` (primary). Mirrored networking deferred (NOT in this migration).

## Non-goals
No model change / re-embedding (vectors stay Qodo-1536). No relocation / red-llm swap / GPU purchase. No
consumer change — `EMBEDDING_SERVICE_URL` stays `http://192.168.1.11:8003`. No mirrored-networking switch.

## Architecture (v2)

```
Win11 dev-pc — boots to LOGIN SCREEN, no logon
└─ Task Scheduler "brain-embedding-boot" (At startup · run whether logged on or not · arman · batch-logon)
   └─ boot.ps1:  wsl -d Ubuntu-24.04 (blocks via keepalive)  →  wait :8003 inside WSL
                 →  netsh portproxy refresh: 0.0.0.0:8003 → <wsl-ip>:8003   (additive; only :8003)
      └─ Ubuntu-24.04: systemd PID1
           ├─ docker.service (native docker-ce, nvidia runtime, no-cgroups=true)
           │    └─ brain_embedding_supervisor (restart=unless-stopped, :8003)
           │         ├─ GPU probe: nvidia/cuda:12.4.1-base-ubuntu22.04 --gpus all (pre-pulled)
           │         └─ lazy: docker start brain_embedding_qodo  → GPU (device=cuda)
           └─ brain-embedding-keepalive.service (sleep infinity)  ← keeps the VM alive
   (.wslconfig: [wsl2] vmIdleTimeout=-1   — networkingMode UNCHANGED/NAT)
```

## File structure

| File | New/Mod | Purpose |
|---|---|---|
| `deploy/dev-pc/setup-docker-ce.sh` | new | Idempotent install: docker-ce + nvidia-container-toolkit + `no-cgroups=true`; verify GPU via CLI **and** API using the exact probe image; pre-pull images. Does NOT start docker (deferred to cutover). |
| `deploy/dev-pc/cutover.sh` | new | Canonical-order cutover: `docker save` DD images → tarball; assert DD-owned; stop DD containers; (manual) integration-off gate; start native; `docker load`; bring up; falsifiable single-owner check. |
| `deploy/dev-pc/finalize.sh` | new | Run after N stable days: `docker rmi` DD images, remove tarball. Separate from cutover so rollback stays possible. |
| `deploy/dev-pc/rollback.sh` | new | Re-enable path: stop native stack, `docker load` tarball into DD (if needed), guide DD integration re-enable. Self-contained, does not assume images persist. |
| `deploy/dev-pc/validate-headless.sh` | new | From pc-serveur: assert `/healthz`=200, `GET /`→`device==cuda`+`cuda_available`, warm-embed dim=1536 + latency ceiling; **times** boot→first-embed; prints `/status` on 503. |
| `deploy/dev-pc/heartbeat-check.sh` + cron | new | **Ships WITH cutover** (not follow-up — judge N1/H2): pc-serveur cron curls `GET /` (NOT `/healthz` — it carries no `device`), alerts on non-200 / `device!=cuda` / unreachable. Catches a silent 3am headless-boot failure (the exact fault this migration exists to fix). Reuses `validate-headless.sh` assertion core. red-monitor/red-alerts integration is a later enrichment, not the baseline. |
| `deploy/dev-pc/systemd/brain-embedding-keepalive.service` | new | Always-on `sleep infinity` unit; keeps the WSL VM alive. Installed + enabled by setup. |
| `deploy/dev-pc/windows/.wslconfig` | new | `[wsl2] vmIdleTimeout=-1` (networkingMode unchanged). Reference to copy to `C:\Users\arman\.wslconfig`. |
| `deploy/dev-pc/windows/boot.ps1` | new | Boot script: start distro (blocks), wait for :8003 in WSL, refresh `netsh portproxy` for :8003 only, ensure firewall rule. |
| `deploy/dev-pc/windows/brain-embedding-boot.xml` | new | Task Scheduler task (At startup, run-whether-logged-on-or-not) running `boot.ps1`. No embedded creds. |
| `deploy/dev-pc/README.md` | mod | Headless runbook: canonical cutover, validation gate, rollback, finalize, single-owner invariant, credential lifecycle + heartbeat, daily ops (gaming-disable). |
| `tests/dev_pc/test_validate_headless.py` | new | pytest stub-server tests for `validate-headless.sh`: cuda body → pass; cpu body → fail; wrong dim → fail; non-200 → fail; over-latency → fail. |

## Canonical cutover order (load-bearing; enforced by cutover.sh asserts)

```
0. Preconditions: native docker INACTIVE (systemctl is-active docker == inactive); DD owns the socket
   (docker info OperatingSystem == "Docker Desktop"). Abort loudly otherwise.
1. SAVE: docker compose build (under DD if images missing); docker save supervisor+qodo → /var/backups/brain-embedding-images.tar
2. STOP (not remove): docker rm -f the two DD CONTAINERS only. Assert DD engine now shows zero brain_embedding_*; persist proof log. Images RETAINED.
3. INTEGRATION OFF (manual, gated): operator disables DD → Ubuntu-24.04 WSL integration; cutover waits for confirm; assert old socket released.
4. NATIVE UP: systemctl enable --now docker; assert engine != "Docker Desktop"; docker load < tarball; docker compose up -d (--no-build) supervisor (+ qodo --no-start).
5. INVARIANT (falsifiable, host-level): exactly one OWNER ENGINE — every :8003 listener PID belongs to native dockerd (IPv6 dual-stack yields one docker-proxy per family; all must resolve to dockerd); log it. (DD-side "clean" was already proven in step 2.)
6. Disable DD auto-start. cutover PROMPTS the operator to run `wsl --shutdown` next (operator-run — the script does NOT run it, since that would kill its own WSL session; note: bounces DD too — fine, next is the no-login reboot).
```

---

## Task 1 — `setup-docker-ce.sh` + keepalive unit (engine + GPU runtime, no start)

- [ ] `set -euo pipefail`. Idempotent. Does NOT start docker (no socket grab pre-cutover).
- [ ] Install docker-ce/cli/containerd/compose-plugin from Docker's apt repo: keyring via
      `install -m0644` (overwrite-safe), repo via `tee` (not append).
- [ ] Install nvidia-container-toolkit; `nvidia-ctk runtime configure --runtime=docker`; **set
      `no-cgroups = true`** in `/etc/nvidia-container-runtime/config.toml` (WSL2 requirement); assert
      `/etc/docker/daemon.json` is valid JSON (`python3 -m json.tool`).
- [ ] Pre-pull `nvidia/cuda:12.4.1-base-ubuntu22.04` (the **exact** supervisor probe image) so cold start
      never waits on a registry pull.
- [ ] Install + enable `deploy/dev-pc/systemd/brain-embedding-keepalive.service` (`sleep infinity`,
      `Restart=always`) and confirm `[wsl2] vmIdleTimeout=-1` guidance is in the README.
- [ ] GPU verify (requires a transient `systemctl start docker` in a guarded `--verify-gpu` subcommand,
      run only when explicitly invoked, then stop): **CLI path** `docker run --rm --gpus all
      nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi -L`, **AND API path** a docker-py
      `device_requests=[DeviceRequest(count=-1, capabilities=[["gpu"]])]` run matching the supervisor +
      compose `deploy.resources` path. Print PASS/FAIL for each.
- [ ] `shellcheck` clean. `bash -n`. Re-run twice in the bats/pytest harness → still clean.
- [ ] Commit: `chore(dev-pc): native docker-ce + nvidia(no-cgroups) install + keepalive unit`.

## Task 2 — `cutover.sh` (canonical order, non-destructive, falsifiable invariant)

- [ ] Implement the 6-step canonical order above; `--yes` for non-interactive; each step asserts its
      precondition and **aborts loudly** on violation (esp. step 0 native-inactive + DD-owns-socket, and
      step 4 engine-is-native).
- [ ] Step 1 SAVE writes the tarball and **proves it restorable** (judge N4): `tar tf` lists both image
      manifests AND a `docker load` into a throwaway tag succeeds — never a size-only check. Step 2 removes
      **containers only**, retains images, persists the zero-`brain_embedding_*` proof. Step 5 invariant is
      host-level (`ss -ltnp 'sport = :8003'` → one listener; resolve PID → assert it is dockerd-owned).
- [ ] `shellcheck` clean. Commit: `chore(dev-pc): non-destructive single-owner cutover (DD→native)`.

## Task 3 — `finalize.sh` + `rollback.sh`

- [ ] `finalize.sh`: run only after N stable days — `docker rmi` the DD images, remove the tarball, print
      that rollback-to-DD now requires a rebuild.
- [ ] `rollback.sh`: stop native stack; if DD images were already finalized, `docker load` the tarball
      back into DD; print the manual DD-integration re-enable steps; verify `:8003` served by DD again.
- [ ] `shellcheck` clean. Commit: `chore(dev-pc): finalize + self-contained rollback scripts`.

## Task 4 — `validate-headless.sh` + pytest (the GPU gate — TDD)

- [ ] Write `tests/dev_pc/test_validate_headless.py` FIRST (failing), stub HTTP server:
      - `GET /` body `{"device":"cuda","cuda_available":true,...}` + `/embed` 1×1536 + fast → exit 0.
      - body `{"device":"cpu","cuda_available":false}` → **exit non-zero** ("CPU fallback rejected").
      - `/embed` wrong inner dim (e.g. 768) → non-zero. non-200 → non-zero. latency over ceiling → non-zero.
- [ ] Implement `validate-headless.sh` (run from pc-serveur): `curl /healthz` 200; `curl GET /` → assert
      `device=="cuda"` AND `cuda_available==true`; `POST /embed {"texts":["gate"]}` → assert outer 1,
      inner 1536, **and** warm latency ≤ ceiling; **time** the first call and fail if > the agreed
      boot→first-embed bound; on 503 print supervisor `/status` (state, gpu_free_mib) for diagnosis.
      Print `HEADLESS-GPU GATE: PASS/FAIL` + reason.
- [ ] Emit `heartbeat-check.sh` (thin wrapper over the same assertion core: `GET /` → `device==cuda` +
      200, exit non-zero + emit an alert line on fail) and a documented pc-serveur cron entry. Ships now,
      not later (judge N1/H2) — it is the only detector of a silent unattended boot failure.
- [ ] Tests green; `shellcheck` clean. Commit: `feat(dev-pc): GPU-proving headless gate + heartbeat + tests`.

## Task 5 — Windows artifacts (NAT + portproxy + headless boot)

- [ ] `windows/.wslconfig`: `[wsl2]\nvmIdleTimeout=-1` (networkingMode unchanged).
- [ ] `windows/boot.ps1`: `wsl -d Ubuntu-24.04 -u root -e /bin/true` to instantiate, then loop until
      `wsl -d Ubuntu-24.04 -e curl -s localhost:8003/healthz` answers (the keepalive unit holds the VM up);
      compute WSL IP (`wsl -d Ubuntu-24.04 -e hostname -I` first token); **delete+add the :8003 portproxy
      rule only** (`netsh interface portproxy delete v4tov4 listenport=8003 listenaddress=0.0.0.0` then
      `add ... connectaddress=<wsl-ip> connectport=8003`) — never `reset all`, never touch :8001/:9100;
      ensure a firewall allow rule for TCP 8003 exists (`netsh advfirewall firewall add rule ... || true`).
- [ ] `windows/brain-embedding-boot.xml`: At-startup task, "run whether logged on or not", RunLevel
      Highest, action `powershell -ExecutionPolicy Bypass -File ...\boot.ps1`. **No embedded credentials**
      in the XML (operator supplies via `schtasks /rp *`).
- [ ] Commit: `chore(dev-pc): NAT portproxy boot script + headless scheduled task + wslconfig`.

## Task 6 — Rewrite `deploy/dev-pc/README.md` (runbook)

- [ ] Sections: Architecture (v2 diagram, NAT+portproxy); **One-time migration** (setup → save/build →
      cutover canonical order → manual integration-off → native up); **Validation gate** (⚠ reboot, do NOT
      log in; from pc-serveur first run `ssh ... wsl ... /usr/lib/wsl/lib/nvidia-smi -L` to isolate GPU-PV,
      then `validate-headless.sh`; only on PASS proceed); **Credential lifecycle** (non-empty password +
      batch-logon right; password change re-breaks boot); **Heartbeat** (pc-serveur cron running
      `heartbeat-check.sh` — curls **`GET /`** for `device==cuda`, NOT `/healthz` which carries no device;
      alerts on fail; **deployed WITH cutover**, red-monitor/red-alerts a later enrichment);
      **Single-owner invariant** (how to check; DD auto-start off); **Daily ops** (gaming
      disable via SSH one-liner, auto-wake; observability); **Rollback** (`rollback.sh`); **Finalize**.
- [ ] Commit: `docs(dev-pc): headless runbook v2 (cutover, GPU gate, rollback, credentials, heartbeat)`.

---

## Operator cutover sequence (run after merge; gated)
1. WSL: `setup-docker-ce.sh` then `setup-docker-ce.sh --verify-gpu` → both GPU paths PASS; then **stop
   docker and confirm `systemctl is-active docker` == inactive** before cutover (judge N2 — `--verify-gpu`
   transiently starts dockerd while DD still owns the socket; it MUST be stopped so cutover step-0's
   precondition holds and the two engines never coexist on `/var/run/docker.sock`).
2. Windows: copy `.wslconfig`, install boot task (`schtasks /create ... /rp *`).
3. WSL: `cutover.sh` (canonical order; integration-off gate; invariant PASS).
4. `cutover.sh` **prompts** the operator to run `wsl --shutdown` next (operator-run — the script does
   NOT run it, as that would kill its own WSL session); run `wsl --shutdown`, then **reboot, do NOT log in.**
5. From pc-serveur, at login screen: (a) `ssh arman@…11 wsl -d Ubuntu-24.04 -e /usr/lib/wsl/lib/nvidia-smi -L`
   (isolates headless GPU-PV); (b) **force a cold start** so the gate's first `GET /` exercises the probe path
   (the gate runs from pc-serveur and cannot stop qodo itself):
   `ssh arman@192.168.1.11 wsl -d Ubuntu-24.04 -e docker stop brain_embedding_qodo`;
   (c) `validate-headless.sh` → **PASS** (device=cuda, dim 1536, GET /+embed within bound).
6. Confirm `brain_search` works from a live Claude session (semantic, not FTS).
7. **Deploy the heartbeat now** (not later): install `heartbeat-check.sh` + its cron on pc-serveur so the
   very next unattended reboot is monitored — a silent boot-task failure must page, not go unnoticed.
8. Gaming-disable test from pc-serveur (login screen): SSH one-liner stops qodo → frees GPU; next `/embed`
   auto-wakes (proves R2+R4 headless).
9. Idle the box past the WSL idle window (no login, no traffic) → `:8003` still answers (proves keepalive).
10. **Second** no-login reboot → gate PASS again (persistence).
11. Leave DD installed (integration off, auto-start off, images retained) N stable days, then `finalize.sh`.

**If step 5 FAILs**: do not trust headless. `rollback.sh`. Escalate; evaluate AutoAdminLogon-to-locked
fallback (requires operator sign-off; stores a cleartext registry password — security regression).

## Acceptance criteria
- Branch: all tasks committed; `shellcheck` clean; Task 4 pytest green (incl. CPU-reject + latency cases);
  repo `pytest` unaffected.
- Runbook self-sufficient. Rollback proven re-runnable (images retained / tarball loadable).
- Post-cutover (operator-run): no-login reboot → `device==cuda`, dim 1536, within bound; `brain_search`
  semantic; gaming-disable frees GPU + auto-wakes; survives idle-past-window AND a 2nd reboot;
  single-owner invariant holds (one :8003 listener, native dockerd; DD auto-start off).
- Brain updated: `brain_update_project_focus` + `brain_learn` for cutover gotchas.

## Rollback (repo)
Branch isolated; scripts inert until run. Live rollback = `rollback.sh` (non-destructive: DD images
retained until `finalize.sh`).

## Live cutover attempt — findings (2026-06-27) — READ BEFORE RETRYING

A live cutover was attempted over SSH and **rolled back** (backbone restored to Docker Desktop, verified
`device=cuda` + LAN-reachable). Brain learning `fed844b3`. What we learned:

**PROVEN working** — GPU under native docker-ce in WSL2: `device=cuda`, `/embed` 1536-d end-to-end,
`docker.service` (enabled) auto-starts when the distro boots. The make-or-break is answered.

**Plan corrections (apply before retry):**
- **qodo needs `runtime: nvidia`**, NOT `deploy.resources.reservations.devices`. Under native docker-ce the
  device-request with `capabilities:[gpu]` mounts only the `gpu` cap (not `compute,utility`) →
  `torch.cuda.is_available()==False` (silent CPU). `runtime: nvidia` + the image's
  `NVIDIA_VISIBLE_DEVICES=all`/`NVIDIA_DRIVER_CAPABILITIES` gives real CUDA. (The device==cuda gate caught this.)
- **Run scripts as `wsl -d Ubuntu-24.04 -u root`** — WSL user `hawkixs` has NO passwordless sudo. (The
  scripts' `exec sudo` re-exec hangs over non-interactive SSH; `-u root` sidesteps it.)
- **No repo/build-context on the dev-pc** — it's deployed from pc-serveur via `docker --context dev-pc`
  (ssh). Native deploy must `docker save | docker load` the images (qodo image is 24 GB / 9.2 GB tarball),
  not build. Live images are `dev-pc-embedding-*` (project basename) — re-tag/`save` what's actually running.

**THE BLOCKER (unsolved — design it properly, do NOT improvise live):** reliable WSL2→LAN exposure of `:8003`.
- NAT + `netsh portproxy` → forward leg `SYN_SENT` (sources from the LAN IP; WSL never replies; the
  pre-existing `:9100` portproxy is broken the same way; WSL2 localhost-forwarding is also off).
- Mirrored networking → exposes the host LAN IP (`eth1=192.168.1.11`, LAN-reachable!) but is **flaky** and
  **transiently breaks docker embedded DNS** (`supervisor→brain_embedding_qodo` name resolution fails → the
  supervisor kills qodo mid-cold-start). Unacceptable for the backbone.
- Windows side has **only PowerShell 5.1** (no python/socat); a PS C# `Add-Type` forwarder launched via
  SSH→cmd→powershell didn't bind (wsl.exe misbehaves in that nested non-interactive context).

**Recommended next approach:** pick a robust LAN-exposure mechanism FIRST — (a) a real forwarder/reverse-proxy
installed as a Windows **service** (e.g. `nssm` + `socat`, or a tiny Go/.NET binary) so it survives reboot and
sources correctly; or (b) investigate the portproxy forward-routing (WSL `rp_filter`, Windows Firewall on the
vEthernet adapter); or (c) Windows **AutoAdminLogon-to-locked** + keep Docker Desktop (simplest, accepts an
always-logged-in session). THEN headless boot via the real Task Scheduler task (needs arman's Windows
password) + a genuine no-login reboot validation. Build/test the forwarder + boot task in the actual
scheduled-task context, never via nested SSH on the live backbone.

**dev-pc leftover state (harmless; clean up if abandoning native):** docker-ce + nvidia-container-toolkit
installed (`no-cgroups=true`) but `docker.service`/`docker.socket` **masked**; keepalive unit installed +
disabled; rollback tarball `/var/backups/brain-embedding-images.tar` (9.2 G); native compose
`/opt/brain-v42-deploy/docker-compose.yml` (has the `runtime: nvidia` fix); forwarder
`C:\Users\arman\brain-embedding-forward.ps1`.
