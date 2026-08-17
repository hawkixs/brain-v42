"""Unit tests for RunbookService.

Tests use AsyncMock to mock PgRunbookRepo — no real DB required.
All tests verify service behavior without touching PostgreSQL.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.models.project_context import ProjectContext
from brain_v42.models.runbook import (
    ExecutionStatus,
    Runbook,
    RunbookCreate,
    RunbookStep,
    RunbookUpdate,
)
from brain_v42.repositories.pg_project_context import PgProjectContextRepo
from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable
from brain_v42.services.runbook_service import RunbookService
from brain_v42.services.ticket_service import UnknownProjectError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_runbook(
    id: uuid.UUID | None = None,
    title: str = "Deploy App",
    project_key: str = "my-proj",
) -> Runbook:
    """Build a minimal Runbook instance for use in test fixtures."""
    return Runbook(
        id=id or uuid.uuid4(),
        title=title,
        description="Deploy the application to production.",
        project_key=project_key,
        trigger="On release tag",
        prerequisites=[],
        steps=[
            RunbookStep(
                order=1,
                title="Build",
                command="make build",
                description="Build the binary",
            )
        ],
        rollback_steps=[],
        tags=["deploy", "production"],
        metadata={},
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_repo() -> MagicMock:
    """Mock PgRunbookRepo with all async methods stubbed."""
    repo = MagicMock()
    repo.create = AsyncMock(return_value=make_runbook())
    repo.set_embedding_if_current = AsyncMock(return_value=None)
    repo.get_by_id = AsyncMock(return_value=make_runbook())
    repo.update = AsyncMock(return_value=make_runbook())
    repo.delete = AsyncMock(return_value=True)
    repo.search_fts = AsyncMock(return_value=[make_runbook()])
    repo.record_execution = AsyncMock(return_value=make_runbook())
    repo.get_by_title = AsyncMock(return_value=make_runbook())
    repo.list_by_project = AsyncMock(return_value=[make_runbook()])
    return repo


@pytest.fixture
def svc(mock_repo: MagicMock) -> RunbookService:
    """RunbookService without EmbeddingService (PG-only mode)."""
    return RunbookService(pg_repo=mock_repo)


@pytest.fixture
def mock_embedding_svc() -> MagicMock:
    """Mock EmbeddingService with async embed method."""
    svc = MagicMock()
    svc.embed = AsyncMock(return_value=[0.1] * 1536)
    return svc


@pytest.fixture
def svc_with_embedding(mock_repo: MagicMock, mock_embedding_svc: MagicMock) -> RunbookService:
    """RunbookService with EmbeddingService injected."""
    return RunbookService(pg_repo=mock_repo, embedding_svc=mock_embedding_svc)


# ---------------------------------------------------------------------------
# TestCreate
# ---------------------------------------------------------------------------


class TestCreate:
    async def test_create_without_embedding_svc_passes_none_embedding(
        self,
        svc: RunbookService,
        mock_repo: MagicMock,
    ) -> None:
        """create() without embedding_svc calls repo.create(data, embedding=None)."""
        data = RunbookCreate(
            title="Deploy App",
            description="Deploy the application.",
            project_key="my-proj",
            trigger="On release tag",
        )
        result = await svc.create(data)

        mock_repo.create.assert_awaited_once_with(data, embedding=None)
        assert isinstance(result, Runbook)

    async def test_create_with_embedding_svc_enriches_after_persisting_null(
        self,
        svc_with_embedding: RunbookService,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """create() stores null first, then uses compare-and-set for the vector."""
        runbook = make_runbook()
        mock_repo.create.return_value = runbook
        mock_repo.set_embedding_if_current.return_value = runbook.model_copy(
            update={"embedding": [0.1] * 1536}
        ).model_dump()
        data = RunbookCreate(
            title="Deploy App",
            description="Deploy the application.",
            project_key="my-proj",
            trigger="On release tag",
        )
        result = await svc_with_embedding.create(data)

        mock_embedding_svc.embed.assert_awaited_once()
        mock_repo.create.assert_awaited_once_with(data, embedding=None)
        mock_repo.set_embedding_if_current.assert_awaited_once_with(
            runbook.id,
            [0.1] * 1536,
            expected_updated_at=runbook.updated_at,
        )
        assert isinstance(result, Runbook)
        assert result.embedding == [0.1] * 1536

    async def test_create_returns_durable_runbook_when_embedding_unavailable(
        self,
        svc_with_embedding: RunbookService,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        runbook = make_runbook()
        mock_repo.create.return_value = runbook
        mock_embedding_svc.embed.side_effect = EmbeddingUnavailable("offline", kind="unreachable")
        data = RunbookCreate(
            title="Deploy",
            description="Durable first",
            project_key="my-proj",
            trigger="release",
        )

        result = await svc_with_embedding.create(data)

        assert result is runbook
        mock_repo.create.assert_awaited_once_with(data, embedding=None)
        mock_repo.set_embedding_if_current.assert_not_awaited()

    async def test_create_commits_before_attempting_embedding(
        self,
        svc_with_embedding: RunbookService,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        events: list[str] = []
        runbook = make_runbook()

        async def create_pending(data, *, embedding):
            events.append("create")
            return runbook

        async def fail_embedding(text):
            events.append("embed")
            raise EmbeddingUnavailable("offline", kind="unreachable")

        mock_repo.create.side_effect = create_pending
        mock_embedding_svc.embed.side_effect = fail_embedding
        data = RunbookCreate(
            title="Order",
            description="PG first",
            project_key="my-proj",
            trigger="release",
        )

        result = await svc_with_embedding.create(data)

        assert result is runbook
        assert events == ["create", "embed"]

    async def test_create_embedding_text_combines_title_description_trigger(
        self,
        svc_with_embedding: RunbookService,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """create() builds embedding text from title + description + trigger."""
        data = RunbookCreate(
            title="MyTitle",
            description="MyDesc",
            project_key="proj",
            trigger="MyTrigger",
        )
        await svc_with_embedding.create(data)

        embed_call_text = mock_embedding_svc.embed.call_args[0][0]
        assert "MyTitle" in embed_call_text
        assert "MyDesc" in embed_call_text
        assert "MyTrigger" in embed_call_text


# ---------------------------------------------------------------------------
# create() — project existence guard (fail-closed)
# ---------------------------------------------------------------------------


def _make_service_with_ctx_repo(
    mock_repo: MagicMock,
    ctx_get_by_key_return: ProjectContext | None,
) -> tuple[RunbookService, MagicMock]:
    """Return (service, project_context_repo_mock) with the guard wired."""
    mock_ctx_repo = MagicMock(spec=PgProjectContextRepo)
    mock_ctx_repo.get_by_key = AsyncMock(return_value=ctx_get_by_key_return)
    svc = RunbookService(pg_repo=mock_repo, project_context_repo=mock_ctx_repo)
    return svc, mock_ctx_repo


class TestRunbookServiceProjectGuard:
    async def test_create_with_unknown_project_key_raises_and_names_the_key(
        self, mock_repo: MagicMock
    ) -> None:
        svc, mock_ctx_repo = _make_service_with_ctx_repo(mock_repo, ctx_get_by_key_return=None)
        data = RunbookCreate(
            title="orphan",
            description="unknown project",
            project_key="red-backup",
            trigger="manual",
        )

        with pytest.raises(UnknownProjectError, match="red-backup"):
            await svc.create(data)

        mock_repo.create.assert_not_awaited()
        mock_ctx_repo.get_by_key.assert_awaited_once_with("red-backup", session=None)

    async def test_create_with_known_project_key_succeeds(self, mock_repo: MagicMock) -> None:
        existing = ProjectContext(
            project_key="brain-v42", name="brain-v42", description="knowledge base"
        )
        svc, mock_ctx_repo = _make_service_with_ctx_repo(mock_repo, ctx_get_by_key_return=existing)
        data = RunbookCreate(
            title="known project",
            description="passes the guard",
            project_key="brain-v42",
            trigger="manual",
        )

        result = await svc.create(data)

        mock_ctx_repo.get_by_key.assert_awaited_once_with("brain-v42", session=None)
        mock_repo.create.assert_awaited_once()
        assert result is not None

    async def test_create_canonicalizes_alias_before_checking_the_guard(
        self, mock_repo: MagicMock
    ) -> None:
        """project_key='brain_v42' canonicalizes to 'brain-v42' before the guard runs."""
        svc, mock_ctx_repo = _make_service_with_ctx_repo(mock_repo, ctx_get_by_key_return=None)
        data = RunbookCreate(
            title="alias",
            description="brain_v42 alias",
            project_key="brain_v42",
            trigger="manual",
        )

        with pytest.raises(UnknownProjectError):
            await svc.create(data)

        mock_ctx_repo.get_by_key.assert_awaited_once_with("brain-v42", session=None)

    async def test_create_without_project_context_repo_skips_the_guard(
        self, svc: RunbookService, mock_repo: MagicMock
    ) -> None:
        """Backward compatibility: project_context_repo is optional at construction."""
        data = RunbookCreate(
            title="no guard wired",
            description="unscoped, still succeeds",
            project_key="my-proj",
            trigger="manual",
        )

        result = await svc.create(data)

        mock_repo.create.assert_awaited_once()
        assert result is not None


# ---------------------------------------------------------------------------
# TestGetById
# ---------------------------------------------------------------------------


class TestGetById:
    async def test_get_by_id_delegates_to_repo(
        self,
        svc: RunbookService,
        mock_repo: MagicMock,
    ) -> None:
        """get_by_id() calls repo.get_by_id(id) and returns its result."""
        runbook_id = uuid.uuid4()
        expected = make_runbook(id=runbook_id)
        mock_repo.get_by_id.return_value = expected

        result = await svc.get_by_id(runbook_id)

        mock_repo.get_by_id.assert_awaited_once_with(runbook_id)
        assert result == expected

    async def test_get_by_id_returns_none_when_not_found(
        self,
        svc: RunbookService,
        mock_repo: MagicMock,
    ) -> None:
        """get_by_id() returns None when repo returns None."""
        mock_repo.get_by_id.return_value = None

        result = await svc.get_by_id(uuid.uuid4())

        assert result is None


# ---------------------------------------------------------------------------
# TestGetByTitle
# ---------------------------------------------------------------------------


class TestGetByTitle:
    async def test_get_by_title_delegates_to_repo(
        self,
        svc: RunbookService,
        mock_repo: MagicMock,
    ) -> None:
        """get_by_title() calls repo.get_by_title(title, project_key)."""
        expected = make_runbook(title="Deploy App", project_key="my-proj")
        mock_repo.get_by_title.return_value = expected

        result = await svc.get_by_title("Deploy App", "my-proj")

        mock_repo.get_by_title.assert_awaited_once_with("Deploy App", "my-proj")
        assert result == expected

    async def test_get_by_title_returns_none_when_not_found(
        self,
        svc: RunbookService,
        mock_repo: MagicMock,
    ) -> None:
        """get_by_title() returns None when repo returns None."""
        mock_repo.get_by_title.return_value = None

        result = await svc.get_by_title("Nonexistent", "proj")

        assert result is None


# ---------------------------------------------------------------------------
# TestListByProject
# ---------------------------------------------------------------------------


class TestListByProject:
    async def test_list_by_project_delegates_to_repo(
        self,
        svc: RunbookService,
        mock_repo: MagicMock,
    ) -> None:
        """list_by_project() calls repo.list_by_project(project_key, limit, offset)."""
        runbooks = [make_runbook(), make_runbook()]
        mock_repo.list_by_project.return_value = runbooks

        result = await svc.list_by_project("my-proj", limit=10, offset=5)

        mock_repo.list_by_project.assert_awaited_once_with("my-proj", limit=10, offset=5)
        assert result == runbooks

    async def test_list_by_project_uses_default_limit_50(
        self,
        svc: RunbookService,
        mock_repo: MagicMock,
    ) -> None:
        """list_by_project() uses limit=50 and offset=0 by default."""
        await svc.list_by_project("my-proj")

        mock_repo.list_by_project.assert_awaited_once_with("my-proj", limit=50, offset=0)

    async def test_list_by_project_returns_empty_list_when_none_found(
        self,
        svc: RunbookService,
        mock_repo: MagicMock,
    ) -> None:
        """list_by_project() returns empty list when repo returns empty."""
        mock_repo.list_by_project.return_value = []

        result = await svc.list_by_project("empty-proj")

        assert result == []


# ---------------------------------------------------------------------------
# TestSearch
# ---------------------------------------------------------------------------


class TestSearch:
    async def test_search_delegates_to_repo_search_fts(
        self,
        svc: RunbookService,
        mock_repo: MagicMock,
    ) -> None:
        """search() calls repo.search_fts(query, project_key, limit, offset)."""
        runbooks = [make_runbook()]
        mock_repo.search_fts.return_value = runbooks

        result = await svc.search("deploy", project_key="my-proj", limit=10, offset=0)

        mock_repo.search_fts.assert_awaited_once_with(
            "deploy", project_key="my-proj", project_keys=None, limit=10, offset=0
        )
        assert result == runbooks

    async def test_search_without_project_key(
        self,
        svc: RunbookService,
        mock_repo: MagicMock,
    ) -> None:
        """search() passes project_key=None when omitted."""
        await svc.search("deploy")

        call_kwargs = mock_repo.search_fts.call_args
        assert call_kwargs.kwargs.get("project_key") is None

    async def test_search_uses_default_limit_20(
        self,
        svc: RunbookService,
        mock_repo: MagicMock,
    ) -> None:
        """search() uses limit=20 by default."""
        await svc.search("deploy")

        call_kwargs = mock_repo.search_fts.call_args
        assert call_kwargs.kwargs.get("limit") == 20


# ---------------------------------------------------------------------------
# TestRecordExecution
# ---------------------------------------------------------------------------


class TestRecordExecution:
    async def test_record_execution_delegates_to_repo(
        self,
        svc: RunbookService,
        mock_repo: MagicMock,
    ) -> None:
        """record_execution() calls repo.record_execution(id, status)."""
        runbook_id = uuid.uuid4()
        expected = make_runbook(id=runbook_id)
        mock_repo.record_execution.return_value = expected

        result = await svc.record_execution(runbook_id, "success")

        mock_repo.record_execution.assert_awaited_once_with(runbook_id, "success")
        assert result == expected

    async def test_record_execution_returns_none_when_not_found(
        self,
        svc: RunbookService,
        mock_repo: MagicMock,
    ) -> None:
        """record_execution() returns None when runbook not found."""
        mock_repo.record_execution.return_value = None

        result = await svc.record_execution(uuid.uuid4(), "failed")

        assert result is None

    @pytest.mark.parametrize("status", ["success", "failed", "partial", "skipped"])
    async def test_record_execution_accepts_all_statuses(
        self,
        svc: RunbookService,
        mock_repo: MagicMock,
        status: ExecutionStatus,
    ) -> None:
        """record_execution() accepts all valid ExecutionStatus values."""
        runbook_id = uuid.uuid4()
        mock_repo.record_execution.return_value = make_runbook(id=runbook_id)

        result = await svc.record_execution(runbook_id, status)

        mock_repo.record_execution.assert_awaited_once_with(runbook_id, status)
        assert result is not None


# ---------------------------------------------------------------------------
# TestUpdate
# ---------------------------------------------------------------------------


class TestUpdate:
    async def test_update_without_embedding_svc_passes_none_embedding(
        self,
        svc: RunbookService,
        mock_repo: MagicMock,
    ) -> None:
        """update() without embedding_svc calls repo.update(id, data, embedding=None)."""
        runbook_id = uuid.uuid4()
        data = RunbookUpdate(title="New Title")

        await svc.update(runbook_id, data)

        mock_repo.update.assert_awaited_once_with(runbook_id, data, embedding=None)

    async def test_update_returns_none_when_not_found(
        self,
        svc: RunbookService,
        mock_repo: MagicMock,
    ) -> None:
        """update() returns None when repo.update returns None."""
        mock_repo.update.return_value = None

        result = await svc.update(uuid.uuid4(), RunbookUpdate(title="New Title"))

        assert result is None

    async def test_update_with_embedding_svc_refreshes_embedding_when_title_changes(
        self,
        svc_with_embedding: RunbookService,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """update() with embedding_svc calls embed() when title is updated."""
        runbook_id = uuid.uuid4()
        existing = make_runbook(id=runbook_id, title="Old Title")
        mock_repo.get_by_id.return_value = existing
        data = RunbookUpdate(title="New Title")

        await svc_with_embedding.update(runbook_id, data)

        mock_embedding_svc.embed.assert_awaited_once()
        call_kwargs = mock_repo.update.call_args
        assert call_kwargs.kwargs.get("embedding") == [0.1] * 1536

    async def test_update_with_embedding_svc_no_embed_when_no_text_change(
        self,
        svc_with_embedding: RunbookService,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """update() with embedding_svc does NOT call embed() when no title/description changes."""
        runbook_id = uuid.uuid4()
        data = RunbookUpdate(tags=["new-tag"])  # no title/description change

        await svc_with_embedding.update(runbook_id, data)

        mock_embedding_svc.embed.assert_not_awaited()
        mock_repo.update.assert_awaited_once_with(runbook_id, data, embedding=None)


# ---------------------------------------------------------------------------
# TestDelete
# ---------------------------------------------------------------------------


class TestDelete:
    async def test_delete_delegates_to_repo(
        self,
        svc: RunbookService,
        mock_repo: MagicMock,
    ) -> None:
        """delete() calls repo.delete(id) and returns its result."""
        runbook_id = uuid.uuid4()
        mock_repo.delete.return_value = True

        result = await svc.delete(runbook_id)

        mock_repo.delete.assert_awaited_once_with(runbook_id)
        assert result is True

    async def test_delete_returns_false_when_not_found(
        self,
        svc: RunbookService,
        mock_repo: MagicMock,
    ) -> None:
        """delete() returns False when runbook does not exist."""
        mock_repo.delete.return_value = False

        result = await svc.delete(uuid.uuid4())

        assert result is False


# ---------------------------------------------------------------------------
# TestInit
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestSemanticSearch
# ---------------------------------------------------------------------------


class TestSemanticSearch:
    async def test_semantic_search_embeds_and_delegates_to_vector_search(
        self,
        svc_with_embedding: RunbookService,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """semantic_search() calls embed() then repo.vector_search()."""
        runbook = make_runbook()
        mock_repo.vector_search = AsyncMock(return_value=[(runbook, 0.9)])

        results = await svc_with_embedding.semantic_search("deploy app")

        mock_embedding_svc.embed.assert_awaited_once_with("deploy app")
        mock_repo.vector_search.assert_awaited_once()
        assert len(results) == 1
        assert results[0] == (runbook, 0.9)

    async def test_semantic_search_passes_project_key_and_limit(
        self,
        svc_with_embedding: RunbookService,
        mock_repo: MagicMock,
    ) -> None:
        """semantic_search() forwards project_key and limit to repo."""
        mock_repo.vector_search = AsyncMock(return_value=[])

        await svc_with_embedding.semantic_search("query", project_key="my-proj", limit=5)

        call_kwargs = mock_repo.vector_search.call_args.kwargs
        assert call_kwargs["project_key"] == "my-proj"
        assert call_kwargs["limit"] == 5

    async def test_semantic_search_returns_empty_without_embedding_svc(
        self,
        svc: RunbookService,
    ) -> None:
        """semantic_search() returns [] when no embedding_svc is configured."""
        results = await svc.semantic_search("query")
        assert results == []

    async def test_semantic_search_returns_list_of_tuples(
        self,
        svc_with_embedding: RunbookService,
        mock_repo: MagicMock,
    ) -> None:
        """semantic_search() returns list[tuple[Runbook, float]]."""
        r1, r2 = make_runbook(title="R1"), make_runbook(title="R2")
        mock_repo.vector_search = AsyncMock(return_value=[(r1, 0.9), (r2, 0.7)])

        results = await svc_with_embedding.semantic_search("deploy")

        assert len(results) == 2
        assert all(isinstance(r, tuple) and len(r) == 2 for r in results)


# ---------------------------------------------------------------------------
# TestInit
# ---------------------------------------------------------------------------


class TestInit:
    def test_init_without_embedding_svc(self, mock_repo: MagicMock) -> None:
        """RunbookService can be constructed with only pg_repo (PG-only mode)."""
        service = RunbookService(pg_repo=mock_repo)
        assert service._repo is mock_repo
        assert service._embedding_svc is None

    def test_init_with_embedding_svc(
        self,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """RunbookService stores injected embedding_svc."""
        service = RunbookService(pg_repo=mock_repo, embedding_svc=mock_embedding_svc)
        assert service._embedding_svc is mock_embedding_svc


# ---------------------------------------------------------------------------
# Graph write-through
# ---------------------------------------------------------------------------


class TestGraphWriteThrough:
    async def test_create_calls_graph_upsert(
        self,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """create() calls graph.upsert_node and link_to_project after PG write."""
        runbook = make_runbook()
        mock_repo.create.return_value = runbook
        mock_graph = MagicMock()
        mock_graph.upsert_node = AsyncMock()
        mock_graph.link_to_project = AsyncMock()

        svc = RunbookService(
            pg_repo=mock_repo,
            embedding_svc=mock_embedding_svc,
            graph=mock_graph,
        )
        data = RunbookCreate(
            title="Deploy App",
            description="Deploy to prod",
            project_key="my-proj",
            trigger="On release",
        )
        await svc.create(data)

        mock_graph.upsert_node.assert_awaited_once_with(
            "Runbook",
            runbook.id,
            {"project_key": "my-proj", "title": "Deploy App"},
        )
        mock_graph.link_to_project.assert_awaited_once_with(runbook.id, "my-proj")

    async def test_create_graph_failure_does_not_raise(
        self,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """create() succeeds even if graph.upsert_node raises."""
        runbook = make_runbook()
        mock_repo.create.return_value = runbook
        mock_graph = MagicMock()
        mock_graph.upsert_node = AsyncMock(side_effect=RuntimeError("neo4j down"))

        svc = RunbookService(
            pg_repo=mock_repo,
            embedding_svc=mock_embedding_svc,
            graph=mock_graph,
        )
        data = RunbookCreate(
            title="Deploy App",
            description="Deploy to prod",
            project_key="my-proj",
            trigger="On release",
        )
        result = await svc.create(data)
        assert result == runbook

    async def test_create_without_graph_works(self, mock_repo: MagicMock) -> None:
        """create() works normally when graph=None."""
        data = RunbookCreate(
            title="Deploy App",
            description="Deploy the app",
            project_key="my-proj",
            trigger="On release",
        )
        result = await RunbookService(pg_repo=mock_repo).create(data)
        assert isinstance(result, Runbook)

    async def test_delete_removes_graph_node(
        self,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """delete() calls graph.delete_node after PG delete."""
        mock_repo.delete.return_value = True
        mock_graph = MagicMock()
        mock_graph.delete_node = AsyncMock()

        svc = RunbookService(
            pg_repo=mock_repo,
            embedding_svc=mock_embedding_svc,
            graph=mock_graph,
        )
        runbook_id = uuid.uuid4()
        result = await svc.delete(runbook_id)

        mock_graph.delete_node.assert_awaited_once_with("Runbook", runbook_id)
        assert result is True
