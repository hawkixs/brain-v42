"""Tests for project_keys list filter on entity repos."""

import importlib
import inspect

import pytest


class TestDecisionRepoProjectKeys:
    """PgDecisionRepo search methods accept project_keys param."""

    def test_search_fts_accepts_project_keys(self):
        """search_fts should accept project_keys parameter."""
        from brain_v42.repositories.pg_decision import PgDecisionRepo

        sig = inspect.signature(PgDecisionRepo.search_fts)
        assert "project_keys" in sig.parameters, "search_fts missing project_keys"

    def test_search_vector_accepts_project_keys(self):
        """search_vector should accept project_keys parameter."""
        from brain_v42.repositories.pg_decision import PgDecisionRepo

        sig = inspect.signature(PgDecisionRepo.search_vector)
        assert "project_keys" in sig.parameters, "search_vector missing project_keys"

    def test_list_all_accepts_project_keys(self):
        """list_all should accept project_keys parameter."""
        from brain_v42.repositories.pg_decision import PgDecisionRepo

        sig = inspect.signature(PgDecisionRepo.list_all)
        assert "project_keys" in sig.parameters, "list_all missing project_keys"


class TestLearningRepoProjectKeys:
    """PgLearningRepo search methods accept project_keys param."""

    def test_search_fts_accepts_project_keys(self):
        from brain_v42.repositories.pg_learning import PgLearningRepo

        sig = inspect.signature(PgLearningRepo.search_fts)
        assert "project_keys" in sig.parameters, "search_fts missing project_keys"

    def test_search_vector_accepts_project_keys(self):
        from brain_v42.repositories.pg_learning import PgLearningRepo

        sig = inspect.signature(PgLearningRepo.search_vector)
        assert "project_keys" in sig.parameters, "search_vector missing project_keys"

    def test_list_all_accepts_project_keys(self):
        from brain_v42.repositories.pg_learning import PgLearningRepo

        sig = inspect.signature(PgLearningRepo.list_all)
        assert "project_keys" in sig.parameters, "list_all missing project_keys"


class TestSnippetRepoProjectKeys:
    """PgSnippetRepo search methods accept project_keys param."""

    def test_search_accepts_project_keys(self):
        """search (FTS wrapper) should accept project_keys parameter."""
        from brain_v42.repositories.pg_snippet import PgSnippetRepo

        sig = inspect.signature(PgSnippetRepo.search)
        assert "project_keys" in sig.parameters, "search missing project_keys"

    def test_vector_search_accepts_project_keys(self):
        """vector_search should accept project_keys parameter."""
        from brain_v42.repositories.pg_snippet import PgSnippetRepo

        sig = inspect.signature(PgSnippetRepo.vector_search)
        assert "project_keys" in sig.parameters, "vector_search missing project_keys"


class TestRunbookRepoProjectKeys:
    """PgRunbookRepo search methods accept project_keys param."""

    def test_search_fts_accepts_project_keys(self):
        from brain_v42.repositories.pg_runbook import PgRunbookRepo

        sig = inspect.signature(PgRunbookRepo.search_fts)
        assert "project_keys" in sig.parameters, "search_fts missing project_keys"

    def test_vector_search_accepts_project_keys(self):
        from brain_v42.repositories.pg_runbook import PgRunbookRepo

        sig = inspect.signature(PgRunbookRepo.vector_search)
        assert "project_keys" in sig.parameters, "vector_search missing project_keys"


class TestADRRepoProjectKeys:
    """PgADRRepo search methods accept project_keys param."""

    def test_search_accepts_project_keys(self):
        """search (FTS) should accept project_keys parameter."""
        from brain_v42.repositories.pg_adr import PgADRRepo

        sig = inspect.signature(PgADRRepo.search)
        assert "project_keys" in sig.parameters, "search missing project_keys"

    def test_vector_search_accepts_project_keys(self):
        from brain_v42.repositories.pg_adr import PgADRRepo

        sig = inspect.signature(PgADRRepo.vector_search)
        assert "project_keys" in sig.parameters, "vector_search missing project_keys"


class TestAllReposProjectKeysParametric:
    """Parametric check: all repos have project_keys on their search methods."""

    @pytest.mark.parametrize(
        "repo_class,module,fts_method,vector_method",
        [
            (
                "PgDecisionRepo",
                "brain_v42.repositories.pg_decision",
                "search_fts",
                "search_vector",
            ),
            (
                "PgLearningRepo",
                "brain_v42.repositories.pg_learning",
                "search_fts",
                "search_vector",
            ),
            (
                "PgSnippetRepo",
                "brain_v42.repositories.pg_snippet",
                "search",
                "vector_search",
            ),
            (
                "PgRunbookRepo",
                "brain_v42.repositories.pg_runbook",
                "search_fts",
                "vector_search",
            ),
            (
                "PgADRRepo",
                "brain_v42.repositories.pg_adr",
                "search",
                "vector_search",
            ),
        ],
    )
    def test_fts_method_has_project_keys(self, repo_class, module, fts_method, vector_method):
        mod = importlib.import_module(module)
        cls = getattr(mod, repo_class)
        method = getattr(cls, fts_method)
        sig = inspect.signature(method)
        assert "project_keys" in sig.parameters, f"{repo_class}.{fts_method} missing project_keys"

    @pytest.mark.parametrize(
        "repo_class,module,fts_method,vector_method",
        [
            (
                "PgDecisionRepo",
                "brain_v42.repositories.pg_decision",
                "search_fts",
                "search_vector",
            ),
            (
                "PgLearningRepo",
                "brain_v42.repositories.pg_learning",
                "search_fts",
                "search_vector",
            ),
            (
                "PgSnippetRepo",
                "brain_v42.repositories.pg_snippet",
                "search",
                "vector_search",
            ),
            (
                "PgRunbookRepo",
                "brain_v42.repositories.pg_runbook",
                "search_fts",
                "vector_search",
            ),
            (
                "PgADRRepo",
                "brain_v42.repositories.pg_adr",
                "search",
                "vector_search",
            ),
        ],
    )
    def test_vector_method_has_project_keys(self, repo_class, module, fts_method, vector_method):
        mod = importlib.import_module(module)
        cls = getattr(mod, repo_class)
        method = getattr(cls, vector_method)
        sig = inspect.signature(method)
        assert "project_keys" in sig.parameters, (
            f"{repo_class}.{vector_method} missing project_keys"
        )

    @pytest.mark.parametrize(
        "repo_class,module",
        [
            ("PgDecisionRepo", "brain_v42.repositories.pg_decision"),
            ("PgLearningRepo", "brain_v42.repositories.pg_learning"),
        ],
    )
    def test_list_all_has_project_keys(self, repo_class, module):
        """Decision and Learning repos list_all should have project_keys."""
        mod = importlib.import_module(module)
        cls = getattr(mod, repo_class)
        sig = inspect.signature(cls.list_all)
        assert "project_keys" in sig.parameters, f"{repo_class}.list_all missing project_keys"


class TestProjectKeysParamIsOptional:
    """project_keys should default to None in all methods."""

    @pytest.mark.parametrize(
        "repo_class,module,method_name",
        [
            ("PgDecisionRepo", "brain_v42.repositories.pg_decision", "search_fts"),
            ("PgDecisionRepo", "brain_v42.repositories.pg_decision", "search_vector"),
            ("PgDecisionRepo", "brain_v42.repositories.pg_decision", "list_all"),
            ("PgLearningRepo", "brain_v42.repositories.pg_learning", "search_fts"),
            ("PgLearningRepo", "brain_v42.repositories.pg_learning", "search_vector"),
            ("PgLearningRepo", "brain_v42.repositories.pg_learning", "list_all"),
            ("PgSnippetRepo", "brain_v42.repositories.pg_snippet", "search"),
            ("PgSnippetRepo", "brain_v42.repositories.pg_snippet", "vector_search"),
            ("PgRunbookRepo", "brain_v42.repositories.pg_runbook", "search_fts"),
            ("PgRunbookRepo", "brain_v42.repositories.pg_runbook", "vector_search"),
            ("PgADRRepo", "brain_v42.repositories.pg_adr", "search"),
            ("PgADRRepo", "brain_v42.repositories.pg_adr", "vector_search"),
        ],
    )
    def test_project_keys_defaults_to_none(self, repo_class, module, method_name):
        mod = importlib.import_module(module)
        cls = getattr(mod, repo_class)
        sig = inspect.signature(getattr(cls, method_name))
        param = sig.parameters["project_keys"]
        assert param.default is None, (
            f"{repo_class}.{method_name} project_keys should default to None, got {param.default}"
        )
