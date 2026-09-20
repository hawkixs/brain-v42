"""Sources a probe reads through: PostgreSQL, the imported release, and restricted host files.

A probe never opens its own connection. The registry opens a source for the
probe's target, runs the probe inside it, then reads the source's identity
INSIDE THE SAME TRANSACTION, so the value and the identity come from one
connection and one snapshot. The 2026-09-12 failure mode this exists for: a
probe pointed at ``brain_test`` (then at 052) would have falsified a true
statement about production. With the identity measured next to the value and
compared with what the operator declared, that probe answers
``unreadable (target_mismatch)`` instead.

The transaction shape is the one of ``plan_index_repair_store.py`` — a session
transaction opened with ``SET TRANSACTION … READ ONLY`` — plus the isolation
level this design needs for a single snapshot. A probe that tried to write
fails with PostgreSQL's own read-only error, not with a mock; nothing is ever
committed, the transaction is rolled back on exit.

A PostgreSQL identity proves the cluster that answered in the same snapshot. A
release identity proves which release the process imported, not what that
release contains. A host identity proves the hostname the process sees, not
who authored a file read beneath the restricted root.
"""

from __future__ import annotations

import re
import socket
import stat
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import sqlalchemy as sa

from brain_v42.facts.model import (
    HostIdentity,
    IdentityUnreadableError,
    ReleaseIdentity,
    SourceIdentity,
)

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: First statement of the source transaction. ``SET TRANSACTION`` must precede
#: any query of the transaction, which is why the session may not have run
#: anything before it; the registry guarantees that by opening the source itself.
_BEGIN_READ_ONLY_SNAPSHOT = sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")

#: The four fields of the PostgreSQL identity, read in the same transaction as
#: the value. ``pg_control_system()`` gives the cluster's 64-bit identifier
#: (assigned at initdb — collision-resistant, kept by a physical restore,
#: changed by a logical restore into a fresh cluster); ``inet_server_addr()``
#: is NULL over a Unix socket, which the declaration may not use, and the
#: address carries a ``/32`` or ``/128`` suffix that ``SourceIdentity`` strips.
_IDENTITY_SQL = sa.text(
    "SELECT (SELECT system_identifier FROM pg_control_system())::text AS system_identifier, "
    "current_database() AS database, "
    "inet_server_addr()::text AS server_addr, "
    "inet_server_port() AS server_port"
)
_RELEASE_SHA = re.compile(r"[0-9a-f]{40}")
_MAX_HOST_FILE_BYTES = 65536


def release_sha_from_path(path: Path) -> str:
    """Read the release SHA from the immutable release layout that imported a module."""
    parts = path.resolve().parts
    for index, part in enumerate(parts):
        if part == "releases":
            if index + 1 >= len(parts) or _RELEASE_SHA.fullmatch(parts[index + 1]) is None:
                raise ValueError("path has no immutable release SHA")
            return parts[index + 1]
    raise ValueError("path has no immutable release SHA")


def read_text_under(root: Path, relative: str) -> tuple[str, int]:
    """Read one small UTF-8 regular file without following a relative symlink."""
    relative_path = Path(relative)
    if not relative or relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("relative path must be non-empty, relative, and contain no '..'")

    unresolved = root
    for component in relative_path.parts:
        unresolved = unresolved / component
        if unresolved.is_symlink():
            raise ValueError("relative path must not contain a symlink")
    resolved_root = root.resolve()
    resolved = unresolved.resolve()
    if resolved_root not in resolved.parents:
        raise ValueError("relative path must resolve strictly inside root")

    metadata = unresolved.stat()
    if stat.S_ISREG(metadata.st_mode) and metadata.st_size > _MAX_HOST_FILE_BYTES:
        raise ValueError("regular file must be at most 65536 bytes")
    return unresolved.read_text(encoding="utf-8", errors="strict"), int(metadata.st_mtime)


class PostgresSourceSession:
    """What a PostgreSQL probe receives: the session of one read-only snapshot."""

    __slots__ = ("session",)

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def identity(self) -> SourceIdentity:
        """Measure who answered, inside the same transaction as the probe's value.

        A NULL address (Unix socket) or a role that may not execute
        ``pg_control_system()`` raises; the registry turns that into
        ``identity_unreadable``, never into a measured value.
        """
        if not self.session.in_transaction():
            # A probe that ended the transaction would let the identity run in
            # a fresh one: value and identity would no longer be one snapshot.
            raise ValueError("source identity must be read inside the source transaction")
        row = (await self.session.execute(_IDENTITY_SQL)).mappings().one()
        if row["server_addr"] is None:
            raise ValueError("source identity has no server address (Unix socket connection)")
        return SourceIdentity.from_mapping(
            {
                "system_identifier": row["system_identifier"],
                "database": row["database"],
                "server_addr": row["server_addr"],
                "server_port": int(row["server_port"]),
            }
        )


class PostgresSourceFactory:
    """Open one read-only REPEATABLE READ transaction per probe run, roll it back on exit."""

    __slots__ = ("_session_factory",)

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    def __call__(self) -> AbstractAsyncContextManager[PostgresSourceSession]:
        return self._open()

    @asynccontextmanager
    async def _open(self) -> AsyncIterator[PostgresSourceSession]:
        async with self._session_factory() as session, session.begin() as transaction:
            try:
                # The transaction's first statement, as the precedent does it.
                await session.execute(_BEGIN_READ_ONLY_SNAPSHOT)
                yield PostgresSourceSession(session)
            finally:
                # Nothing a source reads is ever committed, even a read-only
                # transaction: the snapshot ends here, whatever happened inside,
                # and the `begin()` context finds nothing left to commit.
                await transaction.rollback()


class ReleaseSourceSession:
    """Read the release identity of the package that is executing this process."""

    __slots__ = ("_package_file", "_version")

    def __init__(self, package_file: Path, version: Callable[[], str]) -> None:
        self._package_file = package_file
        self._version = version

    async def identity(self) -> ReleaseIdentity:
        """Return immutable release evidence or raise when this process is a checkout."""
        try:
            release_sha = release_sha_from_path(self._package_file)
        except ValueError as exc:
            raise IdentityUnreadableError(str(exc)) from exc
        package_version = self._version()
        try:
            return ReleaseIdentity(release_sha, package_version)
        except ValueError as exc:
            if package_version == "dev":
                raise IdentityUnreadableError(str(exc)) from exc
            raise


class ReleaseSourceFactory:
    """Open a release source without I/O; imported package paths are resolved at use time."""

    __slots__ = ("_package_file", "_version")

    def __init__(
        self,
        package_file: Path | None = None,
        version: Callable[[], str] | None = None,
    ) -> None:
        self._package_file = package_file
        self._version = version

    def __call__(self) -> AbstractAsyncContextManager[ReleaseSourceSession]:
        return self._open()

    @asynccontextmanager
    async def _open(self) -> AsyncIterator[ReleaseSourceSession]:
        package_file = self._package_file
        version = self._version
        if package_file is None or version is None:
            import brain_v42
            from brain_v42.release import package_version

            if package_file is None:
                if brain_v42.__file__ is None:
                    raise ValueError("brain_v42 has no package file")
                package_file = Path(brain_v42.__file__)
            if version is None:
                version = package_version
        yield ReleaseSourceSession(package_file, version)


class HostSourceSession:
    """Expose only restricted text reads and the hostname the process reports."""

    __slots__ = ("root", "_hostname")

    def __init__(self, root: Path, hostname: Callable[[], str]) -> None:
        self.root = root
        self._hostname = hostname

    def read_text(self, relative: str) -> tuple[str, int]:
        """Read a bounded local configuration file under the fixed source root."""
        return read_text_under(self.root, relative)

    async def identity(self) -> HostIdentity:
        """Return the hostname the process observes, normalized by the identity model."""
        return HostIdentity(self._hostname())


class HostSourceFactory:
    """Open a host source tied to the one root composition chose."""

    __slots__ = ("_root", "_hostname")

    def __init__(self, root: Path, hostname: Callable[[], str] = socket.gethostname) -> None:
        self._root = root
        self._hostname = hostname

    def __call__(self) -> AbstractAsyncContextManager[HostSourceSession]:
        return self._open()

    @asynccontextmanager
    async def _open(self) -> AsyncIterator[HostSourceSession]:
        yield HostSourceSession(self._root, self._hostname)
