"""Un document de référence ne recopie pas la tête du dépôt : elle est dérivée.

Recensement `f7d013eb`, 2026-08-22. Après la réparation du runbook DR
(`681dbe2e`), la question ouverte était : combien d'AUTRES documents portent une
valeur normative périmée ? Trois angles indépendants sur les 134 markdown
suivis — littéral de révision, nom d'actif de récupération, dénominateur de reçu
— ont rendu 70 documents, dont 14 hors des plans et specs datés. Un seul portait
un défaut, et il en portait TROIS :

* `docs/ARCHITECTURE.md:6` annonçait « Repository target: 040 » quand
  `docs/SCHEMA.md` et `docs/MCP_TOOLS.md` disaient tous deux 046 — trois
  documents, deux réponses ;
* `docs/ARCHITECTURE.md:665` annonçait « migrations 001 .. 039 defined » quand
  sa PROPRE ligne 4 disait « 001–046 defined » — le même document se
  contredisait, 661 lignes plus loin ;
* `docs/ARCHITECTURE.md:238` exigeait « a tested PostgreSQL restore at the exact
  deployed head (`037` on current production) » — faux depuis la 040, et
  contredit par sa propre ligne 4 qui dit que la tête déployée « is not asserted
  here — measure it ».

**Pourquoi ce n'est PAS la garde du runbook.** `test_runbook_normative_values_
have_one_source.py` interdit tout littéral hors d'une région déclarée. Cette
forme tient sur un runbook : petite surface normative, blocs de récit nettement
délimités. `ARCHITECTURE.md` a la forme INVERSE — du récit partout (« migration
033 installs the ledger »), trois assertions normatives. L'y appliquer
demanderait d'emballer la majorité du document dans des fenêtres de récit, ce
qui viderait la garde de son sens. Aucun autre document du recensement n'entre
donc dans cette garde-là ; la raison est dite plutôt qu'un second mécanisme
inventé pour la forme.

Ce module applique le MÊME invariant avec une autre source de vérité : au lieu
d'interdire le littéral, il exige que **tout littéral de portée dépôt soit égal
à la tête calculée depuis `alembic/versions/`**. Rien à rebaser à chaque
migration : la valeur attendue se mesure. C'est aussi ce qui le distingue des
épingles de chaîne existantes dans `test_documentation_contract.py`, qui
demandent une passe de bump à chaque bascule et dont ce dépôt a déjà noté le
coût.

Trois contrôles, et un quatrième contre l'angle mort :

1. toute **cible de dépôt** annoncée vaut la tête calculée ;
2. toute **plage de migrations** annoncée se termine sur cette tête ;
3. aucun littéral de révision ne cohabite avec une affirmation de tête
   **DÉPLOYÉE** sur une ligne **NON DATÉE** — une mesure datée reste licite,
   c'est la doctrine ratifiée le 2026-08-04 ;
4. toute ligne qui PARLE d'une cible de dépôt doit être reconnue par les motifs
   du contrôle 1, sinon le test échoue en demandant une formulation connue. Un
   recensement par motifs a un angle mort par construction ; celui-ci le rend
   bruyant au lieu de le laisser se lire « rien à signaler ».
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
VERSIONS = ROOT / "alembic" / "versions"

#: Le témoin négatif : `docs/ARCHITECTURE.md` avant le recensement `f7d013eb`,
#: gelé verbatim. Blob git `a748ff86ba918ae033b960723a9c98bb7cb6c7cf`, dernier
#: commit à l'avoir écrit `88a5c10`. Sans lui ce module est une opinion verte.
BEFORE_F7D013EB = Path(__file__).parent / "data" / "ARCHITECTURE.2026-08-22-before-f7d013eb.md"

#: Les documents de RÉFÉRENCE et d'OPÉRATION. Les plans, specs et ADR datés
#: décrivent l'état de leur jour et n'ont rien à faire ici — c'est la décision
#: de périmètre du recensement, pas un oubli.
GUARDED_DOCUMENTS = (
    ROOT / "README.md",
    ROOT / "docs" / "ARCHITECTURE.md",
    ROOT / "docs" / "SCHEMA.md",
    ROOT / "docs" / "MCP_TOOLS.md",
    ROOT / "docs" / "OPERATIONS.md",
    ROOT / "docs" / "PROJECTS_SYSTEM.md",
)

_REVISION = re.compile(r"^revision(?::\s*[^=]+)?\s*=\s*[\"']([^\"']+)", re.M)
_DOWN_REVISION = re.compile(r"^down_revision(?::\s*[^=]+)?\s*=\s*[\"']([^\"']+)", re.M)

#: « Repository target: 046 », « La cible du dépôt est 046 »,
#: « Migration 046 is the repository target », « La révision 046 est la tête du dépôt ».
_TARGET_PATTERNS = (
    re.compile(r"(?:Repository target:|La cible du dépôt est)\s*`?(0\d{2})`?"),
    re.compile(
        r"(?:Migration|La révision)\s+`?(0\d{2})`?\s+"
        r"(?:is the repository target|est la tête du dépôt)"
    ),
)

#: Le vocabulaire qui ANNONCE une cible de dépôt, quelle que soit la tournure.
#: Le contrôle 4 exige qu'une ligne le portant soit reconnue par un motif.
_TARGET_VOCABULARY = re.compile(r"repository target|cible du dépôt|tête du dépôt", re.I)

#: « migrations 001–046 defined », « migrations 001 .. 046 ».
_RANGE = re.compile(r"migrations?\s+001\s*(?:–|—|-|\.\.|to|à)\s*`?(0\d{2})`?")

#: Le vocabulaire d'une tête DÉPLOYÉE — celle qu'aucun document ne peut prouver.
_DEPLOYED = re.compile(
    r"current production|deployed head|deployed Alembic head|"
    r"production actuelle|head déployé|tête déployée",
    re.I,
)

_HEAD_LITERAL = re.compile(r"(?<![\w.])0\d{2}(?![\w.])")

#: Une mesure DATÉE d'une tête déployée reste licite : c'est ce que ce dépôt a
#: décidé le 2026-08-04. Ce qui est interdit, c'est la même phrase sans sa date.
_DATE = re.compile(
    r"20\d{2}-\d{2}-\d{2}"
    r"|\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September"
    r"|October|November|December)\s+20\d{2}"
    r"|\d{1,2}\s+(?:janvier|février|mars|avril|mai|juin|juillet|août|septembre"
    r"|octobre|novembre|décembre)\s+20\d{2}"
    r"|20\d{6}_\d{6}"
)


@dataclass(frozen=True)
class Claim:
    """Une affirmation de portée dépôt, avec la ligne qui la porte."""

    kind: str
    value: str
    line: int
    text: str

    def __str__(self) -> str:  # pragma: no cover - lisibilité d'échec seulement
        return f"L{self.line} [{self.kind}] {self.value} — {self.text.strip()[:120]}"


def repository_head() -> str:
    """Calcule la tête depuis `alembic/versions/`, sans la lire nulle part."""
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in VERSIONS.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        revision = _REVISION.search(text)
        parent = _DOWN_REVISION.search(text)
        if revision:
            revisions.add(revision.group(1))
        if parent:
            parents.add(parent.group(1))
    heads = sorted(revisions - parents)
    assert len(heads) == 1, f"la chaîne Alembic doit avoir une seule tête, vu {heads}"
    return heads[0]


def repository_claims(document: str) -> list[Claim]:
    """Rend les cibles de dépôt et plages de migrations annoncées."""
    claims: list[Claim] = []
    for index, line in enumerate(document.splitlines(), 1):
        for pattern in _TARGET_PATTERNS:
            claims.extend(
                Claim("repository target", match.group(1), index, line)
                for match in pattern.finditer(line)
            )
        claims.extend(
            Claim("migration range", match.group(1), index, line) for match in _RANGE.finditer(line)
        )
    return claims


def undated_deployed_head_claims(document: str) -> list[Claim]:
    """Rend les littéraux de révision collés à une tête déployée non datée."""
    claims: list[Claim] = []
    for index, line in enumerate(document.splitlines(), 1):
        if not _DEPLOYED.search(line) or _DATE.search(line):
            continue
        claims.extend(
            Claim("deployed head", literal, index, line)
            for literal in sorted(set(_HEAD_LITERAL.findall(line)))
        )
    return claims


def unrecognised_target_lines(document: str) -> list[Claim]:
    """Rend les lignes qui PARLENT d'une cible sans qu'un motif la reconnaisse."""
    return [
        Claim("unrecognised target phrasing", "", index, line)
        for index, line in enumerate(document.splitlines(), 1)
        if _TARGET_VOCABULARY.search(line)
        and not any(pattern.search(line) for pattern in _TARGET_PATTERNS)
    ]


def test_the_repository_head_is_derived_not_declared() -> None:
    """La valeur attendue se mesure : rien à rebaser à chaque migration."""
    head = repository_head()
    files = sorted(VERSIONS.glob("*.py"))

    assert re.fullmatch(r"0\d{2}", head), head
    assert len(files) == int(head), (
        f"{len(files)} fichiers de migration pour une tête `{head}` : la chaîne "
        "et le répertoire ne racontent plus la même histoire"
    )


@pytest.mark.parametrize("document_path", GUARDED_DOCUMENTS, ids=lambda path: path.name)
def test_every_repository_scoped_claim_equals_the_measured_head(document_path: Path) -> None:
    """Trois documents annonçaient la cible; deux disaient 046 et un 040."""
    head = repository_head()
    stale = [
        claim
        for claim in repository_claims(document_path.read_text(encoding="utf-8"))
        if claim.value != head
    ]

    assert not stale, (
        f"{document_path.name} annonce une valeur de portée dépôt différente de la tête "
        f"mesurée `{head}`.\n" + "\n".join(str(claim) for claim in stale)
    )


@pytest.mark.parametrize("document_path", GUARDED_DOCUMENTS, ids=lambda path: path.name)
def test_no_document_asserts_an_undated_deployed_head(document_path: Path) -> None:
    """Aucun document ne peut prouver la tête déployée; il la mesure ou la date."""
    claims = undated_deployed_head_claims(document_path.read_text(encoding="utf-8"))

    assert not claims, (
        f"{document_path.name} affirme une tête DÉPLOYÉE avec un littéral, sur une ligne "
        "sans date. Fais-la mesurer (`select version_num from alembic_version`), ou date "
        "la mesure — une mesure datée reste licite.\n" + "\n".join(str(claim) for claim in claims)
    )


@pytest.mark.parametrize("document_path", GUARDED_DOCUMENTS, ids=lambda path: path.name)
def test_a_new_phrasing_fails_loudly_instead_of_slipping_through(document_path: Path) -> None:
    """L'angle mort de motif rendu bruyant.

    Un recensement par motifs en a un par construction. Sans ce contrôle, une
    quatrième tournure de « cible du dépôt » passerait sans être vérifiée, et
    l'absence de signalement se lirait « rien à signaler ».
    """
    unrecognised = unrecognised_target_lines(document_path.read_text(encoding="utf-8"))

    assert not unrecognised, (
        f"{document_path.name} parle d'une cible de dépôt dans une tournure qu'aucun motif "
        "ne reconnaît : elle ne serait donc PAS vérifiée. Reformule, ou ajoute le motif à "
        "`_TARGET_PATTERNS`.\n" + "\n".join(str(claim) for claim in unrecognised)
    )


def test_the_gate_catches_the_document_it_was_written_for() -> None:
    """Le témoin négatif : `ARCHITECTURE.md` d'AVANT doit échouer, trois fois.

    Verbatim, pas reconstitué — c'est le blob `a748ff86`.
    """
    document = BEFORE_F7D013EB.read_text(encoding="utf-8")
    head = repository_head()

    stale = {
        (claim.kind, claim.value, claim.line)
        for claim in repository_claims(document)
        if claim.value != head
    }
    deployed = {(claim.value, claim.line) for claim in undated_deployed_head_claims(document)}

    assert ("repository target", "040", 6) in stale, "la cible périmée doit être vue"
    assert ("migration range", "039", 665) in stale, "l'auto-contradiction doit être vue"
    assert ("037", 238) in deployed, "la tête déployée non datée doit être vue"


def test_a_dated_measurement_of_the_deployed_head_stays_legitimate() -> None:
    """Le faux positif qui désarmerait la garde ne se produit pas.

    La ligne 4 d'`ARCHITECTURE.md` dit la tête déployée ET la date. Elle est
    licite, elle l'était déjà avant la correction, et elle doit le rester.
    """
    dated = "The deployed Alembic head is not asserted here — measure it. Last measurement: "
    dated += "`045` on 16 August 2026, right after the 044→045 cutover."

    assert undated_deployed_head_claims(dated) == []
    assert undated_deployed_head_claims(BEFORE_F7D013EB.read_text(encoding="utf-8")) != []


def test_the_same_claim_without_its_date_is_caught() -> None:
    """Contre-témoin : c'est la DATE qui autorise, pas le sujet de la phrase."""
    undated = (
        "A tested PostgreSQL restore at the exact deployed head (`037` on current production)."
    )

    assert [claim.value for claim in undated_deployed_head_claims(undated)] == ["037"]
