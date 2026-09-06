"""A Dream prompt may not name a tool the MCP catalogue does not register.

WHY THIS IS NOT COVERED by `test_dream_prompts_match_phase_allowlists.py`.
That test builds its universe of known tools from the allowlists themselves::

    known_tools = {tool for tools in DREAM_PHASE_TOOL_ALLOWLISTS.values() for tool in tools}
    for tool in sorted(set(_ANY_MENTION.findall(text)) & known_tools - allowed):

A name that is in NO allowlist therefore falls out of the intersection and is
never examined. That is fine while every tool a prompt could name is allowed
somewhere — and it stops being fine at the exact moment a tool is REMOVED from
the catalogue, which is when a stale mention is most likely and most harmful.
Measured on 2026-09-03 while removing `brain_list_adrs`: with the tool gone
from every allowlist, the existing guard stays green on a prompt that still
tells the agent the tool exists.

The same hole swallows a typo. `brain_lst_adrs` is in no allowlist, so it is
in `known_tools` for nobody, so nothing reads it.

THE SOURCE MATTERS. This guard reads the REGISTRATION modules, not the
allowlists — a guard whose reference is produced by the thing it guards proves
only self-consistency (learning c34fb865). It parses the decorators rather than
importing the server: the question is which tools the repository declares, and
a syntax-level answer cannot be bent by an import-time condition.

WHAT THIS DOES NOT PROVE. That the tool is REACHABLE by that phase — the
allowlist test owns that. That the running server registers it — the server
imports its constants once and is restarted by hand, so only a live smoke can
say. This says the name is not fiction.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = REPO_ROOT / "scripts" / "dream"
TOOLS_DIR = REPO_ROOT / "src" / "brain_v42" / "mcp" / "tools"

#: `brain_v42` is the Python package, not a tool. It is the only `brain_*`
#: token a prompt legitimately names without a tool behind it; anything else
#: added here should be justified in the same breath.
_NOT_A_TOOL = frozenset({"brain_v42"})


def _registered_tool_names() -> frozenset[str]:
    """Every function the tool modules decorate with `@<something>.tool(...)`."""
    names: set[str] = set()
    for path in sorted(TOOLS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if isinstance(target, ast.Attribute) and target.attr == "tool":
                    names.add(node.name)
    return frozenset(names)


def _prompts() -> list[Path]:
    return sorted(PROMPT_DIR.glob("phase_*.md"))


def _mentions(text: str) -> set[str]:
    import re

    return set(re.findall(r"\b(brain_[a-z][a-z0-9_]*)", text)) - _NOT_A_TOOL


def test_the_catalogue_source_is_not_empty() -> None:
    """Guard on the guard: an empty universe would make every check vacuous."""
    registered = _registered_tool_names()

    assert len(registered) > 40, f"only {len(registered)} tools parsed — the AST rule broke"
    assert "brain_search" in registered


def test_every_prompt_names_at_least_one_tool() -> None:
    """A prompt that mentions nothing would satisfy the check below on nothing."""
    for prompt in _prompts():
        assert _mentions(prompt.read_text(encoding="utf-8")), f"{prompt.name} names no tool"


@pytest.mark.parametrize("prompt", _prompts(), ids=lambda path: path.name)
def test_prompt_names_only_tools_the_repository_registers(prompt: Path) -> None:
    registered = _registered_tool_names()

    unknown = sorted(_mentions(prompt.read_text(encoding="utf-8")) - registered)

    assert unknown == [], (
        f"{prompt.name} names {unknown}, which no tool module registers. "
        "A removed tool leaves this mention behind and the allowlist guard "
        "cannot see it: its universe of known names comes from the allowlists, "
        "which the removal has already emptied of this name."
    )
