"""Structural witness: ClusterGuard links on cosine only and never merges.

Operator ruling d4648d84 (2026-10-06): ClusterGuard stops using the reranker,
and nothing ever merges on a reranker score. Behavioural tests prove the
outcomes; this one proves the machinery is gone, so it cannot be wired back in
silently by a later edit.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from brain_v42.services import cluster_guard
from brain_v42.services.cluster_guard import ClusterGuard

_TREE = ast.parse(Path(inspect.getsourcefile(cluster_guard) or "").read_text())


def _identifiers() -> set[str]:
    """Every name the module code binds or references, docstrings excluded."""
    found: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.arg):
            found.add(node.arg)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
            found.update(alias.asname or alias.name for alias in node.names)
    return found


def test_cluster_guard_defines_no_merge_or_grey_zone_handler() -> None:
    assert not {"_merge_into", "_handle_grey_zone"} & _identifiers()


def test_cluster_guard_references_no_reranker() -> None:
    assert [name for name in _identifiers() if "rerank" in name.lower()] == []


def test_cluster_guard_constructor_takes_no_reranker() -> None:
    assert "reranker" not in inspect.signature(ClusterGuard.__init__).parameters


def test_cluster_guard_has_no_merged_action() -> None:
    literals = {node.value for node in ast.walk(_TREE) if isinstance(node, ast.Constant)}
    assert "merged" not in literals
