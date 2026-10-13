"""Pin unauthenticated metrics reads under operator decision 30cacb0b.

The schema literals below document every exposed field. Changing a literal is
a change of the exposure decision, including a rename or removal of a field.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from copy import deepcopy
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from aiohttp.test_utils import make_mocked_request

from brain_v42.config import Settings
from brain_v42.credentials.verifier import CredentialRefused
from brain_v42.metrics.brain_graph_server import BrainGraphMetricsServer
from brain_v42.metrics.client_activity import ClientActivityRegistry
from brain_v42.metrics.client_observation import ClientObservation
from brain_v42.metrics.collector import MetricsCollector
from brain_v42.repositories.pg_programme import VERSION, PgProgrammeRepo, published_programme

# Only these maps use runtime labels rather than structural field names. The
# wildcard in the literal pins EVERY value, without pinning deployment names.
DYNAMIC_MAPS = {
    "/metrics.tools": "tool names observed by the collector",
    "/metrics.cross_process.tools": "tool names observed across process rows",
    "/metrics.cross_process.by_agent": "declared agent identities in process rows",
    "/metrics.reranker.by_identity": "backend:model identities that served reranks",
    "/metrics.reranker.by_identity.*.operations_by_outcome": "observed operation outcomes",
    "/metrics.reranker.chain.served_by": "rerank link identities that served chain operations",
    "/metrics.graph.nodes_total": "labels returned by the graph service",
    "/metrics.graph.edges_total": "relationship types returned by the graph service",
    "/metrics.receiver_rejections.codex_logs": "HTTP statuses actually rejected",
    "/metrics.receiver_rejections.claude_logs": "HTTP statuses actually rejected",
    "/metrics.receiver_rejections.client_activity": "HTTP statuses actually rejected",
    "/metrics.receiver_refused": "credential refusal causes actually observed",
    "/metrics.mcp_auth_refused": "MCP refusal causes persisted by emitting processes",
    "/metrics.elevation_refused": "elevation refusal causes persisted by emitting processes",
    "/metrics.mcp_auth_agent_unresolved": "identity-resolution causes persisted by processes",
    "/metrics.author_binding_candidates": "tool:cause pairs persisted by emitting processes",
    "/metrics.database.embedding_backlog.worker_last_24h.unavailable_by_kind": (
        "unavailability kinds present in embedding_backfill timeseries rows"
    ),
    "/metrics.dream.last_run.phases": "phase names present in dream_runs",
    "/metrics.dream.promotions.by_type": "target types present in dream_promotions",
}

# Per-block evaluation health, kept apart from payloads that own status keys.
BLOCK_HEALTH = {"status": None, "error_type": None, "as_of": None, "last_ok_at": None}

# Per-tool stats in both tool maps; percentiles cover a window, counters do not.
TOOL_STATS = {
    "calls": None,
    "errors": None,
    "recent_errors": None,
    "avg_latency_ms": None,
    "p50": None,
    "p95": None,
    "p99": None,
    "samples": None,
}

# None denotes a JSON scalar (including null), [shape] a nonempty list whose
# EVERY item must match, and [] an intentionally empty, unimplemented section.
# Fixed vocabularies (tables, entity types, intents, receivers, killswitches,
# eviction causes and ticket categories) are deliberately NOT dynamic maps.
METRICS_SCHEMA = {
    "collected_at": None,
    "started_at": None,
    "version": None,
    "model": None,
    "embedding_dim": None,
    "uptime_seconds": None,
    "memory_rss_bytes": None,
    "embedding_service": {
        "status": None,
        "url": None,
        "avg_latency_ms": None,
        "total_requests": None,
        "total_errors": None,
        "gpu_busy_errors": None,
        "unreachable_errors": None,
        "recent_errors": None,
        "usage": {
            "read": {"total_tokens": None, "reported_requests": None},
            "write": {"total_tokens": None, "reported_requests": None},
        },
        "model": None,
        "backend": None,
        "endpoint_host": None,
        "models_seen": [None],
        "model_source": None,
    },
    "tools": {"*": TOOL_STATS},
    "search_quality": {
        "searches_total": None,
        "searches_with_zero_results": None,
        "avg_score": None,
    },
    "reranker": {
        "backend": None,
        "total_calls": None,
        "total_errors": None,
        "recent_errors": None,
        "total_candidates": None,
        "avg_latency_ms": None,
        "by_identity": {
            "*": {
                "backend": None,
                "model": None,
                "operations": None,
                "attempts": None,
                "retries": None,
                "status_429": None,
                "status_5xx": None,
                "budget_exhausted": None,
                "operations_by_outcome": {"*": None},
                "fallback_rate": None,
                "attempt_latency_ms": {"p50": None, "p95": None, "p99": None},
                "operation_latency_ms": {"p50": None, "p95": None, "p99": None},
            }
        },
        "chain": {
            "operations": None,
            "served_by": {"*": None},
            "handovers": None,
            "failed_all": None,
        },
        "last_probe_ok": None,
        "last_probe_reason": None,
        "last_probe_age_s": None,
    },
    "search_rerank": {
        "rerank_searches": None,
        "rerank_eligible": None,
        "rerank_rrf": None,
        "emitter_started_at": None,
    },
    "decay": {"stale_count": None, "archived_count": None, "access_log_size": None},
    "graph": {
        "status": None,
        "total_queries": None,
        "total_errors": None,
        "recent_errors": None,
        "avg_latency_ms": None,
        "nodes_total": {"*": None},
        "edges_total": {"*": None},
        "orphans_total": {
            "decisions": None,
            "learnings": None,
            "snippets": None,
            "runbooks": None,
            "adrs": None,
        },
        "generated_at": None,
    },
    "receiver_rejections": {
        "codex_logs": {"*": None},
        "claude_logs": {"*": None},
        "client_activity": {"*": None},
    },
    "receiver_refused": {"*": None},
    "client_activity_registry": {
        "evictions_total": {"ttl": None, "capacity": None},
        "evictions_bearing_total": None,
        "conversation_evictions_total": {
            "codex": {"ttl": None, "capacity": None},
            "claude": {"ttl": None, "capacity": None},
        },
        "conversation_evictions_bearing_total": {"codex": None, "claude": None},
        "occupancy": None,
        "capacity": None,
        "folded_observations_total": None,
    },
    "database": {
        "pool": {"active": None, "idle": None, "overflow": None, "max": None},
        "tables": {
            "decisions": {"rows": None, "embedding_coverage": None, "null_embeddings": None},
            "learnings": {"rows": None, "embedding_coverage": None, "null_embeddings": None},
            "snippets": {"rows": None, "embedding_coverage": None, "null_embeddings": None},
            "runbooks": {"rows": None, "embedding_coverage": None, "null_embeddings": None},
            "adrs": {"rows": None, "embedding_coverage": None, "null_embeddings": None},
            "project_contexts": {"rows": None},
        },
        "db_size_bytes": None,
        "dimension_mismatches": None,
        "embedding_backlog": {
            "total": None,
            "by_entity_type": {
                "decision": {"count": None, "oldest_age_seconds": None},
                "learning": {"count": None, "oldest_age_seconds": None},
                "snippet": {"count": None, "oldest_age_seconds": None},
                "runbook": {"count": None, "oldest_age_seconds": None},
                "adr": {"count": None, "oldest_age_seconds": None},
            },
            "worker_last_24h": {
                "attempted": None,
                "stored": None,
                "stale": None,
                "missing": None,
                "unavailable": None,
                "timed_out": None,
                "failed": None,
                "unavailable_by_kind": {"*": None},
            },
        },
        "graph_outbox": {
            "available": None,
            "pending": None,
            "ready": None,
            "claimed": None,
            "exhausted": None,
            "oldest_pending_age_seconds": None,
            "projector": {
                "generation": None,
                "armed": None,
                "lease_active": None,
                "recovery_active": None,
                "healthy": None,
            },
        },
    },
    "mcp_auth_refused": {"*": None},
    "elevation_refused": {"*": None},
    "mcp_auth_agent_unresolved": {"*": None},
    "author_binding_candidates": {"*": None},
    "cross_process": {
        "counters": {
            "epoch": None,
            "since": None,
            "processes": None,
            "tool_latency_window_s": None,
        },
        "active_processes": None,
        "active_agents": None,
        "total_memory_rss_bytes": None,
        "tools": {"*": TOOL_STATS},
        "embedding": {
            "total_requests": None,
            "total_errors": None,
            "gpu_busy_errors": None,
            "unreachable_errors": None,
            "recent_errors": None,
            "avg_latency_ms": None,
            "usage": {
                "read": {"total_tokens": None, "reported_requests": None},
                "write": {"total_tokens": None, "reported_requests": None},
            },
        },
        "by_agent": {
            "*": {"calls": None, "errors": None, "recent_errors": None, "avg_latency_ms": None}
        },
    },
    "dream": {
        "state": None,
        "last_run": {
            "date": None,
            "status": None,
            "judged": None,
            "scheduler_state": None,
            "phases_ok": None,
            "phases_fail": None,
            "total_cost_usd": None,
            "total_tokens": None,
            "total_duration_s": None,
            "phases": {
                "*": {
                    "status": None,
                    "model": None,
                    "duration_s": None,
                    "cost_usd": None,
                    "tokens": None,
                    "api_calls": None,
                    "tool_calls": None,
                    "error_message": None,
                    "dry_run": None,
                }
            },
            "promote_outcome": {
                "dry_run": None,
                "target_type": None,
                "target_id": None,
                "reason": None,
            },
        },
        "history": [{"date": None, "cost_usd": None, "tokens": None, "status": None}],
        "promotions": {"total": None, "by_type": {"*": None}},
        "generated_at": None,
    },
    "nightly": {
        "scheduler": {
            "state": None,
            "source": None,
            "timer": {"unit_file_state": None, "active_state": None, "next_elapse": None},
            "service": {"active_state": None},
            "error_type": None,
        },
        "dream_state": None,
        "killswitches_source": None,
        "killswitches": {
            "promote": None,
            "reorg": None,
            "reorg_dry": None,
            "extract": None,
            "extract_dry": None,
            "roadmap": None,
            "roadmap_dry": None,
            "sweep": None,
            "sweep_dry": None,
            "verify": None,
            "verify_dry": None,
        },
        "extract": {"proposed_pending": None},
        "last_failure": {
            "project_key": None,
            "run_date": None,
            "phase": None,
            "error": None,
            "created_at": None,
        },
        "generated_at": None,
    },
    "tickets": {
        "categories": [{"key": None, "label": None}],
        "projects": [
            {"project": None, "counts": {"todo": None, "to_confirm": None, "waiting": None}}
        ],
        "generated_at": None,
    },
    "session_slots": {
        "projects": [
            {
                "project": None,
                "base": {"revision": None, "focus_updated_at": None, "chars": None},
                "slots": [
                    {
                        "slot_id": None,
                        "title": None,
                        "revision": None,
                        "opened_at": None,
                        "body_updated_at": None,
                        "anchors": [{"kind": None, "ref": None}],
                        "bound_session_id": None,
                        "is_stale": None,
                        "receipt_pending": None,
                    }
                ],
                "sessions": [
                    {
                        "session_id": None,
                        "slot_id": None,
                        "started_at": None,
                        "last_heartbeat_at": None,
                        "last_observed_at": None,
                        "is_stale": None,
                    }
                ],
                "agent_traces_open": None,
            }
        ],
        "generated_at": None,
    },
    "block_status": dict.fromkeys(
        (
            "database",
            "search_quality",
            "cross_process",
            "dream",
            "nightly",
            "tickets",
            "session_slots",
            "graph_inventory",
        ),
        BLOCK_HEALTH,
    ),
}

COCKPIT_SCHEMA = {
    "version": None,
    "pid": None,
    "uptime_s": None,
    "endpoint": None,
    "metrics": {
        "rps": None,
        "p50": None,
        "p95": None,
        "p99": None,
        "err_rate": None,
        "cache_hit": None,
        "active_convs": None,
        "ctx_tokens": None,
        "memory_mb": None,
    },
    "activeConvs": [
        {
            "id": None,
            "topic": None,
            "agent": None,
            "started": None,
            "turns": None,
            "tokens": None,
            "model": None,
            "cost": None,
        }
    ],
    "clients": [
        {
            "id": None,
            "kind": None,
            "agent": None,
            "actor": None,
            "started": None,
            "last_seen_s": None,
            "model": None,
            "turns": None,
            "tokens": None,
            "cost": None,
            "brain_calls": None,
        }
    ],
    "tools": [{"name": None, "calls24h": None, "err": None, "p95": None, "lastErr": None}],
    "skills": [],
    "memory": {
        "episodes": None,
        "episodic_mb": None,
        "semantic_chunks": None,
        "semantic_mb": None,
        "lastCompaction": None,
        "freedLastCompaction": None,
        "vectorIndex": None,
    },
    "retrieval": {
        "p50": None,
        "p95": None,
        "hitRate": None,
        "rerankUsed": None,
        "queries24h": None,
    },
    "block_status": {
        "memory": BLOCK_HEALTH,
        "retrieval": BLOCK_HEALTH,
        "retrieval_log": BLOCK_HEALTH,
    },
    "latencyBuckets": [{"range": None, "count": None, "tone": None}],
    "cost": {
        "today": None,
        "yesterday": None,
        "week": None,
        "month": None,
        "byModel": [{"m": None, "v": None, "pct": None}],
    },
    "handoff": [],
    "rpsHistory": [None],
    "p95History": [None],
    "errHistory": [None],
    "costHistory": [None],
    "recent": [{"lvl": None, "msg": None, "t": None}],
}


# GET /programmes, decided with the frozen contract docs/contracts/programmes_v1.md
# (tickets 8a063256 and 24c1f172): declared one-line texts, gate keys, project keys
# and timestamps, no secret. Every optional object is filled below so the walk
# reaches its fields; the contract test and the sidecar integration test pin the
# semantics.
PROGRAMMES_SCHEMA = {
    "programmes": {
        "version": None,
        "generated_at": None,
        "programmes": [
            {
                "key": None,
                "title": None,
                "coordinator": None,
                "state": None,
                "last_change": {"gate": None, "from": None, "to": None, "at": None, "by": None},
                "gates": [
                    {
                        "key": None,
                        "title": None,
                        "owner": None,
                        "state": None,
                        "depends_on": [None],
                        "weight": None,
                        "waiting_on_operator": {"ask": None, "since": None},
                        "progress": {"done": None, "total": None, "unit": None},
                        "note": None,
                        "updated_at": None,
                        "updated_by": None,
                    }
                ],
            }
        ],
    }
}


def _assert_schema(value: Any, schema: Any, path: str, visited_dynamic_maps: set[str]) -> None:
    """Reject structural drift at any depth, including inside dynamic maps."""
    if isinstance(schema, dict):
        assert isinstance(value, dict), f"{path}: expected an object"
        if path in DYNAMIC_MAPS:
            assert set(schema) == {"*"}, f"{path}: dynamic value schema missing"
            assert value, f"{path}: populate the dynamic map to exercise its values"
            visited_dynamic_maps.add(path)
            for item in value.values():
                _assert_schema(item, schema["*"], f"{path}.*", visited_dynamic_maps)
        else:
            assert "*" not in schema, f"{path}: undocumented dynamic map"
            assert set(value) == set(schema), (
                f"{path}: keys changed; added={set(value) - set(schema)}, "
                f"removed={set(schema) - set(value)}"
            )
            for key, inner in schema.items():
                _assert_schema(value[key], inner, f"{path}.{key}", visited_dynamic_maps)
    elif isinstance(schema, list):
        assert isinstance(value, list), f"{path}: expected a list"
        if not schema:
            assert value == [], f"{path}: the empty section now exposes data"
        else:
            assert value, f"{path}: populate the list to exercise its items"
            for item in value:
                _assert_schema(item, schema[0], f"{path}[]", visited_dynamic_maps)
    else:
        assert value is None or isinstance(value, str | int | float | bool), (
            f"{path}: a scalar became a container"
        )


NOW = datetime(2026, 10, 13, 0, tzinfo=UTC)
SLOT_ID = UUID("10000000-0000-0000-0000-000000000001")
SESSION_ID = UUID("10000000-0000-0000-0000-000000000002")
TICKET_ID = UUID("10000000-0000-0000-0000-000000000003")
REPO_ROOT = Path(__file__).resolve().parents[3]


def _result(rows: list[Any]) -> MagicMock:
    """Match the existing session doubles without replacing payload builders."""
    result = MagicMock()
    result.all.return_value = rows
    result.fetchall.return_value = rows
    result.first.return_value = rows[0] if rows else None
    result.one.return_value = rows[0] if rows else None
    scalar = rows[0][0] if rows and isinstance(rows[0], tuple) else None
    result.scalar.return_value = scalar
    result.scalar_one.return_value = scalar
    mappings = MagicMock()
    mappings.all.return_value = rows
    mappings.__iter__.side_effect = lambda: iter(rows)
    result.mappings.return_value = mappings
    return result


class _DatabaseRows:
    """Return populated raw DB rows so real collectors own every exposed key."""

    def __init__(self, process_rows: list[Any]) -> None:
        self.process_rows = process_rows
        self.unhandled: list[str] = []

    def execute(self, statement: Any, _params: Any = None) -> MagicMock:
        sql = " ".join(str(statement).split())
        if "FROM process_metrics" in sql:
            return _result(deepcopy(self.process_rows))
        if "FROM graph_outbox" in sql:
            return _result([(1, 1, 0, 0, 2.0, 1, True, True, False)])
        if "COUNT(embedding)" in sql:
            return _result([(10, 8, 2, 60.0)])
        if "vector_dims(embedding)" in sql:
            return _result([(0,)])
        if "pg_database_size" in sql:
            return _result([(1024 * 1024,)])
        if "embedding_backfill.%" in sql:
            return _result(
                [
                    ("embedding_backfill.attempted", 3),
                    ("embedding_backfill.unavailable.provider_busy", 1),
                ]
            )
        if "FROM search_log" in sql:
            return _result([(4, 1, 0.8)])
        if "DISTINCT ON (phase)" in sql:
            return _result(
                [
                    (
                        "promote",
                        "test-model",
                        "done",
                        2.0,
                        20,
                        10,
                        0,
                        0,
                        0.01,
                        1,
                        2,
                        date(2026, 10, 12),
                        None,
                        False,
                    )
                ]
            )
        if "WITH latest AS" in sql:
            return _result([(date(2026, 10, 12), 0.01, 30, "success")])
        if "SELECT dp.target_type" in sql:
            return _result([("adr", TICKET_ID, None)])
        if "GROUP BY target_type" in sql:
            return _result([("adr", 1), ("runbook", 2)])
        if "FROM ticket_extraction_proposals" in sql:
            return _result([(2,)])
        if "SELECT project_key, run_date, phase" in sql:
            return _result([("sample-project", date(2026, 10, 12), "extract", "timeout", NOW)])
        if "AS todo" in sql and "AS waiting" in sql:
            return _result(
                [{"project": "sample-project", "todo": 1, "to_confirm": 2, "waiting": 3}]
            )
        if "focus_slot_anchors.slot_id" in sql:
            return _result(
                [
                    {
                        "slot_id": SLOT_ID,
                        "kind": "ticket",
                        "ticket_id": TICKET_ID,
                        "target_release": None,
                        "repository_id": None,
                        "pr_number": None,
                        "completing_row_id": None,
                    }
                ]
            )
        if sql.startswith("SELECT focus_slots.id"):
            return _result(
                [
                    {
                        "id": SLOT_ID,
                        "project_key": "sample-project",
                        "title": "Example focus",
                        "revision": 1,
                        "opened_at": NOW,
                        "body_updated_at": NOW,
                        "bound_session_id": SESSION_ID,
                        "last_bound_ended_at": None,
                    }
                ]
            )
        if sql.startswith("SELECT brain_sessions.id"):
            return _result(
                [
                    {
                        "id": SESSION_ID,
                        "project_key": "sample-project",
                        "slot_id": SLOT_ID,
                        "started_at": NOW,
                        "last_heartbeat_at": NOW,
                        "last_observed_at": NOW,
                    }
                ]
            )
        if "AS open_traces" in sql:
            return _result([{"project_key": "sample-project", "open_traces": 1}])
        if "project_contexts.focus_revision" in sql:
            return _result(
                [
                    {
                        "project_key": "sample-project",
                        "focus_revision": 3,
                        "focus_updated_at": NOW,
                        "chars": 100,
                    }
                ]
            )
        if "FROM pg_class" in sql:
            return _result([(10, 1024 * 1024)])
        if "FROM consolidation_log" in sql:
            return _result([(NOW, 2)])
        if "FROM pg_indexes" in sql:
            return _result([("USING hnsw WITH (m='16', ef_construction='64')",)])
        if "SELECT value FROM metrics_timeseries" in sql:
            return _result([(1.0,), (2.0,)])
        if sql.startswith("SELECT COUNT(*) FROM "):
            return _result([(10,)])
        self.unhandled.append(sql)
        raise AssertionError(f"unhandled metrics source query: {sql}")


@pytest.fixture(params=["local", "cross_process"])
def populated_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[BrainGraphMetricsServer]:
    """Populate all real sections; fake only DB rows and external services."""
    settings = Settings(
        postgres_url="postgresql+asyncpg://u:p@localhost:1/brain_test",
        embedding_service_url="http://localhost:8003",
        _env_file=None,
    )
    monkeypatch.setattr("brain_v42.metrics.collector.get_settings", lambda: settings)
    monkeypatch.setattr("brain_v42.metrics.cockpit.get_settings", lambda: settings)

    # Reuse the session-double pattern from test_metrics_cockpit_collector.py.
    session = AsyncMock()
    session.__aenter__.return_value = session
    factory = MagicMock(return_value=session)
    engine = MagicMock()
    pool = engine.sync_engine.pool
    pool.size.return_value = 5
    pool.checkedout.return_value = 1
    pool.checkedin.return_value = 4
    pool.overflow.return_value = 0
    pool._max_overflow = 5
    collector = MetricsCollector(engine=engine, session_factory=factory)
    collector.record_tool_call("brain_search", 10.0, agent="sample-agent")
    collector.record_tool_call("brain_get", 20.0, agent="other-agent")
    collector.record_search_latency(10.0)
    collector.record_cost("test-model", 0.01)
    collector.push_recent_log("info", "Example activity")
    collector.record_embedding_request(10.0)
    collector.record_embedding_usage("read", 10, 1)
    collector.record_embedding_usage("write", 20, 1)
    collector.record_rerank_backend("shim")
    collector.record_reranker_call(10.0, 2)
    collector.record_rerank_attempt("shim:test-model", "ok", 10.0)
    collector.record_rerank_operation("shim:test-model", "ok", 10.0)
    collector.record_rerank_probe("shim:test-model", True, "ok")
    collector.on_handover()
    collector.on_chain("fallback", "shim:test-model")
    collector.record_search_rerank(eligible=True, rrf=False)
    collector.record_graph_query(10.0)
    collector.record_decay_stats(1, 2, 3)

    # Raw process_metrics JSONB mirrors the flusher's persisted representation;
    # collect_process_metrics and both route handlers still run unmodified.
    flush = collector.get_flush_data()
    process = flush.pop("_process")
    reranker = process["reranker"]
    process_tools = {
        "_reranker": {
            "calls": reranker["total_calls"],
            "errors": reranker["total_errors"],
            "recent_errors": reranker["recent_errors"],
            "total_latency": reranker["total_latency"],
            "total_candidates": reranker["total_candidates"],
            "backend": reranker["backend"],
            "by_identity": reranker["by_identity"],
            "last_probe": reranker["last_probe"]["shim:test-model"],
            "chain": reranker["chain"],
        },
        "_search_rerank": process["search"],
        "_graph": {"calls": 1, "errors": 0, "recent_errors": 0, "total_latency": 10.0},
        "_decay": process["decay"],
        "_mcp_auth_refused": {"missing_token": 1},
        "_elevation_refused": {"not_allowed": 1},
        "_mcp_auth_agent_unresolved": {"missing_agent": 1},
        "_author_binding_candidates": {"brain_ticket_create:unauthenticated": 1},
    }
    # Exercise both sides of the handler's override: local collector fields
    # must stay pinned even when live process rows would normally replace them.
    is_live = request.param == "cross_process"
    if not is_live:
        process_tools.pop("_reranker")
        process_tools.pop("_graph")
    rows = [("_process", 123, NOW, NOW, process_tools, process["embedding"], 1024, is_live)]
    rows.extend(
        (agent, 123, NOW, NOW, data["tools"], {}, 0, is_live) for agent, data in flush.items()
    )
    database = _DatabaseRows(rows)
    session.execute.side_effect = database.execute

    killswitches = tmp_path / "killswitches.conf"
    killswitches.write_text(
        "[Service]\n"
        "Environment=BRAIN_DREAM_PROMOTE_ENABLED=true BRAIN_DREAM_REORG_ENABLED=true\n"
        "Environment=BRAIN_DREAM_REORG_DRY_RUN=true\n"
        "Environment=BRAIN_DREAM_EXTRACT_ENABLED=true BRAIN_DREAM_EXTRACT_DRY_RUN=true\n"
        "Environment=BRAIN_DREAM_ROADMAP_ENABLED=true BRAIN_DREAM_ROADMAP_DRY_RUN=true\n"
        "Environment=BRAIN_DREAM_SWEEP_ENABLED=true BRAIN_DREAM_SWEEP_DRY_RUN=true\n"
        "Environment=BRAIN_DREAM_VERIFY_ENABLED=true BRAIN_DREAM_VERIFY_DRY_RUN=true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("brain_v42.metrics.collector_nightly.KILLSWITCHES_PATH", killswitches)

    # The programme rows a database would return, through the real serializer.
    programme_row = {
        "key": "brain-move",
        "title": "Brain to red-base",
        "coordinator_project": "brain-v42",
        "state": "active",
        "last_change_gate": "G7",
        "last_change_from": "todo",
        "last_change_to": "in_progress",
        "last_change_at": NOW,
        "last_change_by": "red-backup",
    }
    gate_row = {
        "key": "G7",
        "title": "red-backup deployed on red-base",
        "owner_project": "red-backup",
        "state": "in_progress",
        "depends_on": ["G6"],
        "weight": 4,
        "waiting_ask": "validate spec",
        "waiting_since": NOW,
        "progress_done": 6,
        "progress_total": 15,
        "progress_unit": "tasks",
        "note": "first deploy",
        "updated_at": NOW,
        "updated_by": "red-backup",
    }

    async def published_block(_repo: PgProgrammeRepo) -> dict[str, Any]:
        return {"version": VERSION, "programmes": [published_programme(programme_row, [gate_row])]}

    monkeypatch.setattr(PgProgrammeRepo, "published_block", published_block)

    # Reuse the actual telemetry fixture and merged-registry setup from
    # test_metrics_cockpit_collector.py, including the legacy conversation row.
    registry = ClientActivityRegistry(secret=b"\x04" * 32)
    registry.ingest_otlp_json((REPO_ROOT / "tests/fixtures/codex_otlp_logs.json").read_bytes())
    registry.record_observations((ClientObservation(actor="codex", session_id=None, calls=5),))
    graph = AsyncMock()
    graph.healthcheck.return_value = True
    graph.count_nodes_by_label.return_value = {"Decision": 5, "Learning": 3}
    graph.count_edges_by_type.return_value = {"RELATED_TO": 2, "SUPPORTS": 1}
    embedding = AsyncMock()
    embedding.healthcheck.return_value = True
    verifier = AsyncMock()
    verifier.verify.side_effect = CredentialRefused("missing_token")
    server = BrainGraphMetricsServer(
        collector=collector,
        embedding_svc=embedding,
        graph_svc=graph,
        graph_projection_svc=AsyncMock(),
        codex_registry=registry,
        host="0.0.0.0",
        allow_non_loopback=True,
        receiver_auth="credentials",
        credential_verifier=verifier,
    )
    for receiver in ("codex_logs", "claude_logs", "client_activity"):
        server._rejection_counters.increment(receiver, 413)
    server._receiver_error(401, receiver="codex_logs", reason="missing_token")
    yield server
    assert not database.unhandled, database.unhandled


async def test_exact_recursive_read_exposure(populated_server: BrainGraphMetricsServer) -> None:
    visited_dynamic_maps: set[str] = set()
    for path, handler, schema in (
        ("/metrics", "_handle_metrics", METRICS_SCHEMA),
        ("/api/cockpit", "_handle_cockpit", COCKPIT_SCHEMA),
        ("/programmes", "_handle_programmes", PROGRAMMES_SCHEMA),
    ):
        response = await getattr(populated_server, handler)(make_mocked_request("GET", path))
        assert response.status == 200
        assert response.content_type == "application/json"
        _assert_schema(json.loads(response.body), schema, path, visited_dynamic_maps)
    assert visited_dynamic_maps == set(DYNAMIC_MAPS), "dynamic maps differ from the schema walk"


async def test_only_decided_read_routes_are_credential_free(
    populated_server: BrainGraphMetricsServer, aiohttp_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the four public reads while credentialed POST receivers stay closed.

    Graph rejection before loading data is pinned separately by
    test_brain_graph_server.py::test_graph_route_rejects_non_loopback_peer_before_loading_data.
    """
    monkeypatch.setattr("brain_v42.metrics.server._has_loopback_tcp_peer", lambda _: False)
    monkeypatch.setattr(
        "brain_v42.metrics.brain_graph_server._has_loopback_tcp_peer", lambda _: False
    )
    app = populated_server._build_app()
    assert {route.resource.canonical for route in app.router.routes() if route.method == "GET"} == {
        "/metrics",
        "/api/cockpit",
        "/programmes",
        "/healthz",
        "/api/brain-graph/v1",
    }
    client = await aiohttp_client(app)
    allowed = set()
    for path in ("/metrics", "/api/cockpit", "/programmes", "/healthz", "/api/brain-graph/v1"):
        response = await client.get(path)
        if response.status == 200:
            allowed.add(path)
    assert allowed == {"/metrics", "/api/cockpit", "/programmes", "/healthz"}
    for path in ("/v1/logs", "/v1/logs/claude", "/v1/client-activity"):
        response = await client.post(path, json={})
        assert response.status == 401
        assert await response.json() == {"reason": "missing_token"}


@pytest.mark.parametrize(
    ("mutation", "container"), [("added", "dynamic"), ("added", "list"), ("removed", "dict")]
)
def test_recursive_guard_rejects_field_changes(mutation: str, container: str) -> None:
    """Prove the pin rejects drift inside list items and dynamic-map values too."""
    item = {"calls": 1}
    schema: Any = {"calls": None}
    value: Any = item
    path = "/probe"
    if container == "list":
        value, schema = [item], [schema]
    elif container == "dynamic":
        value, schema, path = {"any-tool-name": item}, {"*": schema}, "/metrics.tools"
    if mutation == "added":
        item["unreviewed"] = 1
    else:
        del item["calls"]
    with pytest.raises(AssertionError, match="keys"):
        _assert_schema(value, schema, path, set())
