"""Le contrat v5 atteste la FORME des tables historiques, pas seulement leurs contraintes.

`2bb1988f`, cinquième et dernier enfant de la porte `8eaefe36` — créé par la
passe sceptique qui a RÉFUTÉ la couverture du premier split. Le bullet « fermer
propriétés de relation, colonnes et index des tables historiques » n'était mappé
dans aucun des quatre premiers enfants : `81c4f366` était borné aux
**contraintes**. C'est le pan oublié, et il est mesurable :

- `pg_get_indexdef` n'était appliqué qu'à `brain_sessions` et
  `brain_session_artifacts` ;
- le gabarit de propriétés de relation (`relkind`/`relpersistence`/
  `relrowsecurity`/`reloptions`/héritage/méthode d'accès) ne visait que ces deux
  mêmes tables ;
- les empreintes de colonnes ne couvraient que ces deux tables et les vues codex.

Les **17 index**, **58 colonnes** et **5 relations** de `brain_entities`,
`entity_relations`, `graph_outbox`, `graph_projection_leases` et `projects`
n'étaient donc attestés par RIEN — pas même leur existence en tant que table
ordinaire non partitionnée.

**Parité maximale ici, et c'est mesuré, pas espéré.** Aucun `pg_get_indexdef` ni
aucun `column_default` de ces cinq tables ne porte les motifs que `pg_restore`
normalise (`::character varying::text`, `]::text[]`). Les huit CTE ajoutées sont
donc **littéralement identiques** dans les deux actifs — la liste FERMÉE d'écart
de CTE reste à ses DEUX entrées, intacte.
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

HISTORICAL_TABLES = (
    "brain_entities",
    "entity_relations",
    "graph_outbox",
    "graph_projection_leases",
    "projects",
)

CHECK_ID = "historical_relation_shape"

#: MESURÉ le 2026-08-22 contre la production à la tête `046`. Volume chiffré
#: AVANT d'écrire, comme le ticket l'exige : 5 tables, 17 index, 58 colonnes.
#: Aucun de ces `pg_get_indexdef` ne porte de motif normalisé par `pg_restore` —
#: les md5 sont donc les mêmes dans les deux variantes, ce qu'un test vérifie.
HISTORICAL_INDEXES: tuple[tuple[str, str, bool, bool, tuple[str, ...], str], ...] = (
    # fmt: off
    (
        "brain_entities",
        "brain_entities_pkey",
        True,
        True,
        ("id",),
        "e7f8e9706d0ca15ab1df60fb221f1064",
    ),
    (
        "brain_entities",
        "idx_brain_entities_project_lifecycle",
        False,
        False,
        ("project_key", "lifecycle"),
        "59159e2e0ed1243096e9b04b1005ec58",
    ),
    (
        "brain_entities",
        "idx_brain_entities_type_lifecycle",
        False,
        False,
        ("entity_type", "lifecycle"),
        "bf933545ea4759505dd2a3f28170ce7e",
    ),
    (
        "brain_entities",
        "uq_brain_entities_source_uuid",
        True,
        False,
        ("source_uuid",),
        "6215745c9bbabfc4f6536224f51d9ff0",
    ),
    (
        "brain_entities",
        "uq_brain_entities_type_key",
        True,
        False,
        ("entity_type", "entity_key"),
        "cb21fcf6bca9db57aa5b610f27fc9b47",
    ),
    (
        "entity_relations",
        "entity_relations_pkey",
        True,
        True,
        ("id",),
        "217e4ca0931ea037192712f456ff7577",
    ),
    (
        "entity_relations",
        "idx_entity_relations_source_active",
        False,
        False,
        ("source_entity_id", "lifecycle"),
        "0af525f7cbb3ddfc3763838f93bc6acd",
    ),
    (
        "entity_relations",
        "idx_entity_relations_target_active",
        False,
        False,
        ("target_entity_id", "lifecycle"),
        "5fe4e2413a47315d52716e4999c08991",
    ),
    (
        "entity_relations",
        "idx_entity_relations_type_active",
        False,
        False,
        ("relation_type", "lifecycle"),
        "d900d75c260e8b647bf8fc0a8942ed2a",
    ),
    (
        "entity_relations",
        "uq_entity_relations_endpoints_type",
        True,
        False,
        ("source_entity_id", "target_entity_id", "relation_type"),
        "1fd361469b76df214973bb3f938b498a",
    ),
    (
        "graph_outbox",
        "graph_outbox_event_id_key",
        True,
        False,
        ("event_id",),
        "a56287d3b45a1f279ec5b4cd33ceafd5",
    ),
    ("graph_outbox", "graph_outbox_pkey", True, True, ("id",), "474a1b1b4ac19d21b74731ac8d9a9999"),
    (
        "graph_outbox",
        "idx_graph_outbox_pending",
        False,
        False,
        ("available_at", "id"),
        "ca916f57c7b789b027252a40cd189aa1",
    ),
    (
        "graph_outbox",
        "uq_graph_outbox_entity_revision",
        True,
        False,
        ("entity_id", "aggregate_revision"),
        "ad9028401163d52968e71007bd54bbd7",
    ),
    (
        "graph_outbox",
        "uq_graph_outbox_relation_revision",
        True,
        False,
        ("relation_id", "aggregate_revision"),
        "38e7cf6ec07210a1bf3fbe6fb369b048",
    ),
    (
        "graph_projection_leases",
        "graph_projection_leases_pkey",
        True,
        True,
        ("slot",),
        "093ed839687394ee6da85086e39e6da7",
    ),
    ("projects", "projects_pkey", True, True, ("project_key",), "671e28752233598f9e71ed5a655952ec"),
    # fmt: on
)

#: Empreinte de colonnes par table, MESURÉE le 2026-08-22 avec l'expression
#: littérale de `observed_column_fingerprints` — dix-sept champs
#: d'`information_schema.columns`, agrégés par `ordinal_position`.
HISTORICAL_COLUMN_FINGERPRINTS: dict[str, str] = {
    "brain_entities": "29129c0e227139630018e4da8f8274ef",
    "entity_relations": "6f646a72602beef83e1918181f283e73",
    "graph_outbox": "b4b8228e2ce20ca8f975e385b3712b86",
    "graph_projection_leases": "b2970aedef9c874f2b7d440425ed7430",
    "projects": "09f1991c6d569501b3da449bf8a2b4b7",
}

#: Le gabarit de propriétés de relation, repris MOT POUR MOT de
#: `session_column_mismatches`. Une table ordinaire, permanente, non
#: partitionnée, sans règle, sans RLS, sans `reloptions`, hors héritage, en
#: `heap`. Neuf propriétés : en oublier une, c'est laisser passer sa dérive.
RELATION_PROPERTY_PREDICATES = (
    "relation_record.relkind = 'r'",
    "relation_record.relpersistence = 'p'",
    "NOT relation_record.relispartition",
    "NOT relation_record.relhasrules",
    "NOT relation_record.relrowsecurity",
    "NOT relation_record.relforcerowsecurity",
    "cardinality(COALESCE(relation_record.reloptions, ARRAY[]::text[])) = 0",
    "pg_catalog.pg_inherits",
    "access_method.amname = 'heap'",
)

#: Les dix-sept champs de l'empreinte de colonnes. Le contrat en porte DEUX
#: exemplaires depuis ce lot — la CTE historique et `observed_column_fingerprints`.
#: Un test les compare, sinon l'un dériverait de l'autre en restant vert.
FINGERPRINT_FIELDS = (
    "ordinal_position",
    "column_name",
    "data_type",
    "udt_schema",
    "udt_name",
    "is_nullable",
    "character_maximum_length",
    "numeric_precision",
    "numeric_scale",
    "datetime_precision",
    "column_default",
    "is_identity",
    "identity_generation",
    "is_generated",
    "generation_expression",
    "collation_schema",
    "collation_name",
)


def _index_row(entry: tuple[str, str, bool, bool, tuple[str, ...], str]) -> str:
    table, name, is_unique, is_primary, columns, md5 = entry
    args = ", ".join(f"'{column}'" for column in columns)
    return (
        f"         '{table}',\n"
        f"         '{name}',\n"
        f"         {'TRUE' if is_unique else 'FALSE'},\n"
        f"         {'TRUE' if is_primary else 'FALSE'},\n"
        f"         jsonb_build_array({args}),\n"
        f"         '{md5}'\n"
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
    """Le volume, chiffré avant d'écrire — et qui rougit s'il bouge sans être remesuré."""
    assert len(HISTORICAL_INDEXES) == 17
    assert len(HISTORICAL_COLUMN_FINGERPRINTS) == 5
    assert {entry[0] for entry in HISTORICAL_INDEXES} == set(HISTORICAL_TABLES)
    assert set(HISTORICAL_COLUMN_FINGERPRINTS) == set(HISTORICAL_TABLES)
    assert len({(entry[0], entry[1]) for entry in HISTORICAL_INDEXES}) == 17

    # Un index PRIMARY est toujours UNIQUE, et il y en a exactement un par table.
    for _, _, is_unique, is_primary, _, _ in HISTORICAL_INDEXES:
        assert is_unique or not is_primary
    primaries = [entry[0] for entry in HISTORICAL_INDEXES if entry[3]]
    assert sorted(primaries) == sorted(HISTORICAL_TABLES)

    # Aucun md5 d'index ne collisionne : deux index de définition identique
    # seraient un signal, pas un détail — `pg_get_indexdef` porte le nom.
    assert len({entry[5] for entry in HISTORICAL_INDEXES}) == 17


def test_both_assets_declare_every_historical_index_and_fingerprint() -> None:
    for asset in (V5_SQL, V5_PGRESTORE):
        sql = asset.read_text(encoding="utf-8")
        for entry in HISTORICAL_INDEXES:
            assert _index_row(entry) in sql, f"{asset.name}: {entry[0]}.{entry[1]}"
        for table, md5 in HISTORICAL_COLUMN_FINGERPRINTS.items():
            assert f"('{table}', '{md5}')" in sql, f"{asset.name}: {table}"


def test_the_new_ctes_are_byte_identical_across_the_two_variants() -> None:
    """La parité la plus forte possible — et elle est MESURÉE, pas décrétée.

    Aucun `pg_get_indexdef` ni aucun `column_default` de ces cinq tables ne porte
    `::character varying::text` ni `]::text[]`. Rien à normaliser, donc rien à
    faire diverger : les huit CTE sont les mêmes octets des deux côtés. Si une
    migration future introduisait un défaut casté, ce test rougirait — et ce
    serait le bon moment pour se poser la question, pas six mois plus tard.
    """
    names = (
        "expected_historical_indexes",
        "observed_historical_indexes",
        "historical_index_mismatches",
        "expected_historical_column_fingerprints",
        "observed_historical_column_fingerprints",
        "historical_column_mismatches",
        "expected_historical_relations",
        "historical_relation_property_mismatches",
    )
    for name in names:
        assert _cte_body(V5_SQL, name) == _cte_body(V5_PGRESTORE, name), name

    for asset in (V5_SQL, V5_PGRESTORE):
        for name in names:
            body = _cte_body(asset, name)
            # Les ARGUMENTS de `replace()`, entre quotes — pas la construction
            # SQL nue. `cardinality(COALESCE(reloptions, ARRAY[]::text[]))`
            # contient littéralement `]::text[]` sans être une normalisation :
            # première rédaction rouge là-dessus, et c'est le test qui avait
            # tort. Ce qu'on interdit, c'est qu'une variante normalise ce que
            # l'autre ne normalise pas.
            assert "'::character varying::text'" not in body, f"{asset.name}: {name}"
            assert "']::text[]'" not in body, f"{asset.name}: {name}"


def test_the_index_check_is_bidirectional() -> None:
    """Sans le second sens, un index ajouté à la main sur une table historique
    passerait sans un bruit — et c'est exactement ce qu'un `REINDEX` bâclé ou une
    optimisation posée à chaud laisse derrière lui."""
    for asset in (V5_SQL, V5_PGRESTORE):
        body = _cte_body(asset, "historical_index_mismatches")
        assert "LEFT JOIN observed_historical_indexes" in body, asset.name
        assert "UNION ALL" in body, asset.name
        assert "WHERE NOT EXISTS" in body, asset.name
        assert "observed_index.indisvalid" in body, asset.name
        assert "observed_index.indisready" in body, asset.name
        assert "observed_index.columns = expected_index.columns" in body, asset.name


def test_the_column_check_has_one_direction_and_says_why() -> None:
    """Un contrôle qui NE PEUT PAS échouer est pire que pas de contrôle.

    L'empreinte de colonnes n'a qu'un sens, et c'est délibéré : l'observation est
    bornée par la liste attendue, donc un second terme « observé hors liste »
    serait structurellement vide — il rassurerait sans jamais rien voir. Une
    table qui APPARAÎT est déjà attrapée par `table_set`, une colonne ajoutée ou
    retypée change l'empreinte. Ce test épingle l'absence pour qu'on ne
    « complète » pas plus tard par un terme creux.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        body = _cte_body(asset, "historical_column_mismatches")
        assert body.count("SELECT count(*)") == 1, asset.name
        assert "LEFT JOIN observed_historical_column_fingerprints" in body, asset.name

        observed = _cte_body(asset, "observed_historical_column_fingerprints")
        assert "SELECT table_name FROM expected_historical_column_fingerprints" in observed


def test_the_fingerprint_formula_is_the_contract_s_own_and_has_not_drifted() -> None:
    """Deux exemplaires de la même formule : les comparer, ou l'un dérivera seul.

    `observed_column_fingerprints` existait déjà. La CTE historique en est une
    seconde instance. Chacune resterait verte en dérivant de l'autre — c'est le
    mode de panne des empreintes dupliquées. Les dix-sept champs, dans l'ordre.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        historical = _cte_body(asset, "observed_historical_column_fingerprints")
        original = _cte_body(asset, "observed_column_fingerprints")
        for body in (historical, original):
            found = [field for field in FINGERPRINT_FIELDS if f".{field}," in body]
            assert found == list(FINGERPRINT_FIELDS[:-1]), asset.name
            assert f".{FINGERPRINT_FIELDS[-1]}" in body, asset.name


def test_the_relation_property_template_carries_all_nine_predicates() -> None:
    for asset in (V5_SQL, V5_PGRESTORE):
        body = _cte_body(asset, "historical_relation_property_mismatches")
        for predicate in RELATION_PROPERTY_PREDICATES:
            assert predicate in body, f"{asset.name}: {predicate}"

        listed = _cte_body(asset, "expected_historical_relations")
        for table in HISTORICAL_TABLES:
            assert f"('{table}')" in listed, f"{asset.name}: {table}"


def test_the_check_row_names_its_three_counters_in_both_assets() -> None:
    """Un compteur agrégé anonyme ne dit pas QUOI a bougé — celui-ci le dit."""
    counters = (
        "historical_column_mismatches",
        "historical_index_mismatches",
        "historical_relation_property_mismatches",
    )
    for asset in (V5_SQL, V5_PGRESTORE):
        sql = asset.read_text(encoding="utf-8")
        assert f"'{CHECK_ID}'," in sql, asset.name
        for counter in counters:
            assert f"'{counter}', 0," in sql or f"'{counter}', 0\n" in sql, (
                f"{asset.name}: {counter}"
            )
            assert f"'{counter}', {counter}.value" in sql, f"{asset.name}: {counter}"
            assert f"CROSS JOIN {counter}" in sql or f"FROM {counter}" in sql, asset.name


def test_the_json_manifest_declares_the_historical_check() -> None:
    document = json.loads(V5_JSON.read_text(encoding="utf-8"))
    ids = [check["id"] for check in document["checks"]]

    assert CHECK_ID in ids
    assert ids == sorted(ids)
    # Compte exact : `test_v5_json_is_the_exact_v4_delta`, source unique.
    assert next(check for check in document["checks"] if check["id"] == CHECK_ID) == {
        "id": CHECK_ID,
        "kind": "brain_schema_invariant",
        "name": CHECK_ID,
    }


def test_the_md5_census_keys_on_the_row_never_on_the_digest_alone() -> None:
    """Même règle qu'au 4/5, et pour la même raison mesurée.

    Un md5 empreinte une DÉFINITION, pas un objet : `md5('primary key (id)')`
    est partagé par 24 contraintes de la base. Le recensement porte donc sur la
    LIGNE, jamais sur le digest nu. Le digest nu ne garde qu'un emploi juste :
    le manifeste JSON ne doit porter AUCUN md5.
    """
    hex32 = re.compile(r"\b[0-9a-f]{32}\b")

    # Motif 1 — la ligne entière, dans les actifs GELÉS.
    for name in ("brain-v42-v4.sql", "brain-v42-v4-pgrestore.sql"):
        text = (RECOVERY / name).read_text(encoding="utf-8")
        for entry in HISTORICAL_INDEXES:
            assert _index_row(entry) not in text, f"{name}: {entry[1]}"
        for table, md5 in HISTORICAL_COLUMN_FINGERPRINTS.items():
            assert f"('{table}', '{md5}')" not in text, f"{name}: {table}"

    # Motif 2 — le digest nu, sur le seul fichier où l'absence est l'invariant.
    assert not hex32.findall(V5_JSON.read_text(encoding="utf-8"))

    # Motif 3 — présence effective, des deux côtés, sans distinction de variante.
    mine = {entry[5] for entry in HISTORICAL_INDEXES} | set(HISTORICAL_COLUMN_FINGERPRINTS.values())
    for asset in (V5_SQL, V5_PGRESTORE):
        assert mine <= set(hex32.findall(asset.read_text(encoding="utf-8"))), asset.name
