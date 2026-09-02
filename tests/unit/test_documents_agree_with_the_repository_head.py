"""A reference document does not copy the repository head: it is derived.

Census `f7d013eb`, 2026-08-22. After the DR runbook's repair (`681dbe2e`), the
open question was: how many OTHER documents carry a stale normative value? Three
independent angles over the 134 tracked markdown files — revision literal,
recovery asset name, receipt denominator — returned 70 documents, of which 14
outside the dated plans and specs. Only one carried a defect, and it carried
THREE:

* `docs/ARCHITECTURE.md:6` announced "Repository target: 040" while
  `docs/SCHEMA.md` and `docs/MCP_TOOLS.md` both said 046 — three documents, two
  answers;
* `docs/ARCHITECTURE.md:665` announced "migrations 001 .. 039 defined" while its
  OWN line 4 said "001–046 defined" — the same document contradicting itself, 661
  lines further on;
* `docs/ARCHITECTURE.md:238` required "a tested PostgreSQL restore at the exact
  deployed head (`037` on current production)" — false since 040, and contradicted
  by its own line 4 which says the deployed head "is not asserted here — measure
  it".

**Why this is NOT the runbook's guard.** `test_runbook_normative_values_
have_one_source.py` forbids any literal outside a declared region. That form holds
on a runbook: a small normative surface, clearly delimited narrative blocks.
`ARCHITECTURE.md` has the INVERSE shape — narrative everywhere ("migration 033
installs the ledger"), three normative assertions. Applying it there would require
wrapping most of the document in narrative windows, which would empty the guard of
its meaning. No other document from the census therefore enters that guard; the
reason is stated rather than a second mechanism invented for form's sake.

This module applies the SAME invariant with another source of truth: instead of
forbidding the literal, it requires that **every repository-scoped literal equal
the head computed from `alembic/versions/`**. Nothing to rebase at every
migration: the expected value is measured. That is also what distinguishes it from
the existing string pins in `test_documentation_contract.py`, which require a bump
pass at every cutover and whose cost this repository has already noted.

Three checks, and a fourth against the blind spot:

1. every announced **repository target** equals the computed head;
2. every announced **migration range** ends on that head;
3. no revision literal coexists with a **DEPLOYED** head assertion on an
   **UNDATED** line — a dated measurement stays licit, that is the doctrine
   ratified on 2026-08-04;
4. every line that SPEAKS of a repository target must be recognised by check 1's
   patterns, otherwise the test fails asking for a known phrasing. A
   pattern-based census has a blind spot by construction; this one makes it noisy
   instead of letting it read as "nothing to report".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
VERSIONS = ROOT / "alembic" / "versions"

#: The negative witness: `docs/ARCHITECTURE.md` before census `f7d013eb`, frozen
#: verbatim. Git blob `a748ff86ba918ae033b960723a9c98bb7cb6c7cf`, last commit to
#: have written it `88a5c10`. Without it this module is a green opinion.
BEFORE_F7D013EB = Path(__file__).parent / "data" / "ARCHITECTURE.2026-08-22-before-f7d013eb.md"

#: The REFERENCE and OPERATIONS documents. Dated plans, specs and ADRs describe
#: the state of their own day and have no business here — that is the census's
#: scope decision, not an oversight.
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

#: "Repository target: 046", "La cible du dépôt est 046",
#: "Migration 046 is the repository target", "La révision 046 est la tête du dépôt".
_TARGET_PATTERNS = (
    re.compile(r"(?:Repository target:|La cible du dépôt est)\s*`?(0\d{2})`?"),
    re.compile(
        r"(?:Migration|La révision)\s+`?(0\d{2})`?\s+"
        r"(?:is the repository target|est la tête du dépôt)"
    ),
)

#: The vocabulary that ANNOUNCES a repository target, whatever the phrasing. Check
#: 4 requires a line carrying it to be recognised by a pattern.
_TARGET_VOCABULARY = re.compile(r"repository target|cible du dépôt|tête du dépôt", re.I)

#: "migrations 001–046 defined", "migrations 001 .. 046".
_RANGE = re.compile(r"migrations?\s+001\s*(?:–|—|-|\.\.|to|à)\s*`?(0\d{2})`?")

#: The vocabulary of a DEPLOYED head — the one no document can prove.
_DEPLOYED = re.compile(
    r"current production|deployed head|deployed Alembic head|"
    r"production actuelle|head déployé|tête déployée",
    re.I,
)

_HEAD_LITERAL = re.compile(r"(?<![\w.])0\d{2}(?![\w.])")

#: A DATED measurement of a deployed head stays licit: that is what this
#: repository decided on 2026-08-04. What is forbidden is the same sentence without
#: its date.
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
    """A repository-scoped assertion, with the line that carries it."""

    kind: str
    value: str
    line: int
    text: str

    def __str__(self) -> str:  # pragma: no cover - failure readability only
        return f"L{self.line} [{self.kind}] {self.value} — {self.text.strip()[:120]}"


def repository_head() -> str:
    """Compute the head from `alembic/versions/`, without reading it anywhere."""
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
    """Return the announced repository targets and migration ranges."""
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
    """Return the revision literals attached to an undated deployed head."""
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
    """Return the lines that SPEAK of a target without a pattern recognising it."""
    return [
        Claim("unrecognised target phrasing", "", index, line)
        for index, line in enumerate(document.splitlines(), 1)
        if _TARGET_VOCABULARY.search(line)
        and not any(pattern.search(line) for pattern in _TARGET_PATTERNS)
    ]


def test_the_repository_head_is_derived_not_declared() -> None:
    """The expected value is measured: nothing to rebase at every migration."""
    head = repository_head()
    files = sorted(VERSIONS.glob("*.py"))

    assert re.fullmatch(r"0\d{2}", head), head
    assert len(files) == int(head), (
        f"{len(files)} fichiers de migration pour une tête `{head}` : la chaîne "
        "et le répertoire ne racontent plus la même histoire"
    )


@pytest.mark.parametrize("document_path", GUARDED_DOCUMENTS, ids=lambda path: path.name)
def test_every_repository_scoped_claim_equals_the_measured_head(document_path: Path) -> None:
    """Three documents announced the target; two said 046 and one said 040."""
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
    """No document can prove the deployed head; it measures it or dates it."""
    claims = undated_deployed_head_claims(document_path.read_text(encoding="utf-8"))

    assert not claims, (
        f"{document_path.name} affirme une tête DÉPLOYÉE avec un littéral, sur une ligne "
        "sans date. Fais-la mesurer (`select version_num from alembic_version`), ou date "
        "la mesure — une mesure datée reste licite.\n" + "\n".join(str(claim) for claim in claims)
    )


@pytest.mark.parametrize("document_path", GUARDED_DOCUMENTS, ids=lambda path: path.name)
def test_a_new_phrasing_fails_loudly_instead_of_slipping_through(document_path: Path) -> None:
    """The pattern blind spot made noisy.

    A pattern-based census has one by construction. Without this check, a fourth
    phrasing of "repository target" would pass unverified, and the absence of a
    report would read as "nothing to report".
    """
    unrecognised = unrecognised_target_lines(document_path.read_text(encoding="utf-8"))

    assert not unrecognised, (
        f"{document_path.name} parle d'une cible de dépôt dans une tournure qu'aucun motif "
        "ne reconnaît : elle ne serait donc PAS vérifiée. Reformule, ou ajoute le motif à "
        "`_TARGET_PATTERNS`.\n" + "\n".join(str(claim) for claim in unrecognised)
    )


def test_the_gate_catches_the_document_it_was_written_for() -> None:
    """The negative witness: the ARCHITECTURE.md from BEFORE must fail, three times.

    Verbatim, not reconstructed — it is blob `a748ff86`.
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
    """The false positive that would disarm the guard does not happen.

    `ARCHITECTURE.md`'s line 4 states the deployed head AND the date. It is licit,
    it already was before the fix, and it must stay so.
    """
    dated = "The deployed Alembic head is not asserted here — measure it. Last measurement: "
    dated += "`045` on 16 August 2026, right after the 044→045 cutover."

    assert undated_deployed_head_claims(dated) == []
    assert undated_deployed_head_claims(BEFORE_F7D013EB.read_text(encoding="utf-8")) != []


def test_the_same_claim_without_its_date_is_caught() -> None:
    """Counter-witness: it is the DATE that permits, not the sentence's subject."""
    undated = (
        "A tested PostgreSQL restore at the exact deployed head (`037` on current production)."
    )

    assert [claim.value for claim in undated_deployed_head_claims(undated)] == ["037"]
