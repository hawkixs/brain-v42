"""A fresh-volume restore must never bless a partial or foreign database."""

from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import structlog

from brain_v42.config import get_settings
from brain_v42.scripts import restore_cli

ROOT = Path(__file__).resolve().parents[2]
TOC = """; Archive created by pg_dump
3; 2615 2200 SCHEMA - public brain
215; 1259 12345 TABLE public alembic_version brain
216; 1259 12346 VIEW public codex_brain_entity_v1 brain
4000; 0 0 ACL public TABLE codex_brain_entity_v1 brain
"""
SCHEMA = """REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO codex_ro;
GRANT SELECT ON TABLE public.codex_brain_entity_v1 TO codex_ro;
"""
MARKER = "brain-v42-restore sha256=" + "a" * 64 + " at=2026-10-06T18:00:00Z"


class FakeRunner:
    """Record mutations and reject subprocess calls outside the restore protocol."""

    def __init__(self) -> None:
        self.relations: list[list[str]] = []
        self.marker: str | None = None
        self.rows = 1
        self.toc = TOC
        self.schema = SCHEMA
        self.restore_code = 0
        self.fail_on: str | None = None
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.roles: dict[str, str] = {}
        self.created_roles: list[str] = []

    def __call__(
        self,
        args: list[str],
        *,
        env: dict[str, str],
        capture_output: bool,
        text: bool,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        assert isinstance(args, list)
        assert capture_output and text and not check
        self.calls.append((args, env))
        output = ""
        code = 0
        if self.fail_on and any(self.fail_on in arg for arg in args):
            return subprocess.CompletedProcess(args, 1, "", "private database error")
        if args[0] == "psql":
            sql = args[args.index("--command") + 1]
            assert "--no-psqlrc" in args
            assert "ON_ERROR_STOP=1" in args
            if "pg_class" in sql:
                output = json.dumps({"relations": self.relations, "marker": self.marker})
            elif "count(*)" in sql:
                output = str(self.rows)
            elif "CREATE ROLE" in sql:
                assert "IF NOT EXISTS" in sql
                for role, attributes in re.findall(r"CREATE ROLE (\w+) ([^;]+);", sql):
                    assert attributes == "NOLOGIN"
                    assert f"rolname = '{role}'" in sql
                    if role not in self.roles:
                        self.roles[role] = attributes
                        self.created_roles.append(role)
            elif "COMMENT ON DATABASE" in sql:
                self.marker = sql.split(" IS '", 1)[1].rsplit("'", 1)[0]
            else:
                pytest.fail("unexpected SQL")
        elif args[0] == "pg_restore":
            if "--list" in args:
                output = self.toc
            elif "--schema-only" in args:
                output = self.schema
            else:
                code = self.restore_code
        else:
            pytest.fail("unexpected executable")
        return subprocess.CompletedProcess(args, code, output, "private database error")

    @property
    def restores(self) -> list[list[str]]:
        return [args for args, _ in self.calls if args[0] == "pg_restore" and "--dbname" in args]

    @property
    def mutations(self) -> list[list[str]]:
        return self.restores + [
            args
            for args, _ in self.calls
            if any("CREATE ROLE" in arg or "COMMENT ON DATABASE" in arg for arg in args)
        ]


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    fake = FakeRunner()
    monkeypatch.setenv(
        "POSTGRES_URL", "postgresql+asyncpg://brain:test%40password@postgres:5432/brain"
    )
    monkeypatch.delenv("BRAIN_POSTGRES_URL", raising=False)
    monkeypatch.setattr(restore_cli.subprocess, "run", fake)
    return fake


@pytest.fixture
def dump(tmp_path: Path) -> Path:
    path = tmp_path / "brain.dump"
    path.write_bytes(b"synthetic archive fixture")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    Path(str(path) + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("database", ["other_database", "brain"])
def test_restore_uses_current_environment_after_settings_cache_pollution(
    runner: FakeRunner, dump: Path, monkeypatch: pytest.MonkeyPatch, database: str
) -> None:
    get_settings.cache_clear()
    try:
        with monkeypatch.context() as pollution:
            pollution.setenv(
                "POSTGRES_URL",
                f"postgresql+asyncpg://other:old-password@other-host:5439/{database}",
            )
            cached = get_settings()
        assert get_settings() is cached

        assert restore_cli.main(["--dump", str(dump)]) == 0
        assert runner.restores
        for args, env in runner.calls:
            if args[0] == "psql" or "--dbname" in args:
                assert args[args.index("--host") + 1] == "postgres"
                assert args[args.index("--port") + 1] == "5432"
                assert args[args.index("--username") + 1] == "brain"
            assert env["PGPASSWORD"] == "test@password"
        assert get_settings() is cached
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("runtime_failure", "reason_code"),
    [(False, "unrecognized_initialization"), (True, "restore_runtime_error")],
)
def test_refusal_reaches_stderr_after_logger_cache_pollution(
    runner: FakeRunner,
    dump: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    runtime_failure: bool,
    reason_code: str,
) -> None:
    previous_config = structlog.get_config().copy()
    log_output = io.StringIO()
    try:
        structlog.configure(
            processors=[structlog.processors.JSONRenderer()],
            logger_factory=structlog.PrintLoggerFactory(file=log_output),
            cache_logger_on_first_use=True,
        )
        monkeypatch.setattr(restore_cli, "logger", structlog.get_logger("restore-polluted"))
        restore_cli.logger.info("warm_cached_logger")
        structlog.configure(**previous_config)
        if runtime_failure:

            def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
                raise OSError("private error with test@password")

            monkeypatch.setattr(restore_cli.subprocess, "run", fail)
        else:
            runner.relations = [["alembic_version", "r"]]
        assert restore_cli.main(["--dump", str(dump)]) != 0
        captured = capsys.readouterr()
        assert reason_code in captured.err
        assert "test@password" not in captured.out + captured.err + log_output.getvalue()
        assert reason_code in log_output.getvalue()
        assert not runner.mutations
    finally:
        structlog.configure(**previous_config)


def test_empty_restore_is_transactional_and_marked_only_after_success(
    runner: FakeRunner, dump: Path
) -> None:
    assert restore_cli.main(["--dump", str(dump)]) == 0
    assert len(runner.restores) == 1
    args = runner.restores[0]
    assert {"--single-transaction", "--exit-on-error", "--no-owner"} <= set(args)
    assert "--no-acl" not in args
    assert args[args.index("--dbname") + 1] == "brain"
    assert args[-1] == str(dump)
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    assert runner.marker is not None
    assert re.fullmatch(
        rf"brain-v42-restore sha256={digest} at=\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}:\d{{2}}Z",
        runner.marker,
    )
    assert "COMMENT ON DATABASE brain" in runner.calls[-1][0][-1]
    assert "CREATE ROLE codex_ro" in runner.mutations[1][-1]
    assert not any("CREATE ROLE brain_app" in arg for args, _ in runner.calls for arg in args)


@pytest.mark.parametrize("sidecar", [None, "b" * 64, "invalid", "a" * 64 + "\n" + "b" * 64])
def test_invalid_or_missing_checksum_prevents_all_mutations(
    runner: FakeRunner, dump: Path, sidecar: str | None
) -> None:
    path = Path(str(dump) + ".sha256")
    if sidecar is None:
        path.unlink()
    else:
        path.write_text(sidecar, encoding="utf-8")
    assert restore_cli.main(["--dump", str(dump)]) != 0
    assert not runner.mutations
    assert not any(args[0] == "pg_restore" for args, _ in runner.calls)


def test_marked_database_is_a_noop_even_when_dump_is_unavailable(
    runner: FakeRunner, tmp_path: Path
) -> None:
    runner.relations = [["alembic_version", "r"], ["decisions", "r"]]
    runner.marker = MARKER
    assert restore_cli.main(["--dump", str(tmp_path / "absent.dump")]) == 0
    assert not runner.mutations
    assert not any(args[0] == "pg_restore" for args, _ in runner.calls)


@pytest.mark.parametrize(
    ("relations", "rows", "marker"),
    [
        ([["alembic_version", "r"]], 1, None),
        ([["foreign", "r"]], 1, None),
        ([["alembic_version", "r"], ["foreign", "v"]], 1, None),
        ([["alembic_version", "r"]], 2, MARKER),
        ([["alembic_version", "r"]], 0, MARKER),
        ([["foreign", "r"]], 1, MARKER),
        ([["alembic_version", "v"]], 1, MARKER),
        ([["alembic_version", "r"]], 1, "foreign marker"),
        ([["alembic_version", "r"]], 1, MARKER.replace("2026-10-06", "2026-99-99")),
    ],
)
def test_partial_or_foreign_state_fails_closed(
    runner: FakeRunner,
    dump: Path,
    capsys: pytest.CaptureFixture[str],
    relations: list[list[str]],
    rows: int,
    marker: str | None,
) -> None:
    runner.relations, runner.rows, runner.marker = relations, rows, marker
    assert restore_cli.main(["--dump", str(dump)]) != 0
    captured = capsys.readouterr()
    assert "unrecognized_initialization" in captured.err
    assert not runner.mutations


def test_restore_failure_leaves_no_marker(runner: FakeRunner, dump: Path) -> None:
    runner.restore_code = 7
    assert restore_cli.main(["--dump", str(dump)]) != 0
    assert runner.restores
    assert runner.marker is None
    assert not any("COMMENT ON DATABASE" in arg for args, _ in runner.calls for arg in args)


@pytest.mark.parametrize("owner", ["foreign", '"foreign role"'])
def test_foreign_toc_owner_is_refused_before_bootstrap(
    runner: FakeRunner, dump: Path, owner: str
) -> None:
    runner.toc += f"217; 1259 12347 TABLE public.foreign table_name {owner}\n"
    assert restore_cli.main(["--dump", str(dump)]) != 0
    assert not runner.mutations


@pytest.mark.parametrize(
    "acl",
    [
        "GRANT SELECT ON TABLE public.decisions TO foreign;",
        'GRANT SELECT ON TABLE public.decisions TO "foreign role";',
        "GRANT SELECT ON TABLE public.decisions TO codex_ro, foreign;",
        "ALTER DEFAULT PRIVILEGES FOR ROLE foreign IN SCHEMA public GRANT SELECT ON TABLES TO codex_ro;",
        "ALTER DEFAULT PRIVILEGES FOR ROLE brain IN SCHEMA public GRANT SELECT ON TABLES TO foreign;",
        "ALTER DEFAULT PRIVILEGES FOR ROLE brain REVOKE SELECT ON TABLES FROM foreign;",
    ],
)
def test_foreign_acl_role_is_refused_before_bootstrap(
    runner: FakeRunner, dump: Path, acl: str
) -> None:
    runner.schema += acl + "\n"
    assert restore_cli.main(["--dump", str(dump)]) != 0
    assert not runner.mutations


def test_missing_codex_role_is_bootstrapped_as_nologin(runner: FakeRunner, dump: Path) -> None:
    assert restore_cli.main(["--dump", str(dump)]) == 0
    bootstrap = next(args for args, _ in runner.calls if "CREATE ROLE" in args[-1])
    sql = bootstrap[-1]
    assert "pg_roles" in sql and "rolname = 'codex_ro'" in sql
    assert "CREATE ROLE codex_ro NOLOGIN" in sql
    assert runner.calls.index(next(call for call in runner.calls if call[0] == bootstrap)) < next(
        i for i, (args, _) in enumerate(runner.calls) if args in runner.restores
    )


def test_public_schema_predefined_owner_is_accepted(runner: FakeRunner, dump: Path) -> None:
    runner.toc += "4602; 0 0 ACL - SCHEMA public pg_database_owner\n"
    runner.schema += (
        "ALTER SCHEMA public OWNER TO pg_database_owner;\n"
        "GRANT ALL ON SCHEMA public TO pg_database_owner;\n"
    )
    assert restore_cli.main(["--dump", str(dump)]) == 0
    assert runner.restores
    assert not any(
        "CREATE ROLE pg_database_owner" in arg for args, _ in runner.calls for arg in args
    )


@pytest.mark.parametrize("role", ["postgres", "pg_read_all_data", "pg_write_all_data"])
def test_other_predefined_roles_remain_refused(runner: FakeRunner, dump: Path, role: str) -> None:
    runner.toc += f"4602; 0 0 ACL - SCHEMA public {role}\n"
    assert restore_cli.main(["--dump", str(dump)]) != 0
    assert not runner.mutations


@pytest.mark.parametrize("mention", ["owner", "grantee", "default_acl"])
def test_named_brain_app_is_created_before_restore(
    runner: FakeRunner, dump: Path, mention: str
) -> None:
    if mention == "owner":
        runner.toc += "4603; 0 0 ACL public TABLE decisions brain_app\n"
    elif mention == "grantee":
        # TOC ACL entries only name the owner; the recipient is in the rendered SQL.
        runner.toc += "4603; 0 0 ACL public TABLE decisions brain\n"
        runner.schema += "GRANT SELECT, INSERT ON TABLE public.decisions TO brain_app;\n"
    else:
        runner.schema += (
            "ALTER DEFAULT PRIVILEGES FOR ROLE brain IN SCHEMA public "
            "GRANT SELECT ON TABLES TO brain_app;\n"
        )
    assert restore_cli.main(["--dump", str(dump)]) == 0
    assert "brain_app" in runner.created_roles
    assert runner.roles["brain_app"] == "NOLOGIN"
    bootstrap_index = next(
        i for i, (args, _) in enumerate(runner.calls) if "CREATE ROLE brain_app" in args[-1]
    )
    restore_index = next(i for i, (args, _) in enumerate(runner.calls) if args in runner.restores)
    assert bootstrap_index < restore_index


def test_unnamed_brain_app_is_not_bootstrapped(runner: FakeRunner, dump: Path) -> None:
    runner.schema += "-- brain_app is not a grant recipient in this archive\n"
    assert restore_cli.main(["--dump", str(dump)]) == 0
    assert "brain_app" not in runner.roles
    assert not any("CREATE ROLE brain_app" in arg for args, _ in runner.calls for arg in args)


@pytest.mark.parametrize("role", ["brain_app", "codex_ro"])
def test_existing_roles_are_preserved(runner: FakeRunner, dump: Path, role: str) -> None:
    runner.schema += "GRANT SELECT ON TABLE public.decisions TO brain_app;\n"
    runner.roles[role] = "existing operator attributes"
    assert restore_cli.main(["--dump", str(dump)]) == 0
    assert runner.roles[role] == "existing operator attributes"
    assert role not in runner.created_roles
    assert not any("ALTER ROLE" in arg for args, _ in runner.calls for arg in args)


@pytest.mark.parametrize(
    "step", ["pg_class", "--list", "--schema-only", "CREATE ROLE", "COMMENT ON"]
)
def test_subprocess_failure_is_sanitized_and_never_reports_success(
    runner: FakeRunner, dump: Path, step: str, capsys: pytest.CaptureFixture[str]
) -> None:
    runner.fail_on = step
    assert restore_cli.main(["--dump", str(dump)]) != 0
    assert runner.marker is None
    captured = capsys.readouterr()
    assert "private database error" not in captured.out + captured.err


def test_credentials_are_only_passed_in_password_environment(
    runner: FakeRunner, dump: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert restore_cli.main(["--dump", str(dump)]) == 0
    for args, env in runner.calls:
        assert env["PGPASSWORD"] == "test@password"
        assert "POSTGRES_URL" not in env and "BRAIN_POSTGRES_URL" not in env
        assert "test@password" not in " ".join(args)
        assert "postgresql" not in " ".join(args)
    captured = capsys.readouterr()
    assert "test@password" not in captured.out + captured.err
    assert "test%40password" not in captured.out + captured.err


def test_console_entry_point_targets_sync_main() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert (
        metadata["project"]["scripts"]["brain-v42-restore"] == "brain_v42.scripts.restore_cli:main"
    )


def test_schema_owner_with_allowed_suffix_is_still_refused(runner: FakeRunner, dump: Path) -> None:
    runner.toc += "217; 1259 12347 TABLE public table_name foreign brain\n"
    runner.schema += 'ALTER TABLE public.table_name OWNER TO "foreign brain";\n'
    assert restore_cli.main(["--dump", str(dump)]) != 0
    assert not runner.mutations


def test_default_acl_recipient_in_allowlist_is_preserved(runner: FakeRunner, dump: Path) -> None:
    runner.schema += (
        "ALTER DEFAULT PRIVILEGES FOR ROLE brain IN SCHEMA public "
        "GRANT SELECT ON TABLES TO codex_ro;\n"
    )
    assert restore_cli.main(["--dump", str(dump)]) == 0
    assert runner.restores


def test_granted_by_role_is_validated(runner: FakeRunner, dump: Path) -> None:
    runner.schema += "GRANT SELECT ON TABLE public.decisions TO codex_ro GRANTED BY brain;\n"
    assert restore_cli.main(["--dump", str(dump)]) == 0


def test_missing_executable_error_never_leaks_credentials(
    monkeypatch: pytest.MonkeyPatch,
    runner: FakeRunner,
    dump: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("private error with test@password")

    monkeypatch.setattr(restore_cli.subprocess, "run", fail)
    assert restore_cli.main(["--dump", str(dump)]) != 0
    captured = capsys.readouterr()
    assert "test@password" not in captured.out + captured.err
