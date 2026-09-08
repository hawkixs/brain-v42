"""The bind/claim CAS token must be named, and its churn explained, on three surfaces.

Ticket 00f4e954, measured 2026-09-08 during the first real delivery through the
observable workflow: `brain_delivery_bind_pr` compares `expected_workflow_version`
to `delivery_workflows.row_version`, the view carries that value as
`assessment.assessment_version` (delivery_evaluator.py <- pg_delivery.py), and the
observer advances it on every poll -- about every 60 seconds -- even when the
context confirmation changed nothing. A caller who reads the view and binds a
minute later gets `revision_conflict`, which is a stale read, not a real
conflict. Two operators hit it on the same day (canary-view-binding-cas.json,
then PR #129), and runbook step 5 named a field that does not exist under that
name.

The CAS stays strict by design (commit e06bb044). What this test pins is the
prose: the two tool descriptions agents read, the MCP_TOOLS page, and runbook
step 5 must all name `assessment_version` as the token, say it moves at every
observer poll, and say that `revision_conflict` means re-read and retry. The
poll cadence is read from `DeliverySettings`, not copied, so a changed default
cannot leave a stale number in the docs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from brain_v42.delivery_config import DeliverySettings

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_TOOLS = (REPO_ROOT / "docs" / "MCP_TOOLS.md").read_text(encoding="utf-8")
RUNBOOK = (
    REPO_ROOT / "docs" / "runbooks" / "2026-09-07-observable-delivery-workflows.md"
).read_text(encoding="utf-8")
TOOLS_SOURCE = (REPO_ROOT / "src" / "brain_v42" / "mcp" / "tools" / "delivery_tools.py").read_text(
    encoding="utf-8"
)

TOKEN_SENTENCE = "`expected_workflow_version` is `view.assessment.assessment_version`"
CHURN_SENTENCE = "advances on every observer poll"
RETRY_SENTENCE = "on `revision_conflict` re-read the view and retry"


def _tool_docstring(name: str) -> str:
    match = re.search(rf'async def {name}\(.*?"""(.*?)"""', TOOLS_SOURCE, re.DOTALL)
    assert match is not None, f"`{name}` has no docstring in delivery_tools.py"
    return match.group(1)


@pytest.mark.parametrize("tool", ["brain_delivery_bind_pr", "brain_delivery_claim"])
def test_tool_description_names_the_token_and_its_churn(tool: str) -> None:
    """The docstring is the MCP description: it is what an agent reads before calling."""
    docstring = _tool_docstring(tool)
    assert "assessment_version" in docstring, (
        f"`{tool}` does not tell the caller that `expected_workflow_version` is "
        "`view.assessment.assessment_version`."
    )
    assert "observer poll" in docstring, (
        f"`{tool}` does not say the token advances on every observer poll -- the "
        "caller cannot know a minute-old read is already stale."
    )
    assert "revision_conflict" in docstring, (
        f"`{tool}` does not say that `revision_conflict` means re-read and retry."
    )


def test_mcp_tools_page_explains_the_token_churn_and_the_retry() -> None:
    assert TOKEN_SENTENCE in MCP_TOOLS, (
        "docs/MCP_TOOLS.md must name `view.assessment.assessment_version` as the "
        "value of `expected_workflow_version`, verbatim."
    )
    assert CHURN_SENTENCE in MCP_TOOLS, (
        "docs/MCP_TOOLS.md must say the token advances on every observer poll."
    )
    default = DeliverySettings.model_fields["poll_seconds"].default
    assert f"every {default} seconds" in MCP_TOOLS, (
        f"docs/MCP_TOOLS.md must name the default poll cadence ({default} seconds), "
        "read from DeliverySettings.poll_seconds -- not a copied number."
    )
    assert RETRY_SENTENCE in MCP_TOOLS, (
        "docs/MCP_TOOLS.md must say that `revision_conflict` on that token means "
        "re-read the view and retry."
    )


def _runbook_step_five() -> str:
    start = RUNBOOK.index("5. Read `brain_delivery_get`")
    end = RUNBOOK.index("\n6. ", start)
    return RUNBOOK[start:end]


def test_runbook_step_five_names_the_real_field() -> None:
    step = _runbook_step_five()
    assert "current assessment `expected_workflow_version`" not in step, (
        "runbook step 5 still names a field called `expected_workflow_version` on "
        "the assessment -- no such field exists under that name."
    )
    assert "`view.assessment.assessment_version`" in step, (
        "runbook step 5 must name `view.assessment.assessment_version` as the value "
        "to pass as `expected_workflow_version`."
    )
    assert "revision_conflict" in step, (
        "runbook step 5 must tell the operator what to do on `revision_conflict`: "
        "read the view again and retry at once."
    )
