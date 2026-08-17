"""Bounded, in-memory registry of live client activity.

Generalized from the Codex-only conversation registry: decoding stays in the
per-source modules (``codex_telemetry`` and ``claude_telemetry`` for OTLP,
``client_observation`` for the brain-side wire format), while this module owns
the registry state — TTL, dedup fingerprints, HMAC pseudonymization, and the
bounded cockpit snapshot.

Two sources feed it and they do not overlap. A CLI's OTLP tells us about
prompts, context size and cost; the brain's own middleware tells us how many
tool calls an actor made. They are joined on the HMAC of the session UUID —
never on the UUID itself, which is hashed on arrival and never leaves. That
join key is agent-neutral, because only one of the two sides can observe an
agent at all (see ``_session_key``).

Measured on 2026-08-06 (``docs/upstream/2026-08-06-claude-otlp-session-join.md``):
no client can declare its session in an MCP header today, so the join has no
producer yet. The nominal outcome is therefore two disjoint rows per Claude
session — one OTLP-only, one ``unattributed`` — exactly like Codex. The join is
carried anyway, for the day a client can declare its session.

``None`` — never ``0`` — for every column without a source: a cosmetic zero is
indistinguishable from a measured one.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

from brain_v42.metrics.claude_telemetry import ClaudeRecord, decode_claude_logs
from brain_v42.metrics.client_observation import ClientObservation
from brain_v42.metrics.codex_telemetry import _COMPLETION_EVENTS, _decode, _ProjectedRecord

MAX_ACTIVE_CONVERSATIONS = 64
ACTIVITY_TTL_SECONDS = 600.0
MAX_FINGERPRINTS = 1_024
FINGERPRINT_TTL_SECONDS = 600.0

_ACTOR_KEY_PREFIX = "actor:"
_SESSION_KEY_PREFIX = "session-"
_TRANSPORT_KEY_PREFIX = "transport-"

# Un acteur est déclaré par le client et sert d'ÉTIQUETTE : identifiant de
# ligne et libellé de panneau, jamais champ de texte. ``normalize_agent`` ne
# borne que la longueur — même jeu de caractères que le ``model`` de l'OTLP,
# qui est déjà filtré par ``_MODEL_PATTERN`` côté décodeur. ASCII strict et
# volontaire : l'étiquette est une identité, et laisser passer l'unicode
# ouvrirait l'usurpation par homoglyphe ou par inversion bidirectionnelle.
_ACTOR_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}\Z")
_REJECTED_ACTOR = "_rejected"


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _project_actor(actor: str) -> str:
    """Réduire un acteur déclaré à une étiquette projetable, ou à la sentinelle.

    Tout ce qui sort du jeu autorisé s'effondre sur UN seul seau, plutôt que
    de produire un pseudonyme par littéral : c'est déjà le choix de
    ``normalize_agent`` pour ``_unexpanded`` et du décodeur OTLP pour
    ``unknown``. Un seau unique borne aussi l'arrosage — mille étiquettes
    hostiles occuperaient sinon mille des 64 places du registre et évinceraient
    les acteurs légitimes.

    La sentinelle est un point fixe du filtre, donc l'assainissement est
    idempotent. Un projet réellement nommé ``_rejected`` s'y confondrait, au
    même titre qu'un projet nommé ``unknown`` se confond déjà avec l'absence
    d'acteur : collision assumée, et sans conséquence sur une mesure.
    """
    return actor if _ACTOR_PATTERN.fullmatch(actor) else _REJECTED_ACTOR


@dataclass(frozen=True, slots=True)
class _Conversation:
    pseudonym: str
    session_key: str
    started: str
    turns: int
    tokens: int | None
    token_timestamp: int | None
    model: str
    last_seen: float
    receipt_order: int
    agent: str
    cost: float | None


@dataclass(frozen=True, slots=True)
class _BrainActivity:
    """What the brain itself observed of a caller: call volume and liveness."""

    actor: str
    calls: int
    last_seen: float


class ClientActivityRegistry:
    """Thread-safe bounded registry of pseudonymous client activity."""

    def __init__(
        self,
        *,
        secret: bytes | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _local_now,
    ) -> None:
        process_secret = secrets.token_bytes(32) if secret is None else secret
        if not isinstance(process_secret, bytes) or len(process_secret) != 32:
            raise ValueError("secret must contain exactly 32 bytes")
        self._secret = process_secret
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._conversations: dict[str, _Conversation] = {}
        self._brain: dict[str, _BrainActivity] = {}
        self._fingerprints: dict[bytes, float] = {}
        self._receipt_order = 0

    def _pseudonym(self, identifier: str, agent: str = "codex") -> str:
        """Hash an identifier into an agent-scoped pseudonym.

        The salt carries the agent so two agents cannot collide on a shared
        UUID. ``codex`` keeps its historical salt byte for byte: the projection
        it produces is pinned by the existing receiver tests.
        """
        digest = hmac.new(
            self._secret,
            f"{agent}-conversation-id\0".encode("ascii") + identifier.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return f"{agent}-{digest[:32]}"

    def _session_key(self, identifier: str) -> str:
        """Hash a session UUID into the AGENT-NEUTRAL join key.

        The brain side never learns which CLI is calling it: ``X-Brain-Agent``
        carries a project name, not a kind of agent. Salting this key with an
        agent would make the brain claim one it cannot observe — and it did,
        claiming ``claude`` for everyone. A Codex declaring its session then
        produced a ghost ``claude-…`` row beside its real ``codex-…`` one, and
        never joined; the join only ever worked for one of the two agents.

        Distinct salt from ``_pseudonym``, so the neutral key of a session can
        never be confused with the agent-scoped identifier of a row. The row
        identifiers stay agent-scoped: the OTLP side does know its agent.
        """
        digest = hmac.new(
            self._secret,
            b"brain-session-id\0" + identifier.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return f"{_SESSION_KEY_PREFIX}{digest[:32]}"

    def _transport_key(self, identifier: str) -> str:
        """Hash a server-minted connection id into a per-connection key.

        Third salt, and it earns its place. ``Mcp-Session-Id`` is minted by
        THIS server, so it is not falsifiable — but it has no counterpart in any
        OTLP stream, so it joins nothing. Routing it through ``_session_key``
        would mint a key that promises a join it can never make; routing it
        through ``_pseudonym`` would let a connection id collide with the
        agent-scoped identifier of an OTLP row.

        What it does buy is the case the actor label cannot express: four
        engines running in one directory declare one actor and collapsed into a
        single row. They hold four distinct connections.
        """
        digest = hmac.new(
            self._secret,
            b"brain-transport-id\0" + identifier.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return f"{_TRANSPORT_KEY_PREFIX}{digest[:32]}"

    def _fingerprint(self, record: _ProjectedRecord, pseudonym: str) -> bytes | None:
        if record.timestamp is None:
            return None
        changes_counter = record.event_name == "codex.user_prompt" or (
            record.event_name in _COMPLETION_EVENTS and record.input_tokens is not None
        )
        if not changes_counter:
            return None
        fields = (
            pseudonym,
            record.event_name,
            record.event_kind or "",
            str(record.timestamp),
            "" if record.input_tokens is None else str(record.input_tokens),
        )
        message = b"codex-event-fingerprint\0" + b"\0".join(
            field.encode("ascii") for field in fields
        )
        return hmac.new(self._secret, message, hashlib.sha256).digest()

    def _claude_fingerprint(self, record: ClaudeRecord, pseudonym: str) -> bytes | None:
        """Empreinte d'un enregistrement Claude qui déplace un compteur.

        Même discipline que le côté Codex, domaine de hachage distinct. Le
        récepteur borné répond ``503`` avec ``Retry-After: 1`` dès que ses
        quatre requêtes en vol sont prises — un statut explicitement rejouable,
        que l'exportateur honore en repoussant le même lot. Le coût étant
        cumulatif, un rejeu non dédupliqué gonfle la ligne pour toute sa durée
        de vie : l'erreur ne se résorbe jamais.

        Sans horodatage, pas d'empreinte : rien ne distinguerait alors un rejeu
        d'un événement neuf, et déduplique à tort perdrait une vraie mesure.
        Un enregistrement qui ne déplace aucun compteur n'en a pas besoin — il
        ne fait que rafraîchir ``last_seen`` et le modèle, deux idempotents.
        """
        if record.timestamp is None:
            return None
        counters = (
            record.input_tokens,
            record.cache_read_tokens,
            record.cache_creation_tokens,
            record.cost_usd,
        )
        changes_counter = record.event_name == "user_prompt" or any(
            value is not None for value in counters
        )
        if not changes_counter:
            return None
        fields = (
            pseudonym,
            record.event_name,
            str(record.timestamp),
            *("" if value is None else str(value) for value in counters),
        )
        message = b"claude-event-fingerprint\0" + b"\0".join(
            field.encode("ascii") for field in fields
        )
        return hmac.new(self._secret, message, hashlib.sha256).digest()

    @staticmethod
    def _prune_conversations(
        conversations: dict[str, _Conversation], now: float
    ) -> dict[str, _Conversation]:
        return {
            key: value
            for key, value in conversations.items()
            if now - value.last_seen < ACTIVITY_TTL_SECONDS
        }

    @staticmethod
    def _prune_brain(brain: dict[str, _BrainActivity], now: float) -> dict[str, _BrainActivity]:
        return {
            key: value
            for key, value in brain.items()
            if now - value.last_seen < ACTIVITY_TTL_SECONDS
        }

    @staticmethod
    def _prune_fingerprints(fingerprints: dict[bytes, float], now: float) -> dict[bytes, float]:
        fresh = {
            key: received_at
            for key, received_at in fingerprints.items()
            if now - received_at < FINGERPRINT_TTL_SECONDS
        }
        if len(fresh) <= MAX_FINGERPRINTS:
            return fresh
        insertion_ordered = list(fresh.items())
        return dict(insertion_ordered[-MAX_FINGERPRINTS:])

    @staticmethod
    def _trim_conversations(
        conversations: dict[str, _Conversation],
    ) -> dict[str, _Conversation]:
        """Cap each agent on its own budget, never across agents.

        A shared cap made Claude traffic evict Codex conversations: 64 Claude
        sessions inside one TTL window dropped ``ctx_tokens`` from a measured
        12345 to ``0`` and ``activeConvs`` to ``[]`` — a cosmetic zero written
        over a real measurement, in the very keys the shipped red-monitor panel
        reads. The legacy contract is additive; Claude must not be able to
        reach it at all.

        The registry stays bounded because the set of agents is CLOSED: the
        label comes from the receiver that decoded the batch, never from client
        input, so the ceiling is the cap times the number of receivers. That is
        exactly what does NOT hold for the brain half, whose actors are
        client-declared — hence its single global cap.
        """
        if len(conversations) <= MAX_ACTIVE_CONVERSATIONS:
            return conversations
        per_agent: dict[str, list[tuple[str, _Conversation]]] = {}
        for key, value in conversations.items():
            per_agent.setdefault(value.agent, []).append((key, value))

        kept: dict[str, _Conversation] = {}
        for entries in per_agent.values():
            if len(entries) > MAX_ACTIVE_CONVERSATIONS:
                entries = sorted(
                    entries,
                    key=lambda item: (item[1].last_seen, item[1].receipt_order),
                    reverse=True,
                )[:MAX_ACTIVE_CONVERSATIONS]
            kept.update(entries)
        return kept

    @staticmethod
    def _trim_brain(
        brain: dict[str, _BrainActivity], live_sessions: frozenset[str]
    ) -> dict[str, _BrainActivity]:
        """Plafonner la moitié brain, les lignes JOINTES d'abord.

        Un acteur est déclaré par le client, donc de cardinalité non bornée :
        cette moitié garde un plafond global, là où la moitié OTLP se répartit
        en budgets par agent (voir ``_trim_conversations``).

        Mais la borne d'UN lot — ``MAX_OBSERVATIONS``, 64 — valait la capacité
        TOTALE de ce plafond. Un seul POST de 2151 octets renouvelait donc la
        table entière, et une session jointe des deux côtés (``brain_v42``,
        7 appels) redevenait ``actor=None``, ``brain_calls=None`` : pas un
        résidu évincé, une MESURE effacée et remplacée par des null que le
        panneau rend comme « rien de mesuré ».

        L'arbitrage retenu ordonne avant de couper, plutôt que de baisser la
        borne de lot : ranger d'abord les entrées jointes à une conversation
        OTLP vivante, puis les plus récentes. Il ne coûte rien au cas NOMINAL —
        aucun client ne déclare sa session aujourd'hui, donc aucune entrée
        n'est jointe, le rang s'annule et l'ordre reste « les plus récents
        gagnent » (e5cda111). Il ne mord que là où le défaut mordait.

        Ses contreparties, assumées : entre résidus l'éviction reste
        destructrice — un lot de 64 acteurs neufs remplace bien 63 résidus plus
        anciens, chacun porteur de son propre ``brain_calls``. Et une jointure
        ne survit qu'à des NON-jointes : 65 sessions réellement jointes se
        disputent toujours 64 places, la plus ancienne partant la première. Le
        rang protège la ligne que le panneau existe pour montrer, pas toute
        mesure.

        Le total reste borné par ``MAX_ACTIVE_CONVERSATIONS`` : un rang ne
        crée aucun budget supplémentaire, il ne fait que choisir les survivants.
        """
        if len(brain) <= MAX_ACTIVE_CONVERSATIONS:
            return brain
        ranked = sorted(
            brain.items(),
            key=lambda item: (item[0] in live_sessions, item[1].last_seen),
            reverse=True,
        )
        return dict(ranked[:MAX_ACTIVE_CONVERSATIONS])

    def ingest_otlp_json(self, payload: bytes) -> None:
        """Validate a complete Codex batch, then atomically apply its projection."""
        records = _decode(payload)

        with self._lock:
            now = self._clock()
            started = self._wall_clock().strftime("%H:%M")
            conversations = self._prune_conversations(dict(self._conversations), now)
            fingerprints = self._prune_fingerprints(dict(self._fingerprints), now)
            receipt_order = self._receipt_order

            for record in records:
                pseudonym = self._pseudonym(record.conversation_id, agent="codex")
                fingerprint = self._fingerprint(record, pseudonym)
                if fingerprint is not None and fingerprint in fingerprints:
                    continue

                current = conversations.get(pseudonym)
                if current is None:
                    current = _Conversation(
                        pseudonym=pseudonym,
                        session_key=self._session_key(record.conversation_id),
                        started=started,
                        turns=0,
                        tokens=None,
                        token_timestamp=None,
                        model="unknown",
                        last_seen=now,
                        receipt_order=receipt_order,
                        agent="codex",
                        cost=None,
                    )

                turns = current.turns
                tokens = current.tokens
                token_timestamp = current.token_timestamp
                if record.event_name == "codex.user_prompt":
                    turns += 1
                elif record.event_name in _COMPLETION_EVENTS and record.input_tokens is not None:
                    if record.timestamp is None:
                        if token_timestamp is None:
                            tokens = record.input_tokens
                    elif token_timestamp is None or record.timestamp > token_timestamp:
                        tokens = record.input_tokens
                        token_timestamp = record.timestamp

                receipt_order += 1
                conversations[pseudonym] = replace(
                    current,
                    turns=turns,
                    tokens=tokens,
                    token_timestamp=token_timestamp,
                    model=record.model,
                    last_seen=now,
                    receipt_order=receipt_order,
                )
                if fingerprint is not None:
                    fingerprints[fingerprint] = now

            conversations = self._trim_conversations(conversations)
            fingerprints = self._prune_fingerprints(fingerprints, now)
            self._conversations = conversations
            self._fingerprints = fingerprints
            self._receipt_order = receipt_order

    def ingest_claude_otlp_json(self, payload: bytes) -> None:
        """Validate a complete Claude Code batch, then atomically apply it."""
        records = decode_claude_logs(payload)

        with self._lock:
            now = self._clock()
            started = self._wall_clock().strftime("%H:%M")
            conversations = self._prune_conversations(dict(self._conversations), now)
            fingerprints = self._prune_fingerprints(dict(self._fingerprints), now)
            receipt_order = self._receipt_order

            for record in records:
                pseudonym = self._pseudonym(record.session_id, agent="claude")
                fingerprint = self._claude_fingerprint(record, pseudonym)
                if fingerprint is not None and fingerprint in fingerprints:
                    continue

                current = conversations.get(pseudonym)
                if current is None:
                    current = _Conversation(
                        pseudonym=pseudonym,
                        session_key=self._session_key(record.session_id),
                        started=started,
                        turns=0,
                        tokens=None,
                        token_timestamp=None,
                        model="unknown",
                        last_seen=now,
                        receipt_order=receipt_order,
                        agent="claude",
                        cost=None,
                    )

                turns = current.turns + (1 if record.event_name == "user_prompt" else 0)

                # The real context is the sum of the three input counters.
                # Measured on 2026-08-06: input_tokens=10 for a context of
                # 18590 tokens, the rest living in the two cache counters —
                # showing input_tokens alone would understate by a factor of a
                # thousand. The LAST event wins rather than a running total:
                # this is a context size, not a throughput.
                context = [
                    value
                    for value in (
                        record.input_tokens,
                        record.cache_read_tokens,
                        record.cache_creation_tokens,
                    )
                    if value is not None
                ]
                tokens = current.tokens
                token_timestamp = current.token_timestamp
                if context and (
                    token_timestamp is None or (record.timestamp or 0) > token_timestamp
                ):
                    tokens = sum(context)
                    token_timestamp = record.timestamp

                cost = current.cost
                if record.cost_usd is not None:
                    cost = (cost or 0.0) + record.cost_usd

                receipt_order += 1
                conversations[pseudonym] = replace(
                    current,
                    turns=turns,
                    tokens=tokens,
                    token_timestamp=token_timestamp,
                    model=record.model if record.model != "unknown" else current.model,
                    cost=cost,
                    last_seen=now,
                    receipt_order=receipt_order,
                )
                if fingerprint is not None:
                    fingerprints[fingerprint] = now

            self._conversations = self._trim_conversations(conversations)
            self._fingerprints = self._prune_fingerprints(fingerprints, now)
            self._receipt_order = receipt_order

    def record_observations(self, observations: tuple[ClientObservation, ...]) -> None:
        """Apply a batch of brain-side observations.

        An observation carrying a session joins the OTLP row of that session;
        one without falls back to a per-actor residual row. The residual is the
        honest answer, not a degraded one: no client declares its session today.

        The session key is agent-neutral because this side cannot observe an
        agent — see ``_session_key``.

        C'est ici que du texte déclaré par le client entre dans le registre :
        l'acteur est donc borné à l'entrée par ``_project_actor``, avant même
        de servir de clé de seau. Assainir plus loin, dans la projection,
        laisserait deux étiquettes hostiles distinctes produire deux lignes qui
        se disputeraient un même ``id``.
        """
        with self._lock:
            now = self._clock()
            brain = self._prune_brain(dict(self._brain), now)
            for observation in observations:
                actor = _project_actor(observation.actor)
                # Trois paliers, du plus informatif au moins informatif. La
                # session l'emporte parce qu'elle JOINT : la rétrograder sur un
                # transport ferait perdre à la ligne ses colonnes OTLP au profit
                # d'un identifiant qui ne se joint à rien. Le transport passe
                # ensuite parce qu'il sépare des connexions qu'un acteur
                # confond. Le seau par acteur reste le dernier mot honnête.
                if observation.session_id is not None:
                    key = self._session_key(observation.session_id)
                elif observation.transport is not None:
                    key = self._transport_key(observation.transport)
                else:
                    key = f"{_ACTOR_KEY_PREFIX}{actor}"
                current = brain.get(key)
                brain[key] = _BrainActivity(
                    actor=actor,
                    calls=(current.calls if current else 0) + observation.calls,
                    last_seen=now,
                )
            # Les clés de session encore jointes à une conversation OTLP
            # vivante : ce sont les lignes que le panneau existe pour montrer,
            # et le plafond les range devant les résidus. Le TTL est réappliqué
            # ici parce que ce chemin ne purge pas la moitié OTLP — une
            # conversation périmée ne doit protéger personne.
            live_sessions = frozenset(
                conversation.session_key
                for conversation in self._conversations.values()
                if now - conversation.last_seen < ACTIVITY_TTL_SECONDS
            )
            self._brain = self._trim_brain(brain, live_sessions)

    def snapshot(self) -> dict[str, object]:
        """Return the current cockpit projection, free of unbounded client text.

        The previous wording — "without input-derived free text" — was simply
        false: ``actor`` reached this projection verbatim, both as a column and
        inside the ``unattributed:`` row identifier. What holds is narrower and
        checkable. Every string emitted here is either minted by this process
        (``[redacted]``, an HMAC pseudonym, the agent label of the receiver
        that decoded a batch, a wall-clock stamp) or a client-declared value
        already reduced to a bounded alphabet — the ``model`` by
        ``_model_value`` at decoding, the ``actor`` by ``_project_actor`` at
        ingest.

        Bounded is not safe. Whether these values are escaped on render is
        red-monitor's business, in another repository; this end only refuses to
        hand it anything it did not ask for.
        """
        with self._lock:
            now = self._clock()
            self._conversations = self._prune_conversations(self._conversations, now)
            self._fingerprints = self._prune_fingerprints(self._fingerprints, now)
            self._brain = self._prune_brain(self._brain, now)
            ordered = sorted(
                self._conversations.values(),
                key=lambda item: (item.last_seen, item.receipt_order),
                reverse=True,
            )
            # The legacy list stays exactly what the shipped panel reads: Codex
            # conversations. Letting Claude sessions in would silently change
            # what "Codex activity" counts, before the panel switches over.
            legacy = [item for item in ordered if item.agent == "codex"]
            active = [
                {
                    "id": item.pseudonym,
                    "topic": "[redacted]",
                    "agent": item.agent,
                    "started": item.started,
                    "turns": item.turns,
                    "tokens": item.tokens if item.tokens is not None else 0,
                    "model": item.model,
                    "cost": item.cost,
                }
                for item in legacy
            ]
            return {
                "active_convs": len(active),
                "ctx_tokens": sum(item.tokens or 0 for item in legacy),
                "activeConvs": active,
                "clients": self._client_rows(ordered, now),
            }

    def _client_rows(self, ordered: list[_Conversation], now: float) -> list[dict[str, object]]:
        """One row per client: OTLP columns, brain columns, or both when joined."""
        rows: list[dict[str, object]] = []
        for item in ordered:
            activity = self._brain.get(item.session_key)
            rows.append(
                {
                    "id": item.pseudonym,
                    "kind": "session",
                    "agent": item.agent,
                    "actor": activity.actor if activity else None,
                    "started": item.started,
                    "last_seen_s": int(now - item.last_seen),
                    "model": item.model if item.model != "unknown" else None,
                    "turns": item.turns,
                    "tokens": item.tokens,
                    "cost": item.cost,
                    "brain_calls": activity.calls if activity else None,
                }
            )

        joined = {item.session_key for item in ordered}
        for key, activity in sorted(
            self._brain.items(), key=lambda item: item[1].last_seen, reverse=True
        ):
            if key in joined:
                continue
            # Trois formes, plus la dérivation binaire d'origine qui n'en
            # connaissait que deux. ``transport`` doit rester DISTINCT de
            # ``session`` dans la projection : un consommateur qui lit
            # ``kind == "session"`` attend des colonnes OTLP, et une ligne de
            # transport n'en aura jamais.
            if key.startswith(_ACTOR_KEY_PREFIX):
                row_id, kind = f"unattributed:{activity.actor}", "unattributed"
            elif key.startswith(_TRANSPORT_KEY_PREFIX):
                row_id, kind = key, "transport"
            else:
                row_id, kind = key, "session"
            rows.append(
                {
                    "id": row_id,
                    "kind": kind,
                    "agent": None,
                    "actor": activity.actor,
                    "started": None,
                    "last_seen_s": int(now - activity.last_seen),
                    "model": None,
                    "turns": None,
                    "tokens": None,
                    "cost": None,
                    "brain_calls": activity.calls,
                }
            )
        return rows
