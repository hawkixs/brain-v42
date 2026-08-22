"""Le contrat v5 atteste les NEUF séquences — leur forme, et surtout leur avance.

`f36846a1`, deuxième enfant de la porte `8eaefe36`. Le trou était TOTAL et se
mesurait d'une commande : `grep -ci sequence ops/recovery/brain-v42-v4.sql`
rendait **0**. Aucune séquence n'était attestée, ni par sa forme, ni par sa
liaison à sa colonne, ni par son avance.

**Le contrôle qui compte n'est pas la forme, c'est `last_value >= max(id)`.**
C'est la panne SILENCIEUSE du restore : les séquences repartent à 1, la base
paraît restaurée, tous les `SELECT` passent — et le premier `INSERT` tombe en
collision de clé primaire. Une restauration qui se déclare réussie et refuse la
première écriture est exactement ce que ce contrat existe pour attraper, et
c'est le seul contrôle du lot qui ne peut mordre QUE sur un restore réel : sur
la prod vivante il est vrai par construction.

**Volume chiffré AVANT d'écrire, mesuré le 2026-08-22 contre la tête `046` :**
9 séquences, 9 liées à une colonne, 0 orpheline.

**Parité maximale, mesurée.** Ces CTE ne lisent ni `pg_get_indexdef` ni
`column_default` : il n'y a rien que `pg_restore` normalise, donc les quatre CTE
ajoutées sont les mêmes octets des deux côtés. La liste FERMÉE d'écart de CTE
reste à ses DEUX entrées.

**Lecture SEULE, et ce n'était pas gratuit.** `last_value` est lu dans la vue
`pg_sequences`, jamais par `currval()` ni `nextval()` — un contrat d'attestation
qui ferait avancer une séquence pour l'observer changerait l'objet qu'il mesure.
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

CHECK_ID = "sequence_shape"

#: MESURÉ le 2026-08-22 contre la production à la tête `046`, puis rejoué contre
#: une base construite À NEUF par `alembic upgrade head` : les neuf séquences y
#: sont identiques. `(séquence, table, colonne, type, pas, min, max, départ)` —
#: `cycle` est FALSE partout et n'entre pas dans ce tableau, il est asserté à
#: part pour qu'un `TRUE` qui apparaîtrait ne se cache pas dans une colonne.
SEQUENCES: tuple[tuple[str, str, str, str, int, int, int, int], ...] = (
    ("access_log_id_seq", "access_log", "id", "bigint", 1, 1, 9223372036854775807, 1),
    (
        "consolidation_log_id_seq",
        "consolidation_log",
        "id",
        "bigint",
        1,
        1,
        9223372036854775807,
        1,
    ),
    ("dream_promotions_id_seq", "dream_promotions", "id", "bigint", 1, 1, 9223372036854775807, 1),
    # La SEULE `integer` des neuf, et c'est précisément le genre d'écart qu'une
    # liste écrite à la main aplatirait sans le voir.
    ("dream_runs_id_seq", "dream_runs", "id", "integer", 1, 1, 2147483647, 1),
    ("graph_outbox_id_seq", "graph_outbox", "id", "bigint", 1, 1, 9223372036854775807, 1),
    (
        "roadmap_curation_proposals_id_seq",
        "roadmap_curation_proposals",
        "id",
        "bigint",
        1,
        1,
        9223372036854775807,
        1,
    ),
    ("search_log_id_seq", "search_log", "id", "bigint", 1, 1, 9223372036854775807, 1),
    (
        "ticket_extraction_attempts_id_seq",
        "ticket_extraction_attempts",
        "id",
        "bigint",
        1,
        1,
        9223372036854775807,
        1,
    ),
    (
        "ticket_extraction_proposals_id_seq",
        "ticket_extraction_proposals",
        "id",
        "bigint",
        1,
        1,
        9223372036854775807,
        1,
    ),
)

NEW_CTES = (
    "expected_sequences",
    "observed_sequences",
    "sequence_property_mismatches",
    "sequence_high_water",
    "sequence_backfill_mismatches",
)


def _cte_body(path: Path, name: str) -> str:
    match = re.search(
        rf"^{name}(?:\([^)]*\))? AS \((.*?)^\),$",
        path.read_text(encoding="utf-8"),
        re.M | re.S,
    )
    assert match is not None, f"{path.name}: {name}"
    return match.group(1)


def test_the_measured_volume_is_pinned_and_internally_consistent() -> None:
    """Le volume, chiffré avant d'écrire — et qui rougit s'il bouge sans remesure."""
    assert len(SEQUENCES) == 9
    assert len({entry[0] for entry in SEQUENCES}) == 9
    # Une séquence par table, et jamais deux séquences sur la même colonne : la
    # forme `<table>_<colonne>_seq` de PostgreSQL le suggère, elle ne le garantit
    # pas — un `ALTER SEQUENCE ... OWNED BY` peut la déplacer.
    assert len({(entry[1], entry[2]) for entry in SEQUENCES}) == 9
    for name, table, column, *_ in SEQUENCES:
        assert name == f"{table}_{column}_seq", name


def test_both_assets_declare_every_sequence_with_its_owning_column() -> None:
    for asset in (V5_SQL, V5_PGRESTORE):
        expected = _cte_body(asset, "expected_sequences")
        for name, table, column, data_type, *_ in SEQUENCES:
            assert f"'{name}', '{table}', '{column}', '{data_type}'" in expected, (
                f"{asset.name}: {name}"
            )
        # `cycle` FALSE sur les neuf : une séquence cyclique réémettrait des
        # identifiants déjà attribués.
        assert expected.count("FALSE)") == 9, asset.name
        assert "TRUE)" not in expected, asset.name


def test_the_high_water_mark_reads_every_owning_table() -> None:
    """`last_value >= max(id)` ne peut pas être générique en SQL statique.

    Il faut nommer les neuf tables, une par une. C'est verbeux et c'est le prix :
    une boucle demanderait du SQL dynamique, donc une fonction, donc une écriture
    — dans un contrat qui doit rester en LECTURE SEULE.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        body = _cte_body(asset, "sequence_high_water")
        for name, table, column, *_ in SEQUENCES:
            assert f"('{name}', (SELECT max({column}) FROM {table}))" in body, (
                f"{asset.name}: {name}"
            )
        assert body.count("SELECT max(") == 9, asset.name


def test_the_backfill_check_treats_a_never_called_sequence_as_zero() -> None:
    """`last_value` est NULL quand la séquence n'a jamais servi — pas « à jour ».

    C'est exactement l'état d'une séquence fraîchement restaurée qui n'a pas reçu
    son `setval`. Sans le `COALESCE`, la comparaison rendrait NULL, le `WHERE`
    serait faux, et la panne qu'on cherche passerait en silence. Le `COALESCE`
    sur `max(id)` est l'autre moitié : une table VIDE et une séquence jamais
    appelée sont cohérentes entre elles.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        body = _cte_body(asset, "sequence_backfill_mismatches")
        assert "COALESCE(sequence_state.last_value, 0)" in body, asset.name
        assert "COALESCE(high_water.highest_assigned, 0)" in body, asset.name
        assert "sequence_state.sequencename IS NULL" in body, asset.name


def test_the_contract_never_advances_a_sequence_to_observe_it() -> None:
    """Une attestation qui mute son objet n'atteste rien.

    `currval()` lèverait hors session ; `nextval()` avancerait la séquence — donc
    ferait passer un contrôle qui aurait dû échouer, en le faisant échouer une
    fois de plus au prochain restore. La vue `pg_sequences` lit sans écrire.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        sql = asset.read_text(encoding="utf-8")
        assert "pg_catalog.pg_sequences" in sql, asset.name
        assert "nextval(" not in sql, asset.name
        assert "currval(" not in sql, asset.name
        assert "setval(" not in sql, asset.name


def test_the_property_check_is_bidirectional() -> None:
    """Sans le second sens, une séquence CRÉÉE à la main passerait sans un bruit.

    Et elle ne serait attrapée par rien d'autre : `table_set` lit `pg_tables`,
    qui ignore les séquences, et `catalog_counts` ne compte qu'index et clés
    étrangères. Ce second terme est le SEUL filet.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        body = _cte_body(asset, "sequence_property_mismatches")
        assert body.count("SELECT count(*)") == 2, asset.name
        assert "LEFT JOIN observed_sequences" in body, asset.name
        assert "LEFT JOIN expected_sequences" in body, asset.name
        assert "WHERE expected_sequence.sequence_name IS NULL" in body, asset.name


def test_the_backfill_check_has_one_direction_and_says_why() -> None:
    """Un contrôle qui NE PEUT PAS échouer est pire que pas de contrôle.

    L'avance n'a qu'un sens, et c'est un FAIT sur les séquences, pas un choix :
    une séquence EN AVANCE sur `max(id)` est le régime normal — toute
    transaction annulée en laisse une. Un terme « last_value trop grand »
    rougirait sur du fonctionnement nominal. Ce test épingle l'absence pour
    qu'on ne « complète » pas la symétrie plus tard par un terme faux.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        body = _cte_body(asset, "sequence_backfill_mismatches")
        assert body.count("SELECT count(*)") == 1, asset.name
        assert ">" not in body.replace(">=", ""), asset.name


def test_the_new_ctes_are_byte_identical_across_the_two_variants() -> None:
    """Parité MAXIMALE, et mesurée plutôt que décrétée.

    Ces CTE ne lisent ni `pg_get_indexdef` ni `column_default` : il n'y a
    littéralement rien que `pg_restore` normalise. Si une migration future
    ajoutait un défaut casté sur une de ces colonnes, ce test rougirait — et ce
    serait le bon moment pour se poser la question.
    """
    for name in NEW_CTES:
        assert _cte_body(V5_SQL, name) == _cte_body(V5_PGRESTORE, name), name

    for asset in (V5_SQL, V5_PGRESTORE):
        for name in NEW_CTES:
            body = _cte_body(asset, name)
            assert "'::character varying::text'" not in body, f"{asset.name}: {name}"
            assert "']::text[]'" not in body, f"{asset.name}: {name}"


def test_the_check_row_names_its_two_counters_in_both_assets() -> None:
    """Un échec doit dire LEQUEL des deux contrôles a bougé.

    Gabarit de `brain_runtime_032_036_037` et de `historical_relation_shape` : un
    booléen nu ferait relire tout le SQL pour savoir si c'est la forme ou
    l'avance qui a dérivé — et les deux ont des causes et des correctifs
    entièrement différents.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        sql = asset.read_text(encoding="utf-8")
        row = sql.split(f"'{CHECK_ID}',", 1)[1].split("UNION ALL", 1)[0]
        assert "'sequence_backfill_mismatches', 0" in row, asset.name
        assert "'sequence_property_mismatches', 0" in row, asset.name
        assert "sequence_backfill_mismatches.value = 0" in row, asset.name
        assert "AND sequence_property_mismatches.value = 0" in row, asset.name


def test_the_json_manifest_declares_the_sequence_check() -> None:
    checks = json.loads(V5_JSON.read_text(encoding="utf-8"))["checks"]
    entry = next(check for check in checks if check["id"] == CHECK_ID)
    assert entry == {
        "id": CHECK_ID,
        "kind": "brain_schema_invariant",
        "name": CHECK_ID,
    }
