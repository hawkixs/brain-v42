"""Every knowledge UPDATE and scoped mutation entry point needs a classification."""

import ast
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3] / "src" / "brain_v42"
TABLES = {
    "decisions",
    "learnings",
    "snippets",
    "runbooks",
    "adrs",
    "indexed_plans",
    "table",
    "self.table",
}
GUARDED = {
    ("repositories/pg_base.py", "update._execute"): 1,
    ("repositories/pg_decision.py", "update"): 1,
    ("repositories/pg_decision.py", "supersede"): 1,
    ("repositories/pg_learning.py", "update"): 1,
    ("repositories/pg_learning.py", "validate"): 1,
    ("repositories/pg_snippet.py", "update"): 1,
    ("repositories/pg_snippet.py", "increment_use._execute"): 1,
    ("repositories/pg_runbook.py", "update"): 1,
    ("repositories/pg_runbook.py", "record_execution._execute"): 1,
    ("repositories/pg_adr.py", "update"): 1,
    ("mcp/tools/decay_tools.py", "register_decay_tools.brain_refresh_entity"): 1,
}
EXEMPT = {
    ("services/entity_maintenance_service.py", "refresh"): (
        1,
        "internal maintenance; MCP refresh owns its guarded transaction",
    ),
    ("services/consolidation.py", "_update_merge_pair"): (
        3,
        "admin consolidation, unreachable from scoped knowledge tools",
    ),
    ("services/decay_flusher.py", "_update_entities_batch"): (
        2,
        "deferred counters/freshness only, no caller content or request ContextVar",
    ),
    ("repositories/pg_knowledge_lifecycle.py", "invalidate_run"): (
        1,
        "Dream run ledger, not a knowledge row",
    ),
    ("repositories/pg_knowledge_lifecycle.py", "record_run"): (
        1,
        "Dream run ledger, not a knowledge row",
    ),
    ("repositories/pg_knowledge_lifecycle.py", "set_count_key"): (
        1,
        "Dream run ledger, not a knowledge row",
    ),
    ("repositories/pg_knowledge_lifecycle.py", "checkpoint_request"): (
        1,
        "Dream run ledger, not a knowledge row",
    ),
    ("repositories/promotion.py", "stamp_learning"): (
        1,
        "admin promotion, unreachable from the six tools",
    ),
    ("repositories/pg_base.py", "set_embedding_if_current._execute"): (
        1,
        "background embedding backfill, no caller content",
    ),
    ("repositories/pg_decision.py", "delete"): (
        2,
        "admin delete, unreachable from scoped knowledge tools",
    ),
    ("scripts/scrub_xml_tool_call_leak.py", "_scrub_table"): (1, "operator repair script"),
    ("maintenance/merge_chain_repair.py", "repair"): (1, "operator repair, not MCP"),
}


def test_every_knowledge_update_has_an_explicit_guard_or_exemption():
    found = Counter()
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if (
                not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Attribute)
                or node.func.attr != "update"
            ):
                continue
            receiver = ast.unparse(node.func.value)
            target = ast.unparse(node.args[0]) if receiver == "sa" and node.args else receiver
            if target not in TABLES:
                continue
            names = []
            parent = parents.get(node)
            while parent is not None:
                if isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef):
                    names.append(parent.name)
                parent = parents.get(parent)
            key = (str(path.relative_to(ROOT)), ".".join(reversed(names)))
            found[key] += 1
    assert found == Counter({**GUARDED, **{key: value[0] for key, value in EXEMPT.items()}})


def test_guarded_writers_lock_and_predicate_in_the_same_function():
    for (filename, function), _ in GUARDED.items():
        tree = ast.parse((ROOT / filename).read_text())
        name = function.split(".")[-1]
        candidates = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name
        ]
        assert any(
            "lock_checked_owner" in ast.unparse(node)
            and "checked_project_clause" in ast.unparse(node)
            for node in candidates
        ), (filename, function)
