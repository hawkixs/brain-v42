"""Contracts for resolving a declared claim against one fact definition."""

from __future__ import annotations

import pytest

from brain_v42.facts import FactTarget, ResolvedClaim, resolve_claim
from brain_v42.facts.probe import FactDescriptor
from brain_v42.models.claim_input import ClaimInput


def _descriptor(**overrides: object) -> FactDescriptor:
    """Build a complete catalogue entry with distinct policy names for keying tests."""
    payload: dict[str, object] = {
        "name": "graph_projection_lag",
        "definition_version": 3,
        "target": FactTarget.PRODUCTION,
        "ttl_seconds": 15,
        "timeout_seconds": 3,
        "queue_timeout_seconds": 2,
        "deadline_seconds": 5,
        "briefing": True,
        "policies": {"late_after_seconds": 300, "warning_after_seconds": 300},
        "value_schema": {"lag_seconds": "int", "label": "string", "enabled": "bool"},
    }
    payload.update(overrides)
    return FactDescriptor(**payload)  # type: ignore[arg-type]


def _claim(**overrides: object) -> ClaimInput:
    """Build the smallest valid structural claim before catalogue resolution."""
    payload: dict[str, object] = {
        "statement": "The projection is current.",
        "fact_name": "graph_projection_lag",
        "expected": {"path": "/lag_seconds", "op": "lte", "value": 300},
    }
    payload.update(overrides)
    return ClaimInput(**payload)


@pytest.mark.parametrize(
    ("expected", "want_resolved"),
    [
        (
            {"path": "/lag_seconds", "op": "lte", "policy": "late_after_seconds"},
            {"path": "/lag_seconds", "op": "lte", "value": 300},
        ),
        (
            {"path": "/lag_seconds", "op": "lte", "value": 120},
            {"path": "/lag_seconds", "op": "lte", "value": 120},
        ),
    ],
)
def test_resolve_claim_resolves_policy_once_and_keeps_distinct_immutable_columns(
    expected: dict[str, object], want_resolved: dict[str, object]
) -> None:
    """A stored verifier input must never retain a live policy reference or shared mapping."""
    claim = _claim(expected=expected)

    resolved = resolve_claim(claim, _descriptor())

    assert isinstance(resolved, ResolvedClaim)
    assert dict(resolved.expected) == expected
    assert dict(resolved.expected_resolved) == want_resolved
    assert resolved.expected is not resolved.expected_resolved
    with pytest.raises(TypeError):
        resolved.expected["new"] = "value"  # type: ignore[index]


def test_resolve_claim_refuses_a_descriptor_for_a_different_fact() -> None:
    """A wiring error must not accidentally create a claim for another catalogue entry."""
    with pytest.raises(ValueError, match="descriptor name rule"):
        resolve_claim(_claim(), _descriptor(name="another_fact"))


def test_resolve_claim_lists_available_policy_names_for_an_unknown_policy() -> None:
    """A caller needs the descriptor vocabulary to repair a policy spelling."""
    claim = _claim(expected={"path": "/lag_seconds", "op": "lte", "policy": "missing_policy"})

    with pytest.raises(ValueError, match="policy rule") as exc_info:
        resolve_claim(claim, _descriptor())

    assert "late_after_seconds" in str(exc_info.value)
    assert "warning_after_seconds" in str(exc_info.value)


def test_resolve_claim_refuses_a_path_outside_the_declared_shape() -> None:
    """A path absent from the probe schema would otherwise be permanently unreadable."""
    claim = _claim(expected={"path": "/unknown", "op": "eq", "value": 1})

    with pytest.raises(ValueError, match="path schema rule") as exc_info:
        resolve_claim(claim, _descriptor())

    assert "lag_seconds" in str(exc_info.value)
    assert "label" in str(exc_info.value)


def test_resolve_claim_refuses_a_deeper_pointer_than_the_scalar_schema_declares() -> None:
    """The descriptor describes only top-level scalar values, not nested structure."""
    claim = _claim(expected={"path": "/lag_seconds/detail", "op": "eq", "value": 1})

    with pytest.raises(ValueError, match="path depth rule"):
        resolve_claim(claim, _descriptor())


@pytest.mark.parametrize(
    ("schema", "expected", "raises"),
    [
        ("string", {"path": "/value", "op": "lt", "value": "low"}, True),
        ("int", {"path": "/value", "op": "eq", "value": True}, True),
        ("bool", {"path": "/value", "op": "ne", "value": 1}, True),
        ("null|int", {"path": "/value", "op": "eq", "value": "missing"}, True),
        ("int", {"path": "/value", "op": "in", "value": [1, True]}, True),
        ("string", {"path": "/value", "op": "in", "value": ["ready", 2]}, True),
        ("int", {"path": "/value", "op": "exists"}, False),
        ("bool", {"path": "/value", "op": "exists"}, False),
        ("string", {"path": "/value", "op": "exists"}, False),
        ("null|int", {"path": "/value", "op": "exists"}, False),
    ],
)
def test_resolve_claim_checks_the_operator_value_against_the_declared_type(
    schema: str, expected: dict[str, object], raises: bool
) -> None:
    """Invalid comparisons are rejected before they could become unreadable verdict rows."""
    descriptor = _descriptor(value_schema={"value": schema})
    claim = _claim(expected=expected)

    if raises:
        with pytest.raises(ValueError, match="comparison type rule"):
            resolve_claim(claim, descriptor)
    else:
        assert resolve_claim(claim, descriptor).expected_resolved == expected


@pytest.mark.parametrize(
    ("ttl_seconds", "input_validity", "want_validity"),
    [
        (15, 120, 120),
        (15, None, 60),
        (1, None, 60),
        (10_000_000, None, 31_536_000),
    ],
)
def test_resolve_claim_materialises_validity_at_write_time(
    ttl_seconds: int, input_validity: int | None, want_validity: int
) -> None:
    """The occurrence retains an explicit validity bound after catalogue TTLs later change."""
    resolved = resolve_claim(
        _claim(validity_seconds=input_validity), _descriptor(ttl_seconds=ttl_seconds)
    )

    assert resolved.validity_seconds == want_validity


def test_resolve_claim_keys_the_unresolved_policy_name_not_its_resolved_value() -> None:
    """Equivalent policy values retain distinct author-written content identities."""
    descriptor = _descriptor()
    late = resolve_claim(
        _claim(expected={"path": "/lag_seconds", "op": "lte", "policy": "late_after_seconds"}),
        descriptor,
    )
    warning = resolve_claim(
        _claim(expected={"path": "/lag_seconds", "op": "lte", "policy": "warning_after_seconds"}),
        descriptor,
    )

    assert late.expected_resolved == warning.expected_resolved
    assert late.claim_key != warning.claim_key


def test_resolved_claim_exposes_the_descriptor_identity_and_replacement() -> None:
    """The write path receives every immutable occurrence field without consulting the registry."""
    replacement = "00000000-0000-0000-0000-000000000001"
    claim = _claim(replaces=replacement)

    resolved = resolve_claim(claim, _descriptor())

    assert resolved.statement == claim.statement
    assert resolved.fact_name == claim.fact_name
    assert resolved.definition_version == 3
    assert resolved.target is FactTarget.PRODUCTION
    assert str(resolved.replaces) == replacement
