"""The briefing renders the registry's facts under `### État technique (mesuré)`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from brain_v42.facts.model import FactTarget, Measured, SourceIdentity, Unreadable
from brain_v42.facts.probe import FactDescriptor
from brain_v42.mcp.tools.session_tools import _format_session_briefing, _section_technical_state
from brain_v42.services.dream_run_service import KillswitchState

_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
_IDENTITY = SourceIdentity("7612696091383607335", "brain", "172.31.0.4", 5432)
_DESCRIPTOR = FactDescriptor(
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


def _quiet() -> Measured:
    return Measured.from_value(
        fact="graph_projection_lag",
        definition_version=1,
        target=FactTarget.PRODUCTION,
        source=_IDENTITY,
        value={
            "pending": 0,
            "ready": 0,
            "claimed": 0,
            "exhausted": 0,
            "lag_seconds": 0,
            "generation": 93,
            "armed": True,
            "lease_active": True,
            "recovery_active": False,
            "healthy": True,
        },
        observation_id=uuid4(),
        measured_at=_NOW,
        duration_ms=12,
        ttl_seconds=15,
    )


class FakeRegistry:
    """The registry's briefing surface, with a recorded budget and a scripted answer."""

    def __init__(self, measurement: object, *, fail: bool = False) -> None:
        self._measurement = measurement
        self._fail = fail
        self.calls: list[tuple[tuple[str, ...], timedelta | None]] = []

    def briefing_names(self) -> tuple[str, ...]:
        return ("graph_projection_lag",)

    def describe(self, name: str) -> FactDescriptor:
        return _DESCRIPTOR

    def cached_age_seconds(self, name: str) -> float | None:
        return 0.0

    async def measure_many(self, names, *, max_age=None, budget=None):  # type: ignore[no-untyped-def]
        self.calls.append((tuple(names), budget))
        if self._fail:
            raise RuntimeError("registry exploded")
        return dict.fromkeys(names, self._measurement)


def _killswitches() -> KillswitchState:
    return KillswitchState(
        last_run_date=None,
        promote_enabled=False,
        promote_dry=False,
        reorg_enabled=False,
        reorg_dry=False,
        promote_clean_dry_nights=0,
        reorg_clean_dry_nights=0,
    )


def test_the_legacy_call_shape_renders_the_pinned_fixture_unchanged() -> None:
    """No registry wired: not one character of tests/fixtures/briefing_full.md moves."""
    section = _section_technical_state(None, focus_tracked=True, focus_length=17, focus_octets=17)
    assert section == (
        "### État technique (mesuré)\n"
        "- Focus écrit : inconnu (jamais horodaté)\n"
        "- Focus : 17 / 10000 caractères (marge 9983 ; 17 octets)"
    )


def test_fact_lines_come_after_the_schema_line() -> None:
    section = _section_technical_state(
        "054",
        fact_lines=["- Projection graphe : aucun retard observé dans l'outbox — 0 en attente"],
    )
    assert section.splitlines() == [
        "### État technique (mesuré)",
        "- Schéma : 054",
        "- Projection graphe : aucun retard observé dans l'outbox — 0 en attente",
    ]


def test_the_composer_forwards_fact_lines_into_the_technical_section() -> None:
    ctx = SimpleNamespace(
        project_key="brain-v42", current_focus="ship it", blockers=[], focus_updated_at=None
    )
    text = _format_session_briefing(
        ctx,
        [],
        [],
        _killswitches(),
        None,
        [],
        [],
        schema_revision="054",
        fact_lines=["- Projection graphe : illisible (timeout)"],
    )
    technical = text.split("### État technique (mesuré)\n", 1)[1].split("\n\n", 1)[0]
    assert technical.splitlines()[:2] == [
        "- Schéma : 054",
        "- Projection graphe : illisible (timeout)",
    ]


@pytest.mark.asyncio
async def test_the_loader_measures_the_briefing_facts_under_a_four_second_budget() -> None:
    from brain_v42.mcp.tools.session_tools import _render_briefing_facts

    registry = FakeRegistry(_quiet())
    lines = await _render_briefing_facts(registry)
    assert registry.calls == [(("graph_projection_lag",), timedelta(seconds=4))]
    assert lines == [
        "- Projection graphe : aucun retard observé dans l'outbox — "
        "0 en attente, génération 93 armée, bail tenu"
    ]


@pytest.mark.asyncio
async def test_a_registry_failure_renders_a_line_and_never_removes_the_section() -> None:
    """Silence would send the reader back to the prose this section exists to contradict."""
    from brain_v42.mcp.tools.session_tools import _render_briefing_facts

    lines = await _render_briefing_facts(FakeRegistry(_quiet(), fail=True))
    assert lines == ["- Faits : illisibles (registre indisponible)"]


@pytest.mark.asyncio
async def test_an_unreadable_fact_renders_as_unreadable() -> None:
    from brain_v42.mcp.tools.session_tools import _render_briefing_facts

    unreadable = Unreadable(
        fact="graph_projection_lag",
        definition_version=1,
        target=FactTarget.PRODUCTION,
        error_code="timeout",
        where=None,
        observation_id=uuid4(),
        measured_at=_NOW,
        duration_ms=3000,
        ttl_seconds=15,
        source_kind="probe",
    )
    lines = await _render_briefing_facts(FakeRegistry(unreadable))
    assert lines == ["- Projection graphe : illisible (timeout)"]


def test_the_pinned_fixture_still_matches_without_a_registry() -> None:
    fixture = Path("tests/fixtures/briefing_full.md").read_text(encoding="utf-8")
    assert "Projection graphe" not in fixture
