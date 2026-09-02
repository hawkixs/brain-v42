"""The v5 contract fingerprints the FOURTEEN trigger functions, not just one.

`75112bc6`, third child of the `8eaefe36` gate, and the last of the five.

**The figure in the ticket body was wrong by a factor of ~4, and the sceptical
pass had already corrected it.** It is not 55 functions: 55 is the number of
TRIGGERS. The distinct functions are **14**. The ticket's three numbers were
RE-MEASURED here, at head `046`, on 2026-08-22:

- **55** non-internal triggers;
- **44** already named in the asset, hence **11** were not — the real gap, not
  "~40";
- **6** `trg_*_freshness_stamped`, not 12;
- **14** distinct trigger functions, of which **ONE** was fingerprinted
  (`update_updated_at`, by the inherited 039 invariant). Thirteen could change
  body without a byte of the contract moving.

**THE HARD PRECONDITION IS LIFTED, AND BY MEASUREMENT.** The parent required it in
so many words: normalise then audit the prod vs fresh-migration drift BEFORE any
fingerprint, without which the fingerprint would engrave the drift as the
reference. A database was built AFRESH by `alembic upgrade head`, then the 14
`prosrc_sha256` compared to production: **ZERO difference**. The comparison was
deliberately NOT made against `brain_test` — that database is migrated at every
session but it cannot be established that it was never cloned from production, and
a control where the object may have produced the witness is hollow.

**Sampling by class, proposed by the ticket, was not adopted** — the sceptical
pass had already made it optional by bringing the volume down from 55 to 14.
Fourteen fingerprints fit in ONE `VALUES` list, where the two already-attested
functions cost ~135 CTE lines each. Sampling would have left functions outside the
contract to save lines we have no need to save.
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

CHECK_ID = "trigger_function_fingerprints"

#: MEASURED on 2026-08-22 against production at head `046`, and replayed against a
#: freshly built database: identical. `update_updated_at` appears here AND in the
#: 039 invariant — a test compares the two copies, otherwise one would drift from
#: the other while staying green.
TRIGGER_FUNCTIONS: tuple[tuple[str, str, int], ...] = (
    (
        "enforce_immutable_ticket_participants",
        "cdb295b8a5c811467706ac9c10622fe1140d957e51f94b0fb50911ac9629bb30",
        256,
    ),
    (
        "enforce_live_feature_artifact_target",
        "11f79d4116738608988f29c53bb1db708537cc3fdeac18c5bdede106bf6bccd7",
        496,
    ),
    (
        "increment_project_focus_revision",
        "424dfc1a9154dbc48e08ffc70712920cbfdc42e659a1500da681a2e50526df76",
        215,
    ),
    (
        "normalize_project_key_alias",
        "13b945bb4a5c307f430b0b6ba1387a3a38cc0abe8ac8ed58aa391f9a99e63518",
        921,
    ),
    (
        "normalize_related_project_aliases",
        "f9b325aed559eef8c28c46d5168cd88027e51a851678381146fc94317e012e6a",
        572,
    ),
    (
        "reject_project_context_key_change",
        "e800aecbe1054d8333babd1c43f1f52893db81b6b8f4e7fc6d167c6cd6f9de82",
        226,
    ),
    (
        "set_project_context_updated_at",
        "60c6154d6230d1d0e9244d8f20bc6d6b30e887e71263692e54363c96e22c0419",
        391,
    ),
    (
        "stamp_content_updated_at",
        "070b4db370dbe20a280a4f75e58edc72f337e9abcce9cadf673af1f1d30b2342",
        77,
    ),
    (
        "stamp_freshness_status",
        "179caf250bf9fe5aae1d1e1fdb040b4b08008a9c5d76cc1f65ebaf3272db86dd",
        890,
    ),
    (
        "sync_brain_entity_registry",
        "dab84538fedcd42d28038a3055c1b7e6d4e1f7f02f21891e1195cafdb3f0489c",
        10485,
    ),
    (
        "sync_project_registry",
        "ff39be21e857296038f463ff71eb932a65d7e3be7c7120a2414a3f5832ce4565",
        3699,
    ),
    (
        "sync_referenced_project_registry",
        "6844d14802019487796602f9cef95327f67a2c56798c1cb561541b4537f6a093",
        306,
    ),
    (
        "sync_related_project_registry",
        "f1dd4dd21283d6a98a9f14e801685b13415f4de23dbdc59336201baafb3d60be",
        349,
    ),
    ("update_updated_at", "83ca0f7a3230405dae8b4f4e692b4983869b58e4225b6e60bbf96db3f6ae9a59", 96),
)

#: `update_updated_at`'s fingerprint as the 039 invariant already pins it. Written
#: by hand HERE, on purpose: it is the TERM OF COMPARISON, and deriving it from the
#: same source as what it compares would make it hollow.
UPDATE_UPDATED_AT_SHA256 = "83ca0f7a3230405dae8b4f4e692b4983869b58e4225b6e60bbf96db3f6ae9a59"

#: The 11 stamping triggers the asset did not NAME — 5 from 041, 6 from 043.
#: `19` = BEFORE + INSERT + UPDATE + FOR EACH ROW.
STAMPING_TRIGGERS: tuple[tuple[str, str, str], ...] = (
    ("adrs", "trg_adrs_content_updated", "stamp_content_updated_at"),
    ("adrs", "trg_adrs_freshness_stamped", "stamp_freshness_status"),
    ("decisions", "trg_decisions_content_updated", "stamp_content_updated_at"),
    ("decisions", "trg_decisions_freshness_stamped", "stamp_freshness_status"),
    ("indexed_plans", "trg_indexed_plans_freshness_stamped", "stamp_freshness_status"),
    ("learnings", "trg_learnings_content_updated", "stamp_content_updated_at"),
    ("learnings", "trg_learnings_freshness_stamped", "stamp_freshness_status"),
    ("runbooks", "trg_runbooks_content_updated", "stamp_content_updated_at"),
    ("runbooks", "trg_runbooks_freshness_stamped", "stamp_freshness_status"),
    ("snippets", "trg_snippets_content_updated", "stamp_content_updated_at"),
    ("snippets", "trg_snippets_freshness_stamped", "stamp_freshness_status"),
)

NEW_CTES = (
    "expected_trigger_functions",
    "observed_trigger_functions",
    "trigger_function_mismatches",
    "expected_stamping_triggers",
    "observed_stamping_triggers",
    "stamping_trigger_mismatches",
)

#: The attributes a trigger function must keep to enter the OBSERVED set. Putting
#: them in the predicate rather than in the expected list is what makes a drifted
#: function fall OUT of the observation: it becomes "expected and not found", which
#: is exactly the fact.
INVARIANT_ATTRIBUTES = (
    "prokind = 'f'",
    "provolatile = 'v'",
    "pronargs = 0",
    "pronargdefaults = 0",
    "NOT function_record.prosecdef",
    "NOT function_record.proleakproof",
    "NOT function_record.proretset",
    "proconfig IS NULL",
)


def _cte_body(path: Path, name: str) -> str:
    match = re.search(
        rf"^{name}(?:\([^)]*\))? AS \((.*?)^\),$",
        path.read_text(encoding="utf-8"),
        re.M | re.S,
    )
    assert match is not None, f"{path.name}: {name}"
    return match.group(1)


def test_the_remeasured_volume_is_pinned() -> None:
    """The ticket's three numbers, re-measured — the body had two of them wrong."""
    assert len(TRIGGER_FUNCTIONS) == 14
    assert len({entry[0] for entry in TRIGGER_FUNCTIONS}) == 14
    assert len(STAMPING_TRIGGERS) == 11
    assert sum(1 for _, name, _ in STAMPING_TRIGGERS if name.endswith("_freshness_stamped")) == 6
    assert sum(1 for _, name, _ in STAMPING_TRIGGERS if name.endswith("_content_updated")) == 5
    # All the fingerprints are hexadecimal SHA-256, and all distinct: two functions
    # at the same digest would signal a copy-paste of the list.
    for _, digest, octets in TRIGGER_FUNCTIONS:
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        assert octets > 0
    assert len({entry[1] for entry in TRIGGER_FUNCTIONS}) == 14


def test_both_assets_pin_every_trigger_function() -> None:
    for asset in (V5_SQL, V5_PGRESTORE):
        expected = _cte_body(asset, "expected_trigger_functions")
        for name, digest, octets in TRIGGER_FUNCTIONS:
            assert f"('{name}', '{digest}', {octets})" in expected, f"{asset.name}: {name}"


def test_the_039_fingerprint_and_the_census_agree_on_update_updated_at() -> None:
    """Two copies of the same fingerprint: compare them, or one will drift.

    The 039 invariant has pinned `update_updated_at` for a long time; the census
    pins it again. Each would stay green while drifting from the other — that is
    the failure mode of duplicated fingerprints, the one `2bb1988f` already met on
    the column formula.
    """
    census = {name: digest for name, digest, _ in TRIGGER_FUNCTIONS}
    assert census["update_updated_at"] == UPDATE_UPDATED_AT_SHA256

    for asset in (V5_SQL, V5_PGRESTORE):
        sql = asset.read_text(encoding="utf-8")
        # Present TWICE: once in the 039 invariant, once in the census.
        assert sql.count(UPDATE_UPDATED_AT_SHA256) == 2, asset.name


def test_the_observed_set_is_bounded_by_the_invariant_attributes() -> None:
    """A function that loses one of these attributes LEAVES the observation.

    That is deliberate and it is the point: `SECURITY DEFINER` set on a trigger
    function is a privilege escalation, not a body divergence. By making it leave
    the observed set, it becomes "expected and not found" — the exact truth, and a
    failure rather than a silence.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        body = _cte_body(asset, "observed_trigger_functions")
        for attribute in INVARIANT_ATTRIBUTES:
            assert attribute in body, f"{asset.name}: {attribute}"
        assert "lanname = 'plpgsql'" in body, asset.name


def test_both_assets_declare_the_eleven_unnamed_stamping_triggers() -> None:
    """The 11 the asset did not name — the REAL gap, measured at 11 and not ~40."""
    for asset in (V5_SQL, V5_PGRESTORE):
        expected = _cte_body(asset, "expected_stamping_triggers")
        for table, trigger, function in STAMPING_TRIGGERS:
            assert f"('{table}', '{trigger}', '{function}', 19, " in expected, (
                f"{asset.name}: {trigger}"
            )


def test_the_stamping_check_pins_the_when_clause() -> None:
    """The WHEN clause is the WHOLE meaning of these triggers, and it is losable.

    The 11 are CONDITIONAL: `WHEN (old.x IS DISTINCT FROM new.x)`. Recreated
    without its clause, the trigger would stamp at EVERY write — 041 was written
    precisely so that `content_updated_at` only moves on a real content change. The
    name, the table and the function would all three be intact: only the md5 of the
    condition sees the loss.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        body = _cte_body(asset, "observed_stamping_triggers")
        assert "pg_get_triggerdef" in body, asset.name
        assert "WHEN \\((.*)\\) EXECUTE " in body, asset.name
        mismatches = _cte_body(asset, "stamping_trigger_mismatches")
        assert "condition_md5 = expected_trigger.condition_md5" in mismatches, asset.name


def test_a_disabled_trigger_falls_out_of_the_observed_set() -> None:
    """`tgenabled = 'O'` in the predicate, and it is the most silent failure.

    A DISABLED trigger still exists, carries its name, its table and its function —
    `pg_trigger` returns it, a `\\d table` displays it. It simply no longer does
    anything. Without this predicate, the attestation would count it as present.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        assert "trigger_record.tgenabled = 'O'" in _cte_body(asset, "observed_stamping_triggers"), (
            asset.name
        )


def test_both_checks_are_bidirectional() -> None:
    """Both directions, and the second is not decorative.

    An ADDED trigger function is caught by nothing else: it is neither a table, nor
    an index, nor a constraint. A stamping trigger placed on one more table would
    move `content_updated_at` where nobody expects it — and the function census,
    for its part, would stay green.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        for name in ("trigger_function_mismatches", "stamping_trigger_mismatches"):
            body = _cte_body(asset, name)
            assert body.count("SELECT count(*)") == 2, f"{asset.name}: {name}"
            assert "IS NULL" in body, f"{asset.name}: {name}"


def test_the_stamping_observation_is_bounded_by_function_not_by_name() -> None:
    """The second direction must be able to see a NEW table, not only the eleven.

    Bounding the observation to the expected tables would have made the inverse
    term blind to the case that matters — a stamp placed elsewhere. Bounding it to
    the two stamping FUNCTIONS keeps it open to the whole database.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        body = _cte_body(asset, "observed_stamping_triggers")
        assert (
            "function_record.proname IN ('stamp_content_updated_at', 'stamp_freshness_status')"
            in body
        ), asset.name
        for table, _, _ in STAMPING_TRIGGERS:
            assert f"table_record.relname = '{table}'" not in body, f"{asset.name}: {table}"


def test_the_new_ctes_are_byte_identical_across_the_two_variants() -> None:
    """MEASURED parity: not one WHEN clause carries a normalised pattern.

    They contain bare `::text` — `(old.title)::text` — but no
    `::character varying::text` nor `]::text[]`, the only two forms `pg_restore`
    rewrites. Verified IN THE DATABASE over the 55 triggers, not by eye.
    """
    for name in NEW_CTES:
        assert _cte_body(V5_SQL, name) == _cte_body(V5_PGRESTORE, name), name

    for asset in (V5_SQL, V5_PGRESTORE):
        for name in NEW_CTES:
            body = _cte_body(asset, name)
            assert "'::character varying::text'" not in body, f"{asset.name}: {name}"
            assert "']::text[]'" not in body, f"{asset.name}: {name}"


def test_the_check_row_names_its_two_counters() -> None:
    for asset in (V5_SQL, V5_PGRESTORE):
        sql = asset.read_text(encoding="utf-8")
        row = sql.split(f"'{CHECK_ID}',", 1)[1].split("UNION ALL", 1)[0]
        assert "'stamping_trigger_mismatches', 0" in row, asset.name
        assert "'trigger_function_mismatches', 0" in row, asset.name


def test_the_json_manifest_declares_the_fingerprint_check() -> None:
    checks = json.loads(V5_JSON.read_text(encoding="utf-8"))["checks"]
    entry = next(check for check in checks if check["id"] == CHECK_ID)
    assert entry == {"id": CHECK_ID, "kind": "brain_schema_invariant", "name": CHECK_ID}
