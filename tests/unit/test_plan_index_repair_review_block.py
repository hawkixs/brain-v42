"""Bumping the repair's head pin without reviewing the migration must fail HERE.

Ticket `6cc34303`. `plan_index_repair_store` carries two things that must move
together: `_REQUIRED_ALEMBIC_HEAD`, which refuses to run the repair against a
schema it was not reviewed against, and the comment block above it that records,
revision by revision, WHAT that revision changes on the tables the repair touches
and why the repair stays safe.

Only the first was guarded. `test_plan_index_repair_head_pin.py` compares the
constant to the repository head — a pure number check — so three separate commits
bumped it to 047, then 048, then 049 while the review block stopped at 046. The
block's own closing line names the failure it then suffered: *a missing review
reads exactly like a review that was done*.

THE REVIEWED SET IS DERIVED FROM THE BLOCK, and the chain from Alembic. Nothing
is retyped: a list of revisions written here would be a second thing to bump, and
the whole defect is a thing nobody remembered to bump.

The check is CONTIGUITY, not membership. Requiring only that the head appears
would let 049→051 through with a 051 entry alone, leaving 050 unreviewed and
invisible — the same defect one revision along.
"""

from __future__ import annotations

import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from brain_v42.maintenance.plan_index_repair_store import _REQUIRED_ALEMBIC_HEAD

_REPO_ROOT = Path(__file__).resolve().parents[2]
_STORE = _REPO_ROOT / "src" / "brain_v42" / "maintenance" / "plan_index_repair_store.py"

#: Every entry of the block opens the same way — "Bumped to NNN after ...". The
#: phrasing IS the record: an entry that does not announce its revision is not an
#: entry, it is prose next to the constant.
_REVIEW_ENTRY = re.compile(r"^#\s*Bumped to (\d{3})\b", re.MULTILINE)


def _reviewed_revisions() -> set[str]:
    """The revisions the block claims to have reviewed, read off the block."""
    head_line = _STORE.read_text(encoding="utf-8").split('_REQUIRED_ALEMBIC_HEAD = "')[0]
    return set(_REVIEW_ENTRY.findall(head_line))


def _chain_up_to_head() -> list[str]:
    """The linear revision chain from Alembic, oldest first."""
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    return [revision.revision for revision in reversed(list(script.walk_revisions()))]


def test_the_block_records_at_least_one_review() -> None:
    """Non-vacuity: a reworded block must redden here, not empty the guard.

    Without this, changing "Bumped to 049" into any other phrasing would leave the
    reviewed set empty and every assertion below true on nothing.
    """
    assert _reviewed_revisions(), (
        "no review entry found in plan_index_repair_store.py — the block above "
        "_REQUIRED_ALEMBIC_HEAD must record each reviewed revision as "
        "'# Bumped to NNN after ...', which is what this guard reads"
    )


def test_the_pinned_head_has_been_reviewed() -> None:
    """THE guard that was missing: the constant cannot outrun its own review."""
    reviewed = _reviewed_revisions()

    assert _REQUIRED_ALEMBIC_HEAD in reviewed, (
        f"_REQUIRED_ALEMBIC_HEAD is {_REQUIRED_ALEMBIC_HEAD!r} but the review block "
        f"stops at {max(reviewed) if reviewed else 'nothing'}. Review what "
        f"{_REQUIRED_ALEMBIC_HEAD} changes on the tables the repair writes "
        f"(project_contexts UPDATE, feature_artifacts DELETE, indexed_plans DELETE) "
        f"and write the entry — do not bump the constant alone."
    )


def test_every_revision_since_the_first_review_has_an_entry() -> None:
    """Contiguity: no revision may be skipped between the first review and the head.

    Membership alone would accept 049 -> 051 with a 051 entry, leaving 050
    unreviewed. The chain comes from Alembic, so a merge revision or a renamed
    file cannot fool this into checking a shorter interval than the real one.
    """
    reviewed = _reviewed_revisions()
    chain = _chain_up_to_head()
    first = min(reviewed, key=chain.index)
    expected = chain[chain.index(first) : chain.index(_REQUIRED_ALEMBIC_HEAD) + 1]

    missing = [revision for revision in expected if revision not in reviewed]

    assert not missing, (
        f"revisions {missing} landed between the first reviewed one ({first}) and the "
        f"pinned head ({_REQUIRED_ALEMBIC_HEAD}) with no entry in the review block. "
        f"A missing review reads exactly like a review that was done."
    )


#: The three mutating calls this file is allowed to make, measured from its AST on
#: 2026-09-02. Derived rather than trusted: the 049 entry above rests on the claim
#: that the repair never INSERTS into `indexed_plans`, and a claim load-bearing
#: enough to settle a provenance question must be self-checking.
_ALLOWED_MUTATIONS = {
    ("update", "project_contexts"),
    ("delete", "feature_artifacts"),
    ("delete", "indexed_plans"),
}


def _mutating_calls() -> set[tuple[str, str]]:
    """Every ``sa.insert/update/delete(<table>)`` the store makes, read from the AST."""
    import ast

    tree = ast.parse(_STORE.read_text(encoding="utf-8"))
    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"insert", "update", "delete"}:
            continue
        if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "sa"):
            continue
        if node.args and isinstance(node.args[0], ast.Name):
            found.add((node.func.attr, node.args[0].id))
    return found


def test_the_repair_never_writes_an_indexed_plan_row() -> None:
    """The premise of the 049 entry, made self-checking.

    The repair DELETES stale index rows and verifies through ``ReindexEvidence``
    that a reindex performed elsewhere covered the same snapshot; the re-insertion
    goes through ``pg_indexed_plan_repo``, whose upsert already declares
    ``freshness_source = 'plan_reindex'``. So this file has no provenance to set.

    The day it gains an INSERT — or an UPDATE — on ``indexed_plans``, that stops
    being true and 049's vocabulary becomes its problem. This test is what forces
    the question to be asked then, instead of a row landing with a NULL provenance
    that the 043 trigger will not fill in.
    """
    mutations = _mutating_calls()

    assert mutations == _ALLOWED_MUTATIONS, (
        f"the repair's write surface moved: {sorted(mutations)}. If it now writes "
        f"indexed_plans rows, decide the freshness_source it must declare (049 admits "
        f"'plan_reindex') and update the review block — a row inserted without one "
        f"stays NULL, and the 043 trigger only fires on a status CHANGE."
    )


def test_the_provenance_lives_where_the_row_is_created() -> None:
    """Negative witness: the word must exist somewhere, or the entry is fiction.

    Without it, the 049 entry could point at a writer that never declared
    anything, and read as reassuring on nothing at all.
    """
    upsert = (
        _REPO_ROOT / "src" / "brain_v42" / "repositories" / "pg_indexed_plan_repo.py"
    ).read_text(encoding="utf-8")

    assert upsert.count("plan_reindex") >= 2, (
        "pg_indexed_plan_repo no longer declares 'plan_reindex' on both the INSERT "
        "and the ON CONFLICT branches — the 049 entry in plan_index_repair_store "
        "points at it as the writer that carries the provenance"
    )
