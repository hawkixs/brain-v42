"""``structured``: which rails take an output schema, and what ha checks of an answer.

0.5.3 lot 1 (red-arena G1): a rail constrains its answer through its own measured
mechanism or refuses the schema; ha serialises the schema and checks that the answer
is JSON, nothing more.
"""

from __future__ import annotations

import json
import re
from types import MappingProxyType

import pytest

from headless_agents import structured
from headless_agents.structured import (
    SchemaError,
    check_chain,
    is_json_answer,
    parse_json,
    refuse_unsupported,
    schema_text,
)

S = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}
ALL_RAILS = (
    "claude",
    "codex",
    "agy",
    "opencode",
    "openrouter",
    "mistral",
    "nvidia",
    "openai-compat",
)


def test_schema_text_is_compact_json_of_an_object_rooted_schema() -> None:
    assert schema_text({"type": "object", "properties": {}}) == '{"type":"object","properties":{}}'
    assert (
        schema_text({"type": "object", "description": "réponse"})
        == '{"type":"object","description":"réponse"}'
    )


def test_any_mapping_is_serialised_like_a_dict() -> None:
    schema = MappingProxyType({"type": "object", "properties": MappingProxyType({})})
    assert schema_text(schema) == '{"type":"object","properties":{}}'


@pytest.mark.parametrize("schema", [{"type": "array"}, {}, {"type": ["object"]}, []])
def test_a_schema_that_is_not_object_rooted_is_refused(schema: object) -> None:
    with pytest.raises(SchemaError, match="object-rooted"):
        schema_text(schema)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "schema",
    [{"type": "object", "x": float("nan")}, {"type": "object", "x": object()}],
    ids=["nan", "not-serialisable"],
)
def test_a_schema_that_is_not_json_is_refused(schema: dict[str, object]) -> None:
    with pytest.raises(SchemaError, match="not JSON"):
        schema_text(schema)


def test_an_oversized_schema_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(structured, "MAX_SCHEMA_BYTES", 64)
    schema = {"type": "object", "description": "é" * 40}
    size = len(json.dumps(schema, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    with pytest.raises(SchemaError, match=rf"{size} bytes.*64"):
        schema_text(schema)


def test_a_schema_error_is_a_value_error() -> None:
    assert issubclass(SchemaError, ValueError)


@pytest.mark.parametrize("rail", ALL_RAILS)
def test_only_claude_and_codex_take_a_schema(rail: str) -> None:
    refuse_unsupported(rail, None)
    if rail in ("claude", "codex"):
        refuse_unsupported(rail, S)
        return
    with pytest.raises(SchemaError, match=rf"^{re.escape(rail)} cannot constrain .*nothing ran"):
        refuse_unsupported(rail, S)


def test_a_chain_is_refused_as_a_whole() -> None:
    with pytest.raises(SchemaError, match="opencode") as refused:
        check_chain(["codex", "opencode"], S)
    assert "codex" not in str(refused.value).split(" cannot")[0]
    check_chain(["codex", "claude"], S)
    check_chain(["codex", "opencode"], None)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"ok":true}', True),
        ('  {"ok": true}\n', True),
        ("ok", False),
        ("", False),
        (None, False),
        ("NaN", False),
        ("[" * 100_000 + "]" * 100_000, False),
    ],
    ids=["compact", "spaced", "word", "empty", "none", "nan", "too-deep"],
)
def test_is_json_answer(text: str | None, expected: bool) -> None:
    assert is_json_answer(text) is expected


def test_parse_json_is_strict_and_never_crashes_on_depth() -> None:
    assert parse_json(' {"ok": [1, 2]} ') == {"ok": [1, 2]}
    for text in ("Infinity", "{", "[" * 100_000 + "]" * 100_000):
        with pytest.raises(ValueError):
            parse_json(text)


# ── codex's strict mode (0.5.3 lot 1 Task 3, measured by Task 0) ────────────


@pytest.mark.parametrize(
    ("schema", "where"),
    [
        (
            {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "additionalProperties": False,
            },
            "'required' misses 'ok'",
        ),
        (
            {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
            "'additionalProperties' must be false",
        ),
        (
            {
                "type": "object",
                "properties": {
                    "inner": {
                        "type": "object",
                        "properties": {"x": {"type": "string"}},
                        "additionalProperties": False,
                    }
                },
                "required": ["inner"],
                "additionalProperties": False,
            },
            "$.properties.inner: 'required' misses 'x'",
        ),
        (
            {
                "type": "object",
                "properties": {
                    "list": {"type": "array", "items": {"type": "object", "properties": {}}}
                },
                "required": ["list"],
                "additionalProperties": False,
            },
            "$.properties.list.items: 'additionalProperties' must be false",
        ),
    ],
    ids=["missing-required", "open-object", "nested", "array-items"],
)
def test_codex_refuses_a_schema_its_strict_mode_rejects(
    schema: dict[str, object], where: str
) -> None:
    """Measured (codex-cli 0.156.0): a property missing from ``required`` fails the
    run with an API 400 (``invalid_json_schema``) that reaches only the event
    stream. Refused before anything starts, with ha's own words, instead."""
    with pytest.raises(SchemaError, match=rf"^codex cannot constrain .*{re.escape(where)}"):
        refuse_unsupported("codex", schema)
    with pytest.raises(SchemaError, match=r"^codex cannot constrain"):
        check_chain(["claude", "codex"], schema)
    refuse_unsupported("claude", schema)
    check_chain(["claude"], schema)


def test_a_strict_nested_schema_is_taken_by_codex() -> None:
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
            "note": {"type": ["string", "null"]},
        },
        "required": ["items", "note"],
        "additionalProperties": False,
    }
    refuse_unsupported("codex", schema)
    check_chain(["codex", "claude"], schema)


def test_codex_judges_the_json_it_is_sent() -> None:
    """A tuple is sent as a JSON array and any mapping as an object: the strict check
    reads the schema as codex receives it, not as Python holds it."""
    sent_as_arrays = {
        "type": "object",
        "properties": MappingProxyType({"ok": {"type": "boolean"}}),
        "required": ("ok",),
        "additionalProperties": False,
    }
    refuse_unsupported("codex", sent_as_arrays)
    hidden_in_a_tuple = {
        "type": "object",
        "properties": {"v": {"anyOf": ({"type": "object", "properties": {}},)}},
        "required": ["v"],
        "additionalProperties": False,
    }
    with pytest.raises(
        SchemaError, match=re.escape("$.properties.v.anyOf[0]: 'additionalProperties' must be")
    ):
        refuse_unsupported("codex", hidden_in_a_tuple)


def _deep(depth: int, leaf: dict[str, object]) -> dict[str, object]:
    node: dict[str, object] = leaf
    for _ in range(depth):
        node = {"items": node}
    return {
        "type": "object",
        "properties": {"a": node},
        "required": ["a"],
        "additionalProperties": False,
    }


def test_the_strict_check_holds_past_python_s_recursion_limit() -> None:
    """``json`` serialises deeper than Python recurses (measured on 3.12.11: 9994
    levels, against a default limit of 1000): a schema ``schema_text`` sends is
    judged at any depth, never a ``RecursionError``."""
    depth = 2_000
    assert len(schema_text(_deep(depth, {"type": "string"}))) < structured.MAX_SCHEMA_BYTES
    refuse_unsupported("codex", _deep(depth, {"type": "string"}))
    check_chain(["codex"], _deep(depth, {"type": "string"}))
    with pytest.raises(
        SchemaError, match=r"^codex cannot constrain .*'additionalProperties' must be false"
    ):
        refuse_unsupported("codex", _deep(depth, {"type": "object", "properties": {}}))
