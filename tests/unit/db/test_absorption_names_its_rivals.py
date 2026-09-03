"""The window stage says WHO contested, not just how many rows it refused.

Ticket dfaed283. `AbsorptionOutcome.rivals` used to be a bare COUNT OF
ARTIFACTS, and the only place it surfaced was the log. A client reading
`attributed_knowledge_ids: []` could not tell an honest abstention from a dead
path — the failure mode this project keeps reproducing: a capability armed,
green, and silent where it fails.

Two things are pinned here, and the second is a COST guard, not a behaviour:
naming the rivals costs one extra `SELECT`, so that `SELECT` must not run on the
nominal path where nothing is contested.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from brain_v42.provenance import set_current_transport
from tests.unit.repositories.test_pg_brain_session import _result, _sql

_CONNECTION = "1a2b3c4d5e6f70818283848586878889"
_PROJECT = "brain-v42"
_STARTED_AT = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _connection() -> Any:
    set_current_transport(_CONNECTION)
    yield
    set_current_transport(None)


@pytest.fixture
def _open_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    import brain_v42.db.session_derived_capture as module

    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: MagicMock(brain_session_derived_capture_enabled=True),
    )


@dataclass(frozen=True)
class _Target:
    id: UUID
    project_key: str = _PROJECT
    started_at: datetime = _STARTED_AT


def _session(router: Any) -> tuple[Any, list[Any]]:
    statements: list[Any] = []

    async def execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
        statements.append(statement)
        return router(statement)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=execute)
    savepoint = MagicMock()
    savepoint.__aenter__ = AsyncMock(return_value=session)
    savepoint.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=savepoint)
    return session, statements


def _router(*, contested: int, held: int, rival_ids: list[UUID]) -> Any:
    """Route by SHAPE, never by call order.

    An order-indexed double would freeze the number of queries the
    implementation is allowed to emit, which is not the contract under test.
    """

    def route(statement: Any) -> Any:
        sql = _sql(statement)
        if "count(" in sql:
            # The ledger-occupancy count is the only one that does not join the
            # eligible rows; the two window counters do.
            if "join" not in sql:
                return _result(scalar=0)
            return _result(scalar=contested if "exists" in sql else held)
        if "select distinct" in sql and "brain_sessions" in sql:
            return _result(rows=[{"id": item} for item in rival_ids])
        if "brain_sessions" in sql and "update" not in sql:
            return _result(scalar=None)  # no tracer on this connection
        return _result(rows=[])  # no uncontested candidate to move

    return route


async def _absorb(session: Any, target: _Target) -> Any:
    from brain_v42.db.session_derived_capture import absorb_tracer_ledger

    return await absorb_tracer_ledger(session, target, _CONNECTION)


class TestItNamesTheRivals:
    async def test_the_outcome_carries_the_rival_SESSIONS_not_only_a_count(
        self, _open_flag: None
    ) -> None:
        """The ticket's real case: `rival fantôme 8a7eb7e9` is a SESSION id.

        Counting refused artifacts answers "how much"; the operator undoing a
        bad abstention needs "who".
        """
        rival = uuid4()
        session, _ = _session(_router(contested=3, held=3, rival_ids=[rival]))

        outcome = await _absorb(session, _Target(id=uuid4()))

        assert outcome.reason == "ambiguous"
        assert outcome.rival_sessions == (rival,)
        assert outcome.rival_artifacts == 3

    async def test_it_counts_what_the_tracers_still_hold(self, _open_flag: None) -> None:
        """`held_by_tracers` is measured BEFORE the move, or it counts nothing.

        The update reassigns the rows to a non-`agent` session, which drops them
        out of `_window_donors`. A count taken afterwards would report only the
        refused ones and call it "held".
        """
        session, _ = _session(_router(contested=1, held=4, rival_ids=[uuid4()]))

        outcome = await _absorb(session, _Target(id=uuid4()))

        assert outcome.held_by_tracers == 4

    async def test_it_does_NOT_query_the_rivals_when_nothing_is_contested(
        self, _open_flag: None
    ) -> None:
        """Cost guard: the nominal path must not pay for the diagnosis.

        Naming the rivals is one extra `SELECT DISTINCT` over a range join. It
        is worth paying exactly when there is something to explain.
        """
        session, statements = _session(_router(contested=0, held=0, rival_ids=[]))

        outcome = await _absorb(session, _Target(id=uuid4()))

        assert outcome.rival_sessions == ()
        assert not [
            statement
            for statement in statements
            if "select distinct" in _sql(statement) and "brain_sessions" in _sql(statement)
        ], "aucune requête de nommage ne doit partir quand rien n'est contesté"
