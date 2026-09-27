"""The operator's model catalogue, schema v1 (headless-agents 0.5.2, lot 6).

The catalogue is the operator's file, never ha's: ``~/.config/ha/catalog.toml``
(or ``$XDG_CONFIG_HOME/ha/catalog.toml`` when that variable is absolute),
resolved by the caller through :func:`headless_agents.config_paths.config_file`.
This module only validates a document already read from disk; it never
discovers a config directory and never writes the file back.

The schema is frozen (red-skills, 2026-09-27, revision at red-skills head
``1b6a47c``): a top-level ``schema = 1`` (the integer, not a string or a
boolean), one provider table per rail among :data:`PROVIDER_NAMES`, one model
table per entry (``[provider."model"]``). A non-table top-level value is a
warning naming that key -- whether or not the name coincides with a provider
-- and is ignored; a table value whose name is not a known provider is an
error. Only an unknown field inside a model, task or cost table warns; every
other departure from the schema (a missing required field, a wrong type, a
value outside a closed list, an effort outside its provider's rule, a
duplicate task kind, an unknown provider, a model value that is not a table,
or ``schema != 1``) raises :class:`CatalogueError` naming the offending key.

``tasks`` is frozen as an inline array of inline tables (``tasks = [{kind =
"..."}]``) and ``cost`` as an inline table (``cost = {kind = "..."}``);
:mod:`tomllib` yields the same ``list[dict]`` / ``dict`` for the non-inline
spellings TOML also allows (``[[provider."model".tasks]]``,
``[provider."model".cost]``), so the syntax is checked against the raw text
before the parsed document is walked: those spellings raise
:class:`CatalogueError` naming ``tasks``/``cost`` even though the parsed
values would otherwise validate.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .registry import PROVIDER_NAMES

#: Closed list of task kinds the schema recognises.
TASK_KINDS = frozenset(
    {
        "design-review",
        "closure-check",
        "code-review",
        "build",
        "build-deep",
        "bulk-read",
        "draft",
    }
)
#: The eight effort values codex and opencode may name; forbidden elsewhere.
EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
#: Providers whose ``tasks[].effort`` is optional (codex) or accepted as-is
#: for opencode's own ``--variant``; every other provider forbids the field.
EFFORT_OPTIONAL_PROVIDERS = frozenset({"codex", "opencode"})
COST_KINDS = frozenset({"subscription", "per_token", "window"})
WINDOWS = frozenset({"5h", "daily", "weekly", "monthly"})

_MODEL_FIELDS = frozenset({"purpose", "tasks", "cost", "pitfalls", "verified_at", "source"})
_TASK_FIELDS = frozenset({"kind", "effort"})
_COST_FIELDS = frozenset({"kind", "windows", "note"})

#: A TOML array-of-tables header, ``[[ path ]]``, always alone on its line.
_ARRAY_TABLE_HEADER = re.compile(r"^\[\[(?P<path>[^\[\]]+)\]\]\s*(#.*)?$")
#: A TOML standalone table header, ``[ path ]`` -- excludes the ``[[`` above.
_TABLE_HEADER = re.compile(r"^\[(?!\[)(?P<path>[^\[\]]+)\]\s*(#.*)?$")


class CatalogueError(ValueError):
    """The catalogue file cannot be read or validated; the message names the key."""


@dataclass(frozen=True)
class ModelEntry:
    provider: str
    model: str
    purpose: str
    tasks: tuple[tuple[str, str | None], ...]
    cost_kind: str
    windows: tuple[str, ...]
    cost_note: str | None
    pitfalls: tuple[str, ...]
    verified_at: date
    source: str


@dataclass(frozen=True)
class Catalogue:
    entries: tuple[ModelEntry, ...]
    warnings: tuple[str, ...]


def _require(table: dict[str, object], key: str, prefix: str) -> object:
    if key not in table:
        raise CatalogueError(f"{prefix}.{key}: required")
    return table[key]


def _string(value: object, key: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise CatalogueError(f"{key}: must be a {'non-empty ' if nonempty else ''}string")
    return value


def _choice(value: object, key: str, allowed: frozenset[str]) -> str:
    text = _string(value, key)
    if text not in allowed:
        raise CatalogueError(f"{key}: expected one of {', '.join(sorted(allowed))}")
    return text


def _unknown(table: dict[str, object], known: frozenset[str], prefix: str) -> list[str]:
    return [f"{prefix}.{key}" if prefix else key for key in sorted(table.keys() - known)]


def _load_document(path: Path) -> tuple[dict[str, object], str]:
    """The parsed document, plus the raw text -- needed to tell an inline
    array/table from the non-inline spelling tomllib parses identically."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise CatalogueError(f"{path}: missing") from None
    except OSError as exc:
        raise CatalogueError(f"{path}: {exc}") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CatalogueError(f"{path}: {exc}") from None
    try:
        return tomllib.loads(text), text
    except tomllib.TOMLDecodeError as exc:
        raise CatalogueError(f"{path}: {exc}") from None
    except RecursionError:
        raise CatalogueError(f"{path}: nested too deeply to be a catalogue") from None


def _split_key_path(path: str) -> list[str]:
    """Split a TOML dotted key path into raw segments, respecting quotes: a
    dot inside a quoted segment (``a."b.c"``) is not a separator."""
    segments: list[str] = []
    current = ""
    quote: str | None = None
    for char in path:
        if quote is not None:
            current += char
            if char == quote:
                quote = None
            continue
        if char in ('"', "'"):
            quote = char
            current += char
            continue
        if char == ".":
            segments.append(current.strip())
            current = ""
            continue
        current += char
    segments.append(current.strip())
    return segments


def _unquote_key(segment: str) -> str:
    if len(segment) >= 2 and segment[0] == segment[-1] and segment[0] in ('"', "'"):
        inner = segment[1:-1]
        return inner.replace('\\"', '"') if segment[0] == '"' else inner
    return segment


def _normalized_key_path(path: str) -> str:
    return ".".join(_unquote_key(segment) for segment in _split_key_path(path))


def _reject_non_inline_tasks_and_cost(text: str) -> None:
    """``tasks`` is frozen as an inline array of inline tables and ``cost`` as
    an inline table; tomllib yields the identical ``list[dict]`` / ``dict``
    for the non-inline spellings TOML also allows (``[[provider."model".
    tasks]]``, ``[provider."model".cost]``), so the raw source is checked
    line by line, before the parsed document is walked, for a header whose
    last key is ``tasks`` or ``cost``."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        array_header = _ARRAY_TABLE_HEADER.match(stripped)
        if array_header is not None:
            normalized = _normalized_key_path(array_header.group("path"))
            if normalized == "tasks" or normalized.endswith(".tasks"):
                raise CatalogueError(
                    f"{normalized}: must be an inline array of inline tables "
                    "(tasks = [{...}]), not a [[...]] array of tables"
                )
            continue
        table_header = _TABLE_HEADER.match(stripped)
        if table_header is not None:
            normalized = _normalized_key_path(table_header.group("path"))
            if normalized == "cost" or normalized.endswith(".cost"):
                raise CatalogueError(
                    f"{normalized}: must be an inline table (cost = {{...}}), "
                    "not a standalone [...] table"
                )


def _schema(document: dict[str, object]) -> None:
    value = document.get("schema")
    if type(value) is not int or value != 1:
        raise CatalogueError("schema: must be the integer 1")


def _task(
    task: object, prefix: str, provider: str, seen_kinds: set[str]
) -> tuple[tuple[str, str | None], list[str]]:
    if not isinstance(task, dict):
        raise CatalogueError(f"{prefix}: must be a table")
    kind_key = f"{prefix}.kind"
    kind = _choice(_require(task, "kind", prefix), kind_key, TASK_KINDS)
    if kind in seen_kinds:
        raise CatalogueError(f"{kind_key}: {kind!r} appears more than once in tasks")
    seen_kinds.add(kind)

    effort_key = f"{prefix}.effort"
    effort: str | None = None
    if "effort" in task:
        if provider not in EFFORT_OPTIONAL_PROVIDERS:
            raise CatalogueError(f"{effort_key}: effort is forbidden for provider {provider!r}")
        effort = _choice(task["effort"], effort_key, EFFORTS)

    warnings = [f"{prefix}.{key}" for key in sorted(task.keys() - _TASK_FIELDS)]
    return (kind, effort), warnings


def _cost(value: object, prefix: str) -> tuple[str, tuple[str, ...], str | None, list[str]]:
    if not isinstance(value, dict):
        raise CatalogueError(f"{prefix}: must be a table")
    kind_key = f"{prefix}.kind"
    kind = _choice(_require(value, "kind", prefix), kind_key, COST_KINDS)

    windows_key = f"{prefix}.windows"
    raw_windows = value.get("windows")
    windows: tuple[str, ...] = ()
    if raw_windows is not None:
        if not isinstance(raw_windows, list):
            raise CatalogueError(f"{windows_key}: must be an array")
        windows = tuple(
            _choice(item, f"{windows_key}[{index}]", WINDOWS)
            for index, item in enumerate(raw_windows)
        )

    note: str | None = None
    if "note" in value:
        note = _string(value["note"], f"{prefix}.note")

    warnings = [f"{prefix}.{key}" for key in sorted(value.keys() - _COST_FIELDS)]
    return kind, windows, note, warnings


def _pitfalls(value: object, prefix: str) -> tuple[str, ...]:
    raw = value
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CatalogueError(f"{prefix}: must be an array of strings")
    return tuple(_string(item, f"{prefix}[{index}]") for index, item in enumerate(raw))


def _model_entry(provider: str, model: str, table: object, warnings: list[str]) -> ModelEntry:
    prefix = f"{provider}.{model}"
    if not model:
        raise CatalogueError(f"{prefix}: model name must not be empty")
    if not isinstance(table, dict):
        raise CatalogueError(f"{prefix}: must be a table")

    purpose = _string(_require(table, "purpose", prefix), f"{prefix}.purpose", nonempty=True)

    tasks_key = f"{prefix}.tasks"
    raw_tasks = _require(table, "tasks", prefix)
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise CatalogueError(f"{tasks_key}: must be a non-empty array of tables")
    seen_kinds: set[str] = set()
    tasks: list[tuple[str, str | None]] = []
    for index, raw_task in enumerate(raw_tasks):
        parsed, task_warnings = _task(raw_task, f"{tasks_key}[{index}]", provider, seen_kinds)
        tasks.append(parsed)
        warnings.extend(task_warnings)

    cost_kind, windows, cost_note, cost_warnings = _cost(
        _require(table, "cost", prefix), f"{prefix}.cost"
    )
    warnings.extend(cost_warnings)

    pitfalls = _pitfalls(table.get("pitfalls"), f"{prefix}.pitfalls")

    verified_at = _require(table, "verified_at", prefix)
    if type(verified_at) is not date:
        raise CatalogueError(f"{prefix}.verified_at: must be a TOML local date")

    source = _string(_require(table, "source", prefix), f"{prefix}.source")

    warnings.extend(f"{prefix}.{key}" for key in sorted(table.keys() - _MODEL_FIELDS))

    return ModelEntry(
        provider=provider,
        model=model,
        purpose=purpose,
        tasks=tuple(tasks),
        cost_kind=cost_kind,
        windows=windows,
        cost_note=cost_note,
        pitfalls=pitfalls,
        verified_at=verified_at,
        source=source,
    )


def load_catalogue(path: Path) -> Catalogue:
    """Read and validate ``path`` against the frozen schema v1; never rewrites it."""
    document, text = _load_document(path)
    _schema(document)
    _reject_non_inline_tasks_and_cost(text)

    warnings: list[str] = []
    entries: list[ModelEntry] = []
    for key, value in document.items():
        if key == "schema":
            continue
        if not isinstance(value, dict):
            warnings.append(key)
            continue
        if key not in PROVIDER_NAMES:
            raise CatalogueError(
                f"{key}: unknown provider; valid names: {', '.join(PROVIDER_NAMES)}"
            )
        for model, table in value.items():
            entries.append(_model_entry(key, model, table, warnings))

    return Catalogue(entries=tuple(entries), warnings=tuple(warnings))


__all__ = ["CatalogueError", "Catalogue", "ModelEntry", "load_catalogue"]
