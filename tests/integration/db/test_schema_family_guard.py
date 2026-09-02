"""Every DDL family the guard claims to watch is proven watched, by mutation.

Ticket `3a7da99d` says it in its own title: the residue guard watches a PROXY,
the alembic revision, where the object is the SCHEMA. `schema_fingerprint`'s
first guard closed one family -- trigger STATE -- and that is a case, not the
class. A test doing `CREATE INDEX`, `ALTER TABLE ... ADD COLUMN` or a direct
`GRANT` fell in the same blind spot the ticket described, and nothing said so.

This module is the mutation proof. For each family it plants ONE real mutation in
a disposable database and requires the guard to NAME it -- family and object, not
"something diverged". A guard that reports a count is a guard nobody can act on.

Measured 2026-09-03 before any of this existed: `brain_test` and a database built
by `alembic upgrade head` agree on all nine families, exactly -- 34 tables, 522
columns, 128 constraints, 134 indexes, 58 triggers, 140 functions, 10 views, 10
sequences, 318 grants, zero divergence. That is what licenses a guard with no
allowlist at all: there is nothing legitimate to carve out.

The reference and the mutable database are both disposable and both built by the
chain, so this module proves the DETECTOR, never the health of `brain_test`.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

from tests.integration.disposable_db import (
    asyncpg_dsn,
    create_database,
    drop_database,
    fresh_head_database,
    run_sql,
)
from tests.integration.schema_fingerprint import (
    SCHEMA_FAMILIES,
    describe_family_divergence,
    probe_schema_families,
)

pytestmark = pytest.mark.integration


def _admin_url_or_skip() -> str:
    url = os.environ.get("BRAIN_V42_TEST_DB_URL")
    if not url:
        pytest.skip("BRAIN_V42_TEST_DB_URL is not set")
    return url


@pytest.fixture(scope="module")
def reference_url() -> Iterator[str]:
    """What the alembic chain produces -- derived, never pinned."""
    with fresh_head_database(_admin_url_or_skip(), prefix="brain_famref") as url:
        yield url


@pytest.fixture(scope="module")
def mutable_url() -> Iterator[str]:
    """A second chain-built database, the one this module vandalises."""
    with fresh_head_database(_admin_url_or_skip(), prefix="brain_fammut") as url:
        yield url


#: family -> (statement that plants the mutation, statement that undoes it,
#: the object name the refusal must contain).
MUTATIONS: dict[str, tuple[str, str, str]] = {
    "tables": (
        "CREATE TABLE public.zz_planted_table (id integer)",
        "DROP TABLE public.zz_planted_table",
        "zz_planted_table",
    ),
    "columns": (
        "ALTER TABLE public.learnings ADD COLUMN zz_planted_column text",
        "ALTER TABLE public.learnings DROP COLUMN zz_planted_column",
        "learnings.zz_planted_column",
    ),
    "constraints": (
        "ALTER TABLE public.learnings ADD CONSTRAINT zz_planted_constraint CHECK (true)",
        "ALTER TABLE public.learnings DROP CONSTRAINT zz_planted_constraint",
        "learnings.zz_planted_constraint",
    ),
    "indexes": (
        "CREATE INDEX zz_planted_index ON public.learnings (id)",
        "DROP INDEX public.zz_planted_index",
        "learnings.zz_planted_index",
    ),
    "triggers": (
        # The SHAPE of the second non-alembic mutation this ticket's census found:
        # a trigger switched off directly. The digest carries `tgenabled`, so the
        # trigger is still present and the guard still refuses.
        "ALTER TABLE public.project_focus_history "
        "DISABLE TRIGGER project_focus_history_append_only_trigger",
        "ALTER TABLE public.project_focus_history "
        "ENABLE TRIGGER project_focus_history_append_only_trigger",
        "project_focus_history.project_focus_history_append_only_trigger",
    ),
    "functions": (
        "CREATE FUNCTION public.zz_planted_function() RETURNS integer "
        "LANGUAGE sql AS $$ SELECT 1 $$",
        "DROP FUNCTION public.zz_planted_function()",
        "zz_planted_function",
    ),
    "views": (
        "CREATE VIEW public.zz_planted_view AS SELECT 1 AS one",
        "DROP VIEW public.zz_planted_view",
        "zz_planted_view",
    ),
    "sequences": (
        "CREATE SEQUENCE public.zz_planted_sequence",
        "DROP SEQUENCE public.zz_planted_sequence",
        "zz_planted_sequence",
    ),
    "grants": (
        "GRANT SELECT ON public.learnings TO codex_ro",
        "REVOKE SELECT ON public.learnings FROM codex_ro",
        "learnings",
    ),
}


def test_every_declared_family_has_a_mutation() -> None:
    """No family may claim coverage without a planted mutation proving it.

    This is the guard on the guard: adding a family to `SCHEMA_FAMILIES` without
    a witness here would advertise coverage nobody measured.
    """
    assert set(MUTATIONS) == set(SCHEMA_FAMILIES), (
        f"families without a mutation witness: {sorted(set(SCHEMA_FAMILIES) - set(MUTATIONS))}; "
        f"witnesses for no family: {sorted(set(MUTATIONS) - set(SCHEMA_FAMILIES))}"
    )


def test_two_chain_built_databases_agree_on_every_family(
    reference_url: str, mutable_url: str
) -> None:
    """The negative witness: with nothing planted, the guard says NOTHING.

    Without it, a guard that always refuses would pass every test below and be
    worse than useless.
    """
    reference = probe_schema_families(reference_url)
    observed = probe_schema_families(mutable_url)

    assert describe_family_divergence(reference, observed) is None


@pytest.mark.parametrize("family", sorted(MUTATIONS))
def test_the_guard_names_the_object_of_each_family(
    reference_url: str, mutable_url: str, family: str
) -> None:
    plant, undo, expected_object = MUTATIONS[family]
    reference = probe_schema_families(reference_url)

    run_sql(asyncpg_dsn(mutable_url), [plant])
    try:
        message = describe_family_divergence(reference, probe_schema_families(mutable_url))
    finally:
        run_sql(asyncpg_dsn(mutable_url), [undo])

    assert message is not None, f"{family}: the planted mutation went unseen"
    assert family in message, f"{family}: the refusal does not name the family:\n{message}"
    assert expected_object in message, (
        f"{family}: the refusal does not name the object {expected_object!r}:\n{message}"
    )


def test_the_guard_is_clean_again_once_the_mutation_is_undone(
    reference_url: str, mutable_url: str
) -> None:
    """A guard that latches would turn one planted mutation into a dead suite."""
    reference = probe_schema_families(reference_url)
    assert describe_family_divergence(reference, probe_schema_families(mutable_url)) is None


def test_an_empty_probe_is_an_error_and_never_a_verdict(reference_url: str) -> None:
    """The 2026-08-22 near-miss, generalised: a broken instrument must not confirm.

    A fingerprint query that errors returns nothing on both sides and compares
    equal -- the answer one hopes for. Emptiness is therefore an ERROR here, not
    an outcome.
    """
    from tests.integration.schema_fingerprint import MalformedSchemaProbe

    reference = probe_schema_families(reference_url)
    with pytest.raises(MalformedSchemaProbe):
        describe_family_divergence(reference, {family: {} for family in SCHEMA_FAMILIES})
    with pytest.raises(MalformedSchemaProbe):
        describe_family_divergence(reference, {})


def test_a_probe_against_a_virgin_database_is_an_error(reference_url: str) -> None:
    """A database with no schema at all must not read as "nothing diverged"."""
    from tests.integration.schema_fingerprint import MalformedSchemaProbe

    admin_url = _admin_url_or_skip()
    database = f"brain_famvirgin_{uuid.uuid4().hex[:10]}"
    create_database(admin_url, database)
    try:
        virgin = admin_url.rpartition("/")[0] + "/" + database
        reference = probe_schema_families(reference_url)
        with pytest.raises(MalformedSchemaProbe):
            describe_family_divergence(reference, probe_schema_families(virgin))
    finally:
        drop_database(admin_url, database)
