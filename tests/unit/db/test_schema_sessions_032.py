"""Contract tests for persistent concurrent Brain sessions (migration 032)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_session_lifecycle_modules_exist() -> None:
    expected_modules = (
        "brain_v42.models.brain_session",
        "brain_v42.repositories.pg_brain_session",
        "brain_v42.services.brain_session_service",
    )

    missing = [name for name in expected_modules if importlib.util.find_spec(name) is None]

    assert missing == []


def test_brain_sessions_table_contract() -> None:
    from brain_v42.db import tables

    assert hasattr(tables, "brain_sessions")
    session_table = tables.brain_sessions
    assert session_table.name == "brain_sessions"
    assert {
        "id",
        "project_key",
        "client_key",
        "status",
        "started_focus",
        "started_focus_revision",
        "summary",
        "next_focus",
        "captured_knowledge_ids",
        "nothing_to_capture_reason",
        "abandonment_reason",
        "end_expected_focus_revision",
        "focus_outcome",
        "focus_at_end",
        "focus_revision_at_end",
        "started_at",
        "last_heartbeat_at",
        "ended_at",
        "updated_at",
        # Migration 046 — identité de session et nature. Ensemble FERMÉ : c'est
        # ce qui rend tout ajout de colonne délibéré plutôt que silencieux.
        "started_by_actor",
        "last_observed_at",
        "intent",
        "nature",
        "connection_id",
    } == set(session_table.c.keys())

    project_fks = list(session_table.c.project_key.foreign_keys)
    assert len(project_fks) == 1
    assert project_fks[0].column.table.name == "project_contexts"
    assert project_fks[0].column.name == "project_key"
    assert project_fks[0].ondelete == "RESTRICT"

    unique_constraints = {
        constraint.name: constraint
        for constraint in session_table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert "uq_brain_sessions_project_client" in unique_constraints
    assert {
        column.name for column in unique_constraints["uq_brain_sessions_project_client"].columns
    } == {
        "project_key",
        "client_key",
    }

    check_names = {
        constraint.name
        for constraint in session_table.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert {
        "brain_sessions_status_valid",
        "brain_sessions_client_key_nonblank",
        "brain_sessions_terminal_state_valid",
    } <= check_names

    terminal_constraint = next(
        constraint
        for constraint in session_table.constraints
        if isinstance(constraint, sa.CheckConstraint)
        and constraint.name == "brain_sessions_terminal_state_valid"
    )
    terminal_sql = str(terminal_constraint.sqltext).lower()
    assert "summary is not null" in terminal_sql
    assert "next_focus is not null" in terminal_sql
    assert "nothing_to_capture_reason is not null" in terminal_sql
    assert "abandonment_reason is not null" in terminal_sql

    index_names = {index.name for index in session_table.indexes}
    assert "idx_brain_sessions_project_status_started" in index_names

    assert isinstance(tables.project_contexts.c.focus_revision.type, sa.BigInteger)
    assert tables.project_contexts.c.focus_revision.nullable is False


def test_brain_sessions_table_is_reexported_from_db_package() -> None:
    from brain_v42.db import brain_sessions

    assert brain_sessions.name == "brain_sessions"


def test_migration_032_is_current_chain_link_and_symmetric() -> None:
    migration_path = PROJECT_ROOT / "alembic" / "versions" / "032_brain_sessions.py"
    assert migration_path.exists()

    source = migration_path.read_text()
    assert 'revision = "032"' in source
    assert 'down_revision = "031"' in source
    assert "CREATE TABLE brain_sessions" in source
    assert "REFERENCES project_contexts(project_key) ON DELETE RESTRICT" in source
    assert "started_focus TEXT" in source
    assert "started_focus_revision BIGINT" in source
    assert "ADD COLUMN focus_revision BIGINT" in source
    assert "CREATE TRIGGER project_contexts_focus_revision_trigger" in source
    assert "DROP COLUMN IF EXISTS focus_revision" in source
    assert "uq_brain_sessions_project_client" in source
    assert "brain_sessions_terminal_state_valid" in source
    assert "summary IS NOT NULL" in source
    assert "next_focus IS NOT NULL" in source
    assert "nothing_to_capture_reason IS NOT NULL" in source
    assert "abandonment_reason IS NOT NULL" in source
    assert "DROP TABLE IF EXISTS brain_sessions" in source


def test_capture_array_contract_rejects_null_elements_and_unbounded_payloads() -> None:
    from brain_v42.db import tables

    constraints = {
        constraint.name: str(constraint.sqltext).lower()
        for constraint in tables.brain_sessions.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    capture_sql = constraints["brain_sessions_capture_ids_valid"]

    assert "array_position(captured_knowledge_ids, null) is null" in capture_sql
    assert "cardinality(captured_knowledge_ids) <= 100" in capture_sql

    migration_source = (
        (PROJECT_ROOT / "alembic" / "versions" / "032_brain_sessions.py").read_text().lower()
    )
    assert "constraint brain_sessions_capture_ids_valid" in migration_source
    assert "array_position(captured_knowledge_ids, null) is null" in migration_source
    assert "cardinality(captured_knowledge_ids) <= 100" in migration_source
