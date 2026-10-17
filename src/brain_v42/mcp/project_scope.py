"""Reviewed argument and persisted-row project scope policy."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from pydantic import TypeAdapter, ValidationError

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
ROW_SOURCES: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        **dict.fromkeys(
            (
                "brain_session_resume",
                "brain_session_capture",
                "brain_session_checkpoint",
                "brain_session_heartbeat",
                "brain_session_bind",
                "brain_session_end",
                "brain_session_relay",
                "brain_session_abandon",
            ),
            ("session", "session_id"),
        ),
        "brain_slot_close": ("slot", "slot_id"),
        "brain_claim_verify": ("claim", "claim_id"),
        "brain_update": ("entity", "entity_id"),
        "brain_supersede_decision": ("decision", "old_decision_id"),
        "brain_validate_learning": ("learning", "learning_id"),
        "brain_use_snippet": ("snippet", "snippet_id"),
        "brain_execute_runbook": ("runbook_prefix", "runbook_id"),
        "brain_refresh_entity": ("entity", "entity_id"),
    }
)
KNOWLEDGE_ROW_TOOLS = frozenset(
    {
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
_BOOL = TypeAdapter(bool)


def _relay_override(arguments: Mapping[str, object]) -> bool:
    if arguments.get("initiator") == "operator":
        return True
    try:
        return _BOOL.validate_python(arguments.get("allow_focus_shrink", False))
    except ValidationError:
        return False  # The handler's argument validation refuses malformed booleans.


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
    row_projects: Sequence[str | None] | None = None,
) -> ScopeDecision | None:
    """Check every declared target and the independently resolved actor project."""
    if (
        principal is None
        or principal.allowed_projects is None
        or tool in SCOPE_EXEMPT
        or tool not in SCOPE_ARGUMENTS
        and tool not in ACTOR_REQUIRED
        and tool not in ROW_SOURCES
    ):
        return None
    allowed_projects = principal.allowed_projects
    if tool == "brain_session_relay" and _relay_override(arguments):
        return ScopeDecision("scope_mismatch", "relay_override_refused")
    projects: list[str] = []
    if tool in ROW_SOURCES:
        projects.extend(project for project in row_projects or () if project is not None)
    if tool == "brain_update":
        fields = arguments.get("fields")
        if isinstance(fields, Mapping) and "project_key" in fields:
            target = fields["project_key"]
            if not isinstance(target, str):
                return ScopeDecision("scope_unresolved", "null_project_refused")
            try:
                projects.append(canonicalize_project_key(target))
            except ValueError:
                return ScopeDecision("scope_unresolved", "scope_denied")
    if tool == "brain_supersede_decision":
        target = arguments.get("project_key")
        if not isinstance(target, str):
            return ScopeDecision("scope_unresolved", "null_project_refused")
        try:
            projects.append(canonicalize_project_key(target))
        except ValueError:
            return ScopeDecision("scope_unresolved", "scope_denied")
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
    if tool in ROW_SOURCES and (
        not row_projects
        or len(row_projects) != (2 if tool == "brain_claim_verify" else 1)
        or any(project is None for project in row_projects)
    ):
        return ScopeDecision("scope_unresolved", "scope_denied")
    return None
