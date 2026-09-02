"""Tests for brain_session_start tool."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastmcp import FastMCP

from brain_v42.mcp.tools.session_tools import (
    _format_session_briefing,
    _section_blockers,
    _section_cross_project,
    _section_drill_in_hint,
    _section_focus,
    _section_killswitches,
    _section_last_failure,
    _section_recap,
    _section_roadmap,
    _section_stale_pinned,
    _section_technical_state,
    register_session_tools,
)
from brain_v42.models.brain_session import (
    BrainSession,
    BrainSessionStartResult,
    BrainSessionStatus,
)
from brain_v42.services.cross_project_service import CrossEntry, CrossProjectBlock
from brain_v42.services.dream_run_service import KillswitchState, LastFailureRow

# ---------------------------------------------------------------------------
# _format_session_briefing pure function tests  (updated to 7-arg shape)
# ---------------------------------------------------------------------------


def _no_activity_ks() -> KillswitchState:
    return KillswitchState(
        last_run_date=None,
        promote_enabled=False,
        promote_dry=False,
        reorg_enabled=False,
        reorg_dry=False,
        promote_clean_dry_nights=0,
        reorg_clean_dry_nights=0,
    )


def _brain_session_service(project_key: str = "p") -> MagicMock:
    """Return a persistent lifecycle service stub for facade tests."""
    now = datetime.now(UTC)
    session = BrainSession(
        id=uuid4(),
        project_key=project_key,
        client_key="client-1",
        status=BrainSessionStatus.OPEN,
        started_focus="f",
        started_focus_revision=0,
        started_at=now,
        updated_at=now,
    )
    service = MagicMock()
    service.start = AsyncMock(
        return_value=BrainSessionStartResult(
            session=session,
            replayed=False,
            open_session_count=1,
        )
    )
    return service


class TestFormatSessionBriefing:
    def test_with_full_context(self):
        """Briefing includes focus, decisions, and learnings."""
        ctx = MagicMock()
        ctx.project_key = "brain_v42"
        ctx.current_focus = "hybrid search implementation"
        ctx.description = "Second Cerveau MCP"
        ctx.blockers = []

        decision = MagicMock()
        decision.title = "Use RRF fusion"
        decision.created_at = datetime(2026, 3, 12, tzinfo=UTC)

        learning = MagicMock()
        learning.topic = "RRF"
        learning.insight = "Reciprocal Rank Fusion works well for hybrid search"

        result = _format_session_briefing(
            ctx, [decision], [learning], _no_activity_ks(), None, [], []
        )

        assert "brain_v42" in result
        assert "hybrid search implementation" in result
        assert "Use RRF fusion" in result
        assert "RRF" in result

    def test_no_context(self):
        """Missing project context shows fallback message."""
        result = _format_session_briefing(None, [], [], _no_activity_ks(), None, [], [])
        assert "no project context found" in result.lower()

    def test_empty_decisions_and_learnings(self):
        """Works with empty lists."""
        ctx = MagicMock()
        ctx.project_key = "test"
        ctx.current_focus = "testing"
        ctx.description = None
        ctx.blockers = []

        result = _format_session_briefing(ctx, [], [], _no_activity_ks(), None, [], [])
        assert "testing" in result

    def test_no_description_omits_project_line(self):
        """When ctx.description is None, no Project line in output."""
        ctx = MagicMock()
        ctx.project_key = "test"
        ctx.current_focus = "testing"
        ctx.description = None
        ctx.blockers = []

        result = _format_session_briefing(ctx, [], [], _no_activity_ks(), None, [], [])
        assert "Project:" not in result

    def test_insight_truncation(self):
        """Long insights are truncated."""
        ctx = MagicMock()
        ctx.project_key = "t"
        ctx.current_focus = "f"
        ctx.description = None
        ctx.blockers = []

        learning = MagicMock()
        learning.topic = "topic"
        learning.insight = "x" * 200

        result = _format_session_briefing(ctx, [], [learning], _no_activity_ks(), None, [], [])
        assert "x" * 200 not in result


# ---------------------------------------------------------------------------
# Section helper unit tests
# ---------------------------------------------------------------------------


class TestSectionKillswitches:
    def test_renders_full_state(self):
        state = KillswitchState(
            last_run_date=date(2026, 5, 14),
            promote_enabled=True,
            promote_dry=False,
            reorg_enabled=True,
            reorg_dry=True,
            promote_clean_dry_nights=0,
            reorg_clean_dry_nights=3,
        )
        out = _section_killswitches(state)
        assert "### Killswitches" in out
        assert "2026-05-14" in out
        assert "PROMOTE" in out and "wet" in out.lower()
        assert "REORG" in out and "dry" in out.lower()
        assert "3" in out

    def test_no_activity_renders_anchor(self):
        state = KillswitchState(
            last_run_date=None,
            promote_enabled=False,
            promote_dry=False,
            reorg_enabled=False,
            reorg_dry=False,
            promote_clean_dry_nights=0,
            reorg_clean_dry_nights=0,
        )
        out = _section_killswitches(state)
        assert "no dream pipeline activity" in out.lower()

    def _state(self):
        return KillswitchState(
            last_run_date=date(2026, 5, 14),
            promote_enabled=True,
            promote_dry=False,
            reorg_enabled=True,
            reorg_dry=True,
            promote_clean_dry_nights=0,
            reorg_clean_dry_nights=3,
        )

    def test_graph_row_reflects_config_enabled(self):
        """GRAPH row reflects the graph_enabled flag threaded by the caller —
        resolved from canonical config.graph_enabled at the tool entry, not a
        divergent os.getenv default. The helper stays pure (no Settings load).
        See brain learning 80b4e8a6 + the 2026-05-29 audit drift finding."""
        out = _section_killswitches(self._state(), graph_enabled=True)
        assert "GRAPH" in out
        assert "enabled" in out.split("GRAPH")[1].splitlines()[0]

    def test_graph_row_reflects_config_disabled(self):
        """graph_enabled=False renders the row as disabled."""
        out = _section_killswitches(self._state(), graph_enabled=False)
        assert "GRAPH" in out
        assert "disabled" in out.split("GRAPH")[1].splitlines()[0]

    def test_extract_enabled_dry_with_streak(self):
        """EXTRACT row: enabled (dry · 2 clean DRY nights) when extract_enabled=True/dry/streak=2."""
        state = KillswitchState(
            last_run_date=date(2026, 7, 4),
            promote_enabled=False,
            promote_dry=False,
            reorg_enabled=False,
            reorg_dry=False,
            promote_clean_dry_nights=0,
            reorg_clean_dry_nights=0,
            extract_enabled=True,
            extract_dry=True,
            extract_clean_dry_nights=2,
        )
        out = _section_killswitches(state)
        assert "EXTRACT: enabled (dry · 2 clean DRY nights)" in out

    def test_extract_disabled_by_default(self):
        """EXTRACT row: disabled when extract_enabled=False (default)."""
        out = _section_killswitches(self._state())
        assert "EXTRACT: disabled" in out

    def test_roadmap_enabled_dry_with_streak(self):
        state = KillswitchState(
            last_run_date=date(2026, 7, 4),
            promote_enabled=False,
            promote_dry=False,
            reorg_enabled=False,
            reorg_dry=False,
            promote_clean_dry_nights=0,
            reorg_clean_dry_nights=0,
            roadmap_enabled=True,
            roadmap_dry=True,
            roadmap_clean_dry_nights=2,
        )
        out = _section_killswitches(state)
        assert "- ROADMAP: enabled (dry · 2 clean DRY nights)" in out

    def test_roadmap_disabled_by_default(self):
        state = KillswitchState(
            last_run_date=date(2026, 7, 4),
            promote_enabled=False,
            promote_dry=False,
            reorg_enabled=False,
            reorg_dry=False,
            promote_clean_dry_nights=0,
            reorg_clean_dry_nights=0,
        )
        out = _section_killswitches(state)
        assert "- ROADMAP: disabled" in out

    def test_sweep_disabled_by_default(self):
        out = _section_killswitches(self._state())
        assert "- SWEEP  : disabled" in out

    def test_sweep_enabled_dry_with_streak(self):
        state = KillswitchState(
            last_run_date=date(2026, 8, 7),
            promote_enabled=False,
            promote_dry=False,
            reorg_enabled=False,
            reorg_dry=False,
            promote_clean_dry_nights=0,
            reorg_clean_dry_nights=0,
            sweep_enabled=True,
            sweep_dry=True,
            sweep_clean_dry_nights=3,
        )
        out = _section_killswitches(state)
        assert "- SWEEP  : enabled (dry · 3 clean DRY nights)" in out

    def test_sweep_row_sits_between_roadmap_and_graph(self):
        """The position is the contract: after ROADMAP, just before GRAPH."""
        state = KillswitchState(
            last_run_date=date(2026, 8, 7),
            promote_enabled=False,
            promote_dry=False,
            reorg_enabled=False,
            reorg_dry=False,
            promote_clean_dry_nights=0,
            reorg_clean_dry_nights=0,
            sweep_enabled=True,
            sweep_dry=True,
            sweep_clean_dry_nights=3,
        )

        lines = _section_killswitches(state, graph_enabled=True).splitlines()
        # Located by prefix: this test is about the POSITION, not the rendering
        # (covered by test_sweep_enabled_dry_with_streak).
        sweep_index = next(i for i, line in enumerate(lines) if line.startswith("- SWEEP"))

        assert sweep_index == next(i for i, x in enumerate(lines) if x.startswith("- ROADMAP")) + 1
        assert sweep_index == next(i for i, x in enumerate(lines) if x.startswith("- GRAPH")) - 1


class TestSectionLastFailure:
    def test_renders_with_failure(self):
        failure = LastFailureRow(
            phase="reorg",
            run_date=date(2026, 5, 13),
            error_message="Boom\nstack...",
        )
        out = _section_last_failure(failure)
        assert "### Last failure" in out
        assert "reorg" in out
        assert "Boom" in out
        assert "stack" not in out  # only first line

    def test_omits_when_none(self):
        assert _section_last_failure(None) == ""


class TestSectionRoadmap:
    def _item(self, **kw):
        from brain_v42.services.feature_service import RoadmapAliveFeature

        defaults = {
            "name": "Recherche hybride",
            "status": "building",
            "pinned": False,
            "artifact_count": 7,
            "last_artifact_at": datetime.now(UTC) - timedelta(days=2),
        }
        defaults.update(kw)
        return RoadmapAliveFeature(**defaults)

    def test_renders_spec_format(self):
        out = _section_roadmap([self._item()])
        assert out.startswith("### Roadmap (1)")
        assert "- Recherche hybride [building] — 7 artifacts, dernier il y a 2j" in out

    def test_zero_artifacts_renders_without_last(self):
        out = _section_roadmap([self._item(artifact_count=0, last_artifact_at=None)])
        assert "— 0 artifact" in out
        assert "dernier il y a" not in out

    def test_omits_when_empty(self):
        assert _section_roadmap([]) == ""

    def test_cap_5(self):
        items = [self._item(name=f"f{i}") for i in range(8)]
        out = _section_roadmap(items)
        assert "### Roadmap (5)" in out
        assert "f4" in out and "f5" not in out


class TestSectionStalePinned:
    def test_renders_list(self):
        f = type("F", (), {})()
        f.name = "stale-x"
        f.updated_at = datetime.now(tz=UTC) - timedelta(days=40)
        out = _section_stale_pinned([f])
        assert "### Stale-pinned" in out
        assert "stale-x" in out

    def test_omits_when_empty(self):
        assert _section_stale_pinned([]) == ""

    def test_cap_5(self):
        items = [
            type(
                "F",
                (),
                {"name": f"f{i}", "updated_at": datetime.now(tz=UTC) - timedelta(days=40)},
            )()
            for i in range(7)
        ]
        out = _section_stale_pinned(items)
        assert out.count("- ") == 5


class TestSectionFocus:
    def test_renders_focus(self):
        ctx = type("C", (), {"current_focus": "ship the briefing"})()
        out = _section_focus(ctx)
        assert "### Focus" in out
        assert "ship the briefing" in out

    def test_anchor_when_no_focus(self):
        ctx = type("C", (), {"current_focus": None})()
        out = _section_focus(ctx)
        assert "### Focus" in out


class TestSectionBlockers:
    def test_renders_list(self):
        out = _section_blockers(["calibration pending", "GPU vmem drift"])
        assert "### Blockers" in out
        assert "calibration pending" in out
        assert "GPU vmem drift" in out

    def test_omits_when_empty(self):
        assert _section_blockers([]) == ""

    def test_cap_5(self):
        out = _section_blockers([f"b{i}" for i in range(7)])
        assert out.count("- ") == 5


class TestSectionRecap:
    def test_renders_three_of_each(self):
        decisions = [type("D", (), {"title": f"d{i}"})() for i in range(5)]
        learnings = [
            type(
                "L",
                (),
                {"topic": f"t{i}", "insight": f"insight {i}" * 5},
            )()
            for i in range(5)
        ]
        out = _section_recap(decisions, learnings)
        assert "### Recap" in out
        assert out.count("\n- d:") == 3
        assert out.count("\n- l:") == 3

    def test_empty_returns_empty(self):
        assert _section_recap([], []) == ""


class TestSectionDrillInHint:
    def test_renders_hint(self):
        out = _section_drill_in_hint()
        assert "brain_search" in out or "brain_get" in out


# ---------------------------------------------------------------------------
# brain_session_start tool registration + orchestration
# ---------------------------------------------------------------------------


class TestBrainSessionStartTool:
    @pytest.mark.asyncio
    async def test_lifecycle_tools_registered(self):
        mcp = FastMCP("test")
        register_session_tools(
            mcp,
            AsyncMock(),
            AsyncMock(),
            AsyncMock(),
            AsyncMock(),
            AsyncMock(),
            _brain_session_service(),
        )

        for name in (
            "brain_session_start",
            "brain_session_capture",
            "brain_session_heartbeat",
            "brain_session_end",
            "brain_session_list",
            "brain_session_resume",
            "brain_session_abandon",
        ):
            assert await mcp.get_tool(name) is not None

    @pytest.mark.asyncio
    async def test_calls_all_services(self):
        ctx = MagicMock(project_key="p", current_focus="f", description="d", blockers=[])
        mock_ctx_svc = MagicMock()
        mock_ctx_svc.get_by_key = AsyncMock(return_value=ctx)
        mock_decision_svc = MagicMock()
        mock_decision_svc.list_all = AsyncMock(return_value=[])
        mock_learning_svc = MagicMock()
        mock_learning_svc.list_all = AsyncMock(return_value=[])
        mock_dream_svc = MagicMock()
        mock_dream_svc.killswitch_state = AsyncMock(
            return_value=KillswitchState(
                last_run_date=None,
                promote_enabled=False,
                promote_dry=False,
                reorg_enabled=False,
                reorg_dry=False,
                promote_clean_dry_nights=0,
                reorg_clean_dry_nights=0,
            )
        )
        mock_dream_svc.last_failure = AsyncMock(return_value=None)
        mock_feature_svc = MagicMock()
        mock_feature_svc.roadmap_alive = AsyncMock(return_value=[])
        mock_feature_svc.stale_pinned = AsyncMock(return_value=[])
        mock_brain_session_svc = _brain_session_service()

        mcp = FastMCP("test")
        register_session_tools(
            mcp,
            mock_ctx_svc,
            mock_decision_svc,
            mock_learning_svc,
            mock_dream_svc,
            mock_feature_svc,
            mock_brain_session_svc,
        )
        tool = await mcp.get_tool("brain_session_start")
        result = await tool.fn(project_key="p", client_key="client-1")

        mock_brain_session_svc.start.assert_awaited_once_with(
            project_key="p",
            client_key="client-1",
        )
        mock_ctx_svc.get_by_key.assert_called_once_with("p")
        mock_dream_svc.killswitch_state.assert_called_once()
        mock_dream_svc.last_failure.assert_called_once()
        mock_feature_svc.roadmap_alive.assert_called_once()
        mock_feature_svc.stale_pinned.assert_called_once()
        assert "### Killswitches" in result.briefing
        assert "### Focus" in result.briefing

    @pytest.mark.asyncio
    async def test_partial_failure_returns_degraded_briefing(self):
        """If any service call raises, gather(return_exceptions=True) absorbs
        the error, structlog warns, and the briefing still renders the rest."""
        ctx = MagicMock(project_key="p", current_focus="ship", description="d", blockers=[])
        mock_ctx_svc = MagicMock()
        mock_ctx_svc.get_by_key = AsyncMock(return_value=ctx)
        mock_decision_svc = MagicMock()
        mock_decision_svc.list_all = AsyncMock(return_value=[])
        mock_learning_svc = MagicMock()
        mock_learning_svc.list_all = AsyncMock(return_value=[])
        mock_dream_svc = MagicMock()
        mock_dream_svc.killswitch_state = AsyncMock(
            return_value=KillswitchState(
                last_run_date=None,
                promote_enabled=False,
                promote_dry=False,
                reorg_enabled=False,
                reorg_dry=False,
                promote_clean_dry_nights=0,
                reorg_clean_dry_nights=0,
            )
        )
        mock_dream_svc.last_failure = AsyncMock(return_value=None)
        mock_feature_svc = MagicMock()
        mock_feature_svc.roadmap_alive = AsyncMock(side_effect=RuntimeError("db down"))
        mock_feature_svc.stale_pinned = AsyncMock(return_value=[])

        mcp = FastMCP("test")
        register_session_tools(
            mcp,
            mock_ctx_svc,
            mock_decision_svc,
            mock_learning_svc,
            mock_dream_svc,
            mock_feature_svc,
            _brain_session_service(),
        )
        tool = await mcp.get_tool("brain_session_start")
        result = await tool.fn(project_key="p", client_key="client-1")  # MUST NOT raise

        assert "### Killswitches" in result.briefing
        assert "### Focus" in result.briefing
        assert "ship" in result.briefing

    @pytest.mark.asyncio
    async def test_killswitch_crash_renders_unavailable_not_no_activity(self):
        """When killswitch_state() raises, the section must render
        'status unavailable' — not the misleading 'no activity in 7d' anchor.
        """
        ctx = MagicMock(project_key="p", current_focus="ship", description="d", blockers=[])
        mock_ctx_svc = MagicMock()
        mock_ctx_svc.get_by_key = AsyncMock(return_value=ctx)
        mock_decision_svc = MagicMock()
        mock_decision_svc.list_all = AsyncMock(return_value=[])
        mock_learning_svc = MagicMock()
        mock_learning_svc.list_all = AsyncMock(return_value=[])
        mock_dream_svc = MagicMock()
        mock_dream_svc.killswitch_state = AsyncMock(side_effect=RuntimeError("pg flake"))
        mock_dream_svc.last_failure = AsyncMock(return_value=None)
        mock_feature_svc = MagicMock()
        mock_feature_svc.roadmap_alive = AsyncMock(return_value=[])
        mock_feature_svc.stale_pinned = AsyncMock(return_value=[])

        mcp = FastMCP("test")
        register_session_tools(
            mcp,
            mock_ctx_svc,
            mock_decision_svc,
            mock_learning_svc,
            mock_dream_svc,
            mock_feature_svc,
            _brain_session_service(),
        )
        tool = await mcp.get_tool("brain_session_start")
        result = await tool.fn(project_key="p", client_key="client-1")  # MUST NOT raise

        assert "status unavailable" in result.briefing
        assert "no dream pipeline activity in 7d" not in result.briefing


# ---------------------------------------------------------------------------
# Cross-project section (Spec C MVP β)
# ---------------------------------------------------------------------------


def _block() -> CrossProjectBlock:
    return CrossProjectBlock(
        domains=["ml", "memory"],
        entries=[
            CrossEntry(
                "red-shrik",
                "Decision",
                "embedding healthcheck pattern",
                datetime(2026, 4, 28, tzinfo=UTC),
            ),
            CrossEntry(
                "red-monitor",
                "Learning",
                "go-pubsub close channel race",
                datetime(2026, 4, 15, tzinfo=UTC),
            ),
        ],
    )


class TestCrossProjectSection:
    def test_section_renders_domains_and_entries(self):
        out = _section_cross_project(_block())
        assert out.startswith("### Cross-project (ml, memory)")
        assert "- [red-shrik] Decision · 2026-04-28 · embedding healthcheck pattern" in out
        assert "- [red-monitor] Learning · 2026-04-15 · go-pubsub close channel race" in out

    def test_section_empty_when_block_none(self):
        assert _section_cross_project(None) == ""

    def test_section_empty_when_no_entries(self):
        assert _section_cross_project(CrossProjectBlock(domains=["ml"], entries=[])) == ""

    def test_briefing_backward_compat_without_cross_block(self):
        out = _format_session_briefing(None, [], [], _no_activity_ks(), None, [], [])
        assert "Cross-project" not in out

    def test_briefing_includes_cross_section_before_drill_in(self):
        out = _format_session_briefing(
            None, [], [], _no_activity_ks(), None, [], [], cross_block=_block()
        )
        assert "### Cross-project (ml, memory)" in out
        assert out.index("### Cross-project") > out.index("### Focus")
        assert out.index("### Cross-project") < out.index("→ More:")


def _minimal_services():
    """Minimal mock services that satisfy all section renderers in brain_session_start."""
    ctx = MagicMock(project_key="p", current_focus="f", description=None, blockers=[])
    ctx_svc = MagicMock()
    ctx_svc.get_by_key = AsyncMock(return_value=ctx)
    decision_svc = MagicMock()
    decision_svc.list_all = AsyncMock(return_value=[])
    learning_svc = MagicMock()
    learning_svc.list_all = AsyncMock(return_value=[])
    dream_svc = MagicMock()
    dream_svc.killswitch_state = AsyncMock(return_value=_no_activity_ks())
    dream_svc.last_failure = AsyncMock(return_value=None)
    feature_svc = MagicMock()
    feature_svc.roadmap_alive = AsyncMock(return_value=[])
    feature_svc.stale_pinned = AsyncMock(return_value=[])
    return (
        ctx_svc,
        decision_svc,
        learning_svc,
        dream_svc,
        feature_svc,
        _brain_session_service(),
    )


class TestCrossProjectInTool:
    @pytest.mark.asyncio
    async def test_cross_svc_failure_degrades_to_no_section(self):
        cross_svc = AsyncMock()
        cross_svc.fetch_block.side_effect = RuntimeError("neo4j boom")
        settings = MagicMock(brain_dream_cross_project_enabled=True, graph_enabled=False)
        with patch("brain_v42.mcp.tools.session_tools.get_settings", return_value=settings):
            mcp = FastMCP("test")
            register_session_tools(
                mcp,
                *_minimal_services(),
                cross_project_svc=cross_svc,
            )
            tool = await mcp.get_tool("brain_session_start")
            result = await tool.fn(project_key="p", client_key="client-1")
        assert "Cross-project" not in result.briefing

    @pytest.mark.asyncio
    async def test_cross_svc_not_called_when_flag_off(self):
        cross_svc = AsyncMock()
        settings = MagicMock(brain_dream_cross_project_enabled=False, graph_enabled=False)
        with patch("brain_v42.mcp.tools.session_tools.get_settings", return_value=settings):
            mcp = FastMCP("test")
            register_session_tools(
                mcp,
                *_minimal_services(),
                cross_project_svc=cross_svc,
            )
            tool = await mcp.get_tool("brain_session_start")
            await tool.fn(project_key="p", client_key="client-1")
        cross_svc.fetch_block.assert_not_called()

    @pytest.mark.asyncio
    async def test_cross_svc_used_when_flag_on(self):
        cross_svc = AsyncMock()
        cross_svc.fetch_block.return_value = _block()
        settings = MagicMock(brain_dream_cross_project_enabled=True, graph_enabled=False)
        with patch("brain_v42.mcp.tools.session_tools.get_settings", return_value=settings):
            mcp = FastMCP("test")
            register_session_tools(
                mcp,
                *_minimal_services(),
                cross_project_svc=cross_svc,
            )
            tool = await mcp.get_tool("brain_session_start")
            result = await tool.fn(project_key="p", client_key="client-1")
        assert "### Cross-project (ml, memory)" in result.briefing


# ---------------------------------------------------------------------------
# Tickets section (spec §5 — actionnable, haute, graceful-degrade)
# ---------------------------------------------------------------------------


_SETTINGS_NO_CROSS = MagicMock(brain_dream_cross_project_enabled=False, graph_enabled=False)


class TestTicketsSection:
    """Section ### Tickets — actionnable, haute, graceful-degrade (spec §5)."""

    @pytest.mark.asyncio
    async def test_briefing_shows_tickets_section(self):
        from brain_v42.models.ticket import Ticket, TicketGroups, TicketKind, TicketStatus

        groups = TicketGroups(
            a_traiter=[
                Ticket(
                    kind=TicketKind.REQUEST,
                    title="exposer ndjson",
                    body="b",
                    from_project="red-shrik",
                    to_project="p",
                )
            ],
            a_confirmer=[
                Ticket(
                    kind=TicketKind.REQUEST,
                    title="autre",
                    body="b",
                    from_project="p",
                    to_project="red-data",
                    status=TicketStatus.RESOLVED,
                )
            ],
        )
        ticket_svc = MagicMock()
        ticket_svc.list_grouped = AsyncMock(return_value=groups)
        with patch(
            "brain_v42.mcp.tools.session_tools.get_settings", return_value=_SETTINGS_NO_CROSS
        ):
            mcp = FastMCP("test")
            register_session_tools(mcp, *_minimal_services(), ticket_svc=ticket_svc)
            tool = await mcp.get_tool("brain_session_start")
            result = await tool.fn(project_key="p", client_key="client-1")
        assert "### Tickets (1 à traiter · 1 à confirmer)" in result.briefing
        assert "exposer ndjson" in result.briefing
        assert "vérifie et confirme" in result.briefing

    @pytest.mark.asyncio
    async def test_no_tickets_no_section(self):
        from brain_v42.models.ticket import TicketGroups

        ticket_svc = MagicMock()
        ticket_svc.list_grouped = AsyncMock(return_value=TicketGroups())
        with patch(
            "brain_v42.mcp.tools.session_tools.get_settings", return_value=_SETTINGS_NO_CROSS
        ):
            mcp = FastMCP("test")
            register_session_tools(mcp, *_minimal_services(), ticket_svc=ticket_svc)
            tool = await mcp.get_tool("brain_session_start")
            result = await tool.fn(project_key="p", client_key="client-1")
        assert "### Tickets" not in result.briefing

    @pytest.mark.asyncio
    async def test_ticket_service_failure_degrades_gracefully(self):
        ticket_svc = MagicMock()
        ticket_svc.list_grouped = AsyncMock(side_effect=RuntimeError("db down"))
        with patch(
            "brain_v42.mcp.tools.session_tools.get_settings", return_value=_SETTINGS_NO_CROSS
        ):
            mcp = FastMCP("test")
            register_session_tools(mcp, *_minimal_services(), ticket_svc=ticket_svc)
            tool = await mcp.get_tool("brain_session_start")
            result = await tool.fn(project_key="p", client_key="client-1")
        assert "### Killswitches" in result.briefing
        assert "### Tickets" not in result.briefing

    @pytest.mark.asyncio
    async def test_no_ticket_svc_backward_compatible(self):
        with patch(
            "brain_v42.mcp.tools.session_tools.get_settings", return_value=_SETTINGS_NO_CROSS
        ):
            mcp = FastMCP("test")
            register_session_tools(mcp, *_minimal_services())
            tool = await mcp.get_tool("brain_session_start")
            result = await tool.fn(project_key="p", client_key="client-1")
        assert "### Tickets" not in result.briefing

    @pytest.mark.asyncio
    async def test_third_counter_shown_when_awaiting_group_non_empty(self):
        # spec 2026-08-03-ticket-briefing-fourth-quadrant §2.3, test 5.
        from brain_v42.models.ticket import Ticket, TicketGroups, TicketKind, TicketStatus

        groups = TicketGroups(
            awaiting_requester_confirmation=[
                Ticket(
                    kind=TicketKind.REQUEST,
                    title="exposer ndjson",
                    body="b",
                    from_project="red-writer",
                    to_project="p",
                    status=TicketStatus.WONTFIX,
                )
            ],
        )
        ticket_svc = MagicMock()
        ticket_svc.list_grouped = AsyncMock(return_value=groups)
        with patch(
            "brain_v42.mcp.tools.session_tools.get_settings", return_value=_SETTINGS_NO_CROSS
        ):
            mcp = FastMCP("test")
            register_session_tools(mcp, *_minimal_services(), ticket_svc=ticket_svc)
            tool = await mcp.get_tool("brain_session_start")
            result = await tool.fn(project_key="p", client_key="client-1")
        assert "### Tickets (0 à traiter · 0 à confirmer · 1 livrés à valider)" in result.briefing
        assert "exposer ndjson" not in result.briefing  # counted, not listed (spec §2.3)

    @pytest.mark.asyncio
    async def test_third_counter_omitted_when_awaiting_group_empty(self):
        from brain_v42.models.ticket import Ticket, TicketGroups, TicketKind

        groups = TicketGroups(
            a_traiter=[
                Ticket(
                    kind=TicketKind.REQUEST,
                    title="exposer ndjson",
                    body="b",
                    from_project="red-shrik",
                    to_project="p",
                )
            ],
        )
        ticket_svc = MagicMock()
        ticket_svc.list_grouped = AsyncMock(return_value=groups)
        with patch(
            "brain_v42.mcp.tools.session_tools.get_settings", return_value=_SETTINGS_NO_CROSS
        ):
            mcp = FastMCP("test")
            register_session_tools(mcp, *_minimal_services(), ticket_svc=ticket_svc)
            tool = await mcp.get_tool("brain_session_start")
            result = await tool.fn(project_key="p", client_key="client-1")
        assert "### Tickets (1 à traiter · 0 à confirmer)" in result.briefing
        assert "livrés à valider" not in result.briefing

    @pytest.mark.asyncio
    async def test_section_renders_when_only_awaiting_group_non_empty(self):
        # spec test 6: a_traiter/a_confirmer empty but the early return widened.
        from brain_v42.models.ticket import Ticket, TicketGroups, TicketKind, TicketStatus

        groups = TicketGroups(
            awaiting_requester_confirmation=[
                Ticket(
                    kind=TicketKind.REQUEST,
                    title="livré",
                    body="b",
                    from_project="red-shrik",
                    to_project="p",
                    status=TicketStatus.RESOLVED,
                )
            ],
        )
        ticket_svc = MagicMock()
        ticket_svc.list_grouped = AsyncMock(return_value=groups)
        with patch(
            "brain_v42.mcp.tools.session_tools.get_settings", return_value=_SETTINGS_NO_CROSS
        ):
            mcp = FastMCP("test")
            register_session_tools(mcp, *_minimal_services(), ticket_svc=ticket_svc)
            tool = await mcp.get_tool("brain_session_start")
            result = await tool.fn(project_key="p", client_key="client-1")
        assert "### Tickets" in result.briefing


# ---------------------------------------------------------------------------
# _section_technical_state — derived, never stored (ticket 87ac8b7a)
# ---------------------------------------------------------------------------


class TestTechnicalStateSection:
    """The schema revision is measured at briefing time, never carried forward.

    On 2026-08-04 both the focus and CLAUDE.md asserted migration 037 while the
    database had been on 039 for three days. Nothing reconciled the claim, so it
    survived every session that copied the paragraph forward. This section
    exists so the measured value is present at start/resume and the
    contradiction is visible immediately.
    """

    def test_renders_the_revision_actually_in_force(self) -> None:
        out = _section_technical_state("039")
        assert "### État technique" in out
        assert "039" in out

    def test_unavailable_is_stated_not_silently_omitted(self) -> None:
        """A failed read must not look like "nothing to report".

        Silence sends the reader back to the focus prose — which is exactly the
        stale claim this section is meant to contradict.
        """
        out = _section_technical_state(None, unavailable=True)
        assert out, "an unavailable read must still render the section"
        assert "indisponible" in out.lower()

    def test_omitted_when_not_requested(self) -> None:
        """Not asked for is distinct from asked-and-failed (killswitch precedent)."""
        assert _section_technical_state(None) == ""

    def test_briefing_carries_the_measured_revision(self) -> None:
        out = _format_session_briefing(
            None, [], [], _no_activity_ks(), None, [], [], schema_revision="039"
        )
        assert "### État technique" in out
        assert "039" in out

    def test_briefing_unchanged_when_revision_not_supplied(self) -> None:
        """Back-compat: the existing 7-arg shape must not grow a section."""
        out = _format_session_briefing(None, [], [], _no_activity_ks(), None, [], [])
        assert "### État technique" not in out


class TestFocusAgeLine:
    """How old is the prose below? `updated_at` cannot answer that.

    `project_contexts.updated_at` moves on any write to the row, counters
    included, so it dates the row and not the focus. Migration 040 adds
    `focus_updated_at`, written only when the focus text really changes, so a
    paragraph carried forward keeps ageing instead of looking freshly authored.
    """

    def test_renders_the_age_in_days(self) -> None:
        now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
        out = _section_technical_state(
            "040",
            focus_updated_at=now - timedelta(days=3, hours=2),
            focus_tracked=True,
            now=now,
        )
        assert "Focus écrit" in out
        assert "3j" in out

    def test_renders_hours_below_a_day(self) -> None:
        now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
        out = _section_technical_state(
            "040",
            focus_updated_at=now - timedelta(hours=5),
            focus_tracked=True,
            now=now,
        )
        assert "5h" in out

    def test_never_stamped_reads_as_unknown_not_as_fresh(self) -> None:
        """Migration 040 backfills nothing on purpose.

        Rows written before it carry NULL, which means "never measured". Showing
        that as "0j" would invent a fact, which is the disease being treated.
        """
        out = _section_technical_state("040", focus_updated_at=None, focus_tracked=True)
        assert "Focus écrit" in out
        assert "inconnu" in out.lower()

    def test_no_line_when_no_project_context_was_loaded(self) -> None:
        """No context is "not asked", distinct from "asked and never stamped"."""
        out = _section_technical_state("040", focus_updated_at=None, focus_tracked=False)
        assert "Focus écrit" not in out

    def test_age_renders_even_when_the_schema_read_failed(self) -> None:
        """The two facts are independent; one failing must not hide the other."""
        now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
        out = _section_technical_state(
            None,
            unavailable=True,
            focus_updated_at=now - timedelta(days=9),
            focus_tracked=True,
            now=now,
        )
        assert "indisponible" in out.lower()
        assert "9j" in out

    def test_briefing_dates_the_focus_from_the_project_context(self) -> None:
        ctx = SimpleNamespace(
            name="B",
            project_key="brain-v42",
            current_focus="prose",
            blockers=[],
            focus_updated_at=datetime.now(UTC) - timedelta(days=12),
        )
        out = _format_session_briefing(
            ctx, [], [], _no_activity_ks(), None, [], [], schema_revision="040"
        )
        assert "Focus écrit" in out
        assert "12j" in out
        assert out.index("Focus écrit") < out.index("### Focus")


class TestTechnicalStateWiring:
    """load_briefing measures the revision; a failed read degrades explicitly."""

    @pytest.mark.asyncio
    async def test_start_briefing_carries_the_measured_revision(self) -> None:
        schema_svc = MagicMock()
        schema_svc.current_revision = AsyncMock(return_value="039")
        with patch(
            "brain_v42.mcp.tools.session_tools.get_settings", return_value=_SETTINGS_NO_CROSS
        ):
            mcp = FastMCP("test")
            register_session_tools(mcp, *_minimal_services(), schema_state_svc=schema_svc)
            tool = await mcp.get_tool("brain_session_start")
            result = await tool.fn(project_key="p", client_key="client-1")
        assert "### État technique" in result.briefing
        assert "039" in result.briefing

    @pytest.mark.asyncio
    async def test_measured_revision_sits_above_the_focus_prose(self) -> None:
        """Placement is the point: the measurement must precede the narrative."""
        schema_svc = MagicMock()
        schema_svc.current_revision = AsyncMock(return_value="039")
        with patch(
            "brain_v42.mcp.tools.session_tools.get_settings", return_value=_SETTINGS_NO_CROSS
        ):
            mcp = FastMCP("test")
            register_session_tools(mcp, *_minimal_services(), schema_state_svc=schema_svc)
            tool = await mcp.get_tool("brain_session_start")
            result = await tool.fn(project_key="p", client_key="client-1")
        assert result.briefing.index("### État technique") < result.briefing.index("### Focus")

    @pytest.mark.asyncio
    async def test_failed_read_renders_unavailable_not_silence(self) -> None:
        schema_svc = MagicMock()
        schema_svc.current_revision = AsyncMock(side_effect=RuntimeError("db down"))
        with patch(
            "brain_v42.mcp.tools.session_tools.get_settings", return_value=_SETTINGS_NO_CROSS
        ):
            mcp = FastMCP("test")
            register_session_tools(mcp, *_minimal_services(), schema_state_svc=schema_svc)
            tool = await mcp.get_tool("brain_session_start")
            result = await tool.fn(project_key="p", client_key="client-1")
        assert "### État technique" in result.briefing
        assert "indisponible" in result.briefing.lower()

    @pytest.mark.asyncio
    async def test_no_service_wired_renders_no_schema_line(self) -> None:
        """Back-compat: callers that do not inject the service get no schema fact.

        Since migration 040 the section also carries the focus age, which is
        read off the project context and needs no service. So the section may
        still render — but it must stay silent about a revision nobody measured,
        and must not claim the read failed either.
        """
        with patch(
            "brain_v42.mcp.tools.session_tools.get_settings", return_value=_SETTINGS_NO_CROSS
        ):
            mcp = FastMCP("test")
            register_session_tools(mcp, *_minimal_services())
            tool = await mcp.get_tool("brain_session_start")
            result = await tool.fn(project_key="p", client_key="client-1")
        assert "Schéma" not in result.briefing
        assert "indisponible" not in result.briefing.lower()
