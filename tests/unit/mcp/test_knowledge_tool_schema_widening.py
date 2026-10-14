"""Guard the published knowledge tool schemas against unapproved narrowing or drift."""

from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/fixtures/mcp_knowledge_tool_input_schemas_base.json"
SCRIPT_PATH = ROOT / "scripts/snapshot_knowledge_tool_schemas.py"
_SPEC = importlib.util.spec_from_file_location("schema_snapshot", SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_SCRIPT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SCRIPT)


def _load_base_schemas() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["tools"]


WIDENED: dict[str, dict[str, str]] = {
    "brain_create_runbook": {
        "prerequisites": "description",
        "tags": "description",
        "steps": "steps",
        "rollback_steps": "steps",
    },
    "brain_promote_runbook": {
        "prerequisites": "description",
        "tags": "description",
        "steps": "steps",
        "rollback_steps": "steps",
    },
    "brain_get_neighbors": {"rel_types": "description"},
    "brain_graph_path": {"rel_types": "description"},
    "brain_learn": {"tags": "description"},
    "brain_list": {"tags": "description"},
    "brain_promote_adr": {"tags": "description"},
    "brain_log_decision": {"alternatives": "description", "tags": "description"},
    "brain_supersede_decision": {"alternatives": "description", "tags": "description"},
    "brain_propose_adr": {"tags": "description", "alternatives": "new_optional"},
    "brain_save_snippet": {"dependencies": "description", "tags": "description"},
    "brain_search": {"tags": "description", "types": "description"},
    "brain_set_project_context": {
        "blockers": "description",
        "databases": "description",
        "frameworks": "description",
        "languages": "description",
        "plan_scan_paths": "description",
        "related_projects": "description",
    },
    "brain_update_project_focus": {"blockers": "description", "unpin": "description"},
}


def _without_descriptions(node: Any) -> Any:
    """Remove schema annotations while preserving parameter names in properties."""
    if isinstance(node, dict):
        return {
            key: (
                {name: _without_descriptions(value) for name, value in node[key].items()}
                if key == "properties" and isinstance(node[key], dict)
                else _without_descriptions(value)
            )
            for key, value in node.items()
            if key != "description"
        }
    if isinstance(node, list):
        return [_without_descriptions(value) for value in node]
    return node


def _array_branch(schema: dict[str, Any], label: str) -> tuple[dict[str, Any], list[Any]]:
    """Return the array schema and any non-array branches for a parameter."""
    if "items" in schema:
        return schema, []
    branches = schema.get("anyOf", [])
    arrays = [branch for branch in branches if branch.get("type") == "array"]
    assert len(arrays) == 1, f"{label} must have one array branch"
    return arrays[0], [branch for branch in branches if branch is not arrays[0]]


def _check_steps_widening(before: dict[str, Any], after: dict[str, Any], label: str) -> None:
    """Check that a steps schema contains only its declared item widening."""
    old_array, old_other = _array_branch(_without_descriptions(before), label)
    new_array, new_other = _array_branch(_without_descriptions(after), label)
    assert {key: value for key, value in old_array.items() if key != "items"} == {
        key: value for key, value in new_array.items() if key != "items"
    }, f"{label} array branch changed beyond items"
    assert old_other == new_other, f"{label} non-array branches changed"
    if "anyOf" in before:
        old_parameter = _without_descriptions(before)
        new_parameter = _without_descriptions(after)
        assert {key: value for key, value in old_parameter.items() if key != "anyOf"} == {
            key: value for key, value in new_parameter.items() if key != "anyOf"
        }, f"{label} parameter changed beyond anyOf"
    old_items = old_array["items"]
    new_items = new_array["items"]
    expected = [_without_descriptions(old_items), {"maxLength": 2000, "type": "string"}]
    assert new_items == {"anyOf": expected}, f"{label} items do not match the declared widening"


def test_steps_widening_rejects_array_keyword_drift(
    schemas: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    base, head = schemas
    before = base["brain_create_runbook"]["properties"]["steps"]
    after = head["brain_create_runbook"]["properties"]["steps"]
    _check_steps_widening(before, after, "brain_create_runbook.steps")

    altered = deepcopy(after)
    array, _ = _array_branch(altered, "brain_create_runbook.steps")
    array["maxItems"] = 101
    with pytest.raises(AssertionError):
        _check_steps_widening(before, altered, "brain_create_runbook.steps")


def test_without_descriptions_preserves_description_property() -> None:
    schema = {
        "properties": {"description": {"type": "string", "description": "x"}},
        "description": "y",
    }
    assert _without_descriptions(schema) == {"properties": {"description": {"type": "string"}}}


@pytest.fixture(scope="module")
def schemas() -> tuple[dict[str, Any], dict[str, Any]]:
    base = _load_base_schemas()
    return base, _SCRIPT.published_input_schemas()


def test_every_base_tool_is_still_published(schemas: tuple[dict[str, Any], dict[str, Any]]) -> None:
    base, head = schemas
    missing = sorted(set(base) - set(head))
    assert not missing, f"Base tools missing at head: {missing}"


@pytest.mark.parametrize("tool_name", sorted(set(_load_base_schemas()) - set(WIDENED)))
def test_unlisted_tool_schemas_are_unchanged(
    schemas: tuple[dict[str, Any], dict[str, Any]], tool_name: str
) -> None:
    base, head = schemas
    assert head[tool_name] == base[tool_name], f"{tool_name} schema changed"


@pytest.mark.parametrize("tool_name", sorted(WIDENED))
def test_widened_tools_preserve_required_and_base_properties(
    schemas: tuple[dict[str, Any], dict[str, Any]], tool_name: str
) -> None:
    base, head = schemas
    before, after = base[tool_name], head[tool_name]
    assert after.get("required", []) == before.get("required", []), f"{tool_name} required changed"
    old_properties = before.get("properties", {})
    new_properties = after.get("properties", {})
    for name in old_properties.keys() - new_properties.keys():
        pytest.fail(f"{tool_name}.{name} was removed")
    for name in new_properties.keys() - old_properties.keys():
        assert WIDENED[tool_name].get(name) == "new_optional", (
            f"{tool_name}.{name} is undeclared new property"
        )
        assert name not in after.get("required", []), f"{tool_name}.{name} must be optional"
        assert "default" in new_properties[name] and new_properties[name]["default"] is None, (
            f"{tool_name}.{name} must default to null"
        )
    for name, old_schema in old_properties.items():
        if name not in WIDENED[tool_name]:
            assert new_properties[name] == old_schema, (
                f"{tool_name}.{name} changed without declaration"
            )


@pytest.mark.parametrize(
    ("tool_name", "parameter", "kind"),
    sorted(
        (tool, param, kind) for tool, params in WIDENED.items() for param, kind in params.items()
    ),
)
def test_declared_schema_widenings_hold(
    schemas: tuple[dict[str, Any], dict[str, Any]], tool_name: str, parameter: str, kind: str
) -> None:
    base, head = schemas
    before = base[tool_name]["properties"].get(parameter)
    after = head[tool_name]["properties"].get(parameter)
    assert after is not None, f"{tool_name}.{parameter} missing at head"
    if kind == "new_optional":
        assert before is None, f"{tool_name}.{parameter} already existed in base"
        assert parameter not in head[tool_name].get("required", []), (
            f"{tool_name}.{parameter} must be optional"
        )
        assert "default" in after and after["default"] is None, (
            f"{tool_name}.{parameter} must default to null"
        )
    elif kind == "description":
        assert before is not None, f"{tool_name}.{parameter} missing from base"
        assert _without_descriptions(after) == _without_descriptions(before), (
            f"{tool_name}.{parameter} changed beyond descriptions"
        )
        assert after != before, f"{tool_name}.{parameter} has no description change"
    elif kind == "steps":
        assert before is not None, f"{tool_name}.{parameter} missing from base"
        _check_steps_widening(before, after, f"{tool_name}.{parameter}")
