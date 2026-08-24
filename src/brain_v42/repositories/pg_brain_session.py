"""PostgreSQL persistence for explicit concurrent Brain sessions."""

from __future__ import annotations

import builtins
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from secrets import compare_digest
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.focus_stamp import focus_stamp
from brain_v42.db.tables import (
    adrs,
    brain_session_artifacts,
    brain_sessions,
    decisions,
    indexed_plans,
    learnings,
    project_contexts,
    runbooks,
    snippets,
)
from brain_v42.models.brain_session import (
    AUTO_STALE_ABANDONMENT_REASON,
    AUTO_STALE_AFTER,
    MAX_CAPTURED_KNOWLEDGE_IDS,
    SESSION_STALE_AFTER,
    BrainSession,
    BrainSessionAbandonResult,
    BrainSessionCaptureConflictError,
    BrainSessionCaptureResult,
    BrainSessionClientKeyConflictError,
    BrainSessionEndResult,
    BrainSessionFocusOutcome,
    BrainSessionHeartbeatResult,
    BrainSessionIdentityConflictError,
    BrainSessionInputError,
    BrainSessionListResult,
    BrainSessionNotFoundError,
    BrainSessionResumeResult,
    BrainSessionStartResult,
    BrainSessionStateError,
    BrainSessionStatus,
    BrainSessionSweepCandidate,
    BrainSessionSweepResult,
    BrainSessionTerminalConflictError,
)
from brain_v42.repositories.pg_base import BasePgRepository

Row = dict[str, Any]


def _observation_columns(reference: datetime) -> dict[str, datetime]:
    """Les DEUX horloges qu'une observation de traçante déplace, et pas une de plus.

    Écrit une seule fois parce que les deux écrivains — l'``ON CONFLICT DO
    UPDATE`` de ``auto_open`` et ``observe`` — doivent bouger EXACTEMENT le même
    ensemble. Les laisser diverger donnerait à une connexion réidentifiée une
    horloge de présence différente de celle d'une connexion réobservée, et le
    balayage lirait deux régimes pour un seul geste.
    """
    return {"last_observed_at": reference, "last_heartbeat_at": reference}


CAPTURE_TABLES = (
    (decisions, "decision"),
    (learnings, "learning"),
    (snippets, "snippet"),
    (runbooks, "runbook"),
    (adrs, "adr"),
    (indexed_plans, "indexed_plan"),
)


class PgBrainSessionRepo(BasePgRepository):
    """Own the atomic lifecycle of persistent Brain sessions."""

    table = brain_sessions
    fts_columns: list[str] = []

    async def start(self, project_key: str, client_key: str) -> BrainSessionStartResult:
        """Create or replay a session idempotently for a project/client key."""
        normalized_client_key = client_key.strip()
        if not normalized_client_key:
            raise BrainSessionInputError("client_key must not be blank")

        async with self.transaction() as session:
            focus = await self._load_focus(session, project_key)
            if focus is None:
                raise BrainSessionNotFoundError(f"Project {project_key!r} was not found")

            insert_stmt = (
                pg_insert(brain_sessions)
                .values(
                    project_key=project_key,
                    client_key=normalized_client_key,
                    started_focus=focus["current_focus"],
                    started_focus_revision=focus["focus_revision"],
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        brain_sessions.c.project_key,
                        brain_sessions.c.client_key,
                    ]
                )
                .returning(brain_sessions)
            )
            insert_result = await session.execute(insert_stmt)
            inserted_row = insert_result.mappings().one_or_none()
            row = dict(inserted_row) if inserted_row is not None else None
            replayed = inserted_row is None

            if replayed:
                row = await self._get_by_client_key(session, project_key, normalized_client_key)
            if row is None:
                raise BrainSessionClientKeyConflictError(
                    f"Could not replay client_key {normalized_client_key!r}"
                )

            model = self._to_model(row)
            if replayed and model.status != "open":
                raise BrainSessionClientKeyConflictError(
                    f"client_key {normalized_client_key!r} belongs to a "
                    f"{model.status.value} session; use a new client_key"
                )

            if replayed:
                attributed_ids = await self._load_session_artifact_ids(session, model.id)
                model = model.model_copy(update={"attributed_knowledge_ids": attributed_ids})

            open_count = await self._count_open(session, project_key)
            return BrainSessionStartResult(
                session=model,
                replayed=replayed,
                open_session_count=open_count,
            )

    async def auto_open(self, identity: Any, *, now: datetime | None = None) -> UUID | None:
        """Ouvrir — ou retrouver ET RÉOBSERVER — LA session `agent` d'une connexion.

        Un seul aller-retour, conflit compris. L'idempotence est portée par
        l'index UNIQUE **PARTIEL** ``uq_brain_sessions_connection`` de la 046
        (``WHERE status = 'open'``), pas par le code appelant : deux appels
        concurrents sur la même connexion produisent un conflit, pas deux
        sessions, et une session déjà fermée ne bloque pas la suivante. Un
        index plein aurait brûlé la connexion à vie dès la première
        auto-fermeture (piège hérité, `SPEC-M-G` §5).

        ``ON CONFLICT DO UPDATE`` et non ``DO NOTHING`` : le conflit est le cas
        « cette connexion a déjà sa session », et c'est **une observation**.
        L'ancienne forme le suivait d'un ``SELECT`` pour retrouver l'id — deux
        allers-retours qui ne dataient rien. Ici la même ligne est retrouvée
        ET réobservée, et le ``RETURNING`` rend l'id des deux branches.

        ``client_key`` reçoit un UUID neuf, et ce n'est pas un détail : la
        contrainte ``uq_brain_sessions_project_client`` est **pleine**, donc
        réutiliser une clé stable par connexion ferait échouer la réouverture
        après une fermeture — le piège de l'index partiel, déplacé d'une
        colonne. Sur ce chemin la clé cliente ne garde plus rien de toute
        façon : ``expected_client_key`` en a été retirée (§0ter.3), l'identité
        étant la connexion.

        ``started_at`` est posée EXPLICITEMENT, et seulement sur la branche
        INSERT. Sans elle la colonne tombait sur le ``DEFAULT now()`` de la
        base — l'estampille de DÉBUT DE TRANSACTION, donc postérieure au
        ``reference`` que l'application lit AVANT d'ouvrir la transaction. La
        ligne naissait avec ``last_heartbeat_at`` daté 1,5 ms avant son propre
        démarrage, et le contrat DR le comptait : reçu 28/29 mesuré en
        production le 2026-08-22, sur les deux variantes de l'actif. ``start()``
        n'a jamais eu le défaut parce qu'il ne pose AUCUNE des deux colonnes :
        ses horloges viennent du même défaut. C'est l'asymétrie qui coûtait,
        pas le défaut lui-même.

        Elle reste hors de ``_observation_columns()``, et c'est le fond du
        correctif : réobserver n'est pas rouvrir. La glisser dans l'ensemble
        partagé ferait rajeunir la traçante à chaque appel d'outil, et le
        balayage des 7 j ne prendrait plus jamais rien.

        Rend ``None`` quand le projet n'a pas de contexte : le serveur n'en
        fabrique pas un. Ne lève pas sur ce cas — ``start()`` le fait, parce
        que là c'est un utilisateur qui a nommé un projet inexistant et qu'il
        doit l'apprendre ; ici personne n'a rien nommé.
        """
        reference = now or datetime.now(UTC)
        async with self.transaction() as session:
            focus = await self._load_focus(session, identity.project_key)
            if focus is None:
                return None

            insert_stmt = (
                pg_insert(brain_sessions)
                .values(
                    project_key=identity.project_key,
                    client_key=f"auto:{uuid4().hex}",
                    started_focus=focus["current_focus"],
                    started_focus_revision=focus["focus_revision"],
                    nature=identity.nature,
                    connection_id=identity.connection_id,
                    started_by_actor=identity.started_by_actor,
                    intent=identity.intent,
                    started_at=reference,
                    last_observed_at=reference,
                    last_heartbeat_at=reference,
                )
                .on_conflict_do_update(
                    index_elements=[
                        brain_sessions.c.project_key,
                        brain_sessions.c.connection_id,
                    ],
                    index_where=sa.text("status = 'open'"),
                    set_=_observation_columns(reference),
                    # Garde DURE sur l'ACTION du conflit, symétrique de celle
                    # d'``observe`` : sans elle, une ligne `operator` qui
                    # porterait une connexion verrait `last_heartbeat_at`
                    # re-datée à chaque appel d'outil. Or l'éligibilité 7 jours
                    # du balayage lit cette colonne SANS filtre de nature — la
                    # seule exception écrite au covenant deviendrait
                    # inatteignable, et la ligne un fantôme immortel.
                    where=brain_sessions.c.nature == "agent",
                )
                .returning(brain_sessions.c.id)
            )
            inserted = (await session.execute(insert_stmt)).scalar_one_or_none()
            return UUID(str(inserted)) if inserted is not None else None

    async def absorb_derived_capture(self, session_id: UUID | str, connection_id: str) -> int:
        """Faire absorber à cette session le ledger de la traçante de sa connexion.

        Le dépôt ne décide rien ici : il ouvre la transaction, retrouve la
        session cible et délègue les bornes à ``absorb_tracer_ledger``, qui les
        aligne sur celles d'une capture EXPLICITE. Le service a déjà tranché le
        drapeau et la connexion en amont, pour qu'un drapeau fermé ne coûte pas
        cet aller-retour.

        Rend le nombre de lignes déplacées ; ``0`` couvre tous les refus.
        """
        from brain_v42.db.session_derived_capture import (  # noqa: PLC0415
            absorb_tracer_ledger,
        )

        async with self.transaction() as session:
            row = await self._get_row(session, session_id)
            if row is None:
                return 0
            target = SimpleNamespace(
                id=row["id"],
                project_key=row["project_key"],
                started_at=row["started_at"],
            )
            return await absorb_tracer_ledger(session, target, connection_id)

    async def observe(self, session_id: UUID | str, *, now: datetime | None = None) -> bool:
        """Dater l'observation d'une traçante `agent` ouverte. Rend « encore ouverte ».

        C'est l'écrivain que la 046 attendait : sans lui, ``last_observed_at``
        reste NULL sur toute la table, et la règle des 4 h du balayage ne matche
        RIEN — M-G serait livrée inerte. La garantie 2 du `§0bis.3` est
        littérale : la colonne bouge à **chaque** appel d'outil.

        **Le faux-mort, et pourquoi ``last_heartbeat_at`` bouge aussi.** Le
        balayage 7 j lit ``last_heartbeat_at``. Une traçante dont la connexion
        vit huit jours n'a jamais rappelé de heartbeat — il n'y a pas
        d'utilisateur pour le faire — et serait abandonnée en pleine activité.
        C'est exactement le faux-mort du 2026-08-06. Les deux horloges bougent
        donc ensemble sur ce chemin, et restent deux colonnes : le 4 h lit
        l'observation, le 7 j lit la présence, et une traçante inactive depuis
        plus de sept jours matche encore les DEUX (préséance : `sweep_open_sessions`).

        **``updated_at`` ne bouge PAS**, et c'est délibéré : observer n'est pas
        muter l'état déclaré de la session. Le rafraîchir ferait de la colonne
        de dernière écriture un signal d'activité, exactement le contrôle creux
        que `77348350` a coûté ailleurs.

        Le ``nature = 'agent'`` du prédicat est une garde DURE, pas une
        redondance : ce chemin ne doit jamais pouvoir dater une session
        `operator`, même si une mémo empoisonnée lui en présentait l'UUID.
        """
        reference = now or datetime.now(UTC)
        statement = (
            brain_sessions.update()
            .where(
                brain_sessions.c.id == session_id,
                brain_sessions.c.status == "open",
                brain_sessions.c.nature == "agent",
            )
            .values(**_observation_columns(reference))
            .returning(brain_sessions.c.id)
        )
        async with self.transaction() as session:
            observed = (await session.execute(statement)).scalar_one_or_none()
        return observed is not None

    async def get_by_id(  # type: ignore[override]
        self, session_id: UUID | str
    ) -> BrainSession | None:
        """Return a session by id without changing its state."""
        async with self.get_session() as session:
            row = await self._get_row(session, session_id)
            if row is None:
                return None
            attributed_ids = await self._load_session_artifact_ids(session, row["id"])
        return self._to_model(row, attributed_ids=attributed_ids)

    async def list(
        self,
        project_key: str | None = None,
        status: str | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> BrainSessionListResult:
        """List project sessions in reverse chronological order."""
        if limit < 1 or limit > 100 or offset < 0:
            raise BrainSessionInputError("limit must be between 1 and 100 and offset non-negative")

        filters: list[Any] = []
        if project_key is not None:
            filters.append(brain_sessions.c.project_key == project_key)
        now = datetime.now(UTC)
        if status == "stale":
            filters.extend(
                (
                    brain_sessions.c.status == "open",
                    brain_sessions.c.last_heartbeat_at <= now - SESSION_STALE_AFTER,
                )
            )
        elif status not in (None, "all"):
            filters.append(brain_sessions.c.status == status)

        async with self.get_session() as session:
            count_stmt = sa.select(sa.func.count()).select_from(brain_sessions).where(*filters)
            total = int((await session.execute(count_stmt)).scalar_one())
            list_stmt = (
                sa.select(brain_sessions)
                .where(*filters)
                .order_by(brain_sessions.c.started_at.desc())
                .limit(limit)
                .offset(offset)
            )
            rows = (await session.execute(list_stmt)).mappings().all()
            attributed_by_session = await self._load_artifact_ids_by_session(
                session,
                [row["id"] for row in rows],
            )

        return BrainSessionListResult(
            sessions=[
                self._to_model(
                    row,
                    now=now,
                    attributed_ids=attributed_by_session.get(row["id"], []),
                )
                for row in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def resume(
        self,
        session_id: UUID | str,
        expected_client_key: str,
    ) -> BrainSessionResumeResult:
        """Read an open session and current focus without mutating either."""
        async with self.get_session() as session:
            row = await self._get_row(session, session_id)
            if row is None:
                raise BrainSessionNotFoundError(f"Session {session_id} was not found")
            model = self._to_model(row)
            self._assert_identity(model, expected_client_key)
            if model.status != "open":
                raise BrainSessionStateError(f"Session {session_id} is {model.status}, not open")

            attributed_ids = await self._load_session_artifact_ids(session, model.id)
            model = model.model_copy(update={"attributed_knowledge_ids": attributed_ids})

            focus = await self._load_focus(session, model.project_key)
            if focus is None:
                raise BrainSessionNotFoundError(f"Project {model.project_key!r} was not found")
            open_count = await self._count_open(session, model.project_key)

        return BrainSessionResumeResult(
            session=model,
            open_session_count=open_count,
            current_focus=focus["current_focus"],
            current_focus_revision=focus["focus_revision"],
        )

    async def capture(
        self,
        session_id: UUID | str,
        expected_client_key: str,
        knowledge_ids: Sequence[UUID],
    ) -> BrainSessionCaptureResult:
        """Attach durable knowledge to exactly one explicit session."""
        capture_ids = sorted(knowledge_ids, key=str)
        self._validate_capture_ids(capture_ids, require_nonempty=True)

        async with self.transaction() as session:
            row = await self._get_row(session, session_id, for_update=True)
            if row is None:
                raise BrainSessionNotFoundError(f"Session {session_id} was not found")
            model = self._to_model(row)
            self._assert_identity(model, expected_client_key)

            if model.status != "open":
                existing = await self._load_artifact_rows(session, capture_ids)
                existing_ids = self._owned_capture_ids(existing, model.id)
                if model.status in {"ended", "abandoned"} and existing_ids == set(capture_ids):
                    all_ids = await self._load_session_artifact_ids(session, model.id)
                    return BrainSessionCaptureResult(
                        session=model.model_copy(update={"attributed_knowledge_ids": all_ids}),
                        captured_knowledge_ids=all_ids,
                        newly_captured_knowledge_ids=[],
                        replayed_knowledge_ids=capture_ids,
                        replayed=True,
                    )
                raise BrainSessionStateError(f"Session {session_id} is {model.status}, not open")

            focus = await self._load_focus(session, model.project_key, for_update=True)
            if focus is None:
                raise BrainSessionNotFoundError(f"Project {model.project_key!r} was not found")
            existing = await self._load_artifact_rows(session, capture_ids)
            existing_ids = self._owned_capture_ids(existing, model.id)
            resolved_types = await self._validate_captures(session, model, capture_ids)
            all_existing = await self._load_session_artifact_ids(session, model.id)
            all_after = sorted(set(all_existing) | set(capture_ids), key=str)
            if len(all_after) > MAX_CAPTURED_KNOWLEDGE_IDS:
                raise BrainSessionInputError(
                    "a session may capture at most "
                    f"{MAX_CAPTURED_KNOWLEDGE_IDS} knowledge artifacts"
                )

            missing = [item for item in capture_ids if item not in existing_ids]
            if missing:
                insert_stmt = (
                    pg_insert(brain_session_artifacts)
                    .values(
                        [
                            {
                                "knowledge_id": knowledge_id,
                                "session_id": model.id,
                                "knowledge_type": resolved_types[knowledge_id],
                            }
                            for knowledge_id in missing
                        ]
                    )
                    .on_conflict_do_nothing(index_elements=[brain_session_artifacts.c.knowledge_id])
                    .returning(brain_session_artifacts.c.knowledge_id)
                )
                inserted_ids = set((await session.execute(insert_stmt)).scalars().all())
                if len(inserted_ids) != len(missing):
                    raced_rows = await self._load_artifact_rows(session, missing)
                    raced_owned_ids = self._owned_capture_ids(raced_rows, model.id)
                    unresolved = set(missing) - inserted_ids - raced_owned_ids
                    if unresolved:
                        raise BrainSessionStateError(
                            "session artifact ownership could not be resolved"
                        )
                    existing_ids.update(raced_owned_ids)
                missing = sorted(inserted_ids, key=str)

            now = datetime.now(UTC)
            heartbeat_stmt = (
                brain_sessions.update()
                .where(brain_sessions.c.id == model.id)
                .values(last_heartbeat_at=now, updated_at=now)
                .returning(brain_sessions)
            )
            updated_row = (await session.execute(heartbeat_stmt)).mappings().one_or_none()
            if updated_row is None:
                raise BrainSessionStateError(f"Session {session_id} could not record captures")

            return BrainSessionCaptureResult(
                session=self._to_model(
                    updated_row,
                    now=now,
                    attributed_ids=all_after,
                ),
                captured_knowledge_ids=all_after,
                newly_captured_knowledge_ids=missing,
                replayed_knowledge_ids=sorted(existing_ids, key=str),
                replayed=not missing,
            )

    async def heartbeat(
        self,
        session_id: UUID | str,
        expected_client_key: str,
    ) -> BrainSessionHeartbeatResult:
        """Refresh session presence without changing focus or lifecycle state."""
        async with self.transaction() as session:
            row = await self._get_row(session, session_id, for_update=True)
            if row is None:
                raise BrainSessionNotFoundError(f"Session {session_id} was not found")
            model = self._to_model(row)
            self._assert_identity(model, expected_client_key)
            if model.status != "open":
                raise BrainSessionStateError(f"Session {session_id} is {model.status}, not open")

            attributed_ids = await self._load_session_artifact_ids(session, model.id)

            now = datetime.now(UTC)
            stmt = (
                brain_sessions.update()
                .where(brain_sessions.c.id == model.id)
                .values(last_heartbeat_at=now, updated_at=now)
                .returning(brain_sessions)
            )
            updated_row = (await session.execute(stmt)).mappings().one_or_none()
            if updated_row is None:
                raise BrainSessionStateError(f"Session {session_id} heartbeat was not persisted")
            return BrainSessionHeartbeatResult(
                session=self._to_model(
                    updated_row,
                    now=now,
                    attributed_ids=attributed_ids,
                )
            )

    async def end(
        self,
        session_id: UUID | str,
        expected_client_key: str,
        summary: str,
        next_focus: str,
        expected_focus_revision: int,
        nothing_to_capture_reason: str | None,
    ) -> BrainSessionEndResult:
        """End atomically while treating a stale shared focus as data."""
        normalized_summary = summary.strip()
        normalized_focus = next_focus.strip()
        normalized_reason = self._normalize_optional(nothing_to_capture_reason)
        self._validate_end_input(normalized_summary, normalized_focus, expected_focus_revision)

        async with self.transaction() as session:
            row = await self._get_row(session, session_id, for_update=True)
            if row is None:
                raise BrainSessionNotFoundError(f"Session {session_id} was not found")
            model = self._to_model(row)
            self._assert_identity(model, expected_client_key)

            if model.status != "open":
                return await self._replay_end(
                    session,
                    model,
                    normalized_summary,
                    normalized_focus,
                    expected_focus_revision,
                    normalized_reason,
                )

            focus_before = await self._load_focus(
                session,
                model.project_key,
                for_update=True,
            )
            if focus_before is None:
                raise BrainSessionNotFoundError(f"Project {model.project_key!r} was not found")

            capture_ids = await self._load_session_artifact_ids(session, model.id)
            self._validate_capture_outcome(capture_ids, normalized_reason)
            if capture_ids:
                await self._validate_captures(session, model, capture_ids)

            focus, focus_outcome = await self._apply_focus_if_current(
                session,
                focus_before,
                project_key=model.project_key,
                next_focus=normalized_focus,
                expected_revision=expected_focus_revision,
            )
            ended = await self._mark_ended(
                session,
                model.id,
                normalized_summary,
                normalized_focus,
                capture_ids,
                normalized_reason,
                expected_focus_revision=expected_focus_revision,
                focus_outcome=focus_outcome,
                focus_at_end=focus["current_focus"],
                focus_revision_at_end=focus["focus_revision"],
            )
            remaining = await self._count_open(session, model.project_key)

            return BrainSessionEndResult(
                session=ended,
                replayed=False,
                remaining_open_session_count=remaining,
                current_focus=focus["current_focus"],
                current_focus_revision=focus["focus_revision"],
                focus_outcome=focus_outcome,
                focus_at_end=focus["current_focus"],
                focus_revision_at_end=focus["focus_revision"],
            )

    async def abandon(
        self,
        session_id: UUID | str,
        expected_client_key: str,
        reason: str,
    ) -> BrainSessionAbandonResult:
        """Atomically abandon an open session without touching project focus."""
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise BrainSessionInputError("abandonment reason must not be blank")

        async with self.transaction() as session:
            row = await self._get_row(session, session_id, for_update=True)
            if row is None:
                raise BrainSessionNotFoundError(f"Session {session_id} was not found")
            model = self._to_model(row)
            self._assert_identity(model, expected_client_key)

            if model.status != "open":
                if model.status == "abandoned" and model.abandonment_reason == normalized_reason:
                    attributed_ids = await self._load_session_artifact_ids(session, model.id)
                    remaining = await self._count_open(session, model.project_key)
                    return BrainSessionAbandonResult(
                        session=model.model_copy(
                            update={"attributed_knowledge_ids": attributed_ids}
                        ),
                        replayed=True,
                        remaining_open_session_count=remaining,
                    )
                raise BrainSessionTerminalConflictError(
                    f"Session {session_id} already has terminal state {model.status}"
                )

            attributed_ids = await self._load_session_artifact_ids(session, model.id)
            now = datetime.now(UTC)
            update_stmt = (
                brain_sessions.update()
                .where(brain_sessions.c.id == model.id)
                .values(
                    status="abandoned",
                    abandonment_reason=normalized_reason,
                    ended_at=now,
                    updated_at=now,
                )
                .returning(brain_sessions)
            )
            updated_row = (await session.execute(update_stmt)).mappings().one_or_none()
            if updated_row is None:
                raise BrainSessionStateError(f"Session {session_id} could not be abandoned")
            remaining = await self._count_open(session, model.project_key)
            return BrainSessionAbandonResult(
                session=self._to_model(
                    updated_row,
                    attributed_ids=attributed_ids,
                ),
                replayed=False,
                remaining_open_session_count=remaining,
            )

    async def sweep_open_sessions(
        self,
        *,
        older_than: timedelta = AUTO_STALE_AFTER,
        reason: str = AUTO_STALE_ABANDONMENT_REASON,
        close_inactive_after: timedelta | None = None,
        dry_run: bool = True,
        now: datetime | None = None,
    ) -> BrainSessionSweepResult:
        """Tarir les sessions ouvertes — DEUX règles, UN statement, une préséance.

        Règle 7 j (toujours active) : toute session ouverte sans heartbeat
        depuis ``older_than`` part en ``abandoned``, avec sa raison.

        Règle 4 h (``close_inactive_after``, ``None`` = fermée) : une traçante
        ``nature = 'agent'`` dont l'OBSERVATION date de plus de ce seuil part en
        ``closed_inactive``, sans raison et **avec son ledger intact** — c'est
        toute la raison d'être de la 046. Une session ``operator``, et une
        session ``nature IS NULL`` d'avant la 046, restent hors d'atteinte : la
        résolution (d) refuse de juger rétroactivement.

        **PRÉSÉANCE : 7 j PRIME sur 4 h**, et ce n'est pas cosmétique. Une
        traçante inactive depuis plus de sept jours matche les DEUX prédicats.
        Le ``CASE`` teste la présence en PREMIER, donc elle part en ``abandoned``
        avec sa raison, jamais en ``closed_inactive`` muet. La règle est épinglée
        par un test, pas par ce paragraphe.

        **``last_observed_at IS NULL`` n'est JAMAIS pris par la règle des 4 h**
        (S3, tranché). ``NULL`` veut dire « jamais observée », pas « observée il
        y a longtemps » : c'est le régime des sessions d'avant l'auto-ouverture,
        et une comparaison SQL les laisserait déjà sortir — le prédicat explicite
        est là pour que l'intention se lise, et pour que le test la garde.

        **JAMAIS pendant un appel en vol** (garantie 1 du §0bis.3), et la
        machinerie n'est pas ici : ``observe()`` date la traçante AVANT que
        l'outil ne tourne, donc un appel en vol porte une observation vieille de
        quelques millisecondes. La garantie est structurelle et se lit à
        l'endroit qui la produit ; elle ne tient plus si un seul appel d'outil
        dépasse ``close_inactive_after``, ce qui n'existe pas au catalogue.

        Chemin SERVEUR uniquement : pas de garde ``expected_client_key``, parce
        qu'aucun client ne demande — c'est le serveur. L'amendement doctrinal du
        CLAUDE.md borne ce droit à ce seul chemin ; il n'ouvre rien pour l'agent
        ni pour le client, dont les sept commandes restent explicites.

        Ne touche ni ``project_contexts`` ni ``brain_session_artifacts`` : le
        focus et le ledger de capture survivent aux deux issues, comme pour un
        abandon manuel. Aucun CAS de focus n'est tenté — N fermetures groupées
        produiraient N−1 ``conflict`` fabriqués (`SPEC-M-G` §3.2).
        """
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise BrainSessionInputError("abandonment reason must not be blank")
        if older_than <= timedelta(0):
            raise BrainSessionInputError("older_than must be a positive interval")
        if close_inactive_after is not None and close_inactive_after <= timedelta(0):
            raise BrainSessionInputError("close_inactive_after must be a positive interval")

        reference = now or datetime.now(UTC)
        cutoff = reference - older_than
        inactive_cutoff = None if close_inactive_after is None else reference - close_inactive_after

        # Le prédicat de PRÉSENCE, isolé : il sert deux fois — à l'éligibilité et
        # à la préséance du CASE. Le dupliquer les ferait diverger en silence.
        is_stale = brain_sessions.c.last_heartbeat_at < cutoff
        open_stale = sa.and_(brain_sessions.c.status == "open", is_stale)

        if inactive_cutoff is None:
            eligible: Any = open_stale
            outcome: Any = sa.literal(BrainSessionStatus.ABANDONED.value)
            status_value: Any = BrainSessionStatus.ABANDONED.value
            reason_value: Any = normalized_reason
        else:
            eligible = sa.and_(
                brain_sessions.c.status == "open",
                sa.or_(
                    is_stale,
                    sa.and_(
                        brain_sessions.c.nature == "agent",
                        brain_sessions.c.last_observed_at.is_not(None),
                        brain_sessions.c.last_observed_at < inactive_cutoff,
                    ),
                ),
            )
            # Le `CASE` teste `is_stale` en PREMIER : c'est ICI que vit la
            # préséance 7 j > 4 h, dans du SQL exécuté, pas dans un commentaire.
            outcome = sa.case(
                (is_stale, sa.literal(BrainSessionStatus.ABANDONED.value)),
                else_=sa.literal(BrainSessionStatus.CLOSED_INACTIVE.value),
            )
            status_value = outcome
            # `closed_inactive` INTERDIT `abandonment_reason` (CHECK de la 046) :
            # le `CASE` n'est donc pas une commodité, c'est ce qui rend la ligne
            # acceptable par la base.
            reason_value = sa.case((is_stale, sa.literal(normalized_reason)), else_=sa.null())

        selection = (
            brain_sessions.c.id,
            brain_sessions.c.project_key,
            brain_sessions.c.client_key,
            brain_sessions.c.last_heartbeat_at,
            brain_sessions.c.last_observed_at,
        )

        if dry_run:
            statement: Any = sa.select(*selection, outcome.label("outcome")).where(eligible)
        else:
            # UN SEUL statement. Pas de SELECT puis UPDATE : sous READ
            # COMMITTED, PostgreSQL réévalue `eligible` sous le verrou de ligne,
            # donc un heartbeat qui commit pendant le balayage retire sa ligne
            # de l'update au lieu de perdre la course. C'est la réponse au
            # faux-mort du 2026-08-06 (session vivante abandonnée à tort), et
            # elle couvre la règle neuve sans une ligne de plus.
            statement = (
                brain_sessions.update()
                .where(eligible)
                .values(
                    status=status_value,
                    abandonment_reason=reason_value,
                    ended_at=reference,
                    updated_at=reference,
                )
                # `status` APRÈS l'écriture : RETURNING voit la ligne neuve, donc
                # le rapport lit l'issue réellement persistée, pas une issue
                # recalculée côté Python qui pourrait diverger du CASE.
                .returning(*selection, brain_sessions.c.status.label("outcome"))
            )

        async with self.transaction() as session:
            rows = (await session.execute(statement)).mappings().all()

        candidates = sorted(
            (BrainSessionSweepCandidate(**dict(row)) for row in rows),
            key=lambda candidate: candidate.last_heartbeat_at,
        )
        counted = [] if dry_run else candidates
        return BrainSessionSweepResult(
            candidates=candidates,
            dry_run=dry_run,
            cutoff=cutoff,
            inactive_cutoff=inactive_cutoff,
            abandoned_count=sum(1 for c in counted if c.outcome is BrainSessionStatus.ABANDONED),
            closed_inactive_count=sum(
                1 for c in counted if c.outcome is BrainSessionStatus.CLOSED_INACTIVE
            ),
        )

    async def _get_row(
        self,
        session: AsyncSession,
        session_id: UUID | str,
        *,
        for_update: bool = False,
    ) -> Row | None:
        stmt = sa.select(brain_sessions).where(brain_sessions.c.id == session_id)
        if for_update:
            stmt = stmt.with_for_update()
        row = (await session.execute(stmt)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def _get_by_client_key(
        self,
        session: AsyncSession,
        project_key: str,
        client_key: str,
    ) -> Row | None:
        stmt = sa.select(brain_sessions).where(
            brain_sessions.c.project_key == project_key,
            brain_sessions.c.client_key == client_key,
        )
        row = (await session.execute(stmt)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def _load_focus(
        self,
        session: AsyncSession,
        project_key: str,
        *,
        for_update: bool = False,
    ) -> Row | None:
        stmt = sa.select(
            project_contexts.c.current_focus,
            project_contexts.c.focus_revision,
        ).where(project_contexts.c.project_key == project_key)
        if for_update:
            stmt = stmt.with_for_update()
        row = (await session.execute(stmt)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def _count_open(self, session: AsyncSession, project_key: str) -> int:
        stmt = (
            sa.select(sa.func.count())
            .select_from(brain_sessions)
            .where(
                brain_sessions.c.project_key == project_key,
                brain_sessions.c.status == "open",
            )
        )
        return int((await session.execute(stmt)).scalar_one())

    async def _validate_captures(
        self,
        session: AsyncSession,
        brain_session: BrainSession,
        capture_ids: Sequence[UUID],
    ) -> dict[UUID, str]:
        found: dict[UUID, str] = {}
        ambiguous: set[UUID] = set()
        for table, knowledge_type in CAPTURE_TABLES:
            stmt = (
                sa.select(table.c.id)
                .where(
                    table.c.id.in_(capture_ids),
                    table.c.project_key == brain_session.project_key,
                    table.c.created_at >= brain_session.started_at,
                )
                .with_for_update(read=True, key_share=True)
            )
            for knowledge_id in (await session.execute(stmt)).scalars().all():
                if knowledge_id in found:
                    ambiguous.add(knowledge_id)
                found[knowledge_id] = knowledge_type
        invalid = set(capture_ids) - set(found)
        if invalid or ambiguous:
            rejected = sorted(str(item) for item in invalid | ambiguous)
            raise BrainSessionInputError(
                "Captured knowledge must exist in the same project and have been "
                "created during the session with an unambiguous type; invalid ids: "
                + ", ".join(rejected)
            )
        return found

    async def _load_artifact_rows(
        self,
        session: AsyncSession,
        knowledge_ids: Sequence[UUID],
    ) -> builtins.list[Row]:
        if not knowledge_ids:
            return []
        stmt = sa.select(brain_session_artifacts).where(
            brain_session_artifacts.c.knowledge_id.in_(knowledge_ids)
        )
        return [dict(row) for row in (await session.execute(stmt)).mappings().all()]

    async def _load_session_artifact_ids(
        self,
        session: AsyncSession,
        session_id: UUID,
    ) -> builtins.list[UUID]:
        stmt = (
            sa.select(brain_session_artifacts.c.knowledge_id)
            .where(brain_session_artifacts.c.session_id == session_id)
            .order_by(brain_session_artifacts.c.knowledge_id)
        )
        rows = (await session.execute(stmt)).mappings().all()
        return sorted((row["knowledge_id"] for row in rows), key=str)

    async def _load_artifact_ids_by_session(
        self,
        session: AsyncSession,
        session_ids: Sequence[UUID],
    ) -> dict[UUID, builtins.list[UUID]]:
        if not session_ids:
            return {}
        stmt = (
            sa.select(
                brain_session_artifacts.c.session_id,
                brain_session_artifacts.c.knowledge_id,
            )
            .where(brain_session_artifacts.c.session_id.in_(session_ids))
            .order_by(
                brain_session_artifacts.c.session_id,
                brain_session_artifacts.c.knowledge_id,
            )
        )
        grouped: dict[UUID, builtins.list[UUID]] = {session_id: [] for session_id in session_ids}
        for row in (await session.execute(stmt)).mappings().all():
            grouped[row["session_id"]].append(row["knowledge_id"])
        return grouped

    async def _apply_focus_if_current(
        self,
        session: AsyncSession,
        current: Row,
        project_key: str,
        next_focus: str,
        expected_revision: int,
    ) -> tuple[Row, BrainSessionFocusOutcome]:
        if current["focus_revision"] != expected_revision:
            return current, BrainSessionFocusOutcome.CONFLICT

        stmt = (
            project_contexts.update()
            .where(project_contexts.c.project_key == project_key)
            .values(
                current_focus=next_focus,
                focus_revision=expected_revision + 1,
                # A session close rewrites the whole blob. Re-posting the
                # previous prose verbatim is the copy-forward this column
                # exists to expose, so it must not reset the age.
                focus_updated_at=focus_stamp(next_focus),
                updated_at=datetime.now(UTC),
            )
            .returning(
                project_contexts.c.current_focus,
                project_contexts.c.focus_revision,
            )
        )
        row = (await session.execute(stmt)).mappings().one_or_none()
        if row is None:
            raise BrainSessionStateError(f"Project {project_key!r} focus could not be updated")
        return dict(row), BrainSessionFocusOutcome.APPLIED

    async def _mark_ended(
        self,
        session: AsyncSession,
        session_id: UUID,
        summary: str,
        next_focus: str,
        capture_ids: Sequence[UUID],
        nothing_reason: str | None,
        *,
        expected_focus_revision: int,
        focus_outcome: BrainSessionFocusOutcome,
        focus_at_end: str | None,
        focus_revision_at_end: int,
    ) -> BrainSession:
        now = datetime.now(UTC)
        stmt = (
            brain_sessions.update()
            .where(brain_sessions.c.id == session_id)
            .values(
                status="ended",
                summary=summary,
                next_focus=next_focus,
                captured_knowledge_ids=list(capture_ids),
                nothing_to_capture_reason=nothing_reason,
                end_expected_focus_revision=expected_focus_revision,
                focus_outcome=focus_outcome.value,
                focus_at_end=focus_at_end,
                focus_revision_at_end=focus_revision_at_end,
                ended_at=now,
                updated_at=now,
            )
            .returning(brain_sessions)
        )
        row = (await session.execute(stmt)).mappings().one_or_none()
        if row is None:
            raise BrainSessionStateError(f"Session {session_id} could not be ended")
        return self._to_model(row)

    async def _replay_end(
        self,
        session: AsyncSession,
        model: BrainSession,
        summary: str,
        next_focus: str,
        expected_focus_revision: int,
        nothing_reason: str | None,
    ) -> BrainSessionEndResult:
        exact = (
            model.status == "ended"
            and model.summary == summary
            and model.next_focus == next_focus
            and model.nothing_to_capture_reason == nothing_reason
            and (
                model.end_expected_focus_revision is None
                or model.end_expected_focus_revision == expected_focus_revision
            )
        )
        if not exact:
            raise BrainSessionTerminalConflictError(
                f"Session {model.id} already has a different terminal payload"
            )

        focus = await self._load_focus(session, model.project_key)
        if focus is None:
            raise BrainSessionNotFoundError(f"Project {model.project_key!r} was not found")
        if model.focus_outcome is None:
            raise BrainSessionStateError(f"Session {model.id} has no persisted focus outcome")
        remaining = await self._count_open(session, model.project_key)
        return BrainSessionEndResult(
            session=model,
            replayed=True,
            remaining_open_session_count=remaining,
            current_focus=focus["current_focus"],
            current_focus_revision=focus["focus_revision"],
            focus_outcome=model.focus_outcome,
            focus_at_end=model.focus_at_end,
            focus_revision_at_end=model.focus_revision_at_end,
        )

    @staticmethod
    def _to_model(
        row: Any,
        *,
        now: datetime | None = None,
        attributed_ids: Sequence[UUID] | None = None,
    ) -> BrainSession:
        payload = dict(row)
        heartbeat = payload.get("last_heartbeat_at") or payload.get("updated_at")
        reference = now or datetime.now(UTC)
        payload["is_stale"] = bool(
            payload.get("status") == "open"
            and heartbeat is not None
            and heartbeat <= reference - SESSION_STALE_AFTER
        )
        payload["attributed_knowledge_ids"] = sorted(
            attributed_ids
            if attributed_ids is not None
            else payload.get("captured_knowledge_ids") or [],
            key=str,
        )
        return BrainSession.model_validate(payload)

    @staticmethod
    def _assert_identity(model: BrainSession, expected_client_key: str) -> None:
        normalized = expected_client_key.strip()
        if not normalized or not compare_digest(
            model.client_key.encode("utf-8"),
            normalized.encode("utf-8"),
        ):
            raise BrainSessionIdentityConflictError("session_id does not match expected_client_key")

    @staticmethod
    def _owned_capture_ids(existing: Sequence[Row], session_id: UUID) -> set[UUID]:
        conflicts = [artifact for artifact in existing if artifact["session_id"] != session_id]
        if conflicts:
            conflict_ids = sorted(str(item["knowledge_id"]) for item in conflicts)
            raise BrainSessionCaptureConflictError(
                "Knowledge is already attributed to another session: " + ", ".join(conflict_ids)
            )
        return {artifact["knowledge_id"] for artifact in existing}

    @staticmethod
    def _normalize_optional(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @staticmethod
    def _validate_end_input(
        summary: str,
        next_focus: str,
        expected_revision: int,
    ) -> None:
        if not summary or not next_focus:
            raise BrainSessionInputError("summary and next_focus must not be blank")
        if expected_revision < 0:
            raise BrainSessionInputError("expected_focus_revision must be non-negative")

    @staticmethod
    def _validate_capture_ids(
        capture_ids: Sequence[UUID],
        *,
        require_nonempty: bool,
    ) -> None:
        if require_nonempty and not capture_ids:
            raise BrainSessionInputError("knowledge_ids must not be empty")
        if len(capture_ids) > MAX_CAPTURED_KNOWLEDGE_IDS:
            raise BrainSessionInputError(
                f"captured_knowledge_ids must contain at most {MAX_CAPTURED_KNOWLEDGE_IDS} items"
            )
        if len(set(capture_ids)) != len(capture_ids):
            raise BrainSessionInputError("captured_knowledge_ids must be unique")

    @staticmethod
    def _validate_capture_outcome(
        capture_ids: Sequence[UUID],
        nothing_reason: str | None,
    ) -> None:
        if bool(capture_ids) == bool(nothing_reason):
            raise BrainSessionInputError(
                "Provide captured_knowledge_ids or nothing_to_capture_reason, exclusively"
            )
