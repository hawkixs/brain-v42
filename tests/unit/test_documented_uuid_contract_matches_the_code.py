"""The documented `parse_uuid` inventory is derived from the code, not from memory.

`docs/MCP_TOOLS.md` names the tools that normalise a malformed UUID to
`✗ Invalid UUID: <value>`, split by the two mechanisms that produce it. Nothing
checked that list, and it had drifted in three separate ways at once — measured
2026-09-04:

  * it named `brain_get_runbook` and `brain_execute_runbook`, neither of which
    calls `parse_uuid` at all;
  * it omitted `brain_ticket_reply` and `brain_ticket_transition`, which do;
  * it named three modules where four carry call sites, `ticket_tools.py`
    being the missing one.

The COUNT survived all of it — ten call sites before and after, because two
wrong names were balanced by two missing ones. A total that stays right while
its membership rots is the reason this test asserts the SET and not the number
(learning 57b85cbb: a total does not distinguish its zeros, and it does not
distinguish its swaps either).

WHY THIS ESCAPED THE REVIEW THAT FOUND IT. The reviewer measured only the three
modules the paragraph names, and reported "8 real call sites". Ten exist. A
measurement scoped by the claim it verifies inherits that claim's blind spot;
this test scans every tool module instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "src" / "brain_v42" / "mcp" / "tools"
MCP_TOOLS = REPO_ROOT / "docs" / "MCP_TOOLS.md"

#: The two mechanism bullets this file guards, each keyed on the call it makes.
#: The third bullet exists because naming only the DIRECT `parse_uuid` callers
#: excluded three tools that honour the identical contract one call deeper —
#: and the first version of this test ENFORCED that exclusion.
_MECHANISMS = {
    "**`parse_uuid()` from `parsing.py`**": "parse_uuid(",
    "**`resolve_entity_id()` from `entity_ids.py`**": "resolve_entity_id(",
}
_PARAGRAPH_MARKER = "**`parse_uuid()` from `parsing.py`**"

#: `brain_get` reaches `resolve_entity_id` on its non-plan branch and is listed
#: under the inline `try/except UUID()` bullet, where it is counted once. It is
#: therefore expected in the code scan and not in the prefix bullet.
_COUNTED_ELSEWHERE = {"brain_get"}


def _documented_line(marker: str = _PARAGRAPH_MARKER) -> str:
    for line in MCP_TOOLS.read_text(encoding="utf-8").splitlines():
        if marker in line:
            return line
    raise AssertionError(f"{marker} not found — the paragraph moved")


def _tools_calling(call: str) -> dict[str, str]:
    """Map each tool whose body contains `call` to the module it lives in.

    Read from the source of EVERY tool module, never from the modules the
    documentation happens to name.
    """
    found: dict[str, str] = {}
    for path in sorted(TOOLS_DIR.glob("*.py")):
        current: str | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*async def (brain_[a-z0-9_]+)\s*\(", line)
            if match:
                current = match.group(1)
            elif call in line and current is not None:
                found[current] = path.name
    return found


def _tools_calling_parse_uuid() -> dict[str, str]:
    return _tools_calling("parse_uuid(")


def _call_site_count() -> int:
    """Every `parse_uuid(` occurrence in the tool modules, minus its definition.

    `parsing.py` defines the helper and is not a call site; the import lines
    read `import parse_uuid` without a parenthesis and never match.
    """
    total = 0
    for path in sorted(TOOLS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        total += text.count("parse_uuid(") - text.count("def parse_uuid(")
    return total


def test_the_scan_finds_something_at_all() -> None:
    """Guard on the reader: an empty inventory would make every check vacuous."""
    assert len(_tools_calling_parse_uuid()) > 5


def test_every_documented_tool_really_calls_parse_uuid() -> None:
    """The half that was wrong: two names for tools that never call it."""
    line = _documented_line()
    documented = set(re.findall(r"`(brain_[a-z0-9_]+)`", line))
    actual = set(_tools_calling_parse_uuid())

    assert documented - actual == set(), (
        f"documented but never calling parse_uuid: {sorted(documented - actual)}"
    )


def test_every_calling_tool_is_documented() -> None:
    """The other half: two callers the paragraph never mentioned."""
    line = _documented_line()
    documented = set(re.findall(r"`(brain_[a-z0-9_]+)`", line))
    actual = set(_tools_calling_parse_uuid())

    assert actual - documented == set(), (
        f"calls parse_uuid but undocumented: {sorted(actual - documented)}"
    )


def test_every_module_carrying_a_call_site_is_named() -> None:
    """`ticket_tools.py` carried two call sites and was named by nothing."""
    line = _documented_line()
    # `parsing.py` is where the helper is DEFINED, not a module that calls it;
    # the sentence names it for that reason and it is not part of the inventory.
    documented = set(re.findall(r"`([a-z_]+\.py)`", line)) - {"parsing.py"}
    actual = set(_tools_calling_parse_uuid().values())

    assert actual == documented, (
        f"modules: code says {sorted(actual)}, doc says {sorted(documented)}"
    )


@pytest.mark.parametrize(("marker", "call"), sorted(_MECHANISMS.items()))
def test_each_mechanism_bullet_names_exactly_its_callers(marker: str, call: str) -> None:
    """Both directions, on BOTH mechanisms.

    The first version of this file guarded only the direct-`parse_uuid` bullet,
    which is why it enforced the exclusion of `brain_get_runbook`,
    `brain_execute_runbook` and `brain_ticket_get` instead of catching it: a
    guard scoped to one path cannot see a tool that takes another.
    """
    line = _documented_line(marker)
    # `_COUNTED_ELSEWHERE` leaves BOTH sides: the prefix bullet names `brain_get`
    # on purpose, to say where it IS counted, and a bullet that explains itself
    # must not be punished for it.
    documented = set(re.findall(r"`(brain_[a-z0-9_]+)`", line)) - _COUNTED_ELSEWHERE
    actual = set(_tools_calling(call)) - _COUNTED_ELSEWHERE

    assert documented - actual == set(), (
        f"documented, never calls {call}: {sorted(documented - actual)}"
    )
    assert actual - documented == set(), (
        f"calls {call}, undocumented: {sorted(actual - documented)}"
    )


def test_the_documented_total_counts_every_mechanism() -> None:
    """The headline number, derived rather than remembered.

    It said 13 while three tools honoured the contract through a third path.
    """
    text = MCP_TOOLS.read_text(encoding="utf-8")
    claimed = int(re.search(r"The (\d+) legacy string-returning tools", text).group(1))

    named: set[str] = set()
    for marker in _MECHANISMS:
        named |= set(re.findall(r"`(brain_[a-z0-9_]+)`", _documented_line(marker)))
    named |= set(
        re.findall(
            r"`(brain_[a-z0-9_]+)`",
            _documented_line("**Inline `try/except UUID()` in `crud_tools.py`**"),
        )
    )

    assert claimed == len(named), f"the paragraph says {claimed} and names {len(named)}"


def test_the_documented_call_site_count_matches_the_code() -> None:
    """Kept, but LAST: the count stayed right through three membership errors."""
    line = _documented_line()
    claimed = int(re.search(r"\((\d+) call sites", line).group(1))

    assert claimed == _call_site_count()
