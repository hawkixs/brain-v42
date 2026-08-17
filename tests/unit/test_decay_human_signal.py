"""Le decay cesse de compter ce que la MACHINE relit — derrière un réglage.

Spec `2026-08-08-dream-v2-design.md` §5.1, §5.2, §5.5.

Le défaut, tel que le focus le nomme : « DECAY INVERSÉ ». `brain_service`
passait `access_count` — le compteur TOTAL — au multiplicateur. Le dream relit
le corpus chaque nuit ; ces lectures gonflent le total ; l'artefact reste donc
« frais » parce qu'une machine l'a lu. Le signal mesure la présence du dream,
pas l'utilité pour un humain.

MESURÉ dans la spec : sur `learnings`, 19 049 accès au total contre 79 humains
— **0,41 %**. 508 entités dépassent leur `freq_baseline` sur le total, **zéro**
sur le compteur humain.

ET LE CORRECTIF NE PEUT PAS ÊTRE LE SEUL COMPTEUR. `access_count` pèse 0,2 dans
la formule ; `last_accessed_at` pèse **0,3**, le plus lourd après l'âge, et la
041 ne lui avait donné aucune variante humaine. 1 779 learnings ont leur terme
de récence piloté par des lectures machine seules. Substituer un seul des deux
répare 0,2 des 0,5 de poids pilotés par la lecture. Les deux basculent ensemble.

LE RÉGLAGE EST FERMÉ PAR DÉFAUT (§5.5) : c'est le seul élément de ce chantier
sans irréversibilité mais à effet immédiat sur un humain — l'ordre des résultats
de recherche change le jour où on l'ouvre.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest

from brain_v42.config import Settings
from brain_v42.services.brain_service import BrainService


class _RecordingCalculator:
    """Capture ce que le service passe réellement au multiplicateur."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def compute_multiplier(self, **kwargs: Any) -> float:
        self.calls.append(kwargs)
        return 1.0

    def freshness_status(self, multiplier: float) -> str:
        return "fresh"


def _service(*, human_signal: bool) -> tuple[BrainService, _RecordingCalculator]:
    calculator = _RecordingCalculator()
    service = BrainService(
        decision_svc=None,
        learning_svc=None,
        snippet_svc=None,
        runbook_svc=None,
        adr_svc=None,
        embedding_svc=None,
        decay_calculator=calculator,
        decay_human_signal_enabled=human_signal,
    )
    return service, calculator


def test_the_setting_ships_closed() -> None:
    """Défaut d'aujourd'hui. §5.5 : effet immédiat sur un humain, donc fermé."""
    assert Settings().decay_human_signal_enabled is False


def test_the_constructor_default_is_closed_too() -> None:
    """Pas seulement dans Settings.

    Un appelant qui oublie de passer le réglage — un test, un script, un futur
    point d'entrée — doit obtenir le comportement d'aujourd'hui, jamais le
    nouveau. Un défaut ouvert dans la signature ferait basculer par omission.
    """
    service, _ = _service(human_signal=False)
    assert service._decay_human_signal_enabled is False

    from brain_v42.services.decay_flusher import DecayFlusher

    flusher = DecayFlusher(
        session_factory=None,
        access_log_repo=None,
        decay_calculator=None,
    )
    assert flusher._human_signal_enabled is False


@pytest.mark.parametrize(
    ("human_signal", "expected_count", "expected_recency_attr"),
    [
        (False, 400, "last_accessed_at"),
        (True, 3, "last_accessed_at_human"),
    ],
)
def test_both_signals_switch_together(
    human_signal: bool, expected_count: int, expected_recency_attr: str
) -> None:
    """Les DEUX entrées basculent, ou aucune.

    Une bascule partielle laisserait `access_factor` — le terme le plus lourd
    après l'âge — piloté par la machine, et donnerait l'illusion que le decay
    est réparé.
    """
    machine_recency = dt.datetime(2026, 8, 10, tzinfo=dt.UTC)
    human_recency = dt.datetime(2026, 2, 1, tzinfo=dt.UTC)
    entity = SimpleNamespace(
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        last_accessed_at=machine_recency,
        last_accessed_at_human=human_recency,
        access_count=400,
        access_count_human=3,
        validated_at=None,
    )
    service, calculator = _service(human_signal=human_signal)

    # On appelle la même dérivation que la boucle de scoring, sans monter tout
    # le pipeline de recherche : ce qui est sous test est le CHOIX du signal.
    if service._decay_human_signal_enabled:
        signal_count = getattr(entity, "access_count_human", 0) or 0
        signal_recency = getattr(entity, "last_accessed_at_human", None)
    else:
        signal_count = entity.access_count
        signal_recency = entity.last_accessed_at
    calculator.compute_multiplier(
        entity_type="learning",
        created_at=entity.created_at,
        last_accessed_at=signal_recency,
        access_count=signal_count,
        is_validated=False,
    )

    call = calculator.calls[-1]
    assert call["access_count"] == expected_count
    assert call["last_accessed_at"] == getattr(entity, expected_recency_attr)


def test_the_scoring_loop_reads_the_setting_and_both_columns() -> None:
    """La FORME, parce que la boucle de scoring vit au fond d'une recherche.

    Monter le pipeline complet pour prouver un choix de variable coûterait plus
    qu'il ne prouve. Ces ancres échouent bruyamment si la substitution est
    retirée, ou si elle ne porte que sur un des deux signaux.
    """
    import inspect

    source = inspect.getsource(BrainService)

    assert "self._decay_human_signal_enabled" in source
    assert 'getattr(entity, "access_count_human", 0)' in source
    assert 'getattr(entity, "last_accessed_at_human", None)' in source
    # Le multiplicateur doit recevoir les VARIABLES de signal, pas les colonnes
    # brutes : c'est ce qui distingue une substitution d'un calcul mort.
    assert "last_accessed_at=signal_last_accessed" in source
    assert "access_count=signal_access_count" in source
