"""Closed table selection and handler-compatible id parsing."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.repositories.pg_scope_lookup import ENTITY_TABLES, MISSING, ROW_TABLES, PgScopeLookup


def test_missing_row_has_a_public_type():
    from brain_v42.repositories import pg_scope_lookup

    assert isinstance(MISSING, pg_scope_lookup.Missing)


def lookup_fixture(*results):
    session = AsyncMock()
    session.execute.side_effect = results
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    return PgScopeLookup(lambda: context), session


@pytest.mark.parametrize("kind,table", ROW_TABLES.items())
async def test_each_kind_selects_only_its_fixed_table(kind, table):
    lookup, session = lookup_fixture(MagicMock(one_or_none=lambda: ("allowed",)))
    uid = uuid4()
    assert await lookup(kind, str(uid)) == "allowed"
    statement = session.execute.call_args.args[0]
    assert statement.get_final_froms() == [table]
    assert statement.compile().params == {"id_1": uid}


@pytest.mark.parametrize("kind", ["learning", "decision", "plan", "session", "slot", "claim"])
@pytest.mark.parametrize("bad_id", [None, "short", "abcdef12", "%' OR true --"])
async def test_non_prefix_handlers_never_accept_prefixes(kind, bad_id):
    lookup, session = lookup_fixture()
    assert await lookup(kind, bad_id) is MISSING
    session.execute.assert_not_awaited()


@pytest.mark.parametrize("matches", [[], [uuid4()], [uuid4(), uuid4()]])
async def test_runbook_prefix_uses_shared_resolution_before_ownership(matches):
    lookup, session = lookup_fixture(
        MagicMock(scalars=lambda: MagicMock(all=lambda: matches)),
        MagicMock(one_or_none=lambda: ("allowed",)),
    )
    assert await lookup("runbook_prefix", "a1b2c3d4") == (
        "allowed" if len(matches) == 1 else MISSING
    )
    assert session.execute.await_count == (2 if len(matches) == 1 else 1)


@pytest.mark.parametrize("kind", ENTITY_TABLES)
async def test_claim_follows_registry_reference_to_concrete_source(kind):
    uid = uuid4()
    lookup, session = lookup_fixture(
        MagicMock(one_or_none=lambda: SimpleNamespace(entity_type=kind, source_uuid=uid)),
        MagicMock(one_or_none=lambda: ("source-project",)),
    )
    assert await lookup("claim_source", str(uuid4())) == "source-project"
    first, second = (call.args[0] for call in session.execute.call_args_list)
    assert "brain_entities" in str(first)
    assert second.get_final_froms() == [ENTITY_TABLES[kind]]
    assert uid in second.compile().params.values()


@pytest.mark.parametrize("row,expected", [(None, MISSING), ((None,), None)])
async def test_absent_row_and_null_owner_are_distinct_inside_lookup(row, expected):
    lookup, _ = lookup_fixture(MagicMock(one_or_none=lambda: row))
    assert await lookup("learning", str(uuid4())) is expected
