"""Pure comparison of immutable claim expectations against one measurement."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from brain_v42.facts.model import Measured, Measurement, Unreadable

Verdict = Literal["holds", "falsified", "unreadable"]
_OPERATORS = frozenset({"eq", "ne", "lt", "lte", "gt", "gte", "in", "exists"})
_MISSING = object()


@dataclass(frozen=True, slots=True)
class Comparison:
    """A bounded verdict suitable for persistence without probe diagnostics."""

    verdict: Verdict
    reason: str | None


def _unreadable(reason: str) -> Comparison:
    return Comparison(verdict="unreadable", reason=reason)


def _decode_segment(segment: str) -> str | None:
    decoded: list[str] = []
    index = 0
    while index < len(segment):
        character = segment[index]
        if character != "~":
            decoded.append(character)
            index += 1
            continue
        if index + 1 == len(segment) or segment[index + 1] not in {"0", "1"}:
            return None
        decoded.append("~" if segment[index + 1] == "0" else "/")
        index += 2
    return "".join(decoded)


def _pointer_value(value: Mapping[str, object], path: object) -> object:
    if not isinstance(path, str) or not path.startswith("/"):
        return _MISSING
    current: object = value
    for encoded in path[1:].split("/"):
        segment = _decode_segment(encoded)
        if segment is None:
            return _MISSING
        if isinstance(current, Mapping):
            if segment not in current:
                return _MISSING
            current = current[segment]
        elif isinstance(current, list):
            if (
                not segment
                or len(segment) > 64
                or any(character < "0" or character > "9" for character in segment)
                or (len(segment) > 1 and segment.startswith("0"))
            ):
                return _MISSING
            index = int(segment)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _is_scalar(value: object) -> bool:
    return value is None or type(value) in {bool, int, str}


def _same_scalar_type(left: object, right: object) -> bool:
    return left is None or right is None or type(left) is type(right)


def _scalar_equal(left: object, right: object) -> bool:
    return _same_scalar_type(left, right) and left == right


def _valid_expected(expected: Mapping[str, object]) -> tuple[str, object, object] | None:
    if not isinstance(expected, Mapping):
        return None
    path = expected.get("path")
    op = expected.get("op")
    if not isinstance(path, str) or not isinstance(op, str) or op not in _OPERATORS:
        return None
    if op == "exists":
        return (path, op, _MISSING) if frozenset(expected) == {"path", "op"} else None
    if frozenset(expected) != {"path", "op", "value"}:
        return None
    operand = expected["value"]
    if op == "in":
        if not isinstance(operand, list) or not all(_is_scalar(item) for item in operand):
            return None
    elif not _is_scalar(operand):
        return None
    return path, op, operand


def compare(expected_resolved: Mapping[str, object], measurement: Measurement) -> Comparison:
    """Compare one stored resolved expectation without opening a source or mutating inputs."""
    if isinstance(measurement, Unreadable):
        return _unreadable(f"probe:{measurement.error_code}")
    if not isinstance(measurement, Measured):
        return _unreadable("type_mismatch")

    expected = _valid_expected(expected_resolved)
    if expected is None:
        return _unreadable("type_mismatch")
    path, op, operand = expected
    actual = _pointer_value(measurement.value, path)
    if op == "exists":
        return Comparison(
            "holds" if actual is not _MISSING and actual is not None else "falsified", None
        )
    if actual is _MISSING:
        return _unreadable("path_absent")
    if not _is_scalar(actual):
        return _unreadable("type_mismatch")

    if op == "in":
        assert isinstance(operand, list)
        # A mixed array is valid at write time: read it through its candidates of the
        # measured kind, and call it a type mismatch only when it has none at all.
        comparable = [item for item in operand if _same_scalar_type(actual, item)]
        if operand and not comparable:
            return _unreadable("type_mismatch")
        return Comparison(
            "holds" if any(_scalar_equal(actual, item) for item in comparable) else "falsified",
            None,
        )

    if op in {"lt", "lte", "gt", "gte"}:
        if type(actual) is not int or type(operand) is not int:
            return _unreadable("type_mismatch")
        holds = {
            "lt": actual < operand,
            "lte": actual <= operand,
            "gt": actual > operand,
            "gte": actual >= operand,
        }[op]
        return Comparison("holds" if holds else "falsified", None)

    if not _same_scalar_type(actual, operand):
        return _unreadable("type_mismatch")
    equal = _scalar_equal(actual, operand)
    return Comparison("holds" if (equal if op == "eq" else not equal) else "falsified", None)
