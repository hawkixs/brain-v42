"""Roadmap curation — proposition auditée et auto-apply gardé (spec 2026-07-04 §3).

Batch par projet : features vivantes (statut ∉ done/archived, non mergées)
+ digest des artifacts récents (titre, type, date — PAS les corps), envoyé
au LLM (NVIDIA API, JSON strict SANS tools — squelette exact de
ticket_extract). Quatre ops auditables : merge, archive, status, rename.

Garde-fous durs :
- pinned : seule l'op `status` est proposable ;
- done/archived : hors batch par construction (intouchables) ;
- merge intra-projet uniquement, `into` doit être dans le batch ;
- cap MAX_PROPOSALS_PER_NIGHT proposals/nuit (drop loggé, jamais silencieux).

Régime agressif (2026-07-04 soir) : --wet applique les QUATRE ops uniquement
si le modèle ayant produit le batch est dans AUTO_APPLY_MODELS
(WET_APPLYABLE_OPS = VALID_OPS, merge/rename inclus) ; --apply-ids reste
l'apply reviewé sans LLM.

Usage:
    python -m scripts.roadmap_curate [--limit 10]        # propose (dry)
    python -m scripts.roadmap_curate --limit 10 --wet    # propose + apply (toutes ops)
    python -m scripts.roadmap_curate --apply-ids "3,4"   # apply reviewé, sans LLM
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
from brain_v42.services.proposal_service import PostConditionError as PostConditionError

_API_KEY_VAR = "BRAIN_NVIDIA_API_KEY"
_ROADMAP_MODEL_VAR = "BRAIN_NVIDIA_ROADMAP_MODEL"
_ROADMAP_FALLBACK_MODEL_VAR = "BRAIN_NVIDIA_ROADMAP_FALLBACK_MODEL"
_ENV_FILE = Path.home() / ".config" / "brain-v42" / "nvidia.env"
# qwen/qwen3-next-80b-a3b-instruct a atteint son EOL fournisseur le
# 2026-07-27 (HTTP 410 Gone) et le secours 8B a servi dix nuits en silence.
# Remplaçant choisi par canary du 2026-08-05 sur le VRAI prompt, 3 batches
# réels : deepseek-v4-flash 3/3 valides à 16,6 s/batch — plus rapide que le
# secours 8B lui-même (21,2 s). Écartés : llama-3.1-70b (3/3 mais 117,5 s/batch,
# soit ~1175 s sur 10 projets, au-delà du budget nuit de 720 s),
# nemotron-super-49b-v1.5 et nemotron-3-nano-30b (JSON illisible après
# re-prompt correctif), kimi-k2.6 et nemotron-nano-3-30b (404 malgré leur
# présence dans /v1/models), minimax-m3 (timeout), mistral-medium-3.5 (timeout).
# 2026-08-16 : deepseek-v4-flash est mort à son tour (HTTP 410), neuf jours
# après le canary qui l'avait choisi. Remplaçant retenu sur DEUX mesures, pas
# une, parce que la première a failli faire choisir le mauvais.
#
# 1. VITESSE ET FORME (canary apparié, 3 batches réels, régime proposer-only) :
#    mistral-nemotron 3/3 valides, 12-20 s/batch — 126-204 s sur les dix
#    projets, contre 720 s de budget nuit. Écartés : deepseek-v4-flash-0731,
#    le snapshot daté de la famille morte, 3/3 valides mais 69,3 s/batch soit
#    693 s — 96 % du budget, et QUATRE FOIS plus lent que l'alias qu'il
#    remplace (16,6 s mesurés le 08-05). Un pin daté n'hérite pas du profil de
#    son alias. nemotron-3.5-lightning-30b : 2/3 valides.
#
# 2. QUALITÉ DU CONTENU, qui a renversé le classement. Le triplet mesuré par
#    le canary — validité, secondes, NOMBRE de propositions — ne classe rien :
#    sur trois runs des mêmes batches, mistral-nemotron a rendu 31 puis 21
#    propositions et gpt-oss-20b 29 puis 13. L'écart entre candidats est plus
#    petit que le bruit d'un seul. Jugement en aveugle du CONTENU (modèles
#    anonymisés, trois lentilles, accusations réfutées en adverse) :
#    mistral-nemotron 48/100, gpt-oss-20b 35, llama-3.1-8b 10.
#
#    Le secours 8B est donc le PIRE des trois sur le fond alors qu'il est le
#    plus rapide — recompté à la main : 9 rationales vides, 2 merges vers une
#    cible qu'il archive dans le même lot, 2 renames vers la chaîne identique,
#    et sept runs orchestrator fondus dans le plus ANCIEN d'entre eux au motif
#    que « r202 est une étape de r138 ». Il reste secours et ne devient pas
#    primaire : voir tests/unit/test_roadmap_model_chain.py.
DEFAULT_ROADMAP_MODEL = "mistralai/mistral-nemotron"
# Secours remplacé le 2026-08-29 : le 8B est mort en 410 le 2026-08-26 (nuits
# des 27 et 28 en fail, sonde GONE). gpt-oss-20b est le seul vivant 100 % porté
# sur le vrai prompt à travers trois canaries (08-11, 08-16, 08-29 : 3/3 à
# chaque fois) et jugé en aveugle au-dessus du mort qu'il remplace (35/100
# contre 10). Ses 74,5 s/batch mesurées valent pour le régime PRIMAIRE à pleins
# caps ; en secours il tourne aux caps réduits de FALLBACK_*. deepseek-v4-flash
# -0731 écarté : 69,3 s/batch le 08-16, famille morte deux fois en un mois,
# contenu jamais jugé.
DEFAULT_ROADMAP_FALLBACK_MODEL = "openai/gpt-oss-20b"
# Paire WET remplacée le 2026-08-29 : llama-3.3-70b (canary strict du
# 2026-07-14) est mort en 410 entre les nuits du 27 et du 28 août — maillon
# DORMANT côté roadmap, la phase tournant en DRY ; sans extract qui partageait
# ce modèle, personne ne l'aurait vu mourir. Le secours d'hier devient
# primaire : nemotron-3-super-120b-a12b, 3/3 valides, 31 propositions,
# 54,9 s/batch (549 s projetées sur dix projets, budget 720 s) au canary du
# 08-29 — le plus fort des vivants mesurés sur ce prompt. gpt-oss-120b prend
# le poste de secours : 3/3 valides et 39 propositions le 08-11, lent
# (182 s/batch à pleins caps) mais VALIDE, sur un poste que
# test_roadmap_model_chain exige distinct et que le killswitch DRY laisse
# dormant. Le défaut dry reste économique/proposer-only ; --wet choisit ce
# modèle reviewé lorsque l'opérateur n'en configure pas explicitement un autre.
DEFAULT_WET_ROADMAP_MODEL = "nvidia/nemotron-3-super-120b-a12b"
DEFAULT_WET_ROADMAP_FALLBACK_MODEL = "openai/gpt-oss-120b"
AUTO_APPLY_MODELS = frozenset({DEFAULT_WET_ROADMAP_MODEL, DEFAULT_WET_ROADMAP_FALLBACK_MODEL})
PROPOSER_ONLY_MODELS = frozenset({DEFAULT_ROADMAP_MODEL, DEFAULT_ROADMAP_FALLBACK_MODEL})
# HTTP 410 = le fournisseur a retiré le modèle (EOL). Aucun retry, aucune
# autre taille de batch ne le réparera : seul un changement de configuration
# le peut. À distinguer d'un 503 « occupé », qui est transitoire.
HTTP_GONE = 410
MODEL_GONE_MARKER = "MODÈLE ABSENT CHEZ LE FOURNISSEUR"

VALID_OPS = ("merge", "archive", "status", "rename")
# 'archived' exclu : l'op `archive` existe pour ça.
PROPOSABLE_STATUSES = ("planned", "research", "design", "building", "deployed", "done")
# Régime agressif (2026-07-04 soir, décision Armand) : le wet applique les
# QUATRE ops, merge/rename inclus — la roadmap est peu consommée, le coût
# d'une curation erronée est faible, et Claude valide les applications au
# check matinal. Remplace le rollout §4 (« QUE archive/status »).
WET_APPLYABLE_OPS = VALID_OPS
MAX_FEATURES_PER_PROJECT = 30
MAX_ARTIFACTS_PER_FEATURE = 10
MAX_PROPOSALS_PER_NIGHT = 40
# Le prompt consolidateur produit des réponses longues (batch brain-v42
# tronqué à 4096 au premier run wet 2026-07-04, char 12160) — 2× de marge.
MAX_COMPLETION_TOKENS = 8192
# Les petits batches issus du shrink n'ont pas besoin de réserver 8k tokens
# sur le provider gratuit. Paliers simples : 2k pour ≤3 features, 4k pour le
# batch économique ≤10, 8k uniquement au-delà (historique brain-v42 à 30).
MIN_COMPLETION_TOKENS = 2048
BALANCED_COMPLETION_TOKENS = 4096
# Profil borné pour les modèles gratuits gérés par ROADMAP : Qwen 80B MoE
# reste le modèle principal, mais ne reçoit que le contexte utile à une
# décision courte. Llama 8B prend le relais sur indisponibilité du premier.
BIG_MODEL_FEATURE_CAP = 3
FALLBACK_FEATURE_CAP = 3
FALLBACK_RETRY_FEATURE_CAP = 2
COMPACT_ARTIFACT_CAP = 3
BIG_MODEL_COMPLETION_TOKENS = 512
FALLBACK_COMPLETION_TOKENS = 1024
# Plafond PAR TENTATIVE LLM d'un batch (nuit 2026-07-05 : red a brûlé ~9 min
# en ReadTimeout×3 sur le même payload avant d'échouer). Couvre le premier
# ReadTimeout httpx (read=180 s) + le début du retry — au-delà, on rétrécit.
LLM_ATTEMPT_TIMEOUT_S = 200.0
# Shrink PROGRESSIF sur timeout LLM. L'ancien ÷2-une-fois (30→15) échouait la
# phase quand le shrink à 15 timeout aussi (nuits NIM lentes 2026-07-06
# red-shrik, 2026-07-07 brain-v42). On retente désormais par paliers
# ÷SHRINK_DIVISOR (30→10→3) jusqu'à un plancher d'1 feature : une nuit lente
# pose un petit batch au lieu d'échouer, la rotation ressert le reste. Les
# SHRINK_MAX_RETRIES paliers PARTAGENT une seule fenêtre LLM_ATTEMPT_TIMEOUT_S
# (LLM_ATTEMPT_TIMEOUT_S / SHRINK_MAX_RETRIES chacun) → total LLM par batch
# ≈ 2×LLM_ATTEMPT_TIMEOUT_S, IDENTIQUE au worst-case legacy : la marge sous le
# SIGTERM 20m de dream.sh (cf. NIGHT_BUDGET_S) est préservée.
SHRINK_DIVISOR = 3
SHRINK_MAX_RETRIES = 2
ECONOMIC_FEATURE_CAP = MAX_FEATURES_PER_PROJECT // SHRINK_DIVISOR
# Juge des merges (gate anti-dump two-tier) : appel court, réponse compacte.
JUDGE_TIMEOUT_S = 90.0
JUDGE_MAX_TOKENS = 2048
# Plus AUCUN nouveau batch après ce seuil — fin propre (record_dream_run
# écrit) bien avant le SIGTERM shell à 20 m ; pire cas résiduel par batch :
# full + SHRINK_MAX_RETRIES paliers partageant UNE fenêtre LLM_ATTEMPT_TIMEOUT_S
# = 2×200 s au total + juge 90 s + persist ≈ 8 m ⇒ 12 m + 8 m < 20 m.
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
    # True si la réponse porte sur moins de features que le batch d'origine,
    # que le shrink ait eu lieu avant ou après la première tentative LLM.
    shrunk: bool = False
    # Modèle ayant produit la réponse (ou dernier modèle tenté sur échec).
    model_used: str | None = None
    fallback_used: bool = False
    # Panne du modèle PRIMAIRE, conservée même quand le secours réussit.
    # Sans ce champ, un run entièrement servi par le secours est indiscernable
    # d'un run nominal (qwen 80B mort le 2026-07-27, découvert le 08-05 après
    # dix nuits vertes).
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
    """Digest une ligne — titre/type/date, jamais les corps complets."""
    base = f"{created_at.date().isoformat()} [{artifact_type}] {title}"
    if artifact_type == "plan" and plan_status:
        base += f" (plan {plan_status})"
    return base


def render_batch(batch: ProjectBatch) -> str:
    lines = [f"Projet: {batch.project_key} — {len(batch.features)} features vivantes"]
    for f in batch.features:
        pin = " [PINNED — seule l'op status est permise]" if f.pinned else ""
        lines.append(f"\n- feature_id: {f.id}\n  nom: {f.name}\n  statut: {f.status}{pin}")
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
    """Copie compacte d'un batch, sans muter les cartes chargées depuis la DB."""
    return ProjectBatch(
        project_key=batch.project_key,
        features=[
            FeatureCard(
                id=feature.id,
                name=feature.name,
                status=feature.status,
                pinned=feature.pinned,
                artifacts=list(feature.artifacts[:artifact_cap]),
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
    # Gardes anti-chaîne (prompt agressif 2026-07-04) : une même réponse ne
    # peut ni fusionner deux fois la même feature, ni fusionner dans une
    # survivante elle-même fusionnée — appliquer A→B puis B→C échouerait
    # les artifacts de A sur B archivée (l'apply suit l'ordre des ids).
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
    """Garder uniquement les items sûrs d'une réponse qui ne sera pas appliquée.

    Le tableau JSON reste obligatoire. Chaque item est ensuite validé avec le
    parseur strict ; un item invalide est ignoré sans sacrifier ses voisins.
    Les chaînes de merges sont filtrées une fois les items valides réunis.
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
    """Écarte les proposals sans effet — status identique, rename identique.

    Premier run réel (2026-07-04) : 10/40 proposals étaient des no-ops qui
    brûlaient le cap. Filtre d'effet post-validation ; on ne raise pas (un
    raise déclencherait le re-prompt correctif LLM pour un simple no-op).
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
SELECT f.id, f.name, f.status, COALESCE(f.pinned, false) AS pinned
FROM features f
LEFT JOIN feature_artifacts fa ON fa.feature_id = f.id
WHERE f.project_key = :pk
  AND f.status NOT IN ('done', 'archived')
  AND f.merged_into IS NULL
GROUP BY f.id, f.name, f.status, f.pinned
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
    """Fenêtre glissante déterministe sur la liste (triée) des projets.

    Avance de `limit` positions par jour → cycle complet en ⌈n/limit⌉
    nuits, à liste stable ; si elle change entre nuits la couverture
    reste bornée (l'offset avance quand même chaque jour). Sans
    rotation, ORDER BY + LIMIT scannait les 10 premiers projets
    alphabétiques chaque nuit et jamais les 16 autres (2026-07-04).
    """
    if not keys:
        return []
    offset = (day_ordinal * limit) % len(keys)
    rotated = keys[offset:] + keys[:offset]
    return rotated[:limit]


def batch_allowance(remaining_cap: int, remaining_batches: int) -> int:
    """Part équitable du cap restant pour le prochain batch (ceil).

    Le ceil redistribue les slots non consommés par les batches
    précédents. Sans fair-share, la troncature globale [:cap] en ordre
    de batch servait 3 projets sur 26 (finding 2026-07-04).
    """
    if remaining_batches <= 0 or remaining_cap <= 0:
        return 0
    return -(-remaining_cap // remaining_batches)


# Plancher de la fenêtre LLM fair-share : un appel projet normal prend
# ~35-45s (nuit 2026-07-10 : experteam 42s, mrc-rag 45s) — en dessous de
# 60s on ne servirait plus personne. Le dépassement de budget que le
# plancher autorise reste borné par le hard-break de _run (et la marge
# SIGTERM s'AMÉLIORE : dernier batch ≤ 2×plancher + juge ≪ worst-case
# legacy 2×LLM_ATTEMPT_TIMEOUT_S).
MIN_LLM_WINDOW_S = 60.0


def batch_llm_window(budget_s: float, elapsed_s: float, remaining_batches: int) -> float:
    """Fenêtre LLM fair-share du prochain batch (sœur TEMPS de batch_allowance).

    Nuit 2026-07-10 : le projet red a consommé 383s (fenêtre pleine +
    paliers de shrink) → budget 720s épuisé au 5e projet, 5 projets
    reportés à la rotation. Part = budget restant / batches restants ;
    fenêtre = part/2 car un batch consomme ≈ 2 fenêtres (tentative pleine
    + paliers de shrink partagés, cf. curate_batch), bornée
    [MIN_LLM_WINDOW_S, LLM_ATTEMPT_TIMEOUT_S]. Le slack des batches
    rapides roule vers les suivants (elapsed croît moins vite → parts
    suivantes plus larges).
    """
    if remaining_batches <= 0:
        return LLM_ATTEMPT_TIMEOUT_S
    fair_share = max(0.0, budget_s - elapsed_s) / remaining_batches
    return min(LLM_ATTEMPT_TIMEOUT_S, max(MIN_LLM_WINDOW_S, fair_share / 2))


def _economic_batch_sizes(feature_count: int) -> tuple[int, int]:
    """Tailles initiale/fallback du mode NVIDIA économique."""
    if feature_count <= 0:
        return 0, 0
    first_size = min(feature_count, ECONOMIC_FEATURE_CAP)
    return first_size, max(1, first_size // SHRINK_DIVISOR)


async def fetch_project_batches(
    session_factory: Any, limit: int, day_ordinal: int | None = None
) -> list[ProjectBatch]:
    """Batchs par projet : features vivantes (cap 30) + digests (cap 10/feature).

    La fenêtre de projets tourne chaque jour (rotate_keys) pour que tous
    les projets soient couverts en ⌈n/limit⌉ nuits. L'ordre des features
    tourne aussi : en fenêtre NVIDIA serrée, le pré-shrink ne doit pas servir
    éternellement les mêmes 10 cartes récentes.
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
    """Cap de sortie NVIDIA selon la taille du batch effectivement envoyé."""
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
    """Une tentative LLM complète : appel + re-prompt correctif sur parse error."""
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
    """Nomme la panne, en séparant le définitif du transitoire.

    Un 410 se lit comme un échec ordinaire dans `_exc_str` ; il mérite un
    marqueur, parce que la conduite à tenir n'est pas d'attendre la nuit
    suivante mais de reconfigurer le modèle.
    """
    if isinstance(exc, ModelGoneError):
        # Depuis le 2026-08-12, `_post_chat` nomme lui-même la fin de vie et
        # lève AVANT `raise_for_status()` : la branche HTTPStatusError ci-dessous
        # ne voit donc plus les 410 venus de là. La garder n'est pas de la
        # superstition — roadmap attrape aussi des HTTPStatusError levées
        # ailleurs, et un 410 y resterait sinon muet.
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
    """Essayer le gros modèle compact, puis le secours économique si nécessaire."""
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
        # Le motif détaillé a été rapporté par le batch qui a ouvert le
        # circuit ; ici on conserve au moins le fait que le primaire est
        # écarté, sinon les batches 2..N passent pour du nominal.
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
                # Une autre taille ou un autre modèle ne réparera jamais la clé.
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
    """Planifier les tailles/délais sans dépasser environ 2× la fenêtre.

    Quand partager la fenêtre entre les retries donnerait moins que le délai
    viable observé sur NVIDIA, on passe en mode économique : pré-shrink d'un
    gros batch, puis un seul shrink supplémentaire, chacun avec la fenêtre
    entière. Sinon on conserve les paliers progressifs historiques.
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
    """Curer un projet avec un plan d'appels borné à environ 2× la fenêtre.

    Fenêtre normale : full puis shrink progressif, les retries partageant une
    fenêtre. Fenêtre serrée NVIDIA : pré-shrink des gros batches puis deux
    tentatives maximum avec une fenêtre viable chacune (30→10→3 devient
    10@60s puis 3@60s). _post_chat reste inchangé pour ne pas affecter EXTRACT
    ni domain-backfill ; asyncio borne localement chaque tentative ROADMAP.

    ``proposer_only`` n'existe que pour le canary, et vaut ``None`` en production —
    le routage reste alors décidé par l'appartenance à ``PROPOSER_ONLY_MODELS``,
    inchangé. Il sert à ÉVALUER un candidat dans le régime qu'il aurait une fois
    adopté comme primaire DRY, cet ensemble étant dérivé de
    ``DEFAULT_ROADMAP_MODEL`` : sans lui, le canary mesurerait un routage que la
    production n'appliquera plus le jour de la bascule. Un paramètre explicite
    plutôt qu'une mutation du global — `check_container_image_pins` interdit
    d'écrire un attribut de module, et il a raison : un monkeypatch qui fuite
    laisserait la production routée autrement qu'elle ne l'était.
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

    # Les modèles explicitement reviewés gardent le chemin full historique,
    # puis basculent une seule fois vers le fallback configuré.
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
    """Indices (dans `merges`) à RETENIR — persistés 'proposed', jamais auto-appliqués.

    Gate anti-dump two-tier (nuit 2026-07-05 : 10/23 merges aberrants, pattern
    « tout déverser dans un survivant »). Mesuré sur les 62 merges appliqués :
    ni la similarité embedding ni le comptage par cible ne séparent sains et
    aberrants — le discriminant est sémantique, donc juge LLM. FAIL-CLOSED :
    erreur transport/parse/timeout ou index absent de la réponse → retenu ;
    le silence du juge ne vaut jamais validation.
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
    """INSERT proposals status='proposed', en dédupliquant contre l'existant.

    Dedup inter-nuits (finding 2026-07-04) : en dry les features ne bougent
    pas, chaque nuit re-proposerait les mêmes ops. Une row identique
    (op + feature_id + payload, égalité JSONB sémantique) suffit à skipper :
    'proposed' → refresh (l'id est retourné, le wet du run l'applique) ;
    'rejected' → skip définitif (pas de résurrection en review).
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
                    # asc : 'proposed' < 'rejected' — si les deux existent,
                    # le refresh gagne sur le skip définitif.
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
) -> int:
    """CLI facade applying reviewed proposals — one transaction per proposal.

    allowed_ops : en wet nocturne, WET_APPLYABLE_OPS ; None (--apply-ids,
    review humaine) = toutes les ops. Une post-condition en échec rollback
    la proposal (elle reste 'proposed') et on continue.
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
            await service.apply_roadmap_curation(proposal_id, allowed_ops=allowed_ops)
        except (ProposalNotFoundError, ProposalNotProposedError):
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
    """Phrase de dégradation quand le run a été servi par le secours, sinon None.

    Ne parle que si le secours a réellement servi : une alarme qui se
    déclenche toutes les nuits cesse d'être lue (postmortem Dream 08-04).
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
                        "project_key, phase_dry_run, model) "
                        "VALUES (:run_date, 'roadmap', :status, :duration_s, "
                        ":error_message, :project_key, :phase_dry_run, :model)"
                    ),
                    {
                        "run_date": date.today(),
                        "status": status,
                        "duration_s": duration_s,
                        "error_message": error,
                        "project_key": GLOBAL_PHASE_PROJECT_KEY,
                        "phase_dry_run": dry,
                        "model": model,
                    },
                )
    except Exception as exc:
        print(f"! warning: could not record dream_run: {exc}")


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"doit être >= 1 (reçu : {number})")
    return number


def main() -> int:
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
    args = parser.parse_args()

    if args.wet and args.apply_ids is not None:
        parser.error("--wet et --apply-ids sont incompatibles")

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
    base_url = args.base_url or os.environ.get("BRAIN_NVIDIA_BASE_URL") or DEFAULT_BASE_URL

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
        applied = await apply_proposals(sf, ids, allowed_ops=None)
        duration = clock() - t0
        print(f"apply: {applied} appliqués")
        await record_dream_run(sf, "done", dry=False, duration_s=duration, error=None)
        return 0

    # Propose mode (dry ou wet) — persist ET apply incrémentaux batch par
    # batch (nuit 2026-07-05 : SIGTERM à 20 m en plein batch 7/10, l'apply
    # terminal n'a jamais tourné → 24 proposals 'proposed' jamais appliquées).
    # Le budget nuit coupe AVANT le SIGTERM shell : fin propre, row
    # dream_runs toujours écrite, la rotation resservira les projets restants.
    # Log de progression flush=True (stdout block-bufferisé sous >>).
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
    # Dégradation silencieuse : un secours qui réussit rendait la panne du
    # primaire invisible (qwen 80B mort le 2026-07-27, dix nuits vertes).
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
            # Fenêtre LLM fair-share : un gros projet ne mange plus la part
            # des suivants (nuit 2026-07-10 : red 383s → 5 projets reportés).
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
            # Gate anti-dump two-tier (wet) : les merges recalés par le juge
            # sont persistés 'proposed' (review du matin) mais JAMAIS
            # auto-appliqués — fail-closed dans judge_merges.
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
            # Apply PAR BATCH : un SIGTERM ne perd que le batch en vol.
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
    # `degraded` voyage dans error_message SANS toucher au statut : le
    # briefing ne lit que les rows status != 'done' pour son bloc « Last
    # failure », donc la dégradation reste interrogeable sans déclencher une
    # alarme sur un comportement qui, lui, n'a rien cassé.
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
