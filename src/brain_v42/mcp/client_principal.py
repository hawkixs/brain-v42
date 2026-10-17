"""Parse A3's verified SDK claims without carrying credential material or actor state."""

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import get_access_token

from brain_v42.credentials.families import STORABLE_FAMILIES
from brain_v42.models.project_key import canonicalize_project_key


@dataclass(frozen=True, slots=True)
class ClientPrincipal:
    """Keep rights separate from the validated, declared provenance actor."""

    client_id: str
    families: frozenset[str]
    issuers: tuple[str, ...]
    allowed_projects: tuple[str, ...] | None = None


def current_author_client_id() -> str | None:
    """Attribute new rows only to a credential verified for the current request."""
    principal = resolve_client_principal(get_access_token())
    return principal.client_id if principal is not None else None


def resolve_client_principal(access_token: AccessToken | None) -> ClientPrincipal | None:
    """Accept only the credential verifier's shape; malformed claims grant nothing."""
    if access_token is None:
        return None
    claims = access_token.claims
    client_id = access_token.client_id
    scopes = access_token.scopes
    if (
        not isinstance(client_id, str)
        or not client_id.strip()
        or not isinstance(claims, Mapping)
        or set(claims)
        not in ({"credential_id", "issuers"}, {"credential_id", "issuers", "allowed_projects"})
        or not isinstance(scopes, (list, tuple))
        or any(not isinstance(scope, str) or scope not in STORABLE_FAMILIES for scope in scopes)
    ):
        return None
    credential_id = claims["credential_id"]
    if not isinstance(credential_id, str):
        return None
    try:
        UUID(credential_id)
    except ValueError:
        return None
    issuers = claims["issuers"]
    if not isinstance(issuers, (list, tuple)) or any(
        not isinstance(issuer, str) or not issuer.strip() for issuer in issuers
    ):
        return None
    projects = claims.get("allowed_projects")
    if projects is not None:
        if (
            not isinstance(projects, (list, tuple))
            or not projects
            or any(not isinstance(key, str) or not 1 <= len(key) <= 50 for key in projects)
            or len(set(projects)) != len(projects)
        ):
            return None
        try:
            if any(canonicalize_project_key(key) != key for key in projects):
                return None
        except ValueError:
            return None
    return ClientPrincipal(
        client_id, frozenset(scopes), tuple(issuers), None if projects is None else tuple(projects)
    )
