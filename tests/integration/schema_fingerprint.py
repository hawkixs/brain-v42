"""Watch the schema itself, not the revision number that stands in for it.

The residue guard in :mod:`tests.integration.schema_residue` compares the
deployed Alembic revision to the repository head. That is a **proxy**. A test
that mutates the schema without Alembic — measured: two files, three tests —
moves the object while leaving the proxy untouched, so the guard stays silent
and the damage surfaces much later as a broken database contract.

Measured on a disposable database on 2026-08-22: leaving
``ALTER TABLE feature_artifacts ENABLE REPLICA TRIGGER …`` in place changed the
schema fingerprint and broke ``test_gateway_readiness_accepts_the_real_036_contract``
downstream, while ``alembic_version`` still read ``046`` and the guard said
nothing.

**The reference is DERIVED, never frozen.** A pinned fingerprint would be one
more literal that ages the day a migration lands — this repository has paid for
that lesson repeatedly. What is derived here is a *property* of the migration
chain rather than a value:

    No migration under alembic/versions/ emits ENABLE REPLICA TRIGGER,
    ENABLE ALWAYS TRIGGER, DISABLE TRIGGER, or touches session_replication_role,
    EXCEPT to disable one of the triggers named in
    ``DELIBERATELY_DISABLED_TRIGGERS``.

Therefore every trigger the chain produces is in origin state — or is one named
trigger in the one state its own migration documents — and anything else is
something else's work. The premise is re-derived from the tree by
``migrations_emitting_non_origin_trigger_state`` and pinned by a unit test, so a
migration that ever needs a non-origin trigger fails loudly instead of quietly
widening the guard's blind spot.

That exception was earned, not assumed: 050 tripped this guard on 2026-09-02, and
the guard's own message asks for exactly this — "revisit it before landing that
migration". What was revisited is narrow: a NAME plus a STATE. `R` or `A` on that
same name still refuses, and disabling any other trigger still refuses.

**Universal, never existential.** The verdict is "no trigger diverges", not "a
compliant trigger exists". A single divergence is a refusal, and the message
lists them all.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

# PostgreSQL pg_trigger.tgenabled: O=origin, D=disabled, R=replica, A=always.
ORIGIN_TRIGGER_STATE = "O"
_TRIGGER_STATE_NAMES = {
    "O": "origin (normal)",
    "D": "DISABLED — the trigger never fires",
    "R": "REPLICA — fires only for replication, so not in a normal session",
    "A": "ALWAYS — fires even for replication",
}

#: The chain's ONE deliberate exception, and it is a name plus a state, never a
#: category. Migration 050 must CREATE its constraint trigger and leave it
#: silent: between the `upgrade` and the MCP restart the live process still runs
#: pre-050 code and writes no history row, so an armed trigger would abort every
#: `brain_session_end` carrying `focus_outcome=applied` — fail-closed, session
#: left open, no practicable killswitch. Arming it is a dated operator gesture.
#:
#: The exception is NOT free, and the cost is deliberately left where the design
#: put it: `ops/recovery`'s attestation requires `tgenabled = 'O'` and stays RED
#: while this trigger is off. This guard stops the integration suite from being
#: collateral damage; it does not absolve the switch.
DELIBERATELY_DISABLED_TRIGGERS = {
    "project_contexts_focus_history_required": (
        "050 — created disabled so it cannot abort session_end during the window "
        "between the upgrade and the MCP restart; armed by a named operator gesture"
    ),
}

_NON_ORIGIN_TRIGGER_DDL = re.compile(
    r"ENABLE\s+(REPLICA|ALWAYS)\s+TRIGGER|DISABLE\s+TRIGGER|session_replication_role",
    re.IGNORECASE,
)

#: `DISABLE TRIGGER <named>` — and that verb only. Naming a trigger licenses one
#: statement, not the trigger: `ENABLE REPLICA TRIGGER <same name>` still offends.
_PERMITTED_DISABLE_DDL = re.compile(
    r"DISABLE\s+TRIGGER\s+(?:"
    + "|".join(re.escape(name) for name in DELIBERATELY_DISABLED_TRIGGERS)
    + r")\b",
    re.IGNORECASE,
)


def migrations_emitting_non_origin_trigger_state(versions_dir: Path) -> list[str]:
    """Return migrations whose ``upgrade()`` leaves a trigger off origin state.

    Only ``upgrade()`` is scanned: a ``downgrade()`` never runs in a database
    that is at head, so its text says nothing about the state head produces.

    An empty result is what licenses the guard below. It is recomputed from the
    tree on every run rather than asserted once in prose.
    """
    offenders: list[str] = []
    for path in sorted(versions_dir.glob("*.py")):
        source = path.read_text()
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - a broken migration fails elsewhere
            offenders.append(f"{path.name} (unparseable)")
            continue
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
                segment = ast.get_source_segment(source, node) or ""
                # Blank out the permitted statement FIRST, then search. Deleting
                # it rather than pattern-matching around it keeps the offending
                # regex untouched: whatever it caught yesterday it still catches.
                segment = _PERMITTED_DISABLE_DDL.sub("", segment)
                if _NON_ORIGIN_TRIGGER_DDL.search(segment):
                    offenders.append(path.name)
    return offenders


@dataclass(frozen=True)
class SchemaProbe:
    """What the live schema reported, or why it could not be read.

    ``trigger_states`` maps a non-internal trigger name to its ``tgenabled``
    code. ``None`` means the probe did not produce a reading at all — which is
    NOT the same as "nothing diverges", and the guard below refuses to confuse
    the two.
    """

    trigger_states: Mapping[str, str] | None = None
    session_replication_role: str | None = None
    failure: str | None = None


class MalformedSchemaProbe(RuntimeError):
    """The instrument, not the schema, is what failed."""


def _reject_malformed_probe(probe: SchemaProbe) -> None:
    """Fail on an unusable reading instead of letting it read as agreement.

    This exists because of a concrete near-miss on 2026-08-22: a fingerprint
    query that errored returned an empty string on both sides of a comparison,
    and the script printed "IDENTICAL" — the answer that was hoped for. A broken
    instrument that confirms is invisible on review, so malformedness is an
    ERROR here and never an outcome.
    """
    if probe.failure is not None:
        raise MalformedSchemaProbe(
            f"Schema probe failed and produced no reading: {probe.failure}\n"
            "    This is NOT 'the schema is clean' — the instrument did not answer. "
            "Fix the probe before trusting any verdict."
        )
    if probe.trigger_states is None:
        raise MalformedSchemaProbe(
            "Schema probe returned no trigger reading at all.\n"
            "    An absent reading must never be read as an absence of divergence."
        )
    if not probe.trigger_states:
        raise MalformedSchemaProbe(
            "Schema probe found ZERO non-internal triggers.\n"
            "    A database migrated to head always carries several, so an empty set means "
            "the probe looked in the wrong place or ran against the wrong database — "
            "not that every trigger is healthy."
        )
    unknown = sorted(
        f"{name}={state!r}"
        for name, state in probe.trigger_states.items()
        if state not in _TRIGGER_STATE_NAMES
    )
    if unknown:
        raise MalformedSchemaProbe(
            f"Schema probe returned unknown tgenabled codes: {', '.join(unknown)}.\n"
            "    Refusing to classify a state this guard does not understand."
        )
    if probe.session_replication_role is None:
        raise MalformedSchemaProbe(
            "Schema probe did not read session_replication_role.\n"
            "    A persisted 'replica' setting silences every trigger at once; not reading it "
            "is not the same as it being 'origin'."
        )


def describe_schema_divergence(probe: SchemaProbe) -> str | None:
    """Return the refusal message, or ``None`` when NOTHING diverges.

    Universal by construction: the verdict is built from the set of divergences
    and is ``None`` only when that set is empty. It is never derived from the
    existence of a compliant trigger.

    Raises :class:`MalformedSchemaProbe` when the reading is unusable — that is
    a deliberate third outcome, distinct from both "clean" and "diverged".
    """
    _reject_malformed_probe(probe)
    assert probe.trigger_states is not None  # narrowed by the guard above

    divergent = {
        name: state
        for name, state in sorted(probe.trigger_states.items())
        if state != ORIGIN_TRIGGER_STATE
        # `D` on a NAMED trigger is the state its migration ships. Any other
        # state on that same name is somebody else's work and still refuses —
        # allowlisting a name rather than a (name, state) pair would let the
        # 2026-08-22 replica residue back in through the door opened for 050.
        and not (name in DELIBERATELY_DISABLED_TRIGGERS and state == "D")
    }
    role_diverges = probe.session_replication_role != "origin"
    if not divergent and not role_diverges:
        return None

    total = len(probe.trigger_states)
    lines = [
        "Integration setup refused: the live schema diverges from what the migration "
        "chain produces.",
        "",
        "    The Alembic revision is NOT the thing that moved — it can be perfectly correct while",
        "    this is wrong. No migration under alembic/versions/ emits ENABLE REPLICA/ALWAYS "
        "TRIGGER,",
        "    DISABLE TRIGGER, or touches session_replication_role, so anything below was left by",
        "    something other than a migration — almost certainly an interrupted test that mutates",
        "    the schema directly and restores it in a `finally`.",
        "",
    ]
    if divergent:
        lines.append(
            f"    {len(divergent)} of {total} non-internal triggers are not in origin state:"
        )
        lines.extend(
            f"        {name}: {_TRIGGER_STATE_NAMES.get(state, state)}"
            for name, state in divergent.items()
        )
        lines.append("")
    if role_diverges:
        lines.extend(
            [
                f"    session_replication_role is {probe.session_replication_role!r}, "
                "not 'origin'.",
                "        Every trigger in the database is silenced at once while that holds. "
                "Check for",
                "        a persisted ALTER DATABASE … SET or ALTER ROLE … SET.",
                "",
            ]
        )
    lines.extend(
        [
            "    This guard does NOT repair the schema, for the same reason the residue guard "
            "does not:",
            "    repairing it here would erase the only evidence that it happened. Restore it "
            "yourself,",
            "    against the TEST database only:",
            "",
            "        ALTER TABLE <table> ENABLE TRIGGER <trigger>;",
            "",
            "    then re-run. If you cannot tell which test left it, the breadcrumbs under "
            ".pytest_cache/",
            "    name the migration tests; a schema mutation outside Alembic leaves none.",
        ]
    )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# The CLASS, not the case (ticket 3a7da99d).
#
# The guard above watches ONE family — trigger state — because that is the family
# the ticket's first witness belonged to. The ticket is explicit that this is a
# class: "a future test doing an `ALTER`, a `CREATE INDEX` or a direct `GRANT`
# would fall in the same blind spot". Below is the class, closed by comparing the
# WHOLE fingerprint of the live test database against one derived from the chain.
#
# DERIVED on both sides, and that is the point. Nothing here is a literal that
# ages at the next migration: the reference is a database `alembic upgrade head`
# has just built, so the day 052 lands, both sides move together and the guard
# keeps meaning the same thing. The first guard had to derive a PROPERTY of the
# chain to stay honest; this one derives the whole object and needs no property.
#
# Measured 2026-09-03, and it is why there is no allowlist at all: `brain_test`
# and a fresh head agree on all nine families, exactly — 34 tables, 522 columns,
# 128 constraints, 134 indexes, 58 triggers, 140 functions, 10 views, 10
# sequences, 318 grants, zero divergence. There is nothing legitimate to carve
# out, so carving nothing out is a measurement rather than optimism.
#
# Cost, measured the same day: 0.93 s for the reference database — 0.85 s of it
# `alembic upgrade head` over 51 revisions. The mandate budgeted 10-30 s and
# planned a session cache; at under a second the cache would only add staleness.

#: family -> SQL returning `(name, digest)`. The name is what a refusal PRINTS,
#: so it carries its table: `learnings.zz_index` is actionable, `zz_index` is a
#: guessing game. The digest is empty where presence is the whole property.
#:
#: Deliberately NOT keyed on `attnum` anywhere. A dropped column leaves a hole in
#: the ordinals, so an ordinal-keyed fingerprint measures the INSTANCE's history
#: rather than its schema — the lesson `test_recovery_contract_dense_column_rank`
#: exists to keep.
SCHEMA_FAMILIES: Mapping[str, str] = {
    "tables": """
        SELECT tablename AS name, '' AS digest
        FROM pg_catalog.pg_tables WHERE schemaname = 'public'
    """,
    "columns": """
        SELECT table_name || '.' || column_name AS name,
               md5(concat_ws('|', data_type, udt_name, is_nullable,
                             character_maximum_length, numeric_precision, numeric_scale,
                             datetime_precision, column_default, is_identity,
                             identity_generation, is_generated, generation_expression,
                             collation_name)) AS digest
        FROM information_schema.columns WHERE table_schema = 'public'
    """,
    "constraints": """
        SELECT source_table.relname || '.' || constraint_record.conname AS name,
               md5(regexp_replace(
                   lower(pg_catalog.pg_get_constraintdef(constraint_record.oid, TRUE)),
                   '[[:space:]]+', ' ', 'g')) AS digest
        FROM pg_catalog.pg_constraint AS constraint_record
        JOIN pg_catalog.pg_class AS source_table
          ON source_table.oid = constraint_record.conrelid
        JOIN pg_catalog.pg_namespace AS source_schema
          ON source_schema.oid = source_table.relnamespace
        WHERE source_schema.nspname = 'public' AND source_table.relkind IN ('r', 'p')
    """,
    "indexes": """
        SELECT source_table.relname || '.' || index_class.relname AS name,
               md5(pg_catalog.pg_get_indexdef(index_record.indexrelid)) AS digest
        FROM pg_catalog.pg_index AS index_record
        JOIN pg_catalog.pg_class AS index_class ON index_class.oid = index_record.indexrelid
        JOIN pg_catalog.pg_class AS source_table ON source_table.oid = index_record.indrelid
        JOIN pg_catalog.pg_namespace AS source_schema
          ON source_schema.oid = source_table.relnamespace
        WHERE source_schema.nspname = 'public' AND source_table.relkind IN ('r', 'p')
    """,
    # `tgenabled` is INSIDE the digest, so a trigger switched off directly is a
    # divergence rather than a presence — the shape of both non-alembic mutations
    # this ticket's census found.
    "triggers": """
        SELECT source_table.relname || '.' || trigger_record.tgname AS name,
               concat_ws('|', trigger_record.tgenabled::text, trigger_function.proname,
                         trigger_record.tgtype::text) AS digest
        FROM pg_catalog.pg_trigger AS trigger_record
        JOIN pg_catalog.pg_class AS source_table ON source_table.oid = trigger_record.tgrelid
        JOIN pg_catalog.pg_proc AS trigger_function
          ON trigger_function.oid = trigger_record.tgfoid
        JOIN pg_catalog.pg_namespace AS source_schema
          ON source_schema.oid = source_table.relnamespace
        WHERE source_schema.nspname = 'public' AND NOT trigger_record.tgisinternal
    """,
    "functions": """
        SELECT routine.proname || '(' ||
               pg_catalog.pg_get_function_identity_arguments(routine.oid) || ')' AS name,
               encode(sha256(routine.prosrc::bytea), 'hex') AS digest
        FROM pg_catalog.pg_proc AS routine
        JOIN pg_catalog.pg_namespace AS routine_schema
          ON routine_schema.oid = routine.pronamespace
        WHERE routine_schema.nspname = 'public'
    """,
    "views": """
        SELECT view_class.relname AS name,
               md5(pg_catalog.pg_get_viewdef(view_class.oid, TRUE)) AS digest
        FROM pg_catalog.pg_class AS view_class
        JOIN pg_catalog.pg_namespace AS view_schema
          ON view_schema.oid = view_class.relnamespace
        WHERE view_schema.nspname = 'public' AND view_class.relkind IN ('v', 'm')
    """,
    # Properties, never `last_value`: a sequence that has handed out numbers is
    # not a schema divergence, and comparing values would refuse every used
    # database.
    "sequences": """
        SELECT sequencename AS name,
               concat_ws('|', data_type::text, increment_by::text, min_value::text,
                         max_value::text, cycle::text) AS digest
        FROM pg_catalog.pg_sequences WHERE schemaname = 'public'
    """,
    "grants": """
        SELECT table_name || ':' || grantee || ':' || privilege_type AS name, '' AS digest
        FROM information_schema.role_table_grants WHERE table_schema = 'public'
    """,
}

#: What a family cannot legitimately be. A chain-built database carries every one
#: of these, so an empty family means the probe looked in the wrong place — the
#: 2026-08-22 near-miss, where a query that errored compared equal to another
#: query that errored and printed "IDENTICAL".
SchemaFamilies = Mapping[str, Mapping[str, str]]


def probe_schema_families(db_url: str) -> dict[str, dict[str, str]]:
    """Read every family from a live database. Never partial, never silent."""
    import asyncio

    import asyncpg

    async def probe() -> dict[str, dict[str, str]]:
        connection = await asyncpg.connect(
            db_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        )
        try:
            return {
                family: {
                    str(row["name"]): str(row["digest"]) for row in await connection.fetch(sql)
                }
                for family, sql in SCHEMA_FAMILIES.items()
            }
        finally:
            await connection.close()

    return asyncio.run(probe())


def _reject_malformed_families(reference: SchemaFamilies, observed: SchemaFamilies) -> None:
    """An unusable reading is a third outcome, never "nothing diverged"."""
    for label, probe in (("reference", reference), ("observed", observed)):
        missing = sorted(set(SCHEMA_FAMILIES) - set(probe))
        if missing:
            raise MalformedSchemaProbe(
                f"The {label} schema probe is missing families: {', '.join(missing)}.\n"
                "    An absent family must never be read as an absence of divergence."
            )
        empty = sorted(family for family in SCHEMA_FAMILIES if not probe[family])
        if empty:
            raise MalformedSchemaProbe(
                f"The {label} schema probe found ZERO objects in: {', '.join(empty)}.\n"
                "    A database at head carries objects in every family, so an empty one means "
                "the probe ran against the wrong database or the query broke — not that the "
                "schema is clean."
            )


def describe_family_divergence(reference: SchemaFamilies, observed: SchemaFamilies) -> str | None:
    """Return the refusal NAMING every divergent object, or ``None`` when clean.

    Universal, never existential: the verdict is built from the set of
    divergences and is ``None`` only when that set is empty across all nine
    families.

    Raises :class:`MalformedSchemaProbe` when either reading is unusable.
    """
    _reject_malformed_families(reference, observed)

    sections: list[str] = []
    for family in SCHEMA_FAMILIES:
        expected, actual = reference[family], observed[family]
        extra = sorted(set(actual) - set(expected))
        absent = sorted(set(expected) - set(actual))
        changed = sorted(
            name for name in set(expected) & set(actual) if expected[name] != actual[name]
        )
        if not (extra or absent or changed):
            continue
        lines = [f"    {family}:"]
        lines += [f"        + {name}  (present here, absent from the chain)" for name in extra]
        lines += [f"        - {name}  (the chain produces it; it is gone here)" for name in absent]
        lines += [f"        ~ {name}  (same object, different definition)" for name in changed]
        sections.append("\n".join(lines))

    if not sections:
        return None

    return "\n".join(
        [
            "Integration setup refused: the live schema diverges from the one the migration "
            "chain produces.",
            "",
            "    The alembic revision is NOT what moved -- it can be perfectly correct while "
            "this is",
            "    wrong. The reference below was built by `alembic upgrade head` in a disposable "
            "database",
            "    moments ago, so every line is something a migration did not do: almost "
            "certainly a test",
            "    that mutates the schema directly and restores it in a `finally` that did not run.",
            "",
            *sections,
            "",
            "    This guard does NOT repair the schema, for the same reason the residue guard "
            "does not:",
            "    repairing it would erase the only evidence that it happened. Undo it yourself, "
            "against",
            "    the TEST database only, then re-run.",
        ]
    )


def describe_underivable_premise(offenders: Sequence[str]) -> str:
    """Message for the day a migration legitimately needs a non-origin trigger."""
    return (
        "The premise behind the schema guard no longer holds: these migrations emit a "
        "non-origin trigger state or touch session_replication_role:\n"
        + "\n".join(f"    {name}" for name in offenders)
        + "\n\nThe guard assumes every trigger a migration produces is in origin state, and "
        "refuses\nany trigger that is not. Revisit it before landing that migration — "
        "otherwise it will\nrefuse a database that is perfectly correct."
    )
