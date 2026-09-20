"""Canonical, bounded JSON text used as the immutable fact value."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping

MAX_CANONICAL_DEPTH = 8
MAX_CANONICAL_BYTES = 4096
MEASUREMENT_DIGEST_PREFIX = b"brain-v42-fact-measurement:v1\n"


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
