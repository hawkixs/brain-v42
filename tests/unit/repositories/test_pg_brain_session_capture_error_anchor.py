"""Phase 0 anchor — the CURRENT SHAPE of the capture error, pinned to be broken.

This is pain point **B13**: `_validate_captures` does list the rejected ids, but gives
them **a single aggregated reason**. A caller submitting ten ids, one of them bad,
learns that "some ids are invalid" and has to guess which failed why — wrong project?
created before the session? ambiguous type? nonexistent?

This test does not exist to defend that behaviour. It exists so that **Phase 1 changes
it Red first**: component D2 must return `rejections: [{id, reason}]` with
`reason ∈ {not_found, wrong_project, created_before_session, ambiguous_type,
attributed_elsewhere, unsupported_type}`, plus `capturable_subset`. The day D2 lands,
**the negative assertions at the end of this file fall first** — that is their only
purpose.

Without this anchor, D2 could change the semantics of accepted/refused batches at the
same time as the message shape, and nothing would see it.

An in-house harness, without PostgreSQL: the same doubles as
`tests/unit/repositories/test_pg_brain_session.py`, whose helpers this file reuses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from brain_v42.models.brain_session import BrainSessionInputError
from tests.unit.repositories.test_pg_brain_session import _make_session, _result, _sql

STARTED_AT = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)
PROJECT = "brain-v42"


def _brain_session() -> Any:
    """`_validate_captures` reads only these two attributes — the double says so."""
    return SimpleNamespace(project_key=PROJECT, started_at=STARTED_AT)


def _router_finding(found: dict[UUID, str]):
    """Return `found` on the declared type's table, nothing on the others."""
    seen: list[str] = []

    def route(statement: Any):
        froms = statement.get_final_froms()
        table = froms[0].name if froms else ""
        seen.append(table)
        rows = [{"id": kid} for kid, ktype in found.items() if f"{ktype}s".startswith(table[:4])]
        return _result(rows=rows)

    return route


async def _validate(capture_ids: list[UUID], found: dict[UUID, str] | None = None):
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    session, statements, _, factory = _make_session(_router_finding(found or {}))
    repo = PgBrainSessionRepo(factory)
    result = await repo._validate_captures(session, _brain_session(), capture_ids)
    return result, statements


@pytest.mark.asyncio
async def test_the_capture_window_is_strict_project_and_created_after_start() -> None:
    """The window is in the SQL, not in an application-level guard."""
    _, statements = await _validate([], {})
    assert statements, "aucun statement émis"
    sql = _sql(statements[0])
    assert "project_key = " in sql, "l'égalité stricte de projet doit être dans le SQL"
    assert "created_at >= " in sql, "la borne created_at >= started_at doit être dans le SQL"
    assert "for share" in sql or "key share" in sql, "le verrou de lecture doit être posé"


@pytest.mark.asyncio
async def test_one_bad_id_rejects_the_whole_batch() -> None:
    """All-or-nothing: a mixed batch is refused WHOLESALE (half of B4)."""
    good, bad = uuid4(), uuid4()
    with pytest.raises(BrainSessionInputError) as exc:
        await _validate([good, bad], {good: "decision"})
    assert str(bad) in str(exc.value)


@pytest.mark.asyncio
async def test_rejected_ids_are_listed_and_sorted() -> None:
    ids = [uuid4() for _ in range(3)]
    with pytest.raises(BrainSessionInputError) as exc:
        await _validate(ids, {})
    message = str(exc.value)
    listed = sorted(str(i) for i in ids)
    assert all(item in message for item in listed)
    positions = [message.index(item) for item in listed]
    assert positions == sorted(positions), "les ids sont listés triés"


# ── THE ASSERTIONS THAT MUST FALL FIRST WHEN D2 LANDS ───────────────────────


@pytest.mark.asyncio
async def test_b13_today_all_rejected_ids_share_ONE_aggregated_reason() -> None:
    """B13, pinned exactly as it is today.

    Three ids rejected for potentially different reasons receive the SAME sentence,
    which enumerates every possible reason without saying which applies. **That is
    the defect**, not the desired contract.
    """
    ids = [uuid4() for _ in range(3)]
    with pytest.raises(BrainSessionInputError) as exc:
        await _validate(ids, {})
    message = str(exc.value)

    # A single reason sentence, enumerating the causes instead of assigning them.
    assert message.count("invalid ids:") == 1, "une seule raison agrégée, pour tous les ids"
    assert "must exist in the same project" in message
    assert "created during the session" in message
    assert "unambiguous type" in message


@pytest.mark.asyncio
async def test_b13_today_the_error_carries_no_structured_rejections() -> None:
    """D2 will add `rejections` and `capturable_subset`. Today: nothing.

    These three assertions are the FIRST to fall in Phase 1. Inverting them is the Red
    gesture that opens D2's delivery — not collateral damage to repair afterwards.
    """
    with pytest.raises(BrainSessionInputError) as exc:
        await _validate([uuid4()], {})
    error = exc.value
    assert not hasattr(error, "rejections"), "D2 pas encore livré : pas de rejections"
    assert not hasattr(error, "capturable_subset"), "D2 pas encore livré : pas de subset"
    for reason in ("not_found", "wrong_project", "created_before_session", "ambiguous_type"):
        assert reason not in str(error), f"D2 pas encore livré : pas de motif « {reason} »"
