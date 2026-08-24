"""Contrat SQL de l'auto-ouverture et de l'observation d'une traçante `agent`.

Le harnais compile les statements SQLAlchemy sans PostgreSQL : il prouve la
FORME émise, pas le comportement du moteur. La frontière réelle — l'index
UNIQUE **PARTIEL** qui arbitre le conflit — ne se prouve que contre une vraie
base ; ce qui se prouve ici est que le code la NOMME, et qu'il ne repart pas
sans dater ce qu'il vient de retrouver.

La 046 a livré cinq colonnes et un seul écrivain. `last_observed_at` était la
colonne sans écrivain qui compte : c'est la SEULE que la règle des 4 h du
balayage sait lire, donc la laisser NULL rendait M-G verte et inerte.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
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

NOW = datetime(2026, 8, 22, 9, 30, tzinfo=UTC)
_CONNECTION = "3f2b1a0c9d8e7f6a5b4c3d2e1f0a9b8c"


@dataclass(frozen=True)
class _Identity:
    """Miroir minimal d'`AutoOpenIdentity` — le dépôt ne connaît que ces champs."""

    project_key: str = "brain-v42"
    connection_id: str = _CONNECTION
    started_by_actor: str = "brain_v42"
    nature: str = "agent"
    intent: str | None = None


def _open_router(*, session_id: Any, focus: dict[str, Any] | None):
    def route(statement: Any) -> Any:
        if "from project_contexts" in _sql(statement):
            return _result(row=focus)
        return _result(scalar=session_id)

    return route


async def _auto_open(router: Any, identity: _Identity | None = None) -> tuple[Any, list[Any]]:
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    _, statements, _, factory = _make_session(router)
    opened = await PgBrainSessionRepo(factory).auto_open(identity or _Identity(), now=NOW)
    return opened, statements


class TestAutoOpen:
    async def test_a_single_upsert_carries_the_five_046_columns(self) -> None:
        session_id = uuid4()
        opened, statements = await _auto_open(
            _open_router(session_id=session_id, focus={"current_focus": "f", "focus_revision": 7})
        )

        assert opened == session_id
        insert = statements[-1]
        params = _params(insert)
        assert params["nature"] == "agent"
        assert params["connection_id"] == _CONNECTION
        assert params["started_by_actor"] == "brain_v42"
        # `intent` est le champ de JUGEMENT humain : le serveur n'en fabrique pas.
        assert params["intent"] is None
        assert params["last_observed_at"] == NOW

    async def test_the_conflict_is_inferred_on_the_partial_index_and_dates_the_row(self) -> None:
        """`DO UPDATE`, pas `DO NOTHING` : un conflit EST une observation.

        Et `WHERE status = 'open'` doit figurer dans l'inférence. Sans le
        prédicat, PostgreSQL ne peut pas désigner l'index partiel et lève —
        c'est la différence entre un chemin qui rouvre après une fermeture
        nocturne et un chemin qui échoue tous les matins.
        """
        sql = _sql(
            (
                await _auto_open(
                    _open_router(
                        session_id=uuid4(),
                        focus={"current_focus": "f", "focus_revision": 7},
                    )
                )
            )[1][-1]
        )
        assert "on conflict (project_key, connection_id) where status = 'open' do update" in sql
        assert "last_observed_at" in sql.split("do update")[1]
        assert "returning" in sql

    async def test_retrieving_an_existing_session_costs_no_second_round_trip(self) -> None:
        """TÉMOIN : exactement deux statements — le focus, puis l'upsert.

        L'ancienne forme suivait le `DO NOTHING` d'un `SELECT` pour retrouver
        l'id : deux allers-retours qui ne dataient rien. Compter les statements
        est la seule façon de prouver que ce `SELECT` a bien disparu.
        """
        _, statements = await _auto_open(
            _open_router(session_id=uuid4(), focus={"current_focus": "f", "focus_revision": 7})
        )
        assert len(statements) == 2
        assert "from brain_sessions" not in _sql(statements[0])

    async def test_a_conflict_on_an_operator_row_dates_nothing_and_returns_none(self) -> None:
        """Le conflit ne doit JAMAIS pouvoir dater une ligne non-`agent`.

        `observe()` porte déjà cette garde (`nature = 'agent'` DUR dans son
        WHERE). Le chemin du CONFLIT ne l'avait pas : un `DO UPDATE` sans garde
        re-daterait `last_heartbeat_at` sur une ligne `operator` à chaque appel
        d'outil. Or l'éligibilité 7 jours du balayage lit `last_heartbeat_at`
        **sans filtre de nature** — la seule exception ÉCRITE au covenant
        deviendrait donc inatteignable, et la ligne un fantôme immortel.

        Deux moitiés, et il faut les deux : la FORME émise doit nommer la garde,
        et le refus de PostgreSQL (aucune ligne rendue) doit se traduire par
        `None` sans qu'un rattrapage vienne dater quoi que ce soit derrière.
        """
        opened, statements = await _auto_open(
            _open_router(session_id=None, focus={"current_focus": "f", "focus_revision": 7})
        )
        assert opened is None, "un conflit refusé ne rend pas d'identifiant"
        assert len(statements) == 2, "aucun statement de rattrapage après un refus"

        action = _sql(statements[-1]).split("do update")[1]
        assert " where " in action, "le DO UPDATE ne porte aucune garde de nature"
        guard = action.split(" where ")[1]
        assert "nature" in guard
        assert "agent" in _params(statements[-1]).values()

    async def test_a_project_without_context_opens_nothing_and_writes_nothing(self) -> None:
        """Le serveur ne fabrique pas de projet : personne n'a rien nommé ici."""
        opened, statements = await _auto_open(_open_router(session_id=uuid4(), focus=None))
        assert opened is None
        assert len(statements) == 1


class TestObserve:
    async def _observe(self, *, found: Any) -> tuple[bool, list[Any]]:
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        _, statements, _, factory = _make_session(lambda _stmt: _result(scalar=found))
        alive = await PgBrainSessionRepo(factory).observe(uuid4(), now=NOW)
        return alive, statements

    async def test_observation_moves_both_clocks_and_leaves_updated_at_alone(self) -> None:
        """Deux horloges bougent, la troisième non — et chacune pour sa raison.

        `last_observed_at` nourrit la règle des 4 h. `last_heartbeat_at` évite le
        faux-mort du balayage 7 j sur une connexion qui vit plus d'une semaine
        sans qu'aucun humain ne rappelle de heartbeat. `updated_at` ne bouge
        PAS : observer n'est pas muter l'état déclaré, et en faire un signal
        d'activité rendrait creux tout contrôle qui s'y adosse.
        """
        alive, statements = await self._observe(found=uuid4())

        assert alive is True
        assert len(statements) == 1
        statement = statements[0]
        assert _is_update(statement, "brain_sessions")
        params = _params(statement)
        assert params["last_observed_at"] == NOW
        assert params["last_heartbeat_at"] == NOW
        assert "updated_at" not in _sql(statement).split("where")[0]

    async def test_the_predicate_can_never_reach_an_operator_session(self) -> None:
        """Garde DURE, pas redondance : une mémo empoisonnée ne doit rien dater.

        `nature = 'agent'` et `status = 'open'` sont dans le WHERE, donc une
        session `operator` — ou une session déjà terminale — est hors d'atteinte
        de ce chemin, quel que soit l'UUID qu'on lui présente.
        """
        _, statements = await self._observe(found=uuid4())
        where = _sql(statements[0]).split(" where ")[1]
        assert "brain_sessions.status = " in where
        assert "brain_sessions.nature = " in where
        params = _params(statements[0])
        assert params["status_1"] == "open"
        assert params["nature_1"] == "agent"

    async def test_no_row_means_closed_under_us_not_an_error(self) -> None:
        """`False` est un FAIT — la session a été fermée — pas une panne.

        C'est ce booléen qui fait jeter la mémo côté ouvreur et rouvrir. Le
        confondre avec une erreur ferait perdre la session de cette connexion.
        """
        alive, _ = await self._observe(found=None)
        assert alive is False


@pytest.mark.parametrize("column", ["last_observed_at", "last_heartbeat_at"])
async def test_both_writers_move_exactly_the_same_clocks(column: str) -> None:
    """L'upsert et l'observation doivent dater le MÊME ensemble de colonnes.

    Les laisser diverger donnerait à une connexion réidentifiée une horloge de
    présence différente de celle d'une connexion réobservée : deux régimes pour
    un seul geste, et un balayage qui lirait l'un ou l'autre selon le hasard du
    chemin emprunté.
    """
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    _, statements = await _auto_open(
        _open_router(session_id=uuid4(), focus={"current_focus": "f", "focus_revision": 7})
    )
    upsert_set = _sql(statements[-1]).split("do update set")[1]

    _, observe_statements, _, factory = _make_session(lambda _s: _result(scalar=uuid4()))
    await PgBrainSessionRepo(factory).observe(uuid4(), now=NOW)
    observe_set = _sql(observe_statements[0]).split(" set ")[1].split(" where ")[0]

    assert column in upsert_set
    assert column in observe_set
