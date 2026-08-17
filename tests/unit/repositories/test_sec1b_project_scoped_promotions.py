"""Failure-first repository contract for SEC1b scoped promotions."""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from brain_v42.models.adr import ADRCreate
from brain_v42.models.runbook import RunbookCreate, RunbookStep
from brain_v42.repositories.pg_adr import PgADRRepo, SourceLearningNotFound
from brain_v42.repositories.pg_runbook import PgRunbookRepo

PROJECT_KEY = "sec1b-owned"


class StatementResult:
    def __init__(
        self,
        *,
        scalar: Any = None,
        row: Any = None,
        mapping: Any = None,
        rowcount: int = 1,
    ) -> None:
        self._scalar = scalar
        self._row = row
        self._mapping = mapping
        self.rowcount = rowcount

    def scalar_one(self) -> Any:
        return self._scalar

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def fetchone(self) -> Any:
        return self._row

    def mappings(self) -> StatementResult:
        return self

    def one(self) -> Any:
        return self._mapping


class Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: Any) -> bool:
        return False


class RecordingSession:
    def __init__(self, *, source_exists: bool = True) -> None:
        self.source_exists = source_exists
        self.statements: list[Any] = []

    def begin(self) -> Transaction:
        return Transaction()

    async def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> StatementResult:
        self.statements.append(statement)
        sql = str(statement)
        now = datetime.now(UTC)
        if "FROM learnings" in sql and sql.lstrip().startswith("SELECT"):
            return StatementResult(scalar=uuid4() if self.source_exists else None)
        if "next_number" in sql:
            return StatementResult(scalar=1)
        if sql.lstrip().startswith("INSERT INTO adrs"):
            return StatementResult(
                row={
                    "id": uuid4(),
                    "number": 1,
                    "title": "Scoped ADR",
                    "context": "Context",
                    "decision": "Decision",
                    "consequences": "Consequences",
                    "alternatives_considered": [],
                    "project_key": PROJECT_KEY,
                    "tags": [],
                    "status": "accepted",
                    "decided_at": now,
                    "superseded_by": None,
                    "embedding": None,
                    "metadata": {},
                    "created_at": now,
                    "updated_at": now,
                }
            )
        if sql.lstrip().startswith("INSERT INTO runbooks"):
            return StatementResult(
                mapping={
                    "id": uuid4(),
                    "title": "Scoped runbook",
                    "description": "Description",
                    "project_key": PROJECT_KEY,
                    "trigger": "Trigger",
                    "prerequisites": [],
                    "steps": [{"order": 1, "title": "Step"}],
                    "rollback_steps": [],
                    "estimated_duration": None,
                    "tags": [],
                    "metadata": {},
                    "execution_count": 0,
                    "last_executed_at": None,
                    "last_execution_status": None,
                    "embedding": None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        return StatementResult(rowcount=1)


def session_factory(session: RecordingSession) -> Any:
    @asynccontextmanager
    async def factory() -> Any:
        yield session

    return factory


def adr_data() -> ADRCreate:
    return ADRCreate(
        title="Scoped ADR",
        context="Context",
        decision="Decision",
        consequences="Consequences",
        project_key=PROJECT_KEY,
    )


def runbook_data() -> RunbookCreate:
    return RunbookCreate(
        title="Scoped runbook",
        description="Description",
        project_key=PROJECT_KEY,
        trigger="Trigger",
        steps=[RunbookStep(order=1, title="Step")],
    )


def normalized_sql(statement: Any) -> str:
    return " ".join(str(statement).split())


def index_of(statements: list[Any], fragment: str, *, prefix: str | None = None) -> int:
    for index, statement in enumerate(statements):
        sql = normalized_sql(statement)
        if fragment in sql and (prefix is None or sql.startswith(prefix)):
            return index
    raise AssertionError(f"statement containing {fragment!r} not found")


async def promote_runbook(session: RecordingSession, project_key: str | None) -> None:
    repo = PgRunbookRepo()
    repo.get_session = session_factory(session)
    args = (runbook_data(), None, uuid4(), 41)
    if project_key is None:
        await repo.create_with_promotion(*args)
    else:
        await repo.create_with_promotion(*args, project_key=project_key)


async def promote_adr(session: RecordingSession, project_key: str | None) -> None:
    repo = PgADRRepo()
    repo.get_session = session_factory(session)
    args = (adr_data(), None, uuid4(), True, 41)
    if project_key is None:
        await repo.create_with_promotion(*args)
    else:
        await repo.create_with_promotion(*args, project_key=project_key)


@pytest.mark.asyncio
@pytest.mark.parametrize("project_key", [None, PROJECT_KEY], ids=["admin", "scoped"])
async def test_runbook_target_gate_preserves_admin_and_scoped_lock_order(
    project_key: str | None,
) -> None:
    session = RecordingSession()

    await promote_runbook(session, project_key)

    gate = index_of(session.statements, "pg_advisory_xact_lock")
    target = index_of(session.statements, "INSERT INTO runbooks", prefix="INSERT")
    stamp = index_of(session.statements, "UPDATE learnings", prefix="UPDATE")
    audit = index_of(session.statements, "INSERT INTO dream_promotions", prefix="INSERT")
    if project_key is None:
        assert not any(
            "FROM learnings" in normalized_sql(statement)
            and normalized_sql(statement).startswith("SELECT")
            for statement in session.statements
        )
        assert gate < target < stamp < audit
    else:
        source = index_of(session.statements, "FROM learnings", prefix="SELECT")
        assert gate < source < target < stamp < audit


@pytest.mark.asyncio
async def test_runbook_admin_and_scoped_use_same_target_identity_gate() -> None:
    admin = RecordingSession()
    scoped = RecordingSession()

    await promote_runbook(admin, None)
    await promote_runbook(scoped, PROJECT_KEY)

    admin_gate = admin.statements[index_of(admin.statements, "pg_advisory_xact_lock")]
    scoped_gate = scoped.statements[index_of(scoped.statements, "pg_advisory_xact_lock")]
    assert admin_gate.compile().params == scoped_gate.compile().params
    admin_gate_identity = " ".join(str(value) for value in admin_gate.compile().params.values())
    assert PROJECT_KEY in admin_gate_identity
    assert "Scoped runbook" in admin_gate_identity


@pytest.mark.asyncio
@pytest.mark.parametrize("project_key", [None, PROJECT_KEY], ids=["admin", "scoped"])
async def test_adr_number_gate_preserves_admin_and_scoped_lock_order(
    project_key: str | None,
) -> None:
    session = RecordingSession()

    await promote_adr(session, project_key)

    number_gate = index_of(session.statements, "pg_advisory_xact_lock")
    target = index_of(session.statements, "INSERT INTO adrs", prefix="INSERT")
    if project_key is None:
        assert not any(
            "FROM learnings" in normalized_sql(statement)
            and normalized_sql(statement).startswith("SELECT")
            for statement in session.statements
        )
        assert number_gate < target
    else:
        source = index_of(session.statements, "FROM learnings", prefix="SELECT")
        assert number_gate < source < target


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["adr", "runbook"])
async def test_scoped_source_lock_and_stamp_include_project_predicate(kind: str) -> None:
    session = RecordingSession()

    if kind == "adr":
        await promote_adr(session, PROJECT_KEY)
    else:
        await promote_runbook(session, PROJECT_KEY)

    source = session.statements[index_of(session.statements, "FROM learnings", prefix="SELECT")]
    stamp = session.statements[index_of(session.statements, "UPDATE learnings", prefix="UPDATE")]
    for statement in (source, stamp):
        assert "learnings.project_key" in normalized_sql(statement)
        assert PROJECT_KEY in statement.compile().params.values()
    assert "FOR UPDATE" in normalized_sql(source)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["adr", "runbook"])
async def test_admin_keeps_historical_no_source_select_and_unscoped_stamp(kind: str) -> None:
    session = RecordingSession()

    if kind == "adr":
        await promote_adr(session, None)
    else:
        await promote_runbook(session, None)

    stamp = session.statements[index_of(session.statements, "UPDATE learnings", prefix="UPDATE")]
    assert not any(
        "FROM learnings" in normalized_sql(statement)
        and normalized_sql(statement).startswith("SELECT")
        for statement in session.statements
    )
    assert "learnings.project_key" not in normalized_sql(stamp)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["adr", "runbook"])
async def test_scoped_unavailable_source_is_generic_and_stops_before_any_write(kind: str) -> None:
    session = RecordingSession(source_exists=False)

    with pytest.raises(SourceLearningNotFound) as raised:
        if kind == "adr":
            await promote_adr(session, PROJECT_KEY)
        else:
            await promote_runbook(session, PROJECT_KEY)

    assert str(raised.value) == "source learning not found"
    assert not any(
        normalized_sql(statement).startswith(("INSERT", "UPDATE", "DELETE"))
        for statement in session.statements
    )


def test_internal_scope_is_keyword_only_and_exception_keeps_public_reexport() -> None:
    for method in (PgADRRepo.create_with_promotion, PgRunbookRepo.create_with_promotion):
        parameter = inspect.signature(method).parameters["project_key"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is None
    assert SourceLearningNotFound.__module__ == "brain_v42.repositories.promotion"
