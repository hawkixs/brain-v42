"""The DR runbook declares its current target in ONE place only.

On 2026-08-22, `docs/PLAN_INDEX_REPAIR_RUNBOOK.md` carried two halves that were
not talking about the same database. Its dated sections announced head `046`, the
contract `brain-v42-v5.sql` and a `29/29` receipt; its PROCEDURE — the one an
operator follows in a disaster — still said head `037`, `brain-v42-v4.sql` and
`25/25`. Four contract extensions shipped the same day had all landed in the dated
sections, none had touched the procedure, and no test had reddened.

What this module pins is NOT "two different values in the same file": a document
must be able to speak of its past, and a dated section telling the story of July's
head `037` is legitimate. The rule is narrower and it is decidable without reading
a sentence's intent:

    outside a declared region, the file writes NO head, NO contract asset name and
    NO receipt denominator as a literal.

Two families of regions, and two only:

* `dr-current` — the declaration. The ONLY place a current value is written. One
  value per kind, so two divergent current values are impossible by construction.
* the historical windows registered in `HISTORICAL_REGIONS` — the story of a dated
  cutover, free to carry the numbers of its own time.

Everything else is normative prose, and it no longer cites a number: it refers to
the declaration or makes you measure. This is the doctrine already applied on
2026-08-04 to README, ARCHITECTURE, MCP_TOOLS, SCHEMA, the gateway runbook and the
graph runbook (see `test_documentation_contract.py`, "these gates must now MEASURE
the deployed head instead of asserting 037") — this file had been skipped by that
pass.

Forms discarded, and why:

* **Detecting the imperative mood** ("restore" = instruction, "restored" = story).
  Disarmed at the first false positive: `restore head 037` is imperative and
  historical, `production measured 039` is narrative and normative. We would be
  classifying a sentence's intent with a list of verbs, and the next writer would
  bypass it without knowing.
* **Marking each value** with an inline tag. Correct, but ~50 tags in this file
  alone, unreadable in prose, and an omission is SILENT — it reads as "no value
  here".
* **A single source both halves derive from** (generation). Does not solve the
  problem: a historical section must NOT derive from the current value, or it
  becomes false. What derives and what does not would still have to be marked,
  hence this form plus machinery.

Is the gate bypassable? Yes, by one route: wrapping a live instruction in a
historical window. That is why the window names are ENUMERATED HERE and not
discovered: adding one fails until this file has been edited and reviewed. That is
the intended price.

Scope: this module guards one file only. `docs/ARCHITECTURE.md` carries the same
defect at its line 238 ("a tested PostgreSQL restore at the exact deployed head
(`037` on current production)", false since the 040 cutover) and is NOT covered
here — that is a declared limit, not an oversight. Extending it costs one entry in
`GUARDED_DOCUMENTS`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
RUNBOOK = ROOT / "docs" / "PLAN_INDEX_REPAIR_RUNBOOK.md"

# The document BEFORE ticket `681dbe2e`'s fix, frozen verbatim. Git blob
# `d3af73d62617a07db555dccc09e9249927a06c27`, last commit to have written it
# `bab434b`. This is the negative witness: without it, this module is a green
# opinion.
BEFORE_681DBE2E = (
    Path(__file__).parent / "data" / "PLAN_INDEX_REPAIR_RUNBOOK.2026-08-22-before-681dbe2e.md"
)

GUARDED_DOCUMENTS = (RUNBOOK,)

#: The region that declares the current targets. One only, mandatory.
DECLARATION_REGION = "dr-current"

#: The narrative windows. Enumerated by hand: this is the rule's only possible
#: derogation, and it must cost a reviewed edit of this file.
HISTORICAL_REGIONS = (
    "project-context-cas-039",
    "project-context-focus-updated-at-040",
)

_REGION_MARKER = re.compile(r"<!--\s*([a-z0-9-]+):(start|end)\s*-->")

#: The three governed kinds. The head is looked for by its SHAPE (`0` followed by
#: two digits, isolated), never by the words around it: a keyword-based census has
#: a blind spot by construction, and a guard's blind spot reads as "nothing to
#: report".
GOVERNED_VALUES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("alembic head", re.compile(r"(?<![\w.])0\d{2}(?![\w.])")),
    ("recovery contract asset", re.compile(r"brain-v42-v[\w.-]*\.(?:sql|json)")),
    ("contract receipt", re.compile(r"(?<![\w/.])\d{1,3}/\d{1,3}(?![\d/])")),
)

#: Constructions that carry a number of the SAME shape as a revision without being
#: one. They are removed from the line before the sweep, and enumerated one by one.
#: The other remedy — bounding the head pattern to `0[0-5]\d` to avoid `umask 077`
#: — would make the guard blind to revision 060 without anyone learning it: a
#: silent ceiling reads as "nothing to report".
_NOT_A_HEAD: tuple[re.Pattern[str], ...] = (re.compile(r"\bumask\s+0\d{2}\b"),)


@dataclass(frozen=True)
class Violation:
    """A governed value written outside any declared region."""

    kind: str
    value: str
    line: int
    text: str

    def __str__(self) -> str:  # pragma: no cover - failure readability only
        return f"L{self.line} [{self.kind}] {self.value!r} — {self.text.strip()}"


def _region_of_each_line(document: str) -> tuple[list[str | None], set[str]]:
    """Map each line to the region containing it, and return the names seen.

    The markers themselves belong to their region: otherwise the line
    `<!-- dr-current:start -->` would be normative prose.
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
    """Return every governed value written outside a declared region."""
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
    """Return the values of a kind written INSIDE the declaration."""
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
    """Normative prose cites neither head, nor asset, nor denominator."""
    violations = find_undeclared_values(document_path.read_text(encoding="utf-8"))

    assert not violations, (
        f"{document_path.name} écrit une valeur gouvernée hors de toute région déclarée. "
        "Renvoie à la déclaration `dr-current`, fais mesurer la valeur, ou — si c'est "
        "vraiment du récit — place-la dans une fenêtre historique ENREGISTRÉE dans "
        "HISTORICAL_REGIONS.\n" + "\n".join(str(violation) for violation in violations)
    )


@pytest.mark.parametrize("document_path", GUARDED_DOCUMENTS, ids=lambda path: path.name)
def test_the_declaration_states_every_governed_nature(document_path: Path) -> None:
    """A mute declaration would cancel the guard in silence.

    Without this check, wrapping the whole document in a historical window and
    emptying `dr-current` would make the module green on a file with no target.
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
    """Adding a window is the only escape hatch; it must be expensive."""
    _, seen = _region_of_each_line(document_path.read_text(encoding="utf-8"))

    assert seen == {DECLARATION_REGION, *HISTORICAL_REGIONS}, (
        f"{document_path.name} porte des régions non enregistrées : "
        f"{sorted(seen - {DECLARATION_REGION, *HISTORICAL_REGIONS})}. "
        "Une fenêtre historique exempte son contenu de cette garde — l'enregistrer "
        "ici est le geste relu qui l'autorise."
    )


def test_the_gate_catches_the_document_it_was_written_for() -> None:
    """The negative witness: the runbook from BEFORE the fix must FAIL.

    Verbatim, not reconstructed. Without this test the module proves nothing — it
    would assert that a corrected document is corrected.
    """
    violations = find_undeclared_values(BEFORE_681DBE2E.read_text(encoding="utf-8"))

    assert violations, "le document d'origine passait : la garde ne garde rien"
    by_kind = {
        kind: {violation.value for violation in violations if violation.kind == kind}
        for kind, _ in GOVERNED_VALUES
    }

    # The three kinds ticket `681dbe2e` named, each caught in the document's
    # PROCEDURE half, the one an operator follows.
    assert "037" in by_kind["alembic head"]
    assert "brain-v42-v4.sql" in by_kind["recovery contract asset"]
    assert "25/25" in by_kind["contract receipt"]

    # And the exact lines the ticket cited.
    caught = {violation.line for violation in violations}
    assert {273, 282, 308, 321, 675, 676} <= caught


def test_a_dated_record_may_keep_the_numbers_of_its_day() -> None:
    """The false positive that would disarm the guard does not happen.

    A document MUST be able to say "July's cutover returned 25/25 at head 037 with
    `brain-v42-v4.sql`". Same values, inside a registered window: no report.
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
    """Control witness: it is the WINDOW that exempts, not the text.

    The same sentences as the previous test, outside a window. Without this
    counter-witness, a `find_undeclared_values` returning nothing would pass both.
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
    """The neighbouring shapes that are NOT governed values."""
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
    """Return a level-2 section, region markers included."""
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


def _disaster_restore_invocation(document: str) -> str:
    """The `pg_restore` command an operator runs in a disaster, joined into one line.

    Read from the document rather than pinned here: a copy would go green on the
    day the runbook changed, which is the whole class of defect this module
    exists for.
    """
    joined = document.replace("\\\n", " ")
    invocations = [
        " ".join(line.split())
        for line in joined.splitlines()
        if "pg_restore" in line and "--dbname" in line
    ]
    assert len(invocations) == 1, (
        f"expected exactly one pg_restore invocation with --dbname, found {len(invocations)}"
    )
    return invocations[0]


def test_the_disaster_restore_command_survives_a_cluster_without_the_database() -> None:
    """Ticket `41c7f0e8`. A disaster command that aborts on its own first statement.

    `pg_restore --clean --create` emits `DROP DATABASE <name>` BEFORE the create,
    and `--exit-on-error` makes the missing database fatal: rc=1 on a fresh
    cluster. Measured two days running — by w42 on 2026-09-02 and w47 on
    2026-09-03 — and both drills primed the target by hand to get past it.

    A disaster runbook whose first command fails on the case it exists for is a
    hypothesis, not a procedure (rule `087e74ff`). `--if-exists` changes the
    behaviour in exactly ONE case, the one that fails: an absent object. Where
    the database exists — the real disaster — the emitted DROP is the same.
    """
    invocation = _disaster_restore_invocation(RUNBOOK.read_text(encoding="utf-8"))

    if "--clean" in invocation and "--create" in invocation:
        assert "--if-exists" in invocation, (
            "the disaster restore command carries --clean --create without --if-exists: "
            "it emits DROP DATABASE before CREATE and aborts on a cluster that does not "
            f"have the database yet.\n    {invocation}"
        )


@pytest.mark.parametrize("document_path", GUARDED_DOCUMENTS, ids=lambda path: path.name)
def test_the_caveat_lives_wherever_a_receipt_is_read(document_path: Path) -> None:
    """A receipt is never read without what it does not prove.

    This is the sentence a section rewrite carries away without meaning to: every
    receipt in this file comes from LIVE production and says NOTHING about a real
    restoration. It is therefore required in BOTH places where a denominator is
    read — the declaration at the top, and the dated section telling the story of
    the five extensions of 2026-08-22 — and not in one only, without which moving
    the figure would be enough to dilute it.
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
        # Gate P1 has lived in `58711012` since 2026-08-28: `8eaefe36`, which
        # carried it first, was closed as superseded by its CATALOGUE splits — none
        # of which carries this gate — and `closed` is terminal. This pin cemented
        # the stale number for a day; it now pins the LIVE gate, and correcting it
        # costs, by design, editing this test and the document in a single reviewed
        # move.
        assert "58711012" in text, f"{name} ne nomme pas la porte P1 restée ouverte"
        assert "is true by construction" in text, (
            f"{name} ne dit pas que le contrôle écrit POUR un restore est celui "
            "qu'aucun reçu live ne peut exercer"
        )


@pytest.mark.parametrize("document_path", GUARDED_DOCUMENTS, ids=lambda path: path.name)
def test_the_declaration_comes_before_the_records(document_path: Path) -> None:
    """In a disaster you read top to bottom and stop at the first answer."""
    document = document_path.read_text(encoding="utf-8")
    declaration = document.index(f"<!-- {DECLARATION_REGION}:start -->")

    for historical in HISTORICAL_REGIONS:
        assert declaration < document.index(f"<!-- {historical}:start -->"), (
            f"`{historical}` précède la déclaration : un opérateur pressé lira les "
            "nombres d'une bascule passée avant la cible courante"
        )
