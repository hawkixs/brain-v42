"""Adapt registry identities to FastMCP and refuse before its HTTP body reader."""

import hmac
from contextvars import ContextVar
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from hashlib import sha256

from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from brain_v42.credentials.reasons import TRANSPORT_STATUSES, emit_refusal
from brain_v42.credentials.verifier import CredentialRefused, CredentialVerifier, VerifiedPrincipal
from brain_v42.provenance import (
    get_current_actor,
    get_current_principal,
    normalize_agent,
    set_current_actor,
    set_current_principal,
)


@dataclass(frozen=True, slots=True)
class _RequestVerification:
    verifier: CredentialVerifier = field(repr=False)
    fingerprint: bytes = field(repr=False)
    principal: VerifiedPrincipal


_request_verification: ContextVar[_RequestVerification | None] = ContextVar(
    "brain_v42_request_verification", default=None
)


class CredentialTokenVerifier(TokenVerifier):
    """Expose verified claims without retaining bearer material in SDK objects."""

    def __init__(self, verifier: CredentialVerifier) -> None:
        super().__init__()
        self.verifier = verifier

    async def verify_token(self, token: str) -> AccessToken | None:
        cached = _request_verification.get()
        try:
            if (
                cached is not None
                and cached.verifier is self.verifier
                and len(token) <= 4096
                and hmac.compare_digest(cached.fingerprint, sha256(token.encode()).digest())
            ):
                principal = cached.principal
            else:
                principal = await self.verifier.verify(token)
        except UnicodeEncodeError:
            return None
        except CredentialRefused:
            # The outer guard owns the transport observation, including its peer.
            return None
        return AccessToken(
            token="",
            client_id=principal.client_id,
            scopes=sorted(principal.families),
            expires_at=None
            if principal.expires_at is None
            else int(principal.expires_at.timestamp()),
            claims={
                "credential_id": str(principal.credential_id),
                "issuers": sorted(principal.issuers),
            },
        )


class CredentialGuard:
    """Own HTTP refusals and isolate the verified client to the current request."""

    def __init__(self, app: ASGIApp, *, verifier: CredentialVerifier) -> None:
        self.app = app
        self.verifier = verifier

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        authorization = headers.getlist("authorization")
        token = None
        if len(authorization) == 1 and authorization[0].lower().startswith("bearer "):
            token = authorization[0][7:]
        declared_agents = headers.getlist("x-brain-agent")
        declared_agent = declared_agents[0] if declared_agents else None
        principal = None
        try:
            principal = await self.verifier.verify(token)
            actor = principal.client_id if declared_agent is None else declared_agent
            # Authorize the raw value before normalizing/truncating provenance.
            if len(declared_agents) > 1 or (
                actor != principal.client_id
                and not any(fnmatchcase(actor, pattern) for pattern in principal.issuers)
            ):
                raise CredentialRefused("agent_mismatch")
        except CredentialRefused as exc:
            status = TRANSPORT_STATUSES[exc.reason]
            client = scope.get("client")
            emit_refusal(
                exc.reason,
                status=status,
                client_id=None if principal is None else principal.client_id,
                declared_agent=declared_agent,
                peer=None if client is None else client[0],
                path=scope.get("path"),
            )
            await JSONResponse(
                {"error": exc.reason},
                status_code=status,
                headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
            )(scope, receive, send)
            return
        assert token is not None  # A missing bearer cannot produce a verified principal.
        previous = get_current_principal()
        previous_actor = get_current_actor()
        verification_context = _request_verification.set(
            _RequestVerification(self.verifier, sha256(token.encode()).digest(), principal)
        )
        set_current_principal(principal.client_id)
        set_current_actor(normalize_agent(actor))
        try:
            await self.app(scope, receive, send)
        finally:
            set_current_principal(previous)
            set_current_actor(previous_actor)
            _request_verification.reset(verification_context)
