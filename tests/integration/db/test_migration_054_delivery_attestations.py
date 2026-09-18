"""Migration 054 — the append-only ledger of issuer-declared delivery facts.

Ticket 04bc1f4a. An attestation is declared once, on the issuer's clock, and
exists in no other column: the digest is computed from the payload, but the
payload itself cannot be recomputed from anything this repository holds. The
downgrade therefore refuses to drop a populated table unless an operator names
the opt-in, and the refusal names the count it would destroy.

A double can show that a CREATE TABLE is emitted; only a real PostgreSQL can
show that the composite keys, the checks and the drop fence behave. The
disposable database of this directory is rebuilt from the chain, so this file
runs the real `alembic upgrade` and `alembic downgrade` binaries.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.disposable_db import repository_head

pytestmark = pytest.mark.integration

_PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: A syntactically valid 64-lowercase-hex digest, reused for the content and
#: context-set digests too: this bench is about the table, not the recipe.
_DIGEST = "f" * 64
_PROJECT = "brain-v42"


async def _attestation(engine: AsyncEngine) -> UUID:
    """Insert the minimal graph the composite foreign keys require, plus one row."""
    ticket_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO tickets (id, kind, title, body, from_project, to_project)"
                " VALUES (:id, 'request', :title, 'migration 054 fixture', :project, :project)"
            ),
            {"id": ticket_id, "title": f"attestation {ticket_id}", "project": _PROJECT},
        )
        # The workflow's own FK to its current revision is DEFERRABLE, but the
        # revision's FK back to the workflow is not: the workflow goes first.
        await conn.execute(
            sa.text(
                "INSERT INTO delivery_workflows (ticket_id, current_revision, context_set_digest)"
                " VALUES (:id, 1, :digest)"
            ),
            {"id": ticket_id, "digest": _DIGEST},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO delivery_contract_revisions (ticket_id, contract_revision,"
                " normalized_contract, content_digest, context_set_digest, author_project)"
                " VALUES (:id, 1, '{}'::jsonb, :digest, :digest, :project)"
            ),
            {"id": ticket_id, "digest": _DIGEST, "project": _PROJECT},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO delivery_attestations (ticket_id, contract_revision, kind, payload,"
                " digest, issuer_project, issuer_identity, idempotency_key, emitted_at)"
                " VALUES (:id, 1, 'gate_passed', '{\"gate\": \"unit\"}'::jsonb, :digest,"
                " :project, 'observer', 'migration-054-key', now())"
            ),
            {"id": ticket_id, "digest": _DIGEST, "project": _PROJECT},
        )
    return ticket_id


async def _cleanup(engine: AsyncEngine, ticket_id: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("DELETE FROM delivery_attestations WHERE ticket_id = :id"), {"id": ticket_id}
        )
        # The workflow and its current revision point at each other with
        # ON DELETE RESTRICT. PostgreSQL validates their final absence only when
        # both deletes belong to one data-modifying statement.
        await conn.execute(
            sa.text(
                "WITH deleted_revisions AS ("
                " DELETE FROM delivery_contract_revisions WHERE ticket_id = :id RETURNING ticket_id"
                ") DELETE FROM delivery_workflows WHERE ticket_id = :id"
            ),
            {"id": ticket_id},
        )
        await conn.execute(sa.text("DELETE FROM tickets WHERE id = :id"), {"id": ticket_id})


async def _head(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        return await conn.scalar(sa.text("SELECT version_num FROM alembic_version"))


class TestTheTableLandsAtHead:
    async def test_the_chain_reaches_the_table_and_accepts_one_row(
        self, engine: AsyncEngine
    ) -> None:
        """The disposable database is built by `alembic upgrade head` already."""
        async with engine.connect() as conn:
            present = await conn.scalar(
                sa.text("SELECT to_regclass('public.delivery_attestations')")
            )
        assert present == "delivery_attestations"
        assert await _head(engine) == repository_head()

        ticket_id = await _attestation(engine)
        try:
            async with engine.connect() as conn:
                stored = await conn.scalar(
                    sa.text("SELECT count(*) FROM delivery_attestations WHERE ticket_id = :id"),
                    {"id": ticket_id},
                )
            assert stored == 1
        finally:
            await _cleanup(engine, ticket_id)


class TestTheDowngradeIsAFence:
    async def test_it_refuses_and_names_the_count_it_would_destroy(
        self, engine: AsyncEngine, migration_downgrade_fence
    ) -> None:
        """A mute downgrade would succeed and look healthy — nothing opposes a DROP.

        What disappears is an issuer-declared fact that exists in no other
        column, so the refusal has to name how many rows hang on the gesture.
        """
        ticket_id = await _attestation(engine)
        migration_downgrade_fence("053")
        db_url = os.environ["BRAIN_V42_TEST_DB_URL"]

        try:
            result = subprocess.run(
                [sys.executable, "-m", "alembic", "downgrade", "053"],
                env={**os.environ, "POSTGRES_URL": db_url},
                cwd=str(_PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=120,
            )

            assert result.returncode != 0, "the downgrade destroyed attestations in silence"
            assert "cannot downgrade 054" in result.stderr
            assert "holds 1 row(s)" in result.stderr, "a refusal without the count leaves no move"
            assert "allow_attestation_downgrade=yes" in result.stderr

            async with engine.connect() as conn:
                still_there = await conn.scalar(
                    sa.text("SELECT count(*) FROM delivery_attestations WHERE ticket_id = :id"),
                    {"id": ticket_id},
                )
            # DERIVED, not "054": a refused downgrade leaves the head where it
            # was, and where it was is wherever the chain currently ends.
            assert await _head(engine) == repository_head()
            assert still_there == 1
        finally:
            await _cleanup(engine, ticket_id)

    async def test_the_named_opt_in_lets_a_deliberate_operator_through(
        self, engine: AsyncEngine, migration_downgrade_fence
    ) -> None:
        """A fence, not a wall."""
        ticket_id = await _attestation(engine)
        migration_downgrade_fence("053")
        db_url = os.environ["BRAIN_V42_TEST_DB_URL"]

        try:
            down = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "alembic",
                    "-x",
                    "allow_attestation_downgrade=yes",
                    "downgrade",
                    "053",
                ],
                env={**os.environ, "POSTGRES_URL": db_url},
                cwd=str(_PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert down.returncode == 0, down.stderr

            async with engine.connect() as conn:
                present = await conn.scalar(
                    sa.text("SELECT to_regclass('public.delivery_attestations')")
                )
            assert await _head(engine) == "053"
            assert present is None
        finally:
            # `head`, never a literal: this bench shares its disposable database
            # with every test that runs after it. The delivered attestations were
            # dropped by the opt-in downgrade, so this only restores the schema.
            restored = subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                env={**os.environ, "POSTGRES_URL": db_url},
                cwd=str(_PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=120,
            )
            # Unchecked, a failed restore would leave the shared disposable database
            # at 053 and every later test would fail with an unrelated error.
            assert restored.returncode == 0, restored.stderr
            await _cleanup(engine, ticket_id)
