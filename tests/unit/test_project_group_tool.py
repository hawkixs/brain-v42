"""Tests for brain_list_project_groups tool."""

import inspect


class TestBrainListProjectGroupsTool:
    def test_tool_exists_in_module(self):
        """The tool registration function should include brain_list_project_groups."""
        from brain_v42.mcp.tools import project_context_tools

        source = inspect.getsource(project_context_tools)
        assert "brain_list_project_groups" in source

    def test_list_groups_service_method(self):
        """Service should have list_groups method."""
        from brain_v42.services.project_context_service import ProjectContextService

        assert hasattr(ProjectContextService, "list_groups")
