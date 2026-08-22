"""`is_human_actor` promettait fail-closed et livrait une liste NOIRE.

LE CONTRAT ÉTAIT DÉJÀ ÉCRIT, il n'était pas tenu. Le docstring de la fonction dit
« Fail-closed : un acteur inconnu ou non expansé n'est PAS humain » ; le
commentaire deux lignes plus haut avoue l'inverse — « un acteur absent de cette
liste et non sentinelle est traité comme humain ». Ce lot ALIGNE le code sur sa
propre promesse ; ce n'est pas un changement d'intention.

CE QUI EST PASSÉ AU TRAVERS. `_SYSTEM_ACTOR_PREFIXES` énumérait `dream-codex-`
et lui seul, alors que **trois** rails dream sont câblés dans `dream.sh` et
émettent chacun leur `X-Brain-Agent` :

    codex_runner.py:125    dream-codex-{phase}     reconnu
    claude_runner.py:105   dream-claude-{phase}    classé HUMAIN
    agy_runner.py:116      dream-agy-{phase}       classé HUMAIN

`agy` est le rail de REPLI documenté (`reorg_events.py` : « agy is the
fallback »). Deux rails sur trois comptaient donc leurs relectures nocturnes
comme des lectures humaines.

MESURÉ le 2026-08-22, et c'est ce qui a fait remonter le défaut : sur 44
désarchivages en 18 jours, 10 portaient une « lecture humaine » horodatée entre
**04:03 et 05:21 UTC**, chacune une à deux minutes avant le désarchivage qu'elle
causait. Un humain ne lit pas le corpus à 4 h du matin, six nuits de suite,
90 secondes avant chaque cycle du flusher.

LA FORME DU CORRECTIF, ET POURQUOI CE N'EST PAS UNE LISTE BLANCHE D'HUMAINS. On
ne peut pas énumérer les humains : leur acteur est le basename du projet
(`red-lab`, `brain_v42`, …), arbitraire par construction. Exiger qu'un humain se
déclare casserait le cas légitime — c'est le piège classique « fermer le trou en
cassant le cas qu'on protège ». Ce qui EST énumérable, c'est la FAMILLE système :
les trois rails partagent le préfixe `dream-`, et tout rail futur le partagera
aussi puisque c'est le gabarit du runner. La garde passe donc de l'énumération
d'UN rail à la reconnaissance de la FAMILLE.

ET C'EST LE TEST STRUCTUREL QUI PORTE LA GARANTIE, pas le préfixe. Un quatrième
rail `dream-<nom>-{phase}` sera classé machine par le préfixe, et
`test_every_dream_rail_header_is_machine` le VÉRIFIE en relisant les runners :
si quelqu'un ajoute un runner qui émet autre chose, le test rougit. C'est le
témoin qui aurait attrapé le défaut d'origine.

CE QUE CE LOT NE FAIT PAS. Il n'a **aucun effet rétroactif** : `access_log` est
drainé à chaque flush (mesuré à 0 ligne le 2026-08-22), donc l'historique des
acteurs n'existe plus et `access_count_human` garde sa contamination. Le
correctif arrête l'hémorragie, il ne répare pas le passé.
"""

from __future__ import annotations

import re
from pathlib import Path

from brain_v42.provenance import UNEXPANDED_ACTOR, UNKNOWN_ACTOR, is_human_actor

_DREAM_DIR = Path(__file__).resolve().parents[2] / "scripts" / "dream"
_HEADER_LITERAL = re.compile(r'f"(dream-[a-z0-9]+-)\{phase\}"')


def _emitted_prefixes() -> set[str]:
    """Les préfixes d'acteur que les runners dream émettent RÉELLEMENT."""
    found: set[str] = set()
    for path in sorted(_DREAM_DIR.glob("*.py")):
        found.update(_HEADER_LITERAL.findall(path.read_text(encoding="utf-8")))
    return found


class TestEveryDreamRailIsMachine:
    def test_the_three_wired_rails_are_machine(self) -> None:
        """Nommément, pour qu'une régression sur l'un d'eux se lise."""
        assert is_human_actor("dream-codex-synth") is False
        assert is_human_actor("dream-claude-promote") is False
        assert is_human_actor("dream-agy-reorg") is False

    def test_every_dream_rail_header_is_machine(self) -> None:
        """LA garde : relit les runners et exige que CHAQUE en-tête émis soit machine.

        C'est ce test — pas le préfixe — qui empêche un quatrième rail de repasser
        au travers. Il aurait rougi le jour où `claude_runner` a été écrit.
        """
        prefixes = _emitted_prefixes()
        assert len(prefixes) >= 3, f"motif cassé, rails trouvés : {prefixes}"
        for prefix in sorted(prefixes):
            actor = f"{prefix}somephase"
            assert is_human_actor(actor) is False, (
                f"le rail {prefix!r} est émis par un runner dream mais compté HUMAIN"
            )

    def test_an_unknown_future_rail_is_machine(self) -> None:
        """Fail-closed sur la famille : un rail qui n'existe pas encore."""
        assert is_human_actor("dream-mistral-extract") is False
        assert is_human_actor("dream-whatever-42") is False


class TestTheLegitimateCaseSurvives:
    """TÉMOIN NÉGATIF. Sans lui, tout classer machine rendrait la classe ci-dessus verte."""

    def test_a_real_human_session_stays_human(self) -> None:
        assert is_human_actor("red-lab") is True
        assert is_human_actor("brain_v42") is True
        assert is_human_actor("brain-v42") is True

    def test_a_project_name_that_merely_mentions_dream_stays_human(self) -> None:
        """La garde porte sur le PRÉFIXE, pas sur la présence du mot.

        Un projet nommé `daydream` ou `dreamhouse` est un humain. Une garde par
        sous-chaîne le perdrait, et ce serait la même faute dans l'autre sens.
        """
        assert is_human_actor("daydream") is True
        assert is_human_actor("dreamhouse") is True
        assert is_human_actor("my-dream-project") is True

    def test_the_sentinels_stay_non_human(self) -> None:
        assert is_human_actor(UNKNOWN_ACTOR) is False
        assert is_human_actor(UNEXPANDED_ACTOR) is False
        assert is_human_actor("") is False
        assert is_human_actor(None) is False


class TestThisAloneClosesTheQ1Guard:
    """Corriger `is_human_actor` SUFFIT-il à fermer les trois rails ? Prouvé, pas affirmé.

    La garde Q1 (`b96acad`) lit `stats["count_human"]`, que
    `PgAccessLogRepo.aggregate_in_session` construit en appelant `is_human_actor`
    sur `access_log.actor`. La chaîne est donc :

        actor -> is_human_actor -> count_human -> unarchive_is_robot_only

    Ce test rejoue cette composition exacte pour les trois rails. Si elle tient,
    aucun complément de garde n'est nécessaire — et c'est la question que le
    mandat pose explicitement.
    """

    @staticmethod
    def _count_human(actors: list[str]) -> int:
        """Reproduit le pliage par acteur de `aggregate_in_session` (une lecture chacun)."""
        return sum(1 for a in actors if is_human_actor(a))

    def test_a_night_from_any_rail_leaves_the_guard_shut(self) -> None:
        from brain_v42.services.decay_flusher import unarchive_is_robot_only

        for rail in sorted(_emitted_prefixes()):
            actors = [f"{rail}reorg", f"{rail}synth", f"{rail}promote"]
            human = self._count_human(actors)
            assert human == 0, f"{rail!r} alimente encore count_human ({human})"
            assert (
                unarchive_is_robot_only(
                    old_status="archived", new_status="fresh", human_reads=human
                )
                is True
            ), f"la garde Q1 laisserait {rail!r} désarchiver"

    def test_a_real_human_in_the_same_batch_still_opens_it(self) -> None:
        """TÉMOIN NÉGATIF de la composition.

        Sans lui, un `is_human_actor` qui rendrait False pour TOUT laisserait le
        test précédent vert tout en supprimant le droit humain de désarchiver.
        """
        from brain_v42.services.decay_flusher import unarchive_is_robot_only

        actors = ["dream-agy-reorg", "dream-codex-synth", "red-lab"]
        human = self._count_human(actors)
        assert human == 1
        assert (
            unarchive_is_robot_only(old_status="archived", new_status="fresh", human_reads=human)
            is False
        )
