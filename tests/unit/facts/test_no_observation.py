"""An expected empty state (no wet verify run yet) is not a probe error."""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from brain_v42.facts import SourceIdentity, Unreadable
from brain_v42.facts.model import ERROR_CODES, NoObservationError

from .test_registry import FakeProbe, FakeSource, registry
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
