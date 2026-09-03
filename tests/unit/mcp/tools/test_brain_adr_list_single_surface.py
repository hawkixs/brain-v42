"""The ADR list has ONE public surface: `brain_list(entity_type="adr")`.

This file replaces `test_brain_adr_list_alias.py`, which pinned that the
catalogue carried TWO entries for one behaviour — `brain_list_adrs` and
`brain_list(entity_type="adr")` — and that both reached the same adapter.
Ticket af3b58dd item 3 removes the alias, so the contract flips: what used to
be "both surfaces agree" is now "the second surface does not exist", plus the
behavioural cases the alias file and `test_brain_adr_tools.py` used to pin on
the alias, carried over to the surface that remains.

WHY REMOVAL RATHER THAN A LONGER MIGRATION WINDOW. `docs/MCP_TOOLS.md` asked
for "a later ticket grounded in usage evidence". Measured on 2026-09-03 across
1 513 Dream event logs (2026-07-13 to 2026-09-03), 208 of them PROMOTE:

    grep -o '"tool":"brain_list_adrs"' logs/dream/*.events.jsonl | wc -l   -> 0
    grep -o '"tool":"brain_propose_adr"' logs/dream/*.events.jsonl | wc -l -> 88
    grep -o '"tool":"brain_list"' logs/dream/*.events.jsonl | wc -l        -> 17586

The alias was kept because the Dream rail NAMED it — in the capability
allowlist, in the scope policy, in the PROMOTE prompt's tool list. It never
CALLED it once. Naming is not usage.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastmcp import FastMCP

from brain_v42.mcp.tools import brain_tools, crud_tools
from brain_v42.models.adr import ADR

ADR_STATUSES = ("proposed", "accepted", "deprecated", "superseded")


def _adr(**overrides: Any) -> ADR:
    payload: dict[str, Any] = {
        "id": uuid4(),
        "number": 7,
        "title": "Shared ADR listing",
        "context": "Keep one listing surface.",
        "decision": "Use one adapter.",
        "consequences": "No public contract drift.",
        "project_key": "alias-project",
        "status": "accepted",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    payload.update(overrides)
    return ADR.model_validate(payload)


def _server(adrs: list[ADR] | None = None) -> tuple[FastMCP, MagicMock]:
    server = FastMCP("adr-list-surface")
    adr_svc = MagicMock()
    adr_svc.list_all = AsyncMock(return_value=[_adr()] if adrs is None else adrs)
    service = MagicMock()
    brain_tools.register_tools(
        server,
        decision_svc=service,
        learning_svc=service,
        snippet_svc=service,
        runbook_svc=service,
        adr_svc=adr_svc,
        project_context_svc=service,
        brain_svc=service,
    )
    crud_tools.register_crud_tools(
        server,
        decision_svc=service,
        learning_svc=service,
        snippet_svc=service,
        runbook_svc=service,
        adr_svc=adr_svc,
        session_factory=MagicMock(),
    )
    return server, adr_svc


async def _list_adrs(server: FastMCP):  # noqa: ANN202 - FastMCP tool callable
    tool = await server.get_tool("brain_list")
    assert tool is not None
    return cast(Any, tool).fn


# ── the removal itself ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_alias_is_no_longer_registered() -> None:
    server, _ = _server()

    assert await server.get_tool("brain_list_adrs") is None


@pytest.mark.asyncio
async def test_the_canonical_listing_survives_the_removal() -> None:
    """Guard on the assertion above: absence proves nothing if both are absent."""
    server, _ = _server()

    assert await server.get_tool("brain_list") is not None


# ── the contract the alias used to share ─────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ADR_STATUSES)
@pytest.mark.parametrize("limit", [0, 1, 100, 101])
@pytest.mark.parametrize("offset", [0, 9])
@pytest.mark.parametrize("project_key", [None, "alias-project"])
async def test_the_single_surface_still_goes_through_the_shared_adapter(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    limit: int,
    offset: int,
    project_key: str | None,
) -> None:
    """One request, one adapter call, the caller's raw limit handed to the cap.

    The cap seam is a NAMED function (ticket af3b58dd): spying on it says more
    than spying on `max`/`min`, and survives neighbouring arithmetic.
    """
    original_builder = crud_tools._build_adr_list_adapter
    original_canonicalize = crud_tools.canonicalize_project_key
    original_clamp = crud_tools.clamp_list_limit
    built_adapters: list[object] = []
    adapter_calls: list[tuple[str | None, str | None, int, int, bool]] = []
    canonicalize_calls: list[tuple[str | None, bool]] = []
    clamp_calls: list[int] = []

    def observed_builder(
        adr_svc: MagicMock,
    ) -> Callable[[str | None, str | None, int, int, bool], Awaitable[str]]:
        adapter = original_builder(adr_svc)
        built_adapters.append(adapter)

        async def observed_adapter(
            adapter_project_key: str | None,
            adapter_status: str | None,
            adapter_limit: int,
            adapter_offset: int,
            adapter_include_archived: bool,
        ) -> str:
            adapter_calls.append(
                (
                    adapter_project_key,
                    adapter_status,
                    adapter_limit,
                    adapter_offset,
                    adapter_include_archived,
                )
            )
            return await adapter(
                adapter_project_key,
                adapter_status,
                adapter_limit,
                adapter_offset,
                adapter_include_archived,
            )

        return observed_adapter

    def observed_canonicalize(value: str | None, *, strict: bool) -> str | None:
        canonicalize_calls.append((value, strict))
        return original_canonicalize(value, strict=strict)

    def observed_clamp(value: int, maximum: int = 100) -> tuple[int, str]:
        clamp_calls.append(value)
        return original_clamp(value, maximum)

    monkeypatch.setattr(crud_tools, "_build_adr_list_adapter", observed_builder)
    monkeypatch.setattr(crud_tools, "canonicalize_project_key", observed_canonicalize)
    monkeypatch.setattr(crud_tools, "clamp_list_limit", observed_clamp)
    server, adr_svc = _server()

    # Exactly ONE adapter is built now — the second one belonged to the alias.
    assert len(built_adapters) == 1

    listing = await _list_adrs(server)
    await listing(
        entity_type="adr",
        project_key=project_key,
        status=status,
        limit=limit,
        offset=offset,
        include_archived=False,
    )

    assert len(adapter_calls) == 1
    assert canonicalize_calls == [(project_key, False)]
    assert clamp_calls == [limit]
    adr_svc.list_all.assert_awaited_once_with(
        project_key=project_key,
        status=status,
        limit=max(1, min(limit, 100)),
        offset=offset,
        include_archived=False,
    )


# ── behaviours ported from the alias's own test class ────────────────────────


@pytest.mark.asyncio
async def test_filters_reach_the_service_unchanged() -> None:
    server, adr_svc = _server()
    listing = await _list_adrs(server)

    await listing(entity_type="adr", project_key="brain-v42", status="proposed", limit=10)

    adr_svc.list_all.assert_awaited_once_with(
        project_key="brain-v42",
        status="proposed",
        limit=10,
        offset=0,
        include_archived=False,
    )


@pytest.mark.asyncio
async def test_absent_filters_are_passed_as_none_with_the_default_limit() -> None:
    server, adr_svc = _server([])
    listing = await _list_adrs(server)

    await listing(entity_type="adr")

    adr_svc.list_all.assert_awaited_once_with(
        project_key=None,
        status=None,
        limit=20,
        offset=0,
        include_archived=False,
    )


@pytest.mark.asyncio
async def test_offset_reaches_the_service() -> None:
    server, adr_svc = _server([])
    listing = await _list_adrs(server)

    await listing(entity_type="adr", offset=5)

    assert adr_svc.list_all.await_args.kwargs["offset"] == 5


@pytest.mark.asyncio
async def test_result_names_the_adr_and_its_short_id() -> None:
    adr = _adr(title="Decision X")
    server, _ = _server([adr])
    listing = await _list_adrs(server)

    result = await listing(entity_type="adr")

    assert "1 ADR" in result
    assert "Decision X" in result
    assert str(adr.id)[:8] in result


@pytest.mark.asyncio
async def test_empty_result_is_reported_as_zero_not_as_an_error() -> None:
    server, _ = _server([])
    listing = await _list_adrs(server)

    result = await listing(entity_type="adr")

    assert "0 ADRs found" in result
