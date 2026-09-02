"""Phase 0 anchor — the covenant sentence lives in the docstrings, and nothing guarded it.

The explicitness covenant is not merely a documentation rule: it is written into every
tool's CONTRACT, and it is what an agent reads before calling. Its current form is
`_COVENANT` below; the pre-046 one is `_RETIRED_COVENANT`, and the "REWRITTEN BY NATURE"
box says why it changed.

Yet **no test checked its presence** (surveyed on 2026-08-19: zero occurrences of that
sentence in tests/). It could have disappeared from one or all seven tools without a
single suite turning red — and the covenant would have become an intention in prose.

This test is WRITTEN TO BE EXTENDED. The redesign delivers an eighth tool (`checkpoint`,
migration M-C): the day it lands, `_EXPECTED_TOOL_COUNT` goes to 8 and the word in the
registration docstring goes to "eight", IN THE SAME COMMIT as the tool. That is
deliberately a point of friction: it forces the contract to be updated at the moment the
surface changes, instead of letting it drift.

**REWRITTEN BY NATURE (ADR §0ter (d), ratified resolution).** 046 gives birth to `agent`
sessions the SERVER opens and the nightly sweep closes: the sentence "No hook or
auto-close may invoke this lifecycle boundary." had therefore become false as written. It
is not DELETED — the contract must stay readable where the agent reads it — it now NAMES
its exception. This test turned red on all seven tools before being updated; that was the
Red gesture opening the delivery, not damage.

`_RETIRED_COVENANT` is a NEGATIVE WITNESS, not a redundancy: without it, reintroducing the
old sentence ALONGSIDE the new one would leave both live in the same contract, one of them
lying. An expired criterion is turned into an absence test.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_MODULE = (
    pathlib.Path(__file__).resolve().parents[4]
    / "src/brain_v42/mcp/tools/session_lifecycle_tools.py"
)

#: Compared on NORMALIZED whitespace: the sentence spans three docstring lines, and a
#: test sensitive to line breaks would pin the wrapping, not the contract.
_COVENANT = (
    "An agent tracer is the only session the server opens or closes on its own; "
    "no hook and no auto-close may invoke this lifecycle boundary."
)

#: The PRE-046 form, which knew nothing of tracers. Must have disappeared.
_RETIRED_COVENANT = "No hook or auto-close may invoke this lifecycle boundary."

#: Eight since the checkpoint (M-C, migration 051) landed with its covenant sentence.
_EXPECTED_TOOL_COUNT = 8

#: The word the registration docstring must use for this number.
_COUNT_WORD = {7: "seven", 8: "eight", 9: "nine"}


def _tool_functions() -> dict[str, ast.AsyncFunctionDef]:
    """Return the `brain_session_*` functions, wherever they are nested."""
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("brain_session_")
    }


def test_the_lifecycle_surface_is_exactly_the_expected_size() -> None:
    tools = _tool_functions()
    assert sorted(tools) == sorted(
        [
            "brain_session_start",
            "brain_session_list",
            "brain_session_resume",
            "brain_session_capture",
            "brain_session_heartbeat",
            "brain_session_checkpoint",
            "brain_session_end",
            "brain_session_abandon",
        ]
    )
    assert len(tools) == _EXPECTED_TOOL_COUNT


def _normalized_docstring(name: str) -> str:
    doc = ast.get_docstring(_tool_functions()[name])
    assert doc is not None, f"{name} n'a pas de docstring"
    return " ".join(doc.split())


@pytest.mark.parametrize("name", sorted(_tool_functions()))
def test_every_lifecycle_tool_states_the_covenant_in_its_docstring(name: str) -> None:
    assert _COVENANT in _normalized_docstring(name), (
        f"{name} ne porte plus la phrase-covenant. Le covenant d'explicitation est "
        f"écrit dans le contrat du tool, pas seulement dans CLAUDE.md : le retirer "
        f"change ce qu'un agent lit avant d'appeler."
    )


@pytest.mark.parametrize("name", sorted(_tool_functions()))
def test_no_tool_still_carries_the_pre_046_covenant(name: str) -> None:
    """Negative witness: the old sentence must survive nowhere.

    It asserted that NO auto-closure crosses this boundary. Since 046 that is false
    for `agent` tracers, and two contradictory sentences in the same contract are
    worse than a single expired one.
    """
    assert _RETIRED_COVENANT not in _normalized_docstring(name), (
        f"{name} porte encore la phrase d'avant la 046. Elle promet qu'aucune "
        f"auto-fermeture ne franchit cette frontière — le balayage nocturne la "
        f"franchit désormais sur les sessions `agent`."
    )


def test_the_registration_docstring_counts_the_tools_it_registers() -> None:
    """The number spelled out must follow the real surface.

    Without this test, "seven" would survive the arrival of the eighth tool and the
    docstring would lie about what it registers.
    """
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    register = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == "register_session_lifecycle_tools"
    )
    doc = ast.get_docstring(register) or ""
    expected_word = _COUNT_WORD[_EXPECTED_TOOL_COUNT]
    assert expected_word in doc, (
        f"la docstring de register_session_lifecycle_tools() doit dire « {expected_word} » "
        f"pour {_EXPECTED_TOOL_COUNT} tools ; elle dit : {doc!r}"
    )
