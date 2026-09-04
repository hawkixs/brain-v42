"""The validator carries the declared tally without letting it become evidence.

Ticket 72dc9768. `symmetry_warnings` confronts what the REORG report DECLARED
with what the event stream OBSERVED — ids against ids, both checkable. The
declared tally is a third thing: numbers about entities the phase looked at and
did NOT touch, for which no call exists and none ever will.

It rides along because the morning line needs it, and it is checked the only way
an unverifiable number can be — against itself, and against the one statement
the same report makes that IS checkable, the list of archived ids.

WHAT MUST NOT HAPPEN (learning c34fb865). No declared number may weaken, replace
or satisfy an id-level check. The existing warnings are asserted here verbatim
alongside the new ones, so a future edit that trades one for the other reddens.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from dream.reorg_events import EventScan  # noqa: E402
from dream.reorg_report import REFUSAL_REASONS  # noqa: E402
from dream.reorg_validate import (  # noqa: E402
    apply_dry_run_override,
    parse_report,
    symmetry_warnings,
)

_PROMPT = Path(__file__).resolve().parents[2] / "scripts" / "dream" / "phase_reorg.md"

_ID_A = "11111111-1111-1111-1111-111111111111"
_ID_B = "22222222-2222-2222-2222-222222222222"


def _trailer(body: str) -> str:
    return f"=== REORG REPORT ===\n{body}\n=== END ==="


# ── prompt and reader share one closed vocabulary ────────────────────────────


def test_every_refusal_reason_the_reader_knows_is_defined_in_the_prompt() -> None:
    """A reason the reader accepts but the prompt never teaches is dead code.

    The agent can only emit what the prompt describes, so an unused key would
    quietly become a name nobody writes and nobody notices is missing.
    """
    prompt = _PROMPT.read_text(encoding="utf-8")

    missing = [reason for reason in REFUSAL_REASONS if f"`{reason}`" not in prompt]

    assert missing == [], f"reader knows {missing}, prompt never names them"


def test_every_refusal_key_the_prompt_shows_is_known_to_the_reader() -> None:
    """The reverse drift: a prompt teaching a key the reader treats as unknown.

    Read from the trailer EXAMPLE, which is the line the agent copies, not from
    the prose around it.
    """
    prompt = _PROMPT.read_text(encoding="utf-8")
    example = next(
        line for line in prompt.splitlines() if '"declared"' in line and '"refused"' in line
    )
    refused_block = example.split('"refused":', 1)[1].split("}", 1)[0]
    keys = set(re.findall(r'"([a-z_]+)":', refused_block))

    assert keys, "the trailer example carries no refusal key — the contract moved"
    assert keys <= set(REFUSAL_REASONS), f"prompt teaches {sorted(keys - set(REFUSAL_REASONS))}"


# ── the validator's parser carries the tally ─────────────────────────────────


def test_parse_report_exposes_the_declared_tally() -> None:
    report = parse_report(
        _trailer(
            '{"dry_run": false, "updated": [], "archived": ["' + _ID_B + '"], '
            '"declared": {"candidates_examined": 31, "archived": 1, '
            '"refused": {"already_archived": 28, "dream_managed": 1, '
            '"access_above_threshold": 1}, "deferred": 0}}'
        )
    )

    assert report["archived_ids"] == [_ID_B]
    assert report["declared"] is not None
    assert report["declared"].candidates_examined == 31
    assert report["declared"].refused_total == 30


def test_parse_report_keeps_the_old_shape_for_a_trailer_without_a_tally() -> None:
    """Every night before this change wrote this shape, and it must stay legal."""
    report = parse_report(
        _trailer('{"dry_run": true, "updated": ["' + _ID_A + '"], "archived": []}')
    )

    assert report["updated_ids"] == [_ID_A]
    assert report["dry_run"] is True
    assert report["found_marker"] is True
    assert report["declared"] is None


# ── the id-level checks are untouched ────────────────────────────────────────


def test_a_declared_ghost_is_still_denounced_when_a_tally_is_present() -> None:
    """The tally must not buy an id a pass."""
    report = parse_report(
        _trailer(
            '{"dry_run": false, "updated": ["' + _ID_A + '"], "archived": [], '
            '"declared": {"candidates_examined": 0, "archived": 0, "refused": {}, "deferred": 0}}'
        )
    )

    warnings = symmetry_warnings(report, EventScan(updated_ids=set(), codex_events=3))

    assert any("never passed to" in w and _ID_A in w for w in warnings)


def test_an_undeclared_mutation_is_still_denounced_when_a_tally_is_present() -> None:
    report = parse_report(
        _trailer(
            '{"dry_run": false, "updated": [], "archived": [], '
            '"declared": {"candidates_examined": 0, "archived": 0, "refused": {}, "deferred": 0}}'
        )
    )

    warnings = symmetry_warnings(report, EventScan(updated_ids={_ID_B}, codex_events=3))

    assert any("absent from the" in w and _ID_B in w for w in warnings)


def test_an_unreadable_stream_still_yields_exactly_one_warning() -> None:
    """One cause, one line — adding tally checks must not multiply that line."""
    report = parse_report(
        _trailer(
            '{"dry_run": false, "updated": ["' + _ID_A + '"], "archived": [], '
            '"declared": {"candidates_examined": 1, "archived": 0, '
            '"refused": {"already_archived": 1}, "deferred": 0}}'
        )
    )

    warnings = symmetry_warnings(report, EventScan())

    assert len(warnings) == 1
    assert "UNVERIFIED" in warnings[0]


# ── the new, tally-level warnings ────────────────────────────────────────────


def test_a_tally_that_does_not_add_up_is_warned_about() -> None:
    report = parse_report(
        _trailer(
            '{"dry_run": false, "updated": [], "archived": [], '
            '"declared": {"candidates_examined": 31, "archived": 0, '
            '"refused": {"already_archived": 5}, "deferred": 0}}'
        )
    )

    warnings = symmetry_warnings(report, EventScan(updated_ids=set(), codex_events=1))

    assert any("does not add up" in w for w in warnings)


def test_a_count_that_contradicts_the_archived_list_is_warned_about() -> None:
    report = parse_report(
        _trailer(
            '{"dry_run": false, "updated": [], "archived": ["' + _ID_B + '"], '
            '"declared": {"candidates_examined": 3, "archived": 3, "refused": {}, "deferred": 0}}'
        )
    )

    warnings = symmetry_warnings(report, EventScan(updated_ids={_ID_B}, codex_events=1))

    assert any("only the list is checkable" in w for w in warnings)


def test_an_unknown_refusal_reason_is_warned_about_as_drift() -> None:
    report = parse_report(
        _trailer(
            '{"dry_run": false, "updated": [], "archived": [], '
            '"declared": {"candidates_examined": 2, "archived": 0, '
            '"refused": {"vibes": 2}, "deferred": 0}}'
        )
    )

    warnings = symmetry_warnings(report, EventScan(updated_ids=set(), codex_events=1))

    assert any("vibes" in w for w in warnings)


def test_a_malformed_tally_is_warned_about_rather_than_read_as_zeros() -> None:
    report = parse_report(
        _trailer('{"dry_run": false, "updated": [], "archived": [], "declared": "oops"}')
    )

    warnings = symmetry_warnings(report, EventScan(updated_ids=set(), codex_events=1))

    assert any("unusable" in w for w in warnings)


def test_the_cli_dry_run_override_reaches_the_count_versus_list_check() -> None:
    """The belt+suspenders flag must actually reach the relaxation it enables.

    `_amain` applies the authoritative CLI `--dry-run` with a shallow dict copy
    (`{**report, "dry_run": True}`), while the relaxation added for dry runs
    gates on the frozen `ReorgReport` carried under `parsed` — whose `dry_run`
    comes from the JSON trailer, the very value the override exists to distrust.

    `dream.sh` passes `--dry-run` whenever `reorg_effective_dry_run` is true,
    regardless of what the trailer says. So a dry night whose trailer claims
    `dry_run: false` produced exactly the false alarm the relaxation removes,
    in exactly the case the override covers.
    """
    report = parse_report(
        _trailer(
            '{"dry_run": false, "updated": [], "archived": [], '
            '"declared": {"candidates_examined": 3, "archived": 3, "refused": {}, "deferred": 0}}'
        )
    )
    overridden = apply_dry_run_override(report)

    warnings = symmetry_warnings(overridden, EventScan(updated_ids=set(), codex_events=1))

    assert [w for w in warnings if "only the list is checkable" in w] == []


def test_the_override_leaves_a_wet_report_alone() -> None:
    """Guard on the guard: relaxing unconditionally would silence a real alarm."""
    report = parse_report(
        _trailer(
            '{"dry_run": false, "updated": [], "archived": [], '
            '"declared": {"candidates_examined": 3, "archived": 3, "refused": {}, "deferred": 0}}'
        )
    )

    warnings = symmetry_warnings(report, EventScan(updated_ids=set(), codex_events=1))

    assert any("only the list is checkable" in w for w in warnings)


def test_the_override_moves_both_the_dict_key_and_the_parsed_object() -> None:
    """One fact, two carriers — they must not be allowed to disagree."""
    report = parse_report(_trailer('{"dry_run": false, "updated": [], "archived": []}'))

    overridden = apply_dry_run_override(report)

    assert overridden["dry_run"] is True
    assert overridden["parsed"].dry_run is True
    assert report["parsed"].dry_run is False, "the original must not be mutated"


def test_the_call_site_applies_the_override_not_only_the_helper(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """The wiring, not the helper — the line the defect actually lived on.

    Reverting `_amain`'s call to `apply_dry_run_override` back to a shallow dict
    copy left the ENTIRE unit suite green: the helper was covered, its only
    caller was not. A fix whose call site nothing exercises is a fix one
    careless edit away from being undone in silence.

    Drives the real `_amain` with the CLI flag set, a trailer that CLAIMS to be
    wet, and a declared count that contradicts its own list — the shape that
    produced the false alarm. `validate` is stubbed because the DB is not the
    subject; stderr is read because that is where the alarm would appear.
    """
    import asyncio
    import types

    from dream import reorg_validate

    async def _no_validation(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(reorg_validate, "validate", _no_validation)

    # A RECOGNISABLE stream. An empty file makes `scan.recognised` false, and
    # `symmetry_warnings` then returns its single "UNVERIFIED" line before ever
    # reaching the tally checks — a test written that way passes whatever the
    # call site does, which is how the first draft of this test failed to bite.
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "mcp_tool_call",
                    "server": "brain-v42",
                    "tool": "brain_update",
                    "arguments": {"entity_type": "learning", "entity_id": _ID_A},
                    "status": "completed",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    args = types.SimpleNamespace(
        dry_run=True,
        events_jsonl=str(events),
        dream_run_id=None,
        project_key="brain-v42",
    )
    raw = _trailer(
        '{"dry_run": false, "updated": ["' + _ID_A + '"], "archived": [], '
        '"declared": {"candidates_examined": 3, "archived": 3, "refused": {}, "deferred": 0}}'
    )

    rc = asyncio.run(reorg_validate._amain(raw, {}, object(), args))

    printed = "".join(capsys.readouterr())
    assert rc == 0
    # The fixture verifies ITSELF before the negative assertion below. An empty
    # or unrecognisable stream makes `symmetry_warnings` return its single
    # UNVERIFIED line and never reach the tally checks — which is exactly how
    # the first draft of this test passed whatever the call site did.
    assert "UNVERIFIED" not in printed, (
        "the event stream was not recognised, so the check under test was never "
        "reached and the assertion below proves nothing"
    )
    assert "only the list is checkable" not in printed, (
        "the CLI --dry-run override did not reach declared_list_mismatch() — the "
        "call site lost what the helper provides"
    )


def test_a_coherent_tally_adds_no_warning_at_all() -> None:
    """The quiet case has to stay quiet, or the warnings stop being read."""
    report = parse_report(
        _trailer(
            '{"dry_run": false, "updated": [], "archived": ["' + _ID_B + '"], '
            '"declared": {"candidates_examined": 31, "archived": 1, '
            '"refused": {"already_archived": 28, "dream_managed": 1, '
            '"access_above_threshold": 1}, "deferred": 0}}'
        )
    )

    assert symmetry_warnings(report, EventScan(updated_ids={_ID_B}, codex_events=4)) == []


def test_a_trailer_without_a_tally_adds_no_tally_warning() -> None:
    """An old prompt is not a broken one — it must not generate noise every night."""
    report = parse_report(_trailer('{"dry_run": false, "updated": [], "archived": []}'))

    assert symmetry_warnings(report, EventScan(updated_ids=set(), codex_events=1)) == []
