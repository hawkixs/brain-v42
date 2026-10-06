"""The observer engine bounds every statement, lock wait and idle transaction."""

import asyncio
from unittest.mock import MagicMock

import pytest

import brain_v42.delivery_observer.__main__ as observer_main


class _EngineBuilt(Exception):
    pass


def test_observer_engine_server_settings_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_create(url, **kwargs):
        captured.update(kwargs)
        raise _EngineBuilt

    monkeypatch.setattr(observer_main, "create_async_engine", fake_create)
    settings = MagicMock()
    settings.postgres_url.get_secret_value.return_value = "postgresql+asyncpg://u:p@h:1/d"

    with pytest.raises(_EngineBuilt):
        asyncio.run(
            observer_main._execute(settings, once=True, project_key=None, stop=asyncio.Event())
        )

    assert captured["connect_args"]["command_timeout"] == 15
    assert captured["connect_args"]["server_settings"] == {
        "application_name": "brain-v42-delivery-observer",
        "statement_timeout": "15000",
        "lock_timeout": "5000",
        "idle_in_transaction_session_timeout": "60000",
    }
