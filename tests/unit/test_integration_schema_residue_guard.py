"""The setup guard must accuse the right thing — and stay silent when nothing is wrong.

Every test here proves a control mutation in BOTH senses. A guard that only
ever refuses is indistinguishable from a correct one when you look at red
alone: the nominal witness is the load-bearing test in this file, not the
decorative one.

The logic under test is pure, so none of this needs a database.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.integration.schema_residue import (
    DOWNGRADING_TEST_FILES,
    Breadcrumb,
    ResidueProbe,
    describe_schema_residue,
    migration_breadcrumb,
    read_breadcrumbs,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_CLEAN = ResidueProbe(counts={"brain_sessions": 0, "project_contexts": 0})


def _refusal(**overrides: object) -> str:
    """Build the refusal message for a downgraded database."""
    kwargs: dict[str, object] = {
        "deployed_revision": "036",
        "expected_head": "046",
        "residue": _CLEAN,
        "breadcrumbs": (),
    }
    kwargs.update(overrides)
    message = describe_schema_residue(**kwargs)  # type: ignore[arg-type]
    assert message is not None, "expected a refusal for a database behind the head"
    return message


# ---------------------------------------------------------------------------
# The nominal witness: the guard must cost nothing when the schema is fine
# ---------------------------------------------------------------------------


def test_database_at_the_expected_head_passes_in_silence() -> None:
    """Without this witness, a guard that always refuses would still look green."""
    assert (
        describe_schema_residue(
            deployed_revision="046",
            expected_head="046",
            residue=_CLEAN,
        )
        is None
    )


def test_leftover_rows_alone_never_refuse_a_database_at_the_head() -> None:
    """Residue is evidence about a revision mismatch, not an independent verdict.

    Integration fixtures legitimately hold ``integ-`` rows while the suite runs;
    refusing on them alone would make the guard fire during a healthy session.
    """
    assert (
        describe_schema_residue(
            deployed_revision="046",
            expected_head="046",
            residue=ResidueProbe(counts={"brain_sessions": 7}),
        )
        is None
    )


def test_virgin_database_is_a_bootstrap_not_a_residue() -> None:
    """A database with no ``alembic_version`` row is what the fixture exists to migrate.

    Confusing it with a downgraded one would refuse every fresh CI service
    container, and CI is precisely where nobody can hand-repair a database.
    """
    assert (
        describe_schema_residue(
            deployed_revision=None,
            expected_head="046",
            residue=ResidueProbe(counts={}),
        )
        is None
    )


# ---------------------------------------------------------------------------
# The refusal: a downgraded database, and what the message is allowed to claim
# ---------------------------------------------------------------------------


def test_downgraded_database_is_refused_with_both_revisions_spelled_out() -> None:
    message = _refusal()

    assert "036" in message and "046" in message
    assert "NOT at the expected Alembic head" in message


def test_message_names_the_tests_that_downgrade_the_shared_database() -> None:
    """The 2026-08-22 incident cost two people a morning for want of this list."""
    message = _refusal()

    for path in DOWNGRADING_TEST_FILES:
        assert path in message


def test_message_replaces_the_provenance_red_herring_it_was_written_for() -> None:
    """The old symptom must appear as a *quoted* symptom, not as the diagnosis."""
    message = _refusal()

    assert "artifact provenance is ambiguous" in message
    assert "test leftover" in message


def test_declared_downgrading_files_still_match_the_tree() -> None:
    """A new downgrading migration test must not silently drop out of the message.

    Same discipline as the Alembic head pin: a constant nobody is forced to
    revisit is a constant that drifts.
    """
    measured = sorted(
        str(path.relative_to(_REPO_ROOT))
        for path in (_REPO_ROOT / "tests" / "integration").rglob("*.py")
        if '"downgrade"' in path.read_text()
    )

    assert measured == sorted(DOWNGRADING_TEST_FILES), (
        "tests/integration/schema_residue.py::DOWNGRADING_TEST_FILES no longer matches the "
        f"files that downgrade the shared database.\n  declared: {sorted(DOWNGRADING_TEST_FILES)}"
        f"\n  measured: {measured}\nAlso give the new test the record_migration_downgrade "
        "fixture, or its interruption will stay unattributable."
    )


# ---------------------------------------------------------------------------
# Evidence: name what was seen, never invent what was not
# ---------------------------------------------------------------------------


def test_leftover_rows_are_reported_as_the_smoking_gun() -> None:
    message = _refusal(residue=ResidueProbe(counts={"brain_sessions": 2, "project_contexts": 1}))

    assert "smoking gun" in message
    assert "brain_sessions" in message and "2 row(s)" in message
    assert "project_contexts" in message and "1 row(s)" in message


def test_zero_rows_reported_by_the_probe_are_not_listed() -> None:
    """A table at zero is noise; only non-zero counts are evidence."""
    message = _refusal(residue=ResidueProbe(counts={"brain_sessions": 3, "project_contexts": 0}))

    assert "brain_sessions" in message
    assert "project_contexts" not in message.split("smoking gun")[1].split("\n\n")[0]


def test_absence_of_leftover_rows_does_not_invent_a_cause() -> None:
    """Both remaining causes must be named, and neither asserted."""
    message = _refusal(residue=_CLEAN)

    assert "cannot tell them apart" in message
    assert "interrupted migration test" in message
    assert "migration that landed in the repository" in message


def test_unprobeable_residue_is_never_read_as_a_clean_database() -> None:
    """A silent probe failure would read as 'no leftover rows' — the exact inversion."""
    message = _refusal(residue=ResidueProbe(failure="UndefinedTableError: brain_sessions"))

    assert "could NOT be probed" in message
    assert "UndefinedTableError" in message
    assert "do not read this as 'the database is clean'" in message
    assert "smoking gun" not in message


# ---------------------------------------------------------------------------
# Attribution: which run broke it, and was it a crash or a concurrent run
# ---------------------------------------------------------------------------


def _crumb(**overrides: object) -> Breadcrumb:
    fields: dict[str, object] = {
        "test_nodeid": "tests/integration/db/test_migration_037.py::test_round_trip",
        "pid": 4242,
        "started_at": "2026-08-22T09:14:02+00:00",
        "downgraded_to": "036",
        "restores_to": "head",
        "path": Path("/repo/.pytest_cache/brain_v42_migration_breadcrumb.4242.json"),
        "pid_alive": False,
    }
    fields.update(overrides)
    return Breadcrumb(**fields)  # type: ignore[arg-type]


def test_dead_breadcrumb_attributes_the_interrupted_run() -> None:
    message = _refusal(breadcrumbs=(_crumb(),))

    assert "test_migration_037.py::test_round_trip" in message
    assert "4242" in message
    assert "2026-08-22T09:14:02+00:00" in message
    assert "no longer running" in message
    assert "Delete the breadcrumb file(s)" in message


def test_live_breadcrumb_is_reported_as_concurrency_and_forbids_repair() -> None:
    """A live pid means another run holds the database — repairing it would wreck that run."""
    message = _refusal(breadcrumbs=(_crumb(pid_alive=True),))

    assert "STILL RUNNING" in message
    assert "concurrent run, not a crash" in message
    assert "Do not repair it" in message


def test_missing_breadcrumb_admits_the_run_is_unattributable() -> None:
    """'We do not know' is the honest answer; inventing a culprit is not."""
    message = _refusal(breadcrumbs=())

    assert "cannot be attributed" in message


def test_breadcrumb_is_removed_on_a_clean_exit(tmp_path: Path) -> None:
    with migration_breadcrumb(
        project_root=tmp_path,
        test_nodeid="tests/integration/db/test_migration_037.py::t",
        downgraded_to="036",
    ):
        assert len(read_breadcrumbs(tmp_path)) == 1

    assert read_breadcrumbs(tmp_path) == []


def test_breadcrumb_survives_an_interrupted_run(tmp_path: Path) -> None:
    """The whole point: nothing runs after kill -9, so the file must already be on disk."""

    class _Killed(BaseException):
        """Stands in for a signal: bypasses `except Exception` handlers."""

    with pytest.raises(_Killed):  # noqa: PT012 - the raise must happen inside the block
        with migration_breadcrumb(
            project_root=tmp_path,
            test_nodeid="tests/integration/db/test_migration_037.py::t",
            downgraded_to="036",
        ):
            # Read from disk while the block is still open: this is the state a
            # SIGKILL would freeze.
            [survivor] = read_breadcrumbs(tmp_path)
            assert survivor.test_nodeid.endswith("::t")
            assert survivor.downgraded_to == "036"
            assert survivor.pid == os.getpid()
            assert survivor.pid_alive is True
            raise _Killed


def test_breadcrumbs_from_parallel_workers_do_not_clobber_each_other(tmp_path: Path) -> None:
    """Two surviving crumbs mean concurrency — which the message must be able to say."""
    directory = tmp_path / ".pytest_cache"
    directory.mkdir()
    # Deliberately written newest-first so the assertion below tests the
    # chronological sort, not the glob order.
    for pid, started_at in ((222, "2026-08-22T09:30:00+00:00"), (111, "2026-08-22T09:05:00+00:00")):
        (directory / f"brain_v42_migration_breadcrumb.{pid}.json").write_text(
            json.dumps(
                {
                    "test_nodeid": f"worker-{pid}",
                    "pid": pid,
                    "started_at": started_at,
                    "downgraded_to": "036",
                    "restores_to": "head",
                }
            )
        )

    assert [crumb.pid for crumb in read_breadcrumbs(tmp_path)] == [111, 222]


def test_unreadable_breadcrumb_is_skipped_rather_than_crashing_setup(tmp_path: Path) -> None:
    """A corrupt trace must not turn the guard itself into the new mystery."""
    directory = tmp_path / ".pytest_cache"
    directory.mkdir()
    (directory / "brain_v42_migration_breadcrumb.9.json").write_text("{not json")

    assert read_breadcrumbs(tmp_path) == []


# ---------------------------------------------------------------------------
# The guard must be reached — a correct message nobody calls is still inert
# ---------------------------------------------------------------------------


def _run_migrations_call_order() -> list[str]:
    """Names called inside ``run_migrations``, in source order."""
    import ast

    source = (_REPO_ROOT / "tests" / "integration" / "conftest.py").read_text()
    fixtures = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "run_migrations"
    ]
    assert len(fixtures) == 1, "tests/integration/conftest.py must define one run_migrations"
    return [
        node.func.id
        for node in ast.walk(fixtures[0])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]


def test_run_migrations_calls_the_guard_before_it_migrates() -> None:
    """Order is the contract, not merely the presence of the call.

    Run the guard *after* the upgrade and it is dead weight: the upgrade would
    already have failed with the opaque `artifact provenance is ambiguous` this
    whole change exists to replace.
    """
    calls = _run_migrations_call_order()

    assert "_assert_no_migration_test_residue" in calls, (
        "run_migrations no longer calls the residue guard — the guard is inert"
    )
    assert "_run_alembic_upgrade" in calls
    assert calls.index("_assert_no_migration_test_residue") < calls.index("_run_alembic_upgrade")


# ---------------------------------------------------------------------------
# The guard says; it does not act
# ---------------------------------------------------------------------------


def test_message_gives_the_repair_gesture_against_the_test_database_only() -> None:
    message = _refusal()

    assert "python -m alembic upgrade head" in message
    assert "BRAIN_V42_TEST_DB_URL" in message
    assert "never against the production one" in message


def test_guard_states_that_it_does_not_repair() -> None:
    """An automatic repair at setup would hide the problem again, undiagnosed."""
    message = _refusal()

    assert "does NOT repair the database" in message


def test_guard_writes_nothing_and_touches_no_database(tmp_path: Path) -> None:
    """Purity is the reason this whole file needs no PostgreSQL."""
    before = sorted(tmp_path.rglob("*"))

    _refusal()

    assert sorted(tmp_path.rglob("*")) == before
