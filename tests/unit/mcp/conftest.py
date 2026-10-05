"""Process hygiene for the tests that go through the real ``_run_mcp``.

``_install_session_idle_timeout`` substitutes a symbol INSIDE FastMCP's module,
for want of a public extension point for the stateful sessions' idle deadline. In
production that is inconsequential: one process, one installation, at startup. In
a test suite it is not — several tests call the real ``_run_mcp`` in the same
interpreter, and the substitution would outlive the test that caused it.

Measured while writing this work: without this restoration, five tests of
``test_dream_capability_http.py`` failed while all passing in isolation — the
classic symptom of process state leaking from one test to the next, and the kind
of failure wrongly blamed on the following test.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _restore_fastmcp_session_manager() -> Iterator[None]:
    """Give FastMCP its session manager back after each test."""
    from fastmcp.server import http as fastmcp_http

    original = fastmcp_http.StreamableHTTPSessionManager
    try:
        yield
    finally:
        fastmcp_http.StreamableHTTPSessionManager = original


@pytest.fixture(autouse=True)
def _restore_mcp_transport_class() -> Iterator[None]:
    """Give the SDK's session manager its transport class back after each test.

    ``_install_transport_termination_hook`` substitutes it, for the same want of
    an extension point and with the same leak between tests if left behind.
    """
    from mcp.server import streamable_http_manager

    original = streamable_http_manager.StreamableHTTPServerTransport
    try:
        yield
    finally:
        streamable_http_manager.StreamableHTTPServerTransport = original
