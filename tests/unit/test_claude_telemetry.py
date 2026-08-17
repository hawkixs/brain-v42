"""Décodage borné des logs OTLP/HTTP JSON de Claude Code.

L'oracle des noms d'attributs est `tests/fixtures/claude_otlp_logs.json`, capture
réelle du spike (Claude Code 2.1.220, verdict dans
`docs/upstream/2026-08-06-claude-otlp-session-join.md`). Quand la fixture et le
plan divergent, la fixture gagne : elle a été mesurée, le plan supposait.

Trois écarts mesurés que ces tests figent :

1. `event.name` n'est PAS préfixé — l'attribut vaut `user_prompt`, le préfixe
   `claude_code.` vit dans `body.stringValue`.
2. `input_tokens` ne mesure pas le contexte (10 relevé quand `cache_read_tokens`
   valait 12 973) : les trois compteurs d'entrée doivent être projetés.
3. Chaque enregistrement porte `user.email` en clair. La projection par liste
   blanche est la seule barrière entre cette adresse et un registre exposé en
   HTTP — d'où le premier test du fichier.
"""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

import pytest

from brain_v42.metrics.claude_telemetry import (
    _PROJECTED_KEYS,
    decode_claude_logs,
)
from brain_v42.metrics.claude_telemetry import (
    # Le filtre lui-même, aliasé : `_attribute` plus bas FABRIQUE un attribut
    # OTLP, `_attributes` PROJETTE un enregistrement — un caractère d'écart
    # pour deux rôles opposés.
    _attributes as _project_attributes,
)
from brain_v42.metrics.codex_telemetry import (
    MAX_REQUEST_BYTES,
    CodexTelemetryLimitError,
    CodexTelemetryMalformedError,
)

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "claude_otlp_logs.json"

FAKE_UUID = "12345678-1234-4abc-8def-1234567890ab"
# Distincts de la session : réutiliser FAKE_UUID rendrait l'assertion d'absence
# ininterprétable, la session légitime portant alors la même chaîne.
ACCOUNT_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ORGANIZATION_UUID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

# Valeurs qui ne doivent JAMAIS ressortir du décodage, quelle que soit la forme.
SENSITIVE_VALUES: dict[str, str] = {
    "user.email": "personne@exemple.test",
    "user.id": "e" * 64,
    "user.account_uuid": ACCOUNT_UUID,
    "user.account_id": "compte-en-clair-42",
    "organization.id": ORGANIZATION_UUID,
    "prompt": "phrase secrete de l operateur",
}

# Fragments de nom qui trahissent un canal de données personnelles, qu'il
# s'agisse d'un champ rendu ou d'une clé admise en amont.
PERSONAL_DATA_MARKERS = ("email", "user", "account", "organization", "prompt")

# Valeurs de la liste blanche : leur survie est le contrôle positif de toute
# assertion d'absence de ce fichier.
PROJECTED_MODEL = "claude-opus-5"


def _attribute(key: str, value: dict[str, object]) -> dict[str, object]:
    return {"key": key, "value": value}


def _record(
    *,
    event_name: str = "user_prompt",
    session_id: str | None = FAKE_UUID,
    timestamp: str = "1786039576188000000",
    extra: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    attributes = [_attribute("event.name", {"stringValue": event_name})]
    if session_id is not None:
        attributes.append(_attribute("session.id", {"stringValue": session_id}))
    attributes.extend(extra or [])
    return {"timeUnixNano": timestamp, "attributes": attributes}


def _envelope(records: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {"resourceLogs": [{"scopeLogs": [{"logRecords": records}]}]},
        separators=(",", ":"),
    ).encode()


def _sensitive_attributes() -> list[dict[str, object]]:
    return [_attribute(key, {"stringValue": value}) for key, value in SENSITIVE_VALUES.items()]


def _readable_state(record: object) -> dict[str, Any]:
    """Toutes les valeurs lisibles d'un enregistrement décodé, nom par nom.

    Ne suppose pas la dataclass : un décodeur qui stockerait les attributs bruts
    dans un objet quelconque doit être vu par ce test.
    """
    if is_dataclass(record) and not isinstance(record, type):
        names = [field.name for field in fields(record)]
    else:
        names = [
            name
            for name in dir(record)
            if not name.startswith("__") and not callable(getattr(record, name))
        ]
    return {name: getattr(record, name) for name in names}


class TestPersonalDataNeverSurvives:
    """La liste blanche est la seule barrière avant un registre exposé en HTTP.

    Trois témoins, parce qu'une barrière se casse de trois façons : le rendu
    (un champ personnel dans l'enregistrement décodé), la LISTE (une clé
    personnelle admise) et le FILTRE (la liste plus consultée du tout). Les
    deux premiers ne voient rien du troisième.
    """

    def test_account_identifiers_never_survive_the_projection(self) -> None:
        payload = _envelope(
            [
                _record(
                    event_name="api_request",
                    extra=[
                        _attribute("model", {"stringValue": PROJECTED_MODEL}),
                        _attribute("input_tokens", {"intValue": 10}),
                        *_sensitive_attributes(),
                    ],
                ),
                # Mesuré : les événements de hook portent AUSSI l'e-mail en clair.
                _record(event_name="hook_execution_start", extra=_sensitive_attributes()),
            ]
        )

        # Contrôle d'entrée : les sentinelles sont bien dans la charge soumise.
        for key, value in SENSITIVE_VALUES.items():
            assert value.encode() in payload, f"sentinelle {key} absente de la charge de test"

        records = decode_claude_logs(payload)

        assert len(records) == 1, "l'événement de hook ne doit produire aucun enregistrement"
        state = _readable_state(records[0])
        rendered_by_name = {name: repr(value) for name, value in state.items()}

        # Contrôle positif : le canal de lecture montre bien les valeurs projetées.
        # Sans lui, « aucune sentinelle trouvée » passerait au vert sur un objet vide.
        assert any(PROJECTED_MODEL in rendered for rendered in rendered_by_name.values()), (
            f"le modèle projeté n'apparaît dans aucun champ lu : {rendered_by_name}"
        )
        assert any(FAKE_UUID in rendered for rendered in rendered_by_name.values()), (
            f"la session projetée n'apparaît dans aucun champ lu : {rendered_by_name}"
        )

        for name, rendered in rendered_by_name.items():
            for key, value in SENSITIVE_VALUES.items():
                assert value not in rendered, f"{key} a survécu dans le champ {name}"

        # Aucun champ ne doit même offrir un canal nommé pour ces données.
        for name in rendered_by_name:
            lowered = name.lower()
            for marker in PERSONAL_DATA_MARKERS:
                assert marker not in lowered, f"le champ {name} ouvre un canal « {marker} »"

        # Et rien ne doit fuir par une structure annexe du résultat.
        whole = repr(records)
        for key, value in SENSITIVE_VALUES.items():
            assert value not in whole, f"{key} a survécu dans le résultat complet"

    def test_the_whitelist_admits_no_personal_data_key(self) -> None:
        """La barrière amont, testée pour elle-même.

        Le test ci-dessus ne lit que les champs de l'enregistrement rendu :
        il ne voit donc QUE la seconde barrière, la liste figée des champs.
        Mesuré par mutation — ajouter ``user.email`` à ``_PROJECTED_KEYS`` sans
        toucher à la dataclass lui laissait passer les douze tests. Un tel
        élargissement ne fuit rien le jour où il est fait ; il arme la fuite
        pour le champ suivant qu'on ajoutera. La liste blanche étant décrite
        comme « la seule barrière », elle doit mordre seule.
        """
        # Contrôle positif : sans lui, une liste blanche vidée ou renommée
        # rendrait les deux assertions suivantes vraies pour rien.
        assert {"session.id", "event.name", "model"} <= _PROJECTED_KEYS

        admitted = SENSITIVE_VALUES.keys() & _PROJECTED_KEYS
        assert not admitted, f"clés personnelles mesurées admises : {sorted(admitted)}"

        for key in sorted(_PROJECTED_KEYS):
            lowered = key.lower()
            for marker in PERSONAL_DATA_MARKERS:
                assert marker not in lowered, f"la liste blanche admet {key} (canal « {marker} »)"

    def test_the_projection_drops_every_key_outside_the_whitelist(self) -> None:
        """Le FILTRE, éprouvé sur ce qu'il retient — pas sur ce que la liste contient.

        Les deux tests ci-dessus ne peuvent pas voir ce troisième défaut.
        Mesuré le 2026-08-07 : retirer entièrement
        ``if key not in _PROJECTED_KEYS: continue`` laisse ``decode_claude_logs``
        rendre un ``ClaudeRecord`` IDENTIQUE, champ pour champ, sur une charge
        portant les cinq identifiants personnels. La dataclass est gelée : un
        attribut non projeté n'a nulle part où atterrir, donc aucune assertion
        sur l'objet rendu ne peut mordre. Une liste blanche jamais consultée
        reste pourtant une barrière morte, et le prochain champ ajouté à la
        dataclass la traverserait sans rien casser.

        Le seul endroit où la barrière est observable est donc la projection
        elle-même : quatre clés retenues sur les dix soumises ici. C'est ce que
        ce test mesure, à l'endroit exact où le filtre s'applique.
        """
        record = _record(
            event_name="api_request",
            extra=[
                _attribute("model", {"stringValue": PROJECTED_MODEL}),
                _attribute("input_tokens", {"intValue": 10}),
                *_sensitive_attributes(),
            ],
        )

        retained = _project_attributes(record)

        leaked = SENSITIVE_VALUES.keys() & retained.keys()
        assert not leaked, f"clés personnelles retenues par la projection : {sorted(leaked)}"

        # Exhaustif : mord aussi sur une clé personnelle qu'on n'a pas mesurée.
        # Contrôle positif inclus — une projection vide échouerait ici, ce qui
        # interdit à l'assertion d'absence ci-dessus de passer pour rien.
        assert set(retained) == {"event.name", "session.id", "model", "input_tokens"}


class TestEventFiltering:
    def test_bare_event_name_is_recognized_and_prefixed_one_is_not(self) -> None:
        """Le préfixe `claude_code.` est dans le CORPS, pas dans `event.name`.

        Mesuré le 2026-08-06. Les deux enregistrements sont soumis ensemble :
        un décodeur qui filtrerait sur `claude_code.*` ne rendrait rien, un
        décodeur qui ne filtrerait rien en rendrait deux.
        """
        payload = _envelope(
            [
                _record(event_name="claude_code.user_prompt"),
                _record(event_name="user_prompt"),
            ]
        )

        records = decode_claude_logs(payload)

        assert [record.event_name for record in records] == ["user_prompt"]

    def test_unknown_event_is_ignored_while_known_event_survives(self) -> None:
        payload = _envelope(
            [
                _record(event_name="tool_decision"),
                _record(event_name="plugin_loaded"),
                _record(event_name="api_request"),
            ]
        )

        records = decode_claude_logs(payload)

        assert [record.event_name for record in records] == ["api_request"]


class TestCounters:
    def test_the_three_input_counters_are_projected(self) -> None:
        """`input_tokens` seul sous-estimerait le contexte de trois ordres."""
        payload = _envelope(
            [
                _record(
                    event_name="api_request",
                    extra=[
                        _attribute("input_tokens", {"intValue": 10}),
                        _attribute("cache_read_tokens", {"intValue": 11_776}),
                        _attribute("cache_creation_tokens", {"intValue": 6_804}),
                        _attribute("output_tokens", {"intValue": 43}),
                    ],
                )
            ]
        )

        record = decode_claude_logs(payload)[0]

        assert record.input_tokens == 10
        assert record.cache_read_tokens == 11_776
        assert record.cache_creation_tokens == 6_804
        assert record.output_tokens == 43

    def test_negative_cost_is_dropped_and_positive_cost_survives(self) -> None:
        payload = _envelope(
            [
                _record(
                    event_name="api_request",
                    extra=[_attribute("cost_usd", {"doubleValue": -1.0})],
                ),
                _record(
                    event_name="api_request",
                    extra=[_attribute("cost_usd", {"doubleValue": 0.0125})],
                ),
            ]
        )

        negative, positive = decode_claude_logs(payload)

        assert negative.cost_usd is None
        # Contrôle positif : sans lui, un décodeur qui perdrait TOUT coût passerait.
        assert positive.cost_usd == pytest.approx(0.0125)

    def test_absent_model_falls_back_to_unknown_and_present_model_is_kept(self) -> None:
        payload = _envelope(
            [
                _record(event_name="api_request"),
                _record(
                    event_name="api_request",
                    extra=[_attribute("model", {"stringValue": PROJECTED_MODEL})],
                ),
            ]
        )

        absent, present = decode_claude_logs(payload)

        assert absent.model == "unknown"
        assert present.model == PROJECTED_MODEL


class TestMalformedSession:
    def test_non_uuid_session_is_malformed(self) -> None:
        with pytest.raises(CodexTelemetryMalformedError):
            decode_claude_logs(_envelope([_record(session_id="not-a-uuid")]))

    def test_missing_session_is_malformed(self) -> None:
        with pytest.raises(CodexTelemetryMalformedError):
            decode_claude_logs(_envelope([_record(session_id=None)]))

    def test_canonical_session_is_accepted(self) -> None:
        """Contrôle positif des deux rejets ci-dessus.

        Sans lui, un décodeur qui lèverait sur TOUTE charge les ferait passer.
        """
        record = decode_claude_logs(_envelope([_record(session_id=FAKE_UUID)]))[0]

        assert record.session_id == FAKE_UUID


class TestPayloadBounds:
    def test_payload_over_the_shared_limit_is_rejected(self) -> None:
        oversized = b"{" + b" " * MAX_REQUEST_BYTES + b"}"

        assert len(oversized) > MAX_REQUEST_BYTES
        with pytest.raises(CodexTelemetryLimitError):
            decode_claude_logs(oversized)

    def test_payload_at_the_shared_limit_still_decodes(self) -> None:
        """Contrôle positif : le rejet ci-dessus tient à la taille, pas au reste."""
        envelope = _envelope([_record()])
        padded = envelope + b" " * (MAX_REQUEST_BYTES - len(envelope))

        assert len(padded) == MAX_REQUEST_BYTES
        assert len(decode_claude_logs(padded)) == 1


class TestRealCapture:
    """La capture du spike est l'oracle : si ce test échoue, c'est le code."""

    def test_recorded_capture_decodes_with_its_measured_counters(self) -> None:
        records = decode_claude_logs(FIXTURE_PATH.read_bytes())

        assert [record.event_name for record in records] == ["user_prompt", "api_request"]
        assert {record.session_id for record in records} == {FAKE_UUID}

        prompt, request = records
        assert prompt.timestamp == 1786039576188000000
        assert request.model == "claude-haiku-4-5-20251001"
        assert request.input_tokens == 10
        assert request.cache_read_tokens == 12_973
        assert request.cache_creation_tokens == 5_606
        assert request.output_tokens == 43
        assert request.cost_usd == pytest.approx(0.0127343)
