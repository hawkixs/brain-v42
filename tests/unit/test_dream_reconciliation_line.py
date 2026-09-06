"""RECONCILIATION: dream.sh's own OK count against `dream_runs`' written rows.

`format_reconciliation_line` compares `OK_TOTAL` (minus phases marked
SKIPPED, which write no row) against the `(phase, project)` pairs observed in
`dream_runs`, printing `RECONCILIATION phases_ok=N skipped=S pairs_written=M
gap=K`. A validator-invalidated phase (status `partial`) must count as
unwritten on both sides of that comparison, or the line reports a phantom gap
on every night that hits one.

The tests below also pin dream.sh's own wiring: the counters it passes on the
command line, the WARN it logs on a non-zero gap, and its in-band fallback
recording for a missing manifest.
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.dream.post_run_alert import format_reconciliation_line

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = (REPOSITORY_ROOT / "scripts" / "dream.sh").read_text(encoding="utf-8")


def _row(phase: str, status: str, project_key: str | None) -> dict[str, object]:
    return {"phase": phase, "status": status, "project_key": project_key}


def test_a_night_that_loses_an_insert_produces_a_nonzero_gap() -> None:
    """The 15-16/08 scenario: 61 phases OK, 2 rows — gap 59, readable."""
    rows = [_row("extract", "done", "*"), _row("roadmap", "done", "*")]

    line = format_reconciliation_line(61, rows)

    assert line == "RECONCILIATION phases_ok=61 skipped=0 pairs_written=2 gap=59"


def test_a_complete_night_reconciles_to_zero() -> None:
    rows = [
        _row("extract", "done", "*"),
        _row("clean", "done", "brain-v42"),
        _row("reorg", "done", "brain-v42"),
    ]

    line = format_reconciliation_line(3, rows)

    assert line.endswith("gap=0")


def test_a_fallback_retry_counts_its_pair_once() -> None:
    """dream_runs counts attempts, dream.sh counts phases: the dead codex attempt
    + the gemini catch-up make TWO rows, ONE pair — without which August's six
    fallback nights would all have a negative gap."""
    rows = [
        _row("clean", "fail", "brain-v42"),
        _row("clean", "done", "brain-v42"),
    ]

    line = format_reconciliation_line(1, rows)

    assert line.endswith("pairs_written=1 gap=0")


def test_a_night_with_one_partial_phase_reconciles_to_zero_and_causes_no_warn() -> None:
    """dream.sh's REORG/PROMOTE/CONNECT validators flip a row's status to
    `partial` on failure and force `phase_rc=1`, which dream.sh's own
    `case "$phase_rc" in … *) FAILED_PHASES+=(…)` files under `FAIL_TOTAL`, NOT
    `OK_TOTAL` (see `test_dream_sh_classifies_partial_as_a_failed_phase` below).
    A lone partial phase therefore makes `OK_TOTAL=0` for that night.
    `pairs_written` must exclude the same row for the same reason, or the two
    counters disagree about what "OK" means and produce a deterministic
    off-by-one gap on every partial night (measured 2026-09-06:
    `62 - 0 - 63 = -1`) — not a lost INSERT, just two vocabularies for one
    word. Superseded design (git history): counting `partial` as "written"
    reasoned "the row exists, so nothing was lost" — true of the INSERT, but
    irrelevant to a counter whose whole job is to match dream.sh's verdict."""
    rows = [_row("reorg", "partial", "brain-v42")]

    line = format_reconciliation_line(0, rows)

    assert line == "RECONCILIATION phases_ok=0 skipped=0 pairs_written=0 gap=0"
    # dream.sh's own trigger (test_dream_sh_logs_the_reconciliation_and_warns_on_gap):
    # the WARN fires unless the line ends in exactly " gap=0".
    assert line.endswith(" gap=0"), "a gap=0 line never trips dream.sh's WARN"


def test_dream_sh_classifies_partial_as_a_failed_phase() -> None:
    """Structural pin for the test above: the validators' `partial` write is
    followed, on every phase, by a `case "$phase_rc" in … *) FAILED_PHASES+=`
    that has no branch of its own for 1 — it falls into the same bucket as
    any other non-zero, non-timeout code. `phase_rc=1` is exactly what the
    REORG validator sets right before that `case` runs.

    The branch SET is asserted, not just the presence of three substrings:
    `assert "0) ;;" in case_block` alone stays green if a `1) ;;` branch is
    inserted ALONGSIDE it — the exact regression this test exists to catch,
    a validator-invalidated phase (`phase_rc=1`) reclassified as OK instead
    of falling into the `*)` catch-all that feeds `FAIL_TOTAL`."""
    case_block = DREAM_SH.split('case "$phase_rc" in', 1)[1].split("esac", 1)[0]
    branch_labels = re.findall(r"^\s*([^\s)]+)\)", case_block, re.M)
    assert branch_labels == ["0", "2", "*"], (
        "phase_rc=1 (partial) must have no branch of its own — it must fall "
        f"into the `*)` catch-all that feeds FAIL_TOTAL; got branches {branch_labels}"
    )
    assert "TIMED_OUT_PHASES+=" in case_block
    assert "FAILED_PHASES+=" in case_block


def test_reconciliation_docstring_names_no_ticket() -> None:
    """`b95c5742` is CLOSED (`tickets.status='closed'`, measured 2026-09-06),
    and the causes it named in dream.sh's WARN ("INSERT best-effort perdu ?
    rejeu ?") were themselves wrong for a partial night: the true cause was
    the `partial` miscount fixed above, neither a lost write nor a replay.
    Pinning this doc to an incident id lets the id go stale in silence;
    describing the measurement itself cannot.

    The ticket-id shape is `[0-9a-f]{8}` but a plain `\\b[0-9a-f]{8}\\b` scan
    is both too loose and too easy to fool: an 8-DIGIT date like `20260906`
    (all decimal, no letter) trips it on a perfectly legitimate "measured on"
    note, while deleting the whole docstring — saying nothing at all — passes
    it. A ticket id, unlike a date, always carries at least one `a`-`f`
    letter; requiring that plus a positive check that the doc still names
    what the line measures rules out both failure modes."""
    doc = format_reconciliation_line.__doc__ or ""
    assert doc, "the docstring must exist and describe the measurement"
    assert "pairs_written" in doc, (
        "the docstring must keep describing the measurement (pairs_written), "
        "not just avoid naming a ticket"
    )
    hex_tokens = re.findall(r"\b[0-9a-f]{8}\b", doc)
    ticket_shaped = [token for token in hex_tokens if re.search(r"[a-f]", token)]
    assert not ticket_shaped, (
        "the reconciliation line's own doc must describe what it measures, "
        f"not cite a ticket id that can (and did) close: {ticket_shaped}"
    )


def test_pure_failure_rows_do_not_count_as_written_success() -> None:
    """A pair that has ONLY failures does not explain an OK phase."""
    rows = [
        _row("clean", "fail", "brain-v42"),
        _row("connect", "timeout", "brain-v42"),
    ]

    line = format_reconciliation_line(2, rows)

    assert line.endswith("pairs_written=0 gap=2")


def test_a_negative_gap_is_printed_never_masked() -> None:
    """More pairs written than OK phases (recorded skips, replays): the gap is
    printed as is — clamping to zero would be a counter that lies."""
    rows = [
        _row("promote", "done", "red-lab"),
        _row("promote", "done", "brain-v42"),
    ]

    line = format_reconciliation_line(1, rows)

    assert line.endswith("gap=-1")


def test_skipped_phases_do_not_read_as_lost_inserts() -> None:
    """PR 47 review: OK_TOTAL = TOTAL_PHASES - FAIL_TOTAL includes the SKIPPED
    phases (unchanged corpus, killswitch), which write no row — without
    subtracting them, the gap≠0 WARN would fire on nearly every healthy night: the
    exact wolf-crying this batch fixes for REORG."""
    rows = [_row("extract", "done", "*"), _row("clean", "done", "brain-v42")]

    line = format_reconciliation_line(5, rows, skipped=3)

    assert line == "RECONCILIATION phases_ok=5 skipped=3 pairs_written=2 gap=0"


def test_a_lost_insert_still_shows_through_the_skips() -> None:
    """The inverse witness: with the skips subtracted, a real loss stays visible."""
    rows = [_row("extract", "done", "*")]

    line = format_reconciliation_line(5, rows, skipped=3)

    assert line.endswith("gap=1")


def test_a_recorded_empty_pool_skip_is_not_double_counted() -> None:
    """2nd PR 47 review fix: the "promote empty pool" skip WRITES a real row
    (record-empty-pool, status done) AND lives in SKIPPED_PHASES. Subtracting it
    on top of finding it in pairs_written gave gap=-1 → a WARN on a routine
    healthy night (PROMOTE wet in production). dream.sh therefore passes only the
    skips WITHOUT a row: here, skipped=0 and the written pair covers its phase —
    gap=0, silence."""
    rows = [
        _row("extract", "done", "*"),
        _row("clean", "done", "brain-v42"),
        _row("promote", "done", "brain-v42"),  # the record-empty-pool row
    ]

    line = format_reconciliation_line(3, rows, skipped=0)

    assert line.endswith("gap=0")


def test_dream_sh_does_not_count_the_recorded_empty_pool_as_unwritten() -> None:
    """The structural pin: the UNWRITTEN increment lives in the
    `empty-pool-unrecorded` branch (the write FAILED, no row owed) and NEVER in
    the `empty-pool-recorded` branch (the row exists)."""
    recorded = DREAM_SH.split("empty-pool-recorded", 1)[0].rsplit("if (( record_rc == 0 ))", 1)[1]
    assert "SKIPPED_UNWRITTEN" not in recorded
    unrecorded = DREAM_SH.split("empty-pool-unrecorded", 1)[1].split("fi\n", 1)[0]
    assert "SKIPPED_UNWRITTEN=$(( SKIPPED_UNWRITTEN + 1 ))" in unrecorded


# ---------------------------------------------------------------------------
# The engine wiring — dream.sh is shell, its contract is textual, as for the
# validators (test_dream_sh_reorg_validator.py).
# ---------------------------------------------------------------------------


def test_dream_sh_passes_its_own_ok_counter() -> None:
    assert '--phases-ok "$OK_TOTAL"' in DREAM_SH
    # The count passed is that of the skips WITHOUT a row: a skip that writes
    # (promote empty pool) is already in pairs_written — passing it as skipped too
    # would count it twice and give gap=-1 on a healthy night.
    assert '--phases-skipped "$SKIPPED_UNWRITTEN"' in DREAM_SH
    assert "SKIPPED_UNWRITTEN=0" in DREAM_SH


def test_dream_sh_logs_the_reconciliation_and_warns_on_gap() -> None:
    assert "grep -m1 '^RECONCILIATION '" in DREAM_SH
    assert 'log "=== dream_runs $reconciliation_line ==="' in DREAM_SH
    # The WARN only fires on a non-zero gap — a healthy night stays silent.
    assert '"$reconciliation_line" != *" gap=0"*' in DREAM_SH


def test_dream_sh_records_an_in_band_fallback_durably() -> None:
    """e30a1cec: the reporter keeps its "never 2"; it is dream.sh — the only one
    that KNOWS it wrote a manifest a few minutes earlier — that records the
    fallback (FAIL in the log + a `coverage` dream_runs row), without touching the
    night's exit code."""
    assert '"$coverage_line" == *"mode=fallback"*' in DREAM_SH
    fallback_block = DREAM_SH.split('*"mode=fallback"*', 1)[1].split("fi\n", 1)[0]
    assert "record_coverage_gap" in fallback_block
    assert "FAIL " in fallback_block
    assert "alert_rc" not in fallback_block, (
        "le repli in-band ne touche PAS au code de sortie — escalade de "
        "visibilité, jamais de rouge sur une indécidable"
    )
