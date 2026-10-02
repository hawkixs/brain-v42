"""Measure the latest wet nightly claim-verification run."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import cast

import sqlalchemy as sa

from brain_v42.facts.model import FactTarget, NoObservationError
from brain_v42.facts.nightly import ISSUER_PREFIX, KEY_PREFIX
from brain_v42.facts.probe import SourceSession, ValueType
from brain_v42.facts.sources import PostgresSourceSession
from brain_v42.repositories.pg_claim_nightly import read_last_wet_verify_run


class ClaimsVerificationLastNightProbe:
    """Expose verdicts written by the latest wet run and current eligibility."""

    name: str = "claims_verification_last_night"
    definition_version: int = 1
    target: FactTarget = FactTarget.PRODUCTION
    ttl: timedelta = timedelta(seconds=60)
    timeout: timedelta = timedelta(seconds=3)
    briefing: bool = True
    policies: Mapping[str, int] = {}
    value_schema: Mapping[str, ValueType] = {
        "run_date": "string",
        "run_id": "int",
        "status": "string",
        "holds": "int",
        "falsified": "int",
        "unreadable": "int",
        "verdicts": "int",
        "eligible_now": "int",
    }

    async def measure(self, source: SourceSession) -> Mapping[str, object]:
        """Use the database transaction clock so both counts share a source snapshot."""
        session = cast(PostgresSourceSession, source).session
        now = await session.scalar(sa.text("SELECT now()"))
        if now is None:
            raise ValueError("database transaction clock is unavailable")
        run = await read_last_wet_verify_run(session, ISSUER_PREFIX, KEY_PREFIX, now)
        if run is None:
            raise NoObservationError("dream_runs has no wet verify run")
        return {
            "run_date": run.run_date.isoformat(),
            "run_id": run.run_id,
            "status": run.status,
            "holds": run.holds,
            "falsified": run.falsified,
            "unreadable": run.unreadable,
            "verdicts": run.holds + run.falsified + run.unreadable,
            "eligible_now": run.eligible_now,
        }
