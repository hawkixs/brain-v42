"""The shared credential verifier adds no MCP project policy to the sidecar."""

from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from tests.unit.metrics.test_receiver_credentials import TOKEN, make_server
from tests.unit.test_metrics_receiver_rejection_counters import _request, _stream, _transport


@pytest.mark.parametrize("mode", ["off", "observe", "enforce", "not-an-mcp-mode"])
async def test_sidecar_serves_credentials_under_every_scope_mode(monkeypatch, mode):
    monkeypatch.setenv("BRAIN_CREDENTIAL_PROJECT_SCOPE_MODE", mode)
    server, repository, _ = await make_server(monkeypatch)
    repository.rows[0] = replace(repository.rows[0], allowed_projects=["brain-v42"])
    app = server._build_app()
    async with asynccontextmanager(app.cleanup_ctx[0])(app):
        response = await server._handle_codex_logs(
            _request(
                "/v1/logs",
                headers={"Authorization": "Bearer " + TOKEN},
                transport=_transport("203.0.113.5"),
                payload=_stream(data=b'{"resourceLogs":[]}'),
            )
        )
    assert response.status == 200
    assert server._receiver_refused == {}
