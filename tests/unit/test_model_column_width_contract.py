"""Every Pydantic write field must be bounded by its VARCHAR column.

Ticket 39cc4986 (and its red-shrik duplicate 2af71e69). Twice on 2026-08-06, a
value too long for its column made a write fail at INSERT time instead of being
rejected at validation: ``access_log.actor`` VARCHAR(64) facing an unbounded actor
(fixed by c4122058), then ``runbooks.estimated_duration`` VARCHAR(50) facing a
field with no ``max_length``.

This is therefore not an isolated accident but a CLASS of defect: wherever a
Pydantic model does not mirror its column's width, the failure surfaces too late,
on the database side, in a shape the caller cannot diagnose.

This test is the generic guard the ticket asked for — the only way to prevent a
recurrence. It compares ``db.tables`` (the in-repository source of truth) with the
write models, and fails on ALL the divergences at once.

Two legitimate ways to bound a field:

- ``Field(max_length=n)`` with ``n`` at most the column's width;
- a ``Literal[...]`` annotation whose values all fit in the column — the
  enumeration-typed fields (``status``, ``confidence``, ``freshness_status``) are
  already bounded by construction and need no redundant length constraint.
"""

from __future__ import annotations

from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

import pytest
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from sqlalchemy import String, Table

from brain_v42.db import tables as db_tables
from brain_v42.models.adr import ADRCreate, ADRUpdate
from brain_v42.models.brain_session import BrainSession
from brain_v42.models.decision import DecisionCreate, DecisionUpdate
from brain_v42.models.dream_promotion import DreamPromotionCreate
from brain_v42.models.feature import FeatureCreate
from brain_v42.models.gitlab_event import GitLabEventCreate
from brain_v42.models.indexed_plan import IndexedPlanCreate
from brain_v42.models.indexed_plan_chunk import IndexedPlanChunkCreate
from brain_v42.models.learning import LearningCreate, LearningUpdate
from brain_v42.models.project_context import ProjectContextCreate, ProjectContextUpdate
from brain_v42.models.runbook import RunbookCreate, RunbookUpdate
from brain_v42.models.snippet import SnippetCreate, SnippetUpdate
from brain_v42.models.ticket import TicketCreate

#: Models taken by a client write, table by table. Adding a write model here is
#: mandatory: a model absent from this table is NOT audited, and would silently
#: reopen the class of defect.
WRITE_MODELS_BY_TABLE: dict[str, tuple[type[BaseModel], ...]] = {
    "adrs": (ADRCreate, ADRUpdate),
    # Registered by 046, and it was NOT before: `brain_sessions` already carried
    # bounded columns (`project_key` 50, `client_key` 128) without being audited. 046
    # adds `intent` VARCHAR(500), `started_by_actor` and `connection_id` VARCHAR(64)
    # to it, all with a Pydantic rail — forgetting this line would redden NOTHING,
    # which is exactly the class of defect this file exists to close.
    "brain_sessions": (BrainSession,),
    "decisions": (DecisionCreate, DecisionUpdate),
    "dream_promotions": (DreamPromotionCreate,),
    "features": (FeatureCreate,),
    "gitlab_events": (GitLabEventCreate,),
    "indexed_plan_chunks": (IndexedPlanChunkCreate,),
    "indexed_plans": (IndexedPlanCreate,),
    "learnings": (LearningCreate, LearningUpdate),
    "project_contexts": (ProjectContextCreate, ProjectContextUpdate),
    "runbooks": (RunbookCreate, RunbookUpdate),
    "snippets": (SnippetCreate, SnippetUpdate),
    "tickets": (TicketCreate,),
}


def _declared_max_length(field: FieldInfo) -> int | None:
    """Return the ``max_length`` carried by the field's annotated metadata."""
    for constraint in field.metadata:
        length = getattr(constraint, "max_length", None)
        if length is not None:
            return int(length)
    return None


def _annotation_members(annotation: Any) -> list[Any]:
    """Split an optional/union annotation into its non-``None`` members."""
    if get_origin(annotation) in (Union, UnionType):
        return [arg for arg in get_args(annotation) if arg is not type(None)]
    return [annotation]


def _literal_string_values(annotation: Any) -> list[str] | None:
    """Return the literal string values of the annotation, if it is a ``Literal``."""
    if get_origin(annotation) is not Literal:
        return None
    return [arg for arg in get_args(annotation) if isinstance(arg, str)]


def _violation(
    table: Table,
    column_name: str,
    limit: int,
    model: type[BaseModel],
    field: FieldInfo,
) -> str | None:
    """Describe how *field* fails to respect its column width, or ``None``."""
    for member in _annotation_members(field.annotation):
        literal_values = _literal_string_values(member)
        if literal_values is not None:
            too_long = [value for value in literal_values if len(value) > limit]
            if too_long:
                return (
                    f"{model.__name__}.{column_name}: literal value(s) {too_long} exceed "
                    f"{table.name}.{column_name} VARCHAR({limit})"
                )
            continue
        if member is not str:
            continue
        declared = _declared_max_length(field)
        if declared is None:
            return (
                f"{model.__name__}.{column_name}: no max_length, but "
                f"{table.name}.{column_name} is VARCHAR({limit}) — an over-long "
                f"value fails at INSERT instead of at validation"
            )
        if declared > limit:
            return (
                f"{model.__name__}.{column_name}: max_length={declared} exceeds "
                f"{table.name}.{column_name} VARCHAR({limit})"
            )
    return None


def _bounded_string_columns(table: Table) -> list[tuple[str, int]]:
    """Return ``(name, length)`` for each length-bounded string column."""
    bounded: list[tuple[str, int]] = []
    for column in table.columns:
        column_type = column.type
        if isinstance(column_type, String) and column_type.length is not None:
            bounded.append((column.name, column_type.length))
    return bounded


def test_write_models_respect_their_varchar_widths() -> None:
    """No write field may accept a value its column would truncate."""
    violations: list[str] = []

    for table_name, models in WRITE_MODELS_BY_TABLE.items():
        table = db_tables.METADATA.tables[table_name]
        for column_name, limit in _bounded_string_columns(table):
            for model in models:
                field = model.model_fields.get(column_name)
                if field is None:
                    continue
                found = _violation(table, column_name, limit, model, field)
                if found is not None:
                    violations.append(found)

    assert not violations, "model/column width divergence:\n" + "\n".join(
        f"  - {v}" for v in sorted(violations)
    )


@pytest.mark.parametrize("table_name", sorted(WRITE_MODELS_BY_TABLE))
def test_audited_tables_exist_in_schema(table_name: str) -> None:
    """A typo in the audit map would silently disable the guard for that table."""
    assert table_name in db_tables.METADATA.tables
