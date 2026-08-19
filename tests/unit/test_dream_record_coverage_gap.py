"""La ligne `coverage` : le verdict porté jusqu'à un lecteur qui existe.

Ticket `0a9c067e`. Sa leçon centrale est qu'une alerte que personne ne lit est
indiscernable d'une alerte absente — le comparateur avait raison trois nuits de
suite et rien ne s'est passé. T1 (code retour + ligne journald) n'atteint que le
contrôle du matin ; cette ligne-ci atteint DEUX lecteurs existants sans une
ligne de code chez eux :

- `DreamRunService.last_failure` → section « ### Last failure » du briefing de
  session, lue à chaque ouverture ;
- `collect_nightly_ops` → `/metrics` `nightly.last_failure`.

Prix assumé et dit : étant la plus récente, elle prend la place « Last failure »
d'un échec de phase de la même nuit, et `/metrics` `last_run.status` passe à
`partial` ces nuits-là — ce qui est vrai.

Le writer est un calque de `_promote_helpers._record_empty_pool` : best-effort,
n'élève JAMAIS, pose la sentinelle sans jamais la canonicaliser. « Best-effort »
ne veut pas dire « rend toujours 0 » : son code retour est ce qui rend son propre
échec observable, exactement comme `record-empty-pool`.
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


class _RecordingSession:
    """Session asynchrone qui n'exécute rien et retient ce qu'on lui donne."""

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


# --- La ligne, et ce qu'elle porte ------------------------------------------


@pytest.mark.asyncio
async def test_the_coverage_row_carries_the_global_sentinel() -> None:
    """`coverage` n'a pas de projet : c'est un verdict sur la NUIT entière.

    La sentinelle ne transite jamais par `canonicalize_project_key`, qui la
    rejette — sur un writer best-effort l'exception serait avalée et la colonne
    resterait NULL, en silence, chaque nuit.
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
    """Lu dans les métadonnées réelles, pas recopié : `varchar(10)`, 8 caractères."""
    length = dream_runs.c.phase.type.length

    assert length is not None
    assert len(record_coverage_gap.COVERAGE_PHASE) <= length


def test_the_message_is_bounded() -> None:
    message = record_coverage_gap.build_error_message("x" * 50_000, "y" * 50_000)

    assert len(message) <= record_coverage_gap.MAX_ERROR_MESSAGE_CHARS


def test_an_empty_verdict_still_says_something() -> None:
    message = record_coverage_gap.build_error_message("", "")

    assert message.strip() != ""


@pytest.mark.asyncio
async def test_a_second_run_the_same_night_updates_instead_of_duplicating() -> None:
    """Un rejeu manuel du matin ne doit pas empiler des verdicts contradictoires."""
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


# --- Best-effort : n'élève jamais, mais RAPPORTE ----------------------------


def test_a_dead_database_returns_non_zero_without_raising(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """« Best-effort — n'élève jamais » n'est PAS « rend toujours 0 ».

    C'est ce code retour qui rend l'échec du writer observable, exactement comme
    celui de `record-empty-pool` rend observable la classe D.
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
