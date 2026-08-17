"""Safety contracts for the PostgreSQL-to-Neo4j rebuild command."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from scripts.rebuild_graph_projection import parse_args, run_from_args


def test_rebuild_defaults_to_read_only_inventory() -> None:
    args = parse_args([])

    assert args.apply is False
    assert args.neo4j_cleared_confirmed is False
    assert args.writers_off_confirmed is False


@pytest.mark.parametrize(
    "argv",
    [
        ["--apply"],
        ["--apply", "--neo4j-cleared-confirmed"],
        ["--apply", "--writers-off-confirmed"],
    ],
)
def test_rebuild_apply_requires_both_cutover_confirmations(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        parse_args(argv)

    assert exc.value.code == 2


def test_rebuild_apply_is_retired_even_with_legacy_confirmations() -> None:
    with pytest.raises(SystemExit) as exc:
        parse_args(["--apply", "--neo4j-cleared-confirmed", "--writers-off-confirmed"])

    assert exc.value.code == 2


@pytest.mark.asyncio
async def test_rebuild_rejects_forged_apply_namespace_before_database_access() -> None:
    with patch("scripts.rebuild_graph_projection.PgGraphLedgerRepo") as repo_cls:
        with pytest.raises(RuntimeError, match="recover_graph_projection.py"):
            await run_from_args(SimpleNamespace(apply=True))

    repo_cls.assert_not_called()
