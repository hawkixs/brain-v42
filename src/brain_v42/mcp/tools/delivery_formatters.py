"""Safe, bounded French renderers for read-only delivery facts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from brain_v42.models.delivery import DeliveryPage, context_reference_identity

_ROWS_CAP = 5
_TEXT_CAP = 160


def _bounded(value: object, *, cap: int = _TEXT_CAP) -> str:
    """Keep prose fields useful without echoing arbitrary stored payloads."""
    text = " ".join(str(value).split())
    return text if len(text) <= cap else f"{text[: cap - 1].rstrip()}…"


def _when(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value is not None else "aucune"


def _latest(values: tuple[Any, ...], field: str) -> datetime | None:
    times = [getattr(value, field, None) for value in values]
    return max((value for value in times if isinstance(value, datetime)), default=None)


def _receipt_id(receipt: Any | None) -> str:
    return str(receipt.id) if receipt is not None else "aucun"


def _contexts(view: Any) -> str:
    all_values = tuple(getattr(view, "contexts", ()) or ())
    direct_required = tuple(value for value in all_values if getattr(value, "required", False))
    if direct_required:
        values = direct_required
    else:
        references = tuple(getattr(view.contract, "context_refs", ()) or ())
        required_identities = {
            context_reference_identity(reference)
            for reference in references
            if getattr(reference, "required", False)
        }
        values = tuple(
            value for value in all_values if value.reference_identity in required_identities
        )
    if not values:
        return "aucun contexte requis"
    rendered = []
    for context in values[:2]:
        rendered.append(
            f"{_bounded(context.reference_identity, cap=80)}={context.status} "
            f"(tentative:{_when(context.last_attempt_at)}, succès:{_when(context.last_success_at)})"
        )
    remainder = len(values) - len(rendered)
    if remainder:
        rendered.append(f"+{remainder}")
    return "; ".join(rendered)


def _row(view: Any) -> str:
    assessment = view.assessment
    blockers = tuple(getattr(assessment, "blockers", ()) or ())
    work = tuple(getattr(assessment, "eligible_work", ()) or ())
    blocker = blockers[0].code if blockers else "aucun"
    eligible = f"{work[0].kind}/{work[0].role}" if work else "aucun"
    attempt = _latest(tuple(getattr(view, "bindings", ()) or ()), "last_attempt_at")
    success = _latest(tuple(getattr(view, "bindings", ()) or ()), "last_success_at")
    return (
        f"- ticket={view.contract.ticket_id} | révision={view.contract.contract_revision} | "
        f"assessment={assessment.assessment_id} | "
        f"digest={assessment.delivery_digest} | fraîcheur={assessment.observation_health} | "
        f"étape={assessment.delivery_stage} | acceptation={assessment.acceptance_state} | "
        f"reçus=integration:{_receipt_id(view.integration_receipt)},fulfilled:{_receipt_id(view.fulfillment_receipt)} | "
        f"dernière tentative={_when(attempt)} | dernier succès={_when(success)} | "
        f"contextes requis={_contexts(view)} | blocage={_bounded(blocker, cap=100)} | "
        f"travail={_bounded(eligible, cap=100)}"
    )


def format_delivery_briefing(page: DeliveryPage) -> str:
    """Render up to five current Delivery reads without fetching any provider data."""
    items = tuple(page.items)
    shown = items[:_ROWS_CAP]
    lines = ["### Livraison"]
    if not shown:
        lines.append("- aucune livraison visible")
    else:
        lines.extend(_row(view) for view in shown)
    locally_hidden = len(items) - len(shown)
    total_omitted = page.omitted_count + locally_hidden
    if total_omitted:
        lines.append(
            f"- {total_omitted} livraisons omises au total "
            f"({page.omitted_count} non retournés par la page; "
            f"{locally_hidden} masqués par le cap) — brain_delivery_list pour la liste complète"
        )
    return "\n".join(lines)
