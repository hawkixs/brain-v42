"""The setup guard must be WIRED, not merely written.

``tests/unit/test_integration_schema_residue_guard.py`` proves the message. It
cannot prove that anything calls it — and a guard nobody calls is the exact
failure mode this project keeps hitting: green, and inert. These tests exercise
the fixture's own entry point against the live test database.

They never migrate anything. The revision is read, never written: a test that
downgraded the shared database to prove a guard about downgrades would be the
joke writing itself.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import (
    _PROJECT_ROOT,
    _assert_no_migration_test_residue,
    _expected_alembic_head,
    _probe_schema_state,
)
from tests.integration.schema_residue import ResidueProbe, describe_schema_residue

pytestmark = pytest.mark.integration


def test_the_live_database_is_at_the_derived_head_and_the_guard_says_nothing(
    request: pytest.FixtureRequest,
) -> None:
    """Nominal witness, measured — not copied from a constant.

    Both sides are read at run time: the head from ``alembic/versions``, the
    revision from the database the suite just migrated.
    """
    engine = request.getfixturevalue("engine")
    expected_head = _expected_alembic_head(_PROJECT_ROOT)
    connected, revision, residue = _probe_schema_state(
        engine.url.render_as_string(hide_password=False)
    )

    assert connected
    assert revision == expected_head, (
        f"the suite left the database at {revision!r}, expected {expected_head!r}"
    )
    assert (
        describe_schema_residue(
            deployed_revision=revision,
            expected_head=expected_head,
            residue=residue,
        )
        is None
    )


def test_one_revision_of_drift_flips_the_verdict_on_the_same_measured_state(
    request: pytest.FixtureRequest,
) -> None:
    """Negative control against the real head — the only thing changed is the revision.

    Same probe, same residue, same head: only ``deployed_revision`` moves. If
    the guard passed here it would pass on the residue it exists to catch.
    """
    engine = request.getfixturevalue("engine")
    expected_head = _expected_alembic_head(_PROJECT_ROOT)
    _connected, _revision, residue = _probe_schema_state(
        engine.url.render_as_string(hide_password=False)
    )

    message = describe_schema_residue(
        deployed_revision="036",
        expected_head=expected_head,
        residue=residue,
    )

    assert message is not None
    assert "measured revision : 036" in message
    assert f"expected head     : {expected_head}" in message


def test_the_fixture_entry_point_raises_on_a_downgraded_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves the wiring: the message reaches ``run_migrations``, not just a unit test.

    The probe is replaced rather than the database, so the shared schema is
    never touched. What is under test here is the call, not the SQL.
    """
    monkeypatch.setattr(
        "tests.integration.conftest._probe_schema_state",
        lambda _url: (True, "036", ResidueProbe(counts={"brain_sessions": 2})),
    )

    with pytest.raises(RuntimeError) as caught:
        _assert_no_migration_test_residue("postgresql+asyncpg://unused/brain_test", _PROJECT_ROOT)

    assert "NOT at the expected Alembic head" in str(caught.value)
    assert "smoking gun" in str(caught.value)


def test_the_fixture_entry_point_stays_silent_on_a_healthy_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The witness that keeps the previous test honest."""
    head = _expected_alembic_head(_PROJECT_ROOT)
    monkeypatch.setattr(
        "tests.integration.conftest._probe_schema_state",
        lambda _url: (True, head, ResidueProbe(counts={"brain_sessions": 0})),
    )

    _assert_no_migration_test_residue("postgresql+asyncpg://unused/brain_test", _PROJECT_ROOT)


def test_an_unreachable_database_is_not_treated_as_residue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-regression: unreachable is the pre-existing skip path, not this guard's business.

    Blocking here would replace a clear connectivity failure with a message
    about migration residue — the same category of lie the guard was written to
    remove.
    """
    monkeypatch.setattr(
        "tests.integration.conftest._probe_schema_state",
        lambda _url: (False, None, ResidueProbe(failure="database unreachable")),
    )

    _assert_no_migration_test_residue("postgresql+asyncpg://unused/brain_test", _PROJECT_ROOT)
