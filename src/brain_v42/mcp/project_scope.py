"""Reviewed direct-project scope policy; persisted ownership resolution is PR4."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from brain_v42.mcp.client_principal import ClientPrincipal
from brain_v42.models.project_key import canonicalize_project_key

SCOPE_ARGUMENTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "brain_learn": ("project_key",),
        "brain_log_decision": ("project_key",),
        "brain_save_snippet": ("project_key",),
        "brain_create_runbook": ("project_key",),
        "brain_propose_adr": ("project_key",),
        "brain_session_start": ("project_key",),
        "brain_slot_open": ("project_key",),
        "brain_ticket_create": ("from_project",),
        "brain_ticket_reply": ("author_project",),
        "brain_ticket_transition": ("author_project",),
        "brain_ticket_plan": ("author_project",),
        "brain_delivery_contract_set": ("actor_project",),
        "brain_delivery_bind_pr": ("actor_project",),
        "brain_delivery_refresh": ("actor_project",),
        "brain_delivery_claim": ("actor_project",),
        "brain_delivery_claim_renew": ("actor_project",),
        "brain_delivery_claim_release": ("actor_project",),
        "brain_delivery_accept": ("actor_project",),
        "brain_delivery_attest": ("actor_project",),
        "brain_programme_gate_upsert": ("owner",),
    }
)
ACTOR_REQUIRED = frozenset(
    {
        "brain_programme_upsert",
        "brain_programme_gate_upsert",
        "brain_programme_gate_report",
        "brain_programme_request",
    }
)
DEFERRED_TO_PR4 = frozenset(
    {
        "brain_session_resume",
        "brain_session_capture",
        "brain_session_checkpoint",
        "brain_session_heartbeat",
        "brain_session_bind",
        "brain_session_end",
        "brain_session_relay",
        "brain_session_abandon",
        "brain_slot_close",
        "brain_claim_verify",
        "brain_update",
        "brain_supersede_decision",
        "brain_validate_learning",
        "brain_use_snippet",
        "brain_execute_runbook",
        "brain_refresh_entity",
    }
)
SCOPE_EXEMPT = frozenset(
    {
        "brain_session_list",
        "brain_delivery_get",
        "brain_delivery_list",
        "brain_delivery_attestation_list",
    }
)


@dataclass(frozen=True, slots=True)
class ScopeDecision:
    """Carry only bounded observations, never project values or arguments."""

    cause: str
    reason: str


def scope_decision(
    tool: str,
    arguments: Mapping[str, object],
    principal: ClientPrincipal | None,
    actor_project: str | None,
) -> ScopeDecision | None:
    """Check every declared target and the independently resolved actor project."""
    if (
        principal is None
        or principal.allowed_projects is None
        or tool in SCOPE_EXEMPT
        or tool not in SCOPE_ARGUMENTS
        and tool not in ACTOR_REQUIRED
        and tool not in DEFERRED_TO_PR4
    ):
        return None
    allowed_projects = principal.allowed_projects
    projects: list[str] = []
    for argument in SCOPE_ARGUMENTS.get(tool, ()):
        value = arguments.get(argument)
        optional = tool == "brain_programme_gate_upsert" and argument == "owner"
        if optional and value is None:
            continue
        if not isinstance(value, str):
            return ScopeDecision(
                "scope_unresolved", "scope_denied" if optional else "null_project_refused"
            )
        try:
            projects.append(canonicalize_project_key(value))
        except ValueError:
            return ScopeDecision("scope_unresolved", "scope_denied")
    if actor_project is not None:
        try:
            projects.append(canonicalize_project_key(actor_project))
        except ValueError:
            return ScopeDecision("scope_unresolved", "scope_denied")
    elif tool in ACTOR_REQUIRED:
        return ScopeDecision("scope_unresolved", "null_project_refused")
    if any(project not in allowed_projects for project in projects):
        return ScopeDecision("scope_mismatch", "scope_denied")
    return None
