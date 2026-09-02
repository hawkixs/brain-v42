"""Roadmap curation proposals must be readable from the MCP catalogue.

Ticket 2547b4a2. Measured on 2026-08-11: 499 `proposed` rows in the database, 43 of
them for brain-v42. No MCP tool lists them, and the only apply/reject surface lives
in the Codex gateway `:9211`, which the ticket finds not started. A Dream reviewer had
to open a READ ONLY PostgreSQL transaction to reach a verdict — that is, to leave the
tooling that verdict is supposed to drive.

A 499-row table the catalogue cannot show is not "pending": it is invisible, and its
count appears only in a briefing aggregate that allows no item-by-item attribution.

This batch exposes READING only. The ticket is explicit — "no SQL write is requested
in this ticket" — and the apply/reject surface stays a separate decision.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from brain_v42.mcp.tools.dream_tools import register_dream_tools


class MockMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **_kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = fn
            return fn

        return decorator


_FEATURE_ID = uuid4()


def _row(**overrides: Any) -> Any:
    base = {
        "id": 553,
        "op": "archive",
        "feature_id": _FEATURE_ID,
        "payload": {"reason": "duplicate"},
        "rationale": "Doublon de la feature 42",
        "status": "proposed",
        "created_at": datetime(2026, 7, 14, 4, 12, tzinfo=UTC),
        "feature_name": "Mode link-only pour les signaux",
        "project_key": "brain-v42",
    }
    base.update(overrides)
    row = MagicMock()
    row._mapping = base
    for key, value in base.items():
        setattr(row, key, value)
    return row


def _tools(rows: list[Any], captured: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    class _Result:
        def __init__(self, data: list[Any]) -> None:
            self._data = data

        def mappings(self) -> Any:
            return self

        def all(self) -> list[Any]:
            return [r._mapping for r in self._data]

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, statement: Any, params: Any = None) -> _Result:
            captured.append((str(statement), params or {}))
            return _Result(rows)

    mcp = MockMCP()
    register_dream_tools(
        mcp,
        session_factory=MagicMock(return_value=_Session()),
        auto_linker=None,
        graph_service=None,
    )
    return mcp.registered


class TestListCurationProposals:
    @pytest.mark.asyncio
    async def test_the_tool_is_registered(self) -> None:
        assert "brain_list_curation_proposals" in _tools([], [])

    @pytest.mark.asyncio
    async def test_it_renders_every_field_the_reviewer_needs(self) -> None:
        """The ticket enumerates what is needed to decide item by item.

        Without the target context (the feature name), a bare `feature_id` forces a
        second query per row — that is, exactly the return to raw SQL this tool
        exists to remove.
        """
        captured: list[tuple[str, dict[str, Any]]] = []
        tools = _tools([_row()], captured)

        out = await tools["brain_list_curation_proposals"](project_key="brain-v42")

        for expected in (
            "553",
            "archive",
            str(_FEATURE_ID),
            "Doublon de la feature 42",
            "proposed",
            "2026-07-14",
            "Mode link-only pour les signaux",
        ):
            assert expected in out, f"champ absent du rendu : {expected!r}"

    @pytest.mark.asyncio
    async def test_the_query_is_scoped_to_the_requested_project(self) -> None:
        """The probe that matters: the table has NO project_key.

        Scoping goes through a join on `features`. Removing that filter would return
        all projects' 499 rows under a scoped request — a tool silently overflowing
        its own perimeter.
        """
        captured: list[tuple[str, dict[str, Any]]] = []
        tools = _tools([_row()], captured)

        await tools["brain_list_curation_proposals"](project_key="brain-v42")

        assert captured, "aucune requête exécutée"
        sql, params = captured[0]
        assert "features" in sql, "la requête ne joint pas features : scope impossible"
        # The FILTER, not merely the parameter. First draft of this test: it
        # asserted `params["project_key"] == "brain-v42"`, which stays TRUE when
        # the WHERE clause is deleted — the parameter is bound, simply never used.
        # Verified by mutation: the probe did not bite.
        assert "f.project_key = :project_key" in sql, (
            f"le project_key est lié mais pas filtré — la requête déborde sur "
            f"tous les projets :\n{sql}"
        )
        assert params.get("project_key") == "brain-v42", (
            f"le project_key n'est pas passé en paramètre lié : {params}"
        )

    @pytest.mark.asyncio
    async def test_the_default_status_is_proposed(self) -> None:
        """The 174 applied and 35 rejected ones would drown the 43 left to decide."""
        captured: list[tuple[str, dict[str, Any]]] = []
        tools = _tools([_row()], captured)

        await tools["brain_list_curation_proposals"](project_key="brain-v42")

        assert captured[0][1].get("status") == "proposed"

    @pytest.mark.asyncio
    async def test_an_empty_result_says_so_instead_of_rendering_nothing(self) -> None:
        """An empty output would be indistinguishable from a read failure."""
        tools = _tools([], [])

        out = await tools["brain_list_curation_proposals"](project_key="brain-v42")

        assert out.strip(), "sortie vide : rien ne distingue « aucune » d'une panne"
        assert "brain-v42" in out

    @pytest.mark.asyncio
    async def test_an_oversized_limit_is_capped_and_announced(self) -> None:
        """Reuses ticket af3b58dd's guard: a silent cap would make the page lie."""
        captured: list[tuple[str, dict[str, Any]]] = []
        tools = _tools([_row()], captured)

        out = await tools["brain_list_curation_proposals"](project_key="brain-v42", limit=500)

        assert captured[0][1]["limit"] == 100
        assert "500" in out and "100" in out

    @pytest.mark.asyncio
    async def test_the_payload_is_rendered_without_being_dumped_raw(self) -> None:
        """The payload is free-form JSONB: it is returned, but bounded.

        A full dump of 43 payloads would reproduce the token bomb the rest of the
        catalogue already bounds.
        """
        big = {"reason": "x" * 5_000}
        tools = _tools([_row(payload=big)], [])

        out = await tools["brain_list_curation_proposals"](project_key="brain-v42")

        assert len(out) < 4_000, f"payload non borné : {len(out)} caractères rendus"
        assert json.dumps(big) not in out
