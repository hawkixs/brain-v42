"""Which model each link gets, and where the declared defaults are read (spec 0.5.0 §3.1, §3.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from headless_agents.cli_models import ModelsError, models_for


def _models_file(home: Path, text: str) -> Path:
    directory = home / ".config" / "ha"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "models.toml"
    path.write_text(text)
    return path


def test_a_relative_xdg_config_home_falls_back_to_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _models_file(home, 'codex = "from-home"\n')
    got = models_for((("codex", ""),), default="", environ={"XDG_CONFIG_HOME": "rel"}, home=home)
    assert got == {"codex": "from-home"}


def test_a_models_file_linking_into_a_repository_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".config" / "ha").mkdir(parents=True)
    planted = tmp_path / "repo" / "models.toml"
    planted.parent.mkdir()
    planted.write_text('codex = "planted"\n')
    (home / ".config" / "ha" / "models.toml").symlink_to(planted)
    with pytest.raises(ModelsError, match="outside the configuration directory"):
        models_for((("codex", ""),), default="", environ={}, home=home)
