"""Recovery contract v11 — a candidate for schema head 054.

The generation is immutable lineage, not a production receipt.  Its base and
``pg_restore`` variants are v10 plus pure insertions — `delivery_attestations`,
its seven constraints and four indexes — measured with the asset's own recipes
on a disposable chain-built database at head 054 and on a real custom-format
dump/restore of that database, where the new table's fingerprints proved
identical; the executable yardstick separately proves that those fingerprints
replay against a fresh head.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path

RECOVERY = Path(__file__).resolve().parents[2] / "ops" / "recovery"

V10_ASSETS = {
    "brain-v42-v10.json": "84385af8f6f72bd9a86d54cf3ffebcb449fc828b1eea38e923f80284bdff02d4",
    "brain-v42-v10.sql": "ce67b25927193d75b5d351a9e804ab0126868d80c6ae63f56144239166f1fefc",
    "brain-v42-v10-pgrestore.sql": "23c59ea8ac9c6b67ede8f682381f6ceb84872638cf81952dbbe584d0928972c1",
    "brain-v42-v10-acl.sql": "ef2fbc5615cb737e0839f804709e4b8942aeb99d9f32f0f03207850d6ef74a85",
    "brain-v42-v10-acl.json": "3eaf5ecf9972e5fb3a678822ce74c3c976e26d19d37131a136e861e3c0ba1f92",
    "brain-v42-v10-acl-pgrestore.sql": "f04e00afe8446d605a37b8ec2b384d696a5338e95b776246cb42716485557923",
    "brain-v42-v10-acl-pgrestore.json": "27d30015ab5789666f4aef462686355d3712a7f29cd2836601fe8c49b0aeb294",
}
V11_ASSETS = {
    "brain-v42-v11.json": "b7022b4a981d1c9805bf6ea4259b4727ed284784320a77e8ae78c5657ad509c4",
    "brain-v42-v11.sql": "f186dbec997a47c8c9c26113048a81e48f2135ba5dcf38042fc51908772a4916",
    "brain-v42-v11-pgrestore.sql": "2cb803c78ed1b3c46bba1dd407695fd6b6ab44cca09bb405d22f7df7fce2c1ac",
    "brain-v42-v11-acl.sql": "767e89573bca09bb821468bfb6cfe616e96c7d6a81774fd2a5df3c82e871e2d2",
    "brain-v42-v11-acl.json": "bbff3fad6b7756a29f80097ae9451ebbe5e306c32169f1d43cae17b9323183e9",
    "brain-v42-v11-acl-pgrestore.sql": "3e434e425080afa625dd6f163bc27f06d1a37602b0aa51b12df918df95e37de7",
    "brain-v42-v11-acl-pgrestore.json": "43a580d16b5b40e9c4e3466241a4419b4b25df823e4bc58c89bfdf72d799dd02",
}

V10_SQL = RECOVERY / "brain-v42-v10.sql"
V10_PGRESTORE = RECOVERY / "brain-v42-v10-pgrestore.sql"
V11_SQL = RECOVERY / "brain-v42-v11.sql"
V11_PGRESTORE = RECOVERY / "brain-v42-v11-pgrestore.sql"
V11_JSON = RECOVERY / "brain-v42-v11.json"
V10_ACL = (
    RECOVERY / "brain-v42-v10-acl.sql",
    RECOVERY / "brain-v42-v10-acl-pgrestore.sql",
    RECOVERY / "brain-v42-v10-acl.json",
    RECOVERY / "brain-v42-v10-acl-pgrestore.json",
)
V11_ACL = (
    RECOVERY / "brain-v42-v11-acl.sql",
    RECOVERY / "brain-v42-v11-acl-pgrestore.sql",
    RECOVERY / "brain-v42-v11-acl.json",
    RECOVERY / "brain-v42-v11-acl-pgrestore.json",
)

DELIVERY_TABLES = {
    "delivery_artifact_bindings",
    "delivery_attestations",
    "delivery_confirmations",
    "delivery_contract_revisions",
    "delivery_dependencies",
    "delivery_events",
    "delivery_receipts",
    "delivery_snapshots",
    "delivery_workflows",
}
#: Every constraint and index 054 creates; the candidate must name each one.
ATTESTATION_OBJECTS = {
    "delivery_attestations_pkey",
    "uq_delivery_attestation_idempotency",
    "delivery_attestations_kind_valid",
    "delivery_attestations_payload_object",
    "delivery_attestations_digest_valid",
    "delivery_attestations_ticket_id_fkey",
    "delivery_attestations_ticket_id_contract_revision_fkey",
    "ix_delivery_attestations_ticket_kind_emitted",
    "ix_delivery_attestations_issuer_kind_emitted",
}
#: v10 froze 46 foreign keys and 156 indexes at head 053; 054 adds two foreign
#: keys (`ticket_id`, and the composite one to the contract revision) and four
#: indexes (primary key, unique idempotency key, and the two newest-first read
#: indexes of the ticket and issuer-project scopes).
EXPECTED_CATALOG = {
    "foreign_keys": 48,
    "indexes": 160,
    "invalid_indexes": 0,
    "unvalidated_constraints": 0,
}
#: The only lines v11 takes away from v10: the two catalog counts and the identity.
#: No table row was reworded — the new table is inserted before `delivery_confirmations`
#: in every VALUES list, so even the trailing comma of the last row survives.
REMOVABLE_SQL_LINES = {
    "         'foreign_keys', 46,",
    "         'indexes', 156,",
    " 'contract_id', 'brain-v42/postgresql-recovery/v10',",
    " 'schema_version', 10",
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


def test_v10_recovery_assets_remain_byte_identical() -> None:
    for name, expected in V10_ASSETS.items():
        assert hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest() == expected


def test_v11_candidate_assets_freeze_the_measured_fingerprints() -> None:
    for name, expected in V11_ASSETS.items():
        assert hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest() == expected


def test_v11_sql_assets_are_an_additive_054_candidate() -> None:
    added_by_variant: list[set[str]] = []
    for before, after in ((V10_SQL, V11_SQL), (V10_PGRESTORE, V11_PGRESTORE)):
        added, removed = _added_and_removed(before, after)
        assert set(removed) == REMOVABLE_SQL_LINES
        assert added
        added_by_variant.append(set(added))
        text = after.read_text(encoding="utf-8")
        assert "postgresql-recovery/v10" not in text
        assert "'brain-v42/postgresql-recovery/v11'" in text
        for table in DELIVERY_TABLES:
            assert f"'{table}'" in text
        for name in ATTESTATION_OBJECTS:
            assert f"'{name}'" in text
    # The restore bench re-serialised none of the new table's objects: the twin
    # gains exactly the rows the base asset gains.
    assert added_by_variant[0] == added_by_variant[1]


def test_v11_manifest_is_the_current_054_catalog() -> None:
    document = json.loads(V11_JSON.read_text(encoding="utf-8"))
    assert document["contract_id"] == "brain-v42/postgresql-recovery/v11"
    assert document["schema_version"] == 11
    checks = {check["id"]: check for check in document["checks"]}
    assert {key: checks["catalog_counts"][key] for key in EXPECTED_CATALOG} == EXPECTED_CATALOG
    assert DELIVERY_TABLES <= set(checks["table_set"]["tables"])
    assert len(checks["table_set"]["tables"]) == 44
    assert checks["table_set"]["tables"] == sorted(checks["table_set"]["tables"])


def test_v11_acl_assets_change_only_identity_when_054_has_no_privilege_change() -> None:
    migration = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "054_delivery_attestations.py"
    ).read_text(encoding="utf-8")
    assert "GRANT" not in migration.upper().replace("GRANTED", "")
    assert "REVOKE" not in migration.upper()

    for old, new in zip(V10_ACL, V11_ACL, strict=True):
        expected = old.read_text(encoding="utf-8").replace("/v10", "/v11")
        expected = expected.replace('"schema_version":10', '"schema_version":11')
        expected = expected.replace('"schema_version": 10', '"schema_version": 11')
        expected = expected.replace("'schema_version', 10", "'schema_version', 11")
        assert new.read_text(encoding="utf-8") == expected
