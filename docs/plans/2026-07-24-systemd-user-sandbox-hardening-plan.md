# Sandboxing systemd user units — implementation plan

**Date:** 2026-07-24
**Brain ticket:** `1460c46c-0386-44b9-bbed-9ce45c3c5483`
**Branch:** `feat/systemd-sandbox-hardening`
**Status:** code-ready, three final SHIP reviews, not deployed
**Deployment:** forbidden in this batch

## Objective

Reduce the exploitation surface of the systemd user units managed by brain-v42 without
breaking their application contracts. The batch must also remove two false assurances: automation's
current filesystem profile is not effective under systemd 249 without `PrivateUsers=true`, and
`install.sh --dry-run`, despite its name, publishes units into the user systemd directory.

The expected delivery is **code-ready only**: templates, isolated rendering, tests, documentation
and runbooks. No file under `~/.config/systemd`, `daemon-reload`, enable, start, stop, restart,
timer, or live secret will be modified.

## Established facts

- The host runs systemd 249 and the units concerned are `--user` services.
- The local manual specifies that protections requiring a mount namespace, including
  `ProtectSystem`, `ProtectHome`, `PrivateTmp` and `ReadWritePaths`, only become usable in
  a user unit with `PrivateUsers=true`.
- The kernel allows user namespaces and their nesting, but this does not prove the
  compatibility of a real Codex/Claude run under Dream.
- The observed runtime loads:
  - MCP HTTP active without a sandbox;
  - Dream inactive between two runs, without a sandbox;
  - graph-recon inactive and never proven, without a sandbox;
  - automation inactive with filesystem directives configured but without `PrivateUsers`.
- MCP HTTP listens on loopback and reaches out to PostgreSQL, Neo4j and the embedding/reranking
  services. It reads dynamic plan paths and can write configured `CLAUDE.md` sections;
  this latter behavior must not be silently broken by this batch.
- Graph-recon does not write to the filesystem: it reads the repo and `.env`, then writes only
  to PostgreSQL/Neo4j. Its login shell is unnecessary because the interpreter is absolute and `Settings`
  loads `.env` from `WorkingDirectory`.
- The user manager does not provide `network-online.target`. Dream and graph-recon nonetheless
  keep `After/Wants=network-online.target`: this relation orders nothing. Dream already has
  an explicit MCP preflight; graph-recon must fail normally if its databases are unavailable.
- Dream writes under `logs/dream`, uses the temporary runtime, launches `uv`, Python, Codex and a
  Claude rollback, and depends on caches/authentications under HOME. Its child sandbox can create
  namespaces, use Landlock and seccomp.
- The watchdog launches `curl` then `systemctl --user restart`; it remains an executable unit and must
  receive at least a reduced profile compatible with the user bus.

## Profile decisions

### Strong integrity baseline: automation and graph-recon

These Python services have no sandboxed subprocess and no legitimate filesystem write:

```ini
UMask=0077
NoNewPrivileges=true
PrivateUsers=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=read-only
ProtectClock=true
ProtectControlGroups=true
ProtectKernelLogs=true
ProtectKernelModules=true
ProtectKernelTunables=true
CapabilityBoundingSet=
AmbientCapabilities=
KeyringMode=private
LockPersonality=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
RestrictRealtime=true
RestrictSUIDSGID=true
SystemCallArchitectures=native
```

Graph-recon switches to a direct Python `ExecStart`. No `ReadWritePaths` is added: outputs
stay in journald. `ProtectHome=read-only` protects integrity, not confidentiality: the
same user's SSH keys, agent credentials and other repos remain readable, and the allowed
network can exfiltrate them. A future `ProtectHome=tmpfs` profile with minimal binds must first
resolve the actual interpreter targeted by the `.venv/bin/python` symlink.

### Strong baseline compatible with HOME writes: MCP HTTP

MCP receives the same baseline, but `ProtectSystem=full` replaces `strict` and `ProtectHome` is omitted.
It adds `ReadOnlyPaths=__REPO_ROOT__/.env %h/.config/brain-v42` to prevent modification of the
Brain configurations and credentials. This choice preserves the configured `CLAUDE.md` writes and
the dynamic project paths. It makes `/usr`, `/boot`, `/efi`, `/etc`, `.env` and the Brain
configuration read-only, isolates `/tmp` and the devices, strips capabilities and bounds the socket
families, but **does not confine the rest of HOME**. The server-side bounding of the scan and write
roots remains the SEC1c residual; the documentation will not present this profile as protecting
the confidentiality of user secrets.

### Reduced candidate, not deployable before canary: Dream and watchdog

These units receive only the restrictions that do not require a mount namespace:

```ini
UMask=0077
NoNewPrivileges=true
LockPersonality=true
RestrictRealtime=true
RestrictSUIDSGID=true
SystemCallArchitectures=native
```

Dream does not yet receive `PrivateUsers`, `PrivateTmp`, `ProtectSystem`, `ProtectHome`,
`RestrictNamespaces`, `MemoryDenyWriteExecute`, an empty `CapabilityBoundingSet`, a
`SystemCallFilter` filter, or `RestrictAddressFamilies`. These directives require a real Codex
**and** Claude canary and/or risk breaking the nested sandbox, Node/libuv, the caches, token refresh,
or the shared lock. The watchdog, for its part, adds `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`:
loopback curl and the `systemctl --user` bus need no other family.

### Deferred filters

`SystemCallFilter=@system-service`, `SystemCallFilter=~@mount ...` and
`RestrictNamespaces=true` are not added in this batch. `systemd-analyze verify` cannot
prove their compatibility with asyncpg, Neo4j, uv, Codex, or Claude. They can only be enabled
after a real trace/canary and a known-good rollback.

## Rendering and verification without live publication

Add `install.sh --check-only` with the following invariants:

- incompatible with `--dry-run` and `--uninstall`;
- renders the eight managed files in a bounded `mktemp` under `/tmp`;
- first runs the host-aware preflights without printing their values, then separately launches the
  verifier **exactly once on the eight files** with empty `HOME` and temporary XDG values, a
  `SYSTEMD_UNIT_PATH` limited to the staging area and the five vendor unit directories, as well as
  `--generators=no --man=no`;
- does not create `USER_UNIT_DIR`, does not inspect live units, does not publish any file, and
  never calls `systemctl`;
- cleans up its temporary directory through an explicitly validated path;
- explicitly logs `check-only: no managed units changed`.

Also add `install.sh --render-dir /absolute/new/path`:

- incompatible with the three other modes;
- requires an existing parent, a directory, owned by the user; resolves that parent with
  `realpath -e`, requires `u+wx`, refuses `g+w`/`o+w`, any symlinked component, as well as a
  canonical target under `USER_UNIT_DIR`, and refuses a relative, existing, or symlinked target;
  every ancestor container must belong to the effective root UID or the current user, with
  safe sticky semantics when it is writable;
- applies the same rendering, the same host-aware preflights, and the same hermetic verification;
- creates a sibling private staging area, requires exactly eight regular files with no placeholder,
  then publishes via a single `mv -T --no-clobber` to the final target with mode `0700`; the
  `device:inode` identity of the parent, the staging area, and the rendering is kept and revalidated;
- on failure or signal, removes only a target that still carries the rendering's identity; a
  concurrently replaced target is preserved and flagged, and the staging area is cleaned up only
  if it still holds its own identity;
- publishes this way **into this output directory only**, without reading or writing a live unit and
  without `systemctl`;
- thereby provides an inspectable artifact whose operator can install exactly one exact basename.

Both modes fail closed if `systemd-analyze` is absent or if a single file fails
`verify`: nonzero return, staging cleaned up, and no `--render-dir` published.

The cleanup never does a `stat` followed by `rm -rf` on the public target: it first moves it back by
atomic rename into the staging area's private slot, verifies its identity, and restores any
concurrent replacement. The private parent and `/tmp`'s sticky bit protect against other
UIDs. This Bash contract does not claim isolation against a hostile process of the **same UID**,
which can still race the path-based operations; that boundary would require a helper built on
`dirfd`, `renameat2(RENAME_NOREPLACE)`, and `unlinkat`.

The historical behavior of `--dry-run` remains compatible: rendering and atomic publication into
`USER_UNIT_DIR`, without `systemctl`. The runbooks must stop describing it as side-effect-free
and use `--check-only` for an ephemeral verification or `--render-dir` to prepare a
rollout artifact outside the systemd directory.

A live-publication selector remains out of this batch. The operator window will need to back up the
fragments/drop-ins, produce a `--render-dir`, then atomically copy a single validated basename and
canary graph, MCP, and Dream separately. Normal installation and `--dry-run` must not be
used as a blind global rollout.

## TDD cycle

### RED 1 — user namespace consistency

Create a central contract for the `.service.tmpl` templates that fails if a unit contains a
namespace/mount directive (`PrivateTmp`, `PrivateDevices`, `PrivateMounts`, `ProtectSystem`,
`ProtectHome`, `ProtectClock`, `ProtectControlGroups`, `ProtectKernelLogs`,
`ProtectKernelModules`, `ProtectKernelTunables`, `ProtectProc`, `ProcSubset`, `ReadWritePaths`,
`ReadOnlyPaths`, `InaccessiblePaths`, `BindPaths`, `BindReadOnlyPaths`, `TemporaryFileSystem`,
`NoExecPaths`, `ExecPaths`, `PrivateNetwork`, `PrivateIPC`, `ProtectHostname`, `RootDirectory`,
`RootImage`, `MountAPIVFS`, `MountImages`, `ExtensionImages`) without `PrivateUsers=true`. The test
must fail on automation.

### RED 2 — exact profiles per unit

Test the four main services and the watchdog:

- single occurrence of each directive;
- no `false` value or contradictory override;
- exact integrity baseline for automation/graph;
- `ProtectSystem=full` baseline without `ProtectHome` and with the Brain config read-only for MCP;
- exact reduced baseline and explicit absence of the deferred directives for Dream/watchdog;
- network families limited to `AF_UNIX AF_INET AF_INET6` for the strong profiles and the watchdog,
  but explicitly deferred for Dream;
- graph-recon uses the absolute interpreter without `/bin/bash -lc`; Dream and graph-recon no longer
  claim the `network-online.target` system target absent from the user manager.

### RED 3 — real check-only and isolated render-dir

Extend the installer tests with a fake HOME/XDG and hostile wrappers:

- `returncode == 0`, exact success message, and no `systemctl` called;
- `USER_UNIT_DIR` remains absent and a hostile sentinel is neither read nor scanned;
- the eight exact basenames exist at verify time, with no placeholder, then the check-only
  staging area is removed;
- the verifier's wrapper receives the temporary `HOME`/XDG, the isolated systemd path, and the
  no-generator/no-man options; a fixture secret-sentinel value is absent from stdout/stderr on
  both success and every error;
- flag combinations are rejected before any mutation;
- success, render error, verify error, `INT`, and `TERM` clean up the staging area without touching
  existing units; the absence of `systemd-analyze` fails nonzero before any publication;
- `--render-dir` also refuses a parent symlinked to `USER_UNIT_DIR`, renders into a sibling staging
  area then atomically renames; the final target remains absent after an error, `INT`, or `TERM`,
  and success preserves exactly the eight validated artifacts;
- the historical `--dry-run` path remains covered separately.

### Minimal GREEN

Modify only the five templates, `install.sh`, the contract tests, and the runbooks.
Do not modify the MCP/Dream application code, the secrets, or the live units.

## Expected files

- `deploy/systemd/brain-mcp-http.service.tmpl`
- `deploy/systemd/brain-mcp-http-watchdog.service.tmpl`
- `deploy/systemd/brain-v42-automation.service.tmpl`
- `deploy/systemd/brain-v42-dream.service.tmpl`
- `deploy/systemd/brain-v42-graph-recon.service.tmpl`
- `deploy/systemd/install.sh`
- `deploy/systemd/README.md`
- `deploy/systemd/MCP_HTTP_RUNBOOK.md`
- `tests/unit/deploy/test_systemd_sandbox_profiles.py` (new)
- existing installer tests to the strict necessary
- `tests/integration/test_dream_systemd_install.sh`
- roadmap and this plan for the final proof

## Gates before merge

```bash
pytest -q \
  tests/unit/deploy/test_systemd_sandbox_profiles.py \
  tests/unit/deploy/test_mcp_http_unit.py \
  tests/unit/deploy/test_automation_unit.py \
  tests/unit/deploy/test_systemd_ci_portability.py

bash -n deploy/systemd/install.sh
shellcheck deploy/systemd/install.sh tests/integration/test_dream_systemd_install.sh
REQUIRE_SYSTEMD_ANALYZE=1 bash tests/integration/test_dream_systemd_install.sh
ruff check .
ruff format --check .
mypy src/
pytest -q
git diff --check
```

The smoke shell script must call `--check-only` in its HOME/XDG fixture and verify the eight artifacts
with the real systemd parser. The verifier may perform read-only system reads; the
guarantee is the absence of local HOME/XDG lookup, lifecycle mutation, and any `systemctl`.

## Deferred runtime proof

The ticket stays `in_progress` after merge until an operator window has proven, unit by
unit:

1. persistent backup of the fragment and the known-good drop-ins;
2. from the canonical production checkout at the validated SHA, `--check-only` rendering, then
   `--render-dir`; copy of a single basename via a `.new` file and an
   atomic `mv` into `USER_UNIT_DIR`, `systemctl --user daemon-reload`, without global activation
   of the timers;
3. properties declared by the manager via `systemctl --user show` and heuristic score
   `systemd-analyze security`; these two readings are never presented as proof
   of enforcement on the process;
4. proof of enforcement without a secret: for the strong profiles, a transient probe carrying the
   exact directives of the rendered fragment, write allowed in a fixture and refused
   out of scope; for the strong profiles and the watchdog, an exotic socket family refused. On any
   restarted persistent process, also check `NoNewPrivs: 1` in `/proc/$PID/status` and the
   expected read-only mounts in `/proc/$PID/mountinfo`. Dream logs these protections
   as residual until its dedicated canary;
5. graph-recon in report mode without `--fix` before any mutating run;
6. MCP: first neutralize the watchdog, capture the old `MainPID`, publish the fragment,
   `daemon-reload`, then `restart`; require a new, nonzero, and different `MainPID` before any
   validation. Then check the point-4 enforcement proofs, health, the authenticated read and
   write calls, DB/Neo4j/embedding, and the write of a backed-up/restored fixture `CLAUDE.md`.
   Any failure triggers an immediate rollback before rearming the watchdog;
7. Dream does not start its full unit: `dream.sh --dry-run` retains mutating paths.
   Directly canary `codex_runner`, then the separate Claude path, under a transient unit with the
   same profile and with read-only MCP tools. The full canary remains blocked until a
   real application mode without `--live` scrub, WET EXTRACT/ROADMAP, or alert; automation is validated
   against its lease/cutover contract;
8. matching rollback: MCP restores/reloads/restarts then health; Dream/graph restore before the
   next timer without relaunching the oneshot; automation respects the lease; the watchdog is restored
   then probed without triggering a restart. Never use `install.sh --uninstall`.

## Code-ready exit criteria

- The versioned profiles are exact, differentiated, and do not claim more than their guarantees.
- Every filesystem protection of a user unit requires `PrivateUsers=true`, enforced by test.
- `--check-only` publishes nothing; `--render-dir` only outputs verified artifacts outside systemd.
- The full gates and three independent reviews conclude `SHIP` with no P0–P3 finding.
- The Brain ticket receives the commits, tests, and limitations; it stays open until runtime proof.

## Code-ready proof from July 24, 2026

- Batch commits: plan `dea89c9`, RED contracts `4c1a650`, GREEN implementation `0e29044`;
  the final documentation commit carries the runbooks, the architecture, and the roadmap.
- Initial RED: 56 contracts, 35 expected failures and 21 passes; five additional regressions
  were then added from the review findings (live symlink, permissions/ancestry,
  cleanup, concurrent replacement, and exit failure after publication).
- Final GREEN: 61/61 profile + installer contracts, 228/228 deploy tests and systemd 249 smoke
  with 29 green checks; only the user manager probe is skipped for lack of an available bus.
- Repo gates in the CI Python 3.12.12 environment: Ruff over 600 files, Mypy over 162
  files, 6,114 tests passed and 298 skipped; Bash syntax, ShellCheck, and `git diff --check`
  green.
- Three independent reviews (profiles, security/TOCTOU, and quality/compatibility) conclude
  `SHIP`, with no P0, P1, P2, or P3 finding.
- No live fragment, drop-in, secret, timer, systemd manager, service, database, or container was
  modified. The ticket stays `in_progress` until the canaries and operator enforcement proofs.
- Separate maintenance diagnostic: Python 3.14 accepts the nested JSON used by a SEC2
  contract where Python 3.12 rejects it by recursion. CI 3.12 stays green; this compatibility
  must not be fixed within the systemd batch.
