"""Own process resources above the module DAG, leaving orchestration unchanged."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import Generator
from typing import TextIO

import httpx
import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from brain_v42.config import get_settings
from brain_v42.facts.composition import build_fact_registry_from_settings
from brain_v42.repositories.pg_knowledge_lifecycle import PgKnowledgeLifecycleRepo
from brain_v42.services.knowledge_lifecycle_evidence import RunReport
from brain_v42.services.knowledge_lifecycle_pipeline import LifecyclePipeline

from .adapters import IdentityGates, PostgresEvidence, SDKExtraction, SystemClock, identity_reader
from .command import ArgumentRefusal, execute, parse_args, render_report
from .sources import GitHubSources


class GitHubAuthentication(httpx.Auth):
    """Read only the configured optional variable and send it only to GitHub REST."""

    def __init__(self, variable: str | None) -> None:
        if variable is not None and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable) is None:
            raise ValueError("invalid_github_token_variable_name")
        self.variable = variable

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        if self.variable and request.url.host == "api.github.com":
            credential = os.environ.get(self.variable)
            if credential:
                request.headers["Authorization"] = "Bearer " + credential
        yield request


async def main_async(argv: list[str], stdout: TextIO | None = None) -> int:
    output = stdout if stdout is not None else sys.stdout
    engine, registry = None, None
    client = SDKExtraction()
    try:
        try:
            parse_args(argv)
        except ArgumentRefusal:
            render_report(
                RunReport("", "", "refused", failure_reason="invalid_arguments"),
                "--json" in argv,
                output,
            )
            return 2
        settings = get_settings()
        engine = create_async_engine(settings.postgres_url, echo=False, hide_parameters=True)
        sessions = async_sessionmaker(engine)
        gates = IdentityGates(settings, identity_reader(sessions))
        registry = build_fact_registry_from_settings(settings, sessions)
        database = PostgresEvidence(engine, registry, gates)
        # This variable configures a NAME, never a credential value. Config.py is
        # deliberately limited to the two lifecycle killswitches in this change.
        auth = GitHubAuthentication(os.environ.get("BRAIN_KNOWLEDGE_LIFECYCLE_GITHUB_TOKEN_ENV"))
        async with httpx.AsyncClient(auth=auth, timeout=httpx.Timeout(30, connect=10)) as http:
            sources = GitHubSources(http, database)
            pipeline = LifecyclePipeline(engine, client, sources, SystemClock(), gates)

            async def read_mode(run_id: str) -> str:
                assert engine is not None
                async with engine.connect() as conn:
                    run = await PgKnowledgeLifecycleRepo().read_run(conn, run_id)
                if run["counts"].get("origin_main_sha"):
                    sources.pin_origin(run["counts"]["origin_main_sha"])
                return str(run["mode"])

            return await execute(argv, settings, pipeline, read_mode, stdout=output)
    except Exception as exc:
        structlog.get_logger(__name__).error(
            "lifecycle.setup_refused", error_type=type(exc).__name__
        )
        render_report(
            RunReport("", "", "refused", failure_reason="configuration_unavailable"),
            "--json" in argv,
            output,
        )
        return 2
    finally:
        closers = [client.aclose]
        if registry is not None:
            closers.append(registry.aclose)
        if engine is not None:
            closers.append(engine.dispose)
        for close in closers:
            try:
                await close()
            except Exception as exc:
                structlog.get_logger(__name__).error(
                    "lifecycle.cleanup_failed", error_type=type(exc).__name__
                )


def main() -> int:
    structlog.configure(
        processors=[structlog.processors.add_log_level, structlog.processors.JSONRenderer()],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )
    return asyncio.run(main_async(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
