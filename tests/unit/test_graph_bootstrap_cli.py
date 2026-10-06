"""Pin restore bootstrap identity and fail-closed recovery behavior."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid5

import pytest
from pydantic import SecretStr

from brain_v42.maintenance.graph_projection_recovery import recover_projection_lineage
from brain_v42.repositories.pg_graph_ledger import (
    ProjectionRecoveryLease,
    ProjectionRecoveryPreparation,
)
from brain_v42.scripts import graph_bootstrap as cli

DUMP_SHA256 = "a1" * 32
MARKER = f"brain-v42-restore sha256={DUMP_SHA256} at=2026-10-06T18:00:00Z"


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=[MARKER, None])
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    repo = MagicMock()
    repo.assert_schema_ready = AsyncMock()
    repo.projection_inventory = AsyncMock()
    repo.prepare_projection_recovery = AsyncMock(return_value=None)
    driver = MagicMock()
    driver.verify_connectivity = AsyncMock()
    settings = SimpleNamespace(
        graph_projector_enabled=True,
        graph_projector_neo4j_url="bolt://127.0.0.1:7687",
        graph_projector_neo4j_user="projector",
        graph_projector_neo4j_password=SecretStr(""),
        neo4j_timeout=3.0,
    )
    recover = AsyncMock()
    writer = MagicMock()
    logger = MagicMock()
    close = AsyncMock()
    dispose = AsyncMock()
    create_driver = MagicMock(return_value=driver)
    ensure_schema = AsyncMock()
    monkeypatch.setattr(cli, "get_session_factory", lambda: factory)
    monkeypatch.setattr(cli, "PgGraphLedgerRepo", lambda _factory: repo)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "create_neo4j_driver", create_driver)
    monkeypatch.setattr(cli, "Neo4jGraphProjectionWriter", lambda *_args, **_kwargs: writer)
    monkeypatch.setattr(cli, "ensure_graph_projection_schema", ensure_schema)
    monkeypatch.setattr(cli, "recover_projection_lineage", recover)
    monkeypatch.setattr(cli, "close_neo4j_driver", close)
    monkeypatch.setattr(cli, "dispose_engine", dispose)
    monkeypatch.setattr(cli, "logger", logger)
    return SimpleNamespace(
        session=session,
        repo=repo,
        driver=driver,
        settings=settings,
        recover=recover,
        writer=writer,
        logger=logger,
        close=close,
        dispose=dispose,
        create_driver=create_driver,
        ensure_schema=ensure_schema,
    )


@pytest.fixture
def live_recovery(runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    recovery_id = uuid5(cli.BOOTSTRAP_NAMESPACE, DUMP_SHA256)
    worker_id = "graph-bootstrap-" + str(uuid5(cli.BOOTSTRAP_NAMESPACE, str(recovery_id)))
    lease = ProjectionRecoveryLease(
        recovery_id=recovery_id,
        owner_id=worker_id,
        generation=7,
        lease_until=datetime.now(UTC) + timedelta(seconds=600),
        phase="prepared",
    )
    ready = replace(lease, phase="neo_ready")
    active = SimpleNamespace(recovery_id=recovery_id, owner_id=worker_id, lease=lease, ready=ready)

    def prepare(
        recovery_id: UUID, worker_id: str, *, lease_seconds: int
    ) -> ProjectionRecoveryPreparation | None:
        if recovery_id != active.recovery_id or worker_id != active.owner_id:
            return None
        return ProjectionRecoveryPreparation(status="resumed", lease=active.lease, requeued=None)

    runtime.repo.prepare_projection_recovery.side_effect = prepare
    monkeypatch.setattr(cli, "recover_projection_lineage", recover_projection_lineage)
    runtime.repo.projection_inventory.return_value = SimpleNamespace(
        entity_count=2, relation_count=1, pending_count=3
    )
    runtime.writer.reset_for_recovery = AsyncMock(
        return_value=SimpleNamespace(accepted=True, deleted_nodes=2)
    )
    runtime.repo.mark_projection_recovery_neo_ready = AsyncMock(return_value=ready)
    runtime.writer.finalize_recovery = AsyncMock(return_value=SimpleNamespace(accepted=True))
    runtime.repo.finalize_projection_recovery = AsyncMock(return_value=True)
    return active


def test_no_marker_fails_without_attempting_recovery(runtime: SimpleNamespace) -> None:
    runtime.session.scalar.side_effect = [None]

    assert cli.main([]) != 0

    runtime.logger.error.assert_called_once_with("no_restore_marker")
    runtime.recover.assert_not_awaited()
    runtime.create_driver.assert_not_called()
    runtime.dispose.assert_awaited_once()
    query = str(runtime.session.scalar.call_args.args[0])
    assert "shobj_description(oid, 'pg_database')" in query
    assert "datname = current_database()" in query


def test_completed_marker_ignores_live_projection_lease(runtime: SimpleNamespace) -> None:
    recovery_id = uuid5(cli.BOOTSTRAP_NAMESPACE, DUMP_SHA256)
    runtime.session.scalar.side_effect = [MARKER, recovery_id]
    runtime.repo.prepare_projection_recovery.side_effect = AssertionError("live lease")

    assert cli.main([]) == 0

    runtime.repo.prepare_projection_recovery.assert_not_awaited()
    runtime.recover.assert_not_awaited()
    runtime.create_driver.assert_not_called()
    completed_query = str(runtime.session.scalar.call_args.args[0])
    assert "last_completed_recovery_id" in completed_query
    assert "leased_until" not in completed_query


def test_fresh_marker_recovers_with_stable_ids_and_default_lease(runtime: SimpleNamespace) -> None:
    assert cli.main([]) == 0

    recovery_id = uuid5(cli.BOOTSTRAP_NAMESPACE, DUMP_SHA256)
    worker_id = "graph-bootstrap-" + str(uuid5(cli.BOOTSTRAP_NAMESPACE, str(recovery_id)))
    runtime.recover.assert_awaited_once_with(
        runtime.repo,
        runtime.writer,
        recovery_id=recovery_id,
        worker_id=worker_id,
        lease_seconds=600,
    )
    runtime.driver.verify_connectivity.assert_awaited_once()
    runtime.ensure_schema.assert_awaited_once_with(runtime.driver)
    runtime.close.assert_awaited_once_with(runtime.driver)
    runtime.dispose.assert_awaited_once()


def test_same_marker_on_retry_keeps_both_ids(runtime: SimpleNamespace) -> None:
    runtime.session.scalar.side_effect = [MARKER, None, MARKER, None]

    assert cli.main([]) == 0
    assert cli.main([]) == 0

    assert runtime.recover.await_args_list[0] == runtime.recover.await_args_list[1]


@pytest.mark.parametrize("value", ["60", "86400", "1200"])
def test_explicit_lease_is_passed_to_recovery(runtime: SimpleNamespace, value: str) -> None:
    assert cli.main(["--lease-seconds", value]) == 0
    assert runtime.recover.await_args.kwargs["lease_seconds"] == int(value)


@pytest.mark.parametrize("value", ["0", "59", "86401", "not-an-int"])
def test_out_of_bounds_lease_is_rejected(runtime: SimpleNamespace, value: str) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--lease-seconds", value])
    assert exc.value.code == 2
    runtime.session.scalar.assert_not_awaited()


@pytest.mark.parametrize(
    "marker",
    [
        "",
        "unrelated comment",
        MARKER.replace(DUMP_SHA256, "abc"),
        MARKER.replace("2026-10-06", "2026-99-99"),
        MARKER.replace("Z", "+02:00"),
        MARKER + " extra",
    ],
)
def test_invalid_marker_fails_closed(runtime: SimpleNamespace, marker: str) -> None:
    runtime.session.scalar.side_effect = [marker]
    assert cli.main([]) != 0
    runtime.logger.error.assert_called_once_with("no_restore_marker")
    runtime.recover.assert_not_awaited()


@pytest.mark.parametrize("conflict", ["other-worker", "other-recovery", "live-projector"])
def test_refused_live_lease_returns_nonzero(
    runtime: SimpleNamespace,
    live_recovery: SimpleNamespace,
    conflict: str,
) -> None:
    if conflict == "other-worker":
        live_recovery.owner_id = "other-worker"
    elif conflict == "other-recovery":
        live_recovery.recovery_id = uuid5(cli.BOOTSTRAP_NAMESPACE, "b2" * 32)
    else:
        live_recovery.recovery_id = None
        live_recovery.owner_id = "mcp-projector"

    assert cli.main([]) != 0

    runtime.repo.prepare_projection_recovery.assert_awaited_once_with(
        live_recovery.lease.recovery_id,
        live_recovery.lease.owner_id,
        lease_seconds=600,
    )
    runtime.writer.reset_for_recovery.assert_not_called()
    runtime.repo.mark_projection_recovery_neo_ready.assert_not_awaited()
    runtime.writer.finalize_recovery.assert_not_awaited()
    runtime.repo.finalize_projection_recovery.assert_not_awaited()
    runtime.close.assert_awaited_once_with(runtime.driver)
    runtime.dispose.assert_awaited_once()


def test_same_recovery_and_worker_resume_live_lease(
    runtime: SimpleNamespace, live_recovery: SimpleNamespace
) -> None:
    assert cli.main([]) == 0

    runtime.repo.prepare_projection_recovery.assert_awaited_once_with(
        live_recovery.recovery_id, live_recovery.owner_id, lease_seconds=600
    )
    runtime.writer.reset_for_recovery.assert_awaited_once_with(live_recovery.lease)
    runtime.repo.mark_projection_recovery_neo_ready.assert_awaited_once_with(
        live_recovery.lease, lease_seconds=600
    )
    runtime.writer.finalize_recovery.assert_awaited_once_with(live_recovery.ready)
    runtime.repo.finalize_projection_recovery.assert_awaited_once_with(live_recovery.ready)
    runtime.close.assert_awaited_once_with(runtime.driver)
    runtime.dispose.assert_awaited_once()


def test_private_projector_role_is_required(runtime: SimpleNamespace) -> None:
    runtime.settings.graph_projector_enabled = False
    assert cli.main([]) != 0
    runtime.create_driver.assert_not_called()
    runtime.recover.assert_not_awaited()


def test_failed_preflight_never_starts_recovery(runtime: SimpleNamespace) -> None:
    runtime.driver.verify_connectivity.side_effect = RuntimeError("private-detail")
    assert cli.main([]) != 0
    runtime.recover.assert_not_awaited()
    runtime.logger.error.assert_called_once_with(
        "graph_bootstrap_failed", error_type="RuntimeError"
    )
    runtime.close.assert_awaited_once_with(runtime.driver)
    runtime.dispose.assert_awaited_once()


def test_console_script_is_registered() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject.open("rb") as source:
        scripts = tomllib.load(source)["project"]["scripts"]
    assert scripts["brain-v42-graph-bootstrap"] == "brain_v42.scripts.graph_bootstrap:main"
