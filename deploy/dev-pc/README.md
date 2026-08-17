# Dev-PC headless embedding — runbook (v2)

> **SUPERSEDED FOR ACTIVE BRAIN TRAFFIC (2026-07-06).** The production/default endpoint was
> restored to the local pc-serveur service at `http://localhost:8003` by commit `9c43cc0` after
> the dev-pc failure. This runbook is retained only as rollback/reference material. Do not point
> Brain traffic at `192.168.1.11:8003` without a new explicit cutover and fresh validation.

During the superseded v2 cutover, the Qodo embedding service at `192.168.1.11:8003`
(Qodo-Embed-1-1.5B, ~5.1 GiB VRAM, 1536-d vectors) carried ReD embedding traffic. This runbook
records that **historical v2 headless architecture** — native `docker-ce` inside
`Ubuntu-24.04` (WSL2), started at Windows boot with nobody logged in and surviving a
reboot-to-login-screen. No active Brain consumer should depend on it now.

> **Why v2 exists.** v1 ran under **Docker Desktop**, a Windows GUI app bound to the
> interactive logon session. After any crash/reboot the dev-pc sat at the login screen,
> Docker Desktop never started, and the entire brain degraded to FTS-only search until
> a human physically logged in. v2 removes that single point of failure.

This document is written to be **self-sufficient for a tired operator at 3 a.m.** Read
the section you need top-to-bottom; do not improvise the order of the cutover.

---

## 0. Glossary of files in this directory

| File | Role |
|---|---|
| `docker-compose.yml` | The 2-service stack: `embedding-supervisor` (always up, `:8003`) + `embedding-qodo` (GPU, supervisor-controlled). Both local images use `pull_policy: build`. |
| `setup-docker-ce.sh` | One-time installer (WSL): docker-ce + nvidia toolkit (`no-cgroups=true`) + keepalive unit + pre-pull probe image. `--verify-gpu` proves the GPU. Does **not** start docker. |
| `cutover.sh` | The DD→native migration, **non-destructive**, canonical 6-step order, falsifiable single-owner check. |
| `validate-headless.sh` | The **GPU gate** (run from pc-serveur): proves `device==cuda`, dim 1536, within latency bound. |
| `heartbeat-check.sh` | pc-serveur cron probe: curls `GET /` for `device==cuda`. The **sole detector** of a silent unattended boot failure. |
| `rollback.sh` | Non-destructive revert to Docker Desktop. Images retained until `finalize.sh`. |
| `finalize.sh` | Run **only after N stable days**: `docker rmi` the DD images + delete the tarball. Point of no easy return. |
| `systemd/brain-embedding-keepalive.service` | `sleep infinity` unit that keeps the WSL VM alive. |
| `windows/.wslconfig` | Reference copy of `[wsl2] vmIdleTimeout=-1`. Goes to `C:\Users\arman\.wslconfig`. |
| `windows/boot.ps1` | Boot script: start distro, wait for `:8003`, refresh `netsh portproxy` for `:8003` only. |
| `windows/brain-embedding-boot.xml` | Task Scheduler task (at startup, run-whether-logged-on-or-not). No embedded credentials. |

Shared constants used everywhere below:

```
WSL distro      : Ubuntu-24.04
dev-pc LAN IP   : 192.168.1.11        port: 8003
containers      : brain_embedding_supervisor , brain_embedding_qodo
image tarball   : /var/backups/brain-embedding-images.tar   (SAVE on cutover / LOAD on rollback)
GPU-probe image : nvidia/cuda:12.4.1-base-ubuntu22.04@sha256:0f6bfcbf267e65123bcc2287e2153dedfc0f24772fb5ce84afe16ac4b2fada95
compose env     : GPU_MIN_FREE_MIB=4000  IDLE_TIMEOUT_SEC=900  (qodo dim=1536)
```

The probe uses the exact reference above. The two Compose images remain local build outputs;
their `build:` blocks and `pull_policy: build` prevent a registry image with the same local tag
from replacing them. Follow the [container image pin runbook](../../docs/CONTAINER_IMAGE_PINS.md)
for inventory, rotation and rollback.

---

## 1. Architecture (v2)

```
Windows 11 (dev-pc) — boots to LOGIN SCREEN, no logon required
└─ Task Scheduler "brain-embedding-boot"
   │   (Trigger: At system startup · "Run whether user is logged on or not"
   │    · as arman · highest privileges · batch-logon)
   └─ powershell -ExecutionPolicy Bypass -File ...\boot.ps1
        ├─ wsl -d Ubuntu-24.04           → boots distro, systemd becomes PID 1
        │                                  (held up by the keepalive unit below)
        ├─ wait until :8003 answers inside WSL (curl localhost:8003/healthz)
        └─ netsh portproxy refresh:  0.0.0.0:8003 → <wsl-ip>:8003
              (additive; deletes+adds the :8003 rule ONLY; never touches :8001/:9100)
              + firewall allow rule for TCP 8003

      Ubuntu-24.04 (systemd PID 1)
        ├─ docker.service  (native docker-ce, nvidia runtime, no-cgroups=true)
        │    └─ brain_embedding_supervisor  (restart=unless-stopped, publishes :8003)
        │         ├─ GPU probe: nvidia/cuda:12.4.1-base-ubuntu22.04@sha256:0f6bfcbf267e65123bcc2287e2153dedfc0f24772fb5ce84afe16ac4b2fada95 --gpus all
        │         └─ lazy: docker start brain_embedding_qodo  → GPU (device=cuda)
        └─ brain-embedding-keepalive.service  (sleep infinity)  ← keeps the VM alive

  .wslconfig: [wsl2] vmIdleTimeout=-1        (networkingMode UNCHANGED / default NAT)
```

**Historical request flow (superseded).** During the v2 cutover, `pc-serveur` consumers used
`EMBEDDING_SERVICE_URL=http://192.168.1.11:8003`; only the engine behind that IP:port changed
(Docker Desktop → native docker-ce). Do not restore that setting unless a new explicit cutover
has first revalidated the complete runbook.

**Networking — why NAT + portproxy, not mirrored.** Mirrored networking was
**rejected** (judge C5): WSL issues #10494/#10683/#13868 document that
`networkingMode=mirrored` frequently fails to bind container-published ports on the
Windows side — a silent unowned port on the brain's backbone. We keep **default NAT**
(no global `.wslconfig` blast radius on the `docker-desktop` distro or the box's
existing `:8001`/`:9100` services) and re-point a **single `:8003` `netsh portproxy`
rule** at the current WSL IP on every boot — exactly the model this box already uses
for `:8001`/`:9100`.

**Keepalive — why mandatory.** `wsl -d Ubuntu-24.04 -e /bin/true` exits instantly and
WSL reaps the idle distro (~8 s), killing systemd + docker. So the keepalive is
**two belts**: the always-on `sleep infinity` systemd unit **and** `vmIdleTimeout=-1`
in `.wslconfig`. Validated by **idling the box past the idle window**, not just by
quick reboots.

**Endpoint contract** (`:8003`, supervisor proxies to qodo):

| Method · Path | Owner | Returns | Wakes qodo? |
|---|---|---|---|
| `GET /` | qodo (proxied) | `{"device":"cuda"\|"cpu","cuda_available":bool,...}` | **Yes** |
| `POST /embed {"texts":[...]}` | qodo (proxied) | `[[float × 1536]]` | **Yes** |
| `GET /healthz` | supervisor **only** | 200 liveness — **no `device` field** | No |
| `GET /ready` | supervisor | 200 if qodo READY, else 503 | No |
| `GET /status` | supervisor | `{"state","last_request_ts","gpu_free_mib","target_container"}` | No |

> **The device trap (judge C1).** A 1536-float vector ALONE does **not** prove the GPU.
> Qodo silently falls back to CPU and still returns a perfectly-shaped 1536-d vector.
> `GET /healthz` carries **no** `device` field. **Only `GET /` exposes `device`/`cuda_available`.**
> So the gate and the heartbeat both curl **`GET /`**, never `/healthz`.

---

## 2. One-time migration

Run **in order**. Each step has a hard gate; do not skip ahead. This is the
`Operator cutover sequence` from the plan, expanded.

### 2.1 — Install native docker-ce + GPU runtime (WSL, alongside still-running Docker Desktop)

```bash
ssh arman@192.168.1.11
wsl -d Ubuntu-24.04          # land in Ubuntu-24.04

cd /home/.../brain_v42/deploy/dev-pc   # repo checkout inside WSL

# 1a. Install engine + nvidia toolkit (no-cgroups=true) + keepalive unit.
#     Idempotent; re-runnable. Does NOT start docker (DD still owns the socket).
./setup-docker-ce.sh

# 1b. Prove the GPU on BOTH paths (CLI --gpus all AND docker-py device_requests).
#     This transiently starts dockerd, runs the probes, prints PASS/FAIL per path.
./setup-docker-ce.sh --verify-gpu
```

**Both GPU paths must print PASS.** If either fails, **stop** — do not cut over; the
headless GPU path is unproven.

> ⚠ **Then stop docker and confirm it is inactive (judge N2).** `--verify-gpu`
> transiently started `dockerd` while Docker Desktop still owns `/var/run/docker.sock`.
> The two engines MUST NOT coexist on that socket. Before cutover:
> ```bash
> sudo systemctl stop docker docker.socket
> systemctl is-active docker          # MUST print: inactive
> ```
> `cutover.sh` step 0 asserts this; if you skip it, cutover aborts loudly (correct).

### 2.2 — Install the Windows boot artifacts

From a Windows shell (PowerShell / cmd) on the dev-pc, or via SSH into Windows:

```powershell
# 2a. Keepalive: copy the reference .wslconfig into the user profile.
copy <repo>\deploy\dev-pc\windows\.wslconfig  C:\Users\arman\.wslconfig
# (If a .wslconfig already exists, merge: keep [wsl2] vmIdleTimeout=-1,
#  do NOT add networkingMode — it stays default/NAT.)

# 2b. Headless boot task. The committed XML carries NO credentials.
#     Supply arman's password interactively with /rp * (you are prompted; nothing
#     is stored in the repo). schtasks writes the stored password into the task.
schtasks /create /tn "brain-embedding-boot" ^
  /xml <repo>\deploy\dev-pc\windows\brain-embedding-boot.xml ^
  /ru arman /rp *
```

See **§4 Credential lifecycle** before doing this — `arman` needs a **non-empty
password** and the **"Log on as a batch job"** right, or the task silently fails to run
at boot.

### 2.3 — Cutover (WSL) — canonical 6-step order

`cutover.sh` enforces this order with precondition asserts and **aborts loudly** if run
out of order. **Non-destructive**: it only removes the DD *containers* — the DD *images*
are saved to a tarball and retained as the rollback artifact.

```bash
# back in: ssh arman@192.168.1.11  →  wsl -d Ubuntu-24.04
cd /home/.../brain_v42/deploy/dev-pc

# Pre-cutover verify: the compose image names are PINNED (image: brain-embedding-*:local),
# so confirm the live Docker Desktop images match what compose resolves to. The two lists
# must agree, or cutover SAVE would tarball/rebuild the wrong images.
docker compose -f docker-compose.yml config --images   # prints brain-embedding-supervisor:local + -qodo:local
docker images | grep embedding                         # the live DD images must match those names

./cutover.sh            # interactive; pauses at the manual integration-off gate
# (./cutover.sh --yes   for non-interactive once you trust it)
```

What it does, in order (mirrors the plan's canonical order):

```
0. PRECONDITIONS  native docker INACTIVE (systemctl is-active docker == inactive)
                  AND DD owns the socket (docker info OperatingSystem == "Docker Desktop").
                  Abort loudly otherwise.
1. SAVE           build images under DD if missing; docker save supervisor+qodo
                  → /var/backups/brain-embedding-images.tar
                  Proven restorable: `tar tf` lists both manifests + a throwaway
                  `docker load` succeeds (never a size-only check).
2. STOP (≠ remove) docker rm -f the two DD CONTAINERS only; assert DD engine now shows
                  ZERO brain_embedding_*; persist that proof log. IMAGES RETAINED.
3. INTEGRATION OFF *** MANUAL, GATED *** — cutover pauses and waits for you. See below.
4. NATIVE UP      systemctl enable --now docker; assert engine != "Docker Desktop";
                  docker load < tarball; docker compose up -d --no-build supervisor
                  (+ qodo --no-start, supervisor-controlled).
5. INVARIANT      host-level + falsifiable: exactly one OWNER ENGINE — every :8003
                  listener PID belongs to native dockerd (IPv6 dual-stack yields one
                  docker-proxy per family; all must be dockerd's). Logged.
6. CLEANUP        disable DD auto-start; then cutover PROMPTS YOU to run `wsl --shutdown`
                  next — it does NOT run it itself (that would kill its own WSL session).
                  Run `wsl --shutdown` yourself (it bounces DD too — harmless, the next
                  step is the no-login reboot anyway).
```

> **Step 3 — the one manual action (judge H4).** When `cutover.sh` pauses, in the
> **Docker Desktop GUI** on the dev-pc:
> **Settings → Resources → WSL integration → turn OFF `Ubuntu-24.04`**, Apply & restart.
> This releases `/var/run/docker.sock` so native `dockerd` can own it. Then return to
> the terminal and confirm — cutover verifies the old socket is released before going on.

After `cutover.sh` exits clean (invariant PASS), proceed to **§3 Validation gate**.

---

## 3. Validation gate (make-or-break)

This gate exists because the whole migration is about **boot-with-nobody-logged-in**.
The only way to trust it is to test exactly that.

> ⚠ **REBOOT the dev-pc and do NOT log in.** Leave it at the Windows login screen.
> Everything below is run **from pc-serveur**, never by logging into the dev-pc.

```bash
# On pc-serveur:

# (a) Isolate GPU-PV FIRST. This proves the kernel-level GPU passthrough works at
#     "session 0" (no logon) — separating "headless GPU-PV" from "nvidia runtime in
#     the container". Must list the RTX 5070 Ti:
ssh arman@192.168.1.11 wsl -d Ubuntu-24.04 -e /usr/lib/wsl/lib/nvidia-smi -L

# (b) Only if (a) shows the GPU — FORCE a cold start FIRST. The gate runs from
#     pc-serveur and CANNOT stop qodo itself, so the OPERATOR stops it over SSH so the
#     next GET / triggers the supervisor's separate GPU-probe path (probe → qodo start):
ssh arman@192.168.1.11 wsl -d Ubuntu-24.04 -e docker stop brain_embedding_qodo

# (c) THEN run the gate from pc-serveur. Its first GET / triggers the genuine cold start
#     and is TIMED. The gate does NOT stop qodo — you just did (step b):
#       /healthz == 200
#       GET /     → device=="cuda" AND cuda_available==true   (NOT just a 1536 vector)
#       POST /embed {"texts":["gate"]} → outer 1, inner 1536, within warm-latency ceiling
#       times boot→first-embed (GET / wake + first /embed) and fails if over the bound
#       on 503, prints supervisor /status (state, gpu_free_mib) for diagnosis
cd /home/.../brain_v42/deploy/dev-pc
./validate-headless.sh
```

**Decision:**
- **`HEADLESS-GPU GATE: PASS`** → headless is trustworthy. Proceed to §5 heartbeat,
  then §6 daily-ops smoke tests. Do **not** run `finalize.sh` yet (keep rollback open).
- **`HEADLESS-GPU GATE: FAIL`** (or step (a) shows no GPU, or `device==cpu`) → **do not
  trust headless.** Run `./rollback.sh` (§7). Escalate. Evaluate the
  AutoAdminLogon-to-locked fallback **only with explicit operator sign-off** — it stores
  a **cleartext registry password** (a security regression), so it is a last resort.

After PASS, confirm end-to-end from a live Claude session that `brain_search` returns
**semantic** results (not the FTS-only fallback).

---

## 4. Credential lifecycle (read before §2.2 / after any password change)

The boot task runs **"whether the user is logged on or not"**, which means Windows must
authenticate `arman` non-interactively at boot using a **stored** credential.

**Preconditions for boot to work (judge H2):**
1. **`arman` has a NON-EMPTY password.** A blank password cannot be used for a stored
   batch logon — the task silently does nothing at boot.
2. **`arman` holds the "Log on as a batch job" right.**
   `secpol.msc → Local Policies → User Rights Assignment → Log on as a batch job` → add
   `arman` (or the relevant group). Without it, Task Scheduler refuses to run the task
   under stored credentials.
3. **No credentials live in the repo.** `brain-embedding-boot.xml` carries none; the
   password is supplied once, interactively, via `schtasks ... /rp *` and stored by
   Windows in the task's credential vault.

> ⚠ **A password change RE-BREAKS boot.** When `arman`'s Windows password is rotated,
> the task's stored credential goes stale and the embedding **silently fails to start at
> the next reboot**. After ANY password change you MUST re-register the credential:
> ```powershell
> schtasks /change /tn "brain-embedding-boot" /ru arman /rp *
> ```
> Then do a no-login reboot and re-run the §3 gate. The **§5 heartbeat is what catches
> you** if you forget this — it is the only signal that an unattended boot went silent.

---

## 5. Heartbeat (deployed WITH cutover — not later)

A silent 3 a.m. headless-boot failure is the **exact fault this migration exists to
fix** — so it must page, not go unnoticed. The heartbeat ships **with** cutover (judge
N1/H2), the same day, on **pc-serveur**.

```bash
# On pc-serveur (as hawixs), install heartbeat-check.sh somewhere stable and add a cron
# entry. It curls GET /  (NOT /healthz — /healthz carries no device field) and exits
# non-zero + emits an ALERT line on stderr on: non-200, device != cuda, or unreachable.

# Example cron (every 5 min). The 'OK' line (stdout) is logged; the ALERT line (stderr)
# is deliberately NOT redirected, so on a non-zero exit cron mails it to MAILTO.
MAILTO="ops@example.invalid"   # set to a real address; pc-serveur needs a working MTA
*/5 * * * * /home/hawixs/hawkixs_infra/git_repo/brain_v42/deploy/dev-pc/heartbeat-check.sh >> /var/log/brain-embedding-heartbeat.log
```

> ⚠ **Do NOT append `2>&1`** — that swallows stderr so the ALERT never reaches cron's
> mailer and MAILTO never fires. A **working MTA** (sendmail/postfix/etc.) **and** a real
> `MAILTO` must be configured on pc-serveur, or no page is delivered.

This **MUST run on pc-serveur as `hawixs`**, never on the dev-pc: a dead WSL on the dev-pc
would also kill a dev-pc-hosted check, so the heartbeat lives on a separate host to detect
exactly that failure. The heartbeat reuses `validate-headless.sh`'s assertion core
(`GET /` → `device==cuda` + 200). It is the **only detector** of a silent unattended boot
failure (e.g. a stale password per §4, or a GPU-PV regression at session 0).

> red-monitor / red-alerts integration is a **later enrichment**, not the baseline. The
> cron + log + non-zero exit is the day-one tripwire and must be live before the first
> unattended reboot.

---

## 6. Single-owner invariant

**Exactly one engine** holds `brain_embedding_*` and **exactly one process** listens on
host `:8003` — at all times after cutover (R5, the operator's explicit requirement).

**How to verify (host-level, falsifiable — judge H1):**

```bash
ssh arman@192.168.1.11 wsl -d Ubuntu-24.04 -e bash -c '
  # Exactly one listener on :8003 ...
  ss -ltnp "sport = :8003"
  # ... and its PID must be a child of NATIVE dockerd (not Docker Desktop proxy).
  systemctl is-active docker            # active
  docker info --format "{{.OperatingSystem}}"   # must NOT be "Docker Desktop"
'
```

- Exactly **one** `:8003` listener, owned by native `dockerd` → invariant holds.
- "DD unreachable == clean" is **not** acceptable proof — that is unfalsifiable. The DD
  side was already proven clean during cutover step 2 (zero `brain_embedding_*` on the
  live DD engine, proof persisted).

**Docker Desktop auto-start MUST be OFF.** Otherwise, after a human eventually logs in,
DD could spin a second stack. In the Docker Desktop GUI:
**Settings → General → uncheck "Start Docker Desktop when you sign in"** → Apply. After
the first post-cutover human login, re-run the invariant check above to confirm DD did
not silently bring up a competing stack.

---

## 7. Daily operations

### 7.1 — Disable embedding to reclaim the GPU (gaming)

The gaming-disable toggle is preserved (R4). From **pc-serveur**, even at the dev-pc
login screen:

```bash
ssh arman@192.168.1.11 wsl -d Ubuntu-24.04 -e docker stop brain_embedding_qodo
```

This frees the ~5 GiB of VRAM. The **supervisor stays up**; the next `brain_*` request
(or any `GET /` / `POST /embed`) **auto-wakes** qodo, provided the GPU has
`GPU_MIN_FREE_MIB` (4000) free. No re-enable command needed — auto-wake is the default.

> The supervisor is unchanged from v1: idle-stop after `IDLE_TIMEOUT_SEC` (900 s),
> GPU-headroom gate, manual-stop → auto-wake. R4 is preserved for free.

### 7.2 — Observability

| Endpoint | What it returns | Use |
|---|---|---|
| `GET /healthz` | Supervisor liveness (200 if up) — **no device field** | "Is the supervisor alive?" |
| `GET /ready` | 200 if qodo READY, 503 otherwise | "Is the GPU model loaded right now?" |
| `GET /status` | `{state, last_request_ts, gpu_free_mib, target_container}` | Idle/VRAM diagnosis |
| `GET /` | `{device, cuda_available, ...}` (proxied to qodo — **wakes it**) | "Is it really on the GPU?" |

```bash
# Quick triage from pc-serveur (does NOT wake qodo):
curl -s http://192.168.1.11:8003/healthz
curl -s http://192.168.1.11:8003/status

# Prove GPU (WAKES qodo):
curl -s http://192.168.1.11:8003/
```

If `GET /` ever reports `"device":"cpu"`, the GPU path has regressed — treat it as an
incident (qodo is silently on CPU and slow). See the `debug-brain-v42-embedding-500`
skill for the 500-on-long-payload / VRAM-saturation playbook.

### 7.3 — Upgrade / redeploy (native)

Both services are repository-built local exceptions with `pull_policy: build`. Build them from
the checked-out commit before the `--no-build` start below; do not substitute registry images.

```bash
ssh arman@192.168.1.11
wsl -d Ubuntu-24.04
cd /home/.../brain_v42/deploy/dev-pc
docker compose build
docker compose up -d --no-build --no-start --force-recreate embedding-qodo
docker compose up -d --no-build --force-recreate embedding-supervisor
```

The first `up` replaces qodo with the newly built image but deliberately leaves it
stopped; the supervisor remains the only component allowed to start it lazily. Both
commands disable implicit builds, so the images used are exactly those produced by
the preceding `docker compose build`.

---

## 8. Rollback (non-destructive)

`rollback.sh` reverts to the Docker Desktop stack. It is **self-contained** and does not
assume the DD images still exist:

```bash
ssh arman@192.168.1.11
wsl -d Ubuntu-24.04
cd /home/.../brain_v42/deploy/dev-pc
./rollback.sh
```

What it does:
1. Stops the native stack (`brain_embedding_supervisor` + `brain_embedding_qodo`),
   `systemctl stop docker`.
2. If `finalize.sh` already removed the DD images, `docker load < /var/backups/brain-embedding-images.tar`
   back into Docker Desktop. (If you have **not** finalized, the DD images were retained
   by cutover — this load is a no-op / skipped.)
3. Prints the manual step to **re-enable Docker Desktop's `Ubuntu-24.04` WSL integration**
   (Settings → Resources → WSL integration → turn ON `Ubuntu-24.04`).
4. Verifies `:8003` is served by Docker Desktop again.

Because cutover only removed *containers* and retained *images* (and saved a tarball),
rollback returns the box to today's pre-migration state with no rebuild.

---

## 9. Finalize (only after N stable days)

`finalize.sh` is the **point of no easy return**. Run it **only** after the headless path
has survived: the §3 gate, the §6 invariant, gaming-disable + auto-wake, idling past the
WSL idle window, **and a second no-login reboot** — for **N stable days**.

```bash
ssh arman@192.168.1.11
wsl -d Ubuntu-24.04
cd /home/.../brain_v42/deploy/dev-pc
./finalize.sh
```

It `docker rmi`s the retained DD images and deletes `/var/backups/brain-embedding-images.tar`.
**After finalize, rolling back to Docker Desktop requires a full image rebuild** (the
tarball is gone) — `rollback.sh` will rebuild rather than `docker load`. Keep Docker
Desktop installed but with **integration off and auto-start off** regardless.

---

## 10. End-to-end operator checklist (post-merge, gated)

1. WSL: `setup-docker-ce.sh` → `setup-docker-ce.sh --verify-gpu` (both PASS) → **stop
   docker, confirm `systemctl is-active docker` == inactive**.
2. Windows: copy `.wslconfig`; install boot task (`schtasks /create ... /xml ... /rp *`).
   Verify §4 credential preconditions.
3. WSL: `cutover.sh` (canonical order; manual integration-off gate; invariant PASS).
4. After `cutover.sh` exits (it **prompts** you to run `wsl --shutdown`), run
   `wsl --shutdown` yourself, then **reboot the dev-pc, do NOT log in.**
5. From pc-serveur at the login screen: (a) `ssh ... /usr/lib/wsl/lib/nvidia-smi -L`
   shows the GPU; (b) **force a cold start** —
   `ssh arman@192.168.1.11 wsl -d Ubuntu-24.04 -e docker stop brain_embedding_qodo`;
   (c) `validate-headless.sh` → **PASS** (device=cuda, dim 1536, GET /+embed in bound).
6. Confirm `brain_search` works from a live Claude session (semantic, not FTS).
7. **Deploy the heartbeat NOW** (§5): `heartbeat-check.sh` + cron on pc-serveur.
8. Gaming-disable test (login screen): SSH `docker stop brain_embedding_qodo` → next
   request auto-wakes.
9. Idle the box past the WSL idle window (no login, no traffic) → `:8003` still answers
   (proves keepalive).
10. **Second** no-login reboot → gate PASS again (proves persistence).
11. Leave DD installed (integration off, auto-start off, images retained) **N stable
    days**, then `finalize.sh`.

**If step 5 FAILs:** do not trust headless. `rollback.sh`. Escalate; evaluate the
AutoAdminLogon-to-locked fallback only with operator sign-off (cleartext-registry
password = security regression).
```
