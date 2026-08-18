"""Composition wiring and rollback facade tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_automation_builder_wires_typed_runtime_from_injected_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import brain_v42.automation.runtime as runtime_module
    from brain_v42.automation.ownership import OwnedGitLabIngestor, OwnedProjectKeyResolver
    from brain_v42.config import Settings

    builder = getattr(runtime_module, "build_automation_runtime", None)
    assert builder is not None, "automation needs a real production composition builder"
    engine = MagicMock(name="engine")
    lease = MagicMock(name="lease")
    embedding = MagicMock(name="embedding")
    reranker = MagicMock(name="reranker")
    cluster_guard = MagicMock(name="cluster_guard")
    ingestor = MagicMock(name="ingestor")
    dedup_job = MagicMock(name="dedup_job")
    server = MagicMock(name="server")
    lease_factory = MagicMock(return_value=lease)
    embedding_factory = MagicMock(return_value=embedding)
    reranker_factory = MagicMock(return_value=reranker)
    cluster_guard_factory = MagicMock(return_value=cluster_guard)
    ingestor_factory = MagicMock(return_value=ingestor)
    dedup_factory = MagicMock(return_value=dedup_job)
    server_factory = MagicMock(return_value=server)
    monkeypatch.setattr(runtime_module, "AutomationOwnershipLease", lease_factory)
    monkeypatch.setattr(runtime_module, "build_embedding_service", embedding_factory, raising=False)
    monkeypatch.setattr(runtime_module, "RerankerClient", reranker_factory, raising=False)
    monkeypatch.setattr(runtime_module, "ClusterGuard", cluster_guard_factory, raising=False)
    monkeypatch.setattr(runtime_module, "GitLabIngestor", ingestor_factory, raising=False)
    monkeypatch.setattr(runtime_module, "FeatureDedupJob", dedup_factory, raising=False)
    monkeypatch.setattr(runtime_module, "AutomationServer", server_factory)
    settings = Settings(
        postgres_url="postgresql+asyncpg://u:p@localhost:5433/brain_test",
        automation_host="127.0.0.1",
        automation_port=9301,
        automation_dedup_interval_seconds=19,
        gitlab_webhook_secret="secret",
        _env_file=None,  # type: ignore[call-arg]
    )

    runtime = builder(settings=settings, engine=engine)

    assert runtime._resources.engine is engine
    assert runtime._resources.lease is lease
    assert runtime._resources.embedding_svc is embedding
    assert runtime._resources.reranker is reranker
    assert runtime._resources.dedup_job is dedup_job
    assert runtime._resources.server is server
    assert runtime._dedup_interval == 19
    endpoint = server_factory.call_args.args[0]
    assert isinstance(endpoint._gitlab_ingestor, OwnedGitLabIngestor)
    assert isinstance(endpoint._project_key_resolver, OwnedProjectKeyResolver)
    assert endpoint._webhook_secret == "secret"
    server_factory.assert_called_once_with(endpoint, host="127.0.0.1", port=9301)


def test_metrics_main_exports_exact_runtime_rollback_aliases() -> None:
    import brain_v42.metrics.__main__ as metrics_main
    import brain_v42.metrics.runtime as metrics_runtime
    from brain_v42.automation.dedup import run_dedup_loop

    cleanup_loop = getattr(metrics_runtime, "run_cleanup_loop", None)
    processors = getattr(metrics_runtime, "build_sidecar_structlog_processors", None)

    assert metrics_main._dedup_loop is run_dedup_loop
    assert cleanup_loop is not None
    assert metrics_main._cleanup_loop is cleanup_loop
    assert processors is not None
    assert metrics_main.build_sidecar_structlog_processors is processors


def test_automation_builder_wires_one_lease_guard_through_webhook_mutations() -> None:
    """Both persistence layers must consult the runtime's single owner lease."""
    from brain_v42.automation.runtime import build_automation_runtime
    from brain_v42.config import Settings

    runtime = build_automation_runtime(
        settings=Settings(
            postgres_url="postgresql+asyncpg://u:p@localhost:5433/brain_test",
            gitlab_webhook_secret="secret",
            _env_file=None,  # type: ignore[call-arg]
        ),
        engine=MagicMock(name="engine"),
    )
    lease = runtime._resources.lease
    guarded = runtime._resources.server._webhook_endpoint._gitlab_ingestor
    ingestor = guarded._inner
    cluster_guard = ingestor._cluster_guard

    ingestor_guard = getattr(ingestor, "_mutation_guard", None)
    cluster_guard_callback = getattr(cluster_guard, "_mutation_guard", None)
    assert ingestor_guard == lease.ensure_owned
    assert cluster_guard_callback == lease.ensure_owned
    assert getattr(ingestor_guard, "__self__", None) is lease
    assert getattr(cluster_guard_callback, "__self__", None) is lease
