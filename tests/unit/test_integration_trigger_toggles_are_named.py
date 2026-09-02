"""No integration test may toggle triggers by wildcard.

Found by the census ticket `3a7da99d` asked for, on 2026-09-03:
`tests/integration/db/test_focus_history_writers.py` used
`ALTER TABLE project_focus_history DISABLE TRIGGER USER` and re-enabled with
`ENABLE TRIGGER USER`.

Those two are not inverses. `DISABLE ... USER` switches off whatever is on;
`ENABLE ... USER` switches on EVERYTHING — including a trigger a migration
deliberately ships disabled. That shape exists in this very repository: 050
creates `project_contexts_focus_history_required` disabled on purpose, and arming
it is a dated operator gesture. A wildcard re-enable on that table would perform
the cutover, silently, from a test fixture.

On `project_focus_history` the wildcard covered exactly one trigger, so the defect
was LATENT, not live. This test removes the latency rather than trusting the table
never to gain a second trigger.

The schema family guard would catch the residue after the fact — it compares
trigger state against the chain and names the object. This catches the SOURCE
before it can leave one, which is cheaper and does not need a database.
"""

from __future__ import annotations

import re
from pathlib import Path

INTEGRATION = Path(__file__).parents[1] / "integration"

#: `USER` and `ALL` are the two wildcards `ALTER TABLE ... {ENABLE,DISABLE}
#: TRIGGER` accepts. Anything else is a trigger name, which is what this asks for.
_WILDCARD_TRIGGER_TOGGLE = re.compile(
    r"(?:ENABLE|DISABLE)\s+(?:REPLICA\s+|ALWAYS\s+)?TRIGGER\s+(USER|ALL)\b",
    re.IGNORECASE,
)


def test_no_integration_test_toggles_triggers_by_wildcard() -> None:
    offenders: list[str] = []
    for path in sorted(INTEGRATION.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            # A comment cannot execute DDL, and naming the forbidden shape in
            # prose is how the next reader learns why it is forbidden. Scanning
            # it would make the explanation impossible to write.
            match = _WILDCARD_TRIGGER_TOGGLE.search(line.partition("#")[0])
            if match:
                relative = path.relative_to(INTEGRATION.parents[1])
                offenders.append(f"{relative}:{number} — {match.group(0)}")

    assert not offenders, (
        "these integration tests toggle triggers by wildcard:\n"
        + "\n".join(f"    {offender}" for offender in offenders)
        + "\n\nName the trigger. `ENABLE TRIGGER USER` switches on every user trigger on the "
        "table,\nincluding one a migration ships disabled on purpose — 050 does exactly that "
        "with\n`project_contexts_focus_history_required`, and arming it is an operator gesture, "
        "not\na fixture's business."
    )


def test_the_pattern_still_recognises_the_shape_it_was_written_for() -> None:
    """A guard whose regex silently stopped matching is worse than none."""
    for statement in (
        "ALTER TABLE project_focus_history DISABLE TRIGGER USER",
        "ALTER TABLE t ENABLE TRIGGER ALL",
        "alter table t enable replica trigger user",
    ):
        assert _WILDCARD_TRIGGER_TOGGLE.search(statement), statement

    for named in (
        "ALTER TABLE project_focus_history DISABLE TRIGGER project_focus_history_append_only",
        "ALTER TABLE t ENABLE TRIGGER user_activity_trigger",
    ):
        assert not _WILDCARD_TRIGGER_TOGGLE.search(named), named
