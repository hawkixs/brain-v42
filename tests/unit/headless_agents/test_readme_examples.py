"""The README's ``python`` examples must import against the real package.

Lot-1 review, minor: "imports of the README example" -- a prose fix can drift
from the code it advertises without any gate noticing, because nothing runs
it. This extracts every ```python fenced block from README.md and, per block,
(1) compiles it -- syntax only, since the blocks are progressive prose and a
later one freely reuses names an earlier one bound, so whole-block execution
is out of scope -- and (2) resolves every ``import`` / ``from ... import``
statement it contains against the installed package: the module must import,
and each named attribute must exist on it.

Deliberately not executed: several blocks spawn real CLI agents (``claude``,
``codex``), and this is a documentation guard, not a live test.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
README_PATH = REPO_ROOT / "packages" / "headless-agents" / "README.md"

_PYTHON_FENCE = re.compile(r"```python\n(.*?)```", re.DOTALL)


def _python_blocks(text: str) -> list[str]:
    return _PYTHON_FENCE.findall(text)


def _check_block(source: str, index: int) -> list[str]:
    """Return one failure message per broken import/attribute in this block."""
    failures: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"block {index}: does not compile: {exc}"]
    compile(source, f"<readme-block-{index}>", "exec")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                try:
                    importlib.import_module(alias.name)
                except ImportError as exc:
                    failures.append(f"block {index}: import {alias.name!r} failed: {exc}")
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                continue  # a relative import makes no sense lifted out of a README
            try:
                module = importlib.import_module(node.module)
            except ImportError as exc:
                failures.append(f"block {index}: from {node.module!r} import failed: {exc}")
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                if not hasattr(module, alias.name):
                    failures.append(
                        f"block {index}: {node.module!r} has no attribute {alias.name!r} "
                        f"(from README line importing {alias.name!r})"
                    )
    return failures


def test_readme_has_python_examples() -> None:
    blocks = _python_blocks(README_PATH.read_text(encoding="utf-8"))
    assert blocks, f"expected at least one ```python block in {README_PATH}"


def test_readme_python_examples_import_cleanly() -> None:
    blocks = _python_blocks(README_PATH.read_text(encoding="utf-8"))
    failures: list[str] = []
    for index, block in enumerate(blocks, start=1):
        failures.extend(_check_block(block, index))
    assert not failures, "\n".join(failures)
