"""064 — reproducible least-privilege runtime ACLs, separate from login attributes.

Roles belong to the cluster, so a fresh disposable database can encounter a role
created by another database. Never reset its password or attributes here. The
migrate CLI owns those; object ACLs must come from this chain for recovery proofs.
"""

from alembic import op

revision = "064"
down_revision = "063"
branch_labels = None
depends_on = None

# LegacyGraphStore._ensure_reference_registry directly calls the registry helper.
# MetricsCollector calls vector_dims; PgBaseRepository and ticket_extract use
# vector_norm to reject non-comparable embeddings. These extension members also
# live in public, and are listed by signature rather than granting every function.
# Graph outbox/lease code uses DML and PostgreSQL builtins; notify helpers are
# triggers, which require no runtime EXECUTE grant. Do not grant all functions
# or function defaults: future direct helpers must declare their own ACLs.
_DIRECT_FUNCTIONS = (
    "public.register_referenced_project(text)",
    "public.vector_dims(public.vector)",
    "public.vector_norm(public.vector)",
)


def upgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS "
        "(SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'brain_app') THEN "
        "CREATE ROLE brain_app NOLOGIN; END IF; END $$"
    )
    op.execute("GRANT USAGE ON SCHEMA public, monitoring TO brain_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO brain_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO brain_app")
    for function in _DIRECT_FUNCTIONS:
        op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO brain_app")
    op.execute(
        """
        DO $$ BEGIN
            EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
                'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO brain_app', current_user);
            EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
                'GRANT USAGE, SELECT ON SEQUENCES TO brain_app', current_user);
        END $$
        """
    )


def downgrade() -> None:
    # Find the actual default-ACL owners, including when a different migration
    # owner performs the downgrade. Never DROP OWNED: unrelated grants, ownership
    # and databases elsewhere in the cluster must survive this database's rollback.
    op.execute(
        """
        DO $$ DECLARE owner_name text; BEGIN
          IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'brain_app') THEN
            REVOKE USAGE ON SCHEMA public, monitoring FROM brain_app;
            REVOKE SELECT, INSERT, UPDATE, DELETE
                ON ALL TABLES IN SCHEMA public FROM brain_app;
            REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public FROM brain_app;
        """
        + "\n".join(
            f"            REVOKE EXECUTE ON FUNCTION {function} FROM brain_app;"
            for function in _DIRECT_FUNCTIONS
        )
        + """
            FOR owner_name IN
                SELECT DISTINCT pg_get_userbyid(d.defaclrole)
                FROM pg_default_acl d, LATERAL aclexplode(d.defaclacl) a
                WHERE d.defaclnamespace = 'public'::regnamespace
                  AND d.defaclobjtype IN ('r', 'S')
                  AND a.grantee = (SELECT oid FROM pg_roles WHERE rolname = 'brain_app')
            LOOP
                EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
                    'REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM brain_app', owner_name);
                EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
                    'REVOKE USAGE, SELECT ON SEQUENCES FROM brain_app', owner_name);
            END LOOP;
            IF EXISTS (
                SELECT 1 FROM pg_shdepend
                WHERE refclassid = 'pg_authid'::regclass
                  AND refobjid = (SELECT oid FROM pg_roles WHERE rolname = 'brain_app')
            ) THEN
                RAISE NOTICE 'brain_app retained: database or object dependencies remain';
            ELSE
                DROP ROLE brain_app;
            END IF;
          END IF;
        END $$
        """
    )
