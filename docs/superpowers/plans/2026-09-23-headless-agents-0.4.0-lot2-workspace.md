# headless-agents 0.4.0 — Lot 2 (workspace and context bundle) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a caller hand an agent a directory to read (`Workspace(write=False)`, the default) or to edit (`write=True`, optionally `shell=True`) on the four CLI rails, each rail confined by the mechanism measured to hold, and deliver the instructions that agent needs through a context bundle (preamble and, in write mode, an instruction file).

**Architecture:** Additive inside `packages/headless-agents`. `CapabilityProfile` gains `workspace: Workspace | None`; `RunSpec` gains `context: ContextBundle | None`. A new `context` module resolves and composes the bundle; a new `workspace` module holds what the four rails share (validation, the write-taint rule, the prompt composition). Each rail's `build_*_command` / `run_*` takes the workspace as a new keyword argument defaulting to `None`, and with `None` produces exactly today's argv, files and environment: the Dream golden fixtures gate it. agy gets a package-owned guard, shipped as a module and copied into the ephemeral HOME per run.

**Tech Stack:** Python 3.12, standard library, `pydantic` (still the only runtime dependency), `pytest`, `ruff`, `mypy`, `uv` workspace, `git` (only for the agy file listing and the tests' throwaway repositories).

**Spec:** `docs/specs/2026-09-23-headless-agents-0.4.0-design.md`, section 3.3 and the lot-2 row of section 2 (amended by decisions 7 and 8, PR #189). Brain ticket `8ebebf41`. Measurements: Brain learning `1ffcde71` (2026-09-23), summarised in "Measured facts" below.

## Scope

Lot 2 of 4. Lot 3 (`openai-compat`) and lot 4 (`ha` CLI, the `--write` worktree flow, MCP profiles, `allowed_networks`) get their own plans. The version stays `0.3.0`; nothing is tagged before lot 4.

Lot 2 ships the **capability**, not the CLI flow: it does not create worktrees, does not commit, does not choose the context level from flags. Those are lot 4. Lot 2 gives lot 4 everything it calls: `Workspace`, `ContextBundle`, `resolve_context`, `install_instruction_files`, and a rail that honours both.

## Measured facts (2026-09-23, Brain learning `1ffcde71`)

Measured with a file inside the workspace, a file outside it, and a symlink inside it pointing outside. CLI versions: claude 2.1.280, codex 0.156.0, opencode 1.18.30, agy 1.2.9. Measure from a directory **outside `~/.claude`**: claude flags every path under it as a "sensitive file" and refuses writes there, which fakes a confinement result.

| Rail | Read-only | Writable | `shell=True` | Leaks measured |
|---|---|---|---|---|
| claude | `--restricted --permission-mode dontAsk --tools Read,Glob,Grep`, cwd = path: outside reads refused ("--restricted confines the file tools to the working directory"), inside symlink to outside refused | `--restricted --permission-mode acceptEdits --tools Read,Edit,Write,Glob,Grep`: writes inside accepted, outside refused | `Bash` in `--tools` **and** `--allowedTools Bash`; without the latter, Bash is prompted, hence denied in `-p` | With `--allowedTools Bash` (no `--restricted`), `echo > outside` succeeded: the shell is not confined. `--restricted` also ignores user/project/local settings: without it a **trusted** repository's `.claude/settings.json` could widen permissions |
| codex | `--sandbox read-only -C path`, `features.shell_tool=true`: reads **outside** succeed (accepted residual, spec 3.3), every write refused ("Read-only file system") | `--sandbox workspace-write`: writes inside, "read-only file system" outside, network off (DNS fails) | the same shell, inside the same sandbox | `--ignore-user-config` does **not** stop `$CODEX_HOME/AGENTS.md` (the operator's personal file was obeyed). Without an explicit instruction the model declined to even try a read in read-only mode. Events: `item.started`/`item.completed` of type `command_execution` (`status` `in_progress`/`completed`/`failed`) |
| opencode | inline config `tools: {"*": false, read, glob, grep, list: true}`, `permission: {read, glob, grep, list: allow, external_directory: deny, everything else: deny}`, `--dir path`: outside read/grep/list refused | adds `edit`, `write` allow: writes inside accepted, outside refused | adds `bash` allow | `external_directory` **must** be an explicit `deny` (`--auto` approves anything not denied). `read` **follows an inside symlink to an outside file** (returned the outside content). With `bash` allowed, `echo > outside` succeeded (`cat outside` was refused by `external_directory`) |
| agy | package guard resolving `realpath(join(path, arg))`: `view_file` inside allowed, outside and inside-symlink-to-outside refused, `run_command` and `search_web` refused | the same guard allows `write_to_file` / `replace_file_content` inside, refuses outside | `run_command` allowed | Tools: `view_file(AbsolutePath)`, `write_to_file(TargetFile)`, `replace_file_content(TargetFile)`, `run_command(CommandLine, Cwd)`, `search_web`, `schedule`, `send_message`. **No listing or search tool**; `view_file` on a directory fails. Tools require **absolute** paths ("must be an absolute path"). Hook payload: `{"toolCall": {"name", "args"}, "artifactDirectoryPath", "conversationId", "modelName", "stepIdx"}` |

## Operator decisions this plan implements (2026-09-23, after measurement)

| # | Question | Decision |
|---|---|---|
| 9 | agy has no listing or search tool: how does a read-only agy agent find files? | The agy preamble carries the workspace's absolute path and a bounded file listing (`git ls-files --cached --others --exclude-standard`), counted against `max_prompt_bytes`. The guard stays path-confined `view_file` only. |
| 10 | opencode `read` follows an inside symlink to an outside file | Accepted and documented as a residual, like codex's outside reads: refusing workspaces with outward symlinks would reject every repository with a `.venv`. Proven by a `live` test that records the behaviour. |
| 11 | codex reads `$CODEX_HOME/AGENTS.md` despite `--ignore-user-config` | With a workspace, the codex rail runs on an ephemeral `CODEX_HOME` holding only a symlink to `auth.json`: the context bundle is the only channel for instructions. Runs without a workspace (the Dream) are unchanged; a Brain ticket covers the Dream rail. |

## Decisions this plan makes where the spec is silent or wrong

- **No write to any exclude file (spec 3.3 "local exclude file").** Measured: in a linked worktree `git rev-parse --git-path info/exclude` resolves to the **common** `.git/info/exclude`, shared by every checkout of the repository, so the "worktree's local exclude" does not exist: an entry there would hide an untracked `AGENTS.md` in the operator's main checkout. `install_instruction_files` returns the paths it wrote; `RunResult.to_dict()["context"]` lists them; lot 4's carrier commit excludes them by pathspec. The spec gets this amendment in Task 9.
- **Write taint.** With `workspace.write=True`, a run whose stream shows a local write-capable tool started (or, on claude, whose process started at all) never returns `3` or `4`: a chain must not replay a run that may have edited the worktree. Codes become `failure_code_after_a_write(code)` and `124`.
- **`RunSpec.workspace` (codex's `-C`, existing) and `profile.workspace` are mutually exclusive.** Both set raises `ValueError`: two sources for one working directory is a bug waiting.
- **The preamble always names the workspace's absolute path** on every rail, and tells the agent which tools it has. Measured need: agy requires absolute paths, codex declined to read without being told it may.
- **codex `project_doc_max_bytes`:** `0` without a workspace and in read-only mode (unchanged; read-only content travels in the preamble), `65536` in write mode so the installed `AGENTS.md` (repository files measured up to 53.8 KB) is read whole.
- **The agy guard reads its configuration from `$HOME/.gemini/config/workspace-guard.json`**, i.e. from the ephemeral HOME the hook inherits. No argument parsing in a hook command whose splitting rules are unmeasured; missing or unreadable configuration denies everything.
- **The agy file listing is truncated, not refused.** The listing is a map, not the prompt: past 2,000 entries or 32 KiB it ends with a line `… N more entries not listed`. The prompt itself is still never truncated (spec 3.3).

## Global Constraints

- Runtime dependencies stay `pydantic` alone; no import of `brain_v42` or `scripts` (`tests/unit/headless_agents/test_package_boundary.py`).
- "`workspace=None` keeps every rail **byte-for-byte** on today's behaviour: the Dream golden fixtures (3 rails x 6 phases) must stay identical." Gate: `tests/unit/agents/test_golden_commands.py`, `tests/unit/agents/test_chain_golden.py`, `tests/unit/agents/test_providers_delegate.py` pass unchanged.
- "`shell=True` is refused with `ValueError` unless `write=True`."
- "an agy profile carrying both `workspace` and a caller `tool_guard` is rejected with `ValueError`."
- "The ephemeral HOME stays in every case."
- "The preamble counts against `max_prompt_bytes`. […] a bundle that pushes the prompt over the limit fails the run with exit code `2` before any spawn, never by truncation."
- Context levels: `full` (repository + user-level), `global` (user-level only), `none`. Resolution: "`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`" at the repository root, tracked or ignored; parents only on request.
- "Traceability: `result.json` lists every injected file with its size in bytes and its sha256."
- TDD: every behaviour gets a test that fails first; never edit a test to make code pass.
- Gates before every commit: `ruff check .`, `ruff format --check .`, `mypy src/ packages/headless-agents/src/`, and the tests named in the task. Never `--no-verify`.
- Everything committed is in English; Conventional Commits.
- Never set `BRAIN_V42_TEST_DB_URL` for these tests (Brain ticket `e3292865`).
- `live` tests are marked `@pytest.mark.live`, excluded from the default run, run by hand on the operator machine, from a directory outside `~/.claude`.

## Review Focus

1. **`workspace=None` on every rail.** Not one byte of argv, written config, hooks file or child environment may move. Pinned in each rail task by a "no workspace → identical" test, on top of the golden fixtures.
2. **A symlink or `..` in a path argument the agy guard receives.** `realpath` resolution must refuse `path/link-to-outside`, `path/../x`, a relative path, and a path equal to a sibling prefix (`/ws-evil` when the workspace is `/ws`). Pinned in Task 6.
3. **A chain after a write-mode failure.** A writable run that started an edit, then failed or hit its deadline, must never return `3`/`4`. Pinned per rail in Tasks 4-7.
4. **A preamble that pushes an argv rail over its limit.** agy/opencode must exit `2` before spawning, with the byte counts in stderr; claude/codex (stdin) are unaffected. Pinned in Task 3 and in the agy/opencode tasks.
5. **An instruction file already present.** `install_instruction_files` must never overwrite a file the worktree already holds (tracked or not): it writes only where the rail's file is missing. Pinned in Task 2.

---

## Before you start

```bash
git fetch origin main
git worktree add .claude/worktrees/ha-040-lot2 -b feat/headless-agents-lot2-workspace origin/main
cd .claude/worktrees/ha-040-lot2
uv sync --extra dev --python 3.12
.venv/bin/pytest tests/unit/headless_agents tests/unit/agents -q
```

Expected: all green. A red baseline: stop and report.

Blast radius (to re-measure in the worktree; GitNexus if its index is fresh on the canonical root, else these lines — say which):

```bash
git grep -n -E 'build_(claude|codex|opencode|agy)_command|run_(claude|codex|opencode|agy)\(' -- src scripts
git grep -n 'CapabilityProfile(' -- src scripts
```

Every call site passes keywords only; the new `workspace=`/`preamble=` keywords default to `None`, so none needs to change.

---

### Task 1: `Workspace` and `CapabilityProfile.workspace`

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/profile.py`
- Modify: `packages/headless-agents/src/headless_agents/__init__.py` (export `Workspace`)
- Test: `tests/unit/headless_agents/test_profile.py`

**Interfaces:**
- Produces: `Workspace(path: Path, write: bool = False, shell: bool = False)` (frozen); `CapabilityProfile.workspace: Workspace | None = None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/headless_agents/test_profile.py (append)
from pathlib import Path

import pytest
from pydantic import ValidationError

from headless_agents.profile import CapabilityProfile, Workspace


def test_workspace_defaults_to_read_only(tmp_path: Path) -> None:
    workspace = Workspace(path=tmp_path)
    assert (workspace.write, workspace.shell) == (False, False)


def test_workspace_path_must_be_absolute() -> None:
    with pytest.raises(ValidationError, match="absolute"):
        Workspace(path=Path("relative/dir"))


def test_workspace_path_must_exist(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="does not exist"):
        Workspace(path=tmp_path / "missing")


def test_workspace_path_must_be_a_directory(tmp_path: Path) -> None:
    file = tmp_path / "f"
    file.write_text("x", encoding="utf-8")
    with pytest.raises(ValidationError, match="not a directory"):
        Workspace(path=file)


def test_shell_requires_write(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="shell requires write"):
        Workspace(path=tmp_path, shell=True)


def test_shell_with_write_is_accepted(tmp_path: Path) -> None:
    assert Workspace(path=tmp_path, write=True, shell=True).shell is True


def test_profile_has_no_workspace_by_default() -> None:
    assert CapabilityProfile().workspace is None
```

- [ ] **Step 2: Run them — expect FAIL** (`ImportError: cannot import name 'Workspace'`)

Run: `.venv/bin/pytest tests/unit/headless_agents/test_profile.py -q`

- [ ] **Step 3: Implement**

In `profile.py`, before `CapabilityProfile`:

```python
class Workspace(BaseModel):
    """A directory the agent may read, or edit, and nothing outside it.

    Read-only by default: ``write=False`` gives read tools only, confined to
    ``path`` by each rail's own mechanism (see the rails' docstrings for what
    was measured to hold, and the residuals: codex reads outside by design,
    opencode follows an inside symlink). ``shell`` is refused without
    ``write``: on three rails out of four a shell can write whatever the read
    tools cannot, and only codex confines it.
    """

    model_config = _FROZEN

    path: Path
    write: bool = False
    shell: bool = False

    @field_validator("path")
    @classmethod
    def _existing_absolute_directory(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError(f"workspace path must be absolute: {value}")
        if not value.exists():
            raise ValueError(f"workspace path does not exist: {value}")
        if not value.is_dir():
            raise ValueError(f"workspace path is not a directory: {value}")
        return value

    @model_validator(mode="after")
    def _shell_requires_write(self) -> Workspace:
        if self.shell and not self.write:
            raise ValueError("shell requires write: a shell can write what read tools cannot")
        return self
```

In `CapabilityProfile`, after `environment_passthrough`:

```python
    # A directory the agent may read (default) or edit. ``None``: the rail runs
    # exactly as it did before 0.4.0, in its own throwaway directory.
    workspace: Workspace | None = None
```

Export `Workspace` from `__init__.py` next to `CapabilityProfile`.

- [ ] **Step 4: Run — expect PASS**, then `.venv/bin/pytest tests/unit/headless_agents tests/unit/agents -q` (all green).

- [ ] **Step 5: Commit**

```bash
git add packages/headless-agents/src/headless_agents/profile.py packages/headless-agents/src/headless_agents/__init__.py tests/unit/headless_agents/test_profile.py
git commit -m "feat(headless-agents): a workspace capability, read-only by default"
```

---

### Task 2: `context` — resolve the bundle, compose the preamble, install instruction files

**Files:**
- Create: `packages/headless-agents/src/headless_agents/context.py`
- Test: `tests/unit/headless_agents/test_context.py`

**Interfaces:**
- Produces:
  - `ContextLevel = Literal["full", "global", "none"]`
  - `REPOSITORY_FILE_NAMES: tuple[str, ...] = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")`
  - `@dataclass(frozen=True) ContextFile(source: Path, scope: Literal["repository", "user"], content: str, size_bytes: int, sha256: str)`
  - `@dataclass(frozen=True) ContextBundle(level: ContextLevel, files: tuple[ContextFile, ...])` with `.repository_files()`, `.user_files()`, `.preamble(*, include_repository: bool) -> str`, `.to_list() -> list[dict[str, object]]`
  - `resolve_context(*, level: ContextLevel, repository_root: Path | None, user_files: Sequence[Path] = (), include_parents: bool = False) -> ContextBundle`
  - `INSTRUCTION_FILE_BY_RAIL: Mapping[str, str | None] = {"claude": None, "codex": "AGENTS.md", "opencode": "AGENTS.md", "agy": "AGENTS.md"}` — `None`: the rail reads `CLAUDE.md` natively. agy's name is the measured-to-be-confirmed default; Task 8's live test fixes it.
  - `install_instruction_files(bundle: ContextBundle, *, worktree: Path, rail: str) -> tuple[Path, ...]`

Rules (spec 3.3): every level delivers everything it names, nothing twice. User-level content always travels in the preamble. Repository content travels in the preamble in read-only mode (`include_repository=True`) and as a file in write mode. `install_instruction_files` writes only where the rail's file is missing, composing it from the repository's `CLAUDE.md`, and never touches an exclude file (see "Decisions").

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/headless_agents/test_context.py
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from headless_agents.context import (
    ContextBundle,
    install_instruction_files,
    resolve_context,
)


def _git_repo(root: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def test_full_reads_an_ignored_claude_md(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text("CLAUDE.md\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("RULE-IGNORED-7\n", encoding="utf-8")
    bundle = resolve_context(level="full", repository_root=repo)
    assert [f.source.name for f in bundle.repository_files()] == ["CLAUDE.md"]
    assert "RULE-IGNORED-7" in bundle.preamble(include_repository=True)


def test_levels(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    (repo / "AGENTS.md").write_text("repo rule\n", encoding="utf-8")
    user = tmp_path / "user-CLAUDE.md"
    user.write_text("user rule\n", encoding="utf-8")
    full = resolve_context(level="full", repository_root=repo, user_files=[user])
    glob = resolve_context(level="global", repository_root=repo, user_files=[user])
    none = resolve_context(level="none", repository_root=repo, user_files=[user])
    assert {f.scope for f in full.files} == {"repository", "user"}
    assert {f.scope for f in glob.files} == {"user"}
    assert none.files == ()


def test_missing_user_file_is_skipped_not_raised(tmp_path: Path) -> None:
    bundle = resolve_context(
        level="global", repository_root=None, user_files=[tmp_path / "absent.md"]
    )
    assert bundle.files == ()


def test_parents_only_on_request(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("parent rule\n", encoding="utf-8")
    repo = _git_repo(tmp_path / "repo")
    without = resolve_context(level="full", repository_root=repo)
    with_parents = resolve_context(level="full", repository_root=repo, include_parents=True)
    assert without.files == ()
    assert [f.source for f in with_parents.files if f.source.is_relative_to(tmp_path)] == [
        tmp_path / "CLAUDE.md"
    ]


def test_trace_has_size_and_sha256(tmp_path: Path) -> None:
    user = tmp_path / "u.md"
    user.write_bytes("é\n".encode())
    (entry,) = resolve_context(level="global", repository_root=None, user_files=[user]).to_list()
    assert entry == {
        "path": str(user),
        "scope": "user",
        "size_bytes": 3,
        "sha256": hashlib.sha256("é\n".encode()).hexdigest(),
        "installed_as": None,
    }


def test_preamble_without_repository_keeps_user_content(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    (repo / "CLAUDE.md").write_text("repo rule\n", encoding="utf-8")
    user = tmp_path / "u.md"
    user.write_text("user rule\n", encoding="utf-8")
    bundle = resolve_context(level="full", repository_root=repo, user_files=[user])
    preamble = bundle.preamble(include_repository=False)
    assert "user rule" in preamble and "repo rule" not in preamble


def test_empty_bundle_has_empty_preamble() -> None:
    assert ContextBundle(level="none", files=()).preamble(include_repository=True) == ""


def test_install_writes_agents_md_from_claude_md_when_missing(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    (repo / "CLAUDE.md").write_text("repo rule\n", encoding="utf-8")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    bundle = resolve_context(level="full", repository_root=repo)
    written = install_instruction_files(bundle, worktree=worktree, rail="codex")
    assert written == (worktree / "AGENTS.md",)
    assert "repo rule" in (worktree / "AGENTS.md").read_text(encoding="utf-8")


def test_install_never_overwrites_an_existing_file(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    (repo / "CLAUDE.md").write_text("repo rule\n", encoding="utf-8")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "AGENTS.md").write_text("tracked\n", encoding="utf-8")
    bundle = resolve_context(level="full", repository_root=repo)
    assert install_instruction_files(bundle, worktree=worktree, rail="codex") == ()
    assert (worktree / "AGENTS.md").read_text(encoding="utf-8") == "tracked\n"


def test_install_is_a_no_op_for_claude(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    (repo / "CLAUDE.md").write_text("repo rule\n", encoding="utf-8")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    bundle = resolve_context(level="full", repository_root=repo)
    assert install_instruction_files(bundle, worktree=worktree, rail="claude") == ()


def test_install_touches_no_exclude_file(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    (repo / "CLAUDE.md").write_text("repo rule\n", encoding="utf-8")
    exclude = repo / ".git" / "info" / "exclude"
    before = exclude.read_bytes() if exclude.exists() else None
    install_instruction_files(
        resolve_context(level="full", repository_root=repo), worktree=repo, rail="opencode"
    )
    assert (exclude.read_bytes() if exclude.exists() else None) == before


def test_unknown_rail_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown rail"):
        install_instruction_files(
            ContextBundle(level="none", files=()), worktree=tmp_path, rail="nope"
        )
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: headless_agents.context`)

Run: `.venv/bin/pytest tests/unit/headless_agents/test_context.py -q`

- [ ] **Step 3: Implement `context.py`**

```python
"""The context bundle: the instructions a sub-agent needs, delivered once.

Three leaks make a sub-agent work without its instructions: a worktree checks
out tracked files only (an ignored ``CLAUDE.md`` vanishes), each CLI reads a
different file (codex and opencode read ``AGENTS.md``, claude ``CLAUDE.md``),
and the ephemeral HOME drops the operator's user-level files. The bundle
resolves them from the SOURCE repository and the caller's list, and delivers
them through two channels with one rule each:

- the PREAMBLE carries user-level content always, and repository content in
  read-only mode -- where the working directory is the caller's checkout and
  nothing may be written there;
- an INSTRUCTION FILE carries repository content in write mode, where the
  workspace is a fresh worktree the run owns, and only where the rail's own
  file is missing.

No exclude file is written: in a linked worktree ``info/exclude`` is the
COMMON one, shared by every checkout (measured 2026-09-23). The paths written
are returned instead, for the caller's carrier commit to leave out.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

ContextLevel = Literal["full", "global", "none"]
Scope = Literal["repository", "user"]

REPOSITORY_FILE_NAMES: Final[tuple[str, ...]] = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")

#: The instruction file each rail reads natively from its working directory.
#: ``None``: the rail reads ``CLAUDE.md``, nothing to install.
INSTRUCTION_FILE_BY_RAIL: Final[Mapping[str, str | None]] = {
    "claude": None,
    "codex": "AGENTS.md",
    "opencode": "AGENTS.md",
    "agy": "AGENTS.md",
}


@dataclass(frozen=True)
class ContextFile:
    source: Path
    scope: Scope
    content: str
    size_bytes: int
    sha256: str
    installed_as: Path | None = None


def _read(path: Path, scope: Scope) -> ContextFile | None:
    if not path.is_file():
        return None
    raw = path.read_bytes()
    return ContextFile(
        source=path,
        scope=scope,
        content=raw.decode("utf-8", errors="replace"),
        size_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _block(file: ContextFile) -> str:
    return f'<instructions source="{file.source}" scope="{file.scope}">\n{file.content.rstrip()}\n</instructions>'


@dataclass(frozen=True)
class ContextBundle:
    level: ContextLevel
    files: tuple[ContextFile, ...]

    def repository_files(self) -> tuple[ContextFile, ...]:
        return tuple(f for f in self.files if f.scope == "repository")

    def user_files(self) -> tuple[ContextFile, ...]:
        return tuple(f for f in self.files if f.scope == "user")

    def preamble(self, *, include_repository: bool) -> str:
        """User-level content always; repository content only when asked."""
        chosen = self.user_files() + (self.repository_files() if include_repository else ())
        return "\n\n".join(_block(f) for f in chosen)

    def to_list(self) -> list[dict[str, object]]:
        return [
            {
                "path": str(f.source),
                "scope": f.scope,
                "size_bytes": f.size_bytes,
                "sha256": f.sha256,
                "installed_as": None if f.installed_as is None else str(f.installed_as),
            }
            for f in self.files
        ]


def resolve_context(
    *,
    level: ContextLevel,
    repository_root: Path | None,
    user_files: Sequence[Path] = (),
    include_parents: bool = False,
) -> ContextBundle:
    """Read the bundle a level names. Absent files are skipped, never raised."""
    files: list[ContextFile] = []
    if level == "full" and repository_root is not None:
        # Farthest parent first, the repository last: the nearest file reads
        # last, as each CLI layers its own instruction files.
        directories = [repository_root]
        if include_parents:
            directories = [*reversed(repository_root.parents), repository_root]
        for directory in directories:
            for name in REPOSITORY_FILE_NAMES:
                entry = _read(directory / name, "repository")
                if entry is not None:
                    files.append(entry)
    if level in ("full", "global"):
        for path in user_files:
            entry = _read(path, "user")
            if entry is not None:
                files.append(entry)
    return ContextBundle(level=level, files=tuple(files))


def install_instruction_files(
    bundle: ContextBundle, *, worktree: Path, rail: str
) -> tuple[Path, ...]:
    """Write the rail's instruction file into ``worktree`` where it is missing.

    Composed from the repository's ``CLAUDE.md`` (the file every repository of
    this ecosystem carries). Never overwrites: a file already there -- tracked
    or not -- is the repository's own and wins.
    """
    if rail not in INSTRUCTION_FILE_BY_RAIL:
        raise ValueError(f"unknown rail: {rail!r}")
    name = INSTRUCTION_FILE_BY_RAIL[rail]
    if name is None:
        return ()
    target = worktree / name
    if target.exists() or target.is_symlink():
        return ()
    sources = [f for f in bundle.repository_files() if f.source.name == "CLAUDE.md"]
    if not sources:
        return ()
    target.write_text("\n\n".join(f.content.rstrip() for f in sources) + "\n", encoding="utf-8")
    return (target,)
```

- [ ] **Step 4: Run — expect PASS.** `test_parents_only_on_request` compares against the files under `tmp_path` only, because an ancestor of `tmp_path` on the operator machine may hold its own `CLAUDE.md`: write its last assertion as `assert [f.source for f in with_parents.files if f.source.is_relative_to(tmp_path)] == [tmp_path / "CLAUDE.md"]` from the start (Step 1), not after a failure.

- [ ] **Step 5: Commit**

```bash
git add packages/headless-agents/src/headless_agents/context.py tests/unit/headless_agents/test_context.py
git commit -m "feat(headless-agents): a context bundle resolved once, delivered through one channel each"
```

---

### Task 3: `workspace` helpers, `RunSpec.context`, and `RunResult` context/workspace fields

**Files:**
- Create: `packages/headless-agents/src/headless_agents/workspace.py`
- Modify: `packages/headless-agents/src/headless_agents/spec.py` (field `context`)
- Modify: `packages/headless-agents/src/headless_agents/result.py` (fields `context`, `workspace`; `to_dict`)
- Test: `tests/unit/headless_agents/test_workspace.py`, `tests/unit/headless_agents/test_result.py`

**Interfaces:**
- Consumes: `Workspace` (Task 1), `ContextBundle` (Task 2).
- Produces:
  - `RunSpec.context: ContextBundle | None = None`
  - `RunResult.context: tuple[dict[str, object], ...] | None = None`, `RunResult.workspace: dict[str, object] | None = None`; `to_dict()` emits them in place of the two `None`s.
  - `workspace_of(spec: RunSpec) -> Workspace | None` — raises `ValueError` when both `spec.workspace` and `spec.profile.workspace` are set.
  - `workspace_summary(workspace: Workspace | None) -> dict[str, object] | None` → `{"path": str, "write": bool, "shell": bool}`.
  - `rail_preamble(spec: RunSpec, *, tools_note: str) -> str` — `""` without workspace and without context; else the workspace note (absolute path + `tools_note`) followed by `spec.context.preamble(include_repository=not write)`.
  - `prepend(preamble: str, prompt: str) -> str` — `preamble + "\n\n<task>\n" + prompt + "\n</task>"` when the preamble is non-empty, else `prompt` unchanged.
  - `argv_prompt_or_refusal(prompt: str, limit: int) -> str | None` — `None` if it fits, else the refusal message `"prompt with context too long for argv: {n} bytes > {limit}"`.
  - `INVALID_USAGE_EXIT_CODE = 2` (in `capability.py`).

- [ ] **Step 1: Failing tests**

```python
# tests/unit/headless_agents/test_workspace.py
from pathlib import Path

import pytest

from headless_agents.capability import INVALID_USAGE_EXIT_CODE
from headless_agents.context import ContextBundle, ContextFile
from headless_agents.profile import CapabilityProfile, Workspace
from headless_agents.spec import RunSpec
from headless_agents.workspace import (
    argv_prompt_or_refusal,
    prepend,
    rail_preamble,
    workspace_of,
    workspace_summary,
)


def _file(tmp_path: Path, scope: str, text: str) -> ContextFile:
    return ContextFile(source=tmp_path / f"{scope}.md", scope=scope, content=text,  # type: ignore[arg-type]
                       size_bytes=len(text), sha256="0" * 64)


def test_both_workspaces_is_refused(tmp_path: Path) -> None:
    spec = RunSpec(prompt="p", workspace=tmp_path,
                   profile=CapabilityProfile(workspace=Workspace(path=tmp_path)))
    with pytest.raises(ValueError, match="both"):
        workspace_of(spec)


def test_no_workspace_no_context_means_no_preamble() -> None:
    assert rail_preamble(RunSpec(prompt="p"), tools_note="x") == ""


def test_read_only_preamble_names_path_and_carries_repository(tmp_path: Path) -> None:
    bundle = ContextBundle(level="full", files=(_file(tmp_path, "repository", "REPO"),
                                                _file(tmp_path, "user", "USER")))
    spec = RunSpec(prompt="p", context=bundle,
                   profile=CapabilityProfile(workspace=Workspace(path=tmp_path)))
    preamble = rail_preamble(spec, tools_note="read tools")
    assert str(tmp_path) in preamble and "read tools" in preamble
    assert "REPO" in preamble and "USER" in preamble


def test_write_preamble_leaves_repository_to_the_file(tmp_path: Path) -> None:
    bundle = ContextBundle(level="full", files=(_file(tmp_path, "repository", "REPO"),
                                                _file(tmp_path, "user", "USER")))
    spec = RunSpec(prompt="p", context=bundle,
                   profile=CapabilityProfile(workspace=Workspace(path=tmp_path, write=True)))
    preamble = rail_preamble(spec, tools_note="edit tools")
    assert "USER" in preamble and "REPO" not in preamble


def test_prepend_is_identity_without_preamble() -> None:
    assert prepend("", "the task") == "the task"


def test_prepend_wraps_the_task() -> None:
    assert prepend("PRE", "the task") == "PRE\n\n<task>\nthe task\n</task>"


def test_argv_refusal_counts_bytes_not_chars() -> None:
    assert argv_prompt_or_refusal("é" * 3, 6) is None
    assert argv_prompt_or_refusal("é" * 4, 6) == "prompt with context too long for argv: 8 bytes > 6"


def test_invalid_usage_code() -> None:
    assert INVALID_USAGE_EXIT_CODE == 2


def test_summary(tmp_path: Path) -> None:
    assert workspace_summary(None) is None
    assert workspace_summary(Workspace(path=tmp_path, write=True)) == {
        "path": str(tmp_path), "write": True, "shell": False}
```

```python
# tests/unit/headless_agents/test_result.py (append)
def test_to_dict_carries_context_and_workspace() -> None:
    result = RunResult(
        exit_code=0, provider="codex", model="m", report_path=None, events_log=None,
        tokens=None, duration_seconds=1.0, tool_call_completed=False,
        context=({"path": "/u.md", "scope": "user", "size_bytes": 1, "sha256": "a" * 64,
                  "installed_as": None},),
        workspace={"path": "/ws", "write": False, "shell": False},
    )
    data = result.to_dict()
    assert data["context"] == [{"path": "/u.md", "scope": "user", "size_bytes": 1,
                                "sha256": "a" * 64, "installed_as": None}]
    assert data["workspace"] == {"path": "/ws", "write": False, "shell": False}


def test_to_dict_keeps_null_without_context() -> None:
    result = RunResult(exit_code=0, provider="codex", model="m", report_path=None,
                       events_log=None, tokens=None, duration_seconds=1.0,
                       tool_call_completed=False)
    assert (result.to_dict()["context"], result.to_dict()["workspace"]) == (None, None)
```

- [ ] **Step 2: Run — expect FAIL** (imports).

- [ ] **Step 3: Implement**

`capability.py`, next to the other exit codes:

```python
# The run was refused before any spawn because its own inputs cannot work:
# a prompt that, with its context, no longer fits the argv of the rail that
# must take it there. Never a switchover: the next link gets the same input.
INVALID_USAGE_EXIT_CODE = 2
```

`spec.py`: import `ContextBundle` under `TYPE_CHECKING`-free normal import (no cycle: `context` imports nothing from the package) and add after `environment`:

```python
    context: ContextBundle | None = None
```

and extend the module docstring with one paragraph: "``context`` is the resolved context bundle (:mod:`headless_agents.context`); each rail delivers it through its preamble channel."

`result.py`: add fields after `raw_log`:

```python
    context: tuple[dict[str, object], ...] | None = None
    workspace: dict[str, object] | None = None
```

and in `to_dict()` replace `"context": None, "workspace": None,` with:

```python
            "context": None if self.context is None else [dict(entry) for entry in self.context],
            "workspace": None if self.workspace is None else dict(self.workspace),
```

Update the `to_dict` docstring: `branch` alone stays `None` until lot 4.

`workspace.py`:

```python
"""What the four CLI rails share once a run carries a workspace or a context bundle."""

from __future__ import annotations

from .profile import Workspace
from .spec import RunSpec


def workspace_of(spec: RunSpec) -> Workspace | None:
    """The run's workspace. ``RunSpec.workspace`` (codex's legacy ``-C``) and
    ``profile.workspace`` name the same thing: both set is refused."""
    if spec.workspace is not None and spec.profile.workspace is not None:
        raise ValueError("RunSpec.workspace and profile.workspace are both set: pick one")
    return spec.profile.workspace


def workspace_summary(workspace: Workspace | None) -> dict[str, object] | None:
    if workspace is None:
        return None
    return {"path": str(workspace.path), "write": workspace.write, "shell": workspace.shell}


def rail_preamble(spec: RunSpec, *, tools_note: str) -> str:
    """The workspace note, then the bundle's preamble for this mode."""
    workspace = workspace_of(spec)
    parts: list[str] = []
    if workspace is not None:
        mode = "read and edit" if workspace.write else "read (no edits)"
        parts.append(
            f"<workspace path=\"{workspace.path}\" mode=\"{mode}\">\n"
            f"Work inside {workspace.path} only; use absolute paths under it. {tools_note}\n"
            "</workspace>"
        )
    if spec.context is not None:
        include_repository = workspace is None or not workspace.write
        block = spec.context.preamble(include_repository=include_repository)
        if block:
            parts.append(block)
    return "\n\n".join(parts)


def prepend(preamble: str, prompt: str) -> str:
    if not preamble:
        return prompt
    return f"{preamble}\n\n<task>\n{prompt}\n</task>"


def argv_prompt_or_refusal(prompt: str, limit: int) -> str | None:
    size = len(prompt.encode("utf-8"))
    if size <= limit:
        return None
    return f"prompt with context too long for argv: {size} bytes > {limit}"
```

- [ ] **Step 4: Run — PASS**; then the package and `tests/unit/agents` suites.

- [ ] **Step 5: Commit**

```bash
git add packages/headless-agents/src/headless_agents/{workspace,spec,result,capability}.py tests/unit/headless_agents/test_workspace.py tests/unit/headless_agents/test_result.py
git commit -m "feat(headless-agents): runs carry a context bundle and report their workspace"
```

---

### Task 4: claude — workspace mapping, preamble on `--append-system-prompt`, write taint

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/providers/claude.py`
- Test: `tests/unit/headless_agents/test_provider_claude.py`

**Interfaces:**
- Consumes: `Workspace`, `workspace_of`, `rail_preamble`, `workspace_summary`.
- Produces: `build_claude_command(..., workspace: Workspace | None = None, append_system_prompt: str | None = None)`; `run_claude(..., workspace: Workspace | None = None, append_system_prompt: str | None = None)`.

Mapping (measured, see "Measured facts"):

| Mode | Flags replacing `--permission-mode bypassPermissions --tools ""` | cwd |
|---|---|---|
| none | unchanged | the temp dir (unchanged) |
| read-only | `--restricted --permission-mode dontAsk --tools Read,Glob,Grep` | `workspace.path` |
| write | `--restricted --permission-mode acceptEdits --tools Read,Edit,Write,Glob,Grep` | `workspace.path` |
| write+shell | as write, `--tools Read,Edit,Write,Glob,Grep,Bash`, and `Bash` appended to `--allowedTools` | `workspace.path` |

`--allowedTools` with a server: `mcp__<server>__<tool>,…` then `,Bash` when shell. `--restricted` stays on with `shell` (it keeps the settings sources off); the shell's own confinement under `--restricted` + `--allowedTools Bash` is **measured by the live test of Task 8** and documented as "none" until then.

Write taint: in write mode, once the process started, `run_claude` never returns `3`: claude's only witness (OTEL) is asynchronous and cannot prove no edit happened. Non-zero → `failure_code_after_a_write(code)`; timeout stays `124`.

- [ ] **Step 1: Failing tests** (`tmp_path` workspaces; `mcp_config_path=tmp_path / "m.json"`)

```python
def test_no_workspace_command_is_unchanged(tmp_path):
    base = build_claude_command(model="m", max_turns=3, mcp_config_path=tmp_path / "m.json", mcp=None)
    assert build_claude_command(model="m", max_turns=3, mcp_config_path=tmp_path / "m.json",
                                mcp=None, workspace=None, append_system_prompt=None) == base


def test_read_only_workspace_flags(tmp_path):
    command = build_claude_command(model="m", max_turns=3, mcp_config_path=tmp_path / "m.json",
                                   mcp=None, workspace=Workspace(path=tmp_path))
    assert "bypassPermissions" not in command
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert command[command.index("--tools") + 1] == "Read,Glob,Grep"
    assert "--restricted" in command


def test_write_workspace_flags(tmp_path):
    command = build_claude_command(model="m", max_turns=3, mcp_config_path=tmp_path / "m.json",
                                   mcp=None, workspace=Workspace(path=tmp_path, write=True))
    assert command[command.index("--permission-mode") + 1] == "acceptEdits"
    assert command[command.index("--tools") + 1] == "Read,Edit,Write,Glob,Grep"
    assert "--allowedTools" not in command


def test_shell_adds_bash_to_tools_and_allowed(tmp_path):
    mcp = McpServer(name="brain", url="http://127.0.0.1:8765/mcp", tools=("brain_search",))
    command = build_claude_command(model="m", max_turns=3, mcp_config_path=tmp_path / "m.json",
                                   mcp=mcp, workspace=Workspace(path=tmp_path, write=True, shell=True))
    assert command[command.index("--tools") + 1].endswith(",Bash")
    assert command[command.index("--allowedTools") + 1] == "mcp__brain__brain_search,Bash"


def test_append_system_prompt(tmp_path):
    command = build_claude_command(model="m", max_turns=3, mcp_config_path=tmp_path / "m.json",
                                   mcp=None, append_system_prompt="PRE")
    assert command[command.index("--append-system-prompt") + 1] == "PRE"
```

For the cwd and the taint, a fake executable written to `tmp_path`:

```python
def _fake(tmp_path, body: str) -> str:
    script = tmp_path / "fake-claude"
    script.write_text(f"#!/usr/bin/env bash\ncat >/dev/null\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


def test_workspace_is_the_cwd(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    answer = tmp_path / "answer.log"
    code = run_claude(prompt="p", model="m", max_turns=1, timeout_seconds=30,
                      raw_log=tmp_path / "raw.log", mcp=None, answer_log=answer,
                      executable=_fake(tmp_path, "pwd"), workspace=Workspace(path=ws))
    assert code == 0 and answer.read_text().strip() == str(ws)


def test_write_mode_failure_is_never_replayable(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    code = run_claude(prompt="p", model="m", max_turns=1, timeout_seconds=30,
                      raw_log=tmp_path / "raw.log", mcp=None,
                      executable=_fake(tmp_path, "exit 3"),
                      workspace=Workspace(path=ws, write=True))
    assert code == 1


def test_read_only_failure_stays_replayable(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    code = run_claude(prompt="p", model="m", max_turns=1, timeout_seconds=30,
                      raw_log=tmp_path / "raw.log", mcp=None,
                      executable=_fake(tmp_path, "exit 7"), workspace=Workspace(path=ws))
    assert code == 3
```

Provider-level test: `ClaudeProvider().run(spec)` with `profile.workspace` and a `context` bundle → the fake receives `--append-system-prompt` whose value contains the workspace path; `result.workspace == workspace_summary(...)` and `result.context == tuple(bundle.to_list())`.

- [ ] **Step 2: Run — FAIL** (unexpected keyword `workspace`).

- [ ] **Step 3: Implement**

`build_claude_command`: add the two keywords; replace the fixed block by

```python
    if workspace is None:
        permission_mode, tools = "bypassPermissions", ""
    else:
        tool_list = ["Read", "Edit", "Write", "Glob", "Grep"] if workspace.write else ["Read", "Glob", "Grep"]
        if workspace.shell:
            tool_list.append("Bash")
        permission_mode = "acceptEdits" if workspace.write else "dontAsk"
        tools = ",".join(tool_list)
    command = [executable, "-p", "-", "--model", model, "--max-turns", str(max_turns)]
    if workspace is not None:
        # --restricted: file tools confined to the working directory, user,
        # project and local settings ignored -- a trusted repository's own
        # .claude/settings.json cannot widen what this run may do (measured).
        command.append("--restricted")
    command.extend(("--permission-mode", permission_mode, "--tools", tools))
    allowed = [f"mcp__{mcp.name}__{tool}" for tool in mcp.tools] if mcp is not None else []
    if workspace is not None and workspace.shell:
        allowed.append("Bash")
    if allowed:
        command.extend(("--allowedTools", ",".join(allowed)))
    if append_system_prompt:
        command.extend(("--append-system-prompt", append_system_prompt))
    command.extend(("--mcp-config", str(mcp_config_path), "--strict-mcp-config"))
    return command
```

Check the no-workspace argv is element-for-element today's (the first test and the golden fixtures pin it; note `--allowedTools` is emitted only with a server, exactly as before).

`run_claude`: add the keywords; pass them to `build_claude_command`; `cwd=workspace.path if workspace is not None else runtime_dir`; after the exit code is known:

```python
    if workspace is not None and workspace.write:
        # claude cannot prove that no edit happened (asynchronous OTEL): once
        # the process ran in a writable workspace, nothing is replayable.
        return failure_code_after_a_write(exit_code)
```

placed after the `0` and `124` returns, before the `tool_call_completed` branch.

`ClaudeProvider.run`: `workspace = workspace_of(spec)`; `preamble = rail_preamble(spec, tools_note=_TOOLS_NOTE[mode])` where

```python
_TOOLS_NOTE = {
    "read": "Use Read, Glob and Grep to explore it; you cannot edit.",
    "write": "Use Read, Glob, Grep, Edit and Write; edit only what the task needs.",
}
```

(`mode = "write" if workspace and workspace.write else "read"`); pass `append_system_prompt=preamble or None` and `workspace=workspace` to `run_claude`; set `workspace=workspace_summary(workspace)` and `context=None if spec.context is None else tuple(spec.context.to_list())` on the `RunResult`.

- [ ] **Step 4: Run — PASS**; golden fixtures green.

- [ ] **Step 5: Commit** — `feat(headless-agents): claude reads or edits a workspace, confined by --restricted`

---

### Task 5: codex — sandbox per mode, shell tool, ephemeral `CODEX_HOME`, write taint

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/providers/codex.py`
- Test: `tests/unit/headless_agents/test_provider_codex.py`

**Interfaces:**
- Produces: `build_codex_command(..., workspace_mode: Workspace | None = None)` (the existing `workspace: Path` argument stays the `-C` directory); `run_codex(..., workspace_capability: Workspace | None = None)`; `build_codex_home(*, root: Path, real_codex_home: Path) -> Path`; `write_tool_started(events_log: Path) -> bool`.

Mapping:

| Mode | `--sandbox` | `features.shell_tool` | `project_doc_max_bytes` | `CODEX_HOME` |
|---|---|---|---|---|
| none | `read-only` | `false` | `0` | unchanged (caller's) |
| read-only | `read-only` | `true` | `0` | ephemeral |
| write | `workspace-write` | `false` | `65536` | ephemeral |
| write+shell | `workspace-write` | `true` | `65536` | ephemeral |

Why the shell in read-only mode: it is codex's only way to read, and the `read-only` sandbox refuses every write it attempts (measured). Why not in write mode without `shell`: codex edits through `apply_patch`, not the shell; `shell=False` must mean no shell. **To confirm by the live test** that codex 0.156 edits with `shell_tool=false` in `workspace-write`; if it cannot, the live test fails and the plan's executor stops and reports (do not silently turn the shell on).

`features.shell_tool` is emitted once: remove `"shell_tool"` from the `_DISABLED_FEATURES` loop when a workspace enables it, never emit the key twice (codex's `-c` last-wins behaviour is unmeasured).

Ephemeral `CODEX_HOME` (decision 11): a `0700` directory under `ephemeral_root(environ)` (tmpfs) or `tempfile`, holding a symlink `auth.json → <real CODEX_HOME>/auth.json`, where real = `environ["CODEX_HOME"]` or `~/.codex`. Missing `auth.json` → return `3` before spawn with stderr `codex auth.json not found under <real>` (provider unavailable, replayable). The child environment gets `CODEX_HOME=<ephemeral>` (override on top of `spec.environment` or `os.environ`). Removed after the run.

`write_tool_started(events_log)`: `True` if any `item.started`/`item.completed` has `item.type` in `{"command_execution", "file_change"}`, or the stream is absent/unreadable (fail-closed). In write mode, `_deadline_exit_code` returns `124` and `_failure_exit_code` returns `failure_code_after_a_write(default)` when `write_tool_started` is `True`.

Preamble: prepended to the stdin prompt with `prepend()`. Tools notes: read → "Read files with your shell (cat, rg, ls): the sandbox allows reads and refuses writes, so reading is expected and safe."; write → "Edit files with apply_patch."; write+shell → "Edit with apply_patch; your shell runs inside the same sandbox, network off."

- [ ] **Step 1: Failing tests**

```python
def _flags(command):
    return [command[i + 1] for i, part in enumerate(command) if part == "-c"]


def test_no_workspace_is_unchanged(tmp_path):
    base = build_codex_command(model="m", reasoning_effort="low", report_log=tmp_path / "r",
                               workspace=tmp_path, mcp=None)
    assert build_codex_command(model="m", reasoning_effort="low", report_log=tmp_path / "r",
                               workspace=tmp_path, mcp=None, workspace_mode=None) == base


def test_read_only_enables_shell_inside_read_only_sandbox(tmp_path):
    command = build_codex_command(model="m", reasoning_effort="low", report_log=tmp_path / "r",
                                  workspace=tmp_path, mcp=None, workspace_mode=Workspace(path=tmp_path))
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "features.shell_tool=true" in _flags(command)
    assert "features.shell_tool=false" not in _flags(command)


def test_write_without_shell(tmp_path):
    command = build_codex_command(model="m", reasoning_effort="low", report_log=tmp_path / "r",
                                  workspace=tmp_path, mcp=None,
                                  workspace_mode=Workspace(path=tmp_path, write=True))
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert "features.shell_tool=false" in _flags(command)
    assert "project_doc_max_bytes=65536" in _flags(command)


def test_write_with_shell(tmp_path):
    command = build_codex_command(model="m", reasoning_effort="low", report_log=tmp_path / "r",
                                  workspace=tmp_path, mcp=None,
                                  workspace_mode=Workspace(path=tmp_path, write=True, shell=True))
    assert "features.shell_tool=true" in _flags(command)


def test_codex_home_links_auth_only(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "auth.json").write_text("{}", encoding="utf-8")
    (real / "AGENTS.md").write_text("personal", encoding="utf-8")
    home = build_codex_home(root=tmp_path / "eph", real_codex_home=real)
    assert sorted(p.name for p in home.iterdir()) == ["auth.json"]
    assert (home / "auth.json").resolve() == real / "auth.json"
    assert home.stat().st_mode & 0o777 == 0o700


def test_write_tool_started(tmp_path):
    log = tmp_path / "e.jsonl"
    log.write_text('{"type":"item.started","item":{"type":"command_execution","status":"in_progress"}}\n')
    assert write_tool_started(log) is True
    log.write_text('{"type":"item.completed","item":{"type":"agent_message"}}\n')
    assert write_tool_started(log) is False
    assert write_tool_started(tmp_path / "absent") is True
```

Run-level tests with a fake `codex` script (the file's existing pattern): (a) with a read-only workspace the child sees `CODEX_HOME` ≠ the caller's and containing `auth.json`, and cwd = workspace (`-C` value); (b) missing real `auth.json` → `3`, no spawn (the fake writes a marker file; assert absent); (c) write mode, fake writes a `command_execution` `item.started` line then exits `3` → run returns `1`; (d) write mode, fake sleeps past the deadline after writing that line → `124`; (e) the prompt on stdin starts with the `<workspace path=` block when a workspace is set, and is byte-identical to the caller's prompt without workspace or context.

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement** — `build_codex_command`: compute `sandbox`, `shell_enabled`, `doc_bytes` from `workspace_mode` per the table; build `overrides` with `("project_doc_max_bytes", doc_bytes)`; build the disabled-feature loop from `_DISABLED_FEATURES` minus `"shell_tool"` when `shell_enabled`, then append `("features.shell_tool", True)`; `--sandbox` value from the table. With `workspace_mode=None` every value equals today's. `build_codex_home` and `write_tool_started` as specified above, with docstrings citing the 2026-09-23 measurement. `run_codex`: new keyword `workspace_capability`; when set, resolve the real `CODEX_HOME`, build the ephemeral home inside a `TemporaryDirectory(dir=ephemeral_root or None)`, set `CODEX_HOME` in the child environment, run `_run(workspace_capability.path)`, apply the taint rules. `CodexProvider.run`: `workspace = workspace_of(spec)`; `prompt = prepend(rail_preamble(spec, tools_note=...), spec.prompt)`; pass `workspace_capability=workspace` (and leave `workspace=spec.workspace` for the legacy `-C`); fill `workspace`/`context` on the result.

- [ ] **Step 4: Run — PASS**; golden fixtures green.

- [ ] **Step 5: Commit** — `feat(headless-agents): codex reads through a read-only sandbox, edits in workspace-write, on its own CODEX_HOME`

---

### Task 6: the agy workspace guard (package-owned)

**Files:**
- Create: `packages/headless-agents/src/headless_agents/guards/__init__.py` (empty docstring module)
- Create: `packages/headless-agents/src/headless_agents/guards/agy_workspace.py`
- Test: `tests/unit/headless_agents/test_agy_workspace_guard.py`

**Interfaces:**
- Produces: `decide(payload: str, config: Mapping[str, object]) -> dict[str, str]` (pure); `main() -> int` (reads stdin, reads `$HOME/.gemini/config/workspace-guard.json`, prints the decision); `GUARD_CONFIG_NAME = "workspace-guard.json"`; config shape `{"root": "<abs path>", "write": bool, "shell": bool}`.

Rules: payload unreadable → deny. `finish`, `send_message` → allow. `call_mcp_tool`, `list_resources`, `read_resource` → allow (the MCP perimeter is the bearer's, as in the Dream guard). `view_file` → `AbsolutePath` confined. `write_to_file`, `replace_file_content`, `multi_replace_file_content` → `TargetFile` confined, only when `write`. `run_command` → allow only when `shell` (its `Cwd`, if present, confined). Everything else (`search_web`, `schedule`, any future tool) → deny. Confinement: the argument must be a non-empty **absolute** string; `os.path.realpath(arg)` must equal `realpath(root)` or start with `realpath(root) + os.sep`. Config missing, unreadable, `root` not absolute → deny everything.

- [ ] **Step 1: Failing tests**

```python
import json
from pathlib import Path

import pytest

from headless_agents.guards.agy_workspace import decide


def _p(name, **args):
    return json.dumps({"toolCall": {"name": name, "args": args}, "stepIdx": 0})


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "in.txt").write_text("x")
    outside = tmp_path / "out.txt"
    outside.write_text("y")
    (root / "link.txt").symlink_to(outside)
    (tmp_path / "ws-evil").mkdir()
    return root


def cfg(ws, write=False, shell=False):
    return {"root": str(ws), "write": write, "shell": shell}


@pytest.mark.parametrize("path, decision", [
    ("{ws}/in.txt", "allow"),
    ("{ws}", "allow"),
    ("{ws}/../out.txt", "deny"),
    ("{ws}/link.txt", "deny"),
    ("{ws}-evil/x", "deny"),
    ("in.txt", "deny"),
    ("", "deny"),
])
def test_view_file_confinement(ws, path, decision):
    assert decide(_p("view_file", AbsolutePath=path.format(ws=ws)), cfg(ws))["decision"] == decision


def test_writes_need_write(ws):
    assert decide(_p("write_to_file", TargetFile=f"{ws}/n.txt"), cfg(ws))["decision"] == "deny"
    assert decide(_p("write_to_file", TargetFile=f"{ws}/n.txt"), cfg(ws, write=True))["decision"] == "allow"
    assert decide(_p("replace_file_content", TargetFile=f"{ws}/../o"), cfg(ws, write=True))["decision"] == "deny"


def test_run_command_needs_shell(ws):
    assert decide(_p("run_command", CommandLine="ls", Cwd=str(ws)), cfg(ws, write=True))["decision"] == "deny"
    assert decide(_p("run_command", CommandLine="ls", Cwd=str(ws)), cfg(ws, write=True, shell=True))["decision"] == "allow"


@pytest.mark.parametrize("name", ["search_web", "schedule", "brand_new_tool"])
def test_everything_else_is_denied(ws, name):
    assert decide(_p(name), cfg(ws, write=True, shell=True))["decision"] == "deny"


@pytest.mark.parametrize("name", ["finish", "send_message", "call_mcp_tool"])
def test_answer_and_mcp_allowed(ws, name):
    assert decide(_p(name), cfg(ws))["decision"] == "allow"


@pytest.mark.parametrize("payload", ["", "not json", "[]", '{"toolCall": 3}'])
def test_unreadable_payload_is_denied(ws, payload):
    assert decide(payload, cfg(ws))["decision"] == "deny"


def test_bad_config_denies_everything(ws):
    assert decide(_p("view_file", AbsolutePath=f"{ws}/in.txt"), {"root": "relative"})["decision"] == "deny"
    assert decide(_p("finish"), {})["decision"] == "deny"


def test_main_reads_config_from_home(ws, tmp_path, monkeypatch, capsys):
    import io
    from headless_agents.guards import agy_workspace
    home = tmp_path / "home"
    (home / ".gemini" / "config").mkdir(parents=True)
    (home / ".gemini" / "config" / "workspace-guard.json").write_text(json.dumps(cfg(ws)))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.stdin", io.StringIO(_p("view_file", AbsolutePath=f"{ws}/in.txt")))
    assert agy_workspace.main() == 0
    assert json.loads(capsys.readouterr().out) == {"decision": "allow"}
```

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement** `agy_workspace.py` — standard library only (it runs under whatever `python3` the shebang names); module docstring states it is the confinement of an agy workspace run (spec 3.3, decision 9), what was measured (tool names, absolute paths, payload shape), and that a missing configuration denies everything. `decide` implements the rules above with a `_confined(value, root)` helper using `os.path.realpath`; decisions are `{"decision": "allow"}` or `{"decision": "deny", "reason": "<short english reason>"}`. `main()` reads `sys.stdin.read()`, loads `Path(os.environ.get("HOME", "")) / ".gemini/config" / GUARD_CONFIG_NAME` (any exception → `{}`), writes `json.dumps(decide(...))` to stdout with no newline, returns `0`. `if __name__ == "__main__": raise SystemExit(main())`.

- [ ] **Step 4: Run — PASS.**

- [ ] **Step 5: Commit** — `feat(headless-agents): a package-owned agy guard that confines tools to a workspace`

---

### Task 7: agy and opencode rails honour the workspace

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/sandbox.py` (`build_ephemeral_home(..., workspace: Workspace | None = None, guard_python: str = sys.executable)`)
- Modify: `packages/headless-agents/src/headless_agents/profile.py` (`ToolGuard` docstring; agy check lives in the rail)
- Modify: `packages/headless-agents/src/headless_agents/providers/agy.py`
- Modify: `packages/headless-agents/src/headless_agents/providers/opencode.py`
- Test: `tests/unit/headless_agents/test_sandbox.py`, `test_provider_agy.py`, `test_provider_opencode.py`

**Interfaces:**
- Consumes: Tasks 1-3, `headless_agents.guards.agy_workspace` (Task 6).
- Produces: `workspace_listing(root: Path, *, max_entries: int = 2000, max_bytes: int = 32768) -> str`; `opencode_config(mcp, workspace: Workspace | None = None)`; `build_opencode_command(..., directory: Path | None = None)`; agy/opencode `write_tool_started(events_log) -> bool`.

**agy.** With `workspace`:
- a profile with both `workspace` and `guard` → `ValueError` raised by `AgyProvider.run` / `run_agy` before anything (spec 3.3). Without `workspace`, a missing guard is refused exactly as today.
- `build_ephemeral_home(workspace=...)`: copies `guards/agy_workspace.py` (via `importlib.resources.files("headless_agents.guards") / "agy_workspace.py"`) to `<home>/.gemini/config/workspace_guard.py`, rewriting line 1 to `#!{guard_python}`, mode `0700`; writes `workspace-guard.json` `{"root", "write", "shell"}`; writes `hooks.json` wiring that path (same shape as today, hook name `workspace-guard`); `trustedWorkspaces: [home, workspace.path]`. With `workspace=None`: byte-identical to today (test).
- Guard proof before spawn: run the copied guard with `HOME=<home>` on three payloads — `view_file` inside → allow, `view_file` of `/` → deny, `run_command` → deny unless `shell` — else exit `1` with `agy workspace guard failed its probe: run refused`.
- cwd = `workspace.path`.
- Preamble: `rail_preamble(spec, tools_note=...)` + a `<files root="...">` block with `workspace_listing(path)`, prepended to the prompt; then `argv_prompt_or_refusal(prompt, MAX_PROMPT_BYTES)`; refusal → write it to `stderr_log`, return `INVALID_USAGE_EXIT_CODE`, no spawn. Tools notes: read → "Your only read tool is view_file with an ABSOLUTE path under the workspace; it cannot list directories: use the file list below."; write adds "write_to_file and replace_file_content, absolute paths under the workspace."
- `workspace_listing`: `git -C root ls-files --cached --others --exclude-standard -z` (10 s timeout); not a git repository or git missing → `"(not a git repository: no file list)"`; entries joined by newlines, cut at `max_entries` or `max_bytes`, with a final `… N more entries not listed` line.
- Write taint: `write_tool_started` = any `step_update` with `step_type == "tool"` and `tool_name` in `{"write_to_file", "replace_file_content", "multi_replace_file_content", "run_command"}` in any state (a denied one also shows `ERROR`, which is fine: conservative), fail-closed on absent/unreadable stream. In write mode it drives `124` vs `4` and `3` vs `failure_code_after_a_write`.

**opencode.** With `workspace`:
- `opencode_config(mcp, workspace)`: `tools` = `{"*": False, "read": True, "glob": True, "grep": True, "list": True}` (+ `"edit"`, `"write"` when `write`; + `"bash"` when `shell`) plus the MCP entries as today; `permission` = every `MACHINE_TOOLS` entry `"deny"` except the enabled tools set to `"allow"`, and `"external_directory": "deny"` always. With `workspace=None`: identical dict (test).
- `build_opencode_command(directory=workspace.path)` → `--dir <path>`; cwd = path. Home unchanged (ephemeral).
- Preamble prepended, then `argv_prompt_or_refusal(prompt, MAX_PROMPT_BYTES)` → `2` before spawn. Tools notes: read → "Use read, glob, grep and list inside the workspace."; write adds "edit and write".
- Write taint: `write_tool_started` = any `tool_use` whose `part.tool` is in `{"edit", "write", "patch", "bash"}`, fail-closed.

- [ ] **Step 1: Failing tests** — for each bullet above one test, concretely:

```python
# test_sandbox.py
def test_home_without_workspace_is_unchanged(tmp_path):
    profile = CapabilityProfile(guard=ToolGuard(path=Path("/abs/guard.sh")))
    a = build_ephemeral_home(root=tmp_path / "a", name="h", profile=profile, real_home=tmp_path)
    b = build_ephemeral_home(root=tmp_path / "b", name="h", profile=profile, real_home=tmp_path, workspace=None)
    for rel in (".gemini/config/hooks.json", ".gemini/config/mcp_config.json",
                ".gemini/antigravity-cli/settings.json"):
        assert (a / rel).read_text().replace(str(tmp_path / "a"), "R") == (b / rel).read_text().replace(str(tmp_path / "b"), "R")


def test_home_with_workspace_installs_the_package_guard(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    home = build_ephemeral_home(root=tmp_path / "r", name="h", profile=CapabilityProfile(),
                                real_home=tmp_path, workspace=Workspace(path=ws, write=True))
    guard = home / ".gemini/config/workspace_guard.py"
    assert guard.stat().st_mode & 0o777 == 0o700
    assert guard.read_text().splitlines()[0] == f"#!{sys.executable}"
    assert json.loads((home / ".gemini/config/workspace-guard.json").read_text()) == {
        "root": str(ws), "write": True, "shell": False}
    hooks = json.loads((home / ".gemini/config/hooks.json").read_text())
    assert hooks["workspace-guard"]["PreToolUse"][0]["hooks"][0]["command"] == str(guard)
    settings = json.loads((home / ".gemini/antigravity-cli/settings.json").read_text())
    assert settings["trustedWorkspaces"] == [str(home), str(ws)]


def test_installed_guard_runs_under_the_ephemeral_home(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("x")
    home = build_ephemeral_home(root=tmp_path / "r", name="h", profile=CapabilityProfile(),
                                real_home=tmp_path, workspace=Workspace(path=ws))
    guard = home / ".gemini/config/workspace_guard.py"
    payload = json.dumps({"toolCall": {"name": "view_file", "args": {"AbsolutePath": str(ws / "a.txt")}}})
    out = subprocess.run([str(guard)], input=payload, capture_output=True, text=True,
                         env={"HOME": str(home), "PATH": os.environ["PATH"]}, check=True)
    assert json.loads(out.stdout) == {"decision": "allow"}
```

```python
# test_provider_agy.py
def test_workspace_and_caller_guard_are_refused(tmp_path):
    profile = CapabilityProfile(guard=ToolGuard(path=tmp_path / "g.sh"), workspace=Workspace(path=tmp_path))
    with pytest.raises(ValueError, match="tool_guard"):
        AgyProvider().run(RunSpec(prompt="p", profile=profile, run_dir=tmp_path / "run"))


def test_preamble_over_argv_limit_exits_2_without_spawn(tmp_path, monkeypatch):
    monkeypatch.setattr("headless_agents.providers.agy.MAX_PROMPT_BYTES", 64)
    marker = tmp_path / "spawned"
    fake = tmp_path / "agy"
    fake.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n")
    fake.chmod(0o755)
    ws = tmp_path / "ws"
    ws.mkdir()
    result = AgyProvider().run(RunSpec(prompt="p" * 10, executable=str(fake),
                                       profile=CapabilityProfile(workspace=Workspace(path=ws)),
                                       run_dir=tmp_path / "run"))
    assert result.exit_code == 2 and not marker.exists()
    assert "too long for argv" in (tmp_path / "run" / "stderr.log").read_text()


def test_listing_of_a_git_repo_and_its_cap(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("x")
    listing = workspace_listing(tmp_path, max_entries=3)
    assert listing.splitlines()[:3] == ["f0.txt", "f1.txt", "f2.txt"]
    assert listing.splitlines()[-1] == "… 2 more entries not listed"


def test_listing_outside_git(tmp_path):
    assert workspace_listing(tmp_path) == "(not a git repository: no file list)"


def test_agy_write_tool_started(tmp_path):
    log = tmp_path / "e.jsonl"
    log.write_text(json.dumps({"step_update": {"step_type": "tool", "tool_name": "write_to_file", "state": "ACTIVE"}}) + "\n")
    assert agy_write_tool_started(log) is True
    log.write_text(json.dumps({"step_update": {"step_type": "tool", "tool_name": "view_file", "state": "DONE"}}) + "\n")
    assert agy_write_tool_started(log) is False
```

```python
# test_provider_opencode.py
def test_config_without_workspace_is_unchanged():
    assert opencode_config(None, None) == opencode_config(None)


def test_read_only_config(tmp_path):
    config = opencode_config(None, Workspace(path=tmp_path))
    assert config["tools"] == {"*": False, "read": True, "glob": True, "grep": True, "list": True}
    permission = config["permission"]
    assert permission["external_directory"] == "deny"
    assert {k for k, v in permission.items() if v == "allow"} == {"read", "glob", "grep", "list"}


def test_write_shell_config(tmp_path):
    config = opencode_config(None, Workspace(path=tmp_path, write=True, shell=True))
    assert {k for k, v in config["permission"].items() if v == "allow"} == {
        "read", "glob", "grep", "list", "edit", "write", "bash"}
    assert config["permission"]["external_directory"] == "deny"


def test_dir_is_the_workspace(tmp_path):
    command = build_opencode_command(model="m", prompt="p", home=tmp_path / "home", directory=tmp_path)
    assert command[command.index("--dir") + 1] == str(tmp_path)


def test_opencode_write_tool_started(tmp_path):
    log = tmp_path / "e.jsonl"
    log.write_text(json.dumps({"type": "tool_use", "part": {"tool": "edit", "state": {"status": "completed"}}}) + "\n")
    assert oc_write_tool_started(log) is True
    log.write_text(json.dumps({"type": "tool_use", "part": {"tool": "read", "state": {"status": "completed"}}}) + "\n")
    assert oc_write_tool_started(log) is False
```

Plus, per rail, the run-level taint tests with a fake executable (write mode, a write tool event then exit `3` → `1`; then deadline → `124`), and the argv refusal for opencode, following the agy test above.

- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** as specified in the bullets. Amend the `ToolGuard` docstring: "The runtime ships no guard for a run WITHOUT a workspace: the script is versioned and tested by the caller […]. A run WITH a workspace uses the package-owned guard (:mod:`headless_agents.guards.agy_workspace`); the two do not compose, and a profile carrying both is refused." Amend the `sandbox.py` module docstring the same way ("The guard is never bundled here" becomes true only without a workspace).
- [ ] **Step 4: Run — PASS**; golden fixtures green.
- [ ] **Step 5: Commit** — two commits: `feat(headless-agents): agy reads and edits a workspace under the package guard` and `feat(headless-agents): opencode reads and edits a workspace, external_directory denied`.

---

### Task 8: `live` tests — the measurements, replayable

**Files:**
- Modify: `pyproject.toml` (marker `live`, excluded by default like `bench_docker`)
- Create: `tests/live/headless_agents/__init__.py`, `tests/live/headless_agents/test_workspace_live.py`

**Interfaces:**
- Consumes: the four providers through `get_provider(name)`.

Add to `markers`: `"live: spends real provider quota on this machine; excluded by default, run with -m live"`, and add `-m 'not live'` to the default `addopts` alongside the existing exclusions (read the current `addopts` first and extend it; do not replace).

The module skips unless `HA_LIVE=1` and the rail's `probe(name).available`. Each test builds, under `Path.home() / ".cache" / "ha-live" / <uuid>` (**not** under `~/.claude`), a `ws/inside.txt` (`INSIDE-<uuid>`), an `outside/secret.txt` (`OUTSIDE-<uuid>`) and `ws/link.txt → outside/secret.txt`, runs the provider with a cheap model from env (`HA_LIVE_MODEL_<RAIL>`, defaults: claude `haiku`, codex `gpt-6-luna` with effort `low`, opencode `opencode-go/glm-5.3-flash`, agy default), and asserts on **the filesystem and the answer**, never on the model's self-report alone:

| Test | Assertion |
|---|---|
| `test_read_inside[rail]` | answer contains `INSIDE-<uuid>` |
| `test_read_outside_refused[rail]` (claude, opencode, agy) | answer does not contain `OUTSIDE-<uuid>` |
| `test_codex_reads_outside_by_design` | answer contains `OUTSIDE-<uuid>` — the documented residual; if it ever stops, update the docs |
| `test_opencode_symlink_residual` | answer contains `OUTSIDE-<uuid>` via `link.txt` — decision 10's residual, recorded |
| `test_symlink_refused[claude, agy]` | answer does not contain `OUTSIDE-<uuid>` |
| `test_write_inside[rail]` | `ws/new.txt` exists with the asked content |
| `test_write_outside_refused[rail]` | `outside/new.txt` does not exist |
| `test_read_only_cannot_write[rail]` | `ws/new.txt` does not exist |
| `test_codex_edits_without_shell` | write mode, `shell=False`: `ws/new.txt` exists (confirms `apply_patch`; if it fails, stop and report) |
| `test_claude_shell_confinement_measured` | write+shell, prompt `echo X > outside/sh.txt`: records whether the file exists and writes the result to the test report (`record_property`); asserts nothing — the spec documents the claude shell as unconfined either way |
| `test_codex_home_is_ephemeral` | a `$CODEX_HOME/AGENTS.md`-dependent sentinel: set a real `CODEX_HOME` to a temp dir holding `auth.json` (symlink to the operator's) and an `AGENTS.md` saying "End every answer with SENTINEL-<uuid>"; answer must NOT contain it |
| `test_ignored_claude_md_is_read_by_codex` (spec 4 acceptance) | temp git repo with an ignored `CLAUDE.md` saying "Answer with the word ZEBRA-<uuid> only"; `resolve_context(level="full", …)`, read-only workspace = repo; codex answer contains `ZEBRA-<uuid>` |
| `test_agy_instruction_file_name` | write mode, `install_instruction_files(rail="agy")` writes `AGENTS.md` with "Answer with ZEBRA-<uuid> only" and **no preamble** (context `global`, empty user list): answer contains `ZEBRA-<uuid>`. If it fails, re-run with `GEMINI.md`; set `INSTRUCTION_FILE_BY_RAIL["agy"]` to the name that passes, in a separate commit citing the run |
| `test_opencode_reads_installed_agents_md` | same as above for opencode (`OPENCODE_DISABLE_PROJECT_CONFIG=1` is set by the rail: this proves it does not also disable `AGENTS.md`) |

Prompts must say "you MUST actually call your tools even if you expect a failure" (measured: without it, codex refused to try).

- [ ] **Step 1:** Write the module; `pytest tests/live -q` without `HA_LIVE` → all skipped; `pytest -q` → the live module is deselected.
- [ ] **Step 2:** On the operator machine: `HA_LIVE=1 .venv/bin/pytest -m live tests/live -v -rA`. Paste the summary into the PR description. A failure is a finding, not a flake: stop, report, do not edit the assertion.
- [ ] **Step 3: Commit** — `test(headless-agents): live proofs of workspace confinement per rail`

---

### Task 9: documentation and the spec amendment

**Files:**
- Modify: `packages/headless-agents/README.md` (a "Workspace and context" section: the per-rail table from "Measured facts", the residuals — codex reads outside, opencode follows inside symlinks, the shell is unconfined on claude/opencode/agy —, a read-only and a writable example)
- Modify: `packages/headless-agents/CHANGELOG.md` (under "Unreleased — 0.4.0": Added `Workspace`, `CapabilityProfile.workspace`, `context` module, `RunSpec.context`, `RunResult.context`/`workspace`, `INVALID_USAGE_EXIT_CODE`, the agy workspace guard; Behaviour: "an agy profile with `workspace` uses the package-owned guard"; Security: "a codex run with a workspace uses an ephemeral `CODEX_HOME`")
- Modify: `docs/specs/2026-09-23-headless-agents-0.4.0-design.md` — add to section 8 a table "Measurement amendments (2026-09-23, lot 2 plan)" with decisions 9, 10, 11 and the "no exclude file" correction (measured shared `info/exclude`); in 3.3 replace "added to the worktree's local exclude file" by "returned to the caller and listed in `result.json`; the carrier commit leaves them out by pathspec (lot 4)", and fill the claude row of the mapping table with `--restricted` and `dontAsk`. The spec is an **English** tracked document: keep it English.

- [ ] **Step 1:** Edit the three files.
- [ ] **Step 2:** Whole-lot verification: `.venv/bin/pytest tests/unit -q` (with `POSTGRES_URL` exported in a worktree, see CLAUDE.md "Tests"), `ruff check .`, `ruff format --check .`, `mypy src/ packages/headless-agents/src/`, `.venv/bin/python scripts/check_module_layering.py`.
- [ ] **Step 3: Commit** — `docs(headless-agents): document the workspace capability and amend the spec with the lot-2 measurements`
- [ ] **Step 4:** `detect_changes({scope: "all"})` (GitNexus), then push the branch and open the PR (English title and body; the live summary from Task 8 in the body).

---

## Follow-up tickets (open them when this plan merges, not before)

- brain-v42 → brain-v42: "The Dream's codex rail reads the operator's `$CODEX_HOME/AGENTS.md` despite `--ignore-user-config`" — decision 11 fixes it for workspace runs only; the Dream needs its own decision (it changes every Dream codex phase's instructions).

---

## Execution notes (added by Task 9, 2026-09-23)

This plan is not rewritten: the tasks above are what was dispatched. What follows is
where the shipped code, and the rulings that produced it, diverged from the plan as
written. Read `docs/specs/2026-09-23-headless-agents-0.4.0-design.md` section 8
("Measurement amendments") for the resolved decisions; this list is the pointer from
each plan task to the ruling that superseded it.

- **Single preamble channel (Task 8 → all of Task 1-4's write-mode design).** The plan's
  two-channel design (an instruction file written into a write-mode workspace, plus the
  preamble everywhere else) was measured live and found broken on three rails out of
  four: claude's `--restricted` does not auto-load the workspace `CLAUDE.md`, opencode's
  `OPENCODE_DISABLE_PROJECT_CONFIG=1` also disables `AGENTS.md`, and agy reads
  `AGENTS.md`/`GEMINI.md` only inside a git repository. Ruling: the preamble becomes the
  single channel for repository instructions, in every mode, on every rail;
  `install_instruction_files`/`INSTRUCTION_FILE_BY_RAIL` (named in Tasks 6-8's briefs)
  were never shipped. Spec 3.3, decision 12.
- **codex `project_doc_max_bytes` = 0 in every mode (Task 5).** A consequence of the
  single-channel ruling above: with the preamble carrying repository content on every
  rail, codex's own native `AGENTS.md` read would otherwise deliver it twice. Set to `0`
  unconditionally, not only in write mode as an earlier reading of 3.3 might imply.
- **opencode write-taint predicate (Task 7 part B, two supersessions).** The brief's "any
  `tool_use`" was replaced first by "any `step_start`" for the deadline path (measured:
  `tool_use` lines are written in their terminal state only, so a call still in flight
  leaves no `tool_use` line, only the `step_start` of the step that issued it), then the
  failure-path predicate itself was inverted: not a fixed set of write-tool names
  (`{edit, write, patch, bash}`, which missed `apply_patch`, and does not track opencode's
  own alias of `bash` as `shell` in some contexts) but "any built-in tool that is neither
  a workspace read tool nor an MCP tool the config itself admitted" — see
  `_is_write_tool_use` in `providers/opencode.py`.
- **claude's 131 071-byte preamble ceiling (Task 4).** Not in the original plan: measured
  that `--append-system-prompt` carries the preamble as one argv element, and the kernel
  refuses a single argument at or above `MAX_ARG_STRLEN` (131072 bytes) with `E2BIG`. A
  preamble past `131071` bytes now refuses with `INVALID_USAGE_EXIT_CODE` before any
  spawn, rather than surfacing as an opaque `OSError` inside `Popen` that the existing
  "binary is missing" handler silently read as a switchover.
- **Task 7 split into 7a and 7b.** Dispatched as one task in the plan; run as two
  (`agy` + `sandbox.py`, then `opencode`) after Task 5 hit its dispatch's turn budget —
  two rails' worth of security-sensitive wiring needed separate review surfaces. Neither
  half changed the other rail's files.
