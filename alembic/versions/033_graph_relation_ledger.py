"""Add the canonical project/entity registry, relation ledger, and graph outbox.

Revision ID: 033
Revises: 032
"""

from __future__ import annotations

from alembic import op

revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None

_KNOWLEDGE_TABLES = (
    ("decisions", "decision", "title"),
    ("learnings", "learning", "topic"),
    ("snippets", "snippet", "title"),
    ("runbooks", "runbook", "title"),
    ("adrs", "adr", "title"),
    ("features", "feature", "name"),
    ("indexed_plans", "plan", "title"),
)

_MERGE_TABLES = tuple(table for table in _KNOWLEDGE_TABLES if table[0] != "indexed_plans")

_REGISTRY_TRIGGER_COLUMNS = {
    "decisions": (
        "title",
        "project_key",
        "status",
        "freshness_status",
        "merged_into",
        "superseded_by",
        "metadata",
    ),
    "learnings": (
        "topic",
        "project_key",
        "freshness_status",
        "merged_into",
        "metadata",
    ),
    "snippets": (
        "title",
        "project_key",
        "freshness_status",
        "merged_into",
        "metadata",
    ),
    "runbooks": (
        "title",
        "project_key",
        "freshness_status",
        "merged_into",
        "metadata",
    ),
    "adrs": (
        "title",
        "project_key",
        "status",
        "freshness_status",
        "merged_into",
        "superseded_by",
        "metadata",
    ),
    "features": ("name", "project_key", "status", "merged_into"),
    "indexed_plans": (
        "title",
        "project_key",
        "status",
        "freshness_status",
        "metadata",
    ),
}

_DOMAINS = (
    "infra",
    "ml",
    "backend",
    "memory",
    "tooling",
    "data",
    "ops",
    "frontend",
    "security",
)

_PROJECT_KEY_ALIASES = (
    ("brain_v42", "brain-v42"),
    ("brain", "brain-v42"),
    ("auto_discord", "auto-discord"),
    ("datalake_v2", "datalake-v2"),
    ("edr_hawkixs", "edr-hawkixs"),
    ("hk_anime_list", "hk-anime-list"),
    ("hk_infofeed", "hk-infofeed"),
    ("mrc_rag", "mrc-rag"),
    ("poc_lyriks_v2", "poc-lyriks-v2"),
    ("purple_team_lab", "purple-team-lab"),
    ("red (écosystème parent)", "red"),
    ("red-alerts (notifications Discord)", "red-alerts"),
    ("red_daemon", "red-daemon"),
    ("red_data", "red-data"),
    ("red-lab (agent runtime)", "red-lab"),
    ("red_llm", "red-llm"),
    ("red_ml", "red-ml"),
    ("red-monitor (source des métriques)", "red-monitor"),
    ("red-orchestrator (workflow engine)", "red-orchestrator"),
    ("red_story", "red-story"),
    ("red_tsdb", "red-tsdb"),
    ("red_writer", "red-writer"),
)

_PROJECT_KEY_ALIAS_VALUES_SQL = """
    ('brain_v42', 'brain-v42', 'migration'),
    ('brain', 'brain-v42', 'migration'),
    ('auto_discord', 'auto-discord', 'migration'),
    ('datalake_v2', 'datalake-v2', 'migration'),
    ('edr_hawkixs', 'edr-hawkixs', 'migration'),
    ('hk_anime_list', 'hk-anime-list', 'migration'),
    ('hk_infofeed', 'hk-infofeed', 'migration'),
    ('mrc_rag', 'mrc-rag', 'migration'),
    ('poc_lyriks_v2', 'poc-lyriks-v2', 'migration'),
    ('purple_team_lab', 'purple-team-lab', 'migration'),
    ('red (écosystème parent)', 'red', 'migration'),
    ('red-alerts (notifications Discord)', 'red-alerts', 'migration'),
    ('red_daemon', 'red-daemon', 'migration'),
    ('red_data', 'red-data', 'migration'),
    ('red-lab (agent runtime)', 'red-lab', 'migration'),
    ('red_llm', 'red-llm', 'migration'),
    ('red_ml', 'red-ml', 'migration'),
    ('red-monitor (source des métriques)', 'red-monitor', 'migration'),
    ('red-orchestrator (workflow engine)', 'red-orchestrator', 'migration'),
    ('red_story', 'red-story', 'migration'),
    ('red_tsdb', 'red-tsdb', 'migration'),
    ('red_writer', 'red-writer', 'migration')
"""

_PROJECT_REFERENCE_COLUMNS = (
    ("decisions", ("project_key",)),
    ("learnings", ("project_key",)),
    ("snippets", ("project_key",)),
    ("runbooks", ("project_key",)),
    ("adrs", ("project_key",)),
    ("features", ("project_key",)),
    ("indexed_plans", ("project_key",)),
    ("indexed_plan_chunks", ("project_key",)),
    ("gitlab_events", ("project_key",)),
    ("brain_sessions", ("project_key",)),
    ("search_log", ("project_key",)),
    ("tickets", ("from_project", "to_project")),
    ("ticket_messages", ("author_project",)),
    ("ticket_extraction_proposals", ("target_project",)),
)

_PROJECT_ALIAS_TRIGGER_COLUMNS = (
    ("project_contexts", ("project_key",)),
    *_PROJECT_REFERENCE_COLUMNS,
)

_AUXILIARY_PROJECT_REFERENCE_COLUMNS = (
    (
        "indexed_plan_chunks",
        "indexed_plan_chunks_project_registry_trigger",
        ("project_key",),
    ),
    ("gitlab_events", "gitlab_events_project_registry_trigger", ("project_key",)),
    ("brain_sessions", "brain_sessions_project_registry_trigger", ("project_key",)),
    ("search_log", "search_log_project_registry_trigger", ("project_key",)),
    (
        "tickets",
        "tickets_project_registry_trigger",
        ("from_project", "to_project"),
    ),
    (
        "ticket_messages",
        "ticket_messages_project_registry_trigger",
        ("author_project",),
    ),
    (
        "ticket_extraction_proposals",
        "ticket_extraction_proposals_project_registry_trigger",
        ("target_project",),
    ),
)


def _lock_graph_source_tables() -> None:
    source_tables = (
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
    )
    op.execute(f"LOCK TABLE {', '.join(source_tables)} IN SHARE ROW EXCLUSIVE MODE")


def _create_tables() -> None:
    op.execute(
        """
        CREATE TABLE projects (
            project_key VARCHAR(50) PRIMARY KEY,
            display_name VARCHAR(200),
            registry_status VARCHAR(16) NOT NULL DEFAULT 'unclaimed',
            source VARCHAR(16) NOT NULL DEFAULT 'reference',
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT projects_registry_status_valid
                CHECK (registry_status IN ('claimed', 'unclaimed', 'archived')),
            CONSTRAINT projects_source_valid
                CHECK (source IN ('context', 'reference', 'manual')),
            CONSTRAINT projects_key_format_valid
                CHECK (project_key ~ '^[a-z0-9]+([:-][a-z0-9]+)*$')
        )
        """
    )
    op.execute(
        """
        CREATE TABLE project_aliases (
            alias_key VARCHAR(128) PRIMARY KEY,
            project_key VARCHAR(50) NOT NULL,
            source VARCHAR(16) NOT NULL DEFAULT 'manual',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT project_aliases_project_key_fkey
                FOREIGN KEY (project_key)
                REFERENCES projects(project_key) ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX idx_project_aliases_project_key ON project_aliases(project_key)")
    op.execute(
        """
        CREATE TABLE brain_entities (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            entity_type VARCHAR(32) NOT NULL,
            entity_key TEXT NOT NULL,
            source_uuid UUID,
            project_key VARCHAR(50),
            scope_kind VARCHAR(16) NOT NULL,
            display_label TEXT,
            lifecycle VARCHAR(16) NOT NULL DEFAULT 'active',
            revision BIGINT NOT NULL DEFAULT 1,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMPTZ,
            CONSTRAINT brain_entities_project_key_fkey
                FOREIGN KEY (project_key)
                REFERENCES projects(project_key) ON DELETE RESTRICT,
            CONSTRAINT uq_brain_entities_type_key
                UNIQUE (entity_type, entity_key),
            CONSTRAINT brain_entities_scope_valid CHECK (
                (scope_kind = 'global' AND project_key IS NULL)
                OR (scope_kind = 'project' AND project_key IS NOT NULL)
            ),
            CONSTRAINT brain_entities_lifecycle_valid
                CHECK (lifecycle IN ('active', 'archived', 'deleted'))
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_brain_entities_source_uuid "
        "ON brain_entities(source_uuid) WHERE source_uuid IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX idx_brain_entities_project_lifecycle "
        "ON brain_entities(project_key, lifecycle)"
    )
    op.execute(
        "CREATE INDEX idx_brain_entities_type_lifecycle ON brain_entities(entity_type, lifecycle)"
    )
    op.execute(
        """
        CREATE TABLE entity_relations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source_entity_id UUID NOT NULL,
            target_entity_id UUID NOT NULL,
            relation_type VARCHAR(32) NOT NULL,
            origin VARCHAR(64) NOT NULL,
            origin_ref TEXT,
            confidence DOUBLE PRECISION,
            properties JSONB NOT NULL DEFAULT '{}'::jsonb,
            lifecycle VARCHAR(16) NOT NULL DEFAULT 'active',
            revision BIGINT NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMPTZ,
            CONSTRAINT entity_relations_source_entity_id_fkey
                FOREIGN KEY (source_entity_id)
                REFERENCES brain_entities(id) ON DELETE RESTRICT,
            CONSTRAINT entity_relations_target_entity_id_fkey
                FOREIGN KEY (target_entity_id)
                REFERENCES brain_entities(id) ON DELETE RESTRICT,
            CONSTRAINT uq_entity_relations_endpoints_type
                UNIQUE (source_entity_id, target_entity_id, relation_type),
            CONSTRAINT entity_relations_no_self_loop
                CHECK (source_entity_id <> target_entity_id),
            CONSTRAINT entity_relations_type_valid CHECK (
                relation_type IN (
                    'SUPERSEDES', 'MOTIVATED_BY', 'IMPLEMENTS', 'DOCUMENTS',
                    'USES', 'RELATED_TO', 'CONTAINS', 'DEPENDS_ON',
                    'BELONGS_TO', 'MERGED_INTO', 'BELONGS_TO_DOMAIN'
                )
            ),
            CONSTRAINT entity_relations_confidence_valid
                CHECK (confidence >= 0.0 AND confidence <= 1.0),
            CONSTRAINT entity_relations_lifecycle_valid
                CHECK (lifecycle IN ('active', 'archived', 'deleted'))
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_entity_relations_source_active "
        "ON entity_relations(source_entity_id, lifecycle)"
    )
    op.execute(
        "CREATE INDEX idx_entity_relations_target_active "
        "ON entity_relations(target_entity_id, lifecycle)"
    )
    op.execute(
        "CREATE INDEX idx_entity_relations_type_active "
        "ON entity_relations(relation_type, lifecycle)"
    )
    op.execute(
        """
        CREATE TABLE graph_outbox (
            id BIGSERIAL PRIMARY KEY,
            event_id UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
            entity_id UUID,
            relation_id UUID,
            aggregate_revision BIGINT NOT NULL,
            operation VARCHAR(16) NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            leased_until TIMESTAMPTZ,
            lease_owner VARCHAR(128),
            delivered_at TIMESTAMPTZ,
            last_error_code VARCHAR(100),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT graph_outbox_entity_id_fkey
                FOREIGN KEY (entity_id)
                REFERENCES brain_entities(id) ON DELETE CASCADE,
            CONSTRAINT graph_outbox_relation_id_fkey
                FOREIGN KEY (relation_id)
                REFERENCES entity_relations(id) ON DELETE CASCADE,
            CONSTRAINT uq_graph_outbox_entity_revision
                UNIQUE (entity_id, aggregate_revision),
            CONSTRAINT uq_graph_outbox_relation_revision
                UNIQUE (relation_id, aggregate_revision),
            CONSTRAINT graph_outbox_exactly_one_aggregate CHECK (
                (entity_id IS NOT NULL AND relation_id IS NULL)
                OR (entity_id IS NULL AND relation_id IS NOT NULL)
            ),
            CONSTRAINT graph_outbox_operation_valid
                CHECK (operation IN (
                    'upsert_entity', 'delete_entity',
                    'upsert_relation', 'delete_relation'
                ))
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_graph_outbox_pending
        ON graph_outbox(available_at, id)
        WHERE delivered_at IS NULL
        """
    )


def normalize_known_project_aliases() -> None:
    alias_values = ", ".join(
        f"('{alias_key}', '{project_key}')" for alias_key, project_key in _PROJECT_KEY_ALIASES
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM project_contexts
                WHERE btrim(project_key) IN (
                    SELECT alias_key
                    FROM (VALUES {alias_values}) AS aliases(
                        alias_key,
                        project_key
                    )
                )
            ) THEN
                RAISE EXCEPTION
                    'project alias conflicts with an existing project_context; '
                    'resolve the context before migration 033';
            END IF;
        END;
        $$
        """
    )
    for table_name, columns in _PROJECT_REFERENCE_COLUMNS:
        for column_name in columns:
            op.execute(
                f"""
                UPDATE {table_name}
                SET {column_name} = btrim({column_name})
                WHERE {column_name} IS DISTINCT FROM btrim({column_name})
                """
            )
    op.execute(
        f"""
        UPDATE features
        SET project_key = aliases.project_key
        FROM (VALUES {_PROJECT_KEY_ALIAS_VALUES_SQL}) AS aliases(
            alias_key,
            project_key,
            source
        )
        WHERE btrim(features.project_key) = aliases.alias_key
        """
    )
    for table_name, columns in _PROJECT_REFERENCE_COLUMNS:
        if table_name == "features":
            continue
        for column_name in columns:
            for alias_key, project_key in _PROJECT_KEY_ALIASES:
                op.execute(
                    f"""
                    UPDATE {table_name}
                    SET {column_name} = '{project_key}'
                    WHERE btrim({column_name}) = '{alias_key}'
                    """
                )
    op.execute(
        """
        UPDATE project_contexts
        SET related_projects = ARRAY(
            SELECT btrim(reference.project_key)
            FROM unnest(project_contexts.related_projects) WITH ORDINALITY
                AS reference(project_key, ordinality)
            ORDER BY reference.ordinality
        )
        WHERE EXISTS (
            SELECT 1
            FROM unnest(project_contexts.related_projects) AS reference(project_key)
            WHERE reference.project_key IS DISTINCT FROM btrim(reference.project_key)
        )
        """
    )
    for alias_key, project_key in _PROJECT_KEY_ALIASES:
        op.execute(
            f"""
            UPDATE project_contexts
            SET related_projects = array_replace(
                related_projects,
                '{alias_key}',
                '{project_key}'
            )
            WHERE '{alias_key}' = ANY(related_projects)
            """
        )


def backfill_projects() -> None:
    op.execute(
        """
        INSERT INTO projects (
            project_key,
            display_name,
            registry_status,
            source,
            metadata,
            created_at,
            updated_at
        )
        SELECT
            project_key,
            name,
            'claimed',
            'context',
            metadata,
            created_at,
            updated_at
        FROM project_contexts
        WHERE project_key IS NOT NULL AND btrim(project_key) <> ''
        ON CONFLICT (project_key) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            registry_status = 'claimed',
            source = 'context',
            metadata = EXCLUDED.metadata,
            updated_at = EXCLUDED.updated_at
        """
    )
    op.execute(
        """
        WITH referenced(project_key) AS (
            SELECT project_key FROM decisions
            UNION ALL SELECT project_key FROM learnings
            UNION ALL SELECT project_key FROM snippets
            UNION ALL SELECT project_key FROM runbooks
            UNION ALL SELECT project_key FROM adrs
            UNION ALL SELECT project_key FROM indexed_plans
            UNION ALL SELECT project_key FROM indexed_plan_chunks
            UNION ALL SELECT project_key FROM features
            UNION ALL SELECT project_key FROM gitlab_events
            UNION ALL SELECT from_project FROM tickets
            UNION ALL SELECT to_project FROM tickets
            UNION ALL SELECT author_project FROM ticket_messages
            UNION ALL SELECT target_project FROM ticket_extraction_proposals
            UNION ALL SELECT project_key FROM brain_sessions
            UNION ALL SELECT project_key FROM search_log
            UNION ALL SELECT unnest(related_projects) FROM project_contexts
        )
        INSERT INTO projects (project_key, registry_status, source)
        SELECT DISTINCT btrim(project_key), 'unclaimed', 'reference'
        FROM referenced
        WHERE project_key IS NOT NULL
          AND btrim(project_key) <> ''
          AND length(btrim(project_key)) <= 50
        ON CONFLICT (project_key) DO NOTHING
        """
    )


def backfill_project_aliases() -> None:
    op.execute(
        f"""
        INSERT INTO projects (project_key, registry_status, source)
        SELECT DISTINCT project_key, 'unclaimed', 'reference'
        FROM (VALUES {_PROJECT_KEY_ALIAS_VALUES_SQL}) AS aliases(
            alias_key,
            project_key,
            source
        )
        ON CONFLICT (project_key) DO NOTHING
        """
    )
    op.execute(
        f"""
        INSERT INTO project_aliases (alias_key, project_key, source)
        VALUES {_PROJECT_KEY_ALIAS_VALUES_SQL}
        ON CONFLICT (alias_key) DO UPDATE SET
            project_key = EXCLUDED.project_key,
            source = EXCLUDED.source
        """
    )


def backfill_brain_entities() -> None:
    op.execute(
        """
        INSERT INTO brain_entities (
            id,
            entity_type,
            entity_key,
            source_uuid,
            project_key,
            scope_kind,
            display_label,
            lifecycle,
            metadata,
            created_at,
            updated_at
        )
        SELECT
            COALESCE(project_context.id, gen_random_uuid()),
            'project',
            project.project_key,
            project_context.id,
            project.project_key,
            'project',
            COALESCE(project.display_name, project.project_key),
            CASE
                WHEN project.registry_status = 'archived' THEN 'archived'
                ELSE 'active'
            END,
            project.metadata,
            project.created_at,
            project.updated_at
        FROM projects AS project
        LEFT JOIN project_contexts AS project_context
          ON project_context.project_key = project.project_key
        ON CONFLICT (entity_type, entity_key) DO NOTHING
        """
    )
    domain_values = ", ".join(f"('{domain}')" for domain in _DOMAINS)
    op.execute(
        f"""
        INSERT INTO brain_entities (
            entity_type,
            entity_key,
            scope_kind,
            display_label,
            lifecycle
        )
        SELECT 'domain', domain_name, 'global', domain_name, 'active'
        FROM (VALUES {domain_values}) AS domains(domain_name)
        ON CONFLICT (entity_type, entity_key) DO NOTHING
        """
    )
    for table_name, entity_type, label_column in _KNOWLEDGE_TABLES:
        op.execute(
            f"""
            INSERT INTO brain_entities (
                id,
                entity_type,
                entity_key,
                source_uuid,
                project_key,
                scope_kind,
                display_label,
                lifecycle,
                metadata,
                created_at,
                updated_at
            )
            SELECT
                source_row.id,
                '{entity_type}',
                source_row.id::text,
                source_row.id,
                source_row.project_key,
                CASE
                    WHEN source_row.project_key IS NULL THEN 'global'
                    ELSE 'project'
                END,
                source_row.{label_column},
                CASE
                    WHEN COALESCE(
                        to_jsonb(source_row) ->> 'freshness_status',
                        'fresh'
                    ) = 'archived' THEN 'archived'
                    WHEN COALESCE(to_jsonb(source_row) ->> 'status', '')
                         IN ('archived', 'deleted') THEN 'archived'
                    WHEN to_jsonb(source_row) ->> 'merged_into' IS NOT NULL
                    THEN 'archived'
                    ELSE 'active'
                END,
                COALESCE(
                    to_jsonb(source_row) -> 'metadata',
                    '{{}}'::jsonb
                ),
                source_row.created_at,
                source_row.updated_at
            FROM {table_name} AS source_row
            ON CONFLICT (entity_type, entity_key) DO NOTHING
            """
        )


def backfill_pg_relations() -> None:
    for table_name, entity_type, _label_column in _KNOWLEDGE_TABLES:
        op.execute(
            f"""
            INSERT INTO entity_relations (
                source_entity_id,
                target_entity_id,
                relation_type,
                origin,
                origin_ref,
                confidence
            )
            SELECT
                source_entity.id,
                project_entity.id,
                'BELONGS_TO',
                'postgres',
                '{table_name}.project_key',
                1.0
            FROM {table_name} source_row
            JOIN brain_entities source_entity
              ON source_entity.entity_type = '{entity_type}'
             AND source_entity.source_uuid = source_row.id
            JOIN brain_entities project_entity
              ON project_entity.entity_type = 'project'
             AND project_entity.entity_key = source_row.project_key
            WHERE source_row.project_key IS NOT NULL
              AND source_entity.id <> project_entity.id
            ON CONFLICT (source_entity_id, target_entity_id, relation_type)
            DO NOTHING
            """
        )
    op.execute(
        """
        INSERT INTO entity_relations (
            source_entity_id,
            target_entity_id,
            relation_type,
            origin,
            origin_ref,
            confidence
        )
        SELECT
            newer_entity.id,
            older_entity.id,
            'SUPERSEDES',
            'postgres',
            'decisions.superseded_by',
            1.0
        FROM decisions older_row
        JOIN brain_entities older_entity
          ON older_entity.entity_type = 'decision'
         AND older_entity.source_uuid = older_row.id
        JOIN brain_entities newer_entity
          ON newer_entity.entity_type = 'decision'
         AND newer_entity.source_uuid = older_row.superseded_by
        WHERE older_row.superseded_by IS NOT NULL
          AND newer_entity.id <> older_entity.id
        ON CONFLICT (source_entity_id, target_entity_id, relation_type)
        DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO entity_relations (
            source_entity_id,
            target_entity_id,
            relation_type,
            origin,
            origin_ref,
            confidence
        )
        SELECT
            newer_entity.id,
            older_entity.id,
            'SUPERSEDES',
            'postgres',
            'adrs.superseded_by',
            1.0
        FROM adrs older_row
        JOIN adrs newer_row
          ON newer_row.project_key = older_row.project_key
         AND newer_row.number = older_row.superseded_by
        JOIN brain_entities older_entity
          ON older_entity.entity_type = 'adr'
         AND older_entity.source_uuid = older_row.id
        JOIN brain_entities newer_entity
          ON newer_entity.entity_type = 'adr'
         AND newer_entity.source_uuid = newer_row.id
        WHERE older_row.superseded_by IS NOT NULL
          AND newer_entity.id <> older_entity.id
        ON CONFLICT (source_entity_id, target_entity_id, relation_type)
        DO NOTHING
        """
    )
    for table_name, entity_type, _label_column in _MERGE_TABLES:
        op.execute(
            f"""
            INSERT INTO entity_relations (
                source_entity_id,
                target_entity_id,
                relation_type,
                origin,
                origin_ref,
                confidence
            )
            SELECT
                source_entity.id,
                target_entity.id,
                'MERGED_INTO',
                'postgres',
                '{table_name}.merged_into',
                1.0
            FROM {table_name} source_row
            JOIN brain_entities source_entity
              ON source_entity.entity_type = '{entity_type}'
             AND source_entity.source_uuid = source_row.id
            JOIN brain_entities target_entity
              ON target_entity.entity_type = '{entity_type}'
             AND target_entity.source_uuid = source_row.merged_into
            WHERE source_row.merged_into IS NOT NULL
              AND source_entity.id <> target_entity.id
            ON CONFLICT (source_entity_id, target_entity_id, relation_type)
            DO NOTHING
            """
        )
    op.execute(
        """
        UPDATE entity_relations AS relation
        SET lifecycle = 'archived',
            updated_at = NOW()
        WHERE EXISTS (
            SELECT 1
            FROM brain_entities AS endpoint
            WHERE endpoint.id IN (
                relation.source_entity_id,
                relation.target_entity_id
            )
              AND endpoint.lifecycle = 'deleted'
        )
        """
    )
    op.execute(
        """
        INSERT INTO graph_outbox (entity_id, aggregate_revision, operation)
        SELECT
            id,
            revision,
            CASE WHEN lifecycle = 'deleted' THEN 'delete_entity' ELSE 'upsert_entity' END
        FROM brain_entities
        ON CONFLICT ON CONSTRAINT uq_graph_outbox_entity_revision DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO graph_outbox (relation_id, aggregate_revision, operation)
        SELECT
            relation.id,
            relation.revision,
            CASE
                WHEN relation.lifecycle = 'active' THEN 'upsert_relation'
                ELSE 'delete_relation'
            END
        FROM entity_relations AS relation
        ON CONFLICT ON CONSTRAINT uq_graph_outbox_relation_revision DO NOTHING
        """
    )


def _create_project_alias_normalization() -> None:
    op.execute(
        """
        CREATE FUNCTION normalize_project_key_alias()
        RETURNS trigger AS $$
        DECLARE
            column_name TEXT;
            row_data JSONB := to_jsonb(NEW);
            raw_project_key TEXT;
            canonical_project_key TEXT;
        BEGIN
            FOREACH column_name IN ARRAY TG_ARGV LOOP
                raw_project_key := row_data ->> column_name;
                IF raw_project_key IS NULL THEN
                    CONTINUE;
                END IF;

                SELECT project_key INTO canonical_project_key
                FROM project_aliases
                WHERE alias_key = btrim(raw_project_key);

                row_data := jsonb_set(
                    row_data,
                    ARRAY[column_name],
                    to_jsonb(COALESCE(canonical_project_key, btrim(raw_project_key))),
                    TRUE
                );
            END LOOP;

            NEW := jsonb_populate_record(NEW, row_data);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER project_contexts_project_alias_trigger
        BEFORE INSERT OR UPDATE OF project_key ON project_contexts
        FOR EACH ROW EXECUTE FUNCTION normalize_project_key_alias('project_key')
        """
    )
    for table_name, columns in _PROJECT_ALIAS_TRIGGER_COLUMNS[1:]:
        update_columns = ", ".join(columns)
        trigger_args = ", ".join(f"'{column}'" for column in columns)
        op.execute(
            f"""
            CREATE TRIGGER {table_name}_project_alias_trigger
            BEFORE INSERT OR UPDATE OF {update_columns} ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION normalize_project_key_alias(
                {trigger_args}
            )
            """
        )

    op.execute(
        """
        CREATE FUNCTION normalize_related_project_aliases()
        RETURNS trigger AS $$
        BEGIN
            SELECT COALESCE(
                array_agg(
                    COALESCE(alias.project_key, btrim(reference.project_key))
                    ORDER BY reference.ordinality
                ),
                '{}'::text[]
            )
            INTO NEW.related_projects
            FROM unnest(NEW.related_projects) WITH ORDINALITY
                AS reference(project_key, ordinality)
            LEFT JOIN project_aliases AS alias
              ON alias.alias_key = btrim(reference.project_key);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER project_contexts_related_project_alias_trigger
        BEFORE INSERT OR UPDATE OF related_projects ON project_contexts
        FOR EACH ROW EXECUTE FUNCTION normalize_related_project_aliases()
        """
    )


def _create_reference_registry_sync() -> None:
    op.execute(
        """
        CREATE FUNCTION register_referenced_project(raw_project_key TEXT)
        RETURNS void AS $$
        DECLARE
            referenced_project_key TEXT := NULLIF(btrim(raw_project_key), '');
            registry_id UUID;
            registry_revision BIGINT;
        BEGIN
            IF referenced_project_key IS NULL THEN
                RETURN;
            END IF;

            SELECT id, revision INTO registry_id, registry_revision
            FROM brain_entities
            WHERE entity_type = 'project'
              AND entity_key = referenced_project_key;
            IF registry_id IS NOT NULL THEN
                RETURN;
            END IF;

            INSERT INTO projects (project_key, registry_status, source)
            VALUES (referenced_project_key, 'unclaimed', 'reference')
            ON CONFLICT (project_key) DO NOTHING;

            INSERT INTO brain_entities (
                entity_type,
                entity_key,
                project_key,
                scope_kind,
                display_label,
                lifecycle
            ) VALUES (
                'project',
                referenced_project_key,
                referenced_project_key,
                'project',
                referenced_project_key,
                'active'
            )
            ON CONFLICT (entity_type, entity_key) DO NOTHING;

            SELECT id, revision INTO registry_id, registry_revision
            FROM brain_entities
            WHERE entity_type = 'project'
              AND entity_key = referenced_project_key;

            INSERT INTO graph_outbox (
                entity_id,
                aggregate_revision,
                operation
            ) VALUES (registry_id, registry_revision, 'upsert_entity')
            ON CONFLICT ON CONSTRAINT uq_graph_outbox_entity_revision
            DO NOTHING;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE FUNCTION sync_referenced_project_registry()
        RETURNS trigger AS $$
        DECLARE
            column_name TEXT;
            row_data JSONB := to_jsonb(NEW);
        BEGIN
            FOREACH column_name IN ARRAY TG_ARGV LOOP
                PERFORM register_referenced_project(row_data ->> column_name);
            END LOOP;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table_name, trigger_name, columns in _AUXILIARY_PROJECT_REFERENCE_COLUMNS:
        update_columns = ", ".join(columns)
        trigger_args = ", ".join(f"'{column}'" for column in columns)
        op.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            AFTER INSERT OR UPDATE OF {update_columns} ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION sync_referenced_project_registry(
                {trigger_args}
            )
            """
        )

    op.execute(
        """
        CREATE FUNCTION sync_related_project_registry()
        RETURNS trigger AS $$
        DECLARE
            project_reference TEXT;
        BEGIN
            FOREACH project_reference IN ARRAY COALESCE(
                NEW.related_projects,
                '{}'::text[]
            ) LOOP
                PERFORM register_referenced_project(project_reference);
            END LOOP;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER project_contexts_related_project_registry_trigger
        AFTER INSERT OR UPDATE OF related_projects ON project_contexts
        FOR EACH ROW EXECUTE FUNCTION sync_related_project_registry()
        """
    )


def _create_relation_lifecycle_sync() -> None:
    op.execute(
        """
        CREATE FUNCTION sync_entity_relation_lifecycle(
            registry_entity_id UUID,
            registry_entity_lifecycle VARCHAR
        ) RETURNS void AS $$
        DECLARE
            locked_relation_id UUID;
        BEGIN
            FOR locked_relation_id IN
                SELECT relation.id
                FROM entity_relations AS relation
                WHERE relation.source_entity_id = registry_entity_id
                   OR relation.target_entity_id = registry_entity_id
                ORDER BY relation.id
            LOOP
                PERFORM pg_advisory_xact_lock(
                    hashtextextended(locked_relation_id::text, 420033)
                );
            END LOOP;

            WITH relation_states AS (
                SELECT
                    relation.id,
                    CASE
                        WHEN relation.lifecycle = 'deleted' THEN 'deleted'
                        WHEN registry_entity_lifecycle <> 'deleted'
                         AND source.lifecycle <> 'deleted'
                         AND target.lifecycle <> 'deleted'
                        THEN 'active'
                        ELSE 'archived'
                    END AS next_lifecycle
                FROM entity_relations AS relation
                JOIN brain_entities AS source
                  ON source.id = relation.source_entity_id
                JOIN brain_entities AS target
                  ON target.id = relation.target_entity_id
                WHERE relation.source_entity_id = registry_entity_id
                   OR relation.target_entity_id = registry_entity_id
            ), changed AS (
                UPDATE entity_relations AS relation
                SET lifecycle = relation_states.next_lifecycle,
                    revision = relation.revision + 1,
                    updated_at = NOW(),
                    deleted_at = CASE
                        WHEN relation_states.next_lifecycle = 'active' THEN NULL
                        ELSE NOW()
                    END
                FROM relation_states
                WHERE relation.id = relation_states.id
                  AND relation.lifecycle IS DISTINCT FROM relation_states.next_lifecycle
                RETURNING relation.id, relation.revision, relation.lifecycle
            )
            INSERT INTO graph_outbox (
                relation_id,
                aggregate_revision,
                operation
            )
            SELECT
                id,
                revision,
                CASE
                    WHEN lifecycle = 'active' THEN 'upsert_relation'
                    ELSE 'delete_relation'
                END
            FROM changed
            ON CONFLICT ON CONSTRAINT uq_graph_outbox_relation_revision
            DO NOTHING;
        END;
        $$ LANGUAGE plpgsql
        """
    )


def _create_project_membership_sync() -> None:
    op.execute(
        """
        CREATE FUNCTION sync_entity_project_membership(
            registry_entity_id UUID,
            previous_project_key VARCHAR,
            current_project_key VARCHAR,
            registry_entity_lifecycle VARCHAR
        ) RETURNS void AS $$
        BEGIN
            IF previous_project_key IS NOT NULL
               AND previous_project_key IS DISTINCT FROM current_project_key
            THEN
                WITH changed AS (
                    UPDATE entity_relations AS relation
                    SET lifecycle = 'deleted',
                        revision = relation.revision + 1,
                        updated_at = NOW(),
                        deleted_at = NOW()
                    FROM brain_entities AS project_entity
                    WHERE project_entity.entity_type = 'project'
                      AND project_entity.entity_key = previous_project_key
                      AND relation.source_entity_id = registry_entity_id
                      AND relation.target_entity_id = project_entity.id
                      AND relation.relation_type = 'BELONGS_TO'
                      AND relation.lifecycle IS DISTINCT FROM 'deleted'
                    RETURNING relation.id, relation.revision
                )
                INSERT INTO graph_outbox (
                    relation_id,
                    aggregate_revision,
                    operation
                )
                SELECT id, revision, 'delete_relation'
                FROM changed
                ON CONFLICT ON CONSTRAINT uq_graph_outbox_relation_revision
                DO NOTHING;
            END IF;

            IF current_project_key IS NOT NULL THEN
                WITH project_target AS (
                    SELECT id
                    FROM brain_entities
                    WHERE entity_type = 'project'
                      AND entity_key = current_project_key
                      AND lifecycle = 'active'
                ), changed AS (
                    INSERT INTO entity_relations (
                        source_entity_id,
                        target_entity_id,
                        relation_type,
                        origin,
                        origin_ref,
                        confidence,
                        lifecycle,
                        deleted_at
                    )
                    SELECT
                        registry_entity_id,
                        project_target.id,
                        'BELONGS_TO',
                        'project_membership',
                        'project_key',
                        1.0,
                        CASE
                            WHEN registry_entity_lifecycle <> 'deleted' THEN 'active'
                            ELSE 'archived'
                        END,
                        CASE
                            WHEN registry_entity_lifecycle <> 'deleted' THEN NULL
                            ELSE NOW()
                        END
                    FROM project_target
                    WHERE registry_entity_id <> project_target.id
                    ON CONFLICT (
                        source_entity_id,
                        target_entity_id,
                        relation_type
                    ) DO UPDATE SET
                        lifecycle = EXCLUDED.lifecycle,
                        revision = entity_relations.revision + 1,
                        updated_at = NOW(),
                        deleted_at = EXCLUDED.deleted_at
                    WHERE entity_relations.lifecycle IS DISTINCT FROM EXCLUDED.lifecycle
                    RETURNING id, revision, lifecycle
                )
                INSERT INTO graph_outbox (
                    relation_id,
                    aggregate_revision,
                    operation
                )
                SELECT
                    id,
                    revision,
                    CASE
                        WHEN lifecycle = 'active' THEN 'upsert_relation'
                        ELSE 'delete_relation'
                    END
                FROM changed
                ON CONFLICT ON CONSTRAINT uq_graph_outbox_relation_revision
                DO NOTHING;
            END IF;
        END;
        $$ LANGUAGE plpgsql
        """
    )


def _create_pointer_relation_sync() -> None:
    op.execute(
        """
        CREATE FUNCTION sync_pointer_relation(
            registry_entity_id UUID,
            previous_target_source_uuid UUID,
            current_target_source_uuid UUID,
            pointer_relation_type VARCHAR,
            reverse_direction BOOLEAN,
            pointer_origin_ref TEXT
        ) RETURNS void AS $$
        DECLARE
            previous_target_id UUID;
            current_target_id UUID;
            relation_source_id UUID;
            relation_target_id UUID;
            registry_lifecycle VARCHAR(16);
            target_lifecycle VARCHAR(16);
            desired_lifecycle VARCHAR(16);
        BEGIN
            IF pointer_relation_type NOT IN ('SUPERSEDES', 'MERGED_INTO') THEN
                RAISE EXCEPTION 'unsupported pointer relation: %', pointer_relation_type;
            END IF;

            IF previous_target_source_uuid IS NOT NULL
               AND previous_target_source_uuid IS DISTINCT FROM current_target_source_uuid
            THEN
                SELECT id INTO previous_target_id
                FROM brain_entities
                WHERE source_uuid = previous_target_source_uuid;

                IF previous_target_id IS NOT NULL THEN
                    IF reverse_direction THEN
                        relation_source_id := previous_target_id;
                        relation_target_id := registry_entity_id;
                    ELSE
                        relation_source_id := registry_entity_id;
                        relation_target_id := previous_target_id;
                    END IF;

                    WITH changed AS (
                        UPDATE entity_relations AS relation
                        SET lifecycle = 'deleted',
                            revision = relation.revision + 1,
                            updated_at = NOW(),
                            deleted_at = NOW()
                        WHERE relation.source_entity_id = relation_source_id
                          AND relation.target_entity_id = relation_target_id
                          AND relation.relation_type = pointer_relation_type
                          AND relation.lifecycle IS DISTINCT FROM 'deleted'
                        RETURNING relation.id, relation.revision
                    )
                    INSERT INTO graph_outbox (
                        relation_id,
                        aggregate_revision,
                        operation
                    )
                    SELECT id, revision, 'delete_relation'
                    FROM changed
                    ON CONFLICT ON CONSTRAINT uq_graph_outbox_relation_revision
                    DO NOTHING;
                END IF;
            END IF;

            IF current_target_source_uuid IS NULL THEN
                RETURN;
            END IF;

            SELECT id, lifecycle INTO current_target_id, target_lifecycle
            FROM brain_entities
            WHERE source_uuid = current_target_source_uuid;
            SELECT lifecycle INTO registry_lifecycle
            FROM brain_entities
            WHERE id = registry_entity_id;

            IF current_target_id IS NULL OR current_target_id = registry_entity_id THEN
                RETURN;
            END IF;

            IF reverse_direction THEN
                relation_source_id := current_target_id;
                relation_target_id := registry_entity_id;
            ELSE
                relation_source_id := registry_entity_id;
                relation_target_id := current_target_id;
            END IF;
            desired_lifecycle := CASE
                WHEN registry_lifecycle <> 'deleted' AND target_lifecycle <> 'deleted'
                THEN 'active'
                ELSE 'archived'
            END;

            WITH changed AS (
                INSERT INTO entity_relations (
                    source_entity_id,
                    target_entity_id,
                    relation_type,
                    origin,
                    origin_ref,
                    confidence,
                    lifecycle,
                    deleted_at
                ) VALUES (
                    relation_source_id,
                    relation_target_id,
                    pointer_relation_type,
                    'postgres_pointer',
                    pointer_origin_ref,
                    1.0,
                    desired_lifecycle,
                    CASE WHEN desired_lifecycle = 'active' THEN NULL ELSE NOW() END
                )
                ON CONFLICT (
                    source_entity_id,
                    target_entity_id,
                    relation_type
                ) DO UPDATE SET
                    origin = EXCLUDED.origin,
                    origin_ref = EXCLUDED.origin_ref,
                    confidence = EXCLUDED.confidence,
                    lifecycle = EXCLUDED.lifecycle,
                    revision = entity_relations.revision + 1,
                    updated_at = NOW(),
                    deleted_at = EXCLUDED.deleted_at
                WHERE entity_relations.lifecycle IS DISTINCT FROM EXCLUDED.lifecycle
                   OR entity_relations.origin_ref IS DISTINCT FROM EXCLUDED.origin_ref
                RETURNING id, revision, lifecycle
            )
            INSERT INTO graph_outbox (
                relation_id,
                aggregate_revision,
                operation
            )
            SELECT
                id,
                revision,
                CASE
                    WHEN lifecycle = 'active' THEN 'upsert_relation'
                    ELSE 'delete_relation'
                END
            FROM changed
            ON CONFLICT ON CONSTRAINT uq_graph_outbox_relation_revision
            DO NOTHING;
        END;
        $$ LANGUAGE plpgsql
        """
    )


def _create_project_registry_sync() -> None:
    op.execute(
        """
        CREATE FUNCTION reject_project_context_key_change()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.project_key IS DISTINCT FROM NEW.project_key THEN
                RAISE EXCEPTION 'project_contexts.project_key is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER project_contexts_project_key_immutable_trigger
        BEFORE UPDATE OF project_key ON project_contexts
        FOR EACH ROW EXECUTE FUNCTION reject_project_context_key_change()
        """
    )
    op.execute(
        """
        CREATE FUNCTION sync_project_registry()
        RETURNS trigger AS $$
        DECLARE
            registry_id UUID;
            registry_revision BIGINT;
        BEGIN
            IF TG_OP = 'DELETE'
               OR (
                   TG_OP = 'UPDATE'
                   AND OLD.project_key IS DISTINCT FROM NEW.project_key
               )
            THEN
                UPDATE projects
                SET registry_status = 'unclaimed',
                    source = 'reference',
                    updated_at = NOW()
                WHERE project_key = OLD.project_key;

                UPDATE brain_entities
                SET source_uuid = NULL,
                    revision = revision + 1,
                    updated_at = NOW()
                WHERE entity_type = 'project'
                  AND entity_key = OLD.project_key
                RETURNING id, revision INTO registry_id, registry_revision;

                IF registry_id IS NOT NULL THEN
                    INSERT INTO graph_outbox (
                        entity_id,
                        aggregate_revision,
                        operation
                    ) VALUES (registry_id, registry_revision, 'upsert_entity')
                    ON CONFLICT ON CONSTRAINT uq_graph_outbox_entity_revision
                    DO NOTHING;
                END IF;

                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
            END IF;

            INSERT INTO projects (
                project_key,
                display_name,
                registry_status,
                source,
                metadata,
                created_at,
                updated_at
            ) VALUES (
                NEW.project_key,
                NEW.name,
                'claimed',
                'context',
                NEW.metadata,
                NEW.created_at,
                NEW.updated_at
            )
            ON CONFLICT (project_key) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                registry_status = 'claimed',
                source = 'context',
                metadata = EXCLUDED.metadata,
                updated_at = EXCLUDED.updated_at;

            INSERT INTO brain_entities (
                id,
                entity_type,
                entity_key,
                source_uuid,
                project_key,
                scope_kind,
                display_label,
                lifecycle,
                metadata,
                created_at,
                updated_at
            ) VALUES (
                NEW.id,
                'project',
                NEW.project_key,
                NEW.id,
                NEW.project_key,
                'project',
                NEW.name,
                'active',
                NEW.metadata,
                NEW.created_at,
                NEW.updated_at
            )
            ON CONFLICT (entity_type, entity_key) DO UPDATE SET
                source_uuid = EXCLUDED.source_uuid,
                project_key = EXCLUDED.project_key,
                display_label = EXCLUDED.display_label,
                lifecycle = 'active',
                revision = brain_entities.revision + 1,
                metadata = EXCLUDED.metadata,
                updated_at = EXCLUDED.updated_at,
                deleted_at = NULL
            RETURNING id, revision INTO registry_id, registry_revision;

            INSERT INTO graph_outbox (
                entity_id,
                aggregate_revision,
                operation
            ) VALUES (registry_id, registry_revision, 'upsert_entity')
            ON CONFLICT ON CONSTRAINT uq_graph_outbox_entity_revision
            DO NOTHING;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER project_contexts_brain_registry_trigger
        AFTER INSERT OR DELETE OR UPDATE OF project_key, name, metadata
        ON project_contexts
        FOR EACH ROW EXECUTE FUNCTION sync_project_registry()
        """
    )


def _create_registry_sync() -> None:
    op.execute(
        """
        CREATE FUNCTION sync_brain_entity_registry()
        RETURNS trigger AS $$
        DECLARE
            registry_entity_type VARCHAR(32);
            registry_label TEXT;
            registry_project_key VARCHAR(50);
            previous_project_key VARCHAR(50);
            registry_lifecycle VARCHAR(16);
            registry_id UUID;
            registry_revision BIGINT;
            project_entity_id UUID;
            project_entity_revision BIGINT;
            previous_merged_uuid UUID;
            current_merged_uuid UUID;
            previous_superseder_uuid UUID;
            current_superseder_uuid UUID;
            previous_superseder_number INTEGER;
            current_superseder_number INTEGER;
        BEGIN
            registry_entity_type := CASE TG_TABLE_NAME
                WHEN 'decisions' THEN 'decision'
                WHEN 'learnings' THEN 'learning'
                WHEN 'snippets' THEN 'snippet'
                WHEN 'runbooks' THEN 'runbook'
                WHEN 'adrs' THEN 'adr'
                WHEN 'features' THEN 'feature'
                WHEN 'indexed_plans' THEN 'plan'
                ELSE NULL
            END;

            IF registry_entity_type IS NULL THEN
                RAISE EXCEPTION 'unsupported registry source table: %', TG_TABLE_NAME;
            END IF;

            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                previous_merged_uuid := NULLIF(
                    to_jsonb(OLD) ->> 'merged_into',
                    ''
                )::uuid;
                IF registry_entity_type = 'decision' THEN
                    previous_superseder_uuid := NULLIF(
                        to_jsonb(OLD) ->> 'superseded_by',
                        ''
                    )::uuid;
                ELSIF registry_entity_type = 'adr' THEN
                    previous_superseder_number := NULLIF(
                        to_jsonb(OLD) ->> 'superseded_by',
                        ''
                    )::integer;
                    SELECT id INTO previous_superseder_uuid
                    FROM adrs
                    WHERE project_key = OLD.project_key
                      AND number = previous_superseder_number;
                END IF;
            END IF;

            IF TG_OP <> 'DELETE' THEN
                current_merged_uuid := NULLIF(
                    to_jsonb(NEW) ->> 'merged_into',
                    ''
                )::uuid;
                IF registry_entity_type = 'decision' THEN
                    current_superseder_uuid := NULLIF(
                        to_jsonb(NEW) ->> 'superseded_by',
                        ''
                    )::uuid;
                ELSIF registry_entity_type = 'adr' THEN
                    current_superseder_number := NULLIF(
                        to_jsonb(NEW) ->> 'superseded_by',
                        ''
                    )::integer;
                    SELECT id INTO current_superseder_uuid
                    FROM adrs
                    WHERE project_key = NEW.project_key
                      AND number = current_superseder_number;
                END IF;
            END IF;

            IF TG_OP = 'DELETE' THEN
                UPDATE brain_entities
                SET lifecycle = 'deleted',
                    revision = revision + 1,
                    updated_at = NOW(),
                    deleted_at = NOW()
                WHERE entity_type = registry_entity_type
                  AND source_uuid = OLD.id
                RETURNING id, revision INTO registry_id, registry_revision;

                IF registry_id IS NOT NULL THEN
                    INSERT INTO graph_outbox (
                        entity_id,
                        aggregate_revision,
                        operation
                    ) VALUES (registry_id, registry_revision, 'delete_entity')
                    ON CONFLICT ON CONSTRAINT uq_graph_outbox_entity_revision
                    DO NOTHING;
                    PERFORM sync_entity_project_membership(
                        registry_id,
                        OLD.project_key,
                        NULL,
                        'deleted'
                    );
                    PERFORM sync_pointer_relation(
                        registry_id,
                        previous_merged_uuid,
                        NULL,
                        'MERGED_INTO',
                        FALSE,
                        TG_TABLE_NAME || '.merged_into'
                    );
                    IF registry_entity_type IN ('decision', 'adr') THEN
                        PERFORM sync_pointer_relation(
                            registry_id,
                            previous_superseder_uuid,
                            NULL,
                            'SUPERSEDES',
                            TRUE,
                            TG_TABLE_NAME || '.superseded_by'
                        );
                    END IF;
                    PERFORM sync_entity_relation_lifecycle(registry_id, 'deleted');
                END IF;
                RETURN OLD;
            END IF;

            registry_project_key := NEW.project_key;
            IF TG_OP = 'UPDATE' THEN
                previous_project_key := OLD.project_key;
            ELSE
                previous_project_key := NULL;
            END IF;
            registry_lifecycle := CASE
                WHEN COALESCE(
                    to_jsonb(NEW) ->> 'freshness_status',
                    'fresh'
                ) = 'archived' THEN 'archived'
                WHEN COALESCE(to_jsonb(NEW) ->> 'status', '') = 'deleted'
                THEN 'deleted'
                WHEN COALESCE(to_jsonb(NEW) ->> 'status', '') = 'archived'
                THEN 'archived'
                WHEN to_jsonb(NEW) ->> 'merged_into' IS NOT NULL THEN 'archived'
                ELSE 'active'
            END;
            registry_label := COALESCE(
                to_jsonb(NEW) ->> 'topic',
                to_jsonb(NEW) ->> 'title',
                to_jsonb(NEW) ->> 'name'
            );

            IF registry_project_key IS NOT NULL THEN
                INSERT INTO projects (project_key, registry_status, source)
                VALUES (registry_project_key, 'unclaimed', 'reference')
                ON CONFLICT (project_key) DO NOTHING;

                INSERT INTO brain_entities (
                    entity_type,
                    entity_key,
                    project_key,
                    scope_kind,
                    display_label,
                    lifecycle
                ) VALUES (
                    'project',
                    registry_project_key,
                    registry_project_key,
                    'project',
                    registry_project_key,
                    'active'
                )
                ON CONFLICT (entity_type, entity_key) DO NOTHING;

                SELECT id, revision
                INTO project_entity_id, project_entity_revision
                FROM brain_entities
                WHERE entity_type = 'project'
                  AND entity_key = registry_project_key;

                INSERT INTO graph_outbox (
                    entity_id,
                    aggregate_revision,
                    operation
                ) VALUES (
                    project_entity_id,
                    project_entity_revision,
                    'upsert_entity'
                )
                ON CONFLICT ON CONSTRAINT uq_graph_outbox_entity_revision
                DO NOTHING;
            END IF;

            INSERT INTO brain_entities (
                id,
                entity_type,
                entity_key,
                source_uuid,
                project_key,
                scope_kind,
                display_label,
                lifecycle,
                revision,
                metadata,
                created_at,
                updated_at,
                deleted_at
            ) VALUES (
                NEW.id,
                registry_entity_type,
                NEW.id::text,
                NEW.id,
                registry_project_key,
                CASE
                    WHEN registry_project_key IS NULL THEN 'global'
                    ELSE 'project'
                END,
                registry_label,
                registry_lifecycle,
                1,
                COALESCE(to_jsonb(NEW) -> 'metadata', '{}'::jsonb),
                NEW.created_at,
                NEW.updated_at,
                NULL
            )
            ON CONFLICT (entity_type, entity_key) DO UPDATE SET
                source_uuid = EXCLUDED.source_uuid,
                project_key = EXCLUDED.project_key,
                scope_kind = EXCLUDED.scope_kind,
                display_label = EXCLUDED.display_label,
                lifecycle = EXCLUDED.lifecycle,
                revision = brain_entities.revision + 1,
                metadata = EXCLUDED.metadata,
                updated_at = EXCLUDED.updated_at,
                deleted_at = NULL
            RETURNING id, revision INTO registry_id, registry_revision;

            INSERT INTO graph_outbox (
                entity_id,
                aggregate_revision,
                operation
            ) VALUES (
                registry_id,
                registry_revision,
                CASE
                    WHEN registry_lifecycle = 'deleted' THEN 'delete_entity'
                    ELSE 'upsert_entity'
                END
            )
            ON CONFLICT ON CONSTRAINT uq_graph_outbox_entity_revision
            DO NOTHING;

            PERFORM sync_entity_project_membership(
                registry_id,
                previous_project_key,
                registry_project_key,
                registry_lifecycle
            );
            PERFORM sync_pointer_relation(
                registry_id,
                previous_merged_uuid,
                current_merged_uuid,
                'MERGED_INTO',
                FALSE,
                TG_TABLE_NAME || '.merged_into'
            );
            IF registry_entity_type IN ('decision', 'adr') THEN
                PERFORM sync_pointer_relation(
                    registry_id,
                    previous_superseder_uuid,
                    current_superseder_uuid,
                    'SUPERSEDES',
                    TRUE,
                    TG_TABLE_NAME || '.superseded_by'
                );
            END IF;
            PERFORM sync_entity_relation_lifecycle(registry_id, registry_lifecycle);

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table_name, _entity_type, _label_column in _KNOWLEDGE_TABLES:
        update_columns = ", ".join(_REGISTRY_TRIGGER_COLUMNS[table_name])
        op.execute(
            f"""
            CREATE TRIGGER {table_name}_brain_entity_registry_trigger
            AFTER INSERT OR DELETE OR UPDATE OF {update_columns} ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION sync_brain_entity_registry()
            """
        )


def upgrade() -> None:
    _lock_graph_source_tables()
    _create_tables()
    normalize_known_project_aliases()
    backfill_projects()
    backfill_project_aliases()
    backfill_brain_entities()
    backfill_pg_relations()
    _create_project_alias_normalization()
    _create_reference_registry_sync()
    _create_relation_lifecycle_sync()
    _create_project_membership_sync()
    _create_pointer_relation_sync()
    _create_project_registry_sync()
    _create_registry_sync()


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS project_contexts_related_project_registry_trigger "
        "ON project_contexts"
    )
    for table_name, trigger_name, _columns in reversed(_AUXILIARY_PROJECT_REFERENCE_COLUMNS):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}")
    op.execute(
        "DROP TRIGGER IF EXISTS project_contexts_project_key_immutable_trigger ON project_contexts"
    )
    op.execute("DROP TRIGGER IF EXISTS project_contexts_brain_registry_trigger ON project_contexts")
    for table_name, _entity_type, _label_column in reversed(_KNOWLEDGE_TABLES):
        op.execute(
            f"DROP TRIGGER IF EXISTS {table_name}_brain_entity_registry_trigger ON {table_name}"
        )
    op.execute(
        "DROP TRIGGER IF EXISTS project_contexts_related_project_alias_trigger ON project_contexts"
    )
    for table_name, _columns in reversed(_PROJECT_ALIAS_TRIGGER_COLUMNS):
        op.execute(f"DROP TRIGGER IF EXISTS {table_name}_project_alias_trigger ON {table_name}")
    op.execute("DROP FUNCTION IF EXISTS sync_brain_entity_registry()")
    op.execute("DROP FUNCTION IF EXISTS sync_project_registry()")
    op.execute("DROP FUNCTION IF EXISTS reject_project_context_key_change()")
    op.execute("DROP FUNCTION IF EXISTS sync_related_project_registry()")
    op.execute("DROP FUNCTION IF EXISTS sync_referenced_project_registry()")
    op.execute("DROP FUNCTION IF EXISTS register_referenced_project(TEXT)")
    op.execute("DROP FUNCTION IF EXISTS normalize_related_project_aliases()")
    op.execute("DROP FUNCTION IF EXISTS normalize_project_key_alias()")
    op.execute(
        "DROP FUNCTION IF EXISTS sync_pointer_relation(UUID, UUID, UUID, VARCHAR, BOOLEAN, TEXT)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS sync_entity_project_membership(UUID, VARCHAR, VARCHAR, VARCHAR)"
    )
    op.execute("DROP FUNCTION IF EXISTS sync_entity_relation_lifecycle(UUID, VARCHAR)")
    op.execute("DROP TABLE IF EXISTS graph_outbox")
    op.execute("DROP TABLE IF EXISTS entity_relations")
    op.execute("DROP TABLE IF EXISTS brain_entities")
    op.execute("DROP TABLE IF EXISTS project_aliases")
    op.execute("DROP TABLE IF EXISTS projects")
