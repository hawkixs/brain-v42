"""Les propositions de curation roadmap doivent être lisibles depuis le catalogue MCP.

Ticket 2547b4a2. Mesuré le 2026-08-11 : 499 lignes `proposed` en base, dont 43 pour
brain-v42. Aucun tool MCP ne les liste, et la seule surface apply/reject vit dans la
passerelle Codex `:9211`, que le ticket constate non démarrée. Un relecteur Dream a dû
ouvrir une transaction PostgreSQL READ ONLY pour rendre son verdict — c'est-à-dire
sortir de l'outillage que ce verdict est censé piloter.

Une table de 499 lignes que le catalogue ne sait pas montrer n'est pas « en attente » :
elle est invisible, et son décompte n'apparaît que dans un agrégat de briefing qui ne
permet aucune attribution item par item.

Ce lot expose la LECTURE seule. Le ticket est explicite — « Aucun SQL write n'est
demandé dans ce ticket » — et la surface apply/reject reste une décision séparée.
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
        """Le ticket énumère ce qu'il faut pour décider item par item.

        Sans le contexte de cible (nom de la feature), un `feature_id` nu oblige à
        une seconde requête par ligne — soit exactement le retour au SQL brut que
        ce tool existe pour supprimer.
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
        """La sonde qui compte : la table n'a PAS de project_key.

        Le scope passe par une jointure sur `features`. Retirer ce filtre rendrait
        les 499 lignes de tous les projets sous une demande scopée — un tool qui
        déborde silencieusement de son périmètre.
        """
        captured: list[tuple[str, dict[str, Any]]] = []
        tools = _tools([_row()], captured)

        await tools["brain_list_curation_proposals"](project_key="brain-v42")

        assert captured, "aucune requête exécutée"
        sql, params = captured[0]
        assert "features" in sql, "la requête ne joint pas features : scope impossible"
        # Le FILTRE, pas seulement le paramètre. Première écriture de ce test :
        # j'assertais `params["project_key"] == "brain-v42"`, ce qui reste VRAI
        # quand on supprime la clause WHERE — le paramètre est lié, simplement
        # jamais utilisé. Vérifié par mutation : la sonde ne mordait pas.
        assert "f.project_key = :project_key" in sql, (
            f"le project_key est lié mais pas filtré — la requête déborde sur "
            f"tous les projets :\n{sql}"
        )
        assert params.get("project_key") == "brain-v42", (
            f"le project_key n'est pas passé en paramètre lié : {params}"
        )

    @pytest.mark.asyncio
    async def test_the_default_status_is_proposed(self) -> None:
        """Les 174 appliquées et 35 rejetées noieraient les 43 à décider."""
        captured: list[tuple[str, dict[str, Any]]] = []
        tools = _tools([_row()], captured)

        await tools["brain_list_curation_proposals"](project_key="brain-v42")

        assert captured[0][1].get("status") == "proposed"

    @pytest.mark.asyncio
    async def test_an_empty_result_says_so_instead_of_rendering_nothing(self) -> None:
        """Une sortie vide serait indiscernable d'une panne de lecture."""
        tools = _tools([], [])

        out = await tools["brain_list_curation_proposals"](project_key="brain-v42")

        assert out.strip(), "sortie vide : rien ne distingue « aucune » d'une panne"
        assert "brain-v42" in out

    @pytest.mark.asyncio
    async def test_an_oversized_limit_is_capped_and_announced(self) -> None:
        """Réutilise la garde du ticket af3b58dd : un plafond muet ferait mentir la page."""
        captured: list[tuple[str, dict[str, Any]]] = []
        tools = _tools([_row()], captured)

        out = await tools["brain_list_curation_proposals"](project_key="brain-v42", limit=500)

        assert captured[0][1]["limit"] == 100
        assert "500" in out and "100" in out

    @pytest.mark.asyncio
    async def test_the_payload_is_rendered_without_being_dumped_raw(self) -> None:
        """Le payload est du JSONB libre : il est rendu, mais borné.

        Un dump intégral de 43 payloads reproduirait le token-bomb que le reste du
        catalogue borne déjà.
        """
        big = {"reason": "x" * 5_000}
        tools = _tools([_row(payload=big)], [])

        out = await tools["brain_list_curation_proposals"](project_key="brain-v42")

        assert len(out) < 4_000, f"payload non borné : {len(out)} caractères rendus"
        assert json.dumps(big) not in out
