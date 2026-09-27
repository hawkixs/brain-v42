"""`CLAIM_VERIFY_RUN_LOCK` must never collide with an existing advisory-lock key.

Scans `src/brain_v42` for every `_LOCK`/`LOCK_KEY` assignment (module- or
class-level) and evaluates its value where that is statically possible, so a
new advisory key added anywhere cannot silently share `pg_advisory_lock`'s
single namespace with this one. A class-level alias of an already-module-level
constant (e.g. `ObserverOwnership.LOCK_KEY = DELIVERY_OBSERVER_LOCK`) is not
independently re-evaluated here: its value is caught where it is DEFINED, at
module level, in `pg_delivery.py`.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

from brain_v42.repositories.pg_claim_nightly import CLAIM_VERIFY_RUN_LOCK, VerifyRunOwnershipLost

_SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "brain_v42"


def _module_dotted_name(path: Path) -> str:
    parts = path.relative_to(_SRC_ROOT.parent).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _is_lock_name(name: str) -> bool:
    return name == "LOCK_KEY" or name.endswith("_LOCK")


def _known_lock_values() -> dict[str, int]:
    values: dict[str, int] = {}
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_level_names = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and _is_lock_name(target.id):
                    module_level_names.add(target.id)
        if not module_level_names:
            continue
        module = importlib.import_module(_module_dotted_name(path))
        for name in module_level_names:
            value = getattr(module, name, None)
            if isinstance(value, int):
                values[f"{module.__name__}.{name}"] = value
    return values


def test_lock_key_is_a_bigint_outside_the_int4_hashtext_range() -> None:
    assert 2**32 < CLAIM_VERIFY_RUN_LOCK < 2**63


def test_lock_key_collides_with_no_other_advisory_constant_in_src() -> None:
    known = _known_lock_values()
    others = {
        name: value for name, value in known.items() if not name.endswith(".CLAIM_VERIFY_RUN_LOCK")
    }

    assert others, "the scan must find at least the pre-existing advisory-lock constants"
    colliding = {name: value for name, value in others.items() if value == CLAIM_VERIFY_RUN_LOCK}
    assert colliding == {}


def test_ownership_lost_carries_the_stable_message() -> None:
    assert str(VerifyRunOwnershipLost()) == "claim_verify_ownership_lost"
