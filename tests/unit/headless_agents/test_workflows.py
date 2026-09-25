"""workflows.toml: named instances of the two shapes, validated before anything runs (spec 0.5.0 §3.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from headless_agents.roles import load_roles
from headless_agents.workflows import Workflow, WorkflowsError, load_workflows

ROLES = """\
[implementer]
chain = ["opencode:oc-model", "codex:gpt-6-luna"]
write = true

[reviewer-codex]
provider = "codex"

[reviewer-agy]
provider = "agy"

[judge]
provider = "claude"

[scribe]
provider = "claude"
write = true
"""


def _load(tmp_path: Path, text: str) -> dict[str, Workflow]:
    roles_path = tmp_path / "roles.toml"
    roles_path.write_text(ROLES)
    path = tmp_path / "workflows.toml"
    path.write_text(text)
    return load_workflows(path, roles=load_roles(roles_path, mcp_profiles={}))


def test_an_implement_and_a_review_workflow_load(tmp_path: Path) -> None:
    workflows = _load(
        tmp_path,
        '[build]\nshape = "implement"\nimplement = "implementer"\n\n'
        '[multi-review]\nshape = "review"\nreview = ["reviewer-codex", "reviewer-agy"]\n'
        'judge = "judge"\n\n'
        '[quick-review]\nshape = "review"\nreview = "reviewer-codex"\n',
    )
    assert workflows["build"] == Workflow(
        name="build", shape="implement", implement="implementer", review=(), judge=None
    )
    assert workflows["multi-review"].review == ("reviewer-codex", "reviewer-agy")
    assert workflows["multi-review"].judge == "judge"
    assert workflows["quick-review"] == Workflow(
        name="quick-review", shape="review", implement=None, review=("reviewer-codex",), judge=None
    )


def test_a_slot_may_name_a_providers_implicit_role(tmp_path: Path) -> None:
    workflows = _load(tmp_path, '[peek]\nshape = "review"\nreview = "codex"\n')
    assert workflows["peek"].review == ("codex",)


def test_slot_roles_lists_the_reviewers_then_the_judge(tmp_path: Path) -> None:
    workflows = _load(
        tmp_path,
        '[m]\nshape = "review"\nreview = ["reviewer-codex", "reviewer-agy"]\njudge = "judge"\n',
    )
    assert workflows["m"].slot_roles() == (
        ("review", "reviewer-codex"),
        ("review", "reviewer-agy"),
        ("judge", "judge"),
    )


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ('build = "implementer"\n', "a workflow must be a table"),
        ('[Build]\nshape = "implement"\nimplement = "implementer"\n', "invalid workflow name"),
        ('[codex]\nshape = "implement"\nimplement = "implementer"\n', "collides with a provider"),
        ('[judge]\nshape = "review"\nreview = "codex"\n', "collides with a role of roles.toml"),
        ('[build]\nimplement = "implementer"\n', "the shape is missing"),
        ('[build]\nshape = "pipeline"\n', "unknown shape 'pipeline'"),
        ("[build]\nshape = 3\n", "unknown shape 3"),
        (
            '[build]\nshape = "implement"\nimplement = "implementer"\njudge = "judge"\n',
            "unknown slot 'judge' for the shape implement",
        ),
        (
            '[r]\nshape = "review"\nreview = "codex"\nimplement = "implementer"\n',
            "unknown slot 'implement' for the shape review",
        ),
        ('[build]\nshape = "implement"\n', "the implement slot is missing"),
        (
            '[build]\nshape = "implement"\nimplement = ["implementer"]\n',
            "implement must name a role",
        ),
        (
            '[build]\nshape = "implement"\nimplement = "nobody"\n',
            "implement names unknown role 'nobody'",
        ),
        (
            '[build]\nshape = "implement"\nimplement = "reviewer-codex"\n',
            "the implement slot needs a role with write = true; reviewer-codex does not write",
        ),
        (
            '[build]\nshape = "implement"\nimplement = "codex"\n',
            "the implement slot needs a role with write = true; codex does not write",
        ),
        ('[r]\nshape = "review"\n', "the review slot is missing"),
        ('[r]\nshape = "review"\nreview = []\n', "review must name one role or a non-empty list"),
        ('[r]\nshape = "review"\nreview = 7\n', "review must name one role or a non-empty list"),
        ('[r]\nshape = "review"\nreview = ["codex", 7]\n', "review must name a role"),
        ('[r]\nshape = "review"\nreview = "nobody"\n', "review names unknown role 'nobody'"),
        (
            '[r]\nshape = "review"\nreview = ["codex", "codex"]\njudge = "judge"\n',
            "codex appears twice in review",
        ),
        (
            '[r]\nshape = "review"\nreview = "scribe"\n',
            "the review slot needs roles with write = false; scribe writes",
        ),
        (
            '[r]\nshape = "review"\nreview = "codex"\njudge = "nobody"\n',
            "judge names unknown role 'nobody'",
        ),
        (
            '[r]\nshape = "review"\nreview = "codex"\njudge = "scribe"\n',
            "the judge slot needs a role with write = false; scribe writes",
        ),
        (
            '[r]\nshape = "review"\nreview = ["codex", "agy"]\n',
            "a judge is required with two reviewers or more",
        ),
    ],
)
def test_every_refusal_names_the_file_the_entry_and_the_rule(
    tmp_path: Path, text: str, rule: str
) -> None:
    with pytest.raises(WorkflowsError) as refused:
        _load(tmp_path, text)
    message = str(refused.value)
    assert message.startswith(f"{tmp_path / 'workflows.toml'}: [")
    assert rule in message


def test_no_file_is_no_workflow(tmp_path: Path) -> None:
    assert load_workflows(None, roles={}) == {}
    assert load_workflows(tmp_path / "workflows.toml", roles={}) == {}


def test_a_file_that_does_not_parse_is_refused_naming_it(tmp_path: Path) -> None:
    path = tmp_path / "workflows.toml"
    path.write_text("[build\n")
    with pytest.raises(WorkflowsError, match="workflows.toml"):
        load_workflows(path, roles={})


def test_a_file_nested_too_deeply_is_refused_not_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "workflows.toml"
    path.write_text("x = " + "[" * 2000 + "]" * 2000 + "\n")
    with pytest.raises(WorkflowsError, match="nested too deeply"):
        load_workflows(path, roles={})
