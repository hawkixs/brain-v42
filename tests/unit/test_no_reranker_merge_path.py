"""No module of the package may define or call `merge_features`.

Ruling 9e21964f (extension of d4648d84): nothing ever merges on a reranker score.
`FeatureDedupJob.merge_features` was the merge path reachable from the dedup
loop's reranker score; this scan keeps it from coming back unnoticed.
(ClusterGuard's own merge path is covered by its dedicated change.)
"""

from __future__ import annotations

import ast
from pathlib import Path

import brain_v42

_FORBIDDEN = "merge_features"


def _offences(tree: ast.AST) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == _FORBIDDEN:
            lines.append(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr == _FORBIDDEN:
            lines.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id == _FORBIDDEN:
            lines.append(node.lineno)
    return lines


def test_no_module_defines_or_calls_merge_features() -> None:
    package_root = Path(brain_v42.__file__).parent
    found = {
        f"{path.relative_to(package_root)}:{line}"
        for path in package_root.rglob("*.py")
        for line in _offences(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    }
    assert not found, f"a merge path reachable from a reranker score is back: {sorted(found)}"


def test_scan_detects_a_definition_and_a_call() -> None:
    tree = ast.parse("async def merge_features(): ...\nawait job.merge_features(s, a, b)\n")
    assert _offences(tree) == [1, 2]
