"""Fail closed before any verification, in both modes (spec §6.1, ADR 27 lot C T1.7)."""

from __future__ import annotations

from brain_v42.facts.nightly import check_preconditions


class _Registry:
    def __init__(
        self, *, refusals: dict[str, str] | None = None, disabled: dict[str, str] | None = None
    ) -> None:
        self._refusals = refusals or {}
        self._disabled = disabled or {}

    def refusals(self) -> dict[str, str]:
        return dict(self._refusals)

    def disabled(self) -> dict[str, str]:
        return dict(self._disabled)


def test_unregistered_definitions_fail_closed() -> None:
    failure = check_preconditions(_Registry(), ["fact_a"], definitions_registered=False)

    assert failure is not None
    assert failure.reason == "definitions_unregistered"


def test_an_eligible_refused_fact_fails_closed_with_its_name() -> None:
    registry = _Registry(refusals={"fact_a": "unverifiable_target"})

    failure = check_preconditions(registry, ["fact_a", "fact_b"], definitions_registered=True)

    assert failure is not None
    assert failure.reason == "unverifiable_target: fact_a"


def test_an_eligible_disabled_fact_fails_closed_with_its_name() -> None:
    registry = _Registry(disabled={"fact_a": "definition_drift"})

    failure = check_preconditions(registry, ["fact_a"], definitions_registered=True)

    assert failure is not None
    assert failure.reason == "definition_drift: fact_a"


def test_multiple_refused_facts_are_named_together_sorted() -> None:
    registry = _Registry(refusals={"fact_b": "x", "fact_a": "x"})

    failure = check_preconditions(registry, ["fact_a", "fact_b"], definitions_registered=True)

    assert failure is not None
    assert failure.reason == "unverifiable_target: fact_a, fact_b"


def test_a_refused_fact_no_eligible_claim_names_does_not_fail_the_step() -> None:
    registry = _Registry(refusals={"fact_z": "unverifiable_target"})

    failure = check_preconditions(registry, ["fact_a"], definitions_registered=True)

    assert failure is None


def test_a_disabled_fact_no_eligible_claim_names_does_not_fail_the_step() -> None:
    registry = _Registry(disabled={"fact_z": "definition_drift"})

    failure = check_preconditions(registry, ["fact_a"], definitions_registered=True)

    assert failure is None


def test_refusal_is_checked_before_disabled() -> None:
    """Both apply to the same fact: refused (never registered) wins over disabled."""
    registry = _Registry(
        refusals={"fact_a": "unverifiable_target"}, disabled={"fact_a": "definition_drift"}
    )

    failure = check_preconditions(registry, ["fact_a"], definitions_registered=True)

    assert failure is not None
    assert failure.reason == "unverifiable_target: fact_a"


def test_no_precondition_failure_when_nothing_is_wrong() -> None:
    failure = check_preconditions(_Registry(), ["fact_a"], definitions_registered=True)

    assert failure is None
