"""Public parsing vocabulary shared by the Dream reader and measured facts."""

from __future__ import annotations

from brain_v42.dream_killswitches import (
    KILLSWITCH_SHORT_NAMES,
    _iter_settings,
    iter_killswitch_settings,
)


def test_public_iterator_preserves_the_owned_environment_assignments() -> None:
    """A fact reader must see precisely the assignments the established parser sees."""
    content = (
        "[Service]\n"
        'Environment=BRAIN_DREAM_PROMOTE_ENABLED=true BRAIN_DREAM_REORG_DRY_RUN="false"\n'
        "Environment=UNRELATED_KEY=ignored\n"
    )

    assert list(iter_killswitch_settings(content)) == [
        ("BRAIN_DREAM_PROMOTE_ENABLED", "true"),
        ("BRAIN_DREAM_REORG_DRY_RUN", "false"),
    ]
    assert list(iter_killswitch_settings(content)) == list(_iter_settings(content))


def test_public_short_names_cover_every_killswitch_environment_key() -> None:
    """The probe cannot silently omit a phase when the shared key vocabulary changes."""
    assert KILLSWITCH_SHORT_NAMES == {
        "BRAIN_DREAM_PROMOTE_ENABLED": "promote",
        "BRAIN_DREAM_REORG_ENABLED": "reorg",
        "BRAIN_DREAM_REORG_DRY_RUN": "reorg_dry",
        "BRAIN_DREAM_EXTRACT_ENABLED": "extract",
        "BRAIN_DREAM_EXTRACT_DRY_RUN": "extract_dry",
        "BRAIN_DREAM_ROADMAP_ENABLED": "roadmap",
        "BRAIN_DREAM_ROADMAP_DRY_RUN": "roadmap_dry",
        "BRAIN_DREAM_SWEEP_ENABLED": "sweep",
        "BRAIN_DREAM_SWEEP_DRY_RUN": "sweep_dry",
    }
