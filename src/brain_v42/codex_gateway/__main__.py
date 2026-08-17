"""Run the Codex management gateway with its validated bind settings."""

from __future__ import annotations

import uvicorn

from brain_v42.codex_gateway.app import create_production_app
from brain_v42.config import get_settings


def run() -> None:
    settings = get_settings()
    app = create_production_app(settings)
    uvicorn.run(
        app,
        host=settings.brain_codex_gateway_host,
        port=settings.brain_codex_gateway_port,
    )


if __name__ == "__main__":
    run()
