"""Unit contracts for resolving declared claim inputs before persistence."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from brain_v42.facts import FactTarget
from brain_v42.facts.probe import FactDescriptor
from brain_v42.mcp.tools.claim_writes import resolve_claim_inputs


def _descriptor(name: str = "graph_projection_lag") -> FactDescriptor:
    """Build the closed descriptor consumed by the claim resolution boundary."""
    return FactDescriptor(
        name=name,
        definition_version=1,
        target=FactTarget.PRODUCTION,
        ttl_seconds=15,
        timeout_seconds=3,
        queue_timeout_seconds=2,
        deadline_seconds=5,
        briefing=True,
        policies={"late_after_seconds": 300},
        value_schema={"lag_seconds": "int"},
    )


class _Registry:
    """Minimal closed catalogue whose lookups model the writer dependency."""

    def __init__(self) -> None:
        self._descriptor = _descriptor()

    def describe(self, name: str) -> FactDescriptor:
        if name != self._descriptor.name:
            from brain_v42.facts.registry import UnknownFactError

            raise UnknownFactError(name)
        return self._descriptor

    def names(self) -> tuple[str, ...]:
        return (self._descriptor.name,)


def _claim(**overrides: object) -> dict[str, object]:
    """Return one structurally valid declaration with a literal expected result."""
    claim: dict[str, object] = {
        "statement": "The graph projection is caught up.",
        "fact_name": "graph_projection_lag",
        "expected": {"path": "/lag_seconds", "op": "lte", "value": 300},
    }
    claim.update(overrides)
    return claim


async def test_resolution_refuses_an_unknown_fact_and_names_available_vocabulary() -> None:
    """A misspelling must tell the caller how to form a valid closed-catalogue request."""
    with pytest.raises(ValueError, match="unknown_fact") as exc_info:
        await resolve_claim_inputs(_Registry(), [_claim(fact_name="unknown_fact")])

    assert "graph_projection_lag" in str(exc_info.value)


async def test_resolution_refuses_more_than_ten_inputs_before_catalogue_lookup() -> None:
    """Removing the bound would let one entry monopolise a single write transaction."""
    with pytest.raises(ValueError, match="input count rule"):
        await resolve_claim_inputs(
            _Registry(), [_claim(statement=f"Claim {number}") for number in range(11)]
        )


async def test_resolution_refuses_duplicate_inputs_before_catalogue_lookup() -> None:
    """Duplicate declarations must not become redundant immutable occurrences."""
    claim = _claim()

    with pytest.raises(ValueError, match="duplicate input rule"):
        await resolve_claim_inputs(_Registry(), [claim, claim])


async def test_invalid_second_input_reaches_no_insert() -> None:
    """Resolution completes before persistence, so a late refusal cannot write a prefix."""
    session = AsyncMock()

    with pytest.raises(ValueError, match="missing_fact"):
        await resolve_claim_inputs(
            _Registry(),
            [
                _claim(statement="First valid declaration."),
                _claim(fact_name="missing_fact"),
                _claim(),
            ],
        )

    session.execute.assert_not_awaited()
