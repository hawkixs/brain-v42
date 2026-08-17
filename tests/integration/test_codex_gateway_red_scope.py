"""Real-PostgreSQL proof that every Codex gateway mutation is red-scoped."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import (
    features,
    learnings,
    project_contexts,
    roadmap_curation_proposals,
    ticket_extraction_proposals,
    tickets,
)
from brain_v42.models.ticket import TicketCreate
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.repositories.pg_project_context import PgProjectContextRepo
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.entity_maintenance_service import EntityMaintenanceService
from brain_v42.services.feature_service import FeatureService
from brain_v42.services.learning_service import LearningService
from brain_v42.services.project_group_ticket_service import ProjectGroupTicketService
from brain_v42.services.proposal_service import ProposalNotFoundError, ProposalService
from brain_v42.services.ticket_service import NotAllowedError, TicketNotFoundError, TicketService

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class _ScopedRows:
    red_base: str
    red_child: str
    outside: str
    red_learning: UUID
    outside_learning: UUID
    red_feature: UUID
    outside_feature: UUID
    red_ticket: UUID
    outside_ticket: UUID
    red_ticket_proposal: int
    outside_ticket_proposal: int
    red_roadmap_proposal: int
    outside_roadmap_proposal: int


async def _seed_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> _ScopedRows:
    suffix = uuid4().hex[:8]
    red_base = f"integ-gateway-red-{suffix}"
    red_child = f"{red_base}:worker"
    outside = f"integ-gateway-outside-{suffix}"

    async with session_factory.begin() as session:
        await session.execute(
            project_contexts.insert(),
            [
                {
                    "project_key": red_base,
                    "name": red_base,
                    "description": "gateway red base",
                    "project_group": "red",
                },
                {
                    "project_key": red_child,
                    "name": red_child,
                    "description": "gateway red child",
                    "project_group": None,
                },
                {
                    "project_key": outside,
                    "name": outside,
                    "description": "gateway outside",
                    "project_group": "other",
                },
            ],
        )
        learning_rows = (
            await session.execute(
                learnings.insert()
                .values(
                    [
                        {
                            "topic": "red learning",
                            "insight": "must be mutable",
                            "project_key": red_child,
                            "freshness_status": "stale",
                        },
                        {
                            "topic": "outside learning",
                            "insight": "must stay untouched",
                            "project_key": outside,
                            "freshness_status": "stale",
                        },
                    ]
                )
                .returning(learnings.c.id, learnings.c.project_key)
            )
        ).all()
        learning_ids = {project_key: row_id for row_id, project_key in learning_rows}
        feature_rows = (
            await session.execute(
                features.insert()
                .values(
                    [
                        {
                            "project_key": red_child,
                            "name": "red feature",
                            "description": "must be mutable",
                        },
                        {
                            "project_key": outside,
                            "name": "outside feature",
                            "description": "must stay untouched",
                        },
                    ]
                )
                .returning(features.c.id, features.c.project_key)
            )
        ).all()
        feature_ids = {project_key: row_id for row_id, project_key in feature_rows}
        ticket_rows = (
            await session.execute(
                tickets.insert()
                .values(
                    [
                        {
                            "kind": "request",
                            "title": "red ticket",
                            "body": "must be mutable",
                            "from_project": red_child,
                            "to_project": outside,
                        },
                        {
                            "kind": "request",
                            "title": "outside ticket",
                            "body": "must stay untouched",
                            "from_project": outside,
                            "to_project": outside,
                        },
                    ]
                )
                .returning(tickets.c.id, tickets.c.title)
            )
        ).all()
        ticket_ids = {title: row_id for row_id, title in ticket_rows}
        ticket_proposal_rows = (
            await session.execute(
                ticket_extraction_proposals.insert()
                .values(
                    [
                        {
                            "ticket_id": ticket_ids["red ticket"],
                            "target_type": "learning",
                            "target_project": red_child,
                            "payload": {"topic": "red", "insight": "red"},
                        },
                        {
                            "ticket_id": ticket_ids["outside ticket"],
                            "target_type": "learning",
                            "target_project": outside,
                            "payload": {"topic": "outside", "insight": "outside"},
                        },
                    ]
                )
                .returning(
                    ticket_extraction_proposals.c.id, ticket_extraction_proposals.c.ticket_id
                )
            )
        ).all()
        ticket_proposal_ids = {
            ticket_id: proposal_id for proposal_id, ticket_id in ticket_proposal_rows
        }
        roadmap_rows = (
            await session.execute(
                roadmap_curation_proposals.insert()
                .values(
                    [
                        {
                            "op": "archive",
                            "feature_id": feature_ids[red_child],
                            "payload": {},
                        },
                        {
                            "op": "archive",
                            "feature_id": feature_ids[outside],
                            "payload": {},
                        },
                    ]
                )
                .returning(
                    roadmap_curation_proposals.c.id,
                    roadmap_curation_proposals.c.feature_id,
                )
            )
        ).all()
        roadmap_ids = {feature_id: proposal_id for proposal_id, feature_id in roadmap_rows}

    return _ScopedRows(
        red_base=red_base,
        red_child=red_child,
        outside=outside,
        red_learning=learning_ids[red_child],
        outside_learning=learning_ids[outside],
        red_feature=feature_ids[red_child],
        outside_feature=feature_ids[outside],
        red_ticket=ticket_ids["red ticket"],
        outside_ticket=ticket_ids["outside ticket"],
        red_ticket_proposal=ticket_proposal_ids[ticket_ids["red ticket"]],
        outside_ticket_proposal=ticket_proposal_ids[ticket_ids["outside ticket"]],
        red_roadmap_proposal=roadmap_ids[feature_ids[red_child]],
        outside_roadmap_proposal=roadmap_ids[feature_ids[outside]],
    )


async def test_entity_learning_and_feature_mutations_are_red_scoped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rows = await _seed_scope(session_factory)

    learning_service = LearningService(PgLearningRepo(session_factory))
    maintenance_service = EntityMaintenanceService(session_factory)
    feature_service = FeatureService(session_factory)

    assert await learning_service.validate(rows.red_learning, project_group="red") is not None
    assert await learning_service.validate(rows.outside_learning, project_group="red") is None
    assert (
        await maintenance_service.refresh("learning", rows.red_learning, project_group="red")
        is not None
    )
    assert (
        await maintenance_service.refresh("learning", rows.outside_learning, project_group="red")
        is None
    )
    assert (
        await feature_service.patch(rows.red_feature, pinned=True, project_group="red") is not None
    )
    assert (
        await feature_service.patch(rows.outside_feature, pinned=True, project_group="red") is None
    )

    async with session_factory() as session:
        outside = (
            await session.execute(
                sa.select(
                    learnings.c.validated_at,
                    learnings.c.freshness_status,
                    features.c.pinned,
                )
                .select_from(learnings.join(features, features.c.id == rows.outside_feature))
                .where(learnings.c.id == rows.outside_learning)
            )
        ).one()

    assert outside.validated_at is None
    assert outside.freshness_status == "stale"
    assert outside.pinned is False


async def test_proposal_mutations_hide_outside_ids_and_accept_red_children(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rows = await _seed_scope(session_factory)
    service = ProposalService(session_factory, AsyncMock(), AsyncMock())

    with pytest.raises(ProposalNotFoundError):
        await service.reject_ticket_extraction(
            rows.outside_ticket_proposal,
            project_group="red",
        )
    with pytest.raises(ProposalNotFoundError):
        await service.reject_roadmap_curation(
            rows.outside_roadmap_proposal,
            project_group="red",
        )

    ticket_result = await service.reject_ticket_extraction(
        rows.red_ticket_proposal,
        project_group="red",
    )
    roadmap_result = await service.reject_roadmap_curation(
        rows.red_roadmap_proposal,
        project_group="red",
    )

    assert ticket_result.status == "rejected"
    assert roadmap_result.status == "rejected"


async def test_ticket_gateway_requires_at_least_one_red_participant(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rows = await _seed_scope(session_factory)
    core = TicketService(
        PgTicketRepo(session_factory),
        PgProjectContextRepo(session_factory),
    )
    service = ProjectGroupTicketService(core, session_factory, project_group="red")

    message = await service.reply(rows.red_ticket, rows.red_child, "in scope")
    assert message.body == "in scope"

    with pytest.raises(TicketNotFoundError):
        await service.reply(rows.outside_ticket, rows.outside, "must be hidden")
    with pytest.raises(NotAllowedError, match="project group"):
        await service.create(
            TicketCreate(
                kind="request",
                title="outside create",
                body="must be rejected",
                from_project=rows.outside,
                to_project=rows.outside,
            )
        )

    created = await service.create(
        TicketCreate(
            kind="request",
            title="red create",
            body="one red participant is enough",
            from_project=rows.red_child,
            to_project=rows.outside,
        )
    )
    assert created.from_project == rows.red_child


async def test_ticket_gateway_rejects_an_outside_effective_actor(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rows = await _seed_scope(session_factory)
    core = TicketService(
        PgTicketRepo(session_factory),
        PgProjectContextRepo(session_factory),
    )
    service = ProjectGroupTicketService(core, session_factory, project_group="red")

    with pytest.raises(NotAllowedError, match="project group"):
        await service.create(
            TicketCreate(
                kind="request",
                title="outside impersonation",
                body="the gateway must not create as the outside participant",
                from_project=rows.outside,
                to_project=rows.red_child,
            )
        )
    with pytest.raises(TicketNotFoundError):
        await service.reply(rows.red_ticket, rows.outside, "outside impersonation")
    with pytest.raises(TicketNotFoundError):
        await service.transition(rows.red_ticket, rows.outside, "start")


async def test_scoped_ticket_proposal_apply_rejects_an_outside_target(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rows = await _seed_scope(session_factory)
    async with session_factory.begin() as session:
        proposal_id = (
            await session.execute(
                ticket_extraction_proposals.insert()
                .values(
                    ticket_id=rows.red_ticket,
                    target_type="learning",
                    target_project=rows.outside,
                    payload={
                        "topic": "outside impersonation",
                        "insight": "a scoped apply must not write outside its group",
                    },
                    rationale="security regression proof",
                )
                .returning(ticket_extraction_proposals.c.id)
            )
        ).scalar_one()

    service = ProposalService(
        session_factory,
        LearningService(PgLearningRepo(session_factory)),
        AsyncMock(),
    )

    with pytest.raises(ProposalNotFoundError):
        await service.apply_ticket_extraction(proposal_id, project_group="red")

    async with session_factory() as session:
        created_count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(learnings)
            .where(
                learnings.c.project_key == rows.outside,
                learnings.c.topic == "outside impersonation",
            )
        )
    assert created_count == 0


async def test_scoped_ticket_proposal_accepts_overlapping_red_bases(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rows = await _seed_scope(session_factory)
    async with session_factory.begin() as session:
        await session.execute(
            project_contexts.update()
            .where(project_contexts.c.project_key == rows.red_child)
            .values(project_group="red")
        )

    service = ProposalService(
        session_factory,
        LearningService(PgLearningRepo(session_factory)),
        AsyncMock(),
    )

    result = await service.apply_ticket_extraction(
        rows.red_ticket_proposal,
        project_group="red",
    )

    assert result.status == "applied"
    async with session_factory() as session:
        created_project = await session.scalar(
            sa.select(learnings.c.project_key).where(learnings.c.id == result.entity_id)
        )
    assert created_project == rows.red_child


async def test_ticket_gateway_holds_scope_registry_lock_until_create_finishes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A concurrent group change cannot invalidate a granted write mid-flight."""
    rows = await _seed_scope(session_factory)
    entered = asyncio.Event()
    release = asyncio.Event()
    created = object()
    core = AsyncMock(spec=TicketService)

    async def blocking_create(data: TicketCreate) -> object:
        entered.set()
        await release.wait()
        return created

    core.create.side_effect = blocking_create
    service = ProjectGroupTicketService(core, session_factory, project_group="red")
    payload = TicketCreate(
        kind="request",
        title="registry lock proof",
        body="scope must remain stable",
        from_project=rows.red_child,
        to_project=rows.outside,
    )

    create_task = asyncio.create_task(service.create(payload))
    await asyncio.wait_for(entered.wait(), timeout=5)

    async def move_outside_group() -> None:
        async with session_factory.begin() as session:
            await session.execute(
                project_contexts.update()
                .where(project_contexts.c.project_key == rows.red_base)
                .values(project_group="other")
            )

    registry_update = asyncio.create_task(move_outside_group())
    await asyncio.sleep(0.1)
    assert not registry_update.done()

    release.set()
    assert await asyncio.wait_for(create_task, timeout=5) is created
    await asyncio.wait_for(registry_update, timeout=5)

    with pytest.raises(NotAllowedError, match="project group"):
        await service.create(payload)
    assert core.create.await_count == 1


async def test_colonized_red_base_does_not_authorize_recursive_descendants(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Runtime writes must match the non-recursive scope of migration 036 views."""
    suffix = uuid4().hex[:8]
    rootless_base = f"integ-gateway-colon-{suffix}:red"
    recursive_child = f"{rootless_base}:worker"

    async with session_factory.begin() as session:
        await session.execute(
            project_contexts.insert(),
            [
                {
                    "project_key": rootless_base,
                    "name": rootless_base,
                    "description": "explicit colonized red key",
                    "project_group": "red",
                },
                {
                    "project_key": recursive_child,
                    "name": recursive_child,
                    "description": "must not inherit recursively",
                    "project_group": None,
                },
            ],
        )
        feature_rows = (
            await session.execute(
                features.insert()
                .values(
                    [
                        {
                            "project_key": rootless_base,
                            "name": "explicit red feature",
                            "description": "exact match remains visible",
                        },
                        {
                            "project_key": recursive_child,
                            "name": "recursive child feature",
                            "description": "must stay outside red",
                        },
                    ]
                )
                .returning(features.c.id, features.c.project_key)
            )
        ).all()
    ids = {project_key: feature_id for feature_id, project_key in feature_rows}

    service = FeatureService(session_factory)
    assert await service.patch(ids[rootless_base], pinned=True, project_group="red") is not None
    assert await service.patch(ids[recursive_child], pinned=True, project_group="red") is None

    async with session_factory() as session:
        visible_ids = set(
            (
                await session.execute(
                    sa.text("SELECT id FROM codex_feature_v1 WHERE id = ANY(CAST(:ids AS uuid[]))"),
                    {"ids": list(ids.values())},
                )
            ).scalars()
        )

    assert visible_ids == {ids[rootless_base]}
