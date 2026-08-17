"""Unit tests for the shared proposal mutation service."""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.services import proposal_service as proposal_service_module
from brain_v42.services.proposal_service import (
    ProposalApplyError,
    ProposalNotFoundError,
    ProposalNotProposedError,
    ProposalService,
    ProposalStateConflictError,
)


def _result_row(row: dict[str, Any] | None) -> MagicMock:
    result = MagicMock()
    result.mappings.return_value.one_or_none.return_value = row
    result.mappings.return_value.one.return_value = row
    return result


def _scalar(value: Any) -> MagicMock:
    result = MagicMock()
    result.scalar_one.return_value = value
    return result


def _rows(rows: list[tuple[Any, ...]]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = rows
    return result


def _mapping_rows(rows: list[dict[str, Any]]) -> MagicMock:
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows
    return result


def _factory_with(results: list[Any]) -> tuple[Any, MagicMock]:
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=results)
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=transaction)

    @asynccontextmanager
    async def factory() -> AsyncIterator[MagicMock]:
        yield session

    return factory, session


def _ticket_row(
    *,
    proposal_id: int = 7,
    status: str = "proposed",
    target_type: str = "learning",
    ticket_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": proposal_id,
        "ticket_id": ticket_id or uuid4(),
        "target_type": target_type,
        "target_project": "brain-v42",
        "payload": payload
        or {
            "topic": "Stable contract",
            "insight": "Keep proposal mutations behind one service.",
            "tags": ["architecture"],
        },
        "status": status,
    }


def _roadmap_row(
    *,
    proposal_id: int = 9,
    status: str = "proposed",
    op: str = "archive",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    created_at = datetime.now(UTC)
    return {
        "id": proposal_id,
        "feature_id": uuid4(),
        "op": op,
        "payload": payload or {},
        "rationale": "maintenance",
        "status": status,
        "created_at": created_at,
    }


def _feature_row(
    feature_id: UUID,
    proposal: dict[str, Any],
    *,
    project_key: str = "brain-v42",
    status: str = "building",
    name: str = "Feature",
    merged_into: UUID | None = None,
    pinned: bool = False,
) -> dict[str, Any]:
    return {
        "id": feature_id,
        "project_key": project_key,
        "status": status,
        "name": name,
        "merged_into": merged_into,
        "pinned": pinned,
        "status_updated_at": proposal["created_at"] - timedelta(days=1),
        "updated_at": proposal["created_at"] - timedelta(days=1),
    }


@pytest.mark.asyncio
async def test_apply_ticket_extraction_creates_learning_and_marks_ticket_done() -> None:
    row = _ticket_row()
    entity_id = uuid4()
    learning = AsyncMock()
    learning.create.return_value = MagicMock(id=entity_id)
    factory, session = _factory_with(
        [
            _result_row(row),
            MagicMock(),  # proposal -> applied
            MagicMock(),  # ticket row lock
            _scalar(0),  # no proposal remains for the ticket
            MagicMock(),  # ticket extraction_status -> done
        ]
    )
    service = ProposalService(factory, learning, AsyncMock())

    result = await service.apply_ticket_extraction(row["id"])

    assert result.status == "applied"
    assert result.entity_id == entity_id
    assert result.ticket_id == row["ticket_id"]
    created = learning.create.await_args.args[0]
    assert created.topic == row["payload"]["topic"]
    assert created.project_key == "brain-v42"
    assert created.source == f"ticket:{row['ticket_id']}"
    assert created.source_type == "automated"
    assert created.confidence == "medium"
    assert learning.create.await_args.kwargs["session"] is session
    learning.enrich_created.assert_awaited_once_with(learning.create.return_value, created)
    statements = [call.args[0] for call in session.execute.await_args_list]
    assert dict(statements[1].compile().params)["status"] == "applied"
    assert dict(statements[4].compile().params)["extraction_status"] == "done"


@pytest.mark.asyncio
async def test_apply_ticket_extraction_uses_same_session_for_entity_and_proposal() -> None:
    """The entity insert must roll back with proposal finalization failures."""
    row = _ticket_row()
    learning = AsyncMock()
    learning.create.return_value = MagicMock(id=uuid4())
    factory, session = _factory_with([_result_row(row), MagicMock(), MagicMock(), _scalar(1)])
    service = ProposalService(factory, learning, AsyncMock())

    await service.apply_ticket_extraction(row["id"])

    assert learning.create.await_args.kwargs["session"] is session


@pytest.mark.asyncio
async def test_apply_ticket_extraction_enriches_only_after_commit() -> None:
    row = _ticket_row()
    events: list[str] = []
    learning = AsyncMock()
    learning.create.return_value = MagicMock(id=uuid4())
    factory, session = _factory_with([_result_row(row), MagicMock(), MagicMock(), _scalar(1)])

    async def record_commit(*_args: Any) -> bool:
        events.append("commit")
        return False

    async def record_enrichment(*_args: Any) -> None:
        events.append("enrich")

    session.begin.return_value.__aexit__.side_effect = record_commit
    learning.enrich_created.side_effect = record_enrichment

    await ProposalService(factory, learning, AsyncMock()).apply_ticket_extraction(row["id"])

    assert events == ["commit", "enrich"]


@pytest.mark.asyncio
async def test_post_commit_enrichment_failure_keeps_apply_successful() -> None:
    row = _ticket_row()
    entity_id = uuid4()
    learning = AsyncMock()
    learning.create.return_value = MagicMock(id=entity_id)
    learning.enrich_created.side_effect = RuntimeError("derived work failed")
    factory, _ = _factory_with([_result_row(row), MagicMock(), MagicMock(), _scalar(1)])

    result = await ProposalService(factory, learning, AsyncMock()).apply_ticket_extraction(
        row["id"]
    )

    assert result.status == "applied"
    assert result.entity_id == entity_id


@pytest.mark.asyncio
async def test_apply_ticket_extraction_creates_decision_with_provenance() -> None:
    row = _ticket_row(
        target_type="decision",
        payload={
            "title": "One mutation boundary",
            "description": "Scripts and HTTP share proposal behavior.",
            "reasoning": "Avoid divergent state transitions.",
            "tags": ["ddd"],
        },
    )
    entity_id = uuid4()
    decision = AsyncMock()
    decision.create.return_value = MagicMock(id=entity_id)
    factory, session = _factory_with([_result_row(row), MagicMock(), MagicMock(), _scalar(1)])
    service = ProposalService(factory, AsyncMock(), decision)

    result = await service.apply_ticket_extraction(row["id"])

    assert result.entity_id == entity_id
    created = decision.create.await_args.args[0]
    assert created.project_key == "brain-v42"
    assert created.metadata == {
        "source": f"ticket:{row['ticket_id']}",
        "source_type": "automated",
    }
    assert decision.create.await_args.kwargs["session"] is session
    decision.enrich_created.assert_awaited_once_with(decision.create.return_value, created)


@pytest.mark.asyncio
async def test_ticket_completion_locks_ticket_before_counting_remaining() -> None:
    """Concurrent triage must serialize on the ticket before the final count."""
    row = _ticket_row()
    learning = AsyncMock()
    learning.create.return_value = MagicMock(id=uuid4())
    factory, session = _factory_with([_result_row(row), MagicMock(), MagicMock(), _scalar(1)])
    service = ProposalService(factory, learning, AsyncMock())

    await service.apply_ticket_extraction(row["id"])

    ticket_lock = session.execute.await_args_list[2].args[0]
    assert ticket_lock._for_update_arg is not None


@pytest.mark.asyncio
async def test_apply_ticket_extraction_raises_typed_not_found() -> None:
    factory, _ = _factory_with([_result_row(None)])
    service = ProposalService(factory, AsyncMock(), AsyncMock())

    with pytest.raises(ProposalNotFoundError) as caught:
        await service.apply_ticket_extraction(404)

    assert caught.value.proposal_id == 404
    assert caught.value.family == "ticket-extraction"


@pytest.mark.asyncio
async def test_apply_ticket_extraction_raises_typed_not_proposed() -> None:
    factory, _ = _factory_with([_result_row(_ticket_row(status="rejected"))])
    service = ProposalService(factory, AsyncMock(), AsyncMock())

    with pytest.raises(ProposalNotProposedError) as caught:
        await service.apply_ticket_extraction(7)

    assert caught.value.status == "rejected"


@pytest.mark.asyncio
async def test_apply_ticket_extraction_wraps_entity_failure() -> None:
    row = _ticket_row()
    learning = AsyncMock()
    learning.create.side_effect = RuntimeError("write failed")
    factory, session = _factory_with([_result_row(row)])
    service = ProposalService(factory, learning, AsyncMock())

    with pytest.raises(ProposalApplyError, match="write failed") as caught:
        await service.apply_ticket_extraction(row["id"])

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_reject_ticket_extraction_marks_ticket_done_when_fully_triaged() -> None:
    row = _ticket_row()
    factory, session = _factory_with(
        [_result_row(row), MagicMock(), MagicMock(), _scalar(0), MagicMock()]
    )
    service = ProposalService(factory, AsyncMock(), AsyncMock())

    result = await service.reject_ticket_extraction(row["id"])

    assert result.status == "rejected"
    statements = [call.args[0] for call in session.execute.await_args_list]
    assert dict(statements[1].compile().params)["status"] == "rejected"
    assert dict(statements[4].compile().params)["extraction_status"] == "done"


@pytest.mark.asyncio
async def test_apply_roadmap_archive_keeps_prior_state_in_apply_log() -> None:
    row = _roadmap_row()
    factory, session = _factory_with(
        [
            _result_row(row),
            _mapping_rows(
                [
                    {
                        "id": row["feature_id"],
                        "project_key": "brain-v42",
                        "status": "building",
                        "name": "Feature",
                        "merged_into": None,
                        "pinned": False,
                        "status_updated_at": row["created_at"] - timedelta(days=1),
                        "updated_at": row["created_at"] - timedelta(days=1),
                    }
                ]
            ),
            MagicMock(),
            _result_row({"status": "archived"}),
            MagicMock(),
        ]
    )
    service = ProposalService(factory, AsyncMock(), AsyncMock())

    result = await service.apply_roadmap_curation(row["id"])

    assert result.status == "applied"
    assert result.operation == "archive"
    assert result.apply_log == {"op": "archive", "prior_status": "building"}
    prior_statement = session.execute.await_args_list[1].args[0]
    assert "FOR UPDATE" in str(prior_statement).upper()
    final_statement = session.execute.await_args_list[-1].args[0]
    assert dict(final_statement.compile().params)["apply_log"] == result.apply_log


@pytest.mark.asyncio
async def test_apply_roadmap_merge_records_moved_and_duplicate_artifacts() -> None:
    into = uuid4()
    moved_id, duplicate_id = uuid4(), uuid4()
    row = _roadmap_row(op="merge", payload={"into": str(into)})
    factory, session = _factory_with(
        [
            _result_row(row),
            _mapping_rows(
                [
                    {
                        "id": row["feature_id"],
                        "project_key": "brain-v42",
                        "status": "research",
                        "name": "Loser",
                        "merged_into": None,
                        "pinned": False,
                        "status_updated_at": row["created_at"] - timedelta(days=1),
                        "updated_at": row["created_at"] - timedelta(days=1),
                    },
                    {
                        "id": into,
                        "project_key": "brain-v42",
                        "status": "building",
                        "name": "Winner",
                        "merged_into": None,
                        "pinned": False,
                        "status_updated_at": row["created_at"] - timedelta(days=1),
                        "updated_at": row["created_at"] - timedelta(days=1),
                    },
                ]
            ),
            _rows([("learning", moved_id)]),
            _rows([("decision", duplicate_id)]),
            MagicMock(),
            _result_row({"merged_into": into, "status": "archived"}),
            _scalar(0),
            MagicMock(),
        ]
    )
    service = ProposalService(factory, AsyncMock(), AsyncMock())

    result = await service.apply_roadmap_curation(row["id"])

    assert result.apply_log == {
        "op": "merge",
        "into": str(into),
        "loser_prior_status": "research",
        "loser_prior_name": "Loser",
        "moved_artifacts": [{"artifact_type": "learning", "artifact_id": str(moved_id)}],
        "duplicate_links_deleted": [
            {"artifact_type": "decision", "artifact_id": str(duplicate_id)}
        ],
    }
    lock_statement = str(session.execute.await_args_list[1].args[0]).upper()
    assert "FOR UPDATE" in lock_statement
    assert "ORDER BY ID" in lock_statement


@pytest.mark.asyncio
async def test_apply_roadmap_merge_rejects_self_target_as_conflict() -> None:
    feature_id = uuid4()
    row = _roadmap_row(op="merge", payload={"into": str(feature_id)})
    row["feature_id"] = feature_id
    factory, session = _factory_with([_result_row(row)])
    service = ProposalService(factory, AsyncMock(), AsyncMock())

    with pytest.raises(ProposalStateConflictError, match="itself"):
        await service.apply_roadmap_curation(row["id"])

    assert session.execute.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"into": "not-a-uuid"}])
async def test_apply_roadmap_merge_rejects_invalid_target_payload(
    payload: dict[str, Any],
) -> None:
    row = _roadmap_row(op="merge", payload=payload)
    factory, session = _factory_with([_result_row(row)])
    service = ProposalService(factory, AsyncMock(), AsyncMock())

    with pytest.raises(ProposalStateConflictError, match="target is invalid"):
        await service.apply_roadmap_curation(row["id"])

    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_apply_roadmap_merge_rejects_missing_target() -> None:
    into = uuid4()
    row = _roadmap_row(op="merge", payload={"into": str(into)})
    factory, session = _factory_with(
        [_result_row(row), _mapping_rows([_feature_row(row["feature_id"], row)])]
    )
    service = ProposalService(factory, AsyncMock(), AsyncMock())

    with pytest.raises(ProposalStateConflictError, match="target feature no longer exists"):
        await service.apply_roadmap_curation(row["id"])

    assert session.execute.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("target_state", ["done", "merged"])
async def test_apply_roadmap_merge_rejects_non_live_target(target_state: str) -> None:
    into = uuid4()
    row = _roadmap_row(op="merge", payload={"into": str(into)})
    target = _feature_row(
        into,
        row,
        status="done" if target_state == "done" else "building",
        merged_into=uuid4() if target_state == "merged" else None,
    )
    factory, session = _factory_with(
        [
            _result_row(row),
            _mapping_rows([_feature_row(row["feature_id"], row), target]),
        ]
    )
    service = ProposalService(factory, AsyncMock(), AsyncMock())

    with pytest.raises(ProposalStateConflictError, match="target feature is no longer live"):
        await service.apply_roadmap_curation(row["id"])

    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_apply_roadmap_merge_rejects_pinned_source() -> None:
    into = uuid4()
    row = _roadmap_row(op="merge", payload={"into": str(into)})
    factory, session = _factory_with(
        [
            _result_row(row),
            _mapping_rows(
                [
                    _feature_row(row["feature_id"], row, pinned=True),
                    _feature_row(into, row),
                ]
            ),
        ]
    )
    service = ProposalService(factory, AsyncMock(), AsyncMock())

    with pytest.raises(ProposalStateConflictError, match="source feature is pinned"):
        await service.apply_roadmap_curation(row["id"])

    assert session.execute.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op", "payload"),
    [
        ("archive", {}),
        ("status", {"status": "building"}),
        ("rename", {"name": "new name"}),
    ],
)
async def test_apply_roadmap_non_merge_rejects_stale_source(
    op: str,
    payload: dict[str, Any],
) -> None:
    row = _roadmap_row(op=op, payload=payload)
    factory, session = _factory_with(
        [
            _result_row(row),
            _mapping_rows(
                [
                    {
                        "id": row["feature_id"],
                        "project_key": "brain-v42",
                        "status": "archived",
                        "name": "Stale",
                        "merged_into": uuid4(),
                        "pinned": False,
                        "status_updated_at": row["created_at"] - timedelta(days=1),
                        "updated_at": row["created_at"] - timedelta(days=1),
                    }
                ]
            ),
        ]
    )
    service = ProposalService(factory, AsyncMock(), AsyncMock())

    with pytest.raises(ProposalStateConflictError, match="no longer live"):
        await service.apply_roadmap_curation(row["id"])

    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_apply_roadmap_merge_rejects_cross_project_target() -> None:
    into = uuid4()
    row = _roadmap_row(op="merge", payload={"into": str(into)})
    factory, session = _factory_with(
        [
            _result_row(row),
            _mapping_rows(
                [
                    {
                        "id": row["feature_id"],
                        "project_key": "brain-v42",
                        "status": "research",
                        "name": "Source",
                        "merged_into": None,
                        "pinned": False,
                        "status_updated_at": row["created_at"] - timedelta(days=1),
                        "updated_at": row["created_at"] - timedelta(days=1),
                    },
                    {
                        "id": into,
                        "project_key": "outside",
                        "status": "building",
                        "name": "Target",
                        "merged_into": None,
                        "pinned": False,
                        "status_updated_at": row["created_at"] - timedelta(days=1),
                        "updated_at": row["created_at"] - timedelta(days=1),
                    },
                ]
            ),
        ]
    )
    service = ProposalService(factory, AsyncMock(), AsyncMock())

    with pytest.raises(ProposalStateConflictError, match="same project"):
        await service.apply_roadmap_curation(row["id"])

    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_apply_roadmap_rename_rejects_a_feature_changed_after_review() -> None:
    row = _roadmap_row(op="rename", payload={"name": "stale suggestion"})
    factory, session = _factory_with(
        [
            _result_row(row),
            _mapping_rows(
                [
                    {
                        "id": row["feature_id"],
                        "project_key": "brain-v42",
                        "status": "building",
                        "name": "newer name",
                        "merged_into": None,
                        "pinned": False,
                        "status_updated_at": row["created_at"] - timedelta(days=1),
                        "updated_at": row["created_at"] + timedelta(seconds=1),
                    }
                ]
            ),
        ]
    )
    service = ProposalService(factory, AsyncMock(), AsyncMock())

    with pytest.raises(ProposalStateConflictError, match="changed since review"):
        await service.apply_roadmap_curation(row["id"])

    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_apply_roadmap_rejects_operation_outside_allowed_ops() -> None:
    row = _roadmap_row(op="rename", payload={"name": "new name"})
    factory, session = _factory_with([_result_row(row)])
    service = ProposalService(factory, AsyncMock(), AsyncMock())

    with pytest.raises(ProposalApplyError, match="allowed_ops") as caught:
        await service.apply_roadmap_curation(row["id"], allowed_ops=("archive", "status"))

    assert caught.value.operation == "rename"
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_reject_roadmap_curation_updates_only_proposed_row() -> None:
    row = _roadmap_row()
    factory, session = _factory_with([_result_row(row), MagicMock()])
    service = ProposalService(factory, AsyncMock(), AsyncMock())

    result = await service.reject_roadmap_curation(row["id"])

    assert result.status == "rejected"
    assert result.operation == "archive"
    statement = session.execute.await_args_list[-1].args[0]
    assert dict(statement.compile().params)["status"] == "rejected"


class TestFeatureStateColumnIsALiteral:
    """Invariant portant le `# nosec B608` de `ProposalService._feature_state`.

    La seule interpolation de cette requête est le nom de colonne `field`. Le nosec repose
    sur trois faits vérifiables : l'annotation est un `Literal` fermé, l'unique fragment
    interpolé est bien ce paramètre, et les trois appelants lui passent une constante en
    clair — jamais l'opération lue dans la proposition. Ces tests échouent si quelqu'un
    branche une valeur calculée sur ce paramètre, ce qui doit rouvrir le finding B608.
    """

    ALLOWED_COLUMNS = {"name", "status"}

    @staticmethod
    def _module() -> ast.Module:
        source = Path(proposal_service_module.__file__).read_text(encoding="utf-8")
        return ast.parse(source)

    @classmethod
    def _feature_state_def(cls) -> ast.AsyncFunctionDef:
        return next(
            node
            for node in ast.walk(cls._module())
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_feature_state"
        )

    def test_field_parameter_is_a_closed_literal_annotation(self) -> None:
        definition = self._feature_state_def()
        field_arg = next(arg for arg in definition.args.args if arg.arg == "field")
        annotation = field_arg.annotation

        assert isinstance(annotation, ast.Subscript), "field doit rester annoté par un Literal"
        assert isinstance(annotation.value, ast.Name)
        assert annotation.value.id == "Literal"

        elements = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else []
        assert elements, "le Literal doit énumérer ses colonnes"
        assert {
            element.value for element in elements if isinstance(element, ast.Constant)
        } == self.ALLOWED_COLUMNS

    def test_the_only_interpolated_fragment_is_the_field_parameter(self) -> None:
        definition = self._feature_state_def()
        interpolations = [
            node for node in ast.walk(definition) if isinstance(node, ast.FormattedValue)
        ]

        assert len(interpolations) == 1, "une seconde interpolation invaliderait le nosec B608"
        interpolated = interpolations[0].value
        assert isinstance(interpolated, ast.Name)
        assert interpolated.id == "field"

    def test_every_call_site_passes_a_literal_column_name(self) -> None:
        calls = [
            node
            for node in ast.walk(self._module())
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_feature_state"
        ]

        assert len(calls) == 3, "les appelants de _feature_state ont changé, relire le nosec B608"

        for call in calls:
            passed = [
                keyword.value for keyword in call.keywords if keyword.arg == "field"
            ] or call.args[-1:]
            assert len(passed) == 1
            argument = passed[0]
            assert isinstance(argument, ast.Constant), (
                "le nom de colonne doit rester une constante littérale, "
                f"il vaut {ast.dump(argument)}"
            )
            assert argument.value in self.ALLOWED_COLUMNS
