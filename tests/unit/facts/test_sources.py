"""The PostgreSQL source refuses what it cannot vouch for, without a database."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from brain_v42.facts.model import HostIdentity, IdentityUnreadableError, ReleaseIdentity
from brain_v42.facts.sources import (
    HostSourceFactory,
    HostSourceSession,
    PostgresSourceSession,
    ReleaseSourceFactory,
    ReleaseSourceSession,
    read_text_under,
    release_sha_from_path,
)


class _Rows:
    def __init__(self, row: dict[str, object]) -> None:
        self._row = row

    def mappings(self):  # type: ignore[no-untyped-def]
        return self

    def one(self) -> dict[str, object]:
        return self._row


class _Session:
    def __init__(self, row: dict[str, object], *, in_transaction: bool = True) -> None:
        self._row = row
        self._in_transaction = in_transaction

    def in_transaction(self) -> bool:
        return self._in_transaction

    async def execute(self, statement, *args, **kwargs):  # type: ignore[no-untyped-def]
        return _Rows(self._row)


_ROW: dict[str, object] = {
    "system_identifier": "7612696091383607335",
    "database": "brain",
    "server_addr": "172.31.0.4/32",
    "server_port": 5432,
}


@pytest.mark.asyncio
async def test_a_unix_socket_connection_has_no_address_and_is_refused() -> None:
    """NULL inet_server_addr(): the declaration may not use a socket, so the read cannot vouch."""
    with pytest.raises(ValueError, match="server address"):
        await PostgresSourceSession(_Session({**_ROW, "server_addr": None})).identity()  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_identity_outside_the_source_transaction_is_refused() -> None:
    """A probe that ended the transaction would let identity run in a fresh one: not one snapshot."""
    with pytest.raises(ValueError, match="transaction"):
        await PostgresSourceSession(_Session(_ROW, in_transaction=False)).identity()  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_identity_inside_the_transaction_is_the_four_measured_fields() -> None:
    identity = await PostgresSourceSession(_Session(_ROW)).identity()  # type: ignore[arg-type]
    assert identity.as_dict() == {
        "system_identifier": "7612696091383607335",
        "database": "brain",
        "server_addr": "172.31.0.4",
        "server_port": 5432,
    }


def test_release_sha_from_path_accepts_a_release_nested_under_another_releases_directory() -> None:
    """Only the `releases` component followed by a 40-hex segment names the release;
    an earlier `releases` directory in the path (a mirror, a backup root) does not
    make the process a checkout."""
    nested = Path(
        "/srv/releases/home/user/.local/share/brain-v42/releases/"
        + "c" * 40
        + "/venv/lib/python3.12/site-packages/brain_v42/__init__.py"
    )
    assert release_sha_from_path(nested) == "c" * 40


@pytest.mark.parametrize(
    "path",
    [
        Path(
            "/srv/releases/" + "a" * 40 + "/venv/lib/python3.12/site-packages/brain_v42/__init__.py"
        ),
        Path("/srv/releases/" + "a" * 40 + "/brain-v42/src/brain_v42/__init__.py"),
    ],
)
def test_release_sha_from_path_reads_the_release_component(path: Path) -> None:
    assert release_sha_from_path(path) == "a" * 40


@pytest.mark.parametrize(
    "path",
    [
        Path("/srv/checkout/src/brain_v42/__init__.py"),
        Path("/srv/releases/" + "a" * 39 + "/brain_v42/__init__.py"),
        Path("/srv/releases/" + "A" * 40 + "/brain_v42/__init__.py"),
        Path("/srv/releases"),
    ],
)
def test_release_sha_from_path_refuses_non_immutable_layouts(path: Path) -> None:
    with pytest.raises(ValueError):
        release_sha_from_path(path)


@pytest.mark.asyncio
async def test_release_source_session_measures_the_injected_release_identity() -> None:
    session = ReleaseSourceSession(
        Path("/srv/releases/" + "a" * 40 + "/brain_v42/__init__.py"), lambda: "0.6.0"
    )
    assert await session.identity() == ReleaseIdentity("a" * 40, "0.6.0")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,version",
    [
        (Path("/srv/releases/" + "a" * 40 + "/brain_v42/__init__.py"), lambda: "dev"),
        (Path("/srv/checkout/src/brain_v42/__init__.py"), lambda: "0.6.0"),
    ],
)
async def test_release_source_session_refuses_unreadable_identity(
    path: Path, version: object
) -> None:
    with pytest.raises(IdentityUnreadableError):
        await ReleaseSourceSession(path, version).identity()  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_default_release_source_factory_enters_and_only_reports_a_value_error() -> None:
    factory = ReleaseSourceFactory()
    async with factory() as session:
        try:
            identity = await session.identity()
        except ValueError:
            return
    assert isinstance(identity, ReleaseIdentity)


def test_read_text_under_reads_a_regular_file_and_its_whole_second_mtime(tmp_path: Path) -> None:
    path = tmp_path / "safe.conf"
    path.write_text("enabled=true\n", encoding="utf-8")
    text, mtime = read_text_under(tmp_path, "safe.conf")
    assert text == "enabled=true\n"
    assert mtime == int(os.stat(path).st_mtime)


@pytest.mark.parametrize("relative", ["", "/etc/passwd", "../x", "a/../../x"])
def test_read_text_under_refuses_unsafe_relative_names(tmp_path: Path, relative: str) -> None:
    with pytest.raises(ValueError):
        read_text_under(tmp_path, relative)


def test_read_text_under_refuses_all_symlink_components(tmp_path: Path) -> None:
    inside = tmp_path / "inside"
    inside.mkdir()
    (inside / "value.conf").write_text("inside", encoding="utf-8")
    outside = tmp_path.parent / "outside.conf"
    outside.write_text("outside", encoding="utf-8")
    os.symlink(inside, tmp_path / "inside-link")
    os.symlink(outside, tmp_path / "outside-link")
    with pytest.raises(ValueError):
        read_text_under(tmp_path, "inside-link/value.conf")
    with pytest.raises(ValueError):
        read_text_under(tmp_path, "outside-link")


def test_read_text_under_enforces_the_64_kib_regular_file_limit(tmp_path: Path) -> None:
    (tmp_path / "maximum.conf").write_text("x" * 65536, encoding="utf-8")
    (tmp_path / "too-large.conf").write_text("x" * 65537, encoding="utf-8")
    assert read_text_under(tmp_path, "maximum.conf")[0] == "x" * 65536
    with pytest.raises(ValueError):
        read_text_under(tmp_path, "too-large.conf")


def test_read_text_under_preserves_missing_file_oserror(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_text_under(tmp_path, "missing.conf")


def test_read_text_under_refuses_a_directory_with_a_policy_error(tmp_path: Path) -> None:
    """Only a regular file is a readable host source; a directory is refused, not opened."""
    (tmp_path / "a-directory").mkdir()
    with pytest.raises(ValueError, match="regular file"):
        read_text_under(tmp_path, "a-directory")


def test_read_text_under_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    """A FIFO with no writer blocks a plain open() forever, and a blocked probe
    cannot be cancelled by the registry's deadline: the reader must refuse it
    before reading (fd opened non-blocking, fstat before any read)."""
    import queue
    import threading

    fifo = tmp_path / "pipe.conf"
    os.mkfifo(fifo)
    outcome: queue.Queue[object] = queue.Queue()

    def _attempt() -> None:
        try:
            outcome.put(read_text_under(tmp_path, "pipe.conf"))
        except BaseException as exc:  # noqa: BLE001 - the thread reports, the test judges
            outcome.put(exc)

    # A daemon thread, so a reader that does block cannot hang the interpreter.
    threading.Thread(target=_attempt, daemon=True).start()
    try:
        result = outcome.get(timeout=2)
    except queue.Empty:
        # Unblock the stuck open() by lending it a writer, then fail loudly.
        writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(writer)
        pytest.fail("read_text_under blocked on a FIFO instead of refusing it")
    assert isinstance(result, ValueError)
    assert "regular file" in str(result)


@pytest.mark.asyncio
async def test_host_source_session_exposes_only_host_identity_and_restricted_reader(
    tmp_path: Path,
) -> None:
    session = HostSourceSession(tmp_path, lambda: "HawixsPC")
    assert await session.identity() == HostIdentity("hawixspc")
    with pytest.raises(ValueError):
        session.read_text("../outside")


@pytest.mark.asyncio
async def test_host_source_factory_binds_the_declared_root(tmp_path: Path) -> None:
    factory = HostSourceFactory(tmp_path, hostname=lambda: "host-a")
    async with factory() as session:
        assert session.root == tmp_path
        assert await session.identity() == HostIdentity("host-a")


@pytest.mark.asyncio
async def test_release_source_session_reports_every_malformed_identity_as_unreadable(
    tmp_path: Path,
) -> None:
    """Whatever makes the release identity unbuildable — a `dev` version, a
    70-character version, a non-printable one — is one condition for the
    registry: `identity_unreadable`, on the registry's own call and on the
    `live_release_sha` probe's call alike. A bare ValueError would split it in two."""
    package_file = tmp_path / "releases" / ("d" * 40) / "brain_v42" / "__init__.py"
    package_file.parent.mkdir(parents=True)
    package_file.touch()
    for version in ("dev", "v" * 70, "0.6.0\x00"):
        session = ReleaseSourceSession(package_file, lambda v=version: v)
        with pytest.raises(IdentityUnreadableError):
            await session.identity()
