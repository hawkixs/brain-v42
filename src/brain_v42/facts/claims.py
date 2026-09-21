"""Resolve bounded claim input against the immutable descriptor used for its occurrence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast
from uuid import UUID

from brain_v42.facts.canonical import assert_expected_within_bounds, claim_key
from brain_v42.facts.model import FactTarget
from brain_v42.facts.probe import FactDescriptor, ValueType
from brain_v42.models.claim_input import ClaimInput

_MIN_VALIDITY_SECONDS = 60
_MAX_VALIDITY_SECONDS = 31_536_000
_ORDERING_OPERATORS = frozenset({"lt", "lte", "gt", "gte"})


@dataclass(frozen=True, slots=True)
class ResolvedClaim:
    """One immutable claim occurrence ready for persistence without a live catalogue lookup."""

    claim_key: str
    statement: str
    fact_name: str
    definition_version: int
    target: FactTarget
    expected: Mapping[str, object]
    expected_resolved: Mapping[str, object]
    validity_seconds: int
    replaces: UUID | None


def _immutable_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    """Copy the column value so the two persisted JSON objects cannot share a mapping."""
    return MappingProxyType(dict(value))


def _decode_pointer_segment(segment: str) -> str:
    """Decode the already structurally valid RFC 6901 segment before schema lookup."""
    decoded: list[str] = []
    index = 0
    while index < len(segment):
        character = segment[index]
        if character != "~":
            decoded.append(character)
            index += 1
            continue
        decoded.append("~" if segment[index + 1] == "0" else "/")
        index += 2
    return "".join(decoded)


def _schema_key(
    expected: Mapping[str, object], descriptor: FactDescriptor
) -> tuple[str, ValueType]:
    """Reject paths the declared scalar shape cannot ever produce for verification."""
    path = cast(str, expected["path"])
    segments = path[1:].split("/")
    if len(segments) != 1:
        raise ValueError("path depth rule allows one top-level scalar schema segment")
    key = _decode_pointer_segment(segments[0])
    try:
        return key, descriptor.value_schema[key]
    except KeyError as exc:
        raise ValueError(
            "path schema rule requires a declared key; "
            f"available keys: {sorted(descriptor.value_schema)!r}"
        ) from exc


def _matches_declared_type(value: object, schema: ValueType) -> bool:
    """Keep bool distinct from int because JSON's two values have distinct claim semantics."""
    return (
        (schema == "int" and type(value) is int)
        or (schema == "bool" and type(value) is bool)
        or (schema == "string" and type(value) is str)
        or (schema == "null|int" and (value is None or type(value) is int))
    )


def _assert_comparison_type(expected: Mapping[str, object], schema: ValueType) -> None:
    """Refuse impossible comparisons before they could become unreadable verdict rows."""
    operator = cast(str, expected["op"])
    if operator == "exists":
        return
    if operator in _ORDERING_OPERATORS:
        if schema != "int" or type(expected["value"]) is not int:
            raise ValueError("comparison type rule requires an integer ordering comparison")
        return
    if operator == "in":
        values = cast(list[object], expected["value"])
        if not all(_matches_declared_type(value, schema) for value in values):
            raise ValueError("comparison type rule requires every membership value to match schema")
        return
    if not _matches_declared_type(expected["value"], schema):
        raise ValueError("comparison type rule requires an equality value matching schema")


def _resolved_expected(
    expected: Mapping[str, object], descriptor: FactDescriptor
) -> Mapping[str, object]:
    """Replace a policy name once so a later catalogue edit cannot rewrite history."""
    if "policy" not in expected:
        return _immutable_mapping(expected)
    policy_name = cast(str, expected["policy"])
    try:
        value = descriptor.policies[policy_name]
    except KeyError as exc:
        raise ValueError(
            f"policy rule requires one of {sorted(descriptor.policies)!r}, got {policy_name!r}"
        ) from exc
    resolved = dict(expected)
    del resolved["policy"]
    resolved["value"] = value
    return _immutable_mapping(resolved)


def _validity_seconds(claim: ClaimInput, descriptor: FactDescriptor) -> int:
    """Store a durable validity bound instead of resolving a descriptor TTL later."""
    if claim.validity_seconds is not None:
        return claim.validity_seconds
    derived = descriptor.ttl_seconds * 4
    # Input bounds are a caller contract, but a catalogue TTL is not theirs to
    # repair; clamping keeps an extreme descriptor from making claims impossible.
    return min(max(derived, _MIN_VALIDITY_SECONDS), _MAX_VALIDITY_SECONDS)


def resolve_claim(claim: ClaimInput, descriptor: FactDescriptor) -> ResolvedClaim:
    """Resolve one validated input once against the descriptor the caller already holds."""
    if descriptor.name != claim.fact_name:
        raise ValueError(
            "descriptor name rule requires descriptor.name to equal claim.fact_name "
            f"({descriptor.name!r} != {claim.fact_name!r})"
        )

    expected = _immutable_mapping(claim.expected)
    expected_resolved = _resolved_expected(expected, descriptor)
    _, schema = _schema_key(expected_resolved, descriptor)
    _assert_comparison_type(expected_resolved, schema)
    validity_seconds = _validity_seconds(claim, descriptor)
    assert_expected_within_bounds(expected_resolved)

    return ResolvedClaim(
        claim_key=claim_key(
            statement=claim.statement,
            fact_name=claim.fact_name,
            expected=expected,
            target=descriptor.target,
            definition_version=descriptor.definition_version,
        ),
        statement=claim.statement,
        fact_name=claim.fact_name,
        definition_version=descriptor.definition_version,
        target=descriptor.target,
        expected=expected,
        expected_resolved=expected_resolved,
        validity_seconds=validity_seconds,
        replaces=claim.replaces,
    )
