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
