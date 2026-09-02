"""The night reconciles its own OK phases with its written rows (b95c5742).

On 15-16/08, the loop signed off "61/63 phases OK" while `dream_runs` received
only 2 rows — 240 `InvalidPasswordError` swallowed best-effort. Since then, the
absence of a `done` row does not prove a phase failure: it may prove a lost
INSERT, and any reliability analysis carried out on the table alone is wrong in
the pessimistic direction.

The INSERT stays best-effort — that is 042's lesson, a `NOT NULL` there would
make a warning printed on all of them — but the gap becomes VISIBLE: dream.sh
passes its `OK_TOTAL` counter to `post_run_alert`, which prints a machine line
`RECONCILIATION phases_ok=N pairs_written=M gap=K`. A non-zero `gap` in the
morning is exactly the loss of 15-16/08, readable without cross-checking the log.

The manifest's in-band fallback (e30a1cec) is guarded in the same place: when the
COVERAGE line says `mode=fallback` while dream.sh has just written its manifest,
the engine SAYS so (FAIL) and records it (record_coverage_gap) — without touching
the exit code: the reporter keeps its "never 2" (undecidable pairs), it is the
ONLY caller that knows the manifest was supposed to exist that escalates, and it
escalates visibility only.
"""

from __future__ import annotations

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


def test_a_partial_row_is_a_written_row() -> None:
    """`partial` = the phase wrote THEN the validator invalidated it: the row
    exists, counting it as lost would trigger an INSERT hunt on every night where
    G4 does its job."""
    rows = [_row("reorg", "partial", "brain-v42")]

    line = format_reconciliation_line(1, rows)

    assert line.endswith("pairs_written=1 gap=0")


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
