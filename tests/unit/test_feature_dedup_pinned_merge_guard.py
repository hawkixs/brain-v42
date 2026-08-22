"""`merge_features` doit refuser d'absorber une feature ÉPINGLÉE.

FICHIER FRÈRE : `test_feature_dedup_pinned_guard.py` couvre déjà la même règle
sur `find_candidates`. Les deux ne font pas doublon — ils gardent deux moments
différents, et c'est tout le sujet ici.

Le ticket `4a6fe67e` visait « le dedup applique oldest-absorbs-newest sans
garde sur `pinned` ». Ce n'est plus vrai à la lettre : `find_candidates` porte
cette garde depuis son fichier frère. Mais elle vit dans le chemin de
DÉCOUVERTE, pas dans le chemin de MUTATION, et `merge_features` — le seul qui
écrit — ne la portait pas.

Deux façons d'absorber une épinglée subsistent donc :

1. TOCTOU. `run_dedup_loop` collecte TOUS les candidats d'un projet, puis les
   fusionne un par un, chacun dans sa propre session et après un aller-retour
   reranker. Un humain qui épingle une feature pendant cette fenêtre voit son
   geste ignoré : la décision a été prise sur un instantané d'avant.
2. Appel direct. `merge_features` est publique et le docstring du module la
   documente comme telle. Un appelant qui ne passe pas par `find_candidates`
   n'hérite d'aucune garde.

La garde doit donc lire `pinned` sur la ligne FOR UPDATE — la seule
autorité — et non sur l'instantané passé en argument. Le test 4 le prouve :
instantané non épinglé, ligne autoritaire épinglée, la fusion doit être refusée.

TÉMOIN NÉGATIF, dans ce fichier et non ailleurs : `test_unpinned_source_still_merges`
et `test_pinned_target_still_absorbs`. Sans eux, une garde trop large
désactiverait le dedup et la suite resterait verte — on aurait « protégé » les
épinglées en cassant la fonctionnalité.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.services.feature_dedup_job import FeatureDedupJob


def _row(
    *,
    name: str,
    pinned: bool,
    feature_id: uuid.UUID | None = None,
    description: str = "desc",
) -> MagicMock:
    """Ligne de features mockée.

    `pinned` est OBLIGATOIRE et sans défaut : l'attribut par défaut d'un
    MagicMock est truthy, donc une ligne construite sans le poser simulerait
    une épinglée sans le dire. C'est exactement le piège qui rendrait ce
    fichier vert pour la mauvaise raison.
    """
    row = MagicMock()
    row.id = feature_id or uuid.uuid4()
    row.name = name
    row.description = description
    row.embedding = [0.1] * 1536
    row.created_at = 1000.0
    row.status = "research"
    row.merged_into = None
    row.pinned = pinned
    return row


def _job() -> tuple[FeatureDedupJob, AsyncMock]:
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    embedding_svc = AsyncMock()
    embedding_svc.embed = AsyncMock(return_value=[0.5] * 1536)
    reranker = AsyncMock()
    reranker.rerank = AsyncMock(return_value=[0.9])

    job = FeatureDedupJob(
        session_factory=factory,
        reranker=reranker,
        embedding_svc=embedding_svc,
    )
    return job, session


def _wire_recheck(session: AsyncMock, rows: list[MagicMock]) -> None:
    """La première exécution est le SELECT … FOR UPDATE ; les suivantes du DML."""
    recheck = MagicMock()
    recheck.fetchall.return_value = rows
    session.execute = AsyncMock(side_effect=[recheck] + [MagicMock() for _ in range(8)])


def _wrote_anything(session: AsyncMock) -> bool:
    """Vrai dès qu'une instruction autre que le SELECT FOR UPDATE est partie."""
    return session.execute.await_count > 1


class TestPinnedSourceIsNeverAbsorbed:
    @pytest.mark.asyncio
    async def test_pinned_source_is_refused(self) -> None:
        """Le cas du ticket : la NOUVELLE est épinglée, l'ancienne l'absorberait.

        `source` est la ligne qui DISPARAÎT (status='archived', merged_into=target).
        L'épinglage est le geste par lequel un humain dit « n'y touchez pas » :
        la fusion doit être refusée, pas exécutée.
        """
        job, session = _job()
        target = _row(name="Ancienne", pinned=False)
        source = _row(name="Épinglée par un humain", pinned=True)
        _wire_recheck(session, [target, source])

        result = await job.merge_features(session, target, source)

        assert result is False, (
            f"merge_features a rendu {result!r} sur une source ÉPINGLÉE — "
            "elle vient d'archiver un engagement explicite de l'opérateur"
        )
        assert not _wrote_anything(session), (
            "aucune écriture ne doit partir quand la source est épinglée ; "
            f"{session.execute.await_count - 1} instruction(s) ont été exécutées "
            "après le SELECT FOR UPDATE"
        )

    @pytest.mark.asyncio
    async def test_both_pinned_is_refused(self) -> None:
        """Les DEUX épinglées : on bloque, on ne devine pas.

        Cas remonté explicitement plutôt qu'arbitré dans le code : rien ne dit
        laquelle des deux intentions humaines doit céder.
        """
        job, session = _job()
        target = _row(name="Ancienne épinglée", pinned=True)
        source = _row(name="Nouvelle épinglée", pinned=True)
        _wire_recheck(session, [target, source])

        result = await job.merge_features(session, target, source)

        assert result is False, (
            f"merge_features a rendu {result!r} alors que les DEUX sont épinglées"
        )
        assert not _wrote_anything(session)

    @pytest.mark.asyncio
    async def test_pinned_read_from_authoritative_row_not_snapshot(self) -> None:
        """TOCTOU : l'instantané dit « pas épinglée », la base dit « épinglée ».

        C'est la fenêtre réelle de `run_dedup_loop` — les candidats sont
        collectés en bloc, puis fusionnés un par un. Une garde qui lirait
        l'argument passerait ici, et le geste de l'humain serait perdu.
        """
        job, session = _job()
        source_id = uuid.uuid4()
        target = _row(name="Ancienne", pinned=False)

        # Instantané pris AVANT que l'humain n'épingle.
        stale_snapshot = _row(name="Nouvelle", pinned=False, feature_id=source_id)
        # Ligne autoritaire relue FOR UPDATE, APRÈS l'épinglage.
        authoritative = _row(name="Nouvelle", pinned=True, feature_id=source_id)

        _wire_recheck(session, [target, authoritative])

        result = await job.merge_features(session, target, stale_snapshot)

        assert result is False, (
            f"merge_features a rendu {result!r} : la garde a cru l'instantané "
            "plutôt que la ligne FOR UPDATE, donc elle ne ferme pas la fenêtre TOCTOU"
        )
        assert not _wrote_anything(session)


class TestDedupStillWorks:
    """Témoin négatif — sans lui, une garde trop large passerait pour un succès."""

    @pytest.mark.asyncio
    async def test_unpinned_source_still_merges(self) -> None:
        """Le cas nominal doit continuer d'être dédupliqué."""
        job, session = _job()
        target = _row(name="Ancienne", pinned=False)
        source = _row(name="Nouvelle", pinned=False)
        _wire_recheck(session, [target, source])

        result = await job.merge_features(session, target, source)

        assert result is True, (
            f"merge_features a rendu {result!r} sur une paire NON épinglée — "
            "le dedup a été désactivé, pas gardé"
        )
        assert _wrote_anything(session), (
            "une fusion nominale doit émettre du DML après le SELECT FOR UPDATE"
        )

    @pytest.mark.asyncio
    async def test_pinned_target_still_absorbs(self) -> None:
        """Une épinglée en CIBLE reste autorisée : elle SURVIT à la fusion.

        Interdire ce cas protégerait l'épinglage en empêchant précisément ce
        qu'il demande — que cette feature-là reste.
        """
        job, session = _job()
        target = _row(name="Ancienne ÉPINGLÉE", pinned=True)
        source = _row(name="Nouvelle banale", pinned=False)
        _wire_recheck(session, [target, source])

        result = await job.merge_features(session, target, source)

        assert result is True, (
            f"merge_features a rendu {result!r} alors que seule la CIBLE est "
            "épinglée — la cible survit, il n'y a rien à protéger ici"
        )
        assert _wrote_anything(session)
