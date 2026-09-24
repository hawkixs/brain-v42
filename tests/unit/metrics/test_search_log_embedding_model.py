"""`record_search_log` attributes each row to the embedding model serving it.

Ticket 4fac067a (judging the codestral trial) and operator decision `1669d429`:
`search_log` gained a nullable `embedding_model` column (migration 057) and this
is its insert path. The value must come from the SAME live identity as the
settings (`config.py`'s `embedding_backend`/`embedding_model`) — never a
caller-supplied argument, and never the `SecretStr` API key sitting right next
to it in `Settings`.

The migration round trip itself (column shape, no-backfill on pre-057 rows) is
proved against PostgreSQL in
`tests/integration/db/test_migration_057_search_log_embedding_model.py`; this
file covers only the Python insert path, which needs no database.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from brain_v42.metrics.collector import MetricsCollector

pytestmark = pytest.mark.asyncio


class _FakeSecretStr:
    """Stands in for `pydantic.SecretStr` without importing pydantic here."""

    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value

    def __str__(self) -> str:  # pragma: no cover - mirrors SecretStr's own repr
        return "**********"


class _FakeSettings:
    """The two fields `record_search_log` may read: nothing else exists on it.

    If the insert path ever reached past `.embedding_model` for anything else,
    an `AttributeError` here would say so immediately.
    """

    def __init__(self, embedding_model: str, api_key: str = "sk-do-not-leak") -> None:
        self.embedding_model = embedding_model
        self.embedding_api_key = _FakeSecretStr(api_key)


class _FakeSession:
    """Captures the one INSERT `record_search_log` issues, nothing more."""

    def __init__(self) -> None:
        self.sql: str = ""
        self.params: dict[str, Any] = {}

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def execute(self, statement: object, params: dict[str, Any]) -> None:
        self.sql = str(statement)
        self.params = params

    async def commit(self) -> None:
        return None


@pytest.fixture
def collector() -> MetricsCollector:
    return MetricsCollector(engine=MagicMock(), session_factory=MagicMock())


async def _record(collector: MetricsCollector, session: _FakeSession) -> None:
    with patch.object(collector, "_session_factory", return_value=session):
        await collector.record_search_log(
            tool_name="brain_search",
            project_key="brain-v42",
            result_count=3,
            top_score=0.9,
            avg_score=0.8,
            latency_ms=42.0,
        )


class TestTheConfiguredModelLandsInTheInsert:
    async def test_a_search_logged_under_model_x_stores_x(
        self, collector: MetricsCollector
    ) -> None:
        session = _FakeSession()
        with patch(
            "brain_v42.metrics.collector_db.get_settings",
            return_value=_FakeSettings("codestral-trial"),
        ):
            await _record(collector, session)

        assert "embedding_model" in session.sql
        assert session.params["model"] == "codestral-trial"

    async def test_the_value_tracks_the_live_setting_not_a_hardcoded_default(
        self, collector: MetricsCollector
    ) -> None:
        """Regression guard: a literal (like `collector.py`'s old `"model"` bug,
        ticket 3a4ed612) would pass the test above for the wrong reason."""
        session = _FakeSession()
        with patch(
            "brain_v42.metrics.collector_db.get_settings",
            return_value=_FakeSettings("qodo"),
        ):
            await _record(collector, session)

        assert session.params["model"] == "qodo"


class TestTheSecretNeverReachesTheInsert:
    async def test_the_api_key_is_absent_from_both_sql_and_params(
        self, collector: MetricsCollector
    ) -> None:
        session = _FakeSession()
        with patch(
            "brain_v42.metrics.collector_db.get_settings",
            return_value=_FakeSettings("codestral-trial", api_key="sk-super-secret"),
        ):
            await _record(collector, session)

        assert "sk-super-secret" not in session.sql
        assert "sk-super-secret" not in session.params.values()
        assert not any("api_key" in key.lower() for key in session.params)
