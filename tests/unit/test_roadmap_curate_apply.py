"""Unit tests for scripts.roadmap_curate apply path (mocked session, no DB)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from scripts import roadmap_curate
from scripts.roadmap_curate import CurationDraft, apply_proposals, persist_proposals
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.services.proposal_service import PostConditionError

_FEATURE_REVIEWED_AT = datetime(2026, 7, 21, tzinfo=UTC)
_PROPOSAL_CREATED_AT = datetime(2026, 7, 22, tzinfo=UTC)


def test_post_condition_error_remains_a_compatible_reexport() -> None:
    assert roadmap_curate.PostConditionError is PostConditionError


def _proposal_row(
    proposal_id: int = 1,
    op: str = "archive",
    payload: dict | None = None,
    feature_id=None,
) -> dict:
    return {
        "id": proposal_id,
        "op": op,
        "feature_id": feature_id or uuid4(),
        "payload": payload if payload is not None else {},
        "rationale": "r",
        "status": "proposed",
        "created_at": _PROPOSAL_CREATED_AT,
    }


def _feature_row(
    feature_id,
    *,
    status: str = "building",
    name: str = "Feature",
    project_key: str = "red",
) -> dict:
    return {
        "id": feature_id,
        "project_key": project_key,
        "status": status,
        "name": name,
        "merged_into": None,
        "pinned": False,
        "status_updated_at": _FEATURE_REVIEWED_AT,
        "updated_at": _FEATURE_REVIEWED_AT,
    }


def _session_with(side_effects: list[Any]) -> tuple[Any, MagicMock]:
    """Fake session factory — spec=AsyncSession to catch session.mappings()."""
    mock_session = MagicMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(side_effect=side_effects)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)

    @asynccontextmanager
    async def factory():
        yield mock_session

    return factory, mock_session


def _mappings_all(rows: list[dict]) -> MagicMock:
    r = MagicMock()
    r.mappings.return_value.all.return_value = rows
    r.mappings.return_value.one_or_none.return_value = rows[0] if rows else None
    return r


def _mappings_one(row: dict) -> MagicMock:
    r = MagicMock()
    r.mappings.return_value.one.return_value = row
    r.mappings.return_value.one_or_none.return_value = row
    return r


def _scalar_one(value: Any) -> MagicMock:
    r = MagicMock()
    r.scalar_one.return_value = value
    return r


class TestApplyArchive:
    @pytest.mark.asyncio
    async def test_archive_applies_and_checks_postcondition(self):
        row = _proposal_row(op="archive")
        factory, _ = _session_with(
            [
                _mappings_all([row]),  # SELECT proposals
                _mappings_all([_feature_row(row["feature_id"])]),  # lock the prior state
                MagicMock(),  # UPDATE features → archived
                _mappings_one({"status": "archived"}),  # post-condition re-read
                MagicMock(),  # UPDATE proposal → applied + apply_log
            ]
        )
        applied = await apply_proposals(factory, [1])
        assert applied == 1

    @pytest.mark.asyncio
    async def test_postcondition_failure_skips_proposal(self):
        row = _proposal_row(op="archive")
        factory, session = _session_with(
            [
                _mappings_all([row]),
                _mappings_all([_feature_row(row["feature_id"])]),  # lock the prior state
                MagicMock(),
                _mappings_one({"status": "research"}),  # unexpected state → rollback
            ]
        )
        applied = await apply_proposals(factory, [1])
        assert applied == 0
        assert session.execute.await_count == 4


class TestApplyStatusRename:
    @pytest.mark.asyncio
    async def test_status_postcondition(self):
        row = _proposal_row(op="status", payload={"status": "deployed"})
        factory, _ = _session_with(
            [
                _mappings_all([row]),
                _mappings_all([_feature_row(row["feature_id"])]),  # lock the prior state
                MagicMock(),
                _mappings_one({"status": "deployed"}),
                MagicMock(),
            ]
        )
        assert await apply_proposals(factory, [1]) == 1

    @pytest.mark.asyncio
    async def test_rename_postcondition(self):
        row = _proposal_row(op="rename", payload={"name": "Nouveau nom"})
        factory, _ = _session_with(
            [
                _mappings_all([row]),
                _mappings_all([_feature_row(row["feature_id"], name="Ancien nom")]),
                MagicMock(),
                _mappings_one({"name": "Nouveau nom"}),
                MagicMock(),
            ]
        )
        assert await apply_proposals(factory, [1]) == 1


class TestApplyMerge:
    @pytest.mark.asyncio
    async def test_merge_execute_sequence_and_postconditions(self):
        into = uuid4()
        row = _proposal_row(op="merge", payload={"into": str(into)})
        factory, session = _session_with(
            [
                _mappings_all([row]),  # SELECT proposals
                _mappings_all(
                    [
                        _feature_row(row["feature_id"], status="research", name="Perdant"),
                        _feature_row(into, status="research", name="Cible"),
                    ]
                ),  # source + target locks
                MagicMock(),  # UPDATE fa repoint (RETURNING → iter vide)
                MagicMock(),  # DELETE fa restants (RETURNING → iter vide)
                MagicMock(),  # UPDATE features loser
                _mappings_one({"merged_into": into, "status": "archived"}),
                _scalar_one(0),  # 0 artifacts left on the loser
                MagicMock(),  # UPDATE proposal → applied + apply_log
            ]
        )
        assert await apply_proposals(factory, [1]) == 1
        assert session.execute.call_count == 8

    @pytest.mark.asyncio
    async def test_merge_leftover_artifacts_fails_postcondition(self):
        into = uuid4()
        row = _proposal_row(op="merge", payload={"into": str(into)})
        factory, session = _session_with(
            [
                _mappings_all([row]),
                _mappings_all(
                    [
                        _feature_row(row["feature_id"], status="research", name="Perdant"),
                        _feature_row(into, status="research", name="Cible"),
                    ]
                ),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                _mappings_one({"merged_into": into, "status": "archived"}),
                _scalar_one(2),  # artifacts orphelins → post-condition KO
            ]
        )
        assert await apply_proposals(factory, [1]) == 0
        assert session.execute.await_count == 7


class TestAllowedOps:
    @pytest.mark.asyncio
    async def test_wet_mode_skips_merge_and_rename(self):
        rows = [
            _proposal_row(proposal_id=1, op="merge", payload={"into": str(uuid4())}),
            _proposal_row(proposal_id=2, op="rename", payload={"name": "n"}),
            _proposal_row(proposal_id=3, op="archive"),
        ]
        factory, _ = _session_with(
            [
                _mappings_all([rows[0]]),
                _mappings_all([rows[1]]),
                _mappings_all([rows[2]]),
                # only archive (id 3) is applied:
                _mappings_all([_feature_row(rows[2]["feature_id"])]),  # lock the prior state
                MagicMock(),
                _mappings_one({"status": "archived"}),
                MagicMock(),
            ]
        )
        applied = await apply_proposals(factory, [1, 2, 3], allowed_ops=("archive", "status"))
        assert applied == 1


class TestApplyLogProvenance:
    """Un-merging proved impossible on 2026-07-05 (10 aberrant merges accepted for
    lack of provenance: artifacts commingled without a trace). Each apply now
    captures in proposals.apply_log what is needed to revert: prior states +
    moved artifacts / deleted duplicate links."""

    @staticmethod
    def _rows_result(rows: list[tuple]) -> MagicMock:
        r = MagicMock()
        r.all.return_value = rows
        return r

    @staticmethod
    def _final_update_params(session: MagicMock) -> dict:
        stmt = session.execute.await_args_list[-1].args[0]
        return dict(stmt.compile().params)

    @pytest.mark.asyncio
    async def test_merge_apply_log_captures_prior_state_and_artifacts(self):
        into = uuid4()
        a1, a2, a3 = uuid4(), uuid4(), uuid4()
        row = _proposal_row(op="merge", payload={"into": str(into)})
        factory, session = _session_with(
            [
                _mappings_all([row]),  # SELECT proposals
                _mappings_all(
                    [
                        _feature_row(row["feature_id"], status="research", name="Perdant"),
                        _feature_row(into, status="research", name="Cible"),
                    ]
                ),  # source + target locks
                self._rows_result([("decision", a1), ("learning", a2)]),  # moved
                self._rows_result([("snippet", a3)]),  # deleted duplicates
                MagicMock(),  # UPDATE features loser
                _mappings_one({"merged_into": into, "status": "archived"}),
                _scalar_one(0),
                MagicMock(),  # UPDATE proposal → applied + apply_log
            ]
        )
        assert await apply_proposals(factory, [1]) == 1
        log = self._final_update_params(session)["apply_log"]
        assert log["op"] == "merge"
        assert log["into"] == str(into)
        assert log["loser_prior_status"] == "research"
        assert log["loser_prior_name"] == "Perdant"
        assert {"artifact_type": "decision", "artifact_id": str(a1)} in log["moved_artifacts"]
        assert {"artifact_type": "learning", "artifact_id": str(a2)} in log["moved_artifacts"]
        assert log["duplicate_links_deleted"] == [
            {"artifact_type": "snippet", "artifact_id": str(a3)}
        ]

    @pytest.mark.asyncio
    async def test_archive_apply_log_captures_prior_status(self):
        row = _proposal_row(op="archive")
        factory, session = _session_with(
            [
                _mappings_all([row]),
                _mappings_all([_feature_row(row["feature_id"])]),  # lock prior
                MagicMock(),  # UPDATE features
                _mappings_one({"status": "archived"}),  # post-condition
                MagicMock(),  # UPDATE proposal → applied + apply_log
            ]
        )
        assert await apply_proposals(factory, [1]) == 1
        log = self._final_update_params(session)["apply_log"]
        assert log == {"op": "archive", "prior_status": "building"}

    @pytest.mark.asyncio
    async def test_rename_apply_log_captures_prior_name(self):
        row = _proposal_row(op="rename", payload={"name": "Nouveau nom"})
        factory, session = _session_with(
            [
                _mappings_all([row]),
                _mappings_all([_feature_row(row["feature_id"], name="Ancien nom")]),
                MagicMock(),
                _mappings_one({"name": "Nouveau nom"}),
                MagicMock(),
            ]
        )
        assert await apply_proposals(factory, [1]) == 1
        log = self._final_update_params(session)["apply_log"]
        assert log == {"op": "rename", "prior_name": "Ancien nom"}

    @pytest.mark.asyncio
    async def test_status_apply_log_captures_prior_status(self):
        row = _proposal_row(op="status", payload={"status": "deployed"})
        factory, session = _session_with(
            [
                _mappings_all([row]),
                _mappings_all([_feature_row(row["feature_id"])]),
                MagicMock(),
                _mappings_one({"status": "deployed"}),
                MagicMock(),
            ]
        )
        assert await apply_proposals(factory, [1]) == 1
        log = self._final_update_params(session)["apply_log"]
        assert log == {"op": "status", "prior_status": "building"}


class TestPersistProposals:
    @pytest.mark.asyncio
    async def test_empty_drafts_noop(self):
        factory = MagicMock()
        res = await persist_proposals(factory, [])
        assert res.inserted == [] and res.refreshed == [] and res.rejected_skipped == 0
        factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_duplicate_proposed_is_refreshed_not_reinserted(self):
        """A 'proposed' duplicate → no INSERT, id returned as refreshed (a non-inert WET flip)."""
        draft = CurationDraft(op="archive", feature_id=uuid4(), payload={}, rationale="r")
        dup_found = MagicMock()
        dup_found.first = MagicMock(return_value=(123, "proposed"))
        factory, session = _session_with([dup_found])
        res = await persist_proposals(factory, [draft])
        assert res.inserted == [] and res.refreshed == [123] and res.rejected_skipped == 0
        assert session.execute.await_count == 1  # SELECT only, no INSERT

    @pytest.mark.asyncio
    async def test_duplicate_rejected_is_skipped_for_good(self):
        """A 'rejected' duplicate → neither INSERT nor refresh — no resurrection in review."""
        draft = CurationDraft(op="archive", feature_id=uuid4(), payload={}, rationale="r")
        dup_found = MagicMock()
        dup_found.first = MagicMock(return_value=(55, "rejected"))
        factory, session = _session_with([dup_found])
        res = await persist_proposals(factory, [draft])
        assert res.inserted == [] and res.refreshed == [] and res.rejected_skipped == 1
        assert session.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_new_draft_is_inserted(self):
        """No duplicate → SELECT (None) then INSERT (returning id)."""
        draft = CurationDraft(op="archive", feature_id=uuid4(), payload={}, rationale="r")
        dup_none = MagicMock()
        dup_none.first = MagicMock(return_value=None)
        factory, session = _session_with([dup_none, _scalar_one(7)])
        res = await persist_proposals(factory, [draft])
        assert res.inserted == [7] and res.refreshed == [] and res.rejected_skipped == 0
        assert session.execute.await_count == 2  # SELECT + INSERT


class TestArchiveEmission:
    """The EMISSION of the archive op, from model text through to the INSERT.

    Ticket 2e921e14, point 2: "the nightly run cannot say archive" had been false
    since 2026-08-20 (a real emission on the red project), but this file only
    tested the APPLICATION — a pipeline that lost the op between the parse and the
    persistence would have stayed green. These tests follow a model answer
    CONTAINING archive down to the INSERT's values: that is the test-backed
    property the ticket required, without eroding the guard "a PINNED feature
    accepts ONLY the status op".
    """

    @pytest.mark.asyncio
    async def test_a_model_archive_travels_from_response_to_the_insert(self):
        import json as _json

        from scripts.roadmap_curate import FeatureCard, ProjectBatch, parse_and_validate

        feature_id = uuid4()
        batch = ProjectBatch(
            project_key="brain-v42",
            features=[
                FeatureCard(
                    id=feature_id,
                    name="update dependency X (message de commit promu)",
                    status="research",
                    pinned=False,
                )
            ],
        )
        content = _json.dumps(
            [
                {
                    "op": "archive",
                    "feature_id": str(feature_id),
                    "rationale": "bruit sans valeur roadmap",
                }
            ]
        )

        drafts = parse_and_validate(content, batch)

        assert [d.op for d in drafts] == ["archive"]
        assert drafts[0].payload == {}

        dup_none = MagicMock()
        dup_none.first = MagicMock(return_value=None)
        factory, session = _session_with([dup_none, _scalar_one(91)])
        res = await persist_proposals(factory, drafts)

        assert res.inserted == [91]
        insert_call = session.execute.await_args_list[1]
        compiled = str(insert_call.args[0].compile(compile_kwargs={"literal_binds": False}))
        assert "roadmap_curation_proposals" in compiled
        values = insert_call.args[0].compile().params
        assert values["op"] == "archive"
        assert values["feature_id"] == feature_id
        assert values["payload"] == {}

    def test_emission_on_a_pinned_feature_stays_refused(self):
        """The inverse witness: proving the emission must not erode the prompt's
        guard — PINNED accepts only the status op, and it is the PARSER that
        refuses it, not a sentence."""
        import json as _json

        from scripts.roadmap_curate import (
            FeatureCard,
            ProjectBatch,
            ResponseParseError,
            parse_and_validate,
        )

        pinned_id = uuid4()
        batch = ProjectBatch(
            project_key="brain-v42",
            features=[
                FeatureCard(id=pinned_id, name="Feature épinglée", status="building", pinned=True)
            ],
        )
        content = _json.dumps([{"op": "archive", "feature_id": str(pinned_id)}])

        with pytest.raises(ResponseParseError, match="pinned"):
            parse_and_validate(content, batch)
