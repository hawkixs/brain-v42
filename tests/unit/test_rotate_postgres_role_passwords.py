"""Secret-free PostgreSQL owner/runtime rotation using a fake Docker boundary."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import pytest
import scripts.rotate_postgres_role_passwords as rotation

OLD_OWNER = "old-owner'quote\\slash"
OLD_APP = "old-app/@:percent%"
NEW_OWNER = "new-owner'quote/@:percent%"
NEW_APP = "new-app-canary_0123456789"
NAMES = (
    "postgres-password",
    "brain-app-password",
    "postgres-owner.env",
    "postgres-app.env",
    "delivery-observer.env",
)
FLAGS = (
    "--postgres-password",
    "--brain-app-password",
    "--postgres-owner-env",
    "--postgres-app-env",
    "--delivery-observer-env",
)
OBSERVER_PREFIX = b"# Keep these bytes\r\nBRAIN_DELIVERY_ENABLED=true\r\n\r\n"
OBSERVER_SUFFIX = b"BRAIN_DELIVERY_GITHUB_APP_ID=123\r\nOTHER='untouched'  "


class FakeRunner:
    """Record the only boundary allowed to receive plaintext SQL."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[tuple[list[str], bytes]] = []

    def __call__(self, argv: list[str], stdin: bytes) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((argv, stdin))
        # Both streams may echo complete SQL on success or failure.
        return subprocess.CompletedProcess(argv, self.returncode, stdin, stdin)


@pytest.fixture
def files(tmp_path: Path) -> list[Path]:
    owner_dsn = f"postgresql+asyncpg://brain:{quote(OLD_OWNER, safe='')}@postgres:5432/brain"
    app_dsn = f"postgresql+asyncpg://brain_app:{quote(OLD_APP, safe='')}@postgres:5432/brain"
    contents = (
        (OLD_OWNER + "\r\n").encode(),
        OLD_APP.encode(),
        f"POSTGRES_URL={owner_dsn}\n".encode(),
        f"POSTGRES_URL='{app_dsn}'\n".encode(),
        OBSERVER_PREFIX + f"BRAIN_DELIVERY_POSTGRES_URL={app_dsn}\r\n".encode() + OBSERVER_SUFFIX,
    )
    paths = [tmp_path / name for name in NAMES]
    for path, content in zip(paths, contents, strict=True):
        path.write_bytes(content)
        path.chmod(0o600)
    return paths


@pytest.fixture
def argv(files: list[Path], tmp_path: Path) -> list[str]:
    args = [value for flag, path in zip(FLAGS, files, strict=True) for value in (flag, str(path))]
    return [*args, "--backup-root", str(tmp_path / "backups"), "--container", "test-postgres"]


@pytest.fixture
def generated(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    sizes: list[int] = []
    values = iter((NEW_OWNER, NEW_APP))

    def generate(size: int) -> str:
        sizes.append(size)
        return next(values)

    monkeypatch.setattr(rotation.secrets, "token_urlsafe", generate)
    return sizes


def _dsn(path: Path, key: str = "POSTGRES_URL") -> str:
    value = next(
        line.split("=", 1)[1]
        for line in path.read_text().splitlines()
        if line.startswith(key + "=")
    )
    return value.strip("'\"")


def _assert_no_secrets(text: str) -> None:
    for secret in (OLD_OWNER, OLD_APP, NEW_OWNER, NEW_APP):
        assert secret not in text
        assert quote(secret, safe="") not in text
        assert secret.replace("'", "''") not in text


def test_rotation_outputs_only_paths_and_sends_secrets_only_on_stdin(
    argv: list[str], files: list[Path], generated: list[int], capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner()
    assert rotation.main(argv, runner=runner) == 0
    output = capsys.readouterr()
    backup = next((files[0].parent / "backups").iterdir())
    assert output.out.splitlines() == ["ok", *(str(path) for path in files), str(backup)]
    assert output.err == ""
    _assert_no_secrets(output.out + output.err)
    assert generated == [32, 32]
    assert len(runner.calls) == 1
    command, sql = runner.calls[0]
    assert command == [
        "docker",
        "exec",
        "-i",
        "test-postgres",
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "brain",
        "-d",
        "brain",
    ]
    _assert_no_secrets(" ".join(command))
    assert sql.startswith(b"SET log_statement = 'none';\n")
    assert sql.index(b"BEGIN;") < sql.index(b"ALTER ROLE brain PASSWORD")
    assert sql.index(b"ALTER ROLE brain_app PASSWORD") < sql.index(b"COMMIT;")
    assert b"new-owner''quote/@:percent%" in sql
    assert NEW_APP.encode() in sql


def test_rewrites_matching_dsn_users_and_preserves_other_bytes_and_newlines(
    argv: list[str], files: list[Path], generated: list[int]
) -> None:
    assert rotation.main(argv, runner=FakeRunner()) == 0
    assert files[0].read_bytes() == (NEW_OWNER + "\r\n").encode()
    assert files[1].read_bytes() == NEW_APP.encode()
    for path, user, password, key in (
        (files[2], "brain", NEW_OWNER, "POSTGRES_URL"),
        (files[3], "brain_app", NEW_APP, "POSTGRES_URL"),
        (files[4], "brain_app", NEW_APP, "BRAIN_DELIVERY_POSTGRES_URL"),
    ):
        dsn = _dsn(path, key)
        parsed = urlsplit(dsn)
        assert parsed.username == user
        assert unquote(parsed.password or "") == password
        assert parsed.hostname == "postgres"
        assert parsed.port == 5432
        assert parsed.path == "/brain"
        assert quote(password, safe="") in dsn
    observer = files[4].read_bytes()
    assert observer.startswith(OBSERVER_PREFIX)
    assert observer.endswith(OBSERVER_SUFFIX)
    assert b"\r\nBRAIN_DELIVERY_GITHUB_APP_ID" in observer


def test_private_backup_preserves_all_original_files_before_database_change(
    argv: list[str], files: list[Path], generated: list[int]
) -> None:
    originals = {path.name: (path.read_bytes(), path.stat()) for path in files}

    def runner(command: list[str], sql: bytes) -> subprocess.CompletedProcess[bytes]:
        backup = next((files[0].parent / "backups").iterdir())
        assert stat.S_IMODE(backup.stat().st_mode) == 0o700
        assert {path.name for path in backup.iterdir()} == set(NAMES)
        for name, (content, metadata) in originals.items():
            copy = backup / name
            assert copy.read_bytes() == content
            assert stat.S_IMODE(copy.stat().st_mode) == 0o600
            assert (copy.stat().st_uid, copy.stat().st_gid) == (metadata.st_uid, metadata.st_gid)
        assert all(path.read_bytes() == originals[path.name][0] for path in files)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    assert rotation.main(argv, runner=runner) == 0


def test_atomic_replacement_preserves_ownership_and_mode(
    argv: list[str], files: list[Path], generated: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    originals = {path: path.stat() for path in files}
    chowns: list[tuple[int, int]] = []
    real_chown = os.chown

    def chown(path: Any, uid: int, gid: int, **kwargs: Any) -> None:
        chowns.append((uid, gid))
        real_chown(path, uid, gid, **kwargs)

    monkeypatch.setattr(rotation.os, "chown", chown)
    assert rotation.main(argv, runner=FakeRunner()) == 0
    assert len(chowns) == 10  # Five backup copies and five replacements.
    for path, original in originals.items():
        assert (original.st_uid, original.st_gid) in chowns
        current = path.stat()
        assert (current.st_uid, current.st_gid) == (original.st_uid, original.st_gid)
        assert stat.S_IMODE(current.st_mode) == 0o600
        assert current.st_ino != original.st_ino
    assert not [path for path in files[0].parent.iterdir() if path.name.startswith(".")]


def test_default_runner_does_not_inherit_secret_environment(
    argv: list[str],
    generated: list[int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setenv("PGPASSWORD", NEW_OWNER)
    monkeypatch.setenv("POSTGRES_URL", NEW_APP)
    monkeypatch.setenv("UNRELATED_SECRET", OLD_APP)

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        captured.update(kwargs)
        captured["argv"] = command
        return subprocess.CompletedProcess(command, 0, kwargs["input"], kwargs["input"])

    monkeypatch.setattr(rotation.subprocess, "run", run)
    assert rotation.main(argv) == 0
    assert captured["capture_output"] is True
    assert captured["check"] is False
    assert isinstance(captured["input"], bytes)
    assert "PGPASSWORD" not in captured["env"]
    assert "POSTGRES_URL" not in captured["env"]
    assert "UNRELATED_SECRET" not in captured["env"]
    _assert_no_secrets(str(captured["env"]) + str(captured["argv"]))
    output = capsys.readouterr()
    _assert_no_secrets(output.out + output.err)
    assert os.environ["PGPASSWORD"] == NEW_OWNER


def test_runner_failure_keeps_files_unchanged_and_redacts_echoed_sql(
    argv: list[str], files: list[Path], generated: list[int], capsys: pytest.CaptureFixture[str]
) -> None:
    originals = {path: (path.read_bytes(), path.stat().st_ino) for path in files}
    assert rotation.main(argv, runner=FakeRunner(returncode=7)) != 0
    output = capsys.readouterr()
    assert output.out == ""
    assert "7" in output.err
    _assert_no_secrets(output.out + output.err)
    for path, (content, inode) in originals.items():
        assert path.read_bytes() == content
        assert path.stat().st_ino == inode


def test_runner_exception_is_never_rendered(
    argv: list[str], files: list[Path], generated: list[int], capsys: pytest.CaptureFixture[str]
) -> None:
    originals = [path.read_bytes() for path in files]

    def fail(command: list[str], sql: bytes) -> subprocess.CompletedProcess[bytes]:
        raise RuntimeError(sql.decode())

    assert rotation.main(argv, runner=fail) != 0
    output = capsys.readouterr()
    _assert_no_secrets(output.out + output.err)
    assert [path.read_bytes() for path in files] == originals


@pytest.mark.parametrize("index", range(5))
@pytest.mark.parametrize("fault", ("missing", "symlink", "mode", "directory"))
def test_preflight_refuses_unsafe_files_without_any_writes(
    argv: list[str], files: list[Path], index: int, fault: str
) -> None:
    path = files[index]
    if fault == "missing":
        path.unlink()
    elif fault == "symlink":
        target = path.with_suffix(".original")
        path.rename(target)
        path.symlink_to(target)
    elif fault == "directory":
        path.unlink()
        path.mkdir(mode=0o600)
    else:
        path.chmod(0o640)
    runner = FakeRunner()
    originals = {item: item.read_bytes() for item in files if item.is_file()}
    assert rotation.main(argv, runner=runner) != 0
    assert not runner.calls
    assert not (files[0].parent / "backups").exists()
    assert all(item.read_bytes() == content for item, content in originals.items())


@pytest.mark.parametrize("index", (2, 3, 4))
@pytest.mark.parametrize("fault", ("wrong_user", "duplicate", "missing_key", "invalid_dsn"))
def test_preflight_refuses_invalid_environment_without_writes(
    argv: list[str], files: list[Path], index: int, fault: str, capsys: pytest.CaptureFixture[str]
) -> None:
    path = files[index]
    content = path.read_bytes()
    key = b"BRAIN_DELIVERY_POSTGRES_URL" if index == 4 else b"POSTGRES_URL"
    if fault == "wrong_user":
        content = content.replace(b"//brain:", b"//other:").replace(b"//brain_app:", b"//other:")
    elif fault == "duplicate":
        content += b"\n" + next(line for line in content.splitlines() if line.startswith(key))
    elif fault == "missing_key":
        content = content.replace(key, b"UNMANAGED_KEY")
    else:
        content = content.replace(b"postgresql+asyncpg://", b"https://")
    path.write_bytes(content)
    runner = FakeRunner()
    assert rotation.main(argv, runner=runner) != 0
    assert not runner.calls
    assert not (files[0].parent / "backups").exists()
    assert path.read_bytes() == content
    output = capsys.readouterr()
    _assert_no_secrets(output.out + output.err)


def test_restore_reapplies_old_passwords_and_original_file_metadata(
    argv: list[str], files: list[Path], generated: list[int], capsys: pytest.CaptureFixture[str]
) -> None:
    originals = [(path.read_bytes(), path.stat()) for path in files]
    assert rotation.main(argv, runner=FakeRunner()) == 0
    capsys.readouterr()
    backup = next((files[0].parent / "backups").iterdir())
    timestamp = backup.name.removeprefix("pg-rotation-")
    runner = FakeRunner()
    assert rotation.main([*argv, "--restore", timestamp], runner=runner) == 0
    sql = runner.calls[0][1]
    assert sql.startswith(b"SET log_statement = 'none';\n")
    assert OLD_OWNER.replace("'", "''").encode() in sql
    assert OLD_APP.encode() in sql
    for path, (content, original) in zip(files, originals, strict=True):
        assert path.read_bytes() == content
        assert (path.stat().st_uid, path.stat().st_gid) == (original.st_uid, original.st_gid)
        assert stat.S_IMODE(path.stat().st_mode) == stat.S_IMODE(original.st_mode)
    output = capsys.readouterr()
    assert output.out.splitlines() == ["ok", *(str(path) for path in files), str(backup)]
    assert output.err == ""


def test_restore_can_recover_a_partially_rewritten_generation(
    argv: list[str], files: list[Path], generated: list[int]
) -> None:
    originals = [path.read_bytes() for path in files]
    assert rotation.main(argv, runner=FakeRunner()) == 0
    backup = next((files[0].parent / "backups").iterdir())
    files[0].write_bytes(originals[0])
    timestamp = backup.name.removeprefix("pg-rotation-")
    assert rotation.main([*argv, "--restore", timestamp], runner=FakeRunner()) == 0
    assert [path.read_bytes() for path in files] == originals


def test_dry_run_does_not_generate_call_runner_or_write(
    argv: list[str],
    files: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    originals = [(path.read_bytes(), path.stat().st_ino) for path in files]

    def fail_generation(_size: int) -> str:
        raise AssertionError("dry-run generated a password")

    monkeypatch.setattr(rotation.secrets, "token_urlsafe", fail_generation)
    runner = FakeRunner()
    assert rotation.main([*argv, "--dry-run"], runner=runner) == 0
    assert not runner.calls
    assert not (files[0].parent / "backups").exists()
    assert [(path.read_bytes(), path.stat().st_ino) for path in files] == originals
    output = capsys.readouterr()
    assert output.out.splitlines() == ["ok (dry run)", *(str(path) for path in files)]
    assert output.err == ""


def test_backup_failure_prevents_database_change(
    argv: list[str], files: list[Path], generated: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    originals = [path.read_bytes() for path in files]

    def fail_write(*_args: Any) -> None:
        raise OSError("backup write failed")

    monkeypatch.setattr(rotation, "_atomic_write", fail_write)
    runner = FakeRunner()
    assert rotation.main(argv, runner=runner) != 0
    assert not runner.calls
    assert [path.read_bytes() for path in files] == originals


def test_restore_failure_does_not_replace_any_current_file(
    argv: list[str], files: list[Path], generated: list[int], capsys: pytest.CaptureFixture[str]
) -> None:
    assert rotation.main(argv, runner=FakeRunner()) == 0
    capsys.readouterr()
    current = [path.read_bytes() for path in files]
    backup = next((files[0].parent / "backups").iterdir())
    timestamp = backup.name.removeprefix("pg-rotation-")
    assert rotation.main([*argv, "--restore", timestamp], runner=FakeRunner(9)) != 0
    assert [path.read_bytes() for path in files] == current
    output = capsys.readouterr()
    _assert_no_secrets(output.out + output.err)


@pytest.mark.parametrize("fault", ("symlink", "missing", "mode"))
def test_restore_refuses_an_unsafe_backup_before_calling_runner(
    argv: list[str], files: list[Path], generated: list[int], fault: str
) -> None:
    assert rotation.main(argv, runner=FakeRunner()) == 0
    backup = next((files[0].parent / "backups").iterdir())
    timestamp = backup.name.removeprefix("pg-rotation-")
    saved = backup / NAMES[0]
    if fault == "symlink":
        saved.unlink()
        saved.symlink_to(files[0])
    elif fault == "missing":
        saved.unlink()
    else:
        saved.chmod(0o644)
    runner = FakeRunner()
    current = [path.read_bytes() for path in files]
    assert rotation.main([*argv, "--restore", timestamp], runner=runner) != 0
    assert not runner.calls
    assert [path.read_bytes() for path in files] == current


def test_url_rewrite_preserves_query_containing_an_at_sign(
    argv: list[str], files: list[Path], generated: list[int]
) -> None:
    files[2].write_bytes(
        files[2].read_bytes().replace(b"/brain\n", b"/brain?application_name=ops@local\n")
    )
    assert rotation.main(argv, runner=FakeRunner()) == 0
    dsn = _dsn(files[2])
    assert unquote(urlsplit(dsn).password or "") == NEW_OWNER
    assert urlsplit(dsn).query == "application_name=ops@local"


def test_atomic_write_calls_chown_with_snapshot_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, int]] = []

    def chown(_path: Path, uid: int, gid: int) -> None:
        calls.append((uid, gid))

    monkeypatch.setattr(rotation.os, "chown", chown)
    target = tmp_path / "owner.env"
    original = rotation.FileSnapshot(target, b"old", 999, 1000, 0o600)
    rotation._atomic_write(target, b"new", original)
    assert calls == [(999, 1000)]
    assert target.read_bytes() == b"new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_failure_tail_redacts_before_truncation() -> None:
    password = "synthetic-boundary-password" * 100
    result = subprocess.CompletedProcess([], 1, b"", password.encode() + b" diagnostic")
    tail = rotation._redacted_tail(result, (password,))
    assert tail == "[REDACTED] diagnostic"


def test_cli_defaults_match_compose_paths() -> None:
    args = rotation._parser().parse_args([])
    assert args.container == "brain-v42-postgres"
    assert args.backup_root == Path("/root")
    for name in NAMES:
        assert getattr(args, name.replace(".", "_").replace("-", "_")) == (
            Path("/etc/brain-v42") / name
        )
