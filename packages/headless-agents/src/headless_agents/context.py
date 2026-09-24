"""The context bundle: the instructions a sub-agent needs, delivered once.

Three leaks make a sub-agent work without its instructions: a worktree checks
out tracked files only (an ignored ``CLAUDE.md`` vanishes), each CLI reads a
different file (codex and opencode read ``AGENTS.md``, claude ``CLAUDE.md``),
and the ephemeral HOME drops the operator's user-level files. The bundle
resolves them from the SOURCE repository and the caller's list, and delivers
them through ONE channel, the preamble, in every mode and on every rail.

A second channel -- an instruction file written into a write-mode workspace
for the rail to read natively -- was measured live on 2026-09-23 and failed
on three rails out of four: claude's ``--restricted`` does not auto-load the
workspace ``CLAUDE.md``, opencode's ``OPENCODE_DISABLE_PROJECT_CONFIG`` (kept
for isolation) also disables ``AGENTS.md``, and agy reads ``AGENTS.md`` only
inside a repository checkout. It was removed: nothing is written into a
workspace.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Literal

ContextLevel = Literal["full", "global", "none"]
Scope = Literal["repository", "user", "role"]

REPOSITORY_FILE_NAMES: Final[tuple[str, ...]] = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")


@dataclass(frozen=True)
class ContextFile:
    #: The file it was read from; for a role's instructions, ``"role:<name>"``.
    source: Path | str
    scope: Scope
    content: str
    size_bytes: int
    sha256: str


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


def role_instructions(name: str, text: str) -> ContextFile:
    """A role's instructions as a bundle entry (spec 0.5.0 §3.1): scope ``role``.

    They belong to the role, not to the ambient context, so they are delivered
    at every level, ``none`` included, and count against the preamble's limits
    like any other byte of it.
    """
    raw = text.encode("utf-8")
    return ContextFile(
        source=f"role:{name}",
        scope="role",
        content=text,
        size_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def xml_attribute(value: str) -> str:
    """``value`` safe inside a double-quoted attribute: a path is the
    operator's, but nothing stops it holding a quote or a bracket. It lives
    here, the stdlib-only module, so every rail's blocks share one escaper."""
    return (
        value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _block(file: ContextFile) -> str:
    source = xml_attribute(str(file.source))
    return (
        f'<instructions source="{source}" scope="{file.scope}">\n'
        f"{file.content.rstrip()}\n</instructions>"
    )


@dataclass(frozen=True)
class ContextBundle:
    level: ContextLevel
    files: tuple[ContextFile, ...]

    def repository_files(self) -> tuple[ContextFile, ...]:
        return tuple(f for f in self.files if f.scope == "repository")

    def user_files(self) -> tuple[ContextFile, ...]:
        return tuple(f for f in self.files if f.scope == "user")

    def role_files(self) -> tuple[ContextFile, ...]:
        return tuple(f for f in self.files if f.scope == "role")

    def with_role(self, file: ContextFile | None) -> ContextBundle:
        """This bundle plus a role's instructions; itself, unchanged, without them.

        Unchanged means byte for byte: a run whose role carries no instructions
        sends exactly the 0.4.0 preamble (the Dream's golden fixtures).
        """
        if file is None:
            return self
        return replace(self, files=(*self.files, file))

    def preamble(self) -> str:
        """User-level content, then repository content, then the role's: the most
        specific reads last. Every file, in every mode -- the preamble is the
        bundle's only channel (see above)."""
        ordered = self.user_files() + self.repository_files() + self.role_files()
        return "\n\n".join(_block(f) for f in ordered)

    def to_list(self) -> list[dict[str, object]]:
        return [
            {
                "path": str(f.source),
                "scope": f.scope,
                "size_bytes": f.size_bytes,
                "sha256": f.sha256,
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
