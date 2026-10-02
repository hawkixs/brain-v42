"""An expected empty state (no wet verify run yet) is not a probe error."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from structlog.testing import capture_logs

from brain_v42.facts import SourceIdentity, Unreadable
from brain_v42.facts.model import ERROR_CODES, NoObservationError
from brain_v42.facts.probes.claims_verification_last_night import ClaimsVerificationLastNightProbe
from brain_v42.facts.registry import FactRegistry

from .test_registry import FakeProbe, FakeSource, identity, registry
from .test_render import _DESCRIPTOR, _unreadable, render_fact_line


@pytest.mark.asyncio
async def test_a_probe_with_nothing_to_observe_is_no_observation_not_a_probe_error() -> None:
    probe = FakeProbe("empty", error=NoObservationError("dream_runs has no wet verify run"))
    with capture_logs() as records:
        result = await registry(probe).measure("empty")
    assert isinstance(result, Unreadable)
    assert result.error_code == "no_observation"
    assert result.where is None
    assert "no_observation" in ERROR_CODES
    # Nothing failed: no warning may suggest otherwise.
    assert [r for r in records if r.get("log_level") == "warning"] == []


@pytest.mark.asyncio
async def test_no_observation_on_the_wrong_cluster_is_still_a_target_mismatch() -> None:
    """An empty answer from an unexpected database proves nothing about the expected one."""
    other = SourceIdentity("7612696091383607335", "brain", "127.0.0.1", 5433)
    probe = FakeProbe("empty", error=NoObservationError("no run"))
    result = await registry(probe, source=FakeSource(other)).measure("empty")
    assert isinstance(result, Unreadable)
    assert (result.error_code, result.where) == ("target_mismatch", "server_port")


def test_the_briefing_says_nothing_was_observed_and_does_not_call_it_unreadable() -> None:
    unreadable = _unreadable("no_observation")
    line = render_fact_line(unreadable, _DESCRIPTOR, age_seconds=None)
    assert line == "- Projection graphe : aucune observation enregistrée"
    assert "illisible" not in line


class _BrokenReadSession:
    """The wet-run read itself fails: the probe must not mistake it for an empty answer.

    The clock read succeeds, so the failure lands on the very query whose empty
    result means `no_observation` (review of #265, round 2).
    """

    async def scalar(self, statement: object) -> object:
        return datetime(2026, 10, 2, tzinfo=UTC)

    async def execute(self, statement: object, parameters: object = None) -> object:
        raise OSError("connection reset by peer")


class _ProbeSource:
    def __init__(self, session: object) -> None:
        self.session = session

    async def identity(self) -> SourceIdentity:
        return identity()


def _claims_registry(session: object) -> FactRegistry:
    probe = ClaimsVerificationLastNightProbe()

    @asynccontextmanager
    async def factory() -> AsyncIterator[_ProbeSource]:
        yield _ProbeSource(session)

    fact_registry = FactRegistry(
        sources={probe.target: factory}, expected={probe.target: identity()}
    )
    fact_registry.register(probe)
    return fact_registry


@pytest.mark.asyncio
async def test_a_failed_database_read_of_the_claims_fact_stays_a_probe_error() -> None:
    result = await _claims_registry(_BrokenReadSession()).measure("claims_verification_last_night")
    assert isinstance(result, Unreadable)
    assert result.error_code == "probe_error"


@pytest.mark.asyncio
async def test_no_wet_run_of_the_claims_fact_is_no_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_run(*_: object) -> None:
        return None

    monkeypatch.setattr(
        "brain_v42.facts.probes.claims_verification_last_night.read_last_wet_verify_run", no_run
    )

    class _ClockSession:
        async def scalar(self, statement: object) -> datetime:
            return datetime(2026, 10, 2, tzinfo=UTC)

    result = await _claims_registry(_ClockSession()).measure("claims_verification_last_night")
    assert isinstance(result, Unreadable)
    assert result.error_code == "no_observation"
