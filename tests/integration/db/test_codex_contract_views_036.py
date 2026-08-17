"""Real PostgreSQL coverage for the Codex v2 read contract migration 036."""

from __future__ import annotations

import asyncio
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from brain_v42.codex_gateway.composition import GatewayRuntime

pytestmark = pytest.mark.integration

CONTRACT_COLUMNS = {
    "codex_ticket_v1": (
        "id",
        "kind",
        "title",
        "body",
        "from_project",
        "to_project",
        "status",
        "extraction_status",
        "resolved_at",
        "closed_at",
        "created_at",
        "updated_at",
    ),
    "codex_ticket_message_v1": (
        "id",
        "ticket_id",
        "author_project",
        "body",
        "status_to",
        "created_at",
    ),
    "codex_feature_v1": (
        "id",
        "project_key",
        "name",
        "description",
        "status",
        "status_updated_at",
        "pinned",
        "merged_into",
        "created_at",
        "updated_at",
    ),
    "codex_feature_artifact_v1": (
        "feature_id",
        "artifact_type",
        "artifact_id",
        "similarity_score",
        "created_at",
    ),
    "codex_dream_run_v1": (
        "id",
        "run_date",
        "phase",
        "model",
        "status",
        "phase_dry_run",
        "duration_s",
        "cost_usd",
        "input_tokens",
        "output_tokens",
        "api_calls",
        "tool_calls",
        "error_message",
        "created_at",
    ),
    "codex_dream_promotion_v1": (
        "id",
        "dream_run_id",
        "source_learning_id",
        "target_type",
        "target_adr_id",
        "target_runbook_id",
        "cosine_observed",
        "skipped_reason",
        "created_at",
    ),
    "codex_ticket_extraction_proposal_v1": (
        "id",
        "ticket_id",
        "target_type",
        "target_project",
        "payload",
        "rationale",
        "status",
        "applied_entity_id",
        "created_at",
        "applied_at",
    ),
    "codex_roadmap_curation_proposal_v1": (
        "id",
        "op",
        "feature_id",
        "payload",
        "rationale",
        "status",
        "apply_log",
        "created_at",
        "applied_at",
    ),
    "codex_consolidation_log_v1": (
        "id",
        "source_id",
        "target_id",
        "entity_type",
        "similarity",
        "action",
        "created_at",
    ),
}

BASE_TABLES = (
    "tickets",
    "ticket_messages",
    "features",
    "feature_artifacts",
    "dream_runs",
    "dream_promotions",
    "ticket_extraction_proposals",
    "roadmap_curation_proposals",
    "consolidation_log",
)

SECURITY_BARRIER_VIEWS = (
    "codex_brain_entity_v1",
    "codex_ticket_v1",
    "codex_ticket_message_v1",
    "codex_feature_v1",
    "codex_feature_artifact_v1",
    "codex_ticket_extraction_proposal_v1",
    "codex_roadmap_curation_proposal_v1",
)


def _key(label: str) -> str:
    return f"integ-codex036-{label}-{uuid.uuid4().hex[:8]}"


async def test_gateway_readiness_accepts_the_real_036_contract(engine: AsyncEngine) -> None:
    runtime = GatewayRuntime(
        services=object(),  # type: ignore[arg-type]
        embedding_service=object(),  # type: ignore[arg-type]
        session_factory=async_sessionmaker(engine),
    )

    await runtime.readiness()


@pytest.mark.parametrize(
    ("table", "trigger"),
    [
        ("feature_artifacts", "trg_feature_artifact_live_target"),
        ("tickets", "trg_ticket_participants_immutable"),
    ],
)
async def test_gateway_readiness_rejects_replica_only_triggers(
    engine: AsyncEngine,
    table: str,
    trigger: str,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(sa.text(f"ALTER TABLE {table} ENABLE REPLICA TRIGGER {trigger}"))

    try:
        runtime = GatewayRuntime(
            services=object(),  # type: ignore[arg-type]
            embedding_service=object(),  # type: ignore[arg-type]
            session_factory=async_sessionmaker(engine),
        )

        with pytest.raises(RuntimeError, match="database contract"):
            await runtime.readiness()
    finally:
        async with engine.begin() as connection:
            await connection.execute(sa.text(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}"))


@pytest.mark.parametrize("view", SECURITY_BARRIER_VIEWS)
async def test_red_scoped_views_are_security_barriers(
    engine: AsyncEngine,
    view: str,
) -> None:
    async with engine.connect() as connection:
        options = await connection.scalar(
            sa.text("SELECT reloptions FROM pg_class WHERE oid = to_regclass('public.' || :view)"),
            {"view": view},
        )

    assert options is not None
    assert "security_barrier=true" in options


async def test_codex_ro_predicate_cannot_observe_hidden_entity_rows(
    engine: AsyncEngine,
) -> None:
    hidden_project = _key("barrier-hidden")
    hidden_learning = uuid.uuid4()

    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO learnings (id, topic, insight, project_key) "
                "VALUES (:id, 'hidden barrier sentinel', 'must stay hidden', :project)"
            ),
            {"id": hidden_learning, "project": hidden_project},
        )
        await connection.execute(sa.text("SET LOCAL ROLE codex_ro"))
        await connection.execute(sa.text("CREATE TEMP TABLE barrier_observations(value text)"))
        await connection.execute(
            sa.text(
                """
                CREATE FUNCTION pg_temp.observe_hidden(candidate text, sought text)
                RETURNS boolean
                LANGUAGE plpgsql
                VOLATILE
                COST 0.0001
                AS $$
                BEGIN
                    IF candidate = sought THEN
                        INSERT INTO barrier_observations(value) VALUES (candidate);
                    END IF;
                    RETURN false;
                END
                $$
                """
            )
        )
        visible = await connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM codex_brain_entity_v1 "
                "WHERE pg_temp.observe_hidden(project_key, :project)"
            ),
            {"project": hidden_project},
        )
        observations = await connection.scalar(sa.text("SELECT COUNT(*) FROM barrier_observations"))

    assert visible == 0
    assert observations == 0


@pytest.mark.parametrize("view, expected", CONTRACT_COLUMNS.items())
async def test_view_exposes_exact_contract_columns(
    engine: AsyncEngine,
    view: str,
    expected: tuple[str, ...],
) -> None:
    async with engine.connect() as connection:
        columns = (
            await connection.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :view "
                    "ORDER BY ordinal_position"
                ),
                {"view": view},
            )
        ).scalars()

    assert tuple(columns) == expected


async def test_ticket_family_is_scoped_through_red_tickets(engine: AsyncEngine) -> None:
    red_base = _key("red")
    red_child = f"{red_base}:agent"
    outside = _key("outside")

    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO project_contexts "
                "(project_key, name, description, project_group) "
                "VALUES (:key, :key, 'migration 036 integration', 'red')"
            ),
            {"key": red_base},
        )
        ticket_rows = (
            await connection.execute(
                sa.text(
                    "INSERT INTO tickets (kind, title, body, from_project, to_project) "
                    "VALUES "
                    "('request', 'red sender', 'visible', :red, :outside), "
                    "('request', 'red recipient', 'visible', :outside, :red), "
                    "('request', 'outside', 'hidden', :outside, :outside) "
                    "RETURNING id, title"
                ),
                {"red": red_child, "outside": outside},
            )
        ).all()
        ticket_ids = {title: ticket_id for ticket_id, title in ticket_rows}
        message_rows = (
            await connection.execute(
                sa.text(
                    "INSERT INTO ticket_messages (ticket_id, author_project, body) "
                    "VALUES (:visible, :outside, 'visible'), (:hidden, :outside, 'hidden') "
                    "RETURNING id, body"
                ),
                {
                    "visible": ticket_ids["red sender"],
                    "hidden": ticket_ids["outside"],
                    "outside": outside,
                },
            )
        ).all()
        message_ids = {body: message_id for message_id, body in message_rows}
        proposal_rows = (
            await connection.execute(
                sa.text(
                    "INSERT INTO ticket_extraction_proposals "
                    "(ticket_id, target_type, target_project, payload, rationale) "
                    "VALUES "
                    "(:visible, 'learning', :outside, '{}'::jsonb, 'visible'), "
                    "(:hidden, 'learning', :outside, '{}'::jsonb, 'hidden') "
                    "RETURNING id, rationale"
                ),
                {
                    "visible": ticket_ids["red sender"],
                    "hidden": ticket_ids["outside"],
                    "outside": outside,
                },
            )
        ).all()
        proposal_ids = {rationale: proposal_id for proposal_id, rationale in proposal_rows}

        visible_tickets = set(
            (
                await connection.execute(
                    sa.text("SELECT id FROM codex_ticket_v1 WHERE id = ANY(:ids)"),
                    {"ids": list(ticket_ids.values())},
                )
            ).scalars()
        )
        visible_messages = set(
            (
                await connection.execute(
                    sa.text("SELECT id FROM codex_ticket_message_v1 WHERE id = ANY(:ids)"),
                    {"ids": list(message_ids.values())},
                )
            ).scalars()
        )
        visible_proposals = set(
            (
                await connection.execute(
                    sa.text(
                        "SELECT id FROM codex_ticket_extraction_proposal_v1 WHERE id = ANY(:ids)"
                    ),
                    {"ids": list(proposal_ids.values())},
                )
            ).scalars()
        )

    assert visible_tickets == {
        ticket_ids["red sender"],
        ticket_ids["red recipient"],
    }
    assert visible_messages == {message_ids["visible"]}
    assert visible_proposals == {proposal_ids["visible"]}


async def test_roadmap_family_is_scoped_through_red_features(engine: AsyncEngine) -> None:
    red_base = _key("red")
    red_child = f"{red_base}:agent"
    outside = _key("outside")

    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO project_contexts "
                "(project_key, name, description, project_group) "
                "VALUES (:key, :key, 'migration 036 integration', 'red')"
            ),
            {"key": red_base},
        )
        feature_rows = (
            await connection.execute(
                sa.text(
                    "INSERT INTO features (project_key, name, description) "
                    "VALUES (:red, 'red feature', 'visible'), "
                    "(:outside, 'outside feature', 'hidden') RETURNING id, name"
                ),
                {"red": red_child, "outside": outside},
            )
        ).all()
        feature_ids = {name: feature_id for feature_id, name in feature_rows}
        artifact_rows = (
            await connection.execute(
                sa.text(
                    "INSERT INTO feature_artifacts "
                    "(feature_id, artifact_type, artifact_id, similarity_score) "
                    "VALUES (:visible, 'learning', :visible_artifact, 0.9), "
                    "(:hidden, 'learning', :hidden_artifact, 0.8) "
                    "RETURNING feature_id"
                ),
                {
                    "visible": feature_ids["red feature"],
                    "hidden": feature_ids["outside feature"],
                    "visible_artifact": uuid.uuid4(),
                    "hidden_artifact": uuid.uuid4(),
                },
            )
        ).scalars()
        artifact_feature_ids = set(artifact_rows)
        proposal_rows = (
            await connection.execute(
                sa.text(
                    "INSERT INTO roadmap_curation_proposals "
                    "(op, feature_id, payload, rationale) VALUES "
                    "('archive', :visible, '{}'::jsonb, 'visible'), "
                    "('archive', :hidden, '{}'::jsonb, 'hidden') RETURNING id, rationale"
                ),
                {
                    "visible": feature_ids["red feature"],
                    "hidden": feature_ids["outside feature"],
                },
            )
        ).all()
        proposal_ids = {rationale: proposal_id for proposal_id, rationale in proposal_rows}

        visible_features = set(
            (
                await connection.execute(
                    sa.text("SELECT id FROM codex_feature_v1 WHERE id = ANY(:ids)"),
                    {"ids": list(feature_ids.values())},
                )
            ).scalars()
        )
        visible_artifacts = set(
            (
                await connection.execute(
                    sa.text(
                        "SELECT feature_id FROM codex_feature_artifact_v1 "
                        "WHERE feature_id = ANY(:ids)"
                    ),
                    {"ids": list(artifact_feature_ids)},
                )
            ).scalars()
        )
        visible_proposals = set(
            (
                await connection.execute(
                    sa.text(
                        "SELECT id FROM codex_roadmap_curation_proposal_v1 WHERE id = ANY(:ids)"
                    ),
                    {"ids": list(proposal_ids.values())},
                )
            ).scalars()
        )

    assert visible_features == {feature_ids["red feature"]}
    assert visible_artifacts == {feature_ids["red feature"]}
    assert visible_proposals == {proposal_ids["visible"]}


async def test_plan_only_subpartition_is_visible_in_brain_entity_contract(
    engine: AsyncEngine,
) -> None:
    red_base = _key("plan-red")
    red_child = f"{red_base}:agent"
    plan_id = uuid.uuid4()

    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO project_contexts "
                "(project_key, name, description, project_group) "
                "VALUES (:key, :key, 'plan-only scope proof', 'red')"
            ),
            {"key": red_base},
        )
        await connection.execute(
            sa.text(
                "INSERT INTO indexed_plans "
                "(id, file_path, title, plan_type, project_key, content_hash, content) "
                "VALUES (:id, :path, 'plan-only child', 'plan', :key, :hash, 'proof')"
            ),
            {
                "id": plan_id,
                "path": f"/tmp/{plan_id}.md",
                "key": red_child,
                "hash": uuid.uuid4().hex,
            },
        )
        visible = (
            (
                await connection.execute(
                    sa.text(
                        "SELECT id FROM codex_brain_entity_v1 WHERE type = 'plan' AND id = :id"
                    ),
                    {"id": plan_id},
                )
            )
            .scalars()
            .all()
        )

    assert visible == [plan_id]


async def test_artifact_insert_waits_for_merge_lock_then_rejects_archived_target(
    engine: AsyncEngine,
) -> None:
    project_key = _key("artifact-fence")
    feature_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO features (id, project_key, name, description) "
                "VALUES (:id, :key, 'merge loser', 'artifact fence proof')"
            ),
            {"id": feature_id, "key": project_key},
        )

    locker = await engine.connect()
    transaction = await locker.begin()
    try:
        await locker.execute(
            sa.text(
                "UPDATE features SET status = 'archived', status_updated_at = NOW() WHERE id = :id"
            ),
            {"id": feature_id},
        )

        async def insert_artifact() -> None:
            async with engine.begin() as connection:
                await connection.execute(
                    sa.text(
                        "INSERT INTO feature_artifacts "
                        "(feature_id, artifact_type, artifact_id, similarity_score) "
                        "VALUES (:feature, 'learning', :artifact, 0.9)"
                    ),
                    {"feature": feature_id, "artifact": artifact_id},
                )

        pending = asyncio.create_task(insert_artifact())
        await asyncio.sleep(0.1)
        assert not pending.done()
        await transaction.commit()

        with pytest.raises(sa.exc.DBAPIError):
            await asyncio.wait_for(pending, timeout=5)
    finally:
        if transaction.is_active:
            await transaction.rollback()
        await locker.close()

    async with engine.connect() as connection:
        count = await connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM feature_artifacts "
                "WHERE feature_id = :feature AND artifact_id = :artifact"
            ),
            {"feature": feature_id, "artifact": artifact_id},
        )
    assert count == 0


async def test_ticket_participants_cannot_change_after_creation(engine: AsyncEngine) -> None:
    ticket_id = uuid.uuid4()
    from_project = _key("immutable-from")
    to_project = _key("immutable-to")

    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO tickets (id, kind, title, body, from_project, to_project) "
                "VALUES (:id, 'request', 'immutable participants', 'scope fence', "
                ":from_project, :to_project)"
            ),
            {
                "id": ticket_id,
                "from_project": from_project,
                "to_project": to_project,
            },
        )

    async with engine.begin() as connection:
        await connection.execute(
            sa.text("UPDATE tickets SET status = 'in_progress' WHERE id = :id"),
            {"id": ticket_id},
        )

    with pytest.raises(sa.exc.DBAPIError, match="ticket participants are immutable"):
        async with engine.begin() as connection:
            await connection.execute(
                sa.text("UPDATE tickets SET to_project = :project WHERE id = :id"),
                {"id": ticket_id, "project": _key("replacement")},
            )

    async with engine.connect() as connection:
        participants = (
            await connection.execute(
                sa.text("SELECT from_project, to_project FROM tickets WHERE id = :id"),
                {"id": ticket_id},
            )
        ).one()
        status = await connection.scalar(
            sa.text("SELECT status FROM tickets WHERE id = :id"),
            {"id": ticket_id},
        )

    assert participants == (from_project, to_project)
    assert status == "in_progress"


@pytest.mark.parametrize("view", CONTRACT_COLUMNS)
async def test_codex_ro_has_select_on_contract_view(engine: AsyncEngine, view: str) -> None:
    async with engine.connect() as connection:
        allowed = await connection.scalar(
            sa.text("SELECT has_table_privilege('codex_ro', :view, 'SELECT')"),
            {"view": view},
        )

    assert allowed is True


@pytest.mark.parametrize("table", BASE_TABLES)
async def test_codex_ro_has_no_select_on_base_table(engine: AsyncEngine, table: str) -> None:
    async with engine.connect() as connection:
        allowed = await connection.scalar(
            sa.text("SELECT has_table_privilege('codex_ro', :table, 'SELECT')"),
            {"table": table},
        )

    assert allowed is False
