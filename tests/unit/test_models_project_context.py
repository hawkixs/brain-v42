"""Tests for ProjectContext model — project_group + kebab validator."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from brain_v42.models.project_context import (
    ProjectContext,
    ProjectContextCreate,
    ProjectContextUpdate,
)


class TestProjectKeyValidator:
    """project_key must be kebab-case: lowercase alphanumeric + hyphens + colons."""

    def test_valid_kebab(self):
        pc = ProjectContextCreate(project_key="brain-v42", name="Brain", description="test")
        assert pc.project_key == "brain-v42"

    def test_valid_simple(self):
        pc = ProjectContextCreate(project_key="red", name="Red", description="test")
        assert pc.project_key == "red"

    def test_valid_colon(self):
        pc = ProjectContextCreate(project_key="red-lab:architect", name="Arch", description="test")
        assert pc.project_key == "red-lab:architect"

    def test_confusable_underscore_canonicalized(self):
        # brain_v42 is THE known confusable → auto-canonicalized, not rejected.
        pc = ProjectContextCreate(project_key="brain_v42", name="Brain", description="test")
        assert pc.project_key == "brain-v42"

    def test_generic_underscore_rejected(self):
        # A non-alias underscore key is still rejected loudly ("reject the rest").
        with pytest.raises(ValueError, match="kebab-case"):
            ProjectContextCreate(project_key="my_proj", name="X", description="test")

    def test_invalid_uppercase(self):
        with pytest.raises(ValueError, match="kebab-case"):
            ProjectContextCreate(project_key="BRAIN", name="Brain", description="test")

    def test_invalid_spaces(self):
        with pytest.raises(ValueError, match="kebab-case"):
            ProjectContextCreate(project_key="my project", name="X", description="test")

    def test_invalid_trailing_hyphen(self):
        with pytest.raises(ValueError, match="kebab-case"):
            ProjectContextCreate(project_key="brain-", name="X", description="test")


class TestProjectGroup:
    """project_group is optional."""

    def test_default_none(self):
        pc = ProjectContextCreate(project_key="red", name="Red", description="test")
        assert pc.project_group is None

    def test_set_group(self):
        pc = ProjectContextCreate(
            project_key="red", name="Red", description="test", project_group="red"
        )
        assert pc.project_group == "red"

    def test_update_has_group(self):
        u = ProjectContextUpdate(project_group="watchk")
        assert u.project_group == "watchk"


def test_persisted_project_context_exposes_focus_revision() -> None:
    context = ProjectContext(
        id=uuid4(),
        project_key="brain-v42",
        name="Brain",
        description="test",
        focus_revision=7,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    assert context.focus_revision == 7
