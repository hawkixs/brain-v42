"""Unit tests for ADRService.

All tests use AsyncMock/MagicMock — no real DB or ONNX model required.
Tests verify behavior of ADRService as a thin orchestration layer.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.models.adr import ADR, ADRCreate, ADRUpdate
from brain_v42.models.project_context import ProjectContext
from brain_v42.repositories.pg_project_context import PgProjectContextRepo
from brain_v42.services.adr_service import ADRService
from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable
from brain_v42.services.ticket_service import UnknownProjectError

# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------


def make_adr(**kwargs) -> ADR:
    """Helper to build an ADR with sensible defaults."""
    defaults = {
        "id": uuid.uuid4(),
        "number": 1,
        "title": "Use PostgreSQL",
        "context": "Need a persistent store",
        "decision": "Use PostgreSQL with pgvector",
        "consequences": "Requires migrations",
        "alternatives_considered": [],
        "project_key": "brain-v42",
        "tags": ["db", "postgres"],
        "status": "proposed",
        "decided_at": None,
        "superseded_by": None,
        "embedding": None,
        "metadata": {},
    }
    defaults.update(kwargs)
    return ADR.model_validate(defaults)


@pytest.fixture
def mock_repo() -> MagicMock:
    """Mock PgADRRepo with all async methods."""
    repo = MagicMock()
    repo.create = AsyncMock()
    repo.set_embedding_if_current = AsyncMock(return_value=None)
    repo.accept = AsyncMock()
    repo.search = AsyncMock()
    repo.vector_search = AsyncMock()
    repo.list_all = AsyncMock()
    repo.get_by_id = AsyncMock()
    repo.get_by_number = AsyncMock()
    repo.update = AsyncMock()
    repo.delete = AsyncMock()
    return repo


@pytest.fixture
def mock_embedding_svc() -> MagicMock:
    """Mock EmbeddingService with async embed method."""
    svc = MagicMock()
    svc.embed = AsyncMock(return_value=[0.1] * 1536)
    return svc


@pytest.fixture
def service(mock_repo: MagicMock) -> ADRService:
    """ADRService with mock repo, no embedding_svc."""
    return ADRService(pg_repo=mock_repo)


@pytest.fixture
def service_with_embedding(mock_repo: MagicMock, mock_embedding_svc: MagicMock) -> ADRService:
    """ADRService with mock repo and mock embedding_svc."""
    return ADRService(pg_repo=mock_repo, embedding_svc=mock_embedding_svc)


def make_adr_create(**kwargs) -> ADRCreate:
    """Helper to build an ADRCreate with sensible defaults."""
    defaults = {
        "title": "Use PostgreSQL",
        "context": "Need a persistent store",
        "decision": "Use PostgreSQL with pgvector",
        "consequences": "Requires migrations",
        "alternatives_considered": [],
        "project_key": "brain-v42",
        "tags": ["db"],
        "status": "proposed",
        "metadata": {},
    }
    defaults.update(kwargs)
    return ADRCreate.model_validate(defaults)


# ---------------------------------------------------------------------------
# create()
# ---------------------------------------------------------------------------


class TestCreate:
    async def test_create_without_embedding_svc_calls_repo_with_none_embedding(
        self,
        service: ADRService,
        mock_repo: MagicMock,
    ) -> None:
        """create() with no embedding_svc calls repo.create(data, embedding=None)."""
        adr = make_adr()
        mock_repo.create.return_value = adr
        data = make_adr_create()

        result = await service.create(data)

        mock_repo.create.assert_called_once_with(data, embedding=None)
        assert result == adr

    async def test_create_with_embedding_svc_generates_embedding_from_fields(
        self,
        service_with_embedding: ADRService,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """create() with embedding_svc calls embed with title+context+decision."""
        adr = make_adr()
        mock_repo.create.return_value = adr
        fake_embedding = [0.2] * 1536
        mock_embedding_svc.embed.return_value = fake_embedding
        mock_repo.set_embedding_if_current.return_value = adr.model_copy(
            update={"embedding": fake_embedding}
        ).model_dump()
        data = make_adr_create(title="T", context="C", decision="D")

        result = await service_with_embedding.create(data)

        expected_text = "T C D"
        mock_embedding_svc.embed.assert_called_once_with(expected_text)
        mock_repo.create.assert_awaited_once_with(data, embedding=None)
        mock_repo.set_embedding_if_current.assert_awaited_once_with(
            adr.id,
            fake_embedding,
            expected_updated_at=adr.updated_at,
        )
        assert result.id == adr.id
        assert result.embedding == fake_embedding

    async def test_create_returns_durable_adr_when_embedding_unavailable(
        self,
        service_with_embedding: ADRService,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        adr = make_adr()
        mock_repo.create.return_value = adr
        mock_embedding_svc.embed.side_effect = EmbeddingUnavailable("offline", kind="unreachable")
        data = make_adr_create()

        result = await service_with_embedding.create(data)

        assert result is adr
        mock_repo.create.assert_awaited_once_with(data, embedding=None)
        mock_repo.set_embedding_if_current.assert_not_awaited()

    async def test_create_commits_before_attempting_embedding(
        self,
        service_with_embedding: ADRService,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        events: list[str] = []
        adr = make_adr()

        async def create_pending(data, *, embedding):
            events.append("create")
            return adr

        async def fail_embedding(text):
            events.append("embed")
            raise EmbeddingUnavailable("offline", kind="unreachable")

        mock_repo.create.side_effect = create_pending
        mock_embedding_svc.embed.side_effect = fail_embedding

        result = await service_with_embedding.create(make_adr_create())

        assert result is adr
        assert events == ["create", "embed"]

    async def test_create_returns_adr_from_repo(
        self,
        service: ADRService,
        mock_repo: MagicMock,
    ) -> None:
        """create() returns the ADR returned by repo.create."""
        adr = make_adr(number=5, title="Custom ADR")
        mock_repo.create.return_value = adr
        data = make_adr_create()

        result = await service.create(data)

        assert result.number == 5
        assert result.title == "Custom ADR"


# ---------------------------------------------------------------------------
# create() — project existence guard (fail-closed)
# ---------------------------------------------------------------------------


def _make_service_with_ctx_repo(
    mock_repo: MagicMock,
    ctx_get_by_key_return: ProjectContext | None,
) -> tuple[ADRService, MagicMock]:
    """Return (service, project_context_repo_mock) with the guard wired."""
    mock_ctx_repo = MagicMock(spec=PgProjectContextRepo)
    mock_ctx_repo.get_by_key = AsyncMock(return_value=ctx_get_by_key_return)
    svc = ADRService(pg_repo=mock_repo, project_context_repo=mock_ctx_repo)
    return svc, mock_ctx_repo


class TestADRServiceProjectGuard:
    async def test_create_with_unknown_project_key_raises_and_names_the_key(
        self, mock_repo: MagicMock
    ) -> None:
        svc, mock_ctx_repo = _make_service_with_ctx_repo(mock_repo, ctx_get_by_key_return=None)
        data = make_adr_create(project_key="red-backup")

        with pytest.raises(UnknownProjectError, match="red-backup"):
            await svc.create(data)

        mock_repo.create.assert_not_awaited()
        mock_ctx_repo.get_by_key.assert_awaited_once_with("red-backup", session=None)

    async def test_create_with_known_project_key_succeeds(self, mock_repo: MagicMock) -> None:
        adr = make_adr()
        mock_repo.create.return_value = adr
        existing = ProjectContext(
            project_key="brain-v42", name="brain-v42", description="knowledge base"
        )
        svc, mock_ctx_repo = _make_service_with_ctx_repo(mock_repo, ctx_get_by_key_return=existing)
        data = make_adr_create(project_key="brain-v42")

        result = await svc.create(data)

        mock_ctx_repo.get_by_key.assert_awaited_once_with("brain-v42", session=None)
        mock_repo.create.assert_awaited_once()
        assert result is not None

    async def test_create_canonicalizes_alias_before_checking_the_guard(
        self, mock_repo: MagicMock
    ) -> None:
        """project_key='brain_v42' canonicalizes to 'brain-v42' before the guard runs."""
        svc, mock_ctx_repo = _make_service_with_ctx_repo(mock_repo, ctx_get_by_key_return=None)
        data = make_adr_create(project_key="brain_v42")

        with pytest.raises(UnknownProjectError):
            await svc.create(data)

        mock_ctx_repo.get_by_key.assert_awaited_once_with("brain-v42", session=None)

    async def test_create_without_project_context_repo_skips_the_guard(
        self, service: ADRService, mock_repo: MagicMock
    ) -> None:
        """Backward compatibility: project_context_repo is optional at construction."""
        adr = make_adr()
        mock_repo.create.return_value = adr
        data = make_adr_create()

        result = await service.create(data)

        mock_repo.create.assert_awaited_once()
        assert result is not None


# ---------------------------------------------------------------------------
# accept()
# ---------------------------------------------------------------------------


class TestAccept:
    async def test_accept_delegates_to_repo(
        self,
        service: ADRService,
        mock_repo: MagicMock,
    ) -> None:
        """accept() delegates to repo.accept and returns the updated ADR."""
        adr = make_adr(status="accepted")
        mock_repo.accept.return_value = adr
        adr_id = adr.id

        result = await service.accept(adr_id)

        mock_repo.accept.assert_called_once_with(adr_id)
        assert result == adr

    async def test_accept_returns_none_when_not_found(
        self,
        service: ADRService,
        mock_repo: MagicMock,
    ) -> None:
        """accept() returns None when repo returns None (ADR not found)."""
        mock_repo.accept.return_value = None
        missing_id = uuid.uuid4()

        result = await service.accept(missing_id)

        assert result is None


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


class TestSearch:
    async def test_search_delegates_all_params_to_repo(
        self,
        service: ADRService,
        mock_repo: MagicMock,
    ) -> None:
        """search() forwards all parameters to repo.search."""
        adrs = [make_adr(), make_adr()]
        mock_repo.search.return_value = adrs

        result = await service.search(
            query="postgres",
            project_key="brain-v42",
            status="proposed",
            tags=["db"],
            limit=5,
            offset=2,
        )

        mock_repo.search.assert_called_once_with(
            query="postgres",
            project_key="brain-v42",
            project_keys=None,
            status="proposed",
            tags=["db"],
            limit=5,
            offset=2,
        )
        assert result == adrs

    async def test_search_with_defaults(
        self,
        service: ADRService,
        mock_repo: MagicMock,
    ) -> None:
        """search() works with no parameters (all defaults)."""
        mock_repo.search.return_value = []

        result = await service.search()

        mock_repo.search.assert_called_once()
        assert result == []

    async def test_search_returns_list_of_adrs(
        self,
        service: ADRService,
        mock_repo: MagicMock,
    ) -> None:
        """search() returns a list of ADR models."""
        adr = make_adr()
        mock_repo.search.return_value = [adr]

        result = await service.search(query="test")

        assert len(result) == 1
        assert isinstance(result[0], ADR)


# ---------------------------------------------------------------------------
# semantic_search()
# ---------------------------------------------------------------------------


class TestSemanticSearch:
    async def test_semantic_search_returns_empty_when_no_embedding_svc(
        self,
        service: ADRService,
        mock_repo: MagicMock,
    ) -> None:
        """semantic_search() returns [] when embedding_svc is None."""
        result = await service.semantic_search("query about postgres")

        mock_repo.vector_search.assert_not_called()
        assert result == []

    async def test_semantic_search_embeds_query_and_calls_repo(
        self,
        service_with_embedding: ADRService,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """semantic_search() embeds query then calls repo.vector_search."""
        adr = make_adr()
        scored_results = [(adr, 0.95)]
        mock_repo.vector_search.return_value = scored_results
        query_embedding = [0.3] * 1536
        mock_embedding_svc.embed.return_value = query_embedding

        result = await service_with_embedding.semantic_search(
            "postgres architecture",
            project_key="brain-v42",
            limit=5,
        )

        mock_embedding_svc.embed.assert_called_once_with("postgres architecture")
        mock_repo.vector_search.assert_called_once_with(
            query_embedding=query_embedding,
            limit=5,
            project_key="brain-v42",
            project_keys=None,
        )
        assert result == scored_results

    async def test_semantic_search_returns_scored_tuples(
        self,
        service_with_embedding: ADRService,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """semantic_search() returns list[tuple[ADR, float]]."""
        adr = make_adr()
        mock_repo.vector_search.return_value = [(adr, 0.85)]
        mock_embedding_svc.embed.return_value = [0.1] * 1536

        result = await service_with_embedding.semantic_search("test")

        assert len(result) == 1
        assert isinstance(result[0][0], ADR)
        assert isinstance(result[0][1], float)


# ---------------------------------------------------------------------------
# list_all()
# ---------------------------------------------------------------------------


class TestListAll:
    async def test_list_all_delegates_to_repo(
        self,
        service: ADRService,
        mock_repo: MagicMock,
    ) -> None:
        """list_all() delegates to repo.list_all with optional filters."""
        adrs = [make_adr(), make_adr()]
        mock_repo.list_all.return_value = adrs

        result = await service.list_all(
            project_key="brain-v42",
            status="proposed",
            limit=10,
            offset=0,
        )

        mock_repo.list_all.assert_called_once_with(
            project_key="brain-v42",
            status="proposed",
            limit=10,
            offset=0,
            include_archived=False,
        )
        assert result == adrs

    async def test_list_all_with_no_filters(
        self,
        service: ADRService,
        mock_repo: MagicMock,
    ) -> None:
        """list_all() works with no filters."""
        mock_repo.list_all.return_value = []

        result = await service.list_all()

        mock_repo.list_all.assert_called_once()
        assert result == []


# ---------------------------------------------------------------------------
# get_by_id()
# ---------------------------------------------------------------------------


class TestGetById:
    async def test_get_by_id_delegates_to_repo(
        self,
        service: ADRService,
        mock_repo: MagicMock,
    ) -> None:
        """get_by_id() delegates to repo.get_by_id and returns the ADR."""
        adr = make_adr()
        mock_repo.get_by_id.return_value = adr
        adr_id = adr.id

        result = await service.get_by_id(adr_id)

        mock_repo.get_by_id.assert_called_once_with(adr_id)
        assert result == adr

    async def test_get_by_id_returns_none_when_not_found(
        self,
        service: ADRService,
        mock_repo: MagicMock,
    ) -> None:
        """get_by_id() returns None when repo returns None."""
        mock_repo.get_by_id.return_value = None
        missing_id = uuid.uuid4()

        result = await service.get_by_id(missing_id)

        assert result is None


# ---------------------------------------------------------------------------
# get_by_number()
# ---------------------------------------------------------------------------


class TestGetByNumber:
    async def test_get_by_number_delegates_to_repo(
        self,
        service: ADRService,
        mock_repo: MagicMock,
    ) -> None:
        """get_by_number() delegates to repo.get_by_number."""
        adr = make_adr(number=3)
        mock_repo.get_by_number.return_value = adr

        result = await service.get_by_number(3, "brain-v42")

        mock_repo.get_by_number.assert_called_once_with(3, "brain-v42")
        assert result == adr

    async def test_get_by_number_returns_none_when_not_found(
        self,
        service: ADRService,
        mock_repo: MagicMock,
    ) -> None:
        """get_by_number() returns None when ADR not found."""
        mock_repo.get_by_number.return_value = None

        result = await service.get_by_number(999, "brain-v42")

        assert result is None


# ---------------------------------------------------------------------------
# deprecate()
# ---------------------------------------------------------------------------


class TestDeprecate:
    async def test_deprecate_sets_status_deprecated_and_appends_reason(
        self,
        service: ADRService,
        mock_repo: MagicMock,
    ) -> None:
        """deprecate() sets status='deprecated' and appends reason to consequences."""
        existing = make_adr(consequences="Requires migrations")
        deprecated = make_adr(
            id=existing.id,
            status="deprecated",
            consequences="Requires migrations\n\nDeprecated: No longer needed",
        )
        mock_repo.get_by_id.return_value = existing
        mock_repo.update.return_value = deprecated

        result = await service.deprecate(existing.id, reason="No longer needed")

        mock_repo.get_by_id.assert_called_once_with(existing.id)
        call_args = mock_repo.update.call_args
        assert call_args[0][0] == existing.id
        update_data: ADRUpdate = call_args[0][1]
        assert update_data.status == "deprecated"
        assert update_data.consequences == "Requires migrations\n\nDeprecated: No longer needed"
        assert result == deprecated

    async def test_deprecate_without_reason_appends_no_reason_text(
        self,
        service: ADRService,
        mock_repo: MagicMock,
    ) -> None:
        """deprecate() without reason only sets status='deprecated'."""
        existing = make_adr(consequences="Some consequences")
        deprecated = make_adr(id=existing.id, status="deprecated", consequences="Some consequences")
        mock_repo.get_by_id.return_value = existing
        mock_repo.update.return_value = deprecated

        result = await service.deprecate(existing.id)

        call_args = mock_repo.update.call_args
        update_data: ADRUpdate = call_args[0][1]
        assert update_data.status == "deprecated"
        assert update_data.consequences == "Some consequences"
        assert result == deprecated

    async def test_deprecate_returns_none_when_not_found(
        self,
        service: ADRService,
        mock_repo: MagicMock,
    ) -> None:
        """deprecate() returns None when ADR not found."""
        mock_repo.get_by_id.return_value = None
        missing_id = uuid.uuid4()

        result = await service.deprecate(missing_id, reason="gone")

        mock_repo.get_by_id.assert_called_once_with(missing_id)
        mock_repo.update.assert_not_called()
        assert result is None

    async def test_deprecate_with_empty_consequences_and_reason(
        self,
        service: ADRService,
        mock_repo: MagicMock,
    ) -> None:
        """deprecate() with empty consequences and a reason produces clean text."""
        existing = make_adr(consequences="")
        deprecated = make_adr(
            id=existing.id, status="deprecated", consequences="Deprecated: Replaced"
        )
        mock_repo.get_by_id.return_value = existing
        mock_repo.update.return_value = deprecated

        result = await service.deprecate(existing.id, reason="Replaced")

        call_args = mock_repo.update.call_args
        update_data: ADRUpdate = call_args[0][1]
        assert update_data.consequences == "Deprecated: Replaced"
        assert result == deprecated


# ---------------------------------------------------------------------------
# delete()
# ---------------------------------------------------------------------------


class TestDelete:
    async def test_delete_delegates_to_repo_and_returns_true(
        self,
        service: ADRService,
        mock_repo: MagicMock,
    ) -> None:
        """delete() delegates to repo.delete and returns True when found."""
        mock_repo.delete = AsyncMock(return_value=True)
        adr_id = uuid.uuid4()

        result = await service.delete(adr_id)

        mock_repo.delete.assert_called_once_with(adr_id)
        assert result is True

    async def test_delete_returns_false_when_not_found(
        self,
        service: ADRService,
        mock_repo: MagicMock,
    ) -> None:
        """delete() returns False when repo returns False (not found)."""
        mock_repo.delete = AsyncMock(return_value=False)
        missing_id = uuid.uuid4()

        result = await service.delete(missing_id)

        mock_repo.delete.assert_called_once_with(missing_id)
        assert result is False


# ---------------------------------------------------------------------------
# update()
# ---------------------------------------------------------------------------


class TestUpdate:
    async def test_update_without_embedding_svc_delegates_with_no_embedding(
        self,
        service: ADRService,
        mock_repo: MagicMock,
    ) -> None:
        """update() without embedding_svc delegates to repo.update with embedding=None."""
        adr = make_adr()
        mock_repo.update = AsyncMock(return_value=adr)
        data = ADRUpdate(tags=["new-tag"])
        adr_id = adr.id

        result = await service.update(adr_id, data)

        mock_repo.update.assert_called_once_with(adr_id, data, embedding=None)
        assert result == adr

    async def test_update_with_title_change_re_embeds(
        self,
        service_with_embedding: ADRService,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """update() re-embeds when title changes."""
        existing = make_adr(title="Old Title", context="Ctx", decision="Dec")
        updated = make_adr(id=existing.id, title="New Title")
        mock_repo.get_by_id.return_value = existing
        fake_embedding = [0.5] * 1536
        mock_embedding_svc.embed.return_value = fake_embedding
        mock_repo.update = AsyncMock(return_value=updated)
        data = ADRUpdate(title="New Title")

        result = await service_with_embedding.update(existing.id, data)

        mock_repo.get_by_id.assert_called_once_with(existing.id)
        mock_embedding_svc.embed.assert_called_once_with("New Title Ctx Dec")
        mock_repo.update.assert_called_once_with(existing.id, data, embedding=fake_embedding)
        assert result == updated

    async def test_update_with_context_change_re_embeds(
        self,
        service_with_embedding: ADRService,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """update() re-embeds when context changes."""
        existing = make_adr(title="T", context="Old Ctx", decision="D")
        mock_repo.get_by_id.return_value = existing
        mock_repo.update = AsyncMock(return_value=make_adr())
        mock_embedding_svc.embed.return_value = [0.1] * 1536
        data = ADRUpdate(context="New Ctx")

        await service_with_embedding.update(existing.id, data)

        # The merged text should use the new context
        mock_embedding_svc.embed.assert_called_once_with("T New Ctx D")

    async def test_update_with_decision_change_re_embeds(
        self,
        service_with_embedding: ADRService,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """update() re-embeds when decision changes."""
        existing = make_adr(title="T", context="C", decision="Old Dec")
        mock_repo.get_by_id.return_value = existing
        mock_repo.update = AsyncMock(return_value=make_adr())
        mock_embedding_svc.embed.return_value = [0.1] * 1536
        data = ADRUpdate(decision="New Dec")

        await service_with_embedding.update(existing.id, data)

        mock_embedding_svc.embed.assert_called_once_with("T C New Dec")

    async def test_update_without_semantic_fields_skips_re_embed(
        self,
        service_with_embedding: ADRService,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """update() does NOT re-embed when only non-semantic fields change (e.g. tags)."""
        adr = make_adr()
        mock_repo.update = AsyncMock(return_value=adr)
        data = ADRUpdate(tags=["updated-tag"])

        result = await service_with_embedding.update(adr.id, data)

        mock_embedding_svc.embed.assert_not_called()
        mock_repo.get_by_id.assert_not_called()
        mock_repo.update.assert_called_once_with(adr.id, data, embedding=None)
        assert result == adr

    async def test_update_returns_none_when_not_found(
        self,
        service: ADRService,
        mock_repo: MagicMock,
    ) -> None:
        """update() returns None when repo.update returns None."""
        mock_repo.update = AsyncMock(return_value=None)
        data = ADRUpdate(tags=["x"])
        missing_id = uuid.uuid4()

        result = await service.update(missing_id, data)

        assert result is None

    async def test_update_re_embed_fetches_existing_for_merge(
        self,
        service_with_embedding: ADRService,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """update() fetches existing ADR to merge fields for embedding text."""
        existing = make_adr(title="Orig Title", context="Orig Ctx", decision="Orig Dec")
        updated = make_adr(
            id=existing.id, title="Orig Title", context="New Ctx", decision="Orig Dec"
        )
        mock_repo.get_by_id.return_value = existing
        mock_repo.update = AsyncMock(return_value=updated)
        mock_embedding_svc.embed.return_value = [0.2] * 1536
        # Only changing context
        data = ADRUpdate(context="New Ctx")

        result = await service_with_embedding.update(existing.id, data)

        # Embedding text merges: existing title + updated context + existing decision
        mock_embedding_svc.embed.assert_called_once_with("Orig Title New Ctx Orig Dec")
        assert result == updated


# ---------------------------------------------------------------------------
# Graph write-through
# ---------------------------------------------------------------------------


class TestADRServiceGraphWriteThrough:
    async def test_create_calls_graph_upsert(
        self,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """create() calls graph.upsert_node and link_to_project after PG write."""
        adr = make_adr()
        mock_repo.create.return_value = adr
        mock_graph = MagicMock()
        mock_graph.upsert_node = AsyncMock()
        mock_graph.link_to_project = AsyncMock()

        svc = ADRService(pg_repo=mock_repo, embedding_svc=mock_embedding_svc, graph=mock_graph)
        data = make_adr_create(title="Use PostgreSQL", project_key="brain-v42")
        await svc.create(data)

        mock_graph.upsert_node.assert_awaited_once_with(
            "ADR",
            adr.id,
            {"project_key": "brain-v42", "title": "Use PostgreSQL"},
        )
        mock_graph.link_to_project.assert_awaited_once_with(adr.id, "brain-v42")

    async def test_create_graph_failure_does_not_raise(
        self,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """create() succeeds even if graph.upsert_node raises."""
        adr = make_adr()
        mock_repo.create.return_value = adr
        mock_graph = MagicMock()
        mock_graph.upsert_node = AsyncMock(side_effect=RuntimeError("neo4j down"))

        svc = ADRService(pg_repo=mock_repo, embedding_svc=mock_embedding_svc, graph=mock_graph)
        result = await svc.create(make_adr_create())
        assert result == adr

    async def test_create_without_graph_works(
        self, service: ADRService, mock_repo: MagicMock
    ) -> None:
        """create() works normally when graph=None."""
        adr = make_adr()
        mock_repo.create.return_value = adr
        result = await service.create(make_adr_create())
        assert result == adr

    async def test_delete_removes_graph_node(
        self,
        mock_repo: MagicMock,
        mock_embedding_svc: MagicMock,
    ) -> None:
        """delete() calls graph.delete_node after PG delete."""
        mock_repo.delete.return_value = True
        mock_graph = MagicMock()
        mock_graph.delete_node = AsyncMock()

        svc = ADRService(pg_repo=mock_repo, embedding_svc=mock_embedding_svc, graph=mock_graph)
        adr_id = uuid.uuid4()
        result = await svc.delete(adr_id)

        mock_graph.delete_node.assert_awaited_once_with("ADR", adr_id)
        assert result is True
