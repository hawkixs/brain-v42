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
expected_table_columns(table_name, definition_md5) AS (
 VALUES
     (
         'access_log',
         '080f96be27f36599a68c40c95bd0bd17'
     ),
     (
         'adrs',
         '2039bc0d79cf5f6cc401e668fb52bf14'
     ),
     (
         'alembic_version',
         'd84b1be0d698140b9e2c9ff761d37d8e'
     ),
     (
         'brain_entities',
         '29129c0e227139630018e4da8f8274ef'
     ),
     (
         'brain_session_artifacts',
         '6929b0b5a35595521022bcfe25cf5a07'
     ),
     (
         'brain_sessions',
         'd75989f65d6b2929cb4f7d9377f4d3bc'
     ),
     (
         'consolidation_log',
         'b279baa226ff30a6d8cdc251172a0194'
     ),
     (
         'decisions',
         '44bd8dfa34c39f771c0e7969816d88c5'
     ),
     (
         'dream_promotions',
         '96227b8b8bbd4fd262f3e744ba476776'
     ),
     (
         'dream_runs',
         'bf67c985d3f29eca8d7e934b717a95f2'
     ),
     (
         'entity_relations',
         '6f646a72602beef83e1918181f283e73'
     ),
     (
         'feature_artifacts',
         '6cba054db2d8219c073180b14ee9496d'
     ),
     (
         'features',
         '8a0b5dee203e2a4737f918960d25909e'
     ),
     (
         'gitlab_events',
         '0726c6319dc2718242e609e9352bf6f5'
     ),
     (
         'graph_outbox',
         'b4b8228e2ce20ca8f975e385b3712b86'
     ),
     (
         'graph_projection_leases',
         'b2970aedef9c874f2b7d440425ed7430'
     ),
     (
         'indexed_plan_chunks',
         '235da31fd1f3cc48db3e5160da208ea6'
     ),
     (
         'indexed_plans',
         'af0edf1207eed6d417c2221f3a3c18fb'
     ),
     (
         'learnings',
         '7cf3dcfcc977a55ad3fafb2aa6086ae7'
     ),
     (
         'metrics_timeseries',
         'ee54a28a316db593326c1359301b2e7f'
     ),
     (
         'process_metrics',
         'ac55792aabf38d974db303d915ad91c6'
     ),
     (
         'project_aliases',
         '97c3493a40a125569bea527222fa527e'
     ),
     (
         'project_contexts',
         'edbdc6262165d235ade64e4f67da0aa5'
     ),
     (
         'projects',
         '09f1991c6d569501b3da449bf8a2b4b7'
     ),
     (
         'roadmap_curation_proposals',
         'cce22cf7b5d1b4b3e1969aef03261a52'
     ),
     (
         'runbooks',
         '5e3bb2fc80197bfe252bd49e0bd3ae30'
     ),
     (
         'search_log',
         '2460b7be646c241e769f01253305c8b3'
     ),
     (
         'snippets',
         'c83bebf129271bb01cd46a36fce4cf5e'
     ),
     (
         'ticket_extraction_attempts',
         'a680c4c1e923787d518b0bf4f1981ff8'
     ),
     (
         'ticket_extraction_proposals',
         '1df973c8bc80efcb7306f0a1a398ba78'
     ),
     (
         'ticket_messages',
         '4d1af5cdb34fc2a02ebee3abdd9bd1c7'
     ),
     (
         'tickets',
         '9bde91df3a8426f75dc0dc316819192d'
     )
),
observed_table_columns(table_name, definition_md5) AS (
 SELECT
     dense_column.table_name,
     md5(
         COALESCE(
             jsonb_agg(
                 jsonb_build_array(
                    dense_column.dense_position,
                    dense_column.column_name,
                    dense_column.data_type,
                    dense_column.udt_schema,
                    dense_column.udt_name,
                    dense_column.is_nullable,
                    dense_column.character_maximum_length,
                    dense_column.numeric_precision,
                    dense_column.numeric_scale,
                    dense_column.datetime_precision,
                    dense_column.column_default,
                    dense_column.is_identity,
                    dense_column.identity_generation,
                    dense_column.is_generated,
                    dense_column.generation_expression,
                    dense_column.collation_schema,
                    dense_column.collation_name
                 )
                 ORDER BY dense_column.dense_position
             )::text,
             '[]'
         )
     )
 FROM (
     SELECT
         observed_column.table_name,
         row_number() OVER (
             PARTITION BY observed_column.table_name
             ORDER BY attribute_record.attnum
         ) AS dense_position,
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
     FROM information_schema.columns AS observed_column
     JOIN pg_catalog.pg_class AS table_record
       ON table_record.relname = observed_column.table_name
     JOIN pg_catalog.pg_namespace AS namespace_record
       ON namespace_record.oid = table_record.relnamespace
      AND namespace_record.nspname = observed_column.table_schema
     JOIN pg_catalog.pg_attribute AS attribute_record
       ON attribute_record.attrelid = table_record.oid
      AND attribute_record.attname = observed_column.column_name
     WHERE observed_column.table_schema = 'public'
       AND table_record.relkind IN ('r', 'p')
       AND attribute_record.attnum > 0
       AND NOT attribute_record.attisdropped
 ) AS dense_column
 GROUP BY dense_column.table_name
),
table_column_mismatches AS (
 SELECT (
     SELECT count(*)
     FROM expected_table_columns AS expected_table
     LEFT JOIN observed_table_columns AS observed_table
       ON observed_table.table_name = expected_table.table_name
      AND observed_table.definition_md5 = expected_table.definition_md5
     WHERE observed_table.table_name IS NULL
 ) + (
     SELECT count(*)
     FROM observed_table_columns AS observed_table
     LEFT JOIN expected_table_columns AS expected_table
       ON expected_table.table_name = observed_table.table_name
     WHERE expected_table.table_name IS NULL
 ) AS value
),
expected_table_constraints(table_name, constraint_name, definition_md5) AS (
 VALUES
     (
         'access_log',
         'access_log_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'adrs',
         'adrs_merged_into_fkey',
         'f7c41bb869b415969c826bc1a5ce1e77'
     ),
     (
         'adrs',
         'adrs_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'adrs',
         'adrs_status_check',
         'e07c0a0f93b30d9601bc3422f6de76b6'
     ),
     (
         'adrs',
         'ck_adrs_freshness_source',
         '829a8144b787e0f3b9ee7596037abbdd'
     ),
     (
         'adrs',
         'uq_adrs_number_project',
         'ac9e2a11096ed285e7ed2dfea54cccdf'
     ),
     (
         'alembic_version',
         'alembic_version_pkc',
         'a857341eb837ec18a0ba171a94b10631'
     ),
     (
         'brain_entities',
         'brain_entities_lifecycle_valid',
         'cd2da96da432b61cd47e7266f197cd3b'
     ),
     (
         'brain_entities',
         'brain_entities_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'brain_entities',
         'brain_entities_project_key_fkey',
         'f263e7d4d142bbc04ada44537963b892'
     ),
     (
         'brain_entities',
         'brain_entities_scope_valid',
         '7a2522cefd4d98d52bc658343f54daa9'
     ),
     (
         'brain_entities',
         'uq_brain_entities_type_key',
         'e8fd9124dae08d47c87177bf033b8e00'
     ),
     (
         'brain_session_artifacts',
         'brain_session_artifacts_pkey',
         '64a870d2e309f33c14814747b3a28a04'
     ),
     (
         'brain_session_artifacts',
         'brain_session_artifacts_session_id_fkey',
         'cf936a6262f2e34cd4e237e75d156d48'
     ),
     (
         'brain_session_artifacts',
         'brain_session_artifacts_type_valid',
         '88eee5172367ff1161e41ee1b3e4a765'
     ),
     (
         'brain_sessions',
         'brain_sessions_capture_ids_valid',
         '1a8756bd34b4ea7e8d835643d0fa7ceb'
     ),
     (
         'brain_sessions',
         'brain_sessions_client_key_nonblank',
         '8ec1e8c3738bbe2178e04689dd038e0d'
     ),
     (
         'brain_sessions',
         'brain_sessions_focus_outcome_valid',
         'ebc1583eea145e1804fd1508eab2c0d5'
     ),
     (
         'brain_sessions',
         'brain_sessions_nature_valid',
         '9f0ef14672aa448ce2be6e15fa7c4dd4'
     ),
     (
         'brain_sessions',
         'brain_sessions_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'brain_sessions',
         'brain_sessions_project_key_fkey',
         'b863ba166c02670d9dad0a56f9582d59'
     ),
     (
         'brain_sessions',
         'brain_sessions_status_valid',
         '586d25dcdade2c6c4aea9b415a19f7c5'
     ),
     (
         'brain_sessions',
         'brain_sessions_terminal_state_valid',
         'aab51404804e113ec2c452ba0bc21aa8'
     ),
     (
         'brain_sessions',
         'uq_brain_sessions_project_client',
         '153c25b1acb665316ea262444b4d0d79'
     ),
     (
         'consolidation_log',
         'consolidation_log_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'decisions',
         'ck_decisions_freshness_source',
         '829a8144b787e0f3b9ee7596037abbdd'
     ),
     (
         'decisions',
         'decisions_merged_into_fkey',
         '8653f4c271017c4c2bb1ad9214904071'
     ),
     (
         'decisions',
         'decisions_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'decisions',
         'decisions_status_check',
         '2e878b02a7d8d817c0d435e3b85e7288'
     ),
     (
         'decisions',
         'decisions_superseded_by_fkey',
         '617eda849034d5117cd61a9128adc614'
     ),
     (
         'dream_promotions',
         'dream_promotions_dream_run_id_fkey',
         'fc4b262b57571ce045175efc36f3cb72'
     ),
     (
         'dream_promotions',
         'dream_promotions_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'dream_promotions',
         'dream_promotions_source_learning_id_fkey',
         '55502f96a51606d4f62c935c3458f8e4'
     ),
     (
         'dream_promotions',
         'dream_promotions_target_adr_id_fkey',
         '6e65704825a1c71d33ca454bc5a947cd'
     ),
     (
         'dream_promotions',
         'dream_promotions_target_runbook_id_fkey',
         '511cdc735b335e3de922122741a042bf'
     ),
     (
         'dream_promotions',
         'dream_promotions_target_shape',
         'a3fc62b43dc711bdc7f527c142422c14'
     ),
     (
         'dream_runs',
         'dream_runs_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'entity_relations',
         'entity_relations_confidence_valid',
         '8418df632947fd26246036ee546af632'
     ),
     (
         'entity_relations',
         'entity_relations_lifecycle_valid',
         'cd2da96da432b61cd47e7266f197cd3b'
     ),
     (
         'entity_relations',
         'entity_relations_no_self_loop',
         '5a41e6500e5c57c7f2fd2b366d996831'
     ),
     (
         'entity_relations',
         'entity_relations_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'entity_relations',
         'entity_relations_source_entity_id_fkey',
         '10d758915d3544cd60fbd31505ee24d6'
     ),
     (
         'entity_relations',
         'entity_relations_target_entity_id_fkey',
         '8188bcc6f479fce005c8af00803e91e8'
     ),
     (
         'entity_relations',
         'entity_relations_type_valid',
         'de56c40c7349fe61da69b553ee8ad88a'
     ),
     (
         'entity_relations',
         'uq_entity_relations_endpoints_type',
         'aafe3b4835484bc8352bb3e383f3b3de'
     ),
     (
         'feature_artifacts',
         'feature_artifacts_artifact_type_check',
         '73f006012bd6ee945ea20c684a213805'
     ),
     (
         'feature_artifacts',
         'feature_artifacts_feature_id_artifact_type_artifact_id_key',
         '67541809f9a773770bf51a48410ec9ab'
     ),
     (
         'feature_artifacts',
         'feature_artifacts_feature_id_fkey',
         '91639b446b7c775045ef281313b54501'
     ),
     (
         'features',
         'features_merged_into_fkey',
         'f4b264447b736bdd4742166f979d7ec9'
     ),
     (
         'features',
         'features_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'features',
         'features_status_check',
         '66fdaf08add40e772ea10cd66171edce'
     ),
     (
         'gitlab_events',
         'gitlab_events_feature_id_fkey',
         'b11e4e923391185678f53e9b55eed5d0'
     ),
     (
         'gitlab_events',
         'gitlab_events_gitlab_event_id_key',
         'bccbb09a40d23367682deeb18b0dbe8b'
     ),
     (
         'gitlab_events',
         'gitlab_events_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'graph_outbox',
         'graph_outbox_entity_id_fkey',
         '41669d749feab45b3b21507cbe1e72f8'
     ),
     (
         'graph_outbox',
         'graph_outbox_event_id_key',
         '759bdd8d95917e86a4535f61383231f2'
     ),
     (
         'graph_outbox',
         'graph_outbox_exactly_one_aggregate',
         '43e61c6f8f1d8edd4c7ad839435f3b94'
     ),
     (
         'graph_outbox',
         'graph_outbox_operation_valid',
         '02563be4b2f7d105be2c25775fd09852'
     ),
     (
         'graph_outbox',
         'graph_outbox_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'graph_outbox',
         'graph_outbox_relation_id_fkey',
         '22cd3849e65c946557e6c4a9ea483648'
     ),
     (
         'graph_outbox',
         'uq_graph_outbox_entity_revision',
         '7b1e742994175d24227a5f0a6cff40a6'
     ),
     (
         'graph_outbox',
         'uq_graph_outbox_relation_revision',
         'd5fb45f4a7893c5d45460da33fc32d3b'
     ),
     (
         'graph_projection_leases',
         'graph_projection_leases_armed_generation_valid',
         'e8cee37772e9bc681ba229a778eace5d'
     ),
     (
         'graph_projection_leases',
         'graph_projection_leases_pkey',
         '3608ac6e0b09678c35217c69cc4de206'
     ),
     (
         'graph_projection_leases',
         'graph_projection_leases_protocol_valid',
         '3c5970bbe99c7f44f1a0127458293dea'
     ),
     (
         'graph_projection_leases',
         'graph_projection_leases_recovery_state_valid',
         'da1a1dfd81cb4d6f562aa15f101ec34d'
     ),
     (
         'indexed_plan_chunks',
         'indexed_plan_chunks_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'indexed_plan_chunks',
         'indexed_plan_chunks_plan_id_fkey',
         '241362297bac4220730fa577500ddebb'
     ),
     (
         'indexed_plan_chunks',
         'indexed_plan_chunks_plan_type_check',
         '87c9a4dc4a15ecadde7932af882df02e'
     ),
     (
         'indexed_plan_chunks',
         'indexed_plan_chunks_status_check',
         '3ba7c881ee572cc504796e5782f94e90'
     ),
     (
         'indexed_plans',
         'ck_indexed_plans_freshness_source',
         '829a8144b787e0f3b9ee7596037abbdd'
     ),
     (
         'indexed_plans',
         'indexed_plans_file_path_key',
         'd897960d8b44afa21f6bb2335d22b219'
     ),
     (
         'indexed_plans',
         'indexed_plans_freshness_status_check',
         '0ce929b12a9a441ac20250ae6b48154e'
     ),
     (
         'indexed_plans',
         'indexed_plans_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'indexed_plans',
         'indexed_plans_plan_type_check',
         '87c9a4dc4a15ecadde7932af882df02e'
     ),
     (
         'indexed_plans',
         'indexed_plans_status_check',
         '3ba7c881ee572cc504796e5782f94e90'
     ),
     (
         'learnings',
         'ck_learnings_freshness_source',
         '829a8144b787e0f3b9ee7596037abbdd'
     ),
     (
         'learnings',
         'learnings_confidence_check',
         'aafc16b3a6cf11011071494d8d1d3c92'
     ),
     (
         'learnings',
         'learnings_merged_into_fkey',
         '7dd0adc94db13b4f0a90fe1209950eb2'
     ),
     (
         'learnings',
         'learnings_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'learnings',
         'learnings_source_type_check',
         '02fff311457130a186d6f544a73e7a82'
     ),
     (
         'metrics_timeseries',
         'metrics_timeseries_pkey',
         '17b130f521bafc92959eb0c3dac84a30'
     ),
     (
         'process_metrics',
         'process_metrics_pkey',
         '4b49bdfd7447155e31f5c81f46c1cdc1'
     ),
     (
         'project_aliases',
         'project_aliases_pkey',
         'c7c51868ed0f7c086103f53cc6448f81'
     ),
     (
         'project_aliases',
         'project_aliases_project_key_fkey',
         '70f4ff0b3b91c2bfb8098a6dac7a3d0e'
     ),
     (
         'project_contexts',
         'chk_project_key_format',
         'd2f0e69b15612f6476efceb2a228c6fb'
     ),
     (
         'project_contexts',
         'project_contexts_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'project_contexts',
         'project_contexts_project_key_key',
         'a4caa0fb8e4a5e0863641b15d846f24d'
     ),
     (
         'projects',
         'projects_key_format_valid',
         'd2f0e69b15612f6476efceb2a228c6fb'
     ),
     (
         'projects',
         'projects_pkey',
         'b449ae3aa5c5dbcebd0e93fd552a7787'
     ),
     (
         'projects',
         'projects_registry_status_valid',
         '7da8b1fc307de0337b6647b895313e2e'
     ),
     (
         'projects',
         'projects_source_valid',
         'dea4cf93bb2488104f419ca18ed1bcd2'
     ),
     (
         'roadmap_curation_proposals',
         'rcp_op_valid',
         '80132daadd758ad43d4f3d84a6ad715e'
     ),
     (
         'roadmap_curation_proposals',
         'rcp_status_valid',
         'ff9a25db418713793006c3e4302fcd84'
     ),
     (
         'roadmap_curation_proposals',
         'roadmap_curation_proposals_feature_id_fkey',
         '91639b446b7c775045ef281313b54501'
     ),
     (
         'roadmap_curation_proposals',
         'roadmap_curation_proposals_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'runbooks',
         'ck_runbooks_freshness_source',
         '829a8144b787e0f3b9ee7596037abbdd'
     ),
     (
         'runbooks',
         'runbooks_merged_into_fkey',
         '9a29c89e349e65ef76ac765b3497455f'
     ),
     (
         'runbooks',
         'runbooks_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'runbooks',
         'uq_runbooks_title_project',
         '5a98a8b8b4768b60bbd04f2d3494ee1b'
     ),
     (
         'search_log',
         'search_log_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'snippets',
         'ck_snippets_freshness_source',
         '829a8144b787e0f3b9ee7596037abbdd'
     ),
     (
         'snippets',
         'snippets_merged_into_fkey',
         '48b9c8f5e360d24c47eba1fefc7b6072'
     ),
     (
         'snippets',
         'snippets_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'ticket_extraction_attempts',
         'ticket_extraction_attempts_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'ticket_extraction_attempts',
         'ticket_extraction_attempts_status_valid',
         '44b978c0577aeb9c4bf096402349d552'
     ),
     (
         'ticket_extraction_attempts',
         'ticket_extraction_attempts_ticket_id_fkey',
         'a09e46db9b36f812f69e346284722a29'
     ),
     (
         'ticket_extraction_proposals',
         'tep_status_valid',
         'ff9a25db418713793006c3e4302fcd84'
     ),
     (
         'ticket_extraction_proposals',
         'tep_target_type_valid',
         '46892273a2115132e4b616ba49b15b84'
     ),
     (
         'ticket_extraction_proposals',
         'ticket_extraction_proposals_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'ticket_extraction_proposals',
         'ticket_extraction_proposals_ticket_id_fkey',
         '4dcb9f51fbdf1561231a8db87935a0a0'
     ),
     (
         'ticket_messages',
         'ticket_messages_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'ticket_messages',
         'ticket_messages_ticket_id_fkey',
         'a09e46db9b36f812f69e346284722a29'
     ),
     (
         'tickets',
         'tickets_extraction_status_valid',
         '19380479f8c7666bdb7b0aafb934dad8'
     ),
     (
         'tickets',
         'tickets_kind_valid',
         '57c7da799c35e50a19e6ec43fa936c4e'
     ),
     (
         'tickets',
         'tickets_pkey',
         'cc3552dbb61b18accca876af5296eb1f'
     ),
     (
         'tickets',
         'tickets_status_valid',
         'c58bbdfb462872c884887c3927b29f04'
     )
),
observed_table_constraints(table_name, constraint_name, definition_md5) AS (
 SELECT
     table_record.relname,
     constraint_record.conname,
     md5(replace(replace(regexp_replace(lower(pg_catalog.pg_get_constraintdef(constraint_record.oid, TRUE)), '[[:space:]]+', ' ', 'g'), '::character varying::text', '::character varying'), ']::text[]', ']'))
 FROM pg_catalog.pg_constraint AS constraint_record
 JOIN pg_catalog.pg_class AS table_record
   ON table_record.oid = constraint_record.conrelid
 JOIN pg_catalog.pg_namespace AS namespace_record
   ON namespace_record.oid = table_record.relnamespace
 WHERE namespace_record.nspname = 'public'
   AND table_record.relkind IN ('r', 'p')
),
table_constraint_mismatches AS (
 SELECT (
     SELECT count(*)
     FROM expected_table_constraints AS expected_constraint
     LEFT JOIN observed_table_constraints AS observed_constraint
       ON observed_constraint.table_name = expected_constraint.table_name
      AND observed_constraint.constraint_name = expected_constraint.constraint_name
      AND observed_constraint.definition_md5 = expected_constraint.definition_md5
     WHERE observed_constraint.constraint_name IS NULL
 ) + (
     SELECT count(*)
     FROM observed_table_constraints AS observed_constraint
     LEFT JOIN expected_table_constraints AS expected_constraint
       ON expected_constraint.table_name = observed_constraint.table_name
      AND expected_constraint.constraint_name = observed_constraint.constraint_name
     WHERE expected_constraint.constraint_name IS NULL
 ) AS value
),
expected_table_indexes(table_name, index_name, definition_md5) AS (
 VALUES
     (
         'access_log',
         'access_log_pkey',
         '3b02aba92a2351891f2efc7d1588991b'
     ),
     (
         'access_log',
         'idx_access_log_entity',
         '75e690d2b303f02eb0b788d5473f8b9a'
     ),
     (
         'access_log',
         'idx_access_log_time',
         '16bc6720e737dd9d5c43626d0d340ecc'
     ),
     (
         'adrs',
         'adrs_pkey',
         'db3c644c07015b09a9380a28a052ce03'
     ),
     (
         'adrs',
         'idx_adrs_created',
         '2b6580d300a16eb8d786ab7566b78eb5'
     ),
     (
         'adrs',
         'idx_adrs_embedding',
         '2a4ea9eefa88393a499a4d47aa106645'
     ),
     (
         'adrs',
         'idx_adrs_number',
         'fe67291a616ff7c324178763fdd081f9'
     ),
     (
         'adrs',
         'idx_adrs_project',
         '9c8eeba87cb73e28541eaf8c66b138f4'
     ),
     (
         'adrs',
         'idx_adrs_search',
         'd467446a2a59f8126d368b8ca12b4353'
     ),
     (
         'adrs',
         'idx_adrs_status',
         'bd9868b40daad4767e87d7deee442b1b'
     ),
     (
         'adrs',
         'idx_adrs_tags',
         'becf9d62af842c9684d79824d1c468ba'
     ),
     (
         'adrs',
         'uq_adrs_number_project',
         '47a3a631751feca6be1617d063f2e21b'
     ),
     (
         'alembic_version',
         'alembic_version_pkc',
         '6d6b4a4f04ff9a9e6404ff91b90d5fe5'
     ),
     (
         'brain_entities',
         'brain_entities_pkey',
         'e7f8e9706d0ca15ab1df60fb221f1064'
     ),
     (
         'brain_entities',
         'idx_brain_entities_project_lifecycle',
         '59159e2e0ed1243096e9b04b1005ec58'
     ),
     (
         'brain_entities',
         'idx_brain_entities_type_lifecycle',
         'bf933545ea4759505dd2a3f28170ce7e'
     ),
     (
         'brain_entities',
         'uq_brain_entities_source_uuid',
         '6215745c9bbabfc4f6536224f51d9ff0'
     ),
     (
         'brain_entities',
         'uq_brain_entities_type_key',
         'cb21fcf6bca9db57aa5b610f27fc9b47'
     ),
     (
         'brain_session_artifacts',
         'brain_session_artifacts_pkey',
         'fe2984a0da51999576c9e1c26810a0db'
     ),
     (
         'brain_session_artifacts',
         'idx_brain_session_artifacts_session_captured',
         '1537b589099c6198cc66c39ad6656e94'
     ),
     (
         'brain_sessions',
         'brain_sessions_pkey',
         '6763cd8159ef6f0131abbfedfea044bc'
     ),
     (
         'brain_sessions',
         'idx_brain_sessions_project_status_started',
         'daf2b70c6799177168837efedcb0dbe8'
     ),
     (
         'brain_sessions',
         'uq_brain_sessions_connection',
         '62b298d247237eddf60cb4ba28693af4'
     ),
     (
         'brain_sessions',
         'uq_brain_sessions_project_client',
         '28c33a3d73bf9f0c64d322978b7118a4'
     ),
     (
         'consolidation_log',
         'consolidation_log_pkey',
         'a35dd1a44b54267436db4ee72cd4e921'
     ),
     (
         'consolidation_log',
         'idx_consolidation_log_entity_type',
         '3c9de9496d73bb185c24c99fbbea3525'
     ),
     (
         'decisions',
         'decisions_pkey',
         '9c7035051f9224703c5a04ad4be8ae08'
     ),
     (
         'decisions',
         'idx_decisions_created',
         'c6eefd4ab9b51a52feb30eee659ef0bb'
     ),
     (
         'decisions',
         'idx_decisions_embedding',
         'a3cbe20f2859ebd6348b306b11d382a8'
     ),
     (
         'decisions',
         'idx_decisions_project',
         '5c04187a9e217b78234a87362b482b1d'
     ),
     (
         'decisions',
         'idx_decisions_search',
         'fd05a6c6787e891eddb50e4a15e16c5f'
     ),
     (
         'decisions',
         'idx_decisions_status',
         'b1ee7115d46de9af0b8f06fd9e39ab0b'
     ),
     (
         'decisions',
         'idx_decisions_tags',
         '9d23a3c42564c1b221c059bbd87d791e'
     ),
     (
         'dream_promotions',
         'dream_promotions_pkey',
         'b568da32222e18349be611b0e56f5550'
     ),
     (
         'dream_promotions',
         'idx_dream_promotions_created',
         'bebe8e87be3fe9acfb900ab7d0fd9afd'
     ),
     (
         'dream_promotions',
         'idx_dream_promotions_source',
         '5e77cdcf06f103b50cca1fe52ee5e3d6'
     ),
     (
         'dream_promotions',
         'idx_dream_promotions_source_materialized',
         '79c3d8ef73e7df94b4b1dffcef7d35a6'
     ),
     (
         'dream_runs',
         'dream_runs_pkey',
         '9d8c42ccacc95a8c2a8bc5f77e6c86cd'
     ),
     (
         'dream_runs',
         'idx_dream_runs_date',
         '521b7254b90387d596202521abe4e6dc'
     ),
     (
         'dream_runs',
         'idx_dream_runs_date_project',
         '27b7d8baa46b8d19d29fb6ce6a6086c5'
     ),
     (
         'entity_relations',
         'entity_relations_pkey',
         '217e4ca0931ea037192712f456ff7577'
     ),
     (
         'entity_relations',
         'idx_entity_relations_source_active',
         '0af525f7cbb3ddfc3763838f93bc6acd'
     ),
     (
         'entity_relations',
         'idx_entity_relations_target_active',
         '5fe4e2413a47315d52716e4999c08991'
     ),
     (
         'entity_relations',
         'idx_entity_relations_type_active',
         'd900d75c260e8b647bf8fc0a8942ed2a'
     ),
     (
         'entity_relations',
         'uq_entity_relations_endpoints_type',
         '1fd361469b76df214973bb3f938b498a'
     ),
     (
         'feature_artifacts',
         'feature_artifacts_feature_id_artifact_type_artifact_id_key',
         '63191eae6f0ceafa8e2bc368a27cc153'
     ),
     (
         'feature_artifacts',
         'idx_feature_artifacts_artifact',
         '140d1872f5e9b29496772179580e34d5'
     ),
     (
         'feature_artifacts',
         'idx_feature_artifacts_feature_id',
         'b6bdaba423481c0c44c5316daa9b5b55'
     ),
     (
         'features',
         'features_pkey',
         '1135de7df0965d49ad11302882830e10'
     ),
     (
         'features',
         'idx_features_embedding',
         'a8576e928ea08edb8040378cd4c9a162'
     ),
     (
         'features',
         'idx_features_project_key',
         '6c9b6acbf7eb892fe2f41dfbf8ace31c'
     ),
     (
         'features',
         'idx_features_status',
         '42cf40e0032ab3bed020c2fc29a0ecb3'
     ),
     (
         'gitlab_events',
         'gitlab_events_gitlab_event_id_key',
         '23d10cb17804b7748b1fc8223be4882c'
     ),
     (
         'gitlab_events',
         'gitlab_events_pkey',
         '1f32d87b87b48ea05b8383b304b6abce'
     ),
     (
         'gitlab_events',
         'idx_gitlab_events_embedding',
         'd4db16523b2111bf59fb245295b46bdb'
     ),
     (
         'gitlab_events',
         'idx_gitlab_events_event_type',
         'c8f92095f7f04b7ee8352cc42740a4cf'
     ),
     (
         'gitlab_events',
         'idx_gitlab_events_feature_id',
         'a28d2442b47c3535f66abbae8f5e7b8b'
     ),
     (
         'gitlab_events',
         'idx_gitlab_events_project_key',
         '0eb2aad3eb1d20bb3949b6c8272bd1fd'
     ),
     (
         'graph_outbox',
         'graph_outbox_event_id_key',
         'a56287d3b45a1f279ec5b4cd33ceafd5'
     ),
     (
         'graph_outbox',
         'graph_outbox_pkey',
         '474a1b1b4ac19d21b74731ac8d9a9999'
     ),
     (
         'graph_outbox',
         'idx_graph_outbox_pending',
         'ca916f57c7b789b027252a40cd189aa1'
     ),
     (
         'graph_outbox',
         'uq_graph_outbox_entity_revision',
         'ad9028401163d52968e71007bd54bbd7'
     ),
     (
         'graph_outbox',
         'uq_graph_outbox_relation_revision',
         '38e7cf6ec07210a1bf3fbe6fb369b048'
     ),
     (
         'graph_projection_leases',
         'graph_projection_leases_pkey',
         '093ed839687394ee6da85086e39e6da7'
     ),
     (
         'indexed_plan_chunks',
         'idx_plan_chunks_embedding',
         'd36d264a9bfff7c3ee73fde010536c1d'
     ),
     (
         'indexed_plan_chunks',
         'idx_plan_chunks_pk_type',
         '738e20848b786d22f7b16d00d2c04e44'
     ),
     (
         'indexed_plan_chunks',
         'idx_plan_chunks_plan_id',
         'c5b7981ecd8caa2610594f01c18d3c5c'
     ),
     (
         'indexed_plan_chunks',
         'idx_plan_chunks_search_vector',
         '34426f76207579dd9791b9619ddd73db'
     ),
     (
         'indexed_plan_chunks',
         'idx_plan_chunks_tags',
         '2a8be6445173bcd16bd587ac814a9c2d'
     ),
     (
         'indexed_plan_chunks',
         'indexed_plan_chunks_pkey',
         '82ef8fdaf454cb837e4b7c3e35caf6c9'
     ),
     (
         'indexed_plans',
         'idx_indexed_plans_embedding',
         'a1432e94dce6c2c72996ca54050ad34c'
     ),
     (
         'indexed_plans',
         'idx_indexed_plans_pk_status_fresh',
         '93132db4872d0227ff966cbd8f7d2436'
     ),
     (
         'indexed_plans',
         'idx_indexed_plans_project_key',
         '241eca77733e5ca01d782727ad9eb5db'
     ),
     (
         'indexed_plans',
         'idx_indexed_plans_search_vector',
         '6a7fb499cb47fa0ab4f4992db0bd0d96'
     ),
     (
         'indexed_plans',
         'idx_indexed_plans_tags',
         '48ae61a77333926e21a4ffedcd4f1f3c'
     ),
     (
         'indexed_plans',
         'idx_indexed_plans_updated_at',
         'b1bc4d8f870e0be3c23f3298797e1d53'
     ),
     (
         'indexed_plans',
         'indexed_plans_file_path_key',
         'd83a6c12e5047faee6eb2b1cdd503f2b'
     ),
     (
         'indexed_plans',
         'indexed_plans_pkey',
         '1560100c949dbeca705550e98bbad654'
     ),
     (
         'learnings',
         'idx_learnings_confidence',
         '070158a4815ae642723ab2e9639b93aa'
     ),
     (
         'learnings',
         'idx_learnings_created',
         'cb3897295ff266eddce2700073163314'
     ),
     (
         'learnings',
         'idx_learnings_embedding',
         '8ecca84272d714a065e96adb5175ae60'
     ),
     (
         'learnings',
         'idx_learnings_project',
         'ae0178c49afacaf5721e22b891115906'
     ),
     (
         'learnings',
         'idx_learnings_search',
         '78ef578df43f5de94cf1f3830a3a1746'
     ),
     (
         'learnings',
         'idx_learnings_tags',
         '8970633eea7b0ca17e0cc66cf4cd2c15'
     ),
     (
         'learnings',
         'learnings_pkey',
         'cf799c50aeb28830151ebaf8473b3bf2'
     ),
     (
         'metrics_timeseries',
         'idx_metrics_ts_metric',
         'c304e58964eb60ce016e65a98807e8b6'
     ),
     (
         'metrics_timeseries',
         'metrics_timeseries_pkey',
         '42feb43d92c0b0c7aac32c5462234a8b'
     ),
     (
         'process_metrics',
         'process_metrics_pkey',
         '439f16281a5c90e836b8d9787971f468'
     ),
     (
         'project_aliases',
         'idx_project_aliases_project_key',
         '471fac1b341a5875fe96379547d9a9d0'
     ),
     (
         'project_aliases',
         'project_aliases_pkey',
         'dbbb0b3b0042f9e3c44d6085c3572789'
     ),
     (
         'project_contexts',
         'idx_project_contexts_frameworks',
         '4e00549efed95119d1a60abb6a39bd61'
     ),
     (
         'project_contexts',
         'idx_project_contexts_group',
         '2acf5a58253ca04082c5da9f1c759025'
     ),
     (
         'project_contexts',
         'idx_project_contexts_key',
         '166d90b922ff2216d571eeb4e0e134a1'
     ),
     (
         'project_contexts',
         'idx_project_contexts_languages',
         '600b0d5ace3e89708fdfbd6b92f3ecd9'
     ),
     (
         'project_contexts',
         'project_contexts_pkey',
         '60720b672b40c5b7e9e86221092fea59'
     ),
     (
         'project_contexts',
         'project_contexts_project_key_key',
         '51826fbeb18329de451f50dd940354fa'
     ),
     (
         'projects',
         'projects_pkey',
         '671e28752233598f9e71ed5a655952ec'
     ),
     (
         'roadmap_curation_proposals',
         'idx_rcp_feature',
         '723c87efeb29564d17f33ee638186fcd'
     ),
     (
         'roadmap_curation_proposals',
         'idx_rcp_status',
         'bde87a03d35d10b7a556513605b6098b'
     ),
     (
         'roadmap_curation_proposals',
         'roadmap_curation_proposals_pkey',
         '5844124be59d5ba8e7fd07859ca124c1'
     ),
     (
         'runbooks',
         'idx_runbooks_created',
         'd2a2ab02142f9c51701943be5c2eb527'
     ),
     (
         'runbooks',
         'idx_runbooks_embedding',
         '0dee990c99f8be08099b8f9b1c30b04b'
     ),
     (
         'runbooks',
         'idx_runbooks_project',
         '601163e54debd7511db682812b4d8cb5'
     ),
     (
         'runbooks',
         'idx_runbooks_search',
         'f6a4a278aa0543fcf3a41d8d7a6e7115'
     ),
     (
         'runbooks',
         'idx_runbooks_tags',
         '26158f037d438645cf7be3a07bb0b3e7'
     ),
     (
         'runbooks',
         'runbooks_pkey',
         'ba093fd99be2e835cc355b172ff11e9f'
     ),
     (
         'runbooks',
         'uq_runbooks_title_project',
         '7ebdce0989c092bf05768f8f4813bfc4'
     ),
     (
         'search_log',
         'idx_search_log_created',
         'd11b80cf68f799878b083c0d5d8394c5'
     ),
     (
         'search_log',
         'idx_search_log_tool',
         '32dd51996b1292c5ca8de9f7400c4751'
     ),
     (
         'search_log',
         'search_log_pkey',
         '27ffe8c44760f6f3a927bf76782083aa'
     ),
     (
         'snippets',
         'idx_snippets_created',
         'a7d61cd4e7dc44a2c08861ac061cd6f0'
     ),
     (
         'snippets',
         'idx_snippets_embedding',
         '868c817e2c5fb5f18991218399296b48'
     ),
     (
         'snippets',
         'idx_snippets_language',
         'd8230dd574b7ef8322a075e7b1e7d8fb'
     ),
     (
         'snippets',
         'idx_snippets_project',
         'ff7f97797320fafced144c073bc417df'
     ),
     (
         'snippets',
         'idx_snippets_search',
         'cc35502f48fa9a22c93486d016c75732'
     ),
     (
         'snippets',
         'idx_snippets_tags',
         'ed0300a1e5607e7398150c6dce555c46'
     ),
     (
         'snippets',
         'idx_snippets_use_count',
         '846e0cd705e3880171a9b2037df99f8c'
     ),
     (
         'snippets',
         'snippets_pkey',
         '7b8be18207148c16d3f5d3d27ff935de'
     ),
     (
         'ticket_extraction_attempts',
         'idx_ticket_extraction_attempts_date',
         'f65d5ab1d64f60b2753f90ff96ffefb7'
     ),
     (
         'ticket_extraction_attempts',
         'idx_ticket_extraction_attempts_ticket',
         '6115ec9e8afcb7ea2b8c9d27ea292eb5'
     ),
     (
         'ticket_extraction_attempts',
         'ticket_extraction_attempts_pkey',
         '836dc491256684c1dcc16297688ddb72'
     ),
     (
         'ticket_extraction_proposals',
         'idx_tep_status',
         '9d471f9d104d458aac3ef36e0973135f'
     ),
     (
         'ticket_extraction_proposals',
         'idx_tep_ticket',
         '03f2e5ec7fb9fc243de0c5c0f43ae730'
     ),
     (
         'ticket_extraction_proposals',
         'ticket_extraction_proposals_pkey',
         '9b5330f9d2efbebb14cd1155dada4a71'
     ),
     (
         'ticket_messages',
         'idx_ticket_messages_ticket',
         'bbb9d16a189fe3b79dd040ef7efa2633'
     ),
     (
         'ticket_messages',
         'ticket_messages_pkey',
         '566c075073fe8f5188e3d6ab00aa0f4d'
     ),
     (
         'tickets',
         'idx_tickets_extraction_pending',
         'da93474eac1aaac0956de3acea0d4bf1'
     ),
     (
         'tickets',
         'idx_tickets_from_project_status',
         '45f1c797a5417b3765cbcf9381ffa547'
     ),
     (
         'tickets',
         'idx_tickets_to_project_status',
         '8a2ccee50b78da2786aacad203a5da25'
     ),
     (
         'tickets',
         'tickets_pkey',
         '9581e2367ac6b881093b276518f6ce80'
     )
),
observed_table_indexes(table_name, index_name, definition_md5) AS (
 SELECT
     table_record.relname,
     index_class.relname,
     md5(pg_catalog.pg_get_indexdef(index_record.indexrelid))
 FROM pg_catalog.pg_index AS index_record
 JOIN pg_catalog.pg_class AS index_class
   ON index_class.oid = index_record.indexrelid
 JOIN pg_catalog.pg_class AS table_record
   ON table_record.oid = index_record.indrelid
 JOIN pg_catalog.pg_namespace AS namespace_record
   ON namespace_record.oid = table_record.relnamespace
 WHERE namespace_record.nspname = 'public'
   AND table_record.relkind IN ('r', 'p')
),
table_index_mismatches AS (
 SELECT (
     SELECT count(*)
     FROM expected_table_indexes AS expected_index
     LEFT JOIN observed_table_indexes AS observed_index
       ON observed_index.table_name = expected_index.table_name
      AND observed_index.index_name = expected_index.index_name
      AND observed_index.definition_md5 = expected_index.definition_md5
     WHERE observed_index.index_name IS NULL
 ) + (
     SELECT count(*)
     FROM observed_table_indexes AS observed_index
     LEFT JOIN expected_table_indexes AS expected_index
       ON expected_index.table_name = observed_index.table_name
      AND expected_index.index_name = observed_index.index_name
     WHERE expected_index.index_name IS NULL
 ) AS value
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
expected_historical_indexes(
 table_name,
 index_name,
 is_unique,
 is_primary,
 columns,
 definition_md5
) AS (
 VALUES
     (
         'brain_entities',
         'brain_entities_pkey',
         TRUE,
         TRUE,
         jsonb_build_array('id'),
         'e7f8e9706d0ca15ab1df60fb221f1064'
     ),
     (
         'brain_entities',
         'idx_brain_entities_project_lifecycle',
         FALSE,
         FALSE,
         jsonb_build_array('project_key', 'lifecycle'),
         '59159e2e0ed1243096e9b04b1005ec58'
     ),
     (
         'brain_entities',
         'idx_brain_entities_type_lifecycle',
         FALSE,
         FALSE,
         jsonb_build_array('entity_type', 'lifecycle'),
         'bf933545ea4759505dd2a3f28170ce7e'
     ),
     (
         'brain_entities',
         'uq_brain_entities_source_uuid',
         TRUE,
         FALSE,
         jsonb_build_array('source_uuid'),
         '6215745c9bbabfc4f6536224f51d9ff0'
     ),
     (
         'brain_entities',
         'uq_brain_entities_type_key',
         TRUE,
         FALSE,
         jsonb_build_array('entity_type', 'entity_key'),
         'cb21fcf6bca9db57aa5b610f27fc9b47'
     ),
     (
         'entity_relations',
         'entity_relations_pkey',
         TRUE,
         TRUE,
         jsonb_build_array('id'),
         '217e4ca0931ea037192712f456ff7577'
     ),
     (
         'entity_relations',
         'idx_entity_relations_source_active',
         FALSE,
         FALSE,
         jsonb_build_array('source_entity_id', 'lifecycle'),
         '0af525f7cbb3ddfc3763838f93bc6acd'
     ),
     (
         'entity_relations',
         'idx_entity_relations_target_active',
         FALSE,
         FALSE,
         jsonb_build_array('target_entity_id', 'lifecycle'),
         '5fe4e2413a47315d52716e4999c08991'
     ),
     (
         'entity_relations',
         'idx_entity_relations_type_active',
         FALSE,
         FALSE,
         jsonb_build_array('relation_type', 'lifecycle'),
         'd900d75c260e8b647bf8fc0a8942ed2a'
     ),
     (
         'entity_relations',
         'uq_entity_relations_endpoints_type',
         TRUE,
         FALSE,
         jsonb_build_array('source_entity_id', 'target_entity_id', 'relation_type'),
         '1fd361469b76df214973bb3f938b498a'
     ),
     (
         'graph_outbox',
         'graph_outbox_event_id_key',
         TRUE,
         FALSE,
         jsonb_build_array('event_id'),
         'a56287d3b45a1f279ec5b4cd33ceafd5'
     ),
     (
         'graph_outbox',
         'graph_outbox_pkey',
         TRUE,
         TRUE,
         jsonb_build_array('id'),
         '474a1b1b4ac19d21b74731ac8d9a9999'
     ),
     (
         'graph_outbox',
         'idx_graph_outbox_pending',
         FALSE,
         FALSE,
         jsonb_build_array('available_at', 'id'),
         'ca916f57c7b789b027252a40cd189aa1'
     ),
     (
         'graph_outbox',
         'uq_graph_outbox_entity_revision',
         TRUE,
         FALSE,
         jsonb_build_array('entity_id', 'aggregate_revision'),
         'ad9028401163d52968e71007bd54bbd7'
     ),
     (
         'graph_outbox',
         'uq_graph_outbox_relation_revision',
         TRUE,
         FALSE,
         jsonb_build_array('relation_id', 'aggregate_revision'),
         '38e7cf6ec07210a1bf3fbe6fb369b048'
     ),
     (
         'graph_projection_leases',
         'graph_projection_leases_pkey',
         TRUE,
         TRUE,
         jsonb_build_array('slot'),
         '093ed839687394ee6da85086e39e6da7'
     ),
     (
         'projects',
         'projects_pkey',
         TRUE,
         TRUE,
         jsonb_build_array('project_key'),
         '671e28752233598f9e71ed5a655952ec'
     )
),
observed_historical_indexes AS (
 SELECT
     source_table.relname AS table_name,
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
   AND source_table.relname IN (
       'brain_entities',
       'entity_relations',
       'graph_outbox',
       'graph_projection_leases',
       'projects'
   )
 GROUP BY
     source_table.relname,
     index_table.relname,
     index_record.indexrelid,
     index_record.indisunique,
     index_record.indisprimary,
     index_record.indisvalid,
     index_record.indisready
),
historical_index_mismatches AS (
 SELECT count(*) AS value
 FROM (
     SELECT expected_index.index_name
     FROM expected_historical_indexes AS expected_index
     LEFT JOIN observed_historical_indexes AS observed_index
       ON observed_index.table_name = expected_index.table_name
      AND observed_index.index_name = expected_index.index_name
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
     FROM observed_historical_indexes AS observed_index
     WHERE NOT EXISTS (
         SELECT 1
         FROM expected_historical_indexes AS expected_index
         WHERE expected_index.table_name = observed_index.table_name
           AND expected_index.index_name = observed_index.index_name
     )
 ) AS mismatched_historical_index
),
expected_historical_column_fingerprints(table_name, definition_md5) AS (
 VALUES
     ('brain_entities', '29129c0e227139630018e4da8f8274ef'),
     ('entity_relations', '6f646a72602beef83e1918181f283e73'),
     ('graph_outbox', 'b4b8228e2ce20ca8f975e385b3712b86'),
     ('graph_projection_leases', 'b2970aedef9c874f2b7d440425ed7430'),
     ('projects', '09f1991c6d569501b3da449bf8a2b4b7')
),
observed_historical_column_fingerprints(table_name, definition_md5) AS (
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
       SELECT table_name FROM expected_historical_column_fingerprints
   )
 GROUP BY observed_column.table_name
),
historical_column_mismatches AS (
 SELECT count(*) AS value
 FROM expected_historical_column_fingerprints AS expected_fingerprint
 LEFT JOIN observed_historical_column_fingerprints AS observed_fingerprint
   ON observed_fingerprint.table_name = expected_fingerprint.table_name
  AND observed_fingerprint.definition_md5 = expected_fingerprint.definition_md5
 WHERE observed_fingerprint.table_name IS NULL
),
expected_historical_relations(table_name) AS (
 VALUES
     ('brain_entities'),
     ('entity_relations'),
     ('graph_outbox'),
     ('graph_projection_leases'),
     ('projects')
),
historical_relation_property_mismatches AS (
 SELECT count(*) AS value
 FROM expected_historical_relations AS expected_relation
 LEFT JOIN pg_catalog.pg_namespace AS namespace_record
   ON namespace_record.nspname = 'public'
 LEFT JOIN pg_catalog.pg_class AS relation_record
   ON relation_record.relnamespace = namespace_record.oid
  AND relation_record.relname = expected_relation.table_name
  AND relation_record.relkind = 'r'
  AND relation_record.relpersistence = 'p'
  AND NOT relation_record.relispartition
  AND NOT relation_record.relhasrules
  AND NOT relation_record.relrowsecurity
  AND NOT relation_record.relforcerowsecurity
  AND cardinality(COALESCE(relation_record.reloptions, ARRAY[]::text[])) = 0
 LEFT JOIN pg_catalog.pg_am AS access_method
   ON access_method.oid = relation_record.relam
  AND access_method.amname = 'heap'
 WHERE relation_record.oid IS NULL
    OR access_method.oid IS NULL
    OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_inherits AS inheritance_link
        WHERE relation_record.oid IN (
            inheritance_link.inhrelid,
            inheritance_link.inhparent
        )
    )
),
expected_sequences(
 sequence_name,
 owning_table,
 owning_column,
 data_type,
 increment_by,
 min_value,
 max_value,
 start_value,
 cycles
) AS (
 VALUES
     ('access_log_id_seq', 'access_log', 'id', 'bigint', 1, 1, 9223372036854775807, 1, FALSE),
     ('consolidation_log_id_seq', 'consolidation_log', 'id', 'bigint', 1, 1, 9223372036854775807, 1, FALSE),
     ('dream_promotions_id_seq', 'dream_promotions', 'id', 'bigint', 1, 1, 9223372036854775807, 1, FALSE),
     ('dream_runs_id_seq', 'dream_runs', 'id', 'integer', 1, 1, 2147483647, 1, FALSE),
     ('graph_outbox_id_seq', 'graph_outbox', 'id', 'bigint', 1, 1, 9223372036854775807, 1, FALSE),
     ('roadmap_curation_proposals_id_seq', 'roadmap_curation_proposals', 'id', 'bigint', 1, 1, 9223372036854775807, 1, FALSE),
     ('search_log_id_seq', 'search_log', 'id', 'bigint', 1, 1, 9223372036854775807, 1, FALSE),
     ('ticket_extraction_attempts_id_seq', 'ticket_extraction_attempts', 'id', 'bigint', 1, 1, 9223372036854775807, 1, FALSE),
     ('ticket_extraction_proposals_id_seq', 'ticket_extraction_proposals', 'id', 'bigint', 1, 1, 9223372036854775807, 1, FALSE)
),
observed_sequences AS (
 SELECT
     sequence_record.relname AS sequence_name,
     owning_table.relname AS owning_table,
     owning_column.attname AS owning_column,
     sequence_definition.seqtypid::pg_catalog.regtype::text AS data_type,
     sequence_definition.seqincrement AS increment_by,
     sequence_definition.seqmin AS min_value,
     sequence_definition.seqmax AS max_value,
     sequence_definition.seqstart AS start_value,
     sequence_definition.seqcycle AS cycles,
     sequence_record.oid AS sequence_oid
 FROM pg_catalog.pg_class AS sequence_record
 JOIN pg_catalog.pg_namespace AS namespace_record
   ON namespace_record.oid = sequence_record.relnamespace
  AND namespace_record.nspname = 'public'
 JOIN pg_catalog.pg_sequence AS sequence_definition
   ON sequence_definition.seqrelid = sequence_record.oid
 LEFT JOIN pg_catalog.pg_depend AS ownership_link
   ON ownership_link.objid = sequence_record.oid
  AND ownership_link.classid = 'pg_catalog.pg_class'::pg_catalog.regclass
  AND ownership_link.refclassid = 'pg_catalog.pg_class'::pg_catalog.regclass
  AND ownership_link.deptype IN ('a', 'i')
 LEFT JOIN pg_catalog.pg_class AS owning_table
   ON owning_table.oid = ownership_link.refobjid
 LEFT JOIN pg_catalog.pg_attribute AS owning_column
   ON owning_column.attrelid = ownership_link.refobjid
  AND owning_column.attnum = ownership_link.refobjsubid
 WHERE sequence_record.relkind = 'S'
),
sequence_property_mismatches AS (
 SELECT (
     SELECT count(*)
     FROM expected_sequences AS expected_sequence
     LEFT JOIN observed_sequences AS sequence_record
       ON sequence_record.sequence_name = expected_sequence.sequence_name
      AND sequence_record.owning_table = expected_sequence.owning_table
      AND sequence_record.owning_column = expected_sequence.owning_column
      AND sequence_record.data_type = expected_sequence.data_type
      AND sequence_record.increment_by = expected_sequence.increment_by
      AND sequence_record.min_value = expected_sequence.min_value
      AND sequence_record.max_value = expected_sequence.max_value
      AND sequence_record.start_value = expected_sequence.start_value
      AND sequence_record.cycles = expected_sequence.cycles
     WHERE sequence_record.sequence_oid IS NULL
 ) + (
     SELECT count(*)
     FROM observed_sequences AS sequence_record
     LEFT JOIN expected_sequences AS expected_sequence
       ON expected_sequence.sequence_name = sequence_record.sequence_name
     WHERE expected_sequence.sequence_name IS NULL
 ) AS value
),
sequence_high_water(sequence_name, highest_assigned) AS (
 VALUES
     ('access_log_id_seq', (SELECT max(id) FROM access_log)),
     ('consolidation_log_id_seq', (SELECT max(id) FROM consolidation_log)),
     ('dream_promotions_id_seq', (SELECT max(id) FROM dream_promotions)),
     ('dream_runs_id_seq', (SELECT max(id) FROM dream_runs)),
     ('graph_outbox_id_seq', (SELECT max(id) FROM graph_outbox)),
     ('roadmap_curation_proposals_id_seq', (SELECT max(id) FROM roadmap_curation_proposals)),
     ('search_log_id_seq', (SELECT max(id) FROM search_log)),
     ('ticket_extraction_attempts_id_seq', (SELECT max(id) FROM ticket_extraction_attempts)),
     ('ticket_extraction_proposals_id_seq', (SELECT max(id) FROM ticket_extraction_proposals))
),
sequence_backfill_mismatches AS (
 SELECT count(*) AS value
 FROM sequence_high_water AS high_water
 LEFT JOIN pg_catalog.pg_sequences AS sequence_state
   ON sequence_state.schemaname = 'public'
  AND sequence_state.sequencename = high_water.sequence_name
 WHERE sequence_state.sequencename IS NULL
    OR COALESCE(sequence_state.last_value, 0) < COALESCE(high_water.highest_assigned, 0)
),
expected_trigger_functions(function_name, source_sha256, source_octets) AS (
 VALUES
     ('enforce_immutable_ticket_participants', 'cdb295b8a5c811467706ac9c10622fe1140d957e51f94b0fb50911ac9629bb30', 256),
     ('enforce_live_feature_artifact_target', '11f79d4116738608988f29c53bb1db708537cc3fdeac18c5bdede106bf6bccd7', 496),
     ('increment_project_focus_revision', '424dfc1a9154dbc48e08ffc70712920cbfdc42e659a1500da681a2e50526df76', 215),
     ('normalize_project_key_alias', '13b945bb4a5c307f430b0b6ba1387a3a38cc0abe8ac8ed58aa391f9a99e63518', 921),
     ('normalize_related_project_aliases', 'f9b325aed559eef8c28c46d5168cd88027e51a851678381146fc94317e012e6a', 572),
     ('reject_project_context_key_change', 'e800aecbe1054d8333babd1c43f1f52893db81b6b8f4e7fc6d167c6cd6f9de82', 226),
     ('set_project_context_updated_at', '60c6154d6230d1d0e9244d8f20bc6d6b30e887e71263692e54363c96e22c0419', 391),
     ('stamp_content_updated_at', '070b4db370dbe20a280a4f75e58edc72f337e9abcce9cadf673af1f1d30b2342', 77),
     ('stamp_freshness_status', '179caf250bf9fe5aae1d1e1fdb040b4b08008a9c5d76cc1f65ebaf3272db86dd', 890),
     ('sync_brain_entity_registry', 'dab84538fedcd42d28038a3055c1b7e6d4e1f7f02f21891e1195cafdb3f0489c', 10485),
     ('sync_project_registry', 'ff39be21e857296038f463ff71eb932a65d7e3be7c7120a2414a3f5832ce4565', 3699),
     ('sync_referenced_project_registry', '6844d14802019487796602f9cef95327f67a2c56798c1cb561541b4537f6a093', 306),
     ('sync_related_project_registry', 'f1dd4dd21283d6a98a9f14e801685b13415f4de23dbdc59336201baafb3d60be', 349),
     ('update_updated_at', '83ca0f7a3230405dae8b4f4e692b4983869b58e4225b6e60bbf96db3f6ae9a59', 96)
),
observed_trigger_functions AS (
 SELECT
     function_record.proname AS function_name,
     pg_catalog.encode(
         pg_catalog.sha256(pg_catalog.convert_to(function_record.prosrc, 'UTF8')),
         'hex'
     ) AS source_sha256,
     pg_catalog.octet_length(
         pg_catalog.convert_to(function_record.prosrc, 'UTF8')
     ) AS source_octets,
     function_record.oid AS function_oid
 FROM pg_catalog.pg_proc AS function_record
 JOIN pg_catalog.pg_namespace AS namespace_record
   ON namespace_record.oid = function_record.pronamespace
  AND namespace_record.nspname = 'public'
 JOIN pg_catalog.pg_language AS language_record
   ON language_record.oid = function_record.prolang
  AND language_record.lanname = 'plpgsql'
 WHERE function_record.prorettype = 'pg_catalog.trigger'::pg_catalog.regtype
   AND function_record.prokind = 'f'
   AND function_record.provolatile = 'v'
   AND function_record.pronargs = 0
   AND function_record.pronargdefaults = 0
   AND NOT function_record.prosecdef
   AND NOT function_record.proleakproof
   AND NOT function_record.proretset
   AND function_record.proconfig IS NULL
),
trigger_function_mismatches AS (
 SELECT (
     SELECT count(*)
     FROM expected_trigger_functions AS expected_function
     LEFT JOIN observed_trigger_functions AS function_record
       ON function_record.function_name = expected_function.function_name
      AND function_record.source_sha256 = expected_function.source_sha256
      AND function_record.source_octets = expected_function.source_octets
     WHERE function_record.function_oid IS NULL
 ) + (
     SELECT count(*)
     FROM observed_trigger_functions AS function_record
     LEFT JOIN expected_trigger_functions AS expected_function
       ON expected_function.function_name = function_record.function_name
     WHERE expected_function.function_name IS NULL
 ) AS value
),
expected_stamping_triggers(
 table_name,
 trigger_name,
 function_name,
 trigger_type,
 condition_md5
) AS (
 VALUES
     ('adrs', 'trg_adrs_content_updated', 'stamp_content_updated_at', 19, 'f2a3f9f632c27f4d75dfe895299c774a'),
     ('adrs', 'trg_adrs_freshness_stamped', 'stamp_freshness_status', 19, '056d459e210e03d2db1c626ffeea2669'),
     ('decisions', 'trg_decisions_content_updated', 'stamp_content_updated_at', 19, 'f6a4a83483289e05e38957a169a14c49'),
     ('decisions', 'trg_decisions_freshness_stamped', 'stamp_freshness_status', 19, '056d459e210e03d2db1c626ffeea2669'),
     ('indexed_plans', 'trg_indexed_plans_freshness_stamped', 'stamp_freshness_status', 19, '056d459e210e03d2db1c626ffeea2669'),
     ('learnings', 'trg_learnings_content_updated', 'stamp_content_updated_at', 19, '9c6809ae143cdcf7de23325b0234583f'),
     ('learnings', 'trg_learnings_freshness_stamped', 'stamp_freshness_status', 19, '056d459e210e03d2db1c626ffeea2669'),
     ('runbooks', 'trg_runbooks_content_updated', 'stamp_content_updated_at', 19, 'efbd8534db123d6a9ed8400c8ead10ac'),
     ('runbooks', 'trg_runbooks_freshness_stamped', 'stamp_freshness_status', 19, '056d459e210e03d2db1c626ffeea2669'),
     ('snippets', 'trg_snippets_content_updated', 'stamp_content_updated_at', 19, 'bf02278bc7cc81fbea4c93557699abad'),
     ('snippets', 'trg_snippets_freshness_stamped', 'stamp_freshness_status', 19, '056d459e210e03d2db1c626ffeea2669')
),
observed_stamping_triggers AS (
 SELECT
     table_record.relname AS table_name,
     trigger_record.tgname AS trigger_name,
     function_record.proname AS function_name,
     trigger_record.tgtype::integer AS trigger_type,
     md5(
         regexp_replace(
             lower(
                 COALESCE(
                     substring(
                         pg_catalog.pg_get_triggerdef(trigger_record.oid)
                         FROM ' WHEN \((.*)\) EXECUTE '
                     ),
                     ''
                 )
             ),
             '[[:space:]]+',
             ' ',
             'g'
         )
     ) AS condition_md5,
     trigger_record.oid AS trigger_oid
 FROM pg_catalog.pg_trigger AS trigger_record
 JOIN pg_catalog.pg_class AS table_record
   ON table_record.oid = trigger_record.tgrelid
 JOIN pg_catalog.pg_namespace AS namespace_record
   ON namespace_record.oid = table_record.relnamespace
  AND namespace_record.nspname = 'public'
 JOIN pg_catalog.pg_proc AS function_record
   ON function_record.oid = trigger_record.tgfoid
 WHERE NOT trigger_record.tgisinternal
   AND trigger_record.tgenabled = 'O'
   AND function_record.proname IN ('stamp_content_updated_at', 'stamp_freshness_status')
),
stamping_trigger_mismatches AS (
 SELECT (
     SELECT count(*)
     FROM expected_stamping_triggers AS expected_trigger
     LEFT JOIN observed_stamping_triggers AS trigger_record
       ON trigger_record.table_name = expected_trigger.table_name
      AND trigger_record.trigger_name = expected_trigger.trigger_name
      AND trigger_record.function_name = expected_trigger.function_name
      AND trigger_record.trigger_type = expected_trigger.trigger_type
      AND trigger_record.condition_md5 = expected_trigger.condition_md5
     WHERE trigger_record.trigger_oid IS NULL
 ) + (
     SELECT count(*)
     FROM observed_stamping_triggers AS trigger_record
     LEFT JOIN expected_stamping_triggers AS expected_trigger
       ON expected_trigger.table_name = trigger_record.table_name
      AND expected_trigger.trigger_name = trigger_record.trigger_name
     WHERE expected_trigger.trigger_name IS NULL
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
     'historical_relation_shape',
     jsonb_build_object(
         'historical_column_mismatches', 0,
         'historical_index_mismatches', 0,
         'historical_relation_property_mismatches', 0
     ),
     jsonb_build_object(
         'historical_column_mismatches', historical_column_mismatches.value,
         'historical_index_mismatches', historical_index_mismatches.value,
         'historical_relation_property_mismatches', historical_relation_property_mismatches.value
     ),
     historical_column_mismatches.value = 0
     AND historical_index_mismatches.value = 0
     AND historical_relation_property_mismatches.value = 0
 FROM historical_column_mismatches
 CROSS JOIN historical_index_mismatches
 CROSS JOIN historical_relation_property_mismatches
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
     'sequence_shape',
     jsonb_build_object(
         'sequence_backfill_mismatches', 0,
         'sequence_property_mismatches', 0
     ),
     jsonb_build_object(
         'sequence_backfill_mismatches', sequence_backfill_mismatches.value,
         'sequence_property_mismatches', sequence_property_mismatches.value
     ),
     sequence_backfill_mismatches.value = 0
     AND sequence_property_mismatches.value = 0
 FROM sequence_backfill_mismatches
 CROSS JOIN sequence_property_mismatches
 UNION ALL
 SELECT
     'trigger_function_fingerprints',
     jsonb_build_object(
         'stamping_trigger_mismatches', 0,
         'trigger_function_mismatches', 0
     ),
     jsonb_build_object(
         'stamping_trigger_mismatches', stamping_trigger_mismatches.value,
         'trigger_function_mismatches', trigger_function_mismatches.value
     ),
     stamping_trigger_mismatches.value = 0
     AND trigger_function_mismatches.value = 0
 FROM stamping_trigger_mismatches
 CROSS JOIN trigger_function_mismatches
 UNION ALL
 SELECT
     'table_shape',
     jsonb_build_object(
         'table_column_mismatches', 0,
         'table_constraint_mismatches', 0,
         'table_index_mismatches', 0
     ),
     jsonb_build_object(
         'table_column_mismatches', table_column_mismatches.value,
         'table_constraint_mismatches', table_constraint_mismatches.value,
         'table_index_mismatches', table_index_mismatches.value
     ),
     table_column_mismatches.value = 0
     AND table_constraint_mismatches.value = 0
     AND table_index_mismatches.value = 0
 FROM table_column_mismatches
 CROSS JOIN table_constraint_mismatches
 CROSS JOIN table_index_mismatches
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
