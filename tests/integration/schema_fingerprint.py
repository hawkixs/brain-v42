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
    ENABLE ALWAYS TRIGGER, DISABLE TRIGGER, or touches session_replication_role.

Therefore every trigger the chain produces is in origin state, and any trigger
that is NOT is something else's work. The premise is re-derived from the tree by
``migrations_emitting_non_origin_trigger_state`` and pinned by a unit test, so a
migration that ever needs a non-origin trigger fails loudly instead of quietly
widening the guard's blind spot.

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

_NON_ORIGIN_TRIGGER_DDL = re.compile(
    r"ENABLE\s+(REPLICA|ALWAYS)\s+TRIGGER|DISABLE\s+TRIGGER|session_replication_role",
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
