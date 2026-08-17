# Plans Chunking & Search Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make project plans/specs discoverable via `brain_search` by chunking markdown files on section headers and integrating chunks into the HybridSearcher.

**Architecture:** Extend the existing `IndexedPlan` infrastructure with a new `indexed_plan_chunks` table (cascade delete, denormalized fields), a pure-function `plan_chunker` module, and generic MCP tool dispatchers (`brain_get`, `brain_delete`) that already accept entity types. Zero new MCP tools.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 async, asyncpg, Alembic, pgvector, FastMCP 3.1, pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-04-07-plans-chunking-indexing-design.md`

---

## File Structure

### Created
- `src/brain_v42/services/plan_chunker.py` — pure function `chunk_markdown()` + dataclasses
- `src/brain_v42/models/indexed_plan_chunk.py` — Pydantic models for chunk entity
- `src/brain_v42/repositories/pg_indexed_plan_repo.py` — repository for plans + chunks (if not already present under a different name — see Task 5)
- `alembic/versions/014_plan_chunks.py` — schema migration with backfill
- `tests/unit/services/test_plan_chunker.py`
- `tests/unit/services/test_plan_chunker_edge_cases.py`
- `tests/integration/test_plan_indexer_chunking.py`
- `tests/integration/test_plan_search_integration.py`
- `tests/integration/test_plan_get_delete_integration.py`
- `tests/integration/test_plan_indexer_non_regression.py`

### Modified
- `src/brain_v42/models/indexed_plan.py` — add new fields to `IndexedPlan` and `IndexedPlanCreate`
- `src/brain_v42/services/plan_indexer.py` — call chunker, persist chunks in transaction
- `src/brain_v42/mcp/tools/plan_tools.py` — no signature change, return updated stats shape
- `src/brain_v42/services/search/hybrid.py` — add plan chunks as a searchable source
- `src/brain_v42/mcp/tools/brain_tools.py` — accept `"plan"` in `types` of `brain_search`
- `src/brain_v42/mcp/tools/crud_tools.py` — dispatch `entity_type="plan"` in `brain_get`, `brain_delete`

---

## Task 1: Markdown chunker — core splitting on H2

**Files:**
- Create: `tests/unit/services/test_plan_chunker.py`
- Create: `src/brain_v42/services/plan_chunker.py`

- [ ] **Step 1: Write failing test — basic H2 splitting**

```python
# tests/unit/services/test_plan_chunker.py
"""Unit tests for plan_chunker.chunk_markdown()."""

from brain_v42.services.plan_chunker import chunk_markdown


def test_chunk_markdown_splits_on_h2():
    content = """# Main Title

Intro paragraph.

## Section A

Content of A.

## Section B

Content of B.
"""
    parent, chunks = chunk_markdown(content)

    assert parent.title == "Main Title"
    assert parent.preamble.strip() == "Intro paragraph."
    assert parent.content == content
    assert parent.word_count > 0

    assert len(chunks) == 2
    assert chunks[0].section_title == "Section A"
    assert chunks[0].section_order == 0
    assert "Content of A." in chunks[0].content
    assert chunks[1].section_title == "Section B"
    assert chunks[1].section_order == 1
    assert "Content of B." in chunks[1].content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/services/test_plan_chunker.py::test_chunk_markdown_splits_on_h2 -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'brain_v42.services.plan_chunker'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/brain_v42/services/plan_chunker.py
"""Markdown chunker for project plans and specs.

Splits markdown content on H2/H3 headers into chunks with embedding-ready
content. Pure functions, no I/O, no DB.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class PlanParentData:
    title: str
    preamble: str
    content: str
    summary: str | None = None
    status: str = "active"
    word_count: int = 0
    tags: list[str] = field(default_factory=list)


@dataclass
class ChunkData:
    section_title: str
    section_path: str
    content: str
    section_order: int
    word_count: int = 0


H2_PATTERN = re.compile(r"^## (?!#)(.+?)\s*$", re.MULTILINE)


def _count_words(text: str) -> int:
    return len([w for w in text.split() if w.strip()])


def _extract_title(content: str) -> str:
    """Return the first H1 title or an empty string."""
    for line in content.splitlines():
        if line.startswith("# ") and not line.startswith("## "):
            return line[2:].strip()
    return ""


def chunk_markdown(content: str) -> tuple[PlanParentData, list[ChunkData]]:
    """Split markdown into a parent + ordered chunks.

    - H1 becomes the parent title.
    - H2 is the chunk boundary.
    - Content before the first H2 is the parent preamble.
    """
    title = _extract_title(content)

    # Split on H2 markers, keeping the position so we can extract sections.
    matches = list(H2_PATTERN.finditer(content))

    if not matches:
        preamble = content
        chunks: list[ChunkData] = []
    else:
        preamble_end = matches[0].start()
        # Strip the leading H1 line from the preamble if present.
        preamble_raw = content[:preamble_end]
        preamble_lines = [
            line for line in preamble_raw.splitlines() if not line.startswith("# ")
        ]
        preamble = "\n".join(preamble_lines).strip()

        chunks = []
        for i, match in enumerate(matches):
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            section_body = content[start:end]
            section_title = match.group(1).strip()
            chunks.append(
                ChunkData(
                    section_title=section_title,
                    section_path=section_title,
                    content=section_body.rstrip(),
                    section_order=i,
                    word_count=_count_words(section_body),
                )
            )

    parent = PlanParentData(
        title=title,
        preamble=preamble,
        content=content,
        word_count=_count_words(content),
    )
    return parent, chunks
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/services/test_plan_chunker.py::test_chunk_markdown_splits_on_h2 -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/unit/services/test_plan_chunker.py src/brain_v42/services/plan_chunker.py
git commit -m "feat(plan-chunker): split markdown on H2 headers"
```

---

## Task 2: Markdown chunker — edge cases

**Files:**
- Create: `tests/unit/services/test_plan_chunker_edge_cases.py`
- Modify: `src/brain_v42/services/plan_chunker.py`

- [ ] **Step 1: Write failing tests — code fences, frontmatter, min/max, H3 path**

```python
# tests/unit/services/test_plan_chunker_edge_cases.py
"""Edge case tests for plan_chunker.chunk_markdown()."""

import pytest

from brain_v42.services.plan_chunker import chunk_markdown


def test_ignores_h2_inside_fenced_code_block():
    content = """# Doc

```python
## This is a comment, not a header
x = 1
```

## Real Section

Content.
"""
    parent, chunks = chunk_markdown(content)
    assert len(chunks) == 1
    assert chunks[0].section_title == "Real Section"


def test_extracts_frontmatter_title_status_summary_tags():
    content = """---
title: From Frontmatter
status: draft
summary: A short abstract.
tags: [alpha, beta]
---

# Ignored H1 Title

## Section

Body.
"""
    parent, chunks = chunk_markdown(content)
    assert parent.title == "From Frontmatter"
    assert parent.status == "draft"
    assert parent.summary == "A short abstract."
    assert parent.tags == ["alpha", "beta"]
    # Stored content has frontmatter stripped
    assert "---" not in parent.content
    assert "title: From Frontmatter" not in parent.content


def test_no_h2_produces_empty_chunks_with_full_preamble():
    content = """# Only a title

Just a paragraph, no H2.
"""
    parent, chunks = chunk_markdown(content)
    assert chunks == []
    assert "Just a paragraph" in parent.preamble


def test_tiny_chunk_merged_with_next():
    content = """# Doc

## Tiny

word

## Normal

""" + ("word " * 60)
    parent, chunks = chunk_markdown(content)
    # 4 words in "Tiny" < 50 threshold -> merged into next
    assert len(chunks) == 1
    assert chunks[0].section_title == "Normal"
    assert "Tiny" in chunks[0].content  # merged header line kept
    assert "word " * 60 in chunks[0].content


def test_oversized_chunk_stored_with_warning(caplog):
    big_body = "word " * 1600
    content = f"""# Doc

## Huge

{big_body}
"""
    parent, chunks = chunk_markdown(content)
    assert len(chunks) == 1
    assert chunks[0].word_count > 1500
    assert any("oversized" in rec.message.lower() for rec in caplog.records)


def test_h3_contributes_to_section_path():
    content = """# Doc

## Parent

### Child One

Body of child one.

### Child Two

Body of child two.
"""
    parent, chunks = chunk_markdown(content)
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.section_title == "Parent"
    # Section path should list the H3s as a breadcrumb hint
    assert "Child One" in chunk.section_path or "Child One" in chunk.content
    assert "Child Two" in chunk.content


def test_empty_content_yields_empty_parent():
    parent, chunks = chunk_markdown("")
    assert parent.title == ""
    assert chunks == []


def test_crlf_line_endings_handled():
    content = "# Doc\r\n\r\n## Section\r\n\r\nBody.\r\n"
    parent, chunks = chunk_markdown(content)
    assert parent.title == "Doc"
    assert len(chunks) == 1
    assert chunks[0].section_title == "Section"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/services/test_plan_chunker_edge_cases.py -v`
Expected: Several failures (frontmatter not parsed, fenced code not ignored, min/max rules missing, CRLF not handled).

- [ ] **Step 3: Extend the chunker implementation**

Replace the full content of `src/brain_v42/services/plan_chunker.py` with:

```python
"""Markdown chunker for project plans and specs.

Splits markdown content on H2 headers into chunks. Handles YAML frontmatter,
fenced code blocks (no header bleed), min/max chunk rules, and CRLF line endings.
Pure functions, no I/O, no DB.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import yaml

logger = logging.getLogger(__name__)

MIN_CHUNK_WORDS = 50
MAX_CHUNK_WORDS = 1500


@dataclass
class PlanParentData:
    title: str
    preamble: str
    content: str
    summary: str | None = None
    status: str = "active"
    word_count: int = 0
    tags: list[str] = field(default_factory=list)


@dataclass
class ChunkData:
    section_title: str
    section_path: str
    content: str
    section_order: int
    word_count: int = 0


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_H1_RE = re.compile(r"^# (?!#)(.+?)\s*$", re.MULTILINE)


def _count_words(text: str) -> int:
    return len([w for w in text.split() if w.strip()])


def _normalize_line_endings(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _extract_frontmatter(content: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, content_without_frontmatter)."""
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}, content
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        logger.warning("Failed to parse plan frontmatter; ignoring.")
        return {}, content[match.end() :]
    return data, content[match.end() :]


def _strip_fenced_code_blocks(content: str) -> str:
    """Replace fenced code block bodies with blank lines of equal count.

    Used only for locating H2 headers. The original content is preserved
    in the returned chunks.
    """
    out: list[str] = []
    in_fence = False
    for line in content.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out.append("")
            continue
        out.append("" if in_fence else line)
    return "\n".join(out)


def _extract_title_from_content(content: str) -> str:
    match = _H1_RE.search(content)
    return match.group(1).strip() if match else ""


def _find_h2_positions(scrubbed: str) -> list[tuple[int, int, str]]:
    """Return [(start, end_of_header_line, title), ...] for each H2."""
    positions: list[tuple[int, int, str]] = []
    for line_match in re.finditer(r"^## (?!#)(.+?)\s*$", scrubbed, re.MULTILINE):
        positions.append(
            (line_match.start(), line_match.end(), line_match.group(1).strip())
        )
    return positions


def _build_chunks(
    content: str, scrubbed: str, h2_positions: list[tuple[int, int, str]]
) -> list[ChunkData]:
    chunks: list[ChunkData] = []
    for i, (start, _end, title) in enumerate(h2_positions):
        body_end = h2_positions[i + 1][0] if i + 1 < len(h2_positions) else len(content)
        section_body = content[start:body_end].rstrip()
        # Collect H3s within this section to build a section path
        h3_titles = re.findall(r"^### (?!#)(.+?)\s*$", section_body, re.MULTILINE)
        path = " > ".join([title] + h3_titles) if h3_titles else title
        chunks.append(
            ChunkData(
                section_title=title,
                section_path=path,
                content=section_body,
                section_order=i,
                word_count=_count_words(section_body),
            )
        )
    return chunks


def _apply_min_max_rules(chunks: list[ChunkData]) -> list[ChunkData]:
    # Merge tiny chunks forward
    merged: list[ChunkData] = []
    pending: ChunkData | None = None
    for chunk in chunks:
        if pending is not None:
            chunk = ChunkData(
                section_title=chunk.section_title,
                section_path=chunk.section_path,
                content=f"## {pending.section_title}\n\n{pending.content}\n\n{chunk.content}",
                section_order=chunk.section_order,
                word_count=pending.word_count + chunk.word_count,
            )
            pending = None
        if chunk.word_count < MIN_CHUNK_WORDS:
            pending = chunk
            continue
        merged.append(chunk)
    # If the last chunk was tiny and had nothing to merge into, keep it
    if pending is not None:
        merged.append(pending)

    # Warn on oversized chunks (keep them as-is)
    for chunk in merged:
        if chunk.word_count > MAX_CHUNK_WORDS:
            logger.warning(
                "Oversized chunk detected: section=%r words=%d",
                chunk.section_title,
                chunk.word_count,
            )

    # Reassign section_order after merges
    for i, chunk in enumerate(merged):
        chunk.section_order = i
    return merged


def chunk_markdown(content: str) -> tuple[PlanParentData, list[ChunkData]]:
    """Split markdown into (parent, chunks)."""
    if not content:
        return PlanParentData(title="", preamble="", content=""), []

    content = _normalize_line_endings(content)
    fm, content_no_fm = _extract_frontmatter(content)

    # Title: frontmatter first, then first H1 in content
    title = str(fm.get("title") or fm.get("name") or "").strip()
    if not title:
        title = _extract_title_from_content(content_no_fm)

    status = str(fm.get("status") or "active").strip() or "active"
    summary = fm.get("summary")
    tags = fm.get("tags") or []
    if not isinstance(tags, list):
        tags = []

    scrubbed = _strip_fenced_code_blocks(content_no_fm)
    h2_positions = _find_h2_positions(scrubbed)

    if not h2_positions:
        preamble = content_no_fm
        chunks: list[ChunkData] = []
    else:
        preamble_raw = content_no_fm[: h2_positions[0][0]]
        preamble_lines = [
            line for line in preamble_raw.splitlines() if not line.startswith("# ")
        ]
        preamble = "\n".join(preamble_lines).strip()
        chunks = _build_chunks(content_no_fm, scrubbed, h2_positions)
        chunks = _apply_min_max_rules(chunks)

    parent = PlanParentData(
        title=title,
        preamble=preamble,
        content=content_no_fm,
        summary=str(summary).strip() if summary else None,
        status=status,
        word_count=_count_words(content_no_fm),
        tags=[str(t) for t in tags],
    )
    return parent, chunks
```

- [ ] **Step 4: Run all chunker tests**

Run: `pytest tests/unit/services/test_plan_chunker.py tests/unit/services/test_plan_chunker_edge_cases.py -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/services/test_plan_chunker_edge_cases.py src/brain_v42/services/plan_chunker.py
git commit -m "feat(plan-chunker): frontmatter, code fences, min/max rules, CRLF"
```

---

## Task 3: Alembic migration 014 — schema + backfill

**Files:**
- Create: `alembic/versions/014_plan_chunks.py`

- [ ] **Step 1: Write the migration**

```python
# alembic/versions/014_plan_chunks.py
"""Plan chunking support: extend indexed_plans + create indexed_plan_chunks.

Revision ID: 014
Revises: 013
Create Date: 2026-04-07
"""

from __future__ import annotations

from alembic import op

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Extend indexed_plans ───────────────────────────────────────────────
    op.execute("""
        ALTER TABLE indexed_plans
            ADD COLUMN content TEXT NOT NULL DEFAULT '',
            ADD COLUMN summary TEXT,
            ADD COLUMN search_vector TSVECTOR,
            ADD COLUMN tags VARCHAR[] NOT NULL DEFAULT '{}',
            ADD COLUMN metadata JSONB NOT NULL DEFAULT '{}',
            ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'active'
                CHECK (status IN ('draft', 'active', 'archived')),
            ADD COLUMN chunk_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN word_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN last_accessed_at TIMESTAMPTZ,
            ADD COLUMN freshness_status VARCHAR(20) NOT NULL DEFAULT 'fresh'
                CHECK (freshness_status IN ('fresh', 'stale', 'archived')),
            ADD COLUMN indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    """)

    op.execute(
        "CREATE INDEX idx_indexed_plans_tags ON indexed_plans USING GIN(tags)"
    )
    op.execute(
        "CREATE INDEX idx_indexed_plans_search_vector "
        "ON indexed_plans USING GIN(search_vector)"
    )
    op.execute(
        "CREATE INDEX idx_indexed_plans_pk_status_fresh "
        "ON indexed_plans(project_key, status, freshness_status)"
    )

    # ── 2. Create indexed_plan_chunks ─────────────────────────────────────────
    op.execute("""
        CREATE TABLE indexed_plan_chunks (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            plan_id UUID NOT NULL REFERENCES indexed_plans(id) ON DELETE CASCADE,
            section_title VARCHAR(500) NOT NULL,
            section_path VARCHAR(1000) NOT NULL,
            content TEXT NOT NULL,
            section_order INTEGER NOT NULL,
            word_count INTEGER NOT NULL DEFAULT 0,
            embedding VECTOR(1536) NOT NULL,
            search_vector TSVECTOR,
            tags VARCHAR[] NOT NULL DEFAULT '{}',
            project_key VARCHAR(50) NOT NULL,
            plan_type VARCHAR(20) NOT NULL
                CHECK (plan_type IN ('spec', 'plan')),
            status VARCHAR(20) NOT NULL DEFAULT 'active'
                CHECK (status IN ('draft', 'active', 'archived')),
            access_count INTEGER NOT NULL DEFAULT 0,
            last_accessed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    op.execute(
        "CREATE INDEX idx_plan_chunks_plan_id ON indexed_plan_chunks(plan_id)"
    )
    op.execute("""
        CREATE INDEX idx_plan_chunks_embedding ON indexed_plan_chunks
        USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)
    """)
    op.execute(
        "CREATE INDEX idx_plan_chunks_tags "
        "ON indexed_plan_chunks USING GIN(tags)"
    )
    op.execute(
        "CREATE INDEX idx_plan_chunks_search_vector "
        "ON indexed_plan_chunks USING GIN(search_vector)"
    )
    op.execute(
        "CREATE INDEX idx_plan_chunks_pk_type "
        "ON indexed_plan_chunks(project_key, plan_type)"
    )

    # ── 3. Backfill existing indexed_plans rows ───────────────────────────────
    # File reads are deferred to the first brain_reindex_plans run.
    # We mark existing plans as stale so the next reindex will pick them up.
    op.execute(
        "UPDATE indexed_plans SET freshness_status = 'stale', indexed_at = updated_at"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS indexed_plan_chunks CASCADE")

    op.execute("DROP INDEX IF EXISTS idx_indexed_plans_tags")
    op.execute("DROP INDEX IF EXISTS idx_indexed_plans_search_vector")
    op.execute("DROP INDEX IF EXISTS idx_indexed_plans_pk_status_fresh")

    op.execute("""
        ALTER TABLE indexed_plans
            DROP COLUMN IF EXISTS indexed_at,
            DROP COLUMN IF EXISTS freshness_status,
            DROP COLUMN IF EXISTS last_accessed_at,
            DROP COLUMN IF EXISTS access_count,
            DROP COLUMN IF EXISTS word_count,
            DROP COLUMN IF EXISTS chunk_count,
            DROP COLUMN IF EXISTS status,
            DROP COLUMN IF EXISTS metadata,
            DROP COLUMN IF EXISTS tags,
            DROP COLUMN IF EXISTS search_vector,
            DROP COLUMN IF EXISTS summary,
            DROP COLUMN IF EXISTS content
    """)
```

- [ ] **Step 2: Run the migration**

Run: `alembic upgrade head`
Expected: `INFO  [alembic.runtime.migration] Running upgrade 013 -> 014, Plan chunking support...`

- [ ] **Step 3: Verify schema with psql**

Run:
```bash
psql "postgresql://brain:brain@localhost:5433/brain" -c "\d indexed_plans"
psql "postgresql://brain:brain@localhost:5433/brain" -c "\d indexed_plan_chunks"
```
Expected: New columns visible on `indexed_plans`, `indexed_plan_chunks` table present with all indexes.

- [ ] **Step 4: Verify downgrade works**

Run: `alembic downgrade -1 && alembic upgrade head`
Expected: Both operations succeed with no errors.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/014_plan_chunks.py
git commit -m "feat(db): migration 014 plan chunks schema + backfill"
```

---

## Task 4: Pydantic models for IndexedPlan and IndexedPlanChunk

**Files:**
- Modify: `src/brain_v42/models/indexed_plan.py`
- Create: `src/brain_v42/models/indexed_plan_chunk.py`
- Create: `tests/unit/models/test_indexed_plan_chunk_model.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/models/test_indexed_plan_chunk_model.py
"""Unit tests for IndexedPlanChunk and updated IndexedPlan models."""

from uuid import uuid4

from brain_v42.models.indexed_plan import IndexedPlan, IndexedPlanCreate
from brain_v42.models.indexed_plan_chunk import (
    IndexedPlanChunk,
    IndexedPlanChunkCreate,
)


def test_indexed_plan_create_accepts_new_fields():
    payload = IndexedPlanCreate(
        file_path="docs/spec.md",
        title="My Spec",
        plan_type="spec",
        project_key="brain-v42",
        content_hash="a" * 64,
        content="# My Spec\n\n## Section\n\nBody.",
        summary="Short summary.",
        status="draft",
        tags=["architecture", "brain"],
        metadata={"author": "hawixs"},
        word_count=42,
    )
    assert payload.content.startswith("# My Spec")
    assert payload.status == "draft"
    assert payload.tags == ["architecture", "brain"]


def test_indexed_plan_chunk_create():
    payload = IndexedPlanChunkCreate(
        plan_id=uuid4(),
        section_title="Section",
        section_path="Section",
        content="## Section\n\nBody.",
        section_order=0,
        word_count=5,
        project_key="brain-v42",
        plan_type="spec",
        status="active",
        tags=["architecture"],
    )
    assert payload.section_order == 0
    assert payload.plan_type == "spec"


def test_indexed_plan_exposes_chunks_optional_field():
    from datetime import datetime

    plan = IndexedPlan(
        id=uuid4(),
        file_path="a.md",
        title="A",
        plan_type="spec",
        project_key="brain-v42",
        content_hash="h" * 64,
        content="# A",
        status="active",
        chunk_count=0,
        word_count=1,
        freshness_status="fresh",
        indexed_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    assert plan.status == "active"
    assert plan.chunk_count == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/models/test_indexed_plan_chunk_model.py -v`
Expected: Import errors (`IndexedPlanChunk` not found) and `ValidationError` on new fields.

- [ ] **Step 3: Update IndexedPlan model**

Replace `src/brain_v42/models/indexed_plan.py` with:

```python
"""Pydantic models for IndexedPlan entity."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from brain_v42.models.base import TimestampMixin


class IndexedPlanCreate(BaseModel):
    """Payload for creating/upserting an indexed plan."""

    file_path: str = Field(..., max_length=500)
    title: str = Field(..., max_length=500)
    plan_type: Literal["spec", "plan"]
    project_key: str = Field(..., max_length=50)
    content_hash: str = Field(..., max_length=64)
    content: str
    summary: str | None = None
    status: Literal["draft", "active", "archived"] = "active"
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    chunk_count: int = 0
    word_count: int = 0


class IndexedPlan(TimestampMixin, BaseModel):
    """IndexedPlan as stored in the database."""

    model_config = {"from_attributes": True}

    id: UUID
    file_path: str
    title: str
    plan_type: str
    project_key: str
    content_hash: str
    content: str
    summary: str | None = None
    status: str
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    chunk_count: int
    word_count: int
    access_count: int = 0
    last_accessed_at: datetime | None = None
    freshness_status: str
    indexed_at: datetime
```

- [ ] **Step 4: Create the chunk model**

```python
# src/brain_v42/models/indexed_plan_chunk.py
"""Pydantic models for IndexedPlanChunk entity."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class IndexedPlanChunkCreate(BaseModel):
    """Payload for inserting a chunk.

    `plan_id` is optional because the repository sets it from the parent plan
    row it just upserted; callers build chunks without knowing the plan UUID.
    """

    plan_id: UUID | None = None
    section_title: str = Field(..., max_length=500)
    section_path: str = Field(..., max_length=1000)
    content: str
    section_order: int
    word_count: int = 0
    project_key: str = Field(..., max_length=50)
    plan_type: Literal["spec", "plan"]
    status: Literal["draft", "active", "archived"] = "active"
    tags: list[str] = Field(default_factory=list)


class IndexedPlanChunk(BaseModel):
    """IndexedPlanChunk as stored in the database."""

    model_config = {"from_attributes": True}

    id: UUID
    plan_id: UUID
    section_title: str
    section_path: str
    content: str
    section_order: int
    word_count: int
    project_key: str
    plan_type: str
    status: str
    tags: list[str] = Field(default_factory=list)
    access_count: int = 0
    last_accessed_at: datetime | None = None
    created_at: datetime
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/models/test_indexed_plan_chunk_model.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/models/indexed_plan.py src/brain_v42/models/indexed_plan_chunk.py tests/unit/models/test_indexed_plan_chunk_model.py
git commit -m "feat(models): IndexedPlanChunk + IndexedPlan new fields"
```

---

## Task 5: Repository — upsert plan with chunks, get_with_chunks, delete cascade

**Files:**
- Locate the existing file that performs raw SQL against `indexed_plans`. Based on the codebase it is either `src/brain_v42/repositories/pg_indexed_plan_repo.py` or the CRUD lives inline in `plan_indexer.py`. If no dedicated repo file exists, create `src/brain_v42/repositories/pg_indexed_plan_repo.py` and move inline SQL there.
- Create: `tests/integration/test_plan_repo.py`

- [ ] **Step 1: Locate or create the repository file**

Run: `grep -rln "FROM indexed_plans\|INTO indexed_plans" src/brain_v42/`
If the file is `src/brain_v42/services/plan_indexer.py`, create a new repository file and move the raw SQL there; the service will use the repo. If an existing repo file is found, work with it in place.

- [ ] **Step 2: Write failing integration tests**

```python
# tests/integration/test_plan_repo.py
"""Integration tests for PgIndexedPlanRepo."""

from __future__ import annotations

from uuid import uuid4

import pytest

from brain_v42.models.indexed_plan import IndexedPlanCreate
from brain_v42.models.indexed_plan_chunk import IndexedPlanChunkCreate
from brain_v42.repositories.pg_indexed_plan_repo import PgIndexedPlanRepo


@pytest.mark.asyncio
async def test_upsert_plan_with_chunks_and_get(db_session):
    repo = PgIndexedPlanRepo(db_session)
    embedding = [0.01] * 1536

    plan_data = IndexedPlanCreate(
        file_path="tests/fixtures/plan.md",
        title="Repo Test Plan",
        plan_type="plan",
        project_key="brain-v42",
        content_hash="h" * 64,
        content="# Repo Test Plan\n\n## A\n\nBody A.\n\n## B\n\nBody B.",
        status="active",
        tags=["test"],
        word_count=20,
        chunk_count=2,
    )
    chunks_data = [
        IndexedPlanChunkCreate(
            section_title="A",
            section_path="A",
            content="## A\n\nBody A.",
            section_order=0,
            word_count=3,
            project_key="brain-v42",
            plan_type="plan",
            tags=["test"],
        ),
        IndexedPlanChunkCreate(
            section_title="B",
            section_path="B",
            content="## B\n\nBody B.",
            section_order=1,
            word_count=3,
            project_key="brain-v42",
            plan_type="plan",
            tags=["test"],
        ),
    ]
    chunk_embeddings = [[0.02] * 1536, [0.03] * 1536]

    plan_id = await repo.upsert_plan_with_chunks(
        plan_data, embedding, chunks_data, chunk_embeddings
    )
    assert plan_id is not None

    fetched, chunks = await repo.get_with_chunks(plan_id)
    assert fetched.title == "Repo Test Plan"
    assert fetched.chunk_count == 2
    assert [c.section_order for c in chunks] == [0, 1]
    assert chunks[0].section_title == "A"


@pytest.mark.asyncio
async def test_upsert_replaces_existing_chunks(db_session):
    repo = PgIndexedPlanRepo(db_session)

    base = IndexedPlanCreate(
        file_path="tests/fixtures/replace.md",
        title="Replace Test",
        plan_type="plan",
        project_key="brain-v42",
        content_hash="h" * 64,
        content="# T\n\n## A\n\nA body",
        chunk_count=1,
        word_count=5,
    )
    chunk_a = IndexedPlanChunkCreate(
        section_title="A",
        section_path="A",
        content="## A\n\nA body",
        section_order=0,
        word_count=3,
        project_key="brain-v42",
        plan_type="plan",
    )
    plan_id = await repo.upsert_plan_with_chunks(
        base, [0.01] * 1536, [chunk_a], [[0.02] * 1536]
    )

    # Re-upsert with one different chunk
    base2 = base.model_copy(update={"content_hash": "i" * 64, "chunk_count": 1})
    chunk_b = IndexedPlanChunkCreate(
        section_title="B",
        section_path="B",
        content="## B\n\nB body",
        section_order=0,
        word_count=3,
        project_key="brain-v42",
        plan_type="plan",
    )
    plan_id_2 = await repo.upsert_plan_with_chunks(
        base2, [0.01] * 1536, [chunk_b], [[0.03] * 1536]
    )
    assert plan_id_2 == plan_id  # same file_path -> same row

    _, chunks = await repo.get_with_chunks(plan_id)
    assert len(chunks) == 1
    assert chunks[0].section_title == "B"


@pytest.mark.asyncio
async def test_delete_cascades_to_chunks(db_session):
    repo = PgIndexedPlanRepo(db_session)

    base = IndexedPlanCreate(
        file_path="tests/fixtures/del.md",
        title="Del",
        plan_type="plan",
        project_key="brain-v42",
        content_hash="h" * 64,
        content="# Del\n\n## A\n\nA body",
        chunk_count=1,
        word_count=5,
    )
    chunk = IndexedPlanChunkCreate(
        section_title="A",
        section_path="A",
        content="## A\n\nA body",
        section_order=0,
        word_count=3,
        project_key="brain-v42",
        plan_type="plan",
    )
    plan_id = await repo.upsert_plan_with_chunks(
        base, [0.01] * 1536, [chunk], [[0.02] * 1536]
    )

    deleted = await repo.delete(plan_id)
    assert deleted is True

    result = await repo.get_with_chunks(plan_id)
    assert result is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/integration/test_plan_repo.py -v`
Expected: FAIL (repo methods missing).

- [ ] **Step 4: Implement the repository methods**

```python
# src/brain_v42/repositories/pg_indexed_plan_repo.py
"""Repository for IndexedPlan and IndexedPlanChunk.

Raw SQL via SQLAlchemy async. Upserts plans and their chunks atomically.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.models.indexed_plan import IndexedPlan, IndexedPlanCreate
from brain_v42.models.indexed_plan_chunk import (
    IndexedPlanChunk,
    IndexedPlanChunkCreate,
)


class PgIndexedPlanRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_plan_with_chunks(
        self,
        plan: IndexedPlanCreate,
        plan_embedding: list[float],
        chunks: list[IndexedPlanChunkCreate],
        chunk_embeddings: list[list[float]],
    ) -> UUID:
        """Upsert the plan by file_path, replace its chunks atomically."""
        assert len(chunks) == len(chunk_embeddings), (
            "chunks and embeddings must align"
        )

        upsert_sql = text("""
            INSERT INTO indexed_plans (
                file_path, title, plan_type, project_key, content_hash,
                embedding, content, summary, status, tags, metadata,
                chunk_count, word_count, freshness_status, indexed_at,
                search_vector
            ) VALUES (
                :file_path, :title, :plan_type, :project_key, :content_hash,
                :embedding, :content, :summary, :status, :tags,
                CAST(:metadata AS JSONB),
                :chunk_count, :word_count, 'fresh', NOW(),
                to_tsvector('english', :title || ' ' || :content)
            )
            ON CONFLICT (file_path) DO UPDATE SET
                title = EXCLUDED.title,
                plan_type = EXCLUDED.plan_type,
                project_key = EXCLUDED.project_key,
                content_hash = EXCLUDED.content_hash,
                embedding = EXCLUDED.embedding,
                content = EXCLUDED.content,
                summary = EXCLUDED.summary,
                status = EXCLUDED.status,
                tags = EXCLUDED.tags,
                metadata = EXCLUDED.metadata,
                chunk_count = EXCLUDED.chunk_count,
                word_count = EXCLUDED.word_count,
                freshness_status = 'fresh',
                indexed_at = NOW(),
                search_vector = EXCLUDED.search_vector,
                updated_at = NOW()
            RETURNING id
        """)

        result = await self._session.execute(
            upsert_sql,
            {
                "file_path": plan.file_path,
                "title": plan.title,
                "plan_type": plan.plan_type,
                "project_key": plan.project_key,
                "content_hash": plan.content_hash,
                "embedding": str(plan_embedding),
                "content": plan.content,
                "summary": plan.summary,
                "status": plan.status,
                "tags": plan.tags,
                "metadata": plan.metadata,
                "chunk_count": plan.chunk_count,
                "word_count": plan.word_count,
            },
        )
        plan_id: UUID = result.scalar_one()

        # Replace chunks
        await self._session.execute(
            text("DELETE FROM indexed_plan_chunks WHERE plan_id = :plan_id"),
            {"plan_id": plan_id},
        )

        if chunks:
            insert_chunk_sql = text("""
                INSERT INTO indexed_plan_chunks (
                    plan_id, section_title, section_path, content,
                    section_order, word_count, embedding, search_vector,
                    tags, project_key, plan_type, status
                ) VALUES (
                    :plan_id, :section_title, :section_path, :content,
                    :section_order, :word_count, :embedding,
                    to_tsvector('english', :section_title || ' ' || :content),
                    :tags, :project_key, :plan_type, :status
                )
            """)
            for chunk, emb in zip(chunks, chunk_embeddings, strict=True):
                await self._session.execute(
                    insert_chunk_sql,
                    {
                        "plan_id": plan_id,
                        "section_title": chunk.section_title,
                        "section_path": chunk.section_path,
                        "content": chunk.content,
                        "section_order": chunk.section_order,
                        "word_count": chunk.word_count,
                        "embedding": str(emb),
                        "tags": chunk.tags,
                        "project_key": chunk.project_key,
                        "plan_type": chunk.plan_type,
                        "status": chunk.status,
                    },
                )

        await self._session.commit()
        return plan_id

    async def get_with_chunks(
        self, plan_id: UUID
    ) -> tuple[IndexedPlan, list[IndexedPlanChunk]] | None:
        plan_row = (
            await self._session.execute(
                text("SELECT * FROM indexed_plans WHERE id = :id"),
                {"id": plan_id},
            )
        ).mappings().first()

        if plan_row is None:
            return None

        chunk_rows = (
            await self._session.execute(
                text(
                    "SELECT * FROM indexed_plan_chunks "
                    "WHERE plan_id = :id ORDER BY section_order ASC"
                ),
                {"id": plan_id},
            )
        ).mappings().all()

        plan = IndexedPlan(**dict(plan_row))
        chunks = [IndexedPlanChunk(**dict(row)) for row in chunk_rows]
        return plan, chunks

    async def delete(self, plan_id: UUID) -> bool:
        result = await self._session.execute(
            text("DELETE FROM indexed_plans WHERE id = :id"),
            {"id": plan_id},
        )
        await self._session.commit()
        return (result.rowcount or 0) > 0
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/integration/test_plan_repo.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/repositories/pg_indexed_plan_repo.py tests/integration/test_plan_repo.py
git commit -m "feat(repo): PgIndexedPlanRepo upsert/get/delete with chunks"
```

---

## Task 6: Integrate chunker into PlanIndexer

**Files:**
- Modify: `src/brain_v42/services/plan_indexer.py`
- Create: `tests/integration/test_plan_indexer_chunking.py`

- [ ] **Step 1: Write failing integration test**

```python
# tests/integration/test_plan_indexer_chunking.py
"""Integration: PlanIndexer chunks plans end-to-end."""

from __future__ import annotations

from pathlib import Path

import pytest

from brain_v42.repositories.pg_indexed_plan_repo import PgIndexedPlanRepo
from brain_v42.services.plan_indexer import PlanIndexer


@pytest.mark.asyncio
async def test_plan_indexer_creates_chunks(
    db_session, tmp_path: Path, fake_embedding_client
):
    md = tmp_path / "my-design.md"
    md.write_text(
        "# My Design\n\nIntro.\n\n## Architecture\n\n"
        "Architecture body with enough words to exceed fifty. "
        * 20
        + "\n\n## Tests\n\nTests body with enough words. " * 20
    )

    indexer = PlanIndexer(
        session=db_session,
        embedding_client=fake_embedding_client,
    )
    stats = await indexer.index_file(
        md, project_key="brain-v42", plan_type="plan"
    )

    assert stats.indexed == 1
    assert stats.chunks_created == 2

    repo = PgIndexedPlanRepo(db_session)
    plan_row = (
        await db_session.execute(
            "SELECT id FROM indexed_plans WHERE file_path = :fp",
            {"fp": str(md)},
        )
    ).scalar_one()
    _, chunks = await repo.get_with_chunks(plan_row)
    titles = [c.section_title for c in chunks]
    assert titles == ["Architecture", "Tests"]


@pytest.mark.asyncio
async def test_plan_indexer_skips_unchanged_file(
    db_session, tmp_path, fake_embedding_client
):
    md = tmp_path / "stable.md"
    md.write_text("# Stable\n\n## A\n\n" + ("word " * 60))

    indexer = PlanIndexer(db_session, fake_embedding_client)
    first = await indexer.index_file(md, project_key="brain-v42", plan_type="plan")
    second = await indexer.index_file(md, project_key="brain-v42", plan_type="plan")

    assert first.indexed == 1
    assert second.indexed == 0
    assert second.skipped == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_plan_indexer_chunking.py -v`
Expected: FAIL (no `chunks_created` stat; no chunk rows created).

- [ ] **Step 3: Update PlanIndexer to call the chunker and persist chunks**

In `src/brain_v42/services/plan_indexer.py`, modify the indexing method. The method signature remains; the body now:
1. Reads the file, computes hash.
2. If hash unchanged → skip.
3. Calls `chunk_markdown(content)` → `(parent, chunks)`.
4. Embeds `[parent_embedding_input] + [chunk.content for chunk in chunks]` in a single GPU batch.
5. Builds `IndexedPlanCreate` and `IndexedPlanChunkCreate` lists.
6. Calls `PgIndexedPlanRepo.upsert_plan_with_chunks(...)`.
7. ClusterGuard feature linking continues on the parent.

Concrete change (apply inline to the existing `_process_file` / equivalent method):

```python
# Inside PlanIndexer.index_file / _process_file
from brain_v42.repositories.pg_indexed_plan_repo import PgIndexedPlanRepo
from brain_v42.services.plan_chunker import chunk_markdown
from brain_v42.models.indexed_plan import IndexedPlanCreate
from brain_v42.models.indexed_plan_chunk import IndexedPlanChunkCreate

# After reading file_content and computing content_hash:
parent, chunks = chunk_markdown(file_content)

# Build embedding inputs
parent_embed_input = (
    f"{parent.title}\n\n{parent.summary or parent.preamble}"
)
chunk_inputs = [c.content for c in chunks]
all_inputs = [parent_embed_input] + chunk_inputs

embeddings = await self._embedding_client.embed_batch(all_inputs)
parent_embedding = embeddings[0]
chunk_embeddings = embeddings[1:]

# Determine title: prefer frontmatter > chunker > filename fallback
title = parent.title or _strip_date_prefix(file_path.stem)

plan_create = IndexedPlanCreate(
    file_path=str(file_path),
    title=title[:500],
    plan_type=plan_type,
    project_key=project_key,
    content_hash=content_hash,
    content=parent.content,
    summary=parent.summary,
    status=parent.status,
    tags=parent.tags,
    metadata={"source_file": str(file_path)},
    chunk_count=len(chunks),
    word_count=parent.word_count,
)

chunk_creates = [
    IndexedPlanChunkCreate(
        plan_id=UUID(int=0),  # placeholder, repo ignores
        section_title=c.section_title[:500],
        section_path=c.section_path[:1000],
        content=c.content,
        section_order=c.section_order,
        word_count=c.word_count,
        project_key=project_key,
        plan_type=plan_type,
        status=parent.status,
        tags=parent.tags,
    )
    for c in chunks
]

repo = PgIndexedPlanRepo(self._session)
plan_id = await repo.upsert_plan_with_chunks(
    plan_create, parent_embedding, chunk_creates, chunk_embeddings
)

# Existing ClusterGuard linking continues here, operating on plan_id + title + parent_embedding.

stats.indexed += 1
stats.chunks_created += len(chunks)
```

Also extend the stats dataclass in the same file to include `chunks_created: int = 0`. Update the return schema exposed via `brain_reindex_plans` to include this field.

- [ ] **Step 4: Run tests**

Run: `pytest tests/integration/test_plan_indexer_chunking.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full plan_indexer test module to catch regressions**

Run: `pytest tests/unit/test_plan_indexer.py tests/integration/test_plan_indexer_chunking.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/services/plan_indexer.py tests/integration/test_plan_indexer_chunking.py
git commit -m "feat(plan-indexer): chunk markdown and persist chunks on index"
```

---

## Task 7: HybridSearcher — add plan chunks as a searchable source

**Files:**
- Modify: `src/brain_v42/services/search/hybrid.py`
- Create: `tests/integration/test_plan_search_integration.py`

- [ ] **Step 1: Write failing integration test**

```python
# tests/integration/test_plan_search_integration.py
"""Integration: brain_search returns plan chunks."""

from __future__ import annotations

import pytest

from brain_v42.repositories.pg_indexed_plan_repo import PgIndexedPlanRepo
from brain_v42.services.brain_service import BrainService
from brain_v42.models.indexed_plan import IndexedPlanCreate
from brain_v42.models.indexed_plan_chunk import IndexedPlanChunkCreate


@pytest.mark.asyncio
async def test_brain_search_returns_plan_chunks(
    db_session, brain_service: BrainService
):
    repo = PgIndexedPlanRepo(db_session)
    plan_embed = [0.10] * 1536
    chunk_embed = [0.11] * 1536

    plan = IndexedPlanCreate(
        file_path="/tmp/search-plan.md",
        title="Neo4j Knowledge Graph",
        plan_type="plan",
        project_key="brain-v42",
        content_hash="h" * 64,
        content="# Neo4j Knowledge Graph\n\n## Traversals\n\n"
        + ("traversal words " * 40),
        chunk_count=1,
        word_count=80,
    )
    chunk = IndexedPlanChunkCreate(
        section_title="Traversals",
        section_path="Traversals",
        content="## Traversals\n\n" + ("traversal words " * 40),
        section_order=0,
        word_count=80,
        project_key="brain-v42",
        plan_type="plan",
        tags=["neo4j"],
    )
    await repo.upsert_plan_with_chunks(plan, plan_embed, [chunk], [chunk_embed])

    results = await brain_service.search(
        query="neo4j traversals",
        types=["plan"],
        project_key="brain-v42",
        limit=5,
    )

    assert any(
        r.type == "plan" and r.title == "Traversals" for r in results
    )
    plan_results = [r for r in results if r.type == "plan"]
    assert plan_results[0].parent_id is not None  # chunk knows its parent


@pytest.mark.asyncio
async def test_brain_search_filters_drafts_by_default(
    db_session, brain_service: BrainService
):
    repo = PgIndexedPlanRepo(db_session)
    for status, title in [("draft", "Draft Plan"), ("active", "Active Plan")]:
        plan = IndexedPlanCreate(
            file_path=f"/tmp/{status}.md",
            title=title,
            plan_type="plan",
            project_key="brain-v42",
            content_hash=f"{status}" * 8 + "a" * 56,
            content=f"# {title}\n\n## Body\n\n" + ("keyword " * 60),
            status=status,
            chunk_count=1,
            word_count=60,
        )
        chunk = IndexedPlanChunkCreate(
            section_title="Body",
            section_path="Body",
            content="## Body\n\n" + ("keyword " * 60),
            section_order=0,
            word_count=60,
            project_key="brain-v42",
            plan_type="plan",
            status=status,
        )
        await repo.upsert_plan_with_chunks(
            plan, [0.1] * 1536, [chunk], [[0.1] * 1536]
        )

    results = await brain_service.search(
        query="keyword", types=["plan"], project_key="brain-v42", limit=10
    )
    titles = [r.title for r in results if r.type == "plan"]
    assert "Body" in titles
    # Only one match expected — drafts excluded
    assert len([t for t in titles if t == "Body"]) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_plan_search_integration.py -v`
Expected: FAIL (plan chunks not in search output).

- [ ] **Step 3: Extend HybridSearcher**

In `src/brain_v42/services/search/hybrid.py`, add a new method and register it in the global search dispatch:

```python
# src/brain_v42/services/search/hybrid.py (additions — keep existing code)

async def _search_plan_chunks(
    self,
    query: str,
    query_embedding: list[float],
    project_key: str | None,
    tags: list[str] | None,
    limit: int,
    include_drafts: bool = False,
) -> list[SearchHit]:
    """Hybrid search across indexed_plan_chunks."""
    status_clause = "AND status = 'active'" if not include_drafts else ""
    project_clause = "AND project_key = :project_key" if project_key else ""
    tags_clause = "AND tags && CAST(:tags AS VARCHAR[])" if tags else ""

    # Vector search
    vec_sql = f"""
        SELECT id, plan_id, section_title, section_path, content, tags,
               project_key, 1 - (embedding <=> :emb) AS score
        FROM indexed_plan_chunks
        WHERE 1=1 {status_clause} {project_clause} {tags_clause}
        ORDER BY embedding <=> :emb
        LIMIT :limit
    """
    fts_sql = f"""
        SELECT id, plan_id, section_title, section_path, content, tags,
               project_key, ts_rank(search_vector, plainto_tsquery('english', :q)) AS score
        FROM indexed_plan_chunks
        WHERE search_vector @@ plainto_tsquery('english', :q)
              {status_clause} {project_clause} {tags_clause}
        ORDER BY score DESC
        LIMIT :limit
    """

    params = {"emb": str(query_embedding), "q": query, "limit": limit}
    if project_key:
        params["project_key"] = project_key
    if tags:
        params["tags"] = tags

    vec_rows = (await self._session.execute(text(vec_sql), params)).mappings().all()
    fts_rows = (await self._session.execute(text(fts_sql), params)).mappings().all()

    return self._rrf_fuse_plan_rows(vec_rows, fts_rows, limit)


def _rrf_fuse_plan_rows(self, vec_rows, fts_rows, limit: int) -> list[SearchHit]:
    # Reuse existing RRF fusion utility
    fused = self._rrf_merge(
        {"vec": vec_rows, "fts": fts_rows},
        id_key="id",
    )
    hits: list[SearchHit] = []
    for row in fused[:limit]:
        hits.append(
            SearchHit(
                id=row["id"],
                type="plan",
                title=row["section_title"],
                content=row["content"],
                tags=list(row["tags"] or []),
                project_key=row["project_key"],
                parent_id=row["plan_id"],
                score=row.get("_rrf_score", 0.0),
            )
        )
    return hits
```

Then update the global search orchestration (the method that `asyncio.gather`s all types) to include `"plan"` in its type dispatch and call `_search_plan_chunks`. Also extend the `SearchHit` dataclass/model with an optional `parent_id: UUID | None = None`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/integration/test_plan_search_integration.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/services/search/hybrid.py tests/integration/test_plan_search_integration.py
git commit -m "feat(search): hybrid search across indexed_plan_chunks"
```

---

## Task 8: brain_search MCP tool — accept "plan" in types

**Files:**
- Modify: `src/brain_v42/mcp/tools/brain_tools.py`
- Create: `tests/unit/mcp/tools/test_brain_search_plan_type.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/mcp/tools/test_brain_search_plan_type.py
"""Ensure brain_search MCP accepts 'plan' in types."""

import pytest

from brain_v42.mcp.tools.brain_tools import VALID_SEARCH_TYPES


def test_plan_is_a_valid_search_type():
    assert "plan" in VALID_SEARCH_TYPES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/mcp/tools/test_brain_search_plan_type.py -v`
Expected: FAIL (`"plan"` not in the set).

- [ ] **Step 3: Update brain_search to allow "plan"**

In `src/brain_v42/mcp/tools/brain_tools.py`, locate the validation list/set for the `types` parameter (typically named `VALID_SEARCH_TYPES` or similar; if inlined, extract to a module-level constant). Ensure the set is:

```python
VALID_SEARCH_TYPES = frozenset(
    {"decision", "learning", "snippet", "runbook", "adr", "plan"}
)
```

And that the `brain_search` tool docstring mentions `"plan"` as a valid value.

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/mcp/tools/test_brain_search_plan_type.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/mcp/tools/brain_tools.py tests/unit/mcp/tools/test_brain_search_plan_type.py
git commit -m "feat(mcp): brain_search accepts 'plan' type"
```

---

## Task 9: brain_get / brain_delete — dispatch "plan"

**Files:**
- Modify: `src/brain_v42/mcp/tools/crud_tools.py`
- Create: `tests/integration/test_plan_get_delete_integration.py`

- [ ] **Step 1: Write failing integration test**

```python
# tests/integration/test_plan_get_delete_integration.py
"""Integration: brain_get and brain_delete handle entity_type='plan'."""

import json

import pytest

from brain_v42.mcp.tools.crud_tools import brain_delete, brain_get
from brain_v42.models.indexed_plan import IndexedPlanCreate
from brain_v42.models.indexed_plan_chunk import IndexedPlanChunkCreate
from brain_v42.repositories.pg_indexed_plan_repo import PgIndexedPlanRepo


@pytest.mark.asyncio
async def test_brain_get_returns_plan_with_chunks(db_session):
    repo = PgIndexedPlanRepo(db_session)
    plan = IndexedPlanCreate(
        file_path="/tmp/get-plan.md",
        title="Get Plan",
        plan_type="plan",
        project_key="brain-v42",
        content_hash="h" * 64,
        content="# Get Plan\n\n## Section\n\n" + ("word " * 60),
        chunk_count=1,
        word_count=62,
    )
    chunk = IndexedPlanChunkCreate(
        section_title="Section",
        section_path="Section",
        content="## Section\n\n" + ("word " * 60),
        section_order=0,
        word_count=60,
        project_key="brain-v42",
        plan_type="plan",
    )
    plan_id = await repo.upsert_plan_with_chunks(
        plan, [0.1] * 1536, [chunk], [[0.1] * 1536]
    )

    raw = await brain_get(entity_type="plan", entity_id=str(plan_id))
    payload = json.loads(raw)
    assert payload["title"] == "Get Plan"
    assert payload["chunk_count"] == 1
    assert len(payload["chunks"]) == 1
    assert payload["chunks"][0]["section_title"] == "Section"


@pytest.mark.asyncio
async def test_brain_delete_plan_cascades(db_session):
    repo = PgIndexedPlanRepo(db_session)
    plan = IndexedPlanCreate(
        file_path="/tmp/del-plan.md",
        title="Del",
        plan_type="plan",
        project_key="brain-v42",
        content_hash="h" * 64,
        content="# Del\n\n## S\n\n" + ("word " * 60),
        chunk_count=1,
        word_count=62,
    )
    chunk = IndexedPlanChunkCreate(
        section_title="S",
        section_path="S",
        content="## S\n\n" + ("word " * 60),
        section_order=0,
        word_count=60,
        project_key="brain-v42",
        plan_type="plan",
    )
    plan_id = await repo.upsert_plan_with_chunks(
        plan, [0.1] * 1536, [chunk], [[0.1] * 1536]
    )

    raw = await brain_delete(entity_type="plan", entity_id=str(plan_id))
    payload = json.loads(raw)
    assert payload["deleted"] is True

    result = await repo.get_with_chunks(plan_id)
    assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_plan_get_delete_integration.py -v`
Expected: FAIL (`entity_type="plan"` not routed).

- [ ] **Step 3: Extend brain_get and brain_delete dispatchers**

In `src/brain_v42/mcp/tools/crud_tools.py`, locate the `entity_type` dispatch in `brain_get` and `brain_delete`. Add a new branch for `"plan"` that calls `PgIndexedPlanRepo`:

```python
# At the top of the file
from brain_v42.repositories.pg_indexed_plan_repo import PgIndexedPlanRepo

# Inside brain_get, after existing type branches:
elif entity_type == "plan":
    async with get_session() as session:
        repo = PgIndexedPlanRepo(session)
        result = await repo.get_with_chunks(UUID(entity_id))
        if result is None:
            return json.dumps({"error": "plan not found"})
        plan, chunks = result
        payload = plan.model_dump(mode="json")
        payload["chunks"] = [c.model_dump(mode="json") for c in chunks]
        return json.dumps(payload, default=str)

# Inside brain_delete, after existing type branches:
elif entity_type == "plan":
    async with get_session() as session:
        repo = PgIndexedPlanRepo(session)
        deleted = await repo.delete(UUID(entity_id))
        return json.dumps({"deleted": deleted})
```

Also update the `brain_update` branch if one exists — for now, add an explicit error:

```python
elif entity_type == "plan":
    return json.dumps({
        "error": "plans are immutable; re-run brain_reindex_plans to refresh",
    })
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/integration/test_plan_get_delete_integration.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/mcp/tools/crud_tools.py tests/integration/test_plan_get_delete_integration.py
git commit -m "feat(mcp): brain_get/delete dispatch plan entity type"
```

---

## Task 10: Non-regression — existing brain_reindex_plans still works

**Files:**
- Create: `tests/integration/test_plan_indexer_non_regression.py`

- [ ] **Step 1: Write the test**

```python
# tests/integration/test_plan_indexer_non_regression.py
"""Ensure brain_reindex_plans behaviour (scan + skip + feature link) is preserved."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain_v42.mcp.tools.plan_tools import brain_reindex_plans


@pytest.mark.asyncio
async def test_reindex_scans_and_returns_stats_shape(
    db_session, tmp_path: Path, configured_project
):
    # configured_project fixture writes plan_scan_paths into project_contexts
    (tmp_path / "a-design.md").write_text(
        "# A\n\n## Section\n\n" + ("word " * 60)
    )
    (tmp_path / "b-plan.md").write_text(
        "# B\n\n## Section\n\n" + ("word " * 60)
    )

    raw = await brain_reindex_plans(project_key="brain-v42")
    stats = json.loads(raw)

    assert "indexed" in stats
    assert "skipped" in stats
    assert "chunks_created" in stats  # new field
    assert stats["indexed"] >= 2
    assert stats["chunks_created"] >= 2


@pytest.mark.asyncio
async def test_reindex_skips_unchanged_on_second_run(
    db_session, tmp_path, configured_project
):
    (tmp_path / "c-design.md").write_text(
        "# C\n\n## Section\n\n" + ("word " * 60)
    )
    first = json.loads(await brain_reindex_plans(project_key="brain-v42"))
    second = json.loads(await brain_reindex_plans(project_key="brain-v42"))

    assert first["indexed"] >= 1
    assert second["indexed"] == 0
    assert second["skipped"] >= 1
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/integration/test_plan_indexer_non_regression.py -v`
Expected: PASS (all dispatch and chunking already in place from earlier tasks).

- [ ] **Step 3: Run the full relevant test suite**

Run:
```bash
pytest tests/unit/services/test_plan_chunker.py \
       tests/unit/services/test_plan_chunker_edge_cases.py \
       tests/unit/models/test_indexed_plan_chunk_model.py \
       tests/unit/mcp/tools/test_brain_search_plan_type.py \
       tests/integration/test_plan_repo.py \
       tests/integration/test_plan_indexer_chunking.py \
       tests/integration/test_plan_search_integration.py \
       tests/integration/test_plan_get_delete_integration.py \
       tests/integration/test_plan_indexer_non_regression.py \
       tests/unit/test_plan_indexer.py \
       -v
```
Expected: All pass.

- [ ] **Step 4: Run the full project test suite**

Run: `pytest --cov=brain_v42 --cov-report=term-missing`
Expected: All pass. Coverage ≥ 60% project-wide, ≥ 80% for new modules.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_plan_indexer_non_regression.py
git commit -m "test(plan-indexer): non-regression on reindex flow + chunks stat"
```

---

## Task 11: Index this project's own plans as a smoke test

**Files:** None created; uses live services.

- [ ] **Step 1: Verify project_contexts has plan_scan_paths for brain-v42**

Run:
```bash
psql "postgresql://brain:brain@localhost:5433/brain" \
  -c "SELECT project_key, plan_scan_paths FROM project_contexts WHERE project_key = 'brain-v42'"
```
Expected: A row with `plan_scan_paths` containing `docs/superpowers/specs` and `docs/superpowers/plans` (or equivalent). If empty, update it via existing project-context tools.

- [ ] **Step 2: Run the MCP tool manually via a Python one-shot**

Run:
```bash
uv run python -c "
import asyncio
from brain_v42.mcp.tools.plan_tools import brain_reindex_plans
print(asyncio.run(brain_reindex_plans(project_key='brain-v42')))
"
```
Expected: JSON stats showing `indexed > 0`, `chunks_created > 0`.

- [ ] **Step 3: Verify via brain_search**

Run:
```bash
uv run python -c "
import asyncio
from brain_v42.services.brain_service import BrainService
# (instantiate with your usual fixture / entry point)
"
```

Or from any Claude Code session: `brain_search(query='plan chunking design', types=['plan'])`. Expected: the freshly-indexed design doc appears in the results with `parent_id` set.

- [ ] **Step 4: Commit (no code, stats only)**

No commit needed for Task 11 — it's a smoke test.

---

## Post-Implementation

- Update `CLAUDE.md` `Focus:` line to reflect plan chunking shipped.
- Log the decision via `brain_log_decision`: "chose to extend indexed_plans in-place rather than create a 6th entity type" with the reasoning.
- Save the `plan_chunker` module as a snippet via `brain_save_snippet` if the parsing logic is reusable.
