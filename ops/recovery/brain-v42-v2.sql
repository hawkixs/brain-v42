WITH expected_tables(table_name) AS (
    VALUES
        ('access_log'),
        ('adrs'),
        ('alembic_version'),
        ('brain_entities'),
        ('brain_sessions'),
        ('consolidation_log'),
        ('decisions'),
        ('dream_promotions'),
        ('dream_runs'),
        ('entity_relations'),
        ('feature_artifacts'),
        ('features'),
        ('gitlab_events'),
        ('graph_outbox'),
        ('graph_projection_leases'),
        ('indexed_plan_chunks'),
        ('indexed_plans'),
        ('learnings'),
        ('metrics_timeseries'),
        ('process_metrics'),
        ('project_aliases'),
        ('project_contexts'),
        ('projects'),
        ('roadmap_curation_proposals'),
        ('runbooks'),
        ('search_log'),
        ('snippets'),
        ('ticket_extraction_proposals'),
        ('ticket_messages'),
        ('tickets')
),
table_sets AS (
    SELECT
        (SELECT jsonb_agg(table_name ORDER BY table_name) FROM expected_tables) AS expected,
        (
            SELECT COALESCE(jsonb_agg(tablename ORDER BY tablename), '[]'::jsonb)
            FROM pg_catalog.pg_tables
            WHERE schemaname = 'public'
        ) AS observed
),
head_observation AS (
    SELECT CASE WHEN count(*) = 1 THEN max(version_num) END AS value
    FROM public.alembic_version
),
extension_observation AS (
    SELECT max(extversion) AS value
    FROM pg_catalog.pg_extension
    WHERE extname = 'vector'
),
catalog_counts AS (
    SELECT
        jsonb_build_object(
            'foreign_keys', 24,
            'indexes', 123,
            'invalid_indexes', 0,
            'unvalidated_constraints', 0
        ) AS expected,
        jsonb_build_object(
            'foreign_keys', (
                SELECT count(*)
                FROM pg_catalog.pg_constraint AS constraint_record
                JOIN pg_catalog.pg_namespace AS namespace_record
                    ON namespace_record.oid = constraint_record.connamespace
                WHERE namespace_record.nspname = 'public'
                  AND constraint_record.contype = 'f'
            ),
            'indexes', (
                SELECT count(*)
                FROM pg_catalog.pg_index AS index_record
                JOIN pg_catalog.pg_class AS table_record
                    ON table_record.oid = index_record.indrelid
                JOIN pg_catalog.pg_namespace AS namespace_record
                    ON namespace_record.oid = table_record.relnamespace
                WHERE namespace_record.nspname = 'public'
                  AND table_record.relkind IN ('r', 'p')
            ),
            'invalid_indexes', (
                SELECT count(*)
                FROM pg_catalog.pg_index AS index_record
                JOIN pg_catalog.pg_class AS table_record
                    ON table_record.oid = index_record.indrelid
                JOIN pg_catalog.pg_namespace AS namespace_record
                    ON namespace_record.oid = table_record.relnamespace
                WHERE namespace_record.nspname = 'public'
                  AND table_record.relkind IN ('r', 'p')
                  AND (NOT index_record.indisvalid OR NOT index_record.indisready)
            ),
            'unvalidated_constraints', (
                SELECT count(*)
                FROM pg_catalog.pg_constraint AS constraint_record
                JOIN pg_catalog.pg_namespace AS namespace_record
                    ON namespace_record.oid = constraint_record.connamespace
                WHERE namespace_record.nspname = 'public'
                  AND NOT constraint_record.convalidated
            )
        ) AS observed
),
row_counts AS (
    SELECT
        (SELECT count(*) FROM public.adrs)
            + (SELECT count(*) FROM public.decisions)
            + (SELECT count(*) FROM public.learnings)
            + (SELECT count(*) FROM public.runbooks)
            + (SELECT count(*) FROM public.snippets) AS corpus,
        (SELECT count(*) FROM public.features) AS features,
        (SELECT count(*) FROM public.indexed_plan_chunks) AS indexed_plan_chunks,
        (SELECT count(*) FROM public.indexed_plans) AS indexed_plans,
        (SELECT count(*) FROM public.entity_relations) AS graph_relations,
        (SELECT count(*) FROM public.project_contexts) AS project_contexts
),
vector_types AS (
    SELECT
        table_record.relname AS table_name,
        pg_catalog.format_type(attribute_record.atttypid, attribute_record.atttypmod) AS value
    FROM pg_catalog.pg_attribute AS attribute_record
    JOIN pg_catalog.pg_class AS table_record
        ON table_record.oid = attribute_record.attrelid
    JOIN pg_catalog.pg_namespace AS namespace_record
        ON namespace_record.oid = table_record.relnamespace
    WHERE namespace_record.nspname = 'public'
      AND table_record.relname IN (
          'adrs',
          'decisions',
          'features',
          'gitlab_events',
          'indexed_plan_chunks',
          'indexed_plans',
          'learnings',
          'runbooks',
          'snippets'
      )
      AND attribute_record.attname = 'embedding'
      AND attribute_record.attnum > 0
      AND NOT attribute_record.attisdropped
),
vector_observations(table_name, observed) AS (
    SELECT
        'adrs',
        jsonb_build_object(
            'invalid_dimension_rows', count(*) FILTER (
                WHERE embedding IS NOT NULL AND public.vector_dims(embedding) <> 1536
            ),
            'type', (SELECT value FROM vector_types WHERE table_name = 'adrs')
        )
    FROM public.adrs
    UNION ALL
    SELECT
        'decisions',
        jsonb_build_object(
            'invalid_dimension_rows', count(*) FILTER (
                WHERE embedding IS NOT NULL AND public.vector_dims(embedding) <> 1536
            ),
            'type', (SELECT value FROM vector_types WHERE table_name = 'decisions')
        )
    FROM public.decisions
    UNION ALL
    SELECT
        'features',
        jsonb_build_object(
            'invalid_dimension_rows', count(*) FILTER (
                WHERE embedding IS NOT NULL AND public.vector_dims(embedding) <> 1536
            ),
            'type', (SELECT value FROM vector_types WHERE table_name = 'features')
        )
    FROM public.features
    UNION ALL
    SELECT
        'gitlab_events',
        jsonb_build_object(
            'invalid_dimension_rows', count(*) FILTER (
                WHERE embedding IS NOT NULL AND public.vector_dims(embedding) <> 1536
            ),
            'type', (SELECT value FROM vector_types WHERE table_name = 'gitlab_events')
        )
    FROM public.gitlab_events
    UNION ALL
    SELECT
        'indexed_plan_chunks',
        jsonb_build_object(
            'invalid_dimension_rows', count(*) FILTER (
                WHERE embedding IS NOT NULL AND public.vector_dims(embedding) <> 1536
            ),
            'type', (SELECT value FROM vector_types WHERE table_name = 'indexed_plan_chunks')
        )
    FROM public.indexed_plan_chunks
    UNION ALL
    SELECT
        'indexed_plans',
        jsonb_build_object(
            'invalid_dimension_rows', count(*) FILTER (
                WHERE embedding IS NOT NULL AND public.vector_dims(embedding) <> 1536
            ),
            'type', (SELECT value FROM vector_types WHERE table_name = 'indexed_plans')
        )
    FROM public.indexed_plans
    UNION ALL
    SELECT
        'learnings',
        jsonb_build_object(
            'invalid_dimension_rows', count(*) FILTER (
                WHERE embedding IS NOT NULL AND public.vector_dims(embedding) <> 1536
            ),
            'type', (SELECT value FROM vector_types WHERE table_name = 'learnings')
        )
    FROM public.learnings
    UNION ALL
    SELECT
        'runbooks',
        jsonb_build_object(
            'invalid_dimension_rows', count(*) FILTER (
                WHERE embedding IS NOT NULL AND public.vector_dims(embedding) <> 1536
            ),
            'type', (SELECT value FROM vector_types WHERE table_name = 'runbooks')
        )
    FROM public.runbooks
    UNION ALL
    SELECT
        'snippets',
        jsonb_build_object(
            'invalid_dimension_rows', count(*) FILTER (
                WHERE embedding IS NOT NULL AND public.vector_dims(embedding) <> 1536
            ),
            'type', (SELECT value FROM vector_types WHERE table_name = 'snippets')
        )
    FROM public.snippets
),
orphan_counts AS (
    SELECT
        (
            SELECT count(*)
            FROM public.feature_artifacts AS child_record
            LEFT JOIN public.features AS parent_record
                ON parent_record.id = child_record.feature_id
            WHERE parent_record.id IS NULL
        ) AS feature_artifacts,
        (
            SELECT count(*)
            FROM public.indexed_plan_chunks AS child_record
            LEFT JOIN public.indexed_plans AS parent_record
                ON parent_record.id = child_record.plan_id
            WHERE parent_record.id IS NULL
        ) AS indexed_plan_chunks
),
brain_sessions_032_observation AS (
    SELECT (
        (
            SELECT jsonb_agg(constraint_record.conname ORDER BY constraint_record.conname)
            FROM pg_catalog.pg_constraint AS constraint_record
            JOIN pg_catalog.pg_class AS table_record
              ON table_record.oid = constraint_record.conrelid
            JOIN pg_catalog.pg_namespace AS namespace_record
              ON namespace_record.oid = table_record.relnamespace
            WHERE namespace_record.nspname = 'public'
              AND table_record.relname = 'brain_sessions'
              AND constraint_record.convalidated
        ) = jsonb_build_array(
            'brain_sessions_capture_ids_valid',
            'brain_sessions_client_key_nonblank',
            'brain_sessions_pkey',
            'brain_sessions_project_key_fkey',
            'brain_sessions_status_valid',
            'brain_sessions_terminal_state_valid',
            'uq_brain_sessions_project_client'
        )
        AND EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'project_contexts'
              AND column_name = 'focus_revision'
              AND data_type = 'bigint'
              AND is_nullable = 'NO'
              AND column_default = '0'
        )
        AND EXISTS (
            SELECT 1
            FROM pg_catalog.pg_trigger AS trigger_record
            JOIN pg_catalog.pg_class AS table_record
              ON table_record.oid = trigger_record.tgrelid
            JOIN pg_catalog.pg_namespace AS namespace_record
              ON namespace_record.oid = table_record.relnamespace
            JOIN pg_catalog.pg_proc AS function_record
              ON function_record.oid = trigger_record.tgfoid
            WHERE namespace_record.nspname = 'public'
              AND table_record.relname = 'project_contexts'
              AND trigger_record.tgname = 'project_contexts_focus_revision_trigger'
              AND function_record.proname = 'increment_project_focus_revision'
              AND trigger_record.tgenabled = 'O'
              AND NOT trigger_record.tgisinternal
        )
        AND NOT EXISTS (
            SELECT 1 FROM public.project_contexts WHERE focus_revision < 0
        )
        AND NOT EXISTS (
            SELECT 1
            FROM public.brain_sessions AS session_record
            JOIN public.project_contexts AS project_context
              ON project_context.project_key = session_record.project_key
            WHERE session_record.started_focus_revision < 0
               OR session_record.started_focus_revision > project_context.focus_revision
        )
    ) AS passed
),
expected_033_triggers(table_name, trigger_name, function_name) AS (
    VALUES
        ('adrs', 'adrs_brain_entity_registry_trigger', 'sync_brain_entity_registry'),
        ('adrs', 'adrs_project_alias_trigger', 'normalize_project_key_alias'),
        ('brain_sessions', 'brain_sessions_project_alias_trigger', 'normalize_project_key_alias'),
        ('brain_sessions', 'brain_sessions_project_registry_trigger', 'sync_referenced_project_registry'),
        ('decisions', 'decisions_brain_entity_registry_trigger', 'sync_brain_entity_registry'),
        ('decisions', 'decisions_project_alias_trigger', 'normalize_project_key_alias'),
        ('features', 'features_brain_entity_registry_trigger', 'sync_brain_entity_registry'),
        ('features', 'features_project_alias_trigger', 'normalize_project_key_alias'),
        ('gitlab_events', 'gitlab_events_project_alias_trigger', 'normalize_project_key_alias'),
        ('gitlab_events', 'gitlab_events_project_registry_trigger', 'sync_referenced_project_registry'),
        ('indexed_plan_chunks', 'indexed_plan_chunks_project_alias_trigger', 'normalize_project_key_alias'),
        ('indexed_plan_chunks', 'indexed_plan_chunks_project_registry_trigger', 'sync_referenced_project_registry'),
        ('indexed_plans', 'indexed_plans_brain_entity_registry_trigger', 'sync_brain_entity_registry'),
        ('indexed_plans', 'indexed_plans_project_alias_trigger', 'normalize_project_key_alias'),
        ('learnings', 'learnings_brain_entity_registry_trigger', 'sync_brain_entity_registry'),
        ('learnings', 'learnings_project_alias_trigger', 'normalize_project_key_alias'),
        ('project_contexts', 'project_contexts_brain_registry_trigger', 'sync_project_registry'),
        ('project_contexts', 'project_contexts_project_alias_trigger', 'normalize_project_key_alias'),
        ('project_contexts', 'project_contexts_project_key_immutable_trigger', 'reject_project_context_key_change'),
        ('project_contexts', 'project_contexts_related_project_alias_trigger', 'normalize_related_project_aliases'),
        ('project_contexts', 'project_contexts_related_project_registry_trigger', 'sync_related_project_registry'),
        ('runbooks', 'runbooks_brain_entity_registry_trigger', 'sync_brain_entity_registry'),
        ('runbooks', 'runbooks_project_alias_trigger', 'normalize_project_key_alias'),
        ('search_log', 'search_log_project_alias_trigger', 'normalize_project_key_alias'),
        ('search_log', 'search_log_project_registry_trigger', 'sync_referenced_project_registry'),
        ('snippets', 'snippets_brain_entity_registry_trigger', 'sync_brain_entity_registry'),
        ('snippets', 'snippets_project_alias_trigger', 'normalize_project_key_alias'),
        ('ticket_extraction_proposals', 'ticket_extraction_proposals_project_alias_trigger', 'normalize_project_key_alias'),
        ('ticket_extraction_proposals', 'ticket_extraction_proposals_project_registry_trigger', 'sync_referenced_project_registry'),
        ('ticket_messages', 'ticket_messages_project_alias_trigger', 'normalize_project_key_alias'),
        ('ticket_messages', 'ticket_messages_project_registry_trigger', 'sync_referenced_project_registry'),
        ('tickets', 'tickets_project_alias_trigger', 'normalize_project_key_alias'),
        ('tickets', 'tickets_project_registry_trigger', 'sync_referenced_project_registry')
),
source_entities(entity_type, source_uuid) AS (
    SELECT 'decision', id FROM public.decisions
    UNION ALL SELECT 'learning', id FROM public.learnings
    UNION ALL SELECT 'snippet', id FROM public.snippets
    UNION ALL SELECT 'runbook', id FROM public.runbooks
    UNION ALL SELECT 'adr', id FROM public.adrs
    UNION ALL SELECT 'feature', id FROM public.features
    UNION ALL SELECT 'plan', id FROM public.indexed_plans
),
graph_foundation_033_observation AS (
    SELECT (
        (SELECT count(*) FROM expected_033_triggers) = 33
        AND NOT EXISTS (
            SELECT 1
            FROM expected_033_triggers AS expected_trigger
            LEFT JOIN pg_catalog.pg_namespace AS namespace_record
              ON namespace_record.nspname = 'public'
            LEFT JOIN pg_catalog.pg_class AS table_record
              ON table_record.relnamespace = namespace_record.oid
             AND table_record.relname = expected_trigger.table_name
            LEFT JOIN pg_catalog.pg_trigger AS trigger_record
              ON trigger_record.tgrelid = table_record.oid
             AND trigger_record.tgname = expected_trigger.trigger_name
             AND NOT trigger_record.tgisinternal
            LEFT JOIN pg_catalog.pg_proc AS function_record
              ON function_record.oid = trigger_record.tgfoid
             AND function_record.proname = expected_trigger.function_name
            WHERE trigger_record.oid IS NULL
               OR function_record.oid IS NULL
               OR trigger_record.tgenabled <> 'O'
        )
        AND EXISTS (
            SELECT 1
            FROM pg_catalog.pg_constraint AS constraint_record
            JOIN pg_catalog.pg_class AS table_record
              ON table_record.oid = constraint_record.conrelid
            JOIN pg_catalog.pg_namespace AS namespace_record
              ON namespace_record.oid = table_record.relnamespace
            WHERE namespace_record.nspname = 'public'
              AND table_record.relname = 'projects'
              AND constraint_record.conname = 'projects_key_format_valid'
              AND constraint_record.convalidated
        )
        AND NOT EXISTS (
            SELECT 1
            FROM public.projects
            WHERE project_key !~ '^[a-z0-9]+([:-][a-z0-9]+)*$'
        )
        AND NOT EXISTS (
            SELECT 1
            FROM source_entities AS source_record
            LEFT JOIN public.brain_entities AS entity_record
              ON entity_record.source_uuid = source_record.source_uuid
             AND entity_record.entity_type = source_record.entity_type
            WHERE entity_record.id IS NULL
        )
        AND NOT EXISTS (
            SELECT 1
            FROM public.projects AS project_record
            LEFT JOIN public.brain_entities AS entity_record
              ON entity_record.entity_type = 'project'
             AND entity_record.entity_key = project_record.project_key
             AND entity_record.project_key = project_record.project_key
            WHERE entity_record.id IS NULL
        )
        AND (
            SELECT count(*)
            FROM public.brain_entities
            WHERE entity_type = 'domain'
              AND entity_key IN (
                  'backend', 'data', 'frontend', 'infra', 'memory',
                  'ml', 'ops', 'security', 'tooling'
              )
              AND scope_kind = 'global'
              AND project_key IS NULL
              AND lifecycle <> 'deleted'
        ) = 9
        AND NOT EXISTS (
            SELECT 1
            FROM public.entity_relations AS relation_record
            JOIN public.brain_entities AS source_record
              ON source_record.id = relation_record.source_entity_id
            JOIN public.brain_entities AS target_record
              ON target_record.id = relation_record.target_entity_id
            WHERE relation_record.lifecycle = 'active'
              AND (
                  source_record.lifecycle = 'deleted'
                  OR target_record.lifecycle = 'deleted'
              )
        )
    ) AS passed
),
expected_projection_columns(
    table_name,
    column_name,
    data_type,
    is_nullable,
    character_maximum_length,
    column_default
) AS (
    VALUES
        ('graph_outbox', 'claim_version', 'bigint', 'NO', NULL::integer, '0'),
        ('graph_outbox', 'lease_generation', 'bigint', 'YES', NULL::integer, NULL::text),
        ('graph_projection_leases', 'generation', 'bigint', 'NO', NULL::integer, '0'),
        ('graph_projection_leases', 'last_completed_recovery_id', 'uuid', 'YES', NULL::integer, NULL::text),
        ('graph_projection_leases', 'leased_until', 'timestamp with time zone', 'YES', NULL::integer, NULL::text),
        ('graph_projection_leases', 'neo4j_armed_generation', 'bigint', 'YES', NULL::integer, NULL::text),
        ('graph_projection_leases', 'owner', 'character varying', 'YES', 128, NULL::text),
        ('graph_projection_leases', 'protocol_version', 'integer', 'NO', NULL::integer, '2'),
        ('graph_projection_leases', 'recovery_id', 'uuid', 'YES', NULL::integer, NULL::text),
        ('graph_projection_leases', 'recovery_phase', 'character varying', 'NO', 16, '''idle''::character varying'),
        ('graph_projection_leases', 'slot', 'character varying', 'NO', 32, NULL::text),
        ('graph_projection_leases', 'updated_at', 'timestamp with time zone', 'NO', NULL::integer, 'now()')
),
expected_projection_constraints(constraint_name) AS (
    VALUES
        ('graph_projection_leases_armed_generation_valid'),
        ('graph_projection_leases_pkey'),
        ('graph_projection_leases_protocol_valid'),
        ('graph_projection_leases_recovery_state_valid')
),
graph_projection_034_035_observation AS (
    SELECT (
        NOT EXISTS (
            SELECT 1
            FROM expected_projection_columns AS expected_column
            LEFT JOIN information_schema.columns AS observed_column
              ON observed_column.table_schema = 'public'
             AND observed_column.table_name = expected_column.table_name
             AND observed_column.column_name = expected_column.column_name
             AND observed_column.data_type = expected_column.data_type
             AND observed_column.is_nullable = expected_column.is_nullable
             AND observed_column.character_maximum_length
                 IS NOT DISTINCT FROM expected_column.character_maximum_length
             AND observed_column.column_default
                 IS NOT DISTINCT FROM expected_column.column_default
            WHERE observed_column.column_name IS NULL
        )
        AND NOT EXISTS (
            SELECT 1
            FROM expected_projection_constraints AS expected_constraint
            LEFT JOIN pg_catalog.pg_constraint AS constraint_record
              ON constraint_record.conname = expected_constraint.constraint_name
             AND constraint_record.conrelid = 'public.graph_projection_leases'::regclass
             AND constraint_record.convalidated
            WHERE constraint_record.oid IS NULL
        )
        AND (
            SELECT count(*) FROM public.graph_projection_leases
        ) = 1
        AND NOT EXISTS (
            SELECT 1
            FROM public.graph_projection_leases
            WHERE slot <> 'neo4j'
               OR protocol_version <> 2
               OR generation < 0
               OR (
                   neo4j_armed_generation IS NOT NULL
                   AND neo4j_armed_generation <> generation
               )
               OR (owner IS NULL AND leased_until IS NOT NULL)
               OR (owner IS NOT NULL AND leased_until IS NULL)
        )
        AND NOT EXISTS (
            SELECT 1
            FROM public.graph_outbox
            WHERE claim_version < 0
               OR lease_generation < 0
               OR (lease_owner IS NULL AND leased_until IS NOT NULL)
               OR (
                   lease_owner IS NOT NULL
                   AND (leased_until IS NULL OR lease_generation IS NULL)
               )
        )
    ) AS passed
),
check_rows(id, expected, observed, passed) AS (
    SELECT
        'alembic_head',
        to_jsonb('035'::text),
        to_jsonb(head_observation.value),
        head_observation.value = '035'
    FROM head_observation
    UNION ALL
    SELECT
        'brain_sessions_032',
        to_jsonb(TRUE),
        to_jsonb(brain_sessions_032_observation.passed),
        brain_sessions_032_observation.passed
    FROM brain_sessions_032_observation
    UNION ALL
    SELECT
        'catalog_counts',
        catalog_counts.expected,
        catalog_counts.observed,
        catalog_counts.expected = catalog_counts.observed
    FROM catalog_counts
    UNION ALL
    SELECT
        'corpus_nonempty',
        jsonb_build_object('minimum', 1),
        jsonb_build_object('count', row_counts.corpus),
        row_counts.corpus >= 1
    FROM row_counts
    UNION ALL
    SELECT
        'embedding_adrs',
        jsonb_build_object('invalid_dimension_rows', 0, 'type', 'vector(1536)'),
        vector_observations.observed,
        vector_observations.observed = jsonb_build_object(
            'invalid_dimension_rows', 0, 'type', 'vector(1536)'
        )
    FROM vector_observations
    WHERE table_name = 'adrs'
    UNION ALL
    SELECT
        'embedding_decisions',
        jsonb_build_object('invalid_dimension_rows', 0, 'type', 'vector(1536)'),
        vector_observations.observed,
        vector_observations.observed = jsonb_build_object(
            'invalid_dimension_rows', 0, 'type', 'vector(1536)'
        )
    FROM vector_observations
    WHERE table_name = 'decisions'
    UNION ALL
    SELECT
        'embedding_features',
        jsonb_build_object('invalid_dimension_rows', 0, 'type', 'vector(1536)'),
        vector_observations.observed,
        vector_observations.observed = jsonb_build_object(
            'invalid_dimension_rows', 0, 'type', 'vector(1536)'
        )
    FROM vector_observations
    WHERE table_name = 'features'
    UNION ALL
    SELECT
        'embedding_gitlab_events',
        jsonb_build_object('invalid_dimension_rows', 0, 'type', 'vector(1536)'),
        vector_observations.observed,
        vector_observations.observed = jsonb_build_object(
            'invalid_dimension_rows', 0, 'type', 'vector(1536)'
        )
    FROM vector_observations
    WHERE table_name = 'gitlab_events'
    UNION ALL
    SELECT
        'embedding_indexed_plan_chunks',
        jsonb_build_object('invalid_dimension_rows', 0, 'type', 'vector(1536)'),
        vector_observations.observed,
        vector_observations.observed = jsonb_build_object(
            'invalid_dimension_rows', 0, 'type', 'vector(1536)'
        )
    FROM vector_observations
    WHERE table_name = 'indexed_plan_chunks'
    UNION ALL
    SELECT
        'embedding_indexed_plans',
        jsonb_build_object('invalid_dimension_rows', 0, 'type', 'vector(1536)'),
        vector_observations.observed,
        vector_observations.observed = jsonb_build_object(
            'invalid_dimension_rows', 0, 'type', 'vector(1536)'
        )
    FROM vector_observations
    WHERE table_name = 'indexed_plans'
    UNION ALL
    SELECT
        'embedding_learnings',
        jsonb_build_object('invalid_dimension_rows', 0, 'type', 'vector(1536)'),
        vector_observations.observed,
        vector_observations.observed = jsonb_build_object(
            'invalid_dimension_rows', 0, 'type', 'vector(1536)'
        )
    FROM vector_observations
    WHERE table_name = 'learnings'
    UNION ALL
    SELECT
        'embedding_runbooks',
        jsonb_build_object('invalid_dimension_rows', 0, 'type', 'vector(1536)'),
        vector_observations.observed,
        vector_observations.observed = jsonb_build_object(
            'invalid_dimension_rows', 0, 'type', 'vector(1536)'
        )
    FROM vector_observations
    WHERE table_name = 'runbooks'
    UNION ALL
    SELECT
        'embedding_snippets',
        jsonb_build_object('invalid_dimension_rows', 0, 'type', 'vector(1536)'),
        vector_observations.observed,
        vector_observations.observed = jsonb_build_object(
            'invalid_dimension_rows', 0, 'type', 'vector(1536)'
        )
    FROM vector_observations
    WHERE table_name = 'snippets'
    UNION ALL
    SELECT
        'extension_vector',
        to_jsonb('0.8.2'::text),
        to_jsonb(extension_observation.value),
        extension_observation.value = '0.8.2'
    FROM extension_observation
    UNION ALL
    SELECT
        'features_nonempty',
        jsonb_build_object('minimum', 1),
        jsonb_build_object('count', row_counts.features),
        row_counts.features >= 1
    FROM row_counts
    UNION ALL
    SELECT
        'graph_foundation_033',
        to_jsonb(TRUE),
        to_jsonb(graph_foundation_033_observation.passed),
        graph_foundation_033_observation.passed
    FROM graph_foundation_033_observation
    UNION ALL
    SELECT
        'graph_projection_034_035',
        to_jsonb(TRUE),
        to_jsonb(graph_projection_034_035_observation.passed),
        graph_projection_034_035_observation.passed
    FROM graph_projection_034_035_observation
    UNION ALL
    SELECT
        'graph_relations_nonempty',
        jsonb_build_object('minimum', 1),
        jsonb_build_object('count', row_counts.graph_relations),
        row_counts.graph_relations >= 1
    FROM row_counts
    UNION ALL
    SELECT
        'indexed_plan_chunks_nonempty',
        jsonb_build_object('minimum', 1),
        jsonb_build_object('count', row_counts.indexed_plan_chunks),
        row_counts.indexed_plan_chunks >= 1
    FROM row_counts
    UNION ALL
    SELECT
        'indexed_plans_nonempty',
        jsonb_build_object('minimum', 1),
        jsonb_build_object('count', row_counts.indexed_plans),
        row_counts.indexed_plans >= 1
    FROM row_counts
    UNION ALL
    SELECT
        'orphan_feature_artifacts_features',
        to_jsonb(0),
        to_jsonb(orphan_counts.feature_artifacts),
        orphan_counts.feature_artifacts = 0
    FROM orphan_counts
    UNION ALL
    SELECT
        'orphan_indexed_plan_chunks_plans',
        to_jsonb(0),
        to_jsonb(orphan_counts.indexed_plan_chunks),
        orphan_counts.indexed_plan_chunks = 0
    FROM orphan_counts
    UNION ALL
    SELECT
        'project_contexts_nonempty',
        jsonb_build_object('minimum', 1),
        jsonb_build_object('count', row_counts.project_contexts),
        row_counts.project_contexts >= 1
    FROM row_counts
    UNION ALL
    SELECT
        'table_set',
        table_sets.expected,
        table_sets.observed,
        table_sets.expected = table_sets.observed
    FROM table_sets
)
SELECT jsonb_build_object(
    'checks', jsonb_agg(
        jsonb_build_object(
            'expected', expected,
            'id', id,
            'observed', observed,
            'status', CASE WHEN passed THEN 'pass' ELSE 'fail' END
        )
        ORDER BY id
    ),
    'contract_id', 'brain-v42/postgresql-recovery/v2',
    'schema_version', 2
)::text
FROM check_rows;
