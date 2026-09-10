"""The PROMOTE phase grants the promotion tools, not the creation tools.

Decision D9. When ADR and runbook promotion each got their own tool (PR #102
and PR #113), the promote prompt stopped naming `brain_propose_adr` and
`brain_create_runbook` — but the phase allowlist kept granting them. A grant
nobody asks for is not neutral: it is the difference between "the agent cannot
create an unpromoted ADR" and "it happens not to."

The tightening waited on evidence rather than on reasoning. Measured over the
254 promote event files from 2026-07-13 to 2026-09-10, counting real
`mcp_tool_call` items rather than prose mentions: the last `brain_propose_adr`
call was on 2026-09-07 and the last `brain_create_runbook` call on the same
night — the transition night, which also carried the first two
`brain_promote_adr` calls. The three nights since (09-08, 09-09, 09-10) called
`brain_promote_adr` and nothing else. Three nights is not a proof, and this
test is not the proof either; it is what keeps the decision from silently
rotting back.

`brain_promote_runbook` has never been called by any night. That is not
evidence of disuse: it only became reachable on the 2026-09-09 release, and no
runbook candidate has arisen since. Absence of a candidate is not absence of a
caller, so it stays granted.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from brain_v42.mcp.dream_capabilities import (
    DREAM_PHASE_TOOL_ALLOWLISTS,
    _audit_tool_name,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = REPO_ROOT / "scripts" / "dream"

_ANY_MENTION = re.compile(r"\b(brain_[a-z][a-z0-9_]*)")

RETIRED_FROM_PROMOTE = ("brain_propose_adr", "brain_create_runbook")


def _prompt(phase: str) -> str:
    return (PROMPT_DIR / f"phase_{phase}.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("tool", RETIRED_FROM_PROMOTE)
def test_promote_no_longer_grants_the_creation_tools(tool: str) -> None:
    assert tool not in DREAM_PHASE_TOOL_ALLOWLISTS["promote"], (
        f"`{tool}` is still granted to the promote phase. The prompt routes "
        "materialisation through the promotion tools, and no night has called "
        "this one since 2026-09-07."
    )


@pytest.mark.parametrize("tool", ("brain_promote_adr", "brain_promote_runbook"))
def test_promote_still_grants_both_promotion_tools(tool: str) -> None:
    """The counterweight to the test above.

    Tightening a grant is one edit away from removing the wrong pair.
    `brain_promote_runbook` in particular has never been called and would look
    unused to anyone counting calls without asking why the count is zero.
    """
    assert tool in DREAM_PHASE_TOOL_ALLOWLISTS["promote"], (
        f"`{tool}` must stay granted: it is the phase's materialisation path."
    )


@pytest.mark.parametrize("phase", sorted(DREAM_PHASE_TOOL_ALLOWLISTS))
def test_no_phase_grants_a_tool_its_prompt_never_mentions(phase: str) -> None:
    """A grant no prompt even mentions is a grant nobody decided to make.

    Deliberately weaker than "every granted tool has a call site". Ten entries
    across four phases are granted without a call site and named only in prose —
    a conditional branch, a fallback, an explicit prohibition — and turning those
    into call sites would mean rewriting prompts to satisfy a test. This asserts
    the far lower bar that the prompt at least knows the tool exists.

    On 2026-09-10 exactly one entry in the whole table failed that bar:
    `brain_propose_adr` in `promote`.
    """
    mentioned = set(_ANY_MENTION.findall(_prompt(phase)))
    unmentioned = sorted(set(DREAM_PHASE_TOOL_ALLOWLISTS[phase]) - mentioned)

    assert not unmentioned, (
        f"phase {phase}: {unmentioned} granted but never mentioned in "
        f"phase_{phase}.md. Either the prompt should use it, or the phase "
        "should not be able to."
    )


@pytest.mark.parametrize("tool", RETIRED_FROM_PROMOTE)
def test_a_retired_tool_is_still_named_in_the_denial_audit(tool: str) -> None:
    """Tightening a grant must not blind the log that reports it firing.

    `_SAFE_AUDIT_TOOL_NAMES` is derived from the union of every phase allowlist,
    so removing the last grant of a tool also removes its name from the audit
    vocabulary — and a denial would be recorded as `<redacted>`. That is exactly
    backwards: the one event this decision could produce is an agent reaching for
    a tool it no longer has, and it must be readable by name.

    These are first-party tool names, not caller-supplied strings, so logging
    them carries none of the risk the redaction exists to prevent.
    """
    assert _audit_tool_name(tool) == tool, (
        f"a denied call to `{tool}` would be logged as `<redacted>`, which hides "
        "the only symptom this tightening can produce."
    )


@pytest.mark.parametrize("tool", RETIRED_FROM_PROMOTE)
def test_a_retired_tool_is_still_named_in_the_scope_denial(tool: str) -> None:
    """The same trap, at the second of the two places that can deny the call.

    `dream_project_scope` keeps its own tool vocabulary, deliberately not imported
    from `dream_capabilities` (see
    `test_production_policy_does_not_import_phase_capabilities`). Dropping a tool
    from `PROJECT_TOOL_POLICIES` makes the scope layer deny it `policy_missing` —
    fail-closed, and the right answer — but its `_safe_tool_name` is derived from
    that same table, so the denial would name `<redacted>`.

    Written before the policy entries were removed, so it fails on that removal
    rather than describing it afterwards.
    """
    from brain_v42.services.dream_project_scope import _safe_tool_name

    assert _safe_tool_name(tool) == tool, (
        f"a `policy_missing` denial for `{tool}` would be logged as `<redacted>`. "
        "Two layers deny this call; both must be able to say which call it was."
    )
