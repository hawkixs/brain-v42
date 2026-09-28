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
#: The rails whose mechanism takes a "strict" schema only. Measured on codex-cli
#: 0.156.0: a property missing from ``required`` failed the run with an API 400
#: (``invalid_json_schema``) that reached neither stderr nor the last message,
#: only the ``--json`` event stream. Refused up front instead, in ha's words.
STRICT_RAILS: Final = frozenset({"codex"})


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


def _strict_problem(schema: object) -> str | None:
    """Where ``schema`` breaks the strict-mode rules codex's API enforces, or ``None``.

    Every object lists all its properties in ``required`` and sets
    ``additionalProperties`` to ``false``, at any depth (properties, array items,
    combinators, definitions). ``required`` is checked first: it is the rule
    codex's API named when it refused Task 0's schema. Only these two rules are
    checked here; any other rejection stays the rail's, and reaches the run's
    stderr.

    ``schema`` is the parsed JSON the rail is sent. The walk keeps its own stack,
    depth first: ``json`` serialises deeper than Python recurses.
    """
    pending: list[tuple[str, object]] = [("$", schema)]
    while pending:
        where, node = pending.pop()
        if not isinstance(node, dict):
            continue
        children: list[tuple[str, object]] = []
        kind = node.get("type")
        if (
            kind == "object"
            or (isinstance(kind, list) and "object" in kind)
            or "properties" in node
        ):
            properties = node.get("properties")
            named = properties if isinstance(properties, dict) else {}
            required = node.get("required")
            missing = [key for key in named if not (isinstance(required, list) and key in required)]
            if missing:
                return f"{where}: 'required' misses {', '.join(repr(key) for key in missing)}"
            if node.get("additionalProperties") is not False:
                return f"{where}: 'additionalProperties' must be false"
            children.extend((f"{where}.properties.{key}", sub) for key, sub in named.items())
        items = node.get("items")
        if isinstance(items, dict):
            children.append((f"{where}.items", items))
        elif isinstance(items, list):
            children.extend((f"{where}.items[{i}]", member) for i, member in enumerate(items))
        for keyword in ("anyOf", "allOf", "oneOf", "prefixItems"):
            members = node.get(keyword)
            if isinstance(members, list):
                children.extend((f"{where}.{keyword}[{i}]", sub) for i, sub in enumerate(members))
        for keyword in ("$defs", "definitions"):
            members = node.get(keyword)
            if isinstance(members, dict):
                children.extend((f"{where}.{keyword}.{name}", sub) for name, sub in members.items())
        pending.extend(reversed(children))
    return None


def _strict_refusal(rail: str, problem: str) -> SchemaError:
    return SchemaError(
        f"{rail} cannot constrain an answer to this output schema, which its strict mode "
        f"rejects ({problem}): every object must list all its properties in 'required' and "
        "set 'additionalProperties' to false; nothing ran"
    )


def _refusal(rails: Sequence[str]) -> SchemaError:
    return SchemaError(
        f"{', '.join(rails)} cannot constrain an answer to an output schema "
        f"(only {' and '.join(sorted(SCHEMA_RAILS))} can): nothing ran"
    )


def refuse_unsupported(rail: str, schema: Mapping[str, object] | None) -> None:
    """:class:`SchemaError` when a schema is set and ``rail`` has no native mechanism.

    Each such rail calls it first in ``run()`` and ``build_command()``: a library
    caller running a provider directly is refused before any file or process exists.
    A rail of :data:`STRICT_RAILS` also refuses a schema its strict mode rejects,
    judged on the JSON it would be sent (:func:`schema_text`, whose own refusal
    comes first): a tuple is an array there, any mapping an object.
    """
    if schema is None:
        return
    if rail not in SCHEMA_RAILS:
        raise _refusal([rail])
    if rail in STRICT_RAILS:
        # The text schema_text just wrote parses back: json reads as deep as it writes.
        problem = _strict_problem(json.loads(schema_text(schema)))
        if problem is not None:
            raise _strict_refusal(rail, problem)


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
    for rail in dict.fromkeys(rails):
        refuse_unsupported(rail, schema)


def _not_a_json_constant(name: str) -> object:
    raise ValueError(f"{name} is not JSON")


def parse_json(text: str) -> object:
    """``text`` as one JSON document, surrounding whitespace allowed; :class:`ValueError`.

    Strict: ``NaN`` and ``Infinity``, which Python's ``json`` accepts by default, are
    not JSON. A document nested too deeply for Python's parser is refused the same
    way -- as a ``ValueError``, never the ``RecursionError`` it would raise.
    """
    try:
        return json.loads(text, parse_constant=_not_a_json_constant)
    except RecursionError:
        raise ValueError("JSON nested too deeply to parse") from None


def is_json_answer(text: str | None) -> bool:
    """Whether ``text`` is one JSON document (:func:`parse_json`)."""
    if text is None:
        return False
    try:
        parse_json(text)
    except ValueError:
        return False
    return True


__all__ = [
    "MAX_SCHEMA_BYTES",
    "SCHEMA_RAILS",
    "STRICT_RAILS",
    "SchemaError",
    "check_chain",
    "is_json_answer",
    "parse_json",
    "refuse_unsupported",
    "schema_text",
]
