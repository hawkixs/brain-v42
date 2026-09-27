"""Unit tests for the G1 live test's codex-model resolution (0.5.2 lot 5).

Host rehearsal (2026-09-27) found the live version of ``_codex_model`` crashing with
``ImportError: cannot import name 'MODEL' from 'headless_agents.prove'`` -- the plan's
assumed model source never existed once lot 4a actually merged (PR #242 hard-codes no
model table; ``ha prove`` resolves through the operator's own ``models.toml``). These
tests pin the fix: resolution reuses the real machinery (``cli_models.load_models`` +
``config_paths.config_file``, the same calls ``cli._prove_models`` makes),
``HA_LIVE_CODEX_MODEL`` always overrides, and an import failure of that machinery
degrades to ``None`` (a skip at the call site in ``test_concurrency_live.py``), never a
crash.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import headless_agents.cli_models as cli_models
from tests.live.headless_agents import test_concurrency_live as g1


def test_the_override_env_var_wins_over_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HA_LIVE_CODEX_MODEL", "gpt-x-override")
    assert g1._codex_model() == "gpt-x-override"


def test_it_resolves_from_the_operators_models_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HA_LIVE_CODEX_MODEL", raising=False)
    ha_config = tmp_path / "config" / "ha"
    ha_config.mkdir(parents=True)
    (ha_config / "models.toml").write_text('codex = "gpt-6-luna"\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(g1, "REAL_HOME", tmp_path)
    assert g1._codex_model() == "gpt-6-luna"


def test_it_is_none_when_the_operator_declared_no_codex_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HA_LIVE_CODEX_MODEL", raising=False)
    ha_config = tmp_path / "config" / "ha"
    ha_config.mkdir(parents=True)
    (ha_config / "models.toml").write_text('claude = "haiku"\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(g1, "REAL_HOME", tmp_path)
    assert g1._codex_model() is None


def test_it_is_none_when_no_models_toml_exists_at_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HA_LIVE_CODEX_MODEL", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(g1, "REAL_HOME", tmp_path)
    assert g1._codex_model() is None


def test_it_degrades_to_none_when_the_resolution_surface_cannot_be_imported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact bug class the host rehearsal found: ``ImportError: cannot import name
    'MODEL' from 'headless_agents.prove'`` -- a module that imports fine but no longer
    has the name being imported from it. Deleting the attribute reproduces that exact
    shape (confirmed: this raises a plain ``ImportError``, never the narrower
    ``ModuleNotFoundError`` a missing MODULE would raise, and which the first version
    of ``_codex_model`` caught -- too narrow to survive this). ``_codex_model`` must
    degrade to ``None`` here, never crash.
    """
    monkeypatch.delenv("HA_LIVE_CODEX_MODEL", raising=False)
    monkeypatch.delattr(cli_models, "load_models")
    assert g1._codex_model() is None
