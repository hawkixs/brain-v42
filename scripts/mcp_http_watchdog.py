#!/usr/bin/env python3
"""Probe the MCP HTTP service without confusing a broken probe with an outage.

The watchdog runs every 30 seconds. A missing or broken HTTP client must not
restart a healthy server, and a failed systemd unit must be reset before it can
be started again. An inactive unit is an operator stop, so it remains stopped.
Two consecutive unhealthy HTTP outcomes are required before restarting the
service (ticket 416266ec); the HTTP request is bounded because a wedged event
loop must not leave this oneshot activating forever (incident 2026-07-03).
"""

from __future__ import annotations

import argparse
import http.client
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BROKEN_PROBE = 3
DEFAULT_UNIT = "brain-mcp-http.service"
DEFAULT_THRESHOLD = 2
DEFAULT_TIMEOUT = 10.0
SYSTEMCTL_TIMEOUT = 10.0

Systemctl = Callable[..., subprocess.CompletedProcess[str]]
Opener = Callable[..., object]


def _run_systemctl(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)


def _read_state(unit: str, systemctl: Systemctl) -> str:
    """Read both load and activity so a typo cannot disable supervision silently."""
    result = systemctl(
        ["systemctl", "--user", "show", unit, "-p", "LoadState", "-p", "ActiveState"],
        timeout=SYSTEMCTL_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"systemctl show failed with exit status {result.returncode}")
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if properties.get("LoadState") != "loaded":
        raise RuntimeError(f"systemd unit is not loaded: {properties.get('LoadState', 'missing')}")
    state = properties.get("ActiveState", "")
    if not state:
        raise RuntimeError("systemctl show returned no ActiveState")
    return state


def _state_directory(environ: Mapping[str, str]) -> Path:
    """Use systemd's private writable state directory for the failure counter."""
    value = environ.get("STATE_DIRECTORY", "")
    if not value:
        raise RuntimeError("STATE_DIRECTORY is not set")
    directory = Path(value)
    if not directory.is_dir() or not os.access(directory, os.R_OK | os.W_OK | os.X_OK):
        raise RuntimeError("STATE_DIRECTORY is not a readable and writable directory")
    return directory / "consecutive-failures"


def _counter(path: Path) -> int:
    """Treat missing or damaged state as no consecutive failures."""
    try:
        value = int(path.read_text().strip())
    except FileNotFoundError:
        return 0
    except ValueError:
        return 0
    return max(value, 0)


def _healthy(url: str, timeout: float, opener: Opener) -> bool:
    """Count server-side HTTP/connection failures, but surface local probe faults.

    The ConnectionError family (refused, reset, aborted, broken pipe) and a
    timeout describe the server's side of the socket. A local fault (EPERM from a
    sandbox, EMFILE, EADDRNOTAVAIL) is a plain OSError, not a ConnectionError: it
    propagates as a broken probe, which must never restart a healthy server.
    """
    request = Request(url, method="GET")
    try:
        response = opener(request, timeout=timeout)
    except URLError as exc:
        if isinstance(exc, HTTPError):
            return False
        if isinstance(exc.reason, (ConnectionError, TimeoutError)):
            return False
        raise
    except (TimeoutError, ConnectionError, http.client.HTTPException):
        return False
    with response:  # type: ignore[attr-defined]
        status = getattr(response, "status", None)
        if status is None:
            status = getattr(response, "code", None)
        return status == 200


def _command(systemctl: Systemctl, action: str, unit: str) -> None:
    """Submit lifecycle jobs asynchronously so service startup timeouts cannot mask them."""
    result = systemctl(
        [
            "systemctl",
            "--user",
            *(["--no-block"] if action in {"start", "restart"} else []),
            action,
            unit,
        ],
        timeout=SYSTEMCTL_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"systemctl {action} failed with exit status {result.returncode}")


def run_watchdog(
    *,
    port: int,
    unit: str = DEFAULT_UNIT,
    threshold: int = DEFAULT_THRESHOLD,
    timeout: float = DEFAULT_TIMEOUT,
    systemctl: Systemctl = _run_systemctl,
    opener: Opener = urlopen,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run one watchdog cycle; return 3 only when the probe itself is broken."""
    try:
        state = _read_state(unit, systemctl)
        counter_file = _state_directory(os.environ if environ is None else environ)
        if state == "failed":
            _command(systemctl, "reset-failed", unit)
            _command(systemctl, "start", unit)
            counter_file.unlink(missing_ok=True)
            print(f"watchdog recovered failed unit {unit}", flush=True)
            return 0
        if state == "inactive":
            print(f"watchdog left inactive unit {unit} stopped", flush=True)
            return 0
        if state in {"activating", "deactivating", "reloading"}:
            print(f"watchdog skipped {state} unit {unit}", flush=True)
            return 0
        if state != "active":
            raise RuntimeError(f"unexpected ActiveState {state!r}")

        if _healthy(f"http://127.0.0.1:{port}/health", timeout, opener):
            counter_file.unlink(missing_ok=True)
            return 0

        count = _counter(counter_file) + 1
        if count >= threshold:
            counter_file.unlink(missing_ok=True)
            _command(systemctl, "restart", unit)
            print(f"watchdog restarted unhealthy unit {unit}", flush=True)
        else:
            counter_file.write_text(f"{count}\n")
        return 0
    except Exception as exc:
        print(f"watchdog probe failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return BROKEN_PROBE


def _port(raw: str) -> int:
    value = int(raw)
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError("must be within 1..65535")
    return value


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def _positive_float(raw: str) -> float:
    value = float(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def main(
    argv: Sequence[str] | None = None,
    *,
    systemctl: Systemctl = _run_systemctl,
    opener: Opener = urlopen,
    environ: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=_port, required=True)
    parser.add_argument("--unit", default=DEFAULT_UNIT)
    parser.add_argument("--threshold", type=_positive_int, default=DEFAULT_THRESHOLD)
    parser.add_argument("--timeout", type=_positive_float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)
    return run_watchdog(
        port=args.port,
        unit=args.unit,
        threshold=args.threshold,
        timeout=args.timeout,
        systemctl=systemctl,
        opener=opener,
        environ=environ,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
