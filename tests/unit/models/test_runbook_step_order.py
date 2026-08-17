"""A payload accepted by runbook create must stay acceptable to runbook update.

Reported by red-shrik alongside ticket 2af71e69: ``brain_create_runbook``
auto-assigned ``steps[].order`` while ``brain_update`` required it, so a runbook
created without ``order`` could not be re-updated with the same payload. The
asymmetry lived in the tool layer, which meant the two write paths disagreed
about what a valid step is.

Normalizing in the model instead makes both paths share one definition.
"""

from __future__ import annotations

import pytest

from brain_v42.models.runbook import RunbookCreate, RunbookUpdate


def _create(**overrides: object) -> RunbookCreate:
    payload: dict[str, object] = {
        "title": "t",
        "description": "d",
        "project_key": "brain-v42",
        "trigger": "manual",
    }
    payload.update(overrides)
    return RunbookCreate(**payload)  # type: ignore[arg-type]


_UNORDERED_STEPS = [{"title": "first"}, {"title": "second"}, {"title": "third"}]


def test_create_assigns_sequential_order_when_omitted() -> None:
    """Steps without ``order`` are numbered by position, starting at 1."""
    runbook = _create(steps=_UNORDERED_STEPS)

    assert [step.order for step in runbook.steps] == [1, 2, 3]


def test_update_accepts_the_same_step_payload_as_create() -> None:
    """The round trip red-shrik could not perform: same payload, both paths."""
    update = RunbookUpdate(steps=_UNORDERED_STEPS)

    assert update.steps is not None
    assert [step.order for step in update.steps] == [1, 2, 3]


def test_explicit_order_is_preserved_over_positional_numbering() -> None:
    """A caller that numbers its own steps keeps control of the sequence."""
    steps = [{"title": "a", "order": 10}, {"title": "b", "order": 20}]

    assert [s.order for s in _create(steps=steps).steps] == [10, 20]
    update = RunbookUpdate(steps=steps)
    assert update.steps is not None
    assert [s.order for s in update.steps] == [10, 20]


def test_mixed_payload_numbers_only_the_steps_that_omit_order() -> None:
    """Partial numbering must not renumber the steps the caller pinned."""
    steps = [{"title": "a"}, {"title": "b", "order": 99}, {"title": "c"}]

    assert [s.order for s in _create(steps=steps).steps] == [1, 99, 3]


def test_rollback_steps_are_normalized_like_steps() -> None:
    """``rollback_steps`` is the same shape and must not be forgotten."""
    runbook = _create(steps=[{"title": "go"}], rollback_steps=_UNORDERED_STEPS)

    assert [step.order for step in runbook.rollback_steps] == [1, 2, 3]
    update = RunbookUpdate(rollback_steps=_UNORDERED_STEPS)
    assert update.rollback_steps is not None
    assert [step.order for step in update.rollback_steps] == [1, 2, 3]


@pytest.mark.parametrize("field", ["steps", "rollback_steps"])
def test_step_objects_still_reject_a_missing_title(field: str) -> None:
    """Auto-numbering must not turn into a blanket acceptance of junk steps."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RunbookUpdate(**{field: [{"command": "ls"}]})
