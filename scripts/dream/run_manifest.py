"""One Dream night's manifest: what it DECLARES, re-read in the morning.

Ticket `0a9c067e`, reframed by its thread. The end-of-night comparator already
exists (`post_run_alert.include_missing_expected_phases`, called by `dream.sh`)
and it fired three nights running. It is UNDERSIZED, not absent:
`collector_dream.LOOP_PHASES` carries only `promote` and `reorg`, so the night
of 2026-08-16 announced 20 missing phases when 60 were missing.

Widening the expectation from the drop-in would not work — `_KS_KEYS` has no
key for `scan`, `clean`, `connect` and `synth`, so the filter excludes them
whatever goes into `LOOP_PHASES`. And widening it "naively" would manufacture
false positives: a phase skipped by the preflight or by a killswitch writes no
row and owes none.

Hence this transport: `dream.sh` writes a four-column TSV, AT THE SITE OF EVERY
DECISION, and this module reads it back. Writing at the site rather than at the
end of the night is no detail: a night killed by `TimeoutStartSec`, an OOM or an
unguarded `set -e` would never reach a final flush, and the morning replay would
fall back on the drop-in's expectation — that is, on the hole this module closes.

The format, four TAB-separated fields, trailing columns allowed to be empty:

    meta        run_date        2026-08-18
    meta        planned_phases  63
    expected    scan            red
    skipped     sweep           *               killswitch
    skipped     promote         red-lab         empty-pool-unrecorded
    failed      connect         brain-v42
    timeout     clean           red
    meta        finished        2026-08-18T07:09:32+02:00

FOUR classes of absence, not three. The fourth exists because
`scripts/dream.sh` pushes `SKIPPED_PHASES+=("$PROJECT_KEY/promote")` OUTSIDE the
`if (( record_rc == 0 ))`: "skipped" and "its row is written" are two
INDEPENDENT facts. Subtracting every skip would turn green a path where a
`dream_runs` row is genuinely lost — measured in production, 1 to 6
`empty candidate pool` rows per night from 2026-08-08 to 08-13.

This module does NO database I/O and imports nothing from the `brain_v42`
package — least of all `canonicalize_project_key`, which rejects the global
phases' `*` sentinel and would raise on all three of them every night.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

Pair = tuple[str, str]
"""A ``(phase, project_key)`` pair — the same order as `expected_pairs`."""

# The ONLY two reasons that promise no `dream_runs` row is owed. A CLOSED
# table, and fail-closed: an unknown reason is never subtracted, so an eighth
# skip site added tomorrow with fresh vocabulary makes the detector noisy, never
# blind. That is the only acceptable direction of travel for a detector whose
# ticket says it shrank in silence.
NO_ROW_SKIP_REASONS = frozenset({"preflight", "killswitch"})

# A skip for which dream.sh SAID the row write failed (non-zero `record_rc`).
# `_promote_helpers.main` returns 1 on exception and 0 only after `commit()`, so
# that code is a reliable proxy for "the row was written".
WRITE_FAILED_SKIP_REASON = "empty-pool-unrecorded"

# A skip for which dream.sh said the write SUCCEEDED. The pair stays expected:
# if the row is missing anyway, that is the 2026-08-15 DSN class, not a skip.
WRITE_RECORDED_SKIP_REASON = "empty-pool-recorded"

_KINDS = frozenset({"meta", "expected", "skipped", "failed", "timeout"})

# Cap on enumerating the offending pairs. `error_message` is unbounded `text`,
# but an unreadable report goes unread — that is the original defect.
MAX_LISTED_PAIRS = 10


@dataclass(frozen=True)
class RunManifest:
    """What the night declared, exactly as it declared it."""

    expected: frozenset[Pair]
    skipped: Mapping[Pair, str]
    failed: frozenset[Pair]
    timed_out: frozenset[Pair]
    meta: Mapping[str, str]
    warnings: tuple[str, ...]

    @property
    def complete(self) -> bool:
        """Did the night reach its closing block?

        Its absence IS the interruption marker, and that is intended: it is the
        only non-incremental part of the manifest.
        """
        return "finished" in self.meta


@dataclass(frozen=True)
class CoverageVerdict:
    """The closed partition of the expectation, plus the two structure flags."""

    expected: frozenset[Pair]
    written: frozenset[Pair]
    skipped: frozenset[Pair]
    writefail: frozenset[Pair]
    declared: frozenset[Pair]
    silent: frozenset[Pair]
    extra: frozenset[Pair]
    mismatch: frozenset[Pair]
    """An OVERLAP with `written`, never a sixth class.

    The pairs that do have their `dream_runs` row AND that the night declared
    `failed`/`timeout`. `declared` looks at declarations on MISSING pairs only,
    so a pair that was written but declared failed fell into `written` and
    nowhere else: its declaration was thrown away in silence. Measured on the
    night of the 19th to 20th — reorg declared `failed`, its row left `done`
    because its marking had crashed, and the verdict announced full coverage
    while reading an input file that said the opposite.

    The closed five-class partition therefore stays the invariant, and
    `escalates` deliberately ignores this one: this signal REPORTS.
    """
    consistent: bool
    complete: bool
    planned: int | None

    @property
    def mode(self) -> str:
        return "manifest" if self.complete else "manifest-partial"

    @property
    def escalates(self) -> bool:
        """rc 2: a hole, a write declared failed, or a doubtful structure.

        The threshold is 0, but on `silent + writefail`, never on `silent`
        alone. The ticket body asked for a number to be settled ("a gap of 1 is
        normal"); with four classes the number disappears, because every
        legitimate absence is now DECLARED or already reported by dream.sh.
        """
        return bool(self.silent or self.writefail) or not self.consistent or not self.complete


def _warn(warnings: list[str], raw: str) -> None:
    warnings.append(f"malformed manifest line: {raw!r}")


def parse_run_manifest(text: str) -> RunManifest:
    """Read the TSV. Never raises: a doubtful manifest gets REPORTED."""
    expected: set[Pair] = set()
    skipped: dict[Pair, str] = {}
    failed: set[Pair] = set()
    timed_out: set[Pair] = set()
    meta: dict[str, str] = {}
    warnings: list[str] = []

    for raw in text.splitlines():
        if not raw.strip():
            continue
        fields = [field.strip() for field in raw.split("\t")]
        kind = fields[0]
        rest = fields[1:]
        if kind not in _KINDS:
            # FORWARD compatibility: a fresh `kind` written by a newer
            # dream.sh is ignored, not reported. A line that has not even a
            # second field, on the other hand, is broken.
            if len(rest) < 1:
                _warn(warnings, raw)
            continue

        if kind == "meta":
            if len(rest) < 2 or not rest[0]:
                _warn(warnings, raw)
                continue
            meta[rest[0]] = rest[1]
            continue

        if len(rest) < 2 or not rest[0] or not rest[1]:
            _warn(warnings, raw)
            continue
        pair: Pair = (rest[0], rest[1])
        if kind == "expected":
            expected.add(pair)
        elif kind == "skipped":
            skipped[pair] = rest[2] if len(rest) > 2 else ""
        elif kind == "failed":
            failed.add(pair)
        else:
            timed_out.add(pair)

    return RunManifest(
        expected=frozenset(expected),
        skipped=dict(skipped),
        failed=frozenset(failed),
        timed_out=frozenset(timed_out),
        meta=dict(meta),
        warnings=tuple(warnings),
    )


def load_run_manifest(path: Path, *, run_date: dt.date) -> RunManifest | None:
    """Return `None` — hence the FALLBACK — on the four honest exit doors.

    Absent, unreadable, without any `expected`, or dated from another night. An
    empty expectation would disarm everything in silence; a manifest from another
    night would make a replay dishonest. Explicit fallback, never mute assent.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    manifest = parse_run_manifest(text)
    if not manifest.expected:
        return None
    if manifest.meta.get("run_date") != run_date.isoformat():
        return None
    return manifest


def _counter(meta: Mapping[str, str], key: str) -> tuple[int | None, bool]:
    """Return (value, readable). An unreadable counter is a FACT, not a detail."""
    raw = meta.get(key)
    if raw is None:
        return None, True
    try:
        return int(raw), True
    except ValueError:
        return None, False


def classify_coverage(
    observed_pairs: Iterable[Pair],
    manifest: RunManifest,
    *,
    written_as_failure: Iterable[Pair] = (),
) -> CoverageVerdict:
    """Partition the declared expectation into five disjoint classes.

        written   = expected ∩ observed
        missing   = expected − observed
        skipped   = { p ∈ missing : raison(p) ∈ NO_ROW_SKIP_REASONS }
        writefail = { p ∈ missing : raison(p) = empty-pool-unrecorded }
        declared  = (missing − skipped − writefail) ∩ (failed ∪ timeout)
        silent    = missing − skipped − writefail − declared

    The five sum to `expected` by CONSTRUCTION: `written` is an intersection
    and the other four partition `missing`. A pair both skipped and observed
    falls into `written` and nothing else — the double counting disappears with
    no arbitration.
    """
    expected = manifest.expected
    observed = frozenset(observed_pairs)

    written = expected & observed
    missing = expected - observed
    skipped = frozenset(
        pair for pair in missing if manifest.skipped.get(pair, "") in NO_ROW_SKIP_REASONS
    )
    writefail = frozenset(
        pair for pair in missing if manifest.skipped.get(pair, "") == WRITE_FAILED_SKIP_REASON
    )
    unexplained = missing - skipped - writefail
    declared = unexplained & (manifest.failed | manifest.timed_out)
    silent = unexplained - declared
    extra = observed - expected
    # An overlap, not a partition: a pair that was WRITTEN yet declared failed
    # by the night. Without this line, its declaration is dropped in silence.
    #
    # `written_as_failure` is what the ROW says, and it is the half this
    # comparison lacked until 2026-09-03. The message promises "the row's status
    # does not reflect the declaration"; the test was only "a row exists", so a
    # phase that failed, declared it AND wrote `status=fail` — the machinery
    # working — was reported as a false green. Measured that morning on
    # `*/extract`. A pair whose row already records the failure is therefore
    # subtracted: what remains is the shape the message names, and only it.
    mismatch = (written & (manifest.failed | manifest.timed_out)) - frozenset(written_as_failure)

    assert len(written) + len(skipped) + len(writefail) + len(declared) + len(silent) == len(
        expected
    ), "les cinq classes doivent partitionner l'attendu"

    planned, planned_readable = _counter(manifest.meta, "planned_phases")
    total, total_readable = _counter(manifest.meta, "total_phases")
    complete = manifest.complete

    # Three numbers, three instants, three code paths. `planned_phases` is
    # computed AT THE HEAD of the night, `len(expected)` is what it actually
    # reached, `total_phases` is its own end-of-night counter. Comparing
    # `expected` against an `expected` recomputed from the same arrays would
    # measure nothing: `TOTAL_PHASES = |PHASES| × |POOL| + 3` by construction.
    consistent = planned_readable and total_readable
    if total is not None and total != len(expected):
        consistent = False
    if complete and planned is not None and planned != len(expected):
        consistent = False

    return CoverageVerdict(
        expected=expected,
        written=written,
        skipped=skipped,
        writefail=writefail,
        declared=declared,
        silent=silent,
        extra=extra,
        mismatch=mismatch,
        consistent=consistent,
        complete=complete,
        planned=planned,
    )


def format_machine_line(verdict: CoverageVerdict) -> str:
    """The line `dream.sh` re-emits through `log()`, hence into journald.

    The first field is ALWAYS `mode=`: it is what forbids reading one shape for
    the other. The `manifest` mode sums exactly to `expected`.
    """
    tail = (
        f"written={len(verdict.written)} skipped={len(verdict.skipped)} "
        f"declared={len(verdict.declared)} writefail={len(verdict.writefail)} "
        f"silent={len(verdict.silent)} extra={len(verdict.extra)} "
        f"mismatch={len(verdict.mismatch)}"
    )
    if verdict.complete:
        return f"COVERAGE mode=manifest expected={len(verdict.expected)} {tail}"
    planned = "unknown" if verdict.planned is None else str(verdict.planned)
    return (
        f"COVERAGE mode=manifest-partial planned={planned} reached={len(verdict.expected)} {tail}"
    )


def format_fallback_line(*, expected: int, observed: int, missing: int) -> str:
    """The fallback, with DIFFERENT FIELD NAMES — because the sets differ.

    `observed` is not contained in `expected` (23 pairs expected from the
    drop-in against 62 written on 2026-08-18) and `silent` is not computable.
    Writing `expected=23 written=62` would reproduce the very defect this ticket
    denounces: two numbers side by side that nothing reconciles.
    """
    return (
        f"COVERAGE mode=fallback expected={expected} observed={observed} "
        f"missing={missing} silent=unknown"
    )


def render_pairs(pairs: Iterable[Pair]) -> str:
    ordered = sorted(pairs, key=lambda pair: (pair[1], pair[0]))
    shown = ordered[:MAX_LISTED_PAIRS]
    rendered = ", ".join(f"{project}/{phase}" for phase, project in shown)
    hidden = len(ordered) - len(shown)
    return f"{rendered} and {hidden} more" if hidden else rendered


def format_silent_line(verdict: CoverageVerdict) -> str | None:
    """Name the offending pairs — the two escalating classes, kept distinct.

    "dream.sh said the write failed" does not call for the same first move as
    "nobody knows why the row is missing".
    """
    parts = []
    if verdict.silent:
        parts.append(f"silent={render_pairs(verdict.silent)}")
    if verdict.writefail:
        parts.append(f"writefail={render_pairs(verdict.writefail)}")
    if not parts:
        return None
    return "COVERAGE_SILENT " + " | ".join(parts)
