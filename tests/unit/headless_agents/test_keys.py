"""keys.toml: the file that carries an HTTP preset's API key (decision 0612592e, ticket 4e16c0f4).

The process environment keeps priority; otherwise ``~/.config/ha/keys.toml`` names, per
preset, a ``.env`` file owned by the user and readable by no one else, from which ha
reads that preset's variable only. No message ever carries the value.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from headless_agents import keys
from headless_agents.keys import KeysError, preset_key

SECRET = f"fake-{uuid.uuid4().hex}"


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".config" / "ha").mkdir(parents=True)
    return home


def _env_file(path: Path, text: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def _declare(home: Path, text: str) -> None:
    (home / ".config" / "ha" / "keys.toml").write_text(text)


def test_the_environment_keeps_priority(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _env_file(home / "or.env", "OPENROUTER_API_KEY=from-the-file\n")
    _declare(home, 'openrouter = "~/or.env"\n')
    found = preset_key("openrouter", {"HOME": str(home), "OPENROUTER_API_KEY": SECRET})
    assert found is not None and found.value == SECRET and found.source == "environment"


def test_a_declared_file_supplies_the_key(tmp_path: Path) -> None:
    home = _home(tmp_path)
    path = _env_file(home / ".config/red/openrouter.env", f"OPENROUTER_API_KEY={SECRET}\n")
    _declare(home, 'openrouter = "~/.config/red/openrouter.env"\n')
    found = preset_key("openrouter", {"HOME": str(home)})
    assert found is not None and found.value == SECRET and found.source == str(path)


@pytest.mark.parametrize(
    "line",
    [
        f"export MISTRAL_API_KEY={SECRET}",
        f'MISTRAL_API_KEY="{SECRET}"',
        f"MISTRAL_API_KEY='{SECRET}'",
        f"  MISTRAL_API_KEY = {SECRET}  ",
    ],
)
def test_the_env_file_forms_a_shell_sources_are_read(tmp_path: Path, line: str) -> None:
    home = _home(tmp_path)
    _env_file(home / "m.env", f"# a comment\nOTHER=x\n\n{line}\n")
    _declare(home, f'mistral = "{home / "m.env"}"\n')
    found = preset_key("mistral", {"HOME": str(home)})
    assert found is not None and found.value == SECRET


def test_only_the_presets_own_variable_is_read(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _env_file(home / "all.env", f"NVIDIA_API_KEY={SECRET}\n")
    _declare(home, 'openrouter = "~/all.env"\n')
    with pytest.raises(KeysError, match="does not define OPENROUTER_API_KEY"):
        preset_key("openrouter", {"HOME": str(home)})


def test_an_undeclared_preset_has_no_key(tmp_path: Path) -> None:
    home = _home(tmp_path)
    assert preset_key("nvidia", {"HOME": str(home)}) is None
    _declare(home, 'openrouter = "~/or.env"\n')
    assert preset_key("nvidia", {"HOME": str(home)}) is None


def test_without_home_no_file_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller's environment decides; the process's own HOME is never guessed."""
    home = _home(tmp_path)
    _env_file(home / "or.env", f"OPENROUTER_API_KEY={SECRET}\n")
    _declare(home, 'openrouter = "~/or.env"\n')
    monkeypatch.setenv("HOME", str(home))
    assert preset_key("openrouter", {}) is None


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_a_file_others_can_read_is_refused(tmp_path: Path, mode: int) -> None:
    home = _home(tmp_path)
    path = _env_file(home / "or.env", f"OPENROUTER_API_KEY={SECRET}\n", mode=mode)
    _declare(home, 'openrouter = "~/or.env"\n')
    with pytest.raises(KeysError, match="0600") as refused:
        preset_key("openrouter", {"HOME": str(home)})
    assert str(path) in str(refused.value) and SECRET not in str(refused.value)


def test_a_file_owned_by_another_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    _env_file(home / "or.env", f"OPENROUTER_API_KEY={SECRET}\n")
    _declare(home, 'openrouter = "~/or.env"\n')
    someone_else = os.getuid() + 1
    monkeypatch.setattr(keys.os, "getuid", lambda: someone_else)
    with pytest.raises(KeysError, match="not owned by"):
        preset_key("openrouter", {"HOME": str(home)})


def test_a_missing_declared_file_is_refused_naming_it(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _declare(home, 'openrouter = "~/nowhere.env"\n')
    with pytest.raises(KeysError, match="nowhere.env"):
        preset_key("openrouter", {"HOME": str(home)})


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ('codex = "~/c.env"\n', "not an HTTP preset"),
        ('openai-compat = "~/c.env"\n', "not an HTTP preset"),
        ("openrouter = 7\n", "must be a path"),
        ('openrouter = "relative/or.env"\n', "absolute or start with ~/"),
        ("[openrouter]\n", "must be a path"),
        ("openrouter = \n", "keys.toml"),
    ],
)
def test_keys_toml_is_validated(tmp_path: Path, text: str, rule: str) -> None:
    home = _home(tmp_path)
    _declare(home, text)
    with pytest.raises(KeysError, match=rule):
        preset_key("openrouter", {"HOME": str(home)})


def test_keys_toml_is_read_from_the_configuration_directory_only(tmp_path: Path) -> None:
    """Spec 3.3: a keys.toml linked in from a repository is refused, never read."""
    home = _home(tmp_path)
    planted = tmp_path / "repo" / "keys.toml"
    planted.parent.mkdir()
    planted.write_text('openrouter = "~/or.env"\n')
    (home / ".config" / "ha" / "keys.toml").symlink_to(planted)
    with pytest.raises(KeysError, match="outside the configuration directory"):
        preset_key("openrouter", {"HOME": str(home)})


def test_an_unknown_preset_is_a_programming_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown HTTP preset"):
        preset_key("codex", {"HOME": str(_home(tmp_path))})
