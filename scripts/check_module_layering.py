#!/usr/bin/env python3
"""Fail closed when the top-level module graph stops being acyclic.

A module can only be extracted into a standalone service when it sits in a
single-node strongly connected component. A cycle is a design defect that no
deployment topology can repair: splitting cyclic modules into services turns
the cycle into a distributed monolith. This preflight keeps extraction a
reversible option by proving the graph stays a DAG.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Mapping
from pathlib import Path

_ROOT_MODULE = "_root"
_PACKAGE_NAME = "brain_v42"
_MAX_SOURCE_BYTES = 4 * 1024 * 1024


class ModuleLayeringError(RuntimeError):
    """Raised when the module graph violates the layering contract."""


def _top_level_packages(package_root: Path) -> frozenset[str]:
    """Return the sub-package names, so `brain_v42.X` resolves to a real node."""
    return frozenset(
        entry.name
        for entry in package_root.iterdir()
        if entry.is_dir() and not entry.name.startswith(".") and any(entry.glob("*.py"))
    )


def _module_of(source_path: Path, package_root: Path) -> str:
    relative_parts = source_path.relative_to(package_root).parts
    return relative_parts[0] if len(relative_parts) > 1 else _ROOT_MODULE


def _resolve(dotted_parts: list[str], packages: frozenset[str]) -> str | None:
    if not dotted_parts or dotted_parts[0] != _PACKAGE_NAME:
        return None
    if len(dotted_parts) < 2:
        return _ROOT_MODULE
    return dotted_parts[1] if dotted_parts[1] in packages else _ROOT_MODULE


def _imported_modules(
    source_path: Path,
    package_root: Path,
    packages: frozenset[str],
) -> set[str]:
    if source_path.stat().st_size > _MAX_SOURCE_BYTES:
        raise ModuleLayeringError(f"{source_path.name} exceeds the source size limit")
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise ModuleLayeringError(f"{source_path.name} is unparseable") from exc

    # Package path of the file itself, used to resolve relative imports.
    own_package = [_PACKAGE_NAME, *source_path.relative_to(package_root).parts[:-1]]
    resolved: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = _resolve(alias.name.split("."), packages)
                if target is not None:
                    resolved.add(target)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = own_package[: len(own_package) - (node.level - 1)]
                if not base:
                    raise ModuleLayeringError(
                        f"{source_path.name} escapes the package with a relative import"
                    )
                dotted = [*base, *(node.module.split(".") if node.module else [])]
            else:
                dotted = (node.module or "").split(".")
            target = _resolve(dotted, packages)
            if target is not None:
                resolved.add(target)

    return resolved


def build_module_graph(package_root: Path) -> dict[str, frozenset[str]]:
    """Map each top-level module to the top-level modules it imports."""
    if not package_root.is_dir():
        raise ModuleLayeringError(f"{package_root} is not a package directory")
    packages = _top_level_packages(package_root)
    edges: dict[str, set[str]] = {}

    for source_path in sorted(package_root.rglob("*.py")):
        importer = _module_of(source_path, package_root)
        targets = _imported_modules(source_path, package_root, packages)
        edges.setdefault(importer, set()).update(targets - {importer})

    for module in packages | {_ROOT_MODULE}:
        edges.setdefault(module, set())
    return {module: frozenset(targets) for module, targets in edges.items()}


def find_cycles(graph: Mapping[str, frozenset[str]]) -> list[frozenset[str]]:
    """Return every strongly connected component holding more than one module."""
    order: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[frozenset[str]] = []
    counter = 0

    def visit(root: str) -> None:
        nonlocal counter
        # Explicit stack: recursion would cap the graph at the interpreter limit.
        work: list[tuple[str, list[str]]] = [(root, sorted(graph.get(root, frozenset())))]
        order[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)

        while work:
            node, pending = work[-1]
            if pending:
                neighbour = pending.pop()
                if neighbour not in order:
                    order[neighbour] = low[neighbour] = counter
                    counter += 1
                    stack.append(neighbour)
                    on_stack.add(neighbour)
                    work.append((neighbour, sorted(graph.get(neighbour, frozenset()))))
                elif neighbour in on_stack:
                    low[node] = min(low[node], order[neighbour])
                continue

            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == order[node]:
                component: set[str] = set()
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.add(member)
                    if member == node:
                        break
                if len(component) > 1:
                    components.append(frozenset(component))

    for module in sorted(graph):
        if module not in order:
            visit(module)
    return sorted(components, key=lambda component: sorted(component))


def validate_module_layering(package_root: Path) -> None:
    """Require the module graph to be acyclic."""
    cycles = find_cycles(build_module_graph(package_root))
    if cycles:
        rendered = "; ".join(" <-> ".join(sorted(cycle)) for cycle in cycles)
        raise ModuleLayeringError(f"module cycle: {rendered}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        validate_module_layering(args.package)
    except ModuleLayeringError as exc:
        print(f"module layering preflight failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
