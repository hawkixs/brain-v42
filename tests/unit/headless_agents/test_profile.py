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
        # 0.4.0: ``allowed_networks=None`` replaces ``require_loopback=False``.
        server = _server(url="https://mcp.example.test/mcp", allowed_networks=None)
        assert server.url == "https://mcp.example.test/mcp"


class TestAllowedNetworks:
    """Spec 3.4: ``allowed_networks`` replaces the boolean ``require_loopback``."""

    def test_the_default_is_loopback_only(self) -> None:
        assert _server().allowed_networks == ("127.0.0.0/8", "::1/128")

    def test_a_listed_private_subnet_admits_an_ip_literal_inside_it(self) -> None:
        server = _server(url="http://10.8.0.5:8765/mcp", allowed_networks=("10.8.0.0/24",))
        assert server.url == "http://10.8.0.5:8765/mcp"

    def test_an_ip_literal_outside_the_listed_networks_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="outside"):
            _server(url="http://10.9.0.5:8765/mcp", allowed_networks=("10.8.0.0/24",))

    def test_an_ipv6_literal_is_matched(self) -> None:
        server = _server(url="http://[fd00::5]:8765/mcp", allowed_networks=("fd00::/8",))
        assert server.url == "http://[fd00::5]:8765/mcp"

    def test_a_host_name_is_never_resolved(self) -> None:
        with pytest.raises(ValidationError, match="IP literal"):
            _server(url="http://brain.lan:8765/mcp", allowed_networks=("10.8.0.0/24",))

    def test_localhost_is_accepted_iff_loopback_is_listed(self) -> None:
        assert _server(url="http://localhost:1/mcp", allowed_networks=("127.0.0.0/8",)).url
        with pytest.raises(ValidationError):
            _server(url="http://localhost:1/mcp", allowed_networks=("10.8.0.0/24",))

    def test_none_means_no_restriction(self) -> None:
        server = _server(url="https://mcp.example.test/mcp", allowed_networks=None)
        assert server.allowed_networks is None

    def test_an_empty_tuple_is_rejected_rather_than_read_as_no_restriction(self) -> None:
        with pytest.raises(ValidationError, match="None"):
            _server(allowed_networks=())

    def test_a_malformed_network_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _server(allowed_networks=("not-a-network",))

    def test_the_url_shape_is_still_checked_inside_an_allowed_network(self) -> None:
        for url in ("ftp://10.8.0.5/mcp", "http://u:p@10.8.0.5/mcp", "http://10.8.0.5/mcp#f"):
            with pytest.raises(ValidationError):
                _server(url=url, allowed_networks=("10.8.0.0/24",))

    def test_the_removed_require_loopback_fails_loudly(self) -> None:
        with pytest.raises(ValidationError, match="allowed_networks"):
            _server(require_loopback=False)

    def test_a_listed_non_loopback_literal_goes_to_no_proxy(self) -> None:
        from headless_agents.capability import scoped_environment
        from headless_agents.profile import mcp_no_proxy_hosts

        server = _server(url="http://10.8.0.5:8765/mcp", allowed_networks=("10.8.0.0/24",))
        assert mcp_no_proxy_hosts(server) == ("10.8.0.5",)
        env = scoped_environment(
            {"NO_PROXY": "corp.example"}, no_proxy_hosts=mcp_no_proxy_hosts(server)
        )
        assert env["NO_PROXY"].split(",") == [
            "corp.example",
            "127.0.0.1",
            "localhost",
            "::1",
            "10.8.0.5",
        ]

    def test_loopback_and_unrestricted_servers_add_nothing_to_no_proxy(self) -> None:
        from headless_agents.profile import mcp_no_proxy_hosts

        assert mcp_no_proxy_hosts(_server()) == ()
        assert mcp_no_proxy_hosts(_server(url="http://10.8.0.5/mcp", allowed_networks=None)) == ()
        assert mcp_no_proxy_hosts(None) == ()

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
