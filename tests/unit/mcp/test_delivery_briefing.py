"""Bounded, read-only delivery text embedded in a session briefing."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from brain_v42.mcp.tools.delivery_formatters import format_delivery_briefing
from brain_v42.mcp.tools.session_tools import make_session_briefing_loader
from brain_v42.services.dream_run_service import KillswitchState


def _view(
    ticket_id: UUID,
    *,
    health: str = "fresh",
    assessment_id: str = "a" * 64,
    digest: str = "b" * 64,
) -> SimpleNamespace:
    """A read shape with the facts the formatter is allowed to expose."""
    return SimpleNamespace(
        contract=SimpleNamespace(
            ticket_id=ticket_id,
            contract_revision=4,
            objective="Livrer le contrat observable.",
            context_refs=(),
        ),
        assessment=SimpleNamespace(
            assessment_id=assessment_id,
            delivery_digest=digest,
            observation_health=health,
            delivery_stage="verified",
            acceptance_state="pending",
            integration_receipt_eligible=True,
            completion_eligible_now=False,
            blockers=(SimpleNamespace(code="pr_not_merged", detail="La PR attend son merge."),),
            eligible_work=(SimpleNamespace(kind="integrate", role="executor"),),
        ),
        bindings=(
            SimpleNamespace(
                last_attempt_at=datetime(2026, 9, 8, 10, 30, tzinfo=UTC),
                last_success_at=datetime(2026, 9, 8, 10, 20, tzinfo=UTC),
            ),
        ),
        contexts=(
            SimpleNamespace(
                reference_identity="repository_document:1337:docs/proof.md",
                required=True,
                status="available",
                last_attempt_at=datetime(2026, 9, 8, 10, 31, tzinfo=UTC),
                last_success_at=datetime(2026, 9, 8, 10, 21, tzinfo=UTC),
            ),
            SimpleNamespace(
                reference_identity="repository_document:1337:docs/optional.md",
                required=False,
                status="error",
                last_attempt_at=datetime(2026, 9, 8, 10, 32, tzinfo=UTC),
                last_success_at=None,
            ),
        ),
        integration_receipt=SimpleNamespace(id=uuid4()),
        fulfillment_receipt=SimpleNamespace(id=uuid4()),
    )


def _page(*items: SimpleNamespace, omitted_count: int = 0) -> SimpleNamespace:
    return SimpleNamespace(items=items, omitted_count=omitted_count, next_cursor=None)


def _services() -> tuple[MagicMock, MagicMock, MagicMock, MagicMock, MagicMock, MagicMock]:
    context = MagicMock()
    context.get_by_key = AsyncMock(
        return_value=SimpleNamespace(
            project_key="brain-v42",
            current_focus="Rendre les faits de livraison lisibles.",
            focus_updated_at=datetime(2026, 9, 7, 14, tzinfo=UTC),
            description=None,
            blockers=[],
        )
    )
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
    return context, decisions, learnings, dream, feature, sessions


def test_formatter_keeps_shared_delivery_identity_and_required_action_context() -> None:
    view = _view(UUID("00000000-0000-0000-0000-000000000001"))

    output = format_delivery_briefing(_page(view))

    assert "### Livraison" in output
    assert str(view.contract.ticket_id) in output
    assert "révision=4" in output
    assert view.assessment.assessment_id in output
    assert view.assessment.delivery_digest in output
    assert str(view.integration_receipt.id) in output
    assert str(view.fulfillment_receipt.id) in output
    assert "fraîcheur=fresh" in output
    assert "étape=verified" in output and "acceptation=pending" in output
    assert "dernière tentative" in output and "dernier succès" in output
    assert "pr_not_merged" in output and "integrate/executor" in output
    assert "docs/proof.md" in output
    assert "docs/optional.md" not in output
    assert "claim_token" not in output


def test_formatter_caps_at_five_and_reports_page_and_local_omissions() -> None:
    items = tuple(_view(UUID(int=index + 1)) for index in range(7))

    output = format_delivery_briefing(_page(*items, omitted_count=3))

    assert all(str(item.contract.ticket_id) in output for item in items[:5])
    assert all(str(item.contract.ticket_id) not in output for item in items[5:])
    assert "5 livraisons omises au total" in output
    assert "3 non retournés par la page" in output and "2 masqués par le cap" in output
    assert "brain_delivery_list" in output


@pytest.mark.asyncio
async def test_reusable_loader_declares_focus_date_and_renders_delivery_failure_without_lifecycle() -> (
    None
):
    context, decisions, learnings, dream, feature, sessions = _services()
    delivery = MagicMock()
    delivery.list = AsyncMock(side_effect=RuntimeError("private provider token"))
    loader = make_session_briefing_loader(
        context,
        decisions,
        learnings,
        dream,
        feature,
        sessions,
        delivery_svc=delivery,
    )

    with patch(
        "brain_v42.mcp.tools.session_tools.get_settings",
        return_value=SimpleNamespace(graph_enabled=False, brain_dream_cross_project_enabled=False),
    ):
        output = await loader("brain-v42", uuid4())

    delivery.list.assert_awaited_once_with(actor_project="brain-v42", limit=5)
    sessions.recent_checkpoints.assert_awaited_once()
    assert not hasattr(sessions, "start") or not sessions.start.called
    assert "Focus écrit :" in output
    assert "### Livraison" in output and "indisponible" in output.lower()
    assert "private provider token" not in output
