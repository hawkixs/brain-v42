"""Format de fil des observations d'activité poussées par le processus MCP.

Le middleware de provenance vit dans le serveur MCP (:8765), le registre dans
le sidecar métriques (:9200). Ce module décrit le peu qui traverse la socket
loopback entre les deux, avec les mêmes bornes que le récepteur OTLP.
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
    # Identifiant de CONNEXION frappé par le serveur, distinct de ``session_id``
    # qui identifie une conversation d'agent. Le premier ne joint rien côté
    # OTLP ; le second est la clé de jointure. Un seul champ pour les deux
    # produirait des lignes annonçant une jointure qu'elles ne feront jamais.
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
                # Illisible vaut « non déclaré », jamais un refus du lot : un
                # lot porte jusqu'à MAX_OBSERVATIONS mesures, et lever sur une
                # seule valeur douteuse en jetterait 63 honnêtes avec elle.
                transport=normalize_transport(transport if isinstance(transport, str) else None),
            )
        )
    return tuple(decoded)
