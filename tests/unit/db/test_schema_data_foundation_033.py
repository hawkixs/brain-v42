"""Contract tests for the canonical graph data foundation (migration 033)."""

from __future__ import annotations

import re
from pathlib import Path

import sqlalchemy as sa

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _constraints(table: sa.Table, kind: type[sa.schema.Constraint]) -> dict[str | None, object]:
    return {
        constraint.name: constraint
        for constraint in table.constraints
        if isinstance(constraint, kind)
    }


def test_data_foundation_tables_are_declared_and_reexported() -> None:
    from brain_v42.db import (
        brain_entities,
        entity_relations,
        graph_outbox,
        project_aliases,
        projects,
    )
    from brain_v42.db.tables import METADATA

    assert {
        "projects",
        "project_aliases",
        "brain_entities",
        "entity_relations",
        "graph_outbox",
    } <= set(METADATA.tables)
    assert projects.name == "projects"
    assert project_aliases.name == "project_aliases"
    assert brain_entities.name == "brain_entities"
    assert entity_relations.name == "entity_relations"
    assert graph_outbox.name == "graph_outbox"


def test_projects_separates_identity_from_optional_context() -> None:
    from brain_v42.db.tables import project_aliases, projects

    assert projects.c.project_key.primary_key
    assert projects.c.registry_status.nullable is False
    assert projects.c.source.nullable is False

    alias_fk = next(iter(project_aliases.c.project_key.foreign_keys))
    assert alias_fk.column.table.name == "projects"
    assert alias_fk.column.name == "project_key"
    assert alias_fk.ondelete == "CASCADE"
    check_names = _constraints(projects, sa.CheckConstraint)
    assert "projects_key_format_valid" in check_names


def test_brain_entities_has_typed_identity_project_fk_and_explicit_scope() -> None:
    from brain_v42.db.tables import brain_entities

    assert brain_entities.c.id.primary_key
    assert brain_entities.c.entity_type.nullable is False
    assert brain_entities.c.entity_key.nullable is False
    assert brain_entities.c.scope_kind.nullable is False
    assert brain_entities.c.lifecycle.nullable is False

    project_fk = next(iter(brain_entities.c.project_key.foreign_keys))
    assert project_fk.column.table.name == "projects"
    assert project_fk.ondelete == "RESTRICT"

    unique_names = _constraints(brain_entities, sa.UniqueConstraint)
    assert "uq_brain_entities_type_key" in unique_names
    check_names = _constraints(brain_entities, sa.CheckConstraint)
    assert "brain_entities_scope_valid" in check_names
    assert "brain_entities_lifecycle_valid" in check_names


def test_entity_relations_has_endpoint_fks_provenance_and_idempotency() -> None:
    from brain_v42.db.tables import entity_relations

    for column_name in ("source_entity_id", "target_entity_id"):
        endpoint_fk = next(iter(entity_relations.c[column_name].foreign_keys))
        assert endpoint_fk.column.table.name == "brain_entities"
        assert endpoint_fk.column.name == "id"
        assert endpoint_fk.ondelete == "RESTRICT"

    assert entity_relations.c.origin.nullable is False
    assert entity_relations.c.revision.nullable is False
    assert entity_relations.c.lifecycle.nullable is False

    unique_names = _constraints(entity_relations, sa.UniqueConstraint)
    assert "uq_entity_relations_endpoints_type" in unique_names
    check_names = _constraints(entity_relations, sa.CheckConstraint)
    assert {
        "entity_relations_no_self_loop",
        "entity_relations_type_valid",
        "entity_relations_confidence_valid",
        "entity_relations_lifecycle_valid",
    } <= set(check_names)


def test_graph_outbox_has_retry_state_and_pending_index() -> None:
    from brain_v42.db.tables import graph_outbox

    relation_fk = next(iter(graph_outbox.c.relation_id.foreign_keys))
    assert relation_fk.column.table.name == "entity_relations"
    assert relation_fk.ondelete == "CASCADE"
    assert graph_outbox.c.event_id.unique is True
    assert graph_outbox.c.attempt_count.nullable is False
    assert graph_outbox.c.available_at.nullable is False
    assert "last_error" not in graph_outbox.c
    assert "last_error_code" in graph_outbox.c

    unique_names = _constraints(graph_outbox, sa.UniqueConstraint)
    assert "uq_graph_outbox_relation_revision" in unique_names
    assert "idx_graph_outbox_pending" in {index.name for index in graph_outbox.indexes}


def test_migration_033_extends_032_and_has_symmetric_downgrade() -> None:
    migration_path = PROJECT_ROOT / "alembic" / "versions" / "033_graph_relation_ledger.py"
    assert migration_path.exists()

    source = migration_path.read_text()
    assert 'revision = "033"' in source
    assert 'down_revision = "032"' in source
    for table_name in (
        "projects",
        "project_aliases",
        "brain_entities",
        "entity_relations",
        "graph_outbox",
    ):
        assert f"CREATE TABLE {table_name}" in source
        assert f"DROP TABLE IF EXISTS {table_name}" in source

    assert "backfill_projects" in source
    assert "backfill_brain_entities" in source
    assert "backfill_pg_relations" in source
    assert "sync_brain_entity_registry" in source


def test_migration_locks_all_sources_before_backfill_and_trigger_installation() -> None:
    migration_path = PROJECT_ROOT / "alembic" / "versions" / "033_graph_relation_ledger.py"
    source = " ".join(migration_path.read_text().lower().split())
    upgrade = source.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]

    assert "lock table" in source
    assert "in share row exclusive mode" in source
    assert upgrade.index("_lock_graph_source_tables()") < upgrade.index("_create_tables()")
    for table_name in (
        "project_contexts",
        "decisions",
        "learnings",
        "snippets",
        "runbooks",
        "adrs",
        "features",
        "indexed_plans",
        "indexed_plan_chunks",
        "gitlab_events",
        "brain_sessions",
        "search_log",
        "tickets",
        "ticket_messages",
        "ticket_extraction_proposals",
    ):
        assert (
            table_name
            in source.split("def _lock_graph_source_tables", 1)[1].split("def _create_tables", 1)[0]
        )


def test_integration_cleanup_covers_every_project_registry_source() -> None:
    cleanup_path = PROJECT_ROOT / "tests" / "integration" / "conftest.py"
    source = cleanup_path.read_text()

    for table_name in (
        "indexed_plans",
        "indexed_plan_chunks",
        "gitlab_events",
        "brain_sessions",
        "search_log",
        "tickets",
        "ticket_messages",
        "ticket_extraction_proposals",
    ):
        assert table_name in source


def test_migration_keeps_archived_lineage_projectable_but_deletes_hard_tombstones() -> None:
    migration_path = PROJECT_ROOT / "alembic" / "versions" / "033_graph_relation_ledger.py"
    source = migration_path.read_text().lower()

    assert "update entity_relations" in source
    assert "endpoint.lifecycle = 'deleted'" in source
    assert "when lifecycle = 'deleted' then 'delete_entity' else 'upsert_entity'" in source
    assert "where relation.lifecycle = 'active'" not in source

    relation_backfill = source.split("def backfill_pg_relations", 1)[1].split(
        "def _create_relation_lifecycle_sync", 1
    )[0]
    assert relation_backfill.rfind("update entity_relations") > relation_backfill.rfind(
        "'merged_into'"
    )


def test_registry_trigger_propagates_entity_lifecycle_to_relation_events() -> None:
    migration_path = PROJECT_ROOT / "alembic" / "versions" / "033_graph_relation_ledger.py"
    source = migration_path.read_text().lower()

    assert "create function sync_entity_relation_lifecycle" in source
    assert source.count("perform sync_entity_relation_lifecycle") == 2
    assert "'delete_relation'" in source
    assert "drop function if exists sync_entity_relation_lifecycle" in source


def test_registry_lifecycle_sync_never_resurrects_explicitly_deleted_relation() -> None:
    migration_path = PROJECT_ROOT / "alembic" / "versions" / "033_graph_relation_ledger.py"
    source = " ".join(migration_path.read_text().lower().split())

    assert "when relation.lifecycle = 'deleted' then 'deleted'" in source


def test_registry_lifecycle_sync_serializes_incident_relations_before_recompute() -> None:
    migration_path = PROJECT_ROOT / "alembic" / "versions" / "033_graph_relation_ledger.py"
    source = " ".join(migration_path.read_text().lower().split())
    lifecycle_sync = source.split("create function sync_entity_relation_lifecycle", 1)[1].split(
        "def _create_project_membership_sync", 1
    )[0]

    lock = "hashtextextended(locked_relation_id::text"
    assert "order by relation.id" in lifecycle_sync
    assert "pg_advisory_xact_lock" in lifecycle_sync
    assert lock in lifecycle_sync
    assert lifecycle_sync.index(lock) < lifecycle_sync.index("with relation_states as")


def test_project_registry_reuses_project_context_identity_when_available() -> None:
    migration_path = PROJECT_ROOT / "alembic" / "versions" / "033_graph_relation_ledger.py"
    source = " ".join(migration_path.read_text().lower().split())

    assert "left join project_contexts" in source
    assert "coalesce(project_context.id, gen_random_uuid())" in source


def test_migration_registers_project_context_changes_and_membership_moves() -> None:
    migration_path = PROJECT_ROOT / "alembic" / "versions" / "033_graph_relation_ledger.py"
    source = migration_path.read_text().lower()

    assert "create function sync_project_registry" in source
    assert "create trigger project_contexts_brain_registry_trigger" in source
    assert "create function sync_entity_project_membership" in source
    assert source.count("perform sync_entity_project_membership") >= 2
    assert "drop function if exists sync_project_registry" in source
    assert "drop function if exists sync_entity_project_membership" in source


def test_project_registry_trigger_ignores_focus_and_counter_only_updates() -> None:
    migration_path = PROJECT_ROOT / "alembic" / "versions" / "033_graph_relation_ledger.py"
    source = " ".join(migration_path.read_text().lower().split())
    trigger = source.split("create trigger project_contexts_brain_registry_trigger", 1)[1]
    trigger = trigger.split("for each row", 1)[0]

    assert "update of project_key, name, metadata" in trigger
    assert "current_focus" not in trigger
    assert "decisions_count" not in trigger


def test_migration_registers_feature_and_plan_nodes_for_full_rebuild() -> None:
    migration_path = PROJECT_ROOT / "alembic" / "versions" / "033_graph_relation_ledger.py"
    source = migration_path.read_text().lower()

    assert '("features", "feature", "name")' in source
    assert '("indexed_plans", "plan", "title")' in source
    assert "when 'features' then 'feature'" in source
    assert "when 'indexed_plans' then 'plan'" in source


def test_registry_trigger_tracks_postgres_pointer_relations_after_backfill() -> None:
    migration_path = PROJECT_ROOT / "alembic" / "versions" / "033_graph_relation_ledger.py"
    source = migration_path.read_text().lower()

    assert "create function sync_pointer_relation" in source
    assert source.count("perform sync_pointer_relation") >= 4
    assert "'supersedes'" in source
    assert "'merged_into'" in source
    assert "drop function if exists sync_pointer_relation" in source


def test_registry_triggers_ignore_access_decay_only_updates() -> None:
    migration_path = PROJECT_ROOT / "alembic" / "versions" / "033_graph_relation_ledger.py"
    source = migration_path.read_text().lower()

    assert "update of" in source
    trigger_section = source.split("for table_name, _entity_type, _label_column", 1)[1]
    assert "access_count" not in trigger_section.split("def upgrade", 1)[0]
    assert "last_accessed_at" not in trigger_section.split("def upgrade", 1)[0]


def test_migration_canonicalizes_known_project_aliases_at_rest_and_on_write() -> None:
    migration_path = PROJECT_ROOT / "alembic" / "versions" / "033_graph_relation_ledger.py"
    source = migration_path.read_text().lower()

    assert "('brain_v42', 'brain-v42'" in source
    assert "('brain', 'brain-v42'" in source
    assert "('auto_discord', 'auto-discord'" in source
    for alias_key, project_key in (
        ("edr_hawkixs", "edr-hawkixs"),
        ("purple_team_lab", "purple-team-lab"),
        ("red-alerts (notifications discord)", "red-alerts"),
        ("red (écosystème parent)", "red"),
        ("red-lab (agent runtime)", "red-lab"),
        ("red-monitor (source des métriques)", "red-monitor"),
        ("red-orchestrator (workflow engine)", "red-orchestrator"),
    ):
        assert f'("{alias_key}", "{project_key}")' in source
        assert f"('{alias_key}', '{project_key}', 'migration')" in source
    assert "update features" in source
    assert "create function normalize_project_key_alias" in source
    assert "before insert or update of project_key" in source
    assert "drop function if exists normalize_project_key_alias" in source


def test_migration_and_legacy_import_share_the_exact_project_alias_contract() -> None:
    from brain_v42.services.legacy_graph_models import _LEGACY_PROJECT_KEY_ALIASES

    source = (PROJECT_ROOT / "alembic" / "versions" / "033_graph_relation_ledger.py").read_text()
    alias_block = source.split("_PROJECT_KEY_ALIASES =", 1)[1].split(
        "_PROJECT_KEY_ALIAS_VALUES_SQL", 1
    )[0]
    migration_aliases = dict(
        re.findall(r'^\s+\("([^"]+)", "([^"]+)"\),$', alias_block, flags=re.MULTILINE)
    )

    assert migration_aliases == _LEGACY_PROJECT_KEY_ALIASES


def test_project_registry_tracks_all_auxiliary_references_after_migration() -> None:
    migration_path = PROJECT_ROOT / "alembic" / "versions" / "033_graph_relation_ledger.py"
    source = migration_path.read_text().lower()
    project_backfill = source.split("with referenced(project_key) as", 1)[1].split(
        "insert into projects", 1
    )[0]

    assert "select project_key from search_log" in project_backfill
    assert "create function sync_referenced_project_registry" in source
    for trigger_name in (
        "indexed_plan_chunks_project_registry_trigger",
        "gitlab_events_project_registry_trigger",
        "brain_sessions_project_registry_trigger",
        "search_log_project_registry_trigger",
        "tickets_project_registry_trigger",
        "ticket_messages_project_registry_trigger",
        "ticket_extraction_proposals_project_registry_trigger",
    ):
        assert f'"{trigger_name}"' in source
    assert "create trigger {trigger_name}" in source
    assert "drop trigger if exists {trigger_name}" in source
    assert "create trigger project_contexts_related_project_registry_trigger" in source
    assert "drop trigger if exists project_contexts_related_project_registry_trigger" in source
    assert "drop function if exists sync_referenced_project_registry" in source
    assert "drop function if exists sync_related_project_registry" in source


def test_migration_trims_project_references_and_rejects_noncanonical_keys() -> None:
    migration_path = PROJECT_ROOT / "alembic" / "versions" / "033_graph_relation_ledger.py"
    source = " ".join(migration_path.read_text().lower().split())

    assert "set {column_name} = btrim({column_name})" in source
    assert "constraint projects_key_format_valid" in source
    assert "project_key ~ '^[a-z0-9]+([:-][a-z0-9]+)*$'" in source
