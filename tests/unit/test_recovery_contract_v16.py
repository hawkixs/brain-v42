"""Recovery contract v16 — v15 plus two attestation fixes, still for head 058.

The generation is immutable lineage, not a production receipt. v16 moves no
fingerprint (the schema is v15's); it fixes two defects red-backup's DR-v7 drill
found in the attestation itself (decisions 7d2f7fe8 and ee26407b):

* the final ``jsonb_agg`` orders checks by ``id COLLATE "C"``, so a locale-collated
  server returns them in byte order;
* ``artifact_project_mismatches`` tolerates a captured artifact whose knowledge
  row is gone everywhere and is tombstoned ``lifecycle = 'deleted'`` in
  ``brain_entities``, with the matching type, in the session's project.

These tests pin the delta line by line: anything else that moves is a mistake.
The behaviour is proven on disposable databases by
``tests/integration/db/test_recovery_contract_v16.py``.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path

RECOVERY = Path(__file__).resolve().parents[2] / "ops" / "recovery"

V14_ASSETS = {
    "brain-v42-v14.json": "c6f963bf17450c7db340716b8c396b3358dec4e745369d5f245aaa36b4a48e60",
    "brain-v42-v14.sql": "0264e4b5b26106c440ef30cfc6b38a3b2e547a8b929ea6204432ac3e6308fd36",
    "brain-v42-v14-pgrestore.sql": (
        "1333d01eb31b0e7cfadb4029c67bd80d743cf8a9f758c2ae389492e11a1e665a"
    ),
}

V15_ASSETS = {
    "brain-v42-v15.json": "db8a31d2bf594051a6dee9f4889155d6901a6a423e03d425577e4d7f6f3b1d2e",
    "brain-v42-v15.sql": "512efe488386192bc90d9321d0116748c85a1d14d8bcbe153e60496e2003e1aa",
    "brain-v42-v15-pgrestore.sql": "6e028f17db48c43937b0615d2b424721906007700b18074e0760a8b89b047d54",
}

#: The SHA-256 of the three v16 assets, pinned after the mint.
V16_ASSETS = {
    "brain-v42-v16.json": "59e867bc906be76727e5ce803282e712a2ec965ed06ff1a6deca5ff16b5f0833",
    "brain-v42-v16.sql": "957460544eb68335b690ce7f911e7a45b16f3a65cec07d8c94cacc2e581c63f8",
    "brain-v42-v16-pgrestore.sql": "fbb2bf78e763f8adf898c948b0abaaa47fe41301e78bbbc70054961063b4ef69",
}

V15_SQL = RECOVERY / "brain-v42-v15.sql"
V15_PGRESTORE = RECOVERY / "brain-v42-v15-pgrestore.sql"
V15_JSON = RECOVERY / "brain-v42-v15.json"
V16_SQL = RECOVERY / "brain-v42-v16.sql"
V16_PGRESTORE = RECOVERY / "brain-v42-v16-pgrestore.sql"
V16_JSON = RECOVERY / "brain-v42-v16.json"

#: Every line v16 removes from v15, identical in both variants.
REMOVED_LINES = {
    "     ORDER BY id",
    " GROUP BY artifact_record.session_id, artifact_record.knowledge_id",
    " FROM artifact_source_matches",
    " WHERE source_matches <> 1 OR typed_matches <> 1",
    " 'contract_id', 'brain-v42/postgresql-recovery/v15',",
    " 'schema_version', 15",
}

#: Every line v16 adds, identical in both variants, in order.
ADDED_LINES = [
    "     artifact_record.knowledge_type,",
    "     artifact_record.captured_at,",
    "     session_record.project_key,",
    "     session_record.started_at,",
    " GROUP BY",
    "     artifact_record.session_id,",
    "     artifact_record.knowledge_id,",
    "     artifact_record.knowledge_type,",
    "     artifact_record.captured_at,",
    "     session_record.project_key,",
    "     session_record.started_at",
    " FROM artifact_source_matches AS match_record",
    " WHERE (match_record.source_matches <> 1 OR match_record.typed_matches <> 1)",
    "   AND NOT (",
    "       NOT EXISTS (",
    "           SELECT 1",
    "           FROM knowledge_sources AS any_source",
    "           WHERE any_source.knowledge_id = match_record.knowledge_id",
    "       )",
    "       AND EXISTS (",
    "           SELECT 1",
    "           FROM public.brain_entities AS entity_record",
    "           WHERE entity_record.source_uuid = match_record.knowledge_id",
    "             AND entity_record.lifecycle = 'deleted'",
    "             AND entity_record.created_at >= match_record.started_at",
    "             AND entity_record.created_at <= match_record.captured_at",
    "             AND entity_record.deleted_at >= match_record.captured_at",
    "             AND entity_record.project_key = match_record.project_key",
    "             AND (",
    "                 (",
    "                     match_record.knowledge_type IN (",
    "                         'decision', 'learning', 'snippet', 'runbook', 'adr'",
    "                     )",
    "                     AND entity_record.entity_type = match_record.knowledge_type",
    "                 )",
    "                 OR (",
    "                     match_record.knowledge_type = 'indexed_plan'",
    "                     AND entity_record.entity_type = 'plan'",
    "                 )",
    "                 OR (",
    "                     match_record.knowledge_type = 'legacy'",
    "                     AND entity_record.entity_type IN (",
    "                         'decision', 'learning', 'snippet', 'runbook', 'adr', 'plan'",
    "                     )",
    "                 )",
    "             )",
    "       )",
    "   )",
    '     ORDER BY id COLLATE "C"',
    " 'contract_id', 'brain-v42/postgresql-recovery/v16',",
    " 'schema_version', 16",
]


def _sha256(name: str) -> str:
    return hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest()


def _added_and_removed(before: Path, after: Path) -> tuple[list[str], list[str]]:
    diff = list(
        difflib.ndiff(
            before.read_text(encoding="utf-8").splitlines(),
            after.read_text(encoding="utf-8").splitlines(),
        )
    )
    return (
        [line[2:] for line in diff if line.startswith("+ ")],
        [line[2:] for line in diff if line.startswith("- ")],
    )


def test_previous_generations_remain_byte_identical() -> None:
    """v14 and v15 are lineage: they never change again."""
    for name, expected in {**V14_ASSETS, **V15_ASSETS}.items():
        assert _sha256(name) == expected, name


def test_v16_assets_are_pinned() -> None:
    for name, expected in V16_ASSETS.items():
        assert _sha256(name) == expected, (
            f"{name}: SHA-256 does not match the pinned value — mint the v16 asset "
            f"then paste its digest into V16_ASSETS"
        )


def test_v16_sql_is_v15_plus_exactly_the_two_fixes_in_both_variants() -> None:
    for before, after in ((V15_SQL, V16_SQL), (V15_PGRESTORE, V16_PGRESTORE)):
        added, removed = _added_and_removed(before, after)
        assert set(removed) == REMOVED_LINES, after.name
        assert len(removed) == len(REMOVED_LINES), after.name
        assert added == ADDED_LINES, after.name


def test_the_two_variants_differ_exactly_as_v15_s_do() -> None:
    """The edits touch no fingerprint, so base and twin still differ only where v15's do."""
    v15 = list(
        difflib.unified_diff(
            V15_SQL.read_text().splitlines(), V15_PGRESTORE.read_text().splitlines(), n=0
        )
    )
    v16 = list(
        difflib.unified_diff(
            V16_SQL.read_text().splitlines(), V16_PGRESTORE.read_text().splitlines(), n=0
        )
    )
    changed = [line for line in v15 if line[:1] in "+-" and not line.startswith(("+++", "---"))]
    assert [
        line for line in v16 if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ] == changed


def test_every_ordering_of_the_final_aggregate_is_byte_order() -> None:
    for asset in (V16_SQL, V16_PGRESTORE):
        tail = asset.read_text(encoding="utf-8").rsplit("SELECT jsonb_build_object(", 1)[1]
        assert 'ORDER BY id COLLATE "C"' in tail
        assert "ORDER BY id\n" not in tail


def test_v16_manifest_is_v15_with_only_the_identity_moved() -> None:
    v15 = json.loads(V15_JSON.read_text(encoding="utf-8"))
    v16 = json.loads(V16_JSON.read_text(encoding="utf-8"))
    assert v16["contract_id"] == "brain-v42/postgresql-recovery/v16"
    assert v16["schema_version"] == 16
    v15["contract_id"] = v16["contract_id"]
    v15["schema_version"] = v16["schema_version"]
    assert v16 == v15
    assert V16_JSON.read_text(encoding="utf-8") == (
        json.dumps(v16, sort_keys=True, separators=(",", ":")) + "\n"
    )


def test_v16_ships_no_acl_asset_because_it_grants_nothing() -> None:
    for suffix in ("-acl.sql", "-acl.json", "-acl-pgrestore.sql", "-acl-pgrestore.json"):
        assert not (RECOVERY / f"brain-v42-v16{suffix}").exists()
        assert (RECOVERY / f"brain-v42-v11{suffix}").is_file()


def test_current_binding_names_v16() -> None:
    current = json.loads((RECOVERY / "current.json").read_text(encoding="utf-8"))
    assert current["contract_id"] == "brain-v42/postgresql-recovery/v16"
    assert current["contract_version"] == 16
    assert current["schema_head"] == "058"
    for key, name in (
        ("attestation_sql", "brain-v42-v16.sql"),
        ("restored_attestation_sql", "brain-v42-v16-pgrestore.sql"),
        ("manifest", "brain-v42-v16.json"),
    ):
        assert current[key]["path"] == f"ops/recovery/{name}"
        assert current[key]["sha256"] == _sha256(name)
