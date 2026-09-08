"""PostgreSQL delivery facts survive unchanged when read in a briefing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from brain_v42.mcp.tools.session_tools import make_session_briefing_loader
from brain_v42.services.dream_run_service import KillswitchState
from tests.integration.db.test_delivery_reads import ReadCase
from tests.integration.db.test_delivery_requester_acceptance import _accept

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _loader(delivery_svc):
    context = MagicMock()
    context.get_by_key = AsyncMock(return_value=None)
    decisions = MagicMock(list_all=AsyncMock(return_value=[]))
    learnings = MagicMock(list_all=AsyncMock(return_value=[]))
    dream = MagicMock(
        killswitch_state=AsyncMock(
            return_value=KillswitchState(
                last_run_date=None,
                promote_enabled=False,
                promote_dry=False,
                reorg_enabled=False,
                reorg_dry=False,
                promote_clean_dry_nights=0,
                reorg_clean_dry_nights=0,
            )
        ),
        last_failure=AsyncMock(return_value=None),
    )
    feature = MagicMock(
        roadmap_alive=AsyncMock(return_value=[]), stale_pinned=AsyncMock(return_value=[])
    )
    sessions = MagicMock(recent_checkpoints=AsyncMock(return_value=[]))
    return make_session_briefing_loader(
        context,
        decisions,
        learnings,
        dream,
        feature,
        sessions,
        delivery_svc=delivery_svc,
    )


async def test_pg_briefing_reuses_current_assessment_and_receipt_identities(
    session_factory, monkeypatch
):
    """The briefing cannot fabricate a second delivery projection or poll a provider."""
    from brain_v42.delivery_observer.github import GitHubClient

    async def no_network(*args, **kwargs):
        raise AssertionError("the delivery briefing must only use PostgreSQL reads")

    monkeypatch.setattr(GitHubClient, "collect", no_network)
    case = ReadCase(session_factory)
    ticket = await case.create()
    integration = await case.verify(ticket, merged=True)
    fulfillment = await _accept(
        case.service,
        ticket.id,
        integration,
        actor_project=case.actor,
        caller_identity="briefing-reader",
    )
    view = await case.service.get(ticket.id, actor_project=case.actor)
    assert view.integration_receipt == integration and view.fulfillment_receipt == fulfillment
    loader = _loader(case.service)

    with patch(
        "brain_v42.mcp.tools.session_tools.get_settings",
        return_value=SimpleNamespace(graph_enabled=False, brain_dream_cross_project_enabled=False),
    ):
        briefing = await loader(case.actor, uuid4())

    assert view.assessment.assessment_id in briefing
    assert view.assessment.delivery_digest in briefing
    assert f"fraîcheur={view.assessment.observation_health}" in briefing
    assert f"révision={view.contract.contract_revision}" in briefing
    assert f"étape={view.assessment.delivery_stage}" in briefing
    assert f"acceptation={view.assessment.acceptance_state}" in briefing
    assert str(integration.id) in briefing
    assert str(fulfillment.id) in briefing
