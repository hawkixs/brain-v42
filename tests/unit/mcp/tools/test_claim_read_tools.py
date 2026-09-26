"""MCP contracts for the scoped, SELECT-only claim read tools.

`brain_claim_list` and `brain_claim_history` (spec 2026-09-19, section 6.6) never
write, never probe a fact and never touch ranking; the server owns project scope
where a Dream call carries one, exactly like `brain_list` (crud_tools.py).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast
from uuid import UUID

import pytest
from fastmcp import Client, FastMCP

from brain_v42.facts.verification import ClaimVerificationService
from brain_v42.mcp.dream_capabilities import (
    DREAM_PHASE_TOOL_ALLOWLISTS,
    dream_phase_tool_allowlist,
)
from brain_v42.mcp.server import prepare_tools_for_transport
from brain_v42.mcp.tool_catalog import ToolCatalogProfile, apply_tool_catalog_profile
from brain_v42.provenance import set_current_actor
from brain_v42.repositories.pg_claim_reads import ReadPage
from brain_v42.services.claim_read_service import ClaimHistory, ClaimReadError, ClaimReadService
from brain_v42.services.dream_project_scope import DreamProjectScope, bind_dream_project_scope

ENTITY_ID = UUID("11111111-1111-1111-1111-111111111111")
CLAIM_ID = UUID("7d0b1f53-4c55-4c2e-9e57-a5b1b8d0c001")


class FakeReadService:
    """Records what reaches the service, and nothing else."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.list_calls: list[dict[str, Any]] = []
        self.history_calls: list[dict[str, Any]] = []
        self.error = error

    async def list_claims(self, **kwargs: Any) -> ReadPage[Any]:
        self.list_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return ReadPage((), None)

    async def history(self, claim_id: UUID, **kwargs: Any) -> ClaimHistory:
        self.history_calls.append({"claim_id": claim_id, **kwargs})
        if self.error is not None:
            raise self.error
        from unittest.mock import MagicMock

        from brain_v42.models.claim_read import ClaimState

        return ClaimHistory(cast(ClaimState, MagicMock(claim=MagicMock(id=claim_id))), (), None)


class FakeVerifier:
    async def verify(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("brain_claim_verify was not called by these tests")


@pytest.fixture(autouse=True)
def _actor() -> Iterator[None]:
    set_current_actor("codex")
    yield
    set_current_actor("unknown")


async def _app(read_service: FakeReadService, profile: ToolCatalogProfile) -> FastMCP:
    from brain_v42.mcp.tools.claim_tools import register_claim_tools

    app = FastMCP("claims", mask_error_details=True)
    register_claim_tools(
        app, cast(ClaimVerificationService, FakeVerifier()), cast(ClaimReadService, read_service)
    )
    apply_tool_catalog_profile(app, profile)
    await prepare_tools_for_transport(app, None)
    return app


async def _call(app: FastMCP, profile: str, name: str, arguments: dict[str, object]) -> Any:
    async with Client(app) as client:
        if profile == "compact":
            return await client.call_tool(
                "brain_call_tool",
                {"name": name, "arguments": arguments},
                raise_on_error=False,
            )
        return await client.call_tool(name, arguments, raise_on_error=False)


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_brain_claim_list_defaults_and_forwards_bounded_arguments(profile: str) -> None:
    service = FakeReadService()
    app = await _app(service, profile)

    result = await _call(app, profile, "brain_claim_list", {})

    assert not result.is_error, result.content
    assert service.list_calls == [
        {
            "project_key": None,
            "entry_id": None,
            "include_retired": False,
            "after_seq": 0,
            "limit": 50,
            "trusted_project_key": None,
        }
    ]


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_brain_claim_list_converts_entity_id_to_the_canonical_entry_id(
    profile: str,
) -> None:
    service = FakeReadService()
    app = await _app(service, profile)

    result = await _call(
        app, profile, "brain_claim_list", {"entity_id": str(ENTITY_ID), "include_retired": True}
    )

    assert not result.is_error, result.content
    assert service.list_calls[0]["entry_id"] == ENTITY_ID
    assert service.list_calls[0]["include_retired"] is True


@pytest.mark.parametrize(
    ("entity_id", "after_seq", "limit"),
    [
        ("not-a-uuid", 0, 50),
        (ENTITY_ID.hex, 0, 50),
        (None, -1, 50),
        (None, 0, 0),
        (None, 0, 101),
    ],
)
@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_brain_claim_list_refuses_malformed_arguments_before_the_service(
    profile: str, entity_id: str | None, after_seq: int, limit: int
) -> None:
    service = FakeReadService()
    app = await _app(service, profile)
    arguments: dict[str, object] = {"after_seq": after_seq, "limit": limit}
    if entity_id is not None:
        arguments["entity_id"] = entity_id

    result = await _call(app, profile, "brain_claim_list", arguments)

    assert result.is_error
    assert service.list_calls == []


async def test_brain_claim_list_under_a_dream_scope_ignores_no_public_override() -> None:
    service = FakeReadService()
    app = await _app(service, "native")
    scope = DreamProjectScope(
        project_key="brain-v42",
        resolver=cast(Any, object()),
        audit=cast(Any, object()),
        tool_name="brain_claim_list",
    )

    with bind_dream_project_scope(scope):
        result = await _call(app, "native", "brain_claim_list", {"project_key": "someone-else"})

    assert result.is_error
    assert service.list_calls == []


async def test_brain_claim_list_under_a_dream_scope_is_server_owned() -> None:
    service = FakeReadService()
    app = await _app(service, "native")
    scope = DreamProjectScope(
        project_key="brain-v42",
        resolver=cast(Any, object()),
        audit=cast(Any, object()),
        tool_name="brain_claim_list",
    )

    with bind_dream_project_scope(scope):
        result = await _call(app, "native", "brain_claim_list", {})

    assert not result.is_error, result.content
    assert service.list_calls[0]["project_key"] == "brain-v42"
    assert service.list_calls[0]["trusted_project_key"] == "brain-v42"


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_brain_claim_history_forwards_the_canonical_claim_id(profile: str) -> None:
    service = FakeReadService()
    app = await _app(service, profile)

    result = await _call(
        app, profile, "brain_claim_history", {"claim_id": str(CLAIM_ID), "limit": 10}
    )

    assert not result.is_error, result.content
    assert service.history_calls == [
        {"claim_id": CLAIM_ID, "after_seq": 0, "limit": 10, "trusted_project_key": None}
    ]


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_brain_claim_history_refuses_a_malformed_claim_id(profile: str) -> None:
    service = FakeReadService()
    app = await _app(service, profile)

    result = await _call(app, profile, "brain_claim_history", {"claim_id": "not-a-uuid"})

    assert result.is_error
    assert service.history_calls == []


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_a_missing_or_out_of_scope_claim_refuses_safely(profile: str) -> None:
    service = FakeReadService(error=ClaimReadError("claim_not_found"))
    app = await _app(service, profile)

    result = await _call(app, profile, "brain_claim_history", {"claim_id": str(CLAIM_ID)})

    assert result.is_error
    assert "claim_not_found" in str(result.content)


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_an_unexpected_read_fault_stays_masked(profile: str) -> None:
    service = FakeReadService(error=RuntimeError("password=hunter2 host=10.0.0.9"))
    app = await _app(service, profile)

    result = await _call(app, profile, "brain_claim_list", {})

    assert result.is_error
    assert "hunter2" not in str(result.content)
    assert "10.0.0.9" not in str(result.content)


async def test_both_read_tools_are_read_only_and_facts_tagged() -> None:
    app = await _app(FakeReadService(), "native")

    async with Client(app) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    for name in ("brain_claim_list", "brain_claim_history"):
        tool = tools[name]
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False


@pytest.mark.parametrize("name", ["brain_claim_list", "brain_claim_history"])
@pytest.mark.parametrize("phase", sorted(DREAM_PHASE_TOOL_ALLOWLISTS))
def test_no_dream_phase_may_call_either_read_tool(name: str, phase: str) -> None:
    assert name not in dream_phase_tool_allowlist(phase)


def test_build_server_wires_one_read_service_into_register_claim_tools() -> None:
    """A read service built but never passed in would leave the two tools unregistered."""
    import ast
    import inspect

    from brain_v42.mcp import server

    tree = ast.parse(inspect.getsource(server.build_server))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    names = [
        node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
        for node in calls
    ]

    assert names.count("ClaimReadService") == 1
    register_call = next(
        node
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "register_claim_tools"
    )
    assert len(register_call.args) + len(register_call.keywords) >= 3


async def test_omitting_the_read_service_still_registers_both_read_tools() -> None:
    """The two read tools stay REGISTERED even without a read_service.

    `mcp/server.py`'s composition root is walked statically by the MCP tool
    census (tests/unit/test_documentation_contract.py); that walk cannot trace
    a runtime-conditional tool registration ("dynamic control flow can bypass
    registration calls"). So the optional dependency that preserves the
    standalone `brain_claim_verify` test seam can no longer skip REGISTERING
    the two read tools -- it now gates only their BEHAVIOUR, exactly like the
    optional `claim_read_svc` already does for `brain_get`/`brain_search`
    (crud_tools.py, brain_tools.py): every call refuses with `read_unavailable`
    until a real `read_service` is supplied.
    """
    from brain_v42.mcp.tools.claim_tools import register_claim_tools

    app = FastMCP("claims", mask_error_details=True)
    register_claim_tools(app, cast(ClaimVerificationService, FakeVerifier()))
    await prepare_tools_for_transport(app, None)

    async with Client(app) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert {"brain_claim_list", "brain_claim_history", "brain_claim_verify"} <= names

        list_result = await client.call_tool("brain_claim_list", {}, raise_on_error=False)
        history_result = await client.call_tool(
            "brain_claim_history", {"claim_id": str(CLAIM_ID)}, raise_on_error=False
        )

    assert list_result.is_error
    assert "read_unavailable" in str(list_result.content)
    assert history_result.is_error
    assert "read_unavailable" in str(history_result.content)
