"""Une feature épinglée n'est jamais absorbée par la déduplication.

`pinned` est la marque d'un engagement explicite de l'opérateur. Le job de
dédup, lui, ne l'a jamais lue : `grep -c pinned feature_dedup_job.py` rendait 0
alors que `_get_all_features` fait un `select(features)` qui charge la colonne.

La règle « le plus ancien absorbe le plus récent » (`find_candidates`, comparaison
sur `created_at`) est donc appliquée aux engagements comme au reste. Une feature
épinglée créée explicitement s'est fait manger le 2026-08-14 à 19:17,
`reranker_score=0.8312975168228149` — et le reranker ne compare que les NOMS,
jamais les descriptions qui portent le périmètre.

CE QUE LA GARDE FAIT, ET CE QU'ELLE NE FAIT PAS. Elle refuse la fusion quand la
SOURCE est épinglée — la source est celle qui disparaît. Elle ne swappe pas les
rôles pour faire absorber par l'épinglée : ce serait décider du survivant sur
l'épinglage plutôt que sur l'âge, et fusionner quand même deux périmètres que
rien ne prouve identiques. Une épinglée cible qui absorbe une non-épinglée reste
autorisée, c'est le cas nominal.

PIÈGE DE TEST À NE PAS REPRODUIRE : les lignes de features sont des `MagicMock`,
donc `row.pinned` est truthy par défaut si le champ n'est pas posé. Une garde
écrite sans y penser rendrait TOUTES les fusions impossibles, et la suite
existante passerait au vert en ne fusionnant plus rien. Les tests ci-dessous
posent donc `pinned` explicitement des deux côtés.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.services.feature_dedup_job import FeatureDedupJob


def _row(*, pinned: bool, created_at: float, name: str, similarity: float | None = None):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.name = name
    row.description = f"description de {name}"
    row.embedding = [0.1] * 1536
    row.created_at = created_at
    row.status = "research"
    row.merged_into = None
    row.pinned = pinned
    if similarity is not None:
        row.similarity = similarity
    return row


def _job(all_features, neighbors_by_id, reranker_score: float = 0.95) -> FeatureDedupJob:
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    reranker = AsyncMock()
    reranker.rerank = AsyncMock(return_value=[reranker_score])

    job = FeatureDedupJob(
        session_factory=factory,
        reranker=reranker,
        embedding_svc=AsyncMock(),
        mutation_guard=MagicMock(),
    )
    job._get_all_features = AsyncMock(return_value=all_features)  # type: ignore[method-assign]
    job._find_neighbors = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda _s, feature, _p: neighbors_by_id.get(feature.id, [])
    )
    return job


@pytest.mark.asyncio
async def test_a_pinned_feature_is_never_proposed_for_absorption() -> None:
    """Le cas mesuré le 2026-08-14 : la récente est épinglée, l'ancienne la mange."""
    ancienne = _row(pinned=False, created_at=1000.0, name="Roadmap curation")
    epinglee = _row(pinned=True, created_at=2000.0, name="Roadmap curation v2", similarity=0.93)

    job = _job([ancienne, epinglee], {ancienne.id: [epinglee]})
    candidates = await job.find_candidates("brain-v42")

    assert candidates == [], "une feature épinglée a été proposée à l'absorption"


@pytest.mark.asyncio
async def test_an_unpinned_feature_is_still_absorbed_normally() -> None:
    """La garde ne doit pas éteindre la déduplication — sinon elle est indétectable."""
    ancienne = _row(pinned=False, created_at=1000.0, name="Roadmap curation")
    recente = _row(pinned=False, created_at=2000.0, name="Roadmap curation v2", similarity=0.93)

    job = _job([ancienne, recente], {ancienne.id: [recente]})
    candidates = await job.find_candidates("brain-v42")

    assert len(candidates) == 1
    target, source, _score = candidates[0]
    assert target.id == ancienne.id
    assert source.id == recente.id


@pytest.mark.asyncio
async def test_a_pinned_target_may_still_absorb_an_unpinned_source() -> None:
    """Cas nominal : l'engagement survit et mange le doublon, c'est l'effet voulu."""
    epinglee_ancienne = _row(pinned=True, created_at=1000.0, name="Roadmap curation")
    recente = _row(pinned=False, created_at=2000.0, name="Roadmap curation v2", similarity=0.93)

    job = _job([epinglee_ancienne, recente], {epinglee_ancienne.id: [recente]})
    candidates = await job.find_candidates("brain-v42")

    assert len(candidates) == 1
    assert candidates[0][0].id == epinglee_ancienne.id


@pytest.mark.asyncio
async def test_two_pinned_features_are_left_alone() -> None:
    """Deux engagements : la dédup n'a pas à choisir lequel meurt."""
    a = _row(pinned=True, created_at=1000.0, name="Roadmap curation")
    b = _row(pinned=True, created_at=2000.0, name="Roadmap curation v2", similarity=0.93)

    job = _job([a, b], {a.id: [b]})

    assert await job.find_candidates("brain-v42") == []


@pytest.mark.asyncio
async def test_the_guard_reads_pinned_and_does_not_rely_on_truthiness() -> None:
    """Une source dont `pinned` vaut None ou 0 doit rester fusionnable.

    Ce test existe parce que la colonne est nullable en base : un `if row.pinned`
    naïf traiterait `None` comme non-épinglé par chance, mais un `is not False`
    le traiterait comme épinglé et éteindrait la dédup sur toute ligne héritée.
    """
    ancienne = _row(pinned=False, created_at=1000.0, name="Roadmap curation")
    recente = _row(pinned=False, created_at=2000.0, name="Roadmap curation v2", similarity=0.93)
    recente.pinned = None

    job = _job([ancienne, recente], {ancienne.id: [recente]})

    assert len(await job.find_candidates("brain-v42")) == 1
