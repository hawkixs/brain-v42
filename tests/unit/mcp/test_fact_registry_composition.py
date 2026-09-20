"""The composition root builds the closed catalogue and closes it before the engine."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from brain_v42.facts.composition import build_fact_registry
from brain_v42.facts.model import FactTarget, SourceIdentity
from brain_v42.facts.registry import FactRegistry, RegistryClosedError, RegistryFrozenError

_DECLARED = {
    "system_identifier": "7612696091383607335",
    "database": "brain",
    "server_addr": "172.31.0.4",
    "server_port": 5432,
}
_RELEASE_DECLARED = {"release_sha": "a" * 40, "package_version": "0.6.0"}
_HOST_DECLARED = {"hostname": "host-a"}
_PRODUCTION_FACTS = ("graph_projection_lag", "dream_last_night")


def test_with_a_declared_identity_the_first_fact_is_registered_and_the_catalogue_frozen() -> None:
    registry = build_fact_registry(_DECLARED, session_factory=MagicMock())
    assert registry.names() == _PRODUCTION_FACTS
    assert registry.briefing_names() == ("graph_projection_lag",)
    # Only the production identity is declared: every other target's facts are
    # refused, named, and the service still starts.
    assert registry.refusals() == {
        "live_release_sha": "unverifiable_target",
        "alembic_head_shipped": "unverifiable_target",
        "dream_killswitches_declared": "unverifiable_target",
    }
    assert "dream_last_night" not in registry.briefing_names()
    assert registry.describe("graph_projection_lag").target is FactTarget.PRODUCTION
    with pytest.raises(RegistryFrozenError):
        registry.register(MagicMock(name="late", target=FactTarget.PRODUCTION))


def test_a_declared_host_identity_registers_the_dream_killswitch_fact() -> None:
    registry = build_fact_registry(
        _DECLARED,
        session_factory=MagicMock(),
        declared_live_release_identity=_RELEASE_DECLARED,
        declared_host_identity=_HOST_DECLARED,
    )
    assert registry.names() == (
        "graph_projection_lag",
        "live_release_sha",
        "alembic_head_shipped",
        "dream_killswitches_declared",
        "dream_last_night",
    )
    assert registry.briefing_names() == registry.names()[:-1]
    assert registry.describe("dream_killswitches_declared").target is FactTarget.HOST


@pytest.mark.parametrize(
    ("keyword", "declared", "event"),
    [
        ("declared_live_release_identity", None, "facts.live_release_identity_undeclared"),
        ("declared_host_identity", None, "facts.host_identity_undeclared"),
        (
            "declared_live_release_identity",
            {"release_sha": "bad"},
            "facts.live_release_identity_refused",
        ),
        (
            "declared_host_identity",
            {"hostname": "host-a", "extra": "x"},
            "facts.host_identity_refused",
        ),
    ],
)
def test_new_identity_declarations_log_refusals_but_keep_service_up(
    keyword: str, declared: object, event: str
) -> None:
    with capture_logs() as logs:
        registry = build_fact_registry(
            _DECLARED, session_factory=MagicMock(), **{keyword: declared}
        )
    assert registry.names() == _PRODUCTION_FACTS
    assert any(record["event"] == event for record in logs)


@pytest.mark.parametrize(
    "declared",
    [None, {**_DECLARED, "system_identifier": "abc"}, MagicMock()],
    ids=["undeclared", "refused-by-the-model", "not-a-mapping"],
)
def test_without_a_verifiable_identity_no_production_fact_registers_and_the_service_starts(
    declared: object,
) -> None:
    """Fail closed, not down: an empty catalogue, said once in the journal."""
    with patch("brain_v42.facts.composition.logger") as logger:
        registry = build_fact_registry(declared, session_factory=MagicMock())  # type: ignore[arg-type]
    assert isinstance(registry, FactRegistry)
    assert registry.names() == ()
    assert logger.warning.call_count >= 1
    with pytest.raises(RegistryFrozenError):
        registry.register(MagicMock(name="late", target=FactTarget.PRODUCTION))


def test_a_refused_probe_is_recorded_with_its_reason_for_the_briefing() -> None:
    registry = build_fact_registry(None, session_factory=MagicMock())
    assert registry.refusals() == {
        "graph_projection_lag": "unverifiable_target",
        "live_release_sha": "unverifiable_target",
        "alembic_head_shipped": "unverifiable_target",
        "dream_killswitches_declared": "unverifiable_target",
        "dream_last_night": "unverifiable_target",
    }


def test_undeclared_live_release_refuses_its_facts_while_production_still_registers() -> None:
    registry = build_fact_registry(
        _DECLARED, session_factory=MagicMock(), declared_host_identity=_HOST_DECLARED
    )

    assert registry.names() == (
        "graph_projection_lag",
        "dream_killswitches_declared",
        "dream_last_night",
    )
    assert registry.refusals() == {
        "live_release_sha": "unverifiable_target",
        "alembic_head_shipped": "unverifiable_target",
    }


def test_build_services_exposes_the_registry() -> None:
    session_factory = MagicMock()
    mock_settings = MagicMock(
        brain_embedding_token_file=None,
        embedding_service_url="http://localhost:8003",
        embedding_dimension=1536,
        metrics_enabled=False,
        decay_enabled=False,
        graph_enabled=False,
        graph_projector_enabled=False,
        neo4j_url=None,
        neo4j_user="neo4j",
        neo4j_password="",
        neo4j_timeout=5.0,
    )
    mock_settings.facts_production_identity.return_value = _DECLARED
    mock_settings.facts_live_release_identity.side_effect = ValueError("bad release declaration")
    mock_settings.facts_host_identity.side_effect = ValueError("bad host declaration")
    with capture_logs() as logs:
        with (
            patch("brain_v42.mcp.server.get_session_factory", return_value=session_factory),
            patch("brain_v42.mcp.server.get_settings", return_value=mock_settings),
            patch("brain_v42.mcp.server.build_embedding_service", return_value=MagicMock()),
            patch("brain_v42.mcp.server.FeatureCreationService"),
            patch("brain_v42.db.engine.get_engine", return_value=MagicMock()),
            patch("brain_v42.metrics.collector.get_settings", return_value=mock_settings),
            patch("brain_v42.mcp.server.create_neo4j_driver", return_value=None),
        ):
            from brain_v42.mcp.server import build_services

            services = build_services()
    assert isinstance(services["fact_registry"], FactRegistry)
    assert services["fact_registry"].names() == _PRODUCTION_FACTS
    assert {record["event"] for record in logs} >= {
        "facts.live_release_identity_unreadable",
        "facts.host_identity_unreadable",
    }


@pytest.mark.asyncio
async def test_the_lifecycle_closes_the_registry_before_disposing_the_engine() -> None:
    """A probe task must never outlive its connection factory."""
    order: list[str] = []

    class RecordingRegistry(FactRegistry):
        async def aclose(self) -> None:
            order.append("registry.aclose")
            await super().aclose()

    async def dispose() -> None:
        order.append("dispose_engine")

    registry = RecordingRegistry(
        sources={FactTarget.PRODUCTION: MagicMock()},
        expected={FactTarget.PRODUCTION: SourceIdentity.from_mapping(_DECLARED)},
    )
    registry.freeze()
    settings = MagicMock(otel_tracing_enabled=False, decay_enabled=False)
    services = {
        "access_logger": MagicMock(),
        "plan_indexer": MagicMock(index_all=AsyncMock(return_value={})),
        "neo4j_driver": None,
        "access_log_repo": MagicMock(),
        "decay_calculator": MagicMock(),
        "fact_registry": registry,
    }
    with (
        patch("brain_v42.mcp.server.close_neo4j_driver", new_callable=AsyncMock),
        patch("brain_v42.mcp.server.dispose_engine", side_effect=dispose),
        patch("brain_v42.mcp.server.close_activity_reporter", new_callable=AsyncMock),
    ):
        from brain_v42.mcp.server import app_lifecycle

        async with app_lifecycle(settings, services, MagicMock()):
            await asyncio.sleep(0)

    assert order == ["registry.aclose", "dispose_engine"]
    with pytest.raises(RegistryClosedError):
        await registry.measure("graph_projection_lag")
