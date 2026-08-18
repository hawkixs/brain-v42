"""CLI contract for the bounded embedding backlog worker."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.maintenance.embedding_backfill import build_linkers, parse_args, run_from_args
from brain_v42.services.durable_graph_service import DurableGraphService


def test_parse_args_defaults_to_safe_dry_run() -> None:
    args = parse_args([])

    assert args.execute is False
    assert args.entity_types is None
    assert args.batch_size == 20
    assert args.limit == 200
    assert args.project_key is None


def test_parse_args_accepts_execute_filters_and_bounds() -> None:
    args = parse_args(
        [
            "--execute",
            "--entity-type",
            "decision",
            "--entity-type",
            "adr",
            "--batch-size",
            "10",
            "--limit",
            "50",
            "--project-key",
            "brain-v42",
        ]
    )

    assert args.execute is True
    assert args.entity_types == ["decision", "adr"]
    assert args.batch_size == 10
    assert args.limit == 50
    assert args.project_key == "brain-v42"


def test_parse_args_canonicalizes_project_key_alias() -> None:
    args = parse_args(["--project-key", "brain_v42"])

    assert args.project_key == "brain-v42"


@pytest.mark.parametrize("args", [["--batch-size", "0"], ["--batch-size", "101"], ["--limit", "0"]])
def test_parse_args_rejects_invalid_bounds(args: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        parse_args(args)

    assert exc.value.code == 2


def test_build_linkers_matches_canonical_service_wiring() -> None:
    settings = SimpleNamespace(
        reranker_url="http://reranker",
        reranker_timeout=3.0,
        neo4j_url="bolt://neo4j",
        neo4j_user="neo4j",
        neo4j_password="secret",
        graph_enabled=True,
        graph_ledger_write_enabled=False,
        neo4j_timeout=4.0,
    )
    session_factory = MagicMock()
    embedding_svc = MagicMock()

    with (
        patch("brain_v42.maintenance.embedding_backfill.RerankerClient") as reranker_cls,
        patch("brain_v42.maintenance.embedding_backfill.StatusEngine") as status_cls,
        patch("brain_v42.maintenance.embedding_backfill.ClusterGuard") as guard_cls,
        patch("brain_v42.maintenance.embedding_backfill.FeatureLinker") as feature_cls,
        patch("brain_v42.maintenance.embedding_backfill.create_neo4j_driver") as driver_factory,
        patch("brain_v42.maintenance.embedding_backfill.GraphService") as graph_cls,
        patch("brain_v42.maintenance.embedding_backfill.AutoLinker") as auto_cls,
    ):
        dependencies = build_linkers(settings, session_factory, embedding_svc)

    guard_cls.assert_called_once_with(
        session_factory=session_factory,
        embedding_svc=embedding_svc,
        reranker=reranker_cls.return_value,
        status_engine=status_cls.return_value,
    )
    feature_cls.assert_called_once_with(
        session_factory=session_factory,
        cluster_guard=guard_cls.return_value,
    )
    driver_factory.assert_called_once_with(
        url="bolt://neo4j",
        user="neo4j",
        password="secret",
        enabled=True,
    )
    graph_cls.assert_called_once_with(driver_factory.return_value, timeout=4.0)
    auto_cls.assert_called_once_with(
        session_factory=session_factory,
        graph=graph_cls.return_value,
    )
    assert dependencies.feature_linker is feature_cls.return_value
    assert dependencies.auto_linker is auto_cls.return_value


def test_build_linkers_routes_auto_linker_writes_through_durable_graph() -> None:
    settings = SimpleNamespace(
        reranker_url="http://reranker",
        reranker_timeout=3.0,
        neo4j_url="bolt://neo4j",
        neo4j_user="neo4j",
        neo4j_password="secret",
        graph_enabled=True,
        graph_ledger_write_enabled=True,
        graph_outbox_interval_seconds=5,
        graph_outbox_batch_size=100,
        graph_outbox_max_attempts=10,
        neo4j_timeout=4.0,
    )
    session_factory = MagicMock()
    embedding_svc = MagicMock()

    with (
        patch("brain_v42.maintenance.embedding_backfill.RerankerClient"),
        patch("brain_v42.maintenance.embedding_backfill.StatusEngine"),
        patch("brain_v42.maintenance.embedding_backfill.ClusterGuard"),
        patch("brain_v42.maintenance.embedding_backfill.FeatureLinker"),
        patch("brain_v42.maintenance.embedding_backfill.create_neo4j_driver"),
        patch("brain_v42.maintenance.embedding_backfill.GraphService") as graph_cls,
        patch("brain_v42.maintenance.embedding_backfill.AutoLinker") as auto_cls,
    ):
        build_linkers(settings, session_factory, embedding_svc)

    graph = auto_cls.call_args.kwargs["graph"]
    assert isinstance(graph, DurableGraphService)
    assert graph._graph is graph_cls.return_value


@pytest.mark.asyncio
async def test_execute_asserts_durable_ledger_schema_before_job_writes() -> None:
    events: list[str] = []

    async def assert_schema_ready() -> None:
        events.append("schema_ready")

    async def run_job(**_kwargs: object) -> SimpleNamespace:
        events.append("job_write")
        return SimpleNamespace(has_failures=False, metrics_persisted=False)

    ledger = SimpleNamespace(assert_schema_ready=AsyncMock(side_effect=assert_schema_ready))
    linkers = SimpleNamespace(
        feature_linker=MagicMock(),
        auto_linker=MagicMock(),
        graph_ledger=ledger,
        close=AsyncMock(),
    )
    embedding_svc = MagicMock()
    embedding_svc.close = AsyncMock()
    job = MagicMock()
    job.run = AsyncMock(side_effect=run_job)
    args = SimpleNamespace(
        execute=True,
        entity_types=None,
        batch_size=20,
        limit=200,
        project_key=None,
    )

    with (
        patch(
            "brain_v42.maintenance.embedding_backfill.get_settings",
            return_value=SimpleNamespace(embedding_service_url="http://embedding"),
        ),
        patch("brain_v42.maintenance.embedding_backfill.get_session_factory"),
        patch(
            "brain_v42.maintenance.embedding_backfill.build_embedding_service",
            return_value=embedding_svc,
        ),
        patch(
            "brain_v42.maintenance.embedding_backfill.build_linkers",
            return_value=linkers,
        ),
        patch(
            "brain_v42.maintenance.embedding_backfill.EmbeddingBackfillJob",
            return_value=job,
        ),
        patch(
            "brain_v42.maintenance.embedding_backfill.persist_backfill_metrics",
            new=AsyncMock(return_value=True),
        ),
        patch("brain_v42.maintenance.embedding_backfill.asdict", return_value={}),
        patch(
            "brain_v42.maintenance.embedding_backfill.dispose_engine",
            new=AsyncMock(),
        ),
    ):
        exit_code = await run_from_args(args)

    assert events == ["schema_ready", "job_write"]
    ledger.assert_schema_ready.assert_awaited_once_with()
    assert exit_code == 0
