"""Ephemeral HOMEs and credential materialisation, driven by a profile.

The Dream's agy rail and the extract rescue link used to build their HOMEs
with two functions whose shape was fixed by the Dream (a scoped Brain server,
a ``dream-phase-guard`` hook). Here the same directories are composed from a
:class:`CapabilityProfile`: no MCP server means an empty ``mcpServers``, no
guard means no ``hooks.json``, and the credential list -- symlinked or copied
``0600`` -- is whatever the caller declared.
"""

from __future__ import annotations

import importlib.resources
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from headless_agents import sandbox
from headless_agents.profile import (
    CapabilityProfile,
    Credentials,
    McpServer,
    ToolGuard,
    Workspace,
)
from headless_agents.sandbox import build_ephemeral_home


def _real_home(tmp_path: Path) -> Path:
    real_home = tmp_path / "real-home"
    (real_home / ".x").mkdir(parents=True)
    (real_home / ".x" / "token").write_text("secret-token\n", encoding="utf-8")
    (real_home / ".x" / "token").chmod(0o644)
    return real_home


class TestEphemeralRoot:
    def test_prefers_an_existing_xdg_runtime_dir(self, tmp_path: Path) -> None:
        assert sandbox.ephemeral_root({"XDG_RUNTIME_DIR": str(tmp_path)}) == tmp_path

    def test_none_when_unset_or_missing(self, tmp_path: Path) -> None:
        assert sandbox.ephemeral_root({}) is None
        assert sandbox.ephemeral_root({"XDG_RUNTIME_DIR": str(tmp_path / "absent")}) is None


class TestMaterializeCredentials:
    def test_symlink_mode_links_existing_files_and_never_overwrites(self, tmp_path: Path) -> None:
        real_home = _real_home(tmp_path)
        home = tmp_path / "home"
        home.mkdir()
        credentials = Credentials(paths=(".x/token", ".x/absent"))

        sandbox.materialize_credentials(home=home, real_home=real_home, credentials=credentials)

        linked = home / ".x" / "token"
        assert linked.is_symlink()
        assert os.readlink(linked) == str(real_home / ".x" / "token")
        assert not (home / ".x" / "absent").exists()

        # A second call leaves the existing link alone.
        sandbox.materialize_credentials(home=home, real_home=real_home, credentials=credentials)
        assert linked.is_symlink()

    def test_copy_mode_copies_bytes_and_narrows_to_0600(self, tmp_path: Path) -> None:
        real_home = _real_home(tmp_path)
        home = tmp_path / "home"
        home.mkdir()

        sandbox.materialize_credentials(
            home=home,
            real_home=real_home,
            credentials=Credentials(paths=(".x/token",), mode="copy"),
        )

        copied = home / ".x" / "token"
        assert not copied.is_symlink()
        assert copied.read_text(encoding="utf-8") == "secret-token\n"
        assert stat.S_IMODE(copied.stat().st_mode) == 0o600

    def test_refuses_a_symlinked_source_that_escapes_the_real_home(self, tmp_path: Path) -> None:
        real_home = _real_home(tmp_path)
        outside = tmp_path / "outside-secret"
        outside.write_text("no", encoding="utf-8")
        (real_home / ".x" / "escape").symlink_to(outside)
        home = tmp_path / "home"
        home.mkdir()

        with pytest.raises(ValueError, match="outside"):
            sandbox.materialize_credentials(
                home=home,
                real_home=real_home,
                credentials=Credentials(paths=(".x/escape",), mode="copy"),
            )
        assert not (home / ".x" / "escape").exists()


def _server(**overrides: object) -> McpServer:
    fields: dict[str, object] = {
        "name": "example",
        "url": "http://127.0.0.1:8765/mcp",
        "bearer": SecretStr("scoped-token-placeholder"),
        "headers": {"X-Agent": "example-run", "X-Profile": "native"},
        "tools": ("example_search",),
    }
    fields.update(overrides)
    return McpServer(**fields)  # type: ignore[arg-type]


class TestBuildEphemeralHome:
    def test_writes_server_guard_settings_and_credentials(self, tmp_path: Path) -> None:
        real_home = _real_home(tmp_path)
        profile = CapabilityProfile(
            mcp=_server(),
            guard=ToolGuard(
                path=tmp_path / "guard.sh", hook_name="example-guard", timeout_seconds=7
            ),
            credentials=Credentials(paths=(".x/token",)),
        )

        home = sandbox.build_ephemeral_home(
            root=tmp_path / "root", name="example-run", profile=profile, real_home=real_home
        )

        assert home == tmp_path / "root" / "example-run"
        assert stat.S_IMODE(home.stat().st_mode) == 0o700
        config = home / ".gemini" / "config" / "mcp_config.json"
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
        # Key order is part of the contract: a consumer pins these files byte
        # for byte (tests/fixtures/agents_golden), and JSON key order is what
        # makes two dumps of the same mapping compare equal as strings.
        assert config.read_text(encoding="utf-8") == json.dumps(
            {
                "mcpServers": {
                    "example": {
                        "serverUrl": "http://127.0.0.1:8765/mcp",
                        "headers": {
                            "Authorization": "Bearer scoped-token-placeholder",
                            "X-Agent": "example-run",
                            "X-Profile": "native",
                        },
                        "trust": True,
                    }
                }
            }
        )
        hooks = json.loads((home / ".gemini" / "config" / "hooks.json").read_text())
        assert hooks == {
            "example-guard": {
                "PreToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [
                            {"type": "command", "command": str(tmp_path / "guard.sh"), "timeout": 7}
                        ],
                    }
                ]
            }
        }
        settings = json.loads((home / ".gemini" / "antigravity-cli" / "settings.json").read_text())
        assert settings == {"enableTelemetry": False, "trustedWorkspaces": [str(home)]}
        assert (home / ".x" / "token").is_symlink()

    def test_no_server_and_no_guard_means_empty_servers_and_no_hooks_file(
        self, tmp_path: Path
    ) -> None:
        home = sandbox.build_ephemeral_home(
            root=tmp_path, name="bare", profile=CapabilityProfile(), real_home=tmp_path / "rh"
        )
        config = json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text())
        assert config == {"mcpServers": {}}
        assert not (home / ".gemini" / "config" / "hooks.json").exists()

    def test_a_server_without_a_bearer_value_cannot_be_written_literally(
        self, tmp_path: Path
    ) -> None:
        profile = CapabilityProfile(mcp=_server(bearer=None))
        with pytest.raises(ValueError, match="bearer"):
            sandbox.build_ephemeral_home(
                root=tmp_path, name="x", profile=profile, real_home=tmp_path / "rh"
            )


class TestBuildToollessHome:
    def test_declares_no_server_no_guard_no_settings(self, tmp_path: Path) -> None:
        real_home = _real_home(tmp_path)
        home = sandbox.build_toolless_home(
            root=tmp_path / "root",
            name="toolless",
            real_home=real_home,
            credentials=Credentials(paths=(".x/token",)),
        )
        assert home == tmp_path / "root" / "toolless"
        assert stat.S_IMODE(home.stat().st_mode) == 0o700
        config = home / ".gemini" / "config" / "mcp_config.json"
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
        assert json.loads(config.read_text()) == {"mcpServers": {}}
        assert not (home / ".gemini" / "config" / "hooks.json").exists()
        assert not (home / ".gemini" / "antigravity-cli" / "settings.json").exists()
        assert (home / ".x" / "token").is_symlink()


class TestSandboxEnvironment:
    def test_rebuilds_from_scratch_inheriting_only_path(self, tmp_path: Path) -> None:
        env = sandbox.sandbox_environment(
            tmp_path, environ={"PATH": "/opt/bin:/usr/bin", "HOME": "/real", "SECRET": "x"}
        )
        assert env == {
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "PATH": "/opt/bin:/usr/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }

    def test_falls_back_to_a_system_path_when_the_parent_has_none(self, tmp_path: Path) -> None:
        env = sandbox.sandbox_environment(tmp_path, environ={})
        assert env["PATH"] == sandbox.FALLBACK_PATH


class TestWorkspaceHome:
    """A workspace run gets the package-owned guard, not the caller's."""

    def test_home_without_workspace_is_unchanged(self, tmp_path: Path) -> None:
        profile = CapabilityProfile(guard=ToolGuard(path=Path("/abs/guard.sh")))
        a = build_ephemeral_home(root=tmp_path / "a", name="h", profile=profile, real_home=tmp_path)
        b = build_ephemeral_home(
            root=tmp_path / "b", name="h", profile=profile, real_home=tmp_path, workspace=None
        )
        for rel in (
            ".gemini/config/hooks.json",
            ".gemini/config/mcp_config.json",
            ".gemini/antigravity-cli/settings.json",
        ):
            assert (a / rel).read_text().replace(str(tmp_path / "a"), "R") == (
                b / rel
            ).read_text().replace(str(tmp_path / "b"), "R")

    def test_home_with_workspace_installs_the_package_guard(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        home = build_ephemeral_home(
            root=tmp_path / "r",
            name="h",
            profile=CapabilityProfile(),
            real_home=tmp_path,
            workspace=Workspace(path=ws, write=True),
        )
        guard = home / ".gemini/config/workspace_guard.py"
        assert guard.stat().st_mode & 0o777 == 0o700
        assert guard.read_text().splitlines()[0] == f"#!{sys.executable}"
        assert json.loads((home / ".gemini/config/workspace-guard.json").read_text()) == {
            "root": str(ws),
            "write": True,
            "shell": False,
        }
        hooks = json.loads((home / ".gemini/config/hooks.json").read_text())
        assert hooks["workspace-guard"]["PreToolUse"][0]["hooks"][0]["command"] == str(guard)
        settings = json.loads((home / ".gemini/antigravity-cli/settings.json").read_text())
        assert settings["trustedWorkspaces"] == [str(home), str(ws)]

    def test_installed_guard_runs_under_the_ephemeral_home(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "a.txt").write_text("x")
        home = build_ephemeral_home(
            root=tmp_path / "r",
            name="h",
            profile=CapabilityProfile(),
            real_home=tmp_path,
            workspace=Workspace(path=ws),
        )
        guard = home / ".gemini/config/workspace_guard.py"
        payload = json.dumps(
            {"toolCall": {"name": "view_file", "args": {"AbsolutePath": str(ws / "a.txt")}}}
        )
        out = subprocess.run(
            [str(guard)],
            input=payload,
            capture_output=True,
            text=True,
            env={"HOME": str(home), "PATH": os.environ["PATH"]},
            check=True,
        )
        assert json.loads(out.stdout) == {"decision": "allow"}

    def test_a_workspace_home_declares_only_the_profiles_server(self, tmp_path: Path) -> None:
        """The guard allows ``call_mcp_tool`` unconditionally: any server beyond
        the profile's would be a door the workspace guard never looks at."""
        ws = tmp_path / "ws"
        ws.mkdir()
        home = build_ephemeral_home(
            root=tmp_path / "r",
            name="h",
            profile=CapabilityProfile(mcp=_server()),
            real_home=tmp_path,
            workspace=Workspace(path=ws),
        )
        config = json.loads((home / ".gemini/config/mcp_config.json").read_text())
        assert list(config) == ["mcpServers"]
        assert list(config["mcpServers"]) == [_server().name]

    def test_a_caller_guard_and_a_workspace_do_not_compose(self, tmp_path: Path) -> None:
        profile = CapabilityProfile(guard=ToolGuard(path=Path("/abs/guard.sh")))
        with pytest.raises(ValueError, match="tool_guard"):
            build_ephemeral_home(
                root=tmp_path / "r",
                name="h",
                profile=profile,
                real_home=tmp_path,
                workspace=Workspace(path=tmp_path),
            )

    def test_the_guard_ships_as_a_package_resource(self) -> None:
        """The wheel must carry ``guards/``: the sandbox reads the guard through
        ``importlib.resources``, never through a source-tree path."""
        resource = importlib.resources.files("headless_agents.guards") / "agy_workspace.py"
        assert resource.is_file()
