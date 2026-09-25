"""Named MCP profiles (spec 3.4): ``--mcp NAME`` maps one profile to ``CapabilityProfile.mcp``.

The file names servers and the NAME of the variable holding each bearer;
never a value. The package knows no server by name: ``brain`` exists only in
the operator's configuration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from headless_agents.mcp_profiles import (
    McpProfileError,
    default_profiles_path,
    load_profiles,
    mcp_server,
)

BRAIN_READ = """
[brain-read]
url = "http://127.0.0.1:8765/mcp"
bearer_env = "BRAIN_TOKEN"
tools = ["brain_search", "brain_get"]
"""


def _file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "mcp.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_profile_becomes_an_mcp_server(tmp_path: Path) -> None:
    server = mcp_server("brain-read", path=_file(tmp_path, BRAIN_READ), environ={})
    assert server.name == "brain-read"
    assert server.url == "http://127.0.0.1:8765/mcp"
    assert server.bearer_env_var == "BRAIN_TOKEN"
    assert server.tools == ("brain_search", "brain_get")
    assert server.bearer is None
    assert server.allowed_networks == ("127.0.0.0/8", "::1/128")


def test_the_bearer_value_is_read_from_the_named_variable(tmp_path: Path) -> None:
    server = mcp_server(
        "brain-read", path=_file(tmp_path, BRAIN_READ), environ={"BRAIN_TOKEN": "t-123"}
    )
    assert server.bearer is not None
    assert server.bearer.get_secret_value() == "t-123"


def test_optional_keys(tmp_path: Path) -> None:
    text = """
[lan]
name = "brain"
url = "http://10.8.0.5:8765/mcp"
bearer_env = "T"
tools = ["a"]
headers = { "X-Agent" = "ha" }
allowed_networks = ["10.8.0.0/24"]

[anywhere]
url = "https://mcp.example.test/mcp"
bearer_env = "T"
tools = ["a"]
allowed_networks = "any"
"""
    path = _file(tmp_path, text)
    lan = mcp_server("lan", path=path, environ={})
    assert lan.name == "brain"
    assert lan.headers == {"X-Agent": "ha"}
    assert lan.allowed_networks == ("10.8.0.0/24",)
    assert mcp_server("anywhere", path=path, environ={}).allowed_networks is None


def test_an_unknown_profile_names_the_known_ones(tmp_path: Path) -> None:
    with pytest.raises(McpProfileError, match="brain-read"):
        mcp_server("nope", path=_file(tmp_path, BRAIN_READ), environ={})


def test_a_missing_file_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(McpProfileError, match="no MCP profile file"):
        mcp_server("brain-read", path=tmp_path / "absent.toml", environ={})


@pytest.mark.parametrize("key", ["bearer", "token", "api_key", "password"])
def test_a_secret_value_in_the_file_is_refused(tmp_path: Path, key: str) -> None:
    text = BRAIN_READ + f'{key} = "sk-oops"\n'
    with pytest.raises(McpProfileError, match="bearer_env"):
        load_profiles(_file(tmp_path, text))


@pytest.mark.parametrize(
    "text",
    [
        '[p]\nurl = "http://127.0.0.1/mcp"\ntools = ["a"]\n',  # no bearer_env
        '[p]\nbearer_env = "T"\ntools = ["a"]\n',  # no url
        '[p]\nurl = "http://127.0.0.1/mcp"\nbearer_env = "T"\ntools = ["a"]\nbogus = 1\n',
        '[p]\nurl = "http://192.168.1.2/mcp"\nbearer_env = "T"\ntools = ["a"]\n',  # not loopback
        '[p]\nurl = "http://127.0.0.1/mcp"\nbearer_env = "T"\ntools = ["a"]\nallowed_networks = "all"\n',
        "p = 1\n",  # not a table
        "[p\n",  # not TOML
    ],
)
def test_an_invalid_profile_is_refused_with_its_name(tmp_path: Path, text: str) -> None:
    with pytest.raises(McpProfileError):
        load_profiles(_file(tmp_path, text))


def test_the_default_path_follows_xdg(tmp_path: Path) -> None:
    assert default_profiles_path({"XDG_CONFIG_HOME": str(tmp_path)}, home=Path("/h")) == (
        tmp_path / "ha" / "mcp.toml"
    )
    assert default_profiles_path({}, home=Path("/h")) == Path("/h/.config/ha/mcp.toml")


def test_a_relative_xdg_config_home_is_ignored(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert (
        default_profiles_path({"XDG_CONFIG_HOME": "rel"}, home=home)
        == (home / ".config" / "ha" / "mcp.toml").resolve()
    )


def test_a_profile_file_linking_into_a_repository_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".config" / "ha").mkdir(parents=True)
    planted = _file(tmp_path, BRAIN_READ)
    (home / ".config" / "ha" / "mcp.toml").symlink_to(planted)
    with pytest.raises(McpProfileError, match="outside the configuration directory"):
        mcp_server("brain-read", environ={}, home=home)
