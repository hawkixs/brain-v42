"""Service forwarding and graph-side-effect guards for scoped CRUD."""

from __future__ import annotations

import uuid
from inspect import signature
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.models.adr import ADRUpdate
from brain_v42.models.decision import DecisionUpdate
from brain_v42.models.learning import LearningUpdate
from brain_v42.models.runbook import RunbookUpdate
from brain_v42.models.snippet import SnippetUpdate
from brain_v42.services.adr_service import ADRService
from brain_v42.services.decision_service import DecisionService
from brain_v42.services.learning_service import LearningService
from brain_v42.services.runbook_service import RunbookService
from brain_v42.services.snippet_service import SnippetService

ENTITY_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
PROJECT_KEY = "sec1b-owned"


def _dependencies() -> tuple[MagicMock, MagicMock, MagicMock]:
    repo = MagicMock()
    repo.get_by_id = AsyncMock(return_value=None)
    repo.update = AsyncMock(return_value=None)
    repo.delete = AsyncMock(return_value=False)
    embedding = MagicMock()
    embedding.embed = AsyncMock(return_value=[0.1, 0.2])
    graph = MagicMock()
    graph.delete_node = AsyncMock()
    return repo, embedding, graph


def _service_case(kind: str) -> tuple[Any, MagicMock, MagicMock, Any]:
    repo, embedding, graph = _dependencies()
    if kind == "decision":
        return (
            DecisionService(repo=repo, embedding_svc=embedding, graph=graph),
            repo,
            graph,
            DecisionUpdate(tags=["admin"]),
        )
    if kind == "learning":
        return (
            LearningService(pg_repo=repo, graph=graph),
            repo,
            graph,
            LearningUpdate(tags=["admin"]),
        )
    if kind == "snippet":
        return (
            SnippetService(repo=repo, graph=graph),
            repo,
            graph,
            SnippetUpdate(tags=["admin"]),
        )
    if kind == "runbook":
        return (
            RunbookService(pg_repo=repo, graph=graph),
            repo,
            graph,
            RunbookUpdate(tags=["admin"]),
        )
    if kind == "adr":
        return (
            ADRService(pg_repo=repo, graph=graph),
            repo,
            graph,
            ADRUpdate(tags=["admin"]),
        )
    raise AssertionError(kind)


SERVICE_KINDS = ("decision", "learning", "snippet", "runbook", "adr")
GRAPH_LABELS = {
    "decision": "Decision",
    "learning": "Learning",
    "snippet": "Snippet",
    "runbook": "Runbook",
    "adr": "ADR",
}


def _assert_project_key_parameter(method: Any) -> None:
    assert "project_key" in signature(method).parameters


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", SERVICE_KINDS)
async def test_admin_service_calls_forward_no_new_keyword_arguments(kind: str) -> None:
    service, repo, graph, update = _service_case(kind)

    assert await service.get_by_id(ENTITY_ID) is None
    repo.get_by_id.assert_awaited_once_with(ENTITY_ID)

    assert await service.update(ENTITY_ID, update) is None
    repo.update.assert_awaited_once_with(ENTITY_ID, update, embedding=None)

    assert await service.delete(ENTITY_ID) is False
    repo.delete.assert_awaited_once_with(ENTITY_ID)
    graph.delete_node.assert_awaited_once_with(GRAPH_LABELS[kind], ENTITY_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", SERVICE_KINDS)
async def test_scoped_service_get_forwards_project_key(kind: str) -> None:
    service, repo, _graph, _update = _service_case(kind)
    _assert_project_key_parameter(service.get_by_id)

    assert await service.get_by_id(ENTITY_ID, project_key=PROJECT_KEY) is None

    repo.get_by_id.assert_awaited_once_with(ENTITY_ID, project_key=PROJECT_KEY)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", SERVICE_KINDS)
async def test_scoped_foreign_delete_has_no_graph_side_effect(kind: str) -> None:
    service, repo, graph, _update = _service_case(kind)
    _assert_project_key_parameter(service.delete)

    assert await service.delete(ENTITY_ID, project_key=PROJECT_KEY) is False

    repo.delete.assert_awaited_once_with(ENTITY_ID, project_key=PROJECT_KEY)
    graph.delete_node.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", SERVICE_KINDS)
async def test_scoped_owned_delete_bounds_graph_cleanup_to_same_project(kind: str) -> None:
    service, repo, graph, _update = _service_case(kind)
    repo.delete.return_value = True

    assert await service.delete(ENTITY_ID, project_key=PROJECT_KEY) is True

    repo.delete.assert_awaited_once_with(ENTITY_ID, project_key=PROJECT_KEY)
    graph.delete_node.assert_awaited_once_with(
        GRAPH_LABELS[kind],
        ENTITY_ID,
        project_key=PROJECT_KEY,
    )


def _scoped_update_case(kind: str) -> tuple[Any, MagicMock, Any, Any]:
    repo, embedding, graph = _dependencies()
    if kind == "decision":
        current = SimpleNamespace(title="old", description="desc", reasoning="why")
        repo.get_by_id.return_value = current
        return (
            DecisionService(repo=repo, embedding_svc=embedding, graph=graph),
            repo,
            embedding,
            DecisionUpdate(title="new"),
        )
    if kind == "learning":
        current = SimpleNamespace(topic="old", insight="insight")
        repo.get_by_id.return_value = current
        return (
            LearningService(pg_repo=repo, embedding_svc=embedding, graph=graph),
            repo,
            embedding,
            LearningUpdate(topic="new"),
        )
    if kind == "snippet":
        return (
            SnippetService(repo=repo, embedding_svc=embedding, graph=graph),
            repo,
            embedding,
            SnippetUpdate(intention="new intention"),
        )
    if kind == "runbook":
        current = SimpleNamespace(title="old", description="desc", trigger="trigger")
        repo.get_by_id.return_value = current
        return (
            RunbookService(pg_repo=repo, embedding_svc=embedding, graph=graph),
            repo,
            embedding,
            RunbookUpdate(title="new"),
        )
    if kind == "adr":
        current = SimpleNamespace(title="old", context="context", decision="decision")
        repo.get_by_id.return_value = current
        return (
            ADRService(pg_repo=repo, embedding_svc=embedding, graph=graph),
            repo,
            embedding,
            ADRUpdate(title="new"),
        )
    raise AssertionError(kind)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", SERVICE_KINDS)
async def test_scoped_update_and_reembedding_read_forward_same_project(kind: str) -> None:
    service, repo, embedding, update = _scoped_update_case(kind)
    _assert_project_key_parameter(service.update)

    assert await service.update(ENTITY_ID, update, project_key=PROJECT_KEY) is None

    if kind == "snippet":
        repo.get_by_id.assert_not_awaited()
    else:
        repo.get_by_id.assert_awaited_once_with(ENTITY_ID, project_key=PROJECT_KEY)
    embedding.embed.assert_awaited_once()
    repo.update.assert_awaited_once_with(
        ENTITY_ID,
        update,
        embedding=[0.1, 0.2],
        project_key=PROJECT_KEY,
    )
