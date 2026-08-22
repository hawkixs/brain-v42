"""Static authority checks for the DERIVED-head recovery contract (v5).

Ce contrat applique la signature **S1** (décision `9d22bc6a`) : l'attestation
prouve la **FORME** du schéma, et le contrôle de révision devient « une seule
tête appliquée » au lieu de « la tête vaut N ». Précédent maison :
`test_alembic_env.py` — *« Le head est DÉRIVÉ, pas épinglé : l'invariant est une
seule tête, pas la tête vaut N. »*

**Ce que ça change, et pourquoi c'est le point entier.** Le contrat v4 épinglait
`alembic_head = '039'`. Toute tête postérieure le faisait donc échouer *pour la
seule raison qu'une migration avait atterri*, c'est-à-dire dans le cas nominal.
Mesuré le 2026-08-20, avant la 046 : le reçu vivant rendait **22/25**, et les
trois échecs étaient tous des conséquences de migrations postérieures au mint de
v4 — jamais une dégradation. Un contrat qui vire au rouge à chaque bascule
n'apprend plus rien à personne : c'est la définition d'une alarme qu'on cesse de
lire.

**La révision exacte n'est pas perdue pour autant** : elle reste prouvée, côté
code, par `_REQUIRED_ALEMBIC_HEAD` et son test de pin — fail-closed, et couplé au
couloir. L'attestation cesse de dupliquer une preuve qui vit ailleurs.

Le reçu **rend toujours la valeur observée** : il dit quelle tête porte la base,
il cesse seulement d'exiger laquelle.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).parents[2]
RECOVERY = ROOT / "ops" / "recovery"
V4_JSON = RECOVERY / "brain-v42-v4.json"
V5_JSON = RECOVERY / "brain-v42-v5.json"
V5_SQL = RECOVERY / "brain-v42-v5.sql"
V5_PGRESTORE = RECOVERY / "brain-v42-v5-pgrestore.sql"

#: Les actifs v4 sont GELÉS à l'octet par ce fichier, exactement comme v4 gelait
#: v3. Une lignée d'attestation ne se réécrit pas : chaque contrat décrit l'état
#: qu'il décrivait, et le prochain se dérive du précédent.
V4_SHA256 = {
    "brain-v42-v4.json": "e1ab0520e4e55b69985eefe865ea3e163a562280194a42578eeb788ef0f73e38",
    "brain-v42-v4.sql": "c4dd8293866c1e77e9e8cdf22149886d8b03da99e7e75c8164ec2d8654628c99",
    "brain-v42-v4-pgrestore.sql": (
        "3b1f228e8aa94f4967737b474710beacba1b876a2f7f3b4ba6c2e21bfc6c2335"
    ),
}

#: Empreintes MESURÉES le 2026-08-21 contre la production à la tête `046`, avec
#: les expressions littérales du contrat, et vérifiées identiques sur
#: `brain_test`. Les deux variantes ont des valeurs DIFFÉRENTES pour les mêmes
#: contraintes : `pgrestore` normalise en plus `::character varying::text` et
#: `]::text[]`. Confondre les deux jeux produit un contrat qui échoue sans dire
#: pourquoi.
SESSION_CONSTRAINT_MD5 = {
    "live": {
        "brain_sessions_status_valid": "f5065acef0a32bfc97e66f6d802b9585",
        "brain_sessions_terminal_state_valid": "aab51404804e113ec2c452ba0bc21aa8",
        "brain_sessions_nature_valid": "b3899128eb71e5e3023e994b0f1e26db",
    },
    "pgrestore": {
        "brain_sessions_status_valid": "586d25dcdade2c6c4aea9b415a19f7c5",
        "brain_sessions_terminal_state_valid": "aab51404804e113ec2c452ba0bc21aa8",
        "brain_sessions_nature_valid": "9f0ef14672aa448ce2be6e15fa7c4dd4",
    },
}

CONNECTION_INDEX_MD5 = "62b298d247237eddf60cb4ba28693af4"
SESSIONS_COLUMN_MD5 = "d75989f65d6b2929cb4f7d9377f4d3bc"
DREAM_RUN_VIEW_MD5 = "7eb14c21fea0ec4f95f09a5c03d3996d"


def _expected_v5() -> dict[str, Any]:
    """Le v5 est le DELTA de v4, dérivé — jamais retapé.

    Même discipline que `_expected_v4()`, qui dérivait de v3 : recopier le
    document entier autoriserait une divergence silencieuse entre deux contrats
    censés décrire la même base à une migration près.
    """
    document = cast(dict[str, Any], copy.deepcopy(json.loads(V4_JSON.read_text(encoding="utf-8"))))
    checks = document["checks"]
    assert isinstance(checks, list)
    by_id = {check["id"]: check for check in checks}

    head = by_id["alembic_head"]
    head.clear()
    # La clé `revision` DISPARAÎT. La laisser à une valeur quelconque ferait
    # croire qu'elle est encore lue — un contrat ne doit pas porter de champ mort.
    head.update({"id": "alembic_head", "kind": "alembic_head_single"})

    by_id["catalog_counts"]["indexes"] = 130

    # `81c4f366` : les contraintes HÉRITÉES (033/034/035 et `projects`) n'étaient
    # attestées que par NOM. La check row propre — plutôt qu'une fusion dans
    # `brain_runtime_032_036_037` — est ce qui rend le durcissement VISIBLE au
    # reçu : le dénominateur passe à 26. Fondu, il aurait vérifié plus en
    # rendant toujours 25/25, c'est-à-dire sans que personne puisse le constater.
    checks.append(
        {
            "id": "inherited_constraint_definitions",
            "kind": "brain_schema_invariant",
            "name": "inherited_constraint_definitions",
        }
    )

    document["checks"] = sorted(checks, key=lambda check: check["id"])
    document["contract_id"] = "brain-v42/postgresql-recovery/v5"
    document["schema_version"] = 5
    return document


def test_v4_recovery_assets_remain_byte_identical() -> None:
    assert {
        name: hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest() for name in V4_SHA256
    } == V4_SHA256


def test_v5_json_is_the_exact_v4_delta() -> None:
    raw = V5_JSON.read_bytes()
    document = json.loads(raw)

    assert document == _expected_v5()
    assert (
        raw
        == (
            json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        ).encode()
    )
    assert len(document["checks"]) == 26


def test_the_head_check_is_derived_and_carries_no_revision() -> None:
    """S1, énoncé des deux côtés : le JSON et le SQL doivent s'accorder."""
    head = next(
        c
        for c in json.loads(V5_JSON.read_text(encoding="utf-8"))["checks"]
        if c["id"] == "alembic_head"
    )
    assert head == {"id": "alembic_head", "kind": "alembic_head_single"}
    assert "revision" not in head

    for asset in (V5_SQL, V5_PGRESTORE):
        sql = asset.read_text(encoding="utf-8")
        assert "head_observation.value IS NOT NULL" in sql, asset.name
        assert "head_observation.value = '039'" not in sql, asset.name
        assert "to_jsonb('039'::text)" not in sql, asset.name


def test_the_039_invariant_survives_the_head_becoming_derived() -> None:
    """Le piège qu'un `sed` global déclencherait, épinglé pour de bon.

    `v4.sql` portait SEPT occurrences de « 039 », dont CINQ nommaient
    l'invariant installé PAR la migration 039 — `recovery_039_observation` et le
    check `project_context_updated_at_039`. Seules DEUX étaient le pin de tête.
    Un remplacement global aurait supprimé un contrôle de catalogue entier en
    silence, et le contrat aurait continué de rendre 25/25 en vérifiant moins.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        sql = asset.read_text(encoding="utf-8")
        assert "recovery_039_observation" in sql, asset.name
        assert "'project_context_updated_at_039'" in sql, asset.name
        assert sql.count("039") >= 5, asset.name


def test_v5_sql_carries_every_046_mechanism() -> None:
    for asset, variant in ((V5_SQL, "live"), (V5_PGRESTORE, "pgrestore")):
        sql = asset.read_text(encoding="utf-8")

        assert sql.startswith("WITH ") and sql.endswith(";\n") and sql.count(";") == 1
        assert "'brain-v42/postgresql-recovery/v5'" in sql
        assert "'schema_version', 5" in sql

        # L'index PARTIEL de connexion entre dans la liste FERMÉE, contrôlée
        # deux fois (absent-ou-md5-divergent, puis présent-hors-liste).
        assert f"'{CONNECTION_INDEX_MD5}'" in sql, asset.name
        assert "'uq_brain_sessions_connection'" in sql, asset.name

        for name, md5 in SESSION_CONSTRAINT_MD5[variant].items():
            assert f"'{name}', 'c', NULL::text, '{md5}'" in sql, f"{asset.name}: {name}"

        # Le QUATRIÈME littéral de statut : sans lui, le fragment ne prouve plus
        # que le CHECK terminal connaît l'état que la 046 vient d'ajouter.
        assert "'status::text = ''closed_inactive''::text'" in sql, asset.name

        assert f"('brain_sessions', '{SESSIONS_COLUMN_MD5}')" in sql, asset.name
        assert f"('codex_dream_run_v1', '{DREAM_RUN_VIEW_MD5}')" in sql, asset.name
        assert "'indexes', 130," in sql, asset.name


def test_the_two_v5_variants_keep_their_exact_cte_parity() -> None:
    """L'écart autorisé est CLOS : exactement deux CTE, pas « environ deux »."""

    def cte_names(path: Path) -> set[str]:
        return set(
            re.findall(
                r"^([a-z_][a-z0-9_]*)\s*(?:\([^)]*\))?\s*AS \(",
                path.read_text(encoding="utf-8"),
                re.M,
            )
        )

    live = cte_names(V5_SQL)
    pgrestore = cte_names(V5_PGRESTORE)

    assert pgrestore - live == {"observed_artifact_constraints", "observed_session_constraints"}
    assert not (live - pgrestore)


def test_the_v5_assets_are_regular_non_executable_files() -> None:
    """Ce que git PEUT porter — et le `0600` du runbook n'en fait pas partie.

    Première rédaction de ce test : `st_mode & 0o777 == 0o600`. Vert en local,
    ROUGE en CI, et le test avait tort. **Git ne suit que le bit exécutable** :
    tous les actifs `ops/recovery/` sont stockés `100644` dans l'index, y compris
    les v1 à v4 qui sont pourtant à `0600` sur disque. Un checkout neuf les rend
    donc à `0644` — l'assertion échouait sur une propriété que le dépôt ne peut
    pas transporter.

    Le `0600` que le runbook impose (l. 45) est une propriété OPÉRATIONNELLE du
    fichier déployé, posée à la création et sur l'hôte, pas un invariant de
    dépôt. Ce test garde donc ce qui est gardable : un fichier régulier, non
    exécutable. Le reste appartient au runbook, et le prétendre testé ici serait
    pire que de ne pas le tester — ça donnerait une assurance fausse.
    """
    for asset in (V5_JSON, V5_SQL, V5_PGRESTORE):
        assert asset.is_file(), asset.name
        assert not asset.stat().st_mode & 0o111, f"{asset.name} est exécutable"


def test_the_v5_contract_reads_and_never_writes() -> None:
    """Une attestation qui écrit n'est plus une attestation.

    Recensée par un motif POSITIF — ce que le fichier contient — plutôt que par
    l'absence des mots interdits : énumérer le bien est plus fort que chercher
    le mal, parce qu'un mot d'écriture oublié dans la liste passerait.
    """
    forbidden = re.compile(
        r"^\s*(INSERT|UPDATE|DELETE|TRUNCATE|DROP|CREATE|ALTER|GRANT|REVOKE|COPY|CALL|DO)\b",
        re.IGNORECASE | re.M,
    )
    allowed = re.compile(r"^\s*(WITH|SELECT)\b", re.IGNORECASE | re.M)

    for asset in (V5_SQL, V5_PGRESTORE):
        text = asset.read_text(encoding="utf-8")
        assert not forbidden.findall(text), asset.name
        assert len(allowed.findall(text)) > 100, asset.name
