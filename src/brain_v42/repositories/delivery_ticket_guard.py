"""Delivery decisions inside the canonical ticket transition transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import delivery_artifact_bindings, delivery_workflows
from brain_v42.delivery_config import DeliverySettings
from brain_v42.models.delivery import DeliveryError
from brain_v42.models.delivery_evaluator import evaluate_delivery
from brain_v42.models.ticket import SELF_TRANSITIONS, TRANSITIONS, TicketAction, TicketStatus
from brain_v42.repositories.pg_delivery import PgDeliveryRepo


@dataclass(frozen=True, slots=True)
class DeliveryTransitionMutation:
    """Apply only after the ticket CAS succeeds, while retaining the decision locks."""

    disposition: Literal["active", "fulfilled", "cancelled", "wontfix"]
    reopen: bool = False

    async def apply(self, session: AsyncSession, ticket_id: UUID) -> None:
        values: dict[str, Any] = {
            "disposition": self.disposition,
            "row_version": delivery_workflows.c.row_version + 1,
            "updated_at": sa.func.now(),
        }
        if self.reopen or self.disposition != "active":
            values.update(
                claim_owner=None,
                claim_kind=None,
                claim_digest=None,
                claim_expires_at=None,
                claim_epoch=delivery_workflows.c.claim_epoch + 1,
            )
        if self.reopen:
            values.update(
                attempt=delivery_workflows.c.attempt + 1,
                context_row_version=delivery_workflows.c.context_row_version + 1,
                context_due_at=sa.func.clock_timestamp(),
                latest_context_success_confirmation_id=None,
                latest_context_attempt_confirmation_id=None,
                context_last_attempt_at=None,
                context_last_attempt_outcome=None,
                context_last_error_code=None,
                context_last_success_at=None,
            )
            await session.execute(
                delivery_artifact_bindings.update()
                .where(
                    delivery_artifact_bindings.c.ticket_id == ticket_id,
                    delivery_artifact_bindings.c.active.is_(True),
                )
                .values(active=False)
            )
        await session.execute(
            delivery_workflows.update()
            .where(delivery_workflows.c.ticket_id == ticket_id)
            .values(**values)
        )


async def guard_delivery_transition(
    session: AsyncSession,
    ticket_id: UUID,
    *,
    action: TicketAction | str | None,
    actor_project: str | None,
    expected_status: TicketStatus,
    new_status: TicketStatus,
    settings: DeliverySettings,
) -> DeliveryTransitionMutation | None:
    """Lock graph/tickets/workflows/sources before examining contract existence."""
    try:
        locked = await PgDeliveryRepo().load_locked_decision_inputs(
            session,
            ticket_id,
            feature_enabled=settings.enabled,
            freshness_seconds=settings.freshness_seconds,
        )
    except DeliveryError as error:
        if error.code == "ticket_not_found":
            return None  # Preserve the legacy repository's missing-row CAS result.
        raise
    inputs = locked.inputs
    if inputs is None:
        return None
    ticket = locked.ticket
    if action is None or actor_project is None:
        raise DeliveryError(
            "delivery_transition_required", "contracted transitions require an action and actor"
        )
    if ticket.status != expected_status:
        raise DeliveryError("delivery_transition_conflict", "ticket changed; reload and retry")
    try:
        act = TicketAction(action)
    except (ValueError, TypeError):
        raise DeliveryError("delivery_transition_invalid", "invalid ticket action") from None
    key = (ticket.kind, ticket.status, act)
    self_ticket = ticket.from_project == ticket.to_project
    if self_ticket:
        canonical_status = SELF_TRANSITIONS.get(key)
        expected_actor = ticket.from_project
    else:
        rule = TRANSITIONS.get(key)
        canonical_status = rule[1] if rule else None
        expected_actor = (
            ticket.to_project if rule and rule[0] == "executor" else ticket.from_project
        )
    if canonical_status is None or canonical_status != new_status:
        raise DeliveryError(
            "delivery_transition_invalid", "action does not allow the requested ticket status"
        )
    if actor_project != expected_actor:
        raise DeliveryError("delivery_not_allowed", "action is reserved to the ticket participant")
    if act is TicketAction.REOPEN:
        return DeliveryTransitionMutation("active", reopen=True)
    if act is TicketAction.CANCEL:
        return DeliveryTransitionMutation("cancelled")
    if act is TicketAction.WONTFIX or ticket.status is TicketStatus.WONTFIX:
        return DeliveryTransitionMutation("wontfix")
    if act in {TicketAction.RESOLVE, TicketAction.RESOLVE_PENDING, TicketAction.CONFIRM}:
        completion_action = f"{'self' if self_ticket else 'cross'}_{act.value}"
        assessment = evaluate_delivery(
            inputs.model_copy(update={"requested_completion_action": completion_action}),
            now=locked.decision_time,
        )
        if not assessment.completion_eligible_now:
            raise DeliveryError(
                "delivery_requirements_unsatisfied",
                "current delivery proof does not permit completion",
            )
        if new_status is TicketStatus.CLOSED:
            return DeliveryTransitionMutation("fulfilled")
    return DeliveryTransitionMutation("active")
