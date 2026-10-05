"""Behavioral tests for the stdlib MCP HTTP watchdog probe."""

from __future__ import annotations

import errno
import http.client
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest
from scripts import mcp_http_watchdog as watchdog


@pytest.fixture(autouse=True)
def _no_real_systemctl(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that forgets its systemctl double must fail, not reach the real user
    manager: on a host running brain-mcp-http it would read, and past the failure
    threshold restart, the live unit."""

    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"a watchdog test reached the real systemctl: {args!r}")

    monkeypatch.setattr(watchdog.subprocess, "run", refuse)


def _connection_error(*args: object, **kwargs: object) -> object:
    raise ConnectionError()


class _Response:
    status = 200

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str = "active",
    opener: Callable[..., object] | None = None,
    systemctl: Callable[..., object] | None = None,
) -> tuple[int, list[list[str]], Path]:
    counter = tmp_path / "consecutive-failures"
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> object:
        calls.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout=("LoadState=loaded\nActiveState=" + state + "\n") if argv[2] == "show" else "",
        )

    result = watchdog.main(
        ["--port", "8765"],
        systemctl=systemctl or run,
        opener=opener or (lambda request, timeout: _Response()),
    )
    return result, calls, counter


@pytest.mark.parametrize("state", ["inactive", "activating", "deactivating", "reloading"])
def test_non_running_states_skip_probe_and_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    (tmp_path / "consecutive-failures").write_text("1")
    result, calls, counter = _run(tmp_path, monkeypatch, state)
    assert result == 0
    assert len(calls) == 1
    assert counter.read_text().strip() == "1"


def test_failed_state_resets_and_starts_without_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "consecutive-failures").write_text("1")
    call_options: list[dict[str, object]] = []
    argv_calls: list[list[str]] = []

    def systemctl(argv: list[str], **kwargs: object) -> object:
        argv_calls.append(argv)
        call_options.append(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout="LoadState=loaded\nActiveState=failed\n" if argv[2] == "show" else "",
        )

    result, _, counter = _run(tmp_path, monkeypatch, "failed", systemctl=systemctl)
    assert result == 0
    assert argv_calls == [
        [
            "systemctl",
            "--user",
            "show",
            "brain-mcp-http.service",
            "-p",
            "LoadState",
            "-p",
            "ActiveState",
        ],
        ["systemctl", "--user", "reset-failed", "brain-mcp-http.service"],
        ["systemctl", "--user", "--no-block", "start", "brain-mcp-http.service"],
    ]
    assert all("timeout" in options and options["timeout"] <= 10.0 for options in call_options)
    assert not counter.exists()


def test_unexpected_active_state_is_broken_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, calls, counter = _run(tmp_path, monkeypatch, "unknown")
    assert result == 3
    assert len(calls) == 1
    assert not counter.exists()


def test_healthy_probe_clears_counter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "consecutive-failures").write_text("1")
    result, _, counter = _run(tmp_path, monkeypatch)
    assert result == 0
    assert not counter.exists()


def test_first_unhealthy_probe_writes_one_without_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, calls, counter = _run(tmp_path, monkeypatch, opener=_connection_error)
    assert result == 0
    assert counter.read_text().strip() == "1"
    assert not any("restart" in call for call in calls)


def test_second_unhealthy_probe_restarts_and_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(tmp_path, monkeypatch, opener=_connection_error)
    result, calls, counter = _run(tmp_path, monkeypatch, opener=_connection_error)
    assert result == 0
    assert any("restart" in call for call in calls)
    assert not counter.exists()


def test_healthy_probe_between_failures_resets_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(tmp_path, monkeypatch, opener=_connection_error)
    _run(tmp_path, monkeypatch)
    result, calls, counter = _run(tmp_path, monkeypatch, opener=_connection_error)
    assert result == 0
    assert not any("restart" in call for call in calls)
    assert counter.read_text().strip() == "1"


def test_http_500_counts_as_unhealthy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from urllib.error import HTTPError

    def open_error(request: object, timeout: float) -> object:
        raise HTTPError("http://localhost/health", 500, "error", {}, None)

    result, _, counter = _run(tmp_path, monkeypatch, opener=open_error)
    assert result == 0
    assert counter.read_text().strip() == "1"


def test_timeout_counts_as_unhealthy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            time.sleep(0.2)
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        pytest.skip("sandbox does not permit binding a local test server")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
    calls: list[list[str]] = []

    def systemctl(argv: list[str], **kwargs: object) -> object:
        # Never the real user manager: on a host running brain-mcp-http this test
        # would otherwise read (and, past the threshold, restart) the live unit.
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="LoadState=loaded\nActiveState=active\n")

    try:
        result = watchdog.main(
            ["--port", str(server.server_port), "--timeout", "0.02"], systemctl=systemctl
        )
        assert result == 0
        assert (tmp_path / "consecutive-failures").read_text().strip() == "1"
        assert [call[2] for call in calls] == ["show"]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_unexpected_http_probe_error_is_broken_and_preserves_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter = tmp_path / "consecutive-failures"
    counter.write_text("1")

    def unexpected(*args: object, **kwargs: object) -> object:
        raise RuntimeError("unexpected opener failure")

    result, calls, _ = _run(tmp_path, monkeypatch, opener=unexpected)
    assert result == 3
    assert not any("restart" in call for call in calls)
    assert counter.read_text() == "1"


@pytest.mark.parametrize(
    "reason", [PermissionError(errno.EPERM, "denied"), OSError(errno.EMFILE, "full")]
)
def test_urlerror_with_local_probe_failure_is_broken_and_preserves_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: OSError
) -> None:
    counter = tmp_path / "consecutive-failures"
    counter.write_text("1")

    def opener(*args: object, **kwargs: object) -> object:
        raise URLError(reason)

    result, calls, _ = _run(tmp_path, monkeypatch, opener=opener)
    assert result == 3
    assert not any(call[2] in {"start", "restart"} for call in calls)
    assert counter.read_text().strip() == "1"


def test_bad_status_line_counts_as_unhealthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def opener(*args: object, **kwargs: object) -> object:
        raise http.client.BadStatusLine("garbage")

    result, _, counter = _run(tmp_path, monkeypatch, opener=opener)
    assert result == 0
    assert counter.read_text().strip() == "1"


def test_probe_uses_health_url_and_commands_have_exact_bounded_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls: list[str] = []
    calls: list[tuple[list[str], dict[str, object]]] = []
    (tmp_path / "consecutive-failures").write_text("1")

    def opener(request: object, timeout: float) -> object:
        urls.append(request.full_url)  # type: ignore[attr-defined]
        raise ConnectionRefusedError()

    def systemctl(argv: list[str], **kwargs: object) -> object:
        calls.append((argv, kwargs))
        if argv[2] == "show":
            return SimpleNamespace(returncode=0, stdout="LoadState=loaded\nActiveState=active\n")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
    assert watchdog.main(["--port", "8765"], systemctl=systemctl, opener=opener) == 0
    assert urls == ["http://127.0.0.1:8765/health"]
    assert [call[0] for call in calls] == [
        [
            "systemctl",
            "--user",
            "show",
            "brain-mcp-http.service",
            "-p",
            "LoadState",
            "-p",
            "ActiveState",
        ],
        ["systemctl", "--user", "--no-block", "restart", "brain-mcp-http.service"],
    ]
    assert all("timeout" in kwargs and kwargs["timeout"] <= 10.0 for _, kwargs in calls)


def test_restart_failure_clears_counter_before_returning_broken_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "consecutive-failures").write_text("1")

    def systemctl(argv: list[str], **kwargs: object) -> object:
        if argv[2] == "show":
            return SimpleNamespace(returncode=0, stdout="LoadState=loaded\nActiveState=active\n")
        raise TimeoutError("job submission failed")

    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
    assert watchdog.main(["--port", "8765"], systemctl=systemctl, opener=_connection_error) == 3
    assert not (tmp_path / "consecutive-failures").exists()


def test_unknown_unit_load_state_is_broken_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))

    def systemctl(argv: list[str], **kwargs: object) -> object:
        return SimpleNamespace(returncode=0, stdout="LoadState=not-found\nActiveState=inactive\n")

    assert watchdog.main(["--port", "8765"], systemctl=systemctl) == 3


@pytest.mark.parametrize("failure", ["missing", "nonzero", "empty"])
def test_systemctl_failure_is_broken_probe_and_leaves_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    counter = tmp_path / "consecutive-failures"
    counter.write_text("1")

    def run(argv: list[str], **kwargs: object) -> object:
        if failure == "missing":
            raise FileNotFoundError()
        return SimpleNamespace(
            returncode=1 if failure == "nonzero" else 0,
            stdout="" if failure == "empty" else "active\n",
        )

    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
    assert watchdog.main(["--port", "8765"], systemctl=run) == 3
    assert counter.read_text() == "1"


def test_missing_state_directory_is_broken_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STATE_DIRECTORY", raising=False)
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> object:
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="active\n")

    assert watchdog.main(["--port", "8765"], systemctl=run) == 3
    assert len(calls) == 1
    assert not any(call[2] in {"start", "restart"} for call in calls)


def test_corrupt_counter_counts_as_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "consecutive-failures").write_text("broken")
    result, calls, counter = _run(tmp_path, monkeypatch, opener=_connection_error)
    assert result == 0
    assert counter.read_text().strip() == "1"
    assert not any("restart" in call for call in calls)
