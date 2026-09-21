"""Unit contracts for append-only fact definition registration."""

from __future__ import annotations

import pytest

from brain_v42.facts import FactTarget, definition_digest
from brain_v42.facts.probe import FactDescriptor
from brain_v42.repositories.pg_fact_definitions import DefinitionOutcome, register_definition


def _descriptor() -> FactDescriptor:
    return FactDescriptor(
        name="graph_projection_lag",
        definition_version=1,
        target=FactTarget.PRODUCTION,
        ttl_seconds=15,
        timeout_seconds=3,
        queue_timeout_seconds=2,
        deadline_seconds=5,
        briefing=True,
        policies={"late_after_seconds": 300},
        value_schema={"pending": "int"},
    )


class _Result:
    def __init__(self, scalar: str | None) -> None:
        self._scalar = scalar

    def scalar_one_or_none(self) -> str | None:
        return self._scalar

    def scalar_one(self) -> str:
        assert self._scalar is not None
        return self._scalar


class _Session:
    def __init__(self, results: list[str | None]) -> None:
        self._results = iter(results)
        self.statements: list[object] = []
        self.info: dict[str, object] = {}

    async def execute(self, statement: object) -> _Result:
        self.statements.append(statement)
        return _Result(next(self._results))


def _compiled_statements(session: _Session) -> list[str]:
    return [str(statement).upper() for statement in session.statements]


@pytest.mark.asyncio
async def test_register_definition_reports_inserted_from_returning_row() -> None:
    """The first observed definition is persisted once, without a repair write."""
    descriptor = _descriptor()
    session = _Session([definition_digest(descriptor)])

    outcome, stored = await register_definition(
        session, descriptor, digest=definition_digest(descriptor)
    )

    assert outcome is DefinitionOutcome.INSERTED
    assert stored is None
    assert len(session.statements) == 1
    assert all("UPDATE" not in statement for statement in _compiled_statements(session))


@pytest.mark.asyncio
async def test_register_definition_reports_matched_after_conflict_readback() -> None:
    """A matching immutable row is quiet steady state, not another insert or update."""
    descriptor = _descriptor()
    session = _Session([None, definition_digest(descriptor)])

    outcome, stored = await register_definition(
        session, descriptor, digest=definition_digest(descriptor)
    )

    assert outcome is DefinitionOutcome.MATCHED
    assert stored is None
    assert len(session.statements) == 2
    assert all("UPDATE" not in statement for statement in _compiled_statements(session))


@pytest.mark.asyncio
async def test_register_definition_reports_drift_without_mutating_stored_row() -> None:
    """A competing digest remains evidence of an unversioned definition change."""
    descriptor = _descriptor()
    session = _Session([None, "0" * 64])

    outcome, stored = await register_definition(
        session, descriptor, digest=definition_digest(descriptor)
    )

    assert outcome is DefinitionOutcome.DRIFTED
    # The stored digest is RETURNED, not smuggled through session.info: the caller
    # needs it for the drift event and must not pay a second SELECT for it.
    assert stored == "0" * 64
    assert len(session.statements) == 2
    assert all("UPDATE" not in statement for statement in _compiled_statements(session))
