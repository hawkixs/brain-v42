"""Dream `verify` phase -- nightly claim verification (ADR 27 lot C, T1.8).

Deterministic and model-free: no LLM call, no MCP hop, no token (spec §2). The
CLI composes the exact registry the server does
(`facts.composition.build_fact_registry_from_settings`), selects a bounded,
ordered batch of stale-or-never-verified claims and either verifies them
in process (`--wet`) or classifies and measures without ever calling
`verify_outcome` (dry, the default).

Usage:
    python -m brain_v42.maintenance.claim_verify --run-date 2026-09-27
    python -m brain_v42.maintenance.claim_verify --run-date 2026-09-27 --wet
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

import brain_v42

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_MAX_ERROR_CHARS: Final = 2000
_DEFAULT_RUN_BUDGET_SECONDS: Final = 240.0
_RUN_DATE_TOLERANCE_DAYS: Final = 1
_LOG = structlog.get_logger(__name__)


def _run_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD (got {value!r})") from exc


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1 (got {number})")
    return number


def _max_claims(value: str) -> int:
    number = _positive_int(value)
    if number > 5000:
        raise argparse.ArgumentTypeError(f"must be <= 5000 (got {number})")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claim_verify",
        description="Re-verify a bounded, ordered batch of stale or never-verified claims.",
    )
    parser.add_argument("--run-date", type=_run_date, required=True)
    parser.add_argument("--wet", action="store_true", help="apply verdicts (default: dry)")
    parser.add_argument("--max-claims", type=_max_claims, default=None)
    parser.add_argument("--run-budget-seconds", type=_positive_int, default=None)
    parser.add_argument("--report-dir", type=Path, default=None)
    return parser


def render_report_line(report: object, rc: int) -> str:
    """The one human line spec §7.1 gives as an example, built from the report."""
    mode = "WET" if getattr(report, "mode", None) == "wet" else "DRY"
    unreadable = getattr(report, "unreadable", {})
    unreadable_total = sum(unreadable.values())
    reasons = ", ".join(f"{reason}={count}" for reason, count in sorted(unreadable.items()))
    unreadable_label = f"{unreadable_total} ({reasons})" if reasons else str(unreadable_total)
    errors = sum(getattr(report, "errors", {}).values())
    return (
        f"verify [{mode}] run_id={getattr(report, 'run_id', '?')} "
        f"status={getattr(report, 'status', '?')} rc={rc} "
        f"selected={getattr(report, 'selected', 0)}/{getattr(report, 'max_claims', '?')} "
        f"eligible={getattr(report, 'eligible_at_start', '?')} "
        f"holds={getattr(report, 'holds', 0)} "
        f"falsified={getattr(report, 'falsified', 0)} "
        f"unreadable={unreadable_label} "
        f"replayed={getattr(report, 'replayed', 0)} "
        f"skipped_budget={getattr(report, 'skipped_budget', 0)} "
        f"skipped_release={getattr(report, 'skipped_release_mismatch', 0)}/"
        f"{getattr(report, 'skipped_release_unknown', 0)} "
        f"skipped_self_referential={getattr(report, 'skipped_self_referential', 0)} "
        f"skipped_deadline={getattr(report, 'skipped_deadline', 0)} "
        f"retired={getattr(report, 'retired_mid_run', 0)} errors={errors}"
    )


def _write_report(report_dir: Path | None, run_date_value: date, report: object, rc: int) -> None:
    line = render_report_line(report, rc)
    payload = {
        **(report.as_dict() if hasattr(report, "as_dict") else {}),
        "rc": rc,
    }
    if report_dir is None:
        print(json.dumps(payload), flush=True)
        print(line, flush=True)
        return
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"{run_date_value.isoformat()}_verify.json"
    log_path = report_dir / f"{run_date_value.isoformat()}_verify.log"
    with json_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


async def _eligible_count(session_factory: async_sessionmaker[AsyncSession], now: datetime) -> int:
    """Count the exact eligibility predicate before the selection cap is applied."""
    import sqlalchemy as sa  # noqa: PLC0415

    from brain_v42.repositories.pg_claim_nightly import _eligible_query  # noqa: PLC0415

    async with session_factory() as session, session.begin():
        await session.execute(sa.text("SET TRANSACTION READ ONLY"))
        count = await session.scalar(
            sa.select(sa.func.count()).select_from(_eligible_query(now).subquery())
        )
    return int(count or 0)


async def _run(args: argparse.Namespace) -> int:
    from pydantic import ValidationError  # noqa: PLC0415

    from brain_v42.config import Settings  # noqa: PLC0415
    from brain_v42.db.engine import dispose_engine, get_engine, get_session_factory  # noqa: PLC0415
    from brain_v42.facts.composition import build_fact_registry_from_settings  # noqa: PLC0415
    from brain_v42.facts.definitions_startup import register_fact_definitions  # noqa: PLC0415
    from brain_v42.facts.nightly import (  # noqa: PLC0415
        STATUS_TO_RC,
        NightlyReport,
        NightlyVerifier,
        ReleaseCheck,
        check_preconditions,
    )
    from brain_v42.facts.verification import ClaimVerificationService  # noqa: PLC0415
    from brain_v42.repositories.pg_claim_nightly import (  # noqa: PLC0415
        VerifyRunOwnership,
        VerifyRunOwnershipLost,
        count_self_referential_claims,
        eligible_fact_names,
        finish_dry_run,
        finish_run,
        get_or_create_wet_run,
        insert_dry_run,
        select_nightly_claims,
    )

    run_date = args.run_date
    wet = args.wet
    started_at = datetime.now(UTC).isoformat()
    report: NightlyReport | None = None
    run_id: int | None = None
    eligible_at_start: int | None = None
    max_claims = args.max_claims
    run_finished = False

    def write_exit(status: str, rc: int) -> int:
        nonlocal report
        if report is None:
            report = NightlyReport(
                run_date=run_date.isoformat(),
                run_id=run_id,
                mode="wet" if wet else "dry",
            )
        report.status = status
        report.started_at = started_at
        report.finished_at = datetime.now(UTC).isoformat()
        report.max_claims = max_claims
        report.eligible_at_start = eligible_at_start
        _write_report(args.report_dir, run_date, report, rc)
        return rc

    if abs((run_date - date.today()).days) > _RUN_DATE_TOLERANCE_DAYS:
        print(
            f"claim_verify: --run-date {run_date} is more than "
            f"{_RUN_DATE_TOLERANCE_DAYS} day(s) from today",
            file=sys.stderr,
        )
        return write_exit("fail", 2)

    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print(f"claim_verify: invalid configuration: {exc}", file=sys.stderr)
        return write_exit("fail", 2)

    max_claims = args.max_claims or settings.brain_dream_verify_max_claims
    deadline_seconds = args.run_budget_seconds or _DEFAULT_RUN_BUDGET_SECONDS

    session_factory = get_session_factory()
    engine = get_engine()
    registry = build_fact_registry_from_settings(settings, session_factory)
    now = datetime.now(UTC)
    started = time.monotonic()

    ownership: VerifyRunOwnership | None = None
    try:
        try:
            registered_ok = await register_fact_definitions(registry, session_factory)
            eligible_facts = await eligible_fact_names(session_factory, now=now)
            failure = check_preconditions(
                registry, eligible_facts, definitions_registered=registered_ok
            )

            if wet:
                ownership = VerifyRunOwnership(engine)
                if not await ownership.acquire():
                    print("claim_verify: BUSY verify", file=sys.stderr)
                    return write_exit("busy", 6)
                run_id = await get_or_create_wet_run(ownership, run_date)
            else:
                run_id = await insert_dry_run(session_factory, run_date)

            if failure is not None:
                duration_s = time.monotonic() - started
                if wet:
                    assert ownership is not None  # noqa: S101
                    await finish_run(
                        ownership,
                        run_id,
                        status="fail",
                        duration_s=duration_s,
                        error_message=failure.reason,
                    )
                    run_finished = True
                else:
                    await finish_dry_run(
                        session_factory,
                        run_id,
                        status="fail",
                        duration_s=duration_s,
                        error_message=failure.reason,
                    )
                print(f"claim_verify: FAIL — {failure.reason}", file=sys.stderr)
                return write_exit("fail", 1)

            eligible_at_start = await _eligible_count(session_factory, now)
            skipped_self_referential = await count_self_referential_claims(
                session_factory, now=now, eligible_fact_names=eligible_facts
            )
            claims = await select_nightly_claims(
                session_factory,
                now=now,
                max_claims=max_claims,
                eligible_fact_names=eligible_facts,
            )
            release_check = ReleaseCheck(cli_release_path=Path(brain_v42.__file__))

            if wet:
                assert ownership is not None  # noqa: S101
                service = ClaimVerificationService(registry, session_factory)
                verifier = NightlyVerifier(
                    service=service,
                    release_check=release_check,
                    ownership=ownership,
                    deadline_seconds=deadline_seconds,
                )
                report = await verifier.run(claims, run_id=run_id, run_date=run_date, wet=True)
                report.skipped_self_referential = skipped_self_referential
                duration_s = time.monotonic() - started
                if report.status == "ownership_lost":
                    rc = 7
                else:
                    diagnostic = getattr(report, "error_message", None)
                    if report.status != "done" and diagnostic is None:
                        diagnostic = f"{report.status}: errors={report.errors}, skipped_deadline={report.skipped_deadline}"
                    await finish_run(
                        ownership,
                        run_id,
                        status=report.status,
                        duration_s=duration_s,
                        error_message=diagnostic[:_MAX_ERROR_CHARS] if diagnostic else None,
                    )
                    run_finished = True
                    rc = STATUS_TO_RC[report.status]
            else:
                verifier = NightlyVerifier(
                    service=None,  # type: ignore[arg-type]
                    release_check=release_check,
                )
                report = await verifier.run_dry(
                    claims, registry=registry, run_id=run_id, run_date=run_date
                )
                report.skipped_self_referential = skipped_self_referential
                duration_s = time.monotonic() - started
                await finish_dry_run(
                    session_factory,
                    run_id,
                    status=report.status,
                    duration_s=duration_s,
                    error_message=None,
                )
                rc = STATUS_TO_RC[report.status]

            report.started_at = started_at
            report.finished_at = datetime.now(UTC).isoformat()
            report.max_claims = max_claims
            report.eligible_at_start = eligible_at_start
            _write_report(args.report_dir, run_date, report, rc)
            return rc
        except asyncio.CancelledError:
            raise
        except VerifyRunOwnershipLost:
            if report is None:
                report = NightlyReport(
                    run_date=run_date.isoformat(),
                    run_id=run_id,
                    mode="wet",
                )
            report.status = "ownership_lost"
            report.started_at = started_at
            report.finished_at = datetime.now(UTC).isoformat()
            report.max_claims = max_claims
            report.eligible_at_start = eligible_at_start
            _write_report(args.report_dir, run_date, report, 7)
            return 7
        except Exception as exc:  # noqa: BLE001 -- an unattended nightly job must not crash raw
            detail = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
            print(f"claim_verify: FAIL — unexpected error: {detail}", file=sys.stderr)
            if wet and ownership is not None and run_id is not None and not run_finished:
                try:
                    await finish_run(
                        ownership,
                        run_id,
                        status="fail",
                        duration_s=time.monotonic() - started,
                        error_message=detail,
                    )
                except VerifyRunOwnershipLost:
                    return write_exit("ownership_lost", 7)
                except Exception:  # noqa: BLE001 -- keep the original failure exit code
                    _LOG.exception("claim_verify_finish_run_failed", run_id=run_id)
            return write_exit("fail", 1)
    finally:
        if wet and ownership is not None:
            await ownership.release()
        await registry.aclose()
        await dispose_engine()


def main() -> int:
    return asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    sys.exit(main())
