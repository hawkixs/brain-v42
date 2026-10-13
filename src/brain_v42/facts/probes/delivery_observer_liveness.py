"""Measure whether the delivery observer is holding its lease and making progress."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import cast

from brain_v42.facts.model import FactTarget
from brain_v42.facts.probe import SourceSession, ValueType
from brain_v42.facts.sources import PostgresSourceSession
from brain_v42.repositories.pg_delivery_queue import read_observer_liveness


class DeliveryObserverLivenessProbe:
    """Publish queue progress and lock ownership from one production database."""

    name: str = "delivery_observer_liveness"
    definition_version: int = 1
    target: FactTarget = FactTarget.PRODUCTION
    ttl: timedelta = timedelta(seconds=15)
    timeout: timedelta = timedelta(seconds=3)
    briefing: bool = True
    policies: Mapping[str, int] = {"overdue_after_seconds": 600}
    value_schema: Mapping[str, ValueType] = {
        "lock_held": "bool",
        "overdue": "int",
        "last_confirmation_age_seconds": "null|int",
        "healthy": "bool",
    }

    async def measure(self, source: SourceSession) -> Mapping[str, object]:
        """Translate a repository snapshot without embedding database logic here."""
        value = await read_observer_liveness(
            cast(PostgresSourceSession, source).session,
            overdue_after_seconds=self.policies["overdue_after_seconds"],
        )
        return {
            "lock_held": value.lock_held,
            "overdue": value.overdue,
            "last_confirmation_age_seconds": value.last_confirmation_age_seconds,
            "healthy": value.healthy,
        }
