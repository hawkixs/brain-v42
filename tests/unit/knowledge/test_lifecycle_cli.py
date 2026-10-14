"""CLI gates must refuse before orchestration can create durable evidence."""

import io
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from brain_v42.config import Settings
from brain_v42.lifecycle_cli.command import execute, parse_args
from brain_v42.services.knowledge_lifecycle_pipeline import PipelineRefusal, RunReport


def settings(**overrides: object) -> Settings:
    return Settings(postgres_url="postgresql+asyncpg://unused/unused", _env_file=None, **overrides)


def test_lifecycle_argument_mapping() -> None:
    args = parse_args(["run", "--mode", "dry-run", "--scope", "all", "--ceiling-usd", "1"])
    assert args.mode == "dry_run" and args.scope == {} and args.ceiling_usd == 1


def test_lifecycle_help_names_composition_root(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        parse_args(["--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "python -m brain_v42.lifecycle_cli" in help_text
    assert "brain_v42.knowledge.lifecycle" not in help_text


async def test_lifecycle_run_passes_generated_run_id_to_start() -> None:
    pipeline, output = AsyncMock(), io.StringIO()
    pipeline.start.return_value = RunReport("r", "dry_run", "applied")
    code = await execute(
        ["run", "--mode", "dry-run", "--scope", "all", "--ceiling-usd", "1", "--json"],
        settings(brain_knowledge_lifecycle_enabled=True),
        pipeline,
        stdout=output,
    )
    assert code == 0
    run_id = pipeline.start.call_args.kwargs["run_id"]
    assert run_id.startswith("lifecycle-")
    assert UUID(run_id.removeprefix("lifecycle-")).version == 4


@pytest.mark.parametrize("exception", [RuntimeError("private details"), KeyError("manifest")])
async def test_lifecycle_start_failure_prints_durable_report(exception: Exception) -> None:
    pipeline, output = AsyncMock(), io.StringIO()
    durable: dict[str, RunReport] = {}

    async def start(*args: object, run_id: str = "unassigned") -> RunReport:
        durable[run_id] = RunReport(
            run_id, "dry_run", "evaluated", pending_by_state={"obsolete": 2}, real_cost_usd=0.25
        )
        raise exception

    async def report(run_id: str) -> RunReport:
        return durable[run_id]

    pipeline.start.side_effect = start
    pipeline.report.side_effect = report
    code = await execute(
        ["run", "--mode", "dry-run", "--scope", "all", "--ceiling-usd", "1", "--json"],
        settings(brain_knowledge_lifecycle_enabled=True),
        pipeline,
        stdout=output,
    )
    result = json.loads(output.getvalue())
    assert code == 3 and result["failure_reason"] == "command_failed"
    assert result["run_id"] in durable and result["run_id"] != "unassigned"
    assert result["status"] == "evaluated"
    assert result["pending_by_state"] == {"obsolete": 2} and result["real_cost_usd"] == 0.25
    pipeline.report.assert_awaited_once_with(result["run_id"])
    assert "private details" not in output.getvalue() and "manifest" not in output.getvalue()


@pytest.mark.parametrize("location", ["report", "read_mode", "resume", "invalidate"])
async def test_lifecycle_internal_keyerror_is_command_failed(location: str) -> None:
    pipeline, reader, output = AsyncMock(), AsyncMock(return_value="dry_run"), io.StringIO()
    durable = RunReport("r", "apply", "applying", applied_by_state={"obsolete": 1})
    pipeline.report.return_value = durable
    failure = KeyError("internal private field")
    if location == "report":
        pipeline.report.side_effect = [failure, durable]
    elif location == "read_mode":
        reader.side_effect = failure
    else:
        getattr(pipeline, location).side_effect = failure
    at = datetime.now(UTC)
    command = "resume" if location == "read_mode" else location
    argv = [command, "--run", "r", "--json"]
    if command == "invalidate":
        argv += ["--reason", "wrong", "--backup-run-id", "b", "--backup-sealed-at", at.isoformat()]
    code = await execute(
        argv,
        settings(brain_knowledge_lifecycle_enabled=True, brain_knowledge_lifecycle_dry_run=False),
        pipeline,
        reader,
        now=lambda: at,
        stdout=output,
    )
    result = json.loads(output.getvalue())
    assert code == 3 and result["failure_reason"] == "command_failed"
    assert result["status"] == "applying" and result["applied_by_state"] == {"obsolete": 1}
    assert "internal private field" not in output.getvalue()


@pytest.mark.parametrize("command", ["report", "resume", "invalidate"])
async def test_lifecycle_missing_run_is_typed_refusal(command: str) -> None:
    from brain_v42.repositories.pg_knowledge_lifecycle import RunNotFound

    pipeline, reader, output = AsyncMock(), AsyncMock(), io.StringIO()
    if command == "resume":
        reader.side_effect = RunNotFound("missing")
    else:
        getattr(pipeline, command).side_effect = RunNotFound("missing")
    at = datetime.now(UTC)
    argv = [command, "--run", "missing", "--json"]
    if command == "invalidate":
        argv += ["--reason", "wrong", "--backup-run-id", "b", "--backup-sealed-at", at.isoformat()]
    code = await execute(
        argv,
        settings(brain_knowledge_lifecycle_enabled=True, brain_knowledge_lifecycle_dry_run=False),
        pipeline,
        reader,
        now=lambda: at,
        stdout=output,
    )
    result = json.loads(output.getvalue())
    assert code == 2 and result["status"] == "refused"
    assert result["run_id"] == "missing" and result["failure_reason"] == "run_not_found"


@pytest.mark.parametrize(
    "command",
    [
        ["run", "--mode", "dry-run", "--scope", "all", "--ceiling-usd", "1"],
        ["resume", "--run", "r"],
        ["invalidate", "--run", "r", "--reason", "wrong"],
    ],
)
async def test_lifecycle_disabled_calls_no_orchestration(command: list[str]) -> None:
    pipeline, reader, output = AsyncMock(), AsyncMock(), io.StringIO()
    code = await execute(command + ["--json"], settings(), pipeline, reader, stdout=output)
    assert code == 2 and json.loads(output.getvalue())["failure_reason"] == "lifecycle_disabled"
    assert not pipeline.mock_calls and not reader.mock_calls


@pytest.mark.parametrize(
    "command",
    [
        ["run", "--mode", "apply", "--scope", "all", "--ceiling-usd", "1"],
        ["resume", "--run", "r"],
        ["invalidate", "--run", "r", "--reason", "wrong"],
    ],
)
async def test_lifecycle_dry_switch_calls_no_orchestration(command: list[str]) -> None:
    pipeline, reader = AsyncMock(), AsyncMock(return_value="apply")
    code = await execute(
        command,
        settings(brain_knowledge_lifecycle_enabled=True),
        pipeline,
        reader,
        stdout=io.StringIO(),
    )
    assert code == 2 and not pipeline.mock_calls


@pytest.mark.parametrize(
    "sealed",
    [
        None,
        "2026-10-14T12:00:00",
        "2026-10-14T13:00:00+01:00",
        "bad",
        "2026-10-14T10:59:59Z",
        "2026-10-14T12:00:01Z",
    ],
)
async def test_lifecycle_backup_refused_before_start(sealed: str | None) -> None:
    pipeline = AsyncMock()
    command = ["run", "--mode", "apply", "--scope", "all", "--ceiling-usd", "1"]
    if sealed is not None:
        command += ["--backup-run-id", "backup", "--backup-sealed-at", sealed]
    code = await execute(
        command,
        settings(brain_knowledge_lifecycle_enabled=True, brain_knowledge_lifecycle_dry_run=False),
        pipeline,
        AsyncMock(),
        now=lambda: datetime(2026, 10, 14, 12, tzinfo=UTC),
        stdout=io.StringIO(),
    )
    assert code == 2 and not pipeline.mock_calls


async def test_lifecycle_apply_forwards_fresh_backup() -> None:
    pipeline = AsyncMock()
    pipeline.start.return_value = RunReport("r", "apply", "applied")
    at = datetime.now(UTC)
    code = await execute(
        [
            "run",
            "--mode",
            "apply",
            "--scope",
            "brain-v42",
            "--ceiling-usd",
            "1",
            "--backup-run-id",
            "b",
            "--backup-sealed-at",
            (at - timedelta(hours=1)).isoformat(),
        ],
        settings(brain_knowledge_lifecycle_enabled=True, brain_knowledge_lifecycle_dry_run=False),
        pipeline,
        AsyncMock(),
        now=lambda: at,
        stdout=io.StringIO(),
    )
    assert code == 0
    assert pipeline.start.call_args.args[:3] == ("apply", {"project_key": "brain-v42"}, 1)
    assert pipeline.start.call_args.args[3].run_id == "b"


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (RunReport("r", "dry_run", "applied"), 0),
        (RunReport("r", "dry_run", "failed", failure_reason="cost_guard_refused"), 3),
        (PipelineRefusal("target_mismatch"), 2),
        (PipelineRefusal("run_locked"), 4),
    ],
)
async def test_lifecycle_exit_codes_and_json(outcome: object, code: int) -> None:
    pipeline, output = AsyncMock(), io.StringIO()
    if isinstance(outcome, Exception):
        pipeline.start.side_effect = outcome
    else:
        pipeline.start.return_value = outcome
    actual = await execute(
        ["run", "--mode", "dry-run", "--scope", "all", "--ceiling-usd", "1", "--json"],
        settings(brain_knowledge_lifecycle_enabled=True),
        pipeline,
        AsyncMock(),
        stdout=output,
    )
    assert actual == code
    assert json.loads(output.getvalue())["status"] in {"applied", "failed", "refused"}


async def test_lifecycle_report_ignores_killswitches() -> None:
    pipeline = AsyncMock()
    pipeline.report.return_value = RunReport("r", "apply", "applied")
    assert (
        await execute(
            ["report", "--run", "r", "--json"],
            settings(),
            pipeline,
            AsyncMock(),
            stdout=io.StringIO(),
        )
        == 0
    )
    pipeline.report.assert_awaited_once_with("r")


async def test_lifecycle_resume_dry_run_forwards_checkpoint() -> None:
    pipeline = AsyncMock()
    pipeline.resume.return_value = RunReport("r", "dry_run", "applied")
    assert (
        await execute(
            ["resume", "--run", "r"],
            settings(brain_knowledge_lifecycle_enabled=True),
            pipeline,
            AsyncMock(return_value="dry_run"),
            stdout=io.StringIO(),
        )
        == 0
    )
    pipeline.resume.assert_awaited_once_with("r", None)


@pytest.mark.parametrize(
    "command", [["resume", "--run", "r"], ["invalidate", "--run", "r", "--reason", "wrong"]]
)
async def test_lifecycle_mutations_require_backup_before_orchestration(command: list[str]) -> None:
    pipeline = AsyncMock()
    assert (
        await execute(
            command,
            settings(
                brain_knowledge_lifecycle_enabled=True, brain_knowledge_lifecycle_dry_run=False
            ),
            pipeline,
            AsyncMock(return_value="apply"),
            stdout=io.StringIO(),
        )
        == 2
    )
    assert not pipeline.mock_calls


async def test_lifecycle_invalidate_forwards_reason_and_backup() -> None:
    pipeline = AsyncMock()
    pipeline.invalidate.return_value = RunReport("r", "apply", "invalidated")
    at = datetime.now(UTC)
    assert (
        await execute(
            [
                "invalidate",
                "--run",
                "r",
                "--reason",
                "wrong inputs",
                "--backup-run-id",
                "b",
                "--backup-sealed-at",
                at.isoformat(),
            ],
            settings(
                brain_knowledge_lifecycle_enabled=True, brain_knowledge_lifecycle_dry_run=False
            ),
            pipeline,
            now=lambda: at,
            stdout=io.StringIO(),
        )
        == 0
    )
    assert pipeline.invalidate.call_args.args[:2] == ("r", "wrong inputs")
    assert pipeline.invalidate.call_args.args[2].run_id == "b"


async def test_lifecycle_unexpected_failure_is_sanitized() -> None:
    pipeline, output = AsyncMock(), io.StringIO()
    pipeline.start.side_effect = RuntimeError("entry contents must not be rendered")
    assert (
        await execute(
            ["run", "--mode", "dry-run", "--scope", "all", "--ceiling-usd", "1", "--json"],
            settings(brain_knowledge_lifecycle_enabled=True),
            pipeline,
            stdout=output,
        )
        == 3
    )
    assert json.loads(output.getvalue())["failure_reason"] == "command_failed"
    assert "entry contents" not in output.getvalue()


@pytest.mark.parametrize("ceiling", ["nan", "inf", "-1", "bad"])
async def test_lifecycle_invalid_arguments_print_report(ceiling: str) -> None:
    pipeline, output = AsyncMock(), io.StringIO()
    assert (
        await execute(
            ["run", "--mode", "dry-run", "--scope", "all", "--ceiling-usd", ceiling, "--json"],
            settings(),
            pipeline,
            AsyncMock(),
            stdout=output,
        )
        == 2
    )
    assert json.loads(output.getvalue())["failure_reason"] == "invalid_arguments"
    assert not pipeline.mock_calls
