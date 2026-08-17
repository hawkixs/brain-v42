"""Unit tests for brain_v42 Pydantic models."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from brain_v42.models import (
    ADR,
    ADRCreate,
    ADRUpdate,
    AlternativeConsidered,
    Decision,
    DecisionCreate,
    DecisionUpdate,
    Learning,
    LearningCreate,
    LearningUpdate,
    PaginatedResponse,
    ProjectContext,
    ProjectContextCreate,
    ProjectContextUpdate,
    Runbook,
    RunbookCreate,
    RunbookStep,
    RunbookUpdate,
    Snippet,
    SnippetCreate,
    SnippetUpdate,
    TimestampMixin,
)

# ============================================================
# TimestampMixin tests
# ============================================================


class TestTimestampMixin:
    def test_created_at_is_timezone_aware(self) -> None:
        """TimestampMixin.created_at must have tzinfo set to UTC."""
        obj = TimestampMixin()
        assert obj.created_at.tzinfo is not None

    def test_updated_at_is_timezone_aware(self) -> None:
        """TimestampMixin.updated_at must have tzinfo set to UTC."""
        obj = TimestampMixin()
        assert obj.updated_at.tzinfo is not None

    def test_defaults_use_utc(self) -> None:
        """TimestampMixin defaults must be UTC-aware datetimes."""
        obj = TimestampMixin()
        assert obj.created_at.tzinfo == UTC
        assert obj.updated_at.tzinfo == UTC

    def test_no_utcnow_usage(self) -> None:
        """Verify that datetime.utcnow() is NOT used in models source code."""
        import os

        from brain_v42 import models as models_pkg

        models_dir = os.path.dirname(models_pkg.__file__)
        for fname in os.listdir(models_dir):
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(models_dir, fname)
            with open(fpath) as f:
                content = f.read()
            assert "datetime.utcnow()" not in content, f"{fname} uses deprecated datetime.utcnow()"


# ============================================================
# PaginatedResponse tests
# ============================================================


class TestPaginatedResponse:
    def test_basic_instantiation(self) -> None:
        """PaginatedResponse can be instantiated with required fields."""
        resp: PaginatedResponse[str] = PaginatedResponse(
            items=["a", "b"], total=10, limit=2, offset=0
        )
        assert resp.items == ["a", "b"]
        assert resp.total == 10
        assert resp.limit == 2
        assert resp.offset == 0

    def test_has_more_true(self) -> None:
        """has_more is True when offset + limit < total."""
        resp: PaginatedResponse[str] = PaginatedResponse(items=["a"], total=10, limit=2, offset=0)
        assert resp.has_more is True

    def test_has_more_false(self) -> None:
        """has_more is False when offset + limit >= total."""
        resp: PaginatedResponse[str] = PaginatedResponse(items=["a"], total=3, limit=2, offset=2)
        assert resp.has_more is False

    def test_has_more_exactly_at_end(self) -> None:
        """has_more is False when offset + limit == total."""
        resp: PaginatedResponse[str] = PaginatedResponse(
            items=["a", "b"], total=4, limit=2, offset=2
        )
        assert resp.has_more is False

    def test_empty_items(self) -> None:
        """PaginatedResponse can hold an empty list."""
        resp: PaginatedResponse[str] = PaginatedResponse(items=[], total=0, limit=10, offset=0)
        assert resp.items == []
        assert resp.has_more is False


# ============================================================
# Decision model tests
# ============================================================


class TestDecisionModels:
    def test_decision_create_minimal(self) -> None:
        """DecisionCreate works with required fields only."""
        dc = DecisionCreate(
            title="Use PostgreSQL",
            description="Choose PostgreSQL as primary DB",
            reasoning="Mature, reliable, supports pgvector",
        )
        assert dc.title == "Use PostgreSQL"
        assert dc.status == "active"  # default
        assert dc.alternatives == []
        assert dc.tags == []

    def test_decision_create_status_valid(self) -> None:
        """Decision status accepts valid literal values."""
        for status in ("active", "superseded", "deprecated"):
            dc = DecisionCreate(
                title="Test",
                description="Desc",
                reasoning="Reason",
                status=status,  # type: ignore[arg-type]
            )
            assert dc.status == status

    def test_decision_create_invalid_status(self) -> None:
        """DecisionCreate raises ValidationError for invalid status."""
        with pytest.raises(ValidationError):
            DecisionCreate(
                title="Test",
                description="Desc",
                reasoning="Reason",
                status="invalid_status",  # type: ignore[arg-type]
            )

    def test_decision_full_model_has_id_and_timestamps(self) -> None:
        """Full Decision model auto-generates UUID id and timestamps."""
        d = Decision(
            title="Use pgvector",
            description="Semantic search",
            reasoning="Built into PG",
        )
        assert isinstance(d.id, UUID)
        assert d.created_at.tzinfo is not None
        assert d.superseded_by is None
        assert d.embedding is None

    def test_decision_superseded_by_is_uuid_or_none(self) -> None:
        """Decision.superseded_by accepts UUID value."""
        from uuid import uuid4

        uid = uuid4()
        d = Decision(
            title="Old decision",
            description="Desc",
            reasoning="Reason",
            superseded_by=uid,
        )
        assert d.superseded_by == uid

    def test_decision_from_attributes_config(self) -> None:
        """Decision model_config includes from_attributes=True."""
        assert Decision.model_config.get("from_attributes") is True

    def test_decision_update_all_optional(self) -> None:
        """DecisionUpdate can be instantiated with no fields (all None)."""
        du = DecisionUpdate()
        assert du.title is None
        assert du.status is None

    def test_decision_update_partial(self) -> None:
        """DecisionUpdate only sets provided fields."""
        du = DecisionUpdate(title="New Title")
        assert du.title == "New Title"
        assert du.description is None


# ============================================================
# Learning model tests
# ============================================================


class TestLearningModels:
    def test_learning_create_minimal(self) -> None:
        """LearningCreate works with required fields."""
        lc = LearningCreate(
            topic="Async SQLAlchemy",
            insight="Use AsyncSession for non-blocking DB calls",
        )
        assert lc.topic == "Async SQLAlchemy"
        assert lc.confidence == "medium"
        assert lc.source_type == "experience"

    def test_learning_create_invalid_confidence(self) -> None:
        """LearningCreate raises ValidationError for invalid confidence."""
        with pytest.raises(ValidationError):
            LearningCreate(
                topic="Test",
                insight="Test insight",
                confidence="very_high",  # type: ignore[arg-type]
            )

    def test_learning_create_invalid_source_type(self) -> None:
        """LearningCreate raises ValidationError for invalid source_type."""
        with pytest.raises(ValidationError):
            LearningCreate(
                topic="Test",
                insight="Test insight",
                source_type="blog_post",  # type: ignore[arg-type]
            )

    def test_learning_full_model(self) -> None:
        """Full Learning model has UUID id and optional fields."""
        learning = Learning(
            topic="ONNX Runtime",
            insight="Faster inference than PyTorch",
        )
        assert isinstance(learning.id, UUID)
        assert learning.validated_at is None
        assert learning.embedding is None

    def test_learning_from_attributes_config(self) -> None:
        """Learning model_config includes from_attributes=True."""
        assert Learning.model_config.get("from_attributes") is True

    def test_learning_update_all_optional(self) -> None:
        """LearningUpdate can be instantiated with no fields."""
        lu = LearningUpdate()
        assert lu.topic is None
        assert lu.confidence is None


# ============================================================
# Snippet model tests
# ============================================================


class TestSnippetModels:
    def test_snippet_create_minimal(self) -> None:
        """SnippetCreate works with required fields."""
        sc = SnippetCreate(
            title="Async SQLAlchemy session",
            intention="Create an async session factory",
            code="async_session = sessionmaker(...)",
            language="python",
        )
        assert sc.title == "Async SQLAlchemy session"
        assert sc.language == "python"
        assert sc.dependencies == []
        assert sc.tags == []

    def test_snippet_full_model(self) -> None:
        """Full Snippet model has UUID, use_count default."""
        s = Snippet(
            title="ONNX inference",
            intention="Run ONNX model inference",
            code="session.run(...)",
            language="python",
        )
        assert isinstance(s.id, UUID)
        assert s.use_count == 0
        assert s.last_used_at is None
        assert s.embedding is None

    def test_snippet_from_attributes_config(self) -> None:
        """Snippet model_config includes from_attributes=True."""
        assert Snippet.model_config.get("from_attributes") is True

    def test_snippet_update_all_optional(self) -> None:
        """SnippetUpdate can be instantiated with no fields."""
        su = SnippetUpdate()
        assert su.title is None
        assert su.code is None


# ============================================================
# Runbook model tests
# ============================================================


class TestRunbookModels:
    def test_runbook_step_basic(self) -> None:
        """RunbookStep can be created with required fields."""
        step = RunbookStep(order=1, title="Start service")
        assert step.order == 1
        assert step.title == "Start service"
        assert step.command is None
        assert step.timeout_seconds is None

    def test_runbook_step_with_all_fields(self) -> None:
        """RunbookStep can be created with all optional fields."""
        step = RunbookStep(
            order=2,
            title="Check health",
            command="curl http://localhost/health",
            description="Verify service is healthy",
            expected_output="200 OK",
            timeout_seconds=30,
        )
        assert step.command == "curl http://localhost/health"
        assert step.timeout_seconds == 30

    def test_runbook_create_minimal(self) -> None:
        """RunbookCreate works with required fields and empty steps."""
        rc = RunbookCreate(
            title="Deploy service",
            description="Steps to deploy the service",
            project_key="brain_v42",
            trigger="manual deployment",
        )
        assert rc.title == "Deploy service"
        assert rc.steps == []
        assert rc.rollback_steps == []

    def test_runbook_create_with_steps(self) -> None:
        """RunbookCreate can include RunbookStep objects."""
        rc = RunbookCreate(
            title="Test runbook",
            description="Desc",
            project_key="brain_v42",
            trigger="test",
            steps=[RunbookStep(order=1, title="Step 1")],
            rollback_steps=[RunbookStep(order=1, title="Rollback 1")],
        )
        assert len(rc.steps) == 1
        assert len(rc.rollback_steps) == 1

    def test_runbook_full_model(self) -> None:
        """Full Runbook model has UUID, execution_count, etc."""
        r = Runbook(
            title="Deploy",
            description="Deploy script",
            project_key="brain_v42",
            trigger="tag push",
        )
        assert isinstance(r.id, UUID)
        assert r.execution_count == 0
        assert r.last_executed_at is None
        assert r.last_execution_status is None

    def test_runbook_invalid_execution_status(self) -> None:
        """Runbook raises ValidationError for invalid execution status."""
        with pytest.raises(ValidationError):
            Runbook(
                title="Test",
                description="Desc",
                project_key="brain_v42",
                trigger="test",
                last_execution_status="unknown_status",  # type: ignore[arg-type]
            )

    def test_runbook_from_attributes_config(self) -> None:
        """Runbook model_config includes from_attributes=True."""
        assert Runbook.model_config.get("from_attributes") is True

    def test_runbook_update_all_optional(self) -> None:
        """RunbookUpdate can be instantiated with no fields."""
        ru = RunbookUpdate()
        assert ru.title is None
        assert ru.steps is None


# ============================================================
# ADR model tests
# ============================================================


class TestADRModels:
    def test_alternative_considered_basic(self) -> None:
        """AlternativeConsidered works with title and description."""
        alt = AlternativeConsidered(
            title="Use Redis",
            description="Redis as an alternative",
        )
        assert alt.title == "Use Redis"
        assert alt.reason_rejected is None

    def test_alternative_considered_with_rejection(self) -> None:
        """AlternativeConsidered accepts reason_rejected field."""
        alt = AlternativeConsidered(
            title="Use Redis",
            description="Redis as an alternative",
            reason_rejected="Too heavy for our use case",
        )
        assert alt.reason_rejected == "Too heavy for our use case"

    def test_adr_create_minimal(self) -> None:
        """ADRCreate works with required fields."""
        ac = ADRCreate(
            title="Use PostgreSQL",
            context="Need a reliable database",
            decision="Use PostgreSQL with pgvector extension",
            consequences="Requires pgvector setup",
            project_key="brain_v42",
        )
        assert ac.title == "Use PostgreSQL"
        assert ac.status == "proposed"
        assert ac.alternatives_considered == []

    def test_adr_create_invalid_status(self) -> None:
        """ADRCreate raises ValidationError for invalid status."""
        with pytest.raises(ValidationError):
            ADRCreate(
                title="Test",
                context="Context",
                decision="Decision",
                consequences="Cons",
                project_key="brain_v42",
                status="unknown_status",  # type: ignore[arg-type]
            )

    def test_adr_full_model_has_number(self) -> None:
        """Full ADR model has number field (int) and superseded_by (int|None)."""
        a = ADR(
            title="Use PostgreSQL",
            context="Need DB",
            decision="Use PG",
            consequences="Setup needed",
            project_key="brain_v42",
        )
        assert isinstance(a.id, UUID)
        assert isinstance(a.number, int)
        assert a.superseded_by is None  # NOT UUID, is int|None

    def test_adr_superseded_by_is_int(self) -> None:
        """ADR.superseded_by is an int (ADR number), not a UUID."""
        a = ADR(
            title="Old ADR",
            context="Context",
            decision="Decision",
            consequences="Cons",
            project_key="brain_v42",
            superseded_by=5,
        )
        assert a.superseded_by == 5
        assert isinstance(a.superseded_by, int)

    def test_adr_from_attributes_config(self) -> None:
        """ADR model_config includes from_attributes=True."""
        assert ADR.model_config.get("from_attributes") is True

    def test_adr_update_all_optional(self) -> None:
        """ADRUpdate can be instantiated with no fields."""
        au = ADRUpdate()
        assert au.title is None
        assert au.status is None
        assert au.superseded_by is None

    def test_adr_decided_at_is_datetime_or_none(self) -> None:
        """ADR.decided_at accepts datetime or None."""
        now = datetime.now(UTC)
        a = ADR(
            title="Test ADR",
            context="Context",
            decision="Decision",
            consequences="Cons",
            project_key="brain_v42",
            decided_at=now,
        )
        assert a.decided_at == now


# ============================================================
# ProjectContext model tests
# ============================================================


class TestProjectContextModels:
    def test_project_context_create_minimal(self) -> None:
        """ProjectContextCreate works with required fields."""
        pc = ProjectContextCreate(
            project_key="brain-v42",
            name="Brain v42",
            description="MCP-based memory system",
        )
        assert pc.project_key == "brain-v42"
        assert pc.languages == []
        assert pc.frameworks == []
        assert pc.blockers == []

    def test_project_context_full_model(self) -> None:
        """Full ProjectContext model has UUID and count fields."""
        pc = ProjectContext(
            project_key="brain_v42",
            name="Brain v42",
            description="MCP-based memory system",
        )
        assert isinstance(pc.id, UUID)
        assert pc.decisions_count == 0
        assert pc.learnings_count == 0
        assert pc.snippets_count == 0
        assert pc.runbooks_count == 0
        assert pc.adrs_count == 0

    def test_project_context_no_embedding_field(self) -> None:
        """ProjectContext does NOT have an embedding field."""
        pc = ProjectContext(
            project_key="brain_v42",
            name="Brain v42",
            description="No embedding needed",
        )
        assert not hasattr(pc, "embedding")

    def test_project_context_from_attributes_config(self) -> None:
        """ProjectContext model_config includes from_attributes=True."""
        assert ProjectContext.model_config.get("from_attributes") is True

    def test_project_context_update_all_optional(self) -> None:
        """ProjectContextUpdate can be instantiated with no fields."""
        pu = ProjectContextUpdate()
        assert pu.name is None
        assert pu.description is None
        assert pu.current_focus is None

    def test_project_context_with_all_fields(self) -> None:
        """ProjectContextCreate accepts all optional fields."""
        pc = ProjectContextCreate(
            project_key="brain-v42",
            name="Brain v42",
            description="Full example",
            languages=["python"],
            frameworks=["fastmcp", "sqlalchemy"],
            databases=["postgresql"],
            code_style="black + ruff",
            git_workflow="feature branches",
            test_strategy="TDD",
            current_phase="M1",
            current_focus="Pydantic models",
            blockers=["missing PG connection"],
            related_projects=["datalake_v2"],
            local_path="/home/hawixs/projects/brain_v42",
            repo_url="https://gitlab.example.com/brain_v42",
        )
        assert pc.languages == ["python"]
        assert pc.databases == ["postgresql"]
        assert pc.current_focus == "Pydantic models"


# ============================================================
# __init__.py barrel imports test
# ============================================================


class TestBarrelImports:
    def test_all_classes_importable_from_models(self) -> None:
        """All public classes are accessible from brain_v42.models."""
        from brain_v42 import models

        expected_classes = [
            "TimestampMixin",
            "PaginatedResponse",
            "Decision",
            "DecisionCreate",
            "DecisionUpdate",
            "Learning",
            "LearningCreate",
            "LearningUpdate",
            "Snippet",
            "SnippetCreate",
            "SnippetUpdate",
            "Runbook",
            "RunbookCreate",
            "RunbookUpdate",
            "RunbookStep",
            "ADR",
            "ADRCreate",
            "ADRUpdate",
            "AlternativeConsidered",
            "ProjectContext",
            "ProjectContextCreate",
            "ProjectContextUpdate",
            "ExtractionStatus",
            "Ticket",
            "TicketCreate",
            "TicketGroups",
            "TicketKind",
            "TicketMessage",
            "TicketStatus",
        ]
        for cls_name in expected_classes:
            assert hasattr(models, cls_name), f"Missing export: {cls_name}"

    def test_all_classes_in_dunder_all(self) -> None:
        """All public classes are listed in brain_v42.models.__all__."""
        from brain_v42 import models

        assert hasattr(models, "__all__")
        for cls_name in [
            "TimestampMixin",
            "PaginatedResponse",
            "Decision",
            "Learning",
            "Snippet",
            "Runbook",
            "ADR",
            "ProjectContext",
            "ExtractionStatus",
            "Ticket",
            "TicketCreate",
            "TicketGroups",
            "TicketKind",
            "TicketMessage",
            "TicketStatus",
        ]:
            assert cls_name in models.__all__, f"{cls_name} not in __all__"
