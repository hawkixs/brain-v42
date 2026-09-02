"""RoadmapService — roadmap reads and atomic project-focus coordination.

The project-focus mutation owns one PostgreSQL transaction across the focus,
feature status, and pin state so the MCP composite cannot report false success.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import sqlalchemy as sa
import structlog
from sqlalchemy import text

from brain_v42.db.focus_history import record_focus_history
from brain_v42.db.focus_stamp import focus_stamp
from brain_v42.db.tables import features, project_contexts
from brain_v42.models.feature import VALID_FEATURE_STATUSES, RoadmapFeature, RoadmapProject

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


logger = structlog.get_logger(__name__)

_ARTIFACT_TYPES = ("learning", "decision", "snippet", "runbook", "adr", "plan", "gitlab_event")

_ROADMAP_SQL = """
SELECT
    f.project_key,
    COALESCE(pc.name, f.project_key) AS project_name,
    pc.current_phase,
    f.id AS feature_id,
    f.name AS feature_name,
    f.status,
    f.status_updated_at,
    f.pinned,
    fa.artifact_type,
    COUNT(fa.artifact_id) AS type_count,
    MAX(fa.created_at) AS type_last_activity
FROM features f
LEFT JOIN project_contexts pc ON pc.project_key = f.project_key
LEFT JOIN feature_artifacts fa ON fa.feature_id = f.id
WHERE (CAST(:pk AS VARCHAR) IS NULL OR f.project_key = :pk)
  AND f.status != 'archived'
  AND f.merged_into IS NULL
GROUP BY f.project_key, pc.name, pc.current_phase,
         f.id, f.name, f.status, f.status_updated_at, f.pinned, fa.artifact_type
ORDER BY f.project_key, f.pinned DESC, f.status_updated_at DESC
"""


class ProjectFocusError(RuntimeError):
    """Base error for atomic project-focus mutations."""


class ProjectFocusNotFoundError(ProjectFocusError):
    """Raised when the requested project context does not exist."""


class ProjectFocusConflictError(ProjectFocusError):
    """Raised when the caller's focus revision is stale."""

    def __init__(self, *, current_focus: str | None, current_revision: int) -> None:
        self.current_focus = current_focus
        self.current_revision = current_revision
        super().__init__(
            f"project focus changed concurrently; current revision is {current_revision}"
        )


class ProjectFocusValidationError(ProjectFocusError, ValueError):
    """Raised before any write when a composite mutation is invalid."""


@dataclass(frozen=True)
class ProjectFocusUpdateResult:
    """Committed outcome of one atomic focus and roadmap mutation."""

    current_focus: str
    focus_revision: int
    features_updated: tuple[str, ...]
    features_unpinned: tuple[str, ...]


class RoadmapService:
    """Reads roadmap data from features + feature_artifacts tables."""

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory

    async def update_project_focus(
        self,
        project_key: str,
        current_focus: str,
        *,
        expected_focus_revision: int,
        blockers: list[str] | None = None,
        feature_status: dict[str, str] | None = None,
        unpin: list[str] | None = None,
    ) -> ProjectFocusUpdateResult:
        """Atomically compare-and-swap focus plus optional roadmap mutations."""
        normalized_focus = current_focus.strip()
        if not normalized_focus:
            raise ProjectFocusValidationError("current_focus must not be blank")
        if (
            isinstance(expected_focus_revision, bool)
            or not isinstance(expected_focus_revision, int)
            or expected_focus_revision < 0
        ):
            raise ProjectFocusValidationError(
                "expected_focus_revision must be a non-negative integer"
            )

        status_updates = dict(feature_status or {})
        unpin_names = tuple(dict.fromkeys(unpin or []))
        invalid_statuses = {
            name: status
            for name, status in status_updates.items()
            if status not in VALID_FEATURE_STATUSES
        }
        if invalid_statuses:
            rendered = ", ".join(
                f"{name}={status}" for name, status in sorted(invalid_statuses.items())
            )
            raise ProjectFocusValidationError(f"invalid feature status: {rendered}")

        overlap = sorted(set(status_updates).intersection(unpin_names))
        if overlap:
            raise ProjectFocusValidationError(
                "feature cannot be status-updated and unpinned in the same batch: "
                + ", ".join(overlap)
            )

        requested_names = tuple(sorted(set(status_updates).union(unpin_names)))
        async with self._sf() as session:
            async with session.begin():
                context_row = (
                    (
                        await session.execute(
                            sa.select(
                                project_contexts.c.id,
                                project_contexts.c.current_focus,
                                project_contexts.c.focus_revision,
                            )
                            .where(project_contexts.c.project_key == project_key)
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if context_row is None:
                    raise ProjectFocusNotFoundError(f'Project "{project_key}" not found')
                if context_row["focus_revision"] != expected_focus_revision:
                    raise ProjectFocusConflictError(
                        current_focus=context_row["current_focus"],
                        current_revision=int(context_row["focus_revision"]),
                    )

                feature_rows = await self._lock_requested_features(
                    session,
                    project_key=project_key,
                    requested_names=requested_names,
                )
                resolved = self._validate_requested_features(
                    requested_names=requested_names,
                    feature_rows=feature_rows,
                    status_updates=status_updates,
                )

                for name, status in sorted(status_updates.items()):
                    values: dict[str, object] = {
                        "status": status,
                        "status_updated_at": sa.func.now(),
                        "pinned": status != "archived",
                    }
                    await session.execute(
                        features.update()
                        .where(features.c.id == resolved[name]["id"])
                        .values(**values)
                    )

                for name in unpin_names:
                    await session.execute(
                        features.update()
                        .where(features.c.id == resolved[name]["id"])
                        .values(pinned=False)
                    )

                context_values: dict[str, object] = {
                    "current_focus": normalized_focus,
                    # Consume the CAS token even when the focus text is unchanged:
                    # blockers and roadmap mutations are part of the same versioned batch.
                    "focus_revision": project_contexts.c.focus_revision + 1,
                    # …but a blockers-only batch is not a focus write. Spending
                    # the token must not rejuvenate prose nobody re-authored.
                    "focus_updated_at": focus_stamp(normalized_focus),
                    "updated_at": sa.func.now(),
                }
                if blockers is not None:
                    context_values["blockers"] = blockers
                updated_context = (
                    (
                        await session.execute(
                            project_contexts.update()
                            .where(
                                project_contexts.c.id == context_row["id"],
                                project_contexts.c.focus_revision == expected_focus_revision,
                            )
                            .values(**context_values)
                            .returning(
                                project_contexts.c.current_focus,
                                project_contexts.c.focus_revision,
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if updated_context is None:
                    raise ProjectFocusConflictError(
                        current_focus=context_row["current_focus"],
                        current_revision=int(context_row["focus_revision"]),
                    )
                # After the write, on the revision the write RETURNED — never on
                # `expected + 1` computed here. And after the conflict branch, so
                # a refused CAS leaves no trace of an intent it never applied.
                await record_focus_history(
                    session,
                    project_key=project_key,
                    focus_revision=int(updated_context["focus_revision"]),
                    focus=updated_context["current_focus"],
                    source="focus_tool",
                )

        return ProjectFocusUpdateResult(
            current_focus=str(updated_context["current_focus"]),
            focus_revision=int(updated_context["focus_revision"]),
            features_updated=tuple(sorted(status_updates)),
            features_unpinned=tuple(unpin_names),
        )

    async def _lock_requested_features(
        self,
        session: AsyncSession,
        *,
        project_key: str,
        requested_names: tuple[str, ...],
    ) -> list[dict]:
        if not requested_names:
            return []
        rows = (
            (
                await session.execute(
                    sa.select(
                        features.c.id,
                        features.c.name,
                        features.c.merged_into,
                    )
                    .where(
                        features.c.project_key == project_key,
                        features.c.name.in_(requested_names),
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    @staticmethod
    def _validate_requested_features(
        *,
        requested_names: tuple[str, ...],
        feature_rows: list[dict],
        status_updates: dict[str, str],
    ) -> dict[str, dict]:
        grouped: dict[str, list[dict]] = {}
        for row in feature_rows:
            grouped.setdefault(str(row["name"]), []).append(row)

        missing = [name for name in requested_names if name not in grouped]
        ambiguous = [name for name in requested_names if len(grouped.get(name, [])) > 1]
        merged = [
            name
            for name, status in status_updates.items()
            if status != "archived"
            and len(grouped.get(name, [])) == 1
            and grouped[name][0]["merged_into"] is not None
        ]
        errors = []
        if missing:
            errors.append("missing: " + ", ".join(sorted(missing)))
        if ambiguous:
            errors.append("ambiguous: " + ", ".join(sorted(ambiguous)))
        if merged:
            errors.append("merged features cannot be reactivated: " + ", ".join(sorted(merged)))
        if errors:
            raise ProjectFocusValidationError("; ".join(errors))
        return {name: rows[0] for name, rows in grouped.items()}

    async def get_roadmap(self, project_key: str | None = None) -> list[RoadmapProject]:
        """Return roadmap grouped by project."""
        async with self._sf() as session:
            rows = (await session.execute(text(_ROADMAP_SQL), {"pk": project_key})).fetchall()
        return self._pivot_rows(rows)

    def _pivot_rows(self, rows: list) -> list[RoadmapProject]:
        """Pivot flat SQL rows into nested RoadmapProject -> RoadmapFeature."""
        projects: dict[str, dict] = {}
        feature_map: dict[str, dict] = {}

        for row in rows:
            pk = row.project_key
            fid = str(row.feature_id)

            if pk not in projects:
                projects[pk] = {
                    "project_key": pk,
                    "name": row.project_name,
                    "current_phase": row.current_phase,
                    "feature_ids": [],
                }

            if fid not in feature_map:
                feature_map[fid] = {
                    "project_key": pk,
                    "name": row.feature_name,
                    "status": row.status,
                    "status_updated_at": row.status_updated_at,
                    "pinned": row.pinned or False,
                    "artifact_count": dict.fromkeys(_ARTIFACT_TYPES, 0),
                    "last_activity": row.status_updated_at,
                }
                projects[pk]["feature_ids"].append(fid)

            if row.artifact_type and row.type_count:
                feature_map[fid]["artifact_count"][row.artifact_type] = row.type_count
                if row.type_last_activity:
                    current = feature_map[fid]["last_activity"]
                    if current is None or row.type_last_activity > current:
                        feature_map[fid]["last_activity"] = row.type_last_activity

        result = []
        for _pk, proj_data in projects.items():
            features_list = []
            for fid in proj_data["feature_ids"]:
                fd = feature_map[fid]
                features_list.append(
                    RoadmapFeature(
                        name=fd["name"],
                        status=fd["status"],
                        status_updated_at=fd["status_updated_at"],
                        pinned=fd["pinned"],
                        artifact_count=fd["artifact_count"],
                        last_activity=fd["last_activity"],
                    )
                )

            result.append(
                RoadmapProject(
                    project_key=proj_data["project_key"],
                    name=proj_data["name"],
                    current_phase=proj_data["current_phase"],
                    features=features_list,
                )
            )

        return result

    async def update_feature_statuses(
        self,
        project_key: str,
        feature_status: dict[str, str],
    ) -> int:
        """Update status for named features and pin them. Returns count updated."""
        invalid_statuses = {
            name: status
            for name, status in feature_status.items()
            if status not in VALID_FEATURE_STATUSES
        }
        if invalid_statuses:
            rendered = ", ".join(
                f"{name}={status}" for name, status in sorted(invalid_statuses.items())
            )
            raise ProjectFocusValidationError(f"invalid feature status: {rendered}")

        updated = 0
        async with self._sf() as session:
            for name, new_status in feature_status.items():
                result = await session.execute(
                    text("""
                        UPDATE features
                        SET status = :status, status_updated_at = NOW(), pinned = true
                        WHERE project_key = :pk AND name = :name
                    """),
                    {"status": new_status, "pk": project_key, "name": name},
                )
                if result.rowcount > 0:
                    updated += 1
                else:
                    logger.warning(
                        "roadmap.feature_not_found",
                        project_key=project_key,
                        feature_name=name,
                    )
            await session.commit()
        return updated

    async def unpin_features(
        self,
        project_key: str,
        feature_names: list[str],
    ) -> int:
        """Set pinned=false for named features. Returns count updated."""
        if not feature_names:
            return 0
        async with self._sf() as session:
            result = await session.execute(
                text("""
                    UPDATE features
                    SET pinned = false
                    WHERE project_key = :pk AND name = ANY(:names)
                """),
                {"pk": project_key, "names": feature_names},
            )
            await session.commit()
            return result.rowcount  # type: ignore[no-any-return]
