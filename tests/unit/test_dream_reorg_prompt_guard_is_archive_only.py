"""`phase_reorg.md` must say, black on white, that REORG's guard is archive-only.

Operator decision 2026-09-07 (lot D15): the existing prose already scattered the
individual facts -- line 9's "Never touch content, never delete" and Part 2's
"Archive = set freshness_status=archived ... fully reversible" -- but nowhere
named the guard as a single contract, and nowhere said which pieces of it the
server actually enforces versus which are prompt-only convention. That
distinction matters: `brain_update`'s Pydantic update models (`LearningUpdate`,
`DecisionUpdate`) accept far more than `tags` and `freshness_status` --
`topic`, `insight`, `status`, `description`, `reasoning`, and
`freshness_status` set back to `"fresh"` or `"stale"` are all mechanically
writable through the very same call, and `reorg_validate.py` never checks that
any of them stayed untouched. Only the ownership fields
(`project_key`, `project_group`, `project_keys`, `owner_project_key`,
`dream_run_id`, `superseded_by`) are refused by the server, by NAME
(`ownership_field_forbidden`, `dream_project_scope.py`). Likewise
`brain_merge_entities` and `brain_delete` are unreachable in this phase
because they are simply absent from `DREAM_PHASE_TOOL_ALLOWLISTS["reorg"]`
(`dream_capabilities.py`), not because of any per-call check.

Lesson from lot D18 (four review rounds on a sibling prompt file): a universal
claim ("no entity type...", "never...") must not be written unless the code
proves it for every case it covers. This test therefore pins per-fact
sentences against the evidence gathered for this lot, not a paraphrase of the
whole guard as one blanket claim.

Two kinds of assertion:

- Prose-anchored: the section names itself archive-only, restates the two
  allowed mutations, and names the tools/fields it says are unreachable.
- Enforcement-provenance: the section must say `ownership_field_forbidden` and
  `tool_not_allowed_for_phase` for what the server does refuse, and must
  contain the literal phrase "prompt rule, not enforced by `reorg_validate.py`"
  for what it does not -- so a future edit cannot quietly upgrade a prompt-only
  rule to a claimed guarantee without this test reddening.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from brain_v42.mcp.dream_capabilities import DREAM_PHASE_TOOL_ALLOWLISTS
from brain_v42.services.dream_project_scope import _OWNERSHIP_FIELDS

REPO_ROOT = Path(__file__).resolve().parents[2]
PHASE_REORG_PROMPT = REPO_ROOT / "scripts" / "dream" / "phase_reorg.md"
HEADING = "## Guard — archive only"


def _prompt_text() -> str:
    return PHASE_REORG_PROMPT.read_text(encoding="utf-8")


def _guard_section() -> str:
    """Slice from the heading to the next `## ` heading, or end of file."""
    text = _prompt_text()
    start = text.index(HEADING)
    rest = text[start + len(HEADING) :]
    next_heading = re.search(r"\n## ", rest)
    end = next_heading.start() if next_heading else len(rest)
    return rest[:end]


def test_guard_section_exists() -> None:
    assert HEADING in _prompt_text(), (
        f"phase_reorg.md has no '{HEADING}' section -- nothing states the "
        "archive-only guard as a single, explicit contract."
    )


def test_guard_section_names_itself_archive_only() -> None:
    section = _guard_section()
    assert "archive-only" in section, (
        f"phase_reorg.md's '{HEADING}' section does not describe the phase as archive-only."
    )


def test_guard_section_states_the_two_allowed_mutations() -> None:
    section = _guard_section()
    assert "tags" in section
    assert 'freshness_status="archived"' in section, (
        f"phase_reorg.md's '{HEADING}' section does not restate "
        'freshness_status="archived" as one of the only two mutations allowed.'
    )


@pytest.mark.parametrize("tool", ["brain_merge_entities", "brain_delete"])
def test_guard_section_names_the_unreachable_tools(tool: str) -> None:
    """Merge/dedup and delete are named, and named as unreachable by this phase."""
    section = _guard_section()
    assert tool in section, (
        f"phase_reorg.md's '{HEADING}' section does not name `{tool}` as one of "
        "the tools this phase can never reach."
    )
    assert tool not in DREAM_PHASE_TOOL_ALLOWLISTS["reorg"], (
        f"`{tool}` is in the reorg allowlist -- the guard section's claim that "
        "it is unreachable would be false."
    )


def test_guard_section_names_the_middleware_denial_reason() -> None:
    section = _guard_section()
    assert "tool_not_allowed_for_phase" in section, (
        f"phase_reorg.md's '{HEADING}' section does not name the denial reason "
        "(`tool_not_allowed_for_phase`) `DreamCapabilityMiddleware.on_call_tool` "
        "raises for an off-allowlist tool -- the claim that merge/delete are "
        "unreachable needs its server-side evidence named, not just asserted."
    )


def test_guard_section_names_ownership_field_refusal() -> None:
    section = _guard_section()
    assert "ownership_field_forbidden" in section, (
        f"phase_reorg.md's '{HEADING}' section does not name the "
        "`ownership_field_forbidden` refusal reason that actually blocks "
        "`project_key` and the other ownership fields."
    )
    for field in sorted(_OWNERSHIP_FIELDS):
        assert field in section, (
            f"phase_reorg.md's '{HEADING}' section does not name the ownership "
            f"field `{field}` from `_OWNERSHIP_FIELDS` -- the list is read from "
            "the server policy elsewhere in this repo precisely so it cannot "
            "silently drift from the prompt."
        )


def test_guard_section_flags_the_field_and_unarchive_restriction_as_prompt_only() -> None:
    """The one claim the code does NOT back must say so, verbatim."""
    section = _guard_section()
    assert "prompt rule, not enforced by `reorg_validate.py`" in section, (
        f"phase_reorg.md's '{HEADING}' section must say the restriction to "
        "tags/freshness_status, and the ban on un-archiving, is a prompt rule "
        "not enforced by reorg_validate.py -- nothing server-side stops "
        "`brain_update` from writing `topic`, `insight`, `status`, or "
        '`freshness_status="fresh"`/`"stale"` in this phase.'
    )


def test_guard_section_names_what_reorg_validate_does_enforce() -> None:
    """The companion claim: reorg_validate.py is not toothless, just narrower."""
    section = _guard_section()
    for symbol in ("_MAX_UPDATED", "_MAX_ARCHIVED", "_reject_foreign_project"):
        assert symbol in section, (
            f"phase_reorg.md's '{HEADING}' section does not name `{symbol}`, "
            "one of the checks reorg_validate.py actually performs."
        )


def test_reorg_still_has_no_merge_or_delete_in_its_allowlist() -> None:
    """Self-guard: if this ever changes, the unreachability claim above must
    be revisited, not silently left stale."""
    allowed = DREAM_PHASE_TOOL_ALLOWLISTS["reorg"]
    assert "brain_merge_entities" not in allowed
    assert "brain_delete" not in allowed
