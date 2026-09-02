"""No prompt calls a tool its phase is not allowed to use.

The per-phase capability firewall (SEC1a) stayed INERT from its delivery on
2026-07-16 until the arming of 2026-08-10. For thirteen agent-months, a phase's
allowlist and what its prompt asked it to call could diverge without a single
night noticing: nothing refused a call.

Armed, an off-list call raises `DreamProjectAuthorizationError`. That is
fail-closed and noisy — the phase fails, the unit reddens — so not a silent
failure mode. But it is a lost night for a discrepancy this test catches in
30 ms.

THE READING RULE, and why it really separates. A CALL SITE carries an opening
parenthesis: `brain_list(entity_type=...)`. A PROHIBITION is written without one:
"Do NOT call brain_learn." The four prompts that cite `brain_learn` outside their
list all cite it in order to forbid it, and `phase_promote.md` lists
`brain_update`, `brain_accept_adr` and `brain_delete` with the same intent.
Measured on 2026-08-10: the rule leaves zero false positives over the six
prompts.

What the arming really changes, and what is worth naming: these prohibitions were
PROSE, which the model could ignore. They are now backed by a server-side
refusal. The prompt and the allowlist say the same thing — this test is what
keeps them in agreement.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from brain_v42.mcp.dream_capabilities import DREAM_PHASE_TOOL_ALLOWLISTS

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = REPO_ROOT / "scripts" / "dream"

# Call site = tool name followed by an opening parenthesis.
_CALL_SITE = re.compile(r"\b(brain_[a-z0-9_]+)\s*\(")
# Any mention, call or not. Used by the non-regression guard below.
_ANY_MENTION = re.compile(r"\b(brain_[a-z][a-z0-9_]*)")
_PROHIBITION = re.compile(r"\bNOT\b|\bnever\b|\bNEVER\b|[Ff]orbidden|\bdo not\b")


def _prompt(phase: str) -> str:
    return (PROMPT_DIR / f"phase_{phase}.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("phase", sorted(DREAM_PHASE_TOOL_ALLOWLISTS))
def test_every_call_site_is_inside_the_phase_allowlist(phase: str) -> None:
    called = set(_CALL_SITE.findall(_prompt(phase)))
    allowed = set(DREAM_PHASE_TOOL_ALLOWLISTS[phase])

    assert called <= allowed, (
        f"phase {phase} : le prompt appelle {sorted(called - allowed)}, "
        "qui n'est pas dans sa liste blanche. Armé, le serveur refusera l'appel "
        "et la phase échouera — bruyamment, mais la nuit sera perdue."
    )


@pytest.mark.parametrize("phase", sorted(DREAM_PHASE_TOOL_ALLOWLISTS))
def test_every_prompt_actually_calls_something(phase: str) -> None:
    """Guard on the test itself.

    A prompt rewritten in another syntax — without parentheses — would make the
    assertion above true on the empty set, hence green on nothing. This is the
    kind of false witness this repository has already met three times.
    """
    assert _CALL_SITE.findall(_prompt(phase)), (
        f"phase {phase} : aucun site d'appel détecté. Soit le prompt a changé de "
        "syntaxe et la règle de lecture est à revoir, soit il n'appelle plus rien."
    )


@pytest.mark.parametrize("phase", sorted(DREAM_PHASE_TOOL_ALLOWLISTS))
def test_tools_mentioned_outside_the_allowlist_are_only_prohibitions(phase: str) -> None:
    """An off-list tool may appear only in order to be forbidden.

    Without this assertion, a prompt could ask for an off-list tool in a prose
    sentence — "use brain_delete when…" — with no parenthesis, and the main test
    would not see it.
    """
    text = _prompt(phase)
    allowed = set(DREAM_PHASE_TOOL_ALLOWLISTS[phase])
    known_tools = {tool for tools in DREAM_PHASE_TOOL_ALLOWLISTS.values() for tool in tools}
    lines = text.splitlines()

    for tool in sorted(set(_ANY_MENTION.findall(text)) & known_tools - allowed):
        for index, line in enumerate(lines):
            if tool not in line:
                continue
            # A window, not a single line: the prohibition is also written as a
            # SECTION HEADING followed by the list — `## Forbidden tools` then the
            # names on the next line. A test on the line alone would have required
            # rewriting the prompt to satisfy the test, which is the wrong
            # direction.
            window = "\n".join(lines[max(0, index - 2) : index + 1])
            assert _PROHIBITION.search(window), (
                f"phase {phase} : `{tool}` est hors liste blanche et rien dans les "
                f"trois lignes qui le portent ne l'interdit :\n  {line.strip()}"
            )
