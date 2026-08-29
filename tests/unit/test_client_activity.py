"""Le registre généralisé — équivalence avec l'ancien, puis fusion.

La fusion réunit deux sources qui ne se recouvrent pas : la télémétrie OTLP
d'un CLI (tokens, tours, coût) et l'activité observée côté brain (appels de
tools). La clé de jointure est le pseudonyme HMAC de l'UUID de session.

Mesuré le 2026-08-06 (``docs/upstream/2026-08-06-claude-otlp-session-join.md``) :
aucun client ne sait déclarer sa session dans un en-tête MCP aujourd'hui. Le cas
NOMINAL est donc deux lignes disjointes — une OTLP-only, une « non attribué » —
et non une ligne jointe. Les deux situations sont épinglées ici : la jointure
parce que le code doit la porter pour le jour où un client le pourra, la
disjonction parce que c'est ce que la production produit.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest

from brain_v42.metrics.client_activity import (
    ACTIVITY_TTL_SECONDS,
    MAX_ACTIVE_CONVERSATIONS,
    ClientActivityRegistry,
)
from brain_v42.metrics.client_observation import ClientObservation
from brain_v42.metrics.codex_telemetry import CodexConversationRegistry

FAKE_UUID = "12345678-1234-4abc-8def-1234567890ab"
OTHER_UUID = "87654321-4321-4cba-8fed-ba0987654321"
SECRET = b"\x02" * 32
OTHER_SECRET = b"\x03" * 32
PSEUDONYM_PATTERN = re.compile(r"claude-[0-9a-f]{32}")
CODEX_PSEUDONYM_PATTERN = re.compile(r"codex-[0-9a-f]{32}")
# Côté brain, l'identifiant ne nomme aucun agent : le processus MCP ne sait
# pas quel CLI l'appelle, et un préfixe en inventerait un.
SESSION_PATTERN = re.compile(r"session-[0-9a-f]{32}")


class _Clock:
    """Horloge monotone pilotée par le test.

    Un itérateur de valeurs figées casserait dès que l'implémentation lit
    l'horloge une fois de plus ou de moins ; ici seul le temps écoulé compte.
    """

    def __init__(self) -> None:
        self._value = 0.0

    def __call__(self) -> float:
        return self._value

    def advance(self, seconds: float) -> None:
        self._value += seconds


def _wall_clock() -> datetime:
    return datetime(2026, 8, 6, 12, 31, tzinfo=UTC)


def _registry(clock: _Clock | None = None, secret: bytes = SECRET) -> ClientActivityRegistry:
    return ClientActivityRegistry(
        secret=secret,
        clock=clock or _Clock(),
        wall_clock=_wall_clock,
    )


def _attribute(key: str, value: dict[str, object]) -> dict[str, object]:
    return {"key": key, "value": value}


def _envelope(records: list[dict[str, object]]) -> bytes:
    return json.dumps({"resourceLogs": [{"scopeLogs": [{"logRecords": records}]}]}).encode()


def _claude_prompt(*, session_id: str = FAKE_UUID, timestamp: str = "1") -> dict[str, object]:
    return {
        "timeUnixNano": timestamp,
        "attributes": [
            _attribute("event.name", {"stringValue": "user_prompt"}),
            _attribute("session.id", {"stringValue": session_id}),
        ],
    }


def _claude_api_request(
    *,
    session_id: str = FAKE_UUID,
    timestamp: str = "2",
    model: str = "claude-opus-5",
    input_tokens: int = 10,
    cache_read_tokens: int = 1_000,
    cache_creation_tokens: int = 190,
    output_tokens: int = 340,
    cost_usd: float = 0.05,
) -> dict[str, object]:
    return {
        "timeUnixNano": timestamp,
        "attributes": [
            _attribute("event.name", {"stringValue": "api_request"}),
            _attribute("session.id", {"stringValue": session_id}),
            _attribute("model", {"stringValue": model}),
            _attribute("input_tokens", {"intValue": input_tokens}),
            _attribute("cache_read_tokens", {"intValue": cache_read_tokens}),
            _attribute("cache_creation_tokens", {"intValue": cache_creation_tokens}),
            _attribute("output_tokens", {"intValue": output_tokens}),
            _attribute("cost_usd", {"doubleValue": cost_usd}),
        ],
    }


def _claude_session(session_id: str = FAKE_UUID) -> bytes:
    """Une session Claude nominale : un prompt, puis une requête d'API."""
    return _envelope(
        [
            _claude_prompt(session_id=session_id),
            _claude_api_request(session_id=session_id),
        ]
    )


def _codex_payload(*, conversation_id: str = OTHER_UUID, timestamp: str = "1") -> bytes:
    return _envelope(
        [
            {
                "timeUnixNano": timestamp,
                "attributes": [
                    _attribute("event.name", {"stringValue": "codex.user_prompt"}),
                    _attribute("conversation.id", {"stringValue": conversation_id}),
                    _attribute("model", {"stringValue": "gpt-5.4"}),
                ],
            }
        ]
    )


def _codex_completion(
    *, conversation_id: str = OTHER_UUID, timestamp: str = "2", tokens: int = 12_345
) -> bytes:
    """Une complétion Codex, seule source des ``ctx_tokens`` hérités."""
    return _envelope(
        [
            {
                "timeUnixNano": timestamp,
                "attributes": [
                    _attribute("event.name", {"stringValue": "codex.sse_event"}),
                    _attribute("event.kind", {"stringValue": "response.completed"}),
                    _attribute("conversation.id", {"stringValue": conversation_id}),
                    _attribute("model", {"stringValue": "gpt-5.4"}),
                    _attribute("input_token_count", {"intValue": tokens}),
                ],
            }
        ]
    )


def _nth_uuid(index: int) -> str:
    """Un UUID canonique distinct par index, pour peupler le registre."""
    return f"00000000-0000-4000-8000-{index:012d}"


def _rows(registry: ClientActivityRegistry) -> dict[str, dict[str, object]]:
    """Indexer les lignes par identifiant, en refusant tout doublon d'id.

    Sans ce garde, deux lignes partageant un id s'écraseraient l'une l'autre
    dans le dictionnaire et le test compterait une ligne là où le registre en
    produit deux.
    """
    clients = registry.snapshot()["clients"]
    assert isinstance(clients, list)
    rows = {str(row["id"]): row for row in clients}
    assert len(rows) == len(clients), "deux lignes de clients partagent le même id"
    return rows


def test_codex_registry_is_the_generalized_registry() -> None:
    assert CodexConversationRegistry is ClientActivityRegistry


def test_empty_snapshot_keeps_the_legacy_shape() -> None:
    registry = ClientActivityRegistry(secret=b"\x01" * 32)
    snapshot = registry.snapshot()
    assert snapshot["active_convs"] == 0
    assert snapshot["ctx_tokens"] == 0
    assert snapshot["activeConvs"] == []


class TestJoin:
    def test_same_session_from_both_sources_yields_one_row(self) -> None:
        """La jointure fonctionne — mais aucun client ne la déclenche aujourd'hui.

        Mesuré le 2026-08-06 : ni Claude Code ni Codex ne savent déclarer leur
        session dans un en-tête MCP. Ce test garde la mécanique vivante et
        testée pour le jour où l'un des deux le pourra ; il construit donc une
        observation que la production ne produit pas encore. C'est délibéré :
        ne pas le « corriger » en test d'absence, celui-ci existe à côté.
        """
        registry = _registry()
        registry.ingest_claude_otlp_json(_claude_session())
        registry.record_observations(
            (ClientObservation(actor="brain-v42", session_id=FAKE_UUID, calls=4),)
        )

        rows = _rows(registry)
        assert len(rows) == 1
        (identifier,) = rows
        assert PSEUDONYM_PATTERN.fullmatch(identifier)
        assert rows[identifier] == {
            "id": identifier,
            "kind": "session",
            "agent": "claude",
            "actor": "brain-v42",
            "started": "12:31",
            "last_seen_s": 0,
            "model": "claude-opus-5",
            "turns": 1,
            "tokens": 1_200,
            "cost": 0.05,
            "brain_calls": 4,
        }

    def test_a_codex_that_declares_its_session_joins_its_own_row(self) -> None:
        """La jointure ne doit pas marcher pour un seul des deux agents.

        ``X-Brain-Agent`` porte un nom de projet, pas une marque de CLI : le
        brain ne sait pas QUI l'appelle. Hacher son observation avec le sel
        d'un agent en invente un — mesuré, celui de Claude pour tout le monde.
        Un Codex qui déclarerait sa session sortait alors en ligne fantôme
        ``claude-…`` à côté de sa vraie ligne ``codex-…``, sans jamais joindre.

        Ce test épingle AUSSI l'égalité de la décision ``4890a475`` : la ligne
        OTLP est clée par ``conversation.id``, l'observation brain déclare le
        même UUID comme SESSION — la jointure ne tient que parce que, pour
        Codex, les deux identifiants sont strictement égaux (863ff2ca).
        """
        registry = _registry()
        registry.ingest_otlp_json(_codex_payload())
        registry.record_observations(
            (ClientObservation(actor="codex", session_id=OTHER_UUID, calls=3),)
        )

        rows = _rows(registry)
        assert len(rows) == 1
        (identifier,) = rows
        assert CODEX_PSEUDONYM_PATTERN.fullmatch(identifier)
        assert rows[identifier]["agent"] == "codex"
        assert rows[identifier]["actor"] == "codex"
        assert rows[identifier]["brain_calls"] == 3

    def test_the_brain_side_identifier_claims_no_agent(self) -> None:
        """Une observation seule ne sait pas de quel CLI elle vient.

        Elle doit donc sortir sous un identifiant qui ne nomme aucun agent.
        S'annoncer ``claude-…`` serait une affirmation que rien ne mesure :
        ici l'acteur est ``red-lab``, qui n'est pas plus Claude que Codex.
        """
        registry = _registry()
        registry.record_observations(
            (ClientObservation(actor="red-lab", session_id=FAKE_UUID, calls=2),)
        )

        (identifier,) = _rows(registry)
        assert PSEUDONYM_PATTERN.fullmatch(identifier) is None
        assert CODEX_PSEUDONYM_PATTERN.fullmatch(identifier) is None
        assert SESSION_PATTERN.fullmatch(identifier)

    def test_tokens_sum_the_three_input_counters(self) -> None:
        """``input_tokens`` seul mentirait d'un facteur mille.

        Relevé du 2026-08-06 : ``input_tokens=10`` pour un contexte réel de
        18590 tokens, le reste vivant dans ``cache_read_tokens`` et
        ``cache_creation_tokens``. Les quatre compteurs de la charge sont
        choisis distincts : 1200 n'est atteignable ni par ``input_tokens``
        seul (10), ni par le seul cache (1190), ni par la somme de tout,
        sortie comprise (1540).
        """
        registry = _registry()
        registry.ingest_claude_otlp_json(_claude_session())
        (row,) = _rows(registry).values()
        assert row["tokens"] == 10 + 1_000 + 190

    def test_claude_today_yields_two_rows_not_one(self) -> None:
        """Le cas NOMINAL mesuré : Claude ne déclare pas sa session.

        Son en-tête arrive en gabarit non expansé, que ``normalize_session``
        rejette. On obtient donc une ligne OTLP-only et une ligne résiduelle
        distinctes — exactement la situation de Codex. C'est le comportement
        attendu en production, pas une dégradation accidentelle.
        """
        registry = _registry()
        registry.ingest_claude_otlp_json(_claude_session())
        registry.record_observations(
            (ClientObservation(actor="brain-v42", session_id=None, calls=4),)
        )

        rows = _rows(registry)
        assert len(rows) == 2
        residual = rows["unattributed:brain-v42"]
        (otlp,) = (row for key, row in rows.items() if key != "unattributed:brain-v42")
        # Les deux moitiés de la même session, côte à côte et non jointes.
        # Chaque absence est appariée à sa présence dans l'autre ligne : sans
        # ce couple, un « is None » passerait au vert sur un registre mort.
        assert otlp["tokens"] == 1_200
        assert otlp["brain_calls"] is None
        assert residual["brain_calls"] == 4
        assert residual["tokens"] is None

    def test_the_row_identifier_depends_on_the_process_secret(self) -> None:
        """Le pseudonyme est un HMAC, pas un hachage nu.

        Deux processus voient deux identifiants pour la même session, et
        personne ne remonte à l'UUID en devinant une empreinte.
        """
        first = _registry()
        first.ingest_claude_otlp_json(_claude_session())
        second = _registry(secret=OTHER_SECRET)
        second.ingest_claude_otlp_json(_claude_session())

        (first_id,) = _rows(first)
        (second_id,) = _rows(second)
        assert PSEUDONYM_PATTERN.fullmatch(first_id)
        assert PSEUDONYM_PATTERN.fullmatch(second_id)
        assert first_id != second_id

    def test_raw_identifiers_never_appear_in_the_snapshot(self) -> None:
        """Ni l'UUID de session ni le ``conversation.id`` ne sortent du registre."""
        registry = _registry()
        registry.ingest_claude_otlp_json(_claude_session())
        registry.ingest_otlp_json(_codex_payload())
        registry.record_observations(
            (ClientObservation(actor="brain-v42", session_id=FAKE_UUID, calls=1),)
        )

        serialized = json.dumps(registry.snapshot())
        # Contrôle positif : les deux lignes sont bien là et le pseudonyme est
        # bien dans la charge sérialisée. L'absence de l'UUID ne vient donc pas
        # d'un instantané vide.
        assert len(_rows(registry)) == 2
        assert PSEUDONYM_PATTERN.search(serialized) is not None
        for raw in (FAKE_UUID, OTHER_UUID):
            assert raw not in serialized
            assert raw.replace("-", "") not in serialized


class TestRowShapes:
    def test_otlp_only_row_has_no_brain_columns(self) -> None:
        registry = _registry()
        registry.ingest_claude_otlp_json(_claude_session())

        rows = _rows(registry)
        (identifier,) = rows
        assert PSEUDONYM_PATTERN.fullmatch(identifier)
        assert rows[identifier] == {
            "id": identifier,
            "kind": "session",
            "agent": "claude",
            "actor": None,
            "started": "12:31",
            "last_seen_s": 0,
            "model": "claude-opus-5",
            "turns": 1,
            "tokens": 1_200,
            "cost": 0.05,
            "brain_calls": None,
        }

    def test_brain_only_session_row_has_no_token_columns(self) -> None:
        """``agent`` vaut ``None``, et l'identifiant le dit aussi.

        Cette ligne épinglait ``claude-…`` pour une observation de ``red-lab``,
        c'est-à-dire un agent affirmé là où la colonne voisine avoue n'en
        connaître aucun. L'identifiant neutre est le seul cohérent des deux.
        """
        registry = _registry()
        registry.record_observations(
            (ClientObservation(actor="red-lab", session_id=FAKE_UUID, calls=2),)
        )

        rows = _rows(registry)
        (identifier,) = rows
        assert SESSION_PATTERN.fullmatch(identifier)
        assert rows[identifier] == {
            "id": identifier,
            "kind": "session",
            "agent": None,
            "actor": "red-lab",
            "started": None,
            "last_seen_s": 0,
            "model": None,
            "turns": None,
            "tokens": None,
            "cost": None,
            "brain_calls": 2,
        }

    def test_unattributed_row_fills_only_what_it_measured(self) -> None:
        """Seuls ``actor``, ``brain_calls`` et ``last_seen_s`` ont une source.

        Le reste vaut ``null`` et jamais ``0`` : un zéro cosmétique serait
        indiscernable d'un vrai zéro mesuré.
        """
        registry = _registry()
        registry.record_observations((ClientObservation(actor="codex", session_id=None, calls=7),))

        assert _rows(registry) == {
            "unattributed:codex": {
                "id": "unattributed:codex",
                "kind": "unattributed",
                "agent": None,
                "actor": "codex",
                "started": None,
                "last_seen_s": 0,
                "model": None,
                "turns": None,
                "tokens": None,
                "cost": None,
                "brain_calls": 7,
            }
        }

    def test_a_session_without_api_request_reports_null_not_zero(self) -> None:
        """Un prompt seul ne mesure ni contexte, ni coût, ni modèle."""
        registry = _registry()
        registry.ingest_claude_otlp_json(_envelope([_claude_prompt()]))

        (row,) = _rows(registry).values()
        # Contrôle positif : la ligne est bien alimentée par la source OTLP.
        assert row["turns"] == 1
        assert row["tokens"] is None
        assert row["cost"] is None
        assert row["model"] is None

    def test_the_model_survives_an_event_that_does_not_carry_one(self) -> None:
        """Une vraie session alterne requête d'API et prompt, sans fin.

        Toutes les autres charges de ce fichier ordonnent le prompt AVANT la
        requête d'API — l'ordre d'ouverture d'une session, joué une fois. La
        garde qui reporte le modèle déjà mesuré n'y est donc jamais sollicitée.

        Or dès que l'utilisateur tape la ligne suivante, ``user_prompt``
        arrive : il ne porte aucun attribut ``model``, ``_model_value`` rend
        ``unknown``, et la ligne traduit ``unknown`` en ``None``. Sans la
        garde, la colonne modèle du panneau retombe sur un tiret cadratin à
        chaque frappe et ne se rallume qu'à la réponse suivante, pendant que la
        session est bien vivante. C'est la confusion « null = non mesuré » que
        la doctrine interdit : ici le modèle EST mesuré, il l'a été au tour
        d'avant.
        """
        registry = _registry()
        registry.ingest_claude_otlp_json(_envelope([_claude_api_request(timestamp="2")]))
        # Contrôle positif : le modèle est bien mesuré avant le prompt suivant,
        # sinon sa survie ne prouverait rien.
        (row,) = _rows(registry).values()
        assert row["model"] == "claude-opus-5"

        registry.ingest_claude_otlp_json(_envelope([_claude_prompt(timestamp="3")]))

        (row,) = _rows(registry).values()
        assert row["model"] == "claude-opus-5"
        # Second contrôle positif : le prompt a bien été appliqué. Sans lui, un
        # lot ignoré — dédupliqué, jeté — laisserait le modèle intact pour une
        # tout autre raison que celle qu'on épingle.
        assert row["turns"] == 1

    def test_cost_accumulates_while_tokens_keep_the_latest_context(self) -> None:
        """Deux colonnes voisines, deux natures que la ligne ne traite pas pareil.

        Le coût est une dépense : il se cumule sur toute la session. Les tokens
        sont une taille de contexte : la dernière requête d'API fait foi, un
        cumul gonflerait la ligne à chaque tour.

        Une session à une seule requête d'API ne sait pas distinguer les deux —
        c'est le cas de toutes les autres charges de ce fichier. Il en faut donc
        deux, aux compteurs disjoints : 2400 n'est ni le premier contexte
        (1200), ni la somme des deux (3600).
        """
        registry = _registry()
        registry.ingest_claude_otlp_json(
            _envelope(
                [
                    _claude_api_request(timestamp="2", cost_usd=0.5),
                    _claude_api_request(
                        timestamp="3",
                        input_tokens=20,
                        cache_read_tokens=2_000,
                        cache_creation_tokens=380,
                        cost_usd=0.25,
                    ),
                ]
            )
        )

        (row,) = _rows(registry).values()
        assert row["cost"] == pytest.approx(0.75)
        assert row["tokens"] == 20 + 2_000 + 380


class TestUnattributed:
    def test_residual_and_otlp_rows_coexist_for_codex(self) -> None:
        """Codex sort en N conversations PLUS une ligne non attribuée.

        Ce n'est pas un doublon : sa configuration MCP n'expose aucun
        identifiant de conversation, donc ses appels de tools ne sont
        attribuables à aucune de ses conversations. Le panneau montre ce trou
        au lieu de le combler par une corrélation inventée.
        """
        registry = _registry()
        registry.ingest_otlp_json(_codex_payload())
        registry.record_observations((ClientObservation(actor="codex", session_id=None, calls=3),))

        rows = _rows(registry)
        assert len(rows) == 2
        assert rows["unattributed:codex"]["brain_calls"] == 3
        (conversation,) = (row for key, row in rows.items() if key != "unattributed:codex")
        assert conversation["kind"] == "session"
        assert conversation["agent"] == "codex"
        assert conversation["brain_calls"] is None

    def test_calls_accumulate_across_observations(self) -> None:
        registry = _registry()
        for _ in range(3):
            registry.record_observations(
                (ClientObservation(actor="codex", session_id=None, calls=1),)
            )
        assert _rows(registry)["unattributed:codex"]["brain_calls"] == 3


class TestActorLabel:
    """L'acteur est déclaré par le client : il sert d'étiquette, pas de texte.

    ``normalize_agent`` ne fait que strip / basename / troncature — aucune
    borne sur les caractères. Un ``X-Brain-Agent`` hostile traversait donc le
    registre verbatim jusqu'au JSON de ``/api/cockpit``, à la fois en colonne
    ``actor`` et dans l'``id`` de la ligne non attribuée. La ligne héritée
    écrit ``topic: "[redacted]"`` et le décodeur OTLP effondre un ``model``
    non conforme sur ``unknown`` — précisément pour cette raison ; la moitié
    brain n'avait pas son équivalent.

    Ce qu'un client fait du rendu ne se décide pas ici : on borne à la source.
    """

    HOSTILE = "</script><img src=x onerror=alert(1)>"

    def test_a_hostile_actor_reaches_neither_the_actor_column_nor_the_id(self) -> None:
        registry = _registry()
        registry.record_observations(
            (ClientObservation(actor=self.HOSTILE, session_id=None, calls=3),)
        )

        rows = _rows(registry)
        (row,) = rows.values()
        assert row["actor"] == "_rejected"
        assert row["id"] == "unattributed:_rejected"
        # La mesure survit au rejet de l'étiquette : on refuse le libellé, pas
        # l'observation. Sans ce contrôle, jeter l'observation passerait vert.
        assert row["brain_calls"] == 3
        serialized = json.dumps(registry.snapshot())
        assert self.HOSTILE not in serialized
        assert "<" not in serialized
        assert "onerror" not in serialized

    def test_a_hostile_actor_is_bounded_on_the_joined_row_too(self) -> None:
        """La colonne ``actor`` de la ligne jointe est un second chemin.

        Elle lit le même acteur stocké mais depuis une autre branche de la
        projection : une correction posée sur la seule ligne résiduelle la
        laisserait fuir.
        """
        registry = _registry()
        registry.ingest_claude_otlp_json(_claude_session())
        registry.record_observations(
            (ClientObservation(actor=self.HOSTILE, session_id=FAKE_UUID, calls=4),)
        )

        rows = _rows(registry)
        assert len(rows) == 1
        (row,) = rows.values()
        assert row["actor"] == "_rejected"
        # Contrôle positif : la jointure a bien eu lieu, sinon la colonne
        # ``actor`` vaudrait ``None`` et l'assertion ci-dessus ne prouverait
        # rien de l'assainissement.
        assert row["brain_calls"] == 4
        assert row["tokens"] == 1_200

    @pytest.mark.parametrize(
        "actor",
        ["brain-v42", "red-lab", "codex", "dream-codex-synth", "_unexpanded", "unknown", "a" * 64],
    )
    def test_a_legitimate_actor_reaches_the_panel_intact(self, actor: str) -> None:
        """Contrôle positif indispensable.

        Sans lui, un assainissement qui effondrerait TOUT sur la sentinelle
        passerait au vert tout en effaçant le seul renseignement de la ligne.
        Les six premières valeurs sont celles que ``normalize_agent`` produit
        réellement en production, sentinelles comprises ; la septième est la
        troncature à ``MAX_ACTOR_LENGTH``, longueur légitime maximale.
        """
        registry = _registry()
        registry.record_observations((ClientObservation(actor=actor, session_id=None, calls=1),))

        rows = _rows(registry)
        assert rows[f"unattributed:{actor}"]["actor"] == actor

    def test_two_hostile_labels_collapse_into_a_single_bucket(self) -> None:
        """Un seul seau, pas un pseudonyme par littéral.

        C'est le choix déjà fait par ``normalize_agent`` pour ``_unexpanded``
        et par le décodeur OTLP pour ``unknown``. Il borne aussi l'arrosage :
        un client qui inventerait mille étiquettes hostiles occuperait mille
        lignes du plafond de 64, et évincerait les acteurs légitimes.
        """
        registry = _registry()
        registry.record_observations(
            (
                ClientObservation(actor="<a onerror=1>", session_id=None, calls=1),
                ClientObservation(actor="<b onerror=2>", session_id=None, calls=2),
            )
        )

        rows = _rows(registry)
        assert list(rows) == ["unattributed:_rejected"]
        assert rows["unattributed:_rejected"]["brain_calls"] == 3


class TestLegacyContract:
    def test_snapshot_keeps_the_legacy_keys_next_to_clients(self) -> None:
        """Le contrat est additif : le panneau livré lit encore ces trois clés."""
        registry = _registry()
        registry.ingest_otlp_json(_codex_payload())
        registry.record_observations((ClientObservation(actor="codex", session_id=None, calls=1),))

        snapshot = registry.snapshot()
        legacy = snapshot["activeConvs"]
        assert isinstance(legacy, list)
        assert legacy == [
            {
                "id": legacy[0]["id"],
                "topic": "[redacted]",
                "agent": "codex",
                "started": "12:31",
                "turns": 1,
                "tokens": 0,
                "model": "gpt-5.4",
                "cost": None,
            }
        ]
        assert snapshot["active_convs"] == 1
        assert snapshot["ctx_tokens"] == 0
        assert len(_rows(registry)) == 2

    def test_brain_observations_stay_out_of_the_legacy_list(self) -> None:
        """Une observation brain-side n'est pas une conversation OTLP.

        La faire apparaître dans ``activeConvs`` gonflerait le compteur de
        conversations du panneau livré avec des lignes sans tokens.
        """
        registry = _registry()
        registry.record_observations((ClientObservation(actor="codex", session_id=None, calls=7),))

        snapshot = registry.snapshot()
        assert snapshot["activeConvs"] == []
        assert snapshot["active_convs"] == 0
        assert snapshot["ctx_tokens"] == 0
        # Contrôle positif : l'observation existe bien, ailleurs.
        assert _rows(registry)["unattributed:codex"]["brain_calls"] == 7

    def test_legacy_list_never_mislabels_a_claude_row_as_codex(self) -> None:
        """Le panneau livré affiche ``activeConvs`` sous le titre « Codex ».

        Une session Claude qui s'y présenterait comme ``codex`` lui mentirait.
        """
        codex_registry = _registry()
        codex_registry.ingest_otlp_json(_codex_payload())
        legacy_codex = codex_registry.snapshot()["activeConvs"]
        assert isinstance(legacy_codex, list)
        # Contrôle positif : l'étiquette existe et vaut « codex » pour Codex,
        # sans quoi l'assertion d'absence ci-dessous serait vide de sens.
        assert [entry["agent"] for entry in legacy_codex] == ["codex"]

        claude_registry = _registry()
        claude_registry.ingest_claude_otlp_json(_claude_session())
        legacy_claude = claude_registry.snapshot()["activeConvs"]
        assert isinstance(legacy_claude, list)
        assert "codex" not in [entry["agent"] for entry in legacy_claude]

    def test_a_claude_session_never_enters_the_legacy_codex_list(self) -> None:
        """``activeConvs`` compte des conversations Codex, et rien d'autre.

        Vérifier l'étiquette ne suffit pas : une session Claude admise dans la
        liste s'y annoncerait honnêtement comme ``claude``. Le mensonge serait
        dans les deux compteurs — ``active_convs`` et ``ctx_tokens`` — que le
        panneau « Codex activity » livré affiche aujourd'hui, avant sa bascule.
        """
        registry = _registry()
        registry.ingest_claude_otlp_json(_claude_session())

        snapshot = registry.snapshot()
        assert snapshot["activeConvs"] == []
        assert snapshot["active_convs"] == 0
        assert snapshot["ctx_tokens"] == 0
        # Contrôle positif : la session est bien mesurée, dans la moitié neuve
        # du registre. Sans lui, les trois absences ci-dessus passeraient au
        # vert sur un registre qui n'a rien ingéré.
        (row,) = _rows(registry).values()
        assert row["agent"] == "claude"
        assert row["tokens"] == 1_200


class TestBounds:
    def test_rows_expire_after_the_ttl(self) -> None:
        """Le TTL tient sur les deux moitiés du registre fusionné."""
        clock = _Clock()
        registry = _registry(clock)
        registry.ingest_claude_otlp_json(_claude_session())
        registry.record_observations((ClientObservation(actor="codex", session_id=None, calls=1),))
        # Contrôle positif : les deux lignes sont présentes avant l'échéance.
        assert len(_rows(registry)) == 2

        clock.advance(ACTIVITY_TTL_SECONDS + 1.0)
        assert registry.snapshot()["clients"] == []

    def test_last_seen_is_measured_on_both_halves_and_not_a_constant_zero(self) -> None:
        """``last_seen_s`` est une mesure, pas un zéro décoratif.

        Toutes les autres lignes de ce fichier sont lues à l'instant zéro de
        l'horloge, où un compteur figé à 0 est indiscernable d'un vrai écoulé
        nul. Il faut donc avancer l'horloge sans franchir le TTL, et le faire
        sur les deux moitiés : la ligne OTLP et la ligne non attribuée lisent
        deux horodatages distincts, dans deux branches distinctes du code.
        """
        clock = _Clock()
        registry = _registry(clock)
        registry.ingest_claude_otlp_json(_claude_session())
        registry.record_observations((ClientObservation(actor="codex", session_id=None, calls=1),))

        clock.advance(42.0)
        rows = _rows(registry)
        assert len(rows) == 2
        assert {row["last_seen_s"] for row in rows.values()} == {42}

    def test_brain_side_rows_are_capped(self) -> None:
        """Un acteur inventé par appel ne doit pas faire grossir le registre."""
        registry = _registry()
        registry.record_observations(
            tuple(
                ClientObservation(actor=f"projet-{index}", session_id=None, calls=1)
                for index in range(MAX_ACTIVE_CONVERSATIONS)
            )
        )
        # Contrôle positif : sous le plafond, rien n'est jeté.
        assert len(_rows(registry)) == MAX_ACTIVE_CONVERSATIONS

        registry.record_observations(
            tuple(
                ClientObservation(actor=f"debordement-{index}", session_id=None, calls=1)
                for index in range(5)
            )
        )
        assert len(_rows(registry)) == MAX_ACTIVE_CONVERSATIONS

    def test_the_brain_side_cap_keeps_the_newest_actor_not_the_first_arrived(self) -> None:
        """Compter les survivants ne dit rien du SENS de l'éviction.

        Le test voisin insère ses 69 acteurs sans jamais avancer l'horloge :
        les 69 ``last_seen`` valent le même instant, le tri de ``_trim_brain``
        n'a rien à trier, et ``[:64]`` retombe sur l'ordre d'insertion quel que
        soit le sens du tri. Un plafond inversé y reste vert.

        Or le plafond existe précisément parce qu'un acteur est déclaré par le
        client, donc de cardinalité non bornée. Une fois 64 acteurs distincts
        vus dans la fenêtre de 600 s, une politique « les 64 premiers arrivés
        gagnent » jette les ``brain_calls`` de toute session neuve — y compris
        celle de l'opérateur en train de travailler, qui n'apparaît alors
        jamais dans le panneau. Le panneau « live » devient un panneau
        « les 64 premiers arrivés ».

        Il faut donc faire avancer l'horloge pour que le tri ait de quoi trier.
        """
        clock = _Clock()
        registry = _registry(clock)
        registry.record_observations(
            tuple(
                ClientObservation(actor=f"ancien-{index}", session_id=None, calls=1)
                for index in range(MAX_ACTIVE_CONVERSATIONS)
            )
        )
        # Contrôle positif : le registre est déjà plein d'anciens, sans quoi
        # l'arrivée du nouvel acteur ne disputerait aucune place.
        assert len(_rows(registry)) == MAX_ACTIVE_CONVERSATIONS

        clock.advance(1.0)
        registry.record_observations(
            (ClientObservation(actor="operateur", session_id=None, calls=9),)
        )

        rows = _rows(registry)
        assert len(rows) == MAX_ACTIVE_CONVERSATIONS
        assert rows["unattributed:operateur"]["brain_calls"] == 9

    def test_one_batch_of_new_actors_never_erases_a_measured_join(self) -> None:
        """La borne d'UN lot valait la capacité TOTALE de la moitié brain.

        ``MAX_OBSERVATIONS`` vaut 64, ``MAX_ACTIVE_CONVERSATIONS`` aussi : un
        unique POST pouvait donc renouveler intégralement la table. Mesuré, une
        session Claude jointe des deux côtés — ``actor='brain_v42'``,
        ``brain_calls=7`` — redevenait ``actor=None``, ``brain_calls=None``
        après un seul POST de 64 acteurs neufs (2151 octets). Ce ne sont pas
        « des résidus évincés » : c'est une JOINTURE MESURÉE effacée, remplacée
        par des null que le panneau rend comme « rien de mesuré ». Il perd
        exactement ce pour quoi il existe.

        L'arbitrage ne touche pas au SENS de l'éviction épinglé juste au-dessus
        (e5cda111) : entre résidus, les plus récents gagnent toujours. Ce test
        n'oppose pas deux résidus, il oppose une ligne JOINTE à un lot de
        résidus neufs — et la jointure passe devant.
        """
        clock = _Clock()
        registry = _registry(clock)
        registry.ingest_claude_otlp_json(_claude_session())
        registry.record_observations(
            (ClientObservation(actor="brain_v42", session_id=FAKE_UUID, calls=7),)
        )
        # Contrôle positif : la jointure porte bien une mesure AVANT le lot,
        # sinon « elle survit » passerait au vert sur un registre qui n'a
        # jamais rien joint.
        before = _rows(registry)
        (identifier,) = before
        assert before[identifier]["actor"] == "brain_v42"
        assert before[identifier]["brain_calls"] == 7
        assert before[identifier]["tokens"] == 1_200

        clock.advance(1.0)
        registry.record_observations(
            tuple(
                ClientObservation(actor=f"neuf-{index}", session_id=None, calls=1)
                for index in range(MAX_ACTIVE_CONVERSATIONS)
            )
        )

        rows = _rows(registry)
        assert rows[identifier]["actor"] == "brain_v42"
        assert rows[identifier]["brain_calls"] == 7
        # Contrôle positif : le plafond borne TOUJOURS. Sans lui, la jointure
        # survivrait elle aussi, et les deux assertions ci-dessus passeraient
        # au vert sur un registre qui ne plafonne plus rien.
        assert len(rows) == MAX_ACTIVE_CONVERSATIONS

    def test_a_dead_conversation_protects_no_residual_row(self) -> None:
        """La priorité va à une jointure VIVANTE, pas au souvenir d'une jointure.

        Le chemin des observations ne purge pas la moitié OTLP. Sans y
        réappliquer le TTL, une conversation périmée depuis longtemps rangerait
        encore « sa » ligne brain devant tous les résidus — alors qu'à
        l'instantané suivant cette conversation est purgée et que la ligne
        retombe en ligne orpheline, sans agent, sans tours et sans tokens — un
        résidu comme un autre, mais plus ancien. Il gagnerait alors contre des
        résidus neufs : exactement le sens d'éviction qu'interdit e5cda111, par
        la porte de derrière.
        """
        clock = _Clock()
        registry = _registry(clock)
        registry.ingest_claude_otlp_json(_claude_session())
        registry.record_observations(
            (ClientObservation(actor="brain_v42", session_id=FAKE_UUID, calls=7),)
        )
        # Rafraîchir la moitié brain SANS rafraîchir la moitié OTLP : la ligne
        # brain restera vivante quand sa conversation, elle, aura expiré.
        clock.advance(500.0)
        registry.record_observations(
            (ClientObservation(actor="brain_v42", session_id=FAKE_UUID, calls=1),)
        )
        # Contrôle positif : à cet instant la jointure vit encore et se voit.
        before = _rows(registry)
        (identifier,) = before
        assert before[identifier]["brain_calls"] == 8

        # La conversation OTLP dépasse le TTL ; la ligne brain, elle, ne l'a pas
        # atteint et survit à ``_prune_brain``.
        clock.advance(200.0)
        registry.record_observations(
            tuple(
                ClientObservation(actor=f"neuf-{index}", session_id=None, calls=1)
                for index in range(MAX_ACTIVE_CONVERSATIONS)
            )
        )

        rows = _rows(registry)
        # L'identifiant de la ligne orpheline est la clé de session brute, que
        # le test ne peut pas recalculer sans le secret : c'est l'acteur qui la
        # nomme. Le viser par ``unattributed:brain_v42`` ne mordrait jamais.
        assert all(row["actor"] != "brain_v42" for row in rows.values())
        assert set(rows) == {
            f"unattributed:neuf-{index}" for index in range(MAX_ACTIVE_CONVERSATIONS)
        }

    def test_claude_traffic_never_evicts_the_codex_counters(self) -> None:
        """Le contrat hérité est ADDITIF : le trafic Claude ne le touche pas.

        Les deux moitiés OTLP partageaient un seul plafond de 64. Soixante-
        quatre sessions Claude dans la fenêtre TTL évinçaient donc la
        conversation Codex, et les trois clés que le panneau red-monitor livré
        consomme aujourd'hui retombaient à ``ctx_tokens=0``, ``active_convs=0``,
        ``activeConvs=[]``. C'est la pire façon de casser le contrat : écrire
        un zéro là où une vraie valeur mesurée se trouvait, indiscernable d'un
        zéro honnête.
        """
        registry = _registry()
        registry.ingest_otlp_json(_codex_payload())
        registry.ingest_otlp_json(_codex_completion())
        before = registry.snapshot()
        # Contrôle positif : les compteurs hérités portent bien une mesure
        # avant l'arrivée de Claude, sinon leur survie ne prouverait rien.
        assert before["active_convs"] == 1
        assert before["ctx_tokens"] == 12_345

        for index in range(MAX_ACTIVE_CONVERSATIONS):
            registry.ingest_claude_otlp_json(_claude_session(session_id=_nth_uuid(index)))

        after = registry.snapshot()
        assert after["active_convs"] == 1
        assert after["ctx_tokens"] == 12_345
        assert after["activeConvs"] == before["activeConvs"]

    def test_each_agent_is_capped_on_its_own(self) -> None:
        """Des plafonds par agent restent des plafonds.

        L'ensemble des agents est CLOS — l'étiquette vient du récepteur qui a
        décodé le lot, jamais du client — donc le registre reste borné par le
        plafond multiplié par le nombre de récepteurs, ici deux. Un acteur,
        lui, est déclaré par le client : c'est pourquoi la moitié brain garde
        un plafond global.
        """
        registry = _registry()
        for index in range(MAX_ACTIVE_CONVERSATIONS + 6):
            registry.ingest_claude_otlp_json(_claude_session(session_id=_nth_uuid(index)))

        assert len(_rows(registry)) == MAX_ACTIVE_CONVERSATIONS

    def test_codex_dedup_still_holds_on_the_codex_path(self) -> None:
        """La dédup par empreinte survit au déménagement, sur le chemin Codex.

        Un exportateur OTLP rejoue ses lots ; sans empreinte, un tour serait
        compté deux fois dans la ligne de client comme dans la liste héritée.

        Ce test n'exerce QUE ``ingest_otlp_json``. Son titre a longtemps
        promis « le registre fusionné » — le chemin Claude n'y était pour
        rien, et n'avait aucune dédup. Le pendant Claude est le test suivant.
        """
        registry = _registry()
        registry.ingest_otlp_json(_codex_payload(timestamp="1"))
        registry.ingest_otlp_json(_codex_payload(timestamp="1"))
        (row,) = _rows(registry).values()
        assert row["turns"] == 1

        # Contrôle positif : un vrai second tour, lui, est compté.
        registry.ingest_otlp_json(_codex_payload(timestamp="2"))
        (row,) = _rows(registry).values()
        assert row["turns"] == 2

    def test_claude_dedup_holds_on_the_claude_path(self) -> None:
        """Le rejeu d'un lot Claude ne doit ni recompter, ni recumuler.

        Le récepteur borné répond ``503`` avec ``Retry-After: 1`` dès que ses
        quatre requêtes en vol sont prises : un statut explicitement rejouable,
        que l'exportateur honore en repoussant le MÊME lot. Le coût, lui, est
        cumulatif — un tour à 0,05 $ compté trois fois affiche 0,15 $ et
        l'erreur ne se résorbe jamais tant que la ligne vit.
        """
        registry = _registry()
        replayed = _claude_session()
        for _ in range(3):
            registry.ingest_claude_otlp_json(replayed)

        (row,) = _rows(registry).values()
        assert row["turns"] == 1
        assert row["tokens"] == 1_200
        assert row["cost"] == pytest.approx(0.05)

        # Contrôle positif : un vrai second tour, aux horodatages neufs, compte
        # son tour et sa dépense. Sans lui, un registre qui n'ingère plus rien
        # passerait les trois assertions ci-dessus.
        registry.ingest_claude_otlp_json(
            _envelope([_claude_prompt(timestamp="3"), _claude_api_request(timestamp="4")])
        )
        (row,) = _rows(registry).values()
        assert row["turns"] == 2
        assert row["cost"] == pytest.approx(0.10)


TRANSPORT_A = "0f9d2c1b3a4e5f60718293a4b5c6d7e8"
TRANSPORT_B = "1a2b3c4d5e6f708192a3b4c5d6e7f809"


def _brain_rows(registry: ClientActivityRegistry) -> list[dict[str, object]]:
    """Les lignes portant une activité brain, quel que soit leur ``kind``.

    Filtrer sur ``kind != 'session'`` serait faux : une observation brain qui
    déclare une session produit elle aussi une ligne ``kind='session'``, sans
    conversation OTLP en face. Le critère honnête est « cette ligne porte-t-elle
    des appels brain ».
    """
    rows = registry.snapshot()["clients"]
    assert isinstance(rows, list)
    return [r for r in rows if r["brain_calls"] is not None]


class TestTransportRows:
    """Une connexion frappée par le serveur vaut une LIGNE, pas un seau.

    C'est le problème que ce champ existe pour résoudre : quatre moteurs Claude
    dans un même répertoire déclarent le même acteur et s'effondraient en une
    ligne unique. Ils ont en revanche quatre transports distincts.
    """

    def test_two_transports_same_actor_produce_two_rows(self) -> None:
        registry = _registry()
        registry.record_observations(
            (
                ClientObservation(
                    actor="brain-v42", session_id=None, calls=3, transport=TRANSPORT_A
                ),
                ClientObservation(
                    actor="brain-v42", session_id=None, calls=5, transport=TRANSPORT_B
                ),
            )
        )
        rows = _brain_rows(registry)
        assert len(rows) == 2, "deux connexions du même acteur doivent rester distinctes"
        assert {r["actor"] for r in rows} == {"brain-v42"}
        assert sorted(int(r["brain_calls"] or 0) for r in rows) == [3, 5]
        assert {r["kind"] for r in rows} == {"transport"}

    def test_same_transport_accumulates(self) -> None:
        registry = _registry()
        for _ in range(3):
            registry.record_observations(
                (
                    ClientObservation(
                        actor="brain-v42", session_id=None, calls=2, transport=TRANSPORT_A
                    ),
                )
            )
        (row,) = _brain_rows(registry)
        assert row["brain_calls"] == 6
        # Sans cette ligne le test passerait au vert avec un transport IGNORÉ :
        # les trois observations retomberaient dans le seau de l'acteur et
        # totaliseraient 6 tout pareil.
        assert row["kind"] == "transport"

    def test_transport_row_id_is_pseudonymous(self) -> None:
        """Aucun identifiant brut ne sort du registre — la propriété centrale."""
        registry = _registry()
        registry.record_observations(
            (ClientObservation(actor="brain-v42", session_id=None, calls=1, transport=TRANSPORT_A),)
        )
        (row,) = _brain_rows(registry)
        assert TRANSPORT_A not in json.dumps(row)
        assert str(row["id"]).startswith("transport-")

    def test_transport_and_session_keys_never_collide(self) -> None:
        """Le même octet-à-octet dans deux espaces ne doit pas fusionner.

        Les deux sels diffèrent : sans ça, une valeur qui serait à la fois une
        session et un transport écraserait l'autre ligne.
        """
        registry = _registry()
        shared = "1a2b3c4d5e6f708192a3b4c5d6e7f809"
        registry.record_observations(
            (
                ClientObservation(actor="a", session_id=FAKE_UUID, calls=1, transport=shared),
                ClientObservation(actor="b", session_id=None, calls=1, transport=shared),
            )
        )
        rows = registry.snapshot()["clients"]
        assert isinstance(rows, list)
        ids = [str(r["id"]) for r in rows]
        assert len(set(ids)) == len(ids), f"collision d'identifiants : {ids}"

    def test_session_wins_over_transport(self) -> None:
        """Une session déclarée JOINT ; un transport ne joint rien.

        Quand les deux sont présents, la clé de jointure doit l'emporter,
        sinon la ligne perdrait ses colonnes OTLP au profit d'un identifiant
        de connexion qui ne se joint à rien.
        """
        registry = _registry()
        registry.record_observations(
            (
                ClientObservation(
                    actor="codex", session_id=FAKE_UUID, calls=4, transport=TRANSPORT_A
                ),
            )
        )
        (row,) = _brain_rows(registry)
        assert row["kind"] == "session"
        assert not str(row["id"]).startswith("transport-")

    def test_no_transport_still_falls_back_to_the_actor_bucket(self) -> None:
        """Contrôle négatif : le mode sans état doit garder son comportement."""
        registry = _registry()
        registry.record_observations((ClientObservation(actor="codex", session_id=None, calls=7),))
        (row,) = _brain_rows(registry)
        assert row["kind"] == "unattributed"
        assert row["id"] == "unattributed:codex"
