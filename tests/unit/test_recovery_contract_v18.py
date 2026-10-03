"""Recovery contract v18 — v17 plus the 060 footprint (focus slots, 16314b31), head 060."""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path

RECOVERY = Path(__file__).resolve().parents[2] / "ops" / "recovery"

V17_ASSETS = {
    "brain-v42-v17.json": "ea96516ed62179ecf44a2ba17d55d847ad7cda08dac79eb2c7c9e923d759d33b",
    "brain-v42-v17.sql": "8195744239a27a9f7f6a3d0bc2bc32e80b3f8b54239a2f8b06cbf31bf32bb210",
    "brain-v42-v17-pgrestore.sql": "8e52fa90bc802c26b1365526f606b26d79ebf819ccd12129e3d09766b083d00b",
}
V18_ASSETS = {
    "brain-v42-v18.json": "72873b00f256f27265f131b232aa5d87fa31d65d98cfacffe295fbf8d30e74dc",
    "brain-v42-v18.sql": "581d7258f1ff41a976a5405402338d29e6feafc0da227dc26b913b6c1a645f06",
    "brain-v42-v18-pgrestore.sql": "10be9484c148bdc25b5a5ecbde78c5d9fecf6b7d3853de770942a994e7084e2d",
}
V17_SQL = RECOVERY / "brain-v42-v17.sql"
V17_PGRESTORE = RECOVERY / "brain-v42-v17-pgrestore.sql"
V17_JSON = RECOVERY / "brain-v42-v17.json"
V18_SQL = RECOVERY / "brain-v42-v18.sql"
V18_PGRESTORE = RECOVERY / "brain-v42-v18-pgrestore.sql"
V18_JSON = RECOVERY / "brain-v42-v18.json"
NEW_TABLES = ("focus_slot_anchors", "focus_slot_history", "focus_slots")
UNCHANGED_BY_DECLARATION = (
    "'project_focus_history_append_only', '1f0ebe5434ac466e2441b1adb5d00eafe69531e4181cdaa8a7eba65ec477b58c', 167",
    "'require_project_focus_history', '885808a7976000259087763763a0178f3acaca2722631302053153c4c8d806be', 499",
)


REMOVED_LINES = [
    "     ('knowledge_fact_definitions')",
    "         'd75989f65d6b2929cb4f7d9377f4d3bc'",
    "         '8734d2763c331d00bb51e415f82b9f41'",
    "         'foreign_keys', 53,",
    "         'indexes', 173,",
    "     ('brain_sessions_terminal_state_valid', 'c', NULL::text, '8734d2763c331d00bb51e415f82b9f41'),",
    "     ('uq_brain_sessions_project_client', 'u', NULL::text, '153c25b1acb665316ea262444b4d0d79')",
    "     )",
    "     )",
    "     ('brain_sessions', 'd75989f65d6b2929cb4f7d9377f4d3bc'),",
    "     ('knowledge_fact_definitions', 'knowledge_fact_definitions_append_only')",
    "     ('knowledge_fact_definitions')",
    "     ('knowledge_fact_definitions_append_only', 'db03c025d803b7d299faed702db531df47c76af2f8c3a36eb6963c9d48dda15c', 95)",
    " 'contract_id', 'brain-v42/postgresql-recovery/v17',",
    " 'schema_version', 17",
]
ADDED_LINES = [
    "     ('knowledge_fact_definitions'),",
    "     ('focus_slot_anchors'),",
    "     ('focus_slot_history'),",
    "     ('focus_slots')",
    "         '99486491a505741a44f795d6e78f27b5'",
    "     ),",
    "     (",
    "         'focus_slot_anchors',",
    "         '412b2d4c9591c76be382f8f98c9c78d7'",
    "     ),",
    "     (",
    "         'focus_slot_history',",
    "         'e6339282cc53bdda86c689e08f4f40e1'",
    "     ),",
    "     (",
    "         'focus_slots',",
    "         'b9defe0ff435d14772aec06d62dbd36d'",
    "         'ccc0365c86941b622aaf81af306c3d9d'",
    "     ),",
    "     (",
    "         'brain_sessions',",
    "         'brain_sessions_relay_requires_slot',",
    "         'd83cde5100901f44f2c0531347828fe6'",
    "     ),",
    "     (",
    "         'brain_sessions',",
    "         'brain_sessions_relayed_from_session_id_fkey',",
    "         '086baa92b7b4b73667fc9aa369dc3567'",
    "     ),",
    "     (",
    "         'brain_sessions',",
    "         'brain_sessions_slot_id_fkey',",
    "         '0595f1de1ce3a7c5e9fbb0966c47afe6'",
    "     ),",
    "     (",
    "         'brain_sessions',",
    "         'brain_sessions_slot_operator_only',",
    "         '05e77f66f7972cf224c3d235742868d2'",
    "     ),",
    "     (",
    "         'focus_slot_anchors',",
    "         'focus_slot_anchors_pkey',",
    "         'cc3552dbb61b18accca876af5296eb1f'",
    "     ),",
    "     (",
    "         'focus_slot_anchors',",
    "         'focus_slot_anchors_shape',",
    "         '44a6d67c334c1e36312b5869cf3b08f6'",
    "     ),",
    "     (",
    "         'focus_slot_anchors',",
    "         'focus_slot_anchors_slot_id_fkey',",
    "         '0595f1de1ce3a7c5e9fbb0966c47afe6'",
    "     ),",
    "     (",
    "         'focus_slot_anchors',",
    "         'focus_slot_anchors_ticket_id_fkey',",
    "         'b658aa813cdd232541fcae27c02e9eb9'",
    "     ),",
    "     (",
    "         'focus_slot_anchors',",
    "         'uq_focus_slot_anchors',",
    "         '742f9af96bd819b00c93136e7531b482'",
    "     ),",
    "     (",
    "         'focus_slot_history',",
    "         'focus_slot_history_pkey',",
    "         'd6231907d59f7d7d451ee7fee453288b'",
    "     ),",
    "     (",
    "         'focus_slot_history',",
    "         'focus_slot_history_session_id_fkey',",
    "         '33577b1d6597cae9e8a8641af65ff234'",
    "     ),",
    "     (",
    "         'focus_slot_history',",
    "         'focus_slot_history_session_valid',",
    "         '6de853ff4078f27eb3bce58db1d9805b'",
    "     ),",
    "     (",
    "         'focus_slot_history',",
    "         'focus_slot_history_slot_id_fkey',",
    "         '0595f1de1ce3a7c5e9fbb0966c47afe6'",
    "     ),",
    "     (",
    "         'focus_slot_history',",
    "         'focus_slot_history_source_valid',",
    "         '62c5bb6431f3a0e93e4d78ad4eb2e05d'",
    "     ),",
    "     (",
    "         'focus_slots',",
    "         'focus_slots_body_valid',",
    "         '2ca5b5f8d872df0810c2bb26c229dd69'",
    "     ),",
    "     (",
    "         'focus_slots',",
    "         'focus_slots_close_valid',",
    "         'f41dbfb25cf1a1aa66980952b49644ac'",
    "     ),",
    "     (",
    "         'focus_slots',",
    "         'focus_slots_history_required',",
    "         '40b04baf4eb12bc74afe359f88bfeaaa'",
    "     ),",
    "     (",
    "         'focus_slots',",
    "         'focus_slots_pkey',",
    "         'cc3552dbb61b18accca876af5296eb1f'",
    "     ),",
    "     (",
    "         'focus_slots',",
    "         'focus_slots_project_key_fkey',",
    "         'b863ba166c02670d9dad0a56f9582d59'",
    "     ),",
    "     (",
    "         'focus_slots',",
    "         'focus_slots_revision_valid',",
    "         '2f0c4ecd8bcd749b90028b77d3d33be6'",
    "     ),",
    "     (",
    "         'focus_slots',",
    "         'focus_slots_title_nonblank',",
    "         'e4d2af5d22b578d7ab9fd051021bae24'",
    "     ),",
    "     (",
    "         'brain_sessions',",
    "         'uq_brain_sessions_open_slot',",
    "         '255a6835cac6c73a524201ab544637f6'",
    "     ),",
    "     (",
    "         'brain_sessions',",
    "         'uq_brain_sessions_relayed_from',",
    "         '3679357e3203576017d8277871a5f2e2'",
    "     ),",
    "     (",
    "         'focus_slot_anchors',",
    "         'focus_slot_anchors_pkey',",
    "         'a72902c14cd58cc25ec2c3255dbcf5fd'",
    "     ),",
    "     (",
    "         'focus_slot_anchors',",
    "         'idx_focus_slot_anchors_lot',",
    "         '2aeb59050d694ad168abfee993658b3c'",
    "     ),",
    "     (",
    "         'focus_slot_anchors',",
    "         'idx_focus_slot_anchors_pr',",
    "         '06f0a50ece8d26849f790e25e6b80982'",
    "     ),",
    "     (",
    "         'focus_slot_anchors',",
    "         'idx_focus_slot_anchors_ticket',",
    "         '16c04d6fef3705b60464253bab86c46e'",
    "     ),",
    "     (",
    "         'focus_slot_anchors',",
    "         'uq_focus_slot_anchors',",
    "         'bdebb61f5ac5652a350724bfd3aebf6e'",
    "     ),",
    "     (",
    "         'focus_slot_history',",
    "         'focus_slot_history_pkey',",
    "         'a1e667372186b26881a2456fa01efe1c'",
    "     ),",
    "     (",
    "         'focus_slots',",
    "         'focus_slots_pkey',",
    "         '058d44ede6fcf121c8bdbcf2baa10da2'",
    "     ),",
    "     (",
    "         'focus_slots',",
    "         'uq_focus_slots_open_title',",
    "         '60ca0d38f40bc19440b69ec53a14d452'",
    "         'foreign_keys', 60,",
    "         'indexes', 183,",
    "     ('brain_sessions_terminal_state_valid', 'c', NULL::text, 'ccc0365c86941b622aaf81af306c3d9d'),",
    "     ('uq_brain_sessions_project_client', 'u', NULL::text, '153c25b1acb665316ea262444b4d0d79'),",
    "     ('brain_sessions_relay_requires_slot', 'c', NULL::text, 'd83cde5100901f44f2c0531347828fe6'),",
    "     ('brain_sessions_relayed_from_session_id_fkey', 'f', 'r', '086baa92b7b4b73667fc9aa369dc3567'),",
    "     ('brain_sessions_slot_id_fkey', 'f', 'r', '0595f1de1ce3a7c5e9fbb0966c47afe6'),",
    "     ('brain_sessions_slot_operator_only', 'c', NULL::text, '05e77f66f7972cf224c3d235742868d2')",
    "     ),",
    "     ('brain_sessions_terminal_state_valid', 'nature is not null')",
    "     ),",
    "     ('uq_brain_sessions_open_slot', '255a6835cac6c73a524201ab544637f6'),",
    "     ('uq_brain_sessions_relayed_from', '3679357e3203576017d8277871a5f2e2')",
    "     ('brain_sessions', '99486491a505741a44f795d6e78f27b5'),",
    "     ('knowledge_fact_definitions', 'knowledge_fact_definitions_append_only'),",
    "     ('focus_slot_anchors', 'focus_slot_anchors_append_only_trigger'),",
    "     ('focus_slot_history', 'focus_slot_history_append_only_trigger'),",
    "     ('focus_slots', 'focus_slots_history_required')",
    "     ('knowledge_fact_definitions'),",
    "     ('focus_slot_anchors'),",
    "     ('focus_slot_history'),",
    "     ('focus_slots')",
    "     ('knowledge_fact_definitions_append_only', 'db03c025d803b7d299faed702db531df47c76af2f8c3a36eb6963c9d48dda15c', 95),",
    "     ('focus_slot_anchors_append_only', 'c6553dd490c2e03b43162f64a0cf4d8966d7fe28a8f5d5ccacc6b97218587283', 144),",
    "     ('focus_slot_history_append_only', 'ff0c40383233e72e065a43222c95315dea4ba52cbe68b72232e756670dd215e9', 151),",
    "     ('require_focus_slot_history', '1c22314024b740993c4aac1fd06abfdb104bfa7fd6a450bb2a5bdf0111700520', 431)",
    " 'contract_id', 'brain-v42/postgresql-recovery/v18',",
    " 'schema_version', 18",
]
#: The twin's own canonicalisation, not `pg_restore`, makes these two differ. Alembic created
#: both CHECKs AFTER the restore, so `pg_restore` never touched them. The twin's observed query
#: rewrites `]::text[]` to `]` (`replace(..., ']::text[]', ']')`, ops/recovery/
#: brain-v42-v18-pgrestore.sql:1571) before hashing, while the base hashes the pretty-mode
#: `array[...]::text[]` (lower-cased) as the server renders it: same constraint, two fingerprints.
TWIN_RESERIALISED = {
    "         '6de853ff4078f27eb3bce58db1d9805b'": "         'b20c2943054abbe9999cd1ae17fa71ad'",
    "         '62c5bb6431f3a0e93e4d78ad4eb2e05d'": "         '659f35872b99b87b6f1fbefbdc5bff8b'",
}


def _sha256(name: str) -> str:
    return hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest()


def _added_and_removed(before: Path, after: Path) -> tuple[list[str], list[str]]:
    diff = list(difflib.ndiff(before.read_text().splitlines(), after.read_text().splitlines()))
    return (
        [line[2:] for line in diff if line.startswith("+ ")],
        [line[2:] for line in diff if line.startswith("- ")],
    )


def test_v17_assets_remain_byte_identical() -> None:
    for name, expected in V17_ASSETS.items():
        assert _sha256(name) == expected, name


def test_v18_assets_are_pinned() -> None:
    for name, expected in V18_ASSETS.items():
        assert _sha256(name) == expected, name


def test_v18_sql_is_v17_plus_exactly_the_measured_delta_in_both_variants() -> None:
    twin_added = [TWIN_RESERIALISED.get(line, line) for line in ADDED_LINES]
    for before, after, expected in (
        (V17_SQL, V18_SQL, ADDED_LINES),
        (V17_PGRESTORE, V18_PGRESTORE, twin_added),
    ):
        added, removed = _added_and_removed(before, after)
        assert removed == REMOVED_LINES, after.name
        assert added == expected, after.name


def test_project_focus_history_fingerprints_are_unchanged() -> None:
    for asset in (V18_SQL, V18_PGRESTORE):
        text = asset.read_text()
        for row in UNCHANGED_BY_DECLARATION:
            assert text.count(row) == 1, (asset.name, row)


def test_the_two_variants_differ_exactly_as_v17_s_do_plus_two_reserialised_checks() -> None:
    def changed(before: Path, after: Path) -> list[str]:
        diff = difflib.unified_diff(
            before.read_text().splitlines(), after.read_text().splitlines(), n=0
        )
        return [line for line in diff if line[:1] in "+-" and not line.startswith(("+++", "---"))]

    new_twin_differences = {
        sign + md5
        for base, twin in TWIN_RESERIALISED.items()
        for sign, md5 in (("-", base), ("+", twin))
    }
    v18_differences = [
        line for line in changed(V18_SQL, V18_PGRESTORE) if line not in new_twin_differences
    ]
    assert v18_differences == changed(V17_SQL, V17_PGRESTORE)


def test_v18_manifest_is_v17_with_identity_tables_and_counts_moved() -> None:
    v17 = json.loads(V17_JSON.read_text())
    v18 = json.loads(V18_JSON.read_text())
    assert v18["contract_id"] == "brain-v42/postgresql-recovery/v18"
    assert v18["schema_version"] == 18
    counts = next(check for check in v18["checks"] if check["id"] == "catalog_counts")
    assert (counts["foreign_keys"], counts["indexes"]) == (60, 183)
    tables = next(check for check in v18["checks"] if check["id"] == "table_set")
    old_tables = next(check for check in v17["checks"] if check["id"] == "table_set")
    assert tables["tables"] == sorted([*old_tables["tables"], *NEW_TABLES])
    old_counts = next(check for check in v17["checks"] if check["id"] == "catalog_counts")
    old_counts.update(foreign_keys=60, indexes=183)
    old_tables["tables"] = tables["tables"]
    v17["contract_id"], v17["schema_version"] = v18["contract_id"], v18["schema_version"]
    assert v18 == v17
    assert V18_JSON.read_text() == json.dumps(v18, sort_keys=True, separators=(",", ":")) + "\n"


def test_v18_ships_no_acl_asset_because_060_grants_nothing() -> None:
    for suffix in ("-acl.sql", "-acl.json", "-acl-pgrestore.sql", "-acl-pgrestore.json"):
        assert not (RECOVERY / f"brain-v42-v18{suffix}").exists()
        assert (RECOVERY / f"brain-v42-v11{suffix}").is_file()


def test_current_binding_names_v18() -> None:
    current = json.loads((RECOVERY / "current.json").read_text())
    assert current["contract_id"] == "brain-v42/postgresql-recovery/v18"
    assert current["contract_version"] == 18
    assert current["schema_head"] == "060"
    for key, name in (
        ("attestation_sql", "brain-v42-v18.sql"),
        ("restored_attestation_sql", "brain-v42-v18-pgrestore.sql"),
        ("manifest", "brain-v42-v18.json"),
    ):
        assert current[key]["path"] == f"ops/recovery/{name}"
        assert current[key]["sha256"] == _sha256(name)
