"""Le runbook DR ne déclare sa cible courante qu'à UN seul endroit.

Le 2026-08-22, `docs/PLAN_INDEX_REPAIR_RUNBOOK.md` portait deux moitiés qui ne
parlaient pas de la même base. Ses sections datées annonçaient le head `046`, le
contrat `brain-v42-v5.sql` et un reçu `29/29`; sa PROCÉDURE — celle qu'un
opérateur suit en sinistre — disait encore head `037`, `brain-v42-v4.sql` et
`25/25`. Quatre extensions du contrat livrées le même jour avaient toutes
atterri dans les sections datées, aucune n'avait touché la procédure, et aucun
test n'avait rougi.

Ce que ce module épingle n'est PAS « deux valeurs différentes dans le même
fichier » : un document doit pouvoir parler de son passé, et une section datée
qui raconte le head `037` de juillet est légitime. La règle est plus étroite et
elle est décidable sans lire l'intention d'une phrase :

    hors d'une région déclarée, le fichier n'écrit AUCUN head, AUCUN nom d'actif
    de contrat et AUCUN dénominateur de reçu en littéral.

Deux familles de régions, et deux seulement :

* `dr-current` — la déclaration. Le SEUL endroit où une valeur courante
  s'écrit. Une seule valeur par nature, donc deux valeurs courantes
  divergentes sont impossibles par construction.
* les fenêtres historiques enregistrées dans `HISTORICAL_REGIONS` — le récit
  d'une bascule datée, libre de porter les nombres de son époque.

Tout le reste est de la prose normative, et elle ne cite plus de nombre : elle
renvoie à la déclaration ou fait mesurer. C'est la doctrine déjà appliquée le
2026-08-04 à README, ARCHITECTURE, MCP_TOOLS, SCHEMA, au runbook gateway et au
runbook graph (voir `test_documentation_contract.py`, « ces gates doivent
maintenant MESURER le head déployé au lieu d'affirmer 037 ») — ce fichier-ci
avait été sauté par cette passe.

Formes écartées, et pourquoi :

* **Détecter le mode impératif** (« restaurer » = instruction, « a restauré » =
  récit). Désarmé au premier faux positif : `restore head 037` est impératif et
  historique, `production measured 039` est narratif et normatif. On classerait
  l'intention d'une phrase avec une liste de verbes, et le prochain rédacteur
  la contournerait sans le savoir.
* **Marquer chaque valeur** avec une balise inline. Correct, mais ~50 balises
  dans ce seul fichier, illisible en prose, et un oubli est SILENCIEUX — il se
  lit comme « pas de valeur ici ».
* **Une source unique dont les deux moitiés dérivent** (génération). Ne résout
  pas le problème : une section historique ne DOIT PAS dériver de la valeur
  courante, sinon elle devient fausse. Il faudrait quand même marquer ce qui
  dérive et ce qui ne dérive pas, donc cette forme-ci plus une machinerie.

La porte est-elle contournable ? Oui, par une voie : envelopper une instruction
vivante dans une fenêtre historique. C'est pourquoi les noms de fenêtres sont
ÉNUMÉRÉS ICI et non découverts : en ajouter une échoue tant que ce fichier n'a
pas été édité et relu. C'est le prix voulu.

Portée : ce module ne garde qu'un fichier. `docs/ARCHITECTURE.md` porte le même
défaut à sa ligne 238 (« a tested PostgreSQL restore at the exact deployed head
(`037` on current production) », faux depuis la bascule 040) et n'est PAS couvert
ici — c'est une limite déclarée, pas un oubli. L'étendre coûte une entrée dans
`GUARDED_DOCUMENTS`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
RUNBOOK = ROOT / "docs" / "PLAN_INDEX_REPAIR_RUNBOOK.md"

# Le document AVANT la correction du ticket `681dbe2e`, gelé verbatim. Blob git
# `d3af73d62617a07db555dccc09e9249927a06c27`, dernier commit à l'avoir écrit
# `bab434b`. C'est le témoin négatif : sans lui, ce module est une opinion verte.
BEFORE_681DBE2E = (
    Path(__file__).parent / "data" / "PLAN_INDEX_REPAIR_RUNBOOK.2026-08-22-before-681dbe2e.md"
)

GUARDED_DOCUMENTS = (RUNBOOK,)

#: La région qui déclare les cibles courantes. Une seule, obligatoire.
DECLARATION_REGION = "dr-current"

#: Les fenêtres de récit. Énumérées à la main : c'est la seule dérogation
#: possible à la règle, et elle doit coûter une édition relue de ce fichier.
HISTORICAL_REGIONS = (
    "project-context-cas-039",
    "project-context-focus-updated-at-040",
)

_REGION_MARKER = re.compile(r"<!--\s*([a-z0-9-]+):(start|end)\s*-->")

#: Les trois natures gouvernées. Le head est cherché par sa FORME (`0` suivi de
#: deux chiffres, isolé), jamais par les mots qui l'entourent : un recensement
#: par mots-clés a un angle mort par construction, et l'angle mort d'une garde
#: se lit « rien à signaler ».
GOVERNED_VALUES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("alembic head", re.compile(r"(?<![\w.])0\d{2}(?![\w.])")),
    ("recovery contract asset", re.compile(r"brain-v42-v[\w.-]*\.(?:sql|json)")),
    ("contract receipt", re.compile(r"(?<![\w/.])\d{1,3}/\d{1,3}(?![\d/])")),
)

#: Constructions qui portent un nombre de la MÊME forme qu'une révision sans en
#: être une. Elles sont retirées de la ligne avant le balayage, et énumérées une
#: par une. L'autre remède — borner le motif du head à `0[0-5]\d` pour éviter
#: `umask 077` — rendrait la garde aveugle à la révision 060 sans que personne
#: ne l'apprenne : un plafond silencieux se lit « rien à signaler ».
_NOT_A_HEAD: tuple[re.Pattern[str], ...] = (re.compile(r"\bumask\s+0\d{2}\b"),)


@dataclass(frozen=True)
class Violation:
    """Une valeur gouvernée écrite hors de toute région déclarée."""

    kind: str
    value: str
    line: int
    text: str

    def __str__(self) -> str:  # pragma: no cover - lisibilité d'échec seulement
        return f"L{self.line} [{self.kind}] {self.value!r} — {self.text.strip()}"


def _region_of_each_line(document: str) -> tuple[list[str | None], set[str]]:
    """Associe à chaque ligne la région qui la contient, et rend les noms vus.

    Les marqueurs eux-mêmes appartiennent à leur région : sinon la ligne
    `<!-- dr-current:start -->` serait de la prose normative.
    """
    lines = document.splitlines()
    regions: list[str | None] = [None] * len(lines)
    seen: set[str] = set()
    current: str | None = None
    for index, line in enumerate(lines):
        marker = _REGION_MARKER.search(line)
        if marker is None:
            regions[index] = current
            continue
        name, edge = marker.group(1), marker.group(2)
        seen.add(name)
        if edge == "start":
            assert current is None, f"L{index + 1}: région `{name}` ouverte dans `{current}`"
            current = name
        else:
            assert current == name, f"L{index + 1}: `{name}:end` ferme `{current}`"
        regions[index] = name
        if edge == "end":
            current = None
    assert current is None, f"région `{current}` jamais fermée"
    return regions, seen


def find_undeclared_values(document: str) -> list[Violation]:
    """Rend toute valeur gouvernée écrite hors d'une région déclarée."""
    regions, _ = _region_of_each_line(document)
    violations: list[Violation] = []
    for index, line in enumerate(document.splitlines()):
        if regions[index] is not None:
            continue
        scanned = line
        for exemption in _NOT_A_HEAD:
            scanned = exemption.sub("", scanned)
        for kind, pattern in GOVERNED_VALUES:
            for match in pattern.finditer(scanned):
                violations.append(Violation(kind, match.group(0), index + 1, line))
    return violations


def declared_values(document: str, kind: str) -> list[str]:
    """Rend les valeurs d'une nature écrites DANS la déclaration."""
    regions, _ = _region_of_each_line(document)
    pattern = dict(GOVERNED_VALUES)[kind]
    values: list[str] = []
    for index, line in enumerate(document.splitlines()):
        if regions[index] != DECLARATION_REGION:
            continue
        values.extend(match.group(0) for match in pattern.finditer(line))
    return values


@pytest.mark.parametrize("document_path", GUARDED_DOCUMENTS, ids=lambda path: path.name)
def test_no_normative_value_lives_outside_the_declaration(document_path: Path) -> None:
    """La prose normative ne cite ni head, ni actif, ni dénominateur."""
    violations = find_undeclared_values(document_path.read_text(encoding="utf-8"))

    assert not violations, (
        f"{document_path.name} écrit une valeur gouvernée hors de toute région déclarée. "
        "Renvoie à la déclaration `dr-current`, fais mesurer la valeur, ou — si c'est "
        "vraiment du récit — place-la dans une fenêtre historique ENREGISTRÉE dans "
        "HISTORICAL_REGIONS.\n" + "\n".join(str(violation) for violation in violations)
    )


@pytest.mark.parametrize("document_path", GUARDED_DOCUMENTS, ids=lambda path: path.name)
def test_the_declaration_states_every_governed_nature(document_path: Path) -> None:
    """Une déclaration muette annulerait la garde en silence.

    Sans ce contrôle, envelopper le document entier dans une fenêtre historique
    et vider `dr-current` rendrait le module vert sur un fichier sans cible.
    """
    document = document_path.read_text(encoding="utf-8")
    _, seen = _region_of_each_line(document)

    assert DECLARATION_REGION in seen, f"{document_path.name} n'a pas de région `dr-current`"
    for kind, _pattern in GOVERNED_VALUES:
        assert declared_values(document, kind), (
            f"{document_path.name} : la déclaration ne dit rien de la nature `{kind}`"
        )


@pytest.mark.parametrize("document_path", GUARDED_DOCUMENTS, ids=lambda path: path.name)
def test_no_unregistered_region_may_appear(document_path: Path) -> None:
    """Ajouter une fenêtre est la seule échappatoire; elle doit coûter cher."""
    _, seen = _region_of_each_line(document_path.read_text(encoding="utf-8"))

    assert seen == {DECLARATION_REGION, *HISTORICAL_REGIONS}, (
        f"{document_path.name} porte des régions non enregistrées : "
        f"{sorted(seen - {DECLARATION_REGION, *HISTORICAL_REGIONS})}. "
        "Une fenêtre historique exempte son contenu de cette garde — l'enregistrer "
        "ici est le geste relu qui l'autorise."
    )


def test_the_gate_catches_the_document_it_was_written_for() -> None:
    """Le témoin négatif : le runbook d'AVANT la correction doit ÉCHOUER.

    Verbatim, pas reconstitué. Sans ce test le module ne prouve rien — il
    affirmerait qu'un document corrigé est corrigé.
    """
    violations = find_undeclared_values(BEFORE_681DBE2E.read_text(encoding="utf-8"))

    assert violations, "le document d'origine passait : la garde ne garde rien"
    by_kind = {
        kind: {violation.value for violation in violations if violation.kind == kind}
        for kind, _ in GOVERNED_VALUES
    }

    # Les trois natures que le ticket `681dbe2e` a nommées, chacune attrapée
    # dans la moitié PROCÉDURE du document, celle qu'un opérateur suit.
    assert "037" in by_kind["alembic head"]
    assert "brain-v42-v4.sql" in by_kind["recovery contract asset"]
    assert "25/25" in by_kind["contract receipt"]

    # Et les lignes exactes que le ticket citait.
    caught = {violation.line for violation in violations}
    assert {273, 282, 308, 321, 675, 676} <= caught


def test_a_dated_record_may_keep_the_numbers_of_its_day() -> None:
    """Le faux positif qui désarmerait la garde ne se produit pas.

    Un document DOIT pouvoir dire « la bascule de juillet a rendu 25/25 au head
    037 avec `brain-v42-v4.sql` ». Mêmes valeurs, dans une fenêtre enregistrée :
    aucun signalement.
    """
    record = "\n".join(
        (
            "# Runbook",
            "",
            "<!-- dr-current:start -->",
            "| Alembic head | `046` | 2026-08-22 |",
            "| Recovery contract | `brain-v42-v5.sql` | 2026-08-22 |",
            "| Contract receipt | `29/29` | 2026-08-22 |",
            "<!-- dr-current:end -->",
            "",
            "<!-- project-context-cas-039:start -->",
            "Executed on 2026-08-03: run `brain-v42-v4.sql` against production at",
            "head `037` and retain the live receipt 25/25.",
            "<!-- project-context-cas-039:end -->",
            "",
            "Measure the deployed head before relying on anything here.",
        )
    )

    assert find_undeclared_values(record) == []


def test_the_same_sentence_outside_a_window_is_caught() -> None:
    """Témoin de contrôle : c'est la FENÊTRE qui exempte, pas le texte.

    Mêmes phrases que le test précédent, hors fenêtre. Sans ce contre-témoin,
    un `find_undeclared_values` qui ne rendrait jamais rien passerait les deux.
    """
    live = "\n".join(
        (
            "# Runbook",
            "",
            "<!-- dr-current:start -->",
            "| Alembic head | `046` | 2026-08-22 |",
            "| Recovery contract | `brain-v42-v5.sql` | 2026-08-22 |",
            "| Contract receipt | `29/29` | 2026-08-22 |",
            "<!-- dr-current:end -->",
            "",
            "Run `brain-v42-v4.sql` against production at",
            "head `037` and retain the live receipt 25/25.",
            "Le point final est volontaire : un reçu en fin de phrase doit être vu.",
        )
    )

    caught = {(violation.kind, violation.value) for violation in find_undeclared_values(live)}

    assert caught == {
        ("alembic head", "037"),
        ("recovery contract asset", "brain-v42-v4.sql"),
        ("contract receipt", "25/25"),
    }


def test_ordinary_prose_is_not_mistaken_for_a_governed_value() -> None:
    """Les formes voisines qui NE sont pas des valeurs gouvernées."""
    ordinary = "\n".join(
        (
            "<!-- dr-current:start -->",
            "| head | `046` | contract | `brain-v42-v5.sql` | receipt | `29/29` |",
            "<!-- dr-current:end -->",
            "",
            'install -d -m 0700 "$EVIDENCE_DIR"   # modes octaux',
            'chmod 0600 "$BACKUP_RECEIPT"',
            "umask 077   # octal, pas une révision",
            "extension vector 0.8.2 sur 127.0.0.1:8765",
            "Mesuré le 2026-08-22, port 5433, 32 tables, 7 projets.",
            'test "$(stat -c %a -- "$SNAPSHOT")" = 600',
        )
    )

    assert find_undeclared_values(ordinary) == []


def _heading_section(document: str, heading: str) -> str:
    """Rend une section de niveau 2, marqueurs de région compris."""
    assert document.count(f"\n{heading}\n") == 1, f"`{heading}` n'est pas unique"
    body = document.split(f"\n{heading}\n", 1)[1]
    body = body.split("\n## ", 1)[0]
    return " ".join(body.split())


def _region_text(document: str, name: str) -> str:
    regions, _ = _region_of_each_line(document)
    lines = document.splitlines()
    return " ".join(
        " ".join(line.split())
        for line, region in zip(lines, regions, strict=True)
        if region == name
    )


@pytest.mark.parametrize("document_path", GUARDED_DOCUMENTS, ids=lambda path: path.name)
def test_the_caveat_lives_wherever_a_receipt_is_read(document_path: Path) -> None:
    """Un reçu ne se lit jamais sans ce qu'il ne prouve pas.

    C'est la phrase qu'une réécriture de section emporte sans le vouloir : tous
    les reçus de ce fichier viennent de la production VIVANTE et ne disent RIEN
    d'une restauration réelle. Elle est donc exigée aux DEUX endroits où un
    dénominateur se lit — la déclaration en tête, et la section datée qui
    raconte les cinq extensions du 2026-08-22 — et non pas à un seul, sans quoi
    déplacer le chiffre suffirait à la diluer.
    """
    document = document_path.read_text(encoding="utf-8")
    places = {
        "## Current targets": _heading_section(document, "## Current targets"),
        "project-context-focus-updated-at-040": _region_text(
            document, "project-context-focus-updated-at-040"
        ),
    }

    for name, text in places.items():
        assert "`pg_restore`d" in text, f"{name} ne dit pas d'où le reçu NE vient PAS"
        assert '"DR is proven"' in text, f"{name} laisse lire le reçu comme une preuve DR"
        assert "8eaefe36" in text, f"{name} ne nomme pas la porte P1 restée ouverte"
        assert "is true by construction" in text, (
            f"{name} ne dit pas que le contrôle écrit POUR un restore est celui "
            "qu'aucun reçu live ne peut exercer"
        )


@pytest.mark.parametrize("document_path", GUARDED_DOCUMENTS, ids=lambda path: path.name)
def test_the_declaration_comes_before_the_records(document_path: Path) -> None:
    """En sinistre, on lit de haut en bas et on s'arrête à la première réponse."""
    document = document_path.read_text(encoding="utf-8")
    declaration = document.index(f"<!-- {DECLARATION_REGION}:start -->")

    for historical in HISTORICAL_REGIONS:
        assert declaration < document.index(f"<!-- {historical}:start -->"), (
            f"`{historical}` précède la déclaration : un opérateur pressé lira les "
            "nombres d'une bascule passée avant la cible courante"
        )
