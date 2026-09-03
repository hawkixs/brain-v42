"""Static contract tests for the head-035 PostgreSQL recovery authority."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast

from brain_v42.db.tables import METADATA

PROJECT_ROOT = Path(__file__).parents[2]
RECOVERY_DIR = PROJECT_ROOT / "ops" / "recovery"
V1_CONTRACT_PATH = RECOVERY_DIR / "brain-v42-v1.json"
CONTRACT_PATH = RECOVERY_DIR / "brain-v42-v2.json"
ATTESTATION_SQL_PATH = RECOVERY_DIR / "brain-v42-v2.sql"
EXPECTED_JSON_SHA256 = "715800f584a9d9e6bd4f8af703ba222223b119cb79a629e1f89c4e5b1929a188"
EXPECTED_SQL_SHA256 = "8e7a77ce4701e449bd4a9a7e6fc2c22fedd003bb33f0c64d836b1515316c4953"


def _v1_checks() -> list[dict[str, Any]]:
    document = json.loads(V1_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return cast(list[dict[str, Any]], document["checks"])


def _expected_checks() -> list[dict[str, Any]]:
    checks = json.loads(json.dumps(_v1_checks()))
    by_id = {check["id"]: check for check in checks}
    by_id["alembic_head"]["revision"] = "035"
    by_id["catalog_counts"].update(foreign_keys=24, indexes=123)
    by_id["table_set"]["tables"] = sorted(
        [
            name
            for name in METADATA.tables
            # `project_focus_history` arrives with 050, `brain_session_checkpoints`
            # with 051 and `access_log_daily` with 052 — all long after the 035 this
            # asset describes. See the note in test_recovery_contract.py.
            if name
            not in {
                "access_log_daily",
                "brain_session_artifacts",
                "brain_session_checkpoints",
                "project_focus_history",
                "ticket_extraction_attempts",
            }
        ]
        + ["alembic_version"]
    )
    checks.extend(
        [
            {
                "id": name,
                "kind": "brain_schema_invariant",
                "name": name,
            }
            for name in (
                "brain_sessions_032",
                "graph_foundation_033",
                "graph_projection_034_035",
            )
        ]
    )
    checks.append(
        {
            "id": "graph_relations_nonempty",
            "kind": "row_count_sum_min",
            "minimum": 1,
            "tables": ["entity_relations"],
        }
    )
    return sorted(checks, key=lambda check: check["id"])


def _contract_bytes() -> bytes:
    return CONTRACT_PATH.read_bytes()


def _contract_document() -> dict[str, Any]:
    document = json.loads(_contract_bytes())
    assert isinstance(document, dict)
    return document


def _attestation_sql() -> str:
    return ATTESTATION_SQL_PATH.read_text(encoding="utf-8")


def test_v2_assets_are_exact_canonical_authority() -> None:
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
    assert document == {
        "checks": _expected_checks(),
        "contract_id": "brain-v42/postgresql-recovery/v2",
        "engine": "postgresql",
        "schema_version": 2,
    }
    assert len(document["checks"]) == 24
    assert len(document["checks"][-1]["tables"]) == 30


def test_v2_uses_one_closed_domain_invariant_kind() -> None:
    checks = _contract_document()["checks"]
    domain_checks = [check for check in checks if check["kind"] == "brain_schema_invariant"]

    assert domain_checks == [
        {"id": name, "kind": "brain_schema_invariant", "name": name}
        for name in (
            "brain_sessions_032",
            "graph_foundation_033",
            "graph_projection_034_035",
        )
    ]
    assert not any("sql" in key.lower() for check in checks for key in check)


def test_v2_assets_match_the_audited_sha256_digests() -> None:
    assert hashlib.sha256(_contract_bytes()).hexdigest() == EXPECTED_JSON_SHA256
    assert hashlib.sha256(ATTESTATION_SQL_PATH.read_bytes()).hexdigest() == EXPECTED_SQL_SHA256


def test_v2_attestation_is_fixed_read_only_and_complete() -> None:
    sql = _attestation_sql()
    prohibited = re.compile(
        r"\b(alter|analyze|call|cluster|copy|create|delete|do|drop|execute|grant|insert|"
        r"into|lock|merge|nextval|pg_logical_emit_message|pg_read_file|pg_write_file|refresh|"
        r"reindex|revoke|set_config|setval|truncate|update|vacuum)\b",
        re.IGNORECASE,
    )

    assert sql.startswith("WITH ")
    assert sql.endswith(";\n")
    assert sql.count(";") == 1
    assert prohibited.search(sql) is None
    assert set(re.findall(r"\bpublic\.([a-z][a-z0-9_]*)\s*\(", sql, flags=re.IGNORECASE)) == {
        "vector_dims"
    }
    for check in _expected_checks():
        assert len(re.findall(rf"'{re.escape(check['id'])}'", sql)) == 1
    assert "'brain-v42/postgresql-recovery/v2'" in sql
    assert "'035'" in sql


def test_v2_sql_attests_targeted_032_035_objects_and_invariants() -> None:
    sql = _attestation_sql().lower()
    for fragment in (
        "project_contexts_focus_revision_trigger",
        "increment_project_focus_revision",
        "brain_sessions_terminal_state_valid",
        "projects_key_format_valid",
        "project_contexts_related_project_registry_trigger",
        "sync_brain_entity_registry",
        "graph_projection_leases_protocol_valid",
        "graph_projection_leases_armed_generation_valid",
        "graph_projection_leases_recovery_state_valid",
        "lease_generation",
        "claim_version",
        "last_completed_recovery_id",
    ):
        assert fragment in sql
    assert "count(*) from public.entity_relations" in " ".join(sql.split())
