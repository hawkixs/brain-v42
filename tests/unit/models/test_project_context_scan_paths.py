"""A plan scan path that nothing can resolve must be refused where it is WRITTEN.

Measured 2026-09-22: 25 of the configured scan paths across 12 projects are
relative — `docs/plans`, `docs/specs`, and for `red-orchestrator` a `~/...`
that nothing expands. The plan indexer refuses each of them at every startup
with `plan_indexer.invalid_scan_path`, and then carries on. The result is 131
plans that have never been indexed, silently, for as long as the paths have
been configured — including 42 for red-quant and 37 for red-writer — and the
`plan` type is the third most-read in the corpus.

A warning repeated at every boot is not a guard. It is a guard that lost.

The boundary this file pins: the WRITER refuses what is structurally wrong,
because no environment will ever make a relative path resolvable from an
unknown working directory. The READER keeps refusing what is environmentally
wrong — `missing`, `not_directory`, `unreadable` — because those are true at
read time and can change after the write.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from brain_v42.models.project_context import ProjectContextCreate, ProjectContextUpdate

ABSOLUTE = "/home/hawixs/hawkixs_infra/git_repo/brain_v42/docs"


def _create(**kwargs: object) -> ProjectContextCreate:
    """`name` and `description` are required; they are noise for this file."""
    return ProjectContextCreate(
        project_key="brain-v42", name="brain-v42", description="fixture", **kwargs
    )


class TestTheWriterRefusesWhatNoEnvironmentCanFix:
    @pytest.mark.parametrize(
        "path",
        [
            "docs/plans",
            "docs/specs/",
            "projects/red-watcher/docs/plans",
            "docs/IMPLEMENTATION_PLAN.md",
        ],
        ids=["bare", "trailing-slash", "nested", "file"],
    )
    def test_a_relative_scan_path_is_refused_on_create(self, path: str) -> None:
        with pytest.raises(ValidationError) as exc:
            _create(plan_scan_paths=[path])
        assert path in str(exc.value), "the refusal must name the offending value"

    def test_a_tilde_scan_path_is_refused_because_nothing_expands_it(self) -> None:
        """`~/...` is what red-orchestrator carries, and `Path` never expands it.

        It is the same defect as the switch runbook's token path, one layer
        down: a string that reads absolute to a human and is relative to
        `Path.is_absolute()`.
        """
        with pytest.raises(ValidationError):
            _create(plan_scan_paths=["~/hawkixs_infra/git_repo/ReD_v1/docs/plans"])

    def test_a_relative_scan_path_is_refused_on_update_too(self) -> None:
        """Create-only validation would leave the whole installed base editable."""
        with pytest.raises(ValidationError):
            ProjectContextUpdate(plan_scan_paths=[ABSOLUTE, "docs/plans"])

    def test_an_absolute_scan_path_is_accepted(self) -> None:
        created = _create(plan_scan_paths=[ABSOLUTE])
        assert created.plan_scan_paths == [ABSOLUTE]
        assert ProjectContextUpdate(plan_scan_paths=[ABSOLUTE]).plan_scan_paths == [ABSOLUTE]

    def test_no_scan_path_at_all_stays_valid(self) -> None:
        """Most projects configure none, and that is not an error."""
        assert _create().plan_scan_paths == []
        assert ProjectContextUpdate().plan_scan_paths is None

    def test_the_writer_does_not_check_the_filesystem(self) -> None:
        """An absolute path that does not exist is the READER's refusal, not this one.

        Validating existence here would make a project context unwritable from
        a host where the directory is absent — a CI job, a restored clone, a
        second machine — and would couple a data model to a filesystem.
        """
        path = "/nonexistent/but/absolute/docs/plans"
        assert _create(plan_scan_paths=[path]).plan_scan_paths == [path]
