#!/usr/bin/env python3
"""Rotate the Compose PostgreSQL owner and runtime passwords without displaying secrets."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import hmac
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

Runner = Callable[[list[str], bytes], subprocess.CompletedProcess[bytes]]
_NON_ASCII_PASSWORD_ERROR = (
    "non-ASCII PostgreSQL passwords are refused until SASLprep is supported (brain ticket 4837bc35)"
)
_FILES = (
    ("postgres-password", None, "brain"),
    ("brain-app-password", None, "brain_app"),
    ("postgres-owner.env", "POSTGRES_URL", "brain"),
    ("postgres-app.env", "POSTGRES_URL", "brain_app"),
    ("delivery-observer.env", "BRAIN_DELIVERY_POSTGRES_URL", "brain_app"),
)
_SAFE_SUBPROCESS_ENV_KEYS = (
    "DOCKER_CERT_PATH",
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
    "DOCKER_TLS_VERIFY",
    "HOME",
    "PATH",
    "XDG_RUNTIME_DIR",
)


class RotationError(RuntimeError):
    """An operator error whose message contains no credentials."""


@dataclass(frozen=True)
class FileSnapshot:
    """Preserve bytes and metadata independently of later replacements."""

    path: Path
    content: bytes = field(repr=False)
    uid: int
    gid: int
    mode: int


@contextmanager
def _exclusive_lock(lock_path: Path) -> Iterator[None]:
    """Hold a non-blocking process lock for the entire mutating workflow."""
    lock_fd = -1
    try:
        lock_fd = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if lock_fd >= 0:
            os.close(lock_fd)
        raise RotationError(
            "another credential rotation is already active or lock is unsafe"
        ) from None
    try:
        yield
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_write(target: Path, payload: bytes, original: FileSnapshot) -> None:
    """Install complete bytes with the original ownership before publishing them."""
    temporary_fd, raw_path = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(raw_path)
    try:
        with os.fdopen(temporary_fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.chown(temporary, original.uid, original.gid)
            os.fchmod(stream.fileno(), original.mode)
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_snapshot(path: Path) -> FileSnapshot:
    # Reject symlinks before opening, and check the opened inode again. O_NONBLOCK
    # also prevents a substituted FIFO from hanging this privileged command.
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise RotationError("credential files must be regular files with mode 0600")
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise RotationError("credential paths must be absolute and contain no symlinks")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or opened.st_size > 128 * 1024
        ):
            raise RotationError("credential file is unsafe or changed during preflight")
        content = stream.read(128 * 1024 + 1)
    if len(content) > 128 * 1024:
        raise RotationError("credential file is too large")
    return FileSnapshot(path, content, opened.st_uid, opened.st_gid, stat.S_IMODE(opened.st_mode))


def _line_ending(line: bytes) -> bytes:
    if line.endswith(b"\r\n"):
        return b"\r\n"
    return b"\n" if line.endswith(b"\n") else b""


def _password(content: bytes) -> str:
    value = content.removesuffix(_line_ending(content)).decode("utf-8")
    if not value or any(character in value for character in "\r\n\0"):
        raise RotationError("password file must contain one non-empty value")
    return value


def _environment_dsn(content: bytes, key: str) -> tuple[int, str]:
    matches: list[tuple[int, str]] = []
    for index, line in enumerate(content.splitlines(keepends=True)):
        candidate = line.strip()
        if candidate.startswith(b"export "):
            candidate = candidate.removeprefix(b"export ").lstrip()
        name, separator, value = candidate.partition(b"=")
        if not separator or name.strip() != key.encode():
            continue
        value = value.strip()
        if value.startswith((b"'", b'"')):
            if len(value) < 2 or value[-1:] != value[:1]:
                raise RotationError("managed DSN quoting is invalid")
            value = value[1:-1]
        matches.append((index, value.decode("utf-8")))
    if len(matches) != 1:
        raise RotationError("environment must contain exactly one managed DSN")
    return matches[0]


def _validated_dsn(value: str, role: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "postgresql+asyncpg"
            and parsed.username == role
            and bool(parsed.password)
            and bool(parsed.hostname)
            and parsed.port is not None
            and 1 <= parsed.port <= 65535
            and bool(parsed.path.removeprefix("/"))
            and not parsed.fragment
            and not any(character.isspace() for character in value)
        )
        password = unquote(parsed.password or "", errors="strict")
    except (ValueError, UnicodeError):
        raise RotationError("managed PostgreSQL DSN is invalid") from None
    if not valid or not password or any(character in password for character in "\r\n\0"):
        raise RotationError("managed PostgreSQL DSN is invalid")
    return password


def _preflight(snapshots: list[FileSnapshot], *, consistent: bool) -> tuple[str, str]:
    owner, app = (_password(snapshot.content) for snapshot in snapshots[:2])
    if not owner.isascii() or not app.isascii():
        raise RotationError(_NON_ASCII_PASSWORD_ERROR)
    for snapshot, (_name, key, role) in zip(snapshots[2:], _FILES[2:], strict=True):
        assert key is not None
        _index, dsn = _environment_dsn(snapshot.content, key)
        password = _validated_dsn(dsn, role)
        if not password.isascii():
            raise RotationError(_NON_ASCII_PASSWORD_ERROR)
        if consistent and password != (owner if role == "brain" else app):
            raise RotationError("password files and managed DSNs disagree")
    return owner, app


def _generation_payloads(snapshots: list[FileSnapshot], passwords: tuple[str, str]) -> list[bytes]:
    payloads = []
    for snapshot, (_name, key, role) in zip(snapshots, _FILES, strict=True):
        password = passwords[0 if role == "brain" else 1]
        if key is None:
            payloads.append(password.encode() + _line_ending(snapshot.content))
            continue
        index, dsn = _environment_dsn(snapshot.content, key)
        # Keep the host, port, database and query spelling exactly as supplied.
        parsed = urlsplit(dsn)
        prefix = dsn.split("://", 1)[0]
        userinfo, destination = parsed.netloc.rsplit("@", 1)
        user = userinfo.split(":", 1)[0]
        suffix = dsn[len(prefix) + 3 + len(parsed.netloc) :]
        rewritten = f"{prefix}://{user}:{quote(password, safe='')}@{destination}{suffix}"
        lines = snapshot.content.splitlines(keepends=True)
        lines[index] = f"{key}={rewritten}".encode() + _line_ending(lines[index])
        payloads.append(b"".join(lines))
    return payloads


def _private_directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
        or path.resolve(strict=True) != path
    ):
        raise RotationError("backup directory must be owned by the operator with mode 0700")


def _backup(root: Path, snapshots: list[FileSnapshot]) -> Path:
    if not root.exists():
        root.mkdir(mode=0o700)
        root.chmod(0o700)
    _private_directory(root)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    directory = root / f"pg-rotation-{timestamp}"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    for snapshot, (name, _key, _role) in zip(snapshots, _FILES, strict=True):
        _atomic_write(directory / name, snapshot.content, snapshot)
    _fsync_directory(directory)
    _fsync_directory(root)
    return directory


def _restore_snapshots(
    root: Path, timestamp: str, paths: list[Path]
) -> tuple[Path, list[FileSnapshot]]:
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}(?:[0-9]{6})?Z", timestamp):
        raise RotationError(
            "restore requires the UTC timestamp printed in the backup directory name"
        )
    _private_directory(root)
    directory = root / f"pg-rotation-{timestamp}"
    _private_directory(directory)
    snapshots = [
        replace(_read_snapshot(directory / name), path=target)
        for target, (name, _key, _role) in zip(paths, _FILES, strict=True)
    ]
    _preflight(snapshots, consistent=True)
    return directory, snapshots


def _docker_runner(argv: list[str], stdin: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        check=False,
        timeout=60,
        env={key: os.environ[key] for key in _SAFE_SUBPROCESS_ENV_KEYS if key in os.environ},
    )


def _redacted_tail(result: subprocess.CompletedProcess[bytes], passwords: tuple[str, ...]) -> str:
    content = (result.stdout or b"") + b"\n" + (result.stderr or b"")
    variants = {
        variant.encode()
        for password in passwords
        for variant in (password, quote(password, safe=""), password.replace("'", "''"))
        if variant
    }
    # Redact BEFORE taking a tail; cutting first can expose part of a password.
    for variant in sorted(variants, key=len, reverse=True):
        content = content.replace(variant, b"[REDACTED]")
    return content[-1024:].decode("utf-8", errors="replace").strip()


def _scram_verifier(password: str) -> str:
    """Hash locally so PostgreSQL never receives a plaintext password in SQL.

    Passwords must be ASCII: preflight refuses non-ASCII inputs until SASLprep
    is supported (brain ticket 4837bc35).
    """
    iterations = 4096
    salt = secrets.token_bytes(16)
    # Generated token_urlsafe passwords are ASCII and need no SASLprep.
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    encoded_salt, encoded_stored, encoded_server = (
        base64.b64encode(value).decode("ascii") for value in (salt, stored_key, server_key)
    )
    return f"SCRAM-SHA-256${iterations}:{encoded_salt}${encoded_stored}:{encoded_server}"


def _run_psql(
    container: str,
    sql: bytes,
    known_passwords: tuple[str, ...],
    runner: Runner,
    *,
    operation: str,
) -> None:
    argv = [
        "docker",
        "exec",
        "-i",
        container,
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "brain",
        "-d",
        "brain",
    ]
    try:
        result = runner(argv, sql)
    except Exception:
        raise RotationError(f"PostgreSQL {operation} failed; child output suppressed") from None
    if result.returncode != 0:
        tail = _redacted_tail(result, known_passwords)
        message = f"PostgreSQL {operation} failed (return code {result.returncode})"
        raise RotationError(message + ("\n" + tail if tail else ""))


def _apply_passwords(
    container: str, passwords: tuple[str, str], known_passwords: tuple[str, ...], runner: Runner
) -> None:
    verifiers = tuple(_scram_verifier(password) for password in passwords)
    # Only locally generated SCRAM verifiers enter SQL. Disable utility tracking
    # and statement logging in this superuser session before starting the transaction.
    sql = (
        "SET log_statement = 'none';\n"
        "SET pg_stat_statements.track_utility = off;\n"
        "SET log_min_duration_statement = -1;\n"
        "SET log_min_error_statement = panic;\n"
        "BEGIN;\n"
        "SET LOCAL standard_conforming_strings = on;\n"
        f"ALTER ROLE brain PASSWORD '{verifiers[0]}';\n"
        f"ALTER ROLE brain_app PASSWORD '{verifiers[1]}';\n"
        "COMMIT;\n"
    ).encode("ascii")
    _run_psql(
        container,
        sql,
        (*known_passwords, *verifiers),
        runner,
        operation="password update",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    for name, _key, _role in _FILES:
        parser.add_argument(
            f"--{name.replace('.', '-')}", type=Path, default=Path("/etc/brain-v42") / name
        )
    parser.add_argument("--container", default="brain-v42-postgres")
    parser.add_argument("--backup-root", type=Path, default=Path("/root"))
    parser.add_argument("--restore", metavar="UTC_TIMESTAMP")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _recovery_instructions(backup: Path) -> str:
    timestamp = backup.name.removeprefix("pg-rotation-")
    return (
        f"Backup directory: {backup}\n"
        "Run: sudo python3 scripts/rotate_postgres_role_passwords.py "
        f"--restore {timestamp} (with the same path overrides, if used)"
    )


def main(argv: list[str] | None = None, *, runner: Runner | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        paths = [
            getattr(args, name.replace(".", "_").replace("-", "_")) for name, _key, _role in _FILES
        ]
        if len(set(paths)) != len(paths) or not args.backup_root.is_absolute():
            raise RotationError("credential paths must be distinct and backup root absolute")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", args.container):
            raise RotationError("invalid PostgreSQL container name")
        with _exclusive_lock(paths[0].parent / ".pg-rotation.lock"):
            current = [_read_snapshot(path) for path in paths]
            old_passwords = _preflight(current, consistent=not bool(args.restore))
            backup = None
            snapshots = current
            if args.restore:
                backup, snapshots = _restore_snapshots(args.backup_root, args.restore, paths)
            if args.dry_run:
                _run_psql(
                    args.container,
                    b"SELECT 1;",
                    old_passwords,
                    runner or _docker_runner,
                    operation="connectivity check",
                )
                print("ok (dry run)")
                for path in paths:
                    print(path)
                return 0
            if args.restore:
                passwords = _preflight(snapshots, consistent=True)
                payloads = [snapshot.content for snapshot in snapshots]
            else:
                passwords = (secrets.token_urlsafe(32), secrets.token_urlsafe(32))
                payloads = _generation_payloads(snapshots, passwords)
                backup = _backup(args.backup_root, snapshots)
            assert backup is not None
            try:
                _apply_passwords(
                    args.container,
                    passwords,
                    (*old_passwords, *passwords),
                    runner or _docker_runner,
                )
            except RotationError as error:
                raise RotationError(
                    f"{error}\nThe database may or may not hold the new passwords. "
                    + _recovery_instructions(backup)
                ) from None
            try:
                for snapshot, payload in zip(snapshots, payloads, strict=True):
                    _atomic_write(snapshot.path, payload, snapshot)
            except Exception:
                generation = "RESTORED" if args.restore else "NEW"
                raise RotationError(
                    f"Credential file update failed; the database already holds the {generation} "
                    "passwords. " + _recovery_instructions(backup)
                ) from None
    except RotationError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("PostgreSQL password rotation failed; secret details suppressed", file=sys.stderr)
        return 1
    print("ok")
    for path in paths:
        print(path)
    print(backup)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
