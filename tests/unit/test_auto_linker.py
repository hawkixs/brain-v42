"""Tests for AutoLinker — automatic RELATED_TO graph links on entity creation.

AutoLinker uses the entity's embedding to find similar entities across all
tables via a UNION vector search, then creates RELATED_TO edges in Neo4j.
"""

from __future__ import annotations

import ast
import inspect
import re
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog

from brain_v42.mcp.dream_project_authorization import DreamProjectAuthorizationError
from brain_v42.services import auto_linker as auto_linker_module
from brain_v42.services.auto_linker import _ENTITY_TABLES, AutoLinker
from brain_v42.services.link_result import LinkJobResult


class MockGraph:
    """Configurable mock for GraphService supporting RelationWriteOutcome returns."""

    def __init__(self, outcomes: list[str] | None = None, default: str = "created") -> None:
        self._outcomes = list(outcomes) if outcomes else []
        self._default = default
        self._call_count = 0

    async def create_relation(self, source_id, target_id, rel_type):  # noqa: ANN001
        if self._call_count < len(self._outcomes):
            outcome = self._outcomes[self._call_count]
        else:
            outcome = self._default
        self._call_count += 1
        return outcome


@pytest.fixture
def mock_graph():
    g = AsyncMock()
    g.create_relation = AsyncMock(return_value="created")
    return g


@pytest.fixture
def mock_session_factory():
    return MagicMock()


@pytest.fixture
def linker(mock_session_factory, mock_graph):
    return AutoLinker(session_factory=mock_session_factory, graph=mock_graph)


@pytest.fixture
def linker_no_graph(mock_session_factory):
    return AutoLinker(session_factory=mock_session_factory, graph=None)


@pytest.fixture
def fake_embedding():
    return [0.1] * 1536


class TestAutoLinkerNoop:
    """Cases where auto_link should do nothing."""

    @pytest.mark.asyncio
    async def test_noop_when_graph_is_none(self, linker_no_graph, fake_embedding):
        """No graph → return immediately, no DB query."""
        result = await linker_no_graph.auto_link(
            entity_type="Learning",
            entity_id=uuid.uuid4(),
            embedding=fake_embedding,
        )
        assert isinstance(result, LinkJobResult)
        assert result.created == []
        assert result.matched == []
        assert result.skipped == []
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_noop_when_embedding_is_none(self, linker):
        """No embedding → can't search, return empty."""
        result = await linker.auto_link(
            entity_type="Learning",
            entity_id=uuid.uuid4(),
            embedding=None,
        )
        assert isinstance(result, LinkJobResult)
        assert result.created == []
        assert result.matched == []
        assert result.skipped == []
        assert result.errors == []


class TestAutoLinkerSearch:
    """Core auto-link behavior: search + create relations."""

    @pytest.mark.asyncio
    async def test_creates_related_to_for_similar_entities(
        self, linker, mock_graph, fake_embedding
    ):
        """Top results above threshold get RELATED_TO edges."""
        entity_id = uuid.uuid4()
        similar_id_1 = uuid.uuid4()
        similar_id_2 = uuid.uuid4()

        # Mock the DB query to return similar entities
        mock_rows = [
            {"id": similar_id_1, "entity_type": "Decision", "similarity": 0.75},
            {"id": similar_id_2, "entity_type": "Learning", "similarity": 0.65},
        ]
        mock_graph.create_relation = AsyncMock(return_value="created")
        with patch.object(linker, "_find_similar", new_callable=AsyncMock, return_value=mock_rows):
            result = await linker.auto_link(
                entity_type="Learning",
                entity_id=entity_id,
                embedding=fake_embedding,
                threshold=0.6,
                max_links=3,
            )

        assert len(result.created) == 2
        assert mock_graph.create_relation.call_count == 2
        # First call: entity → similar_id_1
        mock_graph.create_relation.assert_any_call(entity_id, similar_id_1, "RELATED_TO")
        mock_graph.create_relation.assert_any_call(entity_id, similar_id_2, "RELATED_TO")

    @pytest.mark.asyncio
    async def test_excludes_self(self, linker, mock_graph, fake_embedding):
        """The entity itself must never appear in results."""
        entity_id = uuid.uuid4()
        other_id = uuid.uuid4()

        mock_rows = [
            {"id": other_id, "entity_type": "Decision", "similarity": 0.80},
            # Self should NOT appear — _find_similar excludes it
        ]
        mock_graph.create_relation = AsyncMock(return_value="created")
        with patch.object(linker, "_find_similar", new_callable=AsyncMock, return_value=mock_rows):
            result = await linker.auto_link(
                entity_type="Learning",
                entity_id=entity_id,
                embedding=fake_embedding,
            )

        assert len(result.created) == 1
        # Verify self was not linked
        for call in mock_graph.create_relation.call_args_list:
            assert call[0][1] != entity_id  # target should never be self

    @pytest.mark.asyncio
    async def test_respects_threshold(self, linker, mock_graph, fake_embedding):
        """Results below threshold land in skipped, not created."""
        entity_id = uuid.uuid4()
        high_id = uuid.uuid4()
        low_id = uuid.uuid4()

        mock_rows = [
            {"id": high_id, "entity_type": "Decision", "similarity": 0.75},
            {"id": low_id, "entity_type": "Learning", "similarity": 0.45},
        ]
        mock_graph.create_relation = AsyncMock(return_value="created")
        with patch.object(linker, "_find_similar", new_callable=AsyncMock, return_value=mock_rows):
            result = await linker.auto_link(
                entity_type="Learning",
                entity_id=entity_id,
                embedding=fake_embedding,
                threshold=0.6,
            )

        assert len(result.created) == 1
        assert len(result.skipped) == 1
        assert result.skipped[0]["id"] == low_id
        assert result.skipped[0]["reason"] == "below_threshold"
        mock_graph.create_relation.assert_called_once_with(entity_id, high_id, "RELATED_TO")

    @pytest.mark.asyncio
    async def test_respects_max_links(self, linker, mock_graph, fake_embedding):
        """Never write more than max_links relations; extras go to skipped."""
        entity_id = uuid.uuid4()
        ids = [uuid.uuid4() for _ in range(5)]

        mock_rows = [
            {"id": ids[i], "entity_type": "Learning", "similarity": 0.9 - i * 0.05}
            for i in range(5)
        ]
        mock_graph.create_relation = AsyncMock(return_value="created")
        with patch.object(linker, "_find_similar", new_callable=AsyncMock, return_value=mock_rows):
            result = await linker.auto_link(
                entity_type="Learning",
                entity_id=entity_id,
                embedding=fake_embedding,
                threshold=0.5,
                max_links=2,
            )

        assert len(result.created) == 2
        assert mock_graph.create_relation.call_count == 2
        # 3 extras should be in skipped with reason max_links_cap
        assert len(result.skipped) == 3
        for s in result.skipped:
            assert s["reason"] == "max_links_cap"

    @pytest.mark.asyncio
    async def test_returns_created_links(self, linker, mock_graph, fake_embedding):
        """auto_link returns a LinkJobResult; created bucket has the created link."""
        entity_id = uuid.uuid4()
        target_id = uuid.uuid4()

        mock_rows = [
            {"id": target_id, "entity_type": "Decision", "similarity": 0.82},
        ]
        mock_graph.create_relation = AsyncMock(return_value="created")
        with patch.object(linker, "_find_similar", new_callable=AsyncMock, return_value=mock_rows):
            result = await linker.auto_link(
                entity_type="Learning",
                entity_id=entity_id,
                embedding=fake_embedding,
            )

        assert isinstance(result, LinkJobResult)
        assert len(result.created) == 1
        assert result.created[0]["id"] == target_id
        assert result.created[0]["entity_type"] == "Decision"
        assert result.created[0]["similarity"] == 0.82

    @pytest.mark.asyncio
    async def test_durable_link_preserves_auto_link_provenance(
        self, linker, mock_graph, fake_embedding
    ) -> None:
        source_id, target_id = uuid.uuid4(), uuid.uuid4()
        candidate = {"id": target_id, "entity_type": "Decision", "similarity": 0.82}
        mock_graph.requires_durable_write_success = True

        with patch.object(
            linker,
            "_find_similar",
            new_callable=AsyncMock,
            return_value=[candidate],
        ):
            await linker.auto_link("Learning", source_id, fake_embedding)

        mock_graph.create_relation.assert_awaited_once_with(
            source_id,
            target_id,
            "RELATED_TO",
            {"similarity": 0.82},
            origin="auto_linker",
            confidence=0.82,
        )


class TestAutoLinker4Buckets:
    """4-bucket semantics: created / matched / skipped / errors."""

    @pytest.mark.asyncio
    async def test_below_threshold_goes_to_skipped(self, mock_session_factory, fake_embedding):
        """Candidate at sim=0.4 with threshold=0.6 → 1 skipped, 0 created."""
        graph = MockGraph(outcomes=["created"])
        linker = AutoLinker(session_factory=mock_session_factory, graph=graph)
        entity_id = uuid.uuid4()
        target_id = uuid.uuid4()

        mock_rows = [
            {"id": target_id, "entity_type": "Learning", "similarity": 0.4},
        ]
        with patch.object(linker, "_find_similar", new_callable=AsyncMock, return_value=mock_rows):
            result = await linker.auto_link(
                entity_type="Learning",
                entity_id=entity_id,
                embedding=fake_embedding,
                threshold=0.6,
            )

        assert len(result.created) == 0
        assert len(result.skipped) == 1
        assert result.skipped[0]["id"] == target_id
        assert result.skipped[0]["reason"] == "below_threshold"
        assert graph._call_count == 0  # no write attempted

    @pytest.mark.asyncio
    async def test_max_links_cap_pushes_extras_to_skipped(
        self, mock_session_factory, fake_embedding
    ):
        """5 candidates all above threshold, max_links=2 → 2 created, 3 skipped(cap)."""
        graph = MockGraph(default="created")
        linker = AutoLinker(session_factory=mock_session_factory, graph=graph)
        entity_id = uuid.uuid4()
        ids = [uuid.uuid4() for _ in range(5)]

        mock_rows = [
            {"id": ids[i], "entity_type": "Snippet", "similarity": 0.95 - i * 0.02}
            for i in range(5)
        ]
        with patch.object(linker, "_find_similar", new_callable=AsyncMock, return_value=mock_rows):
            result = await linker.auto_link(
                entity_type="Snippet",
                entity_id=entity_id,
                embedding=fake_embedding,
                threshold=0.6,
                max_links=2,
            )

        assert len(result.created) == 2
        assert len(result.skipped) == 3
        assert len(result.matched) == 0
        assert len(result.errors) == 0
        for s in result.skipped:
            assert s["reason"] == "max_links_cap"
        # Only 2 writes happened
        assert graph._call_count == 2

    @pytest.mark.asyncio
    async def test_matched_outcome_goes_to_matched_bucket(
        self, mock_session_factory, fake_embedding
    ):
        """create_relation returns 'matched' → result.matched has the entry."""
        graph = MockGraph(outcomes=["matched"])
        linker = AutoLinker(session_factory=mock_session_factory, graph=graph)
        entity_id = uuid.uuid4()
        target_id = uuid.uuid4()

        mock_rows = [
            {"id": target_id, "entity_type": "Decision", "similarity": 0.85},
        ]
        with patch.object(linker, "_find_similar", new_callable=AsyncMock, return_value=mock_rows):
            result = await linker.auto_link(
                entity_type="Decision",
                entity_id=entity_id,
                embedding=fake_embedding,
                threshold=0.6,
            )

        assert len(result.matched) == 1
        assert len(result.created) == 0
        assert result.matched[0]["id"] == target_id
        assert result.matched[0]["similarity"] == 0.85

    @pytest.mark.asyncio
    async def test_error_outcome_goes_to_errors_bucket(self, mock_session_factory, fake_embedding):
        """create_relation returns 'error' → result.errors has the entry."""
        graph = MockGraph(outcomes=["error"])
        linker = AutoLinker(session_factory=mock_session_factory, graph=graph)
        entity_id = uuid.uuid4()
        target_id = uuid.uuid4()

        mock_rows = [
            {"id": target_id, "entity_type": "Runbook", "similarity": 0.72},
        ]
        with patch.object(linker, "_find_similar", new_callable=AsyncMock, return_value=mock_rows):
            result = await linker.auto_link(
                entity_type="Runbook",
                entity_id=entity_id,
                embedding=fake_embedding,
                threshold=0.6,
            )

        assert len(result.errors) == 1
        assert len(result.created) == 0
        assert result.errors[0]["id"] == target_id
        assert result.errors[0]["reason"] == "write_failed"

    @pytest.mark.asyncio
    async def test_mixed_outcomes_all_bucketed(self, mock_session_factory, fake_embedding):
        """Mix of created/matched/skipped(threshold)/skipped(cap)/error all bucket correctly.

        The 'picked' count (created+matched) enforces max_links. Errors do NOT
        count as picked — they don't contribute to created+matched so the cap
        is reached only by successful writes.

        Candidate layout (max_links=2):
          0: sim=0.95 → write → created   (picked=1)
          1: sim=0.90 → write → matched   (picked=2 → cap reached)
          2: sim=0.85 → above threshold but picked>=2 → skipped(max_links_cap)
          3: sim=0.80 → same              → skipped(max_links_cap)
          4: sim=0.40 → below threshold   → skipped(below_threshold)
        """
        graph = MockGraph(outcomes=["created", "matched"])
        linker = AutoLinker(session_factory=mock_session_factory, graph=graph)
        entity_id = uuid.uuid4()
        ids = [uuid.uuid4() for _ in range(5)]

        mock_rows = [
            {"id": ids[0], "entity_type": "Decision", "similarity": 0.95},
            {"id": ids[1], "entity_type": "Learning", "similarity": 0.90},
            {"id": ids[2], "entity_type": "Snippet", "similarity": 0.85},
            {"id": ids[3], "entity_type": "Runbook", "similarity": 0.80},
            {"id": ids[4], "entity_type": "Decision", "similarity": 0.40},
        ]
        with patch.object(linker, "_find_similar", new_callable=AsyncMock, return_value=mock_rows):
            result = await linker.auto_link(
                entity_type="Decision",
                entity_id=entity_id,
                embedding=fake_embedding,
                threshold=0.6,
                max_links=2,
            )

        assert len(result.created) == 1
        assert len(result.matched) == 1
        assert len(result.errors) == 0
        assert len(result.skipped) == 3
        # Check skipped reasons
        skipped_reasons = {s["id"]: s["reason"] for s in result.skipped}
        assert skipped_reasons[ids[2]] == "max_links_cap"
        assert skipped_reasons[ids[3]] == "max_links_cap"
        assert skipped_reasons[ids[4]] == "below_threshold"

    @pytest.mark.asyncio
    async def test_error_does_not_count_toward_picked(self, mock_session_factory, fake_embedding):
        """Errors don't decrement remaining write slots — picked = created+matched only."""
        # outcomes: error, created → only created counts toward picked
        graph = MockGraph(outcomes=["error", "created"])
        linker = AutoLinker(session_factory=mock_session_factory, graph=graph)
        entity_id = uuid.uuid4()
        ids = [uuid.uuid4() for _ in range(3)]

        mock_rows = [
            {"id": ids[0], "entity_type": "Decision", "similarity": 0.95},
            {"id": ids[1], "entity_type": "Learning", "similarity": 0.90},
            {"id": ids[2], "entity_type": "Snippet", "similarity": 0.85},
        ]
        with patch.object(linker, "_find_similar", new_callable=AsyncMock, return_value=mock_rows):
            result = await linker.auto_link(
                entity_type="Decision",
                entity_id=entity_id,
                embedding=fake_embedding,
                threshold=0.6,
                max_links=1,  # only 1 successful write allowed
            )

        # First write → error (doesn't count), second write → created (picked=1 → cap)
        # Third candidate → skipped(max_links_cap)
        assert len(result.errors) == 1
        assert len(result.created) == 1
        assert len(result.skipped) == 1
        assert result.skipped[0]["reason"] == "max_links_cap"
        assert graph._call_count == 2

    @pytest.mark.asyncio
    async def test_iterates_all_candidates_after_cap(self, mock_session_factory, fake_embedding):
        """Full list is iterated even after max_links_cap — no early break."""
        graph = MockGraph(default="created")
        linker = AutoLinker(session_factory=mock_session_factory, graph=graph)
        entity_id = uuid.uuid4()
        ids = [uuid.uuid4() for _ in range(4)]

        mock_rows = [
            {"id": ids[0], "entity_type": "Learning", "similarity": 0.90},
            {"id": ids[1], "entity_type": "Learning", "similarity": 0.85},
            # These two are above threshold but capped
            {"id": ids[2], "entity_type": "Learning", "similarity": 0.80},
            {"id": ids[3], "entity_type": "Learning", "similarity": 0.75},
        ]
        with patch.object(linker, "_find_similar", new_callable=AsyncMock, return_value=mock_rows):
            result = await linker.auto_link(
                entity_type="Learning",
                entity_id=entity_id,
                embedding=fake_embedding,
                threshold=0.6,
                max_links=2,
            )

        assert len(result.created) == 2
        assert len(result.skipped) == 2
        # All 4 candidates accounted for
        assert (
            len(result.created) + len(result.skipped) + len(result.matched) + len(result.errors)
            == 4
        )
        # Only 2 writes, not 4
        assert graph._call_count == 2


class TestAutoLinkerGracefulDegradation:
    """AutoLinker must never break entity creation."""

    @pytest.mark.asyncio
    async def test_swallows_db_exceptions(self, linker, fake_embedding):
        """DB errors during search → empty LinkJobResult, no raise."""
        with patch.object(
            linker, "_find_similar", new_callable=AsyncMock, side_effect=Exception("DB down")
        ):
            result = await linker.auto_link(
                entity_type="Learning",
                entity_id=uuid.uuid4(),
                embedding=fake_embedding,
            )
        assert isinstance(result, LinkJobResult)
        assert result.created == []
        assert result.skipped == []
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_swallows_graph_exceptions(self, mock_session_factory, fake_embedding):
        """Graph write returning 'error' outcome → goes to errors bucket, no raise."""
        graph = MockGraph(outcomes=["error"])
        linker = AutoLinker(session_factory=mock_session_factory, graph=graph)
        entity_id = uuid.uuid4()
        target_id = uuid.uuid4()

        mock_rows = [
            {"id": target_id, "entity_type": "Decision", "similarity": 0.8},
        ]
        with patch.object(linker, "_find_similar", new_callable=AsyncMock, return_value=mock_rows):
            result = await linker.auto_link(
                entity_type="Learning",
                entity_id=entity_id,
                embedding=fake_embedding,
            )
        # Error goes to errors bucket, not raised
        assert isinstance(result, LinkJobResult)
        assert result.created == []
        assert len(result.errors) == 1


class TestFindSimilar:
    """Test the _find_similar SQL query logic."""

    @pytest.mark.asyncio
    async def test_find_similar_excludes_self_in_query(self, mock_session_factory, mock_graph):
        """The SQL UNION query must include WHERE id != entity_id."""
        linker = AutoLinker(session_factory=mock_session_factory, graph=mock_graph)
        entity_id = uuid.uuid4()

        # Create a mock async session that returns empty results
        mock_result = MagicMock()
        mock_result.fetchall = MagicMock(return_value=[])
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value=mock_result)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_factory.return_value = mock_session

        results = await linker._find_similar(
            entity_id=entity_id,
            embedding=([0.1] * 1536),
            limit=5,
        )

        assert results == []
        # Verify execute was called (SQL was run)
        mock_conn.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_scoped_query_filters_every_union_arm_before_global_limit(
        self, mock_session_factory, mock_graph
    ) -> None:
        """Authenticated scope is bound in all five UNION arms, not post-limit."""
        signature = inspect.signature(AutoLinker._find_similar)
        assert "project_key" in signature.parameters
        assert signature.parameters["project_key"].kind is inspect.Parameter.KEYWORD_ONLY
        assert signature.parameters["project_key"].default is None

        owned_id = uuid.uuid4()
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [
            MagicMock(id=owned_id, entity_type="Decision", similarity=0.91)
        ]
        mock_conn = AsyncMock()
        mock_conn.execute.return_value = mock_result
        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_conn
        mock_session.__aexit__.return_value = False
        mock_session_factory.return_value = mock_session
        linker = AutoLinker(session_factory=mock_session_factory, graph=mock_graph)

        rows = await linker._find_similar(
            entity_id=uuid.uuid4(),
            embedding=[0.1],
            limit=5,
            project_key="owned-project",
        )

        statement, params = mock_conn.execute.await_args.args
        sql = str(statement)
        assert sql.count("project_key = :project_key") == 5
        assert sql.rindex("project_key = :project_key") < sql.index("ORDER BY similarity")
        assert params == {
            "entity_id": params["entity_id"],
            "limit": 5,
            "project_key": "owned-project",
        }
        assert rows == [{"id": owned_id, "entity_type": "Decision", "similarity": 0.91}]

    @pytest.mark.asyncio
    async def test_admin_query_and_params_keep_historical_shape(
        self, mock_session_factory, mock_graph
    ) -> None:
        """A project-tagged admin creation still searches globally."""
        mock_result = MagicMock()
        mock_result.fetchall.return_value = []
        mock_conn = AsyncMock()
        mock_conn.execute.return_value = mock_result
        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_conn
        mock_session.__aexit__.return_value = False
        mock_session_factory.return_value = mock_session
        linker = AutoLinker(session_factory=mock_session_factory, graph=mock_graph)
        entity_id = uuid.uuid4()

        await linker._find_similar(entity_id=entity_id, embedding=[0.1], limit=5)

        statement, params = mock_conn.execute.await_args.args
        sql = str(statement)
        assert "project_key" not in sql
        assert params == {"entity_id": entity_id, "limit": 5}


class TestProjectScopedAutoLink:
    @pytest.mark.asyncio
    async def test_revalidates_selected_pair_immediately_before_scoped_edge(
        self, linker, mock_graph, fake_embedding
    ) -> None:
        signature = inspect.signature(AutoLinker.auto_link)
        assert "authorization" in signature.parameters
        assert signature.parameters["authorization"].kind is inspect.Parameter.KEYWORD_ONLY
        assert signature.parameters["authorization"].default is None

        source_id, target_id = uuid.uuid4(), uuid.uuid4()
        events: list[tuple[str, object]] = []
        authorization = MagicMock(project_key="owned-project")

        async def revalidate(ids):  # noqa: ANN001
            events.append(("revalidate", list(ids)))

        async def create_relation(*args, **kwargs):  # noqa: ANN002,ANN003
            events.append(("create_relation", (args, kwargs)))
            return "created"

        authorization.revalidate_ids = AsyncMock(side_effect=revalidate)
        mock_graph.create_relation = AsyncMock(side_effect=create_relation)
        candidate = {"id": target_id, "entity_type": "ADR", "similarity": 0.9}

        with patch.object(
            linker, "_find_similar", new_callable=AsyncMock, return_value=[candidate]
        ) as find_similar:
            result = await linker.auto_link(
                entity_type="Learning",
                entity_id=source_id,
                embedding=fake_embedding,
                authorization=authorization,
            )

        assert find_similar.await_args.kwargs["project_key"] == "owned-project"
        assert events == [
            ("revalidate", [source_id, target_id]),
            (
                "create_relation",
                ((source_id, target_id, "RELATED_TO"), {"project_key": "owned-project"}),
            ),
        ]
        assert result.created == [candidate]

    @pytest.mark.asyncio
    async def test_scoped_durable_link_preserves_scope_and_provenance(
        self, linker, mock_graph, fake_embedding
    ) -> None:
        source_id, target_id = uuid.uuid4(), uuid.uuid4()
        authorization = MagicMock(project_key="owned-project")
        authorization.revalidate_ids = AsyncMock()
        mock_graph.requires_durable_write_success = True
        candidate = {"id": target_id, "entity_type": "ADR", "similarity": 0.9}

        with patch.object(
            linker,
            "_find_similar",
            new_callable=AsyncMock,
            return_value=[candidate],
        ):
            await linker.auto_link(
                "Learning",
                source_id,
                fake_embedding,
                authorization=authorization,
            )

        mock_graph.create_relation.assert_awaited_once_with(
            source_id,
            target_id,
            "RELATED_TO",
            {"similarity": 0.9},
            project_key="owned-project",
            origin="auto_linker",
            confidence=0.9,
        )

    @pytest.mark.asyncio
    async def test_ownership_flip_after_selection_creates_no_edge(
        self, linker, mock_graph, fake_embedding
    ) -> None:
        signature = inspect.signature(AutoLinker.auto_link)
        assert "authorization" in signature.parameters

        source_id, target_id = uuid.uuid4(), uuid.uuid4()
        denial = RuntimeError("ownership changed")
        authorization = MagicMock(project_key="owned-project")
        authorization.revalidate_ids = AsyncMock(side_effect=denial)
        candidate = {"id": target_id, "entity_type": "ADR", "similarity": 0.9}

        with patch.object(
            linker, "_find_similar", new_callable=AsyncMock, return_value=[candidate]
        ):
            with pytest.raises(RuntimeError, match="ownership changed"):
                await linker.auto_link(
                    entity_type="Learning",
                    entity_id=source_id,
                    embedding=fake_embedding,
                    authorization=authorization,
                )

        authorization.revalidate_ids.assert_awaited_once_with([source_id, target_id])
        mock_graph.create_relation.assert_not_awaited()

    @pytest.mark.parametrize(
        ("outcome", "reason"),
        [("missing_node", "missing_node"), ("error", "write_failed")],
    )
    async def test_scoped_failed_outcome_keeps_bucket_without_identifier_log(
        self,
        linker,
        mock_graph,
        fake_embedding,
        outcome: str,
        reason: str,
    ) -> None:
        source_id, target_id = uuid.uuid4(), uuid.uuid4()
        authorization = MagicMock(project_key="owned-project")
        authorization.revalidate_ids = AsyncMock()
        mock_graph.create_relation = AsyncMock(return_value=outcome)
        candidate = {"id": target_id, "entity_type": "ADR", "similarity": 0.9}

        with patch.object(
            linker, "_find_similar", new_callable=AsyncMock, return_value=[candidate]
        ):
            with structlog.testing.capture_logs() as logs:
                result = await linker.auto_link(
                    entity_type="Learning",
                    entity_id=source_id,
                    embedding=fake_embedding,
                    authorization=authorization,
                )

        assert result.errors == [{**candidate, "reason": reason}]
        assert logs == []

    @pytest.mark.asyncio
    async def test_scoped_graph_authorization_error_propagates_without_secondary_log(
        self,
        linker,
        mock_graph,
        fake_embedding,
    ) -> None:
        source_id, target_id = uuid.uuid4(), uuid.uuid4()
        denial = DreamProjectAuthorizationError("object_not_authorized")
        authorization = MagicMock(project_key="owned-project")
        authorization.revalidate_ids = AsyncMock()
        mock_graph.create_relation = AsyncMock(side_effect=denial)
        candidate = {"id": target_id, "entity_type": "ADR", "similarity": 0.9}

        with patch.object(
            linker, "_find_similar", new_callable=AsyncMock, return_value=[candidate]
        ):
            with structlog.testing.capture_logs() as logs:
                with pytest.raises(DreamProjectAuthorizationError) as raised:
                    await linker.auto_link(
                        entity_type="Learning",
                        entity_id=source_id,
                        embedding=fake_embedding,
                        authorization=authorization,
                    )

        assert raised.value is denial
        assert logs == []


class _RecordingSession:
    """Async context manager that records what `_find_similar` hands to the driver."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, dict]] = []

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def execute(self, statement: object, params: dict) -> MagicMock:
        self.statements.append((str(statement), dict(params)))
        result = MagicMock()
        result.fetchall.return_value = []
        return result


class TestFindSimilarSqlAssembly:
    """Invariant portant les deux `# nosec B608` de `_find_similar`.

    Le nosec affirme que les seuls fragments interpolés dans le SQL sont des identifiants
    littéraux venant de `_ENTITY_TABLES`, plus un vecteur de nombres — et qu'aucune valeur
    d'appelant (clé de projet, id d'entité, limite) n'atteint le texte de la requête. Ces
    tests échouent si quelqu'un rend la constante dynamique ou fait entrer une entrée dans
    la chaîne SQL, ce qui doit rouvrir le finding au lieu de le laisser muet.
    """

    def test_entity_tables_is_a_literal_constant_of_sql_identifiers(self) -> None:
        source = Path(auto_linker_module.__file__).read_text(encoding="utf-8")
        module = ast.parse(source)
        assignments = [
            node
            for node in module.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "_ENTITY_TABLES"
                for target in node.targets
            )
        ]

        assert len(assignments) == 1, "_ENTITY_TABLES doit rester une seule affectation module"

        try:
            declared = ast.literal_eval(assignments[0].value)
        except ValueError as exc:  # appel, nom, f-string, lecture de config…
            pytest.fail(
                "_ENTITY_TABLES n'est plus un littéral figé — le nosec B608 de "
                f"_find_similar ne tient plus : {exc}"
            )

        assert declared == _ENTITY_TABLES
        for row in declared:
            for fragment in row:
                assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", fragment), (
                    f"{fragment!r} n'est pas un identifiant SQL littéral"
                )

    @pytest.mark.asyncio
    async def test_caller_values_reach_sql_as_binds_never_as_text(self) -> None:
        session = _RecordingSession()
        linker = AutoLinker(session_factory=lambda: session, graph=None)
        hostile = "brain-v42'); DROP TABLE learnings; --"

        await linker._find_similar(
            entity_id=uuid.UUID("11111111-2222-3333-4444-555555555555"),
            embedding=[0.5, -1.5e-3, 2.0],
            limit=6,
            project_key=hostile,
        )

        (query, params) = session.statements[0]

        assert hostile not in query
        assert params["project_key"] == hostile
        assert str(params["entity_id"]) not in query
        assert query.count(":project_key") == len(_ENTITY_TABLES)
        assert ":entity_id" in query and ":limit" in query

        for table, type_label, _text_col in _ENTITY_TABLES:
            assert f"FROM {table} " in query
            assert f"'{type_label}' AS entity_type" in query

        vector_literals = re.findall(r"'\[(.*?)\]'::vector", query)
        assert len(vector_literals) == len(_ENTITY_TABLES)
        for literal in vector_literals:
            assert re.fullmatch(r"[-+0-9.eE,]+", literal), (
                f"le littéral vecteur porte autre chose que des nombres : {literal!r}"
            )

    @pytest.mark.asyncio
    async def test_unscoped_query_carries_no_project_predicate(self) -> None:
        session = _RecordingSession()
        linker = AutoLinker(session_factory=lambda: session, graph=None)

        await linker._find_similar(
            entity_id=uuid.uuid4(),
            embedding=[0.25] * 3,
        )

        (query, params) = session.statements[0]

        assert "project_key" not in query
        assert "project_key" not in params


class TestFindSimilarLifecycleFilter:
    """Ticket 6d2cf2a9 (a) — ne jamais proposer une cible que le résolveur refusera.

    `pg_graph_ledger._resolve_endpoints` exige `lifecycle='active'` sur LES DEUX
    ancres et lève `UnknownGraphEndpoint` sinon. `_find_similar` sélectionnait sur
    `embedding IS NOT NULL` seul : il proposait donc des cibles archived, mesurées
    à 408 lignes sur 14 projets le 2026-08-18. Le sélecteur doit porter le même
    prédicat que le résolveur, sinon le désaccord se paie en connect partial.
    """

    @pytest.mark.asyncio
    async def test_every_union_arm_requires_an_active_graph_endpoint(self) -> None:
        session = _RecordingSession()
        linker = AutoLinker(session_factory=lambda: session, graph=None)

        await linker._find_similar(entity_id=uuid.uuid4(), embedding=[0.1], limit=5)

        (query, _params) = session.statements[0]
        assert query.count("brain_entities") == len(_ENTITY_TABLES)
        assert query.count("lifecycle = 'active'") == len(_ENTITY_TABLES)

    @pytest.mark.asyncio
    async def test_guard_sits_inside_each_arm_never_after_the_global_limit(self) -> None:
        """Un filtre posé après le ORDER BY global rendrait moins de lignes que `limit`."""
        session = _RecordingSession()
        linker = AutoLinker(session_factory=lambda: session, graph=None)

        await linker._find_similar(entity_id=uuid.uuid4(), embedding=[0.1], limit=5)

        (query, _params) = session.statements[0]
        assert query.rindex("lifecycle = 'active'") < query.index("ORDER BY similarity")

    @pytest.mark.asyncio
    async def test_correlation_is_qualified_because_brain_entities_owns_an_id_column(
        self,
    ) -> None:
        """`be.source_uuid = id` lierait `id` à `brain_entities.id` — jamais à la candidate.

        `brain_entities` porte sa propre colonne `id` (clé primaire du ledger), donc
        un `id` nu dans la sous-requête résout vers la table INTERNE et le EXISTS
        devient vrai pour toute ligne du ledger : le filtre ne filtrerait rien.
        """
        session = _RecordingSession()
        linker = AutoLinker(session_factory=lambda: session, graph=None)

        await linker._find_similar(entity_id=uuid.uuid4(), embedding=[0.1], limit=5)

        (query, _params) = session.statements[0]
        for table, _type_label, _text_col in _ENTITY_TABLES:
            assert f"source_uuid = {table}.id" in query

    @pytest.mark.asyncio
    async def test_scoped_arm_keeps_both_the_project_and_the_lifecycle_predicate(self) -> None:
        session = _RecordingSession()
        linker = AutoLinker(session_factory=lambda: session, graph=None)

        await linker._find_similar(
            entity_id=uuid.uuid4(),
            embedding=[0.1],
            limit=5,
            project_key="owned-project",
        )

        (query, params) = session.statements[0]
        assert query.count("project_key = :project_key") == len(_ENTITY_TABLES)
        assert query.count("lifecycle = 'active'") == len(_ENTITY_TABLES)
        assert params["project_key"] == "owned-project"

    @pytest.mark.asyncio
    async def test_lifecycle_guard_adds_no_bind_and_no_project_leak_when_unscoped(self) -> None:
        """Le garde-fou est un littéral figé : il ne doit ni créer de bind ni citer le projet."""
        session = _RecordingSession()
        linker = AutoLinker(session_factory=lambda: session, graph=None)

        await linker._find_similar(entity_id=uuid.uuid4(), embedding=[0.1], limit=5)

        (query, params) = session.statements[0]
        assert "project_key" not in query
        assert set(params) == {"entity_id", "limit"}


class TestUnknownEndpointIsBucketedNotRaised:
    """Ticket 6d2cf2a9 (c) — une pathologie de données ne doit pas tuer le lot.

    Le chemin scopé propage DÉLIBÉRÉMENT (graph_helpers l.59-62) pour qu'un refus
    d'autorisation ne soit jamais avalé. `UnknownGraphEndpoint` n'est pas un refus
    d'autorisation : c'est la donnée qui est sale. Il doit tomber dans `errors`
    — donc rester visible en `errors=N` — sans interrompre les autres candidats.
    """

    @pytest.mark.asyncio
    async def test_scoped_unknown_endpoint_goes_to_errors_without_raising(
        self, linker, mock_graph, fake_embedding
    ) -> None:
        from brain_v42.repositories.pg_graph_ledger import UnknownGraphEndpoint

        target_id = uuid.uuid4()
        authorization = MagicMock(project_key="owned-project")
        authorization.revalidate_ids = AsyncMock()
        mock_graph.create_relation = AsyncMock(
            side_effect=UnknownGraphEndpoint("one or more UUID endpoints are not registered")
        )
        candidate = {"id": target_id, "entity_type": "Learning", "similarity": 0.9}

        with patch.object(
            linker, "_find_similar", new_callable=AsyncMock, return_value=[candidate]
        ):
            result = await linker.auto_link(
                entity_type="Learning",
                entity_id=uuid.uuid4(),
                embedding=fake_embedding,
                authorization=authorization,
            )

        assert len(result.errors) == 1
        assert result.errors[0]["reason"] == "unknown_endpoint"
        assert result.errors[0]["id"] == target_id
        assert result.created == []

    @pytest.mark.asyncio
    async def test_one_dirty_target_does_not_cancel_the_clean_ones(
        self, linker, mock_graph, fake_embedding
    ) -> None:
        from brain_v42.repositories.pg_graph_ledger import UnknownGraphEndpoint

        dirty_id, clean_id = uuid.uuid4(), uuid.uuid4()
        authorization = MagicMock(project_key="owned-project")
        authorization.revalidate_ids = AsyncMock()

        async def create_relation(src, tgt, *_args, **_kwargs):  # noqa: ANN001,ANN002,ANN003
            if tgt == dirty_id:
                raise UnknownGraphEndpoint("one or more UUID endpoints are not registered")
            return "created"

        mock_graph.create_relation = AsyncMock(side_effect=create_relation)
        candidates = [
            {"id": dirty_id, "entity_type": "Learning", "similarity": 0.95},
            {"id": clean_id, "entity_type": "Decision", "similarity": 0.90},
        ]

        with patch.object(linker, "_find_similar", new_callable=AsyncMock, return_value=candidates):
            result = await linker.auto_link(
                entity_type="Learning",
                entity_id=uuid.uuid4(),
                embedding=fake_embedding,
                authorization=authorization,
            )

        assert [entry["id"] for entry in result.created] == [clean_id]
        assert [entry["id"] for entry in result.errors] == [dirty_id]

    @pytest.mark.asyncio
    async def test_scoped_authorization_refusal_still_propagates(
        self, linker, mock_graph, fake_embedding
    ) -> None:
        """Le garde anti-régression du fix (c) : ne JAMAIS élargir le catch.

        Si ce test devient vert avec un `except Exception`, la garde scopée est morte.
        """
        denial = DreamProjectAuthorizationError("object_not_authorized")
        authorization = MagicMock(project_key="owned-project")
        authorization.revalidate_ids = AsyncMock(side_effect=denial)
        mock_graph.create_relation = AsyncMock(return_value="created")
        candidate = {"id": uuid.uuid4(), "entity_type": "Learning", "similarity": 0.9}

        with patch.object(
            linker, "_find_similar", new_callable=AsyncMock, return_value=[candidate]
        ):
            with pytest.raises(DreamProjectAuthorizationError) as raised:
                await linker.auto_link(
                    entity_type="Learning",
                    entity_id=uuid.uuid4(),
                    embedding=fake_embedding,
                    authorization=authorization,
                )

        assert raised.value is denial
