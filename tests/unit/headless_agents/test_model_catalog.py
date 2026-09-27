import re
from datetime import date
from pathlib import Path

import pytest

from headless_agents.model_catalog import CatalogueError, load_catalogue

GOOD = """\
schema = 1
[codex."gpt-6-sol"]
purpose = "Review and build."
tasks = [{kind = "design-review", effort = "high"}, {kind = "build"}]
cost = {kind = "subscription", windows = ["5h", "weekly"]}
pitfalls = ["Check findings."]
verified_at = 2026-09-27
source = "Brain decision ea4e57a1"
"""

PROVIDERS = (
    "claude",
    "codex",
    "agy",
    "opencode",
    "openrouter",
    "mistral",
    "nvidia",
    "openai-compat",
)
EFFORTS = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)


def _load(tmp_path: Path, text: str):
    path = tmp_path / "catalog.toml"
    path.write_text(text)
    return load_catalogue(path)


def _model(provider: str, effort: str | None = None) -> str:
    task = 'kind = "build"'
    if effort is not None:
        task += f', effort = "{effort}"'
    return (
        f'schema = 1\n[{provider}."model"]\n'
        'purpose = "Build."\n'
        f"tasks = [{{{task}}}]\n"
        'cost = {kind = "subscription"}\n'
        "verified_at = 2026-09-27\n"
        'source = "bench"\n'
    )


def _error(tmp_path: Path, text: str, key: str) -> None:
    with pytest.raises(CatalogueError, match=re.escape(key)):
        _load(tmp_path, text)


def test_valid_schema_keeps_exact_model_and_local_date(tmp_path: Path) -> None:
    result = _load(tmp_path, GOOD)
    assert result.warnings == ()
    assert result.entries[0].model == "gpt-6-sol"
    assert result.entries[0].tasks == (
        ("design-review", "high"),
        ("build", None),
    )
    assert result.entries[0].verified_at == date(2026, 9, 27)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_every_provider_accepts_a_model_table_without_effort(
    tmp_path: Path,
    provider: str,
) -> None:
    result = _load(tmp_path, _model(provider))
    assert result.warnings == ()
    assert result.entries[0].provider == provider
    assert result.entries[0].tasks == (("build", None),)


@pytest.mark.parametrize("provider", ("codex", "opencode"))
@pytest.mark.parametrize("effort", EFFORTS)
def test_allowed_provider_effort_is_kept_without_model_specific_check(
    tmp_path: Path,
    provider: str,
    effort: str,
) -> None:
    result = _load(tmp_path, _model(provider, effort))
    assert result.entries[0].tasks == (("build", effort),)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        pytest.param(GOOD.replace("schema = 1", "schema = 2"), "schema", id="schema-other-integer"),
        pytest.param(GOOD.replace("schema = 1", "schema = true"), "schema", id="schema-boolean"),
        pytest.param(GOOD.replace("schema = 1\n", ""), "schema", id="schema-missing"),
        pytest.param(GOOD.replace("[codex.", "[other."), "other", id="unknown-provider-table"),
        pytest.param(
            'schema = 1\ncodex = {"model" = "bad"}\n', "codex.model", id="model-value-not-table"
        ),
        pytest.param(GOOD.replace('"gpt-6-sol"', '""', 1), "codex.", id="empty-model-name"),
    ],
)
def test_structure_errors_name_the_key(
    tmp_path: Path,
    text: str,
    key: str,
) -> None:
    _error(tmp_path, text, key)


@pytest.mark.parametrize(
    ("provider", "effort"),
    [
        ("codex", "future"),
        ("opencode", "future"),
        ("agy", "high"),
        ("claude", "high"),
        ("openrouter", "high"),
        ("mistral", "high"),
        ("nvidia", "high"),
        ("openai-compat", "high"),
    ],
)
def test_effort_outside_provider_rule_names_its_key(
    tmp_path: Path,
    provider: str,
    effort: str,
) -> None:
    _error(tmp_path, _model(provider, effort), f"{provider}.model.tasks[0].effort")


def test_non_string_effort_names_its_key(tmp_path: Path) -> None:
    text = _model("opencode", "high").replace(
        'effort = "high"',
        "effort = 4",
    )
    _error(tmp_path, text, "opencode.model.tasks[0].effort")


@pytest.mark.parametrize(
    ("text", "key"),
    [
        pytest.param(
            GOOD.replace('purpose = "Review and build."\n', ""),
            "codex.gpt-6-sol.purpose",
            id="purpose-required",
        ),
        pytest.param(
            GOOD.replace('purpose = "Review and build."', "purpose = 4"),
            "codex.gpt-6-sol.purpose",
            id="purpose-string",
        ),
        pytest.param(
            GOOD.replace('purpose = "Review and build."', 'purpose = "  "'),
            "codex.gpt-6-sol.purpose",
            id="purpose-nonempty",
        ),
        pytest.param(
            GOOD.replace(
                'tasks = [{kind = "design-review", effort = "high"}, {kind = "build"}]\n', ""
            ),
            "codex.gpt-6-sol.tasks",
            id="tasks-required",
        ),
        pytest.param(
            GOOD.replace(
                'tasks = [{kind = "design-review", effort = "high"}, {kind = "build"}]',
                "tasks = []",
            ),
            "codex.gpt-6-sol.tasks",
            id="tasks-nonempty",
        ),
        pytest.param(
            GOOD.replace('{kind = "build"}', '"build"'),
            "codex.gpt-6-sol.tasks[1]",
            id="task-must-be-table",
        ),
        pytest.param(
            'schema = 1\n[codex."gpt-6-sol"]\n'
            'purpose = "Review and build."\n'
            'cost = {kind = "subscription", windows = ["5h", "weekly"]}\n'
            'pitfalls = ["Check findings."]\n'
            "verified_at = 2026-09-27\n"
            'source = "Brain decision ea4e57a1"\n'
            '\n[[codex."gpt-6-sol".tasks]]\n'
            'kind = "design-review"\n'
            'effort = "high"\n'
            '\n[[codex."gpt-6-sol".tasks]]\n'
            'kind = "build"\n',
            "codex.gpt-6-sol.tasks",
            id="tasks-must-be-inline-array-not-array-of-tables",
        ),
        pytest.param(
            GOOD.replace('{kind = "build"}', "{}"),
            "codex.gpt-6-sol.tasks[1].kind",
            id="kind-required",
        ),
        pytest.param(
            GOOD.replace('kind = "build"', "kind = 4"),
            "codex.gpt-6-sol.tasks[1].kind",
            id="kind-string",
        ),
        pytest.param(
            GOOD.replace('kind = "build"', 'kind = "invented"'),
            "codex.gpt-6-sol.tasks[1].kind",
            id="kind-closed-list",
        ),
        pytest.param(
            GOOD.replace('kind = "build"', 'kind = "design-review"'),
            "codex.gpt-6-sol.tasks[1].kind",
            id="kind-unique",
        ),
        pytest.param(
            GOOD.replace('cost = {kind = "subscription", windows = ["5h", "weekly"]}\n', ""),
            "codex.gpt-6-sol.cost",
            id="cost-required",
        ),
        pytest.param(
            GOOD.replace(
                'cost = {kind = "subscription", windows = ["5h", "weekly"]}',
                'cost = "subscription"',
            ),
            "codex.gpt-6-sol.cost",
            id="cost-table",
        ),
        pytest.param(
            'schema = 1\n[codex."gpt-6-sol"]\n'
            'purpose = "Review and build."\n'
            'tasks = [{kind = "design-review", effort = "high"}, {kind = "build"}]\n'
            'pitfalls = ["Check findings."]\n'
            "verified_at = 2026-09-27\n"
            'source = "Brain decision ea4e57a1"\n'
            '\n[codex."gpt-6-sol".cost]\n'
            'kind = "subscription"\n'
            'windows = ["5h", "weekly"]\n',
            "codex.gpt-6-sol.cost",
            id="cost-must-be-inline-table-not-standalone-table",
        ),
        pytest.param(
            GOOD.replace('kind = "subscription"', 'kind = "future"'),
            "codex.gpt-6-sol.cost.kind",
            id="cost-kind-closed-list",
        ),
        pytest.param(
            GOOD.replace('windows = ["5h", "weekly"]', 'windows = ["yearly"]'),
            "codex.gpt-6-sol.cost.windows[0]",
            id="window-closed-list",
        ),
        pytest.param(
            GOOD.replace('windows = ["5h", "weekly"]', 'windows = "5h"'),
            "codex.gpt-6-sol.cost.windows",
            id="windows-array",
        ),
        pytest.param(
            GOOD.replace('windows = ["5h", "weekly"]', 'windows = ["5h", 4]'),
            "codex.gpt-6-sol.cost.windows[1]",
            id="window-string",
        ),
        pytest.param(
            GOOD.replace('pitfalls = ["Check findings."]', "pitfalls = [4]"),
            "codex.gpt-6-sol.pitfalls[0]",
            id="pitfall-string",
        ),
        pytest.param(
            GOOD.replace("verified_at = 2026-09-27", 'verified_at = "2026-09-27"'),
            "codex.gpt-6-sol.verified_at",
            id="quoted-date",
        ),
        pytest.param(
            GOOD.replace("verified_at = 2026-09-27", "verified_at = 2026-09-27T00:00:00"),
            "codex.gpt-6-sol.verified_at",
            id="date-time",
        ),
        pytest.param(
            GOOD.replace('source = "Brain decision ea4e57a1"\n', ""),
            "codex.gpt-6-sol.source",
            id="source-required",
        ),
    ],
)
def test_field_error_names_its_key(
    tmp_path: Path,
    text: str,
    key: str,
) -> None:
    _error(tmp_path, text, key)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        pytest.param(
            GOOD.replace("schema = 1\n", 'schema = 1\nfuture = "top"\n'),
            "future",
            id="unknown-top-level-scalar",
        ),
        pytest.param(
            GOOD.replace("schema = 1\n", "schema = 1\nagy = 4\n"),
            "agy",
            id="known-provider-name-with-scalar-value",
        ),
        pytest.param(
            GOOD.replace("tasks = [", 'extra = "model"\ntasks = ['),
            "codex.gpt-6-sol.extra",
            id="unknown-model-field",
        ),
        pytest.param(
            GOOD.replace('effort = "high"', 'effort = "high", future = true'),
            "codex.gpt-6-sol.tasks[0].future",
            id="unknown-task-field",
        ),
        pytest.param(
            GOOD.replace('kind = "subscription"', 'kind = "subscription", future = "cost"'),
            "codex.gpt-6-sol.cost.future",
            id="unknown-cost-field",
        ),
    ],
)
def test_only_permitted_unknown_keys_warn(
    tmp_path: Path,
    text: str,
    key: str,
) -> None:
    result = _load(tmp_path, text)
    assert result.warnings == (key,)


def test_warning_does_not_hide_invalid_known_field(tmp_path: Path) -> None:
    text = GOOD.replace("schema = 1\n", 'schema = 1\nfuture = "top"\n')
    text = text.replace('purpose = "Review and build."', "purpose = 4")
    _error(tmp_path, text, "codex.gpt-6-sol.purpose")


def test_missing_catalogue_names_its_path(tmp_path: Path) -> None:
    path = tmp_path / "catalog.toml"
    with pytest.raises(CatalogueError, match="catalog.toml"):
        load_catalogue(path)
