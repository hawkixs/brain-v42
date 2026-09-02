"""The `coverage` row: the verdict carried to a reader that exists.

Ticket `0a9c067e`. Its central lesson is that an alert nobody reads is
indistinguishable from an absent alert — the comparator was right three nights in
a row and nothing happened. T1 (return code + journald line) only reaches the
morning check; this row reaches TWO existing readers without a line of code on
their side:

- `DreamRunService.last_failure` → the "### Last failure" section of the session
  briefing, read at every opening;
- `collect_nightly_ops` → `/metrics` `nightly.last_failure`.

An accepted and stated price: being the most recent, it takes the "Last failure"
slot from a phase failure of the same night, and `/metrics` `last_run.status`
turns `partial` on those nights — which is true.

The writer is a tracing of `_promote_helpers._record_empty_pool`: best-effort,
NEVER raises, lays down the sentinel without ever canonicalising it.
"Best-effort" does not mean "always returns 0": its return code is what makes its
own failure observable, exactly like `record-empty-pool`.
"""

from __future__ import annotations

import datetime as dt
import inspect
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from scripts.dream import record_coverage_gap

from brain_v42.db.tables import dream_runs
from brain_v42.dream_run_project_key import GLOBAL_PHASE_PROJECT_KEY

RUN_DATE = dt.date(2026, 8, 18)
SUMMARY = "COVERAGE mode=manifest expected=63 written=3 skipped=0 declared=0 writefail=0 silent=60 extra=0"
DETAIL = "COVERAGE_SILENT silent=p0/clean, p0/connect and 58 more"

_INSERT = re.compile(r"INSERT\s+INTO\s+dream_runs", re.IGNORECASE)
_UPDATE = re.compile(r"UPDATE\s+dream_runs", re.IGNORECASE)
_SELECT = re.compile(r"SELECT\s+dream_runs", re.IGNORECASE)


class _RecordingSession:
    """An async session that executes nothing and remembers what it is given."""

    def __init__(self, calls: list[Any], existing_id: int | None) -> None:
        self._calls = calls
        self._existing_id = existing_id
        self.committed = 0

    async def execute(self, statement: Any, params: Any = None) -> Any:
        self._calls.append(statement)
        return SimpleNamespace(
            scalar_one_or_none=lambda: self._existing_id,
            scalar_one=lambda: self._existing_id,
        )

    async def commit(self) -> None:
        self.committed += 1

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False


def _factory(existing_id: int | None = None) -> tuple[Any, list[Any], _RecordingSession]:
    calls: list[Any] = []
    session = _RecordingSession(calls, existing_id)
    return (lambda: session), calls, session


def _mutation(calls: list[Any]) -> tuple[str, dict[str, Any]]:
    mutations = [call for call in calls if _INSERT.search(str(call)) or _UPDATE.search(str(call))]
    assert len(mutations) == 1, f"attendu 1 écriture dream_runs, vu {len(mutations)}"
    statement = mutations[0]
    kind = "insert" if _INSERT.search(str(statement)) else "update"
    return kind, dict(statement.compile().params)


# --- The row, and what it carries -------------------------------------------


@pytest.mark.asyncio
async def test_the_coverage_row_carries_the_global_sentinel() -> None:
    """`coverage` has no project: it is a verdict on the WHOLE night.

    The sentinel never transits through `canonicalize_project_key`, which rejects
    it — on a best-effort writer the exception would be swallowed and the column
    would stay NULL, silently, every night.
    """
    factory, calls, session = _factory()

    await record_coverage_gap.record_coverage_gap(factory, RUN_DATE, summary=SUMMARY, detail=DETAIL)

    kind, params = _mutation(calls)
    assert kind == "insert"
    assert params["project_key"] == GLOBAL_PHASE_PROJECT_KEY
    assert params["phase"] == record_coverage_gap.COVERAGE_PHASE
    assert params["status"] == record_coverage_gap.COVERAGE_STATUS
    assert params["model"] is None
    assert params["phase_dry_run"] is False
    assert params["run_date"] == RUN_DATE
    assert session.committed == 1


@pytest.mark.asyncio
async def test_the_row_carries_the_verdict_and_the_faulty_pairs() -> None:
    factory, calls, _ = _factory()

    await record_coverage_gap.record_coverage_gap(factory, RUN_DATE, summary=SUMMARY, detail=DETAIL)

    _, params = _mutation(calls)
    assert SUMMARY in params["error_message"]
    assert DETAIL in params["error_message"]


def test_the_phase_name_fits_the_real_column() -> None:
    """Read from the real metadata, not retyped: `varchar(10)`, 8 characters."""
    length = dream_runs.c.phase.type.length

    assert length is not None
    assert len(record_coverage_gap.COVERAGE_PHASE) <= length


def test_the_message_is_bounded() -> None:
    message = record_coverage_gap.build_error_message("x" * 50_000, "y" * 50_000)

    assert len(message) <= record_coverage_gap.MAX_ERROR_MESSAGE_CHARS


def test_an_empty_verdict_still_says_something() -> None:
    message = record_coverage_gap.build_error_message("", "")

    assert message.strip() != ""


def _lookup(calls: list[Any]) -> Any:
    selects = [call for call in calls if _SELECT.search(str(call))]
    assert len(selects) == 1, f"attendu 1 recherche, vue {len(selects)}"
    return selects[0]


@pytest.mark.asyncio
async def test_the_idempotence_key_is_the_NIGHT_not_the_phase_alone() -> None:
    """The WHERE clause, read in the rendered SQL — not in a stub's answer.

    `_RecordingSession` answers the same thing to ANY query: the replay test
    therefore passes with or without the `run_date` filter, mutation executed. Yet
    without that filter, `order_by(id desc) limit 1` would find the `coverage` row
    of any past night: the writer would update YESTERDAY's verdict and would never
    write today's — the briefing and /metrics, this row's only readers, would see
    nothing move.
    """
    factory, calls, _ = _factory(existing_id=None)

    await record_coverage_gap.record_coverage_gap(factory, RUN_DATE, summary=SUMMARY)

    rendered = str(_lookup(calls).compile(compile_kwargs={"literal_binds": True}))
    assert f"dream_runs.run_date = '{RUN_DATE.isoformat()}'" in rendered
    assert f"dream_runs.phase = '{record_coverage_gap.COVERAGE_PHASE}'" in rendered


@pytest.mark.asyncio
async def test_the_lookup_stays_bounded_and_deterministic() -> None:
    """A night carrying two `coverage` rows must converge, not oscillate."""
    factory, calls, _ = _factory(existing_id=None)

    await record_coverage_gap.record_coverage_gap(factory, RUN_DATE, summary=SUMMARY)

    rendered = str(_lookup(calls).compile(compile_kwargs={"literal_binds": True}))
    assert "ORDER BY dream_runs.id DESC" in rendered
    assert "LIMIT 1" in rendered


@pytest.mark.asyncio
async def test_a_second_run_the_same_night_updates_instead_of_duplicating() -> None:
    """A manual morning replay must not stack contradictory verdicts."""
    factory, calls, _ = _factory(existing_id=4242)

    await record_coverage_gap.record_coverage_gap(factory, RUN_DATE, summary=SUMMARY, detail=DETAIL)

    kind, params = _mutation(calls)
    assert kind == "update"
    assert params["status"] == record_coverage_gap.COVERAGE_STATUS
    assert SUMMARY in params["error_message"]


def test_the_writer_never_canonicalizes_the_sentinel() -> None:
    source = inspect.getsource(record_coverage_gap)
    imports = [
        line for line in source.splitlines() if line.lstrip().startswith(("import ", "from "))
    ]

    assert not any("canonicalize_project_key" in line for line in imports)
    assert "canonicalize_project_key(" not in source
    assert "GLOBAL_PHASE_PROJECT_KEY" in source


# --- Best-effort: never raises, but REPORTS ---------------------------------


def test_a_dead_database_returns_non_zero_without_raising(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Best-effort — never raises is NOT the same as always returns 0.

    It is this return code that makes the writer's failure observable, exactly as
    `record-empty-pool`'s makes class D observable.
    """
    monkeypatch.setattr(
        record_coverage_gap,
        "record_coverage_gap",
        AsyncMock(side_effect=RuntimeError("base absente")),
    )
    monkeypatch.setattr(
        record_coverage_gap,
        "Settings",
        MagicMock(return_value=SimpleNamespace(postgres_url="postgresql+asyncpg://unused")),
    )
    monkeypatch.setattr(record_coverage_gap, "_build_factory", MagicMock())

    return_code = record_coverage_gap.main(
        ["--date", "2026-08-18", "--summary", SUMMARY, "--detail", DETAIL]
    )

    assert return_code != 0
    assert "base absente" in capsys.readouterr().err


def test_broken_settings_are_not_an_exception_either(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        record_coverage_gap, "Settings", MagicMock(side_effect=RuntimeError("no DSN"))
    )

    assert record_coverage_gap.main(["--date", "2026-08-18"]) != 0


def test_an_invalid_date_is_refused_before_touching_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = AsyncMock()
    monkeypatch.setattr(record_coverage_gap, "record_coverage_gap", recorder)
    monkeypatch.setattr(
        record_coverage_gap,
        "Settings",
        MagicMock(return_value=SimpleNamespace(postgres_url="postgresql+asyncpg://unused")),
    )

    assert record_coverage_gap.main(["--date", "pas-une-date"]) == 1
    recorder.assert_not_awaited()


def test_the_happy_path_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = AsyncMock()
    monkeypatch.setattr(record_coverage_gap, "record_coverage_gap", recorder)
    monkeypatch.setattr(
        record_coverage_gap,
        "Settings",
        MagicMock(return_value=SimpleNamespace(postgres_url="postgresql+asyncpg://unused")),
    )
    monkeypatch.setattr(record_coverage_gap, "_build_factory", MagicMock())

    assert record_coverage_gap.main(["--date", "2026-08-18", "--summary", SUMMARY]) == 0
    recorder.assert_awaited_once()


def test_module_is_executable_as_main() -> None:
    assert callable(getattr(record_coverage_gap, "main", None))
