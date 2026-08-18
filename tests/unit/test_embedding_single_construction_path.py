"""The embedding client has exactly one construction path.

An operator flipping BRAIN_EMBEDDING_BACKEND expects every runtime to follow.
A site that constructs GPUEmbeddingService directly pins itself to the shim
regardless of configuration, and the symptom — one runtime embedding against a
different model than the other eight — surfaces only as unexplained search
drift, never as an error.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "brain_v42"
FACTORY = SRC / "services" / "embedding_factory.py"


def _constructions(path: pathlib.Path) -> list[int]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "GPUEmbeddingService"
    ]


def test_only_the_factory_constructs_the_embedding_client() -> None:
    offenders = {
        str(path.relative_to(SRC)): lines
        for path in SRC.rglob("*.py")
        if path != FACTORY and (lines := _constructions(path))
    }
    assert not offenders, (
        "these modules construct GPUEmbeddingService directly instead of calling "
        f"build_embedding_service(settings): {offenders}"
    )


def test_the_factory_does_construct_it() -> None:
    """Guards the test above from passing because the name was merely renamed."""
    assert _constructions(FACTORY)
