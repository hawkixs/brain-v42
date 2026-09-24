# headless-agents 0.5.0 — Lot 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Execution method, already chosen by the operator (Q60 = a, 2026-09-25):** this plan is
> reviewed by codex, then implemented inline, test-first, in the pool session that wrote it,
> in three pull requests (A, B, C below), each reviewed by an independent non-Claude
> reviewer and merged under delegation 39f7ea9f (approve verdict + required CI green, never
> `--admin`). No tag and no release in this lot: those stay the operator's (§5 of the spec).

**Goal:** Deliver lot 1 of headless-agents 0.5.0 — roles, the one-step engine that holds every
gate, `run.json` with the `steps/` layout, the whole §3.8 write protocol, the new `ha run
TARGET` grammar with `ha roles`, `--version` and `ha clean` through the registry, and the
executor isolation and confinement proofs — so that daily delegation works on 0.5.0 after
this lot.

**Architecture:** Configuration (roles, models, MCP profiles) is read only from the
operator's configuration directory by `config_paths` and `roles`. A new `engine` module is
the one dispatch path: `plan()` resolves a target and applies every gate that needs neither
git nor mutable state; `execute()` takes the locks of §3.8.2, applies the git and state gates
(admission, quarantines, proofs), runs the step through the unchanged 0.4.0 providers and
`run_chain`, and writes `run.json`. Durable identity, locks, lineages, provenance and
quarantines live in a state directory (`$XDG_STATE_HOME/ha/`) written with atomic
publication; `cli.py` becomes a thin argparse adapter over the engine.

**Tech Stack:** Python 3.12, stdlib `tomllib`, `fcntl.flock`, `os.open(O_EXCL)`, `ctypes`
(`prctl`); `pydantic` and `structlog` only (package boundary guard); git ≥ 2.34 via
`subprocess`; pytest.

**Spec:** `docs/specs/2026-09-24-headless-agents-0.5.0-design.md` (merged in PR #200). Read
§3.1, §3.3, §3.4, §3.8 (all of it), §3.9, §3.10 and §4 before any task. Spec 0.4.0
(`docs/specs/2026-09-23-headless-agents-0.4.0-design.md`) §3.3–3.4 for the context bundle
and the 0.4.0 write flow this lot moves.

## Global Constraints

- Runtime dependencies stay `pydantic` and `structlog`; no import of `brain_v42` from
  `packages/headless-agents` (`tests/unit/headless_agents/test_package_boundary.py`).
- Configuration is TOML read with `tomllib`, never YAML (spec §4, boundary guard).
- `roles.toml`, `models.toml`, `mcp.toml` (and in lot 3 `workflows.toml`) are read only from
  `$XDG_CONFIG_HOME/ha/` (default `~/.config/ha/`); `XDG_CONFIG_HOME` and `XDG_STATE_HOME` are
  honoured only when absolute (spec §3.3).
- State directory: `$XDG_STATE_HOME/ha/` (default `~/.local/state/ha/`); cache of reports:
  `~/.cache/ha/runs/` (spec §3.8, §3.10). `ha clean` never removes state.
- Run id: `<UTC timestamp %Y%m%dT%H%M%S>-<8 hex>`, always minted by the engine (spec §3.8.1).
- Role names: lowercase letters, digits and `-`, starting with a letter, at most 64
  characters; disjoint from provider names (spec §3.1).
- Role defaults: `effort = "medium"`, `timeout = 300`, `context = "global"` (`"full"` when
  `write`), `context_parents = false`, `write = false`, `shell = false` (spec §3.1).
- Lock waits: at most 10 seconds, then exit `2` (spec §3.8.2).
- Every git command `ha` runs goes through `git_tripwire.git_command` + `git_environment`,
  and — except the engine's own commit (§3.8.3 step 8) — with
  `-c core.hooksPath=<an empty directory>` (spec §3.8.3 step 3).
- `result.json` schema 1 key set, the `AgentProvider` protocol and `run_chain`'s own result
  are unchanged (spec §2 Out, §3.9).
- Claude preamble bound: 131 071 bytes; agy and opencode `max_prompt_bytes`: 120 000 bytes
  (spec §3.4); read them from `registry.max_prompt_bytes` and
  `providers.claude.MAX_APPEND_SYSTEM_PROMPT_BYTES`, never retype them.
- One-step exit codes (0.4.0 contract): `0` answer; `1` failure; `2` invalid usage; `3`
  provider unavailable; `4` timeout with no tool call started; `5` write run with no change;
  `124` timeout. An exhausted chain returns its last link's `3` or `4` (spec §3.9).
- Everything written for GitHub — code, comments, tests, commits, PRs — in English.
- Project gates before every commit: `pytest tests/unit`, `ruff check src/ tests/ packages/`,
  `ruff format --check src/ tests/ packages/`, `mypy src/` and the package's own mypy target.
  Worktree note: export `POSTGRES_URL` before `pytest tests/unit` (CLAUDE.md, Tests).

## Decisions this plan takes where the spec leaves the choice to it

| # | Decision | Why |
|---|---|---|
| P1 | Module names: `config_paths`, `roles`, `state`, `locks`, `runs`, `report`, `repo`, `engine`, `gitops`, `lineage`, `provenance`, `quarantine`, `write_flow`, `proofs`, `procgroup`. `cli_write.py` is removed; its behaviour moves into `write_flow.py` unchanged where §3.8.3 does not change it. | Spec §3.4: "final module names are the plan's call". One responsibility per file. |
| P2 | The repository's identity is discovered **from the filesystem, without git**, by a new `repo.discover(start) -> RepoIdentity(work_tree, git_dir, common_dir)`: walk up from `--repo` (or the cwd) to the first `.git`, resolve a `.git` file's `gitdir:` and the git dir's `commondir` with `git_tripwire.resolve_git_dir` / `_common_dir` (made public as `common_dir`), and resolve every path. It replaces 0.4.0's `git rev-parse --show-toplevel`. `plan()` records the raw `--repo` and the cwd; `execute()` calls `discover()` first, before any lock that depends on the repository. The repository quarantine and the lineage index are keyed by the resolved `common_dir`. | Spec §3.8.2: `plan()` never runs git; §3.8.3 step 1 and §3.8.5: quarantines and the repository's lineages are checked before the first git command, so the identity those checks need cannot come from git. (Codex review of this plan, round 2.) |
| P3 | Proof records live in `<state>/proofs/<rail>.json` = `{"rail", "version", "isolation": {"passed", "date"}, "confinement": {"passed", "date"} \| null}`. They are written by `proofs.record_proof()`, which the `live` tests call on success; the engine reads them and compares `version` with `registry.probe(rail).version`. | Spec §3.8.0/§4 require a per-rail proof "with the rail version and date" that classifies write roles and gates executors, but name no storage. The state directory is the one durable, operator-owned place (§3.8). |
| P4 | HTTP providers need no isolation proof: they run no local executor and load no operator configuration. | Spec §3.8.0 lists instruction files, skills, plugins, hooks, settings and MCP servers — all local-CLI concepts. |
| P5 | Every merged state of `main` keeps the spec's guarantees. PR B ships the **isolation** proofs and their gate (Task 15b) together with the engine, and **refuses every write run** (a `write` role or `--write`: `UsageError`, exit 2, "write runs are not available in this build: the write protocol of spec §3.8.3 is not merged yet"). PR C ships the write protocol, the confinement proofs and classification, and lifts that refusal. | Codex review of this plan (round 1, blocker): an engine that runs a rail with no isolation proof, or a write without intent, provenance and locks, contradicts §3.8.0 and §5 even on an intermediate `main`. The installed `ha` is pinned to the `headless-agents-v0.4.0` tag (uv tool receipt), so no merge reaches the operator before a tag either way. |
| P6 | `PR_SET_PDEATHSIG` is applied with `preexec_fn` in each rail's `Popen`, and every provider is started from the thread that waits on it. | The signal fires when the *thread* that forked dies (Linux semantics): a provider must be spawned by the thread that outlives it. |
| P7 | Lot 1's `ha runs` reads `run.json` (new runs) and `result.json` (legacy) minimally; the full listing (cost, duration, quarantines at the top) is lot 2. | `run.json` moves the root `result.json` into `steps/`, which would otherwise hide every new run from 0.4.0's `ha runs`. |
| P8 | The README's CLI synopsis is updated in PR B for the new grammar only; the full README pass stays in lot 5. | A README that documents a removed `-p` is wrong the day PR B merges. |

## Review Focus

Five inputs the spec implies but no spec test exercises, most likely first; each has a test in
the task that owns the code.

1. **A provider spawned from a worker thread.** With `PR_SET_PDEATHSIG` the child is killed
   when the *thread* that forked it exits; a provider must survive while its spawning thread
   waits on it and die when `ha` dies. Test in Task 7.
2. **No prompt on a non-terminal stdin that is empty or closed** (`ha run codex < /dev/null`,
   a pipe closed by the caller): refused with exit `2`, "no prompt", never a hang and never a
   run with an empty task. Test in Task 13.
3. **`--run-dir` inside the repository, inside the state directory, or inside another run's
   directory:** a worktree nested in its own repository or a report written over state. The
   operator expects a refusal (exit `2`) naming why. Test in Task 11.
4. **Two `ha` processes minting a run id in the same second:** the second must get a fresh
   id (retry on `O_EXCL` collision), never share or overwrite the first's registry entry.
   Test in Task 11.
5. **Ctrl-C (SIGINT) during `execute()`:** the provider runs in its own session, so the
   terminal's SIGINT reaches `ha` only; `ha` must kill and reap the provider's process group
   (each rail, Task 7), release every lock, leave the registry entry non-final so the run
   reads `incomplete`, and exit `130` — and the next run proceeds without waiting 10 s on a
   stale lock. Unit test of the rails in Task 7; end-to-end test through a real `ha`
   subprocess and a real provider subprocess in Task 14.

---

## File structure

```text
packages/headless-agents/src/headless_agents/
  config_paths.py   NEW  config/state directory resolution, the trust boundary (§3.3)
  roles.py          NEW  roles.toml: loading, validation, implicit roles (§3.1)
  cli_models.py     MOD  model precedence gains the role level; paths via config_paths
  mcp_profiles.py   MOD  default path via config_paths (absolute XDG only, inside the dir)
  context.py        MOD  scope "role": role instructions block, placed last (§3.1)
  providers/claude.py  MOD  --safe-mode on every command; docstring (2901d5ba)
  providers/codex.py   MOD  write roles: /tmp and $TMPDIR excluded, per-run TMPDIR (§3.8.0)
  procgroup.py      NEW  preexec: PR_SET_PDEATHSIG(SIGKILL) for provider children (§3.8.2)
  providers/*.py    MOD  pass procgroup.preexec to Popen (5 call sites)
  state.py          NEW  atomic publish, write-once create, read-or-unknown (§3.8.1)
  locks.py          NEW  flock locks, fixed order, bounded waits (§3.8.2)
  runs.py           NEW  run registry: mint, register, resolve, status, incomplete (§3.8.1)
  report.py         NEW  run.json: key set, steps, cost, atomic rewrite (§3.10)
  repo.py           NEW  repository identity from the filesystem, no git (P2)
  engine.py         NEW  Request, Target, Plan, plan(), execute(), UsageError (§3.4)
  gitops.py         NEW  hardened git with hooks disabled; the engine-commit variant
  lineage.py        NEW  lineage state documents and transitions (§3.8.1, §3.8.3)
  provenance.py     NEW  provenance records per commit (§3.8.1)
  quarantine.py     NEW  scope classification of tripwire paths, publish, check (§3.8.5)
  write_flow.py     NEW  §3.8.3 steps 1–9 for a write step (replaces cli_write.py)
  proofs.py         NEW  proof records, isolation and confinement procedures (§3.8.0)
  cli.py            REWRITE  thin adapter: run TARGET, roles, providers, runs, clean, --version
  cli_write.py      DELETE
tests/unit/headless_agents/
  test_config_paths.py test_roles.py test_state.py test_locks.py test_runs.py
  test_report.py test_engine_plan.py test_engine_execute.py test_procgroup.py
  test_lineage.py test_quarantine.py test_write_flow.py test_proofs.py   NEW
  test_cli.py test_cli_models.py test_context.py test_provider_claude.py
  test_provider_codex.py                                                   MOD
  test_cli_write.py   DELETE (its cases move into test_write_flow.py)
tests/unit/agents/test_golden_commands.py   unchanged; fixtures below gain --safe-mode
tests/fixtures/agents_golden/claude-*.json  MOD  "--safe-mode" inserted, nothing else
tests/live/headless_agents/test_proofs_live.py   NEW  isolation + confinement per rail
```

## Pull requests

| PR | Tasks | Content | Leaves `main` |
|---|---|---|---|
| A | 1–8 | configuration and roles, role instructions, rail hardening (`--safe-mode`, codex tmp roots, PDEATHSIG), `--version` | 0.4.0 CLI unchanged, library hardened |
| B | 9–15, 15b | state, locks, registry, `run.json`, engine for one-step **read-only** runs, the isolation proofs and their gate, new CLI grammar, `ha roles`, `ha clean` via the registry | new grammar live; every executor isolation-proven; write runs refused (P5) |
| C | 16–22 | the full §3.8.3 write protocol, quarantines, provenance, the unconfined path, `ha clean` rules, the confinement proofs and classification in `ha roles`; write runs enabled | lot 1 complete |

---

# PR A — configuration, roles, rail hardening

### Task 1: Configuration directory and the trust boundary

**Files:**
- Create: `packages/headless-agents/src/headless_agents/config_paths.py`
- Modify: `packages/headless-agents/src/headless_agents/cli_models.py` (`default_models_path`)
- Modify: `packages/headless-agents/src/headless_agents/mcp_profiles.py` (`default_profiles_path`, `load_profiles`)
- Test: `tests/unit/headless_agents/test_config_paths.py`

**Interfaces:**
- Produces: `config_dir(environ, *, home) -> Path` (resolved, symlinks followed);
  `state_dir(environ, *, home) -> Path` (resolved, created `0700` on first use by the caller);
  `config_file(name, environ, *, home) -> Path | None` (`None` when absent; raises
  `ConfigPathError` when the file is or links outside `config_dir`);
  `class ConfigPathError(ValueError)`.

- [ ] **Step 1: Write the failing tests**

```python
"""Where configuration and state live, and what may not be read (spec §3.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from headless_agents.config_paths import ConfigPathError, config_dir, config_file, state_dir


def test_absolute_xdg_config_home_is_used(tmp_path: Path) -> None:
    base = tmp_path / "xdg"
    (base / "ha").mkdir(parents=True)
    assert config_dir({"XDG_CONFIG_HOME": str(base)}, home=tmp_path / "home") == (
        base / "ha"
    ).resolve()


def test_relative_xdg_config_home_is_ignored(tmp_path: Path) -> None:
    home = tmp_path / "home"
    got = config_dir({"XDG_CONFIG_HOME": "relative/cfg"}, home=home)
    assert got == (home / ".config" / "ha").resolve()


def test_relative_xdg_state_home_is_ignored(tmp_path: Path) -> None:
    home = tmp_path / "home"
    got = state_dir({"XDG_STATE_HOME": "rel"}, home=home)
    assert got == (home / ".local" / "state" / "ha").resolve()


def test_a_config_file_linking_outside_the_directory_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    directory = home / ".config" / "ha"
    directory.mkdir(parents=True)
    repo_file = tmp_path / "repo" / "roles.toml"
    repo_file.parent.mkdir()
    repo_file.write_text("[x]\nprovider = 'codex'\n")
    (directory / "roles.toml").symlink_to(repo_file)
    with pytest.raises(ConfigPathError, match=r"roles\.toml.*points to .*repo/roles\.toml"):
        config_file("roles.toml", {}, home=home)


def test_a_symlinked_config_directory_is_followed_once(tmp_path: Path) -> None:
    home = tmp_path / "home"
    real = tmp_path / "dotfiles" / "ha"
    real.mkdir(parents=True)
    (real / "roles.toml").write_text("")
    (home / ".config").mkdir(parents=True)
    (home / ".config" / "ha").symlink_to(real)
    assert config_file("roles.toml", {}, home=home) == (real / "roles.toml").resolve()


def test_an_absent_file_is_none(tmp_path: Path) -> None:
    assert config_file("roles.toml", {}, home=tmp_path) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/unit/headless_agents/test_config_paths.py -v`
Expected: FAIL — `ModuleNotFoundError: headless_agents.config_paths`.

- [ ] **Step 3: Implement**

```python
"""Where ``ha`` reads its configuration and keeps its state (spec 0.5.0 §3.3, §3.8).

Configuration comes from the operator's directory only -- never from a
repository or the working directory: a ``roles.toml`` shipped in a cloned
repository could arm ``write`` and ``shell``. ``XDG_CONFIG_HOME`` and
``XDG_STATE_HOME`` count only when absolute, as the XDG specification
requires; a relative value is ignored, never resolved against the cwd. The
directory is resolved once with its links followed, and every file must then
resolve inside it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


class ConfigPathError(ValueError):
    """A configuration file resolves outside the configuration directory."""


def _xdg(environ: Mapping[str, str], name: str, default: Path) -> Path:
    value = environ.get(name, "")
    return Path(value) if value and os.path.isabs(value) else default


def config_dir(environ: Mapping[str, str], *, home: Path) -> Path:
    return (_xdg(environ, "XDG_CONFIG_HOME", home / ".config") / "ha").resolve()


def state_dir(environ: Mapping[str, str], *, home: Path) -> Path:
    return (_xdg(environ, "XDG_STATE_HOME", home / ".local" / "state") / "ha").resolve()


def config_file(name: str, environ: Mapping[str, str], *, home: Path) -> Path | None:
    directory = config_dir(environ, home=home)
    candidate = directory / name
    if not candidate.exists() and not candidate.is_symlink():
        return None
    resolved = candidate.resolve()
    if not resolved.is_relative_to(directory):
        raise ConfigPathError(
            f"{candidate}: points to {resolved}, outside the configuration directory "
            f"{directory}; configuration is read only from there"
        )
    return resolved
```

Then route `cli_models.default_models_path` and `mcp_profiles.default_profiles_path` through
it: `default_models_path` returns `config_dir(environ, home=home) / "models.toml"` for the
message, and `models_for` loads `config_file("models.toml", ...)` (absent → `{}`), turning a
`ConfigPathError` into `ModelsError`; `mcp_server` does the same with `McpProfileError`.
Add one test each in `test_cli_models.py` and `test_mcp_profiles.py`: a relative
`XDG_CONFIG_HOME` is ignored, and a `models.toml` / `mcp.toml` symlinked into a repository is
refused.

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/unit/headless_agents/test_config_paths.py tests/unit/headless_agents/test_cli_models.py tests/unit/headless_agents/test_mcp_profiles.py -v`
Expected: PASS.

- [ ] **Step 5: Commit** — `feat(headless-agents): read configuration only from the operator's directory`

### Task 2: `roles.toml` — loading, validation, implicit roles

**Files:**
- Create: `packages/headless-agents/src/headless_agents/roles.py`
- Test: `tests/unit/headless_agents/test_roles.py`

**Interfaces:**
- Consumes: `config_paths.config_file`; `registry.PROVIDER_NAMES`, `HTTP_PROVIDER_NAMES`;
  `mcp_profiles.load_profiles`; `providers.openai_compat.GENERIC_NAME`.
- Produces:

```python
@dataclass(frozen=True)
class Link:
    provider: str
    model: str            # "" when the link names none

@dataclass(frozen=True)
class Role:
    name: str
    links: tuple[Link, ...]          # one for a provider role
    model: str                       # the role's `model` ("" when unset; chain roles: "")
    effort: str
    timeout: float
    context: ContextLevel
    context_parents: bool
    mcp: str | None
    write: bool
    shell: bool
    base_url: str | None
    key_env: str | None
    instructions: str | None
    implicit: bool                   # True for a provider name used as a role

class RolesError(ValueError): ...   # message: "<file>: [<entry>] <rule>"

def load_roles(path: Path | None, *, mcp_profiles: Mapping[str, object]) -> dict[str, Role]
def implicit_role(provider: str) -> Role
def resolve_role(name: str, declared: Mapping[str, Role]) -> Role   # KeyError-free: RolesError
NAME_PATTERN: Final = re.compile(r"[a-z][a-z0-9-]{0,63}")
```

- [ ] **Step 1: Write the failing tests** — one test per refusal of §3.1, each asserting
  the message names the file, the entry and the rule:

```python
"""roles.toml (spec §3.1): every refusal names the file, the entry and the rule."""

from __future__ import annotations

from pathlib import Path

import pytest

from headless_agents.roles import RolesError, implicit_role, load_roles, resolve_role


def _load(tmp_path: Path, text: str, profiles: dict[str, object] | None = None):
    path = tmp_path / "roles.toml"
    path.write_text(text)
    return load_roles(path, mcp_profiles=profiles or {})


def test_a_provider_role_takes_every_default(tmp_path: Path) -> None:
    role = _load(tmp_path, '[rev]\nprovider = "codex"\n')["rev"]
    assert (role.effort, role.timeout, role.context, role.write, role.shell) == (
        "medium", 300.0, "global", False, False,
    )


def test_a_write_role_defaults_to_the_full_context(tmp_path: Path) -> None:
    role = _load(tmp_path, '[impl]\nprovider = "codex"\nwrite = true\n')["impl"]
    assert role.context == "full"


def test_a_chain_role_keeps_its_links_in_order(tmp_path: Path) -> None:
    role = _load(tmp_path, '[impl]\nchain = ["opencode:m1", "codex"]\n')["impl"]
    assert [(link.provider, link.model) for link in role.links] == [("opencode", "m1"), ("codex", "")]


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ('[a]\nprovider = "codex"\nbogus = 1\n', "unknown field 'bogus'"),
        ('[a]\nprovider = 3\n', "provider must be a string"),
        ('[a]\neffort = "high"\n', "exactly one of provider and chain"),
        ('[a]\nprovider = "codex"\nchain = ["claude"]\n', "exactly one of provider and chain"),
        ('[a]\nprovider = "nope"\n', "unknown provider 'nope'"),
        ('[a]\nchain = ["codex", "codex:x"]\n', "codex appears twice in chain"),
        ('[a]\nchain = ["codex"]\nmodel = "m"\n', "model is refused on a chain role"),
        ('[a]\nprovider = "codex"\nshell = true\n', "shell requires write"),
        ('[a]\nprovider = "openrouter"\nwrite = true\n', "write needs a CLI rail"),
        ('[a]\nchain = ["codex", "openrouter"]\nmcp = "b"\n', "mcp needs a CLI rail"),
        ('[a]\nprovider = "codex"\nmcp = "missing"\n', "mcp profile 'missing' is not in mcp.toml"),
        ('[a]\nprovider = "openai-compat"\n', "openai-compat needs base_url and key_env"),
        ('[a]\nprovider = "codex"\nbase_url = "http://x"\n', "base_url and key_env apply to openai-compat only"),
        ('["Bad_Name"]\nprovider = "codex"\n', "invalid role name"),
        ('[codex]\nprovider = "claude"\n', "collides with a provider name"),
        # wrong types and invalid values, one per field (spec §4: every refusal)
        ('[a]\nchain = "codex"\n', "chain must be a list of strings"),
        ('[a]\nchain = ["codex", 3]\n', "chain must be a list of strings"),
        ('[a]\nchain = []\n', "chain must name at least one provider"),
        ('[a]\nprovider = "codex"\nmodel = 3\n', "model must be a non-empty string"),
        ('[a]\nprovider = "codex"\nmodel = ""\n', "model must be a non-empty string"),
        ('[a]\nprovider = "codex"\neffort = 1\n', "effort must be a string"),
        ('[a]\nprovider = "codex"\neffort = "loud"\n', "effort 'loud' is not a codex effort"),
        ('[a]\nprovider = "codex"\ntimeout = true\n', "timeout must be a positive number"),
        ('[a]\nprovider = "codex"\ntimeout = "60"\n', "timeout must be a positive number"),
        ('[a]\nprovider = "codex"\ntimeout = 0\n', "timeout must be a positive number"),
        ('[a]\nprovider = "codex"\ncontext = "all"\n', "context must be one of full, global, none"),
        ('[a]\nprovider = "codex"\ncontext_parents = "yes"\n', "context_parents must be a boolean"),
        ('[a]\nprovider = "codex"\nmcp = 1\n', "mcp must be a profile name"),
        ('[a]\nprovider = "codex"\nwrite = 1\n', "write must be a boolean"),
        ('[a]\nprovider = "codex"\nwrite = true\nshell = "no"\n', "shell must be a boolean"),
        ('[a]\nprovider = "openai-compat"\nbase_url = 1\nkey_env = "K"\n', "base_url must be a string"),
        ('[a]\nprovider = "openai-compat"\nbase_url = "http://x"\nkey_env = 2\n', "key_env must be a string"),
        ('[a]\nprovider = "codex"\ninstructions = 5\n', "instructions must be a string"),
        ('a = 1\n', "a role must be a table"),
    ],
)
def test_every_refusal_names_file_entry_and_rule(tmp_path: Path, text: str, rule: str) -> None:
    with pytest.raises(RolesError) as caught:
        _load(tmp_path, text, profiles={"b": object()})
    message = str(caught.value)
    assert str(tmp_path / "roles.toml") in message
    assert rule in message


def test_every_provider_is_an_implicit_role() -> None:
    role = implicit_role("codex")
    assert role.links[0].provider == "codex" and role.instructions is None and role.implicit


def test_resolve_prefers_a_declared_role_then_a_provider(tmp_path: Path) -> None:
    declared = _load(tmp_path, '[rev]\nprovider = "claude"\n')
    assert resolve_role("rev", declared).name == "rev"
    assert resolve_role("codex", declared).implicit
    with pytest.raises(RolesError, match="unknown target 'zzz'"):
        resolve_role("zzz", declared)


def test_an_absent_file_declares_nothing() -> None:
    assert load_roles(None, mcp_profiles={}) == {}


def test_a_deeply_nested_file_is_a_roles_error(tmp_path: Path) -> None:
    with pytest.raises(RolesError, match="nested too deeply"):
        _load(tmp_path, "a = " + "[" * 2000 + "]" * 2000 + "\n")
```

- [ ] **Step 2: Run to verify they fail** — `pytest tests/unit/headless_agents/test_roles.py -v`; expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `roles.py`** — `tomllib.load`, catching `TOMLDecodeError`,
  `OSError` and `RecursionError` (as `cli_models.load_models` does); a table per role; a
  `_FIELDS: Final[Mapping[str, type | tuple[type, ...]]]` map of §3.1's fields and types
  (`timeout` accepts a positive `int` or `float`, refuses `bool`; `effort` any non-empty
  string, and one of `providers.codex.REASONING_EFFORTS` whenever a link is codex — the
  other rails translate or ignore it); the checks in the order of the
  parametrized test above, each raising `RolesError(f"{path}: [{name}] {rule}")`;
  `context` defaults to `"full" if write else "global"`; a chain entry parsed with
  `cli_models.parse_chain` semantics (model after the first colon) but reporting "appears
  twice in chain" through `RolesError`; `implicit_role(p)` builds
  `Role(name=p, links=(Link(p, ""),), model="", effort="medium", timeout=300.0,
  context="global", context_parents=False, mcp=None, write=False, shell=False,
  base_url=None, key_env=None, instructions=None, implicit=True)`.
  The workflow-name collision check arrives with `workflows.toml` in lot 3: note it in the
  module docstring.

- [ ] **Step 4: Run to verify they pass** — same command; expected: PASS.

- [ ] **Step 5: Commit** — `feat(headless-agents): roles.toml with validation and implicit provider roles`

### Task 3: Model precedence gains the role level

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/cli_models.py` (`resolve_models`, `models_for`)
- Test: `tests/unit/headless_agents/test_cli_models.py`

**Interfaces:**
- Produces: `resolve_models(links, *, default: str, role_model: str, declared, declared_path)`
  and `models_for(links, *, default, role_model, environ, home)`; precedence: link's own →
  `-m` (`default`) → `role_model` → `models.toml` → none (refused except agy).

- [ ] **Step 1: Failing tests**

```python
def test_the_role_model_sits_between_m_and_models_toml(tmp_path: Path) -> None:
    declared = {"codex": "from-file"}
    path = tmp_path / "models.toml"
    links = (("codex", ""),)
    assert resolve_models(links, default="", role_model="from-role", declared=declared,
                          declared_path=path) == {"codex": "from-role"}
    assert resolve_models(links, default="from-m", role_model="from-role", declared=declared,
                          declared_path=path) == {"codex": "from-m"}
    assert resolve_models((("codex", "own"),), default="from-m", role_model="from-role",
                          declared=declared, declared_path=path) == {"codex": "own"}
```

- [ ] **Step 2:** run, expect `TypeError: unexpected keyword 'role_model'`.
- [ ] **Step 3:** add the keyword (default `""` so 0.4.0 callers stay valid) and the
  `or role_model.strip()` term between `default` and `declared`; `models_for` reads the file
  only when some link is left without a model after the role level.
- [ ] **Step 4:** run `test_cli_models.py`; expected PASS (existing cases unchanged).
- [ ] **Step 5: Commit** — `feat(headless-agents): the role's model in the precedence of a link`

### Task 4: Role instructions in the context bundle

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/context.py`
- Test: `tests/unit/headless_agents/test_context.py`

**Interfaces:**
- Produces: `Scope = Literal["repository", "user", "role"]`; `ContextFile.source: Path | str`
  (a role's is the string `"role:<name>"`); `role_instructions(name, text) -> ContextFile`;
  `ContextBundle.with_role(file: ContextFile | None) -> ContextBundle`; `preamble()` order:
  user, repository, role; `to_list()` lists the role entry with `"scope": "role"` and
  `"path": "role:<name>"`.

- [ ] **Step 1: Failing tests**

```python
def test_role_instructions_read_last_in_the_preamble(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("repo rules")
    user = tmp_path / "user.md"
    user.write_text("user rules")
    bundle = resolve_context(level="full", repository_root=tmp_path, user_files=(user,))
    with_role = bundle.with_role(role_instructions("reviewer-codex", "review rules"))
    text = with_role.preamble()
    assert text.index("user rules") < text.index("repo rules") < text.index("review rules")
    assert '<instructions source="role:reviewer-codex" scope="role">' in text


def test_role_instructions_reach_the_none_level() -> None:
    bundle = resolve_context(level="none", repository_root=None)
    assert "only mine" in bundle.with_role(role_instructions("r", "only mine")).preamble()


def test_a_bundle_without_role_instructions_is_byte_identical(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("repo rules")
    bundle = resolve_context(level="full", repository_root=tmp_path)
    assert bundle.with_role(None).preamble() == bundle.preamble()
    assert bundle.with_role(None).to_list() == bundle.to_list()


def test_the_role_entry_is_listed_with_scope_role() -> None:
    bundle = resolve_context(level="none", repository_root=None).with_role(
        role_instructions("r", "x")
    )
    assert bundle.to_list()[-1]["scope"] == "role"
    assert bundle.to_list()[-1]["path"] == "role:r"
```

- [ ] **Step 2:** run, expect `ImportError: role_instructions`.
- [ ] **Step 3:** implement; `_block` escapes `str(file.source)` for both kinds;
  `role_instructions` hashes the UTF-8 bytes like `_read` does.
- [ ] **Step 4:** run `test_context.py` and `tests/unit/agents/test_golden_commands.py`;
  expected PASS (the Dream bundles carry no role instructions).
- [ ] **Step 5: Commit** — `feat(headless-agents): role instructions travel as the last preamble block`

### Task 5: claude runs with `--safe-mode`; the `workspace=None` docstring (2901d5ba)

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/providers/claude.py` (`build_claude_command`)
- Modify: `tests/fixtures/agents_golden/claude-{scan,clean,connect,synth,reorg,promote}.json`
- Test: `tests/unit/headless_agents/test_provider_claude.py`

**Interfaces:**
- Produces: every command from `build_claude_command` contains `--safe-mode` right after
  `"-p", "-"`; nothing else in the argv changes.

- [ ] **Step 1: Failing test**

```python
@pytest.mark.parametrize("workspace", [None, "read", "write", "shell"])
def test_every_claude_command_runs_in_safe_mode(tmp_path: Path, workspace: str | None) -> None:
    ws = None
    if workspace is not None:
        ws = Workspace(path=tmp_path, write=workspace != "read", shell=workspace == "shell")
    command = build_claude_command(
        model="m", max_turns=1, mcp_config_path=tmp_path / "mcp.json", mcp=None, workspace=ws
    )
    assert command[:4] == ["claude", "-p", "-", "--safe-mode"]
```

- [ ] **Step 2:** run; expect FAIL (`--model` at index 3).
- [ ] **Step 3:** insert `"--safe-mode"` in the base command; rewrite the docstring paragraph
  "``workspace=None`` keeps this run exactly as it ran before 0.4.0 -- ``bypassPermissions``
  and every tool" to: "``workspace=None`` runs in ``bypassPermissions`` with **no built-in
  tool** (``--tools ""``): only the MCP tools ``--allowedTools`` names are callable." (ticket
  2901d5ba), and add a paragraph for `--safe-mode` citing spec 0.5.0 §3.8.0 (operator
  customisations off, OAuth kept; `--bare` breaks OAuth).
- [ ] **Step 4:** update each golden fixture by inserting `"--safe-mode"` after `"-"` in
  `argv` — by a one-off edit, never by re-running `capture.py` (its docstring forbids it).
  Add to `tests/fixtures/agents_golden/capture.py`'s docstring one line: "2026-09-25:
  `--safe-mode` inserted by hand in every claude fixture (spec 0.5.0 §3.8.0), nothing else."
  Run `pytest tests/unit/headless_agents/test_provider_claude.py tests/unit/agents/test_golden_commands.py tests/unit/test_dream_claude_runner.py -v`; expected PASS.
- [ ] **Step 5: Commit** — `fix(headless-agents): run claude in --safe-mode on every run (G7)`, body naming tickets a5cbb325 and 2901d5ba.

### Task 6: codex write roles — no `/tmp`, no `$TMPDIR`, a per-run scratch `TMPDIR`

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/providers/codex.py` (`build_codex_command`, the run's environment)
- Test: `tests/unit/headless_agents/test_provider_codex.py`

**Interfaces:**
- Produces: for `workspace_mode.write`, overrides
  `sandbox_workspace_write.exclude_slash_tmp=true` and
  `sandbox_workspace_write.exclude_tmpdir_env_var=true`; the child environment's `TMPDIR`
  set to `<run_dir>/tmp` (created `0700`, holding no repository) when the run has a
  `run_dir`, else to a fresh `tempfile.mkdtemp(prefix="ha-codex-tmp-")` removed after the run.

- [ ] **Step 1: Failing tests**

```python
def test_a_write_run_excludes_tmp_roots(tmp_path: Path) -> None:
    command = build_codex_command(
        model="m", reasoning_effort="medium", report_log=tmp_path / "r", workspace=tmp_path,
        mcp=None, workspace_mode=Workspace(path=tmp_path, write=True),
    )
    joined = " ".join(command)
    assert "sandbox_workspace_write.exclude_slash_tmp=true" in joined
    assert "sandbox_workspace_write.exclude_tmpdir_env_var=true" in joined


def test_a_read_only_run_is_unchanged(tmp_path: Path) -> None:
    command = build_codex_command(
        model="m", reasoning_effort="medium", report_log=tmp_path / "r", workspace=tmp_path,
        mcp=None, workspace_mode=Workspace(path=tmp_path),
    )
    assert "exclude_slash_tmp" not in " ".join(command)
```

  plus one test through `CodexProvider().run` with a fake `codex` executable on `PATH` that
  writes `$TMPDIR` into a file: the value is `<run_dir>/tmp`.

- [ ] **Step 2:** run; FAIL.
- [ ] **Step 3:** implement in `build_codex_command` (append the two overrides when
  `sandbox == "workspace-write"`) and in the environment built for the child in `run`.
- [ ] **Step 4:** run `test_provider_codex.py` and `tests/unit/agents/test_golden_commands.py` (the Dream's codex fixtures are read-only runs: unchanged). PASS.
- [ ] **Step 5: Commit** — `fix(headless-agents): close /tmp and TMPDIR to codex write runs (spec 3.8.0)`

### Task 7: Providers die with `ha` — `PR_SET_PDEATHSIG`, and on interruption

**Files:**
- Create: `packages/headless-agents/src/headless_agents/procgroup.py`
- Modify: the `Popen` calls and their waits in `providers/{claude,codex,agy,opencode,openai_compat}.py`
- Test: `tests/unit/headless_agents/test_procgroup.py`, each rail's test file

**Interfaces:**
- Produces: `preexec() -> None` — on Linux calls `prctl(PR_SET_PDEATHSIG, SIGKILL)` through
  `ctypes.CDLL(None, use_errno=True)`; a no-op elsewhere. Every provider `Popen` passes
  `preexec_fn=procgroup.preexec` in addition to `start_new_session=True`.
- Produces: every rail's wait on its child (`communicate`, the read loop) is wrapped so that
  **any** `BaseException` (`KeyboardInterrupt`, `SystemExit`) calls
  `capability.terminate_process_group(process)` before re-raising — as `codex.py` already does
  at its line 550 and `registry.probe` does. Measured on the plan's review: the claude rail's
  `process.communicate(...)` handles only `TimeoutExpired`, so a Ctrl-C leaves `claude` running
  in its own session. PDEATHSIG covers `ha` *dying*; this covers `ha` *being interrupted*.

- [ ] **Step 1: Failing tests** (Linux-only, `pytest.mark.skipif(sys.platform != "linux")`)

```python
def _spawn_sleeper_from(target_pid_file: Path) -> None:
    child = subprocess.Popen(["sleep", "30"], preexec_fn=preexec, start_new_session=True)
    target_pid_file.write_text(str(child.pid))
    child.wait()


def test_a_child_dies_when_ha_dies(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    script = (
        "import sys, subprocess, pathlib\n"
        "from headless_agents.procgroup import preexec\n"
        "c = subprocess.Popen(['sleep','30'], preexec_fn=preexec, start_new_session=True)\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(c.pid))\n"
        "import time; time.sleep(30)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", script])
    _wait_for(pid_file)
    child_pid = int(pid_file.read_text())
    parent.kill()
    parent.wait()
    assert _gone_within(child_pid, seconds=5)


def test_a_child_survives_while_its_spawning_thread_waits(tmp_path: Path) -> None:
    """Review Focus 1: PDEATHSIG follows the spawning THREAD."""
    pid_file = tmp_path / "pid"
    worker = threading.Thread(target=_spawn_sleeper_from, args=(pid_file,), daemon=True)
    worker.start()
    _wait_for(pid_file)
    time.sleep(0.5)
    assert _alive(int(pid_file.read_text()))
    os.kill(int(pid_file.read_text()), signal.SIGKILL)
    worker.join(timeout=5)
```

  (`_wait_for`, `_alive`, `_gone_within` are small polling helpers in the test module using
  `os.kill(pid, 0)`.) Add one assertion per rail that its `Popen` receives
  `preexec_fn=procgroup.preexec` (monkeypatch `subprocess.Popen` in the rail module and
  capture kwargs), in the rail's existing test file. And, per CLI rail, an interruption test:
  a fake executable on `PATH` (a shell script that writes its pid, then `sleep 30`) run
  through `get_provider(rail).run(spec)` in a thread; the main thread raises
  `KeyboardInterrupt` into the wait by patching `subprocess.Popen.communicate` (or the rail's
  read loop) to raise it once the pid file exists; assert the exception propagates **and**
  the sleeper's process group is gone within 6 s (`TERMINATION_GRACE_SECONDS` + 1).

- [ ] **Step 2:** run; FAIL (`ModuleNotFoundError`).
- [ ] **Step 3:** implement `procgroup.py` (`PR_SET_PDEATHSIG = 1`; raise nothing from the
  preexec — a failing `prctl` writes nothing and returns, since a preexec exception aborts the
  spawn; document it) and wire the five rails.
- [ ] **Step 4:** run `tests/unit/headless_agents -v`; PASS.
- [ ] **Step 5: Commit** — `feat(headless-agents): provider processes die with the ha process that started them`

### Task 8: `ha --version`

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/cli.py` (`_parser`, `main`)
- Test: `tests/unit/headless_agents/test_cli.py`

- [ ] **Step 1: Failing test**

```python
def test_version_prints_the_installed_package_version(world: _World) -> None:
    from importlib.metadata import version

    code, out, _ = world.run("--version")
    assert code == 0
    assert out.strip() == f"ha {version('headless-agents')}"
```

- [ ] **Step 2:** run; FAIL (`a command is required`).
- [ ] **Step 3:** `parser.add_argument("--version", action="store_true")`, handled in `main`
  before the command dispatch.
- [ ] **Step 4:** PASS. **Step 5: Commit** — `feat(headless-agents): ha --version`

**PR A gate:** full project gates; open PR "headless-agents 0.5.0 lot 1 (A): configuration,
roles, rail hardening"; codex review via `ha run` (0.4.0 grammar still) against the diff;
merge under 39f7ea9f.

---

# PR B — state, registry, `run.json`, the engine, the new CLI

### Task 9: State files — atomic publication, write-once, unknown

**Files:**
- Create: `packages/headless-agents/src/headless_agents/state.py`
- Test: `tests/unit/headless_agents/test_state.py`

**Interfaces:**
- Produces:

```python
class Unknown(Exception):
    """A state file missing when it should exist, unparsable, or naming another id."""

def publish(path: Path, document: Mapping[str, object]) -> None
    # temp file in the same dir (mkstemp), write, fsync, os.replace, fsync(dir)
def create_once(path: Path, document: Mapping[str, object]) -> None
    # os.open(O_CREAT|O_EXCL|O_WRONLY, 0o600), write, fsync, fsync(dir); FileExistsError propagates
def read(path: Path, *, expect_id: tuple[str, str] | None = None) -> dict[str, object]
    # raises Unknown on missing/invalid JSON/non-object/id mismatch; expect_id=("run_id", "…")
def read_optional(path: Path, *, expect_id=None) -> dict[str, object] | None
    # None only when absent; Unknown otherwise
def ensure_dir(path: Path) -> Path   # mkdir(parents=True, mode=0o700, exist_ok=True)
```

- [ ] **Step 1: Failing tests** — publish replaces atomically (a reader during a concurrent
  publish sees old or new, never partial: 200 publishes in a thread while another reads and
  `json.loads` every read); `create_once` twice raises `FileExistsError`; `read` of a
  truncated file, of `[]`, of a file whose `run_id` differs from `expect_id` raises
  `Unknown` with the path in the message; `read_optional` of an absent file is `None`.

```python
def test_a_document_naming_another_id_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "20260925T000000-aaaaaaaa.json"
    publish(path, {"run_id": "20260925T000000-bbbbbbbb"})
    with pytest.raises(Unknown, match="bbbbbbbb"):
        read(path, expect_id=("run_id", "20260925T000000-aaaaaaaa"))
```

- [ ] **Step 2–4:** fail, implement, pass. **Step 5: Commit** — `feat(headless-agents): state documents with atomic publication`

### Task 10: Locks — fixed order, bounded waits, gone with the process

**Files:**
- Create: `packages/headless-agents/src/headless_agents/locks.py`
- Test: `tests/unit/headless_agents/test_locks.py`

**Interfaces:**
- Produces:

```python
LOCK_WAIT_SECONDS: Final = 10.0

class LockTimeout(Exception):
    """A lock not obtained within its bound; str() says which and why it matters."""

class Rank(IntEnum):
    LIFECYCLE = 1; UNCONFINED = 2; LINEAGE_REGISTRY = 3; LINEAGE = 4

@contextmanager
def held(path: Path, *, rank: Rank, exclusive: bool, wait: float | None = LOCK_WAIT_SECONDS,
         what: str, key: str = "") -> Iterator[None]
    # os.open(path, O_RDWR|O_CREAT|O_CLOEXEC, 0o600); flock with LOCK_NB polled every 50 ms
    # until `wait` (None = no wait: one try); raises LockTimeout(what); released in finally.
    # Enforces the order per thread on (rank, key): taking a lower rank, or within
    # Rank.LINEAGE a key (owner id) not greater than the last one held, raises
    # RuntimeError("lock order violated: ...") -- a programming error, never a user error.

def is_free(path: Path) -> bool      # a non-blocking SHARED acquisition succeeds (stale tests)
```

- [ ] **Step 1: Failing tests** — exclusive excludes shared across processes
  (`multiprocessing` child holds exclusive, parent's shared times out with `wait=0.3`);
  shared+shared coexist; a lock dies with its process (child takes exclusive then
  `os._exit(0)`; parent acquires immediately); a subprocess spawned while a lock is held does
  not inherit the descriptor (`/proc/<pid>/fd` of a `sleep` child contains no link to the lock
  file); order violation raises `RuntimeError`; `is_free` is `True` while another process
  holds it **shared** (shared locks coexist: a writer holds its lineage lock exclusive,
  readers hold it shared, and only a live writer makes the probe fail), `False` while another
  process holds it **exclusive**, and `True` again once that process has exited.
- [ ] **Step 2–4:** fail, implement, pass. **Step 5: Commit** — `feat(headless-agents): ordered flock locks with bounded waits`

### Task 11: Run registry — mint, register, resolve, status, incomplete

**Files:**
- Create: `packages/headless-agents/src/headless_agents/runs.py`
- Test: `tests/unit/headless_agents/test_runs.py`

**Interfaces:**
- Consumes: `state.*`, `locks.*`, `config_paths.state_dir`.
- Produces:

```python
RUN_ID_PATTERN: Final = re.compile(r"\d{8}T\d{6}-[0-9a-f]{8}")
FINAL_STATUSES: Final = frozenset({"answered", "failed", "committed", "no_change", "approved", "changes"})

@dataclass(frozen=True)
class Entry:
    run_id: str; run_dir: Path; repository: Path | None; target: dict[str, str]
    lineage: str | None; status: str | None; cleaned_at: str | None

class Registry:
    def __init__(self, state: Path, *, runs_root: Path) -> None
    def mint(self) -> str                                   # UTC id; never registered yet
    def register(self, *, run_dir: Path | None, target: Mapping[str, str],
                 repository: Path | None, lineage: str | None) -> Entry
        # loop: run_id = self.mint(); path = run_dir or runs_root / run_id;
        # create_once(runs/<run_id>.json, {..., "run_dir": str(path)}); on FileExistsError
        # mint again (Review Focus 4). The default run directory is therefore always built
        # from the id that was actually registered.
    def lifecycle_lock(self, run_id: str) -> Path           # runs/<id>.lock
    def resolve(self, run_id: str) -> Entry                 # RUN_ID_PATTERN else UsageError-like
                                                            # RegistryError; Unknown propagates
    def set_status(self, run_id: str, status: str) -> None  # runs outside any lineage only
    def set_cleaned(self, run_id: str, when: str) -> None
    def effective_status(self, entry: Entry, lineage_status: str | None) -> str
        # final → itself; not final and lifecycle lock free → "incomplete"; else "running"

def make_run_dir(run_dir: Path, *, forbidden: Sequence[Path]) -> None
    # exclusive os.mkdir (parents created first); refuses an existing directory and any
    # run_dir inside one of `forbidden` (the repository, the state dir, the runs root's
    # other runs) -- Review Focus 3 -- raising RegistryError naming which.
class RegistryError(ValueError): ...
```

- [ ] **Step 1: Failing tests**, including the two Review Focus cases:

```python
def test_a_colliding_mint_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = Registry(tmp_path)
    ids = iter(["20260925T000000-aaaaaaaa", "20260925T000000-aaaaaaaa", "20260925T000000-bbbbbbbb"])
    monkeypatch.setattr(registry, "mint", lambda: next(ids))
    first = registry.register(run_dir=tmp_path / "r1", target={"kind": "role", "name": "x"},
                              repository=None, lineage=None)
    second = registry.register(run_dir=tmp_path / "r2", target={"kind": "role", "name": "x"},
                               repository=None, lineage=None)
    assert (first.run_id, second.run_id) == ("20260925T000000-aaaaaaaa", "20260925T000000-bbbbbbbb")


@pytest.mark.parametrize("inside", ["repo", "state", "other-run"])
def test_a_run_dir_inside_a_forbidden_tree_is_refused(tmp_path: Path, inside: str) -> None:
    roots = {"repo": tmp_path / "repo", "state": tmp_path / "state", "other-run": tmp_path / "runs" / "x"}
    for root in roots.values():
        root.mkdir(parents=True)
    with pytest.raises(RegistryError, match=inside.split("-")[0]):
        make_run_dir(roots[inside] / "mine", forbidden=list(roots.values()))
```

  plus: with `run_dir=None` the registered `run_dir` is `runs_root / entry.run_id` (the id
  that was registered, including after a collision retry); an existing `--run-dir` refused;
  two custom run dirs of the same name under two
  parents register two ids (spec §4); a registry entry whose file is corrupt resolves to
  `Unknown`; `effective_status` gives `incomplete` for a `running` entry whose lifecycle lock
  is free and `running` while a child process holds it.
- [ ] **Step 2–4:** fail, implement, pass. **Step 5: Commit** — `feat(headless-agents): run registry with engine-minted ids`

### Task 12: `run.json` — the report

**Files:**
- Create: `packages/headless-agents/src/headless_agents/report.py`
- Test: `tests/unit/headless_agents/test_report.py`

**Interfaces:**
- Produces:

```python
RUN_JSON: Final = "run.json"
RUN_KEYS: Final = ("schema", "run_id", "target", "status", "exit_code", "verdict", "text",
    "repository", "base", "head", "branch", "lineage", "continues", "findings_from",
    "implement_providers", "commits", "failure_reason", "vendor_check", "cleanup", "pid",
    "started_at", "duration_seconds", "cost_usd", "cost_complete", "steps")
STEP_KEYS: Final = ("index", "slot", "role", "dir", "provider", "model", "model_reported",
    "exit_code", "duration_seconds", "tokens", "cost_usd", "tools", "verdict")

def new_report(*, run_id, target, repository, pid, started_at) -> dict[str, object]
def step_entry(*, index: int, slot: str, role: str, step_dir: str, result: RunResult) -> dict
    # tools: None in lot 1 (tool counters are lot 2), tokens from result.tokens or None
def with_step(report, step) -> dict            # appends; recomputes cost_usd / cost_complete
def write_report(run_dir: Path, report) -> None  # state.publish(run_dir / RUN_JSON, report)
def step_dir_name(index: int, slot: str, role: str) -> str   # f"{index:02d}-{slot}-{role}"
```

- [ ] **Step 1: Failing tests** — the key set equals `RUN_KEYS` exactly (spec §3.10 "key set
  pinned by a test"); a step's key set equals `STEP_KEYS`; `cost_usd` is the sum and
  `cost_complete` is `False` when any step's cost is `None`; `null`s for a one-step read-only
  run's write fields; `step_dir_name(1, "run", "codex") == "01-run-codex"`.
- [ ] **Step 2–4:** fail, implement, pass. **Step 5: Commit** — `feat(headless-agents): run.json, the run report`

### Task 13: `engine.plan()` — every gate without git

**Files:**
- Create: `packages/headless-agents/src/headless_agents/engine.py` (plan half)
- Test: `tests/unit/headless_agents/test_engine_plan.py`

**Interfaces:**
- Consumes: `roles`, `cli_models`, `mcp_profiles`, `context`, `registry`, `runs`.
- Produces:

```python
class UsageError(ValueError): ...                      # exit 2, nothing ran

@dataclass(frozen=True)
class Overrides:                                        # ha run options; None = not given
    model: str | None = None; effort: str | None = None; timeout: float | None = None
    context: ContextLevel | None = None; context_parents: bool | None = None
    mcp: str | None = None; write: bool | None = None; shell: bool | None = None
    base_url: str | None = None; key_env: str | None = None

@dataclass(frozen=True)
class Request:
    target: str
    prompt: str | None                                  # None = absent (CLI resolved '-')
    stdin_is_tty: bool
    overrides: Overrides
    base: str | None                                    # --base
    repo: Path | None                                   # --repo, unresolved (P2)
    run_dir: Path | None                                # --run-dir
    cwd: Path
    environ: Mapping[str, str]
    home: Path

@dataclass(frozen=True)
class Plan:
    request: Request
    role: Role                                          # after overrides
    models: Mapping[str, str]                           # per link provider
    prompt: str
    mcp: McpServer | None
    environment: dict[str, str]                         # operator_environment (+ NO_PROXY)
    run_dir: Path | None                                # None = default under the runs root
    state: Path

def plan(request: Request) -> Plan
```

  `operator_environment`, `DEFAULT_CREDENTIALS`, `DEFAULT_EXECUTABLES`, `runs_root` move from
  `cli.py` into `engine.py` unchanged. The context bundle is **not** built in `plan()`: its
  `full` level reads repository files and the repository is resolved in `execute()` (P2);
  `plan()` checks the prompt size against the prompt plus the role instructions plus the
  user-level files (a lower bound), and `execute()` re-checks with the real bundle.

- [ ] **Step 1: Failing tests** — the engine refuses, as `UsageError`, every combination the
  0.4.0 CLI refused (spec §3.4, "a unit test drives the engine API directly"):

```python
@pytest.mark.parametrize(
    ("target", "overrides", "rule"),
    [
        ("codex", Overrides(shell=True), "shell requires write"),
        ("openrouter", Overrides(write=True), "write needs a CLI rail"),
        ("openrouter", Overrides(mcp="brain-read"), "mcp needs a CLI rail"),
        ("openai-compat", Overrides(), "openai-compat needs base_url and key_env"),
        ("codex", Overrides(base_url="http://x"), "apply to openai-compat only"),
        ("nobody", Overrides(), "unknown target 'nobody'"),
    ],
)
def test_the_engine_refuses_what_the_cli_refuses(env: Env, target: str, overrides: Overrides, rule: str) -> None:
    with pytest.raises(UsageError, match=rule):
        plan(env.request(target, "task", overrides=overrides))


def test_base_needs_a_write_run(env: Env) -> None:
    with pytest.raises(UsageError, match="--base needs a write run"):
        plan(env.request("codex", "task", base="main"))


def test_no_prompt_on_a_terminal_is_refused(env: Env) -> None:
    with pytest.raises(UsageError, match="no prompt"):
        plan(env.request("codex", None, stdin_is_tty=True))


def test_an_empty_prompt_from_a_closed_pipe_is_refused(env: Env) -> None:
    """Review Focus 2: the CLI hands the engine '' read from a closed/empty stdin."""
    with pytest.raises(UsageError, match="no prompt"):
        plan(env.request("codex", "", stdin_is_tty=False))


def test_every_link_is_checked_for_prompt_size(env: Env) -> None:
    env.roles('[r]\nchain = ["codex:m", "agy"]\n')
    with pytest.raises(UsageError, match=r"agy.*120000.*bytes"):
        plan(env.request("r", "x" * 130_000))


def test_role_instructions_count_against_the_claude_bound(env: Env) -> None:
    env.roles(f'[r]\nprovider = "claude"\nmodel = "m"\ninstructions = """{"i" * 131_100}"""\n')
    with pytest.raises(UsageError, match="131071"):
        plan(env.request("r", "short"))


def test_plan_runs_no_git(env: Env, fake_git_that_fails: None) -> None:
    plan(env.request("codex", "task", overrides=Overrides(model="m")))
```

  (`Env` is a fixture holding a `home`, a config dir and `roles()`/`request()` helpers;
  `fake_git_that_fails` puts a `git` on `PATH` that writes a marker and exits 99, and the
  fixture asserts at teardown that the marker is absent.)
- [ ] **Step 2–4:** fail, implement, pass. **Step 5: Commit** — `feat(headless-agents): engine.plan holds every gate that needs no git`

### Task 14: `engine.execute()` — one-step runs

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/engine.py` (execute half)
- Delete: `packages/headless-agents/src/headless_agents/cli_write.py` (write runs are refused in PR B, P5; the write flow returns in Task 19)
- Modify: `tests/unit/headless_agents/test_cli_write.py` → skipped module-wide with `pytest.skip("write runs return with the spec 3.8.3 protocol, Task 19", allow_module_level=True)`; Task 19 moves every case into `test_write_flow.py`
- Test: `tests/unit/headless_agents/test_engine_execute.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class Outcome:
    exit_code: int
    run_id: str
    run_dir: Path
    report: Mapping[str, object]         # the final run.json
    final: RunResult | None              # the step's final link result

def execute(plan: Plan, *, say: Callable[[str], None]) -> Outcome
```

  Order inside `execute()` for a read-only step: `repo.discover()` (P2, filesystem only) →
  register (Task 11, `repository = identity.work_tree`) → create the run dir →
  hold the lifecycle lock (exclusive, no wait) → hold the unconfined lock **shared** (10 s) →
  the quarantine checks (added in Task 18) → the isolation gate (Task 15b) → the first git
  command, if any → build the context bundle with the role's instructions
  (Task 4) and re-check the prompt size → write `prompt.md` and the initial `run.json`
  (`running`) → run the chain in `steps/01-run-<role>/` exactly as 0.4.0 `run_links` did →
  write the final `run.json` and set the registry status (`answered` or `failed`). A write
  step (`role.write`, after overrides) is **refused in PR B** — `plan()` raises
  `UsageError("write runs are not available in this build: the write protocol of spec
  §3.8.3 is not merged yet")` (P5). `cli_write.py` is deleted in this PR and its 0.4.0 tests
  are kept, skipped with a reason pointing at Task 19, which re-expresses them against the
  §3.8.3 write flow; `write_flow.py` is created in PR C, not here.

- [ ] **Step 1: Failing tests** — first `repo.discover` (new module
  `packages/headless-agents/src/headless_agents/repo.py`, `tests/unit/headless_agents/test_repo.py`):
  a plain checkout, a linked worktree (its `.git` file → git dir → `commondir`), a start
  directory below the work tree, a symlinked `.git` refused, no repository → `None`; and a
  fake `git` on `PATH` proving `discover` never runs it. Then, with the recording `_Fake`
  provider of `test_cli.py` moved into `tests/unit/headless_agents/conftest.py`:
  - a one-step run writes `run.json` with `status == "answered"`, `steps[0]["dir"] ==
    "steps/01-run-codex"`, and the step directory holds the fake's `result.json`;
  - an exhausted chain returns its last link's code (`3` then `4` → `4`), recorded as the
    step's `exit_code` and the run's;
  - the registry status is `answered` / `failed`;
  - **Review Focus 5 (SIGINT), end to end:** a real `ha` process (`sys.executable -m
    headless_agents.cli run claude "task" -m m`, `HOME`/`XDG_*` pointed at the test's tree)
    runs against a fake `claude` on `PATH` that writes its pid then `sleep 30`; once the pid
    file exists the test sends `SIGINT` to the `ha` process; assert `ha` exits `130`, the
    fake claude's process group is gone within 6 s, the run's lifecycle and unconfined locks
    are free (`locks.is_free`), the registry entry's status is still `running` and
    `effective_status` reads `incomplete`, and a second `ha run` starts at once (its
    lifecycle acquisition measured under 1 s). `cli.main` maps `KeyboardInterrupt` to exit
    `130` after the engine's `finally` blocks have run; the fake isolation proof for
    `claude` is recorded in the test's state directory first (Task 15b);
  - a write role and `--write` are both refused with exit 2 and the P5 message;
  - an unconfined-lock holder (a child process holding it exclusive) makes a read-only run
    wait, then fail with `UsageError("an unconfined write is running")` after the bound
    (`LOCK_WAIT_SECONDS` monkeypatched to 0.3).
- [ ] **Step 2–4:** fail, implement, pass.
- [ ] **Step 5: Commit** — `feat(headless-agents): engine.execute runs one-step read-only roles and writes run.json`

### Task 15: The CLI becomes a thin adapter — `ha run TARGET`, `ha roles`, `ha clean`

**Files:**
- Rewrite: `packages/headless-agents/src/headless_agents/cli.py`
- Modify: `packages/headless-agents/README.md` (CLI synopsis only, P8)
- Test: `tests/unit/headless_agents/test_cli.py` (rewritten for the new grammar)

**Interfaces:**
- Consumes: `engine.plan`, `engine.execute`, `engine.UsageError`, `roles.load_roles`,
  `runs.Registry`.
- Produces: the grammar of spec §3.9 minus the lot 2–4 commands:

```text
ha run TARGET [PROMPT | -] [-m MODEL] [--effort E] [--timeout S] [--context L]
       [--context-parents] [--mcp PROFILE] [--write] [--shell] [--base REF] [--repo PATH]
       [--base-url URL] [--key-env VAR] [--json] [--run-dir DIR]
ha roles [--json]
ha providers [--json]
ha runs [--limit N] [--json]        (lot 1: minimal, P7)
ha clean RUN_ID
ha --version
```

  `-p` and `--chain` are gone: `ha run -p codex x` exits `2` with "`-p` was removed in 0.5.0:
  the provider is the TARGET (`ha run codex …`); a chain is declared in roles.toml".
  `ha run --help` ends with the exit codes of a step (0, 1, 2, 3, 4, 5, 124) and three
  examples. On stdout: the final text, preceded for a write run by the run id, the branch,
  the diffstat and the patch path; `--json` prints `run.json`. `ha roles` lists every
  declared role resolved (provider or chain with each link's model, effort, timeout,
  context, MCP profile, write/shell, instructions size in bytes) and exits `2` on an invalid
  file naming each problem. `ha clean RUN_ID` resolves through the registry, takes the
  lifecycle lock without waiting (active run → exit `2`), removes a worktree through git as
  0.4.0 did and deletes the run dir, sets `cleaned_at`; a run that never started (entry, no
  lineage state, no worktree) is simply removed.

- [ ] **Step 1: Failing tests** — rewrite `test_cli.py` against the new grammar: every 0.4.0
  case re-expressed with `ha run <provider>`; `-p` and `--chain` refused with the message
  above; a declared role runs with its instructions in the spec's context; overrides reach
  the spec (`-m`, `--effort`, `--timeout`); `ha roles` text and `--json`; `ha roles` on an
  invalid file exits `2` naming the file and rule; `ha run --help` contains "Exit codes" and
  three `ha run` examples; `ha clean` of an active run exits `2`; `ha clean` of a finished
  read-only run removes its directory and keeps its registry entry with `cleaned_at`; the
  README synopsis block mentions `ha run TARGET` and not `-p`.
- [ ] **Step 2–4:** fail, implement, pass; run the whole package suite and
  `tests/unit/agents` (the Dream must be untouched).
- [ ] **Step 5: Commit** — `feat(headless-agents)!: ha run TARGET over the engine; -p and --chain removed`

### Task 15b: Isolation proofs and the executor gate

**Files:**
- Create: `packages/headless-agents/src/headless_agents/proofs.py` (isolation half)
- Create: `tests/live/headless_agents/test_proofs_live.py` (isolation half)
- Modify: `engine.py` (gate in `execute()`), `cli.py` (`ha roles` column)
- Test: `tests/unit/headless_agents/test_proofs.py`

**Interfaces:**
- Produces:

```python
CLI_RAILS: Final = ("claude", "codex", "agy", "opencode")
@dataclass(frozen=True)
class ProofRecord:
    rail: str; version: str | None
    isolation: tuple[bool, str] | None          # (passed, ISO date)
    confinement: tuple[bool, str] | None        # filled by Task 22
def record_proof(state: Path, rail: str, *, version: str | None,
                 isolation: bool | None = None, confinement: bool | None = None) -> None
    # merges into <state>/proofs/<rail>.json (state.publish); a new version replaces the record
def read_proof(state: Path, rail: str) -> ProofRecord | None
def isolation_ok(state: Path, rail: str, version: str | None) -> bool
    # an HTTP provider: True (P4); a CLI rail: a record for exactly this version, isolation passed
def plant_isolation_markers(home: Path, rail: str) -> dict[str, Path]
    # the marker text and the sentinel paths the live test plants and later checks
```

  In `execute()`, after the locks and before the step, every link of the role is checked:
  a CLI rail without a passing isolation proof for its probed version is refused
  (`UsageError`, exit 2) naming the rail, the installed version and the command that records
  a proof (`pytest -m live tests/live/headless_agents/test_proofs_live.py -k "isolation and
  <rail>"`). A chain is refused if **any** link is unproven: fault tolerance must not route a
  run onto an unproven executor. `ha roles` shows `isolated (<date>)` or `not proven` per CLI
  rail.

- [ ] **Step 1: Failing unit tests** — a missing record refuses; a record for another version
  refuses; a failed record refuses; an HTTP provider passes without a record; a chain with one
  unproven link is refused before any link runs (the fake providers record no call); `ha
  roles` shows the column.
- [ ] **Step 2: Live tests** (`@pytest.mark.live`, excluded from CI, run by hand on the
  operator machine): per CLI rail, markers planted in that rail's own configuration
  locations — the operator instruction file, a skill, a plugin, a user hook (a script that
  touches a sentinel file when run), a user setting and a user MCP server; a run with and
  without a workspace at context `none` must not echo the marker nor create a sentinel; at
  `global` and `full` the bundle's instruction files arrive exactly once (through the
  preamble). On success the test calls `record_proof(state, rail, version=probe(rail).version,
  isolation=True)`; on failure it records `isolation=False` — never nothing, so `ha roles`
  says why a rail is refused.
- [ ] **Step 3–4:** implement, run the unit tests; run the live isolation suite by hand for
  the four rails and paste its summary in the PR body. A rail whose proof fails stays
  refused: this plan never weakens the gate to make a rail pass.
- [ ] **Step 5: Commit** — `feat(headless-agents): per-rail isolation proofs gate every executor`

**PR B gate:** full project gates; the live isolation run summary in the PR body; PR
"headless-agents 0.5.0 lot 1 (B): state, registry, run.json, engine, isolation gate, new
CLI"; independent codex review; merge under 39f7ea9f.

---

# PR C — the write protocol, quarantines, provenance, proofs

### Task 16: Hardened git with hooks disabled

**Files:**
- Create: `packages/headless-agents/src/headless_agents/gitops.py`
- Test: `tests/unit/headless_agents/test_gitops.py`

**Interfaces:**
- Produces:

```python
GIT_TIMEOUT_SECONDS: Final = 120
def empty_hooks_dir(state: Path) -> Path          # <state>/empty-hooks, 0700, must stay empty
def git(root: Path, args: Sequence[str], environ, *, state: Path, hooks: bool = False,
        tampered: Sequence[str] = ()) -> subprocess.CompletedProcess[str]
    # git_command(root) + (["-c", f"core.hooksPath={empty}"] unless hooks) + args,
    # env=git_environment(environ, root); `hooks=True` is for the engine commit ONLY
```

- [ ] **Step 1: Failing tests** — a `post-checkout` hook planted in the repository does not
  run on `git worktree add` through `gitops.git` (spec §4); it does run through
  `git(..., hooks=True)`; a file appearing in `empty-hooks` makes `git()` raise
  `GitTampered("empty hooks directory is not empty")` before running anything.
- [ ] **Step 2–4.** **Step 5: Commit** — `feat(headless-agents): git with hooks disabled except for the engine commit`

### Task 17: Lineage state and provenance records

**Files:**
- Create: `packages/headless-agents/src/headless_agents/lineage.py`, `provenance.py`
- Test: `tests/unit/headless_agents/test_lineage.py`

**Interfaces:**
- Produces:

```python
# lineage.py
@dataclass(frozen=True)
class PendingWrite:
    run_id: str; providers: tuple[str, ...]; unconfined: bool
    start_tip: str | None; start_reflog: int | None
@dataclass(frozen=True)
class LineageState:
    owner: str; repository: Path; worktree: Path; branch: str; base: str
    members: Mapping[str, str]            # run_id -> status
    pending: PendingWrite | None
    compromised: str | None               # reason, None when sound
def lineage_path(state: Path, owner: str) -> Path       # <state>/lineages/<owner>.json
def lineage_lock(state: Path, owner: str) -> Path       # <state>/lineages/<owner>.lock
def registry_lock(state: Path) -> Path                  # <state>/lineages.lock
def load(state: Path, owner: str) -> LineageState       # state.Unknown on any doubt
def create(state: Path, lineage: LineageState) -> None  # create_once
def save(state: Path, lineage: LineageState) -> None    # publish
def of_repository(state: Path, common_dir: Path) -> list[str]
    # owners whose repository shares this common dir, ascending; an unreadable lineage file
    # is included (it will read as Unknown and refuse)
# provenance.py
MadeBy = Literal["engine", "agent", "hook", "unknown"]
def record(state: Path, sha: str, *, run_id: str, lineage: str, made_by: MadeBy,
           providers: Sequence[str]) -> None           # create_once <state>/provenance/<sha>.json
def lookup(state: Path, sha: str) -> dict[str, object] | None
```

- [ ] **Step 1: Failing tests** — round trip; a lineage file with a mismatched `owner` is
  `Unknown`; `create` twice raises; a provenance record is written once; `of_repository`
  groups two worktrees of one repository and excludes another repository.
- [ ] **Step 2–4.** **Step 5: Commit** — `feat(headless-agents): lineage state and per-commit provenance`

### Task 18: Quarantines — classify, publish, check

**Files:**
- Create: `packages/headless-agents/src/headless_agents/quarantine.py`
- Test: `tests/unit/headless_agents/test_quarantine.py`

**Interfaces:**
- Produces:

```python
Scope = Literal["lineage", "repository", "operator"]
def widest_scope(paths: Sequence[str], *, worktree: Path, git_dir: Path, common_dir: Path,
                 home: Path, environ: Mapping[str, str]) -> Scope
    # the table of spec §3.8.5; any path matching none of the known roots → "operator"
def publish(state: Path, scope: Scope, *, reason: str, run_id: str, paths: Sequence[str],
            common_dir: Path | None) -> None   # repo-<sha256(common_dir)[:16]>.json / operator.json
def check(state: Path, common_dir: Path | None) -> str | None
    # operator first, then repository; returns the refusal message, None when clear;
    # an unreadable quarantine file counts as a quarantine
```

- [ ] **Step 1: Failing tests** — one per row of the §3.8.5 table (a worktree `.git` file →
  lineage; the common dir's `config` → repository; `~/.gitconfig` → operator; a
  `core.hooksPath` outside the repository → operator); `check` returns the operator refusal
  before the repository one; a corrupt quarantine file refuses.
- [ ] **Step 2–4.** **Step 5: Commit** — `feat(headless-agents): quarantine scopes for a fired tripwire`

### Task 19: The write step — §3.8.3 steps 1 to 9 (confined)

**Files:**
- Create: `packages/headless-agents/src/headless_agents/write_flow.py` (the 0.4.0 `cli_write` behaviour where §3.8.3 keeps it)
- Modify: `packages/headless-agents/src/headless_agents/engine.py` (write path)
- Test: `tests/unit/headless_agents/test_write_flow.py` — every case of the skipped
  `test_cli_write.py` re-expressed against the engine (assertions kept where §3.8.3 keeps the
  behaviour, changed only where it changes it — e.g. the commit subject gains `implement`),
  then `test_cli_write.py` deleted

**Interfaces:**
- Produces: `run_write_step(plan, *, run_id, run_dir, state, say) -> WriteOutcome` with
  `WriteOutcome(exit_code, status, failure_reason, commits: tuple[tuple[str, MadeBy], ...],
  branch, base, head, final: RunResult | None)`, implementing, in this order:
  1. admission without git — **every lock first, in the §3.8.2 order, then every state
     check under them** (spec §3.8.3 step 1; codex review of this plan, round 3):
     a. `identity = repo.discover(start)` — filesystem only; `identity.common_dir` keys the
        repository (P2);
     b. lifecycle lock (held by `execute` since registration);
     c. unconfined lock — shared, or exclusive for an unconfined write (Task 20);
     d. lineage **registry** lock, exclusive, **held until the intent of step 2 is
        published**: no lineage can appear while this admission decides, and a review that
        enumerates lineages after it sees this write's pending intent;
     e. under it, compute the owners this write must lock: its own new lineage (owner = its
        run id; its state created with `create_once`) and every **source** lineage whose
        recorded worktree contains `--repo` (§3.8.2); acquire all of their lineage locks
        **in ascending owner order** — own exclusive, sources shared — whatever order they
        were discovered in (`locks.held` enforces ascending keys within `Rank.LINEAGE`);
     f. only now, reading state under all those locks: `quarantine.check(state,
        identity.common_dir)` (operator, then repository); a stale `unconfined-intent.json`
        (Task 20); every lineage of the repository, enumerated with
        `lineage.of_repository` under the held registry lock, read (`Unknown` → refuse) and
        tested for a stale pending write with `locks.is_free(lineage_lock)` (a lock this
        write does not hold); every source lineage known, not compromised, with no pending
        write. A stale pending write → compromise that lineage `unfinalized_write`, publish
        the repository quarantine (and the operator's when that write was unconfined),
        refuse (`UsageError`, exit 2), releasing everything in reverse order;
     No git command has run when step 1 ends; the first one is in step 3.
  2. intent: `pending = PendingWrite(run_id, providers, unconfined, None, None)` saved;
     then the lineage registry lock is released (step 1d);
  3. preparation with `gitops.git` (hooks off): resolve base, `worktree add -b ha/<run_id>`,
     tip must equal base else `preparation_moved_head` (compromise, exit 1);
  4. start point saved (`start_tip`);
  5. the step (chain) with `Workspace(wt, write=True, shell=role.shell)` and the tripwire armed;
  6. tripwire fired → `quarantine.widest_scope` → publish (lineage: compromise), exit 1,
     `failure_reason = "tripwire"`, no git at all, pending write left in place;
  7. `HEAD`/branch tip moved from `start_tip` → every new commit (`rev-list start..tip`)
     recorded `made_by: agent` with the role's providers, lineage compromised,
     `agent_moved_head`, exit 1;
  8. changes present → engine commit with `hooks=True`, message
     `chore(ha): <run_id> implement via <provider>/<model>` for a role write run, or
     `chore(ha): <run_id> residue via …` after a failed step (then exit 1); tip compared again
     whatever `git commit` returned: the engine commit `made_by: engine`, any other new
     commit `made_by: hook` → `hook_committed`; commit refused → `hook_refused`; both
     compromise; no change → exit `5` (`no_change`);
  9. publication: provenance files, then the lineage state (member status + pending cleared)
     in one `publish`, then the report.
  The step names are the section numbers of spec §3.8.3; each is a function of its own in
  `write_flow.py` so a crash can be injected between any two (`_crash_after` test hook
  monkeypatched in tests).

- [ ] **Step 1: Failing tests** (spec §4 engine list, the write part), each with a fake
  provider and a throwaway repository:
  - committed run: branch `ha/<run_id>`, one commit `made_by: engine`, lineage member
    `committed`, pending cleared, `change.patch` = `base..HEAD`;
  - no change → exit 5, nothing committed, member `no_change`;
  - failed step leaving changes → `residue` commit attributed `made_by: engine`, exit 1;
  - fake provider that commits by itself → `agent_moved_head`, its commit recorded
    `made_by: agent`, lineage compromised;
  - hook that creates a commit then fails, and one that amends → `hook_committed`, every new
    commit recorded; refusing hook → `hook_refused`, changes uncommitted, `commit.log` kept;
  - tripwire on the worktree `.git` file → lineage compromised, no git run (fake `git` on
    `PATH` records calls after the step: none);
  - tripwire on the common dir `config` → repository quarantine; a new run in another
    worktree of the same repository refused before any git;
  - crash after the intent and before publication (the `_crash_after("intent")` hook raises
    `SystemExit`) → the next write in that repository compromises the lineage
    `unfinalized_write`, publishes the repository quarantine, and is refused;
  - the worktree's creation is not attributed to the agent (`start_tip` taken after
    preparation);
  - **admission order**, in a repository holding two other lineages (one sound, one with a
    stale pending write): a `git` wrapper on `PATH`, an instrumented `locks.held` and an
    instrumented `quarantine.check` append to one shared event log; assert the log reads
    lifecycle, unconfined, lineage registry, own lineage, **then** the quarantine check,
    then the refusal — with **no** `git` event anywhere; and in a repository with one sound
    other lineage, that the first `git` event comes after the intent publication and after
    the registry lock's release;
  - **a lineage created during admission**: a second process tries to create a lineage in
    the same repository while the first write holds the registry lock between steps 1d and
    2; it waits, and the first write's staleness decision covered a fixed set (the second
    lineage is absent from the first's enumeration and present after);
  - **ascending order with a source lineage**: `--repo` inside the worktree of a lineage
    whose owner id sorts **after** this run's id, and one that sorts **before**: in both
    cases the lineage locks are acquired in ascending owner order (event log), and a
    deliberate descending acquisition in a unit test of `locks.held` raises the
    order-violation `RuntimeError`;
  - a lock-death test: kill the `ha` process (child `multiprocessing` running
    `execute`) during the step → the lineage lock is free at once.
- [ ] **Step 2–4.** **Step 5: Commit** — `feat(headless-agents): the write protocol of spec 3.8.3 for confined roles`

### Task 20: The unconfined path

**Files:**
- Modify: `write_flow.py`, `engine.py`
- Test: `tests/unit/headless_agents/test_write_flow.py`

**Interfaces:**
- Produces: for `role.shell and provider in {"claude", "opencode", "agy"}` (codex keeps its
  sandbox, decision 13): the unconfined lock exclusive (waits ≤ 10 s for running reviews and
  writes, then `UsageError`); `unconfined-intent.json` published and
  `unconfined-writers.json` appended in step 2; the worktree `HEAD` reflog position in the
  start point; step 7 also lists every commit the reflog gained; publication removes the
  intent after the lineage rename. Any process holding the unconfined lock that finds an
  intent treats it as stale → operator quarantine, refuse.

- [ ] **Step 1: Failing tests** — an unconfined write serialises a read-only run (it waits
  on the shared lock, then is refused after the bound); a fake unconfined provider that
  commits then `reset --hard`s its branch → the commit found in the reflog, `made_by:
  agent`; killed before publication in repository A → the first invocation in repository B
  refused with an operator quarantine; a crash between the lineage rename and the intent
  removal → operator quarantine; a new unconfined write finding a leftover intent after
  taking the lock exclusively → operator quarantine, refused.
- [ ] **Step 2–4.** **Step 5: Commit** — `feat(headless-agents): serialise and attribute unconfined writes`

### Task 21: `ha clean` under the lineage rules

**Files:**
- Modify: `cli.py` (`_clean`), `engine.py` (`clean(run_id, ...)` API)
- Test: `tests/unit/headless_agents/test_engine_execute.py`, `test_cli.py`

**Interfaces:**
- Produces: `engine.clean(run_id, *, environ, home, say) -> int`: lifecycle lock without
  waiting (active → `UsageError`); unconfined lock shared; for a lineage member its lineage
  lock; admission checks of §3.8.3 step 1; **no git command** when a quarantine covers the
  repository or the operator, or the lineage is unknown, compromised or holds a pending write
  (exit 1, naming the reason); otherwise the worktree removed through `gitops.git`, the run
  dir deleted, the branch kept, `cleaned_at` set; lineage state and provenance untouched.

- [ ] **Step 1: Failing tests** — `ha clean A` while B (continuing A's lineage in lot 3; in
  lot 1 simulated by holding A's lineage lock in a child) waits then is refused; a
  compromised lineage → exit 1, fake `git` records no call; a cleaned write keeps `committed`
  in its lineage and `cleaned_at` in its registry entry; a never-started run is removed.
- [ ] **Step 2–4.** **Step 5: Commit** — `feat(headless-agents): ha clean never runs git where a write is uncertain`

### Task 22: Confinement proofs, classification, and write runs enabled

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/proofs.py` (confinement half)
- Modify: `tests/live/headless_agents/test_proofs_live.py` (confinement half)
- Modify: `engine.py` (classification; the P5 write refusal removed), `cli.py` (`ha roles`)
- Test: `tests/unit/headless_agents/test_proofs.py`, `test_engine_plan.py`

**Interfaces:**
- Produces:

```python
def confinement(state: Path, rail: str, version: str | None) -> tuple[str, str | None]
    # ("confined", date) only for a passing record of exactly this version; else
    # ("unconfined", date-or-None). A `shell` role on claude/opencode/agy is always unconfined.
def plant_confinement_targets(root: Path, rail: str) -> dict[str, Path]
    # the common git dir file, the ref, the operator git config copy, and one repository
    # under each root the rail treats as writable (codex: /tmp and $TMPDIR)
```

  A write role whose every link is `confined` takes the confined path of Task 19; any
  unconfined link sends the whole write down the unconfined path of Task 20. The P5 refusal
  of write runs in `plan()` is removed in this task, and its test becomes the proof that a
  write run now reaches `write_flow`. `ha roles` shows `confined (<date>)` / `unconfined`
  for write roles.

- [ ] **Step 1: Failing unit tests** — a confined write role takes the confined path, an
  unproven one the unconfined path (observable through the unconfined lock taken exclusive);
  a record for another version is unconfined; `ha roles` shows the column; a write role is
  no longer refused.
- [ ] **Step 2: Live tests** — per rail, a write role asked to write each target of
  `plant_confinement_targets`; each write refused; on success `record_proof(state, rail,
  version=..., confinement=True)`, on failure `confinement=False`.
- [ ] **Step 3–4:** implement, run the unit tests; run the live confinement suite by hand for
  the four rails and paste its summary in the PR body.
- [ ] **Step 5: Commit** — `feat(headless-agents): confinement proofs classify write roles; write runs enabled`

**PR C gate:** full project gates, the live proof run summary in the PR body; PR "headless-
agents 0.5.0 lot 1 (C): write protocol, quarantines, provenance, proofs"; independent codex
review; merge under 39f7ea9f. Then the Brain ticket a5cbb325 (G7) and 2901d5ba are resolved
with the PR links, and fyi b0bfacf1 is acknowledged.

---

## Spec coverage (self-review)

| Spec item (lot 1, §5) | Task |
|---|---|
| roles: loader, validation, implicit roles | 2 |
| roles: instructions in the bundle | 4 |
| model precedence with the role level | 3 |
| trust boundary, config paths | 1 |
| engine for one-step runs, gates moved out of `cli.py` | 13, 14 |
| write flow moved into the engine | 14, 19 |
| `run.json` and `steps/` | 12, 14 |
| §3.8 registry, lifecycle | 9, 10, 11 |
| unconfined and lineage locks | 10, 19, 20 |
| intent and publication, residue commits, per-commit provenance | 17, 19 |
| `agent_moved_head` with the reflog | 19, 20 |
| quarantines | 18, 19, 20 |
| new `ha run` grammar, `ha roles`, `--version`, documented help | 8, 15 |
| `ha clean` through the registry | 15, 21 |
| claude `--safe-mode`, golden fixtures for that flag alone | 5 |
| codex `/tmp` and `$TMPDIR` closed for write roles | 6 |
| providers die with `ha` (PDEATHSIG, own process group) | 7 |
| isolation proof per rail (refuse unproven) | 15b |
| confinement proof per rail (classify) | 22 |
| providers killed on interruption (SIGINT → exit 130) | 7, 14 |
| docstring 2901d5ba | 5 |

Out of lot 1, deliberately: `ha show`, full `ha runs` and tool counters (lot 2);
`workflows.toml`, `ha workflows`, `implement`, `--continue` (lot 3); `review`, the vendor rule,
`--run`, `--findings`, `ha show --dir` (lot 4); README, CHANGELOG, tag (lot 5). The lineage,
provenance and lock machinery of PR C is built so that lots 3 and 4 add shapes, not
protocol.
