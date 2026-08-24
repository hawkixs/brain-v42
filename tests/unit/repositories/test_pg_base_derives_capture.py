"""Le SITE d'appel de la dérivation, épinglé — sans ça un rebase l'emporterait en silence.

Avant ce module, `grep -rn derive_capture src/brain_v42/repositories/` rendait 0
et AUCUN test ne voyait le câblage disparaître : la suite serait restée verte sur
une capture dérivée qui n'existe plus. Un mécanisme dont la disparition ne fait
rougir personne n'est pas livré, il est espéré.

Deux propriétés, et la seconde est celle qui coûte : la création qu'on observe
doit rendre sa ligne QUOI QU'IL ARRIVE dans la dérivation.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from brain_v42.db.tables import learnings
from brain_v42.repositories.pg_base import BasePgRepository
from tests.unit.repositories.test_pg_brain_session import _make_session, _result


class _LearningsRepo(BasePgRepository):
    table = learnings


def _repo_and_row() -> tuple[_LearningsRepo, dict[str, Any], list[Any]]:
    row = {
        "id": uuid4(),
        "project_key": "brain-v42",
        "topic": "t",
        "insight": "i",
    }
    _session, statements, _cm, factory = _make_session(lambda _stmt: _result(row=row))
    return _LearningsRepo(factory), row, statements


async def test_create_derives_once_with_the_table_name_and_the_whole_returning_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Une fois, avec le nom de table et la ligne COMPLÈTE du RETURNING.

    La ligne complète, et pas ``data`` : le projet et l'identifiant sortent du
    RETURNING, donc passer les valeurs d'entrée dériverait sur un artefact dont
    l'id n'existe pas encore. L'argument reçu EST la ligne rendue — c'est aussi
    ce qui prouve que l'appel vient APRÈS sa matérialisation.
    """
    import brain_v42.repositories.pg_base as module

    seen: list[tuple[Any, str, Any]] = []

    async def _spy(session: Any, table_name: str, row: Any) -> None:
        seen.append((session, table_name, row))

    monkeypatch.setattr(module, "derive_capture", _spy)
    repo, row, _statements = _repo_and_row()

    created = await repo.create({"topic": "t", "insight": "i"})

    assert created == row
    assert len(seen) == 1
    _session, table_name, derived_row = seen[0]
    assert table_name == "learnings"
    assert derived_row == row
    assert derived_row is created, "la dérivation doit voir la ligne effectivement rendue"


async def test_create_returns_its_row_even_when_the_connection_lookup_explodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La garde qui compte : la vraie dérivation, avec le drapeau OUVERT, qui casse.

    ``_current_connection_id`` fait un import différé de ``brain_v42.provenance``.
    Hors du ``try``, un ``ImportError`` y remonterait dans l'appel observé —
    exactement ce que les gardes prétendent empêcher. Ce test le fait lever pour
    de vrai et exige quand même la ligne.
    """
    from unittest.mock import MagicMock

    import brain_v42.db.session_derived_capture as derived

    monkeypatch.setattr(
        derived,
        "get_settings",
        lambda: MagicMock(brain_session_derived_capture_enabled=True),
    )

    def _explode() -> str:
        raise ImportError("brain_v42.provenance est introuvable")

    monkeypatch.setattr(derived, "_current_connection_id", _explode)
    repo, row, _statements = _repo_and_row()

    assert await repo.create({"topic": "t", "insight": "i"}) == row
