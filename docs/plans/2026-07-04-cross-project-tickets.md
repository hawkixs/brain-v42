# Tickets Cross-Projet — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Canal de coordination inter-projets dans brain-v42 : tickets adressés (request/fyi) avec fil de messages, machine à états, 5 tools MCP, section briefing, et job dream d'extraction de connaissance proposer-only.

**Architecture:** Nouvelle famille "coordination" orthogonale à la famille mémoire — 3 tables PG (`tickets`, `ticket_messages`, `ticket_extraction_proposals`), **hors** embeddings/search/decay/graph. Repository + Service (machine à états pure en table de transitions), tools MCP closures FastMCP, section briefing avec graceful-degrade, script `scripts/ticket_extract.py` (pattern NVIDIA/deepseek de `domain_backfill.py`) câblé dans `dream.sh` derrière killswitch `BRAIN_DREAM_EXTRACT_*`.

**Tech Stack:** Python 3.12+, SQLAlchemy 2.0 Core async, Alembic (raw SQL `op.execute`), Pydantic 2, FastMCP, structlog, httpx (NVIDIA API), pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-07-04-cross-project-tickets-design.md`

## Global Constraints

- `project_key` canonique kebab-case — TOUJOURS passer par `canonicalize_project_key` (write path strict). Le brain project_key est `brain-v42` (tiret).
- Les tickets sont **exclus** de : `brain_search`, embeddings, decay, classification domaines, sync Neo4j. Aucun code de cette feature ne touche ces sous-systèmes (sauf l'apply de l'extraction qui crée des learnings/decisions **via les services existants**).
- Tools MCP : retour **toujours `str`** (`format_confirmation`/`format_error`), jamais d'exception brute.
- Logging : structlog uniquement, jamais `print` (sauf CLI scripts où `print` vers stdout est le pattern existant de `domain_backfill.py`).
- Type hints partout ; I/O en async.
- Vert AVANT commit : `pytest tests/unit -q`, `ruff check src/ tests/ scripts/`, `ruff format --check src/ tests/ scripts/`, `mypy src/`.
- Conventional Commits ; un commit par tâche.
- Coverage globale ≥ 60 % (gate CI).
- Tests DB-backed : gate par `require_test_db_url()` (skip si `BRAIN_V42_TEST_DB_URL` absent) — jamais de fallback sur la prod.

## File Structure

| Fichier | Responsabilité | Tâche |
|---|---|---|
| `src/brain_v42/models/ticket.py` (create) | Enums, modèles Pydantic, table de transitions (machine à états pure) | 1 |
| `src/brain_v42/db/tables.py` (modify) | Tables Core `tickets`, `ticket_messages`, `ticket_extraction_proposals` | 1, 5 |
| `alembic/versions/028_tickets.py` (create) | Migration tickets + ticket_messages | 1 |
| `src/brain_v42/models/__init__.py` (modify) | Exports des nouveaux modèles | 1 |
| `src/brain_v42/repositories/pg_ticket.py` (create) | Accès PG tickets/messages (CRUD + requêtes groupées) | 2 |
| `src/brain_v42/services/ticket_service.py` (create) | Règles métier : validation projets, machine à états, side-effects | 2 |
| `src/brain_v42/mcp/tools/ticket_tools.py` (create) | 5 tools MCP + formatters locaux | 3 |
| `src/brain_v42/mcp/server.py` (modify) | build_services + registration | 3, 4 |
| `src/brain_v42/mcp/tools/session_tools.py` (modify) | Section briefing `### Tickets` | 4 |
| `alembic/versions/029_ticket_extraction_proposals.py` (create) | Migration proposals | 5 |
| `scripts/ticket_extract.py` (create) | Extraction proposer-only : fetch → LLM → proposals → apply | 5 |
| `scripts/dream.sh` (modify) | Killswitch EXTRACT + step extract post-phases | 6 |
| `src/brain_v42/services/dream_run_service.py` (modify) | `KillswitchState` + champs extract | 6 |
| `docs/MCP_TOOLS.md`, `docs/SCHEMA.md`, `CLAUDE.md` (modify) | Documentation | 7 |

---

### Task 1: Modèles Pydantic + tables + migration 028

**Files:**
- Create: `src/brain_v42/models/ticket.py`
- Modify: `src/brain_v42/db/tables.py` (ajouter 2 tables à la fin, avant tout `__all__` éventuel)
- Create: `alembic/versions/028_tickets.py`
- Modify: `src/brain_v42/models/__init__.py` (exports)
- Modify: `tests/unit/models/test_models.py` (la liste `expected_classes` pin les exports — l'étendre)
- Test: `tests/unit/models/test_ticket_models.py`

**Interfaces:**
- Consumes: `brain_v42.models.base.TimestampMixin`, `brain_v42.models.project_key.canonicalize_project_key`
- Produces (utilisés par les tâches 2-5) :
  - `TicketKind` (StrEnum: `request`/`fyi`), `TicketStatus` (StrEnum: `open`/`in_progress`/`resolved`/`wontfix`/`closed`/`acked`), `TicketAction` (StrEnum: `start`/`resolve`/`wontfix`/`confirm`/`reopen`/`ack`/`cancel`), `ExtractionStatus` (StrEnum: `pending`/`proposed`/`skipped`/`done`)
  - `TERMINAL_STATUSES: frozenset[TicketStatus]`
  - `TRANSITIONS: dict[tuple[TicketKind, TicketStatus, TicketAction], tuple[Role, TicketStatus]]` avec `Role = Literal["executor", "requester"]`
  - `allowed_actions(kind: TicketKind, status: TicketStatus) -> list[str]`
  - `TicketCreate(kind, title, body, from_project, to_project)`, `Ticket` (id, status, extraction_status, resolved_at, closed_at, + timestamps), `TicketMessage(id, ticket_id, author_project, body, status_to, created_at)`, `TicketGroups(a_traiter, a_confirmer, en_attente)`
  - Tables Core : `brain_v42.db.tables.tickets`, `brain_v42.db.tables.ticket_messages`

- [ ] **Step 1: Écrire les tests qui échouent**

`tests/unit/models/test_ticket_models.py` :

```python
"""Unit tests for ticket models and the pure transition table."""

import pytest
from pydantic import ValidationError

from brain_v42.models.ticket import (
    TERMINAL_STATUSES,
    TRANSITIONS,
    ExtractionStatus,
    Ticket,
    TicketAction,
    TicketCreate,
    TicketKind,
    TicketMessage,
    TicketStatus,
    allowed_actions,
)


class TestTicketCreate:
    def test_canonicalizes_both_project_keys(self):
        t = TicketCreate(
            kind=TicketKind.REQUEST,
            title="t",
            body="b",
            from_project="brain_v42",  # confusable connu → brain-v42
            to_project="red-data",
        )
        assert t.from_project == "brain-v42"
        assert t.to_project == "red-data"

    def test_rejects_non_kebab_project_key(self):
        with pytest.raises(ValidationError):
            TicketCreate(
                kind=TicketKind.FYI,
                title="t",
                body="b",
                from_project="Red Data",
                to_project="red-shrik",
            )

    def test_title_max_200(self):
        with pytest.raises(ValidationError):
            TicketCreate(
                kind=TicketKind.REQUEST,
                title="x" * 201,
                body="b",
                from_project="red-shrik",
                to_project="red-data",
            )


class TestTicketDefaults:
    def test_new_ticket_defaults(self):
        t = Ticket(
            kind=TicketKind.REQUEST,
            title="t",
            body="b",
            from_project="red-shrik",
            to_project="red-data",
        )
        assert t.status is TicketStatus.OPEN
        assert t.extraction_status is None
        assert t.resolved_at is None
        assert t.closed_at is None

    def test_message_status_to_optional(self):
        m = TicketMessage(ticket_id=Ticket(
            kind=TicketKind.FYI, title="t", body="b",
            from_project="a-b", to_project="c-d",
        ).id, author_project="a-b", body="hello")
        assert m.status_to is None


class TestTransitionTable:
    def test_terminal_statuses(self):
        assert TERMINAL_STATUSES == frozenset({TicketStatus.CLOSED, TicketStatus.ACKED})

    @pytest.mark.parametrize(
        ("kind", "status", "action", "role", "new_status"),
        [
            (TicketKind.REQUEST, TicketStatus.OPEN, TicketAction.START, "executor", TicketStatus.IN_PROGRESS),
            (TicketKind.REQUEST, TicketStatus.OPEN, TicketAction.RESOLVE, "executor", TicketStatus.RESOLVED),
            (TicketKind.REQUEST, TicketStatus.OPEN, TicketAction.WONTFIX, "executor", TicketStatus.WONTFIX),
            (TicketKind.REQUEST, TicketStatus.IN_PROGRESS, TicketAction.RESOLVE, "executor", TicketStatus.RESOLVED),
            (TicketKind.REQUEST, TicketStatus.IN_PROGRESS, TicketAction.WONTFIX, "executor", TicketStatus.WONTFIX),
            (TicketKind.REQUEST, TicketStatus.RESOLVED, TicketAction.CONFIRM, "requester", TicketStatus.CLOSED),
            (TicketKind.REQUEST, TicketStatus.WONTFIX, TicketAction.CONFIRM, "requester", TicketStatus.CLOSED),
            (TicketKind.REQUEST, TicketStatus.RESOLVED, TicketAction.REOPEN, "requester", TicketStatus.OPEN),
            (TicketKind.REQUEST, TicketStatus.WONTFIX, TicketAction.REOPEN, "requester", TicketStatus.OPEN),
            (TicketKind.REQUEST, TicketStatus.OPEN, TicketAction.CANCEL, "requester", TicketStatus.CLOSED),
            (TicketKind.REQUEST, TicketStatus.IN_PROGRESS, TicketAction.CANCEL, "requester", TicketStatus.CLOSED),
            (TicketKind.REQUEST, TicketStatus.RESOLVED, TicketAction.CANCEL, "requester", TicketStatus.CLOSED),
            (TicketKind.REQUEST, TicketStatus.WONTFIX, TicketAction.CANCEL, "requester", TicketStatus.CLOSED),
            (TicketKind.FYI, TicketStatus.OPEN, TicketAction.ACK, "executor", TicketStatus.ACKED),
            (TicketKind.FYI, TicketStatus.OPEN, TicketAction.CANCEL, "requester", TicketStatus.CLOSED),
        ],
    )
    def test_legal_transitions(self, kind, status, action, role, new_status):
        assert TRANSITIONS[(kind, status, action)] == (role, new_status)

    def test_exactly_15_legal_transitions(self):
        # Pin la surface complète : les 15 légales sont énumérées ci-dessus et
        # ce count garantit qu'AUCUNE autre combinaison (kind × status × action,
        # 84 au total) n'est légale — la matrice illégale est couverte par
        # construction (spec §8), les cas ci-dessous ne sont que documentaires.
        assert len(TRANSITIONS) == 15

    @pytest.mark.parametrize(
        ("kind", "status", "action"),
        [
            (TicketKind.REQUEST, TicketStatus.CLOSED, TicketAction.REOPEN),  # terminal
            (TicketKind.REQUEST, TicketStatus.OPEN, TicketAction.ACK),  # ack = fyi only
            (TicketKind.REQUEST, TicketStatus.OPEN, TicketAction.CONFIRM),  # rien à confirmer
            (TicketKind.FYI, TicketStatus.OPEN, TicketAction.RESOLVE),  # fyi ne se résout pas
            (TicketKind.FYI, TicketStatus.ACKED, TicketAction.ACK),  # terminal
            (TicketKind.FYI, TicketStatus.OPEN, TicketAction.START),
        ],
    )
    def test_illegal_transitions_absent(self, kind, status, action):
        assert (kind, status, action) not in TRANSITIONS

    def test_allowed_actions_open_request(self):
        assert allowed_actions(TicketKind.REQUEST, TicketStatus.OPEN) == [
            "cancel", "resolve", "start", "wontfix",
        ]

    def test_allowed_actions_terminal_is_empty(self):
        assert allowed_actions(TicketKind.FYI, TicketStatus.ACKED) == []


class TestExtractionStatus:
    def test_values(self):
        assert {e.value for e in ExtractionStatus} == {"pending", "proposed", "skipped", "done"}
```

- [ ] **Step 2: Vérifier l'échec**

Run: `pytest tests/unit/models/test_ticket_models.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'brain_v42.models.ticket'`

- [ ] **Step 3: Implémenter `src/brain_v42/models/ticket.py`**

```python
"""Pydantic models for cross-project tickets (coordination family).

Tickets are NOT knowledge entities: no embedding, no decay, no search,
no domain classification, no graph sync (spec §1,
docs/superpowers/specs/2026-07-04-cross-project-tickets-design.md).
The only bridge to memory is the extraction job (spec §6).

Self-tickets (from_project == to_project) are allowed by design: they act
as a note-to-next-session; both roles then collapse onto the same project.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator

from brain_v42.models.base import TimestampMixin
from brain_v42.models.project_key import canonicalize_project_key


class TicketKind(StrEnum):
    REQUEST = "request"
    FYI = "fyi"


class TicketStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    WONTFIX = "wontfix"
    CLOSED = "closed"
    ACKED = "acked"


class TicketAction(StrEnum):
    START = "start"
    RESOLVE = "resolve"
    WONTFIX = "wontfix"
    CONFIRM = "confirm"
    REOPEN = "reopen"
    ACK = "ack"
    CANCEL = "cancel"


class ExtractionStatus(StrEnum):
    PENDING = "pending"
    PROPOSED = "proposed"
    SKIPPED = "skipped"
    DONE = "done"


TERMINAL_STATUSES: frozenset[TicketStatus] = frozenset(
    {TicketStatus.CLOSED, TicketStatus.ACKED}
)

Role = Literal["executor", "requester"]

# (kind, current_status, action) -> (required_role, new_status)
# executor = author == to_project ; requester = author == from_project.
# Toute action absente de cette table est illégale. La discussion (reply)
# n'est PAS une transition : elle est permise quel que soit l'état.
TRANSITIONS: dict[
    tuple[TicketKind, TicketStatus, TicketAction], tuple[Role, TicketStatus]
] = {
    # request — exécutant
    (TicketKind.REQUEST, TicketStatus.OPEN, TicketAction.START): ("executor", TicketStatus.IN_PROGRESS),
    (TicketKind.REQUEST, TicketStatus.OPEN, TicketAction.RESOLVE): ("executor", TicketStatus.RESOLVED),
    (TicketKind.REQUEST, TicketStatus.OPEN, TicketAction.WONTFIX): ("executor", TicketStatus.WONTFIX),
    (TicketKind.REQUEST, TicketStatus.IN_PROGRESS, TicketAction.RESOLVE): ("executor", TicketStatus.RESOLVED),
    (TicketKind.REQUEST, TicketStatus.IN_PROGRESS, TicketAction.WONTFIX): ("executor", TicketStatus.WONTFIX),
    # request — demandeur (boucle de confirmation complète)
    (TicketKind.REQUEST, TicketStatus.RESOLVED, TicketAction.CONFIRM): ("requester", TicketStatus.CLOSED),
    (TicketKind.REQUEST, TicketStatus.WONTFIX, TicketAction.CONFIRM): ("requester", TicketStatus.CLOSED),
    (TicketKind.REQUEST, TicketStatus.RESOLVED, TicketAction.REOPEN): ("requester", TicketStatus.OPEN),
    (TicketKind.REQUEST, TicketStatus.WONTFIX, TicketAction.REOPEN): ("requester", TicketStatus.OPEN),
    (TicketKind.REQUEST, TicketStatus.OPEN, TicketAction.CANCEL): ("requester", TicketStatus.CLOSED),
    (TicketKind.REQUEST, TicketStatus.IN_PROGRESS, TicketAction.CANCEL): ("requester", TicketStatus.CLOSED),
    (TicketKind.REQUEST, TicketStatus.RESOLVED, TicketAction.CANCEL): ("requester", TicketStatus.CLOSED),
    (TicketKind.REQUEST, TicketStatus.WONTFIX, TicketAction.CANCEL): ("requester", TicketStatus.CLOSED),
    # fyi — open → acked, cancel possible par l'émetteur
    (TicketKind.FYI, TicketStatus.OPEN, TicketAction.ACK): ("executor", TicketStatus.ACKED),
    (TicketKind.FYI, TicketStatus.OPEN, TicketAction.CANCEL): ("requester", TicketStatus.CLOSED),
}


def allowed_actions(kind: TicketKind, status: TicketStatus) -> list[str]:
    """Actions légales (triées) depuis un état donné — pour les messages d'erreur et l'UX."""
    return sorted(a.value for (k, s, a) in TRANSITIONS if k == kind and s == status)


class TicketBase(BaseModel):
    kind: TicketKind
    title: str = Field(..., max_length=200)
    body: str
    from_project: str = Field(..., max_length=50)
    to_project: str = Field(..., max_length=50)

    @field_validator("from_project", "to_project")
    @classmethod
    def _canonicalize(cls, v: str) -> str:
        return canonicalize_project_key(v)


class TicketCreate(TicketBase):
    pass


class Ticket(TicketBase, TimestampMixin):
    id: UUID = Field(default_factory=uuid4)
    status: TicketStatus = TicketStatus.OPEN
    extraction_status: ExtractionStatus | None = None
    resolved_at: datetime | None = None
    closed_at: datetime | None = None

    model_config = {"from_attributes": True}


class TicketMessage(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    ticket_id: UUID
    author_project: str = Field(..., max_length=50)
    body: str
    status_to: TicketStatus | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("author_project")
    @classmethod
    def _canonicalize(cls, v: str) -> str:
        return canonicalize_project_key(v)

    model_config = {"from_attributes": True}


class TicketGroups(BaseModel):
    """Vue groupée par action pour brain_ticket_list / le briefing."""

    a_traiter: list[Ticket] = Field(default_factory=list)
    a_confirmer: list[Ticket] = Field(default_factory=list)
    en_attente: list[Ticket] = Field(default_factory=list)
```

- [ ] **Step 4: Ajouter les 2 tables dans `src/brain_v42/db/tables.py`** (à la fin du fichier, style Core existant)

```python
# --- Tickets (coordination family — spec 2026-07-04) -----------------------
# PAS d'embedding, PAS de search_vector, PAS de colonnes decay : les tickets
# sont du transient adressé, hors famille mémoire (spec §1).

tickets = Table(
    "tickets",
    METADATA,
    Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    Column("kind", String(10), nullable=False),
    Column("title", String(200), nullable=False),
    Column("body", Text, nullable=False),
    Column("from_project", String(50), nullable=False),
    Column("to_project", String(50), nullable=False),
    Column("status", String(15), nullable=False, server_default=sa.text("'open'")),
    Column("extraction_status", String(10), nullable=True),
    Column("resolved_at", DateTime(timezone=True), nullable=True),
    Column("closed_at", DateTime(timezone=True), nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    sa.CheckConstraint("kind IN ('request', 'fyi')", name="tickets_kind_valid"),
    sa.CheckConstraint(
        "status IN ('open', 'in_progress', 'resolved', 'wontfix', 'closed', 'acked')",
        name="tickets_status_valid",
    ),
    sa.CheckConstraint(
        "extraction_status IS NULL OR "
        "extraction_status IN ('pending', 'proposed', 'skipped', 'done')",
        name="tickets_extraction_status_valid",
    ),
    Index("idx_tickets_to_project_status", "to_project", "status"),
    Index("idx_tickets_from_project_status", "from_project", "status"),
    Index(
        "idx_tickets_extraction_pending",
        "extraction_status",
        postgresql_where=sa.text("extraction_status = 'pending'"),
    ),
)

ticket_messages = Table(
    "ticket_messages",
    METADATA,
    Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    Column(
        "ticket_id",
        UUID(as_uuid=True),
        sa.ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("author_project", String(50), nullable=False),
    Column("body", Text, nullable=False),
    Column("status_to", String(15), nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    Index("idx_ticket_messages_ticket", "ticket_id", "created_at"),
)
```

- [ ] **Step 5: Créer `alembic/versions/028_tickets.py`** (pattern raw-SQL de `016_dream_promotions.py` ; head actuelle = `027`)

```python
"""Tickets cross-projet : tables tickets + ticket_messages.

Famille coordination (spec 2026-07-04) — pas d'embedding, pas de
search_vector, pas de decay. Voir
docs/superpowers/specs/2026-07-04-cross-project-tickets-design.md.

Revision ID: 028
Revises: 027
"""

from alembic import op

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE tickets (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            kind VARCHAR(10) NOT NULL,
            title VARCHAR(200) NOT NULL,
            body TEXT NOT NULL,
            from_project VARCHAR(50) NOT NULL,
            to_project VARCHAR(50) NOT NULL,
            status VARCHAR(15) NOT NULL DEFAULT 'open',
            extraction_status VARCHAR(10),
            resolved_at TIMESTAMPTZ,
            closed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT tickets_kind_valid CHECK (kind IN ('request', 'fyi')),
            CONSTRAINT tickets_status_valid CHECK (
                status IN ('open', 'in_progress', 'resolved', 'wontfix', 'closed', 'acked')
            ),
            CONSTRAINT tickets_extraction_status_valid CHECK (
                extraction_status IS NULL
                OR extraction_status IN ('pending', 'proposed', 'skipped', 'done')
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_tickets_to_project_status ON tickets (to_project, status)"
    )
    op.execute(
        "CREATE INDEX idx_tickets_from_project_status ON tickets (from_project, status)"
    )
    op.execute(
        "CREATE INDEX idx_tickets_extraction_pending ON tickets (extraction_status)"
        " WHERE extraction_status = 'pending'"
    )
    op.execute(
        """
        CREATE TABLE ticket_messages (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ticket_id UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
            author_project VARCHAR(50) NOT NULL,
            body TEXT NOT NULL,
            status_to VARCHAR(15),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_ticket_messages_ticket ON ticket_messages (ticket_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ticket_messages;")
    op.execute("DROP TABLE IF EXISTS tickets;")
```

- [ ] **Step 6: Exports** — dans `src/brain_v42/models/__init__.py`, ajouter (en respectant l'ordre alphabétique existant des imports et de `__all__`) :

```python
from brain_v42.models.ticket import (
    ExtractionStatus,
    Ticket,
    TicketCreate,
    TicketGroups,
    TicketKind,
    TicketMessage,
    TicketStatus,
)
```

et les 7 noms dans `__all__`. Puis dans `tests/unit/models/test_models.py`, étendre **les deux listes** qui pinnent les exports : `expected_classes` dans `test_all_classes_importable_from_models` (~ligne 564) ET la liste de `test_all_classes_in_dunder_all` (~ligne 596) — les 7 mêmes noms dans chacune.

- [ ] **Step 7: Vérifier vert**

Run: `pytest tests/unit/models/ -q && ruff check src/brain_v42/models/ src/brain_v42/db/ alembic/versions/028_tickets.py && ruff format --check src/ tests/ && mypy src/`
Expected: PASS / clean

- [ ] **Step 8: Commit**

```bash
git add src/brain_v42/models/ticket.py src/brain_v42/models/__init__.py src/brain_v42/db/tables.py alembic/versions/028_tickets.py tests/unit/models/
git commit -m "feat(tickets): modèles + machine à états + tables + migration 028"
```

---

### Task 2: Repository + TicketService

**Files:**
- Create: `src/brain_v42/repositories/pg_ticket.py`
- Create: `src/brain_v42/services/ticket_service.py`
- Test: `tests/unit/services/test_ticket_service.py`
- Test (DB-gated): `tests/integration/db/test_tickets_roundtrip.py`

**Interfaces:**
- Consumes: Task 1 (`Ticket*` models, `TRANSITIONS`, `TERMINAL_STATUSES`, `allowed_actions`), `BasePgRepository` (`get_session`, `_session_factory`), `PgProjectContextRepo.get_by_key(project_key) -> ProjectContext | None`, `canonicalize_project_key`
- Produces (utilisés par les tâches 3-5) :
  - `PgTicketRepo(session_factory)` : `create(TicketCreate) -> Ticket` ; `get_by_id(UUID) -> Ticket | None` ; `get_messages(UUID) -> list[TicketMessage]` ; `add_message(ticket_id: UUID, author_project: str, body: str, status_to: TicketStatus | None = None) -> TicketMessage` ; `apply_transition(ticket_id: UUID, new_status: TicketStatus, *, resolved_at, closed_at, extraction_status) -> Ticket` ; `list_grouped(project_key: str) -> TicketGroups`
  - `TicketService(repo, project_context_repo)` : `create(TicketCreate) -> Ticket` ; `reply(ticket_id: UUID, author_project: str, body: str) -> TicketMessage` ; `transition(ticket_id: UUID, author_project: str, action: str, message: str | None = None) -> Ticket` ; `get_with_thread(ticket_id: UUID) -> tuple[Ticket, list[TicketMessage]] | None` ; `list_grouped(project_key: str) -> TicketGroups`
  - Exceptions : `TicketError(Exception)` base ; `UnknownProjectError`, `TicketNotFoundError`, `NotAllowedError`, `IllegalTransitionError`

- [ ] **Step 1: Tests unitaires du service (repo mocké)** — `tests/unit/services/test_ticket_service.py`

```python
"""Unit tests for TicketService — state machine enforcement with mocked repo."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.models.ticket import (
    ExtractionStatus,
    Ticket,
    TicketCreate,
    TicketKind,
    TicketStatus,
)
from brain_v42.services.ticket_service import (
    IllegalTransitionError,
    NotAllowedError,
    TicketNotFoundError,
    TicketService,
    UnknownProjectError,
)

# PAS de pytestmark : pyproject a asyncio_mode = "auto", les unit tests du
# repo écrivent des `async def test_*` nus (cf. tests/unit existants).

FROM, TO = "red-shrik", "red-data"


def _ticket(kind=TicketKind.REQUEST, status=TicketStatus.OPEN, **kw) -> Ticket:
    return Ticket(
        kind=kind, title="t", body="b",
        from_project=FROM, to_project=TO, status=status, **kw,
    )


def _svc(ticket=None, known_projects=(FROM, TO)):
    repo = MagicMock()
    repo.get_by_id = AsyncMock(return_value=ticket)
    repo.create = AsyncMock(side_effect=lambda data: _ticket(kind=data.kind))
    repo.add_message = AsyncMock()
    # apply_transition renvoie le ticket muté (echo simplifié pour les tests)
    async def _apply(ticket_id, new_status, *, resolved_at, closed_at, extraction_status):
        return _ticket(
            kind=ticket.kind if ticket else TicketKind.REQUEST,
            status=new_status,
            resolved_at=resolved_at,
            closed_at=closed_at,
            extraction_status=extraction_status,
        )
    repo.apply_transition = AsyncMock(side_effect=_apply)
    ctx_repo = MagicMock()
    ctx_repo.get_by_key = AsyncMock(
        side_effect=lambda key: MagicMock() if key in known_projects else None
    )
    return TicketService(repo=repo, project_context_repo=ctx_repo), repo, ctx_repo


class TestCreate:
    async def test_create_validates_both_projects_exist(self):
        svc, repo, ctx_repo = _svc()
        data = TicketCreate(
            kind=TicketKind.REQUEST, title="t", body="b",
            from_project=FROM, to_project=TO,
        )
        await svc.create(data)
        assert ctx_repo.get_by_key.await_count == 2
        repo.create.assert_awaited_once()

    async def test_create_rejects_unknown_to_project(self):
        svc, repo, _ = _svc(known_projects=(FROM,))
        data = TicketCreate(
            kind=TicketKind.REQUEST, title="t", body="b",
            from_project=FROM, to_project=TO,
        )
        with pytest.raises(UnknownProjectError, match="red-data"):
            await svc.create(data)
        repo.create.assert_not_awaited()


class TestTransition:
    async def test_resolve_by_executor_sets_resolved_at(self):
        svc, repo, _ = _svc(ticket=_ticket())
        updated = await svc.transition(uuid4(), TO, "resolve")
        assert updated.status is TicketStatus.RESOLVED
        kwargs = repo.apply_transition.await_args.kwargs
        assert kwargs["resolved_at"] is not None
        assert kwargs["closed_at"] is None
        assert kwargs["extraction_status"] is None

    async def test_resolve_by_requester_forbidden(self):
        svc, _, _ = _svc(ticket=_ticket())
        with pytest.raises(NotAllowedError, match="executor"):
            await svc.transition(uuid4(), FROM, "resolve")

    async def test_confirm_by_requester_closes_and_marks_extraction(self):
        svc, repo, _ = _svc(ticket=_ticket(status=TicketStatus.RESOLVED))
        updated = await svc.transition(uuid4(), FROM, "confirm")
        assert updated.status is TicketStatus.CLOSED
        kwargs = repo.apply_transition.await_args.kwargs
        assert kwargs["closed_at"] is not None
        assert kwargs["extraction_status"] is ExtractionStatus.PENDING

    async def test_confirm_by_executor_forbidden(self):
        svc, _, _ = _svc(ticket=_ticket(status=TicketStatus.RESOLVED))
        with pytest.raises(NotAllowedError, match="requester"):
            await svc.transition(uuid4(), TO, "confirm")

    async def test_reopen_clears_resolved_at(self):
        svc, repo, _ = _svc(
            ticket=_ticket(status=TicketStatus.RESOLVED, resolved_at=datetime.now(UTC))
        )
        updated = await svc.transition(uuid4(), FROM, "reopen")
        assert updated.status is TicketStatus.OPEN
        assert repo.apply_transition.await_args.kwargs["resolved_at"] is None

    async def test_ack_fyi_marks_extraction_pending(self):
        svc, repo, _ = _svc(ticket=_ticket(kind=TicketKind.FYI))
        updated = await svc.transition(uuid4(), TO, "ack")
        assert updated.status is TicketStatus.ACKED
        assert repo.apply_transition.await_args.kwargs["extraction_status"] is ExtractionStatus.PENDING

    async def test_illegal_action_lists_allowed(self):
        svc, _, _ = _svc(ticket=_ticket(kind=TicketKind.FYI))
        with pytest.raises(IllegalTransitionError, match="ack"):
            await svc.transition(uuid4(), TO, "resolve")

    async def test_unknown_action_rejected(self):
        svc, _, _ = _svc(ticket=_ticket())
        with pytest.raises(IllegalTransitionError, match="unknown action"):
            await svc.transition(uuid4(), TO, "explode")

    async def test_terminal_state_has_no_actions(self):
        svc, _, _ = _svc(ticket=_ticket(status=TicketStatus.CLOSED))
        with pytest.raises(IllegalTransitionError, match="terminal"):
            await svc.transition(uuid4(), FROM, "reopen")

    async def test_not_found(self):
        svc, _, _ = _svc(ticket=None)
        with pytest.raises(TicketNotFoundError):
            await svc.transition(uuid4(), FROM, "cancel")

    async def test_transition_with_message_writes_thread_row(self):
        svc, repo, _ = _svc(ticket=_ticket())
        await svc.transition(uuid4(), TO, "resolve", message="c'est déployé")
        repo.add_message.assert_awaited_once()
        assert repo.add_message.await_args.kwargs["status_to"] is TicketStatus.RESOLVED

    async def test_transition_without_message_writes_no_row(self):
        svc, repo, _ = _svc(ticket=_ticket())
        await svc.transition(uuid4(), TO, "resolve")
        repo.add_message.assert_not_awaited()

    async def test_third_party_project_rejected(self):
        svc, _, _ = _svc(ticket=_ticket())
        with pytest.raises(NotAllowedError):
            await svc.transition(uuid4(), "red-lab", "resolve")


class TestReply:
    async def test_reply_by_participant_ok(self):
        svc, repo, _ = _svc(ticket=_ticket())
        await svc.reply(uuid4(), FROM, "des nouvelles ?")
        repo.add_message.assert_awaited_once()

    async def test_reply_by_third_party_rejected(self):
        svc, _, _ = _svc(ticket=_ticket())
        with pytest.raises(NotAllowedError, match="participant"):
            await svc.reply(uuid4(), "red-lab", "hello")

    async def test_reply_allowed_in_terminal_state(self):
        # Le statut contraint les transitions, pas la discussion (spec §3).
        svc, repo, _ = _svc(ticket=_ticket(status=TicketStatus.CLOSED))
        await svc.reply(uuid4(), FROM, "post-mortem")
        repo.add_message.assert_awaited_once()
```

- [ ] **Step 2: Vérifier l'échec**

Run: `pytest tests/unit/services/test_ticket_service.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'brain_v42.services.ticket_service'`

- [ ] **Step 3: Implémenter `src/brain_v42/repositories/pg_ticket.py`**

```python
"""PostgreSQL repository for tickets + ticket_messages (coordination family)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
import structlog

from brain_v42.db.tables import ticket_messages, tickets
from brain_v42.models.ticket import (
    ExtractionStatus,
    Ticket,
    TicketCreate,
    TicketGroups,
    TicketMessage,
    TicketStatus,
)
from brain_v42.repositories.pg_base import BasePgRepository

logger = structlog.get_logger(__name__)

_ACTIONABLE = ("open", "in_progress")
_CONFIRMABLE = ("resolved", "wontfix")


class PgTicketRepo(BasePgRepository):
    table = tickets
    fts_columns: list[str] = []  # hors recherche — famille coordination (spec §1)

    async def create(self, data: TicketCreate) -> Ticket:  # type: ignore[override]
        values = {
            "kind": data.kind.value,
            "title": data.title,
            "body": data.body,
            "from_project": data.from_project,
            "to_project": data.to_project,
        }
        async with self.get_session() as session:
            async with session.begin():
                stmt = tickets.insert().values(**values).returning(tickets)
                row = (await session.execute(stmt)).mappings().one()
                logger.debug("pg_ticket.create", ticket_id=str(row["id"]))
                return Ticket.model_validate(dict(row))

    async def get_by_id(self, ticket_id: UUID) -> Ticket | None:
        async with self.get_session() as session:
            stmt = sa.select(tickets).where(tickets.c.id == ticket_id)
            row = (await session.execute(stmt)).mappings().first()
            return Ticket.model_validate(dict(row)) if row else None

    async def get_messages(self, ticket_id: UUID) -> list[TicketMessage]:
        async with self.get_session() as session:
            stmt = (
                sa.select(ticket_messages)
                .where(ticket_messages.c.ticket_id == ticket_id)
                .order_by(ticket_messages.c.created_at.asc())
            )
            rows = (await session.execute(stmt)).mappings().all()
            return [TicketMessage.model_validate(dict(r)) for r in rows]

    async def add_message(
        self,
        ticket_id: UUID,
        author_project: str,
        body: str,
        status_to: TicketStatus | None = None,
    ) -> TicketMessage:
        async with self.get_session() as session:
            async with session.begin():
                stmt = (
                    ticket_messages.insert()
                    .values(
                        ticket_id=ticket_id,
                        author_project=author_project,
                        body=body,
                        status_to=status_to.value if status_to else None,
                    )
                    .returning(ticket_messages)
                )
                row = (await session.execute(stmt)).mappings().one()
                # Une réponse est de l'activité : bump updated_at du ticket.
                await session.execute(
                    tickets.update()
                    .where(tickets.c.id == ticket_id)
                    .values(updated_at=sa.func.now())
                )
                return TicketMessage.model_validate(dict(row))

    async def apply_transition(
        self,
        ticket_id: UUID,
        new_status: TicketStatus,
        *,
        resolved_at: datetime | None,
        closed_at: datetime | None,
        extraction_status: ExtractionStatus | None,
    ) -> Ticket:
        async with self.get_session() as session:
            async with session.begin():
                stmt = (
                    tickets.update()
                    .where(tickets.c.id == ticket_id)
                    .values(
                        status=new_status.value,
                        resolved_at=resolved_at,
                        closed_at=closed_at,
                        extraction_status=(
                            extraction_status.value if extraction_status else None
                        ),
                        updated_at=sa.func.now(),
                    )
                    .returning(tickets)
                )
                row = (await session.execute(stmt)).mappings().one()
                logger.info(
                    "pg_ticket.transition",
                    ticket_id=str(ticket_id),
                    new_status=new_status.value,
                )
                return Ticket.model_validate(dict(row))

    async def list_grouped(self, project_key: str) -> TicketGroups:
        async with self.get_session() as session:
            def _q(col: sa.Column, statuses: tuple[str, ...]) -> sa.Select:
                return (
                    sa.select(tickets)
                    .where(col == project_key, tickets.c.status.in_(statuses))
                    .order_by(tickets.c.created_at.asc())
                )

            a_traiter = (
                (await session.execute(_q(tickets.c.to_project, _ACTIONABLE)))
                .mappings().all()
            )
            a_confirmer = (
                (await session.execute(_q(tickets.c.from_project, _CONFIRMABLE)))
                .mappings().all()
            )
            en_attente = (
                (await session.execute(_q(tickets.c.from_project, _ACTIONABLE)))
                .mappings().all()
            )
            return TicketGroups(
                a_traiter=[Ticket.model_validate(dict(r)) for r in a_traiter],
                a_confirmer=[Ticket.model_validate(dict(r)) for r in a_confirmer],
                en_attente=[Ticket.model_validate(dict(r)) for r in en_attente],
            )
```

Note : pour un self-ticket (`from == to`), un ticket `open` apparaît à la fois
dans `a_traiter` et `en_attente` — comportement voulu (note-to-self visible).

- [ ] **Step 4: Implémenter `src/brain_v42/services/ticket_service.py`**

```python
"""Business rules for cross-project tickets.

Enforce: project registry validation at create, participant checks,
and the pure transition table from models.ticket. Side effects:
resolved_at / closed_at timestamps and extraction_status=pending on
terminal states (spec §3).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

import structlog

from brain_v42.models.project_key import canonicalize_project_key
from brain_v42.models.ticket import (
    TERMINAL_STATUSES,
    TRANSITIONS,
    ExtractionStatus,
    Ticket,
    TicketAction,
    TicketCreate,
    TicketGroups,
    TicketMessage,
    TicketStatus,
    allowed_actions,
)

if TYPE_CHECKING:
    from brain_v42.repositories.pg_project_context import PgProjectContextRepo
    from brain_v42.repositories.pg_ticket import PgTicketRepo

logger = structlog.get_logger(__name__)


class TicketError(Exception):
    """Base for user-facing ticket errors (tools render str(exc))."""


class UnknownProjectError(TicketError):
    pass


class TicketNotFoundError(TicketError):
    pass


class NotAllowedError(TicketError):
    pass


class IllegalTransitionError(TicketError):
    pass


class TicketService:
    def __init__(
        self,
        repo: PgTicketRepo,
        project_context_repo: PgProjectContextRepo,
    ) -> None:
        self._repo = repo
        self._ctx_repo = project_context_repo

    async def create(self, data: TicketCreate) -> Ticket:
        # Refus si projet inconnu — leçon du drift brain_v42/brain-v42 :
        # aucune création de projet fantôme par typo (spec §2).
        for key in (data.from_project, data.to_project):
            if await self._ctx_repo.get_by_key(key) is None:
                raise UnknownProjectError(
                    f"Unknown project '{key}' — create it first "
                    f"(brain_set_project_context) or check the key "
                    f"(brain_list_projects)"
                )
        ticket = await self._repo.create(data)
        logger.info(
            "ticket.created",
            ticket_id=str(ticket.id),
            kind=ticket.kind.value,
            from_project=ticket.from_project,
            to_project=ticket.to_project,
        )
        return ticket

    async def reply(
        self, ticket_id: UUID, author_project: str, body: str
    ) -> TicketMessage:
        author = canonicalize_project_key(author_project)
        ticket = await self._get_or_raise(ticket_id)
        if author not in (ticket.from_project, ticket.to_project):
            raise NotAllowedError(
                f"'{author}' is not a participant of this ticket "
                f"({ticket.from_project} → {ticket.to_project})"
            )
        return await self._repo.add_message(ticket_id, author, body)

    async def transition(
        self,
        ticket_id: UUID,
        author_project: str,
        action: str,
        message: str | None = None,
    ) -> Ticket:
        author = canonicalize_project_key(author_project)
        ticket = await self._get_or_raise(ticket_id)
        try:
            act = TicketAction(action)
        except ValueError:
            valid = sorted(a.value for a in TicketAction)
            raise IllegalTransitionError(
                f"unknown action '{action}' — valid: {valid}"
            ) from None

        rule = TRANSITIONS.get((ticket.kind, ticket.status, act))
        if rule is None:
            allowed = allowed_actions(ticket.kind, ticket.status)
            hint = ", ".join(allowed) if allowed else "none (terminal state)"
            raise IllegalTransitionError(
                f"'{act.value}' is illegal from status '{ticket.status.value}' "
                f"(kind={ticket.kind.value}); allowed: {hint}"
            )
        role, new_status = rule
        expected = ticket.to_project if role == "executor" else ticket.from_project
        if author != expected:
            raise NotAllowedError(
                f"'{act.value}' is reserved to the {role} ('{expected}'); "
                f"author was '{author}'"
            )

        now = datetime.now(UTC)
        resolved_at = ticket.resolved_at
        extraction = ticket.extraction_status
        closed_at = ticket.closed_at
        if new_status is TicketStatus.RESOLVED:
            resolved_at = now
        if new_status is TicketStatus.OPEN:  # reopen
            resolved_at = None
        if new_status in TERMINAL_STATUSES:
            closed_at = now
            extraction = ExtractionStatus.PENDING

        updated = await self._repo.apply_transition(
            ticket_id,
            new_status,
            resolved_at=resolved_at,
            closed_at=closed_at,
            extraction_status=extraction,
        )
        if message:
            await self._repo.add_message(
                ticket_id, author, message, status_to=new_status
            )
        return updated

    async def get_with_thread(
        self, ticket_id: UUID
    ) -> tuple[Ticket, list[TicketMessage]] | None:
        ticket = await self._repo.get_by_id(ticket_id)
        if ticket is None:
            return None
        messages = await self._repo.get_messages(ticket_id)
        return ticket, messages

    async def list_grouped(self, project_key: str) -> TicketGroups:
        key = canonicalize_project_key(project_key, strict=False)
        return await self._repo.list_grouped(key)

    async def _get_or_raise(self, ticket_id: UUID) -> Ticket:
        ticket = await self._repo.get_by_id(ticket_id)
        if ticket is None:
            raise TicketNotFoundError(f"Ticket '{ticket_id}' not found")
        return ticket
```

- [ ] **Step 5: Vérifier vert unit**

Run: `pytest tests/unit/services/test_ticket_service.py tests/unit/models/ -q`
Expected: PASS

- [ ] **Step 6: Test d'intégration DB (round-trip complet, spec §8)** — `tests/integration/db/test_tickets_roundtrip.py`

Pattern de `tests/integration/db/test_migration_026.py` : `_run_alembic(["upgrade", "head"])` en module-setup, puis engine async sur `BRAIN_V42_TEST_DB_URL` (skip via `require_test_db_url()` du conftest racine). Contenu :

```python
"""Integration round-trip: create → reply → resolve → confirm → extraction_status.

Requires BRAIN_V42_TEST_DB_URL (skipped otherwise). Drives alembic upgrade head
so migration 028 is exercised end-to-end.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from brain_v42.models.ticket import (
    ExtractionStatus,
    TicketCreate,
    TicketKind,
    TicketStatus,
)
from brain_v42.repositories.pg_ticket import PgTicketRepo
from tests.conftest import require_test_db_url

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent

pytestmark = pytest.mark.asyncio


def _run_alembic_upgrade(db_url: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "POSTGRES_URL": db_url},
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade failed:\n{result.stderr}")


async def test_full_request_lifecycle_roundtrip():
    db_url = require_test_db_url()
    _run_alembic_upgrade(db_url)
    engine = create_async_engine(db_url)
    try:
        sf = async_sessionmaker(engine, expire_on_commit=False)
        repo = PgTicketRepo(sf)

        created = await repo.create(
            TicketCreate(
                kind=TicketKind.REQUEST,
                title="exposer /api/signals en ndjson",
                body="détail de la demande",
                from_project="red-shrik",
                to_project="red-data",
            )
        )
        assert created.status is TicketStatus.OPEN

        msg = await repo.add_message(created.id, "red-data", "ok je regarde")
        assert msg.ticket_id == created.id

        resolved = await repo.apply_transition(
            created.id,
            TicketStatus.RESOLVED,
            resolved_at=None,
            closed_at=None,
            extraction_status=None,
        )
        assert resolved.status is TicketStatus.RESOLVED

        from datetime import UTC, datetime

        closed = await repo.apply_transition(
            created.id,
            TicketStatus.CLOSED,
            resolved_at=resolved.resolved_at,
            closed_at=datetime.now(UTC),
            extraction_status=ExtractionStatus.PENDING,
        )
        assert closed.status is TicketStatus.CLOSED
        assert closed.extraction_status is ExtractionStatus.PENDING

        groups = await repo.list_grouped("red-data")
        assert all(t.id != created.id for t in groups.a_traiter)  # terminal → sorti

        thread = await repo.get_messages(created.id)
        assert len(thread) == 1
    finally:
        await engine.dispose()
```

Run: `pytest tests/integration/db/test_tickets_roundtrip.py -q` (PASS si `BRAIN_V42_TEST_DB_URL` défini, sinon SKIP — les deux acceptables ici).

- [ ] **Step 7: Vérifier vert + commit**

Run: `pytest tests/unit -q && ruff check src/ tests/ && ruff format --check src/ tests/ && mypy src/`

```bash
git add src/brain_v42/repositories/pg_ticket.py src/brain_v42/services/ticket_service.py tests/unit/services/test_ticket_service.py tests/integration/db/test_tickets_roundtrip.py
git commit -m "feat(tickets): repository + TicketService (machine à états, validation projets)"
```

---

### Task 3: 5 tools MCP + wiring server

**Files:**
- Create: `src/brain_v42/mcp/tools/ticket_tools.py`
- Modify: `src/brain_v42/mcp/server.py` — `build_services()` (ajout `ticket_svc` au dict) + bloc `__main__` (registration)
- Test: `tests/unit/mcp/test_ticket_tools.py`

**Interfaces:**
- Consumes: Task 2 (`TicketService`, exceptions), Task 1 (modèles), `format_confirmation`/`format_error`/`short_id` de `formatters.py`, `parse_uuid` de `parsing.py`
- Produces: `register_ticket_tools(mcp: Any, ticket_svc: TicketService) -> None` exposant `brain_ticket_create`, `brain_ticket_reply`, `brain_ticket_transition`, `brain_ticket_list`, `brain_ticket_get` (tous retournent `str`). `build_services()["ticket_svc"]`.

- [ ] **Step 1: Tests** — `tests/unit/mcp/test_ticket_tools.py`

```python
"""Unit tests for the 5 brain_ticket_* MCP tools (mocked service)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastmcp import FastMCP

from brain_v42.mcp.tools.ticket_tools import register_ticket_tools
from brain_v42.models.ticket import (
    Ticket,
    TicketGroups,
    TicketKind,
    TicketMessage,
    TicketStatus,
)
from brain_v42.services.ticket_service import IllegalTransitionError, UnknownProjectError

# asyncio_mode = "auto" — pas de pytestmark (style unit tests du repo).

FROM, TO = "red-shrik", "red-data"


def _ticket(**kw) -> Ticket:
    defaults = dict(
        kind=TicketKind.REQUEST, title="exposer ndjson", body="détail",
        from_project=FROM, to_project=TO,
    )
    defaults.update(kw)
    return Ticket(**defaults)


async def _tool(mcp, name):
    tool = await mcp.get_tool(name)
    assert tool is not None
    return tool


def _mcp_with(svc):
    mcp = FastMCP("test")
    register_ticket_tools(mcp, ticket_svc=svc)
    return mcp


class TestRegistration:
    async def test_all_five_tools_registered(self):
        mcp = _mcp_with(MagicMock())
        for name in (
            "brain_ticket_create",
            "brain_ticket_reply",
            "brain_ticket_transition",
            "brain_ticket_list",
            "brain_ticket_get",
        ):
            assert await mcp.get_tool(name) is not None


class TestCreate:
    async def test_create_ok(self):
        svc = MagicMock()
        svc.create = AsyncMock(return_value=_ticket())
        tool = await _tool(_mcp_with(svc), "brain_ticket_create")
        result = await tool.fn(
            from_project=FROM, to_project=TO, kind="request",
            title="exposer ndjson", body="détail",
        )
        assert result.startswith("ok ")
        assert "id:" in result

    async def test_create_invalid_kind(self):
        tool = await _tool(_mcp_with(MagicMock()), "brain_ticket_create")
        result = await tool.fn(
            from_project=FROM, to_project=TO, kind="bug",
            title="t", body="b",
        )
        assert result.startswith("✗")
        assert "request" in result and "fyi" in result

    async def test_create_unknown_project_returns_error_str(self):
        svc = MagicMock()
        svc.create = AsyncMock(side_effect=UnknownProjectError("Unknown project 'red-dataz'"))
        tool = await _tool(_mcp_with(svc), "brain_ticket_create")
        result = await tool.fn(
            from_project=FROM, to_project="red-dataz", kind="fyi",
            title="t", body="b",
        )
        assert result.startswith("✗")
        assert "red-dataz" in result

    async def test_create_malformed_project_key(self):
        tool = await _tool(_mcp_with(MagicMock()), "brain_ticket_create")
        result = await tool.fn(
            from_project="Red Shrik", to_project=TO, kind="request",
            title="t", body="b",
        )
        assert result.startswith("✗")


class TestTransition:
    async def test_transition_ok(self):
        svc = MagicMock()
        svc.transition = AsyncMock(return_value=_ticket(status=TicketStatus.RESOLVED))
        tool = await _tool(_mcp_with(svc), "brain_ticket_transition")
        result = await tool.fn(
            ticket_id=str(uuid4()), author_project=TO, action="resolve",
        )
        assert result.startswith("ok ")
        assert "resolved" in result

    async def test_transition_illegal_is_error_str(self):
        svc = MagicMock()
        svc.transition = AsyncMock(side_effect=IllegalTransitionError("'ack' is illegal"))
        tool = await _tool(_mcp_with(svc), "brain_ticket_transition")
        result = await tool.fn(
            ticket_id=str(uuid4()), author_project=TO, action="ack",
        )
        assert result.startswith("✗")

    async def test_transition_invalid_uuid(self):
        tool = await _tool(_mcp_with(MagicMock()), "brain_ticket_transition")
        result = await tool.fn(ticket_id="nope", author_project=TO, action="resolve")
        assert result.startswith("✗")
        assert "UUID" in result


class TestListAndGet:
    async def test_list_grouped_rendering(self):
        svc = MagicMock()
        svc.list_grouped = AsyncMock(
            return_value=TicketGroups(
                a_traiter=[_ticket()],
                a_confirmer=[_ticket(status=TicketStatus.RESOLVED)],
                en_attente=[],
            )
        )
        tool = await _tool(_mcp_with(svc), "brain_ticket_list")
        result = await tool.fn(project_key=TO)
        assert "À traiter (1)" in result
        assert "À confirmer (1)" in result

    async def test_list_empty(self):
        svc = MagicMock()
        svc.list_grouped = AsyncMock(return_value=TicketGroups())
        tool = await _tool(_mcp_with(svc), "brain_ticket_list")
        result = await tool.fn(project_key=TO)
        assert "aucun ticket" in result

    async def test_get_renders_thread_and_allowed_actions(self):
        t = _ticket()
        svc = MagicMock()
        svc.get_with_thread = AsyncMock(
            return_value=(
                t,
                [
                    TicketMessage(
                        ticket_id=t.id, author_project=TO, body="je regarde",
                        created_at=datetime.now(UTC),
                    )
                ],
            )
        )
        tool = await _tool(_mcp_with(svc), "brain_ticket_get")
        result = await tool.fn(ticket_id=str(t.id))
        assert "je regarde" in result
        assert "resolve" in result  # actions possibles depuis open/request

    async def test_get_not_found(self):
        svc = MagicMock()
        svc.get_with_thread = AsyncMock(return_value=None)
        tool = await _tool(_mcp_with(svc), "brain_ticket_get")
        result = await tool.fn(ticket_id=str(uuid4()))
        assert result.startswith("✗")
```

- [ ] **Step 2: Vérifier l'échec**

Run: `pytest tests/unit/mcp/test_ticket_tools.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'brain_v42.mcp.tools.ticket_tools'`

- [ ] **Step 3: Implémenter `src/brain_v42/mcp/tools/ticket_tools.py`**

```python
"""MCP tools for cross-project tickets: brain_ticket_create / reply /
transition / list / get.

Coordination family — addressed, transient, stateful (spec 2026-07-04).
Formatting stays local (single consumer); shared write-confirmations come
from formatters.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import ValidationError

from brain_v42.mcp.tools.formatters import format_confirmation, format_error, short_id
from brain_v42.mcp.tools.parsing import parse_uuid
from brain_v42.models.ticket import (
    Ticket,
    TicketCreate,
    TicketGroups,
    TicketKind,
    TicketMessage,
    allowed_actions,
)
from brain_v42.services.ticket_service import TicketError

if TYPE_CHECKING:
    from brain_v42.services.ticket_service import TicketService

logger = structlog.get_logger(__name__)

_VALID_KINDS = ("fyi", "request")
_LIST_CAP = 10


def _age_days(dt: datetime) -> str:
    days = max(0, (datetime.now(UTC) - dt).days)
    return f"{days}j"


def _ticket_line(t: Ticket, *, direction: str) -> str:
    # direction: "in" (je suis destinataire) / "out" (je suis émetteur)
    arrow = "⬅️" if direction == "in" else "➡️"
    peer = t.from_project if direction == "in" else t.to_project
    prep = "de" if direction == "in" else "vers"
    return (
        f"{arrow} #{short_id(str(t.id))} [{t.kind.value}] {prep} {peer} : "
        f"« {t.title} » ({t.status.value} · {_age_days(t.created_at)})"
    )


def _format_groups(groups: TicketGroups, project_key: str) -> str:
    total = len(groups.a_traiter) + len(groups.a_confirmer) + len(groups.en_attente)
    if total == 0:
        return f"## Tickets — {project_key}\n(aucun ticket)"
    lines = [f"## Tickets — {project_key}"]
    if groups.a_traiter:
        lines.append(f"\n### À traiter ({len(groups.a_traiter)})")
        lines += [_ticket_line(t, direction="in") for t in groups.a_traiter[:_LIST_CAP]]
    if groups.a_confirmer:
        lines.append(f"\n### À confirmer ({len(groups.a_confirmer)})")
        lines += [
            _ticket_line(t, direction="out") for t in groups.a_confirmer[:_LIST_CAP]
        ]
    if groups.en_attente:
        lines.append(f"\n### En attente de l'autre côté ({len(groups.en_attente)})")
        lines += [
            _ticket_line(t, direction="out") for t in groups.en_attente[:_LIST_CAP]
        ]
    return "\n".join(lines)


def _format_thread(ticket: Ticket, messages: list[TicketMessage]) -> str:
    header = (
        f"## Ticket #{short_id(str(ticket.id))} [{ticket.kind.value}] — « {ticket.title} »\n"
        f"{ticket.from_project} → {ticket.to_project} · status: {ticket.status.value}"
        f" · créé {ticket.created_at.date().isoformat()}"
    )
    if ticket.extraction_status is not None:
        header += f" · extraction: {ticket.extraction_status.value}"
    parts = [header, ticket.body]
    if messages:
        parts.append(f"### Fil ({len(messages)} message{'s' if len(messages) > 1 else ''})")
        for i, m in enumerate(messages, 1):
            suffix = f" (→ {m.status_to.value})" if m.status_to else ""
            parts.append(
                f"{i}. [{m.created_at.date().isoformat()}] {m.author_project}: "
                f"{m.body}{suffix}"
            )
    actions = allowed_actions(ticket.kind, ticket.status)
    if actions:
        parts.append(
            f"Actions possibles ({', '.join(actions)}) via brain_ticket_transition"
        )
    return "\n\n".join(parts)


def register_ticket_tools(
    mcp: Any,
    ticket_svc: TicketService,
) -> None:
    """Register the 5 brain_ticket_* MCP tools on the FastMCP server."""

    @mcp.tool(version="1.0")
    async def brain_ticket_create(
        from_project: str,
        to_project: str,
        kind: str,
        title: str,
        body: str,
    ) -> str:
        """Open a cross-project ticket addressed to another project.

        kind='request': ask the target project to do something — full loop
        (they resolve, you confirm). kind='fyi': heads-up needing only an
        ack (e.g. contract change). The target sees it at its next
        brain_session_start. Both project keys must already exist.
        """
        if kind not in _VALID_KINDS:
            return format_error(f"Invalid kind '{kind}'. Valid: {list(_VALID_KINDS)}")
        try:
            data = TicketCreate(
                kind=TicketKind(kind),
                title=title,
                body=body,
                from_project=from_project,
                to_project=to_project,
            )
            ticket = await ticket_svc.create(data)
        except (TicketError, ValidationError) as exc:
            return format_error(str(exc))
        logger.info(
            "mcp.brain_ticket_create",
            ticket_id=str(ticket.id),
            kind=kind,
            to_project=ticket.to_project,
        )
        return format_confirmation(
            "Ticket created",
            title,
            id=str(ticket.id),
            kind=kind,
            to=ticket.to_project,
        )

    @mcp.tool(version="1.0")
    async def brain_ticket_reply(
        ticket_id: str,
        author_project: str,
        body: str,
    ) -> str:
        """Post a message in a ticket thread (any status, participants only)."""
        tid = parse_uuid(ticket_id)
        if tid is None:
            return format_error(f"Invalid UUID: {ticket_id}")
        try:
            await ticket_svc.reply(tid, author_project, body)
        except TicketError as exc:
            return format_error(str(exc))
        except ValueError as exc:  # canonicalize_project_key strict
            return format_error(str(exc))
        return format_confirmation("Reply posted", body, id=short_id(ticket_id))

    @mcp.tool(version="1.0")
    async def brain_ticket_transition(
        ticket_id: str,
        author_project: str,
        action: str,
        message: str | None = None,
    ) -> str:
        """Change a ticket's status. Actions — executor (to_project): start,
        resolve, wontfix, ack (fyi). Requester (from_project): confirm,
        reopen, cancel. Optional message is appended to the thread.
        """
        tid = parse_uuid(ticket_id)
        if tid is None:
            return format_error(f"Invalid UUID: {ticket_id}")
        try:
            updated = await ticket_svc.transition(
                tid, author_project, action, message=message
            )
        except TicketError as exc:
            return format_error(str(exc))
        except ValueError as exc:
            return format_error(str(exc))
        logger.info(
            "mcp.brain_ticket_transition",
            ticket_id=ticket_id,
            action=action,
            new_status=updated.status.value,
        )
        return format_confirmation(
            "Ticket updated",
            updated.title,
            id=short_id(ticket_id),
            status=updated.status.value,
        )

    @mcp.tool(version="1.0")
    async def brain_ticket_list(project_key: str) -> str:
        """List a project's tickets grouped by needed action: à traiter
        (I'm the target), à confirmer (my requests resolved/wontfixed,
        awaiting my confirmation), en attente (the other side must act).
        """
        try:
            groups = await ticket_svc.list_grouped(project_key)
        except TicketError as exc:
            return format_error(str(exc))
        return _format_groups(groups, project_key)

    @mcp.tool(version="1.0")
    async def brain_ticket_get(ticket_id: str) -> str:
        """Full ticket view: header, body, thread, allowed actions."""
        tid = parse_uuid(ticket_id)
        if tid is None:
            return format_error(f"Invalid UUID: {ticket_id}")
        result = await ticket_svc.get_with_thread(tid)
        if result is None:
            return format_error(f"Ticket '{short_id(ticket_id)}' not found")
        ticket, messages = result
        return _format_thread(ticket, messages)
```

- [ ] **Step 4: Wiring `src/brain_v42/mcp/server.py`**

Dans `build_services()` (après la construction de `project_context_svc`, vers la fin, avant le `return`) :

```python
    # Tickets (coordination family — spec 2026-07-04)
    from brain_v42.repositories.pg_ticket import PgTicketRepo  # noqa: PLC0415
    from brain_v42.services.ticket_service import TicketService  # noqa: PLC0415

    ticket_repo = PgTicketRepo(session_factory)
    ticket_svc = TicketService(
        repo=ticket_repo,
        project_context_repo=project_context_repo,
    )
```

et ajouter `"ticket_svc": ticket_svc,` au dict retourné.
> Si les imports du haut de fichier sont le style dominant pour les repos/services dans `build_services()`, mettre ces imports en tête de module comme les autres (suivre le style constaté — les `# noqa: PLC0415` ne sont utilisés que dans le bloc `__main__`).

Dans le bloc `__main__`, après la registration des dream tools :

```python
    # Ticket tools (coordination cross-projet)
    from brain_v42.mcp.tools.ticket_tools import register_ticket_tools  # noqa: PLC0415

    register_ticket_tools(mcp, ticket_svc=services["ticket_svc"])
```

- [ ] **Step 5: Vérifier vert + commit**

Run: `pytest tests/unit -q && ruff check src/ tests/ && ruff format --check src/ tests/ && mypy src/`

```bash
git add src/brain_v42/mcp/tools/ticket_tools.py src/brain_v42/mcp/server.py tests/unit/mcp/test_ticket_tools.py
git commit -m "feat(tickets): 5 tools MCP brain_ticket_* + wiring server"
```

---

### Task 4: Section briefing `### Tickets` dans brain_session_start

**Files:**
- Modify: `src/brain_v42/mcp/tools/session_tools.py`
- Modify: `src/brain_v42/mcp/server.py` (passer `ticket_svc` à `register_session_tools`)
- Test: `tests/unit/mcp/test_session_tools.py` (ajouter une classe de tests)

**Interfaces:**
- Consumes: Task 2 (`TicketService.list_grouped`), Task 1 (`TicketGroups`)
- Produces: `register_session_tools(..., ticket_svc: Any | None = None)` (keyword, défaut None — rétro-compatible avec tous les appels existants) ; `_section_tickets(groups: Any | None) -> str`

- [ ] **Step 1: Tests (ajouter à `tests/unit/mcp/test_session_tools.py`)**

```python
class TestTicketsSection:
    """Section ### Tickets — actionnable, haute, graceful-degrade (spec §5)."""

    def _services(self):
        ctx = MagicMock(project_key="p", current_focus="f", description="d", blockers=[])
        svcs = []
        for ret in (ctx, [], [], _KILLSWITCHES_OK, None, [], []):
            m = MagicMock()
            svcs.append(m)
        mock_ctx_svc = MagicMock()
        mock_ctx_svc.get_by_key = AsyncMock(return_value=ctx)
        mock_decision_svc = MagicMock()
        mock_decision_svc.list_all = AsyncMock(return_value=[])
        mock_learning_svc = MagicMock()
        mock_learning_svc.list_all = AsyncMock(return_value=[])
        mock_dream_svc = MagicMock()
        mock_dream_svc.killswitch_state = AsyncMock(return_value=_KILLSWITCHES_OK)
        mock_dream_svc.last_failure = AsyncMock(return_value=None)
        mock_feature_svc = MagicMock()
        mock_feature_svc.in_flight = AsyncMock(return_value=[])
        mock_feature_svc.stale_pinned = AsyncMock(return_value=[])
        return (mock_ctx_svc, mock_decision_svc, mock_learning_svc,
                mock_dream_svc, mock_feature_svc)

    @pytest.mark.asyncio
    async def test_briefing_shows_tickets_section(self):
        from brain_v42.models.ticket import Ticket, TicketKind, TicketStatus, TicketGroups

        groups = TicketGroups(
            a_traiter=[Ticket(
                kind=TicketKind.REQUEST, title="exposer ndjson", body="b",
                from_project="red-shrik", to_project="p",
            )],
            a_confirmer=[Ticket(
                kind=TicketKind.REQUEST, title="autre", body="b",
                from_project="p", to_project="red-data",
                status=TicketStatus.RESOLVED,
            )],
        )
        ticket_svc = MagicMock()
        ticket_svc.list_grouped = AsyncMock(return_value=groups)
        mcp = FastMCP("test")
        register_session_tools(
            mcp, *self._services(), ticket_svc=ticket_svc,
        )
        tool = await mcp.get_tool("brain_session_start")
        result = await tool.fn(project_key="p")
        assert "### Tickets (1 à traiter · 1 à confirmer)" in result
        assert "exposer ndjson" in result
        assert "vérifie et confirme" in result

    @pytest.mark.asyncio
    async def test_no_tickets_no_section(self):
        from brain_v42.models.ticket import TicketGroups

        ticket_svc = MagicMock()
        ticket_svc.list_grouped = AsyncMock(return_value=TicketGroups())
        mcp = FastMCP("test")
        register_session_tools(mcp, *self._services(), ticket_svc=ticket_svc)
        tool = await mcp.get_tool("brain_session_start")
        result = await tool.fn(project_key="p")
        assert "### Tickets" not in result

    @pytest.mark.asyncio
    async def test_ticket_service_failure_degrades_gracefully(self):
        ticket_svc = MagicMock()
        ticket_svc.list_grouped = AsyncMock(side_effect=RuntimeError("db down"))
        mcp = FastMCP("test")
        register_session_tools(mcp, *self._services(), ticket_svc=ticket_svc)
        tool = await mcp.get_tool("brain_session_start")
        result = await tool.fn(project_key="p")  # MUST NOT raise
        assert "### Killswitches" in result
        assert "### Tickets" not in result

    @pytest.mark.asyncio
    async def test_no_ticket_svc_backward_compatible(self):
        mcp = FastMCP("test")
        register_session_tools(mcp, *self._services())
        tool = await mcp.get_tool("brain_session_start")
        result = await tool.fn(project_key="p")
        assert "### Tickets" not in result
```

> Adapter `_KILLSWITCHES_OK` / la construction des mocks au style réel du fichier de test existant (réutiliser les helpers/fixtures déjà présents plutôt que dupliquer — ce squelette montre le comportement attendu, pas la lettre).
> **Style local assumé** : `test_session_tools.py` utilise déjà `@pytest.mark.asyncio` sur ses méthodes (lignes 305+). Bien que `asyncio_mode="auto"` les rende inertes, **garder les décorateurs ici** pour matcher le fichier environnant — c'est le seul fichier de test du plan où on les conserve (partout ailleurs en unit : `async def` nus).

- [ ] **Step 2: Vérifier l'échec** — `pytest tests/unit/mcp/test_session_tools.py -q` → FAIL (`register_session_tools` ne connaît pas `ticket_svc`)

- [ ] **Step 3: Implémenter dans `session_tools.py`**

1. Signature : ajouter `ticket_svc: Any | None = None` (keyword-only, après `cross_project_svc`).
2. Helper de section (placer près des autres `_section_*`) :

```python
_TICKETS_CAP = 5


def _section_tickets(groups: Any | None) -> str:
    """### Tickets — actionnable en tête de briefing (spec tickets §5).

    N'affiche que l'actionnable : à traiter (je suis destinataire) et
    à confirmer (mes requests resolved/wontfix). Cap _TICKETS_CAP au total.
    """
    if groups is None:
        return ""
    a_traiter = list(groups.a_traiter)
    a_confirmer = list(groups.a_confirmer)
    if not a_traiter and not a_confirmer:
        return ""
    lines = [f"### Tickets ({len(a_traiter)} à traiter · {len(a_confirmer)} à confirmer)"]
    budget = _TICKETS_CAP
    for t in a_traiter[:budget]:
        age = max(0, (datetime.now(UTC) - t.created_at).days)
        suffix = "— à ack" if t.kind.value == "fyi" else f"({t.status.value} · {age}j)"
        lines.append(
            f"⬅️ #{short_id(str(t.id))} [{t.kind.value}] de {t.from_project} : "
            f"« {t.title} » {suffix}"
        )
    budget -= len(a_traiter[:budget])
    for t in a_confirmer[:budget]:
        lines.append(
            f"➡️ #{short_id(str(t.id))} vers {t.to_project} : « {t.title} » — "
            f"{t.status.value}, vérifie et confirme"
        )
    if len(a_traiter) + len(a_confirmer) > _TICKETS_CAP:
        lines.append("→ brain_ticket_list pour le reste")
    return "\n".join(lines)
```

(Imports à compléter en tête de fichier s'ils n'y sont pas déjà : `from datetime import UTC, datetime` et `from brain_v42.mcp.tools.formatters import short_id` — `short_id` est exporté par `formatters.py:30`.)

3. Dans `brain_session_start`, après le bloc `cross_block` (même pattern env-free, guarded) :

```python
        ticket_groups = None
        if ticket_svc is not None:
            try:
                ticket_groups = await ticket_svc.list_grouped(project_key)
            except Exception as exc:
                logger.warning("brain_session_start_tickets_failed", error=str(exc))
```

4. `_format_session_briefing` : paramètre keyword `ticket_groups: Any | None = None`, et insérer `_section_tickets(ticket_groups)` dans `sections` **entre** `_section_last_failure(...)` et `_section_in_flight(...)` (section haute, spec §5).

5. `server.py` : ajouter `ticket_svc=services["ticket_svc"],` à l'appel `register_session_tools(...)`.

- [ ] **Step 4: Vérifier vert + commit**

Run: `pytest tests/unit/mcp/ -q && ruff check src/ tests/ && ruff format --check src/ tests/ && mypy src/`

```bash
git add src/brain_v42/mcp/tools/session_tools.py src/brain_v42/mcp/server.py tests/unit/mcp/test_session_tools.py
git commit -m "feat(tickets): section Tickets dans le briefing brain_session_start"
```

---

### Task 5: Extraction proposer-only — migration 029 + `scripts/ticket_extract.py`

**Files:**
- Modify: `src/brain_v42/db/tables.py` (table `ticket_extraction_proposals`)
- Create: `alembic/versions/029_ticket_extraction_proposals.py`
- Create: `scripts/ticket_extract.py`
- Test: `tests/unit/test_ticket_extract.py`

**Interfaces:**
- Consumes: `scripts/domain_backfill.py` — réutiliser `load_env_file`, `_post_chat`, `_strip_fences`, `ResponseParseError`, `DEFAULT_BASE_URL`, `DEFAULT_MODEL` (vérifier les noms exacts dans le fichier ; si un helper n'existe pas sous ce nom, copier son équivalent ≤15 lignes en local plutôt que refactorer domain_backfill). `LearningService.create(data: LearningCreate) -> Learning`, `DecisionService.create(data: DecisionCreate) -> Decision`, `GPUEmbeddingService(base_url=...)`, `get_session_factory`, `get_settings`.
- Produces: CLI `python -m scripts.ticket_extract [--limit N] [--wet] [--apply-ids "1,2"]` ; fonctions pures `build_messages(thread)`, `parse_and_validate(content, thread)`, `render_thread(thread)` ; table `ticket_extraction_proposals`.

**Comportement (spec §6):**
- **propose** (défaut, dry) : scanne `tickets.extraction_status='pending'` (states terminaux), 1 appel LLM par ticket (fil complet), propose 0..n entités {learning|decision} avec `target_project ∈ {from, to}` + rationale → INSERT `ticket_extraction_proposals` (status `proposed`) + ticket → `proposed` (ou `skipped` si 0). Récapitulatif imprimé sur stdout (ids des proposals pour la review).
- **--wet** : propose puis applique **uniquement les proposals créées par ce run** (jamais les anciennes en attente de review humaine).
- **--apply-ids "1,2"** : pas de LLM ; applique des proposals `proposed` reviewées à la main. Incompatible avec `--wet`.
- **apply** : learning → `LearningCreate(topic, insight, tags, project_key=target_project, source=f"ticket:{ticket_id}", source_type="automated", confidence="medium")` (leçon 6dfb9064 : confidence jamais reprise du LLM) ; decision → `DecisionCreate(title, description, reasoning, tags, project_key=target_project, metadata={"source": f"ticket:{ticket_id}", "source_type": "automated"})`. Proposal → `applied` + `applied_entity_id` + `applied_at` ; quand un ticket n'a plus aucune proposal `proposed`, ticket → `done`.
- Embeddings best-effort : `GPUEmbeddingService` sondé par un `embed("ping")` au démarrage de l'apply ; en échec → services construits avec `embedding_svc=None` (entités créées sans vecteur ; FTS/`search_vector` reste fonctionnel, hint stdout vers `scripts.regen_embeddings`). Graph : services construits avec `graph=None` (le script ne parle pas à Neo4j ; hint stdout vers `scripts.reconcile_graph`).
- Fin de run : INSERT `dream_runs` (`phase='extract'`, `status='done'|'fail'`, `run_date=date.today()`, `phase_dry_run = not wet_mode`, `duration_s`, `error_message`) — best-effort try/except pour que l'échec du reporting ne masque pas le résultat.

- [ ] **Step 1: Migration + table.** `alembic/versions/029_ticket_extraction_proposals.py` (head `028`) :

```python
"""Proposals d'extraction de connaissance depuis les tickets terminaux.

Pattern PROMOTE (dream_promotions) : table d'audit proposer-only, review
humaine → apply. Spec §6.

Revision ID: 029
Revises: 028
"""

from alembic import op

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ticket_extraction_proposals (
            id BIGSERIAL PRIMARY KEY,
            ticket_id UUID REFERENCES tickets(id) ON DELETE SET NULL,
            target_type VARCHAR(10) NOT NULL,
            target_project VARCHAR(50) NOT NULL,
            payload JSONB NOT NULL,
            rationale TEXT,
            status VARCHAR(10) NOT NULL DEFAULT 'proposed',
            applied_entity_id UUID,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            applied_at TIMESTAMPTZ,

            CONSTRAINT tep_target_type_valid CHECK (target_type IN ('learning', 'decision')),
            CONSTRAINT tep_status_valid CHECK (status IN ('proposed', 'applied', 'rejected'))
        )
        """
    )
    op.execute("CREATE INDEX idx_tep_status ON ticket_extraction_proposals (status)")
    op.execute("CREATE INDEX idx_tep_ticket ON ticket_extraction_proposals (ticket_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ticket_extraction_proposals;")
```

Et la table Core correspondante dans `db/tables.py` (mêmes colonnes, `sa.BigInteger` PK autoincrement, FKs `ondelete="SET NULL"`, mêmes CHECK/Index — copier le style de `dream_promotions`).

- [ ] **Step 2: Tests des fonctions pures** — `tests/unit/test_ticket_extract.py`

```python
"""Unit tests for scripts.ticket_extract pure functions (no DB, no network)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from scripts.ticket_extract import (
    ProposalDraft,
    ResponseParseError,
    TicketThread,
    build_messages,
    parse_and_validate,
    render_thread,
)


def _thread(**kw) -> TicketThread:
    defaults = dict(
        id=uuid4(),
        kind="request",
        title="pourquoi camelCase",
        body="le endpoint /api/signals renvoie du camelCase ?",
        from_project="red-shrik",
        to_project="red-data",
        status="closed",
        messages=[
            ("red-data", "c'est le middleware de sérialisation, voulu", "resolved",
             datetime(2026, 7, 3, tzinfo=UTC)),
        ],
    )
    defaults.update(kw)
    return TicketThread(**defaults)


class TestRenderAndBuild:
    def test_render_thread_contains_all_parts(self):
        text = render_thread(_thread())
        assert "red-shrik" in text and "red-data" in text
        assert "camelCase" in text
        assert "middleware" in text

    def test_build_messages_has_system_and_user(self):
        msgs = build_messages(_thread())
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "JSON" in msgs[0]["content"]


class TestParseAndValidate:
    def test_valid_learning_proposal(self):
        content = (
            '[{"target_type": "learning", "target_project": "red-shrik", '
            '"payload": {"topic": "red-data camelCase", '
            '"insight": "le middleware sérialise en camelCase, contrat voulu", '
            '"tags": ["api"]}, "rationale": "contrat durable"}]'
        )
        drafts = parse_and_validate(content, _thread())
        assert len(drafts) == 1
        assert drafts[0].target_type == "learning"
        assert drafts[0].target_project == "red-shrik"

    def test_empty_array_is_valid_zero_proposals(self):
        assert parse_and_validate("[]", _thread()) == []

    def test_markdown_fences_stripped(self):
        content = '```json\n[]\n```'
        assert parse_and_validate(content, _thread()) == []

    def test_invalid_json_raises(self):
        with pytest.raises(ResponseParseError):
            parse_and_validate("pas du json", _thread())

    def test_unknown_target_type_rejected(self):
        content = (
            '[{"target_type": "runbook", "target_project": "red-shrik", '
            '"payload": {}, "rationale": "x"}]'
        )
        with pytest.raises(ResponseParseError, match="target_type"):
            parse_and_validate(content, _thread())

    def test_target_project_must_be_participant(self):
        content = (
            '[{"target_type": "learning", "target_project": "red-lab", '
            '"payload": {"topic": "t", "insight": "i", "tags": []}, '
            '"rationale": "x"}]'
        )
        with pytest.raises(ResponseParseError, match="target_project"):
            parse_and_validate(content, _thread())

    def test_missing_payload_keys_rejected(self):
        content = (
            '[{"target_type": "decision", "target_project": "red-data", '
            '"payload": {"title": "t"}, "rationale": "x"}]'
        )
        with pytest.raises(ResponseParseError, match="payload"):
            parse_and_validate(content, _thread())

    def test_overlong_topic_truncated_to_200(self):
        content = (
            '[{"target_type": "learning", "target_project": "red-data", '
            '"payload": {"topic": "' + "x" * 300 + '", "insight": "i", "tags": []}, '
            '"rationale": "r"}]'
        )
        drafts = parse_and_validate(content, _thread())
        assert len(drafts[0].payload["topic"]) == 200
```

- [ ] **Step 3: Vérifier l'échec** — `pytest tests/unit/test_ticket_extract.py -q` → FAIL (module absent)

- [ ] **Step 4: Implémenter `scripts/ticket_extract.py`**

Squelette complet (le corps LLM/retry reprend le pattern exact de `classify_batch` dans `domain_backfill.py` — corrective re-prompt une fois, échec → outcome failed) :

```python
"""Ticket knowledge extraction — proposer-only dream step (spec §6).

Scanne les tickets en état terminal (extraction_status='pending'), envoie
chaque fil au LLM (NVIDIA API, JSON strict SANS tools — pattern validé du
domain backfill), stocke des proposals reviewables, applique en wet.

Usage:
    python -m scripts.ticket_extract [--limit 20]          # propose (dry)
    python -m scripts.ticket_extract --limit 20 --wet      # propose + apply du run
    python -m scripts.ticket_extract --apply-ids "3,4"     # apply reviewé, sans LLM
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import sqlalchemy as sa

from scripts.domain_backfill import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ResponseParseError,
    _post_chat,
    _strip_fences,
    load_env_file,
)

_ENV_FILE = Path.home() / ".config" / "brain-v42" / "nvidia.env"
_API_KEY_VAR = "BRAIN_NVIDIA_API_KEY"

_VALID_TARGET_TYPES = ("learning", "decision")
_PAYLOAD_KEYS = {
    "learning": ("topic", "insight", "tags"),
    "decision": ("title", "description", "reasoning", "tags"),
}

_SYSTEM_PROMPT = (
    "Tu extrais de la connaissance durable depuis des tickets de coordination "
    "résolus entre projets d'un même écosystème. Tu réponds UNIQUEMENT avec un "
    "tableau JSON valide (éventuellement vide []) — pas de prose, pas de "
    "markdown. Chaque élément: {\"target_type\": \"learning\"|\"decision\", "
    "\"target_project\": \"<un des deux projets du ticket>\", \"payload\": "
    "{...}, \"rationale\": \"pourquoi c'est durable\"}. "
    "payload learning: {\"topic\": str<=200, \"insight\": str, \"tags\": [str]}. "
    "payload decision: {\"title\": str<=200, \"description\": str, "
    "\"reasoning\": str, \"tags\": [str]}. "
    "N'extrais QUE les insights durables/réutilisables (gotchas, contrats "
    "d'API, choix argumentés). Un simple « fait/déployé/ok merci » → []."
)
_REPROMPT_INSTRUCTION = (
    "Ta réponse précédente n'était pas un tableau JSON valide selon le format "
    "demandé. Renvoie UNIQUEMENT le tableau JSON corrigé."
)


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
            raise ResponseParseError(
                f"item {i}: payload must contain {required}"
            )
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
```

Puis (dans le même fichier) les parties I/O — suivre le style async + `session_factory` de `domain_backfill.py` :

- `async def fetch_pending_threads(session_factory, limit) -> list[TicketThread]` — SELECT `tickets` WHERE `extraction_status='pending'` ORDER BY `closed_at ASC` LIMIT n, puis SELECT des messages par ticket (`ORDER BY created_at`).
- `async def extract_thread(client, model, thread, sleep=asyncio.sleep)` — `_post_chat` + `parse_and_validate` ; sur `ResponseParseError`, un re-prompt correctif (`_REPROMPT_INSTRUCTION`) puis re-parse ; échec final → outcome `failed=True` (le ticket RESTE `pending`, retenté la nuit suivante).
- `async def persist_proposals(session_factory, thread, drafts) -> list[int]` — dans une transaction : INSERT chaque draft dans `ticket_extraction_proposals` (RETURNING id) ; UPDATE ticket `extraction_status = 'proposed' if drafts else 'skipped'`.
- `async def apply_proposals(session_factory, proposal_ids) -> tuple[int, int]` — construit les services une fois :

```python
async def _build_apply_services() -> tuple[Any, Any]:
    from brain_v42.config import get_settings
    from brain_v42.repositories.pg_decision import PgDecisionRepo
    from brain_v42.repositories.pg_learning import PgLearningRepo
    from brain_v42.services.decision_service import DecisionService
    from brain_v42.services.embedding_service import GPUEmbeddingService
    from brain_v42.services.learning_service import LearningService
    from brain_v42.db.session import get_session_factory

    sf = get_session_factory()
    embedding_svc: Any | None = GPUEmbeddingService(
        base_url=get_settings().embedding_service_url
    )
    try:
        await embedding_svc.embed("ping")
    except Exception:
        print("! embedding service unreachable — creating entities without vectors "
              "(run scripts.regen_embeddings later)")
        embedding_svc = None
    learning_svc = LearningService(pg_repo=PgLearningRepo(sf), embedding_svc=embedding_svc)
    decision_svc = DecisionService(repo=PgDecisionRepo(sf), embedding_svc=embedding_svc)
    return learning_svc, decision_svc
```

> Vérifier les chemins d'import réels (`brain_v42.config.get_settings`, `brain_v42.db.session.get_session_factory`, module de `GPUEmbeddingService`) en lisant `server.py` — reprendre exactement ses imports. Si `DecisionService.__init__` prend `repo=` vs `pg_repo=`, matcher la vraie signature (cf. `build_services()`).

  puis pour chaque proposal `proposed` : create l'entité (mapping du **Comportement** ci-dessus), UPDATE proposal → `applied`, `applied_entity_id`, `applied_at=NOW()` ; enfin pour chaque ticket touché sans proposal `proposed` restante → `extraction_status='done'`. Print un hint `scripts.reconcile_graph` si ≥1 entité créée (graph=None dans ce contexte).
- `async def record_dream_run(status: str, dry: bool, duration_s: float, error: str | None)` — INSERT `dream_runs` via `sa.text(...)`, best-effort (`try/except` + print warning).
- `def main() -> int` — argparse (`--limit` défaut 20 avec garde ≥1, `--wet` flag, `--apply-ids` str, `--model`, `--base-url` ; refuser `--wet` + `--apply-ids` ensemble) ; charge `_ENV_FILE` via `load_env_file` ; clé absente → exit 2 avec message. Orchestration : propose → (si `--wet`) apply des ids du run → récap stdout (`N tickets scannés, P proposals, S skipped, F failed`) → `record_dream_run` → exit 0 (ou 1 si ≥1 batch failed).

- [ ] **Step 5: Vérifier vert + commit**

Run: `pytest tests/unit -q && ruff check src/ tests/ scripts/ && ruff format --check src/ tests/ scripts/ && mypy src/`

```bash
git add src/brain_v42/db/tables.py alembic/versions/029_ticket_extraction_proposals.py scripts/ticket_extract.py tests/unit/test_ticket_extract.py
git commit -m "feat(tickets): extraction proposer-only — migration 029 + scripts/ticket_extract.py"
```

---

### Task 6: dream.sh + killswitch EXTRACT + affichage briefing

**Files:**
- Modify: `scripts/dream.sh` (env defaults vers la ligne 33 ; step extract APRÈS la boucle `for phase_spec` — repérer le `done` de la boucle, insérer avant le bloc de résumé final)
- Modify: `src/brain_v42/services/dream_run_service.py` (`KillswitchState` + `killswitch_state()`)
- Modify: `src/brain_v42/mcp/tools/session_tools.py` (`_section_killswitches` : ligne EXTRACT)
- Test: `tests/unit/test_dream_sh_extract.py` (pins grep-style, pattern de `test_dream_sh_phase_timeouts.py`)
- Test: étendre `tests/unit/mcp/test_session_tools.py` (ligne EXTRACT) et les tests existants de `killswitch_state` s'il y en a (chercher `killswitch_state` dans `tests/`)

**Interfaces:**
- Consumes: Task 5 (CLI `scripts.ticket_extract`), conventions dream.sh (`log()`, `LOG_DIR`, `TIMESTAMP`, `FAILED_PHASES`, `uv run python -m`)
- Produces: env killswitches `BRAIN_DREAM_EXTRACT_ENABLED` (défaut `false`) / `BRAIN_DREAM_EXTRACT_DRY_RUN` (défaut `true`) ; `KillswitchState.extract_enabled: bool = False`, `extract_dry: bool = True`, `extract_clean_dry_nights: int = 0`

- [ ] **Step 1: Test pins dream.sh** — `tests/unit/test_dream_sh_extract.py`

```python
"""Pin the EXTRACT killswitch wiring in dream.sh (grep-style, no execution)."""

from pathlib import Path

_DREAM_SH = Path(__file__).parent.parent.parent / "scripts" / "dream.sh"


def _content() -> str:
    return _DREAM_SH.read_text(encoding="utf-8")


def test_extract_killswitch_defaults_closed_and_dry():
    content = _content()
    assert 'BRAIN_DREAM_EXTRACT_ENABLED="${BRAIN_DREAM_EXTRACT_ENABLED:-false}"' in content
    assert 'BRAIN_DREAM_EXTRACT_DRY_RUN="${BRAIN_DREAM_EXTRACT_DRY_RUN:-true}"' in content


def test_extract_step_invokes_cli_module():
    content = _content()
    assert "scripts.ticket_extract" in content
    assert "SKIP extract (killswitch" in content


def test_extract_wet_flag_only_when_dry_run_false():
    content = _content()
    # Le flag --wet doit être conditionné au sous-flag DRY_RUN, jamais inconditionnel.
    assert 'if [[ "$BRAIN_DREAM_EXTRACT_DRY_RUN" != "true" ]]' in content
```

- [ ] **Step 2: Vérifier l'échec** — `pytest tests/unit/test_dream_sh_extract.py -q` → FAIL

- [ ] **Step 3: Modifier `scripts/dream.sh`**

Vers la ligne 33 (après `BRAIN_DREAM_REORG_DRY_RUN=...`) :

```bash
# EXTRACT killswitch — ticket knowledge extraction (proposer-only, spec
# 2026-07-04). Ship CLOSED; once enabled it starts in DRY (propose-only,
# review humaine via ticket_extraction_proposals) — même trajectoire de
# soak que REORG avant tout flip WET.
BRAIN_DREAM_EXTRACT_ENABLED="${BRAIN_DREAM_EXTRACT_ENABLED:-false}"
BRAIN_DREAM_EXTRACT_DRY_RUN="${BRAIN_DREAM_EXTRACT_DRY_RUN:-true}"
```

Après le `done` de la boucle `for phase_spec` et **impérativement AVANT la ligne `FAIL_TOTAL=$(( ... ))`** (~ligne 481 — sinon un échec extract n'incrémente pas `FAIL_TOTAL` et le script sort en `exit 0` silencieux ; c'est le compteur qui déclenche le résumé d'échec et `post_run_alert`) :

```bash
# --- EXTRACT: ticket knowledge extraction (proposer-only) -----------------
# Pas une phase claude -p : CLI Python direct (pattern domain_backfill,
# NVIDIA API JSON strict sans tools). Insère sa propre row dream_runs
# (phase='extract') pour la visibilité briefing (killswitches + last failure).
if [[ "$BRAIN_DREAM_EXTRACT_ENABLED" != "true" ]]; then
  log "SKIP extract (killswitch BRAIN_DREAM_EXTRACT_ENABLED=$BRAIN_DREAM_EXTRACT_ENABLED)"
else
  extract_args=(--limit 20)
  if [[ "$BRAIN_DREAM_EXTRACT_DRY_RUN" != "true" ]]; then
    extract_args+=(--wet)
  fi
  log "extract: ticket_extract starting (dry_run=$BRAIN_DREAM_EXTRACT_DRY_RUN)"
  set +e
  timeout 10m uv run python -m scripts.ticket_extract "${extract_args[@]}" \
    >> "$LOG_DIR/${TIMESTAMP}_extract.log" 2>&1
  extract_rc=$?
  set -e
  if (( extract_rc == 0 )); then
    log "DONE extract"
  else
    log "FAIL extract (rc=$extract_rc) — see ${TIMESTAMP}_extract.log"
    FAILED_PHASES+=("extract")
  fi
fi
```

- [ ] **Step 4: `KillswitchState` + `killswitch_state()`** dans `dream_run_service.py`

Ajouter à la dataclass (avec défauts → rétro-compatible avec tous les constructeurs existants, y compris le fallback de session_tools) :

```python
    extract_enabled: bool = False
    extract_dry: bool = True
    extract_clean_dry_nights: int = 0
```

Dans `killswitch_state()`, après le calcul reorg (mêmes patterns : présence de la phase dans la dernière run_date + `phase_dry_run` + `_clean_dry_streak(session, "extract")`) :

```python
            extract_enabled = "extract" in phases
            extract_dry = bool(phases["extract"]["phase_dry_run"]) if extract_enabled else True
            extract_streak = await self._clean_dry_streak(session, "extract")
```

et passer les 3 champs au `KillswitchState(...)` final (le early-return "no activity" garde ses défauts).

- [ ] **Step 5: Ligne EXTRACT dans `_section_killswitches`** (session_tools.py, après la ligne REORG) :

```python
    lines.append(
        _row("EXTRACT", state.extract_enabled, state.extract_dry, state.extract_clean_dry_nights)
    )
```

Ajouter un test dans `test_session_tools.py` : un briefing avec `extract_enabled=True, extract_dry=True, extract_clean_dry_nights=2` doit contenir `"EXTRACT: enabled (dry · 2 clean DRY nights)"` ; et adapter les assertions existantes si elles pinnent le bloc killswitches entier.

- [ ] **Step 6: Vérifier vert + commit**

Run: `pytest tests/unit -q && ruff check src/ tests/ scripts/ && ruff format --check src/ tests/ scripts/ && mypy src/ && bash -n scripts/dream.sh`
Expected: tout PASS, `bash -n` silencieux (syntaxe ok)

```bash
git add scripts/dream.sh src/brain_v42/services/dream_run_service.py src/brain_v42/mcp/tools/session_tools.py tests/unit/test_dream_sh_extract.py tests/unit/mcp/test_session_tools.py
git commit -m "feat(tickets): step extract dans dream.sh + killswitch EXTRACT (dry par défaut)"
```

---

### Task 7: Documentation

**Files:**
- Modify: `docs/MCP_TOOLS.md` — section "Tickets (coordination cross-projet)" : les 5 tools, signatures, exemple de cycle request complet + fyi
- Modify: `docs/SCHEMA.md` — les 3 tables (colonnes, CHECK, indexes) + note "famille coordination : hors embeddings/search/decay/graph"
- Modify: `CLAUDE.md` — dans le bloc env de Configuration, ajouter :

```bash
# Tickets — extraction nocturne (dream, proposer-only)
BRAIN_DREAM_EXTRACT_ENABLED=false
BRAIN_DREAM_EXTRACT_DRY_RUN=true
```

**Interfaces:** — (docs uniquement, aucun code)

- [ ] **Step 1: Rédiger les 3 fichiers** en suivant le format des sections existantes de chaque doc (tableaux pour MCP_TOOLS, DDL commenté pour SCHEMA).
- [ ] **Step 2: Vérifier** — `ruff format --check src/ tests/ scripts/` (inchangé) et relire que les noms de tools/colonnes matchent le code livré.
- [ ] **Step 3: Commit**

```bash
git add docs/MCP_TOOLS.md docs/SCHEMA.md CLAUDE.md
git commit -m "docs(tickets): surface MCP + schéma + env EXTRACT"
```

---

## Vérification finale (post-Task 7, avant merge)

1. `pytest tests/unit -q` — 100 % vert
2. `pytest --cov=brain_v42 --cov-report=term-missing` — coverage ≥ 60 %
3. `ruff check src/ tests/ scripts/ && ruff format --check src/ tests/ scripts/ && mypy src/`
4. `bash -n scripts/dream.sh`
5. Si `BRAIN_V42_TEST_DB_URL` dispo : `pytest tests/integration/db/test_tickets_roundtrip.py -q`
6. Smoke manuel (optionnel, DB dev) : `alembic upgrade head` puis un cycle create→resolve→confirm via les tools.
