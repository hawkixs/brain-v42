"""Le contrat v5 atteste la FORME des 32 tables, pas celle de sept d'entre elles.

`8eaefe36`, dernier enfant de la porte. Avant ce lot, les empreintes de colonnes,
de contraintes et d'index couvraient SEPT tables — `brain_sessions` et
`brain_session_artifacts` par `brain_runtime_032_036_037`, les cinq relations
historiques par `historical_relation_shape`. Les vingt-cinq autres pouvaient
changer de colonne, de contrainte ou d'index sans qu'un octet du reçu ne bouge.

**Le périmètre est TRANCHÉ : les 32 tables, jamais une liste nommée.** Une liste
nommée serait existentielle, donc aveugle à la 33ᵉ table. Le côté OBSERVÉ se
dérive donc du catalogue entier, et l'écart se compte dans les DEUX sens : une
table attendue qui manque OU diverge, et une table observée que le contrat ne
connaît pas. C'est le second terme qui mord sur une 33ᵉ table.

**Le rang est DENSE, et ce n'est pas un détail de forme.** `ordinal_position`
vaut `pg_attribute.attnum`, qui porte les TROUS des colonnes mortes. Mesuré le
2026-08-23 sur trois bases à la même tête `046` : 24 colonnes mortes sur 8 tables
en production, 11 sur 6 tables sur une base bâtie à neuf par `alembic upgrade
head`, **ZÉRO** sur une base sortie de `pg_restore`. L'empreinte ordinale mesure
donc l'HISTORIQUE DE L'INSTANCE, pas le schéma : étendue aux 32 tables telle
quelle, elle faisait diverger **8 tables sur 32** entre la production et sa
propre restauration, et 6 de plus entre cette restauration et une base Alembic
neuve. Avec le rang dense, les trois bases rendent les **32** mêmes empreintes.

**Le piège dans le piège** : le rang dense se calcule APRÈS le filtre
`NOT attisdropped`. Calculé avant, il vaut `attnum` sur toutes les lignes — et le
bug SURVIT à des tests verts, parce que sur une base sans colonne morte les deux
expressions coïncident. La preuve par colonne morte vit donc dans
`tests/integration/db/test_recovery_contract_dense_column_rank.py`, avec sa
mutation de contrôle dans les deux sens ; ce module-ci ne garde que la forme.

**Une check row PROPRE, et le dénominateur passe à 30.** Première rédaction :
les trois compteurs greffés sur `catalog_counts`, pour tenir le reçu à `29/29`.
Ça tenait le nombre et rien d'autre. Un durcissement fondu dans une check row
existante vérifie plus en rendant toujours le même score — c'est le mot à mot de
`_expected_v5()` à propos de `81c4f366`, et c'est la raison d'être des quatre
check rows de v5. Deuxième raison, mesurée celle-là : `red-backup` modélise
`catalog_counts_equals` en Pydantic `extra="forbid"` sur QUATRE champs
exactement (`ReD_v1/projects/red-backup/src/backup/recovery_contract.py`), donc
un cinquième signal fondu là-dedans ne coûte pas zéro — il casse un autre dépôt.
La déclaration `dr-current` de `docs/PLAN_INDEX_REPAIR_RUNBOOK.md` suit.

**L'expression des index n'est PAS basculée, et ce module l'épingle.**
`pg_get_indexdef(oid, 0, true)` annulerait le résidu de restauration mesuré à
UN index (`idx_dream_promotions_source_materialized`), mais seulement combiné aux
quatre normalisations, et il déplacerait les **130** empreintes d'index. C'est
une décision opérateur ; tant qu'elle n'est pas prise, le contrat garde
`pg_get_indexdef(oid)` nu et le résidu reste nommé.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
RECOVERY = ROOT / "ops" / "recovery"
V5_JSON = RECOVERY / "brain-v42-v5.json"
V5_SQL = RECOVERY / "brain-v42-v5.sql"
V5_PGRESTORE = RECOVERY / "brain-v42-v5-pgrestore.sql"

ASSETS = (V5_SQL, V5_PGRESTORE)

CHECK_ID = "table_shape"

#: Les trois sous-compteurs de la check row, tous attendus à zéro.
COUNTERS = (
    "table_column_mismatches",
    "table_constraint_mismatches",
    "table_index_mismatches",
)

#: MESURÉ le 2026-08-23 contre la production à la tête `046`, puis rejoué à
#: l'identique contre une base `pg_restore` et une base `alembic upgrade head` :
#: 117 contraintes et 130 index sur les 32 tables de base du schéma `public`.
EXPECTED_CONSTRAINT_ROWS = 117
EXPECTED_INDEX_ROWS = 130


def _tables_of_the_contract() -> list[str]:
    """Les tables du contrat, LUES dans `table_set` — jamais retapées ici.

    Retaper la liste créerait une seconde source de vérité qui ne dériverait
    qu'à la lecture : le jour où une 33ᵉ table entre dans `table_set`, ce module
    doit exiger sa couverture, pas continuer d'en compter 32.
    """
    checks = json.loads(V5_JSON.read_text(encoding="utf-8"))["checks"]
    table_set = next(check for check in checks if check["id"] == "table_set")
    return list(table_set["tables"])


def _cte_block(sql: str, cte_name: str) -> str:
    """Le corps d'une CTE, du nom en colonne 0 jusqu'à sa parenthèse fermante."""
    start = sql.index(f"\n{cte_name}")
    return sql[start : sql.index("\n),\n", start)]


def _quoted_names(block: str) -> list[str]:
    return re.findall(r"'([a-z0-9_]+)'", block)


def test_the_column_fingerprint_covers_every_table_of_the_table_set() -> None:
    """Les deux nombres CÔTE À CÔTE : tables couvertes, tables existantes."""
    tables = _tables_of_the_contract()
    assert len(tables) == 32

    for asset in ASSETS:
        block = _cte_block(asset.read_text(encoding="utf-8"), "expected_table_columns")
        covered = [name for name in _quoted_names(block) if not re.fullmatch(r"[0-9a-f]{32}", name)]
        assert covered == sorted(tables), asset.name
        assert len(covered) == len(tables) == 32, asset.name


def test_the_constraint_and_index_fingerprints_carry_every_object() -> None:
    for asset in ASSETS:
        sql = asset.read_text(encoding="utf-8")
        for cte, expected_rows in (
            ("expected_table_constraints", EXPECTED_CONSTRAINT_ROWS),
            ("expected_table_indexes", EXPECTED_INDEX_ROWS),
        ):
            block = _cte_block(sql, cte)
            digests = re.findall(r"'[0-9a-f]{32}'", block)
            assert len(digests) == expected_rows, f"{asset.name}: {cte}"


def test_the_observed_side_is_universal_and_never_filtered_by_the_expected_list() -> None:
    """L'aveuglement à la 33ᵉ table se code, il ne s'oublie pas.

    Un `WHERE table_name IN (SELECT … FROM expected_…)` sur le côté observé rend
    le contrôle existentiel : il prouve que ce qu'on connaît est conforme, et ne
    dit rien de ce qu'on ne connaît pas.
    """
    for asset in ASSETS:
        sql = asset.read_text(encoding="utf-8")
        for observed, expected in (
            ("observed_table_columns", "expected_table_columns"),
            ("observed_table_constraints", "expected_table_constraints"),
            ("observed_table_indexes", "expected_table_indexes"),
        ):
            block = _cte_block(sql, observed)
            assert expected not in block, f"{asset.name}: {observed} filtre sur {expected}"
            assert "relkind IN ('r', 'p')" in block, f"{asset.name}: {observed}"


def test_every_mismatch_counter_counts_in_both_directions() -> None:
    for asset in ASSETS:
        sql = asset.read_text(encoding="utf-8")
        for counter, expected, observed in (
            ("table_column_mismatches", "expected_table_columns", "observed_table_columns"),
            (
                "table_constraint_mismatches",
                "expected_table_constraints",
                "observed_table_constraints",
            ),
            ("table_index_mismatches", "expected_table_indexes", "observed_table_indexes"),
        ):
            block = _cte_block(sql, counter)
            # Sens 1 : attendu sans observé conforme. Sens 2 : observé inconnu.
            assert f"FROM {expected} AS" in block, f"{asset.name}: {counter}"
            assert f"FROM {observed} AS" in block, f"{asset.name}: {counter}"
            assert block.count("LEFT JOIN") == 2, f"{asset.name}: {counter}"


def test_the_column_rank_is_dense_and_computed_after_the_dropped_column_filter() -> None:
    for asset in ASSETS:
        block = _cte_block(asset.read_text(encoding="utf-8"), "observed_table_columns")
        assert "NOT attribute_record.attisdropped" in block, asset.name
        assert "row_number() OVER (" in block, asset.name
        assert "PARTITION BY observed_column.table_name" in block, asset.name
        # Le rang dense REMPLACE `ordinal_position` dans l'empreinte : le laisser
        # dans la charge utile regraverait l'historique de l'instance.
        payload = block.split("jsonb_build_array(", 1)[1]
        assert "ordinal_position" not in payload, asset.name
        assert "dense_position" in payload, asset.name


def test_the_index_expression_is_not_switched_to_the_pretty_form() -> None:
    """La bascule est CHIFFRÉE et NON appliquée — épinglée pour qu'elle se voie."""
    for asset in ASSETS:
        sql = asset.read_text(encoding="utf-8")
        block = _cte_block(sql, "observed_table_indexes")
        assert "pg_catalog.pg_get_indexdef(index_record.indexrelid)" in block, asset.name
        assert ", 0, true)" not in block, asset.name
        assert "pg_get_indexdef(index_record.indexrelid, 0" not in sql, asset.name


def test_the_shape_check_is_a_row_of_its_own_and_leaves_catalog_counts_alone() -> None:
    """Le durcissement se COMPTE au reçu, et `catalog_counts` garde ses 4 champs."""
    checks = json.loads(V5_JSON.read_text(encoding="utf-8"))["checks"]
    assert len(checks) == 30

    entry = next(check for check in checks if check["id"] == CHECK_ID)
    assert entry == {"id": CHECK_ID, "kind": "brain_schema_invariant", "name": CHECK_ID}

    # `red-backup` refuse un champ de plus ici : `CatalogCountsEquals` est
    # `extra="forbid"` sur exactement ces quatre entiers.
    catalog_counts = next(check for check in checks if check["id"] == "catalog_counts")
    assert set(catalog_counts) == {
        "foreign_keys",
        "id",
        "indexes",
        "invalid_indexes",
        "kind",
        "schema",
        "unvalidated_constraints",
    }


def test_the_receipt_names_the_three_counters_rather_than_a_bare_boolean() -> None:
    """Gabarit de `sequence_shape` : un booléen nu ferait relire tout le SQL.

    Colonnes, contraintes et index ont des causes et des correctifs entièrement
    différents — une dérive de colonne est une migration manquante, une dérive
    d'index un `REINDEX`, une dérive de contrainte un round-trip de restauration.
    """
    for asset in ASSETS:
        sql = asset.read_text(encoding="utf-8")
        row = sql.split(f"'{CHECK_ID}',", 1)[1].split("UNION ALL", 1)[0]
        for counter in COUNTERS:
            assert f"'{counter}', 0" in row, f"{asset.name}: {counter} attendu"
            assert f"'{counter}', {counter}.value" in row, f"{asset.name}: {counter} observé"
            assert f"{counter}.value = 0" in row, f"{asset.name}: {counter} verdict"
