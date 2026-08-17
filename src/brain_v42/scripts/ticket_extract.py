"""Ticket knowledge extraction — proposer-only dream step (spec §6).

Scanne les tickets en état terminal (extraction_status='pending'), envoie
chaque fil au LLM (NVIDIA API, JSON strict SANS tools — pattern validé du
domain backfill), stocke des proposals reviewables, applique en wet.

Usage:
    python -m scripts.ticket_extract [--limit 20]          # propose (dry)
    python -m scripts.ticket_extract --limit 20 --wet      # propose + apply du run
    python -m scripts.ticket_extract --apply-ids "3,4"     # override humain reviewé
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import httpx
import sqlalchemy as sa
import structlog

from brain_v42.db.tables import MIN_COMPARABLE_EMBEDDING_NORM
from brain_v42.dream_run_project_key import GLOBAL_PHASE_PROJECT_KEY
from brain_v42.scripts.domain_backfill import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ModelGoneError,
    ResponseParseError,
    _exc_str,
    _post_chat,
    _strip_fences,
    load_env_file,
)

_ENV_FILE = Path.home() / ".config" / "brain-v42" / "nvidia.env"
_API_KEY_VAR = "BRAIN_NVIDIA_API_KEY"
_FALLBACK_MODEL_VAR = "BRAIN_NVIDIA_FALLBACK_MODEL"
#: Secours quand le primaire est retiré chez le fournisseur.
#:
#: PROUVÉ VIVANT, PAS PROUVÉ BON POUR CE PROMPT — la distinction a déjà coûté
#: une nuit ici : un modèle choisi sur une sonde de 16 tokens s'est révélé en
#: TIMEOUT sur le prompt réel (canary 2026-08-05). Celui-ci sert déjà de
#: primaire WET à `roadmap_curate`, donc il tient un prompt de production, mais
#: il n'a JAMAIS été canaryé sur le prompt d'extraction. À canaryer avant de le
#: promouvoir primaire ; en secours il reste strictement meilleur que rien,
#: puisque l'alternative mesurée est 20 tickets perdus en 0,9 s.
DEFAULT_EXTRACT_FALLBACK_MODEL = "meta/llama-3.3-70b-instruct"

_VALID_TARGET_TYPES = ("learning", "decision")
_CORPUS_DEDUP_THRESHOLD = 0.85
_MIN_COMPARABLE_NORM = MIN_COMPARABLE_EMBEDDING_NORM
_PAYLOAD_KEYS = {
    "learning": ("topic", "insight", "tags"),
    "decision": ("title", "description", "reasoning", "tags"),
}

_SYSTEM_PROMPT = (
    "Tu extrais de la connaissance durable depuis des tickets de coordination "
    "résolus entre projets d'un même écosystème. Tu réponds UNIQUEMENT avec un "
    "tableau JSON valide (éventuellement vide []) — pas de prose, pas de "
    'markdown. Chaque élément: {"target_type": "learning"|"decision", '
    '"target_project": "<un des deux projets du ticket>", "payload": '
    '{...}, "rationale": "pourquoi c\'est durable"}. '
    'payload learning: {"topic": str<=200, "insight": str, "tags": [str]}. '
    'payload decision: {"title": str<=200, "description": str, '
    '"reasoning": str, "tags": [str]}. '
    "N'extrais QUE les insights durables/réutilisables (gotchas, contrats "
    "d'API, choix argumentés). Un simple « fait/déployé/ok merci » → []."
)
_REPROMPT_INSTRUCTION = (
    "Ta réponse précédente n'était pas un tableau JSON valide selon le format "
    "demandé. Renvoie UNIQUEMENT le tableau JSON corrigé."
)
_DEFAULT_RUN_BUDGET_SECONDS = 540
_DEFAULT_TICKET_BUDGET_SECONDS = 180
_FINALIZATION_RESERVE_SECONDS = 120
# Floor on the slice a ticket can REALLY use (`remaining - reserve`), not on
# its nominal budget. Requiring the nominal budget to fit whole made the last
# `ticket_budget + reserve` seconds of every window unable to start any work
# — 300s of 540s, measured 2026-08-07 — while the truncating `min()` below
# stood by, its second branch structurally unreachable. Under a minute a
# model call is waste that only manufactures one more timeout, so that is
# where the gate closes now. No CLI flag: dream.sh has no use for the button.
_MIN_TICKET_SLICE_SECONDS = 60
# Withheld from the gate on top of the reserve when `--wet` is on. The reserve
# is dimensioned for `record_dream_run` alone; nothing used to withhold time
# for applying the proposals the loop had just earned, so a loop free to run
# until `remaining` met the reserve exactly landed the wet block on
# `remaining == reserve` and applied NOTHING — the phase trading its actual
# output for extra scanned tickets. Measured on four production nights
# (2026-08-03..07 extract logs): applying up to 16 proposals took at most 2s of
# wall clock, so 30s is a 15x provision that still leaves the gate open through
# the 330th second of a 540s window (against the 240th before the slice work).
_WET_APPLY_ALLOWANCE_SECONDS = 30.0
# An apply started with a sliver of budget is cancelled mid-flight by
# `asyncio.wait_for` and billed as a timeout: a healthy write reported as a
# failure, and exit 3 instead of exit 4. Below this, decline to start.
_MIN_WET_APPLY_SECONDS = 5.0
_SECRET_PATTERN = re.compile(r"(?i)(bearer\s+|api[_-]?key[=:]\s*)(\S+)")

logger = structlog.get_logger(__name__)


@dataclass
class TicketThread:
    id: UUID
    kind: str
    title: str
    body: str
    from_project: str
    to_project: str
    status: str
    # (author_project, body, status_to|None, created_at)
    messages: list[tuple[str, str, str | None, datetime]] = field(default_factory=list)


@dataclass
class ProposalDraft:
    ticket_id: UUID
    target_type: str
    target_project: str
    payload: dict[str, Any]
    rationale: str


@dataclass(frozen=True)
class CorpusDuplicate:
    draft: ProposalDraft
    entity_type: str
    entity_id: UUID
    label: str
    similarity: float
    match_source: Literal["corpus", "run"] = "corpus"


@dataclass
class DedupResult:
    kept: list[ProposalDraft] = field(default_factory=list)
    duplicates: list[CorpusDuplicate] = field(default_factory=list)


class CorpusDedupUnavailable(RuntimeError):
    """The corpus gate could not prove that proposals are novel."""


def _draft_embedding_text(draft: ProposalDraft) -> str:
    from brain_v42.services.embedding_text import (  # noqa: PLC0415
        decision_embedding_text,
        learning_embedding_text,
    )

    if draft.target_type == "learning":
        return str(
            learning_embedding_text(
                str(draft.payload["topic"]),
                str(draft.payload["insight"]),
            )
        )
    return str(
        decision_embedding_text(
            str(draft.payload["title"]),
            str(draft.payload["description"]),
            str(draft.payload["reasoning"]),
        )
    )


def _draft_label(draft: ProposalDraft) -> str:
    key = "topic" if draft.target_type == "learning" else "title"
    return str(draft.payload[key])


def _live_corpus_filters(table: Any) -> tuple[Any, ...]:
    filters: tuple[Any, ...] = (
        table.c.merged_into.is_(None),
        sa.or_(
            table.c.freshness_status.is_(None),
            table.c.freshness_status != "archived",
        ),
    )
    # Dream alerts are operational incidents, not comparable knowledge. They
    # are backfilled on their own cadence and must not make the novelty gate
    # accept unverified learnings/decisions less defensively.
    if table.name == "learnings":
        return (
            *filters,
            sa.or_(
                table.c.source.is_(None),
                table.c.source != "dream_post_run_alert",
            ),
        )
    return filters


def _embedding_unusable(table: Any) -> Any:
    return sa.or_(
        table.c.embedding.is_(None),
        sa.func.vector_norm(table.c.embedding) <= _MIN_COMPARABLE_NORM,
    )


def _corpus_backlog_stmt(project_key: str) -> Any:
    from brain_v42.db.tables import decisions, learnings  # noqa: PLC0415

    learning_missing = sa.exists(
        sa.select(learnings.c.id).where(
            learnings.c.project_key == project_key,
            _embedding_unusable(learnings),
            *_live_corpus_filters(learnings),
        )
    )
    decision_missing = sa.exists(
        sa.select(decisions.c.id).where(
            decisions.c.project_key == project_key,
            _embedding_unusable(decisions),
            decisions.c.status == "active",
            decisions.c.superseded_by.is_(None),
            *_live_corpus_filters(decisions),
        )
    )
    return sa.select(
        learning_missing.label("missing_learning"),
        decision_missing.label("missing_decision"),
    )


def _corpus_match_stmt(
    draft: ProposalDraft,
    embedding: list[float],
    threshold: float,
) -> Any:
    from brain_v42.db.tables import decisions, learnings  # noqa: PLC0415

    def candidate_select(
        table: Any,
        entity_type: str,
        label: Any,
        *extra_clauses: Any,
    ) -> Any:
        similarity = (sa.literal(1.0) - table.c.embedding.cosine_distance(embedding)).label(
            "similarity"
        )
        return sa.select(
            sa.literal(entity_type).label("entity_type"),
            table.c.id.label("entity_id"),
            label.label("label"),
            similarity,
        ).where(
            table.c.project_key == draft.target_project,
            table.c.embedding.is_not(None),
            *_live_corpus_filters(table),
            *extra_clauses,
        )

    # Exact scan is intentional: an approximate HNSW lookup can miss a duplicate.
    # Project scoping bounds the DRY-soak cost while novelty remains fail-closed.
    candidates = sa.union_all(
        candidate_select(learnings, "learning", learnings.c.topic),
        candidate_select(
            decisions,
            "decision",
            decisions.c.title,
            decisions.c.status == "active",
            decisions.c.superseded_by.is_(None),
        ),
    ).subquery("ticket_extract_corpus_candidates")
    return (
        sa.select(
            candidates.c.entity_type,
            candidates.c.entity_id,
            candidates.c.label,
            candidates.c.similarity,
        )
        .where(candidates.c.similarity >= threshold)
        .order_by(candidates.c.similarity.desc())
        .limit(1)
    )


async def deduplicate_drafts(
    session_factory: Any,
    embedding_svc: Any,
    drafts: list[ProposalDraft],
    threshold: float = _CORPUS_DEDUP_THRESHOLD,
) -> DedupResult:
    """Drop same-project near-duplicates already present in durable knowledge.

    Both learnings and active decisions are searched regardless of the proposed
    target type, then accepted drafts are compared within the current run. Any
    missing corpus vector, embedding failure, or corpus-query failure aborts the
    whole gate so EXTRACT cannot persist or auto-apply unchecked proposals.
    """
    if not drafts:
        return DedupResult()

    from brain_v42.db.tables import _EMBEDDING_DIM  # noqa: PLC0415

    projects = list(dict.fromkeys(draft.target_project for draft in drafts))
    try:
        async with session_factory() as session:
            for project_key in projects:
                backlog = (await session.execute(_corpus_backlog_stmt(project_key))).one()
                missing_types = [
                    entity_type
                    for entity_type, missing in (
                        ("learning", backlog.missing_learning),
                        ("decision", backlog.missing_decision),
                    )
                    if bool(missing)
                ]
                if missing_types:
                    raise CorpusDedupUnavailable(
                        "corpus embedding backlog for project "
                        f"{project_key}: active {', '.join(missing_types)} rows "
                        "lack comparable embeddings"
                    )
    except CorpusDedupUnavailable:
        raise
    except Exception as exc:
        raise CorpusDedupUnavailable(f"corpus query unavailable: {type(exc).__name__}") from exc

    vectors: list[list[float]] = []
    norms: list[float] = []
    try:
        for draft in drafts:
            vector = await embedding_svc.embed(_draft_embedding_text(draft))
            if (
                not isinstance(vector, list)
                or len(vector) != _EMBEDDING_DIM
                or not all(
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    for value in vector
                )
            ):
                observed = len(vector) if isinstance(vector, list) else "non-list"
                raise CorpusDedupUnavailable(
                    f"embedding dimension/content invalid: expected {_EMBEDDING_DIM}, "
                    f"observed {observed}"
                )
            numeric_vector = [float(value) for value in vector]
            norm_sq = math.fsum(value * value for value in numeric_vector)
            if not math.isfinite(norm_sq):
                raise CorpusDedupUnavailable("embedding norm invalid: non-finite")
            if norm_sq <= _MIN_COMPARABLE_NORM**2:
                raise CorpusDedupUnavailable("embedding zero norm is not comparable")
            vectors.append(numeric_vector)
            norms.append(math.sqrt(norm_sq))
    except CorpusDedupUnavailable:
        raise
    except Exception as exc:
        raise CorpusDedupUnavailable(f"embedding unavailable: {type(exc).__name__}") from exc

    result = DedupResult()
    accepted: list[tuple[ProposalDraft, list[float], float]] = []
    try:
        async with session_factory() as session:
            for draft, vector, norm in zip(drafts, vectors, norms, strict=True):
                row = (await session.execute(_corpus_match_stmt(draft, vector, threshold))).first()
                if row is not None and float(row.similarity) >= threshold:
                    result.duplicates.append(
                        CorpusDuplicate(
                            draft=draft,
                            entity_type=str(row.entity_type),
                            entity_id=row.entity_id,
                            label=str(row.label),
                            similarity=float(row.similarity),
                        )
                    )
                    continue

                run_matches = [
                    (
                        previous,
                        math.fsum(
                            current * prior
                            for current, prior in zip(vector, previous_vector, strict=True)
                        )
                        / (norm * previous_norm),
                    )
                    for previous, previous_vector, previous_norm in accepted
                    if previous.target_project == draft.target_project
                ]
                best_run_match = max(run_matches, key=lambda item: item[1], default=None)
                if best_run_match is not None and best_run_match[1] >= threshold:
                    previous, similarity = best_run_match
                    result.duplicates.append(
                        CorpusDuplicate(
                            draft=draft,
                            entity_type=previous.target_type,
                            entity_id=previous.ticket_id,
                            label=_draft_label(previous),
                            similarity=similarity,
                            match_source="run",
                        )
                    )
                    continue

                result.kept.append(draft)
                accepted.append((draft, vector, norm))
    except Exception as exc:
        raise CorpusDedupUnavailable(f"corpus query unavailable: {type(exc).__name__}") from exc
    return result


def render_thread(thread: TicketThread) -> str:
    lines = [
        f"Ticket [{thread.kind}] {thread.from_project} → {thread.to_project} "
        f"(status final: {thread.status})",
        f"Titre: {thread.title}",
        f"Demande initiale: {thread.body}",
    ]
    if thread.messages:
        lines.append("Fil:")
        for author, body, status_to, created_at in thread.messages:
            suffix = f" [→ {status_to}]" if status_to else ""
            lines.append(f"- ({created_at.date().isoformat()}) {author}: {body}{suffix}")
    return "\n".join(lines)


def build_messages(thread: TicketThread) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": render_thread(thread)},
    ]


def parse_and_validate(content: str, thread: TicketThread) -> list[ProposalDraft]:
    try:
        data = json.loads(_strip_fences(content))
    except json.JSONDecodeError as exc:
        raise ResponseParseError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ResponseParseError(f"expected a JSON array, got {type(data).__name__}")
    participants = {thread.from_project, thread.to_project}
    drafts: list[ProposalDraft] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ResponseParseError(f"item {i}: expected object")
        ttype = item.get("target_type")
        if ttype not in _VALID_TARGET_TYPES:
            raise ResponseParseError(
                f"item {i}: invalid target_type {ttype!r} (valid: {_VALID_TARGET_TYPES})"
            )
        tproject = item.get("target_project")
        if tproject not in participants:
            raise ResponseParseError(
                f"item {i}: target_project {tproject!r} not in {sorted(participants)}"
            )
        payload = item.get("payload")
        required = _PAYLOAD_KEYS[ttype]
        if not isinstance(payload, dict) or any(k not in payload for k in required):
            raise ResponseParseError(f"item {i}: payload must contain {required}")
        # Truncate forgivingly to model limits.
        for key in ("topic", "title"):
            if key in payload and isinstance(payload[key], str):
                payload[key] = payload[key][:200]
        drafts.append(
            ProposalDraft(
                ticket_id=thread.id,
                target_type=ttype,
                target_project=tproject,
                payload=payload,
                rationale=str(item.get("rationale", "")),
            )
        )
    return drafts


# ── I/O — DB + LLM ───────────────────────────────────────────────────────────


async def fetch_pending_threads(
    session_factory: Any,
    limit: int,
) -> list[TicketThread]:
    """Fetch tickets WHERE extraction_status='pending', plus their messages."""
    from brain_v42.db.tables import ticket_messages, tickets  # noqa: PLC0415

    async with session_factory() as session:
        stmt = (
            sa.select(tickets)
            .where(tickets.c.extraction_status == "pending")
            .order_by(tickets.c.closed_at.asc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).mappings().all()
        if not rows:
            return []

        ticket_ids = [r["id"] for r in rows]
        msg_stmt = (
            sa.select(ticket_messages)
            .where(ticket_messages.c.ticket_id.in_(ticket_ids))
            .order_by(ticket_messages.c.created_at.asc())
        )
        msg_rows = (await session.execute(msg_stmt)).mappings().all()

        msgs_by_ticket: dict[Any, list[tuple[str, str, str | None, datetime]]] = {}
        for m in msg_rows:
            msgs_by_ticket.setdefault(m["ticket_id"], []).append(
                (m["author_project"], m["body"], m["status_to"], m["created_at"])
            )

        threads: list[TicketThread] = []
        for r in rows:
            threads.append(
                TicketThread(
                    id=r["id"],
                    kind=r["kind"],
                    title=r["title"],
                    body=r["body"],
                    from_project=r["from_project"],
                    to_project=r["to_project"],
                    status=r["status"],
                    messages=msgs_by_ticket.get(r["id"], []),
                )
            )
        return threads


@dataclass
class ThreadOutcome:
    thread: TicketThread
    drafts: list[ProposalDraft]
    failed: bool = False
    error: str | None = None


async def extract_thread(
    client: httpx.AsyncClient,
    model: str,
    thread: TicketThread,
    sleep: Any = asyncio.sleep,
) -> ThreadOutcome:
    """Call LLM once; on parse error, one corrective re-prompt; fail → outcome failed."""
    messages = build_messages(thread)
    try:
        content, _usage = await _post_chat(client, model, messages, sleep)
        try:
            drafts = parse_and_validate(content, thread)
        except ResponseParseError:
            # One corrective re-prompt.
            corrective = [
                *messages,
                {"role": "assistant", "content": content},
                {"role": "user", "content": _REPROMPT_INSTRUCTION},
            ]
            content2, _usage2 = await _post_chat(client, model, corrective, sleep)
            try:
                drafts = parse_and_validate(content2, thread)
            except ResponseParseError as exc:
                return ThreadOutcome(
                    thread=thread,
                    drafts=[],
                    failed=True,
                    error=f"unparseable after corrective re-prompt: {exc}",
                )
    except ModelGoneError:
        # NE PAS enterrer dans un outcome `failed` : un modèle retiré n'est pas
        # un ticket fautif. Confondus, ils rendent vingt échecs identiques que
        # personne ne peut relier à leur cause unique — et la boucle perd la
        # seule information qui lui permettrait de basculer sur le secours.
        raise
    except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as exc:
        return ThreadOutcome(thread=thread, drafts=[], failed=True, error=_exc_str(exc))
    return ThreadOutcome(thread=thread, drafts=drafts)


def _safe_error(value: str | None) -> str | None:
    """Return a single-line, bounded operational cause without credentials."""
    if not value:
        return None
    compact = " ".join(value.split())
    return _SECRET_PATTERN.sub(r"\1[redacted]", compact)[:240]


async def _extract_thread_with_budget(
    client: httpx.AsyncClient,
    model: str,
    thread: TicketThread,
    *,
    timeout_seconds: float,
    extract: Any | None = None,
) -> ThreadOutcome:
    """Bound one ticket so the caller can checkpoint before the run deadline."""
    extraction = extract or extract_thread
    try:
        return await asyncio.wait_for(extraction(client, model, thread), timeout=timeout_seconds)
    except TimeoutError:
        return ThreadOutcome(
            thread=thread,
            drafts=[],
            failed=True,
            error=f"ticket timeout after {timeout_seconds:g}s",
        )


async def persist_proposals(
    session_factory: Any,
    thread: TicketThread,
    drafts: list[ProposalDraft],
) -> list[int]:
    """Atomically claim a pending ticket, then persist its proposals."""
    from brain_v42.db.tables import ticket_extraction_proposals, tickets  # noqa: PLC0415

    async with session_factory() as session:
        async with session.begin():
            current_status = (
                await session.execute(
                    sa.select(tickets.c.extraction_status)
                    .where(tickets.c.id == thread.id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if current_status != "pending":
                return []

            ids: list[int] = []
            for draft in drafts:
                stmt = (
                    ticket_extraction_proposals.insert()
                    .values(
                        ticket_id=draft.ticket_id,
                        target_type=draft.target_type,
                        target_project=draft.target_project,
                        payload=draft.payload,
                        rationale=draft.rationale,
                        status="proposed",
                    )
                    .returning(ticket_extraction_proposals.c.id)
                )
                row = (await session.execute(stmt)).scalar_one()
                ids.append(row)

            new_status = "proposed" if drafts else "skipped"
            await session.execute(
                tickets.update()
                .where(tickets.c.id == thread.id)
                .values(extraction_status=new_status, updated_at=sa.func.now())
            )
            return ids


async def _build_apply_services() -> tuple[Any, Any]:
    """Build LearningService + DecisionService with best-effort embedding.

    project_context_repo wires the fail-closed project-existence guard on
    create() (see brain_v42.services.project_guard). The proposal-service
    atomic apply path passes its own session into create(), so the guard
    reuses it instead of opening a second connection.
    """
    from brain_v42.config import get_settings  # noqa: PLC0415
    from brain_v42.db.engine import get_session_factory  # noqa: PLC0415
    from brain_v42.repositories.pg_decision import PgDecisionRepo  # noqa: PLC0415
    from brain_v42.repositories.pg_learning import PgLearningRepo  # noqa: PLC0415
    from brain_v42.repositories.pg_project_context import PgProjectContextRepo  # noqa: PLC0415
    from brain_v42.services.decision_service import DecisionService  # noqa: PLC0415
    from brain_v42.services.gpu_embedding_service import GPUEmbeddingService  # noqa: PLC0415
    from brain_v42.services.learning_service import LearningService  # noqa: PLC0415

    sf = get_session_factory()
    embedding_svc: Any = GPUEmbeddingService(base_url=get_settings().embedding_service_url)
    try:
        await embedding_svc.embed("ping")
    except Exception:
        print(
            "! embedding service unreachable — creating entities without vectors "
            "(run scripts.regen_embeddings later)"
        )
        embedding_svc = None

    project_context_repo = PgProjectContextRepo(sf)
    learning_svc = LearningService(
        pg_repo=PgLearningRepo(sf),
        embedding_svc=embedding_svc,
        project_context_repo=project_context_repo,
    )
    decision_svc = DecisionService(
        repo=PgDecisionRepo(sf),
        embedding_svc=embedding_svc,
        project_context_repo=project_context_repo,
    )
    return learning_svc, decision_svc


async def apply_proposals(
    session_factory: Any,
    proposal_ids: list[int],
) -> tuple[int, int]:
    """CLI facade applying proposals by id. Returns (applied_count, entity_count)."""
    from brain_v42.services.proposal_service import (  # noqa: PLC0415
        ProposalApplyError,
        ProposalNotFoundError,
        ProposalNotProposedError,
        ProposalService,
    )

    learning_svc, decision_svc = await _build_apply_services()
    service = ProposalService(session_factory, learning_svc, decision_svc)
    applied = 0
    entities_created = 0
    for proposal_id in dict.fromkeys(proposal_ids):
        try:
            await service.apply_ticket_extraction(proposal_id)
        except (ProposalNotFoundError, ProposalNotProposedError):
            continue
        except ProposalApplyError as exc:
            cause = exc.__cause__ or exc
            print(f"! failed to create entity for proposal {proposal_id}: {cause}")
            continue
        applied += 1
        entities_created += 1
    return applied, entities_created


def _exit_code(*, timed_out: int, any_failed: bool, deferred: int, hard_failed: int = 0) -> int:
    """Map a run outcome to an exit code the caller can act on.

    `3` is reserved for work that actually exceeded a deadline. A deferral is
    the opposite: the budget declined to *start* work it could not finish, so
    nothing was cut short and nothing was wasted. Collapsing the two made
    dream.sh report TIMEOUT and the systemd unit fail every single night on
    designed behaviour — and an alarm that fires every night is not an alarm.

    `4` keeps the deferral visible without claiming a failure. Callers that do
    not know the code are unaffected: they still see non-zero only when the run
    left work owed.

    `hard_failed` is checked FIRST, and that order is the whole point.
    `any_failed` is also true for timeouts, so it cannot answer "was anything
    broken besides the clock?". As of 2026-08-07 dream.sh reads `3` as a bounded
    deadline and leaves the systemd unit green, so a `3` that also covered a
    hard failure would bury an outage. That is not hypothetical: on the
    2026-08-02 run a dedup outage and a ticket deadline coincided, and the run
    reported `3`.
    """
    if hard_failed:
        return 1
    if timed_out:
        return 3
    if any_failed:
        return 1
    return 4 if deferred else 0


async def record_dream_run(
    session_factory: Any,
    status: str,
    dry: bool,
    duration_s: float,
    error: str | None,
) -> None:
    """INSERT dream_runs row for phase='extract'. Best-effort — never raises.

    `extract` is a GLOBAL phase: it leaves the project loop and runs once a
    night, for nobody in particular. It therefore writes the sentinel, not a
    real key — and the sentinel enters as a bound parameter held by one shared
    constant, never through `canonicalize_project_key`, which rejects it.
    """
    try:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    sa.text(
                        "INSERT INTO dream_runs "
                        "(run_date, phase, status, duration_s, error_message, "
                        "project_key, phase_dry_run) "
                        "VALUES (:run_date, 'extract', :status, :duration_s, "
                        ":error_message, :project_key, :phase_dry_run)"
                    ),
                    {
                        "run_date": date.today(),
                        "status": status,
                        "duration_s": duration_s,
                        "error_message": _safe_error(error),
                        "project_key": GLOBAL_PHASE_PROJECT_KEY,
                        "phase_dry_run": dry,
                    },
                )
    except Exception as exc:
        print(f"! warning: could not record dream_run: {exc}")


async def record_ticket_attempt(
    session_factory: Any,
    thread: TicketThread,
    status: str,
    duration_s: float,
    error: str | None,
) -> None:
    """Persist one terminal ticket attempt; failures must remain resumable."""
    try:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    sa.text(
                        "INSERT INTO ticket_extraction_attempts "
                        "(ticket_id, run_date, status, duration_s, error_message) "
                        "VALUES (:ticket_id, :run_date, :status, :duration_s, :error_message)"
                    ),
                    {
                        "ticket_id": thread.id,
                        "run_date": date.today(),
                        "status": status,
                        "duration_s": duration_s,
                        "error_message": _safe_error(error),
                    },
                )
    except Exception as exc:
        print(f"! warning: could not record ticket attempt {thread.id}: {type(exc).__name__}")


async def _within_deadline(awaitable: Any, deadline: float) -> Any:
    """Await one ticket operation without spending the reserved finalization time."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return await asyncio.wait_for(awaitable, timeout=remaining)


def _log_ticket_timing(
    thread: TicketThread,
    *,
    position: int,
    status: str,
    phase: str,
    ticket_started: float,
    extraction_duration_s: float,
    remaining_before_s: float,
    ticket_slice_s: float,
    ticket_budget_s: float,
) -> None:
    """Emit one structured timing event per terminal ticket outcome.

    Instrumentation only (ticket 572220e9 criterion #1): `extraction_duration_s`
    is the LLM-call-only latency already persisted in `ticket_extraction_attempts`
    (duration_s); `ticket_elapsed_s` additionally covers dedup/persist so the two
    together show where a ticket's time actually went, without changing what is
    written to the DB. `phase` names where a failure happened: "extraction",
    "dedup", "persist" or "done".

    `ticket_slice_s`/`budget_mode` ride on this existing per-ticket event
    rather than on one of their own: the slice granted only means something
    next to the outcome it produced ("do reduced tickets time out more?"),
    and a second event would double the volume for a value that must be
    joined back on `ticket_id` anyway.
    """
    logger.info(
        "extract_ticket_timing",
        ticket_id=str(thread.id),
        position=position,
        status=status,
        phase=phase,
        extraction_duration_s=round(extraction_duration_s, 3),
        ticket_elapsed_s=round(time.monotonic() - ticket_started, 3),
        remaining_before_s=round(remaining_before_s, 3),
        ticket_slice_s=round(ticket_slice_s, 3),
        budget_mode="reduced" if ticket_slice_s < ticket_budget_s else "nominal",
    )


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"doit être >= 1 (reçu : {number})")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="ticket_extract",
        description="Proposer-only ticket knowledge extraction (NVIDIA API).",
    )
    parser.add_argument("--limit", type=_positive_int, default=20, help="max tickets à traiter")
    parser.add_argument(
        "--run-budget-seconds",
        type=_positive_int,
        default=_DEFAULT_RUN_BUDGET_SECONDS,
        help="budget total auto-contrôlé, inférieur au garde-fou systemd",
    )
    parser.add_argument(
        "--ticket-budget-seconds",
        type=_positive_int,
        default=_DEFAULT_TICKET_BUDGET_SECONDS,
        help="budget maximum d'un ticket, retry et re-prompt inclus",
    )
    parser.add_argument(
        "--wet",
        action="store_true",
        help="propose puis applique les proposals de ce run",
    )
    parser.add_argument(
        "--apply-ids",
        default=None,
        help=(
            'override humain: apply des proposals reviewées (ex: "3,4") sans '
            "rejouer le gate corpus — incompatible avec --wet"
        ),
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"défaut: env BRAIN_NVIDIA_MODEL puis {DEFAULT_MODEL}",
    )
    parser.add_argument(
        "--fallback-model",
        default=None,
        help=(
            f"modèle de secours si le primaire est retiré (404/410) ; "
            f"défaut: env {_FALLBACK_MODEL_VAR} puis {DEFAULT_EXTRACT_FALLBACK_MODEL}"
        ),
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"défaut: env BRAIN_NVIDIA_BASE_URL puis {DEFAULT_BASE_URL}",
    )
    args = parser.parse_args()

    if args.wet and args.apply_ids is not None:
        parser.error("--wet et --apply-ids sont incompatibles")

    load_env_file(_ENV_FILE)

    import os  # noqa: PLC0415

    api_key = os.environ.get(_API_KEY_VAR, "")
    if not api_key and args.apply_ids is None:
        # apply-ids mode ne nécessite pas de clé API
        print(
            f"{_API_KEY_VAR} manquant — renseigne-le dans {_ENV_FILE}.",
            file=sys.stderr,
        )
        return 2

    model = args.model or os.environ.get("BRAIN_NVIDIA_MODEL") or DEFAULT_MODEL
    base_url = args.base_url or os.environ.get("BRAIN_NVIDIA_BASE_URL") or DEFAULT_BASE_URL
    fallback_model = (
        args.fallback_model or os.environ.get(_FALLBACK_MODEL_VAR) or DEFAULT_EXTRACT_FALLBACK_MODEL
    )
    if fallback_model == model:
        # Un secours identique au primaire n'est pas un secours : il ferait
        # croire à une chaîne là où il n'y a qu'un seul point de panne.
        fallback_model = None

    return asyncio.run(_run(args, api_key, model, base_url, fallback_model=fallback_model))


async def _run(
    args: Any,
    api_key: str,
    model: str,
    base_url: str,
    *,
    fallback_model: str | None = None,
) -> int:
    from pydantic import ValidationError  # noqa: PLC0415

    from brain_v42.config import Settings  # noqa: PLC0415
    from brain_v42.db.engine import get_session_factory  # noqa: PLC0415

    try:
        settings = Settings()  # type: ignore[call-arg]  # validate config early
    except ValidationError as exc:
        print(f"Config invalide: {exc}", file=sys.stderr)
        return 2

    sf = get_session_factory()
    t0 = time.monotonic()
    any_failed = False
    error_msg: str | None = None

    # --apply-ids mode: no LLM, just apply reviewed proposals.
    if args.apply_ids is not None:
        try:
            ids = [int(x.strip()) for x in args.apply_ids.split(",") if x.strip()]
        except ValueError:
            print(
                "--apply-ids doit être une liste d'entiers séparés par des virgules",
                file=sys.stderr,
            )
            return 1
        applied, entities = await apply_proposals(sf, ids)
        duration = time.monotonic() - t0
        print(f"apply: {applied} appliqués, {entities} entités créées")
        await record_dream_run(sf, "done", dry=False, duration_s=duration, error=None)
        return 0

    # Propose mode (dry or wet).
    threads = await fetch_pending_threads(sf, args.limit)
    if not threads:
        print("Aucun ticket pending — rien à faire.")
        await record_dream_run(
            sf, "done", dry=not args.wet, duration_s=time.monotonic() - t0, error=None
        )
        return 0

    run_budget_seconds = getattr(args, "run_budget_seconds", _DEFAULT_RUN_BUDGET_SECONDS)
    ticket_budget_seconds = getattr(args, "ticket_budget_seconds", _DEFAULT_TICKET_BUDGET_SECONDS)
    http_client = httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0),
    )

    all_proposal_ids: list[int] = []
    scanned = 0
    total_proposals = 0
    skipped = 0
    failed = 0
    timed_out = 0
    # Counted apart from timed_out: these tickets were never started, so no
    # deadline was exceeded and no model call was wasted (ticket 572220e9).
    deferred_count = 0
    # Counted apart from `any_failed`, which is ALSO set by the timeout
    # branches and therefore cannot discriminate. Only this one answers
    # "something broke that the clock does not explain", which is what the
    # exit code owes dream.sh now that `3` leaves the unit green.
    hard_failed = 0
    deduped = 0
    # Le modèle réellement servi, qui n'est plus forcément celui demandé : une
    # fin de vie chez le fournisseur bascule le RUN entier sur le secours.
    active_model = model
    switched_to_fallback = False
    model_gone: str | None = None
    try:
        for position, thread in enumerate(threads):
            elapsed = time.monotonic() - t0
            remaining = run_budget_seconds - elapsed
            if position == 0:
                logger.info(
                    "extract_phase_pretickets", elapsed_before_first_ticket_s=round(elapsed, 3)
                )
            # Neither the reserve (terminal dream_run) nor the wet allowance
            # (applying what this loop proposes) is ever available to a
            # ticket: what is left for work is this.
            wet_allowance = _WET_APPLY_ALLOWANCE_SECONDS if args.wet else 0.0
            usable_slice = remaining - _FINALIZATION_RESERVE_SECONDS - wet_allowance
            if usable_slice < _MIN_TICKET_SLICE_SECONDS:
                deferred = threads[position:]
                logger.info(
                    "extract_gate_closed",
                    position=position,
                    elapsed_s=round(elapsed, 3),
                    remaining_s=round(max(0.0, remaining), 3),
                    usable_slice_s=round(usable_slice, 3),
                    min_ticket_slice_s=_MIN_TICKET_SLICE_SECONDS,
                    ticket_budget_s=ticket_budget_seconds,
                    finalization_reserve_s=_FINALIZATION_RESERVE_SECONDS,
                    wet_allowance_s=wet_allowance,
                    deferred_count=len(deferred),
                )
                for pending in deferred:
                    await record_ticket_attempt(
                        sf,
                        pending,
                        "deferred",
                        0.0,
                        f"run budget exhausted: {max(0.0, usable_slice):.1f}s usable "
                        f"< {_MIN_TICKET_SLICE_SECONDS}s minimum slice",
                    )
                    print(f"progress: ticket {pending.id} deferred (run budget)")
                deferred_count += len(deferred)
                break

            # Truncate rather than refuse: a ticket started near the end of
            # the window gets what is left, never the reserve.
            ticket_slice_seconds = min(ticket_budget_seconds, usable_slice)
            ticket_started = time.monotonic()
            ticket_deadline = ticket_started + ticket_slice_seconds
            while True:
                try:
                    outcome = await _extract_thread_with_budget(
                        http_client,
                        active_model,
                        thread,
                        timeout_seconds=ticket_slice_seconds,
                    )
                    break
                except ModelGoneError as exc:
                    # La bascule est une décision de RUN, pas de ticket : sans
                    # cet état, les 19 tickets suivants repaieraient chacun le
                    # même 410 pour réapprendre la même chose.
                    if switched_to_fallback or not fallback_model:
                        model_gone = _exc_str(exc)
                        print(f"FATAL extract: plus aucun modèle vivant — {model_gone}")
                        break
                    print(
                        f"progress: modèle {exc.model} retiré (HTTP {exc.status_code}) "
                        f"— bascule sur {fallback_model} pour la suite du run"
                    )
                    active_model = fallback_model
                    switched_to_fallback = True
            if model_gone:
                break
            ticket_duration = time.monotonic() - ticket_started
            scanned += 1
            if outcome.failed:
                is_timeout = "timeout" in (outcome.error or "").lower()
                attempt_status = "timeout" if is_timeout else "failed"
                await record_ticket_attempt(
                    sf, thread, attempt_status, ticket_duration, outcome.error
                )
                print(
                    f"progress: ticket {thread.id} {attempt_status}: {_safe_error(outcome.error)}"
                )
                _log_ticket_timing(
                    thread,
                    position=position,
                    status=attempt_status,
                    phase="extraction",
                    ticket_started=ticket_started,
                    extraction_duration_s=ticket_duration,
                    remaining_before_s=remaining,
                    ticket_slice_s=ticket_slice_seconds,
                    ticket_budget_s=ticket_budget_seconds,
                )
                failed += 1
                timed_out += int(is_timeout)
                hard_failed += int(not is_timeout)
                any_failed = True
                error_msg = _safe_error(outcome.error)
                continue

            dedup_result = DedupResult(kept=outcome.drafts)
            if outcome.drafts:
                from brain_v42.services.gpu_embedding_service import (  # noqa: PLC0415
                    GPUEmbeddingService,
                )

                embedding_svc = GPUEmbeddingService(base_url=settings.embedding_service_url)
                try:
                    dedup_result = await _within_deadline(
                        deduplicate_drafts(sf, embedding_svc, outcome.drafts), ticket_deadline
                    )
                except TimeoutError:
                    error_msg = "corpus dedup exceeded the ticket deadline"
                    await record_ticket_attempt(sf, thread, "timeout", ticket_duration, error_msg)
                    print(f"progress: ticket {thread.id} timeout: {_safe_error(error_msg)}")
                    _log_ticket_timing(
                        thread,
                        position=position,
                        status="timeout",
                        phase="dedup",
                        ticket_started=ticket_started,
                        extraction_duration_s=ticket_duration,
                        remaining_before_s=remaining,
                        ticket_slice_s=ticket_slice_seconds,
                        ticket_budget_s=ticket_budget_seconds,
                    )
                    failed += 1
                    timed_out += 1
                    any_failed = True
                    continue
                except Exception as exc:
                    error_msg = _safe_error(f"corpus dedup unavailable: {exc}")
                    await record_ticket_attempt(sf, thread, "failed", ticket_duration, error_msg)
                    print(f"progress: ticket {thread.id} failed: {_safe_error(error_msg)}")
                    _log_ticket_timing(
                        thread,
                        position=position,
                        status="failed",
                        phase="dedup",
                        ticket_started=ticket_started,
                        extraction_duration_s=ticket_duration,
                        remaining_before_s=remaining,
                        ticket_slice_s=ticket_slice_seconds,
                        ticket_budget_s=ticket_budget_seconds,
                    )
                    failed += 1
                    hard_failed += 1
                    any_failed = True
                    continue
                finally:
                    await embedding_svc.close()

            for duplicate in dedup_result.duplicates:
                print(
                    f"dedup: ticket {duplicate.draft.ticket_id} "
                    f"{duplicate.draft.target_type} skipped → "
                    f"{duplicate.entity_type}:{duplicate.entity_id} "
                    f"(cosine={duplicate.similarity:.3f})"
                )
            deduped += len(dedup_result.duplicates)
            try:
                ids = await _within_deadline(
                    persist_proposals(sf, thread, dedup_result.kept), ticket_deadline
                )
            except TimeoutError:
                error_msg = "proposal checkpoint exceeded the ticket deadline"
                await record_ticket_attempt(sf, thread, "timeout", ticket_duration, error_msg)
                print(f"progress: ticket {thread.id} timeout: {_safe_error(error_msg)}")
                _log_ticket_timing(
                    thread,
                    position=position,
                    status="timeout",
                    phase="persist",
                    ticket_started=ticket_started,
                    extraction_duration_s=ticket_duration,
                    remaining_before_s=remaining,
                    ticket_slice_s=ticket_slice_seconds,
                    ticket_budget_s=ticket_budget_seconds,
                )
                failed += 1
                timed_out += 1
                any_failed = True
                continue
            except Exception as exc:
                error_msg = f"proposal checkpoint unavailable: {type(exc).__name__}"
                await record_ticket_attempt(sf, thread, "failed", ticket_duration, error_msg)
                print(f"progress: ticket {thread.id} failed: {_safe_error(error_msg)}")
                _log_ticket_timing(
                    thread,
                    position=position,
                    status="failed",
                    phase="persist",
                    ticket_started=ticket_started,
                    extraction_duration_s=ticket_duration,
                    remaining_before_s=remaining,
                    ticket_slice_s=ticket_slice_seconds,
                    ticket_budget_s=ticket_budget_seconds,
                )
                failed += 1
                hard_failed += 1
                any_failed = True
                continue
            all_proposal_ids.extend(ids)
            total_proposals += len(ids)
            if not dedup_result.kept:
                skipped += 1
            await record_ticket_attempt(sf, thread, "done", ticket_duration, None)
            print(
                f"progress: ticket {thread.id} done "
                f"(proposals={len(ids)}, dedup={len(dedup_result.duplicates)})"
            )
            _log_ticket_timing(
                thread,
                position=position,
                status="done",
                phase="done",
                ticket_started=ticket_started,
                extraction_duration_s=ticket_duration,
                remaining_before_s=remaining,
                ticket_slice_s=ticket_slice_seconds,
                ticket_budget_s=ticket_budget_seconds,
            )
    finally:
        await http_client.aclose()

    if model_gone:
        # Ni une échéance ni un ticket fautif : la phase n'avait plus de modèle
        # à qui parler. `any_failed` porte le rc=1, et l'erreur NOMME le dernier
        # modèle essayé — sans ça la nuit suivante rejoue la même impasse.
        any_failed = True
        hard_failed += 1
        error_msg = _safe_error(model_gone)

    logger.info(
        "extract_phase_summary",
        scanned=scanned,
        total_proposals=total_proposals,
        skipped=skipped,
        deduped=deduped,
        failed=failed,
        timed_out=timed_out,
        deferred_count=deferred_count,
        phase_duration_s=round(time.monotonic() - t0, 3),
    )
    print(
        f"{scanned} tickets scannés, {total_proposals} proposals, "
        f"{skipped} skipped, {deduped} dedup, {failed} failed, "
        f"{timed_out} timeout, {deferred_count} deferred"
    )
    if all_proposal_ids:
        print(f"proposal ids: {all_proposal_ids}")

    # --wet: apply only proposals created in this run.
    if args.wet and all_proposal_ids:
        # Only proposals created in this invocation are auto-applied. An
        # interrupted WET application is resumed explicitly with --apply-ids;
        # this never steals a proposal intentionally left for human review.
        wet_queue = list(dict.fromkeys(all_proposal_ids))
        for index, proposal_id in enumerate(wet_queue):
            remaining = run_budget_seconds - (time.monotonic() - t0)
            wet_budget = remaining - _FINALIZATION_RESERVE_SECONDS
            if wet_budget < _MIN_WET_APPLY_SECONDS:
                # Declining to start is a deferral, not a timeout: the reserve
                # is being spent as designed, on recording the terminal run.
                # The leftovers are named because nothing else will pick them
                # up: the next run only auto-applies ids it created itself, so
                # recovery is a manual `--apply-ids` on exactly these.
                unapplied = wet_queue[index:]
                deferred_count += 1
                logger.warning(
                    "extract_wet_deferred",
                    remaining_s=round(remaining, 3),
                    wet_budget_s=round(wet_budget, 3),
                    min_wet_apply_s=_MIN_WET_APPLY_SECONDS,
                    unapplied_proposal_ids=unapplied,
                )
                print(f"wet: deferred (finalization reserve), non appliqués: {unapplied}")
                break
            try:
                applied, entities = await asyncio.wait_for(
                    apply_proposals(sf, [proposal_id]),
                    timeout=wet_budget,
                )
                print(
                    f"wet: proposal {proposal_id}: {applied} appliqués, {entities} entités créées"
                )
            except TimeoutError:
                timed_out += 1
                error_msg = "wet apply exceeded its bounded finalization budget"
                print("wet: timeout (finalization reserve retained)")
            except Exception as exc:
                hard_failed += 1
                any_failed = True
                error_msg = f"wet apply unavailable: {type(exc).__name__}"
                print(f"wet: failed ({type(exc).__name__})")

    duration = time.monotonic() - t0
    # A deferral-only run is `done`: the briefing reads dream_runs for its
    # "Last failure" block, so recording one here would put a failure that
    # never happened in front of the operator at every session start. The
    # deferred tickets keep their own durable row in ticket_extraction_attempts,
    # which is the table migration 038 exists for.
    status = "timeout" if timed_out else "fail" if any_failed else "done"
    await record_dream_run(
        sf,
        status=status,
        dry=not args.wet,
        duration_s=duration,
        error=(
            error_msg
            or (f"{timed_out} ticket(s) timed out before run deadline" if timed_out else None)
        ),
    )
    return _exit_code(
        timed_out=timed_out,
        any_failed=any_failed,
        deferred=deferred_count,
        hard_failed=hard_failed,
    )


if __name__ == "__main__":
    sys.exit(main())
