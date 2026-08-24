"""`end` MESURE ce qui est resté hors de tout ledger, au lieu de l'exiger.

Le XOR demandait à l'utilisateur de DÉCLARER sa diligence. Ce compteur ne
demande rien : il dit combien d'artefacts du projet, créés pendant la session,
n'appartiennent à AUCUN ledger. C'est une observation, pas une porte — et c'est
la différence entre informer et punir.

La propriété qui le rend non-influençable, et qu'un test dédié épingle :
**une session ne peut pas le faire baisser en ne faisant rien.** L'inaction
produit zéro artefact, donc zéro orphelin, donc zéro à afficher ; le compteur ne
descend qu'en attribuant réellement. Un compteur qu'on améliore en se taisant
serait exactement le reçu qu'on vient de retirer, sous un autre nom.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tests.unit.repositories.test_pg_brain_session import (
    _make_session,
    _params,
    _session_row,
    _sql,
    _terminal_router,
)


async def _end_with(unattributed: int, **overrides: object) -> tuple[object, list[object]]:
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    opened = _session_row()
    ended = _session_row(
        session_id=opened["id"],
        status="ended",
        summary="reviewed design",
        next_focus="implement tools",
        **overrides,  # type: ignore[arg-type]
    )
    _, statements, _, factory = _make_session(
        _terminal_router(
            opened,
            updated_row=ended,
            focus_row={"current_focus": "implement tools", "focus_revision": 8},
            current_focus_row={"current_focus": "old focus", "focus_revision": 7},
            remaining_open=0,
            unattributed=unattributed,
        )
    )
    result = await PgBrainSessionRepo(factory).end(
        opened["id"], "client-a", "reviewed design", "implement tools", 7, None
    )
    return result, statements


@pytest.mark.asyncio
async def test_end_reports_what_stayed_out_of_every_ledger() -> None:
    result, _statements = await _end_with(3)
    assert result.unattributed_in_window == 3  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_session_that_did_nothing_cannot_lower_the_count() -> None:
    """L'inaction ne produit rien à compter — donc zéro, jamais un progrès.

    C'est ce qui empêche le compteur de devenir un score : on ne l'améliore
    qu'en attribuant, jamais en se taisant.
    """
    result, _statements = await _end_with(0)
    assert result.unattributed_in_window == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_the_window_and_the_anti_join_are_both_in_the_query() -> None:
    """Bornes du comptage : le projet, la fenêtre, et « dans AUCUN ledger ».

    Sans l'anti-jointure il compterait tout ce que la session a produit, y
    compris ce qu'elle a attribué — le chiffre monterait quand on fait bien.
    """
    from brain_v42.repositories.pg_brain_session import CAPTURE_TABLES

    _result, statements = await _end_with(2)
    counts = [
        stmt
        for stmt in statements
        if "count(" in _sql(stmt) and "brain_session_artifacts" in _sql(stmt)
    ]
    assert counts, "aucune requête ne compte les artefacts hors ledger"
    query = _sql(counts[-1])
    for table, _knowledge_type in CAPTURE_TABLES:
        assert f"from {table.name}" in query, f"{table.name} hors du comptage"
    assert "not (exists" in query, "l'anti-jointure a disparu du comptage"
    assert "from brain_session_artifacts" in query
    assert "created_at >=" in query and "created_at <=" in query
    values = set(_params(counts[-1]).values())
    assert "brain-v42" in values


@pytest.mark.asyncio
async def test_the_counter_never_blocks_the_close() -> None:
    """Une mesure n'est pas une porte : elle ne peut pas refuser une fermeture."""
    result, _statements = await _end_with(97, captured_knowledge_ids=[uuid4()])
    assert result.session.status.value == "ended"  # type: ignore[attr-defined]
    assert result.unattributed_in_window == 97  # type: ignore[attr-defined]
