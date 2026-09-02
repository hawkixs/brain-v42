"""Bounded decoding of the observations pushed by the MCP process."""

from __future__ import annotations

import json

import pytest

from brain_v42.metrics.client_observation import (
    MAX_CALLS_PER_OBSERVATION,
    MAX_OBSERVATION_BYTES,
    MAX_OBSERVATIONS,
    decode_observations,
)
from brain_v42.metrics.codex_telemetry import (
    CodexTelemetryLimitError,
    CodexTelemetryMalformedError,
)

FAKE_UUID = "12345678-1234-4abc-8def-1234567890ab"


def _payload(items: list[dict[str, object]]) -> bytes:
    return json.dumps({"observations": items}).encode()


def _payload_of_exactly(size: int) -> bytes:
    """A valid, minimal payload whose length is EXACTLY ``size``.

    A single observation, padded with a long actor: the byte bound is thus the only
    one of the three the payload can cross. Padding by multiplying observations
    would make the cardinality bound raise first, and the test would go green
    pinning the wrong guard.
    """
    padding = size - len(_payload([{"actor": "", "calls": 1}]))
    payload = _payload([{"actor": "a" * padding, "calls": 1}])
    assert len(payload) == size
    return payload


class TestDecodeObservations:
    def test_full_observation(self) -> None:
        observation = decode_observations(
            _payload([{"actor": "brain-v42", "session": FAKE_UUID, "calls": 3}])
        )[0]
        assert observation.actor == "brain-v42"
        assert observation.session_id == FAKE_UUID
        assert observation.calls == 3

    def test_session_is_optional(self) -> None:
        observation = decode_observations(_payload([{"actor": "codex", "calls": 1}]))[0]
        assert observation.session_id is None

    def test_actor_is_normalized(self) -> None:
        observation = decode_observations(
            _payload([{"actor": "/home/hawixs/git/red-lab", "calls": 1}])
        )[0]
        assert observation.actor == "red-lab"

    def test_non_uuid_session_is_dropped_not_fatal(self) -> None:
        # An unreadable session degrades the row into "unattributed". Rejecting the
        # whole batch would punish the valid observations of the same send.
        observation = decode_observations(
            _payload([{"actor": "codex", "session": "nope", "calls": 1}])
        )[0]
        assert observation.session_id is None

    def test_missing_root_key_is_malformed(self) -> None:
        with pytest.raises(CodexTelemetryMalformedError):
            decode_observations(json.dumps({"nope": []}).encode())

    def test_non_integer_calls_is_malformed(self) -> None:
        with pytest.raises(CodexTelemetryMalformedError):
            decode_observations(_payload([{"actor": "codex", "calls": "1"}]))

    def test_negative_calls_is_malformed(self) -> None:
        with pytest.raises(CodexTelemetryMalformedError):
            decode_observations(_payload([{"actor": "codex", "calls": -1}]))

    def test_too_many_observations_raises_limit(self) -> None:
        items = [{"actor": "a", "calls": 1}] * (MAX_OBSERVATIONS + 1)
        with pytest.raises(CodexTelemetryLimitError):
            decode_observations(_payload(items))

    def test_calls_at_the_cap_are_accepted(self) -> None:
        """Positive control for the next bound, and an off-by-one guard.

        Without it, a cap lowered to zero would pass the overflow test without
        anything flinching.
        """
        observation = decode_observations(
            _payload([{"actor": "probe", "calls": MAX_CALLS_PER_OBSERVATION}])
        )[0]
        assert observation.calls == MAX_CALLS_PER_OBSERVATION

    def test_calls_over_the_cap_raises_limit(self) -> None:
        """``calls`` is an integer declared by the caller, hence unbounded.

        ``record_observations`` ADDS it to the current counter. A buggy local
        process, or a diagnostic curl posting
        ``{"observations":[{"actor":"probe","calls":10**18}]}``, therefore sets
        ``brain_calls`` to 10^18 in ``/api/cockpit`` — and the next batch adds on
        top, until the TTL expires. Nothing else downstream reads this value back:
        the decoder's bound is the only one.
        """
        with pytest.raises(CodexTelemetryLimitError):
            decode_observations(
                _payload([{"actor": "probe", "calls": MAX_CALLS_PER_OBSERVATION + 1}])
            )

    def test_payload_at_the_byte_cap_is_accepted(self) -> None:
        """Positive control for the next bound: at the limit, we decode.

        Without it, "the oversized payload raises" would go green for any reason at
        all — a malformed payload, a cap at zero.
        """
        observation = decode_observations(_payload_of_exactly(MAX_OBSERVATION_BYTES))[0]
        assert observation.calls == 1

    def test_payload_over_the_byte_cap_raises_limit(self) -> None:
        """The byte bound belongs to the DECODER, not to the HTTP receiver alone.

        The receiver already bounds the body it reads, which masks this guard
        end-to-end. Relying on it leaves the module unbounded on its own terms: the
        next caller — a second receiver, a diagnostic script, a test — would inherit
        a capless decoder. It is therefore exercised here, at the decoder level.
        """
        with pytest.raises(CodexTelemetryLimitError):
            decode_observations(_payload_of_exactly(MAX_OBSERVATION_BYTES + 1))


FAKE_TRANSPORT = "0f9d2c1b3a4e5f60718293a4b5c6d7e8"


class TestTransportField:
    """``transport`` crosses the wire without ever being confused with ``session``.

    The two fields live in disjoint key spaces: ``session`` promises a join with
    the OTLP, ``transport`` identifies a connection and promises none. Mixing them
    would produce rows announcing a join they will never make.
    """

    def test_transport_is_decoded(self) -> None:
        (obs,) = decode_observations(
            _payload([{"actor": "red-lab", "calls": 1, "transport": FAKE_TRANSPORT}])
        )
        assert obs.transport == FAKE_TRANSPORT

    def test_absent_transport_is_none(self) -> None:
        (obs,) = decode_observations(_payload([{"actor": "red-lab", "calls": 1}]))
        assert obs.transport is None

    def test_malformed_transport_is_dropped_not_raised(self) -> None:
        """An unreadable value means "no transport", never a refusal of the batch.

        The batch can carry up to 64 observations: making a single dubious value
        raise would throw away 63 honest measurements with it.
        """
        for bad in ("not-hex", FAKE_TRANSPORT.upper(), 12345, None, "a" * 4096):
            (obs,) = decode_observations(
                _payload([{"actor": "red-lab", "calls": 1, "transport": bad}])
            )
            assert obs.transport is None, f"transport accepté à tort : {bad!r}"

    def test_session_and_transport_are_independent(self) -> None:
        (obs,) = decode_observations(
            _payload(
                [
                    {
                        "actor": "red-lab",
                        "calls": 1,
                        "session": FAKE_UUID,
                        "transport": FAKE_TRANSPORT,
                    }
                ]
            )
        )
        assert obs.session_id == FAKE_UUID
        assert obs.transport == FAKE_TRANSPORT

    def test_transport_in_the_session_field_is_refused(self) -> None:
        """The hex32 shape is not a session: putting it there must join nothing."""
        (obs,) = decode_observations(
            _payload([{"actor": "red-lab", "calls": 1, "session": FAKE_TRANSPORT}])
        )
        assert obs.session_id is None
