"""apply/reject of curation proposals, finally on the brain-v42 side — and asymmetric.

Ticket 2547b4a2: the only action surface lived at the end of the codex_gateway
router, scoped `project_group='red'` — a PERIMETER, not a failure (port 9210 is
listening). brain-v42 had reading (`brain_list_curation_proposals`) and no action:
168 `proposed` at the 2026-08-29 survey, last apply on 2026-07-14.

THE ASYMMETRY IS THE CONTRACT, not an API detail. The proposals read are title
DEGRADATIONS — quoted verbatim from the corpus, hence still in French: "Disaster
recovery vérifiable — PostgreSQL + Neo4j + off-site" becoming "Infrastructure
PostgreSQL sécurisée". A surface that made applying easy would be a trap. Hence:

* `brain_reject_curation_proposals` — COMFORTABLE: a batch of ids (≤ 50), verdicts
  per id, no gesture per proposal.
* `brain_apply_curation_proposal` — COSTLY: ONE proposal per call, by construction
  of the signature (an integer, not a list); the caller re-reads what they apply.

Scoping goes through the JOIN on `features` (the table carries no project key): an
id foreign to the project is refused BY NAME and never reaches the service.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastmcp.exceptions import ToolError

from brain_v42.mcp.tools import dream_tools as dream_tools_module
from brain_v42.mcp.tools.dream_tools import register_dream_tools


class MockMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **_kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = fn
            return fn

        return decorator


def _ownership_row(proposal_id: int, project_key: str, status: str = "proposed") -> dict[str, Any]:
    return {
        "id": proposal_id,
        "status": status,
        "project_key": project_key,
        "op": "rename",
        "feature_id": uuid4(),
        "feature_name": f"feature du proposal {proposal_id}",
    }


class _FakeService:
    """Records the requested mutations, without a database."""

    instances: list[_FakeService] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.applied: list[tuple[int, Any]] = []
        self.rejected: list[int] = []
        _FakeService.instances.append(self)

    async def apply_roadmap_curation(self, proposal_id: int, allowed_ops: Any = None, **_: Any):
        self.applied.append((proposal_id, allowed_ops))
        result = MagicMock()
        result.operation = "rename"
        result.apply_log = f"applied {proposal_id}"
        return result

    async def reject_roadmap_curation(self, proposal_id: int, **_: Any):
        self.rejected.append(proposal_id)
        result = MagicMock()
        result.operation = "rename"
        return result


def _tools(ownership_rows: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    class _Result:
        def mappings(self) -> Any:
            return self

        def all(self) -> list[Any]:
            rows = []
            for data in ownership_rows:
                row = MagicMock()
                row._mapping = data
                rows.append(data)
            return rows

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, statement: Any, params: Any = None) -> _Result:
            return _Result()

    _FakeService.instances.clear()
    monkeypatch.setattr(dream_tools_module, "_proposal_service_factory", _FakeService)
    mcp = MockMCP()
    register_dream_tools(
        mcp,
        session_factory=MagicMock(return_value=_Session()),
        auto_linker=None,
        graph_service=None,
    )
    return mcp.registered


def _service() -> _FakeService:
    assert len(_FakeService.instances) >= 1
    return _FakeService.instances[-1]


@pytest.mark.asyncio
async def test_reject_is_a_comfortable_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _tools(
        [_ownership_row(701, "brain-v42"), _ownership_row(702, "brain-v42")], monkeypatch
    )

    out = await tools["brain_reject_curation_proposals"](
        project_key="brain-v42", proposal_ids=[701, 702]
    )

    assert _service().rejected == [701, 702]
    assert "2 rejetée(s)" in out
    assert "#701" in out and "#702" in out


@pytest.mark.asyncio
async def test_a_foreign_proposal_is_refused_by_name_and_never_reaches_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The join-based scope is the guard: an id from ANOTHER project does not mutate."""
    tools = _tools([_ownership_row(701, "brain-v42"), _ownership_row(999, "red-lab")], monkeypatch)

    out = await tools["brain_reject_curation_proposals"](
        project_key="brain-v42", proposal_ids=[701, 999]
    )

    assert _service().rejected == [701]
    assert "#999" in out and "red-lab" in out


@pytest.mark.asyncio
async def test_an_unknown_proposal_is_named_not_silently_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _tools([_ownership_row(701, "brain-v42")], monkeypatch)

    out = await tools["brain_reject_curation_proposals"](
        project_key="brain-v42", proposal_ids=[701, 12345]
    )

    assert _service().rejected == [701]
    assert "#12345" in out and "introuvable" in out


@pytest.mark.asyncio
async def test_an_already_decided_proposal_is_skipped_with_its_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _tools([_ownership_row(701, "brain-v42", status="applied")], monkeypatch)

    out = await tools["brain_reject_curation_proposals"](
        project_key="brain-v42", proposal_ids=[701]
    )

    assert _service().rejected == []
    assert "applied" in out


@pytest.mark.asyncio
async def test_the_reject_batch_is_capped_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _tools([], monkeypatch)

    with pytest.raises(ToolError, match="50"):
        await tools["brain_reject_curation_proposals"](
            project_key="brain-v42", proposal_ids=list(range(1, 52))
        )

    assert not _FakeService.instances or _service().rejected == []


@pytest.mark.asyncio
async def test_apply_is_deliberately_singular(monkeypatch: pytest.MonkeyPatch) -> None:
    """ONE proposal per call — the cost is in the signature, not in a guideline:
    the proposals read are title degradations."""
    tools = _tools([_ownership_row(553, "brain-v42")], monkeypatch)

    out = await tools["brain_apply_curation_proposal"](project_key="brain-v42", proposal_id=553)

    assert _service().applied == [(553, None)]
    assert "#553" in out and "applied 553" in out

    import inspect

    signature = inspect.signature(tools["brain_apply_curation_proposal"])
    annotation = signature.parameters["proposal_id"].annotation
    assert annotation in (int, "int"), (
        "apply prend UN entier — une liste rendrait l'application confortable, "
        "l'inverse exact du contrat"
    )


@pytest.mark.asyncio
async def test_apply_refuses_a_foreign_proposal(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _tools([_ownership_row(553, "red-lab")], monkeypatch)

    with pytest.raises(ToolError, match="red-lab"):
        await tools["brain_apply_curation_proposal"](project_key="brain-v42", proposal_id=553)

    assert not _FakeService.instances or _service().applied == []


@pytest.mark.asyncio
async def test_both_tools_require_a_project_key(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _tools([], monkeypatch)

    with pytest.raises(ToolError, match="project_key"):
        await tools["brain_reject_curation_proposals"](project_key="", proposal_ids=[1])
    with pytest.raises(ToolError, match="project_key"):
        await tools["brain_apply_curation_proposal"](project_key="", proposal_id=1)
