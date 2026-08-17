"""Le canary doit mesurer le régime que la NUIT impose, pas un régime à lui.

Le 2026-08-16 le primaire roadmap a été basculé sur `mistralai/mistral-nemotron`
sur la foi de deux canaries : 3/3 batches valides, 12,6 à 20,4 s/batch. La
première nuit réelle l'a réfuté — TimeoutError, circuit ouvert, 10/10 batches
retombés sur le secours 8B. La preuve ne prédisait pas la production.

Le canary appelait `_curate_llm_attempt`, l'étage SOUS celui que la nuit
utilise. Il manquait donc quatre contraintes à la fois, pas une :

  1. la borne de temps — la nuit enveloppe chaque tentative dans
     `asyncio.timeout(batch_llm_window(...))`, soit 60 s à dix batches ;
     le canary ne laissait courir que le read timeout httpx, 180 s ;
  2. le plafond de complétion (`BIG_MODEL_COMPLETION_TOKENS`) ;
  3. le compactage du batch (`_compact_batch`) ;
  4. le CIRCUIT — un seul échec du primaire l'écarte de tous les batches
     suivants du run.

Le quatrième est celui qui explique « 10/10 sur le secours » : un batch perdu
condamne les neuf autres. Un canary qui mesure chaque batch isolément ne peut
structurellement pas l'observer, et un canary qui laisse le secours rattraper
sans le dire rend un vert pour un primaire mort — la panne exacte de qwen 80B,
morte le 2026-07-27 et découverte le 08-05 après dix nuits vertes.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from scripts.canary_roadmap_model import measure_model
from scripts.roadmap_curate import (
    LLM_ATTEMPT_TIMEOUT_S,
    NIGHT_BUDGET_S,
    BatchOutcome,
    FeatureCard,
    ProjectBatch,
    batch_llm_window,
)

_PRIMARY = "mistralai/mistral-nemotron"
_FALLBACK = "meta/llama-3.1-8b-instruct"


def _batches(count: int) -> list[ProjectBatch]:
    return [
        ProjectBatch(
            project_key=f"projet-{index}",
            features=[FeatureCard(id=uuid4(), name=f"F{index}", status="building", pinned=False)],
        )
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_each_batch_is_bounded_by_the_night_window_not_the_default() -> None:
    """La fenêtre doit être celle de la nuit, dérivée du budget restant."""
    windows: list[float] = []

    async def curate(
        client: Any,
        model: str,
        batch: ProjectBatch,
        sleep: Any,
        *,
        llm_timeout_s: float,
        fallback_model: str | None,
        disabled_models: set[str],
        proposer_only: bool | None = None,
    ) -> BatchOutcome:
        windows.append(llm_timeout_s)
        return BatchOutcome(batch=batch, drafts=[], model_used=model)

    batches = _batches(10)
    await measure_model(
        None,
        _PRIMARY,
        batches,
        fallback_model=_FALLBACK,
        curate=curate,
        clock=lambda: 0.0,
    )

    assert len(windows) == 10
    assert windows[0] == pytest.approx(60.0), (
        "à dix batches la nuit borne la première tentative à 60 s; le canary "
        f"laissait courir le read timeout httpx, 180 s: {windows[0]}"
    )
    # Le contrat n'est pas une constante mais la FONCTION de la nuit : la part
    # d'un batch dépend du budget restant et du nombre de batches restants.
    # L'horloge est figée dans ce test, donc `elapsed` reste nul et les derniers
    # batches héritent d'une part large — c'est `batch_llm_window` qui décide,
    # et c'est exactement ce qu'on veut épingler.
    assert windows == [batch_llm_window(NIGHT_BUDGET_S, 0.0, 10 - index) for index in range(10)], (
        windows
    )
    assert all(window <= LLM_ATTEMPT_TIMEOUT_S for window in windows), windows


@pytest.mark.asyncio
async def test_a_primary_killed_on_the_first_batch_is_never_blessed_by_its_fallback() -> None:
    """Le circuit est partagé par tout le run, et le verdict nomme le primaire.

    Le secours peut rendre dix batches parfaitement valides : le candidat mesuré
    n'en a porté aucun. Un canary qui compte « 10/10 valides » ici est celui qui
    a fait basculer la production sur un modèle mort.
    """

    async def curate(
        client: Any,
        model: str,
        batch: ProjectBatch,
        sleep: Any,
        *,
        llm_timeout_s: float,
        fallback_model: str | None,
        disabled_models: set[str],
        proposer_only: bool | None = None,
    ) -> BatchOutcome:
        # Reproduit curate_batch : le primaire meurt une fois puis reste écarté,
        # et le secours sert tous les batches suivants.
        if model in disabled_models:
            return BatchOutcome(
                batch=batch,
                drafts=[],
                model_used=fallback_model,
                fallback_used=True,
                primary_error=f"{model}: écarté (circuit ouvert plus tôt dans ce run)",
            )
        disabled_models.add(model)
        return BatchOutcome(
            batch=batch,
            drafts=[],
            model_used=fallback_model,
            fallback_used=True,
            primary_error=f"{model}: TimeoutError",
        )

    measurement = await measure_model(
        None,
        _PRIMARY,
        _batches(10),
        fallback_model=_FALLBACK,
        curate=curate,
        clock=lambda: 0.0,
    )

    assert measurement.carried_by_primary == 0, (
        "le candidat n'a porté aucun batch; le compter valide, c'est publier "
        "le secours sous le nom du primaire"
    )
    assert measurement.rescued_by_fallback == 10
    assert measurement.circuit_opened_at == 1, (
        "le circuit doit s'ouvrir au premier batch et rester ouvert: "
        f"{measurement.circuit_opened_at}"
    )
    assert measurement.verdict == "MORT", measurement.verdict


@pytest.mark.asyncio
async def test_a_primary_that_carries_every_batch_is_alive() -> None:
    """Contre-épreuve : sans repli, le verdict reste vivant."""

    async def curate(
        client: Any,
        model: str,
        batch: ProjectBatch,
        sleep: Any,
        *,
        llm_timeout_s: float,
        fallback_model: str | None,
        disabled_models: set[str],
        proposer_only: bool | None = None,
    ) -> BatchOutcome:
        return BatchOutcome(batch=batch, drafts=[], model_used=model)

    measurement = await measure_model(
        None,
        _PRIMARY,
        _batches(3),
        fallback_model=_FALLBACK,
        curate=curate,
        clock=lambda: 0.0,
    )

    assert measurement.carried_by_primary == 3
    assert measurement.rescued_by_fallback == 0
    assert measurement.circuit_opened_at is None
    assert measurement.verdict == "OK"


@pytest.mark.asyncio
async def test_a_candidate_is_measured_in_the_regime_it_would_have_once_adopted() -> None:
    """`PROPOSER_ONLY_MODELS` est DÉRIVÉ de `DEFAULT_ROADMAP_MODEL`.

    Un candidat qu'on évalue pour la case de primaire DRY y entrerait donc le
    jour de son adoption, et changerait de routage au même instant : parseur
    proposer-only, plafonds et retries du chemin `_curate_managed_model_chain`.
    Le mesurer hors de cet ensemble, c'est mesurer un régime qu'il n'aura plus —
    la faute exacte que ce fichier corrige, répétée un cran plus loin.
    """
    import scripts.roadmap_curate as rc

    seen: list[bool] = []

    async def curate(
        client: Any,
        model: str,
        batch: ProjectBatch,
        sleep: Any,
        *,
        llm_timeout_s: float,
        fallback_model: str | None,
        disabled_models: set[str],
        proposer_only: bool | None = None,
    ) -> BatchOutcome:
        seen.append(proposer_only is True)
        return BatchOutcome(batch=batch, drafts=[], model_used=model)

    candidate = "openai/gpt-oss-120b"
    assert candidate not in rc.PROPOSER_ONLY_MODELS

    await measure_model(
        None,
        candidate,
        _batches(2),
        fallback_model=_FALLBACK,
        curate=curate,
        clock=lambda: 0.0,
        as_dry_primary=True,
    )

    assert seen == [True, True], (
        "le candidat doit être routé comme le primaire DRY qu'il deviendrait"
    )
    # Et le global n'est jamais muté : une mesure ne reroute pas la production.
    assert candidate not in rc.PROPOSER_ONLY_MODELS
