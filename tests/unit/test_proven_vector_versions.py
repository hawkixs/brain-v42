"""`RESTORE_BUILD_VECTOR_VERSIONS` names versions that were PROVED, not tolerated.

Ticket `9ec053b5`. The decision of 2026-08-23 (thread `2ed0d4e0`) made every
`ALTER EXTENSION` conditional on an empty SQL delta, and that proof was done for
`0.8.4`. The set then grew to `{0.8.4, 0.8.5}` by hand, because a restore on the
compose-pinned image installs `0.8.5` and the check went red — the list was
widened to match what was observed, which is the opposite of what it is for.

A tolerated version and a proved one are indistinguishable inside a frozenset
literal. They are distinguishable in a file that carries, per version, the date
of the proof and where to read it — so this module makes the set DERIVE from that
file. Adding a version now means writing down when it was proved and by what.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
PROVEN = ROOT / "ops" / "recovery" / "proven_vector_versions.json"
YARDSTICK = ROOT / "tests" / "integration" / "db" / "test_fresh_head_is_the_yardstick.py"


def _document() -> dict:
    return json.loads(PROVEN.read_text(encoding="utf-8"))


def test_every_listed_version_carries_the_date_and_the_reference_of_its_proof() -> None:
    """A version with no proof recorded is a tolerated version wearing a badge."""
    versions = _document()["versions"]

    assert versions, "the file lists no version — the derived set would be empty"
    for version, entry in versions.items():
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"not a version: {version!r}"
        dt.date.fromisoformat(entry["proved_on"])
        assert entry["reference"].strip(), f"{version} records no reference"
        assert entry["sql_delta"] in {"empty", "non-empty"}, (
            f"{version} does not say whether the SQL delta was empty"
        )


def test_the_yardstick_derives_its_set_instead_of_listing_it() -> None:
    """The one line the ticket asks for: no literal set in the integration guard.

    A frozenset written out by hand goes green the moment somebody widens it, and
    widening it is exactly what happened on 2026-09-02.
    """
    source = YARDSTICK.read_text(encoding="utf-8")

    assert "proven_vector_versions" in source, (
        "the integration guard no longer reads the proof file — its set is a literal again"
    )
    assert not re.search(r"RESTORE_BUILD_VECTOR_VERSIONS\s*=\s*frozenset\(\{\s*\"", source), (
        "RESTORE_BUILD_VECTOR_VERSIONS is a hand-written set again"
    )


def test_the_versions_proved_so_far_are_the_two_measured_ones() -> None:
    """A frozen witness: the file's content, not just its shape.

    Without it, emptying the file would satisfy every assertion above and silently
    empty the guard downstream.
    """
    assert set(_document()["versions"]) == {"0.8.4", "0.8.5"}
