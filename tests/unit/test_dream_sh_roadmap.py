"""Pin the ROADMAP killswitch wiring in dream.sh (grep-style, no execution)."""

from pathlib import Path

_DREAM_SH = Path(__file__).parent.parent.parent / "scripts" / "dream.sh"


def _content() -> str:
    return _DREAM_SH.read_text(encoding="utf-8")


def test_roadmap_killswitch_defaults_closed_and_dry():
    content = _content()
    assert 'BRAIN_DREAM_ROADMAP_ENABLED="${BRAIN_DREAM_ROADMAP_ENABLED:-false}"' in content
    assert 'BRAIN_DREAM_ROADMAP_DRY_RUN="${BRAIN_DREAM_ROADMAP_DRY_RUN:-true}"' in content


def test_roadmap_step_invokes_cli_module():
    content = _content()
    assert "scripts.roadmap_curate" in content
    assert "SKIP roadmap (killswitch" in content


def test_roadmap_wet_flag_only_when_dry_run_false():
    content = _content()
    # The comparison moved into the shared `dream_wants_wet` helper on
    # 2026-09-04: `!= "true"` armed the wet run on ANY value that was not
    # exactly `true`, empty strings and typos included. The intent of this
    # assertion is unchanged; only the spelling it pins moved.
    assert (
        'if dream_wants_wet BRAIN_DREAM_ROADMAP_DRY_RUN "$BRAIN_DREAM_ROADMAP_DRY_RUN"' in content
    )
    assert '"$BRAIN_DREAM_ROADMAP_DRY_RUN" != "true"' not in content


def test_roadmap_step_has_timeout_and_own_log():
    """20 m budget: first real run at 597 s/600 s (2026-07-04) — the same
    archetype as the synth timeouts (the 10→15 bump of 2026-05-03)."""
    content = _content()
    assert "timeout 20m uv run python -m scripts.roadmap_curate" in content
    assert "_roadmap.log" in content
