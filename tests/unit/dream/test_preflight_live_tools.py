"""The night must not run a phase whose prompt names a tool the LIVE server
does not expose.

Incident (2026-09-07 06:00): PR #102 rewrote ``phase_promote.md`` to call
``brain_promote_adr`` while the running ``brain-mcp-http`` process still only
had the old tools loaded — ``scripts/`` is read from the repository at run
time, ``src/`` only becomes live at the next restart.
``tests/unit/test_dream_prompts_only_name_real_tools.py`` cannot see this: it
compares prompts to the REPOSITORY catalogue, never to the running server.

This suite proves ``scripts.dream.preflight_live_tools`` closes that gap: it
asks a REAL loopback HTTP server for its ``tools/list`` (the same transport
Dream phases use), the same way ``tests/unit/mcp/test_dream_capability_http.py``
does — a socket proof, not a mock.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastmcp import FastMCP
from scripts.dream import preflight_live_tools as plt
from scripts.dream._agent_capability import MCP_TOKEN_ENV

from tests.unit.test_dream_prompts_only_name_real_tools import (
    _mentions as repo_guard_mentions,
)
from tests.unit.test_dream_prompts_only_name_real_tools import (
    _prompts as repo_guard_prompts,
)

_TEST_TOKEN = "preflight-live-tools-test-token"  # noqa: S105 — fixture-only, no live server


def _register_tools(server: FastMCP, names: Iterable[str]) -> None:
    for name in names:

        async def _handler() -> dict[str, bool]:
            return {"ok": True}

        server.tool(name=name)(_handler)


@asynccontextmanager
async def _serve_loopback(app: Any) -> AsyncIterator[str]:
    """Real uvicorn on an ephemeral loopback port — same pattern as
    tests/unit/mcp/test_dream_capability_http.py::_serve_loopback."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="on", log_level="error")
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _attempt in range(200):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("uvicorn loopback server did not start")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        listener.close()


@asynccontextmanager
async def _live_server(tool_names: Iterable[str]) -> AsyncIterator[str]:
    fake = FastMCP("preflight-live-tools-fixture")
    _register_tools(fake, tool_names)
    app = fake.http_app(transport="http", stateless_http=True, json_response=True)
    async with _serve_loopback(app) as url:
        yield url


def _write_prompt(directory: Path, phase: str, *tool_names: str) -> None:
    body = "\n".join(f"Call `{name}` now." for name in tool_names)
    (directory / f"phase_{phase}.md").write_text(
        f"You are the Dream Agent for phase {phase}.\n\n{body}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# (d) The extractor agrees with the repository guard — same source, same rule.
# ---------------------------------------------------------------------------


def test_phase_tool_mentions_agrees_with_the_repository_guard() -> None:
    for prompt in repo_guard_prompts():
        phase = prompt.stem.removeprefix("phase_")
        expected = repo_guard_mentions(prompt.read_text(encoding="utf-8"))
        assert plt.phase_tool_mentions()[phase] == expected, (
            f"{prompt.name}: preflight_live_tools disagrees with "
            "test_dream_prompts_only_name_real_tools on which tools it names"
        )


def test_phase_tool_mentions_reads_a_real_phase_from_disk() -> None:
    """Guard on the fixture: an empty universe would make every check vacuous."""
    mentions = plt.phase_tool_mentions()
    assert "scan" in mentions
    assert mentions["scan"], "phase_scan.md names no tool — the regex broke"


# ---------------------------------------------------------------------------
# Pure function — the comparison itself, no network involved.
# ---------------------------------------------------------------------------


def test_missing_tool_pairs_is_empty_when_every_mention_is_registered() -> None:
    mentions = {"scan": frozenset({"brain_search"}), "clean": frozenset({"brain_list"})}
    assert plt.missing_tool_pairs(mentions, frozenset({"brain_search", "brain_list"})) == []


def test_missing_tool_pairs_names_every_absent_pair_sorted() -> None:
    mentions = {
        "scan": frozenset({"brain_search", "brain_ghost"}),
        "clean": frozenset({"brain_ghost"}),
    }
    assert plt.missing_tool_pairs(mentions, frozenset({"brain_search"})) == [
        ("clean", "brain_ghost"),
        ("scan", "brain_ghost"),
    ]


# ---------------------------------------------------------------------------
# (a)/(b)/(c) — main() against a REAL loopback server.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_main_exits_zero_when_the_live_server_registers_every_named_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_prompt(tmp_path, "scan", "brain_widget_alpha")
    monkeypatch.setenv(MCP_TOKEN_ENV, _TEST_TOKEN)

    async with _live_server(["brain_widget_alpha"]) as url:
        rc = await asyncio.to_thread(
            plt.main, ["--url", url, "--prompt-dir", str(tmp_path), "--timeout", "5"]
        )

    assert rc == 0
    assert "OK" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_main_names_the_missing_pair_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exact incident: a prompt names a tool the running server lacks."""
    _write_prompt(tmp_path, "promote", "brain_promote_adr")
    monkeypatch.setenv(MCP_TOKEN_ENV, _TEST_TOKEN)

    # The live server only has the OLD tool loaded — src/ has not been
    # restarted yet, exactly like the incident.
    async with _live_server(["brain_propose_adr"]) as url:
        rc = await asyncio.to_thread(
            plt.main, ["--url", url, "--prompt-dir", str(tmp_path), "--timeout", "5"]
        )

    assert rc == plt.FAIL_EXIT_CODE
    err = capsys.readouterr().err
    assert "promote" in err
    assert "brain_promote_adr" in err


@pytest.mark.asyncio
async def test_main_fails_closed_with_a_named_reason_when_the_server_is_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_prompt(tmp_path, "scan", "brain_widget_alpha")
    monkeypatch.setenv(MCP_TOKEN_ENV, _TEST_TOKEN)

    # Bind-then-close: guaranteed nothing listens on this port.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    rc = await asyncio.wait_for(
        asyncio.to_thread(
            plt.main,
            [
                "--url",
                f"http://127.0.0.1:{dead_port}/mcp",
                "--prompt-dir",
                str(tmp_path),
                "--timeout",
                "2",
            ],
        ),
        timeout=15,
    )

    assert rc == plt.FAIL_EXIT_CODE
    err = capsys.readouterr().err
    assert "could not reach the live MCP server" in err


@pytest.mark.asyncio
async def test_main_fails_closed_within_the_timeout_when_the_server_accepts_but_never_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The wedged-uvicorn shape, not the refused-connection shape above: a
    socket that ACCEPTS the TCP handshake (the kernel completes it from the
    listen backlog even though nothing ever calls ``accept()``) and then
    never answers. ``--timeout`` must bound the WHOLE round trip — connect +
    MCP handshake + list — not just the final ``list_tools()`` call. A
    server wedged this way is indistinguishable, at the socket level, from a
    live uvicorn whose event loop is stuck."""
    _write_prompt(tmp_path, "scan", "brain_widget_alpha")
    monkeypatch.setenv(MCP_TOKEN_ENV, _TEST_TOKEN)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    # Deliberately never accept()/serve: the connection succeeds, nothing
    # ever reads the request or writes a response.

    started = time.monotonic()
    try:
        try:
            rc = await asyncio.wait_for(
                asyncio.to_thread(
                    plt.main,
                    [
                        "--url",
                        f"http://127.0.0.1:{port}/mcp",
                        "--prompt-dir",
                        str(tmp_path),
                        "--timeout",
                        "2",
                    ],
                ),
                timeout=10,
            )
        except TimeoutError:
            pytest.fail(
                "main(['--timeout', '2']) did not return within 10s against a "
                "server that accepts the connection and never answers -- "
                "--timeout must bound connect+handshake, not just list_tools() "
                "(see fetch_live_tool_names)"
            )
    finally:
        listener.close()
    elapsed = time.monotonic() - started

    assert elapsed < 10, f"preflight took {elapsed:.1f}s for a --timeout=2 run"
    assert rc == plt.FAIL_EXIT_CODE
    err = capsys.readouterr().err
    assert "could not reach the live MCP server" in err


def test_main_fails_closed_when_the_token_is_not_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_prompt(tmp_path, "scan", "brain_widget_alpha")
    monkeypatch.delenv(MCP_TOKEN_ENV, raising=False)

    rc = plt.main(["--url", "http://127.0.0.1:1/mcp", "--prompt-dir", str(tmp_path)])

    assert rc == plt.FAIL_EXIT_CODE
    assert MCP_TOKEN_ENV in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The native-profile header: production runs BRAIN_MCP_PROFILE=compact, so a
# plain admin bearer without this header would see only the lifecycle +
# gateway tools and every phase would look broken. Proven against the real
# transform, not asserted in prose.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_live_tool_names_requests_the_native_profile() -> None:
    from brain_v42.mcp.tool_catalog import apply_tool_catalog_profile

    fake = FastMCP("preflight-live-tools-compact-fixture")
    _register_tools(fake, ["brain_widget_alpha"])
    apply_tool_catalog_profile(fake, "compact")
    app = fake.http_app(transport="http", stateless_http=True, json_response=True)

    async with _serve_loopback(app) as url:
        live_tools = await plt.fetch_live_tool_names(url, _TEST_TOKEN, timeout_seconds=5)

    assert "brain_widget_alpha" in live_tools, (
        "the compact default hid a registered tool — preflight_live_tools must "
        "ask for the native profile or it will report every phase as broken"
    )
