"""Unit tests for SnippetService.

All tests use AsyncMock/MagicMock — no real DB or ONNX model required.
Tests verify behavior of SnippetService as a thin orchestration layer.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.models.project_context import ProjectContext
from brain_v42.models.snippet import Snippet, SnippetCreate, SnippetUpdate
from brain_v42.repositories.pg_project_context import PgProjectContextRepo
from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable
from brain_v42.services.snippet_service import SnippetService
from brain_v42.services.ticket_service import UnknownProjectError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_snippet(**kwargs) -> Snippet:
    """Helper to build a Snippet with sensible defaults."""
    defaults = {
        "id": uuid.uuid4(),
        "title": "Test snippet",
        "intention": "Parse JSON from string",
        "code": "import json; data = json.loads(s)",
        "language": "python",
        "dependencies": [],
        "usage_example": None,
        "gotchas": None,
        "project_key": "brain-v42",
        "tags": [],
        "metadata": {},
        "use_count": 0,
        "last_used_at": None,
        "embedding": None,
    }
    defaults.update(kwargs)
    return Snippet.model_validate(defaults)


@pytest.fixture
def mock_repo() -> AsyncMock:
    """Mock PgSnippetRepo with all async methods."""
    repo = AsyncMock()
    repo.set_embedding_if_current.return_value = None
    return repo


@pytest.fixture
def mock_embedding_svc() -> MagicMock:
    """Mock embedding service with async embed method."""
    svc = MagicMock()
    svc.embed = AsyncMock(return_value=[0.1] * 1536)
    return svc


@pytest.fixture
def snippet_service(mock_repo: AsyncMock, mock_embedding_svc: MagicMock) -> SnippetService:
    """SnippetService with injected mocks."""
    return SnippetService(repo=mock_repo, embedding_svc=mock_embedding_svc)


# ---------------------------------------------------------------------------
# create()
# ---------------------------------------------------------------------------


class TestCreate:
    async def test_create_embeds_intention_and_calls_repo(
        self,
        snippet_service: SnippetService,
        mock_repo: AsyncMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """create() stores null first, then enriches the unchanged row."""
        snippet = make_snippet()
        mock_repo.create.return_value = snippet
        fake_embedding = [0.1] * 1536
        mock_embedding_svc.embed.return_value = fake_embedding
        mock_repo.set_embedding_if_current.return_value = snippet.model_copy(
            update={"embedding": fake_embedding}
        ).model_dump()

        data = SnippetCreate(
            title="Test",
            intention="Parse JSON",
            code="json.loads(s)",
            language="python",
        )

        result = await snippet_service.create(data)

        mock_embedding_svc.embed.assert_awaited_once_with(data.intention)
        mock_repo.create.assert_awaited_once_with(data, embedding=None)
        mock_repo.set_embedding_if_current.assert_awaited_once_with(
            snippet.id,
            fake_embedding,
            expected_updated_at=snippet.updated_at,
        )
        assert result.id == snippet.id
        assert result.embedding == fake_embedding

    async def test_create_returns_durable_snippet_when_embedding_unavailable(
        self,
        snippet_service: SnippetService,
        mock_repo: AsyncMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """An embedding outage never turns a committed Snippet into an error."""
        snippet = make_snippet()
        mock_repo.create.return_value = snippet
        mock_embedding_svc.embed.side_effect = EmbeddingUnavailable("offline", kind="unreachable")

        data = SnippetCreate(
            title="Test",
            intention="Parse JSON",
            code="json.loads(s)",
            language="python",
        )

        result = await snippet_service.create(data)

        assert result is snippet
        mock_repo.create.assert_awaited_once_with(data, embedding=None)
        mock_repo.set_embedding_if_current.assert_not_awaited()

    async def test_create_commits_before_attempting_embedding(
        self,
        snippet_service: SnippetService,
        mock_repo: AsyncMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        events: list[str] = []
        snippet = make_snippet()

        async def create_pending(data, *, embedding):
            events.append("create")
            return snippet

        async def fail_embedding(text):
            events.append("embed")
            raise EmbeddingUnavailable("offline", kind="unreachable")

        mock_repo.create.side_effect = create_pending
        mock_embedding_svc.embed.side_effect = fail_embedding
        data = SnippetCreate(
            title="Order",
            intention="PG first",
            code="pass",
            language="python",
        )

        result = await snippet_service.create(data)

        assert result is snippet
        assert events == ["create", "embed"]


# ---------------------------------------------------------------------------
# create() — project existence guard (fail-closed)
# ---------------------------------------------------------------------------


def _make_service_with_ctx_repo(
    mock_repo: AsyncMock,
    ctx_get_by_key_return: ProjectContext | None,
) -> tuple[SnippetService, MagicMock]:
    """Return (service, project_context_repo_mock) with the guard wired."""
    mock_ctx_repo = MagicMock(spec=PgProjectContextRepo)
    mock_ctx_repo.get_by_key = AsyncMock(return_value=ctx_get_by_key_return)
    svc = SnippetService(repo=mock_repo, project_context_repo=mock_ctx_repo)
    return svc, mock_ctx_repo


class TestSnippetServiceProjectGuard:
    async def test_create_with_none_project_key_raises_unknown_project(
        self, mock_repo: AsyncMock
    ) -> None:
        svc, mock_ctx_repo = _make_service_with_ctx_repo(mock_repo, ctx_get_by_key_return=None)
        data = SnippetCreate(
            title="orphan", intention="no project_key supplied", code="pass", language="python"
        )

        with pytest.raises(UnknownProjectError):
            await svc.create(data)

        mock_repo.create.assert_not_awaited()
        mock_ctx_repo.get_by_key.assert_not_awaited()

    async def test_create_with_unknown_project_key_raises_and_names_the_key(
        self, mock_repo: AsyncMock
    ) -> None:
        svc, mock_ctx_repo = _make_service_with_ctx_repo(mock_repo, ctx_get_by_key_return=None)
        data = SnippetCreate(
            title="orphan",
            intention="unknown project",
            code="pass",
            language="python",
            project_key="red-backup",
        )

        with pytest.raises(UnknownProjectError, match="red-backup"):
            await svc.create(data)

        mock_repo.create.assert_not_awaited()
        mock_ctx_repo.get_by_key.assert_awaited_once_with("red-backup", session=None)

    async def test_create_with_known_project_key_succeeds(self, mock_repo: AsyncMock) -> None:
        snippet = make_snippet()
        mock_repo.create.return_value = snippet
        existing = ProjectContext(
            project_key="brain-v42", name="brain-v42", description="knowledge base"
        )
        svc, mock_ctx_repo = _make_service_with_ctx_repo(mock_repo, ctx_get_by_key_return=existing)
        data = SnippetCreate(
            title="known project",
            intention="passes the guard",
            code="pass",
            language="python",
            project_key="brain-v42",
        )

        result = await svc.create(data)

        mock_ctx_repo.get_by_key.assert_awaited_once_with("brain-v42", session=None)
        mock_repo.create.assert_awaited_once()
        assert result.id == snippet.id

    async def test_create_canonicalizes_alias_before_checking_the_guard(
        self, mock_repo: AsyncMock
    ) -> None:
        """project_key='brain_v42' canonicalizes to 'brain-v42' before the guard runs."""
        svc, mock_ctx_repo = _make_service_with_ctx_repo(mock_repo, ctx_get_by_key_return=None)
        data = SnippetCreate(
            title="alias",
            intention="brain_v42 alias",
            code="pass",
            language="python",
            project_key="brain_v42",
        )

        with pytest.raises(UnknownProjectError):
            await svc.create(data)

        mock_ctx_repo.get_by_key.assert_awaited_once_with("brain-v42", session=None)

    async def test_create_without_project_context_repo_skips_the_guard(
        self, snippet_service: SnippetService, mock_repo: AsyncMock
    ) -> None:
        """Backward compatibility: project_context_repo is optional at construction."""
        snippet = make_snippet()
        mock_repo.create.return_value = snippet
        data = SnippetCreate(
            title="no guard wired",
            intention="unscoped, still succeeds",
            code="pass",
            language="python",
        )

        result = await snippet_service.create(data)

        mock_repo.create.assert_awaited_once()
        assert result.id == snippet.id


# ---------------------------------------------------------------------------
# get_by_id()
# ---------------------------------------------------------------------------


class TestGetById:
    async def test_get_by_id_delegates_to_repo(
        self,
        snippet_service: SnippetService,
        mock_repo: AsyncMock,
    ) -> None:
        """get_by_id() delegates to repo.get_by_id and returns the result."""
        snippet = make_snippet()
        mock_repo.get_by_id.return_value = snippet
        snippet_id = snippet.id

        result = await snippet_service.get_by_id(snippet_id)

        mock_repo.get_by_id.assert_called_once_with(snippet_id)
        assert result == snippet

    async def test_get_by_id_returns_none_when_not_found(
        self,
        snippet_service: SnippetService,
        mock_repo: AsyncMock,
    ) -> None:
        """get_by_id() returns None when repo returns None."""
        mock_repo.get_by_id.return_value = None
        missing_id = uuid.uuid4()

        result = await snippet_service.get_by_id(missing_id)

        assert result is None


# ---------------------------------------------------------------------------
# update()
# ---------------------------------------------------------------------------


class TestUpdate:
    async def test_update_regenerates_embedding_when_intention_changes(
        self,
        snippet_service: SnippetService,
        mock_repo: AsyncMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """update() regenerates embedding when data.intention is not None."""
        snippet = make_snippet()
        mock_repo.update.return_value = snippet
        new_embedding = [0.2] * 1536
        mock_embedding_svc.embed.return_value = new_embedding

        snippet_id = snippet.id
        data = SnippetUpdate(intention="New intention")

        result = await snippet_service.update(snippet_id, data)

        mock_embedding_svc.embed.assert_awaited_once_with("New intention")
        mock_repo.update.assert_called_once_with(snippet_id, data, embedding=new_embedding)
        assert result == snippet

    async def test_update_skips_embedding_when_intention_unchanged(
        self,
        snippet_service: SnippetService,
        mock_repo: AsyncMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """update() calls repo.update with embedding=None when intention is None."""
        snippet = make_snippet()
        mock_repo.update.return_value = snippet

        snippet_id = snippet.id
        data = SnippetUpdate(code="new_code = 'updated'")

        result = await snippet_service.update(snippet_id, data)

        mock_embedding_svc.embed.assert_not_awaited()
        mock_repo.update.assert_called_once_with(snippet_id, data, embedding=None)
        assert result == snippet

    async def test_update_returns_none_when_not_found(
        self,
        snippet_service: SnippetService,
        mock_repo: AsyncMock,
    ) -> None:
        """update() returns None when repo returns None (not found)."""
        mock_repo.update.return_value = None
        missing_id = uuid.uuid4()
        data = SnippetUpdate(code="x = 1")

        result = await snippet_service.update(missing_id, data)

        assert result is None


# ---------------------------------------------------------------------------
# delete()
# ---------------------------------------------------------------------------


class TestDelete:
    async def test_delete_delegates_to_repo(
        self,
        snippet_service: SnippetService,
        mock_repo: AsyncMock,
    ) -> None:
        """delete() delegates to repo.delete and returns its result."""
        mock_repo.delete.return_value = True
        snippet_id = uuid.uuid4()

        result = await snippet_service.delete(snippet_id)

        mock_repo.delete.assert_called_once_with(snippet_id)
        assert result is True

    async def test_delete_returns_false_when_not_found(
        self,
        snippet_service: SnippetService,
        mock_repo: AsyncMock,
    ) -> None:
        """delete() returns False when snippet is not found."""
        mock_repo.delete.return_value = False
        missing_id = uuid.uuid4()

        result = await snippet_service.delete(missing_id)

        assert result is False


# ---------------------------------------------------------------------------
# list_snippets()
# ---------------------------------------------------------------------------


class TestListSnippets:
    async def test_list_snippets_delegates_all_params_to_repo(
        self,
        snippet_service: SnippetService,
        mock_repo: AsyncMock,
    ) -> None:
        """list_snippets() forwards all parameters to repo.list_all."""
        snippets = [make_snippet(), make_snippet()]
        mock_repo.list_all.return_value = snippets

        result = await snippet_service.list_snippets(
            project_key="brain-v42",
            language="python",
            limit=10,
            offset=5,
            order_by_use_count=True,
        )

        mock_repo.list_all.assert_called_once_with(
            project_key="brain-v42",
            language="python",
            limit=10,
            offset=5,
            order_by_use_count=True,
            include_archived=False,
        )
        assert result == snippets

    async def test_list_snippets_with_defaults(
        self,
        snippet_service: SnippetService,
        mock_repo: AsyncMock,
    ) -> None:
        """list_snippets() works with no parameters (all defaults)."""
        mock_repo.list_all.return_value = []

        result = await snippet_service.list_snippets()

        mock_repo.list_all.assert_called_once()
        assert result == []


# ---------------------------------------------------------------------------
# semantic_search()
# ---------------------------------------------------------------------------


class TestSemanticSearch:
    async def test_semantic_search_embeds_query_and_calls_repo_vector_search(
        self,
        snippet_service: SnippetService,
        mock_repo: AsyncMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """semantic_search() embeds query then calls repo.vector_search with language as SQL filter."""
        snippet = make_snippet()
        results = [(snippet, 0.95)]
        mock_repo.vector_search.return_value = results
        query_embedding = [0.3] * 1536
        mock_embedding_svc.embed.return_value = query_embedding

        result = await snippet_service.semantic_search(
            query="parse JSON",
            limit=5,
            project_key="brain-v42",
            language="python",
        )

        mock_embedding_svc.embed.assert_awaited_once_with("parse JSON")
        mock_repo.vector_search.assert_called_once_with(
            query_embedding,
            limit=5,
            project_key="brain-v42",
            project_keys=None,
            language="python",
        )
        assert result == results

    async def test_semantic_search_passes_language_to_repo_not_postfilter(
        self,
        snippet_service: SnippetService,
        mock_repo: AsyncMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """semantic_search() passes language kwarg to repo.vector_search (SQL filter, not post-filter)."""
        mock_repo.vector_search.return_value = []
        mock_embedding_svc.embed.return_value = [0.1] * 1536

        await snippet_service.semantic_search(
            query="test",
            limit=3,
            language="javascript",
        )

        call_kwargs = mock_repo.vector_search.call_args.kwargs
        assert "language" in call_kwargs
        assert call_kwargs["language"] == "javascript"


# ---------------------------------------------------------------------------
# increment_use()
# ---------------------------------------------------------------------------


class TestIncrementUse:
    async def test_increment_use_delegates_to_repo(
        self,
        snippet_service: SnippetService,
        mock_repo: AsyncMock,
    ) -> None:
        """increment_use() delegates to repo.increment_use."""
        snippet = make_snippet(use_count=1)
        mock_repo.increment_use.return_value = snippet
        snippet_id = snippet.id

        result = await snippet_service.increment_use(snippet_id)

        mock_repo.increment_use.assert_called_once_with(snippet_id)
        assert result == snippet

    async def test_increment_use_returns_none_when_not_found(
        self,
        snippet_service: SnippetService,
        mock_repo: AsyncMock,
    ) -> None:
        """increment_use() returns None when snippet is not found."""
        mock_repo.increment_use.return_value = None
        missing_id = uuid.uuid4()

        result = await snippet_service.increment_use(missing_id)

        assert result is None


# ---------------------------------------------------------------------------
# search() — FTS full-text search
# ---------------------------------------------------------------------------


class TestSearch:
    async def test_search_fts_delegates_to_repo(
        self,
        snippet_service: SnippetService,
        mock_repo: AsyncMock,
    ) -> None:
        """search() delegates to repo.search() and returns the result."""
        fake_snippet = make_snippet()
        mock_repo.search = AsyncMock(return_value=[fake_snippet])

        results = await snippet_service.search(query="test", project_key=None, limit=10)

        assert len(results) == 1
        mock_repo.search.assert_called_once()

    async def test_search_forwards_all_params(
        self,
        snippet_service: SnippetService,
        mock_repo: AsyncMock,
    ) -> None:
        """search() forwards all parameters to repo.search()."""
        mock_repo.search = AsyncMock(return_value=[])

        await snippet_service.search(
            query="parse json",
            project_key="brain-v42",
            language="python",
            limit=5,
            offset=10,
        )

        mock_repo.search.assert_called_once_with(
            query="parse json",
            project_key="brain-v42",
            project_keys=None,
            language="python",
            limit=5,
            offset=10,
        )


# ---------------------------------------------------------------------------
# Graph write-through
# ---------------------------------------------------------------------------


class TestGraphWriteThrough:
    async def test_create_calls_graph_upsert(
        self,
        mock_repo: AsyncMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """create() calls graph.upsert_node and link_to_project after PG write."""
        snippet = make_snippet()
        mock_repo.create.return_value = snippet
        mock_graph = MagicMock()
        mock_graph.upsert_node = AsyncMock()
        mock_graph.link_to_project = AsyncMock()

        svc = SnippetService(repo=mock_repo, embedding_svc=mock_embedding_svc, graph=mock_graph)
        data = SnippetCreate(
            title="Test",
            intention="Parse JSON",
            code="json.loads(s)",
            language="python",
            project_key="brain-v42",
        )
        await svc.create(data)

        mock_graph.upsert_node.assert_awaited_once_with(
            "Snippet",
            snippet.id,
            {"project_key": "brain-v42", "title": "Test"},
        )
        mock_graph.link_to_project.assert_awaited_once_with(snippet.id, "brain-v42")

    async def test_create_graph_failure_does_not_raise(
        self,
        mock_repo: AsyncMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """create() succeeds even if graph.upsert_node raises."""
        snippet = make_snippet()
        mock_repo.create.return_value = snippet
        mock_graph = MagicMock()
        mock_graph.upsert_node = AsyncMock(side_effect=RuntimeError("neo4j down"))

        svc = SnippetService(repo=mock_repo, embedding_svc=mock_embedding_svc, graph=mock_graph)
        data = SnippetCreate(
            title="Test",
            intention="Parse JSON",
            code="json.loads(s)",
            language="python",
        )
        result = await svc.create(data)
        assert result == snippet

    async def test_create_without_graph_works(
        self,
        snippet_service: SnippetService,
        mock_repo: AsyncMock,
    ) -> None:
        """create() works normally when graph=None."""
        snippet = make_snippet()
        mock_repo.create.return_value = snippet
        data = SnippetCreate(
            title="Test",
            intention="Parse JSON",
            code="json.loads(s)",
            language="python",
        )
        result = await snippet_service.create(data)
        assert result == snippet

    async def test_create_without_embedding_svc_persists_pending_snippet(self) -> None:
        """create() remains available when embedding_svc is None."""
        repo = AsyncMock()
        snippet = make_snippet()
        repo.create.return_value = snippet
        svc = SnippetService(repo=repo, embedding_svc=None)
        data = SnippetCreate(
            title="test",
            code="x=1",
            language="python",
            intention="test intent",
            project_key="test",
        )
        result = await svc.create(data)

        assert result is snippet
        repo.create.assert_awaited_once_with(data, embedding=None)
        repo.set_embedding_if_current.assert_not_awaited()

    async def test_semantic_search_raises_value_error_when_no_embedding_svc(self) -> None:
        """semantic_search() must raise ValueError when embedding_svc is None."""
        repo = AsyncMock()
        svc = SnippetService(repo=repo, embedding_svc=None)
        with pytest.raises(ValueError, match="embedding_svc"):
            await svc.semantic_search("query")

    async def test_delete_removes_graph_node(
        self,
        mock_repo: AsyncMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """delete() calls graph.delete_node after PG delete."""
        mock_repo.delete.return_value = True
        mock_graph = MagicMock()
        mock_graph.delete_node = AsyncMock()

        svc = SnippetService(repo=mock_repo, embedding_svc=mock_embedding_svc, graph=mock_graph)
        snippet_id = uuid.uuid4()
        result = await svc.delete(snippet_id)

        mock_graph.delete_node.assert_awaited_once_with("Snippet", snippet_id)
        assert result is True
