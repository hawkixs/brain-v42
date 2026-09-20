"""Contracts for fact probe descriptors and declared value schemas."""

from __future__ import annotations

from datetime import timedelta

import pytest

from brain_v42.facts import FactTarget
from brain_v42.facts.probe import FactDescriptor, check_value_schema


def test_value_schema_accepts_declared_exact_int_value() -> None:
    """A probe value matching its complete declaration remains measurable."""
    check_value_schema({"pending": 3}, {"pending": "int"})


@pytest.mark.parametrize(
    ("value", "schema"),
    [
        ({}, {"pending": "int"}),
        ({"pending": 3, "extra": 1}, {"pending": "int"}),
        ({"pending": True}, {"pending": "int"}),
        ({"healthy": 1}, {"healthy": "bool"}),
        ({"pending": None}, {"pending": "int"}),
    ],
)
def test_value_schema_refuses_missing_extra_and_wrong_json_types(
    value: dict[str, object], schema: dict[str, str]
) -> None:
    """A declaration catches precisely the shape drift that canonical JSON cannot express."""
    with pytest.raises(ValueError):
        check_value_schema(value, schema)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [None, 0])
def test_value_schema_allows_null_or_int_for_nullable_integer(value: object) -> None:
    """A nullable integer remains a closed two-variant type rather than an untyped null."""
    check_value_schema({"generation": value}, {"generation": "null|int"})


def test_descriptor_deadline_adds_queue_and_probe_bounds() -> None:
    """Readers can publish an honest end-to-end bound without recomputing it themselves."""
    descriptor = FactDescriptor(
        name="projection_lag",
        definition_version=1,
        target=FactTarget.PRODUCTION,
        ttl_seconds=15,
        timeout_seconds=3,
        queue_timeout_seconds=2,
        deadline_seconds=5,
        briefing=True,
        policies={"late_after_seconds": 300},
        value_schema={"pending": "int"},
    )
    assert descriptor.deadline_seconds == int(timedelta(seconds=2).total_seconds()) + 3
