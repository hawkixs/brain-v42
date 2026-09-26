"""Ticket 3a4ed612 — the sidecar's top-level ``model`` was the literal
``"Qodo-Embed-1-1.5B"`` (``collector.py:560``), frozen at the day PR #181 shipped
``embedding_service.usage`` and never updated for the codestral trial (4fac067a)
or the gpu-host rollback path (``deploy/dev-pc``).

The fix, per the W2 plan (sidecar-synthesis.md §4):

* Each MCP process flushes its OWN identity ``{backend, model, host}`` (``host``
  is the embedding endpoint's hostname, never the machine's) into
  ``process_metrics.embedding_stats.identity``, next to PR #181's ``usage``.
* The sidecar aggregates identity from LIVE ``_process`` rows only (the same
  60 s liveness window ``active_processes`` already uses): one live model ->
  that model; more than one distinct live model -> ``"mixed"`` plus
  ``models_seen``.
* No live identity at all (fresh deploy, or every live row still pre-dates this
  feature) -> the sidecar's OWN settings serve as the labelled fallback,
  ``model_source: "sidecar_settings"``.
* Top-level ``model`` mirrors ``embedding_service.model`` — a deprecated alias,
  never a second source of truth.
* ``embedding_api_key`` (SecretStr, config.py:191) must never appear, in any
  form, in anything this feature serialises.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.server import MetricsServer


def _settings(
    *,
    backend: str = "shim",
    model: str = "qodo",
    url: str = "http://localhost:8003",
    api_key: str = "",
) -> MagicMock:
    from pydantic import SecretStr

    return MagicMock(
        embedding_service_url=url,
        embedding_dimension=1536,
        embedding_backend=backend,
        embedding_model=model,
        embedding_api_key=SecretStr(api_key),
    )


# ---------------------------------------------------------------------------
# 1. Each process flushes {backend, model, host} into embedding_stats.identity
# ---------------------------------------------------------------------------


class TestFlushIdentity:
    def test_get_flush_data_carries_process_identity_from_settings(self) -> None:
        with patch(
            "brain_v42.metrics.collector.get_settings",
            return_value=_settings(
                backend="openai", model="text-embed-3", url="http://gpu-host:8003"
            ),
        ):
            collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
            fd = collector.get_flush_data()

        identity = fd["_process"]["embedding"]["identity"]
        assert identity == {"backend": "openai", "model": "text-embed-3", "host": "gpu-host"}

    def test_identity_host_is_hostname_only_not_the_full_url(self) -> None:
        with patch(
            "brain_v42.metrics.collector.get_settings",
            return_value=_settings(url="http://localhost:8003/some/path"),
        ):
            collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
            identity = collector.get_flush_data()["_process"]["embedding"]["identity"]

        assert identity["host"] == "localhost"

    def test_no_secretstr_or_api_key_value_in_the_serialised_flush_payload(self) -> None:
        with patch(
            "brain_v42.metrics.collector.get_settings",
            return_value=_settings(api_key="sk-do-not-leak-this"),
        ):
            collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
            fd = collector.get_flush_data()

        serialised = json.dumps(fd)
        assert "sk-do-not-leak-this" not in serialised
        assert "SecretStr" not in serialised
        assert "embedding_api_key" not in serialised


# ---------------------------------------------------------------------------
# 2. collect_process_metrics aggregates identity from LIVE _process rows
# ---------------------------------------------------------------------------


def _row(
    agent_name: str,
    embedding_stats: dict[str, Any],
    *,
    is_live: bool = True,
) -> tuple[Any, ...]:
    """Match the SELECT column order in collect_process_metrics' SQL text."""
    return (agent_name, 1, None, None, {}, embedding_stats, 0, is_live)


async def _collect(rows: list[tuple[Any, ...]]) -> dict[str, Any]:
    from brain_v42.metrics.collector_db import _DbCollectorsMixin

    class _Result:
        def all(self) -> list[tuple[Any, ...]]:
            return rows

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, statement: Any) -> _Result:
            return _Result()

    mixin = _DbCollectorsMixin.__new__(_DbCollectorsMixin)
    mixin._session_factory = MagicMock(return_value=_Session())  # type: ignore[attr-defined]
    result: dict[str, Any] = await mixin.collect_process_metrics()
    return result


class TestCollectProcessMetricsIdentity:
    @pytest.mark.asyncio
    async def test_single_live_process_reports_its_own_identity(self) -> None:
        result = await _collect(
            [
                _row(
                    "_process",
                    {"identity": {"backend": "shim", "model": "qodo", "host": "localhost"}},
                ),
            ]
        )

        assert result["embedding_identity"] == {
            "model": "qodo",
            "backend": "shim",
            "endpoint_host": "localhost",
            "models_seen": ["qodo"],
        }

    @pytest.mark.asyncio
    async def test_two_distinct_live_models_report_mixed_plus_models_seen(self) -> None:
        result = await _collect(
            [
                _row(
                    "_process",
                    {"identity": {"backend": "shim", "model": "qodo", "host": "localhost"}},
                ),
                _row(
                    "_process",
                    {"identity": {"backend": "openai", "model": "codestral", "host": "gpu-host"}},
                ),
            ]
        )

        assert result["embedding_identity"]["model"] == "mixed"
        assert result["embedding_identity"]["models_seen"] == ["codestral", "qodo"]

    @pytest.mark.asyncio
    async def test_stale_non_live_row_is_excluded_from_identity(self) -> None:
        """A row outside the 60s liveness window must not count as a live model,
        exactly like `active_processes` already excludes it (retention.py)."""
        result = await _collect(
            [
                _row(
                    "_process",
                    {"identity": {"backend": "shim", "model": "qodo", "host": "localhost"}},
                    is_live=False,
                ),
            ]
        )

        assert result["embedding_identity"] is None

    @pytest.mark.asyncio
    async def test_legacy_process_row_with_no_identity_key_does_not_crash(self) -> None:
        """A `_process` row written before this feature shipped carries no
        `identity` key at all — must degrade to None, never raise."""
        result = await _collect(
            [
                _row("_process", {"usage": {}}),
            ]
        )

        assert result["embedding_identity"] is None

    @pytest.mark.asyncio
    async def test_malformed_identity_value_costs_its_own_row_not_the_whole_aggregate(
        self,
    ) -> None:
        result = await _collect(
            [
                _row("_process", {"identity": "not-a-dict"}),
                _row(
                    "_process",
                    {"identity": {"backend": "shim", "model": "qodo", "host": "localhost"}},
                ),
            ]
        )

        assert result["embedding_identity"] == {
            "model": "qodo",
            "backend": "shim",
            "endpoint_host": "localhost",
            "models_seen": ["qodo"],
        }

    @pytest.mark.asyncio
    async def test_no_process_rows_at_all_gives_none_not_a_crash(self) -> None:
        result = await _collect([])
        assert result["embedding_identity"] is None


# ---------------------------------------------------------------------------
# 3. server.py: published embedding_service.{model,backend,endpoint_host,
#    models_seen,model_source}; top-level `model` is a deprecated alias.
# ---------------------------------------------------------------------------


def _mock_embedding_svc() -> MagicMock:
    svc = MagicMock()
    svc.healthcheck = AsyncMock(return_value=True)
    return svc


def _base_collector_stubs(collector: MetricsCollector) -> None:
    collector.collect_db_stats = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "pool": {"active": 0, "idle": 0, "overflow": 0, "max": 15},
            "tables": {},
            "db_size_bytes": 0,
            "dimension_mismatches": 0,
            "embedding_backlog": {"total": 0, "by_entity_type": {}},
            "graph_outbox": {
                "available": False,
                "pending": 0,
                "ready": 0,
                "claimed": 0,
                "exhausted": 0,
                "oldest_pending_age_seconds": 0.0,
                "projector": {
                    "generation": -1,
                    "armed": False,
                    "lease_active": False,
                    "recovery_active": False,
                    "healthy": False,
                },
            },
        }
    )
    collector.collect_search_quality = AsyncMock(  # type: ignore[method-assign]
        return_value={"searches_total": 0, "searches_with_zero_results": 0, "avg_score": 0.0}
    )
    collector.collect_dream_metrics = AsyncMock(return_value={})  # type: ignore[method-assign]
    collector.collect_dream_promotions = AsyncMock(return_value={})  # type: ignore[method-assign]
    collector.collect_dream_promoted_health = AsyncMock(return_value=[])  # type: ignore[method-assign]
    collector.collect_nightly_ops = AsyncMock(return_value={})  # type: ignore[method-assign]


class TestServerEmbeddingServicePayload:
    async def test_no_live_processes_falls_back_to_sidecar_settings(
        self, aiohttp_client: Any
    ) -> None:
        with patch(
            "brain_v42.metrics.collector.get_settings",
            return_value=_settings(backend="shim", model="qodo", url="http://localhost:8003"),
        ):
            collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
            _base_collector_stubs(collector)
            collector.collect_process_metrics = AsyncMock(  # type: ignore[method-assign]
                return_value={
                    "active_processes": 0,
                    "active_agents": 0,
                    "total_memory_rss_bytes": 0,
                    "tools": {},
                    "decay": {"stale_count": 0, "archived_count": 0, "access_log_size": 0},
                    "embedding": {
                        "total_requests": 0,
                        "total_errors": 0,
                        "gpu_busy_errors": 0,
                        "unreachable_errors": 0,
                        "recent_errors": 0,
                        "avg_latency_ms": 0.0,
                        "usage": {
                            "read": {"total_tokens": 0, "reported_requests": 0},
                            "write": {"total_tokens": 0, "reported_requests": 0},
                        },
                    },
                    "by_agent": {},
                    "embedding_identity": None,
                }
            )
            server = MetricsServer(collector, _mock_embedding_svc(), port=0, host="127.0.0.1")
            client = await aiohttp_client(server._build_app())
            resp = await client.get("/metrics")
            metrics = await resp.json()

        assert metrics["embedding_service"]["model"] == "qodo"
        assert metrics["embedding_service"]["backend"] == "shim"
        assert metrics["embedding_service"]["endpoint_host"] == "localhost"
        assert metrics["embedding_service"]["models_seen"] == ["qodo"]
        assert metrics["embedding_service"]["model_source"] == "sidecar_settings"
        assert metrics["model"] == "qodo"
        assert "embedding_identity" not in metrics["cross_process"]

    async def test_live_processes_override_with_their_own_identity(
        self, aiohttp_client: Any
    ) -> None:
        with patch(
            "brain_v42.metrics.collector.get_settings",
            return_value=_settings(backend="shim", model="qodo", url="http://localhost:8003"),
        ):
            collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
            _base_collector_stubs(collector)
            collector.collect_process_metrics = AsyncMock(  # type: ignore[method-assign]
                return_value={
                    "active_processes": 1,
                    "active_agents": 0,
                    "total_memory_rss_bytes": 0,
                    "tools": {},
                    "decay": {"stale_count": 0, "archived_count": 0, "access_log_size": 0},
                    "embedding": {
                        "total_requests": 5,
                        "total_errors": 0,
                        "gpu_busy_errors": 0,
                        "unreachable_errors": 0,
                        "recent_errors": 0,
                        "avg_latency_ms": 1.0,
                        "usage": {
                            "read": {"total_tokens": 0, "reported_requests": 0},
                            "write": {"total_tokens": 0, "reported_requests": 0},
                        },
                    },
                    "by_agent": {},
                    "embedding_identity": {
                        "model": "codestral",
                        "backend": "openai",
                        "endpoint_host": "gpu-host",
                        "models_seen": ["codestral"],
                    },
                }
            )
            server = MetricsServer(collector, _mock_embedding_svc(), port=0, host="127.0.0.1")
            client = await aiohttp_client(server._build_app())
            resp = await client.get("/metrics")
            metrics = await resp.json()

        assert metrics["embedding_service"]["model"] == "codestral"
        assert metrics["embedding_service"]["backend"] == "openai"
        assert metrics["embedding_service"]["endpoint_host"] == "gpu-host"
        assert metrics["embedding_service"]["models_seen"] == ["codestral"]
        assert metrics["embedding_service"]["model_source"] == "live_processes"
        # Deprecated alias — never a second source of truth.
        assert metrics["model"] == metrics["embedding_service"]["model"] == "codestral"
        assert "embedding_identity" not in metrics["cross_process"]

    async def test_no_secretstr_leaks_through_the_full_metrics_payload(
        self, aiohttp_client: Any
    ) -> None:
        with patch(
            "brain_v42.metrics.collector.get_settings",
            return_value=_settings(api_key="sk-server-secret"),
        ):
            collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
            _base_collector_stubs(collector)
            collector.collect_process_metrics = AsyncMock(  # type: ignore[method-assign]
                return_value={
                    "active_processes": 0,
                    "active_agents": 0,
                    "total_memory_rss_bytes": 0,
                    "tools": {},
                    "decay": {"stale_count": 0, "archived_count": 0, "access_log_size": 0},
                    "embedding": {
                        "total_requests": 0,
                        "total_errors": 0,
                        "gpu_busy_errors": 0,
                        "unreachable_errors": 0,
                        "recent_errors": 0,
                        "avg_latency_ms": 0.0,
                        "usage": {
                            "read": {"total_tokens": 0, "reported_requests": 0},
                            "write": {"total_tokens": 0, "reported_requests": 0},
                        },
                    },
                    "by_agent": {},
                    "embedding_identity": None,
                }
            )
            server = MetricsServer(collector, _mock_embedding_svc(), port=0, host="127.0.0.1")
            client = await aiohttp_client(server._build_app())
            resp = await client.get("/metrics")
            metrics = await resp.json()

        serialised = json.dumps(metrics)
        assert "sk-server-secret" not in serialised
        assert "SecretStr" not in serialised


# ---------------------------------------------------------------------------
# 4. The literal (3a4ed612) is gone.
# ---------------------------------------------------------------------------


class TestLiteralRemoved:
    def test_get_metrics_model_reflects_configured_model_not_the_old_literal(self) -> None:
        with patch(
            "brain_v42.metrics.collector.get_settings",
            return_value=_settings(model="a-completely-different-model"),
        ):
            collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
            metrics = collector.get_metrics()

        assert metrics["model"] == "a-completely-different-model"
        assert metrics["model"] != "Qodo-Embed-1-1.5B"
        assert metrics["embedding_service"]["model"] == "a-completely-different-model"
