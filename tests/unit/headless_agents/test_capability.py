"""Process-level primitives every rail shares, with no policy inside.

``brain_v42.agents.capability`` keeps the Dream policy (which registry, which
``(project, phase)`` bearer, whether enforcement is on); what moves here is
the mechanics under it: the ambient-variable allowlist, ``NO_PROXY`` merging,
loopback validation, the two exit codes and process-group termination.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from headless_agents import capability


def test_exit_codes_are_the_three_the_chain_is_written_around() -> None:
    assert capability.PROVIDER_FALLBACK_EXIT_CODE == 3
    assert capability.TIMEOUT_EXIT_CODE == 124
    # The night of 2026-09-19: a link that timed out with an empty stream was
    # read as an ordinary timeout, so the chain never moved past it. This code
    # says both things at once -- the deadline fired AND nothing could have
    # been written -- and only the second half is what lets the chain advance.
    assert capability.TIMEOUT_REPLAYABLE_EXIT_CODE == 4
    assert capability.FALLBACK_EXIT_CODES == frozenset({3, 4})
    # Distinct from every code a runner already returns, and from timeout(1)'s
    # own 124-127 band.
    assert len({0, 1, 2, 3, 4, 124, 125, 126, 127}) == 9


def test_base_allowlist_carries_locale_tls_proxy_and_paths_only() -> None:
    allowlist = capability.BASE_CHILD_ENV_ALLOWLIST
    assert {"HOME", "PATH", "LANG", "TERM", "NO_PROXY", "SSL_CERT_FILE"} <= allowlist
    assert "MCP_HTTP_TOKEN" not in allowlist
    assert "ANTHROPIC_API_KEY" not in allowlist
    assert "CODEX_HOME" not in allowlist


class TestMergedNoProxy:
    def test_appends_the_three_loopback_entries_once(self) -> None:
        merged = capability.merged_no_proxy({"NO_PROXY": "internal.test, localhost"})
        assert merged == "internal.test,localhost,127.0.0.1,::1"

    def test_reads_both_spellings_and_dedupes(self) -> None:
        merged = capability.merged_no_proxy({"NO_PROXY": "a", "no_proxy": "a,b"})
        assert merged == "a,b,127.0.0.1,localhost,::1"

    def test_empty_environment(self) -> None:
        assert capability.merged_no_proxy({}) == "127.0.0.1,localhost,::1"


class TestValidateLoopbackUrl:
    @pytest.mark.parametrize(
        "url", ["http://127.0.0.1:8765/mcp", "https://localhost/mcp", "http://[::1]:1/x"]
    )
    def test_accepts_loopback(self, url: str) -> None:
        capability.validate_loopback_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "http://10.0.0.1/mcp",
            "http://mcp.example.test/mcp",
            "file:///tmp/x",
            "http://u:p@127.0.0.1/mcp",
            "http://127.0.0.1/mcp#f",
            "http://127.0.0.1:port/mcp",
        ],
    )
    def test_refuses_everything_else(self, url: str) -> None:
        with pytest.raises(ValueError, match="loopback"):
            capability.validate_loopback_url(url)


class TestScopedEnvironment:
    def test_filters_to_the_allowlist_merges_no_proxy_and_applies_overrides(self) -> None:
        ambient = {
            "HOME": "/h",
            "PATH": "/usr/bin",
            "SECRET": "leak",
            "MCP_HTTP_TOKEN": "admin",
            "NO_PROXY": "x",
            "CODEX_HOME": "/c",
        }
        scoped = capability.scoped_environment(
            ambient,
            passthrough=("CODEX_HOME",),
            overrides={"MCP_HTTP_TOKEN": "scoped-token"},
        )
        assert scoped == {
            "HOME": "/h",
            "PATH": "/usr/bin",
            "NO_PROXY": "x,127.0.0.1,localhost,::1",
            "no_proxy": "x,127.0.0.1,localhost,::1",
            "CODEX_HOME": "/c",
            "MCP_HTTP_TOKEN": "scoped-token",
        }

    def test_an_override_never_leaks_from_the_ambient_environment_when_absent(self) -> None:
        scoped = capability.scoped_environment({"HOME": "/h", "MCP_HTTP_TOKEN": "admin"})
        assert "MCP_HTTP_TOKEN" not in scoped

    def test_returns_a_fresh_dict(self) -> None:
        ambient = {"HOME": "/h"}
        scoped = capability.scoped_environment(ambient)
        scoped["HOME"] = "/other"
        assert ambient["HOME"] == "/h"


def _sleeping_group() -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "time.sleep(60)",
        ],
        text=True,
        start_new_session=True,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_terminate_process_group_kills_leader_and_children(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capability, "TERMINATION_GRACE_SECONDS", 0.5)
    process = _sleeping_group()
    time.sleep(0.3)  # let the child spawn its own child
    capability.terminate_process_group(process)
    assert process.returncode is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(process.pid, 0)


def test_terminate_process_group_tolerates_an_already_gone_process() -> None:
    process = subprocess.Popen([sys.executable, "-c", "pass"], text=True, start_new_session=True)
    process.wait()
    capability.terminate_process_group(process)
    assert process.returncode == 0
