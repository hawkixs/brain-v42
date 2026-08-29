"""Unit tests for LearningService.

All tests use AsyncMock/MagicMock — no real DB or ONNX model required.
Tests verify behavior of LearningService as a thin orchestration layer.

Test classes:
- TestLearningServiceImport: import + interface checks
- TestLearningServiceCreate: create with/without embedding_svc
- TestLearningServiceSearch: FTS search delegation
- TestLearningServiceSemanticSearch: semantic search with/without embedding_svc
- TestLearningServiceValidate: validate delegation
- TestLearningServiceListAll: list_all with filters
- TestLearningServiceUpdate: update with/without embedding regeneration
- TestLearningServiceDelete: delete delegation
- TestLearningServiceGetById: get_by_id delegation
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.models.learning import Learning, LearningCreate, LearningUpdate
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.repositories.pg_project_context import PgProjectContextRepo
from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable
from brain_v42.services.learning_service import LearningService
from brain_v42.services.ticket_service import UnknownProjectError

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

FIXED_UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")
NOW = datetime.now(UTC)
FAKE_EMBEDDING = [0.1] * 1536

SAMPLE_LEARNING = Learning(
    id=FIXED_UUID,
    topic="Use async sessions",
    insight="Always use async context managers for SQLAlchemy sessions",
    source="experience",
    source_type="experience",
    confidence="high",
    project_key="brain-v42",
    tags=["db", "sqlalchemy"],
    metadata={},
    validated_at=None,
    embedding=None,
    created_at=NOW,
    updated_at=NOW,
)


def _make_service(
    with_embedding_svc: bool = False,
) -> tuple[LearningService, MagicMock, MagicMock | None]:
    """Return (service, repo_mock, embedding_svc_mock | None)."""
    mock_repo = MagicMock(spec=PgLearningRepo)
    mock_repo.create = AsyncMock(return_value=SAMPLE_LEARNING)
    mock_repo.set_embedding_if_current = AsyncMock(
        return_value=SAMPLE_LEARNING.model_copy(update={"embedding": FAKE_EMBEDDING}).model_dump()
    )
    mock_repo.get_by_id = AsyncMock(return_value=SAMPLE_LEARNING)
    mock_repo.update = AsyncMock(return_value=SAMPLE_LEARNING)
    mock_repo.delete = AsyncMock(return_value=True)
    mock_repo.list_all = AsyncMock(return_value=[SAMPLE_LEARNING])
    mock_repo.search_fts = AsyncMock(return_value=[SAMPLE_LEARNING])
    mock_repo.search_vector = AsyncMock(return_value=[(SAMPLE_LEARNING, 0.9)])
    mock_repo.validate = AsyncMock(return_value=SAMPLE_LEARNING)

    if with_embedding_svc:
        mock_embedding_svc = MagicMock()
        mock_embedding_svc.embed = AsyncMock(return_value=FAKE_EMBEDDING)
        mock_embedding_svc.embed_query = AsyncMock(return_value=FAKE_EMBEDDING)
        return (
            LearningService(pg_repo=mock_repo, embedding_svc=mock_embedding_svc),
            mock_repo,
            mock_embedding_svc,
        )

    return LearningService(pg_repo=mock_repo), mock_repo, None


# ---------------------------------------------------------------------------
# Import / interface checks
# ---------------------------------------------------------------------------


class TestLearningServiceImport:
    def test_importable(self) -> None:
        """LearningService can be imported from services package."""
        from brain_v42.services import LearningService as LS  # noqa: F401

        assert LS is LearningService

    def test_has_required_methods(self) -> None:
        """LearningService exposes all required methods."""
        required = [
            "create",
            "search",
            "semantic_search",
            "validate",
            "list_all",
            "update",
            "delete",
            "get_by_id",
        ]
        for method in required:
            assert hasattr(LearningService, method), f"LearningService missing method: {method}"


# ---------------------------------------------------------------------------
# create()
# ---------------------------------------------------------------------------


class TestLearningServiceCreate:
    async def test_create_in_caller_transaction_defers_derived_work(self) -> None:
        """A caller-owned transaction controls durability and leaves enrichment queued."""
        svc, mock_repo, mock_embedding_svc = _make_service(with_embedding_svc=True)
        session = MagicMock(spec=AsyncSession)
        data = LearningCreate(
            topic="Atomic proposal",
            insight="Insert and proposal finalization share one commit",
            project_key="brain-v42",
        )

        result = await svc.create(data, session=session)

        mock_repo.create.assert_awaited_once_with(
            data,
            embedding=None,
            session=session,
        )
        assert mock_embedding_svc is not None
        mock_embedding_svc.embed.assert_not_awaited()
        mock_repo.set_embedding_if_current.assert_not_awaited()
        assert result is SAMPLE_LEARNING

    async def test_create_without_embedding_service(self) -> None:
        """create() works without embedding_svc — passes embedding=None to repo."""
        svc, mock_repo, _ = _make_service(with_embedding_svc=False)

        data = LearningCreate(
            topic="Async patterns",
            insight="Always await async functions",
            project_key="brain-v42",
        )
        result = await svc.create(data)

        mock_repo.create.assert_awaited_once_with(data, embedding=None)
        mock_repo.set_embedding_if_current.assert_not_awaited()
        assert result is SAMPLE_LEARNING

    async def test_create_with_embedding_service(self) -> None:
        """create() calls embedding_svc.embed(topic + insight) when svc is available."""
        svc, mock_repo, mock_embedding_svc = _make_service(with_embedding_svc=True)

        data = LearningCreate(
            topic="Use async sessions",
            insight="Always use async context managers for SQLAlchemy sessions",
            project_key="brain-v42",
        )
        result = await svc.create(data)

        assert mock_embedding_svc is not None
        mock_embedding_svc.embed.assert_awaited_once()
        call_text = mock_embedding_svc.embed.call_args[0][0]
        assert "Use async sessions" in call_text
        assert "Always use async context managers for SQLAlchemy sessions" in call_text

        mock_repo.create.assert_awaited_once_with(data, embedding=None)
        mock_repo.set_embedding_if_current.assert_awaited_once_with(
            SAMPLE_LEARNING.id,
            FAKE_EMBEDDING,
            expected_updated_at=SAMPLE_LEARNING.updated_at,
        )
        assert result.id == SAMPLE_LEARNING.id
        assert result.embedding == FAKE_EMBEDDING

    async def test_create_returns_durable_learning_when_embedding_unavailable(self) -> None:
        """An embedding outage never turns a committed Learning into an error."""
        svc, mock_repo, mock_embedding_svc = _make_service(with_embedding_svc=True)
        assert mock_embedding_svc is not None
        mock_embedding_svc.embed = AsyncMock(
            side_effect=EmbeddingUnavailable("offline", kind="unreachable")
        )
        mock_embedding_svc.embed_query = AsyncMock(
            side_effect=EmbeddingUnavailable("offline", kind="unreachable")
        )
        data = LearningCreate(topic="Durable", insight="Commit first")

        result = await svc.create(data)

        assert result is SAMPLE_LEARNING
        mock_repo.create.assert_awaited_once_with(data, embedding=None)
        mock_repo.set_embedding_if_current.assert_not_awaited()

    async def test_create_commits_before_attempting_embedding(self) -> None:
        events: list[str] = []
        svc, mock_repo, mock_embedding_svc = _make_service(with_embedding_svc=True)
        assert mock_embedding_svc is not None

        async def create_pending(data, *, embedding):
            events.append("create")
            return SAMPLE_LEARNING

        async def fail_embedding(text):
            events.append("embed")
            raise EmbeddingUnavailable("offline", kind="unreachable")

        mock_repo.create = AsyncMock(side_effect=create_pending)
        mock_embedding_svc.embed = AsyncMock(side_effect=fail_embedding)
        mock_embedding_svc.embed_query = AsyncMock(side_effect=fail_embedding)

        result = await svc.create(LearningCreate(topic="Order", insight="PG first"))

        assert result is SAMPLE_LEARNING
        assert events == ["create", "embed"]

    async def test_create_delegates_to_repo(self) -> None:
        """create() always delegates persistence to repo.create()."""
        svc, mock_repo, _ = _make_service()

        data = LearningCreate(
            topic="TDD",
            insight="Write failing tests first",
            confidence="high",
        )
        await svc.create(data)

        mock_repo.create.assert_awaited_once()

    async def test_create_returns_learning(self) -> None:
        """create() returns the Learning returned by repo.create()."""
        svc, mock_repo, _ = _make_service()
        expected = SAMPLE_LEARNING
        mock_repo.create.return_value = expected

        data = LearningCreate(topic="test", insight="insight value")
        result = await svc.create(data)

        assert result is expected


# ---------------------------------------------------------------------------
# create() — project existence guard (fail-closed)
# ---------------------------------------------------------------------------


def _make_service_with_ctx_repo(
    ctx_get_by_key_return: object | None,
) -> tuple[LearningService, MagicMock, MagicMock]:
    """Return (service, repo_mock, project_context_repo_mock) with the guard wired."""
    mock_repo = MagicMock(spec=PgLearningRepo)
    mock_repo.create = AsyncMock(return_value=SAMPLE_LEARNING)
    mock_ctx_repo = MagicMock(spec=PgProjectContextRepo)
    mock_ctx_repo.get_by_key = AsyncMock(return_value=ctx_get_by_key_return)
    svc = LearningService(pg_repo=mock_repo, project_context_repo=mock_ctx_repo)
    return svc, mock_repo, mock_ctx_repo


class TestLearningServiceProjectGuard:
    async def test_create_with_none_project_key_raises_unknown_project(self) -> None:
        svc, mock_repo, mock_ctx_repo = _make_service_with_ctx_repo(ctx_get_by_key_return=None)
        data = LearningCreate(topic="orphan", insight="no project_key supplied")

        with pytest.raises(UnknownProjectError):
            await svc.create(data)

        mock_repo.create.assert_not_awaited()
        mock_ctx_repo.get_by_key.assert_not_awaited()

    async def test_create_with_unknown_project_key_raises_and_names_the_key(self) -> None:
        svc, mock_repo, mock_ctx_repo = _make_service_with_ctx_repo(ctx_get_by_key_return=None)
        data = LearningCreate(topic="orphan", insight="unknown project", project_key="red-backup")

        with pytest.raises(UnknownProjectError, match="red-backup"):
            await svc.create(data)

        mock_repo.create.assert_not_awaited()
        mock_ctx_repo.get_by_key.assert_awaited_once_with("red-backup", session=None)

    async def test_create_with_known_project_key_succeeds(self) -> None:
        from brain_v42.models.project_context import ProjectContext

        existing = ProjectContext(
            project_key="brain-v42", name="brain-v42", description="knowledge base"
        )
        svc, mock_repo, mock_ctx_repo = _make_service_with_ctx_repo(ctx_get_by_key_return=existing)
        data = LearningCreate(
            topic="known project", insight="passes the guard", project_key="brain-v42"
        )

        result = await svc.create(data)

        mock_ctx_repo.get_by_key.assert_awaited_once_with("brain-v42", session=None)
        mock_repo.create.assert_awaited_once()
        assert result is SAMPLE_LEARNING

    async def test_create_canonicalizes_alias_before_checking_the_guard(self) -> None:
        """project_key='brain_v42' canonicalizes to 'brain-v42' before the guard runs."""
        svc, _, mock_ctx_repo = _make_service_with_ctx_repo(ctx_get_by_key_return=None)
        data = LearningCreate(topic="alias", insight="brain_v42 alias", project_key="brain_v42")

        with pytest.raises(UnknownProjectError):
            await svc.create(data)

        mock_ctx_repo.get_by_key.assert_awaited_once_with("brain-v42", session=None)

    async def test_create_without_project_context_repo_skips_the_guard(self) -> None:
        """Backward compatibility: project_context_repo is optional at construction."""
        svc, mock_repo, _ = _make_service()
        data = LearningCreate(topic="no guard wired", insight="unscoped, still succeeds")

        result = await svc.create(data)

        mock_repo.create.assert_awaited_once()
        assert result is SAMPLE_LEARNING

    async def test_create_in_caller_transaction_reuses_the_caller_session_for_the_guard(
        self,
    ) -> None:
        """The proposal-service atomic apply path passes its own session — no 2nd connection."""
        from brain_v42.models.project_context import ProjectContext

        existing = ProjectContext(
            project_key="brain-v42", name="brain-v42", description="knowledge base"
        )
        svc, mock_repo, mock_ctx_repo = _make_service_with_ctx_repo(ctx_get_by_key_return=existing)
        caller_session = MagicMock(spec=AsyncSession)
        data = LearningCreate(
            topic="dream apply", insight="ticket-extraction promotion", project_key="brain-v42"
        )

        await svc.create(data, session=caller_session)

        mock_ctx_repo.get_by_key.assert_awaited_once_with("brain-v42", session=caller_session)
        mock_repo.create.assert_awaited_once_with(data, embedding=None, session=caller_session)


# ---------------------------------------------------------------------------
# search() — FTS
# ---------------------------------------------------------------------------


class TestLearningServiceSearch:
    async def test_search_delegates_to_repo_search_fts(self) -> None:
        """search() calls repo.search_fts() with query and returns list[Learning]."""
        svc, mock_repo, _ = _make_service()
        mock_repo.search_fts.return_value = [SAMPLE_LEARNING]

        await svc.search("async patterns")

        mock_repo.search_fts.assert_awaited_once()
        call_args = mock_repo.search_fts.call_args
        assert call_args[0][0] == "async patterns"

    async def test_search_with_project_key_filter(self) -> None:
        """search() passes project_key filter to repo.search_fts()."""
        svc, mock_repo, _ = _make_service()
        mock_repo.search_fts.return_value = [SAMPLE_LEARNING]

        await svc.search("test", project_key="brain-v42")

        call_kwargs = mock_repo.search_fts.call_args.kwargs
        assert call_kwargs.get("project_key") == "brain-v42"

    async def test_search_with_confidence_filter(self) -> None:
        """search() passes confidence filter to repo.search_fts()."""
        svc, mock_repo, _ = _make_service()
        mock_repo.search_fts.return_value = [SAMPLE_LEARNING]

        await svc.search("test", confidence="high")

        call_kwargs = mock_repo.search_fts.call_args.kwargs
        assert call_kwargs.get("confidence") == "high"

    async def test_search_returns_list_of_learnings(self) -> None:
        """search() returns list[Learning] directly (no score tuples)."""
        svc, mock_repo, _ = _make_service()
        mock_repo.search_fts.return_value = [SAMPLE_LEARNING]

        results = await svc.search("pattern")

        assert isinstance(results, list)
        assert all(isinstance(r, Learning) for r in results)


# ---------------------------------------------------------------------------
# semantic_search()
# ---------------------------------------------------------------------------


class TestLearningServiceSemanticSearch:
    async def test_semantic_search_without_embedding_service_returns_empty(self) -> None:
        """semantic_search() returns [] when embedding_svc is None (graceful degradation)."""
        svc, mock_repo, _ = _make_service(with_embedding_svc=False)

        results = await svc.semantic_search("test query")

        assert results == []
        mock_repo.search_vector.assert_not_awaited()

    async def test_semantic_search_calls_embed_then_repo_search_vector(self) -> None:
        """semantic_search() embeds query then calls repo.search_vector()."""
        svc, mock_repo, mock_embedding_svc = _make_service(with_embedding_svc=True)
        mock_repo.search_vector.return_value = [(SAMPLE_LEARNING, 0.92)]

        results = await svc.semantic_search("best async patterns", project_key="brain-v42", limit=5)

        assert mock_embedding_svc is not None
        mock_embedding_svc.embed_query.assert_awaited_once_with("best async patterns")
        mock_repo.search_vector.assert_awaited_once_with(
            FAKE_EMBEDDING,
            project_key="brain-v42",
            project_keys=None,
            confidence=None,
            limit=5,
        )
        assert len(results) == 1

    async def test_semantic_search_returns_list_of_tuples(self) -> None:
        """semantic_search() returns list[tuple[Learning, float]]."""
        svc, mock_repo, _ = _make_service(with_embedding_svc=True)
        mock_repo.search_vector.return_value = [(SAMPLE_LEARNING, 0.87)]

        results = await svc.semantic_search("async")

        assert isinstance(results, list)
        assert len(results) == 1
        learning, score = results[0]
        assert isinstance(learning, Learning)
        assert isinstance(score, float)


# ---------------------------------------------------------------------------
# validate()
# ---------------------------------------------------------------------------


class TestLearningServiceValidate:
    async def test_validate_delegates_to_repo(self) -> None:
        """validate() delegates to repo.validate() with the learning_id."""
        svc, mock_repo, _ = _make_service()
        mock_repo.validate.return_value = SAMPLE_LEARNING

        result = await svc.validate(FIXED_UUID)

        mock_repo.validate.assert_awaited_once_with(FIXED_UUID)
        assert result is SAMPLE_LEARNING

    async def test_validate_forwards_optional_project_group_scope(self) -> None:
        svc, mock_repo, _ = _make_service()
        mock_repo.validate.return_value = SAMPLE_LEARNING

        result = await svc.validate(FIXED_UUID, project_group="red")

        mock_repo.validate.assert_awaited_once_with(FIXED_UUID, project_group="red")
        assert result is SAMPLE_LEARNING

    async def test_validate_returns_learning_with_validated_at(self) -> None:
        """validate() returns a Learning with validated_at set."""
        svc, mock_repo, _ = _make_service()
        validated = Learning(
            id=FIXED_UUID,
            topic="TDD",
            insight="Write tests first",
            source_type="experience",
            confidence="high",
            validated_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        mock_repo.validate.return_value = validated

        result = await svc.validate(FIXED_UUID)

        assert result is not None
        assert result.validated_at is not None

    async def test_validate_not_found_returns_none(self) -> None:
        """validate() returns None when repo.validate() returns None."""
        svc, mock_repo, _ = _make_service()
        mock_repo.validate.return_value = None

        result = await svc.validate(uuid.uuid4())

        assert result is None


# ---------------------------------------------------------------------------
# list_all()
# ---------------------------------------------------------------------------


class TestLearningServiceListAll:
    async def test_list_all_delegates_to_repo(self) -> None:
        """list_all() delegates to repo.list_all() and returns the result."""
        svc, mock_repo, _ = _make_service()
        mock_repo.list_all.return_value = [SAMPLE_LEARNING]

        results = await svc.list_all()

        mock_repo.list_all.assert_awaited_once()
        assert results == [SAMPLE_LEARNING]

    async def test_list_all_with_project_key_filter(self) -> None:
        """list_all() passes project_key to repo.list_all()."""
        svc, mock_repo, _ = _make_service()
        mock_repo.list_all.return_value = [SAMPLE_LEARNING]

        await svc.list_all(project_key="brain-v42")

        call_kwargs = mock_repo.list_all.call_args.kwargs
        assert call_kwargs.get("project_key") == "brain-v42"

    async def test_list_all_with_confidence_filter(self) -> None:
        """list_all() passes confidence to repo.list_all()."""
        svc, mock_repo, _ = _make_service()
        mock_repo.list_all.return_value = []

        await svc.list_all(confidence="high")

        call_kwargs = mock_repo.list_all.call_args.kwargs
        assert call_kwargs.get("confidence") == "high"

    async def test_list_all_with_tags_filter(self) -> None:
        """list_all() passes tags to repo.list_all()."""
        svc, mock_repo, _ = _make_service()
        mock_repo.list_all.return_value = []

        await svc.list_all(tags=["db", "async"])

        call_kwargs = mock_repo.list_all.call_args.kwargs
        assert call_kwargs.get("tags") == ["db", "async"]


# ---------------------------------------------------------------------------
# update()
# ---------------------------------------------------------------------------


class TestLearningServiceUpdate:
    async def test_update_without_embedding_service(self) -> None:
        """update() works without embedding_svc — no embedding regeneration."""
        svc, mock_repo, _ = _make_service(with_embedding_svc=False)
        mock_repo.update.return_value = SAMPLE_LEARNING

        data = LearningUpdate(topic="New topic")
        result = await svc.update(FIXED_UUID, data)

        # Should not call get_by_id since no embedding_svc
        mock_repo.update.assert_awaited_once_with(FIXED_UUID, data, embedding=None)
        assert result is SAMPLE_LEARNING

    async def test_update_with_embedding_service_regenerates_embedding(self) -> None:
        """update() regenerates embedding when topic or insight changes and svc is available."""
        svc, mock_repo, mock_embedding_svc = _make_service(with_embedding_svc=True)
        mock_repo.get_by_id.return_value = SAMPLE_LEARNING
        mock_repo.update.return_value = SAMPLE_LEARNING

        data = LearningUpdate(topic="Updated topic")
        await svc.update(FIXED_UUID, data)

        assert mock_embedding_svc is not None
        mock_embedding_svc.embed.assert_awaited_once()
        call_text = mock_embedding_svc.embed.call_args[0][0]
        assert "Updated topic" in call_text

        mock_repo.update.assert_awaited_once_with(FIXED_UUID, data, embedding=FAKE_EMBEDDING)

    async def test_a_status_only_update_never_touches_the_gpu(self) -> None:
        """Désarchiver un learning doit réussir GPU éteint (ticket 5ab70135).

        Mesuré le 2026-08-23 : un update ne portant QUE `freshness_status`
        déclenchait 1 appel d'embedding sur `learning` et 0 sur les quatre
        autres types — désarchiver un learning ÉCHOUAIT quand le service
        d'embedding était à terre, une décision réussissait, et la docstring
        affirmait le contraire. L'embedding_svc de ce test LÈVE : si le
        chemin le touche, le test meurt comme la prod mourait.
        """
        svc, mock_repo, mock_embedding_svc = _make_service(with_embedding_svc=True)
        assert mock_embedding_svc is not None
        mock_embedding_svc.embed.side_effect = RuntimeError("GPU à terre — EmbeddingUnavailable")
        mock_repo.update.return_value = SAMPLE_LEARNING

        data = LearningUpdate(freshness_status="fresh")
        result = await svc.update(FIXED_UUID, data)

        assert result is SAMPLE_LEARNING
        mock_embedding_svc.embed.assert_not_awaited()
        mock_repo.update.assert_awaited_once_with(FIXED_UUID, data, embedding=None)

    async def test_update_delegates_to_repo(self) -> None:
        """update() always calls repo.update() with the learning_id and data."""
        svc, mock_repo, _ = _make_service()
        mock_repo.update.return_value = SAMPLE_LEARNING

        data = LearningUpdate(confidence="low")
        await svc.update(FIXED_UUID, data)

        mock_repo.update.assert_awaited_once()


# ---------------------------------------------------------------------------
# delete()
# ---------------------------------------------------------------------------


class TestLearningServiceDelete:
    async def test_delete_delegates_to_repo(self) -> None:
        """delete() delegates to repo.delete() with the learning_id."""
        svc, mock_repo, _ = _make_service()
        mock_repo.delete.return_value = True

        result = await svc.delete(FIXED_UUID)

        mock_repo.delete.assert_awaited_once_with(FIXED_UUID)
        assert result is True

    async def test_delete_returns_true_when_found(self) -> None:
        """delete() returns True when the learning exists and is deleted."""
        svc, mock_repo, _ = _make_service()
        mock_repo.delete.return_value = True

        result = await svc.delete(FIXED_UUID)

        assert result is True

    async def test_delete_returns_false_when_not_found(self) -> None:
        """delete() returns False when the learning does not exist."""
        svc, mock_repo, _ = _make_service()
        mock_repo.delete.return_value = False

        result = await svc.delete(uuid.uuid4())

        assert result is False


# ---------------------------------------------------------------------------
# Graph write-through
# ---------------------------------------------------------------------------


class TestLearningServiceGraphWriteThrough:
    async def test_create_calls_graph_upsert(self) -> None:
        """create() calls graph.upsert_node and link_to_project after PG write."""
        mock_repo = MagicMock(spec=PgLearningRepo)
        mock_repo.create = AsyncMock(return_value=SAMPLE_LEARNING)
        mock_repo.set_embedding_if_current = AsyncMock(
            return_value=SAMPLE_LEARNING.model_copy(
                update={"embedding": FAKE_EMBEDDING}
            ).model_dump()
        )
        mock_embedding_svc = MagicMock()
        mock_embedding_svc.embed = AsyncMock(return_value=FAKE_EMBEDDING)
        mock_embedding_svc.embed_query = AsyncMock(return_value=FAKE_EMBEDDING)
        mock_graph = MagicMock()
        mock_graph.upsert_node = AsyncMock()
        mock_graph.link_to_project = AsyncMock()

        svc = LearningService(pg_repo=mock_repo, embedding_svc=mock_embedding_svc, graph=mock_graph)
        data = LearningCreate(
            topic="Async patterns",
            insight="Always await",
            project_key="brain-v42",
        )
        await svc.create(data)

        mock_graph.upsert_node.assert_awaited_once_with(
            "Learning",
            SAMPLE_LEARNING.id,
            {"project_key": "brain-v42", "topic": "Async patterns"},
        )
        mock_graph.link_to_project.assert_awaited_once_with(SAMPLE_LEARNING.id, "brain-v42")

    async def test_create_graph_failure_does_not_raise(self) -> None:
        """create() succeeds even if graph.upsert_node raises an exception."""
        mock_repo = MagicMock(spec=PgLearningRepo)
        mock_repo.create = AsyncMock(return_value=SAMPLE_LEARNING)
        mock_repo.set_embedding_if_current = AsyncMock(
            return_value=SAMPLE_LEARNING.model_copy(
                update={"embedding": FAKE_EMBEDDING}
            ).model_dump()
        )
        mock_embedding_svc = MagicMock()
        mock_embedding_svc.embed = AsyncMock(return_value=FAKE_EMBEDDING)
        mock_embedding_svc.embed_query = AsyncMock(return_value=FAKE_EMBEDDING)
        mock_graph = MagicMock()
        mock_graph.upsert_node = AsyncMock(side_effect=RuntimeError("neo4j down"))

        svc = LearningService(pg_repo=mock_repo, embedding_svc=mock_embedding_svc, graph=mock_graph)
        data = LearningCreate(topic="test", insight="test insight", project_key="brain-v42")

        # Should NOT raise
        result = await svc.create(data)
        assert result.id == SAMPLE_LEARNING.id

    async def test_create_without_graph_works(self) -> None:
        """create() works normally when graph=None."""
        svc, mock_repo, _ = _make_service(with_embedding_svc=True)
        data = LearningCreate(topic="no graph", insight="works fine")
        result = await svc.create(data)
        assert result.id == SAMPLE_LEARNING.id
        assert result.embedding == FAKE_EMBEDDING

    async def test_delete_removes_graph_node(self) -> None:
        """delete() calls graph.delete_node after PG delete."""
        mock_repo = MagicMock(spec=PgLearningRepo)
        mock_repo.delete = AsyncMock(return_value=True)
        mock_graph = MagicMock()
        mock_graph.delete_node = AsyncMock()

        svc = LearningService(pg_repo=mock_repo, graph=mock_graph)
        result = await svc.delete(FIXED_UUID)

        mock_graph.delete_node.assert_awaited_once_with("Learning", FIXED_UUID)
        assert result is True


# ---------------------------------------------------------------------------
# get_by_id()
# ---------------------------------------------------------------------------


class TestLearningServiceGetById:
    async def test_get_by_id_delegates_to_repo(self) -> None:
        """get_by_id() delegates to repo.get_by_id() and returns the result."""
        svc, mock_repo, _ = _make_service()
        mock_repo.get_by_id.return_value = SAMPLE_LEARNING

        result = await svc.get_by_id(FIXED_UUID)

        mock_repo.get_by_id.assert_awaited_once_with(FIXED_UUID)
        assert result is SAMPLE_LEARNING

    async def test_get_by_id_returns_none_when_not_found(self) -> None:
        """get_by_id() returns None when repo returns None."""
        svc, mock_repo, _ = _make_service()
        mock_repo.get_by_id.return_value = None

        result = await svc.get_by_id(uuid.uuid4())

        assert result is None
