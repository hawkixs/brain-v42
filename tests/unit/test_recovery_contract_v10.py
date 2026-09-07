"""Recovery contract v10 — a candidate for schema head 053.

The generation is immutable lineage, not a production receipt.  Its base and
``pg_restore`` variants are assembled solely from the isolated 053 measurement;
the executable yardstick separately proves that those fingerprints replay
against a fresh head.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path

RECOVERY = Path(__file__).resolve().parents[2] / "ops" / "recovery"

V9_ASSETS = {
    "brain-v42-v9.json": "c838165915e13feb507b4c6d2cdab0b91181ff4d1f4f4d54b9587ec4c3fac551",
    "brain-v42-v9.sql": "8d63620ca15f99b19744d71caefcba7eafb0792b2220039d298a65bb9667b791",
    "brain-v42-v9-pgrestore.sql": "7c67b8cd0a303d044a21158e7518d942719214ff8d7aa2eb5eef1e927e8b272b",
    "brain-v42-v9-acl.sql": "31a340a2ad08500144a41ec3f202a96cea52947e636278cdbae38ad2c8bd7987",
    "brain-v42-v9-acl.json": "1492782a60d69d185f6c600ddf8e38b3d6f4d68bda7460b7e4e02d95b0fe0089",
    "brain-v42-v9-acl-pgrestore.sql": "8169f2572774414539815e48e0432eedeea7c7ab775bdb00ec79c4af89e461c3",
    "brain-v42-v9-acl-pgrestore.json": "5dd4428507720368de7cce48a2d4d0f37779d0a751bd183e27d58f45510ae049",
}
V10_ASSETS = {
    "brain-v42-v10.json": "84385af8f6f72bd9a86d54cf3ffebcb449fc828b1eea38e923f80284bdff02d4",
    "brain-v42-v10.sql": "ce67b25927193d75b5d351a9e804ab0126868d80c6ae63f56144239166f1fefc",
    "brain-v42-v10-pgrestore.sql": "23c59ea8ac9c6b67ede8f682381f6ceb84872638cf81952dbbe584d0928972c1",
    "brain-v42-v10-acl.sql": "ef2fbc5615cb737e0839f804709e4b8942aeb99d9f32f0f03207850d6ef74a85",
    "brain-v42-v10-acl.json": "3eaf5ecf9972e5fb3a678822ce74c3c976e26d19d37131a136e861e3c0ba1f92",
    "brain-v42-v10-acl-pgrestore.sql": "f04e00afe8446d605a37b8ec2b384d696a5338e95b776246cb42716485557923",
    "brain-v42-v10-acl-pgrestore.json": "27d30015ab5789666f4aef462686355d3712a7f29cd2836601fe8c49b0aeb294",
}

V9_SQL = RECOVERY / "brain-v42-v9.sql"
V9_PGRESTORE = RECOVERY / "brain-v42-v9-pgrestore.sql"
V10_SQL = RECOVERY / "brain-v42-v10.sql"
V10_PGRESTORE = RECOVERY / "brain-v42-v10-pgrestore.sql"
V10_JSON = RECOVERY / "brain-v42-v10.json"
V9_ACL = (
    RECOVERY / "brain-v42-v9-acl.sql",
    RECOVERY / "brain-v42-v9-acl-pgrestore.sql",
    RECOVERY / "brain-v42-v9-acl.json",
    RECOVERY / "brain-v42-v9-acl-pgrestore.json",
)
V10_ACL = (
    RECOVERY / "brain-v42-v10-acl.sql",
    RECOVERY / "brain-v42-v10-acl-pgrestore.sql",
    RECOVERY / "brain-v42-v10-acl.json",
    RECOVERY / "brain-v42-v10-acl-pgrestore.json",
)

DELIVERY_TABLES = {
    "delivery_artifact_bindings",
    "delivery_confirmations",
    "delivery_contract_revisions",
    "delivery_dependencies",
    "delivery_events",
    "delivery_receipts",
    "delivery_snapshots",
    "delivery_workflows",
}
EXPECTED_CATALOG = {
    "foreign_keys": 46,
    "indexes": 156,
    "invalid_indexes": 0,
    "unvalidated_constraints": 0,
}
REMOVABLE_SQL_LINES = {
    "     ('tickets')",
    "         'foreign_keys', 27,",
    "         'indexes', 136,",
    " 'contract_id', 'brain-v42/postgresql-recovery/v9',",
    " 'schema_version', 9",
}


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


def test_v9_recovery_assets_remain_byte_identical() -> None:
    for name, expected in V9_ASSETS.items():
        assert hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest() == expected


def test_v10_candidate_assets_freeze_the_measured_fingerprints() -> None:
    for name, expected in V10_ASSETS.items():
        assert hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest() == expected


def test_v10_sql_assets_are_an_additive_053_candidate() -> None:
    for before, after in ((V9_SQL, V10_SQL), (V9_PGRESTORE, V10_PGRESTORE)):
        added, removed = _added_and_removed(before, after)
        assert set(removed) <= REMOVABLE_SQL_LINES
        assert set(removed) == REMOVABLE_SQL_LINES
        assert added
        text = after.read_text(encoding="utf-8")
        assert "postgresql-recovery/v9" not in text
        assert "'brain-v42/postgresql-recovery/v10'" in text
        for table in DELIVERY_TABLES:
            assert f"'{table}'" in text


def test_v10_manifest_is_the_current_053_catalog() -> None:
    document = json.loads(V10_JSON.read_text(encoding="utf-8"))
    assert document["contract_id"] == "brain-v42/postgresql-recovery/v10"
    assert document["schema_version"] == 10
    checks = {check["id"]: check for check in document["checks"]}
    assert {key: checks["catalog_counts"][key] for key in EXPECTED_CATALOG} == EXPECTED_CATALOG
    assert DELIVERY_TABLES <= set(checks["table_set"]["tables"])
    assert len(checks["table_set"]["tables"]) == 43


def test_v10_acl_assets_change_only_identity_when_053_has_no_privilege_change() -> None:
    migration = (
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / "053_delivery_workflows.py"
    ).read_text(encoding="utf-8")
    assert "GRANT" not in migration.upper().replace("GRANTED", "")
    assert "REVOKE" not in migration.upper()

    for old, new in zip(V9_ACL, V10_ACL, strict=True):
        expected = old.read_text(encoding="utf-8").replace("/v9", "/v10")
        expected = expected.replace('"schema_version":9', '"schema_version":10')
        expected = expected.replace('"schema_version": 9', '"schema_version": 10')
        expected = expected.replace("'schema_version', 9", "'schema_version', 10")
        assert new.read_text(encoding="utf-8") == expected
