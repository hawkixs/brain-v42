"""MCP contracts for `brain_claim_verify`: the caller names a claim, the server measures.

Spec 2026-09-19, section 6.3: there is no tool that accepts a measurement. The only
public inputs are a claim id and an idempotency key; the issuer is the MCP actor the
server resolved, the kind is `robot`, and a scoped Dream call contributes its trusted
project -- never an argument.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from datetime import UTC, datetime
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
from brain_v42.models.claim_verdict import ClaimVerificationError
from brain_v42.provenance import set_current_actor
from brain_v42.repositories.pg_claim_verdicts import VerdictRow
from brain_v42.services.dream_project_scope import (
    PROJECT_TOOL_POLICIES,
    DreamProjectScope,
    bind_dream_project_scope,
)

CLAIM_ID = UUID("7d0b1f53-4c55-4c2e-9e57-a5b1b8d0c001")


def _verdict(claim_id: UUID = CLAIM_ID) -> VerdictRow:
    return VerdictRow(
        id=UUID("5d6c7c1e-0a3e-4b8b-8e44-2f1d2f6a9001"),
        seq=41,
        claim_id=claim_id,
        verdict="holds",
        reason=None,
        measurement={"status": "measured", "value": {"lag": 3}},
        measurement_digest="d" * 64,
        observation_id=UUID("0b8f2e6c-2c1a-4d5b-9a55-8c3c1f7e9002"),
        issuer_identity="mcp:codex",
        issuer_kind="robot",
        request_fingerprint="r" * 64,
        outcome_fingerprint="o" * 64,
        idempotency_key="nightly-1",
        emitted_at=datetime(2026, 9, 22, 21, 0, 5, tzinfo=UTC),
        recorded_at=datetime(2026, 9, 22, 21, 0, 6, tzinfo=UTC),
    )


class FakeVerifier:
    """Records what reaches the coordinator, and nothing else."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    async def verify(
        self,
        claim_id: UUID,
        issuer_identity: str,
        issuer_kind: str,
        idempotency_key: str,
        *,
        project_key: str | None = None,
        session: object | None = None,
    ) -> VerdictRow:
        self.calls.append(
            {
                "claim_id": claim_id,
                "issuer_identity": issuer_identity,
                "issuer_kind": issuer_kind,
                "idempotency_key": idempotency_key,
                "project_key": project_key,
                "session": session,
            }
        )
        if self.error is not None:
            raise self.error
        return _verdict(claim_id)


@pytest.fixture(autouse=True)
def _actor() -> Iterator[None]:
    """Every request of this module comes from the `codex` MCP actor unless a test says so."""
    set_current_actor("codex")
    yield
    set_current_actor("unknown")


async def _app(verifier: FakeVerifier, profile: ToolCatalogProfile) -> FastMCP:
    from brain_v42.mcp.tools.claim_tools import register_claim_tools

    app = FastMCP("claims", mask_error_details=True)
    register_claim_tools(app, cast(ClaimVerificationService, verifier))
    apply_tool_catalog_profile(app, profile)
    await prepare_tools_for_transport(app, None)
    return app


async def _call(app: FastMCP, profile: str, arguments: dict[str, object]) -> Any:
    async with Client(app) as client:
        if profile == "compact":
            return await client.call_tool(
                "brain_call_tool",
                {"name": "brain_claim_verify", "arguments": arguments},
                raise_on_error=False,
            )
        return await client.call_tool("brain_claim_verify", arguments, raise_on_error=False)


def test_the_claim_tools_module_is_its_own_registration_root() -> None:
    spec = importlib.util.find_spec("brain_v42.mcp.tools.claim_tools")

    assert spec is not None
    from brain_v42.mcp.tools import claim_tools

    assert callable(getattr(claim_tools, "register_claim_tools", None))


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_a_verification_carries_the_server_owned_issuer_and_returns_the_full_verdict(
    profile: str,
) -> None:
    verifier = FakeVerifier()
    app = await _app(verifier, profile)

    result = await _call(app, profile, {"claim_id": str(CLAIM_ID), "idempotency_key": "nightly-1"})

    assert not result.is_error, result.content
    assert verifier.calls == [
        {
            "claim_id": CLAIM_ID,
            "issuer_identity": "mcp:codex",
            "issuer_kind": "robot",
            "idempotency_key": "nightly-1",
            "project_key": None,
            "session": None,
        }
    ]
    assert result.data == {
        "id": "5d6c7c1e-0a3e-4b8b-8e44-2f1d2f6a9001",
        "seq": 41,
        "claim_id": str(CLAIM_ID),
        "verdict": "holds",
        "reason": None,
        "measurement": {"status": "measured", "value": {"lag": 3}},
        "measurement_digest": "d" * 64,
        "observation_id": "0b8f2e6c-2c1a-4d5b-9a55-8c3c1f7e9002",
        "issuer_identity": "mcp:codex",
        "issuer_kind": "robot",
        "request_fingerprint": "r" * 64,
        "outcome_fingerprint": "o" * 64,
        "idempotency_key": "nightly-1",
        "emitted_at": "2026-09-22T21:00:05Z",
        "recorded_at": "2026-09-22T21:00:06Z",
    }


@pytest.mark.parametrize("actor", ["unknown", "_unexpanded"])
@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_an_unidentified_actor_is_refused_before_the_coordinator(
    profile: str, actor: str
) -> None:
    """`mcp:unknown` would be recorded as an issuer nobody can be held to."""
    set_current_actor(actor)
    verifier = FakeVerifier()
    app = await _app(verifier, profile)

    result = await _call(app, profile, {"claim_id": str(CLAIM_ID), "idempotency_key": "nightly-1"})

    assert result.is_error
    assert "unknown_actor" in str(result.content)
    assert verifier.calls == []


@pytest.mark.parametrize(
    ("claim_id", "key"),
    [
        ("not-a-uuid", "k"),
        (CLAIM_ID.hex, "k"),
        ("{" + str(CLAIM_ID) + "}", "k"),
        ("urn:uuid:" + str(CLAIM_ID), "k"),
        (str(CLAIM_ID).upper(), "k"),
        (str(CLAIM_ID), ""),
        (str(CLAIM_ID), "   "),
        (str(CLAIM_ID), "k" * 201),
    ],
)
@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_a_malformed_claim_id_or_key_is_refused_before_the_coordinator(
    profile: str, claim_id: str, key: str
) -> None:
    """Only the canonical form of a UUID names a claim: one claim, one spelling."""
    verifier = FakeVerifier()
    app = await _app(verifier, profile)

    result = await _call(app, profile, {"claim_id": claim_id, "idempotency_key": key})

    assert result.is_error
    assert "invalid_argument" in str(result.content)
    assert verifier.calls == []


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_extra_measurement_outcome_or_issuer_arguments_cannot_reach_the_coordinator(
    profile: str,
) -> None:
    """A caller never supplies evidence: extra keys are refused or ignored, never used."""
    verifier = FakeVerifier()
    app = await _app(verifier, profile)

    result = await _call(
        app,
        profile,
        {
            "claim_id": str(CLAIM_ID),
            "idempotency_key": "nightly-1",
            "measurement": {"status": "measured", "value": {"lag": 0}},
            "verdict": "holds",
            "emitted_at": "2026-09-22T21:00:05Z",
            "issuer_identity": "human:admin",
            "project_key": "someone-else",
        },
    )

    for call in verifier.calls:
        assert call["issuer_identity"] == "mcp:codex"
        assert call["issuer_kind"] == "robot"
        assert call["project_key"] is None
        assert set(call) == {
            "claim_id",
            "issuer_identity",
            "issuer_kind",
            "idempotency_key",
            "project_key",
            "session",
        }
    if not result.is_error:
        assert result.data["issuer_identity"] == "mcp:codex"


@pytest.mark.parametrize(
    "code",
    [
        "claim_not_found",
        "claim_retired",
        "idempotency_conflict",
        "observation_already_verified",
        "invalid_emitted_at",
        "refresh_budget_exhausted",
    ],
)
@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_a_verification_refusal_survives_masking_with_its_code(
    profile: str, code: str
) -> None:
    """The caller must learn WHY -- retired, conflicting key, already used observation."""
    verifier = FakeVerifier(error=ClaimVerificationError(code))  # type: ignore[arg-type]
    app = await _app(verifier, profile)

    result = await _call(app, profile, {"claim_id": str(CLAIM_ID), "idempotency_key": "nightly-1"})

    assert result.is_error
    assert code in str(result.content)


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_an_unexpected_failure_stays_masked(profile: str) -> None:
    """A database or probe fault must not publish its text through the tool."""
    verifier = FakeVerifier(error=RuntimeError("password=hunter2 host=10.0.0.9"))
    app = await _app(verifier, profile)

    result = await _call(app, profile, {"claim_id": str(CLAIM_ID), "idempotency_key": "nightly-1"})

    assert result.is_error
    assert "hunter2" not in str(result.content)
    assert "10.0.0.9" not in str(result.content)


async def test_a_scoped_dream_call_contributes_its_trusted_project() -> None:
    """Lot C will call through a Dream scope; its project comes from the scope, not an argument."""
    verifier = FakeVerifier()
    app = await _app(verifier, "native")
    scope = DreamProjectScope(
        project_key="brain-v42",
        resolver=cast(Any, object()),
        audit=cast(Any, object()),
        tool_name="brain_claim_verify",
    )

    with bind_dream_project_scope(scope):
        result = await _call(
            app, "native", {"claim_id": str(CLAIM_ID), "idempotency_key": "nightly-1"}
        )

    assert not result.is_error, result.content
    assert verifier.calls[0]["project_key"] == "brain-v42"


async def test_the_tool_is_a_versioned_idempotent_non_destructive_write() -> None:
    """It writes a ledger row, so it is not read-only; a replay writes nothing, so it is idempotent."""
    app = await _app(FakeVerifier(), "native")

    async with Client(app) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    tool = tools["brain_claim_verify"]
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is False
    assert tool.annotations.idempotentHint is True
    assert tool.meta["fastmcp"]["version"] == "1.0"
    assert set(tool.inputSchema["properties"]) == {"claim_id", "idempotency_key"}


async def test_compact_catalogue_search_finds_the_verification_tool() -> None:
    app = await _app(FakeVerifier(), "compact")

    async with Client(app) as client:
        result = await client.call_tool("brain_find_tool", {"query": "brain_claim_verify"})

    assert "brain_claim_verify" in {item["name"] for item in result.data}


@pytest.mark.parametrize("phase", sorted(DREAM_PHASE_TOOL_ALLOWLISTS))
def test_no_dream_phase_may_call_the_verification_tool_in_lot_b(phase: str) -> None:
    """Lot C adds the verify phase with a verified run id; until then every phase is refused."""
    assert "brain_claim_verify" not in dream_phase_tool_allowlist(phase)


def test_no_scoped_dream_policy_admits_the_verification_tool() -> None:
    assert "brain_claim_verify" not in PROJECT_TOOL_POLICIES


def test_every_refusal_code_carries_its_own_safe_text() -> None:
    """One message for six codes would tell a caller nothing it can act on."""
    codes = (
        "invalid_argument",
        "unknown_actor",
        "claim_not_found",
        "claim_retired",
        "idempotency_conflict",
        "observation_already_verified",
        "invalid_emitted_at",
        "refresh_budget_exhausted",
    )
    messages = {str(ClaimVerificationError(code)) for code in codes}  # type: ignore[arg-type]

    assert len(messages) == len(codes)
    for code in codes:
        assert str(ClaimVerificationError(code)).startswith(f"{code}: ")  # type: ignore[arg-type]


def test_build_server_wires_the_verification_tool_to_one_coordinator() -> None:
    """A registration root nobody calls is a tool that exists nowhere in production."""
    import ast
    import inspect

    from brain_v42.mcp import server

    calls = [
        node
        for node in ast.walk(ast.parse(inspect.getsource(server.build_server)))
        if isinstance(node, ast.Call)
    ]
    names = [
        node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
        for node in calls
    ]

    assert names.count("register_claim_tools") == 1
    assert names.count("ClaimVerificationService") == 1
    coordinator = next(
        node
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "ClaimVerificationService"
    )
    rendered = ast.unparse(coordinator)
    assert "fact_registry" in rendered
    assert "get_session_factory()" in rendered
