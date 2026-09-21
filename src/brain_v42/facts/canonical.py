"""Canonical, bounded JSON text used as the immutable fact value."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brain_v42.facts.model import FactTarget
    from brain_v42.facts.probe import FactDescriptor

MAX_CANONICAL_DEPTH = 8
MAX_CANONICAL_BYTES = 4096
MEASUREMENT_DIGEST_PREFIX = b"brain-v42-fact-measurement:v1\n"
FACT_DEFINITION_DIGEST_PREFIX = b"brain-v42-fact-definition:v1\n"
CLAIM_KEY_PREFIX = b"brain-v42-claim-key:v1\n"


class ValueTooLargeError(ValueError):
    """The value is canonical but over the byte bound: a fact is a number, not a corpus."""


def _validate_string(value: str) -> None:
    """Reject strings PostgreSQL or UTF-8 cannot carry after a probe has succeeded."""
    if "\x00" in value:
        raise ValueError("canonical measurement value cannot contain NUL characters")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError("canonical measurement value cannot contain Unicode surrogates")


def _copy_canonical_value(value: Mapping[str, object]) -> dict[str, object]:
    """Iteratively validate and copy containers so hostile nesting cannot recurse."""
    copied: dict[str, object] = {}
    stack: list[tuple[object, dict[str, object] | list[object], int]] = [(value, copied, 0)]

    while stack:
        current, destination, depth = stack.pop()
        if depth >= MAX_CANONICAL_DEPTH:
            raise ValueError(
                "canonical measurement value nests too deep "
                f"(more than {MAX_CANONICAL_DEPTH} levels)"
            )

        if isinstance(current, Mapping):
            items: Iterable[tuple[object, object]] = current.items()
        elif isinstance(current, list | tuple):
            items = enumerate(current)
        else:
            raise ValueError("canonical measurement value has an unsupported container")

        for key, nested_value in items:
            if isinstance(destination, dict):
                if not isinstance(key, str):
                    raise ValueError("canonical measurement value requires string keys")
                _validate_string(key)
                assign_key: str | int = key
            else:
                assign_key = len(destination)

            if nested_value is None or type(nested_value) in {bool, int}:
                normalized: object = nested_value
            elif isinstance(nested_value, float):
                raise ValueError("canonical measurement value cannot contain floats")
            elif isinstance(nested_value, str):
                _validate_string(nested_value)
                normalized = nested_value
            elif isinstance(nested_value, Mapping):
                normalized = {}
                stack.append((nested_value, normalized, depth + 1))
            elif isinstance(nested_value, list | tuple):
                normalized = []
                stack.append((nested_value, normalized, depth + 1))
            else:
                raise ValueError(
                    "canonical measurement value has unsupported value type "
                    f"{type(nested_value).__name__}"
                )

            if isinstance(destination, dict):
                destination[assign_key] = normalized  # type: ignore[index]
            else:
                destination.append(normalized)

    return copied


def canonical_json(value: Mapping[str, object]) -> str:
    """Return one bounded JSON spelling so a digest has exactly one source text."""
    if not isinstance(value, Mapping):
        raise ValueError("canonical measurement value must be an object")
    normalized = _copy_canonical_value(value)
    encoded = json.dumps(
        normalized,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_CANONICAL_BYTES:
        raise ValueTooLargeError(
            f"canonical measurement value exceeds {MAX_CANONICAL_BYTES} UTF-8 bytes"
        )
    return encoded.decode("utf-8")


def measurement_digest(value_json: str) -> str:
    """Hash canonical text with its recipe version to make recipe drift explicit."""
    return hashlib.sha256(MEASUREMENT_DIGEST_PREFIX + value_json.encode("utf-8")).hexdigest()


def definition_digest(descriptor: FactDescriptor) -> str:
    """Hash the fixed persisted fields so later descriptor additions do not rewrite history."""
    # FactTarget is a StrEnum today, but its value is made explicit so an enum
    # base-class change cannot silently alter the persisted definition recipe.
    target = descriptor.target if type(descriptor.target) is str else descriptor.target.value
    payload = {
        "fact_name": descriptor.name,
        "definition_version": descriptor.definition_version,
        "target": target,
        "ttl_seconds": descriptor.ttl_seconds,
        "timeout_seconds": descriptor.timeout_seconds,
        "policies": dict(descriptor.policies),
        "value_schema": dict(descriptor.value_schema),
    }
    value_json = canonical_json(payload)
    return hashlib.sha256(FACT_DEFINITION_DIGEST_PREFIX + value_json.encode("utf-8")).hexdigest()


def claim_key(
    *,
    statement: str,
    fact_name: str,
    expected: Mapping[str, object],
    target: FactTarget | str,
    definition_version: int,
) -> str:
    """Hash immutable claim content with the recipe's third, purpose-specific domain.

    This deliberately follows the same fixed-field and defensive-copy rules as
    ``definition_digest``; see that function for why those rules are load-bearing.
    """
    target_value = target if isinstance(target, str) else target.value
    payload = {
        "statement": statement,
        "fact_name": fact_name,
        "expected": dict(expected),
        "target": target_value,
        "definition_version": definition_version,
    }
    text = canonical_json(payload)
    return hashlib.sha256(CLAIM_KEY_PREFIX + text.encode("utf-8")).hexdigest()


def _canonical_depth(value: Mapping[str, object]) -> int:
    """Count JSON containers iteratively so a hostile expectation cannot recurse."""
    maximum = 1
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        maximum = max(maximum, depth)
        if isinstance(current, Mapping):
            stack.extend(
                (nested, depth + 1)
                for nested in current.values()
                if isinstance(nested, Mapping | list | tuple)
            )
        elif isinstance(current, list | tuple):
            stack.extend(
                (nested, depth + 1)
                for nested in current
                if isinstance(nested, Mapping | list | tuple)
            )
    return maximum


def assert_expected_within_bounds(expected: Mapping[str, object]) -> None:
    """Reject a declared expectation beyond the smaller claim-specific envelope."""
    depth = _canonical_depth(expected)
    if depth > 4:
        raise ValueError(f"expected depth rule exceeds 4 levels (measured {depth})")

    # canonical_json's 8-level / 4096-byte envelope protects all fact values.
    # Claims are deliberately tighter, so callers see the claim rule instead.
    try:
        text = canonical_json(expected)
    except ValueTooLargeError as exc:
        raise ValueError(
            "expected byte-size rule exceeds 1024 UTF-8 bytes (measured more than 4096 bytes)"
        ) from exc
    byte_count = len(text.encode("utf-8"))
    if byte_count > 1024:
        raise ValueError(
            f"expected byte-size rule exceeds 1024 UTF-8 bytes (measured {byte_count})"
        )
