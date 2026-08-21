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
import json
import pathlib
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
from scripts.dream.reorg_events import EventScan, scan_events

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
    to have one — « a ``None`` perimeter disables the check rather than guessing
    one » — which read as prudence and behaved as silence: the validator still
    printed ``REORG VALIDATE: OK`` while checking no perimeter at all, so a
    drapeau dropped from dream.sh's argument array would have looked like a
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
    """Validation ET marquage dans UNE SEULE boucle — voir `main`.

    Le marquage ne peut aboutir que dans la boucle qui a servi la validation :
    `asyncio.run` ferme la sienne en sortant, et `pool_pre_ping=True` retouche
    depuis la boucle suivante des connexions attachées à la boucle morte. Le
    chemin d'échec est le SEUL à enchaîner deux usages du pool, donc le seul à
    l'avoir rencontré — et c'est exactement le chemin qui doit marcher.
    """
    try:
        report = parse_report(raw)
        # CLI --dry-run flag is authoritative over JSON trailer (belt+suspenders)
        if args.dry_run:
            report = {**report, "dry_run": True}
        # Avant `validate`, pour que le verdict de symétrie s'imprime même quand
        # la validation échoue juste après — c'est la nuit qui échoue qui a le
        # plus besoin d'être lue.
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
