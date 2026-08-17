"""La chaîne DRY de roadmap doit rester une CHAÎNE, et rester proposer-only.

Deux invariants que le choix d'un modèle peut casser sans bruit, et qu'aucun
test ne gardait quand le primaire DRY est mort en 410 le 2026-08-07.

1. PRIMAIRE ET SECOURS DISTINCTS. Poser le secours en primaire — la tentation
   évidente quand le primaire meurt, puisque le secours tourne déjà — supprime
   le second maillon. `_curate_managed_model_chain` bascule en plus sur le
   profil réduit (`FALLBACK_FEATURE_CAP`) dès que le modèle EST le secours :
   la nuit perdrait sa capacité de repli ET son profil complet, en silence.

2. L'IDENTITÉ DU MODÈLE EST LA BARRIÈRE D'AUTO-APPLICATION. `AUTO_APPLY_MODELS`
   et `PROPOSER_ONLY_MODELS` sont dérivés des mêmes constantes. Pointer le
   primaire DRY vers un modèle WET — également tentant, ils sont vivants et
   déjà validés — ferait gagner à une nuit DRY le droit d'appliquer.
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
    """L'intersection est vide, sinon un même nom porte deux droits opposés."""
    assert not (PROPOSER_ONLY_MODELS & AUTO_APPLY_MODELS)


def test_the_dry_primary_is_never_a_wet_model() -> None:
    """Redondant avec le test ci-dessus par construction — et gardé exprès.

    Les deux frozensets sont dérivés des constantes : si quelqu'un les
    redéfinit en littéraux, l'intersection pourrait rester vide alors que le
    primaire DRY vaut déjà un modèle WET. Cette assertion-ci lit les constantes
    et survivrait à cette réécriture.
    """
    assert DEFAULT_ROADMAP_MODEL not in {
        DEFAULT_WET_ROADMAP_MODEL,
        DEFAULT_WET_ROADMAP_FALLBACK_MODEL,
    }
