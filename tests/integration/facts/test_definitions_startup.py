"""Real PostgreSQL evidence for definition registration at fact startup."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from brain_v42.db.tables import knowledge_fact_definitions
from brain_v42.facts import FactRegistry, FactTarget, SourceIdentity
from brain_v42.facts.definitions_startup import register_fact_definitions

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class _Source:
    async def identity(self) -> SourceIdentity:
        return SourceIdentity("7612696091383607335", "brain_test", "127.0.0.1", 5432)


class _Probe:
    def __init__(self, name: str) -> None:
        self.name = name
        self.definition_version = 1
        self.target = FactTarget.PRODUCTION
        self.ttl = timedelta(seconds=15)
        self.timeout = timedelta(seconds=1)
        self.briefing = False
        self.policies = {"late_after_seconds": 300}
        self.value_schema = {"pending": "int"}

    async def measure(self, source: _Source) -> Mapping[str, object]:
        return {"pending": 0}


def _registry(*names: str) -> FactRegistry:
    @asynccontextmanager
    async def source_factory() -> AsyncIterator[_Source]:
        yield _Source()

    identity = SourceIdentity("7612696091383607335", "brain_test", "127.0.0.1", 5432)
    registry = FactRegistry(
        sources={FactTarget.PRODUCTION: source_factory},
        expected={FactTarget.PRODUCTION: identity},
    )
    for name in names:
        registry.register(_Probe(name))
    registry.freeze()
    return registry


@asynccontextmanager
async def _rolled_back_session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Give startup sessions one shared future-only transaction that always rolls back."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(connection, class_=AsyncSession, expire_on_commit=False)
        try:
            yield factory
        finally:
            await transaction.rollback()


async def test_startup_registers_idempotently_and_disables_only_drifting_fact(
    engine: AsyncEngine,
) -> None:
    """First-seen rows remain immutable while a forged digest only disables its matching name."""
    first_name = f"definition_first_{uuid4().hex}"
    second_name = f"definition_second_{uuid4().hex}"
    drifting_name = f"definition_drift_{uuid4().hex}"
    registry = _registry(first_name, second_name, drifting_name)

    async with _rolled_back_session_factory(engine) as session_factory:
        async with session_factory() as session, session.begin():
            await session.execute(
                sa.insert(knowledge_fact_definitions).values(
                    fact_name=drifting_name,
                    definition_version=1,
                    target=FactTarget.PRODUCTION.value,
                    ttl_seconds=15,
                    timeout_seconds=1,
                    policies={"late_after_seconds": 300},
                    value_schema={"pending": "int"},
                    digest="0" * 64,
                )
            )

        with capture_logs() as logs:
            await register_fact_definitions(registry, session_factory)
        async with session_factory() as session:
            rows = (
                await session.execute(
                    sa.select(
                        knowledge_fact_definitions.c.fact_name,
                        knowledge_fact_definitions.c.registered_at,
                    ).where(
                        knowledge_fact_definitions.c.fact_name.in_(
                            (first_name, second_name, drifting_name)
                        )
                    )
                )
            ).all()
        assert {row.fact_name for row in rows} == {first_name, second_name, drifting_name}
        first_registered_at = {row.fact_name: row.registered_at for row in rows}
        assert registry.disabled() == {drifting_name: "definition_drift"}
        assert first_name not in registry.disabled()
        assert second_name not in registry.disabled()
        drift_log = next(log for log in logs if log["event"] == "facts.definition_drift")
        assert drift_log["fact"] == drifting_name
        assert drift_log["log_level"] == "error"
        assert drift_log["stored_digest"] == "0" * 64

        await register_fact_definitions(registry, session_factory)
        async with session_factory() as session:
            second_registered_at = dict(
                (
                    await session.execute(
                        sa.select(
                            knowledge_fact_definitions.c.fact_name,
                            knowledge_fact_definitions.c.registered_at,
                        ).where(
                            knowledge_fact_definitions.c.fact_name.in_((first_name, second_name))
                        )
                    )
                ).all()
            )
        assert second_registered_at == {
            first_name: first_registered_at[first_name],
            second_name: first_registered_at[second_name],
        }


async def test_startup_inserts_every_registered_definition_and_keeps_second_run_unchanged(
    engine: AsyncEngine,
) -> None:
    """A clean first start writes the catalogue once and the second only matches it."""
    names = (f"definition_clean_{uuid4().hex}", f"definition_clean_{uuid4().hex}")
    registry = _registry(*names)

    async with _rolled_back_session_factory(engine) as session_factory:
        await register_fact_definitions(registry, session_factory)
        async with session_factory() as session:
            first_rows = dict(
                (
                    await session.execute(
                        sa.select(
                            knowledge_fact_definitions.c.fact_name,
                            knowledge_fact_definitions.c.registered_at,
                        ).where(knowledge_fact_definitions.c.fact_name.in_(names))
                    )
                ).all()
            )
        assert set(first_rows) == set(names)

        await register_fact_definitions(registry, session_factory)
        async with session_factory() as session:
            second_rows = dict(
                (
                    await session.execute(
                        sa.select(
                            knowledge_fact_definitions.c.fact_name,
                            knowledge_fact_definitions.c.registered_at,
                        ).where(knowledge_fact_definitions.c.fact_name.in_(names))
                    )
                ).all()
            )
        assert second_rows == first_rows


async def test_startup_database_failure_leaves_every_fact_enabled_and_logs_error() -> None:
    """Unreachable persistence must preserve the degraded read path rather than stop startup."""
    registry = _registry(f"definition_failure_{uuid4().hex}")

    def failing_session_factory() -> object:
        raise RuntimeError("database unavailable")

    with capture_logs() as logs:
        await register_fact_definitions(registry, failing_session_factory)  # type: ignore[arg-type]

    assert registry.disabled() == {}
    assert any(log["event"] == "facts.definition_registration_failed" for log in logs)
    assert any(log["log_level"] == "error" for log in logs)
