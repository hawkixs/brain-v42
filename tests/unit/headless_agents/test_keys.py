"""keys.toml: the file that carries an HTTP preset's API key (decision 0612592e, ticket 4e16c0f4).

The process environment keeps priority; otherwise ``~/.config/ha/keys.toml`` names, per
preset, a ``.env`` file owned by the user and readable by no one else, from which ha
reads that preset's variable only. No message ever carries the value.
"""

from __future__ import annotations

import ast
import faulthandler
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

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


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("K=abc # note", "abc"),
        ('K="abc" # note', "abc"),
        ("export K = abc   #note", "abc"),
        ("K=abc#def", "abc#def"),
        ('K="a # b"', "a # b"),
    ],
)
def test_an_inline_comment_is_not_part_of_the_key(tmp_path: Path, line: str, expected: str) -> None:
    home = _home(tmp_path)
    path = _env_file(home / "or.env", f"{line}\n")
    _declare(home, 'openrouter = "~/or.env"\n')
    assert keys._read_variable(path, "K") == expected


def test_a_key_defined_twice_is_refused_naming_the_file_and_the_variable(
    tmp_path: Path,
) -> None:
    second = f"{SECRET}-second"
    path = _env_file(tmp_path / "key.env", f'K={SECRET}\nexport K="{second}"\n')
    with pytest.raises(KeysError) as refused:
        keys._read_variable(path, "K")
    message = str(refused.value)
    assert str(path) in message and "K" in message and "1" in message and "2" in message
    assert SECRET not in message and second not in message


def test_an_empty_first_definition_and_a_second_is_still_twice(tmp_path: Path) -> None:
    path = _env_file(tmp_path / "key.env", "K=\nK=b\n")
    with pytest.raises(KeysError, match="defined 2 times"):
        keys._read_variable(path, "K")


def test_a_comment_after_an_empty_value_does_not_define_a_key(tmp_path: Path) -> None:
    path = _env_file(tmp_path / "key.env", "K= # note\n")
    with pytest.raises(KeysError, match="does not define K"):
        keys._read_variable(path, "K")


def test_trailing_text_after_a_closing_quote_is_refused(tmp_path: Path) -> None:
    path = _env_file(tmp_path / "key.env", 'K="abc" junk\n')
    with pytest.raises(KeysError) as refused:
        keys._read_variable(path, "K")
    assert f"{path}:1" in str(refused.value) and "abc" not in str(refused.value)


def test_a_fifo_env_file_is_refused_without_blocking(tmp_path: Path) -> None:
    path = tmp_path / "key.env"
    os.mkfifo(path)
    faulthandler.dump_traceback_later(5, exit=True)
    try:
        with pytest.raises(KeysError, match="not a regular file"):
            keys._read_variable(path, "K")
    finally:
        faulthandler.cancel_dump_traceback_later()


def test_other_variables_are_ignored(tmp_path: Path) -> None:
    path = _env_file(tmp_path / "key.env", "OTHER=x\nOTHER=y\nK=v\n")
    assert keys._read_variable(path, "K") == "v"


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


def test_a_key_file_that_is_a_symlink_is_refused(tmp_path: Path) -> None:
    """Codex review of #215: a link is checked through its target, and whoever controls
    the link's directory could swap it; the key file itself must be the declared path."""
    home = _home(tmp_path)
    target = _env_file(home / "real.env", f"OPENROUTER_API_KEY={SECRET}\n")
    (home / "or.env").symlink_to(target)
    _declare(home, 'openrouter = "~/or.env"\n')
    with pytest.raises(KeysError, match="symbolic link"):
        preset_key("openrouter", {"HOME": str(home)})


@pytest.mark.parametrize("mode", [0o602, 0o666])
def test_a_keys_toml_others_can_write_is_refused(tmp_path: Path, mode: int) -> None:
    """Codex review of #215: whoever can edit keys.toml chooses which file ha reads."""
    home = _home(tmp_path)
    _env_file(home / "or.env", f"OPENROUTER_API_KEY={SECRET}\n")
    _declare(home, 'openrouter = "~/or.env"\n')
    (home / ".config" / "ha" / "keys.toml").chmod(mode)
    with pytest.raises(KeysError, match="writable by others"):
        preset_key("openrouter", {"HOME": str(home)})


def test_a_keys_toml_a_foreign_group_can_write_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    _declare(home, 'openrouter = "~/or.env"\n')
    (home / ".config" / "ha" / "keys.toml").chmod(0o620)
    monkeypatch.setattr(
        keys.grp, "getgrgid", lambda _gid: SimpleNamespace(gr_name="staff", gr_mem=["alice"])
    )
    with pytest.raises(KeysError, match="writable by others"):
        preset_key("openrouter", {"HOME": str(home)})


def test_a_keys_toml_writable_by_the_users_own_group_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured: umask 002 with a user private group creates 0664 files; that group is
    the user alone, so refusing it would refuse the machine's default."""
    home = _home(tmp_path)
    _env_file(home / "or.env", f"OPENROUTER_API_KEY={SECRET}\n")
    _declare(home, 'openrouter = "~/or.env"\n')
    (home / ".config" / "ha" / "keys.toml").chmod(0o664)
    gid = (home / ".config" / "ha" / "keys.toml").stat().st_gid
    monkeypatch.setattr(keys.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_name="u"))
    monkeypatch.setattr(keys.grp, "getgrgid", lambda _gid: SimpleNamespace(gr_name="u", gr_mem=[]))
    monkeypatch.setattr(keys.pwd, "getpwall", lambda: [SimpleNamespace(pw_name="u", pw_gid=gid)])
    found = preset_key("openrouter", {"HOME": str(home)})
    assert found is not None and found.value == SECRET


def test_a_group_named_like_the_user_but_with_members_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    _declare(home, 'openrouter = "~/or.env"\n')
    (home / ".config" / "ha" / "keys.toml").chmod(0o664)
    monkeypatch.setattr(keys.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_name="u"))
    monkeypatch.setattr(
        keys.grp, "getgrgid", lambda _gid: SimpleNamespace(gr_name="u", gr_mem=["bob"])
    )
    with pytest.raises(KeysError, match="writable by others") as refused:
        preset_key("openrouter", {"HOME": str(home)})
    assert SECRET not in str(refused.value)


def test_a_group_with_another_primary_member_is_not_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    _declare(home, 'openrouter = "~/or.env"\n')
    path = home / ".config" / "ha" / "keys.toml"
    path.chmod(0o664)
    gid = path.stat().st_gid
    monkeypatch.setattr(keys.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_name="owner"))
    monkeypatch.setattr(
        keys.pwd,
        "getpwall",
        lambda: [
            SimpleNamespace(pw_name="owner", pw_gid=gid),
            SimpleNamespace(pw_name="other", pw_gid=gid),
        ],
    )
    monkeypatch.setattr(
        keys.grp, "getgrgid", lambda _gid: SimpleNamespace(gr_name="owner", gr_mem=[])
    )
    with pytest.raises(KeysError, match="writable by others"):
        preset_key("openrouter", {"HOME": str(home)})


def test_a_keys_toml_linked_inside_the_configuration_directory_is_refused(tmp_path: Path) -> None:
    home = _home(tmp_path)
    config = home / ".config" / "ha"
    (config / "real.toml").write_text('openrouter = "~/or.env"\n')
    (config / "keys.toml").symlink_to(config / "real.toml")
    with pytest.raises(KeysError, match="symbolic link"):
        preset_key("openrouter", {"HOME": str(home)})


def test_an_unknown_group_is_not_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _home(tmp_path)
    _declare(home, 'openrouter = "~/or.env"\n')
    (home / ".config" / "ha" / "keys.toml").chmod(0o664)
    monkeypatch.setattr(keys.grp, "getgrgid", lambda _gid: (_ for _ in ()).throw(KeyError()))
    with pytest.raises(KeysError, match="writable by others"):
        preset_key("openrouter", {"HOME": str(home)})


def test_a_keys_toml_that_is_a_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    home = _home(tmp_path)
    os.mkfifo(home / ".config" / "ha" / "keys.toml")
    faulthandler.dump_traceback_later(5, exit=True)
    try:
        with pytest.raises(KeysError, match="not a regular file"):
            preset_key("openrouter", {"HOME": str(home)})
    finally:
        faulthandler.cancel_dump_traceback_later()


def test_a_keys_toml_that_is_a_directory_is_refused(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / ".config" / "ha" / "keys.toml").mkdir()
    with pytest.raises(KeysError, match="not a regular file"):
        preset_key("openrouter", {"HOME": str(home)})


def test_a_keys_toml_swapped_for_a_link_after_resolution_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    _declare(home, 'openrouter = "~/or.env"\n')
    path = home / ".config" / "ha" / "keys.toml"
    target = home / "copy.toml"
    target.write_text(path.read_text())
    original = keys.config_file

    def swap(*args: object, **kwargs: object) -> Path | None:
        resolved = original(*args, **kwargs)  # type: ignore[arg-type]
        path.unlink()
        path.symlink_to(target)
        return resolved

    monkeypatch.setattr(keys, "config_file", swap)
    with pytest.raises(KeysError, match="symbolic link"):
        preset_key("openrouter", {"HOME": str(home)})


def test_keys_py_reads_through_descriptors_only() -> None:
    tree = ast.parse(Path(keys.__file__).read_text())
    forbidden = {"read_text", "read_bytes", "stat", "lstat", "exists", "is_file"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        assert node.func.attr not in forbidden
        if node.func.attr == "open":
            assert isinstance(node.func.value, ast.Name) and node.func.value.id == "os"


def test_a_keys_toml_readable_by_others_is_accepted(tmp_path: Path) -> None:
    """keys.toml holds paths, never a key: reading it reveals nothing to protect."""
    home = _home(tmp_path)
    _env_file(home / "or.env", f"OPENROUTER_API_KEY={SECRET}\n")
    _declare(home, 'openrouter = "~/or.env"\n')
    (home / ".config" / "ha" / "keys.toml").chmod(0o644)
    found = preset_key("openrouter", {"HOME": str(home)})
    assert found is not None and found.value == SECRET


def test_a_keys_toml_owned_by_another_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    _declare(home, 'openrouter = "~/or.env"\n')
    someone_else = os.getuid() + 1
    monkeypatch.setattr(keys.os, "getuid", lambda: someone_else)
    with pytest.raises(KeysError, match=r"keys\.toml: not owned by"):
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
