"""What each `indexed_plans` row is, before anything is allowed to touch it.

Measured on the live corpus 2026-09-22, 208 rows: 70 canonical, 70 whose path
is wrong but whose content is on disk, 68 with nothing to point at. The ticket
described the second and third groups as one ("les lignes orphelines"); they
need opposite treatments, and telling them apart is the whole point of a
dry-run inventory.

NOTHING IS DELETED. The operator rule is that no knowledge leaves the brain:
archiving takes a row out of the default views -- `indexed_plan_search_service`
already filters `freshness_status != 'archived'` -- and leaves every byte in
place. A row whose file vanished is the only remaining copy of that plan; it is
the last row anybody should delete.

This module is the pure half: given rows and disk facts, the verdict. Reading
the database and walking the filesystem live in the CLI, so the decision stays
testable without either.
"""

from __future__ import annotations

import pytest

from brain_v42.maintenance.plan_index_inventory import (
    DiskPlanFile,
    PlanRow,
    PlanRowVerdict,
    classify,
)


def _row(
    row_id: str,
    project_key: str,
    file_path: str,
    content_hash: str = "h" * 64,
    resolved_path: str | None = None,
    indexed_at: str = "2026-01-01T00:00:00+00:00",
) -> PlanRow:
    return PlanRow(
        row_id=row_id,
        project_key=project_key,
        file_path=file_path,
        content_hash=content_hash,
        freshness_status="fresh",
        indexed_at=indexed_at,
        resolved_path=resolved_path,
    )


def _disk(path: str, project_key: str, content_hash: str = "h" * 64) -> DiskPlanFile:
    return DiskPlanFile(path=path, project_key=project_key, content_hash=content_hash)


def _only(decisions, row_id):
    return next(d for d in decisions if d.row.row_id == row_id)


def test_a_row_at_a_real_path_under_its_own_project_is_left_alone() -> None:
    rows = [_row("1", "brain-v42", "/repo/brain_v42/docs/a-design.md")]
    disk = [_disk("/repo/brain_v42/docs/a-design.md", "brain-v42")]

    decisions = classify(rows, disk)

    assert _only(decisions, "1").verdict is PlanRowVerdict.CANONICAL


def test_a_symlink_alias_is_rewritten_to_the_canonical_name() -> None:
    """The file is real; only the name the row carries is an alias of it.

    Measured: 19 such rows, 11 of them `auto-discord` and 8 `brain-v42`. The
    ticket named brain-v42's alone. Each is the ONLY row for its file, so
    archiving them would take a plan out of search for a naming detail.
    """
    rows = [
        _row(
            "1",
            "auto-discord",
            "/repo/ReD_v1/projects/auto-discord/docs/plans/a-plan.md",
            resolved_path="/repo/auto_discord/docs/plans/a-plan.md",
        )
    ]
    disk = [_disk("/repo/auto_discord/docs/plans/a-plan.md", "auto-discord")]

    decision = _only(classify(rows, disk), "1")

    assert decision.verdict is PlanRowVerdict.REWRITE
    assert decision.target_path == "/repo/auto_discord/docs/plans/a-plan.md"
    assert decision.target_project == "auto-discord"


def test_an_alias_whose_canonical_row_already_exists_is_archived() -> None:
    """Two rows, one file: the canonical one wins and the alias leaves the view."""
    rows = [
        _row("canon", "brain-v42", "/repo/brain_v42/docs/a-design.md"),
        _row(
            "alias",
            "brain-v42",
            "/repo/ReD_v1/projects/brain-v42/docs/a-design.md",
            resolved_path="/repo/brain_v42/docs/a-design.md",
        ),
    ]
    disk = [_disk("/repo/brain_v42/docs/a-design.md", "brain-v42")]

    decisions = classify(rows, disk)

    assert _only(decisions, "canon").verdict is PlanRowVerdict.CANONICAL
    assert _only(decisions, "alias").verdict is PlanRowVerdict.ARCHIVE_DUPLICATE


def test_a_relative_row_is_reattributed_to_the_project_that_owns_the_content() -> None:
    """The core mis-attribution: red-games holds red-writer's plan.

    A relative scan path resolved against the service's own working directory,
    so projects indexed files they never owned. The content is not stale, it is
    filed under the wrong key -- and the repair is a rewrite, not a delete.
    """
    rows = [_row("1", "red-games", "docs/specs/red-writer-design.md", content_hash="a" * 64)]
    disk = [_disk("/repo/red_writer/docs/specs/red-writer-design.md", "red-writer", "a" * 64)]

    decision = _only(classify(rows, disk), "1")

    assert decision.verdict is PlanRowVerdict.REWRITE
    assert decision.target_path == "/repo/red_writer/docs/specs/red-writer-design.md"
    assert decision.target_project == "red-writer"


def test_a_relative_row_whose_content_is_already_indexed_is_archived() -> None:
    rows = [
        _row("canon", "red-writer", "/repo/red_writer/docs/a-design.md", content_hash="a" * 64),
        _row("rel", "red-games", "docs/a-design.md", content_hash="a" * 64),
    ]
    disk = [_disk("/repo/red_writer/docs/a-design.md", "red-writer", "a" * 64)]

    decisions = classify(rows, disk)

    assert _only(decisions, "canon").verdict is PlanRowVerdict.CANONICAL
    assert _only(decisions, "rel").verdict is PlanRowVerdict.ARCHIVE_DUPLICATE


def test_a_row_whose_content_exists_nowhere_is_archived_never_deleted() -> None:
    """47 rows measured. No file carries this content any more, so this row IS
    the plan. It leaves the default views and stays in the table."""
    rows = [_row("1", "red-quant", "docs/plans/gone-plan.md", content_hash="z" * 64)]
    disk = [_disk("/repo/other/docs/plans/other-plan.md", "red-lab", "a" * 64)]

    decision = _only(classify(rows, disk), "1")

    assert decision.verdict is PlanRowVerdict.ARCHIVE_ORPHAN
    assert decision.target_path is None


def test_no_verdict_ever_deletes() -> None:
    """The guarantee, pinned as a guarantee and not as a habit."""
    assert {v.value for v in PlanRowVerdict} == {
        "canonical",
        "rewrite",
        "archive_duplicate",
        "archive_orphan",
    }
    assert not any("delete" in v.value for v in PlanRowVerdict)


def test_two_rows_competing_for_one_target_path_do_not_both_rewrite() -> None:
    """`file_path` is UNIQUE: a second rewrite onto the same name would abort
    the transaction. The newest row keeps the name; the other is archived."""
    rows = [
        _row(
            "old",
            "red-games",
            "docs/a-design.md",
            content_hash="a" * 64,
            indexed_at="2026-01-01T00:00:00+00:00",
        ),
        _row(
            "new",
            "red-quant",
            "specs/a-design.md",
            content_hash="a" * 64,
            indexed_at="2026-06-01T00:00:00+00:00",
        ),
    ]
    disk = [_disk("/repo/red_writer/docs/a-design.md", "red-writer", "a" * 64)]

    decisions = classify(rows, disk)

    assert _only(decisions, "new").verdict is PlanRowVerdict.REWRITE
    assert _only(decisions, "old").verdict is PlanRowVerdict.ARCHIVE_DUPLICATE


def test_the_collision_tie_break_is_deterministic() -> None:
    """Same timestamp must not make the outcome depend on row order."""
    rows = [
        _row("bbb", "red-games", "docs/a-design.md", content_hash="a" * 64),
        _row("aaa", "red-quant", "specs/a-design.md", content_hash="a" * 64),
    ]
    disk = [_disk("/repo/x/a-design.md", "red-writer", "a" * 64)]

    first = {d.row.row_id: d.verdict for d in classify(rows, disk)}
    second = {d.row.row_id: d.verdict for d in classify(list(reversed(rows)), disk)}

    assert first == second


def test_identical_content_on_several_files_picks_one_target_deterministically() -> None:
    rows = [_row("1", "red-games", "docs/a-design.md", content_hash="a" * 64)]
    disk = [
        _disk("/repo/z/a-design.md", "proj-z", "a" * 64),
        _disk("/repo/a/a-design.md", "proj-a", "a" * 64),
    ]

    forward = _only(classify(rows, disk), "1")
    backward = _only(classify(rows, list(reversed(disk))), "1")

    assert forward.target_path == backward.target_path == "/repo/a/a-design.md"
    assert forward.target_project == "proj-a"


def test_same_canonical_file_claimed_by_two_projects_is_rejected() -> None:
    """A repair must not select an owner arbitrarily from conflicting roots."""
    rows = [_row("1", "red-games", "docs/a-plan.md", content_hash="a" * 64)]
    disk = [
        _disk("/canonical/a-plan.md", "red-games", "a" * 64),
        _disk("/canonical/a-plan.md", "red-writer", "a" * 64),
    ]

    with pytest.raises(ValueError, match="cross_project_path_claim"):
        classify(rows, disk)


def test_cross_project_direct_path_with_different_content_is_refused() -> None:
    rows = [_row("1", "red-games", "/repo/a-plan.md", content_hash="a" * 64)]
    disk = [_disk("/repo/a-plan.md", "red-writer", "b" * 64)]

    with pytest.raises(ValueError, match="cross_project_content_mismatch"):
        classify(rows, disk)


def test_cross_project_alias_with_different_content_is_refused() -> None:
    rows = [
        _row(
            "1",
            "red-games",
            "/alias/a-plan.md",
            content_hash="a" * 64,
            resolved_path="/canonical/a-plan.md",
        )
    ]
    disk = [_disk("/canonical/a-plan.md", "red-writer", "b" * 64)]

    with pytest.raises(ValueError, match="cross_project_content_mismatch"):
        classify(rows, disk)


def test_an_already_archived_row_is_not_reported_as_work() -> None:
    """Re-running the repair must converge, not archive the same rows forever."""
    rows = [
        PlanRow(
            row_id="1",
            project_key="red-quant",
            file_path="docs/plans/gone-plan.md",
            content_hash="z" * 64,
            freshness_status="archived",
            indexed_at="2026-01-01T00:00:00+00:00",
            resolved_path=None,
        )
    ]

    decisions = classify(rows, [])

    assert _only(decisions, "1").verdict is PlanRowVerdict.CANONICAL
    assert _only(decisions, "1").reason == "already_archived"


@pytest.mark.parametrize("verdict", list(PlanRowVerdict))
def test_every_verdict_carries_a_reason(verdict: PlanRowVerdict) -> None:
    """A report an operator has to act on never says only WHAT, always WHY."""
    rows = [
        _row("canon", "p", "/disk/a-design.md", content_hash="a" * 64),
        _row(
            "alias",
            "p",
            "/alias/b-design.md",
            content_hash="b" * 64,
            resolved_path="/disk/b-design.md",
        ),
        _row("dup", "p", "rel/a-design.md", content_hash="a" * 64),
        _row("orphan", "p", "rel/gone-plan.md", content_hash="z" * 64),
    ]
    disk = [
        _disk("/disk/a-design.md", "p", "a" * 64),
        _disk("/disk/b-design.md", "p", "b" * 64),
    ]

    decisions = classify(rows, disk)
    matching = [d for d in decisions if d.verdict is verdict]

    assert matching, f"the fixture must produce at least one {verdict}"
    assert all(d.reason for d in matching)


# ── mutations: what would actually be written ───────────────────────────


def test_a_mutation_plan_never_contains_a_delete() -> None:
    """The operator rule, pinned where the statements are built.

    A verdict enum with no delete member is worth little if the writer can
    still issue one. Every mutation names its kind, and there are two.
    """
    from brain_v42.maintenance.plan_index_inventory import plan_mutations

    rows = [
        _row("rewrite", "red-games", "docs/a-design.md", content_hash="a" * 64),
        _row("orphan", "red-quant", "docs/gone-plan.md", content_hash="z" * 64),
    ]
    disk = [_disk("/repo/red_writer/docs/a-design.md", "red-writer", "a" * 64)]

    mutations = plan_mutations(classify(rows, disk))

    assert {m.kind for m in mutations} == {"rewrite", "archive"}


def test_a_rewrite_carries_the_previous_state_for_recovery() -> None:
    """`--apply` is reversible only if the report says what it overwrote."""
    from brain_v42.maintenance.plan_index_inventory import plan_mutations

    rows = [_row("1", "red-games", "docs/a-design.md", content_hash="a" * 64)]
    disk = [_disk("/repo/red_writer/docs/a-design.md", "red-writer", "a" * 64)]

    mutation = plan_mutations(classify(rows, disk))[0]

    assert mutation.before == {
        "file_path": "docs/a-design.md",
        "project_key": "red-games",
        "freshness_status": "fresh",
        "content_hash": "a" * 64,
        "indexed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": None,
        "freshness_source": None,
    }
    assert mutation.after == {
        "file_path": "/repo/red_writer/docs/a-design.md",
        "project_key": "red-writer",
        "freshness_status": "fresh",
    }


def test_a_mutation_carries_content_and_clock_concurrency_evidence() -> None:
    from brain_v42.maintenance.plan_index_inventory import plan_mutations

    row = PlanRow(
        row_id="1",
        project_key="red-games",
        file_path="docs/a-plan.md",
        content_hash="a" * 64,
        freshness_status="fresh",
        indexed_at="2026-01-01T00:00:00+00:00",
        freshness_source="plan_reindex",
        updated_at="2026-01-02T00:00:00+00:00",
    )
    disk = [_disk("/repo/red_writer/docs/a-plan.md", "red-writer", "a" * 64)]

    mutation = plan_mutations(classify([row], disk))[0]

    assert mutation.before["content_hash"] == "a" * 64
    assert mutation.before["indexed_at"] == "2026-01-01T00:00:00+00:00"
    assert mutation.before["updated_at"] == "2026-01-02T00:00:00+00:00"
    assert mutation.before["freshness_source"] == "plan_reindex"


def test_an_archive_changes_the_status_and_nothing_else() -> None:
    """Archiving must not also move a path: a recovered row has to be restorable
    to exactly what it was, and the path is what identifies it."""
    from brain_v42.maintenance.plan_index_inventory import plan_mutations

    rows = [_row("1", "red-quant", "docs/gone-plan.md", content_hash="z" * 64)]

    mutation = plan_mutations(classify(rows, []))[0]

    assert mutation.kind == "archive"
    assert mutation.after == {
        "file_path": "docs/gone-plan.md",
        "project_key": "red-quant",
        "freshness_status": "archived",
    }


def test_a_canonical_row_produces_no_mutation_at_all() -> None:
    from brain_v42.maintenance.plan_index_inventory import plan_mutations

    rows = [_row("1", "p", "/disk/a-design.md", content_hash="a" * 64)]
    disk = [_disk("/disk/a-design.md", "p", "a" * 64)]

    assert plan_mutations(classify(rows, disk)) == ()


def test_the_mutation_order_is_deterministic() -> None:
    """A dry run an operator read must be the run that executes."""
    from brain_v42.maintenance.plan_index_inventory import plan_mutations

    rows = [
        _row("b", "red-games", "docs/b-design.md", content_hash="b" * 64),
        _row("a", "red-quant", "docs/a-design.md", content_hash="z" * 64),
    ]
    disk = [_disk("/repo/x/b-design.md", "red-writer", "b" * 64)]

    forward = [m.row_id for m in plan_mutations(classify(rows, disk))]
    backward = [m.row_id for m in plan_mutations(classify(list(reversed(rows)), disk))]

    assert forward == backward == ["a", "b"]


# ── verification: rows == files, project by project ─────────────────────


def test_verification_counts_rows_against_real_files_per_project() -> None:
    """Acceptance criterion 4. A global total would hide a whole project."""
    from brain_v42.maintenance.plan_index_inventory import verify_projects

    rows = [
        _row("1", "red-writer", "/w/a-design.md", content_hash="a" * 64),
        _row("2", "red-writer", "/w/b-design.md", content_hash="b" * 64),
        _row("3", "red-games", "/g/c-design.md", content_hash="c" * 64),
    ]
    disk = [
        _disk("/w/a-design.md", "red-writer", "a" * 64),
        _disk("/w/b-design.md", "red-writer", "b" * 64),
        _disk("/g/c-design.md", "red-games", "c" * 64),
        _disk("/g/d-plan.md", "red-games", "d" * 64),
    ]

    report = {v.project_key: v for v in verify_projects(rows, disk)}

    assert report["red-writer"].matches is True
    assert report["red-games"].matches is False
    assert (report["red-games"].indexed_rows, report["red-games"].disk_files) == (1, 2)


def test_verification_rejects_equal_counts_with_the_wrong_paths() -> None:
    """Equal totals do not prove that a project owns the files it claims."""
    from brain_v42.maintenance.plan_index_inventory import verify_projects

    rows = [_row("1", "red-writer", "/w/wrong-plan.md", content_hash="a" * 64)]
    disk = [_disk("/w/right-plan.md", "red-writer", "a" * 64)]

    report = {v.project_key: v for v in verify_projects(rows, disk)}

    assert report["red-writer"].indexed_rows == report["red-writer"].disk_files == 1
    assert report["red-writer"].matches is False


def test_verification_ignores_archived_rows() -> None:
    """An archived row is out of the views, so it must not count as coverage."""
    from brain_v42.maintenance.plan_index_inventory import verify_projects

    rows = [
        _row("live", "p", "/d/a-design.md", content_hash="a" * 64),
        PlanRow(
            row_id="gone",
            project_key="p",
            file_path="docs/old-plan.md",
            content_hash="z" * 64,
            freshness_status="archived",
            indexed_at="2026-01-01T00:00:00+00:00",
        ),
    ]
    disk = [_disk("/d/a-design.md", "p", "a" * 64)]

    report = {v.project_key: v for v in verify_projects(rows, disk)}

    assert report["p"].indexed_rows == 1
    assert report["p"].matches is True


def test_a_project_with_files_and_no_rows_is_reported_not_omitted() -> None:
    """The silent case: nothing indexed at all reads as "no problem" in a
    report keyed on rows."""
    from brain_v42.maintenance.plan_index_inventory import verify_projects

    report = {
        v.project_key: v
        for v in verify_projects([], [_disk("/d/a-design.md", "never-indexed", "a" * 64)])
    }

    assert report["never-indexed"].indexed_rows == 0
    assert report["never-indexed"].disk_files == 1
    assert report["never-indexed"].matches is False
