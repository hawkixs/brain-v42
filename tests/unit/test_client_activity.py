"""The generalised registry — equivalence with the old one, then the merge.

The merge brings together two sources that do not overlap: a CLI's OTLP
telemetry (tokens, turns, cost) and the activity observed on the brain side (tool
calls). The join key is the HMAC pseudonym of the session UUID.

Measured on 2026-08-06 (``docs/upstream/2026-08-06-claude-otlp-session-join.md``):
no client today knows how to declare its session in an MCP header. The NOMINAL
case is therefore two disjoint rows — one OTLP-only, one "unattributed" — and not
a joined row. Both situations are pinned here: the join because the code must
carry it for the day a client can, the disjunction because it is what production
produces.
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
# On the brain side, the identifier names no agent: the MCP process does not know
# which CLI is calling it, and a prefix would invent one.
SESSION_PATTERN = re.compile(r"session-[0-9a-f]{32}")


class _Clock:
    """A monotonic clock driven by the test.

    An iterator of frozen values would break as soon as the implementation reads
    the clock one time more or less; here only the elapsed time matters.
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
    """A nominal Claude session: a prompt, then an API request."""
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
    """A Codex completion, the sole source of the legacy ``ctx_tokens``."""
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
    """A distinct canonical UUID per index, to populate the registry."""
    return f"00000000-0000-4000-8000-{index:012d}"


def _rows(registry: ClientActivityRegistry) -> dict[str, dict[str, object]]:
    """Index the rows by identifier, refusing any duplicate id.

    Without this guard, two rows sharing an id would overwrite each other in the
    dictionary and the test would count one row where the registry produces two.
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
        """The join works — but no client triggers it today.

        Measured on 2026-08-06: neither Claude Code nor Codex knows how to declare
        its session in an MCP header. This test keeps the mechanism alive and
        tested for the day one of the two can; it therefore builds an observation
        production does not yet produce. That is deliberate: do not "fix" it into
        an absence test, that one exists alongside.
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
        """The join must not work for only one of the two agents.

        ``X-Brain-Agent`` carries a project name, not a CLI brand: the brain does
        not know WHO is calling it. Hashing its observation with an agent's salt
        invents one — measured, Claude's for everybody. A Codex that declared its
        session then came out as a ghost ``claude-…`` row next to its real
        ``codex-…`` row, never joining.

        This test ALSO pins the equality of decision ``4890a475``: the OTLP row is
        keyed by ``conversation.id``, the brain observation declares the same UUID
        as its SESSION — the join only holds because, for Codex, the two
        identifiers are strictly equal (863ff2ca).
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
        """An observation alone does not know which CLI it came from.

        It must therefore come out under an identifier that names no agent.
        Announcing itself as ``claude-…`` would be an assertion nothing measures:
        here the actor is ``red-lab``, which is no more Claude than Codex.
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
        """``input_tokens`` alone would lie by a factor of a thousand.

        Reading of 2026-08-06: ``input_tokens=10`` for a real context of 18590
        tokens, the rest living in ``cache_read_tokens`` and
        ``cache_creation_tokens``. The payload's four counters are chosen
        distinct: 1200 is reachable neither by ``input_tokens`` alone (10), nor by
        the cache alone (1190), nor by the sum of everything, output included
        (1540).
        """
        registry = _registry()
        registry.ingest_claude_otlp_json(_claude_session())
        (row,) = _rows(registry).values()
        assert row["tokens"] == 10 + 1_000 + 190

    def test_claude_today_yields_two_rows_not_one(self) -> None:
        """The measured NOMINAL case: Claude does not declare its session.

        Its header arrives as an unexpanded template, which ``normalize_session``
        rejects. We therefore get one OTLP-only row and one residual row, distinct
        — exactly Codex's situation. This is the expected behaviour in production,
        not an accidental degradation.
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
        # The two halves of the same session, side by side and not joined. Each
        # absence is paired with its presence in the other row: without that pair,
        # an "is None" would go green on a dead registry.
        assert otlp["tokens"] == 1_200
        assert otlp["brain_calls"] is None
        assert residual["brain_calls"] == 4
        assert residual["tokens"] is None

    def test_the_row_identifier_depends_on_the_process_secret(self) -> None:
        """The pseudonym is an HMAC, not a bare hash.

        Two processes see two identifiers for the same session, and nobody gets
        back to the UUID by guessing a fingerprint.
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
        """Neither the session UUID nor the ``conversation.id`` leaves the registry."""
        registry = _registry()
        registry.ingest_claude_otlp_json(_claude_session())
        registry.ingest_otlp_json(_codex_payload())
        registry.record_observations(
            (ClientObservation(actor="brain-v42", session_id=FAKE_UUID, calls=1),)
        )

        serialized = json.dumps(registry.snapshot())
        # Positive control: both rows are indeed there and the pseudonym is indeed
        # in the serialised payload. The UUID's absence therefore does not come
        # from an empty snapshot.
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
        """``agent`` is ``None``, and the identifier says so too.

        This row used to pin ``claude-…`` for an observation from ``red-lab``,
        that is, an agent asserted where the neighbouring column admits knowing
        none. The neutral identifier is the only one consistent with both.
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
        """Only ``actor``, ``brain_calls`` and ``last_seen_s`` have a source.

        The rest is ``null`` and never ``0``: a cosmetic zero would be
        indistinguishable from a real measured zero.
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
        """A prompt alone measures neither context, nor cost, nor model."""
        registry = _registry()
        registry.ingest_claude_otlp_json(_envelope([_claude_prompt()]))

        (row,) = _rows(registry).values()
        # Positive control: the row is indeed fed by the OTLP source.
        assert row["turns"] == 1
        assert row["tokens"] is None
        assert row["cost"] is None
        assert row["model"] is None

    def test_the_model_survives_an_event_that_does_not_carry_one(self) -> None:
        """A real session alternates API request and prompt, endlessly.

        Every other payload in this file orders the prompt BEFORE the API request
        — a session's opening order, played once. The guard that carries forward
        the already-measured model is therefore never exercised there.

        Yet as soon as the user types the next line, ``user_prompt`` arrives: it
        carries no ``model`` attribute, ``_model_value`` returns ``unknown``, and
        the row translates ``unknown`` into ``None``. Without the guard, the
        dashboard's model column falls back to an em dash at every keystroke and
        only lights up again at the next answer, while the session is very much
        alive. This is the "null = not measured" confusion the doctrine forbids:
        here the model IS measured, it was measured on the previous turn.
        """
        registry = _registry()
        registry.ingest_claude_otlp_json(_envelope([_claude_api_request(timestamp="2")]))
        # Positive control: the model is indeed measured before the next prompt,
        # otherwise its survival would prove nothing.
        (row,) = _rows(registry).values()
        assert row["model"] == "claude-opus-5"

        registry.ingest_claude_otlp_json(_envelope([_claude_prompt(timestamp="3")]))

        (row,) = _rows(registry).values()
        assert row["model"] == "claude-opus-5"
        # Second positive control: the prompt was indeed applied. Without it, an
        # ignored batch — deduplicated, discarded — would leave the model intact
        # for an entirely different reason than the one being pinned.
        assert row["turns"] == 1

    def test_cost_accumulates_while_tokens_keep_the_latest_context(self) -> None:
        """Two neighbouring columns, two natures the row does not treat alike.

        Cost is an expense: it accumulates over the whole session. Tokens are a
        context size: the last API request is authoritative, an accumulation would
        inflate the row at every turn.

        A session with a single API request cannot distinguish the two — which is
        the case for every other payload in this file. Two are therefore needed,
        with disjoint counters: 2400 is neither the first context (1200), nor the
        sum of the two (3600).
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
        """Codex comes out as N conversations PLUS one unattributed row.

        This is not a duplicate: its MCP configuration exposes no conversation
        identifier, so its tool calls are attributable to none of its
        conversations. The dashboard shows that hole instead of filling it with an
        invented correlation.
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
    """The actor is declared by the client: it serves as a label, not as text.

    ``normalize_agent`` only does strip / basename / truncation — no bound on the
    characters. A hostile ``X-Brain-Agent`` therefore crossed the registry verbatim
    all the way to ``/api/cockpit``'s JSON, both in the ``actor`` column and in the
    unattributed row's ``id``. The legacy row writes ``topic: "[redacted]"`` and
    the OTLP decoder collapses a non-conforming ``model`` onto ``unknown`` — for
    precisely this reason; the brain half had no equivalent.

    What a client does with the rendering is not decided here: we bound at the
    source.
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
        # The measurement survives the label's rejection: we refuse the label, not
        # the observation. Without this control, discarding the observation would
        # go green.
        assert row["brain_calls"] == 3
        serialized = json.dumps(registry.snapshot())
        assert self.HOSTILE not in serialized
        assert "<" not in serialized
        assert "onerror" not in serialized

    def test_a_hostile_actor_is_bounded_on_the_joined_row_too(self) -> None:
        """The joined row's ``actor`` column is a second path.

        It reads the same stored actor but from another branch of the projection:
        a fix applied to the residual row alone would let it leak.
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
        # Positive control: the join did happen, otherwise the ``actor`` column
        # would be ``None`` and the assertion above would prove nothing about the
        # sanitisation.
        assert row["brain_calls"] == 4
        assert row["tokens"] == 1_200

    @pytest.mark.parametrize(
        "actor",
        ["brain-v42", "red-lab", "codex", "dream-codex-synth", "_unexpanded", "unknown", "a" * 64],
    )
    def test_a_legitimate_actor_reaches_the_panel_intact(self, actor: str) -> None:
        """An indispensable positive control.

        Without it, a sanitisation collapsing EVERYTHING onto the sentinel would go
        green while erasing the row's only piece of information. The first six
        values are those ``normalize_agent`` really produces in production,
        sentinels included; the seventh is the truncation at ``MAX_ACTOR_LENGTH``,
        the maximum legitimate length.
        """
        registry = _registry()
        registry.record_observations((ClientObservation(actor=actor, session_id=None, calls=1),))

        rows = _rows(registry)
        assert rows[f"unattributed:{actor}"]["actor"] == actor

    def test_two_hostile_labels_collapse_into_a_single_bucket(self) -> None:
        """A single bucket, not one pseudonym per literal.

        This is the choice already made by ``normalize_agent`` for ``_unexpanded``
        and by the OTLP decoder for ``unknown``. It also bounds the spraying: a
        client inventing a thousand hostile labels would occupy a thousand rows of
        the cap of 64, and would evict the legitimate actors.
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
        """The contract is additive: the shipped dashboard still reads these three keys."""
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
        """A brain-side observation is not an OTLP conversation.

        Making it appear in ``activeConvs`` would inflate the shipped dashboard's
        conversation counter with rows carrying no tokens.
        """
        registry = _registry()
        registry.record_observations((ClientObservation(actor="codex", session_id=None, calls=7),))

        snapshot = registry.snapshot()
        assert snapshot["activeConvs"] == []
        assert snapshot["active_convs"] == 0
        assert snapshot["ctx_tokens"] == 0
        # Positive control: the observation does exist, elsewhere.
        assert _rows(registry)["unattributed:codex"]["brain_calls"] == 7

    def test_legacy_list_never_mislabels_a_claude_row_as_codex(self) -> None:
        """The shipped dashboard displays ``activeConvs`` under the "Codex" title.

        A Claude session presenting itself there as ``codex`` would lie to it.
        """
        codex_registry = _registry()
        codex_registry.ingest_otlp_json(_codex_payload())
        legacy_codex = codex_registry.snapshot()["activeConvs"]
        assert isinstance(legacy_codex, list)
        # Positive control: the label exists and is "codex" for Codex, without
        # which the absence assertion below would be meaningless.
        assert [entry["agent"] for entry in legacy_codex] == ["codex"]

        claude_registry = _registry()
        claude_registry.ingest_claude_otlp_json(_claude_session())
        legacy_claude = claude_registry.snapshot()["activeConvs"]
        assert isinstance(legacy_claude, list)
        assert "codex" not in [entry["agent"] for entry in legacy_claude]

    def test_a_claude_session_never_enters_the_legacy_codex_list(self) -> None:
        """``activeConvs`` counts Codex conversations, and nothing else.

        Checking the label is not enough: a Claude session admitted into the list
        would honestly announce itself as ``claude``. The lie would be in the two
        counters — ``active_convs`` and ``ctx_tokens`` — that the shipped "Codex
        activity" dashboard displays today, before its switch-over.
        """
        registry = _registry()
        registry.ingest_claude_otlp_json(_claude_session())

        snapshot = registry.snapshot()
        assert snapshot["activeConvs"] == []
        assert snapshot["active_convs"] == 0
        assert snapshot["ctx_tokens"] == 0
        # Positive control: the session is indeed measured, in the registry's new
        # half. Without it, the three absences above would go green on a registry
        # that ingested nothing.
        (row,) = _rows(registry).values()
        assert row["agent"] == "claude"
        assert row["tokens"] == 1_200


class TestBounds:
    def test_rows_expire_after_the_ttl(self) -> None:
        """The TTL holds on both halves of the merged registry."""
        clock = _Clock()
        registry = _registry(clock)
        registry.ingest_claude_otlp_json(_claude_session())
        registry.record_observations((ClientObservation(actor="codex", session_id=None, calls=1),))
        # Positive control: both rows are present before the deadline.
        assert len(_rows(registry)) == 2

        clock.advance(ACTIVITY_TTL_SECONDS + 1.0)
        assert registry.snapshot()["clients"] == []

    def test_last_seen_is_measured_on_both_halves_and_not_a_constant_zero(self) -> None:
        """``last_seen_s`` is a measurement, not a decorative zero.

        Every other row in this file is read at the clock's instant zero, where a
        counter frozen at 0 is indistinguishable from a real null elapsed time. The
        clock must therefore be advanced without crossing the TTL, and on both
        halves: the OTLP row and the unattributed row read two distinct timestamps,
        in two distinct branches of the code.
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
        """An actor invented per call must not make the registry grow."""
        registry = _registry()
        registry.record_observations(
            tuple(
                ClientObservation(actor=f"projet-{index}", session_id=None, calls=1)
                for index in range(MAX_ACTIVE_CONVERSATIONS)
            )
        )
        # Positive control: under the cap, nothing is discarded.
        assert len(_rows(registry)) == MAX_ACTIVE_CONVERSATIONS

        registry.record_observations(
            tuple(
                ClientObservation(actor=f"debordement-{index}", session_id=None, calls=1)
                for index in range(5)
            )
        )
        assert len(_rows(registry)) == MAX_ACTIVE_CONVERSATIONS

    def test_the_brain_side_cap_keeps_the_newest_actor_not_the_first_arrived(self) -> None:
        """Counting the survivors says nothing about the DIRECTION of the eviction.

        The neighbouring test inserts its 69 actors without ever advancing the
        clock: the 69 ``last_seen`` are the same instant, ``_trim_brain``'s sort
        has nothing to sort, and ``[:64]`` falls back on the insertion order
        whatever the sort direction. An inverted cap stays green there.

        Yet the cap exists precisely because an actor is declared by the client,
        hence of unbounded cardinality. Once 64 distinct actors have been seen in
        the 600 s window, a "first 64 to arrive win" policy discards the
        ``brain_calls`` of every new session — including that of the operator
        currently at work, who then never appears in the dashboard. The "live"
        dashboard becomes a "first 64 to arrive" dashboard.

        The clock must therefore be advanced so that the sort has something to sort.
        """
        clock = _Clock()
        registry = _registry(clock)
        registry.record_observations(
            tuple(
                ClientObservation(actor=f"ancien-{index}", session_id=None, calls=1)
                for index in range(MAX_ACTIVE_CONVERSATIONS)
            )
        )
        # Positive control: the registry is already full of old ones, without
        # which the new actor's arrival would contest no slot.
        assert len(_rows(registry)) == MAX_ACTIVE_CONVERSATIONS

        clock.advance(1.0)
        registry.record_observations(
            (ClientObservation(actor="operateur", session_id=None, calls=9),)
        )

        rows = _rows(registry)
        assert len(rows) == MAX_ACTIVE_CONVERSATIONS
        assert rows["unattributed:operateur"]["brain_calls"] == 9

    def test_one_batch_of_new_actors_never_erases_a_measured_join(self) -> None:
        """ONE batch's bound equalled the brain half's TOTAL capacity.

        ``MAX_OBSERVATIONS`` is 64, ``MAX_ACTIVE_CONVERSATIONS`` too: a single POST
        could therefore renew the table entirely. Measured, a Claude session joined
        on both sides — ``actor='brain_v42'``, ``brain_calls=7`` — became
        ``actor=None``, ``brain_calls=None`` again after a single POST of 64 fresh
        actors (2151 bytes). These are not "evicted residuals": it is a MEASURED
        JOIN erased, replaced by nulls the dashboard renders as "nothing measured".
        It loses exactly what it exists for.

        The arbitration does not touch the eviction DIRECTION pinned just above
        (e5cda111): between residuals, the most recent always win. This test does
        not oppose two residuals, it opposes a JOINED row to a batch of fresh
        residuals — and the join comes first.
        """
        clock = _Clock()
        registry = _registry(clock)
        registry.ingest_claude_otlp_json(_claude_session())
        registry.record_observations(
            (ClientObservation(actor="brain_v42", session_id=FAKE_UUID, calls=7),)
        )
        # Positive control: the join does carry a measurement BEFORE the batch,
        # otherwise "it survives" would go green on a registry that never joined
        # anything.
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
        # Positive control: the cap ALWAYS bounds. Without it, the join would
        # survive too, and the two assertions above would go green on a registry
        # that no longer caps anything.
        assert len(rows) == MAX_ACTIVE_CONVERSATIONS

    def test_a_dead_conversation_protects_no_residual_row(self) -> None:
        """Priority goes to a LIVE join, not to the memory of a join.

        The observations path does not purge the OTLP half. Without reapplying the
        TTL there, a long-expired conversation would still rank "its" brain row
        ahead of every residual — whereas at the next snapshot that conversation is
        purged and the row falls back to an orphan row, with no agent, no turns and
        no tokens — a residual like any other, but older. It would then win against
        fresh residuals: exactly the eviction direction e5cda111 forbids, through
        the back door.
        """
        clock = _Clock()
        registry = _registry(clock)
        registry.ingest_claude_otlp_json(_claude_session())
        registry.record_observations(
            (ClientObservation(actor="brain_v42", session_id=FAKE_UUID, calls=7),)
        )
        # Refresh the brain half WITHOUT refreshing the OTLP half: the brain row
        # will stay alive when its conversation has expired.
        clock.advance(500.0)
        registry.record_observations(
            (ClientObservation(actor="brain_v42", session_id=FAKE_UUID, calls=1),)
        )
        # Positive control: at this instant the join is still alive and visible.
        before = _rows(registry)
        (identifier,) = before
        assert before[identifier]["brain_calls"] == 8

        # The OTLP conversation passes the TTL; the brain row has not reached it
        # and survives ``_prune_brain``.
        clock.advance(200.0)
        registry.record_observations(
            tuple(
                ClientObservation(actor=f"neuf-{index}", session_id=None, calls=1)
                for index in range(MAX_ACTIVE_CONVERSATIONS)
            )
        )

        rows = _rows(registry)
        # The orphan row's identifier is the raw session key, which the test cannot
        # recompute without the secret: it is the actor that names it. Targeting it
        # through ``unattributed:brain_v42`` would never bite.
        assert all(row["actor"] != "brain_v42" for row in rows.values())
        assert set(rows) == {
            f"unattributed:neuf-{index}" for index in range(MAX_ACTIVE_CONVERSATIONS)
        }

    def test_claude_traffic_never_evicts_the_codex_counters(self) -> None:
        """The legacy contract is ADDITIVE: Claude traffic does not touch it.

        The two OTLP halves shared a single cap of 64. Sixty-four Claude sessions
        in the TTL window therefore evicted the Codex conversation, and the three
        keys the shipped red-monitor dashboard consumes today fell back to
        ``ctx_tokens=0``, ``active_convs=0``, ``activeConvs=[]``. That is the worst
        way to break the contract: writing a zero where a real measured value was,
        indistinguishable from an honest zero.
        """
        registry = _registry()
        registry.ingest_otlp_json(_codex_payload())
        registry.ingest_otlp_json(_codex_completion())
        before = registry.snapshot()
        # Positive control: the legacy counters do carry a measurement before
        # Claude arrives, otherwise their survival would prove nothing.
        assert before["active_convs"] == 1
        assert before["ctx_tokens"] == 12_345

        for index in range(MAX_ACTIVE_CONVERSATIONS):
            registry.ingest_claude_otlp_json(_claude_session(session_id=_nth_uuid(index)))

        after = registry.snapshot()
        assert after["active_convs"] == 1
        assert after["ctx_tokens"] == 12_345
        assert after["activeConvs"] == before["activeConvs"]

    def test_each_agent_is_capped_on_its_own(self) -> None:
        """Per-agent caps are still caps.

        The set of agents is CLOSED — the label comes from the receiver that
        decoded the batch, never from the client — so the registry stays bounded by
        the cap multiplied by the number of receivers, here two. An actor, by
        contrast, is declared by the client: that is why the brain half keeps a
        global cap.
        """
        registry = _registry()
        for index in range(MAX_ACTIVE_CONVERSATIONS + 6):
            registry.ingest_claude_otlp_json(_claude_session(session_id=_nth_uuid(index)))

        assert len(_rows(registry)) == MAX_ACTIVE_CONVERSATIONS

    def test_codex_dedup_still_holds_on_the_codex_path(self) -> None:
        """Fingerprint dedup survives the move, on the Codex path.

        An OTLP exporter replays its batches; without a fingerprint, a turn would
        be counted twice both in the client row and in the legacy list.

        This test exercises ONLY ``ingest_otlp_json``. Its title long promised "the
        merged registry" — the Claude path had nothing to do with it, and had no
        dedup at all. The Claude counterpart is the next test.
        """
        registry = _registry()
        registry.ingest_otlp_json(_codex_payload(timestamp="1"))
        registry.ingest_otlp_json(_codex_payload(timestamp="1"))
        (row,) = _rows(registry).values()
        assert row["turns"] == 1

        # Positive control: a real second turn, by contrast, is counted.
        registry.ingest_otlp_json(_codex_payload(timestamp="2"))
        (row,) = _rows(registry).values()
        assert row["turns"] == 2

    def test_claude_dedup_holds_on_the_claude_path(self) -> None:
        """Replaying a Claude batch must neither recount nor re-accumulate.

        The bounded receiver answers ``503`` with ``Retry-After: 1`` as soon as its
        four in-flight requests are taken: an explicitly replayable status, which
        the exporter honours by pushing the SAME batch again. Cost, for its part,
        is cumulative — a turn at $0.05 counted three times displays $0.15 and the
        error never resolves as long as the row lives.
        """
        registry = _registry()
        replayed = _claude_session()
        for _ in range(3):
            registry.ingest_claude_otlp_json(replayed)

        (row,) = _rows(registry).values()
        assert row["turns"] == 1
        assert row["tokens"] == 1_200
        assert row["cost"] == pytest.approx(0.05)

        # Positive control: a real second turn, with fresh timestamps, counts its
        # turn and its spending. Without it, a registry that ingests nothing would
        # pass the three assertions above.
        registry.ingest_claude_otlp_json(
            _envelope([_claude_prompt(timestamp="3"), _claude_api_request(timestamp="4")])
        )
        (row,) = _rows(registry).values()
        assert row["turns"] == 2
        assert row["cost"] == pytest.approx(0.10)


TRANSPORT_A = "0f9d2c1b3a4e5f60718293a4b5c6d7e8"
TRANSPORT_B = "1a2b3c4d5e6f708192a3b4c5d6e7f809"


def _brain_rows(registry: ClientActivityRegistry) -> list[dict[str, object]]:
    """The rows carrying brain activity, whatever their ``kind``.

    Filtering on ``kind != 'session'`` would be wrong: a brain observation that
    declares a session also produces a ``kind='session'`` row, with no OTLP
    conversation opposite it. The honest criterion is "does this row carry brain
    calls".
    """
    rows = registry.snapshot()["clients"]
    assert isinstance(rows, list)
    return [r for r in rows if r["brain_calls"] is not None]


class TestTransportRows:
    """A server-minted connection is worth a ROW, not a bucket.

    This is the problem this field exists to solve: four Claude engines in the same
    directory declare the same actor and used to collapse into a single row. They
    do, however, have four distinct transports.
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
        # Without this line the test would go green with the transport IGNORED:
        # the three observations would fall back into the actor's bucket and would
        # total 6 all the same.
        assert row["kind"] == "transport"

    def test_transport_row_id_is_pseudonymous(self) -> None:
        """No raw identifier leaves the registry — the central property."""
        registry = _registry()
        registry.record_observations(
            (ClientObservation(actor="brain-v42", session_id=None, calls=1, transport=TRANSPORT_A),)
        )
        (row,) = _brain_rows(registry)
        assert TRANSPORT_A not in json.dumps(row)
        assert str(row["id"]).startswith("transport-")

    def test_transport_and_session_keys_never_collide(self) -> None:
        """The same bytes in two spaces must not merge.

        The two salts differ: without that, a value that was both a session and a
        transport would overwrite the other row.
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
        """A declared session JOINS; a transport joins nothing.

        When both are present, the join key must win, otherwise the row would lose
        its OTLP columns in favour of a connection identifier that joins nothing.
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
        """Negative control: stateless mode must keep its behaviour."""
        registry = _registry()
        registry.record_observations((ClientObservation(actor="codex", session_id=None, calls=7),))
        (row,) = _brain_rows(registry)
        assert row["kind"] == "unattributed"
        assert row["id"] == "unattributed:codex"
