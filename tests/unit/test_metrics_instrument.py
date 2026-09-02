"""Tests for metrics instrumentation — tool decorator and embedding wrapper."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.mcp.dream_project_authorization import DreamProjectAuthorizationError
from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.instrument import (
    InstrumentedEmbeddingService,
    InstrumentedGraphService,
    InstrumentedReranker,
    instrument_tool,
)


class TestInstrumentTool:
    @pytest.fixture
    def collector(self) -> MetricsCollector:
        return MetricsCollector(engine=MagicMock(), session_factory=MagicMock())

    async def test_decorator_records_successful_call(self, collector: MetricsCollector) -> None:
        @instrument_tool(collector, "brain_test")
        async def brain_test(x: int) -> dict:
            return {"result": x * 2}

        result = await brain_test(5)
        assert result == {"result": 10}
        # Outside HTTP context get_http_headers() returns None → agent="unknown"
        assert collector._tool_stats["unknown"]["brain_test"]["calls"] == 1
        assert collector._tool_stats["unknown"]["brain_test"]["errors"] == 0

    async def test_decorator_records_error(self, collector: MetricsCollector) -> None:
        @instrument_tool(collector, "brain_fail")
        async def brain_fail() -> dict:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await brain_fail()

        assert collector._tool_stats["unknown"]["brain_fail"]["calls"] == 1
        assert collector._tool_stats["unknown"]["brain_fail"]["errors"] == 1

    async def test_decorator_records_latency(self, collector: MetricsCollector) -> None:
        @instrument_tool(collector, "brain_slow")
        async def brain_slow() -> dict:
            await asyncio.sleep(0.05)
            return {"ok": True}

        await brain_slow()
        stats = collector._tool_stats["unknown"]["brain_slow"]
        assert stats["total_latency"] >= 40.0  # at least 40ms

    async def test_decorator_logs_only_tool_and_exception_type(
        self, collector: MetricsCollector, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Errors remain diagnosable without logging caller-controlled content."""
        caplog.set_level(logging.ERROR)
        secret_marker = "Bearer SYNTHETIC_SECRET_TOKEN"

        @instrument_tool(collector, "brain_create_runbook")
        async def brain_create_runbook() -> dict:
            raise ValueError(secret_marker)

        with pytest.raises(ValueError):
            await brain_create_runbook()

        matches = [r for r in caplog.records if "brain_create_runbook" in r.getMessage()]
        assert matches, "expected an error log tagged with the tool name"
        assert any("exception_type=ValueError" in r.getMessage() for r in matches)
        assert secret_marker not in caplog.text
        assert "Traceback" not in caplog.text
        assert all(record.exc_info is None for record in matches)

    async def test_decorator_does_not_log_authorization_failure_context(
        self, collector: MetricsCollector, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Authorization failures count as errors without logging their causal context."""
        caplog.set_level(logging.ERROR, logger="brain_v42.metrics.instrument")
        secret_marker = "SYNTHETIC_RESOLVER_SECRET"
        resolver_error = RuntimeError(secret_marker)
        denial = DreamProjectAuthorizationError("resolver_failure")

        @instrument_tool(collector, "brain_backfill_links_batch")
        async def brain_backfill_links_batch() -> dict:
            try:
                raise resolver_error
            except RuntimeError:
                raise denial from resolver_error

        with pytest.raises(DreamProjectAuthorizationError) as raised:
            await brain_backfill_links_batch()

        assert raised.value is denial
        stats = collector._tool_stats["unknown"]["brain_backfill_links_batch"]
        assert stats["calls"] == 1
        assert stats["errors"] == 1

        authorization_records = [
            record for record in caplog.records if record.name == "brain_v42.metrics.instrument"
        ]
        assert authorization_records == [], caplog.text
        assert secret_marker not in caplog.text
        assert "mcp_tool_error" not in caplog.text
        assert "Traceback" not in caplog.text

    async def test_decorator_records_actor_set_by_provenance_middleware(
        self, collector: MetricsCollector
    ) -> None:
        """Provenance now flows through the ContextVar the ProvenanceMiddleware
        sets on_call_tool (before the wrapped tool function ever runs) —
        instrument_tool no longer reads X-Brain-Agent itself, it records
        whatever normalize_agent() already resolved to a basename.

        Path-to-basename normalization itself is covered where it now lives:
        tests/unit/test_provenance.py (the function) and
        tests/unit/mcp/test_provenance_middleware.py (the caller)."""
        from brain_v42.provenance import set_current_actor

        set_current_actor("red-lab")

        @instrument_tool(collector, "brain_search")
        async def brain_search() -> dict:
            return {}

        await brain_search()
        assert collector._tool_stats["red-lab"]["brain_search"]["calls"] == 1

    async def test_decorator_passes_through_static_label(self, collector: MetricsCollector) -> None:
        """Static service labels (red-shrik, gemini, brain-v42, codex) are not
        paths and must pass through unchanged."""
        from brain_v42.provenance import set_current_actor

        set_current_actor("red-shrik")

        @instrument_tool(collector, "brain_search")
        async def brain_search() -> dict:
            return {}

        await brain_search()
        assert collector._tool_stats["red-shrik"]["brain_search"]["calls"] == 1


class TestNormalizeAgent:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("/home/hawixs/hawkixs_infra/git_repo/red-lab", "red-lab"),
            ("/home/u/proj/", "proj"),  # trailing slash
            ("/srv/auto_discord", "auto_discord"),
            ("red-shrik", "red-shrik"),  # static label passthrough
            ("gemini-red-lab", "gemini-red-lab"),
            ("brain-v42", "brain-v42"),
            ("codex", "codex"),
            ("", "unknown"),
            ("   ", "unknown"),
            ("/", "unknown"),
            # Daemon sessions (systemd remote-control: no PWD in env) pass the
            # header template through UNEXPANDED — the literal must not become
            # a phantom agent label (observed: agent_name='${PWD}' in
            # process_metrics, 2026-07-03).
            ("${PWD}", "_unexpanded"),
            ("${workspaceFolder}", "_unexpanded"),
            ("/home/u/${PWD}", "_unexpanded"),  # embedded in a path
        ],
    )
    def test_normalize(self, raw: str, expected: str) -> None:
        from brain_v42.metrics.instrument import _normalize_agent

        assert _normalize_agent(raw) == expected


class TestInstrumentedEmbeddingService:
    @pytest.fixture
    def collector(self) -> MetricsCollector:
        return MetricsCollector(engine=MagicMock(), session_factory=MagicMock())

    async def test_embed_delegates_and_records(self, collector: MetricsCollector) -> None:
        inner = MagicMock()

        async def mock_embed(text: str) -> list[float]:
            return [0.1, 0.2]

        inner.embed = mock_embed
        wrapped = InstrumentedEmbeddingService(inner, collector)

        result = await wrapped.embed("hello")
        assert result == [0.1, 0.2]
        assert collector._embedding_stats["total_requests"] == 1
        assert collector._embedding_stats["total_errors"] == 0

    async def test_embed_records_error(self, collector: MetricsCollector) -> None:
        inner = MagicMock()

        async def fail_embed(text: str) -> list[float]:
            raise ConnectionError("GPU down")

        inner.embed = fail_embed
        wrapped = InstrumentedEmbeddingService(inner, collector)

        with pytest.raises(ConnectionError):
            await wrapped.embed("hello")
        assert collector._embedding_stats["total_errors"] == 1

    async def test_embed_records_split_kind_for_gpu_busy(self, collector: MetricsCollector) -> None:
        """An EmbeddingUnavailable raised from gpu_busy retries must bump the
        gpu_busy split counter, not the unreachable one."""
        from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable

        inner = MagicMock()

        async def fail_embed(text: str) -> list[float]:
            raise EmbeddingUnavailable("gpu_busy after 3 retries", kind="gpu_busy")

        inner.embed = fail_embed
        wrapped = InstrumentedEmbeddingService(inner, collector)

        with pytest.raises(EmbeddingUnavailable):
            await wrapped.embed("hello")
        assert collector._embedding_stats["gpu_busy_errors"] == 1
        assert collector._embedding_stats["unreachable_errors"] == 0

    async def test_embed_records_split_kind_for_unreachable(
        self, collector: MetricsCollector
    ) -> None:
        from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable

        inner = MagicMock()

        async def fail_embed(text: str) -> list[float]:
            raise EmbeddingUnavailable("service unreachable", kind="unreachable")

        inner.embed = fail_embed
        wrapped = InstrumentedEmbeddingService(inner, collector)

        with pytest.raises(EmbeddingUnavailable):
            await wrapped.embed("hello")
        assert collector._embedding_stats["gpu_busy_errors"] == 0
        assert collector._embedding_stats["unreachable_errors"] == 1

    async def test_healthcheck_passthrough(self, collector: MetricsCollector) -> None:
        inner = MagicMock()

        async def hc() -> bool:
            return True

        inner.healthcheck = hc
        wrapped = InstrumentedEmbeddingService(inner, collector)
        assert await wrapped.healthcheck() is True


class TestEmbedTextsEmptyMetrics:
    @pytest.mark.asyncio
    async def test_embed_texts_empty_records_metrics(self):
        """embed_texts([]) must still record a metrics entry."""
        inner = AsyncMock()
        inner.embed_texts = AsyncMock(return_value=[])
        collector = MagicMock(spec=MetricsCollector)

        svc = InstrumentedEmbeddingService(inner, collector)
        result = await svc.embed_texts([])

        assert result == []
        inner.embed_texts.assert_awaited_once_with([])
        collector.record_embedding_request.assert_called_once()
        assert collector.record_embedding_request.call_args[1]["error"] is False


class TestInstrumentedReranker:
    @pytest.fixture
    def collector(self) -> MetricsCollector:
        return MetricsCollector(engine=MagicMock(), session_factory=MagicMock())

    async def test_rerank_delegates_and_records(self, collector: MetricsCollector) -> None:
        inner = MagicMock()
        candidates = [MagicMock(text="doc1"), MagicMock(text="doc2")]

        async def mock_rerank_with_mode(query, cands):
            return ("reranked", list(reversed(cands)))

        inner.rerank_with_mode = mock_rerank_with_mode

        wrapped = InstrumentedReranker(inner, collector)
        mode, result = await wrapped.rerank_with_mode("test query", candidates)

        assert mode == "reranked"
        assert result == list(reversed(candidates))
        assert collector._reranker_stats["total_calls"] == 1
        assert collector._reranker_stats["total_errors"] == 0
        assert collector._reranker_stats["total_candidates"] == 2
        assert collector._reranker_stats["total_latency"] > 0

    async def test_rerank_records_error(self, collector: MetricsCollector) -> None:
        inner = MagicMock()

        async def fail_rerank_with_mode(query, cands):
            raise RuntimeError("model crash")

        inner.rerank_with_mode = fail_rerank_with_mode

        wrapped = InstrumentedReranker(inner, collector)
        with pytest.raises(RuntimeError, match="model crash"):
            await wrapped.rerank_with_mode("query", [MagicMock(text="doc")])

        assert collector._reranker_stats["total_calls"] == 1
        assert collector._reranker_stats["total_errors"] == 1
        assert collector._reranker_stats["total_candidates"] == 1

    async def test_rerank_rrf_fallback_counts_as_error(self, collector: MetricsCollector) -> None:
        """rrf_fallback = reranker service down — must increment total_errors."""
        inner = MagicMock()
        candidates = [MagicMock(text="doc")]

        async def fallback_rerank_with_mode(query, cands):
            return ("rrf_fallback", cands)

        inner.rerank_with_mode = fallback_rerank_with_mode

        wrapped = InstrumentedReranker(inner, collector)
        mode, result = await wrapped.rerank_with_mode("query", candidates)

        assert mode == "rrf_fallback"
        assert result == candidates
        assert collector._reranker_stats["total_calls"] == 1
        assert collector._reranker_stats["total_errors"] == 1

    async def test_rerank_empty_candidates(self, collector: MetricsCollector) -> None:
        inner = MagicMock()

        async def mock_rerank_with_mode(query, cands):
            return ("reranked", [])

        inner.rerank_with_mode = mock_rerank_with_mode

        wrapped = InstrumentedReranker(inner, collector)
        mode, result = await wrapped.rerank_with_mode("query", [])

        assert mode == "reranked"
        assert result == []
        assert collector._reranker_stats["total_calls"] == 1
        assert collector._reranker_stats["total_candidates"] == 0

    async def test_is_available_passthrough(self, collector: MetricsCollector) -> None:
        inner = MagicMock()

        async def mock_is_available():
            return True

        inner.is_available = mock_is_available

        wrapped = InstrumentedReranker(inner, collector)
        assert await wrapped.is_available() is True


class TestInstrumentedGraphService:
    @pytest.fixture
    def collector(self) -> MetricsCollector:
        return MetricsCollector(engine=MagicMock(), session_factory=MagicMock())

    async def test_upsert_node_records_query(self, collector: MetricsCollector) -> None:
        inner = MagicMock()

        async def mock_upsert_node(*args, **kwargs) -> None:
            pass

        inner.upsert_node = mock_upsert_node
        wrapped = InstrumentedGraphService(inner, collector)

        await wrapped.upsert_node("Decision", MagicMock(), {"title": "test"})
        assert collector._graph_stats["total_queries"] == 1
        assert collector._graph_stats["total_errors"] == 0
        assert collector._graph_stats["total_latency"] > 0

    async def test_delete_node_records_query(self, collector: MetricsCollector) -> None:
        inner = MagicMock()

        async def mock_delete_node(*args, **kwargs) -> None:
            pass

        inner.delete_node = mock_delete_node
        wrapped = InstrumentedGraphService(inner, collector)

        await wrapped.delete_node("Decision", MagicMock())
        assert collector._graph_stats["total_queries"] == 1

    async def test_create_relation_records_query(self, collector: MetricsCollector) -> None:
        inner = MagicMock()

        async def mock_create_relation(*args, **kwargs) -> None:
            pass

        inner.create_relation = mock_create_relation
        wrapped = InstrumentedGraphService(inner, collector)

        await wrapped.create_relation(MagicMock(), MagicMock(), "SUPERSEDES")
        assert collector._graph_stats["total_queries"] == 1

    async def test_delete_relation_records_query(self, collector: MetricsCollector) -> None:
        inner = MagicMock()

        async def mock_delete_relation(*args, **kwargs) -> None:
            pass

        inner.delete_relation = mock_delete_relation
        wrapped = InstrumentedGraphService(inner, collector)

        await wrapped.delete_relation(MagicMock(), MagicMock(), "SUPERSEDES")
        assert collector._graph_stats["total_queries"] == 1

    async def test_link_to_project_records_query(self, collector: MetricsCollector) -> None:
        inner = MagicMock()

        async def mock_link_to_project(*args, **kwargs) -> None:
            pass

        inner.link_to_project = mock_link_to_project
        wrapped = InstrumentedGraphService(inner, collector)

        await wrapped.link_to_project(MagicMock(), "brain_v42")
        assert collector._graph_stats["total_queries"] == 1

    @pytest.mark.parametrize("method_name", ["unlink_from_project", "unlink_from_domain"])
    async def test_membership_unlink_records_query_and_propagates_outcome(
        self, collector: MetricsCollector, method_name: str
    ) -> None:
        inner = MagicMock()
        setattr(inner, method_name, AsyncMock(return_value="ok"))
        wrapped = InstrumentedGraphService(inner, collector)

        outcome = await getattr(wrapped, method_name)(MagicMock(), "stable-key")

        assert outcome == "ok"
        assert collector._graph_stats["total_queries"] == 1

    @pytest.mark.parametrize("method_name", ["upsert_project", "delete_project"])
    async def test_project_write_records_query_and_propagates_outcome(
        self, collector: MetricsCollector, method_name: str
    ) -> None:
        inner = MagicMock()
        setattr(inner, method_name, AsyncMock(return_value="ok"))
        wrapped = InstrumentedGraphService(inner, collector)

        outcome = await getattr(wrapped, method_name)("brain-v42", MagicMock())

        assert outcome == "ok"
        assert collector._graph_stats["total_queries"] == 1

    @pytest.mark.parametrize(
        ("method_name", "outcome"),
        [("create_project_relation", "created"), ("delete_project_relation", "ok")],
    )
    async def test_project_relation_write_records_query_and_propagates_outcome(
        self,
        collector: MetricsCollector,
        method_name: str,
        outcome: str,
    ) -> None:
        inner = MagicMock()
        setattr(inner, method_name, AsyncMock(return_value=outcome))
        wrapped = InstrumentedGraphService(inner, collector)

        result = await getattr(wrapped, method_name)("parent", "child", "CONTAINS")

        assert result == outcome
        assert collector._graph_stats["total_queries"] == 1

    async def test_get_neighbors_records_query(self, collector: MetricsCollector) -> None:
        inner = MagicMock()

        async def mock_get_neighbors(*args, **kwargs) -> list:
            return []

        inner.get_neighbors = mock_get_neighbors
        wrapped = InstrumentedGraphService(inner, collector)

        result = await wrapped.get_neighbors(MagicMock())
        assert result == []
        assert collector._graph_stats["total_queries"] == 1

    async def test_get_supersession_chain_records_query(self, collector: MetricsCollector) -> None:
        inner = MagicMock()

        async def mock_get_supersession_chain(*args, **kwargs) -> list:
            return ["id1", "id2"]

        inner.get_supersession_chain = mock_get_supersession_chain
        wrapped = InstrumentedGraphService(inner, collector)

        result = await wrapped.get_supersession_chain(MagicMock())
        assert result == ["id1", "id2"]
        assert collector._graph_stats["total_queries"] == 1

    async def test_get_project_tree_records_query(self, collector: MetricsCollector) -> None:
        inner = MagicMock()

        async def mock_get_project_tree(*args, **kwargs) -> list:
            return ["sub1"]

        inner.get_project_tree = mock_get_project_tree
        wrapped = InstrumentedGraphService(inner, collector)

        result = await wrapped.get_project_tree("brain_v42")
        assert result == ["sub1"]
        assert collector._graph_stats["total_queries"] == 1

    async def test_get_related_ids_records_query(self, collector: MetricsCollector) -> None:
        inner = MagicMock()

        async def mock_get_related_ids(*args, **kwargs) -> dict:
            return {}

        inner.get_related_ids = mock_get_related_ids
        wrapped = InstrumentedGraphService(inner, collector)

        result = await wrapped.get_related_ids([])
        assert result == {}
        assert collector._graph_stats["total_queries"] == 1

    async def test_healthcheck_passthrough(self, collector: MetricsCollector) -> None:
        inner = MagicMock()

        async def mock_healthcheck() -> bool:
            return True

        inner.healthcheck = mock_healthcheck
        wrapped = InstrumentedGraphService(inner, collector)

        assert await wrapped.healthcheck() is True
        # healthcheck does NOT record a query
        assert collector._graph_stats["total_queries"] == 0

    async def test_multiple_calls_accumulate(self, collector: MetricsCollector) -> None:
        inner = MagicMock()

        async def mock_upsert_node(*args, **kwargs) -> None:
            pass

        inner.upsert_node = mock_upsert_node
        wrapped = InstrumentedGraphService(inner, collector)

        await wrapped.upsert_node("Decision", MagicMock(), {})
        await wrapped.upsert_node("Learning", MagicMock(), {})
        assert collector._graph_stats["total_queries"] == 2

    async def test_upsert_node_records_error_on_exception(
        self, collector: MetricsCollector
    ) -> None:
        inner = MagicMock()

        async def mock_upsert_node(*args, **kwargs) -> None:
            raise RuntimeError("Neo4j down")

        inner.upsert_node = mock_upsert_node
        wrapped = InstrumentedGraphService(inner, collector)

        with pytest.raises(RuntimeError, match="Neo4j down"):
            await wrapped.upsert_node("Decision", MagicMock(), {})

        assert collector._graph_stats["total_queries"] == 1
        assert collector._graph_stats["total_errors"] == 1
        assert len(collector._graph_error_times) == 1

    async def test_get_related_ids_records_error_on_exception(
        self, collector: MetricsCollector
    ) -> None:
        inner = MagicMock()

        async def mock_get_related_ids(*args, **kwargs) -> dict:
            raise ConnectionError("connection lost")

        inner.get_related_ids = mock_get_related_ids
        wrapped = InstrumentedGraphService(inner, collector)

        with pytest.raises(ConnectionError, match="connection lost"):
            await wrapped.get_related_ids(["id1"])

        assert collector._graph_stats["total_queries"] == 1
        assert collector._graph_stats["total_errors"] == 1

    async def test_create_relation_returns_outcome(self, collector: MetricsCollector) -> None:
        """create_relation now returns RelationWriteOutcome (A2) — wrapper
        must forward the return value, not discard it."""
        inner = MagicMock()

        async def mock_create_relation(*args, **kwargs) -> str:
            return "created"

        inner.create_relation = mock_create_relation
        wrapped = InstrumentedGraphService(inner, collector)

        outcome = await wrapped.create_relation(MagicMock(), MagicMock(), "RELATED_TO")
        assert outcome == "created"
        assert collector._graph_stats["total_queries"] == 1

    async def test_get_path_records_query(self, collector: MetricsCollector) -> None:
        inner = MagicMock()

        async def mock_get_path(*args, **kwargs) -> list:
            return [{"id": "x", "type": "Decision", "label": "t"}]

        inner.get_path = mock_get_path
        wrapped = InstrumentedGraphService(inner, collector)

        result = await wrapped.get_path(MagicMock(), MagicMock(), max_depth=2)
        assert len(result) == 1
        assert collector._graph_stats["total_queries"] == 1

    async def test_upsert_domain_returns_bool(self, collector: MetricsCollector) -> None:
        inner = MagicMock()

        async def mock_upsert_domain(*args, **kwargs) -> bool:
            return True

        inner.upsert_domain = mock_upsert_domain
        wrapped = InstrumentedGraphService(inner, collector)

        result = await wrapped.upsert_domain("infra")
        assert result is True
        assert collector._graph_stats["total_queries"] == 1

    async def test_link_entity_to_domain_returns_outcome(self, collector: MetricsCollector) -> None:
        inner = MagicMock()

        async def mock_link(*args, **kwargs) -> str:
            return "matched"

        inner.link_entity_to_domain = mock_link
        wrapped = InstrumentedGraphService(inner, collector)

        outcome = await wrapped.link_entity_to_domain(MagicMock(), "infra")
        assert outcome == "matched"
        assert collector._graph_stats["total_queries"] == 1

    async def test_find_orphans_for_classification_records_query(
        self, collector: MetricsCollector
    ) -> None:
        inner = MagicMock()

        async def mock_find(*args, **kwargs) -> list:
            return [{"id": "x", "labels": ["Decision"]}]

        inner.find_orphans_for_classification = mock_find
        wrapped = InstrumentedGraphService(inner, collector)

        result = await wrapped.find_orphans_for_classification(limit=20)
        assert len(result) == 1
        assert collector._graph_stats["total_queries"] == 1

    async def test_fetch_active_domains_records_query(self, collector: MetricsCollector) -> None:
        inner = MagicMock()

        async def mock_fetch(*args, **kwargs) -> list:
            return ["ml", "memory"]

        inner.fetch_active_domains = mock_fetch
        wrapped = InstrumentedGraphService(inner, collector)

        result = await wrapped.fetch_active_domains("brain-v42", top_n=2)
        assert result == ["ml", "memory"]
        assert collector._graph_stats["total_queries"] == 1

    async def test_fetch_cross_project_entity_ids_records_query(
        self, collector: MetricsCollector
    ) -> None:
        inner = MagicMock()

        async def mock_fetch(*args, **kwargs) -> list:
            return [{"id": "abc", "labels": ["Decision"], "project_key": "red-shrik"}]

        inner.fetch_cross_project_entity_ids = mock_fetch
        wrapped = InstrumentedGraphService(inner, collector)

        result = await wrapped.fetch_cross_project_entity_ids(
            ["ml"], exclude_project_key="brain-v42"
        )
        assert len(result) == 1
        assert collector._graph_stats["total_queries"] == 1

    async def test_fetch_decision_ids_in_domain_records_query(
        self, collector: MetricsCollector
    ) -> None:
        inner = MagicMock()

        async def mock_fetch(*args, **kwargs) -> list:
            return ["11111111-1111-1111-1111-111111111111"]

        inner.fetch_decision_ids_in_domain = mock_fetch
        wrapped = InstrumentedGraphService(inner, collector)

        result = await wrapped.fetch_decision_ids_in_domain("ml")
        assert result == ["11111111-1111-1111-1111-111111111111"]
        assert collector._graph_stats["total_queries"] == 1


class _SpySpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}
        self.ended = False

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def end(self) -> None:
        self.ended = True


class _SpyTracer:
    """A tracer double — the OTel SDK is not installed, and these tests must run
    without it (that is the state of CI and of production)."""

    def __init__(self) -> None:
        self.spans: list[_SpySpan] = []

    def start_span(self, name: str, **_kwargs: object) -> _SpySpan:
        span = _SpySpan()
        span.attributes["__name__"] = name
        self.spans.append(span)
        return span


class TestTheSpanNeverContradictsTheCounter:
    """The span and the counter must carry THE SAME verdict.

    Two truths about the same call make none: if the span says `error` where the
    counter says success, nobody knows which to believe any more, and both are lost.
    They therefore read the same variable and the same latency.
    """

    async def test_a_successful_call_emits_a_span_agreeing_with_the_counter(self) -> None:
        from brain_v42 import tracing
        from brain_v42.metrics.instrument import instrument_tool

        tracer = _SpyTracer()
        tracing.set_tracer(tracer)
        tracing.reset_actor_cardinality()
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        try:

            @instrument_tool(collector, "brain_search")
            async def ok() -> str:
                return "ok"

            assert await ok() == "ok"
        finally:
            tracing.set_tracer(None)

        assert len(tracer.spans) == 1
        assert tracer.spans[0].attributes["gen_ai.tool.name"] == "brain_search"
        assert tracer.spans[0].attributes["brain.tool.error"] is False
        assert tracer.spans[0].ended

    async def test_a_failing_call_emits_a_span_naming_the_class_not_the_message(self) -> None:
        from brain_v42 import tracing
        from brain_v42.metrics.instrument import instrument_tool

        tracer = _SpyTracer()
        tracing.set_tracer(tracer)
        tracing.reset_actor_cardinality()
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        try:

            @instrument_tool(collector, "brain_ticket_get")
            async def boom() -> None:
                raise ValueError("ticket du projet confidentiel introuvable")

            with pytest.raises(ValueError):
                await boom()
        finally:
            tracing.set_tracer(None)

        attributes = tracer.spans[0].attributes
        assert attributes["error.type"] == "ValueError"
        assert attributes["brain.tool.error"] is True
        assert "confidentiel" not in " ".join(str(v) for v in attributes.values())

    async def test_tracing_disabled_changes_nothing_for_the_counter(self) -> None:
        """The killswitch must touch ONLY the spans. A counter that depended on the
        tracing would make the metrics disappear the day the flag is closed."""
        from brain_v42 import tracing
        from brain_v42.metrics.instrument import instrument_tool

        tracing.set_tracer(None)
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())

        @instrument_tool(collector, "brain_search")
        async def ok() -> str:
            return "ok"

        assert await ok() == "ok"
        assert collector.get_metrics()["tools"]["brain_search"]["calls"] == 1

    async def test_a_broken_tracer_never_breaks_the_tool_call(self) -> None:
        """Telemetry is an observation channel: it cannot bring down the operation
        it observes."""
        from brain_v42 import tracing
        from brain_v42.metrics.instrument import instrument_tool

        class _BoomTracer:
            def start_span(self, *_a: object, **_kw: object) -> None:
                raise RuntimeError("exporter mort")

        tracing.set_tracer(_BoomTracer())
        collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
        try:

            @instrument_tool(collector, "brain_search")
            async def ok() -> str:
                return "ok"

            assert await ok() == "ok"
        finally:
            tracing.set_tracer(None)
