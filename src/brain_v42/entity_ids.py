"""UUID parsing and entity-id prefix resolution shared across layers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

# Git-style short-id bounds: below 8 hex chars collisions are too likely to be
# useful; 32 hex chars is a full UUID and belongs to parse_uuid().
_PREFIX_MIN_HEX = 8
_PREFIX_MAX_HEX = 31
_HEX_CHARS = frozenset("0123456789abcdef")


def parse_uuid(value: str | UUID | None) -> UUID | None:
    """Parse *value* as a UUID, returning None on any invalid input."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(value)
    except (ValueError, AttributeError):
        return None


def normalize_uuid_prefix(value: str | None) -> str | None:
    """Return the bare-hex lowercase form of a partial UUID, or None."""
    if not value:
        return None
    bare = value.strip().replace("-", "").lower()
    if not _PREFIX_MIN_HEX <= len(bare) <= _PREFIX_MAX_HEX:
        return None
    if not set(bare) <= _HEX_CHARS:
        return None
    return bare


async def resolve_entity_id(
    value: str,
    resolve_prefix: Callable[[str], Awaitable[list[UUID]]],
    *,
    label: str = "entity",
) -> UUID | str:
    """Resolve a full UUID or a unique git-style UUID prefix."""
    uid = parse_uuid(value)
    if uid is not None:
        return uid
    prefix = normalize_uuid_prefix(value)
    if prefix is None:
        return f"Invalid UUID: {value}"
    matches = await resolve_prefix(prefix)
    if not matches:
        return f"No {label} found for id prefix '{value}'"
    if len(matches) > 1:
        listed = ", ".join(str(match) for match in matches)
        return f"Ambiguous id prefix '{value}' — matches: {listed}. Use a longer prefix."
    return matches[0]
