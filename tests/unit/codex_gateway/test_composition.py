"""Production gateway composition and readiness contracts."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.codex_gateway.composition import GatewayRuntime


def _runtime(*, schema_ready: bool) -> tuple[GatewayRuntime, AsyncMock]:
    session = AsyncMock()
    session.scalar.return_value = schema_ready
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=None)
    session_factory = MagicMock(return_value=context)
    runtime = GatewayRuntime(
        services=Any,  # type: ignore[arg-type]
        embedding_service=Any,  # type: ignore[arg-type]
        session_factory=session_factory,
    )
    return runtime, session


@pytest.mark.asyncio
async def test_readiness_requires_the_complete_codex_database_contract() -> None:
    runtime, session = _runtime(schema_ready=False)

    with pytest.raises(RuntimeError, match="database contract"):
        await runtime.readiness()

    statement = str(session.scalar.await_args.args[0])
    assert "codex_ticket_v1" in statement
    assert "codex_consolidation_log_v1" in statement
    assert "trg_feature_artifact_live_target" in statement
    assert "trg_ticket_participants_immutable" in statement
    assert "security_barrier=true" in statement
    assert "tgenabled IN ('O', 'A')" in statement
    assert "tgenabled <> 'D'" not in statement


@pytest.mark.asyncio
async def test_readiness_accepts_a_compatible_future_schema() -> None:
    runtime, _session = _runtime(schema_ready=True)

    await runtime.readiness()


def test_build_production_runtime_wires_the_project_guard() -> None:
    """La SECONDE racine de composition, et c'est le rail vivant du dream.

    ``BRAIN_DREAM_AGENT_PROVIDER=codex`` par défaut : les écritures de connaissance
    de la nuit passent par cette passerelle, pas par le serveur MCP. Un test qui
    n'épinglerait que ``build_services`` laisserait ce chemin-là sans témoin —
    et aucun test ne référençait ``build_production_runtime``.

    Comme son jumeau, il épingle un câblage plutôt qu'un comportement : la garde
    est fail-open quand le repo manque, donc son absence ne casse rien d'autre.
    """
    from unittest.mock import patch

    from brain_v42.codex_gateway.composition import build_production_runtime

    settings = MagicMock(embedding_service_url="http://localhost:8003")
    with (
        patch("brain_v42.codex_gateway.composition.get_session_factory", return_value=MagicMock()),
        # Patch the factory, not the class: composition builds through
        # build_embedding_service now, so patching GPUEmbeddingService here
        # resolves (it survives as a type annotation) but intercepts nothing.
        patch(
            "brain_v42.codex_gateway.composition.build_embedding_service",
            return_value=MagicMock(),
        ),
    ):
        runtime = build_production_runtime(settings)

    # Le service de décision n'est pas exposé directement par GatewayServices : il
    # n'est atteignable qu'à travers ProposalService, qui est justement le chemin
    # d'application des propositions du dream. C'est donc bien celui-là qu'il faut
    # atteindre, pas une référence de commodité.
    for name, service in (
        ("learning", runtime.services.learning),
        ("decision", runtime.services.proposal._decision_service),
    ):
        assert getattr(service, "_project_context_repo", None) is not None, (
            f"le service {name} de la passerelle Codex est construit sans "
            f"project_context_repo : la garde projet-inconnu est désarmée sur le "
            f"rail d'écriture de la nuit dream, et l'échec sera SILENCIEUX"
        )

    # Les tickets, eux, ne PEUVENT pas être mal câblés : TicketService exige son
    # repo en argument positionnel et refuse en ligne, sans passer par
    # require_known_project. Cette asymétrie est la raison d'être du test — les
    # cinq services de connaissance acceptent None et se taisent, celui-ci non.
    assert runtime.services.ticket._service._ctx_repo is not None
