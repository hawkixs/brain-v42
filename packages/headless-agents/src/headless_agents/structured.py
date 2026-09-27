"""Schema-constrained output: which rails take a schema, and what ``ha`` checks.

A caller asks for an answer constrained by a JSON Schema (``RunSpec.output_schema``,
``ha run --output-schema``, 0.5.3 lot 1, red-arena G1). The constraint is only as
real as the mechanism behind it, so a rail honours a schema through its own measured,
native mechanism -- claude's ``--json-schema``, codex's ``--output-schema`` -- or
refuses it before anything starts. It is never imitated by a prompt ("answer in JSON
matching ..."): that returns text that merely looks constrained.

``ha`` does not validate an answer against the schema: a JSON Schema validator lies
outside the package boundary (pydantic only, ``test_package_boundary.py``). The rail
enforces the schema; ``ha`` checks only that the answer is JSON
(:func:`is_json_answer`), and a caller that needs a validated shape validates the
object it parses.

Nothing here imports :mod:`headless_agents.spec`, which imports this module.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Final

#: The rails with a measured native schema mechanism: claude 2.1.283
#: (``--json-schema``) and codex-cli 0.156.0 (``exec --output-schema``).
SCHEMA_RAILS: Final = frozenset({"claude", "codex"})
#: The largest serialised schema, in UTF-8 bytes: well under the 131072-byte
#: per-argument kernel limit claude's inline ``--json-schema`` is bound by.
MAX_SCHEMA_BYTES: Final = 65_536


class SchemaError(ValueError):
    """An output schema that no rail can take, or a rail that cannot take one."""


def _mapping_as_dict(value: object) -> dict[object, object]:
    """``json.dumps``'s hook: any other mapping serialises like a dict."""
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"{type(value).__name__} is not JSON")


def schema_text(schema: Mapping[str, object]) -> str:
    """The schema as compact JSON, or :class:`SchemaError`.

    Object-rooted only: claude's structured output accepts nothing else (measured
    on 2.1.283: "the API only accepts an object-rooted tool input schema"), and an
    answer that is one JSON object is what every caller parses. Non-ASCII is kept
    as is, and the text is at most :data:`MAX_SCHEMA_BYTES` bytes.
    """
    if not isinstance(schema, Mapping) or schema.get("type") != "object":
        raise SchemaError('the output schema must be object-rooted ({"type": "object", ...})')
    try:
        text = json.dumps(
            schema,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=_mapping_as_dict,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise SchemaError(f"the output schema is not JSON: {exc}") from None
    size = len(text.encode("utf-8"))
    if size > MAX_SCHEMA_BYTES:
        raise SchemaError(
            f"the output schema is {size} bytes, more than the {MAX_SCHEMA_BYTES} a rail takes"
        )
    return text


def _refusal(rails: Sequence[str]) -> SchemaError:
    return SchemaError(
        f"{', '.join(rails)} cannot constrain an answer to an output schema "
        f"(only {' and '.join(sorted(SCHEMA_RAILS))} can): nothing ran"
    )


def refuse_unsupported(rail: str, schema: Mapping[str, object] | None) -> None:
    """:class:`SchemaError` when a schema is set and ``rail`` has no native mechanism.

    Each such rail calls it first in ``run()`` and ``build_command()``: a library
    caller running a provider directly is refused before any file or process exists.
    """
    if schema is not None and rail not in SCHEMA_RAILS:
        raise _refusal([rail])


def check_chain(rails: Sequence[str], schema: Mapping[str, object] | None) -> None:
    """:class:`SchemaError` naming every rail of a chain that cannot honour the schema.

    A chain is refused as a whole, before its first link: a fallback must never
    carry a constrained request onto a rail that would ignore the constraint.
    """
    if schema is None:
        return
    refused = [rail for rail in rails if rail not in SCHEMA_RAILS]
    if refused:
        raise _refusal(refused)


def _not_a_json_constant(name: str) -> object:
    raise ValueError(f"{name} is not JSON")


def is_json_answer(text: str | None) -> bool:
    """Whether ``text`` is one JSON document, surrounding whitespace allowed.

    Strict: ``NaN`` and ``Infinity``, which Python's ``json`` accepts by default,
    are not JSON.
    """
    if text is None:
        return False
    try:
        json.loads(text, parse_constant=_not_a_json_constant)
    except ValueError:
        return False
    return True


__all__ = [
    "MAX_SCHEMA_BYTES",
    "SCHEMA_RAILS",
    "SchemaError",
    "check_chain",
    "is_json_answer",
    "refuse_unsupported",
    "schema_text",
]
