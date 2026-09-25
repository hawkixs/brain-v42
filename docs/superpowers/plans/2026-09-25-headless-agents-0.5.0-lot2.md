# headless-agents 0.5.0 — Lot 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Execution method, chosen by the operator for this lot (2026-09-25, the lot 1 method
> Q60 = a):** this plan is reviewed by codex, then implemented inline, test-first, in the
> session that wrote it, in two pull requests (A and B below), each reviewed by an
> independent non-Claude reviewer and merged under delegation 39f7ea9f (approve verdict +
> required CI green, never `--admin`). No tag and no release in this lot: the tag follows
> lot 5 (spec §5), and both stay the operator's.

**Goal:** Deliver lot 2 of headless-agents 0.5.0 — the per-rail tool counters recorded in
`run.json`, `ha show RUN_ID [--json]` rebuilt from the state directory, and the complete
`ha runs` (duration, cost, first line of the task, legacy runs, active quarantines first).

**Architecture:** Each CLI rail gains a pure `count_tools(events_log)` that reads its own
event log through one strict reader; the `registry` facade dispatches on a `RunResult`'s
provider (`tool_counts`), and the engine records the count of each step's final link in
`run.json`. A new `show` module rebuilds a run's report from the state directory — identity
and status from the registry entry, a write run's status, lineage, branch and base from its
lineage state, its commits from the provenance records — and renders it; `cli.py` stays a
thin adapter. Neither `ha show` nor `ha runs` runs git; the only lock either touches is the
non-blocking liveness probe `locks.is_free` that lot 1's `ha runs` already uses.

**Tech Stack:** Python 3.12 standard library (`json`, `collections.Counter`, `pathlib`),
pytest. No new dependency.

**Spec:** `docs/specs/2026-09-24-headless-agents-0.5.0-design.md`. Read §3.8.1, §3.8.3 step
9, §3.8.5, §3.9, §3.10, §3.11 and §4 before any task. The lot 1 plan
(`docs/superpowers/plans/2026-09-25-headless-agents-0.5.0-lot1.md`, decision P7) states what
lot 1 left to this one.

## Global Constraints

- Runtime dependencies stay `pydantic` and `structlog`; no import of `brain_v42` from
  `packages/headless-agents` (`tests/unit/headless_agents/test_package_boundary.py`).
- Unchanged: `result.json` schema 1 and its key set, the `AgentProvider` protocol, and
  `run_chain`'s own result (spec §2 Out; §3.11: "the `AgentProvider` protocol is
  unchanged", "a step's `result.json` stays schema 1").
- Unchanged: the `run.json` key sets `RUN_KEYS` and `STEP_KEYS` (spec §3.10, pinned by
  `tests/unit/headless_agents/test_report.py`). The counts fill `steps[].tools` and nothing
  else — "the counts live in `run.json` only" (§3.11).
- `null` means "not measured", never zero (§3.10): `tools` is `null` when the rail cannot
  measure or its log cannot be read whole, `{}` when it measured no tool call.
- Tool names stay each rail's own; they are never normalised across rails (§3.11).
- A report is never an authority (§3.8.1): identity and status come from the registry
  entry, a write run's status from its lineage state; a `run.json` whose `run_id` is not
  the run's own is ignored.
- 0.4.0 runs are not registered: `ha runs` lists them as `legacy`, and no option accepts
  them (§3.8.1).
- `ha show` and `ha runs` run no git command and never lift a quarantine (§3.8.5, P5).
- Everything written for GitHub — code, comments, tests, fixtures, commits, PRs — in English.
- Gates before every commit, as CI runs them: `.venv/bin/pytest tests/unit/`,
  `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`,
  `.venv/bin/mypy src/ packages/headless-agents/src/`. Outside the canonical root, export
  `POSTGRES_URL` before `pytest tests/unit/` (CLAUDE.md, Tests). The code blocks below are
  not pre-formatted: run `.venv/bin/ruff format` and `.venv/bin/ruff check --fix` on the
  files a task touches before its gates (line length 100, isort).
- The expected counts and golden texts below were produced by running this plan's code on
  its fixtures and reports (2026-09-25), not computed by hand.

## Decisions this plan takes where the spec leaves the choice to it

| # | Decision | Why |
|---|---|---|
| P1 | Each rail's counter is a module-level `count_tools(events_log: Path \| None) -> dict[str, int] \| None` in its provider module (`providers/codex.py`, `opencode.py`, `agy.py`), on top of one strict reader `event_log.read_events`. The facade `registry.tool_counts(result: RunResult)` dispatches on `result.provider`. | §3.11: "each CLI rail gains a function that counts its tool calls by name from its own event log, reached through the registry; the `AgentProvider` protocol is unchanged". A module function beside the rail's other event readers (`tool_call_started`, `write_tool_started`) keeps the protocol untouched and the format knowledge in one file per rail. |
| P2 | A call is counted once, however many events it writes: codex by `item.id`, opencode by `part.id`, agy by `(conversation_id, step_index)`. A call that was refused, failed, or started and never finished still counts: the agent made it. A tool event that does not carry its whole identity (no `item.id`, no `part.id`, no `conversation_id` or no integer `step_index`) makes the whole count unmeasured. | Measured 2026-09-25 on recorded logs: codex writes `item.started` then `item.completed` under one `item.id` (38 of 38 calls in the Dream log `2026-09-25_watchk-claude_synth`); agy writes `ACTIVE` then `DONE` or `ERROR` under one `step_index` (29 of 29 in `2026-09-15_watchk-claude_synth`), and every one of the 4 938 tool-step events of the 84 agy-served Dream logs carries a `conversation_id`, one conversation per log; opencode writes one `tool_use` per call (4 of 4, and 30 of 30 in the operator's three recorded `ha` runs). Counting events would double codex and agy. (Codex review of this plan, round 1: an identity key half-checked would merge calls.) |
| P3 | claude's counts stay `null` in lot 2: the facade wires no counter for it. The live telemetry measurement that could publish them (§4 `live`) ships with the live suite of lot 5, together with its counter and the proof that gates it. | §3.11: "`null` until a `live` test proves the telemetry complete at exit". Measured 2026-09-25: no recorded claude OTEL log with a `claude_code.tool_result` record exists on the operator's machine (none in the Dream's `.otel.log` files, none in `~/.cache/ha/runs`), so a counter written now could only be tested against a synthetic format. |
| P4 | Codex item types that are not tool calls: `agent_message`, `reasoning`, `error`. Every other item type is a tool call, counted under its own type name. | §3.11 excludes messages and reasoning; an `error` item is neither a call nor a message. Item types observed in 507 recorded codex logs: `agent_message`, `command_execution`, `mcp_tool_call`; §3.11 also names `file_change`. |
| P5 | `ha show` reads a write run's diffstat from its run directory's `change.patch` (display only) and runs no git; `ha runs` runs none either. | §3.8.5: every entry point checks the quarantines before its first git command. A display command that runs none needs no such check, and a quarantined repository's runs stay readable. |
| P6 | `ha show` rebuilds from the state: `run_id`, `target` and `repository` from the registry entry; `status` from the entry, or from the lineage state for a write run, with `running`/`incomplete` from the lifecycle lock; for a write run, `lineage`, `branch` and `base` from the lineage state and `commits` from the provenance records naming the run. The fields whose authority is a state record that a later lot introduces are `null`, never taken from `run.json`: `verdict` and `vendor_check` (a review's result in `<state>/reviews/`, §3.8.1 and §3.8.6), `continues`, `findings_from` and `implement_providers` (§3.10: "copy what the state directory records"), and `cleanup` (a review's; `null` on every other run, §3.10). Lot 4 reads the review result when it ships its writer, lot 3 the continuation records. Every other field is display data, taken from `run.json` when its `run_id` is the run's own and `null` otherwise. What it could not show as written is named on stderr; `--json` prints the rebuilt document, with `RUN_KEYS` exactly. An unreadable or malformed registry entry (a well-formed JSON object missing or mistyping a field included: `Registry.resolve` raises `Unknown` for it, never `KeyError`), an unreadable lineage state or provenance record — or a readable lineage that does not list the run — reads `unknown` and exits `1`; `ha runs` applies the same rule to its rows (Task 8). | §3.8.1: one authority per fact, unknown "never as empty", and a report "never read back to decide anything"; §3.8.3 step 9: "after both, only a stale report, rebuilt from the state by `ha show`". A write's lineage is created with its first member already listed (`write_flow._intent`), so a lineage silent about a run it owns is never a run in progress. (Codex review of this plan, round 1.) Reading `<state>/reviews/` now would fix the format of a file nothing writes before lot 4, whose spec defines it with its writer; a report-supplied `vendor_check` would be the forged proof §3.8.6 exists to prevent. (Codex review of this plan, round 2.) |
| P7 | `ha runs --json` stays a JSON list of run rows (lot 1's shape); each row gains `task`, `cost_usd` and `cost_complete` and keeps `text`. The active quarantines come first in text mode and, with `--json`, on stderr, one `ha: quarantine …` line each. | A list stays readable by lot 1's callers and tests. A quarantine is an operator alert, and every entry point it refuses already names it. |
| P8 | Column widths of `ha show`'s step table are computed per table (the widest cell, cells two spaces apart); the example of §3.10 is a layout, not a byte-level contract. | Role names reach 64 characters and model labels vary (`opencode-go/deepseek-v4.1-flash`): fixed widths would misalign or truncate. |
| P9 | Lot 2 ships as two pull requests: A (Tasks 1–5, counters) and B (Tasks 6–8, `ha show` and `ha runs`). | The counters touch three rails and the engine; `ha show` and `ha runs` touch the display side only: each is reviewed on its own. |

## Review Focus

Five inputs the spec implies but no spec test names, most likely first; each has a test in
the task that owns the code.

1. **An event log cut short by a kill** — a provider killed mid-write leaves a half-written
   last line. A person expects `tools` to read "not measured" (`-` in `ha show`), never a
   partial count shown as complete. Tests in Tasks 1, 2 and 3.
2. **`ha show` of a run that `ha clean` removed** — the run directory, `run.json` and
   `prompt.md` are gone. A person expects the run shown from its registry entry (target,
   status), a stderr line saying it was cleaned and when, and exit `0` — not a crash or
   an error. Tests in Tasks 6 and 7.
3. **`ha show` of a 0.4.0 run id** — the id format is the same and the directory exists in
   the runs cache. A person expects exit `2` naming it a 0.4.0 run that `ha runs` lists and
   no command accepts, not a generic "no run". Tests in Tasks 6 and 7.
4. **A `run.json` that names another run** — a directory copied by hand, a report edited.
   A person expects it ignored: the state's facts shown, and stderr saying why. Test in
   Task 6.
5. **An unreadable quarantine file or registry entry while listing** — `ha runs` must still
   list everything else, the quarantine as unreadable (it still refuses, §3.8.5), the run
   as `unknown`, and exit `0`. Test in Task 8.

---

## File structure

```text
packages/headless-agents/src/headless_agents/
  event_log.py           NEW  read_events: a JSON-lines log read whole, or not measured (P2)
  providers/codex.py     MOD  NON_TOOL_ITEM_TYPES, count_tools (§3.11, P4)
  providers/opencode.py  MOD  count_tools
  providers/agy.py       MOD  count_tools
  registry.py            MOD  tool_counts(result) — the facade (P1, P3)
  report.py              MOD  PROMPT_FILE; step_entry(..., tools=)
  engine.py              MOD  records tool_counts(final) for the step; PROMPT_FILE
  provenance.py          MOD  of_run(state, run_id): a run's commits, and what cannot be read
  quarantine.py          MOD  active(state): every quarantine in force, the operator's first
  show.py                NEW  rebuild() from the state, render(), read_task, read_diffstat,
                              format_duration/cost/tokens/tools (§3.10, P5, P6, P8)
  cli.py                 MOD  ha show RUN_ID [--json]; complete ha runs (P7)
packages/headless-agents/README.md   MOD  CLI synopsis gains `ha show` (lot 1 rule P8)
tests/unit/headless_agents/
  fixtures/tool_counts/codex.events.jsonl      NEW  recorded, redacted
  fixtures/tool_counts/opencode.events.jsonl   NEW  recorded, redacted
  fixtures/tool_counts/agy.events.jsonl        NEW  recorded, redacted
  test_tool_counts.py      NEW  the reader, the three counters, the facade
  test_report.py           MOD  a step carries its counts
  test_engine_execute.py   MOD  a step records its counts; chain; HTTP; claude
  test_write_flow.py       MOD  a write step records its counts
  test_show.py             NEW  rebuild from the state; golden rendering
  test_quarantine.py       MOD  active()
  test_cli.py              MOD  ha show; ha runs columns and quarantines
```

## Pull requests

| PR | Tasks | Content | Leaves `main` |
|---|---|---|---|
| A | 1–5 | the strict reader, the three counters with their recorded fixtures, the facade, `steps[].tools` in `run.json` | every new run records its tool counts |
| B | 6–8 | `ha show` (rebuild, render), `provenance.of_run`, `quarantine.active`, the complete `ha runs`, README synopsis | lot 2 complete |

---

# PR A — tool counters

### Task 1: The strict reader, the codex counter, and its recorded log

**Files:**
- Create: `packages/headless-agents/src/headless_agents/event_log.py`
- Create: `tests/unit/headless_agents/fixtures/tool_counts/codex.events.jsonl`
- Modify: `packages/headless-agents/src/headless_agents/providers/codex.py` (imports; new
  `NON_TOOL_ITEM_TYPES` and `count_tools` after `write_tool_started`)
- Create: `tests/unit/headless_agents/test_tool_counts.py`

**Interfaces:**
- Produces:

```python
# event_log.py
def read_events(path: Path | None) -> list[dict[str, object]] | None
# providers/codex.py
NON_TOOL_ITEM_TYPES: Final[frozenset[str]]   # {"agent_message", "reasoning", "error"}
def count_tools(events_log: Path | None) -> dict[str, int] | None
```

- [ ] **Step 1: Record the fixture.** The operator's `ha` run `20260925T101109-6a3b6211`
  (a codex review: four shell calls, one of them failing with exit 2, and two messages),
  structure verbatim, free text redacted, thread id replaced. Create
  `tests/unit/headless_agents/fixtures/tool_counts/codex.events.jsonl` with exactly:

```text
{"type": "thread.started", "thread_id": "fixture-thread-0001"}
{"type": "turn.started"}
{"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "<redacted>"}}
{"type": "item.started", "item": {"id": "item_1", "type": "command_execution", "command": "<redacted command>", "aggregated_output": "", "exit_code": null, "status": "in_progress"}}
{"type": "item.completed", "item": {"id": "item_1", "type": "command_execution", "command": "<redacted command>", "aggregated_output": "", "exit_code": 2, "status": "failed"}}
{"type": "item.started", "item": {"id": "item_2", "type": "command_execution", "command": "<redacted command>", "aggregated_output": "", "exit_code": null, "status": "in_progress"}}
{"type": "item.completed", "item": {"id": "item_2", "type": "command_execution", "command": "<redacted command>", "aggregated_output": "", "exit_code": 0, "status": "completed"}}
{"type": "item.started", "item": {"id": "item_3", "type": "command_execution", "command": "<redacted command>", "aggregated_output": "", "exit_code": null, "status": "in_progress"}}
{"type": "item.completed", "item": {"id": "item_3", "type": "command_execution", "command": "<redacted command>", "aggregated_output": "", "exit_code": 0, "status": "completed"}}
{"type": "item.started", "item": {"id": "item_4", "type": "command_execution", "command": "<redacted command>", "aggregated_output": "", "exit_code": null, "status": "in_progress"}}
{"type": "item.completed", "item": {"id": "item_4", "type": "command_execution", "command": "<redacted command>", "aggregated_output": "", "exit_code": 0, "status": "completed"}}
{"type": "item.completed", "item": {"id": "item_5", "type": "agent_message", "text": "<redacted>"}}
{"type": "turn.completed", "usage": {"input_tokens": 108250, "cached_input_tokens": 88832, "cache_write_input_tokens": 0, "output_tokens": 2373, "reasoning_output_tokens": 1814}}
```

- [ ] **Step 2: Write the failing tests** — `tests/unit/headless_agents/test_tool_counts.py`:

```python
"""Tool counts per rail, from each rail's own event log (spec 0.5.0 §3.11).

The fixtures under ``fixtures/tool_counts/`` are recorded logs: real runs of each
rail, their structure kept verbatim and their free text redacted (plan Tasks 1-3).
"""

from __future__ import annotations

import json
from pathlib import Path

from headless_agents.event_log import read_events
from headless_agents.providers import codex

FIXTURES = Path(__file__).parent / "fixtures" / "tool_counts"


def _log(tmp_path: Path, *events: object, tail: str = "") -> Path:
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events) + tail, encoding="utf-8")
    return path


# ── the reader ──────────────────────────────────────────────────────────────


def test_read_events_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"type": "a"}\n\n   \n{"type": "b"}\n', encoding="utf-8")
    assert read_events(path) == [{"type": "a"}, {"type": "b"}]


def test_read_events_cannot_vouch_for_a_log_it_cannot_read_whole(tmp_path: Path) -> None:
    assert read_events(None) is None
    assert read_events(tmp_path / "absent.jsonl") is None
    assert read_events(_log(tmp_path, {"type": "a"}, tail='{"type": "item.star')) is None
    not_an_object = tmp_path / "list.jsonl"
    not_an_object.write_text("[1, 2]\n", encoding="utf-8")
    assert read_events(not_an_object) is None
    not_utf8 = tmp_path / "bytes.jsonl"
    not_utf8.write_bytes(b'{"type": "\xff"}\n')
    assert read_events(not_utf8) is None


# ── codex ───────────────────────────────────────────────────────────────────


def test_codex_counts_a_recorded_run_by_item_type() -> None:
    """Four shell calls -- one failed with exit 2, still a call -- and two messages."""
    assert codex.count_tools(FIXTURES / "codex.events.jsonl") == {"command_execution": 4}


def test_codex_counts_an_item_once_across_its_events(tmp_path: Path) -> None:
    item = {"id": "item_1", "type": "mcp_tool_call", "server": "brain-v42",
            "tool": "brain_search", "arguments": {}, "result": None, "error": None}
    log = _log(
        tmp_path,
        {"type": "item.started", "item": {**item, "status": "in_progress"}},
        {"type": "item.updated", "item": {**item, "status": "in_progress"}},
        {"type": "item.completed", "item": {**item, "status": "completed"}},
    )
    assert codex.count_tools(log) == {"mcp_tool_call": 1}


def test_codex_counts_a_call_that_never_completed(tmp_path: Path) -> None:
    """A provider killed mid-call: the call started, so it may have run."""
    log = _log(
        tmp_path,
        {"type": "item.started", "item": {"id": "item_7", "type": "file_change", "status": "in_progress"}},
    )
    assert codex.count_tools(log) == {"file_change": 1}


def test_codex_excludes_messages_reasoning_and_errors(tmp_path: Path) -> None:
    log = _log(
        tmp_path,
        {"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "<redacted>"}},
        {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "<redacted>"}},
        {"type": "item.completed", "item": {"id": "item_2", "type": "error", "message": "<redacted>"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    )
    assert codex.count_tools(log) == {}


def test_codex_without_a_whole_log_is_not_measured(tmp_path: Path) -> None:
    assert codex.count_tools(None) is None
    assert codex.count_tools(tmp_path / "absent.jsonl") is None
    assert codex.count_tools(_log(tmp_path, tail='{"type": "item.started", "item": {"id"')) is None
    anonymous = _log(tmp_path, {"type": "item.started", "item": {"type": "command_execution"}})
    assert codex.count_tools(anonymous) is None
```

- [ ] **Step 3: Run to verify they fail**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_tool_counts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'headless_agents.event_log'`.

- [ ] **Step 4: Implement.** Create `packages/headless-agents/src/headless_agents/event_log.py`:

```python
"""A rail's event log, read for a measurement (spec 0.5.0 §3.11).

The rails' own readers skip a line they cannot parse: right for a guard that
must keep going. A count cannot do that. A log with a line it cannot read is a
count it cannot vouch for, so the count is "not measured" -- ``None`` -- never a
partial number passed off as complete. A provider killed mid-write can leave a
half-written last line: that run's tools then read as not measured.
"""

from __future__ import annotations

import json
from pathlib import Path


def read_events(path: Path | None) -> list[dict[str, object]] | None:
    """Every event of the JSON-lines log at ``path``, or ``None`` when it cannot be read whole.

    Blank lines are skipped. ``None`` when ``path`` is ``None``, missing,
    unreadable or not UTF-8, or when any other line is not a JSON object.
    """
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    events: list[dict[str, object]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            return None
        if not isinstance(event, dict):
            return None
        events.append(event)
    return events


__all__ = ["read_events"]
```

  In `providers/codex.py`, add `from collections import Counter` after `import time`,
  change `from typing import Any` to `from typing import Any, Final`, add
  `from ..event_log import read_events` after `from ..capability import (...)`, and after
  `write_tool_started` add:

```python
#: codex item types that are not tool calls (spec §3.11 excludes messages and
#: reasoning; an ``error`` item is neither a call nor a message -- plan P4).
NON_TOOL_ITEM_TYPES: Final = frozenset({"agent_message", "reasoning", "error"})


def count_tools(events_log: Path | None) -> dict[str, int] | None:
    """Tool calls in this ``--json`` event stream, by item type (spec 0.5.0 §3.11).

    codex writes ``item.started`` then ``item.completed`` for one call, under
    one ``item.id``: a call is counted once, by that id (plan P2) -- one that
    started and never completed included, since it may have run. ``None`` when
    the stream cannot be read whole, or names a tool item with no id: not
    measured, never a partial count.
    """
    events = read_events(events_log)
    if events is None:
        return None
    calls: dict[str, str] = {}
    for event in events:
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if not isinstance(kind, str) or kind in NON_TOOL_ITEM_TYPES:
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str):
            return None
        calls.setdefault(item_id, kind)
    return dict(Counter(calls.values()))
```

- [ ] **Step 5: Run to verify they pass**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_tool_counts.py -v`
Expected: PASS (7 tests).

- [ ] **Step 6: Commit**

```bash
git add packages/headless-agents/src/headless_agents/event_log.py \
  packages/headless-agents/src/headless_agents/providers/codex.py \
  tests/unit/headless_agents/fixtures/tool_counts/codex.events.jsonl \
  tests/unit/headless_agents/test_tool_counts.py
git commit -m "feat(headless-agents): count codex tool calls from its event log"
```

### Task 2: The opencode counter, and its recorded log

**Files:**
- Create: `tests/unit/headless_agents/fixtures/tool_counts/opencode.events.jsonl`
- Modify: `packages/headless-agents/src/headless_agents/providers/opencode.py` (imports;
  `count_tools` after `write_tool_started`)
- Modify: `tests/unit/headless_agents/test_tool_counts.py`

**Interfaces:**
- Consumes: `event_log.read_events` (Task 1).
- Produces: `opencode.count_tools(events_log: Path | None) -> dict[str, int] | None`.

- [ ] **Step 1: Record the fixture.** The operator's `ha` run `20260924T035559-62bb47a2`
  (opencode: one `grep`, one `glob`, then an answer), structure verbatim, free text and
  snapshots redacted, session, message, part and call ids replaced. Create
  `tests/unit/headless_agents/fixtures/tool_counts/opencode.events.jsonl` with exactly:

```text
{"type": "step_start", "timestamp": 1790214961990, "sessionID": "ses_fixture_01", "part": {"id": "prt_fixture_01", "messageID": "msg_fixture_01", "sessionID": "ses_fixture_01", "snapshot": "<redacted>", "type": "step-start"}}
{"type": "tool_use", "timestamp": 1790214962184, "sessionID": "ses_fixture_01", "part": {"type": "tool", "tool": "grep", "callID": "call_fixture_01", "state": {"status": "completed", "input": {}, "output": "<redacted>", "metadata": {}, "title": "", "time": {"start": 1790214961999, "end": 1790214962181}}, "id": "prt_fixture_02", "sessionID": "ses_fixture_01", "messageID": "msg_fixture_01"}}
{"type": "tool_use", "timestamp": 1790214962191, "sessionID": "ses_fixture_01", "part": {"type": "tool", "tool": "glob", "callID": "call_fixture_02", "state": {"status": "completed", "input": {}, "output": "<redacted>", "metadata": {}, "title": "", "time": {"start": 1790214962004, "end": 1790214962186}}, "id": "prt_fixture_03", "sessionID": "ses_fixture_01", "messageID": "msg_fixture_01"}}
{"type": "step_finish", "timestamp": 1790214962209, "sessionID": "ses_fixture_01", "part": {"id": "prt_fixture_04", "reason": "tool-calls", "snapshot": "<redacted>", "messageID": "msg_fixture_01", "sessionID": "ses_fixture_01", "type": "step-finish", "tokens": {"total": 3174, "input": 3110, "output": 64, "reasoning": 0, "cache": {"write": 0, "read": 0}}, "cost": 0.00024925}}
{"type": "step_start", "timestamp": 1790214972084, "sessionID": "ses_fixture_01", "part": {"id": "prt_fixture_05", "messageID": "msg_fixture_02", "sessionID": "ses_fixture_01", "snapshot": "<redacted>", "type": "step-start"}}
{"type": "text", "timestamp": 1790214972784, "sessionID": "ses_fixture_01", "part": {"id": "prt_fixture_06", "messageID": "msg_fixture_02", "sessionID": "ses_fixture_01", "type": "text", "text": "<redacted>", "time": {"start": 1790214972702, "end": 1790214972782}}}
{"type": "step_finish", "timestamp": 1790214972799, "sessionID": "ses_fixture_01", "part": {"id": "prt_fixture_07", "reason": "stop", "snapshot": "<redacted>", "messageID": "msg_fixture_02", "sessionID": "ses_fixture_01", "type": "step-finish", "tokens": {"total": 3307, "input": 3275, "output": 32, "reasoning": 0, "cache": {"write": 0, "read": 0}}, "cost": 0.000253625}}
```

- [ ] **Step 2: Write the failing tests** — append to `test_tool_counts.py`, and add
  `opencode` to the import line (`from headless_agents.providers import codex, opencode`):

```python
# ── opencode ────────────────────────────────────────────────────────────────


def test_opencode_counts_a_recorded_run_by_tool() -> None:
    assert opencode.count_tools(FIXTURES / "opencode.events.jsonl") == {"grep": 1, "glob": 1}


def test_opencode_counts_a_part_once(tmp_path: Path) -> None:
    part = {"type": "tool", "tool": "read", "callID": "call_1", "id": "prt_1",
            "state": {"status": "running"}}
    log = _log(
        tmp_path,
        {"type": "tool_use", "part": part},
        {"type": "tool_use", "part": {**part, "state": {"status": "completed"}}},
        {"type": "tool_use", "part": {**part, "id": "prt_2", "callID": "call_2", "tool": "edit"}},
    )
    assert opencode.count_tools(log) == {"read": 1, "edit": 1}


def test_opencode_ignores_steps_and_text(tmp_path: Path) -> None:
    log = _log(
        tmp_path,
        {"type": "step_start", "part": {"id": "prt_1", "type": "step-start"}},
        {"type": "text", "part": {"id": "prt_2", "type": "text", "text": "<redacted>"}},
    )
    assert opencode.count_tools(log) == {}


def test_opencode_without_a_whole_log_is_not_measured(tmp_path: Path) -> None:
    assert opencode.count_tools(None) is None
    assert opencode.count_tools(_log(tmp_path, tail='{"type": "tool_u')) is None
    anonymous = _log(tmp_path, {"type": "tool_use", "part": {"type": "tool", "tool": "read"}})
    assert opencode.count_tools(anonymous) is None
```

- [ ] **Step 3: Run to verify they fail**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_tool_counts.py -v -k opencode`
Expected: FAIL — `AttributeError: module 'headless_agents.providers.opencode' has no attribute 'count_tools'`.

- [ ] **Step 4: Implement.** In `providers/opencode.py`, add `from collections import Counter`
  after `import time`, add `from ..event_log import read_events` after
  `from ..capability import (...)`, and after `write_tool_started` add:

```python
def count_tools(events_log: Path | None) -> dict[str, int] | None:
    """Tool calls in this ``--format json`` event stream, by ``part.tool`` (spec 0.5.0 §3.11).

    opencode writes one ``tool_use`` event per call once it settles; a part
    seen twice is counted once, by ``part.id`` (plan P2). ``None`` when the
    stream cannot be read whole, or names a tool part with no id or tool: not
    measured, never a partial count.
    """
    events = read_events(events_log)
    if events is None:
        return None
    calls: dict[str, str] = {}
    for event in events:
        if event.get("type") != "tool_use":
            continue
        part = event.get("part")
        tool = part.get("tool") if isinstance(part, dict) else None
        part_id = part.get("id") if isinstance(part, dict) else None
        if not isinstance(tool, str) or not isinstance(part_id, str):
            return None
        calls.setdefault(part_id, tool)
    return dict(Counter(calls.values()))
```

- [ ] **Step 5: Run to verify they pass**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_tool_counts.py -v`
Expected: PASS (11 tests).

- [ ] **Step 6: Commit**

```bash
git add packages/headless-agents/src/headless_agents/providers/opencode.py \
  tests/unit/headless_agents/fixtures/tool_counts/opencode.events.jsonl \
  tests/unit/headless_agents/test_tool_counts.py
git commit -m "feat(headless-agents): count opencode tool calls from its event log"
```

### Task 3: The agy counter, and its recorded log

**Files:**
- Create: `tests/unit/headless_agents/fixtures/tool_counts/agy.events.jsonl`
- Modify: `packages/headless-agents/src/headless_agents/providers/agy.py` (imports;
  `count_tools` after `tool_call_completed`)
- Modify: `tests/unit/headless_agents/test_tool_counts.py`

**Interfaces:**
- Consumes: `event_log.read_events` (Task 1).
- Produces: `agy.count_tools(events_log: Path | None) -> dict[str, int] | None`.

- [ ] **Step 1: Record the fixture.** The Dream phase `2026-09-15_watchk-claude_synth`
  served by agy (release `84138170`): the opening events, one MCP call that succeeded, and
  a `view_file` and a `run_command` the guard refused, then the result — structure
  verbatim, free text redacted, the conversation id replaced. Create
  `tests/unit/headless_agents/fixtures/tool_counts/agy.events.jsonl` with exactly:

```text
{"event": "init", "conversation_id": "fixture-agy-0002", "init": {"model": "<redacted>", "cwd": "<redacted>", "tools": [], "permission_mode": "<redacted>"}}
{"event": "step_update", "step_update": {"conversation_id": "fixture-agy-0002", "step_index": 0, "state": "DONE", "step_type": "user_input"}}
{"event": "step_update", "step_update": {"conversation_id": "fixture-agy-0002", "step_index": 1, "state": "DONE", "step_type": "agent_response", "duration_seconds": 4.795272386, "usage": {"input_tokens": 15603, "output_tokens": 417, "thinking_tokens": 345, "cache_read_tokens": 0, "total_tokens": 16020}}}
{"event": "step_update", "step_update": {"conversation_id": "fixture-agy-0002", "step_index": 2, "state": "ACTIVE", "step_type": "tool", "tool_name": "call_mcp_tool", "tool_info": {"name": "call_mcp_tool", "parameters": {}}}}
{"event": "step_update", "step_update": {"conversation_id": "fixture-agy-0002", "step_index": 2, "state": "DONE", "step_type": "tool", "tool_name": "call_mcp_tool", "duration_seconds": 0.116541313, "tool_info": {"name": "call_mcp_tool", "parameters": {}}}}
{"event": "step_update", "step_update": {"conversation_id": "fixture-agy-0002", "step_index": 6, "state": "ACTIVE", "step_type": "tool", "tool_name": "view_file", "tool_info": {"name": "view_file", "parameters": {}}}}
{"event": "step_update", "step_update": {"conversation_id": "fixture-agy-0002", "step_index": 6, "state": "ERROR", "step_type": "tool", "tool_name": "view_file", "duration_seconds": 0.031531681, "tool_info": {"name": "view_file", "parameters": {}, "error": {"type": "TOOL_ERROR", "message": "<redacted>"}}}}
{"event": "step_update", "step_update": {"conversation_id": "fixture-agy-0002", "step_index": 56, "state": "ACTIVE", "step_type": "tool", "tool_name": "run_command", "tool_info": {"name": "run_command", "parameters": {}}}}
{"event": "step_update", "step_update": {"conversation_id": "fixture-agy-0002", "step_index": 56, "state": "ERROR", "step_type": "tool", "tool_name": "run_command", "duration_seconds": 0.174539048, "tool_info": {"name": "run_command", "parameters": {}, "error": {"type": "TOOL_ERROR", "message": "<redacted>"}}}}
{"event": "result", "result": {"conversation_id": "fixture-agy-0002", "status": "SUCCESS", "response": "<redacted>", "duration_seconds": 129.4562663, "num_turns": 1, "usage": {"input_tokens": 133605, "output_tokens": 9307, "thinking_tokens": 5197, "cache_read_tokens": 634901, "total_tokens": 142912}}}
```

- [ ] **Step 2: Write the failing tests** — append to `test_tool_counts.py`, and import
  `agy` too (`from headless_agents.providers import agy, codex, opencode`):

```python
# ── agy ─────────────────────────────────────────────────────────────────────


def test_agy_counts_a_recorded_run_by_tool_name() -> None:
    """One MCP call that succeeded; a file read and a command the guard refused."""
    assert agy.count_tools(FIXTURES / "agy.events.jsonl") == {
        "call_mcp_tool": 1,
        "view_file": 1,
        "run_command": 1,
    }


def test_agy_counts_a_step_once_across_its_states(tmp_path: Path) -> None:
    step = {"conversation_id": "c1", "step_index": 4, "step_type": "tool", "tool_name": "view_file"}
    log = _log(
        tmp_path,
        {"event": "step_update", "step_update": {**step, "state": "ACTIVE"}},
        {"event": "step_update", "step_update": {**step, "state": "DONE"}},
        {"event": "step_update", "step_update": {**step, "step_index": 6, "state": "ACTIVE"}},
    )
    assert agy.count_tools(log) == {"view_file": 2}


def test_agy_ignores_steps_that_are_not_tools(tmp_path: Path) -> None:
    log = _log(
        tmp_path,
        {"event": "step_update", "step_update": {"conversation_id": "c1", "step_index": 1,
                                                  "state": "DONE", "step_type": "agent_response"}},
    )
    assert agy.count_tools(log) == {}


def test_agy_without_a_whole_log_is_not_measured(tmp_path: Path) -> None:
    assert agy.count_tools(None) is None
    assert agy.count_tools(_log(tmp_path, tail='{"event": "step_up')) is None
    unindexed = _log(
        tmp_path,
        {"event": "step_update", "step_update": {"conversation_id": "c1", "step_type": "tool",
                                                  "tool_name": "view_file", "state": "ACTIVE"}},
    )
    assert agy.count_tools(unindexed) is None


def test_agy_without_a_conversation_id_is_not_measured(tmp_path: Path) -> None:
    """Codex review of this plan (round 1): half an identity would merge two calls."""
    anonymous = _log(
        tmp_path,
        {"event": "step_update", "step_update": {"step_index": 3, "step_type": "tool",
                                                  "tool_name": "view_file", "state": "ACTIVE"}},
    )
    assert agy.count_tools(anonymous) is None
```

- [ ] **Step 3: Run to verify they fail**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_tool_counts.py -v -k agy`
Expected: FAIL — `AttributeError: module 'headless_agents.providers.agy' has no attribute 'count_tools'`.

- [ ] **Step 4: Implement.** In `providers/agy.py`, add `from collections import Counter`
  after `import time`, add `from ..event_log import read_events` after
  `from ..context import xml_attribute`, and after `tool_call_completed` add:

```python
def count_tools(events_log: Path | None) -> dict[str, int] | None:
    """Tool calls in this ``stream-json`` flow, by ``tool_name`` (spec 0.5.0 §3.11).

    agy writes a ``tool`` step ``ACTIVE`` before the tool runs, then ``DONE``
    or ``ERROR`` (see :func:`tool_call_started`): a call is counted once, by
    its conversation and step index (plan P2). A step the guard refused
    counts -- the agent made the call. ``None`` when the stream cannot be read
    whole, or names a tool step without its tool name, its conversation id or
    an integer index: not measured, never a partial count.
    """
    events = read_events(events_log)
    if events is None:
        return None
    calls: dict[tuple[str, int], str] = {}
    for event in events:
        step = event.get("step_update")
        if not isinstance(step, dict) or step.get("step_type") != "tool":
            continue
        tool = step.get("tool_name")
        conversation, index = step.get("conversation_id"), step.get("step_index")
        if (
            not isinstance(tool, str)
            or not isinstance(conversation, str)
            or not isinstance(index, int)
            or isinstance(index, bool)
        ):
            return None
        calls.setdefault((conversation, index), tool)
    return dict(Counter(calls.values()))
```

- [ ] **Step 5: Run to verify they pass**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_tool_counts.py -v`
Expected: PASS (16 tests).

- [ ] **Step 6: Commit**

```bash
git add packages/headless-agents/src/headless_agents/providers/agy.py \
  tests/unit/headless_agents/fixtures/tool_counts/agy.events.jsonl \
  tests/unit/headless_agents/test_tool_counts.py
git commit -m "feat(headless-agents): count agy tool calls from its event log"
```

### Task 4: The facade — `registry.tool_counts`

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/registry.py`
- Modify: `tests/unit/headless_agents/test_tool_counts.py`

**Interfaces:**
- Consumes: `codex.count_tools`, `opencode.count_tools`, `agy.count_tools` (Tasks 1–3).
- Produces: `registry.tool_counts(result: RunResult) -> dict[str, int] | None` — `{}` for an
  HTTP provider, `None` for claude (P3), the rail's count otherwise; `UnknownProvider` for
  a name the registry does not know.

- [ ] **Step 1: Write the failing tests** — append to `test_tool_counts.py`; add
  `import pytest`, `from headless_agents.registry import HTTP_PROVIDER_NAMES, UnknownProvider, tool_counts`
  and `from headless_agents.result import RunResult` to the imports:

```python
# ── the facade ──────────────────────────────────────────────────────────────


def _result(provider: str, events_log: Path | None, raw_log: Path | None = None) -> RunResult:
    return RunResult(
        exit_code=0,
        provider=provider,
        model="m",
        report_path=None,
        events_log=events_log,
        raw_log=raw_log,
        tokens=None,
        duration_seconds=1.0,
        tool_call_completed=False,
    )


def test_the_facade_reaches_each_rails_counter() -> None:
    assert tool_counts(_result("codex", FIXTURES / "codex.events.jsonl")) == {"command_execution": 4}
    assert tool_counts(_result("opencode", FIXTURES / "opencode.events.jsonl")) == {"grep": 1, "glob": 1}
    assert tool_counts(_result("agy", FIXTURES / "agy.events.jsonl")) == {
        "call_mcp_tool": 1,
        "view_file": 1,
        "run_command": 1,
    }


@pytest.mark.parametrize("provider", sorted(HTTP_PROVIDER_NAMES))
def test_an_http_provider_has_no_tools(provider: str) -> None:
    assert tool_counts(_result(provider, None)) == {}


def test_claude_is_not_measured_even_with_telemetry(tmp_path: Path) -> None:
    """Plan P3: null until a live test proves its telemetry complete at exit."""
    raw = tmp_path / "raw.log"
    raw.write_text('body: "claude_code.tool_result"\nattributes: { tool_name: "Read", success: "true" }\n')
    assert tool_counts(_result("claude", tmp_path / "events.jsonl", raw)) is None


def test_an_unknown_provider_is_refused() -> None:
    with pytest.raises(UnknownProvider):
        tool_counts(_result("nope", None))
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_tool_counts.py -v -k "facade or http or claude or unknown"`
Expected: FAIL — `ImportError: cannot import name 'tool_counts' from 'headless_agents.registry'`.

- [ ] **Step 3: Implement.** In `registry.py`: add `from pathlib import Path`; add
  `from .providers.agy import count_tools as agy_tools`,
  `from .providers.codex import count_tools as codex_tools`,
  `from .providers.opencode import count_tools as opencode_tools` and
  `from .result import RunResult` beside the provider imports; update the module
  docstring's first paragraph to name `tool_counts` among the facade's questions. Then,
  after `max_prompt_bytes`:

```python
#: The rails whose own event log counts their tool calls (spec §3.11). claude
#: is not here: its counts stay ``None`` until a live test proves its
#: telemetry complete at exit (plan P3).
_TOOL_COUNTERS: Final[Mapping[str, Callable[[Path | None], dict[str, int] | None]]] = {
    "codex": codex_tools,
    "opencode": opencode_tools,
    "agy": agy_tools,
}


def tool_counts(result: RunResult) -> dict[str, int] | None:
    """The tool calls of ``result``'s run, by the rail's own names (spec 0.5.0 §3.11).

    ``{}`` for an HTTP provider -- it has no tools; ``None`` -- not measured --
    for claude (plan P3) and for a rail whose event log cannot be read whole.
    """
    name = _known(result.provider)
    if name in HTTP_PROVIDER_NAMES:
        return {}
    counter = _TOOL_COUNTERS.get(name)
    return None if counter is None else counter(result.events_log)
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_tool_counts.py -v`
Expected: PASS (all of them, the parametrised HTTP case once per provider).

- [ ] **Step 5: Commit**

```bash
git add packages/headless-agents/src/headless_agents/registry.py tests/unit/headless_agents/test_tool_counts.py
git commit -m "feat(headless-agents): tool_counts on the registry facade"
```

### Task 5: `run.json` records each step's counts

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/report.py` (`step_entry`)
- Modify: `packages/headless-agents/src/headless_agents/engine.py` (both `step_entry` call sites)
- Modify: `tests/unit/headless_agents/test_report.py`
- Modify: `tests/unit/headless_agents/test_engine_execute.py` (`_Fake` gains `events`)
- Modify: `tests/unit/headless_agents/test_write_flow.py` (`_Agent` gains `events`)

**Interfaces:**
- Consumes: `registry.tool_counts` (Task 4).
- Produces: `step_entry(*, index, slot, role, step_dir, result, tools: Mapping[str, int] | None = None)`
  — `steps[].tools` is `dict(tools)`, or `None` when not measured. The engine passes
  `tool_counts(final)`: the counts of the link that answered. A chain advances only on
  exit `3` or `4`, when the link has proven no tool call started, so the final link's log
  holds every call of the step.

- [ ] **Step 1: Write the failing tests.** In `test_report.py` (it has a `_result(**overrides)`
  helper), add:

```python
def test_a_step_carries_its_tool_counts() -> None:
    entry = step_entry(
        index=1, slot="run", role="codex", step_dir="steps/01-run-codex", result=_result(),
        tools={"command_execution": 4},
    )
    assert entry["tools"] == {"command_execution": 4}


def test_a_step_without_counts_is_not_measured() -> None:
    entry = step_entry(index=1, slot="run", role="codex", step_dir="steps/01-run-codex", result=_result())
    assert entry["tools"] is None
```

  In `test_engine_execute.py`, give `_Fake` a field `events: str | None = None` and, in
  `run()`, right after `spec = spec.with_run_dir_defaults()`:

```python
        if self.events is not None:
            assert spec.events_log is not None
            spec.events_log.parent.mkdir(parents=True, exist_ok=True)
            spec.events_log.write_text(self.events, encoding="utf-8")
```

  then add:

```python
TOOL_FIXTURES = Path(__file__).parent / "fixtures" / "tool_counts"


def _steps(outcome: engine.Outcome) -> list[dict[str, object]]:
    report = json.loads((outcome.run_dir / "run.json").read_text())
    steps = report["steps"]
    assert isinstance(steps, list)
    return steps


def test_a_step_records_its_tool_counts(world: World) -> None:
    world.fakes["codex"] = _Fake("codex", events=(TOOL_FIXTURES / "codex.events.jsonl").read_text())
    assert _steps(world.run("codex"))[0]["tools"] == {"command_execution": 4}


def test_a_step_whose_log_is_missing_is_not_measured(world: World) -> None:
    assert _steps(world.run("codex"))[0]["tools"] is None


def test_a_claude_step_is_not_measured(world: World) -> None:
    assert _steps(world.run("claude"))[0]["tools"] is None


def test_an_http_step_has_no_tools(world: World) -> None:
    outcome = world.run(
        "openrouter",
        overrides=Overrides(model="some/model"),
        environ={"PATH": "/usr/bin:/bin", "HOME": str(world.home), "OPENROUTER_API_KEY": "k"},
    )
    assert _steps(outcome)[0]["tools"] == {}


def test_a_chain_records_the_counts_of_the_link_that_answered(world: World) -> None:
    world.roles('[pair]\nchain = ["claude:c", "codex:x"]\n')
    world.fakes["claude"] = _Fake("claude", code=3)
    world.fakes["codex"] = _Fake("codex", events=(TOOL_FIXTURES / "codex.events.jsonl").read_text())
    (step,) = _steps(world.run("pair"))
    assert step["provider"] == "codex" and step["tools"] == {"command_execution": 4}
```

  In `test_write_flow.py`, give `_Agent` a field `events: str | None = None`, write it the
  same way in `run()` after `spec = spec.with_run_dir_defaults()`, and add:

```python
def test_a_committed_write_records_its_tool_counts(world: World) -> None:
    world.agent.edit = _edit_app
    world.agent.events = (
        Path(__file__).parent / "fixtures" / "tool_counts" / "codex.events.jsonl"
    ).read_text()
    outcome = world.write()
    report = json.loads((outcome.run_dir / "run.json").read_text())
    assert report["steps"][0]["tools"] == {"command_execution": 4}
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_report.py tests/unit/headless_agents/test_engine_execute.py tests/unit/headless_agents/test_write_flow.py -v -k "tool or counts or measured or http_step"`
Expected: FAIL — `TypeError: step_entry() got an unexpected keyword argument 'tools'`, and
the engine tests with `None` where counts are expected.

- [ ] **Step 3: Implement.** In `report.py`, give `step_entry` the parameter
  `tools: Mapping[str, int] | None = None` and replace the lot 1 placeholder line and its
  comment with:

```python
        # Spec §3.11: the rail's own names; None when the rail could not measure.
        "tools": dict(tools) if tools is not None else None,
```

  In `engine.py`, import `tool_counts` with the registry names
  (`from .registry import HTTP_PROVIDER_NAMES, get_provider, max_prompt_bytes, probe, tool_counts`)
  and pass it at both call sites: `result=outcome.final, tools=tool_counts(outcome.final)` in
  `_execute_write`, and `result=final, tools=tool_counts(final)` in `execute`.

- [ ] **Step 4: Run to verify they pass**, then the package suite and the Dream's golden
  fixtures (the Dream uses the providers directly and must be untouched):

Run: `.venv/bin/pytest tests/unit/headless_agents tests/unit/agents -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/headless-agents/src/headless_agents/report.py \
  packages/headless-agents/src/headless_agents/engine.py \
  tests/unit/headless_agents/test_report.py tests/unit/headless_agents/test_engine_execute.py \
  tests/unit/headless_agents/test_write_flow.py
git commit -m "feat(headless-agents): run.json records each step's tool counts"
```

---

# PR B — `ha show` and the complete `ha runs`

### Task 6: `show.rebuild` — a run's report from the state

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/report.py` (`PROMPT_FILE`)
- Modify: `packages/headless-agents/src/headless_agents/engine.py` (write `prompt.md` through `PROMPT_FILE`)
- Modify: `packages/headless-agents/src/headless_agents/provenance.py` (`of_run`)
- Modify: `packages/headless-agents/src/headless_agents/runs.py` (`Registry._entry` validates
  the entry; `resolve` raises `Unknown` on a malformed one)
- Create: `packages/headless-agents/src/headless_agents/show.py`
- Create: `tests/unit/headless_agents/test_show.py`
- Modify: `tests/unit/headless_agents/test_runs.py`, `tests/unit/headless_agents/test_cli.py`

**Interfaces:**
- Consumes: `runs.Registry` (`resolve`, `effective_status`), `lineage.load`,
  `report.RUN_KEYS`/`SCHEMA`/`RUN_JSON`, `write_flow.PATCH_FILE`,
  `run_record.RESULT_FILE_NAME`.
- Produces:

```python
# report.py
PROMPT_FILE: Final = "prompt.md"
# runs.py -- resolve() raises Unknown, never KeyError, on a well-formed JSON entry whose
# run_dir is missing or not a non-empty string, whose target is not an object, or whose
# repository, lineage, status or cleaned_at is neither a string nor null
Registry._entry(document: Mapping[str, object], path: Path) -> Entry
# provenance.py
def of_run(state: Path, run_id: str) -> tuple[list[dict[str, object]], list[Path]]
    # the records naming run_id, and the record files that cannot be read
# show.py
LATER_AUTHORITIES: Final[tuple[str, ...]]  # null in lot 2, never from run.json (P6)
class NotShown(ValueError)            # not a run id, no registered run, a 0.4.0 run -> exit 2
@dataclass(frozen=True) class Diffstat: insertions: int; deletions: int; files: int
@dataclass(frozen=True) class Shown:
    report: dict[str, object]         # RUN_KEYS exactly, rebuilt (P6)
    task: str | None                  # first non-blank line of prompt.md
    diffstat: Diffstat | None         # a write run's change.patch, counted (P5)
    notes: tuple[str, ...]            # what could not be shown as written -> stderr
    warnings: tuple[str, ...]         # shown on the page: a compromised lineage
    unknown: bool                     # an authority is unreadable, or silent on the run -> exit 1
def read_task(run_dir: Path) -> str | None
def read_diffstat(run_dir: Path) -> Diffstat | None
def rebuild(run_id: str, *, state: Path, runs_root: Path) -> Shown
```

- [ ] **Step 1: Write the failing tests** — `tests/unit/headless_agents/test_show.py`:

```python
"""``ha show``: a run rebuilt from the state directory (spec 0.5.0 §3.8.1, §3.10; plan P6)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from headless_agents import lineage as lineages
from headless_agents import locks, provenance, show
from headless_agents.report import RUN_KEYS
from headless_agents.runs import Registry

RUN = "20260925T120000-ab12cd34"
OTHER = "20260925T130000-cd34ef56"
SHA_1 = "1" * 40
SHA_2 = "2" * 40
BASE = "b" * 40

_STEP: dict[str, object] = {
    "index": 1, "slot": "run", "role": "codex", "dir": "steps/01-run-codex",
    "provider": "codex", "model": "gpt-6-luna", "model_reported": None, "exit_code": 0,
    "duration_seconds": 65.4,
    "tokens": {"input": 12345, "output": 2100, "fresh": None, "cached": None, "thinking": None},
    "cost_usd": 0.031, "tools": {"command_execution": 4, "mcp_tool_call": 1}, "verdict": None,
}


class Home:
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def state(self) -> Path:
        return self.root / ".local" / "state" / "ha"

    @property
    def runs(self) -> Path:
        return self.root / ".cache" / "ha" / "runs"

    def registry(self) -> Registry:
        return Registry(self.state, runs_root=self.runs)

    def rebuild(self, run_id: str = RUN) -> show.Shown:
        return show.rebuild(run_id, state=self.state, runs_root=self.runs)

    def report(self, **fields: object) -> dict[str, object]:
        document: dict[str, object] = dict.fromkeys(RUN_KEYS)
        document.update(
            schema=1, run_id=RUN, target={"kind": "provider", "name": "codex"},
            status="answered", exit_code=0, text="It is a persistent memory server.\n",
            repository=str(self.root / "repo"), steps=[dict(_STEP)], cost_usd=0.031,
            cost_complete=True, duration_seconds=65.4,
        )
        document.update(fields)
        return document

    def read_only(self, *, entry_status: str = "answered", **report: object) -> Path:
        """A read-only run: its registry entry says ``entry_status``, its run.json ``report``."""
        registry = self.registry()
        entry = registry.create(
            RUN, run_dir=None, target={"kind": "provider", "name": "codex"},
            repository=self.root / "repo", lineage=None,
        )
        entry.run_dir.mkdir(parents=True)
        (entry.run_dir / "prompt.md").write_text("Explain what this repository does.\nMore.\n")
        (entry.run_dir / "run.json").write_text(json.dumps(self.report(**report)))
        registry.set_status(RUN, entry_status)
        return entry.run_dir

    def write_run(self, *, member: str = "committed", compromised: str | None = None, **report: object) -> Path:
        registry = self.registry()
        entry = registry.create(
            RUN, run_dir=None, target={"kind": "role", "name": "implementer"},
            repository=self.root / "repo", lineage=RUN,
        )
        entry.run_dir.mkdir(parents=True)
        lineages.create(
            self.state,
            lineages.LineageState(
                owner=RUN, repository=self.root / "repo", common_dir=self.root / "repo" / ".git",
                worktree=entry.run_dir / "wt", branch=f"ha/{RUN}", base=BASE,
                members={RUN: member}, pending=None, compromised=compromised,
            ),
        )
        provenance.record(self.state, SHA_1, run_id=RUN, lineage=RUN, made_by="engine", providers=[])
        provenance.record(self.state, SHA_2, run_id=OTHER, lineage=RUN, made_by="engine", providers=["codex"])
        (entry.run_dir / "run.json").write_text(
            json.dumps(self.report(target={"kind": "role", "name": "implementer"}, **report))
        )
        return entry.run_dir


@pytest.fixture
def home(tmp_path: Path) -> Home:
    return Home(tmp_path / "home")


def test_a_finished_run_is_shown_as_its_report_says(home: Home) -> None:
    home.read_only()
    shown = home.rebuild()
    assert list(shown.report) == list(RUN_KEYS)
    assert shown.report["status"] == "answered" and shown.report["exit_code"] == 0
    assert shown.report["steps"] == [_STEP]
    assert shown.task == "Explain what this repository does."
    assert shown.notes == () and shown.warnings == () and shown.unknown is False


def test_a_stale_report_is_rebuilt_from_the_registry(home: Home) -> None:
    home.read_only(entry_status="answered", status="running", exit_code=None)
    shown = home.rebuild()
    assert shown.report["status"] == "answered"
    assert any("run.json says running; the state says answered" in note for note in shown.notes)


def test_liveness_comes_from_the_lifecycle_lock(home: Home) -> None:
    home.read_only(entry_status="running", status="running")
    assert home.rebuild().report["status"] == "incomplete"
    lock = home.registry().lifecycle_lock(RUN)
    with locks.held(lock, rank=locks.Rank.LIFECYCLE, exclusive=True, wait=None, what="test"):
        assert home.rebuild().report["status"] == "running"


def test_a_cleaned_run_is_shown_from_its_entry(home: Home) -> None:
    run_dir = home.read_only()
    shutil.rmtree(run_dir)
    home.registry().set_cleaned(RUN, "2026-09-25T13:00:00Z")
    shown = home.rebuild()
    assert shown.report["status"] == "answered"
    assert shown.report["target"] == {"kind": "provider", "name": "codex"}
    assert shown.report["steps"] == [] and shown.report["text"] is None
    assert shown.task is None and shown.unknown is False
    assert any("removed by ha clean at 2026-09-25T13:00:00Z" in note for note in shown.notes)


def test_a_report_naming_another_run_is_ignored(home: Home) -> None:
    home.read_only(run_id=OTHER, text="forged")
    shown = home.rebuild()
    assert shown.report["run_id"] == RUN and shown.report["text"] is None
    assert any("names another run" in note for note in shown.notes)


def test_identity_comes_from_the_entry_not_the_report(home: Home) -> None:
    home.read_only(target={"kind": "role", "name": "forged"})
    assert home.rebuild().report["target"] == {"kind": "provider", "name": "codex"}


def test_a_report_never_supplies_what_a_later_lots_state_record_owns(home: Home) -> None:
    """Codex review of this plan (round 2): a review's verdict and vendor check live in its
    review result in the state (spec §3.8.1, §3.8.6) -- a forged run.json must not show them."""
    forged: dict[str, object] = {
        "verdict": "APPROVE",
        "vendor_check": {"commits": [], "authors": ["codex"], "reviewers": {}},
        "cleanup": {"status": "done"},
        "continues": OTHER,
        "findings_from": OTHER,
        "implement_providers": ["codex"],
    }
    assert set(forged) == set(show.LATER_AUTHORITIES)
    home.read_only(**forged)
    report = home.rebuild().report
    assert all(report[key] is None for key in forged)


def test_a_write_run_is_rebuilt_from_its_lineage_and_provenance(home: Home) -> None:
    """A crash after the lineage rename: run.json still says running, and holds no commit."""
    home.write_run(status="running", commits=None, branch=None, base=None)
    shown = home.rebuild()
    report = shown.report
    assert report["status"] == "committed"
    assert report["lineage"] == RUN and report["branch"] == f"ha/{RUN}" and report["base"] == BASE
    assert report["commits"] == [{"sha": SHA_1, "made_by": "engine"}]
    assert shown.unknown is False


def test_a_compromised_lineage_is_a_warning(home: Home) -> None:
    home.write_run(compromised="tripwire")
    assert home.rebuild().warnings == (f"lineage {RUN} is compromised: tripwire",)


def test_an_unreadable_lineage_is_unknown(home: Home) -> None:
    home.write_run()
    lineages.lineage_path(home.state, RUN).write_text("{not json")
    shown = home.rebuild()
    assert shown.report["status"] == "unknown" and shown.unknown is True


def test_a_lineage_silent_about_the_run_is_unknown(home: Home) -> None:
    """Codex review of this plan (round 1): no member status is no status, not a live run."""
    home.write_run()
    path = lineages.lineage_path(home.state, RUN)
    document = json.loads(path.read_text())
    document["members"] = {}
    path.write_text(json.dumps(document))
    lock = home.registry().lifecycle_lock(RUN)
    with locks.held(lock, rank=locks.Rank.LIFECYCLE, exclusive=True, wait=None, what="test"):
        shown = home.rebuild()
    assert shown.report["status"] == "unknown" and shown.unknown is True
    assert any(f"the lineage {RUN} does not list this run" in note for note in shown.notes)


def test_an_unreadable_provenance_record_makes_the_commits_unknown(home: Home) -> None:
    home.write_run()
    (home.state / "provenance" / f"{'3' * 40}.json").write_text("{not json")
    shown = home.rebuild()
    assert shown.unknown is True
    assert any("provenance" in note for note in shown.notes)


def test_an_unreadable_registry_entry_is_unknown(home: Home) -> None:
    home.read_only()
    (home.state / "runs" / f"{RUN}.json").write_text("{not json")
    shown = home.rebuild()
    assert shown.report["status"] == "unknown" and shown.unknown is True


def test_a_malformed_registry_entry_is_unknown(home: Home) -> None:
    """Codex review of this plan (round 3): valid JSON without its run_dir raised KeyError."""
    home.read_only()
    path = home.state / "runs" / f"{RUN}.json"
    document = json.loads(path.read_text())
    del document["run_dir"]
    path.write_text(json.dumps(document))
    shown = home.rebuild()
    assert shown.report["status"] == "unknown" and shown.unknown is True


@pytest.mark.parametrize(
    ("run_id", "needle"),
    [("nope", "not a run id"), ("20260925T000000-00000000", "no run 20260925T000000-00000000")],
)
def test_what_is_not_a_registered_run_is_not_shown(home: Home, run_id: str, needle: str) -> None:
    with pytest.raises(show.NotShown, match=needle):
        home.rebuild(run_id)


def test_a_legacy_run_is_named_as_such(home: Home) -> None:
    legacy = home.runs / "20260920T000000-aaaaaaaa"
    legacy.mkdir(parents=True)
    (legacy / "result.json").write_text(json.dumps({"provider": "codex", "exit_code": 0}))
    with pytest.raises(show.NotShown, match="is a 0.4.0 run: ha runs lists it, no command accepts it"):
        home.rebuild("20260920T000000-aaaaaaaa")


def test_the_diffstat_counts_lines_inside_hunks_only(home: Home) -> None:
    run_dir = home.write_run()
    (run_dir / "change.patch").write_text(
        "diff --git a/README.md b/README.md\n"
        "index 3b18e51..a0f3c9e 100644\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1,3 +1,5 @@\n"
        " # Title\n"
        "-teh typo\n"
        "+the typo\n"
        "+++counter stays a line\n"
        "+one more\n"
    )
    assert home.rebuild().diffstat == show.Diffstat(insertions=3, deletions=1, files=1)
```

  In `test_runs.py` (it already imports `json`, `pytest`, `Path`, `Unknown` and the
  registry names), add:

```python
@pytest.mark.parametrize(
    "broken",
    [
        {"run_dir": None},
        {"run_dir": ""},
        {"run_dir": 7},
        {"target": "codex"},
        {"repository": 1},
        {"lineage": 5},
        {"status": ["answered"]},
        {"cleaned_at": 0},
    ],
)
def test_a_well_formed_entry_with_a_malformed_field_is_unknown(
    tmp_path: Path, broken: dict[str, object]
) -> None:
    """Codex review of the lot 2 plan (round 3): read() vouches for the JSON and the id
    only; a missing or mistyped field escaped resolve() as KeyError."""
    registry = Registry(tmp_path / "state", runs_root=tmp_path / "runs")
    run_id = "20260925T000000-eeeeeeee"
    registry.create(
        run_id, run_dir=None, target={"kind": "provider", "name": "codex"},
        repository=None, lineage=None,
    )
    path = tmp_path / "state" / "runs" / f"{run_id}.json"
    document = json.loads(path.read_text())
    document.update(broken)
    path.write_text(json.dumps(document))
    with pytest.raises(Unknown):
        registry.resolve(run_id)


def test_an_entry_without_its_run_dir_is_unknown(tmp_path: Path) -> None:
    run_id = "20260925T000000-eeeeeeee"
    (tmp_path / "state" / "runs").mkdir(parents=True)
    (tmp_path / "state" / "runs" / f"{run_id}.json").write_text(
        json.dumps({"run_id": run_id, "status": "answered"})
    )
    registry = Registry(tmp_path / "state", runs_root=tmp_path / "runs")
    with pytest.raises(Unknown, match="run_dir"):
        registry.resolve(run_id)
```

  In `test_cli.py`, add:

```python
def test_runs_and_clean_survive_a_malformed_registry_entry(world: _World) -> None:
    """Codex review of the lot 2 plan (round 3): lot 1 crashed here with a KeyError."""
    code, out, _ = world.run("run", "codex", "--json", "go")
    run_id = json.loads(out)["run_id"]
    path = world.state / "runs" / f"{run_id}.json"
    document = json.loads(path.read_text())
    del document["run_dir"]
    path.write_text(json.dumps(document))
    code, out, _ = world.run("runs", "--json")
    (row,) = json.loads(out)
    assert code == 0 and row["status"] == "unknown"
    code, _, err = world.run("clean", run_id)
    assert code == 1 and "recover it by hand" in err
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_show.py tests/unit/headless_agents/test_runs.py tests/unit/headless_agents/test_cli.py -v -k "show or malformed or run_dir or survive"`
Expected: FAIL — `ImportError: cannot import name 'show' from 'headless_agents'`, and
`KeyError: 'run_dir'` out of `Registry.resolve` in the registry and CLI tests.

- [ ] **Step 3: Implement.** In `report.py`, add `PROMPT_FILE: Final = "prompt.md"` under
  `RUN_JSON` and to `__all__`; in `engine.py`, import it with the other report names and
  write the task with `(run_dir / PROMPT_FILE).write_text(plan.prompt, encoding="utf-8")`.
  In `provenance.py`, import `Unknown` and `read` from `.state` beside the existing names
  and add (and export) `of_run`:

```python
def of_run(state: Path, run_id: str) -> tuple[list[dict[str, object]], list[Path]]:
    """Every provenance record naming ``run_id``, and the record files that cannot be read.

    Provenance is keyed by commit, so this scans ``<state>/provenance/``. A
    record that cannot be read is returned apart, never skipped: it may be one
    of this run's commits, and unknown is never empty (spec §3.8.1).
    """
    directory = state / "provenance"
    if not directory.is_dir():
        return [], []
    found: list[dict[str, object]] = []
    unreadable: list[Path] = []
    for path in sorted(directory.glob("*.json")):
        try:
            document = read(path, expect_id=("sha", path.stem))
        except Unknown:
            unreadable.append(path)
            continue
        if document.get("run_id") == run_id:
            found.append(document)
    return found, unreadable
```

  In `runs.py`, import `Unknown` from `.state` beside `create_once, publish, read`; call
  `self._entry(document, self._path(run_id))` in `create` and
  `self._entry(read(path, expect_id=("run_id", run_id)), path)` in `resolve`; and replace
  `_entry` with:

```python
    @staticmethod
    def _entry(document: Mapping[str, object], path: Path) -> Entry:
        """The entry ``document`` states; :class:`Unknown` when a field is missing or ill-typed.

        ``read`` vouches for the JSON and the id only: a well-formed object without its
        ``run_dir`` escaped as ``KeyError`` and crashed ``ha runs`` and ``ha clean``
        (codex review of the lot 2 plan, round 3). Unknown is never empty (§3.8.1).
        """
        run_dir, target = document.get("run_dir"), document.get("target")
        if not isinstance(run_dir, str) or not run_dir:
            raise Unknown(f"{path}: run_dir is malformed")
        if not isinstance(target, dict):
            raise Unknown(f"{path}: target is malformed")
        for key in ("repository", "lineage", "status", "cleaned_at"):
            value = document.get(key)
            if value is not None and not isinstance(value, str):
                raise Unknown(f"{path}: {key} is malformed")
        return Entry(
            run_id=str(document["run_id"]),
            run_dir=Path(run_dir),
            repository=_optional_path(document.get("repository")),
            target={str(k): str(v) for k, v in target.items()},
            lineage=_optional_str(document.get("lineage")),
            status=_optional_str(document.get("status")),
            cleaned_at=_optional_str(document.get("cleaned_at")),
        )
```

  `engine.clean` and `cli._registered_row` already catch `Unknown` ("recover it by hand",
  exit `1`; a row `unknown`): no other caller changes.

  Create `packages/headless-agents/src/headless_agents/show.py`:

```python
"""``ha show``: one run, rebuilt from the state directory, then rendered (spec 0.5.0 §3.9, §3.10).

``run.json`` is a report, never an authority (§3.8.1): a crash after a write's
publication leaves it stale (§3.8.3 step 9), and ``ha clean`` removes it with
the run directory. So every fact with an authority in the state comes from the
state -- identity from the registry entry, the status from the entry or the
lineage state, a write's lineage, branch, base and commits from the lineage
state and the provenance records -- and the rest is display data, read from a
report only when it names this run (plan P6). Nothing here runs git (plan P5);
the only lock touched is the non-blocking liveness probe of the lifecycle lock.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from . import lineage as lineages
from . import provenance
from .report import PROMPT_FILE, RUN_JSON, RUN_KEYS, SCHEMA
from .run_record import RESULT_FILE_NAME
from .runs import RUN_ID_PATTERN, Entry, Registry, RegistryError
from .state import Unknown
from .write_flow import PATCH_FILE

#: Fields whose one authority is a state record a later lot introduces (plan P6): a
#: review's result (spec §3.8.1, §3.8.6) and the continuation records (§3.10).
#: ``ha show`` never takes them from ``run.json``; lots 3 and 4 fill them from the state.
LATER_AUTHORITIES: Final = (
    "verdict", "vendor_check", "cleanup", "continues", "findings_from", "implement_providers",
)


class NotShown(ValueError):
    """What ``ha show`` cannot name: not a run id, no registered run, or a 0.4.0 run."""


@dataclass(frozen=True)
class Diffstat:
    insertions: int
    deletions: int
    files: int


@dataclass(frozen=True)
class Shown:
    report: dict[str, object]
    task: str | None
    diffstat: Diffstat | None
    notes: tuple[str, ...]
    warnings: tuple[str, ...]
    unknown: bool


def read_task(run_dir: Path) -> str | None:
    """The first non-blank line of the run's ``prompt.md``; ``None`` when there is none."""
    try:
        text = (run_dir / PROMPT_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return next((line.strip() for line in text.splitlines() if line.strip()), None)


def read_diffstat(run_dir: Path) -> Diffstat | None:
    """Count ``change.patch`` (``git diff --binary base HEAD``): files, and lines inside hunks.

    Only a line after a hunk header counts, so a file header's ``+++``/``---``
    does not, and an added line whose own text starts with ``++`` does.
    """
    try:
        text = (run_dir / PATCH_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    insertions = deletions = files = 0
    in_hunk = False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            files += 1
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            insertions += 1
        elif in_hunk and line.startswith("-"):
            deletions += 1
    return Diffstat(insertions=insertions, deletions=deletions, files=files)


def _bare(run_id: str) -> dict[str, object]:
    """What the state alone says of a run: its steps, text and measures are unknown."""
    document: dict[str, object] = dict.fromkeys(RUN_KEYS)
    document.update(schema=SCHEMA, run_id=run_id, steps=[], cost_complete=False)
    return document


def _usable_report(entry: Entry, notes: list[str]) -> dict[str, object] | None:
    """The run's ``run.json`` cut to the pinned key set, when it names this run."""
    path = entry.run_dir / RUN_JSON
    if not path.exists():
        if entry.cleaned_at is not None:
            notes.append(f"removed by ha clean at {entry.cleaned_at}: shown from the state")
        else:
            notes.append(f"{path} is missing: shown from the state")
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        notes.append(f"{path} cannot be read: shown from the state")
        return None
    if not isinstance(document, dict) or document.get("run_id") != entry.run_id:
        notes.append(f"{path} names another run: ignored, shown from the state")
        return None
    return {key: document.get(key) for key in RUN_KEYS}


def _commits(reported: object, recorded: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """The recorded commits: in the report's order where it has one, the rest by sha."""
    by_sha = {str(r.get("sha")): {"sha": r.get("sha"), "made_by": r.get("made_by")} for r in recorded}
    order = [c.get("sha") for c in reported if isinstance(c, dict)] if isinstance(reported, list) else []
    known = [by_sha.pop(sha) for sha in order if isinstance(sha, str) and sha in by_sha]
    return known + [by_sha[sha] for sha in sorted(by_sha)]


def rebuild(run_id: str, *, state: Path, runs_root: Path) -> Shown:
    """``run_id``'s report, rebuilt from the state (plan P6).

    :class:`NotShown` when ``run_id`` names no registered run -- a 0.4.0 run
    named as such (§3.8.1). ``unknown`` is set when the registry entry, the
    lineage state or a provenance record cannot be read.
    """
    registry = Registry(state, runs_root=runs_root)
    try:
        entry = registry.resolve(run_id)
    except RegistryError as exc:
        legacy = runs_root / run_id
        if (
            RUN_ID_PATTERN.fullmatch(run_id)
            and (legacy / RESULT_FILE_NAME).is_file()
            and not (legacy / RUN_JSON).exists()
        ):
            raise NotShown(
                f"{run_id} is a 0.4.0 run: ha runs lists it, no command accepts it"
            ) from None
        raise NotShown(str(exc)) from None
    except Unknown as exc:
        document = _bare(run_id)
        document["status"] = "unknown"
        return Shown(
            report=document, task=None, diffstat=None,
            notes=(f"{exc}: recover it by hand",), warnings=(), unknown=True,
        )
    notes: list[str] = []
    warnings: list[str] = []
    unknown = False
    report = _usable_report(entry, notes)
    document = dict(report) if report is not None else _bare(run_id)
    document.update(
        run_id=run_id,
        target=dict(entry.target),
        repository=str(entry.repository) if entry.repository is not None else None,
    )
    # Plan P6: their authority is a state record a later lot adds; run.json never supplies them.
    document.update(dict.fromkeys(LATER_AUTHORITIES))
    status: str | None = None
    lineage_status: str | None = None
    if entry.lineage is not None:
        try:
            lineage = lineages.load(state, entry.lineage)
        except Unknown as exc:
            notes.append(f"{exc}: the lineage cannot be read")
            status, unknown = "unknown", True
        else:
            document.update(lineage=lineage.owner, branch=lineage.branch, base=lineage.base)
            if run_id in lineage.members:
                lineage_status = lineage.members[run_id]
            else:
                # The lineage is created with its first member listed (write_flow._intent):
                # silence about this run is no status, never "running" (plan P6).
                notes.append(f"the lineage {lineage.owner} does not list this run")
                status, unknown = "unknown", True
            if lineage.compromised is not None:
                warnings.append(f"lineage {lineage.owner} is compromised: {lineage.compromised}")
        recorded, unreadable = provenance.of_run(state, run_id)
        if unreadable:
            notes.append(
                f"{len(unreadable)} provenance record(s) cannot be read: the commits may be incomplete"
            )
            unknown = True
        document["commits"] = _commits(document.get("commits"), recorded)
    if status is None:
        status = registry.effective_status(entry, lineage_status)
    if report is not None and report.get("status") != status:
        notes.append(f"run.json says {report.get('status')}; the state says {status}: shown from the state")
    document["status"] = status
    return Shown(
        report=document,
        task=read_task(entry.run_dir),
        diffstat=read_diffstat(entry.run_dir) if entry.lineage is not None else None,
        notes=tuple(notes),
        warnings=tuple(warnings),
        unknown=unknown,
    )


__all__ = [
    "LATER_AUTHORITIES", "Diffstat", "NotShown", "Shown", "read_diffstat", "read_task", "rebuild",
]
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/unit/headless_agents -q`
Expected: PASS — the new tests, and lot 1's registry, engine and CLI tests unchanged.

- [ ] **Step 5: Commit** (two commits: the registry fix stands on its own)

```bash
git add packages/headless-agents/src/headless_agents/runs.py \
  tests/unit/headless_agents/test_runs.py tests/unit/headless_agents/test_cli.py
git commit -m "fix(headless-agents): a malformed registry entry is unknown, not a KeyError"
git add packages/headless-agents/src/headless_agents/show.py \
  packages/headless-agents/src/headless_agents/provenance.py \
  packages/headless-agents/src/headless_agents/report.py \
  packages/headless-agents/src/headless_agents/engine.py tests/unit/headless_agents/test_show.py
git commit -m "feat(headless-agents): rebuild a run's report from the state for ha show"
```

### Task 7: `show.render` against golden texts, and `ha show RUN_ID [--json]`

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/show.py` (`render` and the formatters)
- Modify: `packages/headless-agents/src/headless_agents/cli.py` (`show` subcommand; docstring synopsis)
- Modify: `packages/headless-agents/README.md` (CLI synopsis: `ha show RUN_ID [--json]` after `ha runs`)
- Modify: `tests/unit/headless_agents/test_show.py`, `tests/unit/headless_agents/test_cli.py`

**Interfaces:**
- Consumes: `show.rebuild`, `show.Shown`, `show.NotShown` (Task 6).
- Produces:

```python
def render(shown: Shown) -> str
def format_duration(seconds: object) -> str   # "45s", "2m40s", "1h02m"; "-" when not measured
def format_cost(cost: object) -> str          # "$0.12"; "-"
def format_tokens(tokens: object) -> str      # "in 150k out 3k"; "-"
def format_tools(tools: object) -> str        # "read 25, edit 11" most used first; "none" for {}; "-"
```

  `ha show RUN_ID [--json]`: exit `0` shown; `1` when `Shown.unknown`; `2` on `NotShown`
  (through `UsageError`). Notes go to stderr (`ha: …`); `--json` prints `Shown.report`.
  `ha show --dir` is lot 4: argparse refuses the option (exit `2`).

- [ ] **Step 1: Write the failing tests.** Append to `test_show.py` (the golden texts were
  produced by running this task's code on these reports; `P8`: widths are per table):

```python
HEAD = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"


def _shown(report: dict[str, object], *, task: str | None = None,
           diffstat: show.Diffstat | None = None, warnings: tuple[str, ...] = ()) -> show.Shown:
    return show.Shown(report=report, task=task, diffstat=diffstat, notes=(), warnings=warnings, unknown=False)


GOLDEN_READ_ONLY = """\
20260925T120000-ab12cd34  codex  exit 0  answered
task    Explain what this repository does.
  run  codex  codex  gpt-6-luna  0  1m05s  in 12k out 2k  $0.03  command_execution 4, mcp_tool_call 1
--- run ---
It is a persistent memory server.
"""


def test_a_read_only_run_renders_as_the_golden_text(home: Home) -> None:
    report = home.report()
    assert show.render(_shown(report, task="Explain what this repository does.")) == GOLDEN_READ_ONLY


GOLDEN_WRITE = """\
20260925T120000-ab12cd34  implementer  exit 0  committed
task    Fix the typo in README.
head    ha/20260925T120000-ab12cd34 @ 1a2b3c4  1 commit  +3 -1  1 file
  run  implementer  opencode  opencode-go/deepseek-v4.1-flash  0  3m10s  in 610k out 21k  $0.08  read 25, edit 11, bash 6
--- run ---
Fixed the typo.
"""


def test_a_write_run_renders_as_the_golden_text(home: Home) -> None:
    step = dict(_STEP, role="implementer", provider="opencode", model="opencode-go/deepseek-v4.1-flash",
                duration_seconds=190.0,
                tokens={"input": 610000, "output": 21000, "fresh": None, "cached": None, "thinking": None},
                cost_usd=0.08, tools={"read": 25, "edit": 11, "bash": 6})
    report = home.report(
        target={"kind": "role", "name": "implementer"}, status="committed", text="Fixed the typo.\n",
        branch=f"ha/{RUN}", head=HEAD, commits=[{"sha": HEAD, "made_by": "engine"}], steps=[step],
    )
    stat = show.Diffstat(insertions=3, deletions=1, files=1)
    assert show.render(_shown(report, task="Fix the typo in README.", diffstat=stat)) == GOLDEN_WRITE


GOLDEN_UNMEASURED = """\
20260925T120000-ab12cd34  claude  exit 1  failed
task    -
  run  claude  claude  sonnet  1  12s  -  -  -
"""


def test_what_was_not_measured_renders_as_a_dash(home: Home) -> None:
    step = dict(_STEP, role="claude", provider="claude", model="sonnet", exit_code=1,
                duration_seconds=12.0, tokens=None, cost_usd=None, tools=None)
    report = home.report(target={"kind": "provider", "name": "claude"}, status="failed",
                         exit_code=1, text=None, steps=[step])
    assert show.render(_shown(report)) == GOLDEN_UNMEASURED


GOLDEN_TWO_STEPS = """\
20260925T120000-ab12cd34  multi-review  exit 6  changes requested
task    Review this change.
  review  reviewer-agy  agy     (auto)  0  2m40s  -              -      view_file 9  CHANGES
  judge   judge         claude  opus    0  1m10s  in 90k out 3k  $0.40  -            CHANGES
--- judge ---
Rename the helper.
"""


def test_columns_align_across_steps(home: Home) -> None:
    """Pins the table layout lot 4's reviews will fill (plan P8)."""
    steps = [
        {"slot": "review", "role": "reviewer-agy", "provider": "agy", "model": None, "exit_code": 0,
         "duration_seconds": 160.0, "tokens": None, "cost_usd": None, "tools": {"view_file": 9},
         "verdict": "CHANGES"},
        {"slot": "judge", "role": "judge", "provider": "claude", "model": "opus", "exit_code": 0,
         "duration_seconds": 70.0, "tokens": {"input": 90000, "output": 3000}, "cost_usd": 0.4,
         "tools": None, "verdict": "CHANGES"},
    ]
    report = home.report(target={"kind": "workflow", "name": "multi-review"}, status="changes",
                         exit_code=6, text="Rename the helper.\n", steps=steps)
    assert show.render(_shown(report, task="Review this change.")) == GOLDEN_TWO_STEPS


def test_the_header_names_a_failure_reason_and_a_warning_line_follows(home: Home) -> None:
    report = home.report(status="failed", exit_code=1, failure_reason="agent_moved_head",
                         branch=f"ha/{RUN}", head=None, commits=[], steps=[], text=None)
    text = show.render(_shown(report, warnings=(f"lineage {RUN} is compromised: agent_moved_head",)))
    assert text.splitlines() == [
        f"{RUN}  codex  exit 1  failed (agent_moved_head)",
        "task    -",
        f"head    ha/{RUN} @ -  0 commits",
        f"warning lineage {RUN} is compromised: agent_moved_head",
    ]


@pytest.mark.parametrize(
    ("seconds", "text"), [(None, "-"), (0.4, "0s"), (59.4, "59s"), (65.4, "1m05s"), (3725, "1h02m")]
)
def test_format_duration(seconds: object, text: str) -> None:
    assert show.format_duration(seconds) == text


def test_format_tools_and_tokens() -> None:
    assert show.format_tools(None) == "-" and show.format_tools({}) == "none"
    assert show.format_tools({"edit": 11, "read": 25, "bash": 11}) == "read 25, bash 11, edit 11"
    assert show.format_tokens(None) == "-"
    assert show.format_tokens({"input": 999, "output": None}) == "in 999 out -"
    assert show.format_tokens({"input": 1_500_000, "output": 3000}) == "in 1.5M out 3k"
```

  In `test_cli.py`, add (with `from headless_agents.report import RUN_KEYS` at the top):

```python
# ── ha show ─────────────────────────────────────────────────────────────────


def test_show_renders_a_finished_run(world: _World) -> None:
    code, out, _ = world.run("run", "codex", "--json", "Explain the layout.\nDetails.")
    run_id = json.loads(out)["run_id"]
    code, out, err = world.run("show", run_id)
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines[0] == f"{run_id}  codex  exit 0  answered"
    assert lines[1] == "task    Explain the layout."
    assert lines[-2:] == ["--- run ---", "the answer"]


def test_show_json_prints_the_rebuilt_report(world: _World) -> None:
    code, out, _ = world.run("run", "codex", "--json", "go")
    run_id = json.loads(out)["run_id"]
    code, out, _ = world.run("show", run_id, "--json")
    report = json.loads(out)
    assert code == 0 and list(report) == list(RUN_KEYS) and report["run_id"] == run_id


def test_show_of_a_cleaned_run_says_so_and_exits_0(world: _World) -> None:
    code, out, _ = world.run("run", "codex", "--json", "go")
    run_id = json.loads(out)["run_id"]
    world.run("clean", run_id)
    code, out, err = world.run("show", run_id)
    assert code == 0 and "removed by ha clean" in err
    assert out.splitlines()[0] == f"{run_id}  codex  exit -  answered"


def test_show_refuses_what_is_not_a_registered_run(world: _World) -> None:
    code, _, err = world.run("show", "20260925T000000-00000000")
    assert code == 2 and "no run 20260925T000000-00000000" in err


def test_show_names_a_legacy_run(world: _World) -> None:
    legacy = world.home / ".cache" / "ha" / "runs" / "20260920T000000-aaaaaaaa"
    legacy.mkdir(parents=True)
    (legacy / "result.json").write_text(json.dumps({"provider": "codex", "exit_code": 0}))
    code, _, err = world.run("show", "20260920T000000-aaaaaaaa")
    assert code == 2 and "0.4.0 run" in err


def test_show_exits_1_when_the_state_cannot_be_read(world: _World) -> None:
    code, out, _ = world.run("run", "codex", "--json", "go")
    run_id = json.loads(out)["run_id"]
    (world.state / "runs" / f"{run_id}.json").write_text("{not json")
    code, out, err = world.run("show", run_id)
    assert code == 1 and "recover it by hand" in err
    assert out.splitlines()[0] == f"{run_id}  -  exit -  unknown"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_show.py tests/unit/headless_agents/test_cli.py -v -k "golden or render or align or format or header or show"`
Expected: FAIL — `AttributeError: module 'headless_agents.show' has no attribute 'render'`
and `ha show` refused by argparse (exit `2`).

- [ ] **Step 3: Implement.** Append to `show.py` (`Final` is imported since Task 6; add
  `"format_cost", "format_duration", "format_tokens", "format_tools", "render"` to
  `__all__`):

```python
_STATUS_WORDS: Final[Mapping[str, str]] = {"no_change": "no change", "changes": "changes requested"}


def format_duration(seconds: object) -> str:
    """``45s``, ``2m40s``, ``1h02m``; ``-`` when not measured."""
    if isinstance(seconds, bool) or not isinstance(seconds, int | float):
        return "-"
    whole = round(seconds)
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m{whole % 60:02d}s"
    return f"{whole // 3600}h{whole % 3600 // 60:02d}m"


def format_cost(cost: object) -> str:
    """``$0.12``; ``-`` when not measured."""
    if isinstance(cost, bool) or not isinstance(cost, int | float):
        return "-"
    return f"${cost:.2f}"


def _thousands(count: object) -> str:
    if not isinstance(count, int) or isinstance(count, bool):
        return "-"
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count // 1000}k"
    return f"{count / 1_000_000:.1f}M"


def format_tokens(tokens: object) -> str:
    """``in 150k out 3k``; ``-`` when neither side was measured."""
    if not isinstance(tokens, dict):
        return "-"
    given, produced = tokens.get("input"), tokens.get("output")
    if not isinstance(given, int) and not isinstance(produced, int):
        return "-"
    return f"in {_thousands(given)} out {_thousands(produced)}"


def format_tools(tools: object) -> str:
    """``read 25, edit 11``, most used first; ``none`` for ``{}``; ``-`` when not measured."""
    if not isinstance(tools, dict):
        return "-"
    counts = {str(name): count for name, count in tools.items() if isinstance(count, int)}
    if not counts:
        return "none"
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{name} {count}" for name, count in ranked)


def _dash(value: object) -> str:
    return "-" if value is None else str(value)


def _step_cells(step: Mapping[str, object]) -> list[str]:
    model, verdict = step.get("model"), step.get("verdict")
    return [
        _dash(step.get("slot")),
        _dash(step.get("role")),
        _dash(step.get("provider")),
        model if isinstance(model, str) and model.strip() else "(auto)",
        _dash(step.get("exit_code")),
        format_duration(step.get("duration_seconds")),
        format_tokens(step.get("tokens")),
        format_cost(step.get("cost_usd")),
        format_tools(step.get("tools")),
        verdict if isinstance(verdict, str) else "",
    ]


def _table(rows: Sequence[Sequence[str]]) -> list[str]:
    """Each column as wide as its widest cell, cells two spaces apart (plan P8)."""
    if not rows:
        return []
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    return [
        ("  " + "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True))).rstrip()
        for row in rows
    ]


def render(shown: Shown) -> str:
    """The text of ``ha show`` (spec §3.10): header, task, head, warnings, steps, final text."""
    report = shown.report
    target = report.get("target")
    name = target.get("name") if isinstance(target, dict) else None
    status = str(report.get("status"))
    header = (
        f"{report.get('run_id')}  {name or '-'}  exit {_dash(report.get('exit_code'))}  "
        f"{_STATUS_WORDS.get(status, status)}"
    )
    reason = report.get("failure_reason")
    if isinstance(reason, str):
        header += f" ({reason})"
    lines = [header, f"task    {shown.task or '-'}"]
    branch = report.get("branch")
    if isinstance(branch, str):
        commits = report.get("commits")
        count = len(commits) if isinstance(commits, list) else 0
        head = report.get("head")
        line = (
            f"head    {branch} @ {head[:7] if isinstance(head, str) else '-'}  "
            f"{count} commit{'' if count == 1 else 's'}"
        )
        if shown.diffstat is not None:
            stat = shown.diffstat
            line += (
                f"  +{stat.insertions} -{stat.deletions}  "
                f"{stat.files} file{'' if stat.files == 1 else 's'}"
            )
        lines.append(line)
    lines.extend(f"warning {warning}" for warning in shown.warnings)
    steps = report.get("steps")
    rows = (
        [_step_cells(step) for step in steps if isinstance(step, dict)]
        if isinstance(steps, list)
        else []
    )
    lines.extend(_table(rows))
    text = report.get("text")
    if isinstance(text, str) and text.strip():
        lines.append(f"--- {rows[-1][0] if rows else 'run'} ---")
        lines.append(text.rstrip("\n"))
    return "\n".join(lines) + "\n"
```

  In `cli.py`: add `from . import show` below `from . import lineage as lineages` (ruff's
  isort keeps an `as` import on its own line); add the `ha show RUN_ID [--json]` line to the
  module docstring's synopsis;
  in `_parser()`, after the `runs` parser:

```python
    show_parser = commands.add_parser("show", help="show one run, rebuilt from the state")
    show_parser.add_argument("run_id", help="the run id, as ha run or ha runs printed it")
    show_parser.add_argument("--json", action="store_true", help="print the rebuilt run.json")
```

  and the handler, wired in `main()` (`if args.command == "show": return _show(args, io)`,
  and the "a command is required" message naming `show`):

```python
def _show(args: argparse.Namespace, io: Io) -> int:
    """``ha show RUN_ID``: rebuilt from the state (plan P6); 1 when part of it is unreadable."""
    try:
        shown = show.rebuild(
            args.run_id, state=state_dir(io.environ, home=io.home), runs_root=runs_root(io.home)
        )
    except show.NotShown as exc:
        raise UsageError(str(exc)) from None
    for note in shown.notes:
        io.say(note)
    if args.json:
        io.stdout.write(json.dumps(shown.report, ensure_ascii=False, indent=2) + "\n")
    else:
        io.stdout.write(show.render(shown))
    return 1 if shown.unknown else 0
```

  In `packages/headless-agents/README.md`, add `ha show RUN_ID [--json]` to the synopsis
  block, after `ha runs [--limit N] [--json]`.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/unit/headless_agents -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/headless-agents/src/headless_agents/show.py packages/headless-agents/src/headless_agents/cli.py \
  packages/headless-agents/README.md tests/unit/headless_agents/test_show.py tests/unit/headless_agents/test_cli.py
git commit -m "feat(headless-agents): ha show RUN_ID renders a run rebuilt from the state"
```

### Task 8: `quarantine.active` and the complete `ha runs`

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/quarantine.py` (`active`)
- Modify: `packages/headless-agents/src/headless_agents/cli.py` (`_registered_row`, `_runs`)
- Modify: `tests/unit/headless_agents/test_quarantine.py`, `tests/unit/headless_agents/test_cli.py`

**Interfaces:**
- Consumes: `show.read_task`, `show.format_duration`, `show.format_cost` (Tasks 6–7).
- Produces: `quarantine.active(state: Path) -> list[dict[str, object]]` — one entry per
  file of `<state>/quarantine/`, the operator's first then by name:
  `{"file", "readable", "scope", "reason", "run_id"}`; an unreadable file gives
  `readable: False`, `scope`/`run_id` `None`, and the `Unknown` message as `reason`.
  Rows of `ha runs --json` gain `task`, `cost_usd`, `cost_complete` (P7).

- [ ] **Step 1: Write the failing tests.** In `test_quarantine.py`:

```python
def test_active_lists_every_quarantine_the_operators_first(tmp_path: Path) -> None:
    state = tmp_path / "state"
    common = tmp_path / "repo" / ".git"
    common.mkdir(parents=True)
    quarantine.publish(state, "repository", reason="tripwire", run_id="r1", paths=["x"], common_dir=common)
    quarantine.publish(state, "operator", reason="stale unconfined intent", run_id="r2", paths=[], common_dir=None)
    found = quarantine.active(state)
    assert [(q["scope"], q["run_id"], q["readable"]) for q in found] == [
        ("operator", "r2", True),
        ("repository", "r1", True),
    ]


def test_active_lists_an_unreadable_quarantine(tmp_path: Path) -> None:
    state = tmp_path / "state"
    (state / "quarantine").mkdir(parents=True)
    (state / "quarantine" / "operator.json").write_text("{not json")
    (entry,) = quarantine.active(state)
    assert entry["readable"] is False and "does not parse" in str(entry["reason"])


def test_active_without_a_quarantine_is_empty(tmp_path: Path) -> None:
    assert quarantine.active(tmp_path / "state") == []
```

  In `test_cli.py` (with `from headless_agents import quarantine` in the imports):

```python
def test_runs_rows_carry_the_task_and_the_cost(world: _World) -> None:
    world.run("run", "codex", "Explain the layout.\nDetails.")
    code, out, _ = world.run("runs", "--json")
    (row,) = json.loads(out)
    assert row["task"] == "Explain the layout."
    assert {"cost_usd", "cost_complete", "duration_seconds", "text"} <= row.keys()


def test_runs_prints_duration_cost_and_the_task(world: _World) -> None:
    world.run("run", "codex", "Explain the layout.\nDetails.")
    code, out, _ = world.run("runs")
    (line,) = out.splitlines()
    assert code == 0 and "codex" in line and "answered" in line and "exit 0" in line
    assert line.endswith("  Explain the layout.")


def test_runs_cuts_a_long_task(world: _World) -> None:
    world.run("run", "codex", "x" * 80)
    code, out, _ = world.run("runs")
    assert out.rstrip("\n").endswith("x" * 59 + "…")


def test_runs_lists_a_legacy_runs_cost(world: _World) -> None:
    legacy = world.home / ".cache" / "ha" / "runs" / "20260920T000000-aaaaaaaa"
    legacy.mkdir(parents=True)
    (legacy / "result.json").write_text(
        json.dumps({"provider": "codex", "exit_code": 0, "duration_seconds": 30.0, "cost_usd": 0.12})
    )
    code, out, _ = world.run("runs", "--json")
    (row,) = json.loads(out)
    assert row["status"] == "legacy" and row["cost_usd"] == 0.12 and row["task"] is None


def test_runs_lists_active_quarantines_first(world: _World) -> None:
    world.run("run", "codex", "go")
    quarantine.publish(
        world.state, "operator", reason="tripwire", run_id="20260925T000000-aaaaaaaa",
        paths=["/x"], common_dir=None,
    )
    code, out, _ = world.run("runs")
    lines = out.splitlines()
    assert code == 0 and len(lines) == 2
    assert lines[0].startswith("QUARANTINE operator: tripwire in run 20260925T000000-aaaaaaaa")
    assert lines[0].endswith("lift it by hand after inspection")


def test_runs_json_stays_a_list_and_names_quarantines_on_stderr(world: _World) -> None:
    quarantine.publish(
        world.state, "operator", reason="tripwire", run_id="20260925T000000-aaaaaaaa",
        paths=[], common_dir=None,
    )
    code, out, err = world.run("runs", "--json")
    assert code == 0 and json.loads(out) == []
    assert "ha: quarantine operator: tripwire" in err


def test_runs_reads_a_write_run_its_lineage_does_not_list_as_unknown(world: _World) -> None:
    """Same rule as ha show (plan P6): a silent authority is no status."""
    from headless_agents import lineage as lineages

    run_id = "20260925T000000-eeeeeeee"
    world.registry().create(
        run_id, run_dir=None, target={"kind": "role", "name": "w"}, repository=None, lineage=run_id
    )
    lineages.create(
        world.state,
        lineages.LineageState(
            owner=run_id, repository=world.home, common_dir=world.home / ".git",
            worktree=world.home / "wt", branch=f"ha/{run_id}", base=None,
            members={}, pending=None, compromised=None,
        ),
    )
    code, out, _ = world.run("runs", "--json")
    (row,) = json.loads(out)
    assert row["status"] == "unknown"


def test_runs_survives_an_unreadable_quarantine_and_entry(world: _World) -> None:
    code, out, _ = world.run("run", "codex", "--json", "go")
    run_id = json.loads(out)["run_id"]
    (world.state / "runs" / f"{run_id}.json").write_text("{not json")
    (world.state / "quarantine").mkdir(parents=True, exist_ok=True)
    (world.state / "quarantine" / "operator.json").write_text("{not json")
    code, out, _ = world.run("runs")
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith("QUARANTINE unreadable:")
    assert lines[1].startswith(run_id) and "unknown" in lines[1]
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_quarantine.py tests/unit/headless_agents/test_cli.py -v -k "active or runs"`
Expected: FAIL — `AttributeError: module 'headless_agents.quarantine' has no attribute 'active'`,
and rows without `task`.

- [ ] **Step 3: Implement.** In `quarantine.py`, add (and export) `active`:

```python
def active(state: Path) -> list[dict[str, object]]:
    """Every quarantine in force, the operator's first (spec §3.8.5: at the top of ``ha runs``).

    A file present is a quarantine in force -- ``ha`` never lifts one, the
    operator deletes its file. An unreadable file is listed as such: :func:`check`
    still refuses on it.
    """
    directory = state / "quarantine"
    if not directory.is_dir():
        return []
    paths = sorted(directory.glob("*.json"), key=lambda path: (path.name != "operator.json", path.name))
    found: list[dict[str, object]] = []
    for path in paths:
        try:
            document = read(path)
        except Unknown as exc:
            found.append(
                {"file": str(path), "readable": False, "scope": None, "reason": str(exc), "run_id": None}
            )
            continue
        found.append(
            {
                "file": str(path),
                "readable": True,
                "scope": document.get("scope"),
                "reason": document.get("reason"),
                "run_id": document.get("run_id"),
            }
        )
    return found
```

  In `cli.py`: widen Task 7's import to `from . import quarantine, show`, import
  `format_cost`, `format_duration` and `read_task` from `.show`, and add
  `TASK_WIDTH: Final = 60`. In `_registered_row`, start the row with
  `"cost_usd": None, "cost_complete": None, "task": None` beside the existing keys, set
  `task=read_task(entry.run_dir)` with the entry's fields, and read
  `cost_usd=report.get("cost_usd"), cost_complete=report.get("cost_complete")` with the
  report's. Read a write run's member status as
  `lineages.load(registry.state, entry.lineage).members.get(run_id, "unknown")`: a readable
  lineage silent about the run gives `unknown`, as `ha show` does (P6), where lot 1 fell
  through to `running`/`incomplete`. Give the legacy row
  `"cost_usd": legacy.get("cost_usd")`,
  `"cost_complete": isinstance(legacy.get("cost_usd"), int | float)` and `"task": None`
  (a 0.4.0 run kept no task). Update `_runs`' docstring (the listing is complete now; P7)
  and replace its output with:

```python
    quarantines = quarantine.active(registry.state)
    if args.json:
        for entry in quarantines:
            io.say(_quarantine_line(entry).replace("QUARANTINE", "quarantine", 1))
        io.stdout.write(json.dumps(rows, indent=2) + "\n")
        return 0
    for entry in quarantines:
        io.stdout.write(_quarantine_line(entry) + "\n")
    for row in rows:
        io.stdout.write(_runs_line(row) + "\n")
    return 0
```

  with, above `_runs`:

```python
def _quarantine_line(entry: Mapping[str, object]) -> str:
    """One quarantine in force (spec §3.8.5); an unreadable one still refuses."""
    if entry.get("readable"):
        return (
            f"QUARANTINE {entry.get('scope')}: {entry.get('reason')} in run {entry.get('run_id')} "
            f"({entry.get('file')}); lift it by hand after inspection"
        )
    return f"QUARANTINE unreadable: {entry.get('reason')}; lift it by hand after inspection"


def _runs_line(row: Mapping[str, object]) -> str:
    """run id, target, status, exit code, duration, cost, first line of the task (§3.9)."""
    task = row.get("task")
    shown = task if isinstance(task, str) else "-"
    if len(shown) > TASK_WIDTH:
        shown = shown[: TASK_WIDTH - 1] + "…"
    exit_code = "-" if row.get("exit_code") is None else str(row.get("exit_code"))
    return (
        f"{row.get('run_id')}  {row.get('target') or '-'!s:<16} {row.get('status')!s:<10} "
        f"exit {exit_code:<4} {format_duration(row.get('duration_seconds')):>6}  "
        f"{format_cost(row.get('cost_usd')):>6}  {shown}"
    ).rstrip()
```

- [ ] **Step 4: Run to verify they pass**, then the whole unit suite and the gates:

Run: `.venv/bin/pytest tests/unit/ -q && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy src/ packages/headless-agents/src/`
Expected: PASS; ruff and mypy clean.

- [ ] **Step 5: Commit**

```bash
git add packages/headless-agents/src/headless_agents/quarantine.py packages/headless-agents/src/headless_agents/cli.py \
  tests/unit/headless_agents/test_quarantine.py tests/unit/headless_agents/test_cli.py
git commit -m "feat(headless-agents): complete ha runs — duration, cost, task, quarantines first"
```

---

## Spec coverage (self-review)

| Spec | Where |
|---|---|
| §3.9 `ha show RUN_ID [--json]` | Tasks 6, 7 |
| §3.9 `ha runs`: newest first; run id, target, exit code, duration, cost, first line of the task; a 0.4.0 run listed as `legacy` from its `result.json` | Task 8 (newest-first ordering and legacy detection unchanged from lot 1) |
| §3.8.5 "`ha runs` lists active quarantines at the top" | Task 8 |
| §3.8.1 one authority per fact; a `run.json` read only when its `run_id` matches; 0.4.0 runs accepted by no option | Task 6 (and `ha show` of a legacy id, Tasks 6–7) |
| §3.8.3 step 9 "only a stale report, rebuilt from the state by `ha show`" | Task 6 |
| §3.8.6 `ha show RUN_ID` renders a review's `vendor_check` from the state | lot 4, with the review result and its writer; until then `ha show` shows `null` and never takes it, or any field of `LATER_AUTHORITIES`, from `run.json` (Task 6, P6) |
| §3.10 `incomplete` derived from the free lifecycle lock, never written | Task 6 (`Registry.effective_status`) |
| §3.10 `ha show` rendering; §4 "against a golden text" | Task 7 |
| §3.11 codex / opencode / agy counters | Tasks 1, 2, 3 |
| §3.11 HTTP providers `{}` | Task 4 |
| §3.11 claude `null` until a live test proves the telemetry | Task 4 (P3); the measurement and its counter: lot 5 |
| §3.11 "the counts live in `run.json` only; a step's `result.json` stays schema 1" | Task 5 (`result.json` untouched) |
| §4 "the tool counters against recorded event logs, one per rail" | Tasks 1–3 (three recorded logs; claude's has none to record, P3) |

**Out of lot 2, deliberately:** `ha show --dir PATH`, the `vendors` line, the `review` and
`judge` rows, `cleanup` on the header, and reading a review's result from `<state>/reviews/`
into `ha show` — its `head`, `verdict`, deciding text and `vendor_check` (lot 4); workflows
and the continuation records behind `continues` (lot 3); the claude telemetry
measurement, its counter and its proof, the `live` suite, README and CHANGELOG, then the
tag (lot 5).
