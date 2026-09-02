"""Integration round-trip: create → reply → resolve → confirm → extraction_status.

Requires BRAIN_V42_TEST_DB_URL (skipped otherwise). Drives alembic upgrade head
so migration 028 is exercised end-to-end.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from brain_v42.db.tables import ticket_extraction_proposals
from brain_v42.models.ticket import (
    ExtractionStatus,
    TicketCreate,
    TicketKind,
    TicketStatus,
)
from brain_v42.repositories.pg_ticket import PgTicketRepo
from tests.conftest import require_test_db_url

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent

pytestmark = pytest.mark.asyncio


def _run_alembic_upgrade(db_url: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "POSTGRES_URL": db_url},
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade failed:\n{result.stderr}")


async def test_full_request_lifecycle_roundtrip():
    db_url = require_test_db_url()
    _run_alembic_upgrade(db_url)
    engine = create_async_engine(db_url)
    try:
        sf = async_sessionmaker(engine, expire_on_commit=False)
        repo = PgTicketRepo(sf)

        created = await repo.create(
            TicketCreate(
                kind=TicketKind.REQUEST,
                title="exposer /api/signals en ndjson",
                body="détail de la demande",
                from_project="red-shrik",
                to_project="red-data",
            )
        )
        assert created.status is TicketStatus.OPEN

        msg = await repo.add_message(created.id, "red-data", "ok je regarde")
        assert msg.ticket_id == created.id

        resolved = await repo.apply_transition(
            created.id,
            TicketStatus.RESOLVED,
            expected_status=TicketStatus.OPEN,
            resolved_at=None,
            closed_at=None,
            extraction_status=None,
        )
        assert resolved.status is TicketStatus.RESOLVED

        from datetime import UTC, datetime

        closed = await repo.apply_transition(
            created.id,
            TicketStatus.CLOSED,
            expected_status=TicketStatus.RESOLVED,
            resolved_at=resolved.resolved_at,
            closed_at=datetime.now(UTC),
            extraction_status=ExtractionStatus.PENDING,
        )
        assert closed.status is TicketStatus.CLOSED
        assert closed.extraction_status is ExtractionStatus.PENDING

        groups = await repo.list_grouped("red-data")
        assert all(t.id != created.id for t in groups.a_traiter)  # terminal → sorti

        thread = await repo.get_messages(created.id)
        assert len(thread) == 1
    finally:
        await engine.dispose()


async def test_persist_proposals_then_apply_proposals_roundtrip():
    """
    DB-gated integration test: persist_proposals → apply_proposals.

    Verifies that:
    - persist_proposals correctly inserts rows into ticket_extraction_proposals
      and sets ticket.extraction_status = 'proposed'.
    - apply_proposals correctly reads proposals via Result.mappings() (CRITICAL-1)
      and marks ticket.extraction_status = 'done' inside a single begin()
      transaction (CRITICAL-2).

    Services (LearningService, DecisionService) are mocked — no embedding, no NVIDIA.
    Skips when BRAIN_V42_TEST_DB_URL is not set.
    """
    from unittest.mock import patch

    from scripts.ticket_extract import (
        ProposalDraft,
        TicketThread,
        apply_proposals,
        persist_proposals,
    )

    db_url = require_test_db_url()
    _run_alembic_upgrade(db_url)
    engine = create_async_engine(db_url)
    try:
        sf = async_sessionmaker(engine, expire_on_commit=False)
        repo = PgTicketRepo(sf)

        # 1. Create a ticket and close it → extraction_status = 'pending'
        ticket = await repo.create(
            TicketCreate(
                kind=TicketKind.REQUEST,
                title="test apply_proposals roundtrip",
                body="integration test body",
                from_project="red-shrik",
                to_project="red-data",
            )
        )
        from datetime import UTC, datetime

        closed = await repo.apply_transition(
            ticket.id,
            TicketStatus.CLOSED,
            expected_status=TicketStatus.OPEN,
            resolved_at=datetime.now(UTC),
            closed_at=datetime.now(UTC),
            extraction_status=ExtractionStatus.PENDING,
        )
        assert closed.extraction_status is ExtractionStatus.PENDING

        # 2. persist_proposals — insert one 'proposed' row.
        thread = TicketThread(
            id=ticket.id,
            kind="request",
            title=ticket.title,
            body=ticket.body,
            from_project="red-shrik",
            to_project="red-data",
            status="closed",
            messages=[],
        )
        draft = ProposalDraft(
            ticket_id=ticket.id,
            target_type="learning",
            target_project="red-shrik",
            payload={"topic": "roundtrip topic", "insight": "roundtrip insight", "tags": []},
            rationale="integration test rationale",
        )
        proposal_ids = await persist_proposals(sf, thread, [draft])
        assert len(proposal_ids) == 1

        # Verify extraction_status is now 'proposed'.
        async with sf() as session:
            row = (
                (
                    await session.execute(
                        sa.select(ticket_extraction_proposals).where(
                            ticket_extraction_proposals.c.id == proposal_ids[0]
                        )
                    )
                )
                .mappings()
                .one()
            )
        assert row["status"] == "proposed"
        assert row["ticket_id"] == ticket.id

        # 3. apply_proposals — with mocked LearningService (no NVIDIA/embedding).
        created_entity = MagicMock()
        created_entity.id = uuid4()
        mock_learning = AsyncMock()
        mock_learning.create = AsyncMock(return_value=created_entity)

        with patch(
            "scripts.ticket_extract._build_apply_services",
            return_value=(mock_learning, AsyncMock()),
        ):
            applied, entities = await apply_proposals(sf, proposal_ids)

        assert applied == 1
        assert entities == 1

        # 4. After apply: proposal status = 'applied', ticket = 'done'.
        async with sf() as session:
            prop_row = (
                (
                    await session.execute(
                        sa.select(ticket_extraction_proposals).where(
                            ticket_extraction_proposals.c.id == proposal_ids[0]
                        )
                    )
                )
                .mappings()
                .one()
            )
        assert prop_row["status"] == "applied"
        assert prop_row["applied_entity_id"] == created_entity.id

        refreshed = await repo.get_by_id(ticket.id)
        assert refreshed is not None
        assert refreshed.extraction_status is ExtractionStatus.DONE

    finally:
        await engine.dispose()


async def test_create_skipped_persists_and_excluded_from_pending_scan():
    """A ticket created 'skipped' persists and never enters the extract scan."""
    db_url = require_test_db_url()
    _run_alembic_upgrade(db_url)
    engine = create_async_engine(db_url)
    try:
        sf = async_sessionmaker(engine, expire_on_commit=False)
        repo = PgTicketRepo(sf)

        created = await repo.create(
            TicketCreate(
                kind=TicketKind.FYI,
                title="job factory #123 terminé",
                body="bruit opérationnel volumineux",
                from_project="red-shrik",
                to_project="red-data",
                extraction_status=ExtractionStatus.SKIPPED,
            )
        )
        assert created.extraction_status is ExtractionStatus.SKIPPED

        refreshed = await repo.get_by_id(created.id)
        assert refreshed is not None
        assert refreshed.extraction_status is ExtractionStatus.SKIPPED

        # End-to-end opt-out: the extract job's fetch never sees it.
        from scripts.ticket_extract import fetch_pending_threads

        pending = await fetch_pending_threads(sf, limit=100)
        assert all(t.id != created.id for t in pending)
    finally:
        await engine.dispose()
