WITH runtime_role AS (
 SELECT oid, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls
 FROM pg_catalog.pg_roles WHERE rolname = 'brain_app'
),
runtime_expected_schemas(name) AS (
 VALUES
     ('public'),
     ('monitoring')
),
runtime_expected_table_privileges(privilege) AS (
 VALUES
     ('SELECT'),
     ('INSERT'),
     ('UPDATE'),
     ('DELETE')
),
runtime_expected_sequence_privileges(privilege) AS (
 VALUES
     ('USAGE'),
     ('SELECT')
),
runtime_expected_functions(signature) AS (
 VALUES
     ('public.register_referenced_project(text)'),
     ('public.vector_dims(public.vector)'),
     ('public.vector_norm(public.vector)')
),
runtime_relations AS (
 SELECT c.oid, c.relowner, c.relacl, c.relkind
 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
),
runtime_expected_relations(object_oid, privilege) AS (
 SELECT r.oid, p.privilege FROM runtime_relations r
 CROSS JOIN runtime_expected_table_privileges p WHERE r.relkind <> 'S'
 UNION ALL
 SELECT r.oid, p.privilege FROM runtime_relations r
 CROSS JOIN runtime_expected_sequence_privileges p WHERE r.relkind = 'S'
),
runtime_observed_relations AS (
 SELECT r.oid AS object_oid, a.privilege_type AS privilege,
        a.is_grantable OR a.grantor <> r.relowner AS invalid
 FROM runtime_relations r CROSS JOIN LATERAL pg_catalog.aclexplode(r.relacl) a
 WHERE a.grantee = (SELECT oid FROM runtime_role)
),
runtime_expected_defaults(kind, privilege) AS (
 SELECT 'r', privilege FROM runtime_expected_table_privileges
 UNION ALL
 SELECT 'S', privilege FROM runtime_expected_sequence_privileges
),
runtime_observed_defaults AS (
 SELECT d.defaclobjtype::text AS kind, a.privilege_type AS privilege,
        a.is_grantable OR a.grantor <> d.defaclrole
          OR d.defaclrole <> (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_user)
          AS invalid
 FROM pg_catalog.pg_default_acl d
 CROSS JOIN LATERAL pg_catalog.aclexplode(d.defaclacl) a
 WHERE d.defaclnamespace = 'public'::pg_catalog.regnamespace
   AND a.grantee = (SELECT oid FROM runtime_role)
),
runtime_observed_functions AS (
 SELECT p.oid, a.is_grantable OR a.grantor <> p.proowner AS invalid
 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
 CROSS JOIN LATERAL pg_catalog.aclexplode(p.proacl) a
 WHERE n.nspname = 'public' AND a.grantee = (SELECT oid FROM runtime_role)
   AND a.privilege_type = 'EXECUTE'
),
runtime_acl_counters AS (
 SELECT
   (SELECT CASE WHEN count(*) = 1 AND NOT bool_or(rolsuper OR rolcreatedb OR rolcreaterole
      OR rolreplication OR rolbypassrls) THEN 0 ELSE 1 END FROM runtime_role)
     + (SELECT count(*) FROM pg_catalog.pg_auth_members
        WHERE member = (SELECT oid FROM runtime_role)) AS role_mismatches,
   (SELECT count(*) FROM runtime_expected_schemas e
    WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n,
      LATERAL pg_catalog.aclexplode(n.nspacl) a
      WHERE n.nspname = e.name AND a.grantee = (SELECT oid FROM runtime_role)
        AND a.privilege_type = 'USAGE' AND NOT a.is_grantable AND a.grantor = n.nspowner))
     + (SELECT count(*) FROM pg_catalog.pg_namespace n,
        LATERAL pg_catalog.aclexplode(n.nspacl) a
        WHERE n.nspname IN (SELECT name FROM runtime_expected_schemas)
          AND a.grantee = (SELECT oid FROM runtime_role)
          AND (a.privilege_type <> 'USAGE' OR a.is_grantable OR a.grantor <> n.nspowner))
     AS schema_mismatches,
   (SELECT count(*) FROM runtime_expected_relations e
      LEFT JOIN runtime_observed_relations o USING (object_oid, privilege)
      WHERE o.object_oid IS NULL)
     + (SELECT count(*) FROM runtime_observed_relations o
        LEFT JOIN runtime_expected_relations e USING (object_oid, privilege)
        WHERE e.object_oid IS NULL OR o.invalid) AS relation_mismatches,
   (SELECT count(*) FROM runtime_expected_functions e
      WHERE NOT EXISTS (SELECT 1 FROM runtime_observed_functions o
        WHERE o.oid = pg_catalog.to_regprocedure(e.signature) AND NOT o.invalid))
     + (SELECT count(*) FROM runtime_observed_functions o
        WHERE o.invalid OR NOT EXISTS (SELECT 1 FROM runtime_expected_functions e
          WHERE pg_catalog.to_regprocedure(e.signature) = o.oid)) AS function_mismatches,
   (SELECT count(*) FROM runtime_expected_defaults e
      LEFT JOIN runtime_observed_defaults o USING (kind, privilege) WHERE o.kind IS NULL)
     + (SELECT count(*) FROM runtime_observed_defaults o
        LEFT JOIN runtime_expected_defaults e USING (kind, privilege)
        WHERE e.kind IS NULL OR o.invalid) AS default_mismatches,
   (SELECT count(*) FROM pg_catalog.pg_shdepend
    WHERE refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
      AND refobjid = (SELECT oid FROM runtime_role) AND deptype = 'o') AS ownership_mismatches
),
expected_contract_grants(object_name, grantee, privilege_type) AS (
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
 UNION ALL
 SELECT relation_record.relname, 'brain_app', expected_grant.privilege
 FROM runtime_expected_relations AS expected_grant
 JOIN pg_catalog.pg_class AS relation_record
   ON relation_record.oid = expected_grant.object_oid
),
expected_roles(role_name) AS (
 VALUES ('brain'), ('codex_ro'), ('brain_app')
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
 WHERE observed_grant.grantee NOT IN ('brain', 'codex_ro', 'brain_app')
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
 SELECT 'runtime_acl_064',
     jsonb_build_object('role_mismatches', 0, 'schema_mismatches', 0,
       'relation_mismatches', 0, 'function_mismatches', 0,
       'default_mismatches', 0, 'ownership_mismatches', 0),
     to_jsonb(runtime_acl_counters),
     role_mismatches = 0 AND schema_mismatches = 0 AND relation_mismatches = 0
       AND function_mismatches = 0 AND default_mismatches = 0 AND ownership_mismatches = 0
 FROM runtime_acl_counters
 UNION ALL
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
 'contract_id', 'brain-v42/postgresql-recovery/v22-acl',
 'schema_version', 22
)::text
FROM check_rows;
