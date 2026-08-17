"""Preserve signed project-context timestamps for the bounded repair transaction.

Revision ID: 039
Revises: 038
"""

from alembic import context, op

revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None

_HISTORICAL_SHA256 = "83ca0f7a3230405dae8b4f4e692b4983869b58e4225b6e60bbf96db3f6ae9a59"
_HISTORICAL_OCTETS = 96
_DEDICATED_SHA256 = "60c6154d6230d1d0e9244d8f20bc6d6b30e887e71263692e54363c96e22c0419"
_DEDICATED_OCTETS = 391
_DOWNGRADE_OPT_IN = "allow_project_context_trigger_downgrade"

_CREATE_FUNCTION = """
CREATE FUNCTION public.set_project_context_updated_at()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY INVOKER
AS $function$
BEGIN
    IF current_setting('brain_v42.allow_explicit_project_context_updated_at', true) = 'on' THEN
        IF NEW.updated_at IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '23502',
                MESSAGE = 'explicit_project_context_updated_at_null';
        END IF;
        RETURN NEW;
    END IF;
    NEW.updated_at := CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$function$;
"""

_REMAP_TO_DEDICATED_TRIGGER = """
CREATE OR REPLACE TRIGGER trg_project_contexts_updated
BEFORE UPDATE ON public.project_contexts
FOR EACH ROW EXECUTE FUNCTION public.set_project_context_updated_at()
"""

_REMAP_TO_HISTORICAL_TRIGGER = """
CREATE OR REPLACE TRIGGER trg_project_contexts_updated
BEFORE UPDATE ON public.project_contexts
FOR EACH ROW EXECUTE FUNCTION public.update_updated_at()
"""


def _catalog_guard(
    *,
    expected_context_function: str,
    expected_sha256: str,
    expected_octets: int,
    expected_dedicated_function_count: int,
    expected_dedicated_binding_count: int,
    expected_historical_binding_count: int,
    capture_public_owner_baseline: bool,
) -> str:
    return f"""
DO $$
DECLARE
    historical_oid oid := 'public.update_updated_at()'::regprocedure;
    expected_oid oid := 'public.{expected_context_function}()'::regprocedure;
    historical_owner oid;
    public_owner oid;
    public_owner_baseline text;
    expected_count integer;
BEGIN
    SELECT proowner INTO historical_owner
    FROM pg_catalog.pg_proc
    WHERE oid = historical_oid;
    IF current_user <> pg_catalog.pg_get_userbyid(historical_owner) THEN
        RAISE EXCEPTION 'migration role does not own public.update_updated_at';
    END IF;
    SELECT nspowner INTO public_owner
    FROM pg_catalog.pg_namespace
    WHERE oid = 'public'::regnamespace;
    IF {capture_public_owner_baseline!r} THEN
        PERFORM pg_catalog.set_config(
            'brain_v42.project_context_public_owner_oid', public_owner::text, true
        );
    ELSE
        public_owner_baseline := current_setting(
            'brain_v42.project_context_public_owner_oid', true
        );
        IF public_owner_baseline IS NULL
           OR public_owner_baseline !~ '^[0-9]+$'
           OR public_owner::text <> public_owner_baseline THEN
            RAISE EXCEPTION 'public schema owner changed during migration';
        END IF;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS p
        JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
        JOIN pg_catalog.pg_language AS l ON l.oid = p.prolang
        WHERE p.oid = historical_oid
          AND n.nspname = 'public'
          AND l.lanname = 'plpgsql'
          AND p.prokind = 'f'
          AND p.provolatile = 'v'
          AND p.proparallel = 'u'
          AND NOT p.prosecdef
          AND NOT p.proleakproof
          AND NOT p.proisstrict
          AND NOT p.proretset
          AND p.pronargs = 0
          AND p.pronargdefaults = 0
          AND p.proargtypes = ''::oidvector
          AND p.prorettype = 'trigger'::regtype
          AND p.proconfig IS NULL
          AND encode(
              pg_catalog.sha256(pg_catalog.convert_to(p.prosrc, 'UTF8')), 'hex'
          ) = '{_HISTORICAL_SHA256}'
          AND octet_length(pg_catalog.convert_to(p.prosrc, 'UTF8')) = {_HISTORICAL_OCTETS}
    ) THEN
        RAISE EXCEPTION 'historical function contract mismatch';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_default_acl AS d
        WHERE d.defaclrole = historical_owner
          AND d.defaclobjtype = 'f'
          AND d.defaclnamespace IN (0, 'public'::regnamespace)
    ) THEN
        RAISE EXCEPTION 'function default ACL drift';
    END IF;
    IF (
        SELECT count(*)
        FROM pg_catalog.pg_proc AS p
        WHERE p.pronamespace = 'public'::regnamespace
          AND p.proname = 'update_updated_at'
    ) <> 1 THEN
        RAISE EXCEPTION 'historical function cardinality mismatch';
    END IF;
    IF (
        SELECT count(*)
        FROM pg_catalog.pg_proc AS p
        WHERE p.pronamespace = 'public'::regnamespace
          AND p.proname = 'set_project_context_updated_at'
    ) <> {expected_dedicated_function_count} THEN
        RAISE EXCEPTION 'dedicated function cardinality mismatch';
    END IF;
    SELECT count(*) INTO expected_count
    FROM pg_catalog.pg_proc AS p
    JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
    JOIN pg_catalog.pg_language AS l ON l.oid = p.prolang
    WHERE p.oid = expected_oid
      AND n.nspname = 'public'
      AND l.lanname = 'plpgsql'
      AND p.prokind = 'f'
      AND p.provolatile = 'v'
      AND p.proparallel = 'u'
      AND NOT p.prosecdef
      AND NOT p.proleakproof
      AND NOT p.proisstrict
      AND NOT p.proretset
      AND p.pronargs = 0
      AND p.pronargdefaults = 0
      AND p.proargtypes = ''::oidvector
      AND p.prorettype = 'trigger'::regtype
      AND p.proconfig IS NULL
      AND encode(
          pg_catalog.sha256(pg_catalog.convert_to(p.prosrc, 'UTF8')), 'hex'
      ) = '{expected_sha256}'
      AND octet_length(pg_catalog.convert_to(p.prosrc, 'UTF8')) = {expected_octets};
    IF expected_count <> 1 THEN
        RAISE EXCEPTION 'dedicated function contract mismatch';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS p
        WHERE p.oid = expected_oid
          AND p.proowner = historical_owner
          AND p.proacl IS NULL
          AND COALESCE(
              p.proacl, pg_catalog.acldefault('f', p.proowner)
          ) = pg_catalog.acldefault('f', p.proowner)
    ) THEN
        RAISE EXCEPTION 'dedicated function owner or ACL mismatch';
    END IF;
    IF (
        SELECT count(*)
        FROM pg_catalog.pg_proc AS p
        CROSS JOIN LATERAL pg_catalog.aclexplode(
            pg_catalog.acldefault('f', p.proowner)
        ) AS acl
        WHERE p.oid = expected_oid
          AND acl.privilege_type = 'EXECUTE'
          AND acl.grantor = p.proowner
          AND NOT acl.is_grantable
          AND acl.grantee IN (p.proowner, 0)
    ) <> 2 THEN
        RAISE EXCEPTION 'effective EXECUTE ACL cardinality mismatch';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS p
        CROSS JOIN LATERAL pg_catalog.aclexplode(
            pg_catalog.acldefault('f', p.proowner)
        ) AS acl
        WHERE p.oid = expected_oid
          AND acl.grantee = p.proowner
          AND acl.grantor = p.proowner
          AND acl.privilege_type = 'EXECUTE'
          AND NOT acl.is_grantable
    ) OR NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS p
        CROSS JOIN LATERAL pg_catalog.aclexplode(
            pg_catalog.acldefault('f', p.proowner)
        ) AS acl
        WHERE p.oid = expected_oid
          AND acl.grantee = 0
          AND acl.grantor = p.proowner
          AND acl.privilege_type = 'EXECUTE'
          AND NOT acl.is_grantable
    ) THEN
        RAISE EXCEPTION 'owner or PUBLIC EXECUTE ACL mismatch';
    END IF;
    IF (
        SELECT count(*)
        FROM pg_catalog.pg_trigger AS t
        WHERE t.tgrelid = 'public.project_contexts'::regclass
          AND t.tgname = 'trg_project_contexts_updated'
          AND t.tgfoid = expected_oid
          AND t.tgtype = 19
          AND t.tgattr = ''::int2vector
          AND t.tgqual IS NULL
          AND t.tgparentid = 0
          AND t.tgconstraint = 0
          AND t.tgconstrrelid = 0
          AND t.tgconstrindid = 0
          AND NOT t.tgdeferrable
          AND NOT t.tginitdeferred
          AND t.tgoldtable IS NULL
          AND t.tgnewtable IS NULL
          AND t.tgenabled = 'O'
          AND NOT t.tgisinternal
          AND t.tgnargs = 0
          AND t.tgargs = ''::bytea
    ) <> 1 THEN
        RAISE EXCEPTION 'project_contexts trigger contract mismatch';
    END IF;
    IF (
        SELECT count(*)
        FROM (VALUES
            ('public.decisions'::regclass, 'trg_decisions_updated'),
            ('public.learnings'::regclass, 'trg_learnings_updated'),
            ('public.snippets'::regclass, 'trg_snippets_updated'),
            ('public.runbooks'::regclass, 'trg_runbooks_updated'),
            ('public.adrs'::regclass, 'trg_adrs_updated'),
            ('public.features'::regclass, 'set_features_updated_at'),
            ('public.indexed_plans'::regclass, 'set_indexed_plans_updated_at')
        ) AS required_binding(relid, tgname)
        JOIN pg_catalog.pg_trigger AS t
          ON t.tgrelid = required_binding.relid
         AND t.tgname = required_binding.tgname
        WHERE t.tgfoid = historical_oid
          AND t.tgtype = 19
          AND t.tgattr = ''::int2vector
          AND t.tgqual IS NULL
          AND t.tgparentid = 0
          AND t.tgconstraint = 0
          AND t.tgconstrrelid = 0
          AND t.tgconstrindid = 0
          AND NOT t.tgdeferrable
          AND NOT t.tginitdeferred
          AND t.tgoldtable IS NULL
          AND t.tgnewtable IS NULL
          AND t.tgenabled = 'O'
          AND NOT t.tgisinternal
          AND t.tgnargs = 0
          AND t.tgargs = ''::bytea
    ) <> 7 THEN
        RAISE EXCEPTION 'historical trigger binding mismatch';
    END IF;
    IF (
        SELECT count(*)
        FROM pg_catalog.pg_trigger AS t
        WHERE t.tgfoid = historical_oid
          AND NOT t.tgisinternal
    ) <> {expected_historical_binding_count} THEN
        RAISE EXCEPTION 'historical trigger cardinality mismatch';
    END IF;
    IF (
        SELECT count(*)
        FROM pg_catalog.pg_trigger AS t
        WHERE t.tgfoid = pg_catalog.to_regprocedure(
            'public.set_project_context_updated_at()'
        )
          AND NOT t.tgisinternal
    ) <> {expected_dedicated_binding_count} THEN
        RAISE EXCEPTION 'dedicated trigger cardinality mismatch';
    END IF;
END;
$$;
"""


def upgrade() -> None:
    op.execute("LOCK TABLE public.project_contexts IN ACCESS EXCLUSIVE MODE")
    op.execute(
        _catalog_guard(
            expected_context_function="update_updated_at",
            expected_sha256=_HISTORICAL_SHA256,
            expected_octets=_HISTORICAL_OCTETS,
            expected_dedicated_function_count=0,
            expected_dedicated_binding_count=0,
            expected_historical_binding_count=8,
            capture_public_owner_baseline=True,
        )
    )
    op.execute(_CREATE_FUNCTION)
    op.execute(_REMAP_TO_DEDICATED_TRIGGER)
    op.execute(
        _catalog_guard(
            expected_context_function="set_project_context_updated_at",
            expected_sha256=_DEDICATED_SHA256,
            expected_octets=_DEDICATED_OCTETS,
            expected_dedicated_function_count=1,
            expected_dedicated_binding_count=1,
            expected_historical_binding_count=7,
            capture_public_owner_baseline=False,
        )
    )


def downgrade() -> None:
    arguments = context.get_x_argument(as_dictionary=True)
    if arguments.get(_DOWNGRADE_OPT_IN) != "yes":
        raise RuntimeError("project_context_trigger_downgrade_opt_in_required")
    op.execute("LOCK TABLE public.project_contexts IN ACCESS EXCLUSIVE MODE")
    op.execute(
        _catalog_guard(
            expected_context_function="set_project_context_updated_at",
            expected_sha256=_DEDICATED_SHA256,
            expected_octets=_DEDICATED_OCTETS,
            expected_dedicated_function_count=1,
            expected_dedicated_binding_count=1,
            expected_historical_binding_count=7,
            capture_public_owner_baseline=True,
        )
    )
    op.execute(_REMAP_TO_HISTORICAL_TRIGGER)
    op.execute(
        _catalog_guard(
            expected_context_function="update_updated_at",
            expected_sha256=_HISTORICAL_SHA256,
            expected_octets=_HISTORICAL_OCTETS,
            expected_dedicated_function_count=1,
            expected_dedicated_binding_count=0,
            expected_historical_binding_count=8,
            capture_public_owner_baseline=False,
        )
    )
    op.execute("DROP FUNCTION public.set_project_context_updated_at()")
    op.execute(
        _catalog_guard(
            expected_context_function="update_updated_at",
            expected_sha256=_HISTORICAL_SHA256,
            expected_octets=_HISTORICAL_OCTETS,
            expected_dedicated_function_count=0,
            expected_dedicated_binding_count=0,
            expected_historical_binding_count=8,
            capture_public_owner_baseline=False,
        )
    )
