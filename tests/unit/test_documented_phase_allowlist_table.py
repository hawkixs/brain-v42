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
        "The table mirrors the CODE. The prompt's `Allowed tools` line says what a "
        "phase should CALL and is a different, smaller statement — copying it here "
        "documents a capability the server still grants."
    )


def test_the_table_is_not_a_copy_of_the_prompts() -> None:
    """The distinction that caused the drift, pinned so it cannot recur quietly.

    PROMOTE is the phase where the two differ today: the prompt names the tools
    the agent is told to call, the allowlist also carries the two plain-creation
    tools the server still permits. If a future change makes them identical,
    this test reddens and asks for the comment to be re-read rather than for the
    assertion to be deleted.
    """
    prompt = (
        Path(__file__).resolve().parents[2] / "scripts" / "dream" / "phase_promote.md"
    ).read_text(encoding="utf-8")
    listed = next(
        line for line in prompt.splitlines() if line.startswith("`brain_get`, `brain_search`")
    )
    prompt_tools = tuple(re.findall(r"`(brain_[a-z0-9_]+)`", listed))

    assert set(prompt_tools) < set(DREAM_PHASE_TOOL_ALLOWLISTS["promote"]), (
        "the PROMOTE prompt no longer names a strict subset of the allowlist; "
        "either the allowlist was tightened — in which case this table and this "
        "test are fine and the comment above needs updating — or a tool was "
        "added to the prompt without being permitted, which the allowlist guard "
        "will already have caught"
    )
