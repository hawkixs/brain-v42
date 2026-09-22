"""What each `indexed_plans` row is, before anything is allowed to touch it.

Two defects filed plans under paths the scanner no longer produces, and the
rows they left behind look alike from the database alone:

* a **relative** scan path was resolved against the service's own working
  directory, so a project indexed another project's plans under its own key;
* a scan root reached through a **symlink** was canonicalised by the traversal,
  so rows written under the alias were never refreshed again.

They need opposite treatments. A row whose content still exists on disk is
mis-filed and must be **rewritten**; a row with nothing to point at is the last
remaining copy of that plan. Calling both "orphans" is what made the repair
look like a delete.

NOTHING HERE DELETES. The operator rule is that no knowledge leaves the brain.
Archiving sets `freshness_status='archived'`, which
`indexed_plan_search_service` already filters out of every default view, and
leaves every byte in place. `PlanRowVerdict` carries no delete member, and a
test pins that it never gains one.

This module is the pure half -- given rows and disk facts, the verdict. Reading
the database and walking the filesystem live in the CLI
(`scripts/plan_index_inventory.py`), so the decision stays testable without
either, and a dry run costs nothing but a read.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class PlanRowVerdict(StrEnum):
    """What the repair should do with one row. There is no delete."""

    CANONICAL = "canonical"
    """Nothing to do: the row's path is a real file owned by its project."""

    REWRITE = "rewrite"
    """The content is on disk under another name, another owner, or both."""

    ARCHIVE_DUPLICATE = "archive_duplicate"
    """Another row already carries this content at its canonical path."""

    ARCHIVE_ORPHAN = "archive_orphan"
    """No file carries this content any more. This row IS the plan."""


@dataclass(frozen=True, slots=True)
class PlanRow:
    """One `indexed_plans` row, plus the one filesystem fact the CLI resolved.

    `resolved_path` is the canonical form of `file_path` when it exists on
    disk, and None otherwise. It is passed in rather than computed so this
    module never touches the filesystem: the symlink case is the whole reason
    the rows are wrong, and a classifier that resolves paths itself would
    inherit the ambiguity it is meant to report.
    """

    row_id: str
    project_key: str
    file_path: str
    content_hash: str
    freshness_status: str
    indexed_at: str
    resolved_path: str | None = None


@dataclass(frozen=True, slots=True)
class DiskPlanFile:
    """One real plan file, under the canonical form of a configured scan root."""

    path: str
    project_key: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class RowDecision:
    """One verdict, with the target it would write and the reason it chose."""

    row: PlanRow
    verdict: PlanRowVerdict
    target_path: str | None = None
    target_project: str | None = None
    reason: str = ""


def _index_disk(
    disk_files: list[DiskPlanFile],
) -> tuple[dict[str, DiskPlanFile], dict[str, DiskPlanFile]]:
    """Index disk files by path and by content, both deterministically.

    Several files can carry byte-identical content. Picking the smallest path
    makes the chosen target independent of directory-walk order, so two dry
    runs over an unchanged tree produce the same plan -- which is what makes a
    dry run worth reading before an apply.
    """
    by_path: dict[str, DiskPlanFile] = {}
    by_hash: dict[str, DiskPlanFile] = {}
    for candidate in sorted(disk_files, key=lambda f: f.path):
        by_path.setdefault(candidate.path, candidate)
        by_hash.setdefault(candidate.content_hash, candidate)
    return by_path, by_hash


def _first_pass(
    rows: list[PlanRow],
    by_path: dict[str, DiskPlanFile],
    by_hash: dict[str, DiskPlanFile],
    path_owner_row: dict[str, str],
) -> list[RowDecision]:
    decisions: list[RowDecision] = []
    for row in rows:
        if row.freshness_status == "archived":
            # Already out of the views. Re-archiving it every run would make
            # the report grow instead of converge.
            decisions.append(
                RowDecision(row=row, verdict=PlanRowVerdict.CANONICAL, reason="already_archived")
            )
            continue

        on_disk = by_path.get(row.file_path)
        if on_disk is not None:
            if on_disk.project_key == row.project_key:
                decisions.append(
                    RowDecision(
                        row=row,
                        verdict=PlanRowVerdict.CANONICAL,
                        reason="path_and_owner_match",
                    )
                )
            else:
                decisions.append(
                    RowDecision(
                        row=row,
                        verdict=PlanRowVerdict.REWRITE,
                        target_path=row.file_path,
                        target_project=on_disk.project_key,
                        reason="owner_mismatch",
                    )
                )
            continue

        alias_target = by_path.get(row.resolved_path) if row.resolved_path else None
        if alias_target is not None:
            holder = path_owner_row.get(alias_target.path)
            if holder is not None and holder != row.row_id:
                decisions.append(
                    RowDecision(
                        row=row,
                        verdict=PlanRowVerdict.ARCHIVE_DUPLICATE,
                        reason="alias_of_an_already_indexed_path",
                    )
                )
            else:
                decisions.append(
                    RowDecision(
                        row=row,
                        verdict=PlanRowVerdict.REWRITE,
                        target_path=alias_target.path,
                        target_project=alias_target.project_key,
                        reason="symlink_alias",
                    )
                )
            continue

        twin = by_hash.get(row.content_hash)
        if twin is None:
            decisions.append(
                RowDecision(
                    row=row,
                    verdict=PlanRowVerdict.ARCHIVE_ORPHAN,
                    reason="no_file_carries_this_content",
                )
            )
            continue

        holder = path_owner_row.get(twin.path)
        if holder is not None and holder != row.row_id:
            decisions.append(
                RowDecision(
                    row=row,
                    verdict=PlanRowVerdict.ARCHIVE_DUPLICATE,
                    reason="content_already_indexed_at_its_canonical_path",
                )
            )
        else:
            decisions.append(
                RowDecision(
                    row=row,
                    verdict=PlanRowVerdict.REWRITE,
                    target_path=twin.path,
                    target_project=twin.project_key,
                    reason="content_found_on_disk_by_hash",
                )
            )
    return decisions


def _resolve_collisions(decisions: list[RowDecision]) -> list[RowDecision]:
    """Two rewrites onto one path would abort the transaction, not one row.

    `indexed_plans.file_path` is UNIQUE table-wide. Several wrong rows can
    carry the same content and therefore aim at the same file. The most
    recently indexed keeps the name -- it is the closest to what the file says
    today -- and the others are archived. `row_id` breaks an exact tie so the
    plan does not depend on the order rows came back in.
    """
    contenders: dict[str, list[RowDecision]] = {}
    for decision in decisions:
        if decision.verdict is PlanRowVerdict.REWRITE and decision.target_path is not None:
            contenders.setdefault(decision.target_path, []).append(decision)

    demoted: dict[str, RowDecision] = {}
    for candidates in contenders.values():
        if len(candidates) < 2:
            continue
        winner = max(candidates, key=lambda d: (d.row.indexed_at, d.row.row_id))
        for loser in candidates:
            if loser is winner:
                continue
            demoted[loser.row.row_id] = RowDecision(
                row=loser.row,
                verdict=PlanRowVerdict.ARCHIVE_DUPLICATE,
                reason="lost_a_path_collision_to_a_more_recent_row",
            )

    return [demoted.get(decision.row.row_id, decision) for decision in decisions]


def classify(rows: list[PlanRow], disk_files: list[DiskPlanFile]) -> tuple[RowDecision, ...]:
    """Decide what to do with every row, without touching anything."""
    by_path, by_hash = _index_disk(disk_files)
    path_owner_row = {row.file_path: row.row_id for row in rows}
    return tuple(_resolve_collisions(_first_pass(rows, by_path, by_hash, path_owner_row)))


@dataclass(frozen=True, slots=True)
class RowMutation:
    """One row's change, with the state it replaces.

    `before` is the recovery payload: an apply is reversible only because the
    report says what it overwrote. `kind` has two members and will not grow a
    third -- see the module docstring.
    """

    row_id: str
    kind: Literal["rewrite", "archive"]
    before: dict[str, str]
    after: dict[str, str]


def plan_mutations(decisions: Sequence[RowDecision]) -> tuple[RowMutation, ...]:
    """Turn verdicts into the exact writes an apply would issue.

    Sorted by `row_id` so the dry run an operator reads is the run that
    executes, whatever order the rows came back in.
    """
    mutations: list[RowMutation] = []
    for decision in decisions:
        row = decision.row
        before = {
            "file_path": row.file_path,
            "project_key": row.project_key,
            "freshness_status": row.freshness_status,
        }
        if decision.verdict is PlanRowVerdict.REWRITE:
            mutations.append(
                RowMutation(
                    row_id=row.row_id,
                    kind="rewrite",
                    before=before,
                    after={
                        "file_path": decision.target_path or row.file_path,
                        "project_key": decision.target_project or row.project_key,
                        "freshness_status": row.freshness_status,
                    },
                )
            )
        elif decision.verdict in (
            PlanRowVerdict.ARCHIVE_DUPLICATE,
            PlanRowVerdict.ARCHIVE_ORPHAN,
        ):
            mutations.append(
                RowMutation(
                    row_id=row.row_id,
                    kind="archive",
                    before=before,
                    # The path is deliberately untouched: it is what identifies
                    # the row, and a recovered row has to come back to exactly
                    # what it was.
                    after={**before, "freshness_status": "archived"},
                )
            )
    return tuple(sorted(mutations, key=lambda m: m.row_id))


@dataclass(frozen=True, slots=True)
class ProjectVerification:
    """Acceptance criterion 4, for one project: rows against real files."""

    project_key: str
    indexed_rows: int
    disk_files: int

    @property
    def matches(self) -> bool:
        return self.indexed_rows == self.disk_files


def verify_projects(
    rows: Sequence[PlanRow], disk_files: Sequence[DiskPlanFile]
) -> tuple[ProjectVerification, ...]:
    """Count live rows against real plan files, project by project.

    Per project and never as one total: a global count balances a project
    missing ten plans against another carrying ten it does not own, and reads
    green. Archived rows do not count as coverage -- they are out of every
    default view, which is the whole point of archiving them.

    A project with files and no rows at all is reported, not omitted: keying
    the report on rows is exactly how "nothing was ever indexed here" reads as
    "no problem".
    """
    live: Counter[str] = Counter(
        row.project_key for row in rows if row.freshness_status != "archived"
    )
    on_disk: Counter[str] = Counter(entry.project_key for entry in disk_files)
    return tuple(
        ProjectVerification(
            project_key=project_key,
            indexed_rows=live.get(project_key, 0),
            disk_files=on_disk.get(project_key, 0),
        )
        for project_key in sorted(set(live) | set(on_disk))
    )
