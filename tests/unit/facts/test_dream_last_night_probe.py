"""Unit contracts for the Dream latest-night fact probe."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime

import pytest

from brain_v42.facts import FactRegistry, FactTarget, Measured, SourceIdentity
from brain_v42.facts.probe import check_value_schema
from brain_v42.facts.probes.dream_last_night import DreamLastNightProbe

_IDENTITY = SourceIdentity(
    system_identifier="7612696091383607335",
    database="brain",
    server_addr="172.31.0.4",
    server_port=5432,
)
_ROW = {
    "run_date": date(2026, 9, 19),
    "rows": 4,
    "done": 1,
    "fail": 1,
    "timeout": 0,
    "partial": 1,
    "other": 1,
    "wet": 2,
    "dry": 1,
    "projects": 2,
    "finished_at": datetime(2026, 9, 20, 5, 6, 7, tzinfo=UTC),
}


class _MappingResult:
    """Mirror the mapping-only result shape consumed by the repository."""

    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def mappings(self) -> _MappingResult:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self._row


class _MappingSession:
    """Supply a complete aggregate row without replacing the repository reader."""

    async def execute(self, statement: object) -> _MappingResult:
        del statement
        return _MappingResult(_ROW)


class _Source:
    """Give the probe its caller-owned session and registry its matching identity."""

    def __init__(self) -> None:
        self.session = _MappingSession()

    async def identity(self) -> SourceIdentity:
        return _IDENTITY


async def test_probe_declares_and_maps_the_latest_night_scalars() -> None:
    """A date, timestamp, or aggregate counter with the wrong JSON shape is invalid evidence."""
    probe = DreamLastNightProbe()

    value = await probe.measure(_Source())  # type: ignore[arg-type]

    assert probe.name == "dream_last_night"
    assert probe.definition_version == 1
    assert probe.target is FactTarget.PRODUCTION
    assert probe.ttl.total_seconds() == 60
    assert probe.timeout.total_seconds() == 3
    assert probe.briefing is False
    assert probe.policies == {}
    assert probe.value_schema == {
        "run_date": "string",
        "rows": "int",
        "done": "int",
        "fail": "int",
        "timeout": "int",
        "partial": "int",
        "other": "int",
        "wet": "int",
        "dry": "int",
        "projects": "int",
        "finished_at_epoch": "int",
    }
    assert value == {
        "run_date": "2026-09-19",
        "rows": 4,
        "done": 1,
        "fail": 1,
        "timeout": 0,
        "partial": 1,
        "other": 1,
        "wet": 2,
        "dry": 1,
        "projects": 2,
        "finished_at_epoch": 1789880767,
    }
    check_value_schema(value, probe.value_schema)


async def test_probe_refuses_to_invent_a_night_when_the_repository_has_none(monkeypatch) -> None:
    """A zero summary would falsely claim that an unreadable history was clean."""

    async def no_last_night(session: object) -> None:
        del session
        return None

    monkeypatch.setattr("brain_v42.facts.probes.dream_last_night.read_last_night", no_last_night)

    with pytest.raises(ValueError, match="dream_runs has no night"):
        await DreamLastNightProbe().measure(_Source())  # type: ignore[arg-type]


async def test_registry_measures_the_fact_without_including_it_in_the_briefing() -> None:
    """A non-briefing fact remains tool-readable and shares the normal TTL cache."""

    @asynccontextmanager
    async def source_factory():
        yield _Source()

    registry = FactRegistry(
        sources={FactTarget.PRODUCTION: source_factory},
        expected={FactTarget.PRODUCTION: _IDENTITY},
    )
    registry.register(DreamLastNightProbe())
    registry.freeze()
    try:
        measurement = await registry.measure("dream_last_night")
    finally:
        await registry.aclose()

    assert isinstance(measurement, Measured)
    assert measurement.value["run_date"] == "2026-09-19"
    assert registry.briefing_names() == ()


async def test_a_night_without_a_timestamp_is_unreadable_not_an_epoch_zero() -> None:
    """`created_at` is nullable in the table: a night whose rows all lack it has
    no `finished_at`, and the probe must raise rather than publish a zero."""

    class _NoTimestampSession:
        async def execute(self, statement: object) -> _MappingResult:
            del statement
            return _MappingResult({**_ROW, "finished_at": None})

    class _NoTimestampSource:
        def __init__(self) -> None:
            self.session = _NoTimestampSession()

        async def identity(self) -> SourceIdentity:
            return _IDENTITY

    with pytest.raises(ValueError, match="timestamp"):
        await DreamLastNightProbe().measure(_NoTimestampSource())  # type: ignore[arg-type]
