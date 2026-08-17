"""MCP tools for reading and explicitly mutating the feature roadmap."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from pydantic import ValidationError

from brain_v42.mcp.tools.formatters import (
    _ROADMAP_MAX_PROJECTS,
    format_error,
    format_id,
    format_roadmap,
)
from brain_v42.mcp.tools.tool_annotations import (
    _DESTRUCTIVE_ANNOTATIONS,
    _HEARTBEAT_ANNOTATIONS,
    _READ_ANNOTATIONS,
)
from brain_v42.models.feature import (
    VALID_FEATURE_STATUSES,
    CreatableFeatureStatus,
    FeatureCreate,
)
from brain_v42.models.project_key import canonicalize_project_key
from brain_v42.services.feature_creation_service import FeatureCreationError

if TYPE_CHECKING:
    from brain_v42.services.feature_creation_service import FeatureCreationService
    from brain_v42.services.feature_service import FeatureService
    from brain_v42.services.roadmap_service import RoadmapService

logger = structlog.get_logger(__name__)


def register_roadmap_tools(
    mcp: Any,
    roadmap_svc: RoadmapService,
    feature_svc: FeatureService,
    feature_creation_svc: FeatureCreationService | None,
) -> None:
    """Register roadmap read, creation, and status-update tools on mcp."""

    @mcp.tool(version="1.0", annotations=_HEARTBEAT_ANNOTATIONS)
    async def brain_feature_create(
        name: str,
        description: str,
        project_key: str,
        status: CreatableFeatureStatus = "planned",
        pinned: bool = True,
    ) -> str:
        """Create one explicit roadmap feature in an existing project.

        Malformed input, a missing project, an exact normalized duplicate,
        or an unusable embedding returns an error without a write. The locked
        duplicate recheck and insert are transactional. New explicit features
        are pinned by default and may start as planned, research, design,
        building, deployed, or done.
        """
        if feature_creation_svc is None:
            return format_error("Feature creation service unavailable; no feature created")
        try:
            payload = FeatureCreate(
                project_key=project_key,
                name=name,
                description=description,
                status=status,
                pinned=pinned,
            )
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors(include_input=False, include_url=False)
            )
            return format_error(f"Invalid feature: {details}")

        try:
            created = await feature_creation_svc.create(payload)
        except FeatureCreationError as exc:
            return format_error(str(exc))
        pin_label = "pinned" if created.pinned else "unpinned"
        logger.info(
            "mcp.brain_feature_create",
            feature_id=str(created.id),
            project_key=created.project_key,
            status=created.status,
            pinned=created.pinned,
        )
        return (
            f"Feature « {created.name} » [{format_id(str(created.id))}] created → "
            f"{created.status} ({pin_label}, project {created.project_key})"
        )

    @mcp.tool(version="1.2", annotations=_READ_ANNOTATIONS)
    async def brain_get_roadmap(
        project_key: str | None = None,
        full: bool = False,
    ) -> str:
        """Get the feature roadmap for all projects or a specific project.

        Returns features grouped by project with status, artifact counts,
        and last activity timestamps.

        The all-projects view carries TWO caps, because two different things grow:
        each project's feature table stops at 20 rows, and only the 10 most
        recently active projects are rendered. Each cap has its own notice naming
        what it dropped, and its own escape hatch — they are not interchangeable.

        The project cut is by recency, not by input order: rows arrive ordered by
        project_key, so slicing the head would keep whatever sorts alphabetically
        first and silently drop active work.

        When project_key is provided (scoped view) no feature cap is applied.

        Args:
            project_key: Filter to this project and return all of its features.
            full: Render every project instead of the 10 most recently active.
                Lifts the PROJECT cap only — the per-project feature cap still
                applies, otherwise this hatch would produce output roughly twice
                the size of the one the cap exists to prevent.
        """
        project_key = canonicalize_project_key(project_key, strict=False)
        logger.debug("mcp.brain_get_roadmap", project_key=project_key)
        projects = await roadmap_svc.get_roadmap(project_key=project_key)
        # Scoped call: pass a very high cap so all features are returned —
        # the notice in the all-projects view correctly points here as the escape hatch.
        # All-projects call: use the default cap (20) to avoid token bombs.
        max_features = 10_000 if project_key else 20
        # `full` is deliberately NOT mixed into max_features: reading it as "no caps
        # at all" would render every feature of every project and roughly double the
        # worst case this cap exists to bound.
        max_projects = 10_000 if (project_key or full) else _ROADMAP_MAX_PROJECTS
        return format_roadmap(
            [p.model_dump(mode="json") for p in projects],
            max_features_per_project=max_features,
            max_projects=max_projects,
        )

    @mcp.tool(version="1.0", annotations=_DESTRUCTIVE_ANNOTATIONS)
    async def brain_feature_update(feature: str, status: str, project_key: str) -> str:
        """Update a roadmap feature's status (write-back livraison → roadmap).

        `feature` accepte : nom exact, préfixe d'id git-style (≥8 hex, tirets
        ignorés) ou fragment unique du nom (ILIKE). Ambiguïté → erreur listant
        les candidats (id + nom).

        `status` : planned | research | design | building | deployed | done |
        archived (une session peut archiver une fausse feature à la main).

        Side-effects : status_updated_at=now(), pinned=true — même contrat que
        brain_update_project_focus(feature_status=…), qui reste fonctionnel.

        Consigne : feature livrée → brain_feature_update(name, 'deployed'|'done').
        """
        project_key = canonicalize_project_key(project_key, strict=False)
        logger.debug(
            "mcp.brain_feature_update",
            project_key=project_key,
            feature=feature,
            status=status,
        )
        if status not in VALID_FEATURE_STATUSES:
            return format_error(
                f"Invalid status '{status}' (valid: {', '.join(VALID_FEATURE_STATUSES)})"
            )
        resolved = await feature_svc.resolve_feature(project_key, feature)
        if isinstance(resolved, str):
            return format_error(resolved)
        updated = await feature_svc.update_status(resolved.id, status)
        if updated is None:
            return format_error(f"Feature {resolved.id} disappeared during update")
        return (
            f"Feature « {updated.name} » [{format_id(str(updated.id))}] → "
            f"{updated.status} (pinned, project {updated.project_key})"
        )
