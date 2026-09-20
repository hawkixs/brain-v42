"""Closed probe vocabulary so probes cannot choose their own trust boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType
from typing import Literal, Protocol

from brain_v42.facts.model import FactTarget, SourceIdentity

ValueType = Literal["int", "bool", "string", "null|int"]


class SourceSession(Protocol):
    """Expose identity beside a probe read from the same source transaction."""

    async def identity(self) -> SourceIdentity:
        """Return the source identity that makes this observation trustworthy."""


SourceFactory = Callable[[], AbstractAsyncContextManager[SourceSession]]


class Probe(Protocol):
    """Declare the complete, bounded contract for one named fact reader."""

    name: str
    definition_version: int
    target: FactTarget
    ttl: timedelta
    timeout: timedelta
    briefing: bool
    policies: Mapping[str, int]
    value_schema: Mapping[str, ValueType]

    async def measure(self, source: SourceSession) -> Mapping[str, object]:
        """Read one fact value without writing to the target."""


@dataclass(frozen=True, slots=True)
class FactDescriptor:
    """Publish immutable catalogue metadata without exposing mutable probe fields."""

    name: str
    definition_version: int
    target: FactTarget
    ttl_seconds: int
    timeout_seconds: int
    queue_timeout_seconds: int
    deadline_seconds: int
    briefing: bool
    policies: Mapping[str, int]
    value_schema: Mapping[str, ValueType]

    def __post_init__(self) -> None:
        """Freeze declarations because readers must see the registered definition."""
        object.__setattr__(self, "policies", MappingProxyType(dict(self.policies)))
        object.__setattr__(self, "value_schema", MappingProxyType(dict(self.value_schema)))


def check_value_schema(value: Mapping[str, object], schema: Mapping[str, ValueType]) -> None:
    """Reject values whose shape differs from the declaration persisted with the fact."""
    if not isinstance(value, Mapping):
        # A probe that returns None or a scalar has not measured an object; a
        # TypeError escaping here would be the third state §5.1 forbids.
        raise ValueError(f"value must be an object, not {type(value).__name__}")
    actual_keys = frozenset(value)
    expected_keys = frozenset(schema)
    missing = expected_keys - actual_keys
    extra = actual_keys - expected_keys
    if missing:
        raise ValueError(f"value has missing keys: {sorted(missing)!r}")
    if extra:
        raise ValueError(f"value has extra keys: {sorted(extra)!r}")

    for key, kind in schema.items():
        item = value[key]
        valid = (
            (kind == "int" and type(item) is int)
            or (kind == "bool" and type(item) is bool)
            or (kind == "string" and type(item) is str)
            or (kind == "null|int" and (item is None or type(item) is int))
        )
        if not valid:
            raise ValueError(f"value key {key!r} does not match declared type {kind!r}")
