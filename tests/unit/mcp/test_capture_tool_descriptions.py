"""Published descriptions route callers to the right knowledge capture tool."""

from __future__ import annotations

from tests.unit.mcp.test_knowledge_tool_contract import _registered_knowledge_server

CAPTURE_RULE = (
    "Which capture tool: a choice between options -> brain_log_decision; a procedure -> "
    "brain_create_runbook; code -> brain_save_snippet; durable architecture -> "
    "brain_propose_adr; anything else -> brain_learn."
)


async def _capture_descriptions() -> dict[str, str]:
    server = _registered_knowledge_server()
    tools = {tool.name: tool for tool in await server.list_tools()}
    names = (
        "brain_learn",
        "brain_log_decision",
        "brain_propose_adr",
        "brain_create_runbook",
        "brain_save_snippet",
    )
    return {name: tools[name].description or "" for name in names}


async def test_capture_tool_descriptions_publish_the_rule_once() -> None:
    descriptions = await _capture_descriptions()
    for description in descriptions.values():
        assert description.count(CAPTURE_RULE) == 1


async def test_capture_tool_descriptions_have_distinct_concise_first_lines() -> None:
    descriptions = await _capture_descriptions()
    first_lines = [description.splitlines()[0] for description in descriptions.values()]
    assert len(set(first_lines)) == len(descriptions)
    assert all(len(line) <= 160 for line in first_lines)
    assert all(not line.startswith(CAPTURE_RULE) for line in first_lines)


async def test_capture_tool_descriptions_omit_maintainer_history() -> None:
    descriptions = await _capture_descriptions()
    for description in descriptions.values():
        assert "Until 20" not in description


async def test_brain_learn_first_line_marks_last_resort() -> None:
    descriptions = await _capture_descriptions()
    assert "last resort" in descriptions["brain_learn"].splitlines()[0].lower()


async def test_capture_descriptions_keep_caller_useful_details() -> None:
    descriptions = await _capture_descriptions()
    assert "source_type:" in descriptions["brain_learn"]
    assert "confidence:" in descriptions["brain_learn"]
    assert "``intention`` is the embedded field" in descriptions["brain_save_snippet"]
