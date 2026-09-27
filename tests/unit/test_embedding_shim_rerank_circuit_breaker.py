"""CUDA circuit breaker for services/embedding_shim/shim_backends.py.

Without a breaker, every rerank() call retries CUDA first even while the GPU is
saturated (e.g. OOM from a co-tenant process): extra latency on every request for
as long as the outage lasts. The breaker opens on a CUDA failure, routes every
request straight to CPU for RERANK_CUDA_COOLDOWN_SECONDS without touching CUDA at
all, then lets the next request retry CUDA — closing the breaker on success.

All doubles are FAKE session/tokenizer/clock objects — no real onnxruntime or GPU
here (same style as test_embedding_shim_rerank_gpu.py).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

SHIM_DIR = Path(__file__).resolve().parents[2] / "services" / "embedding_shim"
sys.path.insert(0, str(SHIM_DIR))

from shim_backends import OnnxRerankBackend  # noqa: E402


class _FakeInput:
    def __init__(self, name: str) -> None:
        self.name = name


def _fake_inputs() -> list[_FakeInput]:
    return [_FakeInput("input_ids"), _FakeInput("attention_mask"), _FakeInput("token_type_ids")]


class _Encoding:
    def __init__(self, ids: list[int], attention_mask: list[int], type_ids: list[int]) -> None:
        self.ids = ids
        self.attention_mask = attention_mask
        self.type_ids = type_ids


class _FakeTokenizer:
    """encode_batch always returns a single-token row per pair — length sorting
    and micro-batching are covered by test_embedding_shim_rerank_gpu.py, not
    relevant here."""

    def encode_batch(self, pairs: list[tuple[str, str]]) -> list[_Encoding]:
        return [_Encoding([0], [1], [0]) for _ in pairs]


class SwitchableCudaSession:
    """A CUDA session whose failure/success can be flipped between calls —
    simulates the GPU going OOM and later recovering."""

    def __init__(self, *, should_fail: bool = True) -> None:
        self.should_fail = should_fail
        self.run_calls = 0

    def get_inputs(self) -> list[_FakeInput]:
        return _fake_inputs()

    def get_providers(self) -> list[str]:
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]

    def run(self, _outputs, feeds):
        self.run_calls += 1
        if self.should_fail:
            raise RuntimeError("CUDA allocation failed")
        import numpy as np

        n = feeds["input_ids"].shape[0]
        return [np.array([[7.0] for _ in range(n)])]


class FixedScoreCpuSession:
    def __init__(self, score: float) -> None:
        self._score = score
        self.run_calls = 0

    def get_inputs(self) -> list[_FakeInput]:
        return _fake_inputs()

    def get_providers(self) -> list[str]:
        return ["CPUExecutionProvider"]

    def run(self, _outputs, feeds):
        import numpy as np

        self.run_calls += 1
        n = feeds["input_ids"].shape[0]
        return [np.array([[self._score] for _ in range(n)])]


class FakeClock:
    """A monotonic-clock stand-in the test fully controls."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _backend(
    *, cooldown: float, clock: FakeClock, cuda: SwitchableCudaSession, cpu_score: float = 1.0
) -> tuple[OnnxRerankBackend, FixedScoreCpuSession]:
    backend = OnnxRerankBackend(
        "unused.onnx",
        "unused.json",
        cuda_cooldown_seconds=cooldown,
        clock=clock,
    )
    backend._session = cuda
    backend._tokenizer = _FakeTokenizer()
    cpu_session = FixedScoreCpuSession(cpu_score)
    backend._cpu_session = cpu_session
    return backend, cpu_session


pytestmark = pytest.mark.filterwarnings("ignore")


def test_construction_rejects_negative_cooldown() -> None:
    with pytest.raises(ValueError, match="RERANK_CUDA_COOLDOWN_SECONDS"):
        OnnxRerankBackend("m.onnx", "t.json", cuda_cooldown_seconds=-1)


def test_construction_accepts_zero_cooldown() -> None:
    OnnxRerankBackend("m.onnx", "t.json", cuda_cooldown_seconds=0)


def test_cuda_failure_opens_the_breaker_and_logs_once(caplog) -> None:
    pytest.importorskip("numpy")
    clock = FakeClock()
    cuda = SwitchableCudaSession(should_fail=True)
    backend, cpu = _backend(cooldown=300, clock=clock, cuda=cuda)

    with caplog.at_level(logging.WARNING, logger="shim_backends"):
        scores = backend.rerank("q", ["c1"])

    assert scores == [1.0]
    assert cuda.run_calls == 1
    assert cpu.run_calls == 1
    assert caplog.text.count("rerank_cuda_breaker_open") == 1


def test_requests_during_cooldown_never_touch_cuda(caplog) -> None:
    pytest.importorskip("numpy")
    clock = FakeClock()
    cuda = SwitchableCudaSession(should_fail=True)
    backend, cpu = _backend(cooldown=300, clock=clock, cuda=cuda)

    backend.rerank("q", ["c1"])  # opens the breaker
    assert cuda.run_calls == 1

    clock.advance(100)  # still well within the 300s cooldown
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="shim_backends"):
        scores = backend.rerank("q", ["c1"])

    assert scores == [1.0]
    assert cuda.run_calls == 1, "CUDA must not be touched again while the breaker is open"
    assert cpu.run_calls == 2
    # No duplicate "open" log: the breaker was already open, this is not a
    # new transition.
    assert "rerank_cuda_breaker_open" not in caplog.text


def test_first_request_after_cooldown_retries_cuda_and_success_closes_breaker(caplog) -> None:
    pytest.importorskip("numpy")
    clock = FakeClock()
    cuda = SwitchableCudaSession(should_fail=True)
    backend, cpu = _backend(cooldown=300, clock=clock, cuda=cuda)

    backend.rerank("q", ["c1"])  # opens the breaker at t=0
    assert cuda.run_calls == 1

    clock.advance(300)  # cooldown fully elapsed
    cuda.should_fail = False  # the GPU has recovered
    with caplog.at_level(logging.INFO, logger="shim_backends"):
        scores = backend.rerank("q", ["c1"])

    assert scores == [7.0]
    assert cuda.run_calls == 2, "the next request after cooldown must retry CUDA"
    assert cpu.run_calls == 1, "no CPU fallback needed once CUDA succeeds again"
    assert caplog.text.count("rerank_cuda_breaker_closed") == 1


def test_repeated_failure_after_cooldown_reopens_without_a_duplicate_open_log(caplog) -> None:
    pytest.importorskip("numpy")
    clock = FakeClock()
    cuda = SwitchableCudaSession(should_fail=True)
    backend, cpu = _backend(cooldown=300, clock=clock, cuda=cuda)

    backend.rerank("q", ["c1"])  # opens the breaker at t=0

    clock.advance(300)  # cooldown elapsed, CUDA still failing
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="shim_backends"):
        scores = backend.rerank("q", ["c1"])

    assert scores == [1.0]
    assert cuda.run_calls == 2, "the post-cooldown retry must attempt CUDA"
    assert cpu.run_calls == 2
    # Still open (never closed), so this is a refresh of the cooldown window,
    # not a fresh open transition.
    assert "rerank_cuda_breaker_open" not in caplog.text


def test_zero_cooldown_disables_the_breaker(caplog) -> None:
    pytest.importorskip("numpy")
    clock = FakeClock()
    cuda = SwitchableCudaSession(should_fail=True)
    backend, cpu = _backend(cooldown=0, clock=clock, cuda=cuda)

    with caplog.at_level(logging.WARNING, logger="shim_backends"):
        backend.rerank("q", ["c1"])
    assert cuda.run_calls == 1
    assert "rerank_cuda_breaker_open" not in caplog.text

    clock.advance(1)  # irrelevant, the breaker is disabled
    cuda.should_fail = False
    scores = backend.rerank("q", ["c1"])

    assert scores == [7.0]
    assert cuda.run_calls == 2, "with cooldown=0 every request retries CUDA immediately"


def test_no_cuda_failure_never_opens_the_breaker(caplog) -> None:
    pytest.importorskip("numpy")
    clock = FakeClock()
    cuda = SwitchableCudaSession(should_fail=False)
    backend, cpu = _backend(cooldown=300, clock=clock, cuda=cuda)

    with caplog.at_level(logging.WARNING, logger="shim_backends"):
        scores = backend.rerank("q", ["c1"])

    assert scores == [7.0]
    assert cuda.run_calls == 1
    assert cpu.run_calls == 0
    assert "rerank_cuda_breaker" not in caplog.text
