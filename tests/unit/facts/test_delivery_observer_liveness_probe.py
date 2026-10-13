"""Contracts for the observer liveness measurement."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from brain_v42.facts import FactTarget
from brain_v42.facts.probe import check_value_schema
from brain_v42.facts.probes.delivery_observer_liveness import DeliveryObserverLivenessProbe


async def test_probe_declares_policy_and_translates_repository_snapshot(monkeypatch):
    session = object()
    expected = {
        "lock_held": True,
        "overdue": 0,
        "last_confirmation_age_seconds": 15,
        "healthy": True,
    }

    async def read_liveness(received_session, *, overdue_after_seconds):
        assert received_session is session
        assert overdue_after_seconds == 600
        return SimpleNamespace(**expected)

    monkeypatch.setattr(
        "brain_v42.facts.probes.delivery_observer_liveness.read_observer_liveness",
        read_liveness,
    )
    probe = DeliveryObserverLivenessProbe()
    value = await probe.measure(SimpleNamespace(session=session))  # type: ignore[arg-type]
    assert probe.name == "delivery_observer_liveness"
    assert probe.definition_version == 1
    assert probe.target is FactTarget.PRODUCTION
    assert probe.ttl == timedelta(seconds=15)
    assert probe.timeout == timedelta(seconds=3)
    assert probe.briefing is True
    assert probe.policies == {"overdue_after_seconds": 600}
    assert value == expected
    check_value_schema(value, probe.value_schema)
