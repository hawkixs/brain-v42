"""Test that all registered MCP tools have version metadata."""

from __future__ import annotations

import ast
import pathlib

TOOL_FILES = [
    pathlib.Path("src/brain_v42/mcp/tools/brain_tools.py"),
    pathlib.Path("src/brain_v42/mcp/tools/snippet_tools.py"),
    pathlib.Path("src/brain_v42/mcp/tools/runbook_tools.py"),
    pathlib.Path("src/brain_v42/mcp/tools/project_context_tools.py"),
]


def test_all_tool_decorators_have_version() -> None:
    """Every @mcp.tool() decorator must include version='1.0'."""
    missing_version: list[str] = []
    for path in TOOL_FILES:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                func = dec.func
                if not (isinstance(func, ast.Attribute) and func.attr == "tool"):
                    continue
                has_version = any(kw.arg == "version" for kw in dec.keywords)
                if not has_version:
                    missing_version.append(f"{path}:{node.lineno} ({node.name})")
    assert missing_version == [], f"Tools missing version='1.0': {missing_version}"
