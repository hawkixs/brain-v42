"""Base Pydantic models and mixins."""

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, Field


class TimestampMixin(BaseModel):
    """Mixin providing created_at / updated_at with correct timezone-aware defaults."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DecayMixin(BaseModel):
    """Mixin for decay-related fields on searchable entities.

    The ``_human`` pair is NOT decoration: ``decay_human_signal_enabled``
    substitutes it for the machine counters in the scoring loop
    (``brain_service``). Until 2026-08-22 these two fields were absent, so that
    substitution read ``getattr(entity, "access_count_human", 0)`` on a model
    that had no such attribute and silently fell back on the default — the flag
    switched between the machine signal and NOTHING, while ``DecayFlusher`` read
    the real columns in Core. Arming it would have made the two paths diverge on
    the same row. The columns were in the SELECT all along (``_search_columns``
    excludes only ``embedding`` and ``search_vector``); Pydantic was dropping
    them for want of a declared field.

    ``last_accessed_at_human`` is nullable and NOT backfilled — ``None`` means
    "never read by a human", which is exactly what the decay must weigh, and it
    must stay distinguishable from "read long ago" (migrations 041/044
    doctrine).
    """

    last_accessed_at: datetime | None = None
    access_count: int = 0
    last_accessed_at_human: datetime | None = None
    access_count_human: int = 0
    freshness_status: str = "fresh"
    merged_into: UUID | None = None
