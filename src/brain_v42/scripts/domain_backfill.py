#!/usr/bin/env python3
"""Proposer-only domain backfill via the NVIDIA API (OpenAI-compatible).

Classifies the graph's orphans (0 RELATED_TO + 0 BELONGS_TO_DOMAIN) against the
closed ALLOWED_DOMAINS set and emits PROPOSALS into
logs/domain_backfill/<date>.{jsonl,md}. NO write into the brain — the apply
(brain_assign_domain) is a separate future step.

Deliberate divergence from the CONNECT phase: CONNECT forces `backend` on
ambiguity (it writes directly); here the model must answer `unknown` (a human
reviews the report). No tool-calling by construction (red-shrik gotcha: deepseek
hangs with tools; pure JSON = OK).

Usage:
    python -m scripts.domain_backfill --limit 30
    python -m scripts.domain_backfill --model moonshotai/kimi-k2-instruct
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from brain_v42.db.tables import adrs, decisions, learnings, runbooks, snippets
from brain_v42.services.graph_service import ALLOWED_DOMAINS

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
#: Primary for extract AND backfill. `deepseek-ai/deepseek-v4-pro` held this
#: place until 2026-08-21 while having been dead since 08-12; its replacement
#: `meta/llama-3.3-70b-instruct` died in turn between the nights of 27 August
#: (extract done) and 28 August (extract fail 410, probe GONE on the 29th).
#: Replaced on 2026-08-29 by the fastest of the four live candidates canaried
#: WITHOUT persistence on the real extraction prompt, against three real pending
#: tickets (`fetch_pending_threads` → `extract_thread`, stopped before
#: `persist_proposals`): 3/3 valid, 13 drafts, 16.1 s/ticket — against 25.9 s
#: for mistral-nemotron (fallback), 19.7 s but 8 drafts for nano-30b, 57.5 s for
#: gpt-oss-20b. A 410 is not transient: no retry repairs it, only the constant
#: does.
DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"
DEFAULT_ENV_FILE = Path.home() / ".config" / "brain-v42" / "nvidia.env"
VALID_DOMAINS: frozenset[str] = ALLOWED_DOMAINS | {"unknown"}
VALID_CONFIDENCES: frozenset[str] = frozenset({"high", "medium", "low"})
SNIPPET_MAX_CHARS = 400

# Neo4j label -> (PG core table, title column, content column).
# Table objects (no SQL f-string): native .in_() bind, no asyncpg trap on uuid[]
# arrays.
_TYPE_SOURCES: dict[str, tuple[sa.Table, str, str]] = {
    "Decision": (decisions, "title", "description"),
    "Learning": (learnings, "topic", "insight"),
    "Snippet": (snippets, "title", "code"),
    "Runbook": (runbooks, "title", "description"),
    "ADR": (adrs, "title", "context"),
}
_LABEL_BY_TYPE: dict[str, str] = {label.lower(): label for label in _TYPE_SOURCES}


class GraphServiceLike(Protocol):
    async def find_orphans_for_classification(self, limit: int = 20) -> list[dict]: ...


@dataclass(frozen=True)
class EntityCard:
    entity_id: str
    entity_type: str
    title: str
    snippet: str
    project_key: str | None
    tags: list[str]


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a systemd-style env file (everything after the first '=' is literal).

    Keys already present in os.environ are NOT overwritten (precedence: environ >
    file). Missing file -> {}.
    """
    if not path.is_file():
        return {}
    parsed: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        parsed[key.strip()] = value
    for key, value in parsed.items():
        os.environ.setdefault(key, value)
    return parsed


def entity_type_from_labels(labels: list[str]) -> str | None:
    """Premier label classifiable, en minuscule. None = nœud non classifiable."""
    for label in labels:
        if label in _TYPE_SOURCES:
            return label.lower()
    return None


async def fetch_orphans(graph_service: GraphServiceLike, limit: int) -> list[dict]:
    """Orphans du graph normalisés en [{id, entity_type}] (labels inconnus exclus)."""
    rows = await graph_service.find_orphans_for_classification(limit=limit)
    out: list[dict] = []
    for row in rows:
        etype = entity_type_from_labels(list(row.get("labels", [])))
        if etype is not None:
            out.append({"id": str(row["id"]), "entity_type": etype})
    return out


async def fetch_entity_cards(
    session_factory: async_sessionmaker[AsyncSession], orphans: list[dict]
) -> list[EntityCard]:
    """Hydrate the orphans from PG (title, snippet, project_key, tags).

    Two forms of graph drift are ignored without crashing (visible through the
    report's orphans_seen vs cards_classified gap): an id present in the graph
    but absent from PG, and a non-UUID id (real pollution: a Decision node with
    id="None" — a leaked str(None) — found on the first --limit 50 run on
    2026-07-03).
    """
    by_type: dict[str, list[uuid.UUID]] = {}
    for o in orphans:
        try:
            entity_uuid = uuid.UUID(o["id"])
        except ValueError:
            continue
        by_type.setdefault(o["entity_type"], []).append(entity_uuid)

    cards: list[EntityCard] = []
    async with session_factory() as session:
        for etype, ids in by_type.items():
            table, title_col, content_col = _TYPE_SOURCES[_LABEL_BY_TYPE[etype]]
            stmt = sa.select(
                table.c.id,
                table.c[title_col].label("title"),
                table.c[content_col].label("content"),
                table.c.project_key,
                table.c.tags,
            ).where(table.c.id.in_(ids))
            result = await session.execute(stmt)
            for r in result.mappings():
                cards.append(
                    EntityCard(
                        entity_id=str(r["id"]),
                        entity_type=etype,
                        title=r["title"] or "",
                        snippet=(r["content"] or "")[:SNIPPET_MAX_CHARS],
                        project_key=r["project_key"],
                        tags=list(r["tags"] or []),
                    )
                )
    return cards


# ── Task 2 : prompt builder + parse/validate ─────────────────────────

_SYSTEM_PROMPT = (
    "Tu es un classificateur précis d'entités de connaissance technique. "
    "Tu réponds UNIQUEMENT avec un tableau JSON valide — pas de prose, pas de "
    "fences markdown, pas d'explication hors du JSON."
)

# Canonical definitions copied from scripts/dream/phase_connect.md (Step B).
# Only divergence: `unknown` replaces the `backend` fallback (proposer-only).
_DOMAIN_DEFINITIONS = """\
infra      — deployment, Docker, networking, VPS, CI/CD, systemd
ml         — training, inference, fine-tuning, LoRA, dataset, agent models
backend    — services, APIs, Python/Go services, DB, workers (generic default)
memory     — knowledge graph, brain-v42, embeddings, vector search, consolidation
tooling    — MCP servers, hooks, CLI, dev utilities, skills, prompts
data       — ETL, analytics, red-data, reporting, metrics pipelines
ops        — monitoring, alerting, red-monitor, observability, health
frontend   — SolidJS, UI components, dashboards, styling, WebSockets in the UI
security   — credentials, secrets, auth, red-backup, isolation
unknown    — utilise-le si tu n'es PAS raisonnablement sûr (ne force jamais)"""


@dataclass(frozen=True)
class Proposal:
    entity_id: str
    entity_type: str
    title: str
    project_key: str | None
    domain: str
    confidence: str
    reason: str


@dataclass(frozen=True)
class Rejection:
    entity_id: str | None
    reason_code: str
    detail: str


class ResponseParseError(Exception):
    """The model's content is not a usable JSON array."""


def build_messages(batch: list[EntityCard]) -> list[dict[str, str]]:
    """OpenAI-compatible messages to classify a batch (without tools)."""
    lines: list[str] = [
        "Classifie chaque entité dans EXACTEMENT UN domaine du set fermé :",
        "",
        _DOMAIN_DEFINITIONS,
        "",
        "Règles :",
        "- Utilise title, tags, project_key et snippet comme signal.",
        "- Si hésitation entre 2 domaines, prends le plus spécifique.",
        "- N'invente JAMAIS de domaine hors set. En cas de doute : unknown.",
        "- Réponds avec un tableau JSON, un objet par entité :",
        '  {"entity_id": "<uuid>", "domain": "<domaine>",'
        ' "confidence": "high|medium|low", "reason": "<=140 chars"}',
        "",
        f"Entités ({len(batch)}) :",
    ]
    for i, card in enumerate(batch, start=1):
        lines.append(
            f"{i}. entity_id={card.entity_id} | type={card.entity_type}"
            f" | project={card.project_key or '-'} | tags={','.join(card.tags) or '-'}"
        )
        lines.append(f"   title: {card.title}")
        lines.append(f"   snippet: {card.snippet}")
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _strip_fences(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def parse_and_validate(
    content: str, batch: list[EntityCard]
) -> tuple[list[Proposal], list[Rejection]]:
    """Validate the model's response against the batch that was sent.

    Raises ResponseParseError if the content is not a JSON array — the caller
    (classify_batch, Task 3) then does ONE corrective re-prompt.

    missing_in_response semantics: every card of the batch without an ACCEPTED
    proposal receives a missing_in_response rejection — including when it also
    has rejections of another kind (an invalid_domain card therefore produces 2
    rejections). Intended: the report shows at a glance which entities remain
    orphans after the run.
    """
    try:
        data = json.loads(_strip_fences(content))
    except json.JSONDecodeError as exc:
        raise ResponseParseError(str(exc)) from exc
    if not isinstance(data, list):
        raise ResponseParseError(f"expected JSON array, got {type(data).__name__}")

    cards_by_id = {c.entity_id: c for c in batch}
    proposals: list[Proposal] = []
    rejections: list[Rejection] = []
    seen: set[str] = set()

    for item in data:
        if not isinstance(item, dict) or "entity_id" not in item:
            rejections.append(Rejection(None, "invalid_item", repr(item)[:200]))
            continue
        entity_id = str(item["entity_id"])
        if entity_id not in cards_by_id:
            rejections.append(Rejection(entity_id, "unknown_entity_id", "id hors batch"))
            continue
        if entity_id in seen:
            rejections.append(Rejection(entity_id, "duplicate_entity_id", "déjà proposé"))
            continue
        domain = str(item.get("domain", "")).strip().lower()
        if domain not in VALID_DOMAINS:
            rejections.append(Rejection(entity_id, "invalid_domain", domain))
            continue
        confidence = str(item.get("confidence", "")).strip().lower()
        if confidence not in VALID_CONFIDENCES:
            rejections.append(Rejection(entity_id, "invalid_confidence", confidence))
            continue
        seen.add(entity_id)
        card = cards_by_id[entity_id]
        proposals.append(
            Proposal(
                entity_id=entity_id,
                entity_type=card.entity_type,
                title=card.title,
                project_key=card.project_key,
                domain=domain,
                confidence=confidence,
                reason=str(item.get("reason", ""))[:300],
            )
        )

    proposed_ids = {p.entity_id for p in proposals}
    for card in batch:
        if card.entity_id not in proposed_ids:
            rejections.append(
                Rejection(card.entity_id, "missing_in_response", "aucune proposition acceptée")
            )
    return proposals, rejections


# ── Task 3 : client NVIDIA ───────────────────────────────────────────

MAX_HTTP_ATTEMPTS = 3
# 529 added on 2026-08-05: provider overload, transient just like a 503.
# Without it, a single 529 on one ROADMAP batch opened the primary model's
# circuit and sent the WHOLE night to the fallback (canary: 2 batches out of 8).
# Not to be confused with 410, which is definitive and never retryable.
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504, 529})
_REPROMPT_INSTRUCTION = (
    "Ta réponse précédente n'était pas un tableau JSON valide. Réponds "
    "maintenant UNIQUEMENT avec le tableau JSON demandé — aucun autre texte."
)


#: 404/410: the provider says this model name no longer designates anything.
#: STRICTLY DISJOINT from RETRYABLE_STATUS, and that is the whole point: a 529
#: is a passing overload, a 410 is an end of life. Confusing them one way wastes
#: a night retrying a corpse, the other way replaces a live model over a
#: hiccup.
MODEL_GONE_STATUS: frozenset[int] = frozenset({404, 410})


class NvidiaAuthError(Exception):
    """401/403: invalid key — no point continuing the run."""


class ModelGoneError(RuntimeError):
    """404/410: the model has disappeared at the provider.

    Measured on 2026-08-12: `deepseek-ai/deepseek-v4-pro` returns 410, and the
    night's 20 tickets failed in 0.907 s out of a 540 s budget — twenty times
    the same definitive error, with nothing saying the cause was single and that
    a fallback existed.

    Inherits from RuntimeError so it stays caught by the callers' existing
    guards: a caller that does not yet know this class behaves exactly as
    before.
    """

    def __init__(self, model: str, status_code: int, detail: str = "") -> None:
        self.model = model
        self.status_code = status_code
        suffix = f" — {detail}" if detail else ""
        super().__init__(
            f"HTTP {status_code}: le modèle « {model} » n'existe plus chez le fournisseur{suffix}"
        )


@dataclass
class BatchOutcome:
    proposals: list[Proposal]
    rejections: list[Rejection]
    failed: bool = False
    error: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


def _exc_str(exc: BaseException) -> str:
    """Class name + message — str(exc) alone is often EMPTY for httpx transport
    errors (ReadTimeout, ConnectError…); the class name is the only information
    always present (extract incident 2026-07-04: an empty "failed:" plus
    dream_runs.error_message='')."""
    return f"{type(exc).__name__}: {exc}".rstrip(": ")


async def _post_chat(
    client: httpx.AsyncClient,
    model: str,
    messages: list[dict[str, str]],
    sleep: Callable[[float], Awaitable[Any]],
    max_tokens: int = 4096,
) -> tuple[str, dict[str, Any]]:
    """POST /chat/completions with backoff retry on transients.

    MAX_HTTP_ATTEMPTS = 3 TOTAL attempts (2 retries), backoff 2 s then 4 s.
    Retryable: RETRYABLE_STATUS statuses + httpx transport timeouts (NVIDIA
    queue latency — extract incident 2026-07-04: ~100 s for a trivial prompt,
    ReadTimeout at exactly 180 s on the real prompt). max_tokens is
    configurable (wet roadmap finding 2026-07-04: the consolidator prompt
    truncated the brain-v42 batch at 4096) — default unchanged.
    """
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    last_error = ""
    for attempt in range(1, MAX_HTTP_ATTEMPTS + 1):
        try:
            response = await client.post("/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            last_error = _exc_str(exc)
            if attempt < MAX_HTTP_ATTEMPTS:
                await sleep(float(2**attempt))
            continue
        if response.status_code in (401, 403):
            raise NvidiaAuthError(f"HTTP {response.status_code}: {response.text[:200]}")
        if response.status_code in MODEL_GONE_STATUS:
            # Before the retry, never after: waiting 2 s then 4 s to be told
            # "retired" again only adds 6 s to the certainty.
            raise ModelGoneError(model, response.status_code, response.text[:200])
        if response.status_code in RETRYABLE_STATUS:
            last_error = f"HTTP {response.status_code}"
            if attempt < MAX_HTTP_ATTEMPTS:
                await sleep(float(2**attempt))
            continue
        response.raise_for_status()
        data = response.json()
        content = str(data["choices"][0]["message"]["content"])
        usage = dict(data.get("usage") or {})
        return content, usage
    raise RuntimeError(f"exhausted {MAX_HTTP_ATTEMPTS} attempts ({last_error})")


async def classify_batch(
    client: httpx.AsyncClient,
    model: str,
    batch: list[EntityCard],
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> BatchOutcome:
    """Classifie un batch. Ne lève que NvidiaAuthError (abort run)."""
    messages = build_messages(batch)
    prompt_tokens = 0
    completion_tokens = 0
    try:
        content, usage = await _post_chat(client, model, messages, sleep)
        prompt_tokens += int(usage.get("prompt_tokens") or 0)
        completion_tokens += int(usage.get("completion_tokens") or 0)
        try:
            proposals, rejections = parse_and_validate(content, batch)
        except ResponseParseError:
            corrective = [
                *messages,
                {"role": "assistant", "content": content},
                {"role": "user", "content": _REPROMPT_INSTRUCTION},
            ]
            content, usage = await _post_chat(client, model, corrective, sleep)
            prompt_tokens += int(usage.get("prompt_tokens") or 0)
            completion_tokens += int(usage.get("completion_tokens") or 0)
            try:
                proposals, rejections = parse_and_validate(content, batch)
            except ResponseParseError as exc:
                return BatchOutcome(
                    proposals=[],
                    rejections=[],
                    failed=True,
                    error=f"unparseable after corrective re-prompt: {exc}",
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
    except NvidiaAuthError:
        raise
    except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as exc:
        return BatchOutcome(
            proposals=[],
            rejections=[],
            failed=True,
            error=_exc_str(exc),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
    return BatchOutcome(
        proposals=proposals,
        rejections=rejections,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


@dataclass
class BackfillResult:
    proposals: list[Proposal]
    rejections: list[Rejection]
    failed_batches: list[str]
    orphans_seen: int
    cards_classified: int
    prompt_tokens: int
    completion_tokens: int


ClassifyFn = Callable[[list[EntityCard]], Awaitable[BatchOutcome]]


def _chunks(items: list[EntityCard], size: int) -> Iterable[list[EntityCard]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def run_backfill(
    graph_service: GraphServiceLike,
    session_factory: async_sessionmaker[AsyncSession],
    classify_fn: ClassifyFn,
    *,
    limit: int,
    batch_size: int,
) -> BackfillResult:
    """Fetch → batch → classify → aggregate. A failed batch does not kill the run."""
    orphans = await fetch_orphans(graph_service, limit)
    cards = await fetch_entity_cards(session_factory, orphans)

    proposals: list[Proposal] = []
    rejections: list[Rejection] = []
    failed_batches: list[str] = []
    prompt_tokens = 0
    completion_tokens = 0

    for batch in _chunks(cards, batch_size):
        outcome = await classify_fn(batch)
        prompt_tokens += outcome.prompt_tokens
        completion_tokens += outcome.completion_tokens
        if outcome.failed:
            failed_batches.append(outcome.error or "unknown error")
            continue
        proposals.extend(outcome.proposals)
        rejections.extend(outcome.rejections)

    return BackfillResult(
        proposals=proposals,
        rejections=rejections,
        failed_batches=failed_batches,
        orphans_seen=len(orphans),
        cards_classified=len(cards),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def write_reports(
    out_dir: Path, run_date: str, model: str, result: BackfillResult
) -> tuple[Path, Path]:
    """Write <date>.jsonl (pure proposals) + <date>.md (human summary)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / f"{run_date}.jsonl"
    md_path = out_dir / f"{run_date}.md"

    with jsonl_path.open("w") as fh:
        for p in result.proposals:
            fh.write(
                json.dumps(
                    {"run_date": run_date, "model": model, **asdict(p)},
                    ensure_ascii=False,
                )
                + "\n"
            )

    by_domain: dict[str, list[Proposal]] = {}
    for p in result.proposals:
        by_domain.setdefault(p.domain, []).append(p)

    lines = [
        f"# Domain backfill — {run_date}",
        "",
        f"- Modèle : `{model}`",
        f"- Orphans vus : {result.orphans_seen} · cartes classifiées : {result.cards_classified}",
        f"- Propositions : {len(result.proposals)} · rejets : {len(result.rejections)}"
        f" · batches échoués : {len(result.failed_batches)}",
        f"- Tokens : {result.prompt_tokens} prompt / {result.completion_tokens} completion",
        "",
        "## Propositions par domaine",
        "",
    ]
    order = {"high": 0, "medium": 1, "low": 2}
    for domain in sorted(by_domain):
        lines.append(f"### {domain} ({len(by_domain[domain])})")
        lines.append("")
        lines.append("| confiance | type | titre | projet | raison |")
        lines.append("|---|---|---|---|---|")
        for p in sorted(by_domain[domain], key=lambda x: order[x.confidence]):
            lines.append(
                f"| {p.confidence} | {p.entity_type} | {p.title[:60]}"
                f" | {p.project_key or '-'} | {p.reason[:80]} |"
            )
        lines.append("")
    if result.rejections:
        lines += ["## Rejections", ""]
        for r in result.rejections:
            lines.append(f"- `{r.reason_code}` {r.entity_id or '?'} — {r.detail[:120]}")
        lines.append("")
    if result.failed_batches:
        lines += ["## Failed batches", ""]
        for err in result.failed_batches:
            lines.append(f"- {err[:200]}")
        lines.append("")
    md_path.write_text("\n".join(lines))
    return jsonl_path, md_path


def _positive_int(value: str) -> int:
    """argparse validator: integer >= 1.

    Without this guard, --batch-size 0 reached _chunks (range step 0 → a raw
    ValueError) and a negative --batch-size produced a silently empty run.
    argparse answers with usage + exit 2 (contract: 2 = configuration error).
    """
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"doit être >= 1 (reçu : {number})")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="domain_backfill",
        description="Proposer-only domain classification of graph orphans (NVIDIA API).",
    )
    parser.add_argument("--limit", type=_positive_int, default=30, help="max orphans à traiter")
    parser.add_argument(
        "--batch-size",
        type=_positive_int,
        default=15,
        help="entités par requête LLM",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"défaut: env BRAIN_NVIDIA_MODEL puis {DEFAULT_MODEL}",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"défaut: env BRAIN_NVIDIA_BASE_URL puis {DEFAULT_BASE_URL}",
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--out-dir", type=Path, default=Path("logs/domain_backfill"))
    return parser.parse_args(argv)


def resolve_option(cli_value: str | None, env_key: str, default: str) -> str:
    """Précédence : CLI > env > défaut codé."""
    if cli_value:
        return cli_value
    return os.environ.get(env_key) or default


async def _run(args: argparse.Namespace, api_key: str) -> int:
    from neo4j import AsyncGraphDatabase  # import local : dep runtime du serveur
    from pydantic import ValidationError

    from brain_v42.config import Settings
    from brain_v42.services.graph_service import GraphService

    try:
        settings = Settings()  # type: ignore[call-arg]  # pydantic-settings reads from env
    except ValidationError as exc:
        print(f"Config invalide (env/.env manquant ?) : {exc}", file=sys.stderr)
        return 2
    if not settings.neo4j_url:
        print(
            "NEO4J_URL absent de la config — requis pour lister les orphans.",
            file=sys.stderr,
        )
        return 1

    model = resolve_option(args.model, "BRAIN_NVIDIA_MODEL", DEFAULT_MODEL)
    base_url = resolve_option(args.base_url, "BRAIN_NVIDIA_BASE_URL", DEFAULT_BASE_URL)

    engine = create_async_engine(settings.postgres_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_url, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    graph_service = GraphService(driver, timeout=settings.neo4j_timeout)

    http_client = httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0),
    )

    async def classify_fn(batch: list[EntityCard]) -> BatchOutcome:
        return await classify_batch(http_client, model, batch)

    try:
        result = await run_backfill(
            graph_service,
            session_factory,
            classify_fn,
            limit=args.limit,
            batch_size=args.batch_size,
        )
    except NvidiaAuthError as exc:
        print(f"Clé NVIDIA refusée : {exc}", file=sys.stderr)
        return 2
    finally:
        await http_client.aclose()
        await driver.close()
        await engine.dispose()

    run_date = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
    jsonl_path, md_path = write_reports(args.out_dir, run_date, model, result)
    unknown = sum(1 for p in result.proposals if p.domain == "unknown")
    print(
        f"orphans={result.orphans_seen} classified={result.cards_classified}"
        f" proposals={len(result.proposals)} (unknown={unknown})"
        f" rejections={len(result.rejections)}"
        f" failed_batches={len(result.failed_batches)}"
        f" tokens={result.prompt_tokens}+{result.completion_tokens}"
    )
    print(f"jsonl: {jsonl_path}")
    print(f"md:    {md_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_env_file(args.env_file)
    if args.env_file.is_file():
        mode = args.env_file.stat().st_mode & 0o777
        if mode & 0o077:
            print(
                f"warning: {args.env_file} lisible par groupe/autres"
                f" (mode {oct(mode)}) — chmod 600 recommandé.",
                file=sys.stderr,
            )
    api_key = os.environ.get("BRAIN_NVIDIA_API_KEY", "")
    if not api_key:
        print(
            "BRAIN_NVIDIA_API_KEY manquant — renseigne-le dans"
            f" {args.env_file} (voir deploy/nvidia.env.example).",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(_run(args, api_key))


if __name__ == "__main__":
    sys.exit(main())
