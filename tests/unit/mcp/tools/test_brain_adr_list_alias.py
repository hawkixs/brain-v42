"""FastMCP contract tests for the ADR-list compatibility alias."""

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


def _adr() -> ADR:
    return ADR.model_validate(
        {
            "id": uuid4(),
            "number": 7,
            "title": "Shared ADR listing",
            "context": "Keep a compatibility alias during migration.",
            "decision": "Use one adapter.",
            "consequences": "No public contract drift.",
            "project_key": "alias-project",
            "status": "accepted",
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
    )


def _server() -> tuple[FastMCP, MagicMock]:
    server = FastMCP("adr-list-alias")
    adr_svc = MagicMock()
    adr_svc.list_all = AsyncMock(return_value=[_adr()])
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


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ADR_STATUSES)
@pytest.mark.parametrize("limit", [0, 1, 100, 101])
@pytest.mark.parametrize("offset", [0, 9])
@pytest.mark.parametrize("project_key", [None, "alias-project"])
async def test_fastmcp_adr_alias_matches_canonical_list_contract(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    limit: int,
    offset: int,
    project_key: str | None,
) -> None:
    """Both FastMCP surfaces invoke one shared ADR adapter per request."""
    original_builder = crud_tools._build_adr_list_adapter
    original_canonicalize = crud_tools.canonicalize_project_key
    original_clamp = crud_tools.clamp_list_limit
    built_adapters: list[object] = []
    adapter_calls: list[tuple[str | None, str | None, int, int, bool]] = []
    canonicalize_calls: list[tuple[str | None, bool]] = []
    # La couture du plafond est devenue une fonction NOMMÉE (ticket af3b58dd) :
    # l'espionner vaut mieux que d'espionner `max`/`min`, qui ne disaient rien de
    # l'intention et qui rendaient ce test sensible à toute arithmétique voisine.
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

    def observed_canonicalize(
        value: str | None,
        *,
        strict: bool,
    ) -> str | None:
        canonicalize_calls.append((value, strict))
        return original_canonicalize(value, strict=strict)

    def observed_clamp(value: int, maximum: int = 100) -> tuple[int, str]:
        clamp_calls.append(value)
        return original_clamp(value, maximum)

    monkeypatch.setattr(brain_tools, "_build_adr_list_adapter", observed_builder)
    monkeypatch.setattr(crud_tools, "_build_adr_list_adapter", observed_builder)
    monkeypatch.setattr(crud_tools, "canonicalize_project_key", observed_canonicalize)
    monkeypatch.setattr(crud_tools, "clamp_list_limit", observed_clamp)
    server, adr_svc = _server()
    legacy = await server.get_tool("brain_list_adrs")
    canonical = await server.get_tool("brain_list")

    assert legacy is not None
    assert canonical is not None
    assert len(built_adapters) == 2

    legacy_result = await cast(Any, legacy).fn(
        project_key=project_key,
        status=status,
        limit=limit,
        offset=offset,
    )
    assert len(adapter_calls) == 1
    assert canonicalize_calls == [(project_key, False)]
    # L'alias doit demander le MÊME plafond que le tool canonique, avec la valeur
    # brute de l'appelant — c'est ce qui prouve qu'il ne court-circuite pas la garde.
    assert clamp_calls == [limit]
    adr_svc.list_all.assert_awaited_once_with(
        project_key=project_key,
        status=status,
        limit=max(1, min(limit, 100)),
        offset=offset,
        include_archived=False,
    )

    adr_svc.list_all.reset_mock()
    adapter_calls.clear()
    canonicalize_calls.clear()
    clamp_calls.clear()
    canonical_result = await cast(Any, canonical).fn(
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
    assert legacy_result == canonical_result
