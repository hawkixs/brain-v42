"""Integration test fixtures for brain_v42.

Provides async fixtures against a real PostgreSQL+pgvector instance.
The DB URL is resolved exclusively from BRAIN_V42_TEST_DB_URL. Defaults and
POSTGRES_URL fallback are intentionally absent — if the dedicated variable is
unset, or if it points at the production `brain` database, the suite skips
loudly rather than silently corrupting prod.

All tests in this suite require a running PostgreSQL instance with the
brain_v42 schema applied. Tests are skipped gracefully when the DB is
not reachable.

Migration is run once per session via Alembic subprocess (avoids asyncpg/psycopg2
driver conflicts in the Alembic env.py).

Also provides Neo4j fixtures (neo4j_driver, graph_service) that require the
dedicated BRAIN_V42_TEST_NEO4J_* variables and skip before driver construction
when they are absent. These are used by test_graph_integration.py.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import warnings
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from tests.integration.disposable_db import fresh_head_database
from tests.integration.schema_fingerprint import (
    SchemaProbe,
    describe_family_divergence,
    describe_schema_divergence,
    describe_underivable_premise,
    migrations_emitting_non_origin_trigger_state,
    probe_schema_families,
)
from tests.integration.schema_residue import (
    RESIDUE_TABLES,
    ResidueProbe,
    describe_data_residue_notice,
    describe_head_drift,
    describe_schema_residue,
    migration_breadcrumb,
    read_breadcrumbs,
)

# ---------------------------------------------------------------------------
# DB URL guard
# ---------------------------------------------------------------------------

_PROD_DB_NAME = "brain"


def _resolve_integration_db_url() -> str:
    """Resolve the integration test DB URL from environment variables.

    Only ``BRAIN_V42_TEST_DB_URL`` is accepted. ``POSTGRES_URL`` is deliberately
    ignored so a developer shell configured for a live database cannot redirect
    the integration suite.

    Raises ValueError if:
    - The dedicated test env var is unset
    - The resolved URL points at the production database (db name == 'brain')

    This is an importable helper so it can be unit-tested without triggering
    pytest.skip inside a fixture. Fixes Bug 2: the previous code defaulted to
    the live prod DB when POSTGRES_URL was unset.
    """
    url = os.environ.get("BRAIN_V42_TEST_DB_URL")
    if not url:
        raise ValueError(
            "BRAIN_V42_TEST_DB_URL is not set — skipping integration tests to avoid polluting prod"
        )
    # Validate the decoded path and reject driver options that can override it.
    # Error messages stay generic: never echo the credential-bearing DSN.
    parsed = urlparse(url)
    db_name = unquote(parsed.path).lstrip("/")
    if not db_name:
        raise ValueError("Unsafe integration DB URL — an explicit test database path is required")
    query_keys = {key.casefold() for key, _value in parse_qsl(parsed.query, keep_blank_values=True)}
    if query_keys.intersection({"database", "dbname"}):
        raise ValueError(
            "Unsafe integration DB URL — database override query parameters are forbidden"
        )
    if db_name == _PROD_DB_NAME:
        raise ValueError(
            f"Resolved URL targets the prod '{_PROD_DB_NAME}' database — "
            "skipping integration tests to avoid polluting prod. "
            "Set BRAIN_V42_TEST_DB_URL to a test database (e.g. brain_test)."
        )
    return url


def _get_integration_db_url_or_skip() -> str:
    """Return the integration DB URL, or call pytest.skip() if it is unsafe.

    Used by session-scoped fixtures that need the URL at fixture-setup time.
    """
    try:
        return _resolve_integration_db_url()
    except ValueError as exc:
        pytest.skip(str(exc))


try:
    INTEGRATION_DB_URL = _resolve_integration_db_url()
except ValueError:
    # Keep conftest importable so pytest can report a normal fixture-level skip.
    # Autouse fixtures resolve again and skip before constructing subprocesses,
    # engines, drivers, or network connections.
    INTEGRATION_DB_URL = ""

# Project root (tests/integration/ -> tests/ -> project root)
_PROJECT_ROOT = Path(__file__).parents[2]


# ---------------------------------------------------------------------------
# pytest marker registration
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """Register the 'integration' marker to avoid PytestUnknownMarkWarning."""
    config.addinivalue_line(
        "markers",
        "integration: marks tests as integration tests (require real PostgreSQL)",
    )


def format_missing_db_url_summary(skipped: int, reason: str | None = None) -> str | None:
    """The end-of-session line for a suite that measured nothing, or None.

    Ticket `634203e0`. Without ``BRAIN_V42_TEST_DB_URL`` this suite skips whole
    and exits 0 — measured on 2026-09-02: `423 skipped in 1.04s`, and nothing
    said why. A reader sees green and may believe 423 tests were replayed.

    Three facts, because a line missing any of them leaves the reader stuck: the
    variable to set, HOW MANY tests went unmeasured, and the value to set it to.
    That last one is not decoration — without it the next keystroke points at the
    production database, which is what the guard above exists to refuse.

    ``reason`` distinguishes the two ways to measure nothing, because they need
    different gestures from the reader: unset means "set it", rejected means "the
    value you already chose was refused, and here is what for". It carries the
    resolver's own message and NEVER the URL — the DSN holds a password, and this
    line is printed to a terminal and pasted into reports.

    Returns None when nothing was skipped: printed on every run the line would be
    scrolled past, and invisible on the day it matters.
    """
    if skipped <= 0:
        return None
    cause = (
        "is not set"
        if reason is None
        else f"is set but the suite refused the value it names — {reason}"
    )
    return (
        f"BRAIN_V42_TEST_DB_URL {cause} — {skipped} integration tests were SKIPPED "
        f"and measured nothing. Set it to a test database (e.g. .../brain_test) "
        f"before reading this run as a pass."
    )


def nothing_was_measured(stats: dict[str, Any]) -> bool:
    """True when a run skipped tests and produced no other outcome at all.

    The condition is "nothing was measured", never "the variable is missing". The
    first version of this guard returned early whenever the variable held a
    non-empty value, and that is a test of a KEYSTROKE: a URL pointing at the prod
    `brain` database is rejected by ``_resolve_integration_db_url`` and lands in
    this very ``skipped`` bucket. Measured on 2026-09-02 at HEAD 610c24d, both runs
    print `423 skipped` and exit 0 — one warned, one was silent, and the silent one
    belonged to whoever had tried to configure the suite and got it wrong.

    ``error`` counts as measurement here, deliberately, though it measured nothing
    either: an unreachable host yields `422 errors` and a NON-ZERO exit (measured
    the same day, 63 s of connection retries). That run cannot be misread as a
    pass, so a banner would only add noise to a screen that is already red. This
    line exists for the run that looks GREEN and proves nothing.
    """
    if not stats.get("skipped"):
        return False
    return not any(stats.get(outcome) for outcome in ("passed", "failed", "error"))


def _rejection_reason() -> str | None:
    """Why the configured URL was refused, or None when none was configured."""
    if not os.environ.get("BRAIN_V42_TEST_DB_URL"):
        return None
    try:
        _resolve_integration_db_url()
    except ValueError as exc:
        return str(exc)
    return None


def pytest_terminal_summary(terminalreporter: Any) -> None:
    _report_schema_residue_left_by_the_suite(terminalreporter)
    _report_missing_db_url(terminalreporter)


def _report_schema_residue_left_by_the_suite(terminalreporter: Any) -> None:
    """Name the family and the object, at the end of the run that left them."""
    url = os.environ.get("BRAIN_V42_TEST_DB_URL")
    if not url or _CHAIN_FINGERPRINT is None:
        return
    try:
        message = _describe_schema_residue_left_by_the_suite(_resolve_integration_db_url())
    except ValueError:
        return
    if message is None:
        return
    terminalreporter.write_sep("=", "schema residue left by this suite", red=True)
    for line in message.splitlines():
        terminalreporter.write_line(line)


def _report_missing_db_url(terminalreporter: Any) -> None:
    """Say it, once, at the end — where a reader actually looks.

    The suite already skips loudly test by test, but `-q` collapses those into a
    row of dots and the final line reads `423 skipped`. This is the only place
    the reason survives the summary.
    """
    if not nothing_was_measured(terminalreporter.stats):
        return
    skipped = len(terminalreporter.stats.get("skipped", []))
    line = format_missing_db_url_summary(skipped, _rejection_reason())
    if line is not None:
        terminalreporter.write_sep("=", "integration suite not measured", red=True)
        terminalreporter.write_line(line)


# ---------------------------------------------------------------------------
# Alembic migration (session-scoped, sync)
# ---------------------------------------------------------------------------


def _run_alembic_upgrade(db_url: str, project_root: Path) -> None:
    """Run alembic upgrade head via subprocess.

    Using subprocess avoids asyncpg/psycopg2 driver conflicts in alembic env.py.
    The migration is idempotent (checks alembic_version table).
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "POSTGRES_URL": db_url},
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Alembic migration failed:\n{result.stderr}\n{result.stdout}")


def _expected_alembic_head(project_root: Path) -> str:
    """Return the single head declared under ``alembic/versions``.

    Derived, never hardcoded: a pinned constant is one more thing that drifts
    the day a migration lands.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"expected exactly one Alembic head, got {heads}")
    return str(heads[0])


def _probe_schema_state(db_url: str) -> tuple[bool, str | None, ResidueProbe]:
    """Read the deployed revision and any leftover ``integ-`` rows.

    Returns ``(connected, deployed_revision, residue)``. When the database is
    unreachable, ``connected`` is False and the caller must NOT block: an
    unreachable database is the pre-existing skip/failure path, not a residue.
    ``deployed_revision`` is None when no ``alembic_version`` row exists at all,
    which is a virgin database the session fixture is meant to bootstrap.
    """

    async def probe() -> tuple[bool, str | None, ResidueProbe]:
        engine = create_async_engine(db_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                present = await conn.scalar(sa.text("SELECT to_regclass('public.alembic_version')"))
                revision = (
                    await conn.scalar(sa.text("SELECT version_num FROM alembic_version"))
                    if present is not None
                    else None
                )
                try:
                    counts: dict[str, int] = {}
                    for table in RESIDUE_TABLES:
                        exists = await conn.scalar(
                            sa.text("SELECT to_regclass(:qualified)"),
                            {"qualified": f"public.{table}"},
                        )
                        if exists is None:
                            continue
                        counts[table] = int(
                            await conn.scalar(
                                sa.text(  # noqa: S608 - fixed internal table names only
                                    f"SELECT count(*) FROM {table} "
                                    f"WHERE {_INTEGRATION_PROJECT_PREDICATE}"
                                )
                            )
                            or 0
                        )
                    residue = ResidueProbe(counts=counts)
                except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                    residue = ResidueProbe(failure=f"{type(exc).__name__}: {exc}")
                return True, (str(revision) if revision is not None else None), residue
        except Exception:  # noqa: BLE001 - unreachable DB is not this guard's business
            return False, None, ResidueProbe(failure="database unreachable")
        finally:
            await engine.dispose()

    return asyncio.run(probe())


def _assert_no_migration_test_residue(db_url: str, project_root: Path) -> None:
    """Refuse setup when the database carries an interrupted migration test.

    Fixes the third and only costly defect of the shared-database design: the
    breakage is silent, deferred, and wears a message about data corruption.
    This guard SAYS what happened and gives the repair gesture; it never repairs
    on its own, because an automatic repair would hide the problem again.
    """
    connected, deployed_revision, residue = _probe_schema_state(db_url)
    if not connected:
        return
    message = describe_schema_residue(
        deployed_revision=deployed_revision,
        expected_head=_expected_alembic_head(project_root),
        residue=residue,
        breadcrumbs=read_breadcrumbs(project_root),
    )
    if message is not None:
        raise RuntimeError(message)
    # At head, the counts SPEAK instead of being thrown away (PR 44 review) — a
    # notice, never a refusal: a healthy concurrent run legitimately holds integ-
    # rows during its own suite.
    notice = describe_data_residue_notice(residue=residue)
    if notice is not None:
        warnings.warn(notice, stacklevel=2)


def _probe_live_schema(db_url: str) -> SchemaProbe:
    """Read the trigger states and the replication role from the live database.

    Never returns a partial reading dressed up as a clean one: anything that
    goes wrong is reported in ``failure``, and the guard turns that into an
    error rather than a verdict.
    """

    async def probe() -> SchemaProbe:
        engine = create_async_engine(db_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        sa.text(
                            "SELECT tgname, tgenabled::text AS state "
                            "FROM pg_trigger WHERE NOT tgisinternal"
                        )
                    )
                ).mappings()
                states = {str(row["tgname"]): str(row["state"]) for row in rows}
                role = await conn.scalar(
                    sa.text("SELECT current_setting('session_replication_role')")
                )
                return SchemaProbe(
                    trigger_states=states,
                    session_replication_role=str(role) if role is not None else None,
                )
        except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
            return SchemaProbe(failure=f"{type(exc).__name__}: {exc}")
        finally:
            await engine.dispose()

    return asyncio.run(probe())


def _assert_schema_matches_the_migration_chain(db_url: str, project_root: Path) -> None:
    """Refuse setup when the SCHEMA diverges, even though the revision is right.

    Runs AFTER ``alembic upgrade head``, not before: a virgin database carries
    no triggers at all, and refusing it would break every fresh CI service
    container. Once the chain has run, whatever is off origin state was left by
    something that is not a migration.

    Connectivity needs no special case here: this runs after a SUCCESSFUL
    ``alembic upgrade head``, so the database is reachable by construction. A
    probe that fails at this point is a broken instrument, and the guard says so
    instead of concluding anything about the schema.
    """
    offenders = migrations_emitting_non_origin_trigger_state(project_root / "alembic" / "versions")
    if offenders:
        raise RuntimeError(describe_underivable_premise(offenders))
    message = describe_schema_divergence(_probe_live_schema(db_url))
    if message is not None:
        raise RuntimeError(message)
    _assert_every_schema_family_matches_the_migration_chain(db_url)


#: The chain's own fingerprint, derived ONCE per session and kept so the end-of-run
#: comparison costs nothing. `None` means setup never got far enough to build it —
#: no database, or a refusal upstream — and the end-of-run check stays silent
#: rather than inventing a verdict.
_CHAIN_FINGERPRINT: dict[str, dict[str, str]] | None = None


def _assert_every_schema_family_matches_the_migration_chain(db_url: str) -> None:
    """Close the CLASS ticket 3a7da99d named, not the one family it witnessed.

    The guard above watches trigger STATE. This one watches the whole schema —
    tables, columns, constraints, indexes, triggers and their state, functions,
    views, sequences, grants — against a reference the alembic chain builds in a
    disposable database moments earlier. Both sides derived, so nothing here ages
    when 052 lands.

    Costs 0.93 s per session, measured 2026-09-03 (0.85 s of it the chain itself).
    That is why the reference is built outright rather than cached across runs: a
    cache would buy a second back and pay for it in staleness.
    """
    global _CHAIN_FINGERPRINT
    with fresh_head_database(db_url, prefix="brain_chainref") as reference_url:
        _CHAIN_FINGERPRINT = probe_schema_families(reference_url)
    message = describe_family_divergence(_CHAIN_FINGERPRINT, probe_schema_families(db_url))
    if message is not None:
        raise RuntimeError(message)


def _describe_schema_residue_left_by_the_suite(db_url: str) -> str | None:
    """What the suite LEFT, compared against the same reference it started from.

    Setup refusal catches a residue on the next run, which is one run too late to
    tell you which suite left it. This runs at the end of the run that did it.
    A notice rather than a refusal, matching the data-residue notice above: the
    tests have already reported their own verdicts, and turning a green suite red
    here would hide them behind an infrastructure message.
    """
    if _CHAIN_FINGERPRINT is None:
        return None
    try:
        divergence = describe_family_divergence(_CHAIN_FINGERPRINT, probe_schema_families(db_url))
    except Exception as exc:  # noqa: BLE001 - a broken probe is said, never swallowed
        return f"Could not check the schema this suite left behind: {type(exc).__name__}: {exc}"
    if divergence is None:
        return None
    return (
        "This suite LEFT the test database diverging from the migration chain.\n"
        "The setup that started it found no divergence, so the objects below were "
        "left by a test in this run:\n\n" + divergence
    )


@pytest.fixture(scope="session", autouse=True)
def run_migrations() -> None:
    """Run Alembic migrations once per session before any integration test.

    This is a sync fixture (session-scoped) that calls subprocess.run().
    It runs before the async engine fixtures so the schema is ready.

    Under the SAME advisory lock as the fence: without it, two worktrees launching
    their suite against the shared database interleave their probe+upgrade OUTSIDE
    any protection — that is the hole through which ``288d7121``'s race stayed
    reachable after the fence's lock was laid (a minor finding of the PR 44 review).
    The setup waits its turn instead of reading a database another run is holding
    downgraded.
    """
    db_url = _get_integration_db_url_or_skip()
    lock = _SharedDatabaseMigrationLock(db_url)
    try:
        _assert_no_migration_test_residue(db_url, _PROJECT_ROOT)
        _run_alembic_upgrade(db_url, _PROJECT_ROOT)
        _assert_schema_matches_the_migration_chain(db_url, _PROJECT_ROOT)
    finally:
        lock.release()


_CALL_FAILED = pytest.StashKey[bool]()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Any:
    """Remember whether the test body itself failed, for the fence below.

    A drift left behind by a RED test is collateral; the same drift left by a
    GREEN test means the test lied about its own cleanup. The two deserve
    different verdicts, and only the report knows which happened.
    """
    report = yield
    if report.when == "call":
        item.stash[_CALL_FAILED] = bool(report.failed)
    return report


#: Advisory lock of the tests that migrate the shared database. The key IS the
#: ticket that measured the race: 0x288D7121 ("two concurrent executions interleave
#: destructively"). Held on a DEDICATED connection for the whole fence window: the
#: concurrent runs serialise, and a kill releases the lock with the connection —
#: crash-resistant by construction, where a lock file is not.
_MIGRATION_ADVISORY_LOCK_KEY = 0x288D7121


class _SharedDatabaseMigrationLock:
    """Session-level advisory lock held for the whole fence window.

    The connection lives on its own event loop because the fence is a sync
    fixture: ``asyncio.run`` would close the loop and the connection with it,
    releasing the lock at the exact moment it must start being held.
    """

    def __init__(self, db_url: str) -> None:
        # Each layer cleans up its own on failure: a lock that is not acquired must
        # leave behind neither a connection nor an engine — a leaked holding
        # connection freezes every later fence (a mute infinite wait, the lock having
        # no lock_timeout).
        self._loop = asyncio.new_event_loop()
        try:
            self._engine = create_async_engine(db_url, poolclass=NullPool)
            try:
                self._connection = self._loop.run_until_complete(self._engine.connect())
                try:
                    self._loop.run_until_complete(
                        self._connection.execute(
                            sa.text("SELECT pg_advisory_lock(:key)"),
                            {"key": _MIGRATION_ADVISORY_LOCK_KEY},
                        )
                    )
                except BaseException:
                    self._loop.run_until_complete(self._connection.close())
                    raise
            except BaseException:
                self._loop.run_until_complete(self._engine.dispose())
                raise
        except BaseException:
            self._loop.close()
            raise

    def release(self) -> None:
        # try/finally PER STEP: an unlock that raises must not prevent the
        # connection from closing — it is the connection that actually releases the
        # session lock; leaking it freezes every later fence.
        try:
            try:
                self._loop.run_until_complete(
                    self._connection.execute(
                        sa.text("SELECT pg_advisory_unlock(:key)"),
                        {"key": _MIGRATION_ADVISORY_LOCK_KEY},
                    )
                )
            finally:
                try:
                    self._loop.run_until_complete(self._connection.close())
                finally:
                    self._loop.run_until_complete(self._engine.dispose())
        finally:
            self._loop.close()


@pytest.fixture
def migration_downgrade_fence(
    request: pytest.FixtureRequest,
) -> Iterator[Callable[..., None]]:
    """Declare that a test is about to downgrade the shared database.

    Serializes first, then does two things in teardown — that is, after the
    test function has returned, including after its own ``finally``:

    1. clears the breadcrumb, which stops being true exactly then. Anything that
       kills the process in between leaves the file, and the next setup names
       this test instead of guessing;
    2. restores the Alembic head if the test left it behind, and SAYS SO.

    (2) exists because a per-test ``finally`` drifts: 025 and 026 are the two
    oldest migration tests here and simply never got one, so a single false
    assertion left the database 22 revisions behind. A convention every future
    author must remember is the same class of defect as a hardcoded Alembic
    head — this repository already learned that once.

    The serialization is the advisory lock of ``288d7121`` defect (1): six
    files migrate the SAME shared database and two concurrent runs interleave
    destructively. Waiting on the lock is the intended behaviour, not a hang.
    """
    lock = _SharedDatabaseMigrationLock(_resolve_integration_db_url())
    try:
        with ExitStack() as stack:

            def record(downgraded_to: str, restores_to: str = "head") -> None:
                stack.enter_context(
                    migration_breadcrumb(
                        project_root=_PROJECT_ROOT,
                        test_nodeid=request.node.nodeid,
                        downgraded_to=downgraded_to,
                        restores_to=restores_to,
                    )
                )

            yield record

            # Restore BEFORE the breadcrumb is cleared: the trace must outlive
            # the window it describes, not the other way round.
            _fence_restores_head(request)
    finally:
        lock.release()


def _fence_restores_head(request: pytest.FixtureRequest) -> None:
    """Put the head back when the test left it behind, and never do it silently."""
    db_url = _resolve_integration_db_url()
    connected, deployed_revision, _residue = _probe_schema_state(db_url)
    if not connected:
        return
    expected_head = _expected_alembic_head(_PROJECT_ROOT)
    message = describe_head_drift(
        test_nodeid=request.node.nodeid,
        deployed_revision=deployed_revision,
        expected_head=expected_head,
        test_failed=request.node.stash.get(_CALL_FAILED, False),
    )
    if message is None:
        return
    _run_alembic_upgrade(db_url, _PROJECT_ROOT)
    if request.node.stash.get(_CALL_FAILED, False):
        warnings.warn(message, stacklevel=1)
    else:
        raise RuntimeError(message)


# ---------------------------------------------------------------------------
# Engine (session-scoped)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def engine(run_migrations: None) -> AsyncEngine:  # type: ignore[misc]
    """Session-scoped async engine using NullPool (no connection pooling).

    NullPool is required for integration tests to avoid connection leaks
    and ensure each test gets a clean connection.
    """
    db_url = _get_integration_db_url_or_skip()
    eng = create_async_engine(db_url, poolclass=NullPool, echo=False)
    yield eng  # type: ignore[misc]
    await eng.dispose()


# ---------------------------------------------------------------------------
# DB availability check (session-scoped, autouse)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session", autouse=True)
async def check_db_connection(engine: AsyncEngine) -> None:  # type: ignore[misc]
    """Skip all integration tests if PostgreSQL is not reachable.

    This autouse fixture runs at session scope so a single connectivity
    check gates all tests. Uses a simple SELECT 1 probe.
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
    except Exception:  # noqa: BLE001
        pytest.skip("PostgreSQL test database is not reachable")


# ---------------------------------------------------------------------------
# Session factory (function-scoped)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Function-scoped async_sessionmaker bound to the test engine.

    Used by repos that accept an injected session_factory in __init__
    (PgLearningRepo, PgSnippetRepo, PgRunbookRepo, PgADRRepo, PgProjectContextRepo).
    """
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ---------------------------------------------------------------------------
# DB session (function-scoped)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncSession:
    """Function-scoped AsyncSession.

    Note: PgDecisionRepo calls session.commit() internally, so rollback-based
    isolation is NOT reliable for Decision tests. Use unique project_keys instead.
    For other repos (LearningRepo, etc.) this session can be used directly.
    """
    async with session_factory() as session:
        yield session  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Test data cleanup (session-scoped, runs after all tests)
# ---------------------------------------------------------------------------

_TABLES_WITH_PROJECT_KEY = (
    "indexed_plan_chunks",
    "indexed_plans",
    "gitlab_events",
    "brain_sessions",
    "search_log",
    "learnings",
    "decisions",
    "snippets",
    "runbooks",
    "adrs",
    "features",
)
_INTEGRATION_PROJECT_PREDICATE = (
    "project_key LIKE 'integ-%' OR project_key LIKE 'integ\\_%' ESCAPE '\\'"
)


async def purge_integration_rows(conn: Any) -> None:
    """Delete every row whose project key carries the ``integ-`` / ``integ_`` prefix.

    Extracted from the session fixture so a test can EXERCISE the purge instead of
    trusting it. A teardown nobody can call is a teardown nobody can verify — and
    this one silently spared every fixture that wrote under a real project key.

    Source-table deletes fire migration 033 registry triggers, so canonical
    ledger rows are purged only after every source row and project context has
    been removed.
    """
    await conn.execute(
        sa.text(
            """
            DELETE FROM ticket_extraction_proposals AS proposal
            WHERE proposal.target_project LIKE 'integ-%'
               OR proposal.target_project LIKE 'integ\\_%' ESCAPE '\\'
               OR proposal.ticket_id IN (
                   SELECT ticket.id
                   FROM tickets AS ticket
                   WHERE ticket.from_project LIKE 'integ-%'
                      OR ticket.from_project LIKE 'integ\\_%' ESCAPE '\\'
                      OR ticket.to_project LIKE 'integ-%'
                      OR ticket.to_project LIKE 'integ\\_%' ESCAPE '\\'
               )
            """
        )
    )
    await conn.execute(
        sa.text(
            """
            DELETE FROM ticket_messages AS message
            WHERE message.author_project LIKE 'integ-%'
               OR message.author_project LIKE 'integ\\_%' ESCAPE '\\'
               OR message.ticket_id IN (
                   SELECT ticket.id
                   FROM tickets AS ticket
                   WHERE ticket.from_project LIKE 'integ-%'
                      OR ticket.from_project LIKE 'integ\\_%' ESCAPE '\\'
                      OR ticket.to_project LIKE 'integ-%'
                      OR ticket.to_project LIKE 'integ\\_%' ESCAPE '\\'
               )
            """
        )
    )
    await conn.execute(
        sa.text(
            """
            DELETE FROM tickets
            WHERE from_project LIKE 'integ-%'
               OR from_project LIKE 'integ\\_%' ESCAPE '\\'
               OR to_project LIKE 'integ-%'
               OR to_project LIKE 'integ\\_%' ESCAPE '\\'
            """
        )
    )
    for table in _TABLES_WITH_PROJECT_KEY:
        await conn.execute(
            sa.text(  # noqa: S608 - fixed internal table names only
                f"DELETE FROM {table} WHERE {_INTEGRATION_PROJECT_PREDICATE}"
            )
        )
    await conn.execute(
        sa.text(f"DELETE FROM project_contexts WHERE {_INTEGRATION_PROJECT_PREDICATE}")
    )
    await conn.execute(
        sa.text(
            """
            DELETE FROM entity_relations AS relation
            USING brain_entities AS source, brain_entities AS target
            WHERE source.id = relation.source_entity_id
              AND target.id = relation.target_entity_id
              AND (
                  source.project_key LIKE 'integ-%'
                  OR source.project_key LIKE 'integ\\_%' ESCAPE '\\'
                  OR target.project_key LIKE 'integ-%'
                  OR target.project_key LIKE 'integ\\_%' ESCAPE '\\'
              )
            """
        )
    )
    await conn.execute(
        sa.text(f"DELETE FROM brain_entities WHERE {_INTEGRATION_PROJECT_PREDICATE}")
    )
    await conn.execute(sa.text(f"DELETE FROM projects WHERE {_INTEGRATION_PROJECT_PREDICATE}"))


@pytest_asyncio.fixture(scope="session", autouse=True)
async def cleanup_test_data(engine: AsyncEngine) -> None:  # type: ignore[misc]
    """Run :func:`purge_integration_rows` once every integration test has finished."""
    yield  # type: ignore[misc]
    async with engine.begin() as conn:
        await purge_integration_rows(conn)


# ---------------------------------------------------------------------------
# Neo4j fixtures (function-scoped, skip when unavailable)
# ---------------------------------------------------------------------------


def _resolve_integration_neo4j_config() -> tuple[str, tuple[str, str]]:
    """Return dedicated Neo4j test configuration, with no implicit fallback."""
    url = os.environ.get("BRAIN_V42_TEST_NEO4J_URL")
    user = os.environ.get("BRAIN_V42_TEST_NEO4J_USER")
    password = os.environ.get("BRAIN_V42_TEST_NEO4J_PASSWORD")
    if not all(isinstance(value, str) and value.strip() for value in (url, user, password)):
        raise ValueError(
            "BRAIN_V42_TEST_NEO4J_URL, BRAIN_V42_TEST_NEO4J_USER, and "
            "BRAIN_V42_TEST_NEO4J_PASSWORD are required for graph integration tests"
        )
    return url, (user, password)


def _require_destructive_neo4j_recovery_target(url: str) -> None:
    """Permit projection wipes only on an explicit loopback test target."""
    opt_in = os.environ.get("BRAIN_V42_TEST_NEO4J_DESTRUCTIVE_RECOVERY", "")
    if opt_in.casefold() not in {"1", "true", "yes", "on"}:
        raise ValueError("BRAIN_V42_TEST_NEO4J_DESTRUCTIVE_RECOVERY is required for recovery tests")
    if urlparse(url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("destructive Neo4j recovery tests require a loopback target")


@pytest.fixture(scope="session")
def neo4j_url() -> str:
    """Dedicated Neo4j test URL, or skip before any driver is built."""
    try:
        url, _auth = _resolve_integration_neo4j_config()
    except ValueError as exc:
        pytest.skip(str(exc))
    return url


@pytest.fixture(scope="session")
def neo4j_auth() -> tuple[str, str]:
    """Dedicated Neo4j test credentials, or skip before driver construction."""
    try:
        _url, auth = _resolve_integration_neo4j_config()
    except ValueError as exc:
        pytest.skip(str(exc))
    return auth


@pytest.fixture(scope="session")
def neo4j_destructive_recovery(neo4j_url: str) -> None:
    """Skip destructive recovery tests unless their isolated target is explicit."""
    try:
        _require_destructive_neo4j_recovery_target(neo4j_url)
    except ValueError as exc:
        pytest.skip(str(exc))


@pytest_asyncio.fixture
async def neo4j_driver(neo4j_url: str, neo4j_auth: tuple[str, str]):  # type: ignore[misc]
    """Create Neo4j async driver; skip this test if Neo4j is not reachable.

    Cleans up all nodes whose id starts with 'test-' after each test so that
    graph integration tests don't pollute each other.
    """
    from neo4j import AsyncGraphDatabase

    try:
        driver = AsyncGraphDatabase.driver(neo4j_url, auth=neo4j_auth)
        # Verify the connection is live before handing it to the test.
        async with driver.session() as s:
            await s.run("RETURN 1")
    except Exception:  # noqa: BLE001
        pytest.skip("Neo4j test database is not reachable")
        return

    yield driver  # type: ignore[misc]

    # Teardown: remove all test nodes (id starts with "test-")
    try:
        async with driver.session() as s:
            await s.run("MATCH (n) WHERE n.id STARTS WITH 'test-' DETACH DELETE n")
    finally:
        await driver.close()


@pytest_asyncio.fixture
async def graph_service(neo4j_driver):  # type: ignore[misc]
    """GraphService wired to the real Neo4j driver (skipped if Neo4j unavailable)."""
    from brain_v42.services.graph_service import GraphService

    return GraphService(neo4j_driver)


# ---------------------------------------------------------------------------
# sse-starlette — neutraliser un latch de PROCESSUS entre deux tests
# ---------------------------------------------------------------------------


class SseExitLatchArmed(UserWarning):
    """``sse-starlette``'s exit latch was armed, and we disarmed it.

    Emitted to be SEEN: without it we sweep an upstream defect under a test rug, and
    the day ``sse-starlette`` fixes its latch nobody will know either that this
    fixture exists or why. A mute guard becomes folklore.
    """


#: The upstream defect, in one sentence: ``sse_starlette.sse.AppStatus.should_exit``
#: is a CLASS attribute, process-global, that nothing ever resets to ``False``. A
#: watcher (``sse_starlette/sse.py:81-113``) polls every 0.5 s the uvicorn server it
#: finds through ``signal.getsignal(SIGTERM).__self__``, and arms it as soon as it
#: sees it stop — including for a PERFECTLY NORMAL shutdown, that is, at every bench
#: teardown. Once armed, it makes ``_listen_for_exit_signal`` (l. 311-313) return
#: immediately, which cancels the whole ``EventSourceResponse`` task group just after
#: the headers are sent: the server answers 200 then RETURNS WITH NO BODY, client
#: still connected, without raising or logging. An MCP client whose ``initialize``
#: response transits through that stream then waits forever.
#:
#: MEASURED on 2026-08-25: a bench's teardown window lasts ~0.144 s against a probe
#: at 0.5 s, i.e. ~28 % arming per teardown — consistent with the geometric ratio
#: 0.144/0.5. It is a function of the tests' DURATION, not an independent draw: hence
#: a CI failure that reads as a coin-flip.
#:
#: The repair is UPSTREAM. Here we protect ourselves and leave a trace.
_SSE_LATCH_ATTR = "should_exit"


def _read_sse_exit_latch() -> bool | None:
    """Return the latch's state, or ``None`` if upstream moved under our feet.

    A late and total import: this fixture is autouse over the WHOLE integration
    suite, and a guard that brings down the tests it protects is worse than no guard.
    """
    try:
        from sse_starlette.sse import AppStatus

        return bool(getattr(AppStatus, _SSE_LATCH_ATTR))
    except Exception:  # noqa: BLE001 - never fatal, it is a guard
        return None


def _disarm_sse_exit_latch() -> None:
    try:
        from sse_starlette.sse import AppStatus

        setattr(AppStatus, _SSE_LATCH_ATTR, False)
    except Exception:  # noqa: BLE001 - never fatal, it is a guard
        pass


@pytest.fixture(autouse=True)
def disarm_sse_exit_latch(request: pytest.FixtureRequest) -> Iterator[None]:
    """Disarm the latch before AND after each test, SAYING when it was armed.

    Both moments count, and they do not say the same thing:

    - **before**: the latch was already armed on entry, so an earlier test left it
      that way and this one was about to suffer it without having caused it;
    - **after**: the latch was armed DURING this test or by its teardown — and that
      is the only measurement that NAMES the culprit. That is also why the after
      disarming exists: without it, the before trace would accuse the next victim
      instead of the author.

    This fixture is declared in ``conftest.py`` and not in a module, because the two
    benches that stand up uvicorn live in different directories (``mcp/`` and
    ``metrics/``) and the latch, for its part, ignores directories.
    """
    if _read_sse_exit_latch():
        _disarm_sse_exit_latch()
        warnings.warn(
            f"latch sse-starlette DÉJÀ ARMÉ à l'entrée de {request.node.nodeid} — "
            "désarmé. Un test antérieur l'a laissé armé ; celui-ci en aurait été la "
            "victime, pas la cause. Voir SseExitLatchArmed dans "
            "tests/integration/conftest.py.",
            SseExitLatchArmed,
            stacklevel=2,
        )
    try:
        yield
    finally:
        if _read_sse_exit_latch():
            _disarm_sse_exit_latch()
            warnings.warn(
                f"latch sse-starlette ARMÉ PAR {request.node.nodeid} (test ou "
                "teardown) — désarmé. C'est CE test qui l'a armé : sans cette "
                "fixture, le prochain test servant une réponse JSON-RPC sur un flux "
                "SSE aurait pendu. Voir SseExitLatchArmed dans "
                "tests/integration/conftest.py.",
                SseExitLatchArmed,
                stacklevel=2,
            )
