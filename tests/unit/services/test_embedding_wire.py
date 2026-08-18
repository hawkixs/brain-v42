"""Wire shapes for the embedding backends (TDD Red phase).

The wire owns request shaping and response parsing ONLY. Retry, backoff, the
503 gpu_busy handling and the EmbeddingUnavailable contract stay in
GPUEmbeddingService, so every backend inherits the graceful degradation that
lets brain_search fall back to FTS.
"""

from __future__ import annotations

import math

import pytest

from brain_v42.services.embedding_wire import OpenAIWire, ShimWire


class TestShimWireIsTodaysContract:
    def test_batch_targets_the_private_batch_route(self) -> None:
        assert ShimWire().batch_request(["a", "b"]) == ("/embed", {"texts": ["a", "b"]})

    def test_single_targets_the_private_single_route(self) -> None:
        assert ShimWire().single_request("a") == ("/embed/query", {"text": "a"})

    def test_health_path_is_healthz(self) -> None:
        assert ShimWire().health_path == "/healthz"

    def test_parsing_is_a_passthrough(self) -> None:
        wire = ShimWire()
        assert wire.parse_batch([[1.0], [2.0]], expected=2) == [[1.0], [2.0]]
        assert wire.parse_single([1.0, 2.0]) == [1.0, 2.0]


class TestOpenAIWireSpeaksV1Embeddings:
    def test_batch_posts_model_and_input(self) -> None:
        path, body = OpenAIWire(model="text-embedding-3-small").batch_request(["a", "b"])
        assert path == "/v1/embeddings"
        assert body == {
            "model": "text-embedding-3-small",
            "input": ["a", "b"],
            "encoding_format": "float",
        }

    def test_single_is_a_batch_of_one(self) -> None:
        """Retires the /embed/query misnomer entirely on this backend."""
        path, body = OpenAIWire(model="m").single_request("a")
        assert path == "/v1/embeddings"
        assert body["input"] == ["a"]

    def test_health_path_defaults_to_the_models_listing(self) -> None:
        assert OpenAIWire(model="m").health_path == "/v1/models"

    def test_text_is_bounded_so_the_shims_guard_does_not_vanish(self) -> None:
        wire = OpenAIWire(model="m", max_text_chars=10)
        _, body = wire.batch_request(["x" * 50])
        assert body["input"] == ["x" * 10]


class TestOpenAIWireParsingIsIndexFaithful:
    @staticmethod
    def _payload(vectors: list[list[float]], indices: list[int] | None = None) -> dict:
        idx = indices if indices is not None else list(range(len(vectors)))
        return {"data": [{"index": i, "embedding": v} for i, v in zip(idx, vectors, strict=True)]}

    def test_out_of_order_data_is_restored_to_input_order(self) -> None:
        payload = self._payload([[0.0, 1.0], [1.0, 0.0]], indices=[1, 0])
        result = OpenAIWire(model="m").parse_batch(payload, expected=2)
        assert result == [[1.0, 0.0], [0.0, 1.0]]

    def test_vectors_are_l2_normalised(self) -> None:
        payload = self._payload([[3.0, 4.0]])
        (vec,) = OpenAIWire(model="m").parse_batch(payload, expected=1)
        assert math.isclose(math.sqrt(sum(v * v for v in vec)), 1.0)
        assert math.isclose(vec[0], 0.6) and math.isclose(vec[1], 0.8)

    def test_a_zero_vector_is_left_alone_rather_than_dividing_by_zero(self) -> None:
        payload = self._payload([[0.0, 0.0]])
        assert OpenAIWire(model="m").parse_batch(payload, expected=1) == [[0.0, 0.0]]

    def test_a_short_result_set_is_refused(self) -> None:
        """`sorted()` alone would silently return fewer vectors than texts,
        and the caller would zip them against the wrong rows."""
        payload = self._payload([[1.0]])
        with pytest.raises(ValueError, match="expected 2"):
            OpenAIWire(model="m").parse_batch(payload, expected=2)

    def test_duplicate_indices_are_refused(self) -> None:
        payload = self._payload([[1.0], [2.0]], indices=[0, 0])
        with pytest.raises(ValueError, match="indices"):
            OpenAIWire(model="m").parse_batch(payload, expected=2)

    def test_a_non_numeric_embedding_is_refused_before_it_reaches_pgvector(self) -> None:
        """Some providers answer base64 unless encoding_format is honoured."""
        payload = {"data": [{"index": 0, "embedding": "eyJhIjogMX0="}]}
        with pytest.raises(ValueError):
            OpenAIWire(model="m").parse_batch(payload, expected=1)

    def test_a_non_finite_float_is_refused(self) -> None:
        payload = self._payload([[float("nan"), 1.0]])
        with pytest.raises(ValueError):
            OpenAIWire(model="m").parse_batch(payload, expected=1)

    def test_single_unwraps_the_one_vector(self) -> None:
        payload = self._payload([[3.0, 4.0]])
        assert OpenAIWire(model="m").parse_single(payload) == [0.6, 0.8]
