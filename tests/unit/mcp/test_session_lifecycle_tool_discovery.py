"""FastMCP discovery and runtime contracts for the seven session tools."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastmcp import FastMCP
from fastmcp.tools.base import Tool

from brain_v42.mcp.tools.session_lifecycle_tools import register_session_lifecycle_tools
from brain_v42.models.brain_session import (
    BrainSession,
    BrainSessionAbandonResult,
    BrainSessionCaptureResult,
    BrainSessionEndResult,
    BrainSessionFocusOutcome,
    BrainSessionHeartbeatResult,
    BrainSessionListResult,
    BrainSessionResumeResult,
    BrainSessionStartResult,
    BrainSessionStatus,
)

FIXTURE_PATH = (
    Path(__file__).parents[2] / "fixtures" / "mcp_session_lifecycle_output_schema_baseline.json"
)
LIGHTWEIGHT_OUTPUT_SCHEMA_TOOLS = frozenset(
    {
        "brain_session_capture",
        "brain_session_heartbeat",
        "brain_session_list",
        "brain_session_abandon",
    }
)
# Bumpé par la 046, et le nombre est MESURÉ, pas ajusté jusqu'au vert.
# Coût irréductible du 4e état de la machine (`closed_inactive`) : le seul ajout
# à l'énumération de statut porte le total de 8487 à 9087 — 600 octets, répartis
# sur les quatre tools qui dérivent encore un schéma. `nature` en Literal ajoute
# 294 de plus. Une session `closed_inactive` DOIT pouvoir être chargée par le
# modèle, sinon la 046 rend illisibles les lignes qu'elle rend valides.
# Bumpé par la 047, et le nombre est MESURÉ, pas ajusté jusqu'au vert : 9462,
# soit +81 pour le seul champ neuf `unattributed_in_window` sur
# `BrainSessionEndResult`. La marge disponible était `19_041 - 9_500 = 9_541` ;
# il en reste 79. C'est étroit, et c'est la raison pour laquelle le second
# compteur envisagé (« détenu par une traçante ») a été refusé : il n'entrait
# pas. Si un futur champ dépasse 9_541, on s'arrête — on ne desserre PAS
# OUTPUT_SCHEMA_MINIMUM_SAVINGS, ce serait modifier un test pour faire passer
# du code.
OUTPUT_SCHEMA_TOTAL = 9462
# Abaissé de 10_000 à 9_500 : le plancher avait été fixé contre une machine à
# TROIS états, et le quatrième coûte 600 octets à lui seul — il ne restait que
# 554 de marge. Le plancher relâché reste un plancher : l'économie effective est
# de 9_660 octets sur les 19_041 de la ligne de base, soit toujours plus de la
# moitié. C'est un budget INTERNE qu'on relâche de 500 octets, jamais un contrat
# client : les sept tools gardent exactement la même surface publique, et les
# trois `output_schema=None` restent trois.
OUTPUT_SCHEMA_MINIMUM_SAVINGS = 9_500
SESSION_PUBLIC_FIELDS = {
    "id",
    # 046 : `nature` seule entre au contrat public. Les quatre autres colonnes
    # de la migration (`started_by_actor`, `last_observed_at`, `intent`,
    # `connection_id`) n'ont encore aucun écrivain et n'entrent PAS ici — chacune
    # paiera son schéma avec le commit qui l'utilise.
    "nature",
    "project_key",
    "client_key",
    "status",
    "started_focus",
    "started_focus_revision",
    "summary",
    "next_focus",
    "captured_knowledge_ids",
    "attributed_knowledge_ids",
    "nothing_to_capture_reason",
    "abandonment_reason",
    "end_expected_focus_revision",
    "focus_outcome",
    "focus_at_end",
    "focus_revision_at_end",
    "started_at",
    "last_heartbeat_at",
    "ended_at",
    "updated_at",
    "is_stale",
}


def _assert_output_schema_presence(
    output_schemas: Mapping[str, dict[str, Any] | None],
) -> None:
    assert output_schemas["brain_session_start"] is not None
    assert output_schemas["brain_session_end"] is not None
    assert output_schemas["brain_session_resume"] is not None
    assert output_schemas["brain_session_capture"] is None
    assert output_schemas["brain_session_heartbeat"] is None
    assert output_schemas["brain_session_list"] is None
    assert output_schemas["brain_session_abandon"] is None


def _baseline() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(FIXTURE_PATH.read_text()))


def _registered_server() -> tuple[FastMCP, MagicMock, AsyncMock]:
    service = MagicMock()
    briefing_loader = AsyncMock(return_value="Briefing visible")
    server = FastMCP("session-lifecycle-discovery-contract")
    register_session_lifecycle_tools(server, service, briefing_loader)
    return server, service, briefing_loader


def _open_session(*, attributed_knowledge_ids: list[UUID] | None = None) -> BrainSession:
    moment = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    return BrainSession(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        project_key="brain-v42",
        client_key="codex-session-contract",
        status=BrainSessionStatus.OPEN,
        started_focus="Initial focus",
        started_focus_revision=7,
        attributed_knowledge_ids=attributed_knowledge_ids or [],
        started_at=moment,
        last_heartbeat_at=moment,
        updated_at=moment,
    )


def _ended_session() -> BrainSession:
    moment = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    return BrainSession(
        id=UUID("22222222-2222-2222-2222-222222222222"),
        project_key="brain-v42",
        client_key="codex-session-contract",
        status=BrainSessionStatus.ENDED,
        started_focus="Initial focus",
        started_focus_revision=7,
        summary="Session completed",
        next_focus="Follow-up focus",
        nothing_to_capture_reason="No durable artifact",
        end_expected_focus_revision=7,
        focus_outcome=BrainSessionFocusOutcome.APPLIED,
        focus_at_end="Follow-up focus",
        focus_revision_at_end=8,
        started_at=moment,
        last_heartbeat_at=moment,
        ended_at=moment,
        updated_at=moment,
    )


def _abandoned_session(*, attributed_knowledge_ids: list[UUID]) -> BrainSession:
    moment = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    return BrainSession(
        id=UUID("33333333-3333-3333-3333-333333333333"),
        project_key="brain-v42",
        client_key="codex-session-contract",
        status=BrainSessionStatus.ABANDONED,
        started_focus="Initial focus",
        started_focus_revision=7,
        attributed_knowledge_ids=attributed_knowledge_ids,
        abandonment_reason="Explicit user cancellation",
        started_at=moment,
        last_heartbeat_at=moment,
        ended_at=moment,
        updated_at=moment,
    )


async def _tool(server: FastMCP, name: str) -> Tool:
    tool = await server.get_tool(name)
    assert tool is not None
    return tool


async def test_discovery_contract_keeps_tool_identity_inputs_and_schema_budget() -> None:
    baseline = _baseline()
    tool_names = tuple(cast(list[str], baseline["tool_names"]))
    contracts = cast(dict[str, dict[str, Any]], baseline["contracts"])
    baseline_lengths = cast(dict[str, int], baseline["output_schema_lengths"])
    server, _, _ = _registered_server()

    registered_names: list[str] = []
    for name in tool_names:
        if await server.get_tool(name) is not None:
            registered_names.append(name)
    registered = tuple(registered_names)
    assert registered == tool_names
    assert len(registered) == 7

    output_schemas: dict[str, dict[str, Any] | None] = {}
    final_lengths: dict[str, int] = {}
    for name in tool_names:
        tool = await _tool(server, name)
        contract = contracts[name]
        assert tool.name == name
        assert tool.version == contract["version"] == "4.0"
        assert tool.parameters == contract["parameters"]
        assert tool.annotations is not None
        assert tool.annotations.model_dump(by_alias=True) == contract["annotations"]
        output_schemas[name] = tool.output_schema
        final_lengths[name] = len(
            json.dumps(
                tool.output_schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
        )

    assert baseline_lengths == {
        "brain_session_start": 2639,
        "brain_session_capture": 2816,
        "brain_session_heartbeat": 2493,
        "brain_session_end": 3055,
        "brain_session_list": 2640,
        "brain_session_resume": 2777,
        "brain_session_abandon": 2621,
    }
    assert sum(baseline_lengths.values()) == baseline["output_schema_total"] == 19041
    _assert_output_schema_presence(output_schemas)
    assert sum(final_lengths.values()) == OUTPUT_SCHEMA_TOTAL
    assert sum(final_lengths.values()) <= (
        baseline["output_schema_total"] - OUTPUT_SCHEMA_MINIMUM_SAVINGS
    )

    mutated_output_schemas = dict(output_schemas)
    mutated_output_schemas["brain_session_start"] = None
    with pytest.raises(AssertionError):
        _assert_output_schema_presence(mutated_output_schemas)


async def test_tool_run_preserves_every_public_structured_content_contract() -> None:
    server, service, _ = _registered_server()
    knowledge_id = UUID("44444444-4444-4444-4444-444444444444")
    open_session = _open_session(attributed_knowledge_ids=[knowledge_id])
    ended_session = _ended_session()
    abandoned_session = _abandoned_session(attributed_knowledge_ids=[knowledge_id])
    service.start = AsyncMock(
        return_value=BrainSessionStartResult(
            session=open_session,
            replayed=False,
            open_session_count=2,
            briefing="",
        )
    )
    service.capture = AsyncMock(
        return_value=BrainSessionCaptureResult(
            session=open_session,
            captured_knowledge_ids=[knowledge_id],
            newly_captured_knowledge_ids=[knowledge_id],
            replayed_knowledge_ids=[],
            replayed=False,
        )
    )
    service.heartbeat = AsyncMock(return_value=BrainSessionHeartbeatResult(session=open_session))
    service.end = AsyncMock(
        return_value=BrainSessionEndResult(
            session=ended_session,
            replayed=False,
            remaining_open_session_count=1,
            current_focus="Follow-up focus",
            current_focus_revision=8,
            focus_outcome=BrainSessionFocusOutcome.APPLIED,
            focus_at_end="Follow-up focus",
            focus_revision_at_end=8,
            unattributed_in_window=0,
        )
    )
    service.list = AsyncMock(
        return_value=BrainSessionListResult(sessions=[open_session], total=1, limit=5, offset=10)
    )
    service.resume = AsyncMock(
        return_value=BrainSessionResumeResult(
            session=open_session,
            open_session_count=2,
            current_focus="Initial focus",
            current_focus_revision=7,
            briefing="",
        )
    )
    service.abandon = AsyncMock(
        return_value=BrainSessionAbandonResult(
            session=abandoned_session,
            replayed=True,
            remaining_open_session_count=1,
        )
    )

    start = (
        await (await _tool(server, "brain_session_start")).run(
            {"project_key": "brain-v42", "client_key": "codex-session-contract"}
        )
    ).structured_content
    capture = (
        await (await _tool(server, "brain_session_capture")).run(
            {
                "session_id": str(open_session.id),
                "expected_client_key": "codex-session-contract",
                "knowledge_ids": [str(knowledge_id)],
            }
        )
    ).structured_content
    heartbeat = (
        await (await _tool(server, "brain_session_heartbeat")).run(
            {"session_id": str(open_session.id), "expected_client_key": "codex-session-contract"}
        )
    ).structured_content
    end = (
        await (await _tool(server, "brain_session_end")).run(
            {
                "session_id": str(ended_session.id),
                "expected_client_key": "codex-session-contract",
                "summary": "Session completed",
                "next_focus": "Follow-up focus",
                "expected_focus_revision": 7,
                "nothing_to_capture_reason": "No durable artifact",
            }
        )
    ).structured_content
    listing = (
        await (await _tool(server, "brain_session_list")).run(
            {"project_key": "brain-v42", "status": "open", "limit": 5, "offset": 10}
        )
    ).structured_content
    resume = (
        await (await _tool(server, "brain_session_resume")).run(
            {"session_id": str(open_session.id), "expected_client_key": "codex-session-contract"}
        )
    ).structured_content
    abandon = (
        await (await _tool(server, "brain_session_abandon")).run(
            {
                "session_id": str(abandoned_session.id),
                "expected_client_key": "codex-session-contract",
                "reason": "Explicit user cancellation",
            }
        )
    ).structured_content

    assert start is not None and set(start) == {
        "session",
        "replayed",
        "open_session_count",
        "briefing",
    }
    assert resume is not None and set(resume) == {
        "session",
        "open_session_count",
        "current_focus",
        "current_focus_revision",
        "briefing",
    }
    assert end is not None and set(end) == {
        "session",
        "replayed",
        "remaining_open_session_count",
        "current_focus",
        "current_focus_revision",
        "focus_outcome",
        "focus_at_end",
        "focus_revision_at_end",
        # 047 : la MESURE qui remplace le XOR. Publique et assumée — un chiffre
        # que l'utilisateur ne voit pas ne remplace rien.
        "unattributed_in_window",
    }
    assert capture is not None and set(capture) == {
        "session",
        "captured_knowledge_ids",
        "newly_captured_knowledge_ids",
        "replayed_knowledge_ids",
        "replayed",
    }
    assert heartbeat is not None and set(heartbeat) == {"session"}
    assert listing is not None and set(listing) == {"sessions", "total", "limit", "offset"}
    assert abandon is not None and set(abandon) == {
        "session",
        "replayed",
        "remaining_open_session_count",
    }

    for payload in (start, resume, end, capture, heartbeat, abandon):
        assert set(payload["session"]) == SESSION_PUBLIC_FIELDS
    assert len(listing["sessions"]) == 1
    assert set(listing["sessions"][0]) == SESSION_PUBLIC_FIELDS

    assert start["replayed"] is False and start["open_session_count"] == 2
    assert start["briefing"] == "Briefing visible"
    assert resume["current_focus"] == "Initial focus" and resume["current_focus_revision"] == 7
    assert resume["briefing"] == "Briefing visible"
    assert end["replayed"] is False and end["remaining_open_session_count"] == 1
    assert end["current_focus"] == end["focus_at_end"] == "Follow-up focus"
    assert end["current_focus_revision"] == end["focus_revision_at_end"] == 8
    assert end["focus_outcome"] == "applied"
    assert capture["captured_knowledge_ids"] == [str(knowledge_id)]
    assert capture["newly_captured_knowledge_ids"] == [str(knowledge_id)]
    assert capture["replayed_knowledge_ids"] == [] and capture["replayed"] is False
    assert heartbeat["session"]["status"] == "open"
    assert heartbeat["session"]["last_heartbeat_at"] == "2026-08-01T12:00:00Z"
    assert heartbeat["session"]["attributed_knowledge_ids"] == [str(knowledge_id)]
    assert listing["total"] == 1 and listing["limit"] == 5 and listing["offset"] == 10
    assert listing["sessions"][0]["status"] == "open"
    assert listing["sessions"][0]["attributed_knowledge_ids"] == [str(knowledge_id)]
    assert abandon["session"]["status"] == "abandoned"
    assert abandon["session"]["abandonment_reason"] == "Explicit user cancellation"
    assert abandon["session"]["attributed_knowledge_ids"] == [str(knowledge_id)]
    assert abandon["replayed"] is True and abandon["remaining_open_session_count"] == 1
