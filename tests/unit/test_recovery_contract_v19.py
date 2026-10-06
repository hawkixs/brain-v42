"""Recovery contract v19 — v18 plus the 061 footprint (`brain_session_connections`), head 061."""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path

RECOVERY = Path(__file__).resolve().parents[2] / "ops" / "recovery"

V18_ASSETS = {
    "brain-v42-v18.json": "72873b00f256f27265f131b232aa5d87fa31d65d98cfacffe295fbf8d30e74dc",
    "brain-v42-v18.sql": "581d7258f1ff41a976a5405402338d29e6feafc0da227dc26b913b6c1a645f06",
    "brain-v42-v18-pgrestore.sql": "10be9484c148bdc25b5a5ecbde78c5d9fecf6b7d3853de770942a994e7084e2d",
}
V19_ASSETS = {
    "brain-v42-v19.json": "8c5d510fde49d402199d797937c3fd815d930c8d41a24e21009cf5524fe41e5f",
    "brain-v42-v19.sql": "0a47cd0f50af94728ad503ab61fd62775ebbbe78e6b9a6749c8764fb036c6088",
    "brain-v42-v19-pgrestore.sql": "58162494975b704d81e2519f84e176bcbd25e0aa9c0690d039b8de27757e0940",
}
V18_SQL = RECOVERY / "brain-v42-v18.sql"
V18_PGRESTORE = RECOVERY / "brain-v42-v18-pgrestore.sql"
V18_JSON = RECOVERY / "brain-v42-v18.json"
V19_SQL = RECOVERY / "brain-v42-v19.sql"
V19_PGRESTORE = RECOVERY / "brain-v42-v19-pgrestore.sql"
V19_JSON = RECOVERY / "brain-v42-v19.json"
NEW_TABLES = ("brain_session_connections",)

REMOVED_LINES = [
    "     ('focus_slots')",
    "         'foreign_keys', 60,",
    "         'indexes', 183,",
    " 'contract_id', 'brain-v42/postgresql-recovery/v18',",
    " 'schema_version', 18",
]
ADDED_LINES = [
    "     ('focus_slots'),",
    "     ('brain_session_connections')",
    "     ),",
    "     (",
    "         'brain_session_connections',",
    "         '93af13b1fd8dfedecb03ad6ca1b79ca1'",
    "     ),",
    "     (",
    "         'brain_session_connections',",
    "         'brain_session_connections_connection_nonblank',",
    "         '876e1a5d63c5c810192604f777e19081'",
    "     ),",
    "     (",
    "         'brain_session_connections',",
    "         'brain_session_connections_pkey',",
    "         '148f87bba7fdd5dd4c70ec153c70818e'",
    "     ),",
    "     (",
    "         'brain_session_connections',",
    "         'brain_session_connections_session_id_fkey',",
    "         'cf936a6262f2e34cd4e237e75d156d48'",
    "     ),",
    "     (",
    "         'brain_session_connections',",
    "         'brain_session_connections_pkey',",
    "         '812cfeb68627be0a68d0446a34066dad'",
    "         'foreign_keys', 61,",
    "         'indexes', 184,",
    " 'contract_id', 'brain-v42/postgresql-recovery/v19',",
    " 'schema_version', 19",
]


def _sha256(name: str) -> str:
    return hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest()


def _added_and_removed(before: Path, after: Path) -> tuple[list[str], list[str]]:
    diff = list(difflib.ndiff(before.read_text().splitlines(), after.read_text().splitlines()))
    return (
        [line[2:] for line in diff if line.startswith("+ ")],
        [line[2:] for line in diff if line.startswith("- ")],
    )


def test_v18_assets_remain_byte_identical() -> None:
    for name, expected in V18_ASSETS.items():
        assert _sha256(name) == expected, name


def test_v19_assets_are_pinned() -> None:
    for name, expected in V19_ASSETS.items():
        assert _sha256(name) == expected, name


def test_v19_sql_is_v18_plus_exactly_the_measured_delta_in_both_variants() -> None:
    # 061 adds one table and rewrites no row: the two variants take the same delta
    # (the new CHECK and the primary key carry no array cast to re-serialise).
    for before, after in ((V18_SQL, V19_SQL), (V18_PGRESTORE, V19_PGRESTORE)):
        added, removed = _added_and_removed(before, after)
        assert removed == REMOVED_LINES, after.name
        assert added == ADDED_LINES, after.name


def test_v19_adds_no_trigger_function_and_no_brain_sessions_row() -> None:
    for before, after in ((V18_SQL, V19_SQL), (V18_PGRESTORE, V19_PGRESTORE)):
        added, _ = _added_and_removed(before, after)
        assert not any("brain_sessions" in line for line in added), after.name
        assert not any("append_only" in line or "trigger" in line for line in added), after.name


def test_the_two_variants_differ_exactly_as_v18_s_do() -> None:
    def changed(before: Path, after: Path) -> list[str]:
        diff = difflib.unified_diff(
            before.read_text().splitlines(), after.read_text().splitlines(), n=0
        )
        return [line for line in diff if line[:1] in "+-" and not line.startswith(("+++", "---"))]

    assert changed(V19_SQL, V19_PGRESTORE) == changed(V18_SQL, V18_PGRESTORE)


def test_v19_manifest_is_v18_with_identity_tables_and_counts_moved() -> None:
    v18 = json.loads(V18_JSON.read_text())
    v19 = json.loads(V19_JSON.read_text())
    assert v19["contract_id"] == "brain-v42/postgresql-recovery/v19"
    assert v19["schema_version"] == 19
    counts = next(check for check in v19["checks"] if check["id"] == "catalog_counts")
    assert (counts["foreign_keys"], counts["indexes"]) == (61, 184)
    tables = next(check for check in v19["checks"] if check["id"] == "table_set")
    old_tables = next(check for check in v18["checks"] if check["id"] == "table_set")
    assert tables["tables"] == sorted([*old_tables["tables"], *NEW_TABLES])
    old_counts = next(check for check in v18["checks"] if check["id"] == "catalog_counts")
    old_counts.update(foreign_keys=61, indexes=184)
    old_tables["tables"] = tables["tables"]
    v18["contract_id"], v18["schema_version"] = v19["contract_id"], v19["schema_version"]
    assert v19 == v18
    assert V19_JSON.read_text() == json.dumps(v19, sort_keys=True, separators=(",", ":")) + "\n"


def test_v19_ships_no_acl_asset_because_061_grants_nothing() -> None:
    for suffix in ("-acl.sql", "-acl.json", "-acl-pgrestore.sql", "-acl-pgrestore.json"):
        assert not (RECOVERY / f"brain-v42-v19{suffix}").exists()
        assert (RECOVERY / f"brain-v42-v11{suffix}").is_file()
