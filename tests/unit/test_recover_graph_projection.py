"""Safety contracts for the graph projection recovery command."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from pydantic import SecretStr


def _module() -> Any:
    return importlib.import_module("scripts.recover_graph_projection")


def _apply_args() -> Any:
    return _module().parse_args(
        [
            "--apply",
            "--recovery-id",
            "d7f565ad-943a-4668-8db5-7d91eb19a608",
            "--writers-off-confirmed",
            "--legacy-credential-revoked-confirmed",
            "--neo4j-sessions-zero-confirmed",
            "--neo4j-dedicated-confirmed",
            "--postgres-restore-tested",
        ]
    )


def test_recovery_defaults_to_read_only_inventory() -> None:
    args = _module().parse_args([])

    assert args.apply is False
    assert args.recovery_id is None
    assert args.writers_off_confirmed is False
    assert args.legacy_credential_revoked_confirmed is False
    assert args.neo4j_sessions_zero_confirmed is False
    assert args.neo4j_dedicated_confirmed is False
    assert args.postgres_restore_tested is False
    assert args.lease_seconds == 3600


@pytest.mark.parametrize(
    "argv",
    [
        ["--apply"],
        ["--apply", "--recovery-id", "d7f565ad-943a-4668-8db5-7d91eb19a608"],
        [
            "--apply",
            "--recovery-id",
            "d7f565ad-943a-4668-8db5-7d91eb19a608",
            "--writers-off-confirmed",
            "--legacy-credential-revoked-confirmed",
        ],
        [
            "--apply",
            "--recovery-id",
            "d7f565ad-943a-4668-8db5-7d91eb19a608",
            "--writers-off-confirmed",
            "--legacy-credential-revoked-confirmed",
            "--neo4j-sessions-zero-confirmed",
        ],
        [
            "--apply",
            "--recovery-id",
            "d7f565ad-943a-4668-8db5-7d91eb19a608",
            "--writers-off-confirmed",
            "--legacy-credential-revoked-confirmed",
            "--neo4j-sessions-zero-confirmed",
            "--neo4j-dedicated-confirmed",
        ],
    ],
)
def test_recovery_apply_requires_id_and_all_offline_attestations(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        _module().parse_args(argv)

    assert exc.value.code == 2


def test_recovery_apply_accepts_one_explicit_resumable_id() -> None:
    args = _apply_args()

    assert args.apply is True
    assert args.recovery_id == UUID("d7f565ad-943a-4668-8db5-7d91eb19a608")


def test_option_a_requires_postgres_restore_proof_without_neo4j_backup() -> None:
    args = _module().parse_args(
        [
            "--apply",
            "--recovery-id",
            "d7f565ad-943a-4668-8db5-7d91eb19a608",
            "--writers-off-confirmed",
            "--legacy-credential-revoked-confirmed",
            "--neo4j-sessions-zero-confirmed",
            "--neo4j-dedicated-confirmed",
            "--postgres-restore-tested",
        ]
    )

    assert args.apply is True
    assert args.postgres_restore_tested is True
    assert not hasattr(args, "neo4j_backup_attested")
    assert not hasattr(args, "postgres_recovery_v2_attested")


@pytest.mark.parametrize(
    "omitted",
    ["--neo4j-dedicated-confirmed", "--postgres-restore-tested"],
)
def test_destructive_recovery_requires_dedicated_database_and_postgres_restore(
    omitted: str,
) -> None:
    confirmations = [
        "--writers-off-confirmed",
        "--legacy-credential-revoked-confirmed",
        "--neo4j-sessions-zero-confirmed",
        "--neo4j-dedicated-confirmed",
        "--postgres-restore-tested",
    ]
    confirmations.remove(omitted)

    with pytest.raises(SystemExit) as exc:
        _module().parse_args(
            [
                "--apply",
                "--recovery-id",
                "d7f565ad-943a-4668-8db5-7d91eb19a608",
                *confirmations,
            ]
        )

    assert exc.value.code == 2


@pytest.mark.asyncio
async def test_apply_preflights_neo4j_before_cross_store_recovery() -> None:
    module = _module()
    events: list[str] = []
    repo = MagicMock()
    repo.assert_schema_ready = AsyncMock()
    driver = MagicMock()

    async def verify_connectivity() -> None:
        events.append("connectivity")

    async def ensure_schema(_driver: object) -> None:
        events.append("schema")

    async def recover(*_args: object, **_kwargs: object) -> Any:
        from brain_v42.maintenance.graph_projection_recovery import ProjectionRecoveryReport

        events.append("recover")
        return ProjectionRecoveryReport(
            recovery_id=UUID("d7f565ad-943a-4668-8db5-7d91eb19a608"),
            status="recovered",
            generation=9,
            deleted_nodes=2,
            entity_events=1,
            relation_events=1,
            entity_count=1,
            relation_count=1,
            pending_count_before=0,
        )

    driver.verify_connectivity = AsyncMock(side_effect=verify_connectivity)
    settings = SimpleNamespace(
        graph_projector_enabled=True,
        graph_projector_neo4j_url="bolt://127.0.0.1:7687",
        graph_projector_neo4j_user="projector",
        graph_projector_neo4j_password=SecretStr("private-secret"),
        neo4j_timeout=3.0,
    )
    with (
        patch.object(module, "PgGraphLedgerRepo", return_value=repo),
        patch.object(module, "get_session_factory", return_value=MagicMock()),
        patch.object(module, "get_settings", return_value=settings),
        patch.object(module, "create_neo4j_driver", return_value=driver),
        patch.object(
            module,
            "ensure_graph_projection_schema",
            new=AsyncMock(side_effect=ensure_schema),
            create=True,
        ),
        patch.object(module, "recover_projection_lineage", new=AsyncMock(side_effect=recover)),
        patch.object(module, "close_neo4j_driver", new=AsyncMock()),
        patch.object(module, "dispose_engine", new=AsyncMock()),
    ):
        result = await module.run_from_args(_apply_args())

    assert result == 0
    assert events == ["connectivity", "schema", "recover"]


@pytest.mark.asyncio
async def test_failed_neo4j_preflight_never_enters_cross_store_recovery() -> None:
    module = _module()
    repo = MagicMock()
    repo.assert_schema_ready = AsyncMock()
    driver = MagicMock()
    driver.verify_connectivity = AsyncMock(side_effect=RuntimeError("unreachable"))
    recover = AsyncMock()
    settings = SimpleNamespace(
        graph_projector_enabled=True,
        graph_projector_neo4j_url="bolt://127.0.0.1:7687",
        graph_projector_neo4j_user="projector",
        graph_projector_neo4j_password=SecretStr("private-secret"),
        neo4j_timeout=3.0,
    )
    with (
        patch.object(module, "PgGraphLedgerRepo", return_value=repo),
        patch.object(module, "get_session_factory", return_value=MagicMock()),
        patch.object(module, "get_settings", return_value=settings),
        patch.object(module, "create_neo4j_driver", return_value=driver),
        patch.object(
            module,
            "ensure_graph_projection_schema",
            new=AsyncMock(),
            create=True,
        ),
        patch.object(module, "recover_projection_lineage", new=recover),
        patch.object(module, "close_neo4j_driver", new=AsyncMock()),
        patch.object(module, "dispose_engine", new=AsyncMock()),
    ):
        with pytest.raises(RuntimeError, match="unreachable"):
            await module.run_from_args(_apply_args())

    recover.assert_not_awaited()


@pytest.mark.parametrize(
    "flag",
    [
        "--recovery-id=d7f565ad-943a-4668-8db5-7d91eb19a608",
        "--writers-off-confirmed",
        "--legacy-credential-revoked-confirmed",
        "--neo4j-sessions-zero-confirmed",
        "--neo4j-dedicated-confirmed",
        "--postgres-restore-tested",
    ],
)
def test_mutating_recovery_inputs_are_valid_only_during_apply(flag: str) -> None:
    with pytest.raises(SystemExit) as exc:
        _module().parse_args([flag])

    assert exc.value.code == 2


@pytest.mark.parametrize("value", ["0", "59", "86401", "not-an-int"])
def test_recovery_lease_is_bounded(value: str) -> None:
    with pytest.raises(SystemExit) as exc:
        _module().parse_args(["--lease-seconds", value])

    assert exc.value.code == 2
