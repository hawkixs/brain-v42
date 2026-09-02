"""Shared capability policy for Codex Dream phases."""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Never, Self

import mcp.types as mt
import structlog
from fastmcp.exceptions import AuthorizationError
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool, ToolResult
from pydantic import ConfigDict, Field, SecretStr, field_serializer, model_validator

from brain_v42.mcp.dream_project_authorization import (
    DreamProjectAudit,
    DreamProjectReferenceResolver,
    authorize_dream_project_request,
    bind_dream_project_scope,
)
from brain_v42.models.project_key import canonicalize_project_key
from brain_v42.provenance import get_current_actor

logger = structlog.get_logger(__name__)

DREAM_PHASE_TOOL_ALLOWLISTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "scan": (
            "brain_decay_status",
            "brain_consolidation_candidates",
            "brain_list",
            "brain_search",
        ),
        "clean": (
            "brain_search",
            "brain_get",
            "brain_consolidation_candidates",
            "brain_decay_status",
            "brain_merge_entities",
            "brain_delete",
            "brain_list",
        ),
        "connect": (
            "brain_backfill_links_batch",
            "brain_list_orphans_for_classification",
            "brain_assign_domain",
        ),
        "synth": (
            "brain_get_clusters",
            "brain_get",
            "brain_learn",
            "brain_save_snippet",
            "brain_search",
            "brain_list",
            "brain_get_neighbors",
            "brain_graph_path",
        ),
        "promote": (
            "brain_get",
            "brain_search",
            "brain_propose_adr",
            "brain_create_runbook",
            "brain_list_adrs",
            "brain_list",
            "brain_get_neighbors",
            "brain_graph_path",
        ),
        "reorg": (
            "brain_search",
            "brain_list",
            "brain_get",
            "brain_update",
        ),
    }
)

_SAFE_AUDIT_TOOL_NAMES = frozenset(
    {tool_name for phase_tools in DREAM_PHASE_TOOL_ALLOWLISTS.values() for tool_name in phase_tools}
    | {"brain_call_tool", "brain_find_tool"}
)


def dream_phase_tool_allowlist(phase: str) -> tuple[str, ...]:
    """Return the immutable MCP tool allowlist for a supported Dream phase."""
    try:
        return DREAM_PHASE_TOOL_ALLOWLISTS[phase]
    except KeyError:
        raise ValueError(f"unsupported Dream phase: {phase}") from None


def _audit_tool_name(tool_name: str) -> str:
    return tool_name if tool_name in _SAFE_AUDIT_TOOL_NAMES else "<redacted>"


type DreamCapabilityPrincipalKind = Literal["unscoped", "admin", "scoped", "invalid"]


@dataclass(frozen=True, slots=True)
class DreamCapabilityPrincipal:
    """Secret-free authorization view of the current FastMCP principal."""

    kind: DreamCapabilityPrincipalKind
    client_id: str
    phase: str | None = None
    project_key: str | None = None


def _safe_claim_phase(value: object) -> str | None:
    return value if isinstance(value, str) and value in DREAM_PHASE_TOOL_ALLOWLISTS else None


def _safe_claim_project_key(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        canonical = canonicalize_project_key(value)
    except ValueError:
        return None
    return value if canonical == value else None


def _safe_client_id(value: object, phase: str | None) -> str:
    if value == "brain-admin":
        return "brain-admin"
    expected = f"dream-codex-{phase}" if phase is not None else None
    return value if isinstance(value, str) and value == expected else "invalid"


def resolve_dream_capability_principal(
    access_token: AccessToken | None,
) -> DreamCapabilityPrincipal:
    """Resolve verifier claims into a strict, secret-free authorization principal."""
    if access_token is None:
        return DreamCapabilityPrincipal(kind="unscoped", client_id="unscoped")

    claims = access_token.claims
    claim_type = claims.get("type") if isinstance(claims, Mapping) else None
    phase = _safe_claim_phase(claims.get("phase")) if isinstance(claims, Mapping) else None
    project_key = (
        _safe_claim_project_key(claims.get("project_key")) if isinstance(claims, Mapping) else None
    )
    client_id = _safe_client_id(access_token.client_id, phase)

    if (
        claim_type == "admin"
        and client_id == "brain-admin"
        and tuple(access_token.scopes) == ("brain:admin",)
        and set(claims) == {"type"}
    ):
        return DreamCapabilityPrincipal(kind="admin", client_id=client_id)

    expected_agent = f"dream-codex-{phase}" if phase is not None else None
    if (
        claim_type == "scoped"
        and phase is not None
        and project_key is not None
        and client_id == expected_agent
        and claims.get("agent") == expected_agent
        and tuple(access_token.scopes) == ("brain:dream",)
        and set(claims) == {"type", "agent", "phase", "project_key"}
    ):
        return DreamCapabilityPrincipal(
            kind="scoped",
            client_id=client_id,
            phase=phase,
            project_key=project_key,
        )

    return DreamCapabilityPrincipal(
        kind="invalid",
        client_id=client_id,
        phase=phase,
        project_key=project_key,
    )


def _deny_capability_request(
    *,
    principal: DreamCapabilityPrincipal,
    tool_name: str,
    reason: str,
) -> Never:
    logger.warning(
        "dream_capability.authorization_denied",
        principal=principal.client_id,
        phase=principal.phase or "unknown",
        project_key=principal.project_key or "unknown",
        requested_tool=_audit_tool_name(tool_name),
        reason=reason,
    )
    raise AuthorizationError("Dream capability authorization denied")


class DreamCapabilityMiddleware(Middleware):
    """Enforce phase capabilities after listing and before tool execution."""

    def __init__(
        self,
        *,
        project_resolver: DreamProjectReferenceResolver | None = None,
    ) -> None:
        self._project_resolver = project_resolver

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """Keep admin/STDIO catalogs intact and bound scoped catalogs by phase."""
        tools = await call_next(context)
        principal = resolve_dream_capability_principal(get_access_token())
        if principal.kind in {"unscoped", "admin"}:
            return tools
        if principal.kind == "invalid" or principal.phase is None:
            return ()
        allowed = frozenset(dream_phase_tool_allowlist(principal.phase))
        return tuple(tool for tool in tools if tool.name in allowed)

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Authorize phase and project scope before a handler can run."""
        principal = resolve_dream_capability_principal(get_access_token())
        if principal.kind in {"unscoped", "admin"}:
            return await call_next(context)

        tool_name = context.message.name
        if principal.kind != "scoped" or principal.phase is None or principal.project_key is None:
            _deny_capability_request(
                principal=principal,
                tool_name=tool_name,
                reason="invalid_principal",
            )
        if tool_name not in dream_phase_tool_allowlist(principal.phase):
            _deny_capability_request(
                principal=principal,
                tool_name=tool_name,
                reason="tool_not_allowed_for_phase",
            )

        self._observe_identity_divergence(principal, tool_name)

        authorized = await authorize_dream_project_request(
            tool_name=tool_name,
            arguments=context.message.arguments,
            project_key=principal.project_key,
            resolver=self._project_resolver,
            audit=DreamProjectAudit(
                principal=principal.client_id,
                phase=principal.phase,
            ),
        )
        authorized_message = context.message.model_copy(update={"arguments": authorized.arguments})
        authorized_context = context.copy(message=authorized_message)
        with bind_dream_project_scope(authorized.scope):
            return await call_next(authorized_context)

    @staticmethod
    def _observe_identity_divergence(
        principal: DreamCapabilityPrincipal,
        tool_name: str,
    ) -> None:
        """Count the calls whose TWO identities do not agree.

        Every dream call carries two, in separate namespaces: the TOKEN's —
        `client_id`, strictly verified, the capability bound rests on it — and
        the ACTOR's, the `X-Brain-Agent` header declared by the client and
        checked against nothing.

        This is not a scope bypass: the bound is on the token and it holds. It is
        an ATTRIBUTION defect, and it is STRUCTURAL — the registry only mints
        `dream-codex-*` profiles, while the fallback chain runs runners that
        announce `dream-agy-*` and `dream-claude-*`. Two rails out of three
        diverge on every call, by construction.

        WE REFUSE NOTHING. Refusing would break the fallback rail, that is, the
        night on which the main rail has already gone down. Deriving the actor
        from the token would erase "which rail actually ran", the one thing that
        made measuring the ratio possible. Logging produces the denominator the
        other two forms need, and there is NO other source: `access_log.actor` is
        drained every 300 s, nothing in journald carries the per-call actor, and
        `dream_runs` has no actor column.

        The `except` is TOTAL and tightly scoped, for the same reason as
        `ProvenanceMiddleware._report`: an observation channel cannot be a point
        of failure for the operation it observes. At worst, one line is missing.
        """
        try:
            actor = get_current_actor()
            if actor == principal.client_id:
                return
            logger.info(
                "dream_identity_divergence",
                actor=actor,
                token_client_id=principal.client_id,
                phase=principal.phase,
                project_key=principal.project_key,
                tool=_audit_tool_name(tool_name),
            )
        except Exception:  # noqa: BLE001 - observation only, never the call
            return


class DreamCapabilityConfigurationError(ValueError):
    """Secret-safe configuration error for the Dream capability registry."""


def _secret_token(value: object) -> SecretStr:
    if not isinstance(value, str) or not value.strip():
        raise DreamCapabilityConfigurationError(
            "invalid Dream capability registry: non-blank string token required"
        )
    if value in _SAFE_AUDIT_TOOL_NAMES:
        raise DreamCapabilityConfigurationError(
            "invalid Dream capability registry: reserved token value"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise DreamCapabilityConfigurationError(
            "invalid Dream capability registry: UTF-8 encodable token required"
        ) from None
    return SecretStr(value)


def _admin_secret(value: object) -> SecretStr:
    if not isinstance(value, str) or not value.strip():
        raise DreamCapabilityConfigurationError(
            "invalid Dream capability registry: admin token must be a non-blank UTF-8 string"
        )
    if value in _SAFE_AUDIT_TOOL_NAMES:
        raise DreamCapabilityConfigurationError(
            "invalid Dream capability registry: reserved token value"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise DreamCapabilityConfigurationError(
            "invalid Dream capability registry: admin token must be a non-blank UTF-8 string"
        ) from None
    return SecretStr(value)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DreamCapabilityConfigurationError(
                "invalid Dream capability registry: duplicate JSON key"
            )
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class DreamCapabilityProfile:
    """One immutable, secret-safe Dream capability profile."""

    project_key: str
    phase: str
    active: SecretStr = field(repr=False)
    accepted: tuple[SecretStr, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class DreamCapabilityRegistry:
    """Validated Dream profiles indexed by canonical project and phase."""

    profiles: Mapping[tuple[str, str], DreamCapabilityProfile] = field(repr=False)
    _admin_token: SecretStr = field(repr=False)

    def active_token_for(self, project_key: str, phase: str) -> SecretStr:
        """Return the deterministic outbound token for one Dream profile."""
        return self.profiles[(project_key, phase)].active


class _SecretSafeAccessToken(AccessToken):
    token: str = Field(repr=False)
    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def _freeze_collections(self) -> Self:
        object.__setattr__(self, "scopes", tuple(self.scopes))
        object.__setattr__(self, "claims", MappingProxyType(dict(self.claims)))
        return self

    @field_serializer("scopes")
    def _serialize_scopes(self, scopes: tuple[str, ...]) -> list[str]:
        return list(scopes)

    @field_serializer("claims")
    def _serialize_claims(self, claims: Mapping[str, Any]) -> dict[str, Any]:
        return dict(claims)


@dataclass(frozen=True, slots=True)
class _TokenPrincipal:
    token: SecretStr = field(repr=False)
    client_id: str
    scopes: tuple[str, ...]
    claims: Mapping[str, str]


def _token_principals(registry: DreamCapabilityRegistry) -> tuple[_TokenPrincipal, ...]:
    principals = [
        _TokenPrincipal(
            token=registry._admin_token,
            client_id="brain-admin",
            scopes=("brain:admin",),
            claims=MappingProxyType({"type": "admin"}),
        )
    ]
    for _profile_key, profile in sorted(registry.profiles.items()):
        claims = MappingProxyType(
            {
                "type": "scoped",
                "agent": f"dream-codex-{profile.phase}",
                "phase": profile.phase,
                "project_key": profile.project_key,
            }
        )
        principals.extend(
            _TokenPrincipal(
                token=token,
                client_id=f"dream-codex-{profile.phase}",
                scopes=("brain:dream",),
                claims=claims,
            )
            for token in (profile.active, *profile.accepted)
        )
    return tuple(principals)


class DreamCapabilityTokenVerifier(TokenVerifier):
    """Verify admin and phase-scoped bearers without exposing token material."""

    def __init__(self, registry: DreamCapabilityRegistry) -> None:
        super().__init__()
        self._principals = _token_principals(registry)

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return the principal bound to an opaque bearer, if any."""
        try:
            presented = token.encode("utf-8")
        except UnicodeEncodeError:
            return None
        matches = tuple(
            hmac.compare_digest(
                presented,
                principal.token.get_secret_value().encode("utf-8"),
            )
            for principal in self._principals
        )
        selected = next(
            (
                principal
                for principal, matches_token in zip(self._principals, matches, strict=True)
                if matches_token
            ),
            None,
        )
        if selected is None:
            return None
        return _SecretSafeAccessToken(
            token=token,
            client_id=selected.client_id,
            scopes=list(selected.scopes),
            claims=dict(selected.claims),
        )


def parse_dream_capability_registry(
    raw_registry: str | SecretStr,
    *,
    admin_token: str | SecretStr,
) -> DreamCapabilityRegistry:
    """Parse Dream capability profiles from their JSON configuration."""
    raw_registry_value = (
        raw_registry.get_secret_value() if isinstance(raw_registry, SecretStr) else raw_registry
    )
    admin_token_value = (
        admin_token.get_secret_value() if isinstance(admin_token, SecretStr) else admin_token
    )
    admin_secret = _admin_secret(admin_token_value)
    try:
        payload = json.loads(raw_registry_value, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, TypeError):
        raise DreamCapabilityConfigurationError(
            "invalid Dream capability registry: malformed JSON"
        ) from None
    if not isinstance(payload, dict):
        raise DreamCapabilityConfigurationError(
            "invalid Dream capability registry: JSON object required"
        )
    if not payload:
        raise DreamCapabilityConfigurationError(
            "invalid Dream capability registry: at least one complete project required"
        )
    profiles: dict[tuple[str, str], DreamCapabilityProfile] = {}
    seen_token_values: list[str] = []
    for profile_key, profile_payload in payload.items():
        if not isinstance(profile_key, str) or ":" not in profile_key:
            raise DreamCapabilityConfigurationError(
                "invalid Dream capability registry: project:phase profile key required"
            )
        project_key, phase = profile_key.rsplit(":", 1)
        try:
            canonical_project_key = canonicalize_project_key(project_key)
        except ValueError:
            canonical_project_key = None
        if canonical_project_key != project_key:
            raise DreamCapabilityConfigurationError(
                "invalid Dream capability registry: canonical project key required"
            )
        if phase not in DREAM_PHASE_TOOL_ALLOWLISTS:
            raise DreamCapabilityConfigurationError(
                "invalid Dream capability registry: unsupported phase"
            )
        if not isinstance(profile_payload, dict) or set(profile_payload) != {
            "active",
            "accepted",
        }:
            raise DreamCapabilityConfigurationError(
                "invalid Dream capability registry: profiles require exactly active and accepted"
            )
        accepted_payload = profile_payload["accepted"]
        if not isinstance(accepted_payload, list):
            raise DreamCapabilityConfigurationError(
                "invalid Dream capability registry: accepted must be a list"
            )
        active = _secret_token(profile_payload["active"])
        accepted = tuple(_secret_token(token) for token in accepted_payload)
        profile_token_values = [
            active.get_secret_value(),
            *(token.get_secret_value() for token in accepted),
        ]
        if any(
            token in profile_token_values[:index]
            for index, token in enumerate(profile_token_values)
        ) or any(token in seen_token_values for token in profile_token_values):
            raise DreamCapabilityConfigurationError(
                "invalid Dream capability registry: duplicate token"
            )
        if admin_secret.get_secret_value() in profile_token_values:
            raise DreamCapabilityConfigurationError(
                "invalid Dream capability registry: admin token collision"
            )
        seen_token_values.extend(profile_token_values)
        profiles[(project_key, phase)] = DreamCapabilityProfile(
            project_key=project_key,
            phase=phase,
            active=active,
            accepted=accepted,
        )
    expected_phases = frozenset(DREAM_PHASE_TOOL_ALLOWLISTS)
    projects = {project_key for project_key, _phase in profiles}
    if any(
        {phase for candidate_project, phase in profiles if candidate_project == project_key}
        != expected_phases
        for project_key in projects
    ):
        raise DreamCapabilityConfigurationError(
            "invalid Dream capability registry: complete phase matrix required"
        )
    return DreamCapabilityRegistry(
        profiles=MappingProxyType(profiles),
        _admin_token=admin_secret,
    )
