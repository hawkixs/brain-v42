"""Adapt registry identities to FastMCP and refuse before its HTTP body reader."""

import hmac
import re
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Literal

import structlog
from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from brain_v42.credentials.agent_unresolved import count_agent_unresolved
from brain_v42.credentials.reasons import TRANSPORT_STATUSES, emit_refusal
from brain_v42.credentials.redact import short_id
from brain_v42.credentials.verifier import CredentialRefused, CredentialVerifier, VerifiedPrincipal
from brain_v42.mcp.http_security import PUBLIC_HTTP_PATHS, public_probe_scope
from brain_v42.models.project_key import canonicalize_project_key
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
_SESSION_LIMIT_PER_CLIENT = 256
_logger = structlog.get_logger(__name__)
# Sessions parked outside a project repeat the same warning on every call.
# Stop at a hard cap so varying declared agents cannot grow a process-wide memo.
_seen_unresolved_agents: set[tuple[str, str, str]] = set()
_MAX_UNRESOLVED_AGENTS_TRACKED = 64


def _matches_issuer(raw: str, pattern: str) -> bool:
    """Authorize raw headers with only the grammar supported by the issuing CLI."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9.:-]{0,63}\*?", pattern):
        return False
    return raw.startswith(pattern[:-1]) if pattern.endswith("*") else raw == pattern


def _fallback_unresolved_project_actor(
    client_id: str, agent: str, reason: Literal["not_kebab", "unknown_project"]
) -> str:
    """Option B: retain access under the client actor while surfacing lost project attribution."""
    count_agent_unresolved(reason)
    key = (client_id, agent[:64], reason)
    if (
        key not in _seen_unresolved_agents
        and len(_seen_unresolved_agents) < _MAX_UNRESOLVED_AGENTS_TRACKED
    ):
        _seen_unresolved_agents.add(key)
        _logger.warning(
            "mcp_auth.agent_unresolved", client_id=client_id, agent=key[1], reason=reason
        )
    return client_id


def _project_actor(
    raw: str, verifier: CredentialVerifier, client_id: str
) -> tuple[str, str | None]:
    """Existence in the credential snapshot, rather than syntax, grants project actors."""
    agent = normalize_agent(raw)
    try:
        key = canonicalize_project_key(agent, strict=True)
    except ValueError:
        return _fallback_unresolved_project_actor(client_id, agent, "not_kebab"), None
    if verifier.project_exists(key):
        return key, key
    return _fallback_unresolved_project_actor(client_id, agent, "unknown_project"), None


class CredentialTokenVerifier(TokenVerifier):
    """Expose verified claims without retaining bearer material in SDK objects."""

    def __init__(self, verifier: CredentialVerifier) -> None:
        super().__init__()
        self.verifier = verifier

    def get_middleware(self) -> list[Middleware]:
        # FastMCP prepends provider middleware ahead of the transport guards.
        # CredentialGuard installs these INSIDE its verified request context.
        return []

    def verified_request_middleware(self) -> list[Middleware]:
        """Keep the SDK auth integration behind the sole credential boundary."""
        return super().get_middleware()

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
        self._route_app = app
        self.app = app
        for middleware in reversed(CredentialTokenVerifier(verifier).verified_request_middleware()):
            self.app = middleware.cls(self.app, *middleware.args, **middleware.kwargs)
        self.verifier = verifier
        # Only server-minted IDs enter these maps. Each client can evict only
        # its own sessions, with fixed-size keys and a separate LRU quota.
        self._session_owners: dict[bytes, str] = {}
        self._client_sessions: dict[str, OrderedDict[bytes, None]] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") in PUBLIC_HTTP_PATHS:
            await self.app(public_probe_scope(scope), receive, send)
            return
        if scope.get("path") == "/admin/elevations" and scope.get("method") == "POST":
            # This route verifies its own elevation-only bearer, outside SDK auth.
            await self._route_app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        authorization = headers.getlist("authorization")
        token = None
        if len(authorization) == 1:
            scheme, separator, candidate = authorization[0].partition(" ")
            if (
                scheme.lower() == "bearer"
                and separator
                and candidate
                and not any(character.isspace() for character in candidate)
            ):
                token = candidate
        declared_agents = headers.getlist("x-brain-agent")
        declared_agent = declared_agents[0] if declared_agents else None
        principal = None
        session_id = headers.get("mcp-session-id")
        session_key = None if session_id is None else sha256(session_id.encode()).digest()
        owner = None
        try:
            if token is None:
                raise CredentialRefused("missing_token")
            principal = await self.verifier.verify(token)
            if session_key is not None:
                owner = self._session_owners.get(session_key)
                if owner != principal.client_id:
                    raise CredentialRefused("foreign_client_attach")
            actor = principal.client_id if declared_agent is None else declared_agent
            actor_project = None
            # Authorize the raw value before normalizing/truncating provenance.
            if len(declared_agents) > 1:
                raise CredentialRefused("agent_mismatch")
            if actor != principal.client_id and not any(
                _matches_issuer(actor, pattern)
                for pattern in principal.issuers
                if pattern != "@project"
            ):
                if "@project" not in principal.issuers:
                    raise CredentialRefused("agent_mismatch")
                actor, actor_project = _project_actor(actor, self.verifier, principal.client_id)
        except CredentialRefused as exc:
            status = TRANSPORT_STATUSES[exc.reason]
            client = scope.get("client")
            emit_refusal(
                exc.reason,
                status=status,
                client_id=None
                if principal is None or exc.reason == "foreign_client_attach"
                else principal.client_id,
                requesting_client_id=principal.client_id
                if principal is not None and exc.reason == "foreign_client_attach"
                else None,
                owner_client_id=owner if exc.reason == "foreign_client_attach" else None,
                session_id=short_id(session_id) if exc.reason == "foreign_client_attach" else None,
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
        client_id = principal.client_id
        # No await between ownership validation and LRU/DELETE bookkeeping.
        if session_key is not None:
            sessions = self._client_sessions[client_id]
            if scope.get("method") == "DELETE":
                del self._session_owners[session_key]
                del sessions[session_key]
                if not sessions:
                    del self._client_sessions[client_id]
            else:
                sessions.move_to_end(session_key)

        async def bind_minted_session(message: Message) -> None:
            if message["type"] == "http.response.start":
                minted_id = Headers(raw=message.get("headers", [])).get("mcp-session-id")
                if minted_id is not None:
                    minted_key = sha256(minted_id.encode()).digest()
                    if minted_key not in self._session_owners:
                        sessions = self._client_sessions.setdefault(client_id, OrderedDict())
                        self._session_owners[minted_key] = client_id
                        sessions[minted_key] = None
                        if len(sessions) > _SESSION_LIMIT_PER_CLIENT:
                            evicted_key, _ = sessions.popitem(last=False)
                            del self._session_owners[evicted_key]
            # Bind before exposing the minted ID to the client.
            await send(message)

        state = scope.setdefault("state", {})
        state["brain_principal"] = principal.client_id
        state["brain_actor"] = normalize_agent(actor)
        if actor_project is None:
            try:
                key = canonicalize_project_key(state["brain_actor"], strict=True)
                actor_project = key if self.verifier.project_exists(key) else None
            except Exception:
                # This is trace attribution, not authorization: a registry error
                # must not refuse an actor already accepted by its issuer pattern.
                actor_project = None
        state["brain_actor_project"] = actor_project
        previous = get_current_principal()
        previous_actor = get_current_actor()
        verification_context = _request_verification.set(
            _RequestVerification(self.verifier, sha256(token.encode()).digest(), principal)
        )
        set_current_principal(principal.client_id)
        set_current_actor(state["brain_actor"])
        try:
            await self.app(scope, receive, bind_minted_session if session_id is None else send)
        finally:
            set_current_principal(previous)
            set_current_actor(previous_actor)
            _request_verification.reset(verification_context)
