"""The ACL contract is proven by EXECUTED mutations, not by reasoning about it.

Ticket `8ce279da`, residue of `60708007`. Two directions were already held: a
disappearing GRANT in the asset (`test_recovery_contract_v5_acl.py`, which mutates
the FILE) and a real REVOKE against the schema family fingerprint
(`test_schema_family_guard.py`). What nobody did was replay
`ops/recovery/<head>-acl.sql` itself against a database somebody had actually
damaged.

The difference matters. An asset-side mutation proves the test reads the asset; a
database-side mutation proves the CONTRACT sees the damage. Only the second one
answers "would this receipt have caught it".

Measured here on 2026-09-03, and the baseline was worth measuring on its own: the
ACL asset passes on a database the alembic chain has just built — all four
counters at zero. The yardstick never replayed it, so that had never been
established.

**What this module deliberately does NOT mutate: anything cluster-wide.**
`ALTER ROLE`, `CREATE ROLE` and role membership live in the cluster, not in a
database, so they would reach production `brain` from a disposable database.
`role_privilege_mismatches` is therefore exercised through its one
database-scoped term -- `REVOKE USAGE ON SCHEMA public` -- and its four
cluster-scoped terms stay unproven, which the report says in those words.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# Reused, not duplicated: the yardstick already replays an attestation asset
# READ-ONLY and returns its failures. Imported rather than extracted because
# another worker holds that file today; extracting it into `disposable_db.py` is
# the follow-up, and this import fails LOUDLY if the name moves.
from tests.integration.db.test_fresh_head_is_the_yardstick import _replay
from tests.integration.disposable_db import fresh_head_database

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).parents[3]
ACL_ASSET = PROJECT_ROOT / "ops" / "recovery" / "brain-v42-v8-acl.sql"

#: The term of `contract_grant_mismatches` that catches a grant that VANISHED.
#: Removing it is how this module proves the term is load-bearing rather than
#: decorative -- on a COPY, never on the asset.
MISSING_GRANT_TERM = """     SELECT count(*)
     FROM expected_contract_grants AS expected_grant
     LEFT JOIN observed_relation_privileges AS observed_grant
       ON observed_grant.object_name = expected_grant.object_name
      AND observed_grant.grantee = expected_grant.grantee
      AND observed_grant.privilege_type = expected_grant.privilege_type
     WHERE observed_grant.object_name IS NULL"""

#: (label, damage, repair, the counter that must move). Every statement is
#: DATABASE-scoped: nothing here reaches another database of the cluster.
MUTATIONS: tuple[tuple[str, str, str, str], ...] = (
    (
        "a codex grant is revoked",
        "REVOKE SELECT ON public.codex_ticket_v1 FROM codex_ro",
        "GRANT SELECT ON public.codex_ticket_v1 TO codex_ro",
        "contract_grant_mismatches",
    ),
    (
        "a relation changes owner",
        "ALTER TABLE public.learnings OWNER TO codex_ro",
        "ALTER TABLE public.learnings OWNER TO brain",
        "relation_owner_mismatches",
    ),
    (
        "a codex view is opened to PUBLIC",
        "GRANT SELECT ON public.codex_ticket_v1 TO PUBLIC",
        "REVOKE SELECT ON public.codex_ticket_v1 FROM PUBLIC",
        "unexpected_grantee_mismatches",
    ),
    (
        "codex_ro loses USAGE on the schema",
        "REVOKE USAGE ON SCHEMA public FROM codex_ro",
        "GRANT USAGE ON SCHEMA public TO codex_ro",
        "role_privilege_mismatches",
    ),
)


@pytest.fixture(scope="module")
def acl_database_url() -> Iterator[str]:
    """A database of this module's own: it gets damaged, so nothing shares it."""
    url = _admin_url_or_skip()
    with fresh_head_database(url, prefix="brain_aclmut") as disposable_url:
        yield disposable_url


def _admin_url_or_skip() -> str:
    import os

    url = os.environ.get("BRAIN_V42_TEST_DB_URL")
    if not url:
        pytest.skip("BRAIN_V42_TEST_DB_URL is not set")
    return url


async def _execute(url: str, statement: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(sa.text(statement))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_acl_contract_passes_on_a_chain_built_database(
    acl_database_url: str,
) -> None:
    """The baseline, and it had never been measured: the yardstick skips this asset.

    Without it, every refusal below would be indistinguishable from a contract
    that refuses everything.
    """
    assert await _replay(acl_database_url, ACL_ASSET) == {}


@pytest.mark.parametrize(("label", "damage", "repair", "counter"), MUTATIONS, ids=lambda v: v)
@pytest.mark.asyncio
async def test_a_real_privilege_change_is_caught_and_named(
    acl_database_url: str, label: str, damage: str, repair: str, counter: str
) -> None:
    """Damage the DATABASE, replay the shipped asset, then undo and replay again.

    The repair runs in a `finally`: a mutation left behind would make every later
    test in this module measure the previous one's damage instead of its own.
    """
    await _execute(acl_database_url, damage)
    try:
        failures = await _replay(acl_database_url, ACL_ASSET)
        assert "acl_and_ownership" in failures, f"{label}: the contract did not notice"
        observed = failures["acl_and_ownership"]["observed"]
        assert observed[counter] >= 1, (
            f"{label}: expected {counter} to move, got {json.dumps(observed)}"
        )
    finally:
        await _execute(acl_database_url, repair)

    assert await _replay(acl_database_url, ACL_ASSET) == {}, (
        f"{label}: the contract still refuses after the repair -- either the repair "
        "is incomplete or the counter latches"
    )


@pytest.mark.asyncio
async def test_every_counter_of_the_receipt_has_a_witness() -> None:
    """No counter may be advertised without a mutation that moves it.

    Three of the four are proven by execution above. `role_privilege_mismatches`
    is proven only through its schema-USAGE term; its role-shaped terms are
    cluster-wide and deliberately out of reach here.
    """
    receipt_counters = {
        "contract_grant_mismatches",
        "relation_owner_mismatches",
        "role_privilege_mismatches",
        "unexpected_grantee_mismatches",
    }
    assert {counter for *_, counter in MUTATIONS} == receipt_counters


@pytest.mark.asyncio
async def test_the_missing_grant_term_is_load_bearing(
    acl_database_url: str, tmp_path: Path
) -> None:
    """Remove the term on a COPY and the same REVOKE goes unnoticed.

    This is what makes the test above a measurement rather than a coincidence: a
    contract that passed for its own reasons would pass here too, and this fails
    when it does.
    """
    asset = ACL_ASSET.read_text(encoding="utf-8")
    assert asset.count(MISSING_GRANT_TERM) == 1, (
        "the term this witness removes is no longer in the asset -- re-read the "
        "contract before trusting this module"
    )
    crippled = tmp_path / "acl-without-the-missing-grant-term.sql"
    crippled.write_text(asset.replace(MISSING_GRANT_TERM, "     SELECT 0"), encoding="utf-8")

    await _execute(acl_database_url, "REVOKE SELECT ON public.codex_ticket_v1 FROM codex_ro")
    try:
        assert await _replay(acl_database_url, ACL_ASSET) != {}, "the shipped asset must catch it"
        assert await _replay(acl_database_url, crippled) == {}, (
            "the crippled asset caught the revoke anyway -- the term removed is not "
            "the one that does the work, and this witness proves nothing"
        )
    finally:
        await _execute(acl_database_url, "GRANT SELECT ON public.codex_ticket_v1 TO codex_ro")
