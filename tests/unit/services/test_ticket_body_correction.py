"""A ticket's BODY can be corrected, and one can see that it was.

Ticket `cabb7503`. The triage of the night of 2026-08-22 found six stale tickets,
THREE of which carried a wrong body — a dead premise written in stone. None could
be corrected: the five `brain_ticket_*` tools can create, reply, transition, list
and read, but none writes `tickets.body` after creation. Corrections could
therefore only live in the thread, where a hurried reader does not see them —
while the wrong body stays at the top of the view and keeps steering judgement.

CHOSEN PATH: extend `reply`, not add a tool. A body correction IS a thread
message that additionally rewrites the body; and the public MCP contract has no
free room left.

THREE PROPERTIES THESE TESTS PIN:

1. The justification is MANDATORY. Fixing a dead premise is legitimate;
   rewriting a request to make it retrospectively right is not. A body that
   changes without a word in the thread would be exactly that silent rewrite.
2. THE OLD BODY SURVIVES, inside the message. That is what distinguishes "the
   body was corrected" from "the body always said that": the thread carries the
   trace, and the replaced text is not lost.
3. AN IDENTICAL BODY IS REFUSED. Otherwise we would put into the thread the trace
   of a correction that corrected nothing — a false positive in the very memory
   used to judge.

NEGATIVE WITNESS, here and nowhere else: `test_a_plain_reply_never_touches_the_body`.
Without it, a path rewriting the body on EVERY reply would pass every test above —
we would have made the body correctable by making it unstable.
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
    """Keyword arguments of the last `add_message`, whatever the positional call."""
    assert repo.add_message.await_count == 1, (
        f"add_message appelé {repo.add_message.await_count} fois — attendu exactement 1"
    )
    return repo.add_message.await_args.kwargs


class TestAStaleBodyCanBeCorrected:
    async def test_the_ticket_body_is_replaced(self) -> None:
        """The ticket's case: a dead premise is replaced by the right text."""
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
        """One must be able to see THAT the body changed, and WHAT it said before."""
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
        """Rewriting a body without a word in the thread is rewriting history."""
        svc, repo = _svc()

        with pytest.raises(TicketError):
            await svc.reply(uuid4(), TO, "   ", corrects_body=FIXED_BODY)

        repo.add_message.assert_not_awaited()

    async def test_an_identical_body_is_refused(self) -> None:
        """A correction that corrects nothing would lay down a false trace."""
        svc, repo = _svc()

        with pytest.raises(TicketError):
            await svc.reply(uuid4(), TO, "rien n'a changé", corrects_body=STALE_BODY)

        repo.add_message.assert_not_awaited()

    async def test_a_third_party_cannot_correct_the_body(self) -> None:
        """The participation check applies to a correction too."""
        svc, repo = _svc()

        with pytest.raises(NotAllowedError):
            await svc.reply(
                uuid4(), "brain-v42", "je corrige chez les autres", corrects_body=FIXED_BODY
            )

        repo.add_message.assert_not_awaited()


class TestAPlainReplyIsUnchanged:
    """Negative witness — without it, rewriting on every reply would pass as success."""

    async def test_a_plain_reply_never_touches_the_body(self) -> None:
        svc, repo = _svc()

        await svc.reply(uuid4(), TO, "juste une remarque")

        kwargs = _add_message_kwargs(repo)
        assert kwargs.get("new_ticket_body") is None, (
            "une réponse ordinaire a réécrit le corps du ticket — le corps est "
            "devenu instable au lieu d'être corrigeable"
        )

    async def test_a_plain_reply_body_is_stored_verbatim(self) -> None:
        """No footer must be appended when nothing is corrected."""
        svc, repo = _svc()

        await svc.reply(uuid4(), TO, "juste une remarque")

        recorded = _add_message_kwargs(repo).get("body") or repo.add_message.await_args.args[2]
        assert recorded == "juste une remarque", (
            f"le corps du message a été altéré hors correction : {recorded!r}"
        )
