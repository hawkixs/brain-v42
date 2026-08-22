"""Contrat schéma ↔ modèle : ce que le decay lit en base doit arriver au modèle.

Ce test existe à cause d'un défaut mesuré le 2026-08-22, et il est écrit pour
rendre sa CLASSE impossible, pas pour épingler son instance.

Le défaut : `decay_human_signal_enabled` substituait
`getattr(entity, "access_count_human", 0)` aux compteurs machine, sur des
modèles Pydantic qui ne déclaraient pas ce champ. Le `getattr` tombait donc
toujours sur son défaut. Le drapeau n'était pas un interrupteur entre signal
machine et signal humain : c'était un interrupteur entre signal machine et
**RIEN** — pendant que `DecayFlusher` lisait, lui, les vraies colonnes en
SQLAlchemy Core. L'armer aurait fait diverger les deux chemins sur la même
ligne.

**Pourquoi ce test-ci et pas un test de comportement.** La suite portait déjà
`test_decay_human_signal.py`, qui fabrique un `SimpleNamespace` muni des deux
attributs puis recopie la logique de production dans le corps du test. Il prouve
la FORME du code et ne peut pas voir l'ARRIVÉE de la donnée : c'est lui qui a
laissé passer ce défaut tout le chantier. La preuve de bout en bout vit
désormais en intégration
(`tests/integration/db/test_decay_human_signal_hydration.py`, contre de vraies
lignes). Ce test-ci est le filet qui tourne SANS base, en CI : il dérive les
noms de colonnes des vrais objets `Table` et des vraies listes du flusher — donc
il ne peut pas se tromper en même temps qu'eux, ce qui était exactement le mode
de panne.
"""

from __future__ import annotations

import pytest

from brain_v42.config import Settings
from brain_v42.db.tables import (
    adrs,
    decisions,
    indexed_plans,
    learnings,
    runbooks,
    snippets,
)
from brain_v42.models.adr import ADR
from brain_v42.models.decision import Decision
from brain_v42.models.indexed_plan import IndexedPlan
from brain_v42.models.indexed_plan_chunk import IndexedPlanChunk
from brain_v42.models.learning import Learning
from brain_v42.models.runbook import Runbook
from brain_v42.models.snippet import Snippet

#: Les colonnes du signal humain (migrations 041 et 044). Elles basculent
#: ENSEMBLE : `access_count` pèse 0,2 dans la formule, `last_accessed_at` 0,3 —
#: n'en porter qu'une répare 0,2 des 0,5 pilotés par la lecture et donne
#: l'illusion que le decay est réparé.
_HUMAN_COLUMNS = ("access_count_human", "last_accessed_at_human")

#: Les six tables suivies par le decay, chacune avec le modèle que les dépôts
#: hydratent depuis elle. `indexed_plans` est l'asymétrique : ses chunks n'ont
#: pas de colonnes `_human`, donc le signal humain d'un plan ne peut venir que
#: du parent.
_TABLE_TO_MODEL = (
    (decisions, Decision),
    (learnings, Learning),
    (snippets, Snippet),
    (runbooks, Runbook),
    (adrs, ADR),
    (indexed_plans, IndexedPlan),
)


@pytest.mark.parametrize(
    ("table", "model"),
    _TABLE_TO_MODEL,
    ids=[table.name for table, _ in _TABLE_TO_MODEL],
)
def test_every_human_column_has_a_model_field(table, model) -> None:
    """Ce que la base porte, le modèle doit le déclarer — sinon Pydantic le jette.

    Les projections de recherche renvoyaient DÉJÀ ces colonnes
    (`_search_columns()` n'exclut que `embedding` et `search_vector`) : la donnée
    arrivait dans la ligne et se perdait au passage du modèle, faute de champ.
    """
    for column in _HUMAN_COLUMNS:
        assert column in table.c, f"{table.name} devrait porter {column}"
        assert column in model.model_fields, (
            f"{model.__name__} ne déclare pas {column} : la colonne existe, "
            f"le SELECT la renvoie, et Pydantic la jette en silence"
        )


def test_the_plan_chunk_carries_the_parent_human_signal() -> None:
    """Le seul type où le signal humain ne peut PAS venir de l'entité notée.

    `indexed_plan_chunks` n'a aucune colonne `_human` ; le chemin machine le sait
    déjà et substitue les compteurs du parent. Sans champ `parent_*_human` sur le
    chunk, la branche humaine notait tout plan à 0 accès et récence nulle — pas
    une divergence de valeur, une impossibilité structurelle.
    """
    from brain_v42.db.tables import indexed_plan_chunks

    for column in _HUMAN_COLUMNS:
        assert column not in indexed_plan_chunks.c, (
            f"si {column} apparaît sur les chunks, ce test et la substitution "
            f"parent de brain_service doivent être revus ensemble"
        )
        assert f"parent_{column}" in IndexedPlanChunk.model_fields


def test_the_flusher_and_the_models_read_the_same_names() -> None:
    """La divergence est le danger, pas l'absence.

    Le flusher lit `table.c.access_count_human` en Core ; le service lit
    `entity.access_count_human` en Pydantic. Tant que le modèle ne portait pas le
    champ, armer le drapeau donnait DEUX valeurs pour une même ligne : une
    constante d'un côté, la donnée de l'autre. On compare donc les noms que le
    flusher SELECT réellement à ceux que les modèles déclarent, au lieu de
    recopier une liste à la main — une liste recopiée dériverait avec le code
    qu'elle prétend garder.
    """
    import inspect

    from brain_v42.services.decay_flusher import DecayFlusher

    flusher_source = inspect.getsource(DecayFlusher)
    for column in _HUMAN_COLUMNS:
        assert f"table.c.{column}" in flusher_source, (
            f"le flusher ne lit plus {column} : si cette lecture disparaît, "
            f"c'est l'autre chemin qui devient la seule source"
        )
        for _table, model in _TABLE_TO_MODEL:
            assert column in model.model_fields


def test_the_setting_is_still_closed_by_default() -> None:
    """Ce lot répare l'hydratation. Il n'arme RIEN.

    L'effet visible attendu est zéro : rendre l'armement futur honnête, pas
    l'exécuter. Ouvrir ce drapeau change l'ordre des résultats de recherche le
    jour même — c'est un geste d'opérateur.
    """
    assert Settings.model_fields["decay_human_signal_enabled"].default is False


class TestTheModelReachesTheCalculator:
    """Second maillon : du modèle jusqu'au multiplicateur, par la VRAIE boucle.

    Les deux tests se composent, et il faut les lire ensemble : l'intégration
    prouve que la donnée va de la BASE au MODÈLE ; celui-ci prouve qu'elle va du
    MODÈLE au CALCUL. Aucun des deux ne suffit seul, et c'est précisément la
    moitié manquante qui avait laissé passer le défaut.

    Ce qui le distingue du motif interdit : il appelle
    `BrainService._build_search_results`, la méthode de production, sur une
    vraie instance d'`IndexedPlanChunk`. Il ne recopie pas la substitution dans
    son propre corps — mutation vérifiée : retirer la bascule parent de
    `brain_service` fait rougir ce test, et rien d'autre dans la suite ne la
    voyait.
    """

    @staticmethod
    def _chunk(**overrides):
        import datetime as dt
        import uuid

        payload = {
            "id": uuid.uuid4(),
            "plan_id": uuid.uuid4(),
            "section_title": "s",
            "section_path": "s",
            "content": "c",
            "section_order": 1,
            "word_count": 1,
            "project_key": "integ-decay",
            "plan_type": "plan",
            "status": "active",
            "created_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            "access_count": 7,
            "last_accessed_at": dt.datetime(2026, 8, 10, tzinfo=dt.UTC),
            "parent_access_count": 400,
            "parent_last_accessed_at": dt.datetime(2026, 8, 10, tzinfo=dt.UTC),
            "parent_created_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            "parent_access_count_human": 3,
            "parent_last_accessed_at_human": dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
        }
        payload.update(overrides)
        return IndexedPlanChunk.model_validate(payload)

    def _score(self, *, human_signal: bool):
        from brain_v42.services.brain_service import BrainService

        class _Recorder:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def compute_multiplier(self, **kwargs):
                self.calls.append(kwargs)
                return 1.0

            def freshness_status(self, multiplier: float) -> str:
                return "fresh"

        recorder = _Recorder()
        service = BrainService(
            decision_svc=None,
            learning_svc=None,
            snippet_svc=None,
            runbook_svc=None,
            adr_svc=None,
            embedding_svc=None,
            decay_calculator=recorder,
            decay_human_signal_enabled=human_signal,
        )
        service._build_search_results({"plan": [(self._chunk(), 0.9)]}, limit=10)
        return recorder.calls[-1]

    def test_armed_the_plan_is_scored_on_the_parent_human_counters(self) -> None:
        call = self._score(human_signal=True)
        assert call["access_count"] == 3
        assert call["last_accessed_at"].month == 2

    def test_closed_the_plan_is_scored_on_the_parent_machine_counters(self) -> None:
        """Témoin négatif : sans lui, un calcul figé à 3 passerait aussi."""
        call = self._score(human_signal=False)
        assert call["access_count"] == 400
        assert call["last_accessed_at"].month == 8

    def test_armed_a_learning_is_scored_on_its_own_human_counters(self) -> None:
        """Le cas NON-plan, et il manquait.

        MESURÉ : avec le seul cas `plan`, figer la branche humaine sur le
        compteur machine laissait la suite VERTE — la substitution parent
        réécrit `signal_access_count` juste après, donc elle masquait la ligne
        qu'on croyait tester. Les cinq types de connaissance n'ont pas de
        parent : c'est ici, et seulement ici, que cette ligne se voit.
        """
        import datetime as dt
        import uuid

        learning = Learning.model_validate(
            {
                "id": uuid.uuid4(),
                "topic": "t",
                "insight": "i",
                "project_key": "integ-decay",
                "created_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                "updated_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                "access_count": 400,
                "last_accessed_at": dt.datetime(2026, 8, 10, tzinfo=dt.UTC),
                "access_count_human": 3,
                "last_accessed_at_human": dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
            }
        )
        from brain_v42.services.brain_service import BrainService

        class _Recorder:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def compute_multiplier(self, **kwargs):
                self.calls.append(kwargs)
                return 1.0

            def freshness_status(self, multiplier: float) -> str:
                return "fresh"

        recorder = _Recorder()
        BrainService(
            decision_svc=None,
            learning_svc=None,
            snippet_svc=None,
            runbook_svc=None,
            adr_svc=None,
            embedding_svc=None,
            decay_calculator=recorder,
            decay_human_signal_enabled=True,
        )._build_search_results({"learning": [(learning, 0.9)]}, limit=10)

        call = recorder.calls[-1]
        assert call["access_count"] == 3
        assert call["last_accessed_at"].month == 2
