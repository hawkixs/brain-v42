"""Production wiring for the dedicated Codex gateway process."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from brain_v42.codex_gateway.dependencies import GatewayServices
from brain_v42.codex_gateway.killswitches import KillswitchReader
from brain_v42.config import Settings
from brain_v42.db.engine import dispose_engine, get_session_factory
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.repositories.pg_project_context import PgProjectContextRepo
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.decision_service import DecisionService
from brain_v42.services.entity_maintenance_service import EntityMaintenanceService
from brain_v42.services.feature_service import FeatureService
from brain_v42.services.gpu_embedding_service import GPUEmbeddingService
from brain_v42.services.learning_service import LearningService
from brain_v42.services.project_group_ticket_service import ProjectGroupTicketService
from brain_v42.services.proposal_service import ProposalService
from brain_v42.services.ticket_service import TicketService

_CODEX_CONTRACT_READY = sa.text(
    """
    SELECT
        NOT EXISTS (
            SELECT 1
            FROM unnest(ARRAY[
                'codex_brain_entity_v1',
                'codex_ticket_v1',
                'codex_ticket_message_v1',
                'codex_feature_v1',
                'codex_feature_artifact_v1',
                'codex_dream_run_v1',
                'codex_dream_promotion_v1',
                'codex_ticket_extraction_proposal_v1',
                'codex_roadmap_curation_proposal_v1',
                'codex_consolidation_log_v1'
            ]::text[]) AS required(name)
            WHERE to_regclass('public.' || required.name) IS NULL
        )
        AND NOT EXISTS (
            SELECT 1
            FROM unnest(ARRAY[
                'codex_brain_entity_v1',
                'codex_ticket_v1',
                'codex_ticket_message_v1',
                'codex_feature_v1',
                'codex_feature_artifact_v1',
                'codex_ticket_extraction_proposal_v1',
                'codex_roadmap_curation_proposal_v1'
            ]::text[]) AS scoped(name)
            JOIN pg_class AS contract_view
              ON contract_view.oid = to_regclass('public.' || scoped.name)
            WHERE NOT (
                'security_barrier=true' = ANY(
                    COALESCE(contract_view.reloptions, ARRAY[]::text[])
                )
            )
        )
        AND EXISTS (
            SELECT 1
            FROM pg_trigger
            WHERE tgname = 'trg_feature_artifact_live_target'
              AND tgrelid = to_regclass('public.feature_artifacts')
              AND tgenabled IN ('O', 'A')
              AND NOT tgisinternal
        )
        AND EXISTS (
            SELECT 1
            FROM pg_trigger
            WHERE tgname = 'trg_ticket_participants_immutable'
              AND tgrelid = to_regclass('public.tickets')
              AND tgenabled IN ('O', 'A')
              AND NOT tgisinternal
        )
    """
)


@dataclass(frozen=True, slots=True)
class GatewayRuntime:
    services: GatewayServices
    embedding_service: GPUEmbeddingService
    session_factory: Any

    async def readiness(self) -> None:
        async with self.session_factory() as session:
            compatible = await session.scalar(_CODEX_CONTRACT_READY)
        if compatible is not True:
            raise RuntimeError("Codex gateway database contract is unavailable")

    async def shutdown(self) -> None:
        try:
            await self.embedding_service.close()
        finally:
            await dispose_engine()


def build_production_runtime(settings: Settings) -> GatewayRuntime:
    """Instantiate only the repositories and services required by §3.2."""
    session_factory = get_session_factory()
    embedding_service = GPUEmbeddingService(base_url=settings.embedding_service_url)
    project_context_repo = PgProjectContextRepo(session_factory)
    # project_context_repo wires the fail-closed project-existence guard on
    # create() — see brain_v42.services.project_guard. The proposal-service
    # atomic apply path (session= provided) reuses the caller's transaction
    # for the guard check instead of opening a second connection.
    learning_service = LearningService(
        pg_repo=PgLearningRepo(session_factory),
        embedding_svc=embedding_service,
        project_context_repo=project_context_repo,
    )
    decision_service = DecisionService(
        repo=PgDecisionRepo(session_factory),
        embedding_svc=embedding_service,
        project_context_repo=project_context_repo,
    )
    ticket_service = TicketService(
        repo=PgTicketRepo(session_factory),
        project_context_repo=project_context_repo,
    )
    services = GatewayServices(
        ticket=ProjectGroupTicketService(
            ticket_service,
            session_factory,
            project_group="red",
        ),
        learning=learning_service,
        entity_maintenance=EntityMaintenanceService(session_factory),
        feature=FeatureService(session_factory),
        proposal=ProposalService(session_factory, learning_service, decision_service),
        killswitch=KillswitchReader(settings.brain_codex_gateway_killswitches_path),
    )
    return GatewayRuntime(
        services=services,
        embedding_service=embedding_service,
        session_factory=session_factory,
    )
