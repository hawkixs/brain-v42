"""The roadmap DRY chain must stay a CHAIN, and stay proposer-only.

Two invariants a model choice can break silently, and that no test guarded when
the DRY primary died with a 410 on 2026-08-07.

1. PRIMARY AND FALLBACK DISTINCT. Making the fallback the primary — the obvious
   temptation when the primary dies, since the fallback already runs — removes
   the second link. `_curate_managed_model_chain` also switches to the reduced
   profile (`FALLBACK_FEATURE_CAP`) as soon as the model IS the fallback: the
   night would lose its fallback capacity AND its full profile, silently.

2. THE MODEL'S IDENTITY IS THE AUTO-APPLICATION BARRIER. `AUTO_APPLY_MODELS` and
   `PROPOSER_ONLY_MODELS` are derived from the same constants. Pointing the DRY
   primary at a WET model — equally tempting, they are alive and already
   validated — would earn a DRY night the right to apply.
"""

from __future__ import annotations

from scripts.roadmap_curate import (
    AUTO_APPLY_MODELS,
    DEFAULT_ROADMAP_FALLBACK_MODEL,
    DEFAULT_ROADMAP_MODEL,
    DEFAULT_WET_ROADMAP_FALLBACK_MODEL,
    DEFAULT_WET_ROADMAP_MODEL,
    PROPOSER_ONLY_MODELS,
)


def test_the_dry_chain_has_two_distinct_links() -> None:
    assert DEFAULT_ROADMAP_MODEL != DEFAULT_ROADMAP_FALLBACK_MODEL


def test_the_wet_chain_has_two_distinct_links() -> None:
    assert DEFAULT_WET_ROADMAP_MODEL != DEFAULT_WET_ROADMAP_FALLBACK_MODEL


def test_no_model_is_both_proposer_only_and_auto_apply() -> None:
    """The intersection is empty, otherwise one name carries two opposite rights."""
    assert not (PROPOSER_ONLY_MODELS & AUTO_APPLY_MODELS)


def test_the_dry_primary_is_never_a_wet_model() -> None:
    """Redundant with the test above by construction — and kept on purpose.

    Both frozensets are derived from the constants: if someone redefines them as
    literals, the intersection could stay empty while the DRY primary is already a
    WET model. This assertion reads the constants and would survive that rewrite.
    """
    assert DEFAULT_ROADMAP_MODEL not in {
        DEFAULT_WET_ROADMAP_MODEL,
        DEFAULT_WET_ROADMAP_FALLBACK_MODEL,
    }
