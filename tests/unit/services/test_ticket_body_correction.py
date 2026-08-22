"""Le CORPS d'un ticket peut être corrigé, et on voit qu'il l'a été.

Ticket `cabb7503`. Le triage de la nuit du 2026-08-22 a trouvé six tickets
périmés dont TROIS portaient un corps faux — une prémisse morte écrite en dur.
Aucun n'a pu être corrigé : les cinq tools `brain_ticket_*` savent créer,
répondre, transitionner, lister et lire, mais aucun n'écrit `tickets.body`
après la création. Les corrections n'ont donc pu vivre qu'au fil, là où un
lecteur pressé ne les voit pas — pendant que le corps faux, lui, reste en tête
de la vue et continue d'orienter le jugement.

CHEMIN RETENU : étendre `reply`, pas ajouter un tool. Une correction de corps
EST un message de fil qui, en plus, réécrit le corps ; et le contrat MCP public
n'a plus de marge gratuite.

TROIS PROPRIÉTÉS QUE CES TESTS ÉPINGLENT :

1. La justification est OBLIGATOIRE. Corriger une prémisse morte est légitime ;
   réécrire une demande pour la rendre rétrospectivement juste ne l'est pas.
   Un corps qui change sans un mot au fil serait exactement cette réécriture
   silencieuse.
2. L'ANCIEN CORPS SURVIT, dans le message. C'est ce qui distingue « le corps a
   été corrigé » de « le corps a toujours dit ça » : le fil porte la trace, et
   le texte remplacé n'est pas perdu.
3. UN CORPS IDENTIQUE EST REFUSÉ. Sinon on poserait au fil la trace d'une
   correction qui n'a rien corrigé — un faux positif dans la mémoire même qui
   sert à juger.

TÉMOIN NÉGATIF, ici et pas ailleurs : `test_a_plain_reply_never_touches_the_body`.
Sans lui, un chemin qui réécrirait le corps à CHAQUE réponse passerait tous les
tests ci-dessus — on aurait rendu le corps corrigeable en le rendant instable.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.models.ticket import Ticket, TicketKind, TicketStatus
from brain_v42.services.ticket_service import NotAllowedError, TicketError, TicketService

FROM, TO = "red-shrik", "red-data"
STALE_BODY = "Le rail GitLab doit être réparé avant la bascule."
FIXED_BODY = "Le rail GitLab est retiré (décision 218028c7) : ce ticket ne porte plus que GitHub."


def _ticket(body: str = STALE_BODY) -> Ticket:
    return Ticket(
        kind=TicketKind.REQUEST,
        title="Réparer le rail",
        body=body,
        from_project=FROM,
        to_project=TO,
        status=TicketStatus.OPEN,
    )


def _svc(ticket: Ticket | None = None) -> tuple[TicketService, MagicMock]:
    repo = MagicMock()
    repo.get_by_id = AsyncMock(return_value=ticket if ticket is not None else _ticket())
    repo.add_message = AsyncMock()
    ctx_repo = MagicMock()
    ctx_repo.get_by_project_key = AsyncMock(return_value=MagicMock())
    return TicketService(repo, ctx_repo), repo


def _add_message_kwargs(repo: MagicMock) -> dict:
    """Arguments nommés du dernier `add_message`, quel que soit l'appel positionnel."""
    assert repo.add_message.await_count == 1, (
        f"add_message appelé {repo.add_message.await_count} fois — attendu exactement 1"
    )
    return repo.add_message.await_args.kwargs


class TestAStaleBodyCanBeCorrected:
    async def test_the_ticket_body_is_replaced(self) -> None:
        """Le cas du ticket : une prémisse morte est remplacée par le texte juste."""
        svc, repo = _svc()

        await svc.reply(
            uuid4(), TO, "La décision 218028c7 a retiré GitLab.", corrects_body=FIXED_BODY
        )

        kwargs = _add_message_kwargs(repo)
        assert kwargs.get("new_ticket_body") == FIXED_BODY, (
            "le corps corrigé n'est pas passé au repository — la correction "
            f"n'atteint jamais `tickets.body` (reçu {kwargs.get('new_ticket_body')!r})"
        )

    async def test_the_thread_keeps_the_replaced_text(self) -> None:
        """On doit pouvoir voir QUE le corps a changé, et CE QU'il disait avant."""
        svc, repo = _svc()

        await svc.reply(
            uuid4(), TO, "La décision 218028c7 a retiré GitLab.", corrects_body=FIXED_BODY
        )

        recorded = _add_message_kwargs(repo).get("body") or repo.add_message.await_args.args[2]
        assert "La décision 218028c7 a retiré GitLab." in recorded, (
            "la justification de l'auteur doit rester lisible telle quelle dans le fil"
        )
        assert STALE_BODY in recorded, (
            "le corps REMPLACÉ doit survivre dans le fil : sans lui, on ne peut plus "
            "distinguer « corrigé » de « a toujours dit ça », et le texte est perdu"
        )


class TestASilentRewriteIsRefused:
    async def test_a_correction_without_a_justification_is_refused(self) -> None:
        """Réécrire un corps sans un mot au fil, c'est réécrire l'histoire."""
        svc, repo = _svc()

        with pytest.raises(TicketError):
            await svc.reply(uuid4(), TO, "   ", corrects_body=FIXED_BODY)

        repo.add_message.assert_not_awaited()

    async def test_an_identical_body_is_refused(self) -> None:
        """Une correction qui ne corrige rien poserait une fausse trace."""
        svc, repo = _svc()

        with pytest.raises(TicketError):
            await svc.reply(uuid4(), TO, "rien n'a changé", corrects_body=STALE_BODY)

        repo.add_message.assert_not_awaited()

    async def test_a_third_party_cannot_correct_the_body(self) -> None:
        """Le contrôle de participation vaut aussi pour une correction."""
        svc, repo = _svc()

        with pytest.raises(NotAllowedError):
            await svc.reply(
                uuid4(), "brain-v42", "je corrige chez les autres", corrects_body=FIXED_BODY
            )

        repo.add_message.assert_not_awaited()


class TestAPlainReplyIsUnchanged:
    """Témoin négatif — sans lui, réécrire à chaque réponse passerait pour un succès."""

    async def test_a_plain_reply_never_touches_the_body(self) -> None:
        svc, repo = _svc()

        await svc.reply(uuid4(), TO, "juste une remarque")

        kwargs = _add_message_kwargs(repo)
        assert kwargs.get("new_ticket_body") is None, (
            "une réponse ordinaire a réécrit le corps du ticket — le corps est "
            "devenu instable au lieu d'être corrigeable"
        )

    async def test_a_plain_reply_body_is_stored_verbatim(self) -> None:
        """Aucun pied de page ne doit s'ajouter quand rien n'est corrigé."""
        svc, repo = _svc()

        await svc.reply(uuid4(), TO, "juste une remarque")

        recorded = _add_message_kwargs(repo).get("body") or repo.add_message.await_args.args[2]
        assert recorded == "juste une remarque", (
            f"le corps du message a été altéré hors correction : {recorded!r}"
        )
