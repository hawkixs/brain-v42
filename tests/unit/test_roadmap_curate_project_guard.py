"""`--apply-ids` must not be able to mutate another project's roadmap.

Ticket `e9b2faf4`, defect 2. The "foreign id refused by name" guard of `f67af05`
lives on the MCP side only: `ProposalService._load_proposed` adds an EXISTS on
`features.project_key` when it receives a `project_group`, and the MCP tools pass
one. The CLI passed nothing, so the guard was inert on exactly the path an
operator drives by hand — `--apply-ids "3,4"` typed from a morning review, where
a mistyped id is the expected human error and 25 other projects are one digit
away.

The fix REUSES that guard rather than reproducing it: the CLI now requires
`--project-key` alongside `--apply-ids` and forwards it as `project_group`. A
second predicate written here would be a second source of truth for the same
rule, and the two would only diverge at read time.

WHY THE REFUSAL HAD TO BE NAMED. Passing the group is enough to stop the
mutation, but not enough to be honest: the guard makes a foreign row simply not
match, so the service raises `ProposalNotFoundError` — which `apply_proposals`
already swallowed with a bare `continue`. An operator asking for three ids and
getting "2 applied" had to notice the arithmetic. The refusal is now printed,
and it says what the CLI can actually establish: unknown id OR outside the
declared project. It does not claim to tell the two apart, because at that point
it cannot.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from scripts.roadmap_curate import apply_proposals, main
from sqlalchemy.ext.asyncio import AsyncSession

_PROJECT = "integ-guard"
_PROPOSAL_CREATED_AT = datetime(2026, 9, 2, tzinfo=UTC)


def _mappings_all(rows: list[dict]) -> MagicMock:
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows
    return result


def _mappings_one(row: dict | None) -> MagicMock:
    result = MagicMock()
    result.mappings.return_value.one.return_value = row
    result.mappings.return_value.one_or_none.return_value = row
    return result


def _proposal_row(proposal_id: int = 1) -> dict:
    return {
        "id": proposal_id,
        "op": "archive",
        "feature_id": uuid4(),
        "payload": {},
        "rationale": "r",
        "status": "proposed",
        "created_at": _PROPOSAL_CREATED_AT,
    }


def _feature_row(feature_id: Any) -> dict:
    return {
        "id": feature_id,
        "project_key": _PROJECT,
        "status": "building",
        "name": "Feature",
        "merged_into": None,
        "pinned": False,
        "status_updated_at": _PROPOSAL_CREATED_AT,
        "updated_at": _PROPOSAL_CREATED_AT,
    }


def _session_with(results: list[Any]) -> tuple[Any, MagicMock]:
    """A recording session that replays `results` and remembers every statement."""
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=results)
    session.begin = MagicMock(return_value=_atomic())

    @asynccontextmanager
    async def factory():
        yield session

    return factory, session


@asynccontextmanager
async def _atomic():
    yield


def _statements(session: MagicMock) -> list[str]:
    return [str(call.args[0]) for call in session.execute.await_args_list]


class TestTheCliDeclaresItsProject:
    def test_apply_ids_without_a_project_key_is_refused_before_any_work(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The declaration is MANDATORY, not a default.

        A default would put the guard back to sleep: `--project-key brain-v42`
        assumed would let a `red-lab` id through on a brain-v42 review, which is
        the exact typo this defect is about.
        """
        monkeypatch.setattr("sys.argv", ["roadmap_curate", "--apply-ids", "3,4"])

        with pytest.raises(SystemExit) as exc:
            import asyncio

            asyncio.run(main())

        assert exc.value.code == 2

    def test_project_key_alone_is_accepted_without_apply_ids(self) -> None:
        """Negative witness: the flag must not become mandatory for the propose path.

        The nightly run passes neither, and making `--project-key` globally
        required would fail every night in the name of guarding a path the night
        never takes.
        """
        from scripts.roadmap_curate import _build_parser

        parsed = _build_parser().parse_args(["--limit", "3"])
        assert parsed.apply_ids is None
        assert parsed.project_key is None


class TestTheGuardIsTheSharedOne:
    @pytest.mark.asyncio
    async def test_the_select_carries_the_project_predicate(self) -> None:
        """The predicate comes from ProposalService, not from a copy made here.

        Read off the SQL actually emitted: a test that asserted the argument was
        passed would prove the call, never that the guard reached the database.
        """
        row = _proposal_row()
        factory, session = _session_with(
            [
                _mappings_one(row),
                _mappings_all([_feature_row(row["feature_id"])]),
                MagicMock(),
                _mappings_one({"status": "archived"}),
                MagicMock(),
            ]
        )

        await apply_proposals(factory, [1], project_key=_PROJECT)

        select_sql = _statements(session)[0]
        assert "features" in select_sql
        assert "project_key" in select_sql

    @pytest.mark.asyncio
    async def test_a_foreign_id_is_refused_by_name_and_mutates_nothing(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """THE test of this defect: the guard filters, and the refusal is audible.

        The foreign row simply does not match, so the SELECT returns nothing —
        exactly what the MCP guard produces. One statement is executed and no
        UPDATE follows.
        """
        factory, session = _session_with([_mappings_one(None)])

        applied = await apply_proposals(factory, [7], project_key=_PROJECT)

        assert applied == 0
        assert len(_statements(session)) == 1
        out = capsys.readouterr().out
        assert "7" in out
        assert _PROJECT in out

    @pytest.mark.asyncio
    async def test_an_id_of_the_declared_project_still_applies(self) -> None:
        """Negative witness: without it, a guard that refuses everything would pass."""
        row = _proposal_row()
        factory, _ = _session_with(
            [
                _mappings_one(row),
                _mappings_all([_feature_row(row["feature_id"])]),
                MagicMock(),
                _mappings_one({"status": "archived"}),
                MagicMock(),
            ]
        )

        assert await apply_proposals(factory, [1], project_key=_PROJECT) == 1
