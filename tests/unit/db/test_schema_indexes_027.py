"""Unit tests for schema-indexes-027 workstream.

Covers:
1. consolidation_log now has an index on entity_type (for get_handled_pairs WHERE).
2. indexed_plans now declares the three indexes added by migration 014 (search_vector
   GIN, tags GIN, pk_status_fresh composite) and the updated_at DESC sort index.
3. indexed_plan_chunks is declared in METADATA with correct columns, FK, and indexes.
4. Migration 027 has correct down_revision = '026', upgrade/downgrade symmetry.

All tests are pure Python — no DB connection needed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent


# ─── 1. consolidation_log indexes ────────────────────────────────────────────


class TestConsolidationLogIndex:
    """consolidation_log must have an index covering the get_handled_pairs query."""

    def _get_index_names(self, table: sa.Table) -> set[str]:
        return {idx.name for idx in table.indexes}

    def test_consolidation_log_has_entity_type_index(self) -> None:
        """consolidation_log must have an index on entity_type for get_handled_pairs."""
        from brain_v42.db.tables import consolidation_log

        index_names = self._get_index_names(consolidation_log)
        assert "idx_consolidation_log_entity_type" in index_names, (
            "consolidation_log must have idx_consolidation_log_entity_type index "
            "to avoid seq-scan in get_handled_pairs"
        )

    def test_consolidation_log_entity_type_index_covers_column(self) -> None:
        """The entity_type index covers the entity_type column."""
        from brain_v42.db.tables import consolidation_log

        idx = next(
            (i for i in consolidation_log.indexes if i.name == "idx_consolidation_log_entity_type"),
            None,
        )
        assert idx is not None
        col_names = {col.name for col in idx.columns}
        assert "entity_type" in col_names


# ─── 2. indexed_plans missing indexes ─────────────────────────────────────────


class TestIndexedPlansIndexes:
    """indexed_plans must declare all indexes added by migration 014 + updated_at sort."""

    def _get_index_names(self, table: sa.Table) -> set[str]:
        return {idx.name for idx in table.indexes}

    def test_indexed_plans_has_search_vector_gin_index(self) -> None:
        """indexed_plans must declare idx_indexed_plans_search_vector (GIN)."""
        from brain_v42.db.tables import indexed_plans

        index_names = self._get_index_names(indexed_plans)
        assert "idx_indexed_plans_search_vector" in index_names, (
            "indexed_plans must have idx_indexed_plans_search_vector GIN index "
            "(created by migration 014)"
        )

    def test_indexed_plans_search_vector_index_is_gin(self) -> None:
        """idx_indexed_plans_search_vector must use GIN method."""
        from brain_v42.db.tables import indexed_plans

        idx = next(
            (i for i in indexed_plans.indexes if i.name == "idx_indexed_plans_search_vector"),
            None,
        )
        assert idx is not None
        dialect_kw = getattr(idx, "dialect_kwargs", {})
        using = dialect_kw.get("postgresql_using", "")
        assert using.lower() == "gin", f"Expected GIN, got {using!r}"

    def test_indexed_plans_has_tags_gin_index(self) -> None:
        """indexed_plans must declare idx_indexed_plans_tags (GIN)."""
        from brain_v42.db.tables import indexed_plans

        index_names = self._get_index_names(indexed_plans)
        assert "idx_indexed_plans_tags" in index_names, (
            "indexed_plans must have idx_indexed_plans_tags GIN index (created by migration 014)"
        )

    def test_indexed_plans_tags_index_is_gin(self) -> None:
        """idx_indexed_plans_tags must use GIN method."""
        from brain_v42.db.tables import indexed_plans

        idx = next(
            (i for i in indexed_plans.indexes if i.name == "idx_indexed_plans_tags"),
            None,
        )
        assert idx is not None
        dialect_kw = getattr(idx, "dialect_kwargs", {})
        using = dialect_kw.get("postgresql_using", "")
        assert using.lower() == "gin", f"Expected GIN, got {using!r}"

    def test_indexed_plans_has_pk_status_fresh_composite_index(self) -> None:
        """indexed_plans must declare idx_indexed_plans_pk_status_fresh composite index."""
        from brain_v42.db.tables import indexed_plans

        index_names = self._get_index_names(indexed_plans)
        assert "idx_indexed_plans_pk_status_fresh" in index_names, (
            "indexed_plans must have idx_indexed_plans_pk_status_fresh composite index "
            "(created by migration 014)"
        )

    def test_indexed_plans_pk_status_fresh_covers_three_columns(self) -> None:
        """idx_indexed_plans_pk_status_fresh covers (project_key, status, freshness_status)."""
        from brain_v42.db.tables import indexed_plans

        idx = next(
            (i for i in indexed_plans.indexes if i.name == "idx_indexed_plans_pk_status_fresh"),
            None,
        )
        assert idx is not None
        col_names = {col.name for col in idx.columns}
        assert col_names == {"project_key", "status", "freshness_status"}, (
            f"Expected {{project_key, status, freshness_status}}, got {col_names}"
        )

    def test_indexed_plans_has_updated_at_index(self) -> None:
        """indexed_plans must declare idx_indexed_plans_updated_at for ORDER BY updated_at DESC."""
        from brain_v42.db.tables import indexed_plans

        index_names = self._get_index_names(indexed_plans)
        assert "idx_indexed_plans_updated_at" in index_names, (
            "indexed_plans must have idx_indexed_plans_updated_at index "
            "to support ORDER BY updated_at DESC in list_plans()"
        )


# ─── 3. indexed_plan_chunks table declaration ──────────────────────────────────


class TestIndexedPlanChunksTableDeclaration:
    """indexed_plan_chunks must be declared in METADATA (tables.py)."""

    def test_indexed_plan_chunks_in_metadata(self) -> None:
        """indexed_plan_chunks must be registered in METADATA."""
        from brain_v42.db.tables import METADATA

        assert "indexed_plan_chunks" in METADATA.tables, (
            "indexed_plan_chunks must be declared in tables.py and registered in METADATA"
        )

    def test_indexed_plan_chunks_exported_from_tables(self) -> None:
        """indexed_plan_chunks must be importable from brain_v42.db.tables."""
        from brain_v42.db.tables import indexed_plan_chunks  # noqa: F401

        assert indexed_plan_chunks.name == "indexed_plan_chunks"

    def test_indexed_plan_chunks_exported_from_all(self) -> None:
        """indexed_plan_chunks must appear in __all__ of tables.py."""
        import brain_v42.db.tables as mod

        assert "indexed_plan_chunks" in mod.__all__

    def test_indexed_plan_chunks_has_id_uuid_pk(self) -> None:
        """indexed_plan_chunks.id is UUID primary key."""
        from brain_v42.db.tables import indexed_plan_chunks

        col = indexed_plan_chunks.c["id"]
        assert isinstance(col.type, UUID)
        assert col.primary_key is True

    def test_indexed_plan_chunks_has_plan_id_fk(self) -> None:
        """indexed_plan_chunks.plan_id is a UUID FK → indexed_plans.id ON DELETE CASCADE."""
        from brain_v42.db.tables import indexed_plan_chunks

        col = indexed_plan_chunks.c["plan_id"]
        assert isinstance(col.type, UUID), f"Expected UUID, got {type(col.type)}"
        fks = list(col.foreign_keys)
        assert len(fks) == 1, "plan_id must have exactly one FK"
        fk = fks[0]
        assert fk.column.table.name == "indexed_plans"
        assert fk.column.name == "id"
        assert fk.ondelete == "CASCADE", f"Expected CASCADE, got {fk.ondelete!r}"

    def test_indexed_plan_chunks_has_section_title(self) -> None:
        """indexed_plan_chunks.section_title is String(500) NOT NULL."""
        from brain_v42.db.tables import indexed_plan_chunks

        col = indexed_plan_chunks.c["section_title"]
        assert isinstance(col.type, sa.String)
        assert col.nullable is False

    def test_indexed_plan_chunks_has_section_path(self) -> None:
        """indexed_plan_chunks.section_path is String(1000) NOT NULL."""
        from brain_v42.db.tables import indexed_plan_chunks

        col = indexed_plan_chunks.c["section_path"]
        assert isinstance(col.type, sa.String)
        assert col.nullable is False

    def test_indexed_plan_chunks_has_content_text(self) -> None:
        """indexed_plan_chunks.content is Text NOT NULL."""
        from brain_v42.db.tables import indexed_plan_chunks

        col = indexed_plan_chunks.c["content"]
        assert isinstance(col.type, sa.Text)
        assert col.nullable is False

    def test_indexed_plan_chunks_has_section_order(self) -> None:
        """indexed_plan_chunks.section_order is Integer NOT NULL."""
        from brain_v42.db.tables import indexed_plan_chunks

        col = indexed_plan_chunks.c["section_order"]
        assert isinstance(col.type, sa.Integer)
        assert col.nullable is False

    def test_indexed_plan_chunks_has_word_count(self) -> None:
        """indexed_plan_chunks.word_count is Integer with default 0."""
        from brain_v42.db.tables import indexed_plan_chunks

        col = indexed_plan_chunks.c["word_count"]
        assert isinstance(col.type, sa.Integer)
        assert col.server_default is not None

    def test_indexed_plan_chunks_has_embedding_vector(self) -> None:
        """indexed_plan_chunks.embedding is Vector(1536) NOT NULL."""
        from brain_v42.db.tables import _EMBEDDING_DIM, indexed_plan_chunks

        col = indexed_plan_chunks.c["embedding"]
        assert isinstance(col.type, Vector)
        assert col.type.dim == _EMBEDDING_DIM
        assert col.nullable is False

    def test_indexed_plan_chunks_has_search_vector_tsvector(self) -> None:
        """indexed_plan_chunks.search_vector is TSVECTOR nullable."""
        from brain_v42.db.tables import indexed_plan_chunks

        col = indexed_plan_chunks.c["search_vector"]
        assert isinstance(col.type, TSVECTOR)
        assert col.nullable is True

    def test_indexed_plan_chunks_has_tags_array(self) -> None:
        """indexed_plan_chunks.tags is ARRAY NOT NULL with default '{}'."""
        from brain_v42.db.tables import indexed_plan_chunks

        col = indexed_plan_chunks.c["tags"]
        assert isinstance(col.type, sa.types.ARRAY)
        assert col.nullable is False
        assert col.server_default is not None

    def test_indexed_plan_chunks_has_project_key(self) -> None:
        """indexed_plan_chunks.project_key is String(50) NOT NULL."""
        from brain_v42.db.tables import indexed_plan_chunks

        col = indexed_plan_chunks.c["project_key"]
        assert isinstance(col.type, sa.String)
        assert col.nullable is False

    def test_indexed_plan_chunks_has_plan_type(self) -> None:
        """indexed_plan_chunks.plan_type is String(20) NOT NULL."""
        from brain_v42.db.tables import indexed_plan_chunks

        col = indexed_plan_chunks.c["plan_type"]
        assert isinstance(col.type, sa.String)
        assert col.nullable is False

    def test_indexed_plan_chunks_has_status(self) -> None:
        """indexed_plan_chunks.status is String(20) NOT NULL with default 'active'."""
        from brain_v42.db.tables import indexed_plan_chunks

        col = indexed_plan_chunks.c["status"]
        assert isinstance(col.type, sa.String)
        assert col.nullable is False
        assert col.server_default is not None

    def test_indexed_plan_chunks_has_access_count(self) -> None:
        """indexed_plan_chunks.access_count is Integer NOT NULL with default 0."""
        from brain_v42.db.tables import indexed_plan_chunks

        col = indexed_plan_chunks.c["access_count"]
        assert isinstance(col.type, sa.Integer)
        assert col.server_default is not None

    def test_indexed_plan_chunks_has_last_accessed_at(self) -> None:
        """indexed_plan_chunks.last_accessed_at is DateTime nullable."""
        from brain_v42.db.tables import indexed_plan_chunks

        col = indexed_plan_chunks.c["last_accessed_at"]
        assert isinstance(col.type, sa.DateTime)
        assert col.nullable is True

    def test_indexed_plan_chunks_has_created_at(self) -> None:
        """indexed_plan_chunks.created_at is DateTime NOT NULL with default NOW()."""
        from brain_v42.db.tables import indexed_plan_chunks

        col = indexed_plan_chunks.c["created_at"]
        assert isinstance(col.type, sa.DateTime)
        assert col.nullable is False
        assert col.server_default is not None

    def test_indexed_plan_chunks_has_hnsw_embedding_index(self) -> None:
        """indexed_plan_chunks must have HNSW index on embedding."""
        from brain_v42.db.tables import indexed_plan_chunks

        index_names = {idx.name for idx in indexed_plan_chunks.indexes}
        assert "idx_plan_chunks_embedding" in index_names

    def test_indexed_plan_chunks_has_plan_id_index(self) -> None:
        """indexed_plan_chunks must have index on plan_id."""
        from brain_v42.db.tables import indexed_plan_chunks

        index_names = {idx.name for idx in indexed_plan_chunks.indexes}
        assert "idx_plan_chunks_plan_id" in index_names

    def test_indexed_plan_chunks_has_tags_gin_index(self) -> None:
        """indexed_plan_chunks must have GIN index on tags."""
        from brain_v42.db.tables import indexed_plan_chunks

        index_names = {idx.name for idx in indexed_plan_chunks.indexes}
        assert "idx_plan_chunks_tags" in index_names

    def test_indexed_plan_chunks_has_search_vector_gin_index(self) -> None:
        """indexed_plan_chunks must have GIN index on search_vector."""
        from brain_v42.db.tables import indexed_plan_chunks

        index_names = {idx.name for idx in indexed_plan_chunks.indexes}
        assert "idx_plan_chunks_search_vector" in index_names

    def test_indexed_plan_chunks_has_pk_type_composite_index(self) -> None:
        """indexed_plan_chunks must have composite index on (project_key, plan_type)."""
        from brain_v42.db.tables import indexed_plan_chunks

        index_names = {idx.name for idx in indexed_plan_chunks.indexes}
        assert "idx_plan_chunks_pk_type" in index_names

    def test_all_tables_count_now_includes_chunks(self) -> None:
        """METADATA must now have 18 tables (17 existing + indexed_plan_chunks)."""
        from brain_v42.db.tables import METADATA

        assert "indexed_plan_chunks" in METADATA.tables
        # Minimum count check — other tables may also be added by concurrent agents
        assert len(METADATA.tables) >= 18, (
            f"Expected at least 18 tables, got {len(METADATA.tables)}: "
            f"{sorted(METADATA.tables.keys())}"
        )


class TestIndexedPlanChunksColumnsMatchRawSQL:
    """Verify tables.py declaration matches the raw SQL INSERT in pg_indexed_plan_repo."""

    REPO_COLUMNS = {
        # columns inserted in upsert_plan_with_chunks (insert_chunk_sql)
        "plan_id",
        "section_title",
        "section_path",
        "content",
        "section_order",
        "word_count",
        "embedding",
        "search_vector",
        "tags",
        "project_key",
        "plan_type",
        "status",
    }

    def test_all_repo_insert_columns_exist_in_table_declaration(self) -> None:
        """Every column inserted by pg_indexed_plan_repo exists in the table declaration."""
        from brain_v42.db.tables import indexed_plan_chunks

        declared_cols = set(indexed_plan_chunks.c.keys())
        missing = self.REPO_COLUMNS - declared_cols
        assert not missing, (
            f"Columns inserted in pg_indexed_plan_repo but missing from tables.py declaration: "
            f"{missing}"
        )


# ─── 4. Migration 027 structural checks ──────────────────────────────────────


class TestMigration027Structure:
    """Migration 027 must be correctly wired and contain expected operations."""

    @property
    def migration_path(self) -> Path:
        versions_dir = PROJECT_ROOT / "alembic" / "versions"
        candidates = list(versions_dir.glob("027_*.py"))
        assert len(candidates) == 1, f"Expected exactly one 027_*.py migration, found {candidates}"
        return candidates[0]

    @property
    def content(self) -> str:
        return self.migration_path.read_text()

    def test_migration_027_exists(self) -> None:
        """A file matching 027_*.py must exist in alembic/versions/."""
        versions_dir = PROJECT_ROOT / "alembic" / "versions"
        candidates = list(versions_dir.glob("027_*.py"))
        assert len(candidates) == 1, f"Expected one 027_*.py file, found: {candidates}"

    def test_migration_027_revision_is_027(self) -> None:
        """revision must be '027'."""
        content = self.content
        assert 'revision = "027"' in content or "revision = '027'" in content

    def test_migration_027_down_revision_is_026(self) -> None:
        """down_revision must be '026'."""
        content = self.content
        assert 'down_revision = "026"' in content or "down_revision = '026'" in content

    def test_migration_027_has_upgrade_function(self) -> None:
        """Must have upgrade() function."""
        tree = ast.parse(self.content)
        funcs = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        assert "upgrade" in funcs

    def test_migration_027_has_downgrade_function(self) -> None:
        """Must have downgrade() function."""
        tree = ast.parse(self.content)
        funcs = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        assert "downgrade" in funcs

    def test_migration_027_creates_consolidation_log_index(self) -> None:
        """upgrade() must create the consolidation_log entity_type index."""
        content = self.content
        assert "idx_consolidation_log_entity_type" in content, (
            "Migration 027 must create idx_consolidation_log_entity_type"
        )

    def test_migration_027_creates_indexed_plans_updated_at_index(self) -> None:
        """upgrade() must create the idx_indexed_plans_updated_at index."""
        content = self.content
        assert "idx_indexed_plans_updated_at" in content, (
            "Migration 027 must create idx_indexed_plans_updated_at"
        )

    def test_migration_027_downgrade_drops_consolidation_log_index(self) -> None:
        """downgrade() must drop the consolidation_log index."""
        content = self.content
        assert "DROP INDEX" in content.upper() or "drop_index" in content, (
            "Migration 027 downgrade must drop indexes"
        )

    def test_migration_027_does_not_create_indexed_plan_chunks_table(self) -> None:
        """Migration 027 must NOT create indexed_plan_chunks (already exists from 014)."""
        content = self.content.upper()
        # We check that it doesn't contain CREATE TABLE for indexed_plan_chunks
        # (it's a declaration-only change in tables.py)
        assert "CREATE TABLE INDEXED_PLAN_CHUNKS" not in content, (
            "Migration 027 must NOT create indexed_plan_chunks table — it already exists"
        )

    def test_alembic_chain_includes_027(self) -> None:
        """Migration 027 must be in the chain with down_revision=026.

        Note: 027 is no longer the HEAD (029 supersedes it), but it must
        still exist in the chain and be correctly wired.
        """
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config("alembic.ini")
        script = ScriptDirectory.from_config(cfg)
        rev = script.get_revision("027")
        assert rev is not None, "Revision 027 must exist in alembic/versions/"
        assert rev.down_revision == "026", (
            f"027.down_revision should be '026', got {rev.down_revision!r}"
        )
