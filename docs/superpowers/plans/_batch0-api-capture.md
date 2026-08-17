# Batch 0 — FastMCP 3.4.2 API capture (Task 0.2)

Captured live against the installed package in this worktree's venv:
`fastmcp 3.4.2` (python 3.12), `mcp 1.28.1`, `starlette 1.3.1`. These facts gate Tasks 2.2/2.3/3.1/3.2/5.1.

## Transport entry — `run_http_async` (use this; do NOT pass these to `FastMCP()`)
```
run_http_async(self, show_banner=True, transport: Literal['http','streamable-http','sse']='http',
               host=None, port=None, log_level=None, path=None,
               uvicorn_config: dict|None=None, middleware: list[ASGIMiddleware]|None=None,
               json_response: bool|None=None, stateless_http: bool|None=None,
               stateless: bool|None=None, sockets=None) -> None
```
→ **Task 2.3 call shape confirmed:** `await mcp.run_http_async(transport="http", host=..., port=..., stateless_http=True, json_response=True, uvicorn_config={"timeout_graceful_shutdown": 10}, middleware=[host_origin_guard])`.
→ `http_app(path, middleware: list[ASGIMiddleware], json_response, stateless_http, transport, event_store, retry_interval) -> StarletteWithLifespan` — same kwargs, returns a Starlette app carrying the lifespan.

## C1 confirmed — removed `__init__` kwargs
`FastMCP.__init__` does NOT accept `stateless_http`/`json_response`/`host`/`port` — they are in `_REMOVED_KWARGS` (server.py): e.g. `'stateless_http': 'Pass stateless_http to run_http_async() or http_app(), or set FASTMCP_STATELESS_HTTP.'`. Passing them → TypeError-class failure via `**kwargs`. (Note: in 3.4.2 the attribute `_check_removed_kwargs` no longer exists as a method, but the `_REMOVED_KWARGS` dict is still enforced — same outcome as the 3.1.0 evidence.)

## `FastMCP.__init__` — kwargs the plan needs ARE present
`mask_error_details: bool|None` ✅ (Task 3.2), `lifespan: LifespanCallable|Lifespan|None` ✅ (Task 2.2), `middleware: Sequence[Middleware]|None` ✅ (FastMCP-level middleware, distinct from the ASGI middleware on run_http_async), `auth: AuthProvider|None`.

## H5 RESOLVED — `run_http_async` DOES enter the lifespan
`_lifespan_proxy` (server.py:256-275) raises if a lifespan is defined but its result was never set: *"FastMCP server has a lifespan defined but no lifespan result is set, which means the server's context manager was not entered. Are you running the server in a way that supports lifespans?"* → both `run_http_async` and `run_async(stdio)` enter the lifespan, or this guard fires loudly. So **Task 2.2's `FastMCP(lifespan=make_lifespan(...))` is entered under HTTP** (the architect's "flushers never start" risk is guarded against by the framework). Default lifespan (`default_lifespan`) is a no-op; we replace it.

## H1 confirmed — NO built-in DNS-rebinding/security passthrough
`create_streamable_http_app(server, streamable_http_path, event_store=None, retry_interval=None, auth=None, json_response=False, stateless_http=False, debug=False, routes=None, middleware: list[Middleware]|None=None)` — **no `security_settings`/`allowed_hosts`/`allowed_origins` param**. Confirms Host/Origin enforcement is CODE we write (Task 3.1), injected via the `middleware=` param on `run_http_async`/`http_app`.

## H6 confirmed — header access
`fastmcp.server.dependencies.get_http_headers` (dependencies.py:410): lowercases every header name (`lower_name = name.lower()`, :459) and **returns `{}` when there is no HTTP request** (:464) — so it never raises under stdio. → read `get_http_headers().get("x-brain-agent", "unknown")` (lowercase key) directly in `instrument_tool` (Plan 2 H6).

## Served path
Default streamable-HTTP path is `/mcp` (clients use `http://127.0.0.1:PORT/mcp`). `path=` overrides; `FASTMCP_STREAMABLE_HTTP_PATH` env also works.

## Net effect on Plan 1
No task needs an API rewrite. Task 2.3 uses `run_http_async(...middleware=[guard])`; Task 2.2 passes `lifespan=` to `FastMCP()`; Task 3.1's guard is an `ASGIMiddleware` in the `middleware=` list; Task 3.2 uses `mask_error_details=True`. All confirmed against 3.4.2.
