from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from structlog.testing import capture_logs

from brain_v42.metrics import runtime as runtime_module
from brain_v42.metrics.runtime import build_metrics_runtime, build_sidecar_structlog_processors
from brain_v42.metrics.server import MetricsServer


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_on_bind_error", [False, True])
async def test_bind_failure_is_optional_for_mcp_and_fatal_for_standalone(
    fail_on_bind_error: bool,
) -> None:
    runner = MagicMock()
    runner.setup = AsyncMock()
    runner.cleanup = AsyncMock()
    site = MagicMock()
    site.start = AsyncMock(side_effect=OSError("address in use"))
    server = MetricsServer(MagicMock(), MagicMock(), fail_on_bind_error=fail_on_bind_error)
    with (
        patch("brain_v42.metrics.server.web.AppRunner", return_value=runner),
        patch("brain_v42.metrics.server.web.TCPSite", return_value=site),
    ):
        with capture_logs() as logs:
            if fail_on_bind_error:
                with pytest.raises(RuntimeError, match="failed to bind"):
                    await server.start()
            else:
                await server.start()
    failure_log = next(e for e in logs if e["event"] == "metrics_server.port_in_use")
    assert failure_log["log_level"] == ("error" if fail_on_bind_error else "warning")
    assert server._runner is None
    runner.cleanup.assert_awaited_once()


def test_syslog_prefix_splits_only_on_newline() -> None:
    processor = build_sidecar_structlog_processors(MagicMock())[-1]
    rendered = processor(None, "warning", {"event": "first\u2028second\nthird", "level": "warning"})
    assert "<4> " in rendered
    assert "first\u2028second" in rendered
    assert "\n<4> third" in rendered


def test_standalone_runtime_server_is_fatal_but_default_server_is_tolerant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = MagicMock()
    settings.graph_projector_enabled = False
    settings.metrics_legacy_automation_enabled = False
    settings.graph_enabled = False
    collector = MagicMock()
    monkeypatch.setattr(runtime_module, "MetricsCollector", lambda **_: collector)
    monkeypatch.setattr(runtime_module, "build_embedding_service", lambda _: MagicMock())
    monkeypatch.setattr(runtime_module, "create_neo4j_driver", lambda **_: None)
    runtime = build_metrics_runtime(settings=settings, engine=MagicMock())
    server_factory = runtime._resources.server_factory
    assert server_factory is not None
    assert server_factory(None, None)._fail_on_bind_error is True
    assert MetricsServer(MagicMock(), MagicMock())._fail_on_bind_error is False


@pytest.mark.asyncio
async def test_standalone_entrypoint_propagates_bind_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.metrics import __main__ as entrypoint

    # ``main`` configures structlog globally: restore it for the other tests.
    saved = structlog.get_config()

    class Runtime:
        _resources = MagicMock(collector=MagicMock())

        async def run(self, _stop: object) -> int:
            raise RuntimeError("metrics sidecar failed to bind")

    monkeypatch.setattr(entrypoint, "build_metrics_runtime", lambda: Runtime())
    try:
        with pytest.raises(RuntimeError, match="failed to bind"):
            await entrypoint.main(stop_event=asyncio.Event())
    finally:
        structlog.reset_defaults()
        structlog.configure(**saved)
