WITH expected_contract_grants(object_name, grantee, privilege_type) AS (
 VALUES
     ('codex_brain_entity_v1', 'codex_ro', 'SELECT'),
     ('codex_consolidation_log_v1', 'codex_ro', 'SELECT'),
     ('codex_dream_promotion_v1', 'codex_ro', 'SELECT'),
     ('codex_dream_run_v1', 'codex_ro', 'SELECT'),
     ('codex_feature_artifact_v1', 'codex_ro', 'SELECT'),
     ('codex_feature_v1', 'codex_ro', 'SELECT'),
     ('codex_roadmap_curation_proposal_v1', 'codex_ro', 'SELECT'),
     ('codex_ticket_extraction_proposal_v1', 'codex_ro', 'SELECT'),
     ('codex_ticket_message_v1', 'codex_ro', 'SELECT'),
     ('codex_ticket_v1', 'codex_ro', 'SELECT')
),
expected_roles(role_name) AS (
 VALUES ('brain'), ('codex_ro')
),
observed_relations AS (
 SELECT
     relation_record.relname AS object_name,
     relation_record.relkind::text AS object_kind,
     pg_catalog.pg_get_userbyid(relation_record.relowner) AS owner,
     relation_record.relacl AS acl
 FROM pg_catalog.pg_class AS relation_record
 JOIN pg_catalog.pg_namespace AS namespace_record
   ON namespace_record.oid = relation_record.relnamespace
  AND namespace_record.nspname = 'public'
 WHERE relation_record.relkind IN ('r', 'v', 'S', 'm')
),
observed_relation_privileges AS (
 SELECT
     relation_record.object_name,
     COALESCE(pg_catalog.pg_get_userbyid(acl_entry.grantee), 'PUBLIC') AS grantee,
     acl_entry.privilege_type,
     pg_catalog.pg_get_userbyid(acl_entry.grantor) AS grantor,
     acl_entry.is_grantable
 FROM observed_relations AS relation_record
 CROSS JOIN LATERAL pg_catalog.aclexplode(relation_record.acl) AS acl_entry
),
relation_owner_mismatches AS (
 SELECT count(*) AS value
 FROM observed_relations AS relation_record
 WHERE relation_record.owner <> 'brain'
),
contract_grant_mismatches AS (
 SELECT (
     SELECT count(*)
     FROM expected_contract_grants AS expected_grant
     LEFT JOIN observed_relation_privileges AS observed_grant
       ON observed_grant.object_name = expected_grant.object_name
      AND observed_grant.grantee = expected_grant.grantee
      AND observed_grant.privilege_type = expected_grant.privilege_type
     WHERE observed_grant.object_name IS NULL
 ) + (
     SELECT count(*)
     FROM observed_relation_privileges AS observed_grant
     LEFT JOIN expected_contract_grants AS expected_grant
       ON expected_grant.object_name = observed_grant.object_name
      AND expected_grant.grantee = observed_grant.grantee
      AND expected_grant.privilege_type = observed_grant.privilege_type
     WHERE observed_grant.grantee <> 'brain'
       AND expected_grant.object_name IS NULL
 ) AS value
),
unexpected_grantee_mismatches AS (
 SELECT count(*) AS value
 FROM observed_relation_privileges AS observed_grant
 WHERE observed_grant.grantee NOT IN ('brain', 'codex_ro')
    OR observed_grant.grantor <> 'brain'
    OR observed_grant.is_grantable
),
observed_schema_usage AS (
 SELECT count(*) AS value
 FROM pg_catalog.pg_namespace AS namespace_record
 CROSS JOIN LATERAL pg_catalog.aclexplode(namespace_record.nspacl) AS acl_entry
 WHERE namespace_record.nspname = 'public'
   AND pg_catalog.pg_get_userbyid(acl_entry.grantee) = 'codex_ro'
   AND acl_entry.privilege_type = 'USAGE'
   AND NOT acl_entry.is_grantable
),
role_privilege_mismatches AS (
 SELECT (
     SELECT count(*)
     FROM expected_roles AS expected_role
     LEFT JOIN pg_catalog.pg_roles AS role_record
       ON role_record.rolname = expected_role.role_name
     WHERE role_record.oid IS NULL
 ) + (
     SELECT count(*)
     FROM pg_catalog.pg_roles AS role_record
     LEFT JOIN expected_roles AS expected_role
       ON expected_role.role_name = role_record.rolname
     WHERE role_record.rolname NOT LIKE 'pg\_%'
       AND expected_role.role_name IS NULL
 ) + (
     SELECT count(*)
     FROM pg_catalog.pg_roles AS role_record
     WHERE role_record.rolname = 'codex_ro'
       AND (
           role_record.rolsuper
           OR role_record.rolcreatedb
           OR role_record.rolcreaterole
           OR role_record.rolreplication
           OR role_record.rolbypassrls
       )
 ) + (
     SELECT count(*)
     FROM pg_catalog.pg_auth_members AS membership
     JOIN pg_catalog.pg_roles AS member_record
       ON member_record.oid = membership.member
     WHERE member_record.rolname NOT LIKE 'pg\_%'
 ) + (
     SELECT CASE WHEN observed_schema_usage.value = 1 THEN 0 ELSE 1 END
     FROM observed_schema_usage
 ) AS value
),
check_rows(id, expected, observed, passed) AS (
 SELECT
     'acl_and_ownership',
     jsonb_build_object(
         'contract_grant_mismatches', 0,
         'relation_owner_mismatches', 0,
         'role_privilege_mismatches', 0,
         'unexpected_grantee_mismatches', 0
     ),
     jsonb_build_object(
         'contract_grant_mismatches', contract_grant_mismatches.value,
         'relation_owner_mismatches', relation_owner_mismatches.value,
         'role_privilege_mismatches', role_privilege_mismatches.value,
         'unexpected_grantee_mismatches', unexpected_grantee_mismatches.value
     ),
     contract_grant_mismatches.value = 0
     AND relation_owner_mismatches.value = 0
     AND role_privilege_mismatches.value = 0
     AND unexpected_grantee_mismatches.value = 0
 FROM contract_grant_mismatches
 CROSS JOIN relation_owner_mismatches
 CROSS JOIN role_privilege_mismatches
 CROSS JOIN unexpected_grantee_mismatches
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
 'contract_id', 'brain-v42/postgresql-recovery/v5-acl',
 'schema_version', 5
)::text
FROM check_rows;
