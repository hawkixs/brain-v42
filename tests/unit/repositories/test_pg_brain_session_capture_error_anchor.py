"""Ancrage Phase 0 — la FORME ACTUELLE de l'erreur de capture, épinglée pour être cassée.

C'est la douleur **B13** : `_validate_captures` liste bien les ids rejetés, mais leur
attribue **une seule raison agrégée**. L'appelant qui soumet dix ids dont un mauvais
apprend que « des ids sont invalides » et doit deviner lequel a échoué pourquoi —
projet différent ? créé avant la session ? type ambigu ? inexistant ?

Ce test n'existe pas pour défendre ce comportement. Il existe pour que **la Phase 1 le
change en Red d'abord** : le composant D2 doit rendre `rejections: [{id, reason}]` avec
`reason ∈ {not_found, wrong_project, created_before_session, ambiguous_type,
attributed_elsewhere, unsupported_type}`, plus `capturable_subset`. Le jour où D2
arrive, **les assertions négatives de la fin de ce fichier tombent en premier** — c'est
leur seule raison d'être.

Sans cet ancrage, D2 pourrait changer la sémantique des lots acceptés/refusés en même
temps que la forme du message, et rien ne le verrait.

Harnais maison, sans PostgreSQL : mêmes doubles que
`tests/unit/repositories/test_pg_brain_session.py`, dont ce fichier réutilise les
helpers.
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
    """`_validate_captures` ne lit que ces deux attributs — le double le dit."""
    return SimpleNamespace(project_key=PROJECT, started_at=STARTED_AT)


def _router_finding(found: dict[UUID, str]):
    """Rendre `found` sur la table du type déclaré, rien sur les autres."""
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
    """La fenêtre est dans le SQL, pas dans une garde applicative."""
    _, statements = await _validate([], {})
    assert statements, "aucun statement émis"
    sql = _sql(statements[0])
    assert "project_key = " in sql, "l'égalité stricte de projet doit être dans le SQL"
    assert "created_at >= " in sql, "la borne created_at >= started_at doit être dans le SQL"
    assert "for share" in sql or "key share" in sql, "le verrou de lecture doit être posé"


@pytest.mark.asyncio
async def test_one_bad_id_rejects_the_whole_batch() -> None:
    """All-or-nothing : le lot mélangé est refusé EN BLOC (moitié de B4)."""
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


# ── LES ASSERTIONS QUI DOIVENT TOMBER EN PREMIER QUAND D2 ARRIVE ─────────────


@pytest.mark.asyncio
async def test_b13_today_all_rejected_ids_share_ONE_aggregated_reason() -> None:
    """B13, épinglée telle qu'elle est aujourd'hui.

    Trois ids rejetés pour des motifs potentiellement différents reçoivent la MÊME
    phrase, qui les énumère tous les motifs possibles sans dire lequel s'applique.
    **C'est le défaut**, pas le contrat souhaité.
    """
    ids = [uuid4() for _ in range(3)]
    with pytest.raises(BrainSessionInputError) as exc:
        await _validate(ids, {})
    message = str(exc.value)

    # Une seule phrase de motif, qui énumère les causes au lieu de les attribuer.
    assert message.count("invalid ids:") == 1, "une seule raison agrégée, pour tous les ids"
    assert "must exist in the same project" in message
    assert "created during the session" in message
    assert "unambiguous type" in message


@pytest.mark.asyncio
async def test_b13_today_the_error_carries_no_structured_rejections() -> None:
    """D2 ajoutera `rejections` et `capturable_subset`. Aujourd'hui : rien.

    Ces trois assertions sont les PREMIÈRES à tomber en Phase 1. Les inverser est le
    geste Red qui ouvre la livraison de D2 — pas un dégât collatéral à réparer après.
    """
    with pytest.raises(BrainSessionInputError) as exc:
        await _validate([uuid4()], {})
    error = exc.value
    assert not hasattr(error, "rejections"), "D2 pas encore livré : pas de rejections"
    assert not hasattr(error, "capturable_subset"), "D2 pas encore livré : pas de subset"
    for reason in ("not_found", "wrong_project", "created_before_session", "ambiguous_type"):
        assert reason not in str(error), f"D2 pas encore livré : pas de motif « {reason} »"
