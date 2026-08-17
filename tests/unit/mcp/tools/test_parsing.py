"""Unit tests for parsing.py — UUID parsing and git-style id prefix resolution.

normalize_uuid_prefix is pure (no DB); resolve_entity_id takes an async
resolver callable so no repository is needed — everything mocked.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from brain_v42.mcp.tools.parsing import (
    normalize_uuid_prefix,
    parse_uuid,
    resolve_entity_id,
)


def test_legacy_parsing_module_reexports_canonical_entity_id_functions() -> None:
    from brain_v42.entity_ids import normalize_uuid_prefix as canonical_normalize_uuid_prefix
    from brain_v42.entity_ids import parse_uuid as canonical_parse_uuid
    from brain_v42.entity_ids import resolve_entity_id as canonical_resolve_entity_id

    assert normalize_uuid_prefix is canonical_normalize_uuid_prefix
    assert parse_uuid is canonical_parse_uuid
    assert resolve_entity_id is canonical_resolve_entity_id


# ---------------------------------------------------------------------------
# normalize_uuid_prefix — pure normalization
# ---------------------------------------------------------------------------


class TestNormalizeUuidPrefix:
    def test_bare_8_hex_chars_returned_as_is(self) -> None:
        assert normalize_uuid_prefix("61b0fa47") == "61b0fa47"

    def test_uppercase_is_lowercased(self) -> None:
        assert normalize_uuid_prefix("61B0FA47") == "61b0fa47"

    def test_hyphenated_prefix_has_hyphens_stripped(self) -> None:
        assert normalize_uuid_prefix("61b0fa47-5d31") == "61b0fa475d31"

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert normalize_uuid_prefix("  61b0fa47  ") == "61b0fa47"

    def test_31_hex_chars_accepted(self) -> None:
        prefix = "0" * 31
        assert normalize_uuid_prefix(prefix) == prefix

    def test_less_than_8_hex_chars_rejected(self) -> None:
        assert normalize_uuid_prefix("61b0fa4") is None

    def test_full_32_hex_uuid_rejected_belongs_to_parse_uuid(self) -> None:
        assert normalize_uuid_prefix("61b0fa475d3140aeaa56d7345176c865") is None

    def test_non_hex_chars_rejected(self) -> None:
        assert normalize_uuid_prefix("61b0fa4z") is None

    def test_like_wildcards_rejected(self) -> None:
        assert normalize_uuid_prefix("61b0fa4%") is None
        assert normalize_uuid_prefix("61b0fa4_") is None

    def test_none_rejected(self) -> None:
        assert normalize_uuid_prefix(None) is None

    def test_empty_string_rejected(self) -> None:
        assert normalize_uuid_prefix("") is None


# ---------------------------------------------------------------------------
# resolve_entity_id — exact parse with prefix-resolution fallback
#
# Contract: returns the resolved UUID, or the error message as a str —
# call sites isinstance-check, which mypy narrows exactly (a (uuid, error)
# tuple decorrelates under unpacking and forces ignores at every call site).
# ---------------------------------------------------------------------------


class TestResolveEntityId:
    async def test_full_uuid_resolves_without_calling_resolver(self) -> None:
        uid = uuid.uuid4()
        resolver = AsyncMock()

        assert await resolve_entity_id(str(uid), resolver) == uid
        resolver.assert_not_awaited()

    async def test_unique_prefix_match_resolves(self) -> None:
        uid = uuid.UUID("61b0fa47-5d31-40ae-aa56-d7345176c865")
        resolver = AsyncMock(return_value=[uid])

        assert await resolve_entity_id("61b0fa47", resolver) == uid
        resolver.assert_awaited_once_with("61b0fa47")

    async def test_hyphenated_prefix_is_normalized_before_resolution(self) -> None:
        uid = uuid.UUID("61b0fa47-5d31-40ae-aa56-d7345176c865")
        resolver = AsyncMock(return_value=[uid])

        assert await resolve_entity_id("61B0FA47-5d31", resolver) == uid
        resolver.assert_awaited_once_with("61b0fa475d31")

    async def test_no_match_returns_error_with_label(self) -> None:
        resolver = AsyncMock(return_value=[])

        result = await resolve_entity_id("61b0fa47", resolver, label="runbook")

        assert result == "No runbook found for id prefix '61b0fa47'"

    async def test_ambiguous_prefix_lists_full_uuids(self) -> None:
        a = uuid.UUID("61b0fa47-0000-4000-8000-000000000000")
        b = uuid.UUID("61b0fa47-ffff-4fff-8fff-ffffffffffff")
        resolver = AsyncMock(return_value=[a, b])

        result = await resolve_entity_id("61b0fa47", resolver)

        assert isinstance(result, str)
        assert str(a) in result
        assert str(b) in result
        assert "Ambiguous" in result
        assert "longer prefix" in result

    async def test_garbage_returns_invalid_uuid_without_calling_resolver(self) -> None:
        resolver = AsyncMock()

        result = await resolve_entity_id("not-a-uuid", resolver)

        assert result == "Invalid UUID: not-a-uuid"
        resolver.assert_not_awaited()

    async def test_too_short_hex_returns_invalid_uuid(self) -> None:
        resolver = AsyncMock()

        result = await resolve_entity_id("61b0", resolver)

        assert result == "Invalid UUID: 61b0"
        resolver.assert_not_awaited()


# ---------------------------------------------------------------------------
# parse_uuid — existing contract untouched (regression guard)
# ---------------------------------------------------------------------------


class TestParseUuidRegression:
    def test_canonical_uuid_still_parses(self) -> None:
        uid = uuid.uuid4()
        assert parse_uuid(str(uid)) == uid

    def test_prefix_still_returns_none(self) -> None:
        # Prefix acceptance lives in resolve_entity_id, NOT in parse_uuid.
        assert parse_uuid("61b0fa47") is None
