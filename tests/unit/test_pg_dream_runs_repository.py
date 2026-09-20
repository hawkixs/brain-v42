"""Unit contracts for the latest Dream-night aggregate query."""

from __future__ import annotations

from sqlalchemy.dialects import postgresql

from brain_v42.repositories.pg_dream_runs import _LAST_NIGHT_SQL, read_last_night


def test_last_night_query_is_one_aggregate_statement_for_the_latest_date() -> None:
    """A per-date query could observe a different night or multiply source reads."""
    compiled = str(_LAST_NIGHT_SQL.compile(dialect=postgresql.dialect())).lower()

    assert compiled.startswith("select ")
    assert ";" not in compiled
    assert "max(dream_runs.run_date)" in compiled
    assert compiled.count("filter (where") == 10


async def test_last_night_returns_none_when_the_table_has_no_rows() -> None:
    """An empty table has no night to report, rather than a zero-valued night."""

    class EmptyResult:
        def mappings(self) -> EmptyResult:
            return self

        def one_or_none(self) -> None:
            return None

    class EmptySession:
        async def execute(self, statement: object) -> EmptyResult:
            assert statement is _LAST_NIGHT_SQL
            return EmptyResult()

    assert await read_last_night(EmptySession()) is None  # type: ignore[arg-type]
