"""Schema contracts for the projection fencing protocol (migration 034)."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_projection_lease_table_and_claim_generation_are_declared() -> None:
    from brain_v42.db import graph_projection_leases
    from brain_v42.db.tables import graph_outbox

    assert graph_projection_leases.c.slot.primary_key
    assert graph_projection_leases.c.protocol_version.nullable is False
    assert graph_projection_leases.c.generation.nullable is False
    assert graph_projection_leases.c.owner.nullable is True
    assert graph_projection_leases.c.leased_until.nullable is True
    assert graph_projection_leases.c.neo4j_armed_generation.nullable is True

    assert graph_outbox.c.lease_generation.nullable is True
    assert graph_outbox.c.claim_version.nullable is False
    assert str(graph_outbox.c.claim_version.server_default.arg) == "0"

    checks = {
        constraint.name
        for constraint in graph_projection_leases.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert "graph_projection_leases_protocol_valid" in checks
    assert "graph_projection_leases_armed_generation_valid" in checks


def test_migration_034_is_additive_reversible_and_initializes_protocol_v2() -> None:
    migration_path = PROJECT_ROOT / "alembic" / "versions" / "034_graph_projection_fencing.py"

    assert migration_path.exists()
    source = " ".join(migration_path.read_text().lower().split())
    assert 'revision = "034"' in source
    assert 'down_revision = "033"' in source
    assert "create table graph_projection_leases" in source
    assert "add column lease_generation" in source
    assert "add column claim_version" in source
    assert "insert into graph_projection_leases" in source
    assert "'neo4j', 2, 0, 0" in source
    assert "drop table if exists graph_projection_leases" in source
    assert "drop column if exists claim_version" in source
    assert "drop column if exists lease_generation" in source


def test_projection_schema_readiness_requires_fencing_v2() -> None:
    repo_path = PROJECT_ROOT / "src" / "brain_v42" / "repositories" / "pg_graph_ledger.py"
    source = " ".join(repo_path.read_text().lower().split())

    assert "to_regclass('public.graph_projection_leases') is not null" in source
    assert "protocol_version = 2" in source
    assert "neo4j_armed_generation" in source
    assert "lease_generation" in source
    assert "claim_version" in source
