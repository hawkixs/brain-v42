"""Knowledge services: include_archived=False reaches the repository as SQL clauses.

Ticket 73e68261: archived/merged rows were dropped in Python after the LIMIT.
The default (True) must keep today's call exactly: no extra_clauses at all.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.services.adr_service import ADRService
from brain_v42.services.decision_service import DecisionService
from brain_v42.services.learning_service import LearningService
from brain_v42.services.runbook_service import RunbookService
from brain_v42.services.snippet_service import SnippetService

_LIVE = object()

# (service class, repo kwarg, repo FTS method, repo vector method)
_CASES = [
    pytest.param(LearningService, "pg_repo", "search_fts", "search_vector", id="learning"),
    pytest.param(DecisionService, "repo", "search_fts", "search_vector", id="decision"),
    pytest.param(SnippetService, "repo", "search", "vector_search", id="snippet"),
    pytest.param(RunbookService, "pg_repo", "search_fts", "vector_search", id="runbook"),
    pytest.param(ADRService, "pg_repo", "search", "vector_search", id="adr"),
]


def _service(cls: Any, repo_kwarg: str, fts: str, vec: str) -> tuple[Any, MagicMock]:
    repo = MagicMock()
    repo.live_row_clauses.return_value = [_LIVE]
    setattr(repo, fts, AsyncMock(return_value=[]))
    setattr(repo, vec, AsyncMock(return_value=[]))
    kwargs: dict[str, Any] = {repo_kwarg: repo}
    if cls is DecisionService:
        kwargs["embedding_svc"] = MagicMock()
    return cls(**kwargs), repo


@pytest.mark.parametrize(("cls", "repo_kwarg", "fts", "vec"), _CASES)
class TestIncludeArchivedReachesTheRepository:
    @pytest.mark.asyncio
    async def test_search_passes_live_clauses_when_archived_are_excluded(
        self, cls: Any, repo_kwarg: str, fts: str, vec: str
    ) -> None:
        svc, repo = _service(cls, repo_kwarg, fts, vec)
        await svc.search("q", include_archived=False)
        assert getattr(repo, fts).await_args.kwargs["extra_clauses"] == [_LIVE]

    @pytest.mark.asyncio
    async def test_search_adds_no_clause_by_default(
        self, cls: Any, repo_kwarg: str, fts: str, vec: str
    ) -> None:
        svc, repo = _service(cls, repo_kwarg, fts, vec)
        await svc.search("q")
        assert "extra_clauses" not in getattr(repo, fts).await_args.kwargs

    @pytest.mark.asyncio
    async def test_semantic_search_passes_live_clauses_when_archived_are_excluded(
        self, cls: Any, repo_kwarg: str, fts: str, vec: str
    ) -> None:
        svc, repo = _service(cls, repo_kwarg, fts, vec)
        await svc.semantic_search("q", embedding=[0.1], include_archived=False)
        assert getattr(repo, vec).await_args.kwargs["extra_clauses"] == [_LIVE]

    @pytest.mark.asyncio
    async def test_semantic_search_adds_no_clause_by_default(
        self, cls: Any, repo_kwarg: str, fts: str, vec: str
    ) -> None:
        svc, repo = _service(cls, repo_kwarg, fts, vec)
        await svc.semantic_search("q", embedding=[0.1])
        assert "extra_clauses" not in getattr(repo, vec).await_args.kwargs
