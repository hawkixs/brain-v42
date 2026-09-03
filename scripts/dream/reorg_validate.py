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

- WET updated_ids: each entity must exist in PG AND carry tags that DIFFER from
  the pre-phase snapshot (``--tags-before-json``, written by
  ``scripts.dream.reorg_snapshot``).  Existence + movement together catch both
  hallucinated UUIDs and Part 1 masked failures (agent claims 20 metadata
  updates, performs none).  The snapshot is the only measured ``before``:
  ``updated_at`` is bumped every 300 s by DecayFlusher through an unconditional
  trigger — partly by REORG's own reads — and migration 041's
  ``content_updated_at`` triggers do not watch ``tags`` at all.

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
        --project-key brain-v42 \\
        --tags-before-json logs/dream/2026-07-02_brain-v42_reorg_tags_before.json \\
        --dream-run-id 42 \\
        [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import pathlib
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
from scripts.dream.reorg_events import EventScan, scan_events
from scripts.dream.reorg_report import parse_trailer

# Machine-readable trailer inserted by the agent after the prose report.
# Mirrors the PROMOTE REPORT block so tooling parses both the same way.
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
      ``declared``: DeclaredTally | None — the phase's own Part 2 tally, or None
                                    when the trailer predates it. NOT evidence.
      ``declared_malformed``: bool — a ``declared`` key that could not be read,
                                    which is a different fact from its absence

    Reading is delegated to ``scripts.dream.reorg_report``, the single reader
    shared with the morning report. It used to be two regexes that had to agree
    with nothing enforcing it, and they had already stopped agreeing: the
    alert-side pattern truncated any nested object at its first inner brace.

    Raises ValidationFailure only if the marker IS present but the JSON is
    malformed.  When the marker is absent ``found_marker`` is False and the
    caller (``validate``) decides whether to fail-close.
    """
    try:
        report = parse_trailer(raw)
    except ValueError as exc:
        raise ValidationFailure(str(exc)) from exc

    return {
        "updated_ids": report.updated_ids,
        "archived_ids": report.archived_ids,
        "dry_run": report.dry_run,
        "found_marker": report.found_marker,
        "declared": report.declared,
        "declared_malformed": report.declared_malformed,
        # The parsed object itself, so checks that belong to it are CALLED here
        # rather than reimplemented. A private copy of `declared_list_mismatch`
        # lived below until the review of 2026-09-04: the copy under test was
        # the dead one, and the copy running every night was tested by nothing.
        "parsed": report,
    }


def apply_dry_run_override(report: dict) -> dict:
    """Apply the authoritative CLI `--dry-run` to BOTH carriers of that fact.

    The flag exists to distrust the trailer: `dream.sh` passes `--dry-run`
    whenever `reorg_effective_dry_run` is true, whatever the agent wrote in its
    JSON. So the override has to reach every reader of "is this a dry run".

    There are two, and until 2026-09-04 only one moved. The dict key was
    replaced by a shallow copy while `report["parsed"]` — the frozen
    `ReorgReport` whose `dry_run` comes straight from the trailer — kept the
    value the flag was overriding. `declared_list_mismatch()` gates on that
    object, so the dry-run relaxation was defeated in exactly the belt-and-
    suspenders case it was written for: a dry night whose trailer claims
    `dry_run: false` raised the false alarm the relaxation removes.

    Returns a NEW dict and a NEW frozen report; the caller's originals are
    untouched, because a function that mutates a frozen dataclass's container
    behind the caller's back is the next version of this same bug.
    """
    parsed = report.get("parsed")
    updated = {**report, "dry_run": True}
    if parsed is not None:
        updated["parsed"] = dataclasses.replace(parsed, dry_run=True)
    return updated


def declared_warnings(report: dict) -> list[str]:
    """Check the phase's self-reported tally, and say what that is worth.

    These numbers describe entities REORG looked at and did not touch. No call
    was made, so nothing in PostgreSQL and nothing in the event stream can
    confirm them. Two checks are available and both are weak, which is why they
    warn and never fail:

    - the tally against ITSELF, so a careless count is caught;
    - the declared archive COUNT against the declared archive LIST, the one
      statement in this report that a database can answer.

    A tally is not required. A trailer without one comes from a phase running an
    older prompt, and treating that as a fault would print a warning every night
    of a rollback — noise for a state that is merely old.
    """
    if report.get("declared_malformed"):
        return [
            "the report carried a `declared` block that is unusable — this is NOT the "
            "same fact as a trailer without one, which would simply be an older prompt"
        ]

    declared = report.get("declared")
    if declared is None:
        return []

    warnings: list[str] = []
    complaint = declared.arithmetic_complaint()
    if complaint is not None:
        warnings.append(complaint)
    unknown = declared.unknown_reasons()
    if unknown:
        warnings.append(
            f"declared refusal reason(s) outside the vocabulary phase_reorg.md defines: "
            f"{', '.join(unknown)} — prompt and reader have drifted"
        )
    return warnings


async def _entity_row(
    session: AsyncSession,
    entity_id: UUID,
) -> dict | None:
    """Return id, freshness_status, tags, project_key from learnings or decisions.

    Returns None if not found in either table.
    """
    for tbl in (learnings, decisions):
        row = (
            (
                await session.execute(
                    sa.select(
                        tbl.c.id,
                        tbl.c.freshness_status,
                        tbl.c.tags,
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
    project_key: str,
    *,
    claim: str,
) -> None:
    """Fail when a mutated entity does not belong to the run's project.

    The perimeter is REQUIRED, and deliberately has no ``None`` branch. It used
    to have one — "a ``None`` perimeter disables the check rather than guessing
    one" — which read as prudence and behaved as silence: the validator still
    printed ``REORG VALIDATE: OK`` while checking no perimeter at all, so a
    flag dropped from dream.sh's argument array would have looked like a
    clean night. Parity with promote_validate and connect_validate, which have
    both required it all along.
    """
    if row["project_key"] != project_key:
        raise ValidationFailure(
            f"entity {raw_id} (claimed {claim}) belongs to project "
            f"{row['project_key']!r}, expected project {project_key!r} — "
            f"cross-project mutation; the server-side capability scope should have "
            f"made this impossible, so treat it as an enforcement regression"
        )


def symmetry_warnings(report: dict, scan: EventScan) -> list[str]:
    """Compare what the report DECLARED to what the event stream OBSERVED.

    Both directions, because they fail differently:

    - *declared, never called* is the ``bccc9115`` ghost. The report names an id
      no ``brain_update`` was ever emitted for.
    - *called, never declared* is invisible today, and it is the worse of the two:
      a mutation neither the validator, nor the alert, nor the briefing mentions.
      It exists only in a stream nobody re-reads.

    Archived ids count as declared. ``phase_reorg.md`` §Part 2 d archives through
    the same ``brain_update``, so comparing against ``updated`` alone would
    denounce every archive as an undeclared mutation and make the check shout at
    its own nominal behaviour every night.

    An unreadable stream gets ONE warning naming that inability, never a per-id
    verdict. Its ``updated_ids`` is empty, so a naive comparison would denounce
    every declared id at once — a massive false alarm one quickly learns to
    ignore — while silence would make "nothing wrong" indistinguishable from
    "nothing was read".

    WARNINGS ONLY, by design and for now: escalating to a failure waits for a
    clean week of observation. A guard that starts by failing nights it has never
    been measured against teaches operators to disable it.
    """
    declared = set(report.get("updated_ids", [])) | set(report.get("archived_ids", []))

    if not scan.recognised:
        return [
            "event stream carried no recognisable codex or agy tool call — symmetry "
            "UNVERIFIED (this is NOT the same fact as zero mutations: a new agent "
            f"format or an empty stream reads identically); {len(declared)} id(s) declared"
        ]

    warnings: list[str] = []

    ghosts = sorted(declared - scan.updated_ids)
    if ghosts:
        warnings.append(
            f"{len(ghosts)} id(s) declared in the report but never passed to "
            f"brain_update in the event stream: {', '.join(ghosts)}"
        )

    undeclared = sorted(scan.updated_ids - declared)
    if undeclared:
        warnings.append(
            f"{len(undeclared)} id(s) mutated through brain_update but absent from the "
            f"report: {', '.join(undeclared)}"
        )

    # AFTER the id-level verdict, never instead of it. The tally rides along
    # because the morning line needs it; it can add a warning and can never
    # remove or satisfy one above (learning c34fb865).
    warnings.extend(declared_warnings(report))
    parsed = report.get("parsed")
    mismatch = parsed.declared_list_mismatch() if parsed is not None else None
    if mismatch is not None:
        warnings.append(mismatch)

    return warnings


async def validate(
    report: dict,
    session_factory: async_sessionmaker[AsyncSession],
    dream_run_id: int | None,
    project_key: str,
    tags_before: dict[str, list[str]],
) -> None:
    """Verify that the entities the agent claimed to mutate actually changed.

    PROJECT: every mutated entity must belong to ``project_key``, which is REQUIRED.
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
    WET updated_ids: each entity must exist AND carry tags that differ from
    ``tags_before``, the snapshot taken just before the phase started.
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

            # TAG-MOVEMENT check: the entity's tags must differ from the
            # pre-phase snapshot.  Without it, an existence check alone can never
            # detect a Part 1 masked failure — every claimed ID always exists,
            # because the agent sourced it from its own brain_list scans.
            #
            # This replaces an `updated_at >= run_date` check that was hollow:
            # DecayFlusher bulk-UPDATEs both tables every 300 s and the migration
            # 001 trigger has no WHEN clause, so the timestamp moved on its own.
            # The circuit was worse than the drift — the access rows that drive
            # the flusher come from REORG's own brain_get reads, so the phase
            # manufactured the evidence it was being judged on.
            if raw_id not in tags_before:
                raise ValidationFailure(
                    f"entity {raw_id} (claimed updated) is absent from the pre-phase "
                    f"tags snapshot — it did not exist in project {project_key!r} when "
                    f"the phase started. REORG normalises existing metadata and never "
                    f"creates; treat this as a snapshot taken on the wrong corpus or a "
                    f"phase that stepped outside its contract"
                )
            # Sorted comparison: a pure permutation of identical tags is not a
            # normalisation, so it must not count as movement. Duplicates survive
            # sorting, which is right — de-duplicating IS a real normalisation.
            if sorted(row["tags"] or []) == sorted(tags_before[raw_id]):
                raise ValidationFailure(
                    f"entity {raw_id} (claimed updated) still carries the same tags as "
                    f"before the phase ({sorted(tags_before[raw_id])!r}) — masked "
                    f"failure (no write performed)"
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


def _emit_symmetry(report: dict, events_path: str) -> None:
    """Print the report↔stream symmetry verdict, and never raise.

    Read here rather than in ``main`` so a single fact produces a single line:
    an unreadable stream would otherwise also trip ``scan.recognised``, and two
    warnings for one cause is how an alert stops being read.
    """
    try:
        events_raw = pathlib.Path(events_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(
            f"REORG SYMMETRY WARN: event stream {events_path!r} unreadable ({exc}) — "
            f"symmetry UNVERIFIED",
            file=sys.stderr,
        )
        return
    for warning in symmetry_warnings(report, scan_events(events_raw)):
        print(f"REORG SYMMETRY WARN: {warning}", file=sys.stderr)


async def _amain(
    raw: str,
    tags_before: dict[str, list[str]],
    session_factory: async_sessionmaker[AsyncSession],
    args: argparse.Namespace,
) -> int:
    """Validation AND marking in ONE SINGLE loop — see `main`.

    The marking can only land in the loop that served the validation:
    `asyncio.run` closes its own on exit, and `pool_pre_ping=True` reaches, from
    the next loop, connections attached to the dead one. The failure path is the
    ONLY one to chain two uses of the pool, hence the only one to have met it —
    and it is exactly the path that has to work.
    """
    try:
        report = parse_report(raw)
        # CLI --dry-run flag is authoritative over JSON trailer (belt+suspenders).
        # Applied through the helper so BOTH carriers of the fact move together.
        if args.dry_run:
            report = apply_dry_run_override(report)
        # Before `validate`, so the symmetry verdict prints even when the
        # validation fails right after — the night that fails is the one that
        # most needs reading.
        _emit_symmetry(report, args.events_jsonl)
        await validate(
            report,
            session_factory,
            args.dream_run_id,
            args.project_key,
            tags_before,
        )
    except ValidationFailure as exc:
        await _mark_dream_run_partial(session_factory, args.dream_run_id, str(exc))
        print(f"REORG VALIDATION FAILED: {exc}", file=sys.stderr)
        return 1
    mode = "dry-run" if report.get("dry_run") else "wet"
    print(
        "REORG VALIDATE: OK — "
        f"mode={mode} updated={len(report.get('updated_ids', []))} "
        f"archived={len(report.get('archived_ids', []))}",
        file=sys.stderr,
    )
    return 0


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
        "--events-jsonl",
        required=True,
        help=(
            "Path to the phase event stream (codex or agy JSONL). Used for the "
            "report-vs-observed symmetry check, which WARNS and never fails — "
            "escalation waits for a clean week of observation"
        ),
    )
    parser.add_argument(
        "--tags-before-json",
        required=True,
        help=(
            "Path to the pre-phase tags snapshot written by scripts.dream.reorg_snapshot "
            "— required, deliberately without a default. It is the only measured `before`: "
            "updated_at moves on its own (DecayFlusher + an unconditional trigger) and "
            "content_updated_at ignores `tags` entirely"
        ),
    )
    parser.add_argument(
        "--project-key",
        required=True,
        help=(
            "Perimeter the run was launched with; every mutated entity must belong "
            "to it — required, deliberately without a default. An out-of-band "
            "replay names the project it is replaying (pinned by "
            "tests/unit/test_reorg_validate.py)"
        ),
    )
    args = parser.parse_args(argv)

    with open(args.report_log) as fh:
        raw = fh.read()
    with open(args.tags_before_json) as fh:
        tags_before = json.load(fh)

    settings = Settings()
    session_factory = _build_factory(settings.postgres_url)

    return asyncio.run(_amain(raw, tags_before, session_factory, args))


if __name__ == "__main__":
    sys.exit(main())
