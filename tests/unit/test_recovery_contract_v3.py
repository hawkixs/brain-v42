"""Static authority checks for the head-037 PostgreSQL recovery contract."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).parents[2]
RECOVERY_DIR = PROJECT_ROOT / "ops" / "recovery"
V2_CONTRACT_PATH = RECOVERY_DIR / "brain-v42-v2.json"
V3_CONTRACT_PATH = RECOVERY_DIR / "brain-v42-v3.json"
V3_ATTESTATION_SQL_PATH = RECOVERY_DIR / "brain-v42-v3.sql"

SCOPED_VIEWS = {
    "codex_brain_entity_v1": (
        "id",
        "type",
        "title",
        "status",
        "freshness_status",
        "content",
        "project_key",
        "updated_at",
        "superseded_by",
        "merged_into",
    ),
    "codex_feature_artifact_v1": (
        "feature_id",
        "artifact_type",
        "artifact_id",
        "similarity_score",
        "created_at",
    ),
    "codex_feature_v1": (
        "id",
        "project_key",
        "name",
        "description",
        "status",
        "status_updated_at",
        "pinned",
        "merged_into",
        "created_at",
        "updated_at",
    ),
    "codex_roadmap_curation_proposal_v1": (
        "id",
        "op",
        "feature_id",
        "payload",
        "rationale",
        "status",
        "apply_log",
        "created_at",
        "applied_at",
    ),
    "codex_ticket_extraction_proposal_v1": (
        "id",
        "ticket_id",
        "target_type",
        "target_project",
        "payload",
        "rationale",
        "status",
        "applied_entity_id",
        "created_at",
        "applied_at",
    ),
    "codex_ticket_message_v1": (
        "id",
        "ticket_id",
        "author_project",
        "body",
        "status_to",
        "created_at",
    ),
    "codex_ticket_v1": (
        "id",
        "kind",
        "title",
        "body",
        "from_project",
        "to_project",
        "status",
        "extraction_status",
        "resolved_at",
        "closed_at",
        "created_at",
        "updated_at",
    ),
}

GLOBAL_VIEWS = {
    "codex_consolidation_log_v1": (
        "id",
        "source_id",
        "target_id",
        "entity_type",
        "similarity",
        "action",
        "created_at",
    ),
    "codex_dream_promotion_v1": (
        "id",
        "dream_run_id",
        "source_learning_id",
        "target_type",
        "target_adr_id",
        "target_runbook_id",
        "cosine_observed",
        "skipped_reason",
        "created_at",
    ),
    "codex_dream_run_v1": (
        "id",
        "run_date",
        "phase",
        "model",
        "status",
        "phase_dry_run",
        "duration_s",
        "cost_usd",
        "input_tokens",
        "output_tokens",
        "api_calls",
        "tool_calls",
        "error_message",
        "created_at",
    ),
}

VIEW_DEFINITION_MD5 = {
    "codex_brain_entity_v1": "9ff73f99a8786fe84b24c4e0c5ee6999",
    "codex_consolidation_log_v1": "d0884470e57b41987a2f06cb206b460c",
    "codex_dream_promotion_v1": "c14ec7f88ee97f4359e1a7a90c21b967",
    "codex_dream_run_v1": "465f271c378c98cf26ffbeb0d97a67f3",
    "codex_feature_artifact_v1": "cf55ef799d0c446229f0b781b44099f1",
    "codex_feature_v1": "61a939fd9d0f9da9639beb2d71d57fdf",
    "codex_roadmap_curation_proposal_v1": "80d147acacb1e5b4a94d534fa0a32e3b",
    "codex_ticket_extraction_proposal_v1": "4d5220e663df342c2e0cccccd7910b3e",
    "codex_ticket_message_v1": "d91b467ff450e3b91e7ff14c9ad67e1d",
    "codex_ticket_v1": "9b248acd2480ea9ac252e3cbef09b47a",
}

TRIGGER_FUNCTION_DEFINITION_MD5 = {
    "enforce_immutable_ticket_participants": "cde22e328857b6209337369b8a43aacc",
    "enforce_live_feature_artifact_target": "81d1b2839f665f207df9a019736a87bb",
    "increment_project_focus_revision": "c13b0ab647661e42c74f9726a8ca3c54",
}

TRIGGER_DEFINITION_MD5 = {
    "project_contexts_focus_revision_trigger": "4b1ca0f513d1bca895bfa8931f488e66",
    "trg_feature_artifact_live_target": "09243973b66a4e83a206d21cb01a46e6",
    "trg_ticket_participants_immutable": "9e4f03a836f26bc36fb99be861508a90",
}

ARTIFACT_INDEX_DEFINITION_MD5 = {
    "brain_session_artifacts_pkey": "fe2984a0da51999576c9e1c26810a0db",
    "idx_brain_session_artifacts_session_captured": "1537b589099c6198cc66c39ad6656e94",
}

SESSION_INDEX_DEFINITION_MD5 = {
    "brain_sessions_pkey": "6763cd8159ef6f0131abbfedfea044bc",
    "idx_brain_sessions_project_status_started": "daf2b70c6799177168837efedcb0dbe8",
    "uq_brain_sessions_project_client": "28c33a3d73bf9f0c64d322978b7118a4",
}

COLUMN_DEFINITION_MD5 = {
    "brain_session_artifacts": "6929b0b5a35595521022bcfe25cf5a07",
    "brain_sessions": "bf4c2a47e41aa69872119982b390f45a",
    "codex_brain_entity_v1": "c8aa9c21e5706e1a4983df5a2dd18213",
    "codex_consolidation_log_v1": "9c951f395a2388883bca636bf0ae9147",
    "codex_dream_promotion_v1": "57a2135efb8e79ec1d9ed8df8bfa28b1",
    "codex_dream_run_v1": "373b6f19d4c86571961ba31b662a0853",
    "codex_feature_artifact_v1": "6ce4788f749dcc4cce3f11658117efac",
    "codex_feature_v1": "554926bf35b60549a55414e9202f143f",
    "codex_roadmap_curation_proposal_v1": "9b34ac92a6fab0032a6cdd4a0b9647d9",
    "codex_ticket_extraction_proposal_v1": "11d7b8be4a85efd1312967fa6e253ecc",
    "codex_ticket_message_v1": "9c87fa93b464bf432c1c4a6609721752",
    "codex_ticket_v1": "5abd645cadecf917aac6a5c091a91806",
}


def _v2_document() -> dict[str, Any]:
    document = json.loads(V2_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _expected_checks() -> list[dict[str, Any]]:
    checks = json.loads(json.dumps(_v2_document()["checks"]))
    by_id = {check["id"]: check for check in checks}
    by_id["alembic_head"]["revision"] = "037"
    by_id["catalog_counts"].update(foreign_keys=25, indexes=125)
    by_id["table_set"]["tables"] = sorted(
        [*by_id["table_set"]["tables"], "brain_session_artifacts"]
    )
    by_id["brain_sessions_032"].update(
        id="brain_runtime_032_036_037",
        name="brain_runtime_032_036_037",
    )
    return sorted(checks, key=lambda check: check["id"])


def _v3_bytes() -> bytes:
    return V3_CONTRACT_PATH.read_bytes()


def _v3_document() -> dict[str, Any]:
    document = json.loads(_v3_bytes())
    assert isinstance(document, dict)
    return document


def _attestation_sql() -> str:
    return V3_ATTESTATION_SQL_PATH.read_text(encoding="utf-8")


def test_v3_assets_are_exact_canonical_authority() -> None:
    raw = _v3_bytes()
    document = _v3_document()
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
        "contract_id": "brain-v42/postgresql-recovery/v3",
        "engine": "postgresql",
        "schema_version": 3,
    }
    assert len(document["checks"]) == 24
    assert (
        len(next(check for check in document["checks"] if check["id"] == "table_set")["tables"])
        == 31
    )


def test_v3_preserves_twenty_three_v2_ids_and_replaces_the_runtime_invariant() -> None:
    v2_checks = _v2_document()["checks"]
    v3_checks = _v3_document()["checks"]
    v2_ids = {check["id"] for check in v2_checks}
    v3_ids = {check["id"] for check in v3_checks}

    assert v3_ids - v2_ids == {"brain_runtime_032_036_037"}
    assert v2_ids - v3_ids == {"brain_sessions_032"}
    assert [check for check in v3_checks if check["kind"] == "brain_schema_invariant"] == [
        {
            "id": "brain_runtime_032_036_037",
            "kind": "brain_schema_invariant",
            "name": "brain_runtime_032_036_037",
        },
        {
            "id": "graph_foundation_033",
            "kind": "brain_schema_invariant",
            "name": "graph_foundation_033",
        },
        {
            "id": "graph_projection_034_035",
            "kind": "brain_schema_invariant",
            "name": "graph_projection_034_035",
        },
    ]


def test_v3_attestation_is_one_read_only_complete_statement() -> None:
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
    assert "'brain-v42/postgresql-recovery/v3'" in sql
    assert "'037'" in sql
    assert "'schema_version', 3" in sql


def test_v3_attests_exact_036_views_columns_barriers_and_fences_without_acl() -> None:
    sql = _attestation_sql().lower()

    assert len(SCOPED_VIEWS) == 7
    assert len(GLOBAL_VIEWS) == 3
    all_views = {**SCOPED_VIEWS, **GLOBAL_VIEWS}
    assert set(all_views) == set(VIEW_DEFINITION_MD5)
    assert set(all_views) < set(COLUMN_DEFINITION_MD5)
    for view_name in all_views:
        assert re.search(rf"\(\s*'{re.escape(view_name)}',", sql)
    for view_name in SCOPED_VIEWS:
        assert re.search(rf"\(\s*'{re.escape(view_name)}',\s*true,", sql)
    for view_name in GLOBAL_VIEWS:
        assert re.search(rf"\(\s*'{re.escape(view_name)}',\s*false,", sql)

    for fragment in (
        "enforce_immutable_ticket_participants",
        "enforce_live_feature_artifact_target",
        "trg_feature_artifact_live_target",
        "trg_ticket_participants_immutable",
    ):
        assert fragment in sql
    for acl_fragment in (
        "aclexplode",
        "codex_ro",
        "has_any_column_privilege",
        "has_column_privilege",
        "has_schema_privilege",
        "has_table_privilege",
        "nspowner",
        "pg_authid",
        "pg_get_userbyid",
        "pg_roles",
        "proowner",
        "relacl",
        "relowner",
        "role_table_grants",
        "rolname",
        "tableowner",
        "viewowner",
    ):
        assert re.search(rf"\b{acl_fragment}\b", sql) is None


def test_v3_attests_032_and_037_schema_and_data_with_terminal_only_snapshot_parity() -> None:
    sql = _attestation_sql().lower()

    for fragment in (
        "project_contexts_focus_revision_trigger",
        "increment_project_focus_revision",
        "brain_sessions_capture_ids_valid",
        "brain_sessions_client_key_nonblank",
        "brain_sessions_focus_outcome_valid",
        "brain_sessions_terminal_state_valid",
        "array_position(captured_knowledge_ids, null::uuid) is null",
        "focus_outcome::text = any",
        "focus_revision_at_end = (end_expected_focus_revision + 1)",
        "focus_revision_at_end <> end_expected_focus_revision",
        "expected_session_constraint.definition",
        "last_heartbeat_at",
        "end_expected_focus_revision",
        "focus_outcome",
        "focus_at_end",
        "focus_revision_at_end",
        "brain_session_artifacts_pkey",
        "brain_session_artifacts_session_id_fkey",
        "brain_session_artifacts_type_valid",
        "confdeltype",
        "constraint_record.confdeltype::text = expected_constraint.delete_action",
        "check (knowledge_type::text = any",
        "pg_catalog.pg_get_constraintdef",
        "idx_brain_session_artifacts_session_captured",
        "knowledge_id",
        "session_id",
        "knowledge_type",
        "captured_at",
        "artifact_lifecycle_violations",
        "artifact_source_mismatches",
        "source_record.created_at",
        "source_matches <> 1 or typed_matches <> 1",
        "typed_matches",
        "group by artifact_record.session_id, artifact_record.knowledge_id",
        "artifact_record.captured_at > session_record.ended_at",
        "having count(*) > 100",
    ):
        assert fragment in sql

    assert "session_record.status = 'ended'" in sql
    assert "session_record.status <> 'ended'" not in sql
    assert "session_record.status in ('open', 'abandoned')" not in sql
    assert "ended_snapshot_mismatches" in sql
    assert "artifact_project_mismatches" in sql


def test_v3_pins_exact_view_and_trigger_function_definitions() -> None:
    sql = _attestation_sql().lower()

    assert set(VIEW_DEFINITION_MD5) == {*SCOPED_VIEWS, *GLOBAL_VIEWS}
    assert "expected_contract_views(view_name, security_barrier, definition_md5)" in sql
    assert "pg_catalog.pg_get_viewdef(table_record.oid, true)" in sql
    assert "is distinct from expected_view.definition_md5" in sql
    for view_name, definition_md5 in VIEW_DEFINITION_MD5.items():
        assert view_name in sql
        assert definition_md5 in sql

    assert "function_definition_md5" in sql
    assert "function_namespace_record.nspname = 'public'" in sql
    assert "pg_catalog.pg_get_functiondef(function_record.oid)" in sql
    assert "is distinct from expected_trigger.function_definition_md5" in sql
    for function_name, definition_md5 in TRIGGER_FUNCTION_DEFINITION_MD5.items():
        assert function_name in sql
        assert definition_md5 in sql


def test_v3_pins_exact_artifact_index_definitions() -> None:
    sql = _attestation_sql().lower()

    assert "definition_md5" in sql
    assert "pg_catalog.pg_get_indexdef(index_record.indexrelid)" in sql
    assert "left join pg_catalog.pg_attribute as attribute_record" in sql
    assert "is not distinct from expected_index.definition_md5" in sql
    for index_name, definition_md5 in ARTIFACT_INDEX_DEFINITION_MD5.items():
        assert index_name in sql
        assert definition_md5 in sql


def test_v3_rejects_trigger_when_clauses_and_extra_view_options() -> None:
    sql = _attestation_sql().lower()

    assert "trigger_definition_md5" in sql
    assert "pg_catalog.pg_get_triggerdef(trigger_record.oid, true)" in sql
    assert "is distinct from expected_trigger.trigger_definition_md5" in sql
    for trigger_name, definition_md5 in TRIGGER_DEFINITION_MD5.items():
        assert trigger_name in sql
        assert definition_md5 in sql

    assert "cardinality(coalesce(table_record.reloptions, array[]::text[]))" in sql
    assert "case when expected_view.security_barrier then 1 else 0 end" in sql


def test_v3_preserves_the_exact_focus_revision_column_contract() -> None:
    sql = _attestation_sql().lower()

    for fragment in (
        "observed_focus_column.data_type = 'bigint'",
        "observed_focus_column.is_nullable = 'no'",
        "observed_focus_column.column_default = '0'",
    ):
        assert fragment in sql
    assert "unexpected_focus_constraint" in sql
    assert "focus_attribute.attnum = any(constraint_record.conkey)" in sql


def test_v3_pins_runtime_relations_columns_view_outputs_and_fk_enforcement() -> None:
    sql = _attestation_sql().lower()

    assert "expected_column_fingerprints(object_name, definition_md5)" in sql
    assert "observed_column_fingerprints" in sql
    for field in (
        "observed_column.ordinal_position",
        "observed_column.udt_schema",
        "observed_column.udt_name",
        "observed_column.is_identity",
        "observed_column.is_generated",
        "observed_column.collation_name",
    ):
        assert field in sql
    for object_name, definition_md5 in COLUMN_DEFINITION_MD5.items():
        assert object_name in sql
        assert definition_md5 in sql

    for fragment in (
        "relation_record.relkind = 'r'",
        "relation_record.relpersistence = 'p'",
        "not relation_record.relispartition",
        "not relation_record.relhasrules",
        "not relation_record.relrowsecurity",
        "not relation_record.relforcerowsecurity",
        "cardinality(coalesce(relation_record.reloptions, array[]::text[])) = 0",
        "inheritance_link.inhrelid",
        "inheritance_link.inhparent",
        "access_method.amname = 'heap'",
        "fk_trigger.tgenabled = 'o'",
        "fk_trigger.tgisinternal",
        "count(*) = 4",
    ):
        assert fragment in sql


def test_v3_pins_the_exact_session_index_set() -> None:
    sql = _attestation_sql().lower()

    assert "expected_session_indexes(index_name, definition_md5)" in sql
    assert "pg_catalog.pg_get_indexdef(index_record.indexrelid)" in sql
    for index_name, definition_md5 in SESSION_INDEX_DEFINITION_MD5.items():
        assert index_name in sql
        assert definition_md5 in sql


def test_v3_closes_the_runtime_user_trigger_topology() -> None:
    sql = _attestation_sql().lower()
    topology = sql.split("expected_runtime_user_triggers(table_name, trigger_name) as (", 1)[
        1
    ].split("session_column_mismatches as (", 1)[0]

    assert "expected_runtime_user_triggers(table_name, trigger_name)" in sql
    for trigger_name in (
        "brain_sessions_project_alias_trigger",
        "brain_sessions_project_registry_trigger",
        "trg_feature_artifact_live_target",
        "project_contexts_focus_revision_trigger",
        "project_contexts_brain_registry_trigger",
        "project_contexts_project_alias_trigger",
        "project_contexts_project_key_immutable_trigger",
        "project_contexts_related_project_alias_trigger",
        "project_contexts_related_project_registry_trigger",
        "trg_project_contexts_updated",
        "trg_ticket_participants_immutable",
        "tickets_project_alias_trigger",
        "tickets_project_registry_trigger",
    ):
        assert trigger_name in topology
    for table_name in (
        "brain_sessions",
        "brain_session_artifacts",
        "feature_artifacts",
        "project_contexts",
        "tickets",
    ):
        assert table_name in topology
    assert "not trigger_record.tgisinternal" in sql
    assert "unexpected_runtime_trigger" in sql


def test_v3_rejects_rules_on_runtime_write_tables() -> None:
    sql = _attestation_sql().lower()

    assert "unexpected_runtime_rule" in sql
    assert "runtime_table.relhasrules" in sql


def test_v3_pins_historical_runtime_relation_properties() -> None:
    sql = _attestation_sql().lower()
    relation_check = sql.split("historical_runtime_relation_mismatch", 1)[1].split(") as value", 1)[
        0
    ]

    for table_name in ("feature_artifacts", "project_contexts", "tickets"):
        assert table_name in relation_check
    for fragment in (
        "runtime_table.relkind = 'r'",
        "runtime_table.relpersistence = 'p'",
        "not runtime_table.relispartition",
        "not runtime_table.relrowsecurity",
        "not runtime_table.relforcerowsecurity",
        "runtime_access_method.amname = 'heap'",
        "runtime_inheritance.inhrelid",
        "runtime_inheritance.inhparent",
    ):
        assert fragment in relation_check


def test_v3_output_shape_matches_the_v2_attestation_protocol() -> None:
    sql = " ".join(_attestation_sql().split())

    assert "'checks', jsonb_agg(" in sql
    assert "'expected', expected" in sql
    assert "'id', id" in sql
    assert "'observed', observed" in sql
    assert "'status', CASE WHEN passed THEN 'pass' ELSE 'fail' END" in sql
    assert "ORDER BY id" in sql
