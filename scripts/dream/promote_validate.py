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

# Matched anywhere in the text, not on an exact
# '=== PROMOTE REPORT ===\s*{' sequence: the model has been observed
# appending a stray trailing word after the marker (night 2026-09-06,
# '=== PROMOTE REPORT === Bettina'), which the old single-regex match
# rejected outright even though the JSON body that followed was
# well-formed. The JSON block is located separately, from the end of the
# marker match up to the '=== END ===' sentinel — malformed JSON there
# still fails strictly (see test_parse_report_malformed_json_raises and its
# trailing-word twin).
#
# Deliberately NOT anchored with ^...$/re.MULTILINE: the codex rail writes
# the model's last message verbatim (--output-last-message), so the marker
# can share a line with leading prose too ('Voici le rapport.
# === PROMOTE REPORT ===') — line-anchoring would reject that even though
# it is a superset of both main's original behaviour and the trailing-word
# case (test_parse_report_tolerates_prose_prefix_on_marker_line).
_MARKER_RE = re.compile(
    r"===\s*PROMOTE\s+REPORT\s*===",
)
_REPORT_BODY_RE = re.compile(
    r"(\{.*?\})\s*===\s*END\s*===",
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

# NAMED RESIDUAL (lot P3, 2026-09-07). dream_promotions_target_shape
# (migration 017) enumerates exactly {'skipped_dedup', 'dry_run',
# 'classification_uncertain', 'dedup_unavailable'} for the "nothing
# materialized" branch. 'none' is a valid REPORT-level classification
# (VALID_TARGET_TYPES above) but is NOT admitted by that CHECK constraint —
# confirmed live against production: `INSERT ... target_type='none'` raises
# CheckViolation. Widening the CHECK to admit 'none' as a first-class,
# persisted value needs a migration; this lot owns promote_validate.py only
# (surface note) and deliberately does not write one.
#
# A 'none' report that names a real candidate (see `validate`) is instead
# filed under this bucket rather than 'classification_uncertain':
# promote_prepare.py's candidate query treats a recent
# 'classification_uncertain' verdict as terminal — it excludes the learning
# from future pools until its content changes — and a transient "the promote
# tool was unavailable" refusal must not silently blacklist an otherwise-good
# candidate. 'dedup_unavailable' carries no such exclusion in that query, so
# the candidate is simply re-offered another night. The literal reported
# value ('none') is preserved verbatim in skipped_reason (see
# _NONE_REFUSAL_REASON_MARKER) so the approximation is greppable and
# reversible once a migration adds 'none' as a first-class persisted value.
_NONE_REFUSAL_TARGET_TYPE = "dedup_unavailable"
_NONE_REFUSAL_REASON_MARKER = "[promote reported target_type=none]"


class ValidationFailure(Exception):
    """Any violation of the PROMOTE report contract."""


def parse_report(raw: str) -> dict:
    """Extract the JSON report block between the PROMOTE markers.

    The marker is matched loosely: text before or after
    ``=== PROMOTE REPORT ===`` on its line is tolerated, and the marker
    need not start the line. The JSON body is then located in the
    remainder of the text, up to ``=== END ===``.
    """
    marker_match = _MARKER_RE.search(raw)
    if marker_match is None:
        raise ValidationFailure("missing PROMOTE REPORT markers")
    body_match = _REPORT_BODY_RE.search(raw, marker_match.end())
    if body_match is None:
        raise ValidationFailure("missing PROMOTE REPORT markers")
    try:
        return json.loads(body_match.group(1))
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

    candidate_id = report.get("candidate_id")

    if target_type == "none" and candidate_id is None:
        # Genuine empty pool / no candidate identified at all: nothing
        # happened AND nothing was reported, so there is truly nothing to
        # audit. This is the ONLY 'none' shape that stays a silent no-op —
        # see test_validate_none_is_noop and
        # test_validate_none_with_pool_but_no_reported_candidate_is_still_noop.
        # A 'none' report that DOES name a candidate falls through below: it
        # is a refusal, not an empty run, and is an audited outcome (see the
        # skip-path branch further down and _NONE_REFUSAL_TARGET_TYPE above).
        return

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

            # Skip paths + dry_run + refused-none — validator owns the audit
            # INSERT. A 'none' report naming a candidate is a refusal, not a
            # dry run or a dedup/classification skip: it is bucketed under
            # _NONE_REFUSAL_TARGET_TYPE (see the module-level comment on that
            # constant for why) with the literal reported value tagged onto
            # skipped_reason so no information is silently lost.
            if target_type == "none":
                skip_type = _NONE_REFUSAL_TARGET_TYPE
                cosine = None
                raw_reason = report.get("reason")
                reason = (
                    f"{_NONE_REFUSAL_REASON_MARKER} {raw_reason}"
                    if raw_reason
                    else f"{_NONE_REFUSAL_REASON_MARKER} no reason given"
                )
            else:
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


async def _backfill_dream_run_id_from_candidate(
    session_factory: async_sessionmaker[AsyncSession],
    candidate_id: str,
    dream_run_id: int | None,
) -> None:
    """Backfill dream_promotions.dream_run_id for the night's top candidate.

    Deliberately independent of the PROMOTE report: create_with_promotion
    (T2/T3) writes the adr/runbook audit row with dream_run_id=NULL at
    tool-call time, before this script ever reads the LLM's stdout. Keying
    off ``candidate_id`` (== candidates[0]["id"], known from the candidates
    file alone) means a malformed or unparseable report — the night of
    2026-09-06, a stray word on the marker line — no longer orphans that
    row: production had dream_promotions.dream_run_id NULL for an ADR that
    WAS accepted, and dream_runs stuck on 'partial'.

    ``idx_dream_promotions_source_materialized`` guarantees at most one
    ('adr'|'runbook') row per source_learning_id, so this is unambiguous.
    A candidate with no such row yet (dry_run, skip path, or nothing
    materialized) simply updates zero rows.
    """
    if dream_run_id is None:
        return
    source_uuid = UUID(candidate_id)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                sa.update(dream_promotions)
                .where(
                    dream_promotions.c.source_learning_id == source_uuid,
                    dream_promotions.c.target_type.in_(("adr", "runbook")),
                    dream_promotions.c.dream_run_id.is_(None),
                )
                .values(dream_run_id=dream_run_id)
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


async def _amain(
    raw: str,
    candidates: list,
    session_factory: async_sessionmaker[AsyncSession],
    args: argparse.Namespace,
) -> int:
    """Validation AND marking in ONE SINGLE loop — the same defect as reorg.

    Found not by a night but by reading, after `reorg`'s failure of the 19th to
    20th: the shape was identical here, character for character. Only one of the
    two fired, because only `reorg` failed that night.

    The dream_run_id backfill runs FIRST and unconditionally, before
    parse_report is even attempted — it is keyed off the candidates file,
    not the report, precisely so a report the model garbled (2026-09-06:
    a stray word on the marker line) does not also orphan an audit row
    that create_with_promotion already wrote correctly.

    The backfill call itself is best-effort ("Best-effort — never raises",
    the repo idiom): a DB failure or a non-UUID candidates[0]["id"] used to
    sit outside this function's try/except and crash the process uncaught,
    printing no "PROMOTE VALIDATION FAILED" line and never marking the run
    'partial'. It is caught and warned to stderr so validation still runs.
    """
    if candidates:
        try:
            await _backfill_dream_run_id_from_candidate(
                session_factory, candidates[0]["id"], args.dream_run_id
            )
        except Exception as exc:  # noqa: BLE001 - best-effort, never raises
            print(f"WARN backfill dream_run_id skipped: {exc}", file=sys.stderr)
    try:
        report = parse_report(raw)
        await validate(
            report,
            candidates,
            session_factory,
            args.dream_run_id,
            project_key=args.project_key,
        )
    except ValidationFailure as exc:
        await _mark_dream_run_partial(session_factory, args.dream_run_id, str(exc))
        print(f"PROMOTE VALIDATION FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


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

    return asyncio.run(_amain(raw, candidates, session_factory, args))


if __name__ == "__main__":
    sys.exit(main())
