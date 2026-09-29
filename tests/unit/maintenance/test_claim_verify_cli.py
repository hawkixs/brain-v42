"""Unit contracts for the `claim_verify` CLI (ADR 27 lot C, T1.8): argparse, rc
mapping end to end with a fake orchestrator, report writing, shutdown order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.facts.nightly import NightlyReport, PreconditionFailure
from brain_v42.maintenance.claim_verify import build_parser, render_report_line
from brain_v42.repositories.pg_claim_nightly import VerifyRunOwnershipLost


@dataclass
class _FakeReport:
    run_date: str = "2026-09-27"
    run_id: int = 812
    mode: str = "wet"
    status: str = "done"
    selected: int = 0
    holds: int = 0
    falsified: int = 0
    unreadable: dict[str, int] = field(default_factory=dict)
    replayed: int = 0
    skipped_budget: int = 0
    stopped_facts: list[str] = field(default_factory=list)
    skipped_deadline: int = 0
    skipped_release_mismatch: int = 0
    skipped_release_unknown: int = 0
    retired_mid_run: int = 0
    errors: dict[str, int] = field(default_factory=dict)
    dry_claims: dict[str, object] | None = None
    dry_facts: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        return {"run_date": self.run_date, "run_id": self.run_id, "status": self.status}


def _patch_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    report: _FakeReport | None,
    acquire_result: bool = True,
    precondition_failure: PreconditionFailure | None = None,
    run_side_effect: BaseException | None = None,
) -> dict[str, object]:
    """Patch every collaborator `claim_verify._run` imports, real DB or not."""
    calls: dict[str, object] = {"finish_run": None, "finish_dry_run": None, "order": []}

    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://b:b@localhost:5433/test")

    def record(name: str) -> None:
        calls["order"].append(name)  # type: ignore[union-attr]

    monkeypatch.setattr(
        "brain_v42.db.engine.get_session_factory", lambda: MagicMock(), raising=True
    )
    monkeypatch.setattr("brain_v42.db.engine.get_engine", lambda: MagicMock(), raising=True)

    async def fake_dispose_engine() -> None:
        record("dispose_engine")

    monkeypatch.setattr("brain_v42.db.engine.dispose_engine", fake_dispose_engine, raising=True)

    fake_registry = MagicMock()

    async def fake_aclose() -> None:
        record("registry.aclose")

    fake_registry.aclose = fake_aclose
    monkeypatch.setattr(
        "brain_v42.facts.composition.build_fact_registry_from_settings",
        lambda settings, session_factory: fake_registry,
        raising=True,
    )
    monkeypatch.setattr(
        "brain_v42.facts.definitions_startup.register_fact_definitions",
        AsyncMock(return_value=True),
        raising=True,
    )
    monkeypatch.setattr(
        "brain_v42.repositories.pg_claim_nightly.eligible_fact_names",
        AsyncMock(return_value=()),
        raising=True,
    )
    monkeypatch.setattr(
        "brain_v42.facts.nightly.check_preconditions",
        lambda *a, **k: precondition_failure,
        raising=True,
    )
    monkeypatch.setattr(
        "brain_v42.repositories.pg_claim_nightly.select_nightly_claims",
        AsyncMock(return_value=()),
        raising=True,
    )
    monkeypatch.setattr(
        "brain_v42.repositories.pg_claim_nightly.count_self_referential_claims",
        AsyncMock(return_value=0),
        raising=False,
    )
    monkeypatch.setattr(
        "brain_v42.maintenance.claim_verify._eligible_count",
        AsyncMock(return_value=0),
        raising=True,
    )

    ownership = MagicMock()
    ownership.acquire = AsyncMock(return_value=acquire_result)
    ownership.release = AsyncMock()
    monkeypatch.setattr(
        "brain_v42.repositories.pg_claim_nightly.VerifyRunOwnership",
        lambda engine: ownership,
        raising=True,
    )
    monkeypatch.setattr(
        "brain_v42.repositories.pg_claim_nightly.get_or_create_wet_run",
        AsyncMock(return_value=812),
        raising=True,
    )
    monkeypatch.setattr(
        "brain_v42.repositories.pg_claim_nightly.insert_dry_run",
        AsyncMock(return_value=813),
        raising=True,
    )

    async def fake_finish_run(*a: object, **k: object) -> None:
        calls["finish_run"] = k
        record("finish_run")

    async def fake_finish_dry_run(*a: object, **k: object) -> None:
        calls["finish_dry_run"] = k
        record("finish_dry_run")

    monkeypatch.setattr(
        "brain_v42.repositories.pg_claim_nightly.finish_run", fake_finish_run, raising=True
    )
    monkeypatch.setattr(
        "brain_v42.repositories.pg_claim_nightly.finish_dry_run",
        fake_finish_dry_run,
        raising=True,
    )
    monkeypatch.setattr(
        "brain_v42.facts.verification.ClaimVerificationService",
        lambda *a, **k: MagicMock(),
        raising=True,
    )
    monkeypatch.setattr(
        "brain_v42.facts.nightly.ReleaseCheck", lambda **k: MagicMock(), raising=True
    )

    fake_verifier = MagicMock()
    if run_side_effect is not None:

        async def raise_it(*a: object, **k: object) -> object:
            raise run_side_effect

        fake_verifier.run = AsyncMock(side_effect=raise_it)
    else:
        fake_verifier.run = AsyncMock(return_value=report)
    fake_verifier.run_dry = AsyncMock(return_value=report)
    monkeypatch.setattr(
        "brain_v42.facts.nightly.NightlyVerifier", lambda **k: fake_verifier, raising=True
    )

    return calls


async def test_an_unknown_argument_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["--run-date", "2026-09-27", "--not-a-real-flag"])
    assert exit_info.value.code == 2


@pytest.mark.parametrize("value", ["0", "5001"])
def test_max_claims_outside_setting_bounds_is_a_usage_error(value: str) -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["--run-date", "2026-09-27", "--max-claims", value])
    assert exit_info.value.code == 2


def test_human_line_carries_the_spec_counts() -> None:
    report = NightlyReport(run_date="2026-09-27", run_id=812, mode="wet")
    report.max_claims = 200
    report.eligible_at_start = 14
    report.selected = 14
    report.holds = 11
    report.falsified = 1
    report.unreadable = {"probe:timeout": 2}
    report.skipped_release_mismatch = 1
    report.skipped_self_referential = 1
    report.retired_mid_run = 1

    line = render_report_line(report, 0)

    assert "selected=14/200 eligible=14" in line
    assert "unreadable=2 (probe:timeout=2)" in line
    assert "skipped_release=1/0" in line
    assert "skipped_self_referential=1" in line
    assert "retired=1 errors=0" in line


async def test_a_run_date_more_than_one_day_from_today_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    calls = _patch_pipeline(monkeypatch, report=_FakeReport())
    stale_date = date.today() - timedelta(days=10)
    args = build_parser().parse_args(["--run-date", stale_date.isoformat()])

    rc = await _run(args)

    assert rc == 2
    assert calls["order"] == []  # no DB connection: no collaborator was ever touched


async def test_status_done_maps_to_rc_0(monkeypatch: pytest.MonkeyPatch) -> None:
    from brain_v42.maintenance.claim_verify import _run

    _patch_pipeline(monkeypatch, report=_FakeReport(status="done"))
    args = build_parser().parse_args(["--run-date", date.today().isoformat(), "--wet"])

    rc = await _run(args)

    assert rc == 0


@pytest.mark.parametrize(
    ("status", "expected_rc"),
    [("timeout", 3), ("partial", 5), ("fail", 1)],
)
async def test_each_wet_status_maps_to_its_rc(
    monkeypatch: pytest.MonkeyPatch, status: str, expected_rc: int
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    _patch_pipeline(monkeypatch, report=_FakeReport(status=status))
    args = build_parser().parse_args(["--run-date", date.today().isoformat(), "--wet"])

    rc = await _run(args)

    assert rc == expected_rc


async def test_a_precondition_failure_writes_fail_and_returns_rc_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    calls = _patch_pipeline(
        monkeypatch,
        report=_FakeReport(),
        precondition_failure=PreconditionFailure("unverifiable_target: fact_a"),
    )
    args = build_parser().parse_args(["--run-date", date.today().isoformat(), "--wet"])

    rc = await _run(args)

    assert rc == 1
    assert calls["finish_run"]["status"] == "fail"
    assert calls["finish_run"]["error_message"] == "unverifiable_target: fact_a"


async def test_a_busy_lock_returns_rc_6_and_writes_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    calls = _patch_pipeline(monkeypatch, report=_FakeReport(), acquire_result=False)
    args = build_parser().parse_args(["--run-date", date.today().isoformat(), "--wet"])

    rc = await _run(args)

    assert rc == 6
    assert calls["finish_run"] is None
    assert "finish_run" not in calls["order"]
    assert "get_or_create_wet_run" not in calls["order"]


async def test_ownership_lost_returns_rc_7_and_writes_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    calls = _patch_pipeline(monkeypatch, report=_FakeReport(status="ownership_lost"))
    args = build_parser().parse_args(["--run-date", date.today().isoformat(), "--wet"])

    rc = await _run(args)

    assert rc == 7
    assert calls["finish_run"] is None


async def test_ownership_lost_raised_mid_run_also_returns_rc_7(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    calls = _patch_pipeline(monkeypatch, report=None, run_side_effect=VerifyRunOwnershipLost())
    args = build_parser().parse_args(["--run-date", date.today().isoformat(), "--wet"])

    rc = await _run(args)

    assert rc == 7
    assert calls["finish_run"] is None


@pytest.mark.parametrize("stage", ["get_or_create", "finish", "verify", "report"])
async def test_every_ownership_loss_writes_a_report_without_finishing_the_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stage: str
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    report = _FakeReport(status="ownership_lost" if stage == "report" else "done")
    calls = _patch_pipeline(
        monkeypatch,
        report=report,
        run_side_effect=VerifyRunOwnershipLost() if stage == "verify" else None,
    )
    if stage == "get_or_create":
        monkeypatch.setattr(
            "brain_v42.repositories.pg_claim_nightly.get_or_create_wet_run",
            AsyncMock(side_effect=VerifyRunOwnershipLost()),
        )
    elif stage == "finish":
        monkeypatch.setattr(
            "brain_v42.repositories.pg_claim_nightly.finish_run",
            AsyncMock(side_effect=VerifyRunOwnershipLost()),
        )
    run_date = date.today()
    args = build_parser().parse_args(
        ["--run-date", run_date.isoformat(), "--wet", "--report-dir", str(tmp_path)]
    )

    rc = await _run(args)

    assert rc == 7
    payload = json.loads((tmp_path / f"{run_date.isoformat()}_verify.json").read_text())
    assert payload["status"] == "ownership_lost"
    assert payload["rc"] == 7
    assert "status=ownership_lost" in (tmp_path / f"{run_date.isoformat()}_verify.log").read_text()
    if stage != "finish":
        assert calls["finish_run"] is None


async def test_failed_report_finishes_with_bounded_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    report = _FakeReport(status="fail")
    report.error_message = "RuntimeError: " + "x" * 3000
    calls = _patch_pipeline(monkeypatch, report=report)
    args = build_parser().parse_args(["--run-date", date.today().isoformat(), "--wet"])

    assert await _run(args) == 1
    message = calls["finish_run"]["error_message"]
    assert message.startswith("RuntimeError: ")
    assert len(message) == 2000


async def test_dry_mode_never_touches_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    from brain_v42.maintenance.claim_verify import _run

    calls = _patch_pipeline(monkeypatch, report=_FakeReport(mode="dry", status="done"))
    args = build_parser().parse_args(["--run-date", date.today().isoformat()])

    rc = await _run(args)

    assert rc == 0
    assert calls["finish_dry_run"] is not None
    assert calls["finish_run"] is None


async def test_shutdown_order_is_registry_aclose_before_dispose_engine_on_a_normal_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    calls = _patch_pipeline(monkeypatch, report=_FakeReport(status="done"))
    args = build_parser().parse_args(["--run-date", date.today().isoformat(), "--wet"])

    await _run(args)

    order = calls["order"]
    assert order.index("registry.aclose") < order.index("dispose_engine")  # type: ignore[union-attr]


async def test_shutdown_order_holds_on_a_fail_precondition_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    calls = _patch_pipeline(
        monkeypatch,
        report=_FakeReport(),
        precondition_failure=PreconditionFailure("definitions_unregistered"),
    )
    args = build_parser().parse_args(["--run-date", date.today().isoformat(), "--wet"])

    rc = await _run(args)

    assert rc == 1
    order = calls["order"]
    assert order.index("registry.aclose") < order.index("dispose_engine")  # type: ignore[union-attr]


async def test_shutdown_order_holds_when_ownership_is_lost_mid_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    calls = _patch_pipeline(monkeypatch, report=None, run_side_effect=VerifyRunOwnershipLost())
    args = build_parser().parse_args(["--run-date", date.today().isoformat(), "--wet"])

    rc = await _run(args)

    assert rc == 7
    order = calls["order"]
    assert order.index("registry.aclose") < order.index("dispose_engine")  # type: ignore[union-attr]


async def test_report_dir_appends_json_and_log_lines_and_a_rerun_gives_two(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    run_date = date.today()
    _patch_pipeline(monkeypatch, report=_FakeReport(run_date=run_date.isoformat(), status="done"))
    args = build_parser().parse_args(
        ["--run-date", run_date.isoformat(), "--wet", "--report-dir", str(tmp_path)]
    )

    await _run(args)
    await _run(args)

    json_path = tmp_path / f"{run_date.isoformat()}_verify.json"
    log_path = tmp_path / f"{run_date.isoformat()}_verify.log"
    json_lines = json_path.read_text(encoding="utf-8").splitlines()
    log_lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(json_lines) == 2
    assert len(log_lines) == 2
    for line in json_lines:
        parsed = json.loads(line)
        assert parsed["run_id"] == 812


async def test_json_report_includes_invocation_limits_and_timestamps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    run_date = date.today()
    report = NightlyReport(run_date=run_date.isoformat(), run_id=812, mode="wet")
    _patch_pipeline(monkeypatch, report=report)
    monkeypatch.setattr(
        "brain_v42.maintenance.claim_verify._eligible_count",
        AsyncMock(return_value=17),
    )
    args = build_parser().parse_args(
        [
            "--run-date",
            run_date.isoformat(),
            "--wet",
            "--max-claims",
            "5",
            "--report-dir",
            str(tmp_path),
        ]
    )

    assert await _run(args) == 0
    payload = json.loads((tmp_path / f"{run_date.isoformat()}_verify.json").read_text())
    assert payload["max_claims"] == 5
    assert payload["eligible_at_start"] == 17
    assert payload["started_at"] <= payload["finished_at"]


async def test_without_report_dir_nothing_is_written_to_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    _patch_pipeline(monkeypatch, report=_FakeReport(status="done"))
    args = build_parser().parse_args(["--run-date", date.today().isoformat(), "--wet"])

    await _run(args)

    assert list(tmp_path.iterdir()) == []
    captured = capsys.readouterr()
    assert "verify [WET]" in captured.out


async def test_an_unexpected_exception_returns_rc_1_instead_of_crashing_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unattended nightly job must not die with a raw traceback (session_sweep precedent)."""
    from brain_v42.maintenance.claim_verify import _run

    _patch_pipeline(monkeypatch, report=None, run_side_effect=RuntimeError("db blip"))
    args = build_parser().parse_args(["--run-date", date.today().isoformat(), "--wet"])

    rc = await _run(args)

    assert rc == 1


async def test_unexpected_error_on_reused_wet_run_replaces_done_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    calls = _patch_pipeline(monkeypatch, report=_FakeReport(status="done"))
    args = build_parser().parse_args(["--run-date", date.today().isoformat(), "--wet"])

    assert await _run(args) == 0
    assert calls["finish_run"]["status"] == "done"

    failing_verifier = MagicMock()
    failing_verifier.run = AsyncMock(side_effect=RuntimeError("db blip"))
    monkeypatch.setattr(
        "brain_v42.facts.nightly.NightlyVerifier", lambda **kwargs: failing_verifier
    )

    assert await _run(args) == 1
    assert calls["finish_run"]["status"] == "fail"
    assert calls["finish_run"]["error_message"] == "RuntimeError: db blip"
    assert calls["finish_run"]["duration_s"] >= 0


async def test_selection_uses_the_fact_names_checked_for_this_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    _patch_pipeline(monkeypatch, report=_FakeReport(status="done"))
    monkeypatch.setattr(
        "brain_v42.repositories.pg_claim_nightly.eligible_fact_names",
        AsyncMock(return_value=("allowed_fact",)),
    )
    select = AsyncMock(return_value=())
    monkeypatch.setattr("brain_v42.repositories.pg_claim_nightly.select_nightly_claims", select)
    args = build_parser().parse_args(["--run-date", date.today().isoformat(), "--wet"])

    assert await _run(args) == 0
    assert select.await_args.kwargs["eligible_fact_names"] == ("allowed_fact",)


@pytest.mark.parametrize("wet", [True, False], ids=["wet", "dry"])
async def test_self_referential_count_is_reported_outside_the_selection_budget(
    monkeypatch: pytest.MonkeyPatch, wet: bool
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    report = NightlyReport(
        run_date=date.today().isoformat(), run_id=812, mode="wet" if wet else "dry", selected=1
    )
    _patch_pipeline(monkeypatch, report=report)
    names = ("dream_last_night", "claims_verification_last_night")
    monkeypatch.setattr(
        "brain_v42.repositories.pg_claim_nightly.eligible_fact_names",
        AsyncMock(return_value=names),
    )
    count = AsyncMock(return_value=2)
    monkeypatch.setattr(
        "brain_v42.repositories.pg_claim_nightly.count_self_referential_claims", count
    )
    args = build_parser().parse_args(
        ["--run-date", date.today().isoformat(), "--max-claims", "1", *(["--wet"] if wet else [])]
    )

    assert await _run(args) == 0
    assert report.selected == 1
    assert report.skipped_self_referential == 2
    count.assert_awaited_once()
    assert count.await_args.kwargs["eligible_fact_names"] == names


async def test_unexpected_error_keeps_rc_1_when_finishing_the_run_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    _patch_pipeline(monkeypatch, report=None, run_side_effect=RuntimeError("db blip"))
    finish = AsyncMock(side_effect=RuntimeError("write failed"))
    monkeypatch.setattr("brain_v42.repositories.pg_claim_nightly.finish_run", finish)
    args = build_parser().parse_args(["--run-date", date.today().isoformat(), "--wet"])

    assert await _run(args) == 1
    finish.assert_awaited_once()


async def test_unexpected_error_respects_ownership_loss_during_fallback_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    _patch_pipeline(monkeypatch, report=None, run_side_effect=RuntimeError("db blip"))
    finish = AsyncMock(side_effect=VerifyRunOwnershipLost())
    monkeypatch.setattr("brain_v42.repositories.pg_claim_nightly.finish_run", finish)
    args = build_parser().parse_args(["--run-date", date.today().isoformat(), "--wet"])

    assert await _run(args) == 7
    finish.assert_awaited_once()


async def test_unexpected_error_fallback_bounds_the_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    calls = _patch_pipeline(monkeypatch, report=None, run_side_effect=RuntimeError("x" * 3000))
    args = build_parser().parse_args(["--run-date", date.today().isoformat(), "--wet"])

    assert await _run(args) == 1
    assert len(calls["finish_run"]["error_message"]) == 2000


@pytest.mark.parametrize(
    ("scenario", "expected_status", "expected_rc"),
    [
        ("busy", "busy", 6),
        ("precondition", "fail", 1),
        ("unexpected", "fail", 1),
    ],
)
async def test_early_exit_appends_report_and_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scenario: str,
    expected_status: str,
    expected_rc: int,
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    _patch_pipeline(
        monkeypatch,
        report=None,
        acquire_result=scenario != "busy",
        precondition_failure=(
            PreconditionFailure("definitions_unregistered") if scenario == "precondition" else None
        ),
        run_side_effect=RuntimeError("db blip") if scenario == "unexpected" else None,
    )
    run_date = date.today()
    args = build_parser().parse_args(
        ["--run-date", run_date.isoformat(), "--wet", "--report-dir", str(tmp_path)]
    )

    assert await _run(args) == expected_rc
    payloads = [
        json.loads(line)
        for line in (tmp_path / f"{run_date.isoformat()}_verify.json").read_text().splitlines()
    ]
    log_lines = (tmp_path / f"{run_date.isoformat()}_verify.log").read_text().splitlines()
    assert len(payloads) == len(log_lines) == 1
    assert payloads[0]["status"] == expected_status
    assert payloads[0]["rc"] == expected_rc
    assert f"status={expected_status} rc={expected_rc}" in log_lines[0]


@pytest.mark.parametrize("failure", ["run_date", "configuration"])
async def test_initial_precondition_exit_appends_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    run_date = date.today()
    if failure == "run_date":
        run_date += timedelta(days=2)
    else:
        monkeypatch.delenv("BRAIN_POSTGRES_URL", raising=False)
        monkeypatch.setenv("POSTGRES_URL", "invalid")
    args = build_parser().parse_args(
        ["--run-date", run_date.isoformat(), "--report-dir", str(tmp_path)]
    )

    assert await _run(args) == 2
    payload = json.loads((tmp_path / f"{run_date.isoformat()}_verify.json").read_text())
    assert payload["status"] == "fail"
    assert payload["rc"] == 2


async def test_cancellation_still_propagates_out_of_the_catch_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from brain_v42.maintenance.claim_verify import _run

    _patch_pipeline(monkeypatch, report=None, run_side_effect=asyncio.CancelledError())
    args = build_parser().parse_args(["--run-date", date.today().isoformat(), "--wet"])

    with pytest.raises(asyncio.CancelledError):
        await _run(args)


async def test_shutdown_order_holds_on_an_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.maintenance.claim_verify import _run

    calls = _patch_pipeline(monkeypatch, report=None, run_side_effect=RuntimeError("db blip"))
    args = build_parser().parse_args(["--run-date", date.today().isoformat(), "--wet"])

    rc = await _run(args)

    assert rc == 1
    order = calls["order"]
    assert order.index("registry.aclose") < order.index("dispose_engine")  # type: ignore[union-attr]
