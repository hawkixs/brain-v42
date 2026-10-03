"""Recovery contract v17 — v16 plus the tickets.target_release footprint."""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path

RECOVERY = Path(__file__).resolve().parents[2] / "ops" / "recovery"

V14_ASSETS = {
    "brain-v42-v14.json": "c6f963bf17450c7db340716b8c396b3358dec4e745369d5f245aaa36b4a48e60",
    "brain-v42-v14.sql": "0264e4b5b26106c440ef30cfc6b38a3b2e547a8b929ea6204432ac3e6308fd36",
    "brain-v42-v14-pgrestore.sql": "1333d01eb31b0e7cfadb4029c67bd80d743cf8a9f758c2ae389492e11a1e665a",
}
V15_ASSETS = {
    "brain-v42-v15.json": "db8a31d2bf594051a6dee9f4889155d6901a6a423e03d425577e4d7f6f3b1d2e",
    "brain-v42-v15.sql": "512efe488386192bc90d9321d0116748c85a1d14d8bcbe153e60496e2003e1aa",
    "brain-v42-v15-pgrestore.sql": "6e028f17db48c43937b0615d2b424721906007700b18074e0760a8b89b047d54",
}
V16_ASSETS = {
    "brain-v42-v16.json": "59e867bc906be76727e5ce803282e712a2ec965ed06ff1a6deca5ff16b5f0833",
    "brain-v42-v16.sql": "4055f270058a781428f6f67e189964d2b0cd18f09a889392aeb5cc960b4071e1",
    "brain-v42-v16-pgrestore.sql": "17ca4c51f0c57d65911d574b448ad771488f9463ee6ac599af130dcbf506d6ba",
}
V17_ASSETS = {
    "brain-v42-v17.json": "ea96516ed62179ecf44a2ba17d55d847ad7cda08dac79eb2c7c9e923d759d33b",
    "brain-v42-v17.sql": "8195744239a27a9f7f6a3d0bc2bc32e80b3f8b54239a2f8b06cbf31bf32bb210",
    "brain-v42-v17-pgrestore.sql": "8e52fa90bc802c26b1365526f606b26d79ebf819ccd12129e3d09766b083d00b",
}
V16_SQL = RECOVERY / "brain-v42-v16.sql"
V16_PGRESTORE = RECOVERY / "brain-v42-v16-pgrestore.sql"
V16_JSON = RECOVERY / "brain-v42-v16.json"
V17_SQL = RECOVERY / "brain-v42-v17.sql"
V17_PGRESTORE = RECOVERY / "brain-v42-v17-pgrestore.sql"
V17_JSON = RECOVERY / "brain-v42-v17.json"

REMOVED_LINES = {
    "         '9bde91df3a8426f75dc0dc316819192d'",
    "         'indexes', 172,",
    " 'contract_id', 'brain-v42/postgresql-recovery/v16',",
    " 'schema_version', 16",
}
ADDED_LINES = [
    "         '779e0a32fc5ac5c8fff41607e92f751a'",
    "     ),",
    "     (",
    "         'tickets',",
    "         'tickets_target_release_valid',",
    "         '92895cc5c358ab7367d595a5bbd3d2dc'",
    "     ),",
    "     (",
    "         'tickets',",
    "         'idx_tickets_to_project_target_release',",
    "         '8f47ea9b63db608804b0ffc17cb4ba54'",
    "         'indexes', 173,",
    " 'contract_id', 'brain-v42/postgresql-recovery/v17',",
    " 'schema_version', 17",
]


def _sha256(name: str) -> str:
    return hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest()


def _added_and_removed(before: Path, after: Path) -> tuple[list[str], list[str]]:
    diff = list(difflib.ndiff(before.read_text().splitlines(), after.read_text().splitlines()))
    return (
        [line[2:] for line in diff if line.startswith("+ ")],
        [line[2:] for line in diff if line.startswith("- ")],
    )


def test_previous_generations_remain_byte_identical() -> None:
    for name, expected in {**V14_ASSETS, **V15_ASSETS, **V16_ASSETS}.items():
        assert _sha256(name) == expected, name


def test_v17_assets_are_pinned() -> None:
    for name, expected in V17_ASSETS.items():
        assert _sha256(name) == expected, name


def test_v17_sql_is_v16_plus_exactly_the_measured_delta_in_both_variants() -> None:
    for before, after in ((V16_SQL, V17_SQL), (V16_PGRESTORE, V17_PGRESTORE)):
        added, removed = _added_and_removed(before, after)
        assert set(removed) == REMOVED_LINES, after.name
        assert len(removed) == len(REMOVED_LINES), after.name
        assert added == ADDED_LINES, after.name


def test_the_two_variants_differ_exactly_as_v16_s_do() -> None:
    def changed(before: Path, after: Path) -> list[str]:
        diff = difflib.unified_diff(
            before.read_text().splitlines(), after.read_text().splitlines(), n=0
        )
        return [line for line in diff if line[:1] in "+-" and not line.startswith(("+++", "---"))]

    assert changed(V16_SQL, V16_PGRESTORE) == changed(V17_SQL, V17_PGRESTORE)


def test_v17_manifest_is_v16_with_only_identity_and_index_count_moved() -> None:
    v16 = json.loads(V16_JSON.read_text())
    v17 = json.loads(V17_JSON.read_text())
    assert v17["contract_id"] == "brain-v42/postgresql-recovery/v17"
    assert v17["schema_version"] == 17
    old_counts = next(check for check in v16["checks"] if check["id"] == "catalog_counts")
    new_counts = next(check for check in v17["checks"] if check["id"] == "catalog_counts")
    assert new_counts["indexes"] == 173
    old_counts["indexes"] = 173
    v16["contract_id"] = v17["contract_id"]
    v16["schema_version"] = v17["schema_version"]
    assert v17 == v16
    assert V17_JSON.read_text() == json.dumps(v17, sort_keys=True, separators=(",", ":")) + "\n"


def test_v17_ships_no_acl_asset_because_it_grants_nothing() -> None:
    for suffix in ("-acl.sql", "-acl.json", "-acl-pgrestore.sql", "-acl-pgrestore.json"):
        assert not (RECOVERY / f"brain-v42-v17{suffix}").exists()
        assert (RECOVERY / f"brain-v42-v11{suffix}").is_file()


def test_current_binding_names_v17() -> None:
    current = json.loads((RECOVERY / "current.json").read_text())
    assert current["contract_id"] == "brain-v42/postgresql-recovery/v17"
    assert current["contract_version"] == 17
    assert current["schema_head"] == "059"
    for key, name in (
        ("attestation_sql", "brain-v42-v17.sql"),
        ("restored_attestation_sql", "brain-v42-v17-pgrestore.sql"),
        ("manifest", "brain-v42-v17.json"),
    ):
        assert current[key]["path"] == f"ops/recovery/{name}"
        assert current[key]["sha256"] == _sha256(name)
