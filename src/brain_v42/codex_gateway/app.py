"""FastAPI application factory for the Codex management gateway."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import APIRouter, Depends, FastAPI
from pydantic import SecretStr
from starlette.requests import Request
from starlette.responses import JSONResponse

from brain_v42.codex_gateway.auth import BearerAuthenticator, require_non_empty_token
from brain_v42.codex_gateway.dependencies import GatewayServices
from brain_v42.codex_gateway.management_routes import build_management_router
from brain_v42.codex_gateway.proposal_routes import build_proposal_router
from brain_v42.codex_gateway.ticket_routes import build_ticket_router
from brain_v42.config import Settings, get_settings

ShutdownCallback = Callable[[], Awaitable[None]]
ReadinessCallback = Callable[[], Awaitable[None]]
logger = structlog.get_logger(__name__)


def create_app(
    *,
    services: GatewayServices,
    token: SecretStr,
    shutdown: ShutdownCallback | None = None,
    readiness: ReadinessCallback | None = None,
    readiness_timeout_s: float = 2.0,
) -> FastAPI:
    """Build an isolated app around explicitly injected domain services."""
    require_non_empty_token(token)
    authenticator = BearerAuthenticator(token)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if shutdown is not None:
                await shutdown()

    app = FastAPI(
        title="brain-v42 Codex gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, error: Exception) -> JSONResponse:
        logger.error(
            "codex_gateway.unhandled_error",
            method=request.method,
            path=request.url.path,
            error_type=type(error).__name__,
        )
        return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", response_model=None)
    async def ready() -> dict[str, str] | JSONResponse:
        if readiness is None:
            return {"status": "ready"}
        try:
            async with asyncio.timeout(readiness_timeout_s):
                await readiness()
        except Exception as error:  # noqa: BLE001 - dependency failures become 503
            logger.warning(
                "codex_gateway.readiness_failed",
                error_type=type(error).__name__,
            )
            return JSONResponse(
                status_code=503,
                content={"status": "unavailable"},
            )
        return {"status": "ready"}

    api = APIRouter(prefix="/api", dependencies=[Depends(authenticator)])
    api.include_router(build_ticket_router(services))
    api.include_router(build_management_router(services))
    api.include_router(build_proposal_router(services))
    app.include_router(api)
    return app


def create_production_app(settings: Settings | None = None) -> FastAPI:
    """Compose PostgreSQL-backed services and build the production app."""
    configured = settings if settings is not None else get_settings()
    require_non_empty_token(configured.brain_codex_gateway_token)

    from brain_v42.codex_gateway.composition import build_production_runtime  # noqa: PLC0415

    runtime = build_production_runtime(configured)
    return create_app(
        services=runtime.services,
        token=configured.brain_codex_gateway_token,
        shutdown=runtime.shutdown,
        readiness=runtime.readiness,
    )
