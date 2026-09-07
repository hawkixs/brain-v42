"""Additive PostgreSQL storage for immutable delivery workflow facts.

This module accepts the shared metadata to avoid importing ``db.tables`` back
into the registration path.  Evidence subjects intentionally support both a
binding and repository context before the first pull request exists.
"""

from __future__ import annotations

from typing import Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

DELIVERY_TABLE_NAMES: Final[tuple[str, ...]] = (
    "delivery_workflows",
    "delivery_contract_revisions",
    "delivery_dependencies",
    "delivery_artifact_bindings",
    "delivery_snapshots",
    "delivery_confirmations",
    "delivery_receipts",
    "delivery_events",
)


def register_delivery_tables(metadata: sa.MetaData) -> dict[str, sa.Table]:
    """Register the eight workflow tables on the supplied metadata exactly once."""
    existing = {
        name: metadata.tables[name] for name in DELIVERY_TABLE_NAMES if name in metadata.tables
    }
    if existing:
        if set(existing) != set(DELIVERY_TABLE_NAMES):
            raise RuntimeError("delivery table registration is partial")
        return existing

    workflows = sa.Table(
        "delivery_workflows",
        metadata,
        sa.Column(
            "ticket_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("current_revision", sa.Integer, nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("row_version", sa.BigInteger, nullable=False, server_default=sa.text("1")),
        sa.Column("disposition", sa.String(16), nullable=False, server_default=sa.text("'active'")),
        sa.Column("claim_owner", sa.String(200)),
        sa.Column("claim_kind", sa.String(32)),
        sa.Column("claim_digest", sa.String(64)),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True)),
        sa.Column("claim_epoch", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("context_set_digest", sa.String(64), nullable=False),
        sa.Column("context_due_at", sa.DateTime(timezone=True)),
        sa.Column("context_last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("context_last_attempt_outcome", sa.String(16)),
        sa.Column("context_last_error_code", sa.String(100)),
        sa.Column("context_last_success_at", sa.DateTime(timezone=True)),
        sa.Column("latest_context_success_confirmation_id", UUID(as_uuid=True)),
        sa.Column("latest_context_attempt_confirmation_id", UUID(as_uuid=True)),
        sa.Column(
            "context_row_version", sa.BigInteger, nullable=False, server_default=sa.text("1")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "current_revision >= 1 AND attempt >= 1 AND row_version >= 1",
            name="delivery_workflows_versions_valid",
        ),
        sa.CheckConstraint(
            "context_row_version >= 1", name="delivery_workflows_context_version_valid"
        ),
        sa.CheckConstraint(
            "disposition IN ('active', 'fulfilled', 'cancelled', 'wontfix')",
            name="delivery_workflows_disposition_valid",
        ),
    )
    revisions = sa.Table(
        "delivery_contract_revisions",
        metadata,
        sa.Column(
            "ticket_id",
            UUID(as_uuid=True),
            sa.ForeignKey("delivery_workflows.ticket_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("contract_revision", sa.Integer, primary_key=True),
        sa.Column("normalized_contract", JSONB, nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("context_set_digest", sa.String(64), nullable=False),
        sa.Column("author_project", sa.String(50), nullable=False),
        sa.Column("amendment_reason", sa.Text),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "ticket_id",
            "contract_revision",
            "context_set_digest",
            name="uq_delivery_revision_context_subject",
        ),
        sa.CheckConstraint(
            "contract_revision >= 1", name="delivery_contract_revisions_revision_valid"
        ),
    )
    dependencies = sa.Table(
        "delivery_dependencies",
        metadata,
        sa.Column("ticket_id", UUID(as_uuid=True), nullable=False),
        sa.Column("contract_revision", sa.Integer, nullable=False),
        sa.Column(
            "upstream_ticket_id",
            UUID(as_uuid=True),
            sa.ForeignKey("delivery_workflows.ticket_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("upstream_revision", sa.Integer, nullable=False),
        sa.Column("upstream_attempt", sa.Integer, nullable=False),
        sa.Column("milestone", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(
            ["ticket_id", "contract_revision"],
            [
                "delivery_contract_revisions.ticket_id",
                "delivery_contract_revisions.contract_revision",
            ],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "ticket_id",
            "contract_revision",
            "upstream_ticket_id",
            "upstream_revision",
            "upstream_attempt",
            "milestone",
            name="pk_delivery_dependencies",
        ),
        sa.CheckConstraint(
            "milestone IN ('integrated', 'accepted')", name="delivery_dependencies_milestone_valid"
        ),
    )
    bindings = sa.Table(
        "delivery_artifact_bindings",
        metadata,
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("ticket_id", UUID(as_uuid=True), nullable=False),
        sa.Column("contract_revision", sa.Integer, nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("deliverable_key", sa.String(64), nullable=False),
        sa.Column("repository_id", sa.BigInteger, nullable=False),
        sa.Column("repository_name", sa.String(201), nullable=False),
        sa.Column("pr_number", sa.Integer, nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default=sa.text("'proposed'")),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("head_sha", sa.String(64)),
        sa.Column("base_sha", sa.String(64)),
        sa.Column("integration_sha", sa.String(64)),
        sa.Column("row_version", sa.BigInteger, nullable=False, server_default=sa.text("1")),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("latest_success_confirmation_id", UUID(as_uuid=True)),
        sa.Column("latest_attempt_confirmation_id", UUID(as_uuid=True)),
        sa.ForeignKeyConstraint(
            ["ticket_id", "contract_revision"],
            [
                "delivery_contract_revisions.ticket_id",
                "delivery_contract_revisions.contract_revision",
            ],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('proposed', 'observed')", name="delivery_bindings_state_valid"
        ),
        sa.CheckConstraint(
            "attempt >= 1 AND row_version >= 1 AND pr_number >= 1",
            name="delivery_bindings_versions_valid",
        ),
        sa.Index(
            "uq_delivery_active_binding",
            "ticket_id",
            "contract_revision",
            "attempt",
            "deliverable_key",
            unique=True,
            postgresql_where=sa.text("active"),
        ),
    )
    snapshots = sa.Table(
        "delivery_snapshots",
        metadata,
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("subject_kind", sa.String(24), nullable=False),
        sa.Column(
            "binding_id",
            UUID(as_uuid=True),
            sa.ForeignKey("delivery_artifact_bindings.id", ondelete="RESTRICT"),
        ),
        sa.Column("ticket_id", UUID(as_uuid=True)),
        sa.Column("contract_revision", sa.Integer),
        sa.Column("attempt", sa.Integer),
        sa.Column("context_set_digest", sa.String(64)),
        sa.Column("semantic_digest", sa.String(64), nullable=False),
        sa.Column("evidence", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "(subject_kind = 'artifact_binding' AND binding_id IS NOT NULL AND ticket_id IS NULL AND contract_revision IS NULL AND attempt IS NULL AND context_set_digest IS NULL) OR (subject_kind = 'repository_context' AND binding_id IS NULL AND ticket_id IS NOT NULL AND contract_revision IS NOT NULL AND attempt IS NOT NULL AND context_set_digest IS NOT NULL)",
            name="delivery_snapshots_subject_shape_valid",
        ),
        sa.CheckConstraint(
            "subject_kind <> 'repository_context' OR attempt >= 1",
            name="delivery_snapshots_context_attempt_valid",
        ),
        sa.Index(
            "uq_delivery_snapshot_binding_digest",
            "binding_id",
            "semantic_digest",
            unique=True,
            postgresql_where=sa.text("subject_kind = 'artifact_binding'"),
        ),
        sa.UniqueConstraint("id", "binding_id", name="uq_delivery_snapshot_binding_subject"),
        sa.UniqueConstraint(
            "id",
            "ticket_id",
            "contract_revision",
            "attempt",
            "context_set_digest",
            name="uq_delivery_snapshot_context_subject",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id", "contract_revision", "context_set_digest"],
            [
                "delivery_contract_revisions.ticket_id",
                "delivery_contract_revisions.contract_revision",
                "delivery_contract_revisions.context_set_digest",
            ],
            ondelete="RESTRICT",
        ),
        sa.Index(
            "uq_delivery_snapshot_context_digest",
            "ticket_id",
            "contract_revision",
            "attempt",
            "context_set_digest",
            "semantic_digest",
            unique=True,
            postgresql_where=sa.text("subject_kind = 'repository_context'"),
        ),
    )
    confirmations = sa.Table(
        "delivery_confirmations",
        metadata,
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("subject_kind", sa.String(24), nullable=False),
        sa.Column(
            "binding_id",
            UUID(as_uuid=True),
            sa.ForeignKey("delivery_artifact_bindings.id", ondelete="RESTRICT"),
        ),
        sa.Column("ticket_id", UUID(as_uuid=True)),
        sa.Column("contract_revision", sa.Integer),
        sa.Column("attempt", sa.Integer),
        sa.Column("context_set_digest", sa.String(64)),
        sa.Column("snapshot_id", UUID(as_uuid=True)),
        sa.Column(
            "success_id",
            UUID(as_uuid=True),
            sa.Computed("CASE WHEN outcome = 'success' THEN id END", persisted=True),
        ),
        sa.Column("collection_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collection_finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.CheckConstraint(
            "outcome IN ('success', 'error')", name="delivery_confirmations_outcome_valid"
        ),
        sa.CheckConstraint(
            "collection_finished_at >= collection_started_at",
            name="delivery_confirmations_time_valid",
        ),
        sa.CheckConstraint(
            "(outcome = 'success' AND snapshot_id IS NOT NULL AND error_code IS NULL) OR (outcome = 'error' AND snapshot_id IS NULL AND error_code IS NOT NULL)",
            name="delivery_confirmations_outcome_shape_valid",
        ),
        sa.CheckConstraint(
            "(subject_kind = 'artifact_binding' AND binding_id IS NOT NULL AND ticket_id IS NULL AND contract_revision IS NULL AND attempt IS NULL AND context_set_digest IS NULL) OR (subject_kind = 'repository_context' AND binding_id IS NULL AND ticket_id IS NOT NULL AND contract_revision IS NOT NULL AND attempt IS NOT NULL AND context_set_digest IS NOT NULL)",
            name="delivery_confirmations_subject_shape_valid",
        ),
        sa.CheckConstraint(
            "subject_kind <> 'repository_context' OR attempt >= 1",
            name="delivery_confirmations_context_attempt_valid",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "binding_id"],
            ["delivery_snapshots.id", "delivery_snapshots.binding_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "ticket_id", "contract_revision", "attempt", "context_set_digest"],
            [
                "delivery_snapshots.id",
                "delivery_snapshots.ticket_id",
                "delivery_snapshots.contract_revision",
                "delivery_snapshots.attempt",
                "delivery_snapshots.context_set_digest",
            ],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "binding_id", name="uq_delivery_confirmation_binding_attempt"),
        sa.UniqueConstraint(
            "success_id", "binding_id", name="uq_delivery_confirmation_binding_success"
        ),
        sa.UniqueConstraint(
            "id",
            "ticket_id",
            "contract_revision",
            "attempt",
            "context_set_digest",
            name="uq_delivery_confirmation_context_attempt",
        ),
        sa.UniqueConstraint(
            "success_id",
            "ticket_id",
            "contract_revision",
            "attempt",
            "context_set_digest",
            name="uq_delivery_confirmation_context_success",
        ),
    )
    workflows.append_constraint(
        sa.ForeignKeyConstraint(
            ["ticket_id", "current_revision", "context_set_digest"],
            [
                "delivery_contract_revisions.ticket_id",
                "delivery_contract_revisions.contract_revision",
                "delivery_contract_revisions.context_set_digest",
            ],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
            name="fk_delivery_workflow_current_context_revision",
        )
    )
    bindings.append_constraint(
        sa.ForeignKeyConstraint(
            ["latest_attempt_confirmation_id", "id"],
            ["delivery_confirmations.id", "delivery_confirmations.binding_id"],
            ondelete="RESTRICT",
        )
    )
    bindings.append_constraint(
        sa.ForeignKeyConstraint(
            ["latest_success_confirmation_id", "id"],
            ["delivery_confirmations.success_id", "delivery_confirmations.binding_id"],
            ondelete="RESTRICT",
        )
    )
    for pointer, confirmation in (
        ("latest_context_attempt_confirmation_id", "id"),
        ("latest_context_success_confirmation_id", "success_id"),
    ):
        workflows.append_constraint(
            sa.ForeignKeyConstraint(
                [pointer, "ticket_id", "current_revision", "attempt", "context_set_digest"],
                [
                    f"delivery_confirmations.{confirmation}",
                    "delivery_confirmations.ticket_id",
                    "delivery_confirmations.contract_revision",
                    "delivery_confirmations.attempt",
                    "delivery_confirmations.context_set_digest",
                ],
                ondelete="RESTRICT",
            )
        )
    receipts = sa.Table(
        "delivery_receipts",
        metadata,
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "ticket_id",
            UUID(as_uuid=True),
            sa.ForeignKey("delivery_workflows.ticket_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("contract_revision", sa.Integer, nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("milestone", sa.String(16), nullable=False),
        sa.Column("delivery_digest", sa.String(64), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("issuer", sa.String(200), nullable=False),
        sa.Column("basis", sa.String(16)),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.UniqueConstraint(
            "ticket_id",
            "contract_revision",
            "attempt",
            "milestone",
            "delivery_digest",
            name="uq_delivery_receipt_milestone",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id", "contract_revision"],
            [
                "delivery_contract_revisions.ticket_id",
                "delivery_contract_revisions.contract_revision",
            ],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "milestone IN ('integration', 'fulfilled')", name="delivery_receipts_milestone_valid"
        ),
    )
    events = sa.Table(
        "delivery_events",
        metadata,
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("actor_project", sa.String(50), nullable=False),
        sa.Column(
            "ticket_id",
            UUID(as_uuid=True),
            sa.ForeignKey("delivery_workflows.ticket_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("result", JSONB, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "operation", "actor_project", "idempotency_key", name="uq_delivery_event_idempotency"
        ),
    )
    return {
        table.name: table
        for table in (
            workflows,
            revisions,
            dependencies,
            bindings,
            snapshots,
            confirmations,
            receipts,
            events,
        )
    }
