"""Recovery contract v6 — the 047/048 mint, the extension that tells the truth, ACL parity.

Why a v6: 047 (the rewritten CHECK of the `ended` branch) and 048
(`attribution_mode` + partial index) landed AFTER v5's mint — measured on
2026-08-29, the v5 receipt played against production returned 3 failures, all
consequences of migrations, none a degradation. The S1 lineage (decision
`9d22bc6a`) answers that with a mint, never with a rewrite: v5 stays frozen
byte-for-byte and describes the state it described.

Three NEW things in this mint, each measured before being written:

1. **The extension tells the truth on both sides** (ticket `2ed0d4e0`). The base
   contract pins the SOURCE's complete `extname || ' ' || extversion` inventory.
   The -pgrestore twin pins the NAMES only: the restored version is carried by the
   target IMAGE (`CREATE EXTENSION` with no VERSION clause), and a perfectly
   healthy restoration moves from 0.8.2 to 0.8.5 — measured on a bench on
   2026-08-29. Re-pinning 0.8.2 in the twin would remanufacture the false red PR 41
   documents. The twin's receipt SAYS the versions ("restored under X, origin
   0.8.2") without making a failure of it.

2. **The ACL proof gets its twin back** (ticket `ac7b3a49`). The v5 derogation was
   self-referential: `--no-owner --no-acl` existed only in the sentence justifying
   it, the runbook's disaster command does not carry them. Replayed on a bench
   (real dump, roles pre-created, command in the runbook's form): owners and GRANTs
   survive IN FULL. The only real difference: the superuser role of the target
   cluster's maintenance service — tolerated AND named by the twin, never left
   unsaid.

3. **The GRANT list is DERIVED, no longer retyped** (ticket `60708007`): the test
   below recomputes it from migrations 036/045 on every run.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
RECOVERY = ROOT / "ops" / "recovery"
VERSIONS = ROOT / "alembic" / "versions"

V6_JSON = RECOVERY / "brain-v42-v6.json"
V6_SQL = RECOVERY / "brain-v42-v6.sql"
V6_PGRESTORE = RECOVERY / "brain-v42-v6-pgrestore.sql"
V6_ACL_SQL = RECOVERY / "brain-v42-v6-acl.sql"
V6_ACL_JSON = RECOVERY / "brain-v42-v6-acl.json"
V6_ACL_PGRESTORE = RECOVERY / "brain-v42-v6-acl-pgrestore.sql"
V6_ACL_PGRESTORE_JSON = RECOVERY / "brain-v42-v6-acl-pgrestore.json"

V5_JSON = RECOVERY / "brain-v42-v5.json"

#: The v5 assets are FROZEN byte-for-byte, exactly as v5 froze v4: an attestation
#: lineage is not rewritten, each contract describes the state it described, and
#: the next one is derived from the previous one.
V5_SHA256 = {
    "brain-v42-v5.json": "f1e3a474da46ead7e8f4564caa73a8a6de1b05dbefdce363d579cdc838e14c2b",
    "brain-v42-v5.sql": "86a91b0d5c5ac26ba2f8f8af9a02e39110733230744b3ddce15fe69eda6da4bb",
    "brain-v42-v5-pgrestore.sql": (
        "b70d2038d3b84ebe04209c65e258d37cc9f21b2f8b0a4b9159c8a84c77b4f7fb"
    ),
    "brain-v42-v5-acl.sql": "2548b8dba9df56700203dab3820c7c02d89ec41ae26eec5e491c5946920d64ef",
    "brain-v42-v5-acl.json": "5ca690f7bc703ff0ddaeade4ed5f0d5cd70805bbf631b512eacef3fbd132f1f2",
}


def test_v5_recovery_assets_remain_byte_identical() -> None:
    for name, expected in V5_SHA256.items():
        digest = hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest()
        assert digest == expected, f"{name} a été réécrit — la lignée interdit ça"


def test_v6_json_is_the_exact_v5_delta() -> None:
    """v6 is v5's DELTA, derived — never retyped (the lineage's discipline)."""
    document = json.loads(V5_JSON.read_text(encoding="utf-8"))
    document["contract_id"] = "brain-v42/postgresql-recovery/v6"
    document["schema_version"] = 6
    by_id = {check["id"]: check for check in document["checks"]}

    # 048: the partial attribution index enters the catalogue.
    by_id["catalog_counts"]["indexes"] = 131

    # 2ed0d4e0: the extension version stops being a vector-only scalar.
    extension = by_id["extension_vector"]
    extension.clear()
    extension.update(
        {
            "id": "extension_versions",
            "kind": "extension_inventory",
            "inventory": "plpgsql 1.0, vector 0.8.2",
            "restore_rule": "names-only",
        }
    )
    document["checks"].sort(key=lambda check: check["id"])

    expected = json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    assert V6_JSON.read_text(encoding="utf-8") == expected


def test_the_v6_identities_are_exact() -> None:
    assert "'brain-v42/postgresql-recovery/v6'" in V6_SQL.read_text(encoding="utf-8")
    assert "'brain-v42/postgresql-recovery/v6'" in V6_PGRESTORE.read_text(encoding="utf-8")
    assert "'brain-v42/postgresql-recovery/v6-acl'" in V6_ACL_SQL.read_text(encoding="utf-8")
    assert "'brain-v42/postgresql-recovery/v6-acl-pgrestore'" in V6_ACL_PGRESTORE.read_text(
        encoding="utf-8"
    )
    for asset in (V6_SQL, V6_PGRESTORE, V6_ACL_SQL, V6_ACL_PGRESTORE):
        assert "'schema_version', 6" in asset.read_text(encoding="utf-8"), asset.name


def test_the_two_v6_variants_keep_their_exact_cte_parity() -> None:
    """The allowed difference stays CLOSED: exactly two CTEs, not "about two"."""

    def cte_names(path: Path) -> set[str]:
        return set(
            re.findall(
                r"^([a-z_][a-z0-9_]*)\s*(?:\([^)]*\))?\s*AS \(",
                path.read_text(encoding="utf-8"),
                re.M,
            )
        )

    live = cte_names(V6_SQL)
    pgrestore = cte_names(V6_PGRESTORE)

    assert pgrestore - live == {"observed_artifact_constraints", "observed_session_constraints"}
    assert not (live - pgrestore)


def test_the_v6_assets_are_regular_non_executable_files() -> None:
    for asset in (
        V6_JSON,
        V6_SQL,
        V6_PGRESTORE,
        V6_ACL_SQL,
        V6_ACL_JSON,
        V6_ACL_PGRESTORE,
        V6_ACL_PGRESTORE_JSON,
    ):
        assert asset.is_file(), asset.name
        assert not asset.stat().st_mode & 0o111, f"{asset.name} est exécutable"


def test_the_v6_contracts_read_and_never_write() -> None:
    """An attestation that writes is no longer an attestation."""
    forbidden = re.compile(
        r"^\s*(INSERT|UPDATE|DELETE|TRUNCATE|DROP|CREATE|ALTER|GRANT|REVOKE|COPY|CALL|DO)\b",
        re.IGNORECASE | re.M,
    )
    allowed = re.compile(r"^\s*(WITH|SELECT)\b", re.IGNORECASE | re.M)

    for asset, floor in (
        (V6_SQL, 100),
        (V6_PGRESTORE, 100),
        (V6_ACL_SQL, 5),
        (V6_ACL_PGRESTORE, 5),
    ):
        text = asset.read_text(encoding="utf-8")
        assert not forbidden.findall(text), asset.name
        assert len(allowed.findall(text)) > floor, asset.name


def test_v6_carries_the_047_and_048_mechanisms() -> None:
    """The mint encodes WHAT the migrations changed, and nothing else.

    047: the "empty ledger XOR nothing_to_capture_reason" XOR of the `ended` branch
    is DESTROYED — the fragment pinning it goes out, replaced by the new shape (an
    optional but never blank reason). 048: the `attribution_mode` column, its CHECK
    constraint and the partial index of DERIVED modes enter both variants.
    """
    for asset in (V6_SQL, V6_PGRESTORE):
        text = asset.read_text(encoding="utf-8")
        assert (
            "'cardinality(captured_knowledge_ids) = 0 and nothing_to_capture_reason is not null'"
            not in text
        ), f"{asset.name} épingle encore le XOR que la 047 a détruit"
        assert (
            "'cardinality(captured_knowledge_ids) > 0 and nothing_to_capture_reason is null'"
            not in text
        ), f"{asset.name} épingle encore le XOR que la 047 a détruit"
        assert "nothing_to_capture_reason is null or btrim(nothing_to_capture_reason)" in text
        assert "brain_session_artifacts_attribution_mode_valid" in text, asset.name
        assert "idx_brain_session_artifacts_derived_window" in text, asset.name
        assert "('attribution_mode', 5, 'character varying', 'YES', 24, NULL::text)" in text
        assert "'indexes', 131," in text, asset.name


def test_the_extension_check_tells_the_truth_on_both_sides() -> None:
    """The shape that tells the truth — and does not remanufacture the false red.

    Base: the SOURCE's complete inventory is pinned; a version that moves in
    production without a re-mint must redden. Twin: only the PRESENCE of the
    extensions is required — the version is carried by the target image and a
    healthy restoration changes it (0.8.2 → 0.8.5 measured); the receipt SAYS it
    (observed `inventory`) without making a failure of it.
    """
    base = V6_SQL.read_text(encoding="utf-8")
    twin = V6_PGRESTORE.read_text(encoding="utf-8")

    assert "extname || ' ' || extversion" in base
    assert "extname || ' ' || extversion" in twin
    assert "extension_observation.inventory = 'plpgsql 1.0, vector 0.8.2'" in base

    # The twin NEVER compares a version to decide pass/fail.
    assert "extension_observation.inventory = " not in twin
    assert "= '0.8.2'" not in twin
    assert 'extension_observation.names = \'["plpgsql", "vector"]\'::jsonb' in twin
    # But it SHOWS it: the divergence is visible, never silent.
    assert "'inventory', extension_observation.inventory" in twin


def test_the_acl_grant_list_is_derived_from_the_migrations() -> None:
    """Ticket 60708007: derive from the 036/045 GRANTs, do not copy.

    036 lays down the ten GRANTs; 045 re-lays `codex_dream_run_v1`'s after the
    view's DROP/CREATE (a DROP VIEW takes its rights away). This test recomputes
    the list from the migrations on every run: if a future migration adds a codex
    view, the ACL asset MUST be re-minted or this test reddens — the list can no
    longer drift in silence.
    """
    granted: set[str] = set()
    for migration in ("036_codex_contract_views.py", "045_dream_run_model_width.py"):
        source = (VERSIONS / migration).read_text(encoding="utf-8")
        granted |= set(re.findall(r"GRANT SELECT ON (\w+) TO codex_ro", source))

    for asset in (V6_ACL_SQL, V6_ACL_PGRESTORE):
        pinned = set(
            re.findall(r"\('(\w+)', 'codex_ro', 'SELECT'\)", asset.read_text(encoding="utf-8"))
        )
        assert pinned == granted, (
            f"{asset.name} : la liste des GRANT diverge des migrations — "
            f"manquants={sorted(granted - pinned)} en-trop={sorted(pinned - granted)}"
        )


def test_the_acl_twin_tolerates_only_named_superuser_maintenance_roles() -> None:
    """Parity comes back with ONE stated difference, never an unsaid one (ac7b3a49).

    The restoration cluster carries its maintenance service's superuser (measured:
    `postgres` on the 2026-08-29 bench). The twin tolerates it — AND names it in
    the receipt. An unexpected non-superuser role stays a failure. The base asset,
    for its part, keeps v5's STRICT census: in production, a supernumerary role —
    even a superuser — is a drift.
    """
    twin = V6_ACL_PGRESTORE.read_text(encoding="utf-8")
    base = V6_ACL_SQL.read_text(encoding="utf-8")

    assert "AND NOT role_record.rolsuper" in twin
    assert "'tolerated_superuser_roles'" in twin
    assert "AND NOT role_record.rolsuper" not in base
    assert "'tolerated_superuser_roles'" not in base


def test_the_base_acl_is_v5_verbatim_but_for_its_identity() -> None:
    """No opportunistic rewrite: v6-acl = v5-acl up to its identity."""
    v5 = (RECOVERY / "brain-v42-v5-acl.sql").read_text(encoding="utf-8")
    v6 = V6_ACL_SQL.read_text(encoding="utf-8")

    normalized = v6.replace(
        "'brain-v42/postgresql-recovery/v6-acl'", "'brain-v42/postgresql-recovery/v5-acl'"
    ).replace("'schema_version', 6", "'schema_version', 5")
    assert normalized == v5
