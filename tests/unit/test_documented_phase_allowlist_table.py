"""The six-phase table in ARCHITECTURE.md mirrors the CODE, not the prompts.

`docs/ARCHITECTURE.md` carries a table headed "Exact Brain MCP allowlist", one
row per Dream phase. It claims to reproduce
`DREAM_PHASE_TOOL_ALLOWLISTS` — the constant the server enforces — and nothing
checked that it did. `test_documentation_contract` does not parse it, and the
prompt guards assert only `called ⊆ allowed`, which is a different statement
about a different artefact.

The gap was found by review on 2026-09-04, on a row I had just rewritten myself.
Updating the PROMOTE row for the runbook split, I copied the prompt's "Allowed
tools" line instead of the constant, and dropped `brain_propose_adr` and
`brain_create_runbook`: seven tools documented where nine are enforced. The two
lines look interchangeable and are not. The prompt says what the phase SHOULD
call; the allowlist says what the server WILL permit. A table that silently
narrows the second reads, to an operator, as a capability that was removed.

Measured before this test existed: five of the six rows matched, PROMOTE did
not. So the test closes a class rather than an occurrence — nothing was watching
any of the six, and the five that were right were right by luck.

ORDER IS ASSERTED, NOT ONLY MEMBERSHIP. The constant is a tuple and the table is
a reading aid; when the two drift apart in order, a reader diffing them by eye
finds a difference that is not one, and learns to skip the exercise. Pinning the
order costs one edit per change and buys a table that can be compared by sight.

SINCE 2026-09-10 THE TWO LINES AGREE. Decision D9 retired the last two tools a
phase was granted without its prompt announcing them, so the paragraphs above
describe an incident, not a live difference: today copying the prompt would give
the right answer for PROMOTE too. That is precisely why the last test in this
file pins the agreement rather than leaving it to luck — the habit of reading the
constant is what survives the next divergence, not the coincidence.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from brain_v42.mcp.dream_capabilities import DREAM_PHASE_TOOL_ALLOWLISTS

ARCHITECTURE = Path(__file__).resolve().parents[2] / "docs" / "ARCHITECTURE.md"


def _documented_row(phase: str) -> tuple[str, ...]:
    """The tools named in the table row for `phase`, in the order they appear."""
    prefix = f"| {phase.upper()} |"
    for line in ARCHITECTURE.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return tuple(re.findall(r"`(brain_[a-z0-9_]+)`", line))
    raise AssertionError(f"no {phase.upper()} row in {ARCHITECTURE.name} — the table moved")


def test_the_table_has_a_row_for_every_phase() -> None:
    """Guard on the reader: a renamed phase must redden here, not go unnoticed."""
    for phase in DREAM_PHASE_TOOL_ALLOWLISTS:
        assert _documented_row(phase), f"{phase} row names no tool"


@pytest.mark.parametrize("phase", sorted(DREAM_PHASE_TOOL_ALLOWLISTS))
def test_the_documented_row_matches_the_enforced_allowlist(phase: str) -> None:
    documented = _documented_row(phase)
    enforced = tuple(DREAM_PHASE_TOOL_ALLOWLISTS[phase])

    assert documented == enforced, (
        f"{phase.upper()} row disagrees with DREAM_PHASE_TOOL_ALLOWLISTS.\n"
        f"  only enforced: {[t for t in enforced if t not in documented]}\n"
        f"  only documented: {[t for t in documented if t not in enforced]}\n"
        "The table mirrors the CODE. Since decision D9 every prompt announces "
        "exactly its grant, so copying the prompt's `Allowed tools` line happens "
        "to give the same answer today — which is why the test below pins that "
        "agreement instead of trusting it. Read the constant, not the prompt."
    )


@pytest.mark.parametrize("phase", sorted(DREAM_PHASE_TOOL_ALLOWLISTS))
def test_every_prompt_announces_exactly_what_its_phase_is_granted(phase: str) -> None:
    """What replaced the strict-subset check, and why it is a stronger statement.

    This test used to assert that PROMOTE's prompt named a STRICT subset of its
    allowlist — the prompt listing what to call, the allowlist also carrying the
    two plain-creation tools the server still permitted. It carried its own
    instruction for the day that gap closed: reread the comment, do not delete
    the assertion.

    Decision D9 closed it on 2026-09-10, and PROMOTE was the last phase where the
    two differed. So the property the old test relied on — some phase, somewhere,
    granting more than its prompt announces — no longer exists anywhere in the
    table, and no rewording of it could be green for a real reason.

    What is now true, and worth pinning precisely because it took a decision to
    reach: every prompt announces exactly its phase's grant. Combined with the
    row-by-row check above, the three statements agree — documentation, enforced
    allowlist, and prompt. A future divergence in EITHER direction reddens here:
    a grant the prompt does not announce, or an announcement the server does not
    honour.

    Note the reading rule this needs and the earlier version did not. The six
    prompts do not share a syntax: PROMOTE backticks its tool names, SCAN does
    not. A regex requiring backticks reads SCAN's section as empty, and an empty
    set is a subset of anything — green on nothing, which is the false witness
    this file exists to avoid.
    """
    prompt_path = Path(__file__).resolve().parents[2] / "scripts" / "dream" / f"phase_{phase}.md"
    lines = prompt_path.read_text(encoding="utf-8").splitlines()

    start = next(index for index, line in enumerate(lines) if line.strip() == "## Allowed tools")
    section: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        section.append(line)

    announced = set(re.findall(r"\b(brain_[a-z0-9_]+)", "\n".join(section)))
    granted = set(DREAM_PHASE_TOOL_ALLOWLISTS[phase])

    assert announced, (
        f"phase {phase}: the `## Allowed tools` section names no tool. Either the "
        "section moved or the reading rule is stale — do not let this assert on "
        "the empty set."
    )
    assert announced == granted, (
        f"phase {phase}: prompt and allowlist disagree.\n"
        f"  granted but not announced: {sorted(granted - announced)}\n"
        f"  announced but not granted: {sorted(announced - granted)}\n"
        "A granted tool the prompt hides is a capability nobody decided to give; "
        "an announced tool the server refuses is a lost night."
    )
