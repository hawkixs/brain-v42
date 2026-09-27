"""Compile-only smoke test for the nightly selection SQL (ADR 27 lot C, T1.3).

No PostgreSQL is needed to compile a statement to dialect-specific text. This
does NOT prove the SQL is semantically correct against a real server -- only
`tests/integration/db/test_claim_nightly_selection.py` does that, and it
SKIPPED in the container that wrote this (no reachable database). This test
exists so a bare `pytest tests/unit` still catches an outright syntax
regression (a typo, a broken join) even where no database is configured.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from brain_v42.repositories.pg_claim_nightly import _eligible_query, _ranked_query


def _compiled(statement: sa.Select) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_ranked_query_compiles_with_lateral_joins_and_a_window_function() -> None:
    sql = _compiled(_ranked_query(datetime.now(UTC), 200))

    assert "LEFT OUTER JOIN LATERAL" in sql
    assert "row_number() OVER (PARTITION BY eligible.project_key" in sql
    assert "ORDER BY ranked.rank, ranked.age_key, ranked.seq" in sql
    assert " LIMIT " in sql


def test_eligible_query_excludes_retired_claims_and_covers_the_three_predicates() -> None:
    sql = _compiled(_eligible_query(datetime.now(UTC)))

    assert "knowledge_claims.retired_at IS NULL" in sql
    assert "conclusive.seq IS NULL" in sql
    assert "interval '1 second'" in sql
    assert "latest.verdict = %(verdict_2)s AND latest.seq > conclusive.seq" in sql


def test_eligible_query_joins_are_correlated_to_the_claims_table() -> None:
    """A LATERAL subquery that fails to correlate would compile without WHERE claim_id = c.id."""
    sql = _compiled(_eligible_query(datetime.now(UTC)))

    assert sql.count("knowledge_claim_verdicts.claim_id = knowledge_claims.id") == 2
