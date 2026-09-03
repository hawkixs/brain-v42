"""One parser for the REORG trailer, and a declared tally that is never truth.

Ticket 72dc9768. REORG prints a machine-readable trailer naming the entities it
MUTATED, and states in French prose everything it merely LOOKED AT. Measured on
`logs/dream/2026-08-22_red-shrik:agent_reorg.log`, the one night this month that
archived anything:

    "Trois learnings archivés après contrôle du contenu"
    "28 correspondances étaient déjà archivées."

Thirty-one candidates, three archives, twenty-eight refusals with a reason —
and the morning line could only say "3 archivage(s)". A night that examined 800
entities and matched none reads identically to a night that matched thirty-one
and refused them all.

WHY A SHARED PARSER, AND WHAT IT COSTS. Two parsers read this trailer today:
`reorg_validate.parse_report` and a second regex inside `post_run_alert`. They
had to agree and nothing made them. Measured before writing this module: the
alert's pattern is `\\{"dry_run":\\s*(?:true|false).*?\\}`, non-greedy, so a
trailer carrying a NESTED object stops at the first inner `}` and
`json.loads` raises — inside an `except ValueError: continue` that exists so the
morning report can never fail. The morning line would have gone silently to zero
on every project, and a phase that worked would have read as a phase that did
nothing. The cost of unifying is named: two independent readers were accidental
redundancy, and this file is now the only thing standing between a parser bug
and both consumers. The redundancy that matters is kept elsewhere — the
validator still confronts what the report DECLARES with what the event stream
OBSERVED, and those two sources stay independent.

WHAT IS TRUTH AND WHAT IS HEARSAY (learning c34fb865). `updated` and `archived`
are lists of ids the server can be asked about, so they are checkable. The
declared tally — candidates examined, refusals by reason, deferrals — is the
model talking about itself, and no call proves it. It is carried under a name
that says so, it is arithmetically cross-checked against itself, and it is never
summed into the derived counters.
"""

from __future__ import annotations

import pathlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from dream.reorg_report import (  # noqa: E402
    DeclaredTally,
    ReorgReport,
    iter_trailers,
    parse_trailer,
)

_FIXTURE = Path(__file__).resolve().parents[2] / "logs" / "dream"

_FLAT = (
    "=== REORG REPORT ===\n"
    '{"dry_run": false, "updated": ["11111111-1111-1111-1111-111111111111"], "archived": []}\n'
    "=== END ==="
)

_WITH_DECLARED = (
    "=== REORG REPORT ===\n"
    '{"dry_run": false, "updated": [], '
    '"archived": ["22222222-2222-2222-2222-222222222222"], '
    '"declared": {"candidates_examined": 31, "archived": 1, '
    '"refused": {"already_archived": 28, "dream_managed": 1, '
    '"access_above_threshold": 1, "content_not_trivial": 0}, "deferred": 0}}\n'
    "=== END ==="
)


# ── the regression that made this module necessary ───────────────────────────


def test_a_nested_trailer_survives_the_parser() -> None:
    """The measured trap: a non-greedy pattern truncates at the first inner brace.

    The previous alert-side regex matched up to the first `}`, which a nested
    `refused` object puts in the middle of the payload. The truncated text is
    invalid JSON, and the alert swallows `ValueError` by design so the morning
    report can never crash — so the loss was silent and total.
    """
    report = parse_trailer(_WITH_DECLARED)

    assert report.found_marker is True
    assert report.archived_ids == ["22222222-2222-2222-2222-222222222222"]
    assert report.declared is not None
    assert report.declared.candidates_examined == 31


# ── absent is not zero (learning 083d74e5) ───────────────────────────────────


def test_a_missing_trailer_is_not_an_empty_trailer() -> None:
    report = parse_trailer("the model wrote prose and forgot the block")

    assert report.found_marker is False
    assert report.declared is None


def test_a_trailer_without_the_declared_block_is_not_a_zero_tally() -> None:
    """A phase running the OLD prompt declares nothing. That is not "0 candidates".

    Conflating the two would let a prompt rollback read as a quiet night.
    """
    report = parse_trailer(_FLAT)

    assert report.found_marker is True
    assert report.declared is None


def test_an_explicit_zero_tally_is_a_tally() -> None:
    raw = (
        "=== REORG REPORT ===\n"
        '{"dry_run": false, "updated": [], "archived": [], '
        '"declared": {"candidates_examined": 0, "archived": 0, "refused": {}, "deferred": 0}}\n'
        "=== END ==="
    )

    report = parse_trailer(raw)

    assert report.declared == DeclaredTally(
        candidates_examined=0, archived=0, refused={}, deferred=0
    )


# ── the declared tally is cross-checked, never trusted ───────────────────────


def test_a_coherent_tally_reports_no_arithmetic_complaint() -> None:
    report = parse_trailer(_WITH_DECLARED)
    assert report.declared is not None

    assert report.declared.arithmetic_complaint() is None


def test_a_tally_that_does_not_add_up_is_named() -> None:
    """31 examined, 1 archived, 5 refused, 0 deferred — 25 entities unaccounted for.

    The model is the only source for these numbers, so the only available check
    is that they agree with each other. It is a weak check and it is stated as
    one: it catches a careless tally, never a consistent lie.
    """
    raw = (
        "=== REORG REPORT ===\n"
        '{"dry_run": false, "updated": [], "archived": [], '
        '"declared": {"candidates_examined": 31, "archived": 1, '
        '"refused": {"already_archived": 5}, "deferred": 0}}\n'
        "=== END ==="
    )

    report = parse_trailer(raw)
    assert report.declared is not None

    complaint = report.declared.arithmetic_complaint()
    assert complaint is not None
    assert "31" in complaint and "25" in complaint


def test_an_unknown_refusal_reason_is_kept_and_named() -> None:
    """The vocabulary is closed in the prompt, so an unknown key is a drift signal.

    It is preserved rather than dropped: discarding it would hide the drift, and
    the count still belongs in the arithmetic.
    """
    raw = (
        "=== REORG REPORT ===\n"
        '{"dry_run": false, "updated": [], "archived": [], '
        '"declared": {"candidates_examined": 2, "archived": 0, '
        '"refused": {"vibes": 2}, "deferred": 0}}\n'
        "=== END ==="
    )

    report = parse_trailer(raw)
    assert report.declared is not None

    assert report.declared.refused == {"vibes": 2}
    assert report.declared.unknown_reasons() == ("vibes",)
    assert report.declared.arithmetic_complaint() is None


def test_the_declared_archive_count_is_compared_to_the_declared_list() -> None:
    """Two statements by the same model about the same fact, one a list of ids.

    The list is checkable against PostgreSQL; the count is not. When they
    disagree, the count is the one with no evidence behind it.
    """
    raw = (
        "=== REORG REPORT ===\n"
        '{"dry_run": false, "updated": [], '
        '"archived": ["22222222-2222-2222-2222-222222222222"], '
        '"declared": {"candidates_examined": 4, "archived": 3, "refused": {}, "deferred": 1}}\n'
        "=== END ==="
    )

    report = parse_trailer(raw)

    complaint = report.declared_list_mismatch()
    assert complaint is not None
    assert "3" in complaint and "1" in complaint


def test_a_dry_run_may_count_archives_it_did_not_perform() -> None:
    """On a dry run the LIST is empty by construction, and that is not a mismatch.

    `phase_reorg.md` forbids the `brain_update` call in DRY_RUN, so a candidate
    that passed every guardrail produces no UUID — there was no call to return
    one. The declared count still has to record it, otherwise the arithmetic
    gap would equal the number of would-be archives and the "tally does not add
    up" warning would fire every single dry night. A guard that cries on the
    nominal path is a guard that gets muted.

    The DECLARED-versus-OBSERVED confrontation does not move: it compares ids to
    ids, and a dry run declares none.
    """
    raw = (
        "=== REORG REPORT ===\n"
        '{"dry_run": true, "updated": [], "archived": [], '
        '"declared": {"candidates_examined": 3, "archived": 3, "refused": {}, "deferred": 0}}\n'
        "=== END ==="
    )

    report = parse_trailer(raw)

    assert report.declared is not None
    assert report.declared.arithmetic_complaint() is None
    assert report.declared_list_mismatch() is None


def test_a_wet_run_is_still_held_to_its_list() -> None:
    """The relaxation is dry-run only; a wet night claiming phantom archives is caught."""
    raw = (
        "=== REORG REPORT ===\n"
        '{"dry_run": false, "updated": [], "archived": [], '
        '"declared": {"candidates_examined": 3, "archived": 3, "refused": {}, "deferred": 0}}\n'
        "=== END ==="
    )

    assert parse_trailer(raw).declared_list_mismatch() is not None


# ── shapes that must not raise ───────────────────────────────────────────────


def test_a_malformed_declared_block_does_not_lose_the_checkable_ids() -> None:
    """The ids are evidence; the tally is commentary. Commentary never costs evidence."""
    raw = (
        "=== REORG REPORT ===\n"
        '{"dry_run": false, "updated": ["11111111-1111-1111-1111-111111111111"], '
        '"archived": [], "declared": "not an object"}\n'
        "=== END ==="
    )

    report = parse_trailer(raw)

    assert report.updated_ids == ["11111111-1111-1111-1111-111111111111"]
    assert report.declared is None
    assert report.declared_malformed is True


def test_malformed_json_inside_the_markers_is_raised_not_swallowed() -> None:
    raw = "=== REORG REPORT ===\n{not json at all}\n=== END ==="

    with pytest.raises(ValueError):
        parse_trailer(raw)


def test_ids_are_deduplicated_in_order() -> None:
    raw = (
        "=== REORG REPORT ===\n"
        '{"dry_run": true, "updated": ["b", "a", "b"], "archived": []}\n'
        "=== END ==="
    )

    assert parse_trailer(raw).updated_ids == ["b", "a"]


# ── a retried phase writes two trailers into one log ─────────────────────────


_TWO_TRAILERS = (
    "=== REORG REPORT ===\n"
    '{"dry_run": false, "updated": [], "archived": []}\n'
    "=== END ===\n"
    "phase retried after a timeout\n"
    "=== REORG REPORT ===\n"
    '{"dry_run": false, "updated": ["33333333-3333-3333-3333-333333333333"], "archived": []}\n'
    "=== END ==="
)


def test_two_trailers_in_one_log_are_two_reports() -> None:
    """The retry budget is 2 per night, so a log CAN carry a second run.

    A greedy payload pattern spans from the first opening brace to the last
    closing one and yields a single unparseable blob; a plain lazy one stops at
    the first inner brace of a nested object. Only a lazy pattern ANCHORED on
    the END marker is right for both. No log in this repository carries two
    trailers today, which is precisely why this had to be pinned rather than
    observed.
    """
    reports = iter_trailers(_TWO_TRAILERS)

    assert len(reports) == 2
    assert reports[0].updated_ids == []
    assert reports[1].updated_ids == ["33333333-3333-3333-3333-333333333333"]


def test_the_first_trailer_is_the_one_a_single_read_returns() -> None:
    assert parse_trailer(_TWO_TRAILERS).updated_ids == []


def test_iter_trailers_reads_a_nested_block_in_each() -> None:
    raw = _WITH_DECLARED + "\nretry\n" + _WITH_DECLARED

    reports = iter_trailers(raw)

    assert len(reports) == 2
    assert all(r.declared is not None and r.declared.candidates_examined == 31 for r in reports)


# ── replay against a REAL night (learning 187f107c) ──────────────────────────


_REPLAY = pathlib.Path(__file__).parent / "data" / "2026-09-03_brain-v42_reorg.anonymised.log"


def test_the_parser_reads_a_real_reorg_trailer() -> None:
    """Runs EVERYWHERE, which is the correction.

    The sweep below reads `logs/dream/`, untracked, so it skipped in CI — the
    one place it had to run. This reads the committed anonymised copy of the
    2026-09-03 night instead, and a fixture invented from the parser would only
    prove the parser agrees with itself (learning 187f107c).
    """
    report = parse_trailer(_REPLAY.read_text(encoding="utf-8"))

    assert report.found_marker is True
    assert len(report.updated_ids) == 20
    assert report.archived_ids == []
    assert report.declared is None, (
        "the captured night predates the declared tally — that is what makes "
        "`legacy` a real state rather than a hypothetical one"
    )


def test_the_parser_reads_every_real_reorg_trailer_of_this_repository() -> None:
    """The full sweep, when the untracked logs are there.

    Kept beside the committed replay rather than replaced by it: one fixture is
    a sample, 325 real reports are a population, and only the second can show a
    shape nobody anticipated.
    """
    logs = sorted(_FIXTURE.glob("*_reorg.log"))
    if not logs:
        pytest.skip("no REORG logs in this checkout")

    parsed = declared = 0
    for path in logs:
        report = parse_trailer(path.read_text(encoding="utf-8", errors="replace"))
        if not report.found_marker:
            continue
        parsed += 1
        assert isinstance(report, ReorgReport)
        if report.declared is not None:
            declared += 1

    assert parsed > 0, "no REORG trailer found in any real log — the marker changed"
