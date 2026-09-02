"""The v5 contract attests the SHAPE of the historical tables, not only their constraints.

`2bb1988f`, fifth and last child of the `8eaefe36` gate — created by the sceptical
pass that REFUTED the first split's coverage. The bullet "close relation
properties, columns and indexes of the historical tables" was mapped in none of
the first four children: `81c4f366` was bounded to **constraints**. That is the
forgotten side, and it is measurable:

- `pg_get_indexdef` was only applied to `brain_sessions` and
  `brain_session_artifacts`;
- the relation-properties template (`relkind`/`relpersistence`/
  `relrowsecurity`/`reloptions`/inheritance/access method) only targeted those same
  two tables;
- the column fingerprints only covered those two tables and the codex views.

The **17 indexes**, **58 columns** and **5 relations** of `brain_entities`,
`entity_relations`, `graph_outbox`, `graph_projection_leases` and `projects` were
therefore attested by NOTHING — not even their existence as an ordinary
non-partitioned table.

**Maximal parity here, and it is measured, not hoped for.** No `pg_get_indexdef`
and no `column_default` of these five tables carries the patterns `pg_restore`
normalises (`::character varying::text`, `]::text[]`). The eight added CTEs are
therefore **literally identical** in both assets — the CLOSED list of CTE
differences stays at its TWO entries, intact.
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

#: MEASURED on 2026-08-22 against production at head `046`. Volume quantified
#: BEFORE writing, as the ticket requires: 5 tables, 17 indexes, 58 columns. None
#: of these `pg_get_indexdef` carries a pattern normalised by `pg_restore` — the
#: md5 are therefore the same in both variants, which a test verifies.
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

#: Per-table column fingerprint, MEASURED on 2026-08-22 with
#: `observed_column_fingerprints`'s literal expression — seventeen fields of
#: `information_schema.columns`, aggregated by `ordinal_position`.
HISTORICAL_COLUMN_FINGERPRINTS: dict[str, str] = {
    "brain_entities": "29129c0e227139630018e4da8f8274ef",
    "entity_relations": "6f646a72602beef83e1918181f283e73",
    "graph_outbox": "b4b8228e2ce20ca8f975e385b3712b86",
    "graph_projection_leases": "b2970aedef9c874f2b7d440425ed7430",
    "projects": "09f1991c6d569501b3da449bf8a2b4b7",
}

#: The relation-properties template, taken WORD FOR WORD from
#: `session_column_mismatches`. An ordinary, permanent, non-partitioned table, with
#: no rule, no RLS, no `reloptions`, outside inheritance, on `heap`. Nine
#: properties: forgetting one means letting its drift through.
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

#: The seventeen fields of the column fingerprint. The contract carries TWO copies
#: of it since this batch — the historical CTE and `observed_column_fingerprints`.
#: A test compares them, otherwise one would drift from the other while staying
#: green.
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
    """The volume, quantified before writing — and reddening if it moves without a re-measure."""
    assert len(HISTORICAL_INDEXES) == 17
    assert len(HISTORICAL_COLUMN_FINGERPRINTS) == 5
    assert {entry[0] for entry in HISTORICAL_INDEXES} == set(HISTORICAL_TABLES)
    assert set(HISTORICAL_COLUMN_FINGERPRINTS) == set(HISTORICAL_TABLES)
    assert len({(entry[0], entry[1]) for entry in HISTORICAL_INDEXES}) == 17

    # A PRIMARY index is always UNIQUE, and there is exactly one per table.
    for _, _, is_unique, is_primary, _, _ in HISTORICAL_INDEXES:
        assert is_unique or not is_primary
    primaries = [entry[0] for entry in HISTORICAL_INDEXES if entry[3]]
    assert sorted(primaries) == sorted(HISTORICAL_TABLES)

    # No index md5 collides: two indexes with an identical definition would be a
    # signal, not a detail — `pg_get_indexdef` carries the name.
    assert len({entry[5] for entry in HISTORICAL_INDEXES}) == 17


def test_both_assets_declare_every_historical_index_and_fingerprint() -> None:
    for asset in (V5_SQL, V5_PGRESTORE):
        sql = asset.read_text(encoding="utf-8")
        for entry in HISTORICAL_INDEXES:
            assert _index_row(entry) in sql, f"{asset.name}: {entry[0]}.{entry[1]}"
        for table, md5 in HISTORICAL_COLUMN_FINGERPRINTS.items():
            assert f"('{table}', '{md5}')" in sql, f"{asset.name}: {table}"


def test_the_new_ctes_are_byte_identical_across_the_two_variants() -> None:
    """The strongest possible parity — and it is MEASURED, not decreed.

    No `pg_get_indexdef` and no `column_default` of these five tables carries
    `::character varying::text` nor `]::text[]`. Nothing to normalise, hence
    nothing to make diverge: the eight CTEs are the same bytes on both sides. If a
    future migration introduced a cast default, this test would redden — and that
    would be the right moment to ask the question, not six months later.
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
            # The ARGUMENTS of `replace()`, in quotes — not the bare SQL
            # construction. `cardinality(COALESCE(reloptions, ARRAY[]::text[]))`
            # literally contains `]::text[]` without being a normalisation: the
            # first draft went red on this, and it was the test that was wrong.
            # What is forbidden is one variant normalising what the other does
            # not.
            assert "'::character varying::text'" not in body, f"{asset.name}: {name}"
            assert "']::text[]'" not in body, f"{asset.name}: {name}"


def test_the_index_check_is_bidirectional() -> None:
    """Without the second direction, an index added by hand on a historical table
    would pass unnoticed — and that is exactly what a botched `REINDEX` or a
    hot-applied optimisation leaves behind."""
    for asset in (V5_SQL, V5_PGRESTORE):
        body = _cte_body(asset, "historical_index_mismatches")
        assert "LEFT JOIN observed_historical_indexes" in body, asset.name
        assert "UNION ALL" in body, asset.name
        assert "WHERE NOT EXISTS" in body, asset.name
        assert "observed_index.indisvalid" in body, asset.name
        assert "observed_index.indisready" in body, asset.name
        assert "observed_index.columns = expected_index.columns" in body, asset.name


def test_the_column_check_has_one_direction_and_says_why() -> None:
    """A check that CANNOT fail is worse than no check.

    The column fingerprint has only one direction, and that is deliberate: the
    observation is bounded by the expected list, so a second "observed off-list"
    term would be structurally empty — it would reassure without ever seeing
    anything. A table that APPEARS is already caught by `table_set`, a column added
    or retyped changes the fingerprint. This test pins the absence so that nobody
    "completes" it later with a hollow term.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        body = _cte_body(asset, "historical_column_mismatches")
        assert body.count("SELECT count(*)") == 1, asset.name
        assert "LEFT JOIN observed_historical_column_fingerprints" in body, asset.name

        observed = _cte_body(asset, "observed_historical_column_fingerprints")
        assert "SELECT table_name FROM expected_historical_column_fingerprints" in observed


def test_the_fingerprint_formula_is_the_contract_s_own_and_has_not_drifted() -> None:
    """Two copies of the same formula: compare them, or one will drift alone.

    `observed_column_fingerprints` already existed. The historical CTE is a second
    instance of it. Each would stay green while drifting from the other — that is
    the failure mode of duplicated fingerprints. The seventeen fields, in order.
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
    """An anonymous aggregate counter does not say WHAT moved — this one does."""
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
    """Same rule as in 4/5, and for the same measured reason.

    An md5 fingerprints a DEFINITION, not an object: `md5('primary key (id)')` is
    shared by 24 constraints of the database. The census therefore bears on the
    ROW, never on the bare digest. The bare digest keeps only one correct use: the
    JSON manifest must carry NO md5 at all.
    """
    hex32 = re.compile(r"\b[0-9a-f]{32}\b")

    # Pattern 1 — the whole row, in the FROZEN assets.
    for name in ("brain-v42-v4.sql", "brain-v42-v4-pgrestore.sql"):
        text = (RECOVERY / name).read_text(encoding="utf-8")
        for entry in HISTORICAL_INDEXES:
            assert _index_row(entry) not in text, f"{name}: {entry[1]}"
        for table, md5 in HISTORICAL_COLUMN_FINGERPRINTS.items():
            assert f"('{table}', '{md5}')" not in text, f"{name}: {table}"

    # Pattern 2 — the bare digest, on the only file where absence is the invariant.
    assert not hex32.findall(V5_JSON.read_text(encoding="utf-8"))

    # Pattern 3 — effective presence, on both sides, without variant distinction.
    mine = {entry[5] for entry in HISTORICAL_INDEXES} | set(HISTORICAL_COLUMN_FINGERPRINTS.values())
    for asset in (V5_SQL, V5_PGRESTORE):
        assert mine <= set(hex32.findall(asset.read_text(encoding="utf-8"))), asset.name
