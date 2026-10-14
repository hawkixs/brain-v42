"""All HTTP evidence is replayed locally, including malformed archives."""

import io
import tarfile
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from brain_v42.lifecycle_cli import sources as module
from brain_v42.lifecycle_cli.sources import GitHubSources, _instant, unpack_archive


def archive(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        for name, body in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(body)
            tar.addfile(member, io.BytesIO(body))
    return stream.getvalue()


def test_lifecycle_archive_builds_tree_and_identifier_index(tmp_path: Path) -> None:
    result = unpack_archive(
        archive(
            {
                "brain/src/example.py": b"class Known: pass\n# CommentName\ntext = 'StringName'\n",
                "brain/.github/workflows/ci.yml": b"runs-on: ubuntu-24.04",
            }
        ),
        tmp_path,
    )
    assert result.tree == frozenset({"src/example.py", ".github/workflows/ci.yml"})
    assert "Known" in result.identifiers
    assert "CommentName" not in result.identifiers and "StringName" not in result.identifiers
    assert (tmp_path / "src/example.py").is_file()


@pytest.mark.parametrize(
    "path", ["brain/../../escape", "/escape", "other/file.py", "brain/dir\\file.py"]
)
def test_lifecycle_archive_rejects_traversal_and_multiple_roots(tmp_path: Path, path: str) -> None:
    with pytest.raises(ValueError):
        unpack_archive(archive({"brain/file.py": b"x = 1", path: b"bad"}), tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "kind",
    [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE],
    ids=["symlink", "hardlink", "character-device", "block-device", "fifo"],
)
def test_lifecycle_archive_rejects_links_and_special_files(tmp_path: Path, kind: bytes) -> None:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        member = tarfile.TarInfo("brain/link")
        member.type, member.linkname = kind, "../outside"
        tar.addfile(member)
    with pytest.raises(ValueError, match="^unsafe_archive_member$"):
        unpack_archive(stream.getvalue(), tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("parent_first", [True, False])
def test_lifecycle_archive_rejects_file_directory_collision(
    tmp_path: Path, parent_first: bool
) -> None:
    files = [("brain/parent", b"file"), ("brain/parent/child.py", b"pass")]
    if not parent_first:
        files.reverse()
    with pytest.raises(ValueError, match="^archive_file_directory_collision$"):
        unpack_archive(archive(dict(files)), tmp_path)
    assert not list(tmp_path.iterdir())


def test_lifecycle_archive_rejects_duplicate_members(tmp_path: Path) -> None:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        for body in (b"first", b"second"):
            member = tarfile.TarInfo("brain/duplicate")
            member.size = len(body)
            tar.addfile(member, io.BytesIO(body))
    with pytest.raises(ValueError, match="^duplicate_archive_member$"):
        unpack_archive(stream.getvalue(), tmp_path)
    assert not list(tmp_path.iterdir())


def test_lifecycle_archive_rejects_unreadable_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tarfile.TarFile, "extractfile", lambda self, member: None)
    with pytest.raises(ValueError, match="^unreadable_archive_member$"):
        unpack_archive(archive({"brain/file.py": b"pass"}), tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("value", ["2026-10-14", "2026-10-14T12:00:00"])
def test_lifecycle_source_timestamp_rejects_naive_values(value: str) -> None:
    with pytest.raises(ValueError, match="^naive_source_timestamp$"):
        _instant(value)


class Database:
    async def tickets(self) -> dict[str, tuple[str, None]]:
        return {"12345678": ("open", None)}

    async def facts(self) -> tuple[dict[str, str], dict[str, str]]:
        return {"alembic_head": "066", "live_release_sha": "b" * 40}, {}


async def test_lifecycle_archive_download_stops_at_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    consumed: list[bytes] = []

    class Download(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in (b"abcd", b"e", b"unread"):
                consumed.append(chunk)
                yield chunk

    unpack = Mock(side_effect=AssertionError("oversized download must not be unpacked"))
    monkeypatch.setattr(module, "MAX_ARCHIVE_BYTES", 4)
    monkeypatch.setattr(module, "unpack_archive", unpack)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Download()))
    ) as http:
        provider = GitHubSources(http, Database())
        provider.pin_origin("a" * 40)
        with pytest.raises(ValueError, match="^archive_too_large$"):
            await provider._archive()
    assert consumed == [b"abcd", b"e"]
    unpack.assert_not_called()


async def test_lifecycle_pr_pagination_limit() -> None:
    pages: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        assert page <= 10_000
        return httpx.Response(
            200, json=[], headers={"Link": '<https://untrusted.invalid/next>; rel="next"'}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(ValueError, match="^pr_pagination_limit$"):
            await GitHubSources(http, Database())._prs()
    assert pages == list(range(1, 10_001))


async def test_lifecycle_one_archive_exact_sha_and_paginated_prs() -> None:
    calls: list[str] = []
    sha = "a" * 40

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": sha})
        if request.url.host == "codeload.github.com":
            assert request.url.path.endswith(sha)
            return httpx.Response(
                200,
                content=archive(
                    {
                        "brain/src/sample.py": b"def found(): pass",
                        "brain/.github/workflows/ci.yml": b"runs-on: ubuntu-24.04",
                    }
                ),
            )
        assert request.url.params["state"] == "all"
        if request.url.params["page"] == "1":
            return httpx.Response(
                200,
                json=[{"number": 1, "state": "open", "merged_at": None, "closed_at": None}],
                headers={
                    "Link": '<https://api.github.com/repos/hawkixs/brain-v42/pulls?state=all&per_page=100&page=2>; rel="next"'
                },
            )
        return httpx.Response(
            200,
            json=[
                {
                    "number": 2,
                    "state": "closed",
                    "merged_at": "2026-10-13T10:00:00Z",
                    "closed_at": "2026-10-13T10:00:00Z",
                },
                {
                    "number": 3,
                    "state": "closed",
                    "merged_at": None,
                    "closed_at": "2026-10-12T10:00:00Z",
                },
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await GitHubSources(http, Database()).snapshot(date(2026, 10, 14))
    assert len(calls) == 4
    assert result.sources.origin_main_sha == sha
    assert "found" in result.sources.identifiers
    assert {k: v[0] for k, v in result.sources.prs.items()} == {1: "open", 2: "merged", 3: "closed"}
    assert result.sources.facts["ci.system"] == "github-actions"
    assert result.sources.facts["schema.alembic_head"] == "066"
    assert "fact:dream.nightly" in result.failed


async def test_lifecycle_source_failures_are_explicit_failed_checks() -> None:
    class FailedDatabase(Database):
        async def tickets(self) -> dict[str, tuple[str, None]]:
            raise RuntimeError("unavailable")

        async def facts(self) -> tuple[dict[str, str], dict[str, str]]:
            raise RuntimeError("unavailable")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    ) as http:
        result = await GitHubSources(http, FailedDatabase()).snapshot(date(2026, 10, 14))
    assert {
        "code_path",
        "code_symbol",
        "pr",
        "ticket",
        "fact:ci.system",
        "fact:schema.alembic_head",
    } <= result.failed
    assert not result.sources.tree and not result.sources.prs
    assert all(value is None for value in result.sources.facts.values())


async def test_lifecycle_failed_archive_preserves_resolved_sha() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": "a" * 40})
        if request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[])
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await GitHubSources(http, Database()).snapshot(date(2026, 10, 14))
    assert result.sources.origin_main_sha == "a" * 40
    assert {"code_path", "code_symbol"} <= result.failed


async def test_lifecycle_resume_reuses_durable_origin_sha() -> None:
    sha = "b" * 40
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert not request.url.path.endswith("/commits/main")
        if request.url.host == "codeload.github.com":
            assert request.url.path.endswith(sha)
            return httpx.Response(
                200, content=archive({"brain/example.py": b"class Original: pass"})
            )
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        provider = GitHubSources(http, Database())
        provider.pin_origin(sha)
        result = await provider.snapshot(date(2026, 10, 14))
    assert len(calls) == 2 and result.sources.origin_main_sha == sha
    assert "Original" in result.sources.identifiers
