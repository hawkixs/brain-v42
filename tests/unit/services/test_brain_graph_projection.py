"""Contract tests for the model-free, full Brain graph projection."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from brain_v42.services.brain_graph_projection import (
    BrainGraphProjectionService,
    Neo4jGraphRows,
    PostgresGraphRows,
)

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000001")
OLD_DECISION_ID = UUID("10000000-0000-0000-0000-000000000001")
NEW_DECISION_ID = UUID("10000000-0000-0000-0000-000000000002")
LEARNING_ID = UUID("20000000-0000-0000-0000-000000000001")
SNIPPET_ID = UUID("20000000-0000-0000-0000-000000000002")
RUNBOOK_ID = UUID("20000000-0000-0000-0000-000000000003")
ADR_OLD_ID = UUID("30000000-0000-0000-0000-000000000001")
ADR_NEW_ID = UUID("30000000-0000-0000-0000-000000000002")
FEATURE_ID = UUID("40000000-0000-0000-0000-000000000001")
PLAN_ID = UUID("50000000-0000-0000-0000-000000000001")
EVENT_ID = UUID("60000000-0000-0000-0000-000000000001")
TICKET_ID = UUID("70000000-0000-0000-0000-000000000001")
SESSION_ID = UUID("80000000-0000-0000-0000-000000000001")


class FakePostgresSource:
    def __init__(self, tables: dict[str, list[dict]], *, truncated: bool = False) -> None:
        self.tables = tables
        self.truncated = truncated
        self.calls: list[int] = []

    async def read(self, max_rows: int) -> PostgresGraphRows:
        self.calls.append(max_rows)
        return PostgresGraphRows(tables=self.tables, truncated=self.truncated)


class FakeNeo4jSource:
    def __init__(self, rows: Neo4jGraphRows) -> None:
        self.rows = rows
        self.calls: list[tuple[int, int]] = []

    async def read(self, max_nodes: int, max_edges: int) -> Neo4jGraphRows:
        self.calls.append((max_nodes, max_edges))
        return self.rows


def _empty_tables() -> dict[str, list[dict]]:
    return {
        "project_contexts": [],
        "decisions": [],
        "learnings": [],
        "snippets": [],
        "runbooks": [],
        "adrs": [],
        "features": [],
        "indexed_plans": [],
        "gitlab_events": [],
        "dream_runs": [],
        "dream_promotions": [],
        "tickets": [],
        "brain_sessions": [],
        "ticket_extraction_proposals": [],
        "roadmap_curation_proposals": [],
        "feature_artifacts": [],
        "consolidation_log": [],
    }


def _representative_tables() -> dict[str, list[dict]]:
    rows = _empty_tables()
    rows["project_contexts"] = [
        {
            "id": PROJECT_ID,
            "project_key": "brain-v42",
            "name": "Brain v42",
            "current_phase": "COR 1/3",
            "project_group": "red-triad",
            "blockers": [],
            "related_projects": ["red-monitor"],
            "created_at": NOW,
            "updated_at": NOW,
        }
    ]
    rows["decisions"] = [
        {
            "id": OLD_DECISION_ID,
            "title": "Ancienne décision",
            "project_key": "brain-v42",
            "status": "superseded",
            "freshness_status": "fresh",
            "superseded_by": NEW_DECISION_ID,
            "merged_into": None,
            "access_count": 4,
            "created_at": NOW,
            "updated_at": NOW,
        },
        {
            "id": NEW_DECISION_ID,
            "title": "Décision courante",
            "project_key": "brain-v42",
            "status": "active",
            "freshness_status": "fresh",
            "superseded_by": None,
            "merged_into": None,
            "access_count": 9,
            "created_at": NOW,
            "updated_at": NOW,
        },
    ]
    rows["learnings"] = [
        {
            "id": LEARNING_ID,
            "topic": "Projection unifiée",
            "project_key": "brain-v42",
            "confidence": "high",
            "source_type": "experience",
            "freshness_status": "fresh",
            "merged_into": None,
            "access_count": 2,
            "created_at": NOW,
            "updated_at": NOW,
        }
    ]
    rows["snippets"] = [
        {
            "id": SNIPPET_ID,
            "title": "Exemple de projection",
            "project_key": "brain-v42",
            "language": "python",
            "freshness_status": "fresh",
            "merged_into": None,
            "use_count": 3,
            "access_count": 5,
            "created_at": NOW,
            "updated_at": NOW,
        }
    ]
    rows["runbooks"] = [
        {
            "id": RUNBOOK_ID,
            "title": "Exploiter le graphe",
            "project_key": "brain-v42",
            "freshness_status": "fresh",
            "merged_into": None,
            "execution_count": 2,
            "last_execution_status": "success",
            "access_count": 4,
            "created_at": NOW,
            "updated_at": NOW,
        }
    ]
    rows["adrs"] = [
        {
            "id": ADR_OLD_ID,
            "number": 1,
            "title": "Ancienne architecture",
            "project_key": "brain-v42",
            "status": "superseded",
            "freshness_status": "fresh",
            "superseded_by": 2,
            "merged_into": None,
            "access_count": 1,
            "created_at": NOW,
            "updated_at": NOW,
        },
        {
            "id": ADR_NEW_ID,
            "number": 2,
            "title": "Architecture courante",
            "project_key": "brain-v42",
            "status": "accepted",
            "freshness_status": "fresh",
            "superseded_by": None,
            "merged_into": None,
            "access_count": 3,
            "created_at": NOW,
            "updated_at": NOW,
        },
    ]
    rows["features"] = [
        {
            "id": FEATURE_ID,
            "project_key": "brain-v42",
            "name": "Brain Graph",
            "status": "in_progress",
            "pinned": True,
            "merged_into": None,
            "created_at": NOW,
            "updated_at": NOW,
        }
    ]
    rows["indexed_plans"] = [
        {
            "id": PLAN_ID,
            "title": "Plan graphe intégral",
            "plan_type": "implementation",
            "project_key": "brain-v42",
            "status": "active",
            "freshness_status": "fresh",
            "chunk_count": 4,
            "word_count": 1200,
            "access_count": 1,
            "created_at": NOW,
            "updated_at": NOW,
        }
    ]
    rows["gitlab_events"] = [
        {
            "id": EVENT_ID,
            "event_type": "merge_request",
            "project_key": "brain-v42",
            "title": "feat: graph export",
            "ref": "main",
            "feature_id": FEATURE_ID,
            "processed_at": NOW,
        }
    ]
    rows["feature_artifacts"] = [
        {
            "feature_id": FEATURE_ID,
            "artifact_type": "decision",
            "artifact_id": NEW_DECISION_ID,
            "similarity_score": 0.91,
            "created_at": NOW,
        }
    ]
    rows["dream_runs"] = [
        {
            "id": 41,
            "run_date": date(2026, 7, 20),
            "phase": "promote",
            "model": "test-model",
            "status": "success",
            "duration_s": 12.5,
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_tokens": 30,
            "cache_creation_tokens": 0,
            "cost_usd": 0.03,
            "api_calls": 1,
            "tool_calls": 2,
            "phase_dry_run": False,
            "created_at": NOW,
        }
    ]
    rows["dream_promotions"] = [
        {
            "id": 51,
            "dream_run_id": 41,
            "source_learning_id": LEARNING_ID,
            "target_type": "adr",
            "target_adr_id": ADR_NEW_ID,
            "target_runbook_id": None,
            "cosine_observed": 0.88,
            "skipped_reason": None,
            "created_at": NOW,
        }
    ]
    rows["tickets"] = [
        {
            "id": TICKET_ID,
            "kind": "request",
            "title": "Brancher le graphe",
            "from_project": "brain-v42",
            "to_project": "red-monitor",
            "status": "in_progress",
            "extraction_status": None,
            "created_at": NOW,
            "updated_at": NOW,
            "resolved_at": None,
            "closed_at": None,
        }
    ]
    rows["brain_sessions"] = [
        {
            "id": SESSION_ID,
            "project_key": "brain-v42",
            "status": "open",
            "captured_knowledge_ids": [NEW_DECISION_ID, PLAN_ID],
            "started_at": NOW,
            "ended_at": None,
            "updated_at": NOW,
        }
    ]
    rows["ticket_extraction_proposals"] = [
        {
            "id": 61,
            "ticket_id": TICKET_ID,
            "target_type": "decision",
            "target_project": "brain-v42",
            "status": "applied",
            "applied_entity_id": NEW_DECISION_ID,
            "created_at": NOW,
            "applied_at": NOW,
        }
    ]
    rows["roadmap_curation_proposals"] = [
        {
            "id": 71,
            "op": "archive",
            "feature_id": FEATURE_ID,
            "status": "proposed",
            "created_at": NOW,
            "applied_at": None,
        }
    ]
    return rows


@pytest.mark.asyncio
async def test_full_projection_contains_every_visible_family_and_semantic_edges() -> None:
    postgres = FakePostgresSource(_representative_tables())
    neo4j = FakeNeo4jSource(
        Neo4jGraphRows(
            status="ok",
            nodes=[
                {
                    "identity": str(NEW_DECISION_ID),
                    "labels": ["Decision"],
                    "label": "Label Neo4j moins riche",
                    "project_key": "brain-v42",
                },
                {
                    "identity": "memory",
                    "labels": ["Domain"],
                    "label": "memory",
                    "project_key": None,
                },
            ],
            edges=[
                {
                    "source_identity": str(NEW_DECISION_ID),
                    "source_labels": ["Decision"],
                    "target_identity": "memory",
                    "target_labels": ["Domain"],
                    "type": "BELONGS_TO_DOMAIN",
                    "weight": 1.0,
                }
            ],
        )
    )
    service = BrainGraphProjectionService(
        postgres_source=postgres,
        neo4j_source=neo4j,
        cache_ttl_s=0,
    )

    payload = await service.snapshot()

    assert payload["schema_version"] == "brain-graph.v1"
    assert payload["mode"] == "full"
    assert payload["status"] == "ok"
    assert payload["truncated"] == {"nodes": False, "edges": False}
    nodes = {node["id"]: node for node in payload["nodes"]}
    expected_kinds = {
        "project",
        "decision",
        "learning",
        "snippet",
        "runbook",
        "adr",
        "feature",
        "plan",
        "gitlab-event",
        "dream-night",
        "dream-phase",
        "dream-promotion",
        "ticket",
        "session",
        "ticket-proposal",
        "roadmap-proposal",
        "domain",
    }
    assert expected_kinds <= {node["kind"] for node in nodes.values()}
    assert nodes[f"decision:{NEW_DECISION_ID}"]["label"] == "Décision courante"
    assert nodes[f"decision:{NEW_DECISION_ID}"]["origins"] == ["neo4j", "postgres"]

    edges = {(edge["source"], edge["target"], edge["type"]) for edge in payload["edges"]}
    assert (
        f"decision:{NEW_DECISION_ID}",
        f"decision:{OLD_DECISION_ID}",
        "SUPERSEDES",
    ) in edges
    assert (f"adr:{ADR_NEW_ID}", f"adr:{ADR_OLD_ID}", "SUPERSEDES") in edges
    assert (f"feature:{FEATURE_ID}", f"decision:{NEW_DECISION_ID}", "HAS_ARTIFACT") in edges
    assert (f"feature:{FEATURE_ID}", f"gitlab-event:{EVENT_ID}", "TRACKS_EVENT") in edges
    assert ("dream-phase:41", "dream-night:2026-07-20", "BELONGS_TO_NIGHT") in edges
    assert ("dream-promotion:51", f"learning:{LEARNING_ID}", "EVALUATES") in edges
    assert ("dream-promotion:51", f"adr:{ADR_NEW_ID}", "MATERIALIZED_AS") in edges
    assert (f"ticket:{TICKET_ID}", "project:brain-v42", "SENT_BY") in edges
    assert (f"ticket:{TICKET_ID}", "project:red-monitor", "ASSIGNED_TO") in edges
    assert (f"session:{SESSION_ID}", f"decision:{NEW_DECISION_ID}", "CAPTURED") in edges
    assert (f"session:{SESSION_ID}", f"plan:{PLAN_ID}", "CAPTURED") in edges
    assert payload["integrity"] == {
        "dangling_edges": 0,
        "ambiguous_references": 0,
        "unresolved_references": 0,
        "neo4j_orphans": 0,
    }
    assert all(edge["source"] in nodes and edge["target"] in nodes for edge in payload["edges"])


@pytest.mark.asyncio
async def test_projection_does_not_expose_heavy_or_sensitive_fields() -> None:
    rows = _representative_tables()
    rows["tickets"][0]["body"] = "SECRET BODY"
    rows["indexed_plans"][0].update(
        {"content": "SECRET CONTENT", "file_path": "/private/path", "embedding": [1.0]}
    )
    rows["dream_runs"][0]["error_message"] = "SECRET ERROR"
    rows["ticket_extraction_proposals"][0].update(
        {"payload": {"secret": True}, "rationale": "SECRET RATIONALE"}
    )
    service = BrainGraphProjectionService(
        postgres_source=FakePostgresSource(rows),
        neo4j_source=None,
        cache_ttl_s=0,
    )

    payload = await service.snapshot()
    serialized = repr(payload)

    assert "SECRET" not in serialized
    forbidden_keys = {
        "body",
        "content",
        "code",
        "embedding",
        "search_vector",
        "payload",
        "rationale",
        "file_path",
        "error_message",
    }
    assert all(not (forbidden_keys & set(node)) for node in payload["nodes"])
    assert all(not (forbidden_keys & set(node["attributes"])) for node in payload["nodes"])


@pytest.mark.asyncio
async def test_polymorphic_capture_is_not_invented_when_uuid_is_ambiguous() -> None:
    rows = _empty_tables()
    shared_id = UUID("90000000-0000-0000-0000-000000000001")
    rows["project_contexts"] = [
        {
            "id": PROJECT_ID,
            "project_key": "brain-v42",
            "name": "Brain v42",
            "blockers": [],
            "related_projects": [],
            "created_at": NOW,
            "updated_at": NOW,
        }
    ]
    rows["decisions"] = [
        {
            "id": shared_id,
            "title": "Decision collision",
            "project_key": "brain-v42",
            "created_at": NOW,
            "updated_at": NOW,
        }
    ]
    rows["learnings"] = [
        {
            "id": shared_id,
            "topic": "Learning collision",
            "project_key": "brain-v42",
            "created_at": NOW,
            "updated_at": NOW,
        }
    ]
    rows["brain_sessions"] = [
        {
            "id": SESSION_ID,
            "project_key": "brain-v42",
            "status": "ended",
            "captured_knowledge_ids": [shared_id],
            "started_at": NOW,
            "ended_at": NOW,
            "updated_at": NOW,
        }
    ]
    service = BrainGraphProjectionService(
        postgres_source=FakePostgresSource(rows),
        neo4j_source=None,
        cache_ttl_s=0,
    )

    payload = await service.snapshot()

    captured = [edge for edge in payload["edges"] if edge["type"] == "CAPTURED"]
    assert captured == []
    assert payload["integrity"]["ambiguous_references"] == 1


@pytest.mark.asyncio
async def test_neo4j_unavailable_is_explicit_degraded_state_not_an_empty_success() -> None:
    neo4j = FakeNeo4jSource(Neo4jGraphRows(status="unavailable"))
    service = BrainGraphProjectionService(
        postgres_source=FakePostgresSource(_representative_tables()),
        neo4j_source=neo4j,
        cache_ttl_s=0,
    )

    payload = await service.snapshot()

    assert payload["status"] == "degraded"
    assert payload["sources"]["postgres"]["status"] == "ok"
    assert payload["sources"]["neo4j"]["status"] == "unavailable"
    assert payload["counts"]["nodes"]["total"] > 0


@pytest.mark.asyncio
async def test_limits_are_strict_and_every_emitted_edge_has_two_nodes() -> None:
    rows = _empty_tables()
    rows["project_contexts"] = [
        {
            "id": UUID(int=index + 1),
            "project_key": f"project-{index}",
            "name": f"Project {index}",
            "blockers": [],
            "related_projects": [f"project-{(index + 1) % 5}"],
            "created_at": NOW,
            "updated_at": NOW,
        }
        for index in range(5)
    ]
    service = BrainGraphProjectionService(
        postgres_source=FakePostgresSource(rows),
        neo4j_source=None,
        max_nodes=3,
        max_edges=2,
        cache_ttl_s=0,
    )

    payload = await service.snapshot()

    node_ids = {node["id"] for node in payload["nodes"]}
    assert len(payload["nodes"]) == 3
    assert len(payload["edges"]) <= 2
    assert payload["truncated"]["nodes"] is True
    assert all(
        edge["source"] in node_ids and edge["target"] in node_ids for edge in payload["edges"]
    )


@pytest.mark.asyncio
async def test_unscoped_knowledge_is_valid_and_does_not_count_as_a_dangling_edge() -> None:
    rows = _empty_tables()
    rows["decisions"] = [
        {
            "id": NEW_DECISION_ID,
            "title": "Global decision",
            "project_key": None,
            "status": "active",
            "created_at": NOW,
            "updated_at": NOW,
        }
    ]
    service = BrainGraphProjectionService(
        postgres_source=FakePostgresSource(rows),
        neo4j_source=None,
        cache_ttl_s=0,
    )

    payload = await service.snapshot()

    assert f"decision:{NEW_DECISION_ID}" in {node["id"] for node in payload["nodes"]}
    assert payload["integrity"]["dangling_edges"] == 0


@pytest.mark.asyncio
async def test_cache_ttl_starts_after_the_expensive_snapshot_build() -> None:
    class ManualMonotonic:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    timer = ManualMonotonic()

    class AdvancingPostgresSource(FakePostgresSource):
        async def read(self, max_rows: int) -> PostgresGraphRows:
            result = await super().read(max_rows)
            timer.value += 12.0
            return result

    postgres = AdvancingPostgresSource(_representative_tables())
    service = BrainGraphProjectionService(
        postgres_source=postgres,
        neo4j_source=None,
        cache_ttl_s=15,
        monotonic=timer,
    )

    first = await service.snapshot()
    timer.value = 20.0  # 8 seconds after completion, not 20 seconds after start.
    second = await service.snapshot()

    assert second is first
    assert len(postgres.calls) == 1


@pytest.mark.asyncio
async def test_concurrent_cache_miss_builds_one_snapshot_then_refreshes_after_ttl() -> None:
    class ManualMonotonic:
        value = 10.0

        def __call__(self) -> float:
            return self.value

    class BlockingPostgresSource(FakePostgresSource):
        def __init__(self) -> None:
            super().__init__(_representative_tables())
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def read(self, max_rows: int) -> PostgresGraphRows:
            self.started.set()
            await self.release.wait()
            return await super().read(max_rows)

    timer = ManualMonotonic()
    postgres = BlockingPostgresSource()
    service = BrainGraphProjectionService(
        postgres_source=postgres,
        neo4j_source=None,
        cache_ttl_s=15,
        monotonic=timer,
    )

    requests = [asyncio.create_task(service.snapshot()) for _ in range(8)]
    await asyncio.wait_for(postgres.started.wait(), timeout=0.1)
    postgres.release.set()
    snapshots = await asyncio.gather(*requests)

    assert len(postgres.calls) == 1
    assert all(snapshot is snapshots[0] for snapshot in snapshots)

    timer.value = 26.0
    refreshed = await service.snapshot()

    assert refreshed is not snapshots[0]
    assert len(postgres.calls) == 2


@pytest.mark.asyncio
async def test_edge_cap_is_exact_and_reports_truncation_without_dangling_edges() -> None:
    rows = _empty_tables()
    rows["project_contexts"] = [
        {
            "id": UUID(int=index + 1),
            "project_key": f"project-{index}",
            "name": f"Project {index}",
            "blockers": [],
            "related_projects": [
                f"project-{candidate}" for candidate in range(4) if candidate != index
            ],
            "created_at": NOW,
            "updated_at": NOW,
        }
        for index in range(4)
    ]
    service = BrainGraphProjectionService(
        postgres_source=FakePostgresSource(rows),
        neo4j_source=None,
        max_nodes=10,
        max_edges=3,
        cache_ttl_s=0,
    )

    payload = await service.snapshot()

    assert len(payload["edges"]) == 3
    assert payload["truncated"]["edges"] is True
    assert payload["integrity"]["dangling_edges"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("postgres_status", "neo4j_status", "expected_status"),
    [
        ("unavailable", "ok", "degraded"),
        ("unavailable", "unavailable", "unavailable"),
    ],
)
async def test_source_failure_matrix_is_explicit(
    postgres_status: str,
    neo4j_status: str,
    expected_status: str,
) -> None:
    postgres_rows = PostgresGraphRows(tables=_empty_tables(), status=postgres_status)

    class FixedPostgresSource:
        async def read(self, _max_rows: int) -> PostgresGraphRows:
            return postgres_rows

    neo_nodes = (
        [{"identity": "memory", "labels": ["Domain"], "label": "memory"}]
        if neo4j_status == "ok"
        else []
    )
    service = BrainGraphProjectionService(
        postgres_source=FixedPostgresSource(),
        neo4j_source=FakeNeo4jSource(Neo4jGraphRows(status=neo4j_status, nodes=neo_nodes)),
        cache_ttl_s=0,
    )

    payload = await service.snapshot()

    assert payload["status"] == expected_status
    assert payload["sources"]["postgres"]["status"] == postgres_status
    assert payload["sources"]["neo4j"]["status"] == neo4j_status


@pytest.mark.asyncio
async def test_postgres_metadata_wins_and_duplicate_edges_merge_deterministically() -> None:
    rows = _empty_tables()
    rows["project_contexts"] = [
        {
            "project_key": "brain-v42",
            "name": "Brain v42",
            "blockers": [],
            "related_projects": [],
            "created_at": NOW,
            "updated_at": NOW,
        }
    ]
    rows["decisions"] = [
        {
            "id": NEW_DECISION_ID,
            "title": "Titre PostgreSQL autoritaire",
            "project_key": "brain-v42",
            "status": "active",
            "freshness_status": "fresh",
            "created_at": NOW,
            "updated_at": NOW,
        }
    ]
    neo4j = FakeNeo4jSource(
        Neo4jGraphRows(
            nodes=[
                {
                    "identity": str(NEW_DECISION_ID),
                    "labels": ["Decision"],
                    "label": "Titre Neo4j obsolète",
                    "project_key": "other-project",
                },
                {
                    "identity": "brain-v42",
                    "labels": ["Project"],
                    "label": "Nom Neo4j obsolète",
                },
            ],
            edges=[
                {
                    "source_identity": str(NEW_DECISION_ID),
                    "source_labels": ["Decision"],
                    "target_identity": "brain-v42",
                    "target_labels": ["Project"],
                    "type": "BELONGS_TO",
                    "weight": 0.5,
                }
            ],
        )
    )
    service = BrainGraphProjectionService(
        postgres_source=FakePostgresSource(rows),
        neo4j_source=neo4j,
        cache_ttl_s=0,
    )

    payload = await service.snapshot()
    nodes = {node["id"]: node for node in payload["nodes"]}
    belonging_edges = [edge for edge in payload["edges"] if edge["type"] == "BELONGS_TO"]
    assert len(belonging_edges) == 1
    belonging = belonging_edges[0]

    assert nodes[f"decision:{NEW_DECISION_ID}"]["label"] == "Titre PostgreSQL autoritaire"
    assert nodes[f"decision:{NEW_DECISION_ID}"]["project_key"] == "brain-v42"
    assert nodes[f"decision:{NEW_DECISION_ID}"]["origins"] == ["neo4j", "postgres"]
    assert belonging["origins"] == ["neo4j", "postgres"]
    assert belonging["weight"] == 1.0


@pytest.mark.asyncio
async def test_undirected_edges_are_canonical_and_zero_weight_is_preserved() -> None:
    neo4j = FakeNeo4jSource(
        Neo4jGraphRows(
            nodes=[
                {"identity": "a", "labels": ["Domain"], "label": "A"},
                {"identity": "b", "labels": ["Domain"], "label": "B"},
            ],
            edges=[
                {
                    "source_identity": "a",
                    "source_labels": ["Domain"],
                    "target_identity": "b",
                    "target_labels": ["Domain"],
                    "type": "RELATED_TO",
                    "weight": 0.0,
                },
                {
                    "source_identity": "b",
                    "source_labels": ["Domain"],
                    "target_identity": "a",
                    "target_labels": ["Domain"],
                    "type": "RELATED_TO",
                    "weight": 0.0,
                },
            ],
        )
    )
    service = BrainGraphProjectionService(
        postgres_source=FakePostgresSource(_empty_tables()),
        neo4j_source=neo4j,
        cache_ttl_s=0,
    )

    payload = await service.snapshot()
    related = [edge for edge in payload["edges"] if edge["type"] == "RELATED_TO"]

    assert len(related) == 1
    assert related[0]["source"] == "domain:a"
    assert related[0]["target"] == "domain:b"
    assert related[0]["weight"] == 0.0


@pytest.mark.asyncio
async def test_neo4j_only_entity_is_exposed_as_an_integrity_orphan() -> None:
    service = BrainGraphProjectionService(
        postgres_source=FakePostgresSource(_empty_tables()),
        neo4j_source=FakeNeo4jSource(
            Neo4jGraphRows(
                nodes=[
                    {
                        "identity": str(NEW_DECISION_ID),
                        "labels": ["Decision"],
                        "label": "Orphelin Neo4j",
                    }
                ]
            )
        ),
        cache_ttl_s=0,
    )

    payload = await service.snapshot()

    orphan = payload["nodes"][0]
    assert orphan["id"] == f"decision:{NEW_DECISION_ID}"
    assert orphan["orphaned"] is True
    assert payload["integrity"]["neo4j_orphans"] == 1
