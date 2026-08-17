"""Static contract checks for the Codex read views migration 036."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[3]
MIGRATION = ROOT / "alembic" / "versions" / "036_codex_contract_views.py"

CONTRACT_VIEWS = (
    "codex_ticket_v1",
    "codex_ticket_message_v1",
    "codex_feature_v1",
    "codex_feature_artifact_v1",
    "codex_dream_run_v1",
    "codex_dream_promotion_v1",
    "codex_ticket_extraction_proposal_v1",
    "codex_roadmap_curation_proposal_v1",
    "codex_consolidation_log_v1",
)

SCOPED_VIEWS = (
    "codex_brain_entity_v1",
    "codex_ticket_v1",
    "codex_ticket_message_v1",
    "codex_feature_v1",
    "codex_feature_artifact_v1",
    "codex_ticket_extraction_proposal_v1",
    "codex_roadmap_curation_proposal_v1",
)


def test_migration_036_defines_the_nine_codex_contract_views() -> None:
    assert MIGRATION.is_file(), "migration 036 must deliver the Codex v2 read contract"
    source = MIGRATION.read_text()

    assert 'revision = "036"' in source
    assert 'down_revision = "035"' in source
    for view in CONTRACT_VIEWS:
        assert f"CREATE OR REPLACE VIEW {view}" in source


def test_scoped_views_reuse_the_fail_closed_red_keys_contract() -> None:
    assert MIGRATION.is_file(), "migration 036 must deliver the Codex v2 read contract"
    source = MIGRATION.read_text()

    assert "_RED_KEYS_CTE" in source
    assert "project_group = 'red'" in source
    assert "split_part" in source
    assert "t.from_project IN (SELECT project_key FROM red_keys)" in source
    assert "t.to_project IN (SELECT project_key FROM red_keys)" in source
    assert "f.project_key IN (SELECT project_key FROM red_keys)" in source


def test_upgrade_grants_and_downgrade_drops_every_new_view_symmetrically() -> None:
    assert MIGRATION.is_file(), "migration 036 must deliver the Codex v2 read contract"
    source = MIGRATION.read_text()

    assert "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM codex_ro" in source
    assert "GRANT SELECT ON codex_brain_entity_v1 TO codex_ro" in source
    for view in CONTRACT_VIEWS:
        assert f"GRANT SELECT ON {view} TO codex_ro" in source
        assert f"DROP VIEW IF EXISTS {view}" in source


def test_migration_036_repairs_plan_only_subpartition_scope() -> None:
    source = MIGRATION.read_text()

    assert "_BRAIN_RED_KEYS_CTE" in source
    assert "FROM indexed_plans" in source
    assert "UNION ALL SELECT project_key FROM indexed_plans" in source
    assert "CREATE OR REPLACE VIEW codex_brain_entity_v1 AS" in source
    assert "ALTER VIEW codex_brain_entity_v1 RENAME" not in source
    assert "codex_brain_entity_v1_pre036" not in source
    assert "_RESTORE_BRAIN_ENTITY_VIEW_024" in source


def test_migration_036_fences_artifact_links_to_merged_or_archived_features() -> None:
    source = MIGRATION.read_text()

    assert "enforce_live_feature_artifact_target" in source
    assert "FOR SHARE" in source
    assert "FOR KEY SHARE" not in source
    assert "target_status = 'archived'" in source
    assert "target_merged_into IS NOT NULL" in source
    assert "BEFORE INSERT OR UPDATE OF feature_id ON feature_artifacts" in source


def test_migration_036_makes_ticket_participants_immutable() -> None:
    source = MIGRATION.read_text()

    assert "enforce_immutable_ticket_participants" in source
    assert "OLD.from_project" in source
    assert "OLD.to_project" in source
    assert "BEFORE UPDATE OF from_project, to_project ON tickets" in source


def test_every_red_scoped_view_is_a_security_barrier() -> None:
    source = MIGRATION.read_text()

    for view in SCOPED_VIEWS:
        assert f"CREATE OR REPLACE VIEW {view} WITH (security_barrier = true) AS" in source
    assert "RESET (security_barrier)" in source
