"""L'absorption dérivée face à une connexion MORTE — la moitié qui sert l'utilisateur.

Ce que les tests existants prouvent déjà, et qui ne suffit pas : la dérivation
dépose l'artefact dans la traçante de SA connexion, et l'absorption le rend à la
session de l'utilisateur *tant que la connexion n'a pas changé*. Les deux vivent
dans la même scène, sous le même identifiant de transport, et l'appariement ne
peut donc pas y échouer.

En production il change. `connection_id` est le `Mcp-Session-Id`, un identifiant
de TRANSPORT frappé par le SDK, tué par l'idle timeout de 900 s
(`mcp_http_session_idle_seconds`) bien avant que l'utilisateur ne ferme sa
session. Mesuré le 2026-08-25 sur la première fermeture réelle après armement :
l'artefact était dans la traçante de la connexion `7588e2a2…`, morte 37 minutes
plus tôt ; le `brain_session_end` est arrivé sur `b7d8e65b…`, dont la traçante
était vide. Ledger de l'utilisateur : 0. La durée de vie médiane d'une traçante
mesurée en base est sous 2 minutes, contre 16 h pour la session qu'elle sert.

**Le critère d'acceptation est la PROMESSE, pas le mécanisme** : un artefact créé
sans `brain_session_capture` atterrit dans la session de l'utilisateur à sa
fermeture, même si la connexion qui l'a déposé est morte entre-temps. Ces tests
ne disent RIEN de la clé d'appariement à retenir — elle est en arbitrage — et
doivent rester verts quelle qu'elle soit.

**Ils ne peuvent pas auto-réaliser leur hypothèse.** Le test ne tend jamais un
identifiant à la production : il pose le contextvar que `ProvenanceMiddleware`
poserait, et le code sous test résout ce qu'il veut à partir de là. Poser
l'identifiant à la main est exactement le geste qui rend la suite actuelle verte
pendant que la prod est inerte.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import (
    brain_session_artifacts,
    brain_sessions,
    learnings,
    project_contexts,
)
from brain_v42.models.learning import LearningCreate
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo
from brain_v42.repositories.pg_learning import PgLearningRepo

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class _Identity:
    """Miroir de `AutoOpenIdentity` — l'auto-ouverture ne lit que ces champs."""

    project_key: str
    connection_id: str
    started_by_actor: str = "integ-w20"
    nature: str = "agent"
    intent: str | None = None


@contextmanager
def _derived_capture(enabled: bool) -> Iterator[None]:
    """Ouvrir la dérivation pour de vrai : le drapeau est lu À L'APPEL."""
    from brain_v42.config import get_settings

    key = "BRAIN_SESSION_DERIVED_CAPTURE_ENABLED"
    previous = os.environ.get(key)
    os.environ[key] = "true" if enabled else "false"
    get_settings.cache_clear()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous
        get_settings.cache_clear()


@contextmanager
def _transport(connection_id: str) -> Iterator[str]:
    """Jouer le middleware, et RIEN d'autre.

    C'est la garde anti-triche de ce fichier. `ProvenanceMiddleware` pose le
    `Mcp-Session-Id` sur ce contextvar et n'en dit rien de plus ; tout le reste
    — quelle traçante, quel donneur — est une décision de la production. Un test
    qui tendrait l'identifiant à `absorb_derived_capture` prouverait la requête
    SQL et jamais l'appariement.
    """
    from brain_v42.provenance import get_current_transport, set_current_transport

    previous = get_current_transport()
    set_current_transport(connection_id)
    try:
        yield connection_id
    finally:
        set_current_transport(previous)


async def _absorb_from_the_current_connection(
    repo: PgBrainSessionRepo, session_id: UUID, client_key: str
) -> int:
    """Absorber exactement comme `BrainSessionService._absorb_derived`.

    La connexion vient du contextvar, jamais de l'appelant : c'est tout ce que
    le serveur sait au moment où l'utilisateur ferme sa session.

    `client_key` est passée parce que la GARDE D'IDENTITÉ vit dans l'absorption
    elle-même, dans la même transaction que la mutation. Un banc qui ne la
    passerait pas prouverait un chemin que la production n'emprunte pas.
    """
    from brain_v42.provenance import get_current_transport

    connection_id = (get_current_transport() or "").strip()
    assert connection_id, "le banc doit tourner sous un transport"
    return await repo.absorb_derived_capture(session_id, connection_id, client_key)


@pytest_asyncio.fixture
async def absorption_project(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[str]:
    project_key = f"integ-w20-{uuid4().hex[:10]}"
    async with session_factory.begin() as session:
        await session.execute(
            project_contexts.insert().values(
                project_key=project_key,
                name="Derived absorption integration",
                description="Isolated fixture for the derived-absorption bench",
                current_focus="focus de la session utilisateur",
            )
        )
    try:
        yield project_key
    finally:
        async with session_factory.begin() as session:
            await session.execute(
                brain_session_artifacts.delete().where(
                    brain_session_artifacts.c.session_id.in_(
                        sa.select(brain_sessions.c.id).where(
                            brain_sessions.c.project_key == project_key
                        )
                    )
                )
            )
            await session.execute(
                brain_sessions.delete().where(brain_sessions.c.project_key == project_key)
            )
            await session.execute(learnings.delete().where(learnings.c.project_key == project_key))
            await session.execute(
                project_contexts.delete().where(project_contexts.c.project_key == project_key)
            )


async def _ledger_owner(
    session_factory: async_sessionmaker[AsyncSession], knowledge_id: UUID
) -> UUID | None:
    """Relire le propriétaire DEPUIS LA BASE, jamais depuis le retour du tool."""
    async with session_factory() as session:
        owners = (
            (
                await session.execute(
                    sa.select(brain_session_artifacts.c.session_id).where(
                        brain_session_artifacts.c.knowledge_id == knowledge_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(owners) <= 1, "le ledger est EXCLUSIF : un artefact, un propriétaire"
    return UUID(str(owners[0])) if owners else None


async def _derive_one_artifact(learning_repo: PgLearningRepo, project_key: str) -> UUID:
    """Créer un artefact SANS aucune capture explicite. C'est tout le point.

    Par le VRAI dépôt : `derive_capture` est appelée depuis
    `BasePgRepository.create`, donc une insertion écrite à la main dans
    `learnings` ne dériverait rien et le banc décrirait un monde vide.
    """
    created = await learning_repo.create(
        LearningCreate(
            topic=f"w20 derived {uuid4().hex[:8]}",
            insight="Artefact du banc d'absorption ; aucune portée opérationnelle.",
            project_key=project_key,
            confidence="low",
        )
    )
    return UUID(str(created.id))


async def test_the_user_session_absorbs_what_a_dead_connection_left_behind(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,
) -> None:
    """LA promesse, à travers un changement de connexion. ROUGE aujourd'hui.

    Trois temps, et le second est celui que la production traverse ~26 fois par
    jour : la connexion qui a déposé l'artefact n'existe plus quand
    l'utilisateur ferme. Aucun redémarrage de serveur n'est simulé — il n'y en a
    pas besoin, et en simuler un testerait la mauvaise chose : changer de
    `Mcp-Session-Id` est TOUT le trou.
    """
    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)

    with _derived_capture(True):
        # (1) La session de l'utilisateur. Elle ne porte AUCUNE connexion —
        # mesuré en production : 490 lignes non-`agent`, zéro `connection_id`.
        started = await repo.start(absorption_project, "task-w20")
        user_session = UUID(str(started.session.id))

        # (2) Connexion A : une traçante s'ouvre, l'artefact s'y dérive seul.
        with _transport(uuid4().hex) as connection_a:
            tracer = await repo.auto_open(_Identity(absorption_project, connection_a))
            assert tracer is not None, "sans traçante, la scène ne prouve rien"
            artifact = await _derive_one_artifact(learning_repo, absorption_project)
            assert await _ledger_owner(session_factory, artifact) == UUID(str(tracer)), (
                "la dérivation elle-même est cassée — ce rouge ne dirait rien de l'absorption"
            )

        # (3) Connexion A est MORTE. En production l'idle timeout de 900 s l'a
        # tuée, et rien ne la rejoue. L'utilisateur revient sur une connexion
        # neuve, dont la traçante est vide, et ferme sa session.
        with _transport(uuid4().hex) as connection_b:
            assert connection_b != connection_a
            await repo.auto_open(_Identity(absorption_project, connection_b))
            moved = await _absorb_from_the_current_connection(repo, user_session, "task-w20")

    assert moved == 1, (
        "la session de l'utilisateur n'a rien absorbé : l'artefact est resté "
        "dans la traçante d'une connexion morte"
    )
    assert await _ledger_owner(session_factory, artifact) == user_session


async def test_the_same_scene_on_a_single_connection_already_converges(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,
) -> None:
    """Témoin de BANC, et il ne prétend rien sur le défaut.

    Scène identique, à une variable près : la connexion ne change pas. Si
    celui-ci est vert et l'autre rouge, le rouge porte sur le CHANGEMENT de
    connexion et non sur un banc cassé, un drapeau fermé ou une dérivation en
    panne. Sans ce témoin, un rouge ne vaudrait rien.
    """
    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)

    with _derived_capture(True), _transport(uuid4().hex) as connection:
        started = await repo.start(absorption_project, "task-w20-witness")
        user_session = UUID(str(started.session.id))
        await repo.auto_open(_Identity(absorption_project, connection))
        artifact = await _derive_one_artifact(learning_repo, absorption_project)
        moved = await _absorb_from_the_current_connection(repo, user_session, "task-w20-witness")

    assert moved == 1
    assert await _ledger_owner(session_factory, artifact) == user_session


async def test_the_user_session_never_carries_the_connection_that_served_it(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,
) -> None:
    """Pourquoi la session ne peut pas retrouver « sa » connexion toute seule.

    Mesuré en production le 2026-08-25 : `nature='user'` n'existe NULLE PART, et
    aucune session non-`agent` n'a jamais porté de `connection_id` (490 lignes,
    zéro). La résolution ne peut donc passer que par la connexion COURANTE de
    l'appel qui ferme — celle qui, précisément, n'est plus la bonne.

    Ce test est VERT aujourd'hui et doit le rester : la 046 refuse de promouvoir
    une session d'utilisateur en traçante, sous peine d'en faire un fantôme que
    le balayage 7 j ne peut plus atteindre.
    """
    repo = PgBrainSessionRepo(session_factory)

    with _derived_capture(True), _transport(uuid4().hex):
        started = await repo.start(absorption_project, "task-w20-identity")

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(brain_sessions.c.connection_id, brain_sessions.c.nature).where(
                        brain_sessions.c.id == started.session.id
                    )
                )
            )
            .mappings()
            .one()
        )

    assert (row["connection_id"], row["nature"]) == (None, None)


_ATTRIBUTION_MODE = sa.text(
    "SELECT attribution_mode FROM brain_session_artifacts WHERE knowledge_id = :knowledge_id"
)


async def _attribution_mode(
    session_factory: async_sessionmaker[AsyncSession], knowledge_id: UUID
) -> str | None:
    """Relire la CLÉ d'appariement en base — texte brut, jamais l'objet Table.

    Une constante SQLAlchemy se tairait sur une colonne absente au moment de la
    compilation ; ce texte-ci rougit franchement tant que la 048 n'est pas là.
    """
    async with session_factory() as session:
        rows = (await session.execute(_ATTRIBUTION_MODE, {"knowledge_id": knowledge_id})).all()
    assert rows, "aucune ligne de ledger pour cet artefact"
    return None if rows[0][0] is None else str(rows[0][0])


async def test_two_open_user_sessions_covering_the_instant_block_the_absorption(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,
) -> None:
    """Le témoin d'AMBIGUÏTÉ. Sans lui, la règle ne peut structurellement pas échouer.

    Deux sessions non-`agent` ouvertes couvrent l'instant de création. Aucune des
    deux n'a plus de titre que l'autre sur l'artefact : la règle REFUSE, et
    l'artefact reste chez la traçante — visible, pas perdu.

    Ce test doit rester VERT quelle que soit la clé retenue. S'il rougit un jour,
    c'est que la règle est devenue permissive et attribue au hasard.
    """
    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)

    with _derived_capture(True):
        mine = await repo.start(absorption_project, "task-w20-mine")
        rival = await repo.start(absorption_project, "task-w20-rival")
        assert rival.session.id != mine.session.id

        with _transport(uuid4().hex) as connection_a:
            tracer = await repo.auto_open(_Identity(absorption_project, connection_a))
            artifact = await _derive_one_artifact(learning_repo, absorption_project)
            assert await _ledger_owner(session_factory, artifact) == UUID(str(tracer))

        with _transport(uuid4().hex) as connection_b:
            await repo.auto_open(_Identity(absorption_project, connection_b))
            moved = await _absorb_from_the_current_connection(
                repo, UUID(str(mine.session.id)), "task-w20-mine"
            )

    assert moved == 0, "deux prétendantes valent une abstention, jamais un tirage au sort"
    assert await _ledger_owner(session_factory, artifact) == UUID(str(tracer))


async def test_a_rival_that_closed_before_the_instant_is_not_a_rival(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,
) -> None:
    """La rivale fermée AVANT l'instant ne couvre rien. ROUGE aujourd'hui.

    Deux raisons de rougir aujourd'hui, et c'est voulu : rien ne bouge (l'étage
    fenêtre n'existe pas) et `attribution_mode` n'existe pas encore. La seconde
    assertion est ce qui interdit une rétrogradation silencieuse : un jour où
    l'appariement redeviendrait une devinette, un total resterait vert.
    """
    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)

    with _derived_capture(True):
        rival = await repo.start(absorption_project, "task-w20-departed")
        await repo.abandon(rival.session.id, "task-w20-departed", "partie avant l'instant")
        mine = await repo.start(absorption_project, "task-w20-mine")

        with _transport(uuid4().hex) as connection_a:
            await repo.auto_open(_Identity(absorption_project, connection_a))
            artifact = await _derive_one_artifact(learning_repo, absorption_project)

        with _transport(uuid4().hex) as connection_b:
            await repo.auto_open(_Identity(absorption_project, connection_b))
            moved = await _absorb_from_the_current_connection(
                repo, UUID(str(mine.session.id)), "task-w20-mine"
            )

    assert moved == 1
    assert await _ledger_owner(session_factory, artifact) == UUID(str(mine.session.id))
    assert await _attribution_mode(session_factory, artifact) == "derived_window"


async def test_a_rival_open_at_the_instant_still_blocks_after_it_has_closed(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,
) -> None:
    """La couverture se juge à l'INSTANT, pas au moment de la commande.

    C'est le seul cas qui sépare deux lectures possibles de la règle : la rivale
    était ouverte quand l'artefact est né, puis s'est fermée avant que
    l'utilisateur ne commande. Juger « est-elle ouverte MAINTENANT » rendrait
    l'attribution dépendante de l'ordre de fermeture — deux sessions ambiguës,
    et c'est la dernière à fermer qui rafle tout. On juge donc la couverture.
    """
    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)

    with _derived_capture(True):
        mine = await repo.start(absorption_project, "task-w20-mine")
        rival = await repo.start(absorption_project, "task-w20-rival")

        with _transport(uuid4().hex) as connection_a:
            tracer = await repo.auto_open(_Identity(absorption_project, connection_a))
            artifact = await _derive_one_artifact(learning_repo, absorption_project)

        # La rivale part APRÈS la naissance de l'artefact : elle l'a couvert.
        await repo.abandon(rival.session.id, "task-w20-rival", "partie après l'instant")

        with _transport(uuid4().hex) as connection_b:
            await repo.auto_open(_Identity(absorption_project, connection_b))
            moved = await _absorb_from_the_current_connection(
                repo, UUID(str(mine.session.id)), "task-w20-mine"
            )

    assert moved == 0
    assert await _ledger_owner(session_factory, artifact) == UUID(str(tracer))


async def test_a_human_can_reclaim_what_the_rule_refused_to_attribute(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,
) -> None:
    """La contrepartie du fail-closed : refuser n'est pas perdre.

    La règle d'exclusivité s'abstient dès que deux sessions se chevauchent, et
    l'artefact reste chez le serveur. Sans ce chemin, ce serait une perte sèche :
    `capture()` levait « session artifact ownership could not be resolved » sur
    une ligne détenue par une traçante, donc plus personne ne pouvait l'en
    sortir. Un humain qui NOMME l'UUID doit toujours pouvoir reprendre.

    Le mode devient `explicit` : c'est le seul qui soit une preuve. La ligne
    cesse d'être une déduction du serveur au moment où quelqu'un la revendique.
    """
    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)

    with _derived_capture(True):
        mine = await repo.start(absorption_project, "task-w20-claimant")
        await repo.start(absorption_project, "task-w20-rival")

        with _transport(uuid4().hex) as connection_a:
            tracer = await repo.auto_open(_Identity(absorption_project, connection_a))
            artifact = await _derive_one_artifact(learning_repo, absorption_project)

        # La règle s'abstient — deux prétendantes — et c'est bien le cas qu'on
        # veut réparer à la main, pas un cas artificiel.
        with _transport(uuid4().hex) as connection_b:
            await repo.auto_open(_Identity(absorption_project, connection_b))
            refused = await _absorb_from_the_current_connection(
                repo, UUID(str(mine.session.id)), "task-w20-claimant"
            )
        assert refused == 0
        assert await _ledger_owner(session_factory, artifact) == UUID(str(tracer))

        result = await repo.capture(mine.session.id, "task-w20-claimant", [artifact])

    assert artifact in result.captured_knowledge_ids
    assert await _ledger_owner(session_factory, artifact) == UUID(str(mine.session.id))
    assert await _attribution_mode(session_factory, artifact) == "explicit"


async def test_capture_never_takes_what_another_human_already_holds(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,
) -> None:
    """La reprise est bornée à la NATURE du détenteur, jamais à l'UUID nommé.

    C'est ce qui empêche la greffe de devenir un passe-droit : nommer un UUID
    donne le droit de le reprendre AU SERVEUR, pas à un autre humain.
    L'exclusivité du ledger existe précisément pour ça, et un chemin de
    réparation qui l'enjamberait serait pire que le trou qu'il bouche.
    """
    from brain_v42.models.brain_session import BrainSessionCaptureConflictError

    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)

    with _derived_capture(True):
        holder = await repo.start(absorption_project, "task-w20-holder")
        other = await repo.start(absorption_project, "task-w20-other")

        with _transport(uuid4().hex) as connection:
            tracer = await repo.auto_open(_Identity(absorption_project, connection))
            artifact = await _derive_one_artifact(learning_repo, absorption_project)
            # La ligne part bien de chez le SERVEUR : sans traçante, ce test
            # n'exercerait que l'insertion normale et la frontière qu'il prétend
            # garder — reprendre au serveur, jamais à un humain — resterait
            # entière.
            assert await _ledger_owner(session_factory, artifact) == UUID(str(tracer))

        # Un humain la revendique en premier, par la REPRISE.
        await repo.capture(holder.session.id, "task-w20-holder", [artifact])
        assert await _attribution_mode(session_factory, artifact) == "explicit"

        with pytest.raises(BrainSessionCaptureConflictError):
            await repo.capture(other.session.id, "task-w20-other", [artifact])

    assert await _ledger_owner(session_factory, artifact) == UUID(str(holder.session.id))


async def _derive_one_decision(decision_repo: Any, project_key: str) -> UUID:
    """Un artefact dans la PREMIÈRE branche du `UNION ALL` (`decisions`).

    La branche compte : sans `ORDER BY`, un `LIMIT` posé sur l'union sert les
    premières branches d'abord. Mettre les artefacts contestés en tête est ce
    qui rend le témoin déterministe au lieu de probabiliste.
    """
    from brain_v42.models.decision import DecisionCreate

    created = await decision_repo.create(
        DecisionCreate(
            title=f"w20 contested {uuid4().hex[:8]}",
            description="Artefact du banc de bornage ; aucune portée opérationnelle.",
            reasoning="Occuper la première branche du UNION ALL.",
            project_key=project_key,
        )
    )
    return UUID(str(created.id))


async def test_the_bound_never_hides_an_eligible_artifact_behind_contested_ones(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A1 — filtrer, PUIS borner. Un témoin qui ne dépasse pas le plafond ne verrait rien.

    Le `UNION ALL` des six tables n'a pas d'`ORDER BY`. Borner AVANT le filtre de
    rivalité laissait Postgres rendre un lot arbitraire — ici les deux décisions
    contestées, servies par la première branche — et l'artefact légitime, en
    seconde branche, n'était jamais absorbé. En silence, et différemment d'un
    appel à l'autre.

    Le seuil réel n'est pas le plafond de 100 mais `100 - occupé`, qui rétrécit à
    mesure qu'une session accumule : le cas se produit en fin de session longue,
    exactement quand l'absorption sert le plus. On abaisse donc le plafond plutôt
    que de fabriquer cent artefacts — le défaut porte sur le RAPPORT entre
    `remaining` et le nombre d'éligibles, jamais sur la valeur 100.
    """
    import brain_v42.db.session_derived_capture as module
    from brain_v42.repositories.pg_decision import PgDecisionRepo

    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)
    decision_repo = PgDecisionRepo(session_factory)

    with _derived_capture(True):
        mine = await repo.start(absorption_project, "task-w20-bound")
        rival = await repo.start(absorption_project, "task-w20-bound-rival")

        with _transport(uuid4().hex) as connection_a:
            tracer = await repo.auto_open(_Identity(absorption_project, connection_a))
            # Deux CONTESTÉS, en première branche du UNION ALL, et autant que
            # `remaining` : sous l'ancien ordre ils remplissaient le lot à eux
            # seuls.
            contested = [await _derive_one_decision(decision_repo, absorption_project)]
            contested.append(await _derive_one_decision(decision_repo, absorption_project))

            # La rivale part APRÈS eux : elle les couvre, et elle ne couvrira pas
            # ce qui suit.
            await repo.abandon(rival.session.id, "task-w20-bound-rival", "partie")

            legitimate = await _derive_one_artifact(learning_repo, absorption_project)

        for item in (*contested, legitimate):
            assert await _ledger_owner(session_factory, item) == UUID(str(tracer))

        # Le plafond n'est abaissé qu'ICI. `derive_capture` lit le MÊME
        # plafond : le baisser plus tôt aurait refusé de déposer le troisième
        # artefact, et le témoin aurait décrit une fenêtre qui ne déborde pas —
        # exactement le test qui ne voit rien.
        monkeypatch.setattr(module, "_capture_cap", lambda: 2)

        with _transport(uuid4().hex) as connection_b:
            await repo.auto_open(_Identity(absorption_project, connection_b))
            moved = await _absorb_from_the_current_connection(
                repo, UUID(str(mine.session.id)), "task-w20-bound"
            )

    assert moved == 1, (
        "les artefacts contestés ont rempli le lot avant que le filtre ne "
        "s'applique : l'artefact légitime est resté invisible"
    )
    assert await _ledger_owner(session_factory, legitimate) == UUID(str(mine.session.id))
    assert await _attribution_mode(session_factory, legitimate) == "derived_window"
    # Les contestés n'ont pas bougé : borner après filtrer ne rend pas la règle
    # permissive.
    for item in contested:
        assert await _ledger_owner(session_factory, item) == UUID(str(tracer))


async def test_the_repository_reads_the_ledger_not_the_terminal_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,
) -> None:
    """`attributed_knowledge_ids` lit la SOURCE, pas la photo de fin.

    `brain_sessions.captured_knowledge_ids` n'est écrit qu'à la fermeture, et la
    contrainte `open` interdit qu'il soit rempli avant : mesuré le 2026-08-25,
    aucune session ouverte n'en a jamais porté un non vide. Une implémentation
    qui lirait le tableau rendrait donc TOUJOURS `[]` sur une session vivante —
    et c'est précisément le retard d'un appel que ce lot répare.

    Ce test existe parce que `start` en dépend : il est le seul des cinq à ne pas
    pouvoir absorber avant de matérialiser, et il relit ici ce qu'il vient de
    déplacer.
    """
    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)

    with _derived_capture(True):
        mine = await repo.start(absorption_project, "task-w20-read")
        with _transport(uuid4().hex) as connection:
            await repo.auto_open(_Identity(absorption_project, connection))
            artifact = await _derive_one_artifact(learning_repo, absorption_project)
            moved = await _absorb_from_the_current_connection(
                repo, UUID(str(mine.session.id)), "task-w20-read"
            )

    assert moved == 1
    ids = await repo.attributed_knowledge_ids(mine.session.id)
    assert [UUID(str(item)) for item in ids] == [artifact]

    # Et le tableau terminal est TOUJOURS vide : la session est ouverte.
    async with session_factory() as session:
        snapshot = (
            await session.execute(
                sa.select(brain_sessions.c.captured_knowledge_ids).where(
                    brain_sessions.c.id == mine.session.id
                )
            )
        ).scalar_one()
    assert list(snapshot or []) == [], (
        "si l'instantané se remplit sur une session ouverte, la contrainte `open` "
        "a changé et ce lot doit être relu en entier"
    )


async def test_a_mistargeted_absorption_moves_NOTHING_before_it_refuses(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,
) -> None:
    """La garde d'identité doit précéder la MUTATION, pas seulement la commande.

    `CLAUDE.md` est littéral : « le serveur refuse une paire incohérente AVANT
    TOUTE MUTATION. Cette garde protège du mauvais ciblage entre sessions
    parallèles. » L'absorption mutait sans aucun contrôle de propriété : un appel
    mal ciblé déplaçait le ledger d'une traçante dans la session d'autrui, PUIS
    se faisait refuser. Le ledger étant EXCLUSIF, ce déplacement est
    IRRÉVERSIBLE — et l'appelant, qui ne voit qu'un refus, n'a aucune raison de
    le soupçonner.

    Ce test vérifie l'ABSENCE DE MUTATION, pas la présence du refus : le refus
    existait déjà, c'est lui qui rendait le défaut invisible.
    """
    from brain_v42.models.brain_session import BrainSessionIdentityConflictError

    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)

    with _derived_capture(True):
        victim = await repo.start(absorption_project, "task-w20-victim")

        with _transport(uuid4().hex) as connection:
            tracer = await repo.auto_open(_Identity(absorption_project, connection))
            artifact = await _derive_one_artifact(learning_repo, absorption_project)
            assert await _ledger_owner(session_factory, artifact) == UUID(str(tracer))

            with pytest.raises(BrainSessionIdentityConflictError):
                await repo.absorb_derived_capture(
                    victim.session.id, connection, "task-w20-SOMEONE-ELSE"
                )

    assert await _ledger_owner(session_factory, artifact) == UUID(str(tracer)), (
        "le ledger a bougé avant que la paire incohérente ne soit refusée — et "
        "l'exclusivité du ledger rend ce déplacement irréversible"
    )
