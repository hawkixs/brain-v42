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
"..."}]``) and ``cost`` as an inline table (``cost = {kind = "..."}``).
:mod:`tomllib` yields the identical ``list[dict]`` / ``dict`` for every
non-inline spelling TOML also allows -- a ``[provider."model".cost]``
header, dotted keys (``cost.kind = "..."``), and, for ``tasks``, a
``[[provider."model".tasks]]`` array of tables -- so no amount of parsed-
value inspection tells inline from non-inline apart, and neither does a
scan for header lines: dotted keys open no header at all. What *does*
distinguish them is TOML's own immutability rule (TOML v1.0, "Inline
Table"/"Array"): once written, an inline table or a statically-declared
array is closed -- no later header may extend it -- while a table opened
by a ``[header]`` or by dotted keys, and an array of tables, both accept
a later element. :func:`_reject_non_inline_cost` and
:func:`_reject_non_inline_tasks` probe exactly that: they append one more
header addressing the same field and re-parse. ``TOMLDecodeError`` means
the field refused the extension -- it was inline, the frozen shape -- and
a successful parse means it accepted one, so it was not. For ``cost`` the
probe targets a sub-key that must itself be absent from the field's own
parsed content (:func:`_unused_probe_key`): a catalogue can declare
``_INLINE_PROBE_KEY`` itself, through an escaped TOML key, to collide with
a *fixed* probe key and make the appended header fail for the wrong reason
-- key-already-defined, not inline -- misreading a non-inline ``cost`` as
inline (ticket 115f68a3). ``tasks`` needs no equivalent care: its probe is
an anonymous ``[[...]]`` array-of-tables element with no key of its own,
and a real array of tables accepts one unconditionally, whatever it
already contains. The header segments themselves are also encoded through
:func:`_toml_basic_string`, never string-formatted directly: a provider or
model name is not filtered anywhere upstream and can itself carry a quote,
a backslash or a dot.
"""

from __future__ import annotations

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

#: A key no real catalogue declares (a control character no operator types),
#: used to probe a field's mutability without touching its real content.
_INLINE_PROBE_KEY = "\x00ha-inline-probe"


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


def _toml_basic_string(value: str) -> str:
    """``value`` as a TOML basic string literal, quotes included, escaping
    backslash, quote and control characters exactly as the TOML v1.0
    grammar requires. Deliberately not :func:`json.dumps`: JSON's escaping
    does not always coincide with TOML's -- U+007F (DEL) is left raw by
    JSON but is a control character TOML forbids unescaped, for one."""
    out = ['"']
    for char in value:
        codepoint = ord(char)
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "\b":
            out.append("\\b")
        elif char == "\t":
            out.append("\\t")
        elif char == "\n":
            out.append("\\n")
        elif char == "\f":
            out.append("\\f")
        elif char == "\r":
            out.append("\\r")
        elif codepoint < 0x20 or codepoint == 0x7F:
            out.append(f"\\u{codepoint:04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _unused_probe_key(cost: dict[str, object]) -> str:
    """A probe key absent from ``cost``'s own parsed keys (ticket 115f68a3):
    a catalogue that already declares ``_INLINE_PROBE_KEY`` under ``cost``,
    through an escaped TOML key, would make the fixed probe header collide
    with that existing key instead of opening a fresh one -- a
    ``TOMLDecodeError`` for "already defined", not for "inline" -- and a
    non-inline ``cost`` table would be misread as the frozen shape. Try
    successive suffixes until one is not already a key of this table."""
    key = _INLINE_PROBE_KEY
    suffix = 0
    while key in cost:
        suffix += 1
        key = f"{_INLINE_PROBE_KEY}-{suffix}"
    return key


def _accepts_extension(text: str, probe_header: str) -> bool:
    """Whether re-parsing ``text`` with ``probe_header`` appended still
    parses: an inline table or a statically-declared array is immutable
    (TOML v1.0) -- no later header may extend it, so the probe fails with
    ``TOMLDecodeError``; a table opened by a ``[header]``/dotted keys, and
    an array of tables, both accept one more element and parse clean."""
    try:
        tomllib.loads(f"{text}\n{probe_header}\n")
    except tomllib.TOMLDecodeError:
        return False
    return True


def _reject_inline_model(text: str, provider: str, model: str, table: dict[str, object]) -> None:
    """The model must be a ``[provider."model"]`` table, never an inline one
    (review of the 115f68a3 fix): inside an inline model, or an inline
    provider, the field probes below fail because an ANCESTOR is immutable,
    and a non-inline ``cost.kind = ...`` written there would be misread as
    the frozen inline shape. So the model table itself must accept an
    extension first, through a key absent from its own parsed content."""
    provider_key = _toml_basic_string(provider)
    model_key = _toml_basic_string(model)
    probe_key = _toml_basic_string(_unused_probe_key(table))
    if not _accepts_extension(text, f"[{provider_key}.{model_key}.{probe_key}]"):
        raise CatalogueError(
            f'{provider}.{model}: must be a [{provider}."{model}"] table, not an inline table'
        )


def _reject_non_inline_cost(text: str, provider: str, model: str, cost: dict[str, object]) -> None:
    provider_key = _toml_basic_string(provider)
    model_key = _toml_basic_string(model)
    probe_key = _toml_basic_string(_unused_probe_key(cost))
    header = f"[{provider_key}.{model_key}.cost.{probe_key}]"
    if _accepts_extension(text, header):
        raise CatalogueError(
            f"{provider}.{model}.cost: must be an inline table (cost = {{...}}), "
            "not a standalone [...] table or dotted keys"
        )


def _reject_non_inline_tasks(text: str, provider: str, model: str) -> None:
    provider_key = _toml_basic_string(provider)
    model_key = _toml_basic_string(model)
    header = f"[[{provider_key}.{model_key}.tasks]]"
    if _accepts_extension(text, header):
        raise CatalogueError(
            f"{provider}.{model}.tasks: must be an inline array of inline tables "
            "(tasks = [{...}]), not a [[...]] array of tables"
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


def _model_entry(
    provider: str, model: str, table: object, warnings: list[str], text: str
) -> ModelEntry:
    prefix = f"{provider}.{model}"
    if not model:
        raise CatalogueError(f"{prefix}: model name must not be empty")
    if not isinstance(table, dict):
        raise CatalogueError(f"{prefix}: must be a table")

    _reject_inline_model(text, provider, model, table)
    purpose = _string(_require(table, "purpose", prefix), f"{prefix}.purpose", nonempty=True)

    tasks_key = f"{prefix}.tasks"
    raw_tasks = _require(table, "tasks", prefix)
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise CatalogueError(f"{tasks_key}: must be a non-empty array of tables")
    _reject_non_inline_tasks(text, provider, model)
    seen_kinds: set[str] = set()
    tasks: list[tuple[str, str | None]] = []
    for index, raw_task in enumerate(raw_tasks):
        parsed, task_warnings = _task(raw_task, f"{tasks_key}[{index}]", provider, seen_kinds)
        tasks.append(parsed)
        warnings.extend(task_warnings)

    raw_cost = _require(table, "cost", prefix)
    if isinstance(raw_cost, dict):
        _reject_non_inline_cost(text, provider, model, raw_cost)
    cost_kind, windows, cost_note, cost_warnings = _cost(raw_cost, f"{prefix}.cost")
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
            entries.append(_model_entry(key, model, table, warnings, text))

    return Catalogue(entries=tuple(entries), warnings=tuple(warnings))


__all__ = ["CatalogueError", "Catalogue", "ModelEntry", "load_catalogue"]
