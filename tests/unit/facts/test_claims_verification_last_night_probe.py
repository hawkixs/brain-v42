"""Contracts for the measured nightly claim-verification fact."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from brain_v42.facts import FactTarget, nightly
from brain_v42.facts.probe import check_value_schema
from brain_v42.facts.probes.claims_verification_last_night import ClaimsVerificationLastNightProbe
from brain_v42.repositories.pg_claim_nightly import LAST_WET_VERIFY_RUN_SQL
from tests.integration.db.bench_claim_nightly import RUN_QUERY


def test_query_binds_the_declared_issuer_and_key_prefixes() -> None:
    sql = LAST_WET_VERIFY_RUN_SQL.text
    assert "CAST(:issuer_prefix AS text) || r.id" in sql
    assert "CAST(:key_prefix AS text) || to_char(r.run_date, 'YYYY-MM-DD')" in sql
    assert "dream:verify:" not in sql
    assert "dream-verify:v1:" not in sql
    assert "LEFT JOIN knowledge_claims c ON true" in sql
    assert "LEFT JOIN LATERAL" in sql
    assert "LIMIT 1" in sql
    assert RUN_QUERY is LAST_WET_VERIFY_RUN_SQL


async def test_probe_describes_and_measures_the_wet_run(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[object] = []

    async def reader(session: object, issuer_prefix: str, key_prefix: str, now: object):
        received.extend((session, issuer_prefix, key_prefix, now))
        return SimpleNamespace(
            run_id=812,
            run_date=date(2026, 9, 27),
            status="partial",
            holds=11,
            falsified=1,
            unreadable=2,
            eligible_now=3,
        )

    monkeypatch.setattr(
        "brain_v42.facts.probes.claims_verification_last_night.read_last_wet_verify_run", reader
    )
    clock = datetime(2026, 9, 28, tzinfo=UTC)

    class ClockSession:
        async def scalar(self, statement: object) -> datetime:
            assert "now()" in str(statement)
            return clock

    session = ClockSession()
    source = SimpleNamespace(session=session)
    probe = ClaimsVerificationLastNightProbe()

    value = await probe.measure(source)  # type: ignore[arg-type]

    assert probe.name == "claims_verification_last_night"
    assert probe.definition_version == 1
    assert probe.target is FactTarget.PRODUCTION
    assert probe.ttl.total_seconds() == 60
    assert probe.timeout.total_seconds() == 3
    assert probe.briefing is True
    assert probe.policies == {}
    assert probe.value_schema == {
        "run_date": "string",
        "run_id": "int",
        "status": "string",
        "holds": "int",
        "falsified": "int",
        "unreadable": "int",
        "verdicts": "int",
        "eligible_now": "int",
    }
    assert received[0:3] == [session, nightly.ISSUER_PREFIX, nightly.KEY_PREFIX]
    assert received[3] == clock
    assert value == {
        "run_date": "2026-09-27",
        "run_id": 812,
        "status": "partial",
        "holds": 11,
        "falsified": 1,
        "unreadable": 2,
        "verdicts": 14,
        "eligible_now": 3,
    }
    check_value_schema(value, probe.value_schema)


async def test_probe_raises_when_no_wet_run_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reader(session: object, issuer_prefix: str, key_prefix: str, now: object):
        return None

    monkeypatch.setattr(
        "brain_v42.facts.probes.claims_verification_last_night.read_last_wet_verify_run", reader
    )

    class ClockSession:
        async def scalar(self, statement: object) -> datetime:
            return datetime(2026, 9, 28, tzinfo=UTC)

    with pytest.raises(ValueError, match="no wet verify run"):
        await ClaimsVerificationLastNightProbe().measure(SimpleNamespace(session=ClockSession()))  # type: ignore[arg-type]
