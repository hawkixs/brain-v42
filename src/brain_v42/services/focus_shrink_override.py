"""Keep destructive focus overrides tied to a verified operator elevation."""

from datetime import UTC, datetime

from brain_v42.models.focus_slot import FocusSlotError
from brain_v42.provenance import get_current_principal, get_current_transport
from brain_v42.repositories.pg_client_credentials import PgClientCredentialRepo


async def focus_shrink_override_allowed() -> bool:
    """A declared actor or a shared token cannot stand in for /brain-admin."""
    principal = get_current_principal()
    connection_id = get_current_transport()
    if not principal or not connection_id:
        return False
    try:
        return await PgClientCredentialRepo().has_active_elevation(
            connection_id, principal, datetime.now(UTC)
        )
    except Exception:
        # Repository failures grant no right and expose no private failure text.
        return False


async def require_focus_shrink_override() -> None:
    """Refuse before capture absorption, closure, or any focus mutation."""
    if not await focus_shrink_override_allowed():
        raise FocusSlotError(
            "focus_shrink_override_refused",
            "allow_focus_shrink requires a /brain-admin elevation or an operator relay",
        )
