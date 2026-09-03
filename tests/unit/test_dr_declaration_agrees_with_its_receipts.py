"""The DR declaration cannot claim UNMEASURED what a receipt records.

Ticket `3b3d64a1`, opened 2026-08-28 after `dr-current` announced head `046` for
six days while production sat at `048`, with
`test_runbook_normative_values_have_one_source.py` 9/9 green throughout. That
gate is not at fault and its docstring says so: it governs WHERE a value may be
written, never whether the value is TRUE. This module is the thread that ticket
opened.

It closes ONE half, the half that is honestly closable. A claim of ABSENCE --
"NOT replayed", "NOT MEASURED for v8" -- is the one statement no test can derive
from the database, because it asserts that nothing happened. What it CAN be
derived from is the opposite: a receipt. So receipts became artifacts of this
repository (`ops/recovery/receipts/`), and the rule is a contradiction check --
if a receipt records a real-restore replay of contract vN, the declaration may
not go on saying vN was never replayed.

WHY THIS SHAPE AND NOT THE OTHERS, from the ticket's own list. An integration
test reading `alembic_version` would be faithful but would not run with the
runner off, so it would be green-by-absence -- the very defect being closed.
Comparing against the REPOSITORY head instead of production was explicitly ruled
out: repository and production differ on purpose, and pinning them would make the
declaration lie in the other direction the moment a migration is written but not
applied.

WHAT THIS DOES NOT PROVE, and it matters more here than usual. It does not prove
a receipt is honest -- a receipt is written by whoever ran the drill. It proves
only that the declaration and the receipts in this repository do not contradict
each other. And it would NOT have caught the defect that opened this ticket: on
2026-09-03 the four stale cells were dated, and dated CORRECTLY -- they carried
2026-09-02, the real date of the v7 measurement they described, while asserting
an absence for v8. A freshness rule reads that block as healthy. Only a receipt
can contradict it.

The shelf life of the sentence being repaired is worth writing down: "no dump on
hand can do it" was committed at 00:11 on 2026-09-03 (`8b8368f`) and was false by
03:02, when the nightly `red-backup` rail wrote a dump at head 051 -- two hours
and fifty-one minutes. Nothing marked it perishable.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
RUNBOOK: Final = REPO_ROOT / "docs" / "PLAN_INDEX_REPAIR_RUNBOOK.md"
RECEIPTS_DIR: Final = REPO_ROOT / "ops" / "recovery" / "receipts"

#: The region that holds the CURRENT declaration. Same name the one-source gate
#: uses; read here rather than imported, because that module owns a different
#: rule and coupling the two would make one break the other.
_DECLARATION_REGION: Final = "dr-current"

_FIELD: Final = re.compile(r"^\s*([a-z_]+):\s*(\S.*?)\s*$", re.M)

#: The vocabulary of an ABSENCE. Kept as a tuple because each wording is a
#: separate way of saying "this was never done", and a census that recognised
#: only one of them would read a surviving claim as nothing to report.
_ABSENCE_WORDINGS: Final = (
    "not replayed",
    "not measured",
    "has not been",
    "never replayed",
)


def declaration_block(document: str) -> str:
    start = f"<!-- {_DECLARATION_REGION}:start -->"
    end = f"<!-- {_DECLARATION_REGION}:end -->"
    assert start in document and end in document, "la région `dr-current` a disparu du runbook"
    return document.split(start, 1)[1].split(end, 1)[0]


def receipts() -> list[dict[str, str]]:
    """Every receipt in the repository, as its parsed fields."""
    if not RECEIPTS_DIR.is_dir():
        return []
    parsed: list[dict[str, str]] = []
    for path in sorted(RECEIPTS_DIR.glob("*.md")):
        fields = dict(_FIELD.findall(path.read_text(encoding="utf-8")))
        fields["path"] = str(path.relative_to(REPO_ROOT))
        parsed.append(fields)
    return parsed


def contradicted_absence_claims(block: str, version: str) -> list[str]:
    """Rows of the declaration that still deny a replay of `version`."""
    offending: list[str] = []
    for line in block.splitlines():
        if not line.startswith("|"):
            continue
        lowered = line.lower()
        if version.lower() not in lowered:
            continue
        if any(wording in lowered for wording in _ABSENCE_WORDINGS):
            offending.append(line.strip()[:160])
    return offending


def test_a_real_restore_receipt_exists_for_the_current_contract() -> None:
    """Without a receipt this module proves nothing, and would say so silently."""
    real_restores = [r for r in receipts() if r.get("kind") == "real-restore"]

    assert real_restores, (
        "aucun reçu de restauration RÉELLE dans `ops/recovery/receipts/` : ce module "
        "serait vert par absence de matière, exactement le défaut que le ticket ferme"
    )


def test_the_declaration_does_not_deny_a_replay_a_receipt_records() -> None:
    """The contradiction check, derived from the receipts rather than pinned."""
    block = declaration_block(RUNBOOK.read_text(encoding="utf-8"))

    offending: list[str] = []
    for receipt in receipts():
        if receipt.get("kind") != "real-restore":
            continue
        version = receipt.get("contract_version", "")
        assert version, f"{receipt['path']} : `contract_version` manquant"
        offending.extend(
            f"{receipt['path']} atteste {version} — mais `dr-current` dit encore :\n    {row}"
            for row in contradicted_absence_claims(block, version)
        )

    assert not offending, (
        "`dr-current` nie une restauration réelle qu'un reçu du dépôt enregistre. "
        "Mets la déclaration à jour, ou retire le reçu s'il est faux — mais les deux "
        "ne peuvent pas coexister.\n" + "\n".join(offending)
    )


def test_the_check_catches_a_declaration_that_lags_its_receipt() -> None:
    """Counter-witness, frozen: the exact wordings of 2026-09-03.

    Without it, narrowing `_ABSENCE_WORDINGS` would silently empty the rule while
    leaving every assertion above green.
    """
    lagging = "\n".join(
        (
            "| Recovery contract, restored target | `x-v8-pgrestore.sql` | "
            "**NOT replayed against a real restore.** |",
            "| Contract receipt, `-pgrestore` against a real restore | **NOT MEASURED for v8.** |",
            "| ACL contract, restored target | **Not replayed for v8** |",
        )
    )

    assert len(contradicted_absence_claims(lagging, "v8")) == 3
    assert contradicted_absence_claims(lagging, "v9") == [], "une autre version ne doit pas matcher"
