"""FeatureService — read queries + write-back for session briefing and roadmap.

Queries used by brain_session_start:
- roadmap_alive: features not in {done, archived} and not merged, pinned first
- stale_pinned: pinned features whose updated_at is older than N days

Write-back (Task 7):
- resolve_feature: name exact → id prefix (git-style) → ILIKE unique
- update_status: UPDATE status + status_updated_at=now() + pinned=true

Both filter by project_key and respect a small LIMIT to keep briefing
cheap.
"""

from __future__ import annotations

import uuid as uuid_mod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import sqlalchemy as sa

from brain_v42.db.project_group_scope import project_key_in_group
from brain_v42.db.tables import features as _default_features
from brain_v42.entity_ids import normalize_uuid_prefix, parse_uuid, resolve_entity_id
from brain_v42.models.feature import VALID_FEATURE_STATUSES, Feature

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class FeatureStateConflictError(RuntimeError):
    """A requested feature transition conflicts with its merged state."""


_ROADMAP_ALIVE_SQL = """
SELECT f.name,
       f.status,
       COALESCE(f.pinned, false) AS pinned,
       COUNT(fa.artifact_id) AS artifact_count,
       MAX(fa.created_at) AS last_artifact_at
FROM features f
LEFT JOIN feature_artifacts fa ON fa.feature_id = f.id
WHERE f.project_key = :pk
  AND f.status NOT IN ('done', 'archived')
  AND f.merged_into IS NULL
GROUP BY f.id, f.name, f.status, f.pinned
ORDER BY COALESCE(f.pinned, false) DESC,
         MAX(fa.created_at) DESC NULLS LAST
LIMIT :lim
"""


@dataclass(frozen=True)
class RoadmapAliveFeature:
    """Ligne de la section briefing « Roadmap » (spec 2026-07-04 §5)."""

    name: str
    status: str
    pinned: bool
    artifact_count: int
    last_artifact_at: datetime | None


class FeatureService:
    """Briefing-side read queries for features.

    The `table` parameter allows tests to inject a SQLite-compatible
    table definition. Production callers omit it (default: db.tables.features).
    """

    # Convention: `self._sf` mirrors roadmap_service.py.
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        table: Table | None = None,
    ) -> None:
        self._sf = session_factory
        self._t = table if table is not None else _default_features

    async def roadmap_alive(
        self,
        project_key: str,
        limit: int = 5,
    ) -> list[RoadmapAliveFeature]:
        """Features vivantes : statut ∉ {done, archived} ∧ non mergées.

        Pinned en tête, puis dernière activité artifact desc (NULLS LAST).
        Remplace in_flight (spec 2026-07-04 §5 — la section briefing
        « Roadmap » remplace « In-flight »).
        """
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        sa.text(_ROADMAP_ALIVE_SQL), {"pk": project_key, "lim": limit}
                    )
                )
                .mappings()
                .all()
            )
        return [
            RoadmapAliveFeature(
                name=r["name"],
                status=r["status"],
                pinned=bool(r["pinned"]),
                artifact_count=int(r["artifact_count"] or 0),
                last_artifact_at=r["last_artifact_at"],
            )
            for r in rows
        ]

    async def stale_pinned(
        self,
        project_key: str,
        stale_days: int = 30,
        limit: int = 5,
    ) -> list[Feature]:
        """Return pinned features whose updated_at is older than stale_days.

        Archived features are excluded — they are intentionally inactive and
        must not surface in briefings.  Ordered oldest-first so the most
        neglected items surface first.
        """
        cutoff = datetime.now(tz=UTC) - timedelta(days=stale_days)
        t = self._t
        stmt = (
            sa.select(t)
            .where(t.c.project_key == project_key)
            .where(t.c.pinned.is_(True))
            .where(t.c.updated_at < cutoff)
            .where(t.c.status != "archived")
            .order_by(t.c.updated_at.asc())
            .limit(limit)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).mappings().all()
        return [Feature.model_validate(dict(r)) for r in rows]

    async def resolve_id_prefix(
        self,
        prefix_hex: str,
        *,
        limit: int = 6,
    ) -> list[uuid_mod.UUID]:
        """Préfixe git-style → ids features (pattern PgBaseRepo.resolve_id_prefix)."""
        if not prefix_hex or not set(prefix_hex) <= set("0123456789abcdef"):
            return []
        t = self._t
        bare_id = sa.func.replace(sa.cast(t.c.id, sa.Text), "-", "")
        stmt = sa.select(t.c.id).where(bare_id.like(prefix_hex + "%")).order_by(t.c.id).limit(limit)
        async with self._sf() as session:
            return list((await session.execute(stmt)).scalars().all())

    async def resolve_feature(self, project_key: str, feature: str) -> Feature | str:
        """Résout `feature` : nom exact → id (UUID/préfixe ≥8 hex) → ILIKE unique.

        Retourne la Feature ou un message d'erreur (pattern resolve_entity_id :
        str = erreur, isinstance-check au call site). Les candidats ambigus
        sont listés (id court + nom).
        """
        t = self._t
        async with self._sf() as session:
            exact = (
                (
                    await session.execute(
                        sa.select(t).where(t.c.project_key == project_key, t.c.name == feature)
                    )
                )
                .mappings()
                .all()
            )
        if len(exact) == 1:
            return Feature.model_validate(dict(exact[0]))
        if len(exact) > 1:
            listed = ", ".join(f"{str(r['id'])[:8]} « {r['name']} »" for r in exact[:6])
            return f"Ambiguous feature name '{feature}' — matches: {listed}. Use an id prefix."

        # Branche id : UUID complet ou préfixe git-style (≥8 hex).
        if parse_uuid(feature) is not None or normalize_uuid_prefix(feature) is not None:
            resolved = await resolve_entity_id(feature, self.resolve_id_prefix, label="feature")
            if isinstance(resolved, str):
                return resolved
            async with self._sf() as session:
                row = (
                    (await session.execute(sa.select(t).where(t.c.id == resolved)))
                    .mappings()
                    .one_or_none()
                )
            if row is None:
                return f"No feature found for id {feature}"
            if row["project_key"] != project_key:
                return (
                    f"Feature {resolved} belongs to project '{row['project_key']}', "
                    f"not '{project_key}'"
                )
            return Feature.model_validate(dict(row))

        # ILIKE unique scopé projet.
        async with self._sf() as session:
            fuzzy = (
                (
                    await session.execute(
                        sa.select(t)
                        .where(
                            t.c.project_key == project_key,
                            t.c.name.ilike(f"%{feature}%"),
                        )
                        .order_by(t.c.name)
                        .limit(6)
                    )
                )
                .mappings()
                .all()
            )
        if not fuzzy:
            return f"No feature matching '{feature}' in project '{project_key}'"
        if len(fuzzy) > 1:
            listed = ", ".join(f"{str(r['id'])[:8]} « {r['name']} »" for r in fuzzy)
            return f"Ambiguous feature '{feature}' — matches: {listed}. Be more specific."
        return Feature.model_validate(dict(fuzzy[0]))

    async def update_status(
        self,
        feature_id: uuid_mod.UUID,
        status: str,
    ) -> Feature | None:
        """UPDATE status + status_updated_at=now + pinned=true (même contrat
        que le chemin update_feature_statuses de brain_update_project_focus).

        Exception : archiver une feature ne l'épingle PAS — un feature archivée
        doit disparaître des briefings, pas y rester accrochée indéfiniment.
        """
        t = self._t
        if status == "archived":
            # Archived: status + timestamp only — no pinning.
            values: dict = {"status": status, "status_updated_at": sa.func.now()}
        else:
            values = {"status": status, "status_updated_at": sa.func.now(), "pinned": True}
        stmt = t.update().where(t.c.id == feature_id).values(**values).returning(t)
        async with self._sf() as session:
            async with session.begin():
                row = (await session.execute(stmt)).mappings().one_or_none()
        return Feature.model_validate(dict(row)) if row else None

    async def patch(
        self,
        feature_id: uuid_mod.UUID,
        *,
        status: str | None = None,
        pinned: bool | None = None,
        archived: bool | None = None,
        project_group: str | None = None,
    ) -> Feature | None:
        """Apply one partial management mutation and return the updated feature.

        ``archived=True`` is the explicit UI archive action and aliases the
        canonical ``status='archived'`` state. Status changes keep the existing
        auto-pin behavior unless the caller supplies an explicit ``pinned`` value.
        """
        if status is not None and status not in VALID_FEATURE_STATUSES:
            raise ValueError(f"invalid feature status {status!r}")
        if archived is True and status not in {None, "archived"}:
            raise ValueError("archived=true conflicts with a non-archived status")

        effective_status = "archived" if archived is True else status
        values: dict[str, object] = {}
        if effective_status is not None:
            values.update(status=effective_status, status_updated_at=sa.func.now())
            if effective_status != "archived" and pinned is None:
                values["pinned"] = True
        if pinned is not None:
            values["pinned"] = pinned
        if not values:
            raise ValueError("at least one feature mutation is required")

        conditions = [self._t.c.id == feature_id]
        if project_group is not None:
            conditions.append(project_key_in_group(self._t.c.project_key, project_group))
        stmt = self._t.update().where(*conditions).values(**values).returning(self._t)
        async with self._sf() as session:
            async with session.begin():
                current = (
                    await session.execute(
                        sa.select(self._t.c.merged_into).where(*conditions).with_for_update()
                    )
                ).one_or_none()
                if current is None:
                    return None
                if (
                    effective_status is not None
                    and effective_status != "archived"
                    and current.merged_into is not None
                ):
                    raise FeatureStateConflictError(
                        "a merged feature cannot be reactivated; update its survivor instead"
                    )
                row = (await session.execute(stmt)).mappings().one_or_none()
        return Feature.model_validate(dict(row)) if row else None
