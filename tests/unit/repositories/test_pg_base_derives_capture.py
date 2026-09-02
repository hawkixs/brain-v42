"""Derivation's CALL SITE, pinned — without this a rebase would take it silently.

Before this module, `grep -rn derive_capture src/brain_v42/repositories/`
returned 0 and NO test saw the wiring disappear: the suite would have stayed
green over a derived capture that no longer exists. A mechanism whose
disappearance makes nobody red is not delivered, it is hoped for.

Two properties, and the second is the one that costs: the creation being observed
must return its row WHATEVER HAPPENS in the derivation.
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
    """Once, with the table name and the COMPLETE row from the RETURNING.

    The complete row, and not ``data``: the project and the identifier come out
    of the RETURNING, so passing the input values would derive on an artifact
    whose id does not exist yet. The argument received IS the returned row —
    which is also what proves the call comes AFTER its materialization.
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
    """The guard that matters: the real derivation, flag OPEN, breaking.

    ``_current_connection_id`` performs a deferred import of
    ``brain_v42.provenance``. Outside the ``try``, an ``ImportError`` would
    surface there in the observed call — exactly what the guards claim to
    prevent. This test makes it raise for real and demands the row anyway.
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
