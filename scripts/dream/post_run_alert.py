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
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
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
from brain_v42.documentation.claude_md_claims import CLAUDE_MD
from brain_v42.documentation.claude_md_claims import failing as claude_md_failing
from brain_v42.dream_degradation import DEGRADED_PREFIX
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

# ── DEGRADED: a phase that succeeded on its STANDBY model ────────────────────
# Measured over 2026-08-27 → 09-02: roadmap ran clean once in seven nights,
# three nights were served 10/10 batches by the standby, and all three printed
# `no failures`. `status` reads `'done'` on purpose — a successful fallback is
# not a failure — so the only way to see them is to read the mark, which lives
# in `error_message` and NOWHERE else.
#
# Rendered in French, like the two `###` blocks already under it: this file's
# blocks are French and its headline English, and injecting an English heading
# between them would be accretion, not consistency.
DEGRADED_HEADING = f"### {DEGRADED_PREFIX} (secours)"
# Same 240-char, first-line-only shape as `_detail_line`: a report is read in a
# terminal at 6 a.m., and a wrapped stack trace hides the line under it.
DEGRADED_CAUSE_CHARS = 240
# The producer is `roadmap_curate._degradation_notice`. This regex mirrors its
# sentence but is NOT the contract — the prefix is (see `dream_degradation`).
# A row whose sentence no longer matches is still listed, with its raw message:
# a reader that dropped it would turn a producer-side rewording into a blind
# morning, which is the defect being closed here.
# TWO shapes, because there are two producers and they refuse to share a wording.
# `roadmap` reports a RATIO of batches and falls back when its primary does not
# ANSWER — it may answer next time. `extract` reports a COUNT of tickets, with no
# denominator, and falls back because its primary was WITHDRAWN (404/410): it is
# gone and is not coming back. 89e37c8 states the refusal explicitly — "neither
# borrows the other's word to fit a shared regex" — so the reader learns both
# rather than flattening them into one. Order matters only for readability: the
# two cannot match the same sentence.
_DEGRADED_SHAPES = (
    re.compile(
        r"(?P<fallback>\d+)/(?P<scanned>\d+)\s+(?P<unit>batches)"
        r".*?le primaire\s+(?P<primary>\S+)\s+n'a pas répondu\s+—\s+(?P<cause>.+)",
        re.DOTALL,
    ),
    re.compile(
        r"(?P<scanned>\d+)\s+(?P<unit>tickets)\s+servis"
        r".*?le primaire\s+(?P<primary>\S+)\s+a été retiré\s+—\s+(?P<cause>.+)",
        re.DOTALL,
    ),
)

#: How the primary failed, per shape. Rendering "muet" for a WITHDRAWN model
#: would be the same defect as borrowing its unit: it would send the operator
#: looking for a slow model instead of a removed one.
_PRIMARY_STATE = {"batches": "muet", "tickets": "retiré"}


def _degraded_match(message: str) -> re.Match[str] | None:
    for shape in _DEGRADED_SHAPES:
        match = shape.search(message)
        if match is not None:
            return match
    return None


@dataclass(frozen=True)
class DegradedPhase:
    """One phase that reached `'done'` on its standby model."""

    phase: str
    project_key: str
    message: str
    served_model: str | None = None
    primary_model: str | None = None
    fallback_batches: int | None = None
    scanned: int | None = None
    cause: str | None = None
    #: The producer's OWN word for what it counted — `batches` or `tickets`.
    #: `None` means the sentence matched no known shape, and the rubric then
    #: prints it raw rather than guessing a unit.
    unit: str | None = None


def _is_degraded(row: Mapping[str, object]) -> bool:
    message = row.get("error_message")
    return isinstance(message, str) and message.startswith(DEGRADED_PREFIX)


def degraded_rows(rows: Iterable[Mapping[str, object]]) -> list[DegradedPhase]:
    """The night's degraded phases, read from rows ALREADY fetched.

    Keyed on the prefix, never on "there is a message": `promote` writes its
    empty-pool sentence and `extract` its deferral count on `'done'` rows too,
    and counting those as degradations would make the rubric fire every night —
    an alarm that fires every night stops being read.
    """
    degraded: list[DegradedPhase] = []
    for row in rows:
        if not _is_degraded(row):
            continue
        message = str(row["error_message"])
        served = row.get("model")
        match = _degraded_match(message)
        degraded.append(
            DegradedPhase(
                phase=str(row.get("phase", "?")),
                project_key=str(row.get("project_key") or ""),
                message=message,
                served_model=str(served) if served else None,
                primary_model=match.group("primary") if match else None,
                fallback_batches=(
                    int(match.group("fallback"))
                    if match and "fallback" in match.groupdict()
                    else None
                ),
                scanned=int(match.group("scanned")) if match else None,
                unit=match.group("unit") if match else None,
                cause=match.group("cause").strip() if match else None,
            )
        )
    return degraded


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
#: Ticket `e30a1cec`. The first wording said only what this path MEASURED —
#: "coverage limited to promote/reorg" — which reads at 7am as a clean night with
#: a smaller perimeter. It now says what it CANNOT say, and why. The
#: non-escalation stays exactly as it was, deliberate and pinned by a test: a lost
#: manifest is an observation failure, not a night failure. Making that choice
#: visible in the report is the whole change; whether the fallback should escalate
#: is an operator decision this line does not take.
FALLBACK_WARNING = (
    "manifest absent — expectations derived from the drop-in, coverage limited to "
    "promote/reorg. This verdict is NOT a completeness statement and cannot be read as "
    "one: without a manifest an absent pair is undecidable, so this path deliberately "
    "does not escalate. A green line here means UNMEASURED, not clean."
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


def _degraded_detail_line(degraded: DegradedPhase) -> str:
    project = degraded.project_key or GLOBAL_PHASE_PROJECT_KEY
    if degraded.unit is None:
        return f"- {degraded.phase} [{project}] : {degraded.message[:DEGRADED_CAUSE_CHARS]}"
    if degraded.fallback_batches is None:
        # A COUNT with no denominator. Printing `None/19` would invent a ratio
        # its producer deliberately does not report.
        served_only = degraded.served_model or "(modèle de secours non enregistré)"
        state_only = _PRIMARY_STATE.get(degraded.unit, "hors service")
        return (
            f"- {degraded.phase} [{project}] : {degraded.scanned} {degraded.unit} "
            f"au SECOURS {served_only} — primaire {degraded.primary_model} {state_only}"
        )
    served = degraded.served_model or "(modèle de secours non enregistré)"
    unit = degraded.unit or "unités"
    state = _PRIMARY_STATE.get(degraded.unit or "", "hors service")
    return (
        f"- {degraded.phase} [{project}] : {degraded.fallback_batches}/{degraded.scanned} "
        f"{unit} au SECOURS {served} — primaire {degraded.primary_model} {state}"
    )


def degraded_headline(run_date: dt.date, degraded: Sequence[DegradedPhase]) -> str:
    """The first line when nothing FAILED but something ran degraded.

    It must not contain "no failures": that string is what three nights of a
    dead primary hid behind. The count is the phases, not the batches.
    """
    ran = "1 phase ran" if len(degraded) == 1 else f"{len(degraded)} phases ran"
    return f"no failed phase for {run_date.isoformat()} — but {ran} DEGRADED (standby model)"


#: What REORG did, read from the JSON line it prints at the end of each project
#: report. There is no column: `dream_runs` carries no REORG counter, and adding
#: one would be a migration for a number the phase already writes down.
REORG_HEADING = "### REORG"

_REORG_REPORT = re.compile(r'\{"dry_run":\s*(?:true|false).*?\}')


@dataclass(frozen=True)
class ReorgTally:
    """One night's REORG work, summed over the pool."""

    projects: int = 0
    archived: int = 0
    updated: int = 0


def reorg_tally(run_date: dt.date, log_dir: Path) -> ReorgTally:
    """Sum the per-project REORG reports of one night.

    Reads the FILES, not the database, and that is the whole design: the counts
    exist only in the report each project prints (`{"dry_run":…,"updated":[…],
    "archived":[…]}`), and putting them in `dream_runs` would be a migration for
    a number already written down.

    What this deliberately does NOT count: candidates examined and candidates
    REFUSED. REORG states those in prose -- "Aucune entité archivée. Tous les
    titres correspondant à l'allowlist dépassent le seuil" -- and a regex over a
    model's free French would be a number nobody could trust. Making the phase
    print a machine-readable tally is the follow-up; inventing one here would be
    worse than the silence it replaces.

    A missing or unreadable file is skipped, not raised: this block observes the
    night, it must never be the reason the morning report fails.
    """
    projects = archived = updated = 0
    for path in sorted(log_dir.glob(f"{run_date.isoformat()}_*_reorg.log")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _REORG_REPORT.finditer(text):
            try:
                report = json.loads(match.group(0))
            except ValueError:
                continue
            projects += 1
            archived += len(report.get("archived") or [])
            updated += len(report.get("updated") or [])
    return ReorgTally(projects=projects, archived=archived, updated=updated)


def default_log_dir() -> Path:
    """`logs/dream/` of this repository, the directory `dream.sh` writes into."""
    return Path(__file__).resolve().parents[2] / "logs" / "dream"


def build_reorg_block(run_date: dt.date, tally: ReorgTally) -> list[str]:
    """The line, or nothing when the night has nothing to say.

    Mute when REORG did no work at all -- no archive and no tag update across the
    pool -- because a line repeated every night with two zeros stops being read
    (learning 4480d3df). It is NOT mute when the phase worked on tags and
    archived nothing: that is exactly the shape that went unnoticed for twelve
    nights, and it is the shape this block exists to surface.
    """
    if tally.archived == 0 and tally.updated == 0:
        return []
    lines = [
        f"{REORG_HEADING} — {run_date.isoformat()}",
        "",
        f"- {tally.archived} archivage(s), {tally.updated} tag(s) normalisé(s) "
        f"sur {tally.projects} projet(s)",
    ]
    if tally.archived == 0:
        lines.append(
            "- Aucun archivage : la phase a travaillé les tags sans retirer de "
            "pollution. Candidats et refus ne sont pas comptés ici — REORG les "
            "énonce en prose dans son rapport de projet."
        )
    return lines


def build_degraded_block(run_date: dt.date, degraded: Sequence[DegradedPhase]) -> list[str]:
    """The rubric, kept APART from the failures.

    Bounded by construction: its rows are a subset of the night's own phases,
    already read and already in memory.
    """
    if not degraded:
        return []
    lines = [f"{DEGRADED_HEADING} — {run_date.isoformat()}", ""]
    for phase in degraded:
        lines.append(_degraded_detail_line(phase))
        if phase.cause:
            lines.append(f"  cause : {phase.cause.splitlines()[0][:DEGRADED_CAUSE_CHARS]}")
    lines.append("")
    return lines


CLAUDE_MD_HEADING = "### CLAUDE.md (gitignoré)"


def build_claude_md_block(path: Path | None = None) -> list[str]:
    """The local assertions on `CLAUDE.md`, replayed and counted (ticket 87ac8b7a).

    `CLAUDE.md` is the document read first and confronted with the source least:
    the briefing DERIVES the schema revision but never opens the file, and the
    five assertions that do open it live behind a `pytest` nobody runs at the
    moment they are reading the document. Measured 2026-09-03: it said
    `migration 049` while production had been on `052` since 11:20.

    No new power — this script already runs each morning from the repository with
    filesystem access — and no prose parsing: the claims are IMPORTED from the
    module `test_documentation_contract` imports too, so the guard and the report
    can never hold two copies of one fragment.

    Returns `[]` in two cases, and the second is not a failure: a conforming
    document, and an ABSENT one. `CLAUDE.md` is gitignored since the open-source
    publication, so a clean checkout simply has no file — a block shouting about
    that absence would be noise on every machine that never had it.
    """
    document = path or CLAUDE_MD
    try:
        text = document.read_text(encoding="utf-8")
    except OSError:
        return []
    reds = claude_md_failing(text)
    if not reds:
        return []
    return [
        f"{CLAUDE_MD_HEADING} — {len(reds)} assertion(s) rouge(s)",
        "",
        "  " + ", ".join(claim.id for claim in reds),
        "  le document affirme un état que la source contredit ; "
        "rejouer `pytest tests/unit/test_documentation_contract.py` pour le détail",
        "",
    ]


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
    #: Phases that reached `'done'` on their standby model. Never folded into
    #: `report`: a degradation is not a failure, and the morning must be able to
    #: tell "the night worked" from "the night worked on its spare tyre".
    degraded: list[DegradedPhase] = field(default_factory=list)


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
    *,
    written_as_failure: Iterable[Pair] = (),
) -> CoverageReport:
    """The nominal path: the expectation is what the NIGHT declared.

    `written_as_failure` carries what the ROWS say, so that a phase which
    declared its failure AND wrote it is not accused of a false green.
    """
    verdict = classify_coverage(observed_pairs, manifest, written_as_failure=written_as_failure)
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
) -> tuple[list[dict], int, CoverageReport, list[DegradedPhase]]:
    # `model` and `error_message` ride ALONG: the degradation mark is read from
    # this very SELECT rather than from a fourth one. `review_night`'s
    # three-read contract is pinned by its tests, and a night is not worth one
    # more round trip to learn something already on the wire.
    observed_statement = sa.select(
        dream_runs.c.phase,
        dream_runs.c.status,
        dream_runs.c.project_key,
        dream_runs.c.model,
        dream_runs.c.error_message,
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
    # Read from the SAME rows, with the SAME vocabulary the failure list uses:
    # two definitions of "this row is a failure" would drift the day one of them
    # learns a status.
    written_as_failure = {
        (str(row["phase"]), str(row.get("project_key") or ""))
        for row in observed_rows
        if row.get("status") in FAILED_STATUSES
    }

    if manifest is not None:
        coverage = coverage_from_manifest(
            observed_pairs, manifest, written_as_failure=written_as_failure
        )
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

    return (
        failed,
        synthetic_count + persisted_failure_count,
        coverage,
        degraded_rows(observed_rows),
    )


async def review_night(
    session: AsyncSession,
    run_date: dt.date,
    *,
    manifest: RunManifest | None = None,
) -> NightReport:
    """Read the night ONCE and return its report AND its coverage verdict.

    Read-only, as always: three bounded `SELECT`s, no `commit`.
    """
    failed, total_failures, coverage, degraded = await fetch_failed_runs(
        session, run_date, manifest=manifest
    )
    report = (
        build_alert_insight(run_date, failed, total_failures=total_failures) if failed else None
    )
    return NightReport(report=report, coverage=coverage, degraded=degraded)


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
    *,
    degraded: Sequence[DegradedPhase] = (),
) -> str:
    """The coverage block under the first line, the machine line LAST.

    Always, green nights included: that is the very point of the ticket. The two
    numbers nobody was reconciling end up adjacent in journald, under the
    "N/M phases OK" summary dream.sh has just printed.
    """
    if report:
        body = report.splitlines()
    elif degraded:
        body = [degraded_headline(run_date, degraded)]
    else:
        body = [f"no failures for {run_date.isoformat()}"]
    provenance_block = provenance.block if provenance is not None else []
    # The rubric files itself AFTER the two existing blocks: the coverage block
    # sitting directly under the first line is a pinned contract
    # (`test_the_coverage_block_sits_under_the_first_line_of_the_report`).
    lines = [
        body[0],
        "",
        *coverage.block,
        *provenance_block,
        *build_degraded_block(run_date, degraded),
        # After the degradation rubric and before CLAUDE.md: REORG worked or it
        # did not, which is context for the failures above rather than a failure
        # itself. Reads the night's own reports from disk — `dream_runs` has no
        # REORG counter, and adding one would be a migration for a number the
        # phase already writes down.
        *build_reorg_block(run_date, reorg_tally(run_date, default_log_dir())),
        *build_claude_md_block(),
        *body[1:],
    ]
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
    rendered = render_stdout(
        night.report, run_date, night.coverage, provenance, degraded=night.degraded
    )
    # The verdict still comes from COVERAGE alone. A degraded night is LOUD, not
    # escalating: `dream.sh` turns `rc=2` into a `coverage` dream_runs row that
    # says expected rows are missing, and a degraded night has all of its rows.
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
