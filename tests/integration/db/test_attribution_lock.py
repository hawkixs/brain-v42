"""Exercise the attribution lock through the real lifecycle service and PostgreSQL."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from brain_v42.config import get_settings
from brain_v42.credentials.reasons import refusal_counts, reset_refusal_counts
from brain_v42.db.focus_slots import record_slot_history
from brain_v42.db.tables import (
    brain_session_artifacts,
    brain_session_connections,
    brain_sessions,
    focus_slots,
    project_contexts,
)
from brain_v42.models.brain_session import BrainSessionIdentityConflictError
from brain_v42.provenance import set_current_actor, set_current_principal, set_current_transport
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo
from brain_v42.services.brain_session_service import BrainSessionService

pytestmark = pytest.mark.integration


@dataclass
class AttributionCase:
    factory: async_sessionmaker[AsyncSession]
    repo: PgBrainSessionRepo
    service: BrainSessionService
    project: str
    slot: UUID

    async def owner(self, session_id: UUID) -> str | None:
        async with self.factory() as session:
            return await session.scalar(
                sa.select(brain_sessions.c.opener_client_id).where(
                    brain_sessions.c.id == session_id
                )
            )

    async def snapshot(self, session_id: UUID) -> dict[str, object]:
        """Compare all mutable rows, not just the error returned to the caller."""
        async with self.factory() as session:
            row = (
                (
                    await session.execute(
                        sa.select(brain_sessions).where(brain_sessions.c.id == session_id)
                    )
                )
                .mappings()
                .one()
            )
            counts = {}
            for table in (brain_session_connections, brain_session_artifacts):
                counts[table.name] = await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(table)
                    .where(table.c.session_id == session_id)
                )
            return {"row": dict(row), "counts": counts}


@pytest_asyncio.fixture(name="attribution_case")
async def attribution_case(
    private_head_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AttributionCase]:
    monkeypatch.setenv("BRAIN_SESSION_DERIVED_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("BRAIN_ELEVATABLE_CLIENT_IDS", "workstation-claude")
    get_settings.cache_clear()
    factory = async_sessionmaker(private_head_engine, expire_on_commit=False)
    project, slot = f"attribution-{uuid4().hex[:12]}", uuid4()
    async with factory.begin() as session:
        await session.execute(
            project_contexts.insert().values(
                project_key=project,
                name="Attribution tests",
                description="Private database fixture",
                current_focus="Keep the complete focus",
            )
        )
        slot_row = (
            (
                await session.execute(
                    focus_slots.insert()
                    .values(
                        id=slot,
                        project_key=project,
                        title="Attribution",
                        body="Keep the complete focus",
                    )
                    .returning(focus_slots.c.revision, focus_slots.c.body)
                )
            )
            .mappings()
            .one()
        )
        await record_slot_history(
            session,
            slot_id=slot,
            revision=slot_row["revision"],
            body=slot_row["body"],
            source="slot_open",
        )
    repo = PgBrainSessionRepo(factory)
    set_current_actor("misleading-declared-actor")
    set_current_transport(uuid4().hex)
    set_current_principal(None)
    reset_refusal_counts()
    try:
        yield AttributionCase(factory, repo, BrainSessionService(repo), project, slot)
    finally:
        set_current_actor("unknown")
        set_current_transport(None)
        set_current_principal(None)
        reset_refusal_counts()
        get_settings.cache_clear()
        # Slot rows are append-only: the private module database owns cleanup.


@pytest.mark.parametrize("command", ["bind", "resume"])
async def test_foreign_client_is_refused_without_any_write(
    attribution_case: AttributionCase, command: str
) -> None:
    case = attribution_case
    set_current_principal("workstation-claude")
    started = await case.service.start(case.project, uuid4().hex)
    before = await case.snapshot(started.session.id)
    set_current_principal("red-rail")
    reset_refusal_counts()
    with capture_logs() as events, pytest.raises(BrainSessionIdentityConflictError) as exc:
        if command == "bind":
            await case.service.bind(started.session.id, started.session.client_key, case.slot)
        else:
            await case.service.resume(started.session.id, started.session.client_key)
    assert exc.value.code == "foreign_client_attach"
    assert await case.snapshot(started.session.id) == before
    refused = [event for event in events if event["event"] == "mcp_auth.refused"]
    assert len(refused) == 1
    assert refused[0]["status"] == 403
    assert refused[0]["requesting_client_id"] == "red-rail"
    assert refused[0]["owner_client_id"] == "workstation-claude"
    assert refused[0]["session_id"] == str(started.session.id)
    assert refusal_counts()["foreign_client_attach"] == 1


async def test_allowlisted_client_claims_pre_cutover_session(
    attribution_case: AttributionCase,
) -> None:
    case = attribution_case
    started = await case.repo.start(case.project, uuid4().hex)
    assert await case.owner(started.session.id) is None
    set_current_principal("workstation-claude")
    await case.service.resume(started.session.id, started.session.client_key)
    assert await case.owner(started.session.id) == "workstation-claude"


@pytest.mark.parametrize("command", ["bind", "resume"])
async def test_foreign_client_cannot_claim_unowned_session(
    attribution_case: AttributionCase, command: str
) -> None:
    case = attribution_case
    started = await case.repo.start(case.project, uuid4().hex)
    before = await case.snapshot(started.session.id)
    set_current_principal("red-rail")
    with pytest.raises(BrainSessionIdentityConflictError) as exc:
        if command == "bind":
            await case.service.bind(started.session.id, started.session.client_key, case.slot)
        else:
            await case.service.resume(started.session.id, started.session.client_key)
    assert exc.value.code == "foreign_client_attach"
    assert await case.owner(started.session.id) is None
    assert await case.snapshot(started.session.id) == before


async def test_start_writes_verified_opener(attribution_case: AttributionCase) -> None:
    set_current_principal("workstation-claude")
    started = await attribution_case.service.start(attribution_case.project, uuid4().hex)
    assert await attribution_case.owner(started.session.id) == "workstation-claude"


@pytest.mark.parametrize("bound", [False, True])
async def test_relay_writes_successor_opener_only(
    attribution_case: AttributionCase, bound: bool
) -> None:
    case = attribution_case
    started = await case.repo.start(case.project, uuid4().hex)
    if bound:
        await case.service.bind(started.session.id, started.session.client_key, case.slot)
    # No connection: isolate opener insertion from the first attributed attach's claim.
    set_current_transport(None)
    set_current_principal("workstation-claude")
    revision = {"expected_slot_revision": 0} if bound else {"expected_focus_revision": 0}
    relayed = await case.service.relay(
        started.session.id,
        started.session.client_key,
        summary="Carry over the focus",
        handover="Keep the complete focus",
        new_client_key=uuid4().hex,
        initiator="operator",
        nothing_to_capture_reason="No artifacts created",
        **revision,
    )
    assert await case.owner(started.session.id) is None
    assert await case.owner(relayed.session.id) == "workstation-claude"


async def test_agent_trace_is_never_locked(attribution_case: AttributionCase) -> None:
    case = attribution_case
    connection_id = uuid4().hex
    tracer_id = await case.repo.auto_open(
        SimpleNamespace(
            project_key=case.project,
            connection_id=connection_id,
            started_by_actor="test-agent",
            nature="agent",
            intent=None,
        )
    )
    assert tracer_id is not None
    async with case.factory() as session:
        key = await session.scalar(
            sa.select(brain_sessions.c.client_key).where(brain_sessions.c.id == tracer_id)
        )
    set_current_principal("red-rail")
    await case.service.resume(tracer_id, key)
    assert await case.owner(tracer_id) is None
    assert refusal_counts().get("foreign_client_attach", 0) == 0


async def test_shared_token_keeps_null_attribution(attribution_case: AttributionCase) -> None:
    case = attribution_case
    started = await case.service.start(case.project, uuid4().hex)
    await case.service.bind(started.session.id, started.session.client_key, case.slot)
    await case.service.resume(started.session.id, started.session.client_key)
    assert await case.owner(started.session.id) is None
    async with case.factory() as session:
        rows = (
            (
                await session.execute(
                    sa.select(brain_session_connections.c.client_id).where(
                        brain_session_connections.c.session_id == started.session.id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows and all(client_id is None for client_id in rows)
    assert not refusal_counts()
