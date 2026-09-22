"""Contract tests for the pure, persisted claim comparison boundary."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from uuid import UUID

import pytest

from brain_v42.facts.compare import Comparison, compare
from brain_v42.facts.model import FactTarget, Measured, SourceIdentity, Unreadable


def _measured(value: Mapping[str, object]) -> Measured:
    return Measured.from_value(
        fact="graph_projection_lag",
        definition_version=1,
        target=FactTarget.PRODUCTION,
        source=SourceIdentity("7612696091383607335", "brain", "127.0.0.1", 5432),
        value=value,
        observation_id=UUID("00000000-0000-0000-0000-000000000001"),
        measured_at=datetime(2026, 9, 22, tzinfo=UTC),
        duration_ms=1,
        ttl_seconds=60,
    )


@pytest.mark.parametrize(
    ("expected", "value", "wanted"),
    [
        ({"path": "/number", "op": "eq", "value": 7}, {"number": 7}, "holds"),
        ({"path": "/number", "op": "eq", "value": 7}, {"number": 8}, "falsified"),
        ({"path": "/number", "op": "ne", "value": 7}, {"number": 8}, "holds"),
        ({"path": "/number", "op": "ne", "value": 7}, {"number": 7}, "falsified"),
        ({"path": "/number", "op": "lt", "value": 8}, {"number": 7}, "holds"),
        ({"path": "/number", "op": "lt", "value": 7}, {"number": 7}, "falsified"),
        ({"path": "/number", "op": "lte", "value": 7}, {"number": 7}, "holds"),
        ({"path": "/number", "op": "lte", "value": 6}, {"number": 7}, "falsified"),
        ({"path": "/number", "op": "gt", "value": 6}, {"number": 7}, "holds"),
        ({"path": "/number", "op": "gt", "value": 7}, {"number": 7}, "falsified"),
        ({"path": "/number", "op": "gte", "value": 7}, {"number": 7}, "holds"),
        ({"path": "/number", "op": "gte", "value": 8}, {"number": 7}, "falsified"),
        ({"path": "/number", "op": "in", "value": [6, 7]}, {"number": 7}, "holds"),
        ({"path": "/number", "op": "in", "value": [6, 8]}, {"number": 7}, "falsified"),
        ({"path": "/number", "op": "exists"}, {"number": 7}, "holds"),
        ({"path": "/number", "op": "exists"}, {"other": 7}, "falsified"),
    ],
)
def test_compare_all_operators_return_conclusive_results(
    expected: Mapping[str, object], value: Mapping[str, object], wanted: str
) -> None:
    """Changing any operator branch changes an externally stored verdict."""
    result = compare(expected, _measured(value))

    assert result == Comparison(verdict=wanted, reason=None)


def test_compare_unreadable_measurement_preserves_its_closed_error_code() -> None:
    """Probe failures are evidence of uncertainty, never a false claim."""
    unreadable = Unreadable(
        fact="graph_projection_lag",
        definition_version=1,
        target=FactTarget.PRODUCTION,
        error_code="timeout",
        where=None,
        observation_id=UUID("00000000-0000-0000-0000-000000000002"),
        measured_at=datetime(2026, 9, 22, tzinfo=UTC),
        duration_ms=1,
        ttl_seconds=60,
        source_kind="probe",
    )

    assert compare({"path": "/number", "op": "eq", "value": 7}, unreadable) == Comparison(
        verdict="unreadable", reason="probe:timeout"
    )


def test_compare_traverses_escaped_object_keys_and_canonical_array_indexes() -> None:
    """Escaped pointer syntax must address one key, rather than a different tree path."""
    result = compare(
        {"path": "/a~1b/~0key/0", "op": "eq", "value": 7},
        _measured({"a/b": {"~key": [7]}}),
    )

    assert result == Comparison(verdict="holds", reason=None)
    assert compare(
        {"path": "/items/01", "op": "eq", "value": 7}, _measured({"items": [7, 8]})
    ) == Comparison(verdict="unreadable", reason="path_absent")


@pytest.mark.parametrize("index", ["٠", "9" * 10_000])
def test_compare_refuses_non_ascii_or_unbounded_array_indexes(index: str) -> None:
    """RFC 6901 array indexes use bounded ASCII digits and never raise at the boundary."""
    assert compare(
        {"path": f"/items/{index}", "op": "eq", "value": 7}, _measured({"items": [7]})
    ) == Comparison(verdict="unreadable", reason="path_absent")


def test_compare_distinguishes_an_absent_path_from_a_present_null() -> None:
    """A nullable value is comparable, while an absent non-existence is unknowable."""
    measurement = _measured({"null_value": None})

    assert compare({"path": "/null_value", "op": "eq", "value": None}, measurement) == Comparison(
        verdict="holds", reason=None
    )
    assert compare({"path": "/missing", "op": "eq", "value": None}, measurement) == Comparison(
        verdict="unreadable", reason="path_absent"
    )
    assert compare({"path": "/null_value", "op": "exists"}, measurement) == Comparison(
        verdict="falsified", reason=None
    )


def test_compare_nullable_equality_distinguishes_null_from_an_integer() -> None:
    """A nullable scalar mismatch is conclusive, not a generic type failure."""
    measurement = _measured({"number": 7})

    assert compare({"path": "/number", "op": "eq", "value": None}, measurement) == Comparison(
        verdict="falsified", reason=None
    )
    assert compare({"path": "/number", "op": "ne", "value": None}, measurement) == Comparison(
        verdict="holds", reason=None
    )


@pytest.mark.parametrize("op", ["eq", "ne"])
def test_compare_does_not_treat_bool_as_an_integer(op: str) -> None:
    """Python equality must not collapse distinct persisted scalar kinds."""
    assert compare(
        {"path": "/value", "op": op, "value": 1}, _measured({"value": True})
    ) == Comparison(verdict="unreadable", reason="type_mismatch")


@pytest.mark.parametrize(
    ("expected", "reason"),
    [
        ({"path": "/number", "op": "eq", "value": True}, "type_mismatch"),
        ({"path": "/number", "op": "lt", "value": True}, "type_mismatch"),
        ({"path": "/number", "op": "in", "value": [True]}, "type_mismatch"),
        ({"path": "/number", "op": "unknown", "value": 7}, "type_mismatch"),
        ({"path": "number", "op": "eq", "value": 7}, "path_absent"),
        ({"path": "/number", "op": "exists", "value": 7}, "type_mismatch"),
        ({"path": "/number", "op": "eq"}, "type_mismatch"),
        ({"path": "/number", "op": "in", "value": 7}, "type_mismatch"),
    ],
)
def test_compare_malformed_or_type_incompatible_expectations_are_unreadable(
    expected: Mapping[str, object], reason: str
) -> None:
    """Malformed persisted data cannot escape the pure comparison boundary."""
    assert compare(expected, _measured({"number": 7})) == Comparison(
        verdict="unreadable", reason=reason
    )


def test_compare_does_not_mutate_immutable_expected_input() -> None:
    """Hashable persisted input remains unchanged after a comparison attempt."""
    expected = MappingProxyType({"path": "/number", "op": "in", "value": [6, 7]})

    assert compare(expected, _measured({"number": 7})) == Comparison(verdict="holds", reason=None)
    assert expected["value"] == [6, 7]
