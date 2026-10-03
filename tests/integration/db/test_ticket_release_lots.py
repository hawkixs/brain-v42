"""Release lot aggregation includes only tickets assigned to the project."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from brain_v42.db.tables import project_contexts, tickets
from brain_v42.repositories.pg_ticket import PgTicketRepo
from tests.conftest import require_test_db_url

pytestmark = pytest.mark.asyncio


async def test_release_lots_aggregate_planned_ticket_statuses_per_executor():
    engine = create_async_engine(require_test_db_url())
    project = f"lot-test-{uuid4().hex}"
    other_project = f"{project}-other"
    try:
        async with engine.begin() as connection:
            await connection.execute(
                project_contexts.insert().values(
                    project_key=project,
                    name=project,
                    description="temporary release lot integration fixture",
                )
            )
            await connection.execute(
                tickets.insert(),
                [
                    {
                        "kind": "request",
                        "title": "open",
                        "body": "b",
                        "from_project": project,
                        "to_project": project,
                        "status": "open",
                        "target_release": "0.6.9",
                    },
                    {
                        "kind": "request",
                        "title": "resolved",
                        "body": "b",
                        "from_project": project,
                        "to_project": project,
                        "status": "resolved",
                        "target_release": "0.6.9",
                    },
                    {
                        "kind": "request",
                        "title": "progress",
                        "body": "b",
                        "from_project": project,
                        "to_project": project,
                        "status": "in_progress",
                        "target_release": "0.6.10",
                    },
                    {
                        "kind": "request",
                        "title": "wontfix",
                        "body": "b",
                        "from_project": project,
                        "to_project": project,
                        "status": "wontfix",
                        "target_release": "0.6.3",
                    },
                    {
                        "kind": "request",
                        "title": "unplanned",
                        "body": "b",
                        "from_project": project,
                        "to_project": project,
                        "status": "open",
                        "target_release": None,
                    },
                    {
                        "kind": "request",
                        "title": "other project",
                        "body": "b",
                        "from_project": project,
                        "to_project": other_project,
                        "status": "open",
                        "target_release": "0.6.9",
                    },
                ],
            )

        lots = await PgTicketRepo(async_sessionmaker(engine, expire_on_commit=False)).release_lots(
            project
        )

        assert {lot.target_release: (lot.open, lot.resolved, lot.wontfix) for lot in lots} == {
            "0.6.9": (1, 1, 0),
            "0.6.10": (1, 0, 0),
            "0.6.3": (0, 0, 1),
        }
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                tickets.delete().where(
                    tickets.c.to_project.in_((project, other_project))
                    | tickets.c.from_project.in_((project, other_project))
                )
            )
            await connection.execute(
                project_contexts.delete().where(
                    project_contexts.c.project_key.in_((project, other_project))
                )
            )
        await engine.dispose()
