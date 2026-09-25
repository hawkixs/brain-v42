"""State documents of ``ha`` (spec 0.5.0 §3.8.1).

The state directory holds identity, lineages, review results, provenance and
quarantines: the one authority for each fact. Two write disciplines, one
read discipline:

- :func:`publish` writes a document whole to a temporary file in the same
  directory, ``fsync``s it, renames it over its target and ``fsync``s the
  directory: a reader sees the old or the new document, never a partial one.
- :func:`create_once` creates a "written once" document with ``O_EXCL``; a
  second write raises :class:`FileExistsError` and changes nothing.
- :func:`read` treats a document that is missing when it should exist, does
  not parse, is not a JSON object, or names another id than its own as
  :class:`Unknown` -- and unknown is compromised, never empty.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path


class Unknown(Exception):  # noqa: N818 - the spec's word for this state
    """A state document that cannot be trusted: missing, unparsable, or naming another id."""


def ensure_dir(path: Path) -> Path:
    """``path`` as a directory, created ``0700`` with its parents when absent."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _fsync_dir(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _encode(document: Mapping[str, object]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def publish(path: Path, document: Mapping[str, object]) -> None:
    """Replace ``path`` with ``document`` atomically (``0600``)."""
    ensure_dir(path.parent)
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_encode(document))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def create_once(path: Path, document: Mapping[str, object]) -> None:
    """Create ``path`` with ``document``; :class:`FileExistsError` if it exists."""
    ensure_dir(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_encode(document))
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def read(path: Path, *, expect_id: tuple[str, str] | None = None) -> dict[str, object]:
    """The document at ``path``; :class:`Unknown` on any doubt.

    ``expect_id=("run_id", "<id>")`` also requires the document to name that id.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise Unknown(f"{path}: missing") from None
    except OSError as exc:
        raise Unknown(f"{path}: unreadable ({type(exc).__name__})") from None
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Unknown(f"{path}: does not parse") from None
    if not isinstance(document, dict):
        raise Unknown(f"{path}: not a JSON object")
    if expect_id is not None:
        key, value = expect_id
        if document.get(key) != value:
            raise Unknown(f"{path}: names {key} {document.get(key)!r}, expected {value!r}")
    return document


def read_optional(
    path: Path, *, expect_id: tuple[str, str] | None = None
) -> dict[str, object] | None:
    """Like :func:`read`, but ``None`` when -- and only when -- the file is absent."""
    if not path.exists() and not path.is_symlink():
        return None
    return read(path, expect_id=expect_id)


__all__ = ["Unknown", "create_once", "ensure_dir", "publish", "read", "read_optional"]
