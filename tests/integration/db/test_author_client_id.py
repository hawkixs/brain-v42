"""Credential attribution reaches PostgreSQL through all six real write paths."""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastmcp import Client, FastMCP
from fastmcp.server.auth import AccessToken
from sqlalchemy.ext.asyncio import async_sessionmaker

from brain_v42.db.tables import METADATA
from brain_v42.mcp import client_principal
from brain_v42.mcp.tool_catalog import apply_tool_catalog_profile
from brain_v42.mcp.tools.brain_tools import register_tools
from brain_v42.mcp.tools.crud_tools import register_crud_tools
from brain_v42.mcp.tools.focus_slot_tools import register_focus_slot_tools
from brain_v42.repositories.pg_adr import PgADRRepo
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.repositories.pg_focus_slot import PgFocusSlotRepo
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.repositories.pg_runbook import PgRunbookRepo
from brain_v42.repositories.pg_snippet import PgSnippetRepo
from brain_v42.services.adr_service import ADRService
from brain_v42.services.decision_service import DecisionService
from brain_v42.services.focus_slot_service import FocusSlotService
from brain_v42.services.learning_service import LearningService
from brain_v42.services.runbook_service import RunbookService
from brain_v42.services.snippet_service import SnippetService
from tests.integration.db.test_delivery_focus_slots import ensure_project, ticket

pytestmark = pytest.mark.integration
CASES = [
    (
        "decisions",
        "brain_log_decision",
        {"title": "Choice", "context": "Context", "decision_made": "Choice", "reasoning": "Reason"},
    ),
    ("learnings", "brain_learn", {"topic": "Lesson", "insight": "Insight", "source": "test"}),
    (
        "snippets",
        "brain_save_snippet",
        {"title": "Parse", "intention": "Parse data", "code": "pass", "language": "python"},
    ),
    (
        "runbooks",
        "brain_create_runbook",
        {
            "title": "Deploy",
            "description": "Deploy safely",
            "trigger": "release",
            "steps": ["Build"],
        },
    ),
    (
        "adrs",
        "brain_propose_adr",
        {"title": "Choice", "context": "Context", "decision": "Choice", "consequences": "Effects"},
    ),
    ("focus_slots", "brain_slot_open", {"title": "Topic", "body": "Work"}),
]


@pytest.mark.parametrize("table_name,tool_name,arguments", CASES)
@pytest.mark.parametrize("author", [None, "credential-writer"])
@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_verified_author_is_stored_and_survives_update(
    private_head_engine, monkeypatch, table_name, tool_name, arguments, author, profile
):
    factory = async_sessionmaker(private_head_engine, expire_on_commit=False)
    project = f"authors-{uuid4().hex[:12]}"
    await ensure_project(factory, project)
    server = FastMCP("author-persistence")
    services = {
        "decision_svc": DecisionService(PgDecisionRepo(factory)),
        "learning_svc": LearningService(PgLearningRepo(factory)),
        "snippet_svc": SnippetService(PgSnippetRepo(factory)),
        "runbook_svc": RunbookService(PgRunbookRepo(factory)),
        "adr_svc": ADRService(PgADRRepo(factory)),
    }
    register_tools(server, **services, project_context_svc=MagicMock(), brain_svc=MagicMock())
    register_crud_tools(server, **services, session_factory=factory)
    register_focus_slot_tools(server, FocusSlotService(PgFocusSlotRepo(factory)))
    apply_tool_catalog_profile(server, profile)
    token = (
        None
        if author is None
        else AccessToken(
            token="opaque",
            client_id=author,
            scopes=["write"],
            claims={"credential_id": str(uuid4()), "issuers": []},
        )
    )
    monkeypatch.setattr(client_principal, "get_access_token", lambda: token)
    arguments = dict(arguments, project_key=project)
    if table_name == "focus_slots":
        identifier = await ticket(factory, project)
        arguments["anchors"] = [{"kind": "ticket", "ticket_id": str(identifier)}]
    async with Client(server) as client:

        async def invoke(name, args):
            if profile == "compact":
                return await client.call_tool("brain_call_tool", {"name": name, "arguments": args})
            return await client.call_tool(name, args)

        await invoke(tool_name, arguments)
        table = METADATA.tables[table_name]
        async with factory() as session:
            identifier, stored_author = (
                await session.execute(
                    sa.select(table.c.id, table.c.author_client_id).where(
                        table.c.project_key == project
                    )
                )
            ).one()
        assert stored_author == author
        token = AccessToken(
            token="another",
            client_id="second-writer",
            scopes=["write"],
            claims={"credential_id": str(uuid4()), "issuers": []},
        )
        if table_name == "focus_slots":
            mutations = [
                (tool_name, arguments),
                (
                    "brain_slot_close",
                    {"slot_id": str(identifier), "expected_revision": 0, "note": "Finished"},
                ),
            ]
        else:
            kind = {
                "decisions": "decision",
                "learnings": "learning",
                "snippets": "snippet",
                "runbooks": "runbook",
                "adrs": "adr",
            }[table_name]
            result = await invoke("brain_get", {"entity_type": kind, "entity_id": str(identifier)})
            text = "\n".join(block.text for block in result.content)
            assert (
                (f"author_client_id: {author}" in text)
                if author is not None
                else ("author_client_id:" not in text)
            )
            update_args = {
                "entity_type": kind,
                "entity_id": str(identifier),
                "fields": {"tags": ["edited"]},
            }
            mutations = [("brain_update", update_args)]
            if kind == "learning":
                mutations.append(("brain_validate_learning", {"learning_id": str(identifier)}))
            elif kind == "snippet":
                mutations.append(("brain_use_snippet", {"snippet_id": str(identifier)}))
            elif kind == "runbook":
                mutations.append(("brain_execute_runbook", {"runbook_id": str(identifier)}))
            elif kind == "adr":
                mutations.extend(
                    [
                        ("brain_accept_adr", {"adr_id": str(identifier)}),
                        ("brain_deprecate_adr", {"adr_id": str(identifier)}),
                    ]
                )
        for name, args in mutations:
            await invoke(name, args)
            async with factory() as session:
                assert (
                    await session.scalar(
                        sa.select(table.c.author_client_id).where(table.c.id == identifier)
                    )
                    == author
                )
        if table_name == "decisions":
            await invoke(
                "brain_supersede_decision",
                dict(arguments, old_decision_id=str(identifier), title="Replacement"),
            )
            async with factory() as session:
                assert (
                    await session.scalar(
                        sa.select(table.c.author_client_id).where(
                            table.c.project_key == project, table.c.id != identifier
                        )
                    )
                    == "second-writer"
                )
                assert (
                    await session.scalar(
                        sa.select(table.c.author_client_id).where(table.c.id == identifier)
                    )
                    == author
                )
