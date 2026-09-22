"""Unit contracts for the plan-vector recovery CLI."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "refresh_plan_embeddings.py"


def _cli():
    spec = importlib.util.spec_from_file_location("refresh_plan_embeddings_cli", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_vector_validation_refuses_bad_provider_output() -> None:
    """A provider result must have the exact row count and usable vectors."""
    cli = _cli()

    with pytest.raises(cli.RefreshRefusal, match="embedding_cardinality"):
        cli.validate_vectors([[0.5, 0.5]], expected_count=2, dimension=2)
    with pytest.raises(cli.RefreshRefusal, match="embedding_dimension"):
        cli.validate_vectors([[0.5]], expected_count=1, dimension=2)
    with pytest.raises(cli.RefreshRefusal, match="embedding_invalid"):
        cli.validate_vectors([[float("nan"), 0.5]], expected_count=1, dimension=2)
    with pytest.raises(cli.RefreshRefusal, match="embedding_invalid"):
        cli.validate_vectors([[0.0, 0.0]], expected_count=1, dimension=2)
    with pytest.raises(cli.RefreshRefusal, match="embedding_invalid"):
        cli.validate_vectors([["not-a-number", 0.5]], expected_count=1, dimension=2)
    with pytest.raises(cli.RefreshRefusal, match="embedding_cardinality"):
        cli.validate_vectors(None, expected_count=1, dimension=2)  # type: ignore[arg-type]
    with pytest.raises(cli.RefreshRefusal, match="embedding_invalid"):
        cli.validate_vectors([None], expected_count=1, dimension=2)  # type: ignore[list-item]


def test_recovery_file_is_exclusive_private_even_under_a_permissive_umask(tmp_path: Path) -> None:
    """Recovery state is durable before mutation and cannot replace a prior run."""
    cli = _cli()
    snapshot = cli.PlanVectorSnapshot("brain_recovery", "codestral", None, (), ())
    recovery = (tmp_path / "recovery.json").absolute()
    prior_umask = os.umask(0)
    try:
        cli.write_recovery_snapshot(recovery, snapshot)
    finally:
        os.umask(prior_umask)

    assert recovery.stat().st_mode & 0o777 == 0o600
    with pytest.raises(cli.RefreshRefusal, match="recovery_file_exists"):
        cli.write_recovery_snapshot(recovery, snapshot)

    target = tmp_path / "real-parent"
    target.mkdir()
    alias = tmp_path / "symlink-parent"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(cli.RefreshRefusal, match="recovery_parent_invalid"):
        cli.write_recovery_snapshot(alias / "recovery.json", snapshot)


def test_recovery_parent_cannot_be_redirected_after_validation(monkeypatch, tmp_path) -> None:
    """Replacing an inspected parent by a symlink never redirects private data."""
    cli = _cli()
    parent = tmp_path / "private"
    parent.mkdir()
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    recovery = parent / "recovery.json"
    validate = cli._validate_recovery_path

    def replace_parent_after_validation(path):
        validate(path)
        parent.rename(tmp_path / "original-private")
        parent.symlink_to(redirected, target_is_directory=True)

    monkeypatch.setattr(cli, "_validate_recovery_path", replace_parent_after_validation)
    snapshot = cli.PlanVectorSnapshot("brain", "codestral", None, (), ())
    with pytest.raises(cli.RefreshRefusal):
        cli.write_recovery_snapshot(recovery, snapshot)
    assert not (redirected / "recovery.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_explicit_database_url_overrides_a_valid_environment(monkeypatch, explicit) -> None:
    """An omitted URL uses the environment; a supplied URL selects that database."""
    from brain_v42.services import embedding_factory

    cli = _cli()
    configured = "postgresql+asyncpg://configured-host/brain"
    requested = "postgresql://explicit-host/brain"
    monkeypatch.setattr(
        embedding_factory,
        "get_settings",
        lambda: SimpleNamespace(postgres_url=configured, embedding_model="codestral"),
    )
    conn = AsyncMock()
    conn.fetchval.return_value = "brain"
    connect = AsyncMock(return_value=conn)
    monkeypatch.setattr(cli.asyncpg, "connect", connect)
    snapshot = cli.PlanVectorSnapshot("brain", "codestral", None, (), ())
    monkeypatch.setattr(cli, "read_consistent_snapshot", AsyncMock(return_value=snapshot))

    status = await cli.run(cli.parse_args(["--postgres-url", requested] if explicit else []))

    assert status == 0
    connect.assert_awaited_once_with(
        requested if explicit else configured.replace("postgresql+asyncpg://", "postgresql://")
    )


@pytest.mark.asyncio
async def test_safe_refusal_reason_is_reported_without_driver_details(monkeypatch, capsys) -> None:
    """Operators can distinguish a cohort change from a provider outage."""
    cli = _cli()
    conn = AsyncMock()
    conn.fetchval.return_value = "brain"
    monkeypatch.setattr(
        cli,
        "settings_for_standalone_script",
        lambda _dsn: SimpleNamespace(
            postgres_url="postgresql+asyncpg://brain@localhost/brain", embedding_model="codestral"
        ),
    )
    monkeypatch.setattr(cli.asyncpg, "connect", AsyncMock(return_value=conn))
    refusal = cli.RefreshRefusal("cohort_state_changed")
    refusal.__cause__ = RuntimeError("secret database details")
    monkeypatch.setattr(cli, "read_consistent_snapshot", AsyncMock(side_effect=refusal))

    assert await cli.run(cli.parse_args([])) == 2
    stderr = capsys.readouterr().err
    assert "cohort_state_changed" in stderr
    assert "secret" not in stderr
    conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_default_run_never_builds_an_embedding_client_or_writes(monkeypatch, capsys) -> None:
    """The inspection command is safe to run before a provider cutover."""
    cli = _cli()
    conn = AsyncMock()
    conn.fetchval.return_value = "brain_recovery"
    snapshot = cli.PlanVectorSnapshot("brain_recovery", "codestral", None, (), ())
    factory = MagicMock()
    monkeypatch.setattr(
        cli,
        "settings_for_standalone_script",
        lambda _dsn: SimpleNamespace(
            postgres_url="postgresql+asyncpg://brain@localhost/brain_recovery",
            embedding_model="codestral",
            embedding_dimension=2,
        ),
    )
    monkeypatch.setattr(cli.asyncpg, "connect", AsyncMock(return_value=conn))
    monkeypatch.setattr(cli, "read_consistent_snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(cli, "build_embedding_service", factory)

    status = await cli.run(
        SimpleNamespace(
            postgres_url="postgresql://secret@localhost/brain_recovery",
            project=None,
            apply=False,
            recovery_file=None,
            expected_plans=None,
            expected_chunks=None,
            expected_database=None,
            expected_model=None,
            batch_size=2,
        )
    )

    assert status == 0
    assert factory.call_count == 0
    conn.close.assert_awaited_once()
    assert "secret" not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_settings_failure_is_a_safe_nonzero_result(monkeypatch, capsys) -> None:
    """A malformed configured DSN never reaches stderr or a traceback."""
    cli = _cli()
    monkeypatch.setattr(
        cli,
        "settings_for_standalone_script",
        MagicMock(side_effect=ValueError("secret-dsn-from-settings")),
    )

    status = await cli.run(
        SimpleNamespace(
            postgres_url="postgresql://secret@localhost/brain",
            project=None,
            apply=False,
            recovery_file=None,
            expected_plans=None,
            expected_chunks=None,
            expected_database=None,
            expected_model=None,
            batch_size=2,
        )
    )

    assert status == 2
    assert "secret" not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_connection_close_failure_is_a_safe_nonzero_result(monkeypatch, capsys) -> None:
    """Closing a successfully opened connection cannot expose driver details."""
    cli = _cli()
    conn = AsyncMock()
    conn.fetchval.return_value = "brain"
    conn.close.side_effect = RuntimeError("secret-close-details")
    snapshot = cli.PlanVectorSnapshot("brain", "codestral", None, (), ())
    monkeypatch.setattr(
        cli,
        "settings_for_standalone_script",
        lambda _dsn: SimpleNamespace(
            postgres_url="postgresql+asyncpg://brain@localhost/brain",
            embedding_model="codestral",
            embedding_dimension=2,
        ),
    )
    monkeypatch.setattr(cli.asyncpg, "connect", AsyncMock(return_value=conn))
    monkeypatch.setattr(cli, "read_consistent_snapshot", AsyncMock(return_value=snapshot))

    status = await cli.run(
        SimpleNamespace(
            postgres_url="postgresql://brain@localhost/brain",
            project=None,
            apply=False,
            recovery_file=None,
            expected_plans=None,
            expected_chunks=None,
            expected_database=None,
            expected_model=None,
            batch_size=2,
        )
    )

    assert status == 2
    stderr = capsys.readouterr().err
    assert "secret-close-details" not in stderr
    assert "cleanup failed" in stderr


@pytest.mark.asyncio
async def test_recovery_path_refusal_happens_before_provider_creation(
    monkeypatch, tmp_path: Path
) -> None:
    """A collision must not spend a paid embedding request before refusing."""
    cli = _cli()
    recovery = (tmp_path / "existing-recovery.json").absolute()
    recovery.write_text("existing")
    conn = AsyncMock()
    conn.fetchval.return_value = "brain"
    snapshot = cli.PlanVectorSnapshot("brain", "codestral", None, ({"id": "p"},), ())
    factory = MagicMock()
    monkeypatch.setattr(
        cli,
        "settings_for_standalone_script",
        lambda _dsn: SimpleNamespace(
            postgres_url="postgresql+asyncpg://brain@localhost/brain",
            embedding_model="codestral",
            embedding_dimension=2,
        ),
    )
    monkeypatch.setattr(cli.asyncpg, "connect", AsyncMock(return_value=conn))
    monkeypatch.setattr(cli, "read_consistent_snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(cli, "build_embedding_service", factory)

    status = await cli.run(
        SimpleNamespace(
            postgres_url="postgresql://brain@localhost/brain",
            project=None,
            apply=True,
            recovery_file=str(recovery),
            expected_plans=1,
            expected_chunks=0,
            expected_database="brain",
            expected_model="codestral",
            batch_size=2,
        )
    )

    assert status == 2
    assert factory.call_count == 0
    conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_factory_failure_is_a_safe_nonzero_result(monkeypatch, capsys) -> None:
    """Factory configuration errors close the DB without exposing their body."""
    cli = _cli()
    conn = AsyncMock()
    conn.fetchval.return_value = "brain"
    snapshot = cli.PlanVectorSnapshot("brain", "codestral", None, ({"id": "p"},), ())
    monkeypatch.setattr(
        cli,
        "settings_for_standalone_script",
        lambda _dsn: SimpleNamespace(
            postgres_url="postgresql+asyncpg://brain@localhost/brain",
            embedding_model="codestral",
            embedding_dimension=2,
        ),
    )
    monkeypatch.setattr(cli.asyncpg, "connect", AsyncMock(return_value=conn))
    monkeypatch.setattr(cli, "read_consistent_snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(cli, "compose_inputs", lambda _snapshot: [("parent", "p", "text")])
    monkeypatch.setattr(
        cli, "build_embedding_service", MagicMock(side_effect=RuntimeError("secret factory body"))
    )

    status = await cli.run(
        SimpleNamespace(
            postgres_url="postgresql://brain@localhost/brain",
            project=None,
            apply=True,
            recovery_file="/tmp/new-recovery.json",
            expected_plans=1,
            expected_chunks=0,
            expected_database="brain",
            expected_model="codestral",
            batch_size=2,
        )
    )

    assert status == 2
    conn.close.assert_awaited_once()
    assert "secret factory body" not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_provider_close_failure_still_closes_the_database(monkeypatch, capsys) -> None:
    """A cleanup failure cannot leak the DB connection on an apply path."""
    cli = _cli()
    conn = AsyncMock()
    conn.fetchval.return_value = "brain_recovery"
    snapshot = cli.PlanVectorSnapshot("brain_recovery", "codestral", None, ({"id": "p"},), ())
    provider = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("provider details")))
    monkeypatch.setattr(
        cli,
        "settings_for_standalone_script",
        lambda _dsn: SimpleNamespace(
            postgres_url="postgresql+asyncpg://brain@localhost/brain_recovery",
            embedding_model="codestral",
            embedding_dimension=2,
        ),
    )
    monkeypatch.setattr(cli.asyncpg, "connect", AsyncMock(return_value=conn))
    monkeypatch.setattr(cli, "read_consistent_snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(cli, "compose_inputs", lambda _snapshot: [("parent", "p", "text")])
    monkeypatch.setattr(cli, "build_embedding_service", lambda _settings: provider)
    monkeypatch.setattr(cli, "embed_inputs", AsyncMock(return_value=({"p": [0.5, 0.5]}, {})))
    monkeypatch.setattr(cli, "apply_refresh", AsyncMock(return_value=(1, 0, "digest")))

    status = await cli.run(
        SimpleNamespace(
            postgres_url="postgresql://brain@localhost/brain_recovery",
            project=None,
            apply=True,
            recovery_file="/tmp/recovery.json",
            expected_plans=1,
            expected_chunks=0,
            expected_database="brain_recovery",
            expected_model="codestral",
            batch_size=2,
        )
    )

    assert status == 2
    conn.close.assert_awaited_once()
    assert "provider details" not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_provider_failure_is_a_safe_refusal() -> None:
    """Provider bodies and transport details do not cross the CLI boundary."""
    cli = _cli()
    provider = SimpleNamespace(
        embed_texts=AsyncMock(side_effect=RuntimeError("secret provider body"))
    )

    with pytest.raises(cli.RefreshRefusal, match="embedding_provider_failed"):
        await cli.embed_inputs(
            provider,
            [("parent", "parent-id", "input")],
            batch_size=2,
            dimension=2,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expected_database, expected_model", [("other", "codestral"), ("brain", "other")]
)
async def test_apply_rejects_changed_config_identity_before_provider_use(
    monkeypatch, expected_database, expected_model
) -> None:
    """A DB or model cutover after the read phase cannot refresh old approval."""
    cli = _cli()
    conn = AsyncMock()
    conn.fetchval.return_value = "brain"
    snapshot = cli.PlanVectorSnapshot("brain", "codestral", None, ({"id": "p"},), ())
    factory = MagicMock()
    monkeypatch.setattr(
        cli,
        "settings_for_standalone_script",
        lambda _dsn: SimpleNamespace(
            postgres_url="postgresql+asyncpg://brain@localhost/brain",
            embedding_model="codestral",
            embedding_dimension=2,
        ),
    )
    monkeypatch.setattr(cli.asyncpg, "connect", AsyncMock(return_value=conn))
    monkeypatch.setattr(cli, "read_consistent_snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(cli, "build_embedding_service", factory)

    status = await cli.run(
        SimpleNamespace(
            postgres_url="postgresql://brain@localhost/brain",
            project=None,
            apply=True,
            recovery_file="/tmp/recovery.json",
            expected_plans=1,
            expected_chunks=0,
            expected_database=expected_database,
            expected_model=expected_model,
            batch_size=2,
        )
    )

    assert status == 2
    assert factory.call_count == 0
