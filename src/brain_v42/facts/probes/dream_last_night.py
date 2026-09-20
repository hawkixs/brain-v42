"""Expose the latest Dream-night aggregate as a verified production fact."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import cast

from brain_v42.facts.model import FactTarget
from brain_v42.facts.probe import SourceSession, ValueType
from brain_v42.facts.sources import PostgresSourceSession
from brain_v42.repositories.pg_dream_runs import read_last_night


class DreamLastNightProbe:
    """Publish scalar night evidence while Dream tools retain per-phase detail."""

    name: str = "dream_last_night"
    definition_version: int = 1
    target: FactTarget = FactTarget.PRODUCTION
    ttl: timedelta = timedelta(seconds=60)
    timeout: timedelta = timedelta(seconds=3)
    briefing: bool = False
    policies: Mapping[str, int] = {}
    value_schema: Mapping[str, ValueType] = {
        "run_date": "string",
        "rows": "int",
        "done": "int",
        "fail": "int",
        "timeout": "int",
        "partial": "int",
        "other": "int",
        "wet": "int",
        "dry": "int",
        "projects": "int",
        "finished_at_epoch": "int",
    }

    async def measure(self, source: SourceSession) -> Mapping[str, object]:
        """Translate the shared snapshot and make an absent night unreadable."""
        night = await read_last_night(cast(PostgresSourceSession, source).session)
        if night is None:
            raise ValueError("dream_runs has no night")
        return {
            "run_date": night.run_date.isoformat(),
            "rows": night.rows,
            "done": night.done,
            "fail": night.fail,
            "timeout": night.timeout,
            "partial": night.partial,
            "other": night.other,
            "wet": night.wet,
            "dry": night.dry,
            "projects": night.projects,
            "finished_at_epoch": int(night.finished_at.timestamp()),
        }
