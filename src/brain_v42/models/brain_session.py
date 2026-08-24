"""Domain models and errors for explicit Brain session lifecycles."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from brain_v42.models.project_key import canonicalize_project_key

MAX_CAPTURED_KNOWLEDGE_IDS = 100
SESSION_STALE_AFTER = timedelta(hours=24)
# Deux seuils distincts, volontairement côte à côte pour qu'on ne les confonde
# jamais : SESSION_STALE_AFTER (24 h) est un flag DÉRIVÉ affiché au client, il
# ne change aucun statut ; AUTO_STALE_AFTER (7 j) est le seuil auquel le
# SERVEUR abandonne. Le fossé mesuré le 2026-08-07 entre le fantôme le plus
# récent (10,6 j) et la vivante la plus ancienne (0,4 j) calibre le second.
AUTO_STALE_AFTER = timedelta(days=7)
AUTO_STALE_ABANDONMENT_REASON = "auto_stale_7d"

# TROISIÈME seuil, et le seul qui ne parle pas de la même horloge que les deux
# autres : SESSION_STALE_AFTER et AUTO_STALE_AFTER lisent `last_heartbeat_at`,
# la PRÉSENCE déclarée ; celui-ci lit `last_observed_at`, l'OBSERVATION faite par
# le serveur. Il ne s'applique qu'aux traçantes `agent` (ADR §0ter.5, signé).
#
# CE N'EST PAS UN DÉLAI DE FERMETURE, et l'annoncer comme tel serait faux : logé
# dans le balayage nocturne, 4 h est un seuil d'ÉLIGIBILITÉ évalué une fois par
# nuit. Une traçante devenue inactive juste après un passage vit jusqu'au
# suivant — latence réelle pire cas ≈ 28 h.
AGENT_INACTIVE_AFTER = timedelta(hours=4)


class BrainSessionStatus(StrEnum):
    """Persistent lifecycle states.

    Three are controlled explicitly by the user. ``CLOSED_INACTIVE`` is the
    exception the covenant names: an ``agent`` tracer closed by the nightly
    sweep, without a ritual (migration 046, ADR §0.4 Q15 = route (3)).
    """

    OPEN = "open"
    ENDED = "ended"
    ABANDONED = "abandoned"
    CLOSED_INACTIVE = "closed_inactive"


class BrainSessionNature(StrEnum):
    """What KIND of actor a session belongs to.

    ``NULL`` in the database means "opened before migration 046" and is
    deliberately not backfilled — those sessions stay under the 7-day sweep.
    """

    AGENT = "agent"
    OPERATOR = "operator"


class BrainSessionFocusOutcome(StrEnum):
    """Persisted outcome of the one focus update attempted while ending."""

    APPLIED = "applied"
    CONFLICT = "conflict"


class BrainSessionError(Exception):
    """Base error for explicit Brain session lifecycle operations."""


class BrainSessionInputError(BrainSessionError, ValueError):
    """Raised when a lifecycle command contains invalid input."""


class BrainSessionNotFoundError(BrainSessionError, LookupError):
    """Raised when a requested session does not exist."""


class BrainSessionStateError(BrainSessionError):
    """Raised when an operation is illegal for the current session state."""


class BrainSessionConflictError(BrainSessionError):
    """Base error for concurrency and idempotency conflicts."""


class BrainSessionClientKeyConflictError(BrainSessionConflictError):
    """Raised when a client idempotency key cannot be safely replayed."""


class BrainSessionIdentityConflictError(BrainSessionConflictError):
    """Raised when a session UUID and expected client identity do not match."""


class BrainSessionCaptureConflictError(BrainSessionConflictError):
    """Raised when knowledge is already attributed to another session."""


class BrainSessionFocusConflictError(BrainSessionConflictError):
    """Raised when the project focus revision changed concurrently."""

    def __init__(
        self,
        message: str | None = None,
        *,
        current_focus: str | None = None,
        current_revision: int | None = None,
    ) -> None:
        self.current_focus = current_focus
        self.current_revision = current_revision
        detail = message or (
            f"project focus changed concurrently; current revision is {current_revision}"
        )
        super().__init__(detail)


class BrainSessionTerminalConflictError(BrainSessionConflictError):
    """Raised when a terminal replay differs from the persisted outcome."""


class BrainSession(BaseModel):
    """Persistent state of one explicitly controlled concurrent session."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_key: str = Field(..., max_length=50)
    client_key: str = Field(..., min_length=1, max_length=128)
    status: BrainSessionStatus = BrainSessionStatus.OPEN
    started_focus: str | None = None
    started_focus_revision: int = Field(..., ge=0)
    summary: str | None = None
    next_focus: str | None = None
    captured_knowledge_ids: list[UUID] = Field(
        default_factory=list,
        max_length=MAX_CAPTURED_KNOWLEDGE_IDS,
    )
    attributed_knowledge_ids: list[UUID] = Field(
        default_factory=list,
        max_length=MAX_CAPTURED_KNOWLEDGE_IDS,
        description=(
            "Effective artifact attributions loaded from the session ledger. Unlike the "
            "terminal captured_knowledge_ids snapshot, this remains observable while the "
            "session is open or abandoned."
        ),
    )
    nothing_to_capture_reason: str | None = None
    abandonment_reason: str | None = None
    end_expected_focus_revision: int | None = Field(default=None, ge=0)
    focus_outcome: BrainSessionFocusOutcome | None = None
    focus_at_end: str | None = None
    focus_revision_at_end: int | None = Field(default=None, ge=0)
    # `Literal` et non `BrainSessionNature`, et c'est une décision de BUDGET
    # mesurée, pas un raccourci : l'enum génère une entrée `$defs` recopiée dans
    # les quatre schémas de sortie dérivés (total 10170 octets) là où le
    # `Literal` s'inline (9381). Même contrainte côté client, 789 octets de
    # moins. `BrainSessionNature` reste la forme employée par le CODE.
    #
    # C7 exige que la MACHINE D'ÉTATS bouge avec le CHECK. `nature` en fait
    # partie : la branche `closed_inactive` de la 046 la contraint à `agent`.
    # Les quatre autres colonnes de la 046 — `started_by_actor`,
    # `last_observed_at`, `intent`, `connection_id` — ne sont dans AUCUN CHECK.
    # Trois ont désormais un écrivain (l'auto-ouverture, et l'observation pour
    # `last_observed_at`) ; `intent` n'en a toujours aucun. Avoir un écrivain
    # n'est PAS un titre d'entrée ici : elles n'entrent toujours pas — FastMCP
    # dérive le schéma de sortie des tools de ce modèle, et les cinq colonnes
    # portaient le total de 8487 à 11292 octets — au-dessus du plancher
    # d'économie de 9041 que `test_discovery_contract_keeps_tool_identity_inputs_
    # and_schema_budget` garantit. Chaque colonne rejoindra ce modèle avec le
    # commit qui l'utilise, et paiera son schéma à ce moment-là, délibérément.
    nature: Literal["agent", "operator"] | None = None
    started_at: datetime
    last_heartbeat_at: datetime | None = None
    ended_at: datetime | None = None
    updated_at: datetime
    is_stale: bool = False

    @field_validator("project_key")
    @classmethod
    def _canonicalize_project_key(cls, value: str) -> str:
        return canonicalize_project_key(value)

    @field_validator("client_key")
    @classmethod
    def _normalize_client_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("client_key must not be blank")
        return normalized

    @model_validator(mode="after")
    def _validate_terminal_state(self) -> BrainSession:
        if self.last_heartbeat_at is None:
            self.last_heartbeat_at = self.updated_at
        if not self.attributed_knowledge_ids and self.captured_knowledge_ids:
            self.attributed_knowledge_ids = list(self.captured_knowledge_ids)
        # Every status gets an EXPLICIT branch, and the fallthrough raises.
        # This used to end in a bare `else: self._validate_abandoned_state()`.
        # Adding `closed_inactive` under that shape would have validated it with
        # the ABANDONED rules — which demand `abandonment_reason`, the very field
        # the 046 CHECK forbids on that branch. The Pydantic rail would then
        # reject exactly what the database accepts: a C7 divergence, silent.
        if self.status is BrainSessionStatus.OPEN:
            self._validate_open_state()
        elif self.status is BrainSessionStatus.ENDED:
            self._validate_ended_state()
        elif self.status is BrainSessionStatus.ABANDONED:
            self._validate_abandoned_state()
        elif self.status is BrainSessionStatus.CLOSED_INACTIVE:
            self._validate_closed_inactive_state()
        else:  # pragma: no cover - defended against a future fifth status
            raise ValueError(f"no terminal-state rule for status {self.status!r}")
        return self

    def _validate_open_state(self) -> None:
        terminal_values = (
            self.summary,
            self.next_focus,
            self.nothing_to_capture_reason,
            self.abandonment_reason,
            self.end_expected_focus_revision,
            self.focus_outcome,
            self.focus_at_end,
            self.focus_revision_at_end,
            self.ended_at,
        )
        if any(value is not None for value in terminal_values) or self.captured_knowledge_ids:
            raise ValueError("open session cannot contain terminal outcome fields")

    def _validate_ended_state(self) -> None:
        if self.ended_at is None:
            raise ValueError("ended session requires ended_at")
        if not self.summary or not self.summary.strip():
            raise ValueError("ended session requires summary")
        if not self.next_focus or not self.next_focus.strip():
            raise ValueError("ended session requires next_focus")
        if self.abandonment_reason is not None:
            raise ValueError("ended session cannot contain abandonment_reason")
        if self.focus_outcome is None:
            raise ValueError("ended session requires focus_outcome")
        if self.is_stale:
            raise ValueError("terminal session cannot be stale")
        # Le XOR « ledger non vide XOR raison » a été RETIRÉ avec la 047, et pas
        # affaibli : il mesurait « le client a-t-il DÉCLARÉ ». La capture
        # dérivée supprime le seul mode de panne qu'il attrapait
        # (produit-mais-non-déclaré) et alimenterait désormais son signal côté
        # SERVEUR. Un contrôle est creux dès que l'objet contrôlé peut
        # influencer son signal ; celui-ci serait devenu un reçu que le serveur
        # se délivre à lui-même. Le conserver rendait surtout INFERMABLE toute
        # session dont le serveur avait rempli le ledger.
        #
        # Ce qui reste est le seul contrôle que le serveur ne peut pas
        # satisfaire à la place de l'utilisateur : une raison donnée doit dire
        # quelque chose.
        reason = self.nothing_to_capture_reason
        if reason is not None and not reason.strip():
            raise ValueError("nothing_to_capture_reason must not be blank")
        if set(self.attributed_knowledge_ids) != set(self.captured_knowledge_ids):
            raise ValueError("ended session attribution must match its capture snapshot")
        if self.end_expected_focus_revision is None:
            if (
                self.focus_outcome is not BrainSessionFocusOutcome.APPLIED
                or self.focus_at_end != self.next_focus
                or self.focus_revision_at_end is not None
            ):
                raise ValueError("legacy ended session has an invalid focus snapshot")
            return
        if self.focus_revision_at_end is None:
            raise ValueError("v4 ended session requires a final focus revision")
        if self.focus_outcome is BrainSessionFocusOutcome.APPLIED:
            if (
                self.focus_at_end != self.next_focus
                or self.focus_revision_at_end != self.end_expected_focus_revision + 1
            ):
                raise ValueError("applied focus snapshot is inconsistent")
        elif self.focus_revision_at_end == self.end_expected_focus_revision:
            raise ValueError("conflicted focus snapshot must expose a different revision")

    def _validate_closed_inactive_state(self) -> None:
        """Mirror of the 046 CHECK branch — deliberately, field for field.

        The asymmetry that matters: ``captured_knowledge_ids`` carries NO
        constraint here. ``abandoned`` forces it to zero, which would make an
        agent session that did its work declare an empty ledger in its terminal
        snapshot. And ``nothing_to_capture_reason`` is FORBIDDEN rather than
        required: on the ``ended`` branch it is a human judgement about why
        nothing was captured, and a server filling it in would manufacture
        judgement (objection C9).
        """
        if self.ended_at is None:
            raise ValueError("closed_inactive session requires ended_at")
        if self.nature != BrainSessionNature.AGENT:
            raise ValueError("only an agent session can be closed_inactive")
        if (
            self.summary is not None
            or self.next_focus is not None
            or self.nothing_to_capture_reason is not None
            or self.abandonment_reason is not None
            or self.end_expected_focus_revision is not None
            or self.focus_outcome is not None
            or self.focus_at_end is not None
            or self.focus_revision_at_end is not None
        ):
            raise ValueError("closed_inactive session cannot contain ritual outcome fields")
        if self.is_stale:
            raise ValueError("terminal session cannot be stale")

    def _validate_abandoned_state(self) -> None:
        if self.ended_at is None:
            raise ValueError("abandoned session requires ended_at")
        if not self.abandonment_reason or not self.abandonment_reason.strip():
            raise ValueError("abandoned session requires abandonment_reason")
        if (
            self.summary is not None
            or self.next_focus is not None
            or self.nothing_to_capture_reason is not None
            or self.captured_knowledge_ids
            or self.end_expected_focus_revision is not None
            or self.focus_outcome is not None
            or self.focus_at_end is not None
            or self.focus_revision_at_end is not None
        ):
            raise ValueError("abandoned session cannot contain end outcome fields")
        if self.is_stale:
            raise ValueError("terminal session cannot be stale")


class BrainSessionStartResult(BaseModel):
    """Outcome of an idempotent session start."""

    session: BrainSession
    replayed: bool
    open_session_count: int = Field(..., ge=0)
    briefing: str = ""


class BrainSessionResumeResult(BaseModel):
    """Current context returned when attaching to an open session."""

    session: BrainSession
    open_session_count: int = Field(..., ge=0)
    current_focus: str | None
    current_focus_revision: int = Field(..., ge=0)
    briefing: str = ""


class BrainSessionEndResult(BaseModel):
    """Outcome of ending a session and conditionally updating shared focus."""

    session: BrainSession
    replayed: bool
    remaining_open_session_count: int = Field(..., ge=0)
    current_focus: str | None
    current_focus_revision: int = Field(..., ge=0)
    focus_outcome: BrainSessionFocusOutcome
    focus_at_end: str | None
    focus_revision_at_end: int | None = Field(default=None, ge=0)
    #: Artefacts du projet créés PENDANT la session et présents dans AUCUN
    #: ledger. Une MESURE, jamais une porte : elle ne peut pas refuser une
    #: fermeture. C'est ce qui remplace le XOR — informer au lieu de punir.
    #:
    #: Non-influençable par construction : une session ne peut pas la faire
    #: baisser en ne faisant rien. L'inaction ne produit aucun artefact, donc
    #: aucun orphelin ; le nombre ne descend qu'en attribuant réellement. Un
    #: compteur qu'on améliorerait en se taisant serait le reçu retiré, renommé.
    unattributed_in_window: int = Field(..., ge=0)


class BrainSessionCaptureResult(BaseModel):
    """Persistent artifact attributions recorded for one session."""

    session: BrainSession
    captured_knowledge_ids: list[UUID] = Field(
        default_factory=list,
        max_length=MAX_CAPTURED_KNOWLEDGE_IDS,
    )
    newly_captured_knowledge_ids: list[UUID] = Field(default_factory=list)
    replayed_knowledge_ids: list[UUID] = Field(default_factory=list)
    replayed: bool


class BrainSessionHeartbeatResult(BaseModel):
    """Fresh presence marker for one still-open session."""

    session: BrainSession


class BrainSessionAbandonResult(BaseModel):
    """Outcome of explicitly discarding a session."""

    session: BrainSession
    replayed: bool
    remaining_open_session_count: int = Field(..., ge=0)


class BrainSessionListResult(BaseModel):
    """Paginated session listing."""

    sessions: list[BrainSession]
    total: int = Field(..., ge=0)
    limit: int = Field(..., ge=1)
    offset: int = Field(..., ge=0)


class BrainSessionSweepCandidate(BaseModel):
    """Une session ouverte retenue par le balayage, en DRY comme en WET."""

    id: UUID
    project_key: str
    client_key: str
    last_heartbeat_at: datetime
    #: NULL veut dire « jamais observée » — donc hors d'atteinte de la règle des
    #: 4 h, qui ne prend que ce qu'elle a vu vivre (S3, tranché).
    last_observed_at: datetime | None = None
    #: L'état terminal que CETTE ligne a reçu, ou recevrait. Obligatoire et sans
    #: défaut : deux règles écrivent dans le même statement, et un rapport qui
    #: les confondrait rendrait la préséance invérifiable.
    outcome: BrainSessionStatus

    @field_validator("outcome")
    @classmethod
    def _outcome_is_a_sweep_outcome(cls, value: BrainSessionStatus) -> BrainSessionStatus:
        if value not in (BrainSessionStatus.ABANDONED, BrainSessionStatus.CLOSED_INACTIVE):
            raise ValueError(f"{value} is not an outcome the sweep can produce")
        return value


class BrainSessionSweepResult(BaseModel):
    """Résultat d'un balayage serveur, tous projets confondus."""

    candidates: list[BrainSessionSweepCandidate]
    dry_run: bool
    #: Seuil de PRÉSENCE (7 j), lu sur `last_heartbeat_at`. Toujours actif.
    cutoff: datetime
    #: Seuil d'OBSERVATION (4 h), lu sur `last_observed_at`. ``None`` veut dire
    #: que la règle est fermée — pas qu'aucune session ne l'a atteint.
    inactive_cutoff: datetime | None = None
    # Toujours 0 en DRY. Redondant avec len(candidates) — délibérément : un
    # journal doit rendre « 17 auraient été abandonnées » illisible comme
    # « 17 ont été abandonnées ».
    abandoned_count: int = Field(..., ge=0)
    #: Compteur DISTINCT, jamais mêlé à `abandoned_count`. Les deux règles
    #: produisent deux états terminaux différents — `abandoned` porte une raison
    #: et jamais de ledger, `closed_inactive` porte son ledger et aucune raison.
    #: Les additionner effacerait la seule distinction que la 046 a coûté une
    #: migration à créer.
    closed_inactive_count: int = Field(default=0, ge=0)
