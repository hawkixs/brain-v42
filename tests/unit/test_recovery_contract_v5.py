"""Static authority checks for the DERIVED-head recovery contract (v5).

This contract applies the **S1** signature (decision `9d22bc6a`): the attestation
proves the schema's **SHAPE**, and the revision check becomes "a single applied
head" instead of "the head is N". House precedent: `test_alembic_env.py` — *"The
head is DERIVED, not pinned: the invariant is a sole head, not the head is N."*

**What that changes, and why it is the whole point.** The v4 contract pinned
`alembic_head = '039'`. Any later head therefore made it fail *for the sole reason
that a migration had landed*, that is, in the nominal case. Measured on
2026-08-20, before 046: the live receipt returned **22/25**, and the three
failures were all consequences of migrations later than v4's mint — never a
degradation. A contract that turns red at every cutover no longer teaches anyone
anything: that is the definition of an alarm people stop reading.

**The exact revision is not lost for all that**: it stays proved, on the code
side, by `_REQUIRED_ALEMBIC_HEAD` and its pin test — fail-closed, and coupled to
the corridor. The attestation stops duplicating a proof that lives elsewhere.

The receipt **still returns the observed value**: it says which head the database
carries, it merely stops requiring which one.
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

#: The v4 assets are FROZEN byte-for-byte by this file, exactly as v4 froze v3. An
#: attestation lineage is not rewritten: each contract describes the state it
#: described, and the next one is derived from the previous one.
V4_SHA256 = {
    "brain-v42-v4.json": "e1ab0520e4e55b69985eefe865ea3e163a562280194a42578eeb788ef0f73e38",
    "brain-v42-v4.sql": "c4dd8293866c1e77e9e8cdf22149886d8b03da99e7e75c8164ec2d8654628c99",
    "brain-v42-v4-pgrestore.sql": (
        "3b1f228e8aa94f4967737b474710beacba1b876a2f7f3b4ba6c2e21bfc6c2335"
    ),
}

#: Fingerprints MEASURED on 2026-08-21 against production at head `046`, with the
#: contract's literal expressions, and verified identical on `brain_test`. The two
#: variants have DIFFERENT values for the same constraints: `pgrestore`
#: additionally normalises `::character varying::text` and `]::text[]`. Confusing
#: the two sets produces a contract that fails without saying why.
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
    """v5 is v4's DELTA, derived — never retyped.

    Same discipline as `_expected_v4()`, which derived from v3: copying the whole
    document would allow a silent divergence between two contracts meant to
    describe the same database one migration apart.
    """
    document = cast(dict[str, Any], copy.deepcopy(json.loads(V4_JSON.read_text(encoding="utf-8"))))
    checks = document["checks"]
    assert isinstance(checks, list)
    by_id = {check["id"]: check for check in checks}

    head = by_id["alembic_head"]
    head.clear()
    # The `revision` key DISAPPEARS. Leaving it at some value would suggest it is
    # still read — a contract must not carry a dead field.
    head.update({"id": "alembic_head", "kind": "alembic_head_single"})

    by_id["catalog_counts"]["indexes"] = 130

    # `81c4f366`: the INHERITED constraints (033/034/035 and `projects`) were
    # attested by NAME only. The dedicated check row — rather than a merge into
    # `brain_runtime_032_036_037` — is what makes the hardening VISIBLE in the
    # receipt: the denominator moves to 26. Merged in, it would have verified more
    # while still returning 25/25, that is, without anyone being able to see it.
    checks.append(
        {
            "id": "inherited_constraint_definitions",
            "kind": "brain_schema_invariant",
            "name": "inherited_constraint_definitions",
        }
    )

    # `2bb1988f`, the previous one's forgotten side: indexes, columns and relation
    # properties of the historical tables were attested by NOTHING — `81c4f366` was
    # bounded to CONSTRAINTS. Same reason to be a dedicated check row: a hardening
    # invisible in the receipt is an unverifiable hardening.
    checks.append(
        {
            "id": "historical_relation_shape",
            "kind": "brain_schema_invariant",
            "name": "historical_relation_shape",
        }
    )

    # `f36846a1`: the NINE sequences were attested by nothing — `grep -ci sequence`
    # on `v4.sql` returned 0. The check that matters is not their shape but
    # `last_value >= max(id)`: that is the SILENT restore failure, where the
    # sequences restart at 1 and the following INSERTs hit a PK collision — a
    # database that looks restored and refuses the first write. A dedicated check
    # row, for the same reason as its two sisters.
    checks.append(
        {
            "id": "sequence_shape",
            "kind": "brain_schema_invariant",
            "name": "sequence_shape",
        }
    )

    # `75112bc6`: FOURTEEN trigger functions in production, ONE fingerprinted
    # (`update_updated_at`, by the 039 invariant). The other thirteen — including
    # 041's and 043's two stampers — could change body without a byte of the
    # contract moving. The ticket's hard precondition (normalise the drift BEFORE
    # fingerprinting) was LIFTED by measurement: zero difference between production
    # and a database built afresh by `alembic upgrade head`. Without that
    # measurement, the fingerprint would have engraved the drift.
    checks.append(
        {
            "id": "trigger_function_fingerprints",
            "kind": "brain_schema_invariant",
            "name": "trigger_function_fingerprints",
        }
    )

    # `8eaefe36`: the SHAPE of the 32 tables — columns, constraints, indexes — at
    # DENSE rank. Same reason to be a DEDICATED check row as its four sisters, and
    # one more, measured: the first draft grafted the three counters onto
    # `catalog_counts` to hold the receipt at `29/29`. That held the number and
    # nothing else — `red-backup` models `catalog_counts_equals` in Pydantic
    # `extra="forbid"` over exactly FOUR fields, so a fifth signal merged in there
    # does not cost zero, it breaks another repository. The denominator moves to 30,
    # and the DR runbook's `dr-current` declaration follows.
    checks.append(
        {
            "id": "table_shape",
            "kind": "brain_schema_invariant",
            "name": "table_shape",
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
    assert len(document["checks"]) == 30


def test_the_head_check_is_derived_and_carries_no_revision() -> None:
    """S1, stated on both sides: the JSON and the SQL must agree."""
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
    """The trap a global `sed` would spring, pinned for good.

    `v4.sql` carried SEVEN occurrences of "039", of which FIVE named the invariant
    installed BY migration 039 — `recovery_039_observation` and the check
    `project_context_updated_at_039`. Only TWO were the head pin. A global
    replacement would have removed an entire catalogue check silently, and the
    contract would have kept returning 25/25 while verifying less.
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

        # The PARTIAL connection index enters the CLOSED list, checked twice
        # (absent-or-md5-divergent, then present-off-list).
        assert f"'{CONNECTION_INDEX_MD5}'" in sql, asset.name
        assert "'uq_brain_sessions_connection'" in sql, asset.name

        for name, md5 in SESSION_CONSTRAINT_MD5[variant].items():
            assert f"'{name}', 'c', NULL::text, '{md5}'" in sql, f"{asset.name}: {name}"

        # The FOURTH status literal: without it, the fragment no longer proves the
        # terminal CHECK knows the state 046 has just added.
        assert "'status::text = ''closed_inactive''::text'" in sql, asset.name

        assert f"('brain_sessions', '{SESSIONS_COLUMN_MD5}')" in sql, asset.name
        assert f"('codex_dream_run_v1', '{DREAM_RUN_VIEW_MD5}')" in sql, asset.name
        assert "'indexes', 130," in sql, asset.name


def test_the_two_v5_variants_keep_their_exact_cte_parity() -> None:
    """The allowed difference is CLOSED: exactly two CTEs, not "about two"."""

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
    """What git CAN carry — and the runbook's `0600` is not part of it.

    First draft of this test: `st_mode & 0o777 == 0o600`. Green locally, RED in CI,
    and the test was wrong. **Git only tracks the executable bit**: every
    `ops/recovery/` asset is stored `100644` in the index, including v1 to v4 which
    are nevertheless `0600` on disk. A fresh checkout therefore returns them at
    `0644` — the assertion failed on a property the repository cannot transport.

    The `0600` the runbook mandates (l. 45) is an OPERATIONAL property of the
    deployed file, set at creation time and on the host, not a repository
    invariant. This test therefore guards what is guardable: a regular,
    non-executable file. The rest belongs to the runbook, and claiming it tested
    here would be worse than not testing it — it would give a false assurance.
    """
    for asset in (V5_JSON, V5_SQL, V5_PGRESTORE):
        assert asset.is_file(), asset.name
        assert not asset.stat().st_mode & 0o111, f"{asset.name} est exécutable"


def test_the_v5_contract_reads_and_never_writes() -> None:
    """An attestation that writes is no longer an attestation.

    Surveyed through a POSITIVE pattern — what the file contains — rather than
    through the absence of forbidden words: enumerating the good is stronger than
    looking for the bad, because a write keyword forgotten from the list would pass.
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
