"""Pausing delivery leaves reads available and refuses every public mutation."""

import pytest

from brain_v42.models.delivery import DeliveryError
from tests.integration.db.test_delivery_requester_acceptance import _service, _workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.mark.parametrize("operation", ["refresh", "renew_claim", "release_claim"])
async def test_paused_public_mutations_refuse_before_claim_checks(session_factory, operation):
    ticket, _binding, _enabled = await _workflow(session_factory)
    paused = _service(session_factory, enabled=False)
    kwargs = {"actor_project": "executor"}
    if operation != "refresh":
        kwargs.update(owner_key="external", claim_token="not-a-live-token", epoch=1)
    with pytest.raises(DeliveryError) as error:
        await getattr(paused, operation)(ticket.id, **kwargs)
    assert error.value.code == "delivery_disabled"
    view = await paused.get(ticket.id, actor_project="requester")
    assert view.assessment.observation_health == "disabled"
