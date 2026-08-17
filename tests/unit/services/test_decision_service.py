"""Unit tests for DecisionService.

All tests use unittest.mock (AsyncMock/MagicMock) — no real DB or ONNX model.

Test cases:
1. test_create_generates_embedding_and_calls_repo
2. test_create_returns_decision
3. test_search_fts_delegates_to_repo
4. test_semantic_search_embeds_query_and_calls_repo
5. test_list_all_delegates_to_repo
6. test_update_regenerates_embedding_when_text_fields_changed
7. test_update_skips_embedding_when_no_text_fields
8. test_supersede_generates_embedding_and_calls_repo
9. test_get_supersession_chain_delegates
Graph write-through tests:
10. test_create_calls_graph_upsert
11. test_create_with_related_to
12. test_create_graph_failure_does_not_raise
13. test_create_without_graph
14. test_delete_removes_graph_node
15. test_supersede_creates_supersedes_relation
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.models.decision import Decision, DecisionCreate, DecisionUpdate
from brain_v42.models.project_context import ProjectContext
from brain_v42.repositories.pg_project_context import PgProjectContextRepo
from brain_v42.services.decision_service import DecisionService
from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable
from brain_v42.services.ticket_service import UnknownProjectError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXED_UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")
OLD_UUID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
NOW = datetime.now(UTC)
FAKE_EMBEDDING = [0.1] * 1536


def _make_decision(
    id: uuid.UUID | None = None,
    title: str = "Use PostgreSQL",
    description: str = "We chose PG for persistence",
    reasoning: str = "Maturity and pgvector support",
    project_key: str | None = "brain-v42",
) -> Decision:
    return Decision(
        id=id or FIXED_UUID,
        title=title,
        description=description,
        reasoning=reasoning,
        project_key=project_key,
        created_at=NOW,
        updated_at=NOW,
    )


def _make_service() -> tuple[DecisionService, MagicMock, MagicMock]:
    """Return (service, repo_mock, embedding_svc_mock)."""
    repo = MagicMock()
    repo.set_embedding_if_current = AsyncMock(
        return_value=_make_decision().model_copy(update={"embedding": FAKE_EMBEDDING}).model_dump()
    )
    repo.get_by_id = AsyncMock(return_value=_make_decision())
    embedding_svc = MagicMock()
    embedding_svc.embed = AsyncMock(return_value=FAKE_EMBEDDING)
    svc = DecisionService(repo=repo, embedding_svc=embedding_svc)
    return svc, repo, embedding_svc


# ---------------------------------------------------------------------------
# Tests: create
# ---------------------------------------------------------------------------


class TestCreate:
    async def test_create_in_caller_transaction_defers_derived_work(self) -> None:
        """A caller-owned transaction controls durability and leaves enrichment queued."""
        svc, repo, embedding_svc = _make_service()
        decision = _make_decision()
        repo.create = AsyncMock(return_value=decision)
        session = MagicMock(spec=AsyncSession)
        data = DecisionCreate(
            title="Atomic proposal",
            description="Entity and proposal share one commit",
            reasoning="Retry must not create duplicates",
            project_key="brain-v42",
        )

        result = await svc.create(data, session=session)

        repo.create.assert_awaited_once_with(
            data,
            embedding=None,
            session=session,
        )
        embedding_svc.embed.assert_not_awaited()
        repo.set_embedding_if_current.assert_not_awaited()
        assert result is decision

    async def test_create_generates_embedding_and_calls_repo(self) -> None:
        """create() stores null first, then enriches the unchanged row."""
        svc, repo, embedding_svc = _make_service()
        decision = _make_decision()
        repo.create = AsyncMock(return_value=decision)

        data = DecisionCreate(
            title="Use PostgreSQL",
            description="We chose PG for persistence",
            reasoning="Maturity and pgvector support",
            project_key="brain-v42",
        )
        await svc.create(data)

        embedding_svc.embed.assert_awaited_once()
        call_text = embedding_svc.embed.call_args[0][0]
        assert "Use PostgreSQL" in call_text
        assert "We chose PG for persistence" in call_text
        assert "Maturity and pgvector support" in call_text

        repo.create.assert_awaited_once_with(data, embedding=None)
        repo.set_embedding_if_current.assert_awaited_once_with(
            decision.id,
            FAKE_EMBEDDING,
            expected_updated_at=decision.updated_at,
        )

    async def test_create_returns_durable_decision_when_embedding_unavailable(self) -> None:
        """An embedding outage never turns a committed Decision into an error."""
        svc, repo, embedding_svc = _make_service()
        decision = _make_decision()
        repo.create = AsyncMock(return_value=decision)
        embedding_svc.embed = AsyncMock(
            side_effect=EmbeddingUnavailable("offline", kind="unreachable")
        )

        data = DecisionCreate(
            title="Use PostgreSQL",
            description="We chose PG for persistence",
            reasoning="Maturity and pgvector support",
            project_key="brain-v42",
        )
        result = await svc.create(data)

        assert result is decision
        repo.create.assert_awaited_once_with(data, embedding=None)
        repo.set_embedding_if_current.assert_not_awaited()

    async def test_create_commits_before_attempting_embedding(self) -> None:
        events: list[str] = []
        svc, repo, embedding_svc = _make_service()
        decision = _make_decision()

        async def create_pending(data, *, embedding):
            events.append("create")
            return decision

        async def fail_embedding(text):
            events.append("embed")
            raise EmbeddingUnavailable("offline", kind="unreachable")

        repo.create = AsyncMock(side_effect=create_pending)
        embedding_svc.embed = AsyncMock(side_effect=fail_embedding)
        data = DecisionCreate(
            title="Use PostgreSQL",
            description="We chose PG for persistence",
            reasoning="Maturity and pgvector support",
            project_key="brain-v42",
        )

        result = await svc.create(data)

        assert result is decision
        assert events == ["create", "embed"]


# ---------------------------------------------------------------------------
# Tests: create() — project existence guard (fail-closed)
# ---------------------------------------------------------------------------


def _make_service_with_ctx_repo(
    ctx_get_by_key_return: ProjectContext | None,
) -> tuple[DecisionService, MagicMock, MagicMock]:
    """Return (service, repo_mock, project_context_repo_mock) with the guard wired."""
    decision = _make_decision()
    repo = MagicMock()
    repo.create = AsyncMock(return_value=decision)
    embedding_svc = MagicMock()
    embedding_svc.embed = AsyncMock(return_value=FAKE_EMBEDDING)
    mock_ctx_repo = MagicMock(spec=PgProjectContextRepo)
    mock_ctx_repo.get_by_key = AsyncMock(return_value=ctx_get_by_key_return)
    svc = DecisionService(
        repo=repo, embedding_svc=embedding_svc, project_context_repo=mock_ctx_repo
    )
    return svc, repo, mock_ctx_repo


class TestDecisionServiceProjectGuard:
    async def test_create_with_none_project_key_raises_unknown_project(self) -> None:
        svc, repo, mock_ctx_repo = _make_service_with_ctx_repo(ctx_get_by_key_return=None)
        data = DecisionCreate(
            title="orphan",
            description="no project_key supplied",
            reasoning="none",
        )

        with pytest.raises(UnknownProjectError):
            await svc.create(data)

        repo.create.assert_not_awaited()
        mock_ctx_repo.get_by_key.assert_not_awaited()

    async def test_create_with_unknown_project_key_raises_and_names_the_key(self) -> None:
        svc, repo, mock_ctx_repo = _make_service_with_ctx_repo(ctx_get_by_key_return=None)
        data = DecisionCreate(
            title="orphan",
            description="unknown project",
            reasoning="none",
            project_key="red-backup",
        )

        with pytest.raises(UnknownProjectError, match="red-backup"):
            await svc.create(data)

        repo.create.assert_not_awaited()
        mock_ctx_repo.get_by_key.assert_awaited_once_with("red-backup", session=None)

    async def test_create_with_known_project_key_succeeds(self) -> None:
        existing = ProjectContext(
            project_key="brain-v42", name="brain-v42", description="knowledge base"
        )
        svc, repo, mock_ctx_repo = _make_service_with_ctx_repo(ctx_get_by_key_return=existing)
        data = DecisionCreate(
            title="known project",
            description="passes the guard",
            reasoning="valid key",
            project_key="brain-v42",
        )

        result = await svc.create(data)

        mock_ctx_repo.get_by_key.assert_awaited_once_with("brain-v42", session=None)
        repo.create.assert_awaited_once()
        assert result is not None

    async def test_create_canonicalizes_alias_before_checking_the_guard(self) -> None:
        """project_key='brain_v42' canonicalizes to 'brain-v42' before the guard runs."""
        svc, _, mock_ctx_repo = _make_service_with_ctx_repo(ctx_get_by_key_return=None)
        data = DecisionCreate(
            title="alias",
            description="brain_v42 alias",
            reasoning="none",
            project_key="brain_v42",
        )

        with pytest.raises(UnknownProjectError):
            await svc.create(data)

        mock_ctx_repo.get_by_key.assert_awaited_once_with("brain-v42", session=None)

    async def test_create_without_project_context_repo_skips_the_guard(self) -> None:
        """Backward compatibility: project_context_repo is optional at construction."""
        svc, repo, _ = _make_service()
        decision = _make_decision()
        repo.create = AsyncMock(return_value=decision)
        data = DecisionCreate(
            title="no guard wired",
            description="unscoped, still succeeds",
            reasoning="none",
        )

        result = await svc.create(data)

        repo.create.assert_awaited_once()
        assert result.id == decision.id

    async def test_create_in_caller_transaction_reuses_the_caller_session_for_the_guard(
        self,
    ) -> None:
        """The proposal-service atomic apply path passes its own session — no 2nd connection."""
        existing = ProjectContext(
            project_key="brain-v42", name="brain-v42", description="knowledge base"
        )
        svc, repo, mock_ctx_repo = _make_service_with_ctx_repo(ctx_get_by_key_return=existing)
        caller_session = MagicMock(spec=AsyncSession)
        data = DecisionCreate(
            title="dream apply",
            description="ticket-extraction promotion",
            reasoning="pre-validated by TicketService",
            project_key="brain-v42",
        )

        await svc.create(data, session=caller_session)

        mock_ctx_repo.get_by_key.assert_awaited_once_with("brain-v42", session=caller_session)
        repo.create.assert_awaited_once_with(data, embedding=None, session=caller_session)


# ---------------------------------------------------------------------------
# Tests: search (FTS)
# ---------------------------------------------------------------------------


class TestSearch:
    async def test_search_fts_delegates_to_repo(self) -> None:
        """search() calls repo.search_fts() and returns list[Decision] (stripped scores)."""
        svc, repo, _embedding_svc = _make_service()
        decision = _make_decision()
        repo.search_fts = AsyncMock(return_value=[(decision, 0.85)])

        results = await svc.search("postgresql", project_key="brain-v42", limit=5)

        repo.search_fts.assert_awaited_once_with(
            "postgresql",
            project_key="brain-v42",
            project_keys=None,
            status=None,
            tags=None,
            limit=5,
            offset=0,
        )
        assert results == [decision]
        # Scores must be stripped
        assert all(isinstance(r, Decision) for r in results)


# ---------------------------------------------------------------------------
# Tests: semantic_search
# ---------------------------------------------------------------------------


class TestSemanticSearch:
    async def test_semantic_search_embeds_query_and_calls_repo(self) -> None:
        """semantic_search() embeds the query then calls repo.search_vector(),
        returning list[tuple[Decision, float]]."""
        svc, repo, embedding_svc = _make_service()
        decision = _make_decision()
        repo.search_vector = AsyncMock(return_value=[(decision, 0.92)])

        results = await svc.semantic_search(
            "best database choice", project_key="brain-v42", limit=10
        )

        embedding_svc.embed.assert_awaited_once_with("best database choice")
        repo.search_vector.assert_awaited_once_with(
            FAKE_EMBEDDING, project_key="brain-v42", project_keys=None, limit=10
        )
        assert results == [(decision, 0.92)]


# ---------------------------------------------------------------------------
# Tests: list_all
# ---------------------------------------------------------------------------


class TestListAll:
    async def test_list_all_delegates_to_repo(self) -> None:
        """list_all() delegates to repo.list_all() with all filters."""
        svc, repo, _embedding_svc = _make_service()
        decision = _make_decision()
        repo.list_all = AsyncMock(return_value=[decision])

        results = await svc.list_all(
            project_key="brain-v42", status="active", tags=["arch"], limit=10, offset=5
        )

        repo.list_all.assert_awaited_once_with(
            project_key="brain-v42",
            status="active",
            tags=["arch"],
            limit=10,
            offset=5,
            include_archived=False,
        )
        assert results == [decision]


# ---------------------------------------------------------------------------
# Tests: update
# ---------------------------------------------------------------------------


class TestUpdate:
    async def test_update_regenerates_embedding_when_text_fields_changed(self) -> None:
        """update() calls embedding_svc.embed() when title/description/reasoning changes."""
        svc, repo, embedding_svc = _make_service()
        current = _make_decision()
        updated = _make_decision(title="Updated title")
        repo.get_by_id = AsyncMock(return_value=current)
        repo.update = AsyncMock(return_value=updated)

        data = DecisionUpdate(title="Updated title")
        await svc.update(FIXED_UUID, data)

        embedding_svc.embed.assert_awaited_once()
        repo.update.assert_awaited_once_with(FIXED_UUID, data, embedding=FAKE_EMBEDDING)

    async def test_update_skips_embedding_when_no_text_fields(self) -> None:
        """update() does NOT call embedding_svc.embed() when only status/tags change."""
        svc, repo, embedding_svc = _make_service()
        updated = _make_decision()
        repo.update = AsyncMock(return_value=updated)

        data = DecisionUpdate(status="deprecated")
        await svc.update(FIXED_UUID, data)

        embedding_svc.embed.assert_not_awaited()
        repo.update.assert_awaited_once_with(FIXED_UUID, data, embedding=None)


# ---------------------------------------------------------------------------
# Tests: supersede
# ---------------------------------------------------------------------------


class TestSupersede:
    async def test_supersede_generates_embedding_and_calls_repo(self) -> None:
        """supersede() embeds the new decision text and calls repo.supersede()."""
        svc, repo, embedding_svc = _make_service()
        new_decision = _make_decision(id=uuid.uuid4())
        repo.supersede = AsyncMock(return_value=new_decision)

        new_data = DecisionCreate(
            title="Use pgvector",
            description="Better semantic search",
            reasoning="Native postgres extension",
            project_key="brain-v42",
        )
        result = await svc.supersede(OLD_UUID, new_data)

        embedding_svc.embed.assert_awaited_once()
        call_text = embedding_svc.embed.call_args[0][0]
        assert "Use pgvector" in call_text
        assert "Better semantic search" in call_text
        assert "Native postgres extension" in call_text

        repo.supersede.assert_awaited_once_with(OLD_UUID, new_data, new_embedding=FAKE_EMBEDDING)
        assert result is new_decision


# ---------------------------------------------------------------------------
# Tests: get_supersession_chain
# ---------------------------------------------------------------------------


class TestGetSupersessionChain:
    async def test_get_supersession_chain_delegates(self) -> None:
        """get_supersession_chain() delegates to repo.get_supersession_chain()."""
        svc, repo, _embedding_svc = _make_service()
        chain = [_make_decision(), _make_decision(id=uuid.uuid4())]
        repo.get_supersession_chain = AsyncMock(return_value=chain)

        result = await svc.get_supersession_chain(FIXED_UUID)

        repo.get_supersession_chain.assert_awaited_once_with(FIXED_UUID)
        assert result is chain

    @pytest.mark.asyncio
    async def test_chain_delegates_to_graph_when_available(self) -> None:
        """get_supersession_chain() uses graph when available and returns full Decision objects."""
        decision_a = _make_decision(id=FIXED_UUID)
        decision_b = _make_decision(id=OLD_UUID)

        repo = MagicMock()
        embedding_svc = MagicMock()
        embedding_svc.embed = AsyncMock(return_value=FAKE_EMBEDDING)
        graph = MagicMock()

        # Graph returns chain_ids as strings
        graph.get_supersession_chain = AsyncMock(return_value=[str(FIXED_UUID), str(OLD_UUID)])

        # repo.get_by_id resolves each id to a Decision
        async def _get_by_id(id: uuid.UUID):  # noqa: A002
            if id == FIXED_UUID:
                return decision_a
            if id == OLD_UUID:
                return decision_b
            return None

        repo.get_by_id = AsyncMock(side_effect=_get_by_id)

        svc = DecisionService(repo=repo, embedding_svc=embedding_svc, graph=graph)
        result = await svc.get_supersession_chain(FIXED_UUID)

        graph.get_supersession_chain.assert_awaited_once_with(FIXED_UUID)
        assert result == [decision_a, decision_b]
        # repo.get_supersession_chain should NOT have been called (graph took priority)
        assert not hasattr(repo, "get_supersession_chain") or not repo.get_supersession_chain.called

    @pytest.mark.asyncio
    async def test_chain_falls_back_to_pg_when_no_graph(self) -> None:
        """get_supersession_chain() falls back to PG CTE when graph=None."""
        svc, repo, _embedding_svc = _make_service()
        chain = [_make_decision()]
        repo.get_supersession_chain = AsyncMock(return_value=chain)

        result = await svc.get_supersession_chain(FIXED_UUID)

        repo.get_supersession_chain.assert_awaited_once_with(FIXED_UUID)
        assert result is chain

    @pytest.mark.asyncio
    async def test_chain_falls_back_to_pg_when_graph_returns_empty(self) -> None:
        """get_supersession_chain() falls back to PG CTE when graph returns empty list."""
        repo = MagicMock()
        embedding_svc = MagicMock()
        embedding_svc.embed = AsyncMock(return_value=FAKE_EMBEDDING)
        graph = MagicMock()
        graph.get_supersession_chain = AsyncMock(return_value=[])

        pg_chain = [_make_decision()]
        repo.get_supersession_chain = AsyncMock(return_value=pg_chain)

        svc = DecisionService(repo=repo, embedding_svc=embedding_svc, graph=graph)
        result = await svc.get_supersession_chain(FIXED_UUID)

        # Should fall back to PG when graph returns empty
        repo.get_supersession_chain.assert_awaited_once_with(FIXED_UUID)
        assert result is pg_chain


# ---------------------------------------------------------------------------
# Tests: graph write-through
# ---------------------------------------------------------------------------


def _make_service_with_graph() -> tuple[DecisionService, MagicMock, MagicMock, MagicMock]:
    """Return (service, repo_mock, embedding_svc_mock, graph_mock)."""
    repo = MagicMock()
    repo.set_embedding_if_current = AsyncMock(
        return_value=_make_decision().model_copy(update={"embedding": FAKE_EMBEDDING}).model_dump()
    )
    repo.get_by_id = AsyncMock(return_value=_make_decision())
    embedding_svc = MagicMock()
    embedding_svc.embed = AsyncMock(return_value=FAKE_EMBEDDING)
    graph = MagicMock()
    graph.upsert_node = AsyncMock()
    graph.link_to_project = AsyncMock()
    graph.create_relation = AsyncMock()
    graph.delete_node = AsyncMock()
    svc = DecisionService(repo=repo, embedding_svc=embedding_svc, graph=graph)
    return svc, repo, embedding_svc, graph


class TestDecisionServiceGraphWriteThrough:
    @pytest.mark.asyncio
    async def test_create_calls_graph_upsert(self) -> None:
        """Creating a decision upserts a node and links to project in Neo4j."""
        svc, repo, _embedding_svc, graph = _make_service_with_graph()
        decision = _make_decision()
        repo.create = AsyncMock(return_value=decision)

        data = DecisionCreate(
            title="Use PostgreSQL",
            description="We chose PG for persistence",
            reasoning="Maturity and pgvector support",
            project_key="brain-v42",
        )
        await svc.create(data)

        graph.upsert_node.assert_awaited_once_with(
            "Decision",
            decision.id,
            {"project_key": data.project_key, "title": data.title},
        )
        graph.link_to_project.assert_awaited_once_with(decision.id, data.project_key)

    @pytest.mark.asyncio
    async def test_create_with_related_to(self) -> None:
        """related_to creates graph relations."""
        svc, repo, _embedding_svc, graph = _make_service_with_graph()
        decision = _make_decision()
        repo.create = AsyncMock(return_value=decision)

        rel_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        related_to = [{"id": rel_id, "type": "MOTIVATED_BY"}]

        data = DecisionCreate(
            title="Use PostgreSQL",
            description="We chose PG for persistence",
            reasoning="Maturity and pgvector support",
            project_key="brain-v42",
        )
        await svc.create(data, related_to=related_to)

        graph.create_relation.assert_awaited_once_with(
            decision.id, uuid.UUID(rel_id), "MOTIVATED_BY"
        )

    @pytest.mark.asyncio
    async def test_create_graph_failure_does_not_raise(self) -> None:
        """Graph failure is logged, not raised — PG write still succeeds."""
        svc, repo, _embedding_svc, graph = _make_service_with_graph()
        decision = _make_decision()
        repo.create = AsyncMock(return_value=decision)
        graph.upsert_node.side_effect = Exception("neo4j down")

        data = DecisionCreate(
            title="Use PostgreSQL",
            description="We chose PG for persistence",
            reasoning="Maturity and pgvector support",
            project_key="brain-v42",
        )
        result = await svc.create(data)

        assert result is not None
        assert result.id == decision.id

    @pytest.mark.asyncio
    async def test_create_without_graph(self) -> None:
        """Works normally when graph=None."""
        svc, repo, _embedding_svc = _make_service()
        decision = _make_decision()
        repo.create = AsyncMock(return_value=decision)

        data = DecisionCreate(
            title="Use PostgreSQL",
            description="We chose PG for persistence",
            reasoning="Maturity and pgvector support",
            project_key="brain-v42",
        )
        result = await svc.create(data)

        assert result is not None
        assert result.id == decision.id
        assert result.embedding == FAKE_EMBEDDING

    @pytest.mark.asyncio
    async def test_delete_removes_graph_node(self) -> None:
        """delete() calls graph.delete_node after PG delete."""
        svc, repo, _embedding_svc, graph = _make_service_with_graph()
        repo.delete = AsyncMock(return_value=True)

        result = await svc.delete(FIXED_UUID)

        assert result is True
        graph.delete_node.assert_awaited_once_with("Decision", FIXED_UUID)

    @pytest.mark.asyncio
    async def test_supersede_creates_supersedes_relation(self) -> None:
        """supersede() upserts new node and creates SUPERSEDES relation in graph."""
        svc, repo, _embedding_svc, graph = _make_service_with_graph()
        new_id = uuid.uuid4()
        new_decision = _make_decision(id=new_id)
        repo.supersede = AsyncMock(return_value=new_decision)

        new_data = DecisionCreate(
            title="Use pgvector",
            description="Better semantic search",
            reasoning="Native postgres extension",
            project_key="brain-v42",
        )
        await svc.supersede(OLD_UUID, new_data)

        graph.upsert_node.assert_awaited_once_with(
            "Decision",
            new_id,
            {"project_key": new_data.project_key, "title": new_data.title},
        )
        graph.create_relation.assert_awaited_once_with(new_id, OLD_UUID, "SUPERSEDES")


# ---------------------------------------------------------------------------
# Tests: chain missing decision warning
# ---------------------------------------------------------------------------


class TestChainMissingDecisionWarning:
    @pytest.mark.asyncio
    async def test_chain_logs_warning_for_missing_decision(self) -> None:
        """When graph returns a chain ID that resolves to None in PG,
        a warning must be logged with 'decision.chain_member_missing'."""
        decision_a = _make_decision(id=FIXED_UUID)
        missing_id = uuid.uuid4()

        repo = MagicMock()
        embedding_svc = MagicMock()
        embedding_svc.embed = AsyncMock(return_value=FAKE_EMBEDDING)
        graph = MagicMock()

        # Graph returns two IDs, but repo only has the first
        graph.get_supersession_chain = AsyncMock(return_value=[str(FIXED_UUID), str(missing_id)])

        async def _get_by_id(uid: uuid.UUID):
            if uid == FIXED_UUID:
                return decision_a
            return None

        repo.get_by_id = AsyncMock(side_effect=_get_by_id)

        svc = DecisionService(repo=repo, embedding_svc=embedding_svc, graph=graph)

        with patch("brain_v42.services.decision_service.logger") as mock_logger:
            result = await svc.get_supersession_chain(FIXED_UUID)

        assert result == [decision_a]
        mock_logger.warning.assert_called_once_with(
            "decision.chain_member_missing", decision_id=str(missing_id)
        )
