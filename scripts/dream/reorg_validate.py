"""REORG-phase post-run validator — mirror of promote_validate.py.

Parses the machine-readable trailer produced by the REORG LLM agent:

    === REORG REPORT ===
    {"dry_run": <bool>, "updated": [full-UUIDs], "archived": [full-UUIDs]}
    === END ===

Cross-checks actual state in PostgreSQL to surface masked failures (agent exits
0 but nothing changed).

Design decisions:

- FAIL-CLOSED: if the REORG REPORT block is absent and the run is wet (not
  dry), the validator raises ValidationFailure — the exact same approach used
  by promote_validate for a missing PROMOTE REPORT block.  Dry-run reports are
  allowed to omit the trailer (validator logs a warning and returns); in
  practice the prompt instructs the agent to always emit the block.

- DRY_RUN: detected from ``dry_run: true`` inside the JSON trailer (primary),
  OR from the ``--dry-run`` CLI flag (authoritative override, belt+suspenders).
  In dry-run mode all DB checks are skipped and a clear log line is emitted.
  We do NOT verify the inverse (nothing changed) because a dry run may follow
  a previous wet run whose mutations are already committed.

- WET archived_ids: each entity must have ``freshness_status='archived'`` in PG.
  Not-archived → ValidationFailure (masked failure).  Not-found → ValidationFailure.

- WET updated_ids: each entity must exist in PG AND have
  ``updated_at >= run_date`` (i.e., the entity was actually touched during
  this run).  Existence + recency together catch both hallucinated UUIDs and
  Part 1 masked failures (agent claims 20 metadata updates, performs none).

- CAP ENFORCEMENT: more than 20 updated_ids or archived_ids violates the
  phase_reorg.md contract and is flagged as ValidationFailure.

- Both ``learnings`` and ``decisions`` are searched for each entity ID. REORG
  only touches these two entity types per phase_reorg.md.

- Failure philosophy: ValidationFailure marks the dream_runs row 'partial'
  and exits 1, but NEVER raises to the shell as an unhandled exception.
  Same philosophy as promote_validate.py.

CLI:
    python -m scripts.dream.reorg_validate \\
        --report-log logs/dream/2026-07-02_reorg.log \\
        --dream-run-id 42 \\
        --run-date 2026-07-02 \\
        [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
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
from brain_v42.db.tables import decisions, dream_runs, learnings

# Machine-readable trailer inserted by the agent after the prose report.
# Mirrors the PROMOTE REPORT block so tooling parses both the same way.
_REPORT_RE = re.compile(
    r"===\s*REORG\s+REPORT\s*===\s*(\{.*?\})\s*===\s*END\s*===",
    re.DOTALL,
)

# Maximum mutations per run as specified in phase_reorg.md guardrails.
_MAX_UPDATED = 20
_MAX_ARCHIVED = 20


class ValidationFailure(Exception):
    """Any violation of the REORG report contract."""


def parse_report(raw: str) -> dict:
    """Extract the JSON trailer from the REORG report.

    Returns a dict with keys:
      ``updated_ids``: list[str]   — full UUIDs from the ``updated`` field
      ``archived_ids``: list[str]  — full UUIDs from the ``archived`` field
      ``dry_run``: bool            — from the ``dry_run`` field in the JSON
      ``found_marker``: bool       — True when the REORG REPORT block was present

    Raises ValidationFailure only if the marker IS present but the JSON is
    malformed.  When the marker is absent ``found_marker`` is False and the
    caller (``validate``) decides whether to fail-close.
    """
    m = _REPORT_RE.search(raw)
    if m is None:
        return {
            "updated_ids": [],
            "archived_ids": [],
            "dry_run": False,
            "found_marker": False,
        }

    try:
        payload = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        raise ValidationFailure(f"malformed REORG REPORT JSON: {exc}") from exc

    # Deduplicate while preserving order — the JSON may theoretically repeat
    # a UUID if the agent listed the same entity twice; normalise early so
    # cap-enforcement counts are accurate.
    def _dedup(ids: list) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for x in ids:
            s = str(x)
            if s not in seen:
                seen.add(s)
                out.append(s)
        return out

    return {
        "updated_ids": _dedup(payload.get("updated", [])),
        "archived_ids": _dedup(payload.get("archived", [])),
        "dry_run": bool(payload.get("dry_run", False)),
        "found_marker": True,
    }


async def _entity_row(
    session: AsyncSession,
    entity_id: UUID,
) -> dict | None:
    """Return id, freshness_status, updated_at, project_key from learnings or decisions.

    Returns None if not found in either table.
    """
    for tbl in (learnings, decisions):
        row = (
            (
                await session.execute(
                    sa.select(
                        tbl.c.id,
                        tbl.c.freshness_status,
                        tbl.c.updated_at,
                        tbl.c.project_key,
                    ).where(tbl.c.id == entity_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is not None:
            return dict(row)
    return None


def _reject_foreign_project(
    raw_id: str,
    row: dict,
    project_key: str | None,
    *,
    claim: str,
) -> None:
    """Fail when a mutated entity does not belong to the run's project.

    A ``None`` perimeter disables the check rather than guessing one: inventing a
    scope here would turn a missing wire-up into confident nonsense.
    """
    if project_key is None:
        return
    if row["project_key"] != project_key:
        raise ValidationFailure(
            f"entity {raw_id} (claimed {claim}) belongs to project "
            f"{row['project_key']!r}, expected project {project_key!r} — "
            f"cross-project mutation; the server-side capability scope should have "
            f"made this impossible, so treat it as an enforcement regression"
        )


async def validate(
    report: dict,
    session_factory: async_sessionmaker[AsyncSession],
    dream_run_id: int | None,
    run_date: dt.date | None = None,
    project_key: str | None = None,
) -> None:
    """Verify that the entities the agent claimed to mutate actually changed.

    PROJECT: when ``project_key`` is given, every mutated entity must belong to it.
    Defense in depth — the server already bounds REORG to its project twice (the
    middleware injects ``project_key`` into ``brain_list`` arguments and denies a
    divergent one; all five repositories carry ``AND project_key = :scope`` in the
    UPDATE's WHERE). The reason to check again HERE is measured: ``brain_list`` is
    the only CRUD tool that never calls ``get_dream_project_scope()`` itself, so its
    whole bound lives in the middleware — and ``brain_dream_capability_enforcement``
    defaults to False in code. Should enforcement ever drop, REORG would repaginate
    the entire corpus in silence and nothing downstream would say so. Parity with
    promote_validate, which has refused an out-of-project ADR or runbook all along.

    DRY-RUN: skips all DB checks (nothing should have mutated).
    MISSING MARKER (wet): raises ValidationFailure (fail-closed).
    WET archived_ids: each entity must have freshness_status='archived' in PG.
    WET updated_ids: each entity must exist AND have updated_at >= run_date.
    CAP: > 20 updated or archived raises ValidationFailure.

    Raises ValidationFailure on any integrity violation.
    """
    dry_run: bool = bool(report.get("dry_run"))
    updated_ids: list[str] = report.get("updated_ids", [])
    archived_ids: list[str] = report.get("archived_ids", [])
    found_marker: bool = bool(report.get("found_marker", True))

    if dry_run:
        # Nothing should have changed in dry-run mode. Skip DB checks with
        # an explicit log so operators can audit the validator decision.
        print(
            "REORG VALIDATE: dry-run mode — skipping DB integrity checks",
            file=sys.stderr,
        )
        return

    if not found_marker:
        raise ValidationFailure(
            "missing REORG REPORT markers — agent did not emit a machine-readable "
            "trailer; cannot verify integrity (wet run)"
        )

    # Cap enforcement — phase_reorg.md guardrail: max 20 per section
    if len(updated_ids) > _MAX_UPDATED:
        raise ValidationFailure(
            f"updated_ids count {len(updated_ids)} exceeds cap {_MAX_UPDATED} "
            f"(phase_reorg.md Part 1 guardrail)"
        )
    if len(archived_ids) > _MAX_ARCHIVED:
        raise ValidationFailure(
            f"archived_ids count {len(archived_ids)} exceeds cap {_MAX_ARCHIVED} "
            f"(phase_reorg.md Part 2 guardrail)"
        )

    if not updated_ids and not archived_ids:
        # Empty report: agent did nothing (valid — small corpus, all clean).
        return

    async with session_factory() as session:
        for raw_id in archived_ids:
            try:
                entity_id = UUID(raw_id)
            except ValueError as exc:
                raise ValidationFailure(f"malformed UUID in archived_ids: {raw_id!r}") from exc

            row = await _entity_row(session, entity_id)
            if row is None:
                raise ValidationFailure(
                    f"entity {raw_id} (claimed archived) not found in learnings or decisions"
                )
            _reject_foreign_project(raw_id, row, project_key, claim="archived")
            if row["freshness_status"] != "archived":
                raise ValidationFailure(
                    f"entity {raw_id} claimed archived but freshness_status="
                    f"{row['freshness_status']!r} — masked failure"
                )

        for raw_id in updated_ids:
            try:
                entity_id = UUID(raw_id)
            except ValueError as exc:
                raise ValidationFailure(f"malformed UUID in updated_ids: {raw_id!r}") from exc

            row = await _entity_row(session, entity_id)
            if row is None:
                raise ValidationFailure(
                    f"entity {raw_id} (claimed updated) not found in learnings or decisions"
                )

            _reject_foreign_project(raw_id, row, project_key, claim="updated")

            # updated_at freshness check: the agent must have touched the entity
            # during (or after) run_date.  Without this, an existence check alone
            # can never detect a Part 1 masked failure — every claimed ID always
            # exists because the agent sourced it from brain_list scans.
            if run_date is not None:
                entity_updated_at: dt.datetime = row["updated_at"]
                # updated_at is tz-aware (PostgreSQL timestamptz); compare as date.
                if entity_updated_at.date() < run_date:
                    raise ValidationFailure(
                        f"entity {raw_id} (claimed updated) has updated_at="
                        f"{entity_updated_at.date().isoformat()} which is before "
                        f"run_date={run_date.isoformat()} — masked failure (no write performed)"
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
    parser.add_argument("--report-log", required=True, help="Path to the reorg phase log file")
    parser.add_argument(
        "--dream-run-id",
        type=int,
        default=None,
        help="dream_runs.id for this run (optional; used to mark partial on failure)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Force dry-run mode (skips DB checks); also detected from JSON trailer",
    )
    parser.add_argument(
        "--run-date",
        type=dt.date.fromisoformat,
        default=None,
        help="Date of the dream run (YYYY-MM-DD); used to check updated_at >= run_date",
    )
    parser.add_argument(
        "--project-key",
        default=None,
        help=(
            "Perimeter the run was launched with; every mutated entity must belong "
            "to it. Optional so an operator can validate a log out of band, but "
            "dream.sh always passes it (pinned by tests/unit/test_reorg_validate.py)"
        ),
    )
    args = parser.parse_args(argv)

    with open(args.report_log) as fh:
        raw = fh.read()

    settings = Settings()
    session_factory = _build_factory(settings.postgres_url)

    try:
        report = parse_report(raw)
        # CLI --dry-run flag is authoritative over JSON trailer (belt+suspenders)
        if args.dry_run:
            report = {**report, "dry_run": True}
        asyncio.run(
            validate(
                report,
                session_factory,
                args.dream_run_id,
                args.run_date,
                project_key=args.project_key,
            )
        )
        mode = "dry-run" if report.get("dry_run") else "wet"
        print(
            "REORG VALIDATE: OK — "
            f"mode={mode} updated={len(report.get('updated_ids', []))} "
            f"archived={len(report.get('archived_ids', []))}",
            file=sys.stderr,
        )
    except ValidationFailure as exc:
        asyncio.run(_mark_dream_run_partial(session_factory, args.dream_run_id, str(exc)))
        print(f"REORG VALIDATION FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
