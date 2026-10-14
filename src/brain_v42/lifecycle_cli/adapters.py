"""Adapt installed clients and identity-bound facts without owning checkpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from brain_v42.config import Settings
from brain_v42.db.tables import tickets
from brain_v42.facts.model import Measured, SourceIdentity, identity_matches
from brain_v42.facts.registry import FactRegistry
from brain_v42.facts.sources import PostgresSourceFactory
from brain_v42.knowledge import extraction
from brain_v42.services.knowledge_lifecycle_evidence import Mode

from .sources import Records


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def switch_refusal(settings: Settings, mode: str) -> str | None:
    if not settings.brain_knowledge_lifecycle_enabled:
        return "lifecycle_disabled"
    if mode == Mode.APPLY and settings.brain_knowledge_lifecycle_dry_run:
        return "lifecycle_dry_run_only"
    return None


class IdentityGates:
    """Use the facts declaration and comparison, never derive expected identity from a URL."""

    def __init__(
        self, settings: Settings, measure: Callable[[], Awaitable[SourceIdentity]]
    ) -> None:
        self.settings, self.measure = settings, measure

    async def check(self, mode: str) -> str | None:
        refusal = switch_refusal(self.settings, mode)
        if refusal:
            return refusal
        try:
            declared = self.settings.facts_production_identity()
            if declared is None:
                return "target_undeclared"
            expected = SourceIdentity.from_mapping(declared)
        except Exception:
            return "target_declaration_invalid"
        try:
            observed = await self.measure()
        except Exception:
            return "identity_unreadable"
        return None if identity_matches(observed, expected) else "target_mismatch"


def identity_reader(
    sessions: async_sessionmaker[AsyncSession],
) -> Callable[[], Awaitable[SourceIdentity]]:
    source = PostgresSourceFactory(sessions)

    async def measure() -> SourceIdentity:
        async with source() as snapshot:
            return await snapshot.identity()

    return measure


class PostgresEvidence:
    """Read tickets and measure facts; an operator-only decision has no inferred value."""

    def __init__(self, engine: AsyncEngine, registry: FactRegistry, gates: IdentityGates) -> None:
        self.engine, self.registry, self.gates = engine, registry, gates

    async def tickets(self) -> Records:
        refusal = await self.gates.check(Mode.DRY_RUN)
        if refusal:
            raise ValueError(refusal)
        async with self.engine.connect() as conn, conn.begin():
            await conn.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            rows = (
                (
                    await conn.execute(
                        sa.select(
                            tickets.c.id,
                            tickets.c.status,
                            tickets.c.resolved_at,
                            tickets.c.closed_at,
                        )
                    )
                )
                .mappings()
                .all()
            )
        records: Records = {}
        ambiguous: set[str] = set()
        for row in rows:
            prefix = str(row["id"])[:8]
            if prefix in records or prefix in ambiguous:
                records.pop(prefix, None)
                ambiguous.add(prefix)
                continue
            # Unsupported table states stay unknown to checks rather than being guessed.
            if row["status"] in {"open", "resolved", "closed", "wontfix"}:
                ended = row["resolved_at"] if row["status"] == "resolved" else row["closed_at"]
                records[prefix] = (row["status"], None if row["status"] == "open" else ended)
        return records

    async def facts(self) -> tuple[dict[str, str], dict[str, str]]:
        values: dict[str, str] = {}
        for name, field in (("alembic_head", "revision"), ("live_release_sha", "release_sha")):
            if name not in self.registry.names():
                continue
            measurement = await self.registry.measure(name)
            if isinstance(measurement, Measured):
                value = measurement.value.get(field)
                if isinstance(value, str):
                    values[name] = value
        # There is no machine-readable binding for the operator's Dream decision.
        return values, {}


class BatchStillProcessing(Exception):
    """Leave an unfinished batch resumable without inventing result checkpoints."""

    def __init__(self, batch_id: str, waited_seconds: float) -> None:
        super().__init__("batch_still_processing")
        self.batch_id = batch_id
        self.waited_seconds = waited_seconds


class SDKExtraction:
    """Keep SDK key handling lazy and blocking network calls off the event loop."""

    def __init__(
        self,
        factory: Callable[[], Any] = extraction.make_client,
        *,
        max_wait_seconds: float = 7200,
        poll_interval_seconds: float = 30,
        clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if max_wait_seconds <= 0:
            raise ValueError("max_wait_seconds must be positive")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.factory = factory
        self.max_wait_seconds = max_wait_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.clock = clock
        self.sleep = sleep if sleep is not None else asyncio.sleep
        self.client: Any = None

    async def _client(self) -> Any:
        if self.client is None:
            self.client = await asyncio.to_thread(self.factory)
        return self.client

    async def count(self, manifest: extraction.Manifest) -> extraction.TokenCounts:
        if not manifest.requests:
            return extraction.TokenCounts(extraction.manifest_digest(manifest), {}, 0)
        return await asyncio.to_thread(extraction.count, manifest, await self._client())

    async def submit(self, manifest: extraction.Manifest) -> str:
        return await asyncio.to_thread(extraction.submit, manifest, await self._client())

    async def collect(
        self, batch_id: str, manifest: extraction.Manifest
    ) -> extraction.CollectedResults:
        client = await self._client()
        started = self.clock()
        last_error: Exception | None = None
        while self.clock() - started < self.max_wait_seconds:
            try:
                batch = await asyncio.to_thread(client.messages.batches.retrieve, batch_id)
            except Exception as exc:
                last_error = exc
            else:
                last_error = None
                if batch.processing_status == "ended":
                    return await asyncio.to_thread(extraction.collect, batch_id, manifest, client)
            remaining = self.max_wait_seconds - (self.clock() - started)
            if remaining > 0:
                await self.sleep(min(self.poll_interval_seconds, remaining))
        raise BatchStillProcessing(batch_id, self.clock() - started) from last_error

    async def aclose(self) -> None:
        if self.client is not None:
            await asyncio.to_thread(self.client.close)
