"""Unit tests for IndexedPlanChunk and updated IndexedPlan models."""

from datetime import UTC, datetime
from uuid import uuid4

from brain_v42.models.indexed_plan import IndexedPlan, IndexedPlanCreate
from brain_v42.models.indexed_plan_chunk import (
    IndexedPlanChunk,
    IndexedPlanChunkCreate,
)


def test_indexed_plan_create_accepts_new_fields():
    payload = IndexedPlanCreate(
        file_path="docs/spec.md",
        title="My Spec",
        plan_type="spec",
        project_key="brain-v42",
        content_hash="a" * 64,
        content="# My Spec\n\n## Section\n\nBody.",
        summary="Short summary.",
        status="draft",
        tags=["architecture", "brain"],
        metadata={"author": "hawixs"},
        word_count=42,
    )
    assert payload.content.startswith("# My Spec")
    assert payload.status == "draft"
    assert payload.tags == ["architecture", "brain"]


def test_indexed_plan_chunk_create():
    payload = IndexedPlanChunkCreate(
        section_title="Section",
        section_path="Section",
        content="## Section\n\nBody.",
        section_order=0,
        word_count=5,
        project_key="brain-v42",
        plan_type="spec",
        status="active",
        tags=["architecture"],
    )
    assert payload.section_order == 0
    assert payload.plan_type == "spec"
    assert payload.plan_id is None  # plan_id is optional — repo fills it in


def test_indexed_plan_chunk_create_with_plan_id():
    plan_uuid = uuid4()
    payload = IndexedPlanChunkCreate(
        plan_id=plan_uuid,
        section_title="Section",
        section_path="Section",
        content="## Section\n\nBody.",
        section_order=0,
        word_count=5,
        project_key="brain-v42",
        plan_type="spec",
    )
    assert payload.plan_id == plan_uuid


def test_parent_decay_fields_are_internal_and_excluded_from_public_dump():
    now = datetime.now(UTC)
    chunk = IndexedPlanChunk(
        id=uuid4(),
        plan_id=uuid4(),
        section_title="COR1",
        section_path="cor1",
        content="usage evidence",
        section_order=0,
        word_count=2,
        project_key="brain-v42",
        plan_type="plan",
        status="active",
        created_at=now,
        parent_access_count=17,
        parent_last_accessed_at=now,
        parent_freshness_status="stale",
        parent_created_at=now,
    )

    assert chunk.parent_access_count == 17
    assert chunk.parent_freshness_status == "stale"
    public = chunk.model_dump(mode="json")
    assert "parent_access_count" not in public
    assert "parent_last_accessed_at" not in public
    assert "parent_freshness_status" not in public
    assert "parent_created_at" not in public


def test_indexed_plan_exposes_new_fields():
    from datetime import UTC, datetime

    plan = IndexedPlan(
        id=uuid4(),
        file_path="a.md",
        title="A",
        plan_type="spec",
        project_key="brain-v42",
        content_hash="h" * 64,
        content="# A",
        status="active",
        chunk_count=0,
        word_count=1,
        freshness_status="fresh",
        indexed_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    assert plan.status == "active"
    assert plan.chunk_count == 0
    assert plan.tags == []
    assert plan.metadata == {}
