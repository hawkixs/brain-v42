"""Schema and runtime interlock contracts for graph recovery migration 035."""

from __future__ import annotations

import inspect
from pathlib import Path

import sqlalchemy as sa

from brain_v42.db.tables import graph_projection_leases
from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo

ROOT = Path(__file__).parents[3]
MIGRATION = ROOT / "alembic" / "versions" / "035_graph_projection_recovery.py"


def test_migration_adds_a_durable_recovery_interlock() -> None:
    source = MIGRATION.read_text()

    assert 'revision = "035"' in source
    assert 'down_revision = "034"' in source
    assert "ADD COLUMN recovery_id UUID" in source
    assert "ADD COLUMN recovery_phase VARCHAR(16)" in source
    assert "ADD COLUMN last_completed_recovery_id UUID" in source
    assert "graph_projection_leases_recovery_state_valid" in source
    assert "prepared" in source
    assert "neo_ready" in source
    assert "idle" in source
    assert "owner IS NOT NULL" in source
    assert "leased_until IS NOT NULL" in source
    assert "neo4j_armed_generation IS NULL" in source
    assert "neo4j_armed_generation = generation" in source
    assert "neo4j_armed_generation IS NOT NULL" in source
    assert "recovery_id IS DISTINCT FROM last_completed_recovery_id" in source


def test_table_metadata_exposes_the_recovery_interlock() -> None:
    assert graph_projection_leases.c.recovery_id.nullable is True
    assert graph_projection_leases.c.recovery_phase.nullable is False
    assert graph_projection_leases.c.last_completed_recovery_id.nullable is True
    assert str(graph_projection_leases.c.recovery_phase.server_default.arg) == "'idle'"

    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in graph_projection_leases.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert "graph_projection_leases_recovery_state_valid" in checks
    recovery_check = checks["graph_projection_leases_recovery_state_valid"]
    assert "owner IS NOT NULL" in recovery_check
    assert "leased_until IS NOT NULL" in recovery_check
    assert "recovery_phase = 'prepared'" in recovery_check
    assert "neo4j_armed_generation IS NULL" in recovery_check
    assert "recovery_phase = 'neo_ready'" in recovery_check
    assert "neo4j_armed_generation = generation" in recovery_check
    assert "neo4j_armed_generation IS NOT NULL" in recovery_check
    assert "recovery_id IS DISTINCT FROM last_completed_recovery_id" in recovery_check


def test_every_normal_runtime_lease_path_refuses_active_recovery() -> None:
    for method_name in (
        "acquire_leadership",
        "arm_leadership",
        "release_leadership",
        "claim_pending",
        "renew_claim",
        "mark_delivered",
        "mark_failed",
    ):
        source = inspect.getsource(getattr(PgGraphLedgerRepo, method_name))
        assert "recovery_id IS NULL" in source, method_name


def test_schema_readiness_requires_the_recovery_columns() -> None:
    source = inspect.getsource(PgGraphLedgerRepo.assert_schema_ready)

    assert "recovery_id" in source
    assert "recovery_phase" in source
    assert "pg_constraint" in source
    assert "graph_projection_leases_recovery_state_valid" in source
    assert "migration 035" in source
