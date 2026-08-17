"""Base Pydantic models and mixins."""

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, Field


class TimestampMixin(BaseModel):
    """Mixin providing created_at / updated_at with correct timezone-aware defaults."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DecayMixin(BaseModel):
    """Mixin for decay-related fields on searchable entities."""

    last_accessed_at: datetime | None = None
    access_count: int = 0
    freshness_status: str = "fresh"
    merged_into: UUID | None = None
