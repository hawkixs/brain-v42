#!/usr/bin/env python3
"""Refuse a coverage figure measured on a subset of the code under test.

`tests/conftest.py::require_test_db_url()` SKIPS every database-backed test when
`BRAIN_V42_TEST_DB_URL` is absent. That is a fair guard — it exists so a local
`pytest tests/unit` does not pollute production — but it has a blind spot in CI:
**a test that skips does not turn a job red**.

The `test-coverage` job ran with no Postgres service, without that variable and
with no schema applied. Its database-backed tests therefore skipped in silence,
and the published percentage described a subset of the code actually tested with
nothing saying so to whoever read it. Measured 2026-08-22: **60 tests skipped**.

This guard is ticket `f779092b`'s real deliverable, not the corrected
percentage: repairing the number alone would let it drift again at the next
recipe gap between the two jobs.

**It compares no count against an engraved number**, and that is deliberate. The
workflow comment announced "51 tests", the ticket "55", the measurement finds
"60" — three numbers, three dates, three scheduled lies. A threshold would be the
fourth. We demand only that NO test skipped FOR THIS CAUSE, which stays true
whatever the number of database-backed tests turns out to be
tomorrow.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

#: The skip reason we refuse. It is recognised by the VARIABLE NAME that
#: `require_test_db_url()` quotes in its message: that is the only stable string
#: between the guard and this check. A skip for another reason — GPU service
#: absent, platform — stays legitimate and is not counted here.
_DB_SKIP_MARKER = "BRAIN_V42_TEST_DB_URL"

_MAX_NAMED = 10


def db_skipped_tests(report: Path) -> list[str]:
    """Return the tests the JUnit report says skipped for want of a database."""
    root = ET.parse(report).getroot()
    return [
        f"{case.get('classname')}::{case.get('name')}"
        for case in root.iter("testcase")
        for skipped in case.findall("skipped")
        if _DB_SKIP_MARKER in (skipped.get("message") or "")
    ]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <junit-xml>", file=sys.stderr)
        return 2
    report = Path(argv[1])
    if not report.is_file():
        # Fail-closed: an absent report means we do NOT KNOW whether the tests
        # ran. Passing here would make this check hollow in exactly the case
        # where pytest collapsed before writing its report.
        print(
            f"missing JUnit report {report} — cannot prove the DB-backed tests ran", file=sys.stderr
        )
        return 1

    skipped = db_skipped_tests(report)
    if not skipped:
        print("No DB-backed test was skipped: coverage covers the same world as test-unit.")
        return 0

    print(
        f"{len(skipped)} DB-backed test(s) were SKIPPED, so this coverage describes "
        "a subset of the tested code. Give this job the test-unit recipe: the "
        "postgres service, BRAIN_V42_TEST_DB_URL, and `alembic upgrade head`.",
        file=sys.stderr,
    )
    for name in skipped[:_MAX_NAMED]:
        print(f"  - {name}", file=sys.stderr)
    if len(skipped) > _MAX_NAMED:
        # No silent truncation: a list cut without its remainder reads as
        # "there were only ten".
        print(f"  ... and {len(skipped) - _MAX_NAMED} more", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
