"""SQL contract of auto-opening and of observing an `agent` tracer.

The harness compiles the SQLAlchemy statements without PostgreSQL: it proves the
SHAPE emitted, not the engine's behaviour. The real boundary — the **PARTIAL**
UNIQUE index that arbitrates the conflict — can only be proven against a real
database; what is proven here is that the code NAMES it, and that it does not
leave without stamping what it just found.

046 delivered five columns and a single writer. `last_observed_at` was the
writer-less column that mattered: it is the ONLY one the sweep's 4 h rule can
read, so leaving it NULL made M-G green and inert.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from tests.unit.repositories.test_pg_brain_session import (
    _is_update,
    _make_session,
    _params,
    _result,
    _sql,
)

NOW = datetime(2026, 8, 22, 9, 30, tzinfo=UTC)
_CONNECTION = "3f2b1a0c9d8e7f6a5b4c3d2e1f0a9b8c"


@dataclass(frozen=True)
class _Identity:
    """Minimal mirror of `AutoOpenIdentity` — the repository knows only these fields."""

    project_key: str = "brain-v42"
    connection_id: str = _CONNECTION
    started_by_actor: str = "brain_v42"
    nature: str = "agent"
    intent: str | None = None


def _open_router(*, session_id: Any, focus: dict[str, Any] | None):
    def route(statement: Any) -> Any:
        if "from project_contexts" in _sql(statement):
            return _result(row=focus)
        return _result(scalar=session_id)

    return route


async def _auto_open(router: Any, identity: _Identity | None = None) -> tuple[Any, list[Any]]:
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    _, statements, _, factory = _make_session(router)
    opened = await PgBrainSessionRepo(factory).auto_open(identity or _Identity(), now=NOW)
    return opened, statements


class TestAutoOpen:
    async def test_a_single_upsert_carries_the_five_046_columns(self) -> None:
        session_id = uuid4()
        opened, statements = await _auto_open(
            _open_router(session_id=session_id, focus={"current_focus": "f", "focus_revision": 7})
        )

        assert opened == session_id
        insert = statements[-1]
        params = _params(insert)
        assert params["nature"] == "agent"
        assert params["connection_id"] == _CONNECTION
        assert params["started_by_actor"] == "brain_v42"
        # `intent` is the human JUDGEMENT field: the server manufactures none.
        assert params["intent"] is None
        assert params["last_observed_at"] == NOW

    async def test_the_conflict_is_inferred_on_the_partial_index_and_dates_the_row(self) -> None:
        """`DO UPDATE`, not `DO NOTHING`: a conflict IS an observation.

        And `WHERE status = 'open'` must appear in the inference. Without the
        predicate, PostgreSQL cannot designate the partial index and raises —
        that is the difference between a path that reopens after a nightly
        closure and a path that fails every morning.
        """
        sql = _sql(
            (
                await _auto_open(
                    _open_router(
                        session_id=uuid4(),
                        focus={"current_focus": "f", "focus_revision": 7},
                    )
                )
            )[1][-1]
        )
        assert "on conflict (project_key, connection_id) where status = 'open' do update" in sql
        assert "last_observed_at" in sql.split("do update")[1]
        assert "returning" in sql

    async def test_retrieving_an_existing_session_costs_no_second_round_trip(self) -> None:
        """WITNESS: exactly two statements — the focus, then the upsert.

        The old form followed the `DO NOTHING` with a `SELECT` to find the id:
        two round trips that stamped nothing. Counting the statements is the only
        way to prove that `SELECT` really is gone.
        """
        _, statements = await _auto_open(
            _open_router(session_id=uuid4(), focus={"current_focus": "f", "focus_revision": 7})
        )
        assert len(statements) == 2
        assert "from brain_sessions" not in _sql(statements[0])

    async def test_a_conflict_on_an_operator_row_dates_nothing_and_returns_none(self) -> None:
        """The conflict must NEVER be able to stamp a non-`agent` row.

        `observe()` already carries this guard (a HARD `nature = 'agent'` in its
        WHERE). The CONFLICT path did not: a `DO UPDATE` without a guard would
        re-stamp `last_heartbeat_at` on an `operator` row at every tool call. But
        the sweep's 7-day eligibility reads `last_heartbeat_at` **with no nature
        filter** — the one WRITTEN exception to the covenant would therefore
        become unreachable, and the row an immortal ghost.

        Two halves, and both are needed: the SHAPE emitted must name the guard,
        and PostgreSQL's refusal (no row returned) must translate into `None`
        with no fallback stamping anything behind it.
        """
        opened, statements = await _auto_open(
            _open_router(session_id=None, focus={"current_focus": "f", "focus_revision": 7})
        )
        assert opened is None, "un conflit refusé ne rend pas d'identifiant"
        assert len(statements) == 2, "aucun statement de rattrapage après un refus"

        action = _sql(statements[-1]).split("do update")[1]
        assert " where " in action, "le DO UPDATE ne porte aucune garde de nature"
        guard = action.split(" where ")[1]
        assert "nature" in guard
        assert "agent" in _params(statements[-1]).values()

    async def test_a_project_without_context_opens_nothing_and_writes_nothing(self) -> None:
        """The server manufactures no project: nobody named anything here."""
        opened, statements = await _auto_open(_open_router(session_id=uuid4(), focus=None))
        assert opened is None
        assert len(statements) == 1


class TestObserve:
    async def _observe(self, *, found: Any) -> tuple[bool, list[Any]]:
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        _, statements, _, factory = _make_session(lambda _stmt: _result(scalar=found))
        alive = await PgBrainSessionRepo(factory).observe(uuid4(), now=NOW)
        return alive, statements

    async def test_observation_moves_both_clocks_and_leaves_updated_at_alone(self) -> None:
        """Two clocks move, the third does not — and each for its own reason.

        `last_observed_at` feeds the 4 h rule. `last_heartbeat_at` avoids the 7 d
        sweep's false corpse on a connection that lives more than a week without
        any human sending a heartbeat. `updated_at` does NOT move: observing is
        not mutating the declared state, and turning it into an activity signal
        would hollow out every control that leans on it.
        """
        alive, statements = await self._observe(found=uuid4())

        assert alive is True
        assert len(statements) == 1
        statement = statements[0]
        assert _is_update(statement, "brain_sessions")
        params = _params(statement)
        assert params["last_observed_at"] == NOW
        assert params["last_heartbeat_at"] == NOW
        assert "updated_at" not in _sql(statement).split("where")[0]

    async def test_the_predicate_can_never_reach_an_operator_session(self) -> None:
        """A HARD guard, not a redundancy: a poisoned memo must stamp nothing.

        `nature = 'agent'` and `status = 'open'` are in the WHERE, so an
        `operator` session — or an already terminal one — is out of this path's
        reach, whatever UUID it is handed.
        """
        _, statements = await self._observe(found=uuid4())
        where = _sql(statements[0]).split(" where ")[1]
        assert "brain_sessions.status = " in where
        assert "brain_sessions.nature = " in where
        params = _params(statements[0])
        assert params["status_1"] == "open"
        assert params["nature_1"] == "agent"

    async def test_no_row_means_closed_under_us_not_an_error(self) -> None:
        """`False` is a FACT — the session was closed — not a failure.

        That boolean is what makes the opener discard the memo and reopen.
        Confusing it with an error would lose this connection's session.
        """
        alive, _ = await self._observe(found=None)
        assert alive is False


@pytest.mark.parametrize("column", ["last_observed_at", "last_heartbeat_at"])
async def test_both_writers_move_exactly_the_same_clocks(column: str) -> None:
    """The upsert and the observation must stamp the SAME set of columns.

    Letting them diverge would give a re-identified connection a presence clock
    different from a re-observed one: two regimes for a single gesture, and a
    sweep that would read one or the other depending on which path happened to be
    taken.
    """
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    _, statements = await _auto_open(
        _open_router(session_id=uuid4(), focus={"current_focus": "f", "focus_revision": 7})
    )
    upsert_set = _sql(statements[-1]).split("do update set")[1]

    _, observe_statements, _, factory = _make_session(lambda _s: _result(scalar=uuid4()))
    await PgBrainSessionRepo(factory).observe(uuid4(), now=NOW)
    observe_set = _sql(observe_statements[0]).split(" set ")[1].split(" where ")[0]

    assert column in upsert_set
    assert column in observe_set
