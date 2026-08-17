WITH expected_tables(table_name) AS (
    VALUES
        ('access_log'),
        ('adrs'),
        ('alembic_version'),
        ('consolidation_log'),
        ('decisions'),
        ('dream_promotions'),
        ('dream_runs'),
        ('feature_artifacts'),
        ('features'),
        ('gitlab_events'),
        ('indexed_plan_chunks'),
        ('indexed_plans'),
        ('learnings'),
        ('metrics_timeseries'),
        ('process_metrics'),
        ('project_contexts'),
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
            'foreign_keys', 17,
            'indexes', 101,
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
check_rows(id, expected, observed, passed) AS (
    SELECT
        'alembic_head',
        to_jsonb('031'::text),
        to_jsonb(head_observation.value),
        head_observation.value = '031'
    FROM head_observation
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
    'contract_id', 'brain-v42/postgresql-recovery/v1',
    'schema_version', 1
)::text
FROM check_rows;
