"""Static regression guard for caller-controlled content in scoped log events."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]

FORBIDDEN_FIELDS_BY_EVENT = {
    "learning.created": {"topic"},
    "learning.semantic_search.no_embedding_svc": {"query"},
    "snippet_service.create": {"language", "title"},
    "snippet_service.list_snippets": {"language"},
    "snippet_service.search": {"query"},
    "snippet_service.semantic_search": {"language", "query"},
    "runbook_service.create": {"id", "title"},
    "mcp.brain_create_runbook": {"runbook_id", "title"},
    "mcp.brain_save_snippet": {"snippet_id", "title"},
    "brain_search.unknown_types_ignored": {"types"},
    "mcp.brain_search.grouped": {"query"},
    "mcp.brain_search": {"query"},
    "mcp.brain_get_neighbors": {"entity_id", "rel_types"},
    "mcp.brain_graph_path": {"rel_types", "source_id", "target_id"},
    "graph.invalid_domain": {"name"},
    "batching_reranker.flush": {"query_prefix"},
    "batching_reranker.inner_error": {"error", "query_prefix"},
    "batching_reranker.score_length_mismatch": {"query_prefix"},
    "brain_service.fan_out.service_error": {"error"},
}


def test_scoped_log_events_never_attach_caller_controlled_values() -> None:
    violations: list[str] = []
    for path in (ROOT / "src" / "brain_v42").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            event_arg = node.args[0]
            if not isinstance(event_arg, ast.Constant) or not isinstance(event_arg.value, str):
                continue
            forbidden = FORBIDDEN_FIELDS_BY_EVENT.get(event_arg.value)
            if forbidden is None:
                continue
            actual = {keyword.arg for keyword in node.keywords if keyword.arg is not None}
            leaked = sorted(actual & forbidden)
            if leaked:
                relative = path.relative_to(ROOT)
                violations.append(
                    f"{relative}:{node.lineno} {event_arg.value}: {', '.join(leaked)}"
                )

    assert violations == [], "caller-controlled log fields remain:\n" + "\n".join(violations)
