"""Snapshot published knowledge tool input schemas.

To snapshot an older ref, run:
PYTHONPATH=<checkout of REF>/src python scripts/snapshot_knowledge_tool_schemas.py --source-ref REF
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from fastmcp import FastMCP

from brain_v42.mcp.tools.brain_tools import register_tools
from brain_v42.mcp.tools.crud_tools import register_crud_tools


async def _published_input_schemas() -> dict[str, dict[str, Any]]:
    service = MagicMock()
    server = FastMCP("knowledge-tool-schema-snapshot")
    register_tools(
        server,
        decision_svc=service,
        learning_svc=service,
        snippet_svc=service,
        runbook_svc=service,
        adr_svc=service,
        project_context_svc=service,
        brain_svc=service,
        roadmap_svc=service,
        graph_svc=service,
    )
    register_crud_tools(
        server,
        decision_svc=service,
        learning_svc=service,
        snippet_svc=service,
        runbook_svc=service,
        adr_svc=service,
        session_factory=service,
    )
    return {tool.name: tool.parameters for tool in await server._list_tools()}


def published_input_schemas() -> dict[str, dict[str, Any]]:
    """Build and return the schemas clients receive for registered tools."""
    return asyncio.run(_published_input_schemas())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-ref", required=True, help="Provenance label for this snapshot")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("tests/fixtures/mcp_knowledge_tool_input_schemas_base.json"),
    )
    args = parser.parse_args()
    payload = {"source_ref": args.source_ref, "tools": published_input_schemas()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, sort_keys=True, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
