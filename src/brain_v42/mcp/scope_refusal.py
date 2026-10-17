"""Emit the same request-scoped refusal from authorization and business guards."""

from fastmcp.server.dependencies import get_access_token

from brain_v42.credentials.reasons import emit_refusal
from brain_v42.mcp.client_principal import resolve_client_principal
from brain_v42.provenance import get_current_actor, get_current_peer


def emit_scope_refusal(
    *, tool: str | None = None, path: str | None = None, reason: str = "family_denied"
) -> None:
    principal = resolve_client_principal(get_access_token())
    emit_refusal(
        reason,
        status=403,
        client_id=None if principal is None else principal.client_id,
        declared_agent=get_current_actor(),
        peer=get_current_peer(),
        tool=tool,
        path=path,
    )
