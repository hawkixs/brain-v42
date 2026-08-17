"""PROMOTE-phase post-run validator.

Extracts the ``=== PROMOTE REPORT === ... === END ===`` block from the LLM's
stdout, enforces referential-integrity invariants, and writes audit rows for
skip paths. Marks dream_runs.status='partial' on any integrity failure.

Invoked by dream.sh after the LLM call — exit 0 on success, exit 1 on
validation failure.

CLI:
    python -m scripts.dream.promote_validate \\
        --report-log logs/dream/2026-04-17_promote.log \\
        --candidates-json /tmp/promote_candidates.json \\
        --project-key brain-v42 \\
        --dream-run-id 42
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from brain_v42.config import Settings
from brain_v42.db.tables import adrs, dream_promotions, dream_runs, runbooks

_REPORT_RE = re.compile(
    r"===\s*PROMOTE\s+REPORT\s*===\s*(\{.*?\})\s*===\s*END\s*===",
    re.DOTALL,
)

VALID_TARGET_TYPES = {
    "adr",
    "runbook",
    "skipped_dedup",
    "dry_run",
    "classification_uncertain",
    "dedup_unavailable",
    "none",
}


class ValidationFailure(Exception):
    """Any violation of the PROMOTE report contract."""


def parse_report(raw: str) -> dict:
    """Extract the JSON report block between the PROMOTE markers."""
    m = _REPORT_RE.search(raw)
    if m is None:
        raise ValidationFailure("missing PROMOTE REPORT markers")
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise ValidationFailure(f"malformed JSON: {e}") from e


async def validate(
    report: dict,
    candidates: list[dict],
    session_factory: async_sessionmaker[AsyncSession],
    dream_run_id: int | None,
    project_key: str,
) -> None:
    """Enforce referential integrity and write audit rows for skip paths.

    Raises ValidationFailure on any contract violation. For ADR/runbook
    target types the repo wrote the audit row atomically via
    create_with_promotion (T2/T3) — this validator only asserts the row
    exists. For skip paths the validator owns the INSERT.

    ``project_key`` is the scope the run was launched with. Until the v2
    delivery order (spec §8, lot 1) this validator never looked at it, which is
    invisible at one project and unsafe at 55: a promotion landing in the wrong
    project is a referential-integrity violation like any other here, so it
    fails the run rather than being recorded. The caller marks dream_runs
    partial, as it already does for every other ValidationFailure.
    """
    target_type = report.get("target_type")
    if target_type not in VALID_TARGET_TYPES:
        raise ValidationFailure(f"invalid target_type={target_type!r}")

    if target_type == "none":
        return  # agent reported no work; nothing to audit.

    candidate_id = report.get("candidate_id")
    if not candidates or candidate_id != candidates[0]["id"]:
        top = candidates[0]["id"] if candidates else None
        raise ValidationFailure(
            f"candidate_id {candidate_id!r} does not match candidates[0].id={top!r}"
        )

    source_uuid = UUID(candidate_id)
    target_id = report.get("target_id")
    dry_run = bool(report.get("dry_run"))

    async with session_factory() as session:
        async with session.begin():
            if target_type == "adr" and not dry_run:
                adr_uuid = UUID(target_id)
                adr_row = (
                    (
                        await session.execute(
                            sa.select(adrs.c.status, adrs.c.project_key).where(
                                adrs.c.id == adr_uuid
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                status = adr_row["status"] if adr_row is not None else None
                if status != "accepted":
                    raise ValidationFailure(
                        f"ADR {target_id} not found or not accepted (status={status!r})"
                    )
                if adr_row["project_key"] != project_key:
                    raise ValidationFailure(
                        f"ADR {target_id} belongs to project "
                        f"{adr_row['project_key']!r}, expected project {project_key!r}"
                    )
                count = (
                    await session.execute(
                        sa.select(sa.func.count())
                        .select_from(dream_promotions)
                        .where(dream_promotions.c.target_adr_id == adr_uuid)
                    )
                ).scalar_one()
                if count != 1:
                    raise ValidationFailure(
                        f"expected 1 dream_promotions row for adr {target_id}, got {count}"
                    )
                # Backfill dream_run_id: the repo inserted the audit row with
                # dream_run_id=NULL because the agent has no knowledge of the
                # run id at tool-call time. The validator does — close the loop.
                if dream_run_id is not None:
                    await session.execute(
                        sa.update(dream_promotions)
                        .where(dream_promotions.c.target_adr_id == adr_uuid)
                        .values(dream_run_id=dream_run_id)
                    )
                return

            if target_type == "runbook" and not dry_run:
                rb_uuid = UUID(target_id)
                rb_row = (
                    (
                        await session.execute(
                            sa.select(runbooks.c.id, runbooks.c.project_key).where(
                                runbooks.c.id == rb_uuid
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if rb_row is None:
                    raise ValidationFailure(f"runbook {target_id} not found")
                if rb_row["project_key"] != project_key:
                    raise ValidationFailure(
                        f"runbook {target_id} belongs to project "
                        f"{rb_row['project_key']!r}, expected project {project_key!r}"
                    )
                count = (
                    await session.execute(
                        sa.select(sa.func.count())
                        .select_from(dream_promotions)
                        .where(dream_promotions.c.target_runbook_id == rb_uuid)
                    )
                ).scalar_one()
                if count != 1:
                    raise ValidationFailure(
                        f"expected 1 dream_promotions row for runbook {target_id}, got {count}"
                    )
                if dream_run_id is not None:
                    await session.execute(
                        sa.update(dream_promotions)
                        .where(dream_promotions.c.target_runbook_id == rb_uuid)
                        .values(dream_run_id=dream_run_id)
                    )
                return

            # Skip paths + dry_run — validator owns the audit INSERT.
            skip_type = "dry_run" if dry_run else target_type
            cosine = report.get("cosine_observed") if skip_type == "skipped_dedup" else None
            reason = report.get("reason")
            await session.execute(
                sa.text(
                    """
                    INSERT INTO dream_promotions (
                        dream_run_id, source_learning_id, target_type,
                        cosine_observed, skipped_reason
                    ) VALUES (:run, :src, :typ, :cos, :reason)
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "run": dream_run_id,
                    "src": source_uuid,
                    "typ": skip_type,
                    "cos": cosine,
                    "reason": reason,
                },
            )


async def _mark_dream_run_partial(
    session_factory: async_sessionmaker[AsyncSession],
    dream_run_id: int | None,
    error_message: str,
) -> None:
    """Flip a dream_runs row to status='partial' with the failure message."""
    if dream_run_id is None:
        return
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                sa.update(dream_runs)
                .where(dream_runs.c.id == dream_run_id)
                .values(status="partial", error_message=error_message)
            )


def _build_factory(postgres_url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(postgres_url, pool_pre_ping=True)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-log", required=True)
    parser.add_argument("--candidates-json", required=True)
    parser.add_argument("--dream-run-id", type=int, default=None)
    # Required, not defaulted to "brain-v42": a default would silently validate
    # every project against the wrong scope the day the loop opens, which is the
    # exact class of bug this argument exists to catch.
    parser.add_argument("--project-key", required=True)
    args = parser.parse_args(argv)

    with open(args.report_log) as fh:
        raw = fh.read()
    with open(args.candidates_json) as fh:
        candidates = json.load(fh)

    settings = Settings()
    session_factory = _build_factory(settings.postgres_url)

    try:
        report = parse_report(raw)
        asyncio.run(
            validate(
                report,
                candidates,
                session_factory,
                args.dream_run_id,
                project_key=args.project_key,
            )
        )
    except ValidationFailure as exc:
        asyncio.run(_mark_dream_run_partial(session_factory, args.dream_run_id, str(exc)))
        print(f"PROMOTE VALIDATION FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
