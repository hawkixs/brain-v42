"""Unit tests for SQLAlchemy table definitions (feature #610).

Tests cover: table presence in METADATA, Vector(_EMBEDDING_DIM) types, search_vector absence on
project_contexts, UniqueConstraints on runbooks and adrs, superseded_by FK on decisions.

These tests do NOT require a live DB connection — they only test Python-level table structure.
"""

from __future__ import annotations

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID


class TestMetadataAndTablePresence:
    """All 29 tables must be registered in the shared METADATA instance."""

    def test_metadata_exported(self) -> None:
        """METADATA is exported from brain_v42.db.tables."""
        from brain_v42.db.tables import METADATA

        assert METADATA is not None
        assert isinstance(METADATA, sa.MetaData)

    def test_all_tables_in_metadata(self) -> None:
        """All 30 tables are registered in METADATA (session provenance added in 037)."""
        from brain_v42.db.tables import METADATA

        expected_tables = {
            "decisions",
            "learnings",
            "snippets",
            "runbooks",
            "adrs",
            "project_contexts",
            "project_focus_history",
            "brain_session_checkpoints",
            "brain_sessions",
            "brain_session_artifacts",
            "search_log",
            "process_metrics",
            "features",
            "feature_artifacts",
            "access_log",
            "consolidation_log",
            "indexed_plans",
            "indexed_plan_chunks",
            "gitlab_events",
            "dream_runs",
            "dream_promotions",
            "metrics_timeseries",
            "tickets",
            "ticket_messages",
            "ticket_extraction_proposals",
            "ticket_extraction_attempts",
            "roadmap_curation_proposals",
            "projects",
            "project_aliases",
            "brain_entities",
            "entity_relations",
            "graph_outbox",
            "graph_projection_leases",
        }
        assert expected_tables == set(METADATA.tables.keys())

    def test_table_objects_exported(self) -> None:
        """Individual table objects are exported from brain_v42.db.tables."""
        from brain_v42.db.tables import (
            adrs,
            decisions,
            learnings,
            project_contexts,
            runbooks,
            snippets,
        )

        assert decisions.name == "decisions"
        assert learnings.name == "learnings"
        assert snippets.name == "snippets"
        assert runbooks.name == "runbooks"
        assert adrs.name == "adrs"
        assert project_contexts.name == "project_contexts"

    def test_metadata_reexported_from_db_init(self) -> None:
        """METADATA is re-exported from brain_v42.db __init__."""
        from brain_v42.db import METADATA

        assert METADATA is not None
        assert isinstance(METADATA, sa.MetaData)

    def test_all_tables_reexported_from_db_init(self) -> None:
        """All table objects are re-exported from brain_v42.db."""
        from brain_v42.db import adrs, decisions, learnings, project_contexts, runbooks, snippets

        assert decisions.name == "decisions"
        assert learnings.name == "learnings"
        assert snippets.name == "snippets"
        assert runbooks.name == "runbooks"
        assert adrs.name == "adrs"
        assert project_contexts.name == "project_contexts"


class TestVectorColumns:
    """5 tables (all except project_contexts) must have Vector(_EMBEDDING_DIM) embedding column."""

    def test_decisions_has_embedding_vector_configured_dim(self) -> None:
        """decisions.embedding is Vector(_EMBEDDING_DIM)."""
        from brain_v42.db.tables import _EMBEDDING_DIM, decisions

        col = decisions.c["embedding"]
        assert isinstance(col.type, Vector)
        assert col.type.dim == _EMBEDDING_DIM

    def test_learnings_has_embedding_vector_configured_dim(self) -> None:
        """learnings.embedding is Vector(_EMBEDDING_DIM)."""
        from brain_v42.db.tables import _EMBEDDING_DIM, learnings

        col = learnings.c["embedding"]
        assert isinstance(col.type, Vector)
        assert col.type.dim == _EMBEDDING_DIM

    def test_snippets_has_embedding_vector_configured_dim(self) -> None:
        """snippets.embedding is Vector(_EMBEDDING_DIM)."""
        from brain_v42.db.tables import _EMBEDDING_DIM, snippets

        col = snippets.c["embedding"]
        assert isinstance(col.type, Vector)
        assert col.type.dim == _EMBEDDING_DIM

    def test_runbooks_has_embedding_vector_configured_dim(self) -> None:
        """runbooks.embedding is Vector(_EMBEDDING_DIM)."""
        from brain_v42.db.tables import _EMBEDDING_DIM, runbooks

        col = runbooks.c["embedding"]
        assert isinstance(col.type, Vector)
        assert col.type.dim == _EMBEDDING_DIM

    def test_adrs_has_embedding_vector_configured_dim(self) -> None:
        """adrs.embedding is Vector(_EMBEDDING_DIM)."""
        from brain_v42.db.tables import _EMBEDDING_DIM, adrs

        col = adrs.c["embedding"]
        assert isinstance(col.type, Vector)
        assert col.type.dim == _EMBEDDING_DIM

    def test_project_contexts_has_no_embedding_column(self) -> None:
        """project_contexts must NOT have an embedding column (per SCHEMA.md spec)."""
        from brain_v42.db.tables import project_contexts

        assert "embedding" not in project_contexts.c


class TestProjectContextFocusUpdatedAt:
    """focus_updated_at dates the focus prose, which updated_at cannot do."""

    def test_focus_updated_at_is_a_nullable_timestamptz(self) -> None:
        """Nullable because migration 040 deliberately backfills nothing.

        NULL reads as "never measured". Copying updated_at into it would be a
        number whose label lies, since updated_at moves on any write to the
        row — counters included.
        """
        import sqlalchemy as sa

        from brain_v42.db.tables import project_contexts

        col = project_contexts.c["focus_updated_at"]
        assert isinstance(col.type, sa.DateTime)
        assert col.type.timezone is True
        assert col.nullable is True

    def test_focus_updated_at_has_no_server_default(self) -> None:
        """A server default would stamp rows no one wrote a focus into."""
        from brain_v42.db.tables import project_contexts

        col = project_contexts.c["focus_updated_at"]
        assert col.server_default is None
        assert col.server_onupdate is None


class TestSearchVectorColumns:
    """5 tables must have search_vector column, project_contexts must NOT."""

    def test_decisions_has_search_vector(self) -> None:
        """decisions.search_vector is a TSVECTOR column."""
        from brain_v42.db.tables import decisions

        col = decisions.c["search_vector"]
        assert isinstance(col.type, TSVECTOR)
        assert col.nullable is True

    def test_learnings_has_search_vector(self) -> None:
        """learnings.search_vector is a TSVECTOR column."""
        from brain_v42.db.tables import learnings

        col = learnings.c["search_vector"]
        assert isinstance(col.type, TSVECTOR)

    def test_snippets_has_search_vector(self) -> None:
        """snippets.search_vector is a TSVECTOR column."""
        from brain_v42.db.tables import snippets

        col = snippets.c["search_vector"]
        assert isinstance(col.type, TSVECTOR)

    def test_runbooks_has_search_vector(self) -> None:
        """runbooks.search_vector is a TSVECTOR column."""
        from brain_v42.db.tables import runbooks

        col = runbooks.c["search_vector"]
        assert isinstance(col.type, TSVECTOR)

    def test_adrs_has_search_vector(self) -> None:
        """adrs.search_vector is a TSVECTOR column."""
        from brain_v42.db.tables import adrs

        col = adrs.c["search_vector"]
        assert isinstance(col.type, TSVECTOR)

    def test_project_contexts_has_no_search_vector(self) -> None:
        """project_contexts must NOT have a search_vector column (per SCHEMA.md spec)."""
        from brain_v42.db.tables import project_contexts

        assert "search_vector" not in project_contexts.c


class TestUniqueConstraints:
    """runbooks and adrs must have specific UniqueConstraints."""

    def test_runbooks_unique_constraint_title_project(self) -> None:
        """runbooks has UniqueConstraint('title', 'project_key', name='uq_runbooks_title_project')."""
        from brain_v42.db.tables import runbooks

        constraints = {c.name: c for c in runbooks.constraints if isinstance(c, UniqueConstraint)}
        assert "uq_runbooks_title_project" in constraints
        uc = constraints["uq_runbooks_title_project"]
        col_names = {col.name for col in uc.columns}
        assert col_names == {"title", "project_key"}

    def test_adrs_unique_constraint_number_project(self) -> None:
        """adrs has UniqueConstraint('number', 'project_key', name='uq_adrs_number_project')."""
        from brain_v42.db.tables import adrs

        constraints = {c.name: c for c in adrs.constraints if isinstance(c, UniqueConstraint)}
        assert "uq_adrs_number_project" in constraints
        uc = constraints["uq_adrs_number_project"]
        col_names = {col.name for col in uc.columns}
        assert col_names == {"number", "project_key"}


class TestForeignKeys:
    """decisions.superseded_by must self-reference decisions.id."""

    def test_decisions_superseded_by_fk_to_decisions_id(self) -> None:
        """decisions.superseded_by is a UUID ForeignKey pointing to decisions.id."""
        from brain_v42.db.tables import decisions

        col = decisions.c["superseded_by"]
        assert col.nullable is True
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        fk = fks[0]
        assert fk.column.table.name == "decisions"
        assert fk.column.name == "id"

    def test_decisions_superseded_by_fk_has_on_delete_set_null(self) -> None:
        """decisions.superseded_by FK must use ON DELETE SET NULL."""
        from brain_v42.db.tables import decisions

        col = decisions.c["superseded_by"]
        fk = list(col.foreign_keys)[0]
        assert fk.ondelete == "SET NULL"

    def test_decisions_superseded_by_is_uuid_type(self) -> None:
        """decisions.superseded_by column is UUID type."""
        from brain_v42.db.tables import decisions

        col = decisions.c["superseded_by"]
        assert isinstance(col.type, UUID)

    def test_adrs_superseded_by_is_integer_not_fk(self) -> None:
        """adrs.superseded_by is an Integer (ADR number reference, NOT a UUID FK)."""
        from brain_v42.db.tables import adrs

        col = adrs.c["superseded_by"]
        assert isinstance(col.type, sa.Integer)
        assert len(list(col.foreign_keys)) == 0


class TestIndexes:
    """Spot-check key indexes are defined on tables."""

    def _get_index_names(self, table: sa.Table) -> set[str]:
        return {idx.name for idx in table.indexes}

    def test_decisions_has_hnsw_embedding_index(self) -> None:
        """decisions has an HNSW index on embedding."""
        from brain_v42.db.tables import decisions

        index_names = self._get_index_names(decisions)
        assert "idx_decisions_embedding" in index_names

    def test_decisions_has_gin_search_vector_index(self) -> None:
        """decisions has a GIN index on search_vector."""
        from brain_v42.db.tables import decisions

        index_names = self._get_index_names(decisions)
        assert "idx_decisions_search" in index_names

    def test_learnings_has_embedding_index(self) -> None:
        """learnings has an HNSW index on embedding."""
        from brain_v42.db.tables import learnings

        index_names = self._get_index_names(learnings)
        assert "idx_learnings_embedding" in index_names

    def test_project_contexts_has_gin_languages_index(self) -> None:
        """project_contexts has a GIN index on languages array."""
        from brain_v42.db.tables import project_contexts

        index_names = self._get_index_names(project_contexts)
        assert "idx_project_contexts_languages" in index_names

    def test_project_contexts_has_gin_frameworks_index(self) -> None:
        """project_contexts has a GIN index on frameworks array."""
        from brain_v42.db.tables import project_contexts

        index_names = self._get_index_names(project_contexts)
        assert "idx_project_contexts_frameworks" in index_names


class TestColumnDefaults:
    """Key columns should have server-side defaults."""

    def test_decisions_id_has_gen_random_uuid_default(self) -> None:
        """decisions.id has server_default=gen_random_uuid()."""
        from brain_v42.db.tables import decisions

        col = decisions.c["id"]
        assert col.server_default is not None

    def test_decisions_created_at_has_now_default(self) -> None:
        """decisions.created_at has server_default=NOW()."""
        from brain_v42.db.tables import decisions

        col = decisions.c["created_at"]
        assert col.server_default is not None

    def test_decisions_tags_has_array_default(self) -> None:
        """decisions.tags has a server_default (empty array)."""
        from brain_v42.db.tables import decisions

        col = decisions.c["tags"]
        assert col.server_default is not None

    def test_runbooks_steps_is_jsonb(self) -> None:
        """runbooks.steps is JSONB type."""
        from brain_v42.db.tables import runbooks

        col = runbooks.c["steps"]
        assert isinstance(col.type, JSONB)

    def test_adrs_alternatives_considered_is_jsonb(self) -> None:
        """adrs.alternatives_considered is JSONB type."""
        from brain_v42.db.tables import adrs

        col = adrs.c["alternatives_considered"]
        assert isinstance(col.type, JSONB)


class TestColumnPresence:
    """Spot-check key columns exist on each table."""

    def test_decisions_required_columns(self) -> None:
        """decisions table has all required columns."""
        from brain_v42.db.tables import decisions

        required = {
            "id",
            "title",
            "description",
            "reasoning",
            "alternatives",
            "project_key",
            "tags",
            "status",
            "superseded_by",
            "embedding",
            "search_vector",
            "created_at",
            "updated_at",
        }
        actual = set(decisions.c.keys())
        assert required.issubset(actual)

    def test_learnings_required_columns(self) -> None:
        """learnings table has all required columns."""
        from brain_v42.db.tables import learnings

        required = {
            "id",
            "topic",
            "insight",
            "project_key",
            "tags",
            "embedding",
            "search_vector",
            "created_at",
            "updated_at",
        }
        actual = set(learnings.c.keys())
        assert required.issubset(actual)

    def test_snippets_required_columns(self) -> None:
        """snippets table has all required columns."""
        from brain_v42.db.tables import snippets

        required = {
            "id",
            "title",
            "intention",
            "code",
            "language",
            "project_key",
            "tags",
            "embedding",
            "search_vector",
            "created_at",
            "updated_at",
        }
        actual = set(snippets.c.keys())
        assert required.issubset(actual)

    def test_runbooks_required_columns(self) -> None:
        """runbooks table has all required columns."""
        from brain_v42.db.tables import runbooks

        required = {
            "id",
            "title",
            "description",
            "project_key",
            "trigger",
            "steps",
            "rollback_steps",
            "tags",
            "embedding",
            "search_vector",
            "created_at",
            "updated_at",
        }
        actual = set(runbooks.c.keys())
        assert required.issubset(actual)

    def test_adrs_required_columns(self) -> None:
        """adrs table has all required columns."""
        from brain_v42.db.tables import adrs

        required = {
            "id",
            "number",
            "title",
            "context",
            "decision",
            "consequences",
            "project_key",
            "tags",
            "status",
            "superseded_by",
            "embedding",
            "search_vector",
            "created_at",
            "updated_at",
        }
        actual = set(adrs.c.keys())
        assert required.issubset(actual)

    def test_project_contexts_required_columns(self) -> None:
        """project_contexts table has all required columns."""
        from brain_v42.db.tables import project_contexts

        required = {
            "id",
            "project_key",
            "name",
            "description",
            "languages",
            "frameworks",
            "databases",
            "created_at",
            "updated_at",
        }
        actual = set(project_contexts.c.keys())
        assert required.issubset(actual)


class TestSchemaConsistency:
    """Schema consistency checks for plan_scan_paths, features.embedding, features.status."""

    def test_plan_scan_paths_is_array_type(self) -> None:
        """project_contexts.plan_scan_paths should be ARRAY(Text), not JSONB."""
        from brain_v42.db.tables import project_contexts

        col = project_contexts.c["plan_scan_paths"]
        assert isinstance(col.type, sa.types.ARRAY), (
            f"plan_scan_paths should be ARRAY, got {type(col.type)}"
        )

    def test_features_embedding_is_nullable(self) -> None:
        """features.embedding must be explicitly nullable."""
        from brain_v42.db.tables import features

        col = features.c["embedding"]
        assert col.nullable is True, "features.embedding must be nullable=True"

    def test_features_status_server_default_uses_sa_text(self) -> None:
        """features.status server_default should use sa.text() for consistency."""
        from brain_v42.db.tables import features

        col = features.c["status"]
        default = col.server_default
        assert default is not None
        assert hasattr(default.arg, "text"), "features.status server_default should use sa.text()"
