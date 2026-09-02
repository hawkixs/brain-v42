"""Tests for LLM-first markdown formatters."""

from datetime import UTC, datetime
from uuid import UUID

from brain_v42.models.adr import ADR
from brain_v42.models.brain import KnowledgeByType, SearchResult
from brain_v42.models.decision import Decision
from brain_v42.models.indexed_plan_chunk import IndexedPlanChunk
from brain_v42.models.learning import Learning
from brain_v42.models.project_context import ProjectContext
from brain_v42.models.runbook import Runbook, RunbookStep
from brain_v42.models.snippet import Snippet

# ---------------------------------------------------------------------------
# Test data factories
# ---------------------------------------------------------------------------


def _make_learning(**overrides) -> Learning:
    defaults = {
        "id": UUID("ee3b329a-ba3a-468d-a2cd-04a98c80ea9d"),
        "topic": "GPU collector NVML",
        "insight": "NVML only works with proprietary NVIDIA drivers.",
        "source": None,
        "source_type": "experience",
        "confidence": "high",
        "project_key": "red",
        "tags": ["gpu", "nvml"],
        "metadata": {},
        "created_at": datetime(2026, 3, 12, tzinfo=UTC),
        "updated_at": datetime(2026, 3, 12, tzinfo=UTC),
        "validated_at": None,
        "last_accessed_at": None,
        "access_count": 0,
        "freshness_status": "fresh",
        "merged_into": None,
    }
    defaults.update(overrides)
    return Learning.model_validate(defaults)


def _make_decision(**overrides) -> Decision:
    defaults = {
        "id": UUID("abc12345-0000-0000-0000-000000000000"),
        "title": "Docker containers grouped by Compose stack",
        "description": "Group containers by Compose stack in dashboard.",
        "reasoning": "Better readability, a lone container lacks context.",
        "alternatives": ["Individual containers", "Group by image"],
        "consequences": None,
        "project_key": "red",
        "tags": ["docker", "dashboard"],
        "status": "active",
        "metadata": {},
        "created_at": datetime(2026, 3, 12, tzinfo=UTC),
        "updated_at": datetime(2026, 3, 12, tzinfo=UTC),
        "superseded_by": None,
        "last_accessed_at": None,
        "access_count": 0,
        "freshness_status": "fresh",
        "merged_into": None,
    }
    defaults.update(overrides)
    return Decision.model_validate(defaults)


def _make_snippet(**overrides) -> Snippet:
    defaults = {
        "id": UUID("11223344-0000-0000-0000-000000000000"),
        "title": "Docker health check pattern",
        "intention": "Health check HTTP endpoint for Docker containers",
        "code": "func healthHandler(w http.ResponseWriter, r *http.Request) {\n    w.WriteHeader(http.StatusOK)\n}",
        "language": "go",
        "dependencies": [],
        "usage_example": None,
        "gotchas": "Timeout must be shorter than Docker's health interval",
        "project_key": "red",
        "tags": ["docker", "health"],
        "metadata": {},
        "created_at": datetime(2026, 3, 10, tzinfo=UTC),
        "updated_at": datetime(2026, 3, 10, tzinfo=UTC),
        "use_count": 3,
        "last_used_at": datetime(2026, 3, 12, tzinfo=UTC),
        "last_accessed_at": None,
        "access_count": 0,
        "freshness_status": "fresh",
        "merged_into": None,
    }
    defaults.update(overrides)
    return Snippet.model_validate(defaults)


def _make_runbook(**overrides) -> Runbook:
    defaults = {
        "id": UUID("aabb1122-0000-0000-0000-000000000000"),
        "title": "Deploy red-monitor",
        "description": "Deploy red-monitor to target machine",
        "project_key": "red",
        "trigger": "New version tagged",
        "prerequisites": ["Go 1.22+", "SSH access to target"],
        "steps": [
            RunbookStep(order=1, title="Build", command="go build -o red-monitor ./cmd/server"),
            RunbookStep(order=2, title="Deploy", command="scp red-monitor user@host:/opt/"),
        ],
        "rollback_steps": [
            RunbookStep(order=1, title="Restore", command="cp backup /opt/red-monitor"),
        ],
        "estimated_duration": "15 minutes",
        "tags": ["deploy"],
        "metadata": {},
        "created_at": datetime(2026, 3, 12, tzinfo=UTC),
        "updated_at": datetime(2026, 3, 12, tzinfo=UTC),
        "execution_count": 7,
        "last_executed_at": datetime(2026, 3, 12, tzinfo=UTC),
        "last_execution_status": "success",
        "last_accessed_at": None,
        "access_count": 0,
        "freshness_status": "fresh",
        "merged_into": None,
    }
    defaults.update(overrides)
    return Runbook.model_validate(defaults)


def _make_adr(**overrides) -> ADR:
    defaults = {
        "id": UUID("ccdd3344-0000-0000-0000-000000000000"),
        "number": 4,
        "title": "TSDB custom en Go",
        "context": "PostgreSQL not optimal for time series at scale.",
        "decision": "Build custom TSDB in Go.",
        "consequences": "More dev effort upfront, full control long-term.",
        "alternatives_considered": [],
        "project_key": "red",
        "tags": ["tsdb", "go"],
        "status": "proposed",
        "metadata": {},
        "created_at": datetime(2026, 3, 13, tzinfo=UTC),
        "updated_at": datetime(2026, 3, 13, tzinfo=UTC),
        "decided_at": None,
        "superseded_by": None,
        "last_accessed_at": None,
        "access_count": 0,
        "freshness_status": "fresh",
        "merged_into": None,
    }
    defaults.update(overrides)
    return ADR.model_validate(defaults)


def _make_plan_chunk(**overrides) -> IndexedPlanChunk:
    defaults = {
        "id": UUID("dddd1111-0000-0000-0000-000000000000"),
        "plan_id": UUID("eeee2222-0000-0000-0000-000000000000"),
        "section_title": "Goals",
        "section_path": "Plans Chunking > Goals",
        "content": "Make project plans discoverable via brain_search.",
        "section_order": 1,
        "word_count": 42,
        "project_key": "brain-v42",
        "plan_type": "spec",
        "status": "active",
        "tags": ["plan-chunking", "search"],
        "access_count": 0,
        "last_accessed_at": None,
        "created_at": datetime(2026, 4, 7, tzinfo=UTC),
    }
    defaults.update(overrides)
    return IndexedPlanChunk.model_validate(defaults)


def _make_project_context(**overrides) -> ProjectContext:
    defaults = {
        "id": UUID("00000000-0000-0000-0000-000000000001"),
        "project_key": "red",
        "name": "Mega projet infra personnel",
        "description": "Ecosystem of personal infrastructure tools.",
        "languages": ["Go", "Python"],
        "frameworks": ["FastMCP", "SQLAlchemy"],
        "databases": ["PostgreSQL 16"],
        "code_style": None,
        "git_workflow": None,
        "test_strategy": None,
        "current_phase": "production",
        "current_focus": "Brainstorm feature #8",
        "blockers": [],
        "related_projects": ["brain_v42", "auto_discord"],
        "local_path": None,
        "repo_url": None,
        "metadata": {},
        "created_at": datetime(2026, 3, 10, tzinfo=UTC),
        "updated_at": datetime(2026, 3, 13, tzinfo=UTC),
        "decisions_count": 12,
        "learnings_count": 25,
        "snippets_count": 8,
        "runbooks_count": 3,
        "adrs_count": 4,
    }
    defaults.update(overrides)
    return ProjectContext.model_validate(defaults)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestFormatId:
    def test_uuid_string(self) -> None:
        from brain_v42.mcp.tools.formatters import format_id

        assert (
            format_id("ee3b329a-ba3a-468d-a2cd-04a98c80ea9d")
            == "ee3b329a-ba3a-468d-a2cd-04a98c80ea9d"
        )

    def test_uuid_object(self) -> None:
        from brain_v42.mcp.tools.formatters import format_id

        assert (
            format_id(UUID("ee3b329a-ba3a-468d-a2cd-04a98c80ea9d"))
            == "ee3b329a-ba3a-468d-a2cd-04a98c80ea9d"
        )


class TestShortDate:
    def test_datetime(self) -> None:
        from brain_v42.mcp.tools.formatters import short_date

        dt = datetime(2026, 3, 12, 14, 30, 0, tzinfo=UTC)
        assert short_date(dt) == "2026-03-12"


class TestFormatConfirmation:
    """Compact write-confirmation contract.

    The LLM already holds ``title`` and ``project`` in its context (it just
    passed them as arguments), so echoing them back burns tokens without
    adding information. Only the server-generated ``id`` and any extra
    kwargs (ADR number, execution count, etc.) are new. The leading ``✓``
    is dropped — ``ok`` tokenizes cheaper and serves the same semantic.
    """

    def test_minimal_has_ok_and_action(self) -> None:
        from brain_v42.mcp.tools.formatters import format_confirmation

        result = format_confirmation("Learned", "GPU NVML")
        assert result == "ok Learned"

    def test_does_not_echo_title_or_project(self) -> None:
        from brain_v42.mcp.tools.formatters import format_confirmation

        result = format_confirmation(
            "Learned",
            "GPU NVML",
            id="ee3b329a-ba3a-468d-a2cd-04a98c80ea9d",
            project="red",
        )
        assert "GPU NVML" not in result
        assert "project" not in result
        assert "red" not in result

    def test_id_is_surfaced(self) -> None:
        from brain_v42.mcp.tools.formatters import format_confirmation

        result = format_confirmation(
            "Learned",
            "GPU NVML",
            id="ee3b329a-ba3a-468d-a2cd-04a98c80ea9d",
            project="red",
        )
        assert "id:ee3b329a-ba3a-468d-a2cd-04a98c80ea9d" in result

    def test_extra_kwargs_surfaced(self) -> None:
        from brain_v42.mcp.tools.formatters import format_confirmation

        result = format_confirmation(
            "Snippet saved",
            "Docker health",
            id="11223344-0000-0000-0000-000000000000",
            lang="go",
        )
        assert "lang:go" in result
        assert "id:11223344" in result

    def test_no_leading_checkmark(self) -> None:
        from brain_v42.mcp.tools.formatters import format_confirmation

        result = format_confirmation("Learned", "x", id="abc")
        assert not result.startswith("✓")
        assert result.startswith("ok ")


class TestFormatError:
    def test_basic(self) -> None:
        import pytest
        from fastmcp.exceptions import ToolError

        from brain_v42.mcp.tools.formatters import format_error

        with pytest.raises(ToolError, match="^Learning not found: id:bad00000$"):
            format_error("Learning not found: id:bad00000")


# ---------------------------------------------------------------------------
# Learning formatter
# ---------------------------------------------------------------------------


class TestFormatLearnings:
    def test_single_learning(self) -> None:
        from brain_v42.mcp.tools.formatters import format_learnings

        result = format_learnings([_make_learning()], query="monitoring")
        assert '## 1 learning found for "monitoring"' in result
        assert "**GPU collector NVML** [high]" in result
        assert "id:ee3b329a" in result
        assert "2026-03-12" in result
        assert "NVML only works" in result
        assert "Tags: gpu, nvml" in result
        # Noise fields stripped
        assert "access_count" not in result
        assert "freshness_status" not in result
        assert "merged_into" not in result

    def test_empty_results_with_hint(self) -> None:
        from brain_v42.mcp.tools.formatters import format_learnings

        result = format_learnings([], query="kubernetes")
        assert "0 learnings found" in result
        assert '→ Try brain_search("kubernetes")' in result

    def test_validated_learning(self) -> None:
        from brain_v42.mcp.tools.formatters import format_learnings

        lr = _make_learning(validated_at=datetime(2026, 3, 13, tzinfo=UTC))
        result = format_learnings([lr])
        assert "Validated: 2026-03-13" in result

    def test_no_tags_omits_line(self) -> None:
        from brain_v42.mcp.tools.formatters import format_learnings

        lr = _make_learning(tags=[])
        result = format_learnings([lr])
        assert "Tags:" not in result

    def test_multiple_learnings(self) -> None:
        from brain_v42.mcp.tools.formatters import format_learnings

        lr1 = _make_learning()
        lr2 = _make_learning(
            id=UUID("a1b2c3d4-0000-0000-0000-000000000000"),
            topic="systemd user services",
            insight="User services need lingering enabled.",
            tags=["systemd"],
        )
        result = format_learnings([lr1, lr2])
        assert "## 2 learnings found" in result
        assert "1. **GPU collector NVML**" in result
        assert "2. **systemd user services**" in result

    def test_no_query_no_hint(self) -> None:
        from brain_v42.mcp.tools.formatters import format_learnings

        result = format_learnings([])
        assert "0 learnings found" in result
        assert "→" not in result


# ---------------------------------------------------------------------------
# Decision formatter
# ---------------------------------------------------------------------------


class TestFormatDecisions:
    def test_single_decision(self) -> None:
        from brain_v42.mcp.tools.formatters import format_decisions

        result = format_decisions([_make_decision()], query="monitoring")
        assert '## 1 decision matching "monitoring"' in result
        assert "**Docker containers grouped by Compose stack** [active]" in result
        assert "id:abc12345" in result
        assert "Reasoning:" in result
        assert "access_count" not in result

    def test_superseded_shows_superseded_by(self) -> None:
        from brain_v42.mcp.tools.formatters import format_decisions

        d = _make_decision(
            status="superseded",
            superseded_by=UUID("ff998877-0000-0000-0000-000000000000"),
        )
        result = format_decisions([d])
        assert "[superseded]" in result
        assert "Superseded by: id:ff998877" in result

    def test_empty_results_with_hint(self) -> None:
        from brain_v42.mcp.tools.formatters import format_decisions

        result = format_decisions([], query="kubernetes")
        assert "0 decisions" in result
        assert "→ Try brain_search" in result

    def test_no_query_uses_found(self) -> None:
        from brain_v42.mcp.tools.formatters import format_decisions

        result = format_decisions([_make_decision()])
        assert "## 1 decision found" in result


# ---------------------------------------------------------------------------
# Snippet formatter
# ---------------------------------------------------------------------------


class TestFormatSnippets:
    """Compact snippet rendering for list/search contexts.

    Verbose fields (code body, gotchas, use_count, deps, usage_example, tags)
    are reserved for ``format_snippet_detail`` (brain_get). Token-perf: each
    snippet in a 20-result search drops from ~400 to ~80 tokens.
    """

    def test_compact_header_with_intention(self) -> None:
        from brain_v42.mcp.tools.formatters import format_snippets

        result = format_snippets([_make_snippet()], query="docker health")
        assert '## 1 snippet matching "docker health"' in result
        assert "**Docker health check pattern** [go]" in result
        assert "id:11223344-0000-0000-0000-000000000000" in result
        assert "Health check HTTP endpoint for Docker containers" in result

    def test_omits_code_block(self) -> None:
        from brain_v42.mcp.tools.formatters import format_snippets

        result = format_snippets([_make_snippet()])
        assert "```go" not in result
        assert "healthHandler" not in result

    def test_omits_gotchas(self) -> None:
        from brain_v42.mcp.tools.formatters import format_snippets

        result = format_snippets([_make_snippet()])
        assert "Gotchas" not in result

    def test_omits_use_count(self) -> None:
        from brain_v42.mcp.tools.formatters import format_snippets

        result = format_snippets([_make_snippet(use_count=42)])
        assert "Used" not in result
        assert "42" not in result

    def test_omits_deps_in_compact(self) -> None:
        from brain_v42.mcp.tools.formatters import format_snippets

        s = _make_snippet(dependencies=["aiohttp", "asyncio"])
        result = format_snippets([s])
        assert "Deps:" not in result
        assert "aiohttp" not in result

    def test_omits_usage_example(self) -> None:
        from brain_v42.mcp.tools.formatters import format_snippets

        s = _make_snippet(usage_example="healthHandler(w, r)")
        result = format_snippets([s])
        assert "healthHandler(w, r)" not in result

    def test_omits_tags(self) -> None:
        from brain_v42.mcp.tools.formatters import format_snippets

        s = _make_snippet(tags=["docker", "health"])
        result = format_snippets([s])
        assert "Tags:" not in result

    def test_long_intention_truncated(self) -> None:
        from brain_v42.mcp.tools.formatters import format_snippets

        s = _make_snippet(intention="x" * 500)
        result = format_snippets([s])
        # Compact item line should not exceed ~250 chars total.
        item_lines = [ln for ln in result.splitlines() if ln.startswith("1. **")]
        assert len(item_lines) == 1
        assert len(item_lines[0]) < 250
        assert "…" in item_lines[0] or "..." in item_lines[0]

    def test_empty_snippets(self) -> None:
        from brain_v42.mcp.tools.formatters import format_snippets

        result = format_snippets([], query="nothing")
        assert "0 snippets" in result
        assert "→ Try brain_search" in result


class TestFormatSearchResultsSnippetCompact:
    """Snippets rendered inside a cross-type search must also be compact."""

    def test_search_results_snippet_section_is_compact(self) -> None:
        from brain_v42.mcp.tools.formatters import format_search_results
        from brain_v42.models.brain import SearchResult

        snippet_dict = _make_snippet().model_dump(mode="json")
        results = [SearchResult(type="snippet", score=0.91, item=snippet_dict)]
        rendered = format_search_results(results, query="health")

        assert "### Snippets" in rendered
        assert "**Docker health check pattern** [go]" in rendered
        # Verbose fields must NOT leak into search rendering.
        assert "```go" not in rendered
        assert "healthHandler" not in rendered
        assert "Gotchas" not in rendered
        assert "Used " not in rendered


# ---------------------------------------------------------------------------
# Runbook formatters
# ---------------------------------------------------------------------------


class TestFormatRunbook:
    def test_single_runbook(self) -> None:
        from brain_v42.mcp.tools.formatters import format_runbook

        result = format_runbook(_make_runbook())
        assert "## Runbook: Deploy red-monitor (id:aabb1122-0000-0000-0000-000000000000)" in result
        assert "**Trigger**: New version tagged" in result
        assert "**Prerequisites**: Go 1.22+, SSH access to target" in result
        assert "**Estimated duration**: 15 minutes" in result
        assert "### Steps" in result
        assert "1. **Build**" in result
        assert "go build" in result
        assert "### Rollback" in result
        assert "Last executed: 2026-03-12 (success, 7 times total)" in result

    def test_no_execution_history(self) -> None:
        from brain_v42.mcp.tools.formatters import format_runbook

        rb = _make_runbook(last_executed_at=None, execution_count=0)
        result = format_runbook(rb)
        assert "Last executed" not in result

    def test_no_rollback_steps(self) -> None:
        from brain_v42.mcp.tools.formatters import format_runbook

        rb = _make_runbook(rollback_steps=[])
        result = format_runbook(rb)
        assert "### Rollback" not in result


class TestFormatRunbooks:
    def test_list_of_runbooks(self) -> None:
        from brain_v42.mcp.tools.formatters import format_runbooks

        result = format_runbooks([_make_runbook()])
        assert "## 1 runbook" in result
        assert "**Deploy red-monitor**" in result
        assert "id:aabb1122" in result

    def test_empty_list(self) -> None:
        from brain_v42.mcp.tools.formatters import format_runbooks

        result = format_runbooks([])
        assert "## 0 runbooks found" in result

    def test_with_project_key(self) -> None:
        from brain_v42.mcp.tools.formatters import format_runbooks

        result = format_runbooks([_make_runbook()], project_key="red")
        assert '## 1 runbook for project "red"' in result


# ---------------------------------------------------------------------------
# ADR formatter
# ---------------------------------------------------------------------------


class TestFormatADRs:
    def test_single_adr(self) -> None:
        from brain_v42.mcp.tools.formatters import format_adrs

        result = format_adrs([_make_adr()], project_key="red")
        assert '## 1 ADR for project "red"' in result
        assert "**ADR #4: TSDB custom en Go** [proposed]" in result
        assert "id:ccdd3344" in result

    def test_superseded_adr(self) -> None:
        from brain_v42.mcp.tools.formatters import format_adrs

        adr = _make_adr(status="superseded", superseded_by=5)
        result = format_adrs([adr])
        assert "[superseded]" in result
        assert "Superseded by: ADR #5" in result

    def test_empty_adrs(self) -> None:
        from brain_v42.mcp.tools.formatters import format_adrs

        result = format_adrs([])
        assert "## 0 ADRs found" in result

    def test_adr_shows_decision_text(self) -> None:
        from brain_v42.mcp.tools.formatters import format_adrs

        result = format_adrs([_make_adr()])
        assert "Build custom TSDB in Go." in result


# ---------------------------------------------------------------------------
# Project context formatter
# ---------------------------------------------------------------------------


class TestFormatProjectContext:
    def test_full_context(self) -> None:
        from brain_v42.mcp.tools.formatters import format_project_context

        result = format_project_context(_make_project_context())
        assert "## Project: red (Mega projet infra personnel)" in result
        assert "**Phase**: production" in result
        assert "**Focus**: Brainstorm feature #8" in result
        assert "**Languages**: Go, Python" in result
        assert "**Frameworks**: FastMCP, SQLAlchemy" in result
        assert "**Databases**: PostgreSQL 16" in result
        assert "**Blockers**: none" in result
        assert "**Related projects**: brain_v42, auto_discord" in result
        assert "**Stats**: 12 decisions" in result
        assert "25 learnings" in result

    def test_with_blockers(self) -> None:
        from brain_v42.mcp.tools.formatters import format_project_context

        ctx = _make_project_context(blockers=["waiting on CI", "dependency upgrade"])
        result = format_project_context(ctx)
        assert "**Blockers**: waiting on CI, dependency upgrade" in result

    def test_minimal_context(self) -> None:
        from brain_v42.mcp.tools.formatters import format_project_context

        ctx = _make_project_context(
            current_phase=None,
            current_focus=None,
            languages=[],
            frameworks=[],
            databases=[],
            related_projects=[],
        )
        result = format_project_context(ctx)
        assert "## Project: red" in result
        assert "**Phase**" not in result
        assert "**Focus**" not in result
        assert "**Stats**" in result


# ---------------------------------------------------------------------------
# Search formatters
# ---------------------------------------------------------------------------


class TestFormatSearchResults:
    def test_groups_by_type(self) -> None:
        from brain_v42.mcp.tools.formatters import format_search_results

        results = [
            SearchResult(
                type="learning",
                score=0.85,
                item=_make_learning().model_dump(mode="json"),
            ),
            SearchResult(
                type="decision",
                score=0.80,
                item=_make_decision().model_dump(mode="json"),
            ),
        ]
        result = format_search_results(results, query="monitoring")
        assert '## 2 results for "monitoring" (across all types)' in result
        assert "### Decisions (1)" in result
        assert "### Learnings (1)" in result
        assert "**GPU collector NVML**" in result
        assert "**Docker containers" in result

    def test_empty_results(self) -> None:
        from brain_v42.mcp.tools.formatters import format_search_results

        result = format_search_results([], query="nothing")
        assert "0 results" in result

    def test_ordering_decisions_before_learnings(self) -> None:
        from brain_v42.mcp.tools.formatters import format_search_results

        results = [
            SearchResult(
                type="learning",
                score=0.9,
                item=_make_learning().model_dump(mode="json"),
            ),
            SearchResult(
                type="decision",
                score=0.8,
                item=_make_decision().model_dump(mode="json"),
            ),
        ]
        result = format_search_results(results, query="test")
        decisions_pos = result.index("### Decisions")
        learnings_pos = result.index("### Learnings")
        assert decisions_pos < learnings_pos

    def test_renders_plan_section(self) -> None:
        from brain_v42.mcp.tools.formatters import format_search_results

        chunk = _make_plan_chunk()
        results = [
            SearchResult(
                type="plan",
                score=0.91,
                item=chunk.model_dump(mode="json"),
                title=chunk.section_title,
                project_key=chunk.project_key,
                tags=chunk.tags,
                parent_id=chunk.plan_id,
            ),
        ]
        result = format_search_results(results, query="plan chunking")
        assert '## 1 result for "plan chunking" (across all types)' in result
        assert "### Plans (1)" in result
        assert "**Goals**" in result
        assert "Plans Chunking > Goals" in result
        # parent UUID must be exposed so the LLM can chain into brain_get
        assert "id:eeee2222-0000-0000-0000-000000000000" in result
        # The chunk's own UUID is not a valid brain_get input — it must NOT
        # leak into the rendered output, otherwise the LLM will reach for it
        # first and hit "plan not found".
        assert "dddd1111-0000-0000-0000-000000000000" not in result

    def test_displays_score_per_item(self) -> None:
        """Each rendered hit must expose its similarity score so the LLM
        can rank/filter without a second tool call."""
        from brain_v42.mcp.tools.formatters import format_search_results

        results = [
            SearchResult(
                type="learning",
                score=0.85,
                item=_make_learning().model_dump(mode="json"),
            ),
            SearchResult(
                type="decision",
                score=0.42,
                item=_make_decision().model_dump(mode="json"),
            ),
        ]
        rendered = format_search_results(results, query="x")
        assert "[s:0.85]" in rendered
        assert "[s:0.42]" in rendered

    def test_sorts_within_section_by_score_desc(self) -> None:
        """Within a single type section, items must appear in score-desc order."""
        from brain_v42.mcp.tools.formatters import format_search_results

        l_low = _make_learning(
            id=UUID("11111111-1111-1111-1111-111111111111"),
            topic="lower-score insight",
        )
        l_mid = _make_learning(
            id=UUID("22222222-2222-2222-2222-222222222222"),
            topic="mid-score insight",
        )
        l_high = _make_learning(
            id=UUID("33333333-3333-3333-3333-333333333333"),
            topic="highest-score insight",
        )
        results = [
            SearchResult(type="learning", score=0.30, item=l_low.model_dump(mode="json")),
            SearchResult(type="learning", score=0.91, item=l_high.model_dump(mode="json")),
            SearchResult(type="learning", score=0.62, item=l_mid.model_dump(mode="json")),
        ]
        rendered = format_search_results(results, query="x")
        pos_high = rendered.index("highest-score insight")
        pos_mid = rendered.index("mid-score insight")
        pos_low = rendered.index("lower-score insight")
        assert pos_high < pos_mid < pos_low


class TestFormatKnowledgeByType:
    def test_grouped_results(self) -> None:
        from brain_v42.mcp.tools.formatters import format_knowledge_by_type

        by_type = KnowledgeByType(
            learnings=[
                SearchResult(
                    type="learning",
                    score=0.9,
                    item=_make_learning().model_dump(mode="json"),
                )
            ],
            decisions=[
                SearchResult(
                    type="decision",
                    score=0.8,
                    item=_make_decision().model_dump(mode="json"),
                )
            ],
        )
        result = format_knowledge_by_type(by_type, topic="monitoring")
        assert '## Everything known about "monitoring" (2 items)' in result
        assert "### Decisions (1)" in result
        assert "### Learnings (1)" in result

    def test_empty_knowledge(self) -> None:
        from brain_v42.mcp.tools.formatters import format_knowledge_by_type

        by_type = KnowledgeByType()
        result = format_knowledge_by_type(by_type, topic="nothing")
        assert "0 items" in result

    def test_includes_plans_section(self) -> None:
        from brain_v42.mcp.tools.formatters import format_knowledge_by_type

        chunk = _make_plan_chunk()
        by_type = KnowledgeByType(
            plans=[
                SearchResult(
                    type="plan",
                    score=0.91,
                    item=chunk.model_dump(mode="json"),
                    title=chunk.section_title,
                    project_key=chunk.project_key,
                    tags=chunk.tags,
                    parent_id=chunk.plan_id,
                ),
            ],
        )
        result = format_knowledge_by_type(by_type, topic="plan chunking")
        assert '## Everything known about "plan chunking" (1 items)' in result
        assert "### Plans (1)" in result
        assert "**Goals**" in result


# ---------------------------------------------------------------------------
# Decay, consolidation, roadmap formatters
# ---------------------------------------------------------------------------


class TestFormatDecayStatus:
    """Compact per-type decay summary.

    The old markdown-table form dumped 5 rows × 5 columns even when most
    cells were 0, burning ~250 tokens on sparse corpora. The new form is
    one line per entity type with only non-zero buckets emitted:

        decision: 15f 3s 2a 1d
        learning: 20f 5s 1a
        snippet: 8f
    """

    def test_compact_one_line_per_type(self) -> None:
        from brain_v42.mcp.tools.formatters import format_decay_status

        stats = {
            "stats": {
                "decision": {"fresh": 15, "stale": 3, "archived": 2},
                "learning": {"fresh": 20, "stale": 5, "archived": 1},
                "snippet": {"fresh": 8, "stale": 0, "archived": 0},
                "runbook": {"fresh": 3, "stale": 1, "archived": 0},
                "adr": {"fresh": 4, "stale": 0, "archived": 0},
            },
            "deletion_candidates": {"decision": 1, "learning": 0},
        }
        result = format_decay_status(stats)
        assert "## Decay status" in result
        assert "decision: 15f 3s 2a 1d" in result
        assert "learning: 20f 5s 1a" in result
        assert "snippet: 8f" in result
        assert "runbook: 3f 1s" in result
        assert "adr: 4f" in result
        # Markdown table must NOT be present anymore.
        assert "| Type |" not in result
        assert "|------|" not in result

    def test_omits_type_with_all_zeros(self) -> None:
        from brain_v42.mcp.tools.formatters import format_decay_status

        stats = {
            "stats": {
                "decision": {"fresh": 5, "stale": 0, "archived": 0},
                "learning": {"fresh": 0, "stale": 0, "archived": 0},
            },
            "deletion_candidates": {},
        }
        result = format_decay_status(stats)
        assert "decision: 5f" in result
        assert "learning:" not in result

    def test_plan_is_rendered_when_it_is_the_only_non_zero_type(self) -> None:
        from brain_v42.mcp.tools.formatters import format_decay_status

        result = format_decay_status(
            {
                "stats": {
                    "plan": {"fresh": 3, "stale": 2, "archived": 1},
                },
                "deletion_candidates": {"plan": 4},
            }
        )

        assert result == "## Decay status\nplan: 3f 2s 1a 4d"

    def test_empty_stats_says_all_zero(self) -> None:
        from brain_v42.mcp.tools.formatters import format_decay_status

        result = format_decay_status({})
        assert "## Decay status" in result
        assert "all 0" in result
        assert "decision:" not in result


class TestFormatRoadmap:
    def test_table_format(self) -> None:
        from brain_v42.mcp.tools.formatters import format_roadmap

        projects = [
            {
                "project_key": "red",
                "name": "Mega projet",
                "current_phase": "production",
                "features": [
                    {
                        "name": "Core Monitoring",
                        "status": "deployed",
                        "last_activity": "2026-03-12",
                        "pinned": False,
                        "artifact_count": {"decision": 2, "learning": 3},
                    },
                ],
            }
        ]
        result = format_roadmap(projects)
        assert "## Roadmap" in result
        assert "### red (production)" in result
        assert "Core Monitoring" in result
        assert "deployed" in result
        assert "| Artifacts |" in result
        assert "2dec 3learn" in result

    def test_no_last_activity(self) -> None:
        from brain_v42.mcp.tools.formatters import format_roadmap

        projects = [
            {
                "project_key": "red",
                "current_phase": None,
                "features": [
                    {
                        "name": "Future",
                        "status": "planned",
                        "last_activity": None,
                        "pinned": False,
                        "artifact_count": {},
                    },
                ],
            }
        ]
        result = format_roadmap(projects)
        assert "### red" in result
        assert "\u2014" in result  # em-dash for no activity

    def test_empty_projects(self) -> None:
        from brain_v42.mcp.tools.formatters import format_roadmap

        result = format_roadmap([])
        assert "## Roadmap" in result

    def test_pinned_features(self) -> None:
        from brain_v42.mcp.tools.formatters import format_roadmap

        projects = [
            {
                "project_key": "red",
                "current_phase": "dev",
                "features": [
                    {
                        "name": "GPU Embeddings",
                        "status": "in_progress",
                        "last_activity": "2026-03-14",
                        "pinned": True,
                        "artifact_count": {"plan": 1, "gitlab_event": 3, "learning": 2},
                    },
                    {
                        "name": "Decay System",
                        "status": "done",
                        "last_activity": "2026-03-13",
                        "pinned": False,
                        "artifact_count": {"decision": 1},
                    },
                ],
            }
        ]
        result = format_roadmap(projects)
        assert "in_progress [pinned]" in result
        assert "done" in result
        assert "[pinned]" not in result.split("done")[0].split("in_progress [pinned]")[1]
        # Verify plan and gitlab_event artifact types are displayed
        assert "1plan" in result
        assert "3gl" in result
        assert "2learn" in result
        assert "1dec" in result


class TestFormatConsolidationCandidates:
    def test_format(self) -> None:
        from brain_v42.mcp.tools.formatters import format_consolidation_candidates

        candidates = [
            {
                "entity_type": "learning",
                "similarity": 0.95,
                "id_a": "aabb1122-0000-0000-0000-000000000000",
                "id_b": "ccdd3344-0000-0000-0000-000000000000",
                "title_a": "GPU NVML setup",
                "title_b": "NVML driver requirements",
            },
        ]
        result = format_consolidation_candidates(candidates)
        assert "## 1 consolidation candidate" in result
        assert "**learning** similarity:0.95" in result
        assert "GPU NVML setup" in result
        assert "NVML driver requirements" in result
        assert "id:aabb1122" in result
        assert "id:ccdd3344" in result

    def test_empty_candidates(self) -> None:
        from brain_v42.mcp.tools.formatters import format_consolidation_candidates

        result = format_consolidation_candidates([])
        assert "## 0 consolidation candidates" in result

    def test_multiple_candidates(self) -> None:
        from brain_v42.mcp.tools.formatters import format_consolidation_candidates

        candidates = [
            {
                "entity_type": "learning",
                "similarity": 0.95,
                "id_a": "aabb1122-0000-0000-0000-000000000000",
                "id_b": "ccdd3344-0000-0000-0000-000000000000",
                "title_a": "GPU NVML setup",
                "title_b": "NVML driver requirements",
            },
            {
                "entity_type": "decision",
                "similarity": 0.91,
                "id_a": "eeff5566-0000-0000-0000-000000000000",
                "id_b": "11223344-0000-0000-0000-000000000000",
                "title_a": "VPS localhost bind",
                "title_b": "VPS network security",
            },
        ]
        result = format_consolidation_candidates(candidates)
        assert "## 2 consolidation candidates" in result
        assert "1. **learning**" in result
        assert "2. **decision**" in result


# ---------------------------------------------------------------------------
# Detail formatters (brain_get)
# ---------------------------------------------------------------------------


class TestFormatDecisionDetail:
    def test_full_detail(self) -> None:
        from brain_v42.mcp.tools.formatters import format_decision_detail

        d = _make_decision(
            consequences="Requires Compose label parsing.",
            tags=["docker", "dashboard"],
        )
        result = format_decision_detail(d)
        assert (
            "## Decision: Docker containers grouped by Compose stack (id:abc12345-0000-0000-0000-000000000000)"
            in result
        )
        assert "**Status**: active" in result
        assert "**Created**: 2026-03-12" in result
        assert "**Project**: red" in result
        assert "**Description**: Group containers by Compose stack in dashboard." in result
        assert "**Reasoning**: Better readability" in result
        assert "**Alternatives**: Individual containers, Group by image" in result
        assert "**Consequences**: Requires Compose label parsing." in result
        assert "**Tags**: docker, dashboard" in result

    def test_superseded_shows_link(self) -> None:
        from brain_v42.mcp.tools.formatters import format_decision_detail

        d = _make_decision(
            status="superseded",
            superseded_by=UUID("ff998877-0000-0000-0000-000000000000"),
        )
        result = format_decision_detail(d)
        assert "**Superseded by**: id:ff998877" in result

    def test_no_optional_fields(self) -> None:
        from brain_v42.mcp.tools.formatters import format_decision_detail

        d = _make_decision(
            reasoning="",
            alternatives=[],
            consequences=None,
            tags=[],
            project_key=None,
        )
        result = format_decision_detail(d)
        assert "**Project**" not in result
        assert "**Alternatives**" not in result
        assert "**Consequences**" not in result
        assert "**Tags**" not in result

    def test_active_no_superseded_by(self) -> None:
        from brain_v42.mcp.tools.formatters import format_decision_detail

        d = _make_decision(status="active", superseded_by=None)
        result = format_decision_detail(d)
        assert "**Superseded by**" not in result

    def test_surfaces_access_count_when_nonzero(self) -> None:
        """REORG Part 2 guardrail check requires access_count visibility."""
        from brain_v42.mcp.tools.formatters import format_decision_detail

        d = _make_decision(access_count=12)
        result = format_decision_detail(d)
        assert "**Access count**: 12" in result

    def test_surfaces_freshness_status_when_archived(self) -> None:
        """REORG idempotency check requires freshness_status visibility."""
        from brain_v42.mcp.tools.formatters import format_decision_detail

        d = _make_decision(freshness_status="archived")
        result = format_decision_detail(d)
        assert "**Freshness**: archived" in result

    def test_omits_freshness_status_when_fresh(self) -> None:
        """Default 'fresh' status is implicit; only surface when notable."""
        from brain_v42.mcp.tools.formatters import format_decision_detail

        d = _make_decision(freshness_status="fresh")
        result = format_decision_detail(d)
        assert "**Freshness**" not in result


class TestFormatLearningDetail:
    def test_full_detail(self) -> None:
        from brain_v42.mcp.tools.formatters import format_learning_detail

        lr = _make_learning(
            source="nvidia docs",
            validated_at=datetime(2026, 3, 13, tzinfo=UTC),
        )
        result = format_learning_detail(lr)
        assert (
            "## Learning: GPU collector NVML [high] (id:ee3b329a-ba3a-468d-a2cd-04a98c80ea9d)"
            in result
        )
        assert "**Created**: 2026-03-12" in result
        assert "**Project**: red" in result
        assert "**Source**: nvidia docs (experience)" in result
        assert "**Validated**: 2026-03-13" in result
        assert "NVML only works with proprietary NVIDIA drivers." in result
        assert "**Tags**: gpu, nvml" in result

    def test_no_optional_fields(self) -> None:
        from brain_v42.mcp.tools.formatters import format_learning_detail

        lr = _make_learning(
            source=None,
            validated_at=None,
            project_key=None,
            tags=[],
        )
        result = format_learning_detail(lr)
        # confidence always present (model default: "medium"), but other optionals omitted
        assert "**Project**" not in result
        assert "**Source**" not in result
        assert "**Validated**" not in result
        assert "**Tags**" not in result

    def test_insight_on_own_line(self) -> None:
        from brain_v42.mcp.tools.formatters import format_learning_detail

        lr = _make_learning()
        result = format_learning_detail(lr)
        lines = result.split("\n")
        # Insight should appear after a blank line
        insight_idx = next(i for i, line in enumerate(lines) if "NVML only works" in line)
        assert lines[insight_idx - 1] == ""

    def test_surfaces_access_count_when_nonzero(self) -> None:
        """REORG Part 2 guardrail check requires access_count visibility."""
        from brain_v42.mcp.tools.formatters import format_learning_detail

        lr = _make_learning(access_count=8)
        result = format_learning_detail(lr)
        assert "**Access count**: 8" in result

    def test_surfaces_freshness_status_when_archived(self) -> None:
        """REORG idempotency check requires freshness_status visibility."""
        from brain_v42.mcp.tools.formatters import format_learning_detail

        lr = _make_learning(freshness_status="archived")
        result = format_learning_detail(lr)
        assert "**Freshness**: archived" in result

    def test_omits_freshness_status_when_fresh(self) -> None:
        """Default 'fresh' status is implicit; only surface when notable."""
        from brain_v42.mcp.tools.formatters import format_learning_detail

        lr = _make_learning(freshness_status="fresh")
        result = format_learning_detail(lr)
        assert "**Freshness**" not in result


class TestFormatSnippetDetail:
    def test_full_detail(self) -> None:
        from brain_v42.mcp.tools.formatters import format_snippet_detail

        s = _make_snippet(
            dependencies=["net/http"],
            usage_example="healthHandler(w, r)",
        )
        result = format_snippet_detail(s)
        assert (
            "## Snippet: Docker health check pattern [go] (id:11223344-0000-0000-0000-000000000000)"
            in result
        )
        assert "**Intention**: Health check HTTP endpoint" in result
        assert "**Project**: red" in result
        assert "**Dependencies**: net/http" in result
        assert "```go" in result
        assert "healthHandler" in result
        assert "```" in result
        assert "**Example**: `healthHandler(w, r)`" in result
        assert "**Gotchas**: Timeout must be shorter" in result
        assert "**Used**: 3 times (last: 2026-03-12)" in result
        assert "**Tags**: docker, health" in result

    def test_no_optional_fields(self) -> None:
        from brain_v42.mcp.tools.formatters import format_snippet_detail

        s = _make_snippet(
            dependencies=[],
            usage_example=None,
            gotchas=None,
            use_count=0,
            last_used_at=None,
            project_key=None,
            tags=[],
        )
        result = format_snippet_detail(s)
        assert "**Dependencies**" not in result
        assert "**Example**" not in result
        assert "**Gotchas**" not in result
        assert "**Used**" not in result
        assert "**Project**" not in result
        assert "**Tags**" not in result

    def test_code_block_present(self) -> None:
        from brain_v42.mcp.tools.formatters import format_snippet_detail

        s = _make_snippet(code="print('hello')", language="python")
        result = format_snippet_detail(s)
        assert "```python" in result
        assert "print('hello')" in result
        # code block is closed
        lines = result.split("\n")
        code_start = next(i for i, line in enumerate(lines) if line.startswith("```python"))
        code_end = next(i for i, line in enumerate(lines) if i > code_start and line == "```")
        assert code_end > code_start


class TestFormatADRDetail:
    def test_full_detail(self) -> None:
        from brain_v42.mcp.tools.formatters import format_adr_detail
        from brain_v42.models.adr import AlternativeConsidered

        adr = _make_adr(
            decided_at=datetime(2026, 3, 14, tzinfo=UTC),
            alternatives_considered=[
                AlternativeConsidered(
                    title="Use InfluxDB",
                    description="Managed TSDB",
                    reason_rejected="Vendor lock-in",
                ),
                AlternativeConsidered(
                    title="Use TimescaleDB",
                    description="PG extension",
                    reason_rejected="Complexity",
                ),
            ],
        )
        result = format_adr_detail(adr)
        assert (
            "## ADR #4: TSDB custom en Go [proposed] (id:ccdd3344-0000-0000-0000-000000000000)"
            in result
        )
        assert "**Project**: red" in result
        assert "**Created**: 2026-03-13" in result
        assert "**Decided**: 2026-03-14" in result
        assert "**Context**: PostgreSQL not optimal" in result
        assert "**Decision**: Build custom TSDB in Go." in result
        assert "**Consequences**: More dev effort" in result
        assert "**Alternatives considered**:" in result
        assert "- Use InfluxDB" in result
        assert "- Use TimescaleDB" in result
        assert "**Tags**: tsdb, go" in result

    def test_no_decided_at(self) -> None:
        from brain_v42.mcp.tools.formatters import format_adr_detail

        adr = _make_adr(decided_at=None)
        result = format_adr_detail(adr)
        assert "**Decided**" not in result

    def test_no_alternatives(self) -> None:
        from brain_v42.mcp.tools.formatters import format_adr_detail

        adr = _make_adr(alternatives_considered=[])
        result = format_adr_detail(adr)
        assert "**Alternatives considered**" not in result

    def test_no_tags(self) -> None:
        from brain_v42.mcp.tools.formatters import format_adr_detail

        adr = _make_adr(tags=[])
        result = format_adr_detail(adr)
        assert "**Tags**" not in result


class TestFormatProjectsList:
    def test_multiple_projects(self) -> None:
        from brain_v42.mcp.tools.formatters import format_projects_list

        ctxs = [
            _make_project_context(),
            _make_project_context(
                id=UUID("00000000-0000-0000-0000-000000000002"),
                project_key="brain_v42",
                name="Second Cerveau",
                current_phase="production",
                current_focus="Decay system tuning",
            ),
        ]
        result = format_projects_list(ctxs)
        assert "## 2 projects" in result
        assert "- **red** (Mega projet infra personnel) [production]" in result
        assert "- **brain_v42** (Second Cerveau) [production]" in result
        assert "Brainstorm feature #8" in result
        assert "Decay system tuning" in result

    def test_single_project(self) -> None:
        from brain_v42.mcp.tools.formatters import format_projects_list

        result = format_projects_list([_make_project_context()])
        assert "## 1 project" in result
        assert "projects" not in result  # singular

    def test_empty_list(self) -> None:
        from brain_v42.mcp.tools.formatters import format_projects_list

        result = format_projects_list([])
        assert "## 0 projects" in result

    def test_no_focus_no_phase(self) -> None:
        from brain_v42.mcp.tools.formatters import format_projects_list

        ctx = _make_project_context(current_phase=None, current_focus=None)
        result = format_projects_list([ctx])
        assert "- **red** (Mega projet infra personnel)" in result
        assert "[" not in result.split("\n")[-1]  # no phase bracket


class TestFormatSupersessionChain:
    def test_chain_of_three(self) -> None:
        from brain_v42.mcp.tools.formatters import format_supersession_chain

        chain = [
            _make_decision(
                id=UUID("11111111-0000-0000-0000-000000000000"),
                title="V1: SQLite storage",
                status="superseded",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            _make_decision(
                id=UUID("22222222-0000-0000-0000-000000000000"),
                title="V2: PostgreSQL storage",
                status="superseded",
                created_at=datetime(2026, 2, 1, tzinfo=UTC),
            ),
            _make_decision(
                id=UUID("33333333-0000-0000-0000-000000000000"),
                title="V3: PG + pgvector",
                status="active",
                created_at=datetime(2026, 3, 1, tzinfo=UTC),
            ),
        ]
        result = format_supersession_chain(chain)
        assert "## Supersession chain (3 decisions)" in result
        assert (
            "1. **V1: SQLite storage** [superseded] (2026-01-01, id:11111111-0000-0000-0000-000000000000) -->"
            in result
        )
        assert (
            "2. **V2: PostgreSQL storage** [superseded] (2026-02-01, id:22222222-0000-0000-0000-000000000000) -->"
            in result
        )
        assert (
            "3. **V3: PG + pgvector** [active] (2026-03-01, id:33333333-0000-0000-0000-000000000000) (current)"
            in result
        )

    def test_single_decision(self) -> None:
        from brain_v42.mcp.tools.formatters import format_supersession_chain

        chain = [_make_decision()]
        result = format_supersession_chain(chain)
        assert "## Supersession chain (1 decision)" in result
        assert "(current)" in result
        assert "-->" not in result

    def test_empty_chain(self) -> None:
        from brain_v42.mcp.tools.formatters import format_supersession_chain

        result = format_supersession_chain([])
        assert "No supersession chain found." in result


# ---------------------------------------------------------------------------
# Summary-only mode (brain_list payload-bounding param — REORG Part 1 unblock)
# ---------------------------------------------------------------------------


class TestFormatLearningsSummaryOnly:
    """Token-bounded list output for full-corpus pagination (REORG Part 1)."""

    def test_summary_excludes_insight_body(self) -> None:
        from brain_v42.mcp.tools.formatters import format_learnings

        lr = _make_learning(insight="A " * 500)
        result = format_learnings([lr], summary_only=True)
        assert "A A A" not in result
        assert "GPU collector NVML" in result

    def test_summary_includes_freshness_status(self) -> None:
        from brain_v42.mcp.tools.formatters import format_learnings

        lr = _make_learning(freshness_status="archived")
        result = format_learnings([lr], summary_only=True)
        assert "archived" in result

    def test_summary_includes_access_count(self) -> None:
        from brain_v42.mcp.tools.formatters import format_learnings

        lr = _make_learning(access_count=7)
        result = format_learnings([lr], summary_only=True)
        assert "access:7" in result

    def test_summary_includes_project_key(self) -> None:
        """REORG Part 1 needs project_key to detect missing/variant keys."""
        from brain_v42.mcp.tools.formatters import format_learnings

        lr = _make_learning(project_key="brain-v42")
        result = format_learnings([lr], summary_only=True)
        assert "brain-v42" in result

    def test_summary_includes_tags(self) -> None:
        """REORG Part 1 needs tags to detect variants (bug-fix vs bugfix)."""
        from brain_v42.mcp.tools.formatters import format_learnings

        lr = _make_learning(tags=["bug-fix", "ml"])
        result = format_learnings([lr], summary_only=True)
        assert "bug-fix" in result
        assert "ml" in result

    def test_default_summary_only_false_keeps_body(self) -> None:
        """Backward compat: summary_only defaults to False, body present."""
        from brain_v42.mcp.tools.formatters import format_learnings

        lr = _make_learning(insight="DETAIL_BODY_TOKEN")
        result = format_learnings([lr])
        assert "DETAIL_BODY_TOKEN" in result


class TestFormatDecisionsSummaryOnly:
    def test_summary_excludes_description_and_reasoning(self) -> None:
        from brain_v42.mcp.tools.formatters import format_decisions

        d = _make_decision(
            description="LONG_DESC " * 100,
            reasoning="LONG_REASONING " * 100,
        )
        result = format_decisions([d], summary_only=True)
        assert "LONG_DESC" not in result
        assert "LONG_REASONING" not in result
        assert "Docker containers grouped by Compose stack" in result

    def test_summary_includes_freshness_and_access(self) -> None:
        from brain_v42.mcp.tools.formatters import format_decisions

        d = _make_decision(freshness_status="archived", access_count=4)
        result = format_decisions([d], summary_only=True)
        assert "archived" in result
        assert "access:4" in result

    def test_summary_includes_tags_and_project(self) -> None:
        from brain_v42.mcp.tools.formatters import format_decisions

        d = _make_decision(project_key="brain-v42", tags=["scope", "promotion"])
        result = format_decisions([d], summary_only=True)
        assert "brain-v42" in result
        assert "scope" in result
        assert "promotion" in result

    def test_default_summary_only_false_keeps_description(self) -> None:
        from brain_v42.mcp.tools.formatters import format_decisions

        d = _make_decision(description="DETAIL_DESC_TOKEN")
        result = format_decisions([d])
        assert "DETAIL_DESC_TOKEN" in result


from brain_v42.mcp.tools.formatters import format_roadmap  # noqa: E402


class TestFormatRoadmapProjectCap:
    """The all-projects rendering must be bounded by NUMBER OF PROJECTS, not only
    by features per project.

    Ticket 316a8b50. Measured on 2026-08-10: 30 projects rendered (the ticket
    announced 11, it dated from 08-03), 33,943 characters, ~9.4k tokens. The
    per-project cap can do nothing about it: it truncates 5 out of 30, the volume
    comes from the NUMBER of sections.
    """

    @staticmethod
    def _project(key: str, last_activity: str | None) -> dict:
        return {
            "project_key": key,
            "features": [
                {
                    "name": f"feature de {key}",
                    "status": "building",
                    "artifact_count": {},
                    "last_activity": last_activity,
                }
            ],
            "last_activity": last_activity,
        }

    def test_format_roadmap_caps_projects_and_names_the_omitted_count(self) -> None:
        projects = [self._project(f"proj-{i:02d}", "2026-08-01T00:00:00") for i in range(30)]

        result = format_roadmap(projects, max_projects=10)

        assert sum(1 for line in result.splitlines() if line.startswith("### ")) == 10
        assert "20 project(s) omitted" in result

    def test_format_roadmap_keeps_the_most_recently_active_projects(self) -> None:
        """THE test that tells "a cap" from "the RIGHT cap".

        The formatter receives the projects in SQL order, ``ORDER BY project_key``:
        ALPHABETICAL. A naive ``projects[:cap]`` — the "exact mirror" the ticket
        asked for — would therefore keep the six ``aaa`` dead since 2020 and throw
        away the six ``zzz`` active today. Measured on production: it would have
        thrown away 14 projects active in the last 30 days, one of them active the
        day before.
        """
        projects = [self._project(f"aaa-{i:02d}", "2020-01-01T00:00:00") for i in range(6)]
        projects += [self._project(f"zzz-{i:02d}", "2026-08-10T00:00:00") for i in range(6)]

        result = format_roadmap(projects, max_projects=6)

        for i in range(6):
            assert f"### zzz-{i:02d}" in result, "un projet actif a été coupé"
            assert f"### aaa-{i:02d}" not in result, "un projet mort a été gardé"

    def test_the_omission_notice_names_the_omitted_projects(self) -> None:
        """A count alone leaves the caller with no possible action.

        The per-project notice already gives the recall ARGUMENT; the global notice
        must name what it hid, otherwise one cannot go and fetch it.
        """
        projects = [self._project(f"aaa-{i:02d}", "2020-01-01T00:00:00") for i in range(6)]
        projects += [self._project(f"zzz-{i:02d}", "2026-08-10T00:00:00") for i in range(6)]

        result = format_roadmap(projects, max_projects=6)

        assert "aaa-03" in result

    def test_a_zero_or_negative_cap_never_empties_the_roadmap(self) -> None:
        """Exact symmetry with the per-project clamp: max(1, …), never an empty output."""
        projects = [self._project("solo", "2026-08-10T00:00:00")]

        assert "### solo" in format_roadmap(projects, max_projects=0)
        assert "### solo" in format_roadmap(projects, max_projects=-5)

    def test_a_missing_last_activity_never_crashes_the_sort(self) -> None:
        """NULL means "never measured", not "the most recent".

        The fixtures pass ``None``, the tool path passes an ISO string, and some
        calls a ``datetime``. All three must sort without raising.
        """
        projects = [
            self._project("sans-date", None),
            self._project("avec-date", "2026-08-10T00:00:00"),
        ]

        result = format_roadmap(projects, max_projects=1)

        assert "### avec-date" in result
        assert "### sans-date" not in result


class TestRoadmapPhaseIsRenderedNextToItsBacklog:
    """A project's declared phase must sit next to what it claims to summarize.

    Ticket 2e921e14, point 3. brain-v42's `project_contexts.current_phase` reads
    "production — all milestones done, deployed", and it is the FIRST line an agent
    opening the roadmap reads. Measured on 2026-08-11: 4 `building` features, 23
    `design`, 73 `research`, 1 `planned`, plus 35 open tickets. "All milestones
    done" has been false for a long time.

    Since `current_phase` is free text with no link to the table, nothing
    contradicts it — the same family as the focus that asserted 037 for three days.
    We do not judge the prose: we put the measurement beside it, exactly what the
    briefing's "État technique (mesuré)" block does.
    """

    @staticmethod
    def _project(key: str, phase: str | None, statuses: list[str]) -> dict:
        return {
            "project_key": key,
            "current_phase": phase,
            "last_activity": "2026-08-10T00:00:00",
            "features": [
                {
                    "name": f"f-{index}",
                    "status": status,
                    "artifact_count": {},
                    "last_activity": None,
                }
                for index, status in enumerate(statuses)
            ],
        }

    def test_the_open_work_is_counted_under_the_declared_phase(self) -> None:
        projects = [
            self._project(
                "brain-v42",
                "production — all milestones done, deployed",
                ["building", "building", "design", "research", "done", "archived"],
            )
        ]

        result = format_roadmap(projects)

        assert "production — all milestones done, deployed" in result, "la prose reste"
        assert "2 building" in result
        assert "1 design" in result
        assert "1 research" in result

    def test_finished_statuses_are_not_counted_as_open_work(self) -> None:
        """`done` and `archived` do not contradict "milestones done" — they support it.

        Counting them would inflate the line and make it unreadable on an old
        project, where history always dominates work in progress.
        """
        projects = [self._project("p", "production", ["done", "done", "archived", "deployed"])]

        result = format_roadmap(projects)

        assert "done" not in result.split("### p")[1].split("|")[0].replace("production", "")

    def test_a_project_with_no_open_work_gets_no_counts_line(self) -> None:
        """The nominal case is SILENT.

        A "0 in progress" line on every finished project would teach the reader to
        skip the line, and so to miss it the day it contradicts the phase.
        """
        projects = [self._project("p", "production", ["done", "archived"])]

        section = format_roadmap(projects).split("### p")[1]

        assert "en cours" not in section

    def test_the_counts_survive_a_missing_phase(self) -> None:
        """A project with no declared phase keeps the right to be measured."""
        projects = [self._project("p", None, ["building"])]

        assert "1 building" in format_roadmap(projects)
