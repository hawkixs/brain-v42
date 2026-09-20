"""The French briefing lines of a fact: policy-driven, never more than the probe proved."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from brain_v42.facts.model import (
    FactTarget,
    HostIdentity,
    Measured,
    ReleaseIdentity,
    SourceIdentity,
    Unreadable,
)
from brain_v42.facts.probe import FactDescriptor
from brain_v42.facts.render import render_fact_line

_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
_IDENTITY = SourceIdentity("7612696091383607335", "brain", "172.31.0.4", 5432)
_DESCRIPTOR = FactDescriptor(
    name="graph_projection_lag",
    definition_version=1,
    target=FactTarget.PRODUCTION,
    ttl_seconds=15,
    timeout_seconds=3,
    queue_timeout_seconds=2,
    deadline_seconds=5,
    briefing=True,
    policies={"late_after_seconds": 300},
    value_schema={"pending": "int"},
)
_ALEMBIC_DESCRIPTOR = FactDescriptor(
    name="alembic_head",
    definition_version=1,
    target=FactTarget.PRODUCTION,
    ttl_seconds=60,
    timeout_seconds=3,
    queue_timeout_seconds=2,
    deadline_seconds=5,
    briefing=True,
    policies={},
    value_schema={"revision": "string"},
)

_DREAM_DESCRIPTOR = FactDescriptor(
    name="dream_killswitches_declared",
    definition_version=1,
    target=FactTarget.HOST,
    ttl_seconds=60,
    timeout_seconds=1,
    queue_timeout_seconds=2,
    deadline_seconds=3,
    briefing=True,
    policies={},
    value_schema={
        "promote": "string",
        "reorg": "string",
        "reorg_dry": "string",
        "extract": "string",
        "extract_dry": "string",
        "roadmap": "string",
        "roadmap_dry": "string",
        "sweep": "string",
        "sweep_dry": "string",
        "file_mtime_epoch": "int",
    },
)


def _dream_measured(**overrides: object) -> Measured:
    value: dict[str, object] = {
        "promote": "true",
        "reorg": "true",
        "reorg_dry": "false",
        "extract": "true",
        "extract_dry": "false",
        "roadmap": "false",
        "roadmap_dry": "true",
        "sweep": "true",
        "sweep_dry": "false",
        "file_mtime_epoch": int(datetime(2026, 9, 15, 14, 10, tzinfo=UTC).timestamp()),
    }
    value.update(overrides)
    return Measured.from_value(
        fact="dream_killswitches_declared",
        definition_version=1,
        target=FactTarget.HOST,
        source=HostIdentity("host-a"),
        value=value,
        observation_id=uuid4(),
        measured_at=_NOW,
        duration_ms=1,
        ttl_seconds=60,
    )


def _dream_unreadable(code: str) -> Unreadable:
    return Unreadable(
        fact="dream_killswitches_declared",
        definition_version=1,
        target=FactTarget.HOST,
        error_code=code,
        where=None,
        observation_id=uuid4(),
        measured_at=_NOW,
        duration_ms=1,
        ttl_seconds=60,
        source_kind="probe",
    )


def _measured(**overrides: object) -> Measured:
    value: dict[str, object] = {
        "pending": 0,
        "ready": 0,
        "claimed": 0,
        "exhausted": 0,
        "lag_seconds": 0,
        "generation": 93,
        "armed": True,
        "lease_active": True,
        "recovery_active": False,
        "healthy": True,
    }
    value.update(overrides)
    return Measured.from_value(
        fact="graph_projection_lag",
        definition_version=1,
        target=FactTarget.PRODUCTION,
        source=_IDENTITY,
        value=value,
        observation_id=uuid4(),
        measured_at=_NOW,
        duration_ms=12,
        ttl_seconds=15,
    )


def _unreadable(code: str) -> Unreadable:
    return Unreadable(
        fact="graph_projection_lag",
        definition_version=1,
        target=FactTarget.PRODUCTION,
        error_code=code,
        where=None,
        observation_id=uuid4(),
        measured_at=_NOW,
        duration_ms=3000,
        ttl_seconds=15,
        source_kind="probe",
    )


def _alembic_measured() -> Measured:
    """Build a stamped schema measurement with the production identity."""
    return Measured.from_value(
        fact="alembic_head",
        definition_version=1,
        target=FactTarget.PRODUCTION,
        source=_IDENTITY,
        value={"revision": "054"},
        observation_id=uuid4(),
        measured_at=_NOW,
        duration_ms=12,
        ttl_seconds=60,
    )


def _alembic_unreadable(code: str) -> Unreadable:
    """Build a failed schema measurement without replacing its error code."""
    return Unreadable(
        fact="alembic_head",
        definition_version=1,
        target=FactTarget.PRODUCTION,
        error_code=code,
        where=None,
        observation_id=uuid4(),
        measured_at=_NOW,
        duration_ms=3000,
        ttl_seconds=60,
        source_kind="probe",
    )


def test_a_quiet_projection_says_what_was_proved_and_nothing_more() -> None:
    """Outbox and lease, not Neo4j content: 'à jour' never appears."""
    line = render_fact_line(_measured(), _DESCRIPTOR, age_seconds=None)
    assert line == (
        "- Projection graphe : aucun retard observé dans l'outbox — "
        "0 en attente, génération 93 armée, bail tenu"
    )
    assert "à jour" not in line


@pytest.mark.parametrize(
    "overrides,expected",
    [
        (
            {
                "pending": 114,
                "lag_seconds": 4 * 86400 + 2 * 3600,
                "armed": False,
                "healthy": False,
                "generation": 70,
            },
            "- Projection graphe : EN RETARD de 4j 2h — 114 en attente, génération 70 NON armée, bail tenu",
        ),
        (
            {"pending": 3, "lag_seconds": 301, "generation": 93},
            "- Projection graphe : EN RETARD de 5 min — 3 en attente, génération 93 armée, bail tenu",
        ),
        (
            {"exhausted": 3},
            "- Projection graphe : EN RETARD — 0 en attente, génération 93 armée, bail tenu, 3 épuisées",
        ),
        (
            {"lease_active": False, "healthy": False},
            "- Projection graphe : EN RETARD — 0 en attente, génération 93 armée, bail perdu",
        ),
        (
            {"generation": None, "armed": False, "lease_active": False, "healthy": False},
            "- Projection graphe : EN RETARD — 0 en attente, sans bail",
        ),
        (
            {"recovery_active": True, "healthy": False},
            "- Projection graphe : EN RETARD — 0 en attente, génération 93 armée, bail tenu, "
            "récupération en cours",
        ),
    ],
)
def test_the_loud_form_is_driven_by_the_declared_policy_and_by_health(
    overrides: dict[str, object], expected: str
) -> None:
    assert render_fact_line(_measured(**overrides), _DESCRIPTOR, age_seconds=None) == expected


def test_lag_at_the_policy_bound_is_quiet_and_one_second_over_is_loud() -> None:
    quiet = render_fact_line(_measured(lag_seconds=300, pending=1), _DESCRIPTOR, age_seconds=None)
    loud = render_fact_line(_measured(lag_seconds=301, pending=1), _DESCRIPTOR, age_seconds=None)
    assert quiet.startswith("- Projection graphe : aucun retard observé")
    assert loud.startswith("- Projection graphe : EN RETARD de 5 min")


@pytest.mark.parametrize(
    "code,expected",
    [
        ("timeout", "- Projection graphe : illisible (timeout)"),
        ("target_mismatch", "- Projection graphe : illisible (cible inattendue)"),
        ("identity_unreadable", "- Projection graphe : illisible (identity_unreadable)"),
    ],
)
def test_unreadable_renders_and_names_the_one_case_that_is_never_transient(
    code: str, expected: str
) -> None:
    assert render_fact_line(_unreadable(code), _DESCRIPTOR, age_seconds=None) == expected


def test_a_cached_reading_older_than_a_minute_says_its_age() -> None:
    fresh = render_fact_line(_measured(), _DESCRIPTOR, age_seconds=59)
    old = render_fact_line(_measured(), _DESCRIPTOR, age_seconds=180)
    assert not fresh.endswith(")")
    assert old.endswith(" (mesuré il y a 3 min)")


def test_an_unknown_fact_renders_generically_and_bounded() -> None:
    other = FactDescriptor(
        name="something_else",
        definition_version=1,
        target=FactTarget.PRODUCTION,
        ttl_seconds=15,
        timeout_seconds=3,
        queue_timeout_seconds=2,
        deadline_seconds=5,
        briefing=True,
        policies={},
        value_schema={"k": "string"},
    )
    measured = Measured.from_value(
        fact="something_else",
        definition_version=1,
        target=FactTarget.PRODUCTION,
        source=_IDENTITY,
        value={"k": "x" * 300},
        observation_id=uuid4(),
        measured_at=_NOW,
        duration_ms=1,
        ttl_seconds=15,
    )
    line = render_fact_line(measured, other, age_seconds=None)
    assert line.startswith("- something_else : {")
    assert len(line) <= len("- something_else : ") + 120 + 1
    assert line.endswith("…")


def test_declared_dream_killswitches_render_the_canonical_phase_words() -> None:
    """The line distinguishes enabled phases from their wet execution mode."""
    assert render_fact_line(_dream_measured(), _DREAM_DESCRIPTOR, age_seconds=None) == (
        "- Killswitches déclarés : PROMOTE on, REORG on wet, EXTRACT on wet, "
        "ROADMAP off, SWEEP on wet (drop-in modifié le 2026-09-15 14:10 UTC)"
    )


def test_declared_dream_killswitches_keep_a_noncanonical_dry_value_visible() -> None:
    """A typo is safe because the rail runs dry, but must not look intentional."""
    assert render_fact_line(
        _dream_measured(reorg_dry="True"), _DREAM_DESCRIPTOR, age_seconds=None
    ) == (
        "- Killswitches déclarés : PROMOTE on, REORG on 'True' (illisible → dry), "
        "EXTRACT on wet, ROADMAP off, SWEEP on wet "
        "(drop-in modifié le 2026-09-15 14:10 UTC)"
    )


def test_an_unreadable_dream_killswitch_drop_in_uses_its_operator_subject() -> None:
    """An absent fact line must name its domain instead of disappearing into a generic label."""
    assert render_fact_line(
        _dream_unreadable("probe_error"), _DREAM_DESCRIPTOR, age_seconds=None
    ) == ("- Killswitches déclarés : illisible (probe_error)")


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        (
            "live_release_sha",
            {"release_sha": "b4f7194d84c153dad11ee249ec9eca8951056651", "package_version": "0.6.0"},
            "- Release vivante : b4f7194d (paquet 0.6.0)",
        ),
        ("alembic_head_shipped", {"revision": "054"}, "- Tête Alembic livrée : 054"),
    ],
)
def test_release_fact_lines_render_their_measured_values(
    name: str, value: dict[str, str], expected: str
) -> None:
    descriptor = FactDescriptor(
        name=name,
        definition_version=1,
        target=FactTarget.LIVE_RELEASE,
        ttl_seconds=3650 * 86400,
        timeout_seconds=1,
        queue_timeout_seconds=2,
        deadline_seconds=3,
        briefing=True,
        policies={},
        value_schema=dict.fromkeys(value, "string"),
    )
    measured = Measured.from_value(
        fact=name,
        definition_version=1,
        target=FactTarget.LIVE_RELEASE,
        source=ReleaseIdentity("b4f7194d84c153dad11ee249ec9eca8951056651", "0.6.0"),
        value=value,
        observation_id=uuid4(),
        measured_at=_NOW,
        duration_ms=1,
        ttl_seconds=3650 * 86400,
    )

    assert render_fact_line(measured, descriptor, age_seconds=None) == expected


@pytest.mark.parametrize(
    ("name", "code", "expected"),
    [
        (
            "live_release_sha",
            "identity_unreadable",
            "- Release vivante : illisible (identity_unreadable)",
        ),
        ("alembic_head_shipped", "probe_error", "- Tête Alembic livrée : illisible (probe_error)"),
        (
            "graph_projection_lag",
            "identity_unreadable",
            "- Projection graphe : illisible (identity_unreadable)",
        ),
    ],
)
def test_release_fact_unreadable_lines_name_their_subject(
    name: str, code: str, expected: str
) -> None:
    descriptor = FactDescriptor(
        name=name,
        definition_version=1,
        target=FactTarget.LIVE_RELEASE if name != "graph_projection_lag" else FactTarget.PRODUCTION,
        ttl_seconds=1,
        timeout_seconds=1,
        queue_timeout_seconds=2,
        deadline_seconds=3,
        briefing=True,
        policies={},
        value_schema={},
    )
    unreadable = Unreadable(
        fact=name,
        definition_version=1,
        target=descriptor.target,
        error_code=code,
        where=None,
        observation_id=uuid4(),
        measured_at=_NOW,
        duration_ms=1,
        ttl_seconds=1,
        source_kind="probe",
    )

    assert render_fact_line(unreadable, descriptor, age_seconds=None) == expected


@pytest.mark.parametrize(
    ("measurement", "expected"),
    [
        (_alembic_measured(), "- Schéma : 054"),
        (_alembic_unreadable("timeout"), "- Schéma : illisible (timeout)"),
        (
            _alembic_unreadable("target_mismatch"),
            "- Schéma : illisible (cible inattendue)",
        ),
    ],
)
def test_alembic_head_renders_the_legacy_schema_line_or_its_visible_failure(
    measurement: Measured | Unreadable, expected: str
) -> None:
    """The fact replaces one legacy line without changing a reader's wording."""
    assert render_fact_line(measurement, _ALEMBIC_DESCRIPTOR, age_seconds=None) == expected


def test_a_long_raw_killswitch_value_is_cut_in_the_line() -> None:
    """A 3 000-character typo is the operator's problem to fix, not the briefing's
    to reproduce: the raw value is shown, cut to 40 characters, with an ellipsis."""
    line = render_fact_line(
        _dream_measured(reorg_dry="T" * 100), _DREAM_DESCRIPTOR, age_seconds=None
    )
    assert "'" + "T" * 40 + "…' (illisible → dry)" in line
    assert "T" * 41 not in line


def test_an_absurd_drop_in_mtime_renders_as_unreadable_date_not_an_exception() -> None:
    """`datetime.fromtimestamp` overflows on an epoch far in the future; a
    renderer that raised would take every other fact line down with it."""
    line = render_fact_line(
        _dream_measured(file_mtime_epoch=99_999_999_999_999), _DREAM_DESCRIPTOR, age_seconds=None
    )
    assert line.startswith("- Killswitches déclarés : PROMOTE on, REORG on wet")
    assert line.endswith("(drop-in modifié à une date illisible)")


def test_an_undeclared_killswitch_key_renders_as_undeclared_not_as_a_typo() -> None:
    """Measured on the live drop-in (2026-09-20): the ROADMAP keys are absent since
    the phase retired on 2026-09-10, so the fact carries `""` for them. Absent is
    not illegible — dream.sh then runs the code default — and the line must say so."""
    line = render_fact_line(
        _dream_measured(roadmap="", roadmap_dry=""), _DREAM_DESCRIPTOR, age_seconds=None
    )
    assert "ROADMAP off (non déclaré)" in line
    assert "illisible → off" not in line
    # An undeclared dry key on an enabled phase reads the same way.
    line = render_fact_line(_dream_measured(reorg_dry=""), _DREAM_DESCRIPTOR, age_seconds=None)
    assert "REORG on (mode non déclaré → dry)" in line
