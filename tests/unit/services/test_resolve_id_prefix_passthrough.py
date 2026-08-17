"""Cross-service tests: resolve_id_prefix passthrough to the repository.

Git-style short id resolution is a pg_base concern; every entity service
exposes a 3-line passthrough so MCP tools (which only see services) can
resolve prefixes. One parametrized suite instead of 6 per-file additions —
the behavior is identical by design.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.services.adr_service import ADRService
from brain_v42.services.decision_service import DecisionService
from brain_v42.services.learning_service import LearningService
from brain_v42.services.runbook_service import RunbookService
from brain_v42.services.snippet_service import SnippetService
from brain_v42.services.ticket_service import TicketService


def _make_repo() -> MagicMock:
    repo = MagicMock()
    repo.resolve_id_prefix = AsyncMock(return_value=[])
    return repo


_SERVICE_FACTORIES: dict[str, Callable[[MagicMock], object]] = {
    "runbook": lambda repo: RunbookService(pg_repo=repo),
    "ticket": lambda repo: TicketService(repo=repo, project_context_repo=MagicMock()),
    "decision": lambda repo: DecisionService(repo=repo, embedding_svc=MagicMock()),
    "learning": lambda repo: LearningService(pg_repo=repo),
    "snippet": lambda repo: SnippetService(repo=repo),
    "adr": lambda repo: ADRService(pg_repo=repo),
}


@pytest.mark.parametrize("service_key", sorted(_SERVICE_FACTORIES))
async def test_resolve_id_prefix_delegates_to_repo(service_key: str) -> None:
    """resolve_id_prefix() forwards the bare-hex prefix and returns repo ids."""
    repo = _make_repo()
    expected = [uuid.uuid4()]
    repo.resolve_id_prefix.return_value = expected
    svc = _SERVICE_FACTORIES[service_key](repo)

    result = await svc.resolve_id_prefix("61b0fa47")  # type: ignore[attr-defined]

    repo.resolve_id_prefix.assert_awaited_once_with("61b0fa47")
    assert result == expected
