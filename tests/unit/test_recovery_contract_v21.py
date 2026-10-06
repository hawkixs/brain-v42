"""Recovery contract v21 — v20 plus the 063 footprint, head 063.

063 adds four tables, four trigger functions with their five triggers, one sequence, one
foreign key and a nullable column plus a format CHECK on `brain_sessions` and on
`brain_session_connections`. It installs no extension and grants nothing in `public`.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path

RECOVERY = Path(__file__).resolve().parents[2] / "ops" / "recovery"

V20_ASSETS = {
    "brain-v42-v20.json": "a0736e760d02777ae081ccfd726bb56325b8ee1b5e7f387cb39c62368e539518",
    "brain-v42-v20.sql": "0229a57125021a51a20d5cde59b223abfb3e57c51cbc1a9e0d9ada457fefe976",
    "brain-v42-v20-pgrestore.sql": "f3e0198c341b27b3e76fbe0c8046bab3c29b019a9f3286e4092e7c23dda08d66",
}
V21_ASSETS = {
    "brain-v42-v21.json": "4c7c4523a0e4c47d44962ca865cd8e84c2c14c6251a90cc84049a39ed2ceae8b",
    "brain-v42-v21.sql": "47d0aa50afadd3a946362b860aee4cd575fc342f60ad05399a8b2241766720f0",
    "brain-v42-v21-pgrestore.sql": "baac61ab1a2148d8d373b3afd78553046430fc098da9e92868d00f577a656a16",
}
VARIANTS = ("brain-v42-v21.sql", "brain-v42-v21-pgrestore.sql")

NEW_TABLES = [
    "brain_admin_elevations",
    "brain_client_credentials",
    "brain_credential_audit",
    "brain_schema_compat",
]
NEW_TRIGGER_FUNCTIONS = [
    "brain_client_credentials_notify",
    "brain_credential_audit_notify",
    "brain_session_connections_owner_check",
    "brain_sessions_opener_immutable",
]
NEW_TRIGGERS = [
    ("brain_client_credentials", "brain_client_credentials_notify"),
    ("brain_client_credentials", "brain_client_credentials_notify_update"),
    ("brain_credential_audit", "brain_credential_audit_notify"),
    ("brain_session_connections", "brain_session_connections_owner_check"),
    ("brain_sessions", "brain_sessions_opener_immutable"),
]


def _sha256(name: str) -> str:
    return hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest()


def _manifest(version: int) -> dict:
    return json.loads((RECOVERY / f"brain-v42-v{version}.json").read_text())


def test_v20_assets_remain_byte_identical() -> None:
    for name, expected in V20_ASSETS.items():
        assert _sha256(name) == expected, name


def test_v21_assets_are_pinned() -> None:
    for name, expected in V21_ASSETS.items():
        assert _sha256(name) == expected, name


def test_v21_identity_and_counts_move_in_both_variants() -> None:
    for name in VARIANTS:
        text = (RECOVERY / name).read_text()
        assert " 'contract_id', 'brain-v42/postgresql-recovery/v21'," in text, name
        assert " 'schema_version', 21" in text, name
        assert "         'foreign_keys', 62," in text, name
        assert "         'indexes', 195," in text, name
        assert "postgresql-recovery/v20" not in text, name


def test_v21_names_the_063_footprint_in_both_variants() -> None:
    for name in VARIANTS:
        text = (RECOVERY / name).read_text()
        for table in NEW_TABLES:
            assert f"     ('{table}')" in text, (name, table)
        for function in NEW_TRIGGER_FUNCTIONS:
            assert f"     ('{function}', '" in text, (name, function)
        for table, trigger in NEW_TRIGGERS:
            assert f"     ('{table}', '{trigger}')" in text, (name, trigger)
        assert "('brain_credential_audit_id_seq', 'brain_credential_audit', 'id'," in text, name
        assert "'brain_sessions_opener_client_id_format'" in text, name
        assert "'brain_session_connections_client_id_format'" in text, name


#: The ONLY v20 lines absent from v21, in both variants: the last row of seven blocks
#: (it gains a trailing comma when a row is appended), the three column fingerprints
#: 063 rewrites (`brain_sessions` twice, `brain_session_connections` once), the two
#: moved catalog counts and the identity.
V20_LINES_DROPPED = sorted(
    [
        "     ('brain_session_connections')",
        "     ('brain_sessions_slot_operator_only', 'c', NULL::text, "
        "'05e77f66f7972cf224c3d235742868d2')",
        "     ('focus_slots', 'focus_slots_history_required')",
        "     ('focus_slots')",
        "     ('knowledge_claims_seq_seq', 'knowledge_claims', 'seq', 'bigint', 1, 1, "
        "9223372036854775807, 1, FALSE)",
        "     ('require_focus_slot_history', "
        "'1c22314024b740993c4aac1fd06abfdb104bfa7fd6a450bb2a5bdf0111700520', 431)",
        "         '99486491a505741a44f795d6e78f27b5'",
        "     ('brain_sessions', '99486491a505741a44f795d6e78f27b5'),",
        "         '93af13b1fd8dfedecb03ad6ca1b79ca1'",
        "         'foreign_keys', 61,",
        "         'indexes', 186,",
        " 'contract_id', 'brain-v42/postgresql-recovery/v20',",
        " 'schema_version', 20",
    ]
)


def test_v21_keeps_every_v20_line_but_the_declared_ones() -> None:
    for variant in VARIANTS:
        before = (RECOVERY / variant.replace("v21", "v20")).read_text().splitlines()
        after = (RECOVERY / variant).read_text().splitlines()
        removed = [line[2:] for line in difflib.ndiff(before, after) if line.startswith("- ")]
        # Tolerate the comma the appended-to rows gained, and nothing else.
        assert sorted(line.rstrip(",") for line in removed) == sorted(
            line.rstrip(",") for line in V20_LINES_DROPPED
        ), variant


def test_v21_manifest_is_v20_with_identity_counts_and_tables_moved() -> None:
    v20, v21 = _manifest(20), _manifest(21)
    counts = next(check for check in v21["checks"] if check["id"] == "catalog_counts")
    assert (counts["foreign_keys"], counts["indexes"]) == (62, 195)
    tables = next(check for check in v21["checks"] if check["id"] == "table_set")
    assert tables["tables"] == sorted(
        [*next(c for c in v20["checks"] if c["id"] == "table_set")["tables"], *NEW_TABLES]
    )
    old_counts = next(check for check in v20["checks"] if check["id"] == "catalog_counts")
    old_counts.update(foreign_keys=62, indexes=195)
    next(c for c in v20["checks"] if c["id"] == "table_set")["tables"] = tables["tables"]
    v20["contract_id"], v20["schema_version"] = (
        "brain-v42/postgresql-recovery/v21",
        21,
    )
    assert v21 == v20
    assert (RECOVERY / "brain-v42-v21.json").read_text() == (
        json.dumps(v21, sort_keys=True, separators=(",", ":")) + "\n"
    )


def test_v21_ships_no_acl_asset_because_063_grants_nothing() -> None:
    for suffix in ("-acl.sql", "-acl.json", "-acl-pgrestore.sql", "-acl-pgrestore.json"):
        assert not (RECOVERY / f"brain-v42-v21{suffix}").exists()
        assert (RECOVERY / f"brain-v42-v11{suffix}").is_file()


def test_current_binding_names_v21() -> None:
    current = json.loads((RECOVERY / "current.json").read_text())
    assert current["contract_id"] == "brain-v42/postgresql-recovery/v21"
    assert current["contract_version"] == 21
    assert current["schema_head"] == "063"
    for key, name in (
        ("attestation_sql", "brain-v42-v21.sql"),
        ("restored_attestation_sql", "brain-v42-v21-pgrestore.sql"),
        ("manifest", "brain-v42-v21.json"),
    ):
        assert current[key]["path"] == f"ops/recovery/{name}"
        assert current[key]["sha256"] == _sha256(name)
