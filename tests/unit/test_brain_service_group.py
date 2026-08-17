"""Tests for project_group support in BrainService."""

import inspect


class TestBrainServiceGroupParam:
    """BrainService.search() accepts project_group."""

    def test_search_has_project_group(self):
        from brain_v42.services.brain_service import BrainService

        sig = inspect.signature(BrainService.search)
        assert "project_group" in sig.parameters

    def test_init_has_project_context_svc(self):
        from brain_v42.services.brain_service import BrainService

        sig = inspect.signature(BrainService.__init__)
        assert "project_context_svc" in sig.parameters

    def test_fan_out_has_project_keys(self):
        from brain_v42.services.brain_service import BrainService

        sig = inspect.signature(BrainService._fan_out)
        assert "project_keys" in sig.parameters


class TestHybridSearcherProjectKeys:
    """HybridSearcher.search() accepts project_keys."""

    def test_search_has_project_keys(self):
        from brain_v42.services.search.hybrid import HybridSearcher

        sig = inspect.signature(HybridSearcher.search)
        assert "project_keys" in sig.parameters
