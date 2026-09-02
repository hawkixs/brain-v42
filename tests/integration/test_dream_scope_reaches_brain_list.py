"""The middleware's injection must REACH brain_list's SQL, end to end.

Both halves are already proved, each on its own side:

- `tests/unit/mcp/test_dream_project_authorization.py` proves the middleware
  INJECTS `project_key` into `brain_list`'s arguments and refuses a divergent
  `project_key`;
- `tests/integration/test_project_scoped_crud.py` proves the repositories honour a
  `project_key` they are explicitly PASSED.

Neither proves the junction. A refactor of `brain_list` that stopped honouring the
injected argument — a `project_key` forgotten in the call to the service, a
`canonicalize_project_key` that overwrote it — would leave BOTH suites green, and
the REORG night would re-paginate the whole corpus in silence. This is the setup's
most exposed point: `brain_list` is the ONLY CRUD tool that never calls
`get_dream_project_scope()` itself, so its bound lives entirely in the middleware
and has no downstream redundancy.

The negative witness is inside the test, not next to it: the same page requested
WITHOUT the authorisation must return both projects' corpus. Without it, a
`brain_list` that never returned anything would pass for perfectly bounded.

Against `brain_test`, never `brain` — the keys carry the `integ-` prefix the
end-of-session purge recognises.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.mcp.dream_project_authorization import (
    DreamProjectAudit,
    authorize_dream_project_request,
)
from brain_v42.models.learning import LearningCreate
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.services.learning_service import LearningService

pytestmark = pytest.mark.integration


class MockMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **_kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = fn
            return fn

        return decorator


class _UnusedResolver:
    """`brain_list` has no reference to resolve — calling it would be a defect."""

    async def references_belong_to_project(self, *_args: Any) -> bool:
        raise AssertionError("brain_list carries no reference to resolve")


def _other_services() -> dict[str, Any]:
    """The four services `brain_list` does not touch on the learning path."""
    services: dict[str, Any] = {}
    for name in ("decision_svc", "snippet_svc", "runbook_svc", "adr_svc"):
        svc = MagicMock()
        svc.list_all = AsyncMock(return_value=[])
        svc.list_snippets = AsyncMock(return_value=[])
        svc.list_by_project = AsyncMock(return_value=[])
        services[name] = svc
    return services


@pytest.fixture
def brain_list(session_factory: async_sessionmaker[AsyncSession]) -> Any:
    """The real `brain_list`, wired to a real LearningService and the real database."""
    from brain_v42.mcp.tools.crud_tools import register_crud_tools

    mcp = MockMCP()
    register_crud_tools(
        mcp,
        learning_svc=LearningService(PgLearningRepo(session_factory=session_factory)),
        session_factory=session_factory,
        **_other_services(),
    )
    return mcp.registered["brain_list"]


@pytest_asyncio.fixture
async def two_projects(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[str, str, str, str]:
    """One learning in the run's project, one in a neighbouring project."""
    service = LearningService(PgLearningRepo(session_factory=session_factory))
    suffix = uuid.uuid4().hex[:10]
    mine_key = f"integ-scope-mine-{suffix}"
    theirs_key = f"integ-scope-theirs-{suffix}"
    mine_topic = f"scope-mine-{suffix}"
    theirs_topic = f"scope-theirs-{suffix}"

    await service.create(
        LearningCreate(
            topic=mine_topic,
            insight="corpus of the run project",
            project_key=mine_key,
            source_type="experience",
            confidence="high",
        )
    )
    await service.create(
        LearningCreate(
            topic=theirs_topic,
            insight="corpus of a neighbouring project",
            project_key=theirs_key,
            source_type="experience",
            confidence="high",
        )
    )
    return mine_key, mine_topic, theirs_key, theirs_topic


async def _authorized_arguments(project_key: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await authorize_dream_project_request(
        tool_name="brain_list",
        arguments=arguments,
        project_key=project_key,
        resolver=_UnusedResolver(),
        audit=DreamProjectAudit(principal="dream-codex-reorg", phase="reorg"),
    )
    return dict(result.arguments)


@pytest.mark.asyncio
async def test_an_injected_perimeter_actually_bounds_the_page(
    brain_list: Any, two_projects: tuple[str, str, str, str]
) -> None:
    """THE junction test: the middleware injects, and the SQL takes it into account.

    The page requested is REORG Part 1's — `summary_only`, with no `project_key`,
    because the phase never supplies one itself.
    """
    mine_key, mine_topic, _, theirs_topic = two_projects
    page = {"entity_type": "learning", "limit": 100, "offset": 0, "summary_only": True}

    arguments = await _authorized_arguments(mine_key, page)
    assert arguments["project_key"] == mine_key, (
        "le middleware n'a rien injecté — le reste du test ne mesurerait plus rien"
    )

    output = await brain_list(**arguments)

    assert mine_topic in output, "le corpus du projet du run doit rester listable"
    assert theirs_topic not in output, (
        "un learning d'un autre projet est sorti d'une page BORNÉE par le "
        "middleware : l'injection n'atteint pas le SQL, et REORG repaginerait le "
        "corpus entier en silence"
    )


@pytest.mark.asyncio
async def test_the_same_page_unbounded_returns_both_projects(
    brain_list: Any, two_projects: tuple[str, str, str, str]
) -> None:
    """The negative witness, without which the test above proves nothing.

    A broken `brain_list` that never returned anything would satisfy the assertion
    "the neighbour does not appear". This test requires the neighbour to BE visible
    when nothing bounds the page — that is what makes its absence significant.
    """
    _, mine_topic, _, theirs_topic = two_projects

    output = await brain_list(entity_type="learning", limit=100, offset=0, summary_only=True)

    assert mine_topic in output
    assert theirs_topic in output, (
        "sans périmètre, les deux projets doivent apparaître; sinon l'isolement "
        "mesuré plus haut viendrait d'autre chose que du périmètre"
    )


@pytest.mark.asyncio
async def test_a_forged_perimeter_never_reaches_the_page(
    brain_list: Any, two_projects: tuple[str, str, str, str]
) -> None:
    """The refusal is UPSTREAM of the SQL: the call never happens.

    Completes the junction from the other end — the injection bounds, and an attempt
    to contradict it does not reach the database.
    """
    from fastmcp.exceptions import AuthorizationError

    mine_key, _, theirs_key, _ = two_projects
    page = {"entity_type": "learning", "limit": 100, "project_key": theirs_key}

    with pytest.raises(AuthorizationError):
        await _authorized_arguments(mine_key, page)
