"""Keep the operator CLI dry and its output free of runtime details."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi

from brain_v42.maintenance import merge_chain_repair as module
from brain_v42.maintenance.merge_chain_repair import InventoryReport, MergeChainRepairError
from brain_v42.scripts import repair_merge_chains as cli


def _runtime(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    session = AsyncMock()
    session.begin = MagicMock(return_value=AsyncMock())
    factory = MagicMock()
    factory.return_value.return_value.__aenter__.return_value = session
    monkeypatch.setattr(cli, "get_settings", MagicMock())
    monkeypatch.setattr(cli, "get_session_factory", factory)
    monkeypatch.setattr(cli, "dispose_engine", AsyncMock())
    return session


@pytest.mark.asyncio
async def test_default_only_inventories_in_read_only_transaction(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _runtime(monkeypatch)
    inventory = AsyncMock(return_value=InventoryReport(tables=()))
    repair = AsyncMock()
    monkeypatch.setattr(cli, "inventory", inventory)
    monkeypatch.setattr(cli, "repair", repair)

    assert await cli.run_from_args(cli.parse_args([])) == 0

    session.begin.assert_called_once_with()
    assert str(session.execute.await_args_list[0].args[0]) == "SET TRANSACTION READ ONLY"
    inventory.assert_awaited_once_with(session)
    repair.assert_not_awaited()
    output = capsys.readouterr()
    assert json.loads(output.out)["dry_run"] is True
    assert output.err == ""


def test_execute_requires_expected_count(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli.parse_args(["--execute"])
    assert error.value.code == 2
    assert "--expect" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--expect", "--expect-cross"])
def test_negative_expected_count_is_refused(flag: str) -> None:
    with pytest.raises(SystemExit) as error:
        cli.parse_args(["--execute", flag, "learnings=-1"])
    assert error.value.code == 2


def test_parses_per_table_expected_counts() -> None:
    args = cli.parse_args(["--execute", "--expect", "learnings=3", "--expect", "features=1"])
    assert args.execute is True
    assert args.expect == {"learnings": 3, "features": 1}


def test_parses_separate_cross_project_counts() -> None:
    args = cli.parse_args(
        [
            "--execute",
            "--expect",
            "learnings=3",
            "--expect-cross",
            "learnings=2",
            "--expect-cross",
            "features=1",
        ]
    )
    assert args.expect == {"learnings": 3}
    assert args.expect_cross == {"learnings": 2, "features": 1}


def test_execute_accepts_only_cross_project_approval() -> None:
    args = cli.parse_args(["--execute", "--expect-cross", "learnings=1"])
    assert args.execute is True
    assert args.expect == {}
    assert args.expect_cross == {"learnings": 1}


@pytest.mark.parametrize(
    "value",
    [
        "3",
        "learnings",
        "learnings=",
        "learnings=three",
        "learnings=1=2",
        "unknown=1",
        "learnings=1_0",
        "learnings=+3",
        "learnings= 3",
        "learnings=3 ",
        "learnings=٣",
    ],
)
@pytest.mark.parametrize("flag", ["--expect", "--expect-cross"])
def test_malformed_expected_table_is_refused(value: str, flag: str) -> None:
    with pytest.raises(SystemExit) as error:
        cli.parse_args(["--execute", flag, value])
    assert error.value.code == 2


@pytest.mark.parametrize("flag", ["--expect", "--expect-cross"])
def test_duplicate_expected_table_is_refused(capsys: pytest.CaptureFixture[str], flag: str) -> None:
    with pytest.raises(SystemExit) as error:
        cli.parse_args(["--execute", flag, "features=1", flag, "features=1"])
    assert error.value.code == 2
    assert "duplicate" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_execute_passes_operator_count_and_commits_only_on_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from brain_v42.maintenance.merge_chain_repair import RepairReport

    session = _runtime(monkeypatch)
    result = RepairReport(before=InventoryReport(()), after=InventoryReport(()), changed_count=0)
    repair = AsyncMock(return_value=result)
    monkeypatch.setattr(cli, "repair", repair)
    assert await cli.run_from_args(cli.parse_args(["--execute", "--expect", "learnings=0"])) == 0
    repair.assert_awaited_once_with(session, expected={"learnings": 0}, expected_cross={})
    session.execute.assert_not_awaited()
    session.begin.return_value.__aexit__.assert_awaited_once_with(None, None, None)
    assert json.loads(capsys.readouterr().out)["changed_count"] == 0


@pytest.mark.asyncio
async def test_execute_forwards_only_cross_project_approval(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _runtime(monkeypatch)
    repair = AsyncMock(
        return_value=module.RepairReport(InventoryReport(()), InventoryReport(()), 1)
    )
    monkeypatch.setattr(cli, "repair", repair)
    assert (
        await cli.run_from_args(cli.parse_args(["--execute", "--expect-cross", "learnings=1"])) == 0
    )
    repair.assert_awaited_once_with(session, expected={}, expected_cross={"learnings": 1})
    assert json.loads(capsys.readouterr().out)["changed_count"] == 1


@pytest.mark.asyncio
async def test_refusal_rolls_back_and_reports_only_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _runtime(monkeypatch)
    monkeypatch.setattr(cli, "repair", AsyncMock(side_effect=MergeChainRepairError("cycle")))
    assert await cli.run_from_args(cli.parse_args(["--execute", "--expect", "learnings=1"])) == 2
    exit_args = session.begin.return_value.__aexit__.await_args.args
    assert exit_args[0] is MergeChainRepairError
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"status": "refused", "reason": "cycle"}


@pytest.mark.asyncio
@pytest.mark.parametrize("count_kind", ["in_project", "cross_project"])
async def test_count_mismatch_reports_the_table_and_count_kind(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], count_kind: str
) -> None:
    _runtime(monkeypatch)
    monkeypatch.setattr(
        cli,
        "repair",
        AsyncMock(
            side_effect=MergeChainRepairError(
                "expected_count_mismatch", table="features", count_kind=count_kind
            )
        ),
    )
    assert await cli.run_from_args(cli.parse_args(["--execute", "--expect", "features=1"])) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {
        "status": "refused",
        "reason": "expected_count_mismatch",
        "table": "features",
        "count_kind": count_kind,
    }


@pytest.mark.asyncio
async def test_runtime_error_does_not_echo_database_details(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _runtime(monkeypatch)
    monkeypatch.setattr(cli, "inventory", AsyncMock(side_effect=RuntimeError("private contents")))
    assert await cli.run_from_args(cli.parse_args([])) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "private contents" not in output.err
    assert json.loads(output.err) == {"status": "failed", "error_type": "RuntimeError"}


@pytest.mark.asyncio
async def test_repair_resets_lock_timeout_before_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    session = AsyncMock()
    session.in_transaction = MagicMock(return_value=True)
    session.begin_nested = MagicMock()

    async def inspect_inventory(value: AsyncMock) -> InventoryReport:
        assert value is session
        statements = [str(call.args[0]) for call in session.execute.await_args_list]
        assert statements[0] == "SET LOCAL lock_timeout = '5s'"
        assert statements[1] == (
            "LOCK TABLE adrs, decisions, features, learnings, runbooks, snippets "
            "IN SHARE ROW EXCLUSIVE MODE"
        )
        assert statements[2:] == ["RESET lock_timeout"]
        return InventoryReport(
            tuple(
                module.TableReport(
                    table=table.name,
                    merged_count=0,
                    chained_count=0,
                    transits_other_project=0,
                    depth_histogram=(),
                    cycles=(),
                    dangling_roots=(),
                    cross_project_chains=(),
                    in_project_chains=(),
                )
                for table, _ in module._TABLES
            )
        )

    monkeypatch.setattr(module, "inventory", inspect_inventory)
    assert (await module.repair(session, expected={"learnings": 0})).changed_count == 0


@pytest.mark.asyncio
async def test_inventory_counts_foreign_project_transits() -> None:
    from uuid import uuid4

    session = AsyncMock()
    row = {
        "id": uuid4(),
        "merged_into": uuid4(),
        "current_id": uuid4(),
        "project_key": "test-repair-a",
        "current_project": "test-repair-a",
        "cycle": False,
        "dangling": False,
        "next_id": None,
        "depth": 2,
        "chained": True,
        "transits_other_project": True,
    }
    result = MagicMock()
    result.mappings.return_value.all.return_value = [row]
    session.execute.return_value = result

    report = await module.inventory(session)

    assert all(table.transits_other_project == 1 for table in report.tables)
    assert all(len(table.in_project_chains) == 1 for table in report.tables)
    assert {table.table for table in report.tables} == {
        "adrs",
        "decisions",
        "features",
        "learnings",
        "runbooks",
        "snippets",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("adapted", [False, True], ids=["asyncpg", "sqlalchemy-asyncpg"])
async def test_repair_maps_lock_timeout_and_rolls_back_savepoint(
    monkeypatch: pytest.MonkeyPatch, adapted: bool
) -> None:
    original = asyncpg.LockNotAvailableError("private lock details")
    if adapted:
        original = AsyncAdapt_asyncpg_dbapi.Error("private lock details")
        original.sqlstate = "55P03"
    error = sa.exc.DBAPIError("LOCK TABLE", None, original)
    session = AsyncMock()
    session.in_transaction = MagicMock(return_value=True)
    session.begin_nested = MagicMock()
    session.execute.side_effect = [None, error]
    inventory = AsyncMock()
    monkeypatch.setattr(module, "inventory", inventory)

    with pytest.raises(MergeChainRepairError, match="^lock_timeout$") as refused:
        await module.repair(session, expected={"learnings": 1})

    assert refused.value.__cause__ is error
    assert str(session.execute.await_args_list[0].args[0]) == "SET LOCAL lock_timeout = '5s'"
    assert str(session.execute.await_args_list[1].args[0]).startswith("LOCK TABLE ")
    assert session.execute.await_count == 2
    inventory.assert_not_awaited()
    assert session.begin_nested.return_value.__aexit__.await_args.args[0] is MergeChainRepairError


@pytest.mark.asyncio
async def test_repair_does_not_map_other_database_errors() -> None:
    error = sa.exc.DBAPIError("LOCK TABLE", None, asyncpg.DeadlockDetectedError("private details"))
    session = AsyncMock()
    session.in_transaction = MagicMock(return_value=True)
    session.begin_nested = MagicMock()
    session.execute.side_effect = [None, error]

    with pytest.raises(sa.exc.DBAPIError) as failed:
        await module.repair(session, expected={"learnings": 0})

    assert failed.value is error


def _repair_inventory(*, chained: bool, cross_project: bool = False) -> InventoryReport:
    """Include all six tables because approval and write scopes are per table."""
    from uuid import uuid4

    return InventoryReport(
        tuple(
            module.TableReport(
                table=table.name,
                merged_count=2 if chained and table.name == "learnings" else 0,
                chained_count=1 if chained and table.name == "learnings" else 0,
                transits_other_project=0,
                depth_histogram=(),
                cycles=(),
                dangling_roots=(),
                cross_project_chains=(
                    (module.ChainPointer(uuid4(), uuid4(), uuid4()),)
                    if chained and cross_project and table.name == "learnings"
                    else ()
                ),
                in_project_chains=(
                    (module.ChainPointer(uuid4(), uuid4(), uuid4()),)
                    if chained and not cross_project and table.name == "learnings"
                    else ()
                ),
            )
            for table, _ in module._TABLES
        )
    )


@pytest.mark.asyncio
async def test_dry_run_reports_both_counts_and_cross_project_pointers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _runtime(monkeypatch)
    before = _repair_inventory(chained=True, cross_project=True)
    monkeypatch.setattr(cli, "inventory", AsyncMock(return_value=before))
    assert await cli.run_from_args(cli.parse_args([])) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["in_project_chained_count"] == 0
    assert payload["cross_project_chained_count"] == 1
    for table in payload["tables"]:
        assert table["in_project_chained_count"] == 0
        assert table["cross_project_chained_count"] == (table["table"] == "learnings")
    report = next(table for table in before.tables if table.table == "learnings")
    pointer = report.cross_project_chains[0]
    learning = next(table for table in payload["tables"] if table["table"] == "learnings")
    assert learning["cross_project_chains"] == [
        {"id": str(pointer.id), "merged_into": str(pointer.merged_into), "root": str(pointer.root)}
    ]
    assert learning["in_project_chains"] == []


@pytest.mark.asyncio
async def test_cross_project_final_inventory_refuses_and_exits_savepoint_with_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    session.in_transaction = MagicMock(return_value=True)
    session.begin_nested = MagicMock()
    monkeypatch.setattr(
        module,
        "inventory",
        AsyncMock(
            side_effect=[
                _repair_inventory(chained=True),
                _repair_inventory(chained=True, cross_project=True),
            ]
        ),
    )
    with pytest.raises(MergeChainRepairError, match="^chains_remain$"):
        await module.repair(session, expected={"learnings": 1})
    assert session.begin_nested.return_value.__aexit__.await_args.args[0] is MergeChainRepairError


@pytest.mark.asyncio
async def test_repair_checks_deferred_guard_inside_its_savepoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    session.in_transaction = MagicMock(return_value=True)
    session.begin_nested = MagicMock()
    monkeypatch.setattr(
        module,
        "inventory",
        AsyncMock(side_effect=[_repair_inventory(chained=True), _repair_inventory(chained=False)]),
    )
    error = sa.exc.DBAPIError(
        "SET CONSTRAINTS",
        None,
        asyncpg.CheckViolationError("merge_chain_refused: learnings private-child private-root"),
    )

    async def refuse_guard(statement: object) -> None:
        if str(statement).startswith("SET CONSTRAINTS") and str(statement).endswith("IMMEDIATE"):
            raise error

    session.execute.side_effect = refuse_guard
    session.scalar.return_value = "refuse_merge_chain()"
    with pytest.raises(MergeChainRepairError, match="^merge_chain_refused$"):
        await module.repair(session, expected={"learnings": 1})
    assert session.begin_nested.return_value.__aexit__.await_args.args[0] is MergeChainRepairError


@pytest.mark.asyncio
async def test_no_write_repair_leaves_legacy_guard_events_deferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    session.in_transaction = MagicMock(return_value=True)
    session.begin_nested = MagicMock()
    session.scalar.return_value = "refuse_merge_chain()"
    monkeypatch.setattr(
        module, "inventory", AsyncMock(return_value=_repair_inventory(chained=False))
    )
    await module.repair(session, expected={})
    assert not any(
        str(call.args[0]).startswith("SET CONSTRAINTS") for call in session.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_repair_before_068_needs_no_guard_constraint(monkeypatch: pytest.MonkeyPatch) -> None:
    session = AsyncMock()
    session.in_transaction = MagicMock(return_value=True)
    session.begin_nested = MagicMock()
    session.scalar.return_value = None
    monkeypatch.setattr(
        module,
        "inventory",
        AsyncMock(side_effect=[_repair_inventory(chained=True), _repair_inventory(chained=False)]),
    )
    await module.repair(session, expected={"learnings": 1})
    session.scalar.assert_awaited_once()
    assert not any(
        str(call.args[0]).startswith("SET CONSTRAINTS") for call in session.execute.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_project", [False, True])
async def test_successful_repair_restores_deferred_guard_timing(
    monkeypatch: pytest.MonkeyPatch, cross_project: bool
) -> None:
    session = AsyncMock()
    session.in_transaction = MagicMock(return_value=True)
    session.begin_nested = MagicMock()
    session.scalar.return_value = "refuse_merge_chain()"
    monkeypatch.setattr(
        module,
        "inventory",
        AsyncMock(
            side_effect=[
                _repair_inventory(chained=True, cross_project=cross_project),
                _repair_inventory(chained=False),
            ]
        ),
    )
    assert (
        await module.repair(
            session,
            expected={} if cross_project else {"learnings": 1},
            expected_cross={"learnings": 1} if cross_project else {},
        )
    ).changed_count == 1
    constraints = ", ".join(f"public.{table.name}_merge_chain_guard" for table, _ in module._TABLES)
    assert [str(call.args[0]) for call in session.execute.await_args_list[-2:]] == [
        f"SET CONSTRAINTS {constraints} IMMEDIATE",
        f"SET CONSTRAINTS {constraints} DEFERRED",
    ]


@pytest.mark.asyncio
async def test_guard_refusal_reports_exit_two_without_database_details(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _runtime(monkeypatch)
    error = sa.exc.DBAPIError(
        "COMMIT",
        None,
        asyncpg.CheckViolationError("merge_chain_refused: learnings private-child private-root"),
    )
    session.begin.return_value.__aexit__.side_effect = error
    monkeypatch.setattr(
        cli,
        "repair",
        AsyncMock(
            return_value=module.RepairReport(
                before=InventoryReport(()), after=InventoryReport(()), changed_count=0
            )
        ),
    )
    assert await cli.run_from_args(cli.parse_args(["--execute", "--expect", "learnings=0"])) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"status": "refused", "reason": "merge_chain_refused"}
