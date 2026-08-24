"""Contrat de `derive_capture` : déposer dans la traçante, ne jamais voler, ne jamais casser.

Le harnais compile les statements sans PostgreSQL : il prouve la FORME émise et
les chemins de refus, pas l'arbitrage du moteur. Ce qui ne se prouve qu'en base
— l'unicité de la traçante sur (projet, connexion) — est épinglé côté e2e.

Trois propriétés gouvernent ce module et se lisent une par une plus bas :

1. **Elle ne VOLE jamais.** L'insertion porte `ON CONFLICT DO NOTHING` sur
   `knowledge_id`, qui EST la clé primaire du ledger : un artefact déjà attribué
   — à une session explicite ou à une autre traçante — reste où il est.
2. **Elle ne casse JAMAIS la création qu'elle observe.** Tout passe par un
   `begin_nested()`, et toute exception est avalée. Une capture dérivée qui
   ferait échouer un `brain_learn` serait pire que pas de capture du tout.
3. **Fermée par défaut.** Drapeau fermé ⇒ zéro statement, pas « un statement qui
   ne fait rien ».
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from brain_v42.provenance import set_current_transport
from tests.unit.repositories.test_pg_brain_session import _params, _result, _sql

_CONNECTION = "9a8b7c6d5e4f30211122334455667788"
_PROJECT = "brain-v42"


@pytest.fixture(autouse=True)
def _connection() -> Any:
    set_current_transport(_CONNECTION)
    yield
    set_current_transport(None)


@pytest.fixture
def _open_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    import brain_v42.db.session_derived_capture as module

    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: MagicMock(brain_session_derived_capture_enabled=True),
    )


def _session(router: Any) -> tuple[Any, list[Any]]:
    """Session factice qui sait entrer dans un savepoint, comme la vraie."""
    statements: list[Any] = []

    async def execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
        statements.append(statement)
        return router(statement)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=execute)

    savepoint = MagicMock()
    savepoint.__aenter__ = AsyncMock(return_value=session)
    savepoint.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=savepoint)
    return session, statements


def _router(*, tracer: UUID | None, ledger_size: int = 0, inserted: UUID | None = None) -> Any:
    def route(statement: Any) -> Any:
        sql = _sql(statement)
        if "from brain_sessions" in sql:
            return _result(scalar=tracer)
        if "count(" in sql:
            return _result(scalar=ledger_size)
        return _result(scalar=inserted)

    return route


async def _derive(
    session: Any,
    *,
    table: str = "learnings",
    project_key: str | None = _PROJECT,
    knowledge_id: UUID | None = None,
) -> UUID | None:
    from brain_v42.db.session_derived_capture import derive_capture

    return await derive_capture(
        session,
        table,
        {"id": knowledge_id or uuid4(), "project_key": project_key},
    )


class TestItDeposits:
    async def test_one_row_lands_in_the_tracer_of_this_connection(self, _open_flag: None) -> None:
        tracer, artifact = uuid4(), uuid4()
        session, statements = _session(_router(tracer=tracer, inserted=artifact))

        derived = await _derive(session, knowledge_id=artifact)

        assert derived == artifact
        insert = statements[-1]
        assert "insert into brain_session_artifacts" in _sql(insert)
        params = _params(insert)
        assert params["session_id"] == tracer
        assert params["knowledge_id"] == artifact
        assert params["knowledge_type"] == "learning"

    async def test_the_write_lives_in_a_savepoint(self, _open_flag: None) -> None:
        """Sans savepoint, une erreur ici emporterait la transaction de création."""
        session, _ = _session(_router(tracer=uuid4(), inserted=uuid4()))
        await _derive(session)
        assert session.begin_nested.called

    async def test_the_tracer_is_looked_up_by_project_connection_open_and_agent(
        self, _open_flag: None
    ) -> None:
        """Quatre bornes, et aucune n'est décorative.

        Sans `nature = 'agent'`, la dérivation pourrait déposer dans une session
        `operator` — donc écrire dans la session d'un humain sans qu'il l'ait
        demandé. C'est exactement ce que l'absorption doit rester seule à faire.
        """
        session, statements = _session(_router(tracer=uuid4(), inserted=uuid4()))
        await _derive(session)

        lookup = _sql(statements[0])
        assert "from brain_sessions" in lookup
        values = set(_params(statements[0]).values())
        assert {_PROJECT, _CONNECTION, "open", "agent"} <= values


class TestItRefuses:
    async def test_a_closed_flag_emits_no_statement_at_all(self) -> None:
        session, statements = _session(_router(tracer=uuid4()))
        assert await _derive(session) is None
        assert statements == []
        assert not session.begin_nested.called

    async def test_no_connection_means_nothing_to_derive_into(self, _open_flag: None) -> None:
        """stdio et mode sans état : pas d'identifiant de connexion, pas de clé."""
        set_current_transport(None)
        session, statements = _session(_router(tracer=uuid4()))
        assert await _derive(session) is None
        assert statements == []

    async def test_no_tracer_writes_nothing(self, _open_flag: None) -> None:
        session, statements = _session(_router(tracer=None))
        assert await _derive(session) is None
        assert len(statements) == 1

    async def test_a_table_outside_the_capture_set_is_ignored(self, _open_flag: None) -> None:
        session, statements = _session(_router(tracer=uuid4()))
        assert await _derive(session, table="features") is None
        assert statements == []

    async def test_a_row_without_a_project_is_ignored(self, _open_flag: None) -> None:
        session, statements = _session(_router(tracer=uuid4()))
        assert await _derive(session, project_key=None) is None
        assert statements == []

    async def test_a_full_ledger_is_left_exactly_as_it_is(self, _open_flag: None) -> None:
        """Le plafond de 100 appartient à la capture explicite : on ne le franchit pas.

        Le dépasser par le chemin dérivé rendrait `brain_session_capture`
        refusable pour une raison que l'utilisateur n'a pas provoquée.
        """
        from brain_v42.models.brain_session import MAX_CAPTURED_KNOWLEDGE_IDS

        session, statements = _session(
            _router(tracer=uuid4(), ledger_size=MAX_CAPTURED_KNOWLEDGE_IDS)
        )
        assert await _derive(session) is None
        assert not any("insert into brain_session_artifacts" in _sql(s) for s in statements)

    async def test_an_already_attributed_artifact_is_never_stolen(self, _open_flag: None) -> None:
        """`ON CONFLICT DO NOTHING` sur la PK : la ligne existante gagne, toujours."""
        session, statements = _session(_router(tracer=uuid4(), inserted=None))
        assert await _derive(session) is None
        insert = _sql(statements[-1])
        assert "on conflict (knowledge_id) do nothing" in insert

    async def test_any_failure_is_swallowed_and_never_reaches_the_creation(
        self, _open_flag: None
    ) -> None:
        """Une capture dérivée qui casserait un `brain_learn` serait pire que rien."""

        def explode(_statement: Any) -> Any:
            raise RuntimeError("la base a hoqueté")

        session, _ = _session(explode)
        assert await _derive(session) is None


async def test_the_table_map_agrees_with_the_repository_capture_tables() -> None:
    """Anti-dérive, sans cycle d'import.

    `pg_brain_session` importe `pg_base`, et `pg_base` appellera ce module : lui
    importer `CAPTURE_TABLES` fermerait le cycle. Les deux listes vivent donc
    séparément, et ce test est ce qui les empêche de diverger en silence.
    """
    from brain_v42.db.session_derived_capture import CAPTURE_TABLES as derived
    from brain_v42.repositories.pg_brain_session import CAPTURE_TABLES as canonical

    assert derived == {table.name: knowledge_type for table, knowledge_type in canonical}
