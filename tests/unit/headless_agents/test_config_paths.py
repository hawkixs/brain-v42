"""Where configuration and state live, and what may not be read (spec 0.5.0 §3.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from headless_agents.config_paths import ConfigPathError, config_dir, config_file, state_dir


def test_absolute_xdg_config_home_is_used(tmp_path: Path) -> None:
    base = tmp_path / "xdg"
    (base / "ha").mkdir(parents=True)
    got = config_dir({"XDG_CONFIG_HOME": str(base)}, home=tmp_path / "home")
    assert got == (base / "ha").resolve()


def test_relative_xdg_config_home_is_ignored(tmp_path: Path) -> None:
    home = tmp_path / "home"
    got = config_dir({"XDG_CONFIG_HOME": "relative/cfg"}, home=home)
    assert got == (home / ".config" / "ha").resolve()


def test_relative_xdg_state_home_is_ignored(tmp_path: Path) -> None:
    home = tmp_path / "home"
    got = state_dir({"XDG_STATE_HOME": "rel"}, home=home)
    assert got == (home / ".local" / "state" / "ha").resolve()


def test_absolute_xdg_state_home_is_used(tmp_path: Path) -> None:
    base = tmp_path / "state"
    assert state_dir({"XDG_STATE_HOME": str(base)}, home=tmp_path) == (base / "ha").resolve()


def test_a_config_file_linking_outside_the_directory_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    directory = home / ".config" / "ha"
    directory.mkdir(parents=True)
    repo_file = tmp_path / "repo" / "roles.toml"
    repo_file.parent.mkdir()
    repo_file.write_text("[x]\nprovider = 'codex'\n")
    (directory / "roles.toml").symlink_to(repo_file)
    with pytest.raises(ConfigPathError, match=r"roles\.toml.*points to .*repo/roles\.toml"):
        config_file("roles.toml", {}, home=home)


def test_a_dangling_link_is_refused_not_read_as_absent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    directory = home / ".config" / "ha"
    directory.mkdir(parents=True)
    (directory / "roles.toml").symlink_to(tmp_path / "elsewhere" / "roles.toml")
    with pytest.raises(ConfigPathError, match="outside the configuration directory"):
        config_file("roles.toml", {}, home=home)


def test_a_symlinked_config_directory_is_followed_once(tmp_path: Path) -> None:
    home = tmp_path / "home"
    real = tmp_path / "dotfiles" / "ha"
    real.mkdir(parents=True)
    (real / "roles.toml").write_text("")
    (home / ".config").mkdir(parents=True)
    (home / ".config" / "ha").symlink_to(real)
    assert config_file("roles.toml", {}, home=home) == (real / "roles.toml").resolve()


def test_an_absent_file_is_none(tmp_path: Path) -> None:
    assert config_file("roles.toml", {}, home=tmp_path) is None
