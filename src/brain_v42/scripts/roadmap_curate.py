"""Roadmap curation — audited proposal and guarded auto-apply (spec 2026-07-04 §3).

One batch per project: live features (status ∉ done/archived, not merged), their
description when it says more than the title, plus a digest of recent artifacts
(title, type, date — NOT the bodies), sent to the LLM (NVIDIA API, strict JSON
WITHOUT tools — the exact skeleton of ticket_extract).
Four auditable ops: merge, archive, status, rename.

Hard guardrails:
- pinned: only the `status` op can be proposed;
- done/archived: outside the batch by construction (untouchable);
- merge within a project only, `into` must be in the batch;
- cap of MAX_PROPOSALS_PER_NIGHT proposals/night (drop logged, never silent).

Wet scope (narrowed 2026-09-02): --wet applies only the ops WET_APPLYABLE_OPS
names — `archive` and `status` — and only if the model that produced the batch is
in AUTO_APPLY_MODELS. `merge` and `rename` are still proposed, never applied
unattended; --apply-ids remains the reviewed apply without an LLM, and it applies
every op.

Usage:
    python -m scripts.roadmap_curate [--limit 10]        # propose (dry)
    python -m scripts.roadmap_curate --limit 10 --wet    # propose + apply (archive/status)
    python -m scripts.roadmap_curate --apply-ids "3,4" --project-key red-lab
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import sqlalchemy as sa

from brain_v42.dream_degradation import DEGRADED_PREFIX
from brain_v42.dream_run_project_key import GLOBAL_PHASE_PROJECT_KEY
from brain_v42.scripts.domain_backfill import (
    DEFAULT_BASE_URL,
    ModelGoneError,
    NvidiaAuthError,
    ResponseParseError,
    _exc_str,
    _post_chat,
    _strip_fences,
    load_env_file,
)

# ONE definition of the reasoning-token extractor, shared with the other NVIDIA
# rail: two rails disagreeing on how to read the same provider usage is exactly
# the drift that leaves one column right and the other silently zero.
from brain_v42.services.proposal_service import PostConditionError as PostConditionError

_API_KEY_VAR = "BRAIN_NVIDIA_API_KEY"
_ROADMAP_MODEL_VAR = "BRAIN_NVIDIA_ROADMAP_MODEL"
_ROADMAP_FALLBACK_MODEL_VAR = "BRAIN_NVIDIA_ROADMAP_FALLBACK_MODEL"
#: The operator's explicit "yes, I meant it" for a WET night whose primary can
#: auto-apply. It is NOT a killswitch and it does not choose a model: it only
#: says that the coincidence between `--wet` and an allowlisted primary was
#: intended. Ticket 7511c210.
_AUTO_APPLY_ACK_VAR = "BRAIN_DREAM_ROADMAP_AUTO_APPLY_ACK"
_ENV_FILE = Path.home() / ".config" / "brain-v42" / "nvidia.env"
# qwen/qwen3-next-80b-a3b-instruct reached its provider EOL on 2026-07-27
# (HTTP 410 Gone) and the 8B fallback served ten nights in silence. Replacement
# chosen by the 2026-08-05 canary on the REAL prompt, 3 real batches:
# deepseek-v4-flash 3/3 valid at 16.6 s/batch — faster than the 8B fallback
# itself (21.2 s). Rejected: llama-3.1-70b (3/3 but 117.5 s/batch, i.e. ~1175 s
# over 10 projects, beyond the 720 s night budget), nemotron-super-49b-v1.5 and
# nemotron-3-nano-30b (unreadable JSON after the corrective re-prompt),
# kimi-k2.6 and nemotron-nano-3-30b (404 despite being listed in /v1/models),
# minimax-m3 (timeout), mistral-medium-3.5 (timeout).
# 2026-08-16: deepseek-v4-flash died in turn (HTTP 410), nine days after the
# canary that chose it. Replacement selected on TWO measurements, not one,
# because the first nearly picked the wrong model.
#
# 1. SPEED AND SHAPE (paired canary, 3 real batches, proposer-only regime):
#    mistral-nemotron 3/3 valid, 12-20 s/batch — 126-204 s over the ten
#    projects, against a 720 s night budget. Rejected: deepseek-v4-flash-0731,
#    the dated snapshot of the dead family, 3/3 valid but 69.3 s/batch i.e.
#    693 s — 96 % of the budget, and FOUR TIMES slower than the alias it
#    replaces (16.6 s measured on 08-05). A dated pin does not inherit its
#    alias's profile. nemotron-3.5-lightning-30b: 2/3 valid.
#
# 2. CONTENT QUALITY, which overturned the ranking. The triplet the canary
#    measures — validity, seconds, NUMBER of proposals — ranks nothing: over
#    three runs of the same batches, mistral-nemotron returned 31 then 21
#    proposals and gpt-oss-20b 29 then 13. The gap between candidates is
#    smaller than the noise of a single one. Blind judgement of the CONTENT
#    (models anonymized, three lenses, accusations rebutted adversarially):
#    mistral-nemotron 48/100, gpt-oss-20b 35, llama-3.1-8b 10.
#
#    The 8B fallback is therefore the WORST of the three on substance while
#    being the fastest — recounted by hand: 9 empty rationales, 2 merges into a
#    target it archives in the same batch, 2 renames to an identical string,
#    and seven orchestrator runs folded into the OLDEST of them on the grounds
#    that "r202 is a step of r138". It stays the fallback and does not become
#    primary: see tests/unit/test_roadmap_model_chain.py.
DEFAULT_ROADMAP_MODEL = "mistralai/mistral-nemotron"
# Fallback replaced on 2026-08-29: the 8B died with a 410 on 2026-08-26 (the
# nights of the 27th and 28th failed, probe GONE). gpt-oss-20b is judged blind
# above the corpse it replaces (35/100 against 10) and MEASURED IN ITS EXACT
# REGIME on 2026-08-29, after fixing the instrument (60 s night windows, ten
# real batches, FALLBACK_* fallback caps): 10/10 carried, 12 proposals,
# 7.8 s/batch — 78 s projected for a fully degraded night, budget 720 s. The
# fallback profile is NOT "reduced caps": same 3 features, tokens DOUBLED (1024
# against 512) — and that is what makes it hold: at 512, gpt-oss's reasoning is
# truncated, the corrective re-prompt doubles the bill (74.5 s/batch measured
# under the old instrument); at 1024 it answers in one call. Rejected under the
# same regime: nano-30b (fast, 9.9 s/batch, but 5/10 valid JSON — the nemotron
# signature); deepseek-v4-flash-0731 (69.3 s/batch on 08-16, a family dead twice
# in one month, content never judged).
DEFAULT_ROADMAP_FALLBACK_MODEL = "openai/gpt-oss-20b"
# WET pair replaced on 2026-08-29: llama-3.3-70b (strict canary of 2026-07-14)
# died with a 410 between the nights of 27 and 28 August — a DORMANT link on the
# roadmap side, the phase running in DRY; without extract, which shared that
# model, nobody would have seen it die. Yesterday's fallback becomes the primary
# (nemotron-3-super-120b-a12b: the strongest of the live models measured on this
# prompt — 31 proposals) and gpt-oss-120b takes the fallback post, a post
# test_roadmap_model_chain requires to be distinct.
#
# REARMING PRECONDITION, measured on 2026-08-29 in the real regime: in WET the
# unmanaged path HALVES the window as soon as a fallback exists
# (candidate_timeout_s = llm_timeout_s / 2, i.e. 30 s at ten projects) — the
# bound is NOT httpx's 180 s read timeout. Under those 30 s, this pair as
# ordered DOES NOT CARRY: super-120b 1/10 batches, the other 9 rescued by
# gpt-oss-120b (which holds 30 s warm — its 75 s cold queue concerns only the
# first call). These names are therefore LIVE links judged on substance nowhere:
# setting BRAIN_DREAM_ROADMAP_DRY_RUN back to false requires first a WET canary
# (30 s windows) and probably re-pairing or widening the window — an operator
# gesture, not a single word to change. The current DRY killswitch never
# consumes this pair.
DEFAULT_WET_ROADMAP_MODEL = "nvidia/nemotron-3-super-120b-a12b"
DEFAULT_WET_ROADMAP_FALLBACK_MODEL = "openai/gpt-oss-120b"
AUTO_APPLY_MODELS = frozenset({DEFAULT_WET_ROADMAP_MODEL, DEFAULT_WET_ROADMAP_FALLBACK_MODEL})
PROPOSER_ONLY_MODELS = frozenset({DEFAULT_ROADMAP_MODEL, DEFAULT_ROADMAP_FALLBACK_MODEL})
# HTTP 410 = the provider retired the model (EOL). No retry and no other batch
# size will repair it: only a configuration change can. To be distinguished from
# a 503 "busy", which is transient.
HTTP_GONE = 410
MODEL_GONE_MARKER = "MODÈLE ABSENT CHEZ LE FOURNISSEUR"

VALID_OPS = ("merge", "archive", "status", "rename")
# 'archived' excluded: the `archive` op exists for that.
PROPOSABLE_STATUSES = ("planned", "research", "design", "building", "deployed", "done")
# NARROWED on 2026-09-02 (operator decision) back to what rollout §4 always
# described. The 2026-07-04 aggressive regime bet that a wrong curation was cheap
# and that the morning check would catch it; the history, measured read-only that
# day, says otherwise. Of the 181 proposals the wet has ever applied, 150 (83%)
# were `merge` or `rename` — the two ops the plan told an operator were out of
# scope — and every apply falls inside one 11-day window (2026-07-04 → 07-14),
# nothing since. Human review rejected 592 against those 181, including 14 of 14
# on brain-v42 on 2026-09-02, all title rewrites (decision `892c1491`).
#
# Narrowing is free today because the nightly is DRY: what it changes is the day
# of a flip, which would apply the 185 pending proposals under the old scope and
# 45 under this one. `merge` and `rename` stay applicable BY REVIEW — `--apply-ids`
# and `brain_apply_curation_proposal` both pass `allowed_ops=None`. What is
# bounded here is the UNATTENDED path, and only it.
#
# A tuple, not a frozenset: `apply_proposals(allowed_ops: tuple[str, ...] | None)`
# is the declared type, and membership is all this is ever used for.
WET_APPLYABLE_OPS = ("archive", "status")
MAX_FEATURES_PER_PROJECT = 30
MAX_ARTIFACTS_PER_FEATURE = 10
# Descriptions are prose, and some are essays. MEASURED read-only on 2026-09-02:
# a 30-card brain-v42 batch carries 2 733 bytes of names and would carry 29 469
# more of descriptions — an 11× feature section, ~7 400 tokens of input — with a
# single description reaching 3 543 bytes across all projects. Sending them whole
# would break the path this is meant to improve: that batch was already truncated
# once at 4 096 (first wet run of 2026-07-04, char 12160). The cap keeps the first
# sentences, where these descriptions state their subject, and the truncation is
# ANNOUNCED — a silently cut description reads as a description that stops there.
MAX_DESCRIPTION_CHARS = 240
MAX_PROPOSALS_PER_NIGHT = 40
# The consolidator prompt produces long answers (the brain-v42 batch truncated
# at 4096 on the first wet run of 2026-07-04, char 12160) — 2× margin.
MAX_COMPLETION_TOKENS = 8192
# The small batches produced by the shrink do not need to reserve 8k tokens on
# the free provider. Simple tiers: 2k for ≤3 features, 4k for the economy batch
# ≤10, 8k only beyond that (historically brain-v42 at 30).
MIN_COMPLETION_TOKENS = 2048
BALANCED_COMPLETION_TOKENS = 4096
# Bounded profile for the free models ROADMAP manages: Qwen 80B MoE stays the
# main model, but receives only the context useful for a short decision.
# Llama 8B takes over when the first is unavailable.
BIG_MODEL_FEATURE_CAP = 3
FALLBACK_FEATURE_CAP = 3
FALLBACK_RETRY_FEATURE_CAP = 2
COMPACT_ARTIFACT_CAP = 3
BIG_MODEL_COMPLETION_TOKENS = 512
FALLBACK_COMPLETION_TOKENS = 1024
# Cap PER LLM ATTEMPT on a batch (night of 2026-07-05: red burned ~9 min in
# ReadTimeout×3 on the same payload before failing). Covers the first httpx
# ReadTimeout (read=180 s) plus the start of the retry — beyond that, we
# shrink.
LLM_ATTEMPT_TIMEOUT_S = 200.0
# PROGRESSIVE shrink on LLM timeout. The old halve-once (30→15) failed the phase
# when the shrink to 15 timed out too (slow NIM nights of 2026-07-06 red-shrik,
# 2026-07-07 brain-v42). We now retry in ÷SHRINK_DIVISOR steps (30→10→3) down to
# a floor of 1 feature: a slow night lays a small batch instead of failing, and
# the rotation serves the rest. The SHRINK_MAX_RETRIES steps SHARE a single
# LLM_ATTEMPT_TIMEOUT_S window (LLM_ATTEMPT_TIMEOUT_S / SHRINK_MAX_RETRIES each)
# → total LLM per batch ≈ 2×LLM_ATTEMPT_TIMEOUT_S, IDENTICAL to the legacy worst
# case: the margin under dream.sh's 20 m SIGTERM (cf. NIGHT_BUDGET_S) is
# preserved.
SHRINK_DIVISOR = 3
SHRINK_MAX_RETRIES = 2
ECONOMIC_FEATURE_CAP = MAX_FEATURES_PER_PROJECT // SHRINK_DIVISOR
# Merge judge (two-tier anti-dump gate): short call, compact answer.
JUDGE_TIMEOUT_S = 90.0
JUDGE_MAX_TOKENS = 2048
# NO new batch at all after this threshold — a clean end (record_dream_run
# written) well before the shell SIGTERM at 20 m; residual worst case per batch:
# full + SHRINK_MAX_RETRIES steps sharing ONE LLM_ATTEMPT_TIMEOUT_S window
# = 2×200 s in total + judge 90 s + persist ≈ 8 m ⇒ 12 m + 8 m < 20 m.
NIGHT_BUDGET_S = 720.0

_SYSTEM_PROMPT = (
    "Tu es le cureur nocturne d'une roadmap de features auto-générées par "
    "clustering d'artifacts (decisions, learnings, snippets, runbooks, "
    "plans). Ta mission première est de CONSOLIDER : regrouper les features "
    "granulaires (un commit, un fix ponctuel, une session de travail) en "
    "grands sujets porteurs — une roadmap lisible compte peu de sujets "
    "larges, pas des dizaines de features atomiques. Tu réponds UNIQUEMENT "
    "avec un tableau JSON valide (éventuellement vide []) — pas de prose, "
    "pas de markdown. Chaque "
    'élément: {"op": "merge"|"archive"|"status"|"rename", "feature_id": '
    '"<uuid du batch>", "payload": {...}, "rationale": "pourquoi"}. '
    'payload merge: {"into": "<uuid du batch>"} — fusionne feature_id dans '
    "into (survivante) : doublons ET features relevant du même sujet ou "
    "chantier ; choisis comme survivante la feature au titre le plus "
    "représentatif du sujet (retitre-la via rename si aucun titre ne "
    "convient). Fusionne toujours DIRECTEMENT dans la survivante finale — "
    "jamais de chaîne : si A et B rejoignent le sujet C, propose A→C et "
    "B→C, pas A→B puis B→C ; une feature ne peut être fusionnée qu'une "
    "seule fois par réponse et une survivante ne peut pas être elle-même "
    "fusionnée. Un merge n'est PAS un rangement : la proximité de projet, "
    "d'outil ou de chantier ne suffit pas — source et survivante doivent "
    "relever du même sujet précis ; un gotcha/learning technique isolé ne "
    "rejoint un chantier que si celui-ci le couvre explicitement, et deux "
    "plans ou cycles de travail distincts ne se fusionnent jamais entre "
    "eux ; une feature sans sujet commun dans le batch reste séparée "
    "(ou archive si bruit). "
    "payload archive: {} — feature morte ou bruit sans valeur roadmap. "
    'payload status: {"status": "planned"|"research"|"design"|"building"|'
    '"deployed"|"done"} — aligne le statut sur la réalité des artifacts '
    "(un artifact « livré X »/« déployer X » → deployed ; un plan done → "
    "done). "
    'payload rename: {"name": "<titre clair, ≤200 chars>"} — retitre les '
    "noms de cluster illisibles ou trop granulaires en titres de sujet. "
    "Règles dures : n'utilise QUE des feature_id présents dans le batch ; "
    "une seule proposition par feature ; limite rationale à 120 caractères ; "
    "une feature marquée PINNED n'accepte QUE l'op status ; merge "
    "uniquement entre features du batch (même projet). Ne conserve séparé "
    "que ce qui est réellement un sujet distinct."
)
_REPROMPT_INSTRUCTION = (
    "Ta réponse précédente n'était pas un tableau JSON valide selon le format "
    "demandé. Renvoie UNIQUEMENT le tableau JSON corrigé."
)


@dataclass
class FeatureCard:
    id: UUID
    name: str
    status: str
    pinned: bool
    artifacts: list[str] = field(default_factory=list)
    # Optional, and it must stay so: the shrink rebuilds cards field by field, and
    # a required field would break the retry path at the worst moment. `None` and
    # "same as the name" both mean the same thing to the reader — this feature
    # says nothing beyond its title (359 live features out of 422 on 2026-09-02).
    description: str | None = None


@dataclass
class ProjectBatch:
    project_key: str
    features: list[FeatureCard]


@dataclass
class CurationDraft:
    op: str
    feature_id: UUID
    payload: dict[str, Any]
    rationale: str


@dataclass
class BatchOutcome:
    batch: ProjectBatch
    drafts: list[CurationDraft]
    failed: bool = False
    error: str | None = None
    # True if the answer covers fewer features than the original batch, whether
    # the shrink happened before or after the first LLM attempt.
    shrunk: bool = False
    # Model that produced the answer (or the last model tried on failure).
    model_used: str | None = None
    fallback_used: bool = False
    # Failure of the PRIMARY model, kept even when the fallback succeeds.
    # Without this field, a run served entirely by the fallback is
    # indistinguishable from a nominal one (qwen 80B died on 2026-07-27,
    # discovered on 08-05 after ten green nights).
    primary_error: str | None = None


@dataclass
class PersistResult:
    """Résultat de persist_proposals — voir dedup inter-nuits (2026-07-04)."""

    inserted: list[int] = field(default_factory=list)
    refreshed: list[int] = field(default_factory=list)
    rejected_skipped: int = 0


def format_digest(
    artifact_type: str,
    title: str,
    created_at: datetime,
    plan_status: str | None,
) -> str:
    """Digest one row — title/type/date, never the full bodies."""
    base = f"{created_at.date().isoformat()} [{artifact_type}] {title}"
    if artifact_type == "plan" and plan_status:
        base += f" (plan {plan_status})"
    return base


def _clip(text: str, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    """Bound a description, saying so when it is cut."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return f"{flat[:limit]}… (tronquée à {limit} caractères)"


def render_batch(batch: ProjectBatch) -> str:
    lines = [f"Projet: {batch.project_key} — {len(batch.features)} features vivantes"]
    for f in batch.features:
        pin = " [PINNED — seule l'op status est permise]" if f.pinned else ""
        lines.append(f"\n- feature_id: {f.id}\n  nom: {f.name}\n  statut: {f.status}{pin}")
        # Rendered only when it adds something. Repeating the title on the 85 % of
        # features where description == name would spend prompt budget on a line
        # the model already has, on a path that has already been truncated once
        # (brain-v42, first wet run of 2026-07-04, char 12160). Its ABSENCE is
        # itself the signal a curator needs.
        if f.description and f.description != f.name:
            lines.append(f"  description: {_clip(f.description)}")
        if f.artifacts:
            lines.append("  artifacts récents:")
            lines.extend(f"    - {a}" for a in f.artifacts)
        else:
            lines.append("  artifacts récents: (aucun)")
    return "\n".join(lines)


def _compact_batch(
    batch: ProjectBatch,
    *,
    feature_cap: int,
    artifact_cap: int,
) -> ProjectBatch:
    """Compact copy of a batch, without mutating the cards loaded from the DB."""
    return ProjectBatch(
        project_key=batch.project_key,
        features=[
            FeatureCard(
                id=feature.id,
                name=feature.name,
                status=feature.status,
                pinned=feature.pinned,
                artifacts=list(feature.artifacts[:artifact_cap]),
                description=feature.description,
            )
            for feature in batch.features[:feature_cap]
        ],
    )


def build_messages(batch: ProjectBatch) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": render_batch(batch)},
    ]


def _parse_item_uuid(value: Any, i: int, fieldname: str) -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ResponseParseError(f"item {i}: {fieldname} is not a valid UUID: {value!r}") from exc


def parse_and_validate(content: str, batch: ProjectBatch) -> list[CurationDraft]:
    try:
        data = json.loads(_strip_fences(content))
    except json.JSONDecodeError as exc:
        raise ResponseParseError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ResponseParseError(f"expected a JSON array, got {type(data).__name__}")
    by_id = {f.id: f for f in batch.features}
    drafts: list[CurationDraft] = []
    seen_features: set[UUID] = set()
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ResponseParseError(f"item {i}: expected object")
        op = item.get("op")
        if op not in VALID_OPS:
            raise ResponseParseError(f"item {i}: invalid op {op!r} (valid: {VALID_OPS})")
        fid = _parse_item_uuid(item.get("feature_id"), i, "feature_id")
        feature = by_id.get(fid)
        if feature is None:
            raise ResponseParseError(f"item {i}: feature_id {fid} not in batch")
        if fid in seen_features:
            raise ResponseParseError(
                f"item {i}: une seule proposition par feature — {fid} apparaît plus d'une fois"
            )
        seen_features.add(fid)
        if feature.pinned and op != "status":
            raise ResponseParseError(
                f"item {i}: feature {fid} is pinned — only op 'status' allowed"
            )
        payload = item.get("payload")
        if payload is None and op == "archive":
            payload = {}
        if not isinstance(payload, dict):
            raise ResponseParseError(f"item {i}: payload must be an object")
        if op == "merge":
            into = _parse_item_uuid(payload.get("into"), i, "payload.into")
            if into not in by_id:
                raise ResponseParseError(f"item {i}: merge target {into} not in batch")
            if into == fid:
                raise ResponseParseError(f"item {i}: merge target equals feature_id")
            payload = {"into": str(into)}
        elif op == "status":
            new_status = payload.get("status")
            if new_status not in PROPOSABLE_STATUSES:
                raise ResponseParseError(
                    f"item {i}: invalid status {new_status!r} "
                    f"(valid: {PROPOSABLE_STATUSES}; use op 'archive' to archive)"
                )
            payload = {"status": new_status}
        elif op == "rename":
            name = payload.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ResponseParseError(
                    f"item {i}: rename payload must contain a non-empty 'name'"
                )
            payload = {"name": name.strip()[:200]}
        else:  # archive
            payload = {}
        drafts.append(
            CurationDraft(
                op=op,
                feature_id=fid,
                payload=payload,
                rationale=str(item.get("rationale", ""))[:120],
            )
        )
    # Anti-chain guards (aggressive prompt 2026-07-04): one answer can neither
    # merge the same feature twice, nor merge into a survivor that is itself
    # merged — applying A→B then B→C would strand A's artifacts on an archived B
    # (the apply follows id order).
    losers: set[UUID] = set()
    for i, draft in enumerate(drafts):
        if draft.op != "merge":
            continue
        if draft.feature_id in losers:
            raise ResponseParseError(
                f"item {i}: feature {draft.feature_id} fusionnée plus d'une fois"
            )
        losers.add(draft.feature_id)
    for i, draft in enumerate(drafts):
        if draft.op == "merge" and UUID(draft.payload["into"]) in losers:
            raise ResponseParseError(
                f"item {i}: merge en chaîne — la survivante {draft.payload['into']} "
                "est elle-même fusionnée ; fusionne directement dans la survivante finale"
            )
    return drafts


def _parse_proposer_only_response(content: str, batch: ProjectBatch) -> list[CurationDraft]:
    """Keep only the safe items of an answer that will not be applied.

    The JSON array remains mandatory. Each item is then validated with the
    strict parser; an invalid item is ignored without sacrificing its
    neighbours. Merge chains are filtered once the valid items are gathered.
    """
    try:
        data = json.loads(_strip_fences(content))
    except json.JSONDecodeError as exc:
        raise ResponseParseError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ResponseParseError(f"expected a JSON array, got {type(data).__name__}")

    drafts: list[CurationDraft] = []
    seen_features: set[UUID] = set()
    for item in data:
        try:
            parsed = parse_and_validate(json.dumps([item]), batch)
        except ResponseParseError:
            continue
        if not parsed or parsed[0].feature_id in seen_features:
            continue
        seen_features.add(parsed[0].feature_id)
        drafts.append(parsed[0])

    merge_losers = {draft.feature_id for draft in drafts if draft.op == "merge"}
    return [
        draft
        for draft in drafts
        if draft.op != "merge" or UUID(draft.payload["into"]) not in merge_losers
    ]


def drop_noops(
    drafts: list[CurationDraft], batch: ProjectBatch
) -> tuple[list[CurationDraft], list[CurationDraft]]:
    """Drop proposals with no effect — identical status, identical rename.

    First real run (2026-07-04): 10/40 proposals were no-ops burning the cap. An
    effect filter after validation; we do not raise (a raise would trigger the
    corrective LLM re-prompt for a mere no-op).
    """
    by_id = {f.id: f for f in batch.features}
    kept: list[CurationDraft] = []
    dropped: list[CurationDraft] = []
    for draft in drafts:
        feature = by_id[draft.feature_id]
        is_noop = (draft.op == "status" and draft.payload.get("status") == feature.status) or (
            draft.op == "rename"
            and str(draft.payload.get("name", "")).strip() == feature.name.strip()
        )
        (dropped if is_noop else kept).append(draft)
    return kept, dropped


# ── I/O — DB + LLM ───────────────────────────────────────────────────────────

_KEYS_SQL = """
SELECT DISTINCT project_key FROM features
WHERE status NOT IN ('done', 'archived') AND merged_into IS NULL
ORDER BY project_key
"""

_FEATURES_SQL = """
SELECT f.id, f.name, f.status, COALESCE(f.pinned, false) AS pinned, f.description
FROM features f
LEFT JOIN feature_artifacts fa ON fa.feature_id = f.id
WHERE f.project_key = :pk
  AND f.status NOT IN ('done', 'archived')
  AND f.merged_into IS NULL
GROUP BY f.id, f.name, f.status, f.pinned, f.description
ORDER BY MAX(fa.created_at) DESC NULLS LAST
LIMIT :cap
"""

_ARTIFACTS_SQL = """
SELECT fa.feature_id,
       fa.artifact_type,
       fa.created_at,
       COALESCE(d.title, l.topic, s.title, r.title, a.title, p.title, g.title, '?') AS title,
       p.status AS plan_status
FROM feature_artifacts fa
LEFT JOIN decisions d ON fa.artifact_type = 'decision' AND d.id = fa.artifact_id
LEFT JOIN learnings l ON fa.artifact_type = 'learning' AND l.id = fa.artifact_id
LEFT JOIN snippets s ON fa.artifact_type = 'snippet' AND s.id = fa.artifact_id
LEFT JOIN runbooks r ON fa.artifact_type = 'runbook' AND r.id = fa.artifact_id
LEFT JOIN adrs a ON fa.artifact_type = 'adr' AND a.id = fa.artifact_id
LEFT JOIN indexed_plans p ON fa.artifact_type = 'plan' AND p.id = fa.artifact_id
LEFT JOIN gitlab_events g ON fa.artifact_type = 'gitlab_event' AND g.id = fa.artifact_id
WHERE fa.feature_id = ANY(CAST(:fids AS uuid[]))
ORDER BY fa.feature_id, fa.created_at DESC
"""


def rotate_keys(keys: list[str], limit: int, day_ordinal: int) -> list[str]:
    """Deterministic sliding window over the (sorted) list of projects.

    Advances `limit` positions per day → a full cycle in ⌈n/limit⌉ nights, with
    a stable list; if the list changes between nights, coverage stays bounded
    (the offset advances every day regardless). Without rotation, ORDER BY +
    LIMIT scanned the first 10 alphabetical projects every night and never the
    other 16 (2026-07-04).
    """
    if not keys:
        return []
    offset = (day_ordinal * limit) % len(keys)
    rotated = keys[offset:] + keys[:offset]
    return rotated[:limit]


def batch_allowance(remaining_cap: int, remaining_batches: int) -> int:
    """Fair share of the remaining cap for the next batch (ceil).

    The ceil redistributes the slots the previous batches did not consume.
    Without fair-share, the global [:cap] truncation in batch order served 3
    projects out of 26 (finding of 2026-07-04).
    """
    if remaining_batches <= 0 or remaining_cap <= 0:
        return 0
    return -(-remaining_cap // remaining_batches)


# Floor of the fair-share LLM window: a normal project call takes ~35-45 s
# (night of 2026-07-10: experteam 42 s, mrc-rag 45 s) — below 60 s we would
# serve nobody. The budget overshoot the floor allows stays bounded by _run's
# hard break (and the SIGTERM margin IMPROVES: last batch ≤ 2×floor + judge ≪
# the legacy worst case of 2×LLM_ATTEMPT_TIMEOUT_S).
MIN_LLM_WINDOW_S = 60.0


def batch_llm_window(budget_s: float, elapsed_s: float, remaining_batches: int) -> float:
    """Fair-share LLM window for the next batch (the TIME sibling of batch_allowance).

    Night of 2026-07-10: project red consumed 383 s (full window + shrink steps)
    → the 720 s budget was exhausted at the 5th project, 5 projects deferred to
    the rotation. Share = remaining budget / remaining batches; window = share/2
    because a batch consumes ≈ 2 windows (full attempt + shared shrink steps, cf.
    curate_batch), bounded to [MIN_LLM_WINDOW_S, LLM_ATTEMPT_TIMEOUT_S]. The
    slack of fast batches rolls into the next ones (elapsed grows more slowly →
    wider subsequent shares).
    """
    if remaining_batches <= 0:
        return LLM_ATTEMPT_TIMEOUT_S
    fair_share = max(0.0, budget_s - elapsed_s) / remaining_batches
    return min(LLM_ATTEMPT_TIMEOUT_S, max(MIN_LLM_WINDOW_S, fair_share / 2))


def _economic_batch_sizes(feature_count: int) -> tuple[int, int]:
    """Initial/fallback sizes of the NVIDIA economy mode."""
    if feature_count <= 0:
        return 0, 0
    first_size = min(feature_count, ECONOMIC_FEATURE_CAP)
    return first_size, max(1, first_size // SHRINK_DIVISOR)


async def fetch_project_batches(
    session_factory: Any, limit: int, day_ordinal: int | None = None
) -> list[ProjectBatch]:
    """Batches per project: live features (cap 30) + digests (cap 10/feature).

    The project window rotates each day (rotate_keys) so that every project is
    covered in ⌈n/limit⌉ nights. The feature order rotates too: under a tight
    NVIDIA window, the pre-shrink must not serve the same 10 recent cards
    forever.
    """
    if day_ordinal is None:
        day_ordinal = date.today().toordinal()
    async with session_factory() as session:
        all_keys = [r[0] for r in (await session.execute(sa.text(_KEYS_SQL))).all()]
        keys = rotate_keys(all_keys, limit, day_ordinal)
        project_cycle_days = max(1, -(-len(all_keys) // limit))
        feature_round = day_ordinal // project_cycle_days
        batches: list[ProjectBatch] = []
        for pk in keys:
            feat_rows = (
                (
                    await session.execute(
                        sa.text(_FEATURES_SQL),
                        {"pk": pk, "cap": MAX_FEATURES_PER_PROJECT},
                    )
                )
                .mappings()
                .all()
            )
            if not feat_rows:
                continue
            cards = {
                r["id"]: FeatureCard(
                    id=r["id"],
                    name=r["name"],
                    status=r["status"],
                    pinned=bool(r["pinned"]),
                    description=r["description"],
                )
                for r in feat_rows
            }
            art_rows = (
                (await session.execute(sa.text(_ARTIFACTS_SQL), {"fids": list(cards.keys())}))
                .mappings()
                .all()
            )
            for row in art_rows:
                card = cards[row["feature_id"]]
                if len(card.artifacts) >= MAX_ARTIFACTS_PER_FEATURE:
                    continue
                card.artifacts.append(
                    format_digest(
                        row["artifact_type"],
                        row["title"],
                        row["created_at"],
                        row["plan_status"],
                    )
                )
            features = list(cards.values())
            if len(features) > 1:
                _, step = _economic_batch_sizes(len(features))
                offset = (feature_round * step) % len(features)
                features = features[offset:] + features[:offset]
            batches.append(ProjectBatch(project_key=pk, features=features))
        return batches


def _completion_token_budget(feature_count: int) -> int:
    """NVIDIA output cap according to the size of the batch actually sent."""
    if feature_count <= SHRINK_DIVISOR:
        return MIN_COMPLETION_TOKENS
    if feature_count <= ECONOMIC_FEATURE_CAP:
        return BALANCED_COMPLETION_TOKENS
    return MAX_COMPLETION_TOKENS


async def _curate_llm_attempt(
    client: httpx.AsyncClient,
    model: str,
    batch: ProjectBatch,
    sleep: Any,
    *,
    max_tokens: int | None = None,
    proposer_only: bool = False,
) -> BatchOutcome:
    """One full LLM attempt: call + corrective re-prompt on a parse error."""
    messages = build_messages(batch)
    completion_cap = max_tokens or _completion_token_budget(len(batch.features))
    content, _usage = await _post_chat(client, model, messages, sleep, max_tokens=completion_cap)
    parser = _parse_proposer_only_response if proposer_only else parse_and_validate
    try:
        drafts = parser(content, batch)
    except ResponseParseError as first_error:
        corrective = [
            *messages,
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": f"{_REPROMPT_INSTRUCTION}\nErreur précise : {first_error}",
            },
        ]
        content2, _usage2 = await _post_chat(
            client, model, corrective, sleep, max_tokens=completion_cap
        )
        try:
            drafts = parser(content2, batch)
        except ResponseParseError as exc:
            return BatchOutcome(
                batch=batch,
                drafts=[],
                failed=True,
                error=f"unparseable after corrective re-prompt: {exc}",
                model_used=model,
            )
    return BatchOutcome(batch=batch, drafts=drafts, model_used=model)


def _describe_model_failure(label: str, exc: BaseException) -> str:
    """Name the failure, separating the definitive from the transient.

    A 410 reads like an ordinary failure in `_exc_str`; it deserves a marker,
    because the right response is not to wait for the next night but to
    reconfigure the model.
    """
    if isinstance(exc, ModelGoneError):
        # Since 2026-08-12, `_post_chat` names the end of life itself and
        # raises BEFORE `raise_for_status()`: the HTTPStatusError branch below
        # therefore no longer sees the 410s coming from there. Keeping it is not
        # superstition — roadmap also catches HTTPStatusErrors raised elsewhere,
        # and a 410 would otherwise stay silent there.
        return f"{label}: {MODEL_GONE_MARKER} — HTTP {exc.status_code} {exc}"
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == HTTP_GONE:
        return f"{label}: {MODEL_GONE_MARKER} — HTTP 410 {exc.response.text[:160]}"
    return f"{label}: {_exc_str(exc)}"


async def _curate_managed_model_chain(
    client: httpx.AsyncClient,
    model: str,
    fallback_model: str | None,
    batch: ProjectBatch,
    sleep: Any,
    llm_timeout_s: float,
    disabled_models: set[str] | None,
) -> BatchOutcome:
    """Try the large compact model, then the economy fallback if necessary."""
    profiles = [(model, BIG_MODEL_FEATURE_CAP, BIG_MODEL_COMPLETION_TOKENS, False)]
    if model == DEFAULT_ROADMAP_FALLBACK_MODEL:
        profiles[0] = (
            model,
            FALLBACK_FEATURE_CAP,
            FALLBACK_COMPLETION_TOKENS,
            False,
        )
    elif fallback_model and fallback_model != model:
        profiles.append(
            (
                fallback_model,
                FALLBACK_FEATURE_CAP,
                FALLBACK_COMPLETION_TOKENS,
                True,
            )
        )

    circuit = disabled_models if disabled_models is not None else set()
    has_fallback = any(is_fallback for _, _, _, is_fallback in profiles)
    primary_error: str | None = None
    if model in circuit and has_fallback:
        profiles = [profile for profile in profiles if profile[3]]
        # The detailed reason was reported by the batch that opened the
        # circuit; here we keep at least the fact that the primary is set aside,
        # otherwise batches 2..N pass for nominal.
        primary_error = f"{model}: écarté (circuit ouvert plus tôt dans ce run)"

    errors: list[str] = []
    last_compact_size = len(batch.features)
    last_model = profiles[-1][0]
    last_was_fallback = profiles[-1][3]
    for candidate, feature_cap, completion_cap, is_fallback in profiles:
        attempt_count = 2 if is_fallback else 1
        for attempt_index in range(attempt_count):
            attempt_feature_cap = (
                feature_cap if attempt_index == 0 else min(feature_cap, FALLBACK_RETRY_FEATURE_CAP)
            )
            compact = _compact_batch(
                batch,
                feature_cap=attempt_feature_cap,
                artifact_cap=COMPACT_ARTIFACT_CAP,
            )
            last_compact_size = len(compact.features)
            last_model = candidate
            last_was_fallback = is_fallback
            try:
                async with asyncio.timeout(llm_timeout_s):
                    outcome = await _curate_llm_attempt(
                        client,
                        candidate,
                        compact,
                        sleep,
                        max_tokens=completion_cap,
                        proposer_only=True,
                    )
            except NvidiaAuthError:
                # Another size or another model will never repair the key.
                raise
            except (
                TimeoutError,
                httpx.HTTPError,
                RuntimeError,
                IndexError,
                KeyError,
                TypeError,
                ValueError,
            ) as exc:
                retry_label = f" retry {attempt_index + 1}" if attempt_index else ""
                reason = _describe_model_failure(f"{candidate}{retry_label}", exc)
                errors.append(reason)
                if not is_fallback and has_fallback:
                    circuit.add(candidate)
                    primary_error = reason
                if is_fallback and attempt_index + 1 < attempt_count:
                    continue
                break

            if outcome.failed:
                errors.append(f"{candidate}: {outcome.error}")
                if not is_fallback and has_fallback:
                    circuit.add(candidate)
                    primary_error = f"{candidate}: {outcome.error}"
                break

            outcome.shrunk = len(compact.features) < len(batch.features)
            outcome.model_used = candidate
            outcome.fallback_used = is_fallback
            outcome.primary_error = primary_error if is_fallback else None
            return outcome
    return BatchOutcome(
        batch=batch,
        drafts=[],
        failed=True,
        error="; ".join(errors) or "managed model chain failed",
        shrunk=len(batch.features) > last_compact_size,
        model_used=last_model,
        fallback_used=last_was_fallback,
        primary_error=primary_error,
    )


def _llm_attempt_schedule(
    feature_count: int,
    llm_timeout_s: float,
    *,
    force_economic: bool = False,
) -> list[tuple[int, float]]:
    """Plan the sizes/timeouts without exceeding roughly 2× the window.

    When sharing the window between retries would give less than the viable
    timeout observed on NVIDIA, we switch to economy mode: pre-shrink a large
    batch, then a single extra shrink, each with the whole window. Otherwise we
    keep the historical progressive steps.
    """
    if feature_count <= 0:
        return []

    retry_timeout = llm_timeout_s / SHRINK_MAX_RETRIES
    if force_economic or retry_timeout < MIN_LLM_WINDOW_S:
        size, final_size = _economic_batch_sizes(feature_count)
        attempts = [(size, llm_timeout_s)]
        if final_size < size:
            attempts.append((final_size, llm_timeout_s))
        return attempts

    attempts = [(feature_count, llm_timeout_s)]
    size = feature_count
    for _ in range(SHRINK_MAX_RETRIES):
        next_size = max(1, size // SHRINK_DIVISOR)
        if next_size >= size:
            break
        attempts.append((next_size, retry_timeout))
        size = next_size
    return attempts


async def curate_batch(
    client: httpx.AsyncClient,
    model: str,
    batch: ProjectBatch,
    sleep: Any = asyncio.sleep,
    llm_timeout_s: float = LLM_ATTEMPT_TIMEOUT_S,
    fallback_model: str | None = None,
    disabled_models: set[str] | None = None,
    proposer_only: bool | None = None,
) -> BatchOutcome:
    """Curate a project with a call plan bounded to roughly 2× the window.

    Normal window: full then progressive shrink, the retries sharing one window.
    Tight NVIDIA window: pre-shrink the large batches then two attempts at most,
    each with a viable window (30→10→3 becomes 10@60 s then 3@60 s). _post_chat
    stays unchanged so as not to affect EXTRACT nor domain-backfill; asyncio
    bounds each ROADMAP attempt locally.

    ``proposer_only`` exists only for the canary, and is ``None`` in production —
    routing then stays decided by membership of ``PROPOSER_ONLY_MODELS``,
    unchanged. It serves to EVALUATE a candidate in the regime it would have once
    adopted as the DRY primary, that set being derived from
    ``DEFAULT_ROADMAP_MODEL``: without it, the canary would measure a routing
    production will no longer apply on the day of the switch. An explicit
    parameter rather than a mutation of the global — `check_container_image_pins`
    forbids writing a module attribute, and it is right: a leaking monkeypatch
    would leave production routed differently from how it was.
    """
    if not batch.features:
        return BatchOutcome(batch=batch, drafts=[], model_used=model)

    routed_proposer_only = (
        proposer_only if proposer_only is not None else model in PROPOSER_ONLY_MODELS
    )
    if routed_proposer_only:
        return await _curate_managed_model_chain(
            client,
            model,
            fallback_model,
            batch,
            sleep,
            llm_timeout_s,
            disabled_models,
        )

    circuit = disabled_models if disabled_models is not None else set()
    has_fallback = bool(fallback_model and fallback_model != model)
    candidate_timeout_s = llm_timeout_s / 2 if has_fallback else llm_timeout_s

    async def finish_with_fallback(primary: BatchOutcome) -> BatchOutcome:
        if not primary.failed or not fallback_model or fallback_model == model:
            return primary
        circuit.add(model)
        fallback = await curate_batch(
            client,
            fallback_model,
            batch,
            sleep,
            candidate_timeout_s,
            fallback_model=None,
            disabled_models=circuit,
        )
        fallback.fallback_used = True
        if fallback.failed:
            fallback.error = (
                f"{model}: {primary.error or 'failed'}; "
                f"{fallback_model}: {fallback.error or 'failed'}"
            )
        return fallback

    if model in circuit and fallback_model and fallback_model != model:
        fallback = await curate_batch(
            client,
            fallback_model,
            batch,
            sleep,
            llm_timeout_s,
            fallback_model=None,
            disabled_models=circuit,
        )
        fallback.fallback_used = True
        return fallback

    # Explicitly reviewed models keep the historical full path, then switch
    # once to the configured fallback.
    attempts = _llm_attempt_schedule(len(batch.features), candidate_timeout_s)
    try:
        for size, attempt_timeout in attempts:
            slice_ = (
                batch
                if size == len(batch.features)
                else ProjectBatch(project_key=batch.project_key, features=batch.features[:size])
            )
            try:
                async with asyncio.timeout(attempt_timeout):
                    outcome = await _curate_llm_attempt(client, model, slice_, sleep)
                outcome.shrunk = size < len(batch.features)
                outcome.model_used = model
                return await finish_with_fallback(outcome)
            except TimeoutError:
                continue

        if len(batch.features) == 1:
            return await finish_with_fallback(
                BatchOutcome(
                    batch=batch,
                    drafts=[],
                    failed=True,
                    error=(
                        f"llm timeout ({candidate_timeout_s:.0f}s, "
                        f"{len(batch.features)} feature, shrink impossible)"
                    ),
                    model_used=model,
                )
            )
        final_size = attempts[-1][0]
        windows = "+".join(f"{timeout:.0f}" for _, timeout in attempts)
        return await finish_with_fallback(
            BatchOutcome(
                batch=batch,
                drafts=[],
                failed=True,
                error=(
                    f"llm timeout après shrink {len(batch.features)}→{final_size} features "
                    f"(fenêtres {windows}s)"
                ),
                model_used=model,
            )
        )
    except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as exc:
        return await finish_with_fallback(
            BatchOutcome(
                batch=batch,
                drafts=[],
                failed=True,
                error=_exc_str(exc),
                model_used=model,
            )
        )


_JUDGE_SYSTEM_PROMPT = (
    "Tu es le validateur des fusions proposées par le cureur nocturne d'une "
    "roadmap. Pour chaque fusion (source → cible), décide si la source et la "
    "cible relèvent du MÊME SUJET précis de roadmap — pas simplement du même "
    "projet, du même outil ou d'un chantier voisin. Un gotcha/learning "
    "technique ponctuel ne rejoint un chantier que si celui-ci le couvre "
    "explicitement ; deux plans ou cycles de travail distincts ne se "
    "fusionnent pas entre eux. Réponds UNIQUEMENT avec un tableau JSON : "
    '[{"i": <index>, "same_subject": true|false}] — un élément par fusion, '
    "pas de prose."
)


async def judge_merges(
    client: httpx.AsyncClient,
    model: str,
    batch: ProjectBatch,
    merges: list[CurationDraft],
    sleep: Any = asyncio.sleep,
    timeout_s: float = JUDGE_TIMEOUT_S,
) -> set[int]:
    """Indices (into `merges`) to HOLD BACK — persisted 'proposed', never auto-applied.

    Two-tier anti-dump gate (night of 2026-07-05: 10/23 aberrant merges, the
    "dump everything into one survivor" pattern). Measured over the 62 applied
    merges: neither embedding similarity nor per-target counting separates sound
    from aberrant — the discriminant is semantic, hence an LLM judge.
    FAIL-CLOSED: a transport/parse/timeout error or an index absent from the
    answer → held back; the judge's silence is never a validation.
    """
    if not merges:
        return set()
    cards = {f.id: f for f in batch.features}
    lines = []
    for i, d in enumerate(merges):
        src = cards.get(d.feature_id)
        try:
            tgt = cards.get(UUID(str(d.payload.get("into"))))
        except ValueError:
            tgt = None
        src_name = src.name if src else str(d.feature_id)
        tgt_name = tgt.name if tgt else str(d.payload.get("into"))
        lines.append(
            f'{i}. source: "{src_name}" → cible: "{tgt_name}" (raison du cureur: {d.rationale})'
        )
    messages = [
        {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]
    all_idx = set(range(len(merges)))
    try:
        async with asyncio.timeout(timeout_s):
            content, _usage = await _post_chat(
                client, model, messages, sleep, max_tokens=JUDGE_MAX_TOKENS
            )
        items = json.loads(_strip_fences(content))
        approved = {
            int(item["i"])
            for item in items
            if isinstance(item, dict) and item.get("same_subject") is True
        }
    except (
        TimeoutError,
        httpx.HTTPError,
        RuntimeError,
        KeyError,
        ValueError,
        TypeError,
    ) as exc:
        print(
            f"! judge: erreur ({_exc_str(exc)}) — {len(merges)} merges retenus (fail-closed)",
            flush=True,
        )
        return all_idx
    return all_idx - approved


# ── Apply path + CLI ──────────────────────────────────────────────────────────


async def persist_proposals(session_factory: Any, drafts: list[CurationDraft]) -> PersistResult:
    """INSERT proposals with status='proposed', deduplicating against existing rows.

    Cross-night dedup (finding of 2026-07-04): in dry the features do not move,
    so every night would re-propose the same ops. An identical row (op +
    feature_id + payload, semantic JSONB equality) is enough to skip:
    'proposed' → refresh (the id is returned, the run's wet applies it);
    'rejected' → definitive skip (no resurrection at review).
    """
    from brain_v42.db.tables import roadmap_curation_proposals  # noqa: PLC0415

    result = PersistResult()
    if not drafts:
        return result
    t = roadmap_curation_proposals
    async with session_factory() as session:
        async with session.begin():
            for draft in drafts:
                dup_stmt = (
                    sa.select(t.c.id, t.c.status)
                    .where(
                        t.c.op == draft.op,
                        t.c.feature_id == draft.feature_id,
                        t.c.payload == draft.payload,
                        t.c.status.in_(("proposed", "rejected")),
                    )
                    # asc: 'proposed' < 'rejected' — if both exist, the
                    # refresh wins over the definitive skip.
                    .order_by(t.c.status)
                    .limit(1)
                )
                row = (await session.execute(dup_stmt)).first()
                if row is not None:
                    dup_id, dup_status = row
                    if dup_status == "proposed":
                        result.refreshed.append(dup_id)
                    else:
                        result.rejected_skipped += 1
                    continue
                stmt = (
                    t.insert()
                    .values(
                        op=draft.op,
                        feature_id=draft.feature_id,
                        payload=draft.payload,
                        rationale=draft.rationale,
                        status="proposed",
                    )
                    .returning(t.c.id)
                )
                result.inserted.append((await session.execute(stmt)).scalar_one())
    return result


async def apply_proposals(
    session_factory: Any,
    proposal_ids: list[int],
    allowed_ops: tuple[str, ...] | None = None,
    project_key: str | None = None,
) -> int:
    """CLI facade applying reviewed proposals — one transaction per proposal.

    allowed_ops: in the nightly wet, WET_APPLYABLE_OPS; None (--apply-ids,
    human review) = every op. A failed post-condition rolls the proposal back
    (it stays 'proposed') and we continue.

    project_key: forwarded as the service's `project_group`, which adds the EXISTS
    on `features.project_key` — the SAME guard the MCP tools use, not a copy. The
    reviewed apply always declares it; the nightly wet does not, because its
    proposals come from the run that just produced them.
    """
    from brain_v42.services.proposal_service import (  # noqa: PLC0415
        ProposalApplyError,
        ProposalNotFoundError,
        ProposalNotProposedError,
        ProposalOperationNotAllowedError,
        ProposalService,
    )

    service = ProposalService(session_factory, None, None)
    applied = 0
    for proposal_id in dict.fromkeys(proposal_ids):
        try:
            await service.apply_roadmap_curation(
                proposal_id, allowed_ops=allowed_ops, project_group=project_key
            )
        except ProposalNotFoundError:
            # Under a declared project the guard makes a foreign row simply not
            # match, so this is also how a cross-project id arrives. The CLI
            # cannot tell "unknown" from "outside the project" here and does not
            # pretend to — but staying silent let a mistyped id read as an id
            # that was applied, which is the defect.
            if project_key is not None:
                print(
                    f"~ proposal {proposal_id} inconnue ou hors du projet "
                    f"{project_key} — rien appliqué"
                )
            continue
        except ProposalNotProposedError:
            continue
        except ProposalOperationNotAllowedError as exc:
            print(
                f"~ proposal {proposal_id} ({exc.operation}) hors allowed_ops — laissée en review"
            )
            continue
        except ProposalApplyError as exc:
            cause = exc.__cause__ or exc
            print(f"! proposal {proposal_id} ({exc.operation}) failed: {_exc_str(cause)}")
            continue
        applied += 1
    return applied


def _degradation_notice(
    model: str,
    fallback_batches: int,
    scanned: int,
    primary_errors: list[str],
) -> str | None:
    """Degradation sentence when the run was served by the fallback, else None.

    Speaks only if the fallback actually served: an alarm that fires every night
    stops being read (Dream postmortem 08-04).
    """
    if not fallback_batches or not scanned:
        return None
    cause = " ; ".join(primary_errors) if primary_errors else "cause non capturée"
    return (
        f"{DEGRADED_PREFIX} : {fallback_batches}/{scanned} batches servis par le modèle "
        f"de SECOURS, le primaire {model} n'a pas répondu — {cause}"
    )


async def record_dream_run(
    session_factory: Any,
    status: str,
    dry: bool,
    duration_s: float,
    error: str | None,
    model: str | None = None,
    thinking_tokens: int = 0,
) -> None:
    """INSERT dream_runs row for phase='roadmap'. Best-effort — never raises.

    `roadmap` is a GLOBAL phase and writes the sentinel, not a real key. Its own
    project rotation (`_rotate`) is NOT the loop's coverage and must never be
    read as one — which is precisely why this row names no project.
    """
    try:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    sa.text(
                        "INSERT INTO dream_runs "
                        "(run_date, phase, status, duration_s, error_message, "
                        "project_key, phase_dry_run, model, thinking_tokens) "
                        "VALUES (:run_date, 'roadmap', :status, :duration_s, "
                        ":error_message, :project_key, :phase_dry_run, :model, "
                        ":thinking_tokens)"
                    ),
                    {
                        "run_date": date.today(),
                        "status": status,
                        "duration_s": duration_s,
                        "error_message": error,
                        "project_key": GLOBAL_PHASE_PROJECT_KEY,
                        "phase_dry_run": dry,
                        "model": model,
                        # An INTEGER, never NULL — same contract as the extract
                        # rail, and the same single extractor behind it.
                        "thinking_tokens": thinking_tokens,
                    },
                )
    except Exception as exc:
        print(f"! warning: could not record dream_run: {exc}")


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"doit être >= 1 (reçu : {number})")
    return number


def _build_parser() -> argparse.ArgumentParser:
    """The CLI surface, built apart so a test can read it without running a night."""
    parser = argparse.ArgumentParser(
        prog="roadmap_curate",
        description="Roadmap curation (NVIDIA API).",
    )
    parser.add_argument("--limit", type=_positive_int, default=10, help="max projets à traiter")
    parser.add_argument(
        "--wet",
        action="store_true",
        help="propose puis applique les proposals validées de ce run",
    )
    parser.add_argument(
        "--apply-ids",
        default=None,
        help='apply des proposals reviewées (ex: "3,4") — incompatible avec --wet',
    )
    # No default, deliberately: a default would put the guard back to sleep. The
    # reviewed apply is driven by hand from a morning review, where a mistyped id
    # is the expected human error and 25 other projects are one digit away —
    # ticket e9b2faf4, defect 2.
    parser.add_argument(
        "--project-key",
        default=None,
        help="projet dont les proposals peuvent être appliquées — requis avec --apply-ids",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            f"défaut: env {_ROADMAP_MODEL_VAR}, puis {DEFAULT_ROADMAP_MODEL} en dry "
            f"ou {DEFAULT_WET_ROADMAP_MODEL} en wet"
        ),
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"défaut: env BRAIN_NVIDIA_BASE_URL puis {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--budget-seconds",
        type=float,
        default=NIGHT_BUDGET_S,
        help=(
            "plus aucun nouveau batch après ce seuil — fin propre bien avant "
            f"le SIGTERM shell (défaut: {NIGHT_BUDGET_S:.0f}s)"
        ),
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.wet and args.apply_ids is not None:
        parser.error("--wet et --apply-ids sont incompatibles")
    if args.apply_ids is not None and args.project_key is None:
        parser.error("--apply-ids exige --project-key (garde de projet, ticket e9b2faf4)")

    load_env_file(_ENV_FILE)

    import os  # noqa: PLC0415

    api_key = os.environ.get(_API_KEY_VAR, "")
    if not api_key and args.apply_ids is None:
        print(
            f"{_API_KEY_VAR} manquant — renseigne-le dans {_ENV_FILE}.",
            file=sys.stderr,
        )
        return 2

    model = (
        args.model
        or os.environ.get(_ROADMAP_MODEL_VAR)
        or (DEFAULT_WET_ROADMAP_MODEL if args.wet else DEFAULT_ROADMAP_MODEL)
    )
    if args.wet and model in AUTO_APPLY_MODELS:
        default_fallback_model = (
            DEFAULT_WET_ROADMAP_MODEL
            if model == DEFAULT_WET_ROADMAP_FALLBACK_MODEL
            else DEFAULT_WET_ROADMAP_FALLBACK_MODEL
        )
    else:
        default_fallback_model = DEFAULT_ROADMAP_FALLBACK_MODEL
    fallback_model = os.environ.get(_ROADMAP_FALLBACK_MODEL_VAR) or default_fallback_model
    if fallback_model == model:
        # curate_batch treats a fallback equal to the primary as NO fallback
        # (has_fallback=False), silently. The case only happens through an
        # override — the constants are kept distinct by test_roadmap_model_chain
        # — and it deserves noise: a one-link chain that believes it has two is
        # the failure of 2026-08-28 (a dead fallback discovered mid-night), only
        # worse, because here nobody even configured it.
        print(
            f"WARN secours identique au primaire ({model}) : la chaîne roadmap "
            "tourne à UN seul maillon (has_fallback=False)"
        )
    base_url = args.base_url or os.environ.get("BRAIN_NVIDIA_BASE_URL") or DEFAULT_BASE_URL

    if args.wet and model in AUTO_APPLY_MODELS and os.environ.get(_AUTO_APPLY_ACK_VAR) != "yes":
        # FAIL-CLOSED, and the twin of the refusal just below: that one protects
        # against a WET night that would do NOTHING, this one against a WET night
        # that would do TOO MUCH. Both are decided here because this is the only
        # place that knows the EFFECTIVE primary — the environment overrides a
        # default that itself depends on `--wet`.
        #
        # Why a guard at all when the allowlist is the point of `--wet`: the two
        # facts arrive from different places and neither mentions the other.
        # `BRAIN_DREAM_ROADMAP_DRY_RUN` lives in a systemd drop-in, the primary in
        # ANOTHER drop-in, and the allowlist in this file. On 2026-09-03 a canary
        # legitimately put `DEFAULT_WET_ROADMAP_MODEL` in the DRY chain: from that
        # moment one word flipped the night from proposing to archiving, and
        # nothing in the repository could see it coming.
        print(
            f"! REFUS : le primaire effectif {model} est dans AUTO_APPLY_MODELS. "
            f"En --wet il APPLIQUE ({', '.join(WET_APPLYABLE_OPS)}) au lieu de "
            f"proposer. Poser {_AUTO_APPLY_ACK_VAR}=yes pour confirmer que c'est "
            "voulu, ou remettre BRAIN_DREAM_ROADMAP_DRY_RUN=true.",
            file=sys.stderr,
        )
        return 2

    if args.wet and model not in AUTO_APPLY_MODELS:
        mode = "proposer-only" if model in PROPOSER_ONLY_MODELS else "hors allowlist auto-apply"
        print(
            f"! {model} reste review-only ({mode}) : --wet ignoré "
            "(sélectionne un modèle canaryé pour auto-appliquer)."
        )
        args.wet = False

    return asyncio.run(_run(args, api_key, model, base_url, fallback_model=fallback_model))


async def _run(
    args: Any,
    api_key: str,
    model: str,
    base_url: str,
    *,
    fallback_model: str | None = None,
    clock: Any = time.monotonic,
) -> int:
    from pydantic import ValidationError  # noqa: PLC0415

    from brain_v42.config import Settings  # noqa: PLC0415
    from brain_v42.db.engine import get_session_factory  # noqa: PLC0415

    try:
        Settings()  # type: ignore[call-arg]  # validate config early
    except ValidationError as exc:
        print(f"Config invalide: {exc}", file=sys.stderr)
        return 2

    sf = get_session_factory()
    t0 = clock()
    any_failed = False
    error_msg: str | None = None

    # --apply-ids mode: no LLM, apply reviewé (toutes ops).
    if args.apply_ids is not None:
        try:
            ids = [int(x.strip()) for x in args.apply_ids.split(",") if x.strip()]
        except ValueError:
            print(
                "--apply-ids doit être une liste d'entiers séparés par des virgules",
                file=sys.stderr,
            )
            return 1
        applied = await apply_proposals(sf, ids, allowed_ops=None, project_key=args.project_key)
        duration = clock() - t0
        print(f"apply: {applied} appliqués")
        await record_dream_run(sf, "done", dry=False, duration_s=duration, error=None)
        return 0

    # Propose mode (dry or wet) — persist AND apply incrementally, batch by
    # batch (night of 2026-07-05: SIGTERM at 20 m in the middle of batch 7/10,
    # the terminal apply never ran → 24 'proposed' proposals never applied). The
    # night budget cuts BEFORE the shell SIGTERM: a clean end, the dream_runs row
    # always written, and the rotation will serve the remaining projects.
    # Progress log with flush=True (stdout is block-buffered under >>).
    batches = await fetch_project_batches(sf, args.limit)
    if not batches:
        print("Aucune feature vivante — rien à curer.", flush=True)
        await record_dream_run(sf, "done", dry=not args.wet, duration_s=clock() - t0, error=None)
        return 0

    http_client = httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0),
    )

    budget_s = float(getattr(args, "budget_seconds", NIGHT_BUDGET_S))
    all_ids: list[int] = []
    refreshed_ids: list[int] = []
    held_ids: list[int] = []
    applied_total = 0
    remaining_cap = MAX_PROPOSALS_PER_NIGHT
    scanned = 0
    skipped = 0
    failed = 0
    total = len(batches)
    disabled_models: set[str] = set()
    # Silent degradation: a fallback that succeeds made the primary's failure
    # invisible (qwen 80B died on 2026-07-27, ten green nights).
    fallback_batches = 0
    primary_errors: list[str] = []
    last_model_used: str | None = None
    try:
        for i, batch in enumerate(batches, 1):
            if remaining_cap <= 0:
                print(
                    f"! cap {MAX_PROPOSALS_PER_NIGHT} proposals/nuit épuisé — "
                    f"{total - i + 1} projets non traités ce soir "
                    f"(le cycle de rotation les resservira)",
                    flush=True,
                )
                break
            elapsed = clock() - t0
            if elapsed > budget_s:
                print(
                    f"! budget nuit épuisé ({elapsed:.0f}s > {budget_s:.0f}s) — "
                    f"{total - i + 1} projet(s) non traité(s) ce soir "
                    f"(le cycle de rotation les resservira)",
                    flush=True,
                )
                break
            t_batch = time.monotonic()
            # Fair-share LLM window: a large project no longer eats the share
            # of the next ones (night of 2026-07-10: red 383 s → 5 projects
            # deferred).
            outcome = await curate_batch(
                http_client,
                model,
                batch,
                llm_timeout_s=batch_llm_window(budget_s, elapsed, total - i + 1),
                fallback_model=fallback_model,
                disabled_models=disabled_models,
            )
            scanned += 1
            if outcome.model_used:
                last_model_used = outcome.model_used
            if outcome.fallback_used:
                fallback_batches += 1
                if outcome.primary_error and outcome.primary_error not in primary_errors:
                    primary_errors.append(outcome.primary_error)
            if outcome.failed:
                failed += 1
                any_failed = True
                error_msg = outcome.error
                print(f"! [{i}/{total}] {batch.project_key} failed: {outcome.error}", flush=True)
                continue
            outcome_model = outcome.model_used
            displayed_model = outcome_model or "unknown"
            auto_apply_outcome = (
                args.wet and outcome_model is not None and outcome_model in AUTO_APPLY_MODELS
            )
            if args.wet and not auto_apply_outcome:
                print(
                    f"! [{i}/{total}] {batch.project_key}: model={displayed_model} "
                    "non autorisé à auto-appliquer — proposals conservées pour review",
                    flush=True,
                )
            kept, noops = drop_noops(outcome.drafts, batch)
            allowance = batch_allowance(remaining_cap, total - i + 1)
            to_persist, cap_dropped = kept[:allowance], kept[allowance:]
            # Two-tier anti-dump gate (wet): the merges the judge holds back
            # are persisted 'proposed' (for the morning review) but NEVER
            # auto-applied — fail-closed inside judge_merges.
            held_drafts: list[CurationDraft] = []
            if auto_apply_outcome:
                assert outcome_model is not None
                merge_pos = [j for j, d in enumerate(to_persist) if d.op == "merge"]
                if merge_pos:
                    held_sub = await judge_merges(
                        http_client, outcome_model, batch, [to_persist[j] for j in merge_pos]
                    )
                    held_set = {merge_pos[k] for k in held_sub}
                    if held_set:
                        held_drafts = [to_persist[j] for j in sorted(held_set)]
                        to_persist = [d for j, d in enumerate(to_persist) if j not in held_set]
            res = await persist_proposals(sf, to_persist)
            res_held = await persist_proposals(sf, held_drafts) if held_drafts else PersistResult()
            remaining_cap -= len(res.inserted) + len(res_held.inserted)
            all_ids.extend(res.inserted)
            refreshed_ids.extend(res.refreshed)
            held_ids.extend(res_held.inserted + res_held.refreshed)
            if held_drafts:
                print(
                    f"! judge: {len(held_drafts)} merge(s) retenu(s) pour review "
                    f"(proposals {res_held.inserted + res_held.refreshed})",
                    flush=True,
                )
            if not res.inserted and not res.refreshed and not res_held.inserted:
                skipped += 1
            if cap_dropped:
                print(
                    f"! projet {batch.project_key}: {len(cap_dropped)} proposals "
                    f"au-delà de la part de cap ({allowance}) — droppées "
                    f"(pas de troncature silencieuse)",
                    flush=True,
                )
            # Apply PER BATCH: a SIGTERM loses only the batch in flight.
            if auto_apply_outcome and (res.inserted or res.refreshed):
                applied_total += await apply_proposals(
                    sf, res.inserted + res.refreshed, allowed_ops=WET_APPLYABLE_OPS
                )
            print(
                f"[{i}/{total}] {batch.project_key}: "
                f"{len(outcome.drafts)} drafts, {len(noops)} no-op, "
                f"{len(res.refreshed)} dup, {res.rejected_skipped} rej-skip, "
                f"{len(cap_dropped)} cap-drop, {len(res.inserted)} persistées "
                f"({time.monotonic() - t_batch:.0f}s"
                f"{' · shrunk' if outcome.shrunk else ''}"
                f" · model={displayed_model}"
                f"{' · fallback' if outcome.fallback_used else ''})",
                flush=True,
            )
    finally:
        await http_client.aclose()

    print(
        f"{scanned} projets scannés, {len(all_ids)} proposals, "
        f"{skipped} sans proposition, {failed} failed",
        flush=True,
    )
    if all_ids:
        print(f"proposal ids: {all_ids}", flush=True)
    if refreshed_ids:
        print(f"déjà proposées (refresh): {refreshed_ids}", flush=True)
    if held_ids:
        print(f"retenues par le juge (review matinale): {held_ids}", flush=True)
    if args.wet:
        print(f"wet: {applied_total} appliqués (ops {WET_APPLYABLE_OPS})", flush=True)

    degraded = _degradation_notice(model, fallback_batches, scanned, primary_errors)
    if degraded:
        print(f"! {degraded}", flush=True)

    duration = clock() - t0
    status = "fail" if any_failed else "done"
    # `degraded` travels in error_message WITHOUT touching the status: the
    # briefing reads only rows with status != 'done' for its "Last failure"
    # block, so the degradation stays queryable without raising an alarm over
    # behaviour that broke nothing.
    await record_dream_run(
        sf,
        status=status,
        dry=not args.wet,
        duration_s=duration,
        error=error_msg or degraded,
        model=last_model_used,
    )
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
