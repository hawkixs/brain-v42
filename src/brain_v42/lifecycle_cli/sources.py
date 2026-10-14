"""Measure one public repository revision without executing downloaded code."""

from __future__ import annotations

import asyncio
import io
import re
import tarfile
import tokenize
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Protocol

import httpx
import structlog

from brain_v42.knowledge.checks import Sources
from brain_v42.knowledge.facts_registry import REGISTRY, FactInputs, measure_all
from brain_v42.services.knowledge_lifecycle_evidence import EvaluationSources

log = structlog.get_logger(__name__)
API = "https://api.github.com/repos/hawkixs/brain-v42"
TIMEOUT = httpx.Timeout(30, connect=10)
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_MEMBERS = 100_000
MAX_TEXT_BYTES = 4 * 1024 * 1024
Records = dict[str, tuple[str, date | datetime | None]]


async def _capture[T](name: str, operation: Awaitable[T]) -> T | None:
    try:
        return await operation
    except Exception as exc:
        log.warning("lifecycle.source_failed", source=name, error_type=type(exc).__name__)
        return None


class DatabaseSources(Protocol):
    async def tickets(self) -> Records: ...

    async def facts(self) -> tuple[dict[str, str], dict[str, str]]: ...


@dataclass(frozen=True)
class ArchiveSnapshot:
    tree: frozenset[str]
    identifiers: frozenset[str]
    files: dict[str, str]


def unpack_archive(body: bytes, destination: Path) -> ArchiveSnapshot:
    """Validate the entire archive before writing, and reject links and special files.

    A fresh private temporary directory is required by the caller. Paths retain
    their repository-relative spelling; only the archive's single root is removed.
    Python tokens identify names without accepting names found only in prose.
    """
    if len(body) > MAX_ARCHIVE_BYTES:
        raise ValueError("archive_too_large")
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        members: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
        roots: set[str] = set()
        paths: set[str] = set()
        total = 0
        member_count = 0
        for member in tar:
            member_count += 1
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or "\\" in member.name
                or not (member.isdir() or member.isfile())
            ):
                raise ValueError("unsafe_archive_member")
            roots.add(path.parts[0])
            if len(roots) != 1 or member_count > MAX_MEMBERS:
                raise ValueError("invalid_archive_root_or_size")
            relative = PurePosixPath(*path.parts[1:])
            if str(relative) == ".":
                if not member.isdir():
                    raise ValueError("invalid_archive_root")
                continue
            if str(relative) in paths:
                raise ValueError("duplicate_archive_member")
            paths.add(str(relative))
            total += member.size
            if member.size < 0 or total > MAX_EXPANDED_BYTES:
                raise ValueError("expanded_archive_too_large")
            members.append((member, relative))
        if not members:
            raise ValueError("empty_archive")
        # A file must never act as a parent directory, regardless of tar ordering.
        regular = {str(path) for member, path in members if member.isfile()}
        if any(str(parent) in regular for _, path in members for parent in path.parents):
            raise ValueError("archive_file_directory_collision")
        files: dict[str, str] = {}
        identifiers: set[str] = set()
        for member, relative in members:
            if member.isdir():
                continue
            stream = tar.extractfile(member)
            if stream is None:
                raise ValueError("unreadable_archive_member")
            with stream:
                raw = stream.read()
            target = destination.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("unsafe_archive_destination")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            if len(raw) > MAX_TEXT_BYTES or b"\x00" in raw:
                if relative.suffix in {".py", ".pyi"}:
                    raise ValueError("identifier_index_unreadable")
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeError:
                if relative.suffix in {".py", ".pyi"}:
                    raise ValueError("identifier_index_unreadable") from None
                continue
            files[str(relative)] = text
            if relative.suffix in {".py", ".pyi"}:
                try:
                    identifiers.update(
                        token.string
                        for token in tokenize.generate_tokens(io.StringIO(text).readline)
                        if token.type == tokenize.NAME
                    )
                except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
                    raise ValueError("identifier_index_unreadable") from exc
        return ArchiveSnapshot(frozenset(regular), frozenset(identifiers), files)


def _instant(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid_source_timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("naive_source_timestamp")
    return result.astimezone(UTC)


class GitHubSources:
    """Resolve main once per evaluation snapshot; source failures remain explicit."""

    def __init__(self, http: httpx.AsyncClient, database: DatabaseSources) -> None:
        self.http, self.database = http, database
        self.resolved_sha = ""
        self.pinned_sha: str | None = None

    def pin_origin(self, sha: str) -> None:
        """Resume an interrupted evaluation on the SHA already recorded by the pipeline."""
        if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
            raise ValueError("invalid_main_sha")
        self.pinned_sha = sha

    async def _archive(self) -> tuple[str, ArchiveSnapshot]:
        sha = self.pinned_sha
        if sha is None:
            response = await self.http.get(f"{API}/commits/main", timeout=TIMEOUT)
            response.raise_for_status()
            sha = response.json()["sha"]
        if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
            raise ValueError("invalid_main_sha")
        self.resolved_sha = sha
        self.pinned_sha = sha
        chunks: list[bytes] = []
        size = 0
        async with self.http.stream(
            "GET", f"https://codeload.github.com/hawkixs/brain-v42/tar.gz/{sha}", timeout=TIMEOUT
        ) as download:
            download.raise_for_status()
            async for chunk in download.aiter_bytes():
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    raise ValueError("archive_too_large")
                chunks.append(chunk)
        with TemporaryDirectory(prefix="knowledge-lifecycle-") as directory:
            snapshot = await asyncio.to_thread(unpack_archive, b"".join(chunks), Path(directory))
        return sha, snapshot

    async def _prs(self) -> dict[int, tuple[str, date | datetime | None]]:
        records: dict[int, tuple[str, date | datetime | None]] = {}
        page = 1
        while True:
            response = await self.http.get(
                f"{API}/pulls",
                params={"state": "all", "per_page": 100, "page": page},
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("invalid_pr_listing")
            for item in payload:
                number, state = item["number"], item["state"]
                if type(number) is not int or number < 1 or state not in {"open", "closed"}:
                    raise ValueError("invalid_pr_record")
                merged, closed = _instant(item["merged_at"]), _instant(item["closed_at"])
                current = "merged" if merged else state
                if current != "open" and (merged or closed) is None:
                    raise ValueError("missing_pr_end_timestamp")
                records[number] = (current, merged or closed if current != "open" else None)
            if "next" not in response.links:
                return records
            # Follow page numbers on our fixed origin, never a server-provided URL.
            page += 1
            if page > 10_000:
                raise ValueError("pr_pagination_limit")

    async def snapshot(self, day: date) -> EvaluationSources:
        self.resolved_sha = ""
        repository, pulls, ticket_rows, fact_rows = await asyncio.gather(
            _capture("archive", self._archive()),
            _capture("pr", self._prs()),
            _capture("ticket", self.database.tickets()),
            _capture("facts", self.database.facts()),
        )
        failed: set[str] = set()
        sha, archive = self.resolved_sha, ArchiveSnapshot(frozenset(), frozenset(), {})
        prs: dict[int, tuple[str, date | datetime | None]] = {}
        tickets: Records = {}
        brain_facts: dict[str, str] = {}
        decisions: dict[str, str] = {}
        if repository is None:
            failed.update({"code_path", "code_symbol"})
        else:
            sha, archive = repository
        if pulls is None:
            failed.add("pr")
        else:
            prs = pulls
        if ticket_rows is None:
            failed.add("ticket")
        else:
            tickets = ticket_rows
        if fact_rows is not None:
            brain_facts, decisions = fact_rows
        values = measure_all(FactInputs(archive.files, archive.tree, brain_facts, decisions))
        for key, value in values.items():
            if value is None:
                failed.add(f"fact:{key}")
        return EvaluationSources(
            Sources(archive.tree, archive.identifiers, prs, tickets, values, REGISTRY, sha),
            frozenset(failed),
        )
