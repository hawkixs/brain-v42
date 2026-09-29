"""Unit contract: `register_fact_definitions` reports whether the pass completed.

Real-PostgreSQL evidence for the persistence itself lives in
`tests/integration/facts/test_definitions_startup.py`; this file fakes
`register_definition` so the completion flag can be pinned without a database.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from structlog.testing import capture_logs

from brain_v42.facts.definitions_startup import register_fact_definitions
from brain_v42.facts.model import FactTarget, SourceIdentity
from brain_v42.facts.probe import FactDescriptor
from brain_v42.facts.registry import FactRegistry
from brain_v42.repositories.pg_fact_definitions import DefinitionOutcome


class _FakeTransaction:
    async def __aenter__(self) -> _FakeTransaction:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    def begin(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _fake_session_factory() -> _FakeSession:
    return _FakeSession()


class _Probe:
    def __init__(self, name: str) -> None:
        self.name = name
        self.definition_version = 1
        self.target = FactTarget.PRODUCTION
        self.ttl = timedelta(seconds=15)
        self.timeout = timedelta(seconds=1)
        self.briefing = False
        self.policies: dict[str, int] = {}
        self.value_schema = {"pending": "int"}

    async def measure(self, source: object) -> dict[str, object]:
        return {"pending": 0}


def _registry(*names: str) -> FactRegistry:
    @asynccontextmanager
    async def source():
        yield object()

    identity = SourceIdentity("1", "brain_test", "127.0.0.1", 5432)
    registry = FactRegistry(
        sources={FactTarget.PRODUCTION: source}, expected={FactTarget.PRODUCTION: identity}
    )
    for name in names:
        registry.register(_Probe(name))
    registry.freeze()
    return registry


async def test_returns_true_after_a_clean_full_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_register_definition(
        session: object, descriptor: FactDescriptor, *, digest: str
    ) -> tuple[DefinitionOutcome, str | None]:
        return DefinitionOutcome.INSERTED, None

    monkeypatch.setattr(
        "brain_v42.facts.definitions_startup.register_definition", fake_register_definition
    )
    registry = _registry("fact_a", "fact_b")

    completed = await register_fact_definitions(registry, _fake_session_factory)

    assert completed is True
    assert registry.disabled() == {}


async def test_returns_true_when_a_drifted_fact_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling a drifted fact is still a COMPLETED registration pass."""

    async def fake_register_definition(
        session: object, descriptor: FactDescriptor, *, digest: str
    ) -> tuple[DefinitionOutcome, str | None]:
        if descriptor.name == "drifting":
            return DefinitionOutcome.DRIFTED, "0" * 64
        return DefinitionOutcome.MATCHED, digest

    monkeypatch.setattr(
        "brain_v42.facts.definitions_startup.register_definition", fake_register_definition
    )
    registry = _registry("fact_a", "drifting")

    with capture_logs() as logs:
        completed = await register_fact_definitions(registry, _fake_session_factory)

    assert completed is True
    assert registry.disabled() == {"drifting": "definition_drift"}
    assert any(record["event"] == "facts.definition_drift" for record in logs)


async def test_returns_false_when_a_statement_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_session_factory() -> object:
        raise RuntimeError("database unavailable")

    registry = _registry("fact_a")

    with capture_logs() as logs:
        completed = await register_fact_definitions(registry, failing_session_factory)  # type: ignore[arg-type]

    assert completed is False
    assert registry.disabled() == {}
    assert any(record["event"] == "facts.definition_registration_failed" for record in logs)
