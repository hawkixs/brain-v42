"""Unit contract of the server sweep — TWO rules, ONE statement, one precedence.

The harness compiles the SQLAlchemy statements without PostgreSQL: it proves the
predicate's SHAPE and the fact that DRY emits no UPDATE. The predicate's real
boundary (day N-1 / N+1) can only be proven against a real database: it lives in
tests/integration/db/test_brain_sessions_sweep.py.

**What M-G adds, and what this file must therefore guard:**

- the 4 h rule takes ONLY `nature = 'agent'` tracers;
- it NEVER takes a session whose `last_observed_at` is NULL (S3, settled) —
  `NULL` means "never observed", not "observed a long time ago";
- the 7 d rule BEATS the 4 h rule, because a tracer inactive for more than seven
  days matches BOTH;
- flag closed ⇒ the predicate is the pre-046 one, character for character;
- and all of it stays ONE statement: the `SELECT`-then-`UPDATE` window the false
  corpse of 2026-08-06 cost must not reopen through M-G's door.
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
    """The predicate ALONE, without the RETURNING.

    `last_observed_at` also appears in the projection: reading "everything after
    WHERE" would pass off a merely RETURNED column as a predicate.
    """
    return _sql(statement).split(" where ", 1)[1].split(" returning ", 1)[0]


def _bound(statement: Any, token: str) -> Any:
    """Return the value behind a compiled `%(name)s`."""
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
    """The "one statement" guard must survive the new rule.

    Its neighbour proves it ONLY with the flag closed. Without this one, a second
    pass added for the 4 h rule would reopen the `SELECT`-then-`UPDATE` window
    without a single suite turning red.
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
    """Symmetric with its neighbour: a zero threshold would close EVERY tracer.

    `None` closes the rule; `timedelta(0)` would make it universal. The two
    values look alike when read and have nothing in common when executed.
    """
    from brain_v42.models.brain_session import BrainSessionInputError

    with pytest.raises(BrainSessionInputError):
        await _sweep([], close_inactive_after=timedelta(0), dry_run=False)


class TestTheRuleIsDeliveredClosed:
    """Flag closed ⇒ the sweep is the pre-046 one, character for character."""

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
        """NEGATIVE WITNESS for the test above, without which it would pass over dead code."""
        result, statements = await _sweep([], dry_run=False, close_inactive_after=FOUR_HOURS)
        sql = _sql(statements[0])

        assert result.inactive_cutoff == NOW - FOUR_HOURS
        assert "nature" in sql
        assert "last_observed_at" in sql
        assert "case" in sql


class TestTheFourHourRuleScope:
    @pytest.mark.asyncio
    async def test_only_agent_tracers_are_eligible(self) -> None:
        """An `operator` session is NEVER closed for inactivity (§0bis.3).

        That is the main guarantee, and it does not rest on the threshold: it
        rests on the nature. The predicate must therefore NAME it.
        """
        _, statements = await _sweep([], dry_run=False, close_inactive_after=FOUR_HOURS)

        assert "agent" in _params(statements[0]).values()
        assert "operator" not in _params(statements[0]).values()

    @pytest.mark.asyncio
    async def test_a_never_observed_session_is_out_of_reach(self) -> None:
        """S3, settled: `last_observed_at IS NULL` is NEVER taken by the 4 h rule.

        `NULL` means "never observed", not "observed a long time ago". The
        `IS NOT NULL` predicate is explicit rather than left to SQL comparison
        semantics: the intent must read, and the day someone replaced `<` with
        `IS NOT DISTINCT FROM` or went through `COALESCE`, this line is the one
        that would turn red.
        """
        _, statements = await _sweep([], dry_run=False, close_inactive_after=FOUR_HOURS)
        where = _sql(statements[0]).split(" where ")[1]

        assert "last_observed_at is not null" in where

    @pytest.mark.asyncio
    async def test_the_inactivity_cutoff_is_strict_and_derived_from_the_argument(self) -> None:
        result, _ = await _sweep([], dry_run=True, close_inactive_after=timedelta(hours=9))

        assert result.inactive_cutoff == NOW - timedelta(hours=9)


class TestPrecedence:
    """7 d BEATS 4 h: a tracer inactive for 8 days matches BOTH."""

    @pytest.mark.asyncio
    async def test_the_case_tests_presence_before_observation(self) -> None:
        """The precedence lives in EXECUTED SQL, not in a comment.

        The `CASE` must test `last_heartbeat_at` FIRST. Swapping the two branches
        would send an eight-day-old tracer to a silent `closed_inactive`, where it
        must go to `abandoned` with its reason.
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
        """The fact, not the shape: the persisted outcome of a double match.

        The returned row is the one PostgreSQL wrote (RETURNING reads the NEW
        row), so this test reads the real outcome and not a Python recomputation.
        """
        both = _stale_row(days=8, outcome="abandoned", observed_hours_ago=8 * 24)
        result, _ = await _sweep([both], dry_run=False, close_inactive_after=FOUR_HOURS)

        assert result.abandoned_count == 1
        assert result.closed_inactive_count == 0

    @pytest.mark.asyncio
    async def test_the_abandonment_reason_is_null_on_the_inactive_branch(self) -> None:
        """`closed_inactive` FORBIDS `abandonment_reason` — the 046 CHECK.

        This is not symmetry: without the `CASE`, the row would be refused by the
        database, and the whole night would fall over on a constraint.
        """
        _, statements = await _sweep([], dry_run=False, close_inactive_after=FOUR_HOURS)
        assignment = _sql(statements[0]).split(" set ", 1)[1].split(" where ", 1)[0]
        reason_case = assignment.split("abandonment_reason=", 1)[1]

        assert reason_case.startswith("case")
        assert reason_case.split(" end", 1)[0].endswith("else null")


class TestTheTwoCountersNeverMerge:
    @pytest.mark.asyncio
    async def test_each_outcome_lands_in_its_own_counter(self) -> None:
        """`abandoned_count` used to be alone; it must not absorb the second.

        Adding them would erase the one distinction 046 cost a migration to
        create — a ledger kept versus a ledger emptied.
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
        """A log must never read "2 closed" where nothing was written."""
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
    """The Pydantic rail must refuse what the sweep cannot write.

    `ended` requires a ritual and `open` is not terminal: seeing them come out of
    here would signal a broken `CASE`, and a report displaying them calmly would
    be worse than the error.
    """
    from brain_v42.models.brain_session import BrainSessionSweepCandidate

    with pytest.raises(ValueError, match="not an outcome the sweep can produce"):
        BrainSessionSweepCandidate(**_stale_row(outcome="ended"))
