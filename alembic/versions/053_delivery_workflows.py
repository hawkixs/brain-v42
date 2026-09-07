"""Persist immutable delivery workflow contracts and evidence subjects.

Revision ID: 053
Revises: 052
"""

from __future__ import annotations

from alembic import context, op

revision = "053"
down_revision = "052"
branch_labels = None
depends_on = None

_TABLES = (
    "delivery_events",
    "delivery_receipts",
    "delivery_confirmations",
    "delivery_snapshots",
    "delivery_artifact_bindings",
    "delivery_dependencies",
    "delivery_contract_revisions",
    "delivery_workflows",
)


def upgrade() -> None:
    op.execute("""
    CREATE TABLE delivery_workflows (
      ticket_id UUID PRIMARY KEY REFERENCES tickets(id) ON DELETE RESTRICT,
      current_revision INTEGER NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
      row_version BIGINT NOT NULL DEFAULT 1, disposition VARCHAR(16) NOT NULL DEFAULT 'active',
      claim_owner VARCHAR(200), claim_kind VARCHAR(32), claim_digest VARCHAR(64),
      claim_expires_at TIMESTAMPTZ, claim_epoch BIGINT NOT NULL DEFAULT 0,
      context_set_digest VARCHAR(64) NOT NULL, context_due_at TIMESTAMPTZ,
      context_last_attempt_at TIMESTAMPTZ, context_last_attempt_outcome VARCHAR(16),
      context_last_error_code VARCHAR(100), context_last_success_at TIMESTAMPTZ,
      latest_context_success_confirmation_id UUID, latest_context_attempt_confirmation_id UUID,
      context_row_version BIGINT NOT NULL DEFAULT 1,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT delivery_workflows_versions_valid CHECK (current_revision >= 1 AND attempt >= 1 AND row_version >= 1),
      CONSTRAINT delivery_workflows_context_version_valid CHECK (context_row_version >= 1),
      CONSTRAINT delivery_workflows_disposition_valid CHECK (disposition IN ('active','fulfilled','cancelled','wontfix'))
    )""")
    op.execute("""
    CREATE TABLE delivery_contract_revisions (
      ticket_id UUID NOT NULL REFERENCES delivery_workflows(ticket_id) ON DELETE RESTRICT,
      contract_revision INTEGER NOT NULL, normalized_contract JSONB NOT NULL, content_digest VARCHAR(64) NOT NULL,
      author_project VARCHAR(50) NOT NULL, amendment_reason TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      PRIMARY KEY (ticket_id, contract_revision), CONSTRAINT uq_delivery_revision_digest UNIQUE (ticket_id, content_digest),
      CONSTRAINT delivery_contract_revisions_revision_valid CHECK (contract_revision >= 1)
    )""")
    op.execute("""
    CREATE TABLE delivery_dependencies (
      ticket_id UUID NOT NULL, contract_revision INTEGER NOT NULL,
      upstream_ticket_id UUID NOT NULL REFERENCES delivery_workflows(ticket_id) ON DELETE RESTRICT,
      upstream_revision INTEGER NOT NULL, upstream_attempt INTEGER NOT NULL, milestone VARCHAR(16) NOT NULL,
      CONSTRAINT pk_delivery_dependencies PRIMARY KEY (ticket_id,contract_revision,upstream_ticket_id,upstream_revision,upstream_attempt,milestone),
      FOREIGN KEY (ticket_id,contract_revision) REFERENCES delivery_contract_revisions(ticket_id,contract_revision) ON DELETE RESTRICT,
      CONSTRAINT delivery_dependencies_milestone_valid CHECK (milestone IN ('integrated','accepted'))
    )""")
    op.execute("""
    CREATE TABLE delivery_artifact_bindings (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(), ticket_id UUID NOT NULL, contract_revision INTEGER NOT NULL,
      attempt INTEGER NOT NULL, deliverable_key VARCHAR(64) NOT NULL, repository_id BIGINT NOT NULL,
      repository_name VARCHAR(201) NOT NULL, pr_number INTEGER NOT NULL, state VARCHAR(16) NOT NULL DEFAULT 'proposed',
      active BOOLEAN NOT NULL DEFAULT true, head_sha VARCHAR(64), base_sha VARCHAR(64), integration_sha VARCHAR(64),
      row_version BIGINT NOT NULL DEFAULT 1, due_at TIMESTAMPTZ, last_attempt_at TIMESTAMPTZ,
      last_success_at TIMESTAMPTZ, latest_success_confirmation_id UUID, latest_attempt_confirmation_id UUID,
      FOREIGN KEY (ticket_id,contract_revision) REFERENCES delivery_contract_revisions(ticket_id,contract_revision) ON DELETE RESTRICT,
      CONSTRAINT delivery_bindings_state_valid CHECK (state IN ('proposed','observed')),
      CONSTRAINT delivery_bindings_versions_valid CHECK (attempt >= 1 AND row_version >= 1 AND pr_number >= 1)
    )""")
    op.execute(
        "CREATE UNIQUE INDEX uq_delivery_active_binding ON delivery_artifact_bindings (ticket_id,contract_revision,attempt,deliverable_key) WHERE active"
    )
    op.execute("""
    CREATE TABLE delivery_snapshots (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(), subject_kind VARCHAR(24) NOT NULL,
      binding_id UUID REFERENCES delivery_artifact_bindings(id) ON DELETE RESTRICT,
      ticket_id UUID, contract_revision INTEGER, attempt INTEGER, context_set_digest VARCHAR(64),
      semantic_digest VARCHAR(64) NOT NULL, evidence JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT delivery_snapshots_subject_shape_valid CHECK (
        (subject_kind='artifact_binding' AND binding_id IS NOT NULL AND ticket_id IS NULL AND contract_revision IS NULL AND attempt IS NULL AND context_set_digest IS NULL)
        OR (subject_kind='repository_context' AND binding_id IS NULL AND ticket_id IS NOT NULL AND contract_revision IS NOT NULL AND attempt IS NOT NULL AND context_set_digest IS NOT NULL))
    )""")
    op.execute(
        "CREATE UNIQUE INDEX uq_delivery_snapshot_binding_digest ON delivery_snapshots (binding_id,semantic_digest) WHERE subject_kind='artifact_binding'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_delivery_snapshot_context_digest ON delivery_snapshots (ticket_id,contract_revision,attempt,context_set_digest,semantic_digest) WHERE subject_kind='repository_context'"
    )
    op.execute("""
    CREATE TABLE delivery_confirmations (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(), subject_kind VARCHAR(24) NOT NULL,
      binding_id UUID REFERENCES delivery_artifact_bindings(id) ON DELETE RESTRICT,
      ticket_id UUID, contract_revision INTEGER, attempt INTEGER, context_set_digest VARCHAR(64),
      snapshot_id UUID REFERENCES delivery_snapshots(id) ON DELETE RESTRICT,
      collection_started_at TIMESTAMPTZ NOT NULL, collection_finished_at TIMESTAMPTZ NOT NULL,
      outcome VARCHAR(16) NOT NULL, error_code VARCHAR(100),
      CONSTRAINT delivery_confirmations_outcome_valid CHECK (outcome IN ('success','error')),
      CONSTRAINT delivery_confirmations_time_valid CHECK (collection_finished_at >= collection_started_at),
      CONSTRAINT delivery_confirmations_outcome_shape_valid CHECK ((outcome='success' AND snapshot_id IS NOT NULL AND error_code IS NULL) OR (outcome='error' AND snapshot_id IS NULL AND error_code IS NOT NULL)),
      CONSTRAINT delivery_confirmations_subject_shape_valid CHECK (
        (subject_kind='artifact_binding' AND binding_id IS NOT NULL AND ticket_id IS NULL AND contract_revision IS NULL AND attempt IS NULL AND context_set_digest IS NULL)
        OR (subject_kind='repository_context' AND binding_id IS NULL AND ticket_id IS NOT NULL AND contract_revision IS NOT NULL AND attempt IS NOT NULL AND context_set_digest IS NOT NULL))
    )""")
    for table, columns in (
        (
            "delivery_workflows",
            "latest_context_success_confirmation_id, latest_context_attempt_confirmation_id",
        ),
        (
            "delivery_artifact_bindings",
            "latest_success_confirmation_id, latest_attempt_confirmation_id",
        ),
    ):
        for column in columns.split(", "):
            op.execute(
                f"ALTER TABLE {table} ADD FOREIGN KEY ({column}) REFERENCES delivery_confirmations(id) ON DELETE RESTRICT"
            )
    op.execute("""
    CREATE TABLE delivery_receipts (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(), ticket_id UUID NOT NULL REFERENCES delivery_workflows(ticket_id) ON DELETE RESTRICT,
      contract_revision INTEGER NOT NULL, attempt INTEGER NOT NULL, milestone VARCHAR(16) NOT NULL,
      delivery_digest VARCHAR(64) NOT NULL, payload JSONB NOT NULL, issuer VARCHAR(200) NOT NULL,
      basis VARCHAR(16), issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT uq_delivery_receipt_milestone UNIQUE (ticket_id,contract_revision,attempt,milestone),
      CONSTRAINT delivery_receipts_milestone_valid CHECK (milestone IN ('integration','fulfilled'))
    )""")
    op.execute("""
    CREATE TABLE delivery_events (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(), operation VARCHAR(32) NOT NULL, actor_project VARCHAR(50) NOT NULL,
      ticket_id UUID NOT NULL REFERENCES delivery_workflows(ticket_id) ON DELETE RESTRICT,
      idempotency_key VARCHAR(200) NOT NULL, request_digest VARCHAR(64) NOT NULL,
      result JSONB NOT NULL, payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT uq_delivery_event_idempotency UNIQUE (operation,actor_project,idempotency_key)
    )""")


def downgrade() -> None:
    if (
        context.get_x_argument(as_dictionary=True).get("allow_delivery_workflows_downgrade")
        != "yes"
    ):
        op.execute("""DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM delivery_workflows) THEN
            RAISE EXCEPTION 'cannot downgrade 053: delivery workflow history exists; export it or rerun with -x allow_delivery_workflows_downgrade=yes';
          END IF;
        END $$""")
    for table in _TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")
