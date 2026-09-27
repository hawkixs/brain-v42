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
