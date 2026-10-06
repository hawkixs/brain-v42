"""Parse A3's verified SDK claims without carrying credential material or actor state."""

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from fastmcp.server.auth import AccessToken

from brain_v42.credentials.families import STORABLE_FAMILIES


@dataclass(frozen=True, slots=True)
class ClientPrincipal:
    """Keep rights separate from the validated, declared provenance actor."""

    client_id: str
    families: frozenset[str]
    issuers: tuple[str, ...]


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
        or set(claims) != {"credential_id", "issuers"}
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
    return ClientPrincipal(client_id, frozenset(scopes), tuple(issuers))
