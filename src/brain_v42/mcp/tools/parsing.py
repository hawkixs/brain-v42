"""Compatibility reexports for the shared entity-id primitives."""

from __future__ import annotations

from brain_v42.entity_ids import normalize_uuid_prefix, parse_uuid, resolve_entity_id

__all__ = ("normalize_uuid_prefix", "parse_uuid", "resolve_entity_id")
