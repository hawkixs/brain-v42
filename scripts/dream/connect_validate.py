from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from brain_v42.config import get_settings
from brain_v42.db.tables import dream_runs

_MAX_ERROR_LENGTH = 1_000
_STEP_A_RE = re.compile(
    r"STEP_A: entities_processed=(?P<entities_processed>-?\d+) "
    r"created=(?P<created>-?\d+) matched=(?P<matched>-?\d+) "
    r"skipped=(?P<skipped>-?\d+) errors=(?P<errors>-?\d+) "
    r"freshness=(?P<freshness>-?\d+\.\d{2})"
)
_STEP_B_RE = re.compile(
    r"STEP_B: orphans_listed=(?P<orphans_listed>-?\d+) "
    r"created=(?P<created>-?\d+) matched=(?P<matched>-?\d+) "
    r"invalid=(?P<invalid>-?\d+) errors=(?P<errors>-?\d+)"
)


class ValidationFailure(Exception):
    """Violation of the CONNECT final-report contract."""


@dataclass(frozen=True, slots=True)
class ConnectReport:
    step_a: dict[str, int | float]
    step_b: dict[str, int]


def _bounded_detail(primary: str, secondary: str | None = None) -> str:
    if secondary is None:
        return primary[:_MAX_ERROR_LENGTH]

    separator = "; "
    available = _MAX_ERROR_LENGTH - len(separator)
    primary_budget = available // 2
    secondary_budget = available - primary_budget
    if len(primary) < primary_budget:
        secondary_budget += primary_budget - len(primary)
        primary_budget = len(primary)
    elif len(secondary) < secondary_budget:
        primary_budget += secondary_budget - len(secondary)
        secondary_budget = len(secondary)
    return f"{primary[:primary_budget]}{separator}{secondary[:secondary_budget]}"


def parse_report(raw: str) -> ConnectReport:
    lines = raw.splitlines()
    if len(lines) != 2:
        raise ValidationFailure(f"expected exactly two report lines, got {len(lines)}")
    step_a_match = _STEP_A_RE.fullmatch(lines[0])
    if step_a_match is None:
        raise ValidationFailure("malformed STEP_A report line")
    step_b_match = _STEP_B_RE.fullmatch(lines[1])
    if step_b_match is None:
        raise ValidationFailure("malformed STEP_B report line")

    try:
        step_a: dict[str, int | float] = {
            key: float(value) if key == "freshness" else int(value)
            for key, value in step_a_match.groupdict().items()
        }
        step_b = {key: int(value) for key, value in step_b_match.groupdict().items()}
    except ValueError as exc:
        raise ValidationFailure("numeric report field could not be converted") from exc
    for name, value in (*step_a.items(), *step_b.items()):
        if name != "freshness" and value < 0:
            raise ValidationFailure(f"{name} must be non-negative")
    freshness = float(step_a["freshness"])
    if not 0.0 <= freshness <= 1.0:
        raise ValidationFailure(f"freshness must be within 0.00..1.00, got {freshness:.2f}")
    if step_a["errors"] or step_b["errors"]:
        raise ValidationFailure(
            "CONNECT reported errors: "
            f"STEP_A.errors={step_a['errors']} STEP_B.errors={step_b['errors']}"
        )
    return ConnectReport(step_a=step_a, step_b=step_b)


def connect_run_id_statement(run_date: dt.date, project_key: str) -> sa.Select:
    """SELECT one project's `connect` row for a given date.

    This validator WRITES on the row it returns — it marks it `partial`. Without
    a project filter, across several projects, it would mark one project's
    failure on another project's row (spec §12).
    """
    return (
        sa.select(dream_runs.c.id)
        .where(dream_runs.c.phase == "connect")
        .where(dream_runs.c.run_date == run_date)
        .where(dream_runs.c.project_key == project_key)
        .order_by(dream_runs.c.id.desc())
        .limit(1)
    )


async def _mark_latest_connect_partial(
    session_factory: async_sessionmaker[AsyncSession],
    run_date: dt.date,
    error_message: str,
    project_key: str,
) -> bool:
    async with session_factory() as session:
        async with session.begin():
            run_id = (
                await session.execute(connect_run_id_statement(run_date, project_key))
            ).scalar_one_or_none()
            if run_id is None:
                return False
            await session.execute(
                sa.update(dream_runs)
                .where(dream_runs.c.id == run_id)
                .values(status="partial", error_message=error_message[:_MAX_ERROR_LENGTH])
            )
    return True


def _build_factory(postgres_url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(postgres_url, pool_pre_ping=True)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-log", required=True)
    parser.add_argument("--run-date", required=True, type=dt.date.fromisoformat)
    parser.add_argument(
        "--project-key",
        required=True,
        help="Project the CONNECT row belongs to — required, deliberately without a default",
    )
    args = parser.parse_args(argv)

    try:
        raw = Path(args.report_log).read_text(encoding="utf-8")
        report = parse_report(raw)
    except (OSError, UnicodeError, ValidationFailure) as exc:
        detail = (
            str(exc)
            if isinstance(exc, ValidationFailure)
            else f"failed to read CONNECT report log: {exc}"
        )
        detail = _bounded_detail(detail)
        session_factory = _build_factory(get_settings().postgres_url)
        try:
            marked = asyncio.run(
                _mark_latest_connect_partial(
                    session_factory, args.run_date, detail, args.project_key
                )
            )
        except Exception as marker_exc:  # noqa: BLE001
            detail = _bounded_detail(
                detail,
                f"failed to mark CONNECT dream_runs row partial: {marker_exc}",
            )
        else:
            if not marked:
                detail = _bounded_detail(
                    detail, f"no CONNECT dream_runs row for {args.project_key} on run date"
                )
        print(f"CONNECT VALIDATION FAILED: {detail}", file=sys.stderr)
        return 1

    print(
        "CONNECT VALIDATE: OK — "
        f"STEP_A.errors={report.step_a['errors']} "
        f"STEP_B.errors={report.step_b['errors']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
