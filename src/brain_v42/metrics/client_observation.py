"""Wire format of the activity observations pushed by the MCP process.

The provenance middleware lives in the MCP server (:8765), the registry in the
metrics sidecar (:9200). This module describes the little that crosses the
loopback socket between the two, with the same bounds as the OTLP receiver.
"""

from __future__ import annotations

from dataclasses import dataclass

from brain_v42.metrics.codex_telemetry import (
    CodexTelemetryLimitError,
    _load_json,
    _raise_malformed,
)
from brain_v42.provenance import normalize_agent, normalize_session, normalize_transport

MAX_OBSERVATION_BYTES = 16_384
MAX_OBSERVATIONS = 64
MAX_CALLS_PER_OBSERVATION = 1_000_000


@dataclass(frozen=True, slots=True)
class ClientObservation:
    actor: str
    session_id: str | None
    calls: int
    # CONNECTION identifier minted by the server, distinct from ``session_id``
    # which identifies an agent conversation. The first joins nothing on the
    # OTLP side; the second is the join key. A single field for both would
    # produce rows announcing a join they will never make.
    transport: str | None = None


def decode_observations(payload: bytes) -> tuple[ClientObservation, ...]:
    """Valider un lot complet d'observations avant toute application."""
    if len(payload) > MAX_OBSERVATION_BYTES:
        raise CodexTelemetryLimitError
    root = _load_json(payload)
    items = root.get("observations")
    if not isinstance(items, list):
        _raise_malformed()
    if len(items) > MAX_OBSERVATIONS:
        raise CodexTelemetryLimitError

    decoded: list[ClientObservation] = []
    for item in items:
        if not isinstance(item, dict):
            _raise_malformed()
        actor = item.get("actor")
        calls = item.get("calls")
        if not isinstance(actor, str):
            _raise_malformed()
        if not isinstance(calls, int) or isinstance(calls, bool):
            _raise_malformed()
        if calls < 0:
            _raise_malformed()
        if calls > MAX_CALLS_PER_OBSERVATION:
            raise CodexTelemetryLimitError
        session = item.get("session")
        transport = item.get("transport")
        decoded.append(
            ClientObservation(
                actor=normalize_agent(actor),
                session_id=normalize_session(session if isinstance(session, str) else None),
                calls=calls,
                # Unreadable counts as "not declared", never a refusal of the
                # batch: a batch carries up to MAX_OBSERVATIONS measurements,
                # and raising on a single doubtful value would throw 63 honest
                # ones away with it.
                transport=normalize_transport(transport if isinstance(transport, str) else None),
            )
        )
    return tuple(decoded)
