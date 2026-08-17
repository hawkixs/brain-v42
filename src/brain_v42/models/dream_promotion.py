"""Pydantic models for dream_promotions audit rows.

dream_promotions records one row per PROMOTE-phase candidate evaluation.
Target-type coherence is enforced at both the Pydantic validation layer
(fast feedback during MCP tool calls) and the PG CHECK constraint
(defense in depth against direct SQL writes).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator


class DreamPromotionTargetType(StrEnum):
    ADR = "adr"
    RUNBOOK = "runbook"
    SKIPPED_DEDUP = "skipped_dedup"
    DRY_RUN = "dry_run"
    CLASSIFICATION_UNCERTAIN = "classification_uncertain"
    DEDUP_UNAVAILABLE = "dedup_unavailable"


class DreamPromotionCreate(BaseModel):
    """Payload to insert a dream_promotions row."""

    dream_run_id: int | None = None
    source_learning_id: UUID | None = None
    target_type: DreamPromotionTargetType
    target_adr_id: UUID | None = None
    target_runbook_id: UUID | None = None
    cosine_observed: float | None = None
    skipped_reason: str | None = None

    @model_validator(mode="after")
    def _enforce_target_shape(self) -> DreamPromotionCreate:
        t = self.target_type
        if t is DreamPromotionTargetType.ADR:
            if self.target_adr_id is None:
                raise ValueError("target_adr_id required when target_type='adr'")
            if self.target_runbook_id is not None:
                raise ValueError("target_runbook_id must be None when target_type='adr'")
        elif t is DreamPromotionTargetType.RUNBOOK:
            if self.target_runbook_id is None:
                raise ValueError("target_runbook_id required when target_type='runbook'")
            if self.target_adr_id is not None:
                raise ValueError("target_adr_id must be None when target_type='runbook'")
        else:
            if self.target_adr_id is not None or self.target_runbook_id is not None:
                raise ValueError(
                    f"target_adr_id/target_runbook_id must both be None "
                    f"when target_type={t.value!r}"
                )
        return self


class DreamPromotion(DreamPromotionCreate):
    """Hydrated dream_promotions row."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
