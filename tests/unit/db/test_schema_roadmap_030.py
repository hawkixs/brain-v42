"""Unit tests for migration 030 — curated roadmap (spec 2026-07-04).

Covers:
1. features.merged_into declared in tables.py (UUID nullable, FK self-ref).
2. roadmap_curation_proposals declared in METADATA with columns, checks, indexes.
3. Migration 030 wired (down_revision=029), adds 'archived' to the status CHECK,
   adds merged_into, creates the proposals table; downgrade is symmetric.
4. No CHECK constrains merged_into (postgres-check-vs-on-delete-set-null gotcha).

All tests are pure Python — no DB connection needed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent


class TestFeaturesMergedInto:
    def test_features_has_merged_into_column(self) -> None:
        from brain_v42.db.tables import features

        col = features.c["merged_into"]
        assert isinstance(col.type, UUID)
        assert col.nullable is True

    def test_merged_into_fk_targets_features_id(self) -> None:
        from brain_v42.db.tables import features

        fks = list(features.c["merged_into"].foreign_keys)
        assert len(fks) == 1
        assert fks[0].column.table.name == "features"
        assert fks[0].column.name == "id"
        # Never a DELETE on features → no ON DELETE on this FK.
        assert fks[0].ondelete is None


class TestRoadmapCurationProposalsTable:
    def test_in_metadata_and_all(self) -> None:
        from brain_v42.db import tables as mod
        from brain_v42.db.tables import METADATA

        assert "roadmap_curation_proposals" in METADATA.tables
        assert "roadmap_curation_proposals" in mod.__all__

    def test_columns(self) -> None:
        from brain_v42.db.tables import roadmap_curation_proposals as t

        assert isinstance(t.c["id"].type, sa.BigInteger)
        assert t.c["id"].primary_key is True
        assert isinstance(t.c["op"].type, sa.String)
        assert t.c["op"].nullable is False
        assert isinstance(t.c["feature_id"].type, UUID)
        assert t.c["feature_id"].nullable is False
        assert isinstance(t.c["payload"].type, JSONB)
        assert t.c["payload"].nullable is False
        assert isinstance(t.c["rationale"].type, sa.Text)
        assert isinstance(t.c["status"].type, sa.String)
        assert t.c["status"].server_default is not None
        assert isinstance(t.c["created_at"].type, sa.DateTime)
        assert isinstance(t.c["applied_at"].type, sa.DateTime)
        assert t.c["applied_at"].nullable is True

    def test_feature_id_fk_cascade(self) -> None:
        from brain_v42.db.tables import roadmap_curation_proposals as t

        fks = list(t.c["feature_id"].foreign_keys)
        assert len(fks) == 1
        assert fks[0].column.table.name == "features"
        assert fks[0].ondelete == "CASCADE"

    def test_check_constraints(self) -> None:
        from brain_v42.db.tables import roadmap_curation_proposals as t

        names = {c.name for c in t.constraints if isinstance(c, sa.CheckConstraint)}
        assert "rcp_op_valid" in names
        assert "rcp_status_valid" in names

    def test_indexes(self) -> None:
        from brain_v42.db.tables import roadmap_curation_proposals as t

        index_names = {idx.name for idx in t.indexes}
        assert "idx_rcp_status" in index_names
        assert "idx_rcp_feature" in index_names


class TestMigration030Structure:
    @property
    def migration_path(self) -> Path:
        versions_dir = PROJECT_ROOT / "alembic" / "versions"
        candidates = list(versions_dir.glob("030_*.py"))
        assert len(candidates) == 1, f"Expected one 030_*.py, found {candidates}"
        return candidates[0]

    @property
    def content(self) -> str:
        return self.migration_path.read_text()

    def test_revision_chain(self) -> None:
        assert 'revision = "030"' in self.content
        assert 'down_revision = "029"' in self.content

    def test_has_upgrade_and_downgrade(self) -> None:
        tree = ast.parse(self.content)
        funcs = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        assert "upgrade" in funcs
        assert "downgrade" in funcs

    def test_upgrade_extends_status_check_with_archived(self) -> None:
        content = self.content
        assert "features_status_check" in content
        assert "'archived'" in content

    def test_upgrade_adds_merged_into_with_fk(self) -> None:
        content = self.content
        assert "merged_into" in content
        assert "REFERENCES features(id)" in content

    def test_no_check_clause_constrains_merged_into(self) -> None:
        """postgres-check-vs-on-delete-set-null gotcha: merged_into never in a CHECK.

        We inspect the CONTENT of the CHECK (...) clauses — not the surrounding
        text, otherwise comments trigger false positives.
        """
        import re

        for m in re.finditer(r"CHECK\s*\(([^)]*)\)", self.content, flags=re.IGNORECASE):
            assert "merged_into" not in m.group(1).lower(), m.group(0)

    def test_no_check_constraint_on_features_table_declaration(self) -> None:
        """tables.py: no CheckConstraint of features references merged_into."""
        from brain_v42.db.tables import features

        for c in features.constraints:
            if isinstance(c, sa.CheckConstraint):
                assert "merged_into" not in str(c.sqltext)

    def test_upgrade_creates_proposals_table_with_indexes(self) -> None:
        content = self.content
        assert "CREATE TABLE roadmap_curation_proposals" in content
        assert "idx_rcp_status" in content
        assert "idx_rcp_feature" in content
        assert "rcp_op_valid" in content
        assert "rcp_status_valid" in content

    def test_downgrade_symmetric(self) -> None:
        content = self.content
        assert "DROP TABLE IF EXISTS roadmap_curation_proposals" in content
        assert "DROP COLUMN IF EXISTS merged_into" in content

    def test_downgrade_updates_archived_before_restoring_check(self) -> None:
        """The archived→research UPDATE must precede the restored ADD CONSTRAINT."""
        downgrade_src = self.content.split("def downgrade")[1]
        assert downgrade_src.index("UPDATE features SET status = 'research'") < downgrade_src.index(
            "ADD CONSTRAINT features_status_check"
        )

    def test_alembic_chain_links_030(self) -> None:
        """030 stays chained on 029; the current head lives in the
        test_migration_heads_to_latest canary (031+: apply_log, etc.)."""
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config("alembic.ini")
        script = ScriptDirectory.from_config(cfg)
        rev = script.get_revision("030")
        assert rev is not None
        assert rev.down_revision == "029"
