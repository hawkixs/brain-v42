"""The v22 mint must only ever touch a disposable database, however the DSN is spelled."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "mint_recovery_contract_v22.py"


@pytest.fixture(scope="module")
def mint():
    spec = importlib.util.spec_from_file_location("mint_recovery_contract_v22", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:p@h:5433/brain",
        "postgresql://u:p@h:5433/brain_test",
        # The path a naive rsplit("/") misreads: asyncpg connects to `brain`.
        "postgresql://u:p@h:5433/brain?application_name=x/y",
        # Percent-encoding spells `brain` without the letters.
        "postgresql://u:p@h:5433/%62rain",
        "postgresql://u:p@h:5433/brain_v22_x?dbname=brain",
        "postgresql://u:p@h:5433/brain_v22_x?host=/tmp",
        "postgresql://u:p@h:5433/brain_v22_x/extra",
        "postgresql://u:p@h:5433/",
        "postgresql://u:p@h:5433/other",
    ],
)
def test_the_dsn_guard_refuses_what_is_not_a_disposable_database(mint, dsn: str) -> None:
    with pytest.raises(SystemExit):
        mint.assert_disposable_dsn(dsn)


def test_the_dsn_guard_accepts_a_disposable_database(mint) -> None:
    mint.assert_disposable_dsn("postgresql://u:p@h:5433/brain_v22_base_0123456789")


def test_the_connection_itself_is_verified_before_any_use(
    mint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the connection lands on `brain` whatever the DSN said, it is closed unused."""
    connection = AsyncMock()
    connection.fetchval.return_value = "brain"
    monkeypatch.setattr(mint.asyncpg, "connect", AsyncMock(return_value=connection))

    with pytest.raises(SystemExit):
        asyncio.run(mint.connect("postgresql://u:p@h:5433/brain_v22_x"))

    connection.close.assert_awaited_once()
    connection.fetch.assert_not_called()
