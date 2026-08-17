"""Instrumentation wrappers for MCP tools, embedding service, and reranker."""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import AuthorizationError

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.provenance import get_current_actor, normalize_agent
from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable
from brain_v42.tracing import finish_tool_span, start_tool_span

if TYPE_CHECKING:
    from brain_v42.services.search.hybrid import RankedCandidate

_instrument_logger = logging.getLogger(__name__)

# Alias de compatibilité : la classification vit désormais dans
# brain_v42.provenance, importée par la couche transport comme par les
# services. tests/unit/test_metrics_instrument.py importe ce nom.
_normalize_agent = normalize_agent


def instrument_tool(collector: MetricsCollector, tool_name: str) -> Callable:
    """Decorator that records tool call metrics (calls, errors, latency)."""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.monotonic()
            error = False
            # Le span est ouvert ICI et non dans un middleware : c'est le seul
            # point où l'exception est encore la vraie, avant le masquage de
            # `call_tool` (voir l'en-tête de metrics/tool_instrumentation.py).
            span = start_tool_span(tool_name)
            failure: BaseException | None = None
            unwrap_cause = False
            try:
                return await func(*args, **kwargs)
            except AuthorizationError as exc:
                error = True
                # Pas de déballage : un refus d'autorisation EST le diagnostic,
                # et sa cause éventuelle porterait le contexte qu'on refuse de
                # publier.
                failure = exc
                raise
            except Exception as exc:
                error = True
                failure = exc
                unwrap_cause = True
                _instrument_logger.error(
                    "mcp_tool_error tool=%s exception_type=%s",
                    tool_name,
                    type(exc).__name__,
                )
                raise
            finally:
                latency_ms = (time.monotonic() - start) * 1000
                agent = get_current_actor()
                collector.record_tool_call(tool_name, latency_ms, error=error, agent=agent)
                # MÊMES valeurs que le compteur, délibérément : un span qui le
                # contredirait donnerait deux vérités et en annulerait deux.
                finish_tool_span(
                    span,
                    actor=agent,
                    error=error,
                    latency_ms=latency_ms,
                    exception=failure,
                    unwrap=unwrap_cause,
                )

        return wrapper

    return decorator


class InstrumentedEmbeddingService:
    """Wrapper around GPUEmbeddingService that records metrics.

    Duck-typed: exposes embed(), embed_texts(), similarity(), healthcheck(), close()
    — same interface as GPUEmbeddingService.
    """

    def __init__(self, inner: Any, collector: MetricsCollector) -> None:
        self._inner = inner
        self._collector = collector

    async def embed(self, text: str) -> list[float]:
        start = time.monotonic()
        error = False
        error_kind: str | None = None
        try:
            return await self._inner.embed(text)  # type: ignore[no-any-return]
        except EmbeddingUnavailable as exc:
            error = True
            error_kind = exc.kind
            raise
        except Exception:
            error = True
            raise
        finally:
            latency_ms = (time.monotonic() - start) * 1000
            self._collector.record_embedding_request(latency_ms, error=error, error_kind=error_kind)

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        start = time.monotonic()
        error = False
        error_kind: str | None = None
        try:
            return await self._inner.embed_texts(texts)  # type: ignore[no-any-return]
        except EmbeddingUnavailable as exc:
            error = True
            error_kind = exc.kind
            raise
        except Exception:
            error = True
            raise
        finally:
            latency_ms = (time.monotonic() - start) * 1000
            self._collector.record_embedding_request(latency_ms, error=error, error_kind=error_kind)

    def similarity(self, vec_a: list[float], vec_b: list[float]) -> float:
        return self._inner.similarity(vec_a, vec_b)  # type: ignore[no-any-return]

    async def healthcheck(self) -> bool:
        return await self._inner.healthcheck()  # type: ignore[no-any-return]

    async def close(self) -> None:
        return await self._inner.close()  # type: ignore[no-any-return]


class InstrumentedReranker:
    """Wrapper around HybridReranker that records metrics.

    Duck-typed: exposes async rerank_with_mode(query, candidates) and is_available().

    rerank_with_mode delegates to inner.rerank_with_mode and records latency +
    candidate count via record_reranker_call.  rrf_fallback mode is counted as
    error=True because it signals that the reranker service was unavailable —
    the fallback path is a degraded response, not a successful rerank.
    """

    def __init__(self, inner: Any, collector: MetricsCollector) -> None:
        self._inner = inner
        self._collector = collector

    async def rerank_with_mode(
        self,
        query: str,
        candidates: list[RankedCandidate],
    ) -> tuple[str, list[RankedCandidate]]:
        start = time.monotonic()
        error = False
        candidate_count = len(candidates)
        try:
            mode, result = await self._inner.rerank_with_mode(query, candidates)
            # rrf_fallback means the reranker service was down — count as error
            # so red-monitor can detect sustained degradation.
            if mode == "rrf_fallback":
                error = True
            return mode, result
        except Exception:
            error = True
            raise
        finally:
            latency_ms = (time.monotonic() - start) * 1000
            self._collector.record_reranker_call(latency_ms, candidate_count, error=error)

    async def is_available(self) -> bool:
        return await self._inner.is_available()  # type: ignore[no-any-return]


class InstrumentedGraphService:
    """Wrapper around GraphService that records metrics.

    Duck-typed: exposes the same async interface as GraphService.
    Write/traversal methods are timed and counted; healthcheck is a passthrough.
    """

    def __init__(self, inner: Any, collector: MetricsCollector) -> None:
        self._inner = inner
        self._collector = collector

    async def upsert_node(self, *args: Any, **kwargs: Any) -> Any:
        """Proxy upsert_node and count errors via the returned 'ok'|'error' outcome.

        Fix 2: GraphService.upsert_node now returns a NodeWriteOutcome. The
        previous version caught only exceptions, so swallowed Neo4j errors
        (returned as 'error' outcomes) were never counted in metrics — making
        graph.total_errors structurally dead for node writes.
        """
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.upsert_node(*args, **kwargs)
            if result == "error":
                error = True
            return result
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def delete_node(self, *args: Any, **kwargs: Any) -> Any:
        """Proxy delete_node and count errors via the returned 'ok'|'error' outcome.

        Fix 2: same as upsert_node — counts swallowed Neo4j errors in metrics.
        """
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.delete_node(*args, **kwargs)
            if result == "error":
                error = True
            return result
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def upsert_project(self, *args: Any, **kwargs: Any) -> Any:
        """Proxy upsert_project and count swallowed write errors."""
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.upsert_project(*args, **kwargs)
            if result == "error":
                error = True
            return result
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def delete_project(self, *args: Any, **kwargs: Any) -> Any:
        """Proxy delete_project and count swallowed write errors."""
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.delete_project(*args, **kwargs)
            if result == "error":
                error = True
            return result
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def create_project_relation(self, *args: Any, **kwargs: Any) -> Any:
        """Proxy create_project_relation and count swallowed write errors."""
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.create_project_relation(*args, **kwargs)
            if result == "error":
                error = True
            return result
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def delete_project_relation(self, *args: Any, **kwargs: Any) -> Any:
        """Proxy delete_project_relation and count swallowed write errors."""
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.delete_project_relation(*args, **kwargs)
            if result == "error":
                error = True
            return result
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def create_relation(self, *args: Any, **kwargs: Any) -> Any:
        """Proxy create_relation and count 'error' outcomes in metrics.

        MAJOR 1 fix: 'error' outcome (Neo4j infra failure) is counted as error=True
        in record_graph_query so graph.total_errors is live for RELATION writes.

        Explicit design decision: 'missing_node' does NOT count as an infra metric
        error. It signals structural drift (anchor nodes absent in the graph — data
        quality issue traced to the 2026-06-22 435-edge audit), not a Neo4j failure.
        It is surfaced via WARN logs in graph_create_relation_logged instead, keeping
        the infra metric clean for genuine connectivity failures.
        """
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.create_relation(*args, **kwargs)
            if result == "error":
                error = True
            return result
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def delete_relation(self, *args: Any, **kwargs: Any) -> Any:
        """Proxy delete_relation. Inner now returns NodeWriteOutcome ('ok'|'error')."""
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.delete_relation(*args, **kwargs)
            if result == "error":
                error = True
            return result
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def link_to_project(self, *args: Any, **kwargs: Any) -> Any:
        """Proxy link_to_project and count errors via the returned 'ok'|'error' outcome.

        Fix 2: same as upsert_node — counts swallowed Neo4j errors in metrics.
        """
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.link_to_project(*args, **kwargs)
            if result == "error":
                error = True
            return result
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def unlink_from_project(self, *args: Any, **kwargs: Any) -> Any:
        """Proxy unlink_from_project and count swallowed write errors."""
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.unlink_from_project(*args, **kwargs)
            if result == "error":
                error = True
            return result
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def get_neighbors(self, *args: Any, **kwargs: Any) -> list:
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.get_neighbors(*args, **kwargs)
            return result  # type: ignore[no-any-return]
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def get_supersession_chain(self, *args: Any, **kwargs: Any) -> list:
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.get_supersession_chain(*args, **kwargs)
            return result  # type: ignore[no-any-return]
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def get_project_tree(self, *args: Any, **kwargs: Any) -> list:
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.get_project_tree(*args, **kwargs)
            return result  # type: ignore[no-any-return]
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def get_related_ids(self, *args: Any, **kwargs: Any) -> dict:
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.get_related_ids(*args, **kwargs)
            return result  # type: ignore[no-any-return]
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def find_unlinked_nodes(self, *args: Any, **kwargs: Any) -> list:
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.find_unlinked_nodes(*args, **kwargs)
            return result  # type: ignore[no-any-return]
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def get_all_related_edges(self, *args: Any, **kwargs: Any) -> list:
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.get_all_related_edges(*args, **kwargs)
            return result  # type: ignore[no-any-return]
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def get_path(self, *args: Any, **kwargs: Any) -> list:
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.get_path(*args, **kwargs)
            return result  # type: ignore[no-any-return]
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def upsert_domain(self, *args: Any, **kwargs: Any) -> str:
        """Proxy upsert_domain. Returns Literal['ok','invalid_domain','error'] from inner."""
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.upsert_domain(*args, **kwargs)
            return result  # type: ignore[no-any-return]
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def link_entity_to_domain(self, *args: Any, **kwargs: Any) -> Any:
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.link_entity_to_domain(*args, **kwargs)
            return result
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def unlink_from_domain(self, *args: Any, **kwargs: Any) -> Any:
        """Proxy unlink_from_domain and count swallowed write errors."""
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.unlink_from_domain(*args, **kwargs)
            if result == "error":
                error = True
            return result
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def find_orphans_for_classification(self, *args: Any, **kwargs: Any) -> list:
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.find_orphans_for_classification(*args, **kwargs)
            return result  # type: ignore[no-any-return]
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def fetch_active_domains(self, *args: Any, **kwargs: Any) -> list:
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.fetch_active_domains(*args, **kwargs)
            return result  # type: ignore[no-any-return]
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def fetch_cross_project_entity_ids(self, *args: Any, **kwargs: Any) -> list:
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.fetch_cross_project_entity_ids(*args, **kwargs)
            return result  # type: ignore[no-any-return]
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def fetch_decision_ids_in_domain(self, *args: Any, **kwargs: Any) -> list:
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.fetch_decision_ids_in_domain(*args, **kwargs)
            return result  # type: ignore[no-any-return]
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def healthcheck(self) -> bool:
        return await self._inner.healthcheck()  # type: ignore[no-any-return]
