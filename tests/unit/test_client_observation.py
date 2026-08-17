"""Décodage borné des observations poussées par le processus MCP."""

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
    """Une charge valide et minimale dont la longueur vaut EXACTEMENT ``size``.

    Une seule observation, rembourrée par un acteur long : la borne d'octets
    est ainsi la seule des trois que la charge puisse franchir. Rembourrer en
    multipliant les observations ferait lever la borne de cardinalité d'abord,
    et le test passerait au vert en épinglant la mauvaise garde.
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
        # Une session illisible dégrade la ligne en « non attribué ».
        # Rejeter tout le lot punirait les observations valides du même envoi.
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
        """Contrôle positif de la borne suivante, et garde d'un décalage d'un.

        Sans lui, un plafond descendu à zéro passerait le test de dépassement
        sans que rien ne bronche.
        """
        observation = decode_observations(
            _payload([{"actor": "probe", "calls": MAX_CALLS_PER_OBSERVATION}])
        )[0]
        assert observation.calls == MAX_CALLS_PER_OBSERVATION

    def test_calls_over_the_cap_raises_limit(self) -> None:
        """``calls`` est un entier déclaré par l'appelant, donc non borné.

        ``record_observations`` l'ADDITIONNE au compteur courant. Un processus
        local bogué, ou un curl de diagnostic postant
        ``{"observations":[{"actor":"probe","calls":10**18}]}``, met donc
        ``brain_calls`` à 10^18 dans ``/api/cockpit`` — et le lot suivant
        s'ajoute par-dessus, jusqu'à l'expiration du TTL. Rien d'autre en aval
        ne relit cette valeur : la borne du décodeur est la seule.
        """
        with pytest.raises(CodexTelemetryLimitError):
            decode_observations(
                _payload([{"actor": "probe", "calls": MAX_CALLS_PER_OBSERVATION + 1}])
            )

    def test_payload_at_the_byte_cap_is_accepted(self) -> None:
        """Contrôle positif de la borne suivante : à la limite, on décode.

        Sans lui, « la charge surdimensionnée lève » passerait au vert pour
        n'importe quelle raison — une charge malformée, un plafond à zéro.
        """
        observation = decode_observations(_payload_of_exactly(MAX_OBSERVATION_BYTES))[0]
        assert observation.calls == 1

    def test_payload_over_the_byte_cap_raises_limit(self) -> None:
        """La borne d'octets appartient au DÉCODEUR, pas au seul récepteur HTTP.

        Le récepteur borne déjà le corps qu'il lit, ce qui masque cette garde
        de bout en bout. S'en remettre à lui laisse le module non borné pour
        lui-même : le prochain appelant — un second récepteur, un script de
        diagnostic, un test — hériterait d'un décodeur sans plafond. Elle est
        donc éprouvée ici, au niveau du décodeur.
        """
        with pytest.raises(CodexTelemetryLimitError):
            decode_observations(_payload_of_exactly(MAX_OBSERVATION_BYTES + 1))


FAKE_TRANSPORT = "0f9d2c1b3a4e5f60718293a4b5c6d7e8"


class TestTransportField:
    """``transport`` traverse le fil sans jamais se confondre avec ``session``.

    Les deux champs vivent dans des espaces de clés disjoints : ``session``
    promet une jointure avec l'OTLP, ``transport`` identifie une connexion et
    n'en promet aucune. Les mélanger produirait des lignes qui annoncent une
    jointure qu'elles ne feront jamais.
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
        """Une valeur illisible vaut « pas de transport », jamais un refus du lot.

        Le lot peut porter jusqu'à 64 observations : faire lever une seule
        valeur douteuse jetterait 63 mesures honnêtes avec elle.
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
        """La forme hex32 n'est pas une session : la poser là ne doit rien joindre."""
        (obs,) = decode_observations(
            _payload([{"actor": "red-lab", "calls": 1, "session": FAKE_TRANSPORT}])
        )
        assert obs.session_id is None
