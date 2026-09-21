"""Additive PostgreSQL storage for immutable fact definitions and claim ledgers.

The two ``seq`` columns are ``GENERATED ALWAYS`` identities because verdict reads
use only server insertion order.  A ``bigserial`` is merely a default that an
inserting client can override; an always identity refuses a forged order, so
the ledger's ordering guarantee and its ordering column remain one object.
"""

from __future__ import annotations

from typing import Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

CLAIM_TABLE_NAMES: Final[tuple[str, ...]] = (
    "knowledge_fact_definitions",
    "knowledge_claims",
    "knowledge_claim_verdicts",
)


def register_claim_tables(metadata: sa.MetaData) -> dict[str, sa.Table]:
    """Register the three fact definition and claim tables exactly once."""
    existing = {
        name: metadata.tables[name] for name in CLAIM_TABLE_NAMES if name in metadata.tables
    }
    if existing:
        if set(existing) != set(CLAIM_TABLE_NAMES):
            raise RuntimeError("claim table registration is partial")
        return existing

    definitions = sa.Table(
        "knowledge_fact_definitions",
        metadata,
        sa.Column("fact_name", sa.Text, nullable=False),
        sa.Column("definition_version", sa.Integer, nullable=False),
        sa.Column("target", sa.String(16), nullable=False),
        sa.Column("ttl_seconds", sa.Integer, nullable=False),
        sa.Column("timeout_seconds", sa.Integer, nullable=False),
        sa.Column("policies", JSONB, nullable=False),
        sa.Column("value_schema", JSONB, nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("fact_name", "definition_version"),
        sa.CheckConstraint(
            "target IN ('production', 'live_release', 'host')",
            name="knowledge_fact_definitions_target_valid",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(policies) = 'object'",
            name="knowledge_fact_definitions_policies_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(value_schema) = 'object'",
            name="knowledge_fact_definitions_value_schema_object",
        ),
        sa.CheckConstraint(
            "digest ~ '^[0-9a-f]{64}$'",
            name="knowledge_fact_definitions_digest_valid",
        ),
    )
    claims = sa.Table(
        "knowledge_claims",
        metadata,
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("seq", sa.BigInteger, sa.Identity(always=True), nullable=False, unique=True),
        sa.Column(
            "entity_ref_id",
            UUID(as_uuid=True),
            sa.ForeignKey("brain_entities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(16), nullable=False),
        sa.Column(
            "project_key",
            sa.String(50),
            sa.ForeignKey("projects.project_key", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("claim_key", sa.String(64), nullable=False),
        sa.Column("statement", sa.Text, nullable=False),
        sa.Column("fact_name", sa.Text, nullable=False),
        sa.Column("definition_version", sa.Integer, nullable=False),
        sa.Column("target", sa.String(16), nullable=False),
        sa.Column("expected", JSONB, nullable=False),
        sa.Column("expected_resolved", JSONB, nullable=False),
        sa.Column("validity_seconds", sa.Integer, nullable=False),
        sa.Column("provenance", sa.String(16), nullable=False),
        sa.Column("declared_by", sa.String(64), nullable=False),
        sa.Column("declared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "replaces_id",
            UUID(as_uuid=True),
            sa.ForeignKey("knowledge_claims.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["fact_name", "definition_version"],
            [
                "knowledge_fact_definitions.fact_name",
                "knowledge_fact_definitions.definition_version",
            ],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "entity_type IN ('learning', 'decision', 'adr', 'runbook', 'snippet')",
            name="knowledge_claims_entity_type_valid",
        ),
        sa.CheckConstraint(
            "claim_key ~ '^[0-9a-f]{64}$'",
            name="knowledge_claims_claim_key_valid",
        ),
        sa.CheckConstraint(
            "char_length(statement) between 1 and 500",
            name="knowledge_claims_statement_length_valid",
        ),
        sa.CheckConstraint(
            "target IN ('production', 'live_release', 'host')",
            name="knowledge_claims_target_valid",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(expected) = 'object'",
            name="knowledge_claims_expected_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(expected_resolved) = 'object'",
            name="knowledge_claims_expected_resolved_object",
        ),
        sa.CheckConstraint(
            "validity_seconds between 60 and 31536000",
            name="knowledge_claims_validity_seconds_valid",
        ),
        sa.CheckConstraint(
            "provenance IN ('measured', 'declared')",
            name="knowledge_claims_provenance_valid",
        ),
        sa.Index(
            "uq_knowledge_claims_active_entity_key",
            "entity_ref_id",
            "claim_key",
            unique=True,
            postgresql_where=sa.text("retired_at IS NULL"),
        ),
        sa.Index(
            "ix_knowledge_claims_active_fact_name",
            "fact_name",
            postgresql_where=sa.text("retired_at IS NULL"),
        ),
        sa.Index(
            "ix_knowledge_claims_active_entity_ref",
            "entity_ref_id",
            postgresql_where=sa.text("retired_at IS NULL"),
        ),
    )
    verdicts = sa.Table(
        "knowledge_claim_verdicts",
        metadata,
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("seq", sa.BigInteger, sa.Identity(always=True), nullable=False, unique=True),
        sa.Column(
            "claim_id",
            UUID(as_uuid=True),
            sa.ForeignKey("knowledge_claims.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("measurement", JSONB, nullable=False),
        sa.Column("measurement_digest", sa.String(64), nullable=True),
        sa.Column("observation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("issuer_identity", sa.String(200), nullable=False),
        sa.Column("issuer_kind", sa.String(8), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("outcome_fingerprint", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("emitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "claim_id",
            "issuer_identity",
            "idempotency_key",
            name="uq_knowledge_claim_verdicts_idempotency",
        ),
        sa.UniqueConstraint(
            "claim_id",
            "observation_id",
            name="uq_knowledge_claim_verdicts_observation",
        ),
        sa.CheckConstraint(
            "verdict IN ('holds', 'falsified', 'unreadable')",
            name="knowledge_claim_verdicts_verdict_valid",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(measurement) = 'object'",
            name="knowledge_claim_verdicts_measurement_object",
        ),
        sa.CheckConstraint(
            "measurement_digest IS NULL OR measurement_digest ~ '^[0-9a-f]{64}$'",
            name="knowledge_claim_verdicts_measurement_digest_valid",
        ),
        sa.CheckConstraint(
            "issuer_kind IN ('robot', 'human')",
            name="knowledge_claim_verdicts_issuer_kind_valid",
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="knowledge_claim_verdicts_request_fingerprint_valid",
        ),
        sa.CheckConstraint(
            "outcome_fingerprint ~ '^[0-9a-f]{64}$'",
            name="knowledge_claim_verdicts_outcome_fingerprint_valid",
        ),
        sa.Index("ix_knowledge_claim_verdicts_claim_seq", "claim_id", sa.text("seq DESC")),
    )
    return {table.name: table for table in (definitions, claims, verdicts)}
