"""Post-run report for failed or incomplete Dream phases.

Closes the silent-crash observability gap (2026-05-02 / 05-03 PROMOTE crashes
went undetected for 2 days). Invoked by dream.sh at the end of a run when
FAIL_TOTAL > 0. Session briefings read failures directly from ``dream_runs``.

CLI:
    python -m scripts.dream.post_run_alert --date 2026-05-08 [--manifest PATH]

Exit codes:
    0  → nothing to report, or an anomaly ALREADY reported elsewhere (dream.sh
         itself exits 1 on its failed phases)
    1  → tool or database broken
    2  → a SILENT coverage hole, a `dream_runs` write declared failed, or an
         inconsistent/interrupted manifest. Never returned in fallback mode.
         This 2 is SHARED with argparse's usage error — a `SystemExit` that
         `main()`'s `except Exception` cannot intercept — and with `uv`'s and
         the interpreter's. `dream.sh` therefore believes it only if the
         machine line `COVERAGE …` was printed; it is printed for EVERY exit
         code (`test_exit_codes_follow_the_verdict`), this one included, which
         keeps the positive proof always available.

Coverage (ticket 0a9c067e). This module has long compared the observed against
the cartesian product `{enabled phase} × {pool project}` read from the systemd
drop-in, and it fired three nights running. The defect was not its absence but
its SIZE: `collector_dream.LOOP_PHASES` carries only `promote` and `reorg`, and
the four core phases have no key in `_KS_KEYS` — adding them there would be a
no-op. The night of 2026-08-16 therefore announced 20 missing phases when 60
were missing.

The expectation now comes from the MANIFEST the night writes itself, at the site
of every decision (`scripts/dream.sh`, `scripts.dream.run_manifest`). The
drop-in stays the FALLBACK path, explicitly labelled, with different field
names: 23 pairs expected from the drop-in and 62 pairs written the same night
are not comparable, and putting them side by side would reproduce the very
defect this ticket denounces.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from brain_v42.config import Settings
from brain_v42.db.tables import (
    adrs,
    decisions,
    dream_runs,
    indexed_plans,
    learnings,
    runbooks,
    snippets,
)
from brain_v42.dream_run_project_key import GLOBAL_PHASE_PROJECT_KEY
from brain_v42.metrics.collector_dream import (
    expected_dream_phase_pairs,
    expected_dream_phases,
)
from scripts.dream.run_manifest import (
    CoverageVerdict,
    Pair,
    RunManifest,
    classify_coverage,
    format_fallback_line,
    format_machine_line,
    format_silent_line,
    load_run_manifest,
    render_pairs,
)

MAX_REPORTED_FAILURES = 20
# The FETCH cap sits upstream of the grouping, so a per-project cap does not
# catch up with it: at `MAX_REPORTED_FAILURES + 1`, one noisy project late in
# the night would fill the 21 rows returned and the quiet projects would never
# reach the grouper. The order is `created_at DESC`, so the LAST projects served
# were the winners. 200 rows from a single day cost nothing.
MAX_FETCHED_FAILURES = 200
# §11 — `MAX_REPORTED_FAILURES = 20` was sized for the 9 phases of a
# single-project night. At ten projects the night counts 63, and a GLOBAL cap
# would let the first project consume all twenty rows: "N additional records
# omitted" would then hide WHOLE projects. The cap therefore becomes per project
# as soon as the rows carry a key.
MAX_REPORTED_FAILURES_PER_PROJECT = 8
FAILED_STATUSES = {"fail", "partial", "timeout"}
# Rendering of the sentinel for phases without a project. A leading `*` on a
# report line reads as a bullet or a wildcard, not as "the three global ones".
GLOBAL_GROUP_LABEL = "global"
UNLABELLED_GROUP_LABEL = "unlabelled"

# Four wordings, because four distinct FIRST MOVES. Conflating them sends the
# operator to the wrong place, which wears an alert out as surely as not
# emitting it.
MISSING_EXPECTED_MESSAGE = "expected enabled phase missing from dream_runs"
COVERAGE_SILENT_MESSAGE = "counted OK by dream.sh but wrote no dream_runs row"
COVERAGE_WRITEFAIL_MESSAGE = (
    "dream.sh reported the dream_runs write FAILED for this phase "
    "(see the WARN line in the dated log)"
)
COVERAGE_MISMATCH_MESSAGE = (
    "the night DECLARED this phase failed/timed out but its dream_runs row was "
    "written anyway — the row's status does not reflect the declaration, so a "
    "green coverage verdict here is a FALSE green (check the phase's marker)"
)
NO_ROW_AT_ALL_MESSAGE = (
    "no dream_runs row from any project phase — check the DB connection "
    "(DSN, credentials, schema) BEFORE opening any phase report; the global "
    "phases write in-process from dream.sh and can land alone"
)
INTERRUPTED_MESSAGE = (
    "the night never reached its closing block — this manifest is PARTIAL, "
    "so no green verdict is possible for it"
)
FALLBACK_WARNING = (
    "manifest absent — expectations derived from the drop-in, coverage limited to promote/reorg"
)
COVERAGE_HEADING = "### Couverture dream_runs"

PROVENANCE_HEADING = "### Provenance des transitions de fraîcheur"

#: The six tables tracked by the decay, the ones 043 gave a status clock and a
#: provenance. Enumerated, not discovered: a table gaining those columns without
#: being listed here would drop out of the count in SILENCE.
_FRESHNESS_TABLES = (decisions, learnings, snippets, runbooks, adrs, indexed_plans)

#: The sentence that goes with the number. 043's trigger documents its own blind
#: spot: two consecutive transitions from the SAME source are indistinguishable
#: from an unredeclared source, so the second falls back to NULL. Publishing the
#: count without this would make it read as a count of guilty writers.
PROVENANCE_CAVEAT = (
    "borne HAUTE, pas un compte : le trigger de la 043 remet la source à NULL "
    "quand elle n'est pas redéclarée, y compris pour deux transitions de même source"
)

#: The destination is EXACT — for a mute row, the current status really is that
#: of its last transition, a later transition having declared a source would take
#: it out of the count. But `fresh` is a SUPERSET of unarchival: `stale → fresh`
#: enters it too. The word "unarchival" would be stronger than the measurement,
#: and making it exact would need the PREVIOUS status, which nobody stores —
#: that is, a column, hence a migration.
PROVENANCE_DIRECTION_CAVEAT = (
    "un retour à `fresh` ne vient pas forcément d'une archive : `stale` → `fresh` "
    "y entre aussi, le statut précédent n'étant stocké nulle part"
)


#: The destination that tells a return to the corpus from a departure from it.
#: Step 0's counter NEVER read `freshness_status`: an archival and a return to
#: `fresh` produced the same line. Yet they are two opposite incidents — one
#: loses knowledge, the other resurrects some.
RETURNED_STATUS = "fresh"


@dataclass(frozen=True)
class ProvenanceCount:
    """Freshness transitions with no provenance, for one table.

    `to_fresh_*` is a SUBSET of `night`/`standing`, never a second count: the
    same rows, restricted to those whose destination is
    `fresh`.
    """

    table: str
    night: int
    standing: int
    #: Defaults to zero so that fixtures speaking only of the totals stay
    #: readable. The REAL values always come from the query.
    to_fresh_night: int = 0
    to_fresh_standing: int = 0


@dataclass(frozen=True)
class ProvenanceReport:
    """The count of mute transitions — observation ONLY, no verdict.

    Deliberately without `escalates`, unlike `CoverageReport`: this step makes
    things observable, it decides nothing. That is what allows shipping it
    BEFORE the fix it then has to measure.
    """

    run_date: dt.date
    counts: tuple[ProvenanceCount, ...]

    @property
    def night_total(self) -> int:
        return sum(count.night for count in self.counts)

    @property
    def standing_total(self) -> int:
        return sum(count.standing for count in self.counts)

    @property
    def to_fresh_night_total(self) -> int:
        return sum(count.to_fresh_night for count in self.counts)

    @property
    def to_fresh_standing_total(self) -> int:
        return sum(count.to_fresh_standing for count in self.counts)

    @property
    def block(self) -> list[str]:
        """Printed even at zero: "measured zero" and "not looked at" differ."""
        guilty = ", ".join(f"{count.table} {count.night}" for count in self.counts if count.night)
        detail = f" ({guilty})" if guilty else ""
        return [
            PROVENANCE_HEADING,
            f"transitions sans provenance — {self.run_date.isoformat()} : "
            f"{self.night_total}{detail} · cumul depuis la 043 : {self.standing_total}",
            f"  dont retours à `{RETURNED_STATUS}` : {self.to_fresh_night_total} "
            f"· cumul : {self.to_fresh_standing_total}",
            f"  {PROVENANCE_CAVEAT}",
            f"  {PROVENANCE_DIRECTION_CAVEAT}",
            "",
        ]

    @property
    def machine_line(self) -> str:
        return (
            f"dream_provenance run_date={self.run_date.isoformat()} "
            f"mute_night={self.night_total} mute_standing={self.standing_total} "
            f"mute_to_fresh_night={self.to_fresh_night_total} "
            f"mute_to_fresh_standing={self.to_fresh_standing_total}"
        )


async def fetch_mute_transitions(
    session: AsyncSession,
    run_date: dt.date,
) -> ProvenanceReport:
    """Count the freshness transitions whose provenance was erased.

    The signal is a CONJUNCTION: a transition HAPPENED
    (`freshness_status_updated_at IS NOT NULL`) and its provenance is absent
    (`freshness_source IS NULL`). The second half alone would count almost all
    of the corpus — the rows never through a transition since 043.

    Two windows, never conflated: the night, attributable to the run that just
    finished, and the running total, which states the backlog. One query, six tables.
    """
    selects = []
    for table in _FRESHNESS_TABLES:
        mute = sa.and_(
            table.c.freshness_status_updated_at.isnot(None),
            table.c.freshness_source.is_(None),
        )
        tonight = sa.cast(table.c.freshness_status_updated_at, sa.Date) == run_date
        # The DESTINATION, which step 0 did not query. Restricting the same
        # predicate rather than writing a second one: `to_fresh` must stay a
        # subset of `mute` by CONSTRUCTION, not by convention.
        returned = sa.and_(mute, table.c.freshness_status == RETURNED_STATUS)
        selects.append(
            sa.select(
                sa.literal(table.name).label("table_name"),
                sa.func.count().filter(mute, tonight).label("night"),
                sa.func.count().filter(mute).label("standing"),
                sa.func.count().filter(returned, tonight).label("to_fresh_night"),
                sa.func.count().filter(returned).label("to_fresh_standing"),
            ).select_from(table)
        )
    result = await session.execute(sa.union_all(*selects))
    return ProvenanceReport(
        run_date=run_date,
        counts=tuple(
            ProvenanceCount(
                table=str(row._mapping["table_name"]),
                night=int(row._mapping["night"]),
                standing=int(row._mapping["standing"]),
                to_fresh_night=int(row._mapping["to_fresh_night"]),
                to_fresh_standing=int(row._mapping["to_fresh_standing"]),
            )
            for row in result.all()
        ),
    )


def _detail_line(row: dict) -> str:
    phase = row.get("phase", "?")
    status = row.get("status", "?")
    err = row.get("error_message")
    err_line = err.splitlines()[0][:240] if err else "(no error_message captured)"
    return f"- {phase} [{status}]: {err_line}"


def _group_label(row: dict) -> str:
    project = row.get("project_key")
    if not project:
        # A row written before 042, or by an unmigrated writer. `NULL` means
        # "written before the column", not "unknown project to be fixed".
        return UNLABELLED_GROUP_LABEL
    if project == GLOBAL_PHASE_PROJECT_KEY:
        return GLOBAL_GROUP_LABEL
    return str(project)


def build_alert_insight(
    run_date: dt.date,
    failed: list[dict],
    *,
    total_failures: int | None = None,
) -> str:
    if not failed:
        raise ValueError("build_alert_insight requires a non-empty list of failed phases")

    total = len(failed) if total_failures is None else total_failures
    lines = [f"Dream run on {run_date.isoformat()} had {total} non-OK phase(s):", ""]

    if not any(row.get("project_key") for row in failed):
        # No row carries a project: a single-project night, or a corpus older
        # than 042. Rendered the HISTORICAL way, identically — grouping a list
        # whose every element would fall in the same bucket would make nothing
        # more readable and would change a format already being read.
        lines.extend(_detail_line(row) for row in failed[:MAX_REPORTED_FAILURES])
        omitted = total - min(len(failed), MAX_REPORTED_FAILURES)
        if omitted:
            record_label = "record" if omitted == 1 else "records"
            lines.append(f"{omitted} additional failure {record_label} omitted")
    else:
        grouped: dict[str, list[dict]] = {}
        for row in failed:
            grouped.setdefault(_group_label(row), []).append(row)

        omitted = total - len(failed)
        for label in sorted(grouped):
            rows = grouped[label]
            lines.append(f"{label}:")
            lines.extend(
                f"  {_detail_line(row)}" for row in rows[:MAX_REPORTED_FAILURES_PER_PROJECT]
            )
            hidden = len(rows) - MAX_REPORTED_FAILURES_PER_PROJECT
            if hidden > 0:
                record_label = "record" if hidden == 1 else "records"
                lines.append(f"  {hidden} additional failure {record_label} omitted")
                omitted += hidden
            lines.append("")
        if omitted:
            record_label = "record" if omitted == 1 else "records"
            lines.append(f"{omitted} additional failure {record_label} omitted in total")

    lines.extend(
        [
            "",
            "Inspect logs at logs/dream/" + run_date.isoformat() + ".log",
            "Auto-generated by scripts.dream.post_run_alert.",
        ]
    )
    return "\n".join(lines)


def include_missing_expected_phases(
    rows: list[dict],
    expected: set[str],
    persisted_failures: list[dict] | None = None,
    *,
    expected_pairs: set[tuple[str, str]] | None = None,
) -> list[dict]:
    """Return lexical synthetic partials followed by persisted non-OK rows.

    ``expected_pairs`` carries the cartesian product ``{phase} × {pool
    project}`` when the drop-in declares a pool. Comparing on phase names alone
    disarms itself across several projects: let one project skip `promote` and
    the others make it "observed". When the pairs are absent — no pool declared
    — the comparison by name stays in place, identically.
    """
    failed = (
        [row for row in rows if row.get("status") in FAILED_STATUSES]
        if persisted_failures is None
        else persisted_failures
    )

    if expected_pairs:
        observed_pairs = {(str(row["phase"]), str(row.get("project_key") or "")) for row in rows}
        missing = [
            {
                "phase": phase,
                "project_key": project,
                "status": "partial",
                "error_message": MISSING_EXPECTED_MESSAGE,
            }
            for phase, project in sorted(expected_pairs - observed_pairs)
        ]
        return missing + failed

    observed = {str(row["phase"]) for row in rows}
    missing = [
        {
            "phase": phase,
            "status": "partial",
            "error_message": MISSING_EXPECTED_MESSAGE,
        }
        for phase in sorted(expected - observed)
    ]
    return missing + failed


@dataclass(frozen=True)
class CoverageReport:
    """The coverage verdict, ready to print.

    `verdict` is `None` in fallback mode, and that is no oversight: without a
    manifest, `silent` is not COMPUTABLE. A half-filled object would invite
    reading a zero where there is no measurement.
    """

    block: tuple[str, ...]
    machine_line: str
    silent_line: str | None
    synthetic: tuple[dict, ...]
    escalates: bool
    verdict: CoverageVerdict | None


@dataclass(frozen=True)
class NightReport:
    """The operational report and its coverage verdict, side by side."""

    report: str | None
    coverage: CoverageReport


def missing_rows_from_verdict(verdict: CoverageVerdict) -> list[dict]:
    """Translate the three ABSENT classes that deserve a report line.

    `skipped` produces none: the night declared that nobody attempted a write,
    so there is nothing to investigate. That is exactly what makes six-phase
    coverage possible without manufacturing a false positive.
    """
    rows: list[dict] = []
    for pairs, message in (
        (verdict.silent, COVERAGE_SILENT_MESSAGE),
        (verdict.writefail, COVERAGE_WRITEFAIL_MESSAGE),
        (verdict.declared, MISSING_EXPECTED_MESSAGE),
    ):
        for phase, project in sorted(pairs):
            rows.append(
                {
                    "phase": phase,
                    "project_key": project,
                    "status": "partial",
                    "error_message": message,
                }
            )
    return rows


def _project_scoped(pairs: frozenset[Pair]) -> frozenset[Pair]:
    """The pairs of a REAL project — the global sentinel removed."""
    return frozenset(pair for pair in pairs if pair[1] != GLOBAL_PHASE_PROJECT_KEY)


def lost_the_whole_write_rail(verdict: CoverageVerdict) -> bool:
    """Is the first move "go and look at the connection" rather than a report?

    The criterion is NOT `written == 0`, and that is measured: the nights of
    2026-08-15 and 08-16, the very ones this message cites, each carry two rows
    in the database — `(extract, *)` and `(roadmap, *)`. Those two phases run
    IN PROCESS from dream.sh, with the repository's DSN; the other sixty go
    through the sub-agent rail. A zero-row guard therefore missed precisely its
    own reproduction case, and the operator received 61 lines sending them
    towards phase reports that did not exist.

    The partition is taken as is: no row from any PROJECT phase when the night
    expected some. The fallback on `written` covers the degenerate night that
    expected global phases only.
    """
    if not verdict.expected:
        return False
    if _project_scoped(verdict.expected):
        return not _project_scoped(verdict.written)
    return not verdict.written


def coverage_from_manifest(
    observed_pairs: set[Pair],
    manifest: RunManifest,
) -> CoverageReport:
    """The nominal path: the expectation is what the NIGHT declared."""
    verdict = classify_coverage(observed_pairs, manifest)
    block = [
        COVERAGE_HEADING,
        "",
        f"expected {len(verdict.expected)} · written {len(verdict.written)} "
        f"· skipped {len(verdict.skipped)} · declared {len(verdict.declared)} "
        f"· write-failed {len(verdict.writefail)} · silent {len(verdict.silent)} "
        f"· extra {len(verdict.extra)} · mismatch {len(verdict.mismatch)}",
    ]
    # The coverage block is printed EVERY night, green ones included, whereas
    # `report` is `None` as soon as no row has failed. A mismatch lodged in the
    # body of the report would therefore reach nobody on exactly the nights it
    # occurs — the ones that believe themselves green.
    if verdict.mismatch:
        block.append(f"{COVERAGE_MISMATCH_MESSAGE}: {render_pairs(verdict.mismatch)}")
    if lost_the_whole_write_rail(verdict):
        block.append(NO_ROW_AT_ALL_MESSAGE)
    if not verdict.complete:
        block.append(INTERRUPTED_MESSAGE)
    if not verdict.consistent:
        block.append(
            "manifest counters disagree — planned_phases="
            f"{manifest.meta.get('planned_phases', '-')} "
            f"total_phases={manifest.meta.get('total_phases', '-')} "
            f"reached={len(verdict.expected)}"
        )
    if manifest.warnings:
        block.append(f"{len(manifest.warnings)} malformed manifest line(s) ignored")
    block.append("")
    return CoverageReport(
        block=tuple(block),
        machine_line=format_machine_line(verdict),
        silent_line=format_silent_line(verdict),
        synthetic=tuple(missing_rows_from_verdict(verdict)),
        escalates=verdict.escalates,
        verdict=verdict,
    )


def coverage_fallback(*, expected: int, observed: int, missing: int) -> CoverageReport:
    """The fallback — today's path, spelled out in full.

    It NEVER returns 2. Without a manifest, an absent pair may as well be a hole
    as a phase the night never attempted, and reddening the unit on an
    undecidable is the surest way to make the alarm unreadable.
    """
    return CoverageReport(
        block=(COVERAGE_HEADING, "", FALLBACK_WARNING, ""),
        machine_line=format_fallback_line(expected=expected, observed=observed, missing=missing),
        silent_line=None,
        synthetic=(),
        escalates=False,
        verdict=None,
    )


def format_reconciliation_line(
    phases_ok: int,
    observed_rows: Iterable[Mapping[str, object]],
    *,
    skipped: int = 0,
) -> str:
    """The "N phases OK / M pairs written" gap nobody was reconciling.

    Ticket `b95c5742`: on 15-16/08, "61/63 phases OK" in the log, 2 rows in the
    database, 240 swallowed `InvalidPasswordError`. The INSERT stays best-effort
    (042 says why); this line makes the loss VISIBLE in the morning.

    `pairs_written` counts the (phase, project) pairs carrying AT LEAST
    one row whose status is not a pure failure: `done` of course, but
    `partial` too — a phase marked by the validator DID write, and
    counting it lost would start an INSERT hunt every time G4 does its
    job. A `fail`+`done` pair (a fallback) counts ONCE: dream.sh counts
    phases where dream_runs counts attempts. The SKIPPED phases —
    included in OK_TOTAL — are subtracted: they write no row and
    therefore lose none. A negative gap (recorded skips, replays)
    prints as it stands — a clamp would be a counter that
    lies.
    """
    pairs_written = {
        (str(row["phase"]), str(row.get("project_key") or ""))
        for row in observed_rows
        if str(row["status"]) not in ("fail", "timeout")
    }
    # SKIPPED phases are inside OK_TOTAL (= TOTAL_PHASES - FAIL_TOTAL) and
    # write no row: without subtracting them, the WARN would fire on almost
    # every healthy night — the exact wolf-cry this batch fixes (PR 47 review).
    gap = phases_ok - skipped - len(pairs_written)
    return (
        f"RECONCILIATION phases_ok={phases_ok} skipped={skipped} "
        f"pairs_written={len(pairs_written)} gap={gap}"
    )


async def fetch_failed_runs(
    session: AsyncSession,
    run_date: dt.date,
    *,
    manifest: RunManifest | None = None,
) -> tuple[list[dict], int, CoverageReport]:
    observed_statement = sa.select(
        dream_runs.c.phase,
        dream_runs.c.status,
        dream_runs.c.project_key,
    ).where(dream_runs.c.run_date == run_date)
    observed_result = await session.execute(observed_statement)
    observed_rows = [dict(row._mapping) for row in observed_result.all()]

    failures_filter = dream_runs.c.status.in_(FAILED_STATUSES)
    failures_statement = (
        sa.select(
            dream_runs.c.id,
            dream_runs.c.phase,
            dream_runs.c.status,
            dream_runs.c.project_key,
            dream_runs.c.error_message,
            dream_runs.c.created_at,
        )
        .where(dream_runs.c.run_date == run_date, failures_filter)
        .order_by(dream_runs.c.created_at.desc(), dream_runs.c.id.desc())
        .limit(MAX_FETCHED_FAILURES)
    )
    failures_result = await session.execute(failures_statement)
    persisted_failures = [dict(row._mapping) for row in failures_result.all()]
    failures_count_statement = (
        sa.select(sa.func.count())
        .select_from(dream_runs)
        .where(dream_runs.c.run_date == run_date, failures_filter)
    )
    failures_count_result = await session.execute(failures_count_statement)
    persisted_failure_count = int(failures_count_result.scalar_one())

    observed_pairs = {
        (str(row["phase"]), str(row.get("project_key") or "")) for row in observed_rows
    }

    if manifest is not None:
        coverage = coverage_from_manifest(observed_pairs, manifest)
        failed = [*coverage.synthetic, *persisted_failures]
        synthetic_count = len(coverage.synthetic)
    else:
        expected_pairs = expected_dream_phase_pairs()
        failed = include_missing_expected_phases(
            observed_rows,
            expected_dream_phases(),
            persisted_failures,
            expected_pairs=expected_pairs,
        )
        synthetic_count = len(failed) - len(persisted_failures)
        coverage = coverage_fallback(
            expected=len(expected_pairs),
            observed=len(observed_pairs),
            missing=synthetic_count,
        )

    return failed, synthetic_count + persisted_failure_count, coverage


async def review_night(
    session: AsyncSession,
    run_date: dt.date,
    *,
    manifest: RunManifest | None = None,
) -> NightReport:
    """Read the night ONCE and return its report AND its coverage verdict.

    Read-only, as always: three bounded `SELECT`s, no `commit`.
    """
    failed, total_failures, coverage = await fetch_failed_runs(session, run_date, manifest=manifest)
    report = (
        build_alert_insight(run_date, failed, total_failures=total_failures) if failed else None
    )
    return NightReport(report=report, coverage=coverage)


async def write_alert_if_failed(
    session: AsyncSession,
    run_date: dt.date,
    *,
    manifest: RunManifest | None = None,
) -> str | None:
    """Compatibility: the report ALONE, without its coverage verdict."""
    return (await review_night(session, run_date, manifest=manifest)).report


def render_stdout(
    report: str | None,
    run_date: dt.date,
    coverage: CoverageReport,
    provenance: ProvenanceReport | None = None,
) -> str:
    """The coverage block under the first line, the machine line LAST.

    Always, green nights included: that is the very point of the ticket. The two
    numbers nobody was reconciling end up adjacent in journald, under the
    "N/M phases OK" summary dream.sh has just printed.
    """
    body = report.splitlines() if report else [f"no failures for {run_date.isoformat()}"]
    provenance_block = provenance.block if provenance is not None else []
    lines = [body[0], "", *coverage.block, *provenance_block, *body[1:]]
    if coverage.silent_line:
        lines.append(coverage.silent_line)
    # `COVERAGE …` stays the LAST line of stdout: that is ticket `0a9c067e`'s
    # contract, pinned by `test_exit_codes_follow_the_verdict`. The provenance
    # line files itself just before, it does not take its place.
    if provenance is not None:
        lines.append(provenance.machine_line)
    lines.append(coverage.machine_line)
    return "\n".join(lines) + "\n"


async def review_and_render(
    session: AsyncSession,
    run_date: dt.date,
    *,
    manifest: RunManifest | None = None,
) -> tuple[str, bool]:
    """The LIVE path: the night, its provenance count, and the rendering.

    The provenance read lives HERE and not in `review_night`, for two reasons
    that hold together: `review_night` has a THREE-read contract pinned by its
    tests — one of which is called "never accesses learnings" — and step 0 must
    stay an added observation, incapable of breaking the alert path it
    accompanies.

    Returns the text AND the escalation verdict. The verdict comes from
    COVERAGE alone: the provenance count never escalates.
    """
    night = await review_night(session, run_date, manifest=manifest)
    provenance = await fetch_mute_transitions(session, run_date)
    rendered = render_stdout(night.report, run_date, night.coverage, provenance)
    return rendered, night.coverage.escalates


def default_manifest_path(run_date: dt.date) -> Path:
    """The night's manifest, derived from the repo — an honest replay, no flag."""
    repository_root = Path(__file__).resolve().parents[2]
    return repository_root / "logs" / "dream" / f"{run_date.isoformat()}_manifest.tsv"


async def _run(
    run_date: dt.date,
    manifest_path: Path | None = None,
    phases_ok: int | None = None,
    phases_skipped: int = 0,
) -> int:
    settings = Settings()
    engine = create_async_engine(settings.postgres_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    manifest = (
        load_run_manifest(manifest_path, run_date=run_date) if manifest_path is not None else None
    )
    try:
        async with factory() as session:
            if phases_ok is not None:
                observed = await session.execute(
                    sa.select(
                        dream_runs.c.phase,
                        dream_runs.c.status,
                        dream_runs.c.project_key,
                    ).where(dream_runs.c.run_date == run_date)
                )
                print(
                    format_reconciliation_line(
                        phases_ok,
                        [dict(row._mapping) for row in observed.all()],
                        skipped=phases_skipped,
                    )
                )
            rendered, escalates = await review_and_render(session, run_date, manifest=manifest)
            print(rendered, end="")
        return 2 if escalates else 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    # `--project-key` was removed. It had always been DECORATIVE: declared,
    # passed, received in the signature… and never read, `fetch_failed_runs` not
    # even receiving it and its three queries filtering on `run_date` alone. It
    # was becoming misleading too: with the pool rotating, the value passed by
    # dream.sh would have changed every night without anything reading it. The
    # project now lives in the BODY of the report, grouped, which 042 makes
    # possible.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Run date (YYYY-MM-DD)")
    parser.add_argument(
        "--manifest",
        default=None,
        help=(
            "Manifeste écrit par dream.sh. Défaut : logs/dream/<date>_manifest.tsv "
            "dans le dépôt, pour qu'un rejeu à la main soit honnête sans drapeau."
        ),
    )
    parser.add_argument(
        "--phases-ok",
        type=int,
        default=None,
        help=(
            "Compteur OK_TOTAL de dream.sh. Optionnel : un rejeu à la main n'a "
            "pas ce nombre, et la ligne RECONCILIATION ne s'imprime pas sans lui."
        ),
    )
    parser.add_argument(
        "--phases-skipped",
        type=int,
        default=0,
        help=(
            "Compteur de phases skippées de dream.sh — comprises dans OK_TOTAL, "
            "elles n'écrivent pas de ligne et sont soustraites du gap."
        ),
    )
    args = parser.parse_args(argv)
    run_date = dt.date.fromisoformat(args.date)
    manifest_path = Path(args.manifest) if args.manifest else default_manifest_path(run_date)
    try:
        return asyncio.run(
            _run(
                run_date,
                manifest_path,
                phases_ok=args.phases_ok,
                phases_skipped=args.phases_skipped,
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(f"post_run_alert failed: {exc!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
