"""Contrat unitaire du balayage serveur des sessions sans signe de vie.

Le harnais compile les statements SQLAlchemy sans PostgreSQL : il prouve la
FORME du prédicat et le fait que le DRY n'émet aucun UPDATE. La frontière
réelle du prédicat (N-1 / N+1 jour) ne se prouve que contre une vraie base :
elle vit dans tests/integration/db/test_brain_sessions_sweep.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from tests.unit.repositories.test_pg_brain_session import (
    _is_update,
    _make_session,
    _params,
    _result,
    _sql,
)

NOW = datetime(2026, 8, 7, 6, 0, tzinfo=UTC)


def _stale_row(*, project_key: str = "auto-discord", days: float = 24.1) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "project_key": project_key,
        "client_key": "codex-factory-28aeb338",
        "last_heartbeat_at": NOW - timedelta(days=days),
    }


def _router(rows: list[dict[str, Any]]):
    def route(statement: Any):
        return _result(rows=rows)

    return route


@pytest.mark.asyncio
async def test_dry_run_selects_and_never_updates() -> None:
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    row = _stale_row()
    _, statements, _, factory = _make_session(_router([row]))

    result = await PgBrainSessionRepo(factory).abandon_stale(dry_run=True, now=NOW)

    assert [candidate.project_key for candidate in result.candidates] == ["auto-discord"]
    assert result.dry_run is True
    assert result.abandoned_count == 0
    assert not [stmt for stmt in statements if _is_update(stmt, "brain_sessions")]
    assert len(statements) == 1
    assert _sql(statements[0]).startswith("select")


@pytest.mark.asyncio
async def test_wet_run_updates_in_a_single_statement() -> None:
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    row = _stale_row()
    _, statements, _, factory = _make_session(_router([row]))

    result = await PgBrainSessionRepo(factory).abandon_stale(dry_run=False, now=NOW)

    assert result.dry_run is False
    assert result.abandoned_count == 1
    updates = [stmt for stmt in statements if _is_update(stmt, "brain_sessions")]
    assert len(updates) == 1
    assert len(statements) == 1, "un seul statement : pas de fenêtre SELECT-puis-UPDATE"
    sql = _sql(updates[0])
    assert "returning" in sql
    assert "status" in sql and "abandonment_reason" in sql and "ended_at" in sql
    assert "summary" not in sql
    assert "next_focus" not in sql
    assert "project_contexts" not in sql


@pytest.mark.asyncio
async def test_cutoff_is_now_minus_threshold_and_strict() -> None:
    from brain_v42.models.brain_session import AUTO_STALE_AFTER
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    _, statements, _, factory = _make_session(_router([]))

    result = await PgBrainSessionRepo(factory).abandon_stale(dry_run=True, now=NOW)

    assert AUTO_STALE_AFTER == timedelta(days=7)
    assert result.cutoff == NOW - timedelta(days=7)
    sql = _sql(statements[0])
    assert "status =" in sql
    assert "last_heartbeat_at <" in sql
    assert "last_heartbeat_at <=" not in sql


@pytest.mark.asyncio
async def test_default_reason_is_the_auto_constant() -> None:
    from brain_v42.models.brain_session import AUTO_STALE_ABANDONMENT_REASON
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    _, statements, _, factory = _make_session(_router([_stale_row()]))

    await PgBrainSessionRepo(factory).abandon_stale(dry_run=False, now=NOW)

    assert AUTO_STALE_ABANDONMENT_REASON == "auto_stale_7d"
    assert "auto_stale_7d" in _params(statements[0]).values()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "   "])
async def test_blank_reason_is_refused(bad: str) -> None:
    from brain_v42.models.brain_session import BrainSessionInputError
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    _, _, _, factory = _make_session(_router([]))

    with pytest.raises(BrainSessionInputError):
        await PgBrainSessionRepo(factory).abandon_stale(reason=bad, dry_run=False, now=NOW)


@pytest.mark.asyncio
async def test_non_positive_threshold_is_refused() -> None:
    from brain_v42.models.brain_session import BrainSessionInputError
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    _, _, _, factory = _make_session(_router([]))

    with pytest.raises(BrainSessionInputError):
        await PgBrainSessionRepo(factory).abandon_stale(
            older_than=timedelta(0), dry_run=False, now=NOW
        )
