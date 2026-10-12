"""Input and exception redaction at the actual prepared delivery MCP boundary."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastmcp import Client, FastMCP

from brain_v42.mcp.server import prepare_tools_for_transport
from brain_v42.mcp.tool_catalog import apply_tool_catalog_profile
from brain_v42.models.delivery import DeliveryError

MARKER = "delivery-claim-secret-that-must-never-be-logged"


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_list_defaults_to_summary_and_full_is_opt_in(profile):
    from brain_v42.models.delivery import DeliveryPage, summarize_view
    from tests.unit.models.test_delivery_summary import summary_view

    view = summary_view()
    service = AsyncMock()
    service.list.return_value = DeliveryPage(items=(view,), next_cursor="next", omitted_count=3)
    app = await _app(service, profile)
    for detail in (None, "full"):
        arguments = {"actor_project": "brain-v42", "work": "repair", "limit": 5}
        if detail is not None:
            arguments["detail"] = detail
        result = await _call(app, profile, "brain_delivery_list", arguments)
        assert not result.is_error
        payload = result.structured_content
        assert payload["items"] == [
            summarize_view(view, include_view=detail == "full").model_dump(mode="json")
        ]
        assert payload["next_cursor"] == "next" and payload["omitted_count"] == 3
    service.list.assert_awaited_with(
        actor_project="brain-v42", limit=5, cursor=None, work="repair", blocker=None, stage=None
    )


async def test_list_is_published_as_version_two():
    app = await _app(AsyncMock(), "native")
    async with Client(app) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    tool = tools["brain_delivery_list"]
    assert tool.meta["fastmcp"]["version"] == "2.0"
    assert tool.outputSchema["type"] == "object"
    assert tool.inputSchema["properties"]["detail"]["default"] == "summary"


async def _app(service, profile):
    from brain_v42.mcp.tools.delivery_tools import register_delivery_tools

    app = FastMCP("delivery-errors", mask_error_details=True)
    register_delivery_tools(app, delivery_svc=service)
    apply_tool_catalog_profile(app, profile)
    await prepare_tools_for_transport(app, None)
    return app


async def _call(app, profile, name, arguments):
    async with Client(app) as client:
        if profile == "compact":
            return await client.call_tool(
                "brain_call_tool", {"name": name, "arguments": arguments}, raise_on_error=False
            )
        return await client.call_tool(name, arguments, raise_on_error=False)


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_both_attestation_tools_are_published_as_version_one(profile):
    service = AsyncMock()
    app = await _app(service, profile)
    async with Client(app) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        if profile == "native":
            for name in ("brain_delivery_attest", "brain_delivery_attestation_list"):
                assert tools[name].meta["fastmcp"]["version"] == "1.0"
                required = tools[name].inputSchema["required"]
                assert "actor_project" in required
                # The list reads one ticket OR one project: its ticket is optional.
                assert ("ticket_id" in required) == (name == "brain_delivery_attest")
        else:
            assert "brain_delivery_attest" not in tools
            found: set[str] = set()
            for name in ("brain_delivery_attest", "brain_delivery_attestation_list"):
                result = await client.call_tool("brain_find_tool", {"query": name})
                found.update(item["name"] for item in result.data)
            assert {"brain_delivery_attest", "brain_delivery_attestation_list"} <= found


@pytest.mark.parametrize("profile", ["native", "compact"])
@pytest.mark.parametrize("operation", ["renew", "release"])
async def test_missing_argument_never_echoes_the_claim_token(profile, operation, caplog):
    service = AsyncMock()
    app = await _app(service, profile)
    result = await _call(
        app,
        profile,
        f"brain_delivery_claim_{operation}",
        {
            "ticket_id": str(uuid4()),
            "actor_project": "executor",
            "owner_key": "worker",
            "claim_token": MARKER,
            # The validation error's input would contain this entire payload.
        },
    )
    assert result.is_error and "invalid_arguments" in str(result.content)
    assert MARKER not in str(result.content)
    assert MARKER not in caplog.text
    service.renew_claim.assert_not_called()
    service.release_claim.assert_not_called()


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_unexpected_service_exception_never_reaches_framework_traceback(profile, caplog):
    service = AsyncMock()
    service.renew_claim.side_effect = RuntimeError(MARKER)
    app = await _app(service, profile)
    result = await _call(
        app,
        profile,
        "brain_delivery_claim_renew",
        {
            "ticket_id": str(uuid4()),
            "actor_project": "executor",
            "owner_key": "worker",
            "claim_token": MARKER,
            "epoch": 1,
        },
    )
    service.renew_claim.assert_awaited_once()
    assert result.is_error and "delivery_unavailable" in str(result.content)
    assert MARKER not in str(result.content)
    assert MARKER not in caplog.text
    assert not any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_domain_refusal_keeps_safe_stable_code(profile, caplog):
    service = AsyncMock()
    service.release_claim.side_effect = DeliveryError(
        "claim_fenced", "delivery claim is no longer current"
    )
    app = await _app(service, profile)
    result = await _call(
        app,
        profile,
        "brain_delivery_claim_release",
        {
            "ticket_id": str(uuid4()),
            "actor_project": "executor",
            "owner_key": "worker",
            "claim_token": MARKER,
            "epoch": 1,
        },
    )
    assert result.is_error and "claim_fenced" in str(result.content)
    assert MARKER not in str(result.content) + caplog.text


@pytest.mark.parametrize("ticket_id", ["abcd1234", "a" * 32, "not-a-uuid"])
async def test_full_uuid_is_required_before_a_mutation(ticket_id):
    service = AsyncMock()
    app = await _app(service, "native")
    result = await _call(
        app, "native", "brain_delivery_refresh", {"ticket_id": ticket_id, "actor_project": "p"}
    )
    assert result.is_error and "invalid_arguments" in str(result.content)
    service.refresh.assert_not_called()


@pytest.mark.parametrize("epoch", [True, 1.5, "1"])
async def test_claim_epoch_rejects_coercion(epoch):
    service = AsyncMock()
    app = await _app(service, "native")
    result = await _call(
        app,
        "native",
        "brain_delivery_claim_release",
        {
            "ticket_id": str(uuid4()),
            "actor_project": "executor",
            "owner_key": "worker",
            "claim_token": MARKER,
            "epoch": epoch,
        },
    )
    assert result.is_error and "invalid_arguments" in str(result.content)
    service.release_claim.assert_not_called()


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_sidecar_failure_never_logs_claim_input_or_exception_text(
    profile, monkeypatch, caplog, capsys
):
    from types import SimpleNamespace

    from brain_v42.mcp import provenance_middleware
    from brain_v42.mcp.server import create_mcp_instance
    from brain_v42.mcp.tools.delivery_tools import register_delivery_tools

    def fail_report(*_args):
        raise RuntimeError(MARKER)

    monkeypatch.setattr(
        provenance_middleware,
        "get_http_headers",
        lambda **_kwargs: {"x-brain-agent": "delivery-boundary-test"},
    )
    monkeypatch.setattr(
        provenance_middleware, "get_activity_reporter", lambda: SimpleNamespace(report=fail_report)
    )
    app = create_mcp_instance()
    register_delivery_tools(app, delivery_svc=AsyncMock())
    apply_tool_catalog_profile(app, profile)
    await prepare_tools_for_transport(app, None)
    result = await _call(
        app,
        profile,
        "brain_delivery_claim_renew",
        {
            "ticket_id": str(uuid4()),
            "actor_project": "executor",
            "owner_key": "worker",
            "claim_token": MARKER,
        },
    )
    captured = capsys.readouterr()
    assert result.is_error and "invalid_arguments" in str(result.content)
    assert MARKER not in str(result.content) + caplog.text + captured.out + captured.err


async def test_the_generic_attest_tool_refuses_the_reserved_observer_identity(monkeypatch):
    """A declared X-Brain-Agent must not be able to impersonate the delivery observer."""
    from brain_v42.repositories.pg_release_derivation import OBSERVER_IDENTITY

    monkeypatch.setattr(
        "brain_v42.mcp.tools.delivery_tools.get_current_actor", lambda: OBSERVER_IDENTITY
    )
    service = AsyncMock()
    app = await _app(service, "native")
    result = await _call(
        app,
        "native",
        "brain_delivery_attest",
        {
            "ticket_id": str(uuid4()),
            "actor_project": "executor",
            "kind": "released",
            "payload": {},
            "idempotency_key": "k",
            "emitted_at": "2026-10-03T12:00:00+00:00",
        },
    )
    assert result.is_error and "issuer_identity_reserved" in str(result.content)
    service.attest.assert_not_called()


def _attestation_arguments():
    return {
        "ticket_id": str(uuid4()),
        "actor_project": "executor",
        "kind": "gate_passed",
        "payload": {"gate": "unit"},
        "idempotency_key": "gate:unit",
        "emitted_at": "2026-10-03T12:00:00+00:00",
    }


def _issuer_context(monkeypatch, *, actor="agent:claude", issuers=None):
    from fastmcp.server.auth import AccessToken

    from brain_v42.mcp.tools import delivery_tools

    access = (
        None
        if issuers is None
        else AccessToken(
            token="",
            client_id="client-service",
            scopes=["delivery"],
            claims={"credential_id": str(uuid4()), "issuers": issuers},
        )
    )
    monkeypatch.setattr(delivery_tools, "get_current_actor", lambda: actor)
    monkeypatch.setattr(
        delivery_tools,
        "get_current_principal",
        lambda: None if access is None else access.client_id,
        raising=False,
    )
    monkeypatch.setattr(delivery_tools, "get_access_token", lambda: access, raising=False)


def _issuer_service():
    from datetime import UTC, datetime

    from brain_v42.models.delivery import DeliveryAttestation
    from brain_v42.models.delivery_hashes import canonical_digest

    async def attest(ticket_id, **kwargs):
        return DeliveryAttestation(
            ticket_id=ticket_id,
            issuer_project=kwargs["actor_project"],
            issuer_identity=kwargs["caller_identity"],
            kind=kwargs["kind"],
            payload=kwargs["payload"],
            digest=canonical_digest(kwargs["payload"], domain="attestation"),
            idempotency_key=kwargs["idempotency_key"],
            emitted_at=datetime.fromisoformat(kwargs["emitted_at"]),
            recorded_at=datetime(2026, 10, 3, 12, tzinfo=UTC),
        )

    service = AsyncMock()
    service.attest.side_effect = attest
    # Pin the identity passed to acceptance without inventing integration evidence.
    service.accept.side_effect = DeliveryError("delivery_disabled", "delivery is disabled")
    return service


def _issuer_arguments(operation, issuer):
    arguments = (
        _attestation_arguments()
        if operation == "attest"
        else {
            "ticket_id": str(uuid4()),
            "actor_project": "executor",
            "rationale": "verified",
            "expected_revision": 1,
            "expected_attempt": 1,
            "expected_delivery_digest": "a" * 64,
        }
    )
    if issuer is not None:
        arguments["issuer"] = issuer
    return arguments


@pytest.mark.parametrize("operation", ["attest", "accept"])
async def test_issuer_is_an_optional_top_level_argument(operation):
    app = await _app(AsyncMock(), "native")
    async with Client(app) as client:
        tool = next(t for t in await client.list_tools() if t.name == f"brain_delivery_{operation}")
    schema = tool.inputSchema
    assert "issuer" in schema["properties"]
    assert schema["properties"]["issuer"]["default"] is None
    assert "issuer" not in schema["required"]


@pytest.mark.parametrize("profile", ["native", "compact"])
@pytest.mark.parametrize("operation", ["attest", "accept"])
@pytest.mark.parametrize(
    "actor,issuer,patterns,expected",
    [
        ("agent:claude", "release-bot", ["release-bot"], "release-bot"),
        ("agent:claude", "service:dream", ["service:*"], "service:dream"),
        ("agent:claude", None, ["agent:*"], "agent:claude"),
        ("client-service", None, [], "client-service"),
    ],
)
async def test_credentials_store_only_the_authorized_issuer(
    monkeypatch, profile, operation, actor, issuer, patterns, expected
):
    _issuer_context(monkeypatch, actor=actor, issuers=patterns)
    service = _issuer_service()
    app = await _app(service, profile)
    result = await _call(
        app, profile, f"brain_delivery_{operation}", _issuer_arguments(operation, issuer)
    )
    method = getattr(service, operation)
    method.assert_awaited_once()
    assert method.call_args.kwargs["caller_identity"] == expected
    if operation == "attest":
        assert not result.is_error
        assert result.structured_content["issuer_identity"] == expected
    else:
        assert "delivery_disabled" in str(result.content)


@pytest.mark.parametrize("profile", ["native", "compact"])
@pytest.mark.parametrize("operation", ["attest", "accept"])
@pytest.mark.parametrize(
    "actor,issuer,patterns",
    [
        ("agent:claude", "other-bot", ["agent:*"]),
        ("agent:claude", None, []),
        ("agent:claude", "client-service", []),
        ("agent:claude", "executor", ["@project"]),
        ("agent:claude", "agent:other", ["agent:claude"]),
        ("agent:claude", "agent:other", ["agent:**", "agent:*:other"]),
    ],
)
async def test_credentials_refuse_an_issuer_outside_the_allowlist(
    monkeypatch, profile, operation, actor, issuer, patterns
):
    from structlog.testing import capture_logs

    from brain_v42.credentials.reasons import refusal_counts, reset_refusal_counts

    _issuer_context(monkeypatch, actor=actor, issuers=patterns)
    service = _issuer_service()
    app = await _app(service, profile)
    reset_refusal_counts()
    with capture_logs() as logs:
        result = await _call(
            app, profile, f"brain_delivery_{operation}", _issuer_arguments(operation, issuer)
        )
    assert result.is_error and "issuer_not_allowed" in str(result.content)
    getattr(service, operation).assert_not_called()
    events = [event for event in logs if event["event"] == "mcp_auth.refused"]
    assert len(events) == 1 and events[0]["status"] == 403
    assert refusal_counts() == {"issuer_not_allowed": 1}
    reset_refusal_counts()


@pytest.mark.parametrize("operation", ["attest", "accept"])
@pytest.mark.parametrize("issuer,allowed", [(None, True), ("agent:claude", True), ("other", False)])
async def test_shared_token_can_only_declare_its_actor(monkeypatch, operation, issuer, allowed):
    _issuer_context(monkeypatch)
    service = _issuer_service()
    app = await _app(service, "native")
    result = await _call(
        app, "native", f"brain_delivery_{operation}", _issuer_arguments(operation, issuer)
    )
    if allowed:
        assert getattr(service, operation).call_args.kwargs["caller_identity"] == "agent:claude"
    else:
        assert result.is_error and "issuer_not_allowed" in str(result.content)
        getattr(service, operation).assert_not_called()


@pytest.mark.parametrize("operation", ["attest", "accept"])
@pytest.mark.parametrize("issuer", [":agent", "a b", "a/b", "a" * 65, "", 7])
async def test_explicit_issuer_uses_bounded_actor_label_grammar(monkeypatch, operation, issuer):
    _issuer_context(monkeypatch, issuers=["*"])
    service = _issuer_service()
    app = await _app(service, "native")
    result = await _call(
        app, "native", f"brain_delivery_{operation}", _issuer_arguments(operation, issuer)
    )
    assert result.is_error and "invalid_arguments" in str(result.content)
    getattr(service, operation).assert_not_called()


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_optional_issuer_never_changes_the_payload_or_its_digest(monkeypatch, profile):
    _issuer_context(monkeypatch, issuers=["agent:*", "release-bot"])
    service = _issuer_service()
    app = await _app(service, profile)
    arguments = _attestation_arguments()
    implicit = await _call(app, profile, "brain_delivery_attest", arguments)
    explicit = await _call(
        app, profile, "brain_delivery_attest", arguments | {"issuer": "release-bot"}
    )
    assert not implicit.is_error and not explicit.is_error
    assert implicit.structured_content["issuer_identity"] == "agent:claude"
    assert explicit.structured_content["issuer_identity"] == "release-bot"
    assert implicit.structured_content["digest"] == explicit.structured_content["digest"]
    for result in (implicit, explicit):
        assert result.structured_content["payload"] == arguments["payload"]
    for call in service.attest.call_args_list:
        assert call.kwargs["payload"] == arguments["payload"]
        assert "issuer" not in call.kwargs
