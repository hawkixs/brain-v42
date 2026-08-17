"""Metrics sidecar extension exposing the read-only Brain graph projection."""

from __future__ import annotations

from typing import Any

import structlog
from aiohttp import web

from brain_v42.metrics.server import MetricsServer, _has_loopback_tcp_peer

logger = structlog.get_logger(__name__)


class BrainGraphMetricsServer(MetricsServer):
    """Add the graph route without changing the established MetricsServer routes."""

    def __init__(self, *args: Any, graph_projection_svc: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._graph_projection_svc = graph_projection_svc

    def _build_app(self) -> web.Application:
        app = super()._build_app()
        app.router.add_get("/api/brain-graph/v1", self._handle_brain_graph)
        return app

    async def _handle_brain_graph(self, request: web.Request) -> web.Response:
        if not _has_loopback_tcp_peer(request):
            return web.json_response({"error": "loopback peer required"}, status=403)
        try:
            payload = await self._graph_projection_svc.snapshot()
        except Exception as exc:
            logger.error(
                "brain_graph.endpoint_failed",
                error_type=type(exc).__name__,
            )
            return web.json_response({"error": "brain graph unavailable"}, status=503)
        return web.json_response(
            payload,
            headers={
                "Cache-Control": "private, max-age=5",
                "X-Content-Type-Options": "nosniff",
            },
        )


__all__ = ["BrainGraphMetricsServer"]
