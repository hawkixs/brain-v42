"""Contract tests for the immutable claim metadata tables."""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID, dialect

from brain_v42.db.claim_tables import CLAIM_TABLE_NAMES, register_claim_tables
from brain_v42.db.tables import METADATA


@pytest.mark.parametrize(
    ("table_name", "column_name", "nullable", "type_family"),
    [
        ("knowledge_fact_definitions", "fact_name", False, sa.Text),
        ("knowledge_fact_definitions", "definition_version", False, sa.Integer),
        ("knowledge_fact_definitions", "target", False, sa.String),
        ("knowledge_fact_definitions", "ttl_seconds", False, sa.Integer),
        ("knowledge_fact_definitions", "timeout_seconds", False, sa.Integer),
        ("knowledge_fact_definitions", "policies", False, JSONB),
        ("knowledge_fact_definitions", "value_schema", False, JSONB),
        ("knowledge_fact_definitions", "digest", False, sa.String),
        ("knowledge_fact_definitions", "registered_at", False, sa.DateTime),
        ("knowledge_claims", "id", False, UUID),
        ("knowledge_claims", "seq", False, sa.BigInteger),
        ("knowledge_claims", "entity_ref_id", False, UUID),
        ("knowledge_claims", "entity_type", False, sa.String),
        ("knowledge_claims", "project_key", False, sa.String),
        ("knowledge_claims", "claim_key", False, sa.String),
        ("knowledge_claims", "statement", False, sa.Text),
        ("knowledge_claims", "fact_name", False, sa.Text),
        ("knowledge_claims", "definition_version", False, sa.Integer),
        ("knowledge_claims", "target", False, sa.String),
        ("knowledge_claims", "expected", False, JSONB),
        ("knowledge_claims", "expected_resolved", False, JSONB),
        ("knowledge_claims", "validity_seconds", False, sa.Integer),
        ("knowledge_claims", "provenance", False, sa.String),
        ("knowledge_claims", "declared_by", False, sa.String),
        ("knowledge_claims", "declared_at", False, sa.DateTime),
        ("knowledge_claims", "recorded_at", False, sa.DateTime),
        ("knowledge_claims", "retired_at", True, sa.DateTime),
        ("knowledge_claims", "replaces_id", True, UUID),
        ("knowledge_claim_verdicts", "id", False, UUID),
        ("knowledge_claim_verdicts", "seq", False, sa.BigInteger),
        ("knowledge_claim_verdicts", "claim_id", False, UUID),
        ("knowledge_claim_verdicts", "verdict", False, sa.String),
        ("knowledge_claim_verdicts", "reason", True, sa.Text),
        ("knowledge_claim_verdicts", "measurement", False, JSONB),
        ("knowledge_claim_verdicts", "measurement_digest", True, sa.String),
        ("knowledge_claim_verdicts", "observation_id", False, UUID),
        ("knowledge_claim_verdicts", "issuer_identity", False, sa.String),
        ("knowledge_claim_verdicts", "issuer_kind", False, sa.String),
        ("knowledge_claim_verdicts", "request_fingerprint", False, sa.String),
        ("knowledge_claim_verdicts", "outcome_fingerprint", False, sa.String),
        ("knowledge_claim_verdicts", "idempotency_key", False, sa.String),
        ("knowledge_claim_verdicts", "emitted_at", False, sa.DateTime),
        ("knowledge_claim_verdicts", "recorded_at", False, sa.DateTime),
    ],
)
def test_claim_column_shapes_match_the_schema_contract(
    table_name: str,
    column_name: str,
    nullable: bool,
    type_family: type[sa.types.TypeEngine[Any]],
) -> None:
    """Each persisted claim field keeps its specified nullability and type family."""
    table = METADATA.tables[table_name]
    column = table.c[column_name]

    assert column.nullable is nullable
    assert isinstance(column.type, type_family)


def test_register_claim_tables_is_idempotent_on_fresh_metadata() -> None:
    """Registration returns one stable object for each table on repeat calls."""
    metadata = sa.MetaData()

    first = register_claim_tables(metadata)
    second = register_claim_tables(metadata)

    assert set(first) == set(CLAIM_TABLE_NAMES)
    assert set(second) == set(CLAIM_TABLE_NAMES)
    assert all(first[name] is second[name] for name in CLAIM_TABLE_NAMES)


def test_register_claim_tables_rejects_partial_registration() -> None:
    """A metadata with only part of the group cannot silently diverge."""
    metadata = sa.MetaData()
    sa.Table("knowledge_claims", metadata)

    with pytest.raises(RuntimeError, match="claim table registration is partial"):
        register_claim_tables(metadata)


def test_claim_tables_are_registered_on_shared_metadata() -> None:
    """The common metadata exposes all claim tables to migrations and consumers."""
    assert set(CLAIM_TABLE_NAMES) <= set(METADATA.tables)


@pytest.mark.parametrize("table_name", ("knowledge_claims", "knowledge_claim_verdicts"))
def test_ledger_sequences_are_always_generated_identities(table_name: str) -> None:
    """Ledger read order cannot be supplied by an inserting client."""
    identity = METADATA.tables[table_name].c.seq.identity

    assert isinstance(identity, sa.Identity)
    assert identity.always is True


def test_claim_foreign_keys_preserve_definition_and_replacement_links() -> None:
    """Claim rows reference their versioned definition and predecessor occurrence."""
    claims = METADATA.tables["knowledge_claims"]
    foreign_keys = tuple(claims.foreign_key_constraints)
    definition_fk = next(
        fk for fk in foreign_keys if set(fk.column_keys) == {"fact_name", "definition_version"}
    )
    replacement_fk = next(fk for fk in foreign_keys if fk.column_keys == ["replaces_id"])

    assert {element.target_fullname for element in definition_fk.elements} == {
        "knowledge_fact_definitions.fact_name",
        "knowledge_fact_definitions.definition_version",
    }
    assert replacement_fk.elements[0].target_fullname == "knowledge_claims.id"


def test_active_claim_indexes_are_partial_with_the_specified_uniqueness() -> None:
    """Only active claim lookups use the three partial indexes."""
    claims = METADATA.tables["knowledge_claims"]
    indexes = {index.name: index for index in claims.indexes}
    names = {
        "uq_knowledge_claims_active_entity_key",
        "ix_knowledge_claims_active_fact_name",
        "ix_knowledge_claims_active_entity_ref",
    }

    assert set(indexes) == names
    assert indexes["uq_knowledge_claims_active_entity_key"].unique is True
    assert indexes["ix_knowledge_claims_active_fact_name"].unique is False
    assert indexes["ix_knowledge_claims_active_entity_ref"].unique is False
    assert all(
        index.dialect_options["postgresql"]["where"] is not None for index in indexes.values()
    )


@pytest.mark.parametrize(
    ("table_name", "predicates"),
    [
        (
            "knowledge_fact_definitions",
            (
                "target IN ('production', 'live_release', 'host')",
                "jsonb_typeof(policies) = 'object'",
                "jsonb_typeof(value_schema) = 'object'",
                "digest ~ '^[0-9a-f]{64}$'",
            ),
        ),
        (
            "knowledge_claims",
            (
                "entity_type IN ('learning', 'decision', 'adr', 'runbook', 'snippet')",
                "claim_key ~ '^[0-9a-f]{64}$'",
                "char_length(statement) between 1 and 500",
                "validity_seconds between 60 and 31536000",
            ),
        ),
        (
            "knowledge_claim_verdicts",
            (
                "verdict IN ('holds', 'falsified', 'unreadable')",
                "jsonb_typeof(measurement) = 'object'",
                "measurement_digest IS NULL OR measurement_digest ~ '^[0-9a-f]{64}$'",
                "issuer_kind IN ('robot', 'human')",
            ),
        ),
    ],
)
def test_claim_table_ddl_contains_check_predicates(
    table_name: str, predicates: tuple[str, ...]
) -> None:
    """The PostgreSQL DDL preserves the value constraints rather than documenting them only."""
    ddl = str(sa.schema.CreateTable(METADATA.tables[table_name]).compile(dialect=dialect()))

    for predicate in predicates:
        assert predicate in ddl
