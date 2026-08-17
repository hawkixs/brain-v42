"""Write-path guard: every knowledge Create/Update model canonicalizes project_key.

This is the bulletproof chokepoint — no matter which tool or service builds the
model, the underscore form (or any non-kebab key) can never be persisted as a
phantom project. See learning 7bc821a1.
"""

import pytest

from brain_v42.models.adr import ADRCreate
from brain_v42.models.decision import DecisionCreate, DecisionUpdate
from brain_v42.models.feature import FeatureCreate
from brain_v42.models.learning import LearningCreate, LearningUpdate
from brain_v42.models.runbook import RunbookCreate
from brain_v42.models.snippet import SnippetCreate, SnippetUpdate


def _decision(**kw):
    return DecisionCreate(title="t", description="d", reasoning="r", **kw)


def _learning(**kw):
    return LearningCreate(topic="t", insight="i", **kw)


def _snippet(**kw):
    return SnippetCreate(title="t", intention="i", code="c", language="py", **kw)


def _runbook(**kw):
    return RunbookCreate(title="t", description="d", trigger="x", **kw)


def _adr(**kw):
    return ADRCreate(title="t", context="c", decision="d", consequences="q", **kw)


def _feature(**kw):
    return FeatureCreate(name="n", description="d", **kw)


class TestWritePathCanonicalizesConfusable:
    def test_decision_create(self):
        assert _decision(project_key="brain_v42").project_key == "brain-v42"

    def test_decision_update(self):
        assert DecisionUpdate(project_key="brain_v42").project_key == "brain-v42"

    def test_learning_create(self):
        assert _learning(project_key="brain_v42").project_key == "brain-v42"

    def test_learning_update(self):
        assert LearningUpdate(project_key="brain_v42").project_key == "brain-v42"

    def test_snippet_create(self):
        assert _snippet(project_key="brain_v42").project_key == "brain-v42"

    def test_snippet_update(self):
        assert SnippetUpdate(project_key="brain_v42").project_key == "brain-v42"

    def test_runbook_create(self):
        assert _runbook(project_key="brain_v42").project_key == "brain-v42"

    def test_adr_create(self):
        assert _adr(project_key="brain_v42").project_key == "brain-v42"

    def test_feature_create(self):
        assert _feature(project_key="brain_v42").project_key == "brain-v42"

    def test_bare_brain_alias(self):
        assert _decision(project_key="brain").project_key == "brain-v42"


class TestWritePathRejectsInvalid:
    def test_generic_underscore_rejected(self):
        with pytest.raises(ValueError, match="kebab-case"):
            _decision(project_key="red_data")

    def test_runbook_uppercase_rejected(self):
        with pytest.raises(ValueError, match="kebab-case"):
            _runbook(project_key="Brain-V42")


class TestWritePathOptionalNone:
    def test_decision_none_allowed(self):
        assert _decision().project_key is None

    def test_learning_none_allowed(self):
        assert _learning().project_key is None
