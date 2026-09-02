"""The schema guard must accuse the schema — and refuse to answer on a broken instrument.

Three outcomes, not two: clean, diverged, and **unusable reading**. The third is
the one this file exists for. A fingerprint comparison that errored on
2026-08-22 returned an empty string on both sides and printed "IDENTICAL" — the
answer that was hoped for. A broken instrument that confirms is invisible on
review, so it is an ERROR here and never a verdict.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.schema_fingerprint import (
    DELIBERATELY_DISABLED_TRIGGERS,
    MalformedSchemaProbe,
    SchemaProbe,
    describe_schema_divergence,
    describe_underivable_premise,
    migrations_emitting_non_origin_trigger_state,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VERSIONS = _REPO_ROOT / "alembic" / "versions"

_HEALTHY = {
    "trg_learnings_updated": "O",
    "trg_feature_artifact_live_target": "O",
    "trg_ticket_participants_immutable": "O",
}


def _probe(**overrides: object) -> SchemaProbe:
    fields: dict[str, object] = {
        "trigger_states": dict(_HEALTHY),
        "session_replication_role": "origin",
    }
    fields.update(overrides)
    return SchemaProbe(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The derived premise — the reference is computed from the tree, never frozen
# ---------------------------------------------------------------------------


def test_no_migration_emits_a_non_origin_trigger_state() -> None:
    """The whole guard rests on this. Re-derived from the tree, not asserted in prose.

    The day a migration legitimately needs ENABLE REPLICA TRIGGER, this fails
    and forces the guard to be revisited — instead of the guard quietly refusing
    a database that is perfectly correct.
    """
    offenders = migrations_emitting_non_origin_trigger_state(_VERSIONS)

    assert offenders == [], describe_underivable_premise(offenders)


# ---------------------------------------------------------------------------
# The ONE deliberate exception — named, single-state, and still loud elsewhere
# ---------------------------------------------------------------------------


def test_the_exception_is_a_named_singleton_not_a_category() -> None:
    """An allowlist that grows by habit is the guard rewritten as a suggestion.

    050 needs its constraint trigger to exist and NOT fire: between the upgrade
    and the MCP restart the live process still runs pre-050 code and writes no
    history row, so an armed trigger would abort every `brain_session_end` with
    `focus_outcome=applied` — fail-closed, session left open. The exception is
    therefore real, and it is exactly one trigger.
    """
    assert list(DELIBERATELY_DISABLED_TRIGGERS) == ["project_contexts_focus_history_required"]
    reason = DELIBERATELY_DISABLED_TRIGGERS["project_contexts_focus_history_required"]
    assert "050" in reason, "the exception names the revision that owns it"


def test_the_named_trigger_disabled_is_not_a_divergence() -> None:
    """The state 050 ships. Refusing it would block every integration run."""
    probe = _probe(trigger_states={**_HEALTHY, "project_contexts_focus_history_required": "D"})

    assert describe_schema_divergence(probe) is None


def test_the_named_trigger_once_armed_is_not_a_divergence_either() -> None:
    """Origin is the end state the operator gesture produces. Both are legal, nothing else is."""
    probe = _probe(trigger_states={**_HEALTHY, "project_contexts_focus_history_required": "O"})

    assert describe_schema_divergence(probe) is None


def test_the_named_trigger_in_replica_state_still_refuses() -> None:
    """The exception is for DISABLED alone — `R` on this trigger is somebody else's work.

    Without this, allowlisting a NAME would have allowlisted every state it can
    take, which is the residue of 2026-08-22 walking back in through the door
    opened for 050.
    """
    message = describe_schema_divergence(
        _probe(trigger_states={**_HEALTHY, "project_contexts_focus_history_required": "R"})
    )

    assert message is not None
    assert "REPLICA" in message


def test_the_premise_scan_allows_only_the_named_trigger_to_be_disabled(
    tmp_path: Path,
) -> None:
    """A migration may disable THE named trigger; disabling any other still offends."""
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "050_named.py").write_text(
        "def upgrade() -> None:\n"
        "    op.execute('ALTER TABLE project_contexts "
        "DISABLE TRIGGER project_contexts_focus_history_required')\n"
    )
    (versions / "051_unnamed.py").write_text(
        "def upgrade() -> None:\n"
        "    op.execute('ALTER TABLE project_contexts DISABLE TRIGGER something_else')\n"
    )

    assert migrations_emitting_non_origin_trigger_state(versions) == ["051_unnamed.py"]


def test_the_named_trigger_does_not_license_replica_ddl(tmp_path: Path) -> None:
    """Naming a trigger licenses one verb, not the trigger."""
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "052_replica.py").write_text(
        "def upgrade() -> None:\n"
        "    op.execute('ALTER TABLE project_contexts "
        "ENABLE REPLICA TRIGGER project_contexts_focus_history_required')\n"
    )

    assert migrations_emitting_non_origin_trigger_state(versions) == ["052_replica.py"]


def test_the_premise_scan_reads_upgrade_only(tmp_path: Path) -> None:
    """A downgrade() never runs against a database at head, so its text proves nothing.

    Without this, every migration that restores a trigger on the way down would
    look like an offender and the premise would read as false.
    """
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "001_only_downgrade.py").write_text(
        "def upgrade() -> None:\n"
        "    op.execute('CREATE TRIGGER t BEFORE UPDATE ON x')\n"
        "\n"
        "def downgrade() -> None:\n"
        "    op.execute('ALTER TABLE x ENABLE REPLICA TRIGGER t')\n"
    )

    assert migrations_emitting_non_origin_trigger_state(versions) == []


def test_the_premise_scan_catches_an_offending_upgrade(tmp_path: Path) -> None:
    """Negative witness: a scan that never accuses is indistinguishable from a correct one."""
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "002_offender.py").write_text(
        "def upgrade() -> None:\n    op.execute('ALTER TABLE x DISABLE TRIGGER t')\n"
    )

    assert migrations_emitting_non_origin_trigger_state(versions) == ["002_offender.py"]


def test_the_premise_scan_also_catches_session_replication_role(tmp_path: Path) -> None:
    """One setting silences every trigger at once — it belongs in the same premise."""
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "003_role.py").write_text(
        "def upgrade() -> None:\n    op.execute(\"SET session_replication_role = 'replica'\")\n"
    )

    assert migrations_emitting_non_origin_trigger_state(versions) == ["003_role.py"]


# ---------------------------------------------------------------------------
# Sense 1 — a healthy schema costs nothing
# ---------------------------------------------------------------------------


def test_a_schema_in_origin_state_is_silent() -> None:
    """The nominal witness. Without it, an always-refusing guard still looks green."""
    assert describe_schema_divergence(_probe()) is None


# ---------------------------------------------------------------------------
# Sense 2 — the measured residue, which the revision guard could not see
# ---------------------------------------------------------------------------


def test_a_replica_trigger_is_refused_and_named() -> None:
    """The exact residue measured on 2026-08-22, with the revision still at head."""
    message = describe_schema_divergence(
        _probe(trigger_states={**_HEALTHY, "trg_feature_artifact_live_target": "R"})
    )

    assert message is not None
    assert "trg_feature_artifact_live_target" in message
    assert "REPLICA" in message
    assert "revision is NOT the thing that moved" in message


def test_a_disabled_trigger_is_refused_too() -> None:
    message = describe_schema_divergence(_probe(trigger_states={**_HEALTHY, "x": "D"}))

    assert message is not None
    assert "DISABLED" in message


def test_the_verdict_is_universal_not_existential() -> None:
    """One divergence among many healthy triggers must still refuse.

    An existential check — 'a compliant trigger exists' — would pass here. That
    is the shape that made a derived control blind earlier the same day.
    """
    message = describe_schema_divergence(
        _probe(trigger_states={**_HEALTHY, "trg_ticket_participants_immutable": "R"})
    )

    assert message is not None
    assert "1 of 3 non-internal triggers" in message


def test_every_divergence_is_listed_not_just_the_first() -> None:
    message = describe_schema_divergence(_probe(trigger_states={"a": "R", "b": "D", "c": "O"}))

    assert message is not None
    assert "a:" in message and "b:" in message
    assert "2 of 3 non-internal triggers" in message


def test_a_persisted_replica_role_is_refused_even_with_healthy_triggers() -> None:
    """ALTER DATABASE … SET session_replication_role silences everything, invisibly."""
    message = describe_schema_divergence(_probe(session_replication_role="replica"))

    assert message is not None
    assert "session_replication_role" in message
    assert "silenced at once" in message


def test_the_guard_states_it_does_not_repair() -> None:
    message = describe_schema_divergence(_probe(trigger_states={**_HEALTHY, "x": "R"}))

    assert message is not None
    assert "does NOT repair the schema" in message
    assert "ENABLE TRIGGER" in message


# ---------------------------------------------------------------------------
# Sense 3 — the instrument, not the schema. This is the load-bearing section.
# ---------------------------------------------------------------------------


def test_a_failed_probe_raises_instead_of_reading_as_clean() -> None:
    with pytest.raises(MalformedSchemaProbe) as caught:
        describe_schema_divergence(SchemaProbe(failure='operator is not unique: text || "char"'))

    assert "NOT 'the schema is clean'" in str(caught.value)
    assert "operator is not unique" in str(caught.value)


def test_an_absent_trigger_reading_raises() -> None:
    with pytest.raises(MalformedSchemaProbe, match="never be read as an absence"):
        describe_schema_divergence(SchemaProbe(session_replication_role="origin"))


def test_an_empty_trigger_set_raises_rather_than_declaring_health() -> None:
    """The exact shape of the near-miss: empty compared to empty, printed as agreement.

    A database at head always carries several triggers, so an empty reading
    means the probe looked in the wrong place — never that all is well.
    """
    with pytest.raises(MalformedSchemaProbe, match="ZERO non-internal triggers"):
        describe_schema_divergence(_probe(trigger_states={}))


def test_an_unknown_tgenabled_code_raises_rather_than_being_ignored() -> None:
    """Silently skipping a state we cannot classify would be a hole shaped like a filter."""
    with pytest.raises(MalformedSchemaProbe, match="unknown tgenabled codes"):
        describe_schema_divergence(_probe(trigger_states={**_HEALTHY, "x": "Z"}))


def test_an_unread_replication_role_raises() -> None:
    with pytest.raises(MalformedSchemaProbe, match="did not read session_replication_role"):
        describe_schema_divergence(_probe(session_replication_role=None))


def test_a_malformed_probe_never_returns_none() -> None:
    """The summary property: no unusable reading may produce the clean verdict."""
    unusable = [
        SchemaProbe(failure="boom"),
        SchemaProbe(session_replication_role="origin"),
        _probe(trigger_states={}),
        _probe(trigger_states={"x": "Z"}),
        _probe(session_replication_role=None),
    ]

    for probe in unusable:
        with pytest.raises(MalformedSchemaProbe):
            describe_schema_divergence(probe)


# ---------------------------------------------------------------------------
# The guard must be reached
# ---------------------------------------------------------------------------


def _run_migrations_call_order() -> list[str]:
    import ast

    source = (_REPO_ROOT / "tests" / "integration" / "conftest.py").read_text()
    fixtures = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "run_migrations"
    ]
    assert len(fixtures) == 1
    return [
        node.func.id
        for node in ast.walk(fixtures[0])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]


def test_run_migrations_checks_the_schema_after_it_migrates() -> None:
    """Order is the contract: before the upgrade, a virgin database has no triggers at all.

    Run this check first and every fresh CI service container would be refused
    for the crime of being empty.
    """
    calls = _run_migrations_call_order()

    assert "_assert_schema_matches_the_migration_chain" in calls, (
        "run_migrations no longer checks the schema — the guard is inert"
    )
    assert calls.index("_run_alembic_upgrade") < calls.index(
        "_assert_schema_matches_the_migration_chain"
    )
