"""Dedicated aiohttp server for automation endpoints."""

from __future__ import annotations

from aiohttp import web

from brain_v42.automation.webhook import GitLabWebhookEndpoint

DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 10.0


class AutomationServer:
    """Serve the automation-only HTTP surface."""

    def __init__(
        self,
        webhook_endpoint: GitLabWebhookEndpoint,
        port: int = 9201,
        host: str = "127.0.0.1",
        *,
        shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        self._webhook_endpoint = webhook_endpoint
        self._port = port
        self._host = host
        self._shutdown_timeout = shutdown_timeout
        self._runner: web.AppRunner | None = None

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/health", self._handle_health, allow_head=False)
        app.router.add_post("/gitlab/webhook", self._webhook_endpoint.handle)
        return app

    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.Response(status=200)

    async def start(self) -> None:
        """Bind the HTTP server once and clean up any partial startup."""
        if self._runner is not None:
            return

        runner = web.AppRunner(
            self._build_app(),
            auto_decompress=False,
            shutdown_timeout=self._shutdown_timeout,
        )
        try:
            await runner.setup()
            site = web.TCPSite(runner, self._host, self._port)
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner = runner

    async def stop(self) -> None:
        """Stop the HTTP server once."""
        if self._runner is None:
            return

        runner = self._runner
        self._runner = None
        await runner.cleanup()
