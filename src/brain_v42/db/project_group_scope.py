"""Reusable SQL predicates for fail-closed project-group scoping."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.sql.elements import ColumnElement

from brain_v42.db.tables import project_contexts


def _key_matches_base(
    project_key: ColumnElement[str],
    project_group: str,
) -> ColumnElement[bool]:
    base_key = project_contexts.c.project_key
    return sa.exists(
        sa.select(sa.literal(1))
        .select_from(project_contexts)
        .where(
            project_contexts.c.project_group == project_group,
            sa.or_(
                project_key == base_key,
                sa.and_(
                    base_key.not_like("%:%"),
                    project_key.like(base_key + sa.literal(":%")),
                ),
            ),
        )
    )


def _validated_group(project_group: str) -> str:
    if not isinstance(project_group, str) or not project_group.strip():
        raise ValueError("project_group must be a non-empty string")
    return project_group


def project_key_in_group(
    project_key: ColumnElement[str],
    project_group: str,
) -> ColumnElement[bool]:
    """Match an explicit group key or one of its colon sub-partitions."""
    return _key_matches_base(project_key, _validated_group(project_group))


def ticket_in_group(
    from_project: ColumnElement[str],
    to_project: ColumnElement[str],
    project_group: str,
) -> ColumnElement[bool]:
    """Match tickets where at least one participant belongs to the group."""
    validated_group = _validated_group(project_group)
    return sa.or_(
        _key_matches_base(from_project, validated_group),
        _key_matches_base(to_project, validated_group),
    )
