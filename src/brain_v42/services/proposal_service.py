"""Shared mutations for ticket-extraction and roadmap-curation proposals.

The nightly scripts and the Codex HTTP gateway use this service so proposal
state transitions, roadmap postconditions, and audit logs cannot diverge.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID

import sqlalchemy as sa
import structlog

from brain_v42.db.project_group_scope import project_key_in_group, ticket_in_group
from brain_v42.db.tables import (
    features,
    project_contexts,
    roadmap_curation_proposals,
    ticket_extraction_proposals,
    tickets,
)
from brain_v42.models.decision import DecisionCreate
from brain_v42.models.learning import LearningCreate

ProposalFamily = Literal["ticket-extraction", "roadmap-curation"]
ProposalStatus = Literal["applied", "rejected"]

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ProposalMutationResult:
    """Stable service result usable by CLI counters and JSON gateways."""

    proposal_id: int
    family: ProposalFamily
    status: ProposalStatus
    ticket_id: UUID | None = None
    entity_id: UUID | None = None
    operation: str | None = None
    apply_log: dict[str, Any] | None = None


class ProposalServiceError(RuntimeError):
    """Base error carrying proposal identity for transport-layer mapping."""

    def __init__(
        self,
        message: str,
        *,
        family: ProposalFamily,
        proposal_id: int,
        operation: str | None = None,
    ) -> None:
        super().__init__(message)
        self.family = family
        self.proposal_id = proposal_id
        self.operation = operation


class ProposalNotFoundError(ProposalServiceError, LookupError):
    """The requested proposal id does not exist in the selected family."""


class ProposalNotProposedError(ProposalServiceError):
    """The proposal was already applied or rejected."""

    def __init__(
        self,
        message: str,
        *,
        family: ProposalFamily,
        proposal_id: int,
        status: str,
        operation: str | None = None,
    ) -> None:
        super().__init__(
            message,
            family=family,
            proposal_id=proposal_id,
            operation=operation,
        )
        self.status = status


class ProposalApplyError(ProposalServiceError):
    """A proposal mutation failed and its database transaction was rolled back."""


class ProposalOperationNotAllowedError(ProposalApplyError):
    """The proposal operation is outside the caller's explicit allow-list."""


class ProposalStateConflictError(ProposalApplyError):
    """The reviewed source state changed before the proposal was applied."""


class PostConditionError(RuntimeError):
    """A roadmap feature did not reach the state requested by its proposal."""


class ProposalService:
    """Apply or reject one proposal while enforcing canonical state changes."""

    def __init__(
        self,
        session_factory: Any,
        learning_service: Any,
        decision_service: Any,
    ) -> None:
        self._session_factory = session_factory
        self._learning_service = learning_service
        self._decision_service = decision_service

    async def apply_ticket_extraction(
        self,
        proposal_id: int,
        *,
        project_group: str | None = None,
    ) -> ProposalMutationResult:
        """Create the proposed entity, then mark its proposal and ticket."""
        family: ProposalFamily = "ticket-extraction"
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    row = await self._load_proposed(
                        session,
                        ticket_extraction_proposals,
                        proposal_id,
                        family,
                        project_group=project_group,
                    )
                    if project_group is not None:
                        await self._lock_ticket_target_scope(
                            session,
                            row,
                            proposal_id,
                            project_group,
                        )
                    created_entity, entity_data = await self._create_ticket_entity(session, row)
                    entity_id = cast(UUID, created_entity.id)
                    await session.execute(
                        ticket_extraction_proposals.update()
                        .where(ticket_extraction_proposals.c.id == proposal_id)
                        .values(
                            status="applied",
                            applied_entity_id=entity_id,
                            applied_at=sa.func.now(),
                        )
                    )
                    ticket_id = row["ticket_id"]
                    await self._mark_ticket_done_if_triaged(session, ticket_id)
        except ProposalServiceError:
            raise
        except Exception as exc:
            raise ProposalApplyError(
                f"ticket-extraction proposal {proposal_id} apply failed: {exc}",
                family=family,
                proposal_id=proposal_id,
            ) from exc
        await self._enrich_ticket_entity(row, created_entity, entity_data)
        return ProposalMutationResult(
            proposal_id=proposal_id,
            family=family,
            status="applied",
            ticket_id=ticket_id,
            entity_id=entity_id,
        )

    async def reject_ticket_extraction(
        self,
        proposal_id: int,
        *,
        project_group: str | None = None,
    ) -> ProposalMutationResult:
        """Reject one extraction proposal and finish its ticket when fully triaged."""
        family: ProposalFamily = "ticket-extraction"
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    row = await self._load_proposed(
                        session,
                        ticket_extraction_proposals,
                        proposal_id,
                        family,
                        project_group=project_group,
                    )
                    await session.execute(
                        ticket_extraction_proposals.update()
                        .where(ticket_extraction_proposals.c.id == proposal_id)
                        .values(status="rejected")
                    )
                    ticket_id = row["ticket_id"]
                    await self._mark_ticket_done_if_triaged(session, ticket_id)
            return ProposalMutationResult(
                proposal_id=proposal_id,
                family=family,
                status="rejected",
                ticket_id=ticket_id,
            )
        except ProposalServiceError:
            raise
        except Exception as exc:
            raise ProposalApplyError(
                f"ticket-extraction proposal {proposal_id} reject failed: {exc}",
                family=family,
                proposal_id=proposal_id,
            ) from exc

    async def apply_roadmap_curation(
        self,
        proposal_id: int,
        allowed_ops: tuple[str, ...] | None = None,
        *,
        project_group: str | None = None,
    ) -> ProposalMutationResult:
        """Apply one roadmap proposal with its postconditions and audit log."""
        family: ProposalFamily = "roadmap-curation"
        operation: str | None = None
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    row = await self._load_proposed(
                        session,
                        roadmap_curation_proposals,
                        proposal_id,
                        family,
                        project_group=project_group,
                    )
                    operation = str(row["op"])
                    if allowed_ops is not None and operation not in allowed_ops:
                        raise ProposalOperationNotAllowedError(
                            f"roadmap-curation proposal {proposal_id} operation "
                            f"{operation!r} is outside allowed_ops",
                            family=family,
                            proposal_id=proposal_id,
                            operation=operation,
                        )
                    apply_log = await self._apply_roadmap_change(session, row)
                    await session.execute(
                        roadmap_curation_proposals.update()
                        .where(roadmap_curation_proposals.c.id == proposal_id)
                        .values(
                            status="applied",
                            applied_at=sa.func.now(),
                            apply_log=apply_log,
                        )
                    )
            return ProposalMutationResult(
                proposal_id=proposal_id,
                family=family,
                status="applied",
                operation=operation,
                apply_log=apply_log,
            )
        except ProposalServiceError:
            raise
        except Exception as exc:
            raise ProposalApplyError(
                f"roadmap-curation proposal {proposal_id} apply failed: {exc}",
                family=family,
                proposal_id=proposal_id,
                operation=operation,
            ) from exc

    async def reject_roadmap_curation(
        self,
        proposal_id: int,
        *,
        project_group: str | None = None,
    ) -> ProposalMutationResult:
        """Reject one proposed roadmap curation without mutating its feature."""
        family: ProposalFamily = "roadmap-curation"
        operation: str | None = None
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    row = await self._load_proposed(
                        session,
                        roadmap_curation_proposals,
                        proposal_id,
                        family,
                        project_group=project_group,
                    )
                    operation = str(row["op"])
                    await session.execute(
                        roadmap_curation_proposals.update()
                        .where(roadmap_curation_proposals.c.id == proposal_id)
                        .values(status="rejected")
                    )
            return ProposalMutationResult(
                proposal_id=proposal_id,
                family=family,
                status="rejected",
                operation=operation,
            )
        except ProposalServiceError:
            raise
        except Exception as exc:
            raise ProposalApplyError(
                f"roadmap-curation proposal {proposal_id} reject failed: {exc}",
                family=family,
                proposal_id=proposal_id,
                operation=operation,
            ) from exc

    async def _load_proposed(
        self,
        session: Any,
        table: sa.Table,
        proposal_id: int,
        family: ProposalFamily,
        *,
        project_group: str | None = None,
    ) -> Mapping[str, Any]:
        conditions = [table.c.id == proposal_id]
        if project_group is not None:
            if family == "ticket-extraction":
                conditions.append(
                    sa.exists(
                        sa.select(tickets.c.id).where(
                            tickets.c.id == table.c.ticket_id,
                            ticket_in_group(
                                tickets.c.from_project,
                                tickets.c.to_project,
                                project_group,
                            ),
                        )
                    )
                )
            else:
                conditions.append(
                    sa.exists(
                        sa.select(features.c.id).where(
                            features.c.id == table.c.feature_id,
                            project_key_in_group(features.c.project_key, project_group),
                        )
                    )
                )
        result = await session.execute(sa.select(table).where(*conditions).with_for_update())
        row = result.mappings().one_or_none()
        if row is None:
            raise ProposalNotFoundError(
                f"{family} proposal {proposal_id} not found",
                family=family,
                proposal_id=proposal_id,
            )
        status = str(row["status"])
        if status != "proposed":
            raise ProposalNotProposedError(
                f"{family} proposal {proposal_id} is {status!r}, not 'proposed'",
                family=family,
                proposal_id=proposal_id,
                status=status,
                operation=str(row["op"]) if family == "roadmap-curation" else None,
            )
        return cast(Mapping[str, Any], row)

    async def _lock_ticket_target_scope(
        self,
        session: Any,
        row: Mapping[str, Any],
        proposal_id: int,
        project_group: str,
    ) -> None:
        base_key = project_contexts.c.project_key
        target_project = sa.literal(str(row["target_project"]))
        scoped_bases = (
            (
                await session.execute(
                    sa.select(base_key)
                    .where(
                        project_contexts.c.project_group == project_group,
                        sa.or_(
                            target_project == base_key,
                            sa.and_(
                                base_key.not_like("%:%"),
                                target_project.like(base_key + sa.literal(":%")),
                            ),
                        ),
                    )
                    .order_by(base_key)
                    .with_for_update(read=True)
                )
            )
            .scalars()
            .all()
        )
        if not scoped_bases:
            raise ProposalNotFoundError(
                f"ticket-extraction proposal {proposal_id} not found",
                family="ticket-extraction",
                proposal_id=proposal_id,
            )

    async def _create_ticket_entity(
        self,
        session: Any,
        row: Mapping[str, Any],
    ) -> tuple[Any, LearningCreate | DecisionCreate]:
        payload = row["payload"]
        ticket_id = row["ticket_id"]
        project_key = row["target_project"]
        if row["target_type"] == "learning":
            learning_data = LearningCreate(
                topic=payload["topic"],
                insight=payload["insight"],
                tags=payload.get("tags", []),
                project_key=project_key,
                source=f"ticket:{ticket_id}",
                source_type="automated",
                confidence="medium",
            )
            result = await self._learning_service.create(learning_data, session=session)
            return result, learning_data
        if row["target_type"] == "decision":
            decision_data = DecisionCreate(
                title=payload["title"],
                description=payload["description"],
                reasoning=payload["reasoning"],
                tags=payload.get("tags", []),
                project_key=project_key,
                metadata={
                    "source": f"ticket:{ticket_id}",
                    "source_type": "automated",
                },
            )
            result = await self._decision_service.create(decision_data, session=session)
            return result, decision_data
        raise ValueError(f"unknown ticket proposal target_type {row['target_type']!r}")

    async def _enrich_ticket_entity(
        self,
        row: Mapping[str, Any],
        created_entity: Any,
        entity_data: LearningCreate | DecisionCreate,
    ) -> None:
        """Attempt derived work after commit; the NULL vector remains a durable backlog."""
        try:
            if row["target_type"] == "learning":
                await self._learning_service.enrich_created(created_entity, entity_data)
            else:
                await self._decision_service.enrich_created(created_entity, entity_data)
        except Exception as exc:  # noqa: BLE001 - authoritative proposal/entity already committed
            logger.warning(
                "proposal.ticket_extraction.enrichment_failed",
                proposal_id=int(row["id"]),
                target_type=str(row["target_type"]),
                entity_id=str(created_entity.id),
                reason=type(exc).__name__,
            )

    async def _mark_ticket_done_if_triaged(self, session: Any, ticket_id: UUID | None) -> None:
        if ticket_id is None:
            return
        # Serialize finalization across concurrent apply/reject requests for
        # different proposals belonging to the same ticket.
        await session.execute(
            sa.select(tickets.c.id).where(tickets.c.id == ticket_id).with_for_update()
        )
        remaining = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(ticket_extraction_proposals)
                .where(
                    ticket_extraction_proposals.c.ticket_id == ticket_id,
                    ticket_extraction_proposals.c.status == "proposed",
                )
            )
        ).scalar_one()
        if remaining != 0:
            return
        await session.execute(
            tickets.update()
            .where(tickets.c.id == ticket_id)
            .values(extraction_status="done", updated_at=sa.func.now())
        )

    async def _apply_roadmap_change(
        self,
        session: Any,
        row: Mapping[str, Any],
    ) -> dict[str, Any]:
        operation = row["op"]
        if operation == "merge":
            return await self._apply_merge(session, row)
        if operation == "archive":
            return await self._apply_archive(session, row)
        if operation == "status":
            return await self._apply_status(session, row)
        if operation == "rename":
            return await self._apply_rename(session, row)
        raise PostConditionError(f"unknown op {operation!r}")

    async def _apply_merge(
        self,
        session: Any,
        row: Mapping[str, Any],
    ) -> dict[str, Any]:
        loser = row["feature_id"]
        proposal_id = int(row["id"])
        try:
            into = UUID(str(row["payload"]["into"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ProposalStateConflictError(
                "merge target is invalid; review the proposal again",
                family="roadmap-curation",
                proposal_id=proposal_id,
                operation="merge",
            ) from exc
        if loser == into:
            raise ProposalStateConflictError(
                "a feature cannot be merged into itself",
                family="roadmap-curation",
                proposal_id=proposal_id,
                operation="merge",
            )

        locked = await self._lock_features(session, loser, into)
        prior = self._require_live_feature(
            locked,
            loser,
            proposal_id=proposal_id,
            operation="merge",
            role="source",
            allow_pinned=False,
        )
        target = self._require_live_feature(
            locked,
            into,
            proposal_id=proposal_id,
            operation="merge",
            role="target",
            allow_pinned=True,
        )
        if prior["project_key"] != target["project_key"]:
            raise ProposalStateConflictError(
                "merge source and target must belong to the same project",
                family="roadmap-curation",
                proposal_id=proposal_id,
                operation="merge",
            )
        self._require_reviewed_state(
            row,
            prior,
            field="status_updated_at",
            role="source",
        )
        self._require_reviewed_state(
            row,
            target,
            field="status_updated_at",
            role="target",
        )
        moved = await self._move_merge_artifacts(session, loser, into)
        duplicates = await self._delete_duplicate_merge_links(session, loser)
        await session.execute(
            sa.text(
                "UPDATE features SET merged_into = :into, status = 'archived', "
                "status_updated_at = NOW() WHERE id = :loser"
            ),
            {"into": into, "loser": loser},
        )
        await self._check_merge_postconditions(session, loser, into)
        return {
            "op": "merge",
            "into": str(into),
            "loser_prior_status": prior["status"],
            "loser_prior_name": prior["name"],
            "moved_artifacts": self._artifact_log(moved),
            "duplicate_links_deleted": self._artifact_log(duplicates),
        }

    async def _move_merge_artifacts(
        self,
        session: Any,
        loser: UUID,
        into: UUID,
    ) -> list[Any]:
        result = await session.execute(
            sa.text(
                """
                UPDATE feature_artifacts fa SET feature_id = :into
                WHERE fa.feature_id = :loser
                  AND NOT EXISTS (
                      SELECT 1 FROM feature_artifacts dup
                      WHERE dup.feature_id = :into
                        AND dup.artifact_type = fa.artifact_type
                        AND dup.artifact_id = fa.artifact_id
                  )
                RETURNING fa.artifact_type, fa.artifact_id
                """
            ),
            {"into": into, "loser": loser},
        )
        return list(result.all())

    async def _delete_duplicate_merge_links(self, session: Any, loser: UUID) -> list[Any]:
        result = await session.execute(
            sa.text(
                "DELETE FROM feature_artifacts WHERE feature_id = :loser "
                "RETURNING artifact_type, artifact_id"
            ),
            {"loser": loser},
        )
        return list(result.all())

    async def _check_merge_postconditions(
        self,
        session: Any,
        loser: UUID,
        into: UUID,
    ) -> None:
        check = (
            (
                await session.execute(
                    sa.text("SELECT merged_into, status FROM features WHERE id = :loser"),
                    {"loser": loser},
                )
            )
            .mappings()
            .one()
        )
        if str(check["merged_into"]) != str(into) or check["status"] != "archived":
            raise PostConditionError(f"merge {loser}: unexpected state {dict(check)!r}")
        remaining = (
            await session.execute(
                sa.text("SELECT COUNT(*) FROM feature_artifacts WHERE feature_id = :loser"),
                {"loser": loser},
            )
        ).scalar_one()
        if remaining != 0:
            raise PostConditionError(f"merge {loser}: {remaining} artifacts still on loser")

    async def _apply_archive(
        self,
        session: Any,
        row: Mapping[str, Any],
    ) -> dict[str, Any]:
        feature_id = row["feature_id"]
        proposal_id = int(row["id"])
        prior = self._require_live_feature(
            await self._lock_features(session, feature_id),
            feature_id,
            proposal_id=proposal_id,
            operation="archive",
            role="source",
            allow_pinned=False,
        )
        self._require_reviewed_state(
            row,
            prior,
            field="status_updated_at",
            role="source",
        )
        await session.execute(
            sa.text(
                "UPDATE features SET status = 'archived', status_updated_at = NOW() WHERE id = :fid"
            ),
            {"fid": feature_id},
        )
        check = await self._feature_state(session, feature_id, "status")
        if check["status"] != "archived":
            raise PostConditionError(f"archive {feature_id}: status={check['status']!r}")
        return {"op": "archive", "prior_status": prior["status"]}

    async def _apply_status(
        self,
        session: Any,
        row: Mapping[str, Any],
    ) -> dict[str, Any]:
        feature_id = row["feature_id"]
        proposal_id = int(row["id"])
        new_status = row["payload"]["status"]
        prior = self._require_live_feature(
            await self._lock_features(session, feature_id),
            feature_id,
            proposal_id=proposal_id,
            operation="status",
            role="source",
            allow_pinned=True,
        )
        self._require_reviewed_state(
            row,
            prior,
            field="status_updated_at",
            role="source",
        )
        await session.execute(
            sa.text("UPDATE features SET status = :s, status_updated_at = NOW() WHERE id = :fid"),
            {"s": new_status, "fid": feature_id},
        )
        check = await self._feature_state(session, feature_id, "status")
        if check["status"] != new_status:
            raise PostConditionError(f"status {feature_id}: status={check['status']!r}")
        return {"op": "status", "prior_status": prior["status"]}

    async def _apply_rename(
        self,
        session: Any,
        row: Mapping[str, Any],
    ) -> dict[str, Any]:
        feature_id = row["feature_id"]
        proposal_id = int(row["id"])
        new_name = row["payload"]["name"]
        prior = self._require_live_feature(
            await self._lock_features(session, feature_id),
            feature_id,
            proposal_id=proposal_id,
            operation="rename",
            role="source",
            allow_pinned=False,
        )
        self._require_reviewed_state(
            row,
            prior,
            field="updated_at",
            role="source",
        )
        await session.execute(
            sa.text("UPDATE features SET name = :n, updated_at = NOW() WHERE id = :fid"),
            {"n": new_name, "fid": feature_id},
        )
        check = await self._feature_state(session, feature_id, "name")
        if check["name"] != new_name:
            raise PostConditionError(f"rename {feature_id}: name={check['name']!r}")
        return {"op": "rename", "prior_name": prior["name"]}

    @staticmethod
    async def _lock_features(
        session: Any,
        *feature_ids: UUID,
    ) -> dict[UUID, Mapping[str, Any]]:
        """Lock feature rows in UUID order so competing proposals cannot deadlock."""
        ordered_ids = sorted(set(feature_ids), key=str)
        rows = (
            (
                await session.execute(
                    sa.text(
                        "SELECT id, project_key, status, name, merged_into, "
                        "COALESCE(pinned, false) AS pinned, status_updated_at, updated_at "
                        "FROM features "
                        "WHERE id = ANY(CAST(:feature_ids AS uuid[])) "
                        "ORDER BY id FOR UPDATE"
                    ),
                    {"feature_ids": ordered_ids},
                )
            )
            .mappings()
            .all()
        )
        return {UUID(str(item["id"])): cast(Mapping[str, Any], item) for item in rows}

    @staticmethod
    def _require_live_feature(
        locked: Mapping[UUID, Mapping[str, Any]],
        feature_id: UUID,
        *,
        proposal_id: int,
        operation: str,
        role: str,
        allow_pinned: bool,
    ) -> Mapping[str, Any]:
        feature = locked.get(feature_id)
        if feature is None:
            raise ProposalStateConflictError(
                f"{role} feature no longer exists; review the proposal again",
                family="roadmap-curation",
                proposal_id=proposal_id,
                operation=operation,
            )
        if feature["status"] in {"done", "archived"} or feature["merged_into"] is not None:
            raise ProposalStateConflictError(
                f"{role} feature is no longer live; review the proposal again",
                family="roadmap-curation",
                proposal_id=proposal_id,
                operation=operation,
            )
        if not allow_pinned and bool(feature["pinned"]):
            raise ProposalStateConflictError(
                f"{role} feature is pinned; only status changes are allowed",
                family="roadmap-curation",
                proposal_id=proposal_id,
                operation=operation,
            )
        return feature

    @staticmethod
    def _require_reviewed_state(
        proposal: Mapping[str, Any],
        feature: Mapping[str, Any],
        *,
        field: Literal["status_updated_at", "updated_at"],
        role: str,
    ) -> None:
        if feature[field] <= proposal["created_at"]:
            return
        raise ProposalStateConflictError(
            f"{role} feature changed since review; review the proposal again",
            family="roadmap-curation",
            proposal_id=int(proposal["id"]),
            operation=str(proposal["op"]),
        )

    @staticmethod
    async def _feature_state(
        session: Any,
        feature_id: UUID,
        field: Literal["name", "status"],
    ) -> Mapping[str, Any]:
        row = (
            (
                await session.execute(
                    sa.text(f"SELECT {field} FROM features WHERE id = :fid FOR UPDATE"),  # nosec B608 - field is a Literal["name", "status"] and its only three callers (_apply_archive, _apply_status, _apply_rename) pass the literal constant inline; the operation read from the proposal never reaches this parameter, and feature_id goes out as the :fid bind — exception reviewed on 2026-08-16, to be re-examined before 2026-09-30
                    {"fid": feature_id},
                )
            )
            .mappings()
            .one()
        )
        return cast(Mapping[str, Any], row)

    @staticmethod
    def _artifact_log(rows: list[Any]) -> list[dict[str, str]]:
        return [{"artifact_type": row[0], "artifact_id": str(row[1])} for row in rows]


__all__ = [
    "PostConditionError",
    "ProposalApplyError",
    "ProposalMutationResult",
    "ProposalNotFoundError",
    "ProposalNotProposedError",
    "ProposalOperationNotAllowedError",
    "ProposalStateConflictError",
    "ProposalService",
    "ProposalServiceError",
]
