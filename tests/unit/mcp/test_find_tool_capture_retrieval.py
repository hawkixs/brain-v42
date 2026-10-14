"""Protect capture tool retrieval: shared capture rules affect BM25 description ranking.

The five capture tools share a routing rule in ``capture_rule.py``, and BM25 ranks on
description text. These bounds hold before and after that change (measured 2026-10-13).
Queries come from real client transcripts (``brain_find_tool`` calls of the last 14 days)
and multi-word cases from the review.
"""

import pytest
from fastmcp import Client

from brain_v42.mcp.tool_catalog import apply_tool_catalog_profile
from tests.unit.mcp.test_knowledge_tool_contract import _registered_knowledge_server


@pytest.mark.parametrize(
    ("query", "expected_tool", "top"),
    [
        ("log decision", "brain_log_decision", 1),
        ("log a decision", "brain_log_decision", 1),
        ("log a technical decision with reasoning and alternatives", "brain_log_decision", 1),
        ("log a decision between options", "brain_log_decision", 3),
        ("log a decision with rationale", "brain_log_decision", 1),
        ("log a decision with alternatives and rationale", "brain_log_decision", 1),
        ("why we chose", "brain_log_decision", 3),
        ("supersede decision", "brain_supersede_decision", 1),
        ("update decision", "brain_update", 1),
        ("create runbook", "brain_create_runbook", 1),
        ("procedure steps", "brain_create_runbook", 1),
        ("update an existing runbook steps", "brain_update", 3),
        ("get runbook by id", "brain_get_runbook", 1),
        ("save snippet reusable code", "brain_save_snippet", 1),
        ("save code snippet", "brain_save_snippet", 1),
        ("get snippet by id", "brain_get", 3),
        ("architecture decision", "brain_propose_adr", 3),
        ("propose adr", "brain_propose_adr", 1),
        ("learn", "brain_learn", 3),
        ("record an insight", "brain_learn", 1),
        ("update or correct an existing learning", "brain_update", 1),
    ],
)
async def test_find_tool_ranks_capture_tools(query: str, expected_tool: str, top: int) -> None:
    server = apply_tool_catalog_profile(_registered_knowledge_server(), "compact")
    async with Client(server) as client:
        found = await client.call_tool("brain_find_tool", {"query": query})
        names = [item["name"] for item in found.data]

    assert expected_tool in names[:top], f"query={query!r}; returned names={names}"
