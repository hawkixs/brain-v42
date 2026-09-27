"""Closed structural validation for claim inputs before a writer opens its transaction."""

from __future__ import annotations

import ast
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from brain_v42.models.claim_input import ClaimInput, validate_claim_inputs


def _input(**overrides: object) -> ClaimInput:
    """Build the smallest valid structural claim input for one focused mutation."""
    payload: dict[str, object] = {
        "statement": "The projection is current.",
        "fact_name": "graph_projection_lag",
        "expected": {"path": "/lag_seconds", "op": "lte", "value": 300},
    }
    payload.update(overrides)
    return ClaimInput(**payload)


def _error_for(**overrides: object) -> str:
    """Return Pydantic's caller-visible error text for one rejected payload."""
    with pytest.raises(ValidationError) as exc_info:
        _input(**overrides)
    return str(exc_info.value)


def test_statement_rule_strips_and_refuses_blank_or_overlong_text() -> None:
    """Whitespace must not produce an empty-looking claim or hide a length overflow."""
    assert _input(statement="  stated  ").statement == "stated"
    assert "statement rule" in _error_for(statement=" \t ")
    assert "statement rule" in _error_for(statement="x" * 501)


@pytest.mark.parametrize("fact_name", ["Graph", "1fact", "x" * 65])
def test_fact_name_rule_refuses_names_outside_the_catalogue_vocabulary(fact_name: str) -> None:
    """A malformed name cannot reach the live catalogue lookup later in the write path."""
    assert "fact_name rule" in _error_for(fact_name=fact_name)


@pytest.mark.parametrize("op", ["regex", "matches"])
def test_op_rule_refuses_removed_or_unknown_operators(op: str) -> None:
    """Regex was intentionally removed and must not return through an ignored spelling."""
    message = _error_for(expected={"path": "/value", "op": op, "value": "x"})
    assert "op rule" in message
    if op == "regex":
        assert "removed" in message


def test_expected_fields_rule_requires_path_op_and_one_right_hand_side() -> None:
    """The comparison shape has no ambiguous missing or competing operand."""
    assert "expected fields rule" in _error_for(expected={"op": "eq", "value": 1})
    assert "expected fields rule" in _error_for(expected={"path": "/a", "value": 1})
    assert "expected fields rule" in _error_for(expected={"path": "/a", "op": "eq"})
    assert "expected fields rule" in _error_for(
        expected={"path": "/a", "op": "eq", "value": 1, "policy": "bound"}
    )


def test_path_rule_requires_a_bounded_valid_json_pointer() -> None:
    """Comparison paths are bounded pointers whose escapes are checked before later lookup."""
    assert "path rule" in _error_for(expected={"path": "a", "op": "eq", "value": 1})
    assert "path segments rule" in _error_for(
        expected={"path": "/a/a/a/a/a/a/a/a/a", "op": "eq", "value": 1}
    )
    assert "path segment length rule" in _error_for(
        expected={"path": f"/{'a' * 65}", "op": "eq", "value": 1}
    )
    assert "path escape rule" in _error_for(expected={"path": "/a~", "op": "eq", "value": 1})


def test_path_rule_unescapes_before_segment_measurement() -> None:
    """Escaped slash and tilde remain one segment rather than changing pointer cardinality."""
    claim = _input(expected={"path": "/a~1b", "op": "eq", "value": 1})

    assert claim.expected["path"] == "/a~1b"

    assert _input(expected={"path": "/a~0b", "op": "eq", "value": 1})


def test_value_rule_allows_only_bounded_non_float_scalars() -> None:
    """A claim that canonical JSON cannot key is rejected at the structural boundary."""
    assert "value rule" in _error_for(expected={"path": "/a", "op": "eq", "value": 1.5})
    assert "value rule" in _error_for(expected={"path": "/a", "op": "eq", "value": list(range(33))})
    assert _input(expected={"path": "/a", "op": "eq", "value": [None, True, "x", 1]})


def test_in_rule_requires_a_value_array() -> None:
    """Membership has one valid right-hand-side domain: an array of scalars."""
    assert "in rule" in _error_for(expected={"path": "/a", "op": "in", "value": 1})
    assert "in rule" in _error_for(expected={"path": "/a", "op": "in", "policy": "bound"})
    assert _input(expected={"path": "/a", "op": "in", "value": [1]})


def test_exists_rule_takes_no_right_hand_side() -> None:
    """Presence is unary, so a value or catalogue policy would be ignored ambiguity."""
    assert "exists rule" in _error_for(expected={"path": "/a", "op": "exists", "value": 1})
    assert "exists rule" in _error_for(expected={"path": "/a", "op": "exists", "policy": "bound"})
    assert _input(expected={"path": "/a", "op": "exists"})


def test_policy_rule_requires_a_non_blank_name() -> None:
    """Catalogue resolution can only start from an explicit policy identifier."""
    assert "policy rule" in _error_for(expected={"path": "/a", "op": "eq", "policy": "  "})
    assert _input(expected={"path": "/a", "op": "eq", "policy": "late_after_seconds"})


def test_validity_seconds_rule_enforces_both_inclusive_bounds() -> None:
    """A verdict can be current only within the declared finite validity window."""
    assert "validity_seconds rule" in _error_for(validity_seconds=59)
    assert "validity_seconds rule" in _error_for(validity_seconds=31_536_001)
    assert _input(validity_seconds=60).validity_seconds == 60
    assert _input(validity_seconds=31_536_000).validity_seconds == 31_536_000


def test_replaces_accepts_a_uuid_or_none() -> None:
    """The service receives an optional typed predecessor identity, never opaque text."""
    replacement = UUID("00000000-0000-0000-0000-000000000001")
    assert _input(replaces=replacement).replaces == replacement


def test_measure_rule_defaults_to_false_and_accepts_only_a_bool() -> None:
    """Absent input keeps today's declared-only behaviour; only True/False is a valid ask."""
    assert _input().measure is False
    assert _input(measure=True).measure is True
    assert _input(measure=False).measure is False
    assert "measure rule" in _error_for(measure="true")
    assert "measure rule" in _error_for(measure=1)
    assert "measure rule" in _error_for(measure=None)


def test_input_count_rule_refuses_more_than_ten_inputs() -> None:
    """The write transaction stays bounded even before catalogue resolution begins."""
    with pytest.raises(ValueError, match="input count rule"):
        validate_claim_inputs([_input(statement=str(index)) for index in range(11)])


def test_duplicate_rule_refuses_identical_validated_inputs() -> None:
    """A duplicate request fails as a named caller error before the database unique index."""
    claim = _input()

    with pytest.raises(ValueError, match="duplicate input rule"):
        validate_claim_inputs([claim, claim.model_copy()])


def test_claim_input_module_remains_a_models_leaf() -> None:
    """The model must not hide a facts/service edge behind a runtime import."""
    source_path = Path(__import__("brain_v42.models.claim_input", fromlist=["__file__"]).__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(not alias.name.startswith("brain_v42") for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module is None or not node.module.startswith("brain_v42")
