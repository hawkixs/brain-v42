"""ACLs and owners are a SEPARATE proof, and that is structural.

`60708007`, fourth child of the `8eaefe36` gate. The finding inherited from the
parent is not a filing preference: **the sandbox restore runs
`--no-owner --no-acl`**. A restoration attestation therefore cannot, even in
principle, prove anything about the rights — it erased them before looking.

Hence a separate asset, `brain-v42-v5-acl.sql`, played against PRODUCTION
read-only. And hence, above all, **the absence of a `-pgrestore` variant**: this
is not a parity oversight, it is the content of the decision. A `-pgrestore` twin
of this proof would suggest it applies where it cannot apply. A test below pins
that absence so that nobody "completes" it.

**The main contract is structurally MUTE about this**, and that is measured: under
a `REVOKE` on one of the contract's views, under an owner change, and under an
`ALTER ROLE codex_ro CREATEDB`, it returns exactly its background noise. That is
the split's premise, verified rather than assumed.

**The expected list is DERIVED from migration 036**, never copied — an explicit
requirement of the ticket. The precedent motivating it is 045: a `DROP VIEW` takes
its `GRANT`s away silently, the view comes back, and `codex_ro` has lost its read
access without a single line saying so.

**Volume quantified BEFORE writing, measured on 2026-08-22 at head `046`**: 51
relations (32 tables, 10 views, 9 sequences), all owned by `brain`; 40 carry an
explicit ACL, 11 are `NULL`; a single non-owner grantee, `codex_ro`, with `SELECT`
on exactly 10 views; 0 column ACL, 0 function ACL, 0 `GRANT OPTION`, 0 role
membership.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
RECOVERY = ROOT / "ops" / "recovery"
ACL_SQL = RECOVERY / "brain-v42-v5-acl.sql"
ACL_JSON = RECOVERY / "brain-v42-v5-acl.json"
MIGRATION_036 = ROOT / "alembic" / "versions" / "036_codex_contract_views.py"
MIGRATION_045 = ROOT / "alembic" / "versions" / "045_dream_run_model_width.py"

CHECK_ID = "acl_and_ownership"

#: The four sub-counters. Each was proved by a mutation touching ONLY it — seven
#: mutations in total, on `brain_test`, with the transactions rolled back.
COUNTERS = (
    "contract_grant_mismatches",
    "relation_owner_mismatches",
    "role_privilege_mismatches",
    "unexpected_grantee_mismatches",
)


def _granted_views(source: Path) -> set[str]:
    """The views that receive `SELECT` for `codex_ro`, READ from the migration."""
    return set(re.findall(r"GRANT SELECT ON (codex_\w+) TO codex_ro", source.read_text("utf-8")))


def test_the_expected_grants_are_derived_from_the_migration_not_retyped() -> None:
    """Explicit requirement of the ticket: derive the registry, do not copy it.

    A hand-typed list would drift from the migration at the first view added, and
    the attestation would then validate a scope nobody decided any more. This test
    re-reads 036 on every run.
    """
    expected = _granted_views(MIGRATION_036)
    assert len(expected) == 10

    declared = set(json.loads(ACL_JSON.read_text(encoding="utf-8"))["checks"][0]["objects"])
    assert declared == expected

    sql = ACL_SQL.read_text(encoding="utf-8")
    for view in sorted(expected):
        assert f"('{view}', 'codex_ro', 'SELECT')" in sql, view


def test_the_045_regrant_stays_inside_the_derived_registry() -> None:
    """045 re-lays ONE grant after a `DROP VIEW`. It must be in the list.

    This is the precedent that justifies this whole batch: a `DROP VIEW` takes its
    rights away silently. If 045 re-granted a view absent from 036's registry, the
    registry would already be incomplete — and nobody would know.
    """
    assert _granted_views(MIGRATION_045) <= _granted_views(MIGRATION_036)


def test_this_proof_has_no_pgrestore_twin_and_that_is_the_point() -> None:
    """The absence of a twin is the CONTENT of the decision, not a parity hole.

    The sandbox restore runs `--no-owner --no-acl`: it erases this proof's object
    before observing it. A `brain-v42-v5-acl-pgrestore.sql` would suggest it
    applies over there, and would return `0/1` on any valid restoration.
    """
    assert not (RECOVERY / "brain-v42-v5-acl-pgrestore.sql").exists()
    assert json.loads(ACL_JSON.read_text(encoding="utf-8"))["proof_scope"] == (
        "live-production-only"
    )


def test_the_proof_is_read_only() -> None:
    """An attestation that mutates its object attests nothing."""
    sql = ACL_SQL.read_text(encoding="utf-8")
    assert sql.startswith("WITH ") and sql.endswith(";\n") and sql.count(";") == 1
    for forbidden in ("GRANT ", "REVOKE ", "ALTER ", "CREATE ", "DROP ", "INSERT ", "UPDATE "):
        # The registry's literals contain "GRANT" in comment prose nowhere: the
        # only acceptable occurrence would be inside a quoted string, and there is
        # none.
        assert forbidden not in sql, forbidden


def test_the_check_row_names_its_four_counters() -> None:
    """A failure must say WHICH of the four moved.

    The four have unrelated causes: an accidental `REVOKE`, an
    `ALTER TABLE ... OWNER TO`, a role gaining an attribute, an unexpected grantee.
    A bare boolean would force re-reading all the SQL.
    """
    sql = ACL_SQL.read_text(encoding="utf-8")
    for counter in COUNTERS:
        assert f"'{counter}', 0" in sql, counter
        assert f"{counter}.value" in sql, counter
    entry = json.loads(ACL_JSON.read_text(encoding="utf-8"))["checks"][0]
    assert entry["id"] == CHECK_ID


def test_the_contract_grant_check_is_bidirectional() -> None:
    """Both directions, and they catch two different failures.

    Direction 1 — a registry `GRANT` that has DISAPPEARED: the 045 precedent, a
    recreated view that lost its rights. Direction 2 — an EXTRA `GRANT`: `codex_ro`
    gaining read access to a table outside the contract, which no other check would
    see. The second term excludes `brain`: the owner is not a grantee to be
    surveyed, and including it would redden the 40 relations.
    """
    sql = ACL_SQL.read_text(encoding="utf-8")
    body = sql.split("contract_grant_mismatches AS (", 1)[1].split("\n),", 1)[0]
    assert body.count("SELECT count(*)") == 2
    assert "LEFT JOIN observed_relation_privileges" in body
    assert "LEFT JOIN expected_contract_grants" in body
    assert "observed_grant.grantee <> 'brain'" in body


def test_the_owner_check_reads_every_relation_kind() -> None:
    """Sequences and views included: an owner drifts just as well there.

    A sequence whose owner changes leaves the application able to read and unable
    to insert — the failure presents itself as an application bug, never as a
    rights problem.
    """
    sql = ACL_SQL.read_text(encoding="utf-8")
    assert "relation_record.relkind IN ('r', 'v', 'S', 'm')" in sql
    assert "relation_record.owner <> 'brain'" in sql


def test_the_role_check_pins_what_codex_ro_must_never_gain() -> None:
    """The security fact, not the shape: `codex_ro` stays strictly a reader.

    `brain` is NOT pinned as a superuser — freezing it there would engrave the
    current state as a requirement and would redden the day someone hardened it.
    What is pinned is what must never move in the wrong direction.
    """
    sql = ACL_SQL.read_text(encoding="utf-8")
    for attribute in (
        "rolsuper",
        "rolcreatedb",
        "rolcreaterole",
        "rolreplication",
        "rolbypassrls",
    ):
        assert f"role_record.{attribute}" in sql, attribute
    assert "role_record.rolname = 'codex_ro'" in sql
    # The EXTRA role is the inverse direction, and it counts: a service account
    # added by hand is visible nowhere else in this repository.
    assert "expected_role.role_name IS NULL" in sql
    # Role membership is a classic bypass of the check above: `GRANT brain TO
    # codex_ro` changes no attribute.
    assert "pg_auth_members" in sql
    # And USAGE on the schema: without it, the ten GRANTs are inert.
    assert "acl_entry.privilege_type = 'USAGE'" in sql
