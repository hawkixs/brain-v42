"""Embedding supervisor — lazy lifecycle manager for the qodo container.

On-demand start: when a request arrives, ensure the backing container is
running (start it if not), forward the call, and idle-stop it after a
period without traffic.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Protocol

from aiohttp import ClientSession, ClientTimeout, web

logger = logging.getLogger("embedding_supervisor")


# ── Errors ─────────────────────────────────────────────────────────────


class GpuBusy(Exception):
    def __init__(self, free_mib: int, required_mib: int) -> None:
        self.free_mib = free_mib
        self.required_mib = required_mib
        super().__init__(f"gpu_busy: {free_mib} MiB free, need {required_mib}")


class StartTimeout(Exception):
    pass


class StartFailed(Exception):
    pass


# ── Collaborator protocols ────────────────────────────────────────────


class DockerClient(Protocol):
    async def start(self, container: str) -> None: ...
    async def stop(self, container: str) -> None: ...
    async def wait_healthy(self, container: str, timeout_sec: float) -> bool: ...


class GpuProbe(Protocol):
    async def free_mib(self) -> int: ...


# ── Core state machine ───────────────────────────────────────────────

STATE_IDLE_STOPPED = "IDLE_STOPPED"
STATE_STARTING = "STARTING"
STATE_READY = "READY"
STATE_STOPPING = "STOPPING"


class EmbeddingSupervisor:
    def __init__(
        self,
        docker_client: DockerClient,
        gpu_probe: GpuProbe,
        target_container: str,
        target_url: str,
        idle_timeout_sec: float = 900.0,
        gpu_min_free_mib: int = 6000,
        start_timeout_sec: float = 60.0,
    ) -> None:
        self._docker = docker_client
        self._gpu = gpu_probe
        self.target_container = target_container
        self.target_url = target_url
        self.idle_timeout_sec = idle_timeout_sec
        self.gpu_min_free_mib = gpu_min_free_mib
        self.start_timeout_sec = start_timeout_sec

        self.state = STATE_IDLE_STOPPED
        self.last_request_ts = 0.0
        self._ready_event = asyncio.Event()
        self._state_lock = asyncio.Lock()

    async def ensure_ready(self) -> None:
        async with self._state_lock:
            if self.state == STATE_READY:
                return
            if self.state == STATE_IDLE_STOPPED:
                free = await self._gpu.free_mib()
                if free < self.gpu_min_free_mib:
                    raise GpuBusy(free, self.gpu_min_free_mib)
                self.state = STATE_STARTING
                self._ready_event.clear()
                asyncio.create_task(self._do_start())
            # STATE_STARTING / STATE_STOPPING: fall through and wait.

        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=self.start_timeout_sec)
        except TimeoutError as exc:
            raise StartTimeout() from exc
        if self.state != STATE_READY:
            raise StartFailed()

    async def _do_start(self) -> None:
        try:
            await self._docker.start(self.target_container)
            healthy = await self._docker.wait_healthy(self.target_container, self.start_timeout_sec)
            if not healthy:
                raise RuntimeError("container did not become healthy")
            self.state = STATE_READY
            self.last_request_ts = time.time()
        except Exception:
            logger.exception("supervisor.start_failed")
            self.state = STATE_IDLE_STOPPED
        finally:
            self._ready_event.set()

    async def stop(self) -> None:
        async with self._state_lock:
            if self.state in (STATE_IDLE_STOPPED, STATE_STOPPING):
                return
            self.state = STATE_STOPPING
        try:
            await self._docker.stop(self.target_container)
        except Exception:
            logger.exception("supervisor.stop_failed")
        finally:
            self.state = STATE_IDLE_STOPPED
            self._ready_event.clear()

    async def check_idle_once(self) -> None:
        if self.state != STATE_READY:
            return
        idle = time.time() - self.last_request_ts
        if idle >= self.idle_timeout_sec:
            logger.info("supervisor.idle_timeout_stop", extra={"idle_sec": idle})
            await self.stop()

    async def on_request_done(self) -> None:
        self.last_request_ts = time.time()

    async def mark_lost(self) -> None:
        """Transition READY -> IDLE_STOPPED after detecting the backing
        container went away (user stopped it via Docker Desktop, crash,
        etc.). Next ensure_ready() will restart it.
        """
        async with self._state_lock:
            if self.state == STATE_READY:
                logger.warning("supervisor.backing_container_lost")
                self.state = STATE_IDLE_STOPPED
                self._ready_event.clear()

    async def status(self) -> dict[str, Any]:
        free = await self._gpu.free_mib()
        return {
            "state": self.state,
            "last_request_ts": self.last_request_ts,
            "gpu_free_mib": free,
            "target_container": self.target_container,
        }


# ── Default collaborator impls (prod only, skipped in unit tests) ──


# docker-py is synchronous; wrap its calls in asyncio.to_thread so the
# supervisor's aiohttp loop stays responsive during blocking socket I/O.
def _docker_client():  # noqa: ANN202
    import docker

    return docker.from_env()


class DockerCliClient:
    def __init__(self) -> None:
        self._client = _docker_client()

    async def start(self, container: str) -> None:
        c = await asyncio.to_thread(self._client.containers.get, container)
        await asyncio.to_thread(c.start)

    async def stop(self, container: str) -> None:
        c = await asyncio.to_thread(self._client.containers.get, container)
        await asyncio.to_thread(c.stop, 10)

    async def wait_healthy(self, container: str, timeout_sec: float) -> bool:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_sec
        while loop.time() < deadline:
            try:
                c = await asyncio.to_thread(self._client.containers.get, container)
                await asyncio.to_thread(c.reload)
                health = (c.attrs.get("State", {}).get("Health") or {}).get("Status")
                if health == "healthy":
                    return True
            except Exception:
                logger.exception("supervisor.health_probe_failed")
            await asyncio.sleep(2.0)
        return False


class NvidiaSmiGpuProbe:
    """Probe free VRAM by running nvidia-smi in a one-shot docker container.

    The supervisor image is slim and does NOT bundle CUDA driver libs.
    Rather than bloat the image, we spawn nvidia/cuda:base with `--gpus all`
    on demand. First probe pulls ~300 MiB, then each is ~500 ms — fine
    because probes only run on cold-start (if qodo is already READY we
    know the GPU is held by it and skip the probe).
    """

    def __init__(self) -> None:
        self._client = _docker_client()

    async def free_mib(self) -> int:
        try:
            out = await asyncio.to_thread(
                self._client.containers.run,
                (
                    "nvidia/cuda:12.4.1-base-ubuntu22.04@"
                    "sha256:0f6bfcbf267e65123bcc2287e2153dedfc0f24772fb5ce84afe16ac4b2fada95"
                ),
                command=[
                    "nvidia-smi",
                    "--query-gpu=memory.free",
                    "--format=csv,noheader,nounits",
                ],
                remove=True,
                device_requests=[
                    _docker_gpu_request(),
                ],
                stdout=True,
                stderr=False,
            )
        except Exception:
            logger.exception("supervisor.gpu_probe_failed")
            return 0
        # `containers.run` returns stdout bytes when detach=False (default)
        text = out.decode() if isinstance(out, (bytes, bytearray)) else str(out)
        text = text.strip()
        if not text:
            return 0
        try:
            return int(text.splitlines()[0])
        except ValueError:
            return 0


def _docker_gpu_request():  # noqa: ANN202
    # Built here to avoid importing docker at module import time (unit tests
    # don't require the docker SDK).
    from docker.types import DeviceRequest

    return DeviceRequest(count=-1, capabilities=[["gpu"]])


# ── HTTP proxy app ───────────────────────────────────────────────


class SupervisorApp:
    def __init__(self, sup: EmbeddingSupervisor) -> None:
        self.sup = sup
        self._http_session: ClientSession | None = None

    async def _ensure_http(self) -> ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = ClientSession(timeout=ClientTimeout(total=60))
        return self._http_session

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/healthz", self.handle_healthz)
        app.router.add_get("/ready", self.handle_ready)
        app.router.add_get("/status", self.handle_status)
        app.router.add_get("/", self.handle_proxy)
        app.router.add_post("/embed", self.handle_proxy)
        app.router.add_post("/embed/query", self.handle_proxy)
        app.router.add_post("/embed/single", self.handle_proxy)
        app.router.add_post("/rerank", self.handle_proxy)
        app.on_cleanup.append(self._on_cleanup)
        return app

    async def _on_cleanup(self, _: web.Application) -> None:
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

    async def handle_healthz(self, _: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "supervisor_state": self.sup.state})

    async def handle_ready(self, _: web.Request) -> web.Response:
        if self.sup.state == STATE_READY:
            return web.json_response({"ready": True})
        return web.json_response({"ready": False, "state": self.sup.state}, status=503)

    async def handle_status(self, _: web.Request) -> web.Response:
        snap = await self.sup.status()
        return web.json_response(snap)

    async def _do_proxy(self, request: web.Request) -> web.Response:
        """Single proxy attempt. Caller handles ensure_ready + retries."""
        session = await self._ensure_http()
        path = request.rel_url.path
        qs = request.rel_url.query_string
        target = f"{self.sup.target_url}{path}"
        if qs:
            target = f"{target}?{qs}"
        body = await request.read()
        async with session.request(
            request.method,
            target,
            headers={"Content-Type": request.headers.get("Content-Type", "application/json")},
            data=body if body else None,
        ) as resp:
            payload = await resp.read()
            headers = {"Content-Type": resp.headers.get("Content-Type", "application/json")}
            await self.sup.on_request_done()
            return web.Response(body=payload, status=resp.status, headers=headers)

    async def handle_proxy(self, request: web.Request) -> web.StreamResponse:
        # Up to TWO attempts: if the first fails with a connection error
        # the backing container disappeared (user stopped it) — reset state
        # and let ensure_ready restart it before retrying the proxy.
        for attempt in (1, 2):
            try:
                await self.sup.ensure_ready()
            except GpuBusy as exc:
                return web.json_response(
                    {
                        "error": "gpu_busy",
                        "free_mib": exc.free_mib,
                        "required_mib": exc.required_mib,
                    },
                    status=503,
                )
            except (StartTimeout, StartFailed) as exc:
                return web.json_response(
                    {"error": "container_unavailable", "detail": str(exc)},
                    status=503,
                )
            try:
                return await self._do_proxy(request)
            except Exception as exc:  # noqa: BLE001
                # Connection error typically means the backing container
                # was stopped out-of-band. Mark lost, retry once (attempt 2).
                logger.exception("supervisor.proxy_failed", extra={"attempt": attempt})
                if attempt == 1:
                    await self.sup.mark_lost()
                    continue
                return web.json_response({"error": "proxy_failed", "detail": str(exc)}, status=502)
        return web.json_response(
            {"error": "proxy_failed", "detail": "exhausted attempts"}, status=502
        )


async def _idle_loop(sup: EmbeddingSupervisor, period_sec: float = 60.0) -> None:
    while True:
        try:
            await asyncio.sleep(period_sec)
            await sup.check_idle_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("supervisor.idle_loop_error")


async def _main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    sup = EmbeddingSupervisor(
        docker_client=DockerCliClient(),
        gpu_probe=NvidiaSmiGpuProbe(),
        target_container=os.environ.get("TARGET_CONTAINER", "embedding-qodo"),
        target_url=os.environ.get("TARGET_URL", "http://embedding-qodo:8003"),
        idle_timeout_sec=float(os.environ.get("IDLE_TIMEOUT_SEC", "900")),
        gpu_min_free_mib=int(os.environ.get("GPU_MIN_FREE_MIB", "6000")),
        start_timeout_sec=float(os.environ.get("START_TIMEOUT_SEC", "60")),
    )
    app = SupervisorApp(sup).build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8003)
    await site.start()
    logger.info("supervisor.listening on :8003")

    idle_task = asyncio.create_task(_idle_loop(sup))
    try:
        await asyncio.Event().wait()
    finally:
        idle_task.cancel()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(_main())
