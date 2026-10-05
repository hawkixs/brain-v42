"""The test sessions can never reach a production database (tickets e3292865, 2687faf0).

Two measured incidents, one cause. On 2026-09-21 forty rows landed in production
``dream_runs`` (empty project_key, the literals "something went wrong" and "reorg
integrity failure"), and production Neo4j holds 796 ``integ-%`` nodes that exist
nowhere in PostgreSQL (``integ-dedup-<hex8>`` is the key
``tests/unit/test_promote_prepare_dedup_bands.py`` builds). The only refusal of the
production database NAME lived in ``tests/integration/conftest.py`` -- the unit
suite, which holds 17 of the 18 ``require_test_db_url()`` callers, had none, and
nothing at all identified a production Neo4j.

These tests pin the structural guard in ``tests/database_guards.py``: it runs when
the session is configured, before any engine, driver or subprocess exists.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import database_guards
from tests.conftest import require_test_db_url
from tests.database_guards import (
    UnsafeTestDatabase,
    enforce_database_isolation,
    is_test_database_name,
    neo4j_identity,
    postgres_identity,
    production_neo4j_identities,
    production_postgres_identities,
    refuse_neo4j_holding_production_data,
    registered_production_postgres_identities,
    validate_test_database_url,
    validate_test_neo4j_url,
)

PROD_URL = "postgresql+asyncpg://brain:SECRET-PW@localhost:5433/brain"
TEST_URL = "postgresql+asyncpg://brain:SECRET-PW@localhost:5433/brain_test"
PROD_IDENTITY = ("127.0.0.1", 5433, "brain")
REPO_ROOT = Path(__file__).parents[2]


@pytest.fixture(autouse=True)
def _restore_the_session_registry() -> Iterator[None]:
    """These tests record fake production identities: leave the real session's untouched."""
    postgres = set(database_guards._production_postgres)
    neo4j = set(database_guards._production_neo4j)
    yield
    database_guards._production_postgres.clear()
    database_guards._production_postgres.update(postgres)
    database_guards._production_neo4j.clear()
    database_guards._production_neo4j.update(neo4j)


# ---------------------------------------------------------------------------
# PostgreSQL identity and naming
# ---------------------------------------------------------------------------


def test_identity_ignores_credentials_scheme_and_loopback_spelling() -> None:
    assert postgres_identity("postgresql+asyncpg://a:b@127.0.0.1:5433/brain") == PROD_IDENTITY
    assert postgres_identity("postgresql://x@[::1]:5433/brain") == PROD_IDENTITY
    assert postgres_identity("postgresql://x@localhost:5433/br%61in") == PROD_IDENTITY
    assert postgres_identity("postgresql://x@db.example/brain") == ("db.example", 5432, "brain")


@pytest.mark.parametrize(
    "name", ["brain_test", "brain_migration_ab12cd", "brain_fresh_ab12cd", "scratch_test"]
)
def test_disposable_database_names_are_accepted(name: str) -> None:
    assert is_test_database_name(name)


@pytest.mark.parametrize("name", ["brain", "postgres", "brain_v42", "brain_prod", "testbrain", ""])
def test_everything_else_is_not_a_test_database(name: str) -> None:
    assert not is_test_database_name(name)


def test_a_test_url_naming_the_production_database_is_refused() -> None:
    with pytest.raises(UnsafeTestDatabase, match="same database as POSTGRES_URL"):
        validate_test_database_url(PROD_URL, production={PROD_IDENTITY})


def test_the_production_database_is_recognised_through_another_spelling() -> None:
    with pytest.raises(UnsafeTestDatabase, match="same database as POSTGRES_URL"):
        validate_test_database_url(
            "postgresql+asyncpg://u:p@127.0.0.1:5433/brain", production={PROD_IDENTITY}
        )


def test_a_database_that_is_not_named_like_a_test_database_is_refused() -> None:
    with pytest.raises(UnsafeTestDatabase, match="not a test database"):
        validate_test_database_url("postgresql://u:p@localhost:5433/brain", production=set())


def test_a_connection_override_in_the_query_string_is_refused() -> None:
    with pytest.raises(UnsafeTestDatabase, match="override"):
        validate_test_database_url(f"{TEST_URL}?host=prod.example", production=set())


def test_the_refusal_never_echoes_the_credential() -> None:
    with pytest.raises(UnsafeTestDatabase) as refused:
        validate_test_database_url(PROD_URL, production={PROD_IDENTITY})
    assert "SECRET-PW" not in str(refused.value)


def test_the_ci_shape_is_accepted() -> None:
    """CI exports POSTGRES_URL and BRAIN_V42_TEST_DB_URL as the same brain_test."""
    ci_url = "postgresql+asyncpg://brain:brain@localhost:5432/brain_test"
    environ = {"POSTGRES_URL": ci_url, "BRAIN_V42_TEST_DB_URL": ci_url}

    enforce_database_isolation(environ, dotenv_files=[])

    assert environ["POSTGRES_URL"] == ci_url
    assert validate_test_database_url(ci_url, production=set()) == ci_url


def test_production_identities_come_from_the_environment_and_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("BRAIN_POSTGRES_URL=postgresql://u:p@dotenv-host:5432/brain\n")
    environ = {
        "POSTGRES_URL": PROD_URL,
        "BRAIN_DELIVERY_POSTGRES_URL": "postgresql://u:p@delivery-host/brain",
    }

    found = production_postgres_identities(environ, [dotenv])

    assert found == {
        PROD_IDENTITY,
        ("dotenv-host", 5432, "brain"),
        ("delivery-host", 5432, "brain"),
    }


def test_a_test_named_postgres_url_is_not_a_production_identity() -> None:
    assert production_postgres_identities({"POSTGRES_URL": TEST_URL}, []) == frozenset()


def test_postgres_identities_include_every_source_even_when_shadowed(tmp_path: Path) -> None:
    first = tmp_path / "first.env"
    first.write_text("POSTGRES_URL=postgresql://u:p@first-host/brain\n")
    second = tmp_path / "second.env"
    second.write_text("POSTGRES_URL=postgresql://u:p@second-host/brain\n")

    found = production_postgres_identities({"POSTGRES_URL": TEST_URL}, [first, second])

    assert found == {("first-host", 5432, "brain"), ("second-host", 5432, "brain")}


@pytest.mark.parametrize("source", ["environment", "dotenv"])
@pytest.mark.parametrize(
    "name",
    [
        "POSTGRES_URL",
        "BRAIN_POSTGRES_URL",
        "BRAIN_DELIVERY_POSTGRES_URL",
        "NEO4J_URL",
        "BRAIN_NEO4J_URL",
        "GRAPH_PROJECTOR_NEO4J_URL",
        "BRAIN_GRAPH_PROJECTOR_NEO4J_URL",
    ],
)
def test_guarded_names_are_case_insensitive_and_every_spelling_is_neutralised(
    tmp_path: Path, source: str, name: str
) -> None:
    url = PROD_URL if "POSTGRES" in name else "bolt://production-graph:7000"
    spellings = (name.lower(), name.title())
    configured = dict.fromkeys(spellings, url)
    dotenv = tmp_path / ".env"
    dotenv.write_text("".join(f"{key}={value}\n" for key, value in configured.items()))
    environ = configured.copy() if source == "environment" else {}
    dotenvs = [dotenv] if source == "dotenv" else []
    if "POSTGRES" in name:
        assert PROD_IDENTITY in production_postgres_identities(environ, dotenvs)
    else:
        assert ("production-graph", 7000) in production_neo4j_identities(environ, dotenvs, [])

    enforce_database_isolation(environ, dotenv_files=dotenvs, projector_env_files=[])

    for spelling in spellings:
        safe = environ[spelling]
        identity = postgres_identity(safe) if "POSTGRES" in name else neo4j_identity(safe)
        assert identity[1] == 1


# ---------------------------------------------------------------------------
# Session entry point
# ---------------------------------------------------------------------------


def test_the_session_points_application_settings_at_the_test_database() -> None:
    environ = {
        "POSTGRES_URL": PROD_URL,
        "BRAIN_POSTGRES_URL": PROD_URL,
        "BRAIN_V42_TEST_DB_URL": TEST_URL,
    }

    enforce_database_isolation(environ, dotenv_files=[])

    assert environ["POSTGRES_URL"] == TEST_URL
    assert environ["BRAIN_POSTGRES_URL"] == TEST_URL
    assert PROD_IDENTITY in registered_production_postgres_identities()


@pytest.mark.parametrize("test_url", [None, TEST_URL])
def test_later_env_loading_cannot_restore_a_production_settings_alias(test_url: str | None) -> None:
    environ = {"POSTGRES_URL": PROD_URL, "NEO4J_URL": "bolt://localhost:7687"}
    if test_url:
        environ["BRAIN_V42_TEST_DB_URL"] = test_url

    enforce_database_isolation(environ, dotenv_files=[], projector_env_files=[])
    # domain_backfill.load_env_file uses setdefault for each key it reads.
    environ.setdefault("BRAIN_POSTGRES_URL", PROD_URL)
    environ.setdefault("BRAIN_NEO4J_URL", "bolt://localhost:7687")

    assert environ["BRAIN_POSTGRES_URL"] == environ["POSTGRES_URL"]
    assert environ["BRAIN_NEO4J_URL"] == environ["NEO4J_URL"]
    assert neo4j_identity(environ["BRAIN_NEO4J_URL"])[1] == 1
    if test_url:
        assert environ["BRAIN_POSTGRES_URL"] == test_url
    else:
        assert postgres_identity(environ["BRAIN_POSTGRES_URL"])[1] == 1


def test_child_settings_cannot_read_production_from_case_variants_or_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"Brain_Postgres_Url={PROD_URL}\nBrain_Neo4J_Url=bolt://localhost:7687\n")
    environ = {
        **os.environ,
        "brain_postgres_url": PROD_URL,
        "brain_neo4j_url": "bolt://localhost:7687",
    }
    for name in ("BRAIN_V42_TEST_DB_URL", "BRAIN_V42_TEST_NEO4J_URL"):
        environ.pop(name, None)
    enforce_database_isolation(environ, dotenv_files=[dotenv], projector_env_files=[])
    environ["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + environ.get("PYTHONPATH", "")

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from brain_v42.config import Settings; "
            "settings = Settings(_env_file=sys.argv[1]); "
            "assert settings.postgres_url == sys.argv[2]; "
            "assert settings.neo4j_url == sys.argv[3]",
            str(dotenv),
            database_guards.UNREACHABLE_POSTGRES_URL,
            database_guards.UNREACHABLE_NEO4J_URL,
        ],
        cwd=REPO_ROOT,
        env=environ,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_without_a_test_database_application_settings_point_at_nothing() -> None:
    environ = {"POSTGRES_URL": PROD_URL}

    enforce_database_isolation(environ, dotenv_files=[])

    assert postgres_identity(environ["POSTGRES_URL"]) != PROD_IDENTITY
    assert postgres_identity(environ["POSTGRES_URL"])[1] == 1


def test_a_production_delivery_database_is_neutralised_in_the_session() -> None:
    environ = {"BRAIN_DELIVERY_POSTGRES_URL": "postgresql://u:p@localhost:5433/brain"}

    enforce_database_isolation(environ, dotenv_files=[])

    environ.setdefault("BRAIN_DELIVERY_POSTGRES_URL", PROD_URL)
    assert postgres_identity(environ["BRAIN_DELIVERY_POSTGRES_URL"])[1] == 1


def test_a_production_test_database_url_fails_the_session() -> None:
    environ = {"POSTGRES_URL": PROD_URL, "BRAIN_V42_TEST_DB_URL": PROD_URL}

    with pytest.raises(UnsafeTestDatabase, match="BRAIN_V42_TEST_DB_URL"):
        enforce_database_isolation(environ, dotenv_files=[])


def test_a_dotenv_production_database_cannot_be_the_test_database(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("POSTGRES_URL=postgresql://u:p@localhost:5433/brain\n")
    environ = {"BRAIN_V42_TEST_DB_URL": "postgresql://u:p@localhost:5433/brain"}

    with pytest.raises(UnsafeTestDatabase, match="same database as POSTGRES_URL"):
        enforce_database_isolation(environ, dotenv_files=[dotenv])


def test_a_production_neo4j_test_url_fails_the_session() -> None:
    environ = {"NEO4J_URL": "bolt://localhost:7687", "BRAIN_V42_TEST_NEO4J_URL": "bolt://127.0.0.1"}

    with pytest.raises(UnsafeTestDatabase, match="BRAIN_V42_TEST_NEO4J_URL"):
        enforce_database_isolation(environ, dotenv_files=[])


def test_ambient_neo4j_settings_are_pointed_at_nothing(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("GRAPH_PROJECTOR_NEO4J_URL=bolt://localhost:7687\n")
    environ = {"NEO4J_URL": "bolt://localhost:7687"}

    enforce_database_isolation(environ, dotenv_files=[dotenv])

    for name in ("NEO4J_URL", "GRAPH_PROJECTOR_NEO4J_URL"):
        assert neo4j_identity(environ[name]) == ("127.0.0.1", 1)


def test_an_unconfigured_neo4j_stays_unconfigured() -> None:
    environ: dict[str, str] = {}

    enforce_database_isolation(environ, dotenv_files=[], projector_env_files=[])

    assert environ == {}


def test_require_test_db_url_refuses_a_production_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_V42_TEST_DB_URL", "postgresql://u:p@localhost:5433/brain")

    with pytest.raises(UnsafeTestDatabase):
        require_test_db_url()


def test_pytest_refuses_to_start_on_a_production_test_database() -> None:
    """The refusal is wired at pytest_configure: it holds for every suite, not one conftest."""
    environ = {
        **os.environ,
        "POSTGRES_URL": "postgresql+asyncpg://u:p@localhost:5433/brain",
        "BRAIN_V42_TEST_DB_URL": "postgresql+asyncpg://u:p@localhost:5433/brain",
    }

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/unit/test_alembic_env.py",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO_ROOT,
        env=environ,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    output = result.stderr + result.stdout
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, output
    assert "BRAIN_V42_TEST_DB_URL refused" in output
    assert "the test sessions never touch a production database" in output
    assert "INTERNALERROR" not in output
    assert "collected" not in result.stdout


# ---------------------------------------------------------------------------
# Neo4j identity
# ---------------------------------------------------------------------------


def test_neo4j_identity_ignores_scheme_credentials_and_loopback_spelling() -> None:
    assert neo4j_identity("bolt://localhost:7687") == ("127.0.0.1", 7687)
    assert neo4j_identity("neo4j://u:p@127.0.0.1") == ("127.0.0.1", 7687)
    assert neo4j_identity("bolt+s://[::1]:7687") == ("127.0.0.1", 7687)
    assert neo4j_identity("bolt://graph.example:17687") == ("graph.example", 17687)


def test_the_compose_published_bolt_port_is_always_production() -> None:
    assert ("127.0.0.1", 7687) in production_neo4j_identities({}, [], [])


def test_production_neo4j_comes_from_settings_the_projector_file_and_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("NEO4J_URL=bolt://env-file-host:7000\n")
    projector = tmp_path / "graph-projector.env"
    projector.write_text(
        "GRAPH_PROJECTOR_NEO4J_URL=bolt://projector-host:7001\n"
        "GRAPH_PROJECTOR_NEO4J_PASSWORD=never-read\n"
    )
    environ = {"BRAIN_NEO4J_URL": "bolt://shell-host:7002"}

    found = production_neo4j_identities(environ, [dotenv], [projector])

    assert {
        ("env-file-host", 7000),
        ("projector-host", 7001),
        ("shell-host", 7002),
        ("127.0.0.1", 7687),
    } <= found


def test_neo4j_identities_include_every_source_even_when_shadowed(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("GRAPH_PROJECTOR_NEO4J_URL=bolt://dotenv-host:7000\n")
    projector = tmp_path / "graph-projector.env"
    projector.write_text("GRAPH_PROJECTOR_NEO4J_URL=bolt://production-graph:7001\n")
    environ = {"GRAPH_PROJECTOR_NEO4J_URL": "bolt://shell-host:7002"}

    assert {
        ("dotenv-host", 7000),
        ("production-graph", 7001),
        ("shell-host", 7002),
    } <= production_neo4j_identities(environ, [dotenv], [projector])

    environ["BRAIN_V42_TEST_NEO4J_URL"] = "bolt://production-graph:7001"
    with pytest.raises(UnsafeTestDatabase, match="production Neo4j"):
        enforce_database_isolation(environ, dotenv_files=[dotenv], projector_env_files=[projector])


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "127.42.1.9",
        "0.0.0.0",
        "[::1]",
        "[::ffff:127.0.0.1]",
        "[::ffff:7f2a:109]",
    ],
)
def test_all_loopback_spellings_share_postgres_and_neo4j_identities(host: str) -> None:
    assert postgres_identity(f"postgresql://u:p@{host}:5433/brain") == PROD_IDENTITY
    assert neo4j_identity(f"bolt://{host}:7687") == ("127.0.0.1", 7687)
    with pytest.raises(UnsafeTestDatabase, match="production Neo4j"):
        validate_test_neo4j_url(f"bolt://{host}:7687", production={("127.0.0.1", 7687)})


def test_ipv4_mapped_remote_addresses_share_the_ipv4_identity() -> None:
    assert neo4j_identity("bolt://[::ffff:192.0.2.1]:7000") == ("192.0.2.1", 7000)
    assert postgres_identity("postgresql://u:p@[::ffff:192.0.2.1]/brain") == (
        "192.0.2.1",
        5432,
        "brain",
    )


@pytest.mark.parametrize(
    "url",
    ["bolt://127.0.0.1:7687", "bolt://localhost", "neo4j://localhost:7687", "bolt://[::1]:7687"],
)
def test_a_neo4j_test_url_naming_production_is_refused(url: str) -> None:
    with pytest.raises(UnsafeTestDatabase, match="production Neo4j"):
        validate_test_neo4j_url(url, production={("127.0.0.1", 7687)})


def test_a_neo4j_test_url_on_another_port_is_accepted() -> None:
    url = "bolt://127.0.0.1:17687"
    assert validate_test_neo4j_url(url, production={("127.0.0.1", 7687)}) == url


class _Result:
    def __init__(self, record: object | None) -> None:
        self._record = record

    async def single(self) -> object | None:
        return self._record


class _Session:
    def __init__(self, record: object | None) -> None:
        self._record = record
        self.statements: list[str] = []

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def run(self, statement: str, *_args: object, **_kwargs: object) -> _Result:
        self.statements.append(statement)
        return _Result(self._record)


def _driver(record: object | None) -> SimpleNamespace:
    session = _Session(record)
    return SimpleNamespace(session=lambda: session, seen=session)


@pytest.mark.asyncio
async def test_a_neo4j_holding_a_real_project_is_refused() -> None:
    driver = _driver({"project_key": "brain-v42"})

    with pytest.raises(UnsafeTestDatabase, match="production data"):
        await refuse_neo4j_holding_production_data(driver)


@pytest.mark.asyncio
async def test_a_neo4j_holding_only_test_projects_is_accepted_and_read_only() -> None:
    driver = _driver(None)

    await refuse_neo4j_holding_production_data(driver)

    (statement,) = driver.seen.statements
    assert statement.lstrip().upper().startswith("MATCH")
    for verb in ("CREATE", "MERGE", "DELETE", "SET "):
        assert verb not in statement.upper()
