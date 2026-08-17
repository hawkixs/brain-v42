"""Pydantic model for entity relation input validation."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, field_validator


class RelationInput(BaseModel):
    """Validated relation input for the related_to parameter on write tools."""

    id: str
    type: Literal["MOTIVATED_BY", "IMPLEMENTS", "DOCUMENTS", "USES", "RELATED_TO"]

    @field_validator("id")
    @classmethod
    def validate_uuid(cls, v: str) -> str:
        UUID(v)  # raises ValueError if invalid
        return v
