"""Tests for the versioned, bounded canonical measurement recipe."""

from __future__ import annotations

import hashlib

import pytest

from brain_v42.facts.canonical import canonical_json, measurement_digest


def test_canonical_json_sorts_keys_and_uses_compact_separators() -> None:
    """A stable text prevents equivalent objects from receiving distinct digests."""
    assert canonical_json({"z": 1, "a": [True, None]}) == '{"a":[true,null],"z":1}'


def test_canonical_json_refuses_a_float() -> None:
    """A float would admit platform-dependent numeric representations."""
    with pytest.raises(ValueError, match="float"):
        canonical_json({"ratio": 1.5})


def test_canonical_json_refuses_nan_as_a_float() -> None:
    """NaN remains outside the recipe even when JSON libraries offer to encode it."""
    with pytest.raises(ValueError, match="float"):
        canonical_json({"ratio": float("nan")})


def test_canonical_json_keeps_booleans_distinct_from_integers() -> None:
    """Boolean status values must not collapse into their integer counterparts."""
    assert canonical_json({"false": False, "true": True}) == '{"false":false,"true":true}'


@pytest.mark.parametrize("value", [{"text": "before\x00after"}, {"\x00key": "value"}])
def test_canonical_json_refuses_nul_characters(value: dict[str, str]) -> None:
    """NUL would fail later in PostgreSQL instead of at the recipe boundary."""
    with pytest.raises(ValueError, match="NUL"):
        canonical_json(value)


@pytest.mark.parametrize("value", [{"text": "\ud800"}, {"\ud800": "value"}])
def test_canonical_json_refuses_lone_surrogates(value: dict[str, str]) -> None:
    """Lone surrogates are not valid UTF-8 measurement data."""
    with pytest.raises(ValueError, match="surrogate"):
        canonical_json(value)


def test_canonical_json_refuses_non_string_keys() -> None:
    """JSON object keys need the same unambiguous string domain at every depth."""
    with pytest.raises(ValueError, match="string keys"):
        canonical_json({1: "value"})  # type: ignore[dict-item]


def _nested_mapping(containers: int) -> dict[str, object]:
    value: dict[str, object] = {"leaf": 1}
    for _ in range(containers - 1):
        value = {"child": value}
    return value


def test_canonical_json_accepts_depth_eight_and_refuses_depth_nine() -> None:
    """The small depth limit makes malformed external payloads safely bounded."""
    assert canonical_json(_nested_mapping(8))
    with pytest.raises(ValueError, match="deep"):
        canonical_json(_nested_mapping(9))


def test_canonical_json_enforces_encoded_byte_limit() -> None:
    """The storage bound applies to UTF-8 bytes, not Python character count."""
    assert len(canonical_json({"value": "x" * 4084}).encode("utf-8")) == 4096
    with pytest.raises(ValueError, match="4096"):
        canonical_json({"value": "x" * 4085})


def test_canonical_json_preserves_non_ascii_characters() -> None:
    """UTF-8 text stays readable rather than being rewritten as ASCII escapes."""
    assert canonical_json({"word": "é"}) == '{"word":"é"}'


def test_measurement_digest_pins_its_domain_prefix() -> None:
    """The recipe version belongs to the hashed bytes, not only documentation."""
    expected = hashlib.sha256(b"brain-v42-fact-measurement:v1\n{}").hexdigest()
    assert measurement_digest("{}") == expected


def test_canonical_json_rejects_extreme_depth_without_recursion_error() -> None:
    """Untrusted nesting must fail predictably before JSON encoding can recurse."""
    nested: list[object] = []
    for _ in range(10_000):
        nested = [nested]

    with pytest.raises(ValueError, match="deep"):
        canonical_json({"nested": nested})
