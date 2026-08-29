"""SQLAlchemy table definitions for brain_v42.

All 6 tables: decisions, learnings, snippets, runbooks, adrs, project_contexts.
Uses SQLAlchemy Core (not ORM) for simplicity and pgvector compatibility.

Design decisions:
- SQLAlchemy Core (Table + Column) rather than ORM (DeclarativeBase) for clean
  pgvector compatibility and native Alembic autogenerate support.
- search_vector is a plain nullable TSVECTOR column (NOT Computed). The GENERATED
  ALWAYS AS expression is applied in the Alembic migration via raw SQL ALTER TABLE.
- HNSW indexes with m=16, ef_construction=64, vector_cosine_ops for cosine similarity.
- All JSONB/array defaults use server_default=sa.text(...) to avoid Python-side defaults.
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID

_EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIMENSION", "1536"))
MIN_COMPARABLE_EMBEDDING_NORM = 1e-6

METADATA = MetaData()

# Canonical graph data foundation (migration 033).
projects = Table(
    "projects",
    METADATA,
    Column("project_key", String(50), primary_key=True),
    Column("display_name", String(200), nullable=True),
    Column(
        "registry_status",
        String(16),
        nullable=False,
        server_default=sa.text("'unclaimed'"),
    ),
    Column("source", String(16), nullable=False, server_default=sa.text("'reference'")),
    Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    sa.CheckConstraint(
        "registry_status IN ('claimed', 'unclaimed', 'archived')",
        name="projects_registry_status_valid",
    ),
    sa.CheckConstraint(
        "source IN ('context', 'reference', 'manual')",
        name="projects_source_valid",
    ),
    sa.CheckConstraint(
        "project_key ~ '^[a-z0-9]+([:-][a-z0-9]+)*$'",
        name="projects_key_format_valid",
    ),
)

project_aliases = Table(
    "project_aliases",
    METADATA,
    Column("alias_key", String(128), primary_key=True),
    Column(
        "project_key",
        String(50),
        sa.ForeignKey("projects.project_key", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("source", String(16), nullable=False, server_default=sa.text("'manual'")),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Index("idx_project_aliases_project_key", "project_key"),
)

brain_entities = Table(
    "brain_entities",
    METADATA,
    Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    Column("entity_type", String(32), nullable=False),
    Column("entity_key", Text, nullable=False),
    Column("source_uuid", UUID(as_uuid=True), nullable=True),
    Column(
        "project_key",
        String(50),
        sa.ForeignKey("projects.project_key", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("scope_kind", String(16), nullable=False),
    Column("display_label", Text, nullable=True),
    Column("lifecycle", String(16), nullable=False, server_default=sa.text("'active'")),
    Column("revision", sa.BigInteger, nullable=False, server_default=sa.text("1")),
    Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column("deleted_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("entity_type", "entity_key", name="uq_brain_entities_type_key"),
    sa.CheckConstraint(
        "(scope_kind = 'global' AND project_key IS NULL) "
        "OR (scope_kind = 'project' AND project_key IS NOT NULL)",
        name="brain_entities_scope_valid",
    ),
    sa.CheckConstraint(
        "lifecycle IN ('active', 'archived', 'deleted')",
        name="brain_entities_lifecycle_valid",
    ),
    Index(
        "uq_brain_entities_source_uuid",
        "source_uuid",
        unique=True,
        postgresql_where=sa.text("source_uuid IS NOT NULL"),
    ),
    Index("idx_brain_entities_project_lifecycle", "project_key", "lifecycle"),
    Index("idx_brain_entities_type_lifecycle", "entity_type", "lifecycle"),
)

entity_relations = Table(
    "entity_relations",
    METADATA,
    Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    Column(
        "source_entity_id",
        UUID(as_uuid=True),
        sa.ForeignKey("brain_entities.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "target_entity_id",
        UUID(as_uuid=True),
        sa.ForeignKey("brain_entities.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("relation_type", String(32), nullable=False),
    Column("origin", String(64), nullable=False),
    Column("origin_ref", Text, nullable=True),
    Column("confidence", sa.Float, nullable=True),
    Column("properties", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    Column("lifecycle", String(16), nullable=False, server_default=sa.text("'active'")),
    Column("revision", sa.BigInteger, nullable=False, server_default=sa.text("1")),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column("deleted_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint(
        "source_entity_id",
        "target_entity_id",
        "relation_type",
        name="uq_entity_relations_endpoints_type",
    ),
    sa.CheckConstraint(
        "source_entity_id <> target_entity_id",
        name="entity_relations_no_self_loop",
    ),
    sa.CheckConstraint(
        "relation_type IN ('SUPERSEDES', 'MOTIVATED_BY', 'IMPLEMENTS', "
        "'DOCUMENTS', 'USES', 'RELATED_TO', 'CONTAINS', 'DEPENDS_ON', "
        "'BELONGS_TO', 'MERGED_INTO', 'BELONGS_TO_DOMAIN')",
        name="entity_relations_type_valid",
    ),
    sa.CheckConstraint(
        "confidence >= 0.0 AND confidence <= 1.0",
        name="entity_relations_confidence_valid",
    ),
    sa.CheckConstraint(
        "lifecycle IN ('active', 'archived', 'deleted')",
        name="entity_relations_lifecycle_valid",
    ),
    Index("idx_entity_relations_source_active", "source_entity_id", "lifecycle"),
    Index("idx_entity_relations_target_active", "target_entity_id", "lifecycle"),
    Index("idx_entity_relations_type_active", "relation_type", "lifecycle"),
)

graph_outbox = Table(
    "graph_outbox",
    METADATA,
    Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    Column(
        "event_id",
        UUID(as_uuid=True),
        nullable=False,
        unique=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    Column(
        "entity_id",
        UUID(as_uuid=True),
        sa.ForeignKey("brain_entities.id", ondelete="CASCADE"),
        nullable=True,
    ),
    Column(
        "relation_id",
        UUID(as_uuid=True),
        sa.ForeignKey("entity_relations.id", ondelete="CASCADE"),
        nullable=True,
    ),
    Column("aggregate_revision", sa.BigInteger, nullable=False),
    Column("operation", String(16), nullable=False),
    Column("attempt_count", Integer, nullable=False, server_default=sa.text("0")),
    Column(
        "available_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column("leased_until", DateTime(timezone=True), nullable=True),
    Column("lease_owner", String(128), nullable=True),
    Column("lease_generation", sa.BigInteger, nullable=True),
    Column("claim_version", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    Column("delivered_at", DateTime(timezone=True), nullable=True),
    Column("last_error_code", String(100), nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    UniqueConstraint(
        "entity_id",
        "aggregate_revision",
        name="uq_graph_outbox_entity_revision",
    ),
    UniqueConstraint(
        "relation_id",
        "aggregate_revision",
        name="uq_graph_outbox_relation_revision",
    ),
    sa.CheckConstraint(
        "(entity_id IS NOT NULL AND relation_id IS NULL) "
        "OR (entity_id IS NULL AND relation_id IS NOT NULL)",
        name="graph_outbox_exactly_one_aggregate",
    ),
    sa.CheckConstraint(
        "operation IN ('upsert_entity', 'delete_entity', 'upsert_relation', 'delete_relation')",
        name="graph_outbox_operation_valid",
    ),
    Index(
        "idx_graph_outbox_pending",
        "available_at",
        "id",
        postgresql_where=sa.text("delivered_at IS NULL"),
    ),
)

graph_projection_leases = Table(
    "graph_projection_leases",
    METADATA,
    Column("slot", String(32), primary_key=True),
    Column("protocol_version", Integer, nullable=False, server_default=sa.text("2")),
    Column("generation", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    Column("owner", String(128), nullable=True),
    Column("leased_until", DateTime(timezone=True), nullable=True),
    Column("neo4j_armed_generation", sa.BigInteger, nullable=True),
    Column("recovery_id", UUID(as_uuid=True), nullable=True),
    Column(
        "recovery_phase",
        String(16),
        nullable=False,
        server_default=sa.text("'idle'"),
    ),
    Column("last_completed_recovery_id", UUID(as_uuid=True), nullable=True),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    sa.CheckConstraint(
        "protocol_version = 2",
        name="graph_projection_leases_protocol_valid",
    ),
    sa.CheckConstraint(
        "neo4j_armed_generation IS NULL OR neo4j_armed_generation = generation",
        name="graph_projection_leases_armed_generation_valid",
    ),
    sa.CheckConstraint(
        "(recovery_id IS NULL AND recovery_phase = 'idle') "
        "OR (recovery_id IS NOT NULL "
        "AND recovery_id IS DISTINCT FROM last_completed_recovery_id "
        "AND owner IS NOT NULL "
        "AND leased_until IS NOT NULL AND ("
        "(recovery_phase = 'prepared' AND neo4j_armed_generation IS NULL) "
        "OR (recovery_phase = 'neo_ready' "
        "AND neo4j_armed_generation IS NOT NULL "
        "AND neo4j_armed_generation = generation)))",
        name="graph_projection_leases_recovery_state_valid",
    ),
)

# ─── decisions ────────────────────────────────────────────────────────────────

decisions = Table(
    "decisions",
    METADATA,
    Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    Column("title", String(200), nullable=False),
    Column("description", Text, nullable=False),
    Column("reasoning", Text, nullable=False),
    Column("alternatives", ARRAY(Text), nullable=False, server_default=sa.text("'{}'")),
    Column("consequences", Text, nullable=True),
    Column("project_key", String(50), nullable=True),
    Column("tags", ARRAY(Text), nullable=False, server_default=sa.text("'{}'")),
    Column("status", String(20), nullable=False, server_default=sa.text("'active'")),
    Column(
        "superseded_by",
        UUID(as_uuid=True),
        sa.ForeignKey("decisions.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("embedding", Vector(_EMBEDDING_DIM), nullable=True),
    Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'")),
    Column("search_vector", TSVECTOR, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column("last_accessed_at", DateTime(timezone=True), nullable=True),
    Column("access_count", Integer, server_default=sa.text("0")),
    Column("freshness_status", String(10), server_default=sa.text("'fresh'")),
    Column("merged_into", UUID(as_uuid=True), nullable=True),
    Column("access_count_human", sa.Integer, nullable=False, server_default=sa.text("0")),
    Column("content_updated_at", DateTime(timezone=True), nullable=True),
    Column("freshness_status_updated_at", DateTime(timezone=True), nullable=True),
    Column("freshness_source", String(16), nullable=True),
    Column("last_accessed_at_human", DateTime(timezone=True), nullable=True),
    Index("idx_decisions_search", sa.text("search_vector"), postgresql_using="gin"),
    Index("idx_decisions_project", "project_key"),
    Index("idx_decisions_status", "status"),
    Index("idx_decisions_tags", "tags", postgresql_using="gin"),
    Index("idx_decisions_created", sa.text("created_at DESC")),
    Index(
        "idx_decisions_embedding",
        "embedding",
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    ),
    # CHECK portés par la chaîne alembic et longtemps absents d'ici : un banc
    # create_all() acceptait ce que la prod refuse (8f59f6b7, élargi en PR 44 —
    # 18 CHECK sur 12 tables). La parité est gardée toutes tables par
    # tests/integration/db/test_fresh_head_is_the_yardstick.py.
    sa.CheckConstraint(
        "status IN ('active', 'superseded', 'deprecated')",
        name="decisions_status_check",
    ),
    sa.CheckConstraint(
        "freshness_source IS NULL OR freshness_source IN ('merge', 'judgment', 'score', 'revive')",
        name="ck_decisions_freshness_source",
    ),
)

# ─── learnings ────────────────────────────────────────────────────────────────

learnings = Table(
    "learnings",
    METADATA,
    Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    Column("topic", String(200), nullable=False),
    Column("insight", Text, nullable=False),
    Column("source", Text, nullable=True),
    Column("source_type", String(20), nullable=False, server_default=sa.text("'experience'")),
    Column("confidence", String(10), nullable=False, server_default=sa.text("'medium'")),
    Column("project_key", String(50), nullable=True),
    Column("tags", ARRAY(Text), nullable=False, server_default=sa.text("'{}'")),
    Column("validated_at", DateTime(timezone=True), nullable=True),
    Column("embedding", Vector(_EMBEDDING_DIM), nullable=True),
    Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'")),
    Column("search_vector", TSVECTOR, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column("last_accessed_at", DateTime(timezone=True), nullable=True),
    Column("access_count", Integer, server_default=sa.text("0")),
    Column("freshness_status", String(10), server_default=sa.text("'fresh'")),
    Column("merged_into", UUID(as_uuid=True), nullable=True),
    Column("access_count_human", sa.Integer, nullable=False, server_default=sa.text("0")),
    Column("content_updated_at", DateTime(timezone=True), nullable=True),
    Column("freshness_status_updated_at", DateTime(timezone=True), nullable=True),
    Column("freshness_source", String(16), nullable=True),
    Column("last_accessed_at_human", DateTime(timezone=True), nullable=True),
    Index("idx_learnings_search", sa.text("search_vector"), postgresql_using="gin"),
    Index("idx_learnings_project", "project_key"),
    Index("idx_learnings_confidence", "confidence"),
    Index("idx_learnings_tags", "tags", postgresql_using="gin"),
    Index("idx_learnings_created", sa.text("created_at DESC")),
    Index(
        "idx_learnings_embedding",
        "embedding",
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    ),
    # CHECK portés par la chaîne alembic et longtemps absents d'ici : un banc
    # create_all() acceptait ce que la prod refuse (8f59f6b7, élargi en PR 44 —
    # 18 CHECK sur 12 tables). La parité est gardée toutes tables par
    # tests/integration/db/test_fresh_head_is_the_yardstick.py.
    sa.CheckConstraint(
        "confidence IN ('low', 'medium', 'high')",
        name="learnings_confidence_check",
    ),
    sa.CheckConstraint(
        "source_type IN ('experience', 'documentation', 'code_review', 'bug', "
        "'external', 'article', 'video', 'book', 'conversation', 'research', "
        "'automated')",
        name="learnings_source_type_check",
    ),
    sa.CheckConstraint(
        "freshness_source IS NULL OR freshness_source IN ('merge', 'judgment', 'score', 'revive')",
        name="ck_learnings_freshness_source",
    ),
)

# ─── snippets ─────────────────────────────────────────────────────────────────

snippets = Table(
    "snippets",
    METADATA,
    Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    Column("title", String(200), nullable=False),
    Column("intention", Text, nullable=False),
    Column("code", Text, nullable=False),
    Column("language", String(50), nullable=False),
    Column("dependencies", ARRAY(Text), nullable=False, server_default=sa.text("'{}'")),
    Column("usage_example", Text, nullable=True),
    Column("gotchas", Text, nullable=True),
    Column("project_key", String(50), nullable=True),
    Column("tags", ARRAY(Text), nullable=False, server_default=sa.text("'{}'")),
    Column("use_count", Integer, nullable=False, server_default=sa.text("0")),
    Column("last_used_at", DateTime(timezone=True), nullable=True),
    Column("embedding", Vector(_EMBEDDING_DIM), nullable=True),
    Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'")),
    Column("search_vector", TSVECTOR, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column("last_accessed_at", DateTime(timezone=True), nullable=True),
    Column("access_count", Integer, server_default=sa.text("0")),
    Column("freshness_status", String(10), server_default=sa.text("'fresh'")),
    Column("merged_into", UUID(as_uuid=True), nullable=True),
    Column("access_count_human", sa.Integer, nullable=False, server_default=sa.text("0")),
    Column("content_updated_at", DateTime(timezone=True), nullable=True),
    Column("freshness_status_updated_at", DateTime(timezone=True), nullable=True),
    Column("freshness_source", String(16), nullable=True),
    Column("last_accessed_at_human", DateTime(timezone=True), nullable=True),
    Index("idx_snippets_search", sa.text("search_vector"), postgresql_using="gin"),
    Index("idx_snippets_language", "language"),
    Index("idx_snippets_project", "project_key"),
    Index("idx_snippets_tags", "tags", postgresql_using="gin"),
    Index("idx_snippets_use_count", sa.text("use_count DESC")),
    Index("idx_snippets_created", sa.text("created_at DESC")),
    Index(
        "idx_snippets_embedding",
        "embedding",
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    ),
    # CHECK portés par la chaîne alembic et longtemps absents d'ici : un banc
    # create_all() acceptait ce que la prod refuse (8f59f6b7, élargi en PR 44 —
    # 18 CHECK sur 12 tables). La parité est gardée toutes tables par
    # tests/integration/db/test_fresh_head_is_the_yardstick.py.
    sa.CheckConstraint(
        "freshness_source IS NULL OR freshness_source IN ('merge', 'judgment', 'score', 'revive')",
        name="ck_snippets_freshness_source",
    ),
)

# ─── runbooks ─────────────────────────────────────────────────────────────────

runbooks = Table(
    "runbooks",
    METADATA,
    Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    Column("title", String(200), nullable=False),
    Column("description", Text, nullable=False),
    Column("project_key", String(50), nullable=False),
    Column("trigger", Text, nullable=False),
    Column("prerequisites", ARRAY(Text), nullable=False, server_default=sa.text("'{}'")),
    Column("steps", JSONB, nullable=False, server_default=sa.text("'[]'")),
    Column("rollback_steps", JSONB, nullable=False, server_default=sa.text("'[]'")),
    Column("estimated_duration", String(50), nullable=True),
    Column("tags", ARRAY(Text), nullable=False, server_default=sa.text("'{}'")),
    Column("execution_count", Integer, nullable=False, server_default=sa.text("0")),
    Column("last_executed_at", DateTime(timezone=True), nullable=True),
    Column("last_execution_status", String(20), nullable=True),
    Column("embedding", Vector(_EMBEDDING_DIM), nullable=True),
    Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'")),
    Column("search_vector", TSVECTOR, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column("last_accessed_at", DateTime(timezone=True), nullable=True),
    Column("access_count", Integer, server_default=sa.text("0")),
    Column("freshness_status", String(10), server_default=sa.text("'fresh'")),
    Column("merged_into", UUID(as_uuid=True), nullable=True),
    Column("access_count_human", sa.Integer, nullable=False, server_default=sa.text("0")),
    Column("content_updated_at", DateTime(timezone=True), nullable=True),
    Column("freshness_status_updated_at", DateTime(timezone=True), nullable=True),
    Column("freshness_source", String(16), nullable=True),
    Column("last_accessed_at_human", DateTime(timezone=True), nullable=True),
    UniqueConstraint("title", "project_key", name="uq_runbooks_title_project"),
    Index("idx_runbooks_search", sa.text("search_vector"), postgresql_using="gin"),
    Index("idx_runbooks_project", "project_key"),
    Index("idx_runbooks_tags", "tags", postgresql_using="gin"),
    Index("idx_runbooks_created", sa.text("created_at DESC")),
    Index(
        "idx_runbooks_embedding",
        "embedding",
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    ),
    # CHECK portés par la chaîne alembic et longtemps absents d'ici : un banc
    # create_all() acceptait ce que la prod refuse (8f59f6b7, élargi en PR 44 —
    # 18 CHECK sur 12 tables). La parité est gardée toutes tables par
    # tests/integration/db/test_fresh_head_is_the_yardstick.py.
    sa.CheckConstraint(
        "freshness_source IS NULL OR freshness_source IN ('merge', 'judgment', 'score', 'revive')",
        name="ck_runbooks_freshness_source",
    ),
)

# ─── adrs ─────────────────────────────────────────────────────────────────────

adrs = Table(
    "adrs",
    METADATA,
    Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    Column("number", Integer, nullable=False),
    Column("title", String(200), nullable=False),
    Column("context", Text, nullable=False),
    Column("decision", Text, nullable=False),
    Column("consequences", Text, nullable=False),
    Column(
        "alternatives_considered",
        JSONB,
        nullable=False,
        server_default=sa.text("'[]'"),
    ),
    Column("project_key", String(50), nullable=False),
    Column("tags", ARRAY(Text), nullable=False, server_default=sa.text("'{}'")),
    Column("status", String(20), nullable=False, server_default=sa.text("'proposed'")),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    Column("superseded_by", Integer, nullable=True),
    Column("embedding", Vector(_EMBEDDING_DIM), nullable=True),
    Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'")),
    Column("search_vector", TSVECTOR, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column("last_accessed_at", DateTime(timezone=True), nullable=True),
    Column("access_count", Integer, server_default=sa.text("0")),
    Column("freshness_status", String(10), server_default=sa.text("'fresh'")),
    Column("merged_into", UUID(as_uuid=True), nullable=True),
    Column("access_count_human", sa.Integer, nullable=False, server_default=sa.text("0")),
    Column("content_updated_at", DateTime(timezone=True), nullable=True),
    Column("freshness_status_updated_at", DateTime(timezone=True), nullable=True),
    Column("freshness_source", String(16), nullable=True),
    Column("last_accessed_at_human", DateTime(timezone=True), nullable=True),
    UniqueConstraint("number", "project_key", name="uq_adrs_number_project"),
    Index("idx_adrs_search", sa.text("search_vector"), postgresql_using="gin"),
    Index("idx_adrs_project", "project_key"),
    Index("idx_adrs_status", "status"),
    Index("idx_adrs_tags", "tags", postgresql_using="gin"),
    Index("idx_adrs_number", sa.text("project_key, number DESC")),
    Index("idx_adrs_created", sa.text("created_at DESC")),
    Index(
        "idx_adrs_embedding",
        "embedding",
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    ),
    # CHECK portés par la chaîne alembic et longtemps absents d'ici : un banc
    # create_all() acceptait ce que la prod refuse (8f59f6b7, élargi en PR 44 —
    # 18 CHECK sur 12 tables). La parité est gardée toutes tables par
    # tests/integration/db/test_fresh_head_is_the_yardstick.py.
    sa.CheckConstraint(
        "status IN ('proposed', 'accepted', 'deprecated', 'superseded')",
        name="adrs_status_check",
    ),
    sa.CheckConstraint(
        "freshness_source IS NULL OR freshness_source IN ('merge', 'judgment', 'score', 'revive')",
        name="ck_adrs_freshness_source",
    ),
)

# ─── project_contexts ─────────────────────────────────────────────────────────
# NOTE: No embedding or search_vector columns — per SCHEMA.md spec:
# "Pas d'embedding ni de search_vector pour ProjectContext (pas de recherche sémantique)"

project_contexts = Table(
    "project_contexts",
    METADATA,
    Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    Column("project_key", String(50), nullable=False, unique=True),
    Column("name", String(200), nullable=False),
    Column("description", Text, nullable=False),
    Column("languages", ARRAY(Text), nullable=False, server_default=sa.text("'{}'")),
    Column("frameworks", ARRAY(Text), nullable=False, server_default=sa.text("'{}'")),
    Column("databases", ARRAY(Text), nullable=False, server_default=sa.text("'{}'")),
    Column("code_style", Text, nullable=True),
    Column("git_workflow", Text, nullable=True),
    Column("test_strategy", Text, nullable=True),
    Column("current_phase", Text, nullable=True),
    Column("current_focus", Text, nullable=True),
    Column("focus_revision", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    # Dates the focus prose, which updated_at cannot: updated_at moves on any
    # write to the row, counters included. Nullable with no default because
    # migration 040 backfills nothing — NULL means "never measured", and that
    # is the honest value for rows written before the column existed.
    Column("focus_updated_at", DateTime(timezone=True), nullable=True),
    Column("blockers", ARRAY(Text), nullable=False, server_default=sa.text("'{}'")),
    Column("related_projects", ARRAY(Text), nullable=False, server_default=sa.text("'{}'")),
    Column("local_path", Text, nullable=True),
    Column("repo_url", Text, nullable=True),
    Column("decisions_count", Integer, nullable=False, server_default=sa.text("0")),
    Column("learnings_count", Integer, nullable=False, server_default=sa.text("0")),
    Column("snippets_count", Integer, nullable=False, server_default=sa.text("0")),
    Column("runbooks_count", Integer, nullable=False, server_default=sa.text("0")),
    Column("adrs_count", Integer, nullable=False, server_default=sa.text("0")),
    Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'")),
    Column("plan_scan_paths", ARRAY(Text), nullable=False, server_default=sa.text("'{}'")),
    Column("gitlab_project_path", String(200)),
    Column("project_group", String(50), nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Index("idx_project_contexts_key", "project_key"),
    Index("idx_project_contexts_languages", "languages", postgresql_using="gin"),
    Index("idx_project_contexts_frameworks", "frameworks", postgresql_using="gin"),
    # CHECK portés par la chaîne alembic et longtemps absents d'ici : un banc
    # create_all() acceptait ce que la prod refuse (8f59f6b7, élargi en PR 44 —
    # 18 CHECK sur 12 tables). La parité est gardée toutes tables par
    # tests/integration/db/test_fresh_head_is_the_yardstick.py.
    sa.CheckConstraint(
        "project_key ~ '^[a-z0-9]+([:-][a-z0-9]+)*$'",
        name="chk_project_key_format",
    ),
)

# ─── brain_sessions (explicit agent session lifecycle) ──────────────────────

brain_sessions = Table(
    "brain_sessions",
    METADATA,
    Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    Column(
        "project_key",
        String(50),
        sa.ForeignKey("project_contexts.project_key", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("client_key", String(128), nullable=False),
    Column("status", String(20), nullable=False, server_default=sa.text("'open'")),
    Column("started_focus", Text, nullable=True),
    Column("started_focus_revision", sa.BigInteger, nullable=False),
    Column("summary", Text, nullable=True),
    Column("next_focus", Text, nullable=True),
    Column(
        "captured_knowledge_ids",
        ARRAY(UUID(as_uuid=True)),
        nullable=False,
        server_default=sa.text("'{}'::uuid[]"),
    ),
    Column("nothing_to_capture_reason", Text, nullable=True),
    Column("abandonment_reason", Text, nullable=True),
    Column("end_expected_focus_revision", sa.BigInteger, nullable=True),
    Column("focus_outcome", String(20), nullable=True),
    Column("focus_at_end", Text, nullable=True),
    Column("focus_revision_at_end", sa.BigInteger, nullable=True),
    Column(
        "started_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column(
        "last_heartbeat_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column("ended_at", DateTime(timezone=True), nullable=True),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    # Migration 046 — five nullable columns, none backfilled. NULL means
    # "opened before 046"; see the migration docstring for why no default.
    Column("started_by_actor", String(64), nullable=True),
    Column("last_observed_at", DateTime(timezone=True), nullable=True),
    Column("intent", String(500), nullable=True),
    Column("nature", String(16), nullable=True),
    Column("connection_id", String(64), nullable=True),
    UniqueConstraint(
        "project_key",
        "client_key",
        name="uq_brain_sessions_project_client",
    ),
    # PARTIAL, and it must stay partial: a full unique index would burn a
    # connection for its whole life on the first auto-close (migration 046).
    sa.Index(
        "uq_brain_sessions_connection",
        "project_key",
        "connection_id",
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    ),
    sa.CheckConstraint(
        "status IN ('open', 'ended', 'abandoned', 'closed_inactive')",
        name="brain_sessions_status_valid",
    ),
    sa.CheckConstraint(
        "nature IS NULL OR nature IN ('agent', 'operator')",
        name="brain_sessions_nature_valid",
    ),
    sa.CheckConstraint(
        "btrim(client_key) <> ''",
        name="brain_sessions_client_key_nonblank",
    ),
    sa.CheckConstraint(
        "focus_outcome IS NULL OR focus_outcome IN ('applied', 'conflict')",
        name="brain_sessions_focus_outcome_valid",
    ),
    sa.CheckConstraint(
        """
        cardinality(captured_knowledge_ids) <= 100
        AND array_position(captured_knowledge_ids, NULL) IS NULL
        """,
        name="brain_sessions_capture_ids_valid",
    ),
    sa.CheckConstraint(
        """
        (
            status = 'open'
            AND ended_at IS NULL
            AND summary IS NULL
            AND next_focus IS NULL
            AND cardinality(captured_knowledge_ids) = 0
            AND nothing_to_capture_reason IS NULL
            AND abandonment_reason IS NULL
            AND end_expected_focus_revision IS NULL
            AND focus_outcome IS NULL
            AND focus_at_end IS NULL
            AND focus_revision_at_end IS NULL
        )
        OR (
            status = 'ended'
            AND ended_at IS NOT NULL
            AND summary IS NOT NULL
            AND btrim(summary) <> ''
            AND next_focus IS NOT NULL
            AND btrim(next_focus) <> ''
            AND abandonment_reason IS NULL
            AND focus_outcome IS NOT NULL
            AND (end_expected_focus_revision IS NULL OR end_expected_focus_revision >= 0)
            AND (focus_revision_at_end IS NULL OR focus_revision_at_end >= 0)
            AND (
                (
                    end_expected_focus_revision IS NULL
                    AND focus_outcome = 'applied'
                    AND focus_at_end = next_focus
                    AND focus_revision_at_end IS NULL
                )
                OR (
                    end_expected_focus_revision IS NOT NULL
                    AND focus_revision_at_end IS NOT NULL
                    AND (
                        (
                            focus_outcome = 'applied'
                            AND focus_at_end = next_focus
                            AND focus_revision_at_end = end_expected_focus_revision + 1
                        )
                        OR (
                            focus_outcome = 'conflict'
                            AND focus_revision_at_end <> end_expected_focus_revision
                        )
                    )
                )
            )
            AND (
                nothing_to_capture_reason IS NULL
                OR btrim(nothing_to_capture_reason) <> ''
            )
        )
        OR (
            status = 'abandoned'
            AND ended_at IS NOT NULL
            AND summary IS NULL
            AND next_focus IS NULL
            AND cardinality(captured_knowledge_ids) = 0
            AND nothing_to_capture_reason IS NULL
            AND abandonment_reason IS NOT NULL
            AND btrim(abandonment_reason) <> ''
            AND end_expected_focus_revision IS NULL
            AND focus_outcome IS NULL
            AND focus_at_end IS NULL
            AND focus_revision_at_end IS NULL
        )
        OR (
            status = 'closed_inactive'
            AND nature = 'agent'
            AND ended_at IS NOT NULL
            AND summary IS NULL
            AND next_focus IS NULL
            AND nothing_to_capture_reason IS NULL
            AND abandonment_reason IS NULL
            AND end_expected_focus_revision IS NULL
            AND focus_outcome IS NULL
            AND focus_at_end IS NULL
            AND focus_revision_at_end IS NULL
        )
        """,
        name="brain_sessions_terminal_state_valid",
    ),
    Index(
        "idx_brain_sessions_project_status_started",
        "project_key",
        "status",
        sa.text("started_at DESC"),
    ),
)

# ─── brain_session_artifacts (explicit per-session provenance) ───────────────

brain_session_artifacts = Table(
    "brain_session_artifacts",
    METADATA,
    Column("knowledge_id", UUID(as_uuid=True), primary_key=True),
    Column(
        "session_id",
        UUID(as_uuid=True),
        sa.ForeignKey("brain_sessions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("knowledge_type", String(32), nullable=False),
    Column(
        "captured_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    # 048. PAR QUELLE CLÉ cette ligne a été attribuée. NULL = écrite avant la
    # 048, jamais « inconnu par défaut » : aucun backfill, parce que poser
    # 'explicit' partout mentirait sur ce que `derive_capture` avait déposé.
    # `derived_window` est le seul mode DÉDUIT — c'est lui qu'un audit cherche,
    # et c'est pour lui seul que la 048 pose un index partiel.
    Column("attribution_mode", String(24), nullable=True),
    sa.CheckConstraint(
        "knowledge_type IN ('decision', 'learning', 'snippet', 'runbook', "
        "'adr', 'indexed_plan', 'legacy')",
        name="brain_session_artifacts_type_valid",
    ),
    sa.CheckConstraint(
        "attribution_mode IS NULL OR attribution_mode IN "
        "('explicit', 'derived_deposit', 'derived_connection', 'derived_window')",
        name="brain_session_artifacts_attribution_mode_valid",
    ),
    Index(
        "idx_brain_session_artifacts_session_captured",
        "session_id",
        "captured_at",
    ),
    Index(
        "idx_brain_session_artifacts_derived_window",
        "session_id",
        postgresql_where=sa.text("attribution_mode = 'derived_window'"),
    ),
)

# ─── search_log ──────────────────────────────────────────────────────────────
# One row per search tool call. Tracks search quality over time.
# Retention: 30 days (cleaned by MetricsFlusher).

search_log = Table(
    "search_log",
    METADATA,
    Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column("tool_name", String(50), nullable=False),
    Column("project_key", String(50), nullable=True),
    Column("result_count", Integer, nullable=False),
    Column("top_score", sa.Float, nullable=True),
    Column("avg_score", sa.Float, nullable=True),
    Column("latency_ms", sa.Float, nullable=False),
    Index("idx_search_log_created", sa.text("created_at DESC")),
    Index("idx_search_log_tool", "tool_name"),
)

# ─── process_metrics ─────────────────────────────────────────────────────────
# One row per alive MCP process. Periodic upsert of in-memory counters.
# Each process upserts every 30s. Stale rows (>60s) ignored, cleaned after 1h.

process_metrics = Table(
    "process_metrics",
    METADATA,
    Column(
        "agent_name", String, nullable=False, server_default=sa.text("'unknown'"), primary_key=True
    ),
    Column("pid", Integer),
    Column(
        "started_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column("tool_stats", JSONB, nullable=False, server_default=sa.text("'{}'")),
    Column("embedding_stats", JSONB, nullable=False, server_default=sa.text("'{}'")),
    Column("memory_rss_bytes", sa.BigInteger, nullable=False, server_default=sa.text("0")),
)

# ─── features (roadmap tracking) ─────────────────────────────────────────────

features = Table(
    "features",
    METADATA,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    Column("project_key", String(50), nullable=False),
    Column("name", String(200), nullable=False),
    Column("description", Text, nullable=False),
    Column("embedding", Vector(_EMBEDDING_DIM), nullable=True),
    Column("status", String(20), nullable=False, server_default=sa.text("'planned'")),
    Column(
        "status_updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column("pinned", Boolean, server_default=sa.text("false")),
    Column(
        "merged_into",
        UUID(as_uuid=True),
        sa.ForeignKey("features.id"),
        nullable=True,
    ),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    Index("idx_features_project_key", "project_key"),
    Index("idx_features_status", "status"),
    # CHECK portés par la chaîne alembic et longtemps absents d'ici : un banc
    # create_all() acceptait ce que la prod refuse (8f59f6b7, élargi en PR 44 —
    # 18 CHECK sur 12 tables). La parité est gardée toutes tables par
    # tests/integration/db/test_fresh_head_is_the_yardstick.py.
    sa.CheckConstraint(
        "status IN ('planned', 'research', 'design', 'building', 'deployed', 'done', 'archived')",
        name="features_status_check",
    ),
)

feature_artifacts = Table(
    "feature_artifacts",
    METADATA,
    Column(
        "feature_id",
        UUID(as_uuid=True),
        sa.ForeignKey("features.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("artifact_type", String(20), nullable=False),
    Column("artifact_id", UUID(as_uuid=True), nullable=False),
    Column("similarity_score", sa.Float, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    UniqueConstraint("feature_id", "artifact_type", "artifact_id"),
    Index("idx_feature_artifacts_feature_id", "feature_id"),
    Index("idx_feature_artifacts_artifact", "artifact_type", "artifact_id"),
    # CHECK portés par la chaîne alembic et longtemps absents d'ici : un banc
    # create_all() acceptait ce que la prod refuse (8f59f6b7, élargi en PR 44 —
    # 18 CHECK sur 12 tables). La parité est gardée toutes tables par
    # tests/integration/db/test_fresh_head_is_the_yardstick.py.
    sa.CheckConstraint(
        "artifact_type IN ('learning', 'decision', 'snippet', 'runbook', 'adr', "
        "'plan', 'gitlab_event')",
        name="feature_artifacts_artifact_type_check",
    ),
)

# ─── access_log (memory decay) ───────────────────────────────────────────────

access_log = Table(
    "access_log",
    METADATA,
    Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    Column("entity_type", String(20), nullable=False),
    Column("entity_id", UUID(as_uuid=True), nullable=False),
    Column("access_type", String(20), nullable=False),
    Column(
        "accessed_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column("actor", String(64), nullable=False, server_default=sa.text("'unknown'")),
    Index("idx_access_log_entity", "entity_type", "entity_id"),
    Index("idx_access_log_time", sa.text("accessed_at")),
)

# ─── consolidation_log (memory decay) ────────────────────────────────────────
# Index on entity_type: get_handled_pairs filters WHERE entity_type = :type —
# without this index, every consolidation run does a full seq-scan.

consolidation_log = Table(
    "consolidation_log",
    METADATA,
    Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    Column("source_id", UUID(as_uuid=True), nullable=False),
    Column("target_id", UUID(as_uuid=True), nullable=False),
    Column("entity_type", String(20), nullable=False),
    Column("similarity", sa.Float, nullable=False),
    Column("action", String(20), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=sa.text("NOW()"),
    ),
    Index("idx_consolidation_log_entity_type", "entity_type"),
)

# ─── indexed_plans ──────────────────────────────────────────────────────────

indexed_plans = Table(
    "indexed_plans",
    METADATA,
    Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    Column("file_path", String(500), nullable=False, unique=True),
    Column("title", String(500), nullable=False),
    Column("plan_type", String(20), nullable=False),
    Column("project_key", String(50), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("embedding", Vector(_EMBEDDING_DIM), nullable=True),
    # ── added by migration 014 ───────────────────────────────────────────
    Column("content", Text, nullable=False, server_default=sa.text("''")),
    Column("summary", Text, nullable=True),
    Column("search_vector", TSVECTOR, nullable=True),
    Column("tags", ARRAY(String), nullable=False, server_default=sa.text("'{}'")),
    Column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=sa.text("'{}'"),
        key="plan_metadata",
    ),
    Column(
        "status",
        String(20),
        nullable=False,
        server_default=sa.text("'active'"),
    ),
    Column("chunk_count", Integer, nullable=False, server_default=sa.text("0")),
    Column("word_count", Integer, nullable=False, server_default=sa.text("0")),
    Column("access_count", Integer, nullable=False, server_default=sa.text("0")),
    Column("access_count_human", sa.Integer, nullable=False, server_default=sa.text("0")),
    Column("freshness_status_updated_at", DateTime(timezone=True), nullable=True),
    Column("freshness_source", String(16), nullable=True),
    Column("last_accessed_at_human", DateTime(timezone=True), nullable=True),
    Column("last_accessed_at", DateTime(timezone=True), nullable=True),
    Column(
        "freshness_status",
        String(20),
        nullable=False,
        server_default=sa.text("'fresh'"),
    ),
    Column(
        "indexed_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    # ── timestamps ───────────────────────────────────────────────────────
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Index("idx_indexed_plans_project_key", "project_key"),
    Index(
        "idx_indexed_plans_embedding",
        "embedding",
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    ),
    # ── added by migration 014 (plan_chunks) ─────────────────────────────
    # These indexes exist in DB but were missing from the ORM declaration,
    # causing autogenerate to flag them as unmanaged.
    Index("idx_indexed_plans_tags", "tags", postgresql_using="gin"),
    Index(
        "idx_indexed_plans_search_vector",
        sa.text("search_vector"),
        postgresql_using="gin",
    ),
    Index(
        "idx_indexed_plans_pk_status_fresh",
        "project_key",
        "status",
        "freshness_status",
    ),
    # ── added by migration 027 ────────────────────────────────────────────
    # list_plans() in pg_indexed_plan_repo sorts ORDER BY updated_at DESC;
    # this index eliminates the sort on large datasets.
    Index("idx_indexed_plans_updated_at", sa.text("updated_at DESC")),
    # CHECK portés par la chaîne alembic et longtemps absents d'ici : un banc
    # create_all() acceptait ce que la prod refuse (8f59f6b7, élargi en PR 44 —
    # 18 CHECK sur 12 tables). La parité est gardée toutes tables par
    # tests/integration/db/test_fresh_head_is_the_yardstick.py.
    sa.CheckConstraint(
        "status IN ('draft', 'active', 'archived')",
        name="indexed_plans_status_check",
    ),
    sa.CheckConstraint(
        "plan_type IN ('spec', 'plan')",
        name="indexed_plans_plan_type_check",
    ),
    sa.CheckConstraint(
        "freshness_status IN ('fresh', 'stale', 'archived')",
        name="indexed_plans_freshness_status_check",
    ),
    sa.CheckConstraint(
        "freshness_source IS NULL OR freshness_source IN ('merge', 'judgment', 'score', 'revive')",
        name="ck_indexed_plans_freshness_source",
    ),
)

# ─── indexed_plan_chunks ─────────────────────────────────────────────────────
# Created by migration 014 via raw SQL.  Declared here for autogenerate coverage
# and type-safe access.  Schema mirrors the CREATE TABLE in 014_plan_chunks.py
# exactly — any drift between this declaration and the raw INSERT in
# pg_indexed_plan_repo.py is caught by test_schema_indexes_027.py.

indexed_plan_chunks = Table(
    "indexed_plan_chunks",
    METADATA,
    Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    Column(
        "plan_id",
        UUID(as_uuid=True),
        sa.ForeignKey("indexed_plans.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("section_title", String(500), nullable=False),
    Column("section_path", String(1000), nullable=False),
    Column("content", Text, nullable=False),
    Column("section_order", Integer, nullable=False),
    Column("word_count", Integer, nullable=False, server_default=sa.text("0")),
    Column("embedding", Vector(_EMBEDDING_DIM), nullable=False),
    Column("search_vector", TSVECTOR, nullable=True),
    Column("tags", ARRAY(String), nullable=False, server_default=sa.text("'{}'")),
    Column("project_key", String(50), nullable=False),
    Column("plan_type", String(20), nullable=False),
    Column("status", String(20), nullable=False, server_default=sa.text("'active'")),
    Column("access_count", Integer, nullable=False, server_default=sa.text("0")),
    Column("last_accessed_at", DateTime(timezone=True), nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Index("idx_plan_chunks_plan_id", "plan_id"),
    Index(
        "idx_plan_chunks_embedding",
        "embedding",
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    ),
    Index("idx_plan_chunks_tags", "tags", postgresql_using="gin"),
    Index(
        "idx_plan_chunks_search_vector",
        sa.text("search_vector"),
        postgresql_using="gin",
    ),
    Index("idx_plan_chunks_pk_type", "project_key", "plan_type"),
    # CHECK portés par la chaîne alembic et longtemps absents d'ici : un banc
    # create_all() acceptait ce que la prod refuse (8f59f6b7, élargi en PR 44 —
    # 18 CHECK sur 12 tables). La parité est gardée toutes tables par
    # tests/integration/db/test_fresh_head_is_the_yardstick.py.
    sa.CheckConstraint(
        "status IN ('draft', 'active', 'archived')",
        name="indexed_plan_chunks_status_check",
    ),
    sa.CheckConstraint(
        "plan_type IN ('spec', 'plan')",
        name="indexed_plan_chunks_plan_type_check",
    ),
)

# ─── gitlab_events ─────────────────────────────────────────────────────────

gitlab_events = Table(
    "gitlab_events",
    METADATA,
    Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    Column("gitlab_event_id", String(100), unique=True),
    Column("event_type", String(30), nullable=False),
    Column("project_key", String(50), nullable=False),
    Column("gitlab_project_id", Integer),
    Column("ref", String(200)),
    Column("title", String(500)),
    Column("embedding", Vector(_EMBEDDING_DIM), nullable=True),
    Column(
        "feature_id",
        UUID(as_uuid=True),
        sa.ForeignKey("features.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column(
        "processed_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Index("idx_gitlab_events_project_key", "project_key"),
    Index("idx_gitlab_events_event_type", "event_type"),
    Index("idx_gitlab_events_feature_id", "feature_id"),
    Index(
        "idx_gitlab_events_embedding",
        "embedding",
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    ),
)

# ─── dream_runs ──────────────────────────────────────────────────────────────

dream_runs = Table(
    "dream_runs",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_date", sa.Date, nullable=False),
    Column("phase", String(10), nullable=False),
    # Révision 045. 30 car. refusaient deux des cinq modèles de phase
    # configurés, dont le secours WET — et l'INSERT best-effort perdait la
    # LIGNE entière, pas la seule colonne.
    Column("model", String(120)),
    Column("status", String(10), nullable=False),
    Column("duration_s", sa.Float),
    Column("input_tokens", Integer, server_default="0"),
    Column("output_tokens", Integer, server_default="0"),
    Column("cache_read_tokens", Integer, server_default="0"),
    Column("cache_creation_tokens", Integer, server_default="0"),
    Column("cost_usd", sa.Float, server_default="0"),
    Column("api_calls", Integer, server_default="0"),
    Column("tool_calls", Integer, server_default="0"),
    Column("error_message", sa.Text, nullable=True),
    Column(
        "phase_dry_run",
        sa.Boolean,
        nullable=False,
        server_default=sa.text("false"),
    ),
    # Révision 042. Nullable et sans défaut par conséquence mesurée, pas par
    # prudence : aucun des six écrivains ne fait remonter son échec (spec §15.3).
    # NULL = « écrit avant la 042 » ; '*' = phase globale, sans projet à nommer.
    Column("project_key", String(64), nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=sa.text("NOW()"),
    ),
    Index("idx_dream_runs_date", sa.text("run_date DESC")),
)

# ─── dream_promotions ────────────────────────────────────────────────────────

dream_promotions = Table(
    "dream_promotions",
    METADATA,
    Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    Column(
        "dream_run_id",
        Integer,
        sa.ForeignKey("dream_runs.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column(
        "source_learning_id",
        UUID(as_uuid=True),
        sa.ForeignKey("learnings.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("target_type", String(30), nullable=False),
    Column(
        "target_adr_id",
        UUID(as_uuid=True),
        sa.ForeignKey("adrs.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column(
        "target_runbook_id",
        UUID(as_uuid=True),
        sa.ForeignKey("runbooks.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("cosine_observed", sa.Float, nullable=True),
    Column("skipped_reason", sa.Text, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=sa.text("NOW()"),
    ),
    sa.CheckConstraint(
        """(
            (target_type = 'adr' AND target_runbook_id IS NULL)
            OR (target_type = 'runbook' AND target_adr_id IS NULL)
            OR (target_type IN ('skipped_dedup', 'dry_run',
                                'classification_uncertain', 'dedup_unavailable')
                AND target_adr_id IS NULL AND target_runbook_id IS NULL)
        )""",
        name="dream_promotions_target_shape",
    ),
    Index("idx_dream_promotions_source", "source_learning_id"),
    Index("idx_dream_promotions_created", sa.text("created_at DESC")),
    Index(
        "idx_dream_promotions_source_materialized",
        "source_learning_id",
        unique=True,
        postgresql_where=sa.text(
            "target_type IN ('adr', 'runbook') AND source_learning_id IS NOT NULL"
        ),
    ),
)

# ─── metrics_timeseries (24h history for cockpit) ───────────────────────────

metrics_timeseries = Table(
    "metrics_timeseries",
    METADATA,
    Column("bucket_ts", DateTime(timezone=True), primary_key=True),
    Column("metric", String(50), primary_key=True),
    Column("value", sa.Float, nullable=False),
    Index(
        "idx_metrics_ts_metric",
        "metric",
        sa.text("bucket_ts DESC"),
    ),
)

# --- Tickets (coordination family — spec 2026-07-04) -----------------------
# PAS d'embedding, PAS de search_vector, PAS de colonnes decay : les tickets
# sont du transient adressé, hors famille mémoire (spec §1).

tickets = Table(
    "tickets",
    METADATA,
    Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    Column("kind", String(10), nullable=False),
    Column("title", String(200), nullable=False),
    Column("body", Text, nullable=False),
    Column("from_project", String(50), nullable=False),
    Column("to_project", String(50), nullable=False),
    Column("status", String(15), nullable=False, server_default=sa.text("'open'")),
    Column("extraction_status", String(10), nullable=True),
    Column("resolved_at", DateTime(timezone=True), nullable=True),
    Column("closed_at", DateTime(timezone=True), nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    sa.CheckConstraint("kind IN ('request', 'fyi')", name="tickets_kind_valid"),
    sa.CheckConstraint(
        "status IN ('open', 'in_progress', 'resolved', 'wontfix', 'closed', 'acked')",
        name="tickets_status_valid",
    ),
    sa.CheckConstraint(
        "extraction_status IS NULL OR "
        "extraction_status IN ('pending', 'proposed', 'skipped', 'done')",
        name="tickets_extraction_status_valid",
    ),
    Index("idx_tickets_to_project_status", "to_project", "status"),
    Index("idx_tickets_from_project_status", "from_project", "status"),
    Index(
        "idx_tickets_extraction_pending",
        "extraction_status",
        postgresql_where=sa.text("extraction_status = 'pending'"),
    ),
)

ticket_messages = Table(
    "ticket_messages",
    METADATA,
    Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    Column(
        "ticket_id",
        UUID(as_uuid=True),
        sa.ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("author_project", String(50), nullable=False),
    Column("body", Text, nullable=False),
    Column("status_to", String(15), nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Index("idx_ticket_messages_ticket", "ticket_id", "created_at"),
)

# ─── ticket_extraction_proposals (coordination family — spec 2026-07-04 §6) ──
# Pattern PROMOTE (dream_promotions) : table d'audit proposer-only.
# review humaine → apply via scripts/ticket_extract.py.
# Pas d'embedding, pas de decay : hors famille mémoire.

ticket_extraction_proposals = Table(
    "ticket_extraction_proposals",
    METADATA,
    Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    Column(
        "ticket_id",
        UUID(as_uuid=True),
        sa.ForeignKey("tickets.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("target_type", String(10), nullable=False),
    Column("target_project", String(50), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("rationale", Text, nullable=True),
    Column("status", String(10), nullable=False, server_default=sa.text("'proposed'")),
    Column("applied_entity_id", UUID(as_uuid=True), nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column("applied_at", DateTime(timezone=True), nullable=True),
    sa.CheckConstraint(
        "target_type IN ('learning', 'decision')",
        name="tep_target_type_valid",
    ),
    sa.CheckConstraint(
        "status IN ('proposed', 'applied', 'rejected')",
        name="tep_status_valid",
    ),
    Index("idx_tep_status", "status"),
    Index("idx_tep_ticket", "ticket_id"),
)

# ─── ticket_extraction_attempts (Dream EXTRACT observability) ───────────────
# Terminal-only audit trail. A ticket is never left in a leased/running state:
# after a controlled timeout it remains pending and the next run can resume it.

ticket_extraction_attempts = Table(
    "ticket_extraction_attempts",
    METADATA,
    Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    Column(
        "ticket_id",
        UUID(as_uuid=True),
        sa.ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("run_date", sa.Date, nullable=False),
    Column("status", String(10), nullable=False),
    Column("duration_s", sa.Float, nullable=False),
    Column("error_message", Text, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    sa.CheckConstraint(
        "status IN ('done', 'failed', 'timeout', 'deferred')",
        name="ticket_extraction_attempts_status_valid",
    ),
    Index("idx_ticket_extraction_attempts_ticket", "ticket_id", "created_at"),
    Index("idx_ticket_extraction_attempts_date", "run_date", "status"),
)

# ─── roadmap_curation_proposals (roadmap curée — spec 2026-07-04 §1) ─────────
# Pattern 029 : table d'audit proposer-only, review humaine → apply via
# scripts/roadmap_curate.py --apply-ids. Pas d'embedding, pas de decay.

roadmap_curation_proposals = Table(
    "roadmap_curation_proposals",
    METADATA,
    Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    Column("op", String(10), nullable=False),
    Column(
        "feature_id",
        UUID(as_uuid=True),
        sa.ForeignKey("features.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("payload", JSONB, nullable=False),
    Column("rationale", Text, nullable=True),
    Column("status", String(10), nullable=False, server_default=sa.text("'proposed'")),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column("applied_at", DateTime(timezone=True), nullable=True),
    # Provenance d'apply (031) : états antérieurs + artifacts déplacés au
    # merge — rend la défusion possible (constat 2026-07-05 : irréversible
    # faute de trace, 86 artifacts commingés sur data-lab-endpoints).
    Column("apply_log", JSONB, nullable=True),
    sa.CheckConstraint(
        "op IN ('merge', 'archive', 'status', 'rename')",
        name="rcp_op_valid",
    ),
    sa.CheckConstraint(
        "status IN ('proposed', 'applied', 'rejected')",
        name="rcp_status_valid",
    ),
    Index("idx_rcp_status", "status"),
    Index("idx_rcp_feature", "feature_id"),
)

__all__ = [
    "METADATA",
    "_EMBEDDING_DIM",
    "projects",
    "project_aliases",
    "brain_entities",
    "entity_relations",
    "graph_outbox",
    "graph_projection_leases",
    "decisions",
    "learnings",
    "snippets",
    "runbooks",
    "adrs",
    "project_contexts",
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
]
