WITH expected_tables(table_name) AS (
 VALUES
     ('access_log'),
     ('adrs'),
     ('alembic_version'),
     ('brain_entities'),
     ('brain_session_artifacts'),
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
     ('ticket_extraction_attempts'),
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
         'foreign_keys', 26,
         'indexes', 130,
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
expected_session_columns(
 column_name,
 data_type,
 is_nullable,
 character_maximum_length,
 column_default
) AS (
 VALUES
     ('client_key', 'character varying', 'NO', 128, NULL::text),
     ('started_focus_revision', 'bigint', 'NO', NULL::integer, NULL::text),
     ('captured_knowledge_ids', 'ARRAY', 'NO', NULL::integer, '''{}''::uuid[]'),
     ('nothing_to_capture_reason', 'text', 'YES', NULL::integer, NULL::text),
     ('abandonment_reason', 'text', 'YES', NULL::integer, NULL::text),
     ('last_heartbeat_at', 'timestamp with time zone', 'NO', NULL::integer, 'now()'),
     ('end_expected_focus_revision', 'bigint', 'YES', NULL::integer, NULL::text),
     ('focus_outcome', 'character varying', 'YES', 20, NULL::text),
     ('focus_at_end', 'text', 'YES', NULL::integer, NULL::text),
     ('focus_revision_at_end', 'bigint', 'YES', NULL::integer, NULL::text)
),
expected_session_constraints(
 constraint_name,
 constraint_type,
 delete_action,
 definition_md5
) AS (
 VALUES
     ('brain_sessions_capture_ids_valid', 'c', NULL::text, '1a8756bd34b4ea7e8d835643d0fa7ceb'),
     ('brain_sessions_client_key_nonblank', 'c', NULL::text, '8ec1e8c3738bbe2178e04689dd038e0d'),
     ('brain_sessions_focus_outcome_valid', 'c', NULL::text, 'ebc1583eea145e1804fd1508eab2c0d5'),
     ('brain_sessions_nature_valid', 'c', NULL::text, '9f0ef14672aa448ce2be6e15fa7c4dd4'),
     ('brain_sessions_pkey', 'p', NULL::text, 'cc3552dbb61b18accca876af5296eb1f'),
     ('brain_sessions_project_key_fkey', 'f', 'r', 'b863ba166c02670d9dad0a56f9582d59'),
     ('brain_sessions_status_valid', 'c', NULL::text, '586d25dcdade2c6c4aea9b415a19f7c5'),
     ('brain_sessions_terminal_state_valid', 'c', NULL::text, 'aab51404804e113ec2c452ba0bc21aa8'),
     ('uq_brain_sessions_project_client', 'u', NULL::text, '153c25b1acb665316ea262444b4d0d79')
),
observed_session_constraints AS (
 SELECT
     constraint_record.*,
     replace(replace(regexp_replace(lower(pg_catalog.pg_get_constraintdef(constraint_record.oid, TRUE)), '[[:space:]]+', ' ', 'g'), '::character varying::text', '::character varying'), ']::text[]', ']') AS canonical_definition
 FROM pg_catalog.pg_constraint AS constraint_record
 WHERE constraint_record.conrelid = 'public.brain_sessions'::regclass
),
expected_session_constraint_fragments(constraint_name, definition) AS (
 VALUES
     (
         'brain_sessions_capture_ids_valid',
         'cardinality(captured_knowledge_ids) <= 100'
     ),
     (
         'brain_sessions_capture_ids_valid',
         'array_position(captured_knowledge_ids, null::uuid) is null'
     ),
     (
         'brain_sessions_focus_outcome_valid',
         'focus_outcome is null'
     ),
     (
         'brain_sessions_focus_outcome_valid',
         'focus_outcome::text = any'
     ),
     (
         'brain_sessions_focus_outcome_valid',
         '''applied''::character varying, ''conflict''::character varying'
     ),
     (
         'brain_sessions_terminal_state_valid',
         'status::text = ''open''::text'
     ),
     (
         'brain_sessions_terminal_state_valid',
         'status::text = ''ended''::text'
     ),
     (
         'brain_sessions_terminal_state_valid',
         'status::text = ''abandoned''::text'
     ),
     (
         'brain_sessions_terminal_state_valid',
         'status::text = ''closed_inactive''::text'
     ),
     (
         'brain_sessions_terminal_state_valid',
         'focus_revision_at_end = (end_expected_focus_revision + 1)'
     ),
     (
         'brain_sessions_terminal_state_valid',
         'focus_revision_at_end <> end_expected_focus_revision'
     ),
     (
         'brain_sessions_terminal_state_valid',
         'cardinality(captured_knowledge_ids) > 0 and nothing_to_capture_reason is null'
     ),
     (
         'brain_sessions_terminal_state_valid',
         'cardinality(captured_knowledge_ids) = 0 and nothing_to_capture_reason is not null'
     ),
     (
         'brain_sessions_terminal_state_valid',
         'abandonment_reason is not null'
     )
),
expected_artifact_columns(
 column_name,
 ordinal_position,
 data_type,
 is_nullable,
 character_maximum_length,
 column_default
) AS (
 VALUES
     ('knowledge_id', 1, 'uuid', 'NO', NULL::integer, NULL::text),
     ('session_id', 2, 'uuid', 'NO', NULL::integer, NULL::text),
     ('knowledge_type', 3, 'character varying', 'NO', 32, NULL::text),
     ('captured_at', 4, 'timestamp with time zone', 'NO', NULL::integer, 'now()')
),
expected_artifact_constraints(
 constraint_name,
 constraint_type,
 delete_action,
 definition
) AS (
 VALUES
     (
         'brain_session_artifacts_pkey',
         'p',
         NULL::text,
         'primary key (knowledge_id)'
     ),
     (
         'brain_session_artifacts_session_id_fkey',
         'f',
         'c',
         concat(
             'foreign key (session_id) references brain_sessions(id) on de',
             'lete cascade'
         )
     ),
     (
         'brain_session_artifacts_type_valid',
         'c',
         NULL::text,
         'check (knowledge_type::text = any (array[''decision''::character varying, ''learning''::character varying, ''snippet''::character varying, ''runbook''::character varying, ''adr''::character varying, ''indexed_plan''::character varying, ''legacy''::character varying]))'
     )
),
observed_artifact_constraints AS (
 SELECT
     constraint_record.*,
     replace(replace(regexp_replace(lower(pg_catalog.pg_get_constraintdef(constraint_record.oid, TRUE)), '[[:space:]]+', ' ', 'g'), '::character varying::text', '::character varying'), ']::text[]', ']') AS canonical_definition
 FROM pg_catalog.pg_constraint AS constraint_record
 WHERE constraint_record.conrelid = 'public.brain_session_artifacts'::regclass
),
expected_artifact_indexes(
 index_name,
 is_unique,
 is_primary,
 columns,
 definition_md5
) AS (
 VALUES
     (
         'brain_session_artifacts_pkey',
         TRUE,
         TRUE,
         jsonb_build_array('knowledge_id'),
         'fe2984a0da51999576c9e1c26810a0db'
     ),
     (
         'idx_brain_session_artifacts_session_captured',
         FALSE,
         FALSE,
         jsonb_build_array('session_id', 'captured_at'),
         '1537b589099c6198cc66c39ad6656e94'
     )
),
expected_session_indexes(index_name, definition_md5) AS (
 VALUES
     ('brain_sessions_pkey', '6763cd8159ef6f0131abbfedfea044bc'),
     (
         'idx_brain_sessions_project_status_started',
         'daf2b70c6799177168837efedcb0dbe8'
     ),
     (
         'uq_brain_sessions_project_client',
         '28c33a3d73bf9f0c64d322978b7118a4'
     ),
     (
         'uq_brain_sessions_connection',
         '62b298d247237eddf60cb4ba28693af4'
     )
),
expected_column_fingerprints(object_name, definition_md5) AS (
 VALUES
     ('brain_session_artifacts', '6929b0b5a35595521022bcfe25cf5a07'),
     ('brain_sessions', 'd75989f65d6b2929cb4f7d9377f4d3bc'),
     ('codex_brain_entity_v1', 'c8aa9c21e5706e1a4983df5a2dd18213'),
     ('codex_consolidation_log_v1', '9c951f395a2388883bca636bf0ae9147'),
     ('codex_dream_promotion_v1', '57a2135efb8e79ec1d9ed8df8bfa28b1'),
     ('codex_dream_run_v1', '7eb14c21fea0ec4f95f09a5c03d3996d'),
     ('codex_feature_artifact_v1', '6ce4788f749dcc4cce3f11658117efac'),
     ('codex_feature_v1', '554926bf35b60549a55414e9202f143f'),
     (
         'codex_roadmap_curation_proposal_v1',
         '9b34ac92a6fab0032a6cdd4a0b9647d9'
     ),
     (
         'codex_ticket_extraction_proposal_v1',
         '11d7b8be4a85efd1312967fa6e253ecc'
     ),
     ('codex_ticket_message_v1', '9c87fa93b464bf432c1c4a6609721752'),
     ('codex_ticket_v1', '5abd645cadecf917aac6a5c091a91806')
),
observed_column_fingerprints(object_name, definition_md5) AS (
 SELECT
     observed_column.table_name,
     md5(
         COALESCE(
             jsonb_agg(
                 jsonb_build_array(
                     observed_column.ordinal_position,
                     observed_column.column_name,
                     observed_column.data_type,
                     observed_column.udt_schema,
                     observed_column.udt_name,
                     observed_column.is_nullable,
                     observed_column.character_maximum_length,
                     observed_column.numeric_precision,
                     observed_column.numeric_scale,
                     observed_column.datetime_precision,
                     observed_column.column_default,
                     observed_column.is_identity,
                     observed_column.identity_generation,
                     observed_column.is_generated,
                     observed_column.generation_expression,
                     observed_column.collation_schema,
                     observed_column.collation_name
                 )
                 ORDER BY observed_column.ordinal_position
             )::text,
             '[]'
         )
     )
 FROM information_schema.columns AS observed_column
 WHERE observed_column.table_schema = 'public'
   AND observed_column.table_name IN (
       SELECT object_name FROM expected_column_fingerprints
   )
 GROUP BY observed_column.table_name
),
expected_contract_views(view_name, security_barrier, definition_md5) AS (
 VALUES
     ('codex_brain_entity_v1', TRUE, '9ff73f99a8786fe84b24c4e0c5ee6999'),
     ('codex_consolidation_log_v1', FALSE, 'd0884470e57b41987a2f06cb206b460c'),
     ('codex_dream_promotion_v1', FALSE, 'c14ec7f88ee97f4359e1a7a90c21b967'),
     ('codex_dream_run_v1', FALSE, '465f271c378c98cf26ffbeb0d97a67f3'),
     ('codex_feature_artifact_v1', TRUE, 'cf55ef799d0c446229f0b781b44099f1'),
     ('codex_feature_v1', TRUE, '61a939fd9d0f9da9639beb2d71d57fdf'),
     (
         'codex_roadmap_curation_proposal_v1',
         TRUE,
         '80d147acacb1e5b4a94d534fa0a32e3b'
     ),
     (
         'codex_ticket_extraction_proposal_v1',
         TRUE,
         '4d5220e663df342c2e0cccccd7910b3e'
     ),
     ('codex_ticket_message_v1', TRUE, 'd91b467ff450e3b91e7ff14c9ad67e1d'),
     ('codex_ticket_v1', TRUE, '9b248acd2480ea9ac252e3cbef09b47a')
),
expected_runtime_triggers(
 table_name,
 trigger_name,
 function_name,
 trigger_type,
 trigger_columns,
 trigger_definition_md5,
 function_definition_md5
) AS (
 VALUES
     (
         'feature_artifacts',
         'trg_feature_artifact_live_target',
         'enforce_live_feature_artifact_target',
         23,
         jsonb_build_array('feature_id'),
         '09243973b66a4e83a206d21cb01a46e6',
         '81d1b2839f665f207df9a019736a87bb'
     ),
     (
         'project_contexts',
         'project_contexts_focus_revision_trigger',
         'increment_project_focus_revision',
         19,
         jsonb_build_array('current_focus'),
         '4b1ca0f513d1bca895bfa8931f488e66',
         'c13b0ab647661e42c74f9726a8ca3c54'
     ),
     (
         'tickets',
         'trg_ticket_participants_immutable',
         'enforce_immutable_ticket_participants',
         19,
         jsonb_build_array('from_project', 'to_project'),
         '9e4f03a836f26bc36fb99be861508a90',
         'cde22e328857b6209337369b8a43aacc'
     )
),
expected_runtime_user_triggers(table_name, trigger_name) AS (
 VALUES
     ('brain_sessions', 'brain_sessions_project_alias_trigger'),
     ('brain_sessions', 'brain_sessions_project_registry_trigger'),
     ('feature_artifacts', 'trg_feature_artifact_live_target'),
     ('project_contexts', 'project_contexts_brain_registry_trigger'),
     ('project_contexts', 'project_contexts_focus_revision_trigger'),
     ('project_contexts', 'project_contexts_project_alias_trigger'),
     ('project_contexts', 'project_contexts_project_key_immutable_trigger'),
     ('project_contexts', 'project_contexts_related_project_alias_trigger'),
     ('project_contexts', 'project_contexts_related_project_registry_trigger'),
     ('project_contexts', 'trg_project_contexts_updated'),
     ('tickets', 'tickets_project_alias_trigger'),
     ('tickets', 'tickets_project_registry_trigger'),
     ('tickets', 'trg_ticket_participants_immutable')
),
expected_runtime_trigger_tables(table_name) AS (
 VALUES
     ('brain_session_artifacts'),
     ('brain_sessions'),
     ('feature_artifacts'),
     ('project_contexts'),
     ('tickets')
),
session_column_mismatches AS (
 SELECT
     count(*) + (
         SELECT count(*)
         FROM expected_column_fingerprints AS expected_fingerprint
         LEFT JOIN observed_column_fingerprints AS observed_fingerprint
           ON observed_fingerprint.object_name = expected_fingerprint.object_name
          AND observed_fingerprint.definition_md5 = expected_fingerprint.definition_md5
         WHERE expected_fingerprint.object_name = 'brain_sessions'
           AND observed_fingerprint.object_name IS NULL
     ) + (
         SELECT CASE WHEN count(*) = 1 THEN 0 ELSE 1 END
         FROM pg_catalog.pg_class AS relation_record
         JOIN pg_catalog.pg_namespace AS namespace_record
           ON namespace_record.oid = relation_record.relnamespace
         JOIN pg_catalog.pg_am AS access_method
           ON access_method.oid = relation_record.relam
         WHERE namespace_record.nspname = 'public'
           AND relation_record.relname = 'brain_sessions'
           AND relation_record.relkind = 'r'
           AND relation_record.relpersistence = 'p'
           AND NOT relation_record.relispartition
           AND NOT relation_record.relhasrules
           AND NOT relation_record.relrowsecurity
           AND NOT relation_record.relforcerowsecurity
           AND cardinality(COALESCE(relation_record.reloptions, ARRAY[]::text[])) = 0
           AND NOT EXISTS (
               SELECT 1
               FROM pg_catalog.pg_inherits AS inheritance_link
               WHERE relation_record.oid IN (
                   inheritance_link.inhrelid,
                   inheritance_link.inhparent
               )
           )
           AND access_method.amname = 'heap'
     ) AS value
 FROM expected_session_columns AS expected_column
 LEFT JOIN information_schema.columns AS observed_column
   ON observed_column.table_schema = 'public'
  AND observed_column.table_name = 'brain_sessions'
  AND observed_column.column_name = expected_column.column_name
  AND observed_column.data_type = expected_column.data_type
  AND observed_column.is_nullable = expected_column.is_nullable
  AND observed_column.character_maximum_length
      IS NOT DISTINCT FROM expected_column.character_maximum_length
  AND observed_column.column_default
      IS NOT DISTINCT FROM expected_column.column_default
 WHERE observed_column.column_name IS NULL
),
session_constraint_mismatches AS (
 SELECT
     (
         SELECT count(*)
         FROM expected_session_constraints AS expected_constraint
         LEFT JOIN observed_session_constraints AS constraint_record
           ON constraint_record.conname = expected_constraint.constraint_name
          AND constraint_record.contype::text = expected_constraint.constraint_type
          AND (
              expected_constraint.delete_action IS NULL
              OR constraint_record.confdeltype::text = expected_constraint.delete_action
          )
          AND md5(constraint_record.canonical_definition)
              = expected_constraint.definition_md5
          AND constraint_record.convalidated
          AND (
              expected_constraint.constraint_type <> 'f'
              OR (
                  SELECT count(*) = 4
                  FROM pg_catalog.pg_trigger AS fk_trigger
                  WHERE fk_trigger.tgconstraint = constraint_record.oid
                    AND fk_trigger.tgisinternal
                    AND fk_trigger.tgenabled = 'O'
              )
          )
         WHERE constraint_record.oid IS NULL
     ) + (
         SELECT count(*)
         FROM observed_session_constraints AS constraint_record
         LEFT JOIN expected_session_constraints AS expected_constraint
           ON expected_constraint.constraint_name = constraint_record.conname
         WHERE constraint_record.conrelid = 'public.brain_sessions'::regclass
           AND constraint_record.convalidated
           AND expected_constraint.constraint_name IS NULL
     ) + (
         SELECT count(*)
         FROM expected_session_constraint_fragments AS expected_session_constraint
         JOIN observed_session_constraints AS constraint_record
           ON constraint_record.conname = expected_session_constraint.constraint_name
         WHERE position(
             expected_session_constraint.definition
             IN constraint_record.canonical_definition
         ) = 0
     ) + (
         SELECT count(*)
         FROM expected_session_indexes AS expected_index
         LEFT JOIN pg_catalog.pg_namespace AS index_namespace
           ON index_namespace.nspname = 'public'
         LEFT JOIN pg_catalog.pg_class AS index_table
           ON index_table.relnamespace = index_namespace.oid
          AND index_table.relname = expected_index.index_name
         LEFT JOIN pg_catalog.pg_index AS index_record
           ON index_record.indexrelid = index_table.oid
          AND index_record.indrelid = 'public.brain_sessions'::regclass
          AND index_record.indisvalid
          AND index_record.indisready
          AND md5(pg_catalog.pg_get_indexdef(index_record.indexrelid))
              = expected_index.definition_md5
         WHERE index_record.indexrelid IS NULL
     ) + (
         SELECT count(*)
         FROM pg_catalog.pg_index AS index_record
         JOIN pg_catalog.pg_class AS index_table
           ON index_table.oid = index_record.indexrelid
         WHERE index_record.indrelid = 'public.brain_sessions'::regclass
           AND NOT EXISTS (
               SELECT 1
               FROM expected_session_indexes AS expected_index
               WHERE expected_index.index_name = index_table.relname
           )
     ) AS value
),
artifact_column_mismatches AS (
 SELECT
     count(*) + (
         SELECT count(*)
         FROM expected_column_fingerprints AS expected_fingerprint
         LEFT JOIN observed_column_fingerprints AS observed_fingerprint
           ON observed_fingerprint.object_name = expected_fingerprint.object_name
          AND observed_fingerprint.definition_md5 = expected_fingerprint.definition_md5
         WHERE expected_fingerprint.object_name = 'brain_session_artifacts'
           AND observed_fingerprint.object_name IS NULL
     ) + (
         SELECT CASE WHEN count(*) = 1 THEN 0 ELSE 1 END
         FROM pg_catalog.pg_class AS relation_record
         JOIN pg_catalog.pg_namespace AS namespace_record
           ON namespace_record.oid = relation_record.relnamespace
         JOIN pg_catalog.pg_am AS access_method
           ON access_method.oid = relation_record.relam
         WHERE namespace_record.nspname = 'public'
           AND relation_record.relname = 'brain_session_artifacts'
           AND relation_record.relkind = 'r'
           AND relation_record.relpersistence = 'p'
           AND NOT relation_record.relispartition
           AND NOT relation_record.relhasrules
           AND NOT relation_record.relrowsecurity
           AND NOT relation_record.relforcerowsecurity
           AND cardinality(COALESCE(relation_record.reloptions, ARRAY[]::text[])) = 0
           AND NOT EXISTS (
               SELECT 1
               FROM pg_catalog.pg_inherits AS inheritance_link
               WHERE relation_record.oid IN (
                   inheritance_link.inhrelid,
                   inheritance_link.inhparent
               )
           )
           AND access_method.amname = 'heap'
     ) AS value
 FROM (
     SELECT
         expected_column.column_name,
         expected_column.ordinal_position,
         observed_column.column_name AS observed_name
     FROM expected_artifact_columns AS expected_column
     LEFT JOIN information_schema.columns AS observed_column
       ON observed_column.table_schema = 'public'
      AND observed_column.table_name = 'brain_session_artifacts'
      AND observed_column.column_name = expected_column.column_name
      AND observed_column.ordinal_position = expected_column.ordinal_position
      AND observed_column.data_type = expected_column.data_type
      AND observed_column.is_nullable = expected_column.is_nullable
      AND observed_column.character_maximum_length
          IS NOT DISTINCT FROM expected_column.character_maximum_length
      AND observed_column.column_default
          IS NOT DISTINCT FROM expected_column.column_default
     WHERE observed_column.column_name IS NULL
     UNION ALL
     SELECT
         observed_column.column_name,
         observed_column.ordinal_position,
         NULL::text
     FROM information_schema.columns AS observed_column
     WHERE observed_column.table_schema = 'public'
       AND observed_column.table_name = 'brain_session_artifacts'
       AND NOT EXISTS (
           SELECT 1
           FROM expected_artifact_columns AS expected_column
           WHERE expected_column.column_name = observed_column.column_name
             AND expected_column.ordinal_position = observed_column.ordinal_position
       )
 ) AS mismatched_column
),
artifact_constraint_mismatches AS (
 SELECT
     (
         SELECT count(*)
         FROM expected_artifact_constraints AS expected_constraint
         LEFT JOIN observed_artifact_constraints AS constraint_record
           ON constraint_record.conname = expected_constraint.constraint_name
          AND constraint_record.contype::text = expected_constraint.constraint_type
          AND (
              expected_constraint.delete_action IS NULL
              OR constraint_record.confdeltype::text = expected_constraint.delete_action
          )
          AND constraint_record.canonical_definition = expected_constraint.definition
          AND constraint_record.convalidated
          AND (
              expected_constraint.constraint_type <> 'f'
              OR (
                  SELECT count(*) = 4
                  FROM pg_catalog.pg_trigger AS fk_trigger
                  WHERE fk_trigger.tgconstraint = constraint_record.oid
                    AND fk_trigger.tgisinternal
                    AND fk_trigger.tgenabled = 'O'
              )
          )
         WHERE constraint_record.oid IS NULL
     ) + (
         SELECT count(*)
         FROM observed_artifact_constraints AS constraint_record
         LEFT JOIN expected_artifact_constraints AS expected_constraint
           ON expected_constraint.constraint_name = constraint_record.conname
         WHERE constraint_record.conrelid = 'public.brain_session_artifacts'::regclass
           AND constraint_record.convalidated
           AND expected_constraint.constraint_name IS NULL
     ) AS value
),
observed_artifact_indexes AS (
 SELECT
     index_table.relname AS index_name,
     index_record.indisunique AS is_unique,
     index_record.indisprimary AS is_primary,
     index_record.indisvalid,
     index_record.indisready,
     jsonb_agg(attribute_record.attname ORDER BY index_column.ordinality) AS columns,
     md5(pg_catalog.pg_get_indexdef(index_record.indexrelid)) AS definition_md5
 FROM pg_catalog.pg_index AS index_record
 JOIN pg_catalog.pg_class AS source_table
   ON source_table.oid = index_record.indrelid
 JOIN pg_catalog.pg_namespace AS namespace_record
   ON namespace_record.oid = source_table.relnamespace
 JOIN pg_catalog.pg_class AS index_table
   ON index_table.oid = index_record.indexrelid
 CROSS JOIN LATERAL unnest(index_record.indkey)
   WITH ORDINALITY AS index_column(attribute_number, ordinality)
 LEFT JOIN pg_catalog.pg_attribute AS attribute_record
   ON attribute_record.attrelid = source_table.oid
  AND attribute_record.attnum = index_column.attribute_number
 WHERE namespace_record.nspname = 'public'
   AND source_table.relname = 'brain_session_artifacts'
 GROUP BY
     index_table.relname,
     index_record.indexrelid,
     index_record.indisunique,
     index_record.indisprimary,
     index_record.indisvalid,
     index_record.indisready
),
artifact_index_mismatches AS (
 SELECT count(*) AS value
 FROM (
     SELECT expected_index.index_name
     FROM expected_artifact_indexes AS expected_index
     LEFT JOIN observed_artifact_indexes AS observed_index
       ON observed_index.index_name = expected_index.index_name
      AND observed_index.is_unique = expected_index.is_unique
      AND observed_index.is_primary = expected_index.is_primary
      AND observed_index.indisvalid
      AND observed_index.indisready
      AND observed_index.columns = expected_index.columns
      AND observed_index.definition_md5
          IS NOT DISTINCT FROM expected_index.definition_md5
     WHERE observed_index.index_name IS NULL
     UNION ALL
     SELECT observed_index.index_name
     FROM observed_artifact_indexes AS observed_index
     WHERE NOT EXISTS (
         SELECT 1
         FROM expected_artifact_indexes AS expected_index
         WHERE expected_index.index_name = observed_index.index_name
     )
 ) AS mismatched_index
),
view_column_mismatches AS (
 SELECT count(*) AS value
 FROM expected_column_fingerprints AS expected_fingerprint
 LEFT JOIN observed_column_fingerprints AS observed_fingerprint
   ON observed_fingerprint.object_name = expected_fingerprint.object_name
  AND observed_fingerprint.definition_md5 = expected_fingerprint.definition_md5
 WHERE expected_fingerprint.object_name IN (
     SELECT view_name FROM expected_contract_views
 )
   AND observed_fingerprint.object_name IS NULL
),
view_option_mismatches AS (
 SELECT count(*) AS value
 FROM expected_contract_views AS expected_view
 LEFT JOIN pg_catalog.pg_namespace AS namespace_record
   ON namespace_record.nspname = 'public'
 LEFT JOIN pg_catalog.pg_class AS table_record
   ON table_record.relnamespace = namespace_record.oid
  AND table_record.relname = expected_view.view_name
  AND table_record.relkind = 'v'
 WHERE table_record.oid IS NULL
    OR COALESCE(
        'security_barrier=true' = ANY(table_record.reloptions),
        FALSE
    ) IS DISTINCT FROM expected_view.security_barrier
    OR cardinality(COALESCE(table_record.reloptions, ARRAY[]::text[]))
       <> CASE WHEN expected_view.security_barrier THEN 1 ELSE 0 END
),
view_definition_mismatches AS (
 SELECT count(*) AS value
 FROM expected_contract_views AS expected_view
 LEFT JOIN pg_catalog.pg_namespace AS namespace_record
   ON namespace_record.nspname = 'public'
 LEFT JOIN pg_catalog.pg_class AS table_record
   ON table_record.relnamespace = namespace_record.oid
  AND table_record.relname = expected_view.view_name
  AND table_record.relkind = 'v'
 WHERE table_record.oid IS NULL
    OR md5(
        pg_catalog.pg_get_viewdef(table_record.oid, TRUE)
    ) IS DISTINCT FROM expected_view.definition_md5
),
runtime_trigger_mismatches AS (
 SELECT
     count(*) + (
         SELECT count(*)
         FROM expected_runtime_user_triggers AS expected_user_trigger
         LEFT JOIN pg_catalog.pg_namespace AS runtime_namespace
           ON runtime_namespace.nspname = 'public'
         LEFT JOIN pg_catalog.pg_class AS runtime_table
           ON runtime_table.relnamespace = runtime_namespace.oid
          AND runtime_table.relname = expected_user_trigger.table_name
         LEFT JOIN pg_catalog.pg_trigger AS observed_user_trigger
           ON observed_user_trigger.tgrelid = runtime_table.oid
          AND observed_user_trigger.tgname = expected_user_trigger.trigger_name
          AND NOT observed_user_trigger.tgisinternal
          AND observed_user_trigger.tgenabled = 'O'
         WHERE observed_user_trigger.oid IS NULL
     ) + (
         SELECT count(*) AS unexpected_runtime_trigger
         FROM pg_catalog.pg_trigger AS trigger_record
         JOIN pg_catalog.pg_class AS runtime_table
           ON runtime_table.oid = trigger_record.tgrelid
         JOIN pg_catalog.pg_namespace AS runtime_namespace
           ON runtime_namespace.oid = runtime_table.relnamespace
         WHERE runtime_namespace.nspname = 'public'
           AND runtime_table.relname IN (
               SELECT table_name FROM expected_runtime_trigger_tables
           )
           AND NOT trigger_record.tgisinternal
           AND NOT EXISTS (
               SELECT 1
               FROM expected_runtime_user_triggers AS expected_user_trigger
               WHERE expected_user_trigger.table_name = runtime_table.relname
                 AND expected_user_trigger.trigger_name = trigger_record.tgname
           )
     ) + (
         SELECT count(*) AS unexpected_runtime_rule
         FROM pg_catalog.pg_class AS runtime_table
         JOIN pg_catalog.pg_namespace AS runtime_namespace
           ON runtime_namespace.oid = runtime_table.relnamespace
         WHERE runtime_namespace.nspname = 'public'
           AND runtime_table.relname IN (
               SELECT table_name FROM expected_runtime_trigger_tables
         )
         AND runtime_table.relhasrules
   ) + (
       SELECT count(*) AS historical_runtime_relation_mismatch
       FROM expected_runtime_trigger_tables AS expected_runtime_table
       WHERE expected_runtime_table.table_name IN (
           'feature_artifacts',
           'project_contexts',
           'tickets'
       )
         AND NOT EXISTS (
             SELECT 1
             FROM pg_catalog.pg_class AS runtime_table
             JOIN pg_catalog.pg_namespace AS runtime_namespace
               ON runtime_namespace.oid = runtime_table.relnamespace
             JOIN pg_catalog.pg_am AS runtime_access_method
               ON runtime_access_method.oid = runtime_table.relam
             WHERE runtime_namespace.nspname = 'public'
               AND runtime_table.relname = expected_runtime_table.table_name
               AND runtime_table.relkind = 'r'
               AND runtime_table.relpersistence = 'p'
               AND NOT runtime_table.relispartition
               AND NOT runtime_table.relhasrules
               AND NOT runtime_table.relrowsecurity
               AND NOT runtime_table.relforcerowsecurity
               AND cardinality(COALESCE(runtime_table.reloptions, ARRAY[]::text[])) = 0
               AND runtime_access_method.amname = 'heap'
               AND NOT EXISTS (
                   SELECT 1
                   FROM pg_catalog.pg_inherits AS runtime_inheritance
                   WHERE runtime_table.oid IN (
                       runtime_inheritance.inhrelid,
                       runtime_inheritance.inhparent
                   )
               )
         )
   ) AS value
 FROM expected_runtime_triggers AS expected_trigger
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
 LEFT JOIN pg_catalog.pg_namespace AS function_namespace_record
   ON function_namespace_record.oid = function_record.pronamespace
  AND function_namespace_record.nspname = 'public'
 LEFT JOIN LATERAL (
     SELECT jsonb_agg(attribute_record.attname ORDER BY trigger_column.ordinality) AS columns
     FROM unnest(trigger_record.tgattr)
       WITH ORDINALITY AS trigger_column(attribute_number, ordinality)
     JOIN pg_catalog.pg_attribute AS attribute_record
       ON attribute_record.attrelid = table_record.oid
      AND attribute_record.attnum = trigger_column.attribute_number
 ) AS observed_columns ON TRUE
 WHERE trigger_record.oid IS NULL
    OR function_record.oid IS NULL
    OR function_namespace_record.oid IS NULL
    OR md5(
        pg_catalog.pg_get_triggerdef(trigger_record.oid, TRUE)
    ) IS DISTINCT FROM expected_trigger.trigger_definition_md5
    OR md5(
        pg_catalog.pg_get_functiondef(function_record.oid)
    ) IS DISTINCT FROM expected_trigger.function_definition_md5
    OR trigger_record.tgenabled <> 'O'
    OR trigger_record.tgtype <> expected_trigger.trigger_type
    OR observed_columns.columns IS DISTINCT FROM expected_trigger.trigger_columns
),
focus_revision_violations AS (
 SELECT
     (
         SELECT CASE WHEN count(*) = 1 THEN 0 ELSE 1 END
         FROM information_schema.columns AS observed_focus_column
         WHERE observed_focus_column.table_schema = 'public'
           AND observed_focus_column.table_name = 'project_contexts'
           AND observed_focus_column.column_name = 'focus_revision'
           AND observed_focus_column.data_type = 'bigint'
           AND observed_focus_column.is_nullable = 'NO'
           AND observed_focus_column.column_default = '0'
     ) + (
         SELECT count(*) AS unexpected_focus_constraint
         FROM pg_catalog.pg_constraint AS constraint_record
         JOIN pg_catalog.pg_attribute AS focus_attribute
           ON focus_attribute.attrelid = constraint_record.conrelid
          AND focus_attribute.attname = 'focus_revision'
         WHERE constraint_record.conrelid = 'public.project_contexts'::regclass
           AND focus_attribute.attnum = ANY(constraint_record.conkey)
     ) + (
         SELECT count(*)
         FROM (
             SELECT 1
             FROM public.project_contexts
             WHERE focus_revision < 0
             UNION ALL
             SELECT 1
             FROM public.brain_sessions AS session_record
             JOIN public.project_contexts AS project_context
               ON project_context.project_key = session_record.project_key
             WHERE session_record.started_focus_revision < 0
                OR session_record.started_focus_revision > project_context.focus_revision
                OR session_record.last_heartbeat_at < session_record.started_at
         ) AS invalid_revision
     ) AS value
),
ended_snapshot_mismatches AS (
 SELECT count(*) AS value
 FROM public.brain_sessions AS session_record
 WHERE session_record.status = 'ended'
   AND (
       cardinality(session_record.captured_knowledge_ids) <> (
           SELECT count(*)
           FROM public.brain_session_artifacts AS artifact_record
           WHERE artifact_record.session_id = session_record.id
       )
       OR EXISTS (
           SELECT captured_id
           FROM unnest(session_record.captured_knowledge_ids) AS captured_id
           EXCEPT
           SELECT artifact_record.knowledge_id
           FROM public.brain_session_artifacts AS artifact_record
           WHERE artifact_record.session_id = session_record.id
       )
       OR EXISTS (
           SELECT artifact_record.knowledge_id
           FROM public.brain_session_artifacts AS artifact_record
           WHERE artifact_record.session_id = session_record.id
           EXCEPT
           SELECT captured_id
           FROM unnest(session_record.captured_knowledge_ids) AS captured_id
       )
   )
),
knowledge_sources(knowledge_id, knowledge_type, project_key, created_at) AS (
 SELECT id, 'decision', project_key, created_at FROM public.decisions
 UNION ALL SELECT id, 'learning', project_key, created_at FROM public.learnings
 UNION ALL SELECT id, 'snippet', project_key, created_at FROM public.snippets
 UNION ALL SELECT id, 'runbook', project_key, created_at FROM public.runbooks
 UNION ALL SELECT id, 'adr', project_key, created_at FROM public.adrs
 UNION ALL SELECT id, 'indexed_plan', project_key, created_at FROM public.indexed_plans
),
artifact_source_matches AS (
 SELECT
     artifact_record.knowledge_id,
     count(source_record.knowledge_id) AS source_matches,
     count(source_record.knowledge_id) FILTER (
         WHERE artifact_record.knowledge_type = 'legacy'
            OR source_record.knowledge_type = artifact_record.knowledge_type
     ) AS typed_matches
 FROM public.brain_session_artifacts AS artifact_record
 JOIN public.brain_sessions AS session_record
   ON session_record.id = artifact_record.session_id
 LEFT JOIN knowledge_sources AS source_record
   ON source_record.knowledge_id = artifact_record.knowledge_id
  AND source_record.project_key = session_record.project_key
  AND source_record.created_at >= session_record.started_at
  AND source_record.created_at <= artifact_record.captured_at
 GROUP BY artifact_record.session_id, artifact_record.knowledge_id
),
artifact_source_mismatches AS (
 SELECT count(*) AS value
 FROM artifact_source_matches
 WHERE source_matches <> 1 OR typed_matches <> 1
),
artifact_lifecycle_violations AS (
 SELECT count(*) AS value
 FROM (
     SELECT artifact_record.knowledge_id
     FROM public.brain_session_artifacts AS artifact_record
     JOIN public.brain_sessions AS session_record
       ON session_record.id = artifact_record.session_id
     WHERE artifact_record.captured_at < session_record.started_at
        OR (
            session_record.ended_at IS NOT NULL
            AND artifact_record.captured_at > session_record.ended_at
        )
     UNION ALL
     SELECT artifact_record.session_id
     FROM public.brain_session_artifacts AS artifact_record
     GROUP BY artifact_record.session_id
     HAVING count(*) > 100
 ) AS violation
),
artifact_project_mismatches AS (
 SELECT
     artifact_source_mismatches.value + artifact_lifecycle_violations.value AS value
 FROM artifact_source_mismatches
 CROSS JOIN artifact_lifecycle_violations
),
brain_runtime_observation AS (
 SELECT
     jsonb_build_object(
         'artifact_column_mismatches', 0,
         'artifact_constraint_mismatches', 0,
         'artifact_index_mismatches', 0,
         'artifact_project_mismatches', 0,
         'ended_snapshot_mismatches', 0,
         'focus_revision_violations', 0,
         'runtime_trigger_mismatches', 0,
         'session_column_mismatches', 0,
         'session_constraint_mismatches', 0,
         'view_column_mismatches', 0,
         'view_definition_mismatches', 0,
         'view_option_mismatches', 0
     ) AS expected,
     jsonb_build_object(
         'artifact_column_mismatches', artifact_column_mismatches.value,
         'artifact_constraint_mismatches', artifact_constraint_mismatches.value,
         'artifact_index_mismatches', artifact_index_mismatches.value,
         'artifact_project_mismatches', artifact_project_mismatches.value,
         'ended_snapshot_mismatches', ended_snapshot_mismatches.value,
         'focus_revision_violations', focus_revision_violations.value,
         'runtime_trigger_mismatches', runtime_trigger_mismatches.value,
         'session_column_mismatches', session_column_mismatches.value,
         'session_constraint_mismatches', session_constraint_mismatches.value,
         'view_column_mismatches', view_column_mismatches.value,
         'view_definition_mismatches', view_definition_mismatches.value,
         'view_option_mismatches', view_option_mismatches.value
     ) AS observed
 FROM artifact_column_mismatches
 CROSS JOIN artifact_constraint_mismatches
 CROSS JOIN artifact_index_mismatches
 CROSS JOIN artifact_project_mismatches
 CROSS JOIN ended_snapshot_mismatches
 CROSS JOIN focus_revision_violations
 CROSS JOIN runtime_trigger_mismatches
 CROSS JOIN session_column_mismatches
 CROSS JOIN session_constraint_mismatches
 CROSS JOIN view_column_mismatches
 CROSS JOIN view_definition_mismatches
 CROSS JOIN view_option_mismatches
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
expected_inherited_constraints(
 table_name,
 constraint_name,
 constraint_type,
 delete_action,
 definition_md5
) AS (
 VALUES
     ('brain_entities', 'brain_entities_lifecycle_valid', 'c', NULL::text, 'cd2da96da432b61cd47e7266f197cd3b'),
     ('brain_entities', 'brain_entities_pkey', 'p', NULL::text, 'cc3552dbb61b18accca876af5296eb1f'),
     ('brain_entities', 'brain_entities_project_key_fkey', 'f', 'r', 'f263e7d4d142bbc04ada44537963b892'),
     ('brain_entities', 'brain_entities_scope_valid', 'c', NULL::text, '7a2522cefd4d98d52bc658343f54daa9'),
     ('brain_entities', 'uq_brain_entities_type_key', 'u', NULL::text, 'e8fd9124dae08d47c87177bf033b8e00'),
     ('entity_relations', 'entity_relations_confidence_valid', 'c', NULL::text, '8418df632947fd26246036ee546af632'),
     ('entity_relations', 'entity_relations_lifecycle_valid', 'c', NULL::text, 'cd2da96da432b61cd47e7266f197cd3b'),
     ('entity_relations', 'entity_relations_no_self_loop', 'c', NULL::text, '5a41e6500e5c57c7f2fd2b366d996831'),
     ('entity_relations', 'entity_relations_pkey', 'p', NULL::text, 'cc3552dbb61b18accca876af5296eb1f'),
     ('entity_relations', 'entity_relations_source_entity_id_fkey', 'f', 'r', '10d758915d3544cd60fbd31505ee24d6'),
     ('entity_relations', 'entity_relations_target_entity_id_fkey', 'f', 'r', '8188bcc6f479fce005c8af00803e91e8'),
     ('entity_relations', 'entity_relations_type_valid', 'c', NULL::text, 'de56c40c7349fe61da69b553ee8ad88a'),
     ('entity_relations', 'uq_entity_relations_endpoints_type', 'u', NULL::text, 'aafe3b4835484bc8352bb3e383f3b3de'),
     ('graph_outbox', 'graph_outbox_entity_id_fkey', 'f', 'c', '41669d749feab45b3b21507cbe1e72f8'),
     ('graph_outbox', 'graph_outbox_event_id_key', 'u', NULL::text, '759bdd8d95917e86a4535f61383231f2'),
     ('graph_outbox', 'graph_outbox_exactly_one_aggregate', 'c', NULL::text, '43e61c6f8f1d8edd4c7ad839435f3b94'),
     ('graph_outbox', 'graph_outbox_operation_valid', 'c', NULL::text, '02563be4b2f7d105be2c25775fd09852'),
     ('graph_outbox', 'graph_outbox_pkey', 'p', NULL::text, 'cc3552dbb61b18accca876af5296eb1f'),
     ('graph_outbox', 'graph_outbox_relation_id_fkey', 'f', 'c', '22cd3849e65c946557e6c4a9ea483648'),
     ('graph_outbox', 'uq_graph_outbox_entity_revision', 'u', NULL::text, '7b1e742994175d24227a5f0a6cff40a6'),
     ('graph_outbox', 'uq_graph_outbox_relation_revision', 'u', NULL::text, 'd5fb45f4a7893c5d45460da33fc32d3b'),
     ('graph_projection_leases', 'graph_projection_leases_armed_generation_valid', 'c', NULL::text, 'e8cee37772e9bc681ba229a778eace5d'),
     ('graph_projection_leases', 'graph_projection_leases_pkey', 'p', NULL::text, '3608ac6e0b09678c35217c69cc4de206'),
     ('graph_projection_leases', 'graph_projection_leases_protocol_valid', 'c', NULL::text, '3c5970bbe99c7f44f1a0127458293dea'),
     ('graph_projection_leases', 'graph_projection_leases_recovery_state_valid', 'c', NULL::text, 'da1a1dfd81cb4d6f562aa15f101ec34d'),
     ('projects', 'projects_key_format_valid', 'c', NULL::text, 'd2f0e69b15612f6476efceb2a228c6fb'),
     ('projects', 'projects_pkey', 'p', NULL::text, 'b449ae3aa5c5dbcebd0e93fd552a7787'),
     ('projects', 'projects_registry_status_valid', 'c', NULL::text, '7da8b1fc307de0337b6647b895313e2e'),
     ('projects', 'projects_source_valid', 'c', NULL::text, 'dea4cf93bb2488104f419ca18ed1bcd2')
),
observed_inherited_constraints AS (
 SELECT
     table_record.relname AS table_name,
     constraint_record.oid AS constraint_oid,
     constraint_record.conname AS constraint_name,
     constraint_record.contype::text AS constraint_type,
     constraint_record.confdeltype::text AS delete_action,
     constraint_record.convalidated AS validated,
    md5(replace(replace(regexp_replace(lower(pg_catalog.pg_get_constraintdef(constraint_record.oid, TRUE)), '[[:space:]]+', ' ', 'g'), '::character varying::text', '::character varying'), ']::text[]', ']')) AS definition_md5
 FROM pg_catalog.pg_constraint AS constraint_record
 JOIN pg_catalog.pg_class AS table_record
   ON table_record.oid = constraint_record.conrelid
 JOIN pg_catalog.pg_namespace AS namespace_record
   ON namespace_record.oid = table_record.relnamespace
 WHERE namespace_record.nspname = 'public'
   AND table_record.relname IN (
       'brain_entities',
       'entity_relations',
       'graph_outbox',
       'graph_projection_leases',
       'projects'
   )
),
inherited_constraint_mismatches AS (
 SELECT (
     SELECT count(*)
     FROM expected_inherited_constraints AS expected_constraint
     LEFT JOIN observed_inherited_constraints AS constraint_record
       ON constraint_record.table_name = expected_constraint.table_name
      AND constraint_record.constraint_name = expected_constraint.constraint_name
      AND constraint_record.constraint_type = expected_constraint.constraint_type
      AND (
          expected_constraint.delete_action IS NULL
          OR constraint_record.delete_action = expected_constraint.delete_action
      )
      AND constraint_record.definition_md5 = expected_constraint.definition_md5
      AND constraint_record.validated
     WHERE constraint_record.constraint_oid IS NULL
 ) + (
     SELECT count(*)
     FROM observed_inherited_constraints AS constraint_record
     LEFT JOIN expected_inherited_constraints AS expected_constraint
       ON expected_constraint.table_name = constraint_record.table_name
      AND expected_constraint.constraint_name = constraint_record.constraint_name
     WHERE expected_constraint.constraint_name IS NULL
 ) AS value
),
historical_function AS (
 SELECT
     jsonb_build_object(
         'exists', function_record.oid IS NOT NULL,
         'language', language_record.lanname,
         'owner', pg_catalog.pg_get_userbyid(function_record.proowner),
         'proargtypes', function_record.proargtypes::text,
         'proconfig', function_record.proconfig,
         'prokind', function_record.prokind,
         'proleakproof', function_record.proleakproof,
         'pronargdefaults', function_record.pronargdefaults,
         'pronargs', function_record.pronargs,
         'proparallel', function_record.proparallel,
         'proretset', function_record.proretset,
         'prorettype', function_record.prorettype::pg_catalog.regtype::text,
         'prosecdef', function_record.prosecdef,
         'prosrc_octets', pg_catalog.octet_length(
             pg_catalog.convert_to(function_record.prosrc, 'UTF8')
         ),
         'prosrc_sha256', pg_catalog.encode(
             pg_catalog.sha256(pg_catalog.convert_to(function_record.prosrc, 'UTF8')),
             'hex'
         ),
         'proisstrict', function_record.proisstrict,
         'provolatile', function_record.provolatile,
         'schema', namespace_record.nspname
     ) AS observed,
     function_record.oid AS function_oid,
     function_record.proowner,
     function_record.oid IS NOT NULL
       AND namespace_record.nspname = 'public'
       AND language_record.lanname = 'plpgsql'
       AND function_record.prokind = 'f'
       AND function_record.provolatile = 'v'
       AND function_record.proparallel = 'u'
       AND NOT function_record.prosecdef
       AND NOT function_record.proleakproof
       AND NOT function_record.proisstrict
       AND NOT function_record.proretset
       AND function_record.pronargs = 0
       AND function_record.pronargdefaults = 0
       AND function_record.proargtypes = ''::oidvector
       AND function_record.prorettype = 'pg_catalog.trigger'::pg_catalog.regtype
       AND function_record.proconfig IS NULL
       AND pg_catalog.encode(
           pg_catalog.sha256(pg_catalog.convert_to(function_record.prosrc, 'UTF8')),
           'hex'
       ) = '83ca0f7a3230405dae8b4f4e692b4983869b58e4225b6e60bbf96db3f6ae9a59'
       AND pg_catalog.octet_length(
           pg_catalog.convert_to(function_record.prosrc, 'UTF8')
       ) = 96 AS passed
 FROM (VALUES (TRUE)) AS anchor(present)
 LEFT JOIN pg_catalog.pg_proc AS function_record
   ON function_record.oid = pg_catalog.to_regprocedure('public.update_updated_at()')
 LEFT JOIN pg_catalog.pg_namespace AS namespace_record
   ON namespace_record.oid = function_record.pronamespace
 LEFT JOIN pg_catalog.pg_language AS language_record
   ON language_record.oid = function_record.prolang
),
dedicated_function AS (
 SELECT
     jsonb_build_object(
         'exists', function_record.oid IS NOT NULL,
         'guc_condition', pg_catalog.strpos(
             function_record.prosrc,
             'brain_v42.allow_explicit_project_context_updated_at'
         ) > 0,
         'null_error', pg_catalog.strpos(
             function_record.prosrc,
             'explicit_project_context_updated_at_null'
         ) > 0,
         'language', language_record.lanname,
         'owner', pg_catalog.pg_get_userbyid(function_record.proowner),
         'proargtypes', function_record.proargtypes::text,
         'proconfig', function_record.proconfig,
         'prokind', function_record.prokind,
         'proleakproof', function_record.proleakproof,
         'pronargdefaults', function_record.pronargdefaults,
         'pronargs', function_record.pronargs,
         'proparallel', function_record.proparallel,
         'proretset', function_record.proretset,
         'prorettype', function_record.prorettype::pg_catalog.regtype::text,
         'prosecdef', function_record.prosecdef,
         'prosrc_octets', pg_catalog.octet_length(
             pg_catalog.convert_to(function_record.prosrc, 'UTF8')
         ),
         'prosrc_sha256', pg_catalog.encode(
             pg_catalog.sha256(pg_catalog.convert_to(function_record.prosrc, 'UTF8')),
             'hex'
         ),
         'proisstrict', function_record.proisstrict,
         'provolatile', function_record.provolatile,
         'schema', namespace_record.nspname
     ) AS observed,
     function_record.oid AS function_oid,
     function_record.proowner,
     function_record.proacl,
     function_record.oid IS NOT NULL
       AND namespace_record.nspname = 'public'
       AND language_record.lanname = 'plpgsql'
       AND function_record.prokind = 'f'
       AND function_record.provolatile = 'v'
       AND function_record.proparallel = 'u'
       AND NOT function_record.prosecdef
       AND NOT function_record.proleakproof
       AND NOT function_record.proisstrict
       AND NOT function_record.proretset
       AND function_record.pronargs = 0
       AND function_record.pronargdefaults = 0
       AND function_record.proargtypes = ''::oidvector
       AND function_record.prorettype = 'pg_catalog.trigger'::pg_catalog.regtype
       AND function_record.proconfig IS NULL
       AND pg_catalog.strpos(
           function_record.prosrc,
           'brain_v42.allow_explicit_project_context_updated_at'
       ) > 0
       AND pg_catalog.strpos(
           function_record.prosrc,
           'explicit_project_context_updated_at_null'
       ) > 0
       AND pg_catalog.encode(
           pg_catalog.sha256(pg_catalog.convert_to(function_record.prosrc, 'UTF8')),
           'hex'
       ) = '60c6154d6230d1d0e9244d8f20bc6d6b30e887e71263692e54363c96e22c0419'
       AND pg_catalog.octet_length(
           pg_catalog.convert_to(function_record.prosrc, 'UTF8')
       ) = 391 AS passed
 FROM (VALUES (TRUE)) AS anchor(present)
 LEFT JOIN pg_catalog.pg_proc AS function_record
   ON function_record.oid = pg_catalog.to_regprocedure(
       'public.set_project_context_updated_at()'
   )
 LEFT JOIN pg_catalog.pg_namespace AS namespace_record
   ON namespace_record.oid = function_record.pronamespace
 LEFT JOIN pg_catalog.pg_language AS language_record
   ON language_record.oid = function_record.prolang
),
function_acl AS (
 SELECT
     jsonb_build_object(
         'applicable_default_acl_count', (
             SELECT count(*)
             FROM pg_catalog.pg_default_acl AS default_acl_record
             WHERE default_acl_record.defaclrole = dedicated_function.proowner
               AND default_acl_record.defaclobjtype = 'f'
               AND default_acl_record.defaclnamespace IN (
                   0, 'public'::pg_catalog.regnamespace
               )
         ),
         'effective_acl', (
             SELECT COALESCE(
                 jsonb_agg(
                     jsonb_build_object(
                         'grantee', acl_record.grantee,
                         'grantor', acl_record.grantor,
                         'is_grantable', acl_record.is_grantable,
                         'privilege_type', acl_record.privilege_type
                     )
                     ORDER BY acl_record.grantee
                 ),
                 '[]'::jsonb
             )
             FROM pg_catalog.aclexplode(
                 COALESCE(
                     dedicated_function.proacl,
                     pg_catalog.acldefault('f', dedicated_function.proowner)
                 )
             ) AS acl_record
         ),
         'historical_owner', pg_catalog.pg_get_userbyid(historical_function.proowner),
         'migration_role_has_execute', pg_catalog.has_function_privilege(
             current_user, dedicated_function.function_oid, 'EXECUTE'
         ),
         'proacl', dedicated_function.proacl,
         'public_namespace_owner', (
             SELECT pg_catalog.pg_get_userbyid(namespace_record.nspowner)
             FROM pg_catalog.pg_namespace AS namespace_record
             WHERE namespace_record.nspname = 'public'
         )
     ) AS observed,
     CASE
         WHEN dedicated_function.function_oid IS NULL
           OR dedicated_function.proowner IS NULL
           OR historical_function.proowner IS NULL
         THEN FALSE
         ELSE dedicated_function.proowner = historical_function.proowner
           AND dedicated_function.proacl IS NULL
           AND NOT EXISTS (
               SELECT 1
               FROM pg_catalog.pg_default_acl AS default_acl_record
               WHERE default_acl_record.defaclrole = dedicated_function.proowner
                 AND default_acl_record.defaclobjtype = 'f'
                 AND default_acl_record.defaclnamespace IN (
                     0, 'public'::pg_catalog.regnamespace
                 )
           )
           AND COALESCE(
               dedicated_function.proacl,
               pg_catalog.acldefault('f', dedicated_function.proowner)
           ) = pg_catalog.acldefault('f', dedicated_function.proowner)
           AND (
               SELECT count(*) = 2
                 AND count(*) FILTER (
                     WHERE acl_record.grantor = dedicated_function.proowner
                       AND acl_record.grantee = dedicated_function.proowner
                       AND acl_record.privilege_type = 'EXECUTE'
                       AND NOT acl_record.is_grantable
                 ) = 1
                 AND count(*) FILTER (
                     WHERE acl_record.grantor = dedicated_function.proowner
                       AND acl_record.grantee = 0
                       AND acl_record.privilege_type = 'EXECUTE'
                       AND NOT acl_record.is_grantable
                 ) = 1
               FROM pg_catalog.aclexplode(
                   COALESCE(
                       dedicated_function.proacl,
                       pg_catalog.acldefault('f', dedicated_function.proowner)
                   )
               ) AS acl_record
           )
           AND pg_catalog.has_function_privilege(
               current_user, dedicated_function.function_oid, 'EXECUTE'
           )
     END AS passed
 FROM dedicated_function
 CROSS JOIN historical_function
),
project_context_trigger AS (
 SELECT
     COALESCE(
         jsonb_agg(
             jsonb_build_object(
                 'function_oid', trigger_record.tgfoid,
                 'tgargs', pg_catalog.encode(trigger_record.tgargs, 'hex'),
                 'tgattr', trigger_record.tgattr::text,
                 'tgconstrindid', trigger_record.tgconstrindid,
                 'tgconstrrelid', trigger_record.tgconstrrelid,
                 'tgconstraint', trigger_record.tgconstraint,
                 'tgdeferrable', trigger_record.tgdeferrable,
                 'tgenabled', trigger_record.tgenabled,
                 'tginitdeferred', trigger_record.tginitdeferred,
                 'tgisinternal', trigger_record.tgisinternal,
                 'tgnargs', trigger_record.tgnargs,
                 'tgnewtable', trigger_record.tgnewtable,
                 'tgoldtable', trigger_record.tgoldtable,
                 'tgparentid', trigger_record.tgparentid,
                 'tgqual', trigger_record.tgqual,
                 'tgtype', trigger_record.tgtype
             )
         ),
         '[]'::jsonb
     ) AS observed,
     count(*) = 1
       AND bool_and(
           trigger_record.tgfoid = dedicated_function.function_oid
           AND trigger_record.tgtype = 19
           AND trigger_record.tgattr = ''::int2vector
           AND trigger_record.tgqual IS NULL
           AND trigger_record.tgparentid = 0
           AND trigger_record.tgconstraint = 0
           AND trigger_record.tgconstrrelid = 0
           AND trigger_record.tgconstrindid = 0
           AND NOT trigger_record.tgdeferrable
           AND NOT trigger_record.tginitdeferred
           AND trigger_record.tgoldtable IS NULL
           AND trigger_record.tgnewtable IS NULL
           AND trigger_record.tgenabled = 'O'
           AND NOT trigger_record.tgisinternal
           AND trigger_record.tgnargs = 0
           AND trigger_record.tgargs = ''::bytea
       ) AS passed
 FROM dedicated_function
 LEFT JOIN pg_catalog.pg_trigger AS trigger_record
   ON trigger_record.tgrelid = 'public.project_contexts'::pg_catalog.regclass
  AND trigger_record.tgname = 'trg_project_contexts_updated'
),
expected_updated_at_bindings(table_name, trigger_name, function_name) AS (
 VALUES
     ('adrs', 'trg_adrs_updated', 'public.update_updated_at()'),
     ('decisions', 'trg_decisions_updated', 'public.update_updated_at()'),
     ('features', 'set_features_updated_at', 'public.update_updated_at()'),
     ('indexed_plans', 'set_indexed_plans_updated_at', 'public.update_updated_at()'),
     ('learnings', 'trg_learnings_updated', 'public.update_updated_at()'),
     ('project_contexts', 'trg_project_contexts_updated', 'public.set_project_context_updated_at()'),
     ('runbooks', 'trg_runbooks_updated', 'public.update_updated_at()'),
     ('snippets', 'trg_snippets_updated', 'public.update_updated_at()')
),
observed_updated_at_bindings AS (
 SELECT
     table_record.relname AS table_name,
     trigger_record.tgname AS trigger_name,
     function_namespace.nspname || '.' || function_record.proname || '()' AS function_name,
     trigger_record.tgtype = 19
       AND trigger_record.tgattr = ''::int2vector
       AND trigger_record.tgqual IS NULL
       AND trigger_record.tgparentid = 0
       AND trigger_record.tgconstraint = 0
       AND trigger_record.tgconstrrelid = 0
       AND trigger_record.tgconstrindid = 0
       AND NOT trigger_record.tgdeferrable
       AND NOT trigger_record.tginitdeferred
       AND trigger_record.tgoldtable IS NULL
       AND trigger_record.tgnewtable IS NULL
       AND trigger_record.tgenabled = 'O'
       AND NOT trigger_record.tgisinternal
       AND trigger_record.tgnargs = 0
       AND trigger_record.tgargs = ''::bytea AS trigger_exact
 FROM pg_catalog.pg_trigger AS trigger_record
 JOIN pg_catalog.pg_class AS table_record
   ON table_record.oid = trigger_record.tgrelid
 JOIN pg_catalog.pg_namespace AS table_namespace
   ON table_namespace.oid = table_record.relnamespace
 JOIN pg_catalog.pg_proc AS function_record
   ON function_record.oid = trigger_record.tgfoid
 JOIN pg_catalog.pg_namespace AS function_namespace
   ON function_namespace.oid = function_record.pronamespace
 CROSS JOIN historical_function
 CROSS JOIN dedicated_function
 WHERE table_namespace.nspname = 'public'
   AND NOT trigger_record.tgisinternal
   AND trigger_record.tgfoid IN (
       historical_function.function_oid, dedicated_function.function_oid
   )
),
historical_bindings AS (
 SELECT
     expected_rows.value AS expected,
     observed_rows.value AS observed,
     expected_rows.value = observed_rows.value
       AND (
           SELECT count(*)
           FROM pg_catalog.pg_trigger AS trigger_record
           WHERE NOT trigger_record.tgisinternal
             AND trigger_record.tgfoid = dedicated_function.function_oid
       ) = 1
       AND (
           SELECT count(*)
           FROM pg_catalog.pg_trigger AS trigger_record
           WHERE NOT trigger_record.tgisinternal
             AND trigger_record.tgfoid = historical_function.function_oid
       ) = 7 AS passed
 FROM (
     SELECT jsonb_agg(
         jsonb_build_object(
             'function', function_name,
             'table', table_name,
             'trigger', trigger_name,
             'trigger_exact', TRUE
         )
         ORDER BY table_name, trigger_name
     ) AS value
     FROM expected_updated_at_bindings
 ) AS expected_rows
 CROSS JOIN (
     SELECT COALESCE(
         jsonb_agg(
             jsonb_build_object(
                 'function', function_name,
                 'table', table_name,
                 'trigger', trigger_name,
                 'trigger_exact', trigger_exact
             )
             ORDER BY table_name, trigger_name
         ),
         '[]'::jsonb
     ) AS value
     FROM observed_updated_at_bindings
 ) AS observed_rows
 CROSS JOIN historical_function
 CROSS JOIN dedicated_function
),
recovery_039_observation AS (
 SELECT
     jsonb_build_object(
         'bindings', historical_bindings.observed,
         'dedicated_function', dedicated_function.observed,
         'function_acl', function_acl.observed,
         'historical_function', historical_function.observed,
         'project_context_trigger', project_context_trigger.observed
     ) AS observed,
     historical_function.passed
       AND dedicated_function.passed
       AND function_acl.passed
       AND project_context_trigger.passed
       AND historical_bindings.passed AS passed
 FROM historical_function
 CROSS JOIN dedicated_function
 CROSS JOIN function_acl
 CROSS JOIN project_context_trigger
 CROSS JOIN historical_bindings
),
check_rows(id, expected, observed, passed) AS (
 SELECT
     'alembic_head',
     to_jsonb('exactly one applied head'::text),
     to_jsonb(head_observation.value),
     head_observation.value IS NOT NULL
 FROM head_observation
 UNION ALL
 SELECT
     'brain_runtime_032_036_037',
     brain_runtime_observation.expected,
     brain_runtime_observation.observed,
     brain_runtime_observation.expected = brain_runtime_observation.observed
 FROM brain_runtime_observation
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
     'inherited_constraint_definitions',
     to_jsonb(0),
     to_jsonb(inherited_constraint_mismatches.value),
     inherited_constraint_mismatches.value = 0
 FROM inherited_constraint_mismatches
 UNION ALL
 SELECT
     'orphan_feature_artifacts_features',
     to_jsonb(0),
     to_jsonb(orphan_counts.feature_artifacts),
     orphan_counts.feature_artifacts = 0
 FROM orphan_counts
 UNION ALL
 SELECT
     'project_context_updated_at_039',
     to_jsonb(TRUE),
     recovery_039_observation.observed,
     recovery_039_observation.passed
 FROM recovery_039_observation
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
 'contract_id', 'brain-v42/postgresql-recovery/v5',
 'schema_version', 5
)::text
FROM check_rows;
