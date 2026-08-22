"""Setup guard: name the migration-test residue instead of letting it masquerade.

``tests/integration/db/test_migration_*.py`` deliberately downgrade the shared
test database to prove that the fail-closed migration guards actually close.
Those tests are correct. Their isolation is not:

1. they hold no lock, while several files migrate the same shared database;
2. their cleanup lives in a ``finally``, which is not crash-resistant by
   construction — nothing runs after ``kill -9``;
3. and the resulting breakage is **silent and deferred**. It does not fail the
   run that caused it: it fails the NEXT one, historically with ``artifact
   provenance is ambiguous`` — a message about data corruption raised over a
   test leftover. That cost 352 setup errors and two people's morning on
   2026-08-22.

This module attacks (3) and only (3). A consultative lock for (1) and an
ephemeral database per migration test — which would close all three — are the
right follow-ups, and they must not delay this one. Turning a mystery into a
message is worth more than preventing one occurrence in two.

**The guard says, it does not repair.** Repairing the schema automatically at
setup would hide the problem again, and this time nobody would know it exists.

Everything here is pure and side-effect free at import time so the message can
be unit-tested without a database.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Files under tests/integration/db/ that downgrade the shared database.
# Kept as a constant so the failure message can name them, and pinned by
# tests/unit/test_integration_schema_residue_guard.py so a new downgrading
# migration test cannot silently drop out of it.
DOWNGRADING_TEST_FILES: tuple[str, ...] = (
    "tests/integration/db/test_migration_025.py",
    "tests/integration/db/test_migration_026.py",
    "tests/integration/db/test_migration_037.py",
    "tests/integration/db/test_migration_039_project_context_timestamp.py",
)

# Tables probed for leftover rows, in the order the message lists them.
# Restricted to tables that exist across the revisions those tests downgrade to.
RESIDUE_TABLES: tuple[str, ...] = ("brain_sessions", "project_contexts")

_BREADCRUMB_DIRNAME = ".pytest_cache"
_BREADCRUMB_PREFIX = "brain_v42_migration_breadcrumb"


@dataclass(frozen=True)
class ResidueProbe:
    """Leftover ``integ-``-prefixed rows, or the reason they could not be counted."""

    counts: Mapping[str, int] | None = None
    failure: str | None = None

    @property
    def total(self) -> int:
        return sum(self.counts.values()) if self.counts else 0


@dataclass(frozen=True)
class Breadcrumb:
    """Trace dropped by a migration test before it downgrades the database.

    Written before the downgrade and removed by the same ``finally`` that
    restores the schema, so a surviving file means the run did not finish. It
    exists to answer the question the 2026-08-22 incident could not: *which*
    run broke the database, and was it a crash or a concurrent run?
    """

    test_nodeid: str
    pid: int
    started_at: str
    downgraded_to: str
    restores_to: str
    path: Path
    pid_alive: bool | None = None


def breadcrumb_dir(project_root: Path) -> Path:
    """Directory holding breadcrumbs — pytest-owned and already gitignored."""
    return project_root / _BREADCRUMB_DIRNAME


def _breadcrumb_path(project_root: Path, pid: int) -> Path:
    # The pid is part of the name so parallel workers cannot clobber each
    # other's trace — two surviving breadcrumbs then mean concurrency, not a
    # crash, and the message can say so.
    return breadcrumb_dir(project_root) / f"{_BREADCRUMB_PREFIX}.{pid}.json"


@contextmanager
def migration_breadcrumb(
    *,
    project_root: Path,
    test_nodeid: str,
    downgraded_to: str,
    restores_to: str = "head",
) -> Iterator[None]:
    """Record who is about to downgrade the shared database, and clear it after.

    A ``kill -9`` between the downgrade and the cleanup leaves the file behind;
    the next setup then names the culprit instead of guessing.
    """
    path = _breadcrumb_path(project_root, os.getpid())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "test_nodeid": test_nodeid,
                "pid": os.getpid(),
                "started_at": datetime.now(UTC).isoformat(),
                "downgraded_to": downgraded_to,
                "restores_to": restores_to,
            },
            indent=2,
        )
    )
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool | None:
    """True/False when observable, None when the answer cannot be established."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but belongs to someone else.
        return True
    except OSError:
        return None
    return True


def read_breadcrumbs(project_root: Path) -> list[Breadcrumb]:
    """Return every surviving breadcrumb, newest last. Unreadable files are skipped."""
    directory = breadcrumb_dir(project_root)
    if not directory.is_dir():
        return []
    found: list[Breadcrumb] = []
    for path in sorted(directory.glob(f"{_BREADCRUMB_PREFIX}.*.json")):
        try:
            payload = json.loads(path.read_text())
            pid = int(payload["pid"])
            found.append(
                Breadcrumb(
                    test_nodeid=str(payload["test_nodeid"]),
                    pid=pid,
                    started_at=str(payload["started_at"]),
                    downgraded_to=str(payload["downgraded_to"]),
                    restores_to=str(payload["restores_to"]),
                    path=path,
                    pid_alive=_pid_alive(pid),
                )
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(found, key=lambda crumb: crumb.started_at)


def _residue_block(residue: ResidueProbe) -> str:
    if residue.failure is not None:
        return (
            "Leftover rows could NOT be probed: "
            f"{residue.failure}\n"
            "    Treat the cause as unknown — do not read this as 'the database is clean'."
        )
    if residue.total:
        assert residue.counts is not None
        lines = [
            f"    {table:<20}: {count} row(s) with project_key LIKE 'integ-%'"
            for table, count in residue.counts.items()
            if count
        ]
        return "Leftover integration rows found — this is the smoking gun:\n" + "\n".join(lines)
    return (
        "No leftover 'integ-%' rows were found. Two causes remain, and this guard\n"
        "    cannot tell them apart. Do not assume either one:\n"
        "      - an interrupted migration test whose seeded rows were already cleaned up;\n"
        "      - a migration that landed in the repository since this database was last\n"
        "        migrated, in which case the upgrade below is all that is needed."
    )


def _breadcrumb_block(breadcrumbs: Sequence[Breadcrumb]) -> str:
    if not breadcrumbs:
        return (
            "No breadcrumb survived, so the interrupted run cannot be attributed.\n"
            "    Breadcrumbs only exist for runs started after this guard landed."
        )
    live = [crumb for crumb in breadcrumbs if crumb.pid_alive is True]
    header = (
        f"{len(breadcrumbs)} migration-test breadcrumb(s) survived — "
        "an interrupted or in-flight run:"
    )
    lines = [header]
    for crumb in breadcrumbs:
        if crumb.pid_alive is True:
            liveness = "STILL RUNNING — this is a concurrent run, not a crash"
        elif crumb.pid_alive is False:
            liveness = "no longer running (a pid can be reused; treat as indicative)"
        else:
            liveness = "liveness unknown"
        lines.extend(
            [
                f"    test          : {crumb.test_nodeid}",
                f"    pid           : {crumb.pid} ({liveness})",
                f"    started at    : {crumb.started_at}",
                f"    downgraded to : {crumb.downgraded_to}, meant to restore: {crumb.restores_to}",
                f"    breadcrumb    : {crumb.path}",
            ]
        )
    if live:
        lines.append(
            "    Another run is holding this database RIGHT NOW. Do not repair it —\n"
            "    wait for that run to finish, then re-run this one."
        )
    else:
        lines.append("    Delete the breadcrumb file(s) once the database is repaired.")
    return "\n".join(lines)


def describe_schema_residue(
    *,
    deployed_revision: str | None,
    expected_head: str,
    residue: ResidueProbe,
    breadcrumbs: Sequence[Breadcrumb] = (),
) -> str | None:
    """Return the refusal message, or ``None`` when setup may proceed silently.

    ``deployed_revision is None`` means the database carries no
    ``alembic_version`` row at all: a virgin database that the session fixture
    is there to bootstrap. That is NOT the residue case and must stay allowed,
    otherwise a fresh CI service container could never migrate itself.
    """
    if deployed_revision is None:
        return None
    if deployed_revision == expected_head:
        return None

    named_files = "\n".join(f"      {path}" for path in DOWNGRADING_TEST_FILES)
    return (
        "Integration setup refused: the test database is NOT at the expected Alembic head.\n"
        "\n"
        f"    measured revision : {deployed_revision}\n"
        f"    expected head     : {expected_head}  (single head under alembic/versions)\n"
        "\n"
        "    This is most likely a RESIDUE, not a schema waiting to be migrated. These\n"
        "    tests deliberately downgrade this shared database and restore it in a\n"
        "    `finally`:\n"
        f"{named_files}\n"
        "    A `finally` does not survive kill -9, an OOM kill, or a hard cancel. When one\n"
        "    of those runs is interrupted between its downgrade and its cleanup, the\n"
        "    database stays behind and every LATER run fails — historically with\n"
        "    `artifact provenance is ambiguous`, which describes data corruption while the\n"
        "    real cause is a test leftover.\n"
        "\n"
        f"    {_residue_block(residue)}\n"
        "\n"
        f"    {_breadcrumb_block(breadcrumbs)}\n"
        "\n"
        "    This guard does NOT repair the database, on purpose: repairing it here would\n"
        "    hide the problem again. Repair it deliberately, against the TEST database\n"
        "    only — never against the production one:\n"
        "\n"
        '        POSTGRES_URL="$BRAIN_V42_TEST_DB_URL" python -m alembic upgrade head\n'
        "\n"
        "    Delete any leftover rows listed above before re-running."
    )
