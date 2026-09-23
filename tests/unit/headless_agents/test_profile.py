"""``CapabilityProfile``: the data a caller hands the runtime instead of policy.

Everything the Dream used to resolve from ``brain_v42.mcp.dream_capabilities``
-- which MCP server, which bearer, which tools, which guard, which
credentials -- arrives here as a value. The runtime validates the shape and
refuses the unsafe defaults (a non-loopback MCP URL, a credential path that
escapes the caller's HOME) at construction, so a provider never has to.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from headless_agents.profile import CapabilityProfile, Credentials, McpServer, ToolGuard, Workspace


def _server(**overrides: object) -> McpServer:
    fields: dict[str, object] = {
        "name": "example",
        "url": "http://127.0.0.1:8765/mcp",
        "bearer": SecretStr("token-placeholder"),
        "bearer_env_var": "EXAMPLE_TOKEN",
        "headers": {"X-Agent": "example-run"},
        "tools": ("example_search", "example_get"),
    }
    fields.update(overrides)
    return McpServer(**fields)  # type: ignore[arg-type]


class TestMcpServer:
    def test_is_frozen_and_keeps_its_fields(self) -> None:
        server = _server()
        assert server.name == "example"
        assert server.tools == ("example_search", "example_get")
        with pytest.raises(ValidationError):
            server.name = "other"  # type: ignore[misc]

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8765/mcp",
            "http://localhost:8765/mcp",
            "https://[::1]:8765/mcp",
        ],
    )
    def test_accepts_every_loopback_host(self, url: str) -> None:
        assert _server(url=url).url == url

    @pytest.mark.parametrize(
        "url",
        [
            "http://192.168.1.12:8765/mcp",
            "http://example.test/mcp",
            "ftp://127.0.0.1/mcp",
            "http://user:pw@127.0.0.1:8765/mcp",
            "http://127.0.0.1:8765/mcp#frag",
            "http://127.0.0.1:notaport/mcp",
            "",
        ],
    )
    def test_refuses_a_non_loopback_or_malformed_url_by_default(self, url: str) -> None:
        with pytest.raises(ValidationError, match="loopback"):
            _server(url=url)

    def test_a_remote_url_needs_an_explicit_opt_out(self) -> None:
        server = _server(url="https://mcp.example.test/mcp", require_loopback=False)
        assert server.url == "https://mcp.example.test/mcp"

    def test_bearer_never_shows_in_repr(self) -> None:
        text = repr(_server(bearer=SecretStr("very-secret-value")))
        assert "very-secret-value" not in text

    def test_bearer_may_be_absent_when_the_environment_already_carries_it(self) -> None:
        server = _server(bearer=None)
        assert server.bearer is None
        assert server.bearer_env_var == "EXAMPLE_TOKEN"

    def test_refuses_an_authorization_header_the_bearer_already_provides(self) -> None:
        with pytest.raises(ValidationError, match="Authorization"):
            _server(headers={"authorization": "Bearer twice"})

    def test_refuses_a_blank_name_or_a_blank_tool(self) -> None:
        with pytest.raises(ValidationError):
            _server(name=" ")
        with pytest.raises(ValidationError):
            _server(tools=("ok", ""))

    def test_tools_are_normalised_to_a_tuple_preserving_order(self) -> None:
        assert _server(tools=["b", "a"]).tools == ("b", "a")


class TestToolGuard:
    def test_defaults(self, tmp_path: Path) -> None:
        guard = ToolGuard(path=tmp_path / "guard.sh")
        assert guard.hook_name == "tool-guard"
        assert guard.timeout_seconds == 10

    def test_keeps_the_path_as_given(self) -> None:
        # Not normalised, not required absolute: callers pin the written
        # hooks.json against placeholder paths (see the class docstring).
        assert ToolGuard(path=Path("guard.sh")).path == Path("guard.sh")


class TestCredentials:
    def test_defaults_to_symlinks_and_no_paths(self) -> None:
        credentials = Credentials()
        assert credentials.paths == ()
        assert credentials.mode == "symlink"

    @pytest.mark.parametrize("bad", ["/etc/passwd", "../outside", "a/../../b", ""])
    def test_refuses_absolute_or_escaping_paths(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            Credentials(paths=(bad,))

    def test_copy_mode_is_explicit(self) -> None:
        assert Credentials(paths=(".x/token",), mode="copy").mode == "copy"


class TestCapabilityProfile:
    def test_the_empty_profile_reaches_nothing(self) -> None:
        profile = CapabilityProfile()
        assert profile.mcp is None
        assert profile.guard is None
        assert profile.credentials == Credentials()
        assert profile.environment_passthrough == ()

    def test_carries_every_part(self, tmp_path: Path) -> None:
        profile = CapabilityProfile(
            mcp=_server(),
            guard=ToolGuard(path=tmp_path / "guard.sh", hook_name="phase-guard"),
            credentials=Credentials(paths=(".x/token",)),
            environment_passthrough=("EXTRA_ONE", "EXTRA_TWO"),
        )
        assert profile.mcp is not None and profile.mcp.name == "example"
        assert profile.guard is not None and profile.guard.hook_name == "phase-guard"
        assert profile.credentials.paths == (".x/token",)
        assert profile.environment_passthrough == ("EXTRA_ONE", "EXTRA_TWO")

    def test_is_frozen(self) -> None:
        profile = CapabilityProfile()
        with pytest.raises(ValidationError):
            profile.mcp = _server()  # type: ignore[misc]


def test_workspace_defaults_to_read_only(tmp_path: Path) -> None:
    workspace = Workspace(path=tmp_path)
    assert (workspace.write, workspace.shell) == (False, False)


def test_workspace_path_must_be_absolute() -> None:
    with pytest.raises(ValidationError, match="absolute"):
        Workspace(path=Path("relative/dir"))


def test_workspace_path_must_exist(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="does not exist"):
        Workspace(path=tmp_path / "missing")


def test_workspace_path_must_be_a_directory(tmp_path: Path) -> None:
    file = tmp_path / "f"
    file.write_text("x", encoding="utf-8")
    with pytest.raises(ValidationError, match="not a directory"):
        Workspace(path=file)


def test_shell_requires_write(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="shell requires write"):
        Workspace(path=tmp_path, shell=True)


def test_shell_with_write_is_accepted(tmp_path: Path) -> None:
    assert Workspace(path=tmp_path, write=True, shell=True).shell is True


def test_profile_has_no_workspace_by_default() -> None:
    assert CapabilityProfile().workspace is None
