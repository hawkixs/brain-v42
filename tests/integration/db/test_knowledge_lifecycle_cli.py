"""Run the public CLI dispatch against an isolated migrated PostgreSQL database."""

import io
import json
import tarfile
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from brain_v42.config import Settings
from brain_v42.db import tables as t
from brain_v42.facts.composition import build_fact_registry_from_settings
from brain_v42.facts.model import SourceIdentity
from brain_v42.facts.sources import PostgresSourceSession
from brain_v42.knowledge import extraction
from brain_v42.lifecycle_cli.adapters import (
    IdentityGates,
    PostgresEvidence,
    SystemClock,
    identity_reader,
)
from brain_v42.lifecycle_cli.command import execute
from brain_v42.lifecycle_cli.sources import GitHubSources
from brain_v42.services.knowledge_lifecycle_pipeline import LifecyclePipeline

pytestmark = pytest.mark.integration


class Client:
    async def count(self, manifest: extraction.Manifest) -> extraction.TokenCounts:
        return extraction.TokenCounts(extraction.manifest_digest(manifest), {}, 0)

    async def submit(self, manifest: extraction.Manifest) -> str:
        return "fake-batch"

    async def collect(
        self, batch_id: str, manifest: extraction.Manifest
    ) -> extraction.CollectedResults:
        assertion: extraction.Assertion = {
            "assertion_key": "claim",
            "quote": "gitlab-ci",
            "check": "fact",
            "repo": "brain-v42",
            "ref": "ci.system",
            "asserts": "exists",
            "claimed_status": "unknown",
            "fact_key": "ci.system",
            "claimed_value": "gitlab-ci",
            "centrality": "core",
            "temporal": "durable",
        }
        return extraction.CollectedResults(
            {
                r.custom_id: extraction.RequestResult(
                    extraction.ResultStatus.SUCCEEDED,
                    tuple(
                        extraction.Extraction(e.entity_type, e.entity_id, e.digest, (assertion,))
                        for e in r.entries
                    ),
                )
                for r in manifest.requests
            }
        )


async def test_lifecycle_cli_dry_run_json_has_no_events_or_cache(
    private_head_engine: AsyncEngine,
) -> None:
    assert private_head_engine.url.database not in {"brain", "brain_test"}
    project, entity_id = "cli-" + uuid4().hex, uuid4()
    async with private_head_engine.begin() as conn:
        await conn.execute(
            t.decisions.insert().values(
                id=entity_id,
                title="gitlab-ci",
                description="gitlab-ci",
                reasoning="claim",
                project_key=project,
                content_updated_at=datetime.now(UTC),
            )
        )
    sessions = async_sessionmaker(private_head_engine)

    async def identity() -> SourceIdentity:
        async with sessions() as session, session.begin():
            return await PostgresSourceSession(session).identity()

    declared = (await identity()).as_dict()
    settings = Settings(
        postgres_url="postgresql+asyncpg://unused/unused",
        _env_file=None,
        facts_production_identity_json=json.dumps(declared),
        brain_knowledge_lifecycle_enabled=True,
    )
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        body = b"runs-on: ubuntu-24.04"
        member = tarfile.TarInfo("brain/.github/workflows/ci.yml")
        member.size = len(body)
        tar.addfile(member, io.BytesIO(body))

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "codeload.github.com":
            return httpx.Response(200, content=stream.getvalue())
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": "a" * 40})
        return httpx.Response(200, json=[])

    registry = build_fact_registry_from_settings(settings, sessions)
    gates = IdentityGates(settings, identity_reader(sessions))
    database = PostgresEvidence(private_head_engine, registry, gates)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            pipeline = LifecyclePipeline(
                private_head_engine,
                Client(),
                GitHubSources(http, database),
                SystemClock(),
                gates,
            )
            output = io.StringIO()
            code = await execute(
                ["run", "--mode", "dry-run", "--scope", project, "--ceiling-usd", "1", "--json"],
                settings,
                pipeline,
                stdout=output,
            )
    finally:
        await registry.aclose()
    report = json.loads(output.getvalue())
    assert code == 0 and report["status"] == "applied" and report["mode"] == "dry_run"
    assert report["pending_by_state"] == {"refuted": 1}
    assert "gitlab-ci" not in output.getvalue()
    async with private_head_engine.connect() as conn:
        for table in (t.knowledge_lifecycle_events, t.knowledge_lifecycle_state):
            assert await conn.scalar(sa.select(sa.func.count()).select_from(table)) == 0
        assert (
            await conn.scalar(
                sa.select(t.knowledge_evaluations.c.complete).where(
                    t.knowledge_evaluations.c.entity_id == entity_id
                )
            )
            is True
        )
