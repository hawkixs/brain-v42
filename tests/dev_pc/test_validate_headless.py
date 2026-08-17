"""TDD tests for the headless-GPU validation gate (`validate-headless.sh`).

Plan Task 4 — the gate is the make-or-break check that proves the dev-pc
embedding came up on the **GPU** (not a silent CPU fallback) after a no-login
headless reboot. A 1536-float vector ALONE is not enough: qodo silently falls
back to CPU (judge C1), so the gate must assert `GET /` -> device=="cuda" AND
cuda_available==true, plus a warm-embed dim of 1536 within a latency ceiling,
and that the first (cold) /embed completes within the boot->first-embed bound.

These tests stand up a stub HTTP server (`http.server` in a background thread)
that mimics the supervisor contract on :8003, then shell the real
`validate-headless.sh` against it via the `EMBED_URL` env var. No Docker, no
GPU, no network to the dev-pc — pure contract verification of the gate logic.

Endpoints the stub serves (matching services/embedding_supervisor/main.py):
  GET  /healthz  -> 200 {"status":"ok",...}            (liveness; NO device)
  GET  /         -> {"device":"cuda"|"cpu",            (proxied to qodo)
                     "cuda_available":bool, ...}
  GET  /status   -> {"state","last_request_ts",
                     "gpu_free_mib","target_container"}
  POST /embed    -> [[float x N]]                       (outer 1, inner 1536)

Cases:
  (a) cuda body + 1x1536 fast embed                     -> script exit 0
  (b) cpu  body (cuda_available=false)                  -> non-zero (CPU reject)
  (c) /embed inner dim 768 (not 1536)                   -> non-zero
  (d) /healthz non-200                                  -> non-zero
  (e) warm /embed latency over the ceiling             -> non-zero
  (f) slow GET / wake over the boot->first-embed bound  -> non-zero
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

# Repository root is three levels up from tests/dev_pc/.
REPO_ROOT = Path(__file__).parent.parent.parent
VALIDATE_SCRIPT = REPO_ROOT / "deploy" / "dev-pc" / "validate-headless.sh"


# ── Stub supervisor ────────────────────────────────────────────────────


class StubConfig:
    """Mutable behaviour knobs read by the request handler at serve time."""

    def __init__(self) -> None:
        self.device = "cuda"
        self.cuda_available = True
        self.healthz_status = 200
        self.embed_dim = 1536
        self.embed_outer = 1
        self.embed_sleep_sec = 0.0
        # Delay injected into the GET / handler (the wake/cold-start path). Used
        # to exercise the boot->first-embed bound, which the gate enforces over
        # the WHOLE (GET / + first /embed) window.
        self.root_sleep_sec = 0.0
        self.state = "READY"
        self.gpu_free_mib = 12000


def _make_handler(cfg: StubConfig) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        # Silence the default stderr access log so pytest output stays clean.
        def log_message(self, *_args: object) -> None:  # noqa: D401
            return

        def _json(self, status: int, payload: object) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._json(
                    cfg.healthz_status,
                    {"status": "ok", "supervisor_state": cfg.state},
                )
                return
            if self.path == "/status":
                self._json(
                    200,
                    {
                        "state": cfg.state,
                        "last_request_ts": time.time(),
                        "gpu_free_mib": cfg.gpu_free_mib,
                        "target_container": "brain_embedding_qodo",
                    },
                )
                return
            if self.path == "/":
                if cfg.root_sleep_sec:
                    time.sleep(cfg.root_sleep_sec)
                self._json(
                    200,
                    {
                        "device": cfg.device,
                        "cuda_available": cfg.cuda_available,
                        "model": "qodo",
                        "dim": 1536,
                    },
                )
                return
            self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0") or "0")
            _ = self.rfile.read(length)  # drain request body
            if self.path == "/embed":
                if cfg.embed_sleep_sec:
                    time.sleep(cfg.embed_sleep_sec)
                vectors = [[0.01] * cfg.embed_dim for _ in range(cfg.embed_outer)]
                self._json(200, vectors)
                return
            self._json(404, {"error": "not_found"})

    return Handler


@pytest.fixture
def stub_server():  # type: ignore[no-untyped-def]
    """Start a threaded stub supervisor on an ephemeral port.

    Yields ``(cfg, base_url)``. Mutate ``cfg`` BEFORE invoking the script;
    the handler reads it live on every request.
    """
    cfg = StubConfig()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(cfg))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield cfg, f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run_gate(base_url: str, extra_env: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    """Shell validate-headless.sh against the stub; return CompletedProcess."""
    env = {
        "EMBED_URL": base_url,
        # Keep ceilings generous so timing is never the reason a "pass" case
        # fails on a busy CI box — individual tests tighten them as needed.
        "WARM_LATENCY_CEILING_SEC": "10",
        "FIRST_EMBED_CEILING_SEC": "120",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(VALIDATE_SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )


# ── Tests ──────────────────────────────────────────────────────────────


def test_script_exists_and_is_executable() -> None:
    """The gate script must exist (executable bit is best-effort)."""
    assert VALIDATE_SCRIPT.exists(), f"missing gate script: {VALIDATE_SCRIPT}"


def test_passes_on_cuda_with_1536_fast_embed(stub_server) -> None:  # type: ignore[no-untyped-def]
    """(a) device=cuda + cuda_available + 1x1536 fast embed -> exit 0, PASS."""
    cfg, base_url = stub_server
    cfg.device = "cuda"
    cfg.cuda_available = True
    cfg.embed_dim = 1536
    cfg.embed_outer = 1

    result = _run_gate(base_url)
    assert result.returncode == 0, (
        f"expected exit 0 on healthy cuda gate; got {result.returncode}\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "HEADLESS-GPU GATE: PASS" in result.stdout, (
        f"expected PASS banner; got:\n{result.stdout}"
    )


def test_fails_on_cpu_fallback(stub_server) -> None:  # type: ignore[no-untyped-def]
    """(b) device=cpu / cuda_available=false -> non-zero, CPU fallback rejected."""
    cfg, base_url = stub_server
    cfg.device = "cpu"
    cfg.cuda_available = False

    result = _run_gate(base_url)
    assert result.returncode != 0, (
        f"expected non-zero exit on CPU fallback; got 0\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "HEADLESS-GPU GATE: FAIL" in combined
    assert "cpu" in combined.lower() or "cuda" in combined.lower(), (
        f"failure reason should mention the device/cuda check; got:\n{combined}"
    )


def test_fails_on_wrong_embed_dim(stub_server) -> None:  # type: ignore[no-untyped-def]
    """(c) /embed inner dim 768 (not 1536) -> non-zero."""
    cfg, base_url = stub_server
    cfg.device = "cuda"
    cfg.cuda_available = True
    cfg.embed_dim = 768  # wrong

    result = _run_gate(base_url)
    assert result.returncode != 0, (
        f"expected non-zero exit on wrong embed dim; got 0\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "HEADLESS-GPU GATE: FAIL" in combined
    assert "1536" in combined or "768" in combined or "dim" in combined.lower()


def test_fails_on_healthz_non_200(stub_server) -> None:  # type: ignore[no-untyped-def]
    """(d) /healthz non-200 -> non-zero."""
    cfg, base_url = stub_server
    cfg.healthz_status = 503

    result = _run_gate(base_url)
    assert result.returncode != 0, (
        f"expected non-zero exit on /healthz 503; got 0\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "HEADLESS-GPU GATE: FAIL" in combined
    assert "healthz" in combined.lower() or "503" in combined


def test_fails_on_latency_over_ceiling(stub_server) -> None:  # type: ignore[no-untyped-def]
    """(e) warm /embed latency over the ceiling -> non-zero."""
    cfg, base_url = stub_server
    cfg.device = "cuda"
    cfg.cuda_available = True
    cfg.embed_dim = 1536
    cfg.embed_sleep_sec = 2.0  # stall the embed call

    # Tighten the warm-latency ceiling below the induced 2s stall.
    result = _run_gate(base_url, extra_env={"WARM_LATENCY_CEILING_SEC": "1"})
    assert result.returncode != 0, (
        f"expected non-zero exit on over-ceiling latency; got 0\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "HEADLESS-GPU GATE: FAIL" in combined
    assert "latency" in combined.lower() or "ceiling" in combined.lower()


def test_fails_on_cold_start_over_first_embed_ceiling(stub_server) -> None:  # type: ignore[no-untyped-def]
    """(f) slow GET / wake blows the boot->first-embed bound -> non-zero.

    The cold-start cost lives in the GET / wake (probe -> qodo start), so the
    gate must time the WHOLE (GET / + first /embed) window. We stall GET / by 2s
    and set FIRST_EMBED_CEILING_SEC=1 so the window exceeds the bound; the gate
    must trip on the boot->first-embed timing, not the warm-latency check.
    """
    cfg, base_url = stub_server
    cfg.device = "cuda"
    cfg.cuda_available = True
    cfg.embed_dim = 1536
    cfg.root_sleep_sec = 2.0  # stall the GET / wake (the cold-start path)

    result = _run_gate(base_url, extra_env={"FIRST_EMBED_CEILING_SEC": "1"})
    assert result.returncode != 0, (
        f"expected non-zero exit when GET /+embed exceeds the cold-start bound; got 0\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "HEADLESS-GPU GATE: FAIL" in combined
    assert "boot" in combined.lower() or "ceiling" in combined.lower(), (
        f"failure reason should cite the cold-start/boot timing; got:\n{combined}"
    )
