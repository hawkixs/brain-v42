"""Contrat de récupération v6 — le mint 047/048, l'extension qui dit vrai, la parité ACL.

Pourquoi un v6 : la 047 (le CHECK de la branche `ended` réécrit) et la 048
(`attribution_mode` + index partiel) ont atterri APRÈS le mint de v5 — mesuré le
2026-08-29, le reçu v5 joué contre la production rendait 3 échecs, tous
conséquences de migrations, aucun une dégradation. La lignée S1 (décision
`9d22bc6a`) répond à ça par un mint, jamais par une réécriture : v5 reste gelé
octet pour octet et décrit l'état qu'il décrivait.

Trois choses NEUVES dans ce mint, chacune mesurée avant d'être écrite :

1. **L'extension dit vrai des deux côtés** (ticket `2ed0d4e0`). Le contrat de
   base épingle l'inventaire complet `extname || ' ' || extversion` de la
   SOURCE. Le jumeau -pgrestore n'épingle que les NOMS : la version restaurée
   est portée par l'IMAGE cible (`CREATE EXTENSION` sans clause VERSION), et une
   restauration parfaitement saine passe de 0.8.2 à 0.8.5 — mesuré sur banc le
   2026-08-29. Re-épingler 0.8.2 dans le jumeau refabriquerait le faux rouge
   que la PR 41 documente. Le reçu du jumeau DIT les versions (« restauré sous
   X, origine 0.8.2 ») sans en faire un échec.

2. **La preuve ACL retrouve son jumeau** (ticket `ac7b3a49`). La dérogation v5
   était auto-référentielle : `--no-owner --no-acl` n'existait que dans la
   phrase qui la justifiait, la commande de sinistre du runbook ne les porte
   pas. Rejouée sur banc (dump réel, rôles pré-créés, commande au format du
   runbook) : propriétaires et GRANTs survivent INTÉGRALEMENT. Seule différence
   vraie : le rôle superuser du service de maintenance du cluster cible —
   toléré ET nommé par le jumeau, jamais tu.

3. **La liste des GRANTs est DÉRIVÉE, plus retapée** (ticket `60708007`) : le
   test ci-dessous la recalcule depuis les migrations 036/045 à chaque run.
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

#: Les actifs v5 sont GELÉS à l'octet, exactement comme v5 gelait v4 : une
#: lignée d'attestation ne se réécrit pas, chaque contrat décrit l'état qu'il
#: décrivait, et le suivant se dérive du précédent.
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
    """v6 est le DELTA de v5, dérivé — jamais retapé (discipline de la lignée)."""
    document = json.loads(V5_JSON.read_text(encoding="utf-8"))
    document["contract_id"] = "brain-v42/postgresql-recovery/v6"
    document["schema_version"] = 6
    by_id = {check["id"]: check for check in document["checks"]}

    # 048 : l'index partiel d'attribution entre au catalogue.
    by_id["catalog_counts"]["indexes"] = 131

    # 2ed0d4e0 : la version d'extension cesse d'être un scalaire vector-only.
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
    """L'écart autorisé reste CLOS : exactement deux CTE, pas « environ deux »."""

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
    """Une attestation qui écrit n'est plus une attestation."""
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
    """Le mint encode CE QUE les migrations ont changé, pas autre chose.

    047 : le XOR « ledger vide XOR nothing_to_capture_reason » de la branche
    `ended` est DÉTRUIT — le fragment qui l'épinglait sort, remplacé par la
    forme nouvelle (raison optionnelle mais jamais blanche). 048 : la colonne
    `attribution_mode`, sa contrainte CHECK et l'index partiel des modes
    DÉDUITS entrent dans les deux variantes.
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
    """La forme qui dit vrai — et qui ne refabrique pas le faux rouge.

    Base : l'inventaire complet de la SOURCE est épinglé ; une version qui
    bouge en production sans re-mint doit rougir. Jumeau : seule la PRÉSENCE
    des extensions est exigée — la version est portée par l'image cible et
    une restauration saine la change (0.8.2 → 0.8.5 mesuré) ; le reçu la DIT
    (`inventory` observé) sans en faire un échec.
    """
    base = V6_SQL.read_text(encoding="utf-8")
    twin = V6_PGRESTORE.read_text(encoding="utf-8")

    assert "extname || ' ' || extversion" in base
    assert "extname || ' ' || extversion" in twin
    assert "extension_observation.inventory = 'plpgsql 1.0, vector 0.8.2'" in base

    # Le jumeau ne compare JAMAIS une version pour décider pass/fail.
    assert "extension_observation.inventory = " not in twin
    assert "= '0.8.2'" not in twin
    assert 'extension_observation.names = \'["plpgsql", "vector"]\'::jsonb' in twin
    # Mais il la MONTRE : la divergence est visible, jamais silencieuse.
    assert "'inventory', extension_observation.inventory" in twin


def test_the_acl_grant_list_is_derived_from_the_migrations() -> None:
    """Ticket 60708007 : dériver des GRANT 036/045, ne pas recopier.

    La 036 pose les dix GRANT ; la 045 re-pose celui de `codex_dream_run_v1`
    après le DROP/CREATE de la vue (un DROP VIEW emporte ses droits). Ce test
    recalcule la liste depuis les migrations à chaque run : si une migration
    future ajoute une vue codex, l'actif ACL DOIT être re-minté ou ce test
    rougit — la liste ne peut plus dériver en silence.
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
    """La parité revient avec UNE différence dite, jamais tue (ac7b3a49).

    Le cluster de restauration porte le superuser de son service de
    maintenance (mesuré : `postgres` sur le banc du 2026-08-29). Le jumeau le
    tolère — ET le nomme dans le reçu. Un rôle non-superuser inattendu reste
    un échec. L'actif de base, lui, garde le recensement STRICT de v5 : en
    production, un rôle surnuméraire — même superuser — est une dérive.
    """
    twin = V6_ACL_PGRESTORE.read_text(encoding="utf-8")
    base = V6_ACL_SQL.read_text(encoding="utf-8")

    assert "AND NOT role_record.rolsuper" in twin
    assert "'tolerated_superuser_roles'" in twin
    assert "AND NOT role_record.rolsuper" not in base
    assert "'tolerated_superuser_roles'" not in base


def test_the_base_acl_is_v5_verbatim_but_for_its_identity() -> None:
    """Aucune réécriture d'opportunité : v6-acl = v5-acl à l'identité près."""
    v5 = (RECOVERY / "brain-v42-v5-acl.sql").read_text(encoding="utf-8")
    v6 = V6_ACL_SQL.read_text(encoding="utf-8")

    normalized = v6.replace(
        "'brain-v42/postgresql-recovery/v6-acl'", "'brain-v42/postgresql-recovery/v5-acl'"
    ).replace("'schema_version', 6", "'schema_version', 5")
    assert normalized == v5
