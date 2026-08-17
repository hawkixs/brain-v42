"""TDD tests for three graph-layer fixes (written RED before implementation).

Fix 1 — TIMEOUT NO-OP: _run/_run_counted/_run_read pass Query(text, timeout=...)
         instead of a raw string + timeout kwarg.
Fix 2 — NODE WRITE OUTCOME: _run returns 'ok'|'error'; upsert_node, delete_node,
         link_to_project, upsert_domain propagate it; graph_upsert_entity WARNs on
         'error'; InstrumentedGraphService uses outcome-based error counting.
Fix 3 — MATCH-MISS AS 'missing_node': _run_counted distinguishes 'missing_node'
         from 'matched'; graph_create_relation_logged WARNs on it; auto_linker
         buckets it under errors.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from brain_v42.services.graph_service import GraphService

# ── helpers ────────────────────────────────────────────────────────────────────

FIXED_UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TARGET_UUID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _make_driver_with_summary(
    relationships_created: int = 0,
    anchors: int = 1,
) -> tuple[MagicMock, AsyncMock, MagicMock]:
    """Return (driver, session, summary) mock triple.

    ``anchors`` controls the value returned in the ``RETURN count(*) AS anchors``
    clause that Fix 3 adds to relation queries:
      - anchors=0  → MATCH found no nodes → 'missing_node'
      - anchors>0  → anchor nodes present  → 'matched' or 'created'
    """
    driver = MagicMock()
    driver.verify_connectivity = AsyncMock()

    summary = MagicMock()
    summary.counters.relationships_created = relationships_created

    result = AsyncMock()
    result.consume = AsyncMock(return_value=summary)

    # Async iteration yields the RETURN clause row ({"anchors": <n>}).
    # _run_counted reads this row to distinguish 'missing_node' from 'matched'.
    _anchors = anchors

    async def _aiter(self_):  # type: ignore[no-untyped-def]
        yield {"anchors": _anchors}

    result.__aiter__ = lambda self_: _aiter(self_)

    session = AsyncMock()
    session.run = AsyncMock(return_value=result)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=None)
    driver.session = MagicMock(return_value=ctx)

    return driver, session, summary


# ==============================================================================
# Fix 1 — TIMEOUT NO-OP: Query object must be passed, not kwarg
# ==============================================================================


class TestTimeoutViaQueryObject:
    """session.run must receive a neo4j.Query carrying the timeout — NOT a
    bare string with a ``timeout`` kwarg, which neo4j-driver ≥ 5 silently
    fuses into the Cypher parameter dict instead of applying a network timeout.
    """

    @pytest.mark.asyncio
    async def test_run_passes_query_object(self) -> None:
        """_run sends a neo4j.Query to session.run, not a plain string."""
        from neo4j import Query

        driver, session, _ = _make_driver_with_summary()
        svc = GraphService(driver, timeout=3.0)

        await svc._run("MERGE (n:Test {id: $id})", {"id": "x"})

        assert session.run.await_count >= 1
        call_args = session.run.call_args
        first_arg = call_args[0][0]
        assert isinstance(first_arg, Query), (
            f"Expected neo4j.Query as first arg to session.run, got {type(first_arg)}"
        )
        assert first_arg.timeout == 3.0, f"Expected timeout=3.0 on Query, got {first_arg.timeout}"
        # No 'timeout' kwarg should leak into the call
        assert "timeout" not in call_args.kwargs, (
            "timeout should not appear as a kwarg to session.run"
        )

    @pytest.mark.asyncio
    async def test_run_counted_passes_query_object(self) -> None:
        """_run_counted sends a neo4j.Query to session.run."""
        from neo4j import Query

        driver, session, _ = _make_driver_with_summary(relationships_created=1)
        svc = GraphService(driver, timeout=7.0)

        await svc._run_counted("MERGE (a)-[r:X]->(b)", {})

        call_args = session.run.call_args
        first_arg = call_args[0][0]
        assert isinstance(first_arg, Query)
        assert first_arg.timeout == 7.0
        assert "timeout" not in call_args.kwargs

    @pytest.mark.asyncio
    async def test_run_read_passes_query_object(self) -> None:
        """_run_read sends a neo4j.Query to session.run."""
        from neo4j import Query

        driver, session, _ = _make_driver_with_summary()

        # Wire up async iteration for the read path
        async def _aiter(self_):  # type: ignore[no-untyped-def]
            yield {}

        result = AsyncMock()
        result.__aiter__ = lambda s: _aiter(s)
        session.run = AsyncMock(return_value=result)

        svc = GraphService(driver, timeout=2.5)

        await svc._run_read("MATCH (n) RETURN n", {})

        call_args = session.run.call_args
        first_arg = call_args[0][0]
        assert isinstance(first_arg, Query)
        assert first_arg.timeout == 2.5
        assert "timeout" not in call_args.kwargs

    @pytest.mark.asyncio
    async def test_timeout_is_not_in_params(self) -> None:
        """Regression: timeout MUST NOT appear in the params dict passed to Neo4j."""
        driver, session, _ = _make_driver_with_summary()
        svc = GraphService(driver, timeout=5.0)

        await svc._run("MERGE (n:Test {id: $id})", {"id": "hello"})

        call_args = session.run.call_args
        params_arg = call_args[0][1]  # second positional arg
        assert "timeout" not in params_arg, f"'timeout' must not be in params, got {params_arg}"


# ==============================================================================
# Fix 2 — NODE WRITE OUTCOME: _run returns 'ok'|'error'
# ==============================================================================


class TestRunReturnsOutcome:
    """_run must return 'ok' on success and 'error' on swallowed exception."""

    @pytest.mark.asyncio
    async def test_run_returns_ok_on_success(self) -> None:
        driver, session, _ = _make_driver_with_summary()
        svc = GraphService(driver)

        outcome = await svc._run("MERGE (n:Test {id: $id})", {"id": "x"})

        assert outcome == "ok"

    @pytest.mark.asyncio
    async def test_run_returns_error_on_exception(self) -> None:
        driver, session, _ = _make_driver_with_summary()
        session.run = AsyncMock(side_effect=Exception("neo4j down"))
        driver.verify_connectivity = AsyncMock(side_effect=Exception("still down"))
        svc = GraphService(driver)

        outcome = await svc._run("MERGE (n:Test {id: $id})", {"id": "x"})

        assert outcome == "error"


class TestPublicMethodsReturnOutcome:
    """upsert_node, delete_node, link_to_project, upsert_domain propagate _run outcome."""

    @pytest.mark.asyncio
    async def test_upsert_node_returns_ok(self) -> None:
        driver, session, _ = _make_driver_with_summary()
        svc = GraphService(driver)

        outcome = await svc.upsert_node("Decision", FIXED_UUID, {"title": "x"})

        assert outcome == "ok"

    @pytest.mark.asyncio
    async def test_upsert_node_returns_error(self) -> None:
        driver, session, _ = _make_driver_with_summary()
        session.run = AsyncMock(side_effect=Exception("down"))
        driver.verify_connectivity = AsyncMock(side_effect=Exception("down"))
        svc = GraphService(driver)

        outcome = await svc.upsert_node("Decision", FIXED_UUID, {"title": "x"})

        assert outcome == "error"

    @pytest.mark.asyncio
    async def test_delete_node_returns_ok(self) -> None:
        driver, session, _ = _make_driver_with_summary()
        svc = GraphService(driver)

        outcome = await svc.delete_node("Decision", FIXED_UUID)

        assert outcome == "ok"

    @pytest.mark.asyncio
    async def test_delete_node_returns_error(self) -> None:
        driver, session, _ = _make_driver_with_summary()
        session.run = AsyncMock(side_effect=Exception("down"))
        driver.verify_connectivity = AsyncMock(side_effect=Exception("down"))
        svc = GraphService(driver)

        outcome = await svc.delete_node("Decision", FIXED_UUID)

        assert outcome == "error"

    @pytest.mark.asyncio
    async def test_link_to_project_returns_ok(self) -> None:
        driver, session, _ = _make_driver_with_summary()
        svc = GraphService(driver)

        outcome = await svc.link_to_project(FIXED_UUID, "brain-v42")

        assert outcome == "ok"

    @pytest.mark.asyncio
    async def test_link_to_project_returns_error(self) -> None:
        driver, session, _ = _make_driver_with_summary()
        session.run = AsyncMock(side_effect=Exception("down"))
        driver.verify_connectivity = AsyncMock(side_effect=Exception("down"))
        svc = GraphService(driver)

        outcome = await svc.link_to_project(FIXED_UUID, "brain-v42")

        assert outcome == "error"

    @pytest.mark.asyncio
    async def test_link_to_project_returns_missing_node_when_anchor_absent(self) -> None:
        driver, _session, _ = _make_driver_with_summary(
            relationships_created=0,
            anchors=0,
        )
        svc = GraphService(driver)

        outcome = await svc.link_to_project(FIXED_UUID, "missing-project")

        assert outcome == "missing_node"

    @pytest.mark.asyncio
    async def test_upsert_domain_returns_ok_on_success_legacy(self) -> None:
        """upsert_domain returns 'ok' when _run succeeds (tri-state contract).

        Updated from the old bool contract (True/False) to Literal['ok','invalid_domain','error'].
        See TestUpsertDomainTriState for full contract coverage.
        """
        driver, session, _ = _make_driver_with_summary()
        svc = GraphService(driver)

        result = await svc.upsert_domain("infra")

        assert result == "ok"

    @pytest.mark.asyncio
    async def test_upsert_domain_returns_error_on_run_error_legacy(self) -> None:
        """upsert_domain returns 'error' (not False) when Neo4j write fails.

        Updated from the old bool contract where False was returned for both
        invalid names and Neo4j errors, making them indistinguishable to callers.
        See TestUpsertDomainTriState for full contract coverage.
        """
        driver, session, _ = _make_driver_with_summary()
        session.run = AsyncMock(side_effect=Exception("down"))
        driver.verify_connectivity = AsyncMock(side_effect=Exception("down"))
        svc = GraphService(driver)

        result = await svc.upsert_domain("infra")

        assert result == "error"


class TestGraphUpsertEntityWarnsOnNodeError:
    """graph_upsert_entity must emit a structured WARN when upsert_node returns 'error'."""

    @pytest.mark.asyncio
    async def test_warns_when_upsert_node_fails(self) -> None:
        from brain_v42.services.graph_helpers import graph_upsert_entity

        graph = MagicMock()
        # upsert_node returns 'error' (Neo4j write silently failed)
        graph.upsert_node = AsyncMock(return_value="error")
        graph.link_to_project = AsyncMock(return_value="ok")

        with structlog.testing.capture_logs() as logs:
            await graph_upsert_entity(graph, "Decision", FIXED_UUID, {"title": "x"})

        warnings = [e for e in logs if e.get("log_level") == "warning"]
        assert any(e["event"] == "graph_node_write_degraded" for e in warnings), (
            f"Expected 'graph_node_write_degraded' warning, got: {logs}"
        )

    @pytest.mark.asyncio
    async def test_no_warn_on_ok(self) -> None:
        from brain_v42.services.graph_helpers import graph_upsert_entity

        graph = MagicMock()
        graph.upsert_node = AsyncMock(return_value="ok")
        graph.link_to_project = AsyncMock(return_value="ok")

        with structlog.testing.capture_logs() as logs:
            await graph_upsert_entity(
                graph, "Decision", FIXED_UUID, {"title": "x"}, project_key="brain-v42"
            )

        warnings = [e for e in logs if e.get("log_level") == "warning"]
        assert not any(e["event"] == "graph_node_write_degraded" for e in warnings)

    @pytest.mark.asyncio
    async def test_includes_entity_context_in_warn(self) -> None:
        from brain_v42.services.graph_helpers import graph_upsert_entity

        graph = MagicMock()
        graph.upsert_node = AsyncMock(return_value="error")
        graph.link_to_project = AsyncMock(return_value="ok")

        with structlog.testing.capture_logs() as logs:
            await graph_upsert_entity(graph, "Learning", FIXED_UUID, {"title": "x"})

        warnings = [e for e in logs if e.get("log_level") == "warning"]
        node_warn = next((e for e in warnings if e["event"] == "graph_node_write_degraded"), None)
        assert node_warn is not None
        assert node_warn.get("entity_type") == "Learning"
        assert node_warn.get("entity_id") == str(FIXED_UUID)


class TestInstrumentedGraphServiceOutcome:
    """InstrumentedGraphService must propagate 'ok'/'error' outcomes and count errors."""

    def _make_collector(self) -> MagicMock:
        collector = MagicMock()
        collector.record_graph_query = MagicMock()
        return collector

    @pytest.mark.asyncio
    async def test_upsert_node_propagates_ok(self) -> None:
        from brain_v42.metrics.instrument import InstrumentedGraphService

        inner = MagicMock()
        inner.upsert_node = AsyncMock(return_value="ok")
        collector = self._make_collector()
        svc = InstrumentedGraphService(inner, collector)

        result = await svc.upsert_node("Decision", FIXED_UUID, {})

        assert result == "ok"
        collector.record_graph_query.assert_called_once()
        _, kwargs = collector.record_graph_query.call_args
        assert kwargs.get("error") is False

    @pytest.mark.asyncio
    async def test_upsert_node_propagates_error_and_counts(self) -> None:
        from brain_v42.metrics.instrument import InstrumentedGraphService

        inner = MagicMock()
        inner.upsert_node = AsyncMock(return_value="error")
        collector = self._make_collector()
        svc = InstrumentedGraphService(inner, collector)

        result = await svc.upsert_node("Decision", FIXED_UUID, {})

        assert result == "error"
        _, kwargs = collector.record_graph_query.call_args
        assert kwargs.get("error") is True

    @pytest.mark.asyncio
    async def test_delete_node_propagates_error_and_counts(self) -> None:
        from brain_v42.metrics.instrument import InstrumentedGraphService

        inner = MagicMock()
        inner.delete_node = AsyncMock(return_value="error")
        collector = self._make_collector()
        svc = InstrumentedGraphService(inner, collector)

        result = await svc.delete_node("Decision", FIXED_UUID)

        assert result == "error"
        _, kwargs = collector.record_graph_query.call_args
        assert kwargs.get("error") is True

    @pytest.mark.asyncio
    async def test_link_to_project_propagates_error_and_counts(self) -> None:
        from brain_v42.metrics.instrument import InstrumentedGraphService

        inner = MagicMock()
        inner.link_to_project = AsyncMock(return_value="error")
        collector = self._make_collector()
        svc = InstrumentedGraphService(inner, collector)

        result = await svc.link_to_project(FIXED_UUID, "brain-v42")

        assert result == "error"
        _, kwargs = collector.record_graph_query.call_args
        assert kwargs.get("error") is True


# ==============================================================================
# Fix 3 — MATCH-MISS AS 'missing_node'
# ==============================================================================


class TestMissingNodeOutcome:
    """_run_counted must return 'missing_node' when the MERGE is a no-op
    because anchor nodes were absent (RETURN anchors==0 AND relationships_created==0).

    Fix 3 adds ``RETURN count(<anchor>) AS anchors`` to relation queries so
    _run_counted can distinguish the two zero-relationships_created cases:
      anchors=0 → MATCH found no nodes   → 'missing_node'
      anchors>0 → edge already existed   → 'matched'
    """

    @pytest.mark.asyncio
    async def test_missing_node_when_no_nodes_matched_and_no_rel_created(self) -> None:
        """Create a relation where both anchor nodes are absent:
        MATCH returns 0 rows, MERGE is never reached → anchors==0,
        relationships_created==0 → outcome must be 'missing_node', not 'matched'.
        """
        driver, session, summary = _make_driver_with_summary(
            relationships_created=0,
            anchors=0,
        )
        svc = GraphService(driver)

        outcome = await svc.create_relation(FIXED_UUID, TARGET_UUID, "RELATED_TO")

        assert outcome == "missing_node", (
            f"Expected 'missing_node' when anchor nodes absent, got '{outcome}'"
        )

    @pytest.mark.asyncio
    async def test_matched_when_nodes_present_but_rel_already_existed(self) -> None:
        """MERGE matched an existing edge: anchors>0, relationships_created==0."""
        driver, session, summary = _make_driver_with_summary(
            relationships_created=0,
            anchors=1,
        )
        svc = GraphService(driver)

        outcome = await svc.create_relation(FIXED_UUID, TARGET_UUID, "RELATED_TO")

        assert outcome == "matched"

    @pytest.mark.asyncio
    async def test_created_when_new_edge_inserted(self) -> None:
        """MERGE created a new edge: relationships_created==1."""
        driver, session, summary = _make_driver_with_summary(
            relationships_created=1,
            anchors=1,
        )
        svc = GraphService(driver)

        outcome = await svc.create_relation(FIXED_UUID, TARGET_UUID, "RELATED_TO")

        assert outcome == "created"

    @pytest.mark.asyncio
    async def test_link_entity_to_domain_returns_missing_node(self) -> None:
        """link_entity_to_domain also uses _run_counted and must propagate 'missing_node'."""
        driver, session, summary = _make_driver_with_summary(
            relationships_created=0,
            anchors=0,
        )
        svc = GraphService(driver)

        outcome = await svc.link_entity_to_domain(FIXED_UUID, "ml")

        assert outcome == "missing_node"


class TestMissingNodeInRelationType:
    """RelationWriteOutcome type alias must include 'missing_node'."""

    def test_missing_node_is_valid_outcome_literal(self) -> None:
        # Validate that 'missing_node' is a valid value for RelationWriteOutcome.
        # mypy checks this statically; the runtime check guards against future
        # Literal narrowing that might silently drop the value.
        from brain_v42.services.graph_service import RelationWriteOutcome as _RWO

        valid = "missing_node"
        assert valid in ("created", "matched", "missing_node", "error")
        # Also check the Literal was extended — the __args__ introspection works
        # on Literal types at runtime via typing.get_args.
        import typing

        args = typing.get_args(_RWO)
        assert "missing_node" in args, f"'missing_node' not in RelationWriteOutcome args: {args}"


class TestGraphCreateRelationLoggedOnMissingNode:
    """graph_create_relation_logged must WARN on 'missing_node' outcome."""

    @pytest.mark.asyncio
    async def test_warns_on_missing_node(self) -> None:
        from brain_v42.services.graph_helpers import graph_create_relation_logged

        graph = MagicMock()
        graph.create_relation = AsyncMock(return_value="missing_node")

        with structlog.testing.capture_logs() as logs:
            outcome = await graph_create_relation_logged(
                graph, FIXED_UUID, TARGET_UUID, "RELATED_TO", entity_type="Decision"
            )

        assert outcome == "missing_node"
        warnings = [e for e in logs if e.get("log_level") == "warning"]
        assert any(e["event"] == "graph_relation_missing_node" for e in warnings), (
            f"Expected 'graph_relation_missing_node' warning, got: {logs}"
        )

    @pytest.mark.asyncio
    async def test_missing_node_warn_includes_context(self) -> None:
        from brain_v42.services.graph_helpers import graph_create_relation_logged

        graph = MagicMock()
        graph.create_relation = AsyncMock(return_value="missing_node")

        with structlog.testing.capture_logs() as logs:
            await graph_create_relation_logged(
                graph,
                FIXED_UUID,
                TARGET_UUID,
                "MERGED_INTO",
                entity_type="Learning",
                entity_id=str(FIXED_UUID),
            )

        warnings = [e for e in logs if e.get("log_level") == "warning"]
        w = next((e for e in warnings if e["event"] == "graph_relation_missing_node"), None)
        assert w is not None
        assert w["rel_type"] == "MERGED_INTO"

    @pytest.mark.asyncio
    async def test_no_warn_on_matched(self) -> None:
        from brain_v42.services.graph_helpers import graph_create_relation_logged

        graph = MagicMock()
        graph.create_relation = AsyncMock(return_value="matched")

        with structlog.testing.capture_logs() as logs:
            outcome = await graph_create_relation_logged(
                graph, FIXED_UUID, TARGET_UUID, "RELATED_TO"
            )

        assert outcome == "matched"
        warnings = [e for e in logs if e.get("log_level") == "warning"]
        assert not any(e.get("event") == "graph_relation_missing_node" for e in warnings)


class TestAutoLinkerMissingNodeBucketing:
    """AutoLinker must put 'missing_node' outcomes in the errors bucket."""

    @pytest.mark.asyncio
    async def test_missing_node_goes_to_errors(self) -> None:
        from unittest.mock import patch

        from brain_v42.services.auto_linker import AutoLinker

        graph = MagicMock()
        graph.create_relation = AsyncMock(return_value="missing_node")

        session_factory = MagicMock()
        linker = AutoLinker(session_factory=session_factory, graph=graph)

        # Patch _find_similar to return one candidate above threshold
        candidate = {
            "id": TARGET_UUID,
            "entity_type": "Learning",
            "similarity": 0.9,
        }

        with patch.object(linker, "_find_similar", AsyncMock(return_value=[candidate])):
            result = await linker.auto_link(
                entity_type="Decision",
                entity_id=FIXED_UUID,
                embedding=[0.1] * 10,
                threshold=0.6,
                max_links=3,
            )

        assert len(result.errors) == 1
        assert result.errors[0]["id"] == TARGET_UUID
        assert result.created == []
        assert result.matched == []

    @pytest.mark.asyncio
    async def test_missing_node_reason_in_errors(self) -> None:
        """Error entry for missing_node must have distinguishable reason."""
        from unittest.mock import patch

        from brain_v42.services.auto_linker import AutoLinker

        graph = MagicMock()
        graph.create_relation = AsyncMock(return_value="missing_node")

        linker = AutoLinker(session_factory=MagicMock(), graph=graph)
        candidate = {"id": TARGET_UUID, "entity_type": "Learning", "similarity": 0.95}

        with patch.object(linker, "_find_similar", AsyncMock(return_value=[candidate])):
            result = await linker.auto_link("Decision", FIXED_UUID, [0.1] * 10)

        assert result.errors[0].get("reason") == "missing_node"


# ==============================================================================
# BLOCKER — upsert_domain tri-state return
# ==============================================================================


class TestUpsertDomainTriState:
    """upsert_domain must return Literal['ok','invalid_domain','error'] instead of bool.

    OLD contract: bool (True=ok, False=either invalid-name OR neo4j-error)
    NEW contract: 'ok' | 'invalid_domain' | 'error'
      - 'invalid_domain' — name not in ALLOWED_DOMAINS (no write, docstring reserved)
      - 'error'          — _run failed (Neo4j down, timeout, etc.)
      - 'ok'             — Domain node upserted/matched successfully
    """

    @pytest.mark.asyncio
    async def test_upsert_domain_returns_ok_on_success(self) -> None:
        """upsert_domain returns 'ok' (not True) when _run succeeds."""
        driver, session, _ = _make_driver_with_summary()
        svc = GraphService(driver)

        result = await svc.upsert_domain("infra")

        assert result == "ok", (
            f"Expected 'ok' from upsert_domain on success, got {result!r}. "
            "The tri-state contract replaces the old bool."
        )

    @pytest.mark.asyncio
    async def test_upsert_domain_returns_invalid_domain_on_unknown_name(self) -> None:
        """upsert_domain returns 'invalid_domain' when name not in ALLOWED_DOMAINS (no _run call)."""
        driver, session, _ = _make_driver_with_summary()
        svc = GraphService(driver)

        result = await svc.upsert_domain("not-a-domain")

        assert result == "invalid_domain", (
            f"Expected 'invalid_domain' for unknown name, got {result!r}"
        )
        session.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_upsert_domain_returns_error_on_neo4j_failure(self) -> None:
        """BLOCKER: upsert_domain returns 'error' (not False) when Neo4j write fails.

        With the old bool contract, upsert_domain returned False on neo4j errors,
        causing brain_assign_domain to map it to 'invalid_domain' — telling the LLM
        the domain name was bad instead of signaling an infra failure.
        """
        driver, session, _ = _make_driver_with_summary()
        session.run = AsyncMock(side_effect=Exception("neo4j down"))
        driver.verify_connectivity = AsyncMock(side_effect=Exception("still down"))
        svc = GraphService(driver)

        result = await svc.upsert_domain("infra")

        assert result == "error", (
            f"Expected 'error' from upsert_domain when Neo4j is down, got {result!r}. "
            "Old contract returned False (bool) which was indistinguishable from invalid name."
        )


# ==============================================================================
# MAJOR 1 — InstrumentedGraphService.create_relation error counting
# ==============================================================================


class TestInstrumentedCreateRelationErrorCounting:
    """InstrumentedGraphService.create_relation must count 'error' outcome in metrics.

    The 'error' outcome from create_relation (RelationWriteOutcome) must trigger
    record_graph_query(error=True). 'missing_node' is intentionally NOT counted
    as an infra metric error — it signals structural drift (missing anchor nodes)
    which is a data quality issue, not an infra failure. It is surfaced by
    WARN logs in graph_create_relation_logged instead.
    """

    def _make_collector(self) -> MagicMock:
        collector = MagicMock()
        collector.record_graph_query = MagicMock()
        return collector

    @pytest.mark.asyncio
    async def test_create_relation_error_outcome_counts_as_metric_error(self) -> None:
        """create_relation returning 'error' must set error=True in record_graph_query."""
        from brain_v42.metrics.instrument import InstrumentedGraphService

        inner = MagicMock()
        inner.create_relation = AsyncMock(return_value="error")
        collector = self._make_collector()
        svc = InstrumentedGraphService(inner, collector)

        result = await svc.create_relation(FIXED_UUID, TARGET_UUID, "RELATED_TO")

        assert result == "error"
        _, kwargs = collector.record_graph_query.call_args
        assert kwargs.get("error") is True, (
            "Expected error=True in record_graph_query when create_relation returns 'error'. "
            "graph.total_errors was dead for RELATION writes before this fix."
        )

    @pytest.mark.asyncio
    async def test_create_relation_missing_node_does_not_count_as_metric_error(self) -> None:
        """'missing_node' outcome must NOT set error=True — it's structural drift, not infra failure.

        Explicit design decision: 'missing_node' indicates anchor nodes absent in the graph
        (data quality / write-through drift), not a Neo4j infra failure. It is surfaced
        via WARN logs (graph_create_relation_logged) rather than infra error metrics.
        """
        from brain_v42.metrics.instrument import InstrumentedGraphService

        inner = MagicMock()
        inner.create_relation = AsyncMock(return_value="missing_node")
        collector = self._make_collector()
        svc = InstrumentedGraphService(inner, collector)

        result = await svc.create_relation(FIXED_UUID, TARGET_UUID, "RELATED_TO")

        assert result == "missing_node"
        _, kwargs = collector.record_graph_query.call_args
        assert kwargs.get("error") is False, (
            "Expected error=False in record_graph_query for 'missing_node'. "
            "Structural drift is signaled via WARN log, not infra metric."
        )

    @pytest.mark.asyncio
    async def test_create_relation_created_outcome_does_not_count_as_error(self) -> None:
        """'created' outcome must set error=False."""
        from brain_v42.metrics.instrument import InstrumentedGraphService

        inner = MagicMock()
        inner.create_relation = AsyncMock(return_value="created")
        collector = self._make_collector()
        svc = InstrumentedGraphService(inner, collector)

        result = await svc.create_relation(FIXED_UUID, TARGET_UUID, "RELATED_TO")

        assert result == "created"
        _, kwargs = collector.record_graph_query.call_args
        assert kwargs.get("error") is False


# ==============================================================================
# MAJOR 2 — delete_relation propagates outcome
# ==============================================================================


class TestDeleteRelationPropagatesOutcome:
    """delete_relation must return NodeWriteOutcome ('ok'|'error') instead of None.

    Currently delete_relation calls _run but discards the outcome, returning None.
    Neo4j failures are 100% silent, inconsistent with the contract of upsert_node,
    delete_node, and link_to_project which all propagate the _run outcome.
    """

    @pytest.mark.asyncio
    async def test_delete_relation_returns_ok_on_success(self) -> None:
        """delete_relation returns 'ok' when _run succeeds."""
        driver, session, _ = _make_driver_with_summary()
        svc = GraphService(driver)

        result = await svc.delete_relation(FIXED_UUID, TARGET_UUID, "RELATED_TO")

        assert result == "ok", (
            f"Expected 'ok' from delete_relation on success, got {result!r}. "
            "delete_relation previously discarded the _run outcome and returned None."
        )

    @pytest.mark.asyncio
    async def test_delete_relation_returns_error_on_failure(self) -> None:
        """delete_relation returns 'error' when Neo4j write fails."""
        driver, session, _ = _make_driver_with_summary()
        session.run = AsyncMock(side_effect=Exception("neo4j down"))
        driver.verify_connectivity = AsyncMock(side_effect=Exception("still down"))
        svc = GraphService(driver)

        result = await svc.delete_relation(FIXED_UUID, TARGET_UUID, "RELATED_TO")

        assert result == "error", (
            f"Expected 'error' from delete_relation when Neo4j is down, got {result!r}. "
            "delete_relation was silently discarding the _run outcome."
        )


# ==============================================================================
# MINOR 1 — graph_helpers ignores outcomes
# ==============================================================================


class TestGraphHelpersOutcomeWarnings:
    """graph_upsert_entity and graph_delete_entity must WARN on 'error' outcomes.

    graph_upsert_entity: link_to_project outcome is currently ignored. A returned
    'error' should emit graph_node_write_degraded with (project_key, entity_id).

    graph_delete_entity: delete_node outcome is currently ignored. A returned
    'error' should emit graph_node_write_degraded with (entity_type, entity_id).
    """

    @pytest.mark.asyncio
    async def test_graph_upsert_entity_warns_on_link_to_project_error(self) -> None:
        """graph_upsert_entity WARNs when link_to_project returns 'error'."""
        from brain_v42.services.graph_helpers import graph_upsert_entity

        graph = MagicMock()
        graph.upsert_node = AsyncMock(return_value="ok")
        graph.link_to_project = AsyncMock(return_value="error")

        with structlog.testing.capture_logs() as logs:
            await graph_upsert_entity(
                graph,
                "Decision",
                FIXED_UUID,
                {"title": "x"},
                project_key="brain-v42",
            )

        warnings = [e for e in logs if e.get("log_level") == "warning"]
        assert any(e["event"] == "graph_node_write_degraded" for e in warnings), (
            f"Expected 'graph_node_write_degraded' warning when link_to_project fails, got: {logs}"
        )

    @pytest.mark.asyncio
    async def test_graph_upsert_entity_link_to_project_error_includes_context(self) -> None:
        """The WARN for link_to_project error includes project_key and entity_id context."""
        from brain_v42.services.graph_helpers import graph_upsert_entity

        graph = MagicMock()
        graph.upsert_node = AsyncMock(return_value="ok")
        graph.link_to_project = AsyncMock(return_value="error")

        with structlog.testing.capture_logs() as logs:
            await graph_upsert_entity(
                graph,
                "Decision",
                FIXED_UUID,
                {"title": "x"},
                project_key="brain-v42",
            )

        warnings = [e for e in logs if e.get("log_level") == "warning"]
        # Find the link_to_project-specific warning (has project_key context)
        link_warn = next(
            (e for e in warnings if e.get("project_key") == "brain-v42"),
            None,
        )
        assert link_warn is not None, (
            f"Expected warning with project_key='brain-v42', got warnings: {warnings}"
        )
        assert str(FIXED_UUID) in str(link_warn.get("entity_id", "")), (
            f"Expected entity_id in warning, got: {link_warn}"
        )

    @pytest.mark.asyncio
    async def test_graph_upsert_entity_no_link_warn_on_ok(self) -> None:
        """No link_to_project warning when both upsert_node and link_to_project succeed."""
        from brain_v42.services.graph_helpers import graph_upsert_entity

        graph = MagicMock()
        graph.upsert_node = AsyncMock(return_value="ok")
        graph.link_to_project = AsyncMock(return_value="ok")

        with structlog.testing.capture_logs() as logs:
            await graph_upsert_entity(
                graph,
                "Decision",
                FIXED_UUID,
                {"title": "x"},
                project_key="brain-v42",
            )

        warnings = [e for e in logs if e.get("log_level") == "warning"]
        assert not any(e.get("project_key") == "brain-v42" for e in warnings), (
            f"Unexpected warnings when link_to_project succeeds: {warnings}"
        )

    @pytest.mark.asyncio
    async def test_graph_delete_entity_warns_on_delete_node_error(self) -> None:
        """graph_delete_entity WARNs when delete_node returns 'error'."""
        from brain_v42.services.graph_helpers import graph_delete_entity

        graph = MagicMock()
        graph.delete_node = AsyncMock(return_value="error")

        with structlog.testing.capture_logs() as logs:
            await graph_delete_entity(graph, "Decision", FIXED_UUID)

        warnings = [e for e in logs if e.get("log_level") == "warning"]
        assert any(e["event"] == "graph_node_write_degraded" for e in warnings), (
            f"Expected 'graph_node_write_degraded' warning when delete_node fails, got: {logs}"
        )

    @pytest.mark.asyncio
    async def test_graph_delete_entity_delete_node_error_includes_context(self) -> None:
        """The WARN for delete_node error includes entity_type and entity_id."""
        from brain_v42.services.graph_helpers import graph_delete_entity

        graph = MagicMock()
        graph.delete_node = AsyncMock(return_value="error")

        with structlog.testing.capture_logs() as logs:
            await graph_delete_entity(graph, "Learning", FIXED_UUID)

        warnings = [e for e in logs if e.get("log_level") == "warning"]
        node_warn = next(
            (e for e in warnings if e["event"] == "graph_node_write_degraded"),
            None,
        )
        assert node_warn is not None
        assert node_warn.get("entity_type") == "Learning"
        assert node_warn.get("entity_id") == str(FIXED_UUID)

    @pytest.mark.asyncio
    async def test_graph_delete_entity_no_warn_on_ok(self) -> None:
        """No warning when delete_node returns 'ok'."""
        from brain_v42.services.graph_helpers import graph_delete_entity

        graph = MagicMock()
        graph.delete_node = AsyncMock(return_value="ok")

        with structlog.testing.capture_logs() as logs:
            await graph_delete_entity(graph, "Decision", FIXED_UUID)

        warnings = [e for e in logs if e.get("log_level") == "warning"]
        assert not any(e["event"] == "graph_node_write_degraded" for e in warnings)
