# headless-agents 0.4.0 — Lot 1 (facade) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every consumer one way to reach a provider by name and one result shape: a provider registry with a zero-quota probe and per-provider prompt limits, `RunResult.text`, a JSON-safe schema-1 `RunResult.to_dict()`, and `RunSpec.run_dir` (one directory per run, fixed log names, `result.json`).

**Architecture:** Additive changes inside `packages/headless-agents`. `RunResult` and `RunSpec` gain defaulted fields, so every existing caller keeps working. A small `run_record` module holds what every rail does after its process exits (read the answer, write `result.json`), and each provider's `run()` calls it; no `build_*_command` or `run_*` function changes, which keeps the Dream byte-for-byte identical (it calls the `run_*` functions, never a provider's `run()`). A new `registry` module maps names to provider classes.

**Tech Stack:** Python 3.12, standard library, `pydantic` (unchanged: the package's only runtime dependency), `pytest`, `ruff`, `mypy`, `uv` workspace.

**Spec:** `docs/specs/2026-09-23-headless-agents-0.4.0-design.md` (approved by the merge of PR #187; Brain ticket `8ebebf41`). This plan implements section 3.1 and the lot-1 row of section 2.

## Scope

This plan covers **lot 1 of 4**. Lots 2 (workspace capability, read-only and writable, and context bundle), 3 (`openai-compat` and its presets) and 4 (`ha` CLI, MCP profiles, `allowed_networks` and the Dream's migration to it) each get their own plan once the previous lot has merged: each lot builds on the interfaces the previous one ships, and lot 2 depends on measurements the spec defers to implementation (the agy guard contract, the opencode 1.18.x permission keys, the claude permission mode of a read-only workspace). The version stays `0.3.0` and nothing is tagged until lot 4, per spec section 5.

Where the spec is silent, this plan decides, and says so:

- `PROVIDER_NAMES` holds the four CLI rails in lot 1. Lot 3 extends it to the spec's eight names when the HTTP providers exist.
- `RunResult.text` is `None` whenever `exit_code != 0`, even if the rail's answer file holds something: a partial or stale report is not an answer.
- `text` is **verbatim**. The claude rail never requests `--output-format json`, so its output is never re-read as an envelope. Re-reading it that way would mangle any answer that is itself JSON with a `result` key, which is exactly what a judge's verdict can look like.
- `run_id` is `run_dir.name` (the lot-4 CLI creates `~/.cache/ha/runs/<run_id>/`), and `None` without a `run_dir`.
- `to_dict()` carries `context`, `workspace` and `branch` from schema 1, as `null`, so that a consumer can pin the key set now. Lots 2 and 4 fill them.
- `logs` in `to_dict()` is `{"report", "events", "stderr", "raw"}` → path string or `null`. To make that possible `RunResult` gains `stderr_log` and `raw_log`.

## Global Constraints

- Runtime dependencies stay `pydantic` alone; no import of `brain_v42` or `scripts` (enforced by `tests/unit/headless_agents/test_package_boundary.py`).
- `to_dict()` fields, in this order: `schema, run_id, provider, model, model_reported, exit_code, text, tokens, cost_usd, duration_seconds, tool_call_completed, context, workspace, branch, logs`, with `"schema": 1`.
- "`null` keeps its meaning "not measured", never zero."
- `run_dir` log names: `report.log`, `events.jsonl`, `stderr.log`, `raw.log`; the run writes `result.json` there. "Explicit log paths still win, so existing callers are untouched."
- `max_prompt_bytes(name)`: `None` for the stdin rails (claude, codex); the existing `MAX_PROMPT_BYTES` for agy and opencode.
- `probe(name) -> Probe(available: bool, detail: str, version: str | None)`: "Zero quota: for a CLI rail, the executable on `PATH` and its `--version` […] Never a model call."
- `get_provider(name)`: "An unknown name raises `UnknownProvider` whose message lists the valid names."
- No change to any `build_*_command` or `run_*` function: the Dream golden fixtures (`tests/unit/agents/test_golden_commands.py`, `tests/unit/agents/test_chain_golden.py`) must pass unchanged.
- TDD is mandatory in this repository: every behaviour gets a test that fails first; never edit a test to make code pass.
- Gates before every commit: `ruff check .`, `ruff format --check .`, `mypy src/ packages/headless-agents/src/`, and the tests named in the task. Never `--no-verify`.
- Everything committed is in English. Commits follow Conventional Commits.
- Never set `BRAIN_V42_TEST_DB_URL` for these tests (Brain ticket `e3292865`: a suite once ran against production through it). The package tests need no database.

## Review Focus

The five input classes most likely to bite a consumer that the spec implies without testing. Each is pinned by a test in the task named on its line.

1. **An answer that is itself JSON** (a judge's verdict, `{"result": "pass", …}`) must come back in `text` exactly as written, never re-read as a claude envelope. Pinned in Task 3 (`answer_text`) and Task 5 (claude rail).
2. **A reused log path.** The claude rail *appends* to `raw_log`, and retries reuse stable paths, so `text` must hold only this run's bytes, never a previous run's answer. Pinned in Task 3 (`offset`) and Task 5.
3. **A failed run that left a report behind.** agy and opencode extract their report before the exit code is judged, so `text` must be `None` on a non-zero exit even when the report file is not empty. Pinned in Task 3 and Task 4 (agy).
4. **`run_dir` combined with explicit log paths, and a `run_dir` that does not exist yet.** The explicit path wins, the other logs land in `run_dir`, `result.json` still lands in `run_dir`, and the directory is created. Pinned in Task 2, Task 3 and Task 4 (codex).
5. **A CLI that is absent, broken or hangs on `--version`.** The probe must answer `available=False` with a reason, and within its timeout. Pinned in Task 6.

---

## Before you start

Implementation happens on its own branch, cut from `main` once this plan has merged:

```bash
git fetch origin main
git worktree add .claude/worktrees/ha-040-lot1 -b feat/headless-agents-lot1-facade origin/main
cd .claude/worktrees/ha-040-lot1
uv sync --extra dev --python 3.12
.venv/bin/pytest tests/unit/headless_agents tests/unit/agents -q
```

Expected: every test passes before any change. If one fails, stop and report it: a red baseline hides the next regression.

Blast radius, measured on `f115cf6b` and to be re-measured in your worktree before Task 1:

```bash
git grep -n -E 'Provider\(\)\.run|Provider\([^)]*\)\.run' -- src scripts   # expected: no output
git grep -n -E 'RunSpec\(|RunResult\(' -- src scripts                     # expected: only src/brain_v42/agents/providers/*.py
```

The Dream's own providers construct `RunResult` without the new fields, which is allowed because every new field has a default. If GitNexus is fresh on the canonical root, `impact({target: "RunResult", direction: "upstream"})` must show the same; if it is stale, the two `git grep` lines are the measurement: say so in the task report.

---

### Task 1: `RunResult` — `text`, `run_id`, `stderr_log`, `raw_log`, and `to_dict()` (schema 1)

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/result.py`
- Test: `tests/unit/headless_agents/test_result.py` (new)

**Interfaces:**
- Consumes: nothing new.
- Produces: `RESULT_SCHEMA_VERSION: int = 1`; `RunResult` fields `text: str | None = None`, `run_id: str | None = None`, `stderr_log: Path | None = None`, `raw_log: Path | None = None`; `RunResult.to_dict() -> dict[str, object]`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/headless_agents/test_result.py`:

```python
"""``RunResult.to_dict``: the schema-1 contract of ``result.json`` and ``ha run --json``."""

from __future__ import annotations

import json
from pathlib import Path

from headless_agents.result import RESULT_SCHEMA_VERSION, RunResult, TokenUsage

SCHEMA_1_KEYS = [
    "schema",
    "run_id",
    "provider",
    "model",
    "model_reported",
    "exit_code",
    "text",
    "tokens",
    "cost_usd",
    "duration_seconds",
    "tool_call_completed",
    "context",
    "workspace",
    "branch",
    "logs",
]


def _result(**overrides: object) -> RunResult:
    fields: dict[str, object] = {
        "exit_code": 0,
        "provider": "codex",
        "model": "m",
        "report_path": Path("/runs/r1/report.log"),
        "events_log": Path("/runs/r1/events.jsonl"),
        "tokens": None,
        "duration_seconds": 1.5,
        "tool_call_completed": False,
    }
    fields.update(overrides)
    return RunResult(**fields)  # type: ignore[arg-type]


class TestNewFields:
    def test_default_to_none_so_existing_callers_are_untouched(self) -> None:
        result = _result()
        assert result.text is None
        assert result.run_id is None
        assert result.stderr_log is None
        assert result.raw_log is None


class TestToDict:
    def test_schema_1_carries_exactly_the_documented_keys_in_order(self) -> None:
        assert RESULT_SCHEMA_VERSION == 1
        data = _result().to_dict()
        assert list(data) == SCHEMA_1_KEYS
        assert data["schema"] == 1

    def test_is_json_safe_and_round_trips_with_non_ascii_text(self) -> None:
        result = _result(
            text="déjà vu ✓",
            run_id="r1",
            stderr_log=Path("/runs/r1/stderr.log"),
            tokens=TokenUsage(input=10, output=2, cached=4),
            cost_usd=0.01,
            model_reported="m-2026",
        )
        data = result.to_dict()
        assert json.loads(json.dumps(data, ensure_ascii=False)) == data
        assert data["text"] == "déjà vu ✓"

    def test_logs_are_strings_and_absent_logs_are_null(self) -> None:
        data = _result(stderr_log=Path("/runs/r1/stderr.log")).to_dict()
        assert data["logs"] == {
            "report": "/runs/r1/report.log",
            "events": "/runs/r1/events.jsonl",
            "stderr": "/runs/r1/stderr.log",
            "raw": None,
        }

    def test_null_keeps_its_meaning_not_measured_never_zero(self) -> None:
        measured = _result(tokens=TokenUsage(input=10)).to_dict()
        assert measured["tokens"] == {
            "input": 10,
            "output": None,
            "fresh": None,
            "cached": None,
            "thinking": None,
        }
        assert measured["cost_usd"] is None
        assert _result().to_dict()["tokens"] is None

    def test_fields_the_runtime_cannot_fill_yet_are_present_and_null(self) -> None:
        data = _result().to_dict()
        assert data["context"] is None
        assert data["workspace"] is None
        assert data["branch"] is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_result.py -v`
Expected: collection FAILS with `ImportError: cannot import name 'RESULT_SCHEMA_VERSION' from 'headless_agents.result'`.

- [ ] **Step 3: Write the implementation**

Replace the whole of `packages/headless-agents/src/headless_agents/result.py` with:

```python
"""``RunResult`` -- the common output shape across the providers.

``tokens`` is ``None``, not a zeroed :class:`TokenUsage`, when the rail's own
telemetry does not measure a count: absent is not zero, and a consumer that
persists these figures must be able to tell the two apart. A provider that
cannot observe cached-input tokens must report ``cached=None`` there, never
``0``.

``model`` is the label the caller asked for; ``model_reported`` is what the
CLI's envelope says actually answered, when it says anything (only ``claude``
names its model today). The two differ whenever a CLI silently substitutes,
which is exactly the case a consumer wants to display rather than normalise.

``text`` is the run's final answer, read by the provider from the file its
rail writes the answer to, and ``None`` when the run produced none -- a failed
run, or an answer file left empty. It is VERBATIM: an answer that is itself
JSON comes back as the agent wrote it, never re-read as an envelope.

:meth:`RunResult.to_dict` is the serialised form, schema
:data:`RESULT_SCHEMA_VERSION`: the contract of ``result.json`` and of
``ha run --json``. ``null`` in it keeps the meaning "not measured", never zero.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

RESULT_SCHEMA_VERSION = 1


@dataclass(frozen=True, kw_only=True)
class TokenUsage:
    """Token counts a provider measured for one run. ``None`` = not measured."""

    input: int | None = None
    output: int | None = None
    fresh: int | None = None
    cached: int | None = None
    thinking: int | None = None


def _path(value: Path | None) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True, kw_only=True)
class RunResult:
    """The outcome of one :meth:`AgentProvider.run` call."""

    exit_code: int
    provider: str
    model: str
    report_path: Path | None
    events_log: Path | None
    tokens: TokenUsage | None
    duration_seconds: float
    tool_call_completed: bool
    model_reported: str | None = None
    cost_usd: float | None = None
    text: str | None = None
    run_id: str | None = None
    stderr_log: Path | None = None
    raw_log: Path | None = None

    def to_dict(self) -> dict[str, object]:
        """The JSON-safe schema-1 form of this result.

        ``context``, ``workspace`` and ``branch`` belong to schema 1 from the
        start, so a consumer can pin the key set; they stay ``None`` until the
        runtime can fill them (the context bundle, the workspace capability,
        the ``ha run --write`` carrier branch).
        """
        return {
            "schema": RESULT_SCHEMA_VERSION,
            "run_id": self.run_id,
            "provider": self.provider,
            "model": self.model,
            "model_reported": self.model_reported,
            "exit_code": self.exit_code,
            "text": self.text,
            "tokens": None if self.tokens is None else asdict(self.tokens),
            "cost_usd": self.cost_usd,
            "duration_seconds": self.duration_seconds,
            "tool_call_completed": self.tool_call_completed,
            "context": None,
            "workspace": None,
            "branch": None,
            "logs": {
                "report": _path(self.report_path),
                "events": _path(self.events_log),
                "stderr": _path(self.stderr_log),
                "raw": _path(self.raw_log),
            },
        }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/headless_agents -v`
Expected: PASS, the new file included; nothing else in the package changed behaviour.

- [ ] **Step 5: Gates and commit**

```bash
.venv/bin/ruff check --fix packages/headless-agents tests/unit/headless_agents && .venv/bin/ruff format packages/headless-agents tests/unit/headless_agents
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy src/ packages/headless-agents/src/
git add packages/headless-agents/src/headless_agents/result.py tests/unit/headless_agents/test_result.py
git commit -m "feat(headless-agents): RunResult.text and a schema-1 to_dict"
```

---

### Task 2: `RunSpec.run_dir` — fixed log names, explicit paths win

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/spec.py`
- Test: `tests/unit/headless_agents/test_spec.py` (new)

**Interfaces:**
- Consumes: nothing new.
- Produces: `RUN_DIR_LOG_NAMES: Final[Mapping[str, str]]`; `RunSpec.run_dir: Path | None = None`; `RunSpec.with_run_dir_defaults() -> RunSpec`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/headless_agents/test_spec.py`:

```python
"""``RunSpec.run_dir``: one directory per run, fixed log names, explicit paths win."""

from __future__ import annotations

from pathlib import Path

from headless_agents.spec import RUN_DIR_LOG_NAMES, RunSpec


class TestWithRunDirDefaults:
    def test_without_run_dir_the_spec_is_returned_unchanged(self, tmp_path: Path) -> None:
        spec = RunSpec(prompt="P", report_log=tmp_path / "r.log")
        assert spec.with_run_dir_defaults() is spec

    def test_every_unset_log_takes_its_fixed_name_inside_run_dir(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "runs" / "r1"
        spec = RunSpec(prompt="P", run_dir=run_dir).with_run_dir_defaults()
        assert spec.report_log == run_dir / "report.log"
        assert spec.events_log == run_dir / "events.jsonl"
        assert spec.stderr_log == run_dir / "stderr.log"
        assert spec.raw_log == run_dir / "raw.log"

    def test_an_explicit_log_path_wins_over_run_dir(self, tmp_path: Path) -> None:
        explicit = tmp_path / "elsewhere" / "final.txt"
        spec = RunSpec(
            prompt="P", run_dir=tmp_path / "r1", report_log=explicit
        ).with_run_dir_defaults()
        assert spec.report_log == explicit
        assert spec.events_log == tmp_path / "r1" / "events.jsonl"

    def test_the_original_spec_is_left_as_it_was(self, tmp_path: Path) -> None:
        spec = RunSpec(prompt="P", run_dir=tmp_path / "r1")
        spec.with_run_dir_defaults()
        assert spec.report_log is None

    def test_the_fixed_names_are_the_documented_ones(self) -> None:
        assert dict(RUN_DIR_LOG_NAMES) == {
            "report_log": "report.log",
            "events_log": "events.jsonl",
            "stderr_log": "stderr.log",
            "raw_log": "raw.log",
        }
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_spec.py -v`
Expected: collection FAILS with `ImportError: cannot import name 'RUN_DIR_LOG_NAMES' from 'headless_agents.spec'`.

- [ ] **Step 3: Write the implementation**

In `packages/headless-agents/src/headless_agents/spec.py`:

a) Append this paragraph to the module docstring, before its closing `"""`:

```text
``run_dir`` names one directory per run: every log the caller leaves unset
takes its fixed name inside it (:data:`RUN_DIR_LOG_NAMES`), its name is the
run's id, and the provider writes ``result.json`` there (see
:mod:`headless_agents.run_record`). Explicit log paths still win, so a caller
that names its logs runs exactly as before.
```

b) Replace the import block

```python
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .profile import CapabilityProfile
```

with

```python
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

from .profile import CapabilityProfile

#: The name each log takes inside ``RunSpec.run_dir`` when the caller names
#: none. A reader of a run directory relies on them: they are part of the
#: contract, like the schema of ``result.json``.
RUN_DIR_LOG_NAMES: Final[Mapping[str, str]] = {
    "report_log": "report.log",
    "events_log": "events.jsonl",
    "stderr_log": "stderr.log",
    "raw_log": "raw.log",
}
```

c) In `class RunSpec`, replace

```python
    raw_log: Path | None = None
    workspace: Path | None = None
```

with

```python
    raw_log: Path | None = None
    run_dir: Path | None = None
    workspace: Path | None = None
```

d) Add this method to `RunSpec`, after `effective_timeout_seconds`:

```python
    def with_run_dir_defaults(self) -> RunSpec:
        """This spec with every UNSET log path moved into ``run_dir``.

        Explicit paths win: a caller that names a log keeps it. Without a
        ``run_dir`` the spec itself is returned, unchanged.
        """
        if self.run_dir is None:
            return self
        defaults = {
            field_name: self.run_dir / file_name
            for field_name, file_name in RUN_DIR_LOG_NAMES.items()
            if getattr(self, field_name) is None
        }
        return replace(self, **defaults) if defaults else self
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/headless_agents -v`
Expected: PASS.

- [ ] **Step 5: Gates and commit**

```bash
.venv/bin/ruff check --fix packages/headless-agents tests/unit/headless_agents && .venv/bin/ruff format packages/headless-agents tests/unit/headless_agents
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy src/ packages/headless-agents/src/
git add packages/headless-agents/src/headless_agents/spec.py tests/unit/headless_agents/test_spec.py
git commit -m "feat(headless-agents): RunSpec.run_dir gives a run one directory"
```

---

### Task 3: `run_record` — read the answer, write `result.json`

**Files:**
- Create: `packages/headless-agents/src/headless_agents/run_record.py`
- Test: `tests/unit/headless_agents/test_run_record.py` (new)

**Interfaces:**
- Consumes: `RunResult.to_dict()` (Task 1); `RunSpec.run_dir` (Task 2).
- Produces: `RESULT_FILE_NAME = "result.json"`; `answer_text(path: Path | None, *, exit_code: int, offset: int = 0) -> str | None`; `run_id_of(spec: RunSpec) -> str | None`; `record(spec: RunSpec, result: RunResult) -> RunResult`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/headless_agents/test_run_record.py`:

```python
"""What every rail does after its process exits: read the answer, record the run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from headless_agents.result import RunResult
from headless_agents.run_record import answer_text, record, run_id_of
from headless_agents.spec import RunSpec


def _result(**overrides: object) -> RunResult:
    fields: dict[str, object] = {
        "exit_code": 0,
        "provider": "codex",
        "model": "m",
        "report_path": None,
        "events_log": None,
        "tokens": None,
        "duration_seconds": 0.5,
        "tool_call_completed": False,
    }
    fields.update(overrides)
    return RunResult(**fields)  # type: ignore[arg-type]


class TestAnswerText:
    def test_an_answer_that_is_json_comes_back_verbatim(self, tmp_path: Path) -> None:
        path = tmp_path / "report.log"
        path.write_text('{"result": "pass", "score": 3}\n', encoding="utf-8")
        assert answer_text(path, exit_code=0) == '{"result": "pass", "score": 3}\n'

    def test_a_failed_run_has_no_answer_even_with_a_report(self, tmp_path: Path) -> None:
        path = tmp_path / "report.log"
        path.write_text("half an answer", encoding="utf-8")
        assert answer_text(path, exit_code=1) is None
        assert answer_text(path, exit_code=3) is None
        assert answer_text(path, exit_code=124) is None

    def test_an_absent_or_blank_file_is_no_answer(self, tmp_path: Path) -> None:
        blank = tmp_path / "blank.log"
        blank.write_text(" \n\t\n", encoding="utf-8")
        assert answer_text(None, exit_code=0) is None
        assert answer_text(tmp_path / "absent.log", exit_code=0) is None
        assert answer_text(blank, exit_code=0) is None

    def test_offset_skips_what_a_previous_run_left_in_a_reused_log(self, tmp_path: Path) -> None:
        path = tmp_path / "raw.log"
        path.write_text("PREVIOUS RUN\n", encoding="utf-8")
        offset = path.stat().st_size
        with path.open("a", encoding="utf-8") as stream:
            stream.write("THIS RUN\n")
        assert answer_text(path, exit_code=0, offset=offset) == "THIS RUN\n"

    def test_undecodable_bytes_are_replaced_never_raised(self, tmp_path: Path) -> None:
        path = tmp_path / "report.log"
        path.write_bytes(b"ok \xff\n")
        assert answer_text(path, exit_code=0) == "ok �\n"


class TestRunIdOf:
    def test_is_the_name_of_the_run_directory(self, tmp_path: Path) -> None:
        assert run_id_of(RunSpec(prompt="P", run_dir=tmp_path / "20260923-a1")) == "20260923-a1"

    def test_is_none_without_a_run_directory(self) -> None:
        assert run_id_of(RunSpec(prompt="P")) is None


class TestRecord:
    def test_without_run_dir_nothing_is_written(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = _result()
        assert record(RunSpec(prompt="P"), result) is result
        assert list(tmp_path.iterdir()) == []

    def test_writes_the_schema_1_dict_into_a_run_dir_it_creates(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "runs" / "r1"
        result = _result(text="déjà ✓", run_id="r1")
        assert record(RunSpec(prompt="P", run_dir=run_dir), result) is result
        written = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        assert written == result.to_dict()
        assert sorted(path.name for path in run_dir.iterdir()) == ["result.json"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_run_record.py -v`
Expected: collection FAILS with `ModuleNotFoundError: No module named 'headless_agents.run_record'`.

- [ ] **Step 3: Write the implementation**

Create `packages/headless-agents/src/headless_agents/run_record.py`:

```python
"""What every rail does once its process has exited: read the answer, record the run.

Shared by the four CLI rails so that ``RunResult.text`` means the same thing
on each of them, and so that ``result.json`` is written in one place.
"""

from __future__ import annotations

import json
from pathlib import Path

from .result import RunResult
from .spec import RunSpec

RESULT_FILE_NAME = "result.json"


def answer_text(path: Path | None, *, exit_code: int, offset: int = 0) -> str | None:
    """The run's final answer, VERBATIM, or ``None`` when it produced none.

    ``None`` on a failed run even when the file holds something: a partial or
    stale report is not an answer. ``offset`` skips what the file held before
    this run started -- the claude rail APPENDS to its log, so a reused path
    would otherwise hand back an earlier run's answer. Undecodable bytes are
    replaced, never raised: reading the answer must not fail the run.
    """
    if exit_code != 0 or path is None or not path.is_file():
        return None
    with path.open("rb") as stream:
        stream.seek(offset)
        content = stream.read().decode("utf-8", errors="replace")
    return content if content.strip() else None


def run_id_of(spec: RunSpec) -> str | None:
    """A run is named by its directory: ``run_dir.name``, or ``None`` without one."""
    return spec.run_dir.name if spec.run_dir is not None else None


def record(spec: RunSpec, result: RunResult) -> RunResult:
    """Write ``result.json`` into ``spec.run_dir`` when there is one; return ``result``.

    Written under a temporary name, then renamed: a reader listing run
    directories never parses half a file.
    """
    if spec.run_dir is None:
        return result
    spec.run_dir.mkdir(parents=True, exist_ok=True)
    target = spec.run_dir / RESULT_FILE_NAME
    partial = spec.run_dir / f".{RESULT_FILE_NAME}.partial"
    partial.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    partial.replace(target)
    return result
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/headless_agents -v`
Expected: PASS, `test_package_boundary.py` included (the new module imports nothing outside the package and the standard library).

- [ ] **Step 5: Gates and commit**

```bash
.venv/bin/ruff check --fix packages/headless-agents tests/unit/headless_agents && .venv/bin/ruff format packages/headless-agents tests/unit/headless_agents
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy src/ packages/headless-agents/src/
git add packages/headless-agents/src/headless_agents/run_record.py tests/unit/headless_agents/test_run_record.py
git commit -m "feat(headless-agents): read a run's answer and record it as result.json"
```

---

### Task 4: The report-file rails (codex, agy, opencode) fill `text` and honour `run_dir`

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/providers/codex.py` (`CodexProvider.run`)
- Modify: `packages/headless-agents/src/headless_agents/providers/agy.py` (`AgyProvider.run`)
- Modify: `packages/headless-agents/src/headless_agents/providers/opencode.py` (`OpenCodeProvider.run`)
- Test: `tests/unit/headless_agents/test_provider_codex.py`, `test_provider_agy.py`, `test_provider_opencode.py` (new tests appended to each rail's provider class)

**Interfaces:**
- Consumes: `RunSpec.with_run_dir_defaults()` (Task 2); `answer_text`, `run_id_of`, `record` (Task 3).
- Produces: on these three rails, `RunResult.text` is the report file's content on exit 0 (else `None`), `RunResult.run_id` and `RunResult.stderr_log` are set, and a `run_dir` receives `result.json`.

- [ ] **Step 1: Write the failing tests**

Append to `class TestCodexProvider` in `tests/unit/headless_agents/test_provider_codex.py` (the module already imports `json`, `Path`, `pytest`, `codex` and `RunSpec`):

```python
    def test_run_reads_its_report_as_text_and_records_the_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def fake_run_codex(**kwargs: object) -> int:
            report = kwargs["report_log"]
            assert isinstance(report, Path)
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("the answer\n", encoding="utf-8")
            return 0

        monkeypatch.setattr(codex, "run_codex", fake_run_codex)
        run_dir = tmp_path / "runs" / "r1"
        result = codex.CodexProvider().run(RunSpec(prompt="P", model="m", run_dir=run_dir))
        assert result.text == "the answer\n"
        assert result.run_id == "r1"
        assert result.report_path == run_dir / "report.log"
        assert result.events_log == run_dir / "events.jsonl"
        assert result.stderr_log == run_dir / "stderr.log"
        written = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        assert written == result.to_dict()

    def test_an_explicit_log_path_wins_and_result_json_still_lands_in_run_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def fake_run_codex(**kwargs: object) -> int:
            report = kwargs["report_log"]
            assert isinstance(report, Path)
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("answer", encoding="utf-8")
            return 0

        monkeypatch.setattr(codex, "run_codex", fake_run_codex)
        explicit = tmp_path / "elsewhere" / "final.txt"
        run_dir = tmp_path / "runs" / "r2"
        result = codex.CodexProvider().run(
            RunSpec(prompt="P", model="m", run_dir=run_dir, report_log=explicit)
        )
        assert result.report_path == explicit
        assert result.text == "answer"
        assert result.events_log == run_dir / "events.jsonl"
        assert (run_dir / "result.json").is_file()
```

Append to `class TestAgyProvider` in `tests/unit/headless_agents/test_provider_agy.py` (the module already imports `json`, `Path`, `pytest`, `agy`, `RunSpec`, and defines `_profile(tmp_path)` and `_logs(tmp_path)`):

```python
    def test_run_reads_its_report_as_text_and_records_the_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def fake_run_agy(**kwargs: object) -> int:
            report = kwargs["report_log"]
            assert isinstance(report, Path)
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("agy answer", encoding="utf-8")
            return 0

        monkeypatch.setattr(agy, "run_agy", fake_run_agy)
        run_dir = tmp_path / "runs" / "r1"
        result = agy.AgyProvider().run(
            RunSpec(prompt="P", model="m", profile=_profile(tmp_path), run_dir=run_dir)
        )
        assert result.text == "agy answer"
        assert result.run_id == "r1"
        assert result.stderr_log == run_dir / "stderr.log"
        written = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        assert written == result.to_dict()

    def test_a_failed_run_has_no_text_even_when_the_stream_left_a_report(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """extract_report runs before the exit code is judged, so a failed run can
        leave a report behind; that is not an answer."""

        def fake_run_agy(**kwargs: object) -> int:
            report = kwargs["report_log"]
            assert isinstance(report, Path)
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("half an answer", encoding="utf-8")
            return 1

        monkeypatch.setattr(agy, "run_agy", fake_run_agy)
        result = agy.AgyProvider().run(
            RunSpec(prompt="P", model="m", profile=_profile(tmp_path), **_logs(tmp_path))
        )
        assert result.exit_code == 1
        assert result.text is None
```

Append to `class TestOpenCodeProvider` in `tests/unit/headless_agents/test_provider_opencode.py` (the module already imports `json`, `Path`, `pytest`, `opencode`, `RunSpec`, and defines `_profile()`):

```python
    def test_run_reads_its_report_as_text_and_records_the_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def fake_run(**kwargs: object) -> int:
            report = kwargs["report_log"]
            assert isinstance(report, Path)
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("opencode answer", encoding="utf-8")
            return 0

        monkeypatch.setattr(opencode, "run_opencode", fake_run)
        run_dir = tmp_path / "runs" / "r1"
        result = opencode.OpenCodeProvider().run(
            RunSpec(prompt="P", model="opencode-go/m", profile=_profile(), run_dir=run_dir)
        )
        assert result.text == "opencode answer"
        assert result.run_id == "r1"
        assert result.tokens is None  # no event was written: nothing was measured
        written = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        assert written == result.to_dict()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_provider_codex.py tests/unit/headless_agents/test_provider_agy.py tests/unit/headless_agents/test_provider_opencode.py -v`
Expected: the four tests that pass a `run_dir` FAIL with `AssertionError` raised by `assert spec.report_log is not None` (or `spec.events_log`) inside `run()`, since the defaults are not applied yet. `test_a_failed_run_has_no_text_even_when_the_stream_left_a_report` PASSES already, because `text` defaults to `None`: it is the guard that keeps the exit-code rule once `text` is read. Every other test in the three files passes.

- [ ] **Step 3: Write the implementation**

Add this import to each of the three provider modules:

```python
from ..run_record import answer_text, record, run_id_of
```

It goes directly after `from ..result import RunResult` in `providers/codex.py` and `providers/agy.py`, and directly after `from ..result import RunResult, TokenUsage` in `providers/opencode.py`.

Replace `CodexProvider.run` in `providers/codex.py` with:

```python
    def run(self, spec: RunSpec) -> RunResult:
        spec = spec.with_run_dir_defaults()
        assert spec.report_log is not None
        assert spec.events_log is not None
        assert spec.stderr_log is not None
        start = time.monotonic()
        exit_code = run_codex(
            prompt=spec.prompt,
            model=spec.model,
            reasoning_effort=spec.reasoning_effort,
            timeout_seconds=spec.timeout_seconds,
            report_log=spec.report_log,
            events_log=spec.events_log,
            stderr_log=spec.stderr_log,
            mcp=spec.profile.mcp,
            environment=spec.environment,
            executable=spec.executable or "codex",
            workspace=spec.workspace,
            deadline=spec.deadline,
        )
        duration = time.monotonic() - start
        server = spec.profile.mcp.name if spec.profile.mcp is not None else None
        return record(
            spec,
            RunResult(
                exit_code=exit_code,
                provider=self.name,
                model=spec.model,
                report_path=spec.report_log,
                events_log=spec.events_log,
                # Not measured here: the envelope module reads turn.completed usage
                # for a caller that wants it. ``None`` means "not measured", never
                # a fabricated zero -- see ``result``'s docstring.
                tokens=None,
                duration_seconds=duration,
                tool_call_completed=(
                    server is not None and tool_call_completed(spec.events_log, server=server)
                ),
                # --output-last-message: the report holds the final agent message.
                text=answer_text(spec.report_log, exit_code=exit_code),
                run_id=run_id_of(spec),
                stderr_log=spec.stderr_log,
            ),
        )
```

Replace `AgyProvider.run` in `providers/agy.py` with:

```python
    def run(self, spec: RunSpec) -> RunResult:
        spec = spec.with_run_dir_defaults()
        assert spec.events_log is not None
        assert spec.report_log is not None
        assert spec.stderr_log is not None
        start = time.monotonic()
        exit_code = run_agy(
            prompt=spec.prompt,
            name=spec.name,
            model=spec.model,
            timeout_seconds=spec.timeout_seconds,
            events_log=spec.events_log,
            report_log=spec.report_log,
            stderr_log=spec.stderr_log,
            profile=spec.profile,
            real_home=self._real_home,
            environment=spec.environment,
            executable=spec.executable or "agy",
            ephemeral_root=self._ephemeral_root,
            deadline=spec.deadline,
        )
        duration = time.monotonic() - start
        return record(
            spec,
            RunResult(
                exit_code=exit_code,
                provider=self.name,
                model=spec.model,
                report_path=spec.report_log,
                events_log=spec.events_log,
                tokens=None,
                duration_seconds=duration,
                tool_call_completed=(
                    spec.profile.mcp is not None and tool_call_completed(spec.events_log)
                ),
                # extract_report wrote the stream's final ``response`` there.
                text=answer_text(spec.report_log, exit_code=exit_code),
                run_id=run_id_of(spec),
                stderr_log=spec.stderr_log,
            ),
        )
```

Replace `OpenCodeProvider.run` in `providers/opencode.py` with:

```python
    def run(self, spec: RunSpec) -> RunResult:
        spec = spec.with_run_dir_defaults()
        assert spec.events_log is not None
        assert spec.report_log is not None
        assert spec.stderr_log is not None
        start = time.monotonic()
        exit_code = run_opencode(
            prompt=spec.prompt,
            name=spec.name,
            model=spec.model,
            timeout_seconds=spec.timeout_seconds,
            events_log=spec.events_log,
            report_log=spec.report_log,
            stderr_log=spec.stderr_log,
            profile=spec.profile,
            real_home=self._real_home,
            environment=spec.environment,
            executable=spec.executable or "opencode",
            variant=spec.reasoning_effort,
            title=spec.name,
            ephemeral_root=self._ephemeral_root,
            deadline=spec.deadline,
        )
        duration = time.monotonic() - start
        tokens, cost = telemetry(spec.events_log)
        return record(
            spec,
            RunResult(
                exit_code=exit_code,
                provider=self.name,
                model=spec.model,
                report_path=spec.report_log,
                events_log=spec.events_log,
                tokens=tokens,
                duration_seconds=duration,
                tool_call_completed=self.tool_call_completed(spec),
                cost_usd=cost,
                # extract_report joined the stream's text parts there.
                text=answer_text(spec.report_log, exit_code=exit_code),
                run_id=run_id_of(spec),
                stderr_log=spec.stderr_log,
            ),
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/headless_agents tests/unit/agents -v`
Expected: PASS, the Dream golden fixtures in `tests/unit/agents` included and unchanged.

- [ ] **Step 5: Gates and commit**

```bash
.venv/bin/ruff check --fix packages/headless-agents tests/unit/headless_agents && .venv/bin/ruff format packages/headless-agents tests/unit/headless_agents
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy src/ packages/headless-agents/src/
git add packages/headless-agents/src/headless_agents/providers/codex.py \
  packages/headless-agents/src/headless_agents/providers/agy.py \
  packages/headless-agents/src/headless_agents/providers/opencode.py \
  tests/unit/headless_agents/test_provider_codex.py \
  tests/unit/headless_agents/test_provider_agy.py \
  tests/unit/headless_agents/test_provider_opencode.py
git commit -m "feat(headless-agents): codex, agy and opencode return their answer as text"
```

---

### Task 5: The claude rail fills `text` from this run's bytes and honours `run_dir`

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/providers/claude.py` (`ClaudeProvider.run`)
- Test: `tests/unit/headless_agents/test_provider_claude.py` (new tests appended to `TestClaudeProvider`)

**Interfaces:**
- Consumes: `RunSpec.with_run_dir_defaults()` (Task 2); `answer_text`, `run_id_of`, `record` (Task 3).
- Produces: on the claude rail, `RunResult.text` is what this run appended to `raw_log` on exit 0 (else `None`), `RunResult.raw_log` and `RunResult.run_id` are set, `RunResult.stderr_log` stays `None` (claude merges stderr into `raw_log`), and a `run_dir` receives `result.json`.

- [ ] **Step 1: Write the failing tests**

Append to `class TestClaudeProvider` in `tests/unit/headless_agents/test_provider_claude.py` (the module already imports `json`, `Path`, `pytest`, `claude` and `RunSpec`):

```python
    def test_text_is_only_what_this_run_appended_to_a_reused_log(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        raw_log = tmp_path / "raw.log"
        raw_log.write_text("PREVIOUS RUN\n", encoding="utf-8")

        def fake_run_claude(**kwargs: object) -> int:
            raw = kwargs["raw_log"]
            assert isinstance(raw, Path)
            with raw.open("a", encoding="utf-8") as stream:
                stream.write("THIS RUN\n")
            return 0

        monkeypatch.setattr(claude, "run_claude", fake_run_claude)
        result = claude.ClaudeProvider().run(RunSpec(prompt="P", model="m", raw_log=raw_log))
        assert result.text == "THIS RUN\n"

    def test_an_answer_that_is_json_comes_back_verbatim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A judge's verdict is JSON. A ``result`` key in it must not be read as
        claude's --output-format envelope, which this rail never requests."""
        verdict = '{"result": "pass", "score": 3}\n'

        def fake_run_claude(**kwargs: object) -> int:
            raw = kwargs["raw_log"]
            assert isinstance(raw, Path)
            raw.parent.mkdir(parents=True, exist_ok=True)
            with raw.open("a", encoding="utf-8") as stream:
                stream.write(verdict)
            return 0

        monkeypatch.setattr(claude, "run_claude", fake_run_claude)
        result = claude.ClaudeProvider().run(
            RunSpec(prompt="P", model="m", raw_log=tmp_path / "raw.log")
        )
        assert result.text == verdict

    def test_run_dir_holds_the_raw_log_and_result_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def fake_run_claude(**kwargs: object) -> int:
            raw = kwargs["raw_log"]
            assert isinstance(raw, Path)
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_text("ok\n", encoding="utf-8")
            return 0

        monkeypatch.setattr(claude, "run_claude", fake_run_claude)
        run_dir = tmp_path / "runs" / "r1"
        result = claude.ClaudeProvider().run(RunSpec(prompt="P", model="m", run_dir=run_dir))
        assert result.raw_log == run_dir / "raw.log"
        assert result.stderr_log is None
        assert result.text == "ok\n"
        assert result.run_id == "r1"
        written = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        assert written == result.to_dict()
        assert written["logs"]["stderr"] is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_provider_claude.py -v`
Expected: `test_text_is_only_what_this_run_appended_to_a_reused_log` and `test_an_answer_that_is_json_comes_back_verbatim` FAIL on `assert None == '…'`; `test_run_dir_holds_the_raw_log_and_result_json` FAILS with `AssertionError: RunSpec.raw_log is required for Claude`. The existing tests pass.

- [ ] **Step 3: Write the implementation**

In `providers/claude.py`, add after `from ..result import RunResult`:

```python
from ..run_record import answer_text, record, run_id_of
```

Replace `ClaudeProvider.run` with:

```python
    def run(self, spec: RunSpec) -> RunResult:
        spec = spec.with_run_dir_defaults()
        assert spec.raw_log is not None, "RunSpec.raw_log (or run_dir) is required for Claude"
        raw_log = spec.raw_log
        # run_claude APPENDS to raw_log: remember where this run starts, so the
        # answer never includes what an earlier run left in a reused log.
        offset = raw_log.stat().st_size if raw_log.is_file() else 0
        start = time.monotonic()
        exit_code = run_claude(
            prompt=spec.prompt,
            model=spec.model,
            max_turns=spec.max_turns,
            timeout_seconds=spec.timeout_seconds,
            raw_log=raw_log,
            mcp=spec.profile.mcp,
            environment=spec.environment,
            executable=spec.executable or "claude",
            deadline=spec.deadline,
        )
        duration = time.monotonic() - start
        return record(
            spec,
            RunResult(
                exit_code=exit_code,
                provider=self.name,
                model=spec.model,
                report_path=raw_log,
                events_log=raw_log,
                tokens=None,
                duration_seconds=duration,
                tool_call_completed=spec.profile.mcp is not None and tool_call_completed(raw_log),
                # stdout and stderr share raw_log by design (see run_claude), and
                # this rail requests no JSON envelope: the text is read as written.
                text=answer_text(raw_log, exit_code=exit_code, offset=offset),
                run_id=run_id_of(spec),
                raw_log=raw_log,
            ),
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/headless_agents tests/unit/agents -v`
Expected: PASS.

- [ ] **Step 5: Gates and commit**

```bash
.venv/bin/ruff check --fix packages/headless-agents tests/unit/headless_agents && .venv/bin/ruff format packages/headless-agents tests/unit/headless_agents
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy src/ packages/headless-agents/src/
git add packages/headless-agents/src/headless_agents/providers/claude.py tests/unit/headless_agents/test_provider_claude.py
git commit -m "feat(headless-agents): claude returns only this run's answer as text"
```

---

### Task 6: `registry` — providers by name, a zero-quota probe, prompt limits

**Files:**
- Create: `packages/headless-agents/src/headless_agents/registry.py`
- Test: `tests/unit/headless_agents/test_registry.py` (new)

**Interfaces:**
- Consumes: the four provider classes and `agy.MAX_PROMPT_BYTES`, `opencode.MAX_PROMPT_BYTES` (existing).
- Produces: `PROVIDER_NAMES: tuple[str, ...] = ("claude", "codex", "agy", "opencode")`; `class UnknownProvider(ValueError)` with attribute `name: str`; `get_provider(name: str) -> AgentProvider`; `@dataclass(frozen=True, kw_only=True) class Probe(available: bool, detail: str, version: str | None = None)`; `PROBE_TIMEOUT_SECONDS = 10.0`; `probe(name: str, *, executable: str | None = None, timeout_seconds: float = PROBE_TIMEOUT_SECONDS) -> Probe`; `max_prompt_bytes(name: str) -> int | None`. Lot 3 extends `PROVIDER_NAMES` and the three lookups with the HTTP providers.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/headless_agents/test_registry.py`:

```python
"""The facade: providers by name, a zero-quota probe, per-provider prompt limits."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from headless_agents import registry
from headless_agents.protocol import AgentProvider
from headless_agents.providers import agy, opencode


def _script(directory: Path, body: str, name: str = "fake-cli") -> Path:
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class TestGetProvider:
    @pytest.mark.parametrize("name", ["claude", "codex", "agy", "opencode"])
    def test_every_cli_rail_is_reached_by_its_name(self, name: str) -> None:
        provider = registry.get_provider(name)
        assert isinstance(provider, AgentProvider)
        assert provider.name == name

    def test_the_names_are_the_four_cli_rails(self) -> None:
        assert registry.PROVIDER_NAMES == ("claude", "codex", "agy", "opencode")

    def test_each_call_returns_a_fresh_instance(self) -> None:
        assert registry.get_provider("codex") is not registry.get_provider("codex")

    def test_an_unknown_name_raises_a_message_listing_the_valid_ones(self) -> None:
        with pytest.raises(registry.UnknownProvider) as caught:
            registry.get_provider("gpt")
        message = str(caught.value)
        assert "'gpt'" in message
        for name in registry.PROVIDER_NAMES:
            assert name in message
        assert isinstance(caught.value, ValueError)
        assert caught.value.name == "gpt"


class TestMaxPromptBytes:
    def test_the_stdin_rails_have_no_limit(self) -> None:
        assert registry.max_prompt_bytes("claude") is None
        assert registry.max_prompt_bytes("codex") is None

    def test_the_argv_rails_expose_their_own_limit(self) -> None:
        assert registry.max_prompt_bytes("agy") == agy.MAX_PROMPT_BYTES
        assert registry.max_prompt_bytes("opencode") == opencode.MAX_PROMPT_BYTES

    def test_an_unknown_name_is_refused(self) -> None:
        with pytest.raises(registry.UnknownProvider):
            registry.max_prompt_bytes("gpt")


class TestProbe:
    def test_an_answering_executable_is_available_with_its_version(self, tmp_path: Path) -> None:
        cli = _script(tmp_path, 'echo "2.1.280 (Claude Code)"')
        found = registry.probe("claude", executable=str(cli))
        assert found.available is True
        assert found.version == "2.1.280 (Claude Code)"
        assert found.detail == str(cli)

    def test_an_absent_executable_is_unavailable(self, tmp_path: Path) -> None:
        found = registry.probe("codex", executable=str(tmp_path / "missing"))
        assert found.available is False
        assert found.version is None
        assert "not found" in found.detail

    def test_a_failing_version_is_unavailable_with_its_exit_code(self, tmp_path: Path) -> None:
        cli = _script(tmp_path, "echo broken >&2; exit 3")
        found = registry.probe("agy", executable=str(cli))
        assert found.available is False
        assert "exited 3" in found.detail

    def test_a_hanging_version_is_bounded_by_the_timeout(self, tmp_path: Path) -> None:
        cli = _script(tmp_path, "sleep 5")
        found = registry.probe("opencode", executable=str(cli), timeout_seconds=0.2)
        assert found.available is False
        assert "no answer" in found.detail

    def test_the_default_executable_is_found_on_path_by_the_rails_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _script(tmp_path, 'echo "codex-cli 0.156.0"', name="codex")
        monkeypatch.setenv("PATH", str(tmp_path))
        found = registry.probe("codex")
        assert found.available is True
        assert found.version == "codex-cli 0.156.0"

    def test_an_unknown_name_is_refused_even_with_an_executable(self) -> None:
        with pytest.raises(registry.UnknownProvider):
            registry.probe("gpt")
        with pytest.raises(registry.UnknownProvider):
            registry.probe("gpt", executable="/bin/true")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_registry.py -v`
Expected: collection FAILS with `ImportError: cannot import name 'registry' from 'headless_agents'`.

- [ ] **Step 3: Write the implementation**

Create `packages/headless-agents/src/headless_agents/registry.py`:

```python
"""The facade: providers by name, a zero-quota probe, and per-provider prompt limits.

A consumer that dispatches on a provider name through its own ``if/elif``
rebuilds this module, and drifts from it the day a rail is added. It asks
here instead: :func:`get_provider` for the rail, :func:`probe` to know whether
the rail can run on this machine at all, :func:`max_prompt_bytes` for the
argv rails' limit -- never a hard-coded copy of it.

:func:`probe` costs no quota: it looks for the executable and asks for its
``--version``, never for a model call.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from .protocol import AgentProvider
from .providers.agy import MAX_PROMPT_BYTES as AGY_MAX_PROMPT_BYTES
from .providers.agy import AgyProvider
from .providers.claude import ClaudeProvider
from .providers.codex import CodexProvider
from .providers.opencode import MAX_PROMPT_BYTES as OPENCODE_MAX_PROMPT_BYTES
from .providers.opencode import OpenCodeProvider

_FACTORIES: Final[Mapping[str, Callable[[], AgentProvider]]] = {
    "claude": ClaudeProvider,
    "codex": CodexProvider,
    "agy": AgyProvider,
    "opencode": OpenCodeProvider,
}

PROVIDER_NAMES: Final[tuple[str, ...]] = tuple(_FACTORIES)

# ``None`` for the rails that read the prompt on stdin. agy and opencode MUST
# take it in argv (agy ignores stdin; ``opencode run`` blocks on a piped one),
# where a single argument is bounded by the kernel.
_MAX_PROMPT_BYTES: Final[Mapping[str, int | None]] = {
    "claude": None,
    "codex": None,
    "agy": AGY_MAX_PROMPT_BYTES,
    "opencode": OPENCODE_MAX_PROMPT_BYTES,
}

PROBE_TIMEOUT_SECONDS = 10.0


class UnknownProvider(ValueError):
    """A provider name the registry does not know; the message lists the valid ones."""

    def __init__(self, name: str) -> None:
        super().__init__(f"unknown provider {name!r}; valid names: {', '.join(PROVIDER_NAMES)}")
        self.name = name


@dataclass(frozen=True, kw_only=True)
class Probe:
    """Whether a rail can run here. ``detail`` says where it was found, or why not."""

    available: bool
    detail: str
    version: str | None = None


def _known(name: str) -> str:
    if name not in _FACTORIES:
        raise UnknownProvider(name)
    return name


def get_provider(name: str) -> AgentProvider:
    """A new instance of the rail called ``name``."""
    return _FACTORIES[_known(name)]()


def max_prompt_bytes(name: str) -> int | None:
    """The largest prompt, in UTF-8 bytes, the rail accepts; ``None`` when unbounded."""
    return _MAX_PROMPT_BYTES[_known(name)]


def probe(
    name: str,
    *,
    executable: str | None = None,
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
) -> Probe:
    """Is the rail's executable on ``PATH``, and does it answer ``--version``?

    ``executable`` overrides the rail's default command, as ``RunSpec.executable``
    does for a run. A ``--version`` that does not answer within
    ``timeout_seconds`` makes the rail unavailable: a probe never hangs.
    """
    _known(name)
    # The four CLI rails are invoked by their own name, as the providers do.
    command = executable or name
    path = shutil.which(command)
    if path is None:
        return Probe(available=False, detail=f"{command}: not found or not executable")
    try:
        completed = subprocess.run(
            [path, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Probe(
            available=False, detail=f"{path} --version: no answer within {timeout_seconds:g} s"
        )
    except OSError as exc:
        return Probe(available=False, detail=f"{path} --version: {exc}")
    if completed.returncode != 0:
        return Probe(available=False, detail=f"{path} --version exited {completed.returncode}")
    version = next((line.strip() for line in completed.stdout.splitlines() if line.strip()), None)
    return Probe(available=True, detail=path, version=version)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/headless_agents -v`
Expected: PASS, `test_package_boundary.py` included: the dry import of every module, the new one among them, still loads no heavy dependency and stays under its time bound.

- [ ] **Step 5: Gates and commit**

```bash
.venv/bin/ruff check --fix packages/headless-agents tests/unit/headless_agents && .venv/bin/ruff format packages/headless-agents tests/unit/headless_agents
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy src/ packages/headless-agents/src/
git add packages/headless-agents/src/headless_agents/registry.py tests/unit/headless_agents/test_registry.py
git commit -m "feat(headless-agents): a registry to reach, probe and bound providers by name"
```

---

### Task 7: Document the facade and verify the whole lot

**Files:**
- Modify: `packages/headless-agents/CHANGELOG.md`
- Modify: `packages/headless-agents/README.md`

**Interfaces:**
- Consumes: everything above.
- Produces: the documented contract of lot 1.

- [ ] **Step 1: Extend the contract paragraph of the CHANGELOG**

In `packages/headless-agents/CHANGELOG.md`, replace

```markdown
The surface under contract: the `AgentProvider` protocol (`build_command`,
`child_environment`, `prepare_home`, `tool_call_completed`, `run`), `RunSpec`,
`RunResult` / `TokenUsage`, `CapabilityProfile` / `McpServer` / `ToolGuard` /
`Credentials`, `chain.run_chain`, `envelope.unwrap`, and the exit codes
`PROVIDER_FALLBACK_EXIT_CODE = 3`, `TIMEOUT_EXIT_CODE = 124` and
`TIMEOUT_REPLAYABLE_EXIT_CODE = 4`. A change to any of these is a **breaking** entry
below and a major-or-minor bump while the package is 0.x; a new provider is additive.
```

with

```markdown
The surface under contract: the `AgentProvider` protocol (`build_command`,
`child_environment`, `prepare_home`, `tool_call_completed`, `run`), `RunSpec`,
`RunResult` / `TokenUsage`, `RunResult.to_dict()` (schema 1, the shape of `result.json`),
the `registry` facade (`get_provider`, `PROVIDER_NAMES`, `probe`, `max_prompt_bytes`),
`CapabilityProfile` / `McpServer` / `ToolGuard` / `Credentials`, `chain.run_chain`,
`envelope.unwrap`, and the exit codes `PROVIDER_FALLBACK_EXIT_CODE = 3`,
`TIMEOUT_EXIT_CODE = 124` and `TIMEOUT_REPLAYABLE_EXIT_CODE = 4`. A change to any of
these is a **breaking** entry below and a major-or-minor bump while the package is 0.x;
a new provider is additive.
```

- [ ] **Step 2: Add the unreleased entry**

In the same file, insert immediately before the `## 0.3.0 — 2026-09-20` heading:

```markdown
## Unreleased — 0.4.0, lot 1 of 4: the facade

Nothing here is tagged yet: 0.4.0 ships after lot 4 (the `ha` CLI), per
`docs/specs/2026-09-23-headless-agents-0.4.0-design.md`.

### Added
- `registry`: `get_provider(name)`, `PROVIDER_NAMES`, `UnknownProvider` (a `ValueError`
  whose message lists the valid names), `probe(name) -> Probe(available, detail,
  version)` — zero quota: the executable on `PATH` and its `--version`, never a model
  call, bounded by a timeout — and `max_prompt_bytes(name)`: `None` for the stdin rails
  (claude, codex), the argv limit for agy and opencode. Read it instead of hard-coding a
  limit.
- `RunResult.text`: the final answer, read by the provider from the file its rail writes
  the answer to (`report_log` for codex, agy and opencode; the bytes this run appended to
  `raw_log` for claude). Verbatim — an answer that is itself JSON is never re-read as an
  envelope — and `None` when the run failed or answered nothing.
- `RunResult.run_id`, `RunResult.stderr_log`, `RunResult.raw_log`, and
  `RunResult.to_dict()`: the JSON-safe schema-1 form (`result.RESULT_SCHEMA_VERSION = 1`).
  `context`, `workspace` and `branch` belong to the key set from schema 1 and stay `null`
  until lots 2 and 4 fill them.
- `RunSpec.run_dir`: the logs a caller leaves unset default to `report.log`,
  `events.jsonl`, `stderr.log` and `raw.log` inside it (explicit paths still win), its
  name is the `run_id`, and the run writes `result.json` there.
```

- [ ] **Step 3: Add the facade to the README**

In `packages/headless-agents/README.md`, replace the line

```markdown
An isolated seat -- no server, no user-level configuration, credentials
```

with

````markdown
The same kind of run through the facade, by provider name, with one directory
holding its logs and its `result.json`:

```python
from headless_agents.registry import get_provider, max_prompt_bytes, probe

if probe("codex").available:  # zero quota: the executable and its --version
    result = get_provider("codex").run(
        RunSpec(prompt="Summarise the diff.", model="<model>", run_dir=Path("runs/r-001"))
    )
    print(result.text)  # the final answer, or None when the run produced none
    # runs/r-001/ now holds report.log, events.jsonl, stderr.log and result.json,
    # which is result.to_dict(): schema 1, the same keys for every provider.

limit = max_prompt_bytes("agy")  # an int for the argv rails, None for claude and codex
```

An isolated seat -- no server, no user-level configuration, credentials
````

- [ ] **Step 4: Verify the whole lot**

```bash
.venv/bin/pytest tests/unit/headless_agents -v
.venv/bin/pytest tests/unit/agents tests/unit/test_dream_provider_chain.py tests/unit/test_dream_codex_runner.py tests/unit/test_dream_opencode_runner.py -q
env -u BRAIN_V42_TEST_DB_URL POSTGRES_URL='postgresql+asyncpg://unused:unused@127.0.0.1:9/unused' .venv/bin/pytest tests/unit -q
.venv/bin/ruff check --fix packages/headless-agents tests/unit/headless_agents && .venv/bin/ruff format packages/headless-agents tests/unit/headless_agents
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy src/ packages/headless-agents/src/
```

Expected: every command green. The third runs the whole unit suite against an address where nothing listens, so no test can reach a real database; a failure there that also fails on `origin/main` is reported as pre-existing, with its name, never waved through.

- [ ] **Step 5: Commit**

```bash
git add packages/headless-agents/CHANGELOG.md packages/headless-agents/README.md
git commit -m "docs(headless-agents): document the 0.4.0 facade"
```

Then open the pull request for the branch. Lot 1 is done when its CI is complete and green and the PR is merged; the plan for lot 2 (the workspace and the context bundle) is written next, against the merged interfaces.
