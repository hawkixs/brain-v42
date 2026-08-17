"""Static contract tests for the Brain PostgreSQL recovery attestation."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pgvector.sqlalchemy import Vector

from brain_v42.db.tables import METADATA

PROJECT_ROOT = Path(__file__).parents[2]
CONTRACT_PATH = PROJECT_ROOT / "ops" / "recovery" / "brain-v42-v1.json"
ATTESTATION_SQL_PATH = PROJECT_ROOT / "ops" / "recovery" / "brain-v42-v1.sql"
IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
EXPECTED_JSON_SHA256 = "bbbceceeadfa78af0659a428ba040fc04e7eeee9ae351371f96f92aa63402fcd"
EXPECTED_SQL_SHA256 = "54534703c5e9883c7f7e85ff1563609b15c37bf5f1a03378bd5ada2125c3a508"

EXPECTED_TABLES = [
    "access_log",
    "adrs",
    "alembic_version",
    "consolidation_log",
    "decisions",
    "dream_promotions",
    "dream_runs",
    "feature_artifacts",
    "features",
    "gitlab_events",
    "indexed_plan_chunks",
    "indexed_plans",
    "learnings",
    "metrics_timeseries",
    "process_metrics",
    "project_contexts",
    "roadmap_curation_proposals",
    "runbooks",
    "search_log",
    "snippets",
    "ticket_extraction_proposals",
    "ticket_messages",
    "tickets",
]

VECTOR_TABLES = [
    "adrs",
    "decisions",
    "features",
    "gitlab_events",
    "indexed_plan_chunks",
    "indexed_plans",
    "learnings",
    "runbooks",
    "snippets",
]

EXPECTED_DOCUMENT: dict[str, Any] = {
    "checks": [
        {"id": "alembic_head", "kind": "alembic_head_equals", "revision": "031"},
        {
            "foreign_keys": 17,
            "id": "catalog_counts",
            "indexes": 101,
            "invalid_indexes": 0,
            "kind": "catalog_counts_equals",
            "schema": "public",
            "unvalidated_constraints": 0,
        },
        {
            "id": "corpus_nonempty",
            "kind": "row_count_sum_min",
            "minimum": 1,
            "tables": ["adrs", "decisions", "learnings", "runbooks", "snippets"],
        },
        *[
            {
                "column": "embedding",
                "dimensions": 1536,
                "id": f"embedding_{table}",
                "kind": "vector_column",
                "table": table,
            }
            for table in VECTOR_TABLES
        ],
        {
            "id": "extension_vector",
            "kind": "extension_version_equals",
            "name": "vector",
            "version": "0.8.2",
        },
        {
            "id": "features_nonempty",
            "kind": "row_count_sum_min",
            "minimum": 1,
            "tables": ["features"],
        },
        {
            "id": "indexed_plan_chunks_nonempty",
            "kind": "row_count_sum_min",
            "minimum": 1,
            "tables": ["indexed_plan_chunks"],
        },
        {
            "id": "indexed_plans_nonempty",
            "kind": "row_count_sum_min",
            "minimum": 1,
            "tables": ["indexed_plans"],
        },
        {
            "child_column": "feature_id",
            "child_table": "feature_artifacts",
            "id": "orphan_feature_artifacts_features",
            "kind": "orphan_count_zero",
            "parent_column": "id",
            "parent_table": "features",
        },
        {
            "child_column": "plan_id",
            "child_table": "indexed_plan_chunks",
            "id": "orphan_indexed_plan_chunks_plans",
            "kind": "orphan_count_zero",
            "parent_column": "id",
            "parent_table": "indexed_plans",
        },
        {
            "id": "project_contexts_nonempty",
            "kind": "row_count_sum_min",
            "minimum": 1,
            "tables": ["project_contexts"],
        },
        {
            "id": "table_set",
            "kind": "table_set_equals",
            "schema": "public",
            "tables": EXPECTED_TABLES,
        },
    ],
    "contract_id": "brain-v42/postgresql-recovery/v1",
    "engine": "postgresql",
    "schema_version": 1,
}

EXPECTED_CHECK_IDS = [check["id"] for check in EXPECTED_DOCUMENT["checks"]]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _contract_bytes() -> bytes:
    if not CONTRACT_PATH.is_file():
        pytest.skip("asset presence is covered separately")
    return CONTRACT_PATH.read_bytes()


def _contract_document() -> dict[str, Any]:
    document = json.loads(_contract_bytes(), object_pairs_hook=_reject_duplicate_keys)
    assert isinstance(document, dict)
    return document


def _attestation_sql() -> str:
    if not ATTESTATION_SQL_PATH.is_file():
        pytest.skip("asset presence is covered separately")
    return ATTESTATION_SQL_PATH.read_text(encoding="utf-8")


def _assert_static_read_only_sql(sql: str) -> None:
    prohibited_tokens = re.compile(
        r"\b(alter|analyze|call|cluster|copy|create|delete|do|drop|execute|grant|insert|"
        r"into|lock|merge|nextval|pg_logical_emit_message|pg_read_file|pg_write_file|refresh|"
        r"reindex|revoke|set_config|setval|truncate|update|vacuum)\b",
        re.IGNORECASE,
    )
    public_function_calls = set(
        re.findall(r"\bpublic\.([a-z][a-z0-9_]*)\s*\(", sql, flags=re.IGNORECASE)
    )

    assert sql.lstrip().startswith("WITH ")
    assert sql.count(";") == 1
    assert sql.rstrip().endswith(";")
    assert prohibited_tokens.search(sql) is None
    assert public_function_calls <= {"vector_dims"}
    assert re.search(r"(?m)^\s*\\", sql) is None


def test_recovery_contract_assets_exist() -> None:
    missing = [
        str(path.relative_to(PROJECT_ROOT))
        for path in (CONTRACT_PATH, ATTESTATION_SQL_PATH)
        if not path.is_file()
    ]
    assert missing == [], f"missing recovery assets: {missing}"


def test_contract_is_exact_canonical_json() -> None:
    raw = _contract_bytes()
    document = _contract_document()
    canonical = (
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode()

    assert raw == canonical
    assert document == EXPECTED_DOCUMENT


def test_contract_uses_a_closed_bounded_dsl() -> None:
    document = _contract_document()
    checks = document["checks"]
    allowed_kinds = {
        "alembic_head_equals",
        "catalog_counts_equals",
        "extension_version_equals",
        "orphan_count_zero",
        "row_count_sum_min",
        "table_set_equals",
        "vector_column",
    }

    assert len(checks) == 20
    assert EXPECTED_CHECK_IDS == sorted(EXPECTED_CHECK_IDS)
    assert len(EXPECTED_CHECK_IDS) == len(set(EXPECTED_CHECK_IDS))
    assert all(IDENTIFIER_RE.fullmatch(check["id"]) for check in checks)
    assert {check["kind"] for check in checks} <= allowed_kinds
    assert not any("sql" in key.lower() for check in checks for key in check)

    for check in checks:
        for key in (
            "table",
            "column",
            "child_table",
            "child_column",
            "parent_table",
            "parent_column",
            "name",
        ):
            value = check.get(key)
            if value is not None:
                assert IDENTIFIER_RE.fullmatch(value), (check["id"], key, value)
        tables = check.get("tables", [])
        assert tables == sorted(tables)
        assert len(tables) == len(set(tables))
        assert all(IDENTIFIER_RE.fullmatch(table) for table in tables)


def test_historic_contract_remains_pinned_to_revision_031() -> None:
    document = _contract_document()
    checks = {check["id"]: check for check in document["checks"]}
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)

    assert checks["alembic_head"]["revision"] == "031"
    # Marqueur « ce contrat a été relu à cette tête ». La 045 n'ajoute aucune
    # table — 32 tables `public` mesurées sur `brain` juste après l'application —
    # et n'élargit qu'une colonne existante : un dump/restore la transporte à sa
    # largeur déclarée, sans rien à reprendre côté contrat. Elle recrée
    # `codex_dream_run_v1` à l'identique, en relisant la définition de la 036,
    # et repose son GRANT ; la FORME décrite par le contrat est donc intacte.
    # Il reste épinglé à 031 parce que c'est la révision dont il décrit la
    # forme, pas la tête courante.
    assert script.get_heads() == ["045"]
    post_contract_tables = {
        "brain_session_artifacts",
        "brain_sessions",
        "projects",
        "project_aliases",
        "brain_entities",
        "entity_relations",
        "graph_outbox",
        "graph_projection_leases",
        "ticket_extraction_attempts",
    }
    assert post_contract_tables.isdisjoint(checks["table_set"]["tables"])
    assert checks["table_set"]["tables"] == sorted(
        [name for name in METADATA.tables if name not in post_contract_tables] + ["alembic_version"]
    )


def test_contract_tracks_every_vector_column_and_typmod() -> None:
    document = _contract_document()
    vector_checks = {
        check["table"]: check for check in document["checks"] if check["kind"] == "vector_column"
    }
    metadata_vector_tables = sorted(
        table.name
        for table in METADATA.tables.values()
        if "embedding" in table.c and isinstance(table.c.embedding.type, Vector)
    )

    assert sorted(vector_checks) == metadata_vector_tables == VECTOR_TABLES
    for table_name, check in vector_checks.items():
        column = METADATA.tables[table_name].c[check["column"]]
        assert isinstance(column.type, Vector)
        assert column.type.dim == check["dimensions"] == 1536


def test_contract_pins_the_audited_catalog_and_business_invariants() -> None:
    checks = {check["id"]: check for check in _contract_document()["checks"]}

    assert checks["catalog_counts"] == {
        "foreign_keys": 17,
        "id": "catalog_counts",
        "indexes": 101,
        "invalid_indexes": 0,
        "kind": "catalog_counts_equals",
        "schema": "public",
        "unvalidated_constraints": 0,
    }
    assert checks["extension_vector"]["version"] == "0.8.2"
    assert checks["corpus_nonempty"]["tables"] == [
        "adrs",
        "decisions",
        "learnings",
        "runbooks",
        "snippets",
    ]
    assert checks["orphan_feature_artifacts_features"]["child_table"] == "feature_artifacts"
    assert checks["orphan_indexed_plan_chunks_plans"]["child_table"] == "indexed_plan_chunks"


def test_attestation_sql_is_fixed_independent_and_read_only() -> None:
    sql = _attestation_sql()

    assert sql.endswith("\n")
    _assert_static_read_only_sql(sql)
    assert "brain-v42-v1.json" not in sql
    assert "recovery_contract" not in sql.lower()
    assert "pg_read_file" not in sql.lower()
    assert set(re.findall(r"\bpublic\.([a-z][a-z0-9_]*)\s*\(", sql, flags=re.IGNORECASE)) == {
        "vector_dims"
    }


@pytest.mark.parametrize(
    "unsafe_sql",
    [
        "WITH source AS (SELECT 1) SELECT * INTO TEMP TABLE leaked FROM source;",
        "WITH source AS (SELECT 1) SELECT nextval('owned_sequence') FROM source;",
        "WITH source AS (SELECT 1) SELECT public.side_effect() FROM source;",
    ],
)
def test_static_read_only_gate_rejects_indirect_writes(unsafe_sql: str) -> None:
    with pytest.raises(AssertionError):
        _assert_static_read_only_sql(unsafe_sql)


def test_attestation_sql_covers_the_same_twenty_checks() -> None:
    sql = _attestation_sql()

    for check_id in EXPECTED_CHECK_IDS:
        assert len(re.findall(rf"'{re.escape(check_id)}'", sql)) == 1, check_id
    assert "'brain-v42/postgresql-recovery/v1'" in sql
    assert "'031'" in sql
    assert "'0.8.2'" in sql
    assert "'vector(1536)'" in sql
    assert "jsonb_agg" in sql.lower()
    for key in ("checks", "contract_id", "expected", "id", "observed", "schema_version", "status"):
        assert f"'{key}'" in sql


def test_recovery_assets_match_the_frozen_sha256_digests() -> None:
    json_digest = hashlib.sha256(_contract_bytes()).hexdigest()
    sql_digest = hashlib.sha256(_attestation_sql().encode()).hexdigest()

    assert json_digest == EXPECTED_JSON_SHA256
    assert sql_digest == EXPECTED_SQL_SHA256


def test_verified_v2_recovery_contract_is_published() -> None:
    assert (PROJECT_ROOT / "ops" / "recovery" / "brain-v42-v2.json").is_file()
    assert (PROJECT_ROOT / "ops" / "recovery" / "brain-v42-v2.sql").is_file()
