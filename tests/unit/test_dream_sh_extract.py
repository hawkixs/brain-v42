"""Pin the EXTRACT killswitch wiring in dream.sh (grep-style, no execution)."""

from pathlib import Path

_DREAM_SH = Path(__file__).parent.parent.parent / "scripts" / "dream.sh"


def _content() -> str:
    return _DREAM_SH.read_text(encoding="utf-8")


def test_extract_killswitch_defaults_closed_and_dry():
    content = _content()
    assert 'BRAIN_DREAM_EXTRACT_ENABLED="${BRAIN_DREAM_EXTRACT_ENABLED:-false}"' in content
    assert 'BRAIN_DREAM_EXTRACT_DRY_RUN="${BRAIN_DREAM_EXTRACT_DRY_RUN:-true}"' in content


def test_extract_step_invokes_cli_module():
    content = _content()
    assert "scripts.ticket_extract" in content
    assert "SKIP extract (killswitch" in content


def test_extract_wet_flag_only_when_dry_run_false():
    content = _content()
    # Le flag --wet doit être conditionné au sous-flag DRY_RUN, jamais inconditionnel.
    assert 'if [[ "$BRAIN_DREAM_EXTRACT_DRY_RUN" != "true" ]]' in content


def test_extract_uses_internal_budget_before_outer_timeout():
    content = _content()
    assert (
        "extract_args=(--limit 20 --run-budget-seconds 540 --ticket-budget-seconds 180)" in content
    )
    assert "elif (( extract_rc == 3 )); then" in content
    assert 'TIMED_OUT_PHASES+=("*/extract")' in content


def test_a_deferral_does_not_fail_the_unit():
    """rc=4 is "work owed", not "work broken" (ticket 572220e9).

    Measured on 2026-08-04: 15 deferred, 5 done, zero timeouts — and the unit
    went to `failed` anyway, as it had every night. Deferrals must be logged
    and must not enter FAILED_PHASES or TIMED_OUT_PHASES, both of which feed
    FAIL_TOTAL and force `exit 1`.
    """
    content = _content()

    assert "elif (( extract_rc == 4 )); then" in content
    assert "DEFERRED extract" in content

    branch = content.split("elif (( extract_rc == 4 )); then", maxsplit=1)[1]
    branch = branch.split("elif", maxsplit=1)[0]
    assert "TIMED_OUT_PHASES" not in branch
    assert "FAILED_PHASES" not in branch
