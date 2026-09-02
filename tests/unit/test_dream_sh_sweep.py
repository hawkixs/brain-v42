"""Pins the SWEEP phase wiring in dream.sh (grep, no execution)."""

import re
from pathlib import Path

_DREAM_SH = Path(__file__).parent.parent.parent / "scripts" / "dream.sh"


def _content() -> str:
    return _DREAM_SH.read_text(encoding="utf-8")


def test_sweep_killswitch_defaults_closed_and_dry():
    content = _content()
    assert 'BRAIN_DREAM_SWEEP_ENABLED="${BRAIN_DREAM_SWEEP_ENABLED:-false}"' in content
    assert 'BRAIN_DREAM_SWEEP_DRY_RUN="${BRAIN_DREAM_SWEEP_DRY_RUN:-true}"' in content


def test_sweep_step_invokes_the_cli_module():
    content = _content()
    assert "brain_v42.maintenance.session_sweep" in content
    assert "SKIP sweep (killswitch" in content


def test_sweep_wet_flag_only_when_dry_run_false():
    content = _content()
    assert 'if [[ "$BRAIN_DREAM_SWEEP_DRY_RUN" != "true" ]]' in content


def test_sweep_step_has_timeout_and_own_log():
    content = _content()
    assert "timeout 5m uv run python -m brain_v42.maintenance.session_sweep" in content
    assert "_sweep.log" in content


def test_sweep_step_does_not_duplicate_the_threshold():
    """The threshold lives in brain_v42.models.brain_session.AUTO_STALE_AFTER, not
    in dream.sh. A second copy here would be learning 8dc7e042's time bomb: two
    constants silently contradicting each other the day one of them moves.

    The block is bounded on both sides (the `--- SWEEP` marker through to the
    following `FAIL_TOTAL=`) so that a phase added later after SWEEP can neither
    break nor weaken this test by sliding out of the scanned range.
    """
    content = _content()
    sweep_block = content.split("--- SWEEP", maxsplit=1)[1]
    sweep_block = sweep_block.split("FAIL_TOTAL=", maxsplit=1)[0]

    assert "--older-than-days" not in sweep_block

    # sweep_args must never receive anything but --wet: a threshold flag under
    # any other spelling (or any other argument) must fail this test.
    appended = re.findall(r"sweep_args\+=\(([^)]*)\)", sweep_block)
    assert appended == ["--wet"]
