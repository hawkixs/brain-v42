"""Workflows: named instances of the two shapes coded in the package (spec 0.5.0 §3.2).

One TOML table per workflow in ``workflows.toml``, read from the operator's
configuration directory only (:mod:`headless_agents.config_paths`, §3.3):

.. code-block:: toml

    [build]
    shape     = "implement"
    implement = "implementer"

    [multi-review]
    shape  = "review"
    review = ["reviewer-codex", "reviewer-agy", "reviewer-claude"]
    judge  = "judge"

A shape is reviewed code in the package, never configuration: a workflow only
names the roles that fill its slots, and a slot checks the capability its
role declares -- it never grants one (§3.3). Every refusal names the file,
the entry and the rule, before anything runs.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast

from .registry import PROVIDER_NAMES
from .roles import NAME_PATTERN, Role, RolesError, resolve_role

Shape = Literal["implement", "review"]
SHAPES: Final[tuple[Shape, ...]] = ("implement", "review")
#: The slots of each shape (§3.2); any other key of a workflow is refused.
SLOTS: Final[Mapping[Shape, frozenset[str]]] = {
    "implement": frozenset({"implement"}),
    "review": frozenset({"review", "judge"}),
}


class WorkflowsError(ValueError):
    """``workflows.toml`` or one of its workflows cannot be used; the message says where and why."""


@dataclass(frozen=True)
class Workflow:
    name: str
    shape: Shape
    #: The role of the ``implement`` slot; ``None`` for a ``review`` workflow.
    implement: str | None
    #: The roles of the ``review`` slot, in declaration order; ``()`` for ``implement``.
    review: tuple[str, ...]
    #: The role of the ``judge`` slot, when there is one.
    judge: str | None

    def slot_roles(self) -> tuple[tuple[str, str], ...]:
        """Every ``(slot, role)`` pair: the implementer, or the reviewers then the judge."""
        if self.implement is not None:
            return (("implement", self.implement),)
        pairs = tuple(("review", role) for role in self.review)
        return pairs + ((("judge", self.judge),) if self.judge is not None else ())


def _refuse(path: Path, name: str, rule: str) -> WorkflowsError:
    return WorkflowsError(f"{path}: [{name}] {rule}")


def _slot_role(path: Path, name: str, slot: str, value: object, roles: Mapping[str, Role]) -> Role:
    """The role a slot names: a role of ``roles.toml``, or a provider's implicit role."""
    if not isinstance(value, str):
        raise _refuse(path, name, f"{slot} must name a role")
    try:
        return resolve_role(value, roles)
    except RolesError:
        raise _refuse(path, name, f"{slot} names unknown role {value!r}") from None


def _implement(
    path: Path, name: str, table: Mapping[str, object], roles: Mapping[str, Role]
) -> Workflow:
    if "implement" not in table:
        raise _refuse(path, name, "the implement slot is missing")
    role = _slot_role(path, name, "implement", table["implement"], roles)
    if not role.write:
        raise _refuse(
            path,
            name,
            f"the implement slot needs a role with write = true; {role.name} does not write",
        )
    return Workflow(name=name, shape="implement", implement=role.name, review=(), judge=None)


def _review(
    path: Path, name: str, table: Mapping[str, object], roles: Mapping[str, Role]
) -> Workflow:
    raw = table.get("review")
    if raw is None:
        raise _refuse(path, name, "the review slot is missing")
    names = [raw] if isinstance(raw, str) else raw
    if not isinstance(names, list) or not names:
        raise _refuse(path, name, "review must name one role or a non-empty list of roles")
    reviewers = [_slot_role(path, name, "review", value, roles) for value in names]
    seen: set[str] = set()
    for role in reviewers:
        if role.name in seen:
            raise _refuse(path, name, f"{role.name} appears twice in review")
        seen.add(role.name)
        if role.write:
            raise _refuse(
                path, name, f"the review slot needs roles with write = false; {role.name} writes"
            )
    judge: str | None = None
    if "judge" in table:
        role = _slot_role(path, name, "judge", table["judge"], roles)
        if role.write:
            raise _refuse(
                path, name, f"the judge slot needs a role with write = false; {role.name} writes"
            )
        judge = role.name
    elif len(reviewers) > 1:
        raise _refuse(path, name, "a judge is required with two reviewers or more")
    return Workflow(
        name=name,
        shape="review",
        implement=None,
        review=tuple(role.name for role in reviewers),
        judge=judge,
    )


def _workflow(path: Path, name: str, table: object, roles: Mapping[str, Role]) -> Workflow:
    if not isinstance(table, dict):
        raise _refuse(path, name, "a workflow must be a table")
    if not NAME_PATTERN.fullmatch(name):
        raise _refuse(
            path,
            name,
            "invalid workflow name: lowercase letters, digits and '-', starting with a letter, "
            "at most 64 characters",
        )
    if name in PROVIDER_NAMES or name in roles:
        what = "a provider" if name in PROVIDER_NAMES else "a role of roles.toml"
        raise _refuse(
            path,
            name,
            f"collides with {what}: workflow, role and provider names are disjoint",
        )
    if "shape" not in table:
        raise _refuse(path, name, "the shape is missing; valid shapes: implement, review")
    value = table["shape"]
    if not isinstance(value, str) or value not in SHAPES:
        raise _refuse(path, name, f"unknown shape {value!r}; valid shapes: implement, review")
    shape = cast("Shape", value)
    unknown = sorted(table.keys() - {"shape"} - SLOTS[shape])
    if unknown:
        raise _refuse(path, name, f"unknown slot {unknown[0]!r} for the shape {shape}")
    if shape == "implement":
        return _implement(path, name, table, roles)
    return _review(path, name, table, roles)


def load_workflows(path: Path | None, *, roles: Mapping[str, Role]) -> dict[str, Workflow]:
    """Every workflow declared in ``path``, validated against ``roles``; ``{}`` without a file.

    ``roles`` are the roles of ``roles.toml``: a slot names one of them or a
    provider, and a workflow's name collides with none of them.
    """
    if path is None:
        return {}
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WorkflowsError(f"{path}: {exc}") from None
    except RecursionError:
        # tomllib recurses per nesting level: a value nested hundreds deep is a
        # broken file, not a crash (as roles.load_roles).
        raise WorkflowsError(f"{path}: nested too deeply to be a workflows file") from None
    return {name: _workflow(path, name, table, roles) for name, table in document.items()}


__all__ = ["SHAPES", "SLOTS", "Shape", "Workflow", "WorkflowsError", "load_workflows"]
