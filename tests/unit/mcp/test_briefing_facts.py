"""The briefing renders the registry's facts under `### État technique (mesuré)`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.facts.model import FactTarget, Measured, SourceIdentity, Unreadable
from brain_v42.facts.probe import FactDescriptor
from brain_v42.mcp.tools.session_tools import (
    _format_session_briefing,
    _section_technical_state,
    make_session_briefing_loader,
)
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
_ALEMBIC_DESCRIPTOR = FactDescriptor(
    name="alembic_head",
    definition_version=1,
    target=FactTarget.PRODUCTION,
    ttl_seconds=60,
    timeout_seconds=3,
    queue_timeout_seconds=2,
    deadline_seconds=5,
    briefing=True,
    policies={},
    value_schema={"revision": "string"},
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


def _alembic_head() -> Measured:
    """Build the measured schema fact that replaces the legacy reader."""
    return Measured.from_value(
        fact="alembic_head",
        definition_version=1,
        target=FactTarget.PRODUCTION,
        source=_IDENTITY,
        value={"revision": "054"},
        observation_id=uuid4(),
        measured_at=_NOW,
        duration_ms=12,
        ttl_seconds=60,
    )


class FakeRegistry:
    """The registry's briefing surface, with a recorded budget and a scripted answer."""

    def __init__(self, measurement: object, *, fail: bool = False) -> None:
        self._measurement = measurement
        self._fail = fail
        self.calls: list[tuple[tuple[str, ...], timedelta | None]] = []

    def briefing_names(self) -> tuple[str, ...]:
        return ("graph_projection_lag",)

    def names(self) -> tuple[str, ...]:
        return ("graph_projection_lag",)

    def describe(self, name: str) -> FactDescriptor:
        return _DESCRIPTOR

    def cached_age_seconds(self, name: str) -> float | None:
        return 0.0

    def refusals(self) -> dict[str, str]:
        return {}

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


def _loader_services() -> tuple[MagicMock, MagicMock, MagicMock, MagicMock, MagicMock, MagicMock]:
    """Provide only the independent reads a briefing composes."""
    context = MagicMock()
    context.get_by_key = AsyncMock(
        return_value=SimpleNamespace(
            project_key="brain-v42", current_focus="ship it", blockers=[], focus_updated_at=None
        )
    )
    decisions = MagicMock()
    decisions.list_all = AsyncMock(return_value=[])
    learnings = MagicMock()
    learnings.list_all = AsyncMock(return_value=[])
    dreams = MagicMock()
    dreams.killswitch_state = AsyncMock(return_value=_killswitches())
    dreams.last_failure = AsyncMock(return_value=None)
    features = MagicMock()
    features.roadmap_alive = AsyncMock(return_value=[])
    features.stale_pinned = AsyncMock(return_value=[])
    sessions = MagicMock()
    sessions.recent_checkpoints = AsyncMock(return_value=[])
    return context, decisions, learnings, dreams, features, sessions


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


@pytest.mark.asyncio
async def test_the_age_suffix_belongs_to_the_rendered_reading_only() -> None:
    """A refusal of the registry is not the cached reading: no `(mesuré il y a …)` on it."""
    from brain_v42.mcp.tools.session_tools import _render_briefing_facts

    refused = Unreadable(
        fact="graph_projection_lag",
        definition_version=1,
        target=FactTarget.PRODUCTION,
        error_code="capacity_timeout",
        where=None,
        observation_id=uuid4(),
        measured_at=_NOW,
        duration_ms=0,
        ttl_seconds=15,
        source_kind="probe",
    )

    class StaleCacheRegistry(FakeRegistry):
        def cached_age_seconds(self, name: str) -> float | None:
            return 180.0  # an older, different reading sits in the cache

    lines = await _render_briefing_facts(StaleCacheRegistry(refused))
    assert lines == ["- Projection graphe : illisible (capacity_timeout)"]

    from brain_v42.facts.model import with_source_kind

    served = with_source_kind(_quiet(), "cache")
    lines = await _render_briefing_facts(StaleCacheRegistry(served))
    assert lines[0].endswith(" (mesuré il y a 3 min)")


@pytest.mark.asyncio
async def test_a_fact_refused_at_registration_is_named_in_the_briefing() -> None:
    """An empty catalogue is never silent: the reader learns what was not registered and why."""
    from brain_v42.mcp.tools.session_tools import _render_briefing_facts

    class RefusingRegistry(FakeRegistry):
        def briefing_names(self) -> tuple[str, ...]:
            return ()

        def refusals(self) -> dict[str, str]:
            return {"graph_projection_lag": "unverifiable_target"}

    lines = await _render_briefing_facts(RefusingRegistry(_quiet()))
    assert lines == [
        "- Faits : graph_projection_lag non enregistré (cible production non vérifiable)"
    ]


@pytest.mark.asyncio
async def test_a_registered_alembic_fact_replaces_the_legacy_schema_read() -> None:
    """Two schema lines would leave the reader unsure which one was measured."""

    class AlembicRegistry(FakeRegistry):
        def __init__(self) -> None:
            super().__init__(_alembic_head())

        def names(self) -> tuple[str, ...]:
            return ("alembic_head",)

        def briefing_names(self) -> tuple[str, ...]:
            return ("alembic_head",)

        def describe(self, name: str) -> FactDescriptor:
            return _ALEMBIC_DESCRIPTOR

    schema_state_svc = MagicMock()
    schema_state_svc.current_revision = AsyncMock(return_value="054")
    loader = make_session_briefing_loader(
        *_loader_services(), schema_state_svc=schema_state_svc, fact_registry=AlembicRegistry()
    )

    briefing = await loader("brain-v42", uuid4())

    assert [line for line in briefing.splitlines() if line.startswith("- Schéma :")] == [
        "- Schéma : 054"
    ]
    assert schema_state_svc.current_revision.await_count == 0


@pytest.mark.asyncio
async def test_a_registry_without_alembic_keeps_the_legacy_schema_read() -> None:
    """Older catalogues retain their exact legacy briefing behaviour."""
    schema_state_svc = MagicMock()
    schema_state_svc.current_revision = AsyncMock(return_value="054")
    loader = make_session_briefing_loader(
        *_loader_services(), schema_state_svc=schema_state_svc, fact_registry=FakeRegistry(_quiet())
    )

    briefing = await loader("brain-v42", uuid4())

    assert "- Schéma : 054" in briefing
    assert schema_state_svc.current_revision.await_count == 1


@pytest.mark.asyncio
async def test_a_registry_failure_with_alembic_registered_still_renders_one_schema_line() -> None:
    """The legacy read is skipped only because the fact's line replaces it; when
    the registry fails, the replacement is gone and the section would carry no
    `Schéma` line at all — the silence it exists to prevent. The legacy read
    then runs, and the reader still sees the registry failure named."""

    class FailingAlembicRegistry(FakeRegistry):
        def __init__(self) -> None:
            super().__init__(_alembic_head(), fail=True)

        def names(self) -> tuple[str, ...]:
            return ("alembic_head",)

        def briefing_names(self) -> tuple[str, ...]:
            return ("alembic_head",)

        def describe(self, name: str) -> FactDescriptor:
            return _ALEMBIC_DESCRIPTOR

    schema_state_svc = MagicMock()
    schema_state_svc.current_revision = AsyncMock(return_value="054")
    loader = make_session_briefing_loader(
        *_loader_services(),
        schema_state_svc=schema_state_svc,
        fact_registry=FailingAlembicRegistry(),
    )

    briefing = await loader("brain-v42", uuid4())

    assert [line for line in briefing.splitlines() if line.startswith("- Schéma :")] == [
        "- Schéma : 054"
    ]
    assert "- Faits : illisibles (registre indisponible)" in briefing
    assert schema_state_svc.current_revision.await_count == 1


@pytest.mark.asyncio
async def test_one_fact_whose_renderer_raises_does_not_delete_the_other_lines() -> None:
    """Each line is rendered on its own: a renderer exception names its fact
    and leaves the five other facts readable."""
    from brain_v42.facts.render import render_fact_line
    from brain_v42.mcp.tools.session_tools import _render_briefing_facts

    class TwoFactRegistry(FakeRegistry):
        def __init__(self) -> None:
            super().__init__(_quiet())

        def briefing_names(self) -> tuple[str, ...]:
            return ("graph_projection_lag", "broken")

        def describe(self, name: str) -> FactDescriptor:
            return _DESCRIPTOR

        async def measure_many(self, names, *, max_age=None, budget=None):  # type: ignore[no-untyped-def]
            return {"graph_projection_lag": _quiet(), "broken": object()}

    lines = await _render_briefing_facts(TwoFactRegistry())

    assert lines[0].startswith("- Projection graphe : aucun retard observé")
    assert lines[1] == "- broken : illisible (rendu)"
    del render_fact_line


@pytest.mark.asyncio
async def test_a_registry_double_without_names_does_not_take_the_briefing_down() -> None:
    """The `alembic_head` lookup is part of the facts section, whose failures
    render and never propagate; the legacy schema read then stands."""

    class NamelessRegistry:
        def refusals(self) -> dict[str, str]:
            return {}

        def briefing_names(self) -> tuple[str, ...]:
            return ()

    schema_state_svc = MagicMock()
    schema_state_svc.current_revision = AsyncMock(return_value="054")
    loader = make_session_briefing_loader(
        *_loader_services(), schema_state_svc=schema_state_svc, fact_registry=NamelessRegistry()
    )

    briefing = await loader("brain-v42", uuid4())

    assert "- Schéma : 054" in briefing
