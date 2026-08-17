"""Tests for project_group filtering on ProjectContext repo + service."""

import inspect


class TestPgProjectContextRepoGroup:
    """Repo methods accept project_group param."""

    def test_list_all_has_project_group_param(self):
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        sig = inspect.signature(PgProjectContextRepo.list_all)
        assert "project_group" in sig.parameters

    def test_get_keys_by_group_exists(self):
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        assert hasattr(PgProjectContextRepo, "get_keys_by_group")
        sig = inspect.signature(PgProjectContextRepo.get_keys_by_group)
        assert "project_group" in sig.parameters

    def test_list_groups_exists(self):
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        assert hasattr(PgProjectContextRepo, "list_groups")


class TestProjectContextServiceGroup:
    """Service methods forward group params."""

    def test_list_all_has_project_group_param(self):
        from brain_v42.services.project_context_service import ProjectContextService

        sig = inspect.signature(ProjectContextService.list_all)
        assert "project_group" in sig.parameters

    def test_get_keys_by_group_exists(self):
        from brain_v42.services.project_context_service import ProjectContextService

        assert hasattr(ProjectContextService, "get_keys_by_group")

    def test_list_groups_exists(self):
        from brain_v42.services.project_context_service import ProjectContextService

        assert hasattr(ProjectContextService, "list_groups")
