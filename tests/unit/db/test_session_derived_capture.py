"""`derive_capture` contract: deposit in the tracer, never steal, never break.

The harness compiles the statements without PostgreSQL: it proves the SHAPE
emitted and the refusal paths, not the engine's arbitration. What can only be
proven in the database — the uniqueness of the tracer on (project, connection) —
is pinned on the e2e side.

Three properties govern this module and are read one by one below:

1. **It never STEALS.** The insert carries `ON CONFLICT DO NOTHING` on
   `knowledge_id`, which IS the ledger's primary key: an artifact already
   attributed — to an explicit session or to another tracer — stays where it is.
2. **It does not break the creation it observes.** Everything goes through a
   `begin_nested()`, and every `Exception` is swallowed. "Does not" and not
   "never": `except Exception` does not catch `BaseException`, and a
   `CancelledError` during the `ROLLBACK TO SAVEPOINT` stays outside the
   guarantee. A narrow window, but writing it down costs less than letting
   anyone believe in a total guarantee.
3. **Closed by default.** Flag closed ⇒ zero statements, not "a statement that
   does nothing".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

from brain_v42.provenance import set_current_transport
from tests.unit.repositories.test_pg_brain_session import _params, _result, _sql

_CONNECTION = "9a8b7c6d5e4f30211122334455667788"
_PROJECT = "brain-v42"


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


def _session(router: Any) -> tuple[Any, list[Any]]:
    """A fake session that can enter a savepoint, like the real one."""
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


def _router(*, tracer: UUID | None, ledger_size: int = 0, inserted: UUID | None = None) -> Any:
    def route(statement: Any) -> Any:
        sql = _sql(statement)
        if "from brain_sessions" in sql:
            return _result(scalar=tracer)
        if "count(" in sql:
            return _result(scalar=ledger_size)
        return _result(scalar=inserted)

    return route


async def _derive(
    session: Any,
    *,
    table: str = "learnings",
    project_key: str | None = _PROJECT,
    knowledge_id: UUID | None = None,
) -> UUID | None:
    from brain_v42.db.session_derived_capture import derive_capture

    return await derive_capture(
        session,
        table,
        {"id": knowledge_id or uuid4(), "project_key": project_key},
    )


class TestItDeposits:
    async def test_one_row_lands_in_the_tracer_of_this_connection(self, _open_flag: None) -> None:
        tracer, artifact = uuid4(), uuid4()
        session, statements = _session(_router(tracer=tracer, inserted=artifact))

        derived = await _derive(session, knowledge_id=artifact)

        assert derived == artifact
        insert = statements[-1]
        assert "insert into brain_session_artifacts" in _sql(insert)
        params = _params(insert)
        assert params["session_id"] == tracer
        assert params["knowledge_id"] == artifact
        assert params["knowledge_type"] == "learning"

    async def test_the_write_lives_in_a_savepoint(self, _open_flag: None) -> None:
        """Without a savepoint, an error here would take the creating transaction."""
        session, _ = _session(_router(tracer=uuid4(), inserted=uuid4()))
        await _derive(session)
        assert session.begin_nested.called

    async def test_the_tracer_is_looked_up_by_project_connection_open_and_agent(
        self, _open_flag: None
    ) -> None:
        """Four bounds, and none is decorative.

        Without `nature = 'agent'`, derivation could deposit into an `operator`
        session — that is, write into a human's session without them asking. That
        is exactly what absorption must remain alone in doing.
        """
        session, statements = _session(_router(tracer=uuid4(), inserted=uuid4()))
        await _derive(session)

        lookup = _sql(statements[0])
        assert "from brain_sessions" in lookup
        values = set(_params(statements[0]).values())
        assert {_PROJECT, _CONNECTION, "open", "agent"} <= values


class TestItRefuses:
    async def test_a_closed_flag_emits_no_statement_at_all(self) -> None:
        session, statements = _session(_router(tracer=uuid4()))
        assert await _derive(session) is None
        assert statements == []
        assert not session.begin_nested.called

    async def test_no_connection_means_nothing_to_derive_into(self, _open_flag: None) -> None:
        """stdio and stateless mode: no connection identifier, no key."""
        set_current_transport(None)
        session, statements = _session(_router(tracer=uuid4()))
        assert await _derive(session) is None
        assert statements == []

    async def test_no_tracer_writes_nothing(self, _open_flag: None) -> None:
        session, statements = _session(_router(tracer=None))
        assert await _derive(session) is None
        assert len(statements) == 1

    async def test_a_table_outside_the_capture_set_is_ignored(self, _open_flag: None) -> None:
        session, statements = _session(_router(tracer=uuid4()))
        assert await _derive(session, table="features") is None
        assert statements == []

    async def test_a_row_without_a_project_is_ignored(self, _open_flag: None) -> None:
        session, statements = _session(_router(tracer=uuid4()))
        assert await _derive(session, project_key=None) is None
        assert statements == []

    async def test_a_full_ledger_is_left_exactly_as_it_is(self, _open_flag: None) -> None:
        """The 100 ceiling belongs to explicit capture: we do not cross it.

        Exceeding it through the derived path would make `brain_session_capture`
        refusable for a reason the user did not cause.
        """
        from brain_v42.models.brain_session import MAX_CAPTURED_KNOWLEDGE_IDS

        session, statements = _session(
            _router(tracer=uuid4(), ledger_size=MAX_CAPTURED_KNOWLEDGE_IDS)
        )
        assert await _derive(session) is None
        assert not any("insert into brain_session_artifacts" in _sql(s) for s in statements)

    async def test_an_already_attributed_artifact_is_never_stolen(self, _open_flag: None) -> None:
        """`ON CONFLICT DO NOTHING` on the PK: the existing row wins, always."""
        session, statements = _session(_router(tracer=uuid4(), inserted=None))
        assert await _derive(session) is None
        insert = _sql(statements[-1])
        assert "on conflict (knowledge_id) do nothing" in insert

    async def test_any_failure_is_swallowed_and_never_reaches_the_creation(
        self, _open_flag: None
    ) -> None:
        """A derived capture that broke a `brain_learn` would be worse than nothing."""

        def explode(_statement: Any) -> Any:
            raise RuntimeError("la base a hoqueté")

        session, _ = _session(explode)
        assert await _derive(session) is None


async def test_the_table_map_agrees_with_the_repository_capture_tables() -> None:
    """Anti-drift, without an import cycle.

    `pg_brain_session` imports `pg_base`, and `pg_base` will call this module:
    importing `CAPTURE_TABLES` into it would close the cycle. The two lists
    therefore live separately, and this test is what keeps them from diverging in
    silence.
    """
    from brain_v42.db.session_derived_capture import CAPTURE_TABLES as derived
    from brain_v42.repositories.pg_brain_session import CAPTURE_TABLES as canonical

    assert derived == {table.name: knowledge_type for table, knowledge_type in canonical}


# ---------------------------------------------------------------------------
# absorb_tracer_ledger
# ---------------------------------------------------------------------------

_STARTED_AT = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
_CLIENT_KEY = "task-w20"


def _session_row(session_id: UUID) -> dict[str, Any]:
    """A row complete enough for `_to_model` to validate — the guard reads it."""
    return {
        "id": session_id,
        "project_key": _PROJECT,
        "client_key": _CLIENT_KEY,
        "status": "open",
        "started_focus": None,
        "started_focus_revision": 0,
        "summary": None,
        "next_focus": None,
        "captured_knowledge_ids": [],
        "nothing_to_capture_reason": None,
        "abandonment_reason": None,
        "started_at": _STARTED_AT,
        "ended_at": None,
        "updated_at": _STARTED_AT,
        "last_heartbeat_at": _STARTED_AT,
        "end_expected_focus_revision": None,
        "focus_outcome": None,
        "focus_at_end": None,
        "focus_revision_at_end": None,
        "nature": None,
    }


@dataclass(frozen=True)
class _Target:
    """Minimal mirror of `BrainSession` — absorption reads only these three fields."""

    id: UUID
    project_key: str = _PROJECT
    started_at: datetime = _STARTED_AT


def _absorb_router(
    *, tracer: UUID | None, occupied: int = 0, candidates: list[UUID] | None = None
) -> Any:
    moved = candidates if candidates is not None else []

    def route(statement: Any) -> Any:
        sql = _sql(statement)
        if "from brain_sessions" in sql:
            return _result(scalar=tracer)
        if "count(" in sql:
            return _result(scalar=occupied)
        return _result(rows=[{"knowledge_id": item} for item in moved])

    return route


async def _absorb(
    session: Any, target: _Target | None = None, connection: str = _CONNECTION
) -> int:
    """The TOTAL moved, across all stages.

    `absorb_tracer_ledger` now returns an `AbsorptionOutcome` — a bare total
    could not say by which key the match happened. This helper keeps the contract
    the tests below assert: the number of rows moved. The per-stage tests live in
    `TestTwoStageAbsorption`.
    """
    from brain_v42.db.session_derived_capture import absorb_tracer_ledger

    outcome = await absorb_tracer_ledger(session, target or _Target(id=uuid4()), connection)
    return outcome.total


class TestAbsorption:
    async def test_it_moves_the_tracer_rows_onto_the_target_and_counts_them(
        self, _open_flag: None
    ) -> None:
        target, moved = _Target(id=uuid4()), [uuid4(), uuid4()]
        session, statements = _session(_absorb_router(tracer=uuid4(), candidates=moved))

        assert await _absorb(session, target) == 2
        # Looked up by CONTENT, no longer by position: the window stage emits a
        # rival count after the UPDATE, and `statements[-1]` had come to mean
        # that count. The asserted property has not moved.
        update = next(item for item in statements if "update brain_session_artifacts" in _sql(item))
        assert target.id in _params(update).values()

    async def test_the_donor_can_only_be_an_open_agent_tracer(self, _open_flag: None) -> None:
        """The donor is `agent` ONLY: absorbing an `operator` session would move
        one human's ledger to another human."""
        session, statements = _session(_absorb_router(tracer=uuid4(), candidates=[uuid4()]))
        await _absorb(session)

        values = set(_params(statements[0]).values())
        assert {_PROJECT, _CONNECTION, "open", "agent"} <= values

    async def test_it_accepts_only_what_an_explicit_capture_would_have_accepted(
        self, _open_flag: None
    ) -> None:
        """The batch's INVARIANT: derivation is not a special dispensation.

        `_validate_captures` bounds an explicit capture to "same project AND
        `created_at >= started_at`", over six tables. Absorption must carry
        EXACTLY the same bounds: without that, it would attribute artifacts a
        user could not have captured themselves, and derivation would become a
        path more permissive than the command it replaces.
        """
        from brain_v42.db.session_derived_capture import CAPTURE_TABLES

        session, statements = _session(_absorb_router(tracer=uuid4(), candidates=[uuid4()]))
        await _absorb(session)

        # The bounds travel in the UPDATE's sub-select: a single statement, as
        # the settled shape required.
        update = _sql(statements[-1])
        for table_name in CAPTURE_TABLES:
            assert f"from {table_name}" in update, f"{table_name} hors du périmètre"
        assert update.count("created_at >=") == len(CAPTURE_TABLES)
        assert _STARTED_AT in _params(statements[-1]).values()

    async def test_it_never_pushes_the_target_ledger_past_the_cap(self, _open_flag: None) -> None:
        from brain_v42.models.brain_session import MAX_CAPTURED_KNOWLEDGE_IDS

        session, statements = _session(
            _absorb_router(tracer=uuid4(), occupied=98, candidates=[uuid4(), uuid4()])
        )
        await _absorb(session)

        update = statements[-1]
        assert "limit" in _sql(update)
        assert MAX_CAPTURED_KNOWLEDGE_IDS - 98 in _params(update).values()

    async def test_a_full_target_ledger_absorbs_nothing(self, _open_flag: None) -> None:
        from brain_v42.models.brain_session import MAX_CAPTURED_KNOWLEDGE_IDS

        session, statements = _session(
            _absorb_router(tracer=uuid4(), occupied=MAX_CAPTURED_KNOWLEDGE_IDS)
        )
        assert await _absorb(session) == 0
        assert not any("update brain_session_artifacts" in _sql(s) for s in statements)

    async def test_a_closed_flag_emits_no_statement_at_all(self) -> None:
        session, statements = _session(_absorb_router(tracer=uuid4()))
        assert await _absorb(session) == 0
        assert statements == []

    async def test_no_connection_absorbs_nothing(self, _open_flag: None) -> None:
        """stdio and stateless mode: without a connection identifier, no donor."""
        session, statements = _session(_absorb_router(tracer=uuid4()))
        assert await _absorb(session, connection="") == 0
        assert statements == []

    async def test_no_tracer_absorbs_nothing(self, _open_flag: None) -> None:
        """No tracer on THIS connection ⇒ the exact stage returns nothing.

        AMENDED with the two-stage batch. The previous form asserted
        `len(statements) == 1`, that is, "after the connection stage, we stop" —
        the very design this batch repairs, pinned by a counter. What remains
        asserted is the behaviour, which has not changed: nothing moves, and the
        EXACT stage is still evaluated FIRST.
        """
        session, statements = _session(_absorb_router(tracer=None))
        assert await _absorb(session) == 0
        assert "connection_id" in _sql(statements[0])

    async def test_any_failure_is_swallowed(self, _open_flag: None) -> None:
        def explode(_statement: Any) -> Any:
            raise RuntimeError("la base a hoqueté")

        session, _ = _session(explode)
        assert await _absorb(session) == 0


class TestRepositoryEntryPoint:
    """The repository entry point decides nothing — it finds and delegates."""

    async def _absorb_via_repo(
        self, monkeypatch: pytest.MonkeyPatch, *, row: dict[str, Any] | None
    ) -> tuple[int, list[Any]]:
        import brain_v42.db.session_derived_capture as module
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo
        from tests.unit.repositories.test_pg_brain_session import _make_session

        seen: list[Any] = []

        async def _fake(session: Any, target: Any, connection_id: str) -> Any:
            # The double MIRRORS the real callee's signature, otherwise it would
            # prove a contract nobody honours any more.
            from brain_v42.db.session_derived_capture import AbsorptionOutcome

            seen.append((target, connection_id))
            return AbsorptionOutcome(reason="absorbed", moved_by_connection=3)

        monkeypatch.setattr(module, "absorb_tracer_ledger", _fake)
        _, _statements, _, factory = _make_session(lambda _stmt: _result(row=row))
        moved = await PgBrainSessionRepo(factory).absorb_derived_capture(
            uuid4(), _CONNECTION, _CLIENT_KEY
        )
        return moved, seen

    async def test_it_delegates_with_the_target_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session_id = uuid4()
        moved, seen = await self._absorb_via_repo(
            monkeypatch,
            # `client_key` is now READ by the identity guard, which lives in the
            # same transaction as the mutation. A double row without it would no
            # longer describe the real path.
            row=_session_row(session_id),
        )

        assert moved == 3
        ((target, connection_id),) = seen
        assert (target.id, target.project_key, target.started_at) == (
            session_id,
            _PROJECT,
            _STARTED_AT,
        )
        assert connection_id == _CONNECTION

    async def test_an_unknown_session_absorbs_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        moved, seen = await self._absorb_via_repo(monkeypatch, row=None)
        assert moved == 0
        assert seen == []


# ---------------------------------------------------------------------------
# TWO-stage absorption — the exact stage, then the exclusivity window
# ---------------------------------------------------------------------------

_WINDOW_TRACER = uuid4()


#: Default donor of the window stage's candidates. Distinct from the
#: connection's tracer: that is what makes it possible to assert `donors` really
#: carries the INFERRED donor and not only the exact stage's.
_WINDOW_DONOR = uuid4()


def _tuple_result(rows: list[tuple[Any, ...]]) -> Any:
    """A result whose `.all()` returns TUPLES.

    `_result` builds its scalars by calling `.get()` on each row: it assumes
    mappings. The candidate selection returns `(knowledge_id, session_id)`, hence
    a distinct double — otherwise the harness would break on the shape instead of
    proving anything.
    """
    result = MagicMock()
    result.all.return_value = rows
    return result


def _two_stage_router(
    *,
    connection_tracer: UUID | None = None,
    occupied: int = 0,
    connection_moved: list[UUID] | None = None,
    window_moved: list[UUID] | None = None,
    window_donor: UUID | None = None,
    blocked: int = 0,
) -> Any:
    """A router that tells the TWO stages apart, which the total would mask."""
    donor = window_donor or _WINDOW_DONOR

    def route(statement: Any) -> Any:
        sql = _sql(statement)
        if "count(" in sql and "brain_session_artifacts" in sql and "update" not in sql:
            return _result(scalar=blocked if "rival" in sql else occupied)
        if sql.startswith("select brain_sessions.id"):
            return _result(scalar=connection_tracer)
        if "update brain_session_artifacts" in sql:
            # The stage is read from the MODE WRITTEN, taken from the
            # parameters. That is the semantic discriminant: the previous version
            # looked for the `rival` alias in the SQL, which disappeared from the
            # window UPDATE the day it stopped embedding its filter — the double
            # then silently served the wrong stage's rows.
            # A LIST and not a `set`: an `IN (...)` travels as a list of
            # parameters, which is not hashable. A `set()` raises here, and
            # absorption's `except Exception` would swallow the HARNESS's error
            # and pass it off as a legitimate refusal.
            values = list(_params(statement).values())
            moved = window_moved if "derived_window" in values else connection_moved
            return _result(rows=[{"knowledge_id": item} for item in (moved or [])])
        if sql.startswith("select brain_session_artifacts.knowledge_id"):
            return _tuple_result([(item, donor) for item in (window_moved or [])])
        return _result(rows=[])

    return route


def _window_sql(statements: list[Any]) -> str:
    """The SQL of the WINDOW stage statements, recognized by their `rival` alias."""
    return " ".join(_sql(item) for item in statements if "rival" in _sql(item))


def _window_values(statements: list[Any]) -> set[Any]:
    """The BOUND VALUES of the window stage.

    They are not in the compiled text: SQLAlchemy passes them as parameters.
    Looking for them in the SQL would turn these tests green for the wrong reason
    — they would never see anything.
    """
    values: set[Any] = set()
    for item in statements:
        if "rival" not in _sql(item):
            continue
        for value in _params(item).values():
            # An `IN (...)` travels as a LIST of parameters, not as that many
            # scalars: not flattening it would leave `closed_inactive` invisible
            # and the test green having seen nothing.
            values.update(value) if isinstance(value, list) else values.add(value)
    return values


async def _outcome(session: Any, target: _Target | None = None) -> Any:
    from brain_v42.db.session_derived_capture import absorb_tracer_ledger

    return await absorb_tracer_ledger(session, target or _Target(id=uuid4()), _CONNECTION)


class TestTwoStageAbsorption:
    """A total that masks a demotion from the exact stage to guesswork is a green
    that lies. Absorption must SAY by which key it matched."""

    async def test_it_counts_each_stage_separately(self, _open_flag: None) -> None:
        moved = [uuid4()]
        session, _ = _session(_two_stage_router(connection_tracer=uuid4(), connection_moved=moved))

        outcome = await _outcome(session)

        assert (outcome.moved_by_connection, outcome.moved_by_window) == (1, 0)
        assert outcome.total == 1
        assert outcome.reason == "absorbed"

    async def test_the_window_stage_is_reported_as_such(self, _open_flag: None) -> None:
        """The same total, a different key — and the report must say so."""
        moved = [uuid4()]
        session, _ = _session(_two_stage_router(connection_tracer=None, window_moved=moved))

        outcome = await _outcome(session)

        assert (outcome.moved_by_connection, outcome.moved_by_window) == (0, 1)
        assert outcome.total == 1

    async def test_the_connection_stage_still_demands_an_open_tracer(
        self, _open_flag: None
    ) -> None:
        """The EXACT stage does not change: it stays bounded to the current connection."""
        session, statements = _session(
            _two_stage_router(connection_tracer=uuid4(), connection_moved=[uuid4()])
        )
        await _outcome(session)

        exact = _sql(statements[0])
        assert "connection_id" in exact
        assert "status" in exact and "nature" in exact

    async def test_the_window_stage_accepts_a_closed_inactive_donor(self, _open_flag: None) -> None:
        """The 4 h sweep moves a tracer out of `open` WHILE KEEPING its ledger.

        A fix bounded to `'open'` would go silent again the day the placement of
        the `BRAIN_SESSION_INACTIVE_SWEEP_ENABLED` drop-in is corrected — without
        a sound, because nothing would fail.
        """
        session, statements = _session(_two_stage_router(window_moved=[uuid4()]))
        await _outcome(session)

        values = _window_values(statements)
        assert "closed_inactive" in values, "l'étage fenêtre ignore les traçantes balayées"
        assert "open" in values

    async def test_the_window_stage_never_absorbs_a_system_actor(self, _open_flag: None) -> None:
        """The dream is not an unknown creator: it is identified.

        Leaving it in the common pool would make the failure mode daily instead
        of marginal — the 03:00 `promote` falls inside the window of every
        session open that night.
        """
        session, statements = _session(_two_stage_router(window_moved=[uuid4()]))
        await _outcome(session)

        assert "started_by_actor" in _window_sql(statements), (
            "aucun filtre d'acteur sur l'étage fenêtre"
        )
        assert any(
            isinstance(value, str) and value.startswith("dream-")
            for value in _window_values(statements)
        ), "le préfixe des acteurs système n'est pas exclu"

    async def test_the_window_stage_refuses_what_a_rival_session_covers(
        self, _open_flag: None
    ) -> None:
        """Rivalry is SYMMETRIC: no recency clause.

        Two claimants mean an abstention. Coverage is judged at the creation
        instant, `started_at <= t <= coalesce(ended_at, now())`, so a session
        closed AFTER the instant is still a rival.
        """
        session, statements = _session(_two_stage_router(window_moved=[uuid4()]))
        await _outcome(session)

        window = _window_sql(statements)
        assert "not (exists" in window or "not exists" in window
        assert "ended_at" in window and "started_at" in window


class TestTheThreeZerosAreDistinguishable:
    """A legitimate refusal and "nothing to absorb" returned the same `0`.

    That is this project's running theme: a capability armed, green, and silent
    where it fails. Three `0` returns indistinguishable in the API as in the log
    are the next invisible regression.
    """

    async def test_a_closed_flag_says_so(self) -> None:
        session, statements = _session(_two_stage_router())
        outcome = await _outcome(session)

        assert outcome.total == 0
        assert outcome.reason == "disabled"
        assert statements == [], "drapeau fermé ⇒ zéro statement, pas un statement inutile"

    async def test_no_connection_is_not_a_closed_flag(self, _open_flag: None) -> None:
        from brain_v42.db.session_derived_capture import absorb_tracer_ledger

        session, _ = _session(_two_stage_router())
        outcome = await absorb_tracer_ledger(session, _Target(id=uuid4()), "")

        assert outcome.total == 0
        assert outcome.reason == "no_connection"

    async def test_nothing_eligible_is_not_a_refusal(self, _open_flag: None) -> None:
        """No row to move is not the same thing as a refusal by the rule."""
        session, _ = _session(_two_stage_router(connection_tracer=None))
        outcome = await _outcome(session)

        assert outcome.total == 0
        assert outcome.reason == "nothing_to_absorb"

    async def test_an_ambiguous_window_says_ambiguous_and_counts_its_rivals(
        self, _open_flag: None
    ) -> None:
        session, _ = _session(_two_stage_router(blocked=2))
        outcome = await _outcome(session)

        assert outcome.total == 0
        assert outcome.reason == "ambiguous"
        assert outcome.rivals == 2

    async def test_a_full_ledger_is_its_own_reason(self, _open_flag: None) -> None:
        session, _ = _session(_two_stage_router(connection_tracer=uuid4(), occupied=100))
        outcome = await _outcome(session)

        assert outcome.total == 0
        assert outcome.reason == "ledger_full"


# ---------------------------------------------------------------------------
# THE OBSERVABLE — a batch that makes silence legible must have a log that is READ
# ---------------------------------------------------------------------------


def _absorption_events(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in records if item.get("event") == "session_derived_capture.absorbed"]


class TestTheJournalIsAsserted:
    """Three observability defects survived this suite because it counted the
    counters and never read the log. A batch whose whole purpose is to make
    silence legible cannot ship a log nothing reads — otherwise we start over."""

    async def test_a_window_absorption_names_its_DONOR(self, _open_flag: None) -> None:
        """A2b. Without the donor, the INFERRED stage is the one we cannot undo.

        An UPDATE's `RETURNING` yields the NEW value of `session_id`: the donor
        was lost at the exact moment it matters.
        """
        donor, moved = uuid4(), [uuid4()]
        session, _ = _session(
            _two_stage_router(connection_tracer=None, window_moved=moved, window_donor=donor)
        )

        with capture_logs() as records:
            outcome = await _outcome(session)

        assert outcome.donors == (donor,)
        (event,) = _absorption_events(records)
        assert event["donors"] == [str(donor)]
        assert event["moved_ids"] == [str(moved[0])]
        assert event["moved_by_window"] == 1

    async def test_a_partial_absorption_still_counts_its_rivals(self, _open_flag: None) -> None:
        """A2a. The "total that masks", reintroduced one level down.

        Absorbing 1 row by connection and refusing 5 for ambiguity logged
        `absorbed` / `rivals_blocked=0`. The count sat under `if nothing moved` —
        hence silent as soon as a single row moved.
        """
        session, _ = _session(
            _two_stage_router(connection_tracer=uuid4(), connection_moved=[uuid4()], blocked=5)
        )

        with capture_logs() as records:
            outcome = await _outcome(session)

        assert (outcome.moved_by_connection, outcome.rivals) == (1, 5)
        (event,) = _absorption_events(records)
        assert event["reason"] == "absorbed"
        assert event["rivals_blocked"] == 5, (
            "une absorption partielle tait les artefacts qu'elle a refusés"
        )

    async def test_a_full_ledger_reaches_the_JOURNAL_and_not_only_the_api(
        self, _open_flag: None
    ) -> None:
        """A2c. The batch promised reasons distinguishable "in the API as in the log"."""
        session, _ = _session(_two_stage_router(connection_tracer=uuid4(), occupied=100))

        with capture_logs() as records:
            outcome = await _outcome(session)

        assert outcome.reason == "ledger_full"
        (event,) = _absorption_events(records)
        assert event["reason"] == "ledger_full"

    async def test_the_two_silent_refusals_stay_silent(self, _open_flag: None) -> None:
        """A closed flag and a missing connection write NOTHING, and that is intended.

        They are the only two cases where absorption was not even attempted.
        Logging them would put one line per tool call on every installation that
        has not armed capture — noise that would bury the other six.
        """
        from brain_v42.db.session_derived_capture import absorb_tracer_ledger

        session, _ = _session(_two_stage_router())
        with capture_logs() as records:
            await absorb_tracer_ledger(session, _Target(id=uuid4()), "")
        assert _absorption_events(records) == []


class TestTheWindowFiltersBeforeItBounds:
    """A1, guarded structurally on top of the integration bench."""

    async def test_the_bound_comes_AFTER_the_rivalry_filter(self, _open_flag: None) -> None:
        """Bounding before filtering makes absorption wrong AND non-deterministic.

        The `UNION ALL` has no `ORDER BY`: a `LIMIT` placed before the filter
        lets Postgres return an arbitrary batch, which can be entirely contested.
        The legitimate rows are then never absorbed — silently, and differently
        from one call to the next.
        """
        session, statements = _session(_two_stage_router(window_moved=[uuid4()]))
        await _outcome(session)

        selection = next(
            _sql(item)
            for item in statements
            if _sql(item).startswith("select brain_session_artifacts.knowledge_id")
        )
        assert "limit" in selection, "la sélection des candidats n'est plus bornée"
        assert selection.index("not (exists") < selection.rindex("limit"), (
            "le LIMIT précède le filtre de rivalité : il peut rendre un lot "
            "entièrement contesté et taire les lignes légitimes"
        )
