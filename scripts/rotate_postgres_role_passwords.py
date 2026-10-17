#!/usr/bin/env python3
"""Rotate the Compose PostgreSQL owner and runtime passwords without displaying secrets."""

from __future__ import annotations

import argparse
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

Runner = Callable[[list[str], bytes], subprocess.CompletedProcess[bytes]]
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
    for snapshot, (_name, key, role) in zip(snapshots[2:], _FILES[2:], strict=True):
        assert key is not None
        _index, dsn = _environment_dsn(snapshot.content, key)
        password = _validated_dsn(dsn, role)
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


def _apply_passwords(
    container: str, passwords: tuple[str, str], known_passwords: tuple[str, ...], runner: Runner
) -> None:
    # Pin standard strings so even an old password containing backslashes is a
    # literal during restore. No secret ever enters argv or the child environment.
    literals = ["'" + password.replace("'", "''") + "'" for password in passwords]
    sql = (
        "SET log_statement = 'none';\n"
        "BEGIN;\n"
        "SET LOCAL standard_conforming_strings = on;\n"
        f"ALTER ROLE brain PASSWORD {literals[0]};\n"
        f"ALTER ROLE brain_app PASSWORD {literals[1]};\n"
        "COMMIT;\n"
    ).encode()
    argv = [
        "docker",
        "exec",
        "-i",
        container,
        "psql",
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
        raise RotationError("PostgreSQL password update failed; child output suppressed") from None
    if result.returncode != 0:
        tail = _redacted_tail(result, known_passwords)
        message = f"PostgreSQL password update failed (return code {result.returncode})"
        raise RotationError(message + ("\n" + tail if tail else ""))


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
        current = [_read_snapshot(path) for path in paths]
        old_passwords = _preflight(current, consistent=not bool(args.restore))
        backup = None
        snapshots = current
        if args.restore:
            backup, snapshots = _restore_snapshots(args.backup_root, args.restore, paths)
        if args.dry_run:
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
        _apply_passwords(
            args.container, passwords, (*old_passwords, *passwords), runner or _docker_runner
        )
        for snapshot, payload in zip(snapshots, payloads, strict=True):
            _atomic_write(snapshot.path, payload, snapshot)
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
