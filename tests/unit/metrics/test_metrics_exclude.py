"""Tests for opting out of large top-level metrics blocks."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.metrics.server import EXCLUDABLE_METRICS_KEYS, MetricsServer

# Production measured 40.9 KB total / 27.6 KB session_slots on 2026-10-12.
MIN_EXCLUDED_BODY_BYTES = 12 * 1024
MAX_EXCLUDED_BODY_BYTES = 24 * 1024


def _collector() -> MagicMock:
    collector = MagicMock()
    collector.get_metrics.return_value = {
        "version": "test",
        "tools": {},
        "embedding_service": {},
        "reranker": {},
        "graph": {},
        "decay": {},
        "model": "test",
    }
    collector.collect_db_stats = AsyncMock(return_value={"tables": {}})
    collector.collect_search_quality = AsyncMock(return_value={})
    collector.collect_process_metrics = AsyncMock(
        return_value={
            "active_processes": 0,
            "tools": {},
            "embedding": {},
            "decay": {},
            "mcp_auth_refused": {},
            "elevation_refused": {},
            "mcp_auth_agent_unresolved": {},
        }
    )
    collector.collect_dream_metrics = AsyncMock(return_value={"history": []})
    collector.collect_dream_promotions = AsyncMock(return_value={})
    collector.collect_nightly_ops = AsyncMock(return_value={"state": "ok"})
    collector.collect_ticket_counts = AsyncMock(return_value={"projects": []})
    collector.collect_session_slots = AsyncMock(return_value={"projects": []})
    return collector


def _server(collector: MagicMock) -> MetricsServer:
    return MetricsServer(
        collector,
        MagicMock(healthcheck=AsyncMock(return_value=True)),
    )


def _request(exclude: str | None = None) -> MagicMock:
    request = MagicMock()
    request.query = {} if exclude is None else {"exclude": exclude}
    return request


async def test_excluding_session_slots_skips_collection_and_preserves_other_keys() -> None:
    collector = _collector()
    server = _server(collector)
    default = json.loads((await server._handle_metrics(_request())).body)

    collector.collect_session_slots.reset_mock()
    excluded = json.loads((await server._handle_metrics(_request(" session_slots "))).body)

    assert "session_slots" not in excluded
    assert set(default) - {"session_slots"} <= set(excluded)
    collector.collect_session_slots.assert_not_awaited()


async def test_excluding_multiple_slow_blocks_skips_both() -> None:
    collector = _collector()
    response = await _server(collector)._handle_metrics(_request("session_slots,tickets"))
    data = json.loads(response.body)

    assert "session_slots" not in data
    assert "tickets" not in data
    collector.collect_session_slots.assert_not_awaited()
    collector.collect_ticket_counts.assert_not_awaited()


async def test_unknown_exclude_key_is_rejected_before_collector_work() -> None:
    collector = _collector()
    response = await _server(collector)._handle_metrics(_request("unknown"))

    assert response.status == 400
    data = json.loads(response.body)
    assert data["error"] == "unknown_exclude_key"
    assert data["keys"] == ["unknown"]
    assert data["allowed"] == sorted(data["allowed"])
    collector.get_metrics.assert_not_called()
    collector.collect_db_stats.assert_not_awaited()


async def test_empty_exclude_token_is_rejected() -> None:
    response = await _server(_collector())._handle_metrics(_request("session_slots,,tickets"))

    assert response.status == 400
    assert json.loads(response.body)["error"] == "empty_exclude_key"
    assert json.loads(response.body)["keys"] == [""]


async def test_every_default_top_level_key_is_excludable() -> None:
    # The allowed set is static: a block added to /metrics later must be added
    # to it too, or ?exclude=<that block> answers 400 for a key the payload has.
    response = await _server(_collector())._handle_metrics(_request())

    assert set(json.loads(response.body)) - EXCLUDABLE_METRICS_KEYS == set()


async def test_default_request_keeps_the_full_payload_keys() -> None:
    collector = _collector()
    response = await _server(collector)._handle_metrics(_request())
    data = json.loads(response.body)

    assert set(data) == {
        "version",
        "tools",
        "embedding_service",
        "reranker",
        "graph",
        "decay",
        "model",
        "receiver_rejections",
        "receiver_refused",
        "client_activity_registry",
        "database",
        "search_quality",
        "mcp_auth_refused",
        "elevation_refused",
        "mcp_auth_agent_unresolved",
        "search_rerank",
        "author_binding_candidates",
        "cross_process",
        "dream",
        "nightly",
        "tickets",
        "session_slots",
        "block_status",
    }


async def test_block_status_is_kept_when_slow_block_is_excluded() -> None:
    collector = _collector()
    response = await _server(collector)._handle_metrics(_request("session_slots"))
    data = json.loads(response.body)

    assert "session_slots" in data["block_status"]

    response = await _server(_collector())._handle_metrics(_request("block_status"))
    assert "block_status" not in json.loads(response.body)


@pytest.mark.parametrize("include_slots", [False, True])
async def test_representative_payload_size(include_slots: bool) -> None:
    collector = _collector()

    def tool_payload(index: int) -> dict[str, Any]:
        return {
            "calls": 10 + index,
            "errors": index % 2,
            "recent_errors": index % 3,
            "avg_latency_ms": 12.3 + index,
            "p50": 10.0 + index,
            "p95": 20.0 + index,
            "p99": 30.0 + index,
            "samples": 100 + index,
        }

    tools = {f"tool_{index}": tool_payload(index) for index in range(26)}
    by_agent = {
        str(uuid4()): {
            "calls": 100 + index,
            "errors": index,
            "recent_errors": index % 3,
            "avg_latency_ms": 12.3 + index,
        }
        for index in range(8)
    }
    embedding_usage = {
        direction: {"total_tokens": 10000 + index, "reported_requests": 50 + index}
        for index, direction in enumerate(("read", "write"))
    }
    cross_process = {
        "counters": {
            "epoch": "0123456789abcdef",
            "since": "2026-10-12T12:00:00+00:00",
            "processes": 8,
            "tool_latency_window_s": 300.0,
        },
        "active_processes": 8,
        "tools": {name: tool_payload(index) for index, name in enumerate(tools)},
        "by_agent": by_agent,
        "embedding": {
            "total_requests": 1,
            "total_errors": 0,
            "recent_errors": 0,
            "avg_latency_ms": 2.0,
            "usage": embedding_usage,
        },
        "decay": {"stale_count": 0, "archived_count": 0, "access_log_size": 0},
    }
    metrics = collector.get_metrics.return_value
    metrics.update(
        tools=tools,
        cross_process=cross_process,
        embedding_service={"usage": embedding_usage},
        reranker={
            "backend": "synthetic",
            "counters": {"requests": 500, "errors": 3, "fallbacks": 2},
            "by_identity": {
                "synthetic-model": {
                    "backend": "synthetic",
                    "model": "reranker-v1",
                    "operations_by_outcome": {"success": 400, "error": 2},
                    "attempts": 402,
                    "fallback_rate": 0.01,
                    "attempt_latency_ms": {"p50": 8.1, "p95": 20.2, "p99": 40.3},
                    "operation_latency_ms": {"p50": 10.1, "p95": 24.2, "p99": 45.3},
                }
            },
            "last_probe_status": "ok",
            "last_probe_at": "2026-10-12T12:00:00+00:00",
        },
    )
    collector.collect_process_metrics.return_value = cross_process.copy()
    collector.collect_ticket_counts.return_value = {
        "categories": [
            {"key": key, "label": label}
            for key, label in (
                ("todo", "To do"),
                ("to_confirm", "To confirm"),
                ("waiting", "Waiting"),
            )
        ],
        "projects": [
            {
                "project": f"project_{i}",
                "counts": {"todo": i + 1, "to_confirm": i % 4, "waiting": i % 3},
            }
            for i in range(28)
        ],
    }
    collector.collect_dream_metrics.return_value = {
        "history": [
            {
                "date": f"2026-10-{i + 1:02d}",
                "cost_usd": 0.1234 + i / 100,
                "tokens": 12000 + i * 100,
                "status": "success",
            }
            for i in range(10)
        ],
        "last_run": {
            "date": "2026-10-12",
            "status": "success",
            "phases_ok": 4,
            "phases_fail": 0,
            "total_cost_usd": 0.5678,
            "total_tokens": 45678,
            "total_duration_s": 123.4,
            "phases": {
                phase: {
                    "status": "success",
                    "model": "synthetic-model",
                    "duration_s": 20.5 + index,
                    "cost_usd": 0.1234 + index / 100,
                    "tokens": 12000 + index * 100,
                    "api_calls": 3 + index,
                    "tool_calls": 4 + index,
                    "error_message": None,
                    "dry_run": False,
                }
                for index, phase in enumerate(("extract", "roadmap", "sweep", "verify"))
            },
        },
    }
    # collect_dream_promotions returns the raw counts per type; the server
    # derives the published {"total", "by_type"} from them.
    collector.collect_dream_promotions.return_value = {
        kind: index + 1
        for index, kind in enumerate(
            ("decision", "learning", "snippet", "runbook", "adr", "ticket")
        )
    }
    collector.collect_session_slots.return_value = {
        "projects": [
            {
                "project": f"project_{project_index}",
                "base": {
                    "revision": 1,
                    "focus_updated_at": "2026-10-12T12:00:00+00:00",
                    "chars": 500,
                },
                "slots": [
                    {
                        "slot_id": str(uuid4()),
                        "title": f"Synthetic slot {slot_index}",
                        "revision": 2,
                        "opened_at": "2026-10-12T10:00:00+00:00",
                        "body_updated_at": "2026-10-12T12:00:00+00:00",
                        "anchors": [],
                        "bound_session_id": None,
                        "is_stale": False,
                        "receipt_pending": False,
                    }
                    for slot_index in range(project_index, 20, 16)
                ],
                "sessions": [
                    {
                        "session_id": str(uuid4()),
                        "slot_id": None,
                        "started_at": "2026-10-12T10:00:00+00:00",
                        "last_heartbeat_at": "2026-10-12T12:00:00+00:00",
                        "last_observed_at": "2026-10-12T12:00:00+00:00",
                        "is_stale": False,
                    }
                    for _ in range(project_index, 38, 16)
                ],
                "agent_traces_open": 0,
            }
            for project_index in range(16)
        ]
    }
    collector.collect_db_stats.return_value = {
        "tables": {
            name: {"rows": 100 + index, "embedding_coverage": 0.91, "null_embeddings": index}
            for index, name in enumerate(("decisions", "learnings", "snippets", "runbooks", "adrs"))
        },
        "project_contexts": {"rows": 28},
        "pool": {"active": 4, "idle": 3, "overflow": 0, "max": 10},
        "embedding_backlog": {
            "by_entity_type": {
                entity: {"count": 10 + index, "oldest_age_seconds": 300 + index * 10}
                for index, entity in enumerate(
                    ("decision", "learning", "snippet", "runbook", "adr")
                )
            },
            "worker_last_24h": {
                "claimed": 100,
                "completed": 97,
                "failed": 2,
                "retried": 1,
                "skipped": 3,
                "recovered": 1,
                "deferred": 2,
                "unavailable_by_kind": {"timeout": 1, "provider_error": 2},
            },
        },
        "graph_outbox": {
            "pending": 12,
            "projector": {
                "enabled": True,
                "status": "healthy",
                "last_run": "2026-10-12T12:00:00+00:00",
            },
        },
    }

    request = _request(None if include_slots else "session_slots")
    body = (await _server(collector)._handle_metrics(request)).body
    assert len(body) > 0
    data = json.loads(body)
    # Every representative block must reach the response: a fixture whose
    # collector double raises would silently drop its block and shrink the payload.
    for key in ("dream", "nightly", "tickets", "database", "cross_process", "reranker"):
        assert key in data, f"representative block {key!r} missing from the payload"
    if include_slots:
        assert "session_slots" in data
        assert len(body) > MAX_EXCLUDED_BODY_BYTES
    else:
        assert MIN_EXCLUDED_BODY_BYTES <= len(body) <= MAX_EXCLUDED_BODY_BYTES
        assert "session_slots" not in data


async def test_excluded_block_status_is_not_published() -> None:
    response = await _server(_collector())._handle_metrics(_request("block_status"))
    data: dict[str, Any] = json.loads(response.body)
    assert "block_status" not in data
