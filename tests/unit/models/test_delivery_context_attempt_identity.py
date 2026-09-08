"""The safe context timeline belongs to the replayable assessment identity."""

from datetime import timedelta

import pytest

from brain_v42.models.delivery_evaluator import evaluate_delivery
from tests.delivery_helpers import FIXED_NOW, delivery_inputs


@pytest.mark.parametrize("field", ["last_attempt_at", "last_success_at"])
def test_context_timeline_change_is_observable_in_assessment_identity(field):
    inputs = delivery_inputs()
    original = evaluate_delivery(inputs, now=FIXED_NOW)
    context = inputs.contexts[0].model_copy(update={field: FIXED_NOW - timedelta(seconds=1)})
    changed = evaluate_delivery(inputs.model_copy(update={"contexts": (context,)}), now=FIXED_NOW)
    assert original.assessment_id != changed.assessment_id
    assert original.delivery_digest == changed.delivery_digest
