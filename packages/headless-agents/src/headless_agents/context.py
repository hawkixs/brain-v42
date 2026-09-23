"""The context bundle: the instructions a sub-agent needs, delivered once.

Three leaks make a sub-agent work without its instructions: a worktree checks
out tracked files only (an ignored ``CLAUDE.md`` vanishes), each CLI reads a
different file (codex and opencode read ``AGENTS.md``, claude ``CLAUDE.md``),
and the ephemeral HOME drops the operator's user-level files. The bundle
resolves them from the SOURCE repository and the caller's list, and delivers
them through two channels with one rule each:

- the PREAMBLE carries user-level content always, and repository content in
  read-only mode -- where the working directory is the caller's checkout and
  nothing may be written there;
- an INSTRUCTION FILE carries repository content in write mode, where the
  workspace is a fresh worktree the run owns, and only where the rail's own
  file is missing.

No exclude file is written: in a linked worktree ``info/exclude`` is the
COMMON one, shared by every checkout (measured 2026-09-23). The paths written
are returned instead, for the caller's carrier commit to leave out.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

ContextLevel = Literal["full", "global", "none"]
Scope = Literal["repository", "user"]

REPOSITORY_FILE_NAMES: Final[tuple[str, ...]] = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")

#: The instruction file each rail reads natively from its working directory.
#: ``None``: the rail reads ``CLAUDE.md``, nothing to install.
INSTRUCTION_FILE_BY_RAIL: Final[Mapping[str, str | None]] = {
    "claude": None,
    "codex": "AGENTS.md",
    "opencode": "AGENTS.md",
    "agy": "AGENTS.md",
}


@dataclass(frozen=True)
class ContextFile:
    source: Path
    scope: Scope
    content: str
    size_bytes: int
    sha256: str
    installed_as: Path | None = None


def _read(path: Path, scope: Scope) -> ContextFile | None:
    if not path.is_file():
        return None
    raw = path.read_bytes()
    return ContextFile(
        source=path,
        scope=scope,
        content=raw.decode("utf-8", errors="replace"),
        size_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _block(file: ContextFile) -> str:
    return f'<instructions source="{file.source}" scope="{file.scope}">\n{file.content.rstrip()}\n</instructions>'


@dataclass(frozen=True)
class ContextBundle:
    level: ContextLevel
    files: tuple[ContextFile, ...]

    def repository_files(self) -> tuple[ContextFile, ...]:
        return tuple(f for f in self.files if f.scope == "repository")

    def user_files(self) -> tuple[ContextFile, ...]:
        return tuple(f for f in self.files if f.scope == "user")

    def preamble(self, *, include_repository: bool) -> str:
        """User-level content always; repository content only when asked."""
        chosen = self.user_files() + (self.repository_files() if include_repository else ())
        return "\n\n".join(_block(f) for f in chosen)

    def to_list(self) -> list[dict[str, object]]:
        return [
            {
                "path": str(f.source),
                "scope": f.scope,
                "size_bytes": f.size_bytes,
                "sha256": f.sha256,
                "installed_as": None if f.installed_as is None else str(f.installed_as),
            }
            for f in self.files
        ]


def resolve_context(
    *,
    level: ContextLevel,
    repository_root: Path | None,
    user_files: Sequence[Path] = (),
    include_parents: bool = False,
) -> ContextBundle:
    """Read the bundle a level names. Absent files are skipped, never raised."""
    files: list[ContextFile] = []
    if level == "full" and repository_root is not None:
        # Farthest parent first, the repository last: the nearest file reads
        # last, as each CLI layers its own instruction files.
        directories = [repository_root]
        if include_parents:
            directories = [*reversed(repository_root.parents), repository_root]
        for directory in directories:
            for name in REPOSITORY_FILE_NAMES:
                entry = _read(directory / name, "repository")
                if entry is not None:
                    files.append(entry)
    if level in ("full", "global"):
        for path in user_files:
            entry = _read(path, "user")
            if entry is not None:
                files.append(entry)
    return ContextBundle(level=level, files=tuple(files))


def install_instruction_files(
    bundle: ContextBundle, *, worktree: Path, rail: str
) -> tuple[Path, ...]:
    """Write the rail's instruction file into ``worktree`` where it is missing.

    Composed from the repository's ``CLAUDE.md`` (the file every repository of
    this ecosystem carries). Never overwrites: a file already there -- tracked
    or not -- is the repository's own and wins.
    """
    if rail not in INSTRUCTION_FILE_BY_RAIL:
        raise ValueError(f"unknown rail: {rail!r}")
    name = INSTRUCTION_FILE_BY_RAIL[rail]
    if name is None:
        return ()
    target = worktree / name
    if target.exists() or target.is_symlink():
        return ()
    sources = [f for f in bundle.repository_files() if f.source.name == "CLAUDE.md"]
    if not sources:
        return ()
    target.write_text("\n\n".join(f.content.rstrip() for f in sources) + "\n", encoding="utf-8")
    return (target,)
