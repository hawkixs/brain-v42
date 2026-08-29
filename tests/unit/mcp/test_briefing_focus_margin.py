"""Le briefing dit combien il reste avant le mur du plafond de `next_focus`.

`brain_session_end` exige `next_focus`, plafonné à 10 000 caractères, et cette
valeur REMPLACE `current_focus` quand le compare-and-swap réussit. L'autre
écrivain de la même colonne, `brain_update_project_focus`, n'a **aucune borne** :
ni son argument (`current_focus: str`, un `str` nu), ni le modèle, ni le service,
ni la colonne (`text` illimité). Le plafond MCP est la SEULE borne de longueur du
chemin d'écriture — donc l'écrivain non borné peut mettre le projet dans un état
que l'écrivain borné ne sait pas représenter.

Ce n'est pas théorique. Mesuré le 2026-08-22, reconstruit depuis les instantanés
`brain_sessions.started_focus` : la révision 217 a porté **12 157 caractères
pendant seize heures**, vue par sept sessions ; sa fermeture a ramené le focus à
8 522 — **3 635 caractères, 30 % du focus, retirés en une écriture**.

Ce que cette ligne change : aujourd'hui on découvre le mur **au moment de
fermer, APRÈS le travail**, par un refus de validation. Une ligne lue en premier
le déplace avant.

Ce qu'elle NE change pas : aucun contrat, aucun comportement, aucune écriture.
C'est une observation, comme la mesure du schéma juste au-dessus.

La marge est comptée en CARACTÈRES, parce que c'est ce que `maxLength` de
Pydantic compte. Le même focus mesuré ce jour faisait 9 977 caractères pour
**10 285 octets** : une borne qui compterait des octets serait déjà franchie, et
afficher les deux nombres rendrait illisible la seule ligne qui doit être lue en
urgence. Qui rouvrira le sujet devra dire lequel des deux il compte.
"""

from __future__ import annotations

import pytest

from brain_v42.mcp.tools.session_lifecycle_tools import NEXT_FOCUS_MAX_LENGTH
from brain_v42.mcp.tools.session_tools import _section_technical_state
from tests.unit.mcp.test_session_tools import _no_activity_ks


def _line(focus_length: int) -> str:
    out = _section_technical_state("046", focus_tracked=True, focus_length=focus_length)
    lines = [line for line in out.splitlines() if line.startswith("- Focus :")]
    assert len(lines) == 1, out
    return lines[0]


def test_a_comfortable_margin_states_both_numbers_and_the_margin() -> None:
    line = _line(4200)

    assert "4200" in line
    assert str(NEXT_FOCUS_MAX_LENGTH) in line
    assert "marge 5800" in line


def test_the_measured_focus_of_the_day_shows_twenty_three_left() -> None:
    """9 977 : le focus réel du 2026-08-22, pressé contre le plafond."""
    assert "marge 23" in _line(9977)


def test_a_null_margin_is_LOUD() -> None:
    """LE TÉMOIN QUI COMPTE — première moitié.

    À marge nulle, un seul caractère de plus rend toute fermeture impossible.
    C'est le moment précis où quelqu'un a besoin de cette ligne, donc le moment
    où elle ne doit pas ressembler à une statistique de plus.
    """
    line = _line(NEXT_FOCUS_MAX_LENGTH)

    assert "marge 0" not in line, "à marge nulle, un nombre discret ne suffit pas"
    assert "NULLE" in line
    assert "refus" in line.lower(), "la CONSÉQUENCE doit être dite, pas seulement l'état"


@pytest.mark.parametrize("excess", [1, 240, 2157])
def test_an_exceeded_cap_is_LOUD_and_names_the_two_outcomes(excess: int) -> None:
    """LE TÉMOIN QUI COMPTE — seconde moitié.

    Au-dessus du plafond, la fermeture n'a que deux issues et l'opérateur doit
    les connaître AVANT d'écrire : compresser — donc perdre du texte choisi par
    lui, sans diff ni trace — ou être refusé. Une ligne qui dirait seulement
    « marge -240 » laisserait croire à un détail de comptage.
    """
    line = _line(NEXT_FOCUS_MAX_LENGTH + excess)

    assert "DÉPASSÉ" in line
    assert str(excess) in line
    assert "compress" in line.lower(), "la compression, et ce qu'elle coûte"
    assert "refus" in line.lower(), "l'autre issue"
    assert "marge -" not in line, (
        "une marge négative écrite nue se lirait comme un détail de comptage"
    )


def test_the_cap_is_not_a_parallel_literal() -> None:
    """La ligne et le validateur doivent citer la MÊME borne.

    Deux littéraux 10 000 dériveraient au premier changement, et le briefing
    annoncerait une marge que la validation ne reconnaîtrait pas — exactement le
    défaut que ce lot mesure, reproduit une couche plus haut.
    """
    from pydantic import TypeAdapter, ValidationError

    from brain_v42.mcp.tools.session_lifecycle_tools import FocusArg

    adapter = TypeAdapter(FocusArg)
    adapter.validate_python("x" * NEXT_FOCUS_MAX_LENGTH)
    with pytest.raises(ValidationError):
        adapter.validate_python("x" * (NEXT_FOCUS_MAX_LENGTH + 1))


def test_an_unmeasured_focus_invents_nothing() -> None:
    """Pas de focus, pas de ligne — « non mesuré » n'est pas « marge pleine »."""
    out = _section_technical_state("046", focus_tracked=True, focus_length=None)

    assert "- Focus :" not in out
    assert "- Schéma : 046" in out


def test_the_nominal_path_stays_green_and_quiet() -> None:
    """Le second témoin exigé : la ligne s'ajoute, elle ne remplace rien.

    Sans lui, on aurait rendu la marge visible en cassant ce qu'elle accompagne.
    """
    before = _section_technical_state("046", focus_tracked=True)
    after = _section_technical_state("046", focus_tracked=True, focus_length=4200)

    assert "### État technique (mesuré)" in after
    assert "- Schéma : 046" in after
    assert "- Focus écrit :" in before and "- Focus écrit :" in after
    assert before.splitlines()[:2] == after.splitlines()[:2], (
        "les lignes existantes gardent leur place et leur ordre"
    )


def test_the_line_reaches_the_REAL_briefing_not_just_its_helper() -> None:
    """Le témoin qui manquait aux trois lots verts et inertes du 21-22/08.

    Une ligne calculée par un helper testé, et jamais passée par le composeur,
    se lit exactement comme une ligne absente. Ce test suit le chemin que toute
    session emprunte : `_format_session_briefing` doit tirer la longueur du
    `current_focus` du contexte, sans que l'appelant ait à la fournir.
    """
    from types import SimpleNamespace

    from brain_v42.mcp.tools.session_tools import _format_session_briefing

    focus = "j" * 9977
    ctx = SimpleNamespace(
        project_key="brain-v42",
        current_focus=focus,
        focus_updated_at=None,
        blockers=[],
    )

    briefing = _format_session_briefing(
        ctx, [], [], _no_activity_ks(), None, [], [], schema_revision="046"
    )

    # 9 977 « j » ASCII : caractères et octets coïncident — le composeur
    # dérive les DEUX du contexte, sans que l'appelant ait à les fournir.
    assert "- Focus : 9977 / 10000 caractères (marge 23 ; 9977 octets)" in briefing
    assert briefing.index("- Focus :") < briefing.index("### Focus"), (
        "la mesure doit précéder la prose qu'elle borne"
    )


def test_the_LOUD_form_survives_the_composer_too() -> None:
    """Le golden de bout en bout n'exerce QUE le cas calme — 17 caractères.

    Signalé par l'orchestrateur en relisant le log CI, et c'est juste : le seul
    rendu bout-en-bout du briefing porte une marge de 9 983, donc la forme qui
    compte — celle qu'on lit en urgence — n'y passe jamais. Un golden ne se
    tord pas pour exercer des limites ; c'est ce test-ci qui les porte, mais sur
    le chemin COMPOSÉ, pas sur le helper seul.
    """
    from types import SimpleNamespace

    from brain_v42.mcp.tools.session_tools import _format_session_briefing

    ctx = SimpleNamespace(
        project_key="brain-v42",
        current_focus="d" * (NEXT_FOCUS_MAX_LENGTH + 240),
        focus_updated_at=None,
        blockers=[],
    )

    briefing = _format_session_briefing(
        ctx, [], [], _no_activity_ks(), None, [], [], schema_revision="046"
    )

    assert "DÉPASSÉ de 240" in briefing
    assert "compress" in briefing.lower()
    assert "refus" in briefing.lower()


class TestTheByteFigureOnTheNominalLineOnly:
    """Le sujet rouvert le 2026-08-29 — en disant lequel on compte, comme exigé.

    La borne compte des CARACTÈRES (le `maxLength` Pydantic) ; le même focus
    mesuré le 2026-08-22 faisait 9 977 caractères pour 10 285 OCTETS — toute
    borne qui compterait des octets serait déjà franchie. La ligne nominale
    porte désormais les deux nombres pour que personne ne confonde ; les
    branches BRUYANTES (marge nulle, dépassement) restent pures — la décision
    d'origine « la seule ligne à lire en urgence ne se dilue pas » tient
    exactement là où elle a été argumentée.
    """

    def _line_with_octets(self, focus_length: int, focus_octets: int) -> str:
        out = _section_technical_state(
            "048",
            focus_tracked=True,
            focus_length=focus_length,
            focus_octets=focus_octets,
        )
        lines = [line for line in out.splitlines() if line.startswith("- Focus :")]
        assert len(lines) == 1, out
        return lines[0]

    def test_the_nominal_line_says_both_and_names_both_units(self) -> None:
        line = self._line_with_octets(9977, 10285)

        assert "9977" in line
        assert "caractères" in line
        assert "10285 octets" in line

    def test_the_loud_lines_stay_pure(self) -> None:
        at_cap = self._line_with_octets(NEXT_FOCUS_MAX_LENGTH, NEXT_FOCUS_MAX_LENGTH + 300)
        exceeded = self._line_with_octets(NEXT_FOCUS_MAX_LENGTH + 40, NEXT_FOCUS_MAX_LENGTH + 400)

        assert "octets" not in at_cap
        assert "octets" not in exceeded

    def test_a_legacy_caller_without_octets_renders_unchanged(self) -> None:
        assert "octets" not in _line(4200)
