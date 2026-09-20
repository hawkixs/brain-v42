"""Contracts for the production Alembic-head fact."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.facts.model import FactTarget, SourceIdentity, Unreadable
from brain_v42.facts.probes.alembic_head import AlembicHeadProbe
from brain_v42.facts.registry import FactRegistry
from brain_v42.facts.sources import PostgresSourceSession

_IDENTITY = SourceIdentity("7612696091383607335", "brain", "172.31.0.4", 5432)


def _source_session(revision: object) -> PostgresSourceSession:
    """Build the narrow PostgreSQL source surface the probe receives."""
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=revision)))
    return PostgresSourceSession(session)


def _registry_source(revision: object) -> PostgresSourceSession:
    """Build one source whose revision and identity reads share its session."""
    revision_result = MagicMock(scalar=MagicMock(return_value=revision))
    identity_result = MagicMock()
    identity_result.mappings.return_value.one.return_value = _IDENTITY.as_dict()
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[revision_result, identity_result])
    return PostgresSourceSession(session)


def test_descriptor_declares_the_stamped_production_value() -> None:
    """A changed target, bound or schema makes a fact claim about another thing."""
    probe = AlembicHeadProbe()

    assert probe.name == "alembic_head"
    assert probe.definition_version == 1
    assert probe.target is FactTarget.PRODUCTION
    assert probe.ttl == timedelta(seconds=60)
    assert probe.timeout == timedelta(seconds=3)
    assert probe.briefing is True
    assert probe.policies == {}
    assert probe.value_schema == {"revision": "string"}


@pytest.mark.asyncio
async def test_measure_returns_the_stamped_revision_from_the_source_snapshot() -> None:
    """The fact must read through the source session, never a fresh service session."""
    assert await AlembicHeadProbe().measure(_source_session("054")) == {"revision": "054"}


@pytest.mark.asyncio
async def test_measure_refuses_an_unstamped_database() -> None:
    """No alembic_version row is not a revision value a briefing may display."""
    with pytest.raises(ValueError, match="alembic_version has no row"):
        await AlembicHeadProbe().measure(_source_session(None))


@pytest.mark.asyncio
async def test_registry_measures_the_fact_from_a_verified_production_source() -> None:
    """The registry binds the revision to the source identity it verifies."""
    source = _registry_source("054")

    @asynccontextmanager
    async def factory() -> AsyncIterator[PostgresSourceSession]:
        yield source

    registry = FactRegistry(
        sources={FactTarget.PRODUCTION: factory}, expected={FactTarget.PRODUCTION: _IDENTITY}
    )
    registry.register(AlembicHeadProbe())

    measurement = await registry.measure("alembic_head")

    assert measurement.value == {"revision": "054"}


@pytest.mark.asyncio
async def test_registry_renders_a_failed_probe_as_an_unreadable_measurement() -> None:
    """A read error remains visible without leaking its diagnostic into the briefing."""
    session = MagicMock()
    session.execute = AsyncMock(side_effect=ValueError("database is unavailable"))
    source = PostgresSourceSession(session)

    @asynccontextmanager
    async def factory() -> AsyncIterator[PostgresSourceSession]:
        yield source

    registry = FactRegistry(
        sources={FactTarget.PRODUCTION: factory}, expected={FactTarget.PRODUCTION: _IDENTITY}
    )
    registry.register(AlembicHeadProbe())

    measurement = await registry.measure("alembic_head")

    assert isinstance(measurement, Unreadable)
    assert measurement.error_code == "probe_error"
    assert measurement.where == "ValueError in AlembicHeadProbe.measure"
