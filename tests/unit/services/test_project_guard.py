"""Unit tests for the shared fail-closed project-existence guard.

``require_known_project`` backs the create() guard on LearningService,
DecisionService, SnippetService, RunbookService and ADRService — this file
tests the helper in isolation (repo-agnostic), the per-service wiring is
covered in each service's own test file.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.models.project_context import ProjectContext
from brain_v42.repositories.pg_project_context import PgProjectContextRepo
from brain_v42.services.project_guard import require_known_project
from brain_v42.services.ticket_service import UnknownProjectError

SAMPLE_CONTEXT = ProjectContext(
    project_key="brain-v42", name="brain-v42", description="Second cerveau"
)


def _mock_repo(get_by_key_return: ProjectContext | None) -> MagicMock:
    repo = MagicMock(spec=PgProjectContextRepo)
    repo.get_by_key = AsyncMock(return_value=get_by_key_return)
    return repo


class TestRequireKnownProject:
    async def test_none_project_key_raises(self) -> None:
        repo = _mock_repo(SAMPLE_CONTEXT)

        with pytest.raises(UnknownProjectError):
            await require_known_project(repo, None)

        repo.get_by_key.assert_not_awaited()

    async def test_unknown_project_key_raises_and_names_the_key(self) -> None:
        repo = _mock_repo(None)

        with pytest.raises(UnknownProjectError, match="totally-unknown-project"):
            await require_known_project(repo, "totally-unknown-project")

        repo.get_by_key.assert_awaited_once_with("totally-unknown-project", session=None)

    async def test_known_project_key_passes(self) -> None:
        repo = _mock_repo(SAMPLE_CONTEXT)

        await require_known_project(repo, "brain-v42")

        repo.get_by_key.assert_awaited_once_with("brain-v42", session=None)

    async def test_no_repo_wired_disables_the_guard(self) -> None:
        """A service constructed without project_context_repo skips the check."""
        await require_known_project(None, None)
        await require_known_project(None, "whatever-unknown")

    async def test_reuses_caller_provided_session_instead_of_opening_a_new_one(self) -> None:
        repo = _mock_repo(SAMPLE_CONTEXT)
        caller_session = MagicMock(spec=AsyncSession)

        await require_known_project(repo, "brain-v42", session=caller_session)

        repo.get_by_key.assert_awaited_once_with("brain-v42", session=caller_session)
