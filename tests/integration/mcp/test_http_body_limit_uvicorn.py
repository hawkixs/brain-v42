"""Replay body limits against real Uvicorn, including chunked transfer."""

import asyncio
import socket
from collections.abc import AsyncIterator

import httpx
import pytest
import uvicorn
from fastmcp import FastMCP

from brain_v42.config import Settings
from brain_v42.mcp.server import _configure_http_security


@pytest.mark.asyncio
async def test_real_uvicorn_refuses_a_chunked_oversize_body() -> None:
    cap = 65_536
    settings = Settings(
        postgres_url="postgresql+asyncpg://unused:unused@localhost/unused",
        mcp_http_token="test-token",
        mcp_http_max_body_bytes=cap,
        _env_file=None,
    )
    mcp = FastMCP("body-limit-socket")
    app = mcp.http_app(
        stateless_http=True, json_response=True, middleware=_configure_http_security(mcp, settings)
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _attempt in range(200):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Uvicorn did not start")

        async def chunks() -> AsyncIterator[bytes]:
            yield b"x" * cap
            yield b"x"

        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={
                "Authorization": "Bearer test-token",
                "Accept": "application/json, text/event-stream",
            },
        ) as client:
            declared = await client.post("/mcp", content=b"x" * (cap + 1))
            assert declared.status_code == 413
            streamed = await client.post("/mcp", content=chunks())
            assert streamed.status_code == 413
            small = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "body-limit-test", "version": "1"},
                    },
                },
            )
            assert small.status_code == 200
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        listener.close()
