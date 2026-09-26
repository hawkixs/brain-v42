"""Tests of the GPU-reranking behaviour of services/embedding_shim/shim_backends.py.

Focused module, next to test_embedding_shim.py: provider selection, length
sorting + micro-batching, and the CUDA-failure-falls-back-to-CPU path. All
doubles are FAKE session/tokenizer objects — no real onnxruntime or GPU here.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

SHIM_DIR = Path(__file__).resolve().parents[2] / "services" / "embedding_shim"
sys.path.insert(0, str(SHIM_DIR))

from shim_backends import OnnxRerankBackend, _resolve_providers  # noqa: E402


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


class TaggedFakeTokenizer:
    """encode_batch pads (like a real tokenizer) to the call's own longest
    candidate, keeps the real per-row length recoverable via attention_mask,
    and stamps a caller-chosen identity tag in ids[0] so a test can check
    which output row belongs to which candidate regardless of any internal
    re-ordering."""

    def __init__(self, spec: dict[str, tuple[int, int]]) -> None:
        # candidate text -> (real_token_length, identity_tag)
        self._spec = spec
        self.batches: list[list[tuple[str, str]]] = []

    def encode_batch(self, pairs: list[tuple[str, str]]) -> list[_Encoding]:
        self.batches.append(list(pairs))
        real_lengths = [self._spec[c][0] for _, c in pairs]
        padded = max(real_lengths, default=0)
        encodings = []
        for _, c in pairs:
            real_length, tag = self._spec[c]
            ids = [tag] + [0] * (padded - 1) if padded else [tag]
            attention_mask = [1] * real_length + [0] * (padded - real_length)
            type_ids = [0] * len(ids)
            encodings.append(_Encoding(ids, attention_mask, type_ids))
        return encodings


class TaggedFakeSession:
    """run() returns the ids[0] tag of every row (as a float) — the test
    checks the returned scores against the tags it assigned to candidates."""

    def __init__(self, providers: list[str] | None = None) -> None:
        self.batch_sizes: list[int] = []
        self._providers = providers or ["CPUExecutionProvider"]

    def get_inputs(self) -> list[_FakeInput]:
        return _fake_inputs()

    def get_providers(self) -> list[str]:
        return self._providers

    def run(self, _outputs, feeds):
        n = feeds["input_ids"].shape[0]
        self.batch_sizes.append(n)
        return [feeds["input_ids"][:, 0].astype(float).reshape(-1, 1)]


def _tagged_backend(spec: dict[str, tuple[int, int]], batch_size: int = 32) -> OnnxRerankBackend:
    backend = OnnxRerankBackend("unused.onnx", "unused.json", batch_size=batch_size)
    backend._session = TaggedFakeSession()
    backend._tokenizer = TaggedFakeTokenizer(spec)
    return backend


# --- construction validation -------------------------------------------------


def test_construction_rejects_invalid_device():
    with pytest.raises(ValueError):
        OnnxRerankBackend("m.onnx", "t.json", device="tpu")


@pytest.mark.parametrize("batch_size", [0, -1, -5])
def test_construction_rejects_non_positive_batch_size(batch_size):
    with pytest.raises(ValueError):
        OnnxRerankBackend("m.onnx", "t.json", batch_size=batch_size)


# --- provider selection -------------------------------------------------------


def test_resolve_providers_auto_prefers_cuda_when_available():
    assert _resolve_providers("auto", ["CUDAExecutionProvider", "CPUExecutionProvider"]) == [
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    ]


def test_resolve_providers_auto_falls_back_to_cpu_when_unavailable():
    assert _resolve_providers("auto", ["CPUExecutionProvider"]) == ["CPUExecutionProvider"]


def test_resolve_providers_cpu_forces_cpu_even_when_cuda_available():
    assert _resolve_providers("cpu", ["CUDAExecutionProvider", "CPUExecutionProvider"]) == [
        "CPUExecutionProvider"
    ]


def test_resolve_providers_cuda_requested_and_available():
    assert _resolve_providers("cuda", ["CUDAExecutionProvider", "CPUExecutionProvider"]) == [
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    ]


def test_resolve_providers_cuda_requested_but_unavailable_falls_back_to_cpu(caplog):
    with caplog.at_level(logging.WARNING, logger="shim_backends"):
        result = _resolve_providers("cuda", ["CPUExecutionProvider"])
    assert result == ["CPUExecutionProvider"]
    assert "rerank_cuda_requested_unavailable" in caplog.text


# --- session construction (_load): ORT fallback + CUDA construction failure --


class RecordingSession:
    """Stands in for onnxruntime.InferenceSession — records disable_fallback()
    calls instead of doing anything with a real model."""

    def __init__(self, providers: list[str]) -> None:
        self._providers = providers
        self.disable_fallback_calls = 0

    def disable_fallback(self) -> None:
        self.disable_fallback_calls += 1

    def get_providers(self) -> list[str]:
        return self._providers


class _FakeTokenizerHandle:
    def enable_truncation(self, max_length: int) -> None:
        pass

    def enable_padding(self) -> None:
        pass


class _FakeTokenizerClass:
    @staticmethod
    def from_file(path: str) -> _FakeTokenizerHandle:
        return _FakeTokenizerHandle()


class _FakeTokenizersModule:
    Tokenizer = _FakeTokenizerClass


class FakeOnnxRuntimeModule:
    """Stands in for the `onnxruntime` module — no real model/GPU involved."""

    def __init__(
        self,
        session_factory,
        available_providers: list[str],
    ) -> None:
        self._session_factory = session_factory
        self._available_providers = available_providers

    def get_available_providers(self) -> list[str]:
        return self._available_providers

    def InferenceSession(self, model_path: str, providers: list[str]) -> Any:
        return self._session_factory(providers)


def _install_fake_onnx_modules(monkeypatch, session_factory, available_providers):
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        FakeOnnxRuntimeModule(session_factory, available_providers),
    )
    monkeypatch.setitem(sys.modules, "tokenizers", _FakeTokenizersModule())


def test_load_disables_onnxruntime_automatic_session_fallback(monkeypatch):
    """MAJOR review finding (PR #231): onnxruntime's own EPFail fallback would
    silently retry a failed CUDA run on CPU inside session.run() itself and
    return scores without ever raising — the explicit retry/breaker in
    rerank() would never see the failure, so the breaker would never open and
    the session could stay pinned to CPU even after the GPU recovers.
    disable_fallback() (onnxruntime 1.30 API, Session.disable_fallback in
    onnxruntime_inference_collection.py) forces run() failures to surface as
    exceptions instead.
    """
    sessions: list[RecordingSession] = []

    def factory(providers: list[str]) -> RecordingSession:
        session = RecordingSession(providers)
        sessions.append(session)
        return session

    _install_fake_onnx_modules(
        monkeypatch, factory, available_providers=["CUDAExecutionProvider", "CPUExecutionProvider"]
    )

    backend = OnnxRerankBackend("m.onnx", "t.json", device="cuda")
    backend._load()

    assert len(sessions) == 1
    assert sessions[0].disable_fallback_calls == 1


def test_load_cuda_session_creation_failure_falls_back_to_cpu_and_opens_breaker(
    monkeypatch, caplog
):
    """MAJOR review finding (PR #231): CUDA listed as available but session
    CONSTRUCTION failing (unusable GPU/driver) used to raise straight out of
    _load(), before the run-level retry/breaker ever got a chance to act."""
    cpu_sessions: list[RecordingSession] = []

    def factory(providers: list[str]) -> RecordingSession:
        if providers[0] == "CUDAExecutionProvider":
            raise RuntimeError("CUDA driver initialization failed")
        session = RecordingSession(providers)
        cpu_sessions.append(session)
        return session

    _install_fake_onnx_modules(
        monkeypatch, factory, available_providers=["CUDAExecutionProvider", "CPUExecutionProvider"]
    )

    backend = OnnxRerankBackend("m.onnx", "t.json", device="cuda")

    with caplog.at_level(logging.WARNING, logger="shim_backends"):
        session, _tokenizer = backend._load()

    assert session.get_providers() == ["CPUExecutionProvider"]
    assert len(cpu_sessions) == 1
    assert session.disable_fallback_calls == 1
    assert "rerank_cuda_breaker_open" in caplog.text
    assert backend._cuda_breaker_is_open() is True


def test_load_cpu_only_construction_failure_still_raises(monkeypatch):
    """A CPU-only construction failure has no CUDA fallback to reach for — it
    must still surface, not be swallowed."""

    def factory(providers: list[str]) -> RecordingSession:
        raise RuntimeError("model file is corrupt")

    _install_fake_onnx_modules(monkeypatch, factory, available_providers=["CPUExecutionProvider"])

    backend = OnnxRerankBackend("m.onnx", "t.json", device="cpu")

    with pytest.raises(RuntimeError, match="model file is corrupt"):
        backend._load()


def test_load_cpu_fallback_concurrent_first_calls_create_exactly_one_session():
    """MINOR review finding (PR #231): CPU fallback session creation was
    unlocked. Two threads racing _load_cpu_fallback() on its first call (both
    reading self._cpu_session as None before either assigns it) would each
    construct their own session.

    The (monkeypatched) session construction sleeps just long enough to widen
    the check-then-act window well past the gap between the two threads'
    near-simultaneous start() calls, so the race — if unprotected — reproduces
    deterministically rather than depending on scheduler luck.
    """
    backend = OnnxRerankBackend("m.onnx", "t.json")
    created: list[object] = []

    def slow_create_session(providers: list[str]) -> object:
        time.sleep(0.05)
        session = RecordingSession(providers)
        created.append(session)
        return session

    backend._create_session = slow_create_session  # type: ignore[method-assign]

    threads = [threading.Thread(target=backend._load_cpu_fallback) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(created) == 1
    assert backend._cpu_session is created[0]


# --- length sorting + order restoration --------------------------------------


def test_rerank_restores_original_order_after_length_sort():
    candidates = ["ccc", "a", "bb"]
    spec = {"ccc": (3, 300), "a": (1, 100), "bb": (2, 200)}
    backend = _tagged_backend(spec, batch_size=10)

    scores = backend.rerank("q", candidates)

    assert scores == [300.0, 100.0, 200.0]


def test_rerank_restores_original_order_with_tied_lengths():
    candidates = ["xx", "yy", "zz"]
    spec = {"xx": (2, 10), "yy": (2, 20), "zz": (2, 30)}
    backend = _tagged_backend(spec, batch_size=10)

    scores = backend.rerank("q", candidates)

    assert scores == [10.0, 20.0, 30.0]


def test_rerank_single_candidate():
    spec = {"only": (4, 42)}
    backend = _tagged_backend(spec, batch_size=10)

    assert backend.rerank("q", ["only"]) == [42.0]


# --- micro-batch boundaries ---------------------------------------------------


def _sequential_spec(candidates: list[str]) -> dict[str, tuple[int, int]]:
    return {c: (len(c), (i + 1) * 100) for i, c in enumerate(candidates)}


def test_rerank_microbatch_exact_batch_size_is_a_single_call():
    candidates = ["a", "bb"]
    backend = _tagged_backend(_sequential_spec(candidates), batch_size=2)

    scores = backend.rerank("q", candidates)

    assert backend._session.batch_sizes == [2]
    assert scores == [100.0, 200.0]


def test_rerank_microbatch_one_over_batch_size_splits_in_two_calls():
    candidates = ["a", "bb", "ccc"]
    backend = _tagged_backend(_sequential_spec(candidates), batch_size=2)

    scores = backend.rerank("q", candidates)

    assert backend._session.batch_sizes == [2, 1]
    assert scores == [100.0, 200.0, 300.0]


# --- CUDA failure falls back to CPU -------------------------------------------


class FailingCudaSession:
    def __init__(self) -> None:
        self.run_calls = 0

    def get_inputs(self) -> list[_FakeInput]:
        return _fake_inputs()

    def get_providers(self) -> list[str]:
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]

    def run(self, _outputs, feeds):
        self.run_calls += 1
        raise RuntimeError("CUDA allocation failed")


class FixedScoreCpuSession:
    def get_inputs(self) -> list[_FakeInput]:
        return _fake_inputs()

    def get_providers(self) -> list[str]:
        return ["CPUExecutionProvider"]

    def run(self, _outputs, feeds):
        import numpy as np

        n = feeds["input_ids"].shape[0]
        return [np.array([[42.0] for _ in range(n)])]


def test_rerank_cuda_failure_falls_back_to_cpu_and_returns_cpu_scores(caplog):
    pytest.importorskip("numpy")
    backend = OnnxRerankBackend("m.onnx", "t.json")
    backend._session = FailingCudaSession()
    backend._tokenizer = TaggedFakeTokenizer({"c1": (2, 1), "c2": (3, 2)})
    backend._cpu_session = FixedScoreCpuSession()

    with caplog.at_level(logging.WARNING, logger="shim_backends"):
        scores = backend.rerank("q", ["c1", "c2"])

    assert scores == [42.0, 42.0]
    assert backend._session.run_calls == 1
    assert "rerank_cuda_run_failed_retrying_cpu" in caplog.text


def test_rerank_cpu_session_failure_is_not_swallowed():
    class FailingCpuSession:
        def get_inputs(self) -> list[_FakeInput]:
            return _fake_inputs()

        def get_providers(self) -> list[str]:
            return ["CPUExecutionProvider"]

        def run(self, _outputs, feeds):
            raise RuntimeError("boom")

    backend = OnnxRerankBackend("m.onnx", "t.json")
    backend._session = FailingCpuSession()
    backend._tokenizer = TaggedFakeTokenizer({"c1": (2, 1)})

    with pytest.raises(RuntimeError, match="boom"):
        backend.rerank("q", ["c1"])
