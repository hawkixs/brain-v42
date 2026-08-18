"""Unit tests for graph_helpers standalone functions."""

from __future__ import annotations

import inspect
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from brain_v42.mcp.dream_project_authorization import (
    DreamProjectAudit,
    DreamProjectAuthorizationError,
    DreamProjectScope,
)
from brain_v42.services.graph_helpers import (
    auto_link_if_enabled,
    graph_create_relation_logged,
    graph_delete_entity,
    graph_upsert_entity,
    link_artifact_if_enabled,
)
from brain_v42.services.link_result import LinkJobResult

FIXED_UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")
REL_UUID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
FAKE_EMBEDDING = [0.1] * 1536


# ---------------------------------------------------------------------------
# graph_upsert_entity
# ---------------------------------------------------------------------------


class TestGraphUpsertEntity:
    async def test_noop_when_graph_is_none(self) -> None:
        await graph_upsert_entity(None, "Decision", FIXED_UUID, {"title": "x"})

    async def test_upserts_node(self) -> None:
        graph = MagicMock()
        graph.upsert_node = AsyncMock()
        graph.link_to_project = AsyncMock()
        graph.create_relation = AsyncMock()

        await graph_upsert_entity(graph, "Decision", FIXED_UUID, {"title": "x"}, project_key="pk")

        graph.upsert_node.assert_awaited_once_with("Decision", FIXED_UUID, {"title": "x"})
        graph.link_to_project.assert_awaited_once_with(FIXED_UUID, "pk")

    async def test_skips_link_to_project_when_no_key(self) -> None:
        graph = MagicMock()
        graph.upsert_node = AsyncMock()
        graph.link_to_project = AsyncMock()
        graph.create_relation = AsyncMock()

        await graph_upsert_entity(graph, "Decision", FIXED_UUID, {"title": "x"})

        graph.upsert_node.assert_awaited_once()
        graph.link_to_project.assert_not_awaited()

    async def test_creates_relations(self) -> None:
        graph = MagicMock()
        graph.upsert_node = AsyncMock()
        graph.link_to_project = AsyncMock()
        graph.create_relation = AsyncMock()

        rels = [{"id": str(REL_UUID), "type": "MOTIVATED_BY"}]
        await graph_upsert_entity(graph, "Decision", FIXED_UUID, {"title": "x"}, related_to=rels)

        graph.create_relation.assert_awaited_once_with(FIXED_UUID, REL_UUID, "MOTIVATED_BY")

    async def test_exception_is_swallowed(self) -> None:
        graph = MagicMock()
        graph.upsert_node = AsyncMock(side_effect=RuntimeError("neo4j down"))
        graph.link_to_project = AsyncMock()

        # Should not raise
        await graph_upsert_entity(graph, "Decision", FIXED_UUID, {"title": "x"}, project_key="pk")

    @pytest.mark.parametrize(
        "relation",
        [
            {},
            {"id": "not-a-uuid", "type": "RELATED_TO"},
        ],
    )
    async def test_admin_malformed_related_to_keeps_historical_degradation(
        self,
        relation: dict,
    ) -> None:
        graph = MagicMock()
        graph.upsert_node = AsyncMock(return_value="ok")
        graph.link_to_project = AsyncMock(return_value="ok")
        graph.create_relation = AsyncMock(return_value="created")

        with structlog.testing.capture_logs() as logs:
            try:
                await graph_upsert_entity(
                    graph,
                    "Decision",
                    FIXED_UUID,
                    {"title": "x"},
                    related_to=[relation],
                )
            except Exception as exc:  # noqa: BLE001 - convert regression to assertion failure
                pytest.fail(f"admin malformed relation escaped degradation: {type(exc).__name__}")

        errors = [entry for entry in logs if entry["log_level"] == "error"]
        assert len(errors) == 1
        assert errors[0]["event"] == "graph_write_failed"
        assert errors[0]["entity_id"] == str(FIXED_UUID)
        graph.create_relation.assert_not_awaited()

    async def test_warns_when_related_relation_degrades(self) -> None:
        """A related_to relation that Neo4j reports as failed ('error' outcome,
        no exception) must surface a structured WARN, not vanish silently."""
        graph = MagicMock()
        graph.upsert_node = AsyncMock()
        graph.link_to_project = AsyncMock()
        graph.create_relation = AsyncMock(return_value="error")

        rels = [{"id": str(REL_UUID), "type": "MOTIVATED_BY"}]
        with structlog.testing.capture_logs() as logs:
            await graph_upsert_entity(
                graph, "Decision", FIXED_UUID, {"title": "x"}, related_to=rels
            )

        warnings = [e for e in logs if e["log_level"] == "warning"]
        assert any(e["event"] == "graph_relation_write_degraded" for e in warnings), logs

    async def test_durable_related_relation_failure_propagates(self) -> None:
        graph = MagicMock()
        graph.requires_durable_write_success = True
        graph.upsert_node = AsyncMock(return_value="ok")
        graph.create_relation = AsyncMock(side_effect=RuntimeError("ledger unavailable"))

        with pytest.raises(RuntimeError, match="ledger unavailable"):
            await graph_upsert_entity(
                graph,
                "Decision",
                FIXED_UUID,
                {"title": "x"},
                related_to=[{"id": str(REL_UUID), "type": "MOTIVATED_BY"}],
            )


# ---------------------------------------------------------------------------
# graph_create_relation_logged
# ---------------------------------------------------------------------------


class TestGraphCreateRelationLogged:
    async def test_returns_none_when_graph_is_none(self) -> None:
        assert await graph_create_relation_logged(None, FIXED_UUID, REL_UUID, "MERGED_INTO") is None

    async def test_returns_outcome_without_warning_on_success(self) -> None:
        graph = MagicMock()
        graph.create_relation = AsyncMock(return_value="created")

        with structlog.testing.capture_logs() as logs:
            outcome = await graph_create_relation_logged(graph, FIXED_UUID, REL_UUID, "MERGED_INTO")

        assert outcome == "created"
        graph.create_relation.assert_awaited_once_with(FIXED_UUID, REL_UUID, "MERGED_INTO")
        assert [e for e in logs if e["log_level"] in ("warning", "error")] == []

    async def test_warns_on_error_outcome(self) -> None:
        graph = MagicMock()
        graph.create_relation = AsyncMock(return_value="error")

        with structlog.testing.capture_logs() as logs:
            outcome = await graph_create_relation_logged(
                graph, FIXED_UUID, REL_UUID, "MERGED_INTO", entity_type="Learning"
            )

        assert outcome == "error"
        warnings = [e for e in logs if e["log_level"] == "warning"]
        assert len(warnings) == 1
        assert warnings[0]["event"] == "graph_relation_write_degraded"
        assert warnings[0]["rel_type"] == "MERGED_INTO"
        assert warnings[0]["entity_type"] == "Learning"

    async def test_exception_is_swallowed_and_logged_as_error(self) -> None:
        graph = MagicMock()
        graph.create_relation = AsyncMock(side_effect=RuntimeError("neo4j down"))

        with structlog.testing.capture_logs() as logs:
            outcome = await graph_create_relation_logged(graph, FIXED_UUID, REL_UUID, "SUPERSEDES")

        # A degraded graph must never break the PG path → coerce to "error", no raise.
        assert outcome == "error"
        assert any(
            e["event"] == "graph_relation_write_failed" for e in logs if e["log_level"] == "error"
        )

    async def test_durable_relation_exception_is_not_coerced_to_success(self) -> None:
        graph = MagicMock()
        graph.requires_durable_write_success = True
        graph.create_relation = AsyncMock(side_effect=RuntimeError("ledger unavailable"))

        with pytest.raises(RuntimeError, match="ledger unavailable"):
            await graph_create_relation_logged(
                graph,
                FIXED_UUID,
                REL_UUID,
                "MOTIVATED_BY",
            )

    async def test_scoped_write_revalidates_pair_immediately_before_project_edge(self) -> None:
        signature = inspect.signature(graph_create_relation_logged)
        assert "authorization" in signature.parameters
        assert signature.parameters["authorization"].kind is inspect.Parameter.KEYWORD_ONLY
        assert signature.parameters["authorization"].default is None

        events: list[tuple[str, object]] = []
        graph = MagicMock()
        authorization = MagicMock(project_key="owned-project")

        async def revalidate(ids):  # noqa: ANN001
            events.append(("revalidate", list(ids)))

        async def create_relation(*args, **kwargs):  # noqa: ANN002,ANN003
            events.append(("create_relation", (args, kwargs)))
            return "created"

        authorization.revalidate_ids = AsyncMock(side_effect=revalidate)
        graph.create_relation = AsyncMock(side_effect=create_relation)

        outcome = await graph_create_relation_logged(
            graph,
            FIXED_UUID,
            REL_UUID,
            "RELATED_TO",
            authorization=authorization,
        )

        assert outcome == "created"
        assert events == [
            ("revalidate", [FIXED_UUID, REL_UUID]),
            (
                "create_relation",
                ((FIXED_UUID, REL_UUID, "RELATED_TO"), {"project_key": "owned-project"}),
            ),
        ]

    @pytest.mark.parametrize("outcome", ["missing_node", "error"])
    async def test_scoped_failed_outcome_emits_no_identifier_log(self, outcome: str) -> None:
        graph = MagicMock()
        graph.create_relation = AsyncMock(return_value=outcome)
        authorization = MagicMock(project_key="owned-project")
        authorization.revalidate_ids = AsyncMock()

        with structlog.testing.capture_logs() as logs:
            actual = await graph_create_relation_logged(
                graph,
                FIXED_UUID,
                REL_UUID,
                "RELATED_TO",
                authorization=authorization,
            )

        assert actual == outcome
        assert logs == []

    async def test_scoped_graph_authorization_error_propagates_without_secondary_log(
        self,
    ) -> None:
        denial = DreamProjectAuthorizationError("object_not_authorized")
        graph = MagicMock()
        graph.create_relation = AsyncMock(side_effect=denial)
        authorization = MagicMock(project_key="owned-project")
        authorization.revalidate_ids = AsyncMock()

        with structlog.testing.capture_logs() as logs:
            with pytest.raises(DreamProjectAuthorizationError) as raised:
                await graph_create_relation_logged(
                    graph,
                    FIXED_UUID,
                    REL_UUID,
                    "RELATED_TO",
                    authorization=authorization,
                )

        assert raised.value is denial
        assert logs == []

    @pytest.mark.parametrize("resolver_result", [False, RuntimeError("resolver secret")])
    async def test_scoped_authorization_refusal_has_only_safe_audit_and_no_graph_write(
        self,
        resolver_result: object,
    ) -> None:
        signature = inspect.signature(graph_create_relation_logged)
        assert "authorization" in signature.parameters

        resolver = MagicMock()
        if isinstance(resolver_result, Exception):
            resolver.references_belong_to_project = AsyncMock(side_effect=resolver_result)
        else:
            resolver.references_belong_to_project = AsyncMock(return_value=resolver_result)
        authorization = DreamProjectScope(
            project_key="owned-project",
            resolver=resolver,
            audit=DreamProjectAudit(principal="dream-codex-connect", phase="connect"),
            tool_name="brain_backfill_links_batch",
        )
        graph = MagicMock()
        graph.create_relation = AsyncMock(return_value="created")

        with structlog.testing.capture_logs() as logs:
            with pytest.raises(DreamProjectAuthorizationError):
                await graph_create_relation_logged(
                    graph,
                    FIXED_UUID,
                    REL_UUID,
                    "RELATED_TO",
                    authorization=authorization,
                )

        graph.create_relation.assert_not_awaited()
        assert len(logs) == 1
        assert logs[0]["event"] == "dream_project.authorization_denied"
        assert set(logs[0]) == {
            "event",
            "principal",
            "phase",
            "project_key",
            "requested_tool",
            "reason",
            "log_level",
        }
        rendered = repr(logs)
        assert str(FIXED_UUID) not in rendered
        assert str(REL_UUID) not in rendered
        assert "resolver secret" not in rendered
        assert "exc_info" not in rendered


class TestScopedGraphHelperPropagation:
    async def test_graph_upsert_threads_authorization_to_related_relations(self) -> None:
        signature = inspect.signature(graph_upsert_entity)
        assert "authorization" in signature.parameters
        assert signature.parameters["authorization"].kind is inspect.Parameter.KEYWORD_ONLY
        assert signature.parameters["authorization"].default is None

        graph = MagicMock()
        graph.upsert_node = AsyncMock(return_value="ok")
        graph.link_to_project = AsyncMock(return_value="ok")
        graph.create_relation = AsyncMock(return_value="created")
        authorization = MagicMock(project_key="owned-project")
        authorization.revalidate_ids = AsyncMock()

        await graph_upsert_entity(
            graph,
            "Learning",
            FIXED_UUID,
            {"topic": "Scoped"},
            project_key="payload-project",
            related_to=[{"id": str(REL_UUID), "type": "RELATED_TO"}],
            authorization=authorization,
        )

        assert authorization.revalidate_ids.await_args_list == [
            (([FIXED_UUID, REL_UUID],), {}),
            (([FIXED_UUID, REL_UUID],), {}),
        ]
        graph.create_relation.assert_awaited_once_with(
            FIXED_UUID,
            REL_UUID,
            "RELATED_TO",
            project_key="owned-project",
        )

    async def test_graph_upsert_does_not_swallow_scoped_refusal(self) -> None:
        signature = inspect.signature(graph_upsert_entity)
        assert "authorization" in signature.parameters

        graph = MagicMock()
        graph.upsert_node = AsyncMock(return_value="ok")
        graph.link_to_project = AsyncMock(return_value="ok")
        graph.create_relation = AsyncMock(return_value="created")
        authorization = MagicMock(project_key="owned-project")
        authorization.revalidate_ids = AsyncMock(side_effect=RuntimeError("denied"))

        with structlog.testing.capture_logs() as logs:
            with pytest.raises(RuntimeError, match="denied"):
                await graph_upsert_entity(
                    graph,
                    "Learning",
                    FIXED_UUID,
                    {"topic": "Scoped"},
                    related_to=[{"id": str(REL_UUID), "type": "RELATED_TO"}],
                    authorization=authorization,
                )

        graph.create_relation.assert_not_awaited()
        graph.upsert_node.assert_not_awaited()
        graph.link_to_project.assert_not_awaited()
        assert logs == []

    async def test_auto_link_threads_authorization_and_propagates_refusal(self) -> None:
        signature = inspect.signature(auto_link_if_enabled)
        assert "authorization" in signature.parameters
        assert signature.parameters["authorization"].kind is inspect.Parameter.KEYWORD_ONLY
        assert signature.parameters["authorization"].default is None

        linker = MagicMock()
        linker.auto_link = AsyncMock(side_effect=RuntimeError("denied"))
        authorization = MagicMock(project_key="owned-project")
        authorization.revalidate_ids = AsyncMock()

        with structlog.testing.capture_logs() as logs:
            with pytest.raises(RuntimeError, match="denied"):
                await auto_link_if_enabled(
                    linker,
                    "Learning",
                    FIXED_UUID,
                    FAKE_EMBEDDING,
                    authorization=authorization,
                )

        assert linker.auto_link.await_args.kwargs["authorization"] is authorization
        assert logs == []


# ---------------------------------------------------------------------------
# graph_delete_entity
# ---------------------------------------------------------------------------


class TestGraphDeleteEntity:
    async def test_noop_when_graph_is_none(self) -> None:
        await graph_delete_entity(None, "Decision", FIXED_UUID)

    async def test_deletes_node(self) -> None:
        graph = MagicMock()
        graph.delete_node = AsyncMock()

        await graph_delete_entity(graph, "Decision", FIXED_UUID)

        graph.delete_node.assert_awaited_once_with("Decision", FIXED_UUID)

    async def test_scoped_delete_forwards_project_without_changing_admin_shape(self) -> None:
        signature = inspect.signature(graph_delete_entity)
        assert signature.parameters["project_key"].kind is inspect.Parameter.KEYWORD_ONLY
        assert signature.parameters["project_key"].default is None
        graph = MagicMock()
        graph.delete_node = AsyncMock(return_value="ok")

        await graph_delete_entity(
            graph,
            "Decision",
            FIXED_UUID,
            project_key="owned-project",
        )

        graph.delete_node.assert_awaited_once_with(
            "Decision",
            FIXED_UUID,
            project_key="owned-project",
        )

    async def test_exception_is_swallowed(self) -> None:
        graph = MagicMock()
        graph.delete_node = AsyncMock(side_effect=RuntimeError("neo4j down"))

        # Should not raise
        await graph_delete_entity(graph, "Decision", FIXED_UUID)


# ---------------------------------------------------------------------------
# link_artifact_if_enabled
# ---------------------------------------------------------------------------


class TestLinkArtifactIfEnabled:
    async def test_noop_when_linker_is_none(self) -> None:
        await link_artifact_if_enabled(None, FAKE_EMBEDDING, "decision", FIXED_UUID, "pk", "title")

    async def test_noop_when_embedding_is_none(self) -> None:
        linker = MagicMock()
        linker.link_artifact = AsyncMock()

        await link_artifact_if_enabled(linker, None, "decision", FIXED_UUID, "pk", "title")

        linker.link_artifact.assert_not_awaited()

    async def test_calls_link_artifact(self) -> None:
        linker = MagicMock()
        linker.link_artifact = AsyncMock()

        await link_artifact_if_enabled(
            linker, FAKE_EMBEDDING, "decision", FIXED_UUID, "pk", "My Title"
        )

        linker.link_artifact.assert_awaited_once_with(
            embedding=FAKE_EMBEDDING,
            artifact_type="decision",
            artifact_id=FIXED_UUID,
            project_key="pk",
            title="My Title",
        )

    async def test_exception_is_swallowed_after_authoritative_write(self) -> None:
        linker = MagicMock()
        linker.link_artifact = AsyncMock(side_effect=RuntimeError("linker down"))

        await link_artifact_if_enabled(
            linker, FAKE_EMBEDDING, "decision", FIXED_UUID, "pk", "My Title"
        )

        linker.link_artifact.assert_awaited_once()


class TestAutoLinkIfEnabledReturnsItsResult:
    """Ticket 6d2cf2a9 (d) — le résultat de liaison ne doit plus être jeté en silence.

    `brain_propose_adr` passe par ce helper. Quand un lien graphe échoue, le
    LinkJobResult porte le décompte dans `errors` — mais le helper retournait
    None, donc l'échec n'existait nulle part au-dessus du journal. Le rendre
    permet à un appelant de le remonter ; ceux qui n'en veulent pas l'ignorent
    EXPLICITEMENT, ce qui est une décision lisible plutôt qu'une perte muette.
    """

    async def test_returns_the_link_job_result_in_admin_mode(self) -> None:
        expected = LinkJobResult(errors=[{"reason": "unknown_endpoint"}])
        linker = MagicMock()
        linker.auto_link = AsyncMock(return_value=expected)

        outcome = await auto_link_if_enabled(linker, "ADR", FIXED_UUID, FAKE_EMBEDDING)

        assert outcome is expected

    async def test_returns_the_link_job_result_in_scoped_mode(self) -> None:
        expected = LinkJobResult(created=[{"id": "x"}])
        linker = MagicMock()
        linker.auto_link = AsyncMock(return_value=expected)
        authorization = MagicMock(project_key="owned-project")
        authorization.revalidate_ids = AsyncMock()

        outcome = await auto_link_if_enabled(
            linker,
            "ADR",
            FIXED_UUID,
            FAKE_EMBEDDING,
            authorization=authorization,
        )

        assert outcome is expected

    async def test_returns_none_when_there_is_no_linker(self) -> None:
        assert await auto_link_if_enabled(None, "ADR", FIXED_UUID, FAKE_EMBEDDING) is None

    async def test_returns_none_when_the_admin_path_swallowed_an_exception(self) -> None:
        """Le contrat de dégradation admin est inchangé : on avale, donc pas de résultat."""
        linker = MagicMock()
        linker.auto_link = AsyncMock(side_effect=RuntimeError("graph down"))

        with structlog.testing.capture_logs() as logs:
            outcome = await auto_link_if_enabled(linker, "ADR", FIXED_UUID, FAKE_EMBEDDING)

        assert outcome is None
        assert [entry["event"] for entry in logs] == ["auto_link_failed"]
