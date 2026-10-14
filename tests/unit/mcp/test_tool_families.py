"""Credential families constrain discovery and the actual compact inner call."""

import inspect
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import AuthorizationError
from fastmcp.server.auth import AccessToken
from structlog.testing import capture_logs

from brain_v42.credentials.reasons import refusal_counts, reset_refusal_counts
from brain_v42.mcp import server, tool_families
from brain_v42.mcp.client_principal import resolve_client_principal
from brain_v42.mcp.tool_catalog import ToolCatalogProfile, apply_tool_catalog_profile
from brain_v42.mcp.tool_families import (
    NEUTRAL_TOOLS,
    TOOL_FAMILIES,
    FamilyAuthorizationMiddleware,
    apply_tool_families,
    family_of,
)
from tests.unit.mcp.test_credentials_http_wiring import settings


def _scoped_access(*families: str, projects: object = ("red-shrik:agent-a",)) -> AccessToken:
    access = _access(*families)
    access.claims["allowed_projects"] = projects
    return access


@pytest.mark.parametrize("projects", [None, ["brain-v42", "red-shrik:agent-a"]])
def test_project_scope_claim_resolves_without_changing_existing_rights(projects: object) -> None:
    principal = resolve_client_principal(_scoped_access("read", "write", projects=projects))
    assert principal is not None
    assert principal.families == frozenset({"read", "write"})
    assert principal.allowed_projects == (None if projects is None else tuple(projects))


@pytest.mark.parametrize(
    "projects",
    [[], ["brain"], ["brain_v42"], ["Bad"], ["a" * 51], ["a", "a"], [None], "a", ["a\n"]],
)
def test_malformed_project_scope_claim_grants_nothing(projects: object) -> None:
    assert resolve_client_principal(_scoped_access("write", projects=projects)) is None


@pytest.mark.parametrize("family", ["read", "write", "delivery", "admin"])
async def test_null_scope_retains_every_existing_tool_family(
    monkeypatch: pytest.MonkeyPatch, family: str
) -> None:
    access = _scoped_access("read", "write", "delivery", projects=None)
    monkeypatch.setattr(tool_families, "get_access_token", lambda: access)
    monkeypatch.setattr(tool_families, "get_http_headers", lambda **kw: {"mcp-session-id": "c"})
    middleware = FamilyAuthorizationMiddleware(elevation_checker=AsyncMock(return_value=True))
    for name, tool_family in TOOL_FAMILIES.items():
        if tool_family == family:
            handler = AsyncMock(return_value="unchanged")
            assert (
                await middleware.on_call_tool(
                    SimpleNamespace(message=SimpleNamespace(name=name, arguments={})), handler
                )
                == "unchanged"
            )
            handler.assert_awaited_once()


@pytest.mark.parametrize("profile", ["native", "compact"])
@pytest.mark.parametrize("project", ["red-shrik:agent-a", "brain_v42", "brain"])
async def test_scoped_writer_can_update_only_exact_canonical_focus(
    monkeypatch: pytest.MonkeyPatch, profile: ToolCatalogProfile, project: str
) -> None:
    access = _scoped_access("write", projects=["red-shrik:agent-a", "brain-v42"])
    monkeypatch.setattr(tool_families, "get_access_token", lambda: access)
    mcp = _small_server()

    @mcp.tool
    async def brain_update_project_focus(project_key: str) -> str:
        """Update own project focus."""
        return project_key

    apply_tool_catalog_profile(mcp, profile)
    async with Client(mcp) as client:
        if profile == "native":
            assert "brain_update_project_focus" in {t.name for t in await client.list_tools()}
        else:
            found = await client.call_tool("brain_find_tool", {"query": "update own project focus"})
            assert "brain_update_project_focus" in {t["name"] for t in found.data}
        name = "brain_update_project_focus" if profile == "native" else "brain_call_tool"
        arguments = {"project_key": project}
        if profile == "compact":
            arguments = {"name": "brain_update_project_focus", "arguments": arguments}
        assert (await client.call_tool(name, arguments)).data == project
    assert refusal_counts() == {}


@pytest.mark.parametrize("profile", ["native", "compact"])
@pytest.mark.parametrize(
    "arguments,reason",
    [
        ({"project_key": "red-shrik:agent-a:child"}, "family_denied"),
        ({"project_key": "red-shrik"}, "family_denied"),
        ({"project_key": "other"}, "family_denied"),
        ({"project_key": "Bad"}, "family_denied"),
        ({}, "family_denied"),
        (
            {"project_key": "red-shrik:agent-a", "blockers": ["blocked"]},
            "focus_side_effect_refused",
        ),
        ({"project_key": "red-shrik:agent-a", "blockers": []}, "focus_side_effect_refused"),
        ({"project_key": "red-shrik:agent-a", "feature_status": {}}, "focus_side_effect_refused"),
        ({"project_key": "red-shrik:agent-a", "unpin": []}, "focus_side_effect_refused"),
    ],
)
async def test_scoped_focus_refusal_precedes_handler_in_both_paths(
    monkeypatch: pytest.MonkeyPatch,
    profile: ToolCatalogProfile,
    arguments: dict[str, object],
    reason: str,
) -> None:
    monkeypatch.setattr(tool_families, "get_access_token", lambda: _scoped_access("write"))
    calls: list[str] = []
    mcp = _small_server()

    @mcp.tool
    async def brain_update_project_focus(
        project_key: str,
        blockers: list[str] | None = None,
        feature_status: dict[str, str] | None = None,
        unpin: list[str] | None = None,
    ) -> str:
        calls.append(project_key)
        return "written"

    apply_tool_catalog_profile(mcp, profile)
    async with Client(mcp) as client:
        name = "brain_update_project_focus" if profile == "native" else "brain_call_tool"
        if profile == "compact":
            arguments = {"name": "brain_update_project_focus", "arguments": arguments}
        result = await client.call_tool(name, arguments, raise_on_error=False)
        assert result.is_error
        assert reason in str(result.content)
    assert calls == []
    assert refusal_counts() == {reason: 1}


@pytest.mark.parametrize("profile", ["native", "compact"])
@pytest.mark.parametrize("field", ["blockers", "feature_status", "unpin"])
async def test_scoped_focus_permits_explicit_null_optional_arguments_in_both_paths(
    monkeypatch: pytest.MonkeyPatch, profile: ToolCatalogProfile, field: str
) -> None:
    monkeypatch.setattr(tool_families, "get_access_token", lambda: _scoped_access("write"))
    calls: list[str] = []
    mcp = _small_server()

    @mcp.tool
    async def brain_update_project_focus(
        project_key: str,
        blockers: list[str] | None = None,
        feature_status: dict[str, str] | None = None,
        unpin: list[str] | None = None,
    ) -> str:
        calls.append(project_key)
        assert blockers is feature_status is unpin is None
        return "written"

    apply_tool_catalog_profile(mcp, profile)
    arguments: dict[str, object] = {"project_key": "red-shrik:agent-a", field: None}
    name = "brain_update_project_focus" if profile == "native" else "brain_call_tool"
    if profile == "compact":
        arguments = {"name": "brain_update_project_focus", "arguments": arguments}
    async with Client(mcp) as client:
        assert (await client.call_tool(name, arguments)).data == "written"
    assert calls == ["red-shrik:agent-a"]
    assert refusal_counts() == {}


@pytest.mark.parametrize("families,projects", [("read", ["red-shrik:agent-a"]), ("write", None)])
async def test_scope_alone_and_unscoped_write_grant_no_focus(
    monkeypatch: pytest.MonkeyPatch, families: str, projects: object
) -> None:
    monkeypatch.setattr(
        tool_families, "get_access_token", lambda: _scoped_access(families, projects=projects)
    )
    middleware = FamilyAuthorizationMiddleware()
    assert not await middleware.allows_current_tool("brain_update_project_focus")
    assert not await middleware.allows_current_tool("brain_set_project_context")
    assert await middleware.allows_current_tool("brain_project_focus_get") == (families == "read")


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_own_focus_shrink_still_requires_existing_elevation(
    monkeypatch: pytest.MonkeyPatch, profile: ToolCatalogProfile
) -> None:
    from brain_v42.mcp.tools.project_context_tools import register_project_context_tools
    from brain_v42.services import focus_shrink_override

    monkeypatch.setattr(tool_families, "get_access_token", lambda: _scoped_access("write"))
    lookup = AsyncMock(return_value=False)
    monkeypatch.setattr(focus_shrink_override, "get_current_principal", lambda: "reader")
    monkeypatch.setattr(focus_shrink_override, "get_current_transport", lambda: "c")
    monkeypatch.setattr(
        focus_shrink_override,
        "PgClientCredentialRepo",
        lambda: SimpleNamespace(has_active_elevation=lookup),
    )
    roadmap = MagicMock(update_project_focus=AsyncMock())
    mcp = _small_server()
    register_project_context_tools(mcp, MagicMock(), roadmap_svc=roadmap)
    apply_tool_catalog_profile(mcp, profile)
    arguments = {
        "project_key": "red-shrik:agent-a",
        "current_focus": "focus",
        "expected_focus_revision": 0,
        "allow_focus_shrink": True,
    }
    name = "brain_update_project_focus" if profile == "native" else "brain_call_tool"
    if profile == "compact":
        arguments = {"name": "brain_update_project_focus", "arguments": arguments}
    async with Client(mcp) as client:
        result = await client.call_tool(name, arguments, raise_on_error=False)
        assert "focus_shrink_override_refused" in str(result.content)
    roadmap.update_project_focus.assert_not_awaited()
    lookup.assert_awaited_once()
    assert lookup.await_args.args[:2] == ("c", "reader")


def _access(*families: str, client_id: str = "reader") -> AccessToken:
    return AccessToken(
        token="",
        client_id=client_id,
        scopes=list(families),
        claims={"credential_id": "00000000-0000-0000-0000-000000000001", "issuers": ["actor.*"]},
    )


@pytest.fixture(autouse=True)
def _counts() -> Iterator[None]:
    reset_refusal_counts()
    yield
    reset_refusal_counts()


def _bind(monkeypatch: pytest.MonkeyPatch, *families: str) -> None:
    monkeypatch.setattr(tool_families, "get_access_token", lambda: _access(*families))


def _small_server(checker: AsyncMock | None = None) -> FastMCP:
    mcp = FastMCP("families")
    mcp.add_middleware(FamilyAuthorizationMiddleware(elevation_checker=checker))

    @mcp.tool
    async def brain_search() -> str:
        """Search family contract memory."""
        return "read"

    @mcp.tool
    async def brain_learn() -> str:
        """Capture family contract memory."""
        return "write"

    @mcp.tool
    async def brain_delivery_get() -> str:
        """Read family contract delivery."""
        return "delivery"

    @mcp.tool
    async def brain_delete() -> str:
        """Delete family contract memory."""
        return "admin"

    return mcp


async def test_every_registered_tool_is_classified_and_tagged() -> None:
    from brain_v42.mcp.tools.brain_tools import register_tools
    from brain_v42.mcp.tools.claim_tools import register_claim_tools
    from brain_v42.mcp.tools.crud_tools import register_crud_tools
    from brain_v42.mcp.tools.decay_tools import register_decay_tools
    from brain_v42.mcp.tools.delivery_tools import register_delivery_tools
    from brain_v42.mcp.tools.dream_tools import register_dream_tools
    from brain_v42.mcp.tools.fact_tools import register_fact_tools
    from brain_v42.mcp.tools.focus_slot_tools import register_focus_slot_tools
    from brain_v42.mcp.tools.plan_tools import register_plan_tools
    from brain_v42.mcp.tools.roadmap_tools import register_roadmap_tools
    from brain_v42.mcp.tools.session_tools import register_session_tools
    from brain_v42.mcp.tools.ticket_tools import register_ticket_tools

    mcp = server.create_mcp_instance()
    for register in (
        register_tools,
        register_claim_tools,
        register_crud_tools,
        register_decay_tools,
        register_delivery_tools,
        register_dream_tools,
        register_fact_tools,
        register_focus_slot_tools,
        register_plan_tools,
        register_roadmap_tools,
        register_session_tools,
        register_ticket_tools,
    ):
        required = {
            name: MagicMock()
            for name, parameter in inspect.signature(register).parameters.items()
            if name != "mcp" and parameter.default is inspect.Parameter.empty
        }
        register(mcp, **required)
    tools = await mcp._list_tools()
    names = {tool.name for tool in tools}
    assert not (missing := names - TOOL_FAMILIES.keys() - NEUTRAL_TOOLS), sorted(missing)
    assert set(TOOL_FAMILIES.values()) == {"read", "write", "delivery", "admin"}
    assert not NEUTRAL_TOOLS & TOOL_FAMILIES.keys()
    server.plan_http_transport(mcp, settings(), credential_verifier=MagicMock())
    await server.prepare_tools_for_transport(mcp, None)
    for tool in await mcp._list_tools():
        assert f"family:{family_of(tool.name)}" in tool.tags
        if tool.name.startswith("brain_session_"):
            assert family_of(tool.name) == "write"
        if tool.name.startswith("brain_delivery_"):
            assert family_of(tool.name) == "delivery"


@pytest.mark.parametrize(
    "name,family",
    [
        ("brain_accept_adr", "admin"),
        ("brain_deprecate_adr", "admin"),
        ("brain_propose_adr", "write"),
        ("brain_update", "write"),
        ("brain_validate_learning", "write"),
        ("brain_use_snippet", "write"),
        ("brain_execute_runbook", "write"),
        ("brain_refresh_entity", "write"),
        ("brain_project_archive", "admin"),
        ("brain_project_unarchive", "admin"),
        ("new_unclassified_tool", "admin"),
    ],
)
def test_family_rulings(name: str, family: str) -> None:
    assert family_of(name) == family


async def test_tagging_preserves_identity_version_schema_and_existing_tags() -> None:
    mcp = FastMCP("tags")

    @mcp.tool(tags={"existing"}, version="1.0")
    async def brain_search(query: str) -> str:
        return query

    original = (await mcp._list_tools())[0]
    schema = original.parameters.copy()
    await apply_tool_families(mcp)
    await apply_tool_families(mcp)
    tagged = (await mcp._list_tools())[0]
    assert tagged is original
    assert tagged.tags == {"existing", "family:read"}
    assert tagged.parameters == schema
    assert tagged.version == "1.0"


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_read_catalog_search_and_native_override_are_filtered(
    monkeypatch: pytest.MonkeyPatch, profile: ToolCatalogProfile
) -> None:
    from brain_v42.mcp import tool_catalog

    _bind(monkeypatch, "read")
    mcp = _small_server()
    apply_tool_catalog_profile(mcp, profile)
    async with Client(mcp) as client:
        assert {t.name for t in await client.list_tools()} <= {"brain_search", *NEUTRAL_TOOLS}
        if profile == "compact":
            for query in ("family contract memory", "capture delete delivery"):
                result = await client.call_tool("brain_find_tool", {"query": query})
                assert {t["name"] for t in result.data} <= {"brain_search"}
        monkeypatch.setattr(
            tool_catalog, "get_http_headers", lambda: {"x-brain-tool-profile": "native"}
        )
        assert {t.name for t in await client.list_tools()} == {"brain_search"}
    for name in ("brain_learn", "brain_delivery_get", "brain_delete"):
        assert await mcp.get_tool(name) is None


@pytest.mark.parametrize("families", [("telemetry",), ("elevate",), ("telemetry", "elevate")])
@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_non_tool_families_see_nothing_and_every_call_is_denied(
    monkeypatch: pytest.MonkeyPatch, families: tuple[str, ...], profile: ToolCatalogProfile
) -> None:
    _bind(monkeypatch, *families)
    monkeypatch.setattr(
        tool_families, "get_http_headers", lambda **kwargs: {"mcp-session-id": "active"}
    )
    mcp = _small_server(AsyncMock(return_value=True))
    apply_tool_catalog_profile(mcp, profile)
    async with Client(mcp) as client:
        assert await client.list_tools() == []
        for name in ("brain_search", "brain_delete", *sorted(NEUTRAL_TOOLS)):
            result = await client.call_tool(name, {}, raise_on_error=False)
            assert result.is_error
    assert refusal_counts() == {"family_denied": 4}


@pytest.mark.parametrize("name", ["brain_learn", "brain_delivery_get", "unclassified"])
async def test_denial_precedes_handler_logs_403_and_counts_once(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    _bind(monkeypatch, "read")
    handler = AsyncMock()
    middleware = FamilyAuthorizationMiddleware()
    with capture_logs() as logs, pytest.raises(AuthorizationError):
        await middleware.on_call_tool(SimpleNamespace(message=SimpleNamespace(name=name)), handler)
    handler.assert_not_awaited()
    assert refusal_counts() == {"family_denied": 1}
    assert len(logs) == 1
    assert (
        logs[0].items()
        >= {"event": "mcp_auth.refused", "status": 403, "reason": "family_denied"}.items()
    )


async def test_neutral_gateway_judges_the_inner_call_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, "read")
    mcp = _small_server()
    apply_tool_catalog_profile(mcp, "compact")
    async with Client(mcp) as client:
        assert (
            await client.call_tool("brain_call_tool", {"name": "brain_search", "arguments": {}})
        ).data == "read"
        result = await client.call_tool(
            "brain_call_tool", {"name": "brain_learn", "arguments": {}}, raise_on_error=False
        )
        assert result.is_error
    assert refusal_counts() == {"family_denied": 1}


async def test_discovery_does_not_reuse_another_credentials_rights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(monkeypatch, "write")
    mcp = _small_server()
    apply_tool_catalog_profile(mcp, "compact")
    async with Client(mcp) as client:
        result = await client.call_tool(
            "brain_find_tool", {"query": "capture family contract memory"}
        )
        assert "brain_learn" in {t["name"] for t in result.data}
        _bind(monkeypatch, "read")
        result = await client.call_tool(
            "brain_find_tool", {"query": "capture family contract memory"}
        )
        assert {t["name"] for t in result.data} <= {"brain_search"}


@pytest.mark.parametrize(
    "state", ["active", "other_connection", "other_client", "expired", "revoked", "unavailable"]
)
async def test_repository_elevation_checks_the_frozen_pair_each_call(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=1)
    revoked = False
    connection = "frozen" if state != "other_connection" else "other"
    client_id = "reader" if state != "other_client" else "other"
    if state == "expired":
        expires = now - timedelta(minutes=1)
    if state == "revoked":
        revoked = True

    async def has_active_elevation(cid: str, principal: str, at: datetime) -> bool:
        if state == "unavailable":
            raise OSError("unavailable")
        return (cid, principal) == ("frozen", "reader") and expires > at and not revoked

    repository = MagicMock(has_active_elevation=AsyncMock(side_effect=has_active_elevation))
    monkeypatch.setattr(server, "PgClientCredentialRepo", lambda factory: repository)
    monkeypatch.setattr(server, "get_session_factory", MagicMock())
    mcp = server.create_mcp_instance()
    server.plan_http_transport(mcp, settings(), credential_verifier=MagicMock())
    middleware = next(m for m in mcp.middleware if isinstance(m, FamilyAuthorizationMiddleware))
    monkeypatch.setattr(
        tool_families, "get_access_token", lambda: _access("read", client_id=client_id)
    )
    monkeypatch.setattr(
        tool_families, "get_http_headers", lambda **kwargs: {"mcp-session-id": connection}
    )
    context = SimpleNamespace(message=SimpleNamespace(name="brain_delete"))
    handler = AsyncMock(return_value="admin")
    if state == "active":
        assert await middleware.on_call_tool(context, handler) == "admin"
        revoked = True
    with pytest.raises(AuthorizationError):
        await middleware.on_call_tool(context, handler)
    assert repository.has_active_elevation.await_count == (2 if state == "active" else 1)
    args = repository.has_active_elevation.await_args.args
    assert args[:2] == (connection, client_id)
    assert args[2].tzinfo is UTC


async def test_missing_connection_and_checker_failure_cannot_grant_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(monkeypatch, "read")
    monkeypatch.setattr(tool_families, "get_http_headers", lambda **kwargs: {})
    checker = AsyncMock(return_value=True)
    middleware = FamilyAuthorizationMiddleware(elevation_checker=checker)
    with pytest.raises(AuthorizationError):
        await middleware.on_call_tool(
            SimpleNamespace(message=SimpleNamespace(name="brain_delete")), AsyncMock()
        )
    checker.assert_not_awaited()


async def test_compact_admin_discovery_rechecks_elevation(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, "read")
    monkeypatch.setattr(
        tool_families, "get_http_headers", lambda **kwargs: {"mcp-session-id": "frozen"}
    )
    checker = AsyncMock(return_value=True)
    mcp = _small_server(checker)
    apply_tool_catalog_profile(mcp, "compact")
    async with Client(mcp) as client:
        result = await client.call_tool(
            "brain_find_tool", {"query": "delete family contract memory"}
        )
        assert "brain_delete" in {t["name"] for t in result.data}
        checker.return_value = False
        result = await client.call_tool(
            "brain_find_tool", {"query": "delete family contract memory"}
        )
        assert "brain_delete" not in {t["name"] for t in result.data}
        assert (
            await client.call_tool(
                "brain_call_tool", {"name": "brain_delete", "arguments": {}}, raise_on_error=False
            )
        ).is_error
    assert refusal_counts() == {"family_denied": 1}


@pytest.mark.parametrize("profile", ["native", "compact", "native_override"])
@pytest.mark.parametrize("state", ["active", "inactive", "unavailable"])
async def test_tools_list_resolves_elevation_once_and_rechecks_after_revoke(
    monkeypatch: pytest.MonkeyPatch, profile: str, state: str
) -> None:
    from brain_v42.mcp import tool_catalog

    _bind(monkeypatch, "read")
    monkeypatch.setattr(
        tool_families, "get_http_headers", lambda **kwargs: {"mcp-session-id": "frozen"}
    )
    if profile == "native_override":
        monkeypatch.setattr(
            tool_catalog, "get_http_headers", lambda: {"x-brain-tool-profile": "native"}
        )
    lookup = AsyncMock(return_value=state == "active")
    if state == "unavailable":
        lookup.side_effect = OSError("unavailable")
    repository = MagicMock(has_active_elevation=lookup)
    monkeypatch.setattr(server, "PgClientCredentialRepo", lambda factory: repository)
    monkeypatch.setattr(server, "get_session_factory", MagicMock())
    mcp = server.create_mcp_instance()

    @mcp.tool
    async def brain_search() -> str:
        return "read"

    async def admin() -> str:
        return "admin"

    admin_names = {name for name, family in TOOL_FAMILIES.items() if family == "admin"}
    for name in admin_names:
        mcp.tool(name=name)(admin)
    apply_tool_catalog_profile(mcp, "native" if profile == "native" else "compact")
    server.plan_http_transport(mcp, settings(), credential_verifier=MagicMock())
    async with Client(mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
        if profile != "compact":
            assert names & admin_names == (admin_names if state == "active" else set())
        lookup.assert_awaited_once()
        assert lookup.await_args.args[:2] == ("frozen", "reader")
        lookup.side_effect = None
        lookup.return_value = False
        assert not {tool.name for tool in await client.list_tools()} & admin_names
        assert lookup.await_count == 2


async def test_factory_order_and_actor_principal_remain_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.mcp import provenance_middleware
    from brain_v42.mcp.provenance_middleware import ProvenanceMiddleware
    from brain_v42.provenance import get_current_actor, get_current_principal

    _bind(monkeypatch, "read")
    request = SimpleNamespace(
        scope={"state": {"brain_principal": "reader", "brain_actor": "actor.test"}}
    )
    monkeypatch.setattr(provenance_middleware, "get_http_request", lambda: request)
    monkeypatch.setattr(server, "get_session_factory", MagicMock())
    mcp = server.create_mcp_instance()
    server.plan_http_transport(mcp, settings(), credential_verifier=MagicMock())
    kinds = [type(m) for m in mcp.middleware]
    assert kinds.count(FamilyAuthorizationMiddleware) == 1
    assert kinds.index(ProvenanceMiddleware) < kinds.index(FamilyAuthorizationMiddleware)

    @mcp.tool
    async def brain_search() -> dict[str, str | None]:
        return {"actor": get_current_actor(), "principal": get_current_principal()}

    async with Client(mcp) as client:
        assert (await client.call_tool("brain_search", {})).data == {
            "actor": "actor.test",
            "principal": "reader",
        }


async def test_shared_token_mode_keeps_catalog_calls_and_middleware_unchanged() -> None:
    config = settings()
    config.brain_mcp_auth_mode = "shared_token"
    config.mcp_http_token = "test-only-shared-placeholder"
    mcp = server.create_mcp_instance()
    original = list(mcp.middleware)
    server.plan_http_transport(mcp, config)
    assert mcp.middleware == original

    @mcp.tool
    async def brain_delete() -> str:
        return "unchanged"

    await server.prepare_tools_for_transport(mcp, None)
    assert all(
        not any(tag.startswith("family:") for tag in tool.tags) for tool in await mcp._list_tools()
    )
    apply_tool_catalog_profile(mcp, "native")
    async with Client(mcp) as client:
        assert {t.name for t in await client.list_tools()} == {"brain_delete"}
        assert (await client.call_tool("brain_delete", {})).data == "unchanged"
    assert refusal_counts() == {}


@pytest.mark.parametrize("scopes", [["admin"], ["read", "admin"], ["unknown"]])
async def test_malformed_families_cannot_gain_rights_even_when_elevated(
    monkeypatch: pytest.MonkeyPatch, scopes: list[str]
) -> None:
    _bind(monkeypatch, *scopes)
    monkeypatch.setattr(
        tool_families, "get_http_headers", lambda **kwargs: {"mcp-session-id": "frozen"}
    )
    middleware = FamilyAuthorizationMiddleware(elevation_checker=AsyncMock(return_value=True))
    for name in ("brain_search", "brain_delete", "brain_call_tool"):
        with pytest.raises(AuthorizationError):
            await middleware.on_call_tool(
                SimpleNamespace(message=SimpleNamespace(name=name)), AsyncMock()
            )


def test_access_token_parser_accepts_a3_claims_and_all_storable_families() -> None:
    principal = resolve_client_principal(_access("read", "telemetry", "elevate"))
    assert principal is not None
    assert principal.client_id == "reader"
    assert principal.families == frozenset({"read", "telemetry", "elevate"})
    assert principal.allowed_projects is None


@pytest.mark.parametrize("projects", [None, ["red-shrik:agent-a"]])
async def test_admin_elevation_retains_focus_and_roadmap_rights(
    monkeypatch: pytest.MonkeyPatch, projects: object
) -> None:
    monkeypatch.setattr(
        tool_families, "get_access_token", lambda: _scoped_access("write", projects=projects)
    )
    monkeypatch.setattr(tool_families, "get_http_headers", lambda **kw: {"mcp-session-id": "c"})
    checker = AsyncMock(return_value=True)
    middleware = FamilyAuthorizationMiddleware(elevation_checker=checker)
    handler = AsyncMock(return_value="written")
    assert (
        await middleware.on_call_tool(
            SimpleNamespace(
                message=SimpleNamespace(
                    name="brain_update_project_focus",
                    arguments={"project_key": "other", "blockers": ["blocked"]},
                )
            ),
            handler,
        )
        == "written"
    )
    handler.assert_awaited_once()
    checker.assert_awaited_once()


def test_argument_guidance_is_outer_to_production_authorization_middleware() -> None:
    from brain_v42.mcp.argument_guidance import ArgumentGuidanceMiddleware
    from brain_v42.mcp.dream_capabilities import DreamCapabilityMiddleware
    from tests.unit.mcp.test_dream_capability_http import (
        FakeProjectResolver,
        _registry_json,
    )
    from tests.unit.mcp.test_dream_capability_http import (
        _settings as dream_settings,
    )

    credentials_server = server.create_mcp_instance()
    server._configure_http_security(credentials_server, settings(), credential_verifier=MagicMock())
    credential_kinds = [type(item) for item in credentials_server.middleware]
    assert credential_kinds.index(ArgumentGuidanceMiddleware) < credential_kinds.index(
        FamilyAuthorizationMiddleware
    )

    dream_server = server.create_mcp_instance()
    dream_config = dream_settings(enforcement=True, registry=_registry_json())
    server._configure_http_security(
        dream_server, dream_config, project_resolver=FakeProjectResolver()
    )
    dream_kinds = [type(item) for item in dream_server.middleware]
    assert dream_kinds.index(ArgumentGuidanceMiddleware) < dream_kinds.index(
        DreamCapabilityMiddleware
    )


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_http_family_denial_is_jsonrpc_200_and_token_rotation_uses_current_scopes(
    profile: ToolCatalogProfile,
) -> None:
    from uuid import UUID

    import httpx

    from brain_v42.credentials.verifier import VerifiedPrincipal

    def verified(token: str) -> VerifiedPrincipal:
        return VerifiedPrincipal(
            "reader",
            frozenset({"read" if token == "test-reader-placeholder" else "write"}),
            frozenset({"actor.*"}),
            UUID("00000000-0000-0000-0000-000000000001"),
            None,
        )

    registry = MagicMock(verify=AsyncMock(side_effect=verified))
    mcp = server.create_mcp_instance()
    calls: list[str] = []

    @mcp.tool
    async def brain_learn() -> str:
        calls.append("write")
        return "written"

    @mcp.tool
    async def brain_search() -> str:
        return "read"

    apply_tool_catalog_profile(mcp, profile)
    plan = server.plan_http_transport(mcp, settings(), credential_verifier=registry)
    app = mcp.http_app(middleware=plan.middleware, stateless_http=False, json_response=True)
    headers = {
        "Authorization": "Bearer test-reader-placeholder",
        "X-Brain-Agent": "actor.test",
        "Accept": "application/json, text/event-stream",
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost"
        ) as client,
    ):
        initialized = await client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200
        headers["Mcp-Session-Id"] = initialized.headers["Mcp-Session-Id"]
        await client.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        name = "brain_learn" if profile == "native" else "brain_call_tool"
        arguments = {} if profile == "native" else {"name": "brain_learn", "arguments": {}}
        message = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        with capture_logs() as logs:
            denied = await client.post("/mcp", headers=headers, json=message)
        assert denied.status_code == 200
        assert denied.json()["result"]["isError"] is True
        assert calls == []
        events = [event for event in logs if event["event"] == "mcp_auth.refused"]
        assert len(events) == 1
        assert (
            events[0].items()
            >= {
                "status": 403,
                "reason": "family_denied",
                "client_id": "reader",
                "declared_agent": "actor.test",
                "tool": "brain_learn",
            }.items()
        )
        assert refusal_counts() == {"family_denied": 1}

        # The connection owner stays the same; the request's new credential has
        # different rights from initialize's token inherited by the SDK task.
        headers["Authorization"] = "Bearer test-writer-placeholder"
        allowed = await client.post("/mcp", headers=headers, json=message | {"id": 3})
        assert allowed.status_code == 200
        assert allowed.json()["result"]["isError"] is False
        assert calls == ["write"]
        assert refusal_counts() == {"family_denied": 1}
