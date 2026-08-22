"""Contrat unitaire du balayage serveur — DEUX règles, UN statement, une préséance.

Le harnais compile les statements SQLAlchemy sans PostgreSQL : il prouve la
FORME du prédicat et le fait que le DRY n'émet aucun UPDATE. La frontière
réelle du prédicat (N-1 / N+1 jour) ne se prouve que contre une vraie base :
elle vit dans tests/integration/db/test_brain_sessions_sweep.py.

**Ce que M-G ajoute, et ce que ce fichier doit donc garder :**

- la règle des 4 h ne prend QUE des traçantes `nature = 'agent'` ;
- elle ne prend JAMAIS une session dont `last_observed_at` est NULL (S3,
  tranché) — `NULL` veut dire « jamais observée », pas « observée il y a
  longtemps » ;
- la règle 7 j PRIME sur la règle 4 h, parce qu'une traçante inactive depuis
  plus de sept jours matche les DEUX ;
- drapeau fermé ⇒ le prédicat est celui d'AVANT la 046, au caractère près ;
- et tout cela reste UN SEUL statement : la fenêtre `SELECT`-puis-`UPDATE` que
  le faux-mort du 2026-08-06 a coûtée ne doit pas se rouvrir par la porte de M-G.
"""

from __future__ import annotations

import re
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
FOUR_HOURS = timedelta(hours=4)


def _where(statement: Any) -> str:
    """Le SEUL prédicat, sans le RETURNING.

    `last_observed_at` figure aussi dans la projection : lire « tout ce qui suit
    WHERE » ferait passer pour un prédicat une colonne simplement RENDUE.
    """
    return _sql(statement).split(" where ", 1)[1].split(" returning ", 1)[0]


def _bound(statement: Any, token: str) -> Any:
    """Rendre la valeur derrière un `%(nom)s` compilé."""
    match = re.fullmatch(r"%\((\w+)\)s", token.strip())
    assert match is not None, token
    return _params(statement)[match.group(1)]


def _stale_row(
    *,
    project_key: str = "auto-discord",
    days: float = 24.1,
    outcome: str = "abandoned",
    observed_hours_ago: float | None = None,
) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "project_key": project_key,
        "client_key": "codex-factory-28aeb338",
        "last_heartbeat_at": NOW - timedelta(days=days),
        "last_observed_at": (
            None if observed_hours_ago is None else NOW - timedelta(hours=observed_hours_ago)
        ),
        "outcome": outcome,
    }


def _router(rows: list[dict[str, Any]]):
    def route(statement: Any):
        return _result(rows=rows)

    return route


async def _sweep(rows: list[dict[str, Any]], **kwargs: Any):
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    _, statements, _, factory = _make_session(_router(rows))
    result = await PgBrainSessionRepo(factory).sweep_open_sessions(now=NOW, **kwargs)
    return result, statements


@pytest.mark.asyncio
async def test_dry_run_selects_and_never_updates() -> None:
    result, statements = await _sweep([_stale_row()], dry_run=True)

    assert [candidate.project_key for candidate in result.candidates] == ["auto-discord"]
    assert result.dry_run is True
    assert result.abandoned_count == 0
    assert result.closed_inactive_count == 0
    assert not [stmt for stmt in statements if _is_update(stmt, "brain_sessions")]
    assert len(statements) == 1
    assert _sql(statements[0]).startswith("select")


@pytest.mark.asyncio
async def test_wet_run_updates_in_a_single_statement() -> None:
    result, statements = await _sweep([_stale_row()], dry_run=False)

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
async def test_the_inactivity_rule_still_emits_one_single_statement() -> None:
    """La garde « un seul statement » doit survivre à la règle neuve.

    Le test voisin ne la prouve QUE drapeau fermé. Sans celui-ci, une seconde
    passe ajoutée pour la règle des 4 h rouvrirait la fenêtre
    `SELECT`-puis-`UPDATE` sans qu'une seule suite ne rougisse.
    """
    _, statements = await _sweep(
        [_stale_row(outcome="closed_inactive", observed_hours_ago=5)],
        dry_run=False,
        close_inactive_after=FOUR_HOURS,
    )

    assert len(statements) == 1


@pytest.mark.asyncio
async def test_cutoff_is_now_minus_threshold_and_strict() -> None:
    from brain_v42.models.brain_session import AUTO_STALE_AFTER

    result, statements = await _sweep([], dry_run=True)

    assert AUTO_STALE_AFTER == timedelta(days=7)
    assert result.cutoff == NOW - timedelta(days=7)
    sql = _sql(statements[0])
    assert "status =" in sql
    assert "last_heartbeat_at <" in sql
    assert "last_heartbeat_at <=" not in sql


@pytest.mark.asyncio
async def test_default_reason_is_the_auto_constant() -> None:
    from brain_v42.models.brain_session import AUTO_STALE_ABANDONMENT_REASON

    _, statements = await _sweep([_stale_row()], dry_run=False)

    assert AUTO_STALE_ABANDONMENT_REASON == "auto_stale_7d"
    assert "auto_stale_7d" in _params(statements[0]).values()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "   "])
async def test_blank_reason_is_refused(bad: str) -> None:
    from brain_v42.models.brain_session import BrainSessionInputError

    with pytest.raises(BrainSessionInputError):
        await _sweep([], reason=bad, dry_run=False)


@pytest.mark.asyncio
async def test_non_positive_threshold_is_refused() -> None:
    from brain_v42.models.brain_session import BrainSessionInputError

    with pytest.raises(BrainSessionInputError):
        await _sweep([], older_than=timedelta(0), dry_run=False)


@pytest.mark.asyncio
async def test_non_positive_inactivity_threshold_is_refused() -> None:
    """Symétrie du voisin : un seuil nul fermerait TOUTES les traçantes.

    `None` ferme la règle ; `timedelta(0)` la rendrait universelle. Les deux
    valeurs se ressemblent à la lecture et n'ont rien en commun à l'exécution.
    """
    from brain_v42.models.brain_session import BrainSessionInputError

    with pytest.raises(BrainSessionInputError):
        await _sweep([], close_inactive_after=timedelta(0), dry_run=False)


class TestTheRuleIsDeliveredClosed:
    """Drapeau fermé ⇒ le balayage est celui d'AVANT la 046, au caractère près."""

    @pytest.mark.asyncio
    async def test_a_closed_rule_leaves_the_predicate_untouched(self) -> None:
        result, statements = await _sweep([], dry_run=False)
        sql = _sql(statements[0])

        assert result.inactive_cutoff is None
        assert "nature" not in sql
        assert "last_observed_at" not in _where(statements[0])
        assert "case" not in sql

    @pytest.mark.asyncio
    async def test_an_armed_rule_adds_the_predicate_and_the_case(self) -> None:
        """TÉMOIN NÉGATIF du test ci-dessus, sans lequel il passerait sur du code mort."""
        result, statements = await _sweep([], dry_run=False, close_inactive_after=FOUR_HOURS)
        sql = _sql(statements[0])

        assert result.inactive_cutoff == NOW - FOUR_HOURS
        assert "nature" in sql
        assert "last_observed_at" in sql
        assert "case" in sql


class TestTheFourHourRuleScope:
    @pytest.mark.asyncio
    async def test_only_agent_tracers_are_eligible(self) -> None:
        """Une session `operator` n'est JAMAIS fermée par inactivité (§0bis.3).

        C'est la garantie principale, et elle ne repose pas sur le seuil : elle
        repose sur la nature. Le prédicat doit donc la NOMMER.
        """
        _, statements = await _sweep([], dry_run=False, close_inactive_after=FOUR_HOURS)

        assert "agent" in _params(statements[0]).values()
        assert "operator" not in _params(statements[0]).values()

    @pytest.mark.asyncio
    async def test_a_never_observed_session_is_out_of_reach(self) -> None:
        """S3, tranché : `last_observed_at IS NULL` n'est JAMAIS pris par les 4 h.

        `NULL` veut dire « jamais observée », pas « observée il y a longtemps ».
        Le prédicat `IS NOT NULL` est explicite plutôt que laissé à la sémantique
        de la comparaison SQL : l'intention doit se lire, et un jour où quelqu'un
        remplacerait `<` par `IS NOT DISTINCT FROM` ou passerait par `COALESCE`,
        c'est cette ligne qui rougirait.
        """
        _, statements = await _sweep([], dry_run=False, close_inactive_after=FOUR_HOURS)
        where = _sql(statements[0]).split(" where ")[1]

        assert "last_observed_at is not null" in where

    @pytest.mark.asyncio
    async def test_the_inactivity_cutoff_is_strict_and_derived_from_the_argument(self) -> None:
        result, _ = await _sweep([], dry_run=True, close_inactive_after=timedelta(hours=9))

        assert result.inactive_cutoff == NOW - timedelta(hours=9)


class TestPrecedence:
    """7 j PRIME sur 4 h : une traçante inactive depuis 8 jours matche les DEUX."""

    @pytest.mark.asyncio
    async def test_the_case_tests_presence_before_observation(self) -> None:
        """La préséance vit dans du SQL EXÉCUTÉ, pas dans un commentaire.

        Le `CASE` doit tester `last_heartbeat_at` en PREMIER. Inverser les deux
        branches ferait partir une traçante de huit jours en `closed_inactive`
        muet, là où elle doit partir en `abandoned` avec sa raison.
        """
        _, statements = await _sweep([], dry_run=False, close_inactive_after=FOUR_HOURS)
        branch = _sql(statements[0]).split("set status=case when ", 1)[1].split(" end,", 1)[0]
        condition, outcomes = branch.split(" then ", 1)
        then_branch, else_branch = outcomes.split(" else ", 1)

        assert "last_heartbeat_at" in condition, "la PRÉSENCE doit être testée en premier"
        assert "last_observed_at" not in condition
        assert _bound(statements[0], then_branch) == "abandoned"
        assert _bound(statements[0], else_branch) == "closed_inactive"

    @pytest.mark.asyncio
    async def test_a_session_matching_both_rules_is_abandoned_not_closed(self) -> None:
        """Le fait, pas la forme : l'issue persistée d'une double-matche.

        La ligne rendue est celle que PostgreSQL a écrite (RETURNING lit la ligne
        NEUVE), donc ce test lit l'issue réelle et pas un recalcul Python.
        """
        both = _stale_row(days=8, outcome="abandoned", observed_hours_ago=8 * 24)
        result, _ = await _sweep([both], dry_run=False, close_inactive_after=FOUR_HOURS)

        assert result.abandoned_count == 1
        assert result.closed_inactive_count == 0

    @pytest.mark.asyncio
    async def test_the_abandonment_reason_is_null_on_the_inactive_branch(self) -> None:
        """`closed_inactive` INTERDIT `abandonment_reason` — CHECK de la 046.

        Ce n'est pas de la symétrie : sans le `CASE`, la ligne serait refusée par
        la base, et toute la nuit tomberait sur une contrainte.
        """
        _, statements = await _sweep([], dry_run=False, close_inactive_after=FOUR_HOURS)
        assignment = _sql(statements[0]).split(" set ", 1)[1].split(" where ", 1)[0]
        reason_case = assignment.split("abandonment_reason=", 1)[1]

        assert reason_case.startswith("case")
        assert reason_case.split(" end", 1)[0].endswith("else null")


class TestTheTwoCountersNeverMerge:
    @pytest.mark.asyncio
    async def test_each_outcome_lands_in_its_own_counter(self) -> None:
        """`abandoned_count` était unique ; il ne doit pas absorber le second.

        Les additionner effacerait la seule distinction que la 046 a coûté une
        migration à créer — un ledger conservé contre un ledger vidé.
        """
        rows = [
            _stale_row(project_key="a", days=9, outcome="abandoned"),
            _stale_row(project_key="b", days=0.1, outcome="closed_inactive", observed_hours_ago=5),
            _stale_row(project_key="c", days=0.2, outcome="closed_inactive", observed_hours_ago=6),
        ]
        result, _ = await _sweep(rows, dry_run=False, close_inactive_after=FOUR_HOURS)

        assert result.abandoned_count == 1
        assert result.closed_inactive_count == 2
        assert len(result.candidates) == 3

    @pytest.mark.asyncio
    async def test_dry_leaves_both_counters_at_zero(self) -> None:
        """Un journal ne doit jamais lire « 2 fermées » là où rien n'a été écrit."""
        rows = [
            _stale_row(project_key="a", days=9, outcome="abandoned"),
            _stale_row(project_key="b", days=0.1, outcome="closed_inactive", observed_hours_ago=5),
        ]
        result, _ = await _sweep(rows, dry_run=True, close_inactive_after=FOUR_HOURS)

        assert result.abandoned_count == 0
        assert result.closed_inactive_count == 0
        assert len(result.candidates) == 2


@pytest.mark.asyncio
async def test_an_outcome_the_sweep_cannot_produce_is_refused() -> None:
    """Le rail Pydantic doit refuser ce que le balayage ne peut pas écrire.

    `ended` demande un rituel et `open` n'est pas terminal : les voir sortir
    d'ici signalerait un `CASE` cassé, et un rapport qui les afficherait
    tranquillement serait pire que l'erreur.
    """
    from brain_v42.models.brain_session import BrainSessionSweepCandidate

    with pytest.raises(ValueError, match="not an outcome the sweep can produce"):
        BrainSessionSweepCandidate(**_stale_row(outcome="ended"))
