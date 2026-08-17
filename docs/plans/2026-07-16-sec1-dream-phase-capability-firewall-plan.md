---
title: "SEC1a — Dream phase capability firewall"
status: completed
completed_at: "2026-07-16"
summary: "Bind each Codex Dream phase to a server-authenticated token profile and enforce its exact MCP tool allowlist without activating the policy on the live fleet."
tags:
  - sol-ultra
  - sec1
  - dream
  - authorization
  - capability
  - pattern-auto
---

# SEC1a — Dream phase capability firewall

## Goal

Move Dream's phase tool allowlists from a client-only Codex setting to a server boundary. A
token issued for one Codex Dream phase may list and call only that phase's exact tools. The
server denies cross-phase and destructive calls before tool code runs, logs a secret-safe
audit event, and preserves the unrestricted operator token.

This delivery is the first bounded slice of SEC1 in the
[Sol Ultra roadmap](2026-07-11-sol-ultra-audit-roadmap-plan.md). SEC1 remains open after this
slice. Object-level project ownership, filesystem-root enforcement, and negative
cross-project fixtures need repository-aware authorization because several current tools
accept only entity IDs.

## Starting evidence

- `scripts/dream/codex_runner.py` defines six exact `PHASE_TOOL_ALLOWLISTS` and gives them to
  Codex through `enabled_tools`.
- The runner sends `X-Brain-Agent: dream-codex-<phase>`, but the server does not authenticate
  that identity.
- `BearerTokenGuard` accepts one global `MCP_HTTP_TOKEN`; any holder can call every tool.
- `tool_catalog.py` states that catalog shape is presentation, not authorization. Its compact
  gateway must never become a capability bypass.
- FastMCP 3.4.2 exposes public token-verifier, transform, and `tools/list` / `tools/call`
  middleware hooks. The implementation uses those hooks instead of parsing JSON-RPC bodies.
- The live timer invokes `python3 -m scripts.dream.codex_runner`, although `/usr/bin/python3`
  cannot import the package's `src/` modules. The revised runner invocation must use the same
  `uv run python` environment already used by the rest of `dream.sh`.
- The HTTP unit reads the repository `.env`; the Dream unit reads
  `%h/.config/brain-v42/mcp-token.env`. The latter becomes the shared, private capability
  source and is added to the HTTP template after `.env`.

## Acceptance criteria

1. One immutable policy defines the six current phase allowlists. The Codex runner, tool
   catalog, and server authorization import it; no duplicate phase matrix remains.
2. `BRAIN_DREAM_CAPABILITY_ENFORCEMENT=false` is the safe default. Disabled mode preserves
   the current global bearer behavior, Codex command, and next scheduled Dream run.
3. Enabled mode reads a secret registry keyed by `<project_key>:<phase>`. Each project in the
   registry must define all six phases.
4. Each profile declares exactly one `active` token and zero or more distinct `accepted`
   tokens. The runner uses `active`; the server accepts both sets. This supports overlap during
   rotation without an ambiguous selection rule.
5. Registry validation uses `rsplit(":", 1)` because canonical project keys may contain
   colons. It rejects unknown phases, non-canonical aliases, incomplete project matrices,
   empty or non-string tokens, duplicate tokens, and collisions with the admin token.
6. Errors, validation output, reprs, and logs never render the admin token, a profile token,
   or the raw registry.
7. An application-owned `DreamCapabilityTokenVerifier`, derived from FastMCP's public
   `TokenVerifier`, compares opaque tokens in constant time and returns immutable agent,
   phase, and project claims. It does not use FastMCP's development-only static verifier.
8. An admin principal keeps the current compact/native catalog behavior. A scoped principal
   always receives a native catalog filtered to its phase, regardless of missing, compact,
   native, or forged presentation headers.
9. Authorization middleware denies any scoped `tools/call` outside the authenticated phase
   allowlist. It denies `brain_call_tool` and other compact gateways before their handlers run.
10. Each denial logs principal, phase, project, requested tool, and reason. It omits arguments,
    content, Authorization headers, and token material.
11. Enabled mode makes the Codex child environment from an allowlisted copy: it replaces the
    inherited admin token with the profile's `active` token and removes
    `MCP_HTTP_DREAM_TOKENS`. The parent environment stays unchanged.
12. Enabled mode rejects the Claude rollback provider before any phase. Operators must first
    disable capability enforcement to use the explicit Claude rollback path.
13. The HTTP and Dream templates read the same mode-0600 capability file. No unit is installed,
    restarted, or enabled in this delivery.
14. A real loopback HTTP test proves invalid-token 401, `/health` without bearer, Host/Origin
    protection, admin compatibility, scoped list/call behavior, gateway denial, forged-header
    irrelevance, rotation overlap, and revocation after constructing a new server instance.
15. No migration, live credential read, real token creation, service restart, or production
    rollout occurs in this branch.

## Configuration contract

`BRAIN_DREAM_CAPABILITY_ENFORCEMENT=false` leaves today's behavior intact.

When enabled, `MCP_HTTP_DREAM_TOKENS` is a `SecretStr` JSON object. Keys are canonical
`<project_key>:<phase>` profiles. Values identify one outbound token plus optional overlap:

```json
{
  "brain-v42:scan": {
    "active": "<new-token>",
    "accepted": ["<old-token>"]
  }
}
```

The example is abbreviated; an enabled project must define `scan`, `clean`, `connect`,
`synth`, `promote`, and `reorg`. The server accepts the existing `MCP_HTTP_TOKEN` as admin.
The runner sends only `active`. Removing a token and restarting the HTTP server revokes it.

## Architecture

Create `brain_v42.mcp.dream_capabilities` as the policy bounded context. It owns phase names,
tool sets, profile parsing, secret-safe validation, the application token verifier, and the
FastMCP call middleware. It imports no domain service or repository.

`Settings` stores the raw registry as `SecretStr`. Server setup parses it once. Enabled HTTP
mode assigns `DreamCapabilityTokenVerifier`, adds the call middleware, and retains
`HostOriginGuard`. Disabled HTTP mode retains `BearerTokenGuard`. STDIO remains unchanged.

FastMCP applies catalog transforms inside request middleware. Therefore
`_RequestAwareBM25SearchTransform` must inspect the authenticated principal: scoped tokens
receive the policy's native filtered tools before the outer middleware filters again; admin
tokens retain the current header-controlled compact/native view. The execution middleware is
the final authority and always denies gateways for scoped principals.

FastMCP installs authentication middleware before the supplied Host/Origin middleware. This
slice accepts that framework order: no tool routing occurs before both checks complete. The
real HTTP gate pins invalid Host and Origin rejection on `/mcp` and `/health`, including a
valid-token request.

`dream.sh` invokes the runner through `uv run python` and passes the canonical project key.
The runner imports the shared policy and parser, selects the profile's `active` token, strips
the full registry from the child environment, and keeps client-side `enabled_tools` as a
second layer. A systemd-like smoke runs the exact shell path with a minimal environment.

## Tasks

Each behavioral task records one collecting test that fails on its intended assertion before
production code. An import or collection error is not RED evidence.

### Task 1 — Shared phase policy

Files: create `src/brain_v42/mcp/dream_capabilities.py`; modify
`scripts/dream/codex_runner.py`; modify `tests/unit/test_dream_codex_runner.py`; create
`tests/unit/mcp/test_dream_capabilities.py`.

RED: add a collecting test for a public `dream_phase_tool_allowlist(phase)` and prove the
runner and policy return the same immutable tuple for every phase. GREEN: move the matrix and
retain the runner's public constant as a compatibility alias. Gate both focused test files.

### Task 2 — Secret registry and token verifier

Files: modify `src/brain_v42/mcp/dream_capabilities.py`; modify
`tests/unit/mcp/test_dream_capabilities.py`.

RED/GREEN one behavior at a time: active selection; accepted overlap; `rsplit` profile
parsing; aliases; incomplete matrices; blank/non-string members; duplicates; admin collision;
secret-safe exceptions; constant-time verifier; admin and scoped claims; revocation against a
new verifier instance.

### Task 3 — Catalog and call enforcement

Files: modify `src/brain_v42/mcp/dream_capabilities.py` and
`src/brain_v42/mcp/tool_catalog.py`; modify `tests/unit/mcp/test_dream_capabilities.py` and
`tests/unit/mcp/test_tool_catalog.py`.

RED: prove scoped list filtering for absent/compact/native/forged headers, allowed execution,
cross-phase denial, destructive denial, gateway denial, handler non-execution, and secret-safe
audit fields. GREEN: add the scoped transform path and call middleware. Admin behavior must
remain unchanged.

### Task 4 — HTTP wiring, shared environment source, and socket proof

Files: modify `src/brain_v42/config.py`, `src/brain_v42/mcp/server.py`,
`deploy/systemd/brain-mcp-http.service.tmpl`, `tests/unit/test_config.py`,
`tests/unit/mcp/test_server.py`, `tests/unit/mcp/test_http_security_bearer.py`, and
`tests/integration/test_dream_systemd_install.sh`; create
`tests/unit/mcp/test_dream_capability_http.py`. The loopback HTTP proof is autonomous: it
registers synthetic FastMCP tools and uses no database.

RED: prove disabled compatibility; enabled fail-closed startup; shared EnvironmentFile order;
and real Uvicorn loopback behavior for 401, health, Host/Origin, admin, scoped catalog/call,
gateway denial, rotation, revocation after server reconstruction, and forged headers. GREEN:
wire the verifier and middleware only when enabled. Never log settings or registry values.

### Task 5 — Runner isolation and fail-closed provider boundary

Files: modify `scripts/dream/codex_runner.py`, `scripts/dream.sh`,
`scripts/dream/_promote_smoke.sh`, `tests/unit/test_dream_codex_runner.py`,
`tests/unit/test_dream_sh_agent_provider.py`,
`tests/integration/test_dream_sh_tool_restriction.sh`, and
`tests/integration/test_dream_systemd_install.sh`.

RED: prove exact active-token selection; missing profile rejection before `Popen`; child
environment without admin token or registry; parent immutability; disabled compatibility;
Claude rejection when enabled; `uv run python` invocation; and a minimal-environment smoke of
timer → `dream.sh` → runner. The same smoke must cover `_promote_smoke.sh`, which is a
second direct runner entry point. GREEN: implement only those boundaries.

### Task 6 — Operator contract and delivery evidence

Files: update `README.md`, `docs/ARCHITECTURE.md`, `docs/MCP_TOOLS.md`, and this plan only where
the new dormant contract changes facts. Documentation has no behavioral RED requirement; run
link checks, `git diff --check`, and the relevant contract tests.

Document this later rollout, which requires separate operator authority:

1. Disable the persistent Dream timer and prove `brain-v42-dream.service` is inactive. Record
   whether re-enabling will cause an immediate `Persistent=true` catch-up.
2. Install the revised HTTP unit template, reload systemd, and place the complete registry plus
   `BRAIN_DREAM_CAPABILITY_ENFORCEMENT=true` in the shared mode-0600 token file.
3. Restart only `brain-mcp-http.service`. Prove admin access, every scoped profile, one
   cross-phase denial, one gateway denial, and Host/Origin behavior.
4. Re-enable the Dream timer only after the HTTP drill passes. Handle any catch-up as an
   explicit Dream run; never surprise-start it during configuration.
5. Rotate in a first quiescent window: disable the timer, wait for the Dream oneshot to become
   inactive, and record the persistent catch-up risk. Set `accepted=old` and `active=new`,
   restart only HTTP, then prove both bearers, the scoped catalog, an allowed call, and the
   cross-phase and gateway denials. Re-enable the timer and observe any explicit catch-up or
   Dream run using `new`.
6. Revoke in a second quiescent window: disable the timer, wait for the oneshot to become
   inactive, remove `old`, and restart only HTTP. Prove `old` returns `401` and `new` works,
   then re-enable the timer and handle any persistent catch-up. Never restart the Dream
   oneshot as a rotation or revocation action.

Rollback follows the same quiescence boundary: disable the timer, prove the oneshot inactive,
set enforcement false, restart only HTTP, prove admin access, then re-enable the timer while
handling `Persistent=true`. Never restart the Dream oneshot as a configuration action.

## Task gates

Use the project venv from each task worktree:

```bash
/home/hawixs/hawkixs_infra/git_repo/brain_v42/.venv/bin/python -m pytest <focused tests>
/home/hawixs/hawkixs_infra/git_repo/brain_v42/.venv/bin/ruff check <changed Python files>
/home/hawixs/hawkixs_infra/git_repo/brain_v42/.venv/bin/ruff format --check <changed Python files>
```

Before every production edit, run GitNexus upstream impact analysis for each symbol. Before
every commit, run `gitnexus_detect_changes` and inspect the real task diff.

## Required final gates

```bash
/home/hawixs/hawkixs_infra/git_repo/brain_v42/.venv/bin/python -m pytest tests/unit \
  --cov=brain_v42 --cov-report=term-missing --cov-fail-under=60
/home/hawixs/hawkixs_infra/git_repo/brain_v42/.venv/bin/python -m pytest tests/integration
bash tests/integration/test_dream_sh_tool_restriction.sh
bash tests/integration/test_dream_systemd_install.sh
/home/hawixs/hawkixs_infra/git_repo/brain_v42/.venv/bin/ruff check src tests scripts
/home/hawixs/hawkixs_infra/git_repo/brain_v42/.venv/bin/ruff format --check src tests scripts
/home/hawixs/hawkixs_infra/git_repo/brain_v42/.venv/bin/mypy src
git diff --check main...HEAD
```

The coordinator also runs the new Uvicorn loopback test alone and verifies no live secret,
service, database, or systemd state changed. No Alembic gate is required because this branch
adds no schema revision; existing migration contract tests remain inside the unit suite.

## Delivery evidence

Repository delivery completed on 2026-07-16; operator rollout remains pending. The planning
commit is `04a97f0`. At the final gate, `git diff 04a97f0^..HEAD` covers the complete delivery,
including that planning commit.

- [x] Shared immutable policy and runner compatibility alias.
- [x] Complete `active` / `accepted` registry, secret-safe verifier, and rotation boundary.
- [x] Exact scoped catalog and pre-handler call authorization, including gateway denial.
- [x] Dormant HTTP wiring, shared systemd environment source, and real Uvicorn loopback proof.
- [x] Allowlisted runner environment, loopback-only MCP, provider boundary, and systemd-like
  entrypoint smoke.
- [x] Operator contract and explicit residual SEC1 scope.

At documentation closure, the task-level full unit suite passed with 3,360 tests and 48 skips;
the coordinator owns the final branch coverage gate. The two real Uvicorn socket tests passed
outside the restricted sandbox. Both Dream shell contracts passed; the systemd-like runner and
direct PROMOTE path used local mocks, while the optional live user-manager precedence probe
skipped because no user systemd manager was available. No bearer was created, no unit was
installed, and no service, timer, database, or live configuration was changed.

## Non-goals and residual risks

- Project claims are informational in SEC1a; they do not authorize ID-only objects.
- Server-bounded `plan_scan_paths`, CLAUDE.md roots, and other filesystem writes are SEC1c.
- OAuth, dynamic issuance, a token database, and hot reload are out of scope. Restart is the
  revocation boundary for this LAN-only first slice.
- Enabled mode supports Codex only. The explicit Claude rollback requires disabling the
  firewall first.
- Tokens remain in process memory and are observable to an actor who can inspect that process.
- No Dream business rule, prompt guardrail, mutation cap, or MCP tool schema changes except
  the runner's internal `--project-key` argument.
- No deployment, service restart, token generation, or production denial test occurs here.
