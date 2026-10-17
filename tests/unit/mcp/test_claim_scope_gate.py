"""Scoped verification keeps the shared admission bound and source-before-claim order."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastmcp import Client, FastMCP

from brain_v42.db.tables import learnings
from brain_v42.facts import verification
from brain_v42.facts.verification import ClaimVerificationService
from brain_v42.mcp import tool_families
from brain_v42.mcp.tool_families import FamilyAuthorizationMiddleware
from brain_v42.mcp.tools import claim_tools
from tests.unit.facts.test_claim_verification import _CountingSessionFactory, _Probe, _registry
from tests.unit.mcp.test_tool_families import _scoped_access


@pytest.mark.parametrize(
    "mode,scoped", [("enforce", True), ("observe", True), ("off", True), ("enforce", False)]
)
async def test_verifications_share_admission_bound_in_every_mode(monkeypatch, mode, scoped):
    factory = _CountingSessionFactory()
    verifier = ClaimVerificationService(
        _registry(_Probe()), factory, max_concurrent_verifications=2
    )
    order = []

    async def verify_locked(*args, **kwargs):
        order.append("claim")
        return SimpleNamespace(row=object())

    async def lock_source(*args, **kwargs):
        order.append("source")
        return True

    monkeypatch.setattr(verifier, "_verify", verify_locked)
    monkeypatch.setattr(verification, "claim_source", AsyncMock(return_value=(learnings, uuid4())))
    monkeypatch.setattr(verification, "lock_checked_owner", lock_source)
    monkeypatch.setattr(claim_tools, "get_current_actor", lambda: "test-writer")
    monkeypatch.setattr(claim_tools, "_verdict_json", lambda row: {"verdict": "holds"})
    monkeypatch.setattr(
        tool_families,
        "get_access_token",
        lambda: _scoped_access("write", projects=["brain-v42"] if scoped else None),
    )
    mcp = FastMCP()
    mcp.add_middleware(
        FamilyAuthorizationMiddleware(
            scope_mode=mode, row_project_resolver=AsyncMock(return_value="brain-v42")
        )
    )
    claim_tools.register_claim_tools(mcp, verifier)
    async with Client(mcp) as client:
        results = await asyncio.gather(
            *[
                client.call_tool(
                    "brain_claim_verify",
                    {"claim_id": str(uuid4()), "idempotency_key": f"gate-{index}"},
                )
                for index in range(8)
            ]
        )
    assert all(not result.is_error for result in results)
    assert factory.peak == 2
    assert factory.active == 0
    assert order == (["source", "claim"] * 8 if mode == "enforce" and scoped else ["claim"] * 8)
