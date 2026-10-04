"""Shared fixtures for the repository unit tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

_GUARDED_MODULES = (
    "pg_base",
    "pg_learning",
    "pg_snippet",
    "pg_runbook",
    "pg_adr",
)


@pytest.fixture(autouse=True)
def _capture_ledger_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the mock-session delete tests pin their own statement, not the ledger's.

    Those fakes answer every `execute` with one canned result, so the capture
    guard's two extra statements would read it as "captured". The guard itself is
    covered against PostgreSQL in `tests/integration/db` and, statement by
    statement, in `test_project_scoped_crud.py`, which overrides this fixture.
    """
    for module in _GUARDED_MODULES:
        monkeypatch.setattr(
            f"brain_v42.repositories.{module}.lock_unless_captured",
            AsyncMock(return_value=True),
        )
    # The decision delete locks its own rows, then asks only the ledger.
    monkeypatch.setattr(
        "brain_v42.repositories.pg_decision.refuse_if_captured", AsyncMock(return_value=None)
    )
