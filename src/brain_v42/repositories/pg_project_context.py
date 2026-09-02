"""PostgreSQL repository for ProjectContext entities (CRUD only).

ProjectContext has no embedding or search_vector — it is a configuration/state
record, not a knowledge artifact. Access is always by project_key (unique).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.focus_history import record_focus_history
from brain_v42.db.focus_stamp import focus_stamp
from brain_v42.db.tables import adrs, decisions, learnings, project_contexts, runbooks, snippets
from brain_v42.models.project_context import (
    ProjectContext,
    ProjectContextCreate,
    ProjectContextUpdate,
)
from brain_v42.repositories.pg_base import BasePgRepository

logger = structlog.get_logger(__name__)


class PgProjectContextRepo(BasePgRepository):
    """CRUD repository for project_contexts table.

    Methods:
        create(data)             — INSERT a new project context
        get_by_id(id)            — SELECT by UUID
        get_by_key(project_key)  — SELECT by unique project_key
        update(id, data)         — UPDATE fields (partial, only non-None)
        delete(id)               — DELETE by UUID
        list_all(limit, offset)  — SELECT all, ordered by created_at DESC
        get_or_create(data)      — Upsert by project_key (insert or return existing)
        update_focus(project_key, focus, blockers) — Partial update of current_focus + blockers
        refresh_counts(project_key) — Recompute *_count columns by cross-table COUNT
    """

    table = project_contexts

    # ─── CRUD ─────────────────────────────────────────────────────────────────

    async def create(self, data: ProjectContextCreate) -> ProjectContext:  # type: ignore[override]
        """Insert a new project context and return the created record."""
        async with self.get_session() as session:
            async with session.begin():
                stmt = (
                    sa.insert(project_contexts)
                    .values(
                        project_key=data.project_key,
                        name=data.name,
                        description=data.description,
                        languages=data.languages,
                        frameworks=data.frameworks,
                        databases=data.databases,
                        code_style=data.code_style,
                        git_workflow=data.git_workflow,
                        test_strategy=data.test_strategy,
                        current_phase=data.current_phase,
                        current_focus=data.current_focus,
                        # No row exists to compare against, so the rule reduces
                        # to its base case: a focus supplied here is a focus
                        # written now, and its absence stays honestly undated.
                        focus_updated_at=(
                            datetime.now(UTC) if data.current_focus is not None else None
                        ),
                        blockers=data.blockers,
                        related_projects=data.related_projects,
                        local_path=data.local_path,
                        repo_url=data.repo_url,
                        metadata=data.metadata,
                        plan_scan_paths=data.plan_scan_paths,
                        gitlab_project_path=data.gitlab_project_path,
                        project_group=data.project_group,
                    )
                    .returning(*project_contexts.c)
                )
                result = await session.execute(stmt)
                row = result.mappings().one()
                # Revision 0 of a brand-new context. The deferred constraint
                # trigger sees UPDATEs only, so this line is the ONLY thing
                # standing between a context's first focus and a hole in the
                # trail — route (b) of 050, chosen and said out loud.
                await record_focus_history(
                    session,
                    project_key=str(row["project_key"]),
                    focus_revision=int(row["focus_revision"]),
                    focus=row["current_focus"],
                    source="context_upsert",
                )
                logger.info("project_context.created", project_key=data.project_key)
                return ProjectContext.model_validate(dict(row))

    async def get_by_id(self, id: UUID) -> ProjectContext | None:  # type: ignore[override]
        """Fetch a project context by its UUID."""
        async with self.get_session() as session:
            stmt = sa.select(project_contexts).where(project_contexts.c.id == id)
            result = await session.execute(stmt)
            row = result.mappings().first()
            if row is None:
                return None
            return ProjectContext.model_validate(dict(row))

    async def get_by_key(
        self,
        project_key: str,
        *,
        session: AsyncSession | None = None,
    ) -> ProjectContext | None:
        """Fetch a project context by its unique project_key.

        Accepts an optional caller-owned ``session`` so guards running inside
        an existing transaction (e.g. the proposal-service atomic apply path)
        can check project existence without opening a second connection.
        """

        async def _execute(sess: AsyncSession) -> ProjectContext | None:
            stmt = sa.select(project_contexts).where(project_contexts.c.project_key == project_key)
            result = await sess.execute(stmt)
            row = result.mappings().first()
            if row is None:
                return None
            return ProjectContext.model_validate(dict(row))

        if session is not None:
            return await _execute(session)
        async with self.get_session() as sess:
            return await _execute(sess)

    async def update(self, id: UUID, data: ProjectContextUpdate) -> ProjectContext | None:  # type: ignore[override]
        """Partial update — only fields provided (non-None) are changed."""
        update_values: dict = {
            k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None
        }
        if not update_values:
            return await self.get_by_id(id)
        # A generic partial write can still move the focus; when it does not,
        # the column must stay put (renaming a project is not authoring prose).
        if "current_focus" in update_values:
            update_values["focus_updated_at"] = focus_stamp(update_values["current_focus"])
        update_values["updated_at"] = datetime.now(UTC)
        async with self.get_session() as session:
            async with session.begin():
                stmt = (
                    sa.update(project_contexts)
                    .where(project_contexts.c.id == id)
                    .values(**update_values)
                    .returning(*project_contexts.c)
                )
                result = await session.execute(stmt)
                row = result.mappings().first()
                if row is None:
                    return None
                # Same condition as the stamp two dozen lines up, and for the
                # same reason: renaming a project is not authoring prose. A row
                # written on every partial update would also collide, the
                # revision not having moved.
                if "current_focus" in update_values:
                    await record_focus_history(
                        session,
                        project_key=str(row["project_key"]),
                        focus_revision=int(row["focus_revision"]),
                        focus=row["current_focus"],
                        source="generic_update",
                    )
                return ProjectContext.model_validate(dict(row))

    async def delete(self, id: UUID) -> bool:  # type: ignore[override]
        """Delete a project context by UUID. Returns True if a row was deleted."""
        async with self.get_session() as session:
            async with session.begin():
                stmt = (
                    sa.delete(project_contexts)
                    .where(project_contexts.c.id == id)
                    .returning(project_contexts.c.id)
                )
                result = await session.execute(stmt)
                return result.scalar_one_or_none() is not None

    async def list_all(  # type: ignore[override]
        self,
        limit: int = 20,
        offset: int = 0,
        project_group: str | None = None,
    ) -> list[ProjectContext]:
        """List all project contexts, ordered by created_at DESC."""
        async with self.get_session() as session:
            stmt = sa.select(project_contexts)
            if project_group is not None:
                stmt = stmt.where(project_contexts.c.project_group == project_group)
            stmt = stmt.order_by(project_contexts.c.created_at.desc()).limit(limit).offset(offset)
            result = await session.execute(stmt)
            rows = result.mappings().all()
            return [ProjectContext.model_validate(dict(r)) for r in rows]

    # ─── Group Methods ─────────────────────────────────────────────────────────

    async def get_keys_by_group(self, project_group: str) -> list[str]:
        """Return all project_keys that belong to the given group.

        Includes base keys registered in ``project_contexts`` AND colon-
        sub-partitions scanned from knowledge tables whose base is in the
        group. See docs/superpowers/plans/2026-04-20-group-includes-colon-
        subpartitions.md for rationale.
        """
        base_keys = (
            sa.select(project_contexts.c.project_key).where(
                project_contexts.c.project_group == project_group
            )
        ).subquery()

        knowledge_keys = sa.union_all(
            sa.select(decisions.c.project_key),
            sa.select(learnings.c.project_key),
            sa.select(snippets.c.project_key),
            sa.select(runbooks.c.project_key),
            sa.select(adrs.c.project_key),
        ).subquery()

        # split_part(key, ':', 1) returns the prefix, or the full key if no ':'
        # Typed literal for the position arg — PG requires integer type.
        base_prefix = sa.func.split_part(
            knowledge_keys.c.project_key, ":", sa.literal(1, sa.Integer)
        )

        sub_query = (
            sa.select(knowledge_keys.c.project_key)
            .where(knowledge_keys.c.project_key.is_not(None))
            # key contains a colon (prefix != full key)
            .where(base_prefix != knowledge_keys.c.project_key)
            # and the prefix is a base key of the target group
            .where(base_prefix.in_(sa.select(base_keys.c.project_key)))
            .distinct()
        )

        stmt = sa.union(
            sa.select(base_keys.c.project_key),
            sub_query,
        ).order_by(sa.column("project_key"))

        async with self.get_session() as session:
            result = await session.execute(stmt)
            return [row[0] for row in result.fetchall()]

    async def list_groups(self) -> list[dict]:
        """Return distinct groups with project count."""
        async with self.get_session() as session:
            stmt = (
                sa.select(
                    project_contexts.c.project_group,
                    sa.func.count().label("project_count"),
                )
                .where(project_contexts.c.project_group.is_not(None))
                .group_by(project_contexts.c.project_group)
                .order_by(project_contexts.c.project_group)
            )
            result = await session.execute(stmt)
            return [{"group": row[0], "count": row[1]} for row in result.fetchall()]

    # ─── Specialized Methods ───────────────────────────────────────────────────

    async def get_or_create(self, data: ProjectContextCreate) -> ProjectContext:
        """Upsert project context by project_key.

        INSERT if project_key does not exist, otherwise UPDATE all mutable
        fields with the new values. Returns the resulting record.
        """
        from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: PLC0415

        values = {
            "project_key": data.project_key,
            "name": data.name,
            "description": data.description,
            "languages": data.languages,
            "frameworks": data.frameworks,
            "databases": data.databases,
            "code_style": data.code_style,
            "git_workflow": data.git_workflow,
            "test_strategy": data.test_strategy,
            "current_phase": data.current_phase,
            "current_focus": data.current_focus,
            "blockers": data.blockers,
            "related_projects": data.related_projects,
            "local_path": data.local_path,
            "repo_url": data.repo_url,
            "metadata": data.metadata,
            "plan_scan_paths": data.plan_scan_paths,
            "gitlab_project_path": data.gitlab_project_path,
            "project_group": data.project_group,
        }
        insert_stmt = pg_insert(project_contexts).values(
            **values,
            focus_updated_at=(datetime.now(UTC) if data.current_focus is not None else None),
        )
        # Fields to update on conflict (everything except project_key and id)
        update_fields: dict[str, Any] = {k: v for k, v in values.items() if k != "project_key"}
        # The conflict branch overwrites current_focus, so it dates it — against
        # the row already stored, not against the one being proposed.
        update_fields["focus_updated_at"] = focus_stamp(insert_stmt.excluded.current_focus)
        update_fields["updated_at"] = datetime.now(UTC)

        async with self.get_session() as session:
            async with session.begin():
                stmt = insert_stmt.on_conflict_do_update(
                    index_elements=["project_key"],
                    set_=update_fields,
                ).returning(*project_contexts.c)
                result = await session.execute(stmt)
                row = result.mappings().one()
                # BOTH branches. The conflict branch is the overwrite channel B6
                # names — it rewrites the focus with no CAS, NULL included — so
                # it is the one that most needs recording.
                await record_focus_history(
                    session,
                    project_key=str(row["project_key"]),
                    focus_revision=int(row["focus_revision"]),
                    focus=row["current_focus"],
                    source="context_upsert",
                )
                logger.info("project_context.upserted", project_key=data.project_key)
                return ProjectContext.model_validate(dict(row))

    async def update_focus(
        self,
        project_key: str,
        focus: str,
        blockers: list[str] | None = None,
    ) -> ProjectContext | None:
        """Update current_focus and optionally blockers for a project."""
        update_values: dict = {
            "current_focus": focus,
            "focus_updated_at": focus_stamp(focus),
            "updated_at": datetime.now(UTC),
        }
        if blockers is not None:
            update_values["blockers"] = blockers
        async with self.get_session() as session:
            async with session.begin():
                stmt = (
                    sa.update(project_contexts)
                    .where(project_contexts.c.project_key == project_key)
                    .values(**update_values)
                    .returning(*project_contexts.c)
                )
                result = await session.execute(stmt)
                row = result.mappings().first()
                if row is None:
                    return None
                await record_focus_history(
                    session,
                    project_key=str(row["project_key"]),
                    focus_revision=int(row["focus_revision"]),
                    focus=row["current_focus"],
                    source="focus_tool",
                )
                return ProjectContext.model_validate(dict(row))

    async def refresh_counts(self, project_key: str) -> ProjectContext | None:
        """Recompute *_count columns via cross-table COUNT queries.

        Runs a single UPDATE with 5 scalar subqueries (one per knowledge table)
        to atomically refresh all count columns.
        """
        async with self.get_session() as session:
            async with session.begin():
                stmt = (
                    sa.update(project_contexts)
                    .where(project_contexts.c.project_key == project_key)
                    .values(
                        decisions_count=(
                            sa.select(sa.func.count())
                            .where(decisions.c.project_key == project_key)
                            .scalar_subquery()
                        ),
                        learnings_count=(
                            sa.select(sa.func.count())
                            .where(learnings.c.project_key == project_key)
                            .scalar_subquery()
                        ),
                        snippets_count=(
                            sa.select(sa.func.count())
                            .where(snippets.c.project_key == project_key)
                            .scalar_subquery()
                        ),
                        runbooks_count=(
                            sa.select(sa.func.count())
                            .where(runbooks.c.project_key == project_key)
                            .scalar_subquery()
                        ),
                        adrs_count=(
                            sa.select(sa.func.count())
                            .where(adrs.c.project_key == project_key)
                            .scalar_subquery()
                        ),
                        updated_at=datetime.now(UTC),
                    )
                    .returning(*project_contexts.c)
                )
                result = await session.execute(stmt)
                row = result.mappings().first()
                if row is None:
                    return None
                return ProjectContext.model_validate(dict(row))
