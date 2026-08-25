"""Contrat de `derive_capture` : déposer dans la traçante, ne jamais voler, ne jamais casser.

Le harnais compile les statements sans PostgreSQL : il prouve la FORME émise et
les chemins de refus, pas l'arbitrage du moteur. Ce qui ne se prouve qu'en base
— l'unicité de la traçante sur (projet, connexion) — est épinglé côté e2e.

Trois propriétés gouvernent ce module et se lisent une par une plus bas :

1. **Elle ne VOLE jamais.** L'insertion porte `ON CONFLICT DO NOTHING` sur
   `knowledge_id`, qui EST la clé primaire du ledger : un artefact déjà attribué
   — à une session explicite ou à une autre traçante — reste où il est.
2. **Elle ne casse pas la création qu'elle observe.** Tout passe par un
   `begin_nested()`, et toute `Exception` est avalée. « Pas » et non « jamais » :
   `except Exception` n'attrape pas `BaseException`, et une `CancelledError`
   pendant le `ROLLBACK TO SAVEPOINT` reste hors garantie. Fenêtre étroite,
   mais l'écrire coûte moins cher que de laisser croire à une garantie totale.
3. **Fermée par défaut.** Drapeau fermé ⇒ zéro statement, pas « un statement qui
   ne fait rien ».
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
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


# ---------------------------------------------------------------------------
# absorb_tracer_ledger
# ---------------------------------------------------------------------------

_STARTED_AT = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _Target:
    """Miroir minimal de `BrainSession` — l'absorption ne lit que ces trois champs."""

    id: UUID
    project_key: str = _PROJECT
    started_at: datetime = _STARTED_AT


def _absorb_router(
    *, tracer: UUID | None, occupied: int = 0, candidates: list[UUID] | None = None
) -> Any:
    moved = candidates if candidates is not None else []

    def route(statement: Any) -> Any:
        sql = _sql(statement)
        if "from brain_sessions" in sql:
            return _result(scalar=tracer)
        if "count(" in sql:
            return _result(scalar=occupied)
        return _result(rows=[{"knowledge_id": item} for item in moved])

    return route


async def _absorb(
    session: Any, target: _Target | None = None, connection: str = _CONNECTION
) -> int:
    """Le TOTAL déplacé, tous étages confondus.

    `absorb_tracer_ledger` rend désormais un `AbsorptionOutcome` — un total nu
    ne pouvait pas dire par quelle clé l'appariement avait eu lieu. Ce helper
    garde le contrat que les tests ci-dessous asserent : le nombre de lignes
    déplacées. Les tests par étage vivent dans `TestTwoStageAbsorption`.
    """
    from brain_v42.db.session_derived_capture import absorb_tracer_ledger

    outcome = await absorb_tracer_ledger(session, target or _Target(id=uuid4()), connection)
    return outcome.total


class TestAbsorption:
    async def test_it_moves_the_tracer_rows_onto_the_target_and_counts_them(
        self, _open_flag: None
    ) -> None:
        target, moved = _Target(id=uuid4()), [uuid4(), uuid4()]
        session, statements = _session(_absorb_router(tracer=uuid4(), candidates=moved))

        assert await _absorb(session, target) == 2
        update = statements[-1]
        assert "update brain_session_artifacts" in _sql(update)
        assert target.id in _params(update).values()

    async def test_the_donor_can_only_be_an_open_agent_tracer(self, _open_flag: None) -> None:
        """Le donneur est `agent` UNIQUEMENT : absorber une session `operator`
        déplacerait le ledger d'un humain vers un autre humain."""
        session, statements = _session(_absorb_router(tracer=uuid4(), candidates=[uuid4()]))
        await _absorb(session)

        values = set(_params(statements[0]).values())
        assert {_PROJECT, _CONNECTION, "open", "agent"} <= values

    async def test_it_accepts_only_what_an_explicit_capture_would_have_accepted(
        self, _open_flag: None
    ) -> None:
        """L'INVARIANT du lot : la dérivation n'est pas un passe-droit.

        `_validate_captures` borne une capture explicite à « même projet ET
        `created_at >= started_at` », sur six tables. L'absorption doit porter
        EXACTEMENT les mêmes bornes : sans ça, elle attribuerait des artefacts
        qu'un utilisateur n'aurait pas pu capturer lui-même, et la dérivation
        deviendrait un chemin plus permissif que la commande qu'elle remplace.
        """
        from brain_v42.db.session_derived_capture import CAPTURE_TABLES

        session, statements = _session(_absorb_router(tracer=uuid4(), candidates=[uuid4()]))
        await _absorb(session)

        # Les bornes voyagent dans le sous-select de l'UPDATE : une seule
        # instruction, comme la forme arbitrée le demandait.
        update = _sql(statements[-1])
        for table_name in CAPTURE_TABLES:
            assert f"from {table_name}" in update, f"{table_name} hors du périmètre"
        assert update.count("created_at >=") == len(CAPTURE_TABLES)
        assert _STARTED_AT in _params(statements[-1]).values()

    async def test_it_never_pushes_the_target_ledger_past_the_cap(self, _open_flag: None) -> None:
        from brain_v42.models.brain_session import MAX_CAPTURED_KNOWLEDGE_IDS

        session, statements = _session(
            _absorb_router(tracer=uuid4(), occupied=98, candidates=[uuid4(), uuid4()])
        )
        await _absorb(session)

        update = statements[-1]
        assert "limit" in _sql(update)
        assert MAX_CAPTURED_KNOWLEDGE_IDS - 98 in _params(update).values()

    async def test_a_full_target_ledger_absorbs_nothing(self, _open_flag: None) -> None:
        from brain_v42.models.brain_session import MAX_CAPTURED_KNOWLEDGE_IDS

        session, statements = _session(
            _absorb_router(tracer=uuid4(), occupied=MAX_CAPTURED_KNOWLEDGE_IDS)
        )
        assert await _absorb(session) == 0
        assert not any("update brain_session_artifacts" in _sql(s) for s in statements)

    async def test_a_closed_flag_emits_no_statement_at_all(self) -> None:
        session, statements = _session(_absorb_router(tracer=uuid4()))
        assert await _absorb(session) == 0
        assert statements == []

    async def test_no_connection_absorbs_nothing(self, _open_flag: None) -> None:
        """stdio et mode sans état : sans identifiant de connexion, pas de donneur."""
        session, statements = _session(_absorb_router(tracer=uuid4()))
        assert await _absorb(session, connection="") == 0
        assert statements == []

    async def test_no_tracer_absorbs_nothing(self, _open_flag: None) -> None:
        """Pas de traçante sur CETTE connexion ⇒ l'étage exact ne rend rien.

        AMENDÉ avec le lot à deux étages. La forme précédente asseyait
        `len(statements) == 1`, c'est-à-dire « après l'étage connexion, on
        s'arrête » — la conception même que ce lot répare, épinglée par un
        compteur. Ce qui reste asserté est le comportement, qui n'a pas changé :
        rien ne bouge, et l'étage EXACT est toujours évalué en PREMIER.
        """
        session, statements = _session(_absorb_router(tracer=None))
        assert await _absorb(session) == 0
        assert "connection_id" in _sql(statements[0])

    async def test_any_failure_is_swallowed(self, _open_flag: None) -> None:
        def explode(_statement: Any) -> Any:
            raise RuntimeError("la base a hoqueté")

        session, _ = _session(explode)
        assert await _absorb(session) == 0


class TestRepositoryEntryPoint:
    """Le point d'entrée du dépôt ne décide rien — il retrouve et délègue."""

    async def _absorb_via_repo(
        self, monkeypatch: pytest.MonkeyPatch, *, row: dict[str, Any] | None
    ) -> tuple[int, list[Any]]:
        import brain_v42.db.session_derived_capture as module
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo
        from tests.unit.repositories.test_pg_brain_session import _make_session

        seen: list[Any] = []

        async def _fake(session: Any, target: Any, connection_id: str) -> Any:
            # Le double MIROITE la signature du vrai appelé, sinon il prouverait
            # un contrat que plus personne n'honore.
            from brain_v42.db.session_derived_capture import AbsorptionOutcome

            seen.append((target, connection_id))
            return AbsorptionOutcome(reason="absorbed", moved_by_connection=3)

        monkeypatch.setattr(module, "absorb_tracer_ledger", _fake)
        _, _statements, _, factory = _make_session(lambda _stmt: _result(row=row))
        moved = await PgBrainSessionRepo(factory).absorb_derived_capture(uuid4(), _CONNECTION)
        return moved, seen

    async def test_it_delegates_with_the_target_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session_id = uuid4()
        moved, seen = await self._absorb_via_repo(
            monkeypatch,
            row={"id": session_id, "project_key": _PROJECT, "started_at": _STARTED_AT},
        )

        assert moved == 3
        ((target, connection_id),) = seen
        assert (target.id, target.project_key, target.started_at) == (
            session_id,
            _PROJECT,
            _STARTED_AT,
        )
        assert connection_id == _CONNECTION

    async def test_an_unknown_session_absorbs_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        moved, seen = await self._absorb_via_repo(monkeypatch, row=None)
        assert moved == 0
        assert seen == []


# ---------------------------------------------------------------------------
# Absorption à DEUX étages — l'étage exact, puis la fenêtre d'exclusivité
# ---------------------------------------------------------------------------

_WINDOW_TRACER = uuid4()


def _two_stage_router(
    *,
    connection_tracer: UUID | None = None,
    occupied: int = 0,
    connection_moved: list[UUID] | None = None,
    window_moved: list[UUID] | None = None,
    blocked: int = 0,
) -> Any:
    """Router qui sait distinguer les DEUX étages, ce que le total masquerait."""

    def route(statement: Any) -> Any:
        sql = _sql(statement)
        if "count(" in sql and "brain_session_artifacts" in sql and "update" not in sql:
            return _result(scalar=blocked if "rival" in sql else occupied)
        if sql.startswith("select") and "from brain_sessions" in sql:
            return _result(scalar=connection_tracer)
        if "update brain_session_artifacts" in sql:
            # L'étage se lit à l'alias `rival` : `attribution_mode` voyage en
            # PARAMÈTRE, donc sa valeur n'apparaît jamais dans le SQL compilé.
            moved = window_moved if "rival" in sql else connection_moved
            return _result(rows=[{"knowledge_id": item} for item in (moved or [])])
        return _result(rows=[])

    return route


def _window_sql(statements: list[Any]) -> str:
    """Le SQL des statements de l'étage FENÊTRE, reconnus à leur alias `rival`."""
    return " ".join(_sql(item) for item in statements if "rival" in _sql(item))


def _window_values(statements: list[Any]) -> set[Any]:
    """Les VALEURS liées de l'étage fenêtre.

    Elles ne sont pas dans le texte compilé : SQLAlchemy les passe en
    paramètres. Les chercher dans le SQL rendrait ces tests verts pour une
    mauvaise raison — ils ne verraient jamais rien.
    """
    values: set[Any] = set()
    for item in statements:
        if "rival" not in _sql(item):
            continue
        for value in _params(item).values():
            # Un `IN (...)` voyage comme une LISTE de paramètres, pas comme
            # autant de scalaires : ne pas l'aplatir laisserait `closed_inactive`
            # invisible et le test vert sans rien avoir vu.
            values.update(value) if isinstance(value, list) else values.add(value)
    return values


async def _outcome(session: Any, target: _Target | None = None) -> Any:
    from brain_v42.db.session_derived_capture import absorb_tracer_ledger

    return await absorb_tracer_ledger(session, target or _Target(id=uuid4()), _CONNECTION)


class TestTwoStageAbsorption:
    """Un total qui masque une rétrogradation de l'étage exact vers la devinette
    est un vert qui ment. L'absorption doit DIRE par quelle clé elle a apparié."""

    async def test_it_counts_each_stage_separately(self, _open_flag: None) -> None:
        moved = [uuid4()]
        session, _ = _session(_two_stage_router(connection_tracer=uuid4(), connection_moved=moved))

        outcome = await _outcome(session)

        assert (outcome.moved_by_connection, outcome.moved_by_window) == (1, 0)
        assert outcome.total == 1
        assert outcome.reason == "absorbed"

    async def test_the_window_stage_is_reported_as_such(self, _open_flag: None) -> None:
        """Le même total, une clé différente — et le rapport doit le dire."""
        moved = [uuid4()]
        session, _ = _session(_two_stage_router(connection_tracer=None, window_moved=moved))

        outcome = await _outcome(session)

        assert (outcome.moved_by_connection, outcome.moved_by_window) == (0, 1)
        assert outcome.total == 1

    async def test_the_connection_stage_still_demands_an_open_tracer(
        self, _open_flag: None
    ) -> None:
        """L'étage EXACT ne change pas : il reste borné à la connexion courante."""
        session, statements = _session(
            _two_stage_router(connection_tracer=uuid4(), connection_moved=[uuid4()])
        )
        await _outcome(session)

        exact = _sql(statements[0])
        assert "connection_id" in exact
        assert "status" in exact and "nature" in exact

    async def test_the_window_stage_accepts_a_closed_inactive_donor(self, _open_flag: None) -> None:
        """Le balayage 4 h sort une traçante de `open` EN GARDANT son ledger.

        Un correctif borné à `'open'` redeviendrait muet le jour où le placement
        du drop-in `BRAIN_SESSION_INACTIVE_SWEEP_ENABLED` sera corrigé — sans un
        bruit, parce que rien n'échouerait.
        """
        session, statements = _session(_two_stage_router(window_moved=[uuid4()]))
        await _outcome(session)

        values = _window_values(statements)
        assert "closed_inactive" in values, "l'étage fenêtre ignore les traçantes balayées"
        assert "open" in values

    async def test_the_window_stage_never_absorbs_a_system_actor(self, _open_flag: None) -> None:
        """Le dream n'est pas un créateur inconnu : il est identifié.

        Le laisser dans le pot commun rendrait le mode de panne quotidien au
        lieu de marginal — le `promote` de 03:00 tombe dans la fenêtre de toute
        session ouverte la nuit.
        """
        session, statements = _session(_two_stage_router(window_moved=[uuid4()]))
        await _outcome(session)

        assert "started_by_actor" in _window_sql(statements), (
            "aucun filtre d'acteur sur l'étage fenêtre"
        )
        assert any(
            isinstance(value, str) and value.startswith("dream-")
            for value in _window_values(statements)
        ), "le préfixe des acteurs système n'est pas exclu"

    async def test_the_window_stage_refuses_what_a_rival_session_covers(
        self, _open_flag: None
    ) -> None:
        """La rivalité est SYMÉTRIQUE : aucune clause de fraîcheur.

        Deux prétendantes valent une abstention. La couverture se juge à
        l'instant de création, `started_at <= t <= coalesce(ended_at, now())`,
        donc une session close APRÈS l'instant reste une rivale.
        """
        session, statements = _session(_two_stage_router(window_moved=[uuid4()]))
        await _outcome(session)

        window = _window_sql(statements)
        assert "not (exists" in window or "not exists" in window
        assert "ended_at" in window and "started_at" in window


class TestTheThreeZerosAreDistinguishable:
    """« Refus légitime » et « rien à absorber » rendaient le même `0`.

    C'est le fil rouge de ce projet : une capacité armée, verte, et muette là où
    elle échoue. Trois retours `0` indiscernables en API comme au journal sont
    la prochaine régression invisible.
    """

    async def test_a_closed_flag_says_so(self) -> None:
        session, statements = _session(_two_stage_router())
        outcome = await _outcome(session)

        assert outcome.total == 0
        assert outcome.reason == "disabled"
        assert statements == [], "drapeau fermé ⇒ zéro statement, pas un statement inutile"

    async def test_no_connection_is_not_a_closed_flag(self, _open_flag: None) -> None:
        from brain_v42.db.session_derived_capture import absorb_tracer_ledger

        session, _ = _session(_two_stage_router())
        outcome = await absorb_tracer_ledger(session, _Target(id=uuid4()), "")

        assert outcome.total == 0
        assert outcome.reason == "no_connection"

    async def test_nothing_eligible_is_not_a_refusal(self, _open_flag: None) -> None:
        """Aucune ligne à déplacer n'est pas la même chose qu'un refus de la règle."""
        session, _ = _session(_two_stage_router(connection_tracer=None))
        outcome = await _outcome(session)

        assert outcome.total == 0
        assert outcome.reason == "nothing_to_absorb"

    async def test_an_ambiguous_window_says_ambiguous_and_counts_its_rivals(
        self, _open_flag: None
    ) -> None:
        session, _ = _session(_two_stage_router(blocked=2))
        outcome = await _outcome(session)

        assert outcome.total == 0
        assert outcome.reason == "ambiguous"
        assert outcome.rivals == 2

    async def test_a_full_ledger_is_its_own_reason(self, _open_flag: None) -> None:
        session, _ = _session(_two_stage_router(connection_tracer=uuid4(), occupied=100))
        outcome = await _outcome(session)

        assert outcome.total == 0
        assert outcome.reason == "ledger_full"
