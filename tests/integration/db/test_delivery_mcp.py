"""Real FastMCP + PostgreSQL contracts for the public delivery surface."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.server.auth import AccessToken

from brain_v42.models.delivery import ArtifactBinding
from brain_v42.models.ticket import TicketCreate, TicketKind, TicketStatus
from brain_v42.repositories.pg_ticket import PgTicketRepo
from tests.integration.db.test_delivery_receipt_issuance import RID, _contract
from tests.integration.db.test_delivery_requester_acceptance import _publish_and_issue, _workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

DELIVERY_TOOLS = frozenset(
    {
        "brain_delivery_contract_set",
        "brain_delivery_bind_pr",
        "brain_delivery_get",
        "brain_delivery_list",
        "brain_delivery_refresh",
        "brain_delivery_claim",
        "brain_delivery_claim_renew",
        "brain_delivery_claim_release",
        "brain_delivery_accept",
    }
)
MUTATING_DELIVERY_TOOLS = DELIVERY_TOOLS - {"brain_delivery_get", "brain_delivery_list"}


@pytest_asyncio.fixture
async def delivery_mcp(request, session_factory, engine, monkeypatch) -> AsyncIterator[object]:
    """Use production composition and only replace the global PG singletons."""
    options = getattr(request, "param", {})
    if isinstance(options, str):
        options = {"profile": options}
    profile = options.get("profile", "native")
    enabled = options.get("enabled", True)
    monkeypatch.setenv("BRAIN_DELIVERY_ENABLED", str(enabled).lower())
    monkeypatch.setenv(
        "BRAIN_DELIVERY_REPOSITORY_REGISTRY",
        '{"executor":{"1337360966":"hawkixs/brain-v42"}}',
    )
    for name in (
        "GRAPH_ENABLED",
        "GRAPH_LEDGER_WRITE_ENABLED",
        "DECAY_ENABLED",
        "METRICS_ENABLED",
        "CLIENT_ACTIVITY_REPORTING_ENABLED",
        "BRAIN_DREAM_CAPABILITY_ENFORCEMENT",
    ):
        monkeypatch.setenv(name, "false")
    monkeypatch.setenv("BRAIN_MCP_PROFILE", profile)

    import brain_v42.db.engine as engine_module
    import brain_v42.mcp.server as server_module
    from brain_v42.config import get_settings

    get_settings.cache_clear()
    original_engine, original_factory, original_mcp = (
        engine_module._engine,
        engine_module._session_factory,
        server_module.mcp,
    )
    engine_module._engine = engine
    engine_module._session_factory = session_factory
    fresh = server_module.create_mcp_instance()
    monkeypatch.setattr(server_module, "mcp", fresh)
    try:
        built = server_module.build_server()
        await server_module.prepare_tools_for_transport(built.mcp, built.metrics_collector)
        yield built.mcp
    finally:
        engine_module._engine, engine_module._session_factory, server_module.mcp = (
            original_engine,
            original_factory,
            original_mcp,
        )
        get_settings.cache_clear()


async def _call(client: Client, name: str, arguments: dict[str, object], *, compact: bool):
    result = await client.call_tool(
        "brain_call_tool" if compact else name,
        {"name": name, "arguments": arguments} if compact else arguments,
        raise_on_error=False,
    )
    assert not result.is_error, str(result.content)
    assert isinstance(result.structured_content, dict)
    return result.structured_content


async def _ticket(session_factory, *, requester: str = "requester", executor: str = "executor"):
    return await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title=f"MCP delivery {uuid4()}",
            body="real FastMCP and isolated PostgreSQL fixture",
            from_project=requester,
            to_project=executor,
        )
    )


def _uuid(ticket_id: UUID) -> str:
    return str(ticket_id)


async def _profile(client: Client) -> bool:
    return "brain_call_tool" in {tool.name for tool in await client.list_tools()}


@pytest.mark.parametrize("delivery_mcp", ["native", "compact"], indirect=True)
async def test_catalogues_publish_schema_and_dispatch_the_nine_delivery_tools(delivery_mcp):
    """Tool registration is proved by FastMCP dispatch, never ``hasattr``."""
    async with Client(delivery_mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        compact = "brain_call_tool" in tools
        if compact:
            assert not DELIVERY_TOOLS & tools.keys()
            found_names: set[str] = set()
            for name in DELIVERY_TOOLS:
                found = await client.call_tool("brain_find_tool", {"query": name})
                found_names.update(item["name"] for item in found.data)
            assert DELIVERY_TOOLS <= found_names
        else:
            assert DELIVERY_TOOLS <= tools.keys()
            for name in DELIVERY_TOOLS:
                schema = tools[name].inputSchema
                required = set(schema["required"])
                assert "actor_project" in required
                if name != "brain_delivery_list":
                    assert "ticket_id" in required
                assert tools[name].meta["fastmcp"]["version"] == "1.0"
                assert tools[name].outputSchema["type"] == "object"
        result = await client.call_tool(
            "brain_call_tool" if compact else "brain_delivery_get",
            (
                {
                    "name": "brain_delivery_get",
                    "arguments": {"ticket_id": str(uuid4()), "actor_project": "requester"},
                }
                if compact
                else {"ticket_id": str(uuid4()), "actor_project": "requester"}
            ),
            raise_on_error=False,
        )
    assert result.is_error and "ticket_not_found" in str(result.content)


@pytest.mark.parametrize("delivery_mcp", ["native", "compact"], indirect=True)
async def test_contract_claim_bind_reads_refresh_and_amendment_are_real_pg(
    delivery_mcp, session_factory, caplog, capsys
):
    ticket = await _ticket(session_factory)
    contract = _contract(mode="explicit").model_dump(mode="json")
    async with Client(delivery_mcp) as client:
        compact = await _profile(client)
        revision = await _call(
            client,
            "brain_delivery_contract_set",
            {
                "ticket_id": _uuid(ticket.id),
                "actor_project": "requester",
                "contract": contract,
                "expected_revision": 0,
                "idempotency_key": f"mcp-contract-{ticket.id}",
                "reason": "The MCP contract captures the requested delivery.",
            },
            compact=compact,
        )
        assert revision["contract_revision"] == 1
        initial = await _call(
            client,
            "brain_delivery_get",
            {
                "ticket_id": _uuid(ticket.id),
                "actor_project": "requester",
                "history_limit": 20,
            },
            compact=compact,
        )
        claim = await _call(
            client,
            "brain_delivery_claim",
            {
                "ticket_id": _uuid(ticket.id),
                "actor_project": "executor",
                "owner_key": "mcp-worker",
                "work_kind": "implement",
                "expected_workflow_version": initial["assessment"]["assessment_version"],
                "expected_assessment_id": initial["assessment"]["assessment_id"],
                "ttl_seconds": 60,
            },
            compact=compact,
        )
        assert claim["claim_token"]
        with_claim = await _call(
            client,
            "brain_delivery_get",
            {
                "ticket_id": _uuid(ticket.id),
                "actor_project": "requester",
            },
            compact=compact,
        )
        page = await _call(
            client,
            "brain_delivery_list",
            {
                "actor_project": "requester",
                "limit": 20,
                "work": "implement",
            },
            compact=compact,
        )
        assert claim["claim_token"] not in repr(with_claim)
        assert claim["claim_token"] not in repr(page)
        wrong_token = await client.call_tool(
            "brain_call_tool" if compact else "brain_delivery_claim_release",
            (
                {
                    "name": "brain_delivery_claim_release",
                    "arguments": {
                        "ticket_id": _uuid(ticket.id),
                        "actor_project": "executor",
                        "owner_key": "mcp-worker",
                        "claim_token": "wrong-token",
                        "epoch": claim["epoch"],
                    },
                }
                if compact
                else {
                    "ticket_id": _uuid(ticket.id),
                    "actor_project": "executor",
                    "owner_key": "mcp-worker",
                    "claim_token": "wrong-token",
                    "epoch": claim["epoch"],
                }
            ),
            raise_on_error=False,
        )
        wrong_epoch = await client.call_tool(
            "brain_call_tool" if compact else "brain_delivery_claim_renew",
            (
                {
                    "name": "brain_delivery_claim_renew",
                    "arguments": {
                        "ticket_id": _uuid(ticket.id),
                        "actor_project": "executor",
                        "owner_key": "mcp-worker",
                        "claim_token": claim["claim_token"],
                        "epoch": claim["epoch"] + 1,
                        "ttl_seconds": 60,
                    },
                }
                if compact
                else {
                    "ticket_id": _uuid(ticket.id),
                    "actor_project": "executor",
                    "owner_key": "mcp-worker",
                    "claim_token": claim["claim_token"],
                    "epoch": claim["epoch"] + 1,
                    "ttl_seconds": 60,
                }
            ),
            raise_on_error=False,
        )
        assert wrong_token.is_error and wrong_epoch.is_error
        renewed = await _call(
            client,
            "brain_delivery_claim_renew",
            {
                "ticket_id": _uuid(ticket.id),
                "actor_project": "executor",
                "owner_key": "mcp-worker",
                "claim_token": claim["claim_token"],
                "epoch": claim["epoch"],
                "ttl_seconds": 60,
            },
            compact=compact,
        )
        released = await _call(
            client,
            "brain_delivery_claim_release",
            {
                "ticket_id": _uuid(ticket.id),
                "actor_project": "executor",
                "owner_key": "mcp-worker",
                "claim_token": claim["claim_token"],
                "epoch": renewed["epoch"],
            },
            compact=compact,
        )
        assert released["owner"] is None and released["expires_at"] is None
        binding = await _call(
            client,
            "brain_delivery_bind_pr",
            {
                "ticket_id": _uuid(ticket.id),
                "actor_project": "executor",
                "deliverable_key": "implementation",
                "repository_id": RID,
                "pr_number": 42,
                "expected_revision": 1,
                "expected_workflow_version": initial["assessment"]["assessment_version"],
                "idempotency_key": f"mcp-binding-{ticket.id}",
            },
            compact=compact,
        )
        assert ArtifactBinding.model_validate(binding).repository_id == RID
        refreshed = await _call(
            client,
            "brain_delivery_refresh",
            {
                "ticket_id": _uuid(ticket.id),
                "actor_project": "requester",
            },
            compact=compact,
        )
        assert refreshed["status"] == "queued"
        amended = await _call(
            client,
            "brain_delivery_contract_set",
            {
                "ticket_id": _uuid(ticket.id),
                "actor_project": "requester",
                "contract": contract,
                "expected_revision": 1,
                "idempotency_key": f"mcp-amend-{ticket.id}",
                "reason": "The requester adds a durable amendment rationale.",
            },
            compact=compact,
        )
        history = await _call(
            client,
            "brain_delivery_get",
            {
                "ticket_id": _uuid(ticket.id),
                "actor_project": "requester",
                "history_limit": 20,
            },
            compact=compact,
        )
    captured = capsys.readouterr()
    assert claim["claim_token"] not in caplog.text + captured.out + captured.err
    assert amended["amendment_reason"] == "The requester adds a durable amendment rationale."
    assert any(item["kind"] == "contract_revision" for item in history["history"]["items"])


async def test_acceptance_uses_only_header_identity_and_requires_exact_proof(
    delivery_mcp, session_factory, monkeypatch
):
    ticket = await _ticket(session_factory)
    contract = _contract(mode="explicit").model_dump(mode="json")
    async with Client(delivery_mcp) as client:
        compact = await _profile(client)
        await _call(
            client,
            "brain_delivery_contract_set",
            {
                "ticket_id": _uuid(ticket.id),
                "actor_project": "requester",
                "contract": contract,
                "expected_revision": 0,
                "idempotency_key": f"mcp-accept-contract-{ticket.id}",
                "reason": "An explicit requester approval is required.",
            },
            compact=compact,
        )
        before_proof = await client.call_tool(
            "brain_call_tool" if compact else "brain_delivery_accept",
            (
                {
                    "name": "brain_delivery_accept",
                    "arguments": {
                        "ticket_id": _uuid(ticket.id),
                        "actor_project": "requester",
                        "rationale": "not yet",
                        "expected_revision": 1,
                        "expected_attempt": 1,
                        "expected_delivery_digest": "a" * 64,
                    },
                }
                if compact
                else {
                    "ticket_id": _uuid(ticket.id),
                    "actor_project": "requester",
                    "rationale": "not yet",
                    "expected_revision": 1,
                    "expected_attempt": 1,
                    "expected_delivery_digest": "a" * 64,
                }
            ),
            raise_on_error=False,
        )
        assert before_proof.is_error and "acceptance" in str(before_proof.content)
        bound = await _call(
            client,
            "brain_delivery_bind_pr",
            {
                "ticket_id": _uuid(ticket.id),
                "actor_project": "executor",
                "deliverable_key": "implementation",
                "repository_id": RID,
                "pr_number": 42,
                "expected_revision": 1,
                "expected_workflow_version": 1,
                "idempotency_key": f"mcp-accept-binding-{ticket.id}",
            },
            compact=compact,
        )
        integration = await _publish_and_issue(
            session_factory, ticket.id, ArtifactBinding.model_validate(bound)
        )
        from brain_v42.mcp import provenance_middleware

        monkeypatch.setattr(
            provenance_middleware, "get_http_headers", lambda **_kw: {"x-brain-agent": "unknown"}
        )
        unknown = await client.call_tool(
            "brain_call_tool" if compact else "brain_delivery_accept",
            (
                {
                    "name": "brain_delivery_accept",
                    "arguments": {
                        "ticket_id": _uuid(ticket.id),
                        "actor_project": "requester",
                        "rationale": "Unknown callers cannot approve.",
                        "expected_revision": integration.contract_revision,
                        "expected_attempt": integration.attempt,
                        "expected_delivery_digest": integration.delivery_digest,
                    },
                }
                if compact
                else {
                    "ticket_id": _uuid(ticket.id),
                    "actor_project": "requester",
                    "rationale": "Unknown callers cannot approve.",
                    "expected_revision": integration.contract_revision,
                    "expected_attempt": integration.attempt,
                    "expected_delivery_digest": integration.delivery_digest,
                }
            ),
            raise_on_error=False,
        )
        assert unknown.is_error and "invalid_acceptance" in str(unknown.content)
        monkeypatch.setattr(
            provenance_middleware,
            "get_http_headers",
            lambda **_kw: {"x-brain-agent": "mcp-requester"},
        )
        receipt = await _call(
            client,
            "brain_delivery_accept",
            {
                "ticket_id": _uuid(ticket.id),
                "actor_project": "requester",
                "rationale": "The immutable integration receipt satisfies the explicit contract.",
                "expected_revision": integration.contract_revision,
                "expected_attempt": integration.attempt,
                "expected_delivery_digest": integration.delivery_digest,
            },
            compact=compact,
        )
    assert receipt["proof"]["issuer"]["issuer_identity"] == "mcp-requester"


async def test_stale_cas_wrong_project_and_legacy_transition_are_refused(
    delivery_mcp, session_factory
):
    ticket = await _ticket(session_factory)
    contract = _contract(mode="explicit").model_dump(mode="json")
    async with Client(delivery_mcp) as client:
        compact = await _profile(client)
        await _call(
            client,
            "brain_delivery_contract_set",
            {
                "ticket_id": _uuid(ticket.id),
                "actor_project": "requester",
                "contract": contract,
                "expected_revision": 0,
                "idempotency_key": f"mcp-guard-contract-{ticket.id}",
                "reason": "Initial contract.",
            },
            compact=compact,
        )
        stale = await client.call_tool(
            "brain_delivery_contract_set",
            {
                "ticket_id": _uuid(ticket.id),
                "actor_project": "requester",
                "contract": contract,
                "expected_revision": 0,
                "idempotency_key": f"mcp-stale-{ticket.id}",
                "reason": "Stale update.",
            },
            raise_on_error=False,
        )
        wrong = await client.call_tool(
            "brain_delivery_get",
            {
                "ticket_id": _uuid(ticket.id),
                "actor_project": "outsider",
            },
            raise_on_error=False,
        )
        legacy = await client.call_tool(
            "brain_ticket_transition",
            {
                "ticket_id": _uuid(ticket.id),
                "author_project": "executor",
                "action": "resolve",
            },
            raise_on_error=False,
        )
    assert stale.is_error and "revision" in str(stale.content)
    assert wrong.is_error and "not_allowed" in str(wrong.content)
    assert "delivery_requirements_unsatisfied" in str(legacy.content)
    assert (await PgTicketRepo(session_factory).get_by_id(ticket.id)).status is TicketStatus.OPEN


@pytest.mark.parametrize("delivery_mcp", [{"enabled": False}], indirect=True)
async def test_paused_delivery_keeps_reads_but_refuses_refresh(delivery_mcp, session_factory):
    ticket, _binding, _service = await _workflow(session_factory)
    async with Client(delivery_mcp) as client:
        view = await _call(
            client,
            "brain_delivery_get",
            {"ticket_id": _uuid(ticket.id), "actor_project": "requester"},
            compact=False,
        )
        refresh = await client.call_tool(
            "brain_delivery_refresh",
            {"ticket_id": _uuid(ticket.id), "actor_project": "requester"},
            raise_on_error=False,
        )
        legacy = await client.call_tool(
            "brain_ticket_transition",
            {"ticket_id": _uuid(ticket.id), "author_project": "executor", "action": "resolve"},
            raise_on_error=False,
        )
    assert view["assessment"]["observation_health"] == "disabled"
    assert refresh.is_error and "delivery_disabled" in str(refresh.content)
    assert "delivery_requirements_unsatisfied" in str(legacy.content)
    assert (await PgTicketRepo(session_factory).get_by_id(ticket.id)).status is TicketStatus.OPEN


@pytest.mark.parametrize("delivery_mcp", ["native", "compact"], indirect=True)
@pytest.mark.parametrize("tool_name", sorted(MUTATING_DELIVERY_TOOLS))
async def test_dream_scoped_token_denies_delivery_mutations_direct_and_compact_proxy(
    delivery_mcp, monkeypatch, tool_name
):
    """Actual Dream middleware rejects direct and gateway calls before handlers."""
    from brain_v42.mcp import dream_capabilities, tool_catalog

    access = AccessToken(
        token="test-scoped-token",
        client_id="dream-codex-scan",
        scopes=["brain:dream"],
        claims={
            "type": "scoped",
            "agent": "dream-codex-scan",
            "phase": "scan",
            "project_key": "brain-v42",
        },
    )
    monkeypatch.setattr(dream_capabilities, "get_access_token", lambda: access)
    monkeypatch.setattr(tool_catalog, "get_access_token", lambda: access)
    delivery_mcp.add_middleware(dream_capabilities.DreamCapabilityMiddleware())
    async with Client(delivery_mcp) as client:
        direct = await client.call_tool(
            tool_name,
            {"ticket_id": str(uuid4()), "actor_project": "executor"},
            raise_on_error=False,
        )
        proxy = await client.call_tool(
            "brain_call_tool",
            {
                "name": tool_name,
                "arguments": {"ticket_id": str(uuid4()), "actor_project": "executor"},
            },
            raise_on_error=False,
        )
    assert direct.is_error and "Dream capability authorization denied" in str(direct.content)
    assert proxy.is_error and "Dream capability authorization denied" in str(proxy.content)
