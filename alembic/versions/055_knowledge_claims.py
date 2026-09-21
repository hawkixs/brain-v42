"""Create immutable fact definitions and the claim and verdict ledgers.

Revision ID: 055
Revises: 054

The two ``seq`` columns are ``GENERATED ALWAYS`` identities because verdict
reads use only server insertion order.  A ``bigserial`` is merely a default an
inserting client can override; an always identity refuses a forged order, so
the ordering guarantee and the ordering column remain one object.
"""

from __future__ import annotations

from alembic import context, op

revision = "055"
down_revision = "054"
branch_labels = None
depends_on = None

#: A named acknowledgement: dropping a claim ledger loses facts which no later
#: migration, table, or re-computation can recreate.
_OPT_IN = "allow_claims_downgrade"

_FACT_DEFINITIONS_APPEND_ONLY = """
CREATE OR REPLACE FUNCTION public.knowledge_fact_definitions_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'knowledge_fact_definitions is append-only: % refused', TG_OP;
END;
$function$
"""

_VERDICTS_APPEND_ONLY = """
CREATE OR REPLACE FUNCTION public.knowledge_claim_verdicts_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'knowledge_claim_verdicts is append-only: % refused', TG_OP;
END;
$function$
"""

_CLAIMS_UPDATE_GATE = """
CREATE OR REPLACE FUNCTION public.knowledge_claims_update_gate()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF OLD.id IS DISTINCT FROM NEW.id
       OR OLD.seq IS DISTINCT FROM NEW.seq
       OR OLD.entity_ref_id IS DISTINCT FROM NEW.entity_ref_id
       OR OLD.entity_type IS DISTINCT FROM NEW.entity_type
       OR OLD.project_key IS DISTINCT FROM NEW.project_key
       OR OLD.claim_key IS DISTINCT FROM NEW.claim_key
       OR OLD.statement IS DISTINCT FROM NEW.statement
       OR OLD.fact_name IS DISTINCT FROM NEW.fact_name
       OR OLD.definition_version IS DISTINCT FROM NEW.definition_version
       OR OLD.target IS DISTINCT FROM NEW.target
       OR OLD.expected IS DISTINCT FROM NEW.expected
       OR OLD.expected_resolved IS DISTINCT FROM NEW.expected_resolved
       OR OLD.validity_seconds IS DISTINCT FROM NEW.validity_seconds
       OR OLD.provenance IS DISTINCT FROM NEW.provenance
       OR OLD.declared_by IS DISTINCT FROM NEW.declared_by
       OR OLD.declared_at IS DISTINCT FROM NEW.declared_at
       OR OLD.recorded_at IS DISTINCT FROM NEW.recorded_at
       OR OLD.replaces_id IS DISTINCT FROM NEW.replaces_id
       OR OLD.retired_at IS NOT NULL
       OR NEW.retired_at IS NULL THEN
        RAISE EXCEPTION
            'knowledge_claims only permits retired_at NULL -> non-NULL; UPDATE refused';
    END IF;
    RETURN NEW;
END;
$function$
"""

_CLAIMS_DELETE_GATE = """
CREATE OR REPLACE FUNCTION public.knowledge_claims_delete_gate()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'knowledge_claims is append-only: % refused', TG_OP;
END;
$function$
"""

_CLAIMS_INSERT_GATE = """
CREATE OR REPLACE FUNCTION public.knowledge_claims_insert_gate()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    anchor_entity_type text;
    anchor_project_key varchar(50);
    anchor_scope_kind text;
    anchor_lifecycle text;
    predecessor_entity_ref_id uuid;
    predecessor_claim_key varchar(64);
    predecessor_retired_at timestamptz;
    latest_retired_id uuid;
BEGIN
    SELECT entity_type, project_key, scope_kind, lifecycle
      INTO anchor_entity_type, anchor_project_key, anchor_scope_kind, anchor_lifecycle
      FROM public.brain_entities
     WHERE id = NEW.entity_ref_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'knowledge_claims anchor missing for entity_ref_id %', NEW.entity_ref_id;
    END IF;
    IF anchor_entity_type IS DISTINCT FROM NEW.entity_type THEN
        RAISE EXCEPTION
            'knowledge_claims anchor entity_type mismatch: anchor %, claim %',
            anchor_entity_type, NEW.entity_type;
    END IF;
    IF anchor_scope_kind IS DISTINCT FROM 'project' THEN
        RAISE EXCEPTION
            'knowledge_claims anchor scope_kind must be project, got %', anchor_scope_kind;
    END IF;
    IF anchor_project_key IS DISTINCT FROM NEW.project_key THEN
        RAISE EXCEPTION
            'knowledge_claims anchor project_key mismatch: anchor %, claim %',
            anchor_project_key, NEW.project_key;
    END IF;
    IF anchor_lifecycle IS DISTINCT FROM 'active' THEN
        RAISE EXCEPTION
            'knowledge_claims anchor lifecycle must be active, got %', anchor_lifecycle;
    END IF;

    IF NEW.replaces_id IS NOT NULL THEN
        SELECT entity_ref_id, claim_key, retired_at
          INTO predecessor_entity_ref_id, predecessor_claim_key, predecessor_retired_at
          FROM public.knowledge_claims
         WHERE id = NEW.replaces_id;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'knowledge_claims replacement predecessor % is missing', NEW.replaces_id;
        END IF;
        IF predecessor_entity_ref_id IS DISTINCT FROM NEW.entity_ref_id THEN
            RAISE EXCEPTION
                'knowledge_claims replacement predecessor entity_ref_id differs from the new claim';
        END IF;
        IF predecessor_claim_key IS DISTINCT FROM NEW.claim_key THEN
            RAISE EXCEPTION
                'knowledge_claims replacement predecessor claim_key differs from the new claim';
        END IF;
        IF predecessor_retired_at IS NULL THEN
            RAISE EXCEPTION 'knowledge_claims replacement predecessor must be retired';
        END IF;

        SELECT id
          INTO latest_retired_id
          FROM public.knowledge_claims
         WHERE entity_ref_id = NEW.entity_ref_id
           AND claim_key = NEW.claim_key
           AND retired_at IS NOT NULL
         ORDER BY seq DESC
         LIMIT 1;
        IF latest_retired_id IS DISTINCT FROM NEW.replaces_id THEN
            RAISE EXCEPTION
                'knowledge_claims replacement predecessor must be the latest retired occurrence';
        END IF;
    ELSE
        PERFORM 1
          FROM public.knowledge_claims
         WHERE entity_ref_id = NEW.entity_ref_id
           AND claim_key = NEW.claim_key
           AND retired_at IS NOT NULL;
        IF FOUND THEN
            RAISE EXCEPTION
                'knowledge_claims replacement_required: name the latest retired predecessor';
        END IF;
    END IF;

    RETURN NEW;
END;
$function$
"""


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE knowledge_fact_definitions (
          fact_name TEXT NOT NULL,
          definition_version INTEGER NOT NULL,
          target VARCHAR(16) NOT NULL,
          ttl_seconds INTEGER NOT NULL,
          timeout_seconds INTEGER NOT NULL,
          policies JSONB NOT NULL,
          value_schema JSONB NOT NULL,
          digest VARCHAR(64) NOT NULL,
          registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (fact_name, definition_version),
          CONSTRAINT knowledge_fact_definitions_target_valid
            CHECK (target IN ('production', 'live_release', 'host')),
          CONSTRAINT knowledge_fact_definitions_policies_object
            CHECK (jsonb_typeof(policies) = 'object'),
          CONSTRAINT knowledge_fact_definitions_value_schema_object
            CHECK (jsonb_typeof(value_schema) = 'object'),
          CONSTRAINT knowledge_fact_definitions_digest_valid
            CHECK (digest ~ '^[0-9a-f]{64}$')
        )
        """
    )
    op.execute(
        """
        CREATE TABLE knowledge_claims (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          seq BIGINT GENERATED ALWAYS AS IDENTITY NOT NULL UNIQUE,
          entity_ref_id UUID NOT NULL REFERENCES brain_entities(id) ON DELETE RESTRICT,
          entity_type VARCHAR(16) NOT NULL,
          project_key VARCHAR(50) NOT NULL REFERENCES projects(project_key) ON DELETE RESTRICT,
          claim_key VARCHAR(64) NOT NULL,
          statement TEXT NOT NULL,
          fact_name TEXT NOT NULL,
          definition_version INTEGER NOT NULL,
          target VARCHAR(16) NOT NULL,
          expected JSONB NOT NULL,
          expected_resolved JSONB NOT NULL,
          validity_seconds INTEGER NOT NULL,
          provenance VARCHAR(16) NOT NULL,
          declared_by VARCHAR(64) NOT NULL,
          declared_at TIMESTAMPTZ NOT NULL,
          recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          retired_at TIMESTAMPTZ,
          replaces_id UUID REFERENCES knowledge_claims(id) ON DELETE RESTRICT,
          FOREIGN KEY (fact_name, definition_version)
            REFERENCES knowledge_fact_definitions(fact_name, definition_version) ON DELETE RESTRICT,
          CONSTRAINT knowledge_claims_entity_type_valid
            CHECK (entity_type IN ('learning', 'decision', 'adr', 'runbook', 'snippet')),
          CONSTRAINT knowledge_claims_claim_key_valid
            CHECK (claim_key ~ '^[0-9a-f]{64}$'),
          CONSTRAINT knowledge_claims_statement_length_valid
            CHECK (char_length(statement) between 1 and 500),
          CONSTRAINT knowledge_claims_target_valid
            CHECK (target IN ('production', 'live_release', 'host')),
          CONSTRAINT knowledge_claims_expected_object
            CHECK (jsonb_typeof(expected) = 'object'),
          CONSTRAINT knowledge_claims_expected_resolved_object
            CHECK (jsonb_typeof(expected_resolved) = 'object'),
          CONSTRAINT knowledge_claims_validity_seconds_valid
            CHECK (validity_seconds between 60 and 31536000),
          CONSTRAINT knowledge_claims_provenance_valid
            CHECK (provenance IN ('measured', 'declared'))
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_knowledge_claims_active_entity_key "
        "ON knowledge_claims (entity_ref_id, claim_key) WHERE retired_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_knowledge_claims_active_fact_name "
        "ON knowledge_claims (fact_name) WHERE retired_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_knowledge_claims_active_entity_ref "
        "ON knowledge_claims (entity_ref_id) WHERE retired_at IS NULL"
    )
    op.execute(
        """
        CREATE TABLE knowledge_claim_verdicts (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          seq BIGINT GENERATED ALWAYS AS IDENTITY NOT NULL UNIQUE,
          claim_id UUID NOT NULL REFERENCES knowledge_claims(id) ON DELETE RESTRICT,
          verdict VARCHAR(16) NOT NULL,
          reason TEXT,
          measurement JSONB NOT NULL,
          measurement_digest VARCHAR(64),
          observation_id UUID NOT NULL,
          issuer_identity VARCHAR(200) NOT NULL,
          issuer_kind VARCHAR(8) NOT NULL,
          request_fingerprint VARCHAR(64) NOT NULL,
          outcome_fingerprint VARCHAR(64) NOT NULL,
          idempotency_key VARCHAR(200) NOT NULL,
          emitted_at TIMESTAMPTZ NOT NULL,
          recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_knowledge_claim_verdicts_idempotency
            UNIQUE (claim_id, issuer_identity, idempotency_key),
          CONSTRAINT uq_knowledge_claim_verdicts_observation UNIQUE (claim_id, observation_id),
          CONSTRAINT knowledge_claim_verdicts_verdict_valid
            CHECK (verdict IN ('holds', 'falsified', 'unreadable')),
          CONSTRAINT knowledge_claim_verdicts_measurement_object
            CHECK (jsonb_typeof(measurement) = 'object'),
          CONSTRAINT knowledge_claim_verdicts_measurement_digest_valid
            CHECK (measurement_digest IS NULL OR measurement_digest ~ '^[0-9a-f]{64}$'),
          CONSTRAINT knowledge_claim_verdicts_issuer_kind_valid
            CHECK (issuer_kind IN ('robot', 'human')),
          CONSTRAINT knowledge_claim_verdicts_request_fingerprint_valid
            CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
          CONSTRAINT knowledge_claim_verdicts_outcome_fingerprint_valid
            CHECK (outcome_fingerprint ~ '^[0-9a-f]{64}$')
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_knowledge_claim_verdicts_claim_seq "
        "ON knowledge_claim_verdicts (claim_id, seq DESC)"
    )

    op.execute(_FACT_DEFINITIONS_APPEND_ONLY)
    op.execute(_VERDICTS_APPEND_ONLY)
    op.execute(_CLAIMS_UPDATE_GATE)
    op.execute(_CLAIMS_DELETE_GATE)
    op.execute(_CLAIMS_INSERT_GATE)
    op.execute(
        """
        CREATE TRIGGER knowledge_fact_definitions_append_only
            BEFORE UPDATE OR DELETE ON public.knowledge_fact_definitions
            FOR EACH ROW EXECUTE FUNCTION public.knowledge_fact_definitions_append_only()
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_claim_verdicts_append_only
            BEFORE UPDATE OR DELETE ON public.knowledge_claim_verdicts
            FOR EACH ROW EXECUTE FUNCTION public.knowledge_claim_verdicts_append_only()
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_claims_update_gate
            BEFORE UPDATE ON public.knowledge_claims
            FOR EACH ROW EXECUTE FUNCTION public.knowledge_claims_update_gate()
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_claims_delete_gate
            BEFORE DELETE ON public.knowledge_claims
            FOR EACH ROW EXECUTE FUNCTION public.knowledge_claims_delete_gate()
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_claims_insert_gate
            BEFORE INSERT ON public.knowledge_claims
            FOR EACH ROW EXECUTE FUNCTION public.knowledge_claims_insert_gate()
        """
    )

    # Measured on brain_test 2026-09-21 (learning d857f21c): brain is a
    # superuser, so privilege checks are bypassed.  A REVOKE DELETE is recorded
    # but never consulted and DELETE reaches the trigger which refuses it.  These
    # grants are defence in depth for de-superusering; triggers, not grants, carry
    # today's immutability guarantee.
    op.execute(
        "GRANT SELECT, INSERT ON knowledge_fact_definitions, knowledge_claim_verdicts TO brain"
    )
    op.execute("GRANT SELECT, INSERT ON knowledge_claims TO brain")
    op.execute("GRANT UPDATE (retired_at) ON knowledge_claims TO brain")

    op.execute(
        """
        CREATE VIEW knowledge_claim_current AS
        SELECT latest.claim_id,
               latest.seq AS latest_seq,
               latest.verdict AS latest_verdict,
               latest.reason AS latest_reason,
               latest.recorded_at AS latest_recorded_at,
               latest.observation_id AS latest_observation_id,
               conclusive.seq AS conclusive_seq,
               conclusive.verdict AS conclusive_verdict,
               conclusive.recorded_at AS conclusive_recorded_at,
               conclusive.observation_id AS conclusive_observation_id
          FROM (
              SELECT DISTINCT ON (claim_id)
                     claim_id, seq, verdict, reason, recorded_at, observation_id
                FROM knowledge_claim_verdicts
               ORDER BY claim_id, seq DESC
          ) AS latest
          LEFT JOIN (
              SELECT DISTINCT ON (claim_id)
                     claim_id, seq, verdict, recorded_at, observation_id
                FROM knowledge_claim_verdicts
               WHERE verdict IN ('holds', 'falsified')
               ORDER BY claim_id, seq DESC
          ) AS conclusive USING (claim_id)
        """
    )
    op.execute(
        "COMMENT ON VIEW knowledge_claim_current IS "
        "'Decision d8c016fc (2026-09-21): latest attempt and latest conclusive verdict; staleness is derived in Python.'"
    )


def downgrade() -> None:
    arguments = context.get_x_argument(as_dictionary=True)
    opted = arguments.get(_OPT_IN) == "yes"
    op.execute(
        f"""
        DO $$
        DECLARE
            claims bigint;
            verdicts bigint;
            definitions bigint;
        BEGIN
            SELECT count(*) INTO claims FROM knowledge_claims;
            SELECT count(*) INTO verdicts FROM knowledge_claim_verdicts;
            SELECT count(*) INTO definitions FROM knowledge_fact_definitions;

            IF {"FALSE" if opted else "TRUE"} AND (claims > 0 OR verdicts > 0 OR definitions > 0) THEN
                RAISE EXCEPTION
                    'cannot downgrade 055: knowledge_claims holds % row(s), '
                    'knowledge_claim_verdicts holds % row(s), knowledge_fact_definitions holds % row(s). '
                    'A verdict is a server-produced measurement taken at an instant that cannot be reconstructed, '
                    'and a claim occurrence chain cannot be rebuilt from anything else. Rerun with -x {_OPT_IN}=yes',
                    claims, verdicts, definitions;
            END IF;
        END;
        $$
        """
    )
    op.execute("DROP VIEW IF EXISTS knowledge_claim_current")
    op.execute("DROP TRIGGER IF EXISTS knowledge_claims_insert_gate ON public.knowledge_claims")
    op.execute("DROP TRIGGER IF EXISTS knowledge_claims_delete_gate ON public.knowledge_claims")
    op.execute("DROP TRIGGER IF EXISTS knowledge_claims_update_gate ON public.knowledge_claims")
    op.execute(
        "DROP TRIGGER IF EXISTS knowledge_claim_verdicts_append_only "
        "ON public.knowledge_claim_verdicts"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS knowledge_fact_definitions_append_only "
        "ON public.knowledge_fact_definitions"
    )
    op.execute("DROP FUNCTION IF EXISTS public.knowledge_claims_insert_gate()")
    op.execute("DROP FUNCTION IF EXISTS public.knowledge_claims_delete_gate()")
    op.execute("DROP FUNCTION IF EXISTS public.knowledge_claims_update_gate()")
    op.execute("DROP FUNCTION IF EXISTS public.knowledge_claim_verdicts_append_only()")
    op.execute("DROP FUNCTION IF EXISTS public.knowledge_fact_definitions_append_only()")
    op.execute("DROP TABLE IF EXISTS knowledge_claim_verdicts")
    op.execute("DROP TABLE IF EXISTS knowledge_claims")
    op.execute("DROP TABLE IF EXISTS knowledge_fact_definitions")
