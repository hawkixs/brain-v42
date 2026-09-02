"""Project keys for the unit tests that write to the shared database.

`tests/unit/` hits the SAME `brain_test` database as the integration suite as soon
as `BRAIN_V42_TEST_DB_URL` is set — and both CI rails set it. But the end-of-session
cleanup lives in `tests/integration/conftest.py`, only applies to its own suite, and
recognises a single prefix: `integ-`.

Each unit module therefore built its key on the spot, with its own prefix — `t8-`,
`t9-`, `rv-`, `t-adr-`, `t-run-`. None was purged. Measured on 2026-08-11: 5,674
learnings in brain_test, of which 4,241 under `t8-` and 581 under `t9-`, for 188
real rows. The symptom is INVISIBLE in CI, which recreates its database at every
pipeline; it only grows on development databases, hence differently on each machine
(ticket cb888186).

Going through this module is what attaches a unit key to the cleanup. The contract
is held by tests/unit/test_unit_project_keys_are_purged.py, which applies the REAL
purge to a key built here.

NEVER extend the prefix to a production key like `brain-v42`: the conftest's
guardrail only refuses the database NAME `brain`, so a `BRAIN_V42_TEST_DB_URL`
pointed at a restoration would erase real data. The negative probe
`test_the_purge_leaves_a_non_integration_key_alone` exists to make that temptation
fail.
"""

from __future__ import annotations

import uuid

#: The only prefix `_INTEGRATION_PROJECT_PREDICATE` recognises. The tag stays in the
#: key after it, so that an orphan row still names the test that wrote it.
UNIT_KEY_PREFIX = "integ-"


def make_unit_project_key(tag: str) -> str:
    """Return a per-test project key that the shared purge will delete.

    ``tag`` identifies the calling module (``t8``, ``rv``, …) and has no effect on
    the cleanup: it is the prefix that counts.
    """
    if not tag or not tag.strip():
        raise ValueError("tag is required — an unnamed key cannot be traced back to its test")
    return f"{UNIT_KEY_PREFIX}{tag.strip()}-{uuid.uuid4().hex[:8]}"
