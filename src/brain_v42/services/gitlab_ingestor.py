"""GitLabIngestor — processes GitLab webhook payloads.

Extracts context from MR, push, and pipeline events, embeds the text,
and links signals to features via ClusterGuard.

Event parsing:
    MR action=open   -> signal_type=mr_opened
    MR action=merge  -> signal_type=mr_merged
    MR action=close  -> signal_type=mr_closed  (linking only)
    Push             -> signal_type=push
    Pipeline success -> signal_type=pipeline_success
    Pipeline failure -> returns immediately (no embedding)
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from brain_v42.db.tables import feature_artifacts, gitlab_events

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from brain_v42.services.cluster_guard import ClusterGuard
    from brain_v42.services.gpu_embedding_service import GPUEmbeddingService

logger = structlog.get_logger(__name__)

# Regex for extracting feature name from branch refs
_BRANCH_RE = re.compile(r"^refs/heads/(?:feat|fix|feature|hotfix|bugfix)/(.+)$")

# Regex for stripping conventional commit prefixes from MR titles
_TITLE_PREFIX_RE = re.compile(r"^(?:feat|fix|chore|docs|refactor|test|ci|style|perf|build):\s*")

# MR action to signal type mapping
_MR_ACTION_MAP = {
    "open": "mr_opened",
    "merge": "mr_merged",
    "close": "mr_closed",
}


class GitLabIngestor:
    """Processes GitLab webhook payloads into feature signals."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_svc: GPUEmbeddingService,
        cluster_guard: ClusterGuard,
        *,
        mutation_guard: Callable[[], None] | None = None,
    ) -> None:
        self._sf = session_factory
        self._embedding_svc = embedding_svc
        self._cluster_guard = cluster_guard
        self._mutation_guard = mutation_guard

    async def process_event(self, payload: dict, event_uuid: str, project_key: str) -> dict:
        """Process a single GitLab webhook event.

        Returns:
            dict with at least ``status`` key:
            - ``{"status": "skipped_duplicate"}`` if event_uuid already exists
            - ``{"status": "skipped_pipeline_failure"}`` for failed pipelines
            - ``{"status": "processed", "signal_type": ..., "action": ...}``
        """
        self._ensure_mutation_allowed()

        # 1. Duplicate check
        is_duplicate = await self._is_duplicate(event_uuid)
        self._ensure_mutation_allowed()
        if is_duplicate:
            return {"status": "skipped_duplicate"}

        # 2. Parse event
        event_type, signal_type, text = self._parse_event(payload)

        # 3. Pipeline failure -> return immediately
        if signal_type == "pipeline_failure":
            return {"status": "skipped_pipeline_failure"}

        # 4. Embed
        try:
            embedding = await self._embedding_svc.embed(text[:2000])
        except Exception:
            logger.warning("gitlab_ingestor.embed_failed", exc_info=True)
            embedding = None
        self._ensure_mutation_allowed()
        if embedding is None:
            return {"status": "skipped_embed_failed"}

        # 5. ClusterGuard resolve
        feature, action = await self._cluster_guard.resolve(
            text=text,
            embedding=embedding,
            project_key=project_key,
            signal_type=signal_type,
        )
        self._ensure_mutation_allowed()

        # Link-only mode: signal_type outside CREATING_SIGNALS (e.g.
        # mr_closed) can come back skipped with no confident candidate.
        # feature is then None — store the event without a feature link.
        feature_id = feature.id if feature is not None else None  # type: ignore[attr-defined]

        # 6. Store event
        event_row_id = await self._store_event(
            gitlab_event_id=event_uuid,
            event_type=event_type,
            project_key=project_key,
            ref=self._extract_ref(payload),
            title=text[:500],
            embedding=embedding,
            feature_id=feature_id,
        )
        self._ensure_mutation_allowed()

        # 7. Link to feature via feature_artifacts
        if event_row_id is not None and feature is not None:
            await self._link_to_feature(
                feature_id=feature.id,  # type: ignore[attr-defined]
                artifact_id=event_row_id,
            )
            self._ensure_mutation_allowed()

        logger.info(
            "gitlab_ingestor.processed",
            event_uuid=event_uuid,
            event_type=event_type,
            signal_type=signal_type,
            action=action,
            project_key=project_key,
        )

        return {
            "status": "processed",
            "signal_type": signal_type,
            "action": action,
        }

    def _ensure_mutation_allowed(self) -> None:
        if self._mutation_guard is not None:
            self._mutation_guard()

    # ── event parsing ──────────────────────────────────────────────────

    def _parse_event(self, payload: dict) -> tuple[str, str, str]:
        """Parse payload into (event_type, signal_type, text).

        Returns:
            (event_type, signal_type, extracted_text)
        """
        object_kind = payload.get("object_kind", "")

        if object_kind == "merge_request":
            return self._parse_mr_event(payload)
        elif object_kind == "push":
            return self._parse_push_event(payload)
        elif object_kind == "pipeline":
            return self._parse_pipeline_event(payload)

        return ("unknown", "unknown", "")

    def _parse_mr_event(self, payload: dict) -> tuple[str, str, str]:
        """Parse merge_request event."""
        attrs = payload.get("object_attributes", {})
        action = attrs.get("action", "open")
        signal_type = _MR_ACTION_MAP.get(action, "mr_opened")
        text = self._extract_text(payload)
        return ("merge_request", signal_type, text)

    def _parse_push_event(self, payload: dict) -> tuple[str, str, str]:
        """Parse push event."""
        text = self._extract_text(payload)
        return ("push", "push", text)

    def _parse_pipeline_event(self, payload: dict) -> tuple[str, str, str]:
        """Parse pipeline event."""
        attrs = payload.get("object_attributes", {})
        status = attrs.get("status", "")
        ref = attrs.get("ref", "")

        if status == "success":
            text = f"Pipeline success: {ref}"
            return ("pipeline", "pipeline_success", text)

        return ("pipeline", "pipeline_failure", "")

    def _extract_text(self, payload: dict) -> str:
        """Extract meaningful text from a webhook payload.

        For MR events: title (stripped prefix) + description[:500] + branch
        For push events: branch feature name + commit messages
        """
        object_kind = payload.get("object_kind", "")

        if object_kind == "merge_request":
            attrs = payload.get("object_attributes", {})
            title = attrs.get("title", "")
            description = attrs.get("description", "") or ""
            branch = attrs.get("source_branch", "")

            # Strip conventional commit prefix
            clean_title = _TITLE_PREFIX_RE.sub("", title)

            parts = [clean_title]
            if description:
                parts.append(description[:500])
            if branch:
                parts.append(branch)
            return " ".join(parts)

        elif object_kind == "push":
            ref = payload.get("ref", "")
            commits = payload.get("commits", [])

            parts = []
            feature_name = self._branch_to_feature_name(ref)
            if feature_name:
                parts.append(feature_name)

            for commit in commits:
                msg = commit.get("message", "").strip()
                if msg:
                    parts.append(msg)

            return " ".join(parts)

        return ""

    def _extract_ref(self, payload: dict) -> str | None:
        """Extract ref/branch from payload."""
        object_kind = payload.get("object_kind", "")

        if object_kind == "merge_request":
            result: str | None = payload.get("object_attributes", {}).get("source_branch")
            return result
        elif object_kind == "push":
            ref: str | None = payload.get("ref")
            return ref
        elif object_kind == "pipeline":
            pipe_ref: str | None = payload.get("object_attributes", {}).get("ref")
            return pipe_ref

        return None

    @staticmethod
    def _branch_to_feature_name(ref: str) -> str | None:
        """Parse branch ref into a human-readable feature name.

        ``refs/heads/feat/decay-system`` -> ``Decay System``

        Returns None for non-feature branches.
        """
        match = _BRANCH_RE.match(ref)
        if not match:
            return None
        return match.group(1).replace("-", " ").title()

    # ── database operations ────────────────────────────────────────────

    async def _is_duplicate(self, event_uuid: str) -> bool:
        """Check if gitlab_event_id already exists."""
        async with self._sf() as session:
            stmt = sa.select(gitlab_events.c.id).where(
                gitlab_events.c.gitlab_event_id == event_uuid
            )
            result = await session.execute(stmt)
            return result.fetchone() is not None

    async def _store_event(
        self,
        *,
        gitlab_event_id: str,
        event_type: str,
        project_key: str,
        ref: str | None,
        title: str | None,
        embedding: list[float],
        feature_id: object,
    ) -> object | None:
        """Insert into gitlab_events with on_conflict_do_nothing.

        Returns the event row id, or None if conflict.
        """
        self._ensure_mutation_allowed()
        async with self._sf() as session:
            stmt = (
                pg_insert(gitlab_events)
                .values(
                    gitlab_event_id=gitlab_event_id,
                    event_type=event_type,
                    project_key=project_key,
                    ref=ref,
                    title=title,
                    embedding=embedding,
                    feature_id=feature_id,
                )
                .on_conflict_do_nothing()
                .returning(gitlab_events.c.id)
            )
            self._ensure_mutation_allowed()
            result = await session.execute(stmt)
            self._ensure_mutation_allowed()
            row = result.fetchone()
            self._ensure_mutation_allowed()
            await session.commit()
            self._ensure_mutation_allowed()

            if row is None:
                return None
            event_id: object = row.id
            return event_id

    async def _link_to_feature(
        self,
        feature_id: object,
        artifact_id: object,
    ) -> None:
        """Insert feature_artifact link (gitlab_event -> feature)."""
        self._ensure_mutation_allowed()
        async with self._sf() as session:
            stmt = (
                pg_insert(feature_artifacts)
                .values(
                    feature_id=feature_id,
                    artifact_type="gitlab_event",
                    artifact_id=artifact_id,
                    similarity_score=1.0,
                )
                .on_conflict_do_nothing()
            )
            self._ensure_mutation_allowed()
            await session.execute(stmt)
            self._ensure_mutation_allowed()
            await session.commit()
            self._ensure_mutation_allowed()
