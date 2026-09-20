"""Build identity: which version is running, and which schema it can play.

Both facts are MEASURED at runtime, never written by hand.

- The version comes from the installed package's dist-info, not from
  `pyproject.toml`. Production runs an editable install: the dist-info is frozen
  at `uv sync` time. That is precisely what we want to announce — what is
  running, and not what the repository claims to describe. A gap between the two
  is a signal, not a bug to mask.
- The Alembic head is derived from the revision files SHIPPED with the package:
  the revision nobody names as a parent. A literal in this module would repeat a
  documented mistake of this project — the README asserted "production stays at
  037" for three days after the switch to 039.

Reading the files goes through `ast`, without executing the migrations:
importing them to learn their number would cost 44 imports and side effects, on
a path that serves a liveness probe.

Both reads are memoized. `/health` is called by a watchdog whose failure
RESTARTS the server: nothing that touches the disk must be paid for there on
every request.
"""

from __future__ import annotations

import ast
import importlib.metadata
from functools import cache
from pathlib import Path

#: The answer when no distribution is installed (a bare source tree).
DEV_VERSION = "dev"

_DISTRIBUTION_NAME = "brain_v42"
_REVISION_FIELD = "revision"
_PARENT_FIELD = "down_revision"
#: A migration is a schema file, not a corpus. A read guardrail.
_MAX_REVISION_BYTES = 1024 * 1024


@cache
def package_version() -> str:
    """Version of the installed distribution, or `dev` if nothing is installed."""
    try:
        return importlib.metadata.version(_DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        return DEV_VERSION


def _string_constants(node: ast.expr | None) -> set[str]:
    """The text literals of a value — covers `None`, a str, and a tuple."""
    if node is None:
        return set()
    return {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


def _declared_fields(source: str) -> tuple[str | None, set[str]]:
    """Extract `revision` and the parents declared at module level."""
    revision: str | None = None
    parents: set[str] = set()
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.Assign):
            names = [target.id for target in statement.targets if isinstance(target, ast.Name)]
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            names = [statement.target.id]
        else:
            continue
        value = statement.value
        if _REVISION_FIELD in names:
            declared = _string_constants(value)
            revision = next(iter(declared)) if len(declared) == 1 else None
        if _PARENT_FIELD in names:
            parents |= _string_constants(value)
    return revision, parents


def head_of_versions(directory: Path) -> str | None:
    """Head of the chain carried by `directory`, or None if it is ambiguous.

    The head is the revision no other declares as a parent. Zero candidates (an
    empty directory, a cyclic chain) or several (a forked chain) do not reduce to
    one: better to announce no number than to invent one.
    """
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in sorted(directory.glob("*.py")):
        try:
            if path.stat().st_size > _MAX_REVISION_BYTES:
                continue
            revision, declared_parents = _declared_fields(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError, ValueError):
            continue
        if revision is None:
            continue
        revisions.add(revision)
        parents |= declared_parents
    heads = revisions - parents
    return heads.pop() if len(heads) == 1 else None


def head_of_versions_strict(directory: Path) -> str:
    """Return the one shipped revision head or explain why the package is invalid.

    A probe that skipped an unparsable revision file could announce an older
    head as shipped — the lie called out at ``release.py:90``. Strict readers
    therefore reject every unreadable revision instead of silently omitting it.
    """
    if not directory.exists():
        raise ValueError(f"versions directory does not exist: {directory}")
    if not directory.is_dir():
        raise ValueError(f"versions path is not a directory: {directory}")

    revisions: set[str] = set()
    parents: set[str] = set()
    for path in sorted(directory.glob("*.py")):
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ValueError(f"cannot stat revision file {path.name}") from exc
        if size > _MAX_REVISION_BYTES:
            raise ValueError(f"revision file {path.name} exceeds {_MAX_REVISION_BYTES} bytes")
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot read revision file {path.name}") from exc
        except UnicodeError as exc:
            raise ValueError(f"revision file {path.name} is not UTF-8") from exc
        try:
            revision, declared_parents = _declared_fields(source)
        except (SyntaxError, ValueError) as exc:
            raise ValueError(f"cannot parse revision file {path.name}") from exc
        if revision is None:
            raise ValueError(f"revision file {path.name} declares no single revision")
        revisions.add(revision)
        parents |= declared_parents

    heads = revisions - parents
    if len(heads) == 1:
        return heads.pop()
    if not heads:
        raise ValueError("revision chain has zero heads")
    raise ValueError(f"revision chain has several heads: {sorted(heads)!r}")


def _versions_directory() -> Path | None:
    """Locate the revisions in the two possible layouts.

    Wheel: `force-include` copies them under the importable package. Source tree
    or editable install: they live at the repository root, two levels above
    `src/brain_v42/`.
    """
    package_root = Path(__file__).resolve().parent
    candidates = (
        package_root / "alembic" / "versions",
        package_root.parent.parent / "alembic" / "versions",
    )
    return next((candidate for candidate in candidates if candidate.is_dir()), None)


@cache
def shipped_alembic_head() -> str | None:
    """The Alembic head shipped with THIS package, or None if none is.

    None is not a cosmetic detail: it is a package unable to migrate its own
    database, and it must say so instead of staying silent.
    """
    directory = _versions_directory()
    return head_of_versions(directory) if directory is not None else None


@cache
def shipped_alembic_head_strict() -> str:
    """Return the one Alembic head shipped with this package, never a partial answer.

    The facts probe must fail closed: skipping one bad revision could make an
    older head look like the release's shipped schema capability.
    """
    directory = _versions_directory()
    if directory is None:
        raise ValueError("shipped Alembic versions directory is unavailable")
    return head_of_versions_strict(directory)
