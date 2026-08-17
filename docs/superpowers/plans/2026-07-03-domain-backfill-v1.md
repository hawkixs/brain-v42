# Domain Backfill v1 (proposer-only) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Un CLI manuel `python -m scripts.domain_backfill` qui classifie les orphans du graph (0 `RELATED_TO` + 0 `BELONGS_TO_DOMAIN`) contre le set fermé de 9 domaines via l'API NVIDIA (OpenAI-compat), et émet un rapport de **propositions** (`logs/domain_backfill/<date>.jsonl` + `.md`) — **zéro écriture dans le brain**.

**Architecture:** Script standalone asyncio dans `scripts/` (même moule que `scripts/dream/promote_prepare.py`) : fetch déterministe (Neo4j via `GraphService.find_orphans_for_classification` + PG pour les métadonnées), batches de 15 → 1 requête `chat/completions` NVIDIA par batch (httpx, JSON strict, **aucun tool-calling** — gotcha « deepseek hang avec tools » évité par construction), validation déterministe des réponses, rapport review-able conçu pour un futur `apply --min-confidence high` (étape C, hors scope).

**Tech Stack:** Python 3.12, httpx (>=0.27, déjà dep), SQLAlchemy 2 async + asyncpg, neo4j AsyncGraphDatabase, pytest + pytest-asyncio + httpx.MockTransport. Modèle défaut `deepseek-ai/deepseek-v4-pro` sur `https://integrate.api.nvidia.com/v1`.

## Global Constraints

- **Zéro écriture brain** : jamais d'appel à `assign_domain`/écriture Neo4j/PG. Le script est read-only sur les données.
- **Zéro réseau dans les tests** : client NVIDIA via `httpx.MockTransport` ; Neo4j stubbé ; PG = base de test existante (`require_test_db_url`, skip si absente — convention `test_promote_prepare.py`).
- **TDD strict** : chaque task = test RED d'abord, GREEN minimal, commit. JAMAIS modifier un test pour faire passer le code.
- **Gate avant chaque commit** : `pytest tests/unit -q` + `ruff check src/ tests/ scripts/` + `ruff format --check src/ tests/ scripts/` + `mypy src/ scripts/domain_backfill.py` verts (mypy ciblé : le reste de `scripts/` n'est historiquement pas couvert — parité CI conservée sur `src/`).
- **Retry HTTP** : `MAX_HTTP_ATTEMPTS = 3` = 3 tentatives TOTALES (donc 2 retries max), backoff 2 s puis 4 s. Sémantique confirmée — ne pas « corriger » vers 4 tentatives.
- **Set de domaines** : importer `ALLOWED_DOMAINS` depuis `brain_v42.services.graph_service` (source unique). `unknown` est ajouté côté script (`VALID_DOMAINS = ALLOWED_DOMAINS | {"unknown"}`).
- **Divergence délibérée vs phase CONNECT** : CONNECT force `backend` en cas d'ambiguïté (il écrit) ; ici on demande `unknown` (un humain review). Documentée dans le docstring du module.
- **Clé API** : env `BRAIN_NVIDIA_API_KEY`, chargée depuis `~/.config/brain-v42/nvidia.env` (0600) — jamais en clair dans le repo, jamais loggée.
- **Commits** : Conventional Commits, footer `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Type hints partout ; pas de `print` hors CLI main (le résumé stdout du CLI est légitime, comme `promote_prepare`).

## File Structure

| Fichier | Rôle |
|---|---|
| `scripts/domain_backfill.py` | Module unique (dataclasses, env-file loader, fetch, prompt, parse/validate, client NVIDIA retry, rapports, CLI) — convention single-module des helpers dream |
| `tests/unit/test_domain_backfill.py` | Tous les tests unit (pur + PG-backed skippable) |
| `deploy/nvidia.env.example` | Template documenté de `~/.config/brain-v42/nvidia.env` |
| `logs/domain_backfill/` | Sorties runtime (gitignoré — vérifier que `logs/` l'est déjà, sinon ajouter) |

## Non-goals (v1)

- Pas d'apply (`brain_assign_domain`) — étape C future.
- Pas de scheduling nightly / intégration dream.sh.
- Pas d'autres chantiers (dedup, hygiène learnings, résonance).
- Pas de gestion multi-clés / rotation / secrets manager.

---

### Task 1: Scaffolding — dataclasses, env-file loader, fetch orphans + cartes PG

**Files:**
- Create: `scripts/domain_backfill.py`
- Test: `tests/unit/test_domain_backfill.py`

**Interfaces (produit pour les tasks suivantes):**
```python
DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "deepseek-ai/deepseek-v4-pro"
DEFAULT_ENV_FILE: Path  # ~/.config/brain-v42/nvidia.env
VALID_DOMAINS: frozenset[str]      # ALLOWED_DOMAINS | {"unknown"}
VALID_CONFIDENCES: frozenset[str]  # {"high","medium","low"}
SNIPPET_MAX_CHARS = 400

@dataclass(frozen=True)
class EntityCard:
    entity_id: str
    entity_type: str   # decision|learning|snippet|runbook|adr (minuscule)
    title: str
    snippet: str       # contenu tronqué à SNIPPET_MAX_CHARS
    project_key: str | None
    tags: list[str]

def load_env_file(path: Path) -> dict[str, str]           # parse systemd-style, setdefault os.environ
def entity_type_from_labels(labels: list[str]) -> str | None
async def fetch_orphans(graph_service: GraphServiceLike, limit: int) -> list[dict]
async def fetch_entity_cards(
    session_factory: async_sessionmaker[AsyncSession], orphans: list[dict]
) -> list[EntityCard]

class GraphServiceLike(Protocol):
    async def find_orphans_for_classification(self, limit: int = 20) -> list[dict]: ...
```

- [ ] **Step 1: Écrire les tests RED**

```python
"""Unit tests for scripts.domain_backfill (proposer-only NVIDIA classifier).

Réseau interdit : client NVIDIA mocké (httpx.MockTransport), GraphService
stubbé. Les tests PG suivent la convention test_promote_prepare.py
(require_test_db_url + skip si base absente).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from scripts import domain_backfill as db
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from tests.conftest import require_test_db_url

# ── Task 1 : env file / labels / cartes ──────────────────────────────


def test_load_env_file_parses_systemd_style(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KEY=VALUE littéral après le premier '=', commentaires ignorés.

    Clés NEUTRES (X_BACKFILL_TEST_*) : load_env_file fait un setdefault dans
    os.environ — utiliser les vraies clés BRAIN_NVIDIA_* ici polluerait la
    session pytest entière et fausserait test_main_missing_api_key_exits_2.
    """
    monkeypatch.delenv("X_BACKFILL_TEST_KEY", raising=False)
    monkeypatch.delenv("X_BACKFILL_TEST_MODEL", raising=False)
    f = tmp_path / "nvidia.env"
    f.write_text(
        "# comment\n"
        "X_BACKFILL_TEST_KEY=nvapi-abc=def\n"
        "\n"
        "X_BACKFILL_TEST_MODEL=moonshotai/kimi-k2-instruct\n"
    )
    got = db.load_env_file(f)
    assert got["X_BACKFILL_TEST_KEY"] == "nvapi-abc=def"
    assert got["X_BACKFILL_TEST_MODEL"] == "moonshotai/kimi-k2-instruct"
    assert os.environ["X_BACKFILL_TEST_KEY"] == "nvapi-abc=def"
    monkeypatch.delenv("X_BACKFILL_TEST_KEY", raising=False)
    monkeypatch.delenv("X_BACKFILL_TEST_MODEL", raising=False)


def test_load_env_file_missing_returns_empty(tmp_path: Path) -> None:
    assert db.load_env_file(tmp_path / "absent.env") == {}


def test_load_env_file_does_not_override_existing_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """os.environ gagne sur le fichier (injection CI/tests)."""
    monkeypatch.setenv("X_BACKFILL_TEST_KEY", "from-environ")
    f = tmp_path / "nvidia.env"
    f.write_text("X_BACKFILL_TEST_KEY=from-file\n")
    db.load_env_file(f)
    assert os.environ["X_BACKFILL_TEST_KEY"] == "from-environ"


def test_entity_type_from_labels() -> None:
    assert db.entity_type_from_labels(["Learning"]) == "learning"
    assert db.entity_type_from_labels(["Entity", "ADR"]) == "adr"
    assert db.entity_type_from_labels(["Domain"]) is None
    assert db.entity_type_from_labels([]) is None


class _StubGraph:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.seen_limit: int | None = None

    async def find_orphans_for_classification(self, limit: int = 20) -> list[dict]:
        self.seen_limit = limit
        return self._rows[:limit]


@pytest.mark.asyncio
async def test_fetch_orphans_passes_limit_and_normalizes() -> None:
    rows = [
        {"id": "11111111-1111-1111-1111-111111111111", "labels": ["Learning"]},
        {"id": "22222222-2222-2222-2222-222222222222", "labels": ["Domain"]},  # dropped
    ]
    stub = _StubGraph(rows)
    got = await db.fetch_orphans(stub, limit=10)
    assert stub.seen_limit == 10
    assert got == [
        {"id": "11111111-1111-1111-1111-111111111111", "entity_type": "learning"}
    ]


# ── PG-backed : cartes ───────────────────────────────────────────────


@pytest_asyncio.fixture(scope="module")
async def _engine() -> AsyncEngine:  # type: ignore[misc]
    eng = create_async_engine(require_test_db_url(), poolclass=NullPool, echo=False)
    try:
        async with eng.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL not reachable: {exc}")
    yield eng  # type: ignore[misc]
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.mark.asyncio
async def test_fetch_entity_cards_learning_and_truncation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    lid = uuid.uuid4()
    long_insight = "x" * 1000
    async with session_factory() as s:
        await s.execute(
            sa.text(
                "INSERT INTO learnings (id, topic, insight, project_key, tags)"
                " VALUES (:id, :topic, :insight, :pk, :tags)"
            ),
            {
                "id": str(lid),
                "topic": "T backfill",
                "insight": long_insight,
                "pk": "test-backfill",
                "tags": ["a", "b"],
            },
        )
        await s.commit()
    try:
        cards = await db.fetch_entity_cards(
            session_factory, [{"id": str(lid), "entity_type": "learning"}]
        )
        assert len(cards) == 1
        c = cards[0]
        assert c.entity_id == str(lid)
        assert c.entity_type == "learning"
        assert c.title == "T backfill"
        assert len(c.snippet) == db.SNIPPET_MAX_CHARS
        assert c.project_key == "test-backfill"
        assert c.tags == ["a", "b"]
    finally:
        async with session_factory() as s:
            await s.execute(sa.text("DELETE FROM learnings WHERE id = :id"), {"id": str(lid)})
            await s.commit()


@pytest.mark.asyncio
async def test_fetch_entity_cards_skips_ids_absent_from_pg(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Un id graph sans ligne PG (drift) est ignoré sans crash."""
    cards = await db.fetch_entity_cards(
        session_factory, [{"id": str(uuid.uuid4()), "entity_type": "decision"}]
    )
    assert cards == []
```

- [ ] **Step 2: Vérifier l'échec**

Run: `.venv/bin/python -m pytest tests/unit/test_domain_backfill.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.domain_backfill'` (ou ImportError équivalent).

- [ ] **Step 3: Implémentation minimale**

```python
#!/usr/bin/env python3
"""Proposer-only domain backfill via the NVIDIA API (OpenAI-compatible).

Classifie les orphans du graph (0 RELATED_TO + 0 BELONGS_TO_DOMAIN) contre
le set fermé ALLOWED_DOMAINS et émet des PROPOSITIONS dans
logs/domain_backfill/<date>.{jsonl,md}. AUCUNE écriture dans le brain —
l'apply (brain_assign_domain) est une étape future distincte.

Divergence délibérée vs phase CONNECT : CONNECT force `backend` en cas
d'ambiguïté (il écrit directement) ; ici le modèle doit répondre `unknown`
(un humain review le rapport). Pas de tool-calling par construction
(gotcha red-shrik : deepseek hang avec tools ; JSON pur = OK).

Usage:
    python -m scripts.domain_backfill --limit 30
    python -m scripts.domain_backfill --model moonshotai/kimi-k2-instruct
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import adrs, decisions, learnings, runbooks, snippets
from brain_v42.services.graph_service import ALLOWED_DOMAINS

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "deepseek-ai/deepseek-v4-pro"
DEFAULT_ENV_FILE = Path.home() / ".config" / "brain-v42" / "nvidia.env"
VALID_DOMAINS: frozenset[str] = ALLOWED_DOMAINS | {"unknown"}
VALID_CONFIDENCES: frozenset[str] = frozenset({"high", "medium", "low"})
SNIPPET_MAX_CHARS = 400

# label Neo4j -> (Table core PG, colonne titre, colonne contenu).
# Table objects (pas de SQL f-string) : bind .in_() natif, pas de piège
# asyncpg sur les arrays uuid[].
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
    """Parse un env-file style systemd (tout après le premier '=' est littéral).

    Les clés déjà présentes dans os.environ ne sont PAS écrasées
    (précédence : environ > fichier). Fichier absent -> {}.
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
    """Hydrate les orphans depuis PG (titre, snippet, project_key, tags).

    Les ids présents dans le graph mais absents de PG (drift) sont ignorés.
    """
    by_type: dict[str, list[uuid.UUID]] = {}
    for o in orphans:
        by_type.setdefault(o["entity_type"], []).append(uuid.UUID(o["id"]))

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
```

- [ ] **Step 4: Vérifier le vert**

Run: `.venv/bin/python -m pytest tests/unit/test_domain_backfill.py -q`
Expected: PASS (les 2 tests PG skippent proprement si la base de test est absente).

- [ ] **Step 5: Gate + commit**

```bash
.venv/bin/ruff check scripts/ tests/unit/test_domain_backfill.py && \
.venv/bin/ruff format --check scripts/ tests/unit/test_domain_backfill.py && \
.venv/bin/mypy src/ scripts/domain_backfill.py && .venv/bin/python -m pytest tests/unit -q
git add scripts/domain_backfill.py tests/unit/test_domain_backfill.py
git commit -m "feat(backfill): scaffolding domain_backfill — env loader, orphans fetch, PG cards

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Prompt builder + parse/validate déterministe (pur)

**Files:**
- Modify: `scripts/domain_backfill.py` (ajouter après `fetch_entity_cards`)
- Test: `tests/unit/test_domain_backfill.py` (ajouter)

**Interfaces:**
- Consomme : `EntityCard`, `VALID_DOMAINS`, `VALID_CONFIDENCES` (Task 1)
- Produit :
```python
@dataclass(frozen=True)
class Proposal:
    entity_id: str
    entity_type: str
    title: str
    project_key: str | None
    domain: str        # ∈ VALID_DOMAINS (unknown inclus)
    confidence: str    # ∈ VALID_CONFIDENCES
    reason: str

@dataclass(frozen=True)
class Rejection:
    entity_id: str | None
    reason_code: str   # invalid_domain|invalid_confidence|unknown_entity_id|duplicate_entity_id|invalid_item|missing_in_response
    detail: str

class ResponseParseError(Exception): ...

def build_messages(batch: list[EntityCard]) -> list[dict[str, str]]
def parse_and_validate(content: str, batch: list[EntityCard]) -> tuple[list[Proposal], list[Rejection]]
```

- [ ] **Step 1: Tests RED**

```python
# ── Task 2 : prompt + parse/validate ─────────────────────────────────


def _card(i: int, etype: str = "learning") -> db.EntityCard:
    return db.EntityCard(
        entity_id=f"00000000-0000-0000-0000-{i:012d}",
        entity_type=etype,
        title=f"Titre {i}",
        snippet=f"contenu {i}",
        project_key="brain-v42",
        tags=["t1"],
    )


def test_build_messages_contains_domains_rules_and_entities() -> None:
    batch = [_card(1), _card(2)]
    messages = db.build_messages(batch)
    assert messages[0]["role"] == "system"
    user = messages[1]["content"]
    for domain in sorted(db.VALID_DOMAINS):
        assert domain in user
    assert "unknown" in user
    assert "00000000-0000-0000-0000-000000000001" in user
    assert "Titre 2" in user
    assert "JSON" in messages[0]["content"]


def test_parse_and_validate_happy_path_with_fences() -> None:
    batch = [_card(1)]
    content = (
        "```json\n"
        '[{"entity_id": "00000000-0000-0000-0000-000000000001",'
        ' "domain": "Memory", "confidence": "HIGH", "reason": "brain graph"}]\n'
        "```"
    )
    proposals, rejections = db.parse_and_validate(content, batch)
    assert rejections == []
    assert len(proposals) == 1
    p = proposals[0]
    assert p.domain == "memory"          # normalisé lowercase
    assert p.confidence == "high"
    assert p.title == "Titre 1"          # enrichi depuis la carte


def _item(entity_id: str, domain: str, confidence: str, reason: str = "r") -> dict:
    return {
        "entity_id": entity_id,
        "domain": domain,
        "confidence": confidence,
        "reason": reason,
    }


def test_parse_and_validate_rejects_bad_domain_id_confidence_and_dups() -> None:
    batch = [_card(1), _card(2)]
    content = json.dumps(
        [
            _item(batch[0].entity_id, "blockchain", "high"),
            _item("99999999-9999-9999-9999-999999999999", "infra", "high"),
            _item(batch[1].entity_id, "infra", "certain"),
            _item(batch[1].entity_id, "infra", "high"),
            _item(batch[1].entity_id, "ops", "low", reason="dup"),
            "not-a-dict",
        ]
    )
    proposals, rejections = db.parse_and_validate(content, batch)
    assert [p.entity_id for p in proposals] == [batch[1].entity_id]
    codes = sorted(r.reason_code for r in rejections)
    assert codes == [
        "duplicate_entity_id",
        "invalid_confidence",
        "invalid_domain",
        "invalid_item",
        "missing_in_response",  # batch[0] n'a AUCUNE proposition acceptée
        "unknown_entity_id",
    ]


def test_parse_and_validate_unknown_domain_is_accepted() -> None:
    batch = [_card(1)]
    content = json.dumps(
        [{"entity_id": batch[0].entity_id, "domain": "unknown", "confidence": "low", "reason": "ambigu"}]
    )
    proposals, rejections = db.parse_and_validate(content, batch)
    assert proposals[0].domain == "unknown"
    assert rejections == []


def test_parse_and_validate_raises_on_non_json() -> None:
    with pytest.raises(db.ResponseParseError):
        db.parse_and_validate("désolé, voici la classification :", [_card(1)])


def test_parse_and_validate_raises_on_non_array() -> None:
    with pytest.raises(db.ResponseParseError):
        db.parse_and_validate('{"entity_id": "x"}', [_card(1)])
```

(ajouter `import json` en tête de fichier de test si absent)

- [ ] **Step 2: Vérifier l'échec**

Run: `.venv/bin/python -m pytest tests/unit/test_domain_backfill.py -q -k "messages or parse"`
Expected: FAIL — `AttributeError: module 'scripts.domain_backfill' has no attribute 'build_messages'`.

- [ ] **Step 3: Implémentation**

**Imports à AJOUTER en tête du module** (les imports sont distribués par task — en importer plus tôt = F401 au gate ruff) : `import json`.

```python
_SYSTEM_PROMPT = (
    "Tu es un classificateur précis d'entités de connaissance technique. "
    "Tu réponds UNIQUEMENT avec un tableau JSON valide — pas de prose, pas de "
    "fences markdown, pas d'explication hors du JSON."
)

# Définitions canoniques copiées de scripts/dream/phase_connect.md (Step B).
# Seule divergence : `unknown` remplace le fallback `backend` (proposer-only).
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
    """Le contenu du modèle n'est pas un tableau JSON exploitable."""


def build_messages(batch: list[EntityCard]) -> list[dict[str, str]]:
    """Messages OpenAI-compat pour classifier un batch (sans tools)."""
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
    """Valide la réponse du modèle contre le batch envoyé.

    Raises ResponseParseError si le contenu n'est pas un tableau JSON —
    l'appelant (Task 3) fait alors UN re-prompt correctif.
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
```

**Sémantique `missing_in_response`** : toute carte du batch sans proposition ACCEPTÉE reçoit un rejet `missing_in_response` — y compris si elle a par ailleurs des rejets d'une autre nature (une carte `invalid_domain` produit donc 2 rejets : `invalid_domain` + `missing_in_response`). C'est voulu : le rapport montre d'un coup d'œil quelles entités restent orphelines après le run. Le test `test_parse_and_validate_rejects_bad_domain_id_confidence_and_dups` encode exactement cette sémantique (6 codes).

- [ ] **Step 4: Vérifier le vert**

Run: `.venv/bin/python -m pytest tests/unit/test_domain_backfill.py -q`
Expected: PASS.

- [ ] **Step 5: Gate + commit**

```bash
.venv/bin/ruff check scripts/ tests/unit/test_domain_backfill.py && \
.venv/bin/ruff format --check scripts/ tests/unit/test_domain_backfill.py && \
.venv/bin/mypy src/ scripts/domain_backfill.py && .venv/bin/python -m pytest tests/unit -q
git add scripts/domain_backfill.py tests/unit/test_domain_backfill.py
git commit -m "feat(backfill): prompt builder + validation déterministe des réponses

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Client NVIDIA — retry 429/5xx + re-prompt correctif JSON

**Files:**
- Modify: `scripts/domain_backfill.py`
- Test: `tests/unit/test_domain_backfill.py`

**Interfaces:**
- Consomme : `build_messages`, `parse_and_validate`, `ResponseParseError`, `Proposal`, `Rejection`, `EntityCard`
- Produit :
```python
class NvidiaAuthError(Exception): ...   # 401/403 → abort du run entier

@dataclass
class BatchOutcome:
    proposals: list[Proposal]
    rejections: list[Rejection]
    failed: bool = False
    error: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0

async def classify_batch(
    client: httpx.AsyncClient,
    model: str,
    batch: list[EntityCard],
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> BatchOutcome
```
- Constantes : `MAX_HTTP_ATTEMPTS = 3`, `RETRYABLE_STATUS = {429, 500, 502, 503, 504}`, backoff `2**attempt` secondes.

- [ ] **Step 1: Tests RED**

```python
# ── Task 3 : client NVIDIA ───────────────────────────────────────────


def _ok_payload(batch: list[db.EntityCard]) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        [
                            {
                                "entity_id": c.entity_id,
                                "domain": "memory",
                                "confidence": "high",
                                "reason": "r",
                            }
                            for c in batch
                        ]
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://mock.nvidia.local/v1",
        headers={"Authorization": "Bearer nvapi-test"},
    )
```

(ajouter `from collections.abc import Callable` et `import httpx` en tête du fichier de test à cette task)

```python


async def _no_sleep(_: float) -> None:
    return None


@pytest.mark.asyncio
async def test_classify_batch_happy_path() -> None:
    batch = [_card(1), _card(2)]
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=_ok_payload(batch))

    async with _client(handler) as client:
        outcome = await db.classify_batch(client, "test-model", batch, sleep=_no_sleep)
    assert not outcome.failed
    assert len(outcome.proposals) == 2
    assert outcome.prompt_tokens == 100
    assert calls[0]["model"] == "test-model"
    assert calls[0]["temperature"] == pytest.approx(0.2)
    assert "tools" not in calls[0]  # jamais de tool-calling


@pytest.mark.asyncio
async def test_classify_batch_retries_on_429_then_succeeds() -> None:
    batch = [_card(1)]
    statuses = iter([429, 200])
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        if status == 429:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json=_ok_payload(batch))

    async def spy_sleep(seconds: float) -> None:
        slept.append(seconds)

    async with _client(handler) as client:
        outcome = await db.classify_batch(client, "m", batch, sleep=spy_sleep)
    assert not outcome.failed
    assert slept == [2.0]


@pytest.mark.asyncio
async def test_classify_batch_fails_after_exhausted_5xx() -> None:
    batch = [_card(1)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    async with _client(handler) as client:
        outcome = await db.classify_batch(client, "m", batch, sleep=_no_sleep)
    assert outcome.failed
    assert outcome.error is not None and "503" in outcome.error
    assert outcome.proposals == []


@pytest.mark.asyncio
async def test_classify_batch_reprompts_once_on_bad_json_then_succeeds() -> None:
    batch = [_card(1)]
    responses = iter(
        [
            {"choices": [{"message": {"content": "je pense que c'est memory"}}], "usage": {}},
            _ok_payload(batch),
        ]
    )
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=next(responses))

    async with _client(handler) as client:
        outcome = await db.classify_batch(client, "m", batch, sleep=_no_sleep)
    assert not outcome.failed
    assert len(bodies) == 2
    # le re-prompt embarque la réponse fautive + une consigne corrective
    assert bodies[1]["messages"][-2]["role"] == "assistant"
    assert "JSON" in bodies[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_classify_batch_fails_after_two_bad_json() -> None:
    batch = [_card(1)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "toujours pas du JSON"}}], "usage": {}}
        )

    async with _client(handler) as client:
        outcome = await db.classify_batch(client, "m", batch, sleep=_no_sleep)
    assert outcome.failed
    assert outcome.error is not None and "parse" in outcome.error.lower()


@pytest.mark.asyncio
async def test_classify_batch_401_raises_auth_error() -> None:
    batch = [_card(1)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    async with _client(handler) as client:
        with pytest.raises(db.NvidiaAuthError):
            await db.classify_batch(client, "m", batch, sleep=_no_sleep)
```

- [ ] **Step 2: Vérifier l'échec**

Run: `.venv/bin/python -m pytest tests/unit/test_domain_backfill.py -q -k classify`
Expected: FAIL — `AttributeError: ... no attribute 'classify_batch'`.

- [ ] **Step 3: Implémentation**

**Imports à AJOUTER en tête du module** : `import asyncio` · `import httpx` · `from collections.abc import Awaitable, Callable` · remplacer `from typing import Protocol` par `from typing import Any, Protocol`.

```python
MAX_HTTP_ATTEMPTS = 3
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})
_REPROMPT_INSTRUCTION = (
    "Ta réponse précédente n'était pas un tableau JSON valide. Réponds "
    "maintenant UNIQUEMENT avec le tableau JSON demandé — aucun autre texte."
)


class NvidiaAuthError(Exception):
    """401/403 : clé invalide — inutile de continuer le run."""


@dataclass
class BatchOutcome:
    proposals: list[Proposal]
    rejections: list[Rejection]
    failed: bool = False
    error: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


async def _post_chat(
    client: httpx.AsyncClient,
    model: str,
    messages: list[dict[str, str]],
    sleep: Callable[[float], Awaitable[Any]],
) -> tuple[str, dict[str, Any]]:
    """POST /chat/completions avec retry backoff sur les statuts transitoires."""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 4096,
    }
    last_error = ""
    for attempt in range(1, MAX_HTTP_ATTEMPTS + 1):
        response = await client.post("/chat/completions", json=payload)
        if response.status_code in (401, 403):
            raise NvidiaAuthError(f"HTTP {response.status_code}: {response.text[:200]}")
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
            proposals=[], rejections=[], failed=True, error=str(exc),
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        )
    return BatchOutcome(
        proposals=proposals,
        rejections=rejections,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
```

- [ ] **Step 4: Vérifier le vert**

Run: `.venv/bin/python -m pytest tests/unit/test_domain_backfill.py -q`
Expected: PASS.

- [ ] **Step 5: Gate + commit**

```bash
.venv/bin/ruff check scripts/ tests/unit/test_domain_backfill.py && \
.venv/bin/ruff format --check scripts/ tests/unit/test_domain_backfill.py && \
.venv/bin/mypy src/ scripts/domain_backfill.py && .venv/bin/python -m pytest tests/unit -q
git add scripts/domain_backfill.py tests/unit/test_domain_backfill.py
git commit -m "feat(backfill): client NVIDIA — retry backoff 429/5xx + re-prompt correctif

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Orchestrateur run_backfill + rapports jsonl/md

**Files:**
- Modify: `scripts/domain_backfill.py`
- Test: `tests/unit/test_domain_backfill.py`

**Interfaces:**
- Consomme : `fetch_orphans`, `fetch_entity_cards`, `classify_batch` (signature exacte Task 3), `BatchOutcome`, `Proposal`, `Rejection`
- Produit :
```python
@dataclass
class BackfillResult:
    proposals: list[Proposal]
    rejections: list[Rejection]
    failed_batches: list[str]          # messages d'erreur
    orphans_seen: int
    cards_classified: int
    prompt_tokens: int
    completion_tokens: int

ClassifyFn = Callable[[list[EntityCard]], Awaitable[BatchOutcome]]

async def run_backfill(
    graph_service: GraphServiceLike,
    session_factory: async_sessionmaker[AsyncSession],
    classify_fn: ClassifyFn,
    *,
    limit: int,
    batch_size: int,
) -> BackfillResult

def write_reports(
    out_dir: Path, run_date: str, model: str, result: BackfillResult
) -> tuple[Path, Path]   # (jsonl_path, md_path)
```
- jsonl : une ligne JSON par proposition — champs `run_date, model, entity_id, entity_type, title, project_key, domain, confidence, reason`. Pas de ligne méta (un futur `apply` consomme le fichier tel quel).
- md : en-tête (date, modèle, stats), tableau par domaine, sections `unknown`, `Rejections`, `Failed batches`.

- [ ] **Step 1: Tests RED**

```python
# ── Task 4 : orchestrateur + rapports ────────────────────────────────


class _StubSessionFactoryCards:
    """fetch_entity_cards est testé en PG ailleurs ; ici on stubbe au niveau
    run_backfill via monkeypatch de db.fetch_entity_cards."""


@pytest.mark.asyncio
async def test_run_backfill_batches_and_aggregates(monkeypatch: pytest.MonkeyPatch) -> None:
    cards = [_card(i) for i in range(1, 6)]  # 5 cartes
    orphan_rows = [
        {"id": c.entity_id, "labels": ["Learning"]} for c in cards
    ]
    stub_graph = _StubGraph(orphan_rows)

    async def fake_fetch_cards(
        _session_factory: object, orphans: list[dict]
    ) -> list[db.EntityCard]:
        wanted = {o["id"] for o in orphans}
        return [c for c in cards if c.entity_id in wanted]

    monkeypatch.setattr(db, "fetch_entity_cards", fake_fetch_cards)

    seen_batches: list[int] = []

    async def fake_classify(batch: list[db.EntityCard]) -> db.BatchOutcome:
        seen_batches.append(len(batch))
        if len(seen_batches) == 2:
            return db.BatchOutcome(proposals=[], rejections=[], failed=True, error="boom")
        return db.BatchOutcome(
            proposals=[
                db.Proposal(
                    entity_id=c.entity_id, entity_type=c.entity_type, title=c.title,
                    project_key=c.project_key, domain="memory", confidence="high", reason="r",
                )
                for c in batch
            ],
            rejections=[],
            prompt_tokens=10,
            completion_tokens=5,
        )

    result = await db.run_backfill(
        stub_graph, None, fake_classify, limit=5, batch_size=2  # type: ignore[arg-type]
    )
    assert seen_batches == [2, 2, 1]
    assert result.orphans_seen == 5
    assert result.cards_classified == 5
    assert len(result.proposals) == 3           # batch 2 a échoué
    assert result.failed_batches == ["boom"]
    assert result.prompt_tokens == 20           # 2 batches ok × 10


def test_write_reports_jsonl_roundtrip_and_md_sections(tmp_path: Path) -> None:
    result = db.BackfillResult(
        proposals=[
            db.Proposal("id-1", "learning", "T1", "brain-v42", "memory", "high", "r1"),
            db.Proposal("id-2", "decision", "T2", None, "unknown", "low", "r2"),
        ],
        rejections=[db.Rejection("id-3", "invalid_domain", "blockchain")],
        failed_batches=["HTTP 503"],
        orphans_seen=4,
        cards_classified=3,
        prompt_tokens=42,
        completion_tokens=17,
    )
    jsonl_path, md_path = db.write_reports(tmp_path, "2026-07-03", "test-model", result)
    lines = [json.loads(line) for line in jsonl_path.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["entity_id"] == "id-1"
    assert lines[0]["run_date"] == "2026-07-03"
    assert lines[0]["model"] == "test-model"
    md = md_path.read_text()
    assert "memory" in md and "unknown" in md
    assert "invalid_domain" in md
    assert "HTTP 503" in md
    assert "42" in md  # tokens visibles
```

- [ ] **Step 2: Vérifier l'échec**

Run: `.venv/bin/python -m pytest tests/unit/test_domain_backfill.py -q -k "run_backfill or reports"`
Expected: FAIL — attributs manquants.

- [ ] **Step 3: Implémentation**

**Imports à AJOUTER en tête du module** : remplacer `from collections.abc import Awaitable, Callable` par `from collections.abc import Awaitable, Callable, Iterable` · remplacer `from dataclasses import dataclass` par `from dataclasses import asdict, dataclass`.

```python
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
    """Fetch → batch → classify → agrège. Un batch failed ne tue pas le run."""
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
    """Écrit <date>.jsonl (propositions pures) + <date>.md (résumé humain)."""
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
    for domain in sorted(by_domain):
        lines.append(f"### {domain} ({len(by_domain[domain])})")
        lines.append("")
        lines.append("| confiance | type | titre | projet | raison |")
        lines.append("|---|---|---|---|---|")
        order = {"high": 0, "medium": 1, "low": 2}
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
```

- [ ] **Step 4: Vérifier le vert**

Run: `.venv/bin/python -m pytest tests/unit/test_domain_backfill.py -q`
Expected: PASS.

- [ ] **Step 5: Gate + commit**

```bash
.venv/bin/ruff check scripts/ tests/unit/test_domain_backfill.py && \
.venv/bin/ruff format --check scripts/ tests/unit/test_domain_backfill.py && \
.venv/bin/mypy src/ scripts/domain_backfill.py && .venv/bin/python -m pytest tests/unit -q
git add scripts/domain_backfill.py tests/unit/test_domain_backfill.py
git commit -m "feat(backfill): orchestrateur run_backfill + rapports jsonl/md

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: CLI main + env example + smoke run documenté

**Files:**
- Modify: `scripts/domain_backfill.py`
- Create: `deploy/nvidia.env.example`
- Test: `tests/unit/test_domain_backfill.py`

**Interfaces:**
- Consomme : tout ce qui précède + `Settings` (`postgres_url`, `neo4j_url`, `neo4j_user`, `neo4j_password`, `neo4j_timeout`), `GraphService`, `AsyncGraphDatabase` (pattern `scripts/init_graph.py:29`)
- Produit : `main(argv: list[str] | None = None) -> int` (codes : 0 ok — même avec batches failed, visibles dans le rapport ; 1 erreur infra/fetch ; 2 erreur config).

- [ ] **Step 1: Tests RED**

```python
# ── Task 5 : CLI ─────────────────────────────────────────────────────


def test_main_missing_api_key_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("BRAIN_NVIDIA_API_KEY", raising=False)
    rc = db.main(["--env-file", str(tmp_path / "absent.env")])
    assert rc == 2
    assert "BRAIN_NVIDIA_API_KEY" in capsys.readouterr().err


def test_parse_args_defaults() -> None:
    args = db.parse_args([])
    assert args.limit == 30
    assert args.batch_size == 15
    assert args.model is None            # résolu ensuite : env puis DEFAULT_MODEL
    assert args.base_url is None
    assert args.env_file == db.DEFAULT_ENV_FILE
    assert args.out_dir == Path("logs/domain_backfill")


def test_resolve_model_and_base_url_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_NVIDIA_MODEL", "env-model")
    monkeypatch.setenv("BRAIN_NVIDIA_BASE_URL", "https://env.example/v1")
    assert db.resolve_option(None, "BRAIN_NVIDIA_MODEL", db.DEFAULT_MODEL) == "env-model"
    assert (
        db.resolve_option("cli-model", "BRAIN_NVIDIA_MODEL", db.DEFAULT_MODEL)
        == "cli-model"
    )
    monkeypatch.delenv("BRAIN_NVIDIA_MODEL")
    assert (
        db.resolve_option(None, "BRAIN_NVIDIA_MODEL", db.DEFAULT_MODEL)
        == db.DEFAULT_MODEL
    )


def test_main_warns_on_loose_env_file_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fichier env en 0644 → warning stderr (puis exit 2 : pas de clé dedans)."""
    monkeypatch.delenv("BRAIN_NVIDIA_API_KEY", raising=False)
    f = tmp_path / "nvidia.env"
    f.write_text("# vide\n")
    f.chmod(0o644)
    rc = db.main(["--env-file", str(f)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "chmod 600" in err
```

- [ ] **Step 2: Vérifier l'échec**

Run: `.venv/bin/python -m pytest tests/unit/test_domain_backfill.py -q -k "main or parse_args or resolve"`
Expected: FAIL — `parse_args`/`resolve_option`/`main` absents.

- [ ] **Step 3: Implémentation**

**Imports à AJOUTER en tête du module** : `import argparse` · `import datetime as dt` · `import sys` · remplacer `from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker` par `from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine`.

```python
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="domain_backfill",
        description="Proposer-only domain classification of graph orphans (NVIDIA API).",
    )
    parser.add_argument("--limit", type=int, default=30, help="max orphans à traiter")
    parser.add_argument(
        "--batch-size", type=int, default=15, help="entités par requête LLM"
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
        settings = Settings()
    except ValidationError as exc:
        print(f"Config invalide (env/.env manquant ?) : {exc}", file=sys.stderr)
        return 2
    if not settings.neo4j_url:
        print("NEO4J_URL absent de la config — requis pour lister les orphans.", file=sys.stderr)
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
            graph_service, session_factory, classify_fn,
            limit=args.limit, batch_size=args.batch_size,
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
        f" rejections={len(result.rejections)} failed_batches={len(result.failed_batches)}"
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
```

`deploy/nvidia.env.example` :

```bash
# NVIDIA build API key pour scripts/domain_backfill.py (proposer-only).
# Copier vers ~/.config/brain-v42/nvidia.env  (chmod 600).
# Ne PAS quoter la valeur — parsing style systemd (tout après le premier '=').
# Même clé que red-shrik frontier possible, fichier séparé = rotation indépendante.
BRAIN_NVIDIA_API_KEY=nvapi-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Optionnels (précédence : CLI > env > défaut) :
# BRAIN_NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
# BRAIN_NVIDIA_MODEL=deepseek-ai/deepseek-v4-pro
# Alternative agentique-safe si deepseek déraille : moonshotai/kimi-k2-instruct
```

- [ ] **Step 4: Vérifier le vert**

Run: `.venv/bin/python -m pytest tests/unit/test_domain_backfill.py -q`
Expected: PASS.

- [ ] **Step 5: Vérifier que `logs/` est gitignoré**

Run: `git check-ignore logs/domain_backfill 2>/dev/null && echo IGNORED || echo NOT_IGNORED`
Si `NOT_IGNORED` : ajouter `logs/` au `.gitignore` dans ce commit.

- [ ] **Step 6: Gate + commit**

```bash
.venv/bin/ruff check scripts/ tests/unit/test_domain_backfill.py && \
.venv/bin/ruff format --check scripts/ tests/unit/test_domain_backfill.py && \
.venv/bin/mypy src/ scripts/domain_backfill.py && .venv/bin/python -m pytest tests/unit -q
git add scripts/domain_backfill.py tests/unit/test_domain_backfill.py deploy/nvidia.env.example
git commit -m "feat(backfill): CLI main — wiring Settings/GraphService/NVIDIA + env example

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 7 (post-merge, manuel, PAS en CI): smoke run réel**

Documenté ici pour l'opérateur (le user fournit la clé) :

```bash
mkdir -p ~/.config/brain-v42 && cp deploy/nvidia.env.example ~/.config/brain-v42/nvidia.env
chmod 600 ~/.config/brain-v42/nvidia.env && $EDITOR ~/.config/brain-v42/nvidia.env  # coller la clé
cd /home/hawixs/hawkixs_infra/git_repo/brain_v42
.venv/bin/python -m scripts.domain_backfill --limit 4 --batch-size 2   # mini-run de validation
# puis lire logs/domain_backfill/<date>.md et juger la qualité
```

---

## Self-review + critique 3 juges (2026-07-03)

- **Couverture design** : fetch direct ✓ · batches 15 ✓ · JSON strict sans tools ✓ · retry 3 tentatives (2 retries) + re-prompt ×1 ✓ · validation set fermé + unknown + id ∈ batch ✓ · rapports jsonl+md ✓ · clé env-file (+ warning perms) ✓ · CLI flags ✓ · zéro écriture ✓ · TDD mocké ✓.
- **Placeholders** : aucun TBD/TODO ; chaque step a le code.
- **Cohérence de types** : `classify_fn` (Task 4/5) == partial de `classify_batch` (Task 3) — signatures alignées ; `run_backfill` consomme `GraphServiceLike` (Task 1).
- **Patches post-critique (3 juges parallèles)** : [CRITICAL] bind asyncpg `uuid[]` en `sa.text()` → réécrit en core `select()` + `.in_()` sur les Table objects de `brain_v42.db.tables` (supprime aussi la f-string SQL et son noqa) ; [HIGH] `Settings()` wrappé `ValidationError` → exit 2 ; [HIGH] pollution `os.environ` inter-tests → clés neutres `X_BACKFILL_TEST_*` ; [MED] typing `sleep` élargi `Awaitable[Any]` ; [MED] gate mypy ciblé `scripts/domain_backfill.py` ; helpers de test typés ; lignes ≤100 ; warning perms env-file. Écartés avec preuve : `loop_scope` fixture (pattern `test_promote_prepare.py` vert aujourd'hui — suite 2514 pass), markers asyncio redondants (convention repo existante), footer commit (directive harness session).
