from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from brain_v42.models.dream_promotion import (
    DreamPromotion,
    DreamPromotionCreate,
    DreamPromotionTargetType,
)


class TestDreamPromotionCreate:
    def test_adr_path_requires_target_adr_id(self) -> None:
        learning_id = uuid.uuid4()
        adr_id = uuid.uuid4()
        obj = DreamPromotionCreate(
            source_learning_id=learning_id,
            target_type=DreamPromotionTargetType.ADR,
            target_adr_id=adr_id,
        )
        assert obj.target_adr_id == adr_id
        assert obj.target_runbook_id is None

    def test_adr_path_rejects_missing_adr_id(self) -> None:
        with pytest.raises(ValidationError, match="target_adr_id"):
            DreamPromotionCreate(
                source_learning_id=uuid.uuid4(),
                target_type=DreamPromotionTargetType.ADR,
            )

    def test_adr_path_rejects_runbook_id(self) -> None:
        with pytest.raises(ValidationError, match="target_runbook_id"):
            DreamPromotionCreate(
                source_learning_id=uuid.uuid4(),
                target_type=DreamPromotionTargetType.ADR,
                target_adr_id=uuid.uuid4(),
                target_runbook_id=uuid.uuid4(),
            )

    def test_skipped_dedup_requires_cosine(self) -> None:
        obj = DreamPromotionCreate(
            source_learning_id=uuid.uuid4(),
            target_type=DreamPromotionTargetType.SKIPPED_DEDUP,
            cosine_observed=0.91,
        )
        assert obj.cosine_observed == pytest.approx(0.91)

    def test_skipped_dedup_rejects_target_ids(self) -> None:
        with pytest.raises(ValidationError):
            DreamPromotionCreate(
                source_learning_id=uuid.uuid4(),
                target_type=DreamPromotionTargetType.SKIPPED_DEDUP,
                target_adr_id=uuid.uuid4(),
                cosine_observed=0.91,
            )

    def test_skipped_reason_accepts_long_llm_justification(self) -> None:
        """LLM-generated skip reasons routinely exceed 100 chars (observed
        ~430 chars on Dream nights 2026-05-02 and 2026-05-03 — both crashed
        PROMOTE on StringDataRightTruncationError). The audit field must
        accept the full justification, not silently truncate or reject.
        """
        long_reason = (
            "Source is an exploration/brainstorm survey: enumerates "
            "architecture state, 21-tool inventory, and 6 LLM-UX pain "
            "points without selecting between alternatives (no ADR-shaped "
            "decision/alternatives) and without a sequential reproducible "
            "procedure (no runbook steps). Materializing either target "
            "would require fabricating content not substantively supported "
            "by the insight."
        )
        assert len(long_reason) > 100
        obj = DreamPromotionCreate(
            source_learning_id=uuid.uuid4(),
            target_type=DreamPromotionTargetType.CLASSIFICATION_UNCERTAIN,
            skipped_reason=long_reason,
        )
        assert obj.skipped_reason == long_reason


class TestDreamPromotion:
    def test_from_row(self) -> None:
        row = {
            "id": 42,
            "dream_run_id": 7,
            "source_learning_id": uuid.uuid4(),
            "target_type": "adr",
            "target_adr_id": uuid.uuid4(),
            "target_runbook_id": None,
            "cosine_observed": None,
            "skipped_reason": None,
            "created_at": datetime.now(UTC),
        }
        obj = DreamPromotion.model_validate(row)
        assert obj.id == 42
