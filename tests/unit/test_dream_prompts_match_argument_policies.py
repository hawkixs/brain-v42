"""No prompt instructs an argument its tool's policy refuses.

`test_dream_prompts_match_phase_allowlists.py` keeps prompts and allowlists in
agreement about WHICH TOOLS a phase may call. Nothing kept them in agreement
about the ARGUMENTS those calls may carry — and the two are enforced by the same
middleware, one line apart.

THIS IS NOT HYPOTHETICAL. Measured in `logs/dream/2026-08-15_red_promote.events.jsonl`:

    tool: brain_propose_adr, arguments: {..., "dream_run_id": 1401}
    state: ERROR
    message: "Error in MCP tool execution: Dream project authorization denied"

`phase_promote.md` says `dream_run_id=<injected from env if available>`;
`PROJECT_TOOL_POLICIES["brain_propose_adr"].forbid_dream_run_id` is `True`, and
`dream_project_scope.py` calls `_deny(reason="dream_run_forbidden")` — a function
returning `Never`. The prompt asked for exactly what the server refuses, and one
promotion was lost to it.

It fired ONCE in 1 158 promote logs, and that rarity is the trap rather than a
consolation: `if available` made the failure depend on whether the agent found a
value to inject, so the same instruction is quiet on most nights and lethal on
the one where it is followed. A guard that only reddens under an agent's mood is
not a guard; this test reads the prompt instead.

WHY THE SERVER IS RIGHT AND THE PROMPT IS WRONG. `dream_runs` rows are written by
the orchestrator (`_insert_dream_run`, `_promote_helpers`), never by the phase
agent — CLAUDE.md's "six writers" paragraph names all of them and none is an
agent. SEC1b forbids the argument for that reason: an agent that could name its
own run id could attribute its work to another night's row.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from brain_v42.services.dream_project_scope import PROJECT_TOOL_POLICIES

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = REPO_ROOT / "scripts" / "dream"

_CALL_SITE = re.compile(r"\b(brain_[a-z0-9_]+)\s*\(")

#: Policy flag → the argument name it refuses. One entry today; the mapping
#: exists so a second `forbid_*` flag cannot be added without landing here.
_FORBIDDEN_ARGUMENT_BY_FLAG = {"forbid_dream_run_id": "dream_run_id"}


def _prompts() -> list[Path]:
    return sorted(PROMPT_DIR.glob("phase_*.md"))


def _call_arguments(text: str, start: int) -> str:
    """The argument text of the call whose `(` is at `start`, parens balanced.

    Scanned rather than read to the end of the line: a call broken over two lines
    would otherwise hide its forbidden argument on the second one.
    """
    depth = 0
    for index in range(start, len(text)):
        character = text[index]
        if character in "([":
            depth += 1
        elif character in ")]":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index]
    return text[start + 1 :]


def test_the_flag_mapping_covers_every_forbidding_policy_flag() -> None:
    """Fail-closed on the mapping itself.

    A second `forbid_<argument>` flag added to `DreamProjectToolPolicy` without an
    entry here would leave its argument unguarded in the prompts, silently — the
    exact shape of the hole this file closes.
    """
    from dataclasses import fields

    from brain_v42.services.dream_project_scope import DreamProjectToolPolicy

    forbidding = {f.name for f in fields(DreamProjectToolPolicy) if f.name.startswith("forbid_")}
    assert forbidding == set(_FORBIDDEN_ARGUMENT_BY_FLAG), (
        "un drapeau forbid_* de DreamProjectToolPolicy n'est pas couvert ici : "
        f"{sorted(forbidding ^ set(_FORBIDDEN_ARGUMENT_BY_FLAG))}"
    )


@pytest.mark.parametrize("prompt_path", _prompts(), ids=lambda path: path.stem)
def test_no_prompt_instructs_an_argument_the_policy_refuses(prompt_path: Path) -> None:
    text = prompt_path.read_text(encoding="utf-8")

    offences: list[str] = []
    for match in _CALL_SITE.finditer(text):
        tool_name = match.group(1)
        policy = PROJECT_TOOL_POLICIES.get(tool_name)
        if policy is None:
            continue
        arguments = _call_arguments(text, match.end() - 1)
        for flag, argument in _FORBIDDEN_ARGUMENT_BY_FLAG.items():
            if getattr(policy, flag, False) and re.search(rf"\b{argument}\s*=", arguments):
                line = text.count("\n", 0, match.start()) + 1
                offences.append(f"{prompt_path.name}:{line} {tool_name}({argument}=…)")

    assert not offences, (
        "le prompt instruit un argument que la politique de scope REFUSE — "
        "le serveur lève DreamProjectAuthorizationError et la promotion est "
        f"perdue (mesuré le 2026-08-15) : {offences}"
    )
